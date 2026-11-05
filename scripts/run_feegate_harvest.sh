#!/usr/bin/env bash
# ==============================================================================
# 🌐 FeeGate Real-World Commercial Data Harvest & Sync Runner
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== [$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting FeeGate Harvest Run ==="
cd "$REPO_DIR"

python3 "$SCRIPT_DIR/feegate_harvester.py"

if git diff --quiet data/; then
    echo "=== [✓] Data files are up to date. No new commit needed. ==="
else
    echo "=== [+] Detected new data changes. Staging and committing... ==="
    git add data/
    git commit -m "chore(data): manual feegate real-world datasets ingestion ($(date +%Y-%m-%d)) [skip ci]"
    git push origin main
    echo "=== [✓] Successfully pushed updated FeeGate datasets to GitHub! ==="
fi
