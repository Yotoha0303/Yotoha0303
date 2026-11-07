#!/usr/bin/env python3
"""Render three evidence-backed offers for V2EX, Eleduck, GitHub, Xianyu and CLI.

Python 3.10+, standard library only; no network calls or publishing. Prices,
scope, exclusions, acceptance criteria and SLA come from the commercial portfolio
exporter. Its capability IDs are mapped to the public PKG-01/02/03 names here.
Markdown uses each site's supported layout; terminal cards have exact cell widths
(wide East Asian characters included). Website fonts control the final pixels.

    python3 scripts/cross_platform_portfolio_card_renderer.py
    python3 scripts/cross_platform_portfolio_card_renderer.py --platform terminal
    python3 scripts/cross_platform_portfolio_card_renderer.py --platform xianyu --package PKG-01
    python3 scripts/cross_platform_portfolio_card_renderer.py --payment-links payments.json --output /tmp/cards

Payment JSON maps PKG-01, PKG-02 and/or PKG-03 to HTTPS checkout/escrow URLs.
Absent URLs link to bundled payment instructions; no checkout is fabricated.
Exports contain five combined cards, fifteen individual cards, source terms,
payment instructions, a usage README and a SHA-256 manifest. Existing output
directories are refused. All text is UTF-8 with LF endings.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sys
import unicodedata
from urllib.parse import quote, unquote, urlsplit

if __package__:
    from . import commercial_portfolio_spec_exporter as exporter
    from .phalanx_base import PhalanxDB, CircuitBreaker, ExponentialBackoff, GoldenFixtureLoader
else:
    import commercial_portfolio_spec_exporter as exporter
    from phalanx_base import PhalanxDB, CircuitBreaker, ExponentialBackoff, GoldenFixtureLoader

# Infrastructure is supplied by phalanx_base. Rendering needs neither a database
# nor network retries; do not create a second persistence or resilience stack.
DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = DEFAULT_REPO_ROOT / "docs" / "portfolio_cards"
DEFAULT_REPOSITORY_URL = "https://github.com/Yotoha0303/Yotoha0303"
PACKAGE_DEFINITIONS = (
    ("scraper", "PKG-01", "Web Harvester", "网页数据采集"),
    ("metering_gateway", "PKG-02", "Zero-Support Gateway", "接口计量网关"),
    ("multi_agent_mesh", "PKG-03", "Agent Mesh", "智能体协作网格"),
)
PLATFORMS = ("v2ex", "eleduck", "github", "xianyu", "terminal")
EXTENSIONS = {"v2ex": "md", "eleduck": "md", "github": "md", "xianyu": "txt", "terminal": "ansi"}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
XIANYU_BANNED = ("微信", "电话", "v", "淘宝")
SLA_FIELDS = ("clock_start", "clock_pause", "acceptance", "support", "coverage", "remedy")


class CardRenderError(ValueError):
    """Input cannot be rendered without losing the offer's meaning or safety."""


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise CardRenderError(f"{name} must be nonempty text of at most 4000 characters")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise CardRenderError(f"{name} must not contain control characters")
    return unicodedata.normalize("NFC", value).strip()


def _localized(value, name):
    if not isinstance(value, dict) or set(value) != {"zh", "en"}:
        raise CardRenderError(f"{name} must contain zh and en")
    for language in ("zh", "en"):
        _text(value[language], name)


def _https_url(value):
    value = _text(value, "URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise CardRenderError("URL must be an absolute HTTPS URL") from None
    decoded = unquote(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or port == 0
            or any(char.isspace() or unicodedata.category(char).startswith("C")
                   or char in '<>"\\' for char in decoded)):
        raise CardRenderError("URL must be an absolute HTTPS URL without credentials or controls")
    # Parentheses and brackets cannot break Markdown link destinations.
    return quote(value, safe=":/?#@!$&'*+,;=%-._~")


def _md(value):
    return re.sub(r"([\\`*_{}\[\]()#+!|>~])", r"\\\1", html.escape(value, quote=False))


@dataclass(frozen=True)
class CommercialPackage:
    source_id: str
    package_id: str
    name: str
    title_zh: str
    offer: dict
    summary: dict

    @property
    def price_cny(self):
        return self.offer["price_cny"]


def load_packages(repo_root=DEFAULT_REPO_ROOT, *, portfolio=None):
    """Load and verify exactly the exporter's three offers, in canonical order.

    An optional previously synthesized portfolio supports offline snapshots.
    Terms hashes verify integrity, not payment funding or test execution.
    """
    if portfolio is None:
        portfolio = exporter.synthesize_portfolio(repo_root)
    if not isinstance(portfolio, dict) or portfolio.get("schema_version") != exporter.SCHEMA_VERSION:
        raise CardRenderError("unsupported portfolio schema")
    _localized(portfolio.get("provider"), "provider")
    cards = portfolio.get("deliverable_cards")
    capabilities = portfolio.get("capabilities")
    expected = {item[0] for item in PACKAGE_DEFINITIONS}
    for records in (cards, capabilities):
        if (not isinstance(records, list) or len(records) != 3
                or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                       for item in records)
                or {item["id"] for item in records} != expected):
            raise CardRenderError("portfolio must contain all three distinct commercial packages")
    by_id = {item["id"]: item for item in cards}
    profiles = {item["id"]: item for item in capabilities}
    packages = []
    for source_id, package_id, name, title_zh in PACKAGE_DEFINITIONS:
        card = by_id[source_id]
        if not exporter.verify_escrow_hash(card):
            raise CardRenderError(f"{package_id} terms hash mismatch")
        if (card.get("currency") != "CNY" or type(card.get("price_cny")) is not int
                or not 1 <= card["price_cny"] <= 1_000_000_000
                or type(card.get("delivery_hours")) is not int or card["delivery_hours"] != 48):
            raise CardRenderError(f"{package_id} requires a positive CNY fee and 48-hour offer")
        for key in ("title", "scope", "exclusions"):
            _localized(card.get(key), key)
        for key in ("deliverables", "acceptance"):
            if not isinstance(card.get(key), list) or not card[key]:
                raise CardRenderError(f"{key} must be a nonempty list")
            for item in card[key]:
                _localized(item, key)
        sla = card.get("sla")
        escrow = card.get("escrow_terms")
        if not isinstance(sla, dict) or not isinstance(escrow, dict):
            raise CardRenderError("missing SLA or escrow terms")
        for key in SLA_FIELDS:
            _localized(sla.get(key), key)
        if sla.get("delivery_hours") != card["delivery_hours"]:
            raise CardRenderError("inconsistent delivery SLA")
        for key in ("release", "verification"):
            _localized(escrow.get(key), key)
        evidence = card.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise CardRenderError("missing source evidence")
        for item in evidence:
            if (not isinstance(item, dict) or not isinstance(item.get("source"), str)
                    or not re.fullmatch(r"scripts/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.py", item["source"])):
                raise CardRenderError("invalid source evidence path")
        summary = profiles[source_id].get("summary")
        _localized(summary, "summary")
        packages.append(CommercialPackage(source_id, package_id, name, title_zh,
                                          copy.deepcopy(card), copy.deepcopy(summary)))
    return tuple(packages)


def _xianyu_text(value):
    """Use platform-native guidance, with no external contact or payment routes."""
    value = unicodedata.normalize("NFKC", html.unescape(value))
    value = re.sub(r"(?:https?://|www\.)\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "站内沟通", value, flags=re.I)
    value = re.sub(r"(?<!\d)\+?\d[\d ()-]{5,}\d(?!\d)", "站内沟通", value)
    for term, replacement in (("CSV", "表格数据"), ("README", "使用说明"),
                              ("微信", "站内沟通"), ("电话", "站内沟通"),
                              ("淘宝", "本平台"), ("v", "站内沟通")):
        value = re.sub(re.escape(term), replacement, value, flags=re.I)
    return re.sub(r"[\[\]<>`*_#|]", "", value)


def validate_xianyu_card(content):
    normalized = unicodedata.normalize("NFKC", html.unescape(content)).casefold()
    if (any(term in normalized for term in XIANYU_BANNED)
            or re.search(r"https?://|www\.|@|\x1b|[\[\]<>`*_#|]", normalized)):
        raise CardRenderError("Xianyu card contains prohibited text or external guidance")


def display_width(value):
    """Terminal cells for the text emitted here (ANSI SGR is zero-width)."""
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
               for char in ANSI_RE.sub("", value))


def _wrap_cells(value, width):
    line, cells = "", 0
    for char in value:
        size = display_width(char)
        if cells + size > width:
            boundary = line.rfind(" ")
            if boundary > 0:
                yield line[:boundary]
                line = line[boundary + 1:]
                cells = display_width(line)
            else:
                yield line
                line, cells = "", 0
        line += char
        cells += size
    if line:
        yield line


class PortfolioCardRenderer:
    """Deterministic, channel-specific presentations of the same source offers."""

    def __init__(self, repo_root=DEFAULT_REPO_ROOT, *, portfolio=None, payment_links=None,
                 terminal_width=88, color=True, repository_url=DEFAULT_REPOSITORY_URL):
        self.portfolio = copy.deepcopy(portfolio if portfolio is not None else
                                       exporter.synthesize_portfolio(repo_root))
        self.packages = load_packages(portfolio=self.portfolio)
        self.provider = self.portfolio["provider"]
        if type(terminal_width) is not int or not 60 <= terminal_width <= 160:
            raise CardRenderError("terminal width must be an integer from 60 to 160")
        self.terminal_width = terminal_width
        self.color = color
        self.repository_url = _https_url(repository_url).rstrip("/")
        if urlsplit(self.repository_url).query or urlsplit(self.repository_url).fragment:
            raise CardRenderError("repository URL must not contain a query or fragment")
        if payment_links is None:
            payment_links = {}
        if not isinstance(payment_links, dict) or any(key not in {p.package_id for p in self.packages}
                                                     for key in payment_links):
            raise CardRenderError("payment links must map supported package IDs to HTTPS URLs")
        self.payment_links = {key: _https_url(value) for key, value in payment_links.items()}

    def _selection(self, package_id):
        if package_id is None:
            return self.packages
        for package in self.packages:
            if package.package_id == package_id:
                return (package,)
        raise CardRenderError("unknown package ID; expected PKG-01, PKG-02 or PKG-03")

    def _payment(self, package, prefix):
        return self.payment_links.get(package.package_id,
                                      f"{prefix}payments.md#{package.package_id.lower()}")

    def _markdown_package(self, package, prefix, *, payment=False):
        card = package.offer
        lines = [f"### {package.package_id} · {package.name} · ¥{package.price_cny:,} CNY", "",
                 _md(package.summary["zh"]), ""]
        for label, value in (("固定范围", card["scope"]["zh"]),
                             ("交付物", " ".join(item["zh"] for item in card["deliverables"])),
                             ("验收标准", " ".join(item["zh"] for item in card["acceptance"])),
                             ("范围外", card["exclusions"]["zh"])):
            lines.append(f"- **{label}**：{_md(value)}")
        evidence = " · ".join(f"[源码 {index}]({self.repository_url}/blob/main/{item['source']})"
                              for index, item in enumerate(card["evidence"], 1))
        lines += [f"- **工程证据**：{evidence}",
                  f"- **条款 SHA-256**：`{card['escrow_verification_hash']}`"]
        if payment:
            label = "托管付款" if package.package_id in self.payment_links else "托管付款说明"
            lines += ["", f"[{label}]({self._payment(package, prefix)})"]
        return lines

    def _markdown_terms(self, packages, prefix):
        lines = ["## 交付与合作约定", ""]
        # Usually shared; keep distinct clauses if supplied offers differ.
        seen = set()
        for package in packages:
            card = package.offer
            clauses = [card["sla"][key]["zh"] for key in SLA_FIELDS]
            clauses += [card["escrow_terms"][key]["zh"] for key in ("release", "verification")]
            for clause in clauses:
                if clause not in seen:
                    lines.append(f"- {_md(clause)}")
                    seen.add(clause)
        lines += ["", f"[完整双语规格与原始条款]({prefix}portfolio.json)"]
        return lines

    def _v2ex(self, packages, prefix):
        lines = [f"# {_md(self.provider['zh'])}｜固定范围工程交付", "",
                 "> 数据采集 · 接口计量 · 智能体协作｜人民币固定报价｜48 小时交付，起算条件见下方约定。", ""]
        for package in packages:
            lines += self._markdown_package(package, prefix, payment=True) + ["", "---", ""]
        lines += self._markdown_terms(packages, prefix)
        lines += ["", "请通过站内消息提供目标、输入样例、环境限制与验收标准；确认范围、档期及托管条款后付款。"]
        return lines

    def _eleduck(self, packages, prefix):
        lines = [f"# {_md(self.provider['zh'])}｜远程开发者", "",
                 "**合作方向**：Python 自动化 / API 可靠性 / 多智能体工作流", "",
                 "**协作方式**：远程、异步沟通；按固定范围交付源代码、测试与运行文档。", "",
                 "**需求对齐**：目标 → 输入样例 → 范围确认 → 样例验收 → 交付与缺陷支持。", "",
                 "## 可承接的项目", ""]
        for package in packages:
            lines += self._markdown_package(package, prefix) + [""]
        lines += self._markdown_terms(packages, prefix)
        lines += ["", "请通过电鸭站内私信提供项目背景、预算、期望档期和验收样例，先确认合作匹配度。"]
        return lines

    def _github(self, packages, prefix):
        lines = ['<div align="center">', "",
                 f"<h1>{html.escape(self.provider['en'])}</h1>", "",
                 "<p>Data ingestion · API reliability · Agent orchestration</p>", "",
                 "<p>Fixed scope · Source + tests + runbook · CNY pricing</p>", "", "</div>", "",
                 "## Engineering packages", "",
                 "| Package | Fixed fee | Delivery |", "| :--- | ---: | :--- |"]
        for package in packages:
            lines.append(f"| {package.package_id} · {package.name} | ¥{package.price_cny:,} CNY | 48h, subject to agreed start conditions |")
        lines += ["", "Source and test files are available for review. Acceptance criteria describe the agreed handoff; no test pass rate is inferred.", ""]
        for package in packages:
            lines += self._markdown_package(package, prefix) + [""]
        lines += self._markdown_terms(packages, prefix)
        lines += ["", f"[Discuss a scoped project]({self.repository_url}/issues/new)"]
        return lines

    def _xianyu(self, packages, prefix):
        lines = ["软件开发服务 · 固定范围 · 样例验收", "", "按约定范围交付源码、配置、测试与使用说明。", ""]
        seen = set()
        for package in packages:
            card = package.offer
            lines += [f"{package.package_id} {package.title_zh}", f"固定报价：{package.price_cny:,} 元人民币",
                      f"服务内容：{card['scope']['zh']}",
                      "交付清单：" + " ".join(item["zh"] for item in card["deliverables"]),
                      "验收标准：" + " ".join(item["zh"] for item in card["acceptance"]),
                      f"范围说明：{card['exclusions']['zh']}", ""]
            for key in SLA_FIELDS:
                seen.add(card["sla"][key]["zh"])
        # Preserve source order, including differing SLAs, without set iteration.
        lines += ["交付与售后约定"]
        for package in packages:
            for key in SLA_FIELDS:
                clause = package.offer["sla"][key]["zh"]
                if clause in seen:
                    lines.append(clause)
                    seen.remove(clause)
        lines += ["", "请使用本平台聊天发送需求、输入样例与验收标准，先确认范围、档期和订单条款。",
                  "仅通过本平台下单、付款和验收；售后与争议按本平台订单流程处理。"]
        content = "\n".join(_xianyu_text(line) for line in lines) + "\n"
        validate_xianyu_card(content)
        return content

    def _terminal(self, packages, prefix):
        width = self.terminal_width
        inner = width - 4
        lines = []

        def emit(value, style=""):
            lines.append(f"\x1b[{style}m{value}\x1b[0m" if self.color and style else value)

        def row(value, style=""):
            for part in _wrap_cells(value, inner):
                emit("│ " + part + " " * (inner - display_width(part)) + " │", style)

        emit("┌" + "─" * (width - 2) + "┐", "36")
        row(self.provider["en"], "1;36")
        row("FIXED-SCOPE ENGINEERING | CNY | SOURCE + TESTS + RUNBOOK")
        for package in packages:
            card = package.offer
            emit("├" + "─" * (width - 2) + "┤", "36")
            row(f"{package.package_id}  {package.name}", "1;36")
            row(f"CNY {package.price_cny:,} | 48h after agreed start conditions", "1;32")
            row("Scope: " + card["scope"]["en"])
            row("Handoff: " + " ".join(item["en"] for item in card["deliverables"]))
            row("Acceptance: " + " ".join(item["en"] for item in card["acceptance"]))
            row("Excludes: " + card["exclusions"]["en"])
            row("Terms SHA-256: " + card["escrow_verification_hash"])
        emit("├" + "─" * (width - 2) + "┤", "36")
        seen = set()
        for package in packages:
            clauses = [package.offer["sla"][key]["en"] for key in SLA_FIELDS]
            clauses += [package.offer["escrow_terms"][key]["en"] for key in ("release", "verification")]
            for clause in clauses:
                if clause not in seen:
                    row(clause)
                    seen.add(clause)
        row("Discuss scope and sample inputs before booking.")
        emit("└" + "─" * (width - 2) + "┘", "36")
        return lines

    def render(self, platform, package_id=None):
        """Render a combined profile, or one package, ending in exactly one LF."""
        if not isinstance(platform, str) or platform not in PLATFORMS:
            raise CardRenderError("unknown platform; expected " + ", ".join(PLATFORMS))
        packages = self._selection(package_id)
        prefix = "../" if package_id else ""
        result = getattr(self, "_" + platform)(packages, prefix)
        return result if isinstance(result, str) else "\n".join(result).rstrip() + "\n"

    def render_all(self):
        artifacts = {}
        for platform in PLATFORMS:
            extension = EXTENSIONS[platform]
            artifacts[f"{platform}.{extension}"] = self.render(platform)
            for package in self.packages:
                artifacts[f"{platform}/{package.package_id}.{extension}"] = self.render(platform, package.package_id)
        return artifacts


def _payment_instructions(renderer):
    lines = ["# 托管付款说明", "", "先确认固定范围、验收样例、档期与订单条款，再通过双方确认的托管渠道付款。", ""]
    for package in renderer.packages:
        lines += [f"## {package.package_id}", "", f"{package.name} · ¥{package.price_cny:,} CNY", ""]
        if package.package_id in renderer.payment_links:
            lines += [f"[打开托管付款页面]({renderer.payment_links[package.package_id]})", ""]
        else:
            lines += ["尚未配置收款链接。请在发布渠道通过站内消息确认范围和订单，由服务方提供已确认的托管付款页面。", ""]
    lines += ["完整约定见 [原始双语规格](portfolio.json)。条款哈希不证明托管款已到账。"]
    return "\n".join(lines) + "\n"


def export_cards(renderer, output=DEFAULT_OUTPUT):
    """Create a reviewable bundle; clean up partial writes and preserve existing data."""
    artifacts = renderer.render_all()
    artifacts["portfolio.json"] = exporter.format_json(renderer.portfolio)
    artifacts["payments.md"] = _payment_instructions(renderer)
    artifacts["README.md"] = "\n".join([
        "# Portfolio cards", "",
        "Generated from commercial_portfolio_spec_exporter.py; source terms are in portfolio.json.", "",
        "- v2ex.md: rich Markdown with package payment links.",
        "- eleduck.md: remote developer profile and scoped offers.",
        "- github.md: pasteable GitHub Profile README hero and package section.",
        "- xianyu.txt: plain text with platform-native ordering and support guidance.",
        "- terminal.ansi: ANSI showcase; run `cat terminal.ansi`.",
        "- Each platform directory contains PKG-01, PKG-02 and PKG-03 separately.", "",
        "Local Markdown links assume the bundle layout. When pasting into a website, host portfolio.json",
        "and payments.md and update those links. HTTPS payment links supplied via --payment-links",
        "are already absolute. Unconfigured payment links open instructions, not a checkout.", "",
        "Regenerate into a new directory with:", "",
        "    python3 scripts/cross_platform_portfolio_card_renderer.py --output /tmp/portfolio-cards", "",
        "Payment JSON example (replace with your confirmed HTTPS destinations):", "",
        '    {"PKG-01": "https://your-payment-host/confirmed-order"}', "",
        "The renderer does not publish cards, process payments, or execute acceptance tests.",
        "Markdown pixels depend on the host font; terminal geometry uses Unicode display cells.",
        "manifest.json hashes every other artifact as UTF-8 bytes with LF endings.", "",
    ])
    manifest = {
        "schema_version": "1.0",
        "packages": [{"id": p.package_id, "name": p.name, "source_id": p.source_id,
                      "price_cny": p.price_cny, "terms_sha256": p.offer["escrow_verification_hash"]}
                     for p in renderer.packages],
        "payment_links_configured": sorted(renderer.payment_links),
        "artifacts": {name: {"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                             "bytes": len(content.encode("utf-8"))}
                      for name, content in sorted(artifacts.items())},
    }
    artifacts["manifest.json"] = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir()
    except FileExistsError:
        raise CardRenderError("output already exists; choose a new directory") from None
    try:
        for name, content in sorted(artifacts.items()):
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
    except BaseException:
        shutil.rmtree(output)
        raise
    return sorted(artifacts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--portfolio", type=Path, help="Previously exported portfolio JSON instead of scanning")
    parser.add_argument("--output", type=Path, help="New bundle directory; default: docs/portfolio_cards/")
    parser.add_argument("--platform", choices=PLATFORMS, help="Preview one platform on stdout instead of exporting")
    parser.add_argument("--package", choices=[item[1] for item in PACKAGE_DEFINITIONS], dest="package_id")
    parser.add_argument("--payment-links", type=Path, help="JSON object: package ID to HTTPS checkout/escrow URL")
    parser.add_argument("--repository-url", default=DEFAULT_REPOSITORY_URL)
    parser.add_argument("--width", type=int, default=88, help="Terminal card width, 60–160 cells")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color in terminal cards")
    args = parser.parse_args(argv)
    if args.package_id and not args.platform:
        parser.error("--package requires --platform")
    if args.platform and args.output:
        parser.error("--platform previews stdout and cannot be combined with --output")
    try:
        renderer = PortfolioCardRenderer(
            args.repo, portfolio=exporter.load_json(args.portfolio) if args.portfolio else None,
            payment_links=exporter.load_json(args.payment_links) if args.payment_links else None,
            terminal_width=args.width, color=not args.no_color, repository_url=args.repository_url)
        if args.platform:
            print(renderer.render(args.platform, args.package_id), end="")
        else:
            output = args.output if args.output is not None else DEFAULT_OUTPUT
            print(json.dumps({"output": str(output), "files": export_cards(renderer, output)}, ensure_ascii=False))
    except (CardRenderError, exporter.PortfolioError, OSError) as exc:
        print(f"portfolio card error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
