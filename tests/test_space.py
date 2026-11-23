"""Space module tests: SGP4 against reference values, pass prediction, parsers, caches and routes.

Run: python -m unittest tests.test_space -v      (no network: fixtures in tests/fixtures/space/)

The SGP4 reference values were produced with the vendored satellite.js 7.2.0 (an independent port of
Vallado's code, including the full deep-space SDP4) for real CelesTrak element sets, plus the classic
Spacetrack Report #3 test object 88888.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.net import NetError, NetResponse
from meridian.registry import App, HTTPError, Request, Response
from meridian.scheduler import Scheduler
from meridian.store import Store
from meridian.modules.space import catalog as C
from meridian.modules.space import imagery as I
from meridian.modules.space import launches as L
from meridian.modules.space import sgp4 as S
from meridian.modules.space import weather as W

FIX = Path(__file__).parent / "fixtures" / "space"


def fixture(name: str):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


REF = fixture("sgp4_reference.json")


# ---------------------------------------------------------------------------------------------
# SGP4 and orbit geometry
# ---------------------------------------------------------------------------------------------
class SGP4Tests(unittest.TestCase):
    def test_spacetrack_report_88888(self):
        """The classic verification object, propagated to a day: identical to the reference."""
        e = REF["case_88888_elements"]
        s = S.sgp4init(88888, S.unix_from_jd(e["jdsatepoch"]), e["bstar"], e["ecco"], e["argpo"], e["inclo"], e["mo"],
                       e["no_kozai"], e["nodeo"])
        for case in REF["case_88888"]:
            r, v = S.sgp4(s, case["minutes"])
            self.assertLess(math.dist(r, case["r"]) * 1000, 0.01, f"position at {case['minutes']} min")
            self.assertLess(math.dist(v, case["v"]) * 1000, 0.001, f"velocity at {case['minutes']} min")
        # and the published Vallado value at epoch
        r0, _ = S.sgp4(s, 0.0)
        self.assertAlmostEqual(r0[0], 2328.96975262, places=6)
        self.assertAlmostEqual(r0[1], -5995.22051338, places=6)
        self.assertAlmostEqual(r0[2], 1719.97297192, places=6)

    def test_near_earth_matches_satellitejs(self):
        for obj in REF["objects"]:
            if obj["deep"]:
                continue
            s = S.satrec_from_omm(obj["omm"])
            for smp in obj["samples"]:
                r, v = S.propagate(s, smp["t"])
                self.assertLess(math.dist(r, smp["r"]), 0.005, f"{obj['omm']['OBJECT_NAME']} at {smp['minutes']} min")
                st = S.state_at(s, smp["t"])
                self.assertAlmostEqual(st.lat, smp["lat"], places=4)
                self.assertAlmostEqual(((st.lon - smp["lon"] + 540) % 360) - 180, 0.0, places=4)
                self.assertAlmostEqual(st.alt_km, smp["alt"], delta=0.01)

    def test_deep_space_is_an_honest_approximation(self):
        """No lunar-solar terms: within 80 km of full SDP4 over two days (documented)."""
        for obj in REF["objects"]:
            if not obj["deep"]:
                continue
            s = S.satrec_from_omm(obj["omm"])
            self.assertTrue(s.deep)
            for smp in obj["samples"]:
                r, _ = S.propagate(s, smp["t"])
                self.assertLess(math.dist(r, smp["r"]), 80.0, f"{obj['omm']['OBJECT_NAME']} at {smp['minutes']} min")

    def test_epoch_parsing(self):
        t = S.parse_epoch("2026-11-22T13:24:24.463872")
        self.assertAlmostEqual(t % 60, 24.463872, places=5)
        self.assertEqual(S.parse_epoch("2026-11-22T00:00:00Z"), S.parse_epoch("2026-11-22T00:00:00"))

    def test_sidereal_time_and_sun(self):
        # GMST at J2000.0 is 280.46061837 degrees
        self.assertAlmostEqual(S.gstime(2451545.0) * S.RAD, 280.46061837, places=5)
        # declination of the Sun near the June 2026 solstice (21 Jun 08:24 UTC) is about +23.44 degrees
        t = S.parse_epoch("2026-06-21T08:24:00")
        x, y, z = S.sun_eci(S.jd_from_unix(t))
        dec = math.degrees(math.asin(z / math.sqrt(x * x + y * y + z * z)))
        self.assertAlmostEqual(dec, 23.44, delta=0.05)
        # London at noon in June: Sun high; at midnight: well below the horizon
        london = S.Observer(51.5074, -0.1278)
        self.assertGreater(S.sun_elevation(london, S.parse_epoch("2026-06-21T12:00:00")), 55)
        self.assertLess(S.sun_elevation(london, S.parse_epoch("2026-06-21T00:00:00")), -10)

    def test_orbit_classes_and_footprint(self):
        classes = {o["omm"]["NORAD_CAT_ID"]: S.orbit_info(S.satrec_from_omm(o["omm"]))["orbit_class"] for o in REF["objects"]}
        self.assertEqual(classes[25544], "LEO")
        self.assertEqual(classes[26407], "MEO")
        self.assertEqual(classes[35491], "GEO")
        self.assertEqual(classes[14129], "HEO")
        self.assertAlmostEqual(S.footprint_radius_km(420.0), 2250, delta=20)
        self.assertLess(S.footprint_radius_km(420.0, 10.0), S.footprint_radius_km(420.0))
        self.assertEqual(S.compass(0), "N")
        self.assertEqual(S.compass(225), "SW")

    def test_bad_elements_raise(self):
        with self.assertRaises(S.PropagationError):
            S.sgp4init(1, 0.0, 0.0, 1.5, 0, 0, 0, 0.06, 0)   # eccentricity >= 1


class PassTests(unittest.TestCase):
    def setUp(self):
        self.p = REF["iss_passes_london"]
        self.s = S.satrec_from_omm(REF["objects"][0]["omm"])

    def test_passes_match_brute_force_reference(self):
        mine = S.find_passes(self.s, self.p["lat"], self.p["lon"], self.p["start"], self.p["hours"],
                             min_el=self.p["min_el"])
        self.assertEqual(len(mine), len(self.p["passes"]))
        for a, b in zip(mine, self.p["passes"]):
            self.assertLess(abs(a["rise"]["time"] - b["rise"]), 5)
            self.assertLess(abs(a["set"]["time"] - b["set"]), 5)
            self.assertLess(abs(a["max"]["el"] - b["max_el"]), 0.3)
            self.assertLess(abs(a["max"]["time"] - b["max_t"]), 10)

    def test_visibility_rules(self):
        allp = S.find_passes(self.s, self.p["lat"], self.p["lon"], self.p["start"], 48, min_el=10, std_mag=-1.8)
        vis = S.find_passes(self.s, self.p["lat"], self.p["lon"], self.p["start"], 48, min_el=10, visible_only=True,
                            std_mag=-1.8)
        self.assertTrue(0 < len(vis) < len(allp))
        london = S.Observer(self.p["lat"], self.p["lon"])
        for v in vis:
            self.assertTrue(v["visible"] and v["sunlit"] and v["observer_dark"])
            self.assertLessEqual(S.sun_elevation(london, v["visible_from"]), -6.0 + 0.05)
            self.assertLess(v["magnitude"], 2.0)
        # the first pass of the reference (15:11 UTC in November) happens in daylight
        first = allp[0]
        self.assertFalse(first["visible"])
        self.assertGreater(S.sun_elevation(london, first["max"]["time"]), -6.0)

    def test_short_pass_between_samples_is_found(self):
        """A pass that barely clears the threshold is not lost between two 30 s samples."""
        allp = S.find_passes(self.s, self.p["lat"], self.p["lon"], self.p["start"], 48, min_el=10)
        lowest = min(allp, key=lambda p: p["max"]["el"])
        p2 = S.find_passes(self.s, self.p["lat"], self.p["lon"], self.p["start"], 48, min_el=lowest["max"]["el"] - 0.3)
        self.assertGreaterEqual(len(p2), len(allp))

    def test_ground_track(self):
        pts = S.ground_track(self.s, self.p["start"], self.p["start"] + 5400, 60)
        self.assertEqual(len(pts), 91)
        self.assertTrue(all(abs(lat) <= 52.0 for _, lat, _ in pts))


# ---------------------------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------------------------
class CatalogParserTests(unittest.TestCase):
    def test_parse_gp_skips_broken_records(self):
        recs = C.parse_gp(json.dumps(fixture("gp_stations.json")).encode())
        self.assertEqual([r["id"] for r in recs], [25544, 48274, 49044])
        iss = recs[0]
        self.assertEqual(iss["name"], "ISS (ZARYA)")
        self.assertEqual(iss["cospar"], "1998-067A")
        self.assertEqual(len(C.row(iss)), len(C.ROW_FIELDS))

    def test_parse_gp_message_is_an_error(self):
        with self.assertRaises(C.GPError):
            C.parse_gp(b'Invalid query: "GROUP=noaa&FORMAT=json" (GROUP=noaa not found)')
        with self.assertRaises(C.GPError):
            C.parse_gp(b"[not json")
        self.assertIsNone(C.compact({"NORAD_CAT_ID": 5, "EPOCH": "2026-01-01T00:00:00", "MEAN_MOTION": "nan"}))

    def test_groups_are_valid_celestrak_names(self):
        self.assertNotIn("noaa", C.GROUP_IDS)          # no longer a CelesTrak group
        self.assertNotIn("glonass-ops", C.GROUP_IDS)   # renamed glo-ops
        self.assertIn("glo-ops", C.GROUP_IDS)
        self.assertTrue(C.GROUP_BY_ID["starlink"].get("heavy"))


class LaunchParserTests(unittest.TestCase):
    def setUp(self):
        self.up = L.parse_page(json.dumps(fixture("ll2_upcoming.json")).encode())
        self.prev = L.parse_page(json.dumps(fixture("ll2_previous.json")).encode())

    def test_records(self):
        self.assertEqual(len(self.up), 5)
        r = self.up[1]
        self.assertTrue(r["id"].startswith("ll2:"))
        self.assertIsInstance(r["pad"]["lat"], float)
        self.assertTrue(-90 <= r["pad"]["lat"] <= 90)
        self.assertTrue(r["rocket"] and r["provider"] and r["mission"])
        for rec in self.up + self.prev:
            for link in rec["videos"] + rec["info"]:
                self.assertTrue(link["url"].startswith("https://"))

    def test_phase_and_entity(self):
        fail = [r for r in self.prev if r["status"] == "Failure"][0]
        self.assertEqual(L.phase(fail, now=fail["net"] - 86400), "past")     # finished launches are past
        go = [r for r in self.up if r["status"] == "Go"][0]
        self.assertEqual(L.phase(go, now=go["net"] - 3600), "upcoming")
        self.assertEqual(L.phase(go, now=go["net"] + 7200), "past")
        e = L.entity(go)
        self.assertEqual(e["kind"], "launch")
        self.assertEqual(e["layer"], "space.launches")
        self.assertEqual((e["lat"], e["lon"]), (go["pad"]["lat"], go["pad"]["lon"]))

    def test_pads_and_bad_input(self):
        pads = L.parse_pads_pages([json.dumps(fixture("ll2_pads.json")).encode()])
        self.assertEqual(len(pads), 4)
        self.assertIsNone(L.parse_launch({"id": "../../etc", "pad": {}}))
        self.assertIsNone(L.parse_pad({"id": 1, "latitude": "95", "longitude": "0"}))


class WeatherParserTests(unittest.TestCase):
    def test_parse(self):
        w = W.parse(fixture("openmeteo_london.json"), 51.5, -0.12)
        self.assertEqual(len(w["hourly"]), 24)
        self.assertIn(w["current"]["text"], W.WMO.values())
        self.assertIsNotNone(w["summary"]["min_c"])
        self.assertIn("km/h", W.text_summary(w))
        self.assertEqual(W.describe(95), "Thunderstorm")
        self.assertEqual(W.describe("x"), "Unknown conditions")
        self.assertEqual(W.compass(90), "E")
        with self.assertRaises(ValueError):
            W.parse({"error": True, "reason": "Latitude must be in range"}, 0, 0)


class ImageryParserTests(unittest.TestCase):
    def test_describe_domains(self):
        txt = I.domain_text_from_xml(fixture("gibs_domain_ir.xml").encode())
        times = I.parse_domain(txt, daily=False)
        self.assertEqual(times[-1], "2026-11-22T21:00:00Z")
        self.assertNotIn("2026-11-22T19:30:00Z", times)            # the gap is respected
        self.assertIn("2026-11-22T19:50:00Z", times)
        days = I.parse_domain(I.domain_text_from_xml(fixture("gibs_domain_daily.xml").encode()), daily=True)
        self.assertNotIn("2026-11-09", days)
        self.assertEqual(days[-1], "2026-11-23")

    def test_default_time_prefers_complete_days_for_swaths(self):
        from datetime import datetime as real_datetime, timezone as tz
        p = dict(I.PRODUCTS["terra"])
        layers = [{"times": ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"]}]
        with mock.patch.object(I, "datetime") as dt:
            dt.now.return_value = real_datetime(2026, 11, 22, 21, 0, tzinfo=tz.utc)
            self.assertEqual(I.default_time(p, layers), "2026-11-21")
        self.assertEqual(I.default_time(I.PRODUCTS["sst"], [{"times": ["2026-11-20", "2026-11-21"]}]), "2026-11-21")
        self.assertEqual(len(I.fallback_times("PT10M", now=0)), 144)
        self.assertTrue(I.valid_time("2026-11-22T21:00:00Z") and I.valid_time("2026-11-22"))
        self.assertFalse(I.valid_time("2026-11-22/../x"))

    def test_rainviewer(self):
        out = I.parse_rainviewer(fixture("rainviewer.json"))
        self.assertEqual(len(out["frames"]), 13)
        self.assertEqual(out["max_zoom"], 7)
        self.assertEqual(out["colour"], 2)
        with self.assertRaises(ValueError):
            I.parse_rainviewer({"host": "http://evil.example", "radar": {"past": []}})
        bad = I.parse_rainviewer({"host": "https://tilecache.rainviewer.com",
                                  "radar": {"past": [{"time": 1, "path": "/v2/radar/../../x"}, {"time": 2, "path": "/v2/radar/ab12"}]}})
        self.assertEqual([f["path"] for f in bad["frames"]], ["/v2/radar/ab12"])

    def test_products_are_well_formed(self):
        for pid, p in I.PRODUCTS.items():
            self.assertIn(p["ext"], ("png", "jpg"))
            gibs = [l for l in I.all_layers(p) if not l.startswith("eumetsat:")]
            if not gibs:                       # EUMETView-only products (WMS, no tile matrix)
                self.assertIsNone(p["matrix"], pid)
                continue
            self.assertTrue(p["matrix"].startswith("GoogleMapsCompatible_Level"))
            self.assertEqual(int(p["matrix"].rsplit("Level", 1)[1]), p["maxzoom"], pid)
        self.assertEqual(len(I.WEATHER), 10)   # six originals plus NO2, SO2, sea ice and lightning (Space+)


# ---------------------------------------------------------------------------------------------
# A fake network and the module end to end
# ---------------------------------------------------------------------------------------------
class FakeNet:
    def __init__(self):
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.stats = {"requests": 0}
        self.lock = threading.Lock()

    def set_host_interval(self, host, seconds):
        pass

    def _body(self, url: str, params: dict | None) -> tuple[int, bytes]:
        params = params or {}
        with self.lock:
            self.calls.append(url + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""))
        for key in self.fail:
            if key in url or key in json.dumps(params):
                raise NetError(f"{key}: HTTP 503", status=503)
        if "celestrak.org" in url:
            group = params.get("GROUP")
            if group == "stations":
                return 200, json.dumps(fixture("gp_stations.json")).encode()
            if group == "gps-ops":
                return 200, json.dumps(fixture("gp_gps.json")).encode()
            if params.get("CATNR") == "26407":
                return 200, json.dumps(fixture("gp_gps.json")[:1]).encode()
            return 200, b"No GP data found"
        if "ll.thespacedevs.com" in url:
            if "/launch/upcoming/" in url:
                return 200, json.dumps(fixture("ll2_upcoming.json")).encode()
            if "/launch/previous/" in url:
                return 200, json.dumps(fixture("ll2_previous.json")).encode()
            if "/pad/" in url:
                return 200, json.dumps(fixture("ll2_pads.json")).encode()
            return 404, b"{}"
        if "open-meteo.com" in url:
            return 200, json.dumps(fixture("openmeteo_london.json")).encode()
        if "rainviewer.com" in url:
            return 200, json.dumps(fixture("rainviewer.json")).encode()
        if "gibs.earthdata.nasa.gov" in url:
            if "Clean_Infrared" in url:
                return 200, fixture("gibs_domain_ir.xml").encode()
            return 200, fixture("gibs_domain_daily.xml").encode()
        return 404, b""

    def request(self, url, *, params=None, accept=(200,), **kw):
        status, body = self._body(url, params)
        if status not in accept:
            raise NetError(f"HTTP {status}", status=status)
        return NetResponse(status, {}, body, url)

    def get_bytes(self, url, **kw):
        return self.request(url, params=kw.get("params")).body

    def get_json(self, url, **kw):
        return json.loads(self.get_bytes(url, **kw))

    def download(self, url, dest, **kw):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.get_bytes(url))
        return dest


class ModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MERIDIAN_DATA_DIR"] = cls.tmp.name
        cfg = Config(None)
        log = logging.getLogger("meridian.test.space")
        cls.net = FakeNet()
        app = App(cfg, Store(Path(cls.tmp.name) / "t.db"), cls.net, log, Scheduler(log), DatasetManager(log))
        from meridian.core_api import register_core
        register_core(app)
        from meridian.modules import load_all
        load_all(app, {"space"})
        cls.app = app
        assert app.modules["space"].ok, app.modules["space"].error
        app.datasets.build_all()

    @classmethod
    def tearDownClass(cls):
        cls.app.store.close()
        os.environ.pop("MERIDIAN_DATA_DIR", None)
        try:
            cls.tmp.cleanup()
        except OSError:
            pass

    def get(self, path, **query):
        route, params, _ = self.app.match("GET", path)
        self.assertIsNotNone(route, path)
        req = Request("GET", path, params, {k: [str(v)] for k, v in query.items()}, {})
        out = route.handler(req)
        if isinstance(out, Response):
            return json.loads(out.body)
        return json.loads(json.dumps(out))

    def status(self, path, **query):
        try:
            self.get(path, **query)
        except HTTPError as err:
            return err.status
        return 200

    def test_satellites_group_and_cache(self):
        out = self.get("/api/space/satellites", group="stations")
        self.assertEqual(out["fields"], C.ROW_FIELDS)
        self.assertEqual(len(out["sats"]), 3)
        self.assertEqual(out["info"]["count"], 3)
        before = len([c for c in self.net.calls if "GROUP=stations" in c])
        self.get("/api/space/satellites", group="stations")        # within two hours: no refetch
        self.assertEqual(len([c for c in self.net.calls if "GROUP=stations" in c]), before)
        self.assertTrue((Path(self.tmp.name) / "raw" / "space" / "celestrak" / "stations.json").exists())
        self.assertEqual(self.status("/api/space/satellites", group="nope"), 400)
        self.assertEqual(self.status("/api/space/satellites"), 400)
        self.assertEqual(self.status("/api/space/satellites", group="goes"), 502)  # CelesTrak sends a message

    def test_satellite_detail_and_lookup(self):
        self.get("/api/space/satellites", group="stations")
        d = self.get("/api/space/satellite/25544")
        self.assertEqual(d["orbit"]["orbit_class"], "LEO")
        self.assertIn("stations", d["groups"])
        self.assertIsNotNone(d["position"])
        g = self.get("/api/space/satellite/26407")                   # not in a loaded group: CATNR lookup
        self.assertEqual(g["orbit"]["orbit_class"], "MEO")
        self.assertEqual(self.status("/api/space/satellite/12x"), 400)
        self.assertEqual(self.status("/api/space/satellite/99999"), 404)
        # "²" and "١٢" pass str.isdigit() but not int(); 5,000 digits exceed int()'s limit: 400, not 500
        for raw in ("²", "١٢", "1" * 5000):
            self.assertEqual(self.status(f"/api/space/satellite/{raw}"), 400, raw[:8])

    def test_iss_and_passes(self):
        iss = self.get("/api/space/iss")
        self.assertTrue(-90 <= iss["lat"] <= 90 and 300 < iss["alt_km"] < 500)
        p = self.get("/api/space/passes", lat=51.5, lon=-0.12, hours=48)
        self.assertEqual(p["norad_id"], 25544)
        self.assertTrue(p["visible_only"])
        for x in p["passes"]:
            self.assertTrue(x["visible"])
            self.assertTrue(x["rise"]["iso"].endswith("Z"))
        self.assertEqual(self.status("/api/space/passes", lat=95, lon=0), 400)
        self.assertEqual(self.status("/api/space/passes", lat=10, lon=0, hours=500), 400)

    def test_launches_and_pads(self):
        out = self.get("/api/space/launches")
        self.assertGreaterEqual(len(out["launches"]), 6)
        first = out["launches"][0]
        self.assertIn(first["phase"], ("upcoming", "flying", "past"))
        one = self.get(f"/api/space/launch/{first['ll2_id']}")
        self.assertEqual(one["id"], first["id"])
        self.assertEqual(self.status("/api/space/launch/not-a-uuid"), 404)
        pad = self.get(f"/api/space/pad/{first['pad']['id']}")
        self.assertTrue(pad["launches"])
        self.assertEqual(self.status("/api/space/pad/abc"), 400)
        for raw in ("²", "ll2pad:²", "١٢", "9" * 5000):
            self.assertEqual(self.status(f"/api/space/pad/{raw}"), 400, raw[:8])
        search = next(fn for mod, fn in self.app._search if mod == "space")
        self.assertEqual(search("²²", 5), [])          # the provider used to raise ValueError on int("²²")

    def test_weather_radar_gibs(self):
        w = self.get("/api/space/weather", lat=51.5, lon=-0.12)
        self.assertEqual(len(w["hourly"]), 24)
        self.assertEqual(self.status("/api/space/weather", lat="x", lon=0), 400)
        r = self.get("/api/space/radar")
        self.assertEqual(len(r["frames"]), 13)
        g = self.get("/api/space/gibs", products="clouds,terra")
        self.assertEqual(set(g["products"]), {"clouds", "terra"})
        self.assertEqual(g["products"]["clouds"]["layers"][0]["latest"], "2026-11-22T21:00:00Z")
        self.assertFalse(g["products"]["terra"]["layers"][0]["estimated"])
        self.assertEqual(self.status("/api/space/gibs", products="bogus"), 400)

    def test_search_nearby_events(self):
        self.get("/api/space/satellites", group="stations")
        self.get("/api/space/launches")
        hits = self.app.run_search("iss", 10)
        sats = [h for h in hits if h["kind"] == "satellite"]
        self.assertEqual(sats[0]["id"], "25544")                     # the alias wins over the modules
        self.assertTrue(all(h["kind"] != "launch" or "iss" in h["title"].lower().split() for h in hits))
        self.assertEqual(self.app.run_search("25544", 5)[0]["id"], "25544")
        rec = L.parse_page(json.dumps(fixture("ll2_upcoming.json")).encode())[1]
        near = self.app.run_nearby(rec["pad"]["lat"], rec["pad"]["lon"], 20, 5)
        self.assertTrue(any(n["kind"] == "launch_pad" for n in near))
        with mock.patch("time.time", return_value=rec["net"] - 6 * 3600):
            ev = self.app.collect_events(rec["net"] - 30 * 3600)
        self.assertTrue(any(e["id"] == rec["id"] and 2 <= e["severity"] <= 3 for e in ev))

    def test_ai_tools(self):
        self.get("/api/space/satellites", group="stations")
        iss = self.app.call_ai_tool("iss_position", {})
        self.assertIn("lat", iss)
        p = self.app.call_ai_tool("satellite_passes", {"lat": 51.5, "lon": -0.12, "hours": 24})
        self.assertEqual(p["satellite"]["norad_id"], 25544)
        self.assertIn("accuracy", p)
        bad = self.app.call_ai_tool("satellite_passes", {"lat": 200, "lon": 0})
        self.assertIn("error", bad)
        up = self.app.call_ai_tool("upcoming_launches", {"hours": 720, "limit": 3})
        self.assertLessEqual(up["count"], 3)
        w = self.app.call_ai_tool("weather_at", {"lat": 51.5, "lon": -0.12})
        self.assertIn("summary", w)
        self.assertEqual(len(w["every_3_hours"]), 8)
        names = {t["name"] for t in self.app.ai_tools() if t["module"] == "space"}
        self.assertEqual(names, {"iss_position", "satellite_passes", "upcoming_launches", "weather_at", "space_weather_now",
                                 "latest_scenes"})

    def test_sources_and_layers_declared(self):
        sids = {s["id"] for s in self.app.sources.values() if s.get("module") == "space"}
        self.assertTrue({"space.celestrak", "space.ll2", "space.rainviewer", "space.gibs", "space.openmeteo"} <= sids)
        for s in self.app.sources.values():
            if s.get("module") == "space":
                self.assertTrue(s.get("licence"))
        lids = {l["id"] for l in self.app.layer_specs if l["module"] == "space"}
        self.assertTrue({"space.satellites", "space.iss", "space.launches", "space.radar", "space.daily"} <= lids)

    def test_upstream_failure_is_soft(self):
        self.net.fail.add("open-meteo")
        try:
            self.assertEqual(self.status("/api/space/weather", lat=1.234, lon=5.678), 502)
            self.assertIn("error", self.app.call_ai_tool("weather_at", {"lat": 1.234, "lon": 5.678}))
        finally:
            self.net.fail.discard("open-meteo")


class CatalogCacheTests(unittest.TestCase):
    def test_backoff_and_disk_reload(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            net = FakeNet()
            log = logging.getLogger("meridian.test.space.cat")
            cat = C.Catalog(net, Path(d), log)
            self.assertTrue(cat.refresh("stations"))
            self.assertFalse(cat.refresh("goes"))                   # message instead of data
            n = len(net.calls)
            self.assertFalse(cat.refresh("goes"))                   # backing off: no new request
            self.assertEqual(len(net.calls), n)
            fresh = C.Catalog(net, Path(d), log)
            self.assertEqual(fresh.load_disk(), 1)
            self.assertEqual(fresh.lookup(25544)["name"], "ISS (ZARYA)")
            self.assertEqual([r["id"] for _, r in fresh.search("tiangong")], [48274])
            self.assertEqual(fresh.search("is")[0][1]["id"] if fresh.search("is") else None, 25544)

    def test_launch_budget(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            lc = L.Launches(FakeNet(), Path(d), logging.getLogger("meridian.test.space.ll2"))
            spent = sum(1 for _ in range(15) if lc._spend())
            self.assertEqual(spent, L.HOURLY_BUDGET)
            self.assertEqual(lc.budget_left(), 0)


class ServerRoundTrip(unittest.TestCase):
    """One real HTTP round trip: gzip and ETag on the cached group payload."""

    def test_group_payload_gzip_and_etag(self):
        from meridian.httpserver import make_server
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            os.environ["MERIDIAN_DATA_DIR"] = d
            try:
                cfg = Config(None)
                log = logging.getLogger("meridian.test.space.http")
                app = App(cfg, Store(Path(d) / "t.db"), FakeNet(), log, Scheduler(log), DatasetManager(log))
                from meridian.core_api import register_core
                from meridian.modules import load_all
                register_core(app)
                load_all(app, {"space"})
                srv = make_server(app, "127.0.0.1", 0)
                port = app.bind[1]
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                try:
                    url = f"http://127.0.0.1:{port}/api/space/satellites?group=stations"
                    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        etag = resp.headers["ETag"]
                        body = resp.read()
                        if resp.headers.get("Content-Encoding") == "gzip":
                            body = gzip.decompress(body)
                    self.assertEqual(len(json.loads(body)["sats"]), 3)
                    req2 = urllib.request.Request(url, headers={"If-None-Match": etag})
                    try:
                        urllib.request.urlopen(req2, timeout=10)
                        self.fail("expected 304")
                    except urllib.error.HTTPError as err:
                        self.assertEqual(err.code, 304)
                finally:
                    srv.shutdown()
                    srv.server_close()
                    app.store.close()
            finally:
                os.environ.pop("MERIDIAN_DATA_DIR", None)


if __name__ == "__main__":
    unittest.main()
