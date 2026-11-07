#!/usr/bin/env python3
"""Desktop Leak Sentinel.

Audits outbound socket connections, sanitizes clipboard/text PII,
and verifies workstation air-gap compliance. Pure Python standard library.
"""

import argparse
import ipaddress
import json
import os
import re
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# Regex patterns for sensitive credentials & PII
PATTERNS = {
    "api_key": re.compile(r"sk-[a-zA-Z0-9_-]{20,}", re.IGNORECASE),
    "eth_private_key": re.compile(r"\b0x[a-fA-F0-9]{64}\b"),
    "jwt_token": re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"),
    "protected_email": re.compile(r"\b[a-zA-Z0-9._%+-]+@(gmail|google|corp|internal)\.[a-zA-Z]{2,}\b", re.IGNORECASE),
    "ipv4_private": re.compile(r"\b(?:10|127|172\.(?:1[6-9]|2[0-9]|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"),
}

DEFAULT_ALLOWED_PORTS = {53, 443, 80, 7890, 5173, 5174, 15721, 3001}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def decode_hex_ipv4(hex_addr: str) -> tuple[str, int]:
    """Decodes hex address and port from /proc/net/tcp format."""
    try:
        ip_hex, port_hex = hex_addr.split(":")
        ip_int = int(ip_hex, 16)
        # /proc/net/tcp stores IPv4 in little-endian format
        ip_bytes = struct.pack("<I", ip_int)
        ip_str = socket.inet_ntoa(ip_bytes)
        port = int(port_hex, 16)
        return ip_str, port
    except Exception as e:
        raise ValueError(f"Invalid /proc/net/tcp hex address '{hex_addr}': {e}")


def parse_proc_net_tcp(content: str) -> list[dict]:
    """Parses /proc/net/tcp content into structured connection objects."""
    connections = []
    lines = content.strip().splitlines()
    if not lines:
        return connections

    # First line is header
    for line in lines[1:]:
        parts = line.strip().split()
        if len(parts) < 10:
            continue
        try:
            local_ip, local_port = decode_hex_ipv4(parts[1])
            remote_ip, remote_port = decode_hex_ipv4(parts[2])
            state_hex = parts[3]
            # State 01 = ESTABLISHED
            state_desc = "ESTABLISHED" if state_hex == "01" else f"STATE_{state_hex}"
            connections.append({
                "local_ip": local_ip,
                "local_port": local_port,
                "remote_ip": remote_ip,
                "remote_port": remote_port,
                "state": state_desc,
                "raw_state": state_hex,
            })
        except Exception:
            continue
    return connections


class ConnectionSentinel:
    """Monitors and audits active network connections for unauthorized outbound leaks."""

    def __init__(self, allowed_ports: set[int] = None, allow_private: bool = True):
        self.allowed_ports = allowed_ports or DEFAULT_ALLOWED_PORTS
        self.allow_private = allow_private

    def is_ip_allowed(self, ip_str: str) -> bool:
        """Determines if a destination IP address is permitted under air-gap policies."""
        try:
            ip = ipaddress.ip_address(ip_str)
            if ip.is_loopback:
                return True
            if self.allow_private and (ip.is_private or ip.is_link_local):
                return True
            return False
        except ValueError:
            return False

    def audit_connections(self, proc_content: str = None) -> dict:
        """Audits connections from /proc/net/tcp content or reads from host procfs."""
        if proc_content is None:
            proc_path = Path("/proc/net/tcp")
            if proc_path.exists():
                try:
                    proc_content = proc_path.read_text()
                except Exception:
                    proc_content = ""
            else:
                proc_content = ""

        all_conns = parse_proc_net_tcp(proc_content)
        suspicious = []
        compliant = []

        for conn in all_conns:
            # We are interested in established or outbound connections
            if conn["state"] != "ESTABLISHED":
                continue
            rem_ip = conn["remote_ip"]
            rem_port = conn["remote_port"]

            if self.is_ip_allowed(rem_ip) and rem_port in self.allowed_ports:
                compliant.append(conn)
            elif self.is_ip_allowed(rem_ip):
                compliant.append(conn)
            else:
                suspicious.append(conn)

        return {
            "timestamp": utc_now(),
            "total_established": len(compliant) + len(suspicious),
            "compliant_count": len(compliant),
            "suspicious_count": len(suspicious),
            "suspicious_connections": suspicious,
            "compliant_connections": compliant,
        }


class ClipboardSanitizer:
    """Detects and redacts secrets and PII from text streams or clipboard."""

    @classmethod
    def sanitize(cls, text: str) -> tuple[str, list[dict]]:
        """Replaces detected PII and credentials with redaction markers."""
        findings = []
        result = text

        # Redact Ethereum / Web3 Private Keys
        for match in PATTERNS["eth_private_key"].finditer(text):
            val = match.group(0)
            findings.append({"type": "eth_private_key", "snippet": val[:6] + "..." + val[-4:]})
            result = result.replace(val, "[REDACTED_ETH_PRIVKEY]")

        # Redact API Keys
        for match in PATTERNS["api_key"].finditer(text):
            val = match.group(0)
            findings.append({"type": "api_key", "snippet": val[:7] + "..."})
            result = result.replace(val, "[REDACTED_API_KEY]")

        # Redact JWTs
        for match in PATTERNS["jwt_token"].finditer(text):
            val = match.group(0)
            findings.append({"type": "jwt_token", "snippet": val[:10] + "..."})
            result = result.replace(val, "[REDACTED_JWT_TOKEN]")

        # Redact Protected Emails
        for match in PATTERNS["protected_email"].finditer(text):
            val = match.group(0)
            findings.append({"type": "protected_email", "snippet": val[:3] + "***@" + val.split("@")[1]})
            result = result.replace(val, "[REDACTED_EMAIL]")

        return result, findings


class AirGapComplianceVerifier:
    """Validates local environment variables and system state against leak risks."""

    SENSITIVE_ENV_VARS = {
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN", "GH_TOKEN", "PRIVATE_KEY", "MNEMONIC", "SEED_PHRASE",
    }

    @classmethod
    def audit_environment(cls, env: dict = None) -> list[str]:
        target_env = env if env is not None else os.environ
        leaks = []
        for key, val in target_env.items():
            if key.upper() in cls.SENSITIVE_ENV_VARS and val.strip():
                leaks.append(f"Sensitive credential leaked in env: {key}")
            elif any(pat.search(val) for pat in [PATTERNS["eth_private_key"], PATTERNS["api_key"]]):
                leaks.append(f"Secret pattern discovered in env value: {key}")
        return leaks

    @classmethod
    def full_audit(cls, workdir: Path = None, env: dict = None, proc_content: str = None) -> dict:
        env_leaks = cls.audit_environment(env)
        sentinel = ConnectionSentinel()
        conn_report = sentinel.audit_connections(proc_content)

        is_compliant = len(env_leaks) == 0 and conn_report["suspicious_count"] == 0
        return {
            "timestamp": utc_now(),
            "compliant": is_compliant,
            "environment_leaks": env_leaks,
            "network_audit": conn_report,
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Desktop Leak Sentinel")
    parser.add_argument("--scan-connections", action="store_true", help="Audit active network connections")
    parser.add_argument("--sanitize-text", type=str, help="Sanitize provided text and output redacted version")
    parser.add_argument("--audit-compliance", action="store_true", help="Run full workstation air-gap compliance check")
    parser.add_argument("--output", type=Path, help="Save report JSON to path")
    parser.add_argument("--dry-run", action="store_true", help="Audit without side effects")

    args = parser.parse_args(argv)

    report = {}
    if args.scan_connections or args.audit_compliance or not sys.argv[1:]:
        sentinel = ConnectionSentinel()
        report["network"] = sentinel.audit_connections()

    if args.sanitize_text:
        redacted, findings = ClipboardSanitizer.sanitize(args.sanitize_text)
        report["sanitization"] = {
            "redacted_text": redacted,
            "findings_count": len(findings),
            "findings": findings,
        }
        print(f"Redacted:\n{redacted}")

    if args.audit_compliance or not sys.argv[1:]:
        comp = AirGapComplianceVerifier.full_audit()
        report["compliance"] = comp
        status = "COMPLIANT" if comp["compliant"] else "VIOLATIONS_DETECTED"
        print(f"Air-Gap Status: {status} (Env leaks: {len(comp['environment_leaks'])}, Suspicious conns: {comp['network_audit']['suspicious_count']})")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        print(f"Report saved to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
