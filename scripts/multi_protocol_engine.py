#!/usr/bin/env python3
"""Harvest Ethereum DeFi yields as DefiLlama-compatible /pools JSON.

Run: python3 scripts/multi_protocol_engine.py --output /tmp/pools.json
Offline: python3 scripts/multi_protocol_engine.py --offline --output /tmp/pools.json

Uniswap v3 fees are a pool-wide volume/TVL proxy, not position-specific returns.
Forward Aave and Lido rates can change. Curve and Pendle rates are supplied by
their protocol APIs. Synthetic fallback pools are explicitly marked as mock.
"""

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import sovereign_yield_harvester as sovereign


DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/feegate-multi-protocol-pools.json"
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
DAI = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
CRV = "0xD533a949740bb3306d119CC777fa900bA034cd52"
PENDLE = "0x808507121B80c02388fad14726482e061B8da827"
UNISWAP_POOLS = (
    ("0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640", WETH, USDC),
    ("0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8", WETH, USDC),
)
CURVE_CATEGORIES = ("main", "crypto", "factory", "factory-crvusd")


def quantity(value, *, minimum=0, maximum=100000):
    """Accept finite financial values without bool, NaN, or implicit clamping."""
    if isinstance(value, bool):
        raise ValueError("boolean financial value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise ValueError("invalid financial value") from error
    if not result.is_finite() or result < minimum or result > maximum:
        raise ValueError("financial value outside bounds")
    return result


def percent(value, unit="decimal", *, periods=None):
    """Convert a decimal, percent, or ray APR into APY percentage points."""
    scale = {"decimal": Decimal(1), "percent": Decimal(100), "ray": Decimal(10) ** 27}
    if unit not in scale:
        raise ValueError("unsupported rate unit")
    rate = quantity(value, maximum=scale[unit] * 1000) / scale[unit]
    if periods is not None:
        if not isinstance(periods, int) or isinstance(periods, bool) or periods < 1:
            raise ValueError("invalid compounding periods")
        with localcontext() as context:
            context.prec = 60
            rate = (1 + rate / periods) ** periods - 1
    return float(rate * 100)


def address(value):
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise ValueError("invalid Ethereum address")
    return value.lower()


def pool(*, project, contract, symbol, tvl, base=None, reward=None,
         underlying=(), rewards=(), status="live", source, meta=None):
    contract = address(contract)
    base = None if base is None else float(quantity(base))
    reward = None if reward is None else float(quantity(reward))
    record = {
        "chain": "Ethereum", "project": project, "symbol": symbol,
        "tvlUsd": float(quantity(tvl, maximum=10 ** 15)),
        "apyBase": base, "apyReward": reward,
        "apy": round((base or 0) + (reward or 0), 8),
        "rewardTokens": [address(token) for token in rewards],
        "pool": contract, "underlyingTokens": [address(token) for token in underlying],
        "dataStatus": status, "source": source,
    }
    if meta:
        record["poolMeta"] = meta
    validate_pool(record)
    return record


def validate_pool(record):
    required = ("chain", "project", "symbol", "tvlUsd", "apyBase", "apyReward",
                "apy", "rewardTokens", "pool", "underlyingTokens", "dataStatus", "source")
    if any(key not in record for key in required):
        raise ValueError("missing LlamaFi pool field")
    if record["chain"] != "Ethereum" or record["project"] not in {
            "uniswap-v3", "aave-v3", "curve-dex", "lido", "pendle"}:
        raise ValueError("invalid chain or project")
    if not isinstance(record["symbol"], str) or not record["symbol"].strip():
        raise ValueError("empty symbol")
    if record["dataStatus"] not in {"live", "mock"} or not isinstance(record["source"], str) or not record["source"]:
        raise ValueError("invalid provenance")
    if address(record["pool"]) != record["pool"]:
        raise ValueError("pool address must be normalized")
    for field in ("rewardTokens", "underlyingTokens"):
        if not isinstance(record[field], list) or any(address(token) != token for token in record[field]):
            raise ValueError("invalid token addresses")
    quantity(record["tvlUsd"], maximum=10 ** 15)
    quantity(record["apy"])
    for field in ("apyBase", "apyReward"):
        if record[field] is not None:
            quantity(record[field])
    if not math.isclose(record["apy"], (record["apyBase"] or 0) + (record["apyReward"] or 0), abs_tol=1e-7):
        raise ValueError("APY components do not add up")
    json.dumps(record, allow_nan=False)


def fetch(url, timeout):
    return sovereign.json_request(url, timeout=timeout)


class UniswapV3Adapter:
    project = "uniswap-v3"

    def live(self, context):
        rows = []
        for contract, token0, token1 in UNISWAP_POOLS:
            try:
                url = "https://api.geckoterminal.com/api/v2/networks/eth/pools/" + contract
                data = fetch(url, context.timeout)["data"]
                attributes = data["attributes"]
                relationships = data["relationships"]
                if relationships["dex"]["data"]["id"] != "uniswap_v3" or address(attributes["address"]) != address(contract):
                    raise ValueError("unexpected Uniswap pool")
                actual_tokens = {address(relationships[key]["data"]["id"].split("_", 1)[1])
                                 for key in ("base_token", "quote_token")}
                if actual_tokens != {address(token0), address(token1)}:
                    raise ValueError("unexpected Uniswap tokens")
                fee = quantity(attributes["pool_fee_percentage"], maximum=100)
                volume = quantity(attributes["volume_usd"]["h24"], maximum=10 ** 15)
                tvl = quantity(attributes["reserve_in_usd"], maximum=10 ** 15)
                if tvl == 0:
                    raise ValueError("zero Uniswap TVL")
                fee_apr = volume * fee * 365 / tvl
                rows.append(pool(project=self.project, contract=contract,
                    symbol="WETH-USDC", tvl=tvl, base=fee_apr,
                    underlying=(token0, token1), source=url,
                    meta="Pool-wide 24h fee APR proxy; concentrated positions differ"))
            except (IndexError, KeyError, TypeError, ValueError, sovereign.SourceError):
                continue
        if not rows:
            raise sovereign.SourceError("Uniswap pool data unavailable")
        return rows

    def mock(self, reason):
        contract, token0, token1 = UNISWAP_POOLS[0]
        return [pool(project=self.project, contract=contract, symbol="WETH-USDC",
            tvl=1_000_000, base=5, underlying=(token0, token1),
            status="mock", source="embedded synthetic fixture", meta="Synthetic example: " + reason)]


class AaveV3Adapter:
    project = "aave-v3"

    def live(self, context):
        if context.rpc is None:
            raise sovereign.SourceError("Ethereum RPC unavailable")
        rows = []
        for config in sovereign.POOLS:
            if config["protocol"] != "Aave v3":
                continue
            price = context.prices.get(config["price_id"])
            if price is None:
                continue
            try:
                record = sovereign.harvest_pool(config, context.rpc, price, context.now, context.timeout)
                rows.append(pool(project=self.project, contract=record["raw_inputs"]["atoken_address"],
                    symbol=config["symbol"], tvl=record["tvl_usd"], base=record["apy_pct"],
                    underlying=(config["token"],), source=record["provenance"]["sources"][0],
                    meta="Current supply rate with per-second compounding approximation"))
            except (IndexError, KeyError, TypeError, ValueError, sovereign.SourceError):
                continue
        if not rows:
            raise sovereign.SourceError("Aave reserves or USD prices unavailable")
        return rows

    def mock(self, reason):
        return [pool(project=self.project, contract="0x98c23e9d8f34fefb1b7bd6a91b7ff122f4e16f5c",
            symbol="aUSDC", tvl=1_000_000, base=4, underlying=(USDC,),
            status="mock", source="embedded synthetic fixture", meta="Synthetic example: " + reason)]


class CurveAdapter:
    project = "curve-dex"

    def live(self, context):
        try:
            apy_url = "https://api.curve.finance/api/getFactoryAPYs?version=2"
            apy_data = fetch(apy_url, context.timeout)
            base_by_address = {address(item["poolAddress"]): item["apy"]
                               for item in apy_data["data"]["poolDetails"]}
        except (KeyError, TypeError, ValueError, sovereign.SourceError):
            base_by_address = {}
        rows = {}
        for category in CURVE_CATEGORIES:
            try:
                url = "https://api.curve.finance/api/getPools/ethereum/" + category
                response = fetch(url, context.timeout)
                if response["success"] is not True:
                    continue
                for item in response["data"]["poolData"]:
                    try:
                        contract = address(item["address"])
                        tvl = quantity(item["usdTotal"], maximum=10 ** 15)
                        if tvl <= 0 or item.get("isBroken"):
                            continue
                        tokens = [address(coin["address"]) for coin in item["coins"]]
                        base = base_by_address.get(contract)
                        gauge = item.get("gaugeCrvApy") or []
                        rewards = item.get("gaugeRewards") or []
                        reward = sum((quantity(gauge[0]) if gauge else Decimal(0),
                                      *(quantity(entry["apy"]) for entry in rewards)))
                        if base is None and not gauge and not rewards:
                            continue
                        reward_tokens = ([CRV] if gauge and quantity(gauge[0]) > 0 else []) + [
                            entry["tokenAddress"] for entry in rewards if quantity(entry["apy"]) > 0]
                        rows[contract] = pool(project=self.project, contract=contract,
                            symbol=item["symbol"], tvl=tvl, base=base, reward=reward,
                            underlying=tokens, rewards=reward_tokens, source=url,
                            meta="Unboosted CRV gauge rate; base APY from Curve factory API")
                    except (IndexError, KeyError, TypeError, ValueError, InvalidOperation):
                        continue
            except (IndexError, KeyError, TypeError, ValueError, sovereign.SourceError):
                continue
        if not rows:
            raise sovereign.SourceError("Curve pool data unavailable")
        return list(rows.values())

    def mock(self, reason):
        return [pool(project=self.project, contract="0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7",
            symbol="3Crv", tvl=1_000_000, base=2, reward=1, underlying=(DAI, USDC, USDT),
            rewards=(CRV,), status="mock", source="embedded synthetic fixture",
            meta="Synthetic example: " + reason)]


class LidoAdapter:
    project = "lido"

    def live(self, context):
        if context.rpc is None or "ethereum" not in context.prices:
            raise sovereign.SourceError("Ethereum RPC or ETH price unavailable")
        config = sovereign.POOLS[0]
        record = sovereign.harvest_pool(config, context.rpc, context.prices["ethereum"],
                                        context.now, context.timeout)
        return [pool(project=self.project, contract=sovereign.LIDO, symbol="stETH",
            tvl=record["tvl_usd"], base=record["apy_pct"], underlying=(WETH,),
            source=sovereign.LIDO_API, meta="Trailing net APR with daily compounding approximation")]

    def mock(self, reason):
        return [pool(project=self.project, contract=sovereign.LIDO, symbol="stETH",
            tvl=1_000_000, base=3, underlying=(WETH,), status="mock",
            source="embedded synthetic fixture", meta="Synthetic example: " + reason)]


class PendleAdapter:
    project = "pendle"

    def live(self, context):
        url = "https://api-v2.pendle.finance/core/v1/1/markets/active"
        response = fetch(url, context.timeout)
        rows = []
        for market in response["markets"]:
            try:
                expiry = datetime.fromisoformat(market["expiry"].replace("Z", "+00:00"))
                if expiry.tzinfo is None or expiry.timestamp() <= context.now:
                    continue
                details = market["details"]
                total = quantity(details["aggregatedApy"], maximum=1000)
                reward = quantity(details["pendleApy"], maximum=1000)
                if reward > total:
                    raise ValueError("Pendle reward exceeds total")
                underlying = address(market["underlyingAsset"].split("-", 1)[1])
                rows.append(pool(project=self.project, contract=market["address"],
                    symbol=market["name"], tvl=details["liquidity"],
                    base=percent(total - reward), reward=percent(reward),
                    underlying=(underlying,), rewards=(PENDLE,) if reward else (),
                    source=url, meta="LP aggregated APY; expires " + expiry.date().isoformat()))
            except (IndexError, KeyError, TypeError, ValueError, InvalidOperation):
                continue
        if not rows:
            raise sovereign.SourceError("active Pendle markets unavailable")
        return rows

    def mock(self, reason):
        return [pool(project=self.project, contract="0x34280882267ffa6383b363e278b027be083bbe3b",
            symbol="wstETH", tvl=1_000_000, base=3, reward=0.5,
            underlying=("0x7f39C581F595B53c5cb19BD0b3f8dA6c935E2Ca0",), rewards=(PENDLE,),
            status="mock", source="embedded synthetic fixture", meta="Synthetic example: " + reason)]


class Context:
    def __init__(self, *, offline=False, timeout=8.0, now=None):
        self.now = time.time() if now is None else now
        self.timeout = timeout
        self.rpc = None
        self.prices = {}
        if not offline:
            urls = ([sovereign.os.environ["ETHEREUM_RPC_URL"]]
                    if sovereign.os.environ.get("ETHEREUM_RPC_URL") else []) + list(sovereign.RPC_URLS)
            try:
                self.rpc = sovereign.Ethereum(urls, timeout, self.now)
            except sovereign.SourceError:
                pass
            self.prices = sovereign.fetch_prices(self.now, timeout)


ADAPTERS = (UniswapV3Adapter(), AaveV3Adapter(), CurveAdapter(), LidoAdapter(), PendleAdapter())


def write_output(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp",
                                         delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def run(*, offline=False, timeout=8.0, output=None, context=None):
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("timeout must be in (0, 60]")
    context = context or Context(offline=offline, timeout=timeout)
    records = []
    for adapter in ADAPTERS:
        try:
            if offline:
                raise sovereign.SourceError("offline mode requested")
            rows = adapter.live(context)
        except (sovereign.SourceError, IndexError, KeyError, TypeError, ValueError, OSError) as error:
            rows = adapter.mock(str(error)[:160])
        records.extend(rows)
    records.sort(key=lambda item: (item["project"], item["pool"]))
    identities = [(item["project"], item["pool"]) for item in records]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate project/pool identity")
    for record in records:
        validate_pool(record)
    result = {"status": "success", "data": records}
    if output is not None:
        write_output(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        result = run(offline=args.offline, timeout=args.timeout, output=args.output)
    except (ValueError, OSError) as error:
        parser.exit(1, "Cannot write LlamaFi pools: {}\n".format(error))
    counts = {status: sum(row["dataStatus"] == status for row in result["data"])
              for status in ("live", "mock")}
    print("Wrote {} pools to {} (live={}, mock={})".format(
        len(result["data"]), args.output, counts["live"], counts["mock"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
