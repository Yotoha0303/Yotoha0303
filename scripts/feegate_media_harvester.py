#!/usr/bin/env python3
"""Collect media metrics, preserve unavailable counters, and verify an incremental ledger."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request

if __package__:
    from . import feegate_ops_pipeline as ops
else:
    import feegate_ops_pipeline as ops

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "data/products/media_metrics.json"
DEFAULT_SOURCES = ROOT / "data/media_sources.json"
PLATFORMS = ("youtube", "spotify", "bilibili", "netease")
MAX_BYTES = 2 * 1024 * 1024
MOCK_TIME = "2026-11-10 08:00"


class HarvestError(ValueError):
    """A payload, configuration, network response or hash failed validation."""


def require(condition, message):
    if not condition:
        raise HarvestError(message)


def platform_name(value):
    require(isinstance(value, str), "invalid_platform")
    name = value.strip().casefold()
    require(name in PLATFORMS, "unsupported_platform")
    return name


def counter(value):
    if value is None:
        return None
    require(type(value) in (int, float, str), "invalid_counter")
    text = str(value).strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmMbB万亿]?)", text)
    require(match is not None, "invalid_counter")
    try:
        amount = Decimal(match[1]) * {"": 1, "k": 1000, "m": 10**6, "b": 10**9,
                                    "万": 10**4, "亿": 10**8}[match[2].lower()]
        require(amount.is_finite() and amount == amount.to_integral_value()
                and 0 <= amount <= 2**63 - 1, "invalid_counter")
        return int(amount)
    except InvalidOperation as error:
        raise HarvestError("invalid_counter") from error


def fingerprint(row):
    return hashlib.sha256(ops.canonical({key: value for key, value in row.items()
                                         if key != "sha256_hash"})).hexdigest()


def normalize_record(raw, harvested_at):
    require(isinstance(raw, dict), "invalid_record")
    platform = platform_name(raw.get("platform"))
    media_id = raw.get("media_id")
    require(type(media_id) in (str, int) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(media_id))
            is not None, "invalid_media_id")
    title = raw.get("title")
    require(isinstance(title, str) and bool(ops.clean_string(title)), "invalid_title")
    interactions = raw.get("interactions", {})
    require(isinstance(interactions, dict), "invalid_interactions")
    timestamp = ops.normalize_time(harvested_at)
    result = {"platform": platform, "media_id": str(media_id), "title": ops.clean_string(title),
              "play_count": counter(raw.get("play_count")),
              "interactions": {field: counter(interactions.get(field)) for field in ("likes", "favorites", "comments")},
              "harvested_at": timestamp, "mode": raw.get("mode", "live")}
    require(result["mode"] in ("mock", "live"), "invalid_mode")
    result = ops.clean_value(result)
    require(result["media_id"] == str(media_id), "media_id_changed_by_pipeline_cleaning")
    result["sha256_hash"] = fingerprint(result)
    return result


def adapt_payload(platform, media_id, payload):
    """Map official/provider response fields; unavailable totals stay null."""
    require(isinstance(payload, dict), "invalid_provider_payload")
    platform = platform_name(platform)
    if platform == "youtube":
        items = payload.get("items")
        require(isinstance(items, list) and len(items) == 1, "youtube_item_missing")
        item = items[0]
        require(isinstance(item, dict) and str(item.get("id")) == str(media_id), "media_id_mismatch")
        require(isinstance(item.get("snippet"), dict) and isinstance(item.get("statistics", {}), dict), "invalid_youtube_metrics")
        title, metrics = item.get("snippet", {}).get("title"), item.get("statistics", {})
        plays, likes, favorites, comments = (metrics.get(key) for key in
                                            ("viewCount", "likeCount", "favoriteCount", "commentCount"))
    elif platform == "spotify":
        require(str(payload.get("id")) == str(media_id), "media_id_mismatch")
        title = payload.get("name")
        plays = likes = favorites = comments = None
    elif platform == "bilibili":
        require(payload.get("code") == 0 and isinstance(payload.get("data"), dict), "bilibili_error")
        item = payload["data"]
        require(str(item.get("bvid")) == str(media_id), "media_id_mismatch")
        require(isinstance(item.get("stat", {}), dict), "invalid_bilibili_metrics")
        title, metrics = item.get("title"), item.get("stat", {})
        plays, likes, favorites, comments = (metrics.get(key) for key in ("view", "like", "favorite", "reply"))
    else:
        require(payload.get("code") == 200, "netease_error")
        songs = payload.get("songs")
        require(isinstance(songs, list) and len(songs) == 1 and isinstance(songs[0], dict), "netease_song_missing")
        require(str(songs[0].get("id")) == str(media_id), "media_id_mismatch")
        title = songs[0].get("name")
        plays = likes = favorites = None
        comments = payload.get("comment_total")
    return {"platform": platform, "media_id": media_id, "title": title, "play_count": plays,
            "interactions": {"likes": likes, "favorites": favorites, "comments": comments}}


def request_json(url, *, timeout=5.0, headers=None, mock=False, mock_response=None):
    if mock:
        require(mock_response is not None, "missing_mock_response")
        return mock_response
    require(urllib.parse.urlsplit(url).scheme == "https", "https_required")
    require(math.isfinite(timeout) and timeout > 0, "invalid_timeout")
    request = urllib.request.Request(url, headers={"User-Agent": "MediaMetrics/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            require(response.status == 200, "upstream_http_error")
            body = response.read(MAX_BYTES + 1)
        require(len(body) <= MAX_BYTES, "response_too_large")
        payload = json.loads(body, object_pairs_hook=ops.unique_object, parse_constant=ops.reject_constant)
        require(isinstance(payload, dict), "invalid_provider_payload")
        return payload
    except (OSError, ValueError, HTTPException) as error:
        # URLs may contain API credentials; never interpolate upstream exceptions.
        raise HarvestError("upstream_request_failed") from error


def live_payload(source, timeout):
    require(isinstance(source, dict), "invalid_source")
    platform = platform_name(source.get("platform"))
    media_id = source.get("media_id")
    require(type(media_id) in (int, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(media_id)), "invalid_media_id")
    media_id = str(media_id)
    if platform == "youtube":
        key = os.environ.get("YOUTUBE_API_KEY")
        require(bool(key), "youtube_api_key_required")
        url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(
            {"part": "snippet,statistics", "id": media_id, "key": key})
        payload = request_json(url, timeout=timeout)
    elif platform == "spotify":
        token = os.environ.get("SPOTIFY_ACCESS_TOKEN")
        require(bool(token), "spotify_access_token_required")
        payload = request_json("https://api.spotify.com/v1/tracks/" + media_id,
                               timeout=timeout, headers={"Authorization": "Bearer " + token})
    elif platform == "bilibili":
        payload = request_json("https://api.bilibili.com/x/web-interface/view?bvid=" + media_id, timeout=timeout)
    else:
        require(media_id.isdigit(), "netease_numeric_id_required")
        payload = request_json("https://music.163.com/api/song/detail?" + urllib.parse.urlencode(
            {"id": media_id, "ids": "[" + media_id + "]"}), timeout=timeout)
        comments = request_json("https://music.163.com/api/v1/resource/comments/R_SO_4_" + media_id
                                + "?limit=1", timeout=timeout)
        require(comments.get("code") == 200, "netease_comments_error")
        payload = {**payload, "comment_total": comments.get("total")}
    return adapt_payload(platform, media_id, payload)


def mock_records():
    return [{"platform": platform, "media_id": "1001" if platform == "netease" else "demo_01",
             "title": "Synthetic " + platform + " sample", "play_count": None if platform in (
                 "spotify", "netease") else "1.2万", "interactions": {"comments": 12}}
            for platform in PLATFORMS]


def harvest(*, sources_path=DEFAULT_SOURCES, platform=None, mock=False, mock_payload=None, timeout=5.0):
    require(math.isfinite(timeout) and timeout > 0, "invalid_timeout")
    selected = platform_name(platform) if platform is not None else None
    offline = mock or mock_payload is not None
    raw = ops.read_json(Path(mock_payload)) if mock_payload is not None else (
        mock_records() if mock else ops.read_json(Path(sources_path)))
    require(isinstance(raw, list) and bool(raw), "input_must_be_nonempty_array")
    timestamp = MOCK_TIME if offline else datetime.now(ops.LOCAL_TZ).strftime(ops.TIME_FORMAT)
    records = []
    seen = set()
    for item in raw:
        require(isinstance(item, dict), "invalid_source")
        name = platform_name(item.get("platform"))
        if selected is not None and name != selected:
            continue
        value = item if offline else live_payload(item, timeout)
        if offline and "payload" in item:
            value = adapt_payload(name, item.get("media_id"), item["payload"])
        row = normalize_record({**value, "mode": "mock" if offline else "live"}, timestamp)
        identity = (row["platform"], row["media_id"], row["mode"])
        require(identity not in seen, "duplicate_media_id")
        seen.add(identity)
        records.append(row)
    require(bool(records), "no_matching_media")
    return records


def make_product(records, updated_at):
    product = {"product_id": "media-metrics", "title": "Media metrics ledger", "category": "media_metrics",
               "updated_at": updated_at, "item_count": len(records), "records": records,
               "x402_pricing": {"amount": 0, "currency": "USD"}}
    product = ops.normalize_product(product)
    product["sha256_hash"] = fingerprint(product)
    return product


def verify_catalog(product):
    require(isinstance(product, dict), "invalid_catalog")
    require(isinstance(product.get("sha256_hash"), str) and re.fullmatch(r"[0-9a-f]{64}", product["sha256_hash"])
        and hmac.compare_digest(
        product["sha256_hash"], fingerprint(product)), "catalog_hash_mismatch")
    normalized = ops.normalize_product(product)
    require(product["product_id"] == "media-metrics", "wrong_product_id")
    require({k: v for k, v in product.items() if k != "sha256_hash"} == normalized, "noncanonical_catalog")
    seen = set()
    for row in product["records"]:
        expected = normalize_record(row, row.get("harvested_at"))
        require(row == expected, "record_hash_or_schema_mismatch")
        key = (row["platform"], row["media_id"], row["mode"])
        require(key not in seen, "duplicate_media_id")
        seen.add(key)
    return True


@contextmanager
def catalog_lock(path):
    # Reuse the pipeline's publication lock and atomic writer for ledger transactions.
    with ops.publication_lock(Path(path).parent):
        yield


def export_catalog(records, path=DEFAULT_CATALOG):
    require(isinstance(records, list) and bool(records), "empty_records")
    identities = set()
    for row in records:
        require(row == normalize_record(row, row.get("harvested_at")), "invalid_record_hash")
        key = (row["platform"], row["media_id"], row["mode"])
        require(key not in identities, "duplicate_media_id")
        identities.add(key)
    path = Path(path)
    require(not path.is_symlink(), "symlink_catalog")
    with catalog_lock(path):
        previous = ops.read_json(path) if path.exists() else None
        if previous is not None:
            verify_catalog(previous)
        merged = {(row["platform"], row["media_id"], row["mode"]): row
                  for row in previous["records"]} if previous else {}
        for row in records:
            key = (row["platform"], row["media_id"], row["mode"])
            require(key not in merged or row["harvested_at"] >= merged[key]["harvested_at"], "stale_observation")
            merged[key] = row
        ordered = [merged[key] for key in sorted(merged)]
        product = make_product(ordered, max(row["harvested_at"] for row in ordered))
        verify_catalog(product)
        ops.atomic_write(path, ops.canonical(product) + b"\n")
    return product


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harvest", action="store_true")
    parser.add_argument("--platform", type=platform_name)
    parser.add_argument("--export-catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--verify-hashes", action="store_true")
    parser.add_argument("--mock-payload", type=Path)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    if not (args.harvest or args.verify_hashes):
        parser.error("--harvest or --verify-hashes is required")
    try:
        if args.harvest:
            records = harvest(sources_path=args.sources, platform=args.platform, mock=args.mock,
                              mock_payload=args.mock_payload, timeout=args.timeout)
            product = export_catalog(records, args.export_catalog)
            print(f"Updated: {product['updated_at']}  Records: {product['item_count']}")
        if args.verify_hashes:
            verify_catalog(ops.read_json(args.export_catalog))
            print("Hash verification passed")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"media harvest failed: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
