#!/usr/bin/env python3
"""Lightweight local web server for Today's Frontier Tech Briefing.

Listens strictly on loopback (127.0.0.1) to ensure zero network exposure.
Serves static UI files and local REST APIs for live delta and markdown.

Usage:
    python3 scripts/serve_today_briefing.py --port 8765
    python3 scripts/serve_today_briefing.py --open
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web" / "today-briefing"
DATA_DIR = ROOT / "data" / "tech_delta"
REPORTS_DIR = DATA_DIR / "reports"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class TodayBriefingRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self) -> None:
        if self.path == "/api/today":
            self.serve_today_json()
        elif self.path == "/api/briefing":
            self.serve_latest_markdown()
        elif self.path == "/api/health":
            self.send_json_response({"status": "ok", "host": DEFAULT_HOST})
        else:
            super().do_GET()

    def serve_today_json(self) -> None:
        json_file = DATA_DIR / "daily_frontier_delta.json"
        if not json_file.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "daily_frontier_delta.json not found")
            return
        try:
            with open(json_file, "r", encoding="utf-8") as file_handle:
                payload = json.load(file_handle)
            self.send_json_response(payload)
        except Exception as error:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def serve_latest_markdown(self) -> None:
        briefings = sorted(REPORTS_DIR.glob("MORNING_TECH_BRIEFING_*.md"), reverse=True)
        if not briefings:
            self.send_error(HTTPStatus.NOT_FOUND, "No morning briefing files found")
            return
        target = briefings[0]
        try:
            with open(target, "r", encoding="utf-8") as file_handle:
                content = file_handle.read()
            encoded = content.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except Exception as error:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def send_json_response(self, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # Standard silent logging to stdout
        sys.stdout.write(f"[{self.log_date_time_string()}] {format % args}\n")
        sys.stdout.flush()


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, auto_open: bool = False) -> None:
    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, TodayBriefingRequestHandler)
    url = f"http://{host}:{port}/"
    print(f"[*] Today's News Briefing UI running at: {url}")
    print(f"[*] Serving files from: {WEB_DIR}")
    print("[*] Press Ctrl+C to terminate.")

    if auto_open:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down server gracefully...")
        httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description="Today's News Briefing UI Server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen (default 8765)")
    parser.add_argument("--open", action="store_true", help="Automatically open browser")
    args = parser.parse_args()

    run_server(host=args.host, port=args.port, auto_open=args.open)


if __name__ == "__main__":
    main()
