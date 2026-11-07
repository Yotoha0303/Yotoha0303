#!/usr/bin/env python3
"""Transactional B2B API key authentication, metering, and prepaid accounting.

This standard-library module is a gateway core that can be embedded in an HTTP
service. Administrative methods (registration, credit, revocation, and reports)
must only be exposed to trusted callers. ``debit`` authenticates a raw API key
and atomically admits one request, charges its token count, and appends a ledger
entry. Call it before dispatching work with a known token budget. Credits add
balance; they do not erase historical usage or release RPM/TPM capacity.

Example::

    with B2BAPIGateway(SQLiteStore("metering.sqlite3")) as gateway:
        secret = generate_api_key()  # Deliver once; only its SHA-256 is stored.
        key = gateway.register_key(secret, initial_balance=10000)
        receipt = gateway.debit(secret, 250, request_id="client-request-1")
        gateway.credit(key.key_id, 1000, operation_id="invoice-123")

Request IDs and credit operation IDs are independently scoped to each key.
Repeating an operation with the same ID and amount returns the original receipt;
changing its amount raises IdempotencyConflict. IDs survive SQLite restarts.
Zero-token requests consume RPM. The sliding window is exactly (now - 60, now].
SQLite uses epoch time, BEGIN IMMEDIATE, and an indexed, append-only ledger so
independent gateway connections share balances, limits, and idempotency state.
InMemoryStore is thread safe within a process; SQLiteStore also coordinates
separate processes. Per-key timestamps never decrease when the clock moves back.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Iterator, Protocol


MAX_TOKENS = 2**63 - 1
WINDOW_SECONDS = 60.0


def _integer(value: int, name: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_TOKENS:
        raise ValueError(f"{name} must be an integer from {minimum} to {MAX_TOKENS}")
    return value


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def hash_api_key(api_key: str) -> str:
    """Return a SHA-256 hex digest without trimming or normalizing the secret."""
    _identifier(api_key, "api_key")
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """Generate a high-entropy key suitable for unsalted SHA-256 storage."""
    return "b2b_" + secrets.token_urlsafe(32)


class AuthenticationError(Exception):
    """The API key is missing, unknown, or revoked."""


class InsufficientBalance(Exception):
    """A debit would overdraw the prepaid token balance."""


class IdempotencyConflict(Exception):
    """An operation ID was already used with a different token amount."""


class RateLimitExceeded(Exception):
    """RPM and/or TPM exceeded; retry_after is seconds, or None if too large."""

    def __init__(self, limits: Iterable[str], retry_after: float | None):
        self.limits = tuple(limits)
        self.retry_after = retry_after
        super().__init__("sliding-window rate limit exceeded: " + ", ".join(self.limits))


@dataclass(frozen=True)
class APIKeyRecord:
    key_id: str
    key_hash: str
    rpm_limit: int
    tpm_limit: int
    balance: int
    active: bool
    updated_at: float


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    key_id: str
    kind: str
    operation_id: str
    tokens: int
    timestamp: float
    balance_after: int

    @property
    def delta(self) -> int:
        return -self.tokens if self.kind == "debit" else self.tokens


@dataclass(frozen=True)
class UsageSnapshot:
    key_id: str
    balance: int
    total_credited: int
    total_debited: int
    total_requests: int
    requests_in_window: int
    tokens_in_window: int
    rpm_remaining: int
    tpm_remaining: int

    @property
    def burn_rate_tps(self) -> float:
        """Average token consumption per second over the full last minute."""
        return self.tokens_in_window / WINDOW_SECONDS

    @property
    def seconds_to_exhaustion(self) -> float | None:
        return self.balance / self.burn_rate_tps if self.burn_rate_tps else None


class SlidingWindowRateLimiter:
    """Pure admission policy; the gateway persists admitted events atomically."""

    def check(
        self,
        events: Iterable[tuple[float, int]],
        *,
        now: float,
        tokens: int,
        rpm_limit: int,
        tpm_limit: int,
    ) -> None:
        _integer(tokens, "tokens")
        _integer(rpm_limit, "rpm_limit", 1)
        _integer(tpm_limit, "tpm_limit", 1)
        active = sorted(event for event in events if event[0] > now - WINDOW_SECONDS)
        limits = []
        waits = []
        if len(active) >= rpm_limit:
            limits.append("rpm")
            # Enough old requests must expire to leave room for one more.
            waits.append(active[len(active) - rpm_limit][0] + WINDOW_SECONDS - now)
        if sum(count for _, count in active) + tokens > tpm_limit:
            limits.append("tpm")
            if tokens > tpm_limit:
                raise RateLimitExceeded(limits, None)
            excess = sum(count for _, count in active) + tokens - tpm_limit
            for timestamp, count in active:
                excess -= count
                if excess <= 0:
                    waits.append(timestamp + WINDOW_SECONDS - now)
                    break
        if limits:
            raise RateLimitExceeded(limits, max(waits))


class MeteringStore(Protocol):
    """Storage operations are private and must run inside transaction()."""

    def transaction(self): ...
    def _key_by_id(self, key_id: str) -> APIKeyRecord | None: ...
    def _key_by_hash(self, key_hash: str) -> APIKeyRecord | None: ...
    def _insert_key(self, key: APIKeyRecord) -> None: ...
    def _update_key(self, key: APIKeyRecord) -> None: ...
    def _append_entry(self, entry: LedgerEntry) -> None: ...
    def _entry(self, key_id: str, kind: str, operation_id: str) -> LedgerEntry | None: ...
    def _entries(self, key_id: str, since: float | None = None,
                 kind: str | None = None) -> list[LedgerEntry]: ...
    def close(self) -> None: ...


class InMemoryStore:
    """Process-local storage with rollback and a lock covering each transaction."""

    def __init__(self):
        self._keys: dict[str, APIKeyRecord] = {}
        self._hashes: dict[str, str] = {}
        self._ledger: dict[tuple[str, str, str], LedgerEntry] = {}
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[InMemoryStore]:
        with self._lock:
            # Values are frozen records, so shallow copies are sufficient.
            original = self._keys.copy(), self._hashes.copy(), self._ledger.copy()
            try:
                yield self
            except BaseException:
                self._keys, self._hashes, self._ledger = original
                raise

    def _key_by_id(self, key_id):
        return self._keys.get(key_id)

    def _key_by_hash(self, key_hash):
        key_id = self._hashes.get(key_hash)
        return self._keys.get(key_id)

    def _insert_key(self, key):
        if key.key_id in self._keys or key.key_hash in self._hashes:
            raise ValueError("key ID or API key is already registered")
        self._keys[key.key_id] = key
        self._hashes[key.key_hash] = key.key_id

    def _update_key(self, key):
        self._keys[key.key_id] = key

    def _append_entry(self, entry):
        identity = entry.key_id, entry.kind, entry.operation_id
        if identity in self._ledger:
            raise ValueError("duplicate ledger operation")
        self._ledger[identity] = entry

    def _entry(self, key_id, kind, operation_id):
        return self._ledger.get((key_id, kind, operation_id))

    def _entries(self, key_id, since=None, kind=None):
        return [entry for entry in self._ledger.values()
                if entry.key_id == key_id
                and (since is None or entry.timestamp > since)
                and (kind is None or entry.kind == kind)]

    def close(self):
        pass


class SQLiteStore:
    """Durable, multi-connection accounting with SQLite writer serialization."""

    def __init__(self, path: str = ":memory:"):
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            path, timeout=30, isolation_level=None, check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id TEXT PRIMARY KEY,
                key_hash TEXT NOT NULL UNIQUE,
                rpm_limit INTEGER NOT NULL CHECK (rpm_limit > 0),
                tpm_limit INTEGER NOT NULL CHECK (tpm_limit > 0),
                balance INTEGER NOT NULL CHECK (balance >= 0),
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage_ledger (
                entry_id TEXT PRIMARY KEY,
                key_id TEXT NOT NULL REFERENCES api_keys(key_id),
                kind TEXT NOT NULL CHECK (kind IN ('opening', 'credit', 'debit')),
                operation_id TEXT NOT NULL,
                tokens INTEGER NOT NULL CHECK (tokens >= 0),
                timestamp REAL NOT NULL,
                balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
                UNIQUE (key_id, kind, operation_id)
            );
            CREATE INDEX IF NOT EXISTS ledger_window
                ON usage_ledger(key_id, kind, timestamp);
        """)

    @contextmanager
    def transaction(self) -> Iterator[SQLiteStore]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    @staticmethod
    def _record(row):
        if row is None:
            return None
        values = dict(row)
        values["active"] = bool(values["active"])
        return APIKeyRecord(**values)

    def _key_by_id(self, key_id):
        return self._record(self._connection.execute(
            "SELECT * FROM api_keys WHERE key_id = ?", (key_id,),
        ).fetchone())

    def _key_by_hash(self, key_hash):
        return self._record(self._connection.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,),
        ).fetchone())

    def _insert_key(self, key):
        self._connection.execute(
            "INSERT INTO api_keys VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key.key_id, key.key_hash, key.rpm_limit, key.tpm_limit,
             key.balance, key.active, key.updated_at),
        )

    def _update_key(self, key):
        self._connection.execute(
            "UPDATE api_keys SET balance = ?, active = ?, updated_at = ? WHERE key_id = ?",
            (key.balance, key.active, key.updated_at, key.key_id),
        )

    def _append_entry(self, entry):
        self._connection.execute(
            "INSERT INTO usage_ledger VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry.entry_id, entry.key_id, entry.kind, entry.operation_id,
             entry.tokens, entry.timestamp, entry.balance_after),
        )

    def _entry(self, key_id, kind, operation_id):
        row = self._connection.execute(
            "SELECT * FROM usage_ledger WHERE key_id = ? AND kind = ? AND operation_id = ?",
            (key_id, kind, operation_id),
        ).fetchone()
        return LedgerEntry(**dict(row)) if row is not None else None

    def _entries(self, key_id, since=None, kind=None):
        query = "SELECT * FROM usage_ledger WHERE key_id = ?"
        parameters = [key_id]
        if since is not None:
            query += " AND timestamp > ?"
            parameters.append(since)
        if kind is not None:
            query += " AND kind = ?"
            parameters.append(kind)
        return [LedgerEntry(**dict(row)) for row in self._connection.execute(
            query + " ORDER BY rowid", parameters,
        )]

    def close(self):
        with self._lock:
            self._connection.close()


class B2BAPIGateway:
    """Authenticate and meter prepaid requests using either persistence backend."""

    def __init__(self, store: MeteringStore | None = None,
                 time_func: Callable[[], float] = time.time):
        self.store = store if store is not None else InMemoryStore()
        self.time_func = time_func
        self.rate_limiter = SlidingWindowRateLimiter()

    def _now(self, key: APIKeyRecord | None = None) -> float:
        value = self.time_func()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("clock must return a finite, non-negative timestamp")
        now = float(value)
        if not math.isfinite(now) or now < 0:
            raise ValueError("clock must return a finite, non-negative timestamp")
        return max(now, key.updated_at) if key else now

    def _require_key(self, key_id: str) -> APIKeyRecord:
        _identifier(key_id, "key_id")
        key = self.store._key_by_id(key_id)
        if key is None:
            raise KeyError("unknown key ID")
        return key

    def _authenticate(self, api_key: str) -> APIKeyRecord:
        try:
            digest = hash_api_key(api_key)
        except (ValueError, UnicodeError):
            raise AuthenticationError("invalid API key") from None
        key = self.store._key_by_hash(digest)
        if key is None or not hmac.compare_digest(key.key_hash, digest) or not key.active:
            raise AuthenticationError("invalid API key")
        return key

    def register_key(self, api_key: str, *, key_id: str | None = None,
                     initial_balance: int = 0, rpm_limit: int = 60,
                     tpm_limit: int = 60000) -> APIKeyRecord:
        """Register a secret once; duplicate secrets and IDs are rejected."""
        digest = hash_api_key(api_key)
        key_id = uuid.uuid4().hex if key_id is None else _identifier(key_id, "key_id")
        _integer(initial_balance, "initial_balance")
        _integer(rpm_limit, "rpm_limit", 1)
        _integer(tpm_limit, "tpm_limit", 1)
        with self.store.transaction():
            if self.store._key_by_id(key_id) or self.store._key_by_hash(digest):
                raise ValueError("key ID or API key is already registered")
            now = self._now()
            key = APIKeyRecord(key_id, digest, rpm_limit, tpm_limit,
                               initial_balance, True, now)
            self.store._insert_key(key)
            self.store._append_entry(LedgerEntry(
                uuid.uuid4().hex, key_id, "opening", "opening", initial_balance,
                now, initial_balance,
            ))
            return key

    def authenticate(self, api_key: str) -> APIKeyRecord:
        with self.store.transaction():
            return self._authenticate(api_key)

    def revoke_key(self, key_id: str) -> None:
        with self.store.transaction():
            key = self._require_key(key_id)
            self.store._update_key(replace(key, active=False, updated_at=self._now(key)))

    def _replay(self, key_id: str, kind: str, operation_id: str,
                tokens: int) -> LedgerEntry | None:
        entry = self.store._entry(key_id, kind, operation_id)
        if entry is not None and entry.tokens != tokens:
            raise IdempotencyConflict("operation ID was already used with a different amount")
        return entry

    def _post(self, key: APIKeyRecord, kind: str, operation_id: str,
              tokens: int, now: float) -> LedgerEntry:
        balance = key.balance - tokens if kind == "debit" else key.balance + tokens
        if balance < 0:
            raise InsufficientBalance("insufficient prepaid token balance")
        _integer(balance, "resulting balance")
        entry = LedgerEntry(uuid.uuid4().hex, key.key_id, kind,
                            operation_id, tokens, now, balance)
        self.store._update_key(replace(key, balance=balance, updated_at=now))
        self.store._append_entry(entry)
        return entry

    def debit(self, api_key: str, tokens: int, *, request_id: str) -> LedgerEntry:
        """Admit one request and debit tokens, or leave all state unchanged.

        The token count must include all tokens to be charged (e.g. prompt and
        completion). Even retries must authenticate an active key. A successful
        replay returns the original receipt without consuming RPM or TPM again.
        """
        _integer(tokens, "tokens")
        _identifier(request_id, "request_id")
        with self.store.transaction():
            key = self._authenticate(api_key)
            replay = self._replay(key.key_id, "debit", request_id, tokens)
            if replay is not None:
                return replay
            now = self._now(key)
            events = self.store._entries(key.key_id, now - WINDOW_SECONDS, "debit")
            self.rate_limiter.check(
                ((entry.timestamp, entry.tokens) for entry in events),
                now=now, tokens=tokens, rpm_limit=key.rpm_limit,
                tpm_limit=key.tpm_limit,
            )
            return self._post(key, "debit", request_id, tokens, now)

    def credit(self, key_id: str, tokens: int, *, operation_id: str) -> LedgerEntry:
        """Administrative top-up/refund; does not change historical rate usage."""
        _integer(tokens, "tokens", 1)
        _identifier(operation_id, "operation_id")
        with self.store.transaction():
            key = self._require_key(key_id)
            replay = self._replay(key_id, "credit", operation_id, tokens)
            if replay is not None:
                return replay
            return self._post(key, "credit", operation_id, tokens, self._now(key))

    def ledger(self, key_id: str) -> list[LedgerEntry]:
        """Return immutable ledger entries in commit order, including opening funds."""
        with self.store.transaction():
            self._require_key(key_id)
            return self.store._entries(key_id)

    def usage(self, key_id: str) -> UsageSnapshot:
        """Return a consistent balance, lifetime ledger totals, and minute burndown."""
        with self.store.transaction():
            key = self._require_key(key_id)
            now = self._now(key)
            entries = self.store._entries(key_id)
            debits = [entry for entry in entries if entry.kind == "debit"]
            active = [entry for entry in debits if entry.timestamp > now - WINDOW_SECONDS]
            tokens = sum(entry.tokens for entry in active)
            return UsageSnapshot(
                key_id, key.balance,
                sum(entry.tokens for entry in entries if entry.kind != "debit"),
                sum(entry.tokens for entry in debits), len(debits), len(active), tokens,
                max(0, key.rpm_limit - len(active)), max(0, key.tpm_limit - tokens),
            )

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> B2BAPIGateway:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
