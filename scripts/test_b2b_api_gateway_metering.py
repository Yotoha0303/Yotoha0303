#!/usr/bin/env python3
"""Offline tests: python -m unittest discover -s scripts -p 'test_b2b_api_gateway_metering.py' -v."""

import dataclasses
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from unittest.mock import patch

from b2b_api_gateway_metering import (
    MAX_TOKENS,
    AuthenticationError,
    B2BAPIGateway,
    IdempotencyConflict,
    InMemoryStore,
    InsufficientBalance,
    RateLimitExceeded,
    SQLiteStore,
    SlidingWindowRateLimiter,
    generate_api_key,
    hash_api_key,
)


class MockClock:
    def __init__(self, start=1000.0):
        self.current = start

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current += seconds


def concurrently(count, operation):
    """Start contenders together and propagate every unexpected worker failure."""
    barrier = threading.Barrier(count)

    def run(index):
        barrier.wait(timeout=10)
        return operation(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run, range(count)))


class KeyHelperTests(unittest.TestCase):
    def test_sha256_known_vector_and_exact_secret_bytes(self):
        self.assertEqual(hash_api_key("abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertNotEqual(hash_api_key("abc"), hash_api_key(" abc "))
        self.assertNotEqual(hash_api_key("abc"), hash_api_key("ABC"))
        self.assertEqual(len(hash_api_key("密钥")), 64)

    def test_hash_rejects_missing_nontext_and_blank_secrets(self):
        for secret in (None, "", " \t", b"secret", 12, False):
            with self.subTest(secret=secret), self.assertRaises(ValueError):
                hash_api_key(secret)

    def test_generated_keys_are_unique_and_high_entropy_length(self):
        keys = {generate_api_key() for _ in range(100)}
        self.assertEqual(len(keys), 100)
        self.assertTrue(all(key.startswith("b2b_") and len(key) >= 47 for key in keys))


class BackendContract:
    """The identical behavioral contract runs against both persistence backends."""

    secret = "b2b_test_secret_never_persist_this_literal"

    def setUp(self):
        self.clock = MockClock()
        self.store = self.make_store()
        self.gateway = B2BAPIGateway(self.store, self.clock)
        self.addCleanup(self.gateway.close)

    def register(self, **options):
        defaults = dict(key_id="customer-a", initial_balance=1000,
                        rpm_limit=100, tpm_limit=10000)
        defaults.update(options)
        return self.gateway.register_key(self.secret, **defaults)

    def debit(self, tokens, request_id="request-1"):
        return self.gateway.debit(self.secret, tokens, request_id=request_id)

    def test_registration_authentication_and_digest_only_storage(self):
        key = self.register()
        self.assertEqual(key, self.gateway.authenticate(self.secret))
        self.assertEqual(key.key_hash, hash_api_key(self.secret))
        self.assertNotIn(self.secret, repr(key))
        if isinstance(self.store, InMemoryStore):
            persisted = repr((self.store._keys, self.store._hashes, self.store._ledger))
        else:
            persisted = "\n".join(self.store._connection.iterdump())
        self.assertNotIn(self.secret, persisted)
        self.assertIn(key.key_hash, persisted)

    def test_registration_defaults_and_generated_identifiers(self):
        key = self.gateway.register_key(self.secret)
        self.assertTrue(key.key_id)
        self.assertEqual((key.rpm_limit, key.tpm_limit, key.balance), (60, 60000, 0))
        other = self.gateway.register_key("different-secret")
        self.assertNotEqual(key.key_id, other.key_id)
        self.assertEqual(self.gateway.usage(key.key_id).total_credited, 0)

    def test_duplicate_secret_or_key_id_does_not_overwrite_account(self):
        key = self.register()
        for secret, key_id in ((self.secret, "other-id"), ("other-secret", key.key_id)):
            with self.subTest(key_id=key_id), self.assertRaises(ValueError):
                self.gateway.register_key(secret, key_id=key_id, initial_balance=9999)
        self.assertEqual(self.gateway.authenticate(self.secret), key)
        self.assertEqual(len(self.gateway.ledger(key.key_id)), 1)
        with self.assertRaises(AuthenticationError):
            self.gateway.authenticate("other-secret")

    def test_invalid_or_revoked_authentication_has_no_side_effects(self):
        self.register()
        before = self.gateway.usage("customer-a")
        for secret in (None, "", " ", b"secret", 10, "unknown", "\ud800"):
            with self.subTest(secret=repr(secret)), self.assertRaises(AuthenticationError):
                self.gateway.debit(secret, 1, request_id="invalid")
        self.assertEqual(self.gateway.usage("customer-a"), before)
        self.debit(1)
        self.gateway.revoke_key("customer-a")
        for request_id in ("request-1", "new-request"):
            with self.subTest(request_id=request_id), self.assertRaises(AuthenticationError):
                self.debit(1, request_id)
        with self.assertRaises(AuthenticationError):
            self.gateway.authenticate(self.secret)

    def test_debit_credit_and_ledger_reconcile_each_running_balance(self):
        self.register(initial_balance=100)
        debit = self.debit(35)
        credit = self.gateway.credit("customer-a", 20, operation_id="payment-1")
        self.assertEqual((debit.delta, debit.balance_after), (-35, 65))
        self.assertEqual((credit.delta, credit.balance_after), (20, 85))
        ledger = self.gateway.ledger("customer-a")
        self.assertEqual([entry.kind for entry in ledger], ["opening", "debit", "credit"])
        balance = 0
        for entry in ledger:
            balance += entry.delta
            self.assertEqual(entry.balance_after, balance)
        usage = self.gateway.usage("customer-a")
        self.assertEqual((usage.total_credited, usage.total_debited, usage.balance), (120, 35, 85))
        self.assertEqual(usage.balance, usage.total_credited - usage.total_debited)

    def test_exact_balance_debit_and_zero_token_requests(self):
        self.register(initial_balance=5, rpm_limit=2)
        self.debit(5, "all-funds")
        self.debit(0, "free-request")
        usage = self.gateway.usage("customer-a")
        self.assertEqual((usage.balance, usage.requests_in_window, usage.tokens_in_window), (0, 2, 5))
        with self.assertRaises(RateLimitExceeded):
            self.debit(0, "another-free-request")

    def test_overdraft_changes_neither_balance_ledger_nor_rate_capacity(self):
        self.register(initial_balance=5, rpm_limit=1)
        before = self.gateway.usage("customer-a")
        with self.assertRaises(InsufficientBalance):
            self.debit(6)
        self.assertEqual(self.gateway.usage("customer-a"), before)
        self.assertEqual(len(self.gateway.ledger("customer-a")), 1)
        self.gateway.credit("customer-a", 1, operation_id="topup")
        self.assertEqual(self.debit(6).balance_after, 0)

    def test_rpm_window_fractional_retry_and_exact_expiry(self):
        self.register(rpm_limit=2)
        self.debit(1, "first")
        self.clock.advance(10.25)
        self.debit(1, "second")
        with self.assertRaises(RateLimitExceeded) as error:
            self.debit(1, "third")
        self.assertEqual(error.exception.limits, ("rpm",))
        self.assertAlmostEqual(error.exception.retry_after, 49.75)
        self.clock.advance(49.749)
        with self.assertRaises(RateLimitExceeded):
            self.debit(1, "third")
        self.clock.current = 1060.0
        self.debit(1, "third")
        self.assertEqual(self.gateway.usage("customer-a").requests_in_window, 2)

    def test_window_does_not_reset_at_fixed_minute_boundaries(self):
        self.clock.current = 119.0
        self.register(rpm_limit=1)
        self.debit(1)
        self.clock.current = 120.0
        with self.assertRaises(RateLimitExceeded) as error:
            self.debit(1, "next-minute")
        self.assertEqual(error.exception.retry_after, 59.0)

    def test_tpm_limit_allows_exact_capacity_and_recovers(self):
        self.register(tpm_limit=100)
        self.debit(70, "first")
        self.clock.advance(10)
        self.debit(30, "second")
        with self.assertRaises(RateLimitExceeded) as error:
            self.debit(1, "third")
        self.assertEqual(error.exception.limits, ("tpm",))
        self.assertEqual(error.exception.retry_after, 50)
        self.clock.advance(50)
        self.debit(70, "third")
        self.assertEqual(self.gateway.usage("customer-a").tokens_in_window, 100)

    def test_tpm_retry_waits_for_enough_tokens_not_just_oldest_event(self):
        self.register(tpm_limit=100)
        self.debit(20, "first")
        self.clock.advance(10)
        self.debit(30, "second")
        self.clock.advance(10)
        self.debit(40, "third")
        with self.assertRaises(RateLimitExceeded) as error:
            self.debit(80, "large")
        self.assertEqual(error.exception.retry_after, 60)
        self.clock.advance(50)
        with self.assertRaises(RateLimitExceeded) as error:
            self.debit(80, "large")
        self.assertEqual(error.exception.retry_after, 10)
        self.clock.advance(10)
        self.debit(80, "large")

    def test_combined_limits_report_the_longer_required_wait(self):
        self.register(rpm_limit=2, tpm_limit=100)
        self.debit(20, "first")
        self.clock.advance(10)
        self.debit(80, "second")
        before = self.gateway.usage("customer-a")
        with self.assertRaises(RateLimitExceeded) as error:
            self.debit(30, "third")
        self.assertEqual(error.exception.limits, ("rpm", "tpm"))
        self.assertEqual(error.exception.retry_after, 60)
        self.assertEqual(self.gateway.usage("customer-a"), before)

    def test_request_larger_than_tpm_has_no_finite_retry(self):
        self.register(tpm_limit=10)
        with self.assertRaises(RateLimitExceeded) as error:
            self.debit(11)
        self.assertEqual(error.exception.limits, ("tpm",))
        self.assertIsNone(error.exception.retry_after)
        self.assertEqual(self.gateway.usage("customer-a").balance, 1000)
        self.debit(10)

    def test_debit_retry_returns_original_receipt_without_extra_usage(self):
        self.register(initial_balance=10, rpm_limit=1, tpm_limit=10)
        original = self.debit(10)
        self.assertEqual(self.debit(10), original)
        self.clock.advance(120)
        self.assertEqual(self.debit(10), original)
        usage = self.gateway.usage("customer-a")
        self.assertEqual((usage.balance, usage.total_requests, usage.total_debited), (0, 1, 10))
        self.assertEqual(usage.requests_in_window, 0)
        with self.assertRaises(IdempotencyConflict):
            self.debit(9)

    def test_credit_retry_and_payload_conflict(self):
        self.register(initial_balance=0)
        original = self.gateway.credit("customer-a", 50, operation_id="payment")
        self.clock.advance(10)
        self.assertEqual(self.gateway.credit("customer-a", 50, operation_id="payment"), original)
        with self.assertRaises(IdempotencyConflict):
            self.gateway.credit("customer-a", 51, operation_id="payment")
        self.assertEqual(self.gateway.usage("customer-a").balance, 50)
        self.assertEqual(len(self.gateway.ledger("customer-a")), 2)

    def test_keys_and_operation_types_have_independent_idempotency_scopes(self):
        self.register(rpm_limit=1, tpm_limit=5)
        self.gateway.register_key("second-secret", key_id="customer-b",
                                  initial_balance=20, rpm_limit=1, tpm_limit=5)
        self.debit(5, "shared-id")
        self.gateway.credit("customer-a", 3, operation_id="shared-id")
        self.gateway.debit("second-secret", 5, request_id="shared-id")
        self.assertEqual(self.gateway.usage("customer-a").balance, 998)
        self.assertEqual(self.gateway.usage("customer-b").balance, 15)
        with self.assertRaises(RateLimitExceeded):
            self.debit(0, "full")

    def test_credit_does_not_erase_tpm_or_rpm_history(self):
        self.register(initial_balance=10, rpm_limit=1, tpm_limit=10)
        self.debit(10)
        self.gateway.credit("customer-a", 10, operation_id="refund")
        usage = self.gateway.usage("customer-a")
        self.assertEqual((usage.balance, usage.total_debited, usage.tokens_in_window), (10, 10, 10))
        with self.assertRaises(RateLimitExceeded) as error:
            self.debit(1, "try-again")
        self.assertEqual(error.exception.limits, ("rpm", "tpm"))

    def test_burndown_snapshot_and_lifetime_ledger_survive_window_expiry(self):
        self.register()
        self.debit(120)
        usage = self.gateway.usage("customer-a")
        self.assertEqual((usage.burn_rate_tps, usage.seconds_to_exhaustion), (2, 440))
        self.assertEqual((usage.rpm_remaining, usage.tpm_remaining), (99, 9880))
        self.clock.advance(60)
        usage = self.gateway.usage("customer-a")
        self.assertEqual((usage.burn_rate_tps, usage.requests_in_window), (0, 0))
        self.assertIsNone(usage.seconds_to_exhaustion)
        self.assertEqual((usage.total_debited, usage.total_requests), (120, 1))
        self.assertEqual(len(self.gateway.ledger("customer-a")), 2)

    def test_returned_records_and_ledger_cannot_mutate_persisted_state(self):
        key = self.register()
        receipt = self.debit(1)
        for record, field in ((key, "balance"), (receipt, "tokens"),
                              (self.gateway.usage(key.key_id), "balance")):
            with self.subTest(field=field), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(record, field, 999999)
        entries = self.gateway.ledger(key.key_id)
        entries.clear()
        self.assertEqual(len(self.gateway.ledger(key.key_id)), 2)
        self.assertEqual(self.gateway.authenticate(self.secret).balance, 999)

    def test_registration_validates_limits_balances_ids_and_secrets(self):
        options = [{"initial_balance": value} for value in (-1, True, 1.5, MAX_TOKENS + 1)]
        options += [{field: value} for field in ("rpm_limit", "tpm_limit")
                    for value in (0, -1, True, 1.5, "1", MAX_TOKENS + 1)]
        options += [{"key_id": value} for value in ("", " ", 12)]
        for option in options:
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.register(**option)
        for secret in (None, "", " ", b"secret"):
            with self.subTest(secret=secret), self.assertRaises(ValueError):
                self.gateway.register_key(secret)
        self.register()  # Invalid registrations did not consume the ID or digest.

    def test_debit_and_credit_reject_invalid_amounts_and_operation_ids(self):
        self.register()
        before = self.gateway.usage("customer-a")
        for tokens in (-1, True, False, 1.25, "1", None, MAX_TOKENS + 1):
            for operation in (lambda: self.debit(tokens), lambda: self.gateway.credit(
                    "customer-a", tokens, operation_id="payment")):
                with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                    operation()
        with self.assertRaises(ValueError):
            self.gateway.credit("customer-a", 0, operation_id="payment")
        for identity in ("", " ", None, 123):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.debit(1, identity)
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.gateway.credit("customer-a", 1, operation_id=identity)
        self.assertEqual(self.gateway.usage("customer-a"), before)

    def test_unknown_administrative_key_ids_raise_without_creating_accounts(self):
        for operation in (lambda: self.gateway.credit("missing", 1, operation_id="p"),
                          lambda: self.gateway.ledger("missing"),
                          lambda: self.gateway.usage("missing"),
                          lambda: self.gateway.revoke_key("missing")):
            with self.assertRaises(KeyError):
                operation()

    def test_sql_metacharacters_in_ids_and_secrets_are_literal(self):
        secret = "secret' OR 1=1; --"
        key_id = "customer'); DROP TABLE api_keys; --"
        self.gateway.register_key(secret, key_id=key_id, initial_balance=10)
        self.gateway.debit(secret, 1, request_id="request'); DELETE FROM usage_ledger; --")
        self.gateway.credit(key_id, 1, operation_id="payment' OR 1=1")
        self.assertEqual(self.gateway.usage(key_id).balance, 10)
        self.register()

    def test_integer_overflow_is_atomic_and_lifetime_totals_use_python_integers(self):
        self.register(initial_balance=MAX_TOKENS, tpm_limit=MAX_TOKENS)
        with self.assertRaises(ValueError):
            self.gateway.credit("customer-a", 1, operation_id="overflow")
        self.assertEqual(len(self.gateway.ledger("customer-a")), 1)
        self.debit(MAX_TOKENS)
        self.gateway.credit("customer-a", MAX_TOKENS, operation_id="refill")
        usage = self.gateway.usage("customer-a")
        self.assertEqual(usage.total_credited, 2 * MAX_TOKENS)
        self.assertEqual(usage.balance, MAX_TOKENS)

    def test_backward_clock_does_not_reset_or_extend_persisted_request_time(self):
        self.register(rpm_limit=1)
        self.clock.advance(10)
        self.debit(1)
        self.clock.advance(-100)
        with self.assertRaises(RateLimitExceeded) as error:
            self.debit(1, "second")
        self.assertEqual(error.exception.retry_after, 60)
        self.clock.current = 1070
        self.debit(1, "second")
        self.assertEqual(self.gateway.usage("customer-a").total_requests, 2)

    def test_invalid_clock_does_not_mutate_state(self):
        self.register()
        for value in (float("nan"), float("inf"), -1, True, "1000", None):
            self.clock.current = value
            for operation in (lambda: self.debit(1), lambda: self.gateway.credit(
                    "customer-a", 1, operation_id="payment")):
                with self.subTest(clock=value), self.assertRaises(ValueError):
                    operation()
        self.clock.current = 1000
        self.assertEqual(self.gateway.usage("customer-a").balance, 1000)
        self.assertEqual(len(self.gateway.ledger("customer-a")), 1)

    def fail_after_append(self, entry):
        self.real_append(entry)
        raise RuntimeError("simulated storage failure after ledger insertion")

    def test_registration_rolls_back_account_and_opening_ledger_on_storage_failure(self):
        self.real_append = self.store._append_entry
        with patch.object(self.store, "_append_entry", side_effect=self.fail_after_append):
            with self.assertRaisesRegex(RuntimeError, "storage failure"):
                self.register()
        with self.assertRaises(AuthenticationError):
            self.gateway.authenticate(self.secret)
        self.register()
        self.assertEqual(len(self.gateway.ledger("customer-a")), 1)

    def test_debit_rolls_back_balance_ledger_quota_and_idempotency_on_storage_failure(self):
        self.register(rpm_limit=1)
        before = self.gateway.usage("customer-a")
        self.real_append = self.store._append_entry
        with patch.object(self.store, "_append_entry", side_effect=self.fail_after_append):
            with self.assertRaisesRegex(RuntimeError, "storage failure"):
                self.debit(10)
        self.assertEqual(self.gateway.usage("customer-a"), before)
        self.assertEqual(len(self.gateway.ledger("customer-a")), 1)
        self.assertEqual(self.debit(10).balance_after, 990)

    def test_credit_rolls_back_balance_and_receipt_on_storage_failure(self):
        self.register()
        self.real_append = self.store._append_entry
        with patch.object(self.store, "_append_entry", side_effect=self.fail_after_append):
            with self.assertRaisesRegex(RuntimeError, "storage failure"):
                self.gateway.credit("customer-a", 10, operation_id="payment")
        self.assertEqual(self.gateway.usage("customer-a").balance, 1000)
        self.assertEqual(self.gateway.credit("customer-a", 10,
                                             operation_id="payment").balance_after, 1010)

    def test_concurrent_debits_cannot_overdraw(self):
        self.register(initial_balance=5)

        def debit(index):
            try:
                self.debit(1, f"request-{index}")
                return "ok"
            except InsufficientBalance:
                return "empty"

        results = concurrently(12, debit)
        self.assertEqual((results.count("ok"), results.count("empty")), (5, 7))
        self.assertEqual(self.gateway.usage("customer-a").balance, 0)
        self.assertEqual(len(self.gateway.ledger("customer-a")), 6)

    def test_concurrent_admission_cannot_exceed_rpm_or_tpm(self):
        self.register(rpm_limit=3, tpm_limit=100)

        def debit(index):
            try:
                self.debit(1, f"rpm-{index}")
                return True
            except RateLimitExceeded:
                return False

        self.assertEqual(sum(concurrently(12, debit)), 3)
        self.clock.advance(60)
        # A different key lets TPM, rather than RPM, be the limiting dimension.
        self.gateway.register_key("tpm-secret", key_id="tpm", initial_balance=100,
                                  rpm_limit=100, tpm_limit=5)

        def debit_tpm(index):
            try:
                self.gateway.debit("tpm-secret", 2, request_id=f"tpm-{index}")
                return True
            except RateLimitExceeded:
                return False

        self.assertEqual(sum(concurrently(12, debit_tpm)), 2)
        self.assertEqual(self.gateway.usage("tpm").tokens_in_window, 4)

    def test_concurrent_retries_charge_exactly_once(self):
        self.register(rpm_limit=1)
        receipts = concurrently(12, lambda index: self.debit(10))
        self.assertEqual(len({receipt.entry_id for receipt in receipts}), 1)
        usage = self.gateway.usage("customer-a")
        self.assertEqual((usage.balance, usage.total_requests, usage.tokens_in_window), (990, 1, 10))

    def test_concurrent_credits_and_debits_do_not_lose_updates(self):
        self.register(initial_balance=100)

        def post(index):
            if index % 2:
                return self.debit(1, f"debit-{index}")
            return self.gateway.credit("customer-a", 1, operation_id=f"credit-{index}")

        concurrently(12, post)
        receipts = concurrently(12, lambda index: self.gateway.credit(
            "customer-a", 5, operation_id="same-payment"))
        self.assertEqual(len({receipt.entry_id for receipt in receipts}), 1)
        usage = self.gateway.usage("customer-a")
        self.assertEqual((usage.balance, usage.total_credited, usage.total_debited), (105, 111, 6))


class InMemoryGatewayTests(BackendContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class SQLiteGatewayTests(BackendContract, unittest.TestCase):
    def make_store(self):
        directory = tempfile.TemporaryDirectory(prefix="b2b-metering-test-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "metering.sqlite3"
        return SQLiteStore(self.path)

    def test_restart_preserves_auth_account_ledger_rate_limits_and_retries(self):
        self.register(rpm_limit=1, tpm_limit=10)
        original = self.debit(10)
        self.gateway.credit("customer-a", 5, operation_id="payment")
        before = self.gateway.usage("customer-a")
        self.gateway.close()
        self.store = SQLiteStore(self.path)
        self.gateway = B2BAPIGateway(self.store, self.clock)
        self.addCleanup(self.gateway.close)
        self.assertEqual(self.gateway.usage("customer-a"), before)
        self.assertEqual(self.debit(10), original)
        self.assertEqual(self.gateway.credit("customer-a", 5,
                                             operation_id="payment").balance_after, 995)
        with self.assertRaises(RateLimitExceeded):
            self.debit(1, "new-request")
        self.assertEqual(len(self.gateway.ledger("customer-a")), 3)
        self.clock.advance(60)
        self.assertEqual(self.debit(1, "new-request").balance_after, 994)

    def test_revocation_persists_across_connections_and_restarts(self):
        self.register()
        self.gateway.revoke_key("customer-a")
        other = B2BAPIGateway(SQLiteStore(self.path), self.clock)
        self.addCleanup(other.close)
        with self.assertRaises(AuthenticationError):
            other.authenticate(self.secret)
        self.assertEqual(other.usage("customer-a").balance, 1000)

    def other_gateways(self, count=4):
        gateways = [B2BAPIGateway(SQLiteStore(self.path), self.clock) for _ in range(count)]
        for gateway in gateways:
            self.addCleanup(gateway.close)
        return gateways

    def test_independent_connections_cannot_overdraw(self):
        self.register(initial_balance=5)
        gateways = self.other_gateways()

        def debit(index):
            try:
                gateways[index % len(gateways)].debit(self.secret, 1, request_id=str(index))
                return True
            except InsufficientBalance:
                return False

        self.assertEqual(sum(concurrently(16, debit)), 5)
        self.assertEqual(self.gateway.usage("customer-a").balance, 0)
        self.assertEqual(len(self.gateway.ledger("customer-a")), 6)

    def test_independent_connections_share_rate_limits_and_idempotency(self):
        self.register(rpm_limit=2, tpm_limit=10)
        gateways = self.other_gateways()
        receipts = concurrently(16, lambda index: gateways[index % len(gateways)].debit(
            self.secret, 4, request_id="same-request"))
        self.assertEqual(len({receipt.entry_id for receipt in receipts}), 1)

        def debit(index):
            try:
                gateways[index % len(gateways)].debit(self.secret, 4, request_id=str(index))
                return True
            except RateLimitExceeded:
                return False

        self.assertEqual(sum(concurrently(16, debit)), 1)
        usage = self.gateway.usage("customer-a")
        self.assertEqual((usage.balance, usage.requests_in_window, usage.tokens_in_window), (992, 2, 8))

    def test_independent_connections_share_credits_without_lost_updates(self):
        self.register(initial_balance=0)
        gateways = self.other_gateways()
        concurrently(16, lambda index: gateways[index % len(gateways)].credit(
            "customer-a", 1, operation_id=f"payment-{index}"))
        receipts = concurrently(16, lambda index: gateways[index % len(gateways)].credit(
            "customer-a", 10, operation_id="same-payment"))
        self.assertEqual(len({receipt.entry_id for receipt in receipts}), 1)
        self.assertEqual(self.gateway.usage("customer-a").balance, 26)

    def test_context_manager_closes_sqlite_connection_even_on_error(self):
        store = SQLiteStore()
        with self.assertRaisesRegex(RuntimeError, "caller failure"):
            with B2BAPIGateway(store):
                raise RuntimeError("caller failure")
        with self.assertRaises(sqlite3.ProgrammingError):
            store._connection.execute("SELECT 1")


class SlidingWindowPolicyTests(unittest.TestCase):
    def test_unordered_events_and_expired_records(self):
        limiter = SlidingWindowRateLimiter()
        events = [(1050, 40), (900, 10000), (1010, 20), (1030, 30)]
        with self.assertRaises(RateLimitExceeded) as error:
            limiter.check(events, now=1050, tokens=80, rpm_limit=100, tpm_limit=100)
        self.assertEqual(error.exception.retry_after, 60)
        limiter.check(events, now=1110, tokens=100, rpm_limit=1, tpm_limit=100)

    def test_policy_rejects_invalid_counts_and_limits(self):
        limiter = SlidingWindowRateLimiter()
        for options in ({"tokens": -1}, {"tokens": True}, {"rpm_limit": 0},
                        {"tpm_limit": 0}, {"tpm_limit": 1.5}):
            arguments = dict(now=1000, tokens=1, rpm_limit=1, tpm_limit=1)
            arguments.update(options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                limiter.check([], **arguments)


if __name__ == "__main__":
    unittest.main()
