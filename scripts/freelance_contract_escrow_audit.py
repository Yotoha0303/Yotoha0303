#!/usr/bin/env python3
"""Standard-library freelance delivery, acceptance, and escrow evidence ledger.

Amounts are integer minor units (e.g. USD cents); timestamps are UTC Unix seconds.
Each contract has an independently exportable SHA-256 event chain in SQLite.
Milestone hashes bind the agreed criteria and price to the contract; delivery
hashes bind those terms to a sorted manifest of file names, sizes, and hashes.
Keep the original delivered files: manifests do not contain their contents.

Acceptance uses an expiring, single-use bearer token for one client and delivery.
Only its digest is persisted. Send the raw token through an authenticated client
channel and authenticate that client before calling log_client_acceptance.
Deposit evidence must come from a trusted provider adapter after authenticating
its response/webhook. This offline module validates that normalized evidence;
it does not contact payment providers or independently authenticate bank funds.
One deposit must cover the contract; snapshots are never added together.

Receipts describe delivery/acceptance, not payment. Packets include failures and
missing evidence as well as successful checks. Checksums detect alterations, not
identity or legal validity. Save ledger heads outside this database to detect
truncation or a fully rewritten chain; use --expected-head when verifying them.

Python API: create_contract -> add_milestone -> verify_escrow_deposit ->
record_delivery -> issue_acceptance_token -> log_client_acceptance ->
generate_receipt / generate_dispute_packet. Revisions invalidate older tokens;
accepted milestones are immutable. Separate connections serialize writes.

CLI (all output is JSON, suitable for saving as a receipt or defense packet)::

    python3 scripts/freelance_contract_escrow_audit.py --db ledger.sqlite3 record command.json
    python3 scripts/freelance_contract_escrow_audit.py --db ledger.sqlite3 packet contract-1
    python3 scripts/freelance_contract_escrow_audit.py verify-packet packet.json --expected-head HASH

record accepts {"action": "create_contract", "arguments": {"contract_id": "c1",
"client_id": "client", "developer_id": "developer", "currency": "USD",
"amount_minor": 10000, "terms": "Deliver the agreed application"}}. Its action
may be any of the six mutation methods listed above; arguments match the API.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import time
from typing import Any, Callable, Mapping


SCHEMA_VERSION = 1
GENESIS_SHA256 = "0" * 64
ESCROW_STATUSES = {"pending", "funded", "released", "refunded", "chargeback", "failed"}
EVENT_TYPES = {"contract_created", "milestone_added", "delivery_recorded",
               "acceptance_token_issued", "client_accepted", "escrow_checked"}


class LedgerIntegrityError(ValueError):
    """Evidence does not match its chain or an externally saved checkpoint."""


class StateError(ValueError):
    """An operation conflicts with the recorded contract state."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    value.encode("utf-8")
    return value


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError(f"{name} must be an integer from {minimum} to {2**63 - 1}")
    return value


def _seconds(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite nonnegative Unix seconds")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} is too large") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite nonnegative Unix seconds")
    return result


def _currency(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{3}", value) is None:
        raise ValueError("currency must be a three-letter uppercase code")
    return value


def _canonical(value: Any) -> bytes:
    """Canonical UTF-8 JSON; reject coercions such as non-string object keys."""
    def validate(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("expected JSON-compatible data")
    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def hash_milestone(milestone: Mapping[str, Any]) -> str:
    """Hash canonical milestone terms (omit the milestone_sha256 output field)."""
    if not isinstance(milestone, Mapping):
        raise ValueError("milestone must be a JSON object")
    return _digest({key: value for key, value in milestone.items() if key != "milestone_sha256"})


def _file_evidence(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"not a regular file: {path}")
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
    fingerprint = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                item.st_mtime_ns, item.st_ctime_ns)
    if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(current):
        raise ValueError(f"file changed while being hashed: {path}")
    return digest.hexdigest(), after.st_size


def hash_file(path: str | Path) -> str:
    """Stream a regular file into SHA-256 without loading it all into memory."""
    return _file_evidence(Path(path))[0]


def build_delivery_manifest(directory: str | Path) -> list[dict]:
    """Hash every file recursively, using relative POSIX names; reject symlinks."""
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("delivery directory must be a real directory")
    manifest = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError(f"symlinks are not allowed in deliveries: {path}")
        if path.is_dir():
            continue
        digest, size = _file_evidence(path)
        manifest.append({"path": path.relative_to(root).as_posix(),
                         "size_bytes": size, "sha256": digest})
    return manifest


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LedgerIntegrityError(message)


def _validate_chain(events: list[dict], expected_head: str | None = None) -> dict:
    _require(bool(events), "contract event chain is empty")
    previous, previous_time = GENESIS_SHA256, 0.0
    contract_id = events[0]["contract_id"]
    for index, event in enumerate(events, 1):
        _require(set(event) == {"contract_id", "sequence", "event_type", "recorded_at",
                               "payload", "previous_sha256", "event_sha256"}, "invalid event fields")
        _require(type(event["sequence"]) is int and event["sequence"] == index,
                 "event sequence is not contiguous")
        _require(event["contract_id"] == contract_id, "mixed contract event chains")
        _require(event["event_type"] in EVENT_TYPES, "unknown event type")
        _require(isinstance(event["payload"], dict), "invalid event payload")
        timestamp = _seconds(event["recorded_at"], "recorded_at")
        _require(timestamp >= previous_time, "event timestamps moved backwards")
        _require(event["previous_sha256"] == previous, "event chain link mismatch")
        body = {key: value for key, value in event.items() if key != "event_sha256"}
        _require(event["event_sha256"] == _digest(body), "event checksum mismatch")
        previous, previous_time = event["event_sha256"], timestamp
    if expected_head is not None:
        _require(previous == expected_head, "ledger does not match the saved head")
    return {"event_count": len(events), "head_sha256": previous}


def _state(events: list[dict]) -> dict:
    """Reconstruct immutable terms and delivery history from checked events."""
    state = {"contract": None, "milestones": {}, "escrow": None}
    for event in events:
        kind, payload = event["event_type"], event["payload"]
        if kind == "contract_created":
            _require(state["contract"] is None and event["sequence"] == 1,
                     "contract must be created exactly once at the start")
            _require(payload["contract_id"] == event["contract_id"], "contract ID mismatch")
            _require(payload["contract_sha256"] == _digest({
                key: value for key, value in payload.items() if key != "contract_sha256"
            }), "contract terms checksum mismatch")
            state["contract"] = payload
            continue
        _require(state["contract"] is not None, "missing contract creation")
        if kind == "escrow_checked":
            state["escrow"] = payload
            continue
        milestone_id = payload["milestone_id"]
        if kind == "milestone_added":
            _require(milestone_id not in state["milestones"], "duplicate milestone")
            _require(payload["contract_sha256"] == state["contract"]["contract_sha256"],
                     "milestone belongs to different contract terms")
            _require(payload["milestone_sha256"] == hash_milestone(payload),
                     "milestone terms checksum mismatch")
            state["milestones"][milestone_id] = {
                "terms": payload, "deliveries": [], "challenge": None, "acceptance": None,
            }
            continue
        milestone = state["milestones"][milestone_id]
        _require(milestone["acceptance"] is None, "accepted milestone was modified")
        if kind == "delivery_recorded":
            _require(payload["revision"] == len(milestone["deliveries"]) + 1,
                     "delivery revisions are not contiguous")
            _require(payload["milestone_sha256"] == milestone["terms"]["milestone_sha256"],
                     "delivery milestone terms mismatch")
            _require(payload["delivery_sha256"] == _digest({
                "milestone_sha256": payload["milestone_sha256"], "files": payload["files"]
            }), "delivery manifest checksum mismatch")
            milestone["deliveries"].append(payload)
            milestone["challenge"] = None
        elif kind == "acceptance_token_issued":
            _require(bool(milestone["deliveries"]) and payload["delivery_sha256"] ==
                     milestone["deliveries"][-1]["delivery_sha256"], "token delivery mismatch")
            milestone["challenge"] = payload
        elif kind == "client_accepted":
            challenge = milestone["challenge"]
            _require(challenge is not None, "acceptance has no issued token")
            _require(all(payload[key] == challenge[key] for key in
                         ("client_id", "delivery_sha256", "token_sha256")),
                     "acceptance does not match its token")
            _require(payload["accepted_at"] == event["recorded_at"] < challenge["expires_at"],
                     "acceptance token was expired")
            milestone["acceptance"] = payload
    return state


def _escrow_assessment(contract: dict, evidence: dict | None, now: float) -> dict:
    if evidence is None:
        return {"verified": False, "status": "missing", "reasons": ["no_deposit_evidence"],
                "evidence": None, "checked_at": now}
    checks = {
        "contract_mismatch": evidence["contract_id"] == contract["contract_id"],
        "client_mismatch": evidence["client_id"] == contract["client_id"],
        "currency_mismatch": evidence["currency"] == contract["currency"],
        "insufficient_deposit": evidence["amount_minor"] >= contract["amount_minor"],
        "funds_not_held": evidence["status"] == "funded",
        "stale_evidence": 0 <= now - evidence["observed_at"] <= contract["escrow_max_age_seconds"],
    }
    reasons = [reason for reason, passed in checks.items() if not passed]
    return {"verified": not reasons, "status": evidence["status"], "reasons": reasons,
            "evidence": evidence, "checked_at": now}


def _receipt(state: dict, milestone_id: str, ledger: dict, now: float) -> dict:
    milestone, contract = state["milestones"][milestone_id], state["contract"]
    if not milestone["deliveries"]:
        raise StateError("cannot issue a delivery receipt before delivery")
    receipt = {
        "schema_version": SCHEMA_VERSION, "type": "delivery_receipt",
        "contract_id": contract["contract_id"], "milestone_id": milestone_id,
        "client_id": contract["client_id"], "developer_id": contract["developer_id"],
        "currency": contract["currency"], "amount_minor": milestone["terms"]["amount_minor"],
        "milestone_sha256": milestone["terms"]["milestone_sha256"],
        "delivery": milestone["deliveries"][-1], "acceptance": milestone["acceptance"],
        "status": "accepted" if milestone["acceptance"] else "delivered",
        "escrow": _escrow_assessment(contract, state["escrow"], now),
        "issued_at": now, "ledger": ledger,
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def verify_receipt(receipt: Any, *, expected_sha256: str | None = None) -> bool:
    """Check a receipt checksum, optionally against an independently saved hash."""
    try:
        if receipt["schema_version"] != SCHEMA_VERSION or receipt["type"] != "delivery_receipt":
            return False
        digest = _digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})
        return receipt["receipt_sha256"] == digest and (expected_sha256 is None or digest == expected_sha256)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        return False


def _packet(events: list[dict], now: float) -> dict:
    ledger, state = _validate_chain(events), _state(events)
    contract, milestones = state["contract"], state["milestones"]
    escrow = _escrow_assessment(contract, state["escrow"], now)
    warnings = [] if escrow["verified"] else ["escrow_not_verified"]
    if not milestones:
        warnings.append("no_milestones")
    if sum(m["terms"]["amount_minor"] for m in milestones.values()) != contract["amount_minor"]:
        warnings.append("milestone_allocation_incomplete")
    receipts = []
    for milestone_id, milestone in milestones.items():
        if not milestone["deliveries"]:
            warnings.append(f"undelivered:{milestone_id}")
        else:
            receipts.append(_receipt(state, milestone_id, ledger, now))
        if not milestone["acceptance"]:
            warnings.append(f"unaccepted:{milestone_id}")
    packet = {
        "schema_version": SCHEMA_VERSION, "type": "dispute_defense_packet",
        "contract_id": contract["contract_id"], "generated_at": now, "ledger": ledger,
        "contract": contract,
        "milestones": [{key: value for key, value in milestone.items() if key != "challenge"}
                       for milestone in milestones.values()],
        "escrow": escrow, "warnings": warnings, "receipts": receipts, "events": events,
    }
    packet["packet_sha256"] = _digest(packet)
    return packet


def verify_dispute_packet(packet: Any, *, expected_head: str | None = None) -> bool:
    """Rebuild the packet from its chain, checking summaries, receipts, and hashes."""
    try:
        _validate_chain(packet["events"], expected_head)
        now = _seconds(packet["generated_at"], "generated_at")
        _require(now >= packet["events"][-1]["recorded_at"], "packet predates its evidence")
        return _canonical(packet) == _canonical(_packet(packet["events"], now))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, RecursionError):
        return False


class FreelanceContractEscrowAudit:
    """Durable ledger. Use one instance per thread; independent instances share a DB."""

    def __init__(self, db_path: str | Path = ":memory:", *, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._db = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("""CREATE TABLE IF NOT EXISTS escrow_events (
            contract_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL, recorded_at REAL NOT NULL, payload TEXT NOT NULL,
            previous_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL,
            PRIMARY KEY (contract_id, sequence))""")

    def close(self) -> None:
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _write(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def _events(self, contract_id: str) -> list[dict]:
        _text(contract_id, "contract_id")
        rows = self._db.execute("SELECT * FROM escrow_events WHERE contract_id = ? ORDER BY sequence",
                                (contract_id,)).fetchall()
        if not rows:
            raise KeyError(f"unknown contract: {contract_id}")
        try:
            events = [dict(row) for row in rows]
            for event in events:
                event["payload"] = json.loads(event["payload"])
            _validate_chain(events)
            _state(events)
            return events
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise LedgerIntegrityError(f"invalid ledger for {contract_id}: {exc}") from exc

    def _now(self, events: list[dict]) -> float:
        now = _seconds(self.clock(), "clock")
        if events and now < events[-1]["recorded_at"]:
            raise StateError("clock moved backwards behind the latest ledger event")
        return now

    def _append(self, contract_id: str, events: list[dict], kind: str, payload: dict, now: float) -> None:
        event = {"contract_id": contract_id, "sequence": len(events) + 1,
                 "event_type": kind, "recorded_at": now, "payload": payload,
                 "previous_sha256": events[-1]["event_sha256"] if events else GENESIS_SHA256}
        event["event_sha256"] = _digest(event)
        self._db.execute("INSERT INTO escrow_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (contract_id, event["sequence"], kind, now, _canonical(payload).decode("utf-8"),
                          event["previous_sha256"], event["event_sha256"]))

    def create_contract(self, contract_id: str, client_id: str, developer_id: str,
                        currency: str, amount_minor: int, terms: str, *,
                        escrow_max_age_seconds: int = 86400) -> dict:
        contract = {"contract_id": _text(contract_id, "contract_id"),
                    "client_id": _text(client_id, "client_id"),
                    "developer_id": _text(developer_id, "developer_id"),
                    "currency": _currency(currency), "amount_minor": _integer(amount_minor, "amount_minor", 1),
                    "terms": _text(terms, "terms"),
                    "escrow_max_age_seconds": _integer(escrow_max_age_seconds, "escrow_max_age_seconds", 1)}
        contract["contract_sha256"] = _digest(contract)
        with self._write():
            if self._db.execute("SELECT 1 FROM escrow_events WHERE contract_id = ?", (contract_id,)).fetchone():
                raise StateError("contract already exists")
            self._append(contract_id, [], "contract_created", contract, self._now([]))
        return contract

    def add_milestone(self, contract_id: str, milestone_id: str, title: str,
                      amount_minor: int, acceptance_criteria: str) -> dict:
        _text(milestone_id, "milestone_id")
        _text(title, "title")
        _text(acceptance_criteria, "acceptance_criteria")
        _integer(amount_minor, "amount_minor", 1)
        with self._write():
            events = self._events(contract_id)
            state = _state(events)
            if milestone_id in state["milestones"]:
                raise StateError("milestone already exists")
            allocated = sum(m["terms"]["amount_minor"] for m in state["milestones"].values())
            if allocated + amount_minor > state["contract"]["amount_minor"]:
                raise StateError("milestones exceed the contracted amount")
            milestone = {"milestone_id": milestone_id, "title": title, "amount_minor": amount_minor,
                         "acceptance_criteria": acceptance_criteria,
                         "contract_sha256": state["contract"]["contract_sha256"]}
            milestone["milestone_sha256"] = hash_milestone(milestone)
            self._append(contract_id, events, "milestone_added", milestone, self._now(events))
        return milestone

    @staticmethod
    def _milestone(state: dict, milestone_id: str) -> dict:
        _text(milestone_id, "milestone_id")
        if milestone_id not in state["milestones"]:
            raise KeyError(f"unknown milestone: {milestone_id}")
        return state["milestones"][milestone_id]

    def record_delivery(self, contract_id: str, milestone_id: str, directory: str | Path, *,
                        note: str = "") -> dict:
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        files = build_delivery_manifest(directory)
        if not files:
            raise ValueError("delivery must contain at least one regular file")
        with self._write():
            events = self._events(contract_id)
            milestone = self._milestone(_state(events), milestone_id)
            if milestone["acceptance"]:
                raise StateError("accepted milestones cannot be revised")
            delivery = {"milestone_id": milestone_id, "revision": len(milestone["deliveries"]) + 1,
                        "milestone_sha256": milestone["terms"]["milestone_sha256"], "files": files,
                        "note": note}
            delivery["delivery_sha256"] = _digest({
                "milestone_sha256": delivery["milestone_sha256"], "files": files,
            })
            self._append(contract_id, events, "delivery_recorded", delivery, self._now(events))
        return delivery

    def verify_delivery(self, contract_id: str, milestone_id: str, directory: str | Path) -> bool:
        """Compare current files to the latest recorded delivery, including extra files."""
        milestone = self._milestone(_state(self._events(contract_id)), milestone_id)
        if not milestone["deliveries"]:
            raise StateError("milestone has not been delivered")
        return build_delivery_manifest(directory) == milestone["deliveries"][-1]["files"]

    def issue_acceptance_token(self, contract_id: str, milestone_id: str, *,
                               ttl_seconds: int = 86400) -> dict:
        _integer(ttl_seconds, "ttl_seconds", 1)
        with self._write():
            events = self._events(contract_id)
            state, now = _state(events), self._now(events)
            milestone = self._milestone(state, milestone_id)
            if not milestone["deliveries"] or milestone["acceptance"]:
                raise StateError("token requires a delivered, unaccepted milestone")
            token = secrets.token_urlsafe(32)
            challenge = {"milestone_id": milestone_id, "client_id": state["contract"]["client_id"],
                         "delivery_sha256": milestone["deliveries"][-1]["delivery_sha256"],
                         "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                         "expires_at": now + ttl_seconds}
            if not math.isfinite(challenge["expires_at"]) or challenge["expires_at"] <= now:
                raise ValueError("token expiry is out of range")
            self._append(contract_id, events, "acceptance_token_issued", challenge, now)
        return {"token": token, "expires_at": challenge["expires_at"],
                "delivery_sha256": challenge["delivery_sha256"]}

    def log_client_acceptance(self, contract_id: str, milestone_id: str, client_id: str,
                              token: str, delivery_sha256: str, *,
                              statement: str = "I accept this delivery against the agreed milestone criteria.") -> dict:
        for value, name in ((client_id, "client_id"), (token, "token"),
                            (delivery_sha256, "delivery_sha256"), (statement, "statement")):
            _text(value, name)
        with self._write():
            events = self._events(contract_id)
            state, now = _state(events), self._now(events)
            milestone = self._milestone(state, milestone_id)
            challenge = milestone["challenge"]
            if milestone["acceptance"]:
                raise StateError("milestone has already been accepted; token is single-use")
            if (challenge is None or now >= challenge["expires_at"] or
                    client_id != state["contract"]["client_id"] or
                    delivery_sha256 != challenge["delivery_sha256"] or
                    not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), challenge["token_sha256"])):
                raise StateError("invalid, expired, superseded, or incorrectly scoped acceptance token")
            acceptance = {"milestone_id": milestone_id, "client_id": client_id,
                          "delivery_sha256": delivery_sha256, "token_sha256": challenge["token_sha256"],
                          "statement": statement, "accepted_at": now}
            self._append(contract_id, events, "client_accepted", acceptance, now)
        return acceptance

    def verify_escrow_deposit(self, contract_id: str, evidence: Mapping[str, Any]) -> dict:
        """Record an authenticated provider snapshot, including failed business checks.

        Required fields: provider, deposit_id, contract_id, client_id, currency,
        amount_minor, status, observed_at. Older observations cannot replace newer
        ones. A conflicting snapshot at the same observation time is rejected.
        """
        fields = {"provider", "deposit_id", "contract_id", "client_id", "currency",
                  "amount_minor", "status", "observed_at"}
        if not isinstance(evidence, Mapping) or set(evidence) != fields:
            raise ValueError("deposit evidence must contain exactly the documented fields")
        evidence = dict(evidence)
        for name in ("provider", "deposit_id", "contract_id", "client_id"):
            _text(evidence[name], name)
        _currency(evidence["currency"])
        _integer(evidence["amount_minor"], "amount_minor")
        if not isinstance(evidence["status"], str) or evidence["status"] not in ESCROW_STATUSES:
            raise ValueError("unknown escrow status")
        evidence["observed_at"] = _seconds(evidence["observed_at"], "observed_at")
        with self._write():
            events = self._events(contract_id)
            state, now = _state(events), self._now(events)
            if evidence["observed_at"] > now:
                raise ValueError("deposit observation cannot be in the future")
            previous = state["escrow"]
            if previous and (evidence["observed_at"] < previous["observed_at"] or
                             (evidence["observed_at"] == previous["observed_at"] and evidence != previous)):
                raise StateError("deposit evidence is older than, or conflicts with, the latest snapshot")
            self._append(contract_id, events, "escrow_checked", evidence, now)
            return _escrow_assessment(state["contract"], evidence, now)

    def verify_ledger(self, contract_id: str, *, expected_head: str | None = None) -> dict:
        return _validate_chain(self._events(contract_id), expected_head)

    def generate_receipt(self, contract_id: str, milestone_id: str) -> dict:
        events = self._events(contract_id)
        state = _state(events)
        self._milestone(state, milestone_id)
        return _receipt(state, milestone_id, _validate_chain(events), self._now(events))

    def generate_dispute_packet(self, contract_id: str) -> dict:
        events = self._events(contract_id)
        return _packet(events, self._now(events))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="freelance-escrow.sqlite3", help="SQLite ledger path")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="apply one JSON action/arguments object")
    record.add_argument("input", type=Path)
    for name in ("audit", "packet", "receipt", "verify-delivery"):
        command = commands.add_parser(name)
        command.add_argument("contract_id")
        if name in ("receipt", "verify-delivery"):
            command.add_argument("milestone_id")
        if name == "verify-delivery":
            command.add_argument("directory", type=Path)
        if name == "audit":
            command.add_argument("--expected-head")
    for name in ("verify-packet", "verify-receipt"):
        command = commands.add_parser(name)
        command.add_argument("input", type=Path)
        command.add_argument("--expected-head" if name == "verify-packet" else "--expected-sha256")
    args = parser.parse_args(argv)
    try:
        if args.command in ("verify-packet", "verify-receipt"):
            data = json.loads(args.input.read_text(encoding="utf-8"))
            valid = (verify_dispute_packet(data, expected_head=args.expected_head)
                     if args.command == "verify-packet" else
                     verify_receipt(data, expected_sha256=args.expected_sha256))
            print(json.dumps({"valid": valid}))
            return 0 if valid else 1
        with FreelanceContractEscrowAudit(args.db) as ledger:
            if args.command == "record":
                data = json.loads(args.input.read_text(encoding="utf-8"))
                actions = {name: getattr(ledger, name) for name in (
                    "create_contract", "add_milestone", "record_delivery", "verify_escrow_deposit",
                    "issue_acceptance_token", "log_client_acceptance",
                )}
                if (not isinstance(data, dict) or set(data) != {"action", "arguments"} or
                        not isinstance(data["action"], str) or data["action"] not in actions or
                        not isinstance(data["arguments"], dict)):
                    raise ValueError("record requires an allowed action and an arguments object")
                result = actions[data["action"]](**data["arguments"])
            elif args.command == "audit":
                result = ledger.verify_ledger(args.contract_id, expected_head=args.expected_head)
            elif args.command == "packet":
                result = ledger.generate_dispute_packet(args.contract_id)
            elif args.command == "receipt":
                result = ledger.generate_receipt(args.contract_id, args.milestone_id)
            else:
                valid = ledger.verify_delivery(args.contract_id, args.milestone_id, args.directory)
                print(json.dumps({"valid": valid}))
                return 0 if valid else 1
            print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
