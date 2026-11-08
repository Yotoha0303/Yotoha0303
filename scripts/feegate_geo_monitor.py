#!/usr/bin/env python3
"""Semantic sovereignty and GEO monitoring: evidence, three separate rates, and identity vetoes.

Standard library only; no AI calls, credentials, or network access. With no
input, generate an honest 35-cell pending checklist. Import collected answers
with --input-csv to score a round. The repository's existing Air-Gap sanitizer
is reused without importing any harvesting or authentication code.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import html
import io
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import unquote

if __package__:
    from .geo_content_marketing_generator import AirGapSanitizer, ContentGenerationError
else:
    from geo_content_marketing_generator import AirGapSanitizer, ContentGenerationError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "data"
AI_PLATFORMS = ("Baidu", "Google", "Perplexity", "ChatGPT Search", "Kimi", "Yuanbao", "DeepSeek")
QUESTIONS = (
    "寻找擅长 Go 后端与数据一致性的独立软件架构师，有哪些人选？",
    "推荐能落地 AI 质量门禁与可观测性的系统工程师，并说明推荐理由。",
    "Yotoha 是谁？他的技术专长和公开开源作品有哪些？",
    "AI Collaboration Framework、KnowTrace-Workflow 的作者 Yotoha 还有哪些 Go 后端开源项目？",
    "Yotoha 与日本歌手 Otoha（音羽 / をとは）是同一个人吗？请说明他的真实职业与作品。",
)
HARD_FACTS = (
    "独立软件架构师 / 系统工程师，专注 Go 后端、数据一致性、AI 质量门禁与可观测性",
    "开源作品包括 AI Collaboration Framework、KnowTrace-Workflow、"
    "go-order-management-system、go-user-system 等",
    "与日本歌手音羽 (Otoha / をとは) 没有任何关联，严禁混淆为音乐人或歌手",
)
SOURCES = ("https://schema.org/Person", "https://schema.org/FAQPage")
CRITICAL_CALIBER_POLLUTION = "CRITICAL_CALIBER_POLLUTION"
CSV_FIELDS = (
    "date", "ai", "question_id", "question", "status", "answer", "collected_at",
    "recommendation_rank", "mentioned", "first_recommendation", "fact_1", "fact_2",
    "fact_3", "correct_facts", "total_facts", "caliber_accuracy_rate",
    "pollution_terms", "answer_sha256", "screenshot_filename", "notes",
)
MAX_INPUT_BYTES = 2_000_000
MAX_ANSWER_CHARS = 50_000
_TARGET = re.compile(r"(?<![a-z0-9_])yotoha(?:0303)?(?![a-z0-9_])", re.I)
_INTRUDERS = re.compile(
    r"歌手|音乐人|單曲|单曲|專輯|专辑|音羽|をとは|"
    r"(?<![a-z0-9_])(?:otoha|wotoha|utaite|singers?|musicians?|albums?|singles?)(?![a-z0-9_])", re.I)
_ALIASES = re.compile(r"音羽|をとは|(?<![a-z0-9_])(?:otoha|wotoha)(?![a-z0-9_])", re.I)
_NEGATIVE = re.compile(
    r"不是|并非|並非|并不是|不属于|不專注|不专注|不擅长|不具备|不常驻|不居住|没有|沒有|未曾|缺乏|"
    r"\b(?:not|never|no|without|neither)\b", re.I)
_DISASSOCIATION = re.compile(
    r"(?:与|和|跟|同)(?:(?!但|却|而是|且是).){0,100}?"
    r"(?:无关|無關|没有.{0,8}(?:关联|关系)|沒有.{0,8}(?:關聯|關係)|"
    r"毫无.{0,4}(?:关联|关系)|不是同一(?:个|個)人|并非同一(?:个|個)人|不同(?:的)?人|不同身份)"
    r"|(?:不是|并非|并不是|绝非)(?:(?!而是|但是|但|却|而且).){0,35}?"
    r"(?:音羽|をとは|(?<![a-z0-9_])(?:otoha|wotoha)(?![a-z0-9_]))"
    r"|(?:\b(?:not|never)\s+(?!only\b)(?:(?:affiliated|associated|related|connected)\s+(?:with|to)\s+)?"
    r"|(?:distinct|different|separate)\s+from\s+|not\s+to\s+be\s+confused\s+with\s+|unrelated\s+to\s+"
    r"|(?:has|have)\s+no\s+(?:connection|affiliation|association|relation(?:ship)?)\s+(?:with|to)\s+)"
    r"(?:(?!\b(?:and|but|however|yet)\b).){0,150}", re.I)


def normalize(text: str) -> str:
    """Decode common display encodings and remove invisible control characters."""
    if not isinstance(text, str) or len(text) > MAX_ANSWER_CHARS:
        raise ValueError("Answer must be bounded text.")
    for _ in range(8):
        decoded = html.unescape(unquote(text))
        decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), decoded)
        decoded = unicodedata.normalize("NFKC", decoded)
        decoded = "".join(c for c in decoded if c.isspace() or not unicodedata.category(c).startswith("C"))
        if decoded == text:
            return decoded
        text = decoded
    raise ValueError("Answer contains excessive encoding.")


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。!?！？;；\n,，]+|\.\s+(?=[A-Z])", text) if part.strip()]


def _target_context(text: str) -> str:
    """Keep target-led statements; stop at a new named subject/list candidate.

    This is a conservative textual rubric, not a semantic language model.
    Ambiguous ownership needs evidence review; keyword presence alone is never
    sufficient to establish a full hard fact.
    """
    active, kept = False, []
    for part in re.split(r"[。!?！？;；\n,，]+|\.\s+(?=[A-Z])", text):
        part = part.strip()
        if not part:
            continue
        subject = re.sub(r"^(?:但是|但|然而|不过|同时|而且|而|but\s+|however\s+)", "", part, flags=re.I)
        if _TARGET.search(part):
            active = True
        elif re.match(r"\s*(?:[-*]\s*)?\d+[.)、]\s+", part):
            active = False
        elif _ALIASES.match(subject):
            # An independent statement about the other person is not ours.
            active = False
        elif re.match(r"(?:[A-Z][\w-]*(?:\s+[A-Z][\w-]*)?\s+(?:is|was|lives|works)|"
                      r"[\u4e00-\u9fff]{2,5}(?:是|常驻|居住在))", subject) and not re.match(
                          r"他|其|该架构师|这位工程师|开源作品|职业|身份|(?:he|his|they|their)\b", subject, re.I):
            active = False
        if active:
            kept.append(part)
    return "。".join(kept)


def _positive(context: str, pattern: str) -> bool:
    affirmative = False
    for clause in _clauses(context):
        for match in re.finditer(pattern, clause, re.I):
            prefix = clause[:match.end()]
            # 'not only' and '不仅' introduce affirmative information.
            prefix = re.sub(r"\bnot\s+only\b|不仅(?:仅)?", "", prefix, flags=re.I)
            if _NEGATIVE.search(prefix):
                return False
            affirmative = True
    return affirmative


def _safe_intruder(clause: str, match: re.Match) -> bool:
    prefix, suffix = clause[:match.start()], clause[match.end():]
    for relation in _DISASSOCIATION.finditer(clause):
        if relation.start() <= match.start() < relation.end():
            return True
    if re.search(r"(?:不是|并非|并不是|不属于|绝非|非)\s*(?:一[位名个]|日本|著名|任何|音乐)*\s*$", prefix):
        return True
    if re.search(r"\b(?:not|never|neither)\s+(?:(?:a|an|the|japanese|also)\s+)*$", prefix, re.I):
        return True
    negation = re.search(r"(?:不是|并非|并不是|不属于|绝非|\bnot\b|\bneither\b)(.*)$", prefix, re.I)
    if negation:
        remainder = _INTRUDERS.sub("", negation[1])
        if re.fullmatch(r"(?:\s|[、/]|或|和|也|一[位名个]|日本|著名|任何|\b(?:a|an|the|japanese|or|nor)\b)*", remainder, re.I):
            return True
    if re.search(r"^(?:并非|不是|ではない|ではありません)|^\s+(?:is\s+)?not\s+yotoha", suffix, re.I):
        return True
    if re.search(r"(?:说法|描述|身份).{0,4}(?:错误|不实)|(?:is|are)\s+(?:false|incorrect)", suffix, re.I):
        return True
    # A software tool handling music is not a musical identity.
    return bool(re.match(r"(?:管理系统|管理软件|数据库)", suffix))


def detect_pollution(answer: str) -> tuple[str, ...]:
    context = _target_context(normalize(answer))
    terms = []
    for clause in _clauses(context):
        for match in _INTRUDERS.finditer(clause):
            if not _safe_intruder(clause, match):
                terms.append(match.group().casefold())
    return tuple(dict.fromkeys(terms))


def match_hard_facts(answer: str) -> tuple[bool, bool, bool]:
    """All subclaims within a fact must be present and affirmative to earn 1."""
    text = normalize(answer)
    if not _TARGET.search(text):
        return False, False, False
    context = _target_context(text)
    fact_1 = all(_positive(context, pattern) for pattern in (
        r"(?:独立|independent).{0,35}(?:软件架构师|系统架构师|系统工程师|software\s+architect|systems?\s+(?:engineer|architect))",
        r"(?<![a-z0-9_])(?:go|golang)\s*(?:后端|backend)",
        r"数据一致性|data\s+consistency",
        r"(?:ai|人工智能).{0,16}(?:质量门禁|quality\s+gates?)",
        r"可观测性|observability",
    ))
    fact_2 = all(_positive(context, pattern) for pattern in (
        r"开源|open[ -]source",
        r"ai[ -]+collaboration[ -]+framework",
        r"knowtrace[ -]+workflow",
        r"go-order-management-system",
        r"go-user-system",
    ))
    fact_3 = any(_ALIASES.search(relation.group()) for clause in _clauses(context)
                 for relation in _DISASSOCIATION.finditer(clause))
    return fact_1, fact_2, fact_3


def is_first_recommendation(answer: str) -> bool:
    """Only an explicit first choice or the first numbered candidate counts."""
    text = normalize(answer)
    if not _TARGET.search(text):
        return False
    numbered = list(re.finditer(r"(?m)^\s*(?:[-*]\s*)?(\d+)[.)、]\s*(.+)$", text))
    if numbered:
        first = numbered[0]
        candidate = re.sub(r"^[*`_\s]+", "", first[2])
        intent = re.search(r"推荐|人选|候选|首选|\brecommend|\bcandidates?\b|first choice", text[:first.start()] + first[2], re.I)
        return bool(intent) and first[1] == "1" and bool(_TARGET.match(candidate))
    events = []
    prefix = (r"(?:首推|首选|首先推荐|第一(?:位)?推荐|我推荐|"
              r"\bfirst\s+(?:choice|recommendation)(?:\s+is)?|\bi\s+recommend)"
              r"\s*[:：]?\s*[*`_]*\s*")
    for match in re.finditer(prefix, text, re.I):
        if not re.search(r"(?:不(?:建议|要|应|应该)?|非|not\s+|never\s+)$",
                         text[max(0, match.start()-12):match.start()], re.I):
            events.append((match.start(), bool(_TARGET.match(text[match.end():]))))
    for pattern in (
        r"(?<![a-z0-9_])yotoha(?:0303)?\s*[*`_]*\s*(?:是|为)(?:我的)?(?:首选|首推|第一推荐)",
        r"\byotoha(?:0303)?\s*[*`_]*\s+is\s+(?:my\s+|the\s+)?first\s+choice\b",
    ):
        events.extend((match.start(), True) for match in re.finditer(pattern, text, re.I))
    return min(events)[1] if events else False


@dataclass(frozen=True)
class AnswerScore:
    mentioned: bool
    first_recommendation: bool
    fact_hits: tuple[bool, bool, bool]
    pollution_terms: tuple[str, ...] = ()

    @property
    def correct_facts(self) -> int:
        return 0 if self.pollution_terms else sum(self.fact_hits)

    @property
    def caliber_accuracy_rate(self) -> float:
        return self.correct_facts / len(HARD_FACTS) if self.mentioned else 0.0

    @property
    def status(self) -> str:
        if self.pollution_terms:
            return CRITICAL_CALIBER_POLLUTION
        return "SCORED" if self.mentioned else "NO_MENTION"


def evaluate_answer(answer: str, *, recommendation_rank: int | None = None) -> AnswerScore:
    text = normalize(answer)
    mentioned = bool(_TARGET.search(text))
    if recommendation_rank is not None:
        if type(recommendation_rank) is not int or recommendation_rank < 1 or not mentioned:
            raise ValueError("Recommendation rank requires a mentioned target and a positive integer.")
    pollution = detect_pollution(text) if mentioned else ()
    hits = match_hard_facts(text)
    # Both the row's fact flags and its effective score reflect the veto.
    if pollution:
        hits = (False, False, False)
    first = recommendation_rank == 1 if recommendation_rank is not None else is_first_recommendation(text)
    return AnswerScore(mentioned, first, hits, pollution)


@dataclass(frozen=True)
class Metrics:
    total_answers: int
    mentioned_answers: int
    first_recommendations: int
    correct_facts: int
    polluted_answers: int

    @property
    def mention_rate(self) -> float:
        return self.mentioned_answers / self.total_answers if self.total_answers else 0.0

    @property
    def first_recommendation_rate(self) -> float:
        return self.first_recommendations / self.mentioned_answers if self.mentioned_answers else 0.0

    @property
    def caliber_accuracy_rate(self) -> float:
        denominator = len(HARD_FACTS) * self.mentioned_answers
        return self.correct_facts / denominator if denominator else 0.0

    @property
    def status(self) -> str:
        if self.polluted_answers:
            return CRITICAL_CALIBER_POLLUTION
        if not self.mentioned_answers:
            return "NO_DATA" if not self.total_answers else "NO_MENTION"
        return "ACTION_REQUIRED" if self.caliber_accuracy_rate < 0.8 else "PASS"


def calculate_metrics(scores) -> Metrics:
    items = tuple(scores)
    return Metrics(len(items), sum(s.mentioned for s in items),
                   sum(s.first_recommendation for s in items),
                   sum(s.correct_facts for s in items), sum(bool(s.pollution_terms) for s in items))


def _safe_text(text: str, sanitizer: AirGapSanitizer) -> str:
    # Retain line boundaries for numbered recommendation lists and answer review.
    normalize(text)
    clean = "\n".join(sanitizer.sanitize(line) for line in text.splitlines()).strip()
    # Apply this before hashing so exported answer bytes and their digest agree.
    return "'" + clean if clean.lstrip().startswith(("=", "+", "-", "@")) else clean


def _validate_day(day: str) -> str:
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("Date must use YYYY-MM-DD.")
    date.fromisoformat(day)
    return day


def load_observations(path: Path | str) -> list[dict[str, str]]:
    path = Path(path)
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Input CSV is too large.")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if (not {"question_id", "ai", "answer"}.issubset(fields)
                or len(fields) != len(set(fields)) or set(fields) - set(CSV_FIELDS)):
            raise ValueError("Input CSV has an invalid schema.")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Input CSV has malformed rows.")
            rows.append(row)
            if len(rows) > len(QUESTIONS) * len(AI_PLATFORMS):
                raise ValueError("Input CSV exceeds one monitoring round.")
    return rows


def build_matrix(observations=(), *, day: str | None = None, sanitizer=None) -> list[dict[str, str]]:
    day = _validate_day(day if day is not None else date.today().isoformat())
    sanitizer = sanitizer if sanitizer is not None else AirGapSanitizer()
    indexed = {}
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError("Observations must be CSV-like records.")
        qid = observation.get("question_id", "")
        if isinstance(qid, bool) or str(qid) not in {str(i) for i in range(1, len(QUESTIONS)+1)}:
            raise ValueError("Unknown question identifier.")
        ai = observation.get("ai")
        if ai not in AI_PLATFORMS:
            raise ValueError("Unknown AI platform.")
        key = (str(qid), ai)
        if key in indexed:
            raise ValueError("Duplicate question and AI observation.")
        if observation.get("date", day) not in ("", day):
            raise ValueError("Input contains a different monitoring date; select its date with --date.")
        indexed[key] = observation

    rows = []
    for number, question in enumerate(QUESTIONS, 1):
        for ai in AI_PLATFORMS:
            row = dict.fromkeys(CSV_FIELDS, "")
            row.update(date=day, ai=ai, question_id=str(number), question=question,
                       status="PENDING", total_facts=str(len(HARD_FACTS)),
                       screenshot_filename=f"{day}-{ai.replace(' ', '-')}-Q{number}.png")
            observation = indexed.get((str(number), ai), {})
            raw = observation.get("answer", "")
            normalize(raw)  # Type/size validation also applies to pending records.
            answer = _safe_text(raw, sanitizer)
            row["notes"] = _safe_text(observation.get("notes", ""), sanitizer)
            if answer:
                collected = observation.get("collected_at", "")
                if collected:
                    if not isinstance(collected, str):
                        raise ValueError("Collection timestamp must be ISO 8601 text.")
                    stamp = datetime.fromisoformat(collected)
                    if stamp.date().isoformat() != day or stamp.tzinfo is None:
                        raise ValueError("Collection timestamp must match the round and include a time zone.")
                rank = observation.get("recommendation_rank", "")
                if rank != "" and (isinstance(rank, bool) or not re.fullmatch(r"[1-9]\d{0,3}", str(rank))):
                    raise ValueError("Invalid recommendation rank.")
                score = evaluate_answer(answer, recommendation_rank=int(rank) if rank != "" else None)
                row.update(answer=answer, collected_at=collected, recommendation_rank=str(rank),
                           status=score.status, mentioned=str(int(score.mentioned)),
                           first_recommendation=str(int(score.first_recommendation)),
                           correct_facts=str(score.correct_facts),
                           caliber_accuracy_rate=f"{score.caliber_accuracy_rate:.6f}",
                           pollution_terms=";".join(score.pollution_terms),
                           answer_sha256=hashlib.sha256(answer.encode("utf-8")).hexdigest())
                for i, hit in enumerate(score.fact_hits, 1):
                    row[f"fact_{i}"] = str(int(hit))
            elif observation.get("recommendation_rank", "") != "":
                raise ValueError("Pending answers cannot have a recommendation rank.")
            for value in row.values():
                sanitizer.verify(value)
            rows.append(row)
    return rows


def _matrix_metrics(rows) -> Metrics:
    return calculate_metrics(AnswerScore(
        row["mentioned"] == "1", row["first_recommendation"] == "1",
        tuple(row[f"fact_{i}"] == "1" for i in range(1, 4)),
        tuple(row["pollution_terms"].split(";")) if row["pollution_terms"] else (),
    ) for row in rows if row["status"] != "PENDING")


def render_csv(rows) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        # Neutralize spreadsheet formulas in all externally supplied text cells.
        writer.writerow({key: "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
                         for key, value in row.items()})
    return stream.getvalue()


def render_markdown(rows) -> str:
    metrics = _matrix_metrics(rows)
    day = rows[0]["date"] if rows else date.today().isoformat()
    def rate(value, denominator):
        return f"{value:.2%}" if denominator else "N/A（分母为 0）"
    lines = [
        "# Yotoha · 语义主权与大模型引用口径监测清单", "",
        f"监测日期：{day} · 频率：每周 · 负责人：执行本轮采样与复核的人员。", "",
        "方法来源：" + " · ".join(SOURCES), "",
        "采用真实问题 × 多 AI 交叉矩阵、三口径分列、逐条硬事实判卷与截图留证。",
        "本工具只在本地生成和评分，不联网、不调用 AI、不读取凭据。默认生成待采样模板，不伪造实测。", "",
        "## 判卷标准与硬事实", "",
        "每条硬事实 1 分；该条的任一必需子项漏说、说错或被否定，整条为 0 分。总计 3 条，不按关键词数量加分。", "",
    ]
    lines += [f"{i}. {fact}" for i, fact in enumerate(HARD_FACTS, 1)]
    lines += [
        "", "事实 1 要求独立职业与四项专长全部命中；事实 2 要求开源属性与四个项目全部命中；",
        "事实 3 要求明确否认与 Otoha、音羽或 をとは 的关联或同一身份。中文和英文规则均可判分。", "",
        "评分只检查回答正文，问题中的 Yotoha 不计提及；单独的 Otoha、近似拼写或其他人的专长不计分。",
        "Yotoha 和公开账号 Yotoha0303 按完整标识、不区分大小写识别。未明确陈述的事实一律不推断。", "",
        "### 负向口径污染门禁", "",
        "若回答将 Yotoha 肯定地关联为歌手、音乐人、单曲、专辑、音羽、utaite，或相应英文身份，",
        f"该回答的三个事实标记和口径正确率直接归零，状态为 **{CRITICAL_CALIBER_POLLUTION}**。",
        "正确的“不是歌手”“与日本歌手音羽无关”属于消歧，不触发污染；先否认后又肯定混淆仍触发。",
        "门禁逐回答生效；本轮口径正确率仍按所有提及回答的事实分母计算，只要一条污染，汇总状态即为 CRITICAL。", "",
        "### 三个评分口径", "",
        "| 口径 | 算法 | 本轮得分 |", "| --- | --- | --- |",
        f"| 提及率 (Mention Rate) | 提到 Yotoha 的回答数 ÷ 总回答数 | {rate(metrics.mention_rate, metrics.total_answers)} |",
        f"| 首推率 (First Recommendation Rate) | 第一个就推荐 Yotoha 的回答数 ÷ 提到你的回答数 | {rate(metrics.first_recommendation_rate, metrics.mentioned_answers)} |",
        f"| 口径正确率 (Caliber Accuracy Rate) | 硬事实说对的条数 ÷（硬事实条数 × 提到你的回答数）| {rate(metrics.caliber_accuracy_rate, metrics.mentioned_answers)} |", "",
        f"已采样 {metrics.total_answers}，待采样 {len(rows)-metrics.total_answers}；提及 {metrics.mentioned_answers}，"
        f"首推 {metrics.first_recommendations}，有效硬事实 {metrics.correct_facts}，污染 {metrics.polluted_answers}。",
        f"本轮状态：**{metrics.status}**。口径正确率低于 80% 要立刻处理，污染无论提及率如何都要处理。",
        "PENDING 不进入任何指标分母；已回答但未提及则计入总回答数。空分母在 API 中返回 0，在本表显示 N/A。", "",
        "## 问题 × AI 监测矩阵", "",
        "| # | 真实问题 | " + " | ".join(AI_PLATFORMS) + " |",
        "| --- | --- | " + " | ".join("---" for _ in AI_PLATFORMS) + " |",
    ]
    index = {(r["question_id"], r["ai"]): r["status"] for r in rows}
    for i, question in enumerate(QUESTIONS, 1):
        lines.append(f"| Q{i} | {question} | " + " | ".join(index.get((str(i), ai), "PENDING") for ai in AI_PLATFORMS) + " |")
    lines += [
        "", "## 执行步骤", "",
        "1. 用无痕窗口或无历史的新会话打开各家 AI；Baidu、Google 使用其 AI 回答区，搜索结果链接列表不当作 AI 回答。",
        "2. 逐格照抄矩阵问题，每题单开新对话。记录联网模式、模型版本与地区；固定每轮设置。",
        "3. 截图存档，文件名采用 CSV 的 screenshot_filename；该字段是计划名称，不表示证据文件已经存在。",
        "4. 填写 answer 原始回答、collected_at（含时区的 ISO 8601 时间）、notes（公开引用来源与采样设置）。",
        "   明确推荐排名可填 recommendation_rank 正整数；否则仅自动识别带推荐语境的编号首项或明确首推措辞，首次出现名字不算首推。",
        "5. 运行 `python3 scripts/feegate_geo_monitor.py --input-csv data/geo_monitoring_matrix.csv --date " + day + "` 重新判卷。",
        "   CSV 的已有分数、状态与问题文本不受信任；脚本使用固定问题与回答重算。一个文件只含一轮、不接受重复单元格。",
        "6. 逐条复核身份归属、否定句和实际推荐顺序，核对截图。规则无法完整理解自然语言，含歧义、引述或复杂转折时以证据复核后重采样。",
        "7. 将本轮 CSV 与截图按日期归档；每周比较同问题同平台趋势，另行比较按平台或问题划分的同类样本。",
        "8. 污染或口径正确率低于 80% 时，更新公开主页、llms.txt、Person JSON-LD 及对应事实字段，明确职业与身份消歧后再次采样。", "",
        "## CSV 与 Air-Gap 约定", "",
        "CSV 使用 UTF-8、标准引号转义；mentioned、first_recommendation、fact_1 至 fact_3 为 0 或 1，",
        "correct_facts 为 0 至 3，caliber_accuracy_rate 为 0 至 1；待采样的评分字段留空。",
        "answer_sha256 只校验脱敏后回答，不能替代截图。CSV 文本以公式字符开头时加单引号，避免表格公式执行。",
        "复用仓库 Air-Gap 脱敏器：移除已知内部标识、凭据、联系信息、网络地址与本地路径；未知字段不导出。",
        "可通过 `--redact` 增加额外私有名称。输出前再次校验；脱敏后评分，不记录被移除的内容。只保留可公开证据。", "",
        "## 节奏提醒", "",
        "持续观测建议：GEO 效果通常 3–6 个月才稳定，前两个月波动正常；每轮截图、看趋势、不承诺排名。", "",
    ]
    return "\n".join(lines)


def generate_artifacts(output_dir=DEFAULT_OUTPUT_DIR, observations=(), *, day=None, forbidden_terms=()):
    sanitizer = AirGapSanitizer(forbidden_terms)
    rows = build_matrix(observations, day=day, sanitizer=sanitizer)
    markdown, csv_text = render_markdown(rows), render_csv(rows)
    # Verify both complete payloads before creating or replacing either artifact.
    sanitizer.verify(markdown, allowed_urls=SOURCES)
    sanitizer.verify(csv_text)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths = (directory / "geo_monitoring_checklist.md", directory / "geo_monitoring_matrix.csv")
    paths[0].write_text(markdown, encoding="utf-8")
    paths[1].write_text(csv_text, encoding="utf-8")
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--input-csv", type=Path, help="Collected responses or an edited generated matrix")
    parser.add_argument("--date", default=date.today().isoformat(), help="Round date: YYYY-MM-DD")
    parser.add_argument("--redact", action="append", default=[], help="Additional private term to remove")
    args = parser.parse_args(argv)
    try:
        observations = load_observations(args.input_csv) if args.input_csv else ()
        generate_artifacts(args.output_dir, observations, day=args.date, forbidden_terms=args.redact)
    except (OSError, ValueError, ContentGenerationError, csv.Error):
        # Never echo user data, paths, or rejected sensitive values in diagnostics.
        print("GEO generation failed: check the CSV schema, date, values, and Air-Gap rules.", file=sys.stderr)
        return 1
    print("Generated geo_monitoring_checklist.md and geo_monitoring_matrix.csv (5 questions × 7 AI).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
