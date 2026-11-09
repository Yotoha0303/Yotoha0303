#!/usr/bin/env bash
# ==============================================================================
# Today's Frontier Tech Morning Briefing Launcher
# ==============================================================================
# Launches local zero-exposure HTTP server and opens browser for the Commander.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
SERVE_SCRIPT="$SCRIPT_DIR/serve_today_briefing.py"
PORT="${1:-8765}"
URL="http://127.0.0.1:$PORT/"

# Check if server is already listening
if ss -tln | grep -q ":$PORT "; then
    echo "[*] Server is already running on port $PORT."
else
    echo "[*] Starting local briefing UI server on port $PORT..."
    nohup "$PYTHON_BIN" "$SERVE_SCRIPT" --port "$PORT" > /tmp/serve_today_briefing.log 2>&1 &
    sleep 0.5
fi

echo "[*] Opening today's news UI in browser: $URL"
if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
else
    echo "[*] Direct browser access: $URL"
fi
