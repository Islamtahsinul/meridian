"""IMF PortWatch chokepoint transits (meridian/modules/ships/portwatch.py): polite scheduling (one small,
filtered request per run), parsing, summaries, Meridian's own daily strait tallies and the cross-check that
says plainly when either source has coverage gaps (the Strait of Hormuz case). No network: fixtures are
trimmed from real PortWatch answers of 23 November 2026."""

import copy
import json
import logging
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from meridian.net import NetError
from meridian.modules.ships import portwatch as pw
from meridian.store import Store

FIX = Path(__file__).parent / "fixtures" / "ships"
NOW = pw.epoch_of("2026-11-23") + 12 * 3600            # PortWatch's latest day is then 15 November
LOG = logging.getLogger("test.ships.portwatch")


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeNet:
    """Answers the two PortWatch services from the fixtures (other chokepoints get a steady 40 a day)."""

    def __init__(self):
        self.calls = []
        self.fail = None

    def set_host_interval(self, host, seconds):
        self.interval = (host, seconds)

    def get_json(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {}), kw))
        if self.fail:
            raise self.fail
        if url == pw.POINTS_URL:
            return fixture("portwatch_chokepoints.json")
        if "outStatistics" in (params or {}):
            return fixture("portwatch_stats.json")
        where = params["where"]
        pid = where.split("'")[1]
        start = where.split("DATE '")[1][:10]
        if pid == "chokepoint6":
            data = fixture("portwatch_daily_hormuz.json")
            day = start
            while day < "2026-11-01":                        # older days: a quiet strait, 4 a day
                data["features"].append({"attributes": {"date": day, "portid": pid, "n_total": 4, "n_tanker": 2}})
                day = pw.day_of(pw.epoch_of(day) + 86400)
            return data
        feats = []
        day = start
        while day <= "2026-11-15":
            feats.append({"attributes": {"date": day, "portid": pid, "n_total": 40, "n_tanker": 15, "n_container": 10,
                                         "n_dry_bulk": 9, "n_general_cargo": 4, "n_roro": 2, "n_cargo": 25,
                                         "capacity": 1500000}})
            day = pw.day_of(pw.epoch_of(day) + 86400)
        return {"features": feats}


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Store(Path(self.tmp) / "t.db")
        self.net = FakeNet()
        self.clock = Clock()
        self.statuses = []
        self.pw = pw.PortWatch(self.db, self.net, now=self.clock, log=LOG, status=lambda **kw: self.statuses.append(kw))
        self.pw.create_tables()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fill(self):
        steps = []
        for _ in range(10):
            steps.append(self.pw.step())
            self.clock.t += 180
        return steps


class Parsing(unittest.TestCase):
    def test_points(self):
        pts = pw.parse_points(fixture("portwatch_chokepoints.json"))
        self.assertEqual([p["portid"] for p in pts], ["chokepoint1", "chokepoint6", "chokepoint12", "chokepoint28"])
        hormuz = pts[1]
        self.assertEqual(hormuz["name"], "Strait of Hormuz")
        self.assertAlmostEqual(hormuz["lat"], 26.297, places=3)
        self.assertEqual(pw.parse_points({"error": {"code": 400}}), [])
        self.assertEqual(pw.parse_points("nonsense"), [])

    def test_daily_and_stats(self):
        rows = pw.parse_daily(fixture("portwatch_daily_hormuz.json"))
        self.assertEqual(len(rows), 15)                      # the row with a bad date is skipped
        self.assertEqual(rows[-1], {"portid": "chokepoint6", "day": "2026-11-15", "n_total": 2, "n_tanker": 0,
                                    "n_container": 1, "n_dry_bulk": 0, "n_general_cargo": 1, "n_roro": 0,
                                    "n_cargo": 2, "capacity": 4297})
        epoch = pw.parse_daily({"features": [{"attributes": {"date": 1794700800000, "portid": "chokepoint2",
                                                             "n_total": 31}}]})
        self.assertEqual(epoch[0]["day"], "2026-11-15")      # older layers send epoch milliseconds
        bad = pw.parse_daily({"features": [{"attributes": {"date": "2026-11-15", "portid": "x'; DROP", "n_total": 1}},
                                           {"attributes": {"date": "2026-11-15", "portid": "chokepoint2", "n_total": -4}}]})
        self.assertEqual(bad, [])
        # a day that matches the pattern but is not on the calendar is skipped too
        odd = pw.parse_daily({"features": [{"attributes": {"date": d, "portid": "chokepoint2", "n_total": 3}}
                                           for d in ("2026-11-31", "2026-02-29", "2026-13-01", "2028-02-29")]})
        self.assertEqual([r["day"] for r in odd], ["2028-02-29"])
        stats = pw.parse_stats(fixture("portwatch_stats.json"))
        self.assertAlmostEqual(stats["chokepoint6"]["avg_1y"], 22.337, places=2)
        self.assertEqual(stats["chokepoint6"]["days_1y"], 365)


class Fetching(Base):
    def test_one_small_filtered_request_per_step(self):
        steps = self.fill()
        self.assertEqual(steps[:6], ["points", "stats", "daily", "daily", "daily", "daily"])
        self.assertEqual(steps[6:], ["idle"] * 4)
        self.assertEqual(len(self.net.calls), 6)
        for url, params, kw in self.net.calls:
            self.assertTrue(url.startswith("https://services9.arcgis.com/"))
            self.assertEqual(params["f"], "json")
            self.assertLessEqual(kw["timeout"], 60)
        daily = [c for c in self.net.calls if c[0] == pw.DAILY_URL and "outStatistics" not in c[1]]
        self.assertEqual(len(daily), 4)
        first = daily[0][1]
        self.assertEqual(first["where"], "portid = 'chokepoint6' AND date >= DATE '2026-07-26'")    # Hormuz, 120 days
        order = [c[1]["where"].split("'")[1] for c in daily]
        self.assertEqual(order, ["chokepoint6", "chokepoint1", "chokepoint12", "chokepoint28"])
        self.assertEqual(first["outFields"], pw.DAILY_FIELDS)
        self.assertEqual(first["returnGeometry"], "false")
        stats = next(c for c in self.net.calls if "outStatistics" in c[1])[1]
        self.assertIn("groupByFieldsForStatistics", stats)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM ships_pw_daily WHERE portid = 'chokepoint6'"), 113)    # 26 Jul to 15 Nov
        self.assertTrue(self.statuses[-1]["ok"])
        self.assertIn("4 of 4 chokepoints have daily counts, up to 15 Nov", self.statuses[-1]["note"])
        # A week later: the averages and each chokepoint again, from 14 days before its latest day.
        self.clock.t += 7 * 86400
        self.net.calls.clear()
        self.assertEqual(self.pw.step(), "stats")
        self.assertEqual(self.pw.step(), "daily")
        self.assertEqual(self.net.calls[-1][1]["where"], "portid = 'chokepoint6' AND date >= DATE '2026-11-01'")
        # The list itself only once a month.
        self.clock.t += 30 * 86400
        self.assertEqual(self.pw.step(), "points")

    def test_failures_back_off(self):
        self.net.fail = NetError("services9.arcgis.com: timed out")
        self.assertEqual(self.pw.step(), "error")
        self.assertFalse(self.statuses[-1]["ok"])
        self.assertIn("IMF PortWatch did not answer", self.statuses[-1]["error"])
        self.assertIn("paused after the failure; next try in about 15 min", self.statuses[-1]["note"])
        self.net.fail = None
        self.clock.t += 300
        self.assertEqual(self.pw.step(), "backoff")
        self.assertEqual(len(self.net.calls), 1)
        self.clock.t += 900
        self.assertEqual(self.pw.step(), "points")
        self.assertEqual(self.pw.meta()["fail_n"], 0)

    def test_arcgis_errors_inside_a_200(self):
        orig = self.net.get_json
        self.net.get_json = lambda url, **kw: {"error": {"code": 400, "message": "Invalid query"}}
        self.assertEqual(self.pw.step(), "error")
        self.assertIn("ArcGIS error 400", self.pw.meta()["last_error"])
        self.net.get_json = orig

    def test_a_service_that_asks_for_a_token_is_reported_not_signed_into(self):
        # What the daily service answered from 23 Nov 2026 19:20 UTC (it also left the public directory).
        self.net.get_json = lambda url, **kw: {"error": {"code": 499, "message": "Token Required",
                                                         "messageCode": "GWM_0003", "details": ["Token Required"]}}
        self.assertEqual(self.pw.step(), "error")
        self.assertEqual(self.pw.meta()["last_error"], pw.UNAVAILABLE)
        self.assertIn("does not sign in", self.statuses[-1]["error"])
        self.assertEqual(self.statuses[-1]["error"], pw.UNAVAILABLE)          # not wrapped in "did not answer (...)"
        chk = pw.cross_check(None, {"id": "hormuz", "coverage": False}, {}, pw.UNAVAILABLE)
        self.assertTrue(chk["lines"][0].startswith("IMF PortWatch has no daily counts here: PortWatch's daily "
                                                   "chokepoint service is not public right now"))
        self.assertTrue(chk["pw_gap"] and chk["meridian_gap"])


    def test_a_failed_daily_fetch_replaces_the_being_fetched_note(self):
        # The list answers, then the daily service asks for a token: the note must not keep saying "being fetched".
        orig = self.net.get_json
        self.assertEqual(self.pw.step(), "points")
        self.assertIn("being fetched", self.statuses[-1]["note"])
        self.clock.t += 180
        self.assertEqual(self.pw.step(), "stats")
        self.net.get_json = lambda url, **kw: (orig(url, **kw) if "chokepoints_database" in url else
                                               {"error": {"code": 499, "message": "Token Required"}})
        self.clock.t += 180
        self.assertEqual(self.pw.step(), "error")
        last = self.statuses[-1]
        self.assertFalse(last["ok"])
        self.assertEqual(last["error"], pw.UNAVAILABLE)
        self.assertNotIn("being fetched", last["note"])
        self.assertNotIn("every few minutes", last["note"])
        self.assertIn("4 chokepoints listed; no daily counts yet.", last["note"])
        self.assertIn("next try in about 15 min", last["note"])
        # Each further failure doubles the wait, and the note says so, up to 6 hours.
        waits = []
        for _ in range(6):
            self.clock.t += pw.backoff_s(self.pw.meta()["fail_n"])
            self.assertEqual(self.pw.step(), "error")
            waits.append(self.statuses[-1]["note"].rsplit("about ", 1)[1])
        self.assertEqual(waits, ["30 min.", "1 h.", "2 h.", "4 h.", "6 h.", "6 h."])
        self.net.get_json = orig

    def test_backoff_and_wait_text(self):
        self.assertEqual([pw.backoff_s(n) for n in (1, 2, 3, 6, 7, 50)], [900, 1800, 3600, 6 * 3600, 6 * 3600, 6 * 3600])
        self.assertEqual([pw.wait_text(s) for s in (900, 1800, 3600, 7200, 21600)], ["15 min", "30 min", "1 h", "2 h", "6 h"])


class Summaries(Base):
    def setUp(self):
        super().setUp()
        self.fill()

    def test_hormuz_far_below_its_own_average(self):
        s = self.pw.summaries()["chokepoint6"]
        self.assertEqual(s["strait"], "hormuz")
        self.assertEqual(s["latest"]["day"], "2026-11-15")
        self.assertEqual(s["latest"]["total"], 2)
        self.assertEqual(s["lag_days"], 8)
        self.assertEqual(s["mean_7d"], 2.3)                  # 6, 1, 4, 1, 1, 1, 2
        self.assertEqual(s["mean_1y"], 22.3)
        self.assertEqual(s["change_1y_pct"], -90)
        self.assertEqual(s["missing_28d"], 0)
        self.assertIn("far_below", s["flags"])
        detail = self.pw.summary("chokepoint6", 30)
        self.assertEqual(len(detail["series"]), 30)
        self.assertEqual(detail["series"][-1], ["2026-11-15", 2, 0])
        self.assertIsNone(self.pw.summary("chokepoint6' OR 1=1"))

    def test_cross_check_says_plainly_when_both_have_gaps(self):
        s = self.pw.summary("chokepoint6", 30)
        strait = {"id": "hormuz", "coverage": False, "transits_24h": 0}
        chk = pw.cross_check(s, strait, {})
        text = " ".join(chk["lines"])
        self.assertTrue(chk["pw_gap"] and chk["meridian_gap"])
        self.assertIn("PortWatch counted 2 transits on 15 Nov", text)
        self.assertIn("90% below PortWatch's own 1-year average", text)
        self.assertIn("its satellite AIS may be missing ships", text)
        self.assertIn("Meridian hears no terrestrial AIS here", text)
        self.assertIn("neither source can be confirmed against the other", text)

    def test_cross_check_on_a_shared_day(self):
        s = self.pw.summary("chokepoint1", 30)
        strait = {"id": "suez", "coverage": True, "transits_24h": 44}
        full = {"2026-11-15": {"transits": 50, "transits_ct": 38, "covered_hours": 24}}
        chk = pw.cross_check(s, strait, full)
        text = " ".join(chk["lines"])
        self.assertFalse(chk["pw_gap"] or chk["meridian_gap"])
        self.assertIn("On 15 Nov Meridian counted 38 cargo and tanker transits (50 of all types)", text)
        self.assertIn("The two counts are close (38 against 40)", text)
        partial = pw.cross_check(s, strait, {"2026-11-15": {"transits": 9, "transits_ct": 6, "covered_hours": 11}})
        self.assertTrue(partial["meridian_gap"])
        self.assertIn("partial, so its count is a minimum", " ".join(partial["lines"]))
        none_yet = pw.cross_check(s, strait, {})
        self.assertIn("do not overlap PortWatch's days yet", " ".join(none_yet["lines"]))
        self.assertIn("does not monitor this chokepoint", " ".join(pw.cross_check(s, None, None)["lines"]))
        self.assertTrue(pw.cross_check(None, strait, {})["pw_gap"])

    def test_a_stored_impossible_day_does_not_break_summaries(self):
        # A row written before days were checked (or by hand) must be skipped, not raise on every call.
        self.db.execute("INSERT OR REPLACE INTO ships_pw_daily(portid, day, n_total) VALUES(?, ?, ?)",
                        ("chokepoint6", "2026-11-31", 5))
        self.pw._cache = None
        s = self.pw.summaries()["chokepoint6"]
        self.assertEqual(s["latest"]["day"], "2026-11-15")
        self.assertEqual(self.pw.summary("chokepoint6", 30)["latest"]["day"], "2026-11-15")
        self.clock.t += 8 * 86400                              # a daily fetch is due, then _report() runs
        self.assertIn(self.pw.step(), ("daily", "stats", "points"))

    def test_meridian_daily_tallies(self):
        from meridian.modules.ships.store import VesselStore
        VesselStore(self.db).create_tables()                 # ships_visits and ships_samples
        day0 = pw.epoch_of("2026-11-22")
        self.db.executemany("INSERT INTO ships_visits(strait, mmsi, t_in, t_out, cls, moving, dir) VALUES(?,?,?,?,?,?,?)",
                            [("dover", 230000001, day0 + 3600, day0 + 5400, "cargo", 4, 2),
                             ("dover", 230000002, day0 + 7200, day0 + 9000, "tanker", 3, -2),
                             ("dover", 230000003, day0 + 9000, day0 + 9900, "passenger", 2, 1),
                             ("dover", 230000004, day0 + 9000, day0 + 9900, "cargo", 0, 0)])
        self.db.executemany("INSERT INTO ships_samples(strait, t, inside, moving) VALUES(?,?,?,?)",
                            [("dover", day0 + h * 3600, 20, 10) for h in range(22)])
        self.assertEqual(self.pw.tally_strait_days(["dover", "hormuz"]), 1)
        row = self.pw.strait_days("dover", "2026-11-01")["2026-11-22"]
        self.assertEqual((row["transits"], row["transits_ct"], row["vessels"], row["covered_hours"]), (3, 2, 4, 22))
        self.assertEqual(self.pw.strait_days("hormuz", "2026-11-01"), {})

    def test_heard_near_the_strait_is_not_coverage_of_it(self):
        # Samples are written when anything is heard within about 55 km (ships in the Sea of Marmara for the
        # Bosphorus); with nobody inside the strait those hours are not coverage.
        from meridian.modules.ships.store import VesselStore
        VesselStore(self.db).create_tables()
        day0 = pw.epoch_of("2026-11-22")
        self.db.executemany("INSERT INTO ships_samples(strait, t, inside, moving) VALUES(?,?,?,?)",
                            [("bosphorus", day0 + h * 3600, 0, 0) for h in range(24)])
        self.assertEqual(self.pw.tally_strait_days(["bosphorus"]), 1)
        row = self.pw.strait_days("bosphorus", "2026-11-01")["2026-11-22"]
        self.assertEqual((row["transits"], row["covered_hours"]), (0, 0))
        s = self.pw.summary("chokepoint1", 30)
        strait = {"id": "bosphorus", "coverage": True, "transits_24h": 0, "inside_now": 0, "unique_24h": 0}
        chk = pw.cross_check(s, strait, {"2026-11-15": {"transits": 0, "transits_ct": 0, "covered_hours": 0}})
        text = " ".join(chk["lines"])
        self.assertTrue(chk["meridian_gap"])
        self.assertIn("heard no vessels inside the strait itself", text)
        self.assertNotIn("The two counts differ", text)
        self.assertNotIn("counted 0", text)
        live = pw.cross_check(s, strait, {})
        self.assertTrue(live["meridian_gap"])
        self.assertIn("heard no vessels inside the strait itself in the last 24 hours", " ".join(live["lines"]))


class RegisteredModule(unittest.TestCase):
    """The ships module with PortWatch on: the task, the routes, the strait card and the AI tool."""

    @classmethod
    def setUpClass(cls):
        from meridian.config import Config
        from meridian.datasets import DatasetManager
        from meridian.registry import App
        from meridian.scheduler import Scheduler
        from meridian.modules import ships

        cls.tmp = tempfile.mkdtemp()
        cfg = Config(env_file=None, overrides={"MERIDIAN_DATA_DIR": cls.tmp, "AISSTREAM_API_KEY": "",
                                               "KYSTVERKET_AIS": "0"})
        cls.store = Store(Path(cls.tmp) / "meridian.db")
        cls.net = FakeNet()
        cls.app = App(cfg, cls.store, cls.net, LOG, Scheduler(LOG), DatasetManager(LOG))
        cls.m = cls.app.module("ships")
        ships.register(cls.m)
        cls.svc = cls.m.service
        cls.svc.portwatch.now = Clock()
        for _ in range(8):
            cls.app.scheduler.tasks["ships.portwatch"].fn()
            cls.svc.portwatch.now.t += 180
        cls.app.scheduler.tasks["ships.analysis"].fn()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, **query):
        from meridian.registry import Request
        route, params, _ = self.app.match("GET", path)
        self.assertIsNotNone(route, path)
        return route.handler(Request("GET", path, params, {k: [str(v)] for k, v in query.items()}, {}))

    def test_declared(self):
        self.assertIn("ships.portwatch", self.app.sources)
        self.assertTrue(self.app.sources["ships.portwatch"]["optional"])
        self.assertTrue(self.app.sources["ships.portwatch"]["ok"])
        self.assertFalse(self.app.config.declared["SHIPS_PORTWATCH"]["secret"])
        self.assertEqual(self.net.interval, (pw.HOST, pw.HOST_INTERVAL_S))
        self.assertIn("ships.strait-days", self.app.scheduler.tasks)

    def test_routes(self):
        out = self.get("/api/ships/chokepoints")
        self.assertTrue(out["enabled"])
        self.assertEqual([c["id"] for c in out["chokepoints"]], ["chokepoint1", "chokepoint6", "chokepoint12",
                                                                 "chokepoint28"])
        hz = next(c for c in out["chokepoints"] if c["id"] == "chokepoint6")
        self.assertEqual(hz["meridian"]["strait"], "hormuz")
        self.assertIn("Meridian hears no terrestrial AIS here", " ".join(hz["cross_check"]["lines"]))
        korea = next(c for c in out["chokepoints"] if c["id"] == "chokepoint12")
        self.assertIsNone(korea["strait"])
        self.assertIn("does not monitor this chokepoint", " ".join(korea["cross_check"]["lines"]))
        one = self.get("/api/ships/chokepoints/chokepoint12")
        self.assertEqual(one["name"], "Korea Strait")
        self.assertEqual(len(one["series"]), 90)
        from meridian.registry import HTTPError
        for bad, status in (("chokepoint99", 404), ("hormuz", 400)):
            with self.assertRaises(HTTPError) as cm:
                self.get(f"/api/ships/chokepoints/{bad}")
            self.assertEqual(cm.exception.status, status)
        series = self.get("/api/ships/chokepoints", series=7)["chokepoints"][0]["series"]
        self.assertEqual(len(series), 7)

    def test_strait_cards(self):
        card = self.get("/api/ships/straits/hormuz")
        p = card["portwatch"]
        self.assertEqual(p["id"], "chokepoint6")
        self.assertEqual(len(p["series"]), 90)
        self.assertIn("IMF PortWatch", p["credit"])
        self.assertTrue(p["cross_check"]["pw_gap"] and p["cross_check"]["meridian_gap"])
        self.assertIsNone(self.get("/api/ships/straits/gulf_of_finland")["portwatch"])     # no PortWatch twin
        brief = next(s for s in self.get("/api/ships/straits", geometry=0)["straits"] if s["id"] == "hormuz")
        self.assertEqual(brief["portwatch"]["latest"]["total"], 2)
        st = self.app.call_ai_tool("strait_status", {"strait": "hormuz"})
        self.assertEqual(st["straits"][0]["portwatch"]["mean_1y"], 22.3)
        # if PortWatch's summaries fail, the straits layer and the AI tool still answer (without PortWatch)
        real = self.svc.portwatch.summaries
        self.svc.portwatch.summaries = lambda **kw: (_ for _ in ()).throw(ValueError("day is out of range for month"))
        try:
            items = self.get("/api/ships/straits", geometry=0)["straits"]
            self.assertTrue(items and all(s["portwatch"] is None for s in items))
            self.assertNotIn("portwatch", self.app.call_ai_tool("strait_status", {"strait": "hormuz"})["straits"][0])
        finally:
            self.svc.portwatch.summaries = real
        # Just after a restart the analysis has not filled the straits cache yet: still a monitored strait.
        saved, self.svc.straits_cache = self.svc.straits_cache, []
        try:
            lines = self.get("/api/ships/straits/hormuz")["portwatch"]["cross_check"]["lines"]
            self.assertFalse(any("does not monitor" in line for line in lines))
            self.assertTrue(any("Meridian hears no terrestrial AIS here" in line for line in lines))
            self.svc.portwatch._cache = None
            hz = next(c for c in self.get("/api/ships/chokepoints")["chokepoints"] if c["id"] == "chokepoint6")
            self.assertEqual(hz["meridian"]["strait"], "hormuz")
        finally:
            self.svc.straits_cache = saved

    def test_ai_tool(self):
        r = self.app.call_ai_tool("chokepoint_transits", {"chokepoint": "hormuz", "days": 5})
        c = r["chokepoints"][0]
        self.assertEqual(c["name"], "Strait of Hormuz")
        self.assertEqual(len(c["daily"]), 5)
        self.assertTrue(any("below PortWatch's own 1-year average" in line for line in c["cross_check"]))
        self.assertIn("IMF PortWatch", r["source"])
        self.assertEqual(len(self.app.call_ai_tool("chokepoint_transits", {})["chokepoints"]), 4)
        self.assertIn("error", self.app.call_ai_tool("chokepoint_transits", {"chokepoint": "atlantis"}))
        self.assertIn("error", self.app.call_ai_tool("chokepoint_transits", {"days": "many"}))


if __name__ == "__main__":
    unittest.main()
