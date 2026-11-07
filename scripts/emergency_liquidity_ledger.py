#!/usr/bin/env python3
"""Persistent, standard-library emergency cash ledger and 30-day runway tracker.

Amounts accept strings, integers, or Decimal, with at most six decimal places;
floats are rejected. SQLite stores immutable receipt/expense payloads in integer
millionths, preserving FeeGate micropayments. One ledger uses one currency (CNY
by default, also e.g. USD or USDC). Supply already converted, spendable amounts;
this offline ledger does not convert currencies or fetch provider settlements.
Use a unique, provider-qualified reference for every income transfer. A payout
must represent new cash, not the release of a deposit already counted here.

Only settled receipts increase cash. Pending escrow/deposits are informational.
Fees reduce receipts; expenses reduce cash only when explicitly logged. Missing
days do not silently debit estimated expenses. Keep the ledger up to date.
All default dates use UTC; explicit dates are ISO YYYY-MM-DD. Backdated reports
restate cashflow using the recorded booking/settlement dates.

The living ceiling applies to EVERY rolling 30-calendar-day window, including
windows affected by backdated entries. It is not a daily payment limit: rent
may be paid in one installment. Overspending is rejected unless explicitly
recorded with allow_over_ceiling=True and a note, preserving actual spending.
Daily planned burn = living_ceiling_30_days / 30 + daily_operating_budget.
Observed burn = gross expenses / elapsed days in the trailing 30-day window
(including zero-spend days). DTZ = max(settled cash, 0) / max(planned, observed)
burn; speculative future income is excluded. Zero burn gives unlimited DTZ
(JSON null) only while cash is positive. Threshold comparisons use exact ratios.
Closed gig extension = (settled net receipts - linked expenses) / current burn;
this signed funding contribution is not a promise of future receipts.

Snapshots contain a cash-basis balance sheet, source totals, pending funds,
daily reconciliation, a 30-day no-income forecast, closed gigs and alerts.
Money and day ratios export as six-place strings; calculations use integers
and exact fractions. A SHA-256 checksum binds the full JSON snapshot for
consistency checking; it does not authenticate a bank balance or the operator.

Python API: LedgerConfig -> EmergencyLiquidityLedger -> log_contract_deposit /
log_escrow_payout / log_feegate_payment / log_expense -> settle_income ->
close_gig -> snapshot / export_json / export_markdown. Entry retries with the
same ID and immutable payload are idempotent, including after settlement.

CLI examples (all mutations return JSON)::

    python3 scripts/emergency_liquidity_ledger.py --db /tmp/survival.db init \
        --opening-cash 2000 --living-ceiling 3000 --start-date 2026-11-01
    python3 scripts/emergency_liquidity_ledger.py --db /tmp/survival.db income \
        deposit-1 contract_deposit 1500 --gig-id gig-1 --reference bank:tx-1
    python3 scripts/emergency_liquidity_ledger.py --db /tmp/survival.db expense \
        rent-1 1000 --note 'November rent'
    python3 scripts/emergency_liquidity_ledger.py --db /tmp/survival.db report \
        --format markdown --output /tmp/liquidity.md

Run tests: python3 -m unittest discover -s scripts \
    -p 'test_emergency_liquidity_ledger.py' -v
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable


SCALE = 1_000_000
DAYS = 30
SCHEMA_VERSION = 1
MAX_MICROS = 2**63 - 1
MAX_AMOUNT = Decimal(f"{MAX_MICROS // SCALE}.{MAX_MICROS % SCALE:06d}")
INCOME_KINDS = ("contract_deposit", "escrow_payout", "feegate")


class CeilingExceededError(ValueError):
    """A living expense would exceed the strict rolling 30-day ceiling."""


class IdempotencyConflict(ValueError):
    """A previously recorded entry, transfer reference, or gig conflicts."""


def _text(value, name, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name} must be a {'string' if empty else 'nonempty string'}")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _date(value, name="date"):
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{name} must be an ISO YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a valid calendar date") from error


def _micros(value, name="amount", *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name} must be a decimal string, integer, or Decimal; no floats")
    try:
        amount = Decimal(value)
        if (not amount.is_finite() or amount < 0
                or amount > MAX_AMOUNT):
            raise ValueError(f"{name} must be finite, nonnegative, and within the ledger limit")
        if amount and amount < Decimal("0.000001"):
            raise ValueError(f"{name} must have at most six decimal places")
        numerator, denominator = amount.as_integer_ratio()
        scaled, remainder = divmod(numerator * SCALE, denominator)
        if remainder or (positive and scaled == 0):
            raise ValueError(f"{name} must {'be positive and ' if positive else ''}have at most six decimal places")
        return scaled
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a valid decimal amount") from error


def _fixed(value):
    """Format an integer scaled by 1e6, including negative values, exactly."""
    whole, fraction = divmod(abs(value), SCALE)
    return f"{'-' if value < 0 else ''}{whole}.{fraction:06d}"


def _ratio(value):
    """Six-place ROUND_HALF_UP formatting without floating-point arithmetic."""
    numerator = abs(value.numerator) * SCALE
    rounded = (2 * numerator + value.denominator) // (2 * value.denominator)
    return _fixed(-rounded if value < 0 else rounded)


def _ceil(value):
    return -(-value.numerator // value.denominator)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def _utc_now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LedgerConfig:
    opening_cash: str = "0"
    living_ceiling_30_days: str = "3000"
    daily_operating_budget: str = "0"
    currency: str = "CNY"
    start_date: str = field(default_factory=lambda: _utc_now().date().isoformat())
    red_alert_days: int = 7
    warning_days: int = 14

    def __post_init__(self):
        for name in ("opening_cash", "living_ceiling_30_days", "daily_operating_budget"):
            object.__setattr__(self, name, _fixed(_micros(getattr(self, name), name)))
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{2,9}", self.currency):
            raise ValueError("currency must be an uppercase 3-10 character currency/asset code")
        object.__setattr__(self, "start_date", _date(self.start_date, "start_date").isoformat())
        if (type(self.red_alert_days) is not int or type(self.warning_days) is not int
                or not 1 <= self.red_alert_days <= self.warning_days <= DAYS):
            raise ValueError("thresholds must be integers: 1 <= red_alert_days <= warning_days <= 30")


class EmergencyLiquidityLedger:
    """Durable cash facts; serialized SQLite writes protect ceiling/idempotency checks."""

    def __init__(self, db_path=":memory:", config: LedgerConfig | None = None,
                 *, clock: Callable[[], datetime] = _utc_now):
        if config is not None and not isinstance(config, LedgerConfig):
            raise ValueError("config must be a LedgerConfig")
        self._clock = clock
        self._db = sqlite3.connect(str(db_path), isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entries (
                    entry_id TEXT PRIMARY KEY, payload TEXT NOT NULL, recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settlements (
                    entry_id TEXT PRIMARY KEY REFERENCES entries(entry_id),
                    settled_on TEXT NOT NULL, recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS closed_gigs (
                    gig_id TEXT PRIMARY KEY, closed_on TEXT NOT NULL,
                    note TEXT NOT NULL, recorded_at TEXT NOT NULL
                );
            """)
            with self._write():
                stored = dict(self._db.execute("SELECT key, value FROM metadata"))
                if not stored:
                    if config is None:
                        raise ValueError("new ledger requires a LedgerConfig; use the init command")
                    if _date(config.start_date) > self._now().date():
                        raise ValueError("start_date must not be in the future")
                    self._db.executemany("INSERT INTO metadata VALUES (?, ?)", [
                        ("schema_version", str(SCHEMA_VERSION)),
                        ("config", _canonical(asdict(config))),
                    ])
                else:
                    if stored.get("schema_version") != str(SCHEMA_VERSION):
                        raise ValueError("unsupported ledger schema version")
                    saved = LedgerConfig(**json.loads(stored["config"]))
                    if config is not None and config != saved:
                        raise ValueError("configuration differs from the existing ledger")
                    config = saved
            self.config = config
        except BaseException:
            self._db.close()
            raise

    def _now(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @contextmanager
    def _write(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _day(self, value=None):
        today = self._now().date()
        result = today if value is None else _date(value)
        if not _date(self.config.start_date) <= result <= today:
            raise ValueError("date must be between the ledger start_date and today (UTC)")
        return result.isoformat()

    def _rows(self):
        rows = []
        for record in self._db.execute("""
                SELECT e.payload, e.recorded_at, s.settled_on, s.recorded_at AS settlement_recorded_at
                FROM entries e LEFT JOIN settlements s ON e.entry_id = s.entry_id
                ORDER BY e.entry_id"""):
            row = json.loads(record["payload"])
            row["recorded_at"] = record["recorded_at"]
            row["settled_on"] = (row["booked_on"] if row["initial_status"] == "settled"
                                 else record["settled_on"])
            row["settlement_recorded_at"] = record["settlement_recorded_at"]
            row["status"] = "settled" if row["settled_on"] else "pending"
            rows.append(row)
        return rows

    def list_entries(self, as_of=None):
        """Return independent entry dictionaries, restated at the requested date."""
        day = self._day(as_of)
        return self._entries_as_of(self._rows(), day)

    @staticmethod
    def _entries_as_of(rows, day):
        result = []
        for row in rows:
            if row["booked_on"] > day:
                continue
            row = dict(row)
            if row["settled_on"] and row["settled_on"] > day:
                row.update(status="pending", settled_on=None, settlement_recorded_at=None)
            result.append(row)
        return sorted(result, key=lambda row: (row["booked_on"], row["entry_id"]))

    def _insert(self, payload, *, check_ceiling=False):
        encoded = _canonical(payload)
        with self._write():
            previous = self._db.execute("SELECT payload FROM entries WHERE entry_id = ?",
                                        (payload["entry_id"],)).fetchone()
            if previous is not None:
                if previous[0] != encoded:
                    raise IdempotencyConflict("entry_id already has a different payload")
            else:
                rows = self._rows()
                if payload["kind"] in INCOME_KINDS and any(
                        row["reference"] == payload["reference"] for row in rows):
                    raise IdempotencyConflict("income reference was already recorded under another entry_id")
                if check_ceiling:
                    self._check_living_ceiling(rows + [payload], payload["booked_on"])
                self._db.execute("INSERT INTO entries VALUES (?, ?, ?)",
                                 (payload["entry_id"], encoded, self._now().isoformat()))
            return next(row for row in self._rows() if row["entry_id"] == payload["entry_id"])

    def _check_living_ceiling(self, rows, changed_on):
        expenses = sorted((row["booked_on"], row["amount_micros"]) for row in rows
                          if row["kind"] == "expense" and row["category"] == "living")
        window, total = deque(), 0
        limit = _micros(self.config.living_ceiling_30_days)
        for day, amount in expenses:
            ordinal = _date(day).toordinal()
            while window and window[0][0] <= ordinal - DAYS:
                total -= window.popleft()[1]
            window.append((ordinal, amount))
            total += amount
            if (0 <= ordinal - _date(changed_on).toordinal() < DAYS and total > limit):
                raise CeilingExceededError(
                    f"living expenses exceed the rolling 30-day ceiling ending {day}; "
                    "record actual overspending with allow_over_ceiling=True and a note")

    def record_income(self, entry_id, kind, amount, *, reference, gig_id=None,
                      milestone_id=None, fee="0", status="settled", booked_on=None,
                      currency=None, note=""):
        """Record one gross transfer and its fee, settled or pending (never both)."""
        _text(entry_id, "entry_id")
        if kind not in INCOME_KINDS:
            raise ValueError(f"income kind must be one of {INCOME_KINDS}")
        if status not in ("pending", "settled"):
            raise ValueError("income status must be pending or settled")
        if currency is not None and currency != self.config.currency:
            raise ValueError("currency differs from ledger currency; convert before logging")
        if gig_id is not None:
            _text(gig_id, "gig_id")
        if kind in ("contract_deposit", "escrow_payout") and gig_id is None:
            raise ValueError("contract deposits and escrow payouts require gig_id")
        if kind == "escrow_payout":
            _text(milestone_id, "milestone_id")
        elif milestone_id is not None:
            raise ValueError("only escrow payouts have a milestone_id")
        gross, fees = _micros(amount, positive=True), _micros(fee, "fee")
        if fees > gross:
            raise ValueError("fee must not exceed gross income")
        return self._insert({
            "entry_id": entry_id, "kind": kind, "category": None,
            "amount_micros": gross, "fee_micros": fees,
            "reference": _text(reference, "reference"), "gig_id": gig_id,
            "milestone_id": milestone_id, "initial_status": status,
            "booked_on": self._day(booked_on), "note": _text(note, "note", empty=True),
            "ceiling_override": False,
        })

    def log_contract_deposit(self, entry_id, gig_id, amount, **kwargs):
        return self.record_income(entry_id, "contract_deposit", amount, gig_id=gig_id, **kwargs)

    def log_escrow_payout(self, entry_id, gig_id, milestone_id, amount, **kwargs):
        return self.record_income(entry_id, "escrow_payout", amount, gig_id=gig_id,
                                  milestone_id=milestone_id, **kwargs)

    def log_feegate_payment(self, entry_id, amount, **kwargs):
        return self.record_income(entry_id, "feegate", amount, **kwargs)

    def log_expense(self, entry_id, amount, *, note, category="living", gig_id=None,
                    booked_on=None, currency=None, allow_over_ceiling=False):
        _text(entry_id, "entry_id")
        if category not in ("living", "operating"):
            raise ValueError("expense category must be living or operating")
        if currency is not None and currency != self.config.currency:
            raise ValueError("currency differs from ledger currency")
        if type(allow_over_ceiling) is not bool:
            raise ValueError("allow_over_ceiling must be a boolean")
        if allow_over_ceiling and category != "living":
            raise ValueError("ceiling overrides apply only to living expenses")
        if gig_id is not None:
            _text(gig_id, "gig_id")
        return self._insert({
            "entry_id": entry_id, "kind": "expense", "category": category,
            "amount_micros": _micros(amount, positive=True), "fee_micros": 0,
            "reference": None, "gig_id": gig_id, "milestone_id": None,
            "initial_status": "settled", "booked_on": self._day(booked_on),
            "note": _text(note, "note"), "ceiling_override": allow_over_ceiling,
        }, check_ceiling=category == "living" and not allow_over_ceiling)

    def settle_income(self, entry_id, *, settled_on=None):
        _text(entry_id, "entry_id")
        day = self._day(settled_on)
        with self._write():
            row = next((row for row in self._rows() if row["entry_id"] == entry_id), None)
            if row is None or row["kind"] == "expense":
                raise ValueError("entry_id must identify an income receipt")
            if row["settled_on"]:
                if settled_on is not None and row["settled_on"] != day:
                    raise IdempotencyConflict("income already settled on a different date")
            else:
                if day < row["booked_on"]:
                    raise ValueError("settlement must not precede booking")
                self._db.execute("INSERT INTO settlements VALUES (?, ?, ?)",
                                 (entry_id, day, self._now().isoformat()))
            return next(row for row in self._rows() if row["entry_id"] == entry_id)

    def close_gig(self, gig_id, *, closed_on=None, note=""):
        _text(gig_id, "gig_id")
        day, note = self._day(closed_on), _text(note, "note", empty=True)
        with self._write():
            if not any(row["gig_id"] == gig_id and row["kind"] in INCOME_KINDS
                       and row["booked_on"] <= day for row in self._rows()):
                raise ValueError("a closed gig requires a linked receipt booked by its close date")
            previous = self._db.execute("SELECT * FROM closed_gigs WHERE gig_id = ?", (gig_id,)).fetchone()
            if previous is not None:
                if previous["closed_on"] != day or previous["note"] != note:
                    raise IdempotencyConflict("gig was already closed with different details")
            else:
                self._db.execute("INSERT INTO closed_gigs VALUES (?, ?, ?, ?)",
                                 (gig_id, day, note, self._now().isoformat()))
            return dict(self._db.execute("SELECT * FROM closed_gigs WHERE gig_id = ?", (gig_id,)).fetchone())

    def snapshot(self, as_of=None):
        """Reconciled cash-basis report; all reads share a consistent DB snapshot."""
        day = self._day(as_of)
        self._db.execute("BEGIN")
        try:
            rows = self._entries_as_of(self._rows(), day)
            gigs = [dict(row) for row in self._db.execute(
                "SELECT * FROM closed_gigs WHERE closed_on <= ? ORDER BY gig_id", (day,))]
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return self._build_snapshot(day, rows, gigs)

    def _build_snapshot(self, day, rows, gigs):
        first = max(_date(self.config.start_date).toordinal(), _date(day).toordinal() - DAYS + 1)
        window_start = date.fromordinal(first).isoformat()
        elapsed = _date(day).toordinal() - first + 1
        daily = defaultdict(lambda: {"gross": 0, "fees": 0, "living": 0, "operating": 0})
        sources = {kind: {"gross": 0, "fees": 0, "pending_net": 0, "count": 0}
                   for kind in INCOME_KINDS}
        settled, expenses = [], []
        for row in rows:
            if row["kind"] == "expense":
                expenses.append(row)
                daily[row["booked_on"]][row["category"]] += row["amount_micros"]
            elif row["status"] == "settled":
                settled.append(row)
                source = sources[row["kind"]]
                source["gross"] += row["amount_micros"]
                source["fees"] += row["fee_micros"]
                source["count"] += 1
                daily[row["settled_on"]]["gross"] += row["amount_micros"]
                daily[row["settled_on"]]["fees"] += row["fee_micros"]
            else:
                sources[row["kind"]]["pending_net"] += row["amount_micros"] - row["fee_micros"]
        gross = sum(row["gross"] for row in sources.values())
        fees = sum(row["fees"] for row in sources.values())
        living = sum(row["amount_micros"] for row in expenses if row["category"] == "living")
        operating = sum(row["amount_micros"] for row in expenses if row["category"] == "operating")
        opening = _micros(self.config.opening_cash)
        cash = opening + gross - fees - living - operating
        window_expenses = [row for row in expenses if row["booked_on"] >= window_start]
        window_living = sum(row["amount_micros"] for row in window_expenses if row["category"] == "living")
        ceiling = _micros(self.config.living_ceiling_30_days)
        observed = Fraction(sum(row["amount_micros"] for row in window_expenses), elapsed)
        planned = Fraction(ceiling, DAYS) + _micros(self.config.daily_operating_budget)
        burn = max(planned, observed)
        dtz = Fraction(max(cash, 0), 1) / burn if burn else (None if cash > 0 else Fraction(0))

        balance = opening + sum(row["gross"] - row["fees"] - row["living"] - row["operating"]
                                for when, row in daily.items() if when < window_start)
        window_opening, daily_ledger = balance, []
        for ordinal in range(first, _date(day).toordinal() + 1):
            when = date.fromordinal(ordinal).isoformat()
            item = daily[when]
            net = item["gross"] - item["fees"]
            closing = balance + net - item["living"] - item["operating"]
            daily_ledger.append({"date": when, "opening_cash": _fixed(balance),
                                 "gross_receipts": _fixed(item["gross"]), "payment_fees": _fixed(item["fees"]),
                                 "net_receipts": _fixed(net), "living_expenses": _fixed(item["living"]),
                                 "operating_expenses": _fixed(item["operating"]), "closing_cash": _fixed(closing)})
            balance = closing

        alerts = []
        if cash <= 0:
            alerts.append({"code": "CASH_EXHAUSTED", "severity": "red",
                           "message": "Settled cash is zero or negative."})
        if dtz is not None and dtz <= self.config.red_alert_days:
            alerts.append({"code": "DTZ_CRITICAL", "severity": "red",
                           "message": f"DTZ is at or below {self.config.red_alert_days} days."})
        elif dtz is not None and dtz <= self.config.warning_days:
            alerts.append({"code": "DTZ_WARNING", "severity": "warning",
                           "message": f"DTZ is at or below {self.config.warning_days} days."})
        if window_living > ceiling:
            alerts.append({"code": "LIVING_CEILING_BREACHED", "severity": "red",
                           "message": "Recorded living expenses exceed the rolling 30-day ceiling."})
        target_cost = _ceil(burn * DAYS)
        shortfall = max(0, target_cost - cash)
        projection = []
        for offset in range(1, DAYS + 1):
            # Ceil cumulative fractional millionths; do not round each day's burn.
            cost = _ceil(burn * offset)
            projection.append({"day": offset,
                               "burn": _fixed(cost - _ceil(burn * (offset - 1))),
                               "closing_cash": _fixed(cash - cost)})
        closed = []
        for gig in gigs:
            linked = [row for row in settled if row["gig_id"] == gig["gig_id"]]
            costs = [row for row in expenses if row["gig_id"] == gig["gig_id"]]
            receipts = sum(row["amount_micros"] - row["fee_micros"] for row in linked)
            cost = sum(row["amount_micros"] for row in costs)
            contribution = receipts - cost
            closed.append({**gig, "settled_net_receipts": _fixed(receipts), "expenses": _fixed(cost),
                           "net_cash_contribution": _fixed(contribution),
                           "runway_extension_days": _ratio(Fraction(contribution, 1) / burn) if burn else None,
                           "entry_ids": sorted(row["entry_id"] for row in linked + costs)})
        for source in sources.values():
            source["net"] = source["gross"] - source["fees"]
        report = {
            "schema_version": SCHEMA_VERSION, "as_of": day, "generated_at": self._now().isoformat(),
            "currency": self.config.currency, "basis": "cash", "config": asdict(self.config),
            "balance_sheet": {"opening_cash": _fixed(opening), "gross_receipts": _fixed(gross),
                              "payment_fees": _fixed(fees), "net_receipts": _fixed(gross - fees),
                              "living_expenses": _fixed(living), "operating_expenses": _fixed(operating),
                              "total_expenses": _fixed(living + operating), "closing_cash": _fixed(cash),
                              "reconciliation_difference": _fixed(cash - balance)},
            "window": {"start_date": window_start, "end_date": day, "elapsed_days": elapsed,
                       "opening_cash": _fixed(window_opening), "living_expenses": _fixed(window_living),
                       "living_ceiling": _fixed(ceiling), "living_budget_remaining": _fixed(ceiling - window_living)},
            "burn": {"planned_daily": _ratio(planned / SCALE), "observed_daily": _ratio(observed / SCALE),
                     "effective_daily": _ratio(burn / SCALE),
                     "effective_micros_numerator": burn.numerator, "effective_micros_denominator": burn.denominator},
            "days_to_zero": _ratio(dtz) if dtz is not None else None,
            "survival_30_days": {"required_cash": _fixed(target_cost), "funding_gap": _fixed(shortfall),
                                 "funded": cash >= target_cost},
            "status": "RED_ALERT" if any(a["severity"] == "red" for a in alerts) else ("WARNING" if alerts else "OK"),
            "alerts": alerts, "income_by_source": {
                kind: {key: value if key == "count" else _fixed(value) for key, value in source.items()}
                for kind, source in sources.items()},
            "pending_net_receipts": _fixed(sum(source["pending_net"] for source in sources.values())),
            "daily_ledger": daily_ledger, "projection_30_days": projection,
            "closed_gigs": closed, "entries": rows,
        }
        report["snapshot_sha256"] = hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()
        return report

    def export_json(self, as_of=None):
        return json.dumps(self.snapshot(as_of), ensure_ascii=False, sort_keys=True,
                          indent=2, allow_nan=False) + "\n"

    def export_markdown(self, as_of=None):
        return render_markdown(self.snapshot(as_of))


def render_markdown(report):
    """Render the same snapshot's balances, evidence, thresholds and forecasts."""
    def cell(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(
            ">", "&gt;").replace("|", "&#124;").replace("\n", " ").replace("\r", " ")

    def table(headers, rows):
        return ["| " + " | ".join(map(cell, headers)) + " |",
                "| " + " | ".join("---" for _ in headers) + " |"] + [
                    "| " + " | ".join(map(cell, row)) + " |" for row in rows]

    dtz = report["days_to_zero"] if report["days_to_zero"] is not None else "unlimited (zero burn)"
    lines = ["# Emergency 30-Day Survival Liquidity Ledger", "",
             f"As of {report['as_of']} (UTC) · Currency: {report['currency']} · Basis: cash", "",
             f"**Status: {report['status']} · Days-to-Zero: {dtz}**", "",
             "## Cash balance sheet", ""]
    lines += table(("Account", "Amount"), ((key.replace("_", " "), value)
                   for key, value in report["balance_sheet"].items()))
    lines += ["", "## Burn and 30-day survival", ""]
    metrics = [("Planned daily burn", report["burn"]["planned_daily"]),
               ("Observed daily gross burn", report["burn"]["observed_daily"]),
               ("Effective daily burn", report["burn"]["effective_daily"]),
               ("30-day required cash", report["survival_30_days"]["required_cash"]),
               ("30-day funding gap", report["survival_30_days"]["funding_gap"]),
               ("Pending net receipts (excluded from cash)", report["pending_net_receipts"]),
               ("Rolling living ceiling", report["window"]["living_ceiling"]),
               ("Rolling living expenses", report["window"]["living_expenses"]),
               ("Living budget remaining", report["window"]["living_budget_remaining"])]
    lines += table(("Measure", "Amount"), metrics)
    config = report["config"]
    lines += ["", f"Red alert at DTZ <= {config['red_alert_days']} days; warning at DTZ <= "
              f"{config['warning_days']} days. Zero/negative cash and living ceiling breaches trigger red alerts.", "",
              "## Alerts", ""]
    lines += [f"- {alert['severity'].upper()}: {alert['code']} — {cell(alert['message'])}"
              for alert in report["alerts"]] or ["No active alerts."]
    lines += ["", "## Receipts by source", ""]
    lines += table(("Source", "Gross settled", "Fees", "Net settled", "Pending net"),
                   ((kind, row["gross"], row["fees"], row["net"], row["pending_net"])
                    for kind, row in report["income_by_source"].items()))
    lines += ["", "## Closed gig contributions", ""]
    lines += table(("Gig", "Closed", "Net receipts", "Costs", "Cash contribution", "Extension days"),
                   ((row["gig_id"], row["closed_on"], row["settled_net_receipts"], row["expenses"],
                     row["net_cash_contribution"], row["runway_extension_days"]
                     if row["runway_extension_days"] is not None else "undefined (zero burn)")
                    for row in report["closed_gigs"]))
    lines += ["", "## Daily cash reconciliation", "",
              f"Window: {report['window']['start_date']} through {report['as_of']}; "
              f"opening cash: {report['window']['opening_cash']}.", ""]
    keys = ("date", "opening_cash", "gross_receipts", "payment_fees", "living_expenses", "operating_expenses", "closing_cash")
    lines += table(keys, ([row[key] for key in keys] for row in report["daily_ledger"]))
    lines += ["", "## 30-day projection (no future income)", ""]
    lines += table(("Day after report", "Burn", "Closing cash"),
                   ((row["day"], row["burn"], row["closing_cash"]) for row in report["projection_30_days"]))
    lines += ["", "## Entry audit trail", ""]
    lines += table(("ID", "Booked", "Settled", "Kind/category", "Status", "Gross/expense", "Fee", "Reference", "Gig/milestone", "Override", "Note", "Recorded at", "Settlement recorded at"),
                   ((row["entry_id"], row["booked_on"], row["settled_on"] or "", row["kind"] + ("/" + row["category"] if row["category"] else ""),
                     row["status"], _fixed(row["amount_micros"]), _fixed(row["fee_micros"]), row["reference"] or "",
                     "/".join(filter(None, (row["gig_id"], row["milestone_id"]))), row["ceiling_override"], row["note"],
                     row["recorded_at"], row["settlement_recorded_at"] or "") for row in report["entries"]))
    lines += ["", "DTZ uses settled cash and the higher of planned or observed gross burn. "
              "Only recorded expenses debit cash. The living ceiling covers every rolling 30-day window. "
              "Closed gig extension is net cash contribution divided by current burn. Amounts and day ratios "
              "display six decimals; forecast burn uses cumulative rounding up to one millionth.", "",
              f"Generated at: {report['generated_at']}", "",
              f"Snapshot SHA-256 (canonical companion JSON, excluding this field): `{report['snapshot_sha256']}`", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True, help="SQLite ledger file")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a ledger with immutable configuration")
    init.add_argument("--opening-cash", required=True)
    init.add_argument("--living-ceiling", default="3000", dest="living_ceiling_30_days")
    init.add_argument("--daily-operating-budget", default="0")
    init.add_argument("--currency", default="CNY")
    init.add_argument("--start-date", default=None)
    init.add_argument("--red-alert-days", type=int, default=7)
    init.add_argument("--warning-days", type=int, default=14)
    income = commands.add_parser("income", help="log a settled or pending transfer")
    income.add_argument("entry_id")
    income.add_argument("kind", choices=INCOME_KINDS)
    income.add_argument("amount")
    income.add_argument("--reference", required=True)
    income.add_argument("--gig-id")
    income.add_argument("--milestone-id")
    income.add_argument("--fee", default="0")
    income.add_argument("--pending", action="store_true")
    income.add_argument("--note", default="")
    income.add_argument("--date", dest="booked_on")
    income.add_argument("--currency")
    expense = commands.add_parser("expense", help="log an actual living or operating payment")
    expense.add_argument("entry_id")
    expense.add_argument("amount")
    expense.add_argument("--note", required=True)
    expense.add_argument("--category", choices=("living", "operating"), default="living")
    expense.add_argument("--gig-id")
    expense.add_argument("--date", dest="booked_on")
    expense.add_argument("--allow-over-ceiling", action="store_true")
    expense.add_argument("--currency")
    settle = commands.add_parser("settle", help="settle a pending receipt exactly once")
    settle.add_argument("entry_id")
    settle.add_argument("--date", dest="settled_on")
    gig = commands.add_parser("close-gig", help="mark a gig closed and track its cash contribution")
    gig.add_argument("gig_id")
    gig.add_argument("--date", dest="closed_on")
    gig.add_argument("--note", default="")
    report = commands.add_parser("report", help="export an audit snapshot")
    report.add_argument("--as-of")
    report.add_argument("--format", choices=("markdown", "json"), default="json")
    report.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    values = vars(args).copy()
    command = values.pop("command")
    db_path = values.pop("db")
    try:
        config = None
        if command == "init":
            if values["start_date"] is None:
                values.pop("start_date")
            config = LedgerConfig(**values)
        with EmergencyLiquidityLedger(db_path, config) as ledger:
            if command == "init":
                result = asdict(ledger.config)
            elif command == "income":
                values["status"] = "pending" if values.pop("pending") else "settled"
                result = ledger.record_income(**values)
            elif command == "expense":
                result = ledger.log_expense(**values)
            elif command == "settle":
                result = ledger.settle_income(**values)
            elif command == "close-gig":
                result = ledger.close_gig(**values)
            else:
                snapshot = ledger.snapshot(args.as_of)
                content = (render_markdown(snapshot) if args.format == "markdown" else
                           json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n")
                if args.output:
                    args.output.write_text(content, encoding="utf-8")
                else:
                    print(content, end="")
                return 0
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
