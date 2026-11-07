#!/usr/bin/env python3
"""Audit OEM packages and run an opt-in, loopback-only telemetry sinkhole.

The DNS proxy enforces drops only for clients configured to use it. The packet
sink discards traffic only when that traffic is explicitly routed to its port.
Neither listener changes Android DNS settings or host firewall rules.
"""

import argparse
import ipaddress
import json
import re
import socket
import socketserver
import struct
import subprocess
import sys
import threading
from pathlib import Path


DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "var/system-telemetry-blackhole"
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}\Z")
SERIAL = re.compile(r"[A-Za-z0-9._:-]+\Z")

# Candidate endpoints come from the existing Android privacy guard. Package
# presence alone does not establish that a device contacted these domains.
OEM_PACKAGES = {
    "com.miui.daemon": ("telemetry candidate", ("data.mistat.xiaomi.com",)),
    "com.xiaomi.barrage": ("telemetry candidate", ()),
    "com.miui.analytics": ("review", ()),
    "com.miui.misightservice": ("review", ()),
    "com.xiaomi.security.onetrack": ("review", ()),
    "com.miui.msa.global": ("review", ()),
    "com.miui.securitycenter": ("protected; review", ()),
    "com.qualcomm.qti.services.systemhelper": ("protected; review", ()),
}


class ADBError(RuntimeError):
    pass


def run_adb(args, serial=None, runner=subprocess.run):
    command = ["adb"] + (["-s", serial] if serial else []) + list(args)
    try:
        result = runner(command, capture_output=True, text=True, timeout=20, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise ADBError(str(error)) from error
    if result.returncode:
        raise ADBError((result.stderr or result.stdout).strip() or "ADB failed")
    return result.stdout


def connected_devices(runner=subprocess.run):
    devices = []
    for line in run_adb(["devices", "-l"], runner=runner).splitlines():
        fields = line.split()
        if len(fields) > 1 and fields[1] == "device" and SERIAL.fullmatch(fields[0]):
            devices.append(fields[0])
    return devices


def parse_packages(output):
    return {name for line in output.splitlines() if line.startswith("package:")
            for name in [line.removeprefix("package:").strip()] if PACKAGE.fullmatch(name)}


def normalize_domains(domains):
    normalized = set()
    for raw in domains:
        if not isinstance(raw, str):
            raise ValueError("invalid DNS drop domain")
        domain = raw.lower().rstrip(".")
        if not DOMAIN.fullmatch(domain):
            raise ValueError(f"invalid DNS drop domain: {raw}")
        normalized.add(domain)
    return sorted(normalized)


def build_audit(packages, disabled=(), extra_domains=(), source="mock", serial=None):
    findings = []
    candidates = set(extra_domains)
    for package in sorted(packages):
        if package not in OEM_PACKAGES:
            continue
        category, domains = OEM_PACKAGES[package]
        findings.append({"package": package, "category": category,
                         "disabled_for_user": package in disabled,
                         "candidate_domains": list(domains)})
        if package not in disabled:
            candidates.update(domains)
    return {"source": source, "serial": serial, "package_count": len(packages),
            "findings": findings, "drop_domains": normalize_domains(candidates),
            "scope": "DNS drops require clients to use this loopback proxy; packet drops require explicit routing."}


def render_dns_rules(domains):
    return "".join(f"||{domain}^\n" for domain in normalize_domains(domains))


def parse_endpoint(value):
    host, separator, raw_port = value.rpartition(":")
    if not separator:
        raise ValueError("upstream must be an IPv4 address and port, e.g. 1.1.1.1:53")
    try:
        ipaddress.IPv4Address(host)
        port = int(raw_port)
    except ValueError as error:
        raise ValueError("invalid upstream IPv4 address or port") from error
    if not 1 <= port <= 65535:
        raise ValueError("upstream port must be between 1 and 65535")
    return host, port


def read_question(packet):
    if len(packet) < 12:
        raise ValueError("short DNS header")
    _, flags, questions, _, _, _ = struct.unpack("!HHHHHH", packet[:12])
    if flags & 0x8000 or flags & 0x7800 or questions != 1:
        raise ValueError("unsupported DNS query")
    labels = []
    offset = 12
    end = None
    seen = set()
    while True:
        if offset >= len(packet) or offset in seen:
            raise ValueError("invalid DNS name")
        seen.add(offset)
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("short DNS pointer")
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            if pointer >= offset:
                raise ValueError("invalid DNS pointer")
            if end is None:
                end = offset + 2
            offset = pointer
            continue
        if length & 0xC0 or length > 63:
            raise ValueError("invalid DNS label")
        offset += 1
        if length == 0:
            end = offset if end is None else end
            break
        if offset + length > len(packet):
            raise ValueError("short DNS label")
        try:
            labels.append(packet[offset:offset + length].decode("ascii").lower())
        except UnicodeDecodeError as error:
            raise ValueError("non-ASCII DNS label") from error
        offset += length
        if sum(map(len, labels)) + len(labels) > 255:
            raise ValueError("DNS name too long")
    if end + 4 > len(packet):
        raise ValueError("short DNS question")
    return ".".join(labels), end + 4


def dns_error(packet, code, question_end=12):
    if len(packet) < 12:
        return b""
    request_flags = struct.unpack("!H", packet[2:4])[0]
    flags = 0x8000 | (request_flags & 0x0100) | 0x0080 | code
    questions = 1 if question_end > 12 else 0
    return struct.pack("!HHHHHH", int.from_bytes(packet[:2], "big"), flags,
                       questions, 0, 0, 0) + packet[12:question_end]


class Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {"dns_dropped": 0, "dns_forwarded": 0, "dns_errors": 0,
                       "tcp_packets": 0, "udp_packets": 0, "sink_bytes": 0}

    def add(self, key, value=1):
        with self.lock:
            self.values[key] += value

    def snapshot(self):
        with self.lock:
            return dict(self.values)


class DNSProxy:
    def __init__(self, domains, upstream, counters=None, timeout=3):
        self.domains = frozenset(normalize_domains(domains))
        self.upstream = upstream
        self.counters = counters or Counters()
        self.timeout = timeout

    def blocked(self, name):
        return any(name == domain or name.endswith("." + domain) for domain in self.domains)

    def handle(self, packet, tcp=False):
        try:
            name, question_end = read_question(packet)
        except ValueError:
            self.counters.add("dns_errors")
            return dns_error(packet, 1)
        if self.blocked(name):
            self.counters.add("dns_dropped")
            return dns_error(packet, 3, question_end)
        try:
            if tcp:
                with socket.create_connection(self.upstream, self.timeout) as connection:
                    connection.settimeout(self.timeout)
                    connection.sendall(struct.pack("!H", len(packet)) + packet)
                    size = struct.unpack("!H", recv_exact(connection, 2))[0]
                    response = recv_exact(connection, size)
            else:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                    connection.settimeout(self.timeout)
                    connection.connect(self.upstream)
                    connection.send(packet)
                    response = connection.recv(65535)
            if len(response) < 12 or response[:2] != packet[:2] or not response[2] & 0x80:
                raise ValueError("invalid upstream DNS response")
        except (OSError, ValueError):
            self.counters.add("dns_errors")
            return dns_error(packet, 2, question_end)
        self.counters.add("dns_forwarded")
        return response


def recv_exact(connection, size):
    chunks = []
    while size:
        chunk = connection.recv(size)
        if not chunk:
            raise OSError("connection closed before DNS message completed")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


class ThreadedUDPServer(socketserver.ThreadingMixIn, socketserver.UDPServer):
    daemon_threads = True
    allow_reuse_address = True


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class DNSUDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        packet, connection = self.request
        response = self.server.proxy.handle(packet)
        if response:
            connection.sendto(response, self.client_address)


class DNSTCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(self.server.proxy.timeout)
        try:
            size = struct.unpack("!H", recv_exact(self.request, 2))[0]
            response = self.server.proxy.handle(recv_exact(self.request, size), tcp=True)
            if response:
                self.request.sendall(struct.pack("!H", len(response)) + response)
        except (OSError, struct.error):
            self.server.proxy.counters.add("dns_errors")


class SinkUDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        payload, _ = self.request
        self.server.counters.add("udp_packets")
        self.server.counters.add("sink_bytes", len(payload))


class SinkTCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        self.server.counters.add("tcp_packets")
        try:
            while chunk := self.request.recv(65536):
                self.server.counters.add("sink_bytes", len(chunk))
        except OSError:
            pass


class BlackholeDaemon:
    def __init__(self, listen, dns_port, sink_port, proxy):
        if not ipaddress.IPv4Address(listen).is_loopback:
            raise ValueError("listeners must bind to IPv4 loopback")
        if dns_port == sink_port:
            raise ValueError("DNS and sink ports must differ")
        self.servers = []
        try:
            for server_class, handler, port in (
                (ThreadedUDPServer, DNSUDPHandler, dns_port),
                (ThreadedTCPServer, DNSTCPHandler, dns_port),
                (ThreadedUDPServer, SinkUDPHandler, sink_port),
                (ThreadedTCPServer, SinkTCPHandler, sink_port),
            ):
                server = server_class((listen, port), handler)
                server.proxy = proxy
                server.counters = proxy.counters
                self.servers.append(server)
        except OSError:
            self.close()
            raise
        self.threads = []

    def start(self):
        for server in self.servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        for server in self.servers:
            if getattr(self, "threads", None):
                server.shutdown()
            server.server_close()
        for thread in getattr(self, "threads", ()):
            thread.join()


def main(argv=None, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="run local DNS and packet sink listeners")
    parser.add_argument("--dry-run", action="store_true", help="print audit without writing or listening")
    parser.add_argument("--mock-packages", type=Path, help="offline pm-list fixture; never calls ADB")
    parser.add_argument("--serial", help="ADB serial when multiple devices are connected")
    parser.add_argument("--domain", action="append", default=[], help="extra DNS drop domain")
    parser.add_argument("--upstream", help="upstream DNS IPv4 address:port (required for --serve)")
    parser.add_argument("--listen", default="127.0.0.1", help="IPv4 loopback bind address")
    parser.add_argument("--dns-port", type=int, default=5353)
    parser.add_argument("--sink-port", type=int, default=9377)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.serve and not args.upstream and not args.dry_run:
        parser.error("--serve requires --upstream")
    if args.serial and not SERIAL.fullmatch(args.serial):
        parser.error("invalid ADB serial")
    try:
        if not ipaddress.IPv4Address(args.listen).is_loopback:
            raise ValueError("--listen must be IPv4 loopback")
        if not 1 <= args.dns_port <= 65535 or not 1 <= args.sink_port <= 65535:
            raise ValueError("listener ports must be between 1 and 65535")
        if args.dns_port == args.sink_port:
            raise ValueError("DNS and sink ports must differ")
        upstream = parse_endpoint(args.upstream) if args.upstream else None
        if args.serve and upstream == (args.listen, args.dns_port):
            raise ValueError("upstream cannot point to the DNS listener")
        if args.mock_packages:
            packages = parse_packages(args.mock_packages.read_text(encoding="utf-8"))
            disabled, source, serial = set(), "mock", None
        else:
            try:
                devices = connected_devices(runner)
            except ADBError:
                if args.serial:
                    raise
                devices = []
            if args.serial and args.serial not in devices:
                raise ADBError("selected device is not connected and authorized")
            if len(devices) > 1 and not args.serial:
                raise ADBError("multiple devices connected; specify --serial")
            serial = args.serial or (devices[0] if devices else None)
            packages = parse_packages(run_adb(["shell", "pm", "list", "packages", "--user", "0"],
                                             serial, runner)) if serial else set()
            disabled = parse_packages(run_adb(["shell", "pm", "list", "packages", "-d",
                                               "--user", "0"], serial, runner)) if serial else set()
            source = "adb" if serial else "none"
        audit = build_audit(packages, disabled, args.domain, source, serial)
        if args.dry_run:
            print(json.dumps(audit, indent=2))
            return 0
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
        (args.output / "dns-drop.txt").write_text(render_dns_rules(audit["drop_domains"]), encoding="utf-8")
        if not args.serve:
            print(json.dumps(audit, indent=2))
            return 0
        proxy = DNSProxy(audit["drop_domains"], upstream)
        daemon = BlackholeDaemon(args.listen, args.dns_port, args.sink_port, proxy)
        try:
            daemon.start()
            print(f"Listening on {args.listen}: DNS {args.dns_port}, packet sink {args.sink_port}", flush=True)
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            daemon.close()
        return 0
    except (ADBError, OSError, ValueError) as error:
        print(f"system telemetry blackhole: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
