"""Integration tests for the intel module: register it on a real App with a temporary database and an
offline network stub, ingest the fixture export, and call routes, providers and AI tools directly."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.net import NetError
from meridian.registry import App, HTTPError, Request
from meridian.scheduler import Scheduler
from meridian.store import Store
from meridian.modules import intel
from meridian.modules.intel import db, gdelt

FIX = Path(__file__).parent / "fixtures" / "intel"


class OfflineNet:
    """Every request fails like an unreachable host; host intervals are accepted and ignored."""

    def __init__(self):
        self.stats = {"requests": 0, "errors": 0, "cache_hits": 0, "bytes": 0}
        self.calls = []

    def set_host_interval(self, host, seconds):
        pass

    def _fail(self, url, **kw):
        self.calls.append(url)
        raise NetError("offline in tests", url=url)

    get_json = get_text = get_bytes = request = download = _fail


class IntelServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="intel-test-"))
        config = Config(env_file=None, overrides={"MERIDIAN_DATA_DIR": str(cls.tmp)})
        cls.store = Store(cls.tmp / "test.db")
        log = logging.getLogger("meridian.test")
        cls.app = App(config, cls.store, OfflineNet(), log, Scheduler(log), DatasetManager(log))
        cls.m = cls.app.module("intel")
        intel.register(cls.m)
        cls.app.modules["intel"].ok = True
        cls.svc = cls.m.intel
        # Re-date the fixture so its rows fall inside the windows used by the queries.
        rows = list(gdelt.iter_rows((FIX / "gdelt_sample.export.CSV").read_text(encoding="utf-8")))
        now_ts = gdelt.epoch_to_ts(gdelt.slot_floor(time.time()))
        for r in rows:
            r[gdelt.C_ADDED] = now_ts
            r[gdelt.C_DAY] = now_ts[:8]
        cls.stats = cls.svc.ingest_text("\n".join("\t".join(r) for r in rows) + "\n")
        cls.svc.derive()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path: str):
        split = urllib.parse.urlsplit(path)
        route, params, _ = self.app.match("GET", split.path)
        self.assertIsNotNone(route, path)
        query = urllib.parse.parse_qs(split.query)
        return route.handler(Request("GET", split.path, params, query, {}))

    def test_ingest_counts(self):
        self.assertEqual(self.stats["rows"], 10)
        self.assertEqual(self.stats["kept"], 5)
        self.assertEqual(self.stats["new"], 4)                            # the two Mumbai reports merged
        self.assertEqual(self.stats["news"], 6)
        self.assertEqual(db.count_events(self.store), 4)

    def test_conflict_routes(self):
        fc = self.get("/api/intel/conflict?days=1&min_severity=0")
        self.assertEqual(fc["type"], "FeatureCollection")
        ids = [f["properties"]["id"] for f in fc["features"]]
        self.assertGreaterEqual(len(ids), 3)
        props = fc["features"][0]["properties"]
        self.assertEqual(set(props), {"id", "s", "c", "k", "p", "l", "n", "t"})
        detail = self.get(f"/api/intel/conflict/{ids[0]}")
        self.assertEqual(detail["note"], "Machine-coded from news reports, not verified.")
        self.assertIn("action", detail)
        self.assertIsInstance(detail["sources_list"], list)
        bbox = self.get("/api/intel/conflict?bbox=60,0,100,40&days=1")      # India only
        self.assertTrue(all(60 <= f["geometry"]["coordinates"][0] <= 100 for f in bbox["features"]))
        with self.assertRaises(HTTPError) as err:
            self.get("/api/intel/conflict/not-an-id")
        self.assertEqual(err.exception.status, 400)
        with self.assertRaises(HTTPError) as err:
            self.get("/api/intel/conflict/gd0000000000000a")
        self.assertEqual(err.exception.status, 404)
        with self.assertRaises(HTTPError):
            self.get("/api/intel/conflict?days=99")

    def test_news_routes(self):
        fc = self.get("/api/intel/news?hours=24&min_severity=0")
        self.assertGreaterEqual(len(fc["features"]), 4)
        nid = fc["features"][0]["properties"]["id"]
        item = self.get(f"/api/intel/news/item/{nid}")
        self.assertEqual(item["source"], "gdelt")
        feeds = self.get("/api/intel/news/feeds?hours=24")
        self.assertEqual(len(feeds["theatres"]), 8)
        self.assertIn("doc", feeds)
        with self.assertRaises(HTTPError):
            self.get("/api/intel/news/feeds?theatre=atlantis")
        self.assertEqual(len(self.get("/api/intel/theatres")["theatres"]), 8)

    def test_building_states(self):
        for path in ("/api/intel/frontlines/meta", "/api/intel/ucdp"):
            with self.assertRaises(HTTPError) as err:
                self.get(path)
            self.assertEqual(err.exception.status, 503)
        resp = self.get("/api/intel/frontlines")
        self.assertEqual(resp.status, 503)                                  # served file not built yet
        self.assertIsNone(self.get("/api/intel/country-at?lat=48&lon=35")["country"])
        idx = self.get("/api/intel/countries/index")
        self.assertEqual(idx["countries"], {})
        status = self.get("/api/intel/status")
        self.assertEqual(status["events_14d"], 4)
        cyber = self.get("/api/intel/cyber")
        self.assertEqual(cyber["features"], [])

    def test_countries_after_load(self):
        build = self.m.data_dir
        build.mkdir(parents=True, exist_ok=True)
        fc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"iso2": "IN", "name": "India", "short": "India", "iso3": "IND", "continent": "Asia",
                                               "subregion": "Southern Asia", "pop_est": 1.4e9, "pop_year": 2019, "label": [79.0, 22.0], "note": ""},
             "geometry": {"type": "Polygon", "coordinates": [[[68, 7], [97, 7], [97, 36], [68, 36], [68, 7]]]}}]}
        (build / "countries.geojson").write_text(json.dumps(fc), encoding="utf-8")
        wb = {"built_at": time.time(), "failures": [], "indicators": {"pop": {"code": "SP.POP.TOTL", "label": "Population"}},
              "economies": {"IN": {"iso2": "IN", "iso3": "IND", "name": "India", "capital": "New Delhi", "lat": 28.6, "lon": 77.2,
                                   "region": "South Asia", "income": "Lower middle income",
                                   "ind": {"pop": [1.45e9, 2025], "stability": [-0.6, 2025]}}}}
        (build / "worldbank.json").write_text(json.dumps(wb), encoding="utf-8")
        self.svc.load_countries()
        self.svc.load_worldbank()
        self.svc.compute_index(time.time())
        hit = self.get("/api/intel/country-at?lat=19.07&lon=72.88")
        self.assertEqual(hit["country"]["iso2"], "IN")
        prof = self.get("/api/intel/country/IN")
        self.assertEqual(prof["capital"], "New Delhi")
        self.assertIsNotNone(prof["instability"]["index"])
        self.assertEqual(prof["conflict"]["events_7d"], 1)                  # the Mumbai reports are crime, hidden by default
        with self.assertRaises(HTTPError) as err:
            self.get("/api/intel/country/ZZ")
        self.assertEqual(err.exception.status, 404)
        with self.assertRaises(HTTPError) as err:
            self.get("/api/intel/country/123")
        self.assertEqual(err.exception.status, 400)
        results = self.app.run_search("india", 10)
        self.assertEqual(results[0]["kind"], "country")
        tool = self.app.call_ai_tool("country_profile", {"name": "India"})
        self.assertEqual(tool["iso2"], "IN")
        self.assertIn("caveat", tool)

    def test_providers_and_tools(self):
        fc = self.get("/api/intel/conflict?days=1")
        lon, lat = fc["features"][0]["geometry"]["coordinates"]
        near = self.app.run_nearby(lat, lon, 30, 10)
        self.assertTrue(any(i["kind"] == "conflict_event" and i["distance_km"] < 1 for i in near))
        crime_only = self.get("/api/intel/conflict?days=1&crime=1")
        self.assertGreater(len(crime_only["features"]), len(fc["features"]))
        since = time.time() - 86400
        events = self.app.collect_events(since)
        self.assertTrue(all(e["severity"] >= 4.0 for e in events if e["kind"] == "conflict_event"))
        out = self.app.call_ai_tool("conflict_events", {"days": 2, "min_severity": 0, "limit": 5})
        self.assertGreaterEqual(out["count"], 3)
        self.assertIn("caveat", out)
        bad = self.app.call_ai_tool("conflict_events", {"country": "Narnia"})
        self.assertIn("error", bad)
        news_out = self.app.call_ai_tool("news_search", {"theatre": "sudan", "hours": 24})
        self.assertEqual(news_out["theatre"], "sudan")
        self.assertIn("error", self.app.call_ai_tool("news_search", {"theatre": "atlantis"}))
        conv = self.app.call_ai_tool("convergences", {})
        self.assertEqual(conv["count"], 0)
        names = {t["name"] for t in self.app.ai_tools()}
        self.assertTrue({"country_profile", "conflict_events", "news_search", "convergences"} <= names)

    def test_place_display_rules(self):
        svc = self.svc
        self.assertEqual(svc.place_display("Gaza, Israel", "Palestine"), "Gaza, Palestine")
        self.assertEqual(svc.place_display("Jerusalem, Israel (general), Israel", "Israel"), "Jerusalem")
        self.assertEqual(svc.place_display("Kyiv, Kyyiv, Misto, Ukraine", "Ukraine"), "Kyiv, Ukraine")
        self.assertEqual(svc.place_display("Ukraine", "Ukraine"), "Ukraine")
        # Jerusalem keeps GDELT's attribution even if a polygon says otherwise
        self.assertEqual(svc.iso_of(31.776, 35.22, "IS", "IL"), "IL")

    def test_prune(self):
        removed = db.prune(self.store, time.time() + 30 * 86400)
        self.assertGreaterEqual(removed["events"], 4)
        self.assertEqual(db.count_events(self.store), 0)
        # put the fixture back for any later test in this class
        rows = list(gdelt.iter_rows((FIX / "gdelt_sample.export.CSV").read_text(encoding="utf-8")))
        now_ts = gdelt.epoch_to_ts(gdelt.slot_floor(time.time()))
        for r in rows:
            r[gdelt.C_ADDED] = now_ts
            r[gdelt.C_DAY] = now_ts[:8]
        self.svc.ingest_text("\n".join("\t".join(r) for r in rows) + "\n")


if __name__ == "__main__":
    unittest.main()
