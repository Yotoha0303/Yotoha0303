#!/usr/bin/env python3
"""Unit tests for serve_today_briefing.py."""

from __future__ import annotations

import http.client
import json
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer

import serve_today_briefing as server_mod


class TestTodayBriefingServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 18765
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), server_mod.TodayBriefingRequestHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_health_endpoint(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        conn.close()

    def test_today_json_endpoint(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/today")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json; charset=utf-8")
        data = json.loads(resp.read().decode("utf-8"))
        self.assertIn("records", data)
        self.assertIn("framework_matrix", data)
        conn.close()

    def test_briefing_markdown_endpoint(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/briefing")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/markdown; charset=utf-8")
        content = resp.read().decode("utf-8")
        self.assertIn("Frontier Tech Morning Briefing", content)
        conn.close()

    def test_static_index_html(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/index.html")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        content = resp.read().decode("utf-8")
        self.assertIn("PHALANX INTELLIGENCE", content)
        self.assertIn("Prior Art Adoption Matrix", content)
        conn.close()


if __name__ == "__main__":
    unittest.main()
