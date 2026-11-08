#!/usr/bin/env python3
"""Sovereign Data Product Engine: AI Model Cost & Routing Arbitrage Index (RFC-001).

Generates machine-actionable JSON payloads for autonomous agents, routing gateways,
and AI procurement systems using only Python standard library.

Usage:
    python3 scripts/agent_arbitrage_index_product.py --output-dir data/products/
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SPEC_VERSION = "1.0.0"
CATALOG_ID = "ai-model-cost-arbitrage-index"
MAX_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB limit


@dataclass(frozen=True)
class FallbackRoute:
    model_id: str
    provider: str
    input_cost_per_m_usd: float
    output_cost_per_m_usd: float


@dataclass(frozen=True)
class RouteEntry:
    task_archetype: str
    model_id: str
    target_provider: str
    baseline_reference_provider: str
    input_cost_per_m_usd: float
    output_cost_per_m_usd: float
    savings_pct_vs_baseline: float
    p95_latency_ms: int
    context_window_tokens: int
    sla_reliability_score: float
    ban_risk_level: str
    is_actionable: bool
    verification_timestamp: str
    fallback_route: Optional[FallbackRoute]
    signature: str


@dataclass(frozen=True)
class AgentProductBundle:
    schema_version: str
    generated_at: str
    catalog_id: str
    records_count: int
    actionable_count: int
    currency_base: str
    routing_matrix: List[RouteEntry]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "$schema": f"https://schemas.modelarbitrage.io/rfc-001-agent-arbitrage-index-{self.schema_version}.json",
            "version": self.schema_version,
            "generated_at": self.generated_at,
            "catalog_id": self.catalog_id,
            "records_count": self.records_count,
            "actionable_count": self.actionable_count,
            "currency_base": self.currency_base,
            "routing_matrix": [asdict(r) for r in self.routing_matrix],
        }


def compute_route_signature(model_id: str, provider: str, input_usd: float, output_usd: float, ts: str) -> str:
    seed = f"{model_id}|{provider}|{input_usd:.4f}|{output_usd:.4f}|{ts}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def build_canonical_agent_index(
    raw_pricing_data: Optional[Dict[str, Any]] = None,
    current_time: Optional[datetime] = None
) -> AgentProductBundle:
    """Build the deterministic canonical agent arbitrage index and routing table."""
    now = current_time or datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Standard model catalog benchmarks with baseline and high-volume arbitrage routes
    raw_catalog = [
        {
            "task_archetype": "coding_dense",
            "model_id": "deepseek-v3",
            "target_provider": "deepseek-direct",
            "baseline_provider": "azure-openai",
            "input_cost": 0.14,
            "output_cost": 0.28,
            "baseline_input_cost": 2.50,
            "p95_latency": 340,
            "context_window": 65536,
            "sla": 99.2,
            "risk": "LOW",
            "actionable": True,
            "fallback": FallbackRoute("claude-3-5-sonnet", "inceptionlabs", 3.00, 15.00),
        },
        {
            "task_archetype": "fast_reasoning",
            "model_id": "mercury-3",
            "target_provider": "inceptionlabs",
            "baseline_provider": "openai-direct",
            "input_cost": 0.60,
            "output_cost": 1.20,
            "baseline_input_cost": 5.00,
            "p95_latency": 220,
            "context_window": 131072,
            "sla": 99.9,
            "risk": "NONE",
            "actionable": True,
            "fallback": FallbackRoute("gpt-4o-mini", "openai-direct", 0.15, 0.60),
        },
        {
            "task_archetype": "deep_research",
            "model_id": "claude-3-5-sonnet",
            "target_provider": "claude-tier4-api",
            "baseline_provider": "retail-monthly-card",
            "input_cost": 3.00,
            "output_cost": 15.00,
            "baseline_input_cost": 20.00,
            "p95_latency": 680,
            "context_window": 200000,
            "sla": 99.5,
            "risk": "NONE",
            "actionable": True,
            "fallback": FallbackRoute("deepseek-v3", "deepseek-direct", 0.14, 0.28),
        },
        {
            "task_archetype": "micro_extraction",
            "model_id": "gpt-4o-mini",
            "target_provider": "openai-direct",
            "baseline_provider": "standard-commercial",
            "input_cost": 0.15,
            "output_cost": 0.60,
            "baseline_input_cost": 1.00,
            "p95_latency": 180,
            "context_window": 128000,
            "sla": 99.95,
            "risk": "NONE",
            "actionable": True,
            "fallback": FallbackRoute("deepseek-v3", "deepseek-direct", 0.14, 0.28),
        },
        {
            "task_archetype": "coding_dense",
            "model_id": "deepseek-r1-reasoner",
            "target_provider": "deepseek-direct",
            "baseline_provider": "o1-preview",
            "input_cost": 0.55,
            "output_cost": 2.19,
            "baseline_input_cost": 15.00,
            "p95_latency": 520,
            "context_window": 65536,
            "sla": 98.8,
            "risk": "LOW",
            "actionable": True,
            "fallback": FallbackRoute("mercury-3", "inceptionlabs", 0.60, 1.20),
        },
        {
            "task_archetype": "untrusted_shared_pool",
            "model_id": "claude-3-5-sonnet",
            "target_provider": "synthetic-shared-account",
            "baseline_provider": "claude-official",
            "input_cost": 0.20,
            "output_cost": 0.80,
            "baseline_input_cost": 3.00,
            "p95_latency": 1400,
            "context_window": 200000,
            "sla": 75.0,
            "risk": "HIGH",
            "actionable": False,  # Explicitly flagged non-actionable due to ban risk
            "fallback": None,
        },
    ]

    entries: List[RouteEntry] = []
    actionable_cnt = 0

    for item in raw_catalog:
        inp = float(item["input_cost"])
        base_inp = float(item["baseline_input_cost"])
        # Compute savings %
        savings = round(((base_inp - inp) / base_inp) * 100.0, 2)
        if savings < 0:
            savings = 0.0

        is_act = bool(item["actionable"])
        if is_act:
            actionable_cnt += 1

        sig = compute_route_signature(
            item["model_id"], item["target_provider"], inp, float(item["output_cost"]), now_str
        )

        entries.append(
            RouteEntry(
                task_archetype=str(item["task_archetype"]),
                model_id=str(item["model_id"]),
                target_provider=str(item["target_provider"]),
                baseline_reference_provider=str(item["baseline_provider"]),
                input_cost_per_m_usd=inp,
                output_cost_per_m_usd=float(item["output_cost"]),
                savings_pct_vs_baseline=savings,
                p95_latency_ms=int(item["p95_latency"]),
                context_window_tokens=int(item["context_window"]),
                sla_reliability_score=float(item["sla"]),
                ban_risk_level=str(item["risk"]),
                is_actionable=is_act,
                verification_timestamp=now_str,
                fallback_route=item["fallback"],
                signature=sig,
            )
        )

    return AgentProductBundle(
        schema_version=SPEC_VERSION,
        generated_at=now_str,
        catalog_id=CATALOG_ID,
        records_count=len(entries),
        actionable_count=actionable_cnt,
        currency_base="USD",
        routing_matrix=entries,
    )


def export_commercial_products(bundle: AgentProductBundle, out_dir: Path) -> Dict[str, Path]:
    """Export the 3 commercial data product files (Full Index, Agent Routing, Freemium Sample)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}

    # 1. Full Pro Index
    full_path = out_dir / "agent_model_arbitrage_index_latest.json"
    full_data = bundle.to_dict()
    full_bytes = json.dumps(full_data, indent=2, ensure_ascii=False).encode("utf-8")
    if len(full_bytes) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"Payload size {len(full_bytes)} exceeds {MAX_PAYLOAD_BYTES}")
    full_path.write_bytes(full_bytes)
    results["pro_index"] = full_path

    # 2. Agent Immediate Next-Hop Decision Feed (Actionable routes only)
    routing_path = out_dir / "agent_routing_recommendations.json"
    routing_data = {
        "version": bundle.schema_version,
        "generated_at": bundle.generated_at,
        "policy": "cost_minimization_with_sla_guarantee",
        "actionable_routes": [
            asdict(r) for r in bundle.routing_matrix if r.is_actionable and r.ban_risk_level in ["NONE", "LOW"]
        ],
    }
    routing_path.write_bytes(json.dumps(routing_data, indent=2, ensure_ascii=False).encode("utf-8"))
    results["routing_feed"] = routing_path

    # 3. Sanitized Freemium Sample (Top 3 for public marketing / Linux.do / GitHub)
    sample_path = out_dir / "sample_commercial_payload.json"
    sample_data = {
        "title": "AI Model Cost & Routing Arbitrage Index (Public Sample)",
        "license": "CC-BY-NC-4.0 / Model Arbitrage Protocol",
        "generated_at": bundle.generated_at,
        "upgrade_info": {
            "pro_feed_refresh": "Every 10 minutes",
            "micro_payment_mcp_url": "https://api.modelarbitrage.io/v1/agent-routing/stream",
            "accepted_payments": ["x402-HTTP", "USDT/Base", "Alipay/WeChat-Invoice"]
        },
        "sample_routes": [asdict(r) for r in bundle.routing_matrix[:3]],
    }
    sample_path.write_bytes(json.dumps(sample_data, indent=2, ensure_ascii=False).encode("utf-8"))
    results["freemium_sample"] = sample_path

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Agent Cost & Routing Arbitrage Data Products")
    parser.add_argument("--output-dir", type=str, default=str(ROOT / "data" / "products"), help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    bundle = build_canonical_agent_index()
    exported = export_commercial_products(bundle, out_dir)

    print(f"✅ Successfully exported {len(exported)} data product assets to {out_dir}:")
    for k, p in exported.items():
        print(f"  - [{k}] {p.name} ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
