#!/usr/bin/env python3
"""Delivery harness tests, using real Bubblewrap sandboxes and the SQLite ledger.

Run: python3 -m unittest discover -s scripts -p 'test_automated_delivery_verification_harness.py' -v
Linux and Bubblewrap are required; isolation tests deliberately do not skip.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile

if __package__:
    from . import automated_delivery_verification_harness as harness_module
    from .freelance_contract_escrow_audit import (
        FreelanceContractEscrowAudit, StateError, build_delivery_manifest, verify_receipt,
    )
else:
    import automated_delivery_verification_harness as harness_module
    from freelance_contract_escrow_audit import (
        FreelanceContractEscrowAudit, StateError, build_delivery_manifest, verify_receipt,
    )

Harness = harness_module.AutomatedDeliveryVerificationHarness
Limits = harness_module.SandboxLimits
package_delivery = harness_module.package_delivery
verify_proof_bundle = harness_module.verify_proof_bundle
KEY = b"client-owned-test-signing-key-123456789"
SCRIPT = Path(harness_module.__file__).resolve()
PASSING = '''import unittest
from app import add
class ApplicationTests(unittest.TestCase):
    def test_add(self):
        print('client execution log')
        self.assertEqual(add(2, 3), 5)
    def test_negative(self):
        self.assertEqual(add(-2, -3), -5)
'''


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


class DeliveryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "app.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
        self.write_tests(PASSING)
        self.bundle = self.root / "delivery.pyz"
        self.proof = self.root / "proof"

    def write_tests(self, code):
        (self.source / "test_app.py").write_text(code, encoding="utf-8")

    def run_delivery(self, code=None, *, signing_key=KEY, limits=None, context=True):
        if code is not None:
            self.write_tests(code)
        package_delivery(self.source, self.bundle,
                         **({"contract_id": "c1", "milestone_id": "m1"} if context else {}))
        return Harness(signing_key=signing_key, limits=limits).run_bundle(self.bundle, self.proof)

    def read_certificate(self):
        return json.loads((self.proof / "certificate.json").read_text(encoding="utf-8"))

    def save_certificate(self, certificate, key=KEY):
        """Independently re-sign malformed claims to exercise semantic verification."""
        body = {k: v for k, v in certificate.items() if k not in ("certificate_sha256", "signature")}
        encoded = canonical(body)
        certificate["certificate_sha256"] = sha(encoded)
        certificate["signature"] = None if key is None else {
            "algorithm": "HMAC-SHA256", "value": hmac.new(key, encoded, hashlib.sha256).hexdigest()}
        (self.proof / "certificate.json").write_bytes(canonical(certificate))


class PackagingTests(DeliveryCase):
    def test_deterministic_archive_binds_nested_hidden_unicode_and_binary_files(self):
        (self.source / "docs").mkdir()
        (self.source / "docs" / "说明.md").write_text("交付说明", encoding="utf-8")
        (self.source / ".settings").write_bytes(b"\x00\xff")
        original = build_delivery_manifest(self.source)
        result = package_delivery(self.source, self.bundle, contract_id="c1", milestone_id="m1")
        second = self.root / "again.pyz"
        package_delivery(self.source, second, contract_id="c1", milestone_id="m1")
        self.assertEqual(self.bundle.read_bytes(), second.read_bytes())
        self.assertEqual(result["bundle_sha256"], sha(self.bundle.read_bytes()))
        with zipfile.ZipFile(self.bundle) as archive:
            manifest = json.loads(archive.read("MANIFEST.json"))
            self.assertEqual(manifest["files"], original)
            self.assertEqual(result["manifest_sha256"], sha(canonical(manifest)))
            self.assertEqual(archive.read("delivery/.settings"), b"\x00\xff")
            self.assertEqual(len(manifest["runtime"]), 3)
            self.assertNotIn(str(self.source), json.dumps(manifest))

    def test_self_contained_zipapp_runs_without_source_or_repository_on_path(self):
        package_delivery(self.source, self.bundle)
        for path in self.source.iterdir():
            path.unlink()
        self.source.rmdir()
        result = subprocess.run([sys.executable, "-I", str(self.bundle), str(self.proof)],
                                cwd=self.root, env={}, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["execution"]["tests"]["passed"], 2)
        self.assertTrue(verify_proof_bundle(self.proof))

    def test_custom_discovery_pattern_and_scope_are_preserved(self):
        (self.source / "test_app.py").rename(self.source / "check_app.py")
        package_delivery(self.source, self.bundle, test_pattern="check_*.py",
                         contract_id="客户", milestone_id="阶段")
        certificate = Harness().run_bundle(self.bundle, self.proof)
        self.assertEqual(certificate["context"], {"contract_id": "客户", "milestone_id": "阶段"})
        self.assertEqual(certificate["test_pattern"], "check_*.py")
        self.assertEqual(certificate["execution"]["tests"]["passed"], 2)

    def test_empty_invalid_and_symlink_deliveries_are_rejected(self):
        empty = self.root / "empty"
        empty.mkdir()
        link = self.root / "source-link"
        link.symlink_to(self.source)
        for source in (empty, link, self.root / "missing", self.source / "app.py"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                package_delivery(source, self.bundle)
        for target in (self.source / "app.py", self.root / "missing", empty):
            path = self.source / "link"
            path.symlink_to(target)
            with self.subTest(target=target), self.assertRaises(ValueError):
                package_delivery(self.source, self.bundle)
            path.unlink()
        os.mkfifo(self.source / "pipe")
        with self.assertRaises(ValueError):
            package_delivery(self.source, self.bundle)

    def test_output_cannot_overwrite_or_contaminate_delivery(self):
        with self.assertRaises(ValueError):
            package_delivery(self.source, self.source / "runner.pyz")
        package_delivery(self.source, self.bundle)
        original = self.bundle.read_bytes()
        with self.assertRaises(FileExistsError):
            package_delivery(self.source, self.bundle)
        self.assertEqual(self.bundle.read_bytes(), original)
        Harness().run_bundle(self.bundle, self.proof)
        with self.assertRaises(ValueError):
            Harness().run_bundle(self.bundle, self.proof)

    def test_invalid_patterns_scope_and_unsafe_names_are_rejected(self):
        for pattern in ("", " ", "../test*.py", "/test.py", "x\\test.py", "a\x00.py"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                package_delivery(self.source, self.bundle, test_pattern=pattern)
        for values in ({"contract_id": "c1"}, {"milestone_id": "m1"},
                       {"contract_id": "", "milestone_id": "m1"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                package_delivery(self.source, self.bundle, **values)
        (self.source / "bad\\name.py").write_bytes(b"")
        with self.assertRaises(ValueError):
            package_delivery(self.source, self.bundle)

    def test_changed_missing_and_extra_archive_files_are_rejected_before_execution(self):
        package_delivery(self.source, self.bundle)
        with zipfile.ZipFile(self.bundle) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        variants = [{**members, "delivery/app.py": b"tampered"},
                    {k: v for k, v in members.items() if k != "delivery/app.py"},
                    {**members, "unexpected.txt": b"extra"}]
        for index, variant in enumerate(variants):
            bad = self.root / f"bad-{index}.pyz"
            with zipfile.ZipFile(bad, "w") as archive:
                for name, data in variant.items():
                    archive.writestr(name, data)
            with mock.patch.object(harness_module, "_execute") as execute:
                with self.subTest(index=index), self.assertRaises((ValueError, KeyError)):
                    Harness().run_bundle(bad, self.proof)
                execute.assert_not_called()
        self.assertFalse(self.proof.exists())

    def test_hostile_archive_paths_links_and_duplicate_entries_never_extract(self):
        for index, name in enumerate(("../escape", "/absolute", "C:\\escape", "a/./b", "a//b")):
            bad = self.root / f"path-{index}.pyz"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr(name, b"hostile")
            with self.subTest(name=name), self.assertRaises(ValueError):
                Harness().run_bundle(bad, self.proof)
        bad = self.root / "symlink.pyz"
        with zipfile.ZipFile(bad, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../escape")
        with self.assertRaises(ValueError):
            Harness().run_bundle(bad, self.proof)
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("MANIFEST.json", b"{}")
            with self.assertWarns(UserWarning):
                archive.writestr("MANIFEST.json", b"{}")
        with self.assertRaises(ValueError):
            Harness().run_bundle(bad, self.proof)
        self.assertFalse((self.root / "escape").exists())

    def test_archive_size_and_file_count_limits_are_enforced(self):
        package_delivery(self.source, self.bundle)
        with mock.patch.object(harness_module, "MAX_ARCHIVE_BYTES", 100):
            with self.assertRaises(ValueError):
                Harness().run_bundle(self.bundle, self.proof)
        with mock.patch.object(harness_module, "MAX_FILES", 1):
            with self.assertRaises(ValueError):
                package_delivery(self.source, self.root / "too-many.pyz")


class ExecutionTests(DeliveryCase):
    def test_passing_tests_capture_both_logs_and_preserve_original_source(self):
        self.write_tests(PASSING.replace("print('client execution log')",
                                        "print('client execution log'); __import__('os').write(2, b'raw\\xff\\n')"))
        original = build_delivery_manifest(self.source)
        certificate = self.run_delivery()
        execution = certificate["execution"]
        self.assertEqual(execution["status"], "passed")
        self.assertEqual(execution["tests"]["run"], 2)
        self.assertEqual(execution["tests"]["passed"], 2)
        self.assertIn(b"client execution log", (self.proof / "stdout.log").read_bytes())
        self.assertIn(b"raw\xff", (self.proof / "stderr.log").read_bytes())
        self.assertEqual(build_delivery_manifest(self.source), original)
        self.assertTrue(verify_proof_bundle(self.proof, signing_key=KEY))

    def test_failures_and_errors_are_counted_and_certified(self):
        certificate = self.run_delivery('''import unittest
class Tests(unittest.TestCase):
    def test_ok(self): self.assertTrue(True)
    def test_failure(self): self.assertEqual(1, 2)
    def test_error(self): raise RuntimeError('delivery error')
''')
        execution = certificate["execution"]
        self.assertEqual(execution["status"], "failed")
        self.assertEqual((execution["tests"]["run"], execution["tests"]["passed"],
                          execution["tests"]["failures"], execution["tests"]["errors"]), (3, 1, 1, 1))
        self.assertNotEqual(execution["returncode"], 0)
        self.assertIn("delivery error", (self.proof / "stderr.log").read_text())
        self.assertTrue(verify_proof_bundle(self.proof, signing_key=KEY))

    def test_subtest_failures_count_the_test_once_and_prevent_success(self):
        certificate = self.run_delivery('''import unittest
class Tests(unittest.TestCase):
    def test_subtests(self):
        for number in (1, 2, 3):
            with self.subTest(number=number): self.assertEqual(number, 1)
''')
        counts = certificate["execution"]["tests"]
        self.assertEqual((counts["run"], counts["passed"], counts["failures"]), (1, 0, 1))
        self.assertEqual(certificate["execution"]["status"], "failed")

    def test_empty_suite_never_produces_a_passing_certificate(self):
        certificate = self.run_delivery("# No tests delivered\n")
        self.assertEqual(certificate["execution"]["tests"]["run"], 0)
        self.assertEqual(certificate["execution"]["status"], "failed")
        self.assertTrue(verify_proof_bundle(self.proof, signing_key=KEY))

    def test_discovery_syntax_error_is_recorded_as_an_error(self):
        certificate = self.run_delivery("this is invalid python !!!\n")
        self.assertEqual(certificate["execution"]["tests"]["errors"], 1)
        self.assertEqual(certificate["execution"]["status"], "failed")
        self.assertIn(b"SyntaxError", (self.proof / "stderr.log").read_bytes())

    def test_class_fixture_error_is_not_mistaken_for_zero_successful_tests(self):
        certificate = self.run_delivery('''import unittest
class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise RuntimeError('fixture failed')
    def test_unreachable(self): pass
''')
        self.assertEqual(certificate["execution"]["tests"]["errors"], 1)
        self.assertEqual(certificate["execution"]["status"], "failed")

    def test_skips_expected_failures_and_unexpected_successes_are_explicit(self):
        certificate = self.run_delivery('''import unittest
class Tests(unittest.TestCase):
    @unittest.skip('not delivered')
    def test_skip(self): pass
    @unittest.expectedFailure
    def test_expected(self): self.fail('known issue')
    @unittest.expectedFailure
    def test_unexpected(self): pass
''')
        counts = certificate["execution"]["tests"]
        self.assertEqual((counts["run"], counts["passed"], counts["skipped"],
                          counts["expected_failures"], counts["unexpected_successes"]), (3, 0, 1, 1, 1))
        self.assertEqual(certificate["execution"]["status"], "failed")

    def test_sandbox_hides_host_files_environment_and_network_and_enforces_limits(self):
        secret = self.root / "private-host-secret"
        secret.write_text("host-only")
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        code = f'''import os, pathlib, resource, socket, unittest
class IsolationTests(unittest.TestCase):
    def test_isolation(self):
        self.assertFalse(pathlib.Path({str(secret)!r}).exists())
        self.assertNotIn('DELIVERY_HOST_SECRET', os.environ)
        self.assertEqual(os.environ['HOME'], '/work/home')
        self.assertEqual(resource.getrlimit(resource.RLIMIT_AS)[0], 256 * 1024 * 1024)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_CORE), (0, 0))
        with socket.socket() as client:
            client.settimeout(0.5)
            with self.assertRaises(OSError): client.connect(('127.0.0.1', {listener.getsockname()[1]}))
        with self.assertRaises(OSError): pathlib.Path('/usr/forbidden-delivery-write').write_text('x')
        pathlib.Path('app.py').write_text('changed inside sandbox')
        pathlib.Path('/tmp/private-temp').write_text('sandbox only')
'''
        with mock.patch.dict(os.environ, {"DELIVERY_HOST_SECRET": "must-not-leak"}):
            certificate = self.run_delivery(code)
        self.assertEqual(certificate["execution"]["status"], "passed",
                         (self.proof / "stderr.log").read_text())
        self.assertEqual(secret.read_text(), "host-only")
        self.assertEqual((self.source / "app.py").read_text(), "def add(a, b): return a + b\n")

    def test_timeout_kills_descendants_and_still_bundles_failure_evidence(self):
        marker = "delivery-child-" + self.root.name
        code = f'''import subprocess, sys, time, unittest
class Tests(unittest.TestCase):
    def test_hang(self):
        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', {marker!r}])
        print('child started', flush=True)
        time.sleep(30)
'''
        started = time.monotonic()
        certificate = self.run_delivery(code, limits=Limits(timeout_seconds=0.5))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(certificate["execution"]["status"], "timeout")
        self.assertNotEqual(certificate["execution"]["returncode"], 0)
        self.assertIn(b"child started", (self.proof / "stdout.log").read_bytes())
        self.assertTrue(verify_proof_bundle(self.proof, signing_key=KEY))
        # Check host PID command lines: namespace teardown must kill the child too.
        deadline = time.monotonic() + 2
        while True:
            alive = []
            for path in Path('/proc').glob('[0-9]*/cmdline'):
                try:
                    if marker.encode() in path.read_bytes():
                        alive.append(path)
                except OSError:
                    pass
            if not alive or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        self.assertEqual(alive, [])

    def test_output_flood_is_bounded_and_cannot_earn_acceptance(self):
        certificate = self.run_delivery('''import os, unittest
class Tests(unittest.TestCase):
    def test_flood(self):
        while True:
            os.write(1, b'x' * 8192)
            os.write(2, b'y' * 8192)
''', limits=Limits(timeout_seconds=3, output_limit_bytes=2048))
        self.assertEqual(certificate["execution"]["status"], "output_limit_exceeded")
        size = sum((self.proof / name).stat().st_size for name in ("stdout.log", "stderr.log"))
        self.assertEqual(size, 2048)
        self.assertTrue(verify_proof_bundle(self.proof, signing_key=KEY))

    def test_zero_exit_and_forged_stdout_without_a_report_are_failures(self):
        certificate = self.run_delivery('''import os, unittest
class Tests(unittest.TestCase):
    def test_fake(self):
        print('Ran 999 tests; OK; passed=999', flush=True)
        os._exit(0)
''')
        self.assertEqual(certificate["execution"]["returncode"], 0)
        self.assertEqual(certificate["execution"]["status"], "failed")
        self.assertFalse(certificate["execution"]["report_valid"])
        self.assertEqual(certificate["execution"]["tests"]["passed"], 0)

    def test_missing_sandbox_never_executes_on_host_and_setup_failure_is_failed(self):
        package_delivery(self.source, self.bundle)
        with mock.patch.object(harness_module, "_execute") as execute:
            with self.assertRaises(harness_module.SandboxUnavailableError):
                Harness(sandbox_executable="/no/such/bwrap").run_bundle(self.bundle, self.proof)
            execute.assert_not_called()
        self.assertFalse(self.proof.exists())
        certificate = Harness(sandbox_executable="/usr/bin/false").run_bundle(self.bundle, self.proof)
        self.assertEqual(certificate["execution"]["status"], "failed")
        self.assertFalse(certificate["execution"]["report_valid"])

    def test_invalid_limits_and_short_or_nonbyte_keys_are_rejected(self):
        cases = [{"timeout_seconds": v} for v in (0, -1, True, float('nan'), float('inf'), 3601)]
        cases += [{"memory_limit_mb": v} for v in (True, 31, 4097, 256.0)]
        cases += [{"output_limit_bytes": v} for v in (True, 1023, 16 * 1024 * 1024 + 1)]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Limits(**values)
        for key in (b"", b"short", "x" * 32, 123):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Harness(signing_key=key)


class ProofTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.certificate = self.run_delivery()

    def test_independent_sha256_hmac_and_evidence_checks_match(self):
        certificate = self.certificate
        body = {k: v for k, v in certificate.items() if k not in ("certificate_sha256", "signature")}
        self.assertEqual(certificate["certificate_sha256"], sha(canonical(body)))
        self.assertEqual(certificate["signature"]["value"], hmac.new(KEY, canonical(body), hashlib.sha256).hexdigest())
        for row in certificate["evidence"].values():
            data = (self.proof / row["path"]).read_bytes()
            self.assertEqual((row["size_bytes"], row["sha256"]), (len(data), sha(data)))
        for path in self.proof.iterdir():
            self.assertNotIn(KEY, path.read_bytes())
        self.assertTrue(verify_proof_bundle(self.proof, signing_key=KEY,
                                          expected_certificate_sha256=certificate["certificate_sha256"]))

    def test_tampering_with_every_evidence_file_is_detected(self):
        for path in self.proof.iterdir():
            original = path.read_bytes()
            with self.subTest(path=path.name):
                path.write_bytes(original + b"tampered")
                self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY))
                path.write_bytes(original)
        self.assertTrue(verify_proof_bundle(self.proof, signing_key=KEY))

    def test_hmac_requires_correct_key_and_cannot_be_downgraded(self):
        self.assertFalse(verify_proof_bundle(self.proof))
        self.assertFalse(verify_proof_bundle(self.proof, signing_key=b"wrong-key" * 4))
        self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY, expected_certificate_sha256="0" * 64))
        for signature in (None, {"algorithm": "SHA256", "value": "0" * 64},
                          {"algorithm": "HMAC-SHA256", "value": "0" * 64}):
            changed = copy.deepcopy(self.certificate)
            changed["signature"] = signature
            (self.proof / "certificate.json").write_bytes(canonical(changed))
            self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY))

    def test_recomputed_public_checksum_does_not_bypass_authentication(self):
        changed = copy.deepcopy(self.certificate)
        changed["execution"]["tests"]["passed"] = 999
        body = {k: v for k, v in changed.items() if k not in ("certificate_sha256", "signature")}
        changed["certificate_sha256"] = sha(canonical(body))
        (self.proof / "certificate.json").write_bytes(canonical(changed))
        self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY))

    def test_even_resigned_inconsistent_counts_scope_and_timing_are_rejected(self):
        variants = []
        changed = copy.deepcopy(self.certificate)
        changed["execution"]["tests"]["passed"] = 999
        variants.append(changed)
        changed = copy.deepcopy(self.certificate)
        changed["context"]["milestone_id"] = "m2"
        variants.append(changed)
        changed = copy.deepcopy(self.certificate)
        changed["execution"]["finished_at"] = -1
        variants.append(changed)
        changed = copy.deepcopy(self.certificate)
        changed["execution"]["returncode"] = True
        variants.append(changed)
        changed = copy.deepcopy(self.certificate)
        changed["execution"]["tests"]["errors"] = False
        variants.append(changed)
        changed = copy.deepcopy(self.certificate)
        changed["schema_version"] = True
        variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed):
                self.save_certificate(changed)
                self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY))

    def test_missing_extra_symlink_and_duplicate_json_evidence_are_rejected(self):
        stdout = self.proof / "stdout.log"
        original = stdout.read_bytes()
        stdout.unlink()
        self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY))
        outside = self.root / "outside.log"
        outside.write_bytes(original)
        stdout.symlink_to(outside)
        self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY))
        stdout.unlink()
        stdout.write_bytes(original)
        extra = self.proof / "extra"
        extra.write_bytes(b"")
        self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY))
        extra.unlink()
        link = self.root / "proof-link"
        link.symlink_to(self.proof)
        self.assertFalse(verify_proof_bundle(link, signing_key=KEY))
        (self.proof / "certificate.json").write_bytes(b'{"schema_version":1,"schema_version":1}')
        self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY))

    def test_malformed_structured_report_cannot_claim_a_valid_passing_summary(self):
        for data in (b"not JSON", b'{"schema_version":1}', b"[]", b'{"counts":NaN}'):
            certificate = copy.deepcopy(self.certificate)
            (self.proof / "test-results.json").write_bytes(data)
            certificate["evidence"]["report"] = {"path": "test-results.json", "size_bytes": len(data), "sha256": sha(data)}
            self.save_certificate(certificate)
            with self.subTest(data=data):
                self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY))

    def test_unkeyed_proof_can_be_verified_against_independently_saved_digest(self):
        certificate = copy.deepcopy(self.certificate)
        self.save_certificate(certificate, key=None)
        digest = certificate["certificate_sha256"]
        self.assertTrue(verify_proof_bundle(self.proof, expected_certificate_sha256=digest))
        self.assertFalse(verify_proof_bundle(self.proof, expected_certificate_sha256="0" * 64))
        self.assertFalse(verify_proof_bundle(self.proof, signing_key=KEY))


class AcceptanceTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.db = self.root / "escrow.sqlite3"
        self.ledger = FreelanceContractEscrowAudit(self.db, clock=lambda: self.now)
        self.addCleanup(self.ledger.close)
        self.ledger.create_contract("c1", "client-1", "developer", "USD", 10000, "Deliver app and passing tests")
        self.ledger.add_milestone("c1", "m1", "Application", 10000, "All agreed tests pass")
        self.fund()
        self.ledger.record_delivery("c1", "m1", self.source)
        self.challenge = self.ledger.issue_acceptance_token("c1", "m1", ttl_seconds=60)
        self.certificate = self.run_delivery()

    def fund(self, **changes):
        evidence = {"provider": "authenticated-adapter", "deposit_id": "deposit-1", "contract_id": "c1",
                    "client_id": "client-1", "currency": "USD", "amount_minor": 10000,
                    "status": "funded", "observed_at": self.now}
        evidence.update(changes)
        return self.ledger.verify_escrow_deposit("c1", evidence)

    def accept(self, harness=None, **changes):
        arguments = {"contract_id": "c1", "milestone_id": "m1", "client_id": "client-1",
                     "token": self.challenge["token"]}
        arguments.update(changes)
        return (harness or Harness(signing_key=KEY)).trigger_milestone_acceptance(self.proof, self.ledger, **arguments)

    def test_successful_acceptance_binds_certificate_to_ledger_and_rejects_replay(self):
        receipt = self.accept()
        self.assertEqual(receipt["status"], "accepted")
        self.assertTrue(verify_receipt(receipt))
        statement = receipt["acceptance"]["statement"]
        self.assertIn(self.certificate["certificate_sha256"], statement)
        self.assertIn(self.certificate["bundle_sha256"], statement)
        self.assertIn("2 passing tests", statement)
        before = self.ledger.verify_ledger("c1")
        with self.assertRaises(StateError):
            self.accept()
        self.assertEqual(self.ledger.verify_ledger("c1"), before)

    def test_wrong_expired_and_incorrect_client_tokens_do_not_change_ledger(self):
        before = self.ledger.verify_ledger("c1")
        for changes in ({"token": "wrong"}, {"client_id": "impostor"}):
            with self.subTest(changes=changes), self.assertRaises(StateError):
                self.accept(**changes)
            self.assertEqual(self.ledger.verify_ledger("c1"), before)
        self.now += 60
        with self.assertRaises(StateError):
            self.accept()
        self.assertEqual(self.ledger.verify_ledger("c1"), before)

    def test_scope_mismatch_and_changed_latest_delivery_prevent_acceptance(self):
        with self.assertRaises(StateError):
            self.accept(contract_id="another-contract")
        with self.assertRaises(StateError):
            self.accept(milestone_id="another-milestone")
        (self.source / "app.py").write_text("def add(a, b): return 0\n")
        self.ledger.record_delivery("c1", "m1", self.source)
        self.challenge = self.ledger.issue_acceptance_token("c1", "m1")
        before = self.ledger.verify_ledger("c1")
        with self.assertRaisesRegex(StateError, "latest recorded delivery"):
            self.accept()
        self.assertEqual(self.ledger.verify_ledger("c1"), before)

    def test_identical_revision_still_invalidates_old_client_token(self):
        self.ledger.record_delivery("c1", "m1", self.source)
        before = self.ledger.verify_ledger("c1")
        with self.assertRaises(StateError):
            self.accept()
        self.assertEqual(self.ledger.verify_ledger("c1"), before)

    def test_failing_proof_does_not_trigger_or_mint_acceptance(self):
        # Publish a real failed run for the same ledger scope.
        self.bundle = self.root / "failed.pyz"
        self.proof = self.root / "failed-proof"
        self.run_delivery("import unittest\nclass Tests(unittest.TestCase):\n    def test_fail(self): self.fail('unfinished')\n")
        before = self.ledger.verify_ledger("c1")
        with self.assertRaisesRegex(StateError, "all tests to pass"):
            self.accept()
        self.assertEqual(self.ledger.verify_ledger("c1"), before)

    def test_unverified_and_stale_escrow_prevent_automated_acceptance(self):
        self.now += 1
        self.fund(status="chargeback")
        before = self.ledger.verify_ledger("c1")
        with self.assertRaisesRegex(StateError, "escrow must be verified"):
            self.accept()
        self.assertEqual(self.ledger.verify_ledger("c1"), before)
        self.now += 1
        self.fund()
        self.challenge = self.ledger.issue_acceptance_token("c1", "m1", ttl_seconds=100000)
        self.now += 86401
        with self.assertRaisesRegex(StateError, "escrow must be verified"):
            self.accept()

    def test_acceptance_requires_hmac_or_saved_digest_and_pinned_unsigned_proof_works(self):
        self.save_certificate(self.certificate, key=None)
        before = self.ledger.verify_ledger("c1")
        with self.assertRaisesRegex(ValueError, "independently saved"):
            self.accept(Harness())
        self.assertEqual(self.ledger.verify_ledger("c1"), before)
        receipt = self.accept(Harness(), expected_certificate_sha256=self.certificate["certificate_sha256"])
        self.assertEqual(receipt["status"], "accepted")

    def test_tampered_evidence_leaves_client_token_usable_after_restoration(self):
        path = self.proof / "stdout.log"
        original = path.read_bytes()
        path.write_bytes(b"forged")
        before = self.ledger.verify_ledger("c1")
        with self.assertRaises(ValueError):
            self.accept()
        self.assertEqual(self.ledger.verify_ledger("c1"), before)
        path.write_bytes(original)
        self.assertEqual(self.accept()["status"], "accepted")

    def test_accept_cli_reads_token_and_key_files_and_returns_a_verifiable_receipt(self):
        key_file, token_file = self.root / "client.key", self.root / "acceptance.token"
        key_file.write_bytes(KEY)
        token_file.write_text(self.challenge["token"] + "\n")
        result = subprocess.run([sys.executable, str(SCRIPT), "accept", str(self.proof), "--db", str(self.db),
                                 "--contract-id", "c1", "--milestone-id", "m1", "--client-id", "client-1",
                                 "--token-file", str(token_file), "--key-file", str(key_file)],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "accepted")
        self.assertTrue(verify_receipt(receipt))


class CLITests(DeliveryCase):
    def invoke(self, *arguments):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, arguments)],
                              cwd=self.root, capture_output=True, text=True, timeout=15)

    def test_package_run_and_verify_commands_emit_json_and_correct_exit_codes(self):
        key_file = self.root / "key"
        key_file.write_bytes(KEY)
        packaged = self.invoke("package", self.source, self.bundle, "--contract-id", "c1", "--milestone-id", "m1")
        self.assertEqual(packaged.returncode, 0, packaged.stderr)
        self.assertEqual(len(json.loads(packaged.stdout)["bundle_sha256"]), 64)
        run = self.invoke("run", self.bundle, self.proof, "--key-file", key_file)
        self.assertEqual(run.returncode, 0, run.stderr)
        certificate = json.loads(run.stdout)
        verified = self.invoke("verify", self.proof, "--key-file", key_file,
                               "--expected-sha256", certificate["certificate_sha256"])
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["valid"])
        (self.proof / "stderr.log").write_text("changed")
        invalid = self.invoke("verify", self.proof, "--key-file", key_file)
        self.assertEqual(invalid.returncode, 1)
        self.assertFalse(json.loads(invalid.stdout)["valid"])

    def test_failed_tests_exit_one_and_invalid_inputs_exit_two_with_json_error(self):
        self.write_tests("# No test cases\n")
        self.assertEqual(self.invoke("package", self.source, self.bundle).returncode, 0)
        failed = self.invoke("run", self.bundle, self.proof)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(failed.stdout)["execution"]["status"], "failed")
        missing = self.invoke("package", self.root / "missing", self.root / "missing.pyz")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("error", json.loads(missing.stderr))
        short_key = self.root / "short.key"
        short_key.write_bytes(b"short")
        invalid = self.invoke("verify", self.proof, "--key-file", short_key)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("error", json.loads(invalid.stderr))


if __name__ == "__main__":
    unittest.main()
