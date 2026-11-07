#!/usr/bin/env python3
"""Deterministic billing tests: no network, sleeps, cron changes, or real payments.

Run from the repository root::

    python3 -m unittest discover -s scripts -p 'test_zero_support_reconciliation_engine.py' -v
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from unittest.mock import patch

if __package__:
    from .zero_support_reconciliation_engine import (
        IdempotencyConflict, OrderConflict, PaidOrder, PermanentReconciliationError,
        RetryPolicy, WebhookEvent, ZeroSupportReconciliationEngine, main,
    )
else:
    from zero_support_reconciliation_engine import (
        IdempotencyConflict, OrderConflict, PaidOrder, PermanentReconciliationError,
        RetryPolicy, WebhookEvent, ZeroSupportReconciliationEngine, main,
    )


class Clock:
    def __init__(self, now: float = 36_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RetryPolicyTests(unittest.TestCase):
    def test_exponential_backoff_and_cap(self):
        policy = RetryPolicy(base_delay=5, max_delay=25)
        self.assertEqual([policy.delay(n) for n in range(1, 7)], [5, 10, 20, 25, 25, 25])
        self.assertEqual(policy.delay(10_000), 25)

    def test_extreme_finite_delays_do_not_overflow(self):
        policy = RetryPolicy(base_delay=1e-300, max_delay=1e300)
        self.assertGreater(policy.delay(1500), 0)
        self.assertEqual(policy.delay(10_000), 1e300)

    def test_invalid_policy_and_attempts_are_rejected(self):
        for kwargs in ({"base_delay": 0}, {"base_delay": -1}, {"max_delay": 1},
                       {"base_delay": float("nan")}, {"max_delay": float("inf")},
                       {"base_delay": True}, {"max_attempts": 0}, {"max_attempts": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RetryPolicy(**kwargs)
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RetryPolicy().delay(value)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.engine = ZeroSupportReconciliationEngine(
            clock=self.clock, retry_policy=RetryPolicy(10, 25, 4), compensation_grace_seconds=60,
        )
        self.addCleanup(self.engine.close)

    def event(self, event_id="evt-1", order_id="ord-1", customer_id="customer-1", quota_units=100):
        return WebhookEvent(event_id, order_id, customer_id, quota_units, self.clock() - 120)

    def paid(self, order_id="ord-1", customer_id="customer-1", quota_units=100, age=120):
        return self.engine.register_order(order_id, customer_id, quota_units, paid_at=self.clock() - age)

    def test_successful_webhook_credits_order_and_customer(self):
        result = self.engine.ingest_webhook(self.event())
        self.assertEqual((result.status, result.attempts, result.duplicate), ("processed", 1, False))
        self.assertIsNone(result.next_attempt_at)
        self.assertEqual(self.engine.get_order("ord-1").status, "fulfilled")
        self.assertEqual(self.engine.quota_balance("customer-1"), 100)
        self.assertEqual(self.engine.ledger_entries()[0].reason, "webhook")

    def test_identical_webhook_is_idempotent(self):
        event = self.event()
        self.engine.ingest_webhook(event)
        result = self.engine.ingest_webhook(dict(reversed(list(asdict(event).items()))))
        self.assertTrue(result.duplicate)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(self.engine.ledger_entries()), 1)
        self.assertEqual(self.engine.quota_balance("customer-1"), 100)

    def test_missing_optional_type_normalizes_to_same_event(self):
        data = asdict(self.event())
        data.pop("event_type")
        self.engine.ingest_webhook(data)
        self.assertTrue(self.engine.ingest_webhook(self.event()).duplicate)

    def test_reused_event_id_with_different_content_is_rejected(self):
        self.engine.ingest_webhook(self.event())
        for event in (replace(self.event(), quota_units=200), replace(self.event(), customer_id="other"),
                      replace(self.event(), order_id="other"), replace(self.event(), paid_at=0)):
            with self.subTest(event=event), self.assertRaises(IdempotencyConflict):
                self.engine.ingest_webhook(event)
        self.assertEqual(self.engine.quota_balance("customer-1"), 100)
        self.assertEqual(self.engine.get_event("evt-1").attempts, 1)

    def test_different_events_for_same_order_do_not_duplicate_quota(self):
        self.engine.ingest_webhook(self.event())
        self.engine.ingest_webhook(self.event("evt-2"))
        self.assertEqual(len(self.engine.ledger_entries()), 1)
        self.assertEqual(self.engine.quota_balance("customer-1"), 100)
        self.assertEqual(self.engine.get_event("evt-2").status, "processed")

    def test_order_conflict_rolls_back_event_insertion(self):
        self.paid()
        for event in (self.event(customer_id="other"), self.event(quota_units=101)):
            with self.subTest(event=event), self.assertRaises(OrderConflict):
                self.engine.ingest_webhook(event)
        with self.assertRaises(KeyError):
            self.engine.get_event("evt-1")
        self.assertEqual(self.engine.quota_balance("customer-1"), 0)

    def test_invalid_webhooks_are_rejected_without_side_effects(self):
        base = asdict(self.event())
        invalid = [None, [], {}, {**base, "extra": 1}]
        for key, values in {
            "event_id": ("", "  ", 3), "order_id": (None, ""), "customer_id": ("", []),
            "quota_units": (0, -1, True, 1.5, "100", 2**63),
            "paid_at": (-1, True, "0", float("inf"), float("nan"), 10**1000),
            "event_type": ("payment.failed", None),
        }.items():
            invalid.extend({**base, key: value} for value in values)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.engine.ingest_webhook(data)
        self.assertEqual(self.engine.retry_due(), [])
        self.assertEqual(self.engine.ledger_entries(), [])

    def test_enqueue_only_commits_payment_before_fulfillment(self):
        result = self.engine.ingest_webhook(self.event(), process_immediately=False)
        self.assertEqual((result.status, result.attempts), ("pending", 0))
        self.assertEqual(self.engine.get_order("ord-1").status, "paid")
        self.assertEqual(self.engine.quota_balance("customer-1"), 0)
        self.assertEqual(self.engine.retry_due()[0].status, "processed")

    def test_transient_failure_retries_only_when_due_then_recovers(self):
        with patch.object(self.engine, "_credit_missing", side_effect=RuntimeError("quota unavailable")):
            failed = self.engine.ingest_webhook(self.event())
        self.assertEqual((failed.status, failed.attempts), ("retry", 1))
        self.assertIn("quota unavailable", failed.last_error)
        self.assertEqual(failed.next_attempt_at, self.clock() + 10)
        self.clock.advance(9.99)
        self.assertEqual(self.engine.retry_due(), [])
        self.clock.advance(0.01)
        result = self.engine.retry_due()[0]
        self.assertEqual((result.status, result.attempts), ("processed", 2))
        self.assertIsNone(result.last_error)
        self.assertIsNone(result.next_attempt_at)
        self.assertEqual(self.engine.quota_balance("customer-1"), 100)

    def test_retry_backoff_caps_and_exhaustion_creates_dead_letter(self):
        with patch.object(self.engine, "_credit_missing", side_effect=RuntimeError("down")):
            first = self.engine.ingest_webhook(self.event())
            self.assertEqual(first.next_attempt_at, self.clock() + 10)
            for wait, expected_delay in ((10, 20), (20, 25)):
                self.clock.advance(wait)
                result = self.engine.retry_due()[0]
                self.assertEqual(result.next_attempt_at, self.clock() + expected_delay)
            self.clock.advance(25)
            dead = self.engine.retry_due()[0]
        self.assertEqual((dead.status, dead.attempts), ("dead", 4))
        self.assertIsNone(dead.next_attempt_at)
        self.assertEqual(self.engine.dead_letters(), [dead])
        self.clock.advance(100_000)
        self.assertEqual(self.engine.retry_due(), [])
        self.assertEqual(self.engine.quota_balance("customer-1"), 0)

    def test_permanent_failure_does_not_retry(self):
        with patch.object(self.engine, "_credit_missing", side_effect=PermanentReconciliationError("blocked")):
            result = self.engine.ingest_webhook(self.event())
        self.assertEqual((result.status, result.attempts), ("dead", 1))
        self.assertIsNone(result.next_attempt_at)
        self.assertEqual(self.engine.retry_due(), [])

    def test_duplicate_does_not_bypass_backoff_or_reset_attempts(self):
        event = self.event()
        with patch.object(self.engine, "_credit_missing", side_effect=RuntimeError("down")):
            original = self.engine.ingest_webhook(event)
        with patch.object(self.engine, "_credit_missing") as credit:
            duplicate = self.engine.ingest_webhook(event)
            credit.assert_not_called()
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.next_attempt_at, original.next_attempt_at)
        self.assertEqual(duplicate.attempts, 1)

    def test_duplicate_dead_letter_does_not_reset_retry_budget(self):
        with patch.object(self.engine, "_credit_missing", side_effect=PermanentReconciliationError("blocked")):
            self.engine.ingest_webhook(self.event())
        duplicate = self.engine.ingest_webhook(self.event())
        self.assertEqual((duplicate.status, duplicate.attempts, duplicate.duplicate), ("dead", 1, True))

    def test_retry_batch_limit_preserves_remaining_work(self):
        for n in range(3):
            self.engine.ingest_webhook(self.event(f"evt-{n}", f"ord-{n}"), process_immediately=False)
        self.assertEqual(len(self.engine.retry_due(limit=2)), 2)
        self.assertEqual(self.engine.quota_balance("customer-1"), 200)
        self.assertEqual(len(self.engine.retry_due(limit=2)), 1)
        self.assertEqual(self.engine.quota_balance("customer-1"), 300)
        self.assertEqual(self.engine.retry_due(), [])
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.engine.retry_due(limit)

    def test_failed_processing_rolls_back_partial_quota_write(self):
        credit = self.engine._credit_missing

        def fail_after_write(*args):
            credit(*args)
            raise RuntimeError("failure after insert")

        with patch.object(self.engine, "_credit_missing", side_effect=fail_after_write):
            failed = self.engine.ingest_webhook(self.event())
        self.assertEqual(failed.status, "retry")
        self.assertEqual(self.engine.ledger_entries(), [])
        self.assertEqual(self.engine.get_order("ord-1").status, "paid")
        self.clock.advance(10)
        self.engine.retry_due()
        self.assertEqual(self.engine.quota_balance("customer-1"), 100)

    def test_unpaid_order_is_never_compensated(self):
        pending = self.engine.register_order("ord-1", "customer-1", 100)
        self.assertEqual(pending.status, "pending")
        self.assertEqual(self.engine.run_hourly_audit().compensated_orders, 0)
        with self.assertRaises(ValueError):
            self.engine.record_fulfillment("ord-1", 1, "delivery-1")
        self.assertEqual(self.engine.quota_balance("customer-1"), 0)

    def test_registration_promotes_pending_and_preserves_first_payment_time(self):
        self.engine.register_order("ord-1", "customer-1", 100)
        paid = self.paid()
        repeated = self.engine.register_order("ord-1", "customer-1", 100, paid_at=self.clock())
        self.assertEqual(repeated.paid_at, paid.paid_at)
        pending_duplicate = self.engine.register_order("ord-1", "customer-1", 100)
        self.assertEqual(pending_duplicate.status, "paid")

    def test_partial_fulfillment_has_idempotent_delivery_keys(self):
        self.paid()
        self.assertTrue(self.engine.record_fulfillment("ord-1", 30, "delivery-1"))
        self.assertFalse(self.engine.record_fulfillment("ord-1", 30, "delivery-1"))
        with self.assertRaises(IdempotencyConflict):
            self.engine.record_fulfillment("ord-1", 31, "delivery-1")
        self.assertEqual(self.engine.get_order("ord-1").outstanding_units, 70)

    def test_fulfillment_cannot_exceed_purchased_quota_or_reuse_key_across_orders(self):
        self.paid()
        self.paid("ord-2")
        self.engine.record_fulfillment("ord-1", 100, "delivery-1")
        with self.assertRaises(IdempotencyConflict):
            self.engine.record_fulfillment("ord-2", 100, "delivery-1")
        for units in (1, 101):
            with self.subTest(units=units), self.assertRaises(ValueError):
                self.engine.record_fulfillment("ord-1", units, "new-key")
        self.assertEqual(self.engine.quota_balance("customer-1"), 100)

    def test_customer_balances_are_isolated_and_identifiers_are_sql_safe(self):
        self.engine.ingest_webhook(self.event(order_id="ord'; DROP TABLE orders;--", customer_id="O'Reilly"))
        self.engine.ingest_webhook(self.event("evt-2", "ord-2", "other", 250))
        self.assertEqual(self.engine.quota_balance("O'Reilly"), 100)
        self.assertEqual(self.engine.quota_balance("other"), 250)
        self.assertEqual(self.engine.quota_balance("unknown"), 0)
        self.assertEqual(self.engine.get_order("ord-2").status, "fulfilled")

    def test_unknown_order_and_event_are_explicit_errors(self):
        with self.assertRaises(KeyError):
            self.engine.get_order("missing")
        with self.assertRaises(KeyError):
            self.engine.get_event("missing")

    def test_audit_compensates_only_paid_overdue_orders_at_grace_boundary(self):
        self.paid("overdue", quota_units=10, age=61)
        self.paid("boundary", quota_units=20, age=60)
        self.paid("recent", quota_units=30, age=59)
        self.paid("future", quota_units=40, age=-1)
        self.engine.register_order("unpaid", "customer-1", 50)
        result = self.engine.run_hourly_audit()
        self.assertEqual((result.scanned_orders, result.compensated_orders, result.quota_units), (4, 2, 30))
        self.assertEqual(self.engine.get_order("recent").credited_units, 0)
        self.assertEqual(self.engine.get_order("future").credited_units, 0)
        self.assertEqual(self.engine.get_order("unpaid").credited_units, 0)

    def test_snapshot_recovers_missed_webhook_and_accepts_dataclass_and_mapping(self):
        snapshot = [PaidOrder("missed-1", "alice", 25, self.clock() - 120),
                    {"order_id": "missed-2", "customer_id": "bob", "quota_units": 75,
                     "paid_at": self.clock() - 120}]
        result = self.engine.run_hourly_audit(snapshot)
        self.assertEqual((result.compensated_orders, result.quota_units), (2, 100))
        self.assertEqual(self.engine.quota_balance("alice"), 25)
        self.assertEqual(self.engine.quota_balance("bob"), 75)
        self.assertEqual(self.engine.ledger_entries()[0].reason, "compensation")

    def test_audit_compensates_only_missing_portion(self):
        self.paid()
        self.engine.record_fulfillment("ord-1", 35, "delivery-1")
        self.engine.record_fulfillment("ord-1", 15, "delivery-2")
        result = self.engine.run_hourly_audit()
        self.assertEqual(result.quota_units, 50)
        self.assertEqual(self.engine.quota_balance("customer-1"), 100)
        compensation = [entry for entry in self.engine.ledger_entries("ord-1")
                        if entry.reason == "compensation"]
        self.assertEqual([entry.quota_units for entry in compensation], [50])
        self.assertEqual(self.engine.get_order("ord-1").status, "fulfilled")

    def test_audit_heals_retry_and_dead_letters_and_settles_queue(self):
        with patch.object(self.engine, "_credit_missing", side_effect=RuntimeError("down")):
            self.engine.ingest_webhook(self.event())
        with patch.object(self.engine, "_credit_missing", side_effect=PermanentReconciliationError("blocked")):
            self.engine.ingest_webhook(self.event("evt-2", "ord-2"))
        result = self.engine.run_hourly_audit()
        self.assertEqual((result.compensated_orders, result.quota_units), (2, 200))
        self.assertEqual(self.engine.dead_letters(), [])
        for event_id in ("evt-1", "evt-2"):
            event = self.engine.get_event(event_id)
            self.assertEqual(event.status, "reconciled")
            self.assertIsNone(event.next_attempt_at)
            self.assertIsNone(event.last_error)
            self.assertEqual(event.attempts, 1)
        self.clock.advance(30)
        self.assertEqual(self.engine.retry_due(), [])
        self.assertEqual(self.engine.quota_balance("customer-1"), 200)

    def test_audit_settles_pending_delivery_for_previously_fulfilled_order(self):
        self.engine.ingest_webhook(self.event(), process_immediately=False)
        self.engine.record_fulfillment("ord-1", 100, "delivery-1")
        self.assertEqual(self.engine.run_hourly_audit().compensated_orders, 0)
        self.assertEqual(self.engine.get_event("evt-1").status, "reconciled")
        self.assertEqual(self.engine.retry_due(), [])

    def test_late_webhook_after_compensation_does_not_double_credit(self):
        self.paid()
        self.engine.run_hourly_audit()
        result = self.engine.ingest_webhook(self.event())
        self.assertEqual(result.status, "processed")
        self.assertEqual(self.engine.quota_balance("customer-1"), 100)
        self.assertEqual(len(self.engine.ledger_entries()), 1)

    def test_hourly_audit_is_idempotent_and_runs_again_at_next_utc_hour(self):
        self.paid()
        first = self.engine.run_hourly_audit()
        self.paid("ord-2")
        self.clock.advance(3599)
        second = self.engine.run_hourly_audit()
        self.assertTrue(second.skipped)
        self.assertEqual(second.quota_units, first.quota_units)
        self.assertEqual(self.engine.quota_balance("customer-1"), 100)
        self.clock.advance(1)
        third = self.engine.run_hourly_audit()
        self.assertFalse(third.skipped)
        self.assertEqual(third.compensated_orders, 1)
        self.assertEqual(self.engine.quota_balance("customer-1"), 200)
        self.clock.advance(3600)
        self.assertEqual(self.engine.run_hourly_audit().quota_units, 0)

    def test_empty_snapshot_never_cancels_known_paid_orders(self):
        self.paid()
        result = self.engine.run_hourly_audit([])
        self.assertEqual(result.compensated_orders, 1)
        self.assertEqual(self.engine.get_order("ord-1").status, "fulfilled")

    def test_invalid_snapshot_rolls_back_all_imports_and_allows_same_hour_retry(self):
        valid = PaidOrder("good", "alice", 10, self.clock() - 120)
        with self.assertRaises(ValueError):
            self.engine.run_hourly_audit([valid, {"order_id": "bad"}])
        with self.assertRaises(KeyError):
            self.engine.get_order("good")
        result = self.engine.run_hourly_audit([valid])
        self.assertFalse(result.skipped)
        self.assertEqual(result.quota_units, 10)

    def test_snapshot_conflict_does_not_credit_wrong_customer(self):
        self.paid()
        with self.assertRaises(OrderConflict):
            self.engine.run_hourly_audit([PaidOrder("ord-1", "attacker", 100, self.clock() - 120)])
        self.assertEqual(self.engine.quota_balance("attacker"), 0)
        self.assertEqual(self.engine.quota_balance("customer-1"), 0)
        self.assertFalse(self.engine.run_hourly_audit().skipped)

    def test_audit_failure_isolated_and_partial_grant_rolled_back(self):
        self.paid("bad")
        self.paid("good")
        credit = self.engine._credit_missing

        def fail_one(order_id, reason, now):
            amount = credit(order_id, reason, now)
            if order_id == "bad":
                raise RuntimeError("write interrupted")
            return amount

        with patch.object(self.engine, "_credit_missing", side_effect=fail_one):
            report = self.engine.run_hourly_audit()
        self.assertEqual((report.compensated_orders, report.quota_units), (1, 100))
        self.assertEqual(report.failures[0].order_id, "bad")
        self.assertIn("write interrupted", report.failures[0].error)
        self.assertEqual(self.engine.get_order("bad").credited_units, 0)
        self.assertEqual(self.engine.get_order("good").credited_units, 100)
        self.assertEqual(self.engine.run_hourly_audit().failures, report.failures)
        self.clock.advance(3600)
        self.assertEqual(self.engine.run_hourly_audit().compensated_orders, 1)
        self.assertEqual(self.engine.quota_balance("customer-1"), 200)

    def test_invalid_grace_or_clock_is_rejected(self):
        for grace in (-1, float("inf"), float("nan"), True):
            with self.subTest(grace=grace), self.assertRaises(ValueError):
                ZeroSupportReconciliationEngine(compensation_grace_seconds=grace)
        self.clock.now = float("nan")
        with self.assertRaises(ValueError):
            self.engine.run_hourly_audit()


class PersistenceAndConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested" / "billing.sqlite3"
        self.clock = Clock()

    def engine(self, **kwargs):
        engine = ZeroSupportReconciliationEngine(self.path, clock=self.clock, **kwargs)
        self.addCleanup(engine.close)
        return engine

    def event(self, event_id="evt-1", order_id="ord-1"):
        return WebhookEvent(event_id, order_id, "alice", 100, self.clock() - 600)

    def test_successful_event_and_idempotency_survive_restart(self):
        engine = self.engine()
        engine.ingest_webhook(self.event())
        engine.close()
        reopened = self.engine()
        result = reopened.ingest_webhook(self.event())
        self.assertTrue(result.duplicate)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(reopened.quota_balance("alice"), 100)

    def test_retry_schedule_and_attempt_count_survive_restart(self):
        engine = self.engine(retry_policy=RetryPolicy(10, 100, 3))
        with patch.object(engine, "_credit_missing", side_effect=RuntimeError("offline")):
            engine.ingest_webhook(self.event())
        engine.close()
        reopened = self.engine(retry_policy=RetryPolicy(10, 100, 3))
        self.assertEqual(reopened.get_event("evt-1").attempts, 1)
        self.assertEqual(reopened.retry_due(), [])
        self.clock.advance(10)
        self.assertEqual(reopened.retry_due()[0].attempts, 2)
        self.assertEqual(reopened.quota_balance("alice"), 100)

    def test_crash_between_ingestion_and_fulfillment_recovers(self):
        engine = self.engine()
        with patch.object(engine, "_process_event", side_effect=SystemExit("simulated crash")):
            with self.assertRaises(SystemExit):
                engine.ingest_webhook(self.event())
        engine.close()
        reopened = self.engine()
        self.assertEqual(reopened.get_event("evt-1").status, "pending")
        self.assertEqual(reopened.retry_due()[0].status, "processed")
        self.assertEqual(reopened.quota_balance("alice"), 100)

    def test_hourly_audit_marker_and_report_survive_restart(self):
        engine = self.engine()
        first = engine.run_hourly_audit([PaidOrder("ord-1", "alice", 100, self.clock() - 600)])
        engine.close()
        reopened = self.engine()
        repeated = reopened.run_hourly_audit()
        self.assertTrue(repeated.skipped)
        self.assertEqual(repeated.quota_units, first.quota_units)
        self.assertEqual(reopened.quota_balance("alice"), 100)

    def test_simultaneous_duplicate_delivery_across_connections_credits_once(self):
        engines = [self.engine() for _ in range(4)]
        barrier = threading.Barrier(4)

        def ingest(engine):
            barrier.wait(timeout=5)
            return engine.ingest_webhook(self.event())

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(ingest, engines))
        self.assertEqual(sum(not result.duplicate for result in results), 1)
        self.assertEqual(engines[0].quota_balance("alice"), 100)
        self.assertEqual(len(engines[0].ledger_entries()), 1)

    def test_concurrent_distinct_events_for_same_order_credit_once(self):
        engines = [self.engine() for _ in range(4)]

        def ingest(pair):
            n, engine = pair
            return engine.ingest_webhook(self.event(f"evt-{n}"))

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(ingest, enumerate(engines)))
        self.assertTrue(all(result.status == "processed" for result in results))
        self.assertEqual(engines[0].quota_balance("alice"), 100)
        self.assertEqual(len(engines[0].ledger_entries()), 1)

    def test_concurrent_retry_workers_cannot_process_same_entry_twice(self):
        engines = [self.engine() for _ in range(4)]
        engines[0].ingest_webhook(self.event(), process_immediately=False)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda engine: engine.retry_due(), engines))
        self.assertEqual(sum(len(result) for result in results), 1)
        self.assertEqual(engines[0].get_event("evt-1").attempts, 1)
        self.assertEqual(engines[0].quota_balance("alice"), 100)

    def test_overlapping_hourly_audits_have_one_winner(self):
        engines = [self.engine() for _ in range(4)]
        engines[0].register_order("ord-1", "alice", 100, paid_at=self.clock() - 600)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda engine: engine.run_hourly_audit(), engines))
        self.assertEqual(sum(not report.skipped for report in results), 1)
        self.assertEqual(engines[0].quota_balance("alice"), 100)
        self.assertEqual(len(engines[0].ledger_entries()), 1)

    def test_webhook_racing_audit_never_double_credits(self):
        webhook_engine, audit_engine = self.engine(), self.engine()
        event = self.event()
        snapshot = [PaidOrder(event.order_id, event.customer_id, event.quota_units, event.paid_at)]
        barrier = threading.Barrier(2)

        def ingest():
            barrier.wait(timeout=5)
            return webhook_engine.ingest_webhook(event)

        def audit():
            barrier.wait(timeout=5)
            return audit_engine.run_hourly_audit(snapshot)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(ingest), pool.submit(audit)
            first.result(timeout=10)
            second.result(timeout=10)
        self.assertEqual(webhook_engine.quota_balance("alice"), 100)
        self.assertEqual(len(webhook_engine.ledger_entries()), 1)
        self.assertIn(webhook_engine.get_event("evt-1").status, ("processed", "reconciled"))


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.db = self.path / "billing.sqlite3"

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--db", str(self.db), *args]), 0)
        return json.loads(output.getvalue())

    def test_cli_ingest_retry_audit_and_duplicate_round_trip(self):
        event = WebhookEvent("evt-1", "ord-1", "alice", 100, 0)
        event_file = self.path / "event.json"
        event_file.write_text(json.dumps(asdict(event)), encoding="utf-8")
        self.assertEqual(self.invoke("ingest", str(event_file), "--enqueue-only")["status"], "pending")
        self.assertEqual(self.invoke("retry", "--limit", "1")[0]["status"], "processed")
        self.assertTrue(self.invoke("ingest", str(event_file))["duplicate"])
        snapshot_file = self.path / "paid-orders.json"
        snapshot_file.write_text(json.dumps([asdict(PaidOrder("missed", "alice", 50, 0))]), encoding="utf-8")
        report = self.invoke("audit", "--orders", str(snapshot_file))
        self.assertEqual((report["compensated_orders"], report["quota_units"]), (1, 50))
        self.assertTrue(self.invoke("audit")["skipped"])
        with ZeroSupportReconciliationEngine(self.db) as engine:
            self.assertEqual(engine.quota_balance("alice"), 150)

    def test_cli_rejects_malformed_json_and_non_array_snapshot(self):
        for text in ("{broken", "{}", "null"):
            with self.subTest(text=text):
                snapshot_file = self.path / "bad.json"
                snapshot_file.write_text(text, encoding="utf-8")
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    self.invoke("audit", "--orders", str(snapshot_file))
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
