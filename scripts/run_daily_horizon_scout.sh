#!/usr/bin/env bash
# ==============================================================================
# Frontier Technology Scout Daily Automation Pipeline
# ==============================================================================
# Executed by local cron or systemd timer to collect delta observations,
# verify schema compliance, and render a high-density Markdown morning briefing.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
SCOUT_CLI="$SCRIPT_DIR/horizon_tech_scout.py"
OUTPUT_JSON="$PROJECT_ROOT/data/tech_delta/daily_frontier_delta.json"
REPORTS_DIR="$PROJECT_ROOT/data/tech_delta/reports"

mkdir -p "$REPORTS_DIR"

echo "=== [1/3] Running Frontier Tech Harvester & Briefing Generator ==="
if "$PYTHON_BIN" "$SCOUT_CLI" --output "$OUTPUT_JSON" --render-briefing; then
    echo "[PASS] Live harvesting and briefing generation completed successfully."
else
    echo "[WARN] Live harvesting encountered network or rate limits. Falling back to cache..."
    if [[ -f "$OUTPUT_JSON" ]]; then
        "$PYTHON_BIN" "$SCOUT_CLI" --output "$OUTPUT_JSON" --verify
        "$PYTHON_BIN" "$SCOUT_CLI" --output "$OUTPUT_JSON" --render-briefing
        echo "[PASS] Fallback to existing verified cache completed."
    else
        echo "[WARN] No cache found. Falling back to deterministic mock baseline..."
        "$PYTHON_BIN" "$SCOUT_CLI" --mock --output "$OUTPUT_JSON" --render-briefing --verify
        echo "[PASS] Fallback to deterministic mock completed."
    fi
fi

echo "=== [2/3] Verifying Cache Schema and Integrity ==="
"$PYTHON_BIN" "$SCOUT_CLI" --output "$OUTPUT_JSON" --verify

echo "=== [3/3] Daily Pipeline Execution Report & Obsidian Sync ==="
LATEST_BRIEFING="$(ls -t "$REPORTS_DIR"/MORNING_TECH_BRIEFING_*.md 2>/dev/null | head -n 1 || true)"
if [[ -n "$LATEST_BRIEFING" && -f "$LATEST_BRIEFING" ]]; then
    echo "Latest Morning Briefing: $LATEST_BRIEFING"
    echo "Lines: $(wc -l < "$LATEST_BRIEFING")"

    # Obsidian Dual-View Synchronization ("历史的新闻看 Obsidian")
    OBSIDIAN_VAULT_DIR="${OBSIDIAN_VAULT_DIR:-/home/yotoha/Documents/Obsidian/Obsidian main/AI KnowTrace/前沿技术情报/晨报归档}"
    if [[ -d "$OBSIDIAN_VAULT_DIR" ]]; then
        BRIEFING_FILENAME="$(basename "$LATEST_BRIEFING")"
        cp -f "$LATEST_BRIEFING" "$OBSIDIAN_VAULT_DIR/$BRIEFING_FILENAME"
        echo "[PASS] Synchronized latest briefing to Obsidian: $OBSIDIAN_VAULT_DIR/$BRIEFING_FILENAME"
    else
        echo "[INFO] Obsidian target directory not found; skipped sync ($OBSIDIAN_VAULT_DIR)."
    fi
else
    echo "[WARN] No briefing file located in $REPORTS_DIR"
fi

echo "[DONE] Daily horizon scout pipeline finished successfully."
