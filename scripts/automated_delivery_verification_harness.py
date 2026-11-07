#!/usr/bin/env python3
"""Package, sandbox, and certify Python unittest deliveries (standard library only).

Requires Linux, Python 3.10+, and Bubblewrap (bwrap). A .pyz delivery includes its
source files, manifest, harness, and escrow adapter. Clients can rerun it with
``python3 delivery.pyz PROOF_DIRECTORY``; no repository checkout is needed.
Deliveries must include their dependencies and agreed unittest tests. Nothing
is installed from the network. Skips and expected failures prevent acceptance.

Tests run in new user/PID/network namespaces with a private writable copy, a
minimal read-only system runtime, no host home/environment, resource limits,
bounded logs, and a wall deadline. Sandbox setup failures never fall back to
executing on the host. This backend requires a system /usr/bin/python3.

SHA-256 checksums bind the exact archive, manifest, structured test results, and
raw execution logs. Optional HMAC-SHA256 authenticates the certificate to holders
of a shared key (at least 32 bytes). Keep that key outside the delivery. Hashes
alone are not identity signatures or independent execution attestation: tests
must be trusted/agreed, and certificates need a separately saved digest or key.

Escrow workflow: record_delivery on the original source directory, issue a client
acceptance token, package with matching contract/milestone IDs, run, then call
trigger_milestone_acceptance with that authenticated client's token. Acceptance
requires an authenticated, passing proof, identical latest delivery, and verified
escrow. The audit ledger validates token scope/expiry/replay and stores the proof
digest in the acceptance statement. No token is minted and no funds are moved.

CLI examples (JSON output; exit 0=success, 1=failed verification/tests, 2=error)::

    python3 scripts/automated_delivery_verification_harness.py package src delivery.pyz \
        --contract-id c1 --milestone-id m1
    python3 scripts/automated_delivery_verification_harness.py run delivery.pyz proof \
        --key-file client.key
    python3 scripts/automated_delivery_verification_harness.py verify proof --key-file client.key
    python3 scripts/automated_delivery_verification_harness.py accept proof --db ledger.sqlite3 \
        --contract-id c1 --milestone-id m1 --client-id client --token-file acceptance.token \
        --key-file client.key
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import zipfile

if __package__:
    from .freelance_contract_escrow_audit import (
        FreelanceContractEscrowAudit, StateError, build_delivery_manifest,
    )
else:
    from freelance_contract_escrow_audit import (
        FreelanceContractEscrowAudit, StateError, build_delivery_manifest,
    )


SCHEMA_VERSION = 1
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILES = 4096
MAX_REPORT_BYTES = 1024 * 1024
PROOF_FILES = {"delivery.pyz", "certificate.json", "stdout.log", "stderr.log", "test-results.json"}
OUTCOMES = {"passed", "failures", "errors", "skipped", "expected_failures", "unexpected_successes"}
ENTRY_POINT = b'''import sys
from automated_delivery_verification_harness import main
raise SystemExit(main(["run", sys.argv[0], *sys.argv[1:]]))
'''

# This observer runs outside the delivered module tree. Counts come from unittest
# callbacks, never a regex over test-controlled stdout/stderr. Executable tests
# are still trusted code; this is not a remote attestation protocol.
WORKER = r'''
import json, math, os, resource, sys, traceback, unittest
from pathlib import Path

timeout, memory = float(sys.argv[2]), int(sys.argv[3]) * 1024 * 1024
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
resource.setrlimit(resource.RLIMIT_CPU, (max(1, math.ceil(timeout)), max(2, math.ceil(timeout) + 1)))
resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
os.chdir('/work/project')
sys.path.insert(0, '/work/project')

class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.current = None
    def startTest(self, test):
        super().startTest(test)
        self.current = {'id': test.id(), 'status': 'errors'}
        self.records.append(self.current)
    def stopTest(self, test):
        super().stopTest(test)
        self.current = None
    def mark(self, test, status):
        if self.current is not None and self.current['id'] == test.id():
            if self.current['status'] not in ('failures', 'errors') or status != 'passed':
                self.current['status'] = status
        else:
            self.records.append({'id': test.id(), 'status': status})
    def addSuccess(self, test):
        # A success callback follows startTest; subtest errors never call it.
        self.current['status'] = 'passed'
        super().addSuccess(test)
    def addFailure(self, test, err):
        self.mark(test, 'failures'); super().addFailure(test, err)
    def addError(self, test, err):
        self.mark(test, 'errors'); super().addError(test, err)
    def addSkip(self, test, reason):
        self.mark(test, 'skipped'); super().addSkip(test, reason)
    def addExpectedFailure(self, test, err):
        self.mark(test, 'expected_failures'); super().addExpectedFailure(test, err)
    def addUnexpectedSuccess(self, test):
        self.mark(test, 'unexpected_successes'); super().addUnexpectedSuccess(test)
    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.mark(test, 'failures' if issubclass(err[0], test.failureException) else 'errors')
        super().addSubTest(test, subtest, err)

try:
    suite = unittest.defaultTestLoader.discover('.', pattern=sys.argv[1])
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    counts = {name: 0 for name in ('passed', 'failures', 'errors', 'skipped',
                                  'expected_failures', 'unexpected_successes')}
    for row in result.records:
        counts[row['status']] += 1
    counts['run'] = len(result.records)
    report = {'schema_version': 1, 'python': sys.version.split()[0],
              'tests': result.records, 'counts': counts}
    with Path('/work/test-results.json').open('x', encoding='utf-8') as handle:
        json.dump(report, handle, sort_keys=True, allow_nan=False)
    raise SystemExit(0 if counts['run'] > 0 and counts['passed'] == counts['run'] else 1)
except Exception:
    traceback.print_exc()
    raise SystemExit(2)
'''


class SandboxUnavailableError(RuntimeError):
    """The required isolation backend cannot be used; host execution is forbidden."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: bytes) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def reject(value):
        raise ValueError(f"invalid JSON constant: {value}")
    return json.loads(data, object_pairs_hook=unique, parse_constant=reject)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 for c in value)


def _path(value: Any) -> str:
    _require(_text(value) and "\\" not in value, "invalid relative file name")
    path = PurePosixPath(value)
    _require(not path.is_absolute() and all(p not in ("", ".", "..") for p in value.split("/")),
             "unsafe archive path")
    return value


def _key(key: bytes | None) -> bytes | None:
    _require(key is None or (isinstance(key, bytes) and len(key) >= 32),
             "signing key must contain at least 32 bytes")
    return key


def _pattern(pattern: str) -> str:
    _require(_text(pattern) and not any(c in pattern for c in ("/", "\\", "\x00")),
             "test pattern must be a file-name glob")
    return pattern


def _evidence(path: str, content: bytes) -> dict:
    return {"path": path, "size_bytes": len(content), "sha256": _sha(content)}


def _read_regular(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= maximum, "invalid or oversized evidence file")
        content = stream.read(maximum + 1)
    _require(len(content) <= maximum, "oversized evidence file")
    return content


def _load_bundle(data: bytes) -> tuple[dict, dict[str, bytes]]:
    _require(len(data) <= MAX_ARCHIVE_BYTES, "archive exceeds size limit")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        _require(len(names) <= MAX_FILES + 4 and len(names) == len(set(names)),
                 "too many or duplicate archive entries")
        _require(sum(info.file_size for info in infos) <= MAX_ARCHIVE_BYTES,
                 "uncompressed archive exceeds size limit")
        for info in infos:
            _path(info.filename)
            kind = stat.S_IFMT(info.external_attr >> 16)
            _require(kind in (0, stat.S_IFREG) and not info.flag_bits & 1,
                     "archive must contain unencrypted regular files")
        members = {name: archive.read(name) for name in names}
    manifest = _json(members["MANIFEST.json"])
    _require(set(manifest) == {"schema_version", "type", "context", "test_pattern", "files", "runtime"}
             and type(manifest["schema_version"]) is int and manifest["schema_version"] == SCHEMA_VERSION
             and manifest["type"] == "delivery_test_runner", "invalid runner manifest")
    _pattern(manifest["test_pattern"])
    context = manifest["context"]
    _require(isinstance(context, dict) and set(context) == {"contract_id", "milestone_id"}
             and (all(v is None for v in context.values()) or all(_text(v) for v in context.values())),
             "contract and milestone IDs must be supplied together")
    _require(isinstance(manifest["files"], list) and 0 < len(manifest["files"]) <= MAX_FILES
             and isinstance(manifest["runtime"], list), "invalid delivery manifest")
    expected = {"MANIFEST.json"}
    for rows, prefix in ((manifest["files"], "delivery/"), (manifest["runtime"], "")):
        paths = []
        for row in rows:
            _require(isinstance(row, dict) and set(row) == {"path", "size_bytes", "sha256"},
                     "invalid file evidence")
            name = prefix + _path(row["path"])
            _require(type(row["size_bytes"]) is int and row["size_bytes"] >= 0
                     and row == _evidence(row["path"], members[name]), "file checksum mismatch")
            paths.append(row["path"])
            expected.add(name)
        _require(paths == sorted(set(paths)), "manifest paths must be unique and sorted")
    _require({row["path"] for row in manifest["runtime"]} == {
        "__main__.py", "automated_delivery_verification_harness.py", "freelance_contract_escrow_audit.py"
    } and set(members) == expected, "unexpected or missing archive entries")
    # Forbid a file also serving as a directory before any extraction occurs.
    for name in expected:
        _require(not any(str(parent) in expected for parent in PurePosixPath(name).parents
                         if str(parent) != "."), "conflicting archive paths")
    return manifest, members


def package_delivery(directory: str | Path, bundle_path: str | Path, *,
                     test_pattern: str = "test*.py", contract_id: str | None = None,
                     milestone_id: str | None = None) -> dict:
    """Build a deterministic, self-contained .pyz; never overwrite an existing file."""
    source, target = Path(directory), Path(bundle_path)
    _pattern(test_pattern)
    _require((contract_id is None and milestone_id is None) or
             (_text(contract_id) and _text(milestone_id)), "supply both contract and milestone IDs")
    _require(not target.resolve().is_relative_to(source.resolve()), "bundle must be outside delivery")
    files = build_delivery_manifest(source)
    _require(0 < len(files) <= MAX_FILES and sum(row["size_bytes"] for row in files) < MAX_ARCHIVE_BYTES,
             "delivery must contain files within size limits")
    members = {}
    for row in files:
        _path(row["path"])
        content = _read_regular(source / row["path"], MAX_ARCHIVE_BYTES)
        _require(row == _evidence(row["path"], content), "delivery changed during packaging")
        members["delivery/" + row["path"]] = content
    runtime = {"__main__.py": ENTRY_POINT,
               "automated_delivery_verification_harness.py": Path(__file__).read_bytes(),
               "freelance_contract_escrow_audit.py": Path(__file__).with_name(
                   "freelance_contract_escrow_audit.py").read_bytes()}
    manifest = {"schema_version": SCHEMA_VERSION, "type": "delivery_test_runner",
                "context": {"contract_id": contract_id, "milestone_id": milestone_id},
                "test_pattern": test_pattern, "files": files,
                "runtime": [_evidence(name, data) for name, data in sorted(runtime.items())]}
    members.update(runtime)
    members["MANIFEST.json"] = _canonical(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    data = buffer.getvalue()
    _load_bundle(data)
    # Stage and publish without clobbering existing archives, even on a race.
    with tempfile.NamedTemporaryFile(dir=target.parent) as staged:
        staged.write(data)
        staged.flush()
        os.link(staged.name, target)
    return {"bundle_sha256": _sha(data), "manifest_sha256": _sha(_canonical(manifest)),
            "file_count": len(files)}


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float = 60.0
    memory_limit_mb: int = 256
    output_limit_bytes: int = 1024 * 1024

    def __post_init__(self):
        _require(type(self.timeout_seconds) in (int, float)
                 and math.isfinite(self.timeout_seconds) and 0 < self.timeout_seconds <= 3600,
                 "timeout must be finite and between 0 and 3600 seconds")
        _require(type(self.memory_limit_mb) is int and 32 <= self.memory_limit_mb <= 4096,
                 "memory limit must be an integer from 32 to 4096 MiB")
        _require(type(self.output_limit_bytes) is int and 1024 <= self.output_limit_bytes <= 16 * 1024 * 1024,
                 "output limit must be an integer from 1024 to 16777216 bytes")


def _sandbox_command(executable: str, work: Path, worker: Path, pattern: str, limits: SandboxLimits) -> list[str]:
    backend = shutil.which(executable)
    if sys.platform != "linux" or backend is None or not Path("/usr/bin/python3").is_file():
        raise SandboxUnavailableError("Linux, Bubblewrap, and /usr/bin/python3 are required")
    command = [backend, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
               "--ro-bind", "/usr", "/usr"]
    for name in ("/lib", "/lib64", "/bin"):
        if Path(name).exists():
            command += ["--ro-bind", name, name]
    command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--bind", str(work), "/work", "--ro-bind", str(worker), "/runner.py",
                "--chdir", "/work/project", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
                "--setenv", "HOME", "/work/home", "--setenv", "TMPDIR", "/tmp",
                "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "/usr/bin/python3", "-I", "-S", "-B", "/runner.py", pattern,
                str(limits.timeout_seconds), str(limits.memory_limit_mb)]
    return command


def _execute(command: list[str], limits: SandboxLimits) -> tuple[int, str | None, bytes, bytes]:
    """Drain both pipes concurrently with a total byte cap and process-tree cleanup."""
    output = {"stdout": bytearray(), "stderr": bytearray()}
    reason = None
    deadline = time.monotonic() + limits.timeout_seconds
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env={}, start_new_session=True) as process:
        try:
            with selectors.DefaultSelector() as selector:
                for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map() or process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reason = "timeout"
                        break
                    for key, _ in selector.select(min(remaining, 0.05)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        available = limits.output_limit_bytes - sum(map(len, output.values()))
                        output[key.data].extend(chunk[:available])
                        if len(chunk) > available:
                            reason = "output_limit_exceeded"
                            break
                    if reason:
                        break
        finally:
            # Also terminate descendants on exceptions; bwrap's PID namespace and
            # --die-with-parent cover children that create new process groups.
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
    return process.returncode, reason, bytes(output["stdout"]), bytes(output["stderr"])


def _report(data: bytes) -> dict:
    report = _json(data)
    _require(set(report) == {"schema_version", "python", "tests", "counts"}
             and type(report["schema_version"]) is int and report["schema_version"] == SCHEMA_VERSION
             and _text(report["python"]) and isinstance(report["tests"], list), "invalid test report")
    counts = {name: 0 for name in sorted(OUTCOMES)}
    for row in report["tests"]:
        _require(isinstance(row, dict) and set(row) == {"id", "status"} and _text(row["id"])
                 and row["status"] in OUTCOMES, "invalid test outcome")
        counts[row["status"]] += 1
    counts["run"] = len(report["tests"])
    _require(isinstance(report["counts"], dict)
             and all(type(v) is int for v in report["counts"].values())
             and report["counts"] == counts, "inconsistent test counts")
    return report


def _summary(report_data: bytes, returncode: int, reason: str | None) -> dict:
    try:
        report = _report(report_data)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        report = None
    counts = report["counts"] if report else {name: 0 for name in sorted(OUTCOMES | {"run"})}
    passed = report is not None and counts["run"] > 0 and counts["passed"] == counts["run"]
    status = reason or ("passed" if returncode == 0 and passed else "failed")
    return {"status": status, "report_valid": report is not None, "tests": counts}


def _seal(body: dict, signing_key: bytes | None) -> dict:
    data = _canonical(body)
    return {**body, "certificate_sha256": _sha(data), "signature": None if signing_key is None else {
        "algorithm": "HMAC-SHA256", "value": hmac.new(signing_key, data, hashlib.sha256).hexdigest()}}


def _verified_proof(directory: str | Path, signing_key: bytes | None,
                    expected_certificate_sha256: str | None) -> tuple[dict, dict]:
    _key(signing_key)
    root = Path(directory)
    _require(not root.is_symlink() and root.is_dir() and {p.name for p in root.iterdir()} == PROOF_FILES,
             "proof bundle must contain exactly its five evidence files")
    certificate = _json(_read_regular(root / "certificate.json", MAX_REPORT_BYTES))
    _require(isinstance(certificate, dict) and set(certificate) == {
        "schema_version", "type", "bundle_sha256", "manifest_sha256", "context", "test_pattern",
        "execution", "evidence", "certificate_sha256", "signature"}, "invalid certificate fields")
    body = {k: v for k, v in certificate.items() if k not in ("certificate_sha256", "signature")}
    data = _canonical(body)
    digest = _sha(data)
    _require(type(body["schema_version"]) is int and body["schema_version"] == SCHEMA_VERSION
             and body["type"] == "delivery_verification_certificate"
             and certificate["certificate_sha256"] == digest, "certificate checksum mismatch")
    if expected_certificate_sha256 is not None:
        _require(isinstance(expected_certificate_sha256, str)
                 and re.fullmatch(r"[0-9a-f]{64}", expected_certificate_sha256) is not None
                 and hmac.compare_digest(digest, expected_certificate_sha256), "saved certificate digest mismatch")
    signature = certificate["signature"]
    if signing_key is not None or signature is not None:
        _require(signing_key is not None and isinstance(signature, dict)
                 and set(signature) == {"algorithm", "value"} and signature["algorithm"] == "HMAC-SHA256"
                 and isinstance(signature["value"], str)
                 and hmac.compare_digest(signature["value"], hmac.new(signing_key, data, hashlib.sha256).hexdigest()),
                 "certificate HMAC missing or invalid (signed proofs require a key)")
    bundle_data = _read_regular(root / "delivery.pyz", MAX_ARCHIVE_BYTES)
    manifest, _ = _load_bundle(bundle_data)
    _require(body["bundle_sha256"] == _sha(bundle_data)
             and body["manifest_sha256"] == _sha(_canonical(manifest))
             and body["context"] == manifest["context"] and body["test_pattern"] == manifest["test_pattern"],
             "certificate does not match delivery")
    execution = body["execution"]
    _require(isinstance(execution, dict) and set(execution) == {
        "sandbox", "started_at", "finished_at", "duration_seconds", "limits", "returncode", "reason",
        "status", "report_valid", "tests"} and execution["sandbox"] == "bubblewrap"
             and type(execution["returncode"]) is int and execution["reason"] in
             (None, "timeout", "output_limit_exceeded"), "invalid execution evidence")
    SandboxLimits(**execution["limits"])
    _require(all(type(execution[name]) in (int, float) and math.isfinite(execution[name])
                 and execution[name] >= 0 for name in ("started_at", "finished_at", "duration_seconds"))
             and execution["finished_at"] >= execution["started_at"], "invalid execution timing")
    _require(set(body["evidence"]) == {"stdout", "stderr", "report"}, "invalid evidence references")
    contents = {}
    for name, filename in (("stdout", "stdout.log"), ("stderr", "stderr.log"), ("report", "test-results.json")):
        content = _read_regular(root / filename, MAX_REPORT_BYTES if name == "report" else 16 * 1024 * 1024)
        _require(body["evidence"][name] == _evidence(filename, content), "execution evidence checksum mismatch")
        contents[name] = content
    _require(len(contents["stdout"]) + len(contents["stderr"]) <= execution["limits"]["output_limit_bytes"],
             "logs exceed declared limit")
    expected = _summary(contents["report"], execution["returncode"], execution["reason"])
    _require(type(execution["report_valid"]) is bool and isinstance(execution["tests"], dict)
             and all(type(value) is int for value in execution["tests"].values())
             and all(execution[k] == v for k, v in expected.items()),
             "certificate test summary mismatch")
    return certificate, manifest


def verify_proof_bundle(directory: str | Path, *, signing_key: bytes | None = None,
                        expected_certificate_sha256: str | None = None) -> bool:
    """Verify all evidence, including failed executions; inspect status for success."""
    try:
        _verified_proof(directory, signing_key, expected_certificate_sha256)
        return True
    except (ValueError, TypeError, KeyError, OSError, AttributeError, RecursionError,
            zipfile.BadZipFile, NotImplementedError, OverflowError):
        return False


class AutomatedDeliveryVerificationHarness:
    """Client-facing runner; HMAC keys are used only in the supervising process."""

    def __init__(self, *, limits: SandboxLimits | None = None, signing_key: bytes | None = None,
                 sandbox_executable: str = "bwrap"):
        self.limits = limits if limits is not None else SandboxLimits()
        _require(isinstance(self.limits, SandboxLimits), "limits must be SandboxLimits")
        self.signing_key = _key(signing_key)
        _require(_text(sandbox_executable), "sandbox executable must be nonempty")
        self.sandbox_executable = sandbox_executable

    package_delivery = staticmethod(package_delivery)

    def run_bundle(self, bundle_path: str | Path, proof_directory: str | Path) -> dict:
        """Execute a snapshot and publish its portable five-file proof directory."""
        target = Path(proof_directory)
        _require(not target.exists() and not target.is_symlink(), "proof directory already exists")
        bundle_data = _read_regular(Path(bundle_path), MAX_ARCHIVE_BYTES)
        manifest, members = _load_bundle(bundle_data)
        with tempfile.TemporaryDirectory(prefix="delivery-sandbox-") as temporary:
            sandbox = Path(temporary)
            work = sandbox / "work"
            (work / "project").mkdir(parents=True)
            (work / "home").mkdir()
            for row in manifest["files"]:
                path = work / "project" / row["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(members["delivery/" + row["path"]])
            worker = sandbox / "runner.py"
            worker.write_text(WORKER, encoding="utf-8")
            command = _sandbox_command(self.sandbox_executable, work, worker,
                                       manifest["test_pattern"], self.limits)
            started_at, started = time.time(), time.monotonic()
            returncode, reason, stdout, stderr = _execute(command, self.limits)
            duration = time.monotonic() - started
            finished_at = max(started_at, time.time())
            try:
                report_data = _read_regular(work / "test-results.json", MAX_REPORT_BYTES)
            except (OSError, ValueError):
                report_data = b""
        body = {"schema_version": SCHEMA_VERSION, "type": "delivery_verification_certificate",
                "bundle_sha256": _sha(bundle_data), "manifest_sha256": _sha(_canonical(manifest)),
                "context": manifest["context"], "test_pattern": manifest["test_pattern"],
                "execution": {"sandbox": "bubblewrap", "started_at": started_at, "finished_at": finished_at,
                              "duration_seconds": duration, "limits": dict(vars(self.limits)),
                              "returncode": returncode, "reason": reason,
                              **_summary(report_data, returncode, reason)},
                "evidence": {"stdout": _evidence("stdout.log", stdout),
                             "stderr": _evidence("stderr.log", stderr),
                             "report": _evidence("test-results.json", report_data)}}
        certificate = _seal(body, self.signing_key)
        with tempfile.TemporaryDirectory(prefix=".delivery-proof-", dir=target.parent) as staging:
            stage = Path(staging) / "proof"
            stage.mkdir()
            for filename, data in {"delivery.pyz": bundle_data, "stdout.log": stdout, "stderr.log": stderr,
                                   "test-results.json": report_data, "certificate.json": _canonical(certificate)}.items():
                (stage / filename).write_bytes(data)
            # copytree refuses existing destinations, including symlinks.
            shutil.copytree(stage, target)
        return certificate

    def trigger_milestone_acceptance(self, proof_directory: str | Path,
                                     ledger: FreelanceContractEscrowAudit, *, contract_id: str,
                                     milestone_id: str, client_id: str, token: str,
                                     expected_certificate_sha256: str | None = None) -> dict:
        """Accept the exact passing delivery using an existing client's bearer token."""
        _require(self.signing_key is not None or expected_certificate_sha256 is not None,
                 "acceptance requires an HMAC key or independently saved certificate digest")
        certificate, manifest = _verified_proof(proof_directory, self.signing_key, expected_certificate_sha256)
        if certificate["execution"]["status"] != "passed":
            raise StateError("milestone acceptance requires all tests to pass")
        if manifest["context"] != {"contract_id": contract_id, "milestone_id": milestone_id}:
            raise StateError("proof belongs to a different contract or milestone")
        receipt = ledger.generate_receipt(contract_id, milestone_id)
        if receipt["delivery"]["files"] != manifest["files"]:
            raise StateError("proof does not match the latest recorded delivery")
        if not receipt["escrow"]["verified"]:
            raise StateError("escrow must be verified before automated acceptance")
        ledger.log_client_acceptance(
            contract_id, milestone_id, client_id, token, receipt["delivery"]["delivery_sha256"],
            statement=(f"Client accepted after automated verification: certificate SHA-256 "
                       f"{certificate['certificate_sha256']}; bundle SHA-256 {certificate['bundle_sha256']}; "
                       f"{certificate['execution']['tests']['passed']} passing tests."))
        return ledger.generate_receipt(contract_id, milestone_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package", help="create a self-contained Python test runner")
    package.add_argument("directory", type=Path)
    package.add_argument("bundle", type=Path)
    package.add_argument("--test-pattern", default="test*.py")
    package.add_argument("--contract-id")
    package.add_argument("--milestone-id")
    run = commands.add_parser("run", help="run in Bubblewrap and write a proof directory")
    run.add_argument("bundle", type=Path)
    run.add_argument("proof", type=Path)
    run.add_argument("--timeout", type=float, default=60)
    run.add_argument("--memory-mb", type=int, default=256)
    run.add_argument("--output-limit", type=int, default=1024 * 1024)
    verify = commands.add_parser("verify", help="verify evidence integrity (including failed test runs)")
    accept = commands.add_parser("accept", help="record authenticated client acceptance of a passing delivery")
    for command in (verify, accept):
        command.add_argument("proof", type=Path)
        command.add_argument("--expected-sha256")
    accept.add_argument("--db", type=Path, required=True)
    for name in ("contract-id", "milestone-id", "client-id"):
        accept.add_argument("--" + name, required=True)
    accept.add_argument("--token-file", type=Path, required=True)
    for command in (run, verify, accept):
        command.add_argument("--key-file", type=Path, help="raw shared key bytes; never included in proofs")
    args = parser.parse_args(argv)
    try:
        key = _key(args.key_file.read_bytes()) if getattr(args, "key_file", None) else None
        if args.command == "package":
            result = package_delivery(args.directory, args.bundle, test_pattern=args.test_pattern,
                                      contract_id=args.contract_id, milestone_id=args.milestone_id)
        elif args.command == "verify":
            valid = verify_proof_bundle(args.proof, signing_key=key, expected_certificate_sha256=args.expected_sha256)
            print(json.dumps({"valid": valid}))
            return 0 if valid else 1
        elif args.command == "run":
            harness = AutomatedDeliveryVerificationHarness(
                limits=SandboxLimits(args.timeout, args.memory_mb, args.output_limit), signing_key=key)
            result = harness.run_bundle(args.bundle, args.proof)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["execution"]["status"] == "passed" else 1
        else:
            harness = AutomatedDeliveryVerificationHarness(signing_key=key)
            with FreelanceContractEscrowAudit(args.db) as ledger:
                result = harness.trigger_milestone_acceptance(
                    args.proof, ledger, contract_id=args.contract_id, milestone_id=args.milestone_id,
                    client_id=args.client_id, token=args.token_file.read_text(encoding="utf-8").strip(),
                    expected_certificate_sha256=args.expected_sha256)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
