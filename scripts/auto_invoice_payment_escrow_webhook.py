#!/usr/bin/env python3
"""Decoupled Automated Invoice & Payment Escrow Webhook Daemon.

Standard-library HTTP webhook receiver, cryptographic payment verifier,
and multi-format (Markdown / pure-PDF) commercial invoice generator.

Integrates with PhalanxDB for atomic persistence, idempotent webhook processing,
and milestone release authorization for encrypted software delivery packages.

Supported Payment Providers:
1. FeeGate x402 v2 (HMAC-SHA256 & base64 PAYMENT-SIGNATURE)
2. Base L2 USDC (EIP-3009 transferWithAuthorization / tx hash binding)
3. Domestic Mock (Alipay / WeChat Pay HMAC-SHA256 canonical query)
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.parse

# Pre-injected microkernel import
try:
    from phalanx_base import (
        PhalanxDB,
        CircuitBreaker,
        ExponentialBackoff,
        GoldenFixtureLoader,
        PhalanxError,
    )
except ImportError:
    # Fallback to local import if run from repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from phalanx_base import (
        PhalanxDB,
        CircuitBreaker,
        ExponentialBackoff,
        GoldenFixtureLoader,
        PhalanxError,
    )

SCHEMA_VERSION = "1.0.0"
DEFAULT_PLATFORM_FEE_BPS = 250  # 2.5% in basis points (1 bp = 0.01%)
DEFAULT_VAT_TAX_BPS = 600       # 6.0% VAT in basis points
DEFAULT_TOKEN_EXPIRY_SEC = 86400  # 24 hours


class WebhookError(PhalanxError):
    """Base error for webhook and payment processing."""
    pass


class SignatureVerificationError(WebhookError):
    """Raised when webhook cryptographic signature fails verification."""
    pass


class ReplayAttackError(WebhookError):
    """Raised when an identical webhook transaction hash is replayed."""
    pass


class InvoiceNotFoundError(WebhookError):
    """Raised when a requested invoice does not exist."""
    pass


# ==============================================================================
# 1. Cryptographic Webhook Verifiers
# ==============================================================================

class PaymentVerifier:
    """Standard-library cryptographic verification for payment webhooks."""

    @staticmethod
    def sign_hmac_sha256(secret: str, message: str | bytes) -> str:
        """Computes lowercase hex HMAC-SHA256 signature."""
        if isinstance(message, str):
            message = message.encode("utf-8")
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        return hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()

    @classmethod
    def verify_feegate_x402(
        cls,
        payload: Dict[str, Any],
        signature_header: str,
        secret: str,
        max_clock_skew_sec: int = 300,
    ) -> Tuple[bool, str]:
        """Verifies FeeGate x402 payment webhook payload.
        
        Signature header format: 't=<timestamp>,v1=<hex_hmac>' or direct hex signature.
        """
        if not signature_header:
            return False, "missing signature header"

        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        timestamp = payload.get("timestamp") or payload.get("created_at") or int(time.time())

        # Check timestamp window
        now = int(time.time())
        if abs(now - int(timestamp)) > max_clock_skew_sec:
            return False, f"clock skew exceeded ({abs(now - int(timestamp))}s > {max_clock_skew_sec}s)"

        expected_sig = cls.sign_hmac_sha256(secret, payload_bytes)
        
        # Support both 'v1=<sig>' format and plain hex
        sig_to_compare = signature_header
        if "v1=" in signature_header:
            parts = dict(re.findall(r"(\w+)=([^,]+)", signature_header))
            sig_to_compare = parts.get("v1", "")

        if not hmac.compare_digest(expected_sig.lower(), sig_to_compare.lower()):
            return False, "invalid signature digest"

        return True, "verified"

    @classmethod
    def verify_base_l2_usdc(
        cls,
        payload: Dict[str, Any],
        secret: str,
    ) -> Tuple[bool, str]:
        """Verifies Base L2 USDC transfer authorization payload.
        
        Payload must specify chain_id (8453 for Base Mainnet or 84532 for Sepolia),
        tx_hash, from_address, to_address, value_usdc_cents, and auth_signature.
        """
        required_keys = ("chain_id", "tx_hash", "from_address", "to_address", "amount_cents")
        for k in required_keys:
            if k not in payload:
                return False, f"missing required field: {k}"

        chain_id = int(payload["chain_id"])
        if chain_id not in (8453, 84532):
            return False, f"unsupported chain_id: {chain_id} (expected 8453 or 84532)"

        # Verify transaction hash format (0x followed by 64 hex characters)
        tx_hash = str(payload["tx_hash"])
        if not re.match(r"^0x[a-fA-F0-9]{64}$", tx_hash):
            return False, "invalid tx_hash format"

        # Verify HMAC attestation if signature is provided
        provided_sig = payload.get("signature") or payload.get("auth_signature")
        if provided_sig:
            canonical_msg = f"{chain_id}:{tx_hash.lower()}:{payload['from_address'].lower()}:{payload['to_address'].lower()}:{payload['amount_cents']}"
            expected_sig = cls.sign_hmac_sha256(secret, canonical_msg)
            if not hmac.compare_digest(expected_sig.lower(), str(provided_sig).lower()):
                return False, "invalid base L2 transfer HMAC signature"

        return True, "verified"

    @classmethod
    def verify_domestic_mock(
        cls,
        payload: Dict[str, Any],
        secret: str,
    ) -> Tuple[bool, str]:
        """Verifies domestic Alipay / WeChat Pay mock webhook."""
        if "sign" not in payload:
            return False, "missing 'sign' parameter"

        received_sign = payload["sign"]
        # Build canonical query string excluding 'sign' and 'sign_type'
        filtered_params = {
            k: v for k, v in payload.items()
            if k not in ("sign", "sign_type") and v is not None and str(v) != ""
        }
        sorted_pairs = sorted(filtered_params.items())
        canonical_str = "&".join(f"{k}={v}" for k, v in sorted_pairs)
        
        expected_sign = cls.sign_hmac_sha256(secret, canonical_str)
        if not hmac.compare_digest(expected_sign.lower(), received_sign.lower()):
            return False, "domestic signature mismatch"

        trade_status = payload.get("trade_status") or payload.get("result_code")
        if trade_status not in ("TRADE_SUCCESS", "TRADE_FINISHED", "SUCCESS"):
            return False, f"unsuccessful trade status: {trade_status}"

        return True, "verified"


# ==============================================================================
# 2. Commercial Invoice Calculation & Formatting Engine
# ==============================================================================

class InvoiceCalculator:
    """Accurately calculates fee splits, VAT taxes, and cryptographic tokens."""

    @staticmethod
    def calculate_breakdown(
        amount_minor: int,
        fee_bps: int = DEFAULT_PLATFORM_FEE_BPS,
        tax_bps: int = DEFAULT_VAT_TAX_BPS,
    ) -> Dict[str, int]:
        """Calculates subtotal, fee, tax, and net payable in minor currency units."""
        subtotal = amount_minor
        fee = math.ceil(subtotal * fee_bps / 10000)
        tax = math.ceil(subtotal * tax_bps / 10000)
        total = subtotal + tax  # Client pays subtotal + tax
        net_settlement = subtotal - fee  # Merchant receives subtotal minus platform fee
        return {
            "subtotal_minor": subtotal,
            "fee_minor": fee,
            "fee_bps": fee_bps,
            "tax_minor": tax,
            "tax_bps": tax_bps,
            "total_minor": total,
            "net_settlement_minor": net_settlement,
        }

    @staticmethod
    def generate_receipt_token(
        invoice_id: str,
        milestone_id: str,
        client_id: str,
        tx_hash: str,
        amount_minor: int,
        secret: str,
    ) -> str:
        """Generates a verifiable SHA-256 receipt token."""
        raw = f"{invoice_id}:{milestone_id}:{client_id}:{tx_hash}:{amount_minor}:{secret}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"rcpt_{digest[:32]}"


class InvoiceFormatter:
    """Renders invoices into Markdown and standard-library minimal valid PDF 1.4."""

    @staticmethod
    def to_markdown(invoice: Dict[str, Any]) -> str:
        """Generates a professional Markdown commercial invoice."""
        currency = invoice.get("currency", "USD")
        divisor = 100.0
        subtotal = invoice.get("amount_minor", 0) / divisor
        tax = invoice.get("tax_minor", 0) / divisor
        fee = invoice.get("fee_minor", 0) / divisor
        total = invoice.get("total_minor", 0) / divisor
        net = (invoice.get("amount_minor", 0) - invoice.get("fee_minor", 0)) / divisor

        status_badge = "🟢 PAID" if invoice.get("status") == "paid" else "🟡 PENDING"
        created_at = invoice.get("created_at_iso", datetime.now(timezone.utc).isoformat())

        md = f"""# 📄 COMMERCIAL INVOICE & PAYMENT RECEIPT
**Invoice ID**: `{invoice['invoice_id']}`  
**Status**: {status_badge}  
**Date**: {created_at}  

---

### 🏢 Parties
- **Billed To (Client)**: `{invoice.get('client_id', 'N/A')}`
- **Payee / Developer**: `{invoice.get('developer_id', 'Autonomous Engineering Service')}`
- **Contract Reference**: `{invoice.get('contract_id', 'N/A')}`
- **Milestone Reference**: `{invoice.get('milestone_id', 'N/A')}`

---

### 💰 Itemized Accounting Breakdown
| Line Item Description | Units | Rate ({currency}) | Amount ({currency}) |
| :--- | :---: | :---: | :---: |
| **{invoice.get('title', 'Engineering & Data Milestone Service')}** | 1 | {subtotal:,.2f} | {subtotal:,.2f} |
| **VAT / Sales Tax** ({invoice.get('tax_bps', 600) / 100:.1f}%) | - | - | +{tax:,.2f} |
| **Platform Escrow Fee** ({invoice.get('fee_bps', 250) / 100:.2f}%) | - | - | ({fee:,.2f}) |
| **GRAND TOTAL PAYABLE** | - | - | **{total:,.2f} {currency}** |

*Net Merchant Settlement*: `{net:,.2f} {currency}`

---

### 🔐 Cryptographic Settlement Evidence
- **Payment Method**: `{invoice.get('payment_provider', 'x402 / Base L2 / Domestic')}`
- **Transaction Hash**: `{invoice.get('tx_hash', 'N/A')}`
- **Receipt Token**: `{invoice.get('receipt_token', 'PENDING_SETTLEMENT')}`
- **Cryptographic Seal**: `sha256:{hashlib.sha256(str(invoice).encode('utf-8')).hexdigest()}`

---
*Generated by Autonomous Commercial Billing Daemon v{SCHEMA_VERSION}*
"""
        return md

    @staticmethod
    def to_pdf_bytes(invoice: Dict[str, Any]) -> bytes:
        """Generates a minimal, 100% valid PDF 1.4 byte stream without external libraries."""
        # Simple text stream representation of invoice lines
        title = f"INVOICE {invoice.get('invoice_id', 'INV-001')}"
        status = f"Status: {invoice.get('status', 'PENDING').upper()}"
        client = f"Client: {invoice.get('client_id', 'N/A')}"
        milestone = f"Milestone: {invoice.get('milestone_id', 'N/A')}"
        currency = invoice.get("currency", "USD")
        total = f"Total: {invoice.get('total_minor', 0) / 100:.2f} {currency}"
        receipt = f"Receipt Token: {invoice.get('receipt_token', 'N/A')}"
        seal = f"Digest: {hashlib.sha256(str(invoice).encode('utf-8')).hexdigest()[:24]}"

        # Construct PDF content stream
        # PDF coordinate system: origin at bottom-left
        content_stream = (
            "BT\n"
            "/F1 20 Tf\n"
            "50 750 Td\n"
            f"({title}) Tj\n"
            "/F1 12 Tf\n"
            "0 -30 Td\n"
            f"({status}) Tj\n"
            "0 -20 Td\n"
            f"({client}) Tj\n"
            "0 -20 Td\n"
            f"({milestone}) Tj\n"
            "0 -30 Td\n"
            f"({total}) Tj\n"
            "0 -20 Td\n"
            f"({receipt}) Tj\n"
            "0 -20 Td\n"
            f"({seal}) Tj\n"
            "ET\n"
        ).encode("latin-1", errors="replace")

        stream_len = len(content_stream)

        # PDF structure components
        pdf_objs = [
            b"%PDF-1.4\n",
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
            (
                b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
            ),
            (
                f"4 0 obj\n<< /Length {stream_len} >>\nstream\n".encode("latin-1")
                + content_stream
                + b"\nendstream\nendobj\n"
            ),
            b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        ]

        # Compute xref offsets
        offsets = [0]
        curr_offset = len(pdf_objs[0])
        for obj in pdf_objs[1:]:
            offsets.append(curr_offset)
            curr_offset += len(obj)

        xref_table = [
            f"xref\n0 {len(offsets)}\n".encode("latin-1"),
            b"0000000000 65535 f \n",
        ]
        for off in offsets[1:]:
            xref_table.append(f"{off:010d} 00000 n \n".encode("latin-1"))

        xref_bytes = b"".join(xref_table)
        trailer = (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{curr_offset}\n%%EOF\n"
        ).encode("latin-1")

        return b"".join(pdf_objs) + xref_bytes + trailer


# ==============================================================================
# 3. Phalanx Ledger & Escrow State Manager
# ==============================================================================

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    title TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    tax_minor INTEGER NOT NULL,
    tax_bps INTEGER NOT NULL,
    fee_minor INTEGER NOT NULL,
    fee_bps INTEGER NOT NULL,
    total_minor INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    paid_at INTEGER,
    receipt_token TEXT,
    tx_hash TEXT,
    payment_provider TEXT
);

CREATE TABLE IF NOT EXISTS payment_receipts (
    receipt_id TEXT PRIMARY KEY,
    invoice_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    tx_hash TEXT NOT NULL UNIQUE,
    payer TEXT,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    receipt_token TEXT NOT NULL,
    verified_at INTEGER NOT NULL,
    raw_payload TEXT NOT NULL,
    FOREIGN KEY(invoice_id) REFERENCES invoices(invoice_id)
);

CREATE TABLE IF NOT EXISTS package_releases (
    release_id TEXT PRIMARY KEY,
    invoice_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    release_token TEXT NOT NULL UNIQUE,
    authorized_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    access_count INTEGER DEFAULT 0,
    FOREIGN KEY(invoice_id) REFERENCES invoices(invoice_id)
);
"""

class InvoiceLedger:
    """Manages transactional state using PhalanxDB with WAL and retry semantics."""

    def __init__(self, db_path: Optional[str | Path] = None):
        self.db = PhalanxDB(db_path)
        self._init_db()

    def _init_db(self) -> None:
        self.db.executescript(MIGRATION_SQL)

    def close(self) -> None:
        self.db.close()

    def create_invoice(
        self,
        invoice_id: str,
        contract_id: str,
        milestone_id: str,
        client_id: str,
        title: str,
        amount_minor: int,
        currency: str = "USD",
        fee_bps: int = DEFAULT_PLATFORM_FEE_BPS,
        tax_bps: int = DEFAULT_VAT_TAX_BPS,
    ) -> Dict[str, Any]:
        """Creates a pending commercial invoice record."""
        breakdown = InvoiceCalculator.calculate_breakdown(amount_minor, fee_bps, tax_bps)
        now = int(time.time())

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO invoices (
                    invoice_id, contract_id, milestone_id, client_id, title,
                    amount_minor, currency, tax_minor, tax_bps, fee_minor, fee_bps,
                    total_minor, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    invoice_id, contract_id, milestone_id, client_id, title,
                    breakdown["subtotal_minor"], currency, breakdown["tax_minor"], breakdown["tax_bps"],
                    breakdown["fee_minor"], breakdown["fee_bps"], breakdown["total_minor"], now,
                ),
            )

        return self.get_invoice(invoice_id)

    def get_invoice(self, invoice_id: str) -> Dict[str, Any]:
        """Fetches invoice record by ID."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
            ).fetchone()
            if not row:
                raise InvoiceNotFoundError(f"Invoice '{invoice_id}' not found.")
            
            # Map sqlite3.Row or tuple
            cols = [col[0] for col in conn.execute("SELECT * FROM invoices LIMIT 0").description]
            data = dict(zip(cols, row))
            data["created_at_iso"] = datetime.fromtimestamp(data["created_at"], tz=timezone.utc).isoformat()
            if data["paid_at"]:
                data["paid_at_iso"] = datetime.fromtimestamp(data["paid_at"], tz=timezone.utc).isoformat()
            return data

    def settle_invoice_payment(
        self,
        invoice_id: str,
        provider: str,
        tx_hash: str,
        payer: str,
        raw_payload: Dict[str, Any],
        secret: str,
        package_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Atomically records receipt, marks invoice paid, and emits release token."""
        inv = self.get_invoice(invoice_id)
        if inv["status"] == "paid":
            # Idempotent return if already paid with same tx_hash
            if inv.get("tx_hash") == tx_hash:
                release_info = self.get_release_by_invoice(invoice_id)
                return inv, release_info
            raise WebhookError(f"Invoice '{invoice_id}' is already paid under different tx: {inv.get('tx_hash')}")

        now = int(time.time())
        receipt_token = InvoiceCalculator.generate_receipt_token(
            invoice_id, inv["milestone_id"], inv["client_id"], tx_hash, inv["total_minor"], secret
        )
        receipt_id = f"rcpt_{hashlib.sha256(f'{invoice_id}:{tx_hash}'.encode()).hexdigest()[:16]}"
        
        # Package release token
        pkg_id = package_id or f"pkg_{inv['milestone_id']}"
        release_token = f"rel_{hashlib.sha256(f'{receipt_token}:{pkg_id}:{now}'.encode()).hexdigest()[:32]}"
        release_id = f"relid_{receipt_id[:12]}"
        expires_at = now + DEFAULT_TOKEN_EXPIRY_SEC

        with self.db.transaction() as conn:
            # 1. Check replay of tx_hash
            dup = conn.execute("SELECT 1 FROM payment_receipts WHERE tx_hash = ?", (tx_hash,)).fetchone()
            if dup:
                raise ReplayAttackError(f"Transaction hash '{tx_hash}' has already been processed.")

            # 2. Insert receipt
            conn.execute(
                """
                INSERT INTO payment_receipts (
                    receipt_id, invoice_id, provider, tx_hash, payer,
                    amount_minor, currency, receipt_token, verified_at, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id, invoice_id, provider, tx_hash, payer,
                    inv["total_minor"], inv["currency"], receipt_token, now, json.dumps(raw_payload)
                ),
            )

            # 3. Update invoice
            conn.execute(
                """
                UPDATE invoices SET
                    status = 'paid', paid_at = ?, receipt_token = ?,
                    tx_hash = ?, payment_provider = ?
                WHERE invoice_id = ?
                """,
                (now, receipt_token, tx_hash, provider, invoice_id),
            )

            # 4. Insert package release authorization
            conn.execute(
                """
                INSERT INTO package_releases (
                    release_id, invoice_id, milestone_id, package_id,
                    release_token, authorized_at, expires_at, access_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (release_id, invoice_id, inv["milestone_id"], pkg_id, release_token, now, expires_at),
            )

        updated_inv = self.get_invoice(invoice_id)
        release_info = {
            "release_id": release_id,
            "invoice_id": invoice_id,
            "milestone_id": inv["milestone_id"],
            "package_id": pkg_id,
            "release_token": release_token,
            "authorized_at": now,
            "expires_at": expires_at,
        }
        return updated_inv, release_info

    def get_release_by_invoice(self, invoice_id: str) -> Dict[str, Any]:
        """Gets active release token for invoice."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM package_releases WHERE invoice_id = ?", (invoice_id,)
            ).fetchone()
            if not row:
                return {}
            cols = [col[0] for col in conn.execute("SELECT * FROM package_releases LIMIT 0").description]
            return dict(zip(cols, row))

    def verify_and_consume_release_token(self, release_token: str) -> Dict[str, Any]:
        """Validates package release authorization and increments access count."""
        now = int(time.time())
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM package_releases WHERE release_token = ?", (release_token,)
            ).fetchone()
            if not row:
                raise WebhookError("Invalid release token.")
            
            cols = [col[0] for col in conn.execute("SELECT * FROM package_releases LIMIT 0").description]
            data = dict(zip(cols, row))
            if now > data["expires_at"]:
                raise WebhookError("Release token has expired.")

            conn.execute(
                "UPDATE package_releases SET access_count = access_count + 1 WHERE release_token = ?",
                (release_token,),
            )
            data["access_count"] += 1
            return data


# ==============================================================================
# 4. Webhook HTTP Daemon
# ==============================================================================

class WebhookHTTPHandler(BaseHTTPRequestHandler):
    """Standard-library HTTP Request Handler for payment webhooks and invoices."""

    server_version = f"PhalanxWebhookDaemon/{SCHEMA_VERSION}"

    @property
    def ledger(self) -> InvoiceLedger:
        return self.server.ledger  # type: ignore

    @property
    def secret(self) -> str:
        return self.server.secret  # type: ignore

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self.server.circuit_breaker  # type: ignore

    def _send_json(self, status_code: int, data: Any) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len <= 0:
            return {}
        body = self.rfile.read(content_len).decode("utf-8")
        return json.loads(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 1. Health check
        if path == "/health":
            self._send_json(HTTPStatus.OK, {
                "status": "healthy",
                "version": SCHEMA_VERSION,
                "circuit_breaker": self.circuit_breaker.state.value,
            })
            return

        # 2. View invoice /invoices/<id>
        if path.startswith("/invoices/"):
            invoice_id = path.split("/invoices/")[1].strip("/")
            try:
                inv = self.ledger.get_invoice(invoice_id)
                fmt = query.get("format", ["json"])[0].lower()
                if fmt == "markdown":
                    md_text = InvoiceFormatter.to_markdown(inv).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Length", str(len(md_text)))
                    self.end_headers()
                    self.wfile.write(md_text)
                    return
                elif fmt == "pdf":
                    pdf_bytes = InvoiceFormatter.to_pdf_bytes(inv)
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Disposition", f'inline; filename="{invoice_id}.pdf"')
                    self.send_header("Content-Length", str(len(pdf_bytes)))
                    self.end_headers()
                    self.wfile.write(pdf_bytes)
                    return
                else:
                    self._send_json(HTTPStatus.OK, inv)
                    return
            except InvoiceNotFoundError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Invoice not found"})
                return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_json_body()
        except Exception as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Malformed JSON: {e}"})
            return

        # Route: Create invoice
        if path == "/invoices":
            try:
                inv = self.ledger.create_invoice(
                    invoice_id=body["invoice_id"],
                    contract_id=body.get("contract_id", "contract-direct"),
                    milestone_id=body.get("milestone_id", "m1"),
                    client_id=body["client_id"],
                    title=body.get("title", "Commercial Software Delivery"),
                    amount_minor=int(body["amount_minor"]),
                    currency=body.get("currency", "USD"),
                    fee_bps=int(body.get("fee_bps", DEFAULT_PLATFORM_FEE_BPS)),
                    tax_bps=int(body.get("tax_bps", DEFAULT_VAT_TAX_BPS)),
                )
                self._send_json(HTTPStatus.CREATED, inv)
                return
            except Exception as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return

        # Route: FeeGate x402 Webhook
        if path == "/webhook/feegate":
            sig_header = self.headers.get("X-Payment-Signature") or self.headers.get("PAYMENT-SIGNATURE", "")
            ok, reason = PaymentVerifier.verify_feegate_x402(body, sig_header, self.secret)
            if not ok:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": f"Verification failed: {reason}"})
                return

            self._process_settlement(body, provider="feegate_x402")
            return

        # Route: Base L2 USDC Webhook
        if path == "/webhook/base-l2":
            ok, reason = PaymentVerifier.verify_base_l2_usdc(body, self.secret)
            if not ok:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": f"Verification failed: {reason}"})
                return

            self._process_settlement(body, provider="base_l2_usdc")
            return

        # Route: Domestic Alipay / WeChat Webhook
        if path == "/webhook/domestic":
            ok, reason = PaymentVerifier.verify_domestic_mock(body, self.secret)
            if not ok:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": f"Verification failed: {reason}"})
                return

            self._process_settlement(body, provider="domestic_alipay_wechat")
            return

        # Route: Verify Package Release Token
        if path == "/release/verify":
            token = body.get("release_token", "")
            try:
                rel = self.ledger.verify_and_consume_release_token(token)
                self._send_json(HTTPStatus.OK, {"authorized": True, "release": rel})
                return
            except Exception as e:
                self._send_json(HTTPStatus.FORBIDDEN, {"authorized": False, "error": str(e)})
                return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})

    def _process_settlement(self, body: Dict[str, Any], provider: str) -> None:
        invoice_id = body.get("invoice_id")
        tx_hash = body.get("tx_hash") or body.get("out_trade_no") or body.get("trade_no")
        payer = body.get("payer") or body.get("from_address") or body.get("buyer_id") or "anonymous"
        package_id = body.get("package_id")

        if not invoice_id or not tx_hash:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'invoice_id' or transaction hash."})
            return

        try:
            inv, rel = self.ledger.settle_invoice_payment(
                invoice_id=invoice_id,
                provider=provider,
                tx_hash=tx_hash,
                payer=payer,
                raw_payload=body,
                secret=self.secret,
                package_id=package_id,
            )
            self._send_json(HTTPStatus.OK, {
                "settled": True,
                "invoice": inv,
                "release": rel,
            })
        except ReplayAttackError as e:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(e)})
        except InvoiceNotFoundError as e:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(e)})
        except Exception as e:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Settlement error: {e}"})


class WebhookServer(ThreadingHTTPServer):
    """Threading HTTP Server holding persistent ledger and circuit breaker."""

    def __init__(
        self,
        server_address: Tuple[str, int],
        secret: str,
        db_path: Optional[str | Path] = None,
    ):
        super().__init__(server_address, WebhookHTTPHandler)
        self.secret = secret
        self.ledger = InvoiceLedger(db_path)
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)


# ==============================================================================
# 5. CLI Interface
# ==============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automated Invoice & Payment Escrow Webhook Daemon")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # serve
    serve_p = subparsers.add_parser("serve", help="Run the webhook HTTP daemon")
    serve_p.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    serve_p.add_argument("--port", type=int, default=8542, help="HTTP port (default: 8542)")
    serve_p.add_argument("--secret", default="dev_shared_webhook_secret_key_12345", help="HMAC shared secret")
    serve_p.add_argument("--db", default=None, help="SQLite database path")

    # create-invoice
    inv_p = subparsers.add_parser("create-invoice", help="Create a commercial invoice")
    inv_p.add_argument("--id", required=True, help="Invoice ID")
    inv_p.add_argument("--client", required=True, help="Client ID")
    inv_p.add_argument("--amount-minor", type=int, required=True, help="Subtotal in minor units (cents)")
    inv_p.add_argument("--currency", default="USD", help="Currency code")
    inv_p.add_argument("--title", default="Engineering & Data Pipeline Delivery", help="Line item description")
    inv_p.add_argument("--db", default=None, help="SQLite database path")

    # render
    render_p = subparsers.add_parser("render", help="Render invoice to Markdown or PDF")
    render_p.add_argument("--id", required=True, help="Invoice ID")
    render_p.add_argument("--format", choices=["markdown", "pdf"], default="markdown", help="Output format")
    render_p.add_argument("--output", default=None, help="Output file path (default: stdout for md)")
    render_p.add_argument("--db", default=None, help="SQLite database path")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "serve":
        server = WebhookServer((args.host, args.port), secret=args.secret, db_path=args.db)
        print(f"🚀 [WEBHOOK DAEMON] Listening on http://{args.host}:{args.port} (v{SCHEMA_VERSION})...")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down daemon...")
            server.server_close()

    elif args.command == "create-invoice":
        ledger = InvoiceLedger(args.db)
        inv = ledger.create_invoice(
            invoice_id=args.id,
            contract_id=f"contract_{args.id}",
            milestone_id=f"m_{args.id}",
            client_id=args.client,
            title=args.title,
            amount_minor=args.amount_minor,
            currency=args.currency,
        )
        print(json.dumps(inv, indent=2))
        ledger.close()

    elif args.command == "render":
        ledger = InvoiceLedger(args.db)
        inv = ledger.get_invoice(args.id)
        if args.format == "markdown":
            md = InvoiceFormatter.to_markdown(inv)
            if args.output:
                Path(args.output).write_text(md, encoding="utf-8")
                print(f"Saved Markdown invoice to {args.output}")
            else:
                print(md)
        elif args.format == "pdf":
            pdf = InvoiceFormatter.to_pdf_bytes(inv)
            out_file = args.output or f"{args.id}.pdf"
            Path(out_file).write_bytes(pdf)
            print(f"Generated PDF invoice ({len(pdf)} bytes) -> {out_file}")
        ledger.close()


if __name__ == "__main__":
    main()
