#!/usr/bin/env python3
"""Build a provenance-aware FeeGate music intelligence product with stdlib only.

--mock creates deterministic synthetic audience examples and cited reference
credits without network access. The default mode retrieves bounded official
reference pages; it does not invent private platform analytics. --verify alone
checks an existing file without rewriting it. See the campaign guide for scope.
"""

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data/products/otoha_market_intelligence.json"
SCHEMA_VERSION = "1.0.0"
ARTIST_ID = "music:otoha-jp"
DEVELOPER_ID = "https://github.com/Yotoha0303#identity"
MOCK_TIME = "2026-01-01T00:00:00Z"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
SOURCES = (
    ("black_butler_official", "https://www.kuroshitsuji.tv/music/",
     "Black Butler: Public School Arc official music credits",
     ("音羽", "狂信者のパレード", "青春コンプレックス", "大生解")),
    ("bocchi_official", "https://bocchi.rocks/tv/music/",
     "Bocchi the Rock! official music directory", ("青春コンプレックス",)),
    ("artist_official", "https://otohaofficial.com/profiles",
     "Official artist profile, anime credits and social account cross-links",
     ("音羽", "Dr.STONE", "no man's world", "青春コンプレックス")),
    ("artist_release_official", "https://otohaofficial.com/musics/19736",
     "Official no man's world digital release; 2025-07-11", ("no man's world",)),
)
OFFICIAL_ACCOUNTS = {
    "youtube": ("@otoha_official", "https://www.youtube.com/@otoha_official"),
    "x": ("_______0108", "https://twitter.com/_______0108"),
    "tiktok": ("@_.otoha._0108", "https://www.tiktok.com/@_.otoha._0108"),
}
PLATFORMS = {
    "youtube": ("YouTube", {"subscribers": "people", "mv_views": "views"},
                "Performance hook -> full music video -> repeat viewing",
                "Caption a short systems demo and link to a reproducible walkthrough.",
                "Public channel and MV counters; stable channel/video IDs required."),
    "spotify": ("Spotify", {"monthly_listeners": "people", "streams": "plays"},
                "Anime discovery -> artist catalog -> saves and repeat listening",
                "Route a tutorial to an evergreen documentation series.",
                "Monthly listeners and stream totals are not supplied by ordinary catalog APIs."),
    "apple_music": ("Apple Music", {"monthly_listeners": "people", "streams": "plays"},
                    "Editorial or search discovery -> catalog listening",
                    "Use a curated project index with clear next steps.",
                    "Listener and play analytics require an authorized artist export."),
    "x": ("X / Twitter", {"followers": "people", "impressions": "impressions",
                          "interactions": "interactions", "engagement_rate": "ratio"},
          "Release clip -> discussion -> destination link",
          "Publish a measured engineering result with a demo and tagged documentation link.",
          "Engagement uses interactions / impressions for the same post cohort and period."),
    "tiktok": ("TikTok", {"followers": "people", "views": "views",
                          "interactions": "interactions", "engagement_rate": "ratio"},
               "Immediate hook -> reusable short clip -> profile visit",
               "Show one visible before/after result with captions and a tutorial link.",
               "Engagement uses interactions / views; this denominator differs from X."),
    "bilibili": ("Bilibili", {"followers": "people", "views": "views"},
                 "Subtitled explanation -> community discussion -> collection",
                 "Localize technical subtitles, terminology and a minimal runnable example.",
                 "No official artist account has been established in this baseline."),
    "netease_music": ("NetEase Music", {"followers": "people", "streams": "plays"},
                      "Localized title search -> catalog discovery -> repeat listening",
                      "Localize documentation keywords and preserve canonical project links.",
                      "Catalog availability and an official artist ID require regional verification."),
}
FEATURE_TERMS = {
    "j_pop": ("j-pop", "jpop", "j pop"),
    "rock": ("rock", "ロック"),
    "anison": ("anison", "anime song", "アニソン"),
    "sony_music": ("sony music", "ソニーミュージック"),
    "go_backend": ("go backend", "golang", "goバックエンド"),
    "distributed_systems": ("distributed systems", "分散システム"),
    "multi_agent_frameworks": ("multi-agent", "multi agent", "マルチエージェント"),
    "ai_collaboration_framework": ("ai collaboration framework",),
}
FEATURE_NAMES = tuple(FEATURE_TERMS)
MUSIC_PROFILE = "J-Pop rock anison Sony Music"
TECH_PROFILE = "Go backend distributed systems multi-agent frameworks AI Collaboration Framework"


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(",", ":"))


def checksum(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def check_public_text(text):
    """Reject restricted markers without putting their plaintext in public code."""
    markers = (
        (121, 111, 116, 111, 112, 104, 97, 108, 97, 110, 120),
        (115, 116, 101, 119, 97, 114, 100),
        (112, 104, 97, 108, 97, 110, 120, 45),
        (115, 104, 101, 110, 122, 104, 101, 110),
        (20013, 22269, 28145, 22323),
    )
    decoded = text
    for _ in range(8):
        plain = urllib.parse.unquote(decoded)
        if plain == decoded:
            break
        decoded = plain
    decoded = decoded.casefold()
    if any("".join(map(chr, marker)) in decoded for marker in markers):
        raise ValueError("Public export contains a restricted marker")


def feature_vector(text):
    """Binary term evidence, with word boundaries for Latin terms; names add no evidence."""
    if not isinstance(text, str):
        raise ValueError("Feature input must be text")
    text = re.sub(r"\s+", " ", text.casefold())
    return [int(any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text)
                    if term.isascii() else term in text for term in terms))
            for terms in FEATURE_TERMS.values()]


def classify_vector(vector):
    if (not isinstance(vector, list) or len(vector) != len(FEATURE_NAMES)
            or any(type(value) is not int or value not in (0, 1) for value in vector)):
        raise ValueError("Feature vector must have eight binary integer dimensions")
    music, technology = sum(vector[:4]) / 4, sum(vector[4:]) / 4
    if music and technology:
        label = "ambiguous"
    elif music >= 0.5:
        label = "music_artist"
    elif technology >= 0.5:
        label = "software_developer"
    else:
        label = "insufficient_evidence"
    return {"label": label, "music_score": music, "technology_score": technology}


def safe_ratio(numerator, denominator):
    if (type(numerator) not in (int, float) or type(denominator) not in (int, float)
            or not 0 <= numerator <= denominator < float("inf") or denominator <= 0):
        raise ValueError("Ratio requires 0 <= numerator <= denominator and denominator > 0")
    return round(numerator / denominator, 6)


@dataclass(frozen=True)
class Metric:
    value: object
    unit: str
    status: str
    definition: str
    source_ids: list
    observed_at_utc: object = None


def metric(value, unit, definition, mock):
    return asdict(Metric(value if mock else None, unit,
                         "mock" if mock else "unavailable", definition, []))


def identity_features():
    artist, developer = feature_vector(MUSIC_PROFILE), feature_vector(TECH_PROFILE)
    return {
        "feature_names": list(FEATURE_NAMES),
        "artist": {"entity_id": ARTIST_ID, "profile": MUSIC_PROFILE,
                   "vector": artist, "classification": classify_vector(artist)},
        "developer": {"entity_id": DEVELOPER_ID, "profile": TECH_PROFILE,
                      "vector": developer, "classification": classify_vector(developer)},
        "l1_distance": sum(abs(a - b) for a, b in zip(artist, developer)),
        "decision_rule": "At least two features in one domain; any mixed-domain evidence abstains.",
        "unresolved_name_variants": ["をとは"],
        "name_policy": "A romanization or similar name alone never establishes identity or affiliation.",
    }


def fetch_source(url, timeout=10):
    """Retrieve only pinned public sources; cap time, bytes and redirect targets."""
    if url not in {source[1] for source in SOURCES}:
        raise ValueError("Source is outside the official source registry")

    class PinnedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, newurl):
            if newurl not in {source[1] for source in SOURCES}:
                raise ValueError("Source redirected outside the official source registry")
            return super().redirect_request(request, response, code, message, headers, newurl)

    request = urllib.request.Request(url, headers={
        "User-Agent": "FeeGate-Otoha-Intelligence/1.0", "Accept": "text/html",
    })
    with urllib.request.build_opener(PinnedRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("Source exceeds byte limit")
        return raw


def collect_sources(mock, fetched_at):
    sources = []
    for source_id, url, title, tokens in SOURCES:
        source = {"source_id": source_id, "url": url, "title": title,
                  "status": "reference", "retrieved_at_utc": None,
                  "sha256": None, "matched_tokens": [], "error_code": None}
        if not mock:
            try:
                raw = fetch_source(url)
                # Exclude code and markup before looking for literal credit markers.
                text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", raw.decode("utf-8"),
                              flags=re.S | re.I)
                text = re.sub(r"<[^>]+>", " ", text)
                entities = {"nbsp": " ", "amp": "&", "quot": '"', "apos": "'",
                            "rsquo": "'", "lsquo": "'", "lt": "<", "gt": ">"}
                text = re.sub(r"&#(x[0-9a-fA-F]+|[0-9]+);",
                              lambda match: chr(int(match[1][1:], 16) if match[1].startswith("x") else int(match[1])), text)
                text = re.sub(r"&([a-z]+);", lambda match: entities.get(match[1], match[0]), text)
                text = re.sub(r"\s+", " ", text.replace("’", "'"))
                source.update(status="fetched", retrieved_at_utc=fetched_at,
                              sha256=hashlib.sha256(raw).hexdigest(),
                              matched_tokens=[token for token in tokens if token in text])
            except (OSError, ValueError, urllib.error.URLError):
                # Do not export upstream errors: they may contain proxy paths or credentials.
                source.update(status="error", error_code="source_fetch_failed")
        sources.append(source)
    if not mock and not any(source["matched_tokens"] for source in sources):
        raise ValueError("No official credit markers retrieved; use --mock for offline fixtures")
    return sources


def build_records(product):
    """Normalized search documents; source-specific units remain in their native fields."""
    records = []
    for track in product["discography"]:
        records.append({"record_id": "track:" + track["track_id"], "record_type": "track",
                        "entity_id": ARTIST_ID, "title": track["title"],
                        "data_status": track["status"], "source_ids": track["source_ids"],
                        "search_terms": ["音羽-otoha-", track["title"], track["credit_role"]]})
    for platform in product["platform_reach"]:
        records.append({"record_id": "platform:" + platform["platform_id"],
                        "record_type": "platform", "entity_id": ARTIST_ID,
                        "title": platform["name"],
                        "data_status": "mock" if product["mode"] == "mock" else "unavailable",
                        "source_ids": platform["source_ids"], "search_terms": ["音羽-otoha-", platform["name"]]})
    return records


def seal_product(product):
    product["records"] = build_records(product)
    provenance = product["provenance"]
    provenance["record_count"] = len(product["records"])
    provenance["sha256_checksum"] = checksum(product["records"])
    # The content digest covers the envelope as well as the normalized records.
    provenance.pop("content_sha256", None)
    provenance["content_sha256"] = checksum(product)
    return product


def build_product(mock=False):
    generated = MOCK_TIME if mock else datetime.now(timezone.utc).isoformat(timespec="seconds")
    sources = collect_sources(mock, generated)
    credit_source = ["black_butler_official"]
    tracks = [
        {"track_id": "seishun-complex", "title": "青春コンプレックス",
         "credit_role": "composer", "performer": "Kessoku Band", "release_date": None,
         "status": "reference", "source_ids": credit_source},
        {"track_id": "parade-of-battlers", "title": "狂信者のパレード -The Parade of Battlers",
         "credit_role": "performer", "performer": "音羽-otoha-", "release_date": None,
         "status": "reference", "source_ids": credit_source},
        {"track_id": "daiseikai", "title": "大生解", "credit_role": "performer",
         "performer": "音羽-otoha-", "release_date": "2024-02-14",
         "status": "reference", "source_ids": credit_source},
        {"track_id": "happy-end-roll", "title": "ハッピー・エンド・ロール", "credit_role": "performer",
         "performer": "音羽-otoha-", "release_date": "2024-02-14",
         "status": "reference", "source_ids": credit_source},
        {"track_id": "no-mans-world", "title": "no man's world", "credit_role": "performer",
         "performer": "音羽-otoha-", "release_date": "2025-07-11", "status": "reference",
         "source_ids": ["artist_official", "artist_release_official"]},
    ]
    ties = [
        {"ip_title": "Bocchi the Rock!", "track_id": "seishun-complex", "role": "opening_composer",
         "status": "reference", "source_ids": credit_source,
         "note": "Composition credit; the recording is performed by Kessoku Band."},
        {"ip_title": "Black Butler", "track_id": "parade-of-battlers", "role": "opening_performer",
         "status": "reference", "source_ids": credit_source,
         "note": "Public School Arc opening theme; no streaming rank is asserted."},
        {"ip_title": "Dr. Stone", "track_id": "no-mans-world", "role": "ending_performer",
         "status": "reference", "source_ids": ["artist_official"],
         "note": "SCIENCE FUTURE, second cour ending theme; no streaming rank is asserted."},
    ]
    sample_values = {"subscribers": 100000, "mv_views": 1000000, "monthly_listeners": 50000,
                     "streams": 200000, "followers": 20000, "impressions": 100000,
                     "views": 100000, "interactions": 5000, "engagement_rate": 0.05}
    platforms = []
    for platform_id, (name, fields, mechanism, application, limitation) in PLATFORMS.items():
        metrics = {}
        for field, unit in fields.items():
            definition = ("interactions / " + ("impressions" if platform_id == "x" else "views")
                          if field == "engagement_rate" else field.replace("_", " "))
            metrics[field] = metric(sample_values[field], unit, definition, mock)
        account_id, account_url = OFFICIAL_ACCOUNTS.get(platform_id, (None, None))
        platforms.append({"platform_id": platform_id, "name": name, "entity_id": ARTIST_ID,
                          "account_id": account_id, "account_url": account_url,
                          "account_status": "reference" if account_id else "unverified",
                          "source_ids": ["artist_official"] if account_id else [],
                          "period_start": "2025-12-01" if mock else None,
                          "period_end": "2025-12-31" if mock else None,
                          "metrics": metrics, "viral_mechanism": mechanism,
                          "developer_application": application, "limitation": limitation})
    trajectory = []
    for platform_id in ("spotify", "apple_music", "netease_music"):
        for period, streams in (("2025-11", 100000), ("2025-12", 120000)):
            trajectory.append({"platform_id": platform_id, "period": period,
                               "streams": metric(streams, "plays", "Monthly plays; synthetic cohort", mock),
                               "growth_rate": metric(None if period.endswith("11") else 0.2, "ratio",
                                                     "(current - previous) / previous; same platform", mock)})
    counts = {"impressions": 100000, "engaged_users": 10000, "destination_visits": 2000,
              "activated_users": 500, "retained_users": 100}
    count_metrics = {name: metric(value, "people" if name != "impressions" else "impressions",
                                 "Synthetic single attributable cohort: " + name, mock)
                     for name, value in counts.items()}
    pairs = {"engagement_rate": ("engaged_users", "impressions"),
             "click_through_rate": ("destination_visits", "impressions"),
             "activation_rate": ("activated_users", "destination_visits"),
             "retention_rate": ("retained_users", "activated_users")}
    rates = {name: metric(safe_ratio(counts[num], counts[den]), "ratio", num + " / " + den, mock)
             for name, (num, den) in pairs.items()}
    product = {
        "catalog_id": "otoha-market-intelligence", "schema_version": SCHEMA_VERSION,
        "product_name": "Otoha Multi-Platform Market Intelligence",
        "category": "Cultural and entertainment intelligence",
        "target_audience": "Music analysts, developer advocates and dataset engineers",
        "commercial_value_proposition": "Cited music credits, normalized channel coverage and entity-safe search documents",
        "mode": "mock" if mock else "live_reference",
        "provenance": {"schema_version": SCHEMA_VERSION, "ingested_at_utc": generated,
                       "source_origin": ("Official reference pages and explicitly synthetic offline examples"
                                         if mock else "Official reference pages; platform analytics unavailable"),
                       "coverage": "partial", "record_count": 0, "sha256_checksum": ""},
        "commercial": {"provider": "FeeGate", "distribution": "HTTP REST / x402",
                       "publication_status": "offline_fixture" if mock else "research_preview",
                       "production_ready": False, "proposed_price_usd_per_query": 0.05,
                       "pricing_status": "hypothesis",
                       "rights_status": "Source and redistribution rights review required before sale"},
        "artist": {"entity_id": ARTIST_ID, "canonical_name": "音羽-otoha-",
                   "aliases": ["音羽", "Otoha"], "domain": "music",
                   "genres": ["J-Pop", "rock", "anison"],
                   "label_context": "Sony Music is a feature-space term; label affiliation is not independently verified here.",
                   "scope": "Selected cited works and research coverage, not a complete discography."},
        "sources": sources, "discography": tracks, "anime_ip_tie_ins": ties,
        "platform_reach": platforms, "streaming_trajectory": trajectory,
        "audience_funnel": {"status": "mock" if mock else "unavailable",
                            "period_start": "2025-12-01" if mock else None,
                            "period_end": "2025-12-31" if mock else None,
                            "attribution": "Synthetic single cohort; cross-platform audiences are not deduplicated or summed.",
                            "counts": count_metrics, "rates": rates},
        "entity_disambiguation": identity_features(),
        "limitations": ["All numeric reach examples in mock mode are synthetic, not artist measurements.",
                        "Reference credits carry citations, not independent live verification of every claim.",
                        "No top-track ranking, deduplicated audience, causal uplift or artist affiliation is claimed.",
                        "Streaming trajectories and funnels need authorized analytics before commercial use."],
        "records": [],
    }
    return seal_product(product)


# A small, explicit JSON Schema subset is enough to describe this closed product.
def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def array_schema(items, minimum=1):
    return {"type": "array", "items": items, "minItems": minimum}


TEXT = {"type": "string", "minLength": 1}
NULL_TEXT = {"type": ["string", "null"]}
STRINGS = array_schema(TEXT, 0)
DATE = {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"}
UTC = {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)$"}
HASH = {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}
METRIC_SCHEMA = object_schema({
    "value": {"type": ["number", "null"], "minimum": 0}, "unit": TEXT,
    "status": {"enum": ["mock", "unavailable"]}, "definition": TEXT,
    "source_ids": STRINGS, "observed_at_utc": UTC,
})
GROWTH_SCHEMA = object_schema({**METRIC_SCHEMA["properties"],
                               "value": {"type": ["number", "null"], "minimum": -1}})
CLASSIFICATION_SCHEMA = object_schema({
    "label": {"enum": ["music_artist", "software_developer", "ambiguous", "insufficient_evidence"]},
    "music_score": {"type": "number", "minimum": 0, "maximum": 1},
    "technology_score": {"type": "number", "minimum": 0, "maximum": 1},
})
PROFILE_SCHEMA = object_schema({"entity_id": TEXT, "profile": TEXT,
                                "vector": {"type": "array", "minItems": 8, "maxItems": 8,
                                           "items": {"type": "integer", "enum": [0, 1]}},
                                "classification": CLASSIFICATION_SCHEMA})
PRODUCT_SCHEMA = object_schema({
    "catalog_id": {"const": "otoha-market-intelligence"},
    "schema_version": {"const": SCHEMA_VERSION}, "product_name": TEXT, "category": TEXT,
    "target_audience": TEXT, "commercial_value_proposition": TEXT,
    "mode": {"enum": ["mock", "live_reference"]},
    "provenance": object_schema({"schema_version": {"const": SCHEMA_VERSION},
                                  "ingested_at_utc": {**UTC, "type": "string"},
                                  "source_origin": TEXT, "coverage": {"const": "partial"},
                                  "record_count": {"type": "integer", "minimum": 1},
                                  "sha256_checksum": {**HASH, "type": "string"},
                                  "content_sha256": {**HASH, "type": "string"}}),
    "commercial": object_schema({"provider": {"const": "FeeGate"}, "distribution": TEXT,
                                  "publication_status": {"enum": ["offline_fixture", "research_preview"]},
                                  "production_ready": {"const": False},
                                  "proposed_price_usd_per_query": {"type": "number", "minimum": 0},
                                  "pricing_status": {"const": "hypothesis"}, "rights_status": TEXT}),
    "artist": object_schema({"entity_id": {"const": ARTIST_ID}, "canonical_name": {"const": "音羽-otoha-"},
                             "aliases": {"const": ["音羽", "Otoha"]}, "domain": {"const": "music"}, "genres": STRINGS,
                             "label_context": TEXT, "scope": TEXT}),
    "sources": array_schema(object_schema({"source_id": TEXT, "url": TEXT, "title": TEXT,
                                            "status": {"enum": ["reference", "fetched", "error"]},
                                            "retrieved_at_utc": UTC, "sha256": HASH,
                                            "matched_tokens": STRINGS, "error_code": NULL_TEXT})),
    "discography": array_schema(object_schema({"track_id": TEXT, "title": TEXT,
                                                "credit_role": {"enum": ["composer", "performer"]},
                                                "performer": TEXT, "release_date": DATE,
                                                "status": {"const": "reference"}, "source_ids": STRINGS})),
    "anime_ip_tie_ins": array_schema(object_schema({"ip_title": TEXT, "track_id": NULL_TEXT,
                                                     "role": TEXT, "status": {"enum": ["reference", "unconfirmed"]},
                                                     "source_ids": STRINGS, "note": TEXT})),
    "platform_reach": array_schema(object_schema({"platform_id": {"enum": list(PLATFORMS)},
                                                   "name": TEXT, "entity_id": {"const": ARTIST_ID},
                                                   "account_id": NULL_TEXT, "account_url": NULL_TEXT,
                                                   "account_status": {"enum": ["reference", "unverified"]},
                                                   "source_ids": STRINGS,
                                                   "period_start": DATE, "period_end": DATE,
                                                   "metrics": {"type": "object", "additionalProperties": METRIC_SCHEMA},
                                                   "viral_mechanism": TEXT, "developer_application": TEXT,
                                                   "limitation": TEXT})),
    "streaming_trajectory": array_schema(object_schema({"platform_id": TEXT,
                                                         "period": {"type": "string", "pattern": r"^\d{4}-\d{2}$"},
                                                         "streams": METRIC_SCHEMA, "growth_rate": GROWTH_SCHEMA})),
    "audience_funnel": object_schema({"status": {"enum": ["mock", "unavailable"]},
                                       "period_start": DATE, "period_end": DATE, "attribution": TEXT,
                                       "counts": object_schema({key: METRIC_SCHEMA for key in (
                                           "impressions", "engaged_users", "destination_visits",
                                           "activated_users", "retained_users")}),
                                       "rates": object_schema({key: METRIC_SCHEMA for key in (
                                           "engagement_rate", "click_through_rate", "activation_rate", "retention_rate")})}),
    "entity_disambiguation": object_schema({"feature_names": {"const": list(FEATURE_NAMES)},
                                            "artist": PROFILE_SCHEMA, "developer": PROFILE_SCHEMA,
                                            "l1_distance": {"const": 8}, "decision_rule": TEXT,
                                            "unresolved_name_variants": {"const": ["をとは"]}, "name_policy": TEXT}),
    "limitations": array_schema(TEXT),
    "records": array_schema(object_schema({"record_id": TEXT, "record_type": {"enum": ["track", "platform"]},
                                            "entity_id": {"const": ARTIST_ID}, "title": TEXT,
                                            "data_status": {"enum": ["reference", "mock", "unavailable"]},
                                            "source_ids": STRINGS, "search_terms": STRINGS})),
})
PRODUCT_SCHEMA.update({"$schema": "https://json-schema.org/draft/2020-12/schema",
                       "title": "FeeGate Otoha Market Intelligence 1.0.0"})


def validate_schema(value, schema=PRODUCT_SCHEMA, path="$"):
    """Validate the documented schema subset without third-party dependencies."""
    types = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
             "string": lambda v: isinstance(v, str), "integer": lambda v: type(v) is int,
             "number": lambda v: type(v) in (int, float), "null": lambda v: v is None}
    expected = schema.get("type")
    if expected and not any(types[kind](value) for kind in (expected if isinstance(expected, list) else [expected])):
        raise ValueError(f"{path}: invalid type")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise ValueError(f"{path}: unexpected constant")
    if "enum" in schema and not any(value == item and type(value) is type(item) for item in schema["enum"]):
        raise ValueError(f"{path}: value outside allowed enumeration")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key}: required field missing")
        for key, item in value.items():
            extra = schema.get("additionalProperties", True)
            if key not in properties and extra is False:
                raise ValueError(f"{path}: unexpected field")
            child = properties.get(key, extra if isinstance(extra, dict) else {})
            validate_schema(item, child, path + "." + key)
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            raise ValueError(f"{path}: invalid array length")
        for index, item in enumerate(value):
            validate_schema(item, schema.get("items", {}), f"{path}[{index}]")
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or ("pattern" in schema and not re.fullmatch(schema["pattern"], value)):
            raise ValueError(f"{path}: invalid string")
    elif type(value) in (int, float):
        if not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
            raise ValueError(f"{path}: numeric value out of bounds")


def verify_product(product):
    """Raise ValueError on incomplete, inconsistent, private or corrupted products."""
    serialized = canonical_json(product)  # Reject NaN/Infinity anywhere, including extras.
    check_public_text(serialized)
    validate_schema(product)
    provenance = product["provenance"]
    if provenance["record_count"] != len(product["records"]) or product["records"] != build_records(product):
        raise ValueError("Normalized records or record count do not match source sections")
    if provenance["sha256_checksum"] != checksum(product["records"]):
        raise ValueError("Records checksum mismatch")
    unsigned = json.loads(serialized)
    content_digest = unsigned["provenance"].pop("content_sha256")
    if content_digest != checksum(unsigned):
        raise ValueError("Content checksum mismatch")
    ingested = datetime.fromisoformat(provenance["ingested_at_utc"].replace("Z", "+00:00"))
    source_ids = [item["source_id"] for item in product["sources"]]
    if len(source_ids) != len(set(source_ids)) or set(source_ids) != {item[0] for item in SOURCES}:
        raise ValueError("Official source registry incomplete or duplicated")
    for source in product["sources"]:
        registered = next(item for item in SOURCES if item[0] == source["source_id"])
        if source["url"] != registered[1] or not set(source["matched_tokens"]) <= set(registered[3]):
            raise ValueError("Unrecognized official source or evidence token")
        if source["status"] == "fetched":
            if not source["sha256"] or not source["retrieved_at_utc"] or source["error_code"] is not None:
                raise ValueError("Fetched source is missing provenance")
            retrieved = datetime.fromisoformat(source["retrieved_at_utc"].replace("Z", "+00:00"))
            if retrieved > ingested:
                raise ValueError("Source retrieved after product ingestion")
        elif source["sha256"] is not None or source["retrieved_at_utc"] is not None or source["matched_tokens"]:
            raise ValueError("Unfetched source contains retrieval evidence")
        if (source["status"] == "error") != (source["error_code"] == "source_fetch_failed"):
            raise ValueError("Source error status mismatch")
    mock = product["mode"] == "mock"
    if mock and any(source["status"] != "reference" for source in product["sources"]):
        raise ValueError("Offline fixture contains network evidence")
    if not mock and not any(source["matched_tokens"] for source in product["sources"]):
        raise ValueError("Live reference product has no credit evidence")
    if product["commercial"]["publication_status"] != ("offline_fixture" if mock else "research_preview"):
        raise ValueError("Commercial publication status contradicts data mode")

    def walk(value, field=None):
        if isinstance(value, dict):
            if set(value) == set(METRIC_SCHEMA["properties"]):
                expected = "mock" if mock else "unavailable"
                if value["status"] != expected or value["observed_at_utc"] is not None or value["source_ids"]:
                    raise ValueError("Metric status or provenance contradicts data mode")
                if not mock and value["value"] is not None:
                    raise ValueError("Unavailable metric must be null")
                if value["unit"] == "ratio" and field != "growth_rate" and value["value"] is not None and value["value"] > 1:
                    raise ValueError("Ratio outside [0, 1]")
                if value["unit"] != "ratio" and value["value"] is not None and type(value["value"]) is not int:
                    raise ValueError("Count metric must be an integer")
            if "source_ids" in value and not set(value["source_ids"]) <= set(source_ids):
                raise ValueError("Dangling source reference")
            for key, item in value.items():
                if key in ("period_start", "period_end", "release_date") and item is not None:
                    datetime.strptime(item, "%Y-%m-%d")
                walk(item, key)
            if value.get("period_start") and value.get("period_end") and value["period_start"] > value["period_end"]:
                raise ValueError("Period start is after period end")
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(product)
    tracks = {track["track_id"]: track for track in product["discography"]}
    if len(tracks) != len(product["discography"]) or any(not track["source_ids"] for track in tracks.values()):
        raise ValueError("Track IDs must be unique and credits must have citations")
    ties = product["anime_ip_tie_ins"]
    if {tie["ip_title"] for tie in ties} != {"Bocchi the Rock!", "Black Butler", "Dr. Stone"} or len(ties) != 3:
        raise ValueError("Anime research coverage incomplete or duplicated")
    for tie in ties:
        if not tie["source_ids"]:
            raise ValueError("Anime credit requires a source reference")
        if tie["status"] == "unconfirmed":
            if tie["track_id"] is not None or tie["role"] != "unconfirmed":
                raise ValueError("Unconfirmed tie-in must not assert a track or role")
        elif (tie["track_id"] not in tracks or tie["role"] not in (
                "opening_" + tracks[tie["track_id"]]["credit_role"],
                "ending_" + tracks[tie["track_id"]]["credit_role"])):
            raise ValueError("Anime tie-in has a dangling track or inconsistent credit")
    platforms = product["platform_reach"]
    if {p["platform_id"] for p in platforms} != set(PLATFORMS) or len(platforms) != len(PLATFORMS):
        raise ValueError("Platform coverage incomplete or duplicated")
    for platform in platforms:
        account_id, account_url = OFFICIAL_ACCOUNTS.get(platform["platform_id"], (None, None))
        if (platform["account_id"] != account_id or platform["account_url"] != account_url
                or platform["account_status"] != ("reference" if account_id else "unverified")
                or platform["source_ids"] != (["artist_official"] if account_id else [])):
            raise ValueError("Platform account attribution does not match official cross-links")
        fields = PLATFORMS[platform["platform_id"]][1]
        if set(platform["metrics"]) != set(fields):
            raise ValueError("Platform metric coverage incomplete")
        for name, unit in fields.items():
            if platform["metrics"][name]["unit"] != unit:
                raise ValueError("Platform metric has incorrect units")
        if mock != bool(platform["period_start"] and platform["period_end"]):
            raise ValueError("Platform measurement interval contradicts data mode")
        if not mock and (platform["period_start"] or platform["period_end"]):
            raise ValueError("Unavailable platform must not claim a measurement interval")
        if mock and any(value["value"] is None for value in platform["metrics"].values()):
            raise ValueError("Synthetic platform metric missing")
        if mock and platform["platform_id"] in ("x", "tiktok"):
            metrics = platform["metrics"]
            denominator = "impressions" if platform["platform_id"] == "x" else "views"
            if metrics["engagement_rate"]["value"] != safe_ratio(metrics["interactions"]["value"], metrics[denominator]["value"]):
                raise ValueError("Platform engagement ratio mismatch")
    trajectory = product["streaming_trajectory"]
    keys = [(point["platform_id"], point["period"]) for point in trajectory]
    if len(keys) != len(set(keys)) or {key[0] for key in keys} != {"spotify", "apple_music", "netease_music"}:
        raise ValueError("Streaming trajectory coverage invalid")
    for platform_id in {key[0] for key in keys}:
        points = sorted((point for point in trajectory if point["platform_id"] == platform_id), key=lambda point: point["period"])
        if len(points) < 2 or points[0]["growth_rate"]["value"] is not None:
            raise ValueError("Trajectory needs two periods and a null initial growth rate")
        for point in points:
            datetime.strptime(point["period"], "%Y-%m")
            if point["streams"]["unit"] != "plays" or point["growth_rate"]["unit"] != "ratio":
                raise ValueError("Trajectory metric units invalid")
            if mock and point["streams"]["value"] is None:
                raise ValueError("Synthetic trajectory lacks stream counts")
        for previous, current in zip(points, points[1:]):
            if mock:
                before, after = previous["streams"]["value"], current["streams"]["value"]
                if before is None or after is None or before <= 0:
                    raise ValueError("Synthetic trajectory lacks stream counts")
                if current["growth_rate"]["value"] != round((after - before) / before, 6):
                    raise ValueError("Streaming growth mismatch")
    funnel = product["audience_funnel"]
    if funnel["status"] != ("mock" if mock else "unavailable"):
        raise ValueError("Funnel status mismatch")
    if mock != bool(funnel["period_start"] and funnel["period_end"]):
        raise ValueError("Funnel measurement interval contradicts data mode")
    if not mock and (funnel["period_start"] or funnel["period_end"]):
        raise ValueError("Unavailable funnel must not claim a measurement interval")
    for name, count in funnel["counts"].items():
        if count["unit"] != ("impressions" if name == "impressions" else "people"):
            raise ValueError("Funnel count has incorrect units")
    if any(rate["unit"] != "ratio" for rate in funnel["rates"].values()):
        raise ValueError("Funnel conversion has incorrect units")
    if mock:
        counts = funnel["counts"]
        pairs = {"engagement_rate": ("engaged_users", "impressions"),
                 "click_through_rate": ("destination_visits", "impressions"),
                 "activation_rate": ("activated_users", "destination_visits"),
                 "retention_rate": ("retained_users", "activated_users")}
        values = [counts[name]["value"] for name in (
            "impressions", "engaged_users", "destination_visits", "activated_users", "retained_users")]
        if any(value is None for value in values) or values != sorted(values, reverse=True):
            raise ValueError("Funnel must use a monotonically narrowing synthetic cohort")
        for name, (num, den) in pairs.items():
            if funnel["rates"][name]["value"] != safe_ratio(counts[num]["value"], counts[den]["value"]):
                raise ValueError("Funnel conversion ratio mismatch")
    if product["entity_disambiguation"] != identity_features():
        raise ValueError("Entity feature vectors or separation boundary changed")
    if "をとは" in product["artist"]["aliases"]:
        raise ValueError("Unresolved name variant must not be merged into artist aliases")
    return True


def read_product(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=unique_pairs, parse_constant=reject_constant)


def write_product(product, path=DEFAULT_OUTPUT):
    verify_product(product)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive temporary creation avoids clobbering an unrelated temporary file.
    temporary = path.with_name(path.name + ".tmp")
    created = False
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            created = True
            handle.write(json.dumps(product, ensure_ascii=False, sort_keys=True,
                                    indent=2, allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        if created and temporary.exists():
            temporary.unlink()
    return path


def format_summary(product):
    funnel = product["audience_funnel"]
    rates = funnel["rates"]
    def percent(name):
        value = rates[name]["value"]
        return "unavailable" if value is None else f"{value:.1%}"
    return "\n".join([
        f"{product['product_name']} | mode={product['mode']} | coverage=partial",
        f"{len(product['discography'])} selected works; {len(product['platform_reach'])} platform models; no measured streaming ranking.",
        "Cited roles: Bocchi the Rock! composer; Black Butler opening performer; Dr. Stone ending performer.",
        "Reach and funnel figures are synthetic in mock mode; live analytics remain unavailable.",
        f"Funnel ({funnel['status']}): CTR {percent('click_through_rate')}; activation {percent('activation_rate')}; retention {percent('retention_rate')}.",
        "Marketing hypothesis: short demo -> tagged documentation visit -> runnable example -> retained contributor.",
        "Measure each transition within one attributable cohort; never sum platform audiences.",
        "Identity: music and software profiles have L1 distance 8; mixed evidence abstains.",
    ])


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="Generate deterministic offline synthetic examples")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Dataset path")
    parser.add_argument("--summary", action="store_true", help="Print intelligence and funnel summary")
    parser.add_argument("--verify", action="store_true", help="Verify an existing file, or generated mock output")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.verify and not args.mock:
            product = read_product(args.output)
            verify_product(product)
        else:
            product = build_product(mock=args.mock)
            write_product(product, args.output)
            if args.verify:
                verify_product(read_product(args.output))
        if args.summary:
            print(format_summary(product))
        print("Verification passed." if args.verify else "Dataset generated.")
        return 0
    except (OSError, ValueError, TypeError, RecursionError):
        # Keep filenames and upstream exception text out of public terminal output.
        parser.exit(1, "Error: dataset generation or verification failed; check input integrity, output access and source availability.\n")


if __name__ == "__main__":
    raise SystemExit(main())
