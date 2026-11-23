"""Alerts tests: areas, rule presets, the engine (baseline, first_seen, dedupe, escalation, episodes, GPS
onset, rate limit and digests, storm cones, outbreaks, rule edits, failures), the HTTP API (CRUD
validation, inbox, recent, counts, settings, guards) and the alerts_recent AI tool. No network.

Run: python -m unittest tests.test_alerts -v
"""

from __future__ import annotations

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
from meridian.httpserver import make_server
from meridian.net import Net
from meridian.registry import App
from meridian.scheduler import Scheduler
from meridian.store import Store
from meridian.modules import alerts
from meridian.modules.alerts import areas as A
from meridian.modules.alerts import db
from meridian.modules.alerts import rules as R
from meridian.modules.alerts.engine import decide

NOW = 1_795_400_000.0
H = 3600.0
DAY = 86400.0
LOG = logging.getLogger("meridian.test.alerts")

BALTIC = {"type": "Polygon", "coordinates": [[[10, 54], [30, 54], [30, 66], [10, 66], [10, 54]]]}


def ev(kind="earthquake", id="q1", t=NOW, sev=5, lat=60.0, lon=20.0, title=None, **extra):
    return {"kind": kind, "id": id, "title": title or f"{kind} {id}", "subtitle": "test", "lat": lat, "lon": lon,
            "time": t, "severity": sev, "layer": "test.layer", **extra}


def quake(id, mag, t=NOW, lat=60.0, lon=20.0):
    return ev("earthquake", id, t=t, sev=max(0, round(2 * mag - 6)), lat=lat, lon=lon, title=f"M {mag}, test", magnitude=mag)


class Harness(unittest.TestCase):
    """An App with the core, fake event providers (hazards, flights, ships, intel) and the alerts module."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(None)
        self.app = App(cfg, Store(Path(self.tmp.name) / "t.db"), Net(cfg), LOG, Scheduler(LOG), DatasetManager(LOG))
        register_core(self.app)
        self.feeds: dict[str, list] = {}
        for name in ("hazards", "flights", "ships", "intel"):
            mod = self.app.module(name)
            self.app.modules[name].ok = True
            self.feeds[name] = []
            mod.events(lambda since, n=name: [dict(e) for e in self.feeds[n]])
        self.m = self.app.module("alerts")
        alerts.register(self.m)
        self.app.modules["alerts"].ok = True
        self.engine = alerts.get_engine()
        self.engine.storm_geometry = lambda e: None
        self.engine.outbreak_places = lambda e: []
        self.store = self.app.store

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # helpers ---------------------------------------------------------------------------------
    def snap(self, now):
        return self.app.history.snapshot(now=now)

    def run_at(self, now):
        out = self.engine.run(now=now)
        self.assertTrue(out.get("ok"), out)
        return out

    def watch(self, rules, area=None, name="Baltic", now=NOW + 10):
        a = A.parse_area(area or {"type": "polygon", "geometry": BALTIC})
        return db.create_watchlist(self.store, name=name, area=a, color=None, rules=db.clean_rules(rules), now=now)

    def alerts(self, **kw):
        return db.recent(self.store, since=0, limit=1000, **kw)

    def reasons(self):
        return [(a["ref"], a["reason"]) for a in reversed(self.alerts())]


# =============================================================================================
# areas
# =============================================================================================
class AreaTests(unittest.TestCase):
    def test_drawn_polygon_feature(self):
        feature = {"type": "Feature", "bbox": [10, 54, 30, 66], "properties": {"mode": "polygon", "area_km2": 1},
                   "geometry": {"type": "Polygon", "coordinates": [[[10, 54], [10, 66], [30, 66], [30, 54], [10, 54]]]}}
        area = A.parse_area({"type": "polygon", "geometry": feature})
        ring = area.geometry["coordinates"][0]
        self.assertEqual(ring[0], ring[-1])
        self.assertGreater(A._signed_area(ring[:-1]), 0)                 # turned counter-clockwise
        self.assertTrue(area.contains(60, 20))
        self.assertFalse(area.contains(50, 20))
        self.assertFalse(area.contains(60, 35))
        self.assertEqual(area.bbox(), [10, 54, 30, 66])
        self.assertTrue(area.summary().startswith("Polygon, "))

    def test_antimeridian_rectangle(self):
        g = {"type": "Polygon", "coordinates": [[[170, -25], [195, -25], [195, -5], [170, -5], [170, -25]]]}
        area = A.parse_area({"type": "rectangle", "geometry": g})
        self.assertTrue(area.contains(-12, -168.8))      # 191.2 in continuous longitudes
        self.assertTrue(area.contains(-12, 175))
        self.assertFalse(area.contains(-12, 160))
        self.assertFalse(area.contains(-12, -160))
        w, s, e, n = area.bbox()
        self.assertGreater(w, e)                          # wrapped bbox crosses 180
        self.assertEqual((w, e), (170, -165))
        self.assertEqual(area.view_bbox()[2], 195)

    def test_polygon_shifted_by_a_turn_is_normalised(self):
        g = {"type": "Polygon", "coordinates": [[[370, 0], [380, 0], [380, 10], [370, 10], [370, 0]]]}
        area = A.parse_area({"type": "polygon", "geometry": g})
        self.assertEqual(area.geometry["coordinates"][0][0][0], 10)
        self.assertTrue(area.contains(5, 15))

    def test_circle_and_place_use_great_circle_distance(self):
        area = A.parse_area({"type": "circle", "center": [0.0, 51.5], "radius_km": 100})
        self.assertTrue(area.contains(51.5, 1.4))         # about 97 km east
        self.assertFalse(area.contains(51.5, 1.5))        # about 104 km
        feature = {"type": "Feature", "properties": {"mode": "circle", "center": [179.5, 0], "radius_km": 200},
                   "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}
        across = A.parse_area({"type": "circle", "geometry": feature})
        self.assertTrue(across.contains(0, -179.5))       # across the antimeridian
        place = A.parse_area({"type": "place", "place": {"name": "  Dover ", "lat": 51.13, "lon": 1.31}, "radius_km": 25})
        self.assertEqual(place.place["name"], "Dover")
        self.assertEqual(place.summary(), "25 km around Dover")
        self.assertTrue(place.contains(51.2, 1.4))
        self.assertEqual(len(place.geometry["coordinates"][0]), A.CIRCLE_STEPS + 1)

    def test_world(self):
        area = A.parse_area({"type": "world"})
        self.assertTrue(area.contains(-89, 179))
        self.assertFalse(area.contains(91, 0))
        self.assertIsNone(area.bbox())
        self.assertTrue(area.intersects(BALTIC))

    def test_bad_areas(self):
        bad = [None, [], {}, {"type": "hexagon"}, {"type": "polygon"},
               {"type": "polygon", "geometry": {"type": "Point", "coordinates": [0, 0]}},
               {"type": "polygon", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 0]]]}},
               {"type": "polygon", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [2, 2], [0, 0]]]}},
               {"type": "polygon", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 95], [2, 0], [0, 0]]]}},
               {"type": "polygon", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], ["x", 1], [2, 0], [0, 0]]]}},
               {"type": "polygon", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1e308 * 10, 1], [2, 0]]]}},
               {"type": "polygon", "geometry": {"type": "Polygon", "coordinates": [[[i * 0.001, (i % 2) * 0.001] for i in range(2500)]]}},
               {"type": "circle", "center": [0, 0]}, {"type": "circle", "center": [0, 0], "radius_km": 0},
               {"type": "circle", "center": [0, 0], "radius_km": 6000}, {"type": "circle", "center": [0, 99], "radius_km": 5},
               {"type": "circle", "center": [0, 0], "radius_km": True}, {"type": "circle", "radius_km": 5},
               {"type": "place", "radius_km": 5}, {"type": "place", "place": {"name": "", "lat": 1, "lon": 1}, "radius_km": 5},
               {"type": "place", "place": {"name": "X", "lat": "1", "lon": 1}, "radius_km": 5}]
        for spec in bad:
            with self.assertRaises(A.AreaError, msg=repr(spec)[:80]):
                A.parse_area(spec)

    def test_round_trip(self):
        for spec in ({"type": "world"}, {"type": "circle", "center": [10, 20], "radius_km": 30},
                     {"type": "place", "place": {"name": "Oslo", "lat": 59.9, "lon": 10.7}, "radius_km": 50},
                     {"type": "polygon", "geometry": BALTIC}):
            a = A.parse_area(spec)
            b = A.Area.from_json(json.loads(json.dumps(a.to_json())))
            self.assertEqual(a.to_json(), b.to_json())

    def test_intersections(self):
        area = A.parse_area({"type": "polygon", "geometry": BALTIC})
        cone = {"type": "Polygon", "coordinates": [[[25, 50], [35, 50], [35, 58], [25, 58], [25, 50]]]}   # corner overlap
        far = {"type": "Polygon", "coordinates": [[[40, 40], [45, 40], [45, 45], [40, 45], [40, 40]]]}
        cross = {"type": "Polygon", "coordinates": [[[5, 60], [40, 60], [40, 61], [5, 61], [5, 60]]]}     # no vertex inside
        self.assertTrue(area.intersects(cone))
        self.assertFalse(area.intersects(far))
        self.assertTrue(area.intersects(cross))
        self.assertTrue(area.intersects({"type": "MultiPolygon", "coordinates": [far["coordinates"], cone["coordinates"]]}))
        self.assertFalse(area.intersects(None))
        self.assertTrue(area.touches_line([[5, 60], [40, 61]]))      # crosses, no point inside
        self.assertFalse(area.touches_line([[40, 40], [45, 45]]))
        pacific = A.parse_area({"type": "rectangle", "geometry": {"type": "Polygon", "coordinates": [[[170, -25], [195, -25], [195, -5], [170, -5], [170, -25]]]}})
        west_cone = {"type": "Polygon", "coordinates": [[[-170, -20], [-160, -20], [-160, -10], [-170, -10], [-170, -20]]]}
        self.assertTrue(pacific.intersects(west_cone))                # matched across 180 with a 360 shift
        circle = A.parse_area({"type": "circle", "center": [20, 60], "radius_km": 50})
        self.assertTrue(circle.intersects(A.bbox_polygon([19.9, 59.9, 20.4, 60.4])))
        self.assertFalse(circle.intersects(A.bbox_polygon([25, 59, 25.5, 59.5])))


# =============================================================================================
# presets
# =============================================================================================
class PresetTests(unittest.TestCase):
    def p(self, preset, **params):
        pr = R.get(preset)
        return pr, pr.clean_params(params)

    def test_quakes(self):
        pr, p = self.p("quakes", min_magnitude=5.5)
        self.assertTrue(pr.match(quake("a", 5.5), p))
        self.assertFalse(pr.match(quake("a", 5.4), p))
        self.assertTrue(pr.match(ev("earthquake", title="M 6.1, Japan"), p))     # title fallback
        self.assertFalse(pr.match(ev("fire", magnitude=7), p))
        self.assertEqual(pr.phrase(p), "Earthquakes of magnitude 5.5 or more")

    def test_sanctioned_ships_description_names_every_list(self):
        # CONTRACT.md 5.7: say "sanctions lists" (or the list names), not just "OFAC": the rule fires on any list.
        d = R.PRESETS["ships_sanctioned"].description
        self.assertIn("sanctions lists", d)
        self.assertNotIn("OFAC-listed", d)
        for name in ("OFAC", "UK", "UN", "EU"):
            self.assertIn(name, d)

    def test_aircraft_ships_conflict(self):
        pr, p = self.p("aircraft")
        self.assertTrue(pr.match(ev("aircraft", sev=6, event="emergency"), p))
        self.assertFalse(pr.match(ev("aircraft", sev=6, event="sanctioned"), p))
        pr, p = self.p("aircraft", min_severity=7)
        self.assertFalse(pr.match(ev("aircraft", sev=6, event="emergency"), p))
        pr, p = self.p("ships_dark")
        self.assertTrue(pr.match(ev("ship", category="dark_ship"), p))
        self.assertFalse(pr.match(ev("ship", category="sanctioned_vessel"), p))
        pr, p = self.p("ships_sanctioned")
        self.assertTrue(pr.match(ev("ship", category="sanctioned_vessel"), p))
        self.assertTrue(pr.match(ev("ship", category="sanctioned_vessel", lists=["UK"]), p))   # not only OFAC
        pr, p = self.p("conflict")
        self.assertEqual(p, {"min_severity": 6})
        self.assertTrue(pr.match(ev("conflict_event", sev=6), p))
        self.assertFalse(pr.match(ev("conflict_event", sev=5.9), p))
        self.assertFalse(pr.match(ev("news", sev=9), p))

    def test_hazard_presets(self):
        pr, p = self.p("warnings")
        self.assertTrue(pr.match(ev("warning", sev=6, severity_text="Severe"), p))
        self.assertFalse(pr.match(ev("warning", sev=4, severity_text="Moderate"), p))
        pr, p = self.p("warnings", extreme_only=True)
        self.assertFalse(pr.match(ev("warning", sev=7, severity_text="Severe"), p))
        self.assertTrue(pr.match(ev("warning", sev=9, severity_text="Extreme"), p))
        pr, p = self.p("volcanoes", min_severity=6)
        self.assertTrue(pr.match(ev("volcano", sev=6), p))
        self.assertFalse(pr.match(ev("volcano", sev=3), p))
        pr, p = self.p("storms", min_severity=5)
        self.assertTrue(pr.match(ev("storm", sev=8), p))
        self.assertFalse(pr.match(ev("storm", sev=4), p))
        pr, p = self.p("outbreaks")
        self.assertTrue(pr.match(ev("outbreak"), p))

    def test_gps_and_any(self):
        pr, p = self.p("gps")
        self.assertTrue(pr.match(dict(ev("gps_jam", sev=4), module="flights"), p))
        self.assertTrue(pr.match(dict(ev("gps_jam", sev=5), module="ships"), p))
        pr, p = self.p("gps", min_severity=5, ships=False)
        self.assertFalse(pr.match(dict(ev("gps_jam", sev=4), module="flights"), p))
        self.assertFalse(pr.match(dict(ev("gps_jam", sev=6), module="ships"), p))
        pr, p = self.p("any", min_severity=9)
        self.assertTrue(pr.match(ev("news", sev=9), p))
        self.assertFalse(pr.match(ev("news", sev=8.9), p))

    def test_param_validation(self):
        for preset, params in (("quakes", {"min_magnitude": 5.2}), ("quakes", {"min_magnitude": "5"}),
                               ("quakes", {"colour": 1}), ("storms", {"cones": "yes"}), ("any", {"min_severity": 11}),
                               ("conflict", {"min_severity": True}), ("conflict", [])):
            with self.assertRaises(R.RuleError, msg=f"{preset} {params}"):
                R.get(preset).clean_params(params)
        with self.assertRaises(R.RuleError):
            R.get("everything")
        self.assertEqual(R.get("storms").clean_params(None), {"min_severity": 3, "cones": True})

    def test_episodes_and_cells(self):
        self.assertEqual(R.episode_gap_s(ev("aircraft")), 6 * H)
        self.assertEqual(R.episode_gap_s(ev("ship", category="sanctioned_vessel")), 48 * H)
        self.assertEqual(R.episode_gap_s(ev("gps_jam")), 72 * H)
        self.assertIsNone(R.episode_gap_s(ev("earthquake")))
        a = dict(ev("gps_jam", id="ships:59.91,28.65", lat=59.91, lon=28.65), module="ships")
        b = dict(ev("gps_jam", id="ships:59.95,28.70", lat=59.95, lon=28.70), module="ships")
        self.assertEqual(R.dedupe_ref(a), R.dedupe_ref(b))
        self.assertEqual(R.dedupe_ref(dict(ev("gps_jam", id="gpsjam:60:25:0.5"), module="flights")), "gpsjam:60:25:0.5")


class DecideTests(unittest.TestCase):
    """The pure decision for one matching event."""

    def test_baseline_new_entered_matched(self):
        e = ev("earthquake", sev=5)
        e["first_seen"] = NOW - 100
        self.assertIsNone(decide(e, None, baseline=True, watch_from=NOW, last_check=None, level=2)[0])
        e["first_seen"] = NOW + 100
        self.assertEqual(decide(e, None, baseline=True, watch_from=NOW, last_check=None, level=2)[0], "new")
        self.assertEqual(decide(e, None, baseline=False, watch_from=NOW - 1000, last_check=NOW, level=2)[0], "new")
        e["first_seen"] = NOW - 500
        self.assertEqual(decide(e, None, baseline=False, watch_from=NOW - 1000, last_check=NOW, level=2)[0], "matched")
        s = dict(ev("ship"), first_seen=NOW - 500)
        self.assertEqual(decide(s, None, baseline=False, watch_from=NOW - 1000, last_check=NOW, level=2)[0], "entered")

    def test_existing_record(self):
        rec = {"severity": 5, "event_time": NOW, "level": 2, "alerted": 1}
        e = dict(ev("earthquake", sev=6, t=NOW + 10), first_seen=NOW)
        reason, new, _ = decide(e, rec, baseline=False, watch_from=0, last_check=NOW, level=2)
        self.assertIsNone(reason)
        self.assertEqual(new["severity"], 5)                     # the reference stays at the alerted severity
        e["severity"] = 7
        reason, new, extra = decide(e, rec, baseline=False, watch_from=0, last_check=NOW, level=2)
        self.assertEqual((reason, new["severity"], extra), ("escalated", 7, 5))
        old = dict(ev("earthquake", sev=8, t=NOW - 3 * DAY), first_seen=NOW - 3 * DAY)
        self.assertIsNone(decide(old, rec, baseline=False, watch_from=0, last_check=NOW, level=2, now=NOW)[0])
        self.assertEqual(decide(old, rec, baseline=False, watch_from=0, last_check=NOW, level=2, now=NOW)[1]["severity"], 8)
        a = dict(ev("aircraft", sev=5, t=NOW + 7 * H), first_seen=NOW)
        reason, _, gap = decide(a, rec, baseline=False, watch_from=0, last_check=NOW, level=2)
        self.assertEqual((reason, gap), ("again", 7 * H))
        storm = dict(ev("storm", sev=5, t=NOW + 600), first_seen=NOW)
        self.assertEqual(decide(storm, dict(rec, level=1), baseline=False, watch_from=0, last_check=NOW, level=2)[0], "closer")


# =============================================================================================
# the engine
# =============================================================================================
class EngineTests(Harness):
    def test_baseline_is_silent_then_new_events_alert(self):
        self.feeds["hazards"] = [quake("old", 6.0, t=NOW - H)]
        self.snap(NOW)
        self.watch([{"preset": "quakes"}], now=NOW + 10)
        out = self.run_at(NOW + 20)
        self.assertEqual((out["alerts"], out["absorbed"]), (0, 1))
        self.feeds["hazards"].append(quake("q2", 6.5, t=NOW + 200))
        self.snap(NOW + 300)
        out = self.run_at(NOW + 320)
        self.assertEqual(out["alerts"], 1)
        a = self.alerts()[0]
        self.assertEqual((a["ref"], a["reason"], a["area"], a["rule"]), ("q2", "new", "Baltic", "Earthquakes of magnitude 5 or more"))
        self.assertEqual(a["why"], "New in Baltic: magnitude 6.5, threshold 5")
        self.assertEqual(a["entity"]["kind"], "earthquake")
        self.assertEqual(a["entity"]["id"], "q2")
        # the same event in later snapshots: no second alert
        for k in range(1, 4):
            self.snap(NOW + 300 + k * 300)
            self.run_at(NOW + 320 + k * 300)
        self.assertEqual(len(self.alerts()), 1)

    def test_events_arriving_before_the_first_check_still_alert(self):
        self.snap(NOW)
        self.watch([{"preset": "quakes"}], now=NOW + 10)
        self.feeds["hazards"] = [quake("q1", 5.5, t=NOW + 50)]
        self.snap(NOW + 60)                                 # first seen after the watchlist was made
        self.run_at(NOW + 100)                              # ...but before its first check
        self.assertEqual(self.reasons(), [("q1", "new")])

    def test_late_event_with_old_time_alerts_by_first_seen(self):
        self.snap(NOW)
        self.watch([{"preset": "conflict"}])
        self.run_at(NOW + 20)
        self.feeds["intel"] = [ev("conflict_event", "late", t=NOW - 2 * DAY, sev=7)]
        self.snap(NOW + 300)
        self.run_at(NOW + 320)
        self.assertEqual(self.reasons(), [("late", "new")])
        # older than the 7-day window: left out
        self.feeds["intel"].append(ev("conflict_event", "ancient", t=NOW - 8 * DAY, sev=9))
        self.snap(NOW + 600)
        self.run_at(NOW + 620)
        self.assertEqual(len(self.alerts()), 1)

    def test_outside_the_area_and_below_threshold_are_ignored(self):
        self.snap(NOW)
        self.watch([{"preset": "quakes", "params": {"min_magnitude": 6}}])
        self.run_at(NOW + 20)
        self.feeds["hazards"] = [quake("far", 7.0, lat=10, lon=10), quake("small", 5.9)]
        self.snap(NOW + 300)
        self.run_at(NOW + 320)
        self.assertEqual(self.alerts(), [])

    def test_escalation_needs_two_points(self):
        self.snap(NOW)
        self.watch([{"preset": "conflict"}])
        self.run_at(NOW + 20)
        t = NOW + 100
        for i, sev in enumerate((6, 7, 8, 9, 10)):
            self.feeds["intel"] = [ev("conflict_event", "c1", t=t, sev=sev)]
            self.snap(NOW + 300 * (i + 1))
            self.run_at(NOW + 300 * (i + 1) + 20)
        self.assertEqual(self.reasons(), [("c1", "new"), ("c1", "escalated"), ("c1", "escalated")])
        whys = [a["why"] for a in reversed(self.alerts())]
        self.assertEqual(whys[1], "Severity rose in Baltic: severity 6 to 8")
        self.assertEqual(whys[2], "Severity rose in Baltic: severity 8 to 10")

    def test_old_reports_rescored_later_stay_quiet(self):
        self.feeds["intel"] = [ev("conflict_event", "old", t=NOW - 3 * DAY, sev=5), ev("conflict_event", "recent", t=NOW - H, sev=5),
                               ev("conflict_event", "old2", t=NOW - 3 * DAY, sev=6)]
        self.snap(NOW)
        self.watch([{"preset": "conflict"}])
        self.run_at(NOW + 20)                                                     # old2 is the baseline
        self.feeds["intel"] = [ev("conflict_event", "old", t=NOW - 3 * DAY, sev=7), ev("conflict_event", "recent", t=NOW - H, sev=7),
                               ev("conflict_event", "old2", t=NOW - 3 * DAY, sev=9)]
        self.snap(NOW + 300)                                                      # a module re-scores its reports
        self.run_at(NOW + 320)
        self.assertEqual(self.reasons(), [("recent", "matched")])
        self.assertTrue(self.alerts()[0]["why"].startswith("Now meets your rule in Baltic"))

    def test_known_event_that_starts_matching_alerts_once(self):
        self.feeds["ships"] = [ev("ship", "273000001", t=NOW, sev=6, lat=50, lon=0, category="sanctioned_vessel")]
        self.snap(NOW)
        self.watch([{"preset": "ships_sanctioned"}])
        self.run_at(NOW + 20)                               # outside the Baltic: nothing
        self.feeds["ships"] = [ev("ship", "273000001", t=NOW + 250, sev=6, lat=58, lon=18, category="sanctioned_vessel")]
        self.snap(NOW + 300)
        self.run_at(NOW + 320)
        self.assertEqual(self.reasons(), [("273000001", "entered")])
        self.assertTrue(self.alerts()[0]["why"].startswith("Moved into Baltic"))
        for k, (lat, lon) in enumerate(((50, 0), (58, 18))):   # out and back in within 48 h: still one alert
            self.feeds["ships"] = [ev("ship", "273000001", t=NOW + 600 + k * 300, sev=6, lat=lat, lon=lon, category="sanctioned_vessel")]
            self.snap(NOW + 600 + k * 300)
            self.run_at(NOW + 620 + k * 300)
        self.assertEqual(len(self.alerts()), 1)

    def test_recurring_aircraft_emergency_is_a_new_episode(self):
        self.snap(NOW)
        self.watch([{"preset": "aircraft"}])
        self.run_at(NOW + 20)
        for k in range(3):                                  # one emergency, reported for 15 minutes
            self.feeds["flights"] = [ev("aircraft", "4ca2b3", t=NOW + 100 + k * 300, sev=7, event="emergency")]
            self.snap(NOW + 300 + k * 300)
            self.run_at(NOW + 320 + k * 300)
        self.feeds["flights"] = [ev("aircraft", "4ca2b3", t=NOW + 8 * H, sev=7, event="emergency")]   # the next day's
        self.snap(NOW + 8 * H)
        self.run_at(NOW + 8 * H + 20)
        self.assertEqual(self.reasons(), [("4ca2b3", "new"), ("4ca2b3", "again")])
        self.assertIn("after 8 hours", self.alerts()[0]["why"])

    def test_gps_cells_new_returning_worse_and_chronic(self):
        cell = lambda id, sev, t, lat=60.25, lon=25.25: dict(ev("gps_jam", id, t=t, sev=sev, lat=lat, lon=lon),
                                                            bbox=[lon - 0.25, lat - 0.25, lon + 0.25, lat + 0.25])
        self.feeds["flights"] = [cell("gpsjam:60:25:0.5", 6, NOW), cell("gpsjam:61:26:0.5", 4, NOW, 61.25, 26.25)]
        self.snap(NOW)
        self.watch([{"preset": "gps"}])
        self.assertEqual(self.run_at(NOW + 20)["absorbed"], 2)
        for hour in range(1, 25):                           # chronic: reported every hour for a day
            t = NOW + hour * H
            self.feeds["flights"] = [cell("gpsjam:60:25:0.5", 6, t), cell("gpsjam:61:26:0.5", 4, t, 61.25, 26.25)]
            self.snap(t)
            self.run_at(t + 20)
        self.assertEqual(self.alerts(), [])
        t = NOW + 25 * H                                    # a brand new cell, and the second one gets worse
        self.feeds["flights"] = [cell("gpsjam:60:25:0.5", 6, t), cell("gpsjam:61:26:0.5", 6, t, 61.25, 26.25),
                                 cell("gpsjam:59:20:0.5", 5, t, 59.25, 20.25)]
        self.snap(t)
        self.run_at(t + 20)
        self.assertEqual(sorted(self.reasons()), [("gpsjam:59:20:0.5", "new"), ("gpsjam:61:26:0.5", "escalated")])
        worse = [a for a in self.alerts() if a["reason"] == "escalated"][0]
        self.assertEqual(worse["why"], "Got worse in Baltic: severity 4 to 6")
        quiet_until = t + 4 * DAY                           # the first cell goes quiet for four days, then returns
        self.feeds["flights"] = [cell("gpsjam:60:25:0.5", 6, quiet_until)]
        self.snap(quiet_until)
        self.run_at(quiet_until + 20)
        back = [a for a in self.alerts() if a["reason"] == "again"]
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["why"], "Back in Baltic after 4 days without interference: severity 6")

    def test_gps_cell_outline_touching_a_small_area(self):
        self.snap(NOW)
        self.watch([{"preset": "gps"}], area={"type": "circle", "center": [25.05, 60.05], "radius_km": 5})
        self.run_at(NOW + 20)
        # the cell's centre (60.25, 25.25) is 25 km away, but its 0.5° outline covers the circle
        self.feeds["flights"] = [dict(ev("gps_jam", "gpsjam:60:25:0.5", t=NOW + 100, sev=6, lat=60.25, lon=25.25),
                                      bbox=[25.0, 60.0, 25.5, 60.5])]
        self.snap(NOW + 300)
        self.run_at(NOW + 320)
        self.assertEqual(self.reasons(), [("gpsjam:60:25:0.5", "new")])

    def test_ship_spoofing_hotspots_fold_into_cells(self):
        self.snap(NOW)
        self.watch([{"preset": "gps"}])
        self.run_at(NOW + 20)
        for k, (lat, lon) in enumerate(((59.91, 28.65), (59.93, 28.61), (59.95, 28.70))):
            self.feeds["ships"] = [dict(ev("gps_jam", f"ships:{lat:.2f},{lon:.2f}", t=NOW + 100 + k * 300, sev=5, lat=lat, lon=lon),
                                        category="gnss_interference", radius_km=10)]
            self.snap(NOW + 300 + k * 300)
            self.run_at(NOW + 320 + k * 300)
        self.assertEqual(len(self.alerts()), 1)

    def test_rate_limit_and_digest(self):
        self.snap(NOW)
        self.watch([{"preset": "quakes"}], area={"type": "world"}, name="World")
        self.run_at(NOW + 20)
        self.feeds["hazards"] = [quake(f"q{i}", 5.0 + i * 0.1, t=NOW + 100 + i) for i in range(13)]
        self.snap(NOW + 300)
        out = self.run_at(NOW + 320)
        self.assertEqual((out["alerts"], out["held_back"]), (10, 3))
        inbox = db.inbox(self.store, limit=100)
        digests = [a for a in inbox if a["digest"]]
        self.assertEqual(len(inbox), 11)
        self.assertEqual(len(digests), 1)
        self.assertEqual((digests[0]["count"], digests[0]["title"]), (3, "3 more alerts in World"))
        delivered = {a["ref"] for a in inbox if not a["digest"]}
        self.assertEqual(delivered, {f"q{i}" for i in range(3, 13)})          # the most severe first
        folded = db.inbox(self.store, digest=digests[0]["id"], limit=10)
        self.assertEqual({a["ref"] for a in folded}, {"q0", "q1", "q2"})
        self.assertTrue(all(a["folded"] for a in folded))
        self.assertIn("Most severe: M 5.2, test", digests[0]["why"])
        self.assertEqual(len(self.alerts()), 13)                               # recent: every fired alert, no digest
        self.assertEqual(len(self.alerts(digests=True)), 14)
        db.mark(self.store, [digests[0]["id"]], read=True, now=NOW + 330)
        self.feeds["hazards"] += [quake("q13", 5.5, t=NOW + 400), quake("q14", 5.6, t=NOW + 401)]
        self.snap(NOW + 600)
        out = self.run_at(NOW + 620)
        self.assertEqual((out["alerts"], out["held_back"]), (0, 2))
        digest = [a for a in db.inbox(self.store, limit=100) if a["digest"]]
        self.assertEqual(len(digest), 1)
        self.assertEqual((digest[0]["count"], digest[0]["read"]), (5, False))    # grew, unread again
        self.feeds["hazards"].append(quake("q15", 5.1, t=NOW + 2 * H))
        self.snap(NOW + 2 * H)
        out = self.run_at(NOW + 2 * H + 20)
        self.assertEqual((out["alerts"], out["held_back"]), (1, 0))               # an hour later: delivered again

    def test_rate_limit_setting(self):
        db.save_settings(self.store, {"rate_limit": 5}, NOW)
        self.snap(NOW)
        self.watch([{"preset": "quakes"}], area={"type": "world"}, name="World")
        self.run_at(NOW + 20)
        self.feeds["hazards"] = [quake(f"q{i}", 5.0, t=NOW + 100 + i) for i in range(7)]
        self.snap(NOW + 300)
        self.assertEqual(self.run_at(NOW + 320)["alerts"], 5)

    def test_storm_cone_then_centre_then_stronger(self):
        cone = {"type": "Polygon", "coordinates": [[[25, 52], [35, 52], [35, 58], [25, 58], [25, 52]]]}
        self.engine.storm_geometry = lambda e: {"cone": cone, "buffers": [], "points": [], "track": []} if e.get("id") == "nhc:al1" else None
        self.snap(NOW)
        self.watch([{"preset": "storms"}])
        self.run_at(NOW + 20)
        self.feeds["hazards"] = [ev("storm", "nhc:al1", t=NOW + 100, sev=5, lat=50, lon=40), ev("storm", "nhc:al2", t=NOW + 100, sev=9, lat=50, lon=40)]
        self.snap(NOW + 300)
        self.run_at(NOW + 320)
        self.assertEqual(self.reasons(), [("nhc:al1", "new")])           # al2 has no cone touching
        self.assertIn("forecast cone", self.alerts()[0]["why"])
        self.feeds["hazards"] = [ev("storm", "nhc:al1", t=NOW + 600, sev=5, lat=57, lon=27)]
        self.snap(NOW + 900)
        self.run_at(NOW + 920)
        self.feeds["hazards"] = [ev("storm", "nhc:al1", t=NOW + 1200, sev=7, lat=58, lon=26)]
        self.snap(NOW + 1500)
        self.run_at(NOW + 1520)
        self.assertEqual(self.reasons(), [("nhc:al1", "new"), ("nhc:al1", "closer"), ("nhc:al1", "escalated")])
        # cones off: only the centre counts
        self.engine.storm_geometry = lambda e: {"cone": cone, "buffers": [], "points": [], "track": []}
        self.watch([{"preset": "storms", "params": {"cones": False}}], name="Centre only", now=NOW + 2000)
        self.run_at(NOW + 2020)
        self.feeds["hazards"].append(ev("storm", "nhc:al3", t=NOW + 2100, sev=6, lat=50, lon=40))
        self.snap(NOW + 2400)
        self.run_at(NOW + 2420)
        self.assertEqual([a["ref"] for a in self.alerts() if a["area"] == "Centre only"], [])

    def test_outbreak_matches_any_named_place(self):
        self.engine.outbreak_places = lambda e: [(0.0, 0.0), (59.4, 24.7)]      # the second is Tallinn
        self.snap(NOW)
        self.watch([{"preset": "outbreaks"}])
        self.run_at(NOW + 20)
        self.feeds["hazards"] = [ev("outbreak", "who:2026-DON700", t=NOW + 100, sev=6, lat=0.0, lon=0.0)]
        self.snap(NOW + 300)
        self.run_at(NOW + 320)
        self.assertEqual(self.reasons(), [("who:2026-DON700", "new")])

    def test_editing_a_rule_or_area_starts_again_quietly(self):
        self.feeds["hazards"] = [quake("q55", 5.5)]
        self.snap(NOW)
        wid = self.watch([{"preset": "quakes", "params": {"min_magnitude": 6}}])
        self.run_at(NOW + 20)
        db.set_rule(self.store, wid, "quakes", params={"min_magnitude": 5}, enabled=None, now=NOW + 100)
        self.snap(NOW + 300)
        out = self.run_at(NOW + 320)
        self.assertEqual((out["alerts"], out["absorbed"]), (0, 1))                 # it was there all along
        self.feeds["hazards"].append(quake("far", 6.0, lat=0, lon=100))
        self.snap(NOW + 600)
        self.run_at(NOW + 620)
        db.update_watchlist(self.store, wid, now=NOW + 700, area=A.parse_area({"type": "world"}))
        self.snap(NOW + 900)
        self.run_at(NOW + 920)
        self.assertEqual(self.alerts(), [])
        # paused rules and watchlists are skipped
        db.update_watchlist(self.store, wid, now=NOW + 1000, enabled=False)
        self.feeds["hazards"].append(quake("new", 6.0, t=NOW + 1100))
        self.snap(NOW + 1200)
        self.assertEqual(self.run_at(NOW + 1220)["rules"], 0)
        db.update_watchlist(self.store, wid, now=NOW + 1300, enabled=True)         # resuming starts quietly too
        self.snap(NOW + 1500)
        self.run_at(NOW + 1520)
        self.assertEqual(self.alerts(), [])

    def test_rows_written_after_the_query_are_checked_next_time(self):
        self.snap(NOW)
        self.watch([{"preset": "quakes"}])
        self.run_at(NOW + 20)
        self.snap(NOW + 300)
        self.run_at(NOW + 320)                              # watermark: the snapshot at NOW + 300
        self.feeds["hazards"] = [quake("tail", 6.0, t=NOW + 250)]
        self.snap(NOW + 300)                                # a late chunk of that same snapshot
        self.run_at(NOW + 340)
        self.assertEqual([a["ref"] for a in self.alerts()], ["tail"])

    def test_failures_are_reported_and_survived(self):
        self.snap(NOW)
        self.watch([{"preset": "quakes"}])

        class Broken:
            last = {}

            def events(self, *a, **k):
                raise RuntimeError("database is locked")

        real = self.app.history
        self.app._history = Broken()
        with self.assertLogs("meridian.alerts", level="WARNING"):
            out = self.engine.run(now=NOW + 20)
        self.assertFalse(out["ok"])
        self.assertIn("database is locked", out["error"])
        self.assertFalse(self.app.sources["alerts.engine"]["ok"])
        self.app._history = real
        self.assertTrue(self.engine.run(now=NOW + 40)["ok"])
        self.assertTrue(self.app.sources["alerts.engine"]["ok"])

    def test_unknown_or_missing_modules(self):
        self.snap(NOW)
        self.watch([{"preset": "aircraft"}, {"preset": "any", "params": {"min_severity": 9}}], area={"type": "world"}, name="World")
        self.app.modules["flights"].ok = False
        from meridian.modules.alerts.api import preset_list
        avail = {p["id"]: p["available"] for p in preset_list(self.app)}
        self.assertFalse(avail["aircraft"])
        self.assertTrue(avail["any"])
        self.store.execute("INSERT INTO alerts_rules(watchlist_id, preset, params, enabled, watch_from, created_at, updated_at) "
                           "VALUES(1, 'retired', '{}', 1, 0, 0, 0)")
        self.assertTrue(self.run_at(NOW + 20)["ok"])

    def test_alerts_is_never_an_events_provider(self):
        self.assertNotIn("alerts", [mod for mod, _fn in self.app._events])
        self.assertIn("alerts_recent", [t["name"] for t in self.app.ai_tools()])

    def test_ai_tool(self):
        self.snap(NOW)
        self.watch([{"preset": "quakes"}])
        self.run_at(NOW + 20)
        self.feeds["hazards"] = [quake("q1", 6.3, t=NOW + 100)]
        self.snap(NOW + 300)
        self.run_at(NOW + 320)
        from meridian.modules.alerts.api import ai_recent
        self.assertIn("switched off", ai_recent(self.m, {})["error"])   # opt-in by default
        db.save_settings(self.store, {"ai_share": True}, NOW)
        out = ai_recent(self.m, {"hours": 24}, clock=lambda: NOW + 400)
        self.assertEqual(out["count"], 1)
        a = out["alerts"][0]
        self.assertEqual((a["area"], a["kind"], a["id"], a["held_back"]), ("Baltic", "earthquake", "q1", False))
        self.assertTrue(a["time_utc"].endswith("UTC"))
        self.assertEqual(ai_recent(self.m, {"area": "pacific"}, clock=lambda: NOW + 400)["count"], 0)
        self.assertIn("error", ai_recent(self.m, {"hours": "lots"}))
        db.save_settings(self.store, {"ai_share": False}, NOW)
        self.assertIn("switched off", ai_recent(self.m, {})["error"])

    def test_matches_now_preview(self):
        self.feeds["hazards"] = [quake("q1", 6.0), quake("q2", 5.0, lat=0, lon=0)]
        self.snap(NOW)
        wid = self.watch([{"preset": "quakes"}])
        w = db.get_watchlist(self.store, wid, NOW)
        out = self.engine.matches_now(w, NOW + 30)
        self.assertEqual([e["entity"]["id"] for e in out["events"]], ["q1"])

    def test_inbox_paging_follows_the_order_when_a_digest_grows(self):
        with self.store.tx() as c:
            db.insert_alert(c, 100, title="a1", watchlist_id=1)
            did = db.insert_alert(c, 110, title="digest", reason="digest", watchlist_id=1)
            for i in range(4):
                db.insert_alert(c, 200 + i * 10, title=f"b{i}", watchlist_id=2)
            c.execute("UPDATE alerts_alerts SET updated_at = 500 WHERE id = ?", (did,))     # it grew later
        seen, before = [], None
        for _ in range(5):
            page = db.inbox(self.store, limit=2, before=before[0] if before else None, before_t=before[1] if before else None)
            if not page:
                break
            seen += [a["title"] for a in page]
            before = (page[-1]["id"], page[-1]["updated_at"])
        self.assertEqual(seen, ["digest", "b3", "b2", "b1", "b0", "a1"])

    def test_prune(self):
        self.snap(NOW)
        self.watch([{"preset": "quakes"}])
        self.run_at(NOW + 20)
        self.feeds["hazards"] = [quake("q1", 6.0, t=NOW + 100)]
        self.snap(NOW + 300)
        self.run_at(NOW + 320)
        out = db.prune(self.store, NOW + 61 * DAY)
        self.assertEqual((out["alerts"], out["seen"]), (1, 1))
        self.assertEqual(self.alerts(), [])


# =============================================================================================
# HTTP API
# =============================================================================================
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MERIDIAN_DATA_DIR"] = cls.tmp.name
        cfg = Config(None)
        app = App(cfg, Store(Path(cls.tmp.name) / "t.db"), Net(cfg), LOG, Scheduler(LOG), DatasetManager(LOG))
        register_core(app)
        cls.feed = []
        hz = app.module("hazards")
        app.modules["hazards"].ok = True
        hz.events(lambda since: [dict(e) for e in cls.feed])
        cls.m = app.module("alerts")
        alerts.register(cls.m)
        app.modules["alerts"].ok = True
        cls.engine = alerts.get_engine()
        cls.engine.storm_geometry = lambda e: None
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
        cls.tmp.cleanup()

    def setUp(self):
        for t in ("alerts_alerts", "alerts_seen", "alerts_rules", "alerts_watchlists", "alerts_settings"):
            self.app.store.execute(f"DELETE FROM {t}")

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def post(self, path, body, headers=None):
        hdrs = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}", "X-Meridian": "1"}
        if headers is not None:
            hdrs = headers
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), method="POST",
                                     headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as err:
            raw = err.read() or b"{}"
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, {}

    def create(self, **body):
        body.setdefault("area", {"type": "polygon", "geometry": BALTIC})
        st, out = self.post("/api/alerts/watchlists", {"action": "create", **body})
        self.assertEqual(st, 200, out)
        return out["watchlist"]

    def fire(self, n=1, mag=6.0):
        """Make n quake alerts in the first watchlist through the real history and engine (in the past, as
        real snapshots are)."""
        now = time.time() - 30
        self.app.history.snapshot(now=now)
        self.engine.run(now=now + 1)
        base = len(self.feed)
        self.feed.extend(quake(f"http{base + i}", mag, t=now + 2) for i in range(n))
        self.app.history.snapshot(now=now + 3)
        return self.engine.run(now=now + 4)

    def test_guards(self):
        body = {"action": "create", "area": {"type": "world"}}
        good = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}", "X-Meridian": "1"}
        for drop in ("X-Meridian", "Origin"):
            st, _ = self.post("/api/alerts/watchlists", body, {k: v for k, v in good.items() if k != drop})
            self.assertEqual(st, 403, drop)
        st, _ = self.post("/api/alerts/watchlists", body, {**good, "Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, _ = self.post("/api/alerts/watchlists", body, {**good, "Content-Type": "text/plain"})
        self.assertIn(st, (400, 415))
        self.assertEqual(self.get("/api/alerts/watchlists")[1]["watchlists"], [])

    def test_create_validation(self):
        cases = [({"action": "create"}, "MISSING_FIELD"), ({"action": "create", "area": "world"}, "BAD_FIELD"),
                 ({"action": "create", "area": {"type": "ocean"}}, "BAD_FIELD"),
                 ({"action": "create", "area": {"type": "world"}, "rules": [{"preset": "unicorns"}]}, "BAD_FIELD"),
                 ({"action": "create", "area": {"type": "world"}, "rules": [{"preset": "quakes", "params": {"min_magnitude": 5.1}}]}, "BAD_FIELD"),
                 ({"action": "create", "area": {"type": "world"}, "rules": "quakes"}, "BAD_FIELD"),
                 ({"action": "create", "area": {"type": "world"}, "name": 7}, "BAD_FIELD"),
                 ({"action": "create", "area": {"type": "world"}, "color": "red"}, "BAD_FIELD"),
                 ({"action": "make"}, "BAD_FIELD")]
        for body, code in cases:
            st, out = self.post("/api/alerts/watchlists", body)
            self.assertEqual((st, out["error"]["code"]), (400, code), body)
            self.assertTrue(out["error"]["message"].endswith("."), out)
        w = self.create(name="  North   Sea  " + "x" * 100, rules=["quakes", {"preset": "storms", "params": {"cones": False}}])
        self.assertTrue(w["name"].startswith("North Sea"))
        self.assertLessEqual(len(w["name"]), db.NAME_MAX)
        self.assertEqual([r["preset"] for r in w["rules"]], ["quakes", "storms"])
        self.assertEqual(w["rules"][1]["params"], {"min_severity": 3, "cones": False})
        place = self.create(area={"type": "place", "place": {"name": "Dover", "lat": 51.13, "lon": 1.31}, "radius_km": 25})
        self.assertEqual((place["name"], place["area"]["radius_km"]), ("Dover", 25))
        self.assertEqual(len(place["area"]["center"]), 2)
        world = self.create(area={"type": "world"})
        self.assertEqual(world["name"], "World")
        self.assertNotIn("geometry", world["area"])

    def test_update_rules_and_delete(self):
        w = self.create(name="Baltic", rules=["quakes"])
        wid = w["id"]
        for bad in ({"action": "update", "id": "abc"}, {"action": "update", "id": -1}, {"action": "update", "id": 1.5},
                    {"action": "update", "id": True}, {"action": "update", "id": wid, "name": "  "},
                    {"action": "update", "id": wid, "enabled": "yes"}, {"action": "update", "id": wid, "area": {"type": "circle"}}):
            st, out = self.post("/api/alerts/watchlists", bad)
            self.assertEqual(st, 400, bad)
        self.assertEqual(self.post("/api/alerts/watchlists", {"action": "update", "id": 999999, "name": "x"})[0], 404)
        st, out = self.post("/api/alerts/watchlists", {"action": "update", "id": wid, "name": "Baltic Sea", "color": "#FFB547",
                                                       "show_on_map": False, "enabled": False})
        self.assertEqual(st, 200)
        self.assertEqual((out["watchlist"]["name"], out["watchlist"]["color"], out["watchlist"]["show_on_map"],
                          out["watchlist"]["enabled"]), ("Baltic Sea", "#ffb547", False, False))
        st, out = self.post("/api/alerts/rules", {"action": "set", "watchlist_id": wid, "preset": "conflict", "params": {"min_severity": 8}})
        self.assertEqual(st, 200)
        rules = {r["preset"]: r for r in out["watchlist"]["rules"]}
        self.assertEqual(rules["conflict"]["summary"], "Conflict events of severity 8 or more")
        st, out = self.post("/api/alerts/rules", {"action": "set", "watchlist_id": wid, "preset": "quakes", "enabled": False})
        self.assertFalse({r["preset"]: r for r in out["watchlist"]["rules"]}["quakes"]["enabled"])
        for bad, code in (({"action": "set", "watchlist_id": wid, "preset": "nope"}, 400),
                          ({"action": "set", "watchlist_id": wid, "preset": "quakes", "params": [1]}, 400),
                          ({"action": "set", "watchlist_id": wid, "preset": "quakes", "params": {"min_magnitude": 99}}, 400),
                          ({"action": "set", "watchlist_id": 424242, "preset": "quakes"}, 404),
                          ({"action": "delete", "id": 424242}, 404), ({"action": "drop"}, 400)):
            self.assertEqual(self.post("/api/alerts/rules", bad)[0], code, bad)
        st, one = self.get(f"/api/alerts/watchlists/{wid}")
        self.assertEqual((st, one["name"]), (200, "Baltic Sea"))
        self.assertEqual(self.get("/api/alerts/watchlists/12345")[0], 404)
        self.assertEqual(self.get("/api/alerts/watchlists/abc")[0], 400)
        st, out = self.post("/api/alerts/rules", {"action": "delete", "id": rules["conflict"]["id"]})
        self.assertEqual(st, 200)
        st, out = self.post("/api/alerts/watchlists", {"action": "delete", "id": wid})
        self.assertEqual(st, 200)
        self.assertEqual(self.get("/api/alerts/watchlists")[1]["watchlists"], [])
        self.assertEqual(self.app.store.scalar("SELECT COUNT(*) FROM alerts_rules"), 0)

    def test_recent_matches_the_contract(self):
        self.create(name="Baltic", rules=["quakes"])
        self.fire(2)
        st, body = self.get("/api/alerts/recent")
        self.assertEqual(st, 200)
        self.assertEqual(body["count"], 2)
        for a in body["alerts"]:
            for key in ("id", "rule", "area", "title", "time", "severity", "entity"):
                self.assertIn(key, a)
            self.assertIsInstance(a["rule"], str)
            self.assertEqual(a["area"], "Baltic")
            self.assertEqual(a["entity"]["kind"], "earthquake")
            self.assertTrue({"id", "lat", "lon", "layer", "title"} <= set(a["entity"]))
        self.assertGreaterEqual(body["alerts"][0]["time"], body["alerts"][1]["time"])
        since_ms = int((time.time() + 3600) * 1000)
        self.assertEqual(self.get(f"/api/alerts/recent?since={since_ms}")[1]["count"], 0)
        self.assertEqual(self.get("/api/alerts/recent?limit=1")[1]["count"], 1)
        for q in ("since=yesterday", "since=-5", "limit=0", "limit=1001"):
            self.assertEqual(self.get(f"/api/alerts/recent?{q}")[0], 400, q)
        self.post("/api/alerts/inbox", {"action": "test"})
        self.assertEqual(self.get("/api/alerts/recent")[1]["count"], 2)        # test alerts are not "fired"

    def test_inbox_counts_and_clear(self):
        self.create(name="Baltic", rules=["quakes"])
        self.fire(3)
        st, c = self.get("/api/alerts/counts")
        self.assertEqual((c["unread"], c["inbox"], c["watchlists"]), (3, 3, 1))
        st, box = self.get("/api/alerts/inbox?limit=2")
        self.assertEqual(len(box["alerts"]), 2)
        self.assertIn("next_before", box)
        older = self.get(f"/api/alerts/inbox?before={box['next_before']}&before_t={box['next_before_t']}")[1]["alerts"]
        self.assertEqual(len(older), 1)
        first = box["alerts"][0]["id"]
        st, out = self.post("/api/alerts/inbox", {"action": "read", "ids": [first]})
        self.assertEqual((st, out["unread"]), (200, 2))
        self.assertEqual(self.post("/api/alerts/inbox", {"action": "unread", "ids": [first]})[1]["unread"], 3)
        self.assertEqual(self.get("/api/alerts/inbox?unread=1")[1]["unread"], 3)
        newer = self.get(f"/api/alerts/inbox?after={first - 1}")[1]["alerts"]
        self.assertEqual(newer[0]["id"], first)                                 # oldest first after an id
        self.assertEqual(self.post("/api/alerts/inbox", {"action": "read_all"})[1]["unread"], 0)
        self.assertEqual(self.post("/api/alerts/inbox", {"action": "clear"})[1]["inbox"], 0)
        self.assertEqual(self.get("/api/alerts/recent")[1]["count"], 3)        # still there for the briefing
        for bad in ({"action": "read"}, {"action": "read", "ids": ["x"]}, {"action": "explode"}):
            self.assertEqual(self.post("/api/alerts/inbox", bad)[0], 400, bad)
        st, out = self.post("/api/alerts/inbox", {"action": "test"})
        self.assertEqual((st, out["unread"]), (200, 1))
        test = self.get("/api/alerts/inbox")[1]["alerts"][0]
        self.assertEqual((test["reason"], test["title"]), ("test", "Test alert"))

    def test_settings(self):
        st, s = self.get("/api/alerts/settings")
        self.assertEqual(s, db.DEFAULT_SETTINGS)
        st, s = self.post("/api/alerts/settings", {"quiet_hours": {"on": True, "start": "23:30"}, "rate_limit": 20,
                                                   "notify": {"min_severity": 6}})
        self.assertEqual(st, 200)
        self.assertEqual(s["quiet_hours"], {"on": True, "start": "23:30", "end": "07:00", "allow_extreme": True})
        self.assertEqual((s["rate_limit"], s["notify"]), (20, {"on": True, "min_severity": 6}))
        for bad in ({"volume": 11}, {"rate_limit": 7}, {"rate_limit": True}, {"quiet_hours": {"start": "25:00"}},
                    {"quiet_hours": {"on": "yes"}}, {"quiet_hours": {"colour": 1}}, {"notify": {"min_severity": 12}},
                    {"notify": []}, {"ai_share": 1}):
            self.assertEqual(self.post("/api/alerts/settings", bad)[0], 400, bad)
        self.assertEqual(self.get("/api/alerts/settings")[1]["rate_limit"], 20)

    def test_check_presets_status_and_matches(self):
        w = self.create(name="Baltic", rules=["quakes"])
        st, out = self.post("/api/alerts/check", {})
        self.assertEqual((st, out["ok"]), (200, True))
        self.assertEqual(self.post("/api/alerts/check", {})[0], 429)
        st, p = self.get("/api/alerts/presets")
        self.assertEqual([x["id"] for x in p["presets"]], list(R.ORDER))
        self.assertFalse({x["id"]: x for x in p["presets"]}["aircraft"]["available"])   # no flights module here
        self.assertTrue({x["id"]: x for x in p["presets"]}["quakes"]["available"])
        st, s = self.get("/api/alerts/status")
        self.assertEqual((st, s["watchlists"]), (200, 1))
        st, m = self.get(f"/api/alerts/watchlists/{w['id']}/matches")
        self.assertEqual(st, 200)
        self.assertIn("count", m)


if __name__ == "__main__":
    unittest.main()
