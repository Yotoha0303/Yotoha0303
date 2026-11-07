#!/usr/bin/env python3
"""Inspect Android packages and prepare conservative, per-user privacy controls.

Generated DNS rules are candidate endpoints, not evidence of device traffic.
Android Private DNS accepts a provider hostname, not a local blocklist; import the
AdGuard/NextDNS rules into the provider account or use the Clash rules locally.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path


DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "var/android-privacy-guard"
PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}\Z")
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
SERIAL = re.compile(r"[A-Za-z0-9._:-]+\Z")
APP_OPS = ("READ_CLIPBOARD", "SYSTEM_ALERT_WINDOW", "POST_NOTIFICATION", "GET_USAGE_STATS")

# Only these exact packages are eligible for automated debloat. Other names are
# observations until their role is verified on the specific ROM.
DEBLOAT_PACKAGES = {
    "com.miui.daemon": ("Telemetry Daemon", "MQSAS diagnostic service is installed.",
                        ("data.mistat.xiaomi.com",)),
    "com.xiaomi.barrage": ("Telemetry Daemon", "Xiaomi barrage service is installed.", ()),
}
INVASIVE_APPS = {
    "com.taobao.idlefish": ("Xianyu is installed; review clipboard, overlay, notification and usage access.",
                            ("log.mmstat.com",)),
    "com.taobao.taobao": ("Taobao is installed; review clipboard, overlay, notification and usage access.",
                          ("log.mmstat.com",)),
}
REVIEW_PACKAGES = {
    "com.miui.securitycenter": ("Security Guard", "Security Center is installed; inspect its network and permission settings manually."),
    "com.miui.analytics": ("Telemetry Daemon", "MIUI analytics package is installed; inspect its role manually."),
    "com.miui.msa.global": ("Ad SDK", "MIUI ad service is installed; inspect its role manually."),
    "com.miui.misightservice": ("Telemetry Daemon", "MIUI diagnostics service is installed; inspect its role manually."),
    "com.xiaomi.security.onetrack": ("Telemetry Daemon", "Xiaomi OneTrack package is installed; inspect its role manually."),
}
PROTECTED_PACKAGES = {
    "android", "com.android.phone", "com.android.systemui", "com.android.server.telecom",
    "com.android.emergency", "com.android.cellbroadcastreceiver", "com.android.cellbroadcastservice",
    "com.android.providers.telephony", "com.android.settings", "com.android.shell",
    "com.miui.securitycenter", "com.miui.securitycore", "com.qualcomm.qti.services.systemhelper",
}
PROTECTED_PREFIXES = ("com.android.phone.", "com.android.systemui.",
                      "com.android.cellbroadcast", "com.google.android.cellbroadcast",
                      "com.android.emergency.", "com.android.carrier", "com.android.incallui",
                      "com.android.server.telecom.", "com.android.providers.telephony.")


class ADBUnavailable(RuntimeError):
    pass


def is_protected_package(package):
    return package in PROTECTED_PACKAGES or any(package.startswith(p) for p in PROTECTED_PREFIXES)


def is_debloat_target(package):
    return PACKAGE.fullmatch(package) is not None and package in DEBLOAT_PACKAGES and not is_protected_package(package)


def run_adb(args, serial=None, runner=subprocess.run):
    command = ["adb"] + (["-s", serial] if serial else []) + list(args)
    try:
        result = runner(command, capture_output=True, text=True, timeout=20, check=False)
    except FileNotFoundError as error:
        raise ADBUnavailable("adb executable not found") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("ADB command timed out") from error
    if result.returncode:
        detail = (result.stderr + " " + result.stdout).strip()
        raise RuntimeError("ADB command failed: " + " ".join(args) + ": " + detail)
    return result.stdout


def connected_devices(runner=subprocess.run):
    output = run_adb(["devices", "-l"], runner=runner)
    devices = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device" and SERIAL.fullmatch(fields[0]):
            devices.append(fields[0])
    return devices


def parse_packages(output):
    packages = set()
    for line in output.splitlines():
        if line.startswith("package:"):
            package = line.removeprefix("package:").strip()
            if PACKAGE.fullmatch(package):
                packages.add(package)
    return packages


def detect_packages(packages):
    findings = []
    for package in sorted(packages):
        if package in DEBLOAT_PACKAGES:
            kind, description, domains = DEBLOAT_PACKAGES[package]
            findings.append({"package_name": package, "component_type": kind, "severity": "High",
                             "risk_description": description, "mitigation_profile": "debloat",
                             "domains": list(domains)})
        elif package in INVASIVE_APPS:
            description, domains = INVASIVE_APPS[package]
            findings.append({"package_name": package, "component_type": "Ad SDK", "severity": "Medium",
                             "risk_description": description, "mitigation_profile": "appops",
                             "appops": list(APP_OPS), "domains": list(domains)})
        elif package in REVIEW_PACKAGES:
            kind, description = REVIEW_PACKAGES[package]
            findings.append({"package_name": package, "component_type": kind, "severity": "Medium",
                             "risk_description": description, "mitigation_profile": "observe", "domains": []})
        elif "systemhelp" in package.lower() or "systemhelper" in package.lower():
            findings.append({"package_name": package, "component_type": "OEM Framework", "severity": "Medium",
                             "risk_description": "System helper package is installed; inspect its role manually.",
                             "mitigation_profile": "observe", "domains": []})
    return findings


def make_actions(findings, debloat_mode="disable"):
    if debloat_mode not in ("disable", "uninstall"):
        raise ValueError("unsupported debloat mode")
    actions = []
    for finding in findings:
        package = finding["package_name"]
        if finding["mitigation_profile"] == "debloat":
            if not is_debloat_target(package):
                raise ValueError("protected or unapproved debloat package")
            command = (["shell", "pm", "disable-user", "--user", "0", package]
                       if debloat_mode == "disable" else
                       ["shell", "pm", "uninstall", "-k", "--user", "0", package])
            actions.append({"package": package, "kind": "debloat", "command": command})
        elif finding["mitigation_profile"] == "appops":
            if package not in INVASIVE_APPS or is_protected_package(package):
                raise ValueError("unapproved AppOps package")
            for op in finding["appops"]:
                if op not in APP_OPS:
                    raise ValueError("unapproved AppOps operation")
                actions.append({"package": package, "kind": "appops", "op": op,
                                "command": ["shell", "cmd", "appops", "set", package, op, "ignore"]})
    return actions


def normalize_domains(findings, extra_domains=()):
    domains = set(extra_domains)
    for finding in findings:
        domains.update(finding.get("domains", []))
    for domain in domains:
        if not isinstance(domain, str) or not DOMAIN.fullmatch(domain):
            raise ValueError("invalid blocklist domain")
    return sorted(domains)


def render_script(actions):
    lines = ["#!/bin/sh", "set -eu", ': "${ANDROID_SERIAL:?Set ANDROID_SERIAL to the target ADB serial}"']
    for action in actions:
        command = "adb -s \"$ANDROID_SERIAL\" " + shlex.join(action["command"])
        if action["kind"] == "appops":
            package, op = action["package"], action["op"]
            check = f'adb -s "$ANDROID_SERIAL" shell cmd appops get {shlex.quote(package)} {shlex.quote(op)}'
            lines.extend([f"if appop_result=$({check} 2>&1); then", f"  {command}",
                          "else", '  case "$appop_result" in',
                          f"    *'Unknown operation'*) printf '%s\\n' 'Unsupported AppOp: {op} for {package}' >&2 ;;",
                          '    *) printf \'%s\\n\' "$appop_result" >&2; exit 1 ;;',
                          "  esac", "fi"])
        else:
            lines.append(command)
    return "\n".join(lines) + "\n"


def blocklist_files(domains):
    return {
        "adguard.txt": "\n".join("||" + d + "^" for d in domains) + ("\n" if domains else ""),
        "nextdns.txt": "\n".join(domains) + ("\n" if domains else ""),
        "clash.rules": "\n".join("DOMAIN," + d + ",REJECT" for d in domains) + ("\n" if domains else ""),
    }


def execute_actions(actions, serial, runner=subprocess.run):
    results = []
    for action in actions:
        package = action["package"]
        if action["kind"] == "debloat":
            if not is_debloat_target(package) or action["command"] not in (
                ["shell", "pm", "disable-user", "--user", "0", package],
                ["shell", "pm", "uninstall", "-k", "--user", "0", package],
            ):
                raise ValueError("protected or unapproved debloat command")
        if action["kind"] == "appops":
            if (package not in INVASIVE_APPS or action.get("op") not in APP_OPS or
                    action["command"] != ["shell", "cmd", "appops", "set", package,
                                          action["op"], "ignore"]):
                raise ValueError("unapproved AppOps command")
            try:
                run_adb(["shell", "cmd", "appops", "get", package, action["op"]],
                        serial, runner)
            except RuntimeError as error:
                if "Unknown operation" not in str(error):
                    raise
                results.append({"package": action["package"], "operation": action["op"],
                                "status": "unsupported"})
                continue
        elif action["kind"] != "debloat":
            raise ValueError("unapproved action kind")
        output = run_adb(action["command"], serial, runner)
        if action["kind"] == "debloat" and output.strip().startswith(("Failure", "Error")):
            raise RuntimeError("package manager rejected " + package)
        results.append({"package": action["package"], "operation": action.get("op", action["kind"]),
                        "status": "applied"})
    return results


def ingest_feed(findings, path):
    import privacy_threat_sanitizer as sanitizer

    incoming = sanitizer.parse_input({"reports": findings})
    existing = sanitizer.read_json(path) if path.exists() else {}
    feed = sanitizer.build_feed(existing, incoming)
    violations = sanitizer.audit_pii(feed)
    if violations:
        raise ValueError("feed PII audit failed: " + ", ".join(violations))
    return sanitizer.atomic_write(path, json.dumps(feed, ensure_ascii=False, indent=2) + "\n")


def main(argv=None, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="inspect installed packages")
    parser.add_argument("--generate-script", action="store_true", help="write lockdown script and DNS rules")
    parser.add_argument("--apply-lockdown", action="store_true", help="apply selected controls to the device")
    parser.add_argument("--dry-run", action="store_true", help="print report without writes or lockdown")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="artifact directory")
    parser.add_argument("--serial", help="ADB serial when multiple devices are connected")
    parser.add_argument("--mock-packages", type=Path, help="offline package-list fixture; never calls ADB")
    parser.add_argument("--debloat-mode", choices=("disable", "uninstall"), default="disable")
    parser.add_argument("--domain", action="append", default=[], help="additional exact domain to block")
    parser.add_argument("--ingest-feed", nargs="?", const=Path(__file__).resolve().parents[1] /
                        "data/instabox-personal-privacy-feed.json", type=Path,
                        help="merge detected threats into the privacy feed")
    args = parser.parse_args(argv)
    if args.apply_lockdown and args.mock_packages and not args.dry_run:
        parser.error("--apply-lockdown cannot use --mock-packages")
    if args.serial and not SERIAL.fullmatch(args.serial):
        parser.error("invalid ADB serial")
    try:
        if args.mock_packages:
            packages = parse_packages(args.mock_packages.read_text(encoding="utf-8"))
            source, serial = "mock", None
        else:
            try:
                devices = connected_devices(runner)
            except ADBUnavailable:
                if (args.apply_lockdown and not args.dry_run) or args.serial:
                    raise
                devices = []
            if args.serial and args.serial not in devices:
                raise RuntimeError("selected device is not connected and authorized")
            if len(devices) > 1 and not args.serial:
                raise RuntimeError("multiple devices connected; specify --serial")
            serial = args.serial or (devices[0] if devices else None)
            if args.apply_lockdown and not args.dry_run and serial is None:
                raise RuntimeError("no authorized ADB device connected")
            packages = parse_packages(run_adb(["shell", "pm", "list", "packages", "--user", "0"],
                                              serial, runner)) if serial else set()
            source = "device" if serial else "none"
        findings = detect_packages(packages)
        actions = make_actions(findings, args.debloat_mode)
        domains = normalize_domains(findings, args.domain)
        report = {"source": source, "package_count": len(packages), "findings": findings,
                  "actions": actions, "domains": domains,
                  "dns_note": "Import provider rules into AdGuard/NextDNS; Android Private DNS uses a provider hostname."}
        if args.dry_run:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.apply_lockdown:
            report["results"] = execute_actions(actions, serial, runner)
        if args.scan or args.generate_script or args.apply_lockdown or args.ingest_feed is not None:
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                     encoding="utf-8")
        if args.generate_script:
            script = args.output / "lockdown.sh"
            script.write_text(render_script(actions), encoding="utf-8")
            script.chmod(0o700)
            for name, contents in blocklist_files(domains).items():
                (args.output / name).write_text(contents, encoding="utf-8")
        if args.ingest_feed is not None:
            ingest_feed(findings, args.ingest_feed)
        print(f"Detected {len(findings)} matching packages; planned {len(actions)} actions ({source}).")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Android privacy guard failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
