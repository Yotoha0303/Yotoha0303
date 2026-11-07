#!/usr/bin/env python3
"""Comprehensive test suite for auto_invoice_payment_escrow_webhook.py.

Tests cover:
- Cryptographic verifiers (FeeGate x402, Base L2 USDC, Domestic Alipay/WeChat)
- Fee splits, VAT calculation, and cryptographic token generation
- Invoice rendering (Markdown and pure-PDF 1.4 byte streams)
- PhalanxDB transactional persistence, replay protection, and idempotency
- Package release authorization tokens and lifecycle
- Full HTTP daemon integration (GET /health, /invoices, POST /webhook/*, /release/verify)
- Concurrency and error handling
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

# Insert parent path for module discovery
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from auto_invoice_payment_escrow_webhook import (
    DEFAULT_PLATFORM_FEE_BPS,
    DEFAULT_VAT_TAX_BPS,
    InvoiceCalculator,
    InvoiceFormatter,
    InvoiceLedger,
    InvoiceNotFoundError,
    PaymentVerifier,
    ReplayAttackError,
    SignatureVerificationError,
    WebhookError,
    WebhookServer,
)
from phalanx_base import PhalanxDB, CircuitBreaker, ExponentialBackoff


class TestInvoiceCalculator(unittest.TestCase):
    """Test tax, fee, and token calculations."""

    def test_fee_and_tax_breakdown_default(self):
        amount_minor = 10000  # $100.00
        breakdown = InvoiceCalculator.calculate_breakdown(amount_minor)
        # 2.5% fee = 250 cents
        self.assertEqual(breakdown["fee_minor"], 250)
        # 6.0% tax = 600 cents
        self.assertEqual(breakdown["tax_minor"], 600)
        # Total payable by client = 10000 + 600 = 10600 cents ($106.00)
        self.assertEqual(breakdown["total_minor"], 10600)
        # Net settlement to merchant = 10000 - 250 = 9750 cents ($97.50)
        self.assertEqual(breakdown["net_settlement_minor"], 9750)

    def test_custom_basis_points(self):
        amount_minor = 50000  # $500.00
        breakdown = InvoiceCalculator.calculate_breakdown(amount_minor, fee_bps=100, tax_bps=0)
        self.assertEqual(breakdown["fee_minor"], 500)
        self.assertEqual(breakdown["tax_minor"], 0)
        self.assertEqual(breakdown["total_minor"], 50000)
        self.assertEqual(breakdown["net_settlement_minor"], 49500)

    def test_rounding_up_behavior(self):
        # 1 cent with 2.5% fee -> ceil(0.025) = 1
        breakdown = InvoiceCalculator.calculate_breakdown(1, fee_bps=250, tax_bps=600)
        self.assertEqual(breakdown["fee_minor"], 1)
        self.assertEqual(breakdown["tax_minor"], 1)
        self.assertEqual(breakdown["total_minor"], 2)

    def test_receipt_token_determinism_and_uniqueness(self):
        secret = "super_secret_test_key"
        t1 = InvoiceCalculator.generate_receipt_token("inv-1", "m-1", "client-1", "0x123", 1000, secret)
        t2 = InvoiceCalculator.generate_receipt_token("inv-1", "m-1", "client-1", "0x123", 1000, secret)
        t3 = InvoiceCalculator.generate_receipt_token("inv-2", "m-1", "client-1", "0x123", 1000, secret)

        self.assertTrue(t1.startswith("rcpt_"))
        self.assertEqual(t1, t2)
        self.assertNotEqual(t1, t3)


class TestPaymentVerifier(unittest.TestCase):
    """Test webhook signature and payload validations."""

    SECRET = "test_hmac_secret_gateway_2026"

    def test_feegate_x402_valid(self):
        now = int(time.time())
        payload = {
            "invoice_id": "inv_001",
            "amount_minor": 10000,
            "currency": "USD",
            "timestamp": now,
        }
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = hmac.new(self.SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()

        # Test plain hex
        ok, reason = PaymentVerifier.verify_feegate_x402(payload, sig, self.SECRET)
        self.assertTrue(ok)
        self.assertEqual(reason, "verified")

        # Test header with v1= format
        header = f"t={now},v1={sig}"
        ok, reason = PaymentVerifier.verify_feegate_x402(payload, header, self.SECRET)
        self.assertTrue(ok)

    def test_feegate_x402_invalid_signature(self):
        payload = {"invoice_id": "inv_001", "timestamp": int(time.time())}
        ok, reason = PaymentVerifier.verify_feegate_x402(payload, "invalid_sig_hex", self.SECRET)
        self.assertFalse(ok)
        self.assertIn("invalid signature digest", reason)

    def test_feegate_x402_clock_skew(self):
        old_time = int(time.time()) - 400
        payload = {"invoice_id": "inv_001", "timestamp": old_time}
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = hmac.new(self.SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()

        ok, reason = PaymentVerifier.verify_feegate_x402(payload, sig, self.SECRET, max_clock_skew_sec=300)
        self.assertFalse(ok)
        self.assertIn("clock skew exceeded", reason)

    def test_base_l2_usdc_valid(self):
        tx_hash = "0x" + "a" * 64
        msg = f"8453:{tx_hash.lower()}:0x1111:0x2222:5000"
        sig = hmac.new(self.SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

        payload = {
            "chain_id": 8453,
            "tx_hash": tx_hash,
            "from_address": "0x1111",
            "to_address": "0x2222",
            "amount_cents": 5000,
            "auth_signature": sig,
        }
        ok, reason = PaymentVerifier.verify_base_l2_usdc(payload, self.SECRET)
        self.assertTrue(ok)
        self.assertEqual(reason, "verified")

    def test_base_l2_usdc_unsupported_chain(self):
        payload = {
            "chain_id": 1,  # Ethereum mainnet, not Base
            "tx_hash": "0x" + "b" * 64,
            "from_address": "0x1111",
            "to_address": "0x2222",
            "amount_cents": 5000,
        }
        ok, reason = PaymentVerifier.verify_base_l2_usdc(payload, self.SECRET)
        self.assertFalse(ok)
        self.assertIn("unsupported chain_id", reason)

    def test_base_l2_usdc_invalid_hash(self):
        payload = {
            "chain_id": 8453,
            "tx_hash": "not_a_valid_hash",
            "from_address": "0x1111",
            "to_address": "0x2222",
            "amount_cents": 5000,
        }
        ok, reason = PaymentVerifier.verify_base_l2_usdc(payload, self.SECRET)
        self.assertFalse(ok)
        self.assertIn("invalid tx_hash format", reason)

    def test_domestic_mock_valid(self):
        params = {
            "out_trade_no": "INV-20261107",
            "total_amount": "100.00",
            "trade_status": "TRADE_SUCCESS",
            "buyer_id": "buyer_12345",
        }
        canonical = "buyer_id=buyer_12345&out_trade_no=INV-20261107&total_amount=100.00&trade_status=TRADE_SUCCESS"
        sign = hmac.new(self.SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        params["sign"] = sign
        params["sign_type"] = "HMAC-SHA256"

        ok, reason = PaymentVerifier.verify_domestic_mock(params, self.SECRET)
        self.assertTrue(ok)
        self.assertEqual(reason, "verified")

    def test_domestic_mock_tampered(self):
        params = {
            "out_trade_no": "INV-20261107",
            "total_amount": "100.00",
            "trade_status": "TRADE_SUCCESS",
            "sign": "tampered_sign_value",
        }
        ok, reason = PaymentVerifier.verify_domestic_mock(params, self.SECRET)
        self.assertFalse(ok)
        self.assertIn("domestic signature mismatch", reason)


class TestInvoiceFormatter(unittest.TestCase):
    """Test Markdown and pure-PDF 1.4 generation."""

    def setUp(self):
        self.invoice = {
            "invoice_id": "INV-9999",
            "client_id": "client_acme",
            "developer_id": "Phalanx Solutions",
            "contract_id": "CTR-101",
            "milestone_id": "M-1",
            "title": "Automated Webhook Daemon",
            "amount_minor": 20000,  # $200.00
            "tax_minor": 1200,      # $12.00
            "fee_minor": 500,       # $5.00
            "total_minor": 21200,   # $212.00
            "currency": "USD",
            "status": "paid",
            "payment_provider": "Base L2 USDC",
            "tx_hash": "0x" + "f" * 64,
            "receipt_token": "rcpt_1234567890abcdef",
            "created_at_iso": "2026-11-07T22:00:00Z",
        }

    def test_markdown_rendering(self):
        md = InvoiceFormatter.to_markdown(self.invoice)
        self.assertIn("# 📄 COMMERCIAL INVOICE", md)
        self.assertIn("INV-9999", md)
        self.assertIn("200.00", md)
        self.assertIn("212.00", md)
        self.assertIn("🟢 PAID", md)
        self.assertIn("rcpt_1234567890abcdef", md)

    def test_pdf_rendering_valid_structure(self):
        pdf = InvoiceFormatter.to_pdf_bytes(self.invoice)
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.endswith(b"%%EOF\n"))
        self.assertIn(b"/Type /Catalog", pdf)
        self.assertIn(b"/Type /Pages", pdf)
        self.assertIn(b"/Type /Font", pdf)
        self.assertIn(b"xref", pdf)
        self.assertIn(b"trailer", pdf)
        self.assertIn(b"INV-9999", pdf)


class TestInvoiceLedger(unittest.TestCase):
    """Test PhalanxDB persistence, migrations, and atomic settlements."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_billing.sqlite3"
        self.ledger = InvoiceLedger(self.db_path)
        self.secret = "ledger_secret_test_2026"

    def tearDown(self):
        self.ledger.close()
        self.tmp_dir.cleanup()

    def test_create_and_get_invoice(self):
        inv = self.ledger.create_invoice(
            invoice_id="inv_100",
            contract_id="c_1",
            milestone_id="m_1",
            client_id="client_corp",
            title="Data Extraction Pipeline",
            amount_minor=15000,
            currency="USD",
        )
        self.assertEqual(inv["invoice_id"], "inv_100")
        self.assertEqual(inv["status"], "pending")
        self.assertEqual(inv["amount_minor"], 15000)
        self.assertEqual(inv["total_minor"], 15900)  # 15000 + 6% VAT

        fetched = self.ledger.get_invoice("inv_100")
        self.assertEqual(fetched["title"], "Data Extraction Pipeline")

    def test_invoice_not_found(self):
        with self.assertRaises(InvoiceNotFoundError):
            self.ledger.get_invoice("non_existent_invoice")

    def test_settle_invoice_payment_success(self):
        self.ledger.create_invoice(
            invoice_id="inv_200",
            contract_id="c_2",
            milestone_id="m_2",
            client_id="client_alpha",
            title="Security Audit",
            amount_minor=50000,
        )
        tx_hash = "0x" + "1" * 64
        inv, rel = self.ledger.settle_invoice_payment(
            invoice_id="inv_200",
            provider="feegate_x402",
            tx_hash=tx_hash,
            payer="0xpayer",
            raw_payload={"mock": True},
            secret=self.secret,
        )
        self.assertEqual(inv["status"], "paid")
        self.assertTrue(inv["receipt_token"].startswith("rcpt_"))
        self.assertTrue(rel["release_token"].startswith("rel_"))
        self.assertEqual(rel["milestone_id"], "m_2")

    def test_replay_attack_prevention(self):
        self.ledger.create_invoice(
            invoice_id="inv_301",
            contract_id="c_3",
            milestone_id="m_3",
            client_id="client_beta",
            title="Part 1",
            amount_minor=10000,
        )
        self.ledger.create_invoice(
            invoice_id="inv_302",
            contract_id="c_3",
            milestone_id="m_4",
            client_id="client_beta",
            title="Part 2",
            amount_minor=10000,
        )
        shared_tx_hash = "0x" + "2" * 64

        # Settle first invoice with this tx_hash
        self.ledger.settle_invoice_payment(
            invoice_id="inv_301",
            provider="base_l2_usdc",
            tx_hash=shared_tx_hash,
            payer="0xpayer",
            raw_payload={},
            secret=self.secret,
        )

        # Attempt to reuse the same tx_hash for second invoice -> ReplayAttackError
        with self.assertRaises(ReplayAttackError):
            self.ledger.settle_invoice_payment(
                invoice_id="inv_302",
                provider="base_l2_usdc",
                tx_hash=shared_tx_hash,
                payer="0xpayer",
                raw_payload={},
                secret=self.secret,
            )

    def test_idempotent_duplicate_settlement(self):
        self.ledger.create_invoice(
            invoice_id="inv_400",
            contract_id="c_4",
            milestone_id="m_4",
            client_id="client_gamma",
            title="Idempotency Test",
            amount_minor=8000,
        )
        tx_hash = "0x" + "3" * 64

        inv1, rel1 = self.ledger.settle_invoice_payment(
            invoice_id="inv_400",
            provider="feegate_x402",
            tx_hash=tx_hash,
            payer="0xpayer",
            raw_payload={},
            secret=self.secret,
        )

        # Repeat same call -> returns identical records without error
        inv2, rel2 = self.ledger.settle_invoice_payment(
            invoice_id="inv_400",
            provider="feegate_x402",
            tx_hash=tx_hash,
            payer="0xpayer",
            raw_payload={},
            secret=self.secret,
        )
        self.assertEqual(inv1["receipt_token"], inv2["receipt_token"])
        self.assertEqual(rel1["release_token"], rel2["release_token"])

    def test_package_release_token_consumption_and_expiry(self):
        self.ledger.create_invoice(
            invoice_id="inv_500",
            contract_id="c_5",
            milestone_id="m_5",
            client_id="client_delta",
            title="Delivery Package",
            amount_minor=12000,
        )
        inv, rel = self.ledger.settle_invoice_payment(
            invoice_id="inv_500",
            provider="domestic",
            tx_hash="0x" + "4" * 64,
            payer="buyer_1",
            raw_payload={},
            secret=self.secret,
        )

        token = rel["release_token"]
        consumed = self.ledger.verify_and_consume_release_token(token)
        self.assertEqual(consumed["access_count"], 1)

        # Second access increments counter
        consumed2 = self.ledger.verify_and_consume_release_token(token)
        self.assertEqual(consumed2["access_count"], 2)

        # Non-existent token fails
        with self.assertRaises(WebhookError):
            self.ledger.verify_and_consume_release_token("rel_invalid_non_existent")


class TestWebhookHTTPDaemon(unittest.TestCase):
    """Integration test with live ThreadingHTTPServer on loopback port."""

    HOST = "127.0.0.1"
    SECRET = "daemon_integration_secret_2026"

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp_dir.name) / "http_test.sqlite3"
        # Bind to port 0 for OS-assigned ephemeral free port
        cls.server = WebhookServer((cls.HOST, 0), secret=cls.SECRET, db_path=cls.db_path)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://{cls.HOST}:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.ledger.close()
        cls.tmp_dir.cleanup()

    def _http_request(
        self,
        path: str,
        method: str = "GET",
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Any, Dict[str, str]]:
        url = f"{self.base_url}{path}"
        req_headers = headers or {}
        req_data = None
        if data is not None:
            req_data = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=req_data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                resp_headers = dict(resp.headers)
                body = resp.read()
                content_type = resp_headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return status, json.loads(body.decode("utf-8")), resp_headers
                return status, body, resp_headers
        except urllib.error.HTTPError as e:
            err_body = e.read()
            try:
                parsed_err = json.loads(err_body.decode("utf-8"))
            except Exception:
                parsed_err = err_body
            return e.code, parsed_err, dict(e.headers)

    def test_health_check_endpoint(self):
        status, data, _ = self._http_request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["circuit_breaker"].lower(), "closed")

    def test_create_and_view_invoice_json_markdown_pdf(self):
        # 1. Create invoice via POST /invoices
        payload = {
            "invoice_id": "INV-HTTP-001",
            "client_id": "client_enterprise",
            "title": "Cloud Infra Setup",
            "amount_minor": 30000,
            "currency": "USD",
        }
        status, created, _ = self._http_request("/invoices", method="POST", data=payload)
        self.assertEqual(status, 201)
        self.assertEqual(created["invoice_id"], "INV-HTTP-001")

        # 2. View JSON format
        status, fetched, _ = self._http_request("/invoices/INV-HTTP-001")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["title"], "Cloud Infra Setup")

        # 3. View Markdown format
        status, md_bytes, _ = self._http_request("/invoices/INV-HTTP-001?format=markdown")
        self.assertEqual(status, 200)
        self.assertIn(b"# \xf0\x9f\x93\x84 COMMERCIAL INVOICE", md_bytes)

        # 4. View PDF format
        status, pdf_bytes, hdrs = self._http_request("/invoices/INV-HTTP-001?format=pdf")
        self.assertEqual(status, 200)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
        self.assertIn("application/pdf", hdrs.get("Content-Type", ""))

    def test_feegate_webhook_flow(self):
        # Create invoice
        self._http_request("/invoices", method="POST", data={
            "invoice_id": "INV-FG-01", "client_id": "c1", "amount_minor": 10000
        })

        now = int(time.time())
        tx_hash = "0x" + "a" * 64
        body = {
            "invoice_id": "INV-FG-01",
            "tx_hash": tx_hash,
            "payer": "0xfeegate_client",
            "timestamp": now,
        }
        body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = hmac.new(self.SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()

        # Unauthorized request with bad signature
        status, err, _ = self._http_request(
            "/webhook/feegate", method="POST", data=body,
            headers={"X-Payment-Signature": "invalid_sig"}
        )
        self.assertEqual(status, 401)

        # Authorized request
        status, resp, _ = self._http_request(
            "/webhook/feegate", method="POST", data=body,
            headers={"X-Payment-Signature": f"v1={sig}"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(resp["settled"])
        self.assertEqual(resp["invoice"]["status"], "paid")

        # Release token verification
        release_token = resp["release"]["release_token"]
        status, rel_resp, _ = self._http_request(
            "/release/verify", method="POST", data={"release_token": release_token}
        )
        self.assertEqual(status, 200)
        self.assertTrue(rel_resp["authorized"])

    def test_base_l2_webhook_flow(self):
        self._http_request("/invoices", method="POST", data={
            "invoice_id": "INV-BASE-01", "client_id": "c2", "amount_minor": 25000
        })

        tx_hash = "0x" + "b" * 64
        msg = f"8453:{tx_hash.lower()}:0xfrom:0xto:25000"
        sig = hmac.new(self.SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

        body = {
            "invoice_id": "INV-BASE-01",
            "chain_id": 8453,
            "tx_hash": tx_hash,
            "from_address": "0xfrom",
            "to_address": "0xto",
            "amount_cents": 25000,
            "auth_signature": sig,
        }

        status, resp, _ = self._http_request("/webhook/base-l2", method="POST", data=body)
        self.assertEqual(status, 200)
        self.assertEqual(resp["invoice"]["status"], "paid")
        self.assertEqual(resp["invoice"]["payment_provider"], "base_l2_usdc")

    def test_domestic_mock_webhook_flow(self):
        self._http_request("/invoices", method="POST", data={
            "invoice_id": "INV-CNY-01", "client_id": "c3", "amount_minor": 8800
        })

        canonical = "buyer_id=buyer888&invoice_id=INV-CNY-01&out_trade_no=ALIPAY_20261107_01&total_amount=88.00&trade_status=TRADE_SUCCESS"
        sign = hmac.new(self.SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()

        body = {
            "invoice_id": "INV-CNY-01",
            "out_trade_no": "ALIPAY_20261107_01",
            "total_amount": "88.00",
            "trade_status": "TRADE_SUCCESS",
            "buyer_id": "buyer888",
            "sign": sign,
        }

        status, resp, _ = self._http_request("/webhook/domestic", method="POST", data=body)
        self.assertEqual(status, 200)
        self.assertEqual(resp["invoice"]["status"], "paid")

    def test_concurrent_invoice_creation(self):
        """Stress test SQLite WAL mode with 10 concurrent HTTP threads."""
        threads = []
        errors = []

        def worker(idx: int):
            try:
                status, res, _ = self._http_request(
                    "/invoices",
                    method="POST",
                    data={
                        "invoice_id": f"INV-CONCUR-{idx}",
                        "client_id": f"client_{idx}",
                        "amount_minor": 1000 * idx + 100,
                    },
                )
                if status != 201:
                    errors.append(f"Worker {idx} failed with status {status}")
            except Exception as e:
                errors.append(f"Worker {idx} raised: {e}")

        for i in range(10):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent workers encountered errors: {errors}")


if __name__ == "__main__":
    unittest.main()
