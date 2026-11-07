#!/usr/bin/env python3
"""Durable billing reconciliation and quota repair using only the standard library.

The SQLite quota ledger is the authoritative entitlement store. Integrations must
authenticate provider webhooks before calling ``ingest_webhook``. An event is a
JSON object with event_id, order_id, customer_id, quota_units (a positive integer),
paid_at (Unix seconds), and optional event_type="payment.succeeded". Provider
adapters should normalize their payloads to this schema. Reusing an event ID with
different content is rejected; different event IDs for one order cannot duplicate
quota. Order customer and quota are immutable.

Payment facts and a queue entry commit before fulfillment starts. Quota writes
and queue transitions are transactional, including rollback of partial failures.
Retries use capped exponential delays; max_attempts includes the initial attempt.
Exhausted or permanent failures remain in the dead-letter queue. Duplicate
deliveries never reset the retry budget or bypass backoff.

An audit runs once per UTC hour, even across process restarts or overlapping cron
workers. It compensates only the outstanding quota of paid orders older than the
grace period, including dead letters, and settles their queue entries. Supplying
an authoritative paid-order snapshot also repairs missed webhooks. Snapshots are
additive: absence never cancels an order. Each snapshot row contains order_id,
customer_id, quota_units, and paid_at. Individual compensation failures are
reported and retried at the next hourly audit.

Example cron entries (use absolute paths for both script and database)::

    * * * * * /usr/bin/python3 /srv/app/scripts/zero_support_reconciliation_engine.py --db /srv/app/var/billing.sqlite3 retry
    0 * * * * /usr/bin/python3 /srv/app/scripts/zero_support_reconciliation_engine.py --db /srv/app/var/billing.sqlite3 audit --orders /srv/app/var/paid-orders.json

Omit --orders to audit only already-known payments. The snapshot must be refreshed
by the billing integration. This module does not install or modify cron jobs.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different operation."""


class PermanentReconciliationError(Exception):
    """A processing error that must go directly to the dead-letter queue."""


class OrderConflict(ValueError):
    """An existing order's immutable customer or quota does not match."""


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _units(value: Any, name: str = "quota_units") -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError(f"{name} must be a positive SQLite integer")
    return value


def _seconds(value: Any, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    return result


@dataclass(frozen=True)
class RetryPolicy:
    base_delay: float = 30.0
    max_delay: float = 3600.0
    max_attempts: int = 5

    def __post_init__(self) -> None:
        base = _seconds(self.base_delay, "base_delay")
        cap = _seconds(self.max_delay, "max_delay")
        if base == 0 or cap < base:
            raise ValueError("0 < base_delay <= max_delay is required")
        _units(self.max_attempts, "max_attempts")

    def delay(self, failed_attempts: int) -> float:
        """First failure waits base_delay; subsequent failures double the wait."""
        _units(failed_attempts, "failed_attempts")
        exponent = failed_attempts - 1
        if exponent >= math.log2(self.max_delay) - math.log2(self.base_delay):
            return float(self.max_delay)
        return min(float(self.max_delay), math.ldexp(self.base_delay, exponent))


@dataclass(frozen=True)
class PaidOrder:
    order_id: str
    customer_id: str
    quota_units: int
    paid_at: float

    def __post_init__(self) -> None:
        _identifier(self.order_id, "order_id")
        _identifier(self.customer_id, "customer_id")
        _units(self.quota_units)
        object.__setattr__(self, "paid_at", _seconds(self.paid_at, "paid_at"))


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str
    order_id: str
    customer_id: str
    quota_units: int
    paid_at: float
    event_type: str = "payment.succeeded"

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        order = PaidOrder(self.order_id, self.customer_id, self.quota_units, self.paid_at)
        object.__setattr__(self, "paid_at", order.paid_at)
        if self.event_type != "payment.succeeded":
            raise ValueError("only payment.succeeded events are supported")


def _model(value: Any, model: type) -> Any:
    if isinstance(value, model):
        return value
    if not isinstance(value, Mapping):
        raise ValueError(f"expected a {model.__name__} or JSON object")
    try:
        return model(**value)
    except TypeError as exc:
        raise ValueError(f"invalid {model.__name__} fields: {exc}") from exc


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    quota_units: int
    paid_at: float | None
    credited_units: int

    @property
    def outstanding_units(self) -> int:
        return self.quota_units - self.credited_units

    @property
    def status(self) -> str:
        if self.paid_at is None:
            return "pending"
        return "fulfilled" if self.outstanding_units == 0 else "paid"


@dataclass(frozen=True)
class EventResult:
    event_id: str
    status: str
    attempts: int
    next_attempt_at: float | None
    last_error: str | None
    duplicate: bool = False


@dataclass(frozen=True)
class QuotaGrant:
    grant_id: str
    order_id: str
    quota_units: int
    reason: str
    created_at: float


@dataclass(frozen=True)
class AuditFailure:
    order_id: str
    error: str


@dataclass(frozen=True)
class AuditResult:
    hour: int
    scanned_orders: int
    compensated_orders: int
    quota_units: int
    failures: tuple[AuditFailure, ...] = ()
    skipped: bool = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    quota_units INTEGER NOT NULL CHECK (quota_units > 0),
    paid_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    payload TEXT NOT NULL,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS retry_queue (
    event_id TEXT PRIMARY KEY REFERENCES webhook_events(event_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'retry', 'dead', 'processed', 'reconciled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS retry_due ON retry_queue(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS quota_ledger (
    grant_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    quota_units INTEGER NOT NULL CHECK (quota_units > 0),
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_order ON quota_ledger(order_id);
CREATE INDEX IF NOT EXISTS orders_customer ON orders(customer_id);
CREATE TABLE IF NOT EXISTS audit_runs (
    hour INTEGER PRIMARY KEY,
    completed_at REAL NOT NULL,
    report TEXT NOT NULL
);
"""


class ZeroSupportReconciliationEngine:
    """SQLite-backed, thread-safe engine; separate processes share the same DB.

    SQLite's write transaction serializes grants and queue claims across workers.
    All entitlement effects are local to this database: an external quota service
    would need its own idempotent adapter/outbox, not a side effect in a DB hook.
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        retry_policy: RetryPolicy | None = None,
        compensation_grace_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.retry_policy = retry_policy or RetryPolicy()
        self.compensation_grace_seconds = _seconds(
            compensation_grace_seconds, "compensation_grace_seconds"
        )
        self.clock = clock
        self._lock = threading.RLock()
        path = str(db_path)
        if path != ":memory:":
            path = str(Path(path).expanduser())
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(_SCHEMA)

    def __enter__(self) -> ZeroSupportReconciliationEngine:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _now(self) -> float:
        return _seconds(self.clock(), "clock")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _register_order(
        self, order_id: str, customer_id: str, quota_units: int,
        paid_at: float | None, now: float,
    ) -> None:
        row = self._db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            self._db.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
                             (order_id, customer_id, quota_units, paid_at, now))
        else:
            if row["customer_id"] != customer_id or row["quota_units"] != quota_units:
                raise OrderConflict(f"order {order_id!r} has different customer or quota")
            # Preserve the first recorded payment time, so duplicates cannot delay repair.
            if row["paid_at"] is None and paid_at is not None:
                self._db.execute("UPDATE orders SET paid_at = ? WHERE order_id = ?",
                                 (paid_at, order_id))

    def register_order(
        self, order_id: str, customer_id: str, quota_units: int, *, paid_at: float | None = None,
    ) -> Order:
        """Idempotently record an order/payment without issuing quota."""
        _identifier(order_id, "order_id")
        _identifier(customer_id, "customer_id")
        _units(quota_units)
        if paid_at is not None:
            paid_at = _seconds(paid_at, "paid_at")
        with self._transaction():
            self._register_order(order_id, customer_id, quota_units, paid_at, self._now())
            return self._get_order(order_id)

    def _get_order(self, order_id: str) -> Order:
        row = self._db.execute("""
            SELECT o.order_id, o.customer_id, o.quota_units, o.paid_at,
                   COALESCE(SUM(l.quota_units), 0) AS credited_units
            FROM orders o LEFT JOIN quota_ledger l ON l.order_id = o.order_id
            WHERE o.order_id = ? GROUP BY o.order_id
        """, (order_id,)).fetchone()
        if row is None:
            raise KeyError(order_id)
        return Order(**dict(row))

    def get_order(self, order_id: str) -> Order:
        with self._lock:
            return self._get_order(order_id)

    def quota_balance(self, customer_id: str) -> int:
        """Total issued quota; consumption is outside this billing ledger."""
        with self._lock:
            return sum(row[0] for row in self._db.execute("""
                SELECT l.quota_units FROM quota_ledger l
                JOIN orders o ON o.order_id = l.order_id WHERE o.customer_id = ?
            """, (customer_id,)))

    def ledger_entries(self, order_id: str | None = None) -> list[QuotaGrant]:
        with self._lock:
            query = "SELECT * FROM quota_ledger"
            params = () if order_id is None else (order_id,)
            if order_id is not None:
                query += " WHERE order_id = ?"
            return [QuotaGrant(**dict(row)) for row in
                    self._db.execute(query + " ORDER BY created_at, grant_id", params)]

    def _credit_missing(self, order_id: str, reason: str, now: float) -> int:
        """Must run in the caller's transaction; never performs external I/O."""
        order = self._get_order(order_id)
        if order.paid_at is None:
            raise PermanentReconciliationError("cannot credit an unpaid order")
        missing = order.outstanding_units
        if missing:
            self._db.execute("INSERT INTO quota_ledger VALUES (?, ?, ?, ?, ?)",
                             (f"{reason}:{order_id}", order_id, missing, reason, now))
        return missing

    def record_fulfillment(self, order_id: str, quota_units: int, idempotency_key: str) -> bool:
        """Record partial/full delivery in this ledger; return False for a duplicate."""
        _units(quota_units)
        _identifier(idempotency_key, "idempotency_key")
        key = f"fulfillment:{idempotency_key}"
        with self._transaction():
            old = self._db.execute("SELECT * FROM quota_ledger WHERE grant_id = ?", (key,)).fetchone()
            if old is not None:
                if old["order_id"] != order_id or old["quota_units"] != quota_units:
                    raise IdempotencyConflict("fulfillment key reused with different content")
                return False
            order = self._get_order(order_id)
            if order.paid_at is None or quota_units > order.outstanding_units:
                raise ValueError("fulfillment requires a paid order and cannot exceed outstanding quota")
            self._db.execute("INSERT INTO quota_ledger VALUES (?, ?, ?, ?, ?)",
                             (key, order_id, quota_units, "fulfillment", self._now()))
            return True

    def _event_result(self, event_id: str, duplicate: bool = False) -> EventResult:
        row = self._db.execute("SELECT * FROM retry_queue WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return EventResult(**dict(row), duplicate=duplicate)

    def get_event(self, event_id: str) -> EventResult:
        with self._lock:
            return self._event_result(event_id)

    def dead_letters(self) -> list[EventResult]:
        with self._lock:
            return [EventResult(**dict(row)) for row in self._db.execute(
                "SELECT * FROM retry_queue WHERE status = 'dead' ORDER BY event_id"
            )]

    def ingest_webhook(
        self, event: WebhookEvent | Mapping[str, Any], *, process_immediately: bool = True,
    ) -> EventResult:
        event = _model(event, WebhookEvent)
        payload = json.dumps(asdict(event), sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._transaction():
            old = self._db.execute("SELECT payload FROM webhook_events WHERE event_id = ?",
                                   (event.event_id,)).fetchone()
            if old is not None:
                if old["payload"] != payload:
                    raise IdempotencyConflict(f"event ID {event.event_id!r} reused with different content")
                return self._event_result(event.event_id, duplicate=True)
            now = self._now()
            self._register_order(event.order_id, event.customer_id, event.quota_units, event.paid_at, now)
            self._db.execute("INSERT INTO webhook_events VALUES (?, ?, ?, ?)",
                             (event.event_id, event.order_id, payload, now))
            self._db.execute("""
                INSERT INTO retry_queue (event_id, status, next_attempt_at) VALUES (?, 'pending', ?)
            """, (event.event_id, now))
        if process_immediately:
            self._process_event(event.event_id)
        return self.get_event(event.event_id)

    def _process_event(self, event_id: str) -> EventResult | None:
        with self._transaction():
            entry = self._event_result(event_id)
            now = self._now()
            if entry.status not in {"pending", "retry"} or entry.next_attempt_at > now:
                return None
            attempts = entry.attempts + 1
            event = WebhookEvent(**json.loads(self._db.execute(
                "SELECT payload FROM webhook_events WHERE event_id = ?", (event_id,)
            ).fetchone()[0]))
            self._db.execute("SAVEPOINT fulfillment")
            try:
                self._credit_missing(event.order_id, "webhook", now)
            except Exception as exc:
                self._db.execute("ROLLBACK TO fulfillment")
                dead = isinstance(exc, PermanentReconciliationError) or attempts >= self.retry_policy.max_attempts
                next_at = None if dead else self._now() + self.retry_policy.delay(attempts)
                self._db.execute("""
                    UPDATE retry_queue SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ?
                    WHERE event_id = ?
                """, ("dead" if dead else "retry", attempts, next_at,
                      f"{type(exc).__name__}: {exc}", event_id))
            else:
                self._db.execute("""
                    UPDATE retry_queue SET status = 'processed', attempts = ?,
                        next_attempt_at = NULL, last_error = NULL WHERE event_id = ?
                """, (attempts, event_id))
            finally:
                self._db.execute("RELEASE fulfillment")
            return self._event_result(event_id)

    def retry_due(self, limit: int = 100) -> list[EventResult]:
        """Process a bounded batch of due entries; competing workers recheck each claim."""
        _units(limit, "limit")
        with self._lock:
            ids = [row[0] for row in self._db.execute("""
                SELECT event_id FROM retry_queue
                WHERE status IN ('pending', 'retry') AND next_attempt_at <= ?
                ORDER BY next_attempt_at, event_id LIMIT ?
            """, (self._now(), limit))]
        results = []
        for event_id in ids:
            result = self._process_event(event_id)
            if result is not None:
                results.append(result)
        return results

    def run_hourly_audit(
        self, paid_orders: Iterable[PaidOrder | Mapping[str, Any]] = (),
    ) -> AuditResult:
        """Compensate stale paid orders once per UTC hour; return a persisted report.

        Snapshot validation/conflicts roll back the entire run, allowing a corrected
        snapshot to retry in the same hour. Grant failures are isolated per order.
        """
        with self._transaction():
            now = self._now()
            hour = int(now // 3600)
            old = self._db.execute("SELECT report FROM audit_runs WHERE hour = ?", (hour,)).fetchone()
            if old is not None:
                report = json.loads(old["report"])
                report["failures"] = tuple(AuditFailure(**item) for item in report["failures"])
                return AuditResult(**report, skipped=True)
            for value in paid_orders:
                order = _model(value, PaidOrder)
                self._register_order(order.order_id, order.customer_id, order.quota_units, order.paid_at, now)
            scanned = self._db.execute("SELECT COUNT(*) FROM orders WHERE paid_at IS NOT NULL").fetchone()[0]
            eligible = [row[0] for row in self._db.execute("""
                SELECT o.order_id FROM orders o LEFT JOIN quota_ledger l ON l.order_id = o.order_id
                WHERE o.paid_at <= ? GROUP BY o.order_id
                HAVING o.quota_units > COALESCE(SUM(l.quota_units), 0) ORDER BY o.order_id
            """, (now - self.compensation_grace_seconds,))]
            compensated = quota = 0
            failures = []
            for order_id in eligible:
                self._db.execute("SAVEPOINT compensation")
                try:
                    credited = self._credit_missing(order_id, "compensation", now)
                except Exception as exc:
                    self._db.execute("ROLLBACK TO compensation")
                    failures.append(AuditFailure(order_id, f"{type(exc).__name__}: {exc}"))
                else:
                    compensated += int(credited > 0)
                    quota += credited
                finally:
                    self._db.execute("RELEASE compensation")
            # Healing settles all deliveries for fulfilled orders, even dead letters.
            self._db.execute("""
                UPDATE retry_queue SET status = 'reconciled', next_attempt_at = NULL, last_error = NULL
                WHERE status IN ('pending', 'retry', 'dead') AND event_id IN (
                    SELECT e.event_id FROM webhook_events e JOIN orders o ON o.order_id = e.order_id
                    WHERE o.quota_units = (
                        SELECT COALESCE(SUM(l.quota_units), 0) FROM quota_ledger l WHERE l.order_id = o.order_id
                    )
                )
            """)
            result = AuditResult(hour, scanned, compensated, quota, tuple(failures))
            report = asdict(result)
            report.pop("skipped")
            self._db.execute("INSERT INTO audit_runs VALUES (?, ?, ?)",
                             (hour, now, json.dumps(report, sort_keys=True)))
            return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="var/zero-support-reconciliation.sqlite3")
    parser.add_argument("--grace-seconds", type=float, default=300.0)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="ingest one normalized, authenticated webhook JSON file")
    ingest.add_argument("event_file", type=Path)
    ingest.add_argument("--enqueue-only", action="store_true")
    retry = commands.add_parser("retry", help="process due retry queue entries")
    retry.add_argument("--limit", type=int, default=100)
    audit = commands.add_parser("audit", help="run the hourly reconciliation audit")
    audit.add_argument("--orders", type=Path, help="JSON array of authoritative paid orders")
    args = parser.parse_args(argv)
    try:
        with ZeroSupportReconciliationEngine(args.db, compensation_grace_seconds=args.grace_seconds) as engine:
            if args.command == "ingest":
                event = json.loads(args.event_file.read_text(encoding="utf-8"))
                output = asdict(engine.ingest_webhook(event, process_immediately=not args.enqueue_only))
            elif args.command == "retry":
                output = [asdict(result) for result in engine.retry_due(args.limit)]
            else:
                orders = [] if args.orders is None else json.loads(args.orders.read_text(encoding="utf-8"))
                if not isinstance(orders, list):
                    raise ValueError("paid-order snapshot must be a JSON array")
                output = asdict(engine.run_hourly_audit(orders))
        print(json.dumps(output, sort_keys=True, allow_nan=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
