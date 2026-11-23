"""Hazards module end to end, offline: a fake network serves the fixtures, the real module
registers on a real App (temp data dir), refreshers run, and every route, provider and AI tool is
called the way the core calls them.

Run: python -m unittest tests.test_hazards_service -v
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.net import NetError
from meridian.registry import App, HTTPError, Request, Response
from meridian.scheduler import Scheduler
from meridian.store import Store

FIX = Path(__file__).resolve().parent / "fixtures" / "hazards"
NOW = 1795381800.0          # 2026-11-22 21:10 UTC, inside every fixture's validity window

ROUTES = [
    ("earthquake.usgs.gov/earthquakes/feed/v1.0/detail/", "usgs_detail.json"),
    ("earthquake.usgs.gov/earthquakes/feed/v1.0/summary/", "usgs_feed.json"),
    ("earthquake.usgs.gov/fdsnws/event/1/query", "usgs_feed.json"),
    ("firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20", "firms_viirs.csv"),
    ("firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp", "firms_empty.csv"),
    ("firms.modaps.eosdis.nasa.gov/data/active_fire/modis", "firms_modis.csv"),
    ("eonet.gsfc.nasa.gov/api/v3/events?status=open&category=wildfires", "eonet.json"),
    ("eonet.gsfc.nasa.gov/api/v3/events", "eonet.json"),
    ("gdacsapi/api/events/geteventlist/MAP", "gdacs_tc_map.json"),
    ("gdacsapi/api/events/geteventlist/", "gdacs_events.json"),
    ("gdacsapi/api/polygons/getgeometry", "gdacs_tc_map.json"),
    ("gdacsapi/api/events/geteventdata", None),
    ("www.gdacs.org/xml/rss.xml", "gdacs_rss.xml"),
    ("www.nhc.noaa.gov/CurrentStorms.json", "nhc_current.json"),
    ("NHC_tropical_weather/MapServer/86/query", "nhc_cone.geojson"),
    ("NHC_tropical_weather/MapServer/84/query", "nhc_points.geojson"),
    ("NHC_tropical_weather/MapServer?f=json", "nhc_service.json"),
    ("hans-public/api/volcano/getElevatedVolcanoes", "hans_elevated.json"),
    ("hans-public/api/volcano/getCapElevated", "hans_cap.json"),
    ("webservices.volcano.si.edu", "gvp_wfs.json"),
    ("api.weather.gov/alerts/active", "nws_alerts.json"),
    ("api.weather.gov/zones/", "nws_zone.json"),
    ("feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom", "meteoalarm_uk.xml"),
    ("feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-spain", "meteoalarm_spain.xml"),
    ("feeds.meteoalarm.org/api/v1/warnings/", "meteoalarm_cap.xml"),
    ("repository/tree", [{"name": "MeteoAlarm_Geocodes_2026_11_13.json"}, {"name": "MeteoAlarm_Geocodes-Aliases_2026_11_13.csv"}]),
    ("MeteoAlarm_Geocodes_2026_11_13.json", "meteoalarm_geocodes.json"),
    ("MeteoAlarm_Geocodes-Aliases_2026_11_13.csv", "meteoalarm_aliases.csv"),
    ("tsunami.gov/events/xml/PAAQAtom.xml", "tsunami_PAAQAtom.xml"),
    ("tsunami.gov/events/xml/PHEBAtom.xml", "tsunami_PHEBAtom.xml"),
    ("www.who.int/api/news/diseaseoutbreaknews", "who_don.json"),
    ("ne_110m_admin_0_countries", None),
]


class FakeNet:
    """Answers from fixtures by URL substring; anything else is a 404 (nothing leaves the machine)."""

    def __init__(self):
        self.calls: list[str] = []

    def set_host_interval(self, host, seconds):
        pass

    def _full(self, url, params):
        if not params:
            return url
        return url + ("&" if "?" in url else "?") + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None},
                                                                            safe=",;")

    def _find(self, full):
        self.calls.append(full)
        for key, value in ROUTES:
            if key in full:
                if value is None:
                    raise NetError(f"no fixture for {full}", status=404)
                return value
        raise NetError(f"no fixture for {full}", status=404)

    def _text(self, value):
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return (FIX / value).read_text(encoding="utf-8")

    def get_json(self, url, params=None, **kw):
        return json.loads(self._text(self._find(self._full(url, params))))

    def get_text(self, url, params=None, encoding=None, **kw):
        return self._text(self._find(self._full(url, params)))

    def get_bytes(self, url, params=None, **kw):
        return self.get_text(url, params).encode("utf-8")

    def download(self, url, dest, **kw):
        value = self._find(url)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self._text(value), encoding="utf-8")
        dest.with_name(dest.name + ".meta.json").write_text(json.dumps({"fetched_at": time.time(), "etag": "x"}),
                                                            encoding="utf-8")
        return dest


def _country_fixture(tmp: Path) -> None:
    """A tiny stand-in for the Natural Earth outlines (two countries)."""
    feats = []
    for iso2, ring in (("GB", [[-6, 50], [2, 50], [2, 59], [-6, 59], [-6, 50]]),
                       ("CD", [[12, -13], [31, -13], [31, 5], [12, 5], [12, -13]])):
        feats.append({"type": "Feature", "properties": {"ISO_A2_EH": iso2, "NAME_EN": iso2},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}})
    (tmp / "ne.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats * 60}), encoding="utf-8")


class HazardsServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hazards-test-")
        os.environ["MERIDIAN_DATA_DIR"] = cls.tmp
        cls.clock = mock.patch("time.time", return_value=NOW)
        cls.clock.start()
        cfg = Config(None)
        log = logging.getLogger("meridian.test.hazards")
        cls.net = FakeNet()
        store = Store(Path(cls.tmp) / "t.db")
        cls.app = App(cfg, store, cls.net, log, Scheduler(log), DatasetManager(log))
        import importlib
        from meridian.modules import hazards
        importlib.import_module("meridian.modules.hazards.service").GVP_MIN = 2
        cls.m = cls.app.module("hazards")
        hazards.register(cls.m)
        cls.svc = hazards.get_service()
        # datasets: GVP from the fixture; countries from a tiny local file
        _country_fixture(Path(cls.tmp))
        ROUTES.insert(0, ("ne_110m_admin_0_countries", str(Path(cls.tmp) / "ne.geojson")))
        res = cls.app.datasets.build_all(only={"hazards"})
        cls.datasets = res
        ROUTES.pop(0)
        cls.svc.load_gvp()
        cls.svc.load_country_shapes()
        for layer in ("hazards.quakes", "hazards.fires", "hazards.warnings", "hazards.volcanoes", "hazards.storms",
                      "hazards.disasters", "hazards.natural", "hazards.tsunami", "hazards.outbreaks"):
            cls.svc.touch(layer)
        cls.svc.ma_countries = ["united-kingdom", "spain"]
        cls.results = {name: cls.svc.run(name) for name in ("quakes", "fires", "eonet", "gdacs", "nhc", "hans", "nws",
                                                            "zones", "ma_regions", "meteoalarm", "tsunami", "who")}

    @classmethod
    def tearDownClass(cls):
        cls.clock.stop()
        cls.app.store.close() if hasattr(cls.app.store, "close") else None
        os.environ.pop("MERIDIAN_DATA_DIR", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------------------------------
    def get(self, path, **query):
        route, params, _ = self.app.match("GET", path)
        self.assertIsNotNone(route, path)
        qa = {k: [str(v)] for k, v in query.items() if v is not None}
        result = route.handler(Request("GET", path, params, qa, {}))
        if isinstance(result, Response):
            return json.loads(result.body)
        return json.loads(json.dumps(result, default=str))

    # -- refresh ----------------------------------------------------------------------------------
    def test_every_source_refreshed(self):
        self.assertEqual({k for k, ok in self.results.items() if not ok}, set(), self.results)
        self.assertTrue(all(self.datasets.values()), self.datasets)
        status = self.app.sources
        self.assertTrue(status["hazards.usgs"]["ok"])
        self.assertTrue(status["hazards.who"]["count"] >= 1)
        self.assertNotIn("FIRMS_MAP_KEY", json.dumps(status))

    def test_layers_and_sources_declared(self):
        ids = {s["id"] for s in self.app.layer_specs if s["module"] == "hazards"}
        self.assertEqual(ids, {"hazards.quakes", "hazards.fires", "hazards.natural", "hazards.disasters", "hazards.storms",
                               "hazards.volcanoes", "hazards.warnings", "hazards.tsunami", "hazards.outbreaks",
                               "hazards.mapping", "hazards.floods", "hazards.flood_outlook"})
        for s in self.app.sources.values():
            if s["module"] == "hazards":
                self.assertTrue(s.get("licence"), s["id"])
        keys = {k["name"]: k for k in self.app.config.key_status()}
        self.assertTrue(keys["FIRMS_MAP_KEY"]["secret"])
        self.assertEqual({t["name"] for t in self.app.ai_tools() if t["module"] == "hazards"},
                         {"hazards_recent", "earthquakes", "weather_warnings_at", "flood_warnings", "emergency_mapping"})

    # -- layer endpoints ----------------------------------------------------------------------------
    def test_quakes(self):
        week = self.get("/api/hazards/quakes", window="week")
        self.assertEqual(week["type"], "FeatureCollection")
        self.assertTrue(week["features"])
        p = week["features"][0]["properties"]
        self.assertTrue({"id", "mag", "t", "d", "alert", "tsu", "place"} <= set(p))
        big = self.get("/api/hazards/quakes", window="week", min_mag=6.5)
        self.assertTrue(all(f["properties"]["mag"] >= 6.5 for f in big["features"]))
        with self.assertRaises(HTTPError):
            self.get("/api/hazards/quakes", window="year")

    def test_fires(self):
        grid = self.get("/api/hazards/fires", zoom=2, bbox="-180,-85,180,85")
        self.assertEqual(grid["meta"]["mode"], "grid")
        self.assertEqual(grid["meta"]["count"], 11)
        pts = self.get("/api/hazards/fires", zoom=9, bbox="20,-35,21,-34")
        self.assertEqual(pts["meta"]["mode"], "points")
        self.assertEqual(len(pts["features"]), 4)
        self.assertEqual(pts["meta"]["sources"]["snpp"]["rows"], 0)
        fid = pts["features"][0]["properties"]["id"]
        d = self.get("/api/hazards/details", kind="fire", id=fid)
        self.assertTrue(d["live"])
        self.assertEqual(d["data"]["nearby_count"], 3)
        gone = self.get("/api/hazards/details", kind="fire", id="firms:N20:1.00000:2.00000:202601010000")
        self.assertFalse(gone["live"])
        self.assertEqual(gone["entity"]["lat"], 1.0)

    def test_fires_fallback_without_firms(self):
        saved = self.svc.fire_data
        try:
            from meridian.modules.hazards import firms
            self.svc.fire_data = firms.FireData().finish()
            data = self.get("/api/hazards/fires", zoom=3)
            self.assertEqual(data["meta"]["mode"], "fallback")
            self.assertTrue(all(f["properties"]["kind"] == "event" for f in data["features"]))
        finally:
            self.svc.fire_data = saved

    def test_natural_disasters_storms(self):
        nat = self.get("/api/hazards/natural")
        roles = {f["properties"]["role"] for f in nat["features"]}
        self.assertEqual(roles, {"event", "track"})
        only = self.get("/api/hazards/natural", cats="volcanoes")
        self.assertTrue(all(f["properties"]["cat"] == "volcanoes" for f in only["features"]))
        dis = self.get("/api/hazards/disasters")
        self.assertEqual({f["properties"]["type"] for f in dis["features"]} >= {"EQ", "DR", "TC"}, True)
        orange = self.get("/api/hazards/disasters", min_alert=2)
        self.assertTrue(all(f["properties"]["rank"] >= 2 for f in orange["features"]))
        storms = self.get("/api/hazards/storms")
        names = {s["name"] for s in storms["meta"]["storms"]}
        self.assertEqual(names, {"Odette", "Xavier"})          # NHC storm plus the GDACS one
        roles = {f["properties"]["role"] for f in storms["features"]}
        self.assertTrue({"cone", "track", "pt", "now"} <= roles)
        geo = self.get("/api/hazards/disaster-geometry", id="gdacs:TC:1001350")
        self.assertTrue(geo["features"])
        with self.assertRaises(HTTPError):
            self.get("/api/hazards/disaster-geometry", id="nope")

    def test_volcanoes(self):
        v = self.get("/api/hazards/volcanoes")
        self.assertEqual(v["meta"]["base"], 2)
        kil = [f for f in v["features"] if f["properties"]["id"] == "gvp:332010"][0]
        self.assertGreaterEqual(kil["properties"]["a"], 2)
        active = self.get("/api/hazards/volcanoes", scope="active")
        self.assertTrue(all(f["properties"]["a"] >= 2 for f in active["features"]))
        d = self.get("/api/hazards/details", kind="volcano", id="gvp:332010")
        self.assertEqual(d["entity"]["title"], "Kilauea")
        self.assertTrue(d["data"]["activity"]["hans"])

    def test_warnings(self):
        w = self.get("/api/hazards/warnings", zoom=3, min=1)
        srcs = {f["properties"]["src"] for f in w["features"]}
        self.assertEqual(srcs, {"nws", "meteoalarm"})
        self.assertEqual(w["meta"]["regions"], 4)
        europe = self.get("/api/hazards/warnings", zoom=6, min=1, bbox="-10,35,5,55", src="meteoalarm")
        self.assertTrue(europe["features"])
        self.assertTrue(all(f["properties"]["src"] == "meteoalarm" for f in europe["features"]))
        uk = [f for f in europe["features"] if "England" in f["properties"]["area"]][0]
        d = self.get("/api/hazards/details", kind="warning", id=uk["properties"]["id"])
        self.assertEqual(d["data"]["issuer"], "UK Met Office")      # from the CAP message
        self.assertTrue(d["data"]["outline"])
        here = self.get("/api/hazards/here", lat=50.7, lon=-3.6)
        self.assertEqual(here["warnings"][0]["id"], uk["properties"]["id"])
        nothing = self.get("/api/hazards/here", lat=-60, lon=-120)
        self.assertEqual(nothing["warnings"], [])

    def test_tsunami_and_outbreaks(self):
        t = self.get("/api/hazards/tsunami", days=30)
        self.assertEqual(len(t["features"]), 2)
        o = self.get("/api/hazards/outbreaks")
        reports = [f for f in o["features"] if f["properties"]["role"] == "report"]
        self.assertTrue(reports)
        areas = [f for f in o["features"] if f["properties"]["role"] == "area"]
        self.assertTrue(areas)                                   # the stand-in outlines are used
        oid = reports[0]["properties"]["id"]
        d = self.get("/api/hazards/details", kind="outbreak", id=oid)
        self.assertTrue(d["data"]["summary"])
        self.assertIn("related", d["data"])

    def test_details_every_kind(self):
        for kind, eid in (("earthquake", "usgs:us6000ucij"), ("storm", "nhc:al142026"), ("storm", "gdacs:TC:1001350"),
                          ("tsunami", self.svc.tsunami_msgs[0]["id"]), ("flood", next(i for i in self.svc.gdacs_events if ":FL:" in i)),
                          ("disaster", next(i for i in self.svc.gdacs_events if ":DR:" in i)),
                          ("storm", next(e["id"] for e in self.svc.eonet_events if e["category"] == "severeStorms"))):
            d = self.get("/api/hazards/details", kind=kind, id=eid)
            self.assertTrue(d["live"], eid)
            self.assertTrue(d["entity"]["title"], eid)
            self.assertIsNotNone(d["entity"]["lat"], eid)
        q = self.get("/api/hazards/details", kind="earthquake", id="usgs:us6000ucij")
        self.assertEqual(q["data"]["pager"]["alert"], "green")
        missing = self.get("/api/hazards/details", kind="warning", id="nws:gone")
        self.assertFalse(missing["live"])
        self.assertTrue(missing.get("missing"))
        # anything opened once is remembered for favourites and case files
        arch = self.svc.archived("earthquake", "usgs:us6000ucij")
        self.assertEqual(arch["kind"], "earthquake")
        with self.assertRaises(HTTPError):
            self.get("/api/hazards/details", kind="ship", id="x:1")
        with self.assertRaises(HTTPError):
            self.get("/api/hazards/details", kind="fire", id="nocolon")

    def test_feed_and_request_urls_stay_on_the_provider(self):
        # Feed links (GDACS geometry/details, MeteoAlarm CAP) are third-party data fetched by the server:
        # a poisoned one must never be requested, and ids from the request must not shape upstream URLs.
        from meridian.modules.hazards import gdacs
        eid = next(i for i in self.svc.gdacs_events if ":FL:" in i)
        ev = self.svc.gdacs_events[eid]
        warn = next(w for items in self.svc.ma_warnings.values() for w in items)
        saved_ev, saved_w = dict(ev), dict(warn)
        seen: list[tuple[str, bool]] = []
        real_json, real_text = self.net.get_json, self.net.get_text

        def spy_json(url, params=None, **kw):
            seen.append((self.net._full(url, params), kw.get("allow_redirects", True)))
            return real_json(url, params, **kw)

        def spy_text(url, params=None, **kw):
            seen.append((self.net._full(url, params), kw.get("allow_redirects", True)))
            return real_text(url, params, **kw)

        try:
            ev["geometry_url"] = "http://127.0.0.1:8790/api/core/favourites"
            ev["details_url"] = "https://intranet.example/admin?x=1"
            warn["cap_url"] = "https://10.0.0.1/cap.xml"
            with mock.patch.object(self.net, "get_json", side_effect=spy_json), \
                    mock.patch.object(self.net, "get_text", side_effect=spy_text):
                self.get("/api/hazards/disaster-geometry", id=eid)
                self.get("/api/hazards/details", kind="flood", id=eid)
                self.get("/api/hazards/details", kind="warning", id=warn["id"])
                self.get("/api/hazards/details", kind="earthquake", id="usgs:a b")        # was a 500 (InvalidURL)
                self.get("/api/hazards/details", kind="earthquake", id="usgs:../../x?y=1")
                self.get("/api/hazards/details", kind="storm", id="eonet:../x")
        finally:
            ev.clear()
            ev.update(saved_ev)
            warn.clear()
            warn.update(saved_w)
        urls = [u for u, _ in seen]
        self.assertFalse([u for u in urls if "127.0.0.1" in u or "intranet" in u or "10.0.0.1" in u], urls)
        self.assertFalse([u for u in urls if " " in u or ".." in u], urls)
        self.assertTrue(any(gdacs.GEOMETRY_URL in u for u in urls), urls)   # fell back to the API on gdacs.org
        self.assertFalse([u for u, follow in seen if "gdacs.org/gdacsapi/api/polygons" in u and follow])

    # -- providers --------------------------------------------------------------------------------
    def test_search_nearby_events(self):
        hits = self.app.run_search("kilauea", 10)
        self.assertEqual(hits[0]["kind"], "volcano")
        self.assertEqual(hits[0]["id"], "gvp:332010")
        self.assertTrue(any(h["kind"] == "storm" for h in self.app.run_search("odette", 10)))
        self.assertEqual(self.app.run_search("zz", 10), [])
        near = self.app.run_nearby(19.42, -155.29, 50, 10)
        self.assertTrue(any(n["kind"] == "volcano" for n in near))
        self.assertTrue(all("distance_km" in n for n in near))
        events = self.app.collect_events(NOW - 7 * 86400)
        self.assertTrue(events)
        kinds = {e["kind"] for e in events}
        self.assertTrue({"earthquake", "storm", "volcano", "outbreak"} <= kinds, kinds)
        for e in events:
            self.assertTrue(0 <= e["severity"] <= 10)
            self.assertIsInstance(e["time"], int)
            self.assertTrue(e["layer"].startswith("hazards."))

    def test_ai_tools(self):
        recent = self.app.call_ai_tool("hazards_recent", {"hours": 168, "kinds": ["storm"]})
        self.assertTrue(recent["events"])
        self.assertTrue(all(e["kind"] == "storm" for e in recent["events"]))
        boxed = self.app.call_ai_tool("hazards_recent", {"hours": 168, "bbox": "-80,20,-60,30"})
        self.assertTrue(all(-80 <= e["lon"] <= -60 for e in boxed["events"]))
        with self.assertRaises(ValueError):
            self.app.call_ai_tool("hazards_recent", {"bbox": "bad"})
        q = self.app.call_ai_tool("earthquakes", {"min_magnitude": 5, "hours": 48})
        self.assertTrue(all(x["magnitude"] >= 5 for x in q["earthquakes"]))
        self.assertTrue(q["earthquakes"][0]["time_utc"].endswith("Z"))
        region = self.app.call_ai_tool("earthquakes", {"region": "Timor", "min_magnitude": 0, "hours": 72})
        self.assertTrue(region["earthquakes"])
        long = self.app.call_ai_tool("earthquakes", {"hours": 24 * 30})
        self.assertEqual(long["source"], "USGS FDSN event service")
        w = self.app.call_ai_tool("weather_warnings_at", {"lat": 50.7, "lon": -3.6})
        self.assertEqual(w["warnings"][0]["source"], "MeteoAlarm")
        self.assertEqual(w["country"], "United Kingdom")
        with self.assertRaises(ValueError):
            self.app.call_ai_tool("weather_warnings_at", {"lat": 200, "lon": 0})

    def test_summary_and_recent_route(self):
        s = self.get("/api/hazards/summary")
        self.assertGreater(s["counts"]["quakes"], 0)
        self.assertIn("quakes", s["sources"])
        r = self.get("/api/hazards/recent", hours=168, kinds="earthquake")
        self.assertTrue(all(e["kind"] == "earthquake" for e in r["events"]))
        with self.assertRaises(HTTPError):
            self.get("/api/hazards/recent", bbox="1,2,3")

    def test_failing_source_is_contained(self):
        with mock.patch.object(self.net, "get_json", side_effect=NetError("eonet.gsfc.nasa.gov: HTTP 503", status=503)):
            self.assertFalse(self.svc.run("eonet"))
        st = self.svc.state["eonet"]
        self.assertTrue(st.error)
        self.assertTrue(self.svc.eonet_events)                  # the last good snapshot stays in service
        self.assertFalse(self.app.sources["hazards.eonet"]["ok"])
        self.assertTrue(self.svc.run("eonet"))
        self.assertTrue(self.app.sources["hazards.eonet"]["ok"])


if __name__ == "__main__":
    unittest.main()
