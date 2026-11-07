#!/usr/bin/env python3
"""Harvest FeeGate Ethereum yields using only Python's standard library.

Run: python3 scripts/sovereign_yield_harvester.py
Offline smoke test: python3 scripts/sovereign_yield_harvester.py --offline --output /tmp/yields.json

Sources: Lido's official net stETH APR API; Ethereum Multicall3 reads of Aave
V3, Maker Pot, Sky sUSDS, and Ethena sUSDe; CoinGecko USD spot quotes. No yield
aggregator is used. Contract addresses are Ethereum mainnet deployments; Sky
addresses were checked against https://chainlog.sky.money/api/mainnet/active.json.

APYs are conditional annualizations, not guaranteed returns: daily reinvestment
for Lido; per-second reinvestment of Aave's current supply APR (an approximation
because its liquidity index accrues linearly between reserve updates); exact
per-second savings factors for Maker/Sky; geometric annualization of observed
sUSDe assets-per-share growth over approximately seven days. Incentive tokens,
gas, withdrawal costs and USD price changes are excluded. TVL is supplied/backing
assets, not available liquidity, and must not be summed across overlapping pools.

On failure, reuse this output's checksum-verified live/cached records for at most
--max-cache-age-hours (default 168), preserving their original observation times.
Otherwise calculate conspicuously labeled synthetic records. Mock values are
illustrative inputs, never historical observations. No API keys or packages are
required. ETHEREUM_RPC_URL optionally prepends a custom HTTPS mainnet RPC.

sha256_checksum covers records encoded as UTF-8 JSON with sort_keys=True,
ensure_ascii=False, allow_nan=False and separators=(',', ':'). Writes are atomic.
"""

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path


YEAR = 365 * 24 * 60 * 60
RAY = 10 ** 27
WAD = 10 ** 18
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/feegate-sovereign-defi-yields.json"
LIDO_API = "https://eth-api.lido.fi/v1/protocol/steth/apr/sma"
PRICE_API = ("https://api.coingecko.com/api/v3/simple/price?"
             "ids=ethereum,usd-coin,dai,usds,ethena-usde"
             "&vs_currencies=usd&include_last_updated_at=true")
RPC_URLS = ("https://ethereum-rpc.publicnode.com", "https://eth.drpc.org",
            "https://rpc.mevblocker.io")
MULTICALL = "0xcA11bde05977b3631167028862bE2a173976CA11"
LIDO = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
AAVE = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fa4E2"
POT = "0x197E90f9FAD81970bA7976f33CbD77088E5D7cf7"
SUSDS = "0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD"
SUSDE = "0x9D39A5de30e57443BfF2A8307A4256c8797A3497"

# Ethereum Keccak selectors, NOT hashlib.sha3_256 (which uses different padding).
SELECTORS = {
    "getTotalPooledEther": "37cfdaca", "getReserveData": "35ea6a75",
    "totalSupply": "18160ddd", "dsr": "487bf082", "Pie": "2c69ed58",
    "chi": "c92aecc4", "rho": "20aba08b", "ssr": "03607ceb",
    "totalAssets": "01e1d114", "convertToAssets": "07a2d13a",
}
POOLS = (
    dict(id="lido-steth", protocol="Lido", symbol="stETH", underlying="ETH",
         address=LIDO, price_id="ethereum", decimals=18, category="Liquid Staking",
         risk="Medium", risks=["slashing", "stETH discount", "withdrawal queue", "smart contract"]),
    dict(id="aave-v3-usdc", protocol="Aave v3", symbol="aUSDC", underlying="USDC",
         address=AAVE, token="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
         price_id="usd-coin", decimals=6, category="Stablecoin", risk="Medium",
         risks=["depeg", "issuer freeze", "bad debt", "utilization", "smart contract"]),
    dict(id="aave-v3-dai", protocol="Aave v3", symbol="aDAI", underlying="DAI",
         address=AAVE, token="0x6B175474E89094C44Da98b954EedeAC495271d0F",
         price_id="dai", decimals=18, category="Stablecoin", risk="Medium",
         risks=["depeg", "bad debt", "utilization", "smart contract"]),
    dict(id="maker-dsr-dai", protocol="Sky / MakerDAO", symbol="DSR", underlying="DAI",
         address=POT, price_id="dai", decimals=18, category="Stablecoin", risk="Medium",
         risks=["depeg", "governance rate changes", "collateral", "smart contract"]),
    dict(id="sky-susds", protocol="Sky / MakerDAO", symbol="sUSDS", underlying="USDS",
         address=SUSDS, price_id="usds", decimals=18, category="Stablecoin", risk="Medium",
         risks=["depeg", "governance rate changes", "collateral", "smart contract"]),
    dict(id="ethena-susde", protocol="Ethena", symbol="sUSDe", underlying="USDe",
         address=SUSDE, price_id="ethena-usde", decimals=18, category="Stablecoin", risk="High",
         risks=["depeg", "funding rate", "custodian", "exchange counterparty", "cooldown", "smart contract"]),
)


class SourceError(Exception):
    """Unavailable, malformed, or stale upstream data."""


def utc_timestamp(seconds=None):
    return datetime.fromtimestamp(time.time() if seconds is None else seconds,
                                  timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.timestamp()


def number(value, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise ValueError("boolean is not a financial quantity")
    result = Decimal(str(value))
    if not result.is_finite() or (minimum is not None and result < minimum) or (
            maximum is not None and result > maximum):
        raise ValueError("financial quantity outside permitted range")
    return result


def canonical(records):
    return json.dumps(records, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def checksum(records):
    return hashlib.sha256(canonical(records)).hexdigest()


def fresh(timestamp, now, max_age):
    age = now - float(timestamp)
    if not math.isfinite(age) or age < -300 or age > max_age:
        raise SourceError("source timestamp is stale or in the future")


def json_request(url, payload=None, timeout=8.0):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise SourceError("only HTTPS sources are accepted")
    data = None if payload is None else canonical(payload)
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": "FeeGate-SovereignYieldHarvester/1.0", "Accept": "application/json",
        "Content-Type": "application/json",
    })
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise SourceError("response exceeds size limit")
            return json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")))
        except urllib.error.HTTPError as error:
            failure = "HTTP {}".format(error.code)
            if error.code not in (429, 500, 502, 503, 504):
                break
        except (OSError, ValueError) as error:
            # Do not export RPC credentials, query parameters, or response bodies.
            failure = type(error).__name__
        if attempt == 0:
            time.sleep(0.2)
    raise SourceError(failure)


def word(value):
    if not isinstance(value, int) or not 0 <= value < 2 ** 256:
        raise ValueError("invalid uint256")
    return value.to_bytes(32, "big")


def address_word(address):
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise ValueError("invalid Ethereum address")
    return bytes.fromhex(address[2:]).rjust(32, b"\0")


def calldata(name, argument=None):
    data = SELECTORS[name]
    if argument is not None:
        data += (address_word(argument) if isinstance(argument, str) else word(argument)).hex()
    return "0x" + data


def uint_at(data, offset=0):
    if offset < 0 or offset % 32 or offset + 32 > len(data):
        raise SourceError("truncated or misaligned ABI response")
    return int.from_bytes(data[offset:offset + 32], "big")


def encode_multicall(calls):
    """ABI encode aggregate3((address,bool,bytes)[]), allowing individual reverts."""
    elements = []
    for address, data in calls:
        raw = bytes.fromhex(data[2:])
        elements.append(address_word(address) + word(1) + word(96) + word(len(raw))
                        + raw + b"\0" * (-len(raw) % 32))
    offset = 32 * len(elements)
    offsets = []
    for element in elements:
        offsets.append(word(offset))
        offset += len(element)
    return "0x82ad56cb" + (word(32) + word(len(elements)) + b"".join(offsets)
                           + b"".join(elements)).hex()


def decode_multicall(encoded, expected):
    if not isinstance(encoded, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", encoded):
        raise SourceError("invalid eth_call hex")
    data = bytes.fromhex(encoded[2:])
    start = uint_at(data)
    if start != 32 or uint_at(data, start) != expected:
        raise SourceError("unexpected Multicall result count")
    base = start + 32
    results = []
    for i in range(expected):
        relative = uint_at(data, base + 32 * i)
        if relative < expected * 32:
            raise SourceError("invalid Multicall tuple offset")
        position = base + relative
        if uint_at(data, position) != 1:
            raise SourceError("contract call reverted")
        if uint_at(data, position + 32) != 64:
            raise SourceError("invalid Multicall bytes offset")
        size = uint_at(data, position + 64)
        begin = position + 96
        if not size or size % 32 or begin + size > len(data):
            raise SourceError("empty or truncated contract result")
        results.append(data[begin:begin + size])
    return results


class Ethereum:
    def __init__(self, urls, timeout, now):
        self.timeout = timeout
        self.urls = []
        self.snapshot = None
        for url in dict.fromkeys(urls):
            try:
                if int(self.call(url, "eth_chainId", []), 16) != 1:
                    raise SourceError("RPC is not Ethereum mainnet")
                block = self.call(url, "eth_getBlockByNumber", ["finalized", False])
                timestamp = int(block["timestamp"], 16)
                fresh(timestamp, now, 3600)
                if not re.fullmatch(r"0x[0-9a-fA-F]{64}", block["hash"]):
                    raise SourceError("invalid block hash")
                self.snapshot = {"number": int(block["number"], 16),
                                 "timestamp": timestamp, "hash": block["hash"]}
                self.urls = [url] + [other for other in dict.fromkeys(urls) if other != url]
                break
            except (SourceError, ValueError, TypeError, KeyError):
                continue
        if self.snapshot is None:
            raise SourceError("all Ethereum RPC endpoints unavailable or stale")
        self.verified = {self.urls[0]}

    def call(self, url, method, params):
        reply = json_request(url, {"jsonrpc": "2.0", "id": 1, "method": method,
                                   "params": params}, self.timeout)
        if not isinstance(reply, dict) or reply.get("id") != 1 or reply.get("error") or reply.get("result") is None:
            raise SourceError("invalid or unsuccessful JSON-RPC response")
        return reply["result"]

    def verify(self, url):
        if url not in self.verified:
            if int(self.call(url, "eth_chainId", []), 16) != 1:
                raise SourceError("RPC is not Ethereum mainnet")
            block = self.call(url, "eth_getBlockByNumber", [hex(self.snapshot["number"]), False])
            if block["hash"] != self.snapshot["hash"]:
                raise SourceError("RPC disagrees with finalized snapshot")
            self.verified.add(url)

    def read(self, calls, block=None):
        tag = hex(self.snapshot["number"] if block is None else block)
        for url in self.urls:
            try:
                self.verify(url)
                result = self.call(url, "eth_call", [{"to": MULTICALL,
                                   "data": encode_multicall(calls)}, tag])
                decoded = decode_multicall(result, len(calls))
                return decoded, "https://" + urllib.parse.urlsplit(url).netloc.rsplit("@", 1)[-1]
            except (SourceError, ValueError, TypeError, KeyError):
                continue
        raise SourceError("Multicall unavailable, reverted, or malformed on all RPCs")

    def historical_block(self):
        # Actual block timestamps, rather than the 12-second estimate, set the
        # annualization denominator. This does not require an exact 7-day block.
        height = self.snapshot["number"] - (7 * 24 * 60 * 60 // 12)
        for url in self.urls:
            try:
                self.verify(url)
                block = self.call(url, "eth_getBlockByNumber", [hex(height), False])
                timestamp = int(block["timestamp"], 16)
                elapsed = self.snapshot["timestamp"] - timestamp
                if int(block["number"], 16) != height or not 5 * 86400 <= elapsed <= 10 * 86400:
                    raise SourceError("invalid historical observation window")
                return {"number": height, "timestamp": timestamp, "hash": block["hash"]}
            except (SourceError, ValueError, TypeError, KeyError):
                continue
        raise SourceError("historical block unavailable")


def compounded_apr(apr, periods):
    with localcontext() as ctx:
        ctx.prec = 60
        apr = number(apr, minimum=0, maximum=10)
        return (1 + apr / periods) ** periods - 1


def savings_yield(factor):
    with localcontext() as ctx:
        ctx.prec = 60
        factor = number(factor, minimum=RAY, maximum=RAY + 10 ** 21) / RAY
        return (factor - 1) * YEAR, factor ** YEAR - 1


def growth_yield(before, after, elapsed):
    with localcontext() as ctx:
        ctx.prec = 60
        ratio = number(after, minimum=1) / number(before, minimum=1)
        if elapsed <= 0:
            raise ValueError("nonpositive observation interval")
        return (ratio - 1) * YEAR / elapsed, ratio ** (Decimal(YEAR) / elapsed) - 1


def metric(value, digits=8):
    result = round(float(value), digits)
    if not math.isfinite(result):
        raise ValueError("non-finite calculated metric")
    return result


def build_record(pool, raw, price, provenance):
    """Normalize both live raw measurements and synthetic fallback inputs."""
    with localcontext() as ctx:
        ctx.prec = 60
        kind = raw["kind"]
        if kind == "lido_apr":
            apr = number(raw["net_apr_pct"], minimum=0, maximum=1000) / 100
            apy = compounded_apr(apr, 365)
            units = number(raw["pooled_ether_wei"], minimum=0) / WAD
            basis = "projected; daily reinvestment of trailing net APR"
            formula = "(1 + apr_decimal / 365)^365 - 1"
        elif kind == "aave_supply":
            apr = number(raw["liquidity_rate_ray"], minimum=0) / RAY
            apy = compounded_apr(apr, YEAR)
            units = number(raw["atoken_total_supply_raw"], minimum=0) / (10 ** pool["decimals"])
            basis = "projected; per-second reinvestment approximation of current supply APR"
            formula = "(1 + apr_decimal / 31536000)^31536000 - 1"
        elif kind in ("maker_dsr", "sky_ssr"):
            apr, apy = savings_yield(raw["per_second_rate_ray"])
            if kind == "maker_dsr":
                elapsed = int(raw["block_timestamp"]) - int(raw["rho"])
                if elapsed < 0 or elapsed > 20 * YEAR:
                    raise ValueError("invalid Maker accrual interval")
                chi = number(raw["chi_ray"], minimum=RAY)
                factor = number(raw["per_second_rate_ray"]) / RAY
                units = number(raw["pie_wad"], minimum=0) * chi / RAY / WAD * factor ** elapsed
            else:
                units = number(raw["total_assets_raw"], minimum=0) / WAD
            basis = "projected; current on-chain per-second savings factor"
            formula = "(per_second_rate_ray / 10^27)^31536000 - 1"
        elif kind == "ethena_share_growth":
            apr, apy = growth_yield(raw["assets_per_share_before"], raw["assets_per_share_after"],
                                    int(raw["window_seconds"]))
            units = number(raw["total_assets_raw"], minimum=0) / WAD
            basis = "realized window growth, geometrically annualized"
            formula = "(assets_per_share_after / assets_per_share_before)^(31536000 / window_seconds) - 1"
        else:
            raise ValueError("unknown raw yield kind")
        usd = number(price["usd"], minimum=Decimal("0.00000001"))
        record = {
            "id": pool["id"], "protocol": pool["protocol"], "chain": "Ethereum", "chain_id": 1,
            "symbol": pool["symbol"], "underlying_asset": pool["underlying"],
            "contract_address": pool["address"], "risk_category": pool["category"],
            "risk_level": pool["risk"], "risk_factors": pool["risks"],
            "apr_pct": metric(apr * 100), "apy_pct": metric(apy * 100),
            "tvl_usd": metric(units * usd, 2), "tvl_underlying": metric(units),
            "underlying_price_usd": metric(usd), "price_provenance": price,
            "yield_basis": basis, "apy_formula": formula,
            "raw_inputs": raw, "provenance": provenance,
        }
    validate_record(record, pool)
    return record


def validate_record(record, pool):
    for key, expected in (("id", pool["id"]), ("contract_address", pool["address"]),
                          ("risk_category", pool["category"]), ("chain_id", 1)):
        if record[key] != expected:
            raise ValueError("record identity mismatch")
    number(record["apr_pct"], minimum=-100, maximum=100000)
    number(record["apy_pct"], minimum=-100, maximum=100000)
    for key in ("tvl_usd", "tvl_underlying", "underlying_price_usd"):
        number(record[key], minimum=0)
    if record["provenance"]["data_status"] not in ("live", "cached", "mock"):
        raise ValueError("invalid data status")
    canonical(record)


def fetch_prices(now, timeout):
    try:
        data = json_request(PRICE_API, timeout=timeout)
    except SourceError:
        return {}
    result = {}
    for pool in POOLS:
        key = pool["price_id"]
        try:
            quote = data[key]
            price = number(quote["usd"], minimum=Decimal("0.00000001"))
            fresh(quote["last_updated_at"], now, 6 * 3600)
            result[key] = {"usd": metric(price), "source": PRICE_API,
                           "observed_at_utc": utc_timestamp(quote["last_updated_at"]), "status": "live"}
        except (ValueError, TypeError, KeyError, SourceError, InvalidOperation):
            continue
    return result


def harvest_pool(pool, rpc, price, now, timeout):
    raw = {}
    sources = []
    observed = rpc.snapshot["timestamp"]
    if pool["id"] == "lido-steth":
        response = json_request(LIDO_API, timeout=timeout)
        meta, data = response["meta"], response["data"]
        if meta["address"].lower() != LIDO.lower() or meta["chainId"] != 1:
            raise SourceError("unexpected Lido asset")
        samples = data["aprs"]
        if not samples:
            raise SourceError("empty Lido APR observations")
        for sample in samples:
            fresh(sample["timeUnix"], now, 10 * 86400)
            number(sample["apr"], minimum=0, maximum=1000)
        observed = min(observed, max(sample["timeUnix"] for sample in samples))
        fresh(observed, now, 3 * 86400)
        values, source = rpc.read([(LIDO, calldata("getTotalPooledEther"))])
        raw = {"kind": "lido_apr", "net_apr_pct": str(number(data["smaApr"], minimum=0)),
               "apr_observations": samples, "pooled_ether_wei": str(uint_at(values[0]))}
        sources.extend([LIDO_API, source])
    elif pool["protocol"] == "Aave v3":
        values, source = rpc.read([(AAVE, calldata("getReserveData", pool["token"]))])
        reserve = values[0]
        # Aave V3 getReserveData keeps its legacy ABI: liquidityRate word 2,
        # aTokenAddress word 8, even where stable borrowing is deprecated.
        rate = uint_at(reserve, 2 * 32)
        atoken_int = uint_at(reserve, 8 * 32)
        if not 0 < atoken_int < 2 ** 160:
            raise SourceError("invalid Aave aToken address")
        atoken = "0x{:040x}".format(atoken_int)
        amounts, supply_source = rpc.read([(atoken, calldata("totalSupply"))])
        raw = {"kind": "aave_supply", "liquidity_rate_ray": str(rate),
               "atoken_address": atoken, "underlying_address": pool["token"],
               "underlying_decimals": pool["decimals"],
               "atoken_total_supply_raw": str(uint_at(amounts[0]))}
        sources.extend([source, supply_source])
    elif pool["id"] == "maker-dsr-dai":
        values, source = rpc.read([(POT, calldata(name)) for name in ("dsr", "Pie", "chi", "rho")])
        rate, pie, chi, rho = (uint_at(value) for value in values)
        raw = {"kind": "maker_dsr", "per_second_rate_ray": str(rate),
               "pie_wad": str(pie), "chi_ray": str(chi), "rho": rho,
               "block_timestamp": rpc.snapshot["timestamp"]}
        sources.append(source)
    elif pool["id"] == "sky-susds":
        values, source = rpc.read([(SUSDS, calldata("ssr")), (SUSDS, calldata("totalAssets"))])
        raw = {"kind": "sky_ssr", "per_second_rate_ray": str(uint_at(values[0])),
               "total_assets_raw": str(uint_at(values[1]))}
        sources.append(source)
    elif pool["id"] == "ethena-susde":
        past = rpc.historical_block()
        calls = [(SUSDE, calldata("convertToAssets", WAD)), (SUSDE, calldata("totalAssets"))]
        values, source = rpc.read(calls)
        previous, past_source = rpc.read(calls[:1], past["number"])
        raw = {"kind": "ethena_share_growth", "share_amount_wei": str(WAD),
               "assets_per_share_before": str(uint_at(previous[0])),
               "assets_per_share_after": str(uint_at(values[0])),
               "total_assets_raw": str(uint_at(values[1])),
               "window_seconds": rpc.snapshot["timestamp"] - past["timestamp"],
               "window_start": past}
        sources.extend([source, past_source])
    # The oldest component controls cache age; a fresh run must not re-date data.
    observed = min(observed, parse_timestamp(price["observed_at_utc"]))
    provenance = {"data_status": "live", "observed_at_utc": utc_timestamp(observed),
                  "sources": sorted(set(sources)), "block": rpc.snapshot}
    return build_record(pool, raw, price, provenance)


def mock_record(pool, reason):
    # These intentionally simple numbers are fixtures, not cached market quotes.
    if pool["id"] == "lido-steth":
        raw = {"kind": "lido_apr", "net_apr_pct": "3", "pooled_ether_wei": str(1_000_000 * WAD)}
    elif pool["protocol"] == "Aave v3":
        raw = {"kind": "aave_supply", "liquidity_rate_ray": str(4 * RAY // 100),
               "atoken_total_supply_raw": str(100_000_000 * 10 ** pool["decimals"]),
               "underlying_decimals": pool["decimals"]}
    elif pool["id"] == "maker-dsr-dai":
        raw = {"kind": "maker_dsr", "per_second_rate_ray": str(RAY + 5 * RAY // (100 * YEAR)),
               "pie_wad": str(100_000_000 * WAD), "chi_ray": str(RAY), "rho": 0, "block_timestamp": 0}
    elif pool["id"] == "sky-susds":
        raw = {"kind": "sky_ssr", "per_second_rate_ray": str(RAY + 5 * RAY // (100 * YEAR)),
               "total_assets_raw": str(100_000_000 * WAD)}
    else:
        raw = {"kind": "ethena_share_growth", "share_amount_wei": str(WAD),
               "assets_per_share_before": str(WAD), "assets_per_share_after": str(WAD * 1001 // 1000),
               "window_seconds": 7 * 86400, "total_assets_raw": str(100_000_000 * WAD)}
    price = {"usd": 2500 if pool["underlying"] == "ETH" else 1,
             "source": "synthetic fixture; not a market quote", "observed_at_utc": None, "status": "mock"}
    return build_record(pool, raw, price, {
        "data_status": "mock", "observed_at_utc": None, "sources": ["embedded synthetic inputs"],
        "fallback_reason": reason, "is_synthetic": True,
    })


def load_cache(path, now, max_age):
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        records = payload["records"]
        if (payload["schema_version"] != "1.0.0" or payload["record_count"] != len(records)
                or payload["sha256_checksum"] != checksum(records)):
            raise ValueError("cache checksum/schema/count mismatch")
        indexed = {record["id"]: record for record in records}
        if len(indexed) != len(records):
            raise ValueError("duplicate cache records")
    except (OSError, ValueError, TypeError, KeyError):
        return {}
    usable = {}
    for pool in POOLS:
        try:
            record = indexed[pool["id"]]
            validate_record(record, pool)
            provenance = record["provenance"]
            if provenance["data_status"] not in ("live", "cached") or provenance.get("is_synthetic"):
                continue
            observed = parse_timestamp(provenance["observed_at_utc"])
            fresh(observed, now, max_age)
            if record["price_provenance"]["status"] != "live":
                continue
            fresh(parse_timestamp(record["price_provenance"]["observed_at_utc"]), now, max_age)
            # Recalculate to reject incomplete or internally inconsistent caches.
            calculated = build_record(pool, record["raw_inputs"], record["price_provenance"], provenance)
            if any(calculated[key] != record[key] for key in ("apr_pct", "apy_pct", "tvl_usd", "tvl_underlying")):
                continue
            usable[pool["id"]] = record
        except (SourceError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation, OverflowError):
            continue
    return usable


def atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            # One dense record per line, with a small readable metadata envelope.
            metadata = {key: value for key, value in payload.items() if key != "records"}
            header = json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write(header[:-2] + ',\n  "records": [\n')
            handle.write(",\n".join("    " + canonical(record).decode("utf-8") for record in payload["records"]))
            handle.write("\n  ]\n}\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def run(output=DEFAULT_OUTPUT, offline=False, timeout=8.0, max_cache_age_hours=168):
    now = time.time()
    cached = load_cache(output, now, max_cache_age_hours * 3600)
    rpc, prices = None, {}
    failure = "offline mode requested"
    if not offline:
        urls = ([os.environ["ETHEREUM_RPC_URL"]] if os.environ.get("ETHEREUM_RPC_URL") else []) + list(RPC_URLS)
        try:
            rpc = Ethereum(urls, timeout, now)
        except SourceError as error:
            failure = str(error)
        prices = fetch_prices(now, timeout)
    records = []
    for pool in POOLS:
        try:
            if rpc is None:
                raise SourceError(failure)
            price = prices.get(pool["price_id"])
            if price is None:
                raise SourceError("fresh USD market quote unavailable")
            record = harvest_pool(pool, rpc, price, now, timeout)
        except (SourceError, ValueError, KeyError, TypeError, InvalidOperation, OverflowError) as error:
            reason = str(error) if isinstance(error, SourceError) else "invalid source data ({})".format(type(error).__name__)
            if pool["id"] in cached:
                record = copy.deepcopy(cached[pool["id"]])
                record["provenance"].update(data_status="cached", fallback_reason=reason,
                    cache_age_seconds=int(now - parse_timestamp(record["provenance"]["observed_at_utc"])))
            else:
                record = mock_record(pool, reason)
            print("{}: {} ({})".format(pool["id"], record["provenance"]["data_status"], reason), file=sys.stderr)
        records.append(record)
    records.sort(key=lambda record: record["id"])
    counts = Counter(record["provenance"]["data_status"] for record in records)
    payload = {
        "catalog_id": "feegate-sovereign-defi-yields", "schema_version": "1.0.0",
        "product_name": "FeeGate Sovereign DeFi Yield Intelligence",
        "target_audience": ["B2B treasury", "FinOps", "risk analytics"],
        "timestamp": utc_timestamp(), "record_count": len(records),
        "sha256_checksum": checksum(records),
        "checksum_scope": "records; UTF-8 JSON; sort_keys=true; separators=(',', ':'); ensure_ascii=false; allow_nan=false",
        "units": {"apr_pct": "percent per 365-day year", "apy_pct": "effective percent per 365-day year", "tvl_usd": "USD"},
        "quality_summary": {status: counts[status] for status in ("live", "cached", "mock")},
        "risk_categories": {category: sum(record["risk_category"] == category for record in records)
                            for category in ("Stablecoin", "Liquid Staking", "DEX")},
        "methodology": {
            "coverage": "Ethereum mainnet; Aave v3 USDC/DAI reserves; no DEX pools in requested scope",
            "yield": "Net protocol base yield; excludes incentive tokens, gas, withdrawal costs and USD price changes. Forward rates can change.",
            "tvl": "Underlying backing/supplied assets times market USD quotes; includes lent Aave assets and accrued Maker DSR. Overlapping TVLs are not additive.",
            "risk": "Qualitative exposure categories and engineering judgments, not credit ratings",
            "fallback": "Live inputs, then checksum-verified recent records, then explicitly synthetic calculations; mock observations have no market timestamp",
            "max_cache_age_hours": max_cache_age_hours,
        },
        "records": records,
    }
    atomic_write(output, payload)
    print("Wrote {} records to {} (live={}, cached={}, mock={}); sha256={}".format(
        len(records), output, counts["live"], counts["cached"], counts["mock"], payload["sha256_checksum"]))
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--offline", action="store_true", help="use verified cache or synthetic fixtures without network")
    parser.add_argument("--timeout", type=float, default=8.0, help="per-request timeout in seconds (default: 8)")
    parser.add_argument("--max-cache-age-hours", type=float, default=168, help="maximum original cache age (default: 168)")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        parser.error("--timeout must be in (0, 60]")
    if not math.isfinite(args.max_cache_age_hours) or args.max_cache_age_hours < 0:
        parser.error("--max-cache-age-hours must be finite and nonnegative")
    try:
        run(args.output, args.offline, args.timeout, args.max_cache_age_hours)
    except OSError as error:
        print("Cannot write output: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
