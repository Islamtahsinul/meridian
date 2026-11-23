"""Space weather and aurora tests: SWPC parsers, plain-language alerts, events, OVATION tiles and lines,
darkness, CGM latitude, "aurora chance here tonight", routes, AI tool, search and politeness.

Run: python -m unittest tests.test_space_weather -v      (no network: tests/fixtures/space/swpc/)

The quiet-day fixtures are trimmed copies of the real SWPC products from 23 Nov 2026. The storm
fixtures (alerts_storm.json, *-storm.json) follow SWPC's message formats with invented values.
The OVATION grid is generated here: a Gaussian ring in latitude, in SWPC's exact JSON layout.
"""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.net import NetError, NetResponse
from meridian.registry import App, HTTPError, Request, Response
from meridian.scheduler import Scheduler
from meridian.store import Store
from meridian.modules.space import aurora as A
from meridian.modules.space import cgm
from meridian.modules.space import spaceweather as SW

FIX = Path(__file__).parent / "fixtures" / "space" / "swpc"
NOW = A.iso_time("2026-11-23T15:00:00Z")          # the quiet fixtures were fetched at 14:40 UTC
STORM_NOW = A.iso_time("2026-11-20T04:00:00Z")


def fx(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def ovation_json(peak: float = 40.0, centre: float = 67.0, width: float = 3.0, observed="2026-11-23T14:32:00Z",
                 forecast="2026-11-23T15:54:00Z") -> dict:
    """A synthetic OVATION file: a ring of aurora around each pole, in SWPC's layout."""
    coords = []
    for lon in range(360):
        for lat in range(-90, 91):
            p = peak * math.exp(-((abs(lat) - centre) / width) ** 2) * (0.75 if lat < 0 else 1.0)
            coords.append([lon, lat, int(round(p))])
    return {"Observation Time": observed, "Forecast Time": forecast, "Data Format": "[Longitude, Latitude, Aurora]",
            "coordinates": coords, "type": "MultiPoint"}


GRID = A.Grid.from_json(ovation_json())


def png_rows(png: bytes) -> tuple[int, int, int, list[bytes]]:
    """Minimal PNG reader for our own palette tiles: (width, height, colour type, rows)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    idat = b""
    width = height = ctype = None
    while pos < len(png):
        n = struct.unpack(">I", png[pos:pos + 4])[0]
        tag = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + n]
        crc = struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])[0]
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF, tag
        if tag == b"IHDR":
            width, height, _, ctype = struct.unpack(">IIBB", data[:10])
        elif tag == b"IDAT":
            idat += data
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = width + 1
    rows = [raw[i * stride + 1:(i + 1) * stride] for i in range(height)]
    assert all(raw[i * stride] == 0 for i in range(height))
    return width, height, ctype, rows


# ---------------------------------------------------------------------------------------------
# Parsers on the real (quiet) products
# ---------------------------------------------------------------------------------------------
class ParserTests(unittest.TestCase):
    def test_scales(self):
        s = SW.parse_scales(fx("noaa-scales.json"))
        self.assertEqual(s["now"]["G"]["level"], 0)
        self.assertEqual(s["now"]["R"]["text"], "none")
        self.assertEqual(len(s["forecast"]), 3)
        self.assertEqual(s["forecast"][0]["R"]["minor_prob"], 1)
        self.assertEqual(s["forecast"][0]["S"]["prob"], 1)
        with self.assertRaises(ValueError):
            SW.parse_scales({"error": "nope"})

    def test_kp_lists(self):
        kp = SW.parse_kp(fx("noaa-planetary-k-index.json"))
        self.assertGreater(len(kp), 40)
        self.assertEqual([s["time"] for s in kp], sorted(s["time"] for s in kp))
        fc = SW.parse_kp_forecast(fx("noaa-planetary-k-index-forecast.json"))
        kinds = {s["kind"] for s in fc}
        self.assertEqual(kinds, {"observed", "estimated", "predicted"})
        self.assertTrue(any(s["g"] == 1 for s in fc))               # 19 Nov 00 UTC, Kp 5, noaa_scale G1
        now = SW.parse_kp_now(fx("planetary_k_index_1m.json"))
        self.assertTrue(0 <= now[-1]["kp"] <= 9)
        # the older SWPC layout (a list of lists with a header row) still parses
        old = [["time_tag", "Kp", "a_running", "station_count"], ["2026-11-23 09:00:00.000", "1.33", "5", "8"]]
        self.assertEqual(SW.parse_kp(old)[0]["kp"], 1.33)
        with self.assertRaises(ValueError):
            SW.parse_kp([])

    def test_g_scale_from_kp(self):
        self.assertEqual([SW.g_of_kp(k) for k in (0, 4.33, 4.67, 5.0, 5.67, 6.33, 7.0, 7.67, 8.67, 9.0)],
                         [0, 0, 1, 1, 2, 2, 3, 4, 4, 5])

    def test_flares(self):
        fl = SW.parse_flares(fx("xray-flares-7-day.json"))
        self.assertEqual([f["class"] for f in fl if f["class"][0] == "M"], ["M1.1", "M2.4", "M1.7", "M1.1"])
        m24 = next(f for f in fl if f["class"] == "M2.4")
        self.assertEqual(m24["r"], 1)
        self.assertIn("R1", m24["impact"])
        self.assertEqual(SW.flux_class(2.83e-7), "B2.8")
        self.assertEqual(SW.flux_class(1.23e-3), "X12.3")
        self.assertAlmostEqual(SW.class_flux("X2.4"), 2.4e-4)
        self.assertEqual([SW.r_level(SW.class_flux(c)) for c in ("C9.9", "M1.0", "M5.0", "X1.0", "X10", "X20")],
                         [0, 1, 2, 3, 4, 5])
        now = SW.parse_flare_now(fx("xray-flares-latest.json"))
        self.assertEqual(now["current_class"], "B1.8")
        self.assertEqual(now["flare"]["class"], "B2.8")

    def test_solar_wind(self):
        w = SW.parse_wind(fx("propagated-solar-wind-1-hour.json"))
        self.assertTrue(250 < w["speed"] < 1500)
        self.assertIsNotNone(w["bz"])
        self.assertGreater(w["arrives"], w["time"])                    # propagated from L1 to Earth
        self.assertGreater(len(w["series"]), 30)
        self.assertLessEqual(w["south_minutes"], w["minutes"])
        s = SW.parse_wind_summary(fx("solar-wind-speed.json"), fx("solar-wind-mag-field.json"))
        self.assertEqual((s["speed"], s["bz"], s["bt"]), (323.0, -1.0, 2.0))
        with self.assertRaises(ValueError):
            SW.parse_wind([["time_tag", "speed"]])

    def test_alert_messages_in_plain_words(self):
        al = SW.parse_alerts(fx("alerts.json"))
        self.assertEqual(len(al), 16)
        by_code = {}
        for a in al:
            by_code.setdefault(a["code"], a)
        self.assertEqual(by_code["ALTK05"]["title"], "Kp reached 5: minor geomagnetic storm (G1)")
        self.assertEqual(by_code["ALTK05"]["scale"], "G1")
        self.assertTrue(by_code["WARK04"]["title"].startswith("Kp 4 expected until"))
        self.assertEqual(by_code["ALTEF3"]["title"], "High-energy electrons in orbit")
        self.assertEqual(by_code["ALTTP2"]["title"], "Coronal mass ejection likely")
        self.assertIn("1,972 km/s", by_code["ALTTP2"]["detail"])
        self.assertIn("Aurora - ", " ".join(by_code["ALTK05"]["impacts"]))
        self.assertNotIn("noaa-scales-explanation", by_code["ALTK05"]["message"])
        for a in al:
            self.assertEqual(a["title"], a["title"].strip())
            self.assertNotRegex(a["title"], r"[A-Z]{5,}")                 # no shouting in the plain titles

    def test_alert_states_on_a_real_night(self):
        al = SW.parse_alerts(fx("alerts.json"))
        quiet = SW.alerts_section(al, NOW)
        self.assertEqual(quiet["active"], [])                            # 23 Nov: nothing in force
        self.assertTrue(quiet["recent"])
        t = A.iso_time("2026-11-19T01:30:00Z")
        busy = SW.alerts_section(al, t)
        codes = [a["code"] for a in busy["active"]]
        self.assertEqual(sorted(codes), ["ALTK05", "WARK05"])              # the K4 messages give way to K5
        k5 = next(a for a in busy["active"] if a["code"] == "WARK05")
        self.assertEqual(SW.iso(k5["end"]), "2026-11-19T03:00:00Z")
        # later that morning the K5 warning has expired and the extended K4 warning (until 09:00) is in force
        later = SW.alerts_section(al, A.iso_time("2026-11-19T05:00:00Z"))
        warn = [a for a in later["active"] if a["type"] == "warning"]
        self.assertEqual([a["code"] for a in warn], ["WARK04"])
        self.assertEqual(SW.iso(warn[0]["end"]), "2026-11-19T09:00:00Z")

    def test_swpc_times(self):
        self.assertEqual(SW.iso(SW.swpc_time("2026 Nov 19 0118 UTC")), "2026-11-19T01:18:00Z")
        self.assertIsNone(SW.swpc_time("soon"))
        self.assertEqual(SW.iso(A.iso_time("2026-11-19 01:18:16.670")), "2026-11-19T01:18:16Z")


# ---------------------------------------------------------------------------------------------
# A storm: alerts in force, events, summary
# ---------------------------------------------------------------------------------------------
def storm_data() -> dict:
    return {"alerts": SW.parse_alerts(fx("alerts_storm.json")), "scales": SW.parse_scales(fx("noaa-scales-storm.json")),
            "kp": SW.parse_kp(fx("noaa-planetary-k-index-storm.json")),
            "flares": SW.parse_flares(fx("xray-flares-storm.json")), "flare_now": None}


class StormTests(unittest.TestCase):
    def test_alerts_in_force(self):
        sec = SW.alerts_section(storm_data()["alerts"], STORM_NOW)
        titles = [a["title"] for a in sec["active"]]
        self.assertEqual(titles[:3], ["Strong geomagnetic storm (G3) watch", "Kp reached 7: strong geomagnetic storm (G3)",
                                      "X2.4 solar flare"])
        self.assertIn("Moderate solar radiation storm (S2)", titles)
        self.assertIn("Kp 6 expected until Fri 20 Nov 09:00 UTC", titles)
        codes = {a["code"] for a in sec["active"]}
        self.assertNotIn("WARK05", codes)                                  # cancelled
        self.assertNotIn("WATA20", codes)                                  # superseded by the G3 watch
        self.assertNotIn("WARSUD", codes)                                  # its window has passed
        watch = next(a for a in sec["active"] if a["type"] == "watch")
        self.assertEqual([d["g"] for d in watch["days"]], [1, 3, 2])
        self.assertEqual(SW.iso(watch["end"]), "2026-11-22T00:00:00Z")

    def test_events(self):
        d = storm_data()
        ev = {e["type"]: e for e in SW.build_events(d, STORM_NOW)}
        self.assertEqual(set(ev), {"geomagnetic_storm", "solar_flare", "radiation_storm", "storm_watch"})
        g = ev["geomagnetic_storm"]
        self.assertEqual((g["id"], g["severity"], g["scale"]), ("swpc:gstorm:20261120", 6, "G3"))
        self.assertEqual((g["lat"], g["lon"]), A.POLE)
        f = ev["solar_flare"]
        self.assertEqual((f["id"], f["severity"], f["scale"]), ("swpc:flare:20261119T1305", 5, "R3"))
        self.assertAlmostEqual(f["lat"], -19.5, delta=0.3)                # under the Sun at the peak
        self.assertEqual(ev["radiation_storm"]["severity"], 4)
        self.assertEqual(ev["storm_watch"]["id"], "swpc:gwatch:230")
        for e in ev.values():
            self.assertEqual(e["kind"], "space_weather")
            self.assertTrue(-90 <= e["lat"] <= 90 and -180 <= e["lon"] <= 180)
            self.assertTrue(e["title"] and e["subtitle"] and e["url"].startswith("https://www.spaceweather.gov/"))
            self.assertTrue(SW._EVENT_ID.match(e["id"]))
        # ids are stable while the storm goes on
        later = {e["id"] for e in SW.build_events(d, STORM_NOW + 1800)}
        self.assertEqual(later, {e["id"] for e in ev.values()})
        # the M5.6 flare is notable but not an event; quiet days have no events at all
        self.assertFalse(any("M5.6" in e["title"] for e in ev.values()))
        quiet = {"alerts": SW.parse_alerts(fx("alerts.json")), "scales": SW.parse_scales(fx("noaa-scales.json")),
                 "kp": SW.parse_kp(fx("noaa-planetary-k-index.json")), "flares": SW.parse_flares(fx("xray-flares-7-day.json"))}
        self.assertEqual(SW.build_events(quiet, NOW), [])

    def test_summary(self):
        snap = SW.build_now(storm_data(), STORM_NOW)
        self.assertTrue(snap["summary"].startswith("Strong geomagnetic storm (G3) now, Kp 7.3."))
        self.assertIn("radiation storm (S2)", snap["summary"])
        self.assertIn("X2.4", snap["summary"])
        self.assertEqual(snap["xray"]["counts_7d"], {"C": 0, "M": 1, "X": 1})


# ---------------------------------------------------------------------------------------------
# OVATION: grid, lines, tiles
# ---------------------------------------------------------------------------------------------
class AuroraGridTests(unittest.TestCase):
    def test_grid(self):
        self.assertEqual(GRID.north_max, 40)
        self.assertEqual(GRID.south_max, 30)
        self.assertEqual(SW.iso(GRID.forecast), "2026-11-23T15:54:00Z")
        self.assertAlmostEqual(GRID.value(67.0, 10.0), 40.0, delta=0.01)
        ring66 = round(40 * math.exp(-1 / 9))                              # the synthetic ring at 66 N
        self.assertAlmostEqual(GRID.value(66.5, 359.5), (ring66 + 40) / 2, delta=0.01)   # bilinear, wraps at 360
        with self.assertRaises(ValueError):
            A.Grid.from_json({"coordinates": [[0, 0, 1]]})

    def test_viewing_lines(self):
        edge = GRID.edge(True)
        expected = 67.0 - 3.0 * math.sqrt(math.log(4.0))                 # where the ring falls to 10%
        self.assertTrue(all(abs(v - expected) < 0.6 for v in edge))
        fc = GRID.lines()
        kinds = {(f["properties"]["kind"], f["properties"]["hemisphere"]) for f in fc["features"]}
        self.assertEqual(kinds, {("overhead", "north"), ("horizon", "north"), ("overhead", "south"), ("horizon", "south")})
        over = next(f for f in fc["features"] if f["properties"]["kind"] == "overhead" and f["properties"]["hemisphere"] == "north")
        hor = next(f for f in fc["features"] if f["properties"]["kind"] == "horizon" and f["properties"]["hemisphere"] == "north")
        line = over["geometry"]["coordinates"][0]
        self.assertEqual((line[0][0], line[-1][0], len(line)), (-180.0, 180.0, 361))    # a closed ring, no wrap jump
        self.assertAlmostEqual(line[10][1] - hor["geometry"]["coordinates"][0][10][1], A.HORIZON_DEG, places=2)
        # a hemisphere without 10% anywhere draws no lines
        faint = A.Grid.from_json(ovation_json(peak=8.0))
        self.assertEqual(faint.lines()["features"], [])

    def test_tiles(self):
        w, h, ctype, rows = png_rows(GRID.tile(2, 1, 1))          # 0..90 E, 0..66.5 N: the ring's southern side
        self.assertEqual((w, h, ctype), (256, 256, 3))
        self.assertGreater(max(rows[0]), 30 * A.SCALE)           # top row is near 66 N (pixels are percent x SCALE)
        self.assertEqual(max(rows[-1]), 0)                       # bottom row is the equator
        self.assertIs(GRID.tile(3, 4, 3), A.EMPTY_TILE)          # 0..41 N: nothing there
        _, _, _, rows = png_rows(A.EMPTY_TILE)
        self.assertEqual(sum(map(sum, rows)), 0)
        self.assertEqual(len(A.PAL_RGB), 251 * 3)
        self.assertEqual(len(A.PAL_ALPHA), 251)
        self.assertEqual(A.PAL_ALPHA[0], 0)
        self.assertLessEqual(max(A.PAL_ALPHA), 255)
        self.assertEqual(set(A.PAL_ALPHA[:12]), {0})              # under 5% is transparent
        self.assertGreater(A.PAL_ALPHA[13], 0)
        self.assertEqual(list(A.PAL_ALPHA[13:]), sorted(A.PAL_ALPHA[13:]))   # more opaque with probability
        self.assertEqual(GRID.tile(2, 1, 1), GRID.tile(2, 1, 1))  # cached

    def test_tile_speed(self):
        import time as _t
        g = A.Grid.from_json(ovation_json())
        t0 = _t.perf_counter()
        for x in range(8):
            g.tile(3, x, 1)
        self.assertLess((_t.perf_counter() - t0) / 8, 0.25)


# ---------------------------------------------------------------------------------------------
# Darkness, CGM latitude and "aurora chance here tonight"
# ---------------------------------------------------------------------------------------------
class AuroraHereTests(unittest.TestCase):
    def test_cgm_table(self):
        self.assertAlmostEqual(cgm.lat(51.5, -0.13), 47.2, delta=0.4)        # London: 6 degrees below the dipole value
        self.assertAlmostEqual(cgm.lat(69.65, 18.96), 67.0, delta=0.6)       # Tromso
        self.assertAlmostEqual(cgm.lat(64.84, -147.72), 65.0, delta=0.6)     # Fairbanks
        self.assertAlmostEqual(cgm.lat(-42.88, 147.33), -53.4, delta=0.6)    # Hobart
        self.assertIsNone(cgm.lat(10.0, 0.0))
        self.assertAlmostEqual(cgm.midnight_ut(51.5, -0.13), 23.35, delta=0.3)
        self.assertTrue(0 <= cgm.midnight_ut(0.0 + 45, 179.9) < 24)

    def test_dark_windows(self):
        noon = A.iso_time("2026-11-23T12:00:00Z")
        d = A.dark_window(51.5, -0.13, noon)
        self.assertFalse(d["dark_now"])
        self.assertAlmostEqual(d["start"], A.iso_time("2026-11-23T17:21:00Z"), delta=600)
        self.assertAlmostEqual(d["end"], A.iso_time("2026-11-24T06:13:00Z"), delta=600)
        self.assertLess(A.sun_elevation(51.5, -0.13, d["start"] + 600), -12)
        night = A.dark_window(51.5, -0.13, A.iso_time("2026-11-23T22:00:00Z"))
        self.assertTrue(night["dark_now"])
        self.assertAlmostEqual(night["start"], d["start"], delta=60)
        polar = A.dark_window(86.0, 0.0, A.iso_time("2026-12-21T12:00:00Z"))
        self.assertTrue(polar["dark_now"] and polar["polar_night"])
        summer = A.dark_window(69.65, 18.96, A.iso_time("2026-06-21T12:00:00Z"))
        self.assertIsNone(summer["start"])
        self.assertTrue(A.iso_time("2026-08-01T00:00:00Z") < summer["returns"] < A.iso_time("2026-09-15T00:00:00Z"))

    def slots(self, kp: float, start: float, n: int = 16) -> list[dict]:
        t0 = math.floor(start / 10800) * 10800
        return [{"time": t0 + i * 10800, "kp": kp, "kind": "predicted", "g": SW.g_of_kp(kp)} for i in range(n)]

    def test_london_honest_sentences(self):
        noon = A.iso_time("2026-11-23T12:00:00Z")
        quiet = A.aurora_here(51.5, -0.13, noon, grid=GRID, kp_slots=self.slots(3.0, noon), kp_now=1.0, weather=None)
        self.assertTrue(quiet["relevant"])
        self.assertEqual(quiet["chance"], "unlikely")
        self.assertAlmostEqual(quiet["kp_needed_horizon"], 7.9, delta=0.3)
        self.assertIn("Unlikely tonight", quiet["sentence"])
        self.assertIn("Kp 8", quiet["sentence"])
        stormy = A.aurora_here(51.5, -0.13, noon, grid=GRID, kp_slots=self.slots(8.33, noon), kp_now=6.0,
                               weather={"hourly": [{"time": noon + 3600 * k, "cloud_pct": 90} for k in range(24)],
                                        "utc_offset_s": 0, "timezone": "Europe/London"})
        self.assertEqual(stormy["chance"], "possible")
        self.assertIn("low on the northern horizon tonight", stormy["sentence"])
        self.assertIn("will probably hide it", stormy["sentence"])
        self.assertEqual(stormy["cloud"]["pct"], 90)

    def test_under_the_oval_and_far_away(self):
        night = A.iso_time("2026-11-23T21:00:00Z")
        trom = A.aurora_here(69.65, 18.96, night, grid=GRID, kp_slots=self.slots(2.0, night), kp_now=2.0, weather=None)
        self.assertTrue(trom["dark"]["dark_now"])
        self.assertGreaterEqual(trom["ovation"]["here_pct"], 10)
        self.assertEqual(trom["chance"], "good")
        self.assertTrue(trom["sentence"].startswith("Aurora may be overhead right now"))
        paris = A.aurora_here(48.86, 2.35, night, grid=GRID, kp_slots=self.slots(3.0, night), kp_now=2.0, weather=None)
        self.assertFalse(paris["relevant"])
        self.assertIn("extreme storm", paris["sentence"])
        quito = A.aurora_here(-0.2, -78.5, night, grid=GRID, kp_slots=[], kp_now=None, weather=None)
        self.assertFalse(quito["relevant"])

    def test_morning_looks_ahead_to_tonight(self):
        dawn = A.iso_time("2026-11-24T05:30:00Z")                # London, dark until about 06:13
        res = A.aurora_here(51.5, -0.13, dawn, grid=None, kp_slots=self.slots(2.0, dawn), kp_now=1.0, weather=None)
        self.assertTrue(res["dark"]["dark_now"])
        self.assertFalse(res["night"]["current"])
        self.assertGreater(res["night"]["start"], A.iso_time("2026-11-24T16:00:00Z"))
        self.assertIn("tonight", res["sentence"])

    def test_no_darkness(self):
        t = A.iso_time("2026-06-21T22:00:00Z")
        res = A.aurora_here(69.65, 18.96, t, grid=GRID, kp_slots=self.slots(5.0, t), kp_now=5.0, weather=None)
        self.assertEqual(res["chance"], "none")
        self.assertIn("does not get dark enough", res["sentence"])


# ---------------------------------------------------------------------------------------------
# The module end to end through a fake network
# ---------------------------------------------------------------------------------------------
class FakeNet:
    PATHS = {
        "/products/noaa-scales.json": "noaa-scales.json",
        "/json/planetary_k_index_1m.json": "planetary_k_index_1m.json",
        "/products/noaa-planetary-k-index.json": "noaa-planetary-k-index.json",
        "/products/noaa-planetary-k-index-forecast.json": "noaa-planetary-k-index-forecast.json",
        "/json/goes/primary/xray-flares-7-day.json": "xray-flares-7-day.json",
        "/json/goes/primary/xray-flares-latest.json": "xray-flares-latest.json",
        "/products/geospace/propagated-solar-wind-1-hour.json": "propagated-solar-wind-1-hour.json",
        "/products/summary/solar-wind-speed.json": "solar-wind-speed.json",
        "/products/summary/solar-wind-mag-field.json": "solar-wind-mag-field.json",
        "/products/alerts.json": "alerts.json",
    }
    STORM = {"/products/alerts.json": "alerts_storm.json", "/products/noaa-scales.json": "noaa-scales-storm.json",
             "/products/noaa-planetary-k-index.json": "noaa-planetary-k-index-storm.json",
             "/json/goes/primary/xray-flares-7-day.json": "xray-flares-storm.json"}

    def __init__(self):
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.storm = False
        self.lock = threading.Lock()
        self.ovation = json.dumps(ovation_json()).encode()

    def set_host_interval(self, host, seconds):
        pass

    def _body(self, url: str, params=None) -> bytes:
        with self.lock:
            self.calls.append(url)
        for key in self.fail:
            if key in url:
                raise NetError(f"{key}: HTTP 503", status=503)
        if "services.swpc.noaa.gov" in url:
            path = url.split("services.swpc.noaa.gov", 1)[1]
            if path == "/json/ovation_aurora_latest.json":
                return self.ovation
            name = (self.STORM.get(path) if self.storm else None) or self.PATHS.get(path)
            if name:
                return (FIX / name).read_bytes()
            raise NetError("HTTP 404", status=404)
        if "open-meteo.com" in url:
            return (FIX.parent / "openmeteo_london.json").read_bytes()
        raise NetError("HTTP 404", status=404)

    def request(self, url, *, params=None, accept=(200,), **kw):
        return NetResponse(200, {}, self._body(url, params), url)

    def get_bytes(self, url, **kw):
        return self._body(url, kw.get("params"))

    def get_json(self, url, **kw):
        return json.loads(self.get_bytes(url, **kw))

    def get_text(self, url, **kw):
        return self.get_bytes(url, **kw).decode("utf-8")

    def download(self, url, dest, **kw):
        raise NetError("HTTP 404", status=404)


class ModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MERIDIAN_DATA_DIR"] = cls.tmp.name
        log = logging.getLogger("meridian.test.spaceweather")
        cls.net = FakeNet()
        app = App(Config(None), Store(Path(cls.tmp.name) / "t.db"), cls.net, log, Scheduler(log), DatasetManager(log))
        from meridian.core_api import register_core
        from meridian.modules import load_all
        from meridian.modules import space
        register_core(app)
        load_all(app, {"space"})
        assert app.modules["space"].ok, app.modules["space"].error
        cls.app = app
        cls.svc = space._STATE["spaceweather"]
        cls.m = app.contexts["space"]

    @classmethod
    def tearDownClass(cls):
        cls.app.store.close()
        os.environ.pop("MERIDIAN_DATA_DIR", None)
        try:
            cls.tmp.cleanup()
        except OSError:
            pass

    def setUp(self):
        self.net.storm = False
        self.net.fail.clear()
        for f in self.svc.feeds.values():          # every test starts cold
            f.data, f.fetched_at, f.error, f.fails, f.retry_at = None, 0.0, None, 0, 0.0
        self.svc._snap = None
        for key in (SW.PANEL_KEY, SW.AURORA_KEY):
            self.app._activity.pop(key, None)

    def call(self, path, **query):
        route, params, _ = self.app.match("GET", path)
        self.assertIsNotNone(route, path)
        return route.handler(Request("GET", path, params, {k: [str(v)] for k, v in query.items()}, {}))

    def get(self, path, **query):
        out = self.call(path, **query)
        return json.loads(out.body) if isinstance(out, Response) and "json" in out.content_type else json.loads(json.dumps(out))

    def status(self, path, **query):
        try:
            self.call(path, **query)
        except HTTPError as err:
            return err.status
        return 200

    def test_now_snapshot(self):
        self.assertFalse(self.m.active(SW.PANEL_KEY, 300))
        with mock.patch("time.time", return_value=NOW):
            d = self.get("/api/space/spaceweather")
            self.assertTrue(self.m.active(SW.PANEL_KEY, 300))             # the panel's own activity key
            self.assertFalse(self.m.active(SW.AURORA_KEY, 600))           # ...which does not poll OVATION
        self.assertTrue(d["summary"].startswith("Quiet geomagnetic field"))
        self.assertEqual(d["kp"]["now"]["source"], "estimated each minute")
        self.assertGreater(len(d["kp"]["series"]), 20)
        self.assertEqual(d["scales"]["now"]["G"]["level"], 0)
        self.assertEqual(d["xray"]["now_class"], "B1.8")
        self.assertIsNotNone(d["wind"]["speed"])
        self.assertEqual(d["alerts"]["active"], [])
        self.assertTrue(all(v["ok"] for k, v in d["feeds"].items() if k != "ovation"))
        self.assertTrue(all(l["href"].startswith("https://www.spaceweather.gov/") for l in d["links"]))
        # within the TTLs a second request makes no new upstream calls
        n = len(self.net.calls)
        with mock.patch("time.time", return_value=NOW + 30):
            self.get("/api/space/spaceweather")
        self.assertEqual(len(self.net.calls), n)

    def test_failures_are_soft_and_back_off(self):
        self.net.fail.add("propagated-solar-wind")
        self.net.fail.add("xray-flares")
        with mock.patch("time.time", return_value=NOW):
            d = self.get("/api/space/spaceweather")
        self.assertEqual(d["wind"]["source"], "summary")                   # fell back to the summaries
        self.assertIsNone(d["xray"])
        self.assertFalse(d["feeds"]["flares"]["ok"])
        n = len(self.net.calls)
        with mock.patch("time.time", return_value=NOW + 30):              # backing off: no retry yet
            self.get("/api/space/spaceweather")
        self.assertEqual(len([c for c in self.net.calls[n:] if "xray-flares" in c]), 0)
        src = self.app.sources["space.swpc"]
        self.assertIn("flares", src["error"])
        self.net.fail.add("services.swpc.noaa.gov")
        self.setUp()
        self.net.fail.add("services.swpc.noaa.gov")
        self.assertEqual(self.status("/api/space/spaceweather"), 502)

    def test_aurora_routes(self):
        meta = self.get("/api/space/aurora")
        self.assertTrue(self.m.active(SW.AURORA_KEY, 600))
        self.assertEqual(meta["north_max"], 40)
        self.assertEqual(len(meta["lines"]["features"]), 4)
        self.assertIn("{z}/{x}/{y}.png?v=", meta["tiles"])
        tile = self.call("/api/space/aurora/tiles/2/1/1.png", v=meta["version"])
        self.assertEqual(tile.content_type, "image/png")
        self.assertEqual(tile.cache, "max-age=600")
        self.assertEqual(png_rows(tile.body)[:3], (256, 256, 3))
        self.assertEqual(self.call("/api/space/aurora/tiles/2/1/1.png", v="old").cache, "no-cache")
        for bad in ("/api/space/aurora/tiles/2/9/0.png", "/api/space/aurora/tiles/x/0/0.png", "/api/space/aurora/tiles/9/0/0.png"):
            self.assertEqual(self.status(bad), 400, bad)
        n = len([c for c in self.net.calls if "ovation" in c])
        self.get("/api/space/aurora")
        self.assertEqual(len([c for c in self.net.calls if "ovation" in c]), n)     # cached for 10 minutes

    def test_aurora_here_route(self):
        with mock.patch("time.time", return_value=NOW):
            h = self.get("/api/space/aurora/here", lat=51.5, lon=-0.13)
        self.assertTrue(h["relevant"])
        self.assertTrue(h["sentence"])
        self.assertEqual(h["sources"], {"ovation": True, "kp_forecast": True, "weather": True})
        self.assertIsNotNone(h["utc_offset_s"])
        self.assertEqual(self.status("/api/space/aurora/here", lat=95, lon=0), 400)

    def test_events_and_event_route(self):
        self.net.storm = True
        with mock.patch("time.time", return_value=STORM_NOW):
            self.svc.events(0, wait=True)
            ev = self.app.collect_events(STORM_NOW - 48 * 3600)
            mine = [e for e in ev if e.get("module") == "space" and e["kind"] == "space_weather"]
            self.assertEqual({e["type"] for e in mine}, {"geomagnetic_storm", "solar_flare", "radiation_storm", "storm_watch"})
            one = self.get("/api/space/spaceweather/event/swpc:flare:20261119T1305")
            self.assertEqual(one["scale"], "R3")
        for bad in ("not-an-id", "swpc:flare:2026;drop", "swpc:x:1"):
            self.assertEqual(self.status(f"/api/space/spaceweather/event/{bad}"), 400, bad)
        with mock.patch("time.time", return_value=STORM_NOW):
            self.assertEqual(self.status("/api/space/spaceweather/event/swpc:flare:20201119T1305"), 404)

    def test_events_never_block(self):
        calls = len(self.net.calls)
        self.assertEqual(self.svc.events(0), [])                           # cold: refresh in the background
        self.svc._bg.join(10)
        self.assertGreater(len(self.net.calls), calls)
        self.assertFalse(any("ovation" in c or "solar-wind" in c for c in self.net.calls[calls:]))

    def test_ai_tool(self):
        with mock.patch("time.time", return_value=NOW):
            out = self.app.call_ai_tool("space_weather_now", {})
            self.assertIn("summary", out)
            self.assertEqual(out["noaa_scales"]["now"], {"G": 0, "S": 0, "R": 0})
            self.assertEqual(len(out["noaa_scales"]["forecast"]), 3)
            here = self.app.call_ai_tool("space_weather_now", {"lat": 64.84, "lon": -147.72})
        self.assertIn("aurora_here", here)
        self.assertTrue(here["aurora_here"]["sentence"])
        self.assertIn("error", self.app.call_ai_tool("space_weather_now", {"lat": 10}))
        self.assertIn("error", self.app.call_ai_tool("space_weather_now", {"lat": 100, "lon": 0}))
        tool = next(t for t in self.app.ai_tools() if t["name"] == "space_weather_now")
        self.assertEqual(tool["module"], "space")

    def test_search(self):
        hits = self.app.run_search("aurora", 10)
        ids = [h["id"] for h in hits if h["kind"] == "space_weather"]
        self.assertEqual(ids[:2], ["swpc:aurora:forecast", "swpc:now:panel"])
        self.assertEqual([h["id"] for h in self.app.run_search("kp", 5) if h["kind"] == "space_weather"], ["swpc:now:panel"])
        self.assertEqual([h for h in self.app.run_search("fl", 5) if h["kind"] == "space_weather"], [])
        self.assertEqual([h for h in self.app.run_search("london", 5) if h["kind"] == "space_weather"], [])

    def test_sources_and_layers(self):
        for sid in ("space.swpc", "space.ovation"):
            self.assertIn("Public domain", self.app.sources[sid]["licence"])
        lids = {l["id"] for l in self.app.layer_specs if l["module"] == "space"}
        self.assertTrue({"space.aurora", "space.spaceweather"} <= lids)


if __name__ == "__main__":
    unittest.main()
