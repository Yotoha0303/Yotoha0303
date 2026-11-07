#!/usr/bin/env python3
"""Bilingual, standard-library freelance inquiry daemon (Python 3.10+).

Run a long-lived JSONL stream (one flushed JSON reply per input line)::

    python3 scripts/freelance_chat_assistant_daemon.py \
        --sla-url https://your-service.example/terms --seed 18

Message: {"platform":"xianyu","conversation_id":"customer-1","text":"你好"}
Export:  {"op":"export","platform":"xianyu","conversation_id":"customer-1",
          "format":"markdown"}

Platforms are xianyu/闲鱼, eleduck/电鸭 and v2ex, case insensitive. The normal
path advances at most one state per message: GREETING -> SCOPE_CLARIFICATION ->
QUOTE_PROPOSAL -> ESCROW_AGREEMENT -> HANDOFF. Quotes require a recognizable,
in-card scope, input source/sample, output, CNY budget and delivery window.
Changes after a proposal reopen clarification. Scope descriptions accumulate
conservatively: contradictory or reduced scope may need human review. A human
request or cancellation can hand off early. HANDOFF is terminal.

The caller supplies the actual published SLA URL; no URL is fetched. Existing
intake cards supply prices and conditional SLA terms. All replies are drafts;
no account login, platform messaging, payment verification, or sleeping occurs.
Delays use the existing conditioned Poisson(75) sampler over 45..120 seconds.
recommended_send_at also queues successive drafts within each conversation.
Escrow assent triggers a human/project handoff with funding verification still
required; a customer's payment claim never starts the delivery clock.

Sessions and optional message_id deduplication live in memory, bounded by
max_conversations and max_turns. Restarting does not restore them. JSON/Markdown
exports include both sides, timestamps, state changes, scope and quote terms.
--transcript-dir saves a JSON snapshot after each message, using hashed filenames.
Invalid JSONL events return errors and processing continues. EOF stops the loop;
Ctrl-C exits 130; invalid configuration exits 2. No third-party packages required.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import html
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

if __package__:
    from . import freelance_client_intake_engine as intake
    from .anti_ban_human_behavior_engine import PoissonReadingDelay
else:
    import freelance_client_intake_engine as intake
    from anti_ban_human_behavior_engine import PoissonReadingDelay


SCHEMA_VERSION = "1.0"
MAX_EVENT_LENGTH = 128 * 1024
PLATFORMS = {"xianyu": "xianyu", "闲鱼": "xianyu", "eleduck": "eleduck",
             "电鸭": "eleduck", "v2ex": "v2ex"}


class ChatError(ValueError):
    """Invalid input or an unavailable conversation."""


class ConversationState(str, Enum):
    GREETING = "GREETING"
    SCOPE_CLARIFICATION = "SCOPE_CLARIFICATION"
    QUOTE_PROPOSAL = "QUOTE_PROPOSAL"
    ESCROW_AGREEMENT = "ESCROW_AGREEMENT"
    HANDOFF = "HANDOFF"


def _text(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ChatError(f"{name} must be nonempty text of at most {maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ChatError(f"{name} contains control characters")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ChatError(f"{name} must be valid UTF-8 text") from None
    return value.strip()


def _platform(value):
    key = _text(value, "platform").casefold()
    if key not in PLATFORMS:
        raise ChatError("platform must be xianyu, eleduck or v2ex")
    return PLATFORMS[key]


def _sla_url(value):
    value = _text(value, "sla_url", 4096)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(char.isspace() for char in value)):
            raise ValueError()
        parsed.port
    except ValueError:
        raise ChatError("sla_url must be an HTTP(S) URL without credentials") from None
    return value


def _timestamp(seconds):
    if type(seconds) not in (int, float):
        raise ChatError("clock must return finite nonnegative Unix seconds")
    try:
        if seconds < 0 or not math.isfinite(seconds):
            raise ValueError()
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        raise ChatError("clock is outside the supported timestamp range") from None


_NEGATIVE = re.compile(
    r"\b(?:no|not|never|don't|won't|cannot|can't|decline|reject)\b|"
    r"不(?:同意|接受|行|能|要|想|用)|没(?:同意|确认)|拒绝|太贵|便宜|超预算", re.I)
_QUESTION = re.compile(r"[?？]|\b(?:can|could|would|should|what|why|how|whether)\b|是否|能否|吗", re.I)
_YES = re.compile(r"(?:yes|ok(?:ay)?|agreed|accepted|confirmed|go ahead|sounds good|"
                  r"let's proceed|好(?:的)?|可以|没问题|同意|确认|就这样)[.!。！\s]*", re.I)
_QUOTE_ACCEPT = re.compile(
    r"\b(?:i\s+)?(?:accept|agree to|confirm)\s+(?:the\s+)?(?:quote|price|proposal|scope)\b|"
    r"(?:同意|接受|确认)(?:这个|该|以上)?(?:报价|价格|方案|范围)|按这个报价", re.I)
_ESCROW = re.compile(r"\bescrow\b|平台担保|担保交易|资金托管", re.I)
_ESCROW_ACCEPT = re.compile(
    r"\b(?:agree|accept|confirm|use|proceed)\b.{0,45}\bescrow\b|"
    r"(?:同意|接受|确认|使用|走).{0,15}(?:托管|平台担保|担保交易)", re.I)
_HUMAN = re.compile(r"\b(?:human|real person|speak to (?:a )?(?:person|developer))\b|转人工|人工客服|找真人", re.I)
_CANCEL = re.compile(r"\b(?:cancel|stop (?:this|the) (?:order|project|conversation))\b|取消(?:订单|项目)?|不做了", re.I)
_OUTPUT = re.compile(r"(?<![a-zA-Z0-9])(?:csv|json|xlsx|excel|dashboard|source code|webhook|workflow)(?![a-zA-Z0-9])|"
                     r"表格|报表|源代码|工作流|计量网关", re.I)
_SOURCE = re.compile(r"https?://[^\s<>]+|(?<![a-zA-Z0-9])(?:input sample|sample (?:file|input|data))(?![a-zA-Z0-9])|输入样例|样例文件|样本数据", re.I)
_PRICE_OBJECTION = re.compile(r"\b(?:expensive|discount|cheaper|over budget)\b|太贵|便宜|超预算|优惠", re.I)


def parse_customer_message(text, platform="xianyu"):
    """Extract bilingual inquiry facts and conservative, explicit assent signals."""
    platform = _platform(platform)
    try:
        normalized = intake.normalize_inquiry(text)
        normalized.encode("utf-8")
        analysis = intake.analyze_inquiry(normalized, platform)
    except (ValueError, UnicodeError) as error:
        raise ChatError(str(error)) from None
    affirmative = not (_NEGATIVE.search(normalized) or _QUESTION.search(normalized))
    return {
        "text": normalized, "language": analysis["language"],
        "intent": analysis["classification"]["primary"],
        "scope_counts": analysis["complexity"]["scope_counts"],
        **analysis["quote"]["constraints"],
        "source": bool(_SOURCE.search(normalized)),
        "output": bool(_OUTPUT.search(normalized)),
        "quote_accepted": bool(affirmative and (_YES.fullmatch(normalized)
                                                or _QUOTE_ACCEPT.search(normalized))),
        "escrow_accepted": bool(affirmative and _ESCROW_ACCEPT.search(normalized)),
        "escrow_refused": bool(_ESCROW.search(normalized) and _NEGATIVE.search(normalized)),
        "price_objection": bool(_PRICE_OBJECTION.search(normalized)),
        "human_requested": bool(_HUMAN.search(normalized) and not _NEGATIVE.search(normalized)),
        "cancelled": bool(_CANCEL.search(normalized) and not re.search(
            r"\b(?:don't|do not|not)\b|不要取消|不取消", normalized, re.I)),
    }


@dataclass
class _Conversation:
    platform: str
    conversation_id: str
    state: ConversationState = ConversationState.GREETING
    language: str = "en"
    scope_messages: list = field(default_factory=list)
    profile: dict = field(default_factory=lambda: {
        "source": False, "output": False, "budget_cny": None, "deadline_hours": None})
    quote: dict | None = None
    quote_accepted: bool = False
    escrow_agreed: bool = False
    handoff_reason: str | None = None
    transcript: list = field(default_factory=list)
    transitions: list = field(default_factory=list)
    replies: dict = field(default_factory=dict)
    recommended_at: float = 0.0


class ChatAssistantDaemon:
    """Thread-safe in-memory sessions; methods return detached JSON-ready values."""

    def __init__(self, sla_url, *, seed=None, clock=time.time,
                 max_conversations=1000, max_turns=200):
        self.sla_url = _sla_url(sla_url)
        for value, name in ((max_conversations, "max_conversations"), (max_turns, "max_turns")):
            if type(value) is not int or value < 1:
                raise ChatError(f"{name} must be a positive integer")
        self.max_conversations, self.max_turns = max_conversations, max_turns
        self._clock = clock
        self._pacing = PoissonReadingDelay(rng=random.Random(seed))
        self._sessions = {}
        self._lock = threading.RLock()

    def _key(self, platform, conversation_id):
        return _platform(platform), _text(conversation_id, "conversation_id")

    def _find(self, platform, conversation_id):
        key = self._key(platform, conversation_id)
        if key not in self._sessions:
            raise ChatError("conversation not found")
        return self._sessions[key]

    def _analysis(self, session):
        return intake.analyze_inquiry(
            "\n".join(session.scope_messages) or "Hello", session.platform,
            language=session.language, budget_cny=session.profile["budget_cny"],
            deadline_hours=session.profile["deadline_hours"])

    def _move(self, session, state, reason, timestamp):
        if session.state != state:
            session.transitions.append({"from": session.state.value, "to": state.value,
                                        "reason": reason, "timestamp": timestamp})
            session.state = state

    def _update_scope(self, session, parsed):
        changed = False
        for key in ("budget_cny", "deadline_hours"):
            if parsed[key] is not None and parsed[key] != session.profile[key]:
                session.profile[key] = parsed[key]
                changed = True
        detail = (parsed["intent"] != "unknown" or parsed["scope_counts"]
                  or parsed["source"] or parsed["output"])
        if detail and parsed["text"] not in session.scope_messages:
            session.scope_messages.append(parsed["text"])
            changed = True
        for key in ("source", "output"):
            session.profile[key] |= parsed[key]
        if len("\n".join(session.scope_messages)) > intake.MAX_INQUIRY_LENGTH:
            raise ChatError("accumulated scope exceeds the inquiry length limit")
        return changed

    def _clarification(self, session, analysis):
        zh = session.language == "zh"
        questions = []
        if analysis["classification"]["primary"] == "unknown":
            questions.append("希望完成什么任务？" if zh else "What task should the delivery complete?")
        for key, question_zh, question_en in (
            ("source", "请提供输入链接或小样。", "Please share an input link or small sample."),
            ("output", "希望收到什么格式的结果？", "What output format should you receive?"),
            ("budget_cny", "人民币预算是多少？", "What is your budget in CNY?"),
            ("deadline_hours", "最晚几天或几小时内交付？", "Within how many days or hours do you need delivery?"),
        ):
            if session.profile[key] is None or session.profile[key] is False:
                questions.append(question_zh if zh else question_en)
        questions.extend(issue["message"] for issue in analysis["quote"]["issues"])
        return questions

    def _respond(self, session, parsed, changed, timestamp):
        zh = session.language == "zh"
        state = session.state
        if state == ConversationState.HANDOFF:
            return "已转交人工跟进，您的补充已记录。" if zh else "Your update is recorded for the human handling this handoff."
        if parsed["human_requested"] or parsed["cancelled"]:
            session.handoff_reason = "cancelled" if parsed["cancelled"] else "human_requested"
            self._move(session, ConversationState.HANDOFF, session.handoff_reason, timestamp)
            return ("已记录，我会将对话转交人工跟进。" if zh else
                    "Recorded. I will hand this conversation over for human follow-up.")
        if state in {ConversationState.QUOTE_PROPOSAL, ConversationState.ESCROW_AGREEMENT} and changed:
            session.quote, session.quote_accepted, session.escrow_agreed = None, False, False
            self._move(session, ConversationState.SCOPE_CLARIFICATION, "terms_changed", timestamp)
            state = session.state
            # Always show a fresh proposal on a later turn before accepting it.
            analysis = self._analysis(session)
            questions = self._clarification(session, analysis)
            return (("需求有更新，请重新确认：" if zh else "The terms changed; please reconfirm: ")
                    + " ".join(questions or ["确认后我会重新报价。" if zh else "I will send a revised quote after confirmation."]))
        if state == ConversationState.GREETING:
            self._move(session, ConversationState.SCOPE_CLARIFICATION, "inquiry_received", timestamp)
        if session.state == ConversationState.SCOPE_CLARIFICATION:
            analysis = self._analysis(session)
            questions = self._clarification(session, analysis)
            if state != ConversationState.GREETING and not questions:
                session.quote = copy.deepcopy(analysis["quote"])
                self._move(session, ConversationState.QUOTE_PROPOSAL, "scope_ready", timestamp)
                proposal = analysis["proposal"]
                return " ".join([
                    proposal["outcome"], f"¥{session.quote['price_cny']:,} CNY.",
                    proposal["reference_scope"], proposal["exclusions"], proposal["timeline"],
                    " ".join(proposal["deliverables"]), proposal["acceptance"],
                    proposal["sla"]["clock_start"], proposal["sla"]["support"],
                    "确认这个范围和报价后，我们再确认平台托管条款。" if zh else
                    "Accept this scope and quote, then we can agree platform escrow terms.",
                ])
            intro = {
                "xianyu": ("您好！先把结果和价格说清楚，按样例验收。", "Hello! Let's agree the result and price, then check an example."),
                "eleduck": ("您好！可按远程协作方式确认范围、同步样例并验收。", "Hello! We can work remotely with a clear scope, sample review and acceptance."),
                "v2ex": ("您好！交付包含源代码、运行说明和可重复验收样例。", "Hello! Delivery includes source code, run instructions and repeatable acceptance examples."),
            }[session.platform][0 if zh else 1]
            return intro + " " + " ".join(questions or [
                "请确认以上范围，我会给出参考报价。" if zh else "Please confirm this scope so I can send an indicative quote."])
        if state == ConversationState.QUOTE_PROPOSAL:
            if parsed["quote_accepted"] and not parsed["price_objection"]:
                session.quote_accepted = True
                self._move(session, ConversationState.ESCROW_AGREEMENT, "quote_accepted", timestamp)
                sla = intake.STANDARD_SLA
                return (("请确认使用平台担保或双方认可的托管服务，全额入金；不直接转账。" if zh else
                         "Please agree to platform escrow or a mutually approved escrow provider, fully funded before work starts. ")
                        + " ".join(sla[key][session.language] for key in ("clock_start", "clock_pause", "acceptance", "remedy"))
                        + (" 请明确回复同意平台担保交易。" if zh else " Please explicitly confirm agreement to escrow."))
            if parsed["price_objection"]:
                return ("可以先缩小首期范围，在保留验收和售后保障的前提下重新报价。您最需要保留哪项结果？" if zh else
                        "We can reduce the first phase and re-quote while keeping acceptance and support. Which result matters most?")
            return ("有哪里需要调整？请明确确认当前范围和报价，或告诉我您的顾虑。" if zh else
                    "What would you like adjusted? Please explicitly accept the current scope and quote, or share your concern.")
        if parsed["escrow_accepted"]:
            session.escrow_agreed = True
            session.handoff_reason = "escrow_terms_agreed"
            self._move(session, ConversationState.HANDOFF, session.handoff_reason, timestamp)
            return ("托管条款已确认，现转交项目负责人核验全额入金、资料、验收样例和档期；核验完成后才开始48小时交付计时。" if zh else
                    "Escrow terms agreed. Handoff to the project owner to verify full funding, inputs, acceptance examples and booking before the 48-hour delivery clock starts.")
        if parsed["escrow_refused"]:
            return ("可以先解释担保流程；全额托管和明确验收保护双方，未确认前不会开工。需要人工说明吗？" if zh else
                    "I can explain escrow. Full funding and explicit acceptance protect both sides; work awaits agreement. Would a human explanation help?")
        return ("请明确确认是否同意平台担保交易；付款消息需由负责人核验，暂不开始交付计时。" if zh else
                "Please explicitly confirm agreement to escrow. The project owner must verify funding before the delivery clock starts.")

    def process_message(self, platform, conversation_id, text, *, message_id=None):
        """Create/advance one session and recommend pacing without sending or waiting.

        A repeated message_id and normalized text returns the original reply;
        reusing an ID for different text fails. No ID means no deduplication.
        """
        key = self._key(platform, conversation_id)
        parsed = parse_customer_message(text, key[0])
        if message_id is not None:
            message_id = _text(message_id, "message_id")
        with self._lock:
            existing = self._sessions.get(key)
            if existing is None and len(self._sessions) >= self.max_conversations:
                raise ChatError("conversation capacity reached")
            if existing and message_id in existing.replies:
                prior = existing.replies[message_id]
                if prior["text"] != parsed["text"]:
                    raise ChatError("message_id was already used for different text")
                return copy.deepcopy(prior["reply"])
            if existing and len(existing.transcript) // 2 >= self.max_turns:
                raise ChatError("conversation turn limit reached; export and hand off")
            # Commit only after validation, response generation and pacing succeed.
            session = copy.deepcopy(existing) if existing else _Conversation(*key)
            if re.search(r"[\u3400-\u9fff]", parsed["text"]) or existing is None:
                session.language = parsed["language"]
            now = self._clock()
            timestamp = _timestamp(now)
            before = session.state.value
            changed = (self._update_scope(session, parsed)
                       if session.state != ConversationState.HANDOFF else False)
            response = self._respond(session, parsed, changed, timestamp)
            response += "\nSLA: " + self.sla_url
            delay = self._pacing.sample()
            session.recommended_at = max(now, session.recommended_at) + delay
            send_at = _timestamp(session.recommended_at)
            reply = {
                "schema_version": SCHEMA_VERSION, "platform": key[0], "conversation_id": key[1],
                "state_before": before, "state": session.state.value, "language": session.language,
                "response": response, "sla_url": self.sla_url,
                "reading_delay_seconds": delay,
                "recommended_send_at": send_at, "wait_seconds": session.recommended_at - now,
                "quote": copy.deepcopy(session.quote), "handoff_reason": session.handoff_reason,
                "funding_verified": False,
            }
            session.transcript.extend([
                {"role": "customer", "text": parsed["text"], "timestamp": timestamp,
                 "state": before, "message_id": message_id},
                {"role": "assistant", "text": response, "timestamp": timestamp,
                 "state": session.state.value, "reading_delay_seconds": delay,
                 "recommended_send_at": send_at},
            ])
            if message_id is not None:
                session.replies[message_id] = {"text": parsed["text"], "reply": copy.deepcopy(reply)}
            self._sessions[key] = session
            return reply

    def get_conversation(self, platform, conversation_id):
        with self._lock:
            session = self._find(platform, conversation_id)
            return copy.deepcopy({
                "schema_version": SCHEMA_VERSION, "platform": session.platform,
                "conversation_id": session.conversation_id, "language": session.language,
                "state": session.state.value, "profile": session.profile,
                "scope_messages": session.scope_messages, "quote": session.quote,
                "quote_accepted": session.quote_accepted, "escrow_agreed": session.escrow_agreed,
                "funding_verified": False, "handoff_reason": session.handoff_reason,
                "sla_url": self.sla_url, "transitions": session.transitions,
                "messages": session.transcript,
            })

    def export_transcript(self, platform, conversation_id, *, format="json", path=None):
        """Return UTF-8 JSON/Markdown, optionally replacing a local file atomically."""
        if format not in {"json", "markdown"}:
            raise ChatError("format must be json or markdown")
        data = self.get_conversation(platform, conversation_id)
        if format == "json":
            rendered = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        else:
            def escape(value):
                return re.sub(r"([\\`*_\[\]{}|>#])", r"\\\1", html.escape(str(value), quote=False))

            lines = ["# Conversation transcript", "", f"Platform: {data['platform']}",
                     "Conversation: " + escape(data["conversation_id"]),
                     f"State: {data['state']}", "SLA: " + escape(self.sla_url),
                     "Funding verified: false", "", "## Scope and quote", ""]
            lines.extend(escape(json.dumps({key: data[key]}, ensure_ascii=False))
                         for key in ("profile", "scope_messages", "quote", "handoff_reason"))
            lines += ["", "## State transitions", ""]
            lines.extend(escape(f"{item['timestamp']} {item['from']} -> {item['to']} ({item['reason']})")
                         for item in data["transitions"])
            for message in data["messages"]:
                lines += ["", f"## {message['role']} · {message['timestamp']} · {message['state']}", "",
                          escape(message["text"])]
                if message["role"] == "assistant":
                    lines += ["", f"Reading delay: {message['reading_delay_seconds']}s; "
                              f"recommended send: {message['recommended_send_at']}"]
            rendered = "\n".join(lines) + "\n"
        if path is not None:
            destination = Path(path)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                                 delete=False, prefix=".chat-") as handle:
                    temporary = Path(handle.name)
                    handle.write(rendered)
                os.replace(temporary, destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return rendered

    def handle_event(self, event):
        """Dispatch an untrusted JSONL message or export event."""
        if not isinstance(event, dict):
            raise ChatError("event must be a JSON object")
        operation = event.get("op", "message")
        fields = {"op", "platform", "conversation_id"}
        if operation == "message":
            fields |= {"text", "message_id"}
        elif operation == "export":
            fields |= {"format"}
        else:
            raise ChatError("op must be message or export")
        if event.keys() - fields or not {"platform", "conversation_id"} <= event.keys():
            raise ChatError("event contains unknown fields or lacks platform/conversation_id")
        if operation == "message":
            return self.process_message(event["platform"], event["conversation_id"], event.get("text"),
                                        message_id=event.get("message_id"))
        rendered = self.export_transcript(event["platform"], event["conversation_id"],
                                          format=event.get("format", "json"))
        return {"format": event.get("format", "json"),
                "transcript": json.loads(rendered) if event.get("format", "json") == "json" else rendered}

    def serve(self, input_stream, output_stream, *, transcript_dir=None):
        """Consume JSONL until EOF. Bad events do not terminate other sessions."""
        directory = Path(transcript_dir) if transcript_dir is not None else None
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
        while True:
            line = input_stream.readline(MAX_EVENT_LENGTH + 1)
            if not line:
                return
            try:
                if len(line) > MAX_EVENT_LENGTH:
                    while not line.endswith("\n"):
                        line = input_stream.readline(MAX_EVENT_LENGTH + 1)
                        if not line:
                            break
                    raise ChatError("JSONL event exceeds the length limit")
                event = json.loads(line)
                result = {"ok": True, **self.handle_event(event)}
                if directory is not None and event.get("op", "message") == "message":
                    key = self._key(event["platform"], event["conversation_id"])
                    digest = hashlib.sha256(key[1].encode("utf-8")).hexdigest()
                    self.export_transcript(*key, path=directory / f"{key[0]}-{digest}.json")
            except (ValueError, TypeError, OSError, RecursionError) as error:
                result = {"ok": False, "error": str(error)}
            output_stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
            output_stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sla-url", required=True, help="Published HTTP(S) service terms URL")
    parser.add_argument("--seed", type=int, help="Seed for reproducible delay recommendations")
    parser.add_argument("--transcript-dir", type=Path, help="Save JSON snapshots after every message")
    parser.add_argument("--max-conversations", type=int, default=1000)
    parser.add_argument("--max-turns", type=int, default=200)
    args = parser.parse_args(argv)
    try:
        daemon = ChatAssistantDaemon(args.sla_url, seed=args.seed,
                                     max_conversations=args.max_conversations, max_turns=args.max_turns)
        daemon.serve(sys.stdin, sys.stdout, transcript_dir=args.transcript_dir)
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
