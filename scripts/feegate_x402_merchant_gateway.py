#!/usr/bin/env python3
"""FeeGate: standard-library x402 v2 merchant gateway (Base mainnet USDC).

Run::

    python3 scripts/feegate_x402_merchant_gateway.py --port 8402
    python3 scripts/feegate_x402_merchant_gateway.py --issue-key 1000
    python3 scripts/feegate_x402_merchant_gateway.py --dataset prices=/tmp/prices.csv \
        --api local-api=http://127.0.0.1:9000/data --public-url https://data.example

GET /api/<name> wraps a configured file, fixed upstream GET API, or embedded
Python callable. Query parameters are forwarded only to configured upstreams.
Defaults wrap the GPU radar, Hong Kong talent and CVE dataset files; missing
files return 502 after authentication, never synthetic or free paid data.
GET /.well-known/agent-card.json and /.well-known/mcp-manifest.json are generated
from the actual routes. The card describes REST discovery, not an A2A RPC
implementation. POST /mcp provides stateless JSON MCP initialize, ping,
tools/list and paid tools/call (protocol 2025-03-26 or 2025-06-18).

Payment uses PAYMENT-REQUIRED, PAYMENT-SIGNATURE and PAYMENT-RESPONSE, all base64
JSON. Exact USDC amounts are integers in millionths of a dollar. The fixed PayTo
is the merchant/facilitator recipient, not the facilitator HTTP service URL.
Set FEEGATE_FACILITATOR_URL (or --facilitator-url) to a trusted x402 v2 service.
Local checks enforce the EIP-3009 fields, recipient, amount, window and signature
encoding. The facilitator /verify MUST verify the EIP-712 signature, balance,
on-chain nonce and transfer simulation; /settle MUST confirm the transfer.
The Python standard library has no Ethereum signature verifier. This server
delegates cryptography and chain access; it never treats field checks as proof
of payment and never releases data on verify alone. No wallet private key is
needed. HTTPS is required for facilitators except loopback development servers.

Domestic fallback: X-API-Key or Authorization: Bearer uses prepaid integer RMB
points (100 points = CNY 1, independently priced from USDC). Issue/credit/revoke
keys locally using the CLI; there is no public top-up endpoint. Only SHA-256
key digests are stored. Source failures before settlement/debit do not charge.
SQLite coordinates point debits and one-use EIP-3009 nonces across threads,
processes and restarts. A nonce is reserved before verify and marked settling
BEFORE the settle request. Uncertain/failed settlement and interrupted requests
remain blocked for operator reconciliation, preventing a retry from charging
twice. Each successful request needs a fresh authorization; delivery retries
are not free. Disconnected clients may still have paid, as with ordinary HTTP.

Deploy behind HTTPS with --public-url set to the externally visible origin.
The default bind is loopback. All credentials are headers, never query values.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


PAY_TO = "0x230cd9cd33589bcf33cd32e5aa4f9e0c3994a63b"
BASE_NETWORK = "eip155:8453"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MAX_DATA_BYTES = 8 * 1024 * 1024
MAX_PAYMENT_BYTES = 16 * 1024
MAX_BODY_BYTES = 32 * 1024
MAX_POINTS = 2**63 - 1
POINTS_PER_RMB = 100
ROOT = Path(__file__).resolve().parents[1]


class GatewayError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def encode_header(value):
    return base64.b64encode(json_bytes(value)).decode("ascii")


def decode_payment(value):
    try:
        if not isinstance(value, str) or not 0 < len(value) <= MAX_PAYMENT_BYTES:
            raise ValueError("payment header size")
        raw = base64.b64decode(value, validate=True)
        result = strict_json(raw)
        if not isinstance(result, dict):
            raise ValueError("payment must be an object")
        return result
    except (ValueError, UnicodeError, RecursionError, binascii.Error) as error:
        raise GatewayError(402, "invalid_payment_encoding") from error


def uint256(value):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,77}", value):
        raise ValueError("expected canonical uint256 string")
    result = int(value)
    if result >= 2**256:
        raise ValueError("uint256 overflow")
    return result


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError("invalid EVM address")
    if int(value[2:], 16) == 0:
        raise ValueError("zero address")
    return value.lower()


def positive_points(value, allow_zero=False):
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= MAX_POINTS:
        raise ValueError("invalid integer points")
    return value


def safe_url(value, *, facilitator=False, origin=False):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 for c in value):
        raise ValueError("invalid URL")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            or parts.password or parts.fragment or not parts.port and parts.netloc.endswith(":")):
        raise ValueError("expected an HTTP(S) URL without credentials or fragments")
    if facilitator:
        if parts.query:
            raise ValueError("facilitator URL must not contain a query")
        if parts.scheme != "https" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("facilitator requires HTTPS")
    if origin and (parts.path not in {"", "/"} or parts.query):
        raise ValueError("public URL must be an origin")
    return value.rstrip("/") if origin else value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_bytes(url, *, body=None, headers=None, limit=MAX_DATA_BYTES, timeout=10):
    request = urllib.request.Request(url, data=body, headers={
        "Accept": "application/json", "User-Agent": "FeeGate-x402/1.0", **(headers or {})})
    # Do not follow redirects, including redirects of facilitator credentials.
    try:
        response = urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        error.close()
        raise
    with response:
        raw = response.read(limit + 1)
        if len(raw) > limit or not 200 <= response.status < 300:
            raise ValueError("upstream response exceeds bounds or failed")
        return raw


@dataclass(frozen=True)
class Endpoint:
    name: str
    description: str
    source: object  # Path, fixed HTTP(S) URL, or callable(query) -> bytes
    amount: str = "50000"
    points: int = 5
    mime_type: str = "application/json"
    timeout_seconds: int = 60

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.name):
            raise ValueError("invalid endpoint name")
        if not isinstance(self.description, str) or not 1 <= len(self.description) <= 1000:
            raise ValueError("invalid description")
        if uint256(self.amount) == 0:
            raise ValueError("amount must be positive atomic USDC")
        positive_points(self.points)
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("invalid payment timeout")
        if not isinstance(self.mime_type, str) or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", self.mime_type):
            raise ValueError("invalid MIME type")
        if isinstance(self.source, str):
            safe_url(self.source)
        elif not isinstance(self.source, Path) and not callable(self.source):
            raise ValueError("source must be a Path, HTTP URL or callable")

    @property
    def path(self):
        return "/api/" + self.name

    def load(self, query=""):
        try:
            if isinstance(self.source, Path):
                with self.source.open("rb") as handle:
                    result = handle.read(MAX_DATA_BYTES + 1)
            elif isinstance(self.source, str):
                url = self.source + (("&" if "?" in self.source else "?") + query if query else "")
                result = fetch_bytes(url)
            else:
                result = self.source(query)
            if not isinstance(result, bytes) or len(result) > MAX_DATA_BYTES:
                raise ValueError("source must return bounded bytes")
            return result
        except Exception as error:
            raise GatewayError(502, "source_unavailable") from error


class Facilitator:
    """Trusted x402 v2 verifier/settler. No offline or permissive verification."""

    def __init__(self, url=None, token=None):
        self.url = safe_url(url, facilitator=True).rstrip("/") if url else None
        self.token = token

    def request(self, operation, payment, requirements):
        if not self.url:
            raise GatewayError(503, "facilitator_not_configured")
        try:
            body = json_bytes({"x402Version": 2, "paymentPayload": payment,
                               "paymentRequirements": requirements})
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            raw = fetch_bytes(self.url + "/" + operation, body=body, headers=headers,
                              limit=64 * 1024)
            result = strict_json(raw)
            if not isinstance(result, dict):
                raise ValueError("expected facilitator object")
            return result
        except Exception as error:
            raise GatewayError(503, "facilitator_unavailable") from error

    def verify(self, payment, requirements):
        return self.request("verify", payment, requirements)

    def settle(self, payment, requirements):
        return self.request("settle", payment, requirements)


def validate_authorization(payment, requirements, resource_url, now):
    """Validate structure/policy; cryptographic verification is mandatory next."""
    try:
        if type(payment.get("x402Version")) is not int or payment["x402Version"] != 2:
            raise ValueError("unsupported version")
        if json_bytes(payment.get("accepted")) != json_bytes(requirements):
            raise ValueError("requirements mismatch")
        if "resource" in payment and payment["resource"].get("url") != resource_url:
            raise ValueError("resource mismatch")
        payload = payment["payload"]
        auth = payload["authorization"]
        payer = address(auth["from"])
        if address(auth["to"]) != address(requirements["payTo"]):
            raise ValueError("recipient mismatch")
        if uint256(auth["value"]) != uint256(requirements["amount"]):
            raise ValueError("amount mismatch")
        after, before = uint256(auth["validAfter"]), uint256(auth["validBefore"])
        if not after < now < before or before > now + requirements["maxTimeoutSeconds"]:
            raise ValueError("authorization outside validity window")
        if not isinstance(auth["nonce"], str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", auth["nonce"]):
            raise ValueError("invalid nonce")
        signature = payload["signature"]
        if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
            raise ValueError("expected a 65-byte EIP-3009 signature")
        # Cryptographic recovery, replay-on-chain, balance and simulation are
        # performed by /verify. An arbitrary correctly shaped signature fails it.
        return payer, auth["nonce"].lower()
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
        raise GatewayError(402, "invalid_authorization") from error


class Ledger:
    """Durable prepaid balances and globally unique Base USDC authorizations."""

    def __init__(self, path=":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS accounts (
                digest TEXT PRIMARY KEY, balance INTEGER NOT NULL CHECK(balance >= 0),
                active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS point_events (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, delta INTEGER NOT NULL,
                balance_after INTEGER NOT NULL, resource TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS authorizations (
                network TEXT NOT NULL, asset TEXT NOT NULL, payer TEXT NOT NULL, nonce TEXT NOT NULL,
                state TEXT NOT NULL, resource TEXT NOT NULL, receipt TEXT, created REAL NOT NULL,
                PRIMARY KEY(network, asset, payer, nonce));
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        with self.lock:
            self.db.close()

    @staticmethod
    def digest(key):
        if not isinstance(key, str) or not 16 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key):
            raise GatewayError(401, "invalid_api_key")
        return hashlib.sha256(key.encode("ascii")).hexdigest()

    def issue_key(self, points=0):
        positive_points(points, allow_zero=True)
        secret = "fg_" + secrets.token_urlsafe(32)
        digest = self.digest(secret)
        with self.transaction():
            self.db.execute("INSERT INTO accounts(digest,balance) VALUES (?,?)", (digest, points))
            self._event(digest, points, points, "admin:issue")
        return secret, digest

    def _event(self, digest, delta, balance, resource):
        self.db.execute("INSERT INTO point_events VALUES (?,?,?,?,?,?)",
                        (secrets.token_hex(16), digest, delta, balance, resource, time.time()))

    def credit(self, digest, points):
        positive_points(points)
        with self.transaction():
            row = self.db.execute("SELECT balance FROM accounts WHERE digest=? AND active=1", (digest,)).fetchone()
            if row is None:
                raise ValueError("unknown or revoked key")
            balance = positive_points(row[0] + points)
            self.db.execute("UPDATE accounts SET balance=? WHERE digest=?", (balance, digest))
            self._event(digest, points, balance, "admin:credit")
            return balance

    def revoke(self, digest):
        with self.transaction():
            if self.db.execute("UPDATE accounts SET active=0 WHERE digest=?", (digest,)).rowcount != 1:
                raise ValueError("unknown key")

    def _account(self, key, cost):
        digest = self.digest(key)
        row = self.db.execute("SELECT balance FROM accounts WHERE digest=? AND active=1", (digest,)).fetchone()
        if row is None:
            raise GatewayError(401, "invalid_api_key")
        if row[0] < cost:
            raise GatewayError(402, "insufficient_rmb_points")
        return digest, row[0]

    def check_points(self, key, cost):
        positive_points(cost)
        with self.lock:
            return self._account(key, cost)[1]

    def debit(self, key, cost, resource):
        positive_points(cost)
        with self.transaction():
            digest, balance = self._account(key, cost)
            balance -= cost
            self.db.execute("UPDATE accounts SET balance=? WHERE digest=?", (balance, digest))
            self._event(digest, -cost, balance, resource)
            return {"method": "rmb-points", "pointsCharged": cost, "pointsRemaining": balance,
                    "currency": "CNY", "pointsPerRmb": POINTS_PER_RMB}

    def reserve(self, payer, nonce, resource):
        with self.transaction():
            try:
                self.db.execute("INSERT INTO authorizations VALUES (?,?,?,?,?,?,NULL,?)",
                                (BASE_NETWORK, BASE_USDC.lower(), payer, nonce, "reserved", resource, time.time()))
            except sqlite3.IntegrityError as error:
                raise GatewayError(402, "authorization_already_used") from error

    def state(self, payer, nonce, state, receipt=None):
        with self.transaction():
            self.db.execute("UPDATE authorizations SET state=?, receipt=? WHERE network=? AND asset=? AND payer=? AND nonce=?",
                            (state, json_bytes(receipt).decode() if receipt else None,
                             BASE_NETWORK, BASE_USDC.lower(), payer, nonce))

    def release_unsettled(self, payer, nonce):
        with self.transaction():
            self.db.execute("DELETE FROM authorizations WHERE network=? AND asset=? AND payer=? AND nonce=? AND state='reserved'",
                            (BASE_NETWORK, BASE_USDC.lower(), payer, nonce))


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)

    @classmethod
    def json(cls, status, value, headers=None):
        return cls(status, json_bytes(value), {"Content-Type": "application/json", **(headers or {})})


class MerchantGateway:
    def __init__(self, endpoints, ledger, *, public_url="http://127.0.0.1:8402", facilitator=None, clock=time.time):
        self.public_url = safe_url(public_url, origin=True)
        self.endpoints = {}
        for endpoint in endpoints:
            if endpoint.name in self.endpoints:
                raise ValueError("duplicate endpoint")
            self.endpoints[endpoint.name] = endpoint
        if not self.endpoints:
            raise ValueError("at least one endpoint is required")
        self.ledger, self.facilitator, self.clock = ledger, facilitator or Facilitator(), clock

    def invoice(self, endpoint, resource_url, error=None):
        value = {"x402Version": 2, "resource": {"url": resource_url,
                 "description": endpoint.description, "mimeType": endpoint.mime_type},
                 "accepts": [{"scheme": "exact", "network": BASE_NETWORK, "asset": BASE_USDC,
                              "amount": endpoint.amount, "payTo": PAY_TO,
                              "maxTimeoutSeconds": endpoint.timeout_seconds,
                              "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"}}],
                 "extensions": {"rmb-points": {"headers": ["X-API-Key", "Authorization: Bearer <key>"],
                     "points": endpoint.points, "currency": "CNY", "pointsPerRmb": POINTS_PER_RMB,
                     "rmb": format(Decimal(endpoint.points) / POINTS_PER_RMB, ".2f")}}}
        if error:
            value["error"] = error
        return value

    def discovery(self):
        tools = [{"name": e.name, "description": e.description,
                  "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 4096}},
                                  "additionalProperties": False},
                  "annotations": {"readOnlyHint": True, "openWorldHint": isinstance(e.source, str)}}
                 for e in self.endpoints.values()]
        manifest = {"schemaVersion": "1.0", "name": "FeeGate Merchant Gateway",
                    "protocolVersions": ["2025-03-26", "2025-06-18"],
                    "transport": {"type": "streamable-http", "url": self.public_url + "/mcp"},
                    "tools": tools, "endpoints": [{"name": e.name, "method": "GET",
                        "url": self.public_url + e.path,
                        "paymentRequired": self.invoice(e, self.public_url + e.path)} for e in self.endpoints.values()]}
        card = {"name": "FeeGate Merchant Gateway", "version": "1.0.0", "url": self.public_url,
                "description": "Paid datasets and APIs via x402 Base USDC or prepaid RMB points.",
                "interfaces": [{"protocol": "HTTP REST", "url": self.public_url + "/api/{name}"},
                               {"protocol": "MCP", "url": self.public_url + "/mcp"}],
                "capabilities": {"streaming": False, "x402Version": 2},
                "defaultInputModes": ["application/json"], "defaultOutputModes": ["application/json"],
                "skills": [{"id": e.name, "name": e.name, "description": e.description,
                            "tags": ["data", "x402", "Base", "USDC"]} for e in self.endpoints.values()],
                "mcpManifest": self.public_url + "/.well-known/mcp-manifest.json"}
        return card, manifest

    def export_discovery(self, directory):
        directory = Path(directory) / ".well-known"
        directory.mkdir(parents=True, exist_ok=True)
        for name, value in zip(("agent-card.json", "mcp-manifest.json"), self.discovery()):
            path = directory / name
            temporary = directory / ("." + name + "." + secrets.token_hex(8))
            try:
                temporary.write_bytes(json_bytes(value) + b"\n")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def _paid(self, endpoint, query, headers, resource_url):
        payment_header = headers.get("payment-signature")
        api_key, bearer = headers.get("x-api-key"), headers.get("authorization")
        if sum(x is not None for x in (payment_header, api_key, bearer)) > 1:
            raise GatewayError(400, "ambiguous_credentials")
        if bearer is not None:
            if not bearer.startswith("Bearer "):
                raise GatewayError(401, "invalid_api_key")
            api_key = bearer[7:]
        if api_key is not None:
            self.ledger.check_points(api_key, endpoint.points)
            data = endpoint.load(query)
            receipt = self.ledger.debit(api_key, endpoint.points, resource_url)
            return Response(200, data, {"Content-Type": endpoint.mime_type,
                            "X-Payment-Method": "rmb-points", "X-Points-Charged": str(endpoint.points),
                            "X-Points-Remaining": str(receipt["pointsRemaining"])})
        if payment_header is None:
            raise GatewayError(402, "payment_required")
        invoice = self.invoice(endpoint, resource_url)
        requirements = invoice["accepts"][0]
        payment = decode_payment(payment_header)
        payer, nonce = validate_authorization(payment, requirements, resource_url, self.clock())
        self.ledger.reserve(payer, nonce, resource_url)
        try:
            verification = self.facilitator.verify(payment, requirements)
            if not isinstance(verification, dict) or verification.get("isValid") is not True:
                raise GatewayError(402, "payment_verification_failed")
            if address(verification.get("payer")) != payer:
                raise GatewayError(502, "invalid_facilitator_response")
            data = endpoint.load(query)
            # Persist this BEFORE the network call. Never retry an ambiguous settle.
            self.ledger.state(payer, nonce, "settling")
            settlement = self.facilitator.settle(payment, requirements)
            if not isinstance(settlement, dict) or settlement.get("success") is not True:
                pending = isinstance(settlement, dict) and settlement.get("errorReason") == "settlement_pending"
                self.ledger.state(payer, nonce, "pending" if pending else "failed", settlement)
                if pending:
                    raise GatewayError(503, "settlement_pending")
                raise GatewayError(402, "payment_settlement_failed")
            if (settlement.get("network") != BASE_NETWORK or address(settlement.get("payer")) != payer
                    or not isinstance(settlement.get("transaction"), str)
                    or not re.fullmatch(r"0x[0-9a-fA-F]{64}", settlement["transaction"])):
                raise GatewayError(502, "invalid_facilitator_response")
            self.ledger.state(payer, nonce, "settled", settlement)
            return Response(200, data, {"Content-Type": endpoint.mime_type,
                                      "PAYMENT-RESPONSE": encode_header(settlement)})
        except ValueError as error:
            raise GatewayError(502, "invalid_facilitator_response") from error
        finally:
            self.ledger.release_unsettled(payer, nonce)

    def _mcp(self, body, headers):
        try:
            request = strict_json(body)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError("invalid JSON-RPC")
            method, params, request_id = request.get("method"), request.get("params", {}), request.get("id")
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("invalid method or params")
            if request_id is not None and type(request_id) not in {str, int}:
                raise ValueError("invalid request ID")
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return Response.json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
        if method == "notifications/initialized" and "id" not in request:
            return Response(202, b"")
        if "id" not in request or request_id is None:
            raise GatewayError(400, "request_id_required")
        if method == "initialize":
            requested = params.get("protocolVersion")
            if not isinstance(requested, str):
                raise GatewayError(400, "invalid_protocol_version")
            version = requested if requested in {"2025-03-26", "2025-06-18"} else "2025-06-18"
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "feegate-merchant-gateway", "version": "1.0.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.discovery()[1]["tools"]}
        elif method == "tools/call":
            endpoint = self.endpoints.get(params.get("name")) if isinstance(params.get("name"), str) else None
            arguments = params.get("arguments", {})
            if endpoint is None or not isinstance(arguments, dict) or set(arguments) - {"query"}:
                raise GatewayError(400, "invalid_tool_arguments")
            query = arguments.get("query", "")
            self._query(query)
            response = self._paid(endpoint, query, headers, self.public_url + "/mcp")
            try:
                text = response.body.decode("utf-8")
            except UnicodeError:
                result = {"content": [{"type": "resource", "resource": {"uri": self.public_url + endpoint.path,
                          "mimeType": endpoint.mime_type, "blob": base64.b64encode(response.body).decode("ascii")}}]}
            else:
                result = {"content": [{"type": "text", "text": text}]}
            return Response.json(200, {"jsonrpc": "2.0", "id": request_id, "result": result}, response.headers | {"Content-Type": "application/json"})
        else:
            return Response.json(200, {"jsonrpc": "2.0", "id": request_id,
                                      "error": {"code": -32601, "message": "Method not found"}})
        return Response.json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

    @staticmethod
    def _query(query):
        if not isinstance(query, str) or len(query) > 4096 or any(ord(c) <= 32 or ord(c) >= 127 for c in query) or "#" in query:
            raise GatewayError(400, "invalid_query")

    def handle(self, method, target, headers=None, body=b""):
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        endpoint, resource_url = None, None
        try:
            if not isinstance(target, str) or len(target) > 8192 or not target.startswith("/") or target.startswith("//"):
                raise GatewayError(400, "invalid_target")
            parts = urlsplit(target)
            if parts.fragment:
                raise GatewayError(400, "invalid_target")
            self._query(parts.query)
            if len(body) > MAX_BODY_BYTES:
                raise GatewayError(413, "request_too_large")
            if parts.path == "/mcp":
                if method != "POST":
                    raise GatewayError(405, "method_not_allowed")
                if "origin" in headers and headers["origin"] != self.public_url:
                    raise GatewayError(403, "invalid_origin")
                return self._mcp(body, headers)
            if method != "GET":
                raise GatewayError(405, "method_not_allowed")
            if parts.path == "/health":
                return Response.json(200, {"status": "ok", "service": "feegate-merchant-gateway"})
            if parts.path in {"/.well-known/agent-card.json", "/.well-known/mcp-manifest.json"}:
                card, manifest = self.discovery()
                return Response.json(200, card if parts.path.endswith("agent-card.json") else manifest)
            endpoint = next((e for e in self.endpoints.values() if e.path == parts.path), None)
            if endpoint is None:
                raise GatewayError(404, "not_found")
            resource_url = self.public_url + endpoint.path + ("?" + parts.query if parts.query else "")
            return self._paid(endpoint, parts.query, headers, resource_url)
        except GatewayError as error:
            # MCP payment challenges describe the requested tool and HTTP URL.
            if error.status == 402 and endpoint is None and parts.path == "/mcp":
                try:
                    endpoint = self.endpoints.get(strict_json(body).get("params", {}).get("name"))
                    resource_url = self.public_url + "/mcp"
                except (ValueError, TypeError, AttributeError):
                    pass
            if error.status == 402 and endpoint is not None:
                invoice = self.invoice(endpoint, resource_url, error.code)
                return Response.json(402, invoice, {"PAYMENT-REQUIRED": encode_header(invoice)})
            return Response.json(error.status, {"error": error.code})
        except (sqlite3.Error, OSError):
            return Response.json(503, {"error": "storage_unavailable"})


def make_server(app, host="127.0.0.1", port=8402):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            # Paths/headers may carry caller secrets; do not log request content.
            pass

        def dispatch(self):
            try:
                for name in ("PAYMENT-SIGNATURE", "X-API-Key", "Authorization", "Content-Length"):
                    if len(self.headers.get_all(name, [])) > 1:
                        raise GatewayError(400, "duplicate_header")
                if self.headers.get("Transfer-Encoding"):
                    raise GatewayError(400, "unsupported_transfer_encoding")
                length = self.headers.get("Content-Length", "0")
                if not re.fullmatch(r"[0-9]{1,10}", length):
                    raise GatewayError(400, "invalid_content_length")
                length = int(length)
                if length > MAX_BODY_BYTES:
                    raise GatewayError(413, "request_too_large")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise GatewayError(400, "incomplete_body")
                response = app.handle(self.command, self.path, dict(self.headers), body)
            except GatewayError as error:
                response = Response.json(error.status, {"error": error.code})
            except (TimeoutError, ConnectionError):
                response = Response.json(408, {"error": "request_timeout"})
            except Exception:
                response = Response.json(500, {"error": "internal_error"})
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            if response.status == 405:
                self.send_header("Allow", "POST" if self.path == "/mcp" else "GET")
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(response.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = dispatch

    return ThreadingHTTPServer((host, port), Handler)


def default_endpoints():
    return [Endpoint("gpu-radar", "GPU rental arbitrage radar", ROOT / "data/feegate-gpu-rental-pools.json"),
            Endpoint("hk-talent", "Hong Kong talent statistics", ROOT / "data/feegate-hk-talent-pools.json"),
            Endpoint("cve-security", "CVE security threat feed", ROOT / "data/feegate-security-cve-threat-radar.json")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8402)
    parser.add_argument("--public-url")
    parser.add_argument("--db", type=Path, default=ROOT / "var/feegate-gateway.sqlite3")
    parser.add_argument("--facilitator-url", default=os.environ.get("FEEGATE_FACILITATOR_URL"))
    parser.add_argument("--dataset", action="append", default=[], metavar="NAME=FILE")
    parser.add_argument("--api", action="append", default=[], metavar="NAME=URL")
    parser.add_argument("--amount", default="50000", help="atomic USDC units per request")
    parser.add_argument("--points", type=int, default=5, help="RMB points per request (100 points/CNY)")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--issue-key", type=int, metavar="INITIAL_POINTS")
    actions.add_argument("--credit-key", metavar="KEY_DIGEST")
    actions.add_argument("--revoke-key", metavar="KEY_DIGEST")
    actions.add_argument("--export-discovery", type=Path, metavar="DIRECTORY")
    parser.add_argument("--credit-points", type=int)
    args = parser.parse_args(argv)
    ledger, server = None, None
    try:
        ledger = Ledger(args.db)
        if args.issue_key is not None:
            secret, digest = ledger.issue_key(args.issue_key)
            print(json_bytes({"apiKey": secret, "keyDigest": digest, "points": args.issue_key}).decode())
            return 0
        if args.credit_key:
            balance = ledger.credit(args.credit_key, args.credit_points)
            print(json_bytes({"points": balance}).decode())
            return 0
        if args.revoke_key:
            ledger.revoke(args.revoke_key)
            return 0
        endpoints = []
        for definition, is_api in [(x, False) for x in args.dataset] + [(x, True) for x in args.api]:
            name, separator, source = definition.partition("=")
            if not separator or not source:
                raise ValueError("expected NAME=SOURCE")
            mime = "application/json" if is_api else (mimetypes.guess_type(source)[0] or "application/octet-stream")
            endpoints.append(Endpoint(name, name.replace("-", " "), source if is_api else Path(source), args.amount, args.points, mime))
        if not endpoints:
            endpoints = [Endpoint(e.name, e.description, e.source, args.amount, args.points) for e in default_endpoints()]
        app = MerchantGateway(endpoints, ledger, public_url=args.public_url or f"http://127.0.0.1:{args.port}",
                              facilitator=Facilitator(args.facilitator_url, os.environ.get("FEEGATE_FACILITATOR_TOKEN")))
        if args.export_discovery:
            app.export_discovery(args.export_discovery)
            return 0
        server = make_server(app, args.host, args.port)
        if not args.public_url:
            app.public_url = f"http://127.0.0.1:{server.server_port}"
        print(f"FeeGate listening on {args.host}:{server.server_port}; public origin {app.public_url}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        print("FeeGate startup/administration failed: " + str(error), file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
        if ledger is not None:
            ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
