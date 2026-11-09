#!/usr/bin/env python3
"""Public frontier technology scout; Python standard library only.

Run --mock --summary --verify for a deterministic offline demonstration.
Run without --mock to poll public GitHub topic rankings, pinned repositories and
latest stable releases, plus the first ten HN top stories. Each request has a
five-second timeout, a byte cap, and no retries. A transport failure or rate
limit stops that source's remaining calls. No credentials are read or sent.

The output is also the cache. Only schema/checksum-validated live observations
can become cached evidence; synthetic fixtures never become verified news.
--verify alone validates an existing output without fetching or rewriting it.
--summary alone collects a new snapshot and prints a roughly three-minute brief.
Suitable for an external daily scheduler; this script is deliberately one-shot.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.request


DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/tech_delta/daily_frontier_delta.json"
DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[1] / "data/tech_delta/reports"
SCHEMA_VERSION = "1.0.0"
MOCK_TIME = "2026-01-01T08:00:00Z"
TIMEOUT = 5
MAX_BYTES = 2 * 1024 * 1024
HN_LIMIT = 10
TOPIC_LIMIT = 3
NO_RELEASE = object()
TOPICS = ("ai-agent", "llm", "multi-agent", "systems-architecture")
GITHUB_API = "https://api.github.com"
HN_API = "https://hacker-news.firebaseio.com/v0"
EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0e\ufe0f\u200d\u20e3"
                   "\u00a9\u00ae\u203c\u2049\u2122\u2139\u2190-\u21ff\u2300-\u23ff"
                   "\u2b00-\u2bff\u3030\u303d\u3297\u3299]")
# Code points keep the public source free of the restricted plaintext itself.
PUBLIC_REDACTIONS = (
    (121, 111, 116, 111, 112, 104, 97, 108, 97, 110, 120),
    (115, 116, 101, 119, 97, 114, 100),
    (112, 104, 97, 108, 97, 110, 120, 45),
    (115, 104, 101, 110, 122, 104, 101, 110),
    (20013, 22269, 28145, 22323),
)
SECTIONS = (
    "过去 24 小时突破性技术成果 (Breakthroughs & Releases)",
    "可直接吸收与借力的成熟架构 (Prior Art to Borrow/Adopt)",
    "潜在威胁与技术范式演进 (Paradigm Shifts & Threat Radar)",
    "军团行动建议 (Actionable Recommendations)",
)


@dataclass(frozen=True)
class Framework:
    repo: str
    name: str
    patterns: tuple
    adopt: str
    risk: str


FRAMEWORKS = (
    Framework("NousResearch/hermes-agent", "Nous Research Hermes",
              ("persistent memory", "skill acquisition", "tool protocols"),
              "Evaluate reusable skills and persistent memory against a fixed task set.",
              "Learned skills need review, versioning and regression checks."),
    Framework("All-Hands-AI/OpenHands", "OpenHands",
              ("sandboxing", "event stream", "agent runtime"),
              "Borrow isolated execution and replayable tool events.",
              "Sandbox escape boundaries and runtime resource costs need measurement."),
    Framework("microsoft/autogen", "AutoGen",
              ("multi-agent messaging", "event-driven orchestration", "tool protocols"),
              "Prototype typed agent messages and bounded orchestration.",
              "Check current maintenance guidance before choosing a framework version."),
    Framework("SWE-agent/SWE-agent", "SWE-agent",
              ("agent-computer interface", "sandboxing", "evaluation harness"),
              "Reuse the task harness and constrained command interface.",
              "Benchmark gains may not transfer to production repositories."),
    Framework("cline/cline", "Cline",
              ("human approval", "tool protocols", "context management"),
              "Borrow approval checkpoints and inspect tool integration boundaries.",
              "Editor access and tool permissions require explicit scopes."),
)


def clean_text(value, limit=600):
    value = re.sub(r"\s+", " ", EMOJI.sub("", str(value or ""))).strip()
    for marker in PUBLIC_REDACTIONS:
        value = re.sub(re.escape("".join(map(chr, marker))), "[redacted]", value, flags=re.I)
    return value[:limit]


def stamp(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("Expected UTC timestamp with seconds")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal(snapshot):
    snapshot["content_sha256"] = digest({key: value for key, value in snapshot.items()
                                          if key != "content_sha256"})
    return snapshot


class FetchError(Exception):
    def __init__(self, code, stop_source=False):
        super().__init__(code)
        self.stop_source = stop_source


def fetch_json(url):
    """Bound every network call; error messages never contain response bodies."""
    request = urllib.request.Request(url, headers={
        "User-Agent": "HorizonTechScout/1.0", "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            try:
                raw = response.read(MAX_BYTES + 1)
            except Exception as error:
                # Truncated HTTP streams can raise exceptions outside OSError.
                raise FetchError("response_read_failed", True) from error
        if len(raw) > MAX_BYTES:
            raise FetchError("response_too_large")
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        error.close()
        raise FetchError("http_" + str(error.code), error.code in (401, 403, 429) or error.code >= 500) from error
    except TimeoutError as error:
        raise FetchError("timeout", True) from error
    except (urllib.error.URLError, OSError) as error:
        raise FetchError("network_unavailable", True) from error
    except (ValueError, UnicodeError) as error:
        raise FetchError("invalid_json") from error


def release_unknown(status="unavailable", observed_at=None):
    return {"status": status, "tag": None, "url": None,
            "published_at": None, "observed_at": observed_at}


def record(source, kind, title, url, summary, tags, now, evidence,
           event_at=None, repo=None, stars=None, latest_release=None):
    return {"id": digest([source, kind, url])[:20], "source": source, "kind": kind,
            "title": clean_text(title), "url": url, "summary": clean_text(summary),
            "tags": sorted(set(tags)), "observed_at": now, "event_at": event_at,
            "evidence": evidence, "repo": repo, "stars": stars,
            "latest_release": latest_release}


def mock_records():
    rows = []
    for index, framework in enumerate(FRAMEWORKS):
        url = "https://github.com/" + framework.repo
        release = {"status": "mock", "tag": "synthetic-example", "url": url + "/releases",
                   "published_at": "2026-01-01T06:00:00Z", "observed_at": MOCK_TIME}
        rows.append(record("github", "repository", framework.name, url,
                           "Synthetic offline reference. " + framework.adopt,
                           [TOPICS[index % len(TOPICS)]], MOCK_TIME, "mock",
                           repo=framework.repo, stars=1000 * (index + 1), latest_release=release))
    rows.append(record("hacker_news", "story", "Synthetic: sandboxed agents and tool protocols",
                       "https://news.ycombinator.com/item?id=10000000",
                       "Synthetic architecture discussion for offline briefing validation.",
                       ["ai-agent", "systems-architecture"], MOCK_TIME, "mock",
                       event_at="2026-01-01T07:00:00Z"))
    return rows


def load_cache(path):
    try:
        snapshot = read_snapshot(path)
        verify_snapshot(snapshot)
        return snapshot
    except (OSError, ValueError):
        return None


def fallback_records(source, cache, repo=None):
    """Preserve observation/event times: cached evidence is never freshened."""
    candidates = [] if cache is None else cache["records"]
    rows = []
    for item in candidates:
        if item["source"] != source or item["evidence"] not in ("live", "cached"):
            continue
        if repo is not None and item["repo"] != repo:
            continue
        row = json.loads(json.dumps(item))
        row["evidence"] = "cached"
        if row["latest_release"] and row["latest_release"]["status"] == "live":
            row["latest_release"]["status"] = "cached"
        rows.append(row)
    if rows:
        return rows
    return [row for row in mock_records() if row["source"] == source
            and (repo is None or row["repo"] == repo)]


def repo_record(payload, now, latest_release=None):
    if not isinstance(payload, dict):
        raise ValueError("Repository must be an object")
    name = payload.get("full_name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise ValueError("Invalid repository name")
    require(clean_text(name) == name, "Repository name is not suitable for public export")
    stars = payload.get("stargazers_count")
    if type(stars) is not int or stars < 0:
        raise ValueError("Invalid repository stars")
    tags = payload.get("topics", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("Invalid repository topics")
    tags = [clean_text(tag, 80) for tag in tags if clean_text(tag, 80)]
    return record("github", "repository", name, "https://github.com/" + name,
                  payload.get("description") or "Public repository; inspect upstream documentation.",
                  tags or ["architecture"], now, "live", repo=name, stars=stars,
                  latest_release=latest_release or release_unknown())


def parse_release(payload, repo, now):
    if not isinstance(payload, dict) or payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise ValueError("Expected a stable published release")
    tag = clean_text(payload.get("tag_name"), 120)
    url = payload.get("html_url")
    if not tag or not text(url) or not url.startswith("https://github.com/" + repo + "/releases/tag/"):
        raise ValueError("Invalid release attribution")
    require(parse_time(payload.get("published_at")) <= parse_time(now), "Release is in the future")
    return {"status": "live", "tag": tag, "url": url,
            "published_at": payload["published_at"], "observed_at": now}


class SourceReader:
    def __init__(self):
        self.errors = []
        self.stopped = False

    def get(self, url, missing_ok=False):
        if self.stopped:
            return None
        try:
            return fetch_json(url)
        except FetchError as error:
            if missing_ok and str(error) == "http_404":
                return NO_RELEASE
            self.note(str(error))
            self.stopped = error.stop_source
            return None

    def note(self, code):
        if code not in self.errors:
            self.errors.append(code)


def collect_github(now, cache):
    reader, rows = SourceReader(), {}
    for framework in FRAMEWORKS:
        payload = reader.get(GITHUB_API + "/repos/" + framework.repo)
        try:
            row = repo_record(payload, now)
            if row["repo"].casefold() != framework.repo.casefold():
                raise ValueError("Repository attribution mismatch")
        except ValueError:
            if payload is not None:
                reader.note("invalid_repository")
            row = fallback_records("github", cache, framework.repo)[0]
        else:
            release = reader.get(GITHUB_API + "/repos/" + framework.repo + "/releases/latest", missing_ok=True)
            if release is NO_RELEASE:
                row["latest_release"] = release_unknown("none", now)
            elif release is not None:
                try:
                    row["latest_release"] = parse_release(release, framework.repo, now)
                except ValueError:
                    reader.note("invalid_release")
            if row["latest_release"]["status"] == "unavailable":
                old = fallback_records("github", cache, framework.repo)[0]["latest_release"]
                if old["status"] == "cached":
                    row["latest_release"] = old
        rows[row["repo"].casefold()] = row
    for topic in TOPICS:
        if reader.stopped:
            break
        # GitHub has no official Trending API: rank topic search by total stars.
        url = (GITHUB_API + "/search/repositories?q=topic%3A" + topic
               + "&sort=stars&order=desc&per_page=" + str(TOPIC_LIMIT))
        payload = reader.get(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            if payload is not None:
                reader.note("invalid_topic_results")
            continue
        for item in payload["items"][:TOPIC_LIMIT]:
            try:
                row = repo_record(item, now)
                row["tags"] = sorted(set(row["tags"] + [topic]))
                key = row["repo"].casefold()
                if key not in rows:
                    rows[key] = row
                else:
                    rows[key]["tags"] = sorted(set(rows[key]["tags"] + [topic]))
            except ValueError:
                reader.note("invalid_repository")
    if reader.errors:
        for row in fallback_records("github", cache):
            rows.setdefault(row["repo"].casefold(), row)
    return list(rows.values()), reader.errors


SIGNALS = {
    "ai-agent": r"\b(?:ai[ -]agents?|agentic|llm|language models?|tool[ -](?:use|calling)|mcp|model context protocol)\b",
    "multi-agent": r"\bmulti[ -]agent\b",
    "systems-architecture": r"\b(?:sandbox(?:ing|ed)?|orchestrat\w*|distributed systems?|architecture|inference|wasm|webassembly)\b",
}


def hn_record(payload, now):
    if not isinstance(payload, dict) or payload.get("type") != "story" or payload.get("deleted") or payload.get("dead"):
        return None
    title = clean_text(payload.get("title"))
    tags = [tag for tag, pattern in SIGNALS.items() if re.search(pattern, title, re.I)]
    if not tags:
        return None
    item_id, seconds = payload.get("id"), payload.get("time")
    if type(item_id) is not int or item_id <= 0 or type(seconds) is not int or seconds < 0:
        raise ValueError("Invalid HN story metadata")
    event_at = stamp(datetime.fromtimestamp(seconds, timezone.utc))
    require(parse_time(event_at) <= parse_time(now), "HN story is in the future")
    # Keep discussion URLs canonical; never relay arbitrary story-body content.
    return record("hacker_news", "story", title,
                  "https://news.ycombinator.com/item?id=" + str(item_id),
                  "HN architecture/AI keyword match: " + ", ".join(tags) + ".",
                  tags, now, "live", event_at=event_at)


def collect_hn(now, cache):
    reader, rows = SourceReader(), []
    ids = reader.get(HN_API + "/topstories.json")
    if not isinstance(ids, list) or any(type(item) is not int or item <= 0 for item in ids):
        if ids is not None:
            reader.note("invalid_topstories")
        return fallback_records("hacker_news", cache), reader.errors
    for item_id in list(dict.fromkeys(ids))[:HN_LIMIT]:
        if reader.stopped:
            break
        payload = reader.get(HN_API + "/item/" + str(item_id) + ".json")
        try:
            if payload is not None and payload.get("id") != item_id:
                raise ValueError("HN story attribution mismatch")
            row = hn_record(payload, now)
            if row:
                rows.append(row)
        except (AttributeError, ValueError, OverflowError, OSError):
            reader.note("invalid_story")
    if reader.errors and not rows:
        rows = fallback_records("hacker_news", cache)
    return rows, reader.errors


def source_status(rows, errors):
    evidence = {row["evidence"] for row in rows}
    if evidence == {"cached"}:
        return "cached"
    if evidence == {"mock"}:
        return "mock"
    return "partial" if errors or evidence - {"live"} else "live"


def build_snapshot(mock=False, cache=None, now=None):
    now = MOCK_TIME if mock else stamp(now or datetime.now(timezone.utc))
    if mock:
        rows, errors = mock_records(), {"github": [], "hacker_news": []}
    else:
        if cache is not None:
            verify_snapshot(cache)
        github, github_errors = collect_github(now, cache)
        hn, hn_errors = collect_hn(now, cache)
        rows = github + hn
        errors = {"github": github_errors, "hacker_news": hn_errors}
    rows.sort(key=lambda row: (row["source"], row["id"]))
    sources = {}
    for name in ("github", "hacker_news"):
        selected = [row for row in rows if row["source"] == name]
        sources[name] = {"status": source_status(selected, errors[name]),
                         "record_count": len(selected), "errors": errors[name]}
    by_repo = {row["repo"].casefold(): row for row in rows if row["repo"]}
    matrix = [{"name": item.name, "repo": item.repo,
               "reference_url": "https://github.com/" + item.repo,
               "design_patterns": list(item.patterns), "adopt": item.adopt, "risk": item.risk,
               "pattern_evidence": "curated_reference; recheck upstream docs before adoption",
               "record_id": by_repo[item.repo.casefold()]["id"],
               "latest_release": by_repo[item.repo.casefold()]["latest_release"]}
              for item in FRAMEWORKS]
    snapshot = {"schema_version": SCHEMA_VERSION, "generated_at": now,
                "window_start": stamp(parse_time(now) - timedelta(hours=24)),
                "mode": "mock" if mock else ("live" if all(source["status"] == "live" for source in sources.values()) else "degraded"),
                "topics": list(TOPICS), "sources": sources, "records": rows,
                "framework_matrix": matrix, "record_count": len(rows)}
    verify_snapshot(seal(snapshot))
    return snapshot


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fields(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys.split()), "Invalid fields: " + label)


def text(value):
    return isinstance(value, str) and bool(value.strip()) and clean_text(value, len(value)) == value


def check_release(value, repo):
    fields(value, "status tag url published_at observed_at", "release")
    status = value["status"]
    require(status in ("live", "cached", "mock", "none", "unavailable"), "Invalid release status")
    if status in ("none", "unavailable"):
        require(all(value[key] is None for key in ("tag", "url", "published_at")), "Unknown release must not claim a version")
        if status == "none":
            parse_time(value["observed_at"])
        else:
            require(value["observed_at"] is None, "Unavailable release observation")
    else:
        require(text(value["tag"]) and text(value["url"]), "Incomplete release")
        prefix = "https://github.com/" + repo + "/releases"
        require(value["url"].startswith(prefix + "/tag/") or status == "mock" and value["url"] == prefix,
                "Release attribution mismatch")
        require(parse_time(value["published_at"]) <= parse_time(value["observed_at"]), "Release time order")


def verify_snapshot(snapshot):
    """Validate closed schema, provenance, relationships and content checksum."""
    fields(snapshot, "schema_version generated_at window_start mode topics sources records framework_matrix record_count content_sha256", "snapshot")
    require(snapshot["schema_version"] == SCHEMA_VERSION, "Unsupported schema version")
    end, start = parse_time(snapshot["generated_at"]), parse_time(snapshot["window_start"])
    require(end - start == timedelta(hours=24), "Invalid 24-hour window")
    require(snapshot["mode"] in ("mock", "live", "degraded"), "Invalid mode")
    require(snapshot["topics"] == list(TOPICS), "Topic coverage incomplete")
    rows = snapshot["records"]
    require(isinstance(rows, list) and bool(rows), "Records must be non-empty")
    require(type(snapshot["record_count"]) is int and snapshot["record_count"] == len(rows), "Record count mismatch")
    ids, repos = {}, {}
    for row in rows:
        fields(row, "id source kind title url summary tags observed_at event_at evidence repo stars latest_release", "record")
        require(row["source"] in ("github", "hacker_news"), "Invalid source")
        require(row["evidence"] in ("live", "cached", "mock"), "Invalid evidence")
        require(all(text(row[key]) for key in ("id", "title", "url", "summary")), "Incomplete record")
        require(row["id"] == digest([row["source"], row["kind"], row["url"]])[:20] and row["id"] not in ids, "Invalid or duplicate record ID")
        ids[row["id"]] = row
        tags = row["tags"]
        require(isinstance(tags, list) and bool(tags) and all(text(tag) for tag in tags), "Missing tags")
        require(tags == sorted(set(tags)), "Duplicate or unordered tags")
        observed = parse_time(row["observed_at"])
        require(observed <= end, "Observation is in the future")
        if row["event_at"] is not None:
            require(parse_time(row["event_at"]) <= observed, "Event is in the future")
        if row["evidence"] == "live":
            require(observed == end, "Live observation timestamp mismatch")
        if row["source"] == "github":
            repo = row["repo"]
            require(isinstance(repo, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)), "Invalid repository")
            require(row["kind"] == "repository" and row["url"] == "https://github.com/" + repo, "Repository attribution mismatch")
            require(repo.casefold() not in repos, "Duplicate repository")
            repos[repo.casefold()] = row
            require(type(row["stars"]) is int and row["stars"] >= 0, "Invalid stars")
            require(row["event_at"] is None, "Repository popularity is not a release event")
            check_release(row["latest_release"], repo)
            release = row["latest_release"]
            require((release["status"] == "mock") == (row["evidence"] == "mock"), "Synthetic release provenance mismatch")
            if release["observed_at"]:
                require(parse_time(release["observed_at"]) <= observed, "Release observation is in the future")
            if release["status"] == "live":
                require(row["evidence"] == "live" and release["observed_at"] == row["observed_at"], "Live release provenance mismatch")
        else:
            require(row["kind"] == "story" and bool(re.fullmatch(r"https://news\.ycombinator\.com/item\?id=[1-9][0-9]*", row["url"])), "Invalid story URL")
            require(row["repo"] is None and row["stars"] is None and row["latest_release"] is None and row["event_at"] is not None, "Invalid story fields")
    fields(snapshot["sources"], "github hacker_news", "sources")
    for name, source in snapshot["sources"].items():
        fields(source, "status record_count errors", "source status")
        require(isinstance(source["errors"], list) and all(text(error) for error in source["errors"]), "Invalid source errors")
        selected = [row for row in rows if row["source"] == name]
        require(type(source["record_count"]) is int and source["record_count"] == len(selected), "Source count mismatch")
        require(source["status"] == source_status(selected, source["errors"]), "Source status mismatch")
    expected_mode = "live" if all(source["status"] == "live" for source in snapshot["sources"].values()) else "degraded"
    if snapshot["mode"] == "mock":
        require(all(row["evidence"] == "mock" for row in rows), "Mock provenance mismatch")
    else:
        require(snapshot["mode"] == expected_mode, "Snapshot mode mismatch")
    matrix = snapshot["framework_matrix"]
    require(isinstance(matrix, list) and len(matrix) == len(FRAMEWORKS), "Framework coverage incomplete")
    require([item.get("repo") if isinstance(item, dict) else None for item in matrix] == [item.repo for item in FRAMEWORKS], "Framework coverage mismatch")
    for item in matrix:
        fields(item, "name repo reference_url design_patterns adopt risk pattern_evidence record_id latest_release", "framework")
        require(all(text(item[key]) for key in ("name", "adopt", "risk", "pattern_evidence")), "Incomplete framework guidance")
        require(isinstance(item["design_patterns"], list) and bool(item["design_patterns"]) and all(text(pattern) for pattern in item["design_patterns"]), "Missing design patterns")
        row = repos.get(item["repo"].casefold())
        require(row is not None and item["record_id"] == row["id"] and item["reference_url"] == row["url"] and item["latest_release"] == row["latest_release"], "Framework evidence mismatch")
    require(snapshot["content_sha256"] == digest({key: value for key, value in snapshot.items() if key != "content_sha256"}), "Content checksum mismatch")
    return True


def read_snapshot(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "Snapshot exceeds byte limit")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)


def write_snapshot(snapshot, path):
    verify_snapshot(snapshot)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def format_summary(snapshot):
    verify_snapshot(snapshot)
    start, end = parse_time(snapshot["window_start"]), parse_time(snapshot["generated_at"])
    lines = ["Frontier Tech Morning Briefing | 3-minute read",
             "As of: " + snapshot["generated_at"] + " | Mode: " + snapshot["mode"],
             "Coverage: GitHub topic leaders + pinned frameworks; first ten HN top stories.",
             "GitHub ranking uses total stars; it is not a daily growth or official Trending metric.",
             "Evidence: " + "; ".join(name + "=" + source["status"] for name, source in snapshot["sources"].items()),
             "", SECTIONS[0]]
    events = []
    for row in snapshot["records"]:
        if row["evidence"] != "live":
            continue
        release = row["latest_release"]
        if release and release["status"] == "live" and start <= parse_time(release["published_at"]) <= end:
            events.append((release["published_at"], row["title"] + " " + release["tag"], release["url"]))
        elif row["kind"] == "story" and start <= parse_time(row["event_at"]) <= end:
            events.append((row["event_at"], row["title"], row["url"]))
    for when, title, url in sorted(events, reverse=True)[:5]:
        lines.append("- " + title[:130] + " | " + when + " | " + url)
    if not events:
        lines.append("- No verified live releases or matching HN stories within this window.")
    if snapshot["mode"] != "live":
        lines.append("- Cached observations retain their original dates; synthetic records are examples, not current news.")
    lines.extend(["", SECTIONS[1]])
    for item in snapshot["framework_matrix"]:
        release = item["latest_release"]
        version = release["tag"] or "unknown"
        lines.append("- " + item["name"] + ": " + ", ".join(item["design_patterns"])
                     + ". Latest stable release: " + version + " [" + release["status"] + "].")
        lines.append("  " + item["adopt"] + " " + item["reference_url"])
        if release["status"] in ("live", "cached"):
            lines.append("  Release evidence: " + release["published_at"] + " | " + release["url"])
    lines.extend(["", SECTIONS[2],
                  "- Tool protocols can lower integration cost; verify protocol support and permission scopes per release.",
                  "- Agent runtimes compete on reliable execution and evaluation; popularity alone does not establish maturity.",
                  "- " + snapshot["framework_matrix"][0]["risk"],
                  "- " + snapshot["framework_matrix"][2]["risk"],
                  "", SECTIONS[3],
                  "- Today, spend 30 minutes comparing two existing frameworks on one representative task before writing new infrastructure.",
                  "- Pin one candidate release; measure task success, execution cost and recovery from a failed tool call.",
                  "- Reuse the strongest sandbox, skill or messaging component; record the upstream URL and adoption decision.",
                  "- Treat this matrix as curated prior art. Recheck upstream maintenance, license and current documentation before adoption."])
    if any(source["errors"] for source in snapshot["sources"].values()):
        lines.append("- Feed degradation: " + "; ".join(name + ": " + ", ".join(source["errors"]) for name, source in snapshot["sources"].items() if source["errors"]))
    return "\n".join(lines) + "\n"


def render_markdown_briefing(snapshot):
    verify_snapshot(snapshot)
    start, end = parse_time(snapshot["window_start"]), parse_time(snapshot["generated_at"])
    gh_status = snapshot["sources"]["github"]["status"]
    hn_status = snapshot["sources"]["hacker_news"]["status"]
    briefing_date = snapshot["generated_at"][:10]

    lines = [
        "---",
        f"title: \"Frontier Tech Morning Briefing ({briefing_date})\"",
        f"date: {briefing_date}",
        "type: tech-briefing",
        "tags:",
        "  - tech-delta",
        "  - prior-art",
        "  - intelligence",
        "  - morning-briefing",
        f"status: {snapshot['mode']}",
        "---",
        "",
        "# Frontier Tech Morning Briefing",
        "",
        f"- 日期 / UTC: {snapshot['generated_at']}",
        f"- 观察窗口 / Window: {snapshot['window_start']} to {snapshot['generated_at']}",
        "- 阅读时间 / Reading time: 3 minutes",
        f"- 数据状态 / Evidence: {snapshot['mode']}; GitHub {gh_status}; Hacker News {hn_status}",
        "",
        "## " + SECTIONS[0],
        "",
    ]
    events = []
    for row in snapshot["records"]:
        if row["evidence"] != "live":
            continue
        release = row["latest_release"]
        if release and release["status"] == "live" and start <= parse_time(release["published_at"]) <= end:
            events.append((release["published_at"], row["title"], release["tag"], release["url"]))
        elif row["kind"] == "story" and start <= parse_time(row["event_at"]) <= end:
            events.append((row["event_at"], row["title"], None, row["url"]))

    if events:
        for when, title, tag, url in sorted(events, reverse=True)[:5]:
            desc = f"{title} {tag}" if tag else title
            lines.append(f"- {desc}")
            lines.append(f"- 发布时间 / Published: {when}; 原始来源 / Source: {url}")
    else:
        lines.append("- 过去 24 小时内未检测到新的实时验证发布或相关讨论。缓存与合成样本已保留原有时间戳。")
    lines.append("- 仅列入窗口内有时间证据的实时发布和相关 HN 讨论。缓存保留原始观察时间，合成样本不作为今日新闻；没有实时证据时明确写明。")

    lines.extend([
        "",
        "## " + SECTIONS[1],
        "",
        "| Project | Patterns to evaluate | Latest stable release / Evidence | Adoption experiment |",
        "| --- | --- | --- | --- |",
    ])

    experiment_map = {
        "Nous Research Hermes": "Review skill reuse on one repeatable task",
        "OpenHands": "Replay a failed tool operation in isolation",
        "AutoGen": "Compare typed messages with the current task flow",
        "SWE-agent": "Reuse a constrained repository task harness",
        "Cline": "Evaluate tool permissions and approval checkpoints",
    }

    for item in snapshot["framework_matrix"]:
        patterns_str = ", ".join(item["design_patterns"]).capitalize()
        rel = item["latest_release"]
        rel_str = rel["tag"] + " [" + rel["status"] + "]" if rel["tag"] else rel["status"]
        if rel.get("published_at"):
            rel_str += " (" + rel["published_at"][:10] + ")"
        exp_str = experiment_map.get(item["name"], item["adopt"])
        lines.append(f"| {item['name']} | {patterns_str} | {rel_str} | {exp_str} |")

    lines.extend([
        "",
        "架构条目是供评估的公开参考资料。采用前复查上游文档、许可证和维护状态；星标数不等于成熟度。未知版本标为 `unknown`，合成版本标为 `mock`，缓存版本标为 `cached` 并保留原始时间及发布链接。",
        "",
        "## " + SECTIONS[2],
        "",
        "- Tool protocols can lower integration cost; verify protocol support and permission scopes per release.",
        "- Agent runtimes compete on reliable execution and evaluation; popularity alone does not establish maturity.",
        f"- {snapshot['framework_matrix'][0]['risk']}",
        f"- {snapshot['framework_matrix'][2]['risk']}",
    ])
    if any(source["errors"] for source in snapshot["sources"].values()):
        errs = "; ".join(name + ": " + ", ".join(source["errors"]) for name, source in snapshot["sources"].items() if source["errors"])
        lines.append(f"- 数据降级 / Feed degradation: {errs}")

    lines.extend([
        "",
        "## " + SECTIONS[3],
        "",
        "1. 今天用 30 分钟对照两个现有框架，完成同一个代表性任务。",
        "2. 固定一个候选版本，记录成功率、执行成本及工具失败后的恢复行为。",
        "3. 优先复用一个经过验证的沙箱、技能或消息组件，并记录采用决定及上游链接。",
        "",
        "采集范围：GitHub 四个主题 `ai-agent`、`llm`、`multi-agent`、`systems-architecture` 各取星标排名前三，同时监控上述五个项目及其最新稳定发布；HN 只读取前十条热门故事并按 AI 与架构关键词筛选。GitHub 使用总星标排名，不能据此推断当日增长。",
        "",
        "运行方式：`python3 scripts/horizon_tech_scout.py --summary`。离线演示：`python3 scripts/horizon_tech_scout.py --mock --summary --verify`。验证现有文件：`python3 scripts/horizon_tech_scout.py --verify`，验证过程不修改文件。每天由已有的 cron 或任务调度器运行一次即可；脚本不会自行安装调度任务。",
        ""
    ])
    return "\n".join(lines)


def write_briefing(content, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mock", action="store_true", help="Generate deterministic offline synthetic records")
    parser.add_argument("--verify", action="store_true", help="Validate existing output; with --mock, generate and validate")
    parser.add_argument("--summary", action="store_true", help="Print a concise morning briefing")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output JSON and fallback cache path")
    parser.add_argument("--render-briefing", nargs="?", const="", default=None, metavar="PATH",
                        help="Render Markdown morning briefing; optional path defaults to data/tech_delta/reports/MORNING_TECH_BRIEFING_YYYY-MM-DD.md")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.verify and not args.mock and args.render_briefing is None:
            snapshot = read_snapshot(args.output)
            verify_snapshot(snapshot)
        else:
            snapshot = build_snapshot(mock=args.mock, cache=None if args.mock else load_cache(args.output))
            write_snapshot(snapshot, args.output)
        if args.summary:
            print(format_summary(snapshot), end="")
        if args.render_briefing is not None:
            if args.render_briefing == "":
                briefing_date = snapshot["generated_at"][:10]
                target_path = DEFAULT_REPORTS_DIR / f"MORNING_TECH_BRIEFING_{briefing_date}.md"
            else:
                target_path = Path(args.render_briefing)
            content = render_markdown_briefing(snapshot)
            write_briefing(content, target_path)
            print(f"Rendered morning briefing to {target_path}")
        elif not args.summary:
            print("Verified " + str(snapshot["record_count"]) + " records [" + snapshot["mode"] + "]")
        return 0
    except (OSError, ValueError, TypeError, OverflowError) as error:
        parser.exit(1, "horizon_tech_scout: " + clean_text(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
