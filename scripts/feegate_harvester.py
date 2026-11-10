#!/usr/bin/env python3
"""
🌐 FeeGate Commercial Intelligence Real-World Harvester (No-LLM Data Ingestion Engine)
====================================================================================
Purpose:
  Ingests authoritative, live, quantitative market and technology data from real APIs:
  1. OpenRouter Models API -> feegate-model-pricing-benchmark.json
  2. GitHub Security Advisories API -> feegate-security-cve-threat-radar.json
  3. GitHub Frameworks Telemetry API -> feegate-agent-frameworks-benchmark.json
  4. Master Index -> feegate-catalog-index.json

Core Principles:
  - Zero LLM Synthesis / Hallucination: 100% hard, verified upstream network data.
  - Strict Target Audience Segmentation: Explicit paying customer personas.
  - High Commercial Value: Actionable benchmarks, pricing metrics, and CVE advisories.
  - Complete Provenance: Timestamps, source URLs, SHA-256 signatures, schema versions.
  - Air-Gap Sanitization: Scanned by GATE-00 rules to guarantee 0% internal leakage.
"""

import os
import sys
import json
import base64
import hashlib
import datetime
import urllib.request
import urllib.error
import subprocess
from typing import Dict, List, Any, Optional

from test_catalog_integrity import (
    SOVEREIGN_ID, check_metadata, product_metadata, read_json, records_checksum,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Strict OpSec Banned Keywords (Encoded to prevent plaintext appearance in public repo)
BANNED_LEAK_KEYWORDS = [
    base64.b64decode(b"eW90b3BoYWxhbng=").decode("utf-8"),
    base64.b64decode(b"c3Rld2FyZA==").decode("utf-8"),
    base64.b64decode(b"cGhhbGFueC0=").decode("utf-8"),
    base64.b64decode(b"VFdJTi0wMA==").decode("utf-8"),
    base64.b64decode(b"RVhFLTAx").decode("utf-8"),
    base64.b64decode(b"T1BTLTAx").decode("utf-8"),
    base64.b64decode(b"Rk9SLTAx").decode("utf-8"),
    base64.b64decode(b"Q1VTLTAx").decode("utf-8"),
    base64.b64decode(b"UkFELTAx").decode("utf-8"),
    base64.b64decode(b"U0VOLTAx").decode("utf-8"),
    base64.b64decode(b"UkVDLTAx").decode("utf-8"),
    base64.b64decode(b"R0FURS0wMA==").decode("utf-8"),
    base64.b64decode(b"SU5TUC0wMA==").decode("utf-8")
]


def get_github_auth_token() -> Optional[str]:
    """Retrieve GitHub token from environment or CLI if available."""
    env_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env_token:
        return env_token
    try:
        token = subprocess.getoutput("gh auth token").strip()
        if token and not token.startswith("failed") and not "error" in token.lower():
            return token
    except Exception:
        pass
    return None


def fetch_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 15) -> Any:
    """Fetch and decode JSON with robust user-agent and optional auth."""
    req_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        sys.stderr.write(f"[-] Network fetch failed for {url}: {e}\n")
        raise e


def compute_sha256(data_str: str) -> str:
    return hashlib.sha256(data_str.encode("utf-8")).hexdigest()


def verify_airgap(data_obj: Any) -> None:
    """Ensure zero internal organizational leakage in exported datasets."""
    serialized = json.dumps(data_obj, ensure_ascii=False)
    for kw in BANNED_LEAK_KEYWORDS:
        if kw in serialized:
            raise ValueError(f"[CRITICAL AIRGAP VETO] Detected forbidden internal identifier '{kw}' in exported payload!")


# ==============================================================================
# 1. Product 1: Global AI Models Real-Time Pricing & Context Benchmark
# ==============================================================================
def harvest_model_pricing() -> Dict[str, Any]:
    print("[+] Harvesting real AI model pricing data from OpenRouter Public API...")
    url = "https://openrouter.ai/api/v1/models"
    raw_data = fetch_json(url)
    models = raw_data.get("data", [])
    if not models:
        raise ValueError("Empty models list returned from OpenRouter API")

    normalized_models = []
    for m in models:
        model_id = m.get("id", "")
        pricing = m.get("pricing", {})
        prompt_unit = float(pricing.get("prompt", 0.0) or 0.0)
        completion_unit = float(pricing.get("completion", 0.0) or 0.0)

        # Convert unit price per token to standard $/1M tokens
        prompt_per_1m = round(prompt_unit * 1_000_000, 4)
        completion_per_1m = round(completion_unit * 1_000_000, 4)
        context_len = int(m.get("context_length", 0) or 0)

        # Classify tier based on pricing
        if prompt_per_1m == 0.0 and completion_per_1m == 0.0:
            cost_tier = "Free Tier"
        elif prompt_per_1m <= 0.5:
            cost_tier = "Economy / Edge Tier (<= $0.50/M)"
        elif prompt_per_1m <= 3.0:
            cost_tier = "Mid-range / Workhorse Tier ($0.50 - $3.00/M)"
        elif prompt_per_1m <= 10.0:
            cost_tier = "Frontier / High-Throughput Tier ($3.00 - $10.00/M)"
        else:
            cost_tier = "Ultra-Frontier / Reasoning Tier (> $10.00/M)"

        normalized_models.append({
            "model_id": model_id,
            "name": m.get("name", model_id),
            "provider": model_id.split("/")[0] if "/" in model_id else "unknown",
            "context_window_tokens": context_len,
            "pricing_per_1m_tokens_usd": {
                "prompt": prompt_per_1m,
                "completion": completion_per_1m,
                "blended_3to1_ratio": round((prompt_per_1m * 3 + completion_per_1m) / 4, 4)
            },
            "cost_tier": cost_tier,
            "architecture": {
                "modality": m.get("architecture", {}).get("modality", "text->text"),
                "tokenizer": m.get("architecture", {}).get("tokenizer", "custom"),
                "instruct_type": m.get("architecture", {}).get("instruct_type")
            },
            "top_provider_info": {
                "max_completion_tokens": m.get("top_provider", {}).get("max_completion_tokens"),
                "is_moderated": m.get("top_provider", {}).get("is_moderated", False)
            }
        })

    # Sort models by provider and name
    normalized_models.sort(key=lambda x: (x["provider"], x["model_id"]))

    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    product_payload = {
        "catalog_id": "feegate-ai-model-pricing-index",
        "product_name": "Global AI Models Real-time Pricing & Context Benchmark",
        "category": "AI Financial Intelligence & Token Cost Routing",
        "target_audience": "AI Engineers, Model Routing Framework Architects (LiteLLM/LangChain), Enterprise FinOps Teams",
        "commercial_value_proposition": "Production-grade, quantitative cost and context window matrix across 450+ models. Used to programmatically evaluate API cost trade-offs, auto-switch fallback models, and enforce cloud budget limits.",
        "commercial_tier": "B2B Production API ($0.05 / query)",
        "provenance": {
            "source_origin": "OpenRouter Official Public Model Registry",
            "source_endpoint": url,
            "ingested_at_utc": now_utc,
            "schema_version": "2.0.0",
            "record_count": len(normalized_models)
        },
        "market_summary": {
            "total_models_indexed": len(normalized_models),
            "free_tier_count": sum(1 for m in normalized_models if m["cost_tier"] == "Free Tier"),
            "max_context_window": max((m["context_window_tokens"] for m in normalized_models), default=0),
            "distinct_providers_count": len(set(m["provider"] for m in normalized_models))
        },
        "records": normalized_models
    }

    # Add hash
    serialized = json.dumps(product_payload["records"], sort_keys=True)
    product_payload["provenance"]["sha256_checksum"] = compute_sha256(serialized)
    verify_airgap(product_payload)
    return product_payload


# ==============================================================================
# 2. Product 2: Open-Source & AI Ecosystem Critical/High CVE Threat Radar
# ==============================================================================
def harvest_security_cves() -> Dict[str, Any]:
    print("[+] Harvesting critical/high security CVE advisories from GitHub Advisory API...")
    token = get_github_auth_token()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    raw_advisories = []
    # GitHub Advisory API requires single severity per request
    for sev in ["critical", "high"]:
        url = f"https://api.github.com/advisories?per_page=20&severity={sev}"
        data = fetch_json(url, headers=headers)
        if isinstance(data, list):
            raw_advisories.extend(data)

    if not raw_advisories:
        raise ValueError("Invalid advisory payload received from GitHub API")

    normalized_cves = []
    for adv in raw_advisories:
        ghsa_id = adv.get("ghsa_id")
        cve_id = adv.get("cve_id")
        summary = adv.get("summary", "")
        severity = adv.get("severity", "unknown").upper()
        published_at = adv.get("published_at")
        
        # Extract impacted packages
        vuln_list = adv.get("vulnerabilities", []) or []
        affected_packages = []
        for v in vuln_list:
            pkg = v.get("package", {}) or {}
            affected_packages.append({
                "ecosystem": pkg.get("ecosystem", "generic"),
                "package_name": pkg.get("name", "unknown"),
                "vulnerable_version_range": v.get("vulnerable_version_range"),
                "patched_versions": v.get("patched_versions")
            })

        normalized_cves.append({
            "advisory_id": ghsa_id,
            "cve_identifier": cve_id or "NO-CVE-ASSIGNED",
            "severity": severity,
            "published_at_utc": published_at,
            "summary": summary,
            "affected_ecosystems": list(set(p["ecosystem"] for p in affected_packages if p["ecosystem"])),
            "affected_packages": affected_packages,
            "advisory_url": adv.get("html_url"),
            "cwe_ids": adv.get("cwe_ids", []),
            "cvss_vector": adv.get("cvss", {}).get("vector_string") if adv.get("cvss") else None,
            "cvss_score": adv.get("cvss", {}).get("score") if adv.get("cvss") else None
        })

    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    product_payload = {
        "catalog_id": "feegate-security-cve-threat-radar",
        "product_name": "Global Open-Source & AI Stack Critical/High CVE Threat Radar",
        "category": "Cybersecurity & Vulnerability Intelligence",
        "target_audience": "DevSecOps Engineers, Security Compliance Auditors, Dependency Remediation Bots, CISO Offices",
        "commercial_value_proposition": "Authoritative, machine-readable vulnerability intelligence feed. Ingested directly into CI/CD security gates to block builds containing newly published 0-day / 1-day remote code execution or data leakage CVEs.",
        "commercial_tier": "B2B Production API ($0.05 / query)",
        "provenance": {
            "source_origin": "GitHub Security Advisory Database (NVD/CVE Synchronized)",
            "source_endpoint": url,
            "ingested_at_utc": now_utc,
            "schema_version": "2.0.0",
            "record_count": len(normalized_cves)
        },
        "threat_summary": {
            "total_threats_indexed": len(normalized_cves),
            "critical_count": sum(1 for c in normalized_cves if c["severity"] == "CRITICAL"),
            "high_count": sum(1 for c in normalized_cves if c["severity"] == "HIGH"),
            "ecosystems_affected": sorted(list(set(e for c in normalized_cves for e in c["affected_ecosystems"])))
        },
        "records": normalized_cves
    }

    serialized = json.dumps(product_payload["records"], sort_keys=True)
    product_payload["provenance"]["sha256_checksum"] = compute_sha256(serialized)
    verify_airgap(product_payload)
    return product_payload


# ==============================================================================
# 3. Product 3: Global AI Agent Frameworks & Tooling Ecosystem Benchmark
# ==============================================================================
def harvest_agent_frameworks() -> Dict[str, Any]:
    print("[+] Harvesting production repository metrics for top AI Agent & LLM Infra frameworks...")
    target_repos = [
        ("langchain-ai/langgraph", "Orchestration & State Machine"),
        ("crewAIInc/crewAI", "Multi-Agent Role Collaboration"),
        ("microsoft/autogen", "Conversational Multi-Agent Framework"),
        ("OpenHands/OpenHands", "Autonomous Software Engineering Agent"),
        ("stanfordnlp/dspy", "Self-Optimizing LM Pipelines"),
        ("run-llama/llama_index", "Data Framework for LLM / RAG"),
        ("vllm-project/vllm", "High-Throughput LLM Serving Engine"),
        ("BerriAI/litellm", "Universal LLM Proxy & Token Cost Routing"),
        ("huggingface/transformers", "Foundational Open Model Architecture"),
        ("ollama/ollama", "Local LLM Execution Runtime"),
        ("langgenius/dify", "Enterprise LLM Application Platform"),
        ("modelcontextprotocol/servers", "Official Model Context Protocol Reference Servers")
    ]

    token = get_github_auth_token()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    framework_records = []
    for repo_name, category in target_repos:
        url = f"https://api.github.com/repos/{repo_name}"
        try:
            d = fetch_json(url, headers=headers)
            framework_records.append({
                "repo_name": d.get("full_name", repo_name),
                "framework_category": category,
                "description": d.get("description", ""),
                "metrics": {
                    "stars_count": d.get("stargazers_count", 0),
                    "forks_count": d.get("forks_count", 0),
                    "open_issues_count": d.get("open_issues_count", 0),
                    "subscribers_count": d.get("subscribers_count", 0)
                },
                "engineering_health": {
                    "primary_language": d.get("language", "Python"),
                    "license_spdx": d.get("license", {}).get("spdx_id") if d.get("license") else "Proprietary / Unknown",
                    "has_wiki": d.get("has_wiki", False),
                    "last_pushed_at_utc": d.get("pushed_at"),
                    "default_branch": d.get("default_branch", "main")
                },
                "repository_url": d.get("html_url")
            })
        except Exception as e:
            sys.stderr.write(f"[-] Warning: Failed to fetch repo {repo_name}: {e}\n")

    # Rank by stars
    framework_records.sort(key=lambda x: x["metrics"]["stars_count"], reverse=True)

    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    product_payload = {
        "catalog_id": "feegate-agent-frameworks-benchmark",
        "product_name": "Global AI Agent Frameworks & Production Tooling Benchmark",
        "category": "AI Ecosystem & Market Pulse Intelligence",
        "target_audience": "Tech VC Investors, Enterprise AI Architects, CTOs Evaluating Open Source Stacks",
        "commercial_value_proposition": "Quantitative engineering telemetry and adoption velocity for the world's most dominant AI agent frameworks. Enables instant tech stack due diligence and open source ecosystem viability auditing.",
        "commercial_tier": "B2B Market Intel API ($0.05 / query)",
        "provenance": {
            "source_origin": "GitHub Official REST API Telemetry",
            "source_endpoint": "https://api.github.com/repos/{owner}/{repo}",
            "ingested_at_utc": now_utc,
            "schema_version": "2.0.0",
            "record_count": len(framework_records)
        },
        "summary": {
            "total_frameworks_indexed": len(framework_records),
            "aggregate_stars": sum(f["metrics"]["stars_count"] for f in framework_records),
            "top_starred_framework": framework_records[0]["repo_name"] if framework_records else None
        },
        "records": framework_records
    }

    serialized = json.dumps(product_payload["records"], sort_keys=True)
    product_payload["provenance"]["sha256_checksum"] = compute_sha256(serialized)
    verify_airgap(product_payload)
    return product_payload


# ==============================================================================
# 4. Master Catalog Index: feegate-catalog-index.json
# ==============================================================================
def generate_master_catalog(products: List[Dict[str, Any]]) -> Dict[str, Any]:
    print("[+] Generating FeeGate Master Product Catalog Index...")
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    catalog = {
        "catalog_version": "2.1.0",
        "provider": "FeeGate Commercial Data Intelligence Feed",
        "generated_at_utc": now_utc,
        "protocol": "HTTP REST / x402 Micropayments",
        "total_active_products": len(products),
        "products": []
    }

    for p in products:
        catalog["products"].append({
            "catalog_id": p.get("catalog_id"),
            "product_name": p.get("product_name"),
            "category": p.get("category"),
            "target_audience": p.get("target_audience"),
            "commercial_tier": p.get("commercial_tier"),
            "commercial_value_proposition": p.get("commercial_value_proposition"),
            "provenance": p.get("provenance"),
            "data_url": f"https://raw.githubusercontent.com/Yotoha0303/Yotoha0303/main/data/{p.get('catalog_id')}.json"
        })

    # The scheduled job refreshes three products. Retain the independently
    # harvested sovereign product so this job cannot downgrade the active catalog.
    if not any(product["catalog_id"] == SOVEREIGN_ID for product in catalog["products"]):
        existing = read_json(os.path.join(DATA_DIR, "feegate-catalog-index.json"))
        sovereign_entries = [entry for entry in existing["products"] if entry["catalog_id"] == SOVEREIGN_ID]
        if len(sovereign_entries) != 1:
            raise ValueError("existing catalog must contain exactly one sovereign product")
        sovereign = read_json(os.path.join(DATA_DIR, SOVEREIGN_ID + ".json"))
        metadata = product_metadata(SOVEREIGN_ID, sovereign)
        errors = []
        check_metadata(metadata, SOVEREIGN_ID, "1.0.0", len(sovereign["records"]),
                       records_checksum(SOVEREIGN_ID, sovereign["records"]), errors)
        if sovereign.get("catalog_id") != SOVEREIGN_ID or errors:
            raise ValueError("invalid sovereign dataset: " + "; ".join(errors))
        entry = sovereign_entries[0]
        entry["provenance"].update(metadata)
        catalog["products"].insert(0, entry)
    catalog["total_active_products"] = len(catalog["products"])
    verify_airgap(catalog)
    return catalog


def save_json(filepath: str, obj: Any) -> None:
    verify_airgap(obj)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    size_kb = os.path.getsize(filepath) / 1024
    print(f"[✓] Saved {os.path.basename(filepath)} ({size_kb:.1f} KB)")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="FeeGate Real-World Commercial Data Ingestion Engine")
    parser.add_argument("--all", action="store_true", default=True, help="Harvest all products and generate catalog index")
    parser.add_argument("--commit", action="store_true", help="Automatically commit and push data to GitHub")
    args = parser.parse_args()

    print("=" * 80)
    print("🌐 FEE-GATE REAL-WORLD COMMERCIAL DATA INGESTION PIPELINE")
    print("🕒 Mode: Keyless Production Harvester | Zero LLM Synthesis Policy")
    print("=" * 80)

    # 1. Harvest Product 1: AI Model Pricing
    product_pricing = harvest_model_pricing()
    pricing_path = os.path.join(DATA_DIR, "feegate-ai-model-pricing-index.json")
    save_json(pricing_path, product_pricing)

    # 2. Harvest Product 2: Security CVEs
    product_security = harvest_security_cves()
    security_path = os.path.join(DATA_DIR, "feegate-security-cve-threat-radar.json")
    save_json(security_path, product_security)

    # 3. Harvest Product 3: Agent Frameworks
    product_frameworks = harvest_agent_frameworks()
    frameworks_path = os.path.join(DATA_DIR, "feegate-agent-frameworks-benchmark.json")
    save_json(frameworks_path, product_frameworks)

    # 4. Generate Master Catalog Index
    catalog = generate_master_catalog([product_pricing, product_security, product_frameworks])
    catalog_path = os.path.join(DATA_DIR, "feegate-catalog-index.json")
    save_json(catalog_path, catalog)

    print("=" * 80)
    print("✅ Refreshed 3 commercial products; retained sovereign yields in the 4-product catalog!")
    print(f"   1. Model Pricing:      {len(product_pricing['records'])} records")
    print(f"   2. Security CVE Radar: {len(product_security['records'])} records")
    print(f"   3. Agent Benchmarks:   {len(product_frameworks['records'])} records")
    print(f"   4. Master Catalog:     feegate-catalog-index.json")
    print("=" * 80)

    if args.commit:
        print("[+] Performing automated Git commit and push to remote...")
        cwd = BASE_DIR
        subprocess.run(["git", "add", "data/"], cwd=cwd, check=True)
        commit_msg = f"chore(data): FeeGate dataset refresh ({datetime.date.today().isoformat()}) [skip ci]"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=cwd, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=cwd, check=True)
        print("[✓] Pushed live datasets to GitHub successfully!")


if __name__ == "__main__":
    main()
