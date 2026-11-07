#!/usr/bin/env python3
"""Interactive, standard-library client delivery showcase.

Run: python3 scripts/freelance_delivery_showcase_server.py --port 8080
Test: python3 -m unittest discover -s scripts -p 'test_freelance_delivery_showcase_server.py' -v

GET / serves the dashboard; GET /health (also /api/health), /api/scrapers,
and /api/schema expose health, available demos, and JSON Schema contracts.
POST /api/tokens with {"client_name": "Client demo"} mints an ephemeral demo
Bearer token. POST /api/scrape with {"scraper_id": "hk-talent", "mode":
"offline"} runs a demo. GET /api/results/<run_id> downloads its validated
envelope, using the same Bearer token. The other built-in ID is gpu-pricing.

Tokens are delivered once, stored as SHA-256 digests, and have a fixed expiry
and run allowance. An admitted run consumes one allowance even if it fails.
Downloads do not consume allowances. Tokens and bounded results live in memory
and disappear on restart. Minting is deliberately self-service for this demo;
these tokens are not billing credentials or an administrative security boundary.
The default bind address is loopback. --allow-live explicitly enables fixed
upstream sources; offline runs use clearly marked synthetic data. Clients cannot
select commands, files, URLs, or upstream credentials. Live fetches have bounded
timeouts, and upstream failures never silently become offline results.

The internal validator implements only the JSON Schema keywords used by the
published contracts, not arbitrary third-party schemas. Scraper-specific
validators also check record semantics, provenance, and SHA-256 fingerprints.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import secrets
import socket
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid


SCHEMA_VERSION = "1.0.0"
MAX_BODY_BYTES = 16 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 10


class SchemaError(ValueError):
    """A value violates a published contract."""


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def _object(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
COUNT = {"type": "integer", "minimum": 0}
VERSION = {"type": "string", "enum": [SCHEMA_VERSION]}
RUN_COUNTS = _object({name: COUNT for name in ("started", "active", "succeeded", "failed")})
SCRAPER_HEALTH = _object({
    "id": TEXT, "name": TEXT,
    "status": {"type": "string", "enum": ["ready", "running", "ok", "error"]},
    "last_run_at_utc": {"type": ["string", "null"]}, "runs": RUN_COUNTS,
})
PAYLOAD_SCHEMA = {
    "type": "object", "required": ["status", "schema", "schema_version", "record_count",
                                   "generated_at_utc", "sha256_checksum", "data"],
    "properties": {
        "status": {"type": "string", "enum": ["success"]},
        "schema": {"type": "string", "enum": ["llamafi-economic-pools"]},
        "schema_version": VERSION, "record_count": COUNT, "generated_at_utc": TEXT,
        "sha256_checksum": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "data": {"type": "array", "items": {"type": "object"}},
    },
}
SCHEMAS = {
    "health": _object({
        "schema_version": VERSION, "service": {"type": "string", "enum": ["freelance-delivery-showcase"]},
        "status": {"type": "string", "enum": ["ok", "degraded"]},
        "started_at_utc": TEXT, "uptime_seconds": {"type": "number", "minimum": 0},
        "requests_total": COUNT, "tokens_active": COUNT, "runs": RUN_COUNTS,
        "scrapers": {"type": "array", "items": SCRAPER_HEALTH},
    }),
    "token_request": _object({"client_name": {"type": "string", "minLength": 1, "maxLength": 80}}),
    "token": _object({
        "schema_version": VERSION, "token": {"type": "string", "pattern": "^demo_[A-Za-z0-9_-]{43}$"},
        "token_type": {"type": "string", "enum": ["Bearer"]}, "client_name": TEXT,
        "expires_at_utc": TEXT, "expires_in_seconds": {"type": "integer", "minimum": 1},
        "runs_remaining": {"type": "integer", "minimum": 1},
    }),
    "scrape_request": _object({
        "scraper_id": TEXT, "mode": {"type": "string", "enum": ["offline", "live"]},
    }, ["scraper_id"]),
    "catalog": _object({
        "schema_version": VERSION, "live_enabled": {"type": "boolean"},
        "scrapers": {"type": "array", "items": _object({
            "id": TEXT, "name": TEXT, "description": TEXT,
            "modes": {"type": "array", "items": {"type": "string", "enum": ["offline", "live"]}},
        })},
    }),
    "result": _object({
        "schema_version": VERSION, "run_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "scraper_id": TEXT, "mode": {"type": "string", "enum": ["offline", "live"]},
        "status": {"type": "string", "enum": ["success"]}, "generated_at_utc": TEXT,
        "duration_ms": {"type": "number", "minimum": 0},
        "schema_validated": {"type": "boolean", "enum": [True]},
        "runs_remaining": COUNT, "payload": PAYLOAD_SCHEMA,
    }),
    "error": _object({"schema_version": VERSION,
                      "error": _object({"code": TEXT, "message": TEXT})}),
}


def validate_schema(value, schema, path="$", _depth=0):
    """Validate the small, explicit subset used by SCHEMAS; reject non-JSON data."""
    if _depth > 64:
        raise SchemaError(f"{path}: nesting exceeds 64 levels")
    kind = ("null" if value is None else "boolean" if type(value) is bool else
            "integer" if type(value) is int else "number" if type(value) is float else
            "string" if isinstance(value, str) else "array" if isinstance(value, list) else
            "object" if isinstance(value, dict) else None)
    if kind is None or (kind == "number" and not math.isfinite(value)):
        raise SchemaError(f"{path}: expected a finite JSON value")
    expected = schema.get("type", [])
    expected = [expected] if isinstance(expected, str) else expected
    if expected and kind not in expected and not (kind == "integer" and "number" in expected):
        raise SchemaError(f"{path}: expected {' or '.join(expected)}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: unsupported value")
    if kind == "object":
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            raise SchemaError(f"{path}: missing required field")
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError(f"{path}: object keys must be strings")
            if key not in properties and schema.get("additionalProperties") is False:
                raise SchemaError(f"{path}: unknown field")
            validate_schema(item, properties.get(key, {}), f"{path}.{key}", _depth + 1)
    elif kind == "array":
        for index, item in enumerate(value):
            validate_schema(item, schema.get("items", {}), f"{path}[{index}]", _depth + 1)
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf):
            raise SchemaError(f"{path}: invalid text length")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise SchemaError(f"{path}: invalid text format")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise SchemaError(f"{path}: invalid Unicode") from None
    elif kind in ("integer", "number") and value < schema.get("minimum", -math.inf):
        raise SchemaError(f"{path}: value below minimum")
    return True


def _request(value, schema_name):
    try:
        validate_schema(value, SCHEMAS[schema_name])
    except SchemaError as exc:
        raise GatewayError(400, "invalid_request", str(exc)) from None


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ScraperSpec:
    scraper_id: str
    name: str
    description: str
    run: Callable[[str], dict]
    validate: Callable[[dict], object]


def default_scrapers():
    if __package__:
        from . import feegate_hk_talent_scraper as talent
        from . import rentgpu_arbitrage_scanner as gpu
    else:
        import feegate_hk_talent_scraper as talent
        import rentgpu_arbitrage_scanner as gpu
    return (
        ScraperSpec("hk-talent", "Hong Kong talent statistics",
                    "TTPS, QMAS and CIES statistics with provenance and integrity checks.",
                    lambda mode: talent.run(offline=mode == "offline", strict=mode == "live",
                                            timeout=5, attempts=1), talent.verify_integrity),
        ScraperSpec("gpu-pricing", "GPU rental pricing",
                    "Provider pricing, comparable spreads and hourly volatility signals.",
                    lambda mode: gpu.run(offline=mode == "offline", strict=mode == "live", timeout=5,
                                         live=("runpod", "lambda") if mode == "live" else ()),
                    gpu.verify_integrity),
    )


@dataclass
class _Token:
    deadline: float
    remaining: int


class ShowcaseApp:
    """Thread-safe demo state, admission limits, execution, and result ownership."""

    def __init__(self, *, scrapers=None, allow_live=False, token_ttl=900, token_quota=10,
                 max_tokens=256, max_results=64, max_concurrent_runs=4,
                 clock=time.time, monotonic=time.monotonic):
        for name, value in (("token_ttl", token_ttl), ("token_quota", token_quota),
                            ("max_tokens", max_tokens), ("max_results", max_results),
                            ("max_concurrent_runs", max_concurrent_runs)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(allow_live) is not bool:
            raise ValueError("allow_live must be a boolean")
        specs = tuple(default_scrapers() if scrapers is None else scrapers)
        self.scrapers = {}
        for spec in specs:
            if (not isinstance(spec, ScraperSpec) or
                    re.fullmatch(r"[a-z][a-z0-9-]{0,63}", spec.scraper_id) is None or
                    spec.scraper_id in self.scrapers or not spec.name or not spec.description or
                    not callable(spec.run) or not callable(spec.validate)):
                raise ValueError("invalid or duplicate scraper specification")
            self.scrapers[spec.scraper_id] = spec
        if not self.scrapers:
            raise ValueError("at least one scraper is required")
        self.allow_live, self.token_ttl, self.token_quota = allow_live, token_ttl, token_quota
        self.max_tokens, self.max_results = max_tokens, max_results
        self.max_concurrent_runs = max_concurrent_runs
        self.clock, self.monotonic = clock, monotonic
        self.started_at = _timestamp(clock())
        self._started = monotonic()
        self._lock = threading.RLock()
        self._tokens: dict[str, _Token] = {}
        self._results: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        self._requests = 0
        self._counts = {name: 0 for name in ("started", "active", "succeeded", "failed")}
        self._scraper_state = {key: {"last_status": "ready", "last_run_at_utc": None,
                                    "runs": self._counts.copy()} for key in self.scrapers}

    def _prune(self):
        now = self.monotonic()
        for digest in [key for key, token in self._tokens.items() if token.deadline <= now]:
            del self._tokens[digest]
        for run_id in [key for key, (owner, _) in self._results.items() if owner not in self._tokens]:
            del self._results[run_id]

    def _authenticate(self, token):
        self._prune()
        if not isinstance(token, str) or re.fullmatch(r"demo_[A-Za-z0-9_-]{43}", token) is None:
            raise GatewayError(401, "unauthorized", "A valid, unexpired Bearer token is required")
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        if digest not in self._tokens:
            raise GatewayError(401, "unauthorized", "A valid, unexpired Bearer token is required")
        return digest, self._tokens[digest]

    def record_request(self):
        with self._lock:
            self._requests += 1

    def health(self):
        with self._lock:
            self._prune()
            scrapers = []
            for key, spec in self.scrapers.items():
                state = self._scraper_state[key]
                scrapers.append({"id": key, "name": spec.name,
                                 "status": "running" if state["runs"]["active"] else state["last_status"],
                                 "last_run_at_utc": state["last_run_at_utc"], "runs": state["runs"].copy()})
            return {"schema_version": SCHEMA_VERSION, "service": "freelance-delivery-showcase",
                    "status": "degraded" if any(s["status"] == "error" for s in scrapers) else "ok",
                    "started_at_utc": self.started_at,
                    "uptime_seconds": round(max(0, self.monotonic() - self._started), 3),
                    "requests_total": self._requests, "tokens_active": len(self._tokens),
                    "runs": self._counts.copy(), "scrapers": scrapers}

    def catalog(self):
        return {"schema_version": SCHEMA_VERSION, "live_enabled": self.allow_live,
                "scrapers": [{"id": key, "name": spec.name, "description": spec.description,
                              "modes": ["offline", "live"] if self.allow_live else ["offline"]}
                             for key, spec in self.scrapers.items()]}

    def mint_token(self, request):
        _request(request, "token_request")
        name = request["client_name"].strip()
        if not name:
            raise GatewayError(400, "invalid_request", "client_name must contain text")
        with self._lock:
            self._prune()
            if len(self._tokens) >= self.max_tokens:
                raise GatewayError(429, "token_capacity", "Demo token capacity reached; retry after expiry")
            token = "demo_" + secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            self._tokens[digest] = _Token(self.monotonic() + self.token_ttl, self.token_quota)
            return {"schema_version": SCHEMA_VERSION, "token": token, "token_type": "Bearer",
                    "client_name": name, "expires_at_utc": _timestamp(self.clock() + self.token_ttl),
                    "expires_in_seconds": self.token_ttl, "runs_remaining": self.token_quota}

    def execute_scraper(self, token, request):
        _request(request, "scrape_request")
        key, mode = request["scraper_id"], request.get("mode", "offline")
        with self._lock:
            digest, record = self._authenticate(token)
            if key not in self.scrapers:
                raise GatewayError(404, "unknown_scraper", "Unknown scraper ID")
            if mode == "live" and not self.allow_live:
                raise GatewayError(403, "live_disabled", "Live execution is disabled on this server")
            if record.remaining == 0:
                raise GatewayError(429, "quota_exhausted", "Demo token run allowance exhausted")
            if self._counts["active"] >= self.max_concurrent_runs:
                raise GatewayError(503, "server_busy", "All demo execution slots are busy; retry shortly")
            record.remaining -= 1
            remaining = record.remaining
            state = self._scraper_state[key]
            for counts in (self._counts, state["runs"]):
                counts["started"] += 1
                counts["active"] += 1
        began = self.monotonic()
        succeeded = False
        try:
            spec = self.scrapers[key]
            payload = spec.run(mode)
            validate_schema(payload, PAYLOAD_SCHEMA)
            if payload["record_count"] != len(payload["data"]) or spec.validate(payload) is False:
                raise SchemaError("scraper output failed validation")
            result = {"schema_version": SCHEMA_VERSION, "run_id": uuid.uuid4().hex,
                      "scraper_id": key, "mode": mode, "status": "success",
                      "generated_at_utc": _timestamp(self.clock()),
                      "duration_ms": round(max(0, self.monotonic() - began) * 1000, 3),
                      "schema_validated": True, "runs_remaining": remaining, "payload": payload}
            validate_schema(result, SCHEMAS["result"])
            raw = _json_bytes(result)
            if len(raw) > MAX_RESULT_BYTES:
                raise SchemaError("scraper output exceeds retention limit")
            with self._lock:
                self._prune()
                if digest in self._tokens:
                    self._results[result["run_id"]] = (digest, raw)
                    while len(self._results) > self.max_results:
                        self._results.popitem(last=False)
            succeeded = True
            return json.loads(raw)
        except Exception:
            # Never send upstream exception strings (which may contain credentials) to a client.
            raise GatewayError(502, "scraper_failed", "Scraper execution or output validation failed") from None
        finally:
            with self._lock:
                for counts in (self._counts, state["runs"]):
                    counts["active"] -= 1
                    counts["succeeded" if succeeded else "failed"] += 1
                state["last_status"] = "ok" if succeeded else "error"
                state["last_run_at_utc"] = _timestamp(self.clock())

    def get_result(self, token, run_id):
        with self._lock:
            digest, _ = self._authenticate(token)
            result = self._results.get(run_id)
            if result is None or result[0] != digest:
                raise GatewayError(404, "result_not_found", "Result is unavailable for this token")
            return json.loads(result[1])


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Client Delivery Showcase</title><style>
:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#101824;color:#e3edf7}
body{max-width:1050px;margin:auto;padding:32px 20px}h1{font-size:2.4rem;margin-bottom:8px}
p{color:#b7c9dc;line-height:1.6}section{background:#192638;border:1px solid #334b65;border-radius:12px;padding:22px;margin:22px 0}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:15px}.metric{padding:12px;background:#101824;border-radius:8px}
.metric strong{display:block;font-size:1.6rem;color:#6fe0c6}label{display:block;margin:14px 0 6px}input,select,button{font:inherit;padding:10px;border:1px solid #66829d;border-radius:6px;background:#101824;color:inherit;max-width:100%;box-sizing:border-box}
button{cursor:pointer;background:#225b63;margin:12px 8px 0 0}button:disabled{opacity:.5;cursor:wait}input{width:320px}a{color:#6fe0c6}
pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:480px;overflow:auto;background:#101824;padding:14px;border-radius:8px}small{color:#b7c9dc}#notice{min-height:1.5em}
</style></head><body><header><small>Yotoha · Interactive portfolio</small><h1>Client Delivery Showcase</h1>
<p>Explore a working data delivery gateway: monitor health, mint a demo token, and run a scraper to inspect its validated JSON output.</p></header>
<section aria-labelledby="health-title"><h2 id="health-title">Live service health</h2><p id="health-status" role="status">Connecting…</p>
<div class="metrics" id="metrics"></div><ul id="scraper-health"></ul><small>Refreshes every 3 seconds. <a href="/api/health">Health JSON</a> · <a href="/api/schema">Response schemas</a></small></section>
<section aria-labelledby="demo-title"><h2 id="demo-title">Try a delivery</h2><p>Offline demos use synthetic observations, clearly labeled in their provenance. Each demo token lasts 15 minutes and allows 10 runs by default.</p>
<label for="client-name">Client name</label><input id="client-name" maxlength="80" value="Client demo" autocomplete="off">
<button id="mint" type="button">Mint demo token</button><pre id="token-info">Mint a token to begin.</pre>
<label for="scraper">Scraper</label><select id="scraper"></select><p id="description"></p>
<label for="mode">Data source</label><select id="mode"><option value="offline">Offline · synthetic demo</option></select>
<button id="run" type="button" disabled>Run scraper</button><p id="notice" role="status" aria-live="polite"></p>
<a id="download" hidden>Download validated JSON</a><pre id="output" tabindex="0">Your delivery result will appear here.</pre></section>
<script nonce="__NONCE__">
const el=id=>document.getElementById(id);let token='',catalog=[],downloadUrl='';
async function api(path,options={}){const response=await fetch(path,options);const data=await response.json();if(!response.ok)throw new Error(data.error.message);return data;}
function post(body,auth=false){return {method:'POST',headers:{'Content-Type':'application/json',...(auth?{Authorization:'Bearer '+token}:{})},body:JSON.stringify(body)};}
async function refresh(){try{const h=await api('/api/health');el('health-status').textContent=h.status==='ok'?'Service operational':'A scraper needs attention';
const metrics=[['Uptime',Math.floor(h.uptime_seconds)+' s'],['HTTP requests',h.requests_total],['Active tokens',h.tokens_active],['Successful runs',h.runs.succeeded],['Failed runs',h.runs.failed],['Running',h.runs.active]];
el('metrics').replaceChildren(...metrics.map(([label,value])=>{const d=document.createElement('div');d.className='metric';const v=document.createElement('strong');v.textContent=value;d.append(v,document.createTextNode(label));return d;}));
el('scraper-health').replaceChildren(...h.scrapers.map(s=>{const li=document.createElement('li');li.textContent=s.name+': '+s.status;return li;}));}catch(e){el('health-status').textContent='Health refresh failed: '+e.message;}}
el('mint').onclick=async()=>{el('mint').disabled=true;try{const t=await api('/api/tokens',post({client_name:el('client-name').value}));token=t.token;el('token-info').textContent=t.token+'\nExpires: '+t.expires_at_utc+' · Runs available: '+t.runs_remaining;el('run').disabled=!catalog.length;el('notice').textContent='Demo token ready.';}catch(e){el('notice').textContent=e.message;}finally{el('mint').disabled=false;refresh();}};
el('run').onclick=async()=>{el('run').disabled=true;el('notice').textContent='Running scraper and validating output…';el('download').hidden=true;if(downloadUrl){URL.revokeObjectURL(downloadUrl);downloadUrl='';}
try{const r=await api('/api/scrape',post({scraper_id:el('scraper').value,mode:el('mode').value},true));const text=JSON.stringify(r,null,2);el('output').textContent=text;el('notice').textContent='Delivery validated · '+r.duration_ms+' ms · Runs remaining: '+r.runs_remaining;
downloadUrl=URL.createObjectURL(new Blob([text],{type:'application/json'}));el('download').href=downloadUrl;el('download').download='delivery-'+r.run_id+'.json';el('download').hidden=false;}catch(e){el('notice').textContent=e.message;el('output').textContent='The delivery could not be completed.';}finally{el('run').disabled=!token||!catalog.length;refresh();}};
el('scraper').onchange=()=>{const s=catalog.find(s=>s.id===el('scraper').value);el('description').textContent=s?s.description:'';};
async function initialize(){try{const c=await api('/api/scrapers');catalog=c.scrapers;el('scraper').replaceChildren(...catalog.map(s=>{const o=document.createElement('option');o.value=s.id;o.textContent=s.name;return o;}));if(c.live_enabled){const o=document.createElement('option');o.value='live';o.textContent='Live · upstream sources';el('mode').append(o);}el('scraper').onchange();el('run').disabled=!token||!catalog.length;}catch(e){el('notice').textContent=e.message;}}
initialize();refresh();setInterval(refresh,3000);
</script></body></html>"""


def make_handler(app):
    class ShowcaseHandler(BaseHTTPRequestHandler):
        server_version = "DeliveryShowcase/1.0"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(REQUEST_TIMEOUT)

        def log_message(self, format, *args):
            # Request paths and headers may contain pasted credentials. Keep them out of logs.
            pass

        def _send(self, status, body, content_type, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, value, schema_name=None, headers=None):
            if schema_name:
                validate_schema(value, SCHEMAS[schema_name])
            self._send(status, _json_bytes(value), "application/json; charset=utf-8", headers)

        def send_error(self, code, message=None, explain=None):
            self.close_connection = True
            self._json(code, {"schema_version": SCHEMA_VERSION,
                              "error": {"code": "http_error", "message": self.responses.get(code, ("HTTP error",))[0]}}, "error")

        def _read_json(self):
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                raise GatewayError(403, "cross_origin", "Use the showcase from its own origin")
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlsplit(origin)
                if (parsed.scheme not in ("http", "https") or
                        parsed.netloc.lower() != self.headers.get("Host", "").lower() or
                        parsed.path or parsed.query or parsed.fragment):
                    raise GatewayError(403, "cross_origin", "Use the showcase from its own origin")
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding"):
                raise GatewayError(400, "invalid_request", "Transfer-Encoding is unsupported")
            if not lengths:
                raise GatewayError(411, "length_required", "Content-Length is required")
            if len(lengths) != 1 or re.fullmatch(r"[0-9]{1,10}", lengths[0]) is None:
                raise GatewayError(400, "invalid_request", "Invalid Content-Length")
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                raise GatewayError(413, "body_too_large", "JSON request exceeds 16 KiB")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise GatewayError(415, "unsupported_media_type", "Content-Type must be application/json")

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result

            def finite(text):
                value = float(text)
                if not math.isfinite(value):
                    raise ValueError("non-finite value")
                return value

            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete body")
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                                   parse_constant=finite, parse_float=finite)
                if not isinstance(value, dict):
                    raise ValueError("object required")
                return value
            except (ValueError, UnicodeError, RecursionError):
                raise GatewayError(400, "invalid_json", "Body must be a valid UTF-8 JSON object with unique keys and finite numbers") from None

        def _bearer(self):
            values = self.headers.get_all("Authorization", [])
            parts = values[0].split() if len(values) == 1 else []
            return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None

        def _dispatch(self):
            app.record_request()
            try:
                target = urlsplit(self.path)
                if target.scheme or target.netloc or target.fragment:
                    raise GatewayError(400, "invalid_request", "Use an origin-relative request path")
                path = target.path
                download = re.fullmatch(r"/api/results/([0-9a-f]{32})", path)
                allowed = (["GET", "HEAD"] if path in ("/", "/health", "/api/health", "/api/schema", "/api/scrapers") or download
                           else ["POST"] if path in ("/api/tokens", "/api/scrape") else [])
                if not allowed:
                    raise GatewayError(404, "not_found", "Endpoint not found")
                if self.command not in allowed:
                    self._json(405, {"schema_version": SCHEMA_VERSION,
                                     "error": {"code": "method_not_allowed", "message": "Method not allowed"}},
                               "error", {"Allow": ", ".join(allowed)})
                elif path == "/":
                    nonce = secrets.token_urlsafe(24)
                    self._send(200, DASHBOARD_HTML.replace("__NONCE__", nonce).encode("utf-8"),
                               "text/html; charset=utf-8", {"Content-Security-Policy":
                               "default-src 'self'; script-src 'nonce-" + nonce + "'; style-src 'unsafe-inline'; "
                               "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
                elif path in ("/health", "/api/health"):
                    self._json(200, app.health(), "health")
                elif path == "/api/scrapers":
                    self._json(200, app.catalog(), "catalog")
                elif path == "/api/schema":
                    self._json(200, {"schema_version": SCHEMA_VERSION, "schemas": {
                        name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
                        for name, schema in SCHEMAS.items()}})
                elif path == "/api/tokens":
                    self._json(201, app.mint_token(self._read_json()), "token")
                elif path == "/api/scrape":
                    self._json(200, app.execute_scraper(self._bearer(), self._read_json()), "result")
                elif download:
                    run_id = download.group(1)
                    self._json(200, app.get_result(self._bearer(), run_id), "result",
                               {"Content-Disposition": f'attachment; filename="delivery-{run_id}.json"'})
            except GatewayError as exc:
                self._json(exc.status, {"schema_version": SCHEMA_VERSION,
                                       "error": {"code": exc.code, "message": str(exc)}}, "error",
                           {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None)
            except (socket.timeout, TimeoutError):
                self._json(408, {"schema_version": SCHEMA_VERSION,
                                 "error": {"code": "request_timeout", "message": "Request body timed out"}}, "error")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._json(500, {"schema_version": SCHEMA_VERSION,
                                 "error": {"code": "internal_error", "message": "The request could not be completed"}}, "error")

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _dispatch

    return ShowcaseHandler


def create_server(host="127.0.0.1", port=8080, *, app=None):
    """Return a bound server; port=0 selects an ephemeral port for embedding/tests."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer from 0 to 65535")
    return ThreadingHTTPServer((host, port), make_handler(app if app is not None else ShowcaseApp()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--allow-live", action="store_true", help="enable fixed upstream sources")
    parser.add_argument("--token-ttl", type=int, default=900, help="demo token lifetime in seconds")
    parser.add_argument("--token-quota", type=int, default=10, help="runs per demo token")
    args = parser.parse_args(argv)
    try:
        app = ShowcaseApp(allow_live=args.allow_live, token_ttl=args.token_ttl, token_quota=args.token_quota)
        with create_server(args.host, args.port, app=app) as server:
            print(f"Client delivery showcase on http://{args.host}:{server.server_port} "
                  f"(live {'enabled' if args.allow_live else 'disabled'})", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
