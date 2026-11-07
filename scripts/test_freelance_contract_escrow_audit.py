#!/usr/bin/env python3
"""Offline tests for the freelance evidence ledger, including hostile evidence.

Run from the repository root:
    python3 -m unittest discover -s scripts -p 'test_freelance_contract_escrow_audit.py' -v
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

if __package__:
    from .freelance_contract_escrow_audit import (
        FreelanceContractEscrowAudit, LedgerIntegrityError, StateError,
        build_delivery_manifest, hash_file, hash_milestone, main,
        verify_dispute_packet, verify_receipt,
    )
else:
    from freelance_contract_escrow_audit import (
        FreelanceContractEscrowAudit, LedgerIntegrityError, StateError,
        build_delivery_manifest, hash_file, hash_milestone, main,
        verify_dispute_packet, verify_receipt,
    )


def checksum(value):
    """Independent encoder for checking exported evidence and simulating edits."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=1):
        self.now += seconds


class HashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_sha256_known_vectors_and_large_binary_file(self):
        path = self.root / "artifact"
        path.write_bytes(b"")
        self.assertEqual(hash_file(path), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        path.write_bytes(b"abc")
        self.assertEqual(hash_file(path), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        content = bytes(range(256)) * 8193
        path.write_bytes(content)
        self.assertEqual(hash_file(path), hashlib.sha256(content).hexdigest())

    def test_milestone_hash_is_canonical_and_binds_terms(self):
        terms = {"title": "交付", "amount_minor": 999, "criteria": ["Tests pass", "Docs supplied"]}
        digest = hash_milestone(terms)
        self.assertEqual(digest, checksum(terms))
        self.assertEqual(digest, hash_milestone(dict(reversed(list(terms.items())))))
        self.assertEqual(digest, hash_milestone({**terms, "milestone_sha256": digest}))
        for changes in ({"amount_minor": 1000}, {"title": "Delivery"},
                        {"criteria": ["Docs supplied", "Tests pass"]}):
            with self.subTest(changes=changes):
                self.assertNotEqual(digest, hash_milestone({**terms, **changes}))

    def test_non_json_or_nonfinite_milestone_data_is_rejected(self):
        for value in (None, [], {1: "key"}, {"x": object()}, {"x": (1, 2)},
                      {"x": float("nan")}, {"x": float("inf")}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hash_milestone(value)

    def test_manifest_includes_hidden_nested_and_unicode_files_in_sorted_order(self):
        (self.root / "z.bin").write_bytes(b"\x00\xff")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "说明.md").write_text("hello", encoding="utf-8")
        (self.root / ".config").write_bytes(b"")
        manifest = build_delivery_manifest(self.root)
        self.assertEqual([row["path"] for row in manifest], [".config", "docs/说明.md", "z.bin"])
        self.assertEqual([row["size_bytes"] for row in manifest], [0, 5, 2])
        self.assertEqual(manifest[-1]["sha256"], hashlib.sha256(b"\x00\xff").hexdigest())
        self.assertNotIn(str(self.root), json.dumps(manifest))

    def test_symlink_files_directories_and_roots_are_rejected(self):
        real = self.root / "real"
        real.mkdir()
        (real / "file").write_bytes(b"private")
        delivery = self.root / "delivery"
        delivery.mkdir()
        for target in (real, real / "file", self.root / "missing"):
            with self.subTest(target=target):
                link = delivery / "link"
                link.symlink_to(target)
                with self.assertRaises(ValueError):
                    build_delivery_manifest(delivery)
                link.unlink()
        link = self.root / "root-link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            build_delivery_manifest(link)
        with self.assertRaises(OSError):
            hash_file(link)

    def test_nonregular_files_and_invalid_roots_are_rejected(self):
        for path in (self.root / "missing", self.root / "file"):
            if path.name == "file":
                path.write_bytes(b"not a directory")
            with self.subTest(path=path), self.assertRaises(ValueError):
                build_delivery_manifest(path)
        with self.assertRaises(ValueError):
            hash_file(self.root)
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            build_delivery_manifest(self.root)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.delivery_dir = self.root / "delivery"
        self.delivery_dir.mkdir()
        (self.delivery_dir / "app.py").write_bytes(b"print('delivered')\n")
        (self.delivery_dir / "docs").mkdir()
        (self.delivery_dir / "docs" / "README.md").write_text("Usage: python app.py\n", encoding="utf-8")
        self.clock = Clock()
        self.db_path = self.root / "ledger.sqlite3"
        self.ledger = FreelanceContractEscrowAudit(self.db_path, clock=self.clock)
        self.addCleanup(self.ledger.close)
        self.create_contract()
        self.ledger.add_milestone("c1", "m1", "Application", 10_000, "Runs and includes documentation")

    def create_contract(self, identifier="c1", **changes):
        args = {"contract_id": identifier, "client_id": "client-1", "developer_id": "dev-1",
                "currency": "USD", "amount_minor": 10_000, "terms": "Deliver the agreed application"}
        args.update(changes)
        return self.ledger.create_contract(**args)

    def evidence(self, **changes):
        evidence = {"provider": "test-provider", "deposit_id": "deposit-123", "contract_id": "c1",
                    "client_id": "client-1", "currency": "USD", "amount_minor": 10_000,
                    "status": "funded", "observed_at": self.clock()}
        evidence.update(changes)
        return evidence

    def deliver(self):
        return self.ledger.record_delivery("c1", "m1", self.delivery_dir, note="First delivery")

    def accept(self, challenge=None, **changes):
        if challenge is None:
            challenge = self.ledger.issue_acceptance_token("c1", "m1")
        args = {"contract_id": "c1", "milestone_id": "m1", "client_id": "client-1",
                "token": challenge["token"], "delivery_sha256": challenge["delivery_sha256"]}
        args.update(changes)
        return self.ledger.log_client_acceptance(**args)

    def head(self):
        return self.ledger.verify_ledger("c1")


class LedgerTests(LedgerCase):
    def test_complete_funded_delivery_acceptance_and_export_workflow(self):
        self.assertTrue(self.ledger.verify_escrow_deposit("c1", self.evidence())["verified"])
        delivery = self.deliver()
        acceptance = self.accept(statement="Accepted after running the delivered tests.")
        packet = self.ledger.generate_dispute_packet("c1")
        receipt = packet["receipts"][0]
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["amount_minor"], 10_000)
        self.assertEqual(acceptance["delivery_sha256"], delivery["delivery_sha256"])
        self.assertEqual(packet["warnings"], [])
        self.assertEqual(packet["ledger"]["event_count"], 6)
        self.assertTrue(verify_dispute_packet(packet, expected_head=self.head()["head_sha256"]))
        self.assertTrue(verify_receipt(receipt))
        self.assertTrue(self.ledger.verify_delivery("c1", "m1", self.delivery_dir))

    def test_duplicate_contract_milestone_and_overallocation_are_atomic(self):
        before = self.head()
        operations = [lambda: self.create_contract(),
                      lambda: self.ledger.add_milestone("c1", "m1", "Duplicate", 1, "Criteria"),
                      lambda: self.ledger.add_milestone("c1", "m2", "Too much", 1, "Criteria")]
        for operation in operations:
            with self.assertRaises(StateError):
                operation()
            self.assertEqual(self.head(), before)

    def test_contract_validation_rejects_invalid_money_currency_and_identifiers(self):
        cases = []
        for key, values in {
            "contract_id": ("", " ", None), "client_id": ("", 1), "developer_id": (None, ""),
            "currency": ("usd", "US", "USDD", None, "123"),
            "amount_minor": (0, -1, True, 10.5, "100", 2**63), "terms": (None, " "),
            "escrow_max_age_seconds": (0, -1, True, 1.5),
        }.items():
            cases.extend({key: value} for value in values)
        before = self.head()
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.create_contract("invalid", **changes)
        self.assertEqual(self.head(), before)
        with self.assertRaises(KeyError):
            self.ledger.verify_ledger("invalid")

    def test_invalid_milestones_and_empty_deliveries_do_not_append_events(self):
        before = self.head()
        for args in (("", "Title", 1, "Criteria"), ("m2", "", 1, "Criteria"),
                     ("m2", "Title", True, "Criteria"), ("m2", "Title", 1, " ")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.ledger.add_milestone("c1", *args)
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            self.ledger.record_delivery("c1", "m1", empty)
        with self.assertRaises(ValueError):
            self.ledger.record_delivery("c1", "m1", self.delivery_dir, note=None)
        self.assertEqual(self.head(), before)

    def test_changed_added_deleted_and_renamed_files_fail_delivery_verification(self):
        self.deliver()
        file = self.delivery_dir / "app.py"
        original = file.read_bytes()
        file.write_bytes(original.replace(b"delivered", b"tampered!"))
        self.assertFalse(self.ledger.verify_delivery("c1", "m1", self.delivery_dir))
        file.write_bytes(original)
        extra = self.delivery_dir / "extra"
        extra.write_bytes(b"new content")
        self.assertFalse(self.ledger.verify_delivery("c1", "m1", self.delivery_dir))
        extra.unlink()
        renamed = self.delivery_dir / "renamed.py"
        file.rename(renamed)
        self.assertFalse(self.ledger.verify_delivery("c1", "m1", self.delivery_dir))
        renamed.rename(file)
        file.unlink()
        self.assertFalse(self.ledger.verify_delivery("c1", "m1", self.delivery_dir))
        file.write_bytes(original)
        self.assertTrue(self.ledger.verify_delivery("c1", "m1", self.delivery_dir))

    def test_revisions_keep_history_and_change_manifest_digest(self):
        first = self.deliver()
        (self.delivery_dir / "app.py").write_text("print('revision two')\n", encoding="utf-8")
        second = self.deliver()
        packet = self.ledger.generate_dispute_packet("c1")
        self.assertEqual([d["revision"] for d in packet["milestones"][0]["deliveries"]], [1, 2])
        self.assertNotEqual(first["delivery_sha256"], second["delivery_sha256"])
        self.assertEqual(packet["receipts"][0]["delivery"], second)
        self.assertTrue(verify_dispute_packet(packet))

    def test_identical_files_for_different_contracts_have_different_delivery_hashes(self):
        first = self.deliver()
        self.create_contract("c2", terms="A different agreement")
        self.ledger.add_milestone("c2", "m1", "Application", 10_000, "Runs and includes documentation")
        second = self.ledger.record_delivery("c2", "m1", self.delivery_dir)
        self.assertEqual(first["files"], second["files"])
        self.assertNotEqual(first["delivery_sha256"], second["delivery_sha256"])

    def test_unknown_contract_and_milestone_are_explicit_errors(self):
        before = self.head()
        for call in (lambda: self.ledger.generate_dispute_packet("missing"),
                     lambda: self.ledger.generate_receipt("c1", "missing"),
                     lambda: self.ledger.record_delivery("c1", "missing", self.delivery_dir),
                     lambda: self.ledger.issue_acceptance_token("missing", "m1")):
            with self.assertRaises(KeyError):
                call()
        self.assertEqual(before, self.head())

    def test_returned_objects_cannot_modify_persisted_evidence(self):
        delivery = self.deliver()
        digest = delivery["delivery_sha256"]
        delivery["files"][0]["sha256"] = "forged"
        receipt = self.ledger.generate_receipt("c1", "m1")
        self.assertEqual(receipt["delivery"]["delivery_sha256"], digest)
        self.assertNotEqual(receipt["delivery"]["files"][0]["sha256"], "forged")
        receipt["client_id"] = "another-client"
        self.assertEqual(self.ledger.generate_receipt("c1", "m1")["client_id"], "client-1")

    def test_restart_preserves_chains_and_acceptance_tokens(self):
        self.deliver()
        token = self.ledger.issue_acceptance_token("c1", "m1")
        before = self.head()
        with FreelanceContractEscrowAudit(self.db_path, clock=self.clock) as reopened:
            self.assertEqual(reopened.verify_ledger("c1"), before)
            reopened.log_client_acceptance("c1", "m1", "client-1", token["token"], token["delivery_sha256"])
        self.assertEqual(self.ledger.generate_receipt("c1", "m1")["status"], "accepted")

    def test_corrupt_payload_blocks_exports_and_further_writes(self):
        self.deliver()
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE escrow_events SET payload = ? WHERE contract_id = 'c1' AND sequence = 2",
                       ('{"milestone_id":"forged"}',))
        for call in (lambda: self.head(), lambda: self.ledger.generate_dispute_packet("c1"),
                     lambda: self.ledger.issue_acceptance_token("c1", "m1")):
            with self.assertRaises(LedgerIntegrityError):
                call()
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM escrow_events").fetchone()[0], 3)

    def test_deleted_middle_event_is_detected(self):
        self.deliver()
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM escrow_events WHERE sequence = 2")
        with self.assertRaises(LedgerIntegrityError):
            self.head()

    def test_external_checkpoint_detects_valid_prefix_truncation(self):
        self.deliver()
        saved = self.head()["head_sha256"]
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM escrow_events WHERE sequence = 3")
        self.assertEqual(self.head()["event_count"], 2)
        with self.assertRaises(LedgerIntegrityError):
            self.ledger.verify_ledger("c1", expected_head=saved)

    def test_separate_connections_serialize_single_use_token_consumption(self):
        self.deliver()
        token = self.ledger.issue_acceptance_token("c1", "m1")

        def try_accept(_):
            with FreelanceContractEscrowAudit(self.db_path, clock=self.clock) as worker:
                try:
                    worker.log_client_acceptance("c1", "m1", "client-1", token["token"], token["delivery_sha256"])
                    return "accepted"
                except StateError:
                    return "already_consumed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(try_accept, range(2)), ["accepted", "already_consumed"])
        self.assertEqual(self.head()["event_count"], 5)

    def test_clock_rollback_and_invalid_clock_do_not_write_events(self):
        before = self.head()
        for value in (self.clock() - 1, float("nan"), float("inf"), True, -1, 10**1000):
            self.clock.now = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.deliver()
            self.assertEqual(self.head(), before)


class AcceptanceTests(LedgerCase):
    def test_only_token_digest_is_persisted_or_exported(self):
        self.deliver()
        token = self.ledger.issue_acceptance_token("c1", "m1")
        self.accept(token)
        packet = self.ledger.generate_dispute_packet("c1")
        encoded = json.dumps(packet)
        self.assertNotIn(token["token"], encoded)
        self.assertNotIn(token["token"].encode(), self.db_path.read_bytes())
        self.assertIn(hashlib.sha256(token["token"].encode()).hexdigest(), encoded)

    def test_wrong_token_client_and_delivery_are_rejected_without_consuming_token(self):
        self.deliver()
        token = self.ledger.issue_acceptance_token("c1", "m1")
        before = self.head()
        for changes in ({"token": "wrong"}, {"client_id": "attacker"},
                        {"delivery_sha256": "0" * 64}, {"token": " "}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.accept(token, **changes)
            self.assertEqual(self.head(), before)
        self.assertEqual(self.accept(token)["client_id"], "client-1")

    def test_acceptance_and_token_issuance_require_delivery(self):
        with self.assertRaises(StateError):
            self.ledger.issue_acceptance_token("c1", "m1")
        with self.assertRaises(StateError):
            self.accept({"token": "anything", "delivery_sha256": "0" * 64})
        with self.assertRaises(StateError):
            self.ledger.verify_delivery("c1", "m1", self.delivery_dir)

    def test_expiry_boundary_and_reissuing_after_expiration(self):
        self.deliver()
        token = self.ledger.issue_acceptance_token("c1", "m1", ttl_seconds=10)
        self.clock.advance(10)
        before = self.head()
        with self.assertRaises(StateError):
            self.accept(token)
        self.assertEqual(self.head(), before)
        fresh = self.ledger.issue_acceptance_token("c1", "m1", ttl_seconds=10)
        self.clock.advance(9.999)
        self.accept(fresh)
        self.assertTrue(verify_dispute_packet(self.ledger.generate_dispute_packet("c1")))

    def test_invalid_ttls_and_empty_acceptance_statements_are_rejected(self):
        self.deliver()
        before = self.head()
        for ttl in (0, -1, True, 1.5, float("inf"), "10", 2**63):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.ledger.issue_acceptance_token("c1", "m1", ttl_seconds=ttl)
        self.assertEqual(self.head(), before)
        token = self.ledger.issue_acceptance_token("c1", "m1")
        for statement in (None, "", " ", 123):
            with self.subTest(statement=statement), self.assertRaises(ValueError):
                self.accept(token, statement=statement)

    def test_reissue_supersedes_the_old_token(self):
        self.deliver()
        old = self.ledger.issue_acceptance_token("c1", "m1")
        fresh = self.ledger.issue_acceptance_token("c1", "m1")
        self.assertNotEqual(old["token"], fresh["token"])
        with self.assertRaises(StateError):
            self.accept(old)
        self.accept(fresh)

    def test_new_revision_invalidates_tokens_even_for_identical_files(self):
        self.deliver()
        old = self.ledger.issue_acceptance_token("c1", "m1")
        self.deliver()
        with self.assertRaises(StateError):
            self.accept(old)
        fresh = self.ledger.issue_acceptance_token("c1", "m1")
        self.assertEqual(old["delivery_sha256"], fresh["delivery_sha256"])
        with self.assertRaises(StateError):
            self.accept(old)
        self.accept(fresh)

    def test_acceptance_replay_and_post_acceptance_modifications_are_rejected(self):
        self.deliver()
        token = self.ledger.issue_acceptance_token("c1", "m1")
        accepted = self.accept(token, statement="Approved ✔")
        before = self.head()
        for call in (lambda: self.accept(token), lambda: self.deliver(),
                     lambda: self.ledger.issue_acceptance_token("c1", "m1")):
            with self.assertRaises(StateError):
                call()
        self.assertEqual(before, self.head())
        self.assertEqual(self.ledger.generate_receipt("c1", "m1")["acceptance"], accepted)

    def test_token_is_bound_to_contract_and_milestone(self):
        self.create_contract("c2", amount_minor=20_000)
        for mid in ("m1", "m2"):
            self.ledger.add_milestone("c2", mid, "Other application", 10_000, "Other criteria")
            self.ledger.record_delivery("c2", mid, self.delivery_dir)
        first = self.ledger.issue_acceptance_token("c2", "m1")
        second = self.ledger.issue_acceptance_token("c2", "m2")
        self.deliver()
        original = self.ledger.issue_acceptance_token("c1", "m1")
        with self.assertRaises(StateError):
            self.ledger.log_client_acceptance("c2", "m2", "client-1", first["token"], second["delivery_sha256"])
        with self.assertRaises(StateError):
            self.accept(original, token=first["token"])
        self.accept(original)


class EscrowTests(LedgerCase):
    def test_full_or_excess_deposit_is_verified_but_partial_snapshots_are_not_summed(self):
        for amount in (5000, 5000, 10_000, 15_000):
            self.clock.advance()
            result = self.ledger.verify_escrow_deposit("c1", self.evidence(amount_minor=amount))
            self.assertEqual(result["verified"], amount >= 10_000)
        self.assertEqual(len(self.ledger.generate_dispute_packet("c1")["events"]), 6)

    def test_pending_failed_released_refunded_and_chargeback_statuses_are_not_funded(self):
        for status in ("funded", "pending", "failed", "released", "refunded", "chargeback"):
            self.clock.advance()
            result = self.ledger.verify_escrow_deposit("c1", self.evidence(status=status))
            self.assertEqual(result["verified"], status == "funded")
            self.assertEqual(result["status"], status)
            if status != "funded":
                self.assertIn("funds_not_held", result["reasons"])
        packet = self.ledger.generate_dispute_packet("c1")
        self.assertFalse(packet["escrow"]["verified"])
        self.assertEqual(packet["escrow"]["status"], "chargeback")
        self.assertEqual(packet["events"][-1]["payload"]["status"], "chargeback")

    def test_mismatched_contract_client_currency_and_amount_are_logged_as_failed_checks(self):
        cases = [({"contract_id": "other"}, "contract_mismatch"),
                 ({"client_id": "other"}, "client_mismatch"),
                 ({"currency": "EUR"}, "currency_mismatch"),
                 ({"amount_minor": 9999}, "insufficient_deposit")]
        for changes, reason in cases:
            self.clock.advance()
            with self.subTest(changes=changes):
                result = self.ledger.verify_escrow_deposit("c1", self.evidence(**changes))
                self.assertFalse(result["verified"])
                self.assertEqual(result["reasons"], [reason])
                self.assertEqual(self.ledger.generate_dispute_packet("c1")["events"][-1]["payload"],
                                 result["evidence"])

    def test_freshness_is_recomputed_for_packets_and_receipts_without_writing(self):
        self.ledger.verify_escrow_deposit("c1", self.evidence())
        self.deliver()
        before = self.head()
        self.clock.advance(86400)
        self.assertTrue(self.ledger.generate_receipt("c1", "m1")["escrow"]["verified"])
        self.clock.advance(0.001)
        packet = self.ledger.generate_dispute_packet("c1")
        self.assertEqual(packet["escrow"]["reasons"], ["stale_evidence"])
        self.assertFalse(packet["receipts"][0]["escrow"]["verified"])
        self.assertEqual(self.head(), before)
        self.assertTrue(verify_dispute_packet(packet))

    def test_stale_evidence_is_logged_and_never_verified(self):
        result = self.ledger.verify_escrow_deposit("c1", self.evidence(observed_at=self.clock() - 86401))
        self.assertEqual(result["reasons"], ["stale_evidence"])
        self.assertFalse(result["verified"])

    def test_invalid_snapshot_schema_types_status_and_future_time_do_not_write(self):
        base = self.evidence()
        invalid = [None, [], {}, {**base, "extra": "untrusted"}]
        for key, values in {
            "provider": (None, " "), "deposit_id": (1, ""), "client_id": (None,),
            "contract_id": ("",), "currency": ("usd", True),
            "amount_minor": (-1, True, 1.5, 2**63),
            "status": ("paid", None, []),
            "observed_at": (float("nan"), float("inf"), -1, True, "today", self.clock() + 1, 10**1000),
        }.items():
            invalid.extend({**base, key: value} for value in values)
        before = self.head()
        for evidence in invalid:
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.ledger.verify_escrow_deposit("c1", evidence)
        self.assertEqual(self.head(), before)

    def test_older_or_conflicting_snapshots_cannot_reactivate_reversed_funds(self):
        original = self.evidence()
        self.ledger.verify_escrow_deposit("c1", original)
        self.clock.advance()
        reversal = self.evidence(status="chargeback")
        self.ledger.verify_escrow_deposit("c1", reversal)
        before = self.head()
        for stale in (original, self.evidence(status="funded")):
            with self.assertRaises(StateError):
                self.ledger.verify_escrow_deposit("c1", stale)
        self.assertEqual(self.head(), before)
        self.assertFalse(self.ledger.verify_escrow_deposit("c1", reversal)["verified"])

    def test_missing_deposit_is_explicit_and_does_not_invent_payment(self):
        self.deliver()
        receipt = self.ledger.generate_receipt("c1", "m1")
        self.assertFalse(receipt["escrow"]["verified"])
        self.assertEqual(receipt["escrow"]["status"], "missing")
        self.assertIsNone(receipt["escrow"]["evidence"])
        self.assertEqual(receipt["status"], "delivered")


class PacketReceiptTests(LedgerCase):
    def test_receipt_requires_delivery_and_distinguishes_pending_acceptance(self):
        with self.assertRaises(StateError):
            self.ledger.generate_receipt("c1", "m1")
        self.deliver()
        pending = self.ledger.generate_receipt("c1", "m1")
        self.assertEqual(pending["status"], "delivered")
        self.assertIsNone(pending["acceptance"])
        self.accept()
        accepted = self.ledger.generate_receipt("c1", "m1")
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["ledger"], self.head())
        self.assertNotEqual(pending["receipt_sha256"], accepted["receipt_sha256"])
        self.assertTrue(verify_receipt(pending))
        self.assertTrue(verify_receipt(accepted, expected_sha256=accepted["receipt_sha256"]))

    def test_receipt_checksum_and_external_anchor_detect_edits(self):
        self.deliver()
        receipt = self.ledger.generate_receipt("c1", "m1")
        saved = receipt["receipt_sha256"]
        receipt["amount_minor"] += 1
        self.assertFalse(verify_receipt(receipt))
        receipt["receipt_sha256"] = checksum({key: value for key, value in receipt.items()
                                               if key != "receipt_sha256"})
        self.assertTrue(verify_receipt(receipt))
        self.assertFalse(verify_receipt(receipt, expected_sha256=saved))

    def test_packet_is_portable_json_with_independently_reproducible_checksums(self):
        self.deliver()
        self.accept()
        packet = json.loads(json.dumps(self.ledger.generate_dispute_packet("c1"), ensure_ascii=False))
        self.assertTrue(verify_dispute_packet(packet))
        self.assertEqual(packet["packet_sha256"], checksum({key: value for key, value in packet.items()
                                                           if key != "packet_sha256"}))
        self.assertTrue(all(verify_receipt(receipt) for receipt in packet["receipts"]))
        self.assertEqual(packet, self.ledger.generate_dispute_packet("c1"))

    def test_packet_summary_edits_are_detected_even_if_envelope_is_rehashed(self):
        self.deliver()
        original = self.ledger.generate_dispute_packet("c1")
        for section in ("contract", "receipts", "warnings", "escrow"):
            packet = copy.deepcopy(original)
            if section == "contract":
                packet[section]["client_id"] = "forged"
            elif section == "receipts":
                packet[section][0]["status"] = "accepted"
            elif section == "warnings":
                packet[section] = []
            else:
                packet[section]["verified"] = True
            packet["packet_sha256"] = checksum({key: value for key, value in packet.items()
                                                if key != "packet_sha256"})
            with self.subTest(section=section):
                self.assertFalse(verify_dispute_packet(packet))

    def test_packet_event_edits_reordering_deletion_and_wrong_checkpoint_fail(self):
        self.deliver()
        self.accept()
        original = self.ledger.generate_dispute_packet("c1")
        edits = [lambda events: events[1]["payload"].update(title="forged"),
                 lambda events: events.reverse(), lambda events: events.pop(1),
                 lambda events: events.pop(), lambda events: events[1].update(previous_sha256="f" * 64)]
        for edit in edits:
            packet = copy.deepcopy(original)
            edit(packet["events"])
            with self.subTest(edit=edit):
                self.assertFalse(verify_dispute_packet(packet))
        self.assertFalse(verify_dispute_packet(original, expected_head="f" * 64))

    def test_contract_packets_do_not_export_other_clients_evidence(self):
        self.deliver()
        self.create_contract("c2", client_id="private-client", terms="private contractual terms")
        self.ledger.add_milestone("c2", "secret", "Private work", 10_000, "Private criteria")
        self.ledger.record_delivery("c2", "secret", self.delivery_dir)
        other = self.ledger.issue_acceptance_token("c2", "secret")
        packet = self.ledger.generate_dispute_packet("c1")
        encoded = json.dumps(packet)
        self.assertNotIn("private-client", encoded)
        self.assertNotIn("private contractual terms", encoded)
        self.assertNotIn(hashlib.sha256(other["token"].encode()).hexdigest(), encoded)
        self.assertTrue(all(event["contract_id"] == "c1" for event in packet["events"]))
        self.assertTrue(verify_dispute_packet(packet))

    def test_packets_report_missing_milestones_deliveries_and_acceptances(self):
        self.create_contract("c2")
        empty = self.ledger.generate_dispute_packet("c2")
        self.assertEqual(set(empty["warnings"]),
                         {"escrow_not_verified", "no_milestones", "milestone_allocation_incomplete"})
        self.ledger.add_milestone("c2", "partial", "Part one", 5000, "Agreed criteria")
        packet = self.ledger.generate_dispute_packet("c2")
        self.assertEqual(set(packet["warnings"]), {"escrow_not_verified", "milestone_allocation_incomplete",
                                                  "undelivered:partial", "unaccepted:partial"})
        self.assertEqual(packet["receipts"], [])
        self.assertTrue(verify_dispute_packet(packet))

    def test_verifiers_return_false_for_malformed_untrusted_documents(self):
        for value in (None, [], {}, "text", {"events": []}, {"events": [None]},
                      {"events": [{"contract_id": "c1"}], "generated_at": float("nan")},
                      {"schema_version": 1, "type": "delivery_receipt", "receipt_sha256": None}):
            with self.subTest(value=value):
                self.assertFalse(verify_dispute_packet(value))
                self.assertFalse(verify_receipt(value))


class CLITests(LedgerCase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--db", str(self.db_path), *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def command(self, action, **arguments):
        path = self.root / "command.json"
        path.write_text(json.dumps({"action": action, "arguments": arguments}), encoding="utf-8")
        code, out, err = self.cli("record", path)
        self.assertEqual((code, err), (0, ""))
        return json.loads(out)

    def test_cli_records_full_workflow_and_exports_receipt_packet_and_audit(self):
        self.command("create_contract", contract_id="cli", client_id="cli-client", developer_id="dev",
                     amount_minor=900, currency="USD", terms="CLI agreement")
        self.command("add_milestone", contract_id="cli", milestone_id="m", title="CLI delivery",
                     amount_minor=900, acceptance_criteria="CLI criteria")
        evidence = self.evidence(contract_id="cli", client_id="cli-client", amount_minor=900,
                                 observed_at=0)
        # The CLI uses the real clock; this explicitly stale snapshot must stay unverified.
        self.assertFalse(self.command("verify_escrow_deposit", contract_id="cli", evidence=evidence)["verified"])
        self.command("record_delivery", contract_id="cli", milestone_id="m", directory=str(self.delivery_dir))
        token = self.command("issue_acceptance_token", contract_id="cli", milestone_id="m")
        self.command("log_client_acceptance", contract_id="cli", milestone_id="m", client_id="cli-client",
                     token=token["token"], delivery_sha256=token["delivery_sha256"])
        for command, extra in (("receipt", ("m",)), ("packet", ()), ("audit", ())):
            code, out, err = self.cli(command, "cli", *extra)
            self.assertEqual((code, err), (0, ""))
            result = json.loads(out)
            if command == "receipt":
                self.assertTrue(verify_receipt(result))
                self.assertEqual(result["status"], "accepted")
            elif command == "packet":
                self.assertTrue(verify_dispute_packet(result))
                self.assertEqual(result["ledger"]["event_count"], 6)
            else:
                self.assertEqual(result["event_count"], 6)

    def test_cli_rejects_unlisted_actions_and_bad_arguments(self):
        path = self.root / "invalid.json"
        cases = [{"action": "close", "arguments": {}}, {"action": "_events", "arguments": {}},
                 {"action": [], "arguments": {}}, {"action": "create_contract", "arguments": []},
                 {"action": "create_contract", "arguments": {}}, [], {}]
        before = self.head()
        for data in cases:
            path.write_text(json.dumps(data), encoding="utf-8")
            code, out, err = self.cli("record", path)
            self.assertEqual((code, out), (2, ""))
            self.assertIn("error", json.loads(err))
        self.assertEqual(self.head(), before)

    def test_cli_invalid_json_missing_file_unknown_contract_and_checkpoint(self):
        invalid = self.root / "invalid.json"
        invalid.write_text("{bad JSON", encoding="utf-8")
        for args in (("record", invalid), ("record", self.root / "missing"),
                     ("packet", "missing"), ("audit", "c1", "--expected-head", "f" * 64)):
            with self.subTest(args=args):
                code, out, err = self.cli(*args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("error", json.loads(err))
                self.assertNotIn("Traceback", err)

    def test_cli_verifies_exported_documents_without_creating_database(self):
        self.deliver()
        packet = self.ledger.generate_dispute_packet("c1")
        unused_db = self.root / "must-not-be-created.sqlite3"
        for command, document, flag, anchor in (
            ("verify-packet", packet, "--expected-head", packet["ledger"]["head_sha256"]),
            ("verify-receipt", packet["receipts"][0], "--expected-sha256", packet["receipts"][0]["receipt_sha256"]),
        ):
            path = self.root / f"{command}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            for expected, code in ((anchor, 0), ("f" * 64, 1)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    result = main(["--db", str(unused_db), command, str(path), flag, expected])
                self.assertEqual(result, code)
                self.assertEqual(json.loads(out.getvalue()), {"valid": code == 0})
        self.assertFalse(unused_db.exists())

    def test_cli_delivery_verification_exit_status(self):
        self.deliver()
        code, out, err = self.cli("verify-delivery", "c1", "m1", self.delivery_dir)
        self.assertEqual((code, json.loads(out), err), (0, {"valid": True}, ""))
        (self.delivery_dir / "extra").write_bytes(b"unexpected")
        code, out, err = self.cli("verify-delivery", "c1", "m1", self.delivery_dir)
        self.assertEqual((code, json.loads(out), err), (1, {"valid": False}, ""))

    def test_standalone_script_help(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("freelance_contract_escrow_audit.py")),
                                 "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verify-packet", result.stdout)
        self.assertIn("record", result.stdout)


if __name__ == "__main__":
    unittest.main()
