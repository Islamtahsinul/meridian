"""Core fixes from the Quality division review: redirects never carry credentials to another host or
down to plain http, failed datasets back off, and the app version matches the release.

Run: python -m unittest tests.test_core_quality -v   (no network: loopback servers only)
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import meridian
from meridian import datasets as D
from meridian import net as N
from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.net import Net, NetError
from meridian.registry import App
from meridian.scheduler import Scheduler
from meridian.store import Store

SECRETS = {"Authorization": "Bearer DUMMY-TOKEN", "X-Api-Key": "DUMMYKEY", "Cookie": "s=1",
           "X-Auth-Token": "tok", "apikey": "k"}


class RedirectHandlerTests(unittest.TestCase):
    def redirect(self, old, new, headers=None):
        req = Request(old, headers={**SECRETS, "Accept": "*/*", **(headers or {})})
        return N._NoRedirectToFile().redirect_request(req, BytesIO(b""), 302, "Found", Message(), new)

    def test_other_host_loses_every_credential_header(self):
        new = self.redirect("https://api.example.org/v1/x", "https://cdn.example.net/y")
        sent = {k.lower() for k, _v in new.header_items()}
        for name in SECRETS:
            self.assertNotIn(name.lower(), sent)
        self.assertIn("accept", sent)                       # ordinary headers still go

    def test_same_host_keeps_credentials(self):
        new = self.redirect("https://api.example.org/v1/x", "https://API.example.org/v2/x")
        self.assertEqual(dict(new.header_items()).get("Authorization"), "Bearer DUMMY-TOKEN")

    def test_https_to_http_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.redirect("https://api.example.org/x", "http://api.example.org/x")
        with self.assertRaises(urllib.error.HTTPError):
            self.redirect("https://api.example.org/x", "http://other.example/collect")
        with self.assertRaises(urllib.error.HTTPError):
            self.redirect("https://api.example.org/x", "file:///etc/passwd")

    def test_http_to_https_still_follows(self):
        new = self.redirect("http://api.example.org/x", "https://api.example.org/x")
        self.assertEqual(new.full_url, "https://api.example.org/x")


class _Collector(BaseHTTPRequestHandler):
    seen: list = []
    target = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/start"):
            self.send_response(302)
            self.send_header("Location", _Collector.target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        _Collector.seen.append({k.lower(): v for k, v in self.headers.items()})
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RedirectEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Collector.seen = []

    def test_request_and_download_drop_secrets_on_another_host(self):
        _Collector.target = f"http://localhost:{self.port}/collect"     # same server, another host name
        net = Net(Config(None))
        resp = net.request(f"http://127.0.0.1:{self.port}/start", headers=dict(SECRETS))
        self.assertEqual(resp.status, 200)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            net.download(f"http://127.0.0.1:{self.port}/start", Path(d) / "a.json", headers=dict(SECRETS))
        self.assertEqual(len(_Collector.seen), 2)
        for got in _Collector.seen:
            for name in SECRETS:
                self.assertNotIn(name.lower(), got)

    def test_same_host_redirect_keeps_the_key(self):
        _Collector.target = f"http://127.0.0.1:{self.port}/collect"
        Net(Config(None)).request(f"http://127.0.0.1:{self.port}/start", headers={"X-Api-Key": "DUMMYKEY"})
        self.assertEqual(_Collector.seen[0].get("x-api-key"), "DUMMYKEY")


def _app(tmp: Path) -> App:
    os.environ["MERIDIAN_DATA_DIR"] = str(tmp)
    cfg = Config(None)
    log = logging.getLogger("meridian.test.quality")
    return App(cfg, Store(tmp / "t.db"), Net(cfg), log, Scheduler(log), DatasetManager(log))


class DatasetBackoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.app = _app(Path(self.tmp.name))
        self.m = self.app.module("demo")
        self.calls = {"bad": 0, "deferred": 0}

        def bad(ds):
            self.calls["bad"] += 1
            raise NetError("example.org: HTTP 500")

        def deferred(ds):
            self.calls["deferred"] += 1
            raise D.DatasetDeferred("next try within 6 hours")

        self.m.dataset("bad", bad, max_age_hours=1)
        self.m.dataset("deferred", deferred, max_age_hours=1)

    def tearDown(self):
        self.app.store.close()
        os.environ.pop("MERIDIAN_DATA_DIR", None)
        self.tmp.cleanup()

    def test_backoff_schedule(self):
        self.assertEqual([D.backoff_seconds(n) for n in range(1, 8)],
                         [1800, 3600, 7200, 14400, 28800, 43200, 43200])
        self.assertEqual(D.backoff_seconds(0), 0)

    def test_failed_dataset_waits_before_the_next_round_and_resets_on_success(self):
        mgr = self.app.datasets
        stop = threading.Event()
        mgr._build_round(stop)
        self.assertEqual(self.calls["bad"], 1)
        ds = mgr.items["demo.bad"]
        self.assertEqual(ds.failures, 1)
        self.assertAlmostEqual(ds.next_attempt - time.time(), 1800, delta=5)
        mgr._build_round(stop)                       # the next round, still within the wait: no request
        self.assertEqual(self.calls["bad"], 1)
        ds.next_attempt = time.time() - 1            # the wait is over
        mgr._build_round(stop)
        self.assertEqual(self.calls["bad"], 2)
        self.assertEqual(ds.failures, 2)
        self.assertAlmostEqual(ds.next_attempt - time.time(), 3600, delta=5)
        info = {i["key"]: i for i in mgr.info()}["demo.bad"]
        self.assertEqual(info["failures"], 2)
        # --build ignores the wait; a success clears the count.
        ds.build = lambda c: c.write_json("ok.json", {})
        self.assertTrue(mgr.build("demo.bad"))
        self.assertEqual((ds.failures, ds.next_attempt, ds.last_error), (0, None, None))

    def test_repeated_and_deferred_failures_log_at_info(self):
        mgr = self.app.datasets
        with self.assertLogs("meridian.test.quality", level="INFO") as logs:
            mgr.build("demo.bad")
            mgr.build("demo.bad")
            mgr.build("demo.deferred")
        levels = [(r.levelname, "demo.bad" in r.getMessage()) for r in logs.records if "failed" in r.getMessage()]
        self.assertEqual(levels, [("WARNING", True), ("INFO", True), ("INFO", False)])


class VersionTests(unittest.TestCase):
    def test_version_is_the_programme_2_release(self):
        self.assertEqual(meridian.__version__, "0.2.0")


if __name__ == "__main__":
    unittest.main()
