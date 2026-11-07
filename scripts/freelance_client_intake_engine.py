#!/usr/bin/env python3
"""Offline, bilingual client intake and provisional scope quotes (Python 3.10+).

    python3 scripts/freelance_client_intake_engine.py --platform xianyu \
        --inquiry '采集一个公开网站的商品价格，导出 CSV，预算1500元，3天内完成'
    python3 scripts/freelance_client_intake_engine.py --platform v2ex \
        --inquiry-file inquiry.txt --format markdown
    python3 scripts/freelance_client_intake_engine.py --inquiry-file - < inquiry.txt

Only the standard library is used; no model, network, credentials or platform
account is needed. Rules recognize Chinese/English signals, local negations and
explicit scope counts. Scores and confidence labels are heuristics, not calibrated
probabilities. Repeated words do not increase a rule's score. Unknown and mixed
requests remain visible instead of being forced into a fixed quote.

The three offer prices, scope caps and shared SLA follow the repository's
commercial_portfolio_spec_exporter.py cards. They are conditional offers, not
confirmed bookings. A data pipeline or a request beyond a card's limits needs a
separate scope review; the nearest card is only a price reference. Engineering
effort estimates are distinct from the conditional 48 elapsed hour delivery SLA.
All public analysis functions return fresh, JSON-serializable dictionaries/lists.
CLI exits: 0 analysis produced (including inquiries needing review), 2 bad input.
"""

from __future__ import annotations

import argparse
import copy
from decimal import Decimal
import html
import json
import math
from pathlib import Path
import re
import sys
import unicodedata


SCHEMA_VERSION = "1.0"
MAX_INQUIRY_LENGTH = 20_000
INTENTS = {
    "scraper": ("Scraper", "网页信息采集"),
    "api_integration": ("API Integration", "连接现有服务"),
    "agent_automation": ("Agent Automation", "日常工作自动处理"),
    "data_pipeline": ("Data Pipeline", "数据整理与汇总"),
}
PLATFORMS = {"xianyu": "Xianyu", "v2ex": "V2EX", "eleduck": "Eleduck"}
_PLATFORM_ALIASES = {"闲鱼": "xianyu", "电鸭": "eleduck", **{key: key for key in PLATFORMS}}


class IntakeError(ValueError):
    """An inquiry or quote constraint is invalid."""


def _pair(zh, en):
    return {"zh": zh, "en": en}


# Keep these commercial terms aligned with commercial_portfolio_spec_exporter.
STANDARD_SLA = {
    "delivery_hours": 48, "acceptance_hours": 24, "support_calendar_days": 7,
    "response_business_hours": 4, "remediation_business_hours": 9,
    "included_revision_rounds": 1, "late_credit_percent_per_started_24h": 10,
    "late_credit_cap_percent": 30,
    "coverage": "Mon-Fri 09:00-18:00 Asia/Shanghai",
    "clock_start": _pair(
        "双方确认范围、验收样例与档期，资料和访问权限齐备且托管方确认全额入金后开始计时。",
        "Starts after scope, acceptance examples and booking are agreed, inputs/access are ready and full escrow funding is confirmed."),
    "clock_pause": _pair(
        "资料或权限缺失、客户依赖服务中断时，双方记录并确认暂停和恢复时间；新增需求另行报价。",
        "Missing inputs/access or client dependency outages pause the clock with mutually recorded stop/resume times; scope changes require a new quote."),
    "acceptance": _pair(
        "交付后24小时内按约定样例验收或列明问题；托管款在明确验收后释放，超时不自动放款。",
        "Review agreed examples within 24 hours of handoff or list defects; escrow releases on explicit acceptance, with no automatic release on timeout."),
    "support": _pair(
        "交付后7个自然日内，范围内问题4个工作小时响应，9个工作小时修复或提供可用替代方案；含一轮范围内修订。",
        "For 7 calendar days after handoff, acknowledge in-scope defects within 4 business hours and fix or provide a working workaround within 9 business hours; one in-scope revision included."),
    "remedy": _pair(
        "供应方延迟交付每开始24小时抵扣服务费10%；修复时限违约按同一比例抵扣，合计上限30%。",
        "Supplier delivery delay earns 10% fee credit per started 24 hours; missed remediation deadlines earn the same credit, with a combined 30% cap."),
}
SLA_CARDS = {
    "scraper": {
        "id": "scraper", "price_cny": 1500,
        "title": _pair("网页采集交付卡", "Scraper delivery card"),
        "limits": {"sites": 1, "templates": 3, "fields": 10},
        "scope": _pair("1个授权站点，最多3种静态页面、10个字段，导出JSON或CSV。",
                       "One authorized site, up to 3 static page templates and 10 fields; JSON/CSV export."),
        "exclusions": _pair("不含动态页面渲染、验证码处理、付费数据和长期托管。",
                            "Excludes JS rendering, CAPTCHA handling, paid data acquisition and ongoing hosting."),
    },
    "metering_gateway": {
        "id": "metering_gateway", "price_cny": 3500,
        "title": _pair("服务连接与用量计费交付卡", "API metering gateway delivery card"),
        "limits": {"upstream_apis": 1, "ledgers": 1, "quota_tiers": 3},
        "scope": _pair("1个上游服务、1本SQLite账本，最多3档调用频率或预付额度配置。",
                       "One upstream API, one SQLite ledger, up to 3 RPM/TPM/prepaid quota configurations."),
        "exclusions": _pair("不含支付渠道开户、税务发票、跨区高可用和上游服务费用。",
                            "Excludes payment-provider onboarding, tax invoicing, multi-region HA and upstream API fees."),
    },
    "multi_agent_mesh": {
        "id": "multi_agent_mesh", "price_cny": 8000,
        "title": _pair("智能工作流交付卡", "Multi-agent workflow delivery card"),
        "limits": {"agents": 3, "workflows": 1, "environments": 1},
        "scope": _pair("单一运行环境，最多3个智能体或节点、1条工作流，演示任务分配、故障切换和状态同步。",
                       "Up to 3 agents/peers in one runtime environment and one workflow; routing, failover and state-sync demo."),
        "exclusions": _pair("不含模型训练、模型调用费、公网节点运维和生产规模性能承诺。",
                            "Excludes model training, inference fees, public-node operations and production-scale performance commitments."),
    },
}

_RULES = {
    "scraper": (
        (5, r"爬虫|爬取|抓取|网页采集|网站采集|采集器"),
        (5, r"\b(?:scrap(?:e|er|ers|ing)|crawl(?:er|ers|ing)?)\b"),
        (4, r"(?:采集|提取|收集).{0,16}(?:网页|网站|商品|价格)|(?:网站|网页).{0,16}(?:采集|提取|收集|整理)"),
        (4, r"\b(?:extract|collect|monitor)\b.{0,35}\b(?:websites?|web pages?|prices?)\b"),
    ),
    "api_integration": (
        (5, r"\bapi[- ]integration\b|接口对接|对接.{0,12}(?:api|接口)|(?:api|接口).{0,12}(?:对接|接入)"),
        (4, r"\b(?:webhooks?|oauth|payment gateway|metering gateway)\b|计量网关|支付回调|预付配额"),
        (3, r"(?<![a-z0-9_])api(?![a-z0-9_])|接口"),
        (4, r"\b(?:connect|integrate)\b.{0,35}\b(?:stripe|shopify|services?|systems?)\b|接入.{0,12}(?:支付|服务)"),
    ),
    "agent_automation": (
        (5, r"\b(?:agents?|multi[- ]agent|agent[- ]automation|rpa|chatbots?)\b|智能体|机器人"),
        (4, r"\b(?:llm|ai assistant|automate|automation)\b|自动化|自动回复|自动填写|自动处理|代替人工"),
        (4, r"\b(?:without manual intervention|no[- ]code workflow)\b|无人值守|自动完成"),
    ),
    "data_pipeline": (
        (5, r"\b(?:data[- ]pipelines?|etl|elt|data warehouse)\b|数据管道|数据流水线|数据仓库|数仓"),
        (4, r"\b(?:data[- ]cleaning|deduplicat(?:e|ion|ing)|transform(?:ation)?|merge spreadsheets?|data[- ]sync)\b|数据清洗|清洗|去重|数据同步|定时同步|合并表格"),
        (3, r"(?:汇总|合并).{0,16}(?:表格|报表|数据)|\b(?:aggregate|consolidate)\b.{0,25}\b(?:data|reports?|files?)\b"),
    ),
}
_COMPILED_RULES = {key: tuple((weight, re.compile(pattern, re.I)) for weight, pattern in rules)
                   for key, rules in _RULES.items()}
_CLAUSE_SPLIT = re.compile(
    r"[\n。！？!?;；]+|(?<!\d)[,.，](?!\d)|\b(?:but|however|instead)\b|但是|不过|而是", re.I)
_NEGATION = re.compile(
    r"\b(?:no|not|never|without|don't|doesn't|do not|does not)\b.{0,45}$|"
    r"(?:不需要|不考虑|不涉及|不包含|不含|不要|不用|无需|不做|不想).{0,18}$", re.I)
_NEGATIVE_SUFFIX = re.compile(
    r"\s*(?:(?:is|are)\s+)?(?:not (?:needed|required)|out of scope)\b|\s*(?:不需要|不用做|不要做)", re.I)
_POSITIVE_EXCEPTIONS = re.compile(
    r"\b(?:not only|no more than|no[- ]code|without (?:manual|human)(?: intervention)?)\b|不仅|不但", re.I)


def normalize_inquiry(inquiry):
    if not isinstance(inquiry, str) or len(inquiry) > MAX_INQUIRY_LENGTH:
        raise IntakeError(f"inquiry must be text of at most {MAX_INQUIRY_LENGTH} characters")
    text = html.unescape(unicodedata.normalize("NFKC", inquiry))
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.translate(str.maketrans({"’": "'", "–": "-", "—": "-", "‑": "-", "‐": "-"}))
    text = "".join(char for char in text if char.isspace() or not unicodedata.category(char).startswith("C"))
    text = re.sub(r"[^\S\n]+", " ", text).strip()
    if not text or len(text) > MAX_INQUIRY_LENGTH:
        raise IntakeError("inquiry must contain visible text within the length limit")
    return text


def _affirmative(clause, match):
    prefix = _POSITIVE_EXCEPTIONS.sub(" ", clause[:match.start()])
    return not (_NEGATION.search(prefix) or _NEGATIVE_SUFFIX.match(clause, match.end()))


def _signals(text, pattern):
    found = set()
    for clause in _CLAUSE_SPLIT.split(text):
        for match in pattern.finditer(clause):
            if _affirmative(clause, match):
                found.add(match.group().strip().casefold())
    return sorted(found, key=lambda item: (-len(item), item))[:5]


def classify_intent(inquiry):
    """Rank all four intents, preserving ambiguity and rule evidence."""
    text = normalize_inquiry(inquiry)
    scores, evidence = {}, {}
    for intent, rules in _COMPILED_RULES.items():
        score, matches = 0, set()
        for weight, pattern in rules:
            signals = _signals(text, pattern)
            if signals:
                score += weight
                matches.update(signals)
        scores[intent] = score
        evidence[intent] = sorted(matches)[:8]
    ranked = sorted((key for key in INTENTS if scores[key]), key=lambda key: -scores[key])
    primary = ranked[0] if ranked else "unknown"
    ambiguous = not ranked or (len(ranked) > 1 and scores[ranked[1]] >= scores[primary] * 0.75)
    confidence = "low" if ambiguous or scores.get(primary, 0) < 4 else (
        "high" if scores[primary] >= 8 else "medium")
    return {"primary": primary, "label": INTENTS.get(primary, ("Unknown",))[0],
            "secondary": ranked[1:], "ambiguous": ambiguous, "confidence": confidence,
            "scores": scores, "evidence": evidence}


_NUMBER = (r"(?<![a-z0-9])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*[k千万]?|"
           r"\d{1,12}(?:\.\d+)?\s*[k千万]?|[零一二两三四五六七八九十百千万]+|"
           r"zero\b|one\b|two\b|three\b|four\b|five\b|six\b|seven\b|eight\b|nine\b|ten\b)")
_COUNT_RULES = {
    "sites": rf"({_NUMBER})\s*(?:个)?\s*(?:授权|公开|authorized\s+|authorised\s+|public\s+)?(?:网站|站点|websites?\b|sites?\b)",
    "templates": rf"({_NUMBER})\s*(?:个|种)?\s*(?:静态|static\s+)?(?:页面模板|页面|templates?\b|page templates?\b|pages?\b)",
    "fields": rf"({_NUMBER})\s*(?:个)?\s*(?:字段|fields?\b)",
    "upstream_apis": rf"({_NUMBER})\s*(?:个)?\s*(?:上游\s*|upstream\s+)?(?:apis?(?![a-z0-9_])|接口)",
    "ledgers": rf"({_NUMBER})\s*(?:个|本)?\s*(?:sqlite\s*)?(?:账本|ledgers?\b)",
    "quota_tiers": rf"({_NUMBER})\s*(?:档|种)\s*(?:调用|配额|额度|频率)|({_NUMBER})\s*(?:quota|prepaid)\s+tiers?\b",
    "agents": rf"({_NUMBER})\s*(?:个)?\s*(?:智能体|节点|agents?\b|peers?\b|nodes?\b)",
    "workflows": rf"({_NUMBER})\s*(?:条|个)?\s*(?:工作流|workflows?\b)",
    "environments": rf"({_NUMBER})\s*(?:个)?\s*(?:运行环境|环境|(?:runtime\s+)?environments?\b)",
    "records": rf"({_NUMBER})\s*(?:条|个)?\s*(?:记录|数据|records?\b|rows?\b)",
}


def _integer(token):
    token = token.strip().casefold()
    words = {word: number for number, word in enumerate(
        ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"))}
    if token in words:
        return words[token]
    if token[0].isascii() and token[0].isdigit():
        return _money(token)
    digits = dict(zip("零一二两三四五六七八九", (0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    section, total, current = 0, 0, 0
    for char in token:
        if char in digits:
            current = digits[char]
        elif char == "万":
            total += (section + current or 1) * 10_000
            section, current = 0, 0
        else:
            section += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
    return total + section + current


def _extract_counts(text):
    counts = {}
    for key, expression in _COUNT_RULES.items():
        values = []
        for clause in _CLAUSE_SPLIT.split(text):
            for match in re.finditer(expression, clause, re.I):
                if _affirmative(clause, match):
                    values.append(_integer(next(group for group in match.groups() if group)))
        if values:
            counts[key] = max(values)
    return counts


_FACTOR_RULES = {
    "js_rendering": (3, r"\b(?:javascript rendering|js rendering|headless browser|playwright|selenium)\b|动态页面|浏览器渲染"),
    "captcha": (4, r"\bcaptchas?\b|验证码"),
    "login": (2, r"\b(?:login|log in|sign in)\b|登录|登陆"),
    "realtime": (3, r"\breal[- ]time\b|实时"),
    "production_ha": (5, r"\b(?:high availability|multi[- ]region|99\.99%|production[- ]scale|24/7)\b|高可用|跨区|生产规模|全天候"),
    "model_training": (5, r"\b(?:model training|fine[- ]tun(?:e|ing))\b|模型训练|微调模型"),
    "hosting": (2, r"\b(?:ongoing hosting|managed hosting|long[- ]term hosting)\b|长期托管|长期运维"),
    "scheduled": (1, r"\b(?:daily|hourly|weekly|scheduled|every day|every hour)\b|每天|每日|定时|每小时|每周"),
    "billing": (1, r"\b(?:billing|metering|prepaid|ledger)\b|计费|计量|账本|预付配额"),
    "open_ended": (4, r"\b(?:unlimited|any number of|all websites)\b|不限数量|不限站点|所有网站|无限"),
    "multiple_sources": (3, r"\b(?:multiple|several)\s+(?:websites?|sites?|upstream apis?|workflows?|environments?)\b|多个网站|多个站点|多站点|多个上游|多条工作流|多个环境"),
    "urgent": (1, r"\b(?:urgent|asap|today|tomorrow)\b|加急|今晚|今天|明天|尽快"),
}
_COMPILED_FACTORS = {key: (weight, re.compile(pattern, re.I))
                     for key, (weight, pattern) in _FACTOR_RULES.items()}


def estimate_complexity(inquiry):
    """Estimate engineering effort, independent of the card's elapsed SLA."""
    text = normalize_inquiry(inquiry)
    classification = classify_intent(text)
    counts = _extract_counts(text)
    factors = []
    score = {"scraper": 1, "api_integration": 2, "agent_automation": 4,
             "data_pipeline": 2, "unknown": 0}[classification["primary"]]
    for key, (weight, pattern) in _COMPILED_FACTORS.items():
        signals = _signals(text, pattern)
        if signals:
            factors.append({"id": key, "weight": weight, "evidence": signals})
            score += weight
    if classification["secondary"]:
        weight = 2 * len(classification["secondary"])
        factors.append({"id": "mixed_intents", "weight": weight,
                        "evidence": classification["secondary"]})
        score += weight
    if any(counts.get(key, 0) > cap for key, cap in (
            ("sites", 1), ("upstream_apis", 1), ("agents", 3), ("workflows", 1),
            ("environments", 1), ("fields", 10), ("templates", 3), ("quota_tiers", 3), ("ledgers", 1))):
        factors.append({"id": "expanded_scope", "weight": 3, "evidence": []})
        score += 3
    if counts.get("records", 0) >= 100_000:
        factors.append({"id": "high_volume", "weight": 3, "evidence": [str(counts["records"])]})
        score += 3
    level = "unknown" if classification["primary"] == "unknown" else (
        "low" if score <= 3 else "medium" if score <= 7 else "high")
    effort = {"unknown": None, "low": [4, 12], "medium": [12, 32], "high": [32, 80]}[level]
    return {"level": level, "score": score, "estimated_effort_hours": effort,
            "scope_counts": counts, "factors": factors}


_AMOUNT = r"\d{1,12}(?:,\d{3})*(?:\.\d{1,2})?\s*[k千万]?(?![\d.])"
_BUDGET = re.compile(
    rf"(?:预算|budget|最多|上限)\s*(?:是|为|of|is|:|=|不超过|约|大约|around|up to)?\s*"
    rf"(?:cny|rmb|[¥￥])?\s*({_AMOUNT})\s*(?:元|块|cny|rmb|yuan)?"
    rf"(?:\s*(?:-|~|至|到|to)\s*(?:[¥￥])?\s*({_AMOUNT}))?", re.I)
_CURRENCY_AMOUNT = re.compile(rf"(?:[¥￥]|\b(?:cny|rmb)\s*)\s*({_AMOUNT})|({_AMOUNT})\s*(?:元|块|\byuan\b)", re.I)
_DEADLINE = re.compile(
    rf"(?:\b(?:within|in|deadline(?: is|:)?)\s*|(?:交期|工期|交付期限)\s*(?:是|为|:)?\s*)"
    rf"({_NUMBER})\s*(小时|天|周|hours?\b|hrs?\b|days?\b|weeks?\b)|"
    rf"({_NUMBER})\s*(小时|天|周)\s*(?:内|之内)?\s*(?:交付|完成|交货|搞定|内)", re.I)


def _money(token):
    value = token.strip().casefold().replace(",", "")
    multiplier = {"k": 1000, "千": 1000, "万": 10_000}.get(value[-1], 1)
    if multiplier != 1:
        value = value[:-1].strip()
    amount = Decimal(value) * multiplier
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _constraint(value, name):
    if value is None:
        return None
    if type(value) not in (int, float) or not 0 <= value <= 1_000_000_000 or not math.isfinite(value):
        raise IntakeError(f"{name} must be a finite number between 0 and 1000000000")
    return value


def _constraints(text, budget_cny, deadline_hours):
    budget_cny = _constraint(budget_cny, "budget_cny")
    deadline_hours = _constraint(deadline_hours, "deadline_hours")
    if budget_cny is None:
        budgets = [match for match in _BUDGET.finditer(text)
                   if not re.match(r"\s*(?:usd\b|eur\b|dollars?\b|euros?\b|美元|欧元|[$€£])",
                                   text[match.end():], re.I)]
        if budgets:
            budget_cny = min(max(_money(token) for token in match.groups() if token) for match in budgets)
        else:
            amounts = [_money(next(group for group in match.groups() if group))
                       for match in _CURRENCY_AMOUNT.finditer(text)]
            if len(amounts) == 1:
                budget_cny = amounts[0]
    if deadline_hours is None:
        deadlines = []
        for match in _DEADLINE.finditer(text):
            number, unit = match.groups()[:2] if match.group(1) else match.groups()[2:]
            multiplier = 168 if unit.casefold().startswith("week") or unit == "周" else (
                24 if unit.casefold().startswith("day") or unit == "天" else 1)
            deadlines.append(_integer(number) * multiplier)
        if deadlines:
            deadline_hours = min(deadlines)
    return {"budget_cny": _constraint(budget_cny, "budget_cny"),
            "deadline_hours": _constraint(deadline_hours, "deadline_hours")}


def _language(text, language):
    if not isinstance(language, str) or language not in {"auto", "zh", "en"}:
        raise IntakeError("language must be auto, zh or en")
    return ("zh" if re.search(r"[\u3400-\u9fff]", text) else "en") if language == "auto" else language


_QUESTIONS = {
    "goal": _pair("您最想省掉哪一步工作？做完后希望拿到什么结果？",
                  "Which task should this save you from doing, and what result would you like to receive?"),
    "priority": _pair("您提到了几件事，哪件必须先完成？其余部分可以放到下一阶段吗？",
                      "You mentioned several tasks. Which must come first, and could the others wait for a later phase?"),
    "sources": _pair("需要用到哪些网站、服务或文件？可以发一个链接或小样吗？",
                     "Which websites, services or files will we use? Could you share a link or a small example?"),
    "fields": _pair("最后的表格需要哪些栏目？可以发一份您喜欢的样表吗？",
                    "Which columns should the final table contain? Could you share a sample table?"),
    "volume": _pair("每次大约要处理多少条信息或多少份文件？",
                    "About how many items or files need to be handled each time?"),
    "frequency": _pair("这件事只做一次，还是需要每天或每周重复做？",
                       "Is this a one-time task, or should it repeat daily or weekly?"),
    "access": _pair("这些资料可以直接打开，还是需要您提供账号或开通访问权限？",
                    "Can these materials be opened directly, or will you need to arrange account access?"),
    "example": _pair("能否提供一组输入和您期望的结果，作为交付时一起核对的例子？",
                      "Could you share one input and the expected result so we can check the delivery together?"),
    "budget": _pair("这次希望控制在多少预算内？第三方服务费用是否单独计算？",
                    "What budget should this fit within, and will third-party service costs be paid separately?"),
    "deadline": _pair("最晚什么时候需要拿到可使用的版本？",
                      "When is the latest you need a usable version?"),
    "gateway_fit": _pair("连接服务后，是否还需要记录每人的用量并按额度收费？",
                         "After connecting the service, do you also need to track each person's usage and charge against a balance?"),
}


def _questions(text, classification, complexity, constraints, language):
    ids = []
    primary = classification["primary"]
    factors = {factor["id"] for factor in complexity["factors"]}
    if primary == "unknown":
        ids.append("goal")
    if classification["secondary"]:
        ids.append("priority")
    if not re.search(r"https?://\S+", text):
        ids.append("sources")
    if primary == "scraper" and "fields" not in complexity["scope_counts"]:
        ids.append("fields")
    if primary == "api_integration" and "billing" not in factors:
        ids.append("gateway_fit")
    if "records" not in complexity["scope_counts"]:
        ids.append("volume")
    if not (factors & {"scheduled", "realtime"}) and not re.search(r"一次|\bone[- ]time\b|\bmanually\b|手动", text, re.I):
        ids.append("frequency")
    if not re.search(r"公开|已授权|\b(?:public|authorized|authorised)\b", text, re.I):
        ids.append("access")
    ids.append("example")
    if constraints["budget_cny"] is None:
        ids.append("budget")
    if constraints["deadline_hours"] is None:
        ids.append("deadline")
    return [{"id": key, "question": _QUESTIONS[key][language]} for key in ids]


def generate_clarifying_questions(inquiry, *, language="auto", budget_cny=None, deadline_hours=None):
    text = normalize_inquiry(inquiry)
    return _questions(text, classify_intent(text), estimate_complexity(text),
                      _constraints(text, budget_cny, deadline_hours), _language(text, language))


_ISSUES = {
    "unknown_intent": _pair("需要先明确希望交付的结果。", "The desired outcome needs clarification first."),
    "mixed_intents": _pair("需求横跨多类工作，需要先确认首期边界。", "Several kinds of work are requested; agree the first phase's boundaries."),
    "ambiguous_intent": _pair("需求方向存在歧义，需要先确认。", "The intended kind of work needs clarification."),
    "pipeline_custom": _pair("现有交付卡未覆盖独立数据管道，需要单独确认范围和报价。", "A standalone data pipeline has no standard card and needs a separate scope and quote."),
    "gateway_fit": _pair("¥3,500交付卡包含用量计费；其他服务连接需求需另行确认。", "The ¥3,500 card covers a metering gateway; other integrations need a separate scope check."),
    "scope_limit": _pair("所需数量超过交付卡上限。", "Requested quantities exceed the card's scope limits."),
    "excluded_feature": _pair("需求包含交付卡范围外的功能。", "The inquiry includes features outside this card."),
    "high_complexity": _pair("工作量较高，需要分阶段确认交期和报价。", "The estimated effort needs a phased delivery and price review."),
    "budget_gap": _pair("当前预算低于参考交付卡价格，可先缩小首期范围。", "The budget is below the reference card; consider a smaller first phase."),
    "deadline_gap": _pair("期望交期短于标准48小时，需要单独确认档期。", "The requested deadline is shorter than the standard 48 hours; confirm availability separately."),
}


def _match(classification, complexity, constraints, language):
    primary = classification["primary"]
    card_id = {"scraper": "scraper", "api_integration": "metering_gateway",
               "agent_automation": "multi_agent_mesh", "data_pipeline": "metering_gateway"}.get(primary)
    card = copy.deepcopy(SLA_CARDS[card_id]) if card_id else None
    issues = []

    def issue(code, **detail):
        issues.append({"code": code, "message": _ISSUES[code][language], **detail})

    if card is None:
        issue("unknown_intent")
    else:
        factors = {factor["id"] for factor in complexity["factors"]}
        if classification["secondary"]:
            issue("mixed_intents", intents=[primary, *classification["secondary"]])
        elif classification["ambiguous"]:
            issue("ambiguous_intent")
        if primary == "data_pipeline":
            issue("pipeline_custom")
        if primary == "api_integration" and "billing" not in factors:
            issue("gateway_fit")
        for name, cap in card["limits"].items():
            requested = complexity["scope_counts"].get(name)
            if requested is not None and requested > cap:
                issue("scope_limit", dimension=name, requested=requested, included=cap)
        excluded = factors & {"production_ha", "model_training", "hosting", "open_ended", "high_volume", "realtime", "multiple_sources"}
        if card_id == "scraper":
            excluded |= factors & {"js_rendering", "captcha"}
        if excluded:
            issue("excluded_feature", features=sorted(excluded))
        if complexity["level"] == "high":
            issue("high_complexity")
        if constraints["budget_cny"] is not None and constraints["budget_cny"] < card["price_cny"]:
            issue("budget_gap", shortfall_cny=card["price_cny"] - constraints["budget_cny"])
        if constraints["deadline_hours"] is not None and constraints["deadline_hours"] < STANDARD_SLA["delivery_hours"]:
            issue("deadline_gap")
    status = "needs_clarification" if card is None else "scope_review_required" if issues else "indicative"
    return {"status": status, "card_id": card_id, "price_cny": card["price_cny"] if card else None,
            "currency": "CNY", "reference_only": bool(issues), "card": card,
            "delivery_hours": STANDARD_SLA["delivery_hours"] if card and not issues else None,
            "sla": copy.deepcopy(STANDARD_SLA) if card else None, "issues": issues,
            "constraints": constraints}


def match_sla_card(inquiry, *, budget_cny=None, deadline_hours=None, language="auto"):
    text = normalize_inquiry(inquiry)
    return _match(classify_intent(text), estimate_complexity(text),
                  _constraints(text, budget_cny, deadline_hours), _language(text, language))


_OUTCOMES = {
    "scraper": _pair("把您指定网页里的信息整理成可直接使用的表格，减少重复复制。",
                     "Turn information from your chosen pages into a usable table and reduce repeated copying."),
    "api_integration": _pair("让现有服务按约定传递信息，减少来回搬运和人工核对。",
                             "Let your existing services exchange the agreed information and reduce manual transfer and checking."),
    "agent_automation": _pair("把重复工作串成可检查的流程，让您把时间留给需要判断的事情。",
                              "Turn repeat tasks into a workflow you can check, leaving you time for decisions."),
    "data_pipeline": _pair("把分散的数据整理、汇总成一致的结果，方便后续使用。",
                           "Clean and combine scattered data into consistent results you can use."),
    "unknown": _pair("先确认最有价值的一步，再按可检查的结果确定首期范围。",
                     "Identify the most useful first step, then agree a small delivery with a result you can check."),
}
_ACCEPTANCE = {
    "scraper": _pair("按约定样例核对表格栏目、内容、重复项和来源；异常情况能明确提示。",
                     "Check agreed examples for columns, values, duplicates and sources, with clear failure messages."),
    "api_integration": _pair("用约定样例核对服务间传递的内容；失败和重复提交的处理符合约定。",
                             "Check the agreed data exchange examples, including failures and repeated submissions."),
    "agent_automation": _pair("按约定步骤演示完整流程，核对每一步结果，以及失败后的恢复方式。",
                              "Walk through the agreed workflow, checking each result and recovery after a failed step."),
    "data_pipeline": _pair("用约定样例核对整理前后的数量、栏目、重复项和汇总结果。",
                           "Check the agreed examples for input/output counts, columns, duplicates and summary results."),
    "unknown": _pair("双方先确认一组输入和期望结果，再开始交付计时。",
                     "Agree one input and expected result together before starting the delivery clock."),
}


def analyze_inquiry(inquiry, platform="xianyu", *, budget_cny=None, deadline_hours=None, language="auto"):
    """Produce an auditable analysis and a client-facing proposal; send nothing."""
    text = normalize_inquiry(inquiry)
    if not isinstance(platform, str) or platform.strip().casefold() not in _PLATFORM_ALIASES:
        raise IntakeError("platform must be xianyu, v2ex or eleduck (闲鱼/电鸭 also accepted)")
    platform = _PLATFORM_ALIASES[platform.strip().casefold()]
    language = _language(text, language)
    classification = classify_intent(text)
    complexity = estimate_complexity(text)
    constraints = _constraints(text, budget_cny, deadline_hours)
    quote = _match(classification, complexity, constraints, language)
    questions = _questions(text, classification, complexity, constraints, language)
    primary = classification["primary"]
    intro = {
        "xianyu": _pair("您好，可以先把首期要做的结果和价格说清楚，再按样例验收。",
                        "Hello! Let's agree the first result and price, then check the delivery against an example."),
        "v2ex": _pair("可以按明确范围交付，附完整代码、运行说明和可重复核对的样例。",
                      "I can scope a concrete delivery with source code, run instructions and repeatable acceptance examples."),
        "eleduck": _pair("可以按远程协作方式推进：先确认范围，再同步样例，最后一起验收。",
                         "We can work remotely in clear stages: agree scope, review an example, then check the delivery together."),
    }[platform][language]
    title = INTENTS[primary][1 if language == "zh" else 0] if primary in INTENTS else (
        "需求确认与首期方案" if language == "zh" else "Clarify the first delivery")
    money = f"¥{quote['price_cny']:,}" if quote["price_cny"] is not None else (
        "确认需求后报价" if language == "zh" else "Quote after clarification")
    proposal = {
        "title": title, "opening": intro, "client_goal": " ".join(text.split())[:240],
        "outcome": _OUTCOMES[primary][language],
        "price_display": money,
        "price_note": _pair("参考价格；范围、样例和档期经双方确认后生效。",
                            "Indicative price, subject to mutual confirmation of scope, examples and availability.")[language],
        "reference_scope": quote["card"]["scope"][language] if quote["card"] else None,
        "deliverables": [item[language] for item in (
            _pair("完整源代码、配置样例和启动/恢复说明。", "Source code, example configuration and startup/recovery instructions."),
            _pair("可重复运行的验收样例、测试结果和交付文件清单。", "Repeatable acceptance examples, test results and a delivery file manifest."))],
        "acceptance": _ACCEPTANCE[primary][language],
        "timeline": (_pair("标准范围在前置条件满足后48个自然小时交付。",
                           "Standard scope: delivery within 48 elapsed hours after the start conditions are met.") if quote["delivery_hours"] else
                     _pair("交期在确认首期范围和档期后单独约定。", "Agree a delivery date after reviewing the first phase and availability."))[language],
        "sla": {key: value[language] if isinstance(value, dict) else value
                for key, value in (quote["sla"] or {}).items()},
        "exclusions": quote["card"]["exclusions"][language] if quote["card"] else None,
        "scope_notes": [item["message"] for item in quote["issues"]],
        "questions": [item["question"] for item in questions],
        "next_step": _pair("先回复以上问题并提供一个小样，我会据此确认首期范围、验收方式和最终报价。",
                           "Reply to the questions with a small example so I can confirm the first scope, acceptance checks and final quote.")[language],
    }
    return {"schema_version": SCHEMA_VERSION, "platform": platform, "language": language,
            "classification": classification, "complexity": complexity, "quote": quote,
            "clarifying_questions": questions, "proposal": proposal}


def _markdown_text(value):
    # Inquiry text is untrusted; prevent raw HTML, links and image markup.
    return re.sub(r"([\\`*_\[\]{}|])", r"\\\1", html.escape(value, quote=False))


def render_proposal(analysis):
    """Render a generated analysis as a copyable Markdown proposal card."""
    proposal, language = analysis["proposal"], analysis["language"]
    zh = language == "zh"
    lines = [f"## {proposal['title']}", "", proposal["opening"], "",
             ("您的目标：" if zh else "Your goal: ") + _markdown_text(proposal["client_goal"]), "",
             proposal["outcome"], "", f"**{proposal['price_display']}** — {proposal['price_note']}", ""]
    if proposal["reference_scope"]:
        lines += [("参考范围：" if zh else "Reference scope: ") + proposal["reference_scope"], ""]
    lines += [("交付内容：" if zh else "Deliverables:"), "",
              *(f"- {item}" for item in proposal["deliverables"]), "", proposal["acceptance"], "", proposal["timeline"], ""]
    if proposal["sla"]:
        sla = proposal["sla"]
        lines += [sla["clock_start"], "", sla["clock_pause"], "", sla["acceptance"], "", sla["support"], "",
                  ("工作时段：" if zh else "Business hours: ") + sla["coverage"], "", sla["remedy"], ""]
    if proposal["exclusions"]:
        lines += [proposal["exclusions"], ""]
    if proposal["scope_notes"]:
        lines += [("需要确认：" if zh else "Scope to confirm:"), "",
                  *(f"- {item}" for item in proposal["scope_notes"]), ""]
    lines += [("请帮忙确认：" if zh else "Please clarify:"), "",
              *(f"{index}. {question}" for index, question in enumerate(proposal["questions"], 1)),
              "", proposal["next_step"], ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--inquiry", help="Chinese or English client message")
    source.add_argument("--inquiry-file", help="UTF-8 message file, or - for stdin")
    parser.add_argument("--platform", default="xianyu", help="xianyu / v2ex / eleduck")
    parser.add_argument("--budget-cny", type=float, help="Explicit CNY budget overrides message extraction")
    parser.add_argument("--deadline-hours", type=float, help="Explicit elapsed-hour deadline overrides message extraction")
    parser.add_argument("--language", choices=("auto", "zh", "en"), default="auto")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    try:
        inquiry = args.inquiry
        if args.inquiry_file == "-":
            inquiry = sys.stdin.read(MAX_INQUIRY_LENGTH + 1)
        elif args.inquiry_file:
            with Path(args.inquiry_file).open(encoding="utf-8") as handle:
                inquiry = handle.read(MAX_INQUIRY_LENGTH + 1)
        result = analyze_inquiry(inquiry, args.platform, budget_cny=args.budget_cny,
                                 deadline_hours=args.deadline_hours, language=args.language)
        print(render_proposal(result) if args.format == "markdown" else
              json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (IntakeError, OSError, UnicodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
