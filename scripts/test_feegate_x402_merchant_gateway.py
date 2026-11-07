#!/usr/bin/env python3
"""Offline tests: python3 -m unittest discover -s scripts -p 'test_feegate_x402_merchant_gateway.py' -v.

Facilitator fixtures simulate /verify and /settle; no test signs or sends funds.
Loopback integration exercises the real stdlib HTTP client and merchant server.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feegate_x402_merchant_gateway as gateway


NOW = 1_800_000_000
PAYER = "0x" + "12" * 20
TX = "0x" + "34" * 32
DATA = '{"data":[{"name":"香港人才","count":42}]}'.encode()
SIGNATURE = "0x" + "56" * 64 + "1b"


def verifier():
    result = Mock()
    result.verify.return_value = {"isValid": True, "payer": PAYER}
    result.settle.return_value = {"success": True, "payer": PAYER, "network": gateway.BASE_NETWORK, "transaction": TX}
    return result


@contextmanager
def running_server(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("test HTTP server did not stop")


def http_request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class GatewayFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "ledger.sqlite3"
        self.ledger = gateway.Ledger(self.db_path)
        self.addCleanup(self.ledger.close)
        self.source = Mock(return_value=DATA)
        self.endpoint = gateway.Endpoint("fixture", "Private fixture data", self.source)
        self.facilitator = verifier()
        self.app = gateway.MerchantGateway([self.endpoint], self.ledger, public_url="https://merchant.example",
                                           facilitator=self.facilitator, clock=lambda: NOW)
        self.url = "https://merchant.example/api/fixture"

    def payment(self, nonce="ab", url=None):
        url = url or self.url
        invoice = self.app.invoice(self.endpoint, url)
        return {"x402Version": 2, "accepted": invoice["accepts"][0], "resource": invoice["resource"],
                "payload": {"signature": SIGNATURE, "authorization": {
                    "from": PAYER, "to": gateway.PAY_TO, "value": self.endpoint.amount,
                    "validAfter": str(NOW - 1), "validBefore": str(NOW + 59), "nonce": "0x" + nonce * 32}}}

    def paid(self, payment=None, **options):
        return self.app.handle("GET", options.pop("target", "/api/fixture"),
                               {"PAYMENT-SIGNATURE": gateway.encode_header(payment or self.payment()), **options})

    def points_key(self, points=20):
        return self.ledger.issue_key(points)

    def mcp(self, method, params=None, headers=None, request_id=1):
        return self.app.handle("POST", "/mcp", headers, gateway.json_bytes({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}))


class GatewayTests(GatewayFixture):
    def test_402_invoice_contains_base_mainnet_usdc_and_fixed_facilitator_payto(self):
        response = self.app.handle("GET", "/api/fixture")
        self.assertEqual(response.status, 402)
        invoice = json.loads(response.body)
        self.assertEqual(gateway.decode_payment(response.headers["PAYMENT-REQUIRED"]), invoice)
        self.assertEqual(invoice["x402Version"], 2)
        self.assertEqual(invoice["resource"], {"url": self.url, "description": self.endpoint.description,
                                              "mimeType": "application/json"})
        requirement = invoice["accepts"][0]
        self.assertEqual(requirement, {"scheme": "exact", "network": "eip155:8453", "amount": "50000",
                                      "asset": gateway.BASE_USDC, "payTo": gateway.PAY_TO,
                                      "maxTimeoutSeconds": 60,
                                      "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"}})
        self.assertEqual(gateway.PAY_TO, "0x230cd9cd33589bcf33cd32e5aa4f9e0c3994a63b")
        self.assertEqual(invoice["extensions"]["rmb-points"]["rmb"], "0.05")
        self.source.assert_not_called()
        self.facilitator.verify.assert_not_called()
        self.assertNotIn(DATA, response.body)

    def test_success_requires_verify_then_source_then_confirmed_settlement(self):
        sequence = []
        self.facilitator.verify.side_effect = lambda *args: sequence.append("verify") or {"isValid": True, "payer": PAYER}
        self.source.side_effect = lambda query: sequence.append("source") or DATA
        self.facilitator.settle.side_effect = lambda *args: sequence.append("settle") or {
            "success": True, "payer": PAYER, "network": gateway.BASE_NETWORK, "transaction": TX}
        response = self.paid()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, DATA)
        self.assertEqual(sequence, ["verify", "source", "settle"])
        receipt = gateway.decode_payment(response.headers["PAYMENT-RESPONSE"])
        self.assertEqual((receipt["transaction"], receipt["payer"]), (TX, PAYER))
        self.assertEqual(self.ledger.db.execute("SELECT state FROM authorizations").fetchone()[0], "settled")

    def test_malformed_payment_headers_fail_without_source_or_facilitator(self):
        for header in ("", "not base64!", "W10=", "bnVsbA==", "e30=", "x" * (gateway.MAX_PAYMENT_BYTES + 1),
                       "eyJ4NDAyVmVyc2lvbiI6MiwieDQwMlZlcnNpb24iOjJ9", "/w=="):
            with self.subTest(header=header[:30]):
                response = self.app.handle("GET", "/api/fixture", {"PAYMENT-SIGNATURE": header})
                self.assertEqual(response.status, 402)
                self.assertIn("PAYMENT-REQUIRED", response.headers)
        self.source.assert_not_called()
        self.facilitator.verify.assert_not_called()

    def test_mismatched_network_token_price_payto_domain_and_version_rejected(self):
        for field, value in (("network", "eip155:84532"), ("asset", PAYER), ("amount", "1"),
                             ("payTo", PAYER), ("scheme", "upto"), ("maxTimeoutSeconds", 500),
                             ("extra", {"name": "Counterfeit", "version": "2"})):
            payment = self.payment()
            payment["accepted"][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.paid(payment).status, 402)
        for value in (1, "2", True, None, 2.0):
            payment = self.payment()
            payment["x402Version"] = value
            self.assertEqual(self.paid(payment).status, 402)
        self.facilitator.verify.assert_not_called()

    def test_eip3009_authorization_field_validation(self):
        changes = [("to", PAYER), ("from", "0x" + "0" * 40), ("from", "invalid"),
                   ("value", "49999"), ("value", "50001"), ("value", 50000), ("value", True),
                   ("value", "050000"), ("value", str(2**256)), ("value", "-1"),
                   ("nonce", "0x1234"), ("nonce", "0x" + "zz" * 32), ("nonce", None)]
        for field, value in changes:
            payment = self.payment()
            payment["payload"]["authorization"][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(self.paid(payment).status, 402)
        for field in ("from", "to", "value", "nonce", "validAfter", "validBefore"):
            payment = self.payment()
            del payment["payload"]["authorization"][field]
            self.assertEqual(self.paid(payment).status, 402)
        self.facilitator.verify.assert_not_called()

    def test_expired_future_overlong_and_boundary_authorizations_rejected(self):
        for after, before in ((NOW - 60, NOW), (NOW, NOW + 59), (NOW + 1, NOW + 59),
                              (NOW - 1, NOW + 61), (NOW - 3, NOW - 2), (NOW + 3, NOW + 2)):
            payment = self.payment()
            payment["payload"]["authorization"].update(validAfter=str(after), validBefore=str(before))
            with self.subTest(after=after, before=before):
                self.assertEqual(self.paid(payment).status, 402)
        payment = self.payment()
        payment["payload"]["authorization"]["validBefore"] = str(NOW + 60)
        self.assertEqual(self.paid(payment).status, 200)

    def test_malformed_signatures_and_nested_values_rejected(self):
        for signature in (None, "0x", "0x" + "0" * 128, "0x" + "g" * 130, 123):
            payment = self.payment()
            payment["payload"]["signature"] = signature
            self.assertEqual(self.paid(payment).status, 402)
        for field in ("payload", "resource"):
            for value in (None, [], "wrong"):
                payment = self.payment()
                payment[field] = value
                self.assertEqual(self.paid(payment).status, 402)
        self.facilitator.verify.assert_not_called()

    def test_forged_well_shaped_signature_requires_actual_facilitator_verification(self):
        self.facilitator.verify.return_value = {"isValid": False, "invalidReason": "invalid_exact_evm_payload_signature"}
        payment = self.payment()
        payment["payload"]["signature"] = "0x" + "00" * 65
        response = self.paid(payment)
        self.assertEqual(response.status, 402)
        self.assertEqual(json.loads(response.body)["error"], "payment_verification_failed")
        self.facilitator.verify.assert_called_once()
        self.facilitator.settle.assert_not_called()
        self.source.assert_not_called()
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM authorizations").fetchone()[0], 0)

    def test_verifier_response_is_strict_and_payer_bound(self):
        for result in ({"isValid": "true", "payer": PAYER}, {"isValid": 1, "payer": PAYER}, [],
                       {"isValid": True}, {"isValid": True, "payer": gateway.PAY_TO}):
            self.facilitator.verify.return_value = result
            with self.subTest(result=result):
                self.assertIn(self.paid().status, (402, 502))
        self.source.assert_not_called()
        self.facilitator.settle.assert_not_called()

    def test_verify_unavailable_never_releases_data_and_can_retry_before_settle(self):
        self.facilitator.verify.side_effect = gateway.GatewayError(503, "facilitator_unavailable")
        self.assertEqual(self.paid().status, 503)
        self.source.assert_not_called()
        self.facilitator.verify.side_effect = None
        self.assertEqual(self.paid().status, 200)

    def test_unconfigured_facilitator_fails_closed(self):
        self.app.facilitator = gateway.Facilitator()
        response = self.paid()
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body)["error"], "facilitator_not_configured")
        self.source.assert_not_called()

    def test_source_failure_does_not_settle_and_allows_unspent_nonce_retry(self):
        self.source.side_effect = OSError("private filename must not escape")
        response = self.paid()
        self.assertEqual(response.status, 502)
        self.assertNotIn(b"private filename", response.body)
        self.facilitator.settle.assert_not_called()
        self.source.side_effect = None
        self.assertEqual(self.paid().status, 200)

    def test_failed_settlement_never_releases_data_or_reuses_nonce(self):
        self.facilitator.settle.return_value = {"success": False, "errorReason": "insufficient_funds"}
        response = self.paid()
        self.assertEqual(response.status, 402)
        self.assertNotIn(DATA, response.body)
        self.assertEqual(self.paid().status, 402)
        self.facilitator.settle.assert_called_once()

    def test_settlement_timeout_persists_reservation_and_blocks_duplicate_charge(self):
        self.facilitator.settle.side_effect = gateway.GatewayError(503, "facilitator_unavailable")
        self.assertEqual(self.paid().status, 503)
        other = gateway.Ledger(self.db_path)
        self.addCleanup(other.close)
        self.app.ledger = other
        self.assertEqual(self.paid().status, 402)
        self.facilitator.verify.assert_called_once()
        self.facilitator.settle.assert_called_once()
        self.assertEqual(other.db.execute("SELECT state FROM authorizations").fetchone()[0], "settling")

    def test_pending_settlement_is_unavailable_and_requires_reconciliation(self):
        self.facilitator.settle.return_value = {"success": False, "errorReason": "settlement_pending",
                                               "transaction": TX, "network": gateway.BASE_NETWORK}
        response = self.paid()
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body)["error"], "settlement_pending")
        self.assertNotIn("PAYMENT-REQUIRED", response.headers)
        self.assertEqual(self.ledger.db.execute("SELECT state FROM authorizations").fetchone()[0], "pending")
        self.assertEqual(self.paid().status, 402)
        self.facilitator.settle.assert_called_once()

    def test_settlement_receipt_network_payer_transaction_and_boolean_are_checked(self):
        for index, changes in enumerate(({"network": "eip155:1"}, {"payer": gateway.PAY_TO},
                                         {"payer": None}, {"transaction": "abc"}, {"transaction": None},
                                         {"success": "true"}, {"success": 1})):
            self.facilitator.settle.return_value = {"success": True, "payer": PAYER, "network": gateway.BASE_NETWORK,
                                                     "transaction": TX, **changes}
            with self.subTest(changes=changes):
                response = self.paid(self.payment(nonce=f"{index:02x}"))
                self.assertIn(response.status, (402, 502))
                self.assertNotIn(DATA, response.body)

    def test_payment_replay_blocked_after_restart_and_across_resources(self):
        self.assertEqual(self.paid().status, 200)
        with gateway_context(self.db_path) as other:
            second = gateway.MerchantGateway([self.endpoint], other, public_url=self.app.public_url,
                                              facilitator=self.facilitator, clock=lambda: NOW)
            response = second.handle("GET", "/api/fixture", {"PAYMENT-SIGNATURE": gateway.encode_header(self.payment())})
            self.assertEqual(json.loads(response.body)["error"], "authorization_already_used")
        self.app.endpoints["other"] = gateway.Endpoint("other", self.endpoint.description, self.source)
        payment = self.payment(url="https://merchant.example/api/other")
        # Nonce identity is independent of route, signature encoding and casing.
        payment["payload"]["authorization"]["nonce"] = payment["payload"]["authorization"]["nonce"].upper().replace("0X", "0x")
        response = self.app.handle("GET", "/api/other", {"PAYMENT-SIGNATURE": gateway.encode_header(payment)})
        self.assertEqual(json.loads(response.body)["error"], "authorization_already_used")
        self.facilitator.settle.assert_called_once()

    def test_concurrent_same_payment_settles_once(self):
        barrier = threading.Barrier(8)
        def run(_):
            barrier.wait(timeout=5)
            return self.paid().status
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(run, range(8)))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(402), 7)
        self.facilitator.settle.assert_called_once()

    def test_api_key_rmb_debits_exactly_and_survives_restart(self):
        key, digest = self.points_key(12)
        for expected in (7, 2):
            response = self.app.handle("GET", "/api/fixture", {"X-API-Key": key})
            self.assertEqual(response.status, 200)
            self.assertEqual(response.body, DATA)
            self.assertEqual(response.headers["X-Points-Remaining"], str(expected))
        with gateway_context(self.db_path) as other:
            self.assertEqual(other.check_points(key, 1), 2)
        debits = self.ledger.db.execute("SELECT delta,balance_after FROM point_events WHERE resource=? ORDER BY rowid", (self.url,)).fetchall()
        self.assertEqual(debits, [(-5, 7), (-5, 2)])
        self.assertNotIn(key, "\n".join(self.ledger.db.iterdump()))
        self.assertEqual(digest, hashlib.sha256(key.encode()).hexdigest())
        self.facilitator.verify.assert_not_called()

    def test_bearer_key_works_but_query_key_does_not_authenticate(self):
        key, _ = self.points_key()
        self.assertEqual(self.app.handle("GET", "/api/fixture", {"Authorization": "Bearer " + key}).status, 200)
        self.assertEqual(self.app.handle("GET", "/api/fixture?api_key=" + key).status, 402)
        self.assertEqual(self.app.handle("GET", "/api/fixture", {"Authorization": "Basic " + key}).status, 401)

    def test_insufficient_unknown_revoked_and_malformed_keys_do_not_charge(self):
        key, digest = self.points_key(4)
        response = self.app.handle("GET", "/api/fixture", {"X-API-Key": key})
        self.assertEqual(response.status, 402)
        self.assertEqual(json.loads(response.body)["error"], "insufficient_rmb_points")
        self.assertIn("PAYMENT-REQUIRED", response.headers)
        self.ledger.revoke(digest)
        for invalid in (key, "unknown-api-key-0123456789", "", "abc", "界" * 20, "a " * 20):
            self.assertEqual(self.app.handle("GET", "/api/fixture", {"X-API-Key": invalid}).status, 401)
        self.source.assert_not_called()

    def test_source_failure_does_not_debit_points(self):
        key, _ = self.points_key()
        self.source.side_effect = ValueError("bad source")
        self.assertEqual(self.app.handle("GET", "/api/fixture", {"X-API-Key": key}).status, 502)
        self.assertEqual(self.ledger.check_points(key, 1), 20)

    def test_concurrent_points_cannot_overdraw_across_database_connections(self):
        key, _ = self.points_key(15)
        other = gateway.Ledger(self.db_path)
        self.addCleanup(other.close)
        barrier = threading.Barrier(10)
        def debit(index):
            barrier.wait(timeout=5)
            try:
                (self.ledger if index % 2 else other).debit(key, 5, self.url)
                return 200
            except gateway.GatewayError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=10) as pool:
            statuses = list(pool.map(debit, range(10)))
        self.assertEqual(statuses.count(200), 3)
        self.assertEqual(statuses.count(402), 7)
        self.assertEqual(self.ledger.db.execute("SELECT balance FROM accounts").fetchone()[0], 0)

    def test_credit_revoke_and_integer_point_limits(self):
        key, digest = self.points_key(0)
        self.assertEqual(self.ledger.credit(digest, 10), 10)
        self.assertEqual(self.ledger.check_points(key, 1), 10)
        for invalid in (-1, 0, True, 1.5, "100", None, 2**63):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.ledger.credit(digest, invalid)
        self.ledger.revoke(digest)
        with self.assertRaises(ValueError):
            self.ledger.credit(digest, 10)
        with self.assertRaises(gateway.GatewayError):
            self.ledger.debit(key, 1, self.url)

    def test_ambiguous_payment_credentials_rejected_without_fallback(self):
        key, _ = self.points_key()
        for headers in ({"X-API-Key": key, "Authorization": "Bearer " + key},
                        {"X-API-Key": key, "PAYMENT-SIGNATURE": "garbage"},
                        {"Authorization": "Bearer " + key, "PAYMENT-SIGNATURE": "garbage"}):
            self.assertEqual(self.app.handle("GET", "/api/fixture", headers).status, 400)
        self.assertEqual(self.ledger.check_points(key, 1), 20)
        self.source.assert_not_called()

    def test_query_and_external_origin_binding_ignore_host_spoofing(self):
        response = self.app.handle("GET", "/api/fixture?region=HK", {"Host": "attacker.example"})
        invoice = json.loads(response.body)
        self.assertEqual(invoice["resource"]["url"], self.url + "?region=HK")
        payment = self.payment()
        self.assertEqual(self.paid(payment, target="/api/fixture?region=HK").status, 402)
        self.assertEqual(self.paid(self.payment(url=self.url + "?region=HK"), target="/api/fixture?region=HK").status, 200)
        self.source.assert_called_once_with("region=HK")

    def test_free_discovery_matches_routes_and_exports_valid_json(self):
        for path in ("/.well-known/agent-card.json", "/.well-known/mcp-manifest.json", "/health"):
            response = self.app.handle("GET", path)
            self.assertEqual(response.status, 200)
            self.assertIsInstance(json.loads(response.body), dict)
        card, manifest = self.app.discovery()
        self.assertEqual(card["skills"][0]["id"], "fixture")
        self.assertEqual(manifest["transport"]["url"], "https://merchant.example/mcp")
        self.assertEqual(manifest["endpoints"][0]["paymentRequired"]["accepts"][0]["payTo"], gateway.PAY_TO)
        self.app.export_discovery(self.tmp.name)
        for filename, expected in (("agent-card.json", card), ("mcp-manifest.json", manifest)):
            self.assertEqual(json.loads((Path(self.tmp.name) / ".well-known" / filename).read_bytes()), expected)
        self.source.assert_not_called()

    def test_unknown_paths_traversal_and_unsupported_methods_never_load_or_charge(self):
        key, _ = self.points_key()
        for path in ("/", "/api/unknown", "/api/../secret", "/api/%2e%2e/secret", "/.well-known/secret"):
            self.assertEqual(self.app.handle("GET", path, {"X-API-Key": key}).status, 404)
        for method in ("POST", "HEAD", "DELETE", "PUT", "OPTIONS"):
            self.assertEqual(self.app.handle(method, "/api/fixture", {"X-API-Key": key}).status, 405)
        for path in ("http://attacker/api/fixture", "//attacker/api/fixture", "/api/fixture#fragment"):
            self.assertEqual(self.app.handle("GET", path).status, 400)
        self.assertEqual(self.ledger.check_points(key, 1), 20)
        self.source.assert_not_called()

    def test_mcp_initialize_ping_and_tool_discovery_are_free(self):
        response = self.mcp("initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(json.loads(response.body)["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(json.loads(self.mcp("ping").body)["result"], {})
        self.assertEqual(json.loads(self.mcp("tools/list").body)["result"]["tools"][0]["name"], "fixture")
        self.assertEqual(json.loads(self.mcp("unknown").body)["error"]["code"], -32601)
        self.source.assert_not_called()

    def test_mcp_initialize_requires_a_string_protocol_version(self):
        for version in (None, [], {}, True, 1):
            with self.subTest(version=version):
                self.assertEqual(self.mcp("initialize", {"protocolVersion": version}).status, 400)

    def test_mcp_paid_tools_call_supports_points_and_x402(self):
        params = {"name": "fixture", "arguments": {"query": "region=HK"}}
        response = self.mcp("tools/call", params)
        self.assertEqual(response.status, 402)
        self.assertEqual(json.loads(response.body)["resource"]["url"], "https://merchant.example/mcp")
        key, _ = self.points_key()
        response = self.mcp("tools/call", params, {"X-API-Key": key})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["result"]["content"][0]["text"].encode(), DATA)
        self.assertEqual(response.headers["X-Points-Charged"], "5")
        payment = self.payment(url="https://merchant.example/mcp")
        response = self.mcp("tools/call", params, {"PAYMENT-SIGNATURE": gateway.encode_header(payment)}, request_id="paid-1")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["id"], "paid-1")
        self.assertIn("PAYMENT-RESPONSE", response.headers)

    def test_mcp_invalid_json_arguments_notifications_and_binary_results(self):
        for raw in (b"not json", b"[]", b'{"jsonrpc":"2.0","jsonrpc":"2.0"}', b'{"jsonrpc":"2.0","method":"ping","id":true}'):
            self.assertEqual(self.app.handle("POST", "/mcp", body=raw).status, 400)
        for arguments in ({"unexpected": 1}, {"query": "\r\n"}, {"query": "#other"}, {"query": 1}, []):
            self.assertEqual(self.mcp("tools/call", {"name": "fixture", "arguments": arguments}).status, 400)
        self.assertEqual(self.mcp("tools/call", {"name": "unknown"}).status, 400)
        response = self.app.handle("POST", "/mcp", body=gateway.json_bytes({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(response.status, 202)
        self.source.return_value = b"\x00\xff\x80"
        key, _ = self.points_key()
        result = json.loads(self.mcp("tools/call", {"name": "fixture"}, {"X-API-Key": key}).body)["result"]
        self.assertEqual(result["content"][0]["resource"]["blob"], "AP+A")

    def test_mcp_origin_is_validated_before_authentication_or_billing(self):
        key, _ = self.points_key()
        for origin, status in (("https://attacker.example", 403), ("null", 403), (self.app.public_url, 200)):
            response = self.mcp("tools/call", {"name": "fixture"}, {"X-API-Key": key, "Origin": origin})
            self.assertEqual(response.status, status)
        self.assertEqual(self.ledger.check_points(key, 1), 15)

    def test_source_size_type_and_missing_file_fail_before_charging(self):
        key, _ = self.points_key()
        for value in ({"data": []}, "not bytes", DATA * 3):
            self.source.return_value = value
            with patch.object(gateway, "MAX_DATA_BYTES", len(DATA) * 2):
                self.assertEqual(self.app.handle("GET", "/api/fixture", {"X-API-Key": key}).status, 502)
        self.assertEqual(self.ledger.check_points(key, 1), 20)
        endpoint = gateway.Endpoint("missing", "Absent", Path(self.tmp.name) / "absent.json")
        with self.assertRaises(gateway.GatewayError) as error:
            endpoint.load()
        self.assertEqual(error.exception.status, 502)

    def test_storage_failure_never_releases_paid_content(self):
        with patch.object(self.ledger, "reserve", side_effect=sqlite3.OperationalError("disk full")):
            self.assertEqual(self.paid().status, 503)
        self.facilitator.verify.assert_not_called()
        self.source.assert_not_called()


@contextmanager
def gateway_context(path):
    ledger = gateway.Ledger(path)
    try:
        yield ledger
    finally:
        ledger.close()


class ConfigurationTests(unittest.TestCase):
    def test_endpoint_and_origin_validation(self):
        for options in ({"name": "../secret"}, {"name": ""}, {"name": "x y"}, {"amount": "0"},
                        {"amount": "0.1"}, {"points": 0}, {"points": True}, {"timeout_seconds": 0},
                        {"timeout_seconds": True}, {"mime_type": "text/html\r\nHeader: value"},
                        {"source": "file:///etc/passwd"}, {"source": "https://user:secret@example/data"}):
            args = {"name": "valid", "description": "Valid", "source": lambda q: DATA, **options}
            with self.subTest(options=options), self.assertRaises(ValueError):
                gateway.Endpoint(**args)
        for url in ("https://a.example/path", "https://a.example?query=1", "javascript:bad", "https://a.example/#fragment"):
            with self.assertRaises(ValueError):
                gateway.safe_url(url, origin=True)
        for url in ("http://example.com/facilitator", "https://user:secret@example.com", "https://example.com/\r\n"):
            with self.assertRaises(ValueError):
                gateway.Facilitator(url)
        with gateway_context(":memory:") as ledger:
            endpoint = gateway.Endpoint("fixture", "Fixture", lambda q: DATA)
            with self.assertRaises(ValueError):
                gateway.MerchantGateway([endpoint, endpoint], ledger)

    def test_uint256_and_strict_json_reject_ambiguity(self):
        self.assertEqual(gateway.uint256(str(2**256 - 1)), 2**256 - 1)
        for value in (True, 1, "01", "-1", "1e3", str(2**256)):
            with self.assertRaises(ValueError):
                gateway.uint256(value)
        for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
            with self.assertRaises(ValueError):
                gateway.strict_json(raw)

    def test_cli_issue_credit_revoke_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "admin.sqlite3")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(gateway.main(["--db", db, "--issue-key", "100"]), 0)
            key = json.loads(output.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(gateway.main(["--db", db, "--credit-key", key["keyDigest"], "--credit-points", "25"]), 0)
            with gateway_context(db) as ledger:
                self.assertEqual(ledger.check_points(key["apiKey"], 1), 125)
            self.assertEqual(gateway.main(["--db", db, "--revoke-key", key["keyDigest"]]), 0)
            self.assertEqual(gateway.main(["--db", db, "--dataset", "prices=" + tmp + "/data.csv",
                                          "--api", "local=http://127.0.0.1:9000/data", "--export-discovery", tmp]), 0)
            manifest = json.loads((Path(tmp) / ".well-known/mcp-manifest.json").read_bytes())
            self.assertEqual([t["name"] for t in manifest["tools"]], ["prices", "local"])
            self.assertEqual(manifest["endpoints"][0]["paymentRequired"]["resource"]["mimeType"], "text/csv")

    def test_cli_invalid_configuration_returns_error_without_binding(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(io.StringIO()):
            for args in (("--dataset", "bad"), ("--points", "-1"), ("--amount", "0"),
                         ("--issue-key", "-10"), ("--facilitator-url", "http://untrusted.example")):
                self.assertEqual(gateway.main(["--db", str(Path(tmp) / "bad.sqlite3"), *args]), 1)


class HTTPIntegrationTests(GatewayFixture):
    def test_real_http_invoice_points_head_and_security_headers(self):
        key, _ = self.points_key()
        with running_server(gateway.make_server(self.app, port=0)) as server:
            status, headers, body = http_request(server, "GET", "/api/fixture")
            self.assertEqual(status, 402)
            self.assertEqual(gateway.decode_payment(headers["PAYMENT-REQUIRED"]), json.loads(body))
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(int(headers["Content-Length"]), len(body))
            self.assertNotIn(key.encode(), body)
            status, _, body = http_request(server, "GET", "/api/fixture", headers={"X-API-Key": key})
            self.assertEqual((status, body), (200, DATA))
            status, headers, body = http_request(server, "HEAD", "/api/fixture", headers={"X-API-Key": key})
            self.assertEqual((status, body, headers["Allow"]), (405, b"", "GET"))
            self.assertEqual(self.ledger.check_points(key, 1), 15)

    def test_real_http_rejects_duplicate_credentials_and_oversized_bodies(self):
        key, _ = self.points_key()
        with running_server(gateway.make_server(self.app, port=0)) as server:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.putrequest("GET", "/api/fixture")
                connection.putheader("X-API-Key", key)
                connection.putheader("X-API-Key", key)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                connection.close()
            status, _, _ = http_request(server, "POST", "/mcp", headers={"Content-Length": str(gateway.MAX_BODY_BYTES + 1)})
            self.assertEqual(status, 413)
            status, _, _ = http_request(server, "POST", "/mcp", headers={"Transfer-Encoding": "chunked"})
            self.assertEqual(status, 400)
        self.source.assert_not_called()

    def test_real_http_mcp_initialize_discovery_and_paid_call(self):
        key, _ = self.points_key()
        with running_server(gateway.make_server(self.app, port=0)) as server:
            body = gateway.json_bytes({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2025-06-18"}})
            status, _, raw = http_request(server, "POST", "/mcp", body, {"Content-Type": "application/json"})
            self.assertEqual((status, json.loads(raw)["result"]["protocolVersion"]), (200, "2025-06-18"))
            body = gateway.json_bytes({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "fixture"}})
            status, headers, _ = http_request(server, "POST", "/mcp", body)
            self.assertEqual(status, 402)
            self.assertIn("PAYMENT-REQUIRED", headers)
            status, headers, raw = http_request(server, "POST", "/mcp", body, {"X-API-Key": key})
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(json.loads(raw)["result"]["content"][0]["text"].encode(), DATA)

    def test_actual_http_facilitator_verify_settle_and_fixed_upstream_api(self):
        calls = []
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                calls.append(("source", self.path))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(DATA)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, body, self.headers.get("Authorization")))
                payment = body["paymentPayload"]
                if self.path == "/verify":
                    result = {"isValid": payment["payload"]["signature"] == SIGNATURE, "payer": PAYER}
                else:
                    result = {"success": True, "payer": PAYER, "network": gateway.BASE_NETWORK, "transaction": TX}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(gateway.json_bytes(result))
        with running_server(ThreadingHTTPServer(("127.0.0.1", 0), Upstream)) as upstream:
            origin = f"http://127.0.0.1:{upstream.server_port}"
            self.app.facilitator = gateway.Facilitator(origin, "test-facilitator-secret")
            self.app.endpoints["fixture"] = gateway.Endpoint("fixture", "Private fixture data", origin + "/data?fixed=1")
            with running_server(gateway.make_server(self.app, port=0)) as merchant:
                payment = self.payment(url=self.url + "?region=HK")
                status, headers, body = http_request(merchant, "GET", "/api/fixture?region=HK",
                    headers={"PAYMENT-SIGNATURE": gateway.encode_header(payment)})
                self.assertEqual((status, body), (200, DATA))
                self.assertEqual(gateway.decode_payment(headers["PAYMENT-RESPONSE"])["transaction"], TX)
        self.assertEqual([entry[0] for entry in calls], ["/verify", "source", "/settle"])
        self.assertEqual(calls[1][1], "/data?fixed=1&region=HK")
        self.assertEqual(calls[0][1]["paymentRequirements"]["payTo"], gateway.PAY_TO)
        self.assertEqual(calls[0][1]["x402Version"], 2)
        self.assertEqual(calls[0][2], "Bearer test-facilitator-secret")
        self.assertNotIn(b"test-facilitator-secret", body)

    def test_local_dataset_binary_file_is_served_exactly(self):
        path = Path(self.tmp.name) / "private.csv"
        data = b"gpu,price\r\nA100,1.25\r\n"
        path.write_bytes(data)
        self.app.endpoints["fixture"] = gateway.Endpoint("fixture", "CSV dataset", path, mime_type="text/csv")
        key, _ = self.points_key()
        with running_server(gateway.make_server(self.app, port=0)) as server:
            status, headers, body = http_request(server, "GET", "/api/fixture", headers={"X-API-Key": key})
            self.assertEqual((status, body, headers["Content-Type"]), (200, data, "text/csv"))

    def test_facilitator_redirect_and_invalid_json_fail_closed(self):
        calls = []
        class BadFacilitator(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                calls.append(self.path)
                if self.path.startswith("/redirect"):
                    self.send_response(307)
                    self.send_header("Location", "/stolen")
                else:
                    self.send_response(200)
                self.end_headers()
                self.wfile.write(b"not JSON")
        with running_server(ThreadingHTTPServer(("127.0.0.1", 0), BadFacilitator)) as server:
            for suffix in ("/redirect", "/bad"):
                self.app.facilitator = gateway.Facilitator(f"http://127.0.0.1:{server.server_port}" + suffix)
                self.assertEqual(self.paid().status, 503)
        self.assertEqual(calls, ["/redirect/verify", "/bad/verify"])
        self.source.assert_not_called()


if __name__ == "__main__":
    unittest.main()
