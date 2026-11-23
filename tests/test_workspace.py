"""Workspace module tests: state and place validation, the built-in presets, storage (CRUD, unique names,
import), and the HTTP routes behind the standard guards. No network.

Run: python -m unittest tests.test_workspace -v
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from meridian.config import Config
from meridian.core_api import register_core
from meridian.datasets import DatasetManager
from meridian.httpserver import make_server
from meridian.modules import load_all
from meridian.modules.workspace import db, model
from meridian.net import Net
from meridian.registry import App
from meridian.scheduler import Scheduler
from meridian.store import Store

ROOT = Path(__file__).resolve().parent.parent
NOW = 1795400000.0
GOOD_STATE = {"v": 1, "view": {"center": [24.9, 59.6], "zoom": 6.2, "bearing": 0, "pitch": 0}, "basemap": "dark",
              "projection": "mercator", "terrain": False,
              "layers": {"ships": {"on": True, "options": {"types": "tanker"}}, "ships.dark": {"on": True}}}


def make_app(modules: set[str] | None = None) -> tuple[App, str]:
    tmp = tempfile.mkdtemp(prefix="meridian-ws-test-")
    os.environ["MERIDIAN_DATA_DIR"] = tmp
    cfg = Config(None)
    log = logging.getLogger("meridian.test.workspace")
    app = App(cfg, Store(Path(tmp) / "t.db"), Net(cfg), log, Scheduler(log), DatasetManager(log))
    register_core(app)
    load_all(app, modules or {"workspace"})
    return app, tmp


# ---------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------
class StateTests(unittest.TestCase):
    def test_a_good_state_round_trips(self):
        self.assertEqual(model.clean_state(GOOD_STATE), {**GOOD_STATE, "view": {"center": [24.9, 59.6], "zoom": 6.2,
                                                                                 "bearing": 0.0, "pitch": 0.0}})
        self.assertEqual(model.clean_state(model.clean_state(GOOD_STATE)), model.clean_state(GOOD_STATE))

    def test_hostile_values_are_dropped_not_stored(self):
        s = model.clean_state({"v": 1, "view": {"center": [999, 95], "zoom": 1e308}, "basemap": "javascript:alert(1)",
                               "projection": "<script>", "terrain": "yes", "evil": {"x": 1},
                               "layers": {"ok.id": {"on": True, "options": {"a": float("nan"), "b": {"x": 1}, "c": [1],
                                                                            "bad key!": 1, "d": "x" * 999, "e": 2.5,
                                                                            "f": True}},
                                          "bad id": {"on": True}, "x" * 81: {"on": True}, "off.one": {"on": "true"},
                                          "": {"on": True}}})
        self.assertEqual(set(s), {"v", "layers"})
        self.assertEqual(s["layers"]["ok.id"]["options"], {"d": "x" * model.OPTION_STR_MAX, "e": 2.5, "f": True})
        self.assertEqual(s["layers"]["off.one"], {"on": False})
        self.assertEqual(set(s["layers"]), {"ok.id", "off.one"})

    def test_views_are_clamped_and_wrapped(self):
        v = model.clean_view({"center": [190.0, 10.0], "zoom": 40, "bearing": 270, "pitch": -5})
        self.assertEqual(v, {"center": [-170.0, 10.0], "zoom": 22.0, "bearing": -90.0, "pitch": 0.0})
        self.assertEqual(model.clean_view({"center": {"lng": 1, "lat": 2}})["center"], [1, 2])
        self.assertIsNone(model.clean_view({"center": [1]}))
        self.assertIsNone(model.clean_view({"center": [True, 2]}))

    def test_versions_sizes_and_shapes(self):
        for bad in (None, [], "x", {"v": 2}, {"v": True}):
            with self.assertRaises(model.InputError, msg=repr(bad)):
                model.clean_state(bad)
        with self.assertRaises(model.InputError):
            model.clean_state({"v": 1, "notes": "", "layers": {f"layer.{i}": {"on": True, "options": {f"o{j}": "y" * 250 for j in range(40)}}
                                                           for i in range(10)}})
        s = model.clean_state({"layers": ["ships", "flights", 3, "bad id"]})
        self.assertEqual(s["layers"], {"ships": {"on": True}, "flights": {"on": True}})
        many = model.clean_state({"layers": {f"l{i}": {"on": True} for i in range(500)}})
        self.assertEqual(len(many["layers"]), model.MAX_LAYERS)

    def test_notes_and_names_lose_control_characters(self):
        self.assertEqual(model.clean_state({"notes": "a\x00b\u202ec\r\nd"})["notes"], "abc\nd")
        self.assertEqual(model.clean_name("  Baltic \t  watch\x1b "), "Baltic watch")
        for bad in ("", "   ", None, 3):
            with self.assertRaises(model.InputError):
                model.clean_name(bad)
        self.assertEqual(len(model.clean_name("x" * 200)), model.NAME_MAX)

    def test_places(self):
        p = model.clean_place({"name": " Home ", "type": "home", "lat": 51.50735123, "lon": -0.1277, "radius_km": 25})
        self.assertEqual(p, {"name": "Home", "type": "home", "lat": 51.50735, "lon": -0.1277, "radius_km": 25.0, "zoom": 11.0})
        for bad in ({"name": "x", "type": "palace", "lat": 0, "lon": 0}, {"name": "x", "lat": 91, "lon": 0},
                    {"name": "x", "lat": 0, "lon": 181}, {"name": "", "lat": 0, "lon": 0},
                    {"name": "x", "lat": 0, "lon": 0, "radius_km": 0.5}, {"name": "x", "lat": 0, "lon": 0, "radius_km": 5000},
                    {"name": "x", "lat": "0", "lon": 0}, {"name": "x", "lat": True, "lon": 0}, "nope"):
            with self.assertRaises(model.InputError, msg=repr(bad)):
                model.clean_place(bad)
        self.assertEqual(model.clean_place({"notes": "hi\x07"}, partial=True), {"notes": "hi"})
        self.assertEqual(model.clean_place({"name": "x", "lat": 0, "lon": 0, "zoom": 99})["zoom"], 18.0)


class PresetTests(unittest.TestCase):
    def test_the_five_presets(self):
        self.assertEqual([p["name"] for p in model.PRESETS], ["Middle East watch", "UK weather day", "Maritime chokepoints",
                                                              "Space and aurora tonight", "Disaster response"])
        for p in model.PRESETS:
            self.assertTrue(p["id"].startswith("builtin:") and p["builtin"])
            self.assertEqual(model.clean_state(p["state"]), p["state"], p["name"])
            self.assertIn("view", p["state"])
            self.assertGreaterEqual(len(p["state"]["layers"]), 4, p["name"])
            self.assertTrue(p["description"].endswith("."))
        self.assertEqual(model.preset("builtin:space-aurora")["state"]["projection"], "globe")
        self.assertIsNone(model.preset("builtin:nope"))

    def test_preset_layers_and_options_exist_in_the_browser_code(self):
        """Every layer id a preset turns on is registered by some web/js/layers file, and every option
        it sets is declared there (the browser ignores unknown ones, so a typo would fail silently)."""
        sources = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "web" / "js" / "layers").rglob("*.js"))
        for p in model.PRESETS:
            for lid, entry in p["state"]["layers"].items():
                self.assertRegex(sources, rf"id: '{re.escape(lid)}'", f"{p['name']}: layer {lid}")
                for opt in entry.get("options", {}):
                    self.assertRegex(sources, rf"id: '{re.escape(opt)}', label", f"{p['name']}: option {opt} of {lid}")


# ---------------------------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------------------------
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meridian-ws-db-")
        self.store = Store(Path(self.tmp) / "t.db")
        db.setup(self.store)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_crud_and_unique_names(self):
        wid = db.create_workspace(self.store, "Baltic watch", GOOD_STATE, NOW)
        self.assertEqual(db.get_workspace(self.store, wid)["state"]["layers"]["ships"]["options"], {"types": "tanker"})
        with self.assertRaises(db.NameTaken):
            db.create_workspace(self.store, "  BALTIC   watch ", {"v": 1}, NOW)
        other = db.create_workspace(self.store, "Gulf", {"v": 1}, NOW + 1)
        with self.assertRaises(db.NameTaken):
            db.update_workspace(self.store, other, NOW + 2, name="baltic watch")
        self.assertTrue(db.update_workspace(self.store, wid, NOW + 3, name="Baltic and Gulf of Finland"))
        self.assertTrue(db.update_workspace(self.store, wid, NOW + 4, state={"v": 1, "basemap": "relief"}))
        w = db.get_workspace(self.store, wid)
        self.assertEqual((w["name"], w["state"], w["summary"]), ("Baltic and Gulf of Finland", {"v": 1, "basemap": "relief"},
                                                                 "layers unchanged, relief map"))
        self.assertEqual([x["name"] for x in db.list_workspaces(self.store)], ["Baltic and Gulf of Finland", "Gulf"])
        with self.assertRaises(model.InputError):
            db.update_workspace(self.store, wid, NOW)
        self.assertFalse(db.update_workspace(self.store, 999, NOW, name="x"))
        self.assertTrue(db.delete_workspace(self.store, wid))
        self.assertFalse(db.delete_workspace(self.store, wid))
        self.assertEqual(db.count_workspaces(self.store), 1)

    def test_limits(self):
        old = db.MAX_WORKSPACES
        db.MAX_WORKSPACES = 2
        try:
            db.create_workspace(self.store, "a", {"v": 1}, NOW)
            db.create_workspace(self.store, "b", {"v": 1}, NOW)
            with self.assertRaises(model.InputError):
                db.create_workspace(self.store, "c", {"v": 1}, NOW)
            out = db.import_workspaces(self.store, [{"name": "a", "state": {"v": 1, "basemap": "light"}},
                                                    {"name": "d", "state": {"v": 1}}], NOW)
            self.assertEqual((out["created"], out["replaced"]), (0, 1))
            self.assertEqual(out["skipped"][0]["name"], "d")
        finally:
            db.MAX_WORKSPACES = old

    def test_import_merges_by_name_and_skips_what_it_cannot_use(self):
        db.create_workspace(self.store, "Baltic watch", {"v": 1}, NOW)
        out = db.import_workspaces(self.store, [
            {"name": "baltic WATCH", "state": GOOD_STATE},
            {"name": "New one", "state": {"v": 1, "layers": ["hazards.quakes"]}},
            {"name": "new ONE", "state": {"v": 1}},
            {"name": "Future", "state": {"v": 9}},
            {"name": "", "state": {"v": 1}},
            "junk",
        ], NOW + 5)
        self.assertEqual((out["created"], out["replaced"]), (1, 1))
        self.assertEqual([s["index"] for s in out["skipped"]], [2, 3, 4, 5])
        names = {w["name"]: w for w in db.list_workspaces(self.store)}
        self.assertEqual(set(names), {"Baltic watch", "New one"})
        self.assertEqual(names["Baltic watch"]["state"]["layers"]["ships"], {"on": True, "options": {"types": "tanker"}})
        with self.assertRaises(model.InputError):
            db.import_workspaces(self.store, {"name": "x"}, NOW)

    def test_places_crud_and_watchlist_link(self):
        pid = db.create_place(self.store, {"name": "Home", "type": "home", "lat": 51.5, "lon": -0.12}, NOW)
        self.assertTrue(db.update_place(self.store, pid, {"name": "Home sweet home", "radius_km": 10}, NOW + 1))
        self.assertTrue(db.set_place_watchlist(self.store, pid, 7, NOW + 2))
        p = db.get_place(self.store, pid)
        self.assertEqual((p["name"], p["radius_km"], p["alerts_watchlist_id"], p["lat"]), ("Home sweet home", 10.0, 7, 51.5))
        with self.assertRaises(model.InputError):
            db.update_place(self.store, pid, {}, NOW)
        with self.assertRaises(model.InputError):
            db.update_place(self.store, pid, {"lat": 95}, NOW)
        self.assertTrue(db.delete_place(self.store, pid))
        self.assertEqual(db.list_places(self.store), [])


# ---------------------------------------------------------------------------------------------
# HTTP, through the real server and its guards
# ---------------------------------------------------------------------------------------------
class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.tmp = make_app()
        assert cls.app.modules["workspace"].ok, cls.app.modules["workspace"].error
        cls.srv = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.app.bind[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.app.store.close()
        os.environ.pop("MERIDIAN_DATA_DIR", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, path, body=None, *, headers=None, method=None, raw=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = dict(headers) if headers is not None else (
            {"Origin": f"http://127.0.0.1:{self.port}", "X-Meridian": "1", "Content-Type": "application/json"} if data is not None else {})
        r = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"), headers=hdrs)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def test_presets_and_crud(self):
        st, body = self.req("/api/workspace/workspaces")
        self.assertEqual(st, 200)
        self.assertEqual(len(body["presets"]), 5)
        self.assertEqual(body["max"], db.MAX_WORKSPACES)
        st, p = self.req("/api/workspace/workspaces/builtin:maritime")
        self.assertEqual((st, p["name"]), (200, "Maritime chokepoints"))
        self.assertEqual(self.req("/api/workspace/workspaces/builtin:nope")[0], 404)
        st, made = self.req("/api/workspace/workspaces", {"action": "create", "name": "HTTP one", "state": GOOD_STATE})
        self.assertEqual(st, 200)
        wid = made["id"]
        self.assertEqual(made["workspace"]["summary"], "2 layers, dark map")
        st, err = self.req("/api/workspace/workspaces", {"action": "create", "name": "http ONE", "state": {"v": 1}})
        self.assertEqual((st, err["error"]["code"]), (409, "NAME_TAKEN"))
        st, body = self.req("/api/workspace/workspaces", {"action": "rename", "id": wid, "name": "HTTP two"})
        self.assertEqual((st, body["workspace"]["name"]), (200, "HTTP two"))
        st, body = self.req("/api/workspace/workspaces", {"action": "update", "id": str(wid), "state": {"v": 1, "terrain": True}})
        self.assertEqual((st, body["workspace"]["state"]), (200, {"v": 1, "terrain": True}))
        self.assertEqual(self.req(f"/api/workspace/workspaces/{wid}")[1]["name"], "HTTP two")
        st, body = self.req("/api/workspace/workspaces", {"action": "delete", "id": wid})
        self.assertEqual((st, body), (200, {"ok": True}))
        self.assertEqual(self.req(f"/api/workspace/workspaces/{wid}")[0], 404)
        self.assertEqual(self.req("/api/workspace/workspaces", {"action": "delete", "id": wid})[0], 404)

    def test_bad_requests(self):
        cases = [
            ({"action": "explode"}, 400), ({"action": "create", "name": "x"}, 400), ({"action": "create", "state": {}}, 400),
            ({"action": "create", "name": "x", "state": {"v": 7}}, 400), ({"action": "create", "name": "  ", "state": {}}, 400),
            ({"action": "delete", "id": "1; DROP TABLE workspace_saved"}, 400), ({"action": "delete", "id": 1.5}, 400),
            ({"action": "delete", "id": True}, 400), ({"action": "import", "workspaces": "nope"}, 400),
            ({"action": "import", "file": {"kind": "other", "workspaces": []}}, 400), ({"name": "no action"}, 400),
        ]
        for body, status in cases:
            st, err = self.req("/api/workspace/workspaces", body)
            self.assertEqual(st, status, body)
            self.assertIn("error", err)
        self.assertEqual(self.req("/api/workspace/workspaces/abc")[0], 400)
        self.assertEqual(self.req("/api/workspace/workspaces", raw=b"{not json")[0], 400)
        self.assertEqual(self.oversized("/api/workspace/workspaces", 600 * 1024), 413)
        self.assertEqual(self.oversized("/api/workspace/places", 17 * 1024), 413)

    def oversized(self, path, length):
        """Announce a body larger than the route takes and send none: the server answers from the headers."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", path, skip_accept_encoding=True)
            for k, v in (("Origin", f"http://127.0.0.1:{self.port}"), ("X-Meridian", "1"),
                         ("Content-Type", "application/json"), ("Content-Length", str(length))):
                conn.putheader(k, v)
            conn.endheaders()
            return conn.getresponse().status
        finally:
            conn.close()

    def test_guards(self):
        origin = f"http://127.0.0.1:{self.port}"
        body = json.dumps({"action": "create", "name": "guarded", "state": {"v": 1}}).encode()
        for hdrs, code in (({"Content-Type": "application/json", "X-Meridian": "1"}, "FORBIDDEN_ORIGIN"),
                           ({"Content-Type": "application/json", "X-Meridian": "1", "Origin": "http://evil.example"}, "FORBIDDEN_ORIGIN"),
                           ({"Content-Type": "application/json", "Origin": origin}, "MISSING_CSRF_HEADER"),
                           ({"Content-Type": "text/plain", "Origin": origin, "X-Meridian": "1"}, "UNSUPPORTED_MEDIA_TYPE")):
            st, err = self.req("/api/workspace/workspaces", raw=body, headers=hdrs)
            self.assertIn(st, (403, 415), hdrs)
            self.assertEqual(err["error"]["code"], code)
        st, err = self.req("/api/workspace/places", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((st, err["error"]["code"]), (403, "CROSS_SITE"))
        self.assertEqual(self.req("/api/workspace/places", headers={"Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertNotIn("guarded", [w["name"] for w in self.req("/api/workspace/workspaces")[1]["workspaces"]])

    def test_export_and_import_round_trip(self):
        self.req("/api/workspace/workspaces", {"action": "create", "name": "Round trip", "state": GOOD_STATE})
        st, doc = self.req("/api/workspace/export")
        self.assertEqual((st, doc["kind"], doc["v"]), (200, "meridian-workspaces", 1))
        self.assertIn("Round trip", [w["name"] for w in doc["workspaces"]])
        self.assertRegex(doc["exported_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        doc["workspaces"].append({"name": "From another PC", "state": {"v": 1, "projection": "globe"}})
        st, out = self.req("/api/workspace/workspaces", {"action": "import", "file": doc})
        self.assertEqual(st, 200)
        self.assertGreaterEqual(out["replaced"], 1)
        self.assertEqual(out["created"], 1)
        names = [w["name"] for w in out["workspaces"]]
        self.assertIn("From another PC", names)
        self.assertEqual(names.count("Round trip"), 1)

    def test_places_routes(self):
        st, made = self.req("/api/workspace/places", {"action": "create", "place": {"name": "Work", "type": "work", "lat": 51.52, "lon": -0.08}})
        self.assertEqual(st, 200)
        pid = made["id"]
        st, body = self.req("/api/workspace/places", {"action": "update", "id": pid, "place": {"radius_km": 50, "type": "family"}})
        self.assertEqual((st, body["place"]["radius_km"], body["place"]["type"]), (200, 50.0, "family"))
        st, body = self.req("/api/workspace/places", {"action": "watchlist", "id": pid, "watchlist_id": 12})
        self.assertEqual(body["place"]["alerts_watchlist_id"], 12)
        st, body = self.req("/api/workspace/places", {"action": "watchlist", "id": pid, "watchlist_id": None})
        self.assertIsNone(body["place"]["alerts_watchlist_id"])
        self.assertEqual(self.req("/api/workspace/places", {"action": "watchlist", "id": pid, "watchlist_id": "x"})[0], 400)
        self.assertEqual(self.req("/api/workspace/places", {"action": "create", "place": {"name": "x", "lat": 99, "lon": 0}})[0], 400)
        self.assertEqual(self.req("/api/workspace/places", {"action": "update", "id": 9999, "place": {"name": "x"}})[0], 404)
        st, listing = self.req("/api/workspace/places")
        self.assertIn("Work", [p["name"] for p in listing["places"]])
        self.assertEqual(self.req("/api/workspace/places", {"action": "delete", "id": pid})[1], {"ok": True})

    def test_places_are_not_offered_to_the_assistant(self):
        names = [t["name"] for t in self.app.ai_tools()]
        self.assertFalse([n for n in names if "place" in n], names)
        spec = next(s for s in self.app.layer_specs if s["id"] == "workspace.places")
        self.assertNotIn("_count", spec)   # not even how many places there are


if __name__ == "__main__":
    unittest.main()
