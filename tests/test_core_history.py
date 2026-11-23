"""Event history tests (CONTRACT.md section 15.3): upserts, retention, queries, summaries, failing
providers and the /api/core/history endpoints.

Run: python -m unittest tests.test_core_history -v   (no network)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from meridian import history as H
from meridian.config import Config
from meridian.core_api import register_core
from meridian.datasets import DatasetManager
from meridian.history import EventHistory
from meridian.httpserver import make_server
from meridian.net import Net
from meridian.registry import App
from meridian.scheduler import Scheduler
from meridian.store import Store

NOW = 1_795_400_000.0
DAY = 86400.0
LOG = logging.getLogger("meridian.test.history")


def ev(kind="earthquake", id="us1", module="hazards", t=NOW - 600, sev=5, lat=10.0, lon=20.0, **extra):
    return {"kind": kind, "id": id, "module": module, "title": f"{kind} {id}", "subtitle": "test",
            "lat": lat, "lon": lon, "time": t, "severity": sev, "layer": "hazards.quakes", **extra}


class Feed:
    """A stand-in for App.collect_events: returns whatever the test sets."""

    def __init__(self):
        self.items: list = []
        self.calls: list[float] = []

    def __call__(self, since):
        self.calls.append(since)
        return [dict(i) if isinstance(i, dict) else i for i in self.items]


class HistoryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.feed = Feed()
        self.h = EventHistory(self.store, self.feed, log=LOG, clock=lambda: NOW)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def rows(self):
        return self.store.query(f"SELECT * FROM {H.TABLE} ORDER BY module, kind, id")


class UpsertTests(HistoryBase):
    def test_upsert_keeps_one_row_per_key_with_first_and_last_seen(self):
        self.feed.items = [ev(sev=5, mag=4.6)]
        r1 = self.h.snapshot(now=NOW)
        self.assertEqual((r1["events"], r1["new"]), (1, 1))
        self.assertEqual(self.feed.calls, [NOW - 30 * DAY])          # asks for the retention window
        # Later: lower severity, new title and payload. Severity keeps the highest, the rest updates.
        self.feed.items = [ev(sev=3, mag=4.9, title="M4.9 revised")]
        r2 = self.h.snapshot(now=NOW + 300)
        self.assertEqual((r2["new"], r2["updated"]), (0, 1))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["severity"], 5)
        self.assertEqual(row["title"], "M4.9 revised")
        self.assertEqual(json.loads(row["payload"]), {"mag": 4.9})
        self.assertEqual(row["first_seen"], NOW)
        self.assertEqual(row["last_seen"], NOW + 300)
        # A higher severity replaces it.
        self.feed.items = [ev(sev=8)]
        self.h.snapshot(now=NOW + 600)
        self.assertEqual(self.rows()[0]["severity"], 8)

    def test_same_id_in_other_module_or_kind_is_another_row(self):
        self.feed.items = [ev(), ev(module="intel"), ev(kind="fire")]
        self.h.snapshot(now=NOW)
        self.assertEqual(len(self.rows()), 3)

    def test_ongoing_event_widens_its_time_span(self):
        self.feed.items = [ev(kind="storm", id="al01", t=NOW - 7200)]
        self.h.snapshot(now=NOW)
        self.feed.items = [ev(kind="storm", id="al01", t=NOW - 60, lat=11.0)]
        self.h.snapshot(now=NOW + 300)
        row = self.rows()[0]
        self.assertEqual((row["first_time"], row["time"], row["lat"]), (NOW - 7200, NOW - 60, 11.0))
        # A stale report with an older time never moves the span backwards.
        self.feed.items = [ev(kind="storm", id="al01", t=NOW - 3600, lat=11.0)]
        self.h.snapshot(now=NOW + 600)
        row = self.rows()[0]
        self.assertEqual((row["first_time"], row["time"]), (NOW - 7200, NOW - 60))
        # A provider's own first_time (flights does this) starts the span.
        self.feed.items = [ev(kind="aircraft", id="4ca2b3", t=NOW - 30, first_time=NOW - 5000)]
        self.h.snapshot(now=NOW + 900)
        air = [r for r in self.rows() if r["kind"] == "aircraft"][0]
        self.assertEqual((air["first_time"], air["time"]), (NOW - 5000, NOW - 30))
        self.assertNotIn("first_time", json.loads(air["payload"]))

    def test_unchanged_event_rewrites_last_seen_only_every_step(self):
        self.feed.items = [ev()]
        self.h.snapshot(now=NOW)
        r = self.h.snapshot(now=NOW + 300)                      # nothing changed, within the step
        self.assertEqual(r["updated"], 0)
        self.assertEqual(self.rows()[0]["last_seen"], NOW)
        r = self.h.snapshot(now=NOW + H.LAST_SEEN_STEP_S)
        self.assertEqual(r["updated"], 1)
        self.assertEqual(self.rows()[0]["last_seen"], NOW + H.LAST_SEEN_STEP_S)

    def test_duplicate_keys_in_one_snapshot_keep_the_most_severe(self):
        self.feed.items = [ev(kind="ship", id="123", sev=5, title="Dark ship", t=NOW - 100),
                           ev(kind="ship", id="123", sev=6, title="Sanctioned vessel seen", t=NOW - 900)]
        r = self.h.snapshot(now=NOW)
        self.assertEqual(r["events"], 1)
        row = self.rows()[0]
        self.assertEqual((row["title"], row["severity"]), ("Sanctioned vessel seen", 6))
        self.assertEqual((row["first_time"], row["time"]), (NOW - 900, NOW - 100))

    def test_bad_events_are_skipped_and_odd_values_cleaned(self):
        self.feed.items = [
            "not a dict", None, {"kind": "x"},                      # no id
            ev(id=""), ev(id={"a": 1}), ev(kind=""),
            ev(id="badlat", lat=95), ev(id="nolat", lat=None), ev(id="boollat", lat=True),
            ev(id="badtime", t="yesterday"), ev(id="ancient", t=5), ev(id="far", t=NOW + 2 * 366 * DAY),
            ev(id="ms", t=(NOW - 60) * 1000),                       # milliseconds are converted
            ev(id="iso", t="2026-11-22T10:00:00Z"),
            ev(id="notime", t=None),                                # missing time: treated as now
            ev(id="weird", sev="high", title=None, mag=float("nan"), obj=object(), lat=10.123456789),
            ev(id=12345, module=None),
        ]
        r = self.h.snapshot(now=NOW)
        rows = {r_["id"]: r_ for r_ in self.rows()}
        self.assertEqual(sorted(rows), ["12345", "iso", "ms", "notime", "weird"])
        self.assertEqual(r["skipped"], 12)
        self.assertEqual(rows["ms"]["time"], NOW - 60)
        self.assertEqual(rows["notime"]["time"], NOW)
        self.assertEqual(rows["12345"]["module"], "unknown")
        weird = rows["weird"]
        self.assertEqual((weird["severity"], weird["title"], weird["lat"]), (0, "", 10.12346))
        payload = json.loads(weird["payload"])
        self.assertIsNone(payload["mag"])                           # NaN became null
        self.assertIsInstance(payload["obj"], str)

    def test_big_payloads_lose_their_largest_fields(self):
        self.feed.items = [ev(geometry={"coords": "x" * 20000}, note="kept")]
        self.h.snapshot(now=NOW)
        payload = json.loads(self.rows()[0]["payload"])
        self.assertEqual(payload, {"note": "kept", "payload_truncated": True})

    def test_snapshot_cap_keeps_the_most_severe(self):
        self.feed.items = [ev(id=f"e{i}", sev=i % 10) for i in range(30)]
        with mock.patch.object(H, "MAX_PER_SNAPSHOT", 5):
            r = self.h.snapshot(now=NOW)
        self.assertEqual((r["events"], r["dropped"]), (5, 25))
        self.assertEqual(sorted(row["severity"] for row in self.rows()), [8, 8, 9, 9, 9])

    def test_many_events_are_written_in_chunks(self):
        self.feed.items = [ev(id=f"e{i}", lat=(i % 170) - 85.0, lon=(i % 350) - 175.0) for i in range(1234)]
        r = self.h.snapshot(now=NOW)
        self.assertEqual((r["events"], r["new"]), (1234, 1234))
        self.assertEqual(self.h.rows, 1234)


class FailingProviderTests(unittest.TestCase):
    def test_one_failing_provider_never_breaks_a_snapshot(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            cfg = Config(None)
            app = App(cfg, Store(Path(d) / "t.db"), Net(cfg), LOG, Scheduler(LOG), DatasetManager(LOG))
            good, bad, odd, junk = (app.module(n) for n in ("good", "bad", "odd", "junk"))
            good.events(lambda since: [ev(id="ok1")])

            def explode(since):
                raise RuntimeError("feed down")

            bad.events(explode)
            odd.events(lambda since: [ev(id="t1", t="not a time"), ev(id="t2", t=NOW - 100), 42])
            junk.events(lambda since: 17)                         # not even a list
            with self.assertLogs("meridian.test.history", "WARNING"):
                r = app.history.snapshot(now=NOW)
            self.assertEqual(r["events"], 2)
            ids = sorted(e["id"] for e in app.history.events(NOW - DAY))
            self.assertEqual(ids, ["ok1", "t2"])
            self.assertIs(app.history, app.history)               # one instance per app
            app.store.close()

    def test_a_collector_that_raises_records_an_empty_snapshot(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            store = Store(Path(d) / "t.db")

            def broken(since):
                raise ValueError("boom")

            h = EventHistory(store, broken, log=LOG)
            with self.assertLogs("meridian.test.history", "WARNING"):
                r = h.snapshot(now=NOW)
            self.assertEqual((r["events"], r["new"]), (0, 0))
            store.close()

    def test_overlapping_snapshots_are_skipped(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            store = Store(Path(d) / "t.db")
            gate = threading.Event()
            entered = threading.Event()

            def slow(since):
                entered.set()
                gate.wait(5)
                return [ev()]

            h = EventHistory(store, slow, log=LOG)
            t = threading.Thread(target=h.snapshot, kwargs={"now": NOW})
            t.start()
            entered.wait(5)
            self.assertTrue(h.snapshot(now=NOW).get("skipped"))
            gate.set()
            t.join(5)
            self.assertEqual(h.rows, 1)
            store.close()


class RetentionTests(HistoryBase):
    def test_prune_drops_events_older_than_30_days(self):
        self.feed.items = [ev(id="old", t=NOW - 36 * DAY)]
        self.h.snapshot(now=NOW - 35 * DAY)                      # happened and last seen long ago
        self.feed.items = [ev(id="still-reported", t=NOW - 31 * DAY), ev(id="recent", t=NOW - 2 * DAY),
                           ev(id="new", t=NOW - 60)]
        self.h.snapshot(now=NOW - 60)
        r = self.h.prune(now=NOW)
        self.assertEqual(r["deleted"], 1)
        self.assertEqual(sorted(x["id"] for x in self.rows()), ["new", "recent", "still-reported"])
        self.assertEqual(self.h.rows, 3)
        r = self.h.prune(now=NOW + 3 * DAY)                     # "recent" is now 5 days old: kept
        self.assertEqual((r["deleted"], self.h.rows), (0, 3))
        # 30 days after its last sighting the long-reported one goes, with everything else this old.
        r = self.h.prune(now=NOW + 31 * DAY)
        self.assertEqual((r["deleted"], self.rows()), (3, []))

    def test_row_cap(self):
        self.feed.items = [ev(id=f"e{i}", t=NOW - i) for i in range(20)]
        self.h.snapshot(now=NOW)
        with mock.patch.object(H, "MAX_ROWS", 8):
            r = self.h.prune(now=NOW)
        self.assertEqual((r["deleted"], r["capped"]), (0, 12))
        self.assertEqual(len(self.rows()), 8)

    def test_schedule_registers_snapshot_and_prune_once(self):
        sched = Scheduler(LOG)
        self.h.schedule(sched)
        self.h.schedule(sched)
        tasks = {t["name"]: t for t in sched.info()}
        self.assertEqual(tasks["core.history"]["every"], 300.0)
        self.assertEqual(tasks["core.history_prune"]["every"], 86400.0)
        self.assertTrue(self.h.info()["scheduled"])


class QueryTests(HistoryBase):
    def setUp(self):
        super().setUp()
        self.feed.items = [
            ev(id="q1", t=NOW - 3 * 3600, sev=4, lat=51.5, lon=-0.1),                          # London
            ev(id="q2", t=NOW - 26 * 3600, sev=7, lat=35.7, lon=139.7),                        # Tokyo, yesterday
            ev(kind="fire", id="f1", t=NOW - 1800, sev=5, lat=-33.9, lon=151.2, module="hazards"),
            ev(kind="news", id="n1", t=NOW - 600, sev=6.5, lat=48.85, lon=2.35, module="intel"),
            ev(kind="ship", id="s1", t=NOW - 10 * 3600, sev=6, lat=0.5, lon=179.5, module="ships"),
            ev(kind="ship", id="s2", t=NOW - 3600, sev=3, lat=0.5, lon=-179.5, module="ships"),
        ]
        self.h.snapshot(now=NOW - 20 * 3600)
        # The storm is reported from 20 h ago until now: an ongoing event.
        self.feed.items = [ev(kind="storm", id="al01", t=NOW - 20 * 3600, sev=6, lat=20, lon=-60)]
        self.h.snapshot(now=NOW - 20 * 3600 + 1)
        self.feed.items = [ev(kind="storm", id="al01", t=NOW - 120, sev=7, lat=22, lon=-62)]
        self.h.snapshot(now=NOW)

    def ids(self, *a, **kw):
        return [e["id"] for e in self.h.events(*a, **kw)]

    def test_window_and_ordering(self):
        self.assertEqual(self.ids(NOW - DAY), ["al01", "n1", "f1", "s2", "q1", "s1"])
        self.assertEqual(self.ids(NOW - 2 * DAY, NOW - DAY), ["q2"])
        # One-off events need their time in the window; the ongoing storm matches any part of its span.
        self.assertEqual(self.ids(NOW - 12 * 3600, NOW - 9 * 3600), ["al01", "s1"])
        self.assertEqual(self.ids(NOW + 60), [])
        self.assertEqual(self.ids(NOW - 3600, NOW - 3600), ["al01", "s2"])           # an instant

    def test_event_shape(self):
        storm = self.h.events(NOW - DAY, kinds=["storm"])[0]
        self.assertEqual({k: storm[k] for k in ("module", "kind", "id", "lat", "lon", "layer", "severity")},
                         {"module": "hazards", "kind": "storm", "id": "al01", "lat": 22.0, "lon": -62.0,
                          "layer": "hazards.quakes", "severity": 7})
        self.assertEqual((storm["first_time"], storm["time"]), (int(NOW - 20 * 3600), int(NOW - 120)))
        self.assertEqual((storm["first_seen"], storm["last_seen"]), (int(NOW - 20 * 3600 + 1), int(NOW)))
        self.assertIsInstance(storm["severity"], int)
        news = self.h.events(NOW - DAY, kinds="news")[0]
        self.assertEqual(news["severity"], 6.5)

    def test_filters(self):
        self.assertEqual(sorted(self.ids(NOW - 2 * DAY, kinds="earthquake, fire")), ["f1", "q1", "q2"])
        self.assertEqual(sorted(self.ids(NOW - 2 * DAY, kinds={"ship"})), ["s1", "s2"])
        self.assertEqual(self.ids(NOW - 2 * DAY, kinds=[]), self.ids(NOW - 2 * DAY))  # empty = all
        self.assertEqual(sorted(self.ids(NOW - 2 * DAY, min_severity=6.5)), ["al01", "n1", "q2"])
        self.assertEqual(sorted(self.ids(NOW - 2 * DAY, bbox=(-10, 40, 10, 60))), ["n1", "q1"])
        self.assertEqual(sorted(self.ids(NOW - 2 * DAY, bbox="170,-5,-170,5")), ["s1", "s2"])  # antimeridian
        self.assertEqual(sorted(self.ids(NOW - 2 * DAY, bbox=[170, -5, -170, 5])), ["s1", "s2"])
        self.assertEqual(len(self.ids(NOW - 2 * DAY, limit=2)), 2)

    def test_bad_arguments_raise_value_error(self):
        bad = [dict(since="yesterday"), dict(since=float("nan")), dict(since=-5), dict(since=None),
               dict(since=NOW, until=NOW - 1), dict(since=NOW, kinds=5), dict(since=NOW, kinds=["x" * 41]),
               dict(since=NOW, kinds=[f"k{i}" for i in range(101)]), dict(since=NOW, bbox="1,2,3"),
               dict(since=NOW, bbox=(0, 0, 1, 99)), dict(since=NOW, min_severity="high"),
               dict(since=NOW, limit=0), dict(since=NOW, limit=2.5), dict(since=NOW, limit=True)]
        for kw in bad:
            with self.assertRaises(ValueError, msg=repr(kw)):
                self.h.events(**kw)
        # Milliseconds are accepted as a courtesy.
        self.assertEqual(self.ids((NOW - DAY) * 1000), self.ids(NOW - DAY))

    def test_summary(self):
        s = self.h.summary(NOW - DAY)
        self.assertEqual(s["total"], 6)
        self.assertEqual(s["max_severity"], 7)
        self.assertEqual(s["by_kind"]["ship"], {"count": 2, "max_severity": 6})
        self.assertEqual(list(s["by_kind"])[0], "ship")                        # largest first
        self.assertEqual(s["by_module"], {"hazards": {"count": 3, "max_severity": 7},
                                          "ships": {"count": 2, "max_severity": 6},
                                          "intel": {"count": 1, "max_severity": 6.5}})
        hours = {b["t"]: b["count"] for b in s["by_hour"]}
        self.assertEqual(sum(hours.values()), 6)
        self.assertEqual(hours[int((NOW - 600) // 3600 * 3600)], 2)            # the news and the storm
        self.assertEqual([b["t"] for b in s["by_hour"]], sorted(hours))
        # Clamped into the window: the storm (ongoing past "until") counts in the window's last hour.
        s2 = self.h.summary(NOW - 12 * 3600, NOW - 9 * 3600)
        self.assertEqual(s2["total"], 2)
        self.assertEqual([b["t"] for b in s2["by_hour"]], sorted({int((NOW - 10 * 3600) // 3600 * 3600),
                                                                  int((NOW - 9 * 3600) // 3600 * 3600)}))
        self.assertEqual(self.h.summary(NOW - DAY, bbox=(-10, 40, 10, 60))["total"], 2)
        empty = self.h.summary(NOW + 3600)
        self.assertEqual((empty["total"], empty["max_severity"], empty["by_kind"], empty["by_hour"]), (0, None, {}, []))
        with self.assertRaises(ValueError):
            self.h.summary(NOW, NOW - 10)

    def test_summary_matches_grouped_sql_and_uses_the_covering_index(self):
        # Reference: the old four-query answer, computed straight in SQL.
        clause, params = "time >= ?", [NOW - DAY]
        ref_kind = {r["kind"]: (r["n"], r["s"]) for r in self.store.query(
            f"SELECT kind, COUNT(*) n, MAX(severity) s FROM {H.TABLE} WHERE {clause} GROUP BY kind", params)}
        s = self.h.summary(NOW - DAY)
        self.assertEqual({k: (v["count"], v["max_severity"]) for k, v in s["by_kind"].items()}, ref_kind)
        plan = " ".join(str(tuple(r)) for r in self.store.conn().execute(
            f"EXPLAIN QUERY PLAN SELECT kind, module, time, severity FROM {H.TABLE} INDEXED BY {H.TABLE}_span "
            f"WHERE time >= ? AND lat BETWEEN ? AND ?", (NOW - DAY, -10, 10)).fetchall())
        self.assertIn("COVERING INDEX", plan)

    def test_summary_cache_is_dropped_by_the_next_snapshot(self):
        first = self.h.summary(NOW - DAY)
        first["total"] = -1                      # callers may mutate the answer (the route pops keys)
        self.assertEqual(self.h.summary(NOW - DAY)["total"], 6)
        self.feed.items = [ev(kind="fire", id="new1", t=NOW - 60, sev=2)]
        self.h.snapshot(now=NOW)
        self.assertEqual(self.h.summary(NOW - DAY)["total"], 7)


class HistoryHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MERIDIAN_DATA_DIR"] = cls.tmp.name
        cfg = Config(None)
        app = App(cfg, Store(Path(cls.tmp.name) / "t.db"), Net(cfg), LOG, Scheduler(LOG), DatasetManager(LOG))
        register_core(app)
        m = app.module("hazards")
        import time as _time
        now = _time.time()
        cls.now = now
        m.events(lambda since: [ev(id="q1", t=now - 3600, sev=6), ev(kind="fire", id="f1", t=now - 7200, sev=4,
                                                                      lat=-30, lon=150),
                                ev(id="q0", t=now - 3 * DAY, sev=8)])
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
        cls.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def test_events_endpoint(self):
        st, body = self.get("/api/core/history/events")                       # default: last 24 h
        self.assertEqual(st, 200)
        self.assertEqual([e["id"] for e in body["events"]], ["q1", "f1"])
        self.assertEqual((body["count"], body["truncated"], body["to"]), (2, False, None))
        e = body["events"][0]
        for key in ("module", "kind", "id", "title", "lat", "lon", "time", "severity", "first_time",
                    "first_seen", "last_seen", "layer"):
            self.assertIn(key, e)
        st, body = self.get(f"/api/core/history/events?from={int(self.now - 4 * DAY)}&kinds=earthquake&limit=1")
        self.assertEqual(([e["id"] for e in body["events"]], body["count"], body["truncated"]), (["q1"], 1, True))
        st, body = self.get(f"/api/core/history/events?from={int(self.now - 4 * DAY)}&min_severity=7")
        self.assertEqual([e["id"] for e in body["events"]], ["q0"])
        st, body = self.get(f"/api/core/history/events?from={int(self.now - DAY) * 1000}&bbox=140,-40,160,-20")
        self.assertEqual([e["id"] for e in body["events"]], ["f1"])
        st, body = self.get(f"/api/core/history/events?from={int(self.now - 4 * DAY)}&to={int(self.now - 2 * DAY)}")
        self.assertEqual([e["id"] for e in body["events"]], ["q0"])

    def test_summary_endpoint(self):
        st, body = self.get(f"/api/core/history/summary?from={int(self.now - 4 * DAY)}")
        self.assertEqual(st, 200)
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["by_kind"]["earthquake"]["count"], 2)
        self.assertEqual(body["by_module"]["hazards"]["max_severity"], 8)
        self.assertEqual(sum(b["count"] for b in body["by_hour"]), 3)
        self.assertIn("from", body)
        self.assertNotIn("since", body)

    def test_validation(self):
        for q in ("from=yesterday", "from=-1", "from=nan", f"from={self.now}&to={self.now - 10}",
                  "kinds=" + ",".join(f"k{i}" for i in range(101)), "kinds=" + "x" * 41, "bbox=1,2,3",
                  "min_severity=11", "limit=0", "limit=10001", "limit=abc"):
            st, body = self.get(f"/api/core/history/events?{q}")
            self.assertEqual((st, body["error"]["code"]), (400, "BAD_ARG"), q)
        st, body = self.get(f"/api/core/history/summary?from={self.now}&to={self.now - 10}")
        self.assertEqual(st, 400)

    def test_status_reports_history(self):
        st, body = self.get("/api/core/status")
        self.assertEqual(st, 200)
        self.assertEqual(body["history"]["rows"], 3)
        self.assertEqual(body["history"]["last_snapshot"]["new"], 3)
        self.assertFalse(body["history"]["scheduled"])          # only server.py schedules it

    def test_status_lists_the_retention_setting(self):
        st, body = self.get("/api/core/status")
        keys = {k["name"]: k for k in body["keys"]}
        self.assertIn("MERIDIAN_HISTORY_DAYS", keys)
        self.assertEqual((keys["MERIDIAN_HISTORY_DAYS"]["secret"], keys["MERIDIAN_HISTORY_DAYS"]["module"]),
                         (False, "core"))
        self.assertEqual(body["history"]["retention_days"], 30)


class SeenSinceTests(unittest.TestCase):
    """seen_since filters on last_seen (added 2026-11-23 for the alerts engine)."""

    def test_seen_since_keeps_only_recently_written_rows(self):
        import sqlite3, tempfile, time as _time
        from pathlib import Path
        from meridian.store import Store
        from meridian.history import EventHistory, TABLE
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            store = Store(Path(d) / "h.db")
            hist = EventHistory(store, None) if EventHistory.__init__.__code__.co_argcount >= 3 else EventHistory(store)
            now = _time.time()
            store.script(f"DELETE FROM {TABLE}")
            for i, seen in enumerate((now - 7200, now - 60)):
                store.execute(f"INSERT INTO {TABLE}(module, kind, id, title, subtitle, lat, lon, time, first_time, severity, "
                              f"layer, payload, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              ("m", "earthquake", f"e{i}", "t", "", 1.0, 2.0, now - 100, now - 100, 5, "", "{}", seen, seen))
            all_rows = hist.events(now - 86400)
            recent = hist.events(now - 86400, seen_since=now - 600)
            self.assertEqual(len(all_rows), 2)
            self.assertEqual([e["id"] for e in recent], ["e1"])
            with self.assertRaises(ValueError):
                hist.events(now - 86400, seen_since="soon")
            store.close()


class RetentionSettingTests(unittest.TestCase):
    """MERIDIAN_HISTORY_DAYS (added 2026-11-23 by the Operations team): 1 to 90 days, default 30."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.stores: list = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.tmp.cleanup()

    def make(self, value=None) -> App:
        cfg = Config(None, overrides={} if value is None else {"MERIDIAN_HISTORY_DAYS": value})
        store = Store(Path(self.tmp.name) / f"t{len(self.stores)}.db")
        self.stores.append(store)
        return App(cfg, store, Net(cfg), LOG, Scheduler(LOG), DatasetManager(LOG))

    @staticmethod
    def setting(app) -> dict:
        return [k for k in app.config.key_status() if k["name"] == "MERIDIAN_HISTORY_DAYS"][0]

    def test_default_is_30_days_and_declared_as_a_core_setting(self):
        app = self.make()
        self.assertEqual(app.history.info()["retention_days"], 30)
        meta = app.config.declared["MERIDIAN_HISTORY_DAYS"]
        self.assertEqual((meta["module"], meta["secret"]), ("core", False))
        self.assertIn("1 to 90", meta["description"])
        self.assertEqual((self.setting(app)["configured"], self.setting(app)["value"]), (False, None))

    def test_the_setting_moves_the_collect_window_and_pruning(self):
        app = self.make("45")
        h = app.history
        self.assertEqual(h.info()["retention_days"], 45)
        self.assertEqual((self.setting(app)["configured"], self.setting(app)["value"]), (True, "45"))
        feed = Feed()
        h.collect = feed
        feed.items = [ev(id="d50", t=NOW - 50 * DAY)]
        h.snapshot(now=NOW - 50 * DAY)
        feed.items = [ev(id="d40", t=NOW - 40 * DAY)]
        h.snapshot(now=NOW - 40 * DAY)
        self.assertEqual(feed.calls[-1], NOW - 40 * DAY - 45 * DAY)
        r = h.prune(now=NOW)                                     # 30 days would drop both
        self.assertEqual(r["deleted"], 1)
        self.assertEqual([e["id"] for e in h.events(NOW - 60 * DAY)], ["d40"])

    def test_values_are_held_between_1_and_90_days(self):
        with self.assertLogs("meridian.history", "WARNING") as logs:
            for value, days in [("0", 1), ("0.5", 1), ("-3", 1), ("1", 1), ("7.5", 7.5), ("90", 90),
                                ("365", 90), ("1000", 90), ("thirty", 30), ("nan", 30), ("inf", 30)]:
                self.assertEqual(self.make(value).history.info()["retention_days"], days, value)
        self.assertEqual(len(logs.records), 8)                  # every adjusted or unreadable value warns

    def test_explicit_config_and_retention_arguments(self):
        store = Store(Path(self.tmp.name) / "x.db")
        self.stores.append(store)
        cfg = Config(None, overrides={"MERIDIAN_HISTORY_DAYS": "60"})
        self.assertEqual(EventHistory(store, Feed(), log=LOG, config=cfg).info()["retention_days"], 60)
        self.assertIn("MERIDIAN_HISTORY_DAYS", cfg.declared)
        self.assertEqual(EventHistory(store, Feed(), log=LOG, config=cfg, retention_days=3).info()["retention_days"], 3)
        self.assertEqual(EventHistory(store, Feed(), log=LOG).info()["retention_days"], 30)   # no config at all
        self.assertEqual(H.retention_days_setting(None), 30)


if __name__ == "__main__":
    unittest.main()
