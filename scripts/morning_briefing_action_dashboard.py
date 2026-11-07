#!/usr/bin/env python3
"""Synthesize a solo founder's morning briefing offline, using only stdlib.

    python scripts/morning_briefing_action_dashboard.py --leads jobs.json \
        --opportunities ranked.json --outreach outbound/outbound-plan.json \
        --escrow contract-packet.json --cashflow-config cashflow-config.json \
        --output morning.md

Inputs are the existing tools' exports: aggregator {records: [...]}, indexer
{hits: [...]} or {search: {hits: [...]}}, outbound dispatch plans, escrow
dispute-defense packets, and cashflow simulate() results. Leads/opportunities
also accept arrays or JSONL. Repeat --escrow for multiple contract packets;
an array of packets is accepted too. --cashflow-config runs the existing seeded
cashflow model instead of loading a result (its CLI exports Markdown only).

The Python API is build_briefing(...) -> generate_markdown_report(...).
Inputs are never mutated. No harvesting, sending, clipboard access, ledger
writes, or network calls occur. Missing feeds remain explicitly unknown; an
empty supplied feed means zero records. Latest leads sort by publication time;
opportunities retain the indexer's composite scores and sort descending.
The display limit applies separately to leads, opportunities, and drafts; all
escrow milestones and holds remain visible. Message bodies are copied verbatim.

Escrow packets are checked with the audit tool, then deposit freshness is
rechecked at briefing time. This checks recorded evidence, not a live provider.
Cashflow labels match the model: Stable >=90%, Watch >=50%, Critical <50%
horizon survival. Pending outreach and escrow never count as settled cash.
Exit 0: generated; 1: generated with upstream partial data or invalid escrow
integrity; 2: invalid input/configuration or an I/O error. Omitted feeds and stale
snapshots appear in data notes without changing the exit code.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys

if __package__:
    from . import freelance_contract_escrow_audit as audit
    from . import solo_founder_cashflow_modeler as cashflow_model
    from .commercial_bounty_crawler import canonical_url
else:
    import freelance_contract_escrow_audit as audit
    import solo_founder_cashflow_modeler as cashflow_model
    from commercial_bounty_crawler import canonical_url


CHANNELS = ("email", "linkedin", "wechat", "platform")
SOURCE_LABELS = {"leads": "Latest leads", "opportunities": "Ranked opportunities",
                 "outreach": "Outreach plan", "escrow": "Escrow audit", "cashflow": "Runway model"}
MAX_INPUT_BYTES = 10_000_000


def _number(value, name, *, minimum=0, maximum=None):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} must be a finite number in {minimum}..{maximum}")
    return value


def _text(value, name, *, required=False):
    if not isinstance(value, str) or required and not value.strip():
        raise ValueError(f"{name} must be {'nonempty ' if required else ''}text")
    return value


def _moment(value, name):
    try:
        if isinstance(value, datetime):
            moment = value
        elif type(value) in (int, float):
            _number(value, name)
            moment = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError("expected ISO timestamp")
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return moment
    except (ValueError, OSError, OverflowError) as error:
        raise ValueError(f"invalid {name}: {error}") from error


def _published(value):
    if value in (None, ""):
        return None
    # The aggregator can emit a date without a time; sort it at UTC midnight.
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        value += "T00:00:00Z"
    return _moment(value, "published_at").astimezone(timezone.utc).isoformat()


def _records(payload, name):
    if payload is None:
        return []
    if isinstance(payload, dict):
        if name == "leads":
            payload = payload.get("records")
        else:
            payload = payload.get("search", payload)
            payload = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise ValueError(f"{name} must contain an array of objects")
    return copy.deepcopy(payload)


def _jobs(payload, name):
    merged = {}
    for row in _records(payload, name):
        row["title"] = _text(row.get("title"), "job title", required=True)
        row["url"] = canonical_url(row.get("url"))
        row["published_at"] = _published(row.get("published_at"))
        row["company"] = _text(row.get("company") or "", "company")
        row["source"] = _text(row.get("source") or "", "source")
        _pay(row)
        details = row.get("remote_details") or {}
        if not isinstance(details, dict) or not isinstance(details.get("constraints", []), list) or any(
                not isinstance(item, str) for item in details.get("constraints", [])):
            raise ValueError("remote_details constraints must be an array of strings")
        for key, maximum in (("match_score", 100), ("ranking_score", None)):
            if row.get(key) is not None:
                _number(row[key], key, maximum=maximum)
        screening = row.get("recruiter_screening") or {}
        if not isinstance(screening, dict):
            raise ValueError("recruiter_screening must be an object")
        previous = merged.get(row["url"])
        if previous is None or (row["published_at"] or "") >= (previous["published_at"] or ""):
            merged[row["url"]] = row
    # A rejection in any observation also suppresses a less detailed duplicate.
    rejected = {canonical_url(row.get("url")) for row in _records(payload, name)
                if (isinstance(row.get("recruiter_screening"), dict)
                    and row["recruiter_screening"].get("rejected") is True)
                or row.get("closed") is True or row.get("status") in {"closed", "expired"}}
    rows = [row for url, row in merged.items() if url not in rejected]
    if name == "opportunities":
        rows.sort(key=lambda row: (-(row.get("ranking_score") or 0), -(row.get("match_score") or 0), row["url"]))
    else:
        rows.sort(key=lambda row: (row["published_at"] or "", row["url"]), reverse=True)
    return rows


def _dispatches(payload, now, jobs):
    if payload is None:
        return [], []
    if not isinstance(payload, dict) or not isinstance(payload.get("dispatches"), list):
        raise ValueError("outreach must be an outbound dispatch plan with a dispatches array")
    titles = {row["id"]: row["title"] for row in jobs if isinstance(row.get("id"), str)}
    entries = payload.get("leads", [])
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("outreach leads must be an array of objects")
    for entry in entries:
        lead = entry.get("lead", {})
        if isinstance(lead, dict) and isinstance(lead.get("id"), str):
            titles[lead["id"]] = _text(lead.get("title", lead["id"]), "outreach lead title")
    merged = {}
    statuses = {"ready", "rate_limited", "blocked", "duplicate", "sent", "cancelled", "skipped"}
    for raw in payload["dispatches"]:
        if not isinstance(raw, dict):
            raise ValueError("dispatches must contain objects")
        row = copy.deepcopy(raw)
        identity = _text(row.get("id"), "dispatch id", required=True)
        if row.get("status") not in statuses:
            raise ValueError("unsupported dispatch status")
        merged[identity] = row  # Later terminal states suppress earlier pending copies.
    pending = []
    for row in merged.values():
        if row["status"] not in {"ready", "rate_limited", "blocked"}:
            continue
        if row.get("channel") not in CHANNELS:
            raise ValueError("unsupported outreach channel")
        for key in ("lead_id", "recipient", "body"):
            _text(row.get(key), f"dispatch {key}", required=True)
        row["subject"] = _text(row.get("subject", ""), "dispatch subject")
        row["title"] = titles.get(row["lead_id"], row["lead_id"])
        scheduled = _moment(row["not_before"], "not_before") if row.get("not_before") else None
        row["state"] = (row["status"] if row["status"] != "ready" else
                        "review" if scheduled is None else "scheduled" if scheduled > now else "ready")
        row["not_before"] = scheduled.isoformat() if scheduled else None
        pending.append(row)
    order = {"ready": 0, "review": 1, "scheduled": 2, "rate_limited": 3, "blocked": 4}
    pending.sort(key=lambda row: (order[row["state"]], row["not_before"] or "", row["id"]))
    unavailable = payload.get("unavailable_routes", [])
    if not isinstance(unavailable, list) or any(not isinstance(row, dict) for row in unavailable):
        raise ValueError("unavailable_routes must be an array of objects")
    return pending, copy.deepcopy(unavailable)


def _escrow_packets(payload, now):
    if payload is None:
        return []
    packets = payload if isinstance(payload, list) else [payload]
    if any(not isinstance(packet, dict) for packet in packets):
        raise ValueError("escrow must contain audit packet objects")
    merged = {}
    for packet in packets:
        identity = _text(packet.get("contract_id"), "escrow contract_id", required=True)
        if identity in merged:
            raise ValueError(f"duplicate escrow contract: {identity}")
        valid = audit.verify_dispute_packet(packet)
        row = {"contract_id": identity, "integrity_verified": valid, "deposit_verified": False,
               "reasons": [], "milestones": []}
        merged[identity] = row
        if not valid:
            row["reasons"] = ["packet integrity check failed"]
            continue
        row["reasons"] = list(packet["escrow"]["reasons"])
        evidence = packet["escrow"]["evidence"]
        if packet["generated_at"] > now.timestamp():
            row["reasons"].append("audit packet is dated in the future")
        if evidence and not 0 <= now.timestamp() - evidence["observed_at"] <= packet["contract"]["escrow_max_age_seconds"]:
            row["reasons"].append("deposit evidence is stale or future-dated")
        row["reasons"] = list(dict.fromkeys(row["reasons"]))
        row["deposit_verified"] = packet["escrow"]["verified"] is True and not row["reasons"]
        row["warnings"] = copy.deepcopy(packet["warnings"])
        for milestone in packet["milestones"]:
            terms = milestone["terms"]
            row["milestones"].append({"id": terms["milestone_id"], "title": terms["title"],
                                      "state": "accepted" if milestone["acceptance"] else
                                      "awaiting acceptance" if milestone["deliveries"] else "not delivered"})
    return [merged[key] for key in sorted(merged)]


def _runway(payload):
    if payload is None:
        return None
    if not isinstance(payload, dict) or payload.get("currency") != "CNY":
        raise ValueError("cashflow must be a CNY simulate() result")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("cashflow parameters are required")
    horizon = parameters.get("horizon_days")
    if type(horizon) is not int or horizon < 1:
        raise ValueError("cashflow horizon_days must be a positive integer")
    initial = _number(parameters.get("initial_cash_cny"), "initial_cash_cny")
    burn = _number(payload.get("fixed_burn_per_30_days_cny"), "fixed burn")
    probability = _number(payload.get("survival_probability"), "survival_probability", maximum=1)
    baseline = payload.get("no_income_runway_days")
    if baseline is None and burn != 0:
        raise ValueError("no_income_runway_days may be null only for zero fixed burn")
    if baseline is not None:
        _number(baseline, "no_income_runway_days")
    quantiles = {}
    for name, minimum, maximum in (("runway_days", 0, horizon), ("terminal_cash_cny", None, None)):
        values = payload.get(name)
        if not isinstance(values, dict):
            raise ValueError(f"cashflow {name} must be an object")
        quantiles[name] = {key: _number(values.get(key), f"{name}.{key}", minimum=minimum, maximum=maximum)
                           for key in ("p10", "p50", "p90")}
        if not quantiles[name]["p10"] <= quantiles[name]["p50"] <= quantiles[name]["p90"]:
            raise ValueError(f"cashflow {name} quantiles must be ordered")
    return {"health": "Stable" if probability >= .9 else "Watch" if probability >= .5 else "Critical",
            "horizon_days": horizon, "starting_cash_cny": initial, "burn_cny": burn,
            "survival_probability": probability, "no_income_runway_days": baseline, **quantiles}


def build_briefing(*, leads=None, opportunities=None, outreach=None, escrow=None,
                   cashflow=None, now=None, limit=5, stale_after_hours=24):
    """Normalize upstream snapshots and produce an independent, deterministic dashboard."""
    now = _moment(now if now is not None else datetime.now().astimezone(), "now")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    _number(stale_after_hours, "stale_after_hours", minimum=0.000001)
    inputs = {"leads": leads, "opportunities": opportunities, "outreach": outreach,
              "escrow": escrow, "cashflow": cashflow}
    notes, partial = [], False
    for name, payload in inputs.items():
        label = SOURCE_LABELS[name]
        if payload is None:
            notes.append(f"{label}: not supplied.")
            continue
        if isinstance(payload, dict):
            if payload.get("partial") is True:
                notes.append(f"{label}: upstream reports partial data.")
                partial = True
            if payload.get("generated_at") is not None:
                observed = _moment(payload["generated_at"], f"{name} generated_at")
                if (now - observed).total_seconds() > stale_after_hours * 3600:
                    notes.append(f"{label}: snapshot is older than {stale_after_hours:g} hours.")
                elif observed > now:
                    notes.append(f"{label}: snapshot is dated in the future.")
            search = payload.get("search", payload)
            if name == "opportunities" and isinstance(search, dict) and search.get("error"):
                notes.append("Ranked opportunities: offline fallback was used.")
    latest, ranked = _jobs(leads, "leads"), _jobs(opportunities, "opportunities")
    pending, unavailable = _dispatches(outreach, now, latest + ranked)
    contracts = _escrow_packets(escrow, now)
    runway = _runway(cashflow)
    if any(not contract["integrity_verified"] for contract in contracts):
        partial = True
        notes.append("Escrow audit: packet integrity failed; untrusted milestones are omitted.")
    focus = []
    if runway and runway["health"] != "Stable":
        focus.append(f"{runway['health']} runway: review burn and prioritize settled revenue.")
    for contract in contracts:
        if not contract["deposit_verified"]:
            focus.append(f"Verify escrow evidence for {contract['contract_id']} before starting further work.")
        elif any(milestone["state"] == "awaiting acceptance" for milestone in contract["milestones"]):
            focus.append(f"Request client acceptance for delivered milestones on {contract['contract_id']}.")
    ready = sum(row["state"] == "ready" for row in pending)
    if ready:
        focus.append(f"Review pacing and send {ready} due outreach draft(s).")
    if ranked:
        focus.append(f"Review the top opportunity: {ranked[0]['title']}.")
    if not focus:
        focus.append("Review the available leads and refresh missing inputs.")
    return {"generated_at": now.isoformat(), "limit": limit, "partial": partial,
            "available": {name: payload is not None for name, payload in inputs.items()},
            "leads": latest, "opportunities": ranked, "outreach": pending,
            "unavailable_routes": unavailable, "escrow": contracts, "runway": runway,
            "focus": focus[:3], "notes": notes}


def _md(value):
    """Keep source text on one line and outside Markdown/HTML syntax."""
    value = " ".join(str(value).split())
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", value)


def _link(row):
    url = row["url"].replace("<", "%3C").replace(">", "%3E").replace("\\", "%5C")
    return f"[{_md(row['title'])}](<{url}>)"


def _fence(text):
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}" + ("" if text.endswith("\n") else "\n") + fence


def _pay(row):
    pay = row.get("compensation") or row.get("budget") or row
    if not isinstance(pay, dict):
        raise ValueError("compensation must be an object")
    low, high = pay.get("amount_min"), pay.get("amount_max")
    if low is None and high is None:
        return "Pay undisclosed"
    for value in (low, high):
        if value is not None:
            _number(value, "compensation amount")
    if low is not None and high is not None and low > high:
        raise ValueError("compensation minimum exceeds maximum")
    currency = _text(pay.get("currency"), "compensation currency", required=True)
    amount = (f"{low:,.2f}–{high:,.2f}" if low is not None and high is not None and low != high else
              f"{low:,.2f}" if low == high else f"from {low:,.2f}" if low is not None else f"up to {high:,.2f}")
    return f"{currency} {amount}/{pay.get('period') or 'unknown'}"


def generate_markdown_report(briefing):
    """Render a compact briefing, preserving raw subject/body text inside fences."""
    limit, available = briefing["limit"], briefing["available"]
    moment = _moment(briefing["generated_at"], "generated_at")
    lines = [f"# Morning briefing — {moment:%Y-%m-%d}", "",
             f"As of {moment.isoformat(timespec='seconds')}", "", "## Today's focus", ""]
    lines.extend(f"- [ ] {_md(action)}" for action in briefing["focus"])
    lines += ["", "## Runway health", ""]
    runway = briefing["runway"]
    if runway is None:
        lines.append("**Unknown** — supply a cashflow result or configuration.")
    else:
        baseline = runway["no_income_runway_days"]
        median = runway["runway_days"]["p50"]
        median_text = f"≥ {runway['horizon_days']} days" if median == runway["horizon_days"] else f"{median:,.1f} days"
        lines += [f"**{runway['health']}** · {runway['horizon_days']}-day survival: **{runway['survival_probability']:.1%}**", "",
                  f"- Model starting cash: CNY {runway['starting_cash_cny']:,.2f}; fixed burn / 30 days: CNY {runway['burn_cny']:,.2f}.",
                  "- No-income runway: " + (f"{baseline:,.1f} days." if baseline is not None else "Unlimited at zero fixed burn."),
                  f"- Median funded runway: {median_text} (horizon-capped).",
                  "- Terminal cash P10 / median / P90: " + " / ".join(
                      f"CNY {runway['terminal_cash_cny'][key]:,.2f}" for key in ("p10", "p50", "p90")) + ".", "",
                  "Model estimates use supplied income assumptions. Outreach and escrow balances are not settled cash."]
    for name, heading in (("opportunities", "Ranked opportunities"), ("leads", "Latest leads")):
        rows = briefing[name]
        lines += ["", f"## {heading}", ""]
        if not available[name]:
            lines.append("Not supplied.")
        elif not rows:
            lines.append("No opportunities." if name == "opportunities" else "No new leads.")
        for row in rows[:limit]:
            details = list(filter(None, [row["company"], row["source"], _pay(row)]))
            if name == "opportunities":
                details.append(f"rank {row['ranking_score']:.2f}" if row.get("ranking_score") is not None else "rank unavailable")
                if row.get("match_score") is not None:
                    details.append(f"match {row['match_score']:g}/100")
            else:
                details.append(row["published_at"][:10] if row["published_at"] else "publication date unknown")
            lines.append(f"- {_link(row)} — " + " · ".join(_md(detail) for detail in details))
            remote = row.get("remote_details") or {}
            constraints = remote.get("constraints", []) if isinstance(remote, dict) else []
            if constraints:
                lines.append("  Restrictions: " + _md(", ".join(constraints)))
        if len(rows) > limit:
            lines += ["", f"Showing {limit} of {len(rows)}."]
    pending = briefing["outreach"]
    lines += ["", "## Outreach action dashboard", ""]
    if not available["outreach"]:
        lines.append("Not supplied.")
    elif not pending:
        lines.append("No pending outreach drafts.")
    else:
        lines += ["| Channel | Due for review | Scheduled | Review schedule | On hold |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for channel in CHANNELS:
            rows = [row for row in pending if row["channel"] == channel]
            if rows:
                counts = [sum(row["state"] == state for row in rows) for state in ("ready", "scheduled", "review")]
                counts.append(sum(row["state"] in {"blocked", "rate_limited"} for row in rows))
                lines.append(f"| {channel} | " + " | ".join(map(str, counts)) + " |")
        labels = {"ready": "Due for review", "scheduled": "Scheduled", "review": "Review schedule",
                  "rate_limited": "On hold: rate limited", "blocked": "On hold: blocked"}
        for row in pending[:limit]:
            lines += ["", f"### {_md(row['title'])} · {row['channel']}", "",
                      f"**{labels[row['state']]}** · To: {_md(row['recipient'])}"]
            if row["not_before"]:
                lines.append(f"Not before: {row['not_before']}")
            if row["state"] in {"blocked", "rate_limited"}:
                lines.append("Refresh the dispatch plan after reviewing the hold.")
            else:
                message = (row["subject"] + "\n\n" if row["subject"] else "") + row["body"]
                lines += ["", _fence(message)]
        if len(pending) > limit:
            lines += ["", f"Showing {limit} of {len(pending)} pending drafts."]
        # Holds must remain visible even when message previews are truncated.
        for row in pending[limit:]:
            if row["state"] in {"blocked", "rate_limited"}:
                lines.append(f"- {labels[row['state']]}: {_md(row['title'])} · {row['channel']} · {_md(row['recipient'])}.")
    if briefing["unavailable_routes"]:
        lines += ["", f"Missing contact routes: {len(briefing['unavailable_routes'])}. Add explicit contacts in the outreach plan."]
    lines += ["", "## Escrow and milestones", ""]
    if not available["escrow"]:
        lines.append("Not supplied.")
    elif not briefing["escrow"]:
        lines.append("No contracts supplied.")
    for contract in briefing["escrow"]:
        status = "Deposit verified from recorded evidence" if contract["deposit_verified"] else "Deposit unverified"
        lines += [f"- **{_md(contract['contract_id'])}: {status}.**"]
        if contract["reasons"]:
            lines.append("  Action: " + _md("; ".join(contract["reasons"])) + ".")
        for milestone in contract["milestones"]:
            lines.append(f"  - {_md(milestone['title'])} ({_md(milestone['id'])}): {milestone['state']}.")
        for warning in contract.get("warnings", []):
            if warning in {"no_milestones", "milestone_allocation_incomplete"}:
                lines.append("  Action: " + _md(warning.replace("_", " ")) + ".")
    if briefing["notes"]:
        lines += ["", "## Data notes", ""] + [f"- {_md(note)}" for note in briefing["notes"]]
    return "\n".join(lines) + "\n"


def _reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def read_payload(path, *, jsonl=False):
    """Read bounded UTF-8 JSON; JSONL is allowed only for record feeds."""
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds {MAX_INPUT_BYTES} bytes: {path}")
    content = raw.decode("utf-8-sig")
    if jsonl and Path(path).suffix.lower() == ".jsonl":
        return [json.loads(line, parse_constant=_reject_constant) for line in content.splitlines() if line.strip()]
    return json.loads(content, parse_constant=_reject_constant)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("leads", "opportunities", "outreach"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--escrow", type=Path, action="append", default=[])
    financial = parser.add_mutually_exclusive_group()
    financial.add_argument("--cashflow", type=Path, help="JSON simulate() result")
    financial.add_argument("--cashflow-config", type=Path, help="run the cashflow model with this JSON config")
    parser.add_argument("--now", help="timezone-aware ISO timestamp; defaults to local time")
    parser.add_argument("--limit", type=int, default=5, help="maximum entries per lead/draft section")
    parser.add_argument("--stale-after-hours", type=float, default=24)
    parser.add_argument("--output", type=Path, help="UTF-8 Markdown destination; defaults to stdout")
    args = parser.parse_args(argv)
    try:
        inputs = {name: read_payload(getattr(args, name), jsonl=name != "outreach")
                  if getattr(args, name) else None for name in ("leads", "opportunities", "outreach")}
        packets = []
        for path in args.escrow:
            payload = read_payload(path)
            packets.extend(payload if isinstance(payload, list) else [payload])
        financial_result = (cashflow_model.simulate(cashflow_model.config_from_dict(read_payload(args.cashflow_config)))
                            if args.cashflow_config else read_payload(args.cashflow) if args.cashflow else None)
        briefing = build_briefing(**inputs, escrow=packets if args.escrow else None, cashflow=financial_result,
                                  now=args.now, limit=args.limit, stale_after_hours=args.stale_after_hours)
        output = generate_markdown_report(briefing)
        if args.output:
            input_paths = [getattr(args, name) for name in ("leads", "opportunities", "outreach", "cashflow", "cashflow_config")]
            if args.output.resolve() in {path.resolve() for path in input_paths + args.escrow if path}:
                raise ValueError("output must not overwrite an input file")
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 1 if briefing["partial"] else 0
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError) as error:
        print(f"morning_briefing_action_dashboard: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
