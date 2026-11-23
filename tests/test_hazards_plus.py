"""Hazards+ (phase 3a): Copernicus EMS rapid mapping, England flood warnings and gauges, and the GloFAS
flood outlook. Parsers first, then the real module on a real App with a fake network that answers
from fixtures (tests/fixtures/hazards/cems_*, ea_*, glofas_caps.xml). No network.

Run: python -m unittest tests.test_hazards_plus -v
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.net import NetError
from meridian.registry import App, HTTPError, Request, Response
from meridian.scheduler import Scheduler
from meridian.store import Store
from meridian.modules.hazards import cems, ea, glofas

FIX = Path(__file__).resolve().parent / "fixtures" / "hazards"
NOW = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc).timestamp()
TILE = glofas.blank_png(4)


def fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# =============================================================================================== parsers
class CemsParserTests(unittest.TestCase):
    def test_wkt(self):
        self.assertEqual(cems.parse_wkt("POINT (-7.5447 40.4072)"), {"type": "Point", "coordinates": [-7.5447, 40.4072]})
        poly = cems.parse_wkt("POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0), (0.2 0.2, 0.4 0.2, 0.4 0.4, 0.2 0.2))")
        self.assertEqual(poly["type"], "Polygon")
        self.assertEqual(len(poly["coordinates"]), 2)
        multi = cems.parse_wkt("MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)), ((5 5, 6 5, 6 6, 5 5)))")
        self.assertEqual(multi["type"], "MultiPolygon")
        self.assertEqual(cems.parse_wkt("MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)))")["type"], "Polygon")
        self.assertEqual(cems.parse_wkt("POINT Z (1 2 3)")["coordinates"], [1.0, 2.0])
        for bad in (None, "", "POINT", "POINT (200 0)", "POLYGON ((0 0, 1 1))", "CIRCLE (1 2)", "POINT (1 2", 42,
                    "POLYGON ((a b, c d, e f, a b))"):
            self.assertIsNone(cems.parse_wkt(bad), bad)

    def test_list(self):
        items = cems.parse_list(fixture("cems_list.json"))
        self.assertEqual([a["code"] for a in items[:2]], ["EMSR937", "EMSR936"])       # newest activation first
        a = items[1]
        self.assertEqual(a["id"], "cems:EMSR936")
        self.assertEqual(a["cat"], "flood")
        self.assertFalse(a["closed"])
        self.assertEqual(a["gdacs_id"], "FL1104167")
        self.assertAlmostEqual(a["lat"], -2.11982, places=4)
        self.assertEqual(a["page"], "https://mapping.emergency.copernicus.eu/activations/EMSR936")
        self.assertEqual(items[0]["cat"], "landslide")                                 # "Mass movement"
        self.assertEqual(cems.parse_list({"results": [{"code": "nope"}, {"code": "EMSR1", "name": "x"}]})[0]["code"], "EMSR1")
        self.assertEqual(cems.parse_list(None), [])

    def test_detail(self):
        d = cems.parse_detail(fixture("cems_EMSR936.json"))
        self.assertEqual(d["code"], "EMSR936")
        self.assertEqual(d["subcategory"], "Riverine flood")
        self.assertEqual(d["charter_number"], "1059")
        self.assertEqual(d["requester"], "EC Services, DG ECHO")
        self.assertEqual(len(d["aois"]), 1)
        aoi = d["aois"][0]
        self.assertEqual(aoi["name"], "Milagro")
        self.assertEqual(aoi["geometry"]["type"], "Polygon")
        self.assertEqual([p["type"] for p in aoi["products"]], ["DEL", "DEL", "FEP"])      # delineation first
        mon = [p for p in aoi["products"] if p["monitoring"]][0]
        self.assertEqual(mon["label"], "Delineation, monitoring 1")
        self.assertTrue(all(p["delivered"] and p["status"] == "Delivered" for p in aoi["products"]))
        self.assertTrue(all(p["download"].startswith("https://rapidmapping.emergency.copernicus.eu/") for p in aoi["products"]))
        self.assertTrue(mon["observed"].startswith("https://rapidmapping-viewer.s3.eu-west-1.amazonaws.com/EMSR936/"))
        self.assertTrue(any(s["label"] == "Largest mapped extent" and s["unit"] == "ha" for s in d["stats"]))
        self.assertEqual(d["maps"], 6)
        layers = cems.observed_layers(d)
        self.assertEqual(len(layers), 1)
        self.assertIn("DEL_MONIT01", layers[0]["url"])                                 # the newest delivered product
        pending = cems.parse_detail(fixture("cems_EMSR937.json"))
        p = pending["aois"][0]["products"][0]
        self.assertEqual((p["type"], p["status"], p["delivered"], p["download"]), ("GRA", "In production", False, None))
        self.assertIsNotNone(p["expected"])
        self.assertEqual(pending["pending"], 1)
        self.assertIsNone(cems.parse_detail({"results": []}))

    def test_links_and_ids(self):
        self.assertEqual(cems.gdacs_event_id("FL1104167"), "gdacs:FL:1104167")
        self.assertEqual(cems.gdacs_event_id("wf1029794"), "gdacs:WF:1029794")
        for bad in (None, "", "XX123", "FL", "FL12a"):
            self.assertIsNone(cems.gdacs_event_id(bad))
        self.assertIsNone(cems.https_link("http://example.org/x"))
        self.assertIsNone(cems.https_link("javascript:alert(1)"))
        self.assertIsNone(cems.https_link("https://user:pw@example.org/"))
        self.assertIsNone(cems.download_link("https://evil.example/EMSR936.zip"))
        ok = "https://rapidmapping-viewer.s3.eu-west-1.amazonaws.com/EMSR936/AOI01/x_observedEventA_v1.json"
        self.assertEqual(cems.viewer_json(ok, "EMSR936"), ok)
        for bad in (ok.replace("EMSR936/", "EMSR935/"), ok + "?x=1", ok.replace(".json", ".xml"),
                    "https://evil.example/EMSR936/a.json", ok.replace("/AOI01/", "/../AOI01/")):
            self.assertIsNone(cems.viewer_json(bad, "EMSR936"), bad)
        self.assertEqual(cems.valid_code("emsr936"), "EMSR936")
        self.assertIsNone(cems.valid_code("EMSR936/../x"))


class EaParserTests(unittest.TestCase):
    def test_uk_time(self):
        winter = datetime(2026, 11, 23, 11, 4, tzinfo=timezone.utc).timestamp()
        self.assertEqual(ea.uk_time("2026-11-23T11:04:00"), winter)                   # GMT: no shift
        summer = datetime(2026, 7, 1, 11, 4, tzinfo=timezone.utc).timestamp()
        self.assertEqual(ea.uk_time("2026-07-01T12:04:00"), summer)                   # BST: an hour ahead of UTC
        self.assertEqual(ea.uk_time("2026-07-01T12:04:00Z"), summer + 3600)            # readings say Z
        self.assertEqual(ea.uk_time("2026-03-29T00:30:00"), datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ea.uk_time("2026-03-29T03:00:00"), datetime(2026, 3, 29, 2, 0, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(ea.uk_time(""))
        self.assertIsNone(ea.uk_time(None))

    def test_floods(self):
        items = ea.parse_floods(fixture("ea_floods.json"))
        self.assertEqual([w["level"] for w in items], [1, 2, 3, 3, 4])               # most severe first; bad rows dropped
        sev = items[0]
        self.assertEqual(sev["id"], "ea:113FWCSEVERE")
        self.assertEqual(sev["severity"], "Severe flood warning")
        self.assertEqual(sev["meaning"], "Severe flooding, danger to life.")
        self.assertTrue(sev["tidal"])
        self.assertEqual(items[1]["meaning"], "Flooding is expected, immediate action required.")
        self.assertEqual(items[2]["meaning"], "Flooding is possible, be prepared.")
        self.assertEqual(items[2]["message"], "Flooding is possible for: Penzance, Hayle and St Ives.\nRiver levels remain high.")
        self.assertEqual(ea.parse_floods({"items": "nope"}), [])

    def test_polygon_and_stations(self):
        p = ea.parse_polygon(fixture("ea_polygon_114WAFT1W01A00.json"))
        self.assertEqual(p["geometry"]["type"], "MultiPolygon")
        self.assertEqual((p["name"], p["quick_dial"]), ("West Cornwall Rivers", "211008"))
        self.assertIsNone(ea.parse_polygon({"features": []}))
        st = {s["ref"]: s for s in ea.parse_stations(fixture("ea_stations.json"))}
        self.assertEqual(set(st), {"48109", "47133", "E70024", "FAR1"})               # groundwater and closed left out
        self.assertEqual(st["48109"]["label"], "Angarrack")                            # shouted names tidied
        self.assertEqual(st["47133"]["qualifier"], "Stage")                             # stage wins over downstream
        self.assertEqual(st["47133"]["measure"], "47133-level-stage-i-15_min-mASD")
        self.assertEqual(st["47133"]["typical_high"], 1.4)
        self.assertEqual(st["E70024"]["kind"], "sea")
        self.assertEqual(st["E70024"]["rloi"], "8061")
        self.assertEqual(st["E70024"]["lat"], 50.62)
        self.assertEqual(ea.tidy_name("BRIDGE OF WEIR STW"), "Bridge of Weir STW")

    def test_readings_and_states(self):
        r = ea.parse_readings(fixture("ea_readings.json"))
        self.assertEqual(r["47133-level-stage-i-15_min-mASD"][0], 1.731)
        self.assertEqual(r["E70024-level-tidal_level-i-15_min-mAOD"][0], 2.1)       # a doubled reading (list)
        self.assertNotIn("FAR1-level-stage-i-15_min-m", r)                           # not a number
        st = {"typical_high": 1.4, "typical_low": 0.08, "recent_high": {"value": 2.25}}
        self.assertEqual(ea.gauge_state(st, 1.731), "high")
        self.assertEqual(ea.gauge_state(st, 2.3), "highest")
        self.assertEqual(ea.gauge_state(st, 0.5), "normal")
        self.assertEqual(ea.gauge_state(st, 0.01), "low")
        self.assertEqual(ea.gauge_state({}, 0.5), "unknown")
        self.assertEqual(ea.gauge_state(st, None), "unknown")
        self.assertEqual(ea.trend((1.0, 100.0), (1.2, 1000.0)), "rising")
        self.assertEqual(ea.trend((1.0, 100.0), (0.9, 1000.0)), "falling")
        self.assertEqual(ea.trend((1.0, 100.0), (1.005, 1000.0)), "steady")
        self.assertIsNone(ea.trend((1.0, 100.0), (1.5, 100.0 + 7 * 3600)))           # too far apart
        self.assertIsNone(ea.trend(None, (1.0, 1.0)))
        pts = ea.parse_history(fixture("ea_history.json"))
        self.assertEqual(len(pts), 40)
        self.assertEqual(pts, sorted(pts, key=lambda p: p["t"]))


class GlofasTests(unittest.TestCase):
    def test_tiles_and_capabilities(self):
        w, s, e, n = glofas.tile_bbox_3857(0, 0, 0)
        self.assertAlmostEqual(w, -20037508.34, places=1)
        self.assertAlmostEqual(n, 20037508.34, places=1)
        self.assertEqual(glofas.tile_bbox_3857(1, 1, 0)[0], 0.0)
        self.assertTrue(glofas.valid_tile(3, 7, 7))
        for z, x, y in ((3, 8, 0), (-1, 0, 0), (11, 0, 0), (2, 0, -1)):
            self.assertFalse(glofas.valid_tile(z, x, y))
        p = glofas.getmap_params("sumAL41EGE", 4, 4, 8, "2026-11-23T00:00Z")
        self.assertEqual((p["crs"], p["layers"], p["time"]), ("EPSG:3857", "sumAL41EGE", "2026-11-23T00:00Z"))
        times = glofas.parse_times((FIX / "glofas_caps.xml").read_text(encoding="utf-8"))
        self.assertEqual(set(times), {"sumAL41EGE", "sumAL42EGE", "sumAL43EGE", "reportingPoints"})
        self.assertEqual(times["sumAL41EGE"], "2026-11-23T00:00Z")
        self.assertEqual(glofas.time_for_date("2026-11-20", NOW), "2026-11-20T00:00Z")
        for bad in ("1999-01-01", "2027-01-01", "2026-13-01", "x", None):
            self.assertIsNone(glofas.time_for_date(bad, NOW), bad)
        self.assertTrue(glofas.is_png(glofas.BLANK))
        self.assertFalse(glofas.is_png(b"<ServiceExceptionReport/>"))


# =============================================================================================== service
ROUTES = [
    ("rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations-info/", "cems_list.json"),
    ("public-activations/?code=EMSR936", "cems_EMSR936.json"),
    ("public-activations/?code=EMSR937", "cems_EMSR937.json"),
    ("public-activations/?code=", None),
    ("rapidmapping-viewer.s3.eu-west-1.amazonaws.com/EMSR936/", "cems_observed.json"),
    ("flood-monitoring/id/floods", "ea_floods.json"),
    ("floodAreas/114WAFT1W01A00/polygon", "ea_polygon_114WAFT1W01A00.json"),
    ("floodAreas/113FWFEXE01/polygon", "ea_polygon_113FWFEXE01.json"),
    ("floodAreas/113FWCSEVERE/polygon", "ea_polygon_113FWCSEVERE.json"),
    ("floodAreas/", None),
    ("flood-monitoring/id/stations?", "ea_stations.json"),
    ("flood-monitoring/data/readings?latest", "ea_readings.json"),
    ("flood-monitoring/id/measures/", "ea_history.json"),
    ("ows.py?service=WMS&request=GetCapabilities", "glofas_caps.xml"),
    ("layers=sumAL43EGE", b"<ServiceExceptionReport>no</ServiceExceptionReport>"),
    ("ows.py?service=WMS&request=GetMap", TILE),
]


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class FakeNet:
    """Answers from fixtures by URL substring; anything else is a 404 (nothing leaves the machine)."""

    def __init__(self):
        self.calls: list[str] = []
        self.not_modified: set[str] = set()      # URL substrings that answer 304 to a conditional GET
        self.overrides: dict[str, object] = {}

    def set_host_interval(self, host, seconds):
        pass

    @staticmethod
    def full(url, params):
        if not params:
            return url
        return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}, safe=",;:")

    def _find(self, full):
        self.calls.append(full)
        for key, value in list(self.overrides.items()) + ROUTES:
            if key in full:
                if value is None:
                    raise NetError(f"no fixture for {full}", status=404)
                return value
        raise NetError(f"no fixture for {full}", status=404)

    @staticmethod
    def _bytes(value) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value).encode("utf-8")
        return (FIX / value).read_bytes()

    def request(self, url, params=None, headers=None, accept=(200,), **kw):
        full = self.full(url, params)
        value = self._find(full)
        if headers and headers.get("If-Modified-Since") and any(k in full for k in self.not_modified) and 304 in accept:
            return FakeResponse(304, b"", {"last-modified": "Mon, 23 Nov 2026 15:33:12 GMT"})
        return FakeResponse(200, self._bytes(value), {"last-modified": "Mon, 23 Nov 2026 15:33:12 GMT"})

    def get_json(self, url, params=None, **kw):
        return json.loads(self._bytes(self._find(self.full(url, params))).decode("utf-8"))

    def get_text(self, url, params=None, encoding=None, **kw):
        return self._bytes(self._find(self.full(url, params))).decode("utf-8")

    def get_bytes(self, url, params=None, **kw):
        return self._bytes(self._find(self.full(url, params)))


class HazardsPlusServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hazplus-test-")
        os.environ["MERIDIAN_DATA_DIR"] = cls.tmp
        cls.clock = mock.patch("time.time", return_value=NOW)
        cls.clock.start()
        log = logging.getLogger("meridian.test.hazardsplus")
        cls.net = FakeNet()
        cls.app = App(Config(None), Store(Path(cls.tmp) / "t.db"), cls.net, log, Scheduler(log), DatasetManager(log))
        from meridian.modules import hazards
        from meridian.modules.hazards import floods
        floods.MIN_STATIONS = 2                     # the fixture has a handful of stations, not 3,400
        cls.m = cls.app.module("hazards")
        hazards.register(cls.m)
        cls.svc = hazards.get_service()
        cls.plus = cls.svc.extras
        for layer in ("hazards.mapping", "hazards.floods", "hazards.flood_outlook"):
            cls.svc.touch(layer)
        cls.results = {name: cls.svc.run(name) for name in ("cems", "ea_floods", "ea_stations", "ea_gauges", "glofas")}

    @classmethod
    def tearDownClass(cls):
        cls.clock.stop()
        os.environ.pop("MERIDIAN_DATA_DIR", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, raw=False, **query):
        route, params, _ = self.app.match("GET", path)
        self.assertIsNotNone(route, path)
        qa = {k: [str(v)] for k, v in query.items() if v is not None}
        result = route.handler(Request("GET", path, params, qa, {}))
        if raw:
            return result
        if isinstance(result, Response):
            return json.loads(result.body)
        return json.loads(json.dumps(result, default=str))

    # -- registration ---------------------------------------------------------------------------
    def test_sources_layers_and_refresh(self):
        self.assertEqual({k for k, ok in self.results.items() if not ok}, set(), self.results)
        for sid in ("hazards.cems", "hazards.ea_floods", "hazards.ea_gauges", "hazards.ea_stations", "hazards.glofas"):
            src = self.app.sources[sid]
            self.assertTrue(src["licence"], sid)
            self.assertTrue(src["ok"], sid)
        self.assertIn("Open Government Licence", self.app.sources["hazards.ea_floods"]["licence"])
        ids = {s["id"] for s in self.app.layer_specs if s["module"] == "hazards"}
        self.assertTrue({"hazards.mapping", "hazards.floods", "hazards.flood_outlook"} <= ids)
        tools = {t["name"] for t in self.app.ai_tools()}
        self.assertTrue({"flood_warnings", "emergency_mapping"} <= tools)
        self.assertIn("2 detail requests failed", self.app.sources["hazards.cems"]["note"])   # EMSR935/934 404 here

    def test_polling_is_polite(self):
        st = self.svc.state
        self.assertEqual(st["ea_floods"].active_every, 900)          # the API's 15 minutes
        self.assertEqual(st["ea_gauges"].active_every, 900)
        self.assertIsNone(st["ea_gauges"].interval(False))          # levels only while the layer is used
        self.assertIsNone(st["ea_stations"].interval(False))
        self.assertIsNone(st["glofas"].interval(False))
        self.assertEqual(st["ea_floods"].interval(False), 3600)     # slow cadence for events
        self.assertEqual(st["cems"].interval(False), 7200)
        floods = self.plus.floods
        saved = floods.areas
        try:
            floods.areas = {}
            self.assertEqual(set(floods.wanted_outlines(2)), {"113FWFEXE01", "113FWCSEVERE"})   # idle: warnings only
            self.assertEqual(set(floods.wanted_outlines(3)), {"114WAFT1W01A00", "113FWFEXE01", "113FWCSEVERE"})
            self.assertIn("112WAFTUBA", floods._failed)          # its 404 is not retried for an hour
        finally:
            floods.areas = saved

    # -- emergency mapping ------------------------------------------------------------------------
    def test_mapping_layer(self):
        d = self.get("/api/hazards/mapping", days=60)
        pts = [f["properties"] for f in d["features"] if f["properties"]["role"] == "point"]
        aois = [f["properties"] for f in d["features"] if f["properties"]["role"] == "aoi"]
        self.assertEqual(d["meta"]["open"], 2)
        self.assertEqual({p["code"] for p in pts}, {"EMSR937", "EMSR936", "EMSR935", "EMSR934", "EMSR933"})
        self.assertEqual({a["code"] for a in aois}, {"EMSR937", "EMSR936"})         # details known for these
        only = self.get("/api/hazards/mapping", scope="open")
        self.assertTrue(all(f["properties"]["open"] == 1 for f in only["features"]))
        with self.assertRaises(HTTPError):
            self.get("/api/hazards/mapping", scope="all")

    def test_mapping_detail(self):
        d = self.get("/api/hazards/details", kind="emergency_mapping", id="cems:EMSR936")
        self.assertEqual(d["type"], "cems")
        self.assertTrue(d["live"])
        x = d["data"]
        self.assertTrue(x["open"])
        self.assertEqual(x["gdacs"][0]["match"], "linked")
        self.assertEqual(x["gdacs"][0]["entity"]["id"], "gdacs:FL:1104167")
        self.assertIn("© 2026 European Union", x["credit"])
        self.assertTrue(x["has_extent"])
        self.assertEqual(len(x["bbox"]), 4)
        self.assertEqual(x["severity"], 6)                                          # Charter activation
        self.assertEqual(d["entity"]["kind"], "emergency_mapping")
        p = self.get("/api/hazards/details", kind="emergency_mapping", id="cems:EMSR937")["data"]
        self.assertEqual(p["aois"][0]["products"][0]["status"], "In production")
        old = self.get("/api/hazards/details", kind="emergency_mapping", id="cems:EMSR935")
        self.assertTrue(old["live"])                                                # list record without a detail
        self.assertFalse(old["data"]["detail"])
        gone = self.get("/api/hazards/details", kind="emergency_mapping", id="cems:EMSR1")
        self.assertTrue(gone.get("missing"))

    def test_sensitive_activation_hides_its_areas(self):
        det = self.plus.mapping.details["EMSR937"]
        det["sensitive"] = True
        try:
            x = self.plus.mapping.detail(self.svc, "cems:EMSR937")["data"]
            self.assertTrue(all(a["geometry"] is None for a in x["aois"]))
            self.assertFalse(x["has_extent"])
            fc = self.plus.mapping.map_fc(60, False)
            self.assertFalse([f for f in fc["features"] if f["properties"].get("code") == "EMSR937" and f["properties"]["role"] == "aoi"])
            self.assertFalse(self.plus.mapping.extent("EMSR937")["features"])
        finally:
            det["sensitive"] = False

    def test_mapping_extent_stays_on_the_viewer_bucket(self):
        ext = self.get("/api/hazards/mapping/extent", id="cems:EMSR936")
        self.assertEqual(len(ext["features"]), 2)                                  # the point is dropped
        self.assertTrue(all(f["properties"]["role"] == "extent" for f in ext["features"]))
        for bad in ("EMSR936", "cems:EMSR936/../x", "cems:", "usgs:EMSR936"):
            with self.assertRaises(HTTPError):
                self.get("/api/hazards/mapping/extent", id=bad)
        # a poisoned product link in the cached record is never fetched
        aoi = self.plus.mapping.details["EMSR936"]["aois"][0]
        saved = [dict(p) for p in aoi["products"]]
        try:
            for p in aoi["products"]:
                p["observed"] = "https://evil.example/EMSR936/x.json"
            self.plus.mapping._extent.clear()
            before = len(self.net.calls)
            out = self.plus.mapping.extent("EMSR936")
            self.assertFalse(out["features"])
            self.assertFalse([u for u in self.net.calls[before:] if "evil" in u])
        finally:
            aoi["products"] = saved
            self.plus.mapping._extent.clear()

    def test_gdacs_card_links_back(self):
        ev = {"id": "gdacs:FL:1104167", "type": "FL", "kind": "flood", "type_name": "Flood", "alert": "Orange",
              "name": "Flood in Ecuador", "lat": -2.1, "lon": -79.6, "from": NOW - 9 * 86400, "to": NOW - 86400,
              "current": True, "details_url": None, "report_url": "https://www.gdacs.org/report.aspx?eventid=1104167"}
        self.svc.gdacs_events[ev["id"]] = ev
        try:
            d = self.get("/api/hazards/details", kind="flood", id="gdacs:FL:1104167")
            self.assertEqual([m["id"] for m in d["data"]["emergency_mapping"]], ["cems:EMSR936"])
            x = self.plus.mapping.detail(self.svc, "cems:EMSR936")["data"]
            self.assertEqual(x["gdacs"][0]["alert"], "Orange")
        finally:
            self.svc.gdacs_events.pop(ev["id"], None)

    # -- England floods -----------------------------------------------------------------------------
    def test_floods_layer(self):
        d = self.get("/api/hazards/floods", zoom=6)
        roles = {}
        for f in d["features"]:
            roles.setdefault(f["properties"]["role"], []).append(f["properties"])
        self.assertEqual({a["id"] for a in roles["area"]}, {"ea:114WAFT1W01A00", "ea:113FWFEXE01", "ea:113FWCSEVERE"})
        self.assertEqual(len(roles["label"]), 3)
        gauges = {g["id"]: g for g in roles["gauge"]}
        self.assertEqual(set(gauges), {"eagauge:48109", "eagauge:47133", "eagauge:E70024"})
        self.assertEqual(gauges["eagauge:47133"]["st"], "high")
        self.assertEqual(gauges["eagauge:E70024"]["sea"], 1)
        m = d["meta"]
        self.assertEqual(m["counts"], {"severe": 1, "warning": 1, "alert": 2, "ended": 1})
        self.assertEqual(m["count"], 4)                         # in force in England, not just in view
        self.assertEqual(m["outlines_pending"], 1)              # Upper Tamar's outline is a 404 here
        self.assertEqual(m["gauges_high"], 1)
        self.assertIn("Environment Agency", m["credit"])
        warn = self.get("/api/hazards/floods", zoom=6, min=2)
        self.assertEqual({f["properties"]["id"] for f in warn["features"] if f["properties"]["role"] == "area"},
                         {"ea:113FWFEXE01", "ea:113FWCSEVERE"})
        self.assertNotIn("eagauge:48109", {f["properties"]["id"] for f in warn["features"]})
        bare = self.get("/api/hazards/floods", zoom=6, gauges=0)
        self.assertFalse([f for f in bare["features"] if f["properties"]["role"] == "gauge"])
        view = self.get("/api/hazards/floods", zoom=11, bbox="-3.7,50.5,-3.3,50.8")
        self.assertEqual(view["meta"]["level"], "fine")
        self.assertEqual({f["properties"]["id"] for f in view["features"] if f["properties"]["role"] == "area"},
                         {"ea:113FWFEXE01", "ea:113FWCSEVERE"})
        for bad in ({"min": 5}, {"zoom": "x"}, {"bbox": "1,2,3"}):
            with self.assertRaises(HTTPError):
                self.get("/api/hazards/floods", **bad)

    def test_flood_and_gauge_cards(self):
        d = self.get("/api/hazards/details", kind="flood", id="ea:113FWFEXE01")
        self.assertEqual(d["type"], "ea_flood")
        x = d["data"]
        self.assertEqual(x["severity"], "Flood warning")
        self.assertEqual(x["meaning"], "Flooding is expected, immediate action required.")
        self.assertEqual([g["ref"] for g in x["gauges"]], ["47133"])
        self.assertEqual(x["page"], "https://check-for-flooding.service.gov.uk/target-area/113FWFEXE01")
        self.assertEqual(x["quick_dial"], "211008")
        self.assertTrue(x["outline"])
        self.assertAlmostEqual(d["entity"]["lat"], 50.725, places=3)
        no_outline = self.get("/api/hazards/details", kind="flood", id="ea:112WAFTUBA")
        self.assertTrue(no_outline["live"])
        self.assertEqual(no_outline["data"]["gauges"], [])
        for gone in ("ea:NOPE1", "ea:../x", "ea:"):
            self.assertFalse(self.get("/api/hazards/details", kind="flood", id=gone)["live"], gone)
        self.assertFalse([u for u in self.net.calls if ".." in u])                     # ids never shape URLs
        g = self.get("/api/hazards/details", kind="gauge", id="eagauge:47133")
        self.assertEqual(g["type"], "ea_gauge")
        self.assertEqual((g["data"]["value"], g["data"]["state"]), (1.731, "high"))
        self.assertEqual([w["id"] for w in g["data"]["warnings"]], ["ea:113FWFEXE01"])
        self.assertEqual(g["data"]["page"], "https://check-for-flooding.service.gov.uk/station/3187")
        h = self.get("/api/hazards/floods/gauge-history", id="eagauge:47133", hours=48)
        self.assertEqual(len(h["points"]), 40)
        self.assertEqual(h["unit"], "mASD")
        self.assertTrue(any("/id/measures/47133-level-stage-i-15_min-mASD/readings" in u for u in self.net.calls))
        for bad in ("eagauge:../x", "47133", "eagauge:a b"):
            with self.assertRaises(HTTPError):
                self.get("/api/hazards/floods/gauge-history", id=bad)
        self.assertEqual(self.get("/api/hazards/floods/gauge-history", id="eagauge:ZZZ")["points"], [])

    def test_trend_and_conditional_get(self):
        floods = self.plus.floods
        self.net.overrides["flood-monitoring/data/readings?latest"] = "ea_readings_later.json"
        try:
            self.assertTrue(self.svc.run("ea_gauges"))
            self.assertEqual(floods.gauge("47133")["trend"], "rising")
            self.assertEqual(floods.gauge("47133")["value"], 1.8)
        finally:
            self.net.overrides.clear()
            self.svc.run("ea_gauges")
        self.net.not_modified.add("flood-monitoring/id/floods")
        try:
            before = len(floods.warnings)
            self.assertTrue(self.svc.run("ea_floods"))           # 304: the snapshot stays
            self.assertEqual(len(floods.warnings), before)
        finally:
            self.net.not_modified.clear()

    def test_floods_here(self):
        r = self.get("/api/hazards/floods/here", lat=50.72, lon=-3.50)
        self.assertTrue(r["england"])
        self.assertEqual(r["warnings"][0]["id"], "ea:113FWFEXE01")
        self.assertTrue(r["warnings"][0]["inside"])
        self.assertIn("eagauge:47133", [g["id"] for g in r["gauges"]])
        near = self.get("/api/hazards/floods/here", lat=50.66, lon=-3.55)      # 1 km north of the severe area
        self.assertEqual(near["warnings"][0]["id"], "ea:113FWCSEVERE")
        self.assertFalse(near["warnings"][0]["inside"])
        paris = self.get("/api/hazards/floods/here", lat=48.85, lon=2.35)
        self.assertFalse(paris["england"])
        self.assertEqual(paris["warnings"], [])
        with self.assertRaises(HTTPError):
            self.get("/api/hazards/floods/here", lat=95, lon=0)

    # -- flood outlook ------------------------------------------------------------------------------
    def test_outlook(self):
        meta = self.get("/api/hazards/flood-outlook")
        self.assertEqual(meta["date"], "2026-11-23")
        self.assertIn("Generated using Copernicus Emergency Management Service information 2026", meta["credit"])
        self.assertEqual({p["id"] for p in meta["products"]}, {"days1-3", "days4-10", "days11-15", "points"})
        tile = self.get("/api/hazards/flood-outlook/tile/days1-3/3/4/3", raw=True, t="2026-11-23")
        self.assertEqual((tile.content_type, tile.cache), ("image/png", "max-age=21600"))
        self.assertEqual(tile.body, TILE)
        n = len(self.net.calls)
        self.get("/api/hazards/flood-outlook/tile/days1-3/3/4/3", raw=True, t="2026-11-23")
        self.assertEqual(len(self.net.calls), n)                                       # cached
        fetched = [u for u in self.net.calls if "GetMap" in u][-1]
        self.assertIn("time=2026-11-23T00:00Z", fetched)
        self.assertIn("crs=EPSG:3857", fetched)
        suffix = self.get("/api/hazards/flood-outlook/tile/points/2/1/1.png", raw=True)
        self.assertEqual(suffix.body, TILE)
        broken = self.get("/api/hazards/flood-outlook/tile/days11-15/3/4/3", raw=True)
        self.assertEqual((broken.body, broken.cache), (glofas.BLANK, "no-store"))       # an exception, not an image
        self.assertGreaterEqual(self.get("/api/hazards/flood-outlook")["tile_errors"], 1)
        for bad in ("/api/hazards/flood-outlook/tile/rain/3/4/3", "/api/hazards/flood-outlook/tile/days1-3/11/0/0",
                    "/api/hazards/flood-outlook/tile/days1-3/2/9/0", "/api/hazards/flood-outlook/tile/days1-3/x/0/0"):
            with self.assertRaises(HTTPError, msg=bad):
                self.get(bad, raw=True)
        old = self.get("/api/hazards/flood-outlook/tile/days4-10/1/0/0", raw=True, t="1999-01-01")
        self.assertEqual(old.body, TILE)                                               # falls back to the latest run
        with self.assertRaises(HTTPError):
            self.get("/api/hazards/flood-outlook/tile/days4-10/1/0/0", raw=True, t="x" * 40)

    # -- discovery -------------------------------------------------------------------------------------
    def test_search_nearby_events(self):
        self.assertEqual(self.app.run_search("EMSR936", 5)[0]["id"], "cems:EMSR936")
        self.assertEqual(self.app.run_search("Exmouth seafront", 5)[0]["id"], "ea:113FWCSEVERE")
        self.assertIn("eagauge:47133", [h["id"] for h in self.app.run_search("Crowford", 10)])
        near = self.app.run_nearby(50.72, -3.50, 20, 10)
        kinds = {(n["kind"], n["id"]) for n in near}
        self.assertIn(("flood", "ea:113FWFEXE01"), kinds)
        self.assertIn(("gauge", "eagauge:47133"), kinds)
        ecuador = self.app.run_nearby(-2.12, -79.62, 10, 10)
        hit = [n for n in ecuador if n["id"] == "cems:EMSR936"][0]
        self.assertEqual(hit["distance_km"], 0.0)                                       # inside the area of interest
        events = {e["id"]: e for e in self.app.collect_events(NOW - 7 * 86400)}
        self.assertEqual(events["ea:113FWCSEVERE"]["severity"], 9)
        self.assertEqual(events["ea:113FWFEXE01"]["severity"], 6)
        self.assertNotIn("ea:114WAFT1W01A00", events)                                  # alerts are not events
        self.assertIn("first_time", events["ea:113FWFEXE01"])
        self.assertEqual(events["cems:EMSR937"]["severity"], 5)
        self.assertEqual(events["cems:EMSR936"]["kind"], "emergency_mapping")
        for e in events.values():
            self.assertTrue(0 <= e["severity"] <= 10)
            self.assertIsInstance(e["time"], int)
        recent = self.app.call_ai_tool("hazards_recent", {"hours": 168, "kinds": ["flood", "emergency_mapping"]})
        self.assertTrue({"ea:113FWCSEVERE", "cems:EMSR936"} <= {e["id"] for e in recent["events"]})

    def test_ai_tools(self):
        devon = self.app.call_ai_tool("flood_warnings", {"area": "Devon"})
        self.assertEqual([w["id"] for w in devon["warnings"]], ["ea:113FWCSEVERE", "ea:113FWFEXE01", "ea:112WAFTUBA"])
        self.assertEqual(devon["in_force"], {"severe_flood_warnings": 1, "flood_warnings": 1, "flood_alerts": 2})
        self.assertEqual(devon["warnings"][1]["gauges"][0]["name"], "Crowford Bridge")
        self.assertIn("England only", devon["notes"][0])
        point = self.app.call_ai_tool("flood_warnings", {"lat": 50.72, "lon": -3.5, "radius_km": 5})
        self.assertEqual(point["warnings"][0]["distance_km"], 0.0)
        severe = self.app.call_ai_tool("flood_warnings", {"min_severity": "severe"})
        self.assertEqual([w["severity_level"] for w in severe["warnings"]], [1])
        boxed = self.app.call_ai_tool("flood_warnings", {"bbox": "-6,50,-5,50.5", "include_gauges": False})
        self.assertEqual([w["id"] for w in boxed["warnings"]], ["ea:114WAFT1W01A00"])
        self.assertNotIn("gauges", boxed["warnings"][0])
        for bad in ({"lat": 50}, {"min_severity": "huge"}, {"bbox": "x"}, {"lat": 95, "lon": 0}, {"limit": "many"}):
            with self.assertRaises(ValueError, msg=bad):
                self.app.call_ai_tool("flood_warnings", bad)
        em = self.app.call_ai_tool("emergency_mapping", {})
        self.assertEqual([a["code"] for a in em["activations"][:2]], ["EMSR937", "EMSR936"])
        self.assertEqual(em["open_now"], 2)
        one = self.app.call_ai_tool("emergency_mapping", {"code": "emsr936"})
        self.assertEqual(one["activations"][0]["areas"][0]["name"], "Milagro")
        self.assertEqual(one["activations"][0]["gdacs_event"], "gdacs:FL:1104167")
        spain = self.app.call_ai_tool("emergency_mapping", {"country": "Spain", "days": 60})
        self.assertEqual([a["code"] for a in spain["activations"]], ["EMSR935"])
        self.assertFalse(self.app.call_ai_tool("emergency_mapping", {"country": "Spain"})["activations"])   # 35 days ago
        flood = self.app.call_ai_tool("emergency_mapping", {"category": "flood", "days": 400})
        self.assertTrue(all(a["category"] == "Flood" for a in flood["activations"]))
        self.assertTrue(all(a["open"] for a in self.app.call_ai_tool("emergency_mapping", {"open_only": True})["activations"]))
        with self.assertRaises(ValueError):
            self.app.call_ai_tool("emergency_mapping", {"code": "../x"})
        with self.assertRaises(ValueError):
            self.app.call_ai_tool("emergency_mapping", {"days": "lots"})

    # -- resilience -------------------------------------------------------------------------------------
    def test_failing_source_is_contained(self):
        with mock.patch.object(self.net, "get_json", side_effect=NetError("rapidmapping: HTTP 503", status=503)):
            self.assertFalse(self.svc.run("cems"))
        self.assertTrue(self.plus.mapping.items)                    # the last good list stays in service
        self.assertFalse(self.app.sources["hazards.cems"]["ok"])
        layer = self.get("/api/hazards/mapping", days=30)
        self.assertTrue(layer["meta"]["source"]["error"])
        self.assertTrue(self.svc.run("cems"))
        self.assertTrue(self.app.sources["hazards.cems"]["ok"])

    def test_snapshots_survive_a_restart(self):
        from meridian.modules.hazards.floods import Floods
        from meridian.modules.hazards.mapping import Mapping
        mp = Mapping(self.svc)
        mp.ensure_tables()
        self.assertEqual(len(mp.items), len(self.plus.mapping.items))
        self.assertIn("EMSR936", mp.details)
        fl = Floods(self.svc)
        fl.ensure_tables()
        self.assertEqual(len(fl.warnings), 5)
        self.assertIn("113FWFEXE01", fl.areas)                       # outline from SQLite, no network
        self.assertTrue(fl.stations)                                 # station file from data/build/hazards


if __name__ == "__main__":
    unittest.main()
