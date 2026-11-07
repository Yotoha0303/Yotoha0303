#!/usr/bin/env python3
"""GPU rental price radar, using only the Python standard library.

Examples::

    python3 scripts/rentgpu_arbitrage_scanner.py --offline --output /tmp/gpus.json
    python3 scripts/rentgpu_arbitrage_scanner.py --input quotes.json --history yesterday.json
    python3 scripts/rentgpu_arbitrage_scanner.py --live runpod --live lambda
    python3 scripts/rentgpu_arbitrage_scanner.py --source aws=https://example.org/aws.json
    python3 scripts/rentgpu_arbitrage_scanner.py --verify /tmp/gpus.json

Input is an array or {"records": [...]} of normalized quote inputs. Required
fields: provider, offer_id, gpu, region, market (spot/on_demand), price,
price_unit (gpu_hour/instance_hour/instance_minute/instance_second), observed_at.
Optional: num_gpus=1, vcpus=null, available=true, currency=USD, region_group=region.
History accepts the same input or a verified export. GPU variants must be explicit
(e.g. H100-SXM-80GB); ambiguous names such as A100 are rejected. Unknown resource
counts stay null. Prices are exact decimal strings; derived ratios round to 8 dp.

AWS, Azure, GCP and other providers accept operator-supplied normalized feeds;
built-in live adapters cover RunPod's GPU catalog and Lambda's instance API.
LAMBDA_API_KEY is used only in the request header. RunPod catalog prices have
unknown region/vCPU capacity and are marked global with vcpus=null. They are
catalog quotes, not reservations. Failed live sources never become mock prices.
--offline generates deterministic synthetic observations across all providers;
it makes no claim about actual hardware availability or current market rates.

Spreads compare fresh, available quotes of the same GPU variant, market, region
group, GPU count and data status. Regional equivalence is explicitly supplied by
the feed operator. No spot/on-demand or live/mock mixing occurs. Savings exclude
taxes, storage, transfer, interruptions and migration costs. They are procurement
opportunities, not guaranteed resale profits. Volatility uses the last sample
in each UTC hour for the same offer/configuration; gaps cannot count as hourly
changes. The latest price is tested against up to 24 prior hourly prices.

The repository's LlamaFi economic /pools extension uses null TVL/APY for rental
quotes. SHA-256 covers compact, sorted UTF-8 JSON, excluding only the object's
own sha256_checksum. Hashes detect changes, not publisher identity. With --pay-to
and --resource-url, exports include x402 v2 PaymentRequired metadata (Base USDC,
default 50000 atomic units = $0.05) and its PAYMENT-REQUIRED header. Settlement
and HTTP payment enforcement belong to the serving FeeGate gateway.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import itertools
import json
import os
import re
import statistics
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit


SCHEMA_VERSION = "1.0.0"
CATALOG_ID = "feegate-gpu-rental-arbitrage"
SCHEMA = "llamafi-economic-pools"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/feegate-gpu-rental-pools.json"
CHECKSUM_SCOPE = "UTF-8 JSON; sort_keys=true; separators=(',', ':'); ensure_ascii=false; allow_nan=false; exclude own sha256_checksum"
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 50000
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PROVIDERS = {
    "aws": "Amazon Web Services", "azure": "Microsoft Azure", "gcp": "Google Cloud",
    "runpod": "RunPod", "vast": "Vast.ai", "lambda": "Lambda Cloud",
    "coreweave": "CoreWeave", "paperspace": "Paperspace", "fluidstack": "Fluidstack",
    "nebius": "Nebius", "crusoe": "Crusoe Cloud", "tensordock": "TensorDock",
    "hyperstack": "Hyperstack",
}
RUNPOD_URL = "https://api.runpod.io/graphql"
LAMBDA_URL = "https://cloud.lambdalabs.com/api/v1/instance-types"


class SourceError(ValueError):
    """A price source was unavailable or malformed."""


class IntegrityError(ValueError):
    """An export failed schema or fingerprint verification."""


@dataclass(frozen=True)
class GPUCard:
    gpu_id: str
    name: str
    vendor: str
    vram_gib: int
    aliases: tuple[str, ...] = ()


_CARDS = (
    ("b200-180gb", "B200 180GB", 180, ("B200",)),
    ("h200-sxm-141gb", "H200 SXM 141GB", 141, ("H200", "H200 SXM")),
    ("h100-sxm-80gb", "H100 SXM 80GB", 80, ("H100 SXM", "H100 SXM5", "H100 SXM5 80GB", "H100 80GB HBM3")),
    ("h100-pcie-80gb", "H100 PCIe 80GB", 80, ("H100 PCIe",)),
    ("h100-nvl-94gb", "H100 NVL 94GB", 94, ("H100 NVL",)),
    ("a100-sxm4-80gb", "A100 SXM4 80GB", 80, ("A100 SXM 80GB",)),
    ("a100-pcie-80gb", "A100 PCIe 80GB", 80, ()),
    ("a100-sxm4-40gb", "A100 SXM4 40GB", 40, ("A100 SXM 40GB",)),
    ("a100-pcie-40gb", "A100 PCIe 40GB", 40, ()),
    ("a800-80gb", "A800 80GB", 80, ("A800",)),
    ("h800-80gb", "H800 80GB", 80, ("H800",)),
    ("l40s-48gb", "L40S 48GB", 48, ("L40S",)),
    ("l40-48gb", "L40 48GB", 48, ("L40",)),
    ("l4-24gb", "L4 24GB", 24, ("L4",)),
    ("a10-24gb", "A10 24GB", 24, ("A10",)),
    ("a10g-24gb", "A10G 24GB", 24, ("A10G",)),
    ("a40-48gb", "A40 48GB", 48, ("A40",)),
    ("rtx-a6000-48gb", "RTX A6000 48GB", 48, ("RTX A6000",)),
    ("rtx-6000-ada-48gb", "RTX 6000 Ada 48GB", 48, ("RTX 6000 Ada", "RTX 6000 Ada Generation")),
    ("rtx-5000-ada-32gb", "RTX 5000 Ada 32GB", 32, ("RTX 5000 Ada",)),
    ("rtx-4500-ada-24gb", "RTX 4500 Ada 24GB", 24, ("RTX 4500 Ada",)),
    ("rtx-4000-ada-20gb", "RTX 4000 Ada 20GB", 20, ("RTX 4000 Ada",)),
    ("quadro-rtx-8000-48gb", "Quadro RTX 8000 48GB", 48, ("Quadro RTX 8000",)),
    ("quadro-rtx-6000-24gb", "Quadro RTX 6000 24GB", 24, ("Quadro RTX 6000",)),
    ("rtx-a5000-24gb", "RTX A5000 24GB", 24, ("RTX A5000",)),
    ("rtx-a4500-20gb", "RTX A4500 20GB", 20, ("RTX A4500",)),
    ("rtx-a4000-16gb", "RTX A4000 16GB", 16, ("RTX A4000",)),
    ("rtx-4090-24gb", "RTX 4090 24GB", 24, ("RTX 4090", "4090")),
    ("rtx-4080-16gb", "RTX 4080 16GB", 16, ("RTX 4080",)),
    ("rtx-4080-super-16gb", "RTX 4080 SUPER 16GB", 16, ("RTX 4080 SUPER",)),
    ("rtx-4070-ti-12gb", "RTX 4070 Ti 12GB", 12, ("RTX 4070 Ti",)),
    ("rtx-3090-24gb", "RTX 3090 24GB", 24, ("RTX 3090",)),
    ("rtx-3080-10gb", "RTX 3080 10GB", 10, ()),
    ("rtx-3080-ti-12gb", "RTX 3080 Ti 12GB", 12, ("RTX 3080 Ti",)),
    ("rtx-2080-ti-11gb", "RTX 2080 Ti 11GB", 11, ("RTX 2080 Ti",)),
    ("t4-16gb", "T4 16GB", 16, ("T4",)),
    ("v100-sxm2-16gb", "V100 SXM2 16GB", 16, ()),
    ("v100-sxm2-32gb", "V100 SXM2 32GB", 32, ()),
    ("v100-pcie-16gb", "V100 PCIe 16GB", 16, ()),
    ("v100-pcie-32gb", "V100 PCIe 32GB", 32, ()),
    ("p100-16gb", "P100 16GB", 16, ("P100",)),
    ("p40-24gb", "P40 24GB", 24, ("P40",)),
    ("p4-8gb", "P4 8GB", 8, ("P4",)),
)
GPU_CATALOG = {key: GPUCard(key, name, "NVIDIA", memory, aliases)
               for key, name, memory, aliases in _CARDS}
for _key, _name, _memory, _aliases in (
        ("mi300x-192gb", "Instinct MI300X 192GB", 192, ("MI300X",)),
        ("mi210-64gb", "Instinct MI210 64GB", 64, ("MI210",)),
        ("rx-7900-xtx-24gb", "Radeon RX 7900 XTX 24GB", 24, ("RX 7900 XTX",))):
    GPU_CATALOG[_key] = GPUCard(_key, _name, "AMD", _memory, _aliases)


def _gpu_key(value):
    return re.sub(r"[^a-z0-9]", "", re.sub(
        r"\b(nvidia|amd|tesla|geforce)\b", "", value.lower()))


_GPU_ALIASES = {}
for _card in GPU_CATALOG.values():
    for _alias in (_card.gpu_id, _card.name, *_card.aliases):
        _GPU_ALIASES.setdefault(_gpu_key(_alias), set()).add(_card.gpu_id)
        # Lambda writes e.g. "H100 (80 GB SXM5)"; RunPod uses "H100 SXM5".
        _reordered = re.sub(r"\b(SXM[0-9]?|PCIe)\s+(\d+)\s*GB\b", r"\2GB \1", _alias)
        _GPU_ALIASES.setdefault(_gpu_key(_reordered), set()).add(_card.gpu_id)
    if (_card.gpu_id.startswith(("rtx-", "quadro-", "l4-", "l40-", "l40s-", "a10-", "a10g-"))
            or _card.gpu_id in {"t4-16gb", "p4-8gb", "p40-24gb", "p100-16gb"}):
        _GPU_ALIASES.setdefault(_gpu_key(_card.name + " PCIe"), set()).add(_card.gpu_id)


def resolve_gpu(value):
    if not isinstance(value, str):
        raise ValueError("gpu must name a catalog GPU variant")
    matches = _GPU_ALIASES.get(_gpu_key(value), set())
    if len(matches) != 1:
        raise ValueError("unknown or ambiguous GPU variant: " + value)
    return GPU_CATALOG[next(iter(matches))]


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def checksum(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _unsigned(value):
    return {key: item for key, item in value.items() if key != "sha256_checksum"}


def _number(value, name, *, minimum=0, maximum=1000000, positive=False):
    if isinstance(value, bool):
        raise ValueError(name + " cannot be boolean")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("invalid " + name) from error
    if not result.is_finite() or not minimum <= result <= maximum or (positive and result == 0):
        raise ValueError(name + " outside bounds")
    return result


def _integer(value, name, minimum=1, maximum=1024):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(name + " must be a bounded integer")
    return value


def _text(value, name):
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or any(ord(c) < 32 for c in value)):
        raise ValueError("invalid " + name)
    return value.strip()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("expected timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 for c in value):
        raise ValueError("expected HTTPS URL without credentials, query or fragment")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("expected HTTPS URL without credentials, query or fragment")
    return value


def _decimal_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _ratio(value):
    return round(float(value), 8)


@dataclass(frozen=True)
class RentalQuote:
    provider: str
    offer_id: str
    gpu: str
    region: str
    region_group: str
    market: str
    instance_hour_usd: Decimal
    num_gpus: int
    vcpus: int | None
    available: bool
    observed_at: str
    data_status: str
    source: str

    def __post_init__(self):
        if self.provider not in PROVIDERS or self.gpu not in GPU_CATALOG:
            raise ValueError("unsupported provider or GPU")
        for field in ("offer_id", "region", "region_group"):
            if _text(getattr(self, field), field) != getattr(self, field):
                raise ValueError("quote text must be normalized")
        if self.market not in {"spot", "on_demand"} or self.data_status not in {"live", "provided", "mock"}:
            raise ValueError("invalid market or data_status")
        if type(self.available) is not bool:
            raise ValueError("available must be boolean")
        _integer(self.num_gpus, "num_gpus")
        if self.vcpus is not None:
            _integer(self.vcpus, "vcpus", maximum=65536)
        if not isinstance(self.instance_hour_usd, Decimal):
            raise ValueError("instance_hour_usd must be Decimal")
        _number(self.instance_hour_usd, "hourly price", minimum=Decimal("1e-9"), maximum=10**9)
        timestamp(self.observed_at)
        if self.data_status == "live":
            safe_url(self.source)
        elif self.source != {"provided": "local-input", "mock": "embedded://synthetic-gpu-v1"}[self.data_status]:
            raise ValueError("invalid quote provenance")

    @property
    def gpu_hour_usd(self):
        return self.instance_hour_usd / self.num_gpus

    @property
    def series_key(self):
        return (self.provider, self.offer_id, self.gpu, self.region, self.region_group,
                self.market, self.num_gpus, self.vcpus, self.data_status)

    @property
    def pool_id(self):
        return "gpu-" + checksum(self.series_key)[:32]

    def to_input(self):
        return {"provider": self.provider, "offer_id": self.offer_id, "gpu": self.gpu,
                "region": self.region, "region_group": self.region_group, "market": self.market,
                "price": _decimal_text(self.instance_hour_usd), "price_unit": "instance_hour",
                "currency": "USD", "num_gpus": self.num_gpus, "vcpus": self.vcpus,
                "available": self.available, "observed_at": self.observed_at}


def normalize_quote(row, *, status="provided", source="local-input", provider=None):
    if not isinstance(row, dict):
        raise ValueError("quote must be an object")
    if provider is not None and row.get("provider", provider) != provider:
        raise ValueError("source/provider mismatch")
    name = provider or row.get("provider")
    card = resolve_gpu(row.get("gpu"))
    count = _integer(row.get("num_gpus", 1), "num_gpus")
    if row.get("currency", "USD") != "USD":
        raise ValueError("only USD quotes supported; convert currency upstream")
    price = _number(row.get("price"), "price", minimum=Decimal("1e-9"), positive=True)
    unit = row.get("price_unit")
    factors = {"gpu_hour": count, "instance_hour": 1, "instance_minute": 60, "instance_second": 3600}
    if unit not in factors:
        raise ValueError("unsupported price_unit")
    region = _text(row.get("region"), "region")
    return RentalQuote(name, _text(row.get("offer_id"), "offer_id"), card.gpu_id, region,
                       _text(row.get("region_group", region), "region_group"), row.get("market"),
                       price * factors[unit], count, row.get("vcpus"), row.get("available", True),
                       timestamp(row.get("observed_at")).isoformat(), status, source)


def price_performance(quote):
    """Whole-instance resource ratios; vCPU ratios are null when unknown."""
    memory = GPU_CATALOG[quote.gpu].vram_gib * quote.num_gpus
    price = quote.instance_hour_usd
    return {"usd_per_vram_gib_hour": _ratio(price / memory),
            "vram_gib_per_usd_hour": _ratio(Decimal(memory) / price),
            "usd_per_vcpu_hour": _ratio(price / quote.vcpus) if quote.vcpus else None,
            "vcpus_per_usd_hour": _ratio(Decimal(quote.vcpus) / price) if quote.vcpus else None}


def latest_quotes(quotes, *, now, max_age_seconds=3600):
    _number(max_age_seconds, "max_age_seconds", maximum=7 * 86400, positive=True)
    clock = timestamp(now)
    latest, seen = {}, {}
    for quote in quotes:
        observed = timestamp(quote.observed_at)
        if observed > clock:
            raise ValueError("quote observation is later than scan timestamp")
        identity = (quote.series_key, observed)
        if identity in seen and seen[identity] != quote:
            raise ValueError("conflicting observations for the same offer and timestamp")
        seen[identity] = quote
        previous = latest.get(quote.series_key)
        if previous is None or observed > timestamp(previous.observed_at):
            latest[quote.series_key] = quote
    fresh = [quote for quote in latest.values() if quote.available and
             (clock - timestamp(quote.observed_at)).total_seconds() <= float(max_age_seconds)]
    return sorted(fresh, key=lambda q: q.pool_id)


def compute_spreads(quotes, *, min_spread_pct=0):
    threshold = _number(min_spread_pct, "min_spread_pct", maximum=10**12)
    groups = {}
    for quote in quotes:
        if not quote.available:
            continue
        key = (quote.gpu, quote.market, quote.region_group, quote.num_gpus, quote.data_status)
        providers = groups.setdefault(key, {})
        previous = providers.get(quote.provider)
        if previous is None or (quote.gpu_hour_usd, quote.pool_id) < (previous.gpu_hour_usd, previous.pool_id):
            providers[quote.provider] = quote
    spreads = []
    for key, providers in sorted(groups.items()):
        if len(providers) < 2:
            continue
        ordered = sorted(providers.values(), key=lambda q: (q.gpu_hour_usd, q.provider, q.pool_id))
        low, high = ordered[0], ordered[-1]
        delta = high.gpu_hour_usd - low.gpu_hour_usd
        percent = delta / low.gpu_hour_usd * 100
        if delta == 0 or percent < threshold:
            continue
        pairs = [{"cheaper_pool": a.pool_id, "dearer_pool": b.pool_id,
                  "usd_per_gpu_hour": _ratio(b.gpu_hour_usd - a.gpu_hour_usd),
                  "spread_pct": _ratio((b.gpu_hour_usd / a.gpu_hour_usd - 1) * 100)}
                 for a, b in itertools.combinations(ordered, 2)]
        spreads.append({"gpu": key[0], "market": key[1], "region_group": key[2],
                        "num_gpus": key[3], "data_status": key[4], "provider_count": len(providers),
                        "cheapest_pool": low.pool_id, "dearest_pool": high.pool_id,
                        "cheapest_provider": low.provider, "dearest_provider": high.provider,
                        "min_usd_per_gpu_hour": _ratio(low.gpu_hour_usd),
                        "max_usd_per_gpu_hour": _ratio(high.gpu_hour_usd),
                        "median_usd_per_gpu_hour": _ratio(statistics.median(q.gpu_hour_usd for q in ordered)),
                        "spread_usd_per_gpu_hour": _ratio(delta), "spread_pct": _ratio(percent),
                        "savings_pct": _ratio(delta / high.gpu_hour_usd * 100),
                        "instance_savings_usd_hour": _ratio(delta * key[3]), "pairs": pairs})
    return sorted(spreads, key=lambda row: (-row["spread_pct"], row["cheapest_pool"]))


def detect_volatility(quotes, current, *, baseline_hours=24, min_history=3,
                      change_threshold_pct=20, z_threshold=3):
    _integer(baseline_hours, "baseline_hours", 3, 168)
    _integer(min_history, "min_history", 3, baseline_hours)
    change_threshold = float(_number(change_threshold_pct, "change_threshold_pct", positive=True))
    z_limit = float(_number(z_threshold, "z_threshold", positive=True))
    series = {}
    for quote in quotes:
        hour = timestamp(quote.observed_at).replace(minute=0, second=0, microsecond=0)
        buckets = series.setdefault(quote.series_key, {})
        if hour not in buckets or timestamp(quote.observed_at) > timestamp(buckets[hour].observed_at):
            buckets[hour] = quote
    result = []
    for quote in current:
        hour = timestamp(quote.observed_at).replace(minute=0, second=0, microsecond=0)
        buckets = series.get(quote.series_key, {})
        baseline = [buckets[hour - timedelta(hours=offset)].gpu_hour_usd
                    for offset in range(1, baseline_hours + 1) if hour - timedelta(hours=offset) in buckets]
        previous = buckets.get(hour - timedelta(hours=1))
        mean = statistics.mean(baseline) if baseline else None
        std = statistics.pstdev(baseline) if baseline else None
        change = (quote.gpu_hour_usd / previous.gpu_hour_usd - 1) * 100 if previous else None
        z = (quote.gpu_hour_usd - mean) / std if std else None
        anomaly = (len(baseline) >= min_history and change is not None
                   and abs(change) >= change_threshold
                   and (abs(z) >= z_limit if z is not None else quote.gpu_hour_usd != mean))
        state = ("hour_gap" if previous is None else "insufficient_history" if len(baseline) < min_history
                 else "spike" if anomaly and change > 0 else "drop" if anomaly else "normal")
        result.append({"pool": quote.pool_id, "baseline_samples": len(baseline),
                       "baseline_mean_usd_per_gpu_hour": _ratio(mean) if mean is not None else None,
                       "baseline_stddev_usd_per_gpu_hour": _ratio(std) if std is not None else None,
                       "coefficient_of_variation_pct": _ratio(std / mean * 100) if mean else None,
                       "hourly_change_pct": _ratio(change) if change is not None else None,
                       "z_score": _ratio(z) if z is not None else None,
                       "is_anomaly": bool(anomaly), "state": state})
    return sorted(result, key=lambda row: row["pool"])


def to_pool(quote):
    card = GPU_CATALOG[quote.gpu]
    result = {"pool": quote.pool_id, "chain": "Offchain", "project": "gpu-rental-" + quote.provider,
              "symbol": card.name, "tvlUsd": None, "apy": None, "apyBase": None, "apyReward": None,
              "underlyingTokens": [], "rewardTokens": [], "dataStatus": quote.data_status,
              "source": quote.source, "economic": {"quote": quote.to_input(),
                  "usd_per_gpu_hour": _decimal_text(quote.gpu_hour_usd),
                  "gpu_vendor": card.vendor, "vram_gib_per_gpu": card.vram_gib,
                  "price_performance": price_performance(quote)}}
    result["sha256_checksum"] = checksum(result)
    return result


def payment_required(pay_to, resource_url, amount="50000"):
    if not isinstance(pay_to, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", pay_to) or int(pay_to[2:], 16) == 0:
        raise ValueError("pay_to must be a nonzero EVM address")
    safe_url(resource_url)
    if not isinstance(amount, str) or not re.fullmatch(r"[1-9][0-9]{0,17}", amount):
        raise ValueError("payment amount must be positive atomic USDC units")
    return {"x402Version": 2, "resource": {"url": resource_url,
                "description": "FeeGate GPU rental spot-price and arbitrage radar", "mimeType": "application/json"},
            "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": amount,
                         "asset": USDC_BASE, "payTo": pay_to, "maxTimeoutSeconds": 60,
                         "extra": {"name": "USD Coin", "version": "2"}}]}


def build_payload(quotes, *, now=None, max_age_seconds=3600, min_spread_pct=0,
                  baseline_hours=24, min_history=3, change_threshold_pct=20, z_threshold=3,
                  pay_to=None, resource_url=None, payment_amount="50000", source_failures=()):
    quotes = list(quotes)
    clock = timestamp(now or utc_now()).isoformat()
    current = latest_quotes(quotes, now=clock, max_age_seconds=max_age_seconds)
    options = {"max_age_seconds": max_age_seconds, "min_spread_pct": min_spread_pct,
               "baseline_hours": baseline_hours, "min_history": min_history,
               "change_threshold_pct": change_threshold_pct, "z_threshold": z_threshold}
    spreads = compute_spreads(current, min_spread_pct=min_spread_pct)
    volatility = detect_volatility(quotes, current, baseline_hours=baseline_hours, min_history=min_history,
                                   change_threshold_pct=change_threshold_pct, z_threshold=z_threshold)
    if not current:
        raise SourceError("no fresh, available GPU rental quotes")
    if bool(pay_to) != bool(resource_url):
        raise ValueError("pay_to and resource_url must be configured together")
    payment = payment_required(pay_to, resource_url, payment_amount) if pay_to else None
    transport = {"protocol": "HTTP REST / x402 Micropayments", "enabled": payment is not None,
                 "payment_required": payment, "payment_required_header":
                     base64.b64encode(canonical_bytes(payment)).decode("ascii") if payment else None}
    # Retain normalized history so consumers can reproduce anomaly calculations.
    unique = {(q.series_key, q.observed_at): q for q in quotes}
    history = [{"quote": q.to_input(), "data_status": q.data_status, "source": q.source}
               for q in sorted(unique.values(), key=lambda q: (q.pool_id, q.observed_at))]
    payload = {"status": "success", "schema": SCHEMA, "schema_version": SCHEMA_VERSION,
               "catalog_id": CATALOG_ID, "generated_at_utc": clock, "record_count": len(current),
               "checksum_scope": CHECKSUM_SCOPE, "scanner_options": options, "transport": transport,
               "quality_summary": {"status_counts": {s: sum(q.data_status == s for q in current)
                   for s in ("live", "provided", "mock")}, "providers": len({q.provider for q in current}),
                   "gpu_types": len({q.gpu for q in current}), "historical_observations": len(history)},
               "methodology": {"spreads": "Same GPU variant, market, region_group, GPU count and data status; cheapest offer per provider",
                   "volatility": "Last observation per UTC hour; prior hourly price baseline; change requires adjacent hour",
                   "financial_fields": "TVL and APY are null; prices are rental costs, not investment returns",
                   "limitations": "Quotes do not reserve capacity; savings exclude tax, storage, network, migration and interruption costs",
                   "resource_ratios": "Resource capacity per rental dollar; vCPU architecture and workload throughput are not benchmarked"},
               "source_failures": list(source_failures), "data": [to_pool(q) for q in current],
               "price_history": history, "spreads": spreads, "volatility": volatility}
    payload["sha256_checksum"] = checksum(payload)
    return payload


def verify_integrity(payload):
    """Check hashes, pool schema and independently reproduce the entire scan."""
    try:
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("invalid envelope")
        digest = payload.get("sha256_checksum")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not hmac.compare_digest(digest, checksum(_unsigned(payload))):
            raise ValueError("document SHA-256 mismatch")
        history = payload["price_history"]
        if not isinstance(history, list) or not 1 <= len(history) <= MAX_ROWS:
            raise ValueError("invalid price history")
        quotes = [normalize_quote(item["quote"], status=item["data_status"], source=item["source"]) for item in history]
        payment = payload["transport"]["payment_required"]
        payment_options = {} if payment is None else {"pay_to": payment["accepts"][0]["payTo"],
            "resource_url": payment["resource"]["url"], "payment_amount": payment["accepts"][0]["amount"]}
        expected = build_payload(quotes, now=payload["generated_at_utc"], **payload["scanner_options"],
                                 source_failures=payload["source_failures"], **payment_options)
        if canonical_bytes(expected) != canonical_bytes(payload):
            raise ValueError("export violates reproducible GPU radar schema")
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as error:
        raise IntegrityError(str(error)) from error
    return True


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise SourceError("non-finite JSON constant: " + value)


def strict_json(raw):
    if len(raw) > MAX_BYTES:
        raise SourceError("JSON exceeds size limit")
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def read_quotes(document, *, status="provided", source="local-input", provider=None):
    rows = document.get("records") if isinstance(document, dict) else document
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise SourceError("source must contain a nonempty bounded quote array")
    return [normalize_quote(row, status=status, source=source, provider=provider) for row in rows]


def fetch_json(url, *, timeout=15, headers=None, body=None):
    safe_url(url)
    _number(timeout, "timeout", maximum=60, positive=True)
    request = urllib.request.Request(url, data=canonical_bytes(body) if body is not None else None,
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "FeeGate-GPU-Radar/1.0", **(headers or {})})
    try:
        # Reject before following: urllib otherwise forwards Authorization headers.
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=float(timeout)) as response:
            if response.geturl() != url:
                raise SourceError("price API redirected")
            raw = response.read(MAX_BYTES + 1)
        return strict_json(raw)
    except (OSError, ValueError) as error:
        raise SourceError("price API request failed") from error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceError("price API redirects are unsupported")


def runpod_quotes(document, *, observed_at):
    if not isinstance(document, dict) or document.get("errors"):
        raise SourceError("RunPod GraphQL response failed")
    rows = document.get("data", {}).get("gpuTypes")
    if not isinstance(rows, list) or not rows:
        raise SourceError("RunPod GPU catalog missing")
    quotes = []
    for row in rows:
        try:
            card = resolve_gpu(row["displayName"])
        except (ValueError, KeyError):
            continue  # New/ambiguous hardware cannot silently become another variant.
        if row.get("memoryInGb") != card.vram_gib:
            raise SourceError("RunPod GPU memory contradicts catalog variant")
        for cloud in ("secure", "community"):
            for market, suffix in (("spot", "SpotPrice"), ("on_demand", "Price")):
                price = row.get(cloud + suffix)
                if price is None or price == 0:
                    continue
                quotes.append(normalize_quote({"offer_id": str(row["id"]) + "-" + cloud,
                    "gpu": card.gpu_id, "region": "global", "market": market, "price": price,
                    "price_unit": "gpu_hour", "observed_at": observed_at,
                    "available": row.get(cloud + "Cloud", False)}, provider="runpod", status="live", source=RUNPOD_URL))
    if not quotes:
        raise SourceError("RunPod has no supported GPU prices")
    return quotes


def lambda_quotes(document, *, observed_at):
    rows = document.get("data") if isinstance(document, dict) else None
    if not isinstance(rows, dict) or not rows:
        raise SourceError("Lambda instance catalog missing")
    quotes = []
    for entry in rows.values():
        instance = entry["instance_type"]
        specs = instance["specs"]
        try:
            description = instance.get("gpu_description", instance.get("description", ""))
            # Some responses expose only the full instance description, e.g. 8x H100.
            description = re.sub(r"^\s*[0-9]+x\s+", "", description)
            card = resolve_gpu(description)
        except ValueError:
            continue
        for region in entry.get("regions_with_capacity_available", []):
            quotes.append(normalize_quote({"offer_id": instance["name"], "gpu": card.gpu_id,
                "region": region["name"], "market": "on_demand", "num_gpus": specs["gpus"],
                "vcpus": specs["vcpus"], "price": _number(instance["price_cents_per_hour"], "cents") / 100,
                "price_unit": "instance_hour", "observed_at": observed_at},
                provider="lambda", status="live", source=LAMBDA_URL))
    if not quotes:
        raise SourceError("Lambda has no supported GPU capacity")
    return quotes


def fetch_live(provider, *, now, timeout=15):
    if provider == "runpod":
        query = "{ gpuTypes { id displayName memoryInGb secureCloud communityCloud securePrice communityPrice secureSpotPrice communitySpotPrice } }"
        return runpod_quotes(fetch_json(RUNPOD_URL, timeout=timeout, body={"query": query}), observed_at=now)
    if provider == "lambda":
        token = os.environ.get("LAMBDA_API_KEY")
        if not token:
            raise SourceError("Lambda requires LAMBDA_API_KEY")
        authorization = "Basic " + base64.b64encode((token + ":").encode()).decode("ascii")
        return lambda_quotes(fetch_json(LAMBDA_URL, timeout=timeout,
                             headers={"Authorization": authorization}), observed_at=now)
    raise ValueError("no built-in live adapter for provider")


def demo_quotes(now):
    """Synthetic cross-cloud coverage; never a published price/availability claim."""
    hour = timestamp(now).replace(minute=0, second=0, microsecond=0)
    providers = tuple(PROVIDERS)
    result = []
    for index, card in enumerate(GPU_CATALOG.values()):
        for offset in range(3):
            provider = providers[(index + offset) % len(providers)]
            base = Decimal(card.vram_gib) / 40 * (1 + Decimal(offset) / 4)
            for age in range(5):
                price = base * (Decimal("1.6") if age == 0 and index == 0 and offset == 0 else 1)
                result.append(normalize_quote({"provider": provider, "offer_id": "demo-" + card.gpu_id,
                    "gpu": card.gpu_id, "region": "demo-us-east", "market": "spot",
                    "vcpus": 16, "price": price, "price_unit": "gpu_hour",
                    "observed_at": (hour - timedelta(hours=age)).isoformat()},
                    status="mock", source="embedded://synthetic-gpu-v1"))
    return result


def write_output(path, payload):
    verify_integrity(payload)
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n"
    if len(raw) > MAX_BYTES:
        raise ValueError("export exceeds size limit")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name, suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def _read_file(path):
    with Path(path).open("rb") as handle:
        return strict_json(handle.read(MAX_BYTES + 1))


def run(*, offline=False, input_paths=(), history_paths=(), live=(), sources=(), strict=False,
        timeout=15, output=None, now=None, **options):
    _number(timeout, "timeout", maximum=60, positive=True)
    if offline and (input_paths or history_paths or live or sources):
        raise ValueError("offline cannot be combined with other price sources")
    clock = timestamp(now or utc_now()).isoformat()
    quotes = demo_quotes(clock) if offline else []
    failures = []
    for path in input_paths:
        quotes.extend(read_quotes(_read_file(path)))
    for path in history_paths:
        document = _read_file(path)
        if isinstance(document, dict) and "sha256_checksum" in document:
            verify_integrity(document)
            quotes.extend(normalize_quote(row["quote"], status=row["data_status"], source=row["source"])
                          for row in document["price_history"])
        else:
            quotes.extend(read_quotes(document))
    for provider in dict.fromkeys(live):
        if provider not in {"runpod", "lambda"}:
            raise ValueError("unsupported built-in live provider")
        try:
            quotes.extend(fetch_live(provider, now=clock, timeout=timeout))
        except (SourceError, ValueError, KeyError, TypeError):
            failures.append({"provider": provider, "reason": "source_unavailable_or_invalid"})
    for spec in dict.fromkeys(sources):
        provider, separator, url = spec.partition("=")
        if not separator or provider not in PROVIDERS:
            raise ValueError("source must be provider=https://host/path.json")
        safe_url(url)
        try:
            quotes.extend(read_quotes(fetch_json(url, timeout=timeout), provider=provider, status="live", source=url))
        except (SourceError, ValueError, KeyError, TypeError):
            failures.append({"provider": provider, "reason": "source_unavailable_or_invalid"})
    if strict and failures:
        raise SourceError("one or more price sources failed")
    if len(quotes) > MAX_ROWS:
        raise SourceError("too many price observations")
    payload = build_payload(quotes, now=clock, source_failures=failures, **options)
    if output is not None:
        write_output(output, payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument("--history", type=Path, action="append", default=[])
    parser.add_argument("--live", choices=("runpod", "lambda"), action="append", default=[])
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--strict", action="store_true", help="fail if any remote source fails")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--max-age-seconds", type=float, default=3600)
    parser.add_argument("--min-spread-pct", type=float, default=0)
    parser.add_argument("--baseline-hours", type=int, default=24)
    parser.add_argument("--min-history", type=int, default=3)
    parser.add_argument("--change-threshold-pct", type=float, default=20)
    parser.add_argument("--z-threshold", type=float, default=3)
    parser.add_argument("--pay-to", help="FeeGate recipient wallet; enables x402 v2 requirements")
    parser.add_argument("--resource-url", help="HTTPS URL served by the FeeGate gateway")
    parser.add_argument("--payment-amount", default="50000", help="atomic Base USDC units per query")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.verify:
            verify_integrity(_read_file(args.verify))
            print("GPU radar schema and SHA-256 verified")
            return 0
        payload = run(offline=args.offline, input_paths=args.input, history_paths=args.history,
            live=args.live, sources=args.source, strict=args.strict, timeout=args.timeout, output=args.output,
            max_age_seconds=args.max_age_seconds, min_spread_pct=args.min_spread_pct,
            baseline_hours=args.baseline_hours, min_history=args.min_history,
            change_threshold_pct=args.change_threshold_pct, z_threshold=args.z_threshold,
            pay_to=args.pay_to, resource_url=args.resource_url, payment_amount=args.payment_amount)
    except (OSError, ValueError, UnicodeError) as error:
        print("GPU radar failed: " + str(error), file=sys.stderr)
        return 1
    print(f"Wrote {payload['record_count']} quotes, {len(payload['spreads'])} spreads, "
          f"{sum(row['is_anomaly'] for row in payload['volatility'])} volatility anomalies to {args.output}; SHA-256 verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
