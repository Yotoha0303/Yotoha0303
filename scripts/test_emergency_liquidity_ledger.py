#!/usr/bin/env python3
"""Offline financial arithmetic, persistence, concurrency, audit, and CLI tests.

    python3 -m unittest discover -s scripts -p 'test_emergency_liquidity_ledger.py' -v
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

if __package__:
    from . import emergency_liquidity_ledger as model
else:
    import emergency_liquidity_ledger as model


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 30, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, days=1):
        self.now += timedelta(days=days)


def config(**changes):
    values = {"opening_cash": "3000", "living_ceiling_30_days": "3000",
              "start_date": "2026-01-01"}
    values.update(changes)
    return model.LedgerConfig(**values)


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_exact_six_decimal_normalization(self):
        value = config(opening_cash=Decimal("100.000001"), daily_operating_budget=3)
        self.assertEqual(value.opening_cash, "100.000001")
        self.assertEqual(value.daily_operating_budget, "3.000000")
        self.assertEqual(value.currency, "CNY")
        self.assertEqual((value.red_alert_days, value.warning_days), (7, 14))
        self.assertEqual(model.LedgerConfig(**json.loads(json.dumps(asdict(value)))), value)

    def test_invalid_money_types_ranges_and_excess_precision(self):
        invalid = (True, False, None, [], {}, 1.0, float("nan"), float("inf"),
                   "NaN", "sNaN", "Infinity", "-1", "invalid", "0.0000001",
                   "1.000000000000000000000000000001", Decimal("1e-9999"),
                   Decimal("1e9999"), 10**100)
        for field in ("opening_cash", "living_ceiling_30_days", "daily_operating_budget"):
            for value in invalid:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    config(**{field: value})
        self.assertEqual(config(opening_cash="9223372036854.775807").opening_cash,
                         "9223372036854.775807")
        with self.assertRaises(ValueError):
            config(opening_cash="9223372036854.775808")

    def test_money_precision_does_not_depend_on_decimal_context(self):
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(config(opening_cash="123456.123456").opening_cash,
                             "123456.123456")
            self.assertEqual(config(opening_cash="9223372036854.775807").opening_cash,
                             "9223372036854.775807")
            with self.assertRaises(ValueError):
                config(opening_cash="9223372036854.775808")
            with self.assertRaises(ValueError):
                config(opening_cash="1.000000000000000000000000000001")

    def test_currency_date_and_threshold_validation(self):
        invalid = ({"currency": "usd"}, {"currency": "US"}, {"currency": "CNY/USD"},
                   {"currency": None}, {"start_date": "2026-02-30"},
                   {"start_date": "20260101"}, {"start_date": datetime(2026, 1, 1)},
                   {"red_alert_days": True}, {"red_alert_days": 0},
                   {"red_alert_days": 15, "warning_days": 14},
                   {"warning_days": 31}, {"warning_days": 7.5})
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                config(**values)
        self.assertEqual(config(currency="USDC", start_date=date(2026, 1, 1)).start_date,
                         "2026-01-01")

    def test_new_ledger_requires_config_and_rejects_future_start_or_bad_clock(self):
        with self.assertRaises(ValueError):
            model.EmergencyLiquidityLedger()
        with self.assertRaises(ValueError):
            model.EmergencyLiquidityLedger(config=config(start_date="2026-02-01"), clock=Clock())
        with self.assertRaises(ValueError):
            model.EmergencyLiquidityLedger(config=config(), clock=lambda: datetime(2026, 1, 30))
        with self.assertRaises(ValueError):
            model.EmergencyLiquidityLedger(config={})


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ledger = model.EmergencyLiquidityLedger(config=config(), clock=self.clock)
        self.addCleanup(self.ledger.close)

    def make_ledger(self, **changes):
        ledger = model.EmergencyLiquidityLedger(config=config(**changes), clock=self.clock)
        self.addCleanup(ledger.close)
        return ledger

    def fee(self, entry_id="fee-1", amount="0.000001", **changes):
        values = {"reference": "feegate:" + entry_id}
        values.update(changes)
        return self.ledger.log_feegate_payment(entry_id, amount, **values)

    def expense(self, entry_id="expense-1", amount="100", **changes):
        values = {"note": "Living expense"}
        values.update(changes)
        return self.ledger.log_expense(entry_id, amount, **values)


class CashflowTests(LedgerCase):
    def test_empty_ledger_has_thirty_days_and_no_synthetic_cash_debits(self):
        report = self.ledger.snapshot()
        self.assertEqual(report["balance_sheet"]["closing_cash"], "3000.000000")
        self.assertEqual(report["days_to_zero"], "30.000000")
        self.assertEqual(report["burn"]["effective_daily"], "100.000000")
        self.assertEqual(report["burn"]["observed_daily"], "0.000000")
        self.assertTrue(report["survival_30_days"]["funded"])
        self.assertEqual(report["survival_30_days"]["funding_gap"], "0.000000")
        self.assertEqual(report["status"], "OK")
        self.assertEqual(len(report["daily_ledger"]), 30)
        self.assertEqual({row["closing_cash"] for row in report["daily_ledger"]}, {"3000.000000"})
        self.assertEqual(report["projection_30_days"][-1]["closing_cash"], "0.000000")

    def test_all_income_sources_and_fees_reconcile_with_expenses(self):
        self.ledger.log_contract_deposit("d1", "gig-1", "1000", reference="bank:1", fee="10")
        self.ledger.log_escrow_payout("e1", "gig-1", "m1", "500", reference="escrow:1", fee="5")
        self.fee(amount="0.050001", fee="0.000001")
        self.expense(amount="250")
        self.expense("hosting", "25", category="operating", gig_id="gig-1")
        report = self.ledger.snapshot()
        sheet = report["balance_sheet"]
        self.assertEqual(sheet["gross_receipts"], "1500.050001")
        self.assertEqual(sheet["payment_fees"], "15.000001")
        self.assertEqual(sheet["net_receipts"], "1485.050000")
        self.assertEqual(sheet["total_expenses"], "275.000000")
        self.assertEqual(sheet["closing_cash"], "4210.050000")
        self.assertEqual(sheet["reconciliation_difference"], "0.000000")
        self.assertEqual(report["days_to_zero"], "42.100500")
        self.assertEqual(set(report["income_by_source"]), set(model.INCOME_KINDS))
        self.assertEqual(report["income_by_source"]["escrow_payout"]["net"], "495.000000")

    def test_many_micropayments_preserve_every_millionth(self):
        for index in range(101):
            self.fee(str(index))
        report = self.ledger.snapshot()
        self.assertEqual(report["balance_sheet"]["closing_cash"], "3000.000101")
        self.assertEqual(report["income_by_source"]["feegate"]["gross"], "0.000101")
        self.assertEqual(report["income_by_source"]["feegate"]["count"], 101)

    def test_pending_income_never_funds_cash_or_forecast(self):
        self.ledger.log_contract_deposit("d1", "gig-1", "800", reference="bank:pending", status="pending")
        self.ledger.log_escrow_payout("e1", "gig-1", "m1", "9000", fee="100",
                                     reference="escrow:pending", status="pending")
        self.fee(amount="10", status="pending")
        report = self.ledger.snapshot()
        self.assertEqual(report["pending_net_receipts"], "9710.000000")
        self.assertEqual(report["balance_sheet"]["closing_cash"], "3000.000000")
        self.assertEqual(report["days_to_zero"], "30.000000")
        self.assertEqual(report["projection_30_days"][-1]["closing_cash"], "0.000000")

    def test_settlement_posts_on_settlement_day_and_historical_report_stays_pending(self):
        self.fee(amount="100", fee="1", status="pending", booked_on="2026-01-05")
        row = self.ledger.settle_income("fee-1", settled_on="2026-01-20")
        self.assertEqual(row["status"], "settled")
        self.assertIsNotNone(row["settlement_recorded_at"])
        before = self.ledger.snapshot("2026-01-19")
        self.assertEqual(before["pending_net_receipts"], "99.000000")
        self.assertEqual(before["balance_sheet"]["closing_cash"], "3000.000000")
        self.assertEqual(before["entries"][0]["status"], "pending")
        self.assertIsNone(before["entries"][0]["settled_on"])
        after = self.ledger.snapshot()
        payday = next(row for row in after["daily_ledger"] if row["date"] == "2026-01-20")
        self.assertEqual(payday["net_receipts"], "99.000000")
        self.assertEqual(after["balance_sheet"]["closing_cash"], "3099.000000")
        self.assertEqual(after["pending_net_receipts"], "0.000000")

    def test_observed_gross_burn_increases_runway_denominator_despite_income(self):
        self.expense(amount="6000", category="operating")
        self.fee(amount="9000")
        report = self.ledger.snapshot()
        self.assertEqual(report["burn"]["planned_daily"], "100.000000")
        self.assertEqual(report["burn"]["observed_daily"], "200.000000")
        self.assertEqual(report["burn"]["effective_daily"], "200.000000")
        self.assertEqual(report["balance_sheet"]["closing_cash"], "6000.000000")
        self.assertEqual(report["days_to_zero"], "30.000000")

    def test_operating_budget_and_early_observation_window(self):
        ledger = self.make_ledger(daily_operating_budget="20")
        report = ledger.snapshot("2026-01-01")
        self.assertEqual(report["burn"]["planned_daily"], "120.000000")
        self.assertEqual(report["days_to_zero"], "25.000000")
        self.assertEqual(report["window"]["elapsed_days"], 1)
        self.assertEqual(report["survival_30_days"]["funding_gap"], "600.000000")
        ledger.log_expense("one", "1000", category="operating", note="Equipment", booked_on="2026-01-01")
        first_day = ledger.snapshot("2026-01-01")
        self.assertEqual(first_day["burn"]["observed_daily"], "1000.000000")
        self.assertEqual(first_day["days_to_zero"], "2.000000")
        self.assertEqual(ledger.snapshot()["burn"]["effective_daily"], "120.000000")

    def test_zero_burn_positive_cash_is_unlimited_and_serializes_as_null(self):
        ledger = self.make_ledger(living_ceiling_30_days="0")
        report = json.loads(ledger.export_json())
        self.assertIsNone(report["days_to_zero"])
        self.assertTrue(report["survival_30_days"]["funded"])
        self.assertEqual(report["status"], "OK")
        self.assertEqual({row["closing_cash"] for row in report["projection_30_days"]}, {"3000.000000"})

    def test_zero_or_negative_cash_reports_zero_dtz_including_zero_burn(self):
        for ceiling in ("0", "3000"):
            ledger = self.make_ledger(opening_cash="0", living_ceiling_30_days=ceiling)
            report = ledger.snapshot()
            self.assertEqual(report["days_to_zero"], "0.000000")
            self.assertEqual(report["status"], "RED_ALERT")
            self.assertIn("CASH_EXHAUSTED", {row["code"] for row in report["alerts"]})
        self.expense(amount="3500", category="operating")
        report = self.ledger.snapshot()
        self.assertEqual(report["balance_sheet"]["closing_cash"], "-500.000000")
        self.assertEqual(report["days_to_zero"], "0.000000")
        self.assertGreater(Decimal(report["survival_30_days"]["funding_gap"]), Decimal("3500"))

    def test_fractional_burn_projection_apportions_instead_of_losing_micropayments(self):
        ledger = self.make_ledger(opening_cash="0.000001", living_ceiling_30_days="0.000001")
        report = ledger.snapshot()
        self.assertEqual(report["days_to_zero"], "30.000000")
        self.assertEqual(report["survival_30_days"]["required_cash"], "0.000001")
        self.assertEqual(sum(Decimal(row["burn"]) for row in report["projection_30_days"]), Decimal("0.000001"))
        self.assertEqual(report["projection_30_days"][-1]["closing_cash"], "0.000000")
        self.assertEqual((report["burn"]["effective_micros_numerator"],
                          report["burn"]["effective_micros_denominator"]), (1, 30))

    def test_window_opening_retains_old_cashflows_but_burn_expires_after_thirty_days(self):
        self.fee(amount="100", booked_on="2026-01-01")
        self.expense(amount="300", booked_on="2026-01-01")
        self.clock.advance()
        report = self.ledger.snapshot()
        self.assertEqual(report["window"]["start_date"], "2026-01-02")
        self.assertEqual(report["window"]["opening_cash"], "2800.000000")
        self.assertEqual(report["burn"]["observed_daily"], "0.000000")
        self.assertEqual(report["balance_sheet"]["closing_cash"], "2800.000000")
        self.assertEqual(report["daily_ledger"][0]["opening_cash"], "2800.000000")
        self.assertEqual(report["balance_sheet"]["reconciliation_difference"], "0.000000")


class CeilingAndAlertTests(LedgerCase):
    def test_lump_sum_rent_at_exact_ceiling_is_allowed_and_extra_micro_is_rejected(self):
        self.expense(amount="3000", note="Monthly rent")
        with self.assertRaises(model.CeilingExceededError):
            self.expense("extra", "0.000001")
        report = self.ledger.snapshot()
        self.assertEqual(len(report["entries"]), 1)
        self.assertEqual(report["window"]["living_budget_remaining"], "0.000000")

    def test_ceiling_is_rolling_not_a_calendar_month_reset(self):
        self.expense(amount="2000", booked_on="2026-01-30")
        self.clock.advance(days=2)
        with self.assertRaises(model.CeilingExceededError):
            self.expense("feb", "1000.000001", booked_on="2026-02-01")
        self.expense("feb", "1000", booked_on="2026-02-01")

    def test_backdated_expense_checks_later_affected_windows_and_rolls_back(self):
        self.expense(amount="2000", booked_on="2026-01-30")
        before = self.ledger.export_json()
        with self.assertRaises(model.CeilingExceededError):
            self.expense("backdated", "1500", booked_on="2026-01-01")
        self.assertEqual(self.ledger.export_json(), before)

    def test_ceiling_reset_boundary_excludes_exactly_thirty_day_old_payment(self):
        self.expense(amount="3000", booked_on="2026-01-01")
        self.clock.advance()
        self.expense("next-window", "3000", booked_on="2026-01-31")
        report = self.ledger.snapshot()
        self.assertEqual(report["window"]["living_expenses"], "3000.000000")
        self.assertNotIn("LIVING_CEILING_BREACHED", {alert["code"] for alert in report["alerts"]})

    def test_explicit_override_preserves_real_cashflow_and_triggers_red_alert(self):
        ledger = self.make_ledger(opening_cash="10000")
        ledger.log_expense("actual", "3000.000001", note="Actual emergency rent",
                           allow_over_ceiling=True)
        report = ledger.snapshot()
        self.assertEqual(report["balance_sheet"]["closing_cash"], "6999.999999")
        self.assertEqual(report["window"]["living_budget_remaining"], "-0.000001")
        self.assertEqual(report["status"], "RED_ALERT")
        self.assertIn("LIVING_CEILING_BREACHED", {alert["code"] for alert in report["alerts"]})
        self.assertTrue(report["entries"][0]["ceiling_override"])
        with self.assertRaises(model.CeilingExceededError):
            ledger.log_expense("extra", "1", note="Would worsen existing breach")

    def test_override_requires_boolean_living_category_and_nonempty_note(self):
        for values in ({"allow_over_ceiling": 1}, {"allow_over_ceiling": "yes"},
                       {"allow_over_ceiling": True, "category": "operating"},
                       {"allow_over_ceiling": True, "note": ""}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.expense(**values)
        self.assertEqual(self.ledger.list_entries(), [])

    def test_alert_thresholds_are_inclusive_and_not_rounded_before_comparison(self):
        cases = (("700", "RED_ALERT", "DTZ_CRITICAL"),
                 ("700.000001", "WARNING", "DTZ_WARNING"),
                 ("1400", "WARNING", "DTZ_WARNING"),
                 ("1400.000001", "OK", None))
        for cash, status, code in cases:
            with self.subTest(cash=cash):
                report = self.make_ledger(opening_cash=cash).snapshot()
                self.assertEqual(report["status"], status)
                self.assertEqual({alert["code"] for alert in report["alerts"]}, {code} if code else set())

    def test_configurable_thresholds_and_recovery_after_settled_gig(self):
        ledger = self.make_ledger(opening_cash="100", red_alert_days=2, warning_days=5)
        self.assertEqual(ledger.snapshot()["status"], "RED_ALERT")
        ledger.log_contract_deposit("win", "gig", "4000", reference="bank:win", status="pending")
        self.assertEqual(ledger.snapshot()["status"], "RED_ALERT")
        ledger.settle_income("win")
        self.assertEqual(ledger.snapshot()["status"], "OK")
        self.assertEqual(ledger.snapshot()["days_to_zero"], "41.000000")


class GigTests(LedgerCase):
    def test_closed_gig_extension_uses_all_settled_receipts_less_fees_and_costs(self):
        self.ledger.log_contract_deposit("d", "gig-1", "1000", fee="10", reference="bank:d")
        self.ledger.log_escrow_payout("e", "gig-1", "m1", "500", fee="5", reference="escrow:e")
        self.ledger.log_escrow_payout("pending", "gig-1", "m2", "9000", reference="escrow:p", status="pending")
        self.expense(amount="85", category="operating", gig_id="gig-1")
        self.ledger.close_gig("gig-1")
        gig = self.ledger.snapshot()["closed_gigs"][0]
        self.assertEqual(gig["settled_net_receipts"], "1485.000000")
        self.assertEqual(gig["net_cash_contribution"], "1400.000000")
        self.assertEqual(gig["runway_extension_days"], "14.000000")
        self.assertEqual(gig["entry_ids"], ["d", "e", "expense-1"])

    def test_open_gigs_do_not_appear_and_later_settlement_extends_closed_gig(self):
        self.ledger.log_contract_deposit("d", "gig-1", "1000", reference="bank:d", status="pending")
        self.assertEqual(self.ledger.snapshot()["closed_gigs"], [])
        self.ledger.close_gig("gig-1")
        self.assertEqual(self.ledger.snapshot()["closed_gigs"][0]["runway_extension_days"], "0.000000")
        self.clock.advance()
        self.ledger.settle_income("d")
        self.assertEqual(self.ledger.snapshot()["closed_gigs"][0]["runway_extension_days"], "10.000000")
        self.assertEqual(self.ledger.snapshot("2026-01-30")["closed_gigs"][0]["runway_extension_days"], "0.000000")

    def test_multiple_gigs_have_independent_contributions_and_historical_closure(self):
        self.ledger.log_contract_deposit("one", "gig-1", "100", reference="bank:1", booked_on="2026-01-01")
        self.ledger.log_contract_deposit("two", "gig-2", "200", reference="bank:2", booked_on="2026-01-01")
        self.ledger.close_gig("gig-1", closed_on="2026-01-10")
        self.ledger.close_gig("gig-2", closed_on="2026-01-20")
        self.assertEqual([gig["runway_extension_days"] for gig in self.ledger.snapshot()["closed_gigs"]],
                         ["1.000000", "2.000000"])
        self.assertEqual([gig["gig_id"] for gig in self.ledger.snapshot("2026-01-15")["closed_gigs"]], ["gig-1"])

    def test_negative_gig_contribution_is_signed_and_zero_burn_is_undefined(self):
        self.ledger.log_contract_deposit("one", "gig-1", "100", reference="bank:1")
        self.expense(amount="150", gig_id="gig-1", category="operating")
        self.ledger.close_gig("gig-1")
        self.assertEqual(self.ledger.snapshot()["closed_gigs"][0]["runway_extension_days"], "-0.500000")
        ledger = self.make_ledger(living_ceiling_30_days="0")
        ledger.log_contract_deposit("one", "gig", "100", reference="bank:zero")
        ledger.close_gig("gig")
        self.assertIsNone(ledger.snapshot()["closed_gigs"][0]["runway_extension_days"])

    def test_close_gig_requires_existing_receipt_and_matching_retry(self):
        with self.assertRaises(ValueError):
            self.ledger.close_gig("missing")
        self.ledger.log_contract_deposit("d", "gig", "10", reference="bank:d", booked_on="2026-01-10")
        with self.assertRaises(ValueError):
            self.ledger.close_gig("gig", closed_on="2026-01-09")
        first = self.ledger.close_gig("gig", note="Delivered")
        self.assertEqual(self.ledger.close_gig("gig", note="Delivered"), first)
        with self.assertRaises(model.IdempotencyConflict):
            self.ledger.close_gig("gig", note="Changed")


class ValidationAndRetryTests(LedgerCase):
    def test_entry_retries_are_idempotent_and_conflicting_amount_or_fee_fails(self):
        first = self.fee(amount="10", fee="1")
        self.assertEqual(self.fee(amount=Decimal("10.000000"), fee=1), first)
        for values in ({"amount": "11", "fee": "1"}, {"amount": "10", "fee": "2"},
                       {"amount": "10", "fee": "1", "note": "changed"}):
            with self.subTest(values=values), self.assertRaises(model.IdempotencyConflict):
                self.fee(**values)
        self.expense()
        self.expense()
        self.assertEqual(len(self.ledger.list_entries()), 2)

    def test_replaying_original_pending_entry_after_settlement_does_not_double_count(self):
        self.fee(amount="10", status="pending")
        first = self.ledger.settle_income("fee-1")
        self.assertEqual(self.ledger.settle_income("fee-1"), first)
        self.assertEqual(self.fee(amount="10", status="pending"), first)
        self.assertEqual(self.ledger.snapshot()["balance_sheet"]["closing_cash"], "3010.000000")
        with self.assertRaises(model.IdempotencyConflict):
            self.ledger.settle_income("fee-1", settled_on="2026-01-29")
        self.clock.advance()
        self.assertEqual(self.ledger.settle_income("fee-1"), first)
        self.assertEqual(self.ledger.snapshot()["balance_sheet"]["closing_cash"], "3010.000000")

    def test_transfer_reference_cannot_be_recredited_with_different_id_or_kind(self):
        self.fee(amount="10", reference="bank:same")
        with self.assertRaises(model.IdempotencyConflict):
            self.fee("different-id", "10", reference="bank:same")
        with self.assertRaises(model.IdempotencyConflict):
            self.ledger.log_contract_deposit("deposit", "gig", "10", reference="bank:same")
        self.assertEqual(len(self.ledger.list_entries()), 1)

    def test_invalid_income_amounts_fees_status_and_currency_are_atomic(self):
        invalid = ({"amount": "0"}, {"amount": "-1"}, {"amount": 0.05},
                   {"amount": "0.0000001"}, {"amount": "10", "fee": "10.000001"},
                   {"fee": "-1"}, {"status": "failed"}, {"status": None},
                   {"currency": "USD"}, {"reference": ""}, {"entry_id": ""},
                   {"note": "two\nlines"}, {"gig_id": " "}, {"milestone_id": "wrong-kind"})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.fee(**changes)
        self.assertEqual(self.ledger.list_entries(), [])
        self.fee(amount="10", fee="10")
        self.assertEqual(self.ledger.snapshot()["income_by_source"]["feegate"]["net"], "0.000000")

    def test_income_metadata_and_expense_validation(self):
        for values in ({"kind": "unknown"}, {"kind": "contract_deposit"},
                       {"kind": "escrow_payout", "gig_id": "gig"},
                       {"kind": "feegate", "milestone_id": "m"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.ledger.record_income("bad", amount="1", reference="provider:bad", **values)
        for values in ({"category": "fun"}, {"note": ""}, {"amount": "0"},
                       {"amount": True}, {"currency": "USDC"}, {"gig_id": ""}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.expense(**values)
        self.assertEqual(self.ledger.list_entries(), [])

    def test_dates_reject_before_start_future_malformed_and_impossible_dates(self):
        for value in ("2025-12-31", "2026-01-31", "2026-02-30", "20260102", "2026-1-02", True):
            for operation in (lambda: self.fee(booked_on=value),
                              lambda: self.expense(booked_on=value),
                              lambda: self.ledger.snapshot(value)):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    operation()
        self.assertEqual(self.ledger.list_entries(), [])
        self.fee(booked_on=date(2026, 1, 10))
        self.assertEqual(self.ledger.snapshot("2026-01-09")["entries"], [])

    def test_settlement_rejects_missing_expense_and_date_before_booking(self):
        self.expense()
        self.fee(status="pending", booked_on="2026-01-20")
        for identifier, when in (("missing", "2026-01-30"), ("expense-1", "2026-01-30"),
                                 ("fee-1", "2026-01-19"), ("fee-1", "2026-01-31")):
            with self.subTest(identifier=identifier, when=when), self.assertRaises(ValueError):
                self.ledger.settle_income(identifier, settled_on=when)
        pending = next(row for row in self.ledger.list_entries() if row["entry_id"] == "fee-1")
        self.assertEqual(pending["status"], "pending")

    def test_returned_data_cannot_mutate_persisted_facts(self):
        first = self.fee(amount="10")
        first["amount_micros"] = 999
        self.ledger.list_entries()[0]["note"] = "Tampered"
        report = self.ledger.snapshot()
        report["entries"][0]["amount_micros"] = 0
        self.assertEqual(self.ledger.snapshot()["balance_sheet"]["closing_cash"], "3010.000000")
        self.assertEqual(self.ledger.list_entries()[0]["note"], "")

    def test_utc_clock_normalization_determines_default_booking_day(self):
        self.clock.now = datetime(2026, 1, 31, 1, tzinfo=timezone(timedelta(hours=8)))
        row = self.fee()
        self.assertEqual(row["booked_on"], "2026-01-30")
        self.assertEqual(row["recorded_at"], "2026-01-30T17:00:00+00:00")


class AuditTests(LedgerCase):
    def test_json_is_strict_roundtrippable_and_checksum_binds_all_content(self):
        self.fee(amount="0.05", note="API 收入")
        raw = self.ledger.export_json()
        report = json.loads(raw)
        digest = report.pop("snapshot_sha256")
        canonical = json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False,
                               separators=(",", ":"))
        self.assertEqual(digest, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertIn("API 收入", raw)
        self.assertNotIn("NaN", raw)
        self.assertNotIn("Infinity", raw)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["basis"], "cash")
        self.assertEqual(report["entries"][0]["amount_micros"], 50000)
        self.expense(amount="1")
        self.assertNotEqual(digest, self.ledger.snapshot()["snapshot_sha256"])

    def test_markdown_contains_balances_source_evidence_gigs_and_forecast(self):
        self.ledger.log_escrow_payout("p1", "gig-1", "m1", "100", reference="escrow:tx1")
        self.ledger.close_gig("gig-1")
        report = self.ledger.snapshot()
        markdown = model.render_markdown(report)
        for expected in ("Cash balance sheet", "Days-to-Zero: 31.000000", "3100.000000",
                         "escrow:tx1", "gig-1/m1", "Closed gig contributions", "Entry audit trail",
                         "Daily cash reconciliation", "30-day projection", "DTZ <= 7", report["snapshot_sha256"]):
            with self.subTest(expected=expected):
                self.assertIn(expected, markdown)
        self.assertEqual(self.ledger.export_markdown(), markdown)

    def test_markdown_escapes_user_controlled_html_and_table_delimiters(self):
        self.fee(entry_id="<script>|entry", note="<img src=x>|note", reference="provider:tx|1")
        markdown = self.ledger.export_markdown()
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("<img", markdown)
        self.assertIn("&lt;script&gt;&#124;entry", markdown)
        self.assertIn("provider:tx&#124;1", markdown)

    def test_each_daily_row_and_lifetime_balance_reconcile_independently(self):
        self.fee("f1", "10", fee="1", booked_on="2026-01-01")
        self.fee("f2", "100", fee="2", status="pending", booked_on="2026-01-05")
        self.ledger.settle_income("f2", settled_on="2026-01-25")
        self.expense("l1", "100", booked_on="2026-01-10")
        self.expense("h1", "15.000001", booked_on="2026-01-20", category="operating")
        report = self.ledger.snapshot()
        previous = Decimal(report["window"]["opening_cash"])
        for row in report["daily_ledger"]:
            self.assertEqual(Decimal(row["opening_cash"]), previous)
            calculated = (previous + Decimal(row["gross_receipts"]) - Decimal(row["payment_fees"])
                          - Decimal(row["living_expenses"]) - Decimal(row["operating_expenses"]))
            self.assertEqual(Decimal(row["closing_cash"]), calculated)
            previous = calculated
        self.assertEqual(previous, Decimal("2991.999999"))
        sheet = report["balance_sheet"]
        self.assertEqual(Decimal(sheet["closing_cash"]), previous)
        self.assertEqual(Decimal(sheet["opening_cash"]) + Decimal(sheet["net_receipts"])
                         - Decimal(sheet["total_expenses"]), previous)


class PersistenceAndConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "ledger.sqlite3"
        self.clock = Clock()

    def test_reopen_preserves_config_cash_settlements_gigs_and_idempotency(self):
        with model.EmergencyLiquidityLedger(self.db_path, config(), clock=self.clock) as ledger:
            ledger.log_contract_deposit("d", "gig", "100", reference="bank:d", status="pending")
            ledger.settle_income("d")
            ledger.close_gig("gig")
            ledger.log_expense("l", "50", note="Food")
            before = ledger.export_json()
        with model.EmergencyLiquidityLedger(self.db_path, clock=self.clock) as ledger:
            self.assertEqual(ledger.config, config())
            self.assertEqual(ledger.export_json(), before)
            ledger.log_contract_deposit("d", "gig", "100", reference="bank:d", status="pending")
            ledger.settle_income("d")
            self.assertEqual(ledger.snapshot()["balance_sheet"]["closing_cash"], "3050.000000")
        with self.assertRaises(ValueError):
            model.EmergencyLiquidityLedger(self.db_path, config(opening_cash="1"), clock=self.clock)

    def test_unsupported_schema_is_rejected(self):
        with model.EmergencyLiquidityLedger(self.db_path, config(), clock=self.clock):
            pass
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
        with self.assertRaisesRegex(ValueError, "schema version"):
            model.EmergencyLiquidityLedger(self.db_path, clock=self.clock)

    def test_concurrent_duplicate_receipts_credit_only_once(self):
        with model.EmergencyLiquidityLedger(self.db_path, config(), clock=self.clock):
            pass
        barrier = threading.Barrier(2)

        def record():
            with model.EmergencyLiquidityLedger(self.db_path, clock=self.clock) as ledger:
                barrier.wait(timeout=10)
                return ledger.log_feegate_payment("same", "0.000001", reference="feegate:once")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: record(), range(2)))
        self.assertEqual(results[0], results[1])
        with model.EmergencyLiquidityLedger(self.db_path, clock=self.clock) as ledger:
            self.assertEqual(ledger.snapshot()["balance_sheet"]["closing_cash"], "3000.000001")
            self.assertEqual(len(ledger.list_entries()), 1)

    def test_concurrent_expenses_cannot_bypass_ceiling(self):
        with model.EmergencyLiquidityLedger(self.db_path, config(), clock=self.clock):
            pass
        barrier = threading.Barrier(2)

        def spend(identifier):
            with model.EmergencyLiquidityLedger(self.db_path, clock=self.clock) as ledger:
                barrier.wait(timeout=10)
                try:
                    ledger.log_expense(identifier, "2000", note="Rent")
                    return "recorded"
                except model.CeilingExceededError:
                    return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(spend, ("one", "two")))
        self.assertCountEqual(results, ["recorded", "rejected"])
        with model.EmergencyLiquidityLedger(self.db_path, clock=self.clock) as ledger:
            self.assertEqual(ledger.snapshot()["window"]["living_expenses"], "2000.000000")


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "ledger.sqlite3"
        self.script = Path(model.__file__).resolve()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.script), "--db", str(self.db_path), *args],
                              capture_output=True, text=True, timeout=15)

    def successful(self, *args):
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def initialize(self):
        self.successful("init", "--opening-cash", "3000", "--living-ceiling", "3000", "--currency", "CNY")

    def test_cli_end_to_end_records_pending_settlement_and_exports_both_formats(self):
        self.initialize()
        self.successful("income", "d1", "contract_deposit", "1000", "--gig-id", "gig",
                        "--reference", "bank:d1", "--fee", "10", "--pending")
        pending = json.loads(self.successful("report").stdout)
        self.assertEqual(pending["balance_sheet"]["closing_cash"], "3000.000000")
        self.successful("settle", "d1")
        self.successful("expense", "food", "100", "--note", "Food")
        self.successful("close-gig", "gig", "--note", "Delivered")
        json_path, md_path = self.root / "balance.json", self.root / "balance.md"
        self.assertEqual(self.successful("report", "--output", str(json_path)).stdout, "")
        report = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(report["balance_sheet"]["closing_cash"], "3890.000000")
        self.assertEqual(report["closed_gigs"][0]["settled_net_receipts"], "990.000000")
        self.successful("report", "--format", "markdown", "--output", str(md_path))
        self.assertIn("3890.000000", md_path.read_text(encoding="utf-8"))
        self.assertIn("Entry audit trail", md_path.read_text(encoding="utf-8"))

    def test_cli_reports_errors_without_tracebacks_or_partial_financial_writes(self):
        self.assertNotEqual(self.run_cli("report").returncode, 0)
        self.initialize()
        cases = (("expense", "bad", "3001", "--note", "Too much"),
                 ("income", "bad", "feegate", "NaN", "--reference", "feegate:nan"),
                 ("income", "bad", "escrow_payout", "10", "--reference", "escrow:bad"),
                 ("settle", "missing"),
                 ("report", "--as-of", "not-a-date"),
                 ("report", "--output", str(self.root / "missing" / "report.json")))
        for args in cases:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(json.loads(self.successful("report").stdout)["entries"], [])

    def test_cli_explicit_ceiling_override_records_breach(self):
        self.initialize()
        self.successful("expense", "actual", "3001", "--note", "Actual emergency spending",
                        "--allow-over-ceiling")
        report = json.loads(self.successful("report").stdout)
        self.assertEqual(report["status"], "RED_ALERT")
        self.assertIn("LIVING_CEILING_BREACHED", {alert["code"] for alert in report["alerts"]})


if __name__ == "__main__":
    unittest.main()
