"""Core tests: guards, routing, request parsing, geo, store, datasets, net and an end-to-end server.

Run: python -m unittest tests.test_core -v   (no internet needed; a local HTTP server stands in)
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from meridian import geo
from meridian.config import Config, parse_env_file
from meridian.datasets import DatasetManager
from meridian.guards import BindRefused, check_request, resolve_bind
from meridian.httpserver import make_server
from meridian.net import Net, NetError
from meridian.registry import App, HTTPError, Request, compile_pattern, json_bytes
from meridian.scheduler import Scheduler
from meridian.store import Store

HOST = ("Host", "127.0.0.1:8790")


class GuardTests(unittest.TestCase):
    def check(self, method, path, headers):
        return check_request(method, path, headers, "127.0.0.1", 8790)

    def test_bind_only_loopback(self):
        self.assertEqual(resolve_bind("127.0.0.1")[0], "127.0.0.1")
        self.assertEqual(resolve_bind(" localhost ")[0], "127.0.0.1")
        for bad in ("0.0.0.0", "", "10.20.30.5", "::", "127.0.0.2", "example.com"):
            with self.assertRaises(BindRefused):
                resolve_bind(bad)

    def test_host_header(self):
        self.assertIsNone(self.check("GET", "/", [HOST]))
        self.assertIsNone(self.check("GET", "/", [("Host", "localhost:8790")]))
        self.assertEqual(self.check("GET", "/", [("Host", "evil.example")]).code, "FORBIDDEN_HOST")
        self.assertEqual(self.check("GET", "/", [HOST, HOST]).code, "FORBIDDEN_HOST")
        self.assertEqual(self.check("GET", "/", []).code, "FORBIDDEN_HOST")

    def test_proxy_and_method(self):
        self.assertEqual(self.check("GET", "/", [HOST, ("X-Forwarded-For", "1.2.3.4")]).code, "PROXY_HEADER")
        self.assertEqual(self.check("PUT", "/api/x", [HOST]).code, "METHOD_NOT_ALLOWED")
        self.assertEqual(self.check("POST", "/index.html", [HOST]).code, "METHOD_NOT_ALLOWED")

    def test_post_needs_origin_marker_and_json(self):
        ok = [HOST, ("Origin", "http://127.0.0.1:8790"), ("X-Meridian", "1"), ("Content-Type", "application/json"),
              ("Content-Length", "2")]
        self.assertIsNone(self.check("POST", "/api/core/favourites", ok))
        self.assertEqual(self.check("POST", "/api/a", [h for h in ok if h[0] != "Origin"]).code, "FORBIDDEN_ORIGIN")
        self.assertEqual(self.check("POST", "/api/a", [h for h in ok if h[0] != "X-Meridian"]).code, "MISSING_CSRF_HEADER")
        bad_type = [h if h[0] != "Content-Type" else ("Content-Type", "text/plain") for h in ok]
        self.assertEqual(self.check("POST", "/api/a", bad_type).code, "UNSUPPORTED_MEDIA_TYPE")
        self.assertEqual(self.check("POST", "/api/a", ok + [("Transfer-Encoding", "chunked")]).code, "BAD_REQUEST")
        self.assertEqual(self.check("POST", "/api/a", [h for h in ok if h[0] != "Content-Length"]).code, "LENGTH_REQUIRED")
        self.assertEqual(self.check("POST", "/api/a", ok + [("Sec-Fetch-Site", "cross-site")]).code, "CROSS_SITE")

    def test_cross_site_reads_of_api_refused(self):
        self.assertEqual(self.check("GET", "/api/core/manifest", [HOST, ("Sec-Fetch-Site", "cross-site")]).code, "CROSS_SITE")
        self.assertIsNone(self.check("GET", "/api/core/manifest", [HOST, ("Sec-Fetch-Site", "same-origin")]))
        self.assertIsNone(self.check("GET", "/", [HOST, ("Sec-Fetch-Site", "cross-site")]))  # the page itself

    def test_odd_targets(self):
        self.assertEqual(self.check("GET", "//evil", [HOST]).code, "BAD_REQUEST")
        self.assertEqual(self.check("GET", "http://x/", [HOST]).code, "BAD_REQUEST")


class RegistryTests(unittest.TestCase):
    def test_patterns(self):
        rx = compile_pattern("/api/x/things/{tid}")
        self.assertEqual(rx.match("/api/x/things/abc").group("tid"), "abc")
        self.assertIsNone(rx.match("/api/x/things/a/b"))
        rx2 = compile_pattern("/api/x/files/{rest:path}")
        self.assertEqual(rx2.match("/api/x/files/a/b.json").group("rest"), "a/b.json")

    def test_request_args(self):
        req = Request("GET", "/api/x", {}, {"n": ["5"], "f": ["nan"], "b": ["1,2,3,4"], "bad": ["x"]}, {})
        self.assertEqual(req.arg("n", type=int, min=1, max=10), 5)
        self.assertEqual(req.arg("missing", 7, type=int), 7)
        with self.assertRaises(HTTPError):
            req.arg("f", type=float)
        with self.assertRaises(HTTPError):
            req.arg("n", type=int, max=3)
        with self.assertRaises(HTTPError):
            req.arg("missing")
        self.assertEqual(req.bbox("b"), (1.0, 2.0, 3.0, 4.0))
        with self.assertRaises(HTTPError):
            req.bbox("bad")

    def test_json_bytes_cleans_nan(self):
        self.assertEqual(json.loads(json_bytes({"a": float("nan"), "b": [1.5, float("inf")]})), {"a": None, "b": [1.5, None]})


class GeoTests(unittest.TestCase):
    def test_distance_and_destination(self):
        d = geo.haversine_km(51.4700, -0.4543, 40.6413, -73.7781)  # LHR to JFK
        self.assertAlmostEqual(d, 5540, delta=15)
        lat, lon = geo.destination(0, 0, 90, 111.195)
        self.assertAlmostEqual(lat, 0, places=3)
        self.assertAlmostEqual(lon, 1.0, places=2)

    def test_bbox_antimeridian(self):
        bb = geo.parse_bbox("170,-10,-170,10")
        self.assertTrue(geo.in_bbox(0, 175, bb))
        self.assertTrue(geo.in_bbox(0, -175, bb))
        self.assertFalse(geo.in_bbox(0, 0, bb))

    def test_polygon(self):
        square = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
                                                      [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]]}
        self.assertTrue(geo.point_in_geometry(2, 2, square))
        self.assertFalse(geo.point_in_geometry(5, 5, square))   # in the hole
        self.assertFalse(geo.point_in_geometry(11, 5, square))

    def test_polygon_across_the_antimeridian(self):
        # ctx.draw keeps continuous longitudes for shapes that cross 180 degrees (CONTRACT 15.3).
        box = {"type": "Polygon", "coordinates": [[[175, -5], [185, -5], [185, 5], [175, 5], [175, -5]]]}
        self.assertTrue(geo.point_in_geometry(178, 0, box))
        self.assertTrue(geo.point_in_geometry(-178, 0, box))     # 182 east
        self.assertTrue(geo.point_in_geometry(180, 0, box))
        self.assertFalse(geo.point_in_geometry(-170, 0, box))
        self.assertFalse(geo.point_in_geometry(0, 0, box))
        west = {"type": "Polygon", "coordinates": [[[-185, -5], [-175, -5], [-175, 5], [-185, 5], [-185, -5]]]}
        self.assertTrue(geo.point_in_geometry(178, 0, west))      # -182
        self.assertFalse(geo.point_in_geometry(170, 0, west))

    def test_grid_index(self):
        idx = geo.GridIndex(1.0)
        idx.add(51.5, -0.12, "london")
        idx.add(48.85, 2.35, "paris")
        idx.add(0.0, 179.9, "east")
        idx.add(0.0, -179.9, "west")
        self.assertEqual([i for _, i in idx.near(51.5, -0.1, 50)], ["london"])
        self.assertEqual(len(idx.near(51.0, 1.0, 400)), 2)
        self.assertEqual(sorted(i for _, i in idx.near(0.0, 180.0, 30)), ["east", "west"])
        self.assertEqual(sorted(idx.in_bbox((170, -1, -170, 1))), ["east", "west"])


class StoreAndConfigTests(unittest.TestCase):
    def test_store_kv_and_tx(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            s = Store(Path(d) / "t.db")
            s.kv_set("a", {"x": 1})
            self.assertEqual(s.kv_get("a"), {"x": 1})
            s.script("CREATE TABLE IF NOT EXISTS t_rows (v INTEGER)")
            with self.assertRaises(RuntimeError):
                with s.tx() as c:
                    c.execute("INSERT INTO t_rows VALUES (1)")
                    raise RuntimeError("roll back")
            self.assertEqual(s.scalar("SELECT COUNT(*) FROM t_rows"), 0)
            s.close()

    def test_env_parsing_and_redaction(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            p = Path(d) / ".env"
            p.write_text('# comment\nexport ONE_KEY="abc123secret"\nTWO=plain # note\nEMPTY=\n', encoding="utf-8")
            vals = parse_env_file(p)
            self.assertEqual(vals["ONE_KEY"], "abc123secret")
            self.assertEqual(vals["TWO"], "plain")
            cfg = Config(p)
            cfg.declare("ONE_KEY", description="x")
            self.assertIsNone(cfg.get("EMPTY"))
            self.assertEqual(cfg.redact("url?key=abc123secret&x=1"), "url?key=***&x=1")
            status = cfg.key_status()
            self.assertTrue(status[0]["configured"])
            self.assertNotIn("value", status[0])


class _Local(BaseHTTPRequestHandler):
    hits = {"n": 0}

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Local.hits["n"] += 1
        if self.path.startswith("/flaky") and _Local.hits["n"] % 2 == 1:
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"path": self.path, "n": _Local.hits["n"]}).encode()
        if "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body)
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Local)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_gzip_cache_retry(self):
        net = Net(Config(None))
        a = net.get_json(self.base + "/x", ttl=60)
        b = net.get_json(self.base + "/x", ttl=60)
        self.assertEqual(a, b)                      # second came from the cache
        self.assertEqual(net.stats["cache_hits"], 1)
        r = net.get_json(self.base + "/flaky", retries=2)
        self.assertEqual(r["path"], "/flaky")
        with self.assertRaises(NetError):
            net.get_json("file:///etc/passwd")

    def test_download_atomic(self):
        net = Net(Config(None))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            dest = Path(d) / "raw" / "a.json"
            net.download(self.base + "/file", dest)
            self.assertTrue(dest.exists())
            self.assertTrue(dest.with_name("a.json.meta.json").exists())
            before = dest.stat().st_mtime
            net.download(self.base + "/file", dest, max_age_hours=1)   # fresh: no refetch
            self.assertEqual(dest.stat().st_mtime, before)


def _make_app(tmp: Path) -> App:
    os.environ["MERIDIAN_DATA_DIR"] = str(tmp)
    cfg = Config(None)
    log = logging.getLogger("meridian.test")
    store = Store(tmp / "t.db")
    app = App(cfg, store, Net(cfg), log, Scheduler(log), DatasetManager(log))
    return app


class DatasetTests(unittest.TestCase):
    def test_build_and_listeners(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            app = _make_app(Path(d))
            m = app.module("demo")
            seen = []

            def build(ds):
                ds.write_json("out.json", {"ok": True})
                ds.set_rows(1)

            def broken(ds):
                raise ValueError("source down")

            m.dataset("good", build, max_age_hours=1)
            m.dataset("bad", broken, max_age_hours=1, after=("good",))
            m.on_dataset("good", lambda: seen.append("good"))
            res = app.datasets.build_all()
            self.assertEqual(res, {"demo.good": True, "demo.bad": False})
            self.assertEqual(seen, ["good"])
            self.assertTrue(m.dataset_ready("good"))
            self.assertFalse(m.dataset_ready("bad"))
            info = {i["key"]: i for i in app.datasets.info()}
            self.assertIn("source down", info["demo.bad"]["last_error"])
            app.store.close()
            os.environ.pop("MERIDIAN_DATA_DIR", None)

    def test_background_round_waits_for_dependencies_to_finish(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            app = _make_app(Path(d))
            m = app.module("demo")
            order = []

            def slow(ds):
                order.append(("slow-start", time.monotonic()))
                time.sleep(0.6)
                ds.write_json("slow.json", {})
                order.append(("slow-end", time.monotonic()))

            def dependant(ds):
                order.append(("dep-start", time.monotonic()))
                ds.write_json("dep.json", {})

            m.dataset("slow", slow, max_age_hours=1)
            m.dataset("dep", dependant, max_age_hours=1, after=("slow",))
            stop = threading.Event()
            app.datasets._build_round(stop)
            names = [n for n, _ in order]
            self.assertEqual(names, ["slow-start", "slow-end", "dep-start"])
            self.assertTrue(m.dataset_ready("dep"))
            app.store.close()
            os.environ.pop("MERIDIAN_DATA_DIR", None)


class ServerEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        app = _make_app(Path(cls.tmp.name))
        from meridian.core_api import register_core
        register_core(app)
        m = app.module("demo")

        @m.route("GET", "/api/demo/echo/{word}")
        def echo(req):
            return {"word": req.params["word"], "n": req.arg("n", 1, type=int)}

        @m.route("POST", "/api/demo/post")
        def post(req):
            return {"got": req.field("x", type=int)}

        cls.srv = make_server(app, "127.0.0.1", 0)
        cls.port = app.bind[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.environ.pop("MERIDIAN_DATA_DIR", None)
        try:
            cls.tmp.cleanup()
        except OSError:
            pass

    def req(self, path, *, method="GET", body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), dict(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}"), dict(err.headers)

    def test_routes_and_errors(self):
        st, body, hdrs = self.req("/api/demo/echo/hi?n=3")
        self.assertEqual((st, body), (200, {"word": "hi", "n": 3}))
        self.assertIn("default-src 'self'", hdrs["Content-Security-Policy"])
        self.assertEqual(hdrs["X-Content-Type-Options"], "nosniff")
        st, body, _ = self.req("/api/demo/echo/hi?n=x")
        self.assertEqual((st, body["error"]["code"]), (400, "BAD_ARG"))
        st, body, _ = self.req("/api/nope")
        self.assertEqual(st, 404)

    def test_post_guards(self):
        good = {"Origin": f"http://127.0.0.1:{self.port}", "X-Meridian": "1", "Content-Type": "application/json"}
        st, body, _ = self.req("/api/demo/post", method="POST", body={"x": 4}, headers=good)
        self.assertEqual((st, body), (200, {"got": 4}))
        st, body, _ = self.req("/api/demo/post", method="POST", body={"x": 4}, headers={"Content-Type": "application/json"})
        self.assertEqual(st, 403)
        st, body, _ = self.req("/api/demo/post", method="POST", body={"x": "four"}, headers=good)
        self.assertEqual((st, body["error"]["code"]), (400, "BAD_FIELD"))

    def test_static_and_traversal(self):
        url = f"http://127.0.0.1:{self.port}/js/core/app.js"
        with urllib.request.urlopen(url, timeout=10) as resp:
            self.assertTrue(resp.headers["Content-Type"].startswith("text/javascript"))
        for bad in ("/../server.py", "/%2e%2e/server.py", "/js/../../meridian/config.py"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}{bad}", timeout=10)
                self.fail(f"{bad} was served")
            except urllib.error.HTTPError as err:
                self.assertIn(err.code, (400, 403, 404))

    def test_casefiles_flow(self):
        good = {"Origin": f"http://127.0.0.1:{self.port}", "X-Meridian": "1", "Content-Type": "application/json"}
        st, body, _ = self.req("/api/core/casefiles", method="POST", body={"action": "create", "title": "Red Sea"}, headers=good)
        cid = body["id"]
        self.req("/api/core/casefiles", method="POST", headers=good, body={"action": "add_item", "id": cid,
                 "item": {"kind": "ship", "ref": "123456789", "title": "EVER TEST", "lat": 12.5, "lon": 43.3}})
        st, body, _ = self.req(f"/api/core/casefiles/{cid}")
        self.assertEqual(body["items"][0]["title"], "EVER TEST")
        self.req("/api/core/casefiles", method="POST", headers=good, body={"action": "delete", "id": cid})
        st, body, _ = self.req(f"/api/core/casefiles/{cid}")
        self.assertEqual(st, 404)


class SecurityRegressionTests(unittest.TestCase):
    """Fixes from the security review (2026-11-23)."""

    def test_gzip_bomb_is_refused(self):
        from meridian.net import _decode, DecodedTooLarge
        bomb = gzip.compress(b"\0" * 5_000_000)
        self.assertLess(len(bomb), 20_000)
        with self.assertRaises(DecodedTooLarge):
            _decode(bomb, "gzip", max_bytes=1_000_000)
        self.assertEqual(len(_decode(bomb, "gzip", max_bytes=5_000_000)), 5_000_000)
        import zlib
        self.assertEqual(_decode(zlib.compress(b"hello"), "deflate", 100), b"hello")

    def test_redaction_covers_url_encoded_keys(self):
        cfg = Config(None, overrides={"X_API_KEY": "a/b+c=d&e"})
        cfg.declare("X_API_KEY", description="x")
        from urllib.parse import quote, quote_plus
        for text in (f"key={quote('a/b+c=d&e', safe='')}", f"key={quote_plus('a/b+c=d&e')}", "key=a/b+c=d&e"):
            self.assertNotIn("a/b", cfg.redact(text).replace("***", ""), text)

    def test_ids_are_bounded(self):
        from meridian.core_api import _int
        self.assertEqual(_int("42"), 42)
        self.assertEqual(_int(7.0), 7)
        for bad in ("9" * 40, 1e999, 2.5, "١٢", "4²", True, None, [], 2 ** 70):
            with self.assertRaises(HTTPError, msg=repr(bad)):
                _int(bad)


class ServerHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        app = _make_app(Path(cls.tmp.name))
        from meridian.core_api import register_core
        register_core(app)
        cls.app = app
        cls.srv = make_server(app, "127.0.0.1", 0)
        cls.port = app.bind[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.app.store.close()
        os.environ.pop("MERIDIAN_DATA_DIR", None)

    def status(self, path, **kw):
        try:
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", **kw), timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as err:
            return err.code

    def test_too_many_query_fields_is_a_400(self):
        q = "&".join(f"a{i}=1" for i in range(300))
        self.assertEqual(self.status(f"/api/core/search?q=xx&{q}"), 400)

    def test_huge_casefile_id_is_a_400(self):
        self.assertEqual(self.status("/api/core/casefiles/" + "9" * 40), 400)
        body = json.dumps({"action": "rename", "id": 1e300, "title": "x"}).encode()
        hdrs = {"Origin": f"http://127.0.0.1:{self.port}", "X-Meridian": "1", "Content-Type": "application/json"}
        self.assertEqual(self.status("/api/core/casefiles", data=body, method="POST", headers=hdrs), 400)


if __name__ == "__main__":
    unittest.main()


class CallRouteTests(unittest.TestCase):
    def test_in_process_route_call(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            app = _make_app(Path(d))
            m = app.module("demo")

            @m.route("GET", "/api/demo/echo/{word}")
            def echo(req):
                return {"word": req.params["word"], "n": req.arg("n", 1, type=int)}

            self.assertEqual(app.call_route("GET", "/api/demo/echo/hi", {"n": 3}), {"word": "hi", "n": 3})
            with self.assertRaises(HTTPError) as ctx:
                app.call_route("GET", "/api/nothing/here")
            self.assertEqual(ctx.exception.status, 404)
            with self.assertRaises(HTTPError):
                app.call_route("GET", "/api/demo/echo/hi", {"n": "x"})
            app.store.close()
            os.environ.pop("MERIDIAN_DATA_DIR", None)


class RouteActivityTests(unittest.TestCase):
    def test_activity_false_routes_do_not_mark_the_module_active(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            app = _make_app(Path(d))
            m = app.module("demo")

            @m.route("GET", "/api/demo/live")
            def live(req):
                return {"ok": True}

            @m.route("GET", "/api/demo/history", activity=False)
            def hist(req):
                return {"ok": True}

            srv = make_server(app, "127.0.0.1", 0)
            port = app.bind[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/demo/history", timeout=10).read()
                self.assertFalse(m.active())
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/demo/live", timeout=10).read()
                self.assertTrue(m.active())
            finally:
                srv.shutdown()
                srv.server_close()
                app.store.close()
                os.environ.pop("MERIDIAN_DATA_DIR", None)
