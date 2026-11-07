#!/usr/bin/env python3
"""Export bilingual CTO capability sheets, fixed-scope offers and outreach.

Python 3.10+, standard library only; offline and deterministic. Examples::

    python3 scripts/commercial_portfolio_spec_exporter.py --repo . --output /tmp/portfolio
    python3 scripts/commercial_portfolio_spec_exporter.py --lang en --kind spec
    python3 scripts/commercial_portfolio_spec_exporter.py --metrics run.json --format json
    python3 -m unittest discover -s scripts -p 'test_commercial_portfolio_spec_exporter.py' -v

Default capability profiles describe the repository's scraper, metering gateway
and multi-agent mesh implementations. AST inspection reads their docstrings and
requires matching test definitions, without importing or executing any code.
--profiles accepts a JSON object with optional bilingual ``provider`` and a
``capabilities`` array. Each entry has id (scraper, metering_gateway or
multi_agent_mesh), bilingual title/summary objects ({"zh": ..., "en": ...}), and
``modules`` (non-test Python paths relative to the repository's scripts/).
Every specified module must have a sibling test_<name>.py containing tests.
Default profiles with missing evidence are omitted and reported as unavailable;
explicit profiles with missing evidence fail validation.

--metrics accepts JSON with total, passed, failed, errors, skipped, source, and
optional expected_failures/unexpected_successes (default 0). Counts must sum to
total. Optional inventory_sha256 binds the report to the scanned Python sources;
a mismatch is rejected. Executed results are always labeled input-reported, as
the exporter does not run or authenticate the report. Declared functions are not
executed cases; no passing-test or performance claims are inferred from them.

Escrow hashes bind the entire bilingual card, including prices, evidence and SLA,
using sorted-key, compact UTF-8 JSON excluding escrow_verification_hash. They
verify terms integrity, not funding; funding is checked separately by the escrow
provider. Use verify_escrow_hash(card, expected_hash) with an independently saved
hash to check a card. No messages are sent and existing exports are not replaced.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import shutil
import sys
import unicodedata


SCHEMA_VERSION = "1.0"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 2_000_000
LANGUAGES = ("zh", "en")
PRICES_CNY = {"scraper": 1500, "metering_gateway": 3500, "multi_agent_mesh": 8000}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}


class PortfolioError(ValueError):
    """An input cannot produce a reliable portfolio export."""


def bilingual(zh, en):
    return {"zh": zh, "en": en}


DEFAULT_PROFILES = (
    {"id": "scraper", "title": bilingual("弹性数据采集器", "Resilient scraper"),
     "summary": bilingual("静态 HTML/JSON/CSV 采集、限次重试、去重与来源追踪。",
                          "Static HTML/JSON/CSV ingestion, bounded retries, deduplication and provenance."),
     "modules": ["scripts/rapid_lead_scraper_resilience.py"]},
    {"id": "metering_gateway", "title": bilingual("API 计量网关", "API metering gateway"),
     "summary": bilingual("API 密钥鉴权、RPM/TPM 限流、SQLite 幂等计费与账单补偿。",
                          "API key authentication, RPM/TPM limits, SQLite idempotent billing and reconciliation."),
     "modules": ["scripts/b2b_api_gateway_metering.py", "scripts/zero_support_reconciliation_engine.py"]},
    {"id": "multi_agent_mesh", "title": bilingual("多智能体协作网格", "Multi-agent mesh"),
     "summary": bilingual("智能体路由、熔断与退避、CRDT 冲突合并和 Merkle 状态同步。",
                          "Agent routing, circuit breakers/backoff, CRDT conflict merging and Merkle state sync."),
     "modules": ["scripts/agent_fleet_load_balancer.py", "scripts/decentralized_state_sync_protocol.py"]},
)


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PortfolioError("expected finite, UTF-8 JSON data") from None


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _text(value, name, limit=240):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PortfolioError(f"{name} must be nonempty text of at most {limit} characters")
    value = unicodedata.normalize("NFC", value).strip()
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise PortfolioError(f"{name} must not contain control characters")
    return value


def _localized(value, name, limit=240):
    if not isinstance(value, dict) or set(value) != set(LANGUAGES):
        raise PortfolioError(f"{name} must contain zh and en text")
    return {lang: _text(value[lang], name, limit) for lang in LANGUAGES}


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise PortfolioError("input must be a bounded regular file without symlinks")
    try:
        content = path.read_bytes()
    except OSError:
        raise PortfolioError("cannot read input file") from None
    if len(content) > MAX_FILE_BYTES:
        raise PortfolioError("input exceeds the file size limit")
    return content


def load_json(path):
    """Read strict UTF-8 JSON; duplicate keys and nonfinite values are errors."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PortfolioError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise PortfolioError("nonfinite JSON number")

    try:
        return json.loads(_read(path).decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, PortfolioError):
            raise
        raise PortfolioError("invalid UTF-8 JSON input") from None


def _test_count(tree):
    # Count declarations at module/class scope, excluding nested helper functions.
    # Inheritance and parameterization may change the number of executed cases.
    return sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name.startswith("test_")
               for top in tree.body
               for node in (top.body if isinstance(top, ast.ClassDef) else [top]))


def scan_repository(repo_root):
    """Read scripts recursively without imports, execution or following symlinks."""
    root = Path(repo_root).absolute()
    scripts = root / "scripts"
    if root.is_symlink() or scripts.is_symlink() or not scripts.is_dir():
        raise PortfolioError("repository must contain a regular scripts directory")
    files = {}
    for directory, dirs, names in os.walk(scripts, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS
                         and not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if path.suffix != ".py" or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if not re.fullmatch(r"[A-Za-z0-9_./-]+\.py", relative):
                raise PortfolioError("Python evidence paths must use plain relative names")
            content = _read(path)
            try:
                tree = ast.parse(content, filename=relative)
            except (SyntaxError, ValueError, UnicodeError, RecursionError):
                raise PortfolioError(f"cannot inspect Python source: {relative}") from None
            is_test = name.startswith("test_")
            doc = (ast.get_docstring(tree) or "").splitlines()
            files[relative] = {"sha256": hashlib.sha256(content).hexdigest(),
                               "declared_tests": _test_count(tree) if is_test else 0,
                               "summary": doc[0][:240] if doc else ""}
    files = dict(sorted(files.items()))
    test_files = [name for name in files if Path(name).name.startswith("test_")]
    return {"files": files, "inventory": {
        "method": "static_ast", "tests_executed": False,
        "declared_test_functions": sum(files[name]["declared_tests"] for name in test_files),
        "test_files": len(test_files), "implementation_files": len(files) - len(test_files),
        "inventory_sha256": _digest({name: item["sha256"] for name, item in files.items()}),
    }}


def read_capability_profiles(repository, payload=None):
    """Resolve curated or supplied profiles against actual source/test pairs."""
    explicit = payload is not None
    if payload is None:
        payload = {"provider": bilingual("Yotoha 工程交付", "Yotoha Engineering"),
                   "capabilities": copy.deepcopy(list(DEFAULT_PROFILES))}
    if not isinstance(payload, dict):
        raise PortfolioError("profiles must be a JSON object")
    provider = _localized(payload.get("provider", bilingual("工程交付", "Engineering delivery")),
                          "provider", 80)
    records = payload.get("capabilities")
    if not isinstance(records, list) or not 1 <= len(records) <= 3:
        raise PortfolioError("capabilities must contain one to three profiles")
    capabilities, unavailable, seen = [], [], set()
    for record in records:
        if not isinstance(record, dict):
            raise PortfolioError("capability profile must be an object")
        identity = record.get("id")
        if not isinstance(identity, str) or identity not in PRICES_CNY or identity in seen:
            raise PortfolioError("capability id must be a distinct supported tier")
        seen.add(identity)
        title = _localized(record.get("title"), "capability title", 80)
        summary = _localized(record.get("summary"), "capability summary")
        modules = record.get("modules")
        if not isinstance(modules, list) or not 1 <= len(modules) <= 6:
            raise PortfolioError("modules must contain one to six implementation paths")
        evidence, missing, module_names = [], [], set()
        for module in modules:
            if (not isinstance(module, str)
                    or not re.fullmatch(r"scripts/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.py", module)
                    or Path(module).name.startswith("test_") or module in module_names):
                raise PortfolioError("modules must be distinct relative implementation paths in scripts/")
            module_names.add(module)
            test = str(Path(module).with_name("test_" + Path(module).name))
            files = repository["files"]
            if module not in files or test not in files or not files[test]["declared_tests"]:
                missing.append(module)
                continue
            evidence.append({"source": module, "source_sha256": files[module]["sha256"],
                             "source_summary": files[module]["summary"],
                             "tests": test, "tests_sha256": files[test]["sha256"],
                             "declared_test_functions": files[test]["declared_tests"]})
        if missing:
            if explicit:
                raise PortfolioError(f"profile {identity} lacks source/test evidence: {', '.join(missing)}")
            unavailable.append({"id": identity, "missing_modules": sorted(missing)})
            continue
        capabilities.append({"id": identity, "title": title, "summary": summary,
                             "profile_origin": "input_supplied" if explicit else "repository_curated",
                             "evidence": sorted(evidence, key=lambda item: item["source"]),
                             "declared_test_functions": sum(item["declared_test_functions"] for item in evidence)})
    if not capabilities:
        raise PortfolioError("no capability profiles backed by source/test pairs")
    order = list(PRICES_CNY)
    return provider, sorted(capabilities, key=lambda item: order.index(item["id"])), unavailable


def read_test_metrics(payload, inventory):
    """Validate an attributed run, without confusing static and executed totals."""
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise PortfolioError("test metrics must be a JSON object")
    counts = {}
    for key in ("total", "passed", "failed", "errors", "skipped", "expected_failures", "unexpected_successes"):
        value = payload.get(key, 0 if key in {"expected_failures", "unexpected_successes"} else None)
        if type(value) is not int or not (1 if key == "total" else 0) <= value <= 1_000_000_000:
            raise PortfolioError(f"{key} must be a bounded integer count")
        counts[key] = value
    if counts["total"] != sum(value for key, value in counts.items() if key != "total"):
        raise PortfolioError("test outcome counts must sum to total")
    binding = payload.get("inventory_sha256")
    if binding is not None and binding != inventory["inventory_sha256"]:
        raise PortfolioError("test metrics inventory hash does not match the repository")
    source = _text(payload.get("source"), "test metrics source", 240)
    rate = "100%" if counts["passed"] == counts["total"] else f"{counts['passed'] / counts['total'] * 100:.2f}%"
    if rate == "100.00%":
        rate = "<100%"
    return {**counts, "source": source, "status": "input_reported", "pass_rate": rate,
            "inventory_binding": "matched" if binding is not None else "unspecified",
            "metrics_sha256": _digest({**counts, "source": source, "inventory_sha256": binding})}


def _sla():
    return {
        "delivery_hours": 48, "acceptance_hours": 24, "support_calendar_days": 7,
        "response_business_hours": 4, "remediation_business_hours": 9,
        "included_revision_rounds": 1, "late_credit_percent_per_started_24h": 10,
        "late_credit_cap_percent": 30,
        "coverage": bilingual("周一至周五 09:00–18:00，Asia/Shanghai，按此工作时段计时。",
                              "Mon–Fri 09:00–18:00, Asia/Shanghai; business hours accrue only in this window."),
        "clock_start": bilingual("双方确认固定范围、验收样例与档期，资料/访问权限齐备且托管方确认全额入金后，48 个自然小时开始计时。",
                                 "48 elapsed hours start after scope, acceptance fixtures and booking are agreed, inputs/access are ready and the provider confirms full escrow funding."),
        "clock_pause": bilingual("客户资料或权限缺失、客户依赖服务中断时，记录原因与双方确认的暂停/恢复时间；变更范围另行报价。",
                                 "Missing client inputs/access or client dependency outages pause the clock with recorded, mutually agreed stop/resume times; scope changes require a new quote."),
        "acceptance": bilingual("交付后 24 个自然小时内按约定样例验收或列明缺陷；托管款仅在明确验收后释放，逾期不自动放款。",
                                "Review agreed fixtures within 24 elapsed hours of handoff or list defects; escrow releases only on explicit acceptance, with no automatic release on timeout."),
        "support": bilingual("交付后 7 个自然日内，范围内缺陷 4 个工作小时响应、9 个工作小时修复或提供可用替代方案；含一轮范围内修订。",
                             "For 7 calendar days after handoff: acknowledge in-scope defects within 4 business hours and fix or provide a working workaround within 9 business hours; one in-scope revision included."),
        "remedy": bilingual("供应方延迟交付每开始 24 小时抵扣服务费 10%，上限 30%；修复时限违约按同一比例抵扣，合计不超过 30%。",
                            "Supplier delivery delay earns 10% fee credit per started 24 hours, capped at 30%; missed remediation deadlines earn the same credit, with a combined 30% cap."),
    }


OFFER_DETAILS = {
    "scraper": {
        "scope": bilingual("1 个授权站点、最多 3 个静态页面模板、10 个字段；JSON/CSV 导出。",
                           "One authorized site, up to 3 static page templates and 10 fields; JSON/CSV export."),
        "acceptance": [bilingual("约定样例输出字段、去重结果和来源信息与预期一致。",
                                 "Agreed fixtures match expected fields, deduplication and provenance."),
                       bilingual("429/超时重试次数与等待上限测试通过；附可重复运行命令。",
                                 "429/timeout retry and maximum-wait tests pass; reproducible run command included.")],
        "exclusions": bilingual("不含 JS 渲染、验证码处理、付费数据采购与长期托管。",
                                "Excludes JS rendering, CAPTCHA handling, paid data acquisition and ongoing hosting."),
    },
    "metering_gateway": {
        "scope": bilingual("1 个上游 API、1 个 SQLite 账本、最多 3 档 RPM/TPM/预付配额配置。",
                           "One upstream API, one SQLite ledger, up to 3 RPM/TPM/prepaid quota configurations."),
        "acceptance": [bilingual("无效密钥拒绝；重复请求只扣费一次；并发请求不透支。",
                                 "Invalid keys are rejected; retries charge once; concurrent debits cannot overdraw."),
                       bilingual("重启后账本可复核；支付样例幂等入账与超期补偿测试通过。",
                                 "Ledger survives restart; idempotent payment fixtures and overdue compensation tests pass.")],
        "exclusions": bilingual("不含真实支付渠道开户、税务发票、跨区高可用与上游 API 费用。",
                                "Excludes payment-provider onboarding, tax invoicing, multi-region HA and upstream API fees."),
    },
    "multi_agent_mesh": {
        "scope": bilingual("单一运行环境中最多 3 个智能体/节点、1 条工作流；路由、熔断与状态同步演示。",
                           "Up to 3 agents/peers in one runtime environment and one workflow; routing, failover and state-sync demo."),
        "acceptance": [bilingual("故障节点触发熔断/路由切换；重试预算与退避符合配置。",
                                 "Failed peers trigger circuit breaking/rerouting; retry budgets and backoff match configuration."),
                       bilingual("断连重连与乱序/重复快照下，CRDT 合并及 Merkle 状态同步样例通过。",
                                 "CRDT merges and Merkle sync pass disconnect/reconnect and reordered/duplicate snapshot fixtures.")],
        "exclusions": bilingual("不含模型训练、模型调用费、公网节点运维与生产规模性能承诺。",
                                "Excludes model training, inference fees, public-node operations and production-scale performance commitments."),
    },
}


def calculate_escrow_hash(card):
    """SHA-256 of all card fields except the hash itself (canonical UTF-8 JSON)."""
    if not isinstance(card, dict):
        raise PortfolioError("deliverable card must be an object")
    return _digest({key: value for key, value in card.items() if key != "escrow_verification_hash"})


def verify_escrow_hash(card, expected_hash=None):
    """Check a card and, optionally, a separately retained original hash."""
    if not isinstance(card, dict):
        return False
    supplied = card.get("escrow_verification_hash")
    if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return False
    if expected_hash is not None and (not isinstance(expected_hash, str)
                                     or not hmac.compare_digest(supplied, expected_hash)):
        return False
    try:
        return hmac.compare_digest(supplied, calculate_escrow_hash(card))
    except PortfolioError:
        return False


def build_deliverable_card(capability):
    identity = capability["id"]
    card = {"schema_version": SCHEMA_VERSION, "id": identity,
            "title": copy.deepcopy(capability["title"]), "currency": "CNY",
            "price_cny": PRICES_CNY[identity], "delivery_hours": 48,
            **copy.deepcopy(OFFER_DETAILS[identity]), "sla": _sla(),
            "deliverables": [
                bilingual("完整源代码、配置样例与 README 启动/恢复手册。",
                          "Source code, example configuration and README startup/recovery runbook."),
                bilingual("自动化验收测试、样例结果与含文件 SHA-256 的交付清单。",
                          "Automated acceptance tests, fixture results and a file SHA-256 delivery manifest.")],
            "escrow_terms": {
                "deposit_percent": 100,
                "release": bilingual("由托管方确认全额入金，客户明确验收后放款；争议依托管平台流程处理。",
                                     "Escrow provider confirms full funding; release follows explicit client acceptance; disputes follow the provider's process."),
                "verification": bilingual("此哈希仅校验范围、价格、SLA 与证据是否改动，不证明托管款已到账。",
                                          "This hash checks scope, price, SLA and evidence integrity; it does not prove escrow funding.")},
            "evidence": copy.deepcopy(capability["evidence"]),
            "hash_scheme": "SHA-256; sorted keys; compact JSON; UTF-8; exclude escrow_verification_hash"}
    card["escrow_verification_hash"] = calculate_escrow_hash(card)
    return card


def synthesize_portfolio(repo_root=DEFAULT_REPO_ROOT, *, profiles=None, test_metrics=None):
    """Build a deterministic, JSON-compatible bilingual portfolio from evidence."""
    repository = scan_repository(repo_root)
    provider, capabilities, unavailable = read_capability_profiles(repository, profiles)
    portfolio = {"schema_version": SCHEMA_VERSION, "provider": provider,
                 "inventory": repository["inventory"],
                 "test_run": read_test_metrics(test_metrics, repository["inventory"]),
                 "capabilities": capabilities, "unavailable_tiers": unavailable,
                 "deliverable_cards": [build_deliverable_card(item) for item in capabilities],
                 "source_manifest": {name: item["sha256"] for name, item in repository["files"].items()}}
    portfolio["spec_sheets"] = {lang: format_spec_sheet(portfolio, lang) for lang in LANGUAGES}
    portfolio["outreach"] = {lang: _outreach(portfolio, lang) for lang in LANGUAGES}
    return portfolio


def _language(lang):
    if not isinstance(lang, str) or lang not in LANGUAGES:
        raise PortfolioError("language must be zh or en")


def _md(value):
    return re.sub(r"([\\`*_{}\[\]()#+!|>~])", r"\\\1", html.escape(str(value), quote=False))


def _metrics_statement(portfolio, lang):
    inventory, run = portfolio["inventory"], portfolio["test_run"]
    count, files = inventory["declared_test_functions"], inventory["test_files"]
    text = (f"静态盘点：{count:,} 个单元测试函数声明，分布于 {files} 个测试文件；此导出器未执行测试。" if lang == "zh" else
            f"Static inventory: {count:,} declared unit-test functions across {files} test files; the exporter did not execute them.")
    if run is None:
        return text + (" 未提供执行结果，通过率未验证。" if lang == "zh" else " No run supplied; pass rate is unverified.")
    outcomes = "/".join(str(run[key]) for key in ("failed", "errors", "skipped", "expected_failures", "unexpected_successes"))
    if lang == "zh":
        return (text + f" 输入报告：{run['passed']:,}/{run['total']:,} 通过（{run['pass_rate']}）；"
                f"失败/错误/跳过/预期失败/意外成功：{outcomes}；来源：{run['source']}。")
    return (text + f" Input-reported run: {run['passed']:,}/{run['total']:,} passed ({run['pass_rate']}); "
            f"failed/errors/skipped/expected failures/unexpected successes: {outcomes}; source: {run['source']}.")


def format_spec_sheet(portfolio, lang="zh"):
    """A compact, standalone Markdown sheet designed for a single printed page."""
    _language(lang)
    zh = lang == "zh"
    cards = portfolio["deliverable_cards"]
    sla = cards[0]["sla"]
    lines = [f"# {_md(portfolio['provider'][lang])} · " + ("CTO 技术能力规格表" if zh else "CTO capability spec sheet"), "",
             "固定范围工程交付 · 48 小时 · CNY 一口价；档期与书面范围确认后生效。" if zh else
             "Fixed-scope engineering · 48-hour delivery · fixed CNY fees; subject to booking and written scope agreement.", "",
             _md(_metrics_statement(portfolio, lang)), "",
             "| 能力 | 实现范围 | 测试声明 |" if zh else "| Capability | Implementation scope | Test declarations |",
             "| --- | --- | ---: |"]
    for item in portfolio["capabilities"]:
        lines.append(f"| {_md(item['title'][lang])} | {_md(item['summary'][lang])} | {item['declared_test_functions']} |")
    lines += ["", "| 交付包 | 固定价格 | 范围上限 |" if zh else "| Deliverable | Fixed fee | Scope limit |",
              "| --- | ---: | --- |"]
    for card in cards:
        lines.append(f"| {_md(card['title'][lang])} | ¥{card['price_cny']:,} | {_md(card['scope'][lang])} |")
    lines += ["", "**交付与 SLA**" if zh else "**Delivery & SLA**", "",
              f"- {_md(sla['clock_start'][lang])}", f"- {_md(sla['clock_pause'][lang])}",
              f"- {_md(sla['support'][lang])} {_md(sla['coverage'][lang])}",
              f"- {_md(sla['remedy'][lang])}", "",
              "**验收与托管**：" if zh else "**Acceptance & escrow**: "]
    lines[-1] += _md(sla["acceptance"][lang]) + " " + _md(cards[0]["escrow_terms"]["verification"][lang])
    lines += ["", ("交付含源代码、配置、启动/恢复文档、验收测试、样例与 SHA-256 文件清单；每张交付卡含可复核的条款哈希。" if zh else
                   "Handoff: source, configuration, startup/recovery docs, acceptance tests, fixtures and SHA-256 file manifest; each card includes a verifiable terms hash."), "",
              ("证据：" if zh else "Evidence: ") + "; ".join(
                  " / ".join(_md(e["source"]) for e in item["evidence"]) for item in portfolio["capabilities"]), "",
              "请提供目标、输入样例、环境限制与验收标准，以确认档期及固定范围。" if zh else
              "Share your objective, sample inputs, runtime constraints and acceptance criteria to confirm booking and scope."]
    return "\n".join(lines) + "\n"


def format_deliverable_cards(portfolio, lang="zh"):
    """Render full, independently reviewable terms for every available tier."""
    _language(lang)
    zh = lang == "zh"
    sections = []
    for card in portfolio["deliverable_cards"]:
        lines = [f"## {_md(card['title'][lang])} · ¥{card['price_cny']:,} CNY · 48 " + ("小时" if zh else "hours"), ""]
        for label, value in (
            ("固定范围" if zh else "Fixed scope", card["scope"][lang]),
            ("交付物" if zh else "Deliverables", " ".join(item[lang] for item in card["deliverables"])),
            ("验收标准" if zh else "Acceptance criteria", " ".join(item[lang] for item in card["acceptance"])),
            ("范围外" if zh else "Exclusions", card["exclusions"][lang]),
        ):
            lines += [f"**{label}**: {_md(value)}", ""]
        lines += ["**SLA**", ""]
        lines += [f"- {_md(card['sla'][key][lang])}" for key in
                  ("clock_start", "clock_pause", "acceptance", "support", "coverage", "remedy")]
        lines += ["", "**托管**: " if zh else "**Escrow**: "]
        lines[-1] += _md(card["escrow_terms"]["release"][lang]) + " " + _md(card["escrow_terms"]["verification"][lang])
        lines += ["", f"Escrow SHA-256: `{card['escrow_verification_hash']}`", "",
                  "校验：对 portfolio.json 中此卡删除 escrow_verification_hash 字段，按键排序、无多余空白的 UTF-8 JSON 计算 SHA-256，并与独立保存的哈希比较。" if zh else
                  "Verify: remove escrow_verification_hash from this card in portfolio.json; hash sorted-key, compact UTF-8 JSON with SHA-256 and compare against a separately saved hash.", "",
                  ("证据：" if zh else "Evidence: ") + "; ".join(
                      _md(item["source"]) + " + " + _md(item["tests"]) for item in card["evidence"])]
        sections.append("\n".join(lines))
    return "\n\n---\n\n".join(sections) + "\n"


def _outreach(portfolio, lang):
    zh = lang == "zh"
    offers = "；".join(f"{card['title'][lang]} ¥{card['price_cny']:,}" for card in portfolio["deliverable_cards"])
    body = (("您好，我们提供可验收的固定范围工程交付：" if zh else "Hello, we offer verifiable fixed-scope engineering deliveries: ")
            + offers + ". " + _metrics_statement(portfolio, lang) + " "
            + ("资料、书面范围、档期与全额托管入金确认后 48 小时交付；含 7 日缺陷支持和条款 SHA-256 校验。请提供输入样例与验收目标，以确认适配范围。" if zh else
               "Delivery is 48 hours after inputs, written scope, booking and full escrow funding are confirmed; includes 7-day defect support and a SHA-256 terms hash. Share sample inputs and acceptance goals to confirm fit."))
    return {"subject": ("48 小时固定范围技术交付 | " if zh else "48-hour fixed-scope engineering | ") + portfolio["provider"][lang],
            "body": body}


def format_outreach(portfolio, lang="zh"):
    _language(lang)
    message = portfolio["outreach"][lang]
    return f"{_md(message['subject'])}\n\n{_md(message['body'])}\n"


def format_markdown(portfolio, lang="zh", kind="all"):
    _language(lang)
    formatters = {"spec": format_spec_sheet, "cards": format_deliverable_cards, "outreach": format_outreach}
    if kind in formatters:
        return formatters[kind](portfolio, lang)
    if kind != "all":
        raise PortfolioError("kind must be spec, cards, outreach or all")
    return "\n---\n\n".join(formatter(portfolio, lang) for formatter in formatters.values())


def format_json(portfolio):
    _canonical(portfolio)
    return json.dumps(portfolio, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def export_bundle(portfolio, output):
    """Create seven UTF-8 artifacts; refuse existing directories and files."""
    artifacts = {"portfolio.json": format_json(portfolio)}
    for lang in LANGUAGES:
        artifacts[f"capability-spec.{lang}.md"] = format_spec_sheet(portfolio, lang)
        artifacts[f"deliverable-cards.{lang}.md"] = format_deliverable_cards(portfolio, lang)
        artifacts[f"outreach.{lang}.md"] = format_outreach(portfolio, lang)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir()
    except FileExistsError:
        raise PortfolioError("output already exists; choose a new directory") from None
    try:
        for name, content in artifacts.items():
            with (output / name).open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
    except BaseException:
        shutil.rmtree(output)
        raise
    return sorted(artifacts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--profiles", type=Path, help="Bilingual capability profile JSON")
    parser.add_argument("--metrics", type=Path, help="Attributed test-run metrics JSON")
    parser.add_argument("--lang", choices=LANGUAGES, default="zh")
    parser.add_argument("--kind", choices=("spec", "cards", "outreach", "all"), default="all")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, help="New directory for all seven bilingual artifacts")
    args = parser.parse_args(argv)
    try:
        portfolio = synthesize_portfolio(args.repo,
                                         profiles=load_json(args.profiles) if args.profiles else None,
                                         test_metrics=load_json(args.metrics) if args.metrics else None)
        if args.output is not None:
            names = export_bundle(portfolio, args.output)
            print(json.dumps({"output": str(args.output), "files": names}, ensure_ascii=False))
        else:
            print(format_json(portfolio) if args.format == "json" else
                  format_markdown(portfolio, args.lang, args.kind), end="")
    except (PortfolioError, OSError) as exc:
        print(f"portfolio export error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
