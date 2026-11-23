"""Airspace and airport status without network: FAA NAS Status airport events, FAA temporary flight
restrictions, EASA conflict-zone bulletins and the outlines they are drawn on. Parsers are tested on
trimmed real samples; the service runs on a real App with a fake HTTP client.

Run: python -m unittest tests.test_flights_airspace -v
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.modules.flights import airspace_parse as ap
from meridian.modules.flights import shapes
from meridian.net import Net, NetError, NetResponse
from meridian.registry import App, HTTPError, Request
from meridian.scheduler import Scheduler
from meridian.store import Store

FIX = Path(__file__).parent / "fixtures" / "flights"
NOW = 1795447000.0            # 2026-11-23 14:36:40 UTC, when the samples were current


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------------------------
class NasParseTests(unittest.TestCase):
    def setUp(self):
        self.airports = {a["faa"]: a for a in ap.parse_nas_events(load("faa_airport_events.json"), NOW)}

    def test_kinds_levels_and_skips(self):
        self.assertEqual(set(self.airports), {"SAN", "OPF", "LAS", "ABQ", "EWR", "HNL"})   # ORD config only, BAD off the map
        ewr = self.airports["EWR"]
        self.assertEqual(ewr["icao"], "KEWR")
        self.assertEqual([s["code"] for s in ewr["statuses"]], ["ground_stop", "arrival_delay"])
        gs, arr = ewr["statuses"]
        self.assertEqual((gs["level"], gs["delay"], gs["extension"], gs["reason"]), (3, True, "Medium", "Thunderstorms"))
        self.assertEqual(gs["end"], ap.iso_ts("2026-11-23T16:00:00Z"))
        self.assertEqual(gs["scope"], ["ZNY", "ZDC", "ZBW"])
        self.assertIn("held at their departure airports until 16:00 UTC", gs["summary"])
        self.assertEqual((arr["min_delay_min"], arr["max_delay_min"], arr["avg_delay_min"], arr["trend"]), (46, 75, 60, "Increasing"))
        self.assertEqual(arr["reason"], "Weather: thunderstorms")
        self.assertEqual(ewr["runways"], {"arrival": "22L", "departure": "22R", "arrival_rate": 28})

    def test_delay_programme_and_notices(self):
        san = self.airports["SAN"]
        gdp, notice = san["statuses"]
        self.assertEqual((gdp["code"], gdp["type"], gdp["avg_delay_min"], gdp["max_delay_min"]), ("ground_delay", "Ground delay programme", 30, 80))
        self.assertEqual(gdp["reason"], "Airport volume")
        self.assertTrue(gdp["advisory"].startswith("https://www.fly.faa.gov/"))
        self.assertEqual(notice["code"], "partial_closure")
        self.assertFalse(notice["delay"])
        las = self.airports["LAS"]["statuses"][0]
        self.assertEqual(las["code"], "partial_closure")
        self.assertIn("Closed to non-scheduled transient general aviation aircraft, except with 24 hours' prior permission",
                      las["summary"])
        self.assertNotRegex(las["summary"], r"\d{3}-\d{3}-\d{4}")       # phone numbers stay in the notice text only
        self.assertEqual(self.airports["ABQ"]["statuses"][0]["code"], "deicing")
        hnl = self.airports["HNL"]
        self.assertEqual((hnl["icao"], hnl["statuses"][0]["code"], hnl["statuses"][0]["level"]), ("PHNL", "closure", 3))
        opf = self.airports["OPF"]["statuses"][0]
        self.assertEqual(opf["summary"], "Departures are delayed 16 to 30 min, increasing.")

    def test_helpers(self):
        self.assertEqual(ap.minutes_of("1 hour and 20 minutes"), 80)
        self.assertEqual(ap.minutes_of("30 minutes"), 30)
        self.assertEqual(ap.minutes_of("15"), 15)
        self.assertEqual(ap.minutes_of(45.0), 45)
        self.assertIsNone(ap.minutes_of("soon"))
        self.assertEqual(ap.reason_text("WX:Low Ceilings"), "Weather: low ceilings")
        self.assertEqual(ap.reason_text("TM Initiatives:MIT"), "Traffic management: MIT")
        self.assertEqual(ap.reason_text("VIP Movement"), "VIP movement")
        self.assertEqual(ap.faa_for("KSFO"), "SFO")
        self.assertEqual(ap.faa_for("PHNL"), "HNL")
        self.assertEqual(ap.faa_for("sfo"), "SFO")
        self.assertIsNone(ap.faa_for("EGLL"))
        self.assertIsNone(ap.faa_for("<script>"))
        self.assertEqual(ap.icao_for("SJU"), "TJSJ")
        self.assertEqual(ap.icao_for("ATL"), "KATL")
        self.assertIsNone(ap.icao_for("1O2"))


class TfrParseTests(unittest.TestCase):
    def test_list_and_polygons(self):
        listing = ap.parse_tfr_list(load("tfr_list.json"))
        self.assertEqual(len(listing), 5)
        self.assertEqual(listing["6/9335"]["category"], "vip")
        self.assertEqual(listing["6/8986"]["category"], "drones")
        self.assertEqual(listing["6/9435"]["created"], 1795392000)           # 11/23/2026
        polys = ap.parse_tfr_wfs(load("tfr_wfs.json"))
        self.assertEqual(set(polys), {"6/9435", "6/9335", "6/6924", "6/8986"})
        self.assertEqual(len(polys["6/9335"]["polygons"]), 2)               # outer ring and inner core
        self.assertEqual(polys["6/9435"]["modified"], "202611231403")
        self.assertEqual(ap.nid_of("6/9435"), "6_9435")
        self.assertEqual(ap.notam_of("6_9435"), "6/9435")
        self.assertIsNone(ap.nid_of("6/94"))
        self.assertEqual(ap.tfr_link("6/9435"), "https://tfr.faa.gov/tfr3/?page=detail_6_9435")

    def test_details(self):
        hz = ap.parse_tfr_detail((FIX / "tfr_detail_6_9435.xml").read_bytes())
        self.assertEqual(hz["start"], ap.iso_ts("2026-11-23T14:00:00Z"))
        self.assertEqual(hz["end"], ap.iso_ts("2026-11-30T23:00:00Z"))
        self.assertEqual(hz["areas"][0]["lower"], "Surface")
        self.assertEqual(hz["areas"][0]["upper"], "5,200 ft MSL")
        self.assertEqual(hz["areas"][0]["schedule"], "Daily, 14:00 to 23:00 UTC")
        self.assertIn("firefighting", hz["rule_text"])
        self.assertEqual(ap.plain_purpose(hz["purpose"]), "To provide a safe environment for fire fighting aircraft operations")
        vip = ap.parse_tfr_detail((FIX / "tfr_detail_6_9335.xml").read_bytes())
        self.assertEqual(vip["time_zone"], "EST")
        self.assertEqual(vip["start"], ap.notam_ts("2611242300"))           # the XML times are UTC whatever the zone
        self.assertEqual(vip["rule"], "91.141")
        self.assertEqual(len(vip["areas"]), 2)
        perm = ap.parse_tfr_detail((FIX / "tfr_detail_6_6924.xml").read_bytes())
        self.assertTrue(perm["permanent"])
        self.assertIsNone(perm["end"])
        self.assertEqual(perm["areas"][0]["upper"], "3,000 ft above ground")
        self.assertEqual(ap._altitude("STD", "180", "FL"), ("FL180", 18000))
        with self.assertRaises(ValueError):
            ap.parse_tfr_detail(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><XNOTAM-Update/>')


class CzibParseTests(unittest.TestCase):
    def setUp(self):
        self.items = {c["id"]: c for c in ap.parse_czib_csv((FIX / "easa_czib.csv").read_bytes())}

    def test_bulletins(self):
        self.assertEqual(len(self.items), 6)
        self.assertEqual(sum(1 for c in self.items.values() if c["active"]), 5)
        ua = self.items["czib-2022-01"]
        self.assertEqual((ua["number"], ua["revision"], ua["level"], ua["altitudes"]), ("CZIB-2022-01R14", 14, "avoid", "All altitudes"))
        self.assertFalse(ua["partial"])
        self.assertEqual(ua["countries"], [{"iso2": "UA", "name": "Ukraine"}])
        self.assertEqual(ap.fir_codes(ua["airspace"]), ["UKLV", "UKBV", "UKBU", "UKDV", "UKFV", "UKOV"])
        self.assertEqual(ua["applicability"], "Operators, EUROCONTROL Network Manager")
        self.assertEqual(ua["valid_until"], ap.dmy_ts("31/01/2027", end_of_day=True))
        ru = self.items["czib-2025-01"]
        self.assertEqual(ru["clip"], (60.0, True))
        self.assertFalse(ru["partial"])                 # the meridian limit is drawn exactly
        self.assertNotIn("DONU", ap.fir_codes(ru["airspace"]))
        ml = self.items["czib-2017-01"]
        self.assertTrue(ml["territory"])
        self.assertEqual(ml["altitudes"], "Below FL260")
        self.assertEqual(self.items["czib-2026-08"]["level"], "caution")
        self.assertTrue(self.items["czib-2026-07"]["partial"])     # over the waters of the Gulf only
        self.assertFalse(self.items["czib-2026-03"]["active"])

    def test_links_and_helpers(self):
        links = ap.parse_rss_links((FIX / "easa_czib_feed.xml").read_bytes())
        self.assertEqual(ap.match_link(self.items["czib-2025-01"], links),
                         "https://www.easa.europa.eu/domains/air-operations/czibs/2025-01-r3")
        self.assertIsNone(ap.match_link(self.items["czib-2026-03"], links))
        self.assertEqual(ap.czib_base_id("2025-01 R3"), "czib-2025-01")
        self.assertEqual(ap.recommendation_level("EASA recommends not to fly within the airspace"), "avoid")
        self.assertEqual(ap.recommendation_level("Exercise caution in the remainder"), "caution")
        self.assertEqual(ap.altitude_limit("Not operate at or below FL 260.", None), "Below FL260")
        self.assertEqual(ap.clean_easa("60� East, Sana�a � FIR"), "60° East, Sana’a – FIR")
        self.assertEqual(ap.lon_clip("west of longitude 30° West"), (-30.0, True))


class ShapesTests(unittest.TestCase):
    def test_vatspy_and_natural_earth(self):
        firs = shapes.parse_vatspy(load("vatspy_firs.geojson"))
        self.assertNotIn("UKBV-E", firs)                 # sectors are left out
        self.assertNotIn("ADR", firs)
        self.assertEqual(len(shapes.polygons_of(firs["OJAC"]["g"])), 2)    # two pieces merged
        self.assertEqual(firs["UKBV"]["label"], [30.9, 50.1])
        ne = shapes.parse_natural_earth(load("ne_countries.geojson"))
        self.assertEqual(ne["CY"]["name"], "Cyprus")     # the coded unit names the country
        self.assertEqual(len(shapes.polygons_of(ne["CY"]["g"])), 2)
        self.assertIn("FR", ne)                          # ISO_A2 -99 but ISO_A2_EH FR

    def test_geometry(self):
        ring = [[0, 0], [0.5, 0], [1, 0.001], [1.5, 0], [2, 0], [2, 1], [2, 2], [1, 2], [0, 2], [0, 1], [0, 0]]
        simple = shapes.simplify_ring(ring, 0.01)
        self.assertEqual(simple[0], simple[-1])
        self.assertEqual(len(simple), 5)                 # the 0.001 bump and the collinear points are gone
        self.assertEqual(len(shapes.simplify_ring(ring[:5] + [[0, 0]], 0.01)), 6)   # tiny rings are kept as they are
        box = {"type": "Polygon", "coordinates": [[[50, 50], [70, 50], [70, 60], [50, 60], [50, 50]]]}
        west = shapes.clip_geometry_lon(box, 60.0, west=True)
        self.assertEqual(shapes.bbox_of(west), [50.0, 50.0, 60.0, 60.0])
        self.assertIsNone(shapes.clip_geometry_lon(box, 40.0, west=True))
        u = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [8, 10], [8, 2], [2, 2], [2, 10], [0, 10], [0, 0]]]}
        lab = shapes.label_point(u)
        self.assertTrue(shapes.contains(u, lab[1], lab[0]))   # the centroid of a U is outside it; the label is not
        self.assertIsNone(shapes.clean_ring([[0, 0], [1, 1], [0, 0]]))


# ---------------------------------------------------------------------------------------------
# the service, end to end
# ---------------------------------------------------------------------------------------------
class FakeNet:
    def __init__(self):
        self.routes: list = []
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def set_host_interval(self, host, seconds):
        pass

    def request(self, url, *, method="GET", params=None, headers=None, data=None, json_body=None, timeout=20,
                retries=1, ttl=0, min_interval=None, max_bytes=0, accept=(200,), cache_key=None):
        full = Net.build_url(url, params)
        with self.lock:
            self.calls.append(full)
        for pattern, fn in self.routes:
            m = re.search(pattern, full)
            if m:
                status, body = fn(m)
                if status in accept:
                    return NetResponse(status, {}, body, full)
                raise NetError(f"{urllib.parse.urlsplit(full).hostname}: HTTP {status}", status=status, url=full)
        raise NetError("no route in the fake", url=full)

    def get_json(self, url, **kw):
        return self.request(url, **kw).json()

    def get_bytes(self, url, **kw):
        return self.request(url, **kw).body

    def count(self, pattern: str) -> int:
        with self.lock:
            return sum(1 for c in self.calls if re.search(pattern, c))


def _file(name):
    return lambda m: (200, (FIX / name).read_bytes())


def _detail(m):
    p = FIX / f"tfr_detail_{m.group(1)}.xml"
    return (200, p.read_bytes()) if p.exists() else (404, b"not found")


class AirspaceEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MERIDIAN_DATA_DIR"] = self.tmp.name
        self.net = FakeNet()
        self.net.routes = [
            (r"nasstatus\.faa\.gov/api/airport-events", _file("faa_airport_events.json")),
            (r"tfr\.faa\.gov/tfrapi/exportTfrList", _file("tfr_list.json")),
            (r"tfr\.faa\.gov/geoserver/TFR/ows", _file("tfr_wfs.json")),
            (r"tfr\.faa\.gov/download/detail_(\d_\d{4})\.xml", _detail),
            (r"easa\.europa\.eu/en/domains/air-operations/czibs/export", _file("easa_czib.csv")),
            (r"easa\.europa\.eu/en/domains/air-operations/czibs/feed\.xml", _file("easa_czib_feed.xml")),
        ]
        cfg = Config(None, overrides={"FLIGHTS_GPSJAM_SAMPLER": "0"})
        log = logging.getLogger("meridian.test")
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.app = App(cfg, self.store, self.net, log, Scheduler(log), DatasetManager(log))
        self.m = self.app.module("flights")
        from meridian.modules import flights
        flights.register(self.m)
        self.app.modules["flights"].ok = True
        self.air = self.m.airspace
        self.air.clock = lambda: NOW
        out = self.m.data_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / shapes.FIRS_FILE).write_text(json.dumps({"items": shapes.parse_vatspy(load("vatspy_firs.geojson"))}),
                                            encoding="utf-8")
        (out / shapes.COUNTRIES_FILE).write_text(json.dumps({"items": shapes.parse_natural_earth(load("ne_countries.geojson"))}),
                                                 encoding="utf-8")

    def tearDown(self):
        self.store.close()
        os.environ.pop("MERIDIAN_DATA_DIR", None)
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def get(self, path_qs: str):
        split = urllib.parse.urlsplit(path_qs)
        route, params, _ = self.app.match("GET", split.path)
        self.assertIsNotNone(route, path_qs)
        req = Request("GET", split.path, params, urllib.parse.parse_qs(split.query), {}, None)
        return route.handler(req)

    # -- US airport delays -------------------------------------------------------------------
    def test_airport_status_contract_shape(self):
        self.assertTrue(self.air.refresh_nas())
        d = self.get("/api/flights/airport-status/KSAN")
        agreed = {"delay", "type", "reason", "avg_delay_min", "trend", "source", "updated"}
        self.assertTrue(agreed <= set(d))
        self.assertEqual({k: d[k] for k in agreed - {"updated"}},
                         {"delay": True, "type": "Ground delay programme", "reason": "Airport volume", "avg_delay_min": 30,
                          "trend": None, "source": "FAA NAS Status"})
        self.assertIsInstance(d["updated"], float)
        self.assertEqual(len(d["items"]), 2)
        self.assertEqual(d["entity"]["kind"], "airport_delay")
        self.assertEqual(self.get("/api/flights/airport-status/SAN")["type"], "Ground delay programme")
        self.assertEqual(self.get("/api/flights/airport-status/PHNL")["type"], "Airport closed")
        ewr = self.get("/api/flights/airport-status/KEWR")
        self.assertEqual((ewr["type"], ewr["level"]), ("Ground stop", "high"))
        abq = self.get("/api/flights/airport-status/KABQ")
        self.assertFalse(abq["delay"])                  # de-icing is information, not a delay
        for ident in ("KJFK", "EGLL"):
            with self.assertRaises(HTTPError) as cm:
                self.get(f"/api/flights/airport-status/{ident}")
            self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(HTTPError) as cm:
            self.get("/api/flights/airport-status/K<SF")
        self.assertEqual(cm.exception.status, 400)

    def test_airport_status_when_the_faa_is_down(self):
        self.net.routes.insert(0, (r"nasstatus\.faa\.gov", lambda m: (503, b"")))
        with self.assertRaises(HTTPError) as cm:
            self.get("/api/flights/airport-status/KSAN")
        self.assertEqual(cm.exception.status, 503)
        fc = self.get("/api/flights/airport-status")
        self.assertEqual(fc["features"], [])
        self.assertIn("HTTP 503", fc["meta"]["error"])
        self.assertFalse(self.app.sources["flights.faa_nas"]["ok"])

    def test_delays_layer_and_events(self):
        fc = self.get("/api/flights/airport-status")
        self.assertEqual(fc["meta"]["airports"], 6)
        self.assertEqual(fc["meta"]["delayed"], 4)       # EWR, HNL, SAN, OPF
        # The source note counts delays and full closures only; partial closures and de-icing are "any status"
        self.assertTrue(self.air.refresh_nas())
        note = self.app.sources["flights.faa_nas"]["note"]
        self.assertTrue(note.startswith("4 airports with delays or full closures ("), note)
        self.assertTrue(note.endswith(", 6 with any status"), note)
        first = fc["features"][0]["properties"]
        self.assertEqual(first["level"], 3)
        events = [e for e in self.app.collect_events(NOW - 3600) if e["kind"] == "airport_delay"]
        ids = [e["id"] for e in events]
        self.assertEqual(len(ids), len(set(ids)))        # one id per episode, so event history keeps them apart
        self.assertTrue(all(re.fullmatch(r"[A-Z]{4}:[a-z_]+:\d+", i) for i in ids))
        ev = {(e["id"].split(":")[0], e["event"]): e for e in events}
        self.assertEqual(ev[("KEWR", "ground_stop")]["severity"], 5)
        self.assertEqual(ev[("KEWR", "arrival_delay")]["severity"], 4)
        self.assertEqual(ev[("KSAN", "ground_delay")]["severity"], 3)
        self.assertEqual(ev[("PHNL", "closure")]["severity"], 5)
        self.assertNotIn(("KOPF", "departure_delay"), ev)       # 16 to 30 minutes is not a long delay
        n = self.store.scalar("SELECT COUNT(*) FROM flights_airport_events")
        self.air.clock = lambda: NOW + 300
        self.air.refresh_nas()
        self.assertEqual(self.store.scalar("SELECT COUNT(*) FROM flights_airport_events"), n)   # same episodes
        self.assertEqual(self.store.scalar("SELECT last_t FROM flights_airport_events WHERE code = 'arrival_delay'"), NOW + 300)

    def test_polling_cadence_and_backoff(self):
        self.air.refresh_nas()
        self.assertEqual(self.net.count("airport-events"), 1)
        self.air.tick_nas()                                       # not in use, fetched just now
        self.assertEqual(self.net.count("airport-events"), 1)
        self.air.clock = lambda: NOW + 600
        self.air.tick_nas()                                       # idle cadence is 15 min
        self.assertEqual(self.net.count("airport-events"), 1)
        self.m.touch("flights.delays")
        self.air.tick_nas()                                       # in use: every 2 min
        self.assertEqual(self.net.count("airport-events"), 2)
        self.net.routes.insert(0, (r"nasstatus\.faa\.gov", lambda m: (500, b"")))
        self.air.clock = lambda: NOW + 800
        self.air.tick_nas()
        self.air.tick_nas()                                       # backing off: no second try straight away
        self.assertEqual(self.net.count("airport-events"), 3)
        self.assertIsNotNone(self.air.nas_feed.error)
        self.assertEqual(len(self.air.airports), 6)               # the last good snapshot stays in service

    # -- flight restrictions ----------------------------------------------------------------
    def test_tfr_layer_details_and_events(self):
        self.assertTrue(self.air.refresh_tfr())
        fc = self.get("/api/flights/tfrs")
        meta = fc["meta"]
        self.assertEqual((meta["count"], meta["mapped"]), (5, 4))
        self.assertEqual(len([f for f in fc["features"] if f["properties"]["role"] == "area"]), 5)
        self.assertEqual(self.air.backfill_details(limit=10), 3)
        self.air.backfill_details(limit=10)                       # misses are not retried straight away
        self.assertEqual(self.net.count(r"detail_"), 5)
        vip = self.get("/api/flights/tfrs/6_9335")
        self.assertEqual((vip["status"], vip["type_label"], vip["upper"]), ("upcoming", "VIP movement", "17,999 ft MSL"))
        self.assertEqual(vip["place"], "West Palm Beach, Florida")
        self.assertEqual(vip["city"], "West Palm Beach")
        self.assertEqual(vip["entity"], {"kind": "flight_restriction", "id": "6/9335", "title": "West Palm Beach, Florida",
                                         "subtitle": "VIP movement, FDC 6/9335", "layer": "flights.tfr", "zoom": 9,
                                         "lon": vip["center"][0], "lat": vip["center"][1], "bbox": vip["bbox"]})
        hz = self.get("/api/flights/tfrs/6_9435")
        self.assertEqual((hz["status"], hz["schedule"]), ("active", "Daily, 14:00 to 23:00 UTC"))
        disney = self.get("/api/flights/tfrs/6_6924")
        self.assertTrue(disney["permanent"])
        drones = self.get("/api/flights/tfrs/6_8986")
        self.assertEqual((drones["status"], drones["has_detail"]), ("listed", False))
        national = self.get("/api/flights/tfrs/6_3366")
        self.assertFalse(national["mapped"])
        for bad, status in (("1_0000", 404), ("6-9335", 400)):
            with self.assertRaises(HTTPError) as cm:
                self.get(f"/api/flights/tfrs/{bad}")
            self.assertEqual(cm.exception.status, status)
        ev = [e for e in self.app.collect_events(NOW - 2 * 86400) if e["kind"] == "flight_restriction"]
        self.assertEqual([(e["id"], e["severity"]) for e in ev], [("6/9335", 4)])     # VIP; drones and the remote fire are not events
        self.assertIn("VIP movement", ev[0]["title"])

    # -- conflict-zone bulletins ------------------------------------------------------------
    def test_advisories_shapes_and_cards(self):
        self.assertTrue(self.air.refresh_czib())
        fc = self.get("/api/flights/advisories")
        meta = fc["meta"]
        self.assertEqual((meta["count"], meta["mapped"], meta["withdrawn"]), (5, 5, 1))
        self.assertEqual(meta["by_shape"], {"fir": 4, "country": 1})
        ru = self.air.czib("czib-2025-01")
        self.assertEqual(ru["shape"], "fir")
        self.assertLessEqual(ru["bbox"][2], 60.0)                      # clipped at 60° E
        ml = self.air.czib("czib-2017-01")
        self.assertEqual((ml["shape"], [p["code"] for p in ml["parts"]]), ("country", ["ML"]))
        ua = self.get("/api/flights/advisories/czib-2022-01")
        self.assertEqual([p["code"] for p in ua["parts"]], ["UKLV", "UKBV", "UKDV", "UKFV", "UKOV"])
        self.assertEqual(ua["link"], "https://www.easa.europa.eu/domains/air-operations/czibs/czib-2022-01r14")
        self.assertEqual(ua["level_label"], "Do not operate")
        self.assertEqual(ua["short"], "Ukraine")
        self.assertTrue(shapes.contains(self.air.czib("czib-2022-01")["geometry"], ua["label"][1], ua["label"][0]))
        labels = [f for f in fc["features"] if f["properties"]["role"] == "label"]
        self.assertEqual(len(labels), 5)
        for bad, status in (("czib-1999-99", 404), ("ukraine", 400)):
            with self.assertRaises(HTTPError) as cm:
                self.get(f"/api/flights/advisories/{bad}")
            self.assertEqual(cm.exception.status, status)
        jo = self.air.czib("czib-2026-08")
        ev = [e for e in self.app.collect_events(jo["issued"] - 1) if e["kind"] == "airspace_advisory"]
        self.assertIn(("czib-2026-08", 4), [(e["id"], e["severity"]) for e in ev])

    def test_bulletins_survive_a_restart_without_refetching(self):
        from meridian.modules.flights.airspace import AirspaceService
        self.air.refresh_czib()
        calls = self.net.count("czibs/export")
        again = AirspaceService(self.m, clock=lambda: NOW + 600)
        self.assertEqual(len(again.active_czibs()), 5)
        self.assertEqual(again.czib("czib-2017-01")["shape"], "country")
        again.tick_czib()
        self.assertEqual(self.net.count("czibs/export"), calls)       # not due for 12 hours
        self.store.kv_set("flights.czib.format", 0)                    # stored by an older parser
        older = AirspaceService(self.m, clock=lambda: NOW + 600)
        self.assertEqual(len(older.active_czibs()), 5)                 # still shown meanwhile
        older.tick_czib()
        self.assertEqual(self.net.count("czibs/export"), calls + 1)

    # -- discovery and AI tools -------------------------------------------------------------
    def test_whats_here_search_nearby(self):
        self.air.refresh_nas()
        self.air.refresh_tfr()
        self.air.backfill_details(limit=10)
        self.air.refresh_czib()
        at = self.get("/api/flights/airspace/at?lat=50&lon=30")
        self.assertEqual([c["id"] for c in at["advisories"]], ["czib-2022-01"])
        at = self.get("/api/flights/airspace/at?lat=26.68&lon=-80.06")
        self.assertEqual([t["id"] for t in at["tfrs"]], ["6/9335"])
        self.assertEqual(at["tfrs"][0]["entity"]["kind"], "flight_restriction")
        at = self.get("/api/flights/airspace/at?lat=32.74&lon=-117.2")
        self.assertEqual(at["delays"][0]["id"], "KSAN")
        found = self.app.run_search("6/9435", 5)
        self.assertEqual((found[0]["kind"], found[0]["id"]), ("flight_restriction", "6/9435"))
        found = self.app.run_search("KSAN", 5)
        self.assertEqual((found[0]["kind"], found[0]["id"]), ("airport_delay", "KSAN"))
        found = self.app.run_search("Ukraine", 5)
        self.assertEqual((found[0]["kind"], found[0]["id"]), ("airspace_advisory", "czib-2022-01"))
        stops = [f["id"] for f in self.app.run_search("ground stop", 10) if f["kind"] == "airport_delay"]
        self.assertEqual(stops, ["KEWR"])
        delayed = {f["id"] for f in self.app.run_search("delays", 10) if f["kind"] == "airport_delay"}
        self.assertEqual(delayed, {"KEWR", "KSAN", "KOPF"})              # not de-icing or notices
        near = self.app.run_nearby(32.73, -117.19, 20, 10)
        self.assertIn(("airport_delay", "KSAN"), [(n["kind"], n["id"]) for n in near])
        with self.assertRaises(HTTPError):
            self.get("/api/flights/airspace/at?lat=95&lon=0")

    def test_ai_tools(self):
        self.air.refresh_nas()
        self.air.refresh_tfr()
        self.air.backfill_details(limit=10)
        self.air.refresh_czib()
        res = self.app.call_ai_tool("airport_delays", {})
        self.assertEqual(res["count"], 4)
        self.assertIn(res["airports"][0]["faa"], ("EWR", "HNL"))
        self.assertEqual(res["counts_by_type"]["ground_stop"], 1)
        one = self.app.call_ai_tool("airport_delays", {"airport": "KEWR"})
        self.assertTrue(one["found"])
        self.assertEqual(one["statuses"][0]["type"], "Ground stop")
        self.assertEqual(one["statuses"][0]["end_utc"], "2026-11-23T16:00Z")
        self.assertFalse(self.app.call_ai_tool("airport_delays", {"airport": "EGLL"})["found"])
        stops = self.app.call_ai_tool("airport_delays", {"type": "ground_stop"})
        self.assertEqual([a["faa"] for a in stops["airports"]], ["EWR"])
        with self.assertRaises(ValueError):
            self.app.call_ai_tool("airport_delays", {"type": "bogus"})
        cz = self.app.call_ai_tool("airspace_restrictions", {"kind": "conflict_zone", "country": "UA"})
        self.assertEqual([c["id"] for c in cz["conflict_zones"]["items"]], ["czib-2022-01"])
        self.assertNotIn("tfrs", cz)
        self.assertEqual(self.app.call_ai_tool("airspace_restrictions", {"kind": "conflict_zone", "country": "Mali"})
                         ["conflict_zones"]["items"][0]["altitudes"], "Below FL260")
        near = self.app.call_ai_tool("airspace_restrictions", {"kind": "tfr", "lat": 26.7, "lon": -80.06, "radius_km": 20})
        self.assertEqual(near["tfrs"]["count"], 0)                    # the VIP restriction starts later
        later = self.app.call_ai_tool("airspace_restrictions", {"kind": "tfr", "lat": 26.7, "lon": -80.06, "radius_km": 20,
                                                                "active_only": False})
        self.assertEqual(later["tfrs"]["items"][0]["id"], "6/9335")
        self.assertEqual(later["tfrs"]["items"][0]["distance_km"], 0.0)
        everything = self.app.call_ai_tool("airspace_restrictions", {})
        self.assertEqual(everything["conflict_zones"]["count"], 5)
        self.assertEqual(self.app.call_ai_tool("airspace_restrictions", {"kind": "tfr", "country": "FR"})["tfrs"]["count"], 0)
        for bad in ({"kind": "nope"}, {"active_only": "yes"}, {"lat": 100, "lon": 0}):
            with self.assertRaises(ValueError):
                self.app.call_ai_tool("airspace_restrictions", bad)

    def test_declarations(self):
        ids = [s["id"] for s in self.app.layer_specs]
        for lid in ("flights.delays", "flights.tfr", "flights.czib"):
            self.assertIn(lid, ids)
        for sid in ("flights.faa_nas", "flights.faa_tfr", "flights.easa_czib", "flights.vatspy_firs", "flights.ne_countries"):
            self.assertIn(sid, self.app.sources)
            self.assertTrue(self.app.sources[sid]["licence"])
        names = [t["name"] for t in self.app.ai_tools()]
        self.assertIn("airport_delays", names)
        self.assertIn("airspace_restrictions", names)
        st = self.get("/api/flights/airspace/status")
        self.assertEqual(set(st), {"nas", "tfr", "czib", "in_use"})


if __name__ == "__main__":
    unittest.main()
