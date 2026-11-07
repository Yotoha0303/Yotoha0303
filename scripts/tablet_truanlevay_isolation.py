#!/usr/bin/env python3
"""Audit a Xiaomi Pad 6 and plan reversible per-user account isolation controls.

No Google credentials or account identifiers are queried or stored. A browser
session alone cannot provide Android user isolation; see the accompanying SOP.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


PREFERRED_SERIAL = "be6ce4f9"
EXPECTED_DEVICE = "pipa"
EXPECTED_MODEL = "23043RP34C"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "var/tablet-truanlevay-isolation"
PACKAGE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
SERIAL_RE = re.compile(r"[A-Za-z0-9._:-]+\Z")
OEM_DISABLE = ("com.miui.misightservice", "com.xiaomi.security.onetrack")
OEM_REVIEW = "com.qualcomm.qti.services.systemhelper"
INVASIVE_APPS = ("com.taobao.idlefish", "com.taobao.taobao")
EDGE = "com.microsoft.emmx"
APP_OPS = ("READ_CLIPBOARD", "SYSTEM_ALERT_WINDOW")
PROTECTED = frozenset({
    "android", "com.android.systemui", "com.android.settings", "com.android.phone",
    "com.android.server.telecom", "com.android.providers.telephony",
    "com.android.emergency", "com.android.cellbroadcastreceiver",
    "com.android.cellbroadcastservice", "com.miui.securitycenter",
    "com.miui.securitycore", OEM_REVIEW,
})
PROTECTED_PREFIXES = ("com.android.", "com.google.android.cellbroadcast", "com.qti.phone")


class ADBError(RuntimeError):
    """ADB is missing, disconnected, or returned an error."""


def run_adb(args, serial=None, runner=subprocess.run):
    command = ["adb"] + (["-s", serial] if serial else []) + list(args)
    try:
        result = runner(command, capture_output=True, text=True, timeout=20, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise ADBError(str(error)) from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ADBError(f"{' '.join(args)}: {detail or 'ADB failed'}")
    return result.stdout


def attached_devices(runner=subprocess.run):
    devices = []
    for line in run_adb(["devices", "-l"], runner=runner).splitlines():
        fields = line.split()
        if len(fields) > 1 and fields[1] == "device" and SERIAL_RE.fullmatch(fields[0]):
            devices.append(fields[0])
    return devices


def select_device(devices):
    if PREFERRED_SERIAL in devices:
        return PREFERRED_SERIAL
    if len(devices) == 1:
        return devices[0]
    raise ADBError("target unavailable; attach one authorized device or use --mock-packages")


def parse_packages(output):
    return {name for line in output.splitlines()
            if line.startswith("package:")
            for name in [line.removeprefix("package:").strip()]
            if PACKAGE_RE.fullmatch(name)}


def is_protected(package):
    return package in PROTECTED or any(package.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def categorize(package):
    if is_protected(package):
        return "protected"
    if package in OEM_DISABLE:
        return "oem_disable_candidate"
    if package in INVASIVE_APPS:
        return "appops_candidate"
    if package == EDGE:
        return "browser"
    return "other"


def parse_appop_mode(output, op):
    match = re.search(r"^\s*" + re.escape(op) + r":\s*(allow|ignore|deny|default)\b",
                      output, re.MULTILINE | re.IGNORECASE)
    if match:
        return match.group(1).lower()
    if output.strip() == "No operations.":
        return "default"
    raise ValueError(f"cannot determine current {op} mode")


def inspect_system_helper(serial, runner=subprocess.run):
    output = run_adb(["shell", "dumpsys", "package", OEM_REVIEW], serial, runner)
    code_path = re.search(r"^\s*codePath=(\S+)", output, re.MULTILINE)
    version = re.search(r"^\s*versionName=(\S+)", output, re.MULTILINE)
    permission_block = re.search(r"^\s*requested permissions:\s*\n(.*?)(?=^\s*install permissions:)",
                                 output, re.MULTILINE | re.DOTALL)
    permissions = sorted(set(re.findall(r"^\s+([A-Za-z][A-Za-z0-9_.]+)\s*$",
                                        permission_block.group(1), re.MULTILINE))) if permission_block else []
    return {
        "code_path": code_path.group(1) if code_path else None,
        "version": version.group(1) if version else None,
        "requested_permissions": permissions,
        "privileged_path": bool(code_path and "/priv-app/" in code_path.group(1)),
        "assessment": "Package metadata is not evidence of account or network telemetry.",
    }


def audit_device(serial, runner=subprocess.run, user_id=0):
    if not isinstance(user_id, int) or user_id < 0:
        raise ValueError("user ID must be a non-negative integer")
    user = str(user_id)
    device = run_adb(["shell", "getprop", "ro.product.device"], serial, runner).strip()
    model = run_adb(["shell", "getprop", "ro.product.model"], serial, runner).strip()
    packages = parse_packages(run_adb(["shell", "pm", "list", "packages", "--user", user],
                                      serial, runner))
    disabled = parse_packages(run_adb(["shell", "pm", "list", "packages", "-d", "--user", user],
                                      serial, runner))
    dns_mode = run_adb(["shell", "settings", "get", "global", "private_dns_mode"],
                       serial, runner).strip()
    dns_host = run_adb(["shell", "settings", "get", "global", "private_dns_specifier"],
                       serial, runner).strip()
    system_helper = inspect_system_helper(serial, runner) if OEM_REVIEW in packages else None
    return {
        "source": "adb", "serial": serial, "device": device, "model": model,
        "user_id": user_id,
        "identity_matches": device == EXPECTED_DEVICE and model == EXPECTED_MODEL,
        "packages": packages, "disabled": disabled,
        "private_dns": {"mode": dns_mode, "hostname": dns_host},
        "system_helper": system_helper,
    }


def make_plan(audit, runner=subprocess.run, debloat_mode="disable"):
    packages = audit["packages"]
    disabled = audit["disabled"]
    serial = audit.get("serial")
    user = str(audit["user_id"])
    actions = []
    notes = []
    for package in OEM_DISABLE:
        if package in packages and package not in disabled:
            if debloat_mode == "uninstall":
                actions.append({
                    "kind": "uninstall", "package": package,
                    "command": ["shell", "pm", "uninstall", "-k", "--user", user, package],
                    "rollback": ["shell", "cmd", "package", "install-existing", "--user", user, package],
                })
            else:
                actions.append({
                    "kind": "disable", "package": package,
                    "command": ["shell", "pm", "disable-user", "--user", user, package],
                    "rollback": ["shell", "pm", "enable", "--user", user, package],
                })
    for package in INVASIVE_APPS:
        if package not in packages or package in disabled:
            continue
        for op in APP_OPS:
            if audit["source"] == "mock":
                previous = "default"
            else:
                try:
                    output = run_adb(["shell", "cmd", "appops", "get", "--user", user,
                                      package, op], serial, runner)
                    previous = parse_appop_mode(output, op)
                except ADBError as error:
                    if "Unknown operation" in str(error):
                        notes.append(f"{package}: {op} unsupported; skipped")
                        continue
                    raise
            if previous != "ignore":
                actions.append({
                    "kind": "appops", "package": package, "op": op,
                    "command": ["shell", "cmd", "appops", "set", "--user", user,
                                package, op, "ignore"],
                    "rollback": ["shell", "cmd", "appops", "set", "--user", user,
                                 package, op, previous],
                })
    if OEM_REVIEW in packages:
        notes.append("Qualcomm System Helper is a privileged service; inspect permissions "
                     "and observed traffic before any manual decision. It is not auto-disabled.")
    if EDGE not in packages:
        notes.append(f"Edge is absent from user {audit['user_id']}; install it in the "
                     "dedicated Android user before signing in through the browser.")
    if not audit["identity_matches"] and audit["source"] == "adb":
        notes.append("Device identity differs from the expected Xiaomi Pad 6; apply is blocked.")
    return {"actions": actions, "notes": notes}


def validate_action(action, user_id=0):
    if not isinstance(user_id, int) or user_id < 0:
        raise ValueError("invalid user ID")
    user = str(user_id)
    package = action.get("package")
    if is_protected(package or ""):
        raise ValueError("protected package cannot be changed")
    if action.get("kind") == "disable" and package in OEM_DISABLE:
        expected = ["shell", "pm", "disable-user", "--user", user, package]
    elif action.get("kind") == "uninstall" and package in OEM_DISABLE:
        expected = ["shell", "pm", "uninstall", "-k", "--user", user, package]
    elif action.get("kind") == "appops" and package in INVASIVE_APPS and action.get("op") in APP_OPS:
        expected = ["shell", "cmd", "appops", "set", "--user", user,
                    package, action["op"], "ignore"]
    else:
        raise ValueError("action is outside the allowlist")
    if action.get("command") != expected:
        raise ValueError("action command does not match the allowlist")


def execute_plan(audit, plan, runner=subprocess.run):
    if audit["source"] != "adb" or not audit["identity_matches"]:
        raise ValueError("apply requires the verified Xiaomi Pad 6")
    for action in plan["actions"]:
        validate_action(action, audit["user_id"])
    serial = audit["serial"]
    # Recheck identity and package state immediately before mutation.
    current = audit_device(serial, runner, audit["user_id"])
    if not current["identity_matches"] or current["packages"] != audit["packages"] or \
            current["disabled"] != audit["disabled"]:
        raise ADBError("device identity or package state changed; rerun the audit")
    for action in plan["actions"]:
        if action["kind"] == "appops":
            output = run_adb(["shell", "cmd", "appops", "get", "--user",
                              str(audit["user_id"]),
                              action["package"], action["op"]], serial, runner)
            if parse_appop_mode(output, action["op"]) != action["rollback"][-1]:
                raise ADBError("AppOps state changed; rerun the plan")
    results = []
    for action in plan["actions"]:
        output = run_adb(action["command"], serial, runner)
        if output.strip().startswith(("Failure", "Error")) or "SecurityException" in output:
            raise ADBError(f"device rejected {action['package']}: {output.strip()}")
        results.append({"package": action["package"], "kind": action["kind"],
                        "op": action.get("op"), "status": "applied"})
    return results


def render_report(audit, plan):
    relevant = (*OEM_DISABLE, OEM_REVIEW, *INVASIVE_APPS, EDGE,
                "android", "com.android.systemui", "com.android.settings")
    return {
        "source": audit["source"], "serial": audit.get("serial"),
        "user_id": audit["user_id"],
        "device": audit.get("device"), "model": audit.get("model"),
        "identity_matches": audit["identity_matches"],
        "private_dns": audit["private_dns"],
        "system_helper": audit.get("system_helper"),
        "packages": [{"name": package, "installed": package in audit["packages"],
                      "disabled_for_user": package in audit["disabled"],
                      "category": categorize(package)} for package in relevant],
        "plan": plan,
    }


def write_report(path, payload):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as report_file:
        os.fchmod(report_file.fileno(), 0o600)
        report_file.write(payload)


def main(argv=None, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", action="store_true", help="read-only inventory (default)")
    parser.add_argument("--prepare-env", action="store_true", help="export a reversible action plan")
    parser.add_argument("--apply", action="store_true", help="apply allowlisted plan to verified tablet")
    parser.add_argument("--dry-run", action="store_true", help="print report without writing or applying")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="report directory")
    parser.add_argument("--user-id", type=int, default=0, help="Android user ID (default: 0)")
    parser.add_argument("--debloat-mode", choices=("disable", "uninstall"), default="disable",
                        help="per-user debloat strategy (default: disable; use uninstall on HyperOS)")
    parser.add_argument("--mock-packages", type=Path, help="offline pm-list fixture for audit/planning")
    args = parser.parse_args(argv)
    if args.apply and not args.prepare_env:
        parser.error("--apply requires --prepare-env")
    if args.apply and args.mock_packages and not args.dry_run:
        parser.error("mock data cannot be applied")
    if args.user_id < 0:
        parser.error("--user-id must be non-negative")
    try:
        if args.mock_packages:
            packages = parse_packages(args.mock_packages.read_text(encoding="utf-8"))
            audit = {"source": "mock", "serial": None, "device": None, "model": None,
                     "identity_matches": False, "user_id": args.user_id,
                     "packages": packages, "disabled": set(),
                     "private_dns": {"mode": "unknown", "hostname": "unknown"}}
        else:
            serial = select_device(attached_devices(runner))
            audit = audit_device(serial, runner, args.user_id)
        plan = make_plan(audit, runner, debloat_mode=args.debloat_mode) if args.prepare_env else {"actions": [], "notes": []}
        report = render_report(audit, plan)
        payload = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
        stem = "plan" if args.prepare_env else "audit"
        report_path = args.output / (f"{stem}.json" if args.user_id == 0
                                     else f"{stem}-user-{args.user_id}.json")
        if not args.dry_run:
            args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_report(report_path, payload)
        if args.apply and not args.dry_run:
            report["results"] = execute_plan(audit, plan, runner)
            payload = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
            write_report(report_path, payload)
        print(payload, end="")
        return 0
    except (ADBError, OSError, ValueError) as error:
        print(f"isolation: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
