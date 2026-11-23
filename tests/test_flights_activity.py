"""Air activity detectors on synthetic history: military surges by region, GPS interference trends and
airport movement drops, with baseline learning, the benign cases (routine patrols at usual levels,
weekly training schedules, trainers and helicopters, chronic interference, quiet hours), coverage
controls, the history seed, routes, AI tools and events. No network.

Run: python -m unittest tests.test_flights_activity -v
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.registry import App, HTTPError, Request
from meridian.scheduler import Scheduler
from meridian.store import Store
from meridian.modules.flights import activity_rules as R
from meridian.modules.flights.activity import ActivityService

H = 3600
DAY = 86400
SLOT = 600
# Monday 23 November 2026, 15:05 UTC: the last 3 complete hours are 12:00 to 15:00
NOW = calendar.timegm((2026, 11, 23, 15, 5, 0))
END = NOW - NOW % H
BALTIC = (57.0, 19.5)                  # inside the Baltic Sea region
EFHK = (60.3172, 24.9633)


class FakeNet:
    def set_host_interval(self, host, seconds):
        pass

    def request(self, url, **kw):
        from meridian.net import NetError
        raise NetError("no network in tests", url=url)

    def get_json(self, url, **kw):
        return self.request(url, **kw)


class Base(unittest.TestCase):
    overrides = {"FLIGHTS_GPSJAM_SAMPLER": "0", "FLIGHTS_MIL_SAMPLER": "0", "FLIGHTS_AIRPORT_WATCH": "0"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MERIDIAN_DATA_DIR"] = self.tmp.name
        cfg = Config(None, overrides=dict(self.overrides))
        log = logging.getLogger("meridian.test")
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.app = App(cfg, self.store, FakeNet(), log, Scheduler(log), DatasetManager(log))
        self.m = self.app.module("flights")
        from meridian.modules import flights
        flights.register(self.m)
        self.app.modules["flights"].ok = True
        self.svc = self.m.flights
        self.act: ActivityService = self.svc.activity
        self.act.clock = lambda: NOW
        self.api = self.svc.activity_api

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

    # -- synthetic military history (hourly tallies, as the sampler would have written them) ----------
    def mil_hours(self, region: str, counts: dict[int, int], *, roles: dict | None = None, world: int = 300,
                  covered: set | None = None, seeded: bool = False):
        """``counts`` {hour_start: counted aircraft}; every hour in ``covered`` (default: all of them)
        gets 6 snapshots of ``world`` military aircraft worldwide."""
        rows, cov = [], []
        for hs, n in counts.items():
            r = dict(roles or {"transport": n})
            rows.append((region, hs // H, n, json.dumps(r), ""))
        for hs in (covered if covered is not None else set(counts)):
            cov.append((hs // H, 0b111111, world * 6, 1 if seeded else 0))
        with self.store.tx() as c:
            c.executemany("INSERT OR REPLACE INTO flights_mil_hour(region, hour, n, roles, hexes) VALUES(?,?,?,?,?)", rows)
            c.executemany("INSERT OR REPLACE INTO flights_mil_cov(hour, mask, world_sum, seeded) VALUES(?,?,?,?)", cov)

    def window_hexes(self, region: str, n: int, prefix: str = "ae"):
        """Distinct aircraft in the current 3-hour window (the hexes column)."""
        hexes = " ".join(f"{prefix}{i:04x}:C" for i in range(n))
        for k in range(1, 4):
            self.store.execute("UPDATE flights_mil_hour SET hexes = ? WHERE region = ? AND hour = ?",
                               (hexes, region, (END - k * H) // H))

    # -- synthetic airport history (hourly movements, as the snapshots would have written them) ----------
    def airport_days(self, ident: str, days: int, moves, *, high=10, last=None):
        rows = []
        for d in range(1, days + 1):
            for k in range(24):
                hs = END - d * DAY + (k - 12) * H
                n = moves(hs)
                rows.append((ident, hs // H, 0b111111, n // 2 + n % 2, n // 2, 5, high, ""))
        for hs, (n, hi, mask) in (last or {}).items():
            rows.append((ident, hs // H, mask, n // 2 + n % 2, n // 2, 1, hi, ""))
        with self.store.tx() as c:
            c.executemany("INSERT OR REPLACE INTO flights_apt_hour(ident, hour, mask, arr, dep, gnd, high, sets) "
                          "VALUES(?,?,?,?,?,?,?,?)", rows)

    @staticmethod
    def busy(hs):
        hr = time.gmtime(hs).tm_hour
        return 30 + (hs // DAY) % 4 if 5 <= hr <= 22 else 2      # quiet nights (curfew-like)

    def history_days(self, days: int, per_hour, *, region: str = "baltic", start_offset: int = 0, **kw):
        counts = {}
        for d in range(start_offset + 1, start_offset + days + 1):
            for k in range(24):
                hs = END - d * DAY + (k - 12) * H
                counts[hs] = per_hour(hs, d)
        self.mil_hours(region, counts, **kw)
        return counts


# =================================================================================================
# roles and regions
# =================================================================================================
class RolesAndRegions(unittest.TestCase):
    def test_roles(self):
        self.assertEqual(R.role_of("K35R"), "tanker")
        self.assertEqual(R.role_of("R135"), "isr")
        self.assertEqual(R.role_of("P8"), "isr")
        self.assertEqual(R.role_of("C17"), "transport")
        self.assertEqual(R.role_of("F16"), "fighter")
        self.assertEqual(R.role_of("B52"), "bomber")
        self.assertEqual(R.role_of("PC21"), "trainer")
        self.assertEqual(R.role_of("H60"), "helicopter")
        self.assertEqual(R.role_of(None, "military", "heli"), "helicopter")
        self.assertEqual(R.role_of("ZZZZ", "military", "heavy"), "transport")
        self.assertEqual(R.role_of(None, "military", "uav"), "isr")
        self.assertEqual(R.role_of(None), "other")
        self.assertFalse(R.counted("trainer"))
        self.assertFalse(R.counted("helicopter"))
        self.assertTrue(R.counted("tanker"))

    def test_regions_contain_known_places(self):
        cases = {(54.38, 18.47): "baltic", (37.57, 126.98): "korea", (25.03, 121.56): "taiwan_strait",
                 (25.29, 51.53): "persian_gulf", (46.48, 30.73): "black_sea", (32.01, 34.89): "east_med",
                 (52.17, 20.97): "east_flank", (33.3, 44.4): "iraq_syria", (18.4, -66.1): "caribbean"}
        for (lat, lon), rid in cases.items():
            self.assertIn(rid, R.regions_at(lat, lon), (lat, lon))
        self.assertEqual(R.regions_at(51.47, -0.46), [])            # London is in no watched region
        for reg in R.REGIONS:
            self.assertEqual(reg.ring[0], reg.ring[-1], reg.id)
            self.assertTrue(reg.contains(reg.label[1], reg.label[0]), reg.id)
        self.assertEqual(len({r.id for r in R.REGIONS}), len(R.REGIONS))
        self.assertGreaterEqual(len(R.REGIONS), 10)

    def test_place_names(self):
        self.assertEqual(R.place_name(55.0, 20.0), "Kaliningrad and the southern Baltic")
        self.assertEqual(R.place_name(-40.0, -10.0), "40.0° S, 10.0° W")


# =================================================================================================
# military surges
# =================================================================================================
class MilitarySurges(Base):
    def steady(self, hs, d):
        return 4 + (d % 3) - 1                       # routine patrols: 3 to 5 aircraft an hour

    def test_learning_until_seven_days(self):
        self.history_days(4, self.steady)
        self.mil_hours("baltic", {END - k * H: 12 for k in (1, 2, 3)})
        self.window_hexes("baltic", 18)
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["state"], "learning")
        self.assertEqual(st["status"], "Learning baseline: 4 of 7 days")
        self.assertIn("nothing is flagged", st["evidence"])
        self.act.update_episodes(NOW)
        self.assertEqual(self.act.events(NOW - DAY), [])

    def test_routine_patrols_at_usual_levels_stay_quiet(self):
        self.history_days(14, self.steady)
        self.mil_hours("baltic", {END - k * H: 4 + (k % 2) for k in (1, 2, 3)})
        self.window_hexes("baltic", 6)
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["state"], "normal")
        self.assertEqual(st["baseline_days"], 14)
        # two weeks: the same-weekday baseline (13.5) is ready, and is used on its own (like with like)
        self.assertEqual(st["profile"], "week")
        self.assertAlmostEqual(st["usual"], 13.5)
        self.act.update_episodes(NOW)
        self.assertEqual(self.act.events(NOW - DAY), [])

    def test_surge_is_flagged_with_ratio_confidence_and_evidence(self):
        self.history_days(14, self.steady)
        self.mil_hours("baltic", {END - k * H: 16 for k in (1, 2, 3)},
                       roles={"tanker": 6, "isr": 4, "transport": 6})
        self.window_hexes("baltic", 20)
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["state"], "unusual", st["status"])
        self.assertAlmostEqual(st["ratio"], 48 / 13.5)          # against the same-weekday level
        self.assertGreaterEqual(st["z"], 3.0)
        self.assertEqual(st["distinct"], 20)
        self.assertIn(st["confidence"], ("medium", "high"))
        self.assertTrue(st["confidence_reasons"])
        self.assertGreaterEqual(st["severity"], 4)
        self.assertLessEqual(st["severity"], 6)
        self.assertIn("16 military aircraft an hour", st["evidence"])
        self.assertIn("3.6 times the usual", st["evidence"])
        self.assertIn("Mondays", st["evidence"])
        self.assertTrue(any("Exercises" in x for x in st["innocent"]))
        self.assertIn("broadcast ADS-B", st["limits"])
        # the episode becomes an air_activity event
        self.act.update_episodes(NOW)
        ev = self.act.events(NOW - DAY)
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual(e["kind"], "air_activity")
        self.assertEqual(e["category"], "military_surge")
        self.assertTrue(e["id"].startswith("flights:surge:baltic:"))
        self.assertIn("over the Baltic Sea", e["title"])
        self.assertEqual(e["layer"], "flights.activity")
        self.assertEqual(len(e["bbox"]), 4)
        for word in ("illegal", "attack", "confirmed", "suspicious"):
            self.assertNotIn(word, (e["title"] + e["subtitle"]).lower())
        # a later run continues the same episode rather than opening another
        self.act.update_episodes(NOW + 600)
        self.assertEqual([x["id"] for x in self.act.events(NOW - DAY)], [e["id"]])
        # and the core event collection carries it
        kinds = {(x["kind"], x.get("category")) for x in self.app.collect_events(NOW - DAY)}
        self.assertIn(("air_activity", "military_surge"), kinds)

    def test_too_few_aircraft_is_never_a_surge(self):
        self.history_days(14, lambda hs, d: 1)        # one aircraft an hour, usually
        self.mil_hours("baltic", {END - k * H: 3 for k in (1, 2, 3)})
        self.window_hexes("baltic", 3)                # three aircraft, all window long
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertNotEqual(st["state"], "unusual")

    def test_weekly_training_schedule_is_learned(self):
        """Every Monday 12:00 to 15:00 brings a training wave: from two weeks of history the
        hour-of-week baseline knows it, so this Monday's wave is not a surge."""
        monday = lambda hs: time.gmtime(hs).tm_wday == 0 and 12 <= time.gmtime(hs).tm_hour < 15  # noqa: E731
        self.history_days(21, lambda hs, d: 16 if monday(hs) else self.steady(hs, d))
        self.mil_hours("baltic", {END - k * H: 16 for k in (1, 2, 3)})
        self.window_hexes("baltic", 20)
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["profile"], "week")
        self.assertIn("Mondays", st["profile_text"])
        self.assertNotEqual(st["state"], "unusual")
        # the same wave on a Tuesday afternoon would be unusual: move the clock a day on
        tuesday = NOW + DAY
        end2 = END + DAY
        self.mil_hours("baltic", {end2 - k * H: 16 for k in (1, 2, 3)})
        self.mil_hours("baltic", {END + k * H: 4 for k in range(0, 21)})
        hexes = " ".join(f"ae{i:04x}:C" for i in range(20))
        for k in (1, 2, 3):
            self.store.execute("UPDATE flights_mil_hour SET hexes = ? WHERE region = 'baltic' AND hour = ?",
                               (hexes, (end2 - k * H) // H))
        st2 = self.act.refresh_regions(tuesday)["baltic"]
        self.assertEqual(st2["state"], "unusual")

    def test_weekly_training_in_the_second_week_is_held_back(self):
        """Days 7 to 13: no same-weekday baseline yet, but last Monday's identical wave is in the data,
        so this Monday's wave is only 'busier than usual' and raises no event. A wave that was not there a
        week ago (the Tuesday) is still a surge."""
        monday = lambda hs: time.gmtime(hs).tm_wday == 0 and 12 <= time.gmtime(hs).tm_hour < 15  # noqa: E731
        self.history_days(10, lambda hs, d: 16 if monday(hs) else self.steady(hs, d))
        self.mil_hours("baltic", {END - k * H: 16 for k in (1, 2, 3)})
        self.window_hexes("baltic", 20)
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["profile"], "day")
        self.assertEqual(st["state"], "notable", st["status"])
        self.assertIn("like the same hours a week ago", st["status"])
        self.assertIn("weekly schedule", st["evidence"])
        self.assertTrue(any("weekly schedule" in r for r in st["confidence_reasons"]))
        self.act.update_episodes(NOW)
        self.assertEqual(self.act.events(NOW - DAY), [])
        tuesday, end2 = NOW + DAY, END + DAY
        self.mil_hours("baltic", {END + k * H: 4 for k in range(0, 21)})
        self.mil_hours("baltic", {end2 - k * H: 16 for k in (1, 2, 3)})
        hexes = " ".join(f"ae{i:04x}:C" for i in range(20))
        for k in (1, 2, 3):
            self.store.execute("UPDATE flights_mil_hour SET hexes = ? WHERE region = 'baltic' AND hour = ?",
                               (hexes, (end2 - k * H) // H))
        self.assertEqual(self.act.refresh_regions(tuesday)["baltic"]["state"], "unusual")

    def test_weekends_are_compared_with_weekends(self):
        """Four weeks with weekdays at 6 military aircraft an hour and weekends at 1. A Saturday at 5 an
        hour from 15 aircraft is 5 times the Saturday level: unusual, and the evidence quotes the Saturday
        median it was judged against (not the busier weekday level)."""
        sat = NOW - 2 * DAY
        end = sat - sat % H
        counts = {}
        for d in range(1, 29):
            for k in range(24):
                hs = end - d * DAY + (k - 12) * H
                counts[hs] = 1 if time.gmtime(hs).tm_wday >= 5 else 6
        counts.update({end - k * H: 5 for k in (1, 2, 3)})
        self.mil_hours("baltic", counts)
        hexes = " ".join(f"ae{i:04x}:C" for i in range(15))
        for k in (1, 2, 3):
            self.store.execute("UPDATE flights_mil_hour SET hexes = ? WHERE region = 'baltic' AND hour = ?",
                               (hexes, (end - k * H) // H))
        st = self.act.refresh_regions(sat)["baltic"]
        self.assertEqual(st["profile"], "week")
        self.assertAlmostEqual(st["usual_per_hour"], 1.0)
        self.assertIn("Usual for this time on Saturdays (median of 4 weeks) is 1 an hour", st["evidence"])
        self.assertEqual(st["state"], "unusual", st["status"])
        self.assertAlmostEqual(st["ratio"], 5.0)
        # the role usuals follow the same profile
        self.assertAlmostEqual(st["roles"]["transport"]["usual"], 3.0)

    def test_no_multiple_of_almost_nothing(self):
        """Taiwan Strait: the window is empty on 10 of 14 days, now 12 aircraft-hours from 6 aircraft. The
        usual is under one aircraft-hour, so no multiple is published; titles and labels lead with the
        count."""
        self.history_days(14, lambda hs, d: 1 if d in (3, 5, 9, 11) else 0, region="taiwan_strait")
        self.mil_hours("taiwan_strait", {END - k * H: 4 for k in (1, 2, 3)})
        self.window_hexes("taiwan_strait", 6)
        st = self.act.refresh_regions(NOW)["taiwan_strait"]
        self.assertEqual(st["state"], "unusual", st["status"])
        self.assertEqual(st["usual"], 0)
        self.assertIsNone(st["ratio"])
        self.assertEqual(st["status"], "Unusual: 4 an hour where usually there are none")
        self.assertIn("is none.", st["evidence"])
        for text in (st["status"], st["evidence"]):
            self.assertNotIn("times the usual", text)
        self.act.update_episodes(NOW)
        ev = self.act.events(NOW - DAY)[0]
        self.assertEqual(ev["title"], "More military aircraft than usual over the Taiwan Strait: 4 an hour, usually none")
        self.assertTrue(ev["subtitle"].startswith("12 aircraft-hours over the last 3 hours, 6 different aircraft"))
        self.assertIsNone(ev["ratio"])
        labels = {f["properties"]["id"]: f["properties"]["text"] for f in self.api.map_fc()["features"]
                  if f["properties"]["kind"] == "region_label"}
        self.assertEqual(labels["taiwan_strait"], "Taiwan Strait 4/h")
        brief = [r for r in self.get("/api/flights/activity")["regions"] if r["id"] == "taiwan_strait"][0]
        self.assertIsNone(brief["ratio"])
        # with a real usual the multiple stays, after the count
        self.history_days(14, self.steady)
        self.mil_hours("baltic", {END - k * H: 16 for k in (1, 2, 3)})
        self.window_hexes("baltic", 20)
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(R.region_label_text(st, 3), "Baltic Sea 16/h (3.6×)")
        title, sub = R.surge_words(st, "over the Baltic Sea")
        self.assertEqual(title, "More military aircraft than usual over the Baltic Sea: 16 an hour, usually 4.5")
        self.assertTrue(sub.startswith("3.6 times the usual over the last 3 hours, 20 different aircraft"))

    def test_region_chart_usual_is_the_detectors_own(self):
        """The card's usual line is the detector's baseline (the same 3-hour windows and profile, None while
        learning): in the weekly training case the line sits on the Monday wave, not at a quarter of it."""
        monday = lambda hs: time.gmtime(hs).tm_wday == 0 and 12 <= time.gmtime(hs).tm_hour < 15  # noqa: E731
        self.history_days(21, lambda hs, d: 16 if monday(hs) else self.steady(hs, d))
        self.mil_hours("baltic", {END - k * H: 16 for k in (1, 2, 3)})
        self.window_hexes("baltic", 20)
        self.act.detect(NOW, force_gps=True)
        st = self.act.region_states["baltic"]
        self.assertNotEqual(st["state"], "unusual")
        d = self.get("/api/flights/activity/region/baltic")
        by_t = {t: (n, cov, usual) for t, n, cov, usual in d["series"]}
        for k in (1, 2, 3):                                    # Monday 12:00 to 15:00, the compared window
            n, cov, usual = by_t[END - k * H]
            self.assertEqual(n, 16)
            self.assertAlmostEqual(usual, st["usual_per_hour"])
            self.assertAlmostEqual(usual, 16.0)
        # learning: no usual line at all
        with self.store.tx() as c:
            c.execute("DELETE FROM flights_mil_hour WHERE hour < ?", ((END - 3 * DAY) // H,))
            c.execute("DELETE FROM flights_mil_cov WHERE hour < ?", ((END - 3 * DAY) // H,))
        self.act.detect(NOW, force_gps=True)
        self.assertEqual(self.act.region_states["baltic"]["state"], "learning")
        self.assertTrue(all(r[3] is None for r in self.get("/api/flights/activity/region/baltic")["series"]))

    def test_trainers_and_helicopters_are_left_out(self):
        t = END + 60
        items = [(f"ae{i:04x}", *BALTIC, "trainer") for i in range(20)]
        items += [(f"af{i:04x}", *BALTIC, "helicopter") for i in range(10)]
        items += [(f"ad{i:04x}", *BALTIC, "tanker") for i in range(2)]
        self.assertTrue(self.act.add_mil_snapshot(t, items))
        row = self.store.one("SELECT n, roles FROM flights_mil_hour WHERE region = 'baltic' AND hour = ?", (t // H,))
        self.assertEqual(row["n"], 2)
        self.assertEqual(json.loads(row["roles"]), {"helicopter": 10, "tanker": 2, "trainer": 20})

    def test_quiet_periods_count_only_when_meridian_was_running(self):
        # 14 days of history, but on 5 of them Meridian was off for the window's hours
        counts = self.history_days(14, self.steady)
        off_days = {2, 4, 6, 8, 10}
        covered = {hs for hs in counts if not any(END - d * DAY - 4 * H <= hs < END - d * DAY for d in off_days)}
        with self.store.tx() as c:
            c.execute("DELETE FROM flights_mil_cov")
        self.mil_hours("baltic", {hs: n for hs, n in counts.items()}, covered=covered)
        self.mil_hours("baltic", {END - k * H: 4 for k in (1, 2, 3)})
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["baseline_days"], 9)       # the off days are left out, not read as quiet
        # no snapshots in the current window at all: no data, not "quiet"
        with self.store.tx() as c:
            c.execute("DELETE FROM flights_mil_cov WHERE hour >= ?", ((END - 3 * H) // H,))
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["state"], "no_data")

    def test_feed_change_suppresses_surges(self):
        counts = self.history_days(14, self.steady, world=300)
        self.mil_hours("baltic", {END - k * H: 16 for k in (1, 2, 3)}, world=700)   # the whole feed more than doubled
        self.window_hexes("baltic", 20)
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["state"], "feed_changed")
        self.assertIn("worldwide military feed", st["status"])
        self.act.update_episodes(NOW)
        self.assertEqual(self.act.events(NOW - DAY), [])
        self.assertTrue(counts)

    def _traffic(self, share_now: float):
        rows = []
        for d in range(0, 15):
            for k in range(24):
                hs = END - d * DAY - (k + 1) * H
                n = 300 if d else int(300 * share_now)
                for spot in ("kaliningrad", "gulf-of-finland"):
                    rows.append((spot, hs // H, 6, n * 6))
        with self.store.tx() as c:
            c.executemany("INSERT OR REPLACE INTO flights_traffic_hour(spot, hour, polls, total) VALUES(?,?,?,?)", rows)

    def test_receiver_coverage_gain_suppresses_but_a_traffic_drop_does_not(self):
        self.history_days(14, self.steady)
        self.mil_hours("baltic", {END - k * H: 16 for k in (1, 2, 3)})
        self.window_hexes("baltic", 20)
        self._traffic(2.0)                              # all traffic doubled: more receivers, not more activity
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["state"], "coverage_changed")
        self._traffic(0.4)                              # civil traffic collapsed (closures): still a surge
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["state"], "unusual")
        self.assertTrue(any("civil traffic is 40%" in r for r in st["confidence_reasons"]))
        self.assertIsNotNone(st["checks"]["receivers"])

    def test_regions_without_a_coverage_check_say_so(self):
        self.history_days(14, self.steady, region="caribbean")
        self.mil_hours("caribbean", {END - k * H: 16 for k in (1, 2, 3)})
        self.window_hexes("caribbean", 20)
        st = self.act.refresh_regions(NOW)["caribbean"]
        self.assertEqual(st["state"], "unusual")
        self.assertIn("no receiver-coverage check for this region", st["confidence_reasons"])
        self.assertNotEqual(st["confidence"], "high")

    def test_one_snapshot_per_slot_whatever_the_poll_rate(self):
        """Polled every 60 s (map open) or every 10 min (background), an hour gets 6 snapshots and the
        same distinct count."""
        base = END + 0
        fast = [base + i * 60 for i in range(60)]
        slow = [base + DAY + i * 600 for i in range(6)]
        seen = lambda i: [(f"ae{j:04x}", *BALTIC, "tanker") for j in range(i % 5 + 3)]  # noqa: E731
        recorded = [self.act.add_mil_snapshot(t, seen(i // 10)) for i, t in enumerate(fast)]
        self.assertEqual(sum(recorded), 6)
        for i, t in enumerate(slow):
            self.act.add_mil_snapshot(t, seen(i))
        a = self.store.one("SELECT n FROM flights_mil_hour WHERE region='baltic' AND hour=?", (base // H,))["n"]
        b = self.store.one("SELECT n FROM flights_mil_hour WHERE region='baltic' AND hour=?", ((base + DAY) // H,))["n"]
        self.assertEqual(a, b)
        cov = self.store.one("SELECT mask FROM flights_mil_cov WHERE hour=?", (base // H,))
        self.assertEqual(R.popcount(cov["mask"]), 6)

    def test_snapshots_survive_a_restart_within_the_hour(self):
        t = END + 30
        self.act.add_mil_snapshot(t, [("ae0001", *BALTIC, "tanker")])
        again = ActivityService(self.m, self.svc, None, clock=lambda: t + 60)
        self.assertFalse(again.add_mil_snapshot(t + 60, [("ae0002", *BALTIC, "tanker")]))   # same slot
        self.assertTrue(again.add_mil_snapshot(t + 600, [("ae0002", *BALTIC, "tanker")]))
        row = self.store.one("SELECT n, hexes FROM flights_mil_hour WHERE region='baltic' AND hour=?", (t // H,))
        self.assertEqual(row["n"], 2)
        self.assertIn("ae0001:T", row["hexes"])

    def test_observe_mil_uses_the_tracker(self):
        t = time.time()
        obs = [{"hex": "ae1234", "lat": BALTIC[0], "lon": BALTIC[1], "pos_t": t - 5, "alt": 30000, "gnd": False,
                "typ": "K35R", "db_flags": 1},
               {"hex": "ae1235", "lat": BALTIC[0], "lon": BALTIC[1], "pos_t": t - 5, "alt": 0, "gnd": True,
                "typ": "C17", "db_flags": 1},                                    # on the ground: left out
               {"hex": "a0f00d", "lat": BALTIC[0], "lon": BALTIC[1], "pos_t": t - 5, "alt": 9000, "gnd": False,
                "typ": "C17", "db_flags": 1 | 8}]                                 # privacy programme: left out
        res = self.svc.tracker.ingest(obs, "adsbfi")
        self.assertTrue(self.act.observe_mil(res.hexes, t))
        row = self.store.one("SELECT n, roles FROM flights_mil_hour WHERE region='baltic' AND hour=?", (int(t) // H,))
        self.assertEqual(row["n"], 1)
        self.assertEqual(json.loads(row["roles"]), {"tanker": 1})


class HistorySeed(Base):
    def test_seed_uses_only_slots_when_the_world_list_was_polled(self):
        hist = self.svc.history
        self.assertIsNotNone(hist)
        t0 = int(NOW - 30 * H)
        rows, idents = [], []
        # Slots 0 to 5 (the first hour): the worldwide list was polled, 60 military aircraft over the US
        # plus 5 over the Baltic. Slots 6 to 11: only a few military aircraft inside a GPS-sampler circle.
        for s in range(12):
            t = t0 - t0 % 600 + s * 600 + 30
            if s < 6:
                for i in range(60):
                    rows.append((t, 0xAE0000 + i, int(38.0e5), int(-77.0e5), 20000, 3, 2))
                for i in range(5):
                    rows.append((t, 0xAF0000 + i, int(BALTIC[0] * 1e5), int(BALTIC[1] * 1e5), 25000, 3, 2))
            else:
                for i in range(4):
                    rows.append((t, 0xAF1000 + i, int(55.0e5), int(20.0e5), 25000, 3, 2))
        for i in range(5):
            idents.append((0xAF0000 + i, t0 - 3600, t0 + 7200, "", "", "K35R", ""))
        with self.store.tx() as c:
            c.executemany("INSERT INTO flights_history(t, hex, lat, lon, alt, trk, cls, flags) VALUES(?,?,?,?,?,0,?,?)",
                          [(a, b, c_, d, e, f, g) for a, b, c_, d, e, f, g in rows])
            c.executemany("INSERT INTO flights_history_ident(hex, first_t, last_t, cs, reg, typ, descr) VALUES(?,?,?,?,?,?,?)",
                          idents)
        res = self.act.seed_from_history()
        self.assertEqual(res["covered_slots"], 6)
        self.assertEqual(res["hours"], 1)
        cov = self.store.query("SELECT hour, mask, seeded FROM flights_mil_cov")
        self.assertEqual(len(cov), 1)
        self.assertEqual(cov[0]["seeded"], 1)
        self.assertEqual(R.popcount(cov[0]["mask"]), 6)
        row = self.store.one("SELECT n, roles FROM flights_mil_hour WHERE region = 'baltic'")
        self.assertEqual(row["n"], 5)
        self.assertEqual(json.loads(row["roles"]), {"tanker": 5})
        self.assertIsNone(self.act.seed_from_history())          # once only


# =================================================================================================
# GPS interference trends
# =================================================================================================
class GpsTrends(unittest.TestCase):
    def days(self, total, bad, n=7):
        return [(total, bad)] * n

    def test_classify(self):
        c = R.classify_gps
        self.assertEqual(c(60, 18, self.days(200, 60))["label"], "chronic")          # 30% every day, 30% now
        self.assertEqual(c(60, 24, self.days(200, 0))["label"], "new")               # quiet week, 40% now
        self.assertEqual(c(60, 24, self.days(200, 10))["label"], "worse")            # 5% a day, 40% now
        self.assertEqual(c(60, 24, [(200, 0), (200, 0)] + [(0, 0)] * 5)["label"], "learning")
        self.assertEqual(c(60, 9, [(200, 30), (200, 30)] + self.days(200, 0, 5))["label"], "recurring")
        self.assertEqual(c(60, 0, self.days(200, 60))["label"], "easing")
        self.assertEqual(c(60, 0, self.days(200, 0))["label"], "quiet")
        # a cell that swings to 40% most days is chronic, not "worse", when it is at 40% again
        self.assertEqual(c(60, 24, self.days(200, 80))["label"], "chronic")
        # too few samples now: never new
        self.assertEqual(c(8, 4, self.days(200, 0))["label"], "quiet")
        # amber (2 to 10%) every day is chronic too, judged now or not
        self.assertEqual(c(60, 3, self.days(200, 8))["label"], "chronic")
        self.assertEqual(c(5, 0, self.days(200, 8))["label"], "chronic")
        # too few samples now: a chronic cell is not called easing on the strength of a quiet night
        self.assertEqual(c(6, 0, self.days(200, 60))["label"], "chronic")
        t = c(60, 24, self.days(200, 0))
        self.assertIn("New: 40%", R.trend_text(t))
        self.assertEqual(R.GPS_CATEGORY["chronic"], "chronic_interference")

    def test_same_clock_hours_make_a_daily_pattern_chronic(self):
        """The baseline is the same 6 clock hours on each earlier day. A cell affected only at some hours
        of every day is chronic in those hours, not 'worse' against its diluted 24-hour share."""
        c = R.classify_gps
        # Hormuz, copied-back week: 22 of 105 in these hours every day (43 of 545 over each whole day)
        t = c(105, 22, [(105, 22)] * 7, [(545, 43)] * 7, shared_hours=4)
        self.assertEqual(t["label"], "chronic")
        self.assertEqual(t["days_affected"], 7)
        # 60% in these hours on 7 of 7 days (15% over each whole day): chronic, never worse
        self.assertEqual(c(60, 36, [(60, 36)] * 7, [(240, 36)] * 7, shared_hours=6)["label"], "chronic")
        # a chronic cell that more than doubles its worst day in these hours is still worse
        t = c(60, 54, [(60, 20), (60, 24), (60, 18)] + [(60, 22)] * 4, [(240, 30)] * 7, shared_hours=6)
        self.assertEqual(t["label"], "worse")
        self.assertIn("at most 40% on any of them", R.trend_text(t))
        # quiet in these hours but affected at other hours every day: chronic, not easing or quiet
        t = c(60, 0, [(60, 0)] * 7, [(240, 30)] * 7, shared_hours=0)
        self.assertEqual(t["label"], "chronic")
        self.assertIn("at other hours than now", R.trend_text(t))
        self.assertEqual(c(60, 0, [(60, 18)] * 7, shared_hours=0)["label"], "easing")     # chronic in these hours
        # interference at hours that are usually clean, in a cell that is affected at other hours every
        # day: not "new" (the week was not quiet), but worse than these hours have been
        t = c(60, 24, [(60, 0)] * 7, [(240, 30)] * 7, shared_hours=4)
        self.assertEqual(t["label"], "worse")
        self.assertIn("affected at other hours on 7 of the last 7 days", R.trend_text(t))

    def test_one_aircraft_at_a_time_is_not_interference(self):
        """4 affected aircraft-hours in a quiet cell from one aircraft (never 2 affected in the same hour)
        are not new interference; 2 or more affected aircraft in the same hour, twice, are."""
        quiet_week = [(18, 0)] * 7
        t = R.classify_gps(22, 4, quiet_week, shared_hours=0)
        self.assertEqual(t["label"], "quiet")
        self.assertTrue(t["one_aircraft_at_a_time"])
        self.assertIn("one aircraft at a time", R.trend_text(t))
        self.assertEqual(R.classify_gps(22, 4, quiet_week, shared_hours=1)["label"], "quiet")
        self.assertEqual(R.classify_gps(26, 8, quiet_week, shared_hours=2)["label"], "new")

    def test_thin_baselines_learn_longer_and_lower_confidence(self):
        c = R.classify_gps
        three = [(15, 0)] * 3 + [(0, 0)] * 4
        t = c(20, 8, three, shared_hours=2, sampled=False)
        self.assertEqual(t["label"], "learning")                      # 3 days was enough before: no longer
        self.assertIn("Learning: 3 of 5 days", R.trend_text(t))
        five = [(15, 0)] * 5 + [(0, 0)] * 2
        t = c(20, 8, five, shared_hours=2, sampled=False)
        self.assertEqual(t["label"], "new")
        self.assertEqual(t["confidence"], "low")                       # thin, and only from viewing
        self.assertIn("5 earlier days with data (75 aircraft-hours)", R.trend_text(t))
        self.assertEqual(c(20, 8, five, shared_hours=2, sampled=True)["confidence"], "medium")
        self.assertEqual(c(20, 8, [(15, 0)] * 7, shared_hours=2, sampled=False)["confidence"], "medium")
        full = c(20, 8, [(15, 0)] * 7, shared_hours=2, sampled=True)
        self.assertEqual(full["confidence"], "high")
        self.assertTrue(any("GPS-sampler circle" in r for r in full["confidence_reasons"]))
        # clusters: severity 6 needs a high-confidence baseline, a low one gives 4
        low = c(40, 16, five, shared_hours=4, sampled=False)
        high = c(40, 16, [(15, 0)] * 7, shared_hours=4, sampled=True)
        self.assertEqual(R.gps_clusters({(100, 100): low}, 0.5)[0]["severity"], 4)
        self.assertEqual(R.gps_clusters({(100, 100): high}, 0.5)[0]["severity"], 6)
        title, sub = R.gps_cluster_words(R.gps_clusters({(100, 100): low}, 0.5)[0])
        self.assertIn("in the same hours on 5 earlier days with data (75 aircraft-hours)", sub)
        self.assertIn("confidence low", sub)
        self.assertNotIn("previous 7 days", sub)

    def test_clusters(self):
        new = R.classify_gps(60, 24, self.days(200, 0))
        chronic = R.classify_gps(60, 18, self.days(200, 60))
        trends = {(250, 400): new, (250, 401): new, (251, 401): new, (100, 100): new, (300, 300): chronic}
        cl = R.gps_clusters(trends, 0.5)
        self.assertEqual([c["n"] for c in cl], [3, 1])
        self.assertEqual(cl[0]["change"], "new")
        self.assertEqual(cl[0]["bbox"], [20.0, 35.0, 21.0, 36.0])
        title, sub = R.gps_cluster_words(cl[0])
        self.assertTrue(title.startswith("New GPS interference"))
        self.assertIn("3 cells", sub)


class GpsService(Base):
    def setUp(self):
        super().setUp()
        self.svc.clock = lambda: NOW

    def gps_rows(self):
        h_now = NOW // H
        rows = []
        for h in range(h_now - 8 * 24, h_now + 1):
            recent = h > h_now - 6
            # chronic cell near Kaliningrad: 30% every hour
            rows.append((h, 290, 400, 10, 3))
            # three adjacent cells off Lebanon: quiet all week, 40% in the last 6 hours
            for r, c in ((247, 429), (247, 430), (248, 430)):
                rows.append((h, r, c, 10, 4 if recent else 0))
        with self.store.tx() as c:
            c.executemany("INSERT INTO flights_gpsjam(hour, row, col, total, bad) VALUES(?,?,?,?,?)", rows)
        self.svc.gps._cache.clear()

    def test_only_new_cells_raise_air_activity_events_and_chronic_cells_are_tagged(self):
        self.gps_rows()
        self.act.detect(NOW, force_gps=True)
        self.assertEqual(self.act.gps_trend_of(290, 400)["label"], "chronic")
        self.assertEqual(self.act.gps_trend_of(247, 429)["label"], "new")
        self.assertEqual(len(self.act.gps_cluster_list), 1)
        self.assertEqual(self.act.gps_counts["chronic"], 1)
        air = [e for e in self.act.events(NOW - DAY) if e["category"] == "gps_interference"]
        self.assertEqual(len(air), 1)
        self.assertEqual(air[0]["kind"], "air_activity")
        self.assertEqual(air[0]["cells"], 3)
        self.assertEqual(air[0]["change"], "new")
        self.assertTrue(air[0]["title"].startswith("New GPS interference"))
        # the gps_jam state events stay (severity unchanged) and carry the trend
        jam = {e["id"]: e for e in self.svc.events(NOW - 6 * H) if e["kind"] == "gps_jam"}
        chronic = jam["gpsjam:55:20:0.5"]
        self.assertEqual(chronic["category"], "chronic_interference")
        self.assertEqual(chronic["trend"], "chronic")
        self.assertEqual(chronic["days_affected_7d"], 7)
        self.assertEqual(chronic["severity"], 6)
        new = jam["gpsjam:33.5:35:0.5"]
        self.assertEqual(new["category"], "new_interference")
        # four gps_jam cells, one air_activity event
        self.assertEqual(len(jam), 4)

    def test_episode_continues_and_card_trend(self):
        self.gps_rows()
        self.act.detect(NOW, force_gps=True)
        first = [e["id"] for e in self.act.events(NOW - DAY)]
        with self.store.tx() as c:                      # the area grows by one cell
            for h in range(NOW // H - 5, NOW // H + 1):
                c.execute("INSERT INTO flights_gpsjam(hour, row, col, total, bad) VALUES(?,?,?,?,?)", (h, 248, 431, 10, 4))
            for h in range(NOW // H - 8 * 24, NOW // H - 5):
                c.execute("INSERT INTO flights_gpsjam(hour, row, col, total, bad) VALUES(?,?,?,?,?)", (h, 248, 431, 10, 0))
        self.act.detect(NOW + 600, force_gps=True)
        ids = [e["id"] for e in self.act.events(NOW - DAY) if e["category"] == "gps_interference"]
        self.assertEqual(ids, first)
        ep = self.api.episode(ids[0])
        self.assertEqual(ep["data"]["n"], 4)
        self.assertEqual(len(ep["days"]), 7)
        self.assertTrue(ep["innocent"])
        # the GPS cell card gets a 7-day trend
        cell = self.get(f"/api/flights/gpsjam/cell?lat=55.2&lon=20.2&hours=24")
        self.assertEqual(cell["trend"]["label"], "chronic")
        self.assertEqual(len(cell["trend"]["days"]), 7)
        self.assertIn("Repeats daily", cell["trend"]["text"])

    def test_learning_cells_raise_nothing(self):
        h_now = NOW // H
        with self.store.tx() as c:
            for h in range(h_now - 30, h_now + 1):     # only a day and a bit of samples
                c.execute("INSERT INTO flights_gpsjam(hour, row, col, total, bad) VALUES(?,?,?,?,?)", (h, 247, 429, 10, 5))
        self.svc.gps._cache.clear()
        self.act.detect(NOW, force_gps=True)
        self.assertEqual(self.act.gps_trend_of(247, 429)["label"], "learning")
        self.assertEqual(self.act.gps_cluster_list, [])

    def insert(self, rows):
        with self.store.tx() as c:
            c.executemany("INSERT OR REPLACE INTO flights_gpsjam(hour, row, col, total, bad) VALUES(?,?,?,?,?)", rows)
        self.svc.gps._cache.clear()

    def at(self, t):
        self.act.clock = self.svc.clock = lambda t=t: t

    def test_a_daily_pattern_at_the_same_hours_never_raises_events(self):
        """Off Cyprus, 30% of aircraft affected from 16:00 to 20:00 UTC every day and none otherwise. At
        every hour of the day the cell is chronic (never new, worse or easing), so it raises no
        air_activity event and its gps_jam event says chronic_interference. A day at 90% is worse."""
        t0 = calendar.timegm((2026, 11, 23, 0, 0, 0))
        cell = (249, 426)

        def day(start, hours, bad=6):
            return [((start + hh * H) // H, *cell, 20, bad if 16 <= hh < 20 else 0) for hh in hours]

        self.insert([r for d in range(1, 10) for r in day(t0 - d * DAY, range(24))])
        labels = {}
        for hh in range(1, 25):                         # 01:05 to 00:05 the next day, one complete hour more each time
            self.insert(day(t0, [hh - 1]))
            now = t0 + hh * H + 300
            self.at(now)
            self.act.detect(now, force_gps=True)
            labels[hh] = self.act.gps_trend_of(*cell)["label"]
            self.assertEqual(self.act.gps_cluster_list, [], (hh, self.act.gps_trend_of(*cell)))
        self.assertEqual(set(labels.values()), {"chronic"}, labels)
        self.assertEqual([e for e in self.act.events(t0 - DAY) if e["category"] == "gps_interference"], [])
        jam = [e for e in self.svc.events(now - DAY) if e["kind"] == "gps_jam"]
        self.assertEqual([(e["trend"], e["category"], e["days_affected_7d"]) for e in jam],
                         [("chronic", "chronic_interference", 7)])
        card = self.act.cell_trend(*cell, 1, now)                         # 00:05: 18:00 to 00:00 holds two affected hours
        self.assertEqual(card["label"], "chronic")
        self.assertIn("affected in these hours on 7 of the last 7 days", card["text"])
        card = self.act.cell_trend(*cell, 1, t0 + 10 * H + 300)          # 10:05: clean at this time of day, not "easing"
        self.assertEqual(card["label"], "chronic")
        self.assertIn("at other hours than now", card["text"])
        # the next day the same hours reach 90%: clearly worse than any earlier day at this time
        self.insert(day(t0 + DAY, range(22), bad=18))
        now = t0 + DAY + 22 * H + 300
        self.at(now)
        self.act.detect(now, force_gps=True)
        self.assertEqual(self.act.gps_trend_of(*cell)["label"], "worse")
        ev = [e for e in self.act.events(t0) if e["category"] == "gps_interference"]
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["title"].startswith("Worse GPS interference: Cyprus, Lebanon and Israel"))
        self.assertIn("in the same hours on 7 earlier days with data", ev[0]["subtitle"])
        card = self.act.cell_trend(*cell, 1, now)
        self.assertIn("(16:00 to 22:00 UTC)", card["text"])

    def test_one_aircraft_circling_a_quiet_cell_is_not_new(self):
        """Finnmark: 3 normal aircraft an hour for a week, then one aircraft reporting NACp 6 for 4 of the
        last 6 hours, through the real collector. 18% of aircraft-hours are affected, but never 2 aircraft
        in the same hour: not new. Two such aircraft together in a neighbouring quiet cell are."""
        h_now = NOW // H
        lone, pair = (319, 417), (319, 420)
        self.insert([(h, *c, 3, 0) for h in range(h_now - 8 * 24, h_now - 6) for c in (lone, pair)])
        base = {"lat": 69.6, "alt": 32000, "gnd": False, "ptype": "adsb_icao", "version": 2, "nac_p": 10}
        gps = self.svc.gps
        for h in range(h_now - 6, h_now):
            t = h * H + 600
            obs = [dict(base, hex=f"4a{c[1]:03d}{i}", lon=lon, pos_t=t + i) for c, lon in ((lone, 28.6), (pair, 30.1))
                   for i in range(3)]
            if h >= h_now - 4:
                obs.append(dict(base, hex="47c001", lon=28.6, nac_p=6, pos_t=t + 5))
                obs += [dict(base, hex=f"47c1{i:02d}", lon=30.1, nac_p=6, pos_t=t + 6 + i) for i in range(2)]
            gps.observe(obs, now=t + 20)
            gps.flush(now=t + 30)
        row = self.store.one("SELECT SUM(total) AS t, SUM(bad) AS b FROM flights_gpsjam WHERE row = ? AND col = ? "
                             "AND hour >= ?", (*lone, h_now - 6))
        self.assertEqual((row["t"], row["b"]), (22, 4))                   # 18% affected, all from one aircraft
        self.act.detect(NOW, force_gps=True)
        t = self.act.gps_trend_of(*lone)
        self.assertNotIn(t["label"], ("new", "worse"))
        self.assertTrue(t["one_aircraft_at_a_time"])
        self.assertEqual(self.act.gps_trend_of(*pair)["label"], "new")
        ev = [e for e in self.act.events(NOW - DAY) if e["category"] == "gps_interference"]
        self.assertEqual([e["cells"] for e in ev], [1])
        self.assertIn("Finnmark", ev[0]["title"])
        self.assertAlmostEqual(ev[0]["lat"], 69.75)
        self.assertAlmostEqual(ev[0]["lon"], 30.25)

    def test_a_viewed_only_cell_needs_five_days_and_reports_its_thin_baseline(self):
        """English Channel (no sampler circle): viewed for about an hour at these hours on 3 earlier days,
        then 40% affected. That is learning, not a severity-6 event; with 5 such days it is new, at low
        confidence and severity 4, and the subtitle gives the real days and aircraft-hours."""
        h_now = NOW // H
        cell = (280, 360)

        def viewed(days):
            self.insert([(h_now - 6 - d * 24 + 2, *cell, 15, 0) for d in days]
                        + [(h, *cell, 10, 4) for h in (h_now - 2, h_now - 1)])

        viewed(range(1, 4))
        self.act.detect(NOW, force_gps=True)
        self.assertEqual(self.act.gps_trend_of(*cell)["label"], "learning")
        self.assertEqual(self.act.events(NOW - DAY), [])
        viewed(range(4, 6))
        self.act.detect(NOW + 60, force_gps=True)
        t = self.act.gps_trend_of(*cell)
        self.assertEqual((t["label"], t["confidence"], t["days_with_data"]), ("new", "low", 5))
        ev = [e for e in self.act.events(NOW - DAY) if e["category"] == "gps_interference"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["severity"], ev[0]["confidence"]), (4, "low"))
        self.assertIn("5 earlier days with data (75 aircraft-hours)", ev[0]["subtitle"])
        brief = self.get("/api/flights/activity")["gps"]["clusters"][0]
        self.assertEqual((brief["confidence"], brief["days_with_data"], brief["base_total"]), ("low", 5, 75))
        self.assertTrue(any("outside the GPS-sampler circles" in r for r in brief["confidence_reasons"]))

    def test_a_sampled_cell_with_a_full_week_is_high_confidence(self):
        self.svc.gps_sampler = True                      # the Lebanon cells lie inside the eastern Mediterranean circle
        self.gps_rows()
        self.act.detect(NOW, force_gps=True)
        cl = self.act.gps_cluster_list[0]
        self.assertEqual((cl["confidence"], cl["severity"], cl["days_with_data"]), ("high", 6, 7))
        ev = [e for e in self.act.events(NOW - DAY) if e["category"] == "gps_interference"]
        self.assertEqual(ev[0]["confidence"], "high")

    def test_window_queries_read_the_hour_range_not_the_whole_table(self):
        """With 14 days of buckets kept, the layer's window and the trend queries must read the primary
        key's hour range: SQLite otherwise scans the whole table through the (row, col) index."""
        h_now = NOW // H
        self.insert([(h, 250 + i // 10, 400 + i % 10, 10, i % 3) for h in range(h_now - 14 * 24, h_now + 1)
                     for i in range(30)])
        seen = []
        real = self.store.query

        def spy(sql, params=()):
            if "FROM flights_gpsjam" in sql:
                seen.append((sql, tuple(params)))
            return real(sql, params)

        self.store.query = spy
        try:
            self.svc.gps.cells(hours=24, now=NOW)
            self.svc.gps.cells(hours=48, now=NOW)
            self.act.refresh_gps(NOW)
        finally:
            del self.store.query
        self.assertGreaterEqual(len(seen), 4)
        for sql, params in seen:
            plan = " | ".join(str(r[3]) for r in self.store.conn().execute("EXPLAIN QUERY PLAN " + sql, params))
            self.assertNotIn("SCAN flights_gpsjam", plan, sql)
            self.assertIn("hour>", plan, sql)


# =================================================================================================
# airport movement drops
# =================================================================================================
class AirportDrops(Base):
    def test_learning(self):
        self.airport_days("EFHK", 3, self.busy, last={END - H: (3, 10, 0b111111), END - 2 * H: (3, 10, 0b111111)})
        st = self.act.refresh_airports(NOW)["EFHK"]
        self.assertEqual(st["state"], "learning")
        self.assertEqual(st["status"], "Learning baseline: 3 of 7 days")

    def test_drop_for_two_hours_is_an_event(self):
        self.airport_days("EFHK", 10, self.busy, last={END - H: (3, 10, 0b111111), END - 2 * H: (2, 9, 0b111111)})
        st = self.act.refresh_airports(NOW)["EFHK"]
        self.assertEqual(st["state"], "drop", st["status"])
        self.assertEqual(st["severity"], 5)
        self.assertEqual(st["receivers"], "ok")
        self.assertIn("Overflights look normal", st["evidence"])
        self.assertTrue(any("holiday" in x for x in st["innocent"]))
        self.act.update_episodes(NOW)
        ev = [e for e in self.act.events(NOW - DAY) if e["category"] == "airport_drop"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["kind"], "air_activity")
        self.assertTrue(ev[0]["id"].startswith("flights:airport:EFHK:"))
        self.assertEqual(ev[0]["title"], "Far fewer movements than usual at Helsinki (EFHK)")
        self.assertAlmostEqual(ev[0]["lat"], EFHK[0], places=3)

    def test_one_low_hour_is_only_notable(self):
        self.airport_days("EFHK", 10, self.busy, last={END - H: (3, 10, 0b111111), END - 2 * H: (31, 10, 0b111111)})
        st = self.act.refresh_airports(NOW)["EFHK"]
        self.assertEqual(st["state"], "notable")
        self.act.update_episodes(NOW)
        self.assertEqual(self.act.events(NOW - DAY), [])

    def test_quiet_scheduled_hours_are_never_judged(self):
        night = NOW - 13 * H            # 02:05 UTC: the last complete hours are 00:00 to 02:00, usually 2 movements
        end = night - night % H
        self.airport_days("EFHK", 10, self.busy, last={end - H: (0, 10, 0b111111), end - 2 * H: (0, 10, 0b111111)})
        st = self.act.refresh_airports(night)["EFHK"]
        self.assertEqual(st["state"], "normal")
        self.assertIn("quiet hour", st["status"])

    def test_no_snapshots_is_not_a_drop(self):
        self.airport_days("EFHK", 10, self.busy, last={END - H: (0, 0, 0b000001), END - 2 * H: (0, 0, 0)})
        st = self.act.refresh_airports(NOW)["EFHK"]
        self.assertEqual(st["state"], "no_coverage")
        self.act.update_episodes(NOW)
        self.assertEqual(self.act.events(NOW - DAY), [])

    def test_overflights_down_too_is_flagged_as_unverified(self):
        self.airport_days("OLBA", 10, self.busy, last={END - H: (2, 1, 0b111111), END - 2 * H: (1, 0, 0b111111)})
        st = self.act.refresh_airports(NOW)["OLBA"]
        self.assertEqual(st["state"], "drop_unverified")
        self.assertEqual(st["severity"], 3)
        self.assertEqual(st["confidence"], "low")
        self.assertIn("airspace may be closed", st["status"])
        self.act.update_episodes(NOW)
        ev = self.act.events(NOW - DAY)
        self.assertEqual(ev[0]["title"], "Far fewer aircraft than usual around Beirut (OLBA)")

    def test_the_same_weekday_is_used_on_its_own(self):
        """A weekend-busy airport (Saturdays 40 movements an hour, other days 15): this Saturday at 15 for
        two hours is a drop against Saturdays. The lower of the two baselines (15, every day) would hide
        it, and the evidence names the baseline it used."""
        sat = NOW - 2 * DAY
        end = sat - sat % H
        rows = []
        for d in range(1, 29):
            for k in range(24):
                hs = end - d * DAY + (k - 12) * H
                n = 40 if time.gmtime(hs).tm_wday == 5 else 15
                rows.append(("EFHK", hs // H, 0b111111, n // 2 + n % 2, n // 2, 5, 10, ""))
        rows += [("EFHK", hs // H, 0b111111, 8, 7, 1, 10, "") for hs in (end - H, end - 2 * H)]
        with self.store.tx() as c:
            c.executemany("INSERT OR REPLACE INTO flights_apt_hour(ident, hour, mask, arr, dep, gnd, high, sets) "
                          "VALUES(?,?,?,?,?,?,?,?)", rows)
        st = self.act.refresh_airports(sat)["EFHK"]
        self.assertEqual(st["profile"], "week")
        self.assertEqual(st["usual"], 40)
        self.assertEqual(st["state"], "drop", st["status"])
        self.assertIn("usual for this hour on Saturdays (median of 4 weeks) is 40", st["evidence"])

    def test_airport_chart_usual_is_the_detectors_own(self):
        self.airport_days("EFHK", 3, self.busy, last={END - H: (3, 10, 0b111111), END - 2 * H: (3, 10, 0b111111)})
        self.act.detect(NOW, force_gps=True)
        mv = self.get("/api/flights/airport/EFHK/movements")
        self.assertEqual(mv["state"], "learning")
        self.assertTrue(all(r[6] is None for r in mv["series"]))        # no usual line while learning
        self.airport_days("EFHK", 10, self.busy, last={END - H: (3, 10, 0b111111), END - 2 * H: (2, 9, 0b111111)})
        self.act.detect(NOW + 60, force_gps=True)
        st = self.act.airport_states["EFHK"]
        mv = self.get("/api/flights/airport/EFHK/movements")
        last = {r[0]: r for r in mv["series"]}[END - H]
        self.assertEqual(last[6], st["usual"])
        self.assertEqual(self.app.call_ai_tool("airport_movements", {"ident": "EFHK"})["last_24h"][-1]["usual"], st["usual"])

    def test_faa_link_only_for_us_airports(self):
        self.act.refresh_airports(NOW)
        self.assertEqual(self.act.airport_states["KJFK"]["faa"]["link"], "https://nasstatus.faa.gov/")
        self.assertIsNone(self.act.airport_states["EFHK"]["faa"])

    def test_snapshot_classifies_movements(self):
        real = time.time()
        t = real - real % 600 - 500          # 100 s into the previous slot: always in the past, never at an edge
        lat, lon = EFHK
        base = {"pos_t": t - 1, "gnd": False, "ptype": "adsb_icao", "cat": "A3", "gs": 200}
        obs = [dict(base, hex="461e01", lat=lat + 0.15, lon=lon, alt=6000, vr=-900, trk=180, cs="FIN1"),   # arriving
               dict(base, hex="461e02", lat=lat - 0.1, lon=lon, alt=4000, vr=1800, trk=180, cs="FIN2"),    # departing
               dict(base, hex="461e03", lat=lat + 0.01, lon=lon, alt=0, gnd=True, gs=10, cs="FIN3"),       # on the ground
               dict(base, hex="461e04", lat=lat, lon=lon + 0.2, alt=37000, vr=0, trk=90, cs="SAS4"),       # overflight
               dict(base, hex="461e05", lat=lat + 0.05, lon=lon, alt=1500, vr=-500, cat="A7"),             # helicopter
               dict(base, hex="a0f00d", lat=lat + 0.12, lon=lon, alt=5000, vr=-800, db_flags=8),           # privacy
               dict(base, hex="461e06", lat=lat + 2.0, lon=lon, alt=5000, vr=-800)]                         # elsewhere
        self.svc._absorb(obs, "adsbfi", t, circle=(lat, lon, 45.0))
        row = self.store.one("SELECT * FROM flights_apt_hour WHERE ident = 'EFHK'")
        self.assertEqual((row["arr"], row["dep"], row["gnd"], row["high"]), (1, 1, 1, 1))
        self.assertEqual(R.popcount(row["mask"]), 1)
        self.assertNotIn("a0f00d", row["sets"])
        self.svc._absorb(obs, "adsbfi", t + 5, circle=(lat, lon, 45.0))        # same slot: no second snapshot
        self.assertEqual(R.popcount(self.store.one("SELECT mask FROM flights_apt_hour WHERE ident='EFHK'")["mask"]), 1)
        # Tallinn's counting circle is not wholly inside that circle: no snapshot for it
        self.assertIsNone(self.store.one("SELECT * FROM flights_apt_hour WHERE ident = 'EETN'"))
        # a circle off to one side of Helsinki gives none either
        self.svc._absorb(obs, "adsbfi", t + 700, circle=(lat + 0.6, lon, 60.0))
        self.assertEqual(R.popcount(self.store.one("SELECT mask FROM flights_apt_hour WHERE ident='EFHK'")["mask"]), 1)

    def test_only_adsbfi_answers_become_snapshots(self):
        """Fallback networks have other feeders: their answers never enter a baseline."""
        real = time.time()
        t = real - real % 600 - 500
        lat, lon = EFHK
        obs = [{"hex": "461e01", "lat": lat + 0.15, "lon": lon, "pos_t": t - 1, "gnd": False, "alt": 6000, "vr": -900,
                "trk": 180, "cat": "A3", "gs": 200, "ptype": "adsb_icao"}]
        self.svc._absorb(obs, "adsblol", t, circle=(lat, lon, 45.0))
        self.assertIsNone(self.store.one("SELECT * FROM flights_apt_hour WHERE ident = 'EFHK'"))
        mil = [{"hex": "ae4444", "lat": 57.0, "lon": 19.5, "pos_t": real - 2, "alt": 30000, "gnd": False, "typ": "K35R",
                "db_flags": 1}]

        class Lol:
            id = "adsblol"

            def mil(self):
                return real, mil

        self.svc._run_mil(Lol())
        self.assertIn("ae4444", self.svc.mil_hexes)              # the map still gets the aircraft
        self.assertIsNone(self.store.one("SELECT * FROM flights_mil_cov"))


# =================================================================================================
# routes, AI tools, layer and contract details
# =================================================================================================
class RoutesAndTools(Base):
    def seed_some(self):
        self.history_days(14, lambda hs, d: 4)
        self.mil_hours("baltic", {END - k * H: 16 for k in (1, 2, 3)})
        self.window_hexes("baltic", 20)
        self.airport_days("EFHK", 10, self.busy, last={END - H: (3, 10, 0b111111), END - 2 * H: (2, 9, 0b111111)})
        self.act.detect(NOW, force_gps=True)

    def test_region_card_note_says_when_no_aircraft_are_listed(self):
        """The card shows aircraft_note only for an empty list, so the note must say the list is empty."""
        self.seed_some()
        d = self.get("/api/flights/activity/region/baltic")
        self.assertEqual(d["aircraft"], [])
        self.assertTrue(d["aircraft_note"].startswith("None in the latest worldwide list"), d["aircraft_note"])
        obs = [{"hex": "ae1234", "lat": BALTIC[0], "lon": BALTIC[1], "pos_t": NOW - 5, "alt": 30000, "gnd": False,
                "typ": "K35R", "db_flags": 1}]
        self.svc.tracker.ingest(obs, "adsbfi")
        d = self.get("/api/flights/activity/region/baltic")
        self.assertEqual([a["id"] for a in d["aircraft"]], ["ae1234"])
        self.assertTrue(d["aircraft_note"].startswith("Military aircraft in the region now"), d["aircraft_note"])

    def test_routes(self):
        self.seed_some()
        s = self.get("/api/flights/activity")
        self.assertEqual(s["regions"][0]["id"], "baltic")             # flagged first
        self.assertEqual(s["regions"][0]["state"], "unusual")
        self.assertEqual(s["airports"][0]["ident"], "EFHK")
        self.assertEqual(s["flags"], 2)
        self.assertIn("military", s["sampler"])
        self.assertEqual(set(s["categories"]), {"military_surge", "gps_interference", "airport_drop"})
        self.assertEqual(len(s["episodes"]), 2)
        fc = self.get("/api/flights/activity/map")
        kinds = {f["properties"]["kind"] for f in fc["features"]}
        self.assertTrue({"region", "region_label", "airport"} <= kinds)
        d = self.get("/api/flights/activity/region/baltic")
        self.assertEqual(len(d["series"]), 49)
        self.assertEqual(d["geometry"]["type"], "Polygon")
        self.assertEqual(len(d["episodes"]), 1)
        with self.assertRaises(HTTPError) as e:
            self.get("/api/flights/activity/region/atlantis")
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(HTTPError):
            self.get("/api/flights/activity/region/Bad-Id")
        ep_id = s["episodes"][0]["id"]
        ep = self.get("/api/flights/activity/episode?id=" + urllib.parse.quote(ep_id))
        self.assertEqual(ep["id"], ep_id)
        self.assertTrue(ep["method"])
        with self.assertRaises(HTTPError) as e:
            self.get("/api/flights/activity/episode?id=nonsense")
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(HTTPError) as e:
            self.get("/api/flights/activity/episode?id=flights:surge:baltic:1")
        self.assertEqual(e.exception.status, 404)
        mv = self.get("/api/flights/airport/EFHK/movements")
        self.assertTrue(mv["monitored"])
        self.assertEqual(mv["state"], "drop")
        self.assertEqual(len(mv["series"]), 49)
        self.assertIn("coverage_note", mv)
        other = self.get("/api/flights/airport/ZZZZ/movements")
        self.assertFalse(other["monitored"])
        with self.assertRaises(HTTPError):
            self.get("/api/flights/airport/E<FHK/movements")

    def test_ai_tools(self):
        self.seed_some()
        out = self.app.call_ai_tool("air_activity", {"region": "baltic"})
        self.assertEqual(out["military"]["regions"][0]["state"], "unusual")
        self.assertIn("evidence", out["military"]["regions"][0])
        self.assertTrue(out["caveats"])
        by_name = self.app.call_ai_tool("air_activity", {"region": "Korean"})
        self.assertEqual([r["id"] for r in by_name["military"]["regions"]], ["korea"])
        area = self.app.call_ai_tool("air_activity", {"bbox": [20, 58, 30, 62], "categories": ["airport_drop"]})
        self.assertEqual(area["airports"]["items"][0]["ident"], "EFHK")
        self.assertNotIn("military", area)
        none = self.app.call_ai_tool("air_activity", {"region": "atlantis"})
        self.assertFalse(none["found"])
        with self.assertRaises(ValueError):
            self.app.call_ai_tool("air_activity", {"categories": ["nonsense"]})
        apt = self.app.call_ai_tool("airport_movements", {"ident": "efhk"})
        self.assertEqual(apt["ident"], "EFHK")
        self.assertEqual(len(apt["last_24h"]), 24)
        self.assertEqual(apt["state"], "drop")
        with self.assertRaises(ValueError):
            self.app.call_ai_tool("airport_movements", {"ident": "<x>"})

    def test_ai_region_names_matching_several_regions_scope_every_section(self):
        """'gulf' matches the Persian Gulf and the Red Sea and Gulf of Aden: the GPS, airport and episode
        sections cover those two regions (never Helsinki or the Baltic), and 'scope' says so."""
        self.airport_days("OTHH", 10, self.busy, last={END - H: (31, 10, 0b111111), END - 2 * H: (31, 10, 0b111111)})
        self.seed_some()
        out = self.app.call_ai_tool("air_activity", {"region": "gulf"})
        self.assertEqual({r["id"] for r in out["military"]["regions"]}, {"persian_gulf", "red_sea"})
        self.assertEqual(out["scope"]["by"], "regions")
        self.assertEqual(set(out["scope"]["regions"]), {"persian_gulf", "red_sea"})
        self.assertIn("2 regions matching 'gulf'", out["scope"]["note"])
        idents = [a["ident"] for a in out["airports"]["items"]]
        self.assertEqual(idents, ["OTHH"])                         # Doha is inside the Persian Gulf box; Helsinki is not
        self.assertEqual(out["recent_episodes"], [])
        self.assertEqual(out["gps"]["new_or_worse"], [])
        one = self.app.call_ai_tool("air_activity", {"region": "baltic"})
        self.assertEqual(one["scope"]["regions"], ["baltic"])
        self.assertIn("EFHK", [a["ident"] for a in one["airports"]["items"]])
        self.assertEqual({e["ref"] for e in one["recent_episodes"]}, {"baltic", "EFHK"})
        self.assertEqual(self.app.call_ai_tool("air_activity", {})["scope"]["by"], "worldwide")

    def test_read_routes_never_mark_the_module_active(self):
        for path in ("/api/flights/activity", "/api/flights/activity/map", "/api/flights/activity/region/baltic",
                     "/api/flights/activity/episode", "/api/flights/airport/EFHK/movements",
                     "/api/flights/history", "/api/flights/history/coverage", "/api/flights/history/abcdef"):
            route, _, _ = self.app.match("GET", path)
            self.assertIsNotNone(route, path)
            self.assertFalse(route.touch, path)
        live, _, _ = self.app.match("GET", "/api/flights/live")
        self.assertTrue(live.touch)

    def test_layer_source_settings_and_status(self):
        ids = [s["id"] for s in self.app.layer_specs]
        self.assertIn("flights.activity", ids)
        self.assertIn("flights.activity", self.app.sources)
        keys = {k["name"]: k for k in self.app.config.key_status()}
        self.assertFalse(keys["FLIGHTS_MIL_SAMPLER"]["secret"])
        self.assertFalse(keys["FLIGHTS_AIRPORT_WATCH"]["secret"])
        self.seed_some()
        self.assertEqual(self.app.sources["flights.activity"]["count"], 2)
        info = self.act.sampler_info(NOW)
        self.assertFalse(info["military"]["on"])
        self.assertEqual(info["military"]["requests_per_day"], 0)

    def test_episodes_are_pruned_after_30_days(self):
        self.seed_some()
        self.act.prune(NOW + 31 * DAY)
        self.assertEqual(self.act.events(0), [])


class Samplers(Base):
    overrides = {"FLIGHTS_GPSJAM_SAMPLER": "0", "FLIGHTS_MIL_SAMPLER": "1", "FLIGHTS_AIRPORT_WATCH": "1"}

    def test_military_sampler_uses_adsbfi_only_once_per_slot(self):
        calls = []
        self.svc.poll_military = lambda background=False: calls.append(background) or object()
        self.act.clock = lambda: END + 30
        self.assertIsNotNone(self.act.sample_mil())
        self.assertEqual(calls, [True])
        self.assertIsNone(self.act.sample_mil())               # a job went out less than a minute ago
        self.act.add_mil_snapshot(END + 40, [])
        self.act.mil_job_t = 0
        self.assertIsNone(self.act.sample_mil())               # the slot has its snapshot
        self.act.clock = lambda: END + 630
        self.assertIsNotNone(self.act.sample_mil())
        self.assertEqual(calls, [True, True])

    def test_background_military_poll_never_spends_adsblol(self):
        self.svc.adsbfi.health.fail("down", retry_after=600)
        self.assertIsNone(self.svc.poll_military(background=True))
        self.assertIsNotNone(self.svc.poll_military())          # the map may still fall back to adsb.lol

    def test_favourited_airports_are_watched(self):
        self.store.execute("INSERT INTO favourites(kind, ref, title, subtitle, lat, lon, layer, payload, created_at) "
                           "VALUES('airport', 'LEBL', 'Barcelona', '', 41.2971, 2.0785, 'infra.airports', '{}', ?)",
                           (time.time(),))
        submitted = []

        class Q:
            def size(self):
                return 0

            def submit(self, key, fn, priority=5, ttl=None):
                submitted.append(key)
                return None

        self.svc.adsbfi.queue = Q()
        self.act.clock = lambda: END + 30
        self.act.sample_airports()
        self.assertEqual(self.act.watched, ["LEBL"])
        self.assertIn("LEBL", self.act.airports)
        self.assertEqual(submitted, ["apt:LEBL"])


class SlowCadenceSetting(Base):
    """FLIGHTS_MIL_SAMPLE_S used to slow the sampler to every 2nd or 3rd slot, so no hour reached 5 of 6
    snapshots and every region stayed 'learning' for ever. The cadence is now fixed at the slot."""
    overrides = {"FLIGHTS_GPSJAM_SAMPLER": "0", "FLIGHTS_MIL_SAMPLER": "1", "FLIGHTS_AIRPORT_WATCH": "0",
                 "FLIGHTS_MIL_SAMPLE_S": "1800"}

    def test_a_slower_cadence_setting_cannot_starve_the_baseline(self):
        self.assertEqual(self.act.mil_every, 600)
        info = self.act.sampler_info(NOW)["military"]
        self.assertEqual((info["every_s"], info["expected_24h"], info["requests_per_day"]), (600, 144, 144))
        clock = {"t": 0.0}
        self.act.clock = lambda: clock["t"]
        self.svc.poll_military = lambda background=False: self.act.add_mil_snapshot(
            clock["t"], [(f"ae{i:04x}", *BALTIC, "tanker") for i in range(4)]) or object()
        # the sampler, run every 10 minutes through the 3-hour window on each of the last 8 days and today
        for d in range(8, -1, -1):
            for s in range(18):
                clock["t"] = END - d * DAY - 3 * H + s * SLOT + 30
                self.act.mil_job_t = 0
                self.act.sample_mil()
        st = self.act.refresh_regions(NOW)["baltic"]
        self.assertEqual(st["coverage"], 1.0)
        self.assertGreaterEqual(st["baseline_days"], 7)
        self.assertNotEqual(st["state"], "learning")
        self.assertEqual(st["state"], "normal")
