#!/usr/bin/env python3
"""
Automated Open-Source Market & Tech Intelligence Crawler
Maintained by Yotoha.
Fetches real-time global technology news, AI breakthroughs, and venture capital signals.
Exports normalized JSON datasets compliant with open data and API schema specifications.
"""

import os
import sys
import json
import time
import re
import urllib.request
from typing import List, Dict, Any, Optional

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

AI_KEYWORDS = ["ai", "agent", "llm", "大模型", "智能体", "anthropic", "openai", "claude", "gemini", "deepseek", "model"]
VC_KEYWORDS = ["融资", "投资", "收购", "ipo", "估值", "fund", "venture", "seed", "series a", "series b", "capital"]
BIZ_KEYWORDS = ["商业化", "变现", "出海", "营收", "saas", "pricing", "mrr", "arr", "盈利", "现金流", "revenue", "market"]
HW_KEYWORDS = ["芯片", "gpu", "英伟达", "nvidia", "esp32", "硬件", "semiconductor", "compute", "hardware"]


def fetch_url(url: str, timeout: int = 10) -> Optional[str]:
    """Fetch text from URL with custom User-Agent and timeout protection."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[WARN] Failed to fetch {url}: {e}", file=sys.stderr)
        return None


def analyze_signal(text: str) -> tuple[str, List[str], int]:
    """Classify category, tags, and score for a given signal text."""
    lower = text.lower()
    signals = []
    score = 50

    has_ai = any(w in lower for w in AI_KEYWORDS)
    has_vc = any(w in lower for w in VC_KEYWORDS)
    has_biz = any(w in lower for w in BIZ_KEYWORDS)
    has_hw = any(w in lower for w in HW_KEYWORDS)

    if has_ai:
        signals.append("AI & Agents")
        score += 25
    if has_vc:
        signals.append("Venture Capital")
        score += 20
    if has_biz:
        signals.append("Monetization & SaaS")
        score += 20
    if has_hw:
        signals.append("Hardware & Compute")
        score += 15

    if has_ai and has_biz:
        category = "AI Commercialization"
    elif has_ai:
        category = "AI & Frontier Models"
    elif has_vc:
        category = "Venture & Investment"
    elif has_hw:
        category = "Hardware & Infrastructure"
    else:
        category = "General Tech & Market"

    return category, signals, min(score, 100)


def harvest_hacker_news(limit: int = 15) -> List[Dict[str, Any]]:
    """Harvest top stories from official public Hacker News API."""
    items = []
    raw = fetch_url("https://hacker-news.firebaseio.com/v0/topstories.json")
    if not raw:
        return items

    try:
        top_ids = json.loads(raw)[:limit]
    except Exception:
        return items

    for item_id in top_ids:
        story_raw = fetch_url(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json")
        if not story_raw:
            continue
        try:
            story = json.loads(story_raw)
            title = story.get("title", "")
            url = story.get("url", f"https://news.ycombinator.com/item?id={item_id}")
            score = story.get("score", 0)

            category, signals, rel_score = analyze_signal(title)
            rel_score += min(score // 10, 30)

            items.append({
                "signal_id": f"hn_{item_id}",
                "title": title,
                "url": url,
                "source": "HackerNews",
                "category": category,
                "business_signals": ", ".join(signals) if signals else "General Tech",
                "relevance_score": min(rel_score, 100),
                "summary": f"HN Score: {score} | Comments: {story.get('descendants', 0)}",
                "as_of": time.strftime("%Y-%m-%d", time.gmtime()),
            })
        except Exception:
            pass

    return items


def harvest_chinese_commercial_flashes(limit: int = 15) -> List[Dict[str, Any]]:
    """Harvest real-time Chinese commercial newsflashes via public feeds."""
    items = []
    sources = [
        "https://rsshub.rssforever.com/36kr/newsflashes",
        "https://rsshub.feeded.xyz/36kr/newsflashes",
    ]

    content = None
    for src in sources:
        content = fetch_url(src, timeout=8)
        if content:
            break

    if not content:
        return items

    blocks = re.findall(r"<item>(.*?)</item>", content, re.DOTALL)
    for idx, block in enumerate(blocks[:limit]):
        title_match = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", block)
        link_match = re.search(r"<link>(.*?)</link>", block)
        desc_match = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>|<description>(.*?)</description>", block)

        title = title_match.group(1) or title_match.group(2) if title_match else ""
        link = link_match.group(1) if link_match else "https://36kr.com"
        desc = ""
        if desc_match:
            desc = desc_match.group(1) or desc_match.group(2) or ""
            desc = re.sub(r"<[^>]+>", "", desc).strip()

        if not title:
            continue

        category, signals, rel_score = analyze_signal(title + " " + desc)

        items.append({
            "signal_id": f"flash_{idx}_{int(time.time())}",
            "title": title,
            "url": link,
            "source": "CommercialNewsflash",
            "category": category,
            "business_signals": ", ".join(signals) if signals else "Industry Dynamics",
            "relevance_score": min(rel_score, 100),
            "summary": desc[:180] + ("..." if len(desc) > 180 else ""),
            "as_of": time.strftime("%Y-%m-%d", time.gmtime()),
        })

    return items


def main():
    print("[INFO] Starting automated intelligence collection...", file=sys.stderr)
    all_rows = []
    all_rows.extend(harvest_chinese_commercial_flashes(limit=15))
    all_rows.extend(harvest_hacker_news(limit=15))

    # Sort descending by relevance score
    all_rows.sort(key=lambda x: x["relevance_score"], reverse=True)
    print(f"[INFO] Harvested {len(all_rows)} total signals.", file=sys.stderr)

    dataset = {
        "_demo": False,
        "_note": "Automated open-source intelligence feed covering global AI industry trends, commercial monetization, and venture capital signals. Maintained by Yotoha.",
        "asset": "Global AI & Commercial Tech Intelligence Feed",
        "frequency": "daily",
        "rows_per_run": len(all_rows),
        "license": "CC BY 4.0",
        "updated_at": time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime()),
        "schema": {
            "signal_id": "string",
            "title": "string",
            "url": "string",
            "source": "string",
            "category": "string",
            "business_signals": "string",
            "relevance_score": "integer",
            "summary": "string",
            "as_of": "date"
        },
        "rows": all_rows
    }

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target_path = os.path.join(base_dir, "data", "feegate-asset-sample.json")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    print(f"[SUCCESS] Updated asset dataset saved to: {target_path} ({len(all_rows)} rows)")


if __name__ == "__main__":
    main()
