"""Tests for the briefing module: section building from synthetic history, trend maths, regions,
scheduling with a fake clock, storage, Markdown export, the narrative prompt, gathering and the HTTP
guards. No network.

Run: python -m unittest tests.test_briefing -v
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from meridian.config import Config
from meridian.core_api import register_core
from meridian.datasets import DatasetManager
from meridian.history import EventHistory
from meridian.httpserver import make_server
from meridian.modules import load_all
from meridian.modules.ai import helper
from meridian.modules.briefing import build as B
from meridian.modules.briefing import gather as G
from meridian.modules.briefing import markdown as MD
from meridian.modules.briefing import narrative as N
from meridian.modules.briefing import schedule as S
from meridian.modules.briefing import trends as T
from meridian.modules.briefing.regions import (CountryIndex, Regionizer, coarse_region, ocean_region,
                                               region_of_country)
from meridian.modules.briefing.service import BriefingService, Busy
from meridian.modules.briefing.store import BriefingStore, valid_id
from meridian.net import Net
from meridian.registry import App, Response
from meridian.scheduler import Scheduler
from meridian.store import Store

FIX = Path(__file__).resolve().parent / "fixtures" / "briefing"
LOG = logging.getLogger("meridian.test.briefing")
logging.getLogger("meridian").addHandler(logging.NullHandler())
H = 3600.0
DAY = 86400.0
NOW = float(calendar.timegm((2026, 11, 23, 8, 0, 0)))        # Monday 23 Nov 2026, 08:00 UTC

# places inside the fixture countries
GB, UA, SD, EG, MM, US = (52.0, -1.0), (49.0, 31.0), (15.0, 30.0), (26.0, 30.0), (21.0, 96.0), (39.0, -98.0)


def ev(kind, eid, *, module="intel", t, sev=5.0, at=(0.0, 0.0), first_time=None, **extra):
    d = {"kind": kind, "id": eid, "module": module, "title": extra.pop("title", f"{kind} {eid}"),
         "subtitle": extra.pop("subtitle", "a test event"), "lat": at[0], "lon": at[1], "time": t, "severity": sev,
         "layer": extra.pop("layer", f"{module}.{kind}")}
    if first_time is not None:
        d["first_time"] = first_time
    d.update(extra)
    return d


def conflicts(n, at, t0, t1, prefix):
    return [ev("conflict_event", f"{prefix}{i}", t=t0 + (t1 - t0) * (i + 0.5) / n, sev=6.0, at=at,
               title=f"Air strike {prefix} {i}") for i in range(n)]


class Feed:
    def __init__(self):
        self.items = []

    def __call__(self, since):
        return [dict(i) for i in self.items]


def index():
    return CountryIndex(FIX / "countries.geojson") if (FIX / "countries.geojson").exists() else None


def make_builder(since=NOW - DAY, until=NOW, now=NOW, idx=None):
    idx = idx or index()
    idx.ensure()
    rz = Regionizer(idx)
    return B.Builder(since=since, until=until, now=now, place=rz.place, country_entity=rz.country_entity,
                     countries_ready=idx.ready)


def build_doc(cur, prev=(), extras=None, previous=None, **kw):
    b = make_builder(**kw)
    return b.build(list(cur), list(prev), extras or {}, previous, bid="20261123T080000Z", trigger="manual",
                   basis="24h", local_date="2026-11-23")


def section(doc, sid):
    return next(s for s in doc["sections"] if s["id"] == sid)


# ---------------------------------------------------------------------------------------------
# Trend maths
# ---------------------------------------------------------------------------------------------
class TrendMathTests(unittest.TestCase):
    def test_poisson_tails_match_closed_forms(self):
        e2 = 2.718281828459045 ** -2
        self.assertAlmostEqual(T.poisson_cdf(1, 2.0), e2 * 3, places=9)
        self.assertAlmostEqual(T.poisson_sf(5, 2.0), 1 - e2 * (1 + 2 + 2 + 4 / 3 + 2 / 3), places=9)
        for lam in (0.5, 3.0, 40.0, 900.0):
            for k in (0, 1, int(lam), int(lam * 2) + 1):
                self.assertAlmostEqual(T.poisson_cdf(k - 1, lam) + T.poisson_sf(k, lam), 1.0, places=9)
        self.assertLess(T.poisson_sf(3000, 900.0), 1e-100)       # far tails stay finite
        self.assertEqual(T.poisson_sf(0, 5.0), 1.0)
        self.assertEqual(T.poisson_cdf(-1, 5.0), 0.0)

    def test_compare_directions_and_notability(self):
        self.assertEqual(T.compare(0, 0)["direction"], "none")
        self.assertEqual(T.compare(7, 0)["direction"], "new")
        self.assertEqual(T.compare(0, 9)["direction"], "gone")
        steady = T.compare(105, 100)
        self.assertEqual((steady["direction"], steady["notable"], steady["pct"]), ("steady", False, 5.0))
        up = T.compare(40, 10)
        self.assertEqual((up["direction"], up["notable"], up["pct"]), ("up", True, 300.0))
        down = T.compare(10, 60)
        self.assertEqual((down["direction"], down["notable"]), ("down", True))
        # small counts are not notable even when the ratio is large
        self.assertFalse(T.compare(4, 1)["notable"])
        self.assertFalse(T.compare(9, 3)["notable"])
        # the overall volume doubled: a country that doubled too is not notable
        self.assertFalse(T.compare(200, 100, scale=2.0)["notable"])
        self.assertTrue(T.compare(200, 100, scale=1.0)["notable"])
        # a steady raw count is never flagged, whatever the scaled expectation says
        self.assertFalse(T.compare(32, 30, scale=2.0)["notable"])

    def test_words(self):
        self.assertEqual(T.words(T.compare(54, 31)), "up from 31 the previous day (+74%)")
        self.assertEqual(T.words(T.compare(10, 40)), "down from 40 the previous day (-75%)")
        self.assertEqual(T.words(T.compare(101, 100)), "about the same as the previous day (100)")
        self.assertEqual(T.words(T.compare(3, 0), prev_label="the previous 12 hours"), "none the previous 12 hours")
        self.assertEqual(T.words(T.compare(0, 2)), "down from 2 the previous day")

    def test_rank_countries_top_and_notable_extras(self):
        cur = {"AA": 100, "BB": 90, "CC": 80, "DD": 40, "EE": 12, "FF": 2}
        prev = {"AA": 95, "BB": 88, "CC": 82, "DD": 5, "EE": 11, "FF": 1}
        rows = T.rank_countries(cur, prev, top=3, extra=2)
        self.assertEqual([c for c, _ in rows], ["AA", "BB", "CC", "DD"])     # DD rose sharply; EE and FF did not
        self.assertTrue(rows[-1][1]["notable"])
        self.assertEqual(T.volume_scale(10, 5), 1.0)                         # too small to scale
        self.assertEqual(T.volume_scale(200, 100), 2.0)


# ---------------------------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------------------------
class RegionTests(unittest.TestCase):
    def test_country_regions(self):
        self.assertEqual(region_of_country("RU", 55.7, 37.6), "europe")
        self.assertEqual(region_of_country("RU", 43.1, 131.9), "apac")
        self.assertEqual(region_of_country("RU", 65.0, -175.0), "apac")         # Chukotka across 180°
        cases = {"TR": "europe", "GE": "europe", "SD": "ssa", "EG": "mena", "IR": "mena", "KZ": "apac",
                 "GL": "americas", "PS": "mena", "XK": "europe", "AU": "apac", "MX": "americas", "DJ": "ssa"}
        for code, rid in cases.items():
            self.assertEqual(region_of_country(code, 0, 0), rid, code)
        self.assertIsNone(region_of_country("ZZ", 0, 0))
        self.assertIsNone(region_of_country(None, 0, 0))

    def test_ocean_and_coarse_rules(self):
        self.assertEqual(ocean_region(33.5, 15.0), "mena")        # off Libya
        self.assertEqual(ocean_region(40.0, 5.0), "europe")       # western Mediterranean
        self.assertEqual(ocean_region(56.0, 3.0), "europe")       # North Sea
        self.assertEqual(ocean_region(15.0, 40.0), "mena")        # Red Sea
        self.assertEqual(ocean_region(0.0, 3.0), "ssa")           # Gulf of Guinea
        self.assertEqual(ocean_region(-20.0, 70.0), "apac")       # Indian Ocean, east
        self.assertEqual(ocean_region(-10.0, -170.0), "apac")     # South Pacific
        self.assertEqual(ocean_region(15.0, -75.0), "americas")   # Caribbean
        self.assertEqual(ocean_region(80.0, 10.0), "europe")      # Arctic north of Norway
        self.assertEqual(coarse_region(51.5, -0.1), "europe")
        self.assertEqual(coarse_region(15.5, 32.5), "ssa")
        self.assertEqual(coarse_region(30.0, 31.0), "mena")
        self.assertEqual(coarse_region(35.0, 139.0), "apac")
        self.assertEqual(coarse_region(40.0, -74.0), "americas")

    def test_country_index_lookups(self):
        idx = index()
        self.assertTrue(idx.ensure())
        rz = Regionizer(idx)
        self.assertEqual(rz.place(*GB), ("europe", {"iso2": "GB", "name": "United Kingdom"}))
        self.assertEqual(rz.place(*SD)[0], "ssa")
        self.assertEqual(rz.place(*EG)[0], "mena")
        self.assertEqual(rz.place(*MM)[0], "apac")
        self.assertEqual(rz.place(*US)[0], "americas")
        self.assertEqual(rz.place(55.0, 100.0), ("apac", {"iso2": "RU", "name": "Russia"}))
        self.assertEqual(idx.lookup(41.9, 12.45)["iso2"], "VA")                  # the smallest shape wins
        self.assertEqual(idx.lookup(42.5, 12.0)["iso2"], "IT")
        self.assertEqual(idx.lookup(-29.5, 28.0)["iso2"], "LS")                 # inside the hole of ZA
        self.assertEqual(idx.lookup(-25.0, 20.0)["iso2"], "ZA")
        # just off a coast: that country; 100 km out: only the region; far out: the ocean rule
        self.assertEqual(rz.place(52.0, 2.2)[1]["iso2"], "GB")
        self.assertEqual(rz.place(52.0, 3.5), ("europe", None))
        self.assertEqual(rz.place(-10.0, -140.0), ("apac", None))
        # Crimea: drawn inside Russia, attributed to neither country
        self.assertEqual(rz.place(44.95, 34.1), ("europe", None))
        self.assertEqual(rz.place(48.0, 35.0)[1]["iso2"], "UA")
        ent = rz.country_entity("SD")
        self.assertEqual((ent["kind"], ent["id"], ent["layer"], ent["lat"], ent["lon"]), ("country", "SD", "intel.countries", 15, 30))
        self.assertEqual(rz.place("x", None), ("global", None))

    def test_missing_shapes_fall_back_to_boxes(self):
        idx = CountryIndex(Path(tempfile.gettempdir()) / "no-such-countries-file.geojson")
        self.assertFalse(idx.ensure())
        rz = Regionizer(idx)
        self.assertEqual(rz.place(51.5, -0.1), ("europe", None))
        self.assertEqual(rz.place(15.5, 32.5), ("ssa", None))


# ---------------------------------------------------------------------------------------------
# Building sections from synthetic history
# ---------------------------------------------------------------------------------------------
class HistoryBuildTests(unittest.TestCase):
    """Events go through the real EventHistory (snapshots at fake times), then into the builder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.feed = Feed()
        self.history = EventHistory(self.store, self.feed, log=LOG, clock=lambda: NOW)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def windows(self, since=NOW - DAY, until=NOW):
        cur = self.history.events(since, until, limit=20000)
        prev = self.history.events(since - (until - since), since, limit=20000)
        return cur, prev

    def seed_two_days(self):
        prev = (conflicts(5, SD, NOW - 2 * DAY, NOW - DAY, "sd-old") + conflicts(30, UA, NOW - 2 * DAY, NOW - DAY, "ua-old")
                + conflicts(30, US, NOW - 2 * DAY, NOW - DAY, "us-old"))
        storm_old = ev("storm", "gdacs:TC:1", module="hazards", t=NOW - 30 * H, sev=7, at=(-12.0, 170.0),
                       title="Tropical cyclone Old", source="GDACS (JTWC)")
        self.feed.items = prev + [storm_old]
        self.history.snapshot(now=NOW - 47 * H)               # the history started before the previous day
        cur = (conflicts(40, SD, NOW - DAY, NOW, "sd") + conflicts(32, UA, NOW - DAY, NOW, "ua")
               + conflicts(28, US, NOW - DAY, NOW, "us"))
        storm_old = dict(storm_old, time=NOW - 1 * H)
        storm_new = ev("storm", "gdacs:TC:2", module="hazards", t=NOW - 2 * H, sev=8, at=(15.0, 130.0),
                       title="Tropical cyclone New", subtitle="Category 4, 230 km/h", source="GDACS (JTWC)")
        quake = ev("earthquake", "usgs:1", module="hazards", t=NOW - 5 * H, sev=8, at=MM, title="M 6.6, Myanmar",
                   magnitude=6.6, source="USGS", tsunami=False)
        news = [ev("news", "n1", t=NOW - 3 * H, sev=9, at=EG, title="Big Story In Cairo"),
                ev("news", "n2", t=NOW - 2 * H, sev=9, at=(30.0, 31.0), title="big story in cairo")]
        ship = ev("ship", "273000001", module="ships", t=NOW - 1 * H, sev=6, at=(58.0, 0.5), category="sanctioned_vessel",
                  title="Sanctioned vessel seen: TEST", layer="ships.sanctioned")
        self.feed.items = cur + [storm_old, storm_new, quake, ship] + news
        self.history.snapshot(now=NOW)

    def test_regions_trends_and_items(self):
        self.seed_two_days()
        cur, prev = self.windows()
        doc = build_doc(cur, prev)
        ids = [s["id"] for s in doc["sections"]]
        self.assertEqual(ids, ["headline", "europe", "mena", "ssa", "apac", "americas", "space", "cyber", "markets"])
        for s in doc["sections"]:
            self.assertIn("region", s)
            self.assertIsInstance(s["items"], list)
        ssa = section(doc, "ssa")
        sudan = next(t for t in ssa["trends"] if t["metric"] == "conflict_reports")
        self.assertEqual((sudan["title"], sudan["value"], sudan["previous"]), ("Sudan", 40, 5))
        self.assertTrue(sudan["notable"])
        self.assertTrue(sudan["unverified"])
        self.assertEqual(sudan["entity"]["kind"], "country")
        self.assertIn("40 conflict reports, up from 5 the previous day", sudan["line"])
        ukraine = next(t for t in section(doc, "europe")["trends"] if t["title"] == "Ukraine")
        self.assertFalse(ukraine["notable"])                    # 30 -> 32 is ordinary
        self.assertIn("about the same", ukraine["line"])
        # a section never shows more than three conflict events, all labelled unverified
        europe_items = section(doc, "europe")["items"]
        conflict_items = [i for i in europe_items if i["kind"] == "conflict_event"]
        self.assertLessEqual(len(conflict_items), 3)
        self.assertTrue(all(i["unverified"] for i in conflict_items))
        # news with the same headline in two places is one item
        news = [i for i in section(doc, "mena")["items"] if i["kind"] == "news"]
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["count"], 2)
        self.assertIn("2 similar reports", news[0]["line"])
        # the new storm is new; the storm that was already active is not
        storms = {i["title"]: i for i in section(doc, "apac")["items"] if i["kind"] == "storm"}
        self.assertTrue(storms["Tropical cyclone New"].get("new"))
        self.assertFalse(storms["Tropical cyclone Old"].get("new"))
        self.assertEqual(storms["Tropical cyclone New"]["source"], "GDACS (JTWC)")
        # entities open the right thing on the map
        q = next(i for i in section(doc, "apac")["items"] if i["kind"] == "earthquake")
        self.assertEqual((q["entity"]["kind"], q["entity"]["id"], q["entity"]["layer"]), ("earthquake", "usgs:1", "hazards.earthquake"))
        self.assertNotIn("unverified", q)
        quakes = next(t for t in section(doc, "apac")["trends"] if t["metric"] == "quakes_m5")
        self.assertEqual(quakes["value"], 1)
        self.assertTrue(doc["coverage"]["full"])
        self.assertEqual(doc["coverage"]["notes"], [])
        self.assertEqual(doc["metrics"]["conflict_total"], 100)

    def test_headline_mixes_sources_and_caps_machine_coded_news(self):
        self.seed_two_days()
        cur, prev = self.windows()
        head = section(build_doc(cur, prev), "headline")
        items = head["items"]
        self.assertTrue(3 <= len(items) <= 5, items)
        self.assertLessEqual(sum(1 for i in items if i.get("unverified")), 2)
        self.assertEqual(len({i["title"] for i in items}), len(items))
        self.assertTrue(all(i.get("region") for i in items))
        kinds = [i["kind"] for i in items]
        self.assertIn("storm", kinds)
        self.assertIn("earthquake", kinds)

    def test_young_history_leaves_out_comparisons_it_cannot_make(self):
        ship = ev("ship", "273000002", module="ships", t=NOW - 1 * H, sev=6, at=(58.0, 0.5), category="dark_ship",
                  title="Dark ship: TEST", layer="ships.dark")
        self.feed.items = conflicts(20, SD, NOW - 2 * DAY + H, NOW - DAY, "old") + conflicts(25, SD, NOW - DAY, NOW, "new") + [ship]
        self.history.snapshot(now=NOW)                         # one snapshot: the history is minutes old
        cur, prev = self.windows()
        doc = build_doc(cur, prev)
        self.assertFalse(doc["coverage"]["full"])
        self.assertTrue(any("starts at" in n for n in doc["coverage"]["notes"]))
        dark = next(t for t in section(doc, "europe")["trends"] if t["metric"] == "dark_ships")
        self.assertIsNone(dark["change"])
        self.assertIn("no comparison yet", dark["line"])
        # conflict events reach back into the previous day, so that comparison is still made
        sudan = next(t for t in section(doc, "ssa")["trends"] if t["metric"] == "conflict_reports")
        self.assertEqual((sudan["value"], sudan["previous"]), (25, 20))
        # nothing can be "new" when the history did not run before the period
        self.assertFalse(any(i.get("new") for s in doc["sections"] for i in s["items"]))

    def test_quiet_period(self):
        doc = build_doc([], [])
        head = section(doc, "headline")
        self.assertEqual(len(head["items"]), 1)
        self.assertEqual(head["items"][0]["title"], "A quiet period")
        self.assertIn("A quiet period", doc["text"])
        self.assertEqual(section(doc, "europe")["note"], "Nothing notable recorded here in this period.")
        self.assertEqual(doc["stats"]["events"], 0)

    def test_deterministic_text(self):
        self.seed_two_days()
        cur, prev = self.windows()
        doc = build_doc(cur, prev)
        text = doc["text"]
        self.assertTrue(text.startswith("Briefing for 22 Nov 08:00 to 23 Nov 08:00 UTC (last 24 hours)."))
        self.assertIn("Headline: ", text)
        self.assertIn("Sub-Saharan Africa: ", text)
        self.assertIn(B.UNVERIFIED_NOTE, text)
        self.assertEqual(doc, json.loads(json.dumps(doc)))       # plain JSON all the way down


class GlobalSectionTests(unittest.TestCase):
    SPACE = {"summary": "Quiet geomagnetic field, Kp 2 now.", "kp": {"now": 2.0, "last_3h": 2.3, "max_24h": 5.33},
             "noaa_scales": {"now": {"G": 0, "S": 0, "R": 0}, "max_24h": {"G": 1, "S": 0, "R": 1}},
             "xray": {"recent": [{"class": "C4.0", "peak_utc": "2026-11-23T05:00:00+00:00"},
                                 {"class": "M2.1", "peak_utc": "2026-11-23T03:12:00+00:00"},
                                 {"class": "X1.0", "peak_utc": "2026-11-20T03:00:00+00:00"}]},
             "aurora": {"north_max_pct": 40, "south_max_pct": 12}}
    CYBER = {"type": "FeatureCollection", "meta": {"outages_at": {"ioda": NOW - 600}}, "features": [
        {"type": "Feature", "properties": {"role": "c2", "iso2": "US", "name": "United States", "count": 7, "online": 2},
         "geometry": {"type": "Point", "coordinates": [-98, 39]}},
        {"type": "Feature", "properties": {"role": "c2", "iso2": "DE", "name": "Germany", "count": 3, "online": 0},
         "geometry": {"type": "Point", "coordinates": [10, 51]}},
        {"type": "Feature", "properties": {"role": "outage", "iso2": "IR", "name": "Iran", "sev": 7.5, "events": 4, "src": "IODA"},
         "geometry": {"type": "Point", "coordinates": [53, 32]}}]}
    MARKETS = [
        {"id": "brent", "name": "Brent crude", "available": True, "price": 70.0, "currency": "USD", "unit": "per barrel",
         "change_pct": -3.1, "delay": "end of day", "as_of": "2026-11-21", "time": NOW - 2 * DAY, "source": "FRED"},
        {"id": "ttf", "name": "European natural gas", "available": True, "price": 30.0, "currency": "USD",
         "unit": "per million BTU", "change_pct": 12.0, "delay": "monthly average", "as_of": "October 2026", "source": "World Bank"},
        {"id": "spx", "name": "S&P 500", "available": True, "price": 6900.0, "unit": "index", "change_pct": -4.0,
         "delay": "end of day", "time": NOW - 9 * DAY, "source": "FRED",
         "fly": {"lat": 40.7, "lon": -74.0, "zoom": 13, "label": "New York Stock Exchange"}},
        {"id": "fx_gbpusd", "name": "GBP/USD", "available": True, "price": 1.30744, "change_pct": 0.01, "delay": "daily reference rate",
         "time": NOW - H, "source": "ECB"},
        {"id": "btc", "name": "Bitcoin", "available": False, "reason": "not answering"}]
    GRID = {"available": True, "carbon_intensity": {"g_co2_per_kwh": 180, "index": "moderate"}, "renewable_percent": 41.0,
            "demand_mw": 31000, "price": {"gbp_per_mwh": 85.0}}

    def test_space_cyber_markets_and_grid(self):
        yesterday = {"time": NOW - DAY, "metrics": {"kp_max_24h": 3.0, "c2_listed": 6, "grid_gb": {"intensity": 240}}}
        doc = build_doc([], [], {"space": self.SPACE, "cyber": self.CYBER, "markets": self.MARKETS, "grid": self.GRID},
                        previous=yesterday)
        space = section(doc, "space")
        kp = next(t for t in space["trends"] if t["metric"] == "kp")
        self.assertIn("up to 5.3 in the past day", kp["line"])
        self.assertIn("yesterday's briefing had Kp up to 3", kp["line"])
        self.assertEqual(kp["entity"]["id"], "swpc:now:panel")
        flares = next(t for t in space["trends"] if t["metric"] == "flares")
        self.assertEqual(flares["value"], 1)                      # M2.1 in the period; C-class and old X left out
        self.assertIn("M2.1", flares["line"])
        cyber = section(doc, "cyber")
        out = cyber["items"][0]
        self.assertEqual((out["entity"]["kind"], out["entity"]["id"], out["severity"]), ("outage", "ioda:IR", 7.5))
        c2 = cyber["trends"][0]
        self.assertEqual(c2["value"], 10)
        self.assertIn("most in United States (7)", c2["line"])
        self.assertIn("in yesterday’s briefing", c2["line"])
        markets = section(doc, "markets")
        rows = {t["metric"]: t for t in markets["trends"]}
        self.assertTrue(rows["market:brent"]["notable"])
        self.assertIn("$70.00 per barrel, down 3.1% on the previous value", rows["market:brent"]["line"])
        self.assertFalse(rows["market:ttf"]["notable"])          # a monthly average is not a move
        self.assertFalse(rows["market:spx"]["notable"])          # nine days old: not a move either
        self.assertEqual(rows["market:spx"]["entity"]["kind"], "place")
        self.assertIn("1.3074, unchanged", rows["market:fx_gbpusd"]["line"])
        self.assertNotIn("market:btc", rows)
        self.assertEqual([i["title"] for i in markets["items"]], ["Brent crude down 3.1%"])
        grid = rows["grid_gb"]
        self.assertEqual(grid["line"], "180 g CO2 per kWh (moderate), 41% renewable, demand 31.0 GW, £85 per MWh; "
                                       "240 g in yesterday’s briefing")
        self.assertEqual(grid["entity"]["layer"], "infra.grid")
        self.assertEqual(doc["metrics"]["grid_gb"], {"intensity": 180, "renewable": 41.0})
        self.assertEqual(doc["metrics"]["markets"]["brent"], 70.0)
        self.assertEqual(doc["metrics"]["c2_listed"], 10)

    def test_unavailable_live_data_reads_plainly(self):
        doc = build_doc([], [], {"errors": {"space": "did not answer in time"}})
        self.assertEqual(section(doc, "space")["summary"], "Space weather data is not available right now.")
        self.assertEqual(section(doc, "markets")["summary"], "Market and grid data is not available right now.")
        self.assertIn("Space weather: did not answer in time", doc["coverage"]["notes"])
        self.assertEqual(section(doc, "cyber")["summary"], "No cyber or internet data in this period.")
        quiet = build_doc([], [], {"cyber": {"type": "FeatureCollection", "features": [],
                                             "meta": {"outages_at": {"ioda": NOW}}}})
        self.assertEqual(section(quiet, "cyber")["summary"], "No internet outage signals in the latest data.")
        loading = build_doc([], [], {"cyber": {"type": "FeatureCollection", "features": [], "meta": {}}})
        self.assertEqual(section(loading, "cyber")["summary"], "Internet outage signals were still loading.")

    def test_your_areas(self):
        self.assertNotIn("areas", [s["id"] for s in build_doc([], [], {})["sections"]])      # no alerts module
        empty = section(build_doc([], [], {"alerts": []}), "areas")
        self.assertEqual((empty["summary"], empty["items"]), ("No alerts fired in your areas in this period.", []))
        alerts = [
            {"id": 1, "time": NOW - H, "area": "Home", "rule": "Earthquakes M4+", "title": "M 4.8 near Home", "severity": 5,
             "why": "M4.8 within 80 km", "lat": 51.5, "lon": -0.1, "entity": {"kind": "earthquake", "id": "usgs:9"}},
            {"id": 2, "time": NOW - 2 * H, "area": {"name": "Baltic"}, "rule": "Dark ships", "title": "Dark ship: X",
             "severity": 6, "entity": {"kind": "ship", "id": 273000009, "lat": 58.0, "lon": 20.0}},
            {"id": 3, "time": NOW - 3 * DAY, "area": "Old", "title": "Too old", "severity": 9},
            {"title": None}]
        sec = section(build_doc([], [], {"alerts": alerts}), "areas")
        self.assertEqual([i["title"] for i in sec["items"]], ["Dark ship: X", "M 4.8 near Home"])
        self.assertEqual(sec["items"][0]["entity"]["id"], "273000009")
        self.assertEqual(sec["items"][1]["entity"]["lat"], 51.5)             # filled from the alert row
        self.assertEqual(sec["items"][1]["line"], "Home, Earthquakes M4+: M4.8 within 80 km")
        self.assertEqual(sec["summary"], "2 alerts in 2 of your areas.")


    def test_your_areas_one_area(self):
        one = [{"time": NOW - H, "area": "Home", "title": "M 4.8 near Home", "severity": 5},
               {"time": NOW - 2 * H, "area": "Home", "title": "M 4.1 near Home", "severity": 4}]
        self.assertEqual(section(build_doc([], [], {"alerts": one[:1]}), "areas")["summary"],
                         "1 alert in one of your areas.")
        self.assertEqual(section(build_doc([], [], {"alerts": one}), "areas")["summary"],
                         "2 alerts in one of your areas.")

    def test_sanctioned_vessels_name_the_lists_not_ofac(self):
        ships = [ev("ship", "1", module="ships", t=NOW - H, at=(58.0, 0.5), category="sanctioned_vessel", lists=["UK"]),
                 ev("ship", "2", module="ships", t=NOW - 2 * H, at=(58.0, 0.6), category="sanctioned_vessel",
                    lists=["OFAC", "UK"])]
        doc = build_doc(ships, [])
        rows = [t for s in doc["sections"] for t in s.get("trends") or [] if t["title"] == "Sanctioned vessels seen"]
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("OFAC list", row["line"])
            self.assertIn("sanctions lists", row["line"])
            self.assertEqual(row["source"], "AIS with sanctions lists")
        self.assertEqual(B.source_of({"module": "ships", "category": "sanctioned_vessel", "lists": ["UK"]}),
                         "AIS with sanctions lists")
        self.assertEqual(B.source_of({"module": "ships", "category": "sanctioned_vessel"}), "AIS with sanctions lists")
        self.assertNotIn("OFAC", json.dumps(doc))

    def test_short_source_ids_get_display_names(self):
        self.assertEqual(B.source_of({"module": "intel", "kind": "advice", "source": "fcdo"}), "FCDO travel advice")
        self.assertEqual(B.source_of({"module": "intel", "kind": "advice", "source": "us"}),
                         "US State Department travel advisory")
        self.assertEqual(B.source_of({"module": "hazards", "source": "USGS"}), "USGS")
        self.assertEqual(B.source_of({"module": "hazards", "source": "us"}), "us")    # only intel's ids are mapped


# ---------------------------------------------------------------------------------------------
# Markdown and the narrative prompt
# ---------------------------------------------------------------------------------------------
def sample_doc():
    cur = [ev("conflict_event", "c1", t=NOW - H, sev=8, at=UA, title="Air strike [click](http://evil.example) <b>"),
           ev("earthquake", "q1", module="hazards", t=NOW - 2 * H, sev=7, at=MM, title="M 6.1, Myanmar", source="USGS")]
    doc = build_doc(cur, [], {"alerts": [{"time": NOW - H, "area": "Home", "title": "Something near Home", "severity": 6,
                                          "lat": 51.5, "lon": -0.12}]})
    doc["ai"] = {"text": "Unverified reports of an air strike [Europe].\n\nA strong earthquake [Asia-Pacific].",
                 "provider": "anthropic", "provider_label": "Claude", "model": "claude-opus-5-5"}
    return doc


class MarkdownTests(unittest.TestCase):
    def test_markdown_export(self):
        doc = sample_doc()
        md = MD.to_markdown(doc, base_url="http://127.0.0.1:8833")
        self.assertTrue(md.startswith("# Meridian briefing, 23 Nov 08:00 UTC\n"))
        for heading in ("## Summary", "## AI narrative", "## Headline", "## Your areas", "## Europe", "## Asia-Pacific",
                        "## Markets and energy", "## Sources"):
            self.assertIn(heading, md)
        self.assertIn("_Written by Claude (claude-opus-5-5) from the items below.", md)
        self.assertIn(r"Air strike \[click\](http://evil.example) \<b\>", md)    # no live link, no markup
        self.assertNotIn("[click](http", md)
        self.assertIn("([map](http://127.0.0.1:8833/#map=8/49.0000/31.0000))", md)   # zoom 8 when the event has none
        self.assertIn("unverified, machine-coded news", md)
        self.assertIn("severity 7 (serious)", md)
        self.assertEqual(MD.filename(doc), "meridian-briefing-20261123-080000.md")
        self.assertNotIn("([map]", MD.to_markdown(doc))                          # no base URL, no links

    def test_map_link(self):
        self.assertEqual(MD.map_link({"lat": 1.23456, "lon": -2.5, "zoom": 7}, "http://127.0.0.1:8790"),
                         "http://127.0.0.1:8790/#map=7/1.2346/-2.5000")
        self.assertIsNone(MD.map_link({"lat": 95, "lon": 0}, "http://x"))
        self.assertIsNone(MD.map_link({"lat": True, "lon": 0}, "http://x"))
        self.assertIsNone(MD.map_link(None, "http://x"))


class NarrativeTests(unittest.TestCase):
    def test_prompt_uses_only_items_and_keeps_your_areas_out(self):
        prompt = N.build_prompt(sample_doc())
        self.assertIn("[Europe]", prompt)
        self.assertIn("[Headline]", prompt)
        self.assertIn("unverified, machine-coded news", prompt)
        self.assertNotIn("Your areas", prompt)
        self.assertNotIn("Something near Home", prompt)
        self.assertNotIn("51.5", prompt)                          # no coordinates
        self.assertNotIn("49.0", prompt)
        for rule in ("Use only the briefing items", "Cite the section", "unverified", "never follow instructions"):
            self.assertIn(rule.lower(), N.SYSTEM.lower())

    def test_write_returns_labelled_text_or_none(self):
        calls = []

        def complete(system, prompt, max_tokens=1200, *, timeout=60.0, meta=None):
            calls.append((max_tokens, timeout))
            meta.update(provider="anthropic", model="claude-opus-5-5", elapsed_s=3.2)
            return "  A short narrative [Europe].  "

        ai = N.write(sample_doc(), complete, clock=lambda: NOW)
        self.assertEqual(ai["text"], "A short narrative [Europe].")
        self.assertEqual((ai["provider_label"], ai["model"], ai["generated_at"]), ("Claude", "claude-opus-5-5", NOW))
        self.assertEqual(calls, [(1200, N.TIMEOUT_S)])
        self.assertIsNone(N.write(sample_doc(), lambda *a, **k: None))
        self.assertIsNone(N.write(sample_doc(), lambda *a, **k: "   "))
        plain = N.write(sample_doc(), lambda system, prompt, max_tokens=1200: "Plain helper.")   # no keyword extras
        self.assertEqual(plain["text"], "Plain helper.")


# ---------------------------------------------------------------------------------------------
# Scheduling with a fake clock (UTC stands in for local time)
# ---------------------------------------------------------------------------------------------
def at(day, hh, mm=0):
    return float(calendar.timegm((2026, 11, day, hh, mm, 0)))


class ScheduleTests(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(S.parse_time("07:00"), (7, 0, True))
        self.assertEqual(S.parse_time(" 6.45 "), (6, 45, True))
        self.assertEqual(S.parse_time("23:59"), (23, 59, True))
        self.assertEqual(S.parse_time("25:00"), (7, 0, False))
        self.assertEqual(S.parse_time("seven"), (7, 0, False))
        self.assertEqual(S.parse_time(None), (7, 0, True))
        self.assertEqual(S.parse_time(""), (7, 0, True))

    def test_due_once_a_day_after_the_time(self):
        g = time.gmtime
        self.assertFalse(S.is_due(at(23, 6, 59), None, "07:00", g))
        self.assertTrue(S.is_due(at(23, 7, 0), None, "07:00", g))
        self.assertTrue(S.is_due(at(23, 22, 0), "2026-11-22", "07:00", g))       # started late in the day
        self.assertFalse(S.is_due(at(23, 7, 10), "2026-11-23", "07:00", g))      # already made today
        self.assertFalse(S.is_due(at(24, 6, 0), "2026-11-23", "07:00", g))
        self.assertTrue(S.is_due(at(24, 7, 5), "2026-11-23", "07:00", g))
        self.assertFalse(S.is_due(at(23, 18, 0), None, "18:30", g))
        self.assertTrue(S.is_due(at(23, 18, 30), None, "18:30", g))

    def test_next_due(self):
        g, mk = time.gmtime, calendar.timegm
        self.assertEqual(S.next_due(at(23, 6, 0), None, "07:00", g, mk), at(23, 7, 0))
        self.assertEqual(S.next_due(at(23, 9, 0), "2026-11-23", "07:00", g, mk), at(24, 7, 0))
        self.assertEqual(S.next_due(at(30, 9, 0), "2026-11-30", "07:00", g, mk), float(calendar.timegm((2026, 12, 1, 7, 0, 0))))
        self.assertEqual(S.next_due(at(23, 9, 0), None, "07:00", g, mk), at(23, 9, 0))      # due now

    def test_attempts(self):
        a = S.Attempts(limit=2)
        self.assertTrue(a.allowed("2026-11-23"))
        a.failed("2026-11-23")
        a.failed("2026-11-23")
        self.assertFalse(a.allowed("2026-11-23"))
        self.assertTrue(a.allowed("2026-11-24"))


class FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class ServiceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(None, overrides={"BRIEFING_TIME": "07:00"})
        self.app = App(cfg, Store(Path(self.tmp.name) / "t.db"), Net(cfg), LOG, Scheduler(LOG), DatasetManager(LOG))
        self.clock = FakeClock(at(23, 6, 0))
        self.feed = Feed()
        self.app._history = EventHistory(self.app.store, self.feed, log=LOG, clock=self.clock)
        self.m = self.app.module("briefing")
        self.ai_ready = False
        self.completions = []

        def complete(system, prompt, max_tokens=1200, *, timeout=60.0, meta=None):
            self.completions.append(prompt)
            if meta is not None:
                meta.update(provider="ollama", model="qwen3:8b", elapsed_s=1.0)
            return self.ai_text

        self.ai_text = "Narrative [Europe]."
        self.svc = BriefingService(self.m, clock=self.clock, localtime=time.gmtime, mktime=calendar.timegm,
                                   gather_fn=lambda since: {}, complete_fn=complete,
                                   ai_status_fn=lambda: {"ready": self.ai_ready, "provider": "ollama"},
                                   countries_path=FIX / "countries.geojson")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()


class ServiceTests(ServiceBase):
    def test_tick_makes_one_scheduled_briefing_a_day(self):
        self.feed.items = conflicts(6, UA, at(22, 8), at(23, 5), "ua")
        self.app.history.snapshot(now=at(23, 5, 30))
        self.assertIsNone(self.svc.tick())                       # 06:00: not yet
        self.clock.t = at(23, 7, 5)
        doc = self.svc.tick()
        self.assertEqual((doc["trigger"], doc["local_date"], doc["period"]["basis"]), ("scheduled", "2026-11-23", "24h"))
        self.assertEqual(doc["ai_status"], "off")
        self.assertEqual(self.completions, [])
        self.clock.t = at(23, 7, 15)
        self.assertIsNone(self.svc.tick())                       # once a day
        self.clock.t = at(24, 7, 4)
        doc2 = self.svc.tick()
        self.assertEqual(doc2["period"]["basis"], "since_last")  # since yesterday's briefing
        self.assertEqual(doc2["period"]["from"], doc["period"]["to"])
        self.assertEqual(self.svc.db.count(), 2)
        self.assertEqual([b["id"] for b in self.svc.db.list()[0]], [doc2["id"], doc["id"]])
        st = self.svc.status()
        self.assertEqual((st["schedule"]["local_date_done"], st["schedule"]["next_due"]), ("2026-11-24", at(25, 7, 0)))
        self.assertEqual(self.app.sources["briefing.generator"]["ok"], True)

    def test_failures_are_retried_at_most_three_times_a_day(self):
        self.clock.t = at(23, 7, 5)
        calls = []

        def broken(since):
            calls.append(since)
            raise RuntimeError("boom")

        self.svc.gather_fn = broken
        for _ in range(5):
            self.assertIsNone(self.svc.tick())
        self.assertEqual(len(calls), 3)
        self.assertFalse(self.app.sources["briefing.generator"]["ok"])
        self.assertIn("boom", self.app.sources["briefing.generator"]["error"])
        self.assertEqual(self.svc.status()["last"]["ok"], False)

    def test_narrative_when_a_provider_is_ready(self):
        self.ai_ready = True
        self.clock.t = at(23, 9, 0)
        doc = self.svc.generate("manual", "24h")
        self.assertEqual(doc["ai_status"], "done")
        self.assertEqual(doc["ai"]["text"], "Narrative [Europe].")
        self.assertEqual(self.svc.db.get(doc["id"])["ai"]["provider_label"], "Ollama")
        self.assertEqual(len(self.completions), 1)
        self.ai_text = None
        self.clock.t = at(23, 10, 0)
        doc = self.svc.generate("manual", "24h")
        self.assertEqual((doc["ai_status"], doc["ai"]), ("failed", None))
        self.assertFalse(self.app.sources["briefing.narrative"]["ok"])

    def test_busy_and_periods(self):
        self.clock.t = at(23, 9, 0)
        self.assertTrue(self.svc._lock.acquire(blocking=False))
        try:
            with self.assertRaises(Busy):
                self.svc.generate()
            self.assertEqual(self.svc.start()["status"], "running")
        finally:
            self.svc._lock.release()
        since, until, basis = self.svc.period("hours", 6, at(23, 9, 0))
        self.assertEqual((until - since, basis), (6 * H, "hours"))
        self.assertEqual(self.svc.period("since_last", None, at(23, 9, 0))[2], "24h")       # nothing before
        first = self.svc.generate("manual", "24h", now=at(23, 9, 0))
        since, _, basis = self.svc.period("since_last", None, at(23, 12, 0))
        self.assertEqual((since, basis), (first["period"]["to"], "since_last"))

    def test_yesterday_is_the_briefing_nearest_24_hours_ago(self):
        values = iter([300, 100, 200])
        self.svc.gather_fn = lambda since: {"grid": {"available": True, "carbon_intensity": {
            "g_co2_per_kwh": next(values), "index": "moderate"}}}
        self.svc.generate("manual", "24h", now=at(22, 9, 0))
        self.svc.generate("manual", "24h", now=at(23, 8, 0))         # an hour ago: not "yesterday"
        doc = self.svc.generate("manual", "24h", now=at(23, 9, 0))
        row = next(t for t in section(doc, "markets")["trends"] if t["metric"] == "grid_gb")
        self.assertEqual(row["line"], "200 g CO2 per kWh (moderate); 300 g in yesterday’s briefing")

    def test_prune_keeps_sixty_days(self):
        old = self.svc.generate("manual", "24h", now=at(23, 9, 0) - 61 * DAY)
        keep = self.svc.generate("manual", "24h", now=at(23, 9, 0) - 59 * DAY)
        self.clock.t = at(23, 9, 0)
        self.svc.tick()
        self.assertIsNone(self.svc.db.get(old["id"]))
        self.assertIsNotNone(self.svc.db.get(keep["id"]))


class StoreTests(unittest.TestCase):
    def test_ids_lists_and_validation(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            store = Store(Path(tmp.name) / "t.db")
            db = BriefingStore(store)
            a = db.new_id(NOW)
            self.assertEqual(a, "20261123T080000Z")
            doc = build_doc([], [])
            doc["id"] = a
            db.save(doc)
            self.assertEqual(db.new_id(NOW), "20261123T080000Z-2")
            self.assertTrue(valid_id("20261123T080000Z-2"))
            for bad in ("", "latest", "../x", "20261123T080000Z;", "2026-11-23", "20261123T080000Z-1234"):
                self.assertFalse(valid_id(bad), bad)
            self.assertIsNone(db.get("../etc/passwd"))
            self.assertEqual(db.get(a)["id"], a)
            rows, total = db.list(10)
            self.assertEqual((total, rows[0]["headline"], rows[0]["ai"]), (1, "A quiet period", False))
            store.close()
        finally:
            tmp.cleanup()


# ---------------------------------------------------------------------------------------------
# Gathering live data from other modules
# ---------------------------------------------------------------------------------------------
class GatherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(None)
        self.app = App(cfg, Store(Path(self.tmp.name) / "t.db"), Net(cfg), LOG, Scheduler(LOG), DatasetManager(LOG))

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def add_module(self, name):
        m = self.app.module(name)
        self.app.modules[name].ok = True
        return m

    def test_alerts_skip_silently_when_the_module_is_absent(self):
        out = G.gather(self.app, NOW - DAY, budget_s=5)
        self.assertNotIn("alerts", out)
        self.assertEqual(out["errors"], {})

    def test_alerts_route_in_process(self):
        m = self.add_module("alerts")
        seen = {}

        @m.route("GET", "/api/alerts/recent")
        def recent(req):
            seen.update(req.query)
            return {"since": 1, "count": 1, "alerts": [{"id": 1, "title": "x", "time": NOW}, "junk"]}

        self.assertEqual(G.alerts_recent(self.app, NOW - DAY), [{"id": 1, "title": "x", "time": NOW}])
        self.assertEqual(seen, {"since": str(int(NOW - DAY)), "limit": "100"})

    def test_routes_returning_responses_and_errors(self):
        m = self.add_module("intel")

        @m.route("GET", "/api/intel/cyber")
        def cyber(req):
            return Response.json({"type": "FeatureCollection", "features": [], "meta": {"outages_at": {"ioda": 1}}})

        self.assertEqual(G.cyber(self.app)["features"], [])
        a = self.add_module("alerts")

        @a.route("GET", "/api/alerts/recent")
        def broken(req):
            return Response.json({"error": {"code": "X"}}, status=500)

        out = G.gather(self.app, NOW - DAY, budget_s=5, jobs={"alerts": lambda: G.alerts_recent(self.app, NOW - DAY)})
        self.assertEqual(out["errors"], {"alerts": "HTTP 500"})

    def test_tools_and_budget(self):
        m = self.add_module("space")
        m.ai_tool("space_weather_now", "x", {"type": "object"}, lambda args: {"summary": "Quiet."})
        self.assertEqual(G.space_weather(self.app), {"summary": "Quiet."})
        i = self.add_module("infra")
        i.ai_tool("grid_now", "x", {"type": "object"}, lambda args: {"available": False, "message": "NESO down"})
        with self.assertRaises(RuntimeError):
            G.grid_gb(self.app)
        release = threading.Event()
        started = time.monotonic()
        out = G.gather(self.app, NOW, budget_s=0.5, jobs={"slow": lambda: release.wait(5), "fast": lambda: 42,
                                                         "skip": lambda: (_ for _ in ()).throw(G.Skip("x"))})
        release.set()
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual((out.get("fast"), out["errors"]), (42, {"slow": "did not answer in time"}))
        self.assertNotIn("skip", out)


# ---------------------------------------------------------------------------------------------
# HTTP: routes, shapes, guards, the AI tool
# ---------------------------------------------------------------------------------------------
class SharingTests(unittest.TestCase):
    """CONTRACT 15.5: the user's alerts reach briefing_latest only while Alerts sharing (ai_share) is on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(None, overrides={"AI_PROVIDER": "off", "ANTHROPIC_API_KEY": ""})
        self.app = App(cfg, Store(Path(self.tmp.name) / "t.db"), Net(cfg), LOG, Scheduler(LOG), DatasetManager(LOG))
        register_core(self.app)

    def tearDown(self):
        self.app.store.close()
        helper.unbind()
        self.tmp.cleanup()

    def load(self, names):
        load_all(self.app, set(names))
        for n in names:
            self.assertTrue(self.app.modules[n].ok, self.app.modules[n].error)
        doc = sample_doc()
        self.app.module("briefing").service.db.save(doc)
        return doc

    def tool(self, **args):
        return self.app.call_ai_tool("briefing_latest", args)

    def assert_withheld(self, doc):
        part = self.tool(section="your_areas")
        self.assertNotIn("section", part)
        self.assertEqual(part["message"],
                         "The user has switched off sharing alerts with the assistant (Alerts, Settings).")
        self.assertNotIn("Something near Home", json.dumps(part))
        over = self.tool()
        self.assertNotIn("your_areas", over)                         # no hint pointing at the alerts
        self.assertNotIn("Your areas", over["text"])
        self.assertIn("Your areas", doc["text"])
        self.assertTrue(over["text"].startswith("Briefing for"))
        self.assertNotIn("Something near Home", json.dumps(over))

    def test_sharing_off_by_default_then_on(self):
        from meridian.modules.alerts import db as alerts_db
        doc = self.load(["alerts", "briefing"])
        self.assertFalse(alerts_db.settings(self.app.store)["ai_share"])
        self.assert_withheld(doc)
        alerts_db.save_settings(self.app.store, {"ai_share": True}, time.time())
        part = self.tool(section="your_areas")
        self.assertEqual(part["section"]["items"][0]["title"], "Something near Home")
        over = self.tool()
        self.assertIn("your_areas", over)
        self.assertEqual(over["text"], doc["text"])

    def test_no_alerts_module_counts_as_off(self):
        self.assert_withheld(self.load(["briefing"]))

    def test_without_areas(self):
        from meridian.modules.briefing import without_areas
        self.assertEqual(without_areas("A\n\nYour areas: 1 alert in one of your areas.\n\nB"), "A\n\nB")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MERIDIAN_DATA_DIR"] = cls.tmp.name
        cfg = Config(None, overrides={"AI_PROVIDER": "off", "ANTHROPIC_API_KEY": ""})
        app = App(cfg, Store(Path(cls.tmp.name) / "t.db"), Net(cfg), LOG, Scheduler(LOG), DatasetManager(LOG))
        register_core(app)
        events = app.module("hazards")
        now = time.time()
        events.events(lambda since: [ev("earthquake", "q1", module="hazards", t=now - 3600, sev=7, at=(36.0, 140.0),
                                        title="M 6.0, Japan", source="USGS")])
        app.modules["hazards"].ok = True
        load_all(app, {"briefing"})
        assert app.modules["briefing"].ok, app.modules["briefing"].error
        app.history.snapshot()
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
        helper.unbind()
        cls.tmp.cleanup()

    def req(self, path, body=None, headers=None, raw=False):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"} if data is not None else {}
        h.update(headers or {})
        r = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                payload = resp.read()
                return resp.status, (payload.decode() if raw else json.loads(payload)), dict(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}"), dict(err.headers)

    def post(self, path, body):
        return self.req(path, body, {"Origin": f"http://127.0.0.1:{self.port}", "X-Meridian": "1"})

    def wait_idle(self):
        for _ in range(200):
            st, body, _ = self.req("/api/briefing/status")
            if not body["running"]:
                return body
            time.sleep(0.05)
        self.fail("generation did not finish")

    def test_a_flow(self):
        st, body, _ = self.req("/api/briefing/latest")
        self.assertEqual((st, body["error"]["code"]), (404, "NOT_FOUND"))
        st, body, _ = self.req("/api/briefing/generate", {"period": "24h"})                 # no Origin, no X-Meridian
        self.assertEqual(st, 403)
        st, body, _ = self.post("/api/briefing/generate", {"period": "fortnight"})
        self.assertEqual((st, body["error"]["code"]), (400, "BAD_FIELD"))
        st, body, _ = self.post("/api/briefing/generate", {"period": "hours", "hours": 0})
        self.assertEqual(st, 400)
        st, body, _ = self.post("/api/briefing/generate", {"period": "24h"})
        self.assertEqual((st, body["ok"]), (200, True))
        status = self.wait_idle()
        self.assertTrue(status["last"]["ok"], status)
        self.assertFalse(status["narrative"]["ready"])
        st, doc, _ = self.req("/api/briefing/latest")
        self.assertEqual(st, 200)
        for key in ("id", "time", "period", "sections", "text", "ai"):
            self.assertIn(key, doc)
        self.assertEqual(doc["ai_status"], "off")
        apac = next(s for s in doc["sections"] if s["id"] == "apac")
        self.assertEqual(apac["items"][0]["title"], "M 6.0, Japan")
        st, same, _ = self.req(f"/api/briefing/{doc['id']}")
        self.assertEqual((st, same["id"]), (200, doc["id"]))
        st, listing, _ = self.req("/api/briefing/list?limit=5")
        self.assertEqual((listing["total"], listing["briefings"][0]["id"], listing["keep_days"]), (1, doc["id"], 60))
        st, text, hdrs = self.req(f"/api/briefing/{doc['id']}/markdown", raw=True)
        self.assertEqual(st, 200)
        self.assertTrue(text.startswith("# Meridian briefing"))
        self.assertIn("text/markdown", hdrs["Content-Type"])
        self.assertIn('attachment; filename="meridian-briefing-', hdrs["Content-Disposition"])
        self.assertIn(f"http://127.0.0.1:{self.port}/#map=", text)
        st, _, _ = self.req("/api/briefing/latest/markdown", raw=True)
        self.assertEqual(st, 200)
        # the AI tool
        out = self.app.call_ai_tool("briefing_latest", {})
        self.assertEqual((out["available"], out["id"]), (True, doc["id"]))
        self.assertNotIn("areas", [s["id"] for s in out["sections"]])
        self.assertEqual(out["text"], doc["text"])
        self.assertEqual(out["headline"][0]["title"], "M 6.0, Japan")
        self.assertLess(len(json.dumps(out)), 8000)               # fits a tool result (Ollama's limit)
        one = self.app.call_ai_tool("briefing_latest", {"section": "apac"})
        self.assertEqual(one["section"]["id"], "apac")
        self.assertEqual(one["section"]["items"][0]["entity"]["kind"], "earthquake")
        self.assertIn("message", self.app.call_ai_tool("briefing_latest", {"section": "your_areas"}))
        self.assertIn("error", self.app.call_ai_tool("briefing_latest", {"section": "mars"}))
        self.assertIn("error", self.app.call_ai_tool("briefing_latest", {"id": "../x"}))
        self.assertEqual(self.app.call_ai_tool("briefing_latest", {"id": "20200101T000000Z"})["available"], False)

    def test_bad_ids_and_args(self):
        self.assertEqual(self.req("/api/briefing/not-an-id")[0], 400)
        self.assertEqual(self.req("/api/briefing/20200101T000000Z")[0], 404)
        self.assertEqual(self.req("/api/briefing/20200101T000000Z/markdown")[0], 404)
        self.assertEqual(self.req("/api/briefing/list?limit=0")[0], 400)
        self.assertEqual(self.req("/api/briefing/list?limit=abc")[0], 400)
        st, body, _ = self.req("/api/briefing/latest", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 403)

    def test_status_shape(self):
        st, body, _ = self.req("/api/briefing/status")
        self.assertEqual(st, 200)
        self.assertEqual(body["schedule"]["briefing_time"], "07:00")
        self.assertIn("next_due", body["schedule"])
        self.assertFalse(body["schedule"]["running"])                 # tasks are not started in tests
        self.assertEqual(body["keep_days"], 60)
        names = [t["name"] for t in self.app.ai_tools()]
        self.assertIn("briefing_latest", names)
        self.assertIn("briefing.generator", self.app.sources)
        keys = {k["name"]: k for k in self.app.config.key_status()}
        self.assertFalse(keys["BRIEFING_TIME"]["secret"])


if __name__ == "__main__":
    unittest.main()
