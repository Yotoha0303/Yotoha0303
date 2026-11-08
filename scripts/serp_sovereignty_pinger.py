#!/usr/bin/env python3
"""SERP Sovereignty Arbiter & Entity Disambiguation Engine (SERP-01).

Standard library only. Provides Schema.org Person JSON-LD validation, IndexNow
payload generation, Baidu sitemap XML generation, and SERP collision auditing
to protect the canonical identity of 'Yotoha' against confusing overlaps with
the Japanese musical artist 'Otoha' (音羽 / をとは).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENTITY_PATH = ROOT / "docs" / "yotoha_entity.jsonld"
DEFAULT_OUTPUT_DIR = ROOT / "data"

CANONICAL_TARGET = "yotoha"
INTRUDER_KEYWORDS = (
    "otoha",
    "wotoha",
    "音羽",
    "をとは",
    "singer",
    "歌手",
    "music",
    "musician",
    "album",
    "single",
    "utaite",
    "歌い手",
    "j-pop",
    "jpop",
)

OFFICIAL_URL_PREFIXES = (
    "https://github.com/Yotoha0303",
    "https://v2ex.com/t/",
    "https://eleduck.com/",
    "https://knowtrace-workflow.vercel.app",
    "https://go-order-management-system.vercel.app",
)


class EntityClassification(str, Enum):
    SOVEREIGN = "SOVEREIGN"
    INTRUDER = "INTRUDER"
    NEUTRAL = "NEUTRAL"


class SovereigntyStatus(str, Enum):
    SOVEREIGN_DOMINANT = "SOVEREIGN_DOMINANT"  # Sovereignty >= 80% & intruder rank > 10 or absent
    CONTESTED = "CONTESTED"                    # Intruder in top 10 (rank 3..10)
    CRITICAL_INTRUSION = "CRITICAL_INTRUSION"  # Intruder in rank 1..2


class ValidationError(ValueError):
    """Raised when Schema.org Person JSON-LD does not satisfy canonical standards."""


@dataclass
class SearchResult:
    rank: int
    title: str
    url: str
    snippet: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchResult:
        return cls(
            rank=int(data.get("rank", 0)),
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            snippet=str(data.get("snippet", "")),
        )


@dataclass
class ClassifiedResult:
    rank: int
    title: str
    url: str
    classification: EntityClassification
    matched_clues: list[str]


@dataclass
class AuditReport:
    target_entity: str
    total_audited: int
    sovereign_count: int
    intruder_count: int
    neutral_count: int
    sovereignty_ratio: float
    highest_intruder_rank: Optional[int]
    status: SovereigntyStatus
    recommendations: list[str]
    classified_results: list[ClassifiedResult]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        for item in data["classified_results"]:
            item["classification"] = item["classification"]
        return data


def load_and_validate_entity(path: Path | str) -> dict[str, Any]:
    """Load and strictly validate the Schema.org Person JSON-LD entity definition."""
    target_path = Path(path)
    if not target_path.exists():
        raise FileNotFoundError(f"Entity schema file not found at: {target_path}")

    with open(target_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValidationError("Schema must be a JSON object dictionary.")

    context = data.get("@context")
    if context not in ("https://schema.org", "http://schema.org"):
        raise ValidationError(f"Invalid @context: {context}")

    entity_type = data.get("@type")
    if entity_type != "Person":
        raise ValidationError(f"Expected @type 'Person', found '{entity_type}'")

    name = data.get("name")
    if name != "Yotoha":
        raise ValidationError(f"Expected canonical name 'Yotoha', found '{name}'")

    disambiguation = data.get("disambiguatingDescription", "")
    if not disambiguation:
        raise ValidationError("Missing 'disambiguatingDescription' field in entity record.")

    disambiguation_lower = disambiguation.lower()
    if "otoha" not in disambiguation_lower or "音羽" not in disambiguation:
        raise ValidationError(
            "disambiguatingDescription must explicitly mention the confusing entity 'Otoha (音羽)'."
        )

    negation_keywords = ("not affiliated", "distinct from", "not to be confused", "strictly distinct")
    if not any(k in disambiguation_lower for k in negation_keywords):
        raise ValidationError(
            f"disambiguatingDescription must state explicit non-affiliation (found: {disambiguation})"
        )

    # Check notAffiliatedWith block
    not_affiliated = data.get("notAffiliatedWith", [])
    if not isinstance(not_affiliated, list) or len(not_affiliated) == 0:
        raise ValidationError("Must define at least one entry in 'notAffiliatedWith'.")

    return data


def generate_indexnow_payload(
    host: str,
    key: str,
    url_list: Iterable[str],
    key_location: Optional[str] = None,
) -> dict[str, Any]:
    """Generate RFC-compliant IndexNow protocol payload for fast Bing/Yandex submission."""
    urls = [str(u).strip() for u in url_list if str(u).strip()]
    if not urls:
        raise ValueError("URL list cannot be empty for IndexNow submission.")
    if not key or len(key) < 8:
        raise ValueError("IndexNow key must be at least 8 characters.")

    payload: dict[str, Any] = {
        "host": host,
        "key": key,
        "urlList": urls,
    }
    if key_location:
        payload["keyLocation"] = key_location
    return payload


def generate_baidu_sitemap_xml(urls: Iterable[str]) -> str:
    """Generate standard XML sitemap block for Baidu Webmaster submission."""
    clean_urls = [str(u).strip() for u in urls if str(u).strip()]
    if not clean_urls:
        raise ValueError("URL list cannot be empty for sitemap generation.")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in clean_urls:
        lines.append("  <url>")
        lines.append(f"    <loc>{url}</loc>")
        lines.append("    <changefreq>daily</changefreq>")
        lines.append("    <priority>1.0</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines)


def classify_search_result(
    result: SearchResult,
    target_entity: str = CANONICAL_TARGET,
    intruder_keywords: Tuple[str, ...] = INTRUDER_KEYWORDS,
) -> Tuple[EntityClassification, list[str]]:
    """Classify a search result item into SOVEREIGN, INTRUDER, or NEUTRAL."""
    combined_text = f"{result.title} {result.snippet}".lower()
    matched_clues: list[str] = []

    # 1. Check if URL is official
    for prefix in OFFICIAL_URL_PREFIXES:
        if result.url.startswith(prefix):
            matched_clues.append(f"official_prefix:{prefix}")
            return EntityClassification.SOVEREIGN, matched_clues

    # 2. Check for target entity signals
    sovereign_signals = ("yotoha", "yotoha0303", "ai collaboration framework", "knowtrace")
    has_sovereign = any(sig in combined_text or sig in result.url.lower() for sig in sovereign_signals)

    # 3. Check for intruder signals
    intruder_hits = [k for k in intruder_keywords if k in combined_text or k in result.url.lower()]

    if intruder_hits:
        matched_clues.extend(intruder_hits)
        # If it explicitly talks about music, song, artist, album, utaite -> definitely intruder
        return EntityClassification.INTRUDER, matched_clues

    if has_sovereign:
        matched_clues.append("target_entity_match")
        return EntityClassification.SOVEREIGN, matched_clues

    return EntityClassification.NEUTRAL, matched_clues


def audit_serp_data(
    results: list[SearchResult | dict[str, Any]],
    target_entity: str = CANONICAL_TARGET,
) -> AuditReport:
    """Audit a list of SERP rankings and compute sovereignty ratios and intruder suppression."""
    if not results:
        return AuditReport(
            target_entity=target_entity,
            total_audited=0,
            sovereign_count=0,
            intruder_count=0,
            neutral_count=0,
            sovereignty_ratio=0.0,
            highest_intruder_rank=None,
            status=SovereigntyStatus.CONTESTED,
            recommendations=["No SERP results provided to audit."],
            classified_results=[],
        )

    classified: list[ClassifiedResult] = []
    sovereign_cnt = 0
    intruder_cnt = 0
    neutral_cnt = 0
    highest_intruder_rank: Optional[int] = None

    for item in results:
        res = item if isinstance(item, SearchResult) else SearchResult.from_dict(item)
        classification, clues = classify_search_result(res, target_entity=target_entity)

        if classification == EntityClassification.SOVEREIGN:
            sovereign_cnt += 1
        elif classification == EntityClassification.INTRUDER:
            intruder_cnt += 1
            if highest_intruder_rank is None or res.rank < highest_intruder_rank:
                highest_intruder_rank = res.rank
        else:
            neutral_cnt += 1

        classified.append(
            ClassifiedResult(
                rank=res.rank,
                title=res.title,
                url=res.url,
                classification=classification,
                matched_clues=clues,
            )
        )

    total = len(results)
    sovereignty_ratio = round((sovereign_cnt / total) * 100, 2) if total > 0 else 0.0

    # Determine status & recommendations
    recommendations: list[str] = []
    if highest_intruder_rank is not None and highest_intruder_rank in (1, 2):
        status = SovereigntyStatus.CRITICAL_INTRUSION
        recommendations.append(
            f"CRITICAL: Intruder entity found at Rank {highest_intruder_rank}! Immediately submit Schema.org JSON-LD and request Baidu/Google re-indexing."
        )
        recommendations.append(
            "Deploy high-PageRank backlinks from GitHub, V2EX, and Eleduck containing exact title 'Yotoha'."
        )
    elif highest_intruder_rank is not None and highest_intruder_rank <= 10:
        status = SovereigntyStatus.CONTESTED
        recommendations.append(
            f"WARNING: Intruder present on page 1 (Rank {highest_intruder_rank}). Increase content freshness and IndexNow pings."
        )
    else:
        status = SovereigntyStatus.SOVEREIGN_DOMINANT
        recommendations.append(
            "SOVEREIGN: Target entity dominates SERP. Otoha suppressed beyond top 10 or absent. Maintain automated pings."
        )

    return AuditReport(
        target_entity=target_entity,
        total_audited=total,
        sovereign_count=sovereign_cnt,
        intruder_count=intruder_cnt,
        neutral_count=neutral_cnt,
        sovereignty_ratio=sovereignty_ratio,
        highest_intruder_rank=highest_intruder_rank,
        status=status,
        recommendations=recommendations,
        classified_results=classified,
    )


def format_markdown_report(report: AuditReport) -> str:
    """Format AuditReport into human-readable Markdown."""
    lines = [
        f"# 🛡️ SERP Sovereignty Audit Report | 搜索主权巡检总账",
        "",
        f"- **Target Entity (目标实体)**: `{report.target_entity}`",
        f"- **Sovereignty Status (主权状态)**: `{report.status.value}`",
        f"- **Sovereignty Ratio (主权占领率)**: `{report.sovereignty_ratio}%`",
        f"- **Total Audited (审计总条数)**: `{report.total_audited}`",
        f"- **Sovereign Assets (官方正统资产数)**: `{report.sovereign_count}`",
        f"- **Intruder Collisions (音羽歌手混淆数)**: `{report.intruder_count}`",
        f"- **Highest Intruder Rank (混淆最高排位)**: `{report.highest_intruder_rank or 'None (已彻底压制或未出现)'}`",
        "",
        "## 📋 Recommendations | 行动建议",
    ]
    for rec in report.recommendations:
        lines.append(f"- {rec}")

    lines.append("")
    lines.append("## 🔍 Classified SERP Items | 结果分类明细")
    lines.append("| Rank | Status | Title | URL | Clues |")
    lines.append("| :--- | :--- | :--- | :--- | :--- |")
    for r in report.classified_results:
        clues_str = ", ".join(r.matched_clues) if r.matched_clues else "none"
        lines.append(f"| {r.rank} | {r.classification.value} | {r.title} | {r.url} | {clues_str} |")

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="SERP Sovereignty Arbiter & Entity Disambiguation Engine."
    )
    parser.add_argument(
        "--validate-entity",
        action="store_true",
        help="Validate the Schema.org Person JSON-LD entity definition.",
    )
    parser.add_argument(
        "--entity-path",
        type=Path,
        default=DEFAULT_ENTITY_PATH,
        help="Path to the JSON-LD entity schema file.",
    )
    parser.add_argument(
        "--generate-indexnow",
        action="store_true",
        help="Generate an IndexNow payload for Bing/Yandex search engines.",
    )
    parser.add_argument(
        "--generate-sitemap",
        action="store_true",
        help="Generate a Baidu-compliant XML sitemap snippet.",
    )
    parser.add_argument(
        "--audit-sample",
        action="store_true",
        help="Run an audit simulation against a sample SERP result dataset.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write output artifacts.",
    )

    args = parser.parse_args(argv)

    if not any([args.validate_entity, args.generate_indexnow, args.generate_sitemap, args.audit_sample]):
        # Default action: run full suite
        args.validate_entity = True
        args.generate_indexnow = True
        args.generate_sitemap = True
        args.audit_sample = True

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.validate_entity:
        entity = load_and_validate_entity(args.entity_path)
        print(f"✅ Schema.org Entity validation passed: {entity.get('name')} ({args.entity_path})")

    urls = [
        "https://github.com/Yotoha0303",
        "https://github.com/Yotoha0303/ai-collaboration-framework",
        "https://github.com/Yotoha0303/KnowTrace-Workflow",
        "https://github.com/Yotoha0303/go-order-management-system",
        "https://github.com/Yotoha0303/go-user-system",
    ]

    if args.generate_indexnow:
        key = sha256(b"yotoha-sovereign-indexnow-key-2026").hexdigest()[:32]
        payload = generate_indexnow_payload(
            host="github.com",
            key=key,
            url_list=urls,
            key_location="https://raw.githubusercontent.com/Yotoha0303/Yotoha0303/main/docs/indexnow_key.txt",
        )
        out_file = output_dir / "indexnow_submission.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"📦 IndexNow payload generated: {out_file}")

    if args.generate_sitemap:
        sitemap_xml = generate_baidu_sitemap_xml(urls)
        out_sitemap = output_dir / "baidu_sitemap.xml"
        with open(out_sitemap, "w", encoding="utf-8") as f:
            f.write(sitemap_xml)
        print(f"📦 Baidu sitemap XML generated: {out_sitemap}")

    if args.audit_sample:
        sample_results = [
            {
                "rank": 1,
                "title": "Yotoha (Yotoha0303) · GitHub Profile",
                "url": "https://github.com/Yotoha0303",
                "snippet": "Independent Systems Architect and AI Engineer. Go backend reliability.",
            },
            {
                "rank": 2,
                "title": "ai-collaboration-framework / Yotoha0303",
                "url": "https://github.com/Yotoha0303/ai-collaboration-framework",
                "snippet": "Executable quality gates and multi-agent coordination.",
            },
            {
                "rank": 3,
                "title": "KnowTrace-Workflow / Yotoha0303",
                "url": "https://github.com/Yotoha0303/KnowTrace-Workflow",
                "snippet": "AI-assisted knowledge workflow with traceable evidence.",
            },
            {
                "rank": 4,
                "title": "音羽-otoha- 官方音乐主页与最新单曲",
                "url": "https://music.example.com/artist/otoha",
                "snippet": "日本创作歌手 音羽-otoha- 最新专辑与单曲发售信息，J-Pop 歌单。",
            },
            {
                "rank": 5,
                "title": "V2EX - Yotoha 分享创造：2,176 条精选架构与数据单品",
                "url": "https://v2ex.com/t/1089201",
                "snippet": "在 V2EX 创造分享节点发布的极客数据包与 FTS5 引擎。",
            },
        ]
        report = audit_serp_data(sample_results, target_entity=CANONICAL_TARGET)
        md_report = format_markdown_report(report)
        out_report = output_dir / "serp_audit_report.md"
        with open(out_report, "w", encoding="utf-8") as f:
            f.write(md_report)
        print(f"🛡️ SERP Audit report saved: {out_report} (Status: {report.status.value}, Sovereignty: {report.sovereignty_ratio}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
