"""Flights module unit tests: parsing, classification, tiling, tracker (merge, trails, dead
reckoning, events), GPS interference, lookups and provider politeness. No network.

Run: python -m unittest tests.test_flights -v
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

from meridian import geo
from meridian.net import NetError, NetResponse
from meridian.store import Store
from meridian.modules.flights import classify as cl
from meridian.modules.flights import gpsjam, lookup, parse, tiles
from meridian.modules.flights.const import (EST_MAX_AGE, FLAG_EMG, FLAG_EST, FLAG_GND, FLAG_MIL, FLAG_MLAT,
                                            FLAG_PRIV, FLAG_SANC, FIELDS, LIVE_MAX_AGE)
from meridian.modules.flights.providers import (AdsbFi, BudgetExhausted, DailyBudget, Health, HostQueue, OpenSky)
from meridian.modules.flights.tracker import Tracker, estimate

FIX = Path(__file__).parent / "fixtures" / "flights"
NOW = 1795382034.0


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def obs_by_hex(observations):
    return {o["hex"]: o for o in observations}


class ParseTests(unittest.TestCase):
    def test_readsb_v3(self):
        now, obs = parse.parse_readsb(load("adsbfi_v3_point.json"))
        self.assertAlmostEqual(now, NOW)
        by = obs_by_hex(obs)
        self.assertNotIn("~2a1b3c", by)                       # non-ICAO TIS-B address skipped
        self.assertEqual(len(obs), 12)
        sas = by["4cacb5"]
        self.assertEqual((sas["cs"], sas["reg"], sas["typ"], sas["cat"], sas["sq"]), ("SAS87J", "EI-SIB", "A20N", "A3", "6240"))
        self.assertAlmostEqual(sas["pos_t"], NOW - 0.3)
        self.assertEqual(sas["alt"], 4225)
        self.assertEqual(sas["vr"], -1280)
        self.assertFalse(sas["gnd"])
        gnd = by["42592e"]
        self.assertTrue(gnd["gnd"])
        self.assertEqual(gnd["alt"], 0)
        c17 = by["ae0805"]
        self.assertEqual(c17["vr"], -64)                        # geom_rate fallback
        self.assertEqual(c17["db_flags"], 1)
        stale = by["4d2111"]
        self.assertTrue(stale["stale_pos"])                     # from lastPosition
        self.assertAlmostEqual(stale["pos_t"], NOW - 95.0)
        jam = by["48c138"]
        self.assertEqual((jam["ptype"], jam["nac_p"], jam["nic"]), ("mlat", 0, 0))
        self.assertAlmostEqual(jam["gps_ok_before"], 1795381357.5)

    def test_readsb_v2_seconds(self):
        now, obs = parse.parse_readsb(load("adsbfi_v2_point.json"))
        self.assertAlmostEqual(now, 1795382024.0)
        self.assertEqual({o["hex"] for o in obs}, {"407d45", "48e305"})

    def test_bad_fields_rejected(self):
        payload = {"now": NOW, "ac": [
            {"hex": "ABCDEF", "lat": 95.0, "lon": 0, "alt_baro": 123456, "squawk": "9999", "flight": "<script>",
             "category": "Z9", "t": "TOOLONGTYPE", "seen": "x"},
            {"hex": "12345", "lat": 1, "lon": 1}, "not a dict", {"hex": None}]}
        now, obs = parse.parse_readsb(payload)
        self.assertEqual(len(obs), 1)
        o = obs[0]
        self.assertEqual(o["hex"], "abcdef")
        for key in ("lat", "alt", "sq", "cs", "cat", "typ"):
            self.assertNotIn(key, o)
        with self.assertRaises(ValueError):
            parse.parse_readsb([1, 2])
        with self.assertRaises(ValueError):
            parse.parse_readsb({"ac": []})       # no timestamp

    def test_opensky(self):
        now, obs = parse.parse_opensky(load("opensky_states.json"))
        by = obs_by_hex(obs)
        self.assertEqual(len(obs), 3)
        a = by["46b82a"]
        self.assertEqual(a["cs"], "AEE2FL")
        self.assertEqual(a["alt"], int(round(1363.98 * parse.M_TO_FT)))
        self.assertAlmostEqual(a["gs"], 102.5 * parse.MS_TO_KT, places=3)
        self.assertEqual(a["cat"], "A3")
        self.assertTrue(by["4079d0"]["gnd"])
        self.assertNotIn("lat", by["abcdef"])

    def test_trace(self):
        pts = parse.parse_trace(load("adsblol_trace.json"))
        self.assertEqual(len(pts), 4)
        self.assertEqual(pts[0], (1795381000.0, 51.2, -1.5, 9000))
        self.assertEqual(pts[3][3], 0)            # "ground"
        self.assertEqual(parse.parse_trace({"trace": "x"}), [])


class ClassifyTests(unittest.TestCase):
    def c(self, **kw):
        return cl.classify(**kw)

    def test_classes(self):
        self.assertEqual(self.c(category="A3", typ="A20N", callsign="SAS87J")[:2], ("airliner", "jet"))
        self.assertEqual(self.c(category="A5", typ="B77L", callsign="FDX5871")[:2], ("cargo", "heavy"))
        self.assertEqual(self.c(category="A3", typ="GL7T", callsign="N1209X", reg="N1209X")[:2], ("business", "bizjet"))
        self.assertEqual(self.c(category="A5", typ="C17", db_flags=1, callsign="RCH5041")[:2], ("military", "heavy"))
        self.assertEqual(self.c(category="A6", typ="EUFI")[:2], ("military", "fighter"))
        self.assertEqual(self.c(category="A7", typ="EC35", callsign="GMPSC")[:2], ("helicopter", "heli"))
        self.assertEqual(self.c(category="A1", typ="C172", callsign="GABCD", reg="G-ABCD")[:2], ("light", "prop"))
        self.assertEqual(self.c(category="B1", typ="DG80")[:2], ("glider", "glider"))
        self.assertEqual(self.c(category="B2")[:2], ("glider", "balloon"))
        self.assertEqual(self.c(category="B6")[:2], ("glider", "uav"))
        self.assertEqual(self.c(category="C2", typ="GND")[:2], ("ground", "ground"))
        self.assertEqual(self.c(typ="TWR")[:2], ("ground", "ground"))
        self.assertEqual(self.c(category="A2", typ="AT76", callsign="LOG91YC")[:2], ("airliner", "turboprop"))
        self.assertEqual(self.c(callsign="BAW123")[:2], ("airliner", "jet"))       # airline callsign only
        self.assertEqual(self.c(callsign="NATO01")[0], "military")
        self.assertEqual(self.c()[:2], ("unknown", "generic"))
        self.assertEqual(self.c(category="A3", typ="A306", ownop="Some Cargo Airlines")[0], "cargo")

    def test_sizes(self):
        self.assertEqual(self.c(category="A5", typ="B744")[2], 1.0)
        self.assertLess(self.c(category="A1", typ="C172")[2], self.c(category="A3", typ="A320")[2])
        self.assertEqual(self.c(category="C1")[2], 0.5)

    def test_airline_prefix(self):
        self.assertEqual(cl.airline_prefix("BAW49TA"), "BAW")
        self.assertIsNone(cl.airline_prefix("GABCD"))
        self.assertIsNone(cl.airline_prefix(None))


class TileTests(unittest.TestCase):
    def test_radius_never_over_250nm(self):
        for level in tiles.LEVELS:
            step = tiles.LEVELS[level]
            for row in range(0, int(180 / step), 3):
                t = tiles.tile_for(level, row, 0)
                self.assertLessEqual(t.radius_nm, 250)
                # the circle really covers its cell corners
                lat0 = -90 + row * step
                n = tiles._ncols(step, row)
                lon0 = -180.0
                for la in (lat0, min(90.0, lat0 + step)):
                    for lo in (lon0, lon0 + 360.0 / n):
                        d = geo.haversine_km(t.lat, t.lon, la, lo)
                        if t.radius_nm < 250:
                            self.assertLessEqual(d, t.radius_km + 1.0)

    def test_plans(self):
        city = tiles.plan((-0.6, 51.35, -0.2, 51.6), 11)
        self.assertEqual((city.mode, city.level), ("tiles", 2))
        self.assertLessEqual(len(city.tiles), 4)
        country = tiles.plan((-8.0, 49.5, 2.5, 59.0), 6)
        self.assertEqual(country.mode, "tiles")
        self.assertLessEqual(len(country.tiles), 6)
        europe = tiles.plan((-15.0, 35.0, 35.0, 62.0), 5)
        self.assertEqual(europe.mode, "wide")
        self.assertEqual(len(europe.tiles), 6)            # centre tiles from zoom 4.5
        world = tiles.plan((-180.0, -85.0, 180.0, 85.0), 2)
        self.assertEqual((world.mode, world.tiles), ("wide", []))
        # cover: every corner of the city view is inside some tile circle
        for la, lo in ((51.35, -0.6), (51.6, -0.2), (51.35, -0.2), (51.6, -0.6)):
            self.assertTrue(any(geo.haversine_km(t.lat, t.lon, la, lo) <= t.radius_km for t in city.tiles))

    def test_antimeridian(self):
        p = tiles.plan((179.0, -18.5, -179.0, -16.5), 9)
        self.assertEqual(p.mode, "tiles")
        lons = [t.lon for t in p.tiles]
        self.assertTrue(any(lo > 170 for lo in lons) and any(lo < -170 for lo in lons))

    def test_adhoc_and_cover(self):
        t = tiles.adhoc_tile(51.4706, -0.4619, 27)
        self.assertEqual(t.id, "P:51.47:-0.46:27")
        self.assertTrue(tiles.circle_covers(t, 51.4706, -0.4619, 40))
        self.assertFalse(tiles.circle_covers(t, 52.5, -0.46, 40))


def fresh(o: dict, now: float, age: float = 1.0) -> dict:
    o = dict(o)
    o["pos_t"] = now - age
    o["seen_t"] = now - age
    return o


class TrackerTests(unittest.TestCase):
    def setUp(self):
        _, self.obs = parse.parse_readsb(load("adsbfi_v3_point.json"))
        self.by = obs_by_hex(self.obs)
        self.t = Tracker(clock=lambda: NOW)

    def test_ingest_rows_and_flags(self):
        self.t.ingest(self.obs, "adsbfi", NOW)
        items = self.t.displayed(NOW)
        hexes = {ac.hex for ac, *_ in items}
        self.assertIn("4cacb5", hexes)
        self.assertNotIn("4d2111", hexes)                 # 95 s old, not a dropout
        rows, truncated = self.t.rows(items, NOW, 1000)
        self.assertFalse(truncated)
        by = {r[0]: dict(zip(FIELDS, r)) for r in rows}
        self.assertEqual(by["4cacb5"]["cls"], "airliner")
        self.assertTrue(by["ae0805"]["flags"] & FLAG_MIL)
        self.assertTrue(by["400a0e"]["flags"] & FLAG_EMG)
        self.assertTrue(by["42592e"]["flags"] & FLAG_GND)
        self.assertTrue(by["48c138"]["flags"] & FLAG_MLAT)
        self.assertTrue(by["a0f00d"]["flags"] & FLAG_PRIV)
        self.assertEqual(by["4cacb5"]["lat"], round(51.548177, 5))
        # emergencies and sanctioned draw last (on top)
        self.assertEqual(rows[-1][0], "400a0e")

    def test_freshest_position_wins(self):
        a = fresh(self.by["4cacb5"], NOW, 5.0)
        b = dict(a, lat=51.6, lon=-0.9, pos_t=NOW - 1.0, seen_t=NOW - 1.0)
        old = dict(a, lat=50.0, lon=-2.0, pos_t=NOW - 30.0, seen_t=NOW - 30.0)
        self.t.ingest([a], "adsbfi", NOW)
        self.t.ingest([b], "adsblol", NOW)
        self.t.ingest([old], "opensky", NOW)
        ac = self.t.get("4cacb5")
        self.assertEqual((ac.lat, ac.lon, ac.pos_src), (51.6, -0.9, "adsblol"))
        self.assertEqual(set(ac.sources), {"adsbfi", "adsblol", "opensky"})

    def test_dropout_dead_reckoning(self):
        fdx = fresh(self.by["a1b2c3"], NOW, 1.0)       # 36,000 ft, 488 kt, track 92
        self.t.ingest([fdx], "adsbfi", NOW)
        later = NOW + 300
        ac = self.t.get("a1b2c3")
        self.assertIsNone(ac.display(later))            # stale and never reported missing: hidden
        n = self.t.mark_dropouts(51.9, -0.1, 300.0, present=set(), fetched_at=later)
        self.assertEqual(n, 1)
        lat, lon, est = ac.display(later)
        self.assertTrue(est)
        elat, elon = estimate(51.9, -0.1, 92.0, 488.0, 301.0)
        self.assertAlmostEqual(lat, elat, places=6)
        self.assertAlmostEqual(lon, elon, places=6)
        dist = geo.haversine_km(51.9, -0.1, lat, lon)
        self.assertAlmostEqual(dist, 488 * 1.852 * 301 / 3600, delta=0.5)
        row = ac.row(lat, lon, est, later)
        self.assertTrue(dict(zip(FIELDS, row))["flags"] & FLAG_EST)
        self.assertIsNone(ac.display(NOW + EST_MAX_AGE + 5))   # estimates stop after 20 min

    def test_other_networks_miss_is_not_a_dropout(self):
        fdx = fresh(self.by["a1b2c3"], NOW, 1.0)
        self.t.ingest([fdx], "adsblol", NOW)
        self.assertEqual(self.t.mark_dropouts(51.9, -0.1, 300.0, set(), NOW + 300, source="adsbfi"), 0)
        self.assertEqual(self.t.mark_dropouts(51.9, -0.1, 300.0, set(), NOW + 300, source="adsblol"), 1)

    def test_landing_is_not_estimated(self):
        baw = fresh(self.by["400a0e"], NOW, 1.0)       # 1,450 ft descending
        self.t.ingest([baw], "adsbfi", NOW)
        self.t.mark_dropouts(51.47, -0.30, 50, set(), NOW + 120)
        self.assertIsNone(self.t.get("400a0e").display(NOW + 120))

    def test_present_aircraft_not_dropped(self):
        sas = fresh(self.by["4cacb5"], NOW, 1.0)
        self.t.ingest([sas], "adsbfi", NOW)
        self.assertEqual(self.t.mark_dropouts(51.5, -0.8, 100, {"4cacb5"}, NOW + 70), 0)
        self.assertEqual(self.t.mark_dropouts(40.0, 10.0, 100, set(), NOW + 70), 0)   # outside the circle

    def test_slow_sources_live_longer(self):
        c17 = fresh(self.by["ae0805"], NOW, 1.0)
        self.t.ingest([c17], "adsbfi", NOW, slow=True)
        self.assertIsNotNone(self.t.get("ae0805").display(NOW + LIVE_MAX_AGE + 30))
        self.t.ingest([dict(c17, pos_t=NOW + 1, seen_t=NOW + 1)], "adsbfi", NOW + 1)
        self.assertIsNone(self.t.get("ae0805").display(NOW + LIVE_MAX_AGE + 30))

    def test_trail_sampling_and_merge(self):
        base = fresh(self.by["4cacb5"], NOW, 0.0)
        for i in range(0, 120, 5):                       # every 5 s for 2 min, straight
            o = dict(base, pos_t=NOW + i, seen_t=NOW + i, lat=51.5 + i * 0.001)
            self.t.ingest([o], "adsbfi", NOW + i)
        ac = self.t.get("4cacb5")
        pts = ac.trail_points()
        self.assertLessEqual(len(pts), 9)                # thinned to >= 15 s steps
        self.assertGreaterEqual(len(pts), 7)
        trace = parse.parse_trace(load("adsblol_trace.json"))
        shifted = [(NOW - 1000 + (p[0] - 1795381000.0), p[1], p[2], p[3]) for p in trace]
        added = ac.merge_trail(shifted, NOW + 120)
        self.assertGreaterEqual(added, 2)
        merged = ac.trail_points()
        self.assertEqual([p[0] for p in merged], sorted(p[0] for p in merged))
        ac.prune_trail(NOW + 120 + 45 * 60)
        self.assertLess(len(ac.trail_points()), len(merged))

    def test_events_and_sanctions(self):
        hits = {"730abc": {"uid": "99999", "program": "IRAN] [SDGT", "registration": "EP-TST", "match": "mode_s",
                           "operator": "Example Air", "linked_to": ["EXAMPLE AIR", "DOE, John"]}}
        t = Tracker(sanctions_lookup=lambda h, r: hits.get(h), clock=lambda: NOW)
        res = t.ingest([fresh(o, NOW) for o in self.obs if "lat" in o], "adsbfi", NOW)
        kinds = sorted((k, h) for k, h, _, _ in res.events)
        self.assertIn(("emergency", "400a0e"), kinds)
        self.assertIn(("sanctioned", "730abc"), kinds)
        rows, _ = t.rows(t.displayed(NOW), NOW, 100)
        flags = {r[0]: r[FIELDS.index("flags")] for r in rows}
        self.assertTrue(flags["730abc"] & FLAG_SANC)
        res2 = t.ingest([fresh(self.by["400a0e"], NOW + 10)], "adsbfi", NOW + 10)
        self.assertEqual(res2.events, [])              # same episode, too soon for an update

    def test_lifeguard_is_not_an_emergency(self):
        ambulance = fresh(dict(self.by["407f11"], emg="lifeguard"), NOW)
        glitch = fresh(dict(self.by["4cacb5"], emg="downed"), NOW)
        res = self.t.ingest([ambulance, glitch], "adsbfi", NOW)
        self.assertIsNone(self.t.get("407f11").emergency_code())
        self.assertEqual(self.t.get("4cacb5").emergency_code(), "downed")
        self.assertEqual([h for k, h, _, _ in res.events if k == "emergency"], ["4cacb5"])

    def test_search_and_near(self):
        self.t.ingest([fresh(o, NOW) for o in self.obs if "lat" in o], "adsbfi", NOW)
        hits = self.t.search("SAS87J", 10, NOW)
        self.assertEqual(hits[0]["id"], "4cacb5")
        self.assertEqual(hits[0]["kind"], "aircraft")
        self.assertEqual(self.t.search("ei-sib", 10, NOW)[0]["id"], "4cacb5")
        self.assertEqual(self.t.search("4cacb5", 10, NOW)[0]["score"], 0.95)
        self.assertTrue(any(h["id"] == "4cacb5" for h in self.t.search("SAS8", 10, NOW)))
        near = self.t.near(51.47, -0.45, 30, NOW)
        self.assertEqual(near[0][1].hex, "42592e")      # the ground vehicle at Heathrow is closest
        self.assertEqual(self.t.search("x", 10, NOW), [])

    def test_prune(self):
        self.t.ingest([fresh(self.by["4cacb5"], NOW)], "adsbfi", NOW)
        self.assertEqual(self.t.prune(NOW + 60), 0)
        self.assertEqual(self.t.prune(NOW + 3 * 3600), 1)
        self.assertIsNone(self.t.get("4cacb5"))


class GpsJamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.g = gpsjam.GpsJam(self.store, clock=lambda: NOW)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_sample_rules(self):
        _, obs = parse.parse_readsb(load("adsbfi_v3_point.json"))
        by = obs_by_hex(obs)
        self.assertIs(gpsjam.sample(by["48c138"], NOW), True)            # MLAT, NACp 0, GPS lost
        self.assertIs(gpsjam.sample(by["4cacb5"], NOW), False)
        self.assertIsNone(gpsjam.sample(by["42592e"], NOW))              # on the ground
        self.assertIsNone(gpsjam.sample(by["4d2111"], NOW))              # stale lastPosition
        self.assertIsNone(gpsjam.sample({"hex": "abcdef", "lat": 1, "lon": 1, "pos_t": NOW, "alt": 30000,
                                         "ptype": "mlat", "nic": 0}, NOW))  # Mode S only: no verdict
        self.assertIsNone(gpsjam.sample(dict(by["4cacb5"], ptype="tisb_icao"), NOW))
        self.assertIs(gpsjam.sample({"hex": "abcdef", "lat": 1, "lon": 1, "pos_t": NOW, "alt": 30000,
                                     "version": 2, "nic": 3}, NOW), True)
        self.assertIsNone(gpsjam.sample(dict(by["4cacb5"], pos_t=NOW - 100), NOW))

    def test_levels(self):
        self.assertEqual(gpsjam.level(5, 3), "none")
        self.assertEqual(gpsjam.level(100, 1), "green")
        self.assertEqual(gpsjam.level(100, 3), "amber")
        self.assertEqual(gpsjam.level(20, 1), "green")          # 5% but only one affected aircraft
        self.assertEqual(gpsjam.level(20, 5), "red")

    def _obs(self, n, bad, lat=55.2, lon=20.2, hour_offset=0, prefix="a"):
        out = []
        for i in range(n):
            out.append({"hex": f"{prefix}{i:05x}"[:6], "lat": lat, "lon": lon, "pos_t": NOW - 1 + hour_offset * 3600,
                        "alt": 30000, "gnd": False, "ptype": "adsb_icao", "version": 2, "nic": 8,
                        "nac_p": 0 if i < bad else 9})
        return out

    def test_aggregate_flush_and_read(self):
        n, bad = self.g.observe(self._obs(30, 9), NOW)
        self.assertEqual((n, bad), (30, 9))
        self.g.observe(self._obs(30, 9), NOW)             # same aircraft again: still 30 aircraft-hours
        self.assertEqual(self.g.flush(NOW), 1)
        self.g.observe(self._obs(10, 0, prefix="b"), NOW)  # more aircraft in the same hour
        self.g.flush(NOW)
        cells = self.g.cells(now=NOW)
        self.assertEqual(len(cells), 1)
        c = cells[0]
        self.assertEqual((c["total"], c["bad"], c["level"]), (40, 9, "red"))
        self.assertEqual((c["lat0"], c["lon0"], c["size"]), (55.0, 20.0, 0.5))
        self.assertEqual(self.g.cells(now=NOW, bbox=(0, 0, 10, 10)), [])
        agg = self.g.cells(now=NOW, agg=2)
        self.assertEqual(agg[0]["size"], 1.0)
        d = self.g.cell_detail(55.3, 20.4, now=NOW)
        self.assertEqual((d["total"], d["bad"], d["level"]), (40, 9, "red"))
        self.assertEqual(len(d["hourly"]), 1)
        self.assertIn("not a direct measurement", d["method"])

    def test_restart_keeps_max_and_prune(self):
        self.g.observe(self._obs(20, 2), NOW)
        self.g.flush(NOW)
        g2 = gpsjam.GpsJam(self.store, clock=lambda: NOW)    # a restart loses in-memory hour sets
        g2.observe(self._obs(5, 0, prefix="c"), NOW)
        g2.flush(NOW)
        row = self.store.one("SELECT total, bad FROM flights_gpsjam")
        self.assertEqual((row["total"], row["bad"]), (20, 2))   # MAX, not overwritten by the smaller set
        g2.prune(NOW + 49 * 3600)                              # kept 14 days for the 7-day trend
        self.assertIsNotNone(self.store.one("SELECT * FROM flights_gpsjam"))
        g2.prune(NOW + 15 * 86400)
        self.assertIsNone(self.store.one("SELECT * FROM flights_gpsjam"))


class LookupTests(unittest.TestCase):
    def test_operator_privacy(self):
        ov = lookup.operator_view
        self.assertEqual(ov(owner="Scandinavian Airlines Connect", flag_code="SZS"), ("Scandinavian Airlines Connect", "airline"))
        self.assertEqual(ov(owner="BANK OF UTAH TRUSTEE", flag_code=None)[1], "company")
        self.assertEqual(ov(owner="EXAMPLE AVIATION LLC", flag_code=None)[1], "company")
        self.assertEqual(ov(owner="SMITH JOHN A", flag_code=None), ("Private owner", "private"))
        self.assertEqual(ov(owner="John Smith", flag_code=None), ("Private owner", "private"))
        self.assertEqual(ov(owner="THE SMITH FAMILY TRUST", flag_code=None), ("Private owner", "private"))
        self.assertEqual(ov(owner="Anything", flag_code="BAW", db_flags=8), ("Withheld (privacy programme)", "withheld"))
        self.assertEqual(ov(owner="United States Air Force", flag_code=None, military=True)[1], "government")
        self.assertEqual(ov(owner=None, flag_code=None, route_airline="Loganair"), ("Loganair", "airline"))
        self.assertEqual(ov(owner=None, flag_code=None), (None, "unknown"))
        self.assertEqual(ov(owner="KLM ROYAL DUTCH AIRLINES", flag_code=None)[1], "company")
        self.assertEqual(ov(owner="ACME HOLDINGS AB", flag_code=None)[1], "company")
        self.assertEqual(ov(owner="AB SMITH", flag_code=None)[1], "private")   # initials, not a legal form

    def test_route_parse_and_check(self):
        route = lookup.parse_route(load("adsbdb_both.json"))
        self.assertEqual(route["origin"]["iata"], "ARN")
        self.assertEqual(lookup.route_text(route), "ARN to LHR")
        self.assertEqual(lookup.route_check(route, 55.5, 10.0), "plausible")     # over Denmark
        self.assertEqual(lookup.route_check(route, 40.0, -3.7), "doubtful")      # over Madrid
        self.assertIsNone(lookup.route_check(route, None, None))
        self.assertIsNone(lookup.parse_route(load("adsbdb_unknown.json")))
        info = lookup.parse_aircraft(load("adsbdb_both.json"))
        self.assertEqual((info["registration"], info["operator_flag"]), ("EI-SIB", "SZS"))

    def test_cache_and_negative_cache(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        store = Store(Path(tmp.name) / "t.db")
        calls = []

        class Net:
            def request(self, url, **kw):
                calls.append(url)
                if "callsign/SAS87J" in url:
                    return NetResponse(200, {}, (FIX / "adsbdb_both.json").read_bytes(), url)
                return NetResponse(404, {}, b'{"response": "unknown callsign"}', url)

        q = HostQueue("adsbdb")
        stop = threading.Event()
        threading.Thread(target=q.run, args=(stop,), daemon=True).start()
        try:
            lk = lookup.Lookups(Net(), store, logging.getLogger("t"), q, lambda **kw: None)
            self.assertEqual(lk.route("SAS87J", wait=3)[0], "ok")
            self.assertEqual(lk.route("SAS87J")[0], "ok")
            self.assertEqual(lk.route("ZZZ999", wait=3), ("unknown", None))
            self.assertEqual(lk.route("ZZZ999"), ("unknown", None))
            self.assertEqual(len(calls), 2)                 # both answers cached, including the miss
            lk2 = lookup.Lookups(Net(), store, logging.getLogger("t"), q, lambda **kw: None)
            self.assertEqual(lk2.cached_route("SAS87J")[0], "ok")   # survives a restart (SQLite)
            self.assertEqual(lk.route("bad cs!"), ("unknown", None))
        finally:
            stop.set()
            store.close()
            tmp.cleanup()


class ProviderTests(unittest.TestCase):
    def test_queue_priority_coalesce_expiry(self):
        q = HostQueue("t")
        order = []
        q.submit("low", lambda: order.append("low"), priority=5)
        q.submit("high", lambda: order.append("high"), priority=0)
        self.assertIs(q.submit("low", lambda: order.append("dup"), priority=6), q.pending("low"))
        expired = q.submit("old", lambda: order.append("old"), priority=1, ttl=0.001)
        time.sleep(0.01)
        q.drain_once()
        self.assertEqual(order, ["high", "low"])
        self.assertIsInstance(expired.error, TimeoutError)
        self.assertEqual(q.size(), 0)

    def test_queue_reprioritise(self):
        q = HostQueue("t")
        order = []
        q.submit("a", lambda: order.append("a"), priority=5)
        q.submit("b", lambda: order.append("b"), priority=4)
        q.submit("a", lambda: order.append("a2"), priority=1)    # same key, more urgent
        q.drain_once()
        self.assertEqual(order, ["a", "b"])

    def test_budget(self):
        clock = [1000.0]
        b = DailyBudget(None, "t", per_day=86400 / 10, burst=2, clock=lambda: clock[0])   # one token per 10 s
        b.take()
        b.take()
        self.assertFalse(b.can())
        with self.assertRaises(BudgetExhausted):
            b.take()
        clock[0] += 10
        self.assertTrue(b.can())
        b.note_remote(5, 60)
        b.reserve = 10
        self.assertFalse(b.can())                 # provider says nearly used up

    def test_health_backoff(self):
        h = Health()
        h.fail("x", now=100.0)
        self.assertFalse(h.available(110.0))
        self.assertTrue(h.available(116.0))
        h.fail("x", retry_after=120, now=200.0)
        self.assertFalse(h.available(300.0))
        h.ok()
        self.assertTrue(h.available())

    def test_adsbfi_429_cools_down(self):
        statuses = []

        class Net:
            def request(self, url, **kw):
                return NetResponse(429, {"retry-after": "42"}, b"", url)

        p = AdsbFi(Net(), logging.getLogger("t"), lambda **kw: statuses.append(kw))
        with self.assertRaises(NetError):
            p.point(51.5, -0.5, 40)
        self.assertFalse(p.available("point"))
        self.assertGreaterEqual(p.health.info(time.time())["cooling_down_s"], 40)
        self.assertFalse(statuses[-1]["ok"])

    def test_adsbfi_urls_are_valid(self):
        seen = []

        class Net:
            def request(self, url, **kw):
                seen.append(url)
                return NetResponse(200, {}, (FIX / "adsbfi_empty.json").read_bytes(), url)

        p = AdsbFi(Net(), logging.getLogger("t"), lambda **kw: None)
        p.point(51.123456, -0.98765, 400)
        p.hexes(["4cacb5", "a1b2c3"])
        p.sqk("7700")
        self.assertEqual(seen[0], "https://opendata.adsb.fi/api/v3/lat/51.1235/lon/-0.9877/dist/250")
        self.assertEqual(seen[1], "https://opendata.adsb.fi/api/v2/icao/4cacb5,a1b2c3")
        self.assertEqual(seen[2], "https://opendata.adsb.fi/api/v2/sqk/7700")

    def test_opensky_credits_and_oauth(self):
        self.assertEqual(OpenSky.credits_for((0, 50, 5, 54)), 1)
        self.assertEqual(OpenSky.credits_for((0, 40, 10, 50)), 2)
        self.assertEqual(OpenSky.credits_for((-180, -85, 180, 85)), 4)
        seen = []

        class Net:
            def request(self, url, **kw):
                seen.append((url, kw.get("headers"), kw.get("data")))
                if "openid-connect/token" in url:
                    return NetResponse(200, {}, b'{"access_token": "tok123", "expires_in": 1800}', url)
                return NetResponse(200, {"x-rate-limit-remaining": "3990"}, (FIX / "opensky_states.json").read_bytes(), url)

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        store = Store(Path(tmp.name) / "t.db")
        try:
            p = OpenSky(Net(), logging.getLogger("t"), lambda **kw: None, store=store, client_id="id", client_secret="s3cr3t!")
            now, obs = p.point(51.45, -0.5, 30)
            self.assertIn("46b82a", {o["hex"] for o in obs})
            for o in obs:      # the box answer is trimmed back to the circle
                if "lat" in o:
                    self.assertLessEqual(geo.haversine_km(51.45, -0.5, o["lat"], o["lon"]), 30 * 1.852 + 0.01)
            token_call, states_call = seen[0], seen[1]
            self.assertIn(b"client_secret=s3cr3t%21", token_call[2])
            self.assertEqual(states_call[1], {"Authorization": "Bearer tok123"})
            self.assertEqual(p.budget.info()["provider_remaining"], 3990)

            class Down:
                def request(self, url, **kw):
                    raise NetError("auth.opensky-network.org: HTTP 401", status=401)

            q = OpenSky(Down(), logging.getLogger("t"), lambda **kw: None, store=store, client_id="id", client_secret="s3cr3t!")
            self.assertIsNone(q._bearer())
            self.assertNotIn("s3cr3t", q.auth_error)
        finally:
            store.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
