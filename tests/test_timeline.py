"""Timeline replay tests (CONTRACT.md section 15.4): aircraft history sampling, retention and caps
(flights/history.py), the 72-hour vessel history (ships/history.py), validation of the four
history endpoints, the history_near AI tool, and the browser's interpolation maths in Node
(tests/test_timeline_web.mjs, skipped when node is not installed).

Run: python -m unittest tests.test_timeline -v   (no network; synthetic history only)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.history import EventHistory
from meridian.net import NetError
from meridian.registry import App, HTTPError, Request
from meridian.scheduler import Scheduler
from meridian.store import Store
from meridian.modules.flights import history as FH
from meridian.modules.flights.tracker import Tracker

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_795_400_000.0
LOG = logging.getLogger("meridian.test.timeline")


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


class NoNet:
    """Every request fails: these tests never reach a network."""

    def __init__(self):
        self.calls = 0

    def set_host_interval(self, host, seconds):
        pass

    def request(self, url, **kw):
        self.calls += 1
        raise NetError("no network in tests", url=url)

    def get_json(self, url, **kw):
        return self.request(url, **kw)

    def get_text(self, url, **kw):
        return self.request(url, **kw)

    def get_bytes(self, url, **kw):
        return self.request(url, **kw)


def ob(hexid: str, t: float, lat: float, lon: float, *, alt=35000, trk=90.0, gs=450.0, **extra) -> dict:
    """An observation as the providers' parsers produce it."""
    o = {"hex": hexid, "lat": lat, "lon": lon, "pos_t": t, "seen_t": t, "alt": alt, "gnd": False, "gs": gs, "trk": trk,
         "cat": "A3", "typ": "B738", "desc": "BOEING 737-800"}
    o.update(extra)
    return o


def call(app: App, path: str, **query):
    route, params, _ = app.match("GET", path)
    if route is None:
        raise AssertionError(f"no route for {path}")
    return route.handler(Request("GET", path, params, {k: [str(v)] for k, v in query.items()}, {}))


# ---------------------------------------------------------------------------------------------
# Sampling (flights/history.py)
# ---------------------------------------------------------------------------------------------
class Sampling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.clock = Clock()
        self.tracker = Tracker(clock=self.clock)
        self.hist = FH.FlightsHistory(self.store, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def feed(self, *observations, notable_only=False, source="adsbfi"):
        self.clock.t = max(self.clock.t, max(o["pos_t"] for o in observations))
        res = self.tracker.ingest(list(observations), source, now=self.clock.t)
        return self.hist.sample(self.tracker, res.hexes, notable_only=notable_only)

    def stored(self, hexid=None):
        self.hist.tick(now=self.clock.t)
        sql = f"SELECT t, hex, lat, lon, alt, trk, cls, flags FROM {FH.TABLE}"
        params: tuple = ()
        if hexid:
            sql += " WHERE hex = ?"
            params = (int(hexid, 16),)
        return [tuple(r.values()) for r in self.store.query(sql + " ORDER BY t", params)]

    def test_first_sight_then_at_most_every_minute_and_at_least_every_five(self):
        t0 = NOW - 1000
        self.assertEqual(self.feed(ob("4ca2b3", t0, 53.0, -6.0)), 1)
        lon = -6.0
        for dt in range(10, 301, 10):                   # straight and level, reported every 10 s
            lon += 0.02
            self.feed(ob("4ca2b3", t0 + dt, 53.0, lon))
        rows = self.stored("4ca2b3")
        self.assertEqual([r[0] - int(t0) for r in rows], [0, 300])   # nothing in between on a straight line
        row = rows[0]
        self.assertEqual(row[1], 0x4ca2b3)
        self.assertEqual((row[2], row[3], row[4], row[5]), (5300000, -600000, 35000, 90))
        self.assertEqual(FH.CLASS_IDS[row[6]], "airliner")

    def test_turns_climbs_and_flag_changes_sample_once_a_minute_has_passed(self):
        t0 = NOW - 2000
        self.feed(ob("abc123", t0, 50.0, 10.0, trk=90.0))
        self.feed(ob("abc123", t0 + 30, 50.0, 10.1, trk=120.0))            # turned, but too soon
        self.assertEqual(len(self.stored("abc123")), 1)
        self.feed(ob("abc123", t0 + 60, 50.0, 10.2, trk=121.0))            # still turned: now it counts
        self.feed(ob("abc123", t0 + 130, 50.0, 10.3, trk=121.0, alt=36500))   # climbed 1,500 ft
        self.feed(ob("abc123", t0 + 200, 50.0, 10.4, trk=121.0, alt=36500, sq="7700"))   # emergency
        rows = self.stored("abc123")
        self.assertEqual([r[0] - int(t0) for r in rows], [0, 60, 130, 200])
        self.assertTrue(rows[-1][7] & 4)                                    # FLAG_EMG
        self.assertFalse(rows[0][7] & 4)

    def test_samples_are_never_closer_than_60_seconds(self):
        t0 = NOW - 5000
        trk = 0.0
        for i in range(400):                           # a tight turn reported every 3 s
            trk = (trk + 7.0) % 360
            self.feed(ob("00beef", t0 + i * 3, 40.0 + i * 0.001, 5.0, trk=trk, alt=10000 + i * 40))
        times = [r[0] for r in self.stored("00beef")]
        self.assertGreater(len(times), 10)
        self.assertTrue(all(b - a >= FH.MIN_STEP_S for a, b in zip(times, times[1:])), times)

    def test_last_observation_is_written_when_the_aircraft_stops_arriving(self):
        t0 = NOW - 3000
        self.clock.t = t0                              # the clock follows the observations
        for dt in range(0, 181, 15):
            self.feed(ob("123abc", t0 + dt, 60.0, 24.0 + dt * 0.001))
        self.assertEqual([r[0] - int(t0) for r in self.stored("123abc")], [0])
        self.clock.t = t0 + 180 + FH.TAIL_AFTER_S + 1
        self.assertEqual([r[0] - int(t0) for r in self.stored("123abc")], [0, 180])
        self.assertEqual(self.hist.stats["tails"], 1)

    def test_a_restart_keeps_the_spacing_and_the_callsign_spell(self):
        t0 = NOW - 600
        self.clock.t = t0
        self.feed(ob("4b1801", t0, 47.0, 8.0, cs="SWR12"))
        self.stored()
        hist2 = FH.FlightsHistory(self.store, clock=self.clock)          # the server restarted
        self.assertEqual(hist2.stats["restored"], 1)
        self.hist = hist2
        self.feed(ob("4b1801", t0 + 20, 47.0, 8.1, cs="SWR12", trk=170.0))    # turned, but 20 s later
        self.assertEqual([r[0] - int(t0) for r in self.stored("4b1801")], [0])
        self.feed(ob("4b1801", t0 + 70, 47.0, 8.2, cs="SWR12", trk=170.0))
        self.assertEqual([r[0] - int(t0) for r in self.stored("4b1801")], [0, 70])
        self.assertEqual(self.store.scalar(f"SELECT COUNT(*) FROM {FH.IDENT}"), 1)   # the same spell
        # A stale position from before the restored half hour, a few seconds from a stored sample.
        old = int(NOW - 3000)
        with self.store.tx() as c:
            c.execute(FH._INSERT, (old, 0x4b1802, 0, 0, None, None, 0, 0))
        self.feed(ob("4b1802", old + 25, 0.001, 0.001))
        self.assertEqual(len(self.stored("4b1802")), 1)

    def test_privacy_programme_aircraft_are_never_kept(self):
        t0 = NOW - 900
        self.feed(ob("a1b2c3", t0, 40.0, -74.0, db_flags=8))     # LADD
        self.feed(ob("a1b2c4", t0, 40.1, -74.0, db_flags=4))     # PIA
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.hist.stats["private"], 2)

    def test_hotspot_batches_keep_only_military_emergency_and_listed_aircraft(self):
        t0 = NOW - 700
        self.feed(ob("4d0001", t0, 55.0, 20.0), ob("4d0002", t0, 55.1, 20.1, db_flags=1),
                  ob("4d0003", t0, 55.2, 20.2, sq="7700"), notable_only=True)
        kept = {f"{r[1]:06x}" for r in self.stored()}
        self.assertEqual(kept, {"4d0002", "4d0003"})

    def test_callsign_spells(self):
        t0 = NOW - 4000
        self.feed(ob("4ca2b3", t0, 53.0, -6.0, cs="RYR8NE", reg="EI-DCL"))
        self.feed(ob("4ca2b3", t0 + 400, 53.0, -5.0, cs="RYR8NE"))
        self.feed(ob("4ca2b3", t0 + 3000, 51.5, -0.4, cs="RYR9AB"))
        self.stored()
        spells = self.hist.spells_for({0x4ca2b3}, t0 - 10, t0 + 4000)[0x4ca2b3]
        self.assertEqual([(s["cs"], s["from"] - int(t0), s["to"] - int(t0)) for s in spells],
                         [("RYR8NE", 0, 400), ("RYR9AB", 3000, 3000)])
        self.assertEqual(spells[0]["reg"], "EI-DCL")
        self.assertEqual(self.hist.spells_for({0x4ca2b3}, t0 + 2000, t0 + 4000)[0x4ca2b3][0]["cs"], "RYR9AB")

    def test_window_box_antimeridian_limit_and_unwritten_samples(self):
        t0 = NOW - 600
        self.feed(ob("aaaaa1", t0, 10.0, 179.5), ob("aaaaa2", t0, 10.0, -179.5), ob("aaaaa3", t0, 10.0, 0.0))
        self.stored()
        self.feed(ob("aaaaa4", t0 + 5, 10.5, 179.9))           # still in the write buffer
        rows, trunc = self.hist.window(t0 - 1, t0 + 10, (179.0, 5.0, -179.0, 15.0))
        self.assertEqual({f"{r[1]:06x}" for r in rows}, {"aaaaa1", "aaaaa2", "aaaaa4"})
        self.assertFalse(trunc)
        rows, trunc = self.hist.window(t0 - 1, t0 + 10, None, limit=2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(trunc)
        self.assertEqual(self.hist.window(t0 + 60, t0 + 120)[0], [])
        pos = FH.position(rows[0])
        self.assertEqual(set(pos), {"hex", "t", "lat", "lon", "alt", "trk", "cls", "flags"})


class Retention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def insert(self, rows):
        with self.store.tx() as c:
            c.executemany(FH._INSERT, rows)

    def test_prune_keeps_72_hours(self):
        hist = FH.FlightsHistory(self.store, clock=lambda: NOW)
        self.insert([(int(NOW - 73 * 3600 + i), 0x100000 + i, 0, 0, None, None, 0, 0) for i in range(50)]
                    + [(int(NOW - 71 * 3600 + i), 0x200000 + i, 0, 0, None, None, 0, 0) for i in range(30)])
        with self.store.tx() as c:
            c.execute(FH._UPSERT_IDENT, (1, int(NOW - 80 * 3600), int(NOW - 74 * 3600), "OLD1", "", "", ""))
            c.execute(FH._UPSERT_IDENT, (2, int(NOW - 70 * 3600), int(NOW - 69 * 3600), "NEW1", "", "", ""))
        out = hist.prune(now=NOW)
        self.assertEqual((out["deleted"], out["rows"]), (50, 30))
        self.assertEqual(self.store.scalar(f"SELECT MIN(t) FROM {FH.TABLE}"), int(NOW - 71 * 3600))
        self.assertEqual([r["cs"] for r in self.store.query(f"SELECT cs FROM {FH.IDENT}")], ["NEW1"])
        self.assertEqual(sum(b["samples"] for b in hist.coverage(NOW - 72 * 3600, NOW)), 30)

    def test_row_cap_removes_the_oldest_first(self):
        hist = FH.FlightsHistory(self.store, clock=lambda: NOW, max_rows=1000)
        self.insert([(int(NOW - 3600 * 10 + i * 10), 0x300000 + i % 50, 0, 0, None, None, 0, 0) for i in range(1500)])
        out = hist.prune(now=NOW)
        self.assertEqual(out["rows"], 1000)
        self.assertEqual(self.store.scalar(f"SELECT COUNT(*) FROM {FH.TABLE}"), 1000)
        self.assertEqual(self.store.scalar(f"SELECT MIN(t) FROM {FH.TABLE}"), int(NOW - 3600 * 10 + 500 * 10))

    def test_write_buffer_and_state_are_bounded(self):
        clock = Clock(NOW)
        hist = FH.FlightsHistory(self.store, clock=clock)
        tracker = Tracker(clock=clock)
        with mock.patch.object(FH, "PENDING_MAX", 100):
            obs = [ob(f"{0x500000 + i:06x}", NOW - 10, 1.0 + i * 0.001, 1.0) for i in range(250)]
            res = tracker.ingest(obs, "adsbfi", now=NOW)
            hist.sample(tracker, res.hexes)
            self.assertEqual(len(hist.pending), 100)
            self.assertEqual(hist.stats["dropped"], 150)
        with mock.patch.object(FH, "STATE_MAX", 50):
            hist2 = FH.FlightsHistory(self.store, clock=clock)
            hist2.sample(tracker, res.hexes)
            self.assertLessEqual(len(hist2.state), 50)


# ---------------------------------------------------------------------------------------------
# The flights module with its hook and routes
# ---------------------------------------------------------------------------------------------
class FlightsModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(None, overrides={"FLIGHTS_GPSJAM_SAMPLER": "0", "MERIDIAN_DATA_DIR": self.tmp.name})
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.net = NoNet()
        self.app = App(cfg, self.store, self.net, LOG, Scheduler(LOG), DatasetManager(LOG))
        self.m = self.app.module("flights")
        from meridian.modules import flights
        flights.register(self.m)
        self.svc = self.m.flights
        self.now = float(int(time.time()))      # whole seconds: stored times are integers

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_registration(self):
        self.assertIsInstance(self.svc.history, FH.FlightsHistory)
        self.assertIn("flights.history", self.app.scheduler.tasks)
        self.assertIn("flights.history", self.app.sources)
        self.assertFalse(self.app.config.declared["FLIGHTS_HISTORY_MAX_ROWS"]["secret"])

    def test_hook_samples_what_the_service_absorbs(self):
        t = self.now - 30
        self.svc._absorb([ob("4ca2b3", t, 53.0, -6.0, cs="RYR8NE")], "adsbfi", t)
        self.svc._absorb([ob("4ca2b4", t, 53.0, -6.1), ob("4ca2b5", t, 53.0, -6.2, db_flags=1)], "adsbfi", t,
                         circle=(53.0, -6.0, 300.0), history="notable")
        self.assertEqual(sorted(f"{r[1]:06x}" for r in self.svc.history.pending), ["4ca2b3", "4ca2b5"])
        self.assertEqual(self.net.calls, 0)

    def test_window_endpoint(self):
        t = int(self.now - 120)
        self.svc._absorb([ob("4ca2b3", t, 53.0, -6.0, cs="RYR8NE", reg="EI-DCL")], "adsbfi", t)
        self.app.scheduler.tasks["flights.history"].fn()        # write to SQLite
        out = call(self.app, "/api/flights/history", **{"from": t - 60, "to": t + 60})
        self.assertEqual(out["count"], 1)
        self.assertFalse(out["truncated"])
        p = out["positions"][0]
        self.assertEqual(p, {"hex": "4ca2b3", "t": t, "lat": 53.0, "lon": -6.0, "alt": 35000, "trk": 90,
                             "cls": "airliner", "flags": 0})
        self.assertEqual(out["idents"]["4ca2b3"]["cs"], "RYR8NE")
        self.assertEqual(out["available"], {"from": t, "to": t})
        rows = call(self.app, "/api/flights/history", **{"from": (t - 60) * 1000, "to": (t + 60) * 1000,
                                                       "format": "rows", "bbox": "-7,52,-5,54"})
        self.assertEqual(rows["fields"], ["hex", "t", "lat", "lon", "alt", "trk", "cls", "flags"])
        self.assertEqual(rows["rows"], [["4ca2b3", t, 53.0, -6.0, 35000, 90, "airliner", 0]])
        self.assertEqual(call(self.app, "/api/flights/history", **{"from": t - 60, "to": t + 60,
                                                                 "bbox": "10,10,20,20"})["count"], 0)
        default = call(self.app, "/api/flights/history")
        self.assertAlmostEqual(default["to"] - default["from"], 3600, delta=1)

    def test_aircraft_and_coverage_endpoints(self):
        t = int(self.now - 900)
        for i in range(4):
            self.svc._absorb([ob("abcdef", t + i * 300, 50.0, 8.0 + i * 0.3, cs="DLH4AB")], "adsbfi", t + i * 300)
        self.app.scheduler.tasks["flights.history"].fn()
        out = call(self.app, "/api/flights/history/abcdef")
        self.assertEqual(out["hex"], "abcdef")
        self.assertEqual([p["t"] - t for p in out["positions"]], [0, 300, 600, 900])
        self.assertEqual(out["idents"][0]["cs"], "DLH4AB")
        none = call(self.app, "/api/flights/history/abcdee")
        self.assertEqual((none["count"], none["positions"]), (0, []))
        self.assertIn("No recorded positions", none["note"])
        cov = call(self.app, "/api/flights/history/coverage")
        self.assertEqual(sum(b["samples"] for b in cov["by_hour"]), 4)
        self.assertTrue(all(b["t"] % 3600 == 0 for b in cov["by_hour"]))

    def test_validation(self):
        t = int(self.now)
        bad = [("/api/flights/history", {"from": t, "to": t - 10}),
               ("/api/flights/history", {"from": t - 7 * 3600, "to": t}),
               ("/api/flights/history", {"from": "yesterday"}),
               ("/api/flights/history", {"from": -5}),
               ("/api/flights/history", {"bbox": "1,2,3"}),
               ("/api/flights/history", {"bbox": "0,80,10,70"}),
               ("/api/flights/history", {"limit": 0}),
               ("/api/flights/history", {"limit": 50001}),
               ("/api/flights/history", {"format": "xml"}),
               ("/api/flights/history/zzz", {}),
               ("/api/flights/history/4ca2b", {}),
               ("/api/flights/history/4ca2b3", {"from": t - 100 * 3600, "to": t}),
               ("/api/flights/history/coverage", {"from": t, "to": t - 1})]
        for path, query in bad:
            with self.subTest(path=path, query=query):
                with self.assertRaises(HTTPError) as cm:
                    call(self.app, path, **query)
                self.assertEqual((cm.exception.status, cm.exception.code), (400, "BAD_ARG"))

    def test_off_switch(self):
        cfg = Config(None, overrides={"FLIGHTS_GPSJAM_SAMPLER": "0", "FLIGHTS_HISTORY": "0"})
        app = App(cfg, self.store, NoNet(), LOG, Scheduler(LOG), DatasetManager(LOG))
        m = app.module("flights")
        from meridian.modules import flights
        flights.register(m)
        self.assertIsNone(m.flights.history)
        m.flights._absorb([ob("4ca2b3", self.now, 53.0, -6.0)], "adsbfi", self.now)   # the hook is a no-op
        self.assertIn("off", call(app, "/api/flights/history")["note"])


# ---------------------------------------------------------------------------------------------
# Ships: 72 h of track and its routes
# ---------------------------------------------------------------------------------------------
class ShipsModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = Config(env_file=None, overrides={"MERIDIAN_DATA_DIR": self.tmp, "AISSTREAM_API_KEY": "",
                                               "KYSTVERKET_AIS": "0"})
        self.store = Store(Path(self.tmp) / "meridian.db")
        self.app = App(cfg, self.store, NoNet(), LOG, Scheduler(LOG), DatasetManager(LOG))
        self.m = self.app.module("ships")
        from meridian.modules import ships
        ships.register(self.m)
        self.svc = self.m.service
        self.hist = self.svc.history
        self.now = float(int(time.time()))      # whole seconds: ships_track times are integers

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def track_rows(self, rows):
        self.store.executemany("INSERT OR REPLACE INTO ships_track(mmsi, t, lat, lon, sog, cog) VALUES(?,?,?,?,?,?)", rows)

    def test_track_retention_is_72_hours(self):
        from meridian.modules.ships import store as S
        self.assertEqual(S.KEEP_TRACK_S, 72 * 3600)
        now = int(self.now)
        self.track_rows([(230000001, now - 73 * 3600, 6000000, 2400000, 100, 900),
                         (230000001, now - 48 * 3600, 6000100, 2400100, 100, 900)])
        self.svc.store.prune()
        self.assertEqual([r["t"] for r in self.store.query("SELECT t FROM ships_track")], [now - 48 * 3600])
        self.assertIn("ships.history-cap", self.app.scheduler.tasks)

    def test_window_merges_stored_unwritten_and_latest_points(self):
        st = self.svc.store
        now = self.now
        st.static(230000002, now - 900, name="TEST CARGO", ship_type=70)
        st.position(230000002, now - 900, 60.0, 24.0, sog=12.0, cog=90.0)     # first point: track
        st.flush()                                                            # ... written
        st.position(230000002, now - 120, 60.0, 24.1, sog=12.0, cog=91.0)     # 13 min later: track, unwritten
        st.position(230000002, now - 60, 60.0, 24.106, sog=12.0, cog=91.0)    # latest fix, not a track point
        rows, trunc = self.hist.window(now - 1000, now, (23.0, 59.0, 25.0, 61.0))
        self.assertEqual([round(now - r[1]) for r in rows], [900, 120, 60])
        self.assertFalse(trunc)
        out = call(self.app, "/api/ships/history", **{"from": now - 1000, "to": now, "bbox": "23,59,25,61"})
        self.assertEqual(out["count"], 3)
        p = out["positions"][0]
        self.assertEqual(set(p), {"mmsi", "t", "lat", "lon", "sog", "cog", "cls"})
        self.assertEqual((p["mmsi"], p["lat"], p["lon"], p["sog"], p["cog"], p["cls"]),
                         ("230000002", 60.0, 24.0, 12.0, 90.0, "cargo"))
        self.assertEqual(out["idents"]["230000002"]["name"], "TEST CARGO")
        compact = call(self.app, "/api/ships/history", **{"from": now - 1000, "to": now, "format": "rows"})
        self.assertEqual(compact["fields"], ["mmsi", "t", "lat", "lon", "sog", "cog", "cls"])
        older = call(self.app, "/api/ships/history", **{"from": now - 7200, "to": now - 3600})
        self.assertEqual(older["count"], 0)                    # no latest fix outside recent windows
        one = call(self.app, "/api/ships/history/230000002")
        self.assertEqual((one["name"], one["cls"], one["count"]), ("TEST CARGO", "cargo", 3))
        cov = call(self.app, "/api/ships/history/coverage")
        self.assertGreaterEqual(sum(b["samples"] for b in cov["by_hour"]), 2)

    def test_validation(self):
        t = int(self.now)
        bad = [("/api/ships/history", {"from": t, "to": t - 1}),
               ("/api/ships/history", {"from": t - 7 * 3600, "to": t}),
               ("/api/ships/history", {"to": "soon"}),
               ("/api/ships/history", {"bbox": "a,b,c,d"}),
               ("/api/ships/history", {"limit": 50001}),
               ("/api/ships/history", {"format": "csv"}),
               ("/api/ships/history/12345", {}),
               ("/api/ships/history/2300000020", {}),
               ("/api/ships/history/230000002", {"from": t - 100 * 3600, "to": t})]
        for path, query in bad:
            with self.subTest(path=path, query=query):
                with self.assertRaises(HTTPError) as cm:
                    call(self.app, path, **query)
                self.assertEqual(cm.exception.status, 400)

    def test_row_cap(self):
        self.hist.max_rows = 1000
        now = int(self.now)
        self.track_rows([(230000003 + i % 7, now - 20000 + i * 10, 6000000, 2400000, 0, 0) for i in range(1500)])
        out = self.hist.enforce_cap()
        self.assertEqual((out["rows"], out["deleted"]), (1000, 500))
        self.assertEqual(self.store.scalar("SELECT MIN(t) FROM ships_track"), now - 20000 + 500 * 10)


# ---------------------------------------------------------------------------------------------
# The history_near AI tool
# ---------------------------------------------------------------------------------------------
class HistoryNear(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.app = App(Config(None), self.store, NoNet(), LOG, Scheduler(LOG), DatasetManager(LOG))
        self.items: list[dict] = []
        self.app._history = EventHistory(self.store, lambda since: [dict(i) for i in self.items], log=LOG,
                                         clock=lambda: NOW)
        from meridian.modules import timeline
        self.m = self.app.module("timeline")
        timeline.register(self.m)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ev(self, id, lat, lon, t, sev, kind="earthquake", **extra):
        return {"module": "hazards", "kind": kind, "id": id, "title": f"{kind} {id}", "lat": lat, "lon": lon,
                "time": t, "severity": sev, "layer": "hazards.quakes", **extra}

    def test_events_near_a_place_in_a_window(self):
        self.items = [self.ev("near-big", 54.40, 18.70, NOW - 3600, 7, mag=5.1),
                      self.ev("near-small", 54.35, 18.60, NOW - 1800, 3),
                      self.ev("far", 50.0, 10.0, NOW - 1800, 9),
                      self.ev("old", 54.40, 18.65, NOW - 40 * 3600, 8),
                      self.ev("jam", 54.5, 18.9, NOW - 600, 5, kind="gps_jam", first_time=NOW - 7200)]
        self.app.history.snapshot(now=NOW)
        out = self.app.call_ai_tool("history_near", {"lat": 54.37, "lon": 18.64, "from": NOW - 6 * 3600,
                                                     "to": NOW, "radius_km": 50})
        self.assertEqual([e["id"] for e in out["events"]], ["near-big", "jam", "near-small"])
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["by_kind"]["earthquake"], {"count": 2, "max_severity": 7})
        first = out["events"][0]
        self.assertEqual(first["mag"], 5.1)
        self.assertLess(first["distance_km"], 10)
        self.assertTrue(first["time_utc"].endswith("Z"))
        self.assertIn("first_time_utc", out["events"][1])
        iso = self.app.call_ai_tool("history_near", {"lat": 54.37, "lon": 18.64, "radius_km": 50, "kinds": ["gps_jam"],
                                                     "from": "2026-11-17T03:00:00Z", "to": str(int(NOW * 1000))})
        self.assertEqual([e["id"] for e in iso["events"]], ["jam"])
        only = self.app.call_ai_tool("history_near", {"lat": 54.37, "lon": 18.64, "radius_km": 50, "min_severity": 6,
                                                      "from": NOW - 6 * 3600, "to": NOW, "limit": 1})
        self.assertEqual([e["id"] for e in only["events"]], ["near-big"])

    def test_bad_arguments(self):
        tool = lambda **a: self.app.call_ai_tool("history_near", a)   # noqa: E731
        for args in ({"lon": 18.0}, {"lat": 95, "lon": 0}, {"lat": 1, "lon": 1, "radius_km": 0},
                     {"lat": 1, "lon": 1, "from": NOW, "to": NOW - 10}, {"lat": 1, "lon": 1, "from": "last tuesday"},
                     {"lat": 1, "lon": 1, "from": NOW - 40 * 86400, "to": NOW}, {"lat": True, "lon": 1},
                     {"lat": 1, "lon": 1, "limit": 500}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    tool(**args)

    def test_no_events_provider(self):
        self.assertFalse(any(mod == "timeline" for mod, _fn in self.app._events))


# ---------------------------------------------------------------------------------------------
# Browser maths (Node)
# ---------------------------------------------------------------------------------------------
class BrowserMaths(unittest.TestCase):
    def test_interpolation_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run([node, str(ROOT / "tests" / "test_timeline_web.mjs")], cwd=ROOT, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("passed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
