"""Integration tests for the Intelligence+ additions: the intel module on a temporary app with a network stub
that serves fixtures. Covers Cloudflare Radar polling and shapes, the travel-advice poller and change events,
protests from GDELT rows, the news-spike detector on synthetic tallies, and the bounded history seed."""

from __future__ import annotations

import copy
import io
import json
import logging
import math
import shutil
import tempfile
import time
import unittest
import urllib.parse
import zipfile
from pathlib import Path

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.net import NetError
from meridian.registry import App, HTTPError, Request
from meridian.scheduler import Scheduler
from meridian.store import Store
from meridian.modules import intel
from meridian.modules.intel import advice, db, gdelt, radar, spikes
from meridian.modules.intel.seed import HistorySeed

from tests.test_intel_plus import row

FIX = Path(__file__).parent / "fixtures" / "intel"
TOKEN = "radar-test-token-0123456789"
# iso2, iso3, name, label lon, label lat
PLACES = [("SD", "SDN", "Sudan", 30.0, 15.0), ("TD", "TCD", "Chad", 18.6, 15.3), ("DJ", "DJI", "Djibouti", 42.6, 11.7),
          ("FR", "FRA", "France", 2.4, 46.6), ("UA", "UKR", "Ukraine", 31.2, 49.0), ("YE", "YEM", "Yemen", 47.6, 15.9),
          ("BH", "BHR", "Bahrain", 50.6, 26.1), ("MX", "MEX", "Mexico", -102.0, 23.9), ("IR", "IRN", "Iran", 53.7, 32.4),
          ("TO", "TON", "Tonga", -175.2, -21.2), ("GA", "GAB", "Gabon", 11.8, -0.6), ("CG", "COG", "Congo", 15.9, -0.8),
          ("CM", "CMR", "Cameroon", 12.5, 4.6), ("GF", "GUF", "French Guiana", -53.2, 3.9), ("CH", "CHE", "Switzerland", 8.2, 46.8),
          ("PK", "PAK", "Pakistan", 69.0, 29.9), ("MY", "MYS", "Malaysia", 102.0, 4.0), ("KE", "KEN", "Kenya", 37.9, 0.5),
          ("EE", "EST", "Estonia", 25.5, 58.7), ("US", "USA", "United States", -98.0, 39.0), ("GB", "GBR", "United Kingdom", -2.0, 53.0),
          ("NG", "NGA", "Nigeria", 8.0, 9.6), ("JP", "JPN", "Japan", 138.0, 36.5), ("IN", "IND", "India", 79.0, 22.0),
          ("TR", "TUR", "Turkey", 35.0, 39.0), ("PH", "PHL", "Philippines", 122.0, 12.0), ("ID", "IDN", "Indonesia", 113.0, -1.0),
          ("CO", "COL", "Colombia", -73.0, 4.0), ("BR", "BRA", "Brazil", -51.0, -10.0)]


def fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FixtureNet:
    """Serves JSON payloads by URL prefix; everything else fails like an unreachable host."""

    def __init__(self):
        self.routes: list[tuple[str, object]] = []
        self.calls: list[tuple[str, dict, dict]] = []
        self.bytes_routes: dict[str, bytes] = {}

    def set_host_interval(self, host, seconds):
        pass

    def get_json(self, url, params=None, headers=None, **kw):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        for prefix, payload in self.routes:
            if url == prefix or url.startswith(prefix):
                return payload(url) if callable(payload) else copy.deepcopy(payload)
        raise NetError("offline in tests", url=url)

    def get_bytes(self, url, **kw):
        self.calls.append((url, {}, {}))
        if url in self.bytes_routes:
            data = self.bytes_routes[url]
            if data is None:
                raise NetError("not found", status=404, url=url)
            return data
        raise NetError("offline in tests", url=url)

    def _fail(self, url, **kw):
        raise NetError("offline in tests", url=url)

    get_text = request = download = _fail


def make_app(tmp: Path, token: str | None = TOKEN):
    overrides = {"MERIDIAN_DATA_DIR": str(tmp)}
    if token:
        overrides["CLOUDFLARE_RADAR_TOKEN"] = token
    config = Config(env_file=None, overrides=overrides)
    store = Store(tmp / "test.db")
    log = logging.getLogger("meridian.test")
    net = FixtureNet()
    app = App(config, store, net, log, Scheduler(log), DatasetManager(log))
    m = app.module("intel")
    intel.register(m)
    app.modules["intel"].ok = True
    svc = m.intel
    build = m.data_dir
    build.mkdir(parents=True, exist_ok=True)
    feats = [{"type": "Feature", "properties": {"iso2": i2, "name": n, "short": n, "iso3": i3, "continent": "", "subregion": "",
                                                "pop_est": 1e7, "pop_year": 2020, "label": [lon, lat], "note": ""},
              "geometry": {"type": "Polygon", "coordinates": [[[lon - 1, lat - 1], [lon + 1, lat - 1], [lon + 1, lat + 1],
                                                               [lon - 1, lat + 1], [lon - 1, lat - 1]]]}}
             for i2, i3, n, lon, lat in PLACES]
    (build / "countries.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    wb = {"built_at": time.time(), "failures": [], "indicators": {},
          "economies": {i2: {"iso2": i2, "iso3": i3, "name": n, "capital": "", "lat": lat, "lon": lon, "region": "", "income": "",
                             "ind": {}} for i2, i3, n, lon, lat in PLACES}}
    (build / "worldbank.json").write_text(json.dumps(wb), encoding="utf-8")
    svc.load_countries()
    svc.load_worldbank()
    return app, m, svc, store, net


def call(app, path: str):
    split = urllib.parse.urlsplit(path)
    route, params, _ = app.match("GET", urllib.parse.unquote(split.path))
    if route is None:
        raise AssertionError(f"no route for {path}")
    return route.handler(Request("GET", split.path, params, urllib.parse.parse_qs(split.query), {}))


def export_text(rows: list[list[str]]) -> str:
    return "\n".join("\t".join(r) for r in rows) + "\n"


def now_ts() -> str:
    return gdelt.epoch_to_ts(gdelt.slot_floor(time.time()))


# =============================================================================================
class RadarAndAdviceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="intelplus-test-"))
        cls.app, cls.m, cls.svc, cls.store, cls.net = make_app(cls.tmp)
        net = cls.net
        net.routes += [(radar.ANOMALIES_URL, fixture("radar_anomalies.json")), (radar.ANNOTATIONS_URL, fixture("radar_annotations.json")),
                       (radar.HIJACKS_URL, fixture("radar_hijacks.json")), (radar.LEAKS_URL, fixture("radar_leaks.json"))]
        cls.svc.advice.min_fcdo = 1
        cls.svc.advice.min_us = 1

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- Cloudflare Radar ------------------------------------------------------------------------
    def test_radar(self):
        svc = self.svc
        svc.poll_radar()
        snap = svc.radar.snapshot()
        self.assertEqual(len(snap["anomalies"]), 8)
        self.assertGreaterEqual(len(snap["hijacks"]), 3)
        self.assertEqual(len(snap["leaks"]), 3)
        radar_calls = [c for c in self.net.calls if c[0].startswith(radar.API)]
        self.assertEqual(len(radar_calls), 4)
        self.assertTrue(all(c[2].get("Authorization") == f"Bearer {TOKEN}" for c in radar_calls))
        self.assertTrue(all(TOKEN not in json.dumps(c[1]) for c in radar_calls))          # never in the query string
        self.assertIn("CH", {o["iso2"] for o in svc.outages["cloudflare"]})               # AS-located outage report
        self.assertEqual(self.app.sources["intel.cloudflare_radar"]["ok"], True)
        self.assertEqual(self.app.sources["intel.cloudflare_bgp"]["ok"], True)

        fc = call(self.app, "/api/intel/cyber")
        roles = {f["properties"]["role"] for f in fc["features"]}
        self.assertTrue({"outage", "leak"} <= roles)
        self.assertTrue({"bgp", "bgp-local"} & roles)
        arcs = [f for f in fc["features"] if f["properties"]["role"] == "bgp"]
        for f in arcs:
            self.assertEqual(f["geometry"]["type"], "LineString")
            self.assertGreater(len(f["geometry"]["coordinates"]), 10)
            self.assertLessEqual(f["properties"]["n10"], f["properties"]["n"])
        self.assertTrue(fc["meta"]["cloudflare_token"])

        out = call(self.app, "/api/intel/radar")
        self.assertEqual(out["counts"]["anomalies"], 8)
        gabon = next(a for a in out["anomalies"] if a["iso2"] == "GA")
        detail = call(self.app, f"/api/intel/radar/anomaly/{gabon['id']}")
        self.assertEqual(detail["title"], "Internet traffic anomaly in Gabon")
        self.assertEqual(detail["status_words"], "Verified by Radar and a third-party source")
        gf = next(a for a in out["anomalies"] if a["iso2"] == "GF")
        self.assertIn("power", call(self.app, f"/api/intel/radar/anomaly/{gf['id']}")["annotation"]["cause"])
        with self.assertRaises(HTTPError) as err:
            call(self.app, "/api/intel/radar/anomaly/00000000-0000-0000-0000-000000000000")
        self.assertEqual(err.exception.status, 404)
        with self.assertRaises(HTTPError) as err:
            call(self.app, "/api/intel/radar/anomaly/not!an-id")
        self.assertEqual(err.exception.status, 400)
        hj = call(self.app, "/api/intel/radar/bgp/hijack/174249")
        self.assertEqual(hj["confidence"], 12)
        pair = call(self.app, "/api/intel/radar/bgp/pair/MY-MY")
        self.assertTrue(any(h["id"] == 174249 for h in pair["hijacks"]))
        leak = call(self.app, f"/api/intel/radar/bgp/leak/{snap['leaks'][0]['id']}")
        self.assertEqual(leak["country"], "Estonia")
        with self.assertRaises(HTTPError):
            call(self.app, "/api/intel/radar/bgp/worm/1")
        country = call(self.app, "/api/intel/radar?country=MY")
        self.assertTrue(country["hijacks"])

        events = svc.radar_events(time.time() - 30 * 86400)
        outages = [e for e in events if e["kind"] == "outage"]
        self.assertEqual(len(outages), 8)
        self.assertTrue(all(e["id"].startswith("cfr:") and e["layer"] == "intel.cyber" for e in outages))
        self.assertTrue(all(e["severity"] <= 7 for e in events))
        cyber = [e for e in events if e["kind"] == "cyber"]
        self.assertTrue(all(e["id"].startswith("cfbgp:") for e in cyber))
        for h in snap["hijacks"]:
            want = h["confidence"] >= 10 and h["ongoing"]
            self.assertEqual(any(e["id"] == f"cfbgp:{h['id']}" for e in cyber), want)

        tool = self.app.call_ai_tool("internet_anomalies", {"country": "Gabon"})
        self.assertEqual(len(tool["traffic_anomalies_7d"]), 1)
        self.assertIn("licence", tool)
        self.assertIn("error", self.app.call_ai_tool("internet_anomalies", {"country": "Narnia"}))
        found = self.app.run_search("AS64498", 10)
        self.assertTrue(any(e.get("id") == "cfbgp:174249" for e in found))
        prof = call(self.app, "/api/intel/country/MY")
        self.assertTrue(prof["internet"]["token"] and prof["internet"]["hijacks"])
        # the token never leaves the server
        blob = json.dumps([out, fc, prof, tool, call(self.app, "/api/intel/status"), self.app.sources], default=str)
        self.assertNotIn(TOKEN, blob)

    def test_radar_without_token(self):
        tmp = Path(tempfile.mkdtemp(prefix="intelplus-notoken-"))
        try:
            app, m, svc, store, net = make_app(tmp, token=None)
            svc.poll_radar()
            self.assertEqual(net.calls, [])
            self.assertIsNone(app.sources["intel.cloudflare_radar"].get("ok"))           # status bar: "Needs ..."
            self.assertEqual(app.sources["intel.cloudflare_radar"]["needs_key"], "CLOUDFLARE_RADAR_TOKEN")
            self.assertFalse(call(app, "/api/intel/cyber")["meta"]["cloudflare_token"])
            self.assertFalse(call(app, "/api/intel/country/FR")["internet"]["token"])
            self.assertIn("note", app.call_ai_tool("internet_anomalies", {}))
            store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- travel advice -----------------------------------------------------------------------------
    def test_advice(self):
        svc, net = self.svc, self.net
        index = fixture("fcdo_index.json")
        pages = fixture("fcdo_pages.json")
        net.routes.insert(0, (advice.FCDO_INDEX_URL + "?", None))          # placeholder never matched exactly
        net.routes.append((advice.FCDO_INDEX_URL, lambda url: copy.deepcopy(index) if url == advice.FCDO_INDEX_URL
                           else copy.deepcopy(pages[url.rsplit("/", 1)[-1]])))
        us = fixture("us_advisories.json")
        net.routes.append((advice.US_URL, lambda url: copy.deepcopy(us)))
        svc.advice.poll_fcdo()
        first_pages = [c for c in net.calls if c[0].startswith(advice.FCDO_API)]
        self.assertEqual(len(first_pages), 5)
        summ = call(self.app, "/api/intel/advice")
        self.assertEqual({k: v["l"] for k, v in summ["fcdo"].items()}, {"SD": 4, "TD": 3, "DJ": 2, "FR": 1, "UA": 4})
        self.assertEqual(summ["fcdo"]["SD"]["b"], "text")
        self.assertEqual(db.query_advice_changes(self.store, since=0), [])             # first sight: no changes
        self.assertTrue(self.app.sources["intel.fcdo"]["ok"])

        # a second poll fetches only pages whose time moved; Chad's advice is lifted
        index2 = copy.deepcopy(index)
        changed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        for ch in index2["links"]["children"]:
            if ch["details"]["country"]["slug"] == "chad":
                ch["public_updated_at"] = changed
        pages["chad"]["public_updated_at"] = changed
        pages["chad"]["details"]["alert_status"] = []
        pages["chad"]["details"]["parts"][0]["body"] = "<p>Check our advice before you travel.</p>"
        index.clear()
        index.update(index2)
        before = len([c for c in net.calls if c[0].startswith(advice.FCDO_API)])
        svc.advice.poll_fcdo()
        after = len([c for c in net.calls if c[0].startswith(advice.FCDO_API)])
        self.assertEqual(after - before, 1)
        changes = db.query_advice_changes(self.store, since=0)
        self.assertEqual(len(changes), 1)
        ch = changes[0]
        self.assertEqual((ch["iso2"], ch["kind"], ch["old_level"], ch["new_level"]), ("TD", "level", 3, 1))
        self.assertEqual(ch["text"], "FCDO no longer advises against travel to Chad "
                                     "(previously against all but essential travel, and all travel to parts)")
        self.assertEqual(ch["id"], f"fcdo:TD:{int(advice.parse_time(changed))}")

        # a newer parser that reads the same FCDO codes differently is not an FCDO change
        n_before = len(db.query_advice_changes(self.store, since=0))
        svc.advice.fcdo["DJ"] = dict(svc.advice.fcdo["DJ"], pv=1, level=4, label="Against all travel")
        self.assertEqual(svc.advice.apply_fcdo(advice.parse_fcdo_country(copy.deepcopy(pages["djibouti"]), "djibouti")), [])
        self.assertEqual(len(db.query_advice_changes(self.store, since=0)), n_before)
        self.assertEqual(svc.advice.fcdo["DJ"]["pv"], advice.PARSER_VERSION)

        # FCDO notes about changed areas seed change events the first time a page is read
        recent = time.time() - 2 * 86400
        page = advice.parse_fcdo_country(copy.deepcopy(pages["france"]), "france")
        page["history"] = [{"t": recent, "note": "FCDO now advises against all travel to the island of Mayotte."},
                           {"t": recent - 60 * 86400, "note": "FCDO now advises against all travel to the old area."}]
        svc.advice.fcdo.pop("FR")
        seeded = svc.advice.apply_fcdo(page)
        self.assertEqual([c["kind"] for c in seeded], ["areas"])                       # the 60-day-old note is left out
        self.assertIn("Mayotte", seeded[0]["text"])

        # US list: ISO3 from the link, FIPS fallback, duplicates resolved; a level change is detected
        svc.advice.poll_us()
        u = call(self.app, "/api/intel/advice")["us"]
        self.assertEqual((u["YE"]["l"], u["BH"]["l"], u["TO"]["l"], u["MX"]["l"]), (4, 3, 1, 2))
        for it in us:
            if it["Title"].startswith("Yemen"):
                it["Title"] = "Yemen - Level 3: Reconsider Travel"
                it["Updated"] = time.strftime("%Y-%m-%dT%H:%M:%S-04:00", time.gmtime(time.time() - 7200))
        svc.advice.poll_us()
        uch = [c for c in db.query_advice_changes(self.store, since=0) if c["source"] == "us"]
        self.assertEqual(len(uch), 1)
        self.assertEqual(uch[0]["text"], "US State Department lowers Yemen to Level 3: reconsider travel (previously Level 4)")

        # routes, events, the AI tool and the country profile
        det = call(self.app, "/api/intel/advice/SD")
        self.assertEqual((det["fcdo"]["level"], det["us"]["level"]), (4, 4))
        self.assertTrue(det["fcdo"]["sentences"])
        lst = call(self.app, "/api/intel/advice/changes?days=30")
        self.assertGreaterEqual(lst["count"], 3)
        one = call(self.app, f"/api/intel/advice/change/{ch['id']}")
        self.assertEqual(one["entity"]["kind"], "advice_change")
        with self.assertRaises(HTTPError) as err:
            call(self.app, "/api/intel/advice/change/fcdo:ZZ:1")
        self.assertEqual(err.exception.status, 404)
        with self.assertRaises(HTTPError) as err:
            call(self.app, "/api/intel/advice/change/drop-table")
        self.assertEqual(err.exception.status, 400)
        evs = [e for e in self.app.collect_events(time.time() - 30 * 86400) if e["kind"] == "advice_change"]
        self.assertTrue(any(e["id"] == uch[0]["id"] and e["severity"] == 3.0 for e in evs))       # lowered: 3
        self.assertTrue(all(e["layer"] == "intel.advice" and e.get("lat") is not None for e in evs))
        tool = self.app.call_ai_tool("travel_advice", {"country": "Sudan"})
        self.assertEqual((tool["uk_fcdo"]["level"], tool["us_state_department"]["level"]), (4, 4))
        self.assertEqual(tool["uk_fcdo"]["level_from"], "advice text")
        overview = self.app.call_ai_tool("travel_advice", {})
        self.assertIn("Sudan", overview["uk_fcdo_level_4"])
        self.assertIn("error", self.app.call_ai_tool("travel_advice", {"country": "Atlantis"}))
        prof = call(self.app, "/api/intel/country/SD")
        self.assertEqual(prof["travel_advice"]["fcdo"]["level"], 4)
        # the stored advice survives a restart
        reloaded = advice_service_reload(self.m, self.svc)
        self.assertEqual(reloaded.fcdo["SD"]["level"], 4)


def advice_service_reload(m, svc):
    from meridian.modules.intel.advice_service import TravelAdvice
    t = TravelAdvice(m, svc.resolve_country, svc.iso3_to_iso2, lambda f: None)
    t.load()
    return t


# =============================================================================================
class ProtestsAndSeedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="intelplus-protests-"))
        cls.app, cls.m, cls.svc, cls.store, cls.net = make_app(cls.tmp, token=None)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rows(self, ts: str):
        day = ts[:8]
        return [row(id="1", added=ts, day=day), row(id="2", added=ts, day=day, mentions="5", sources="1", articles="2"),
                row(id="3", added=ts, day=day, mentions="4", sources="1", articles="1"),
                row(id="4", added=ts, day=day, code="145", mentions="80", sources="9", articles="40", gname="Paris, Ile-de-France, France",
                    gcc="FR", glat="48.8667", glon="2.3333", feature="-1456928", a1code="FRA", a1name="FRANCE", a2code="FRA", a2name="FRANCE"),
                row(id="5", added=ts, day=day, sources="1", articles="1", gname="Lagos, Lagos, Nigeria", gcc="NI", glat="6.45",
                    glon="3.3958", feature="-2332459"),
                row(id="6", added=ts, day=day, root="19", code="190", quad="4", a1type="MIL")]

    def test_protests(self):
        ts = now_ts()
        stats = self.svc.ingest_text(export_text(self.rows(ts)))
        self.assertEqual(stats["protests"], 5)
        self.assertEqual(db.count_events(self.store, table="intel_protests"), 3)       # Nairobi reports merged
        nairobi = next(e for e in db.query_events(self.store, table="intel_protests") if e["iso2"] == "KE")
        self.assertEqual((nairobi["reports"], nairobi["sources"]), (3, 4))
        fc = call(self.app, "/api/intel/protests?days=1")
        places = {f["properties"]["n"] for f in fc["features"]}
        self.assertEqual(places, {"Nairobi", "Paris"})                                  # Lagos is single-source
        self.assertEqual(len(call(self.app, "/api/intel/protests?days=1&single=1")["features"]), 3)
        paris = next(f for f in fc["features"] if f["properties"]["n"] == "Paris")
        self.assertEqual(paris["properties"]["c"], "r")
        detail = call(self.app, f"/api/intel/protests/{paris['properties']['id']}")
        self.assertEqual((detail["category"], detail["country"]), ("riot", "France"))
        self.assertEqual(detail["note"], "Machine-coded from news reports, not verified.")
        with self.assertRaises(HTTPError) as err:
            call(self.app, "/api/intel/protests/gp0000000000000a")
        self.assertEqual(err.exception.status, 404)
        with self.assertRaises(HTTPError) as err:
            call(self.app, "/api/intel/protests/gd0000000000000a")
        self.assertEqual(err.exception.status, 400)
        # conflict rows still go to the conflict table only
        self.assertEqual(db.count_events(self.store), 1)
        # hourly and daily protest tallies
        hour = gdelt.hour_of_row(self.rows(ts)[0])
        ke = self.store.one("SELECT * FROM intel_country_hour WHERE iso2 = 'KE' AND hour = ?", (hour,))
        self.assertEqual((ke["rows"], ke["protest_rows"]), (4, 3))                        # the conflict row is in Kenya too
        self.assertEqual(self.store.scalar("SELECT protest_rows FROM intel_country_day WHERE iso2 = 'FR'"), 1)
        # providers and tools
        self.svc.derive_plus(time.time())
        self.assertTrue(any(e["layer"] == "intel.protests" for e in self.app.run_search("nairobi", 10)))
        near = self.app.run_nearby(-1.28, 36.82, 20, 10)
        self.assertTrue(any(e["kind"] == "protest" for e in near))
        tool = self.app.call_ai_tool("protests", {"country": "Kenya", "days": 1, "min_severity": 0})
        self.assertEqual(tool["count"], 1)
        self.assertIn("not verified", tool["caveat"])
        riots = self.app.call_ai_tool("protests", {"kind": "riot", "days": 1, "min_severity": 0})
        self.assertEqual([p["country"] for p in riots["protests"]], ["FR"])
        self.assertIn("error", self.app.call_ai_tool("protests", {"kind": "parade"}))
        events = self.svc.protest_events(time.time() - 86400)
        self.assertEqual([e["title"] for e in events], ["Riot or violent protest in Paris"])
        prof = call(self.app, "/api/intel/country/KE")
        self.assertEqual(prof["protests"]["events_7d"], 1)

    def test_seed_plan_and_modes(self):
        latest = now_ts()
        t0 = gdelt.ts_to_epoch(latest)
        slot = lambda h: gdelt.epoch_to_ts(t0 - h * 3600)
        # slot(1) has no record and sits inside the normal window: the GDELT loop owns it
        db.mark_file(self.store, slot(2), "ok", tallies=1)              # ingested before protests were counted: retally
        db.mark_file(self.store, slot(30), "ok", tallies=1)             # the same outside the window
        db.mark_file(self.store, slot(31), "missing")                   # GDELT has no such file
        db.mark_file(self.store, slot(32), "ok")                        # already complete
        seed = HistorySeed(self.svc, days=2, max_mb=10)
        plan = dict(seed.plan(latest, backfill_hours=24))
        self.assertNotIn(slot(1), plan)
        self.assertEqual(plan[slot(2)], "retally")
        self.assertEqual(plan[slot(30)], "retally")
        self.assertNotIn(slot(31), plan)
        self.assertNotIn(slot(32), plan)
        self.assertEqual(plan[slot(33)], "full")                         # a gap inside the event window
        self.assertTrue(all(ts < slot(24) or mode == "retally" for ts, mode in plan.items()))
        old = HistorySeed(self.svc, days=20, max_mb=10).plan(latest, backfill_hours=24)
        self.assertEqual(dict(old)[slot(20 * 24 - 1)], "tallies")         # older than the event window

        # retally: protest events, protest counts and hourly tallies only
        ts = slot(30)
        before = self.store.scalar("SELECT COALESCE(SUM(rows), 0) FROM intel_country_day")
        self.svc.ingest_text(export_text(self.rows(ts)), mode="retally", ts=ts)
        self.assertEqual(self.store.scalar("SELECT COALESCE(SUM(rows), 0) FROM intel_country_day"), before)
        self.assertEqual(self.store.one("SELECT tallies FROM intel_files WHERE ts = ?", (ts,))["tallies"], db.TALLIES_VERSION)
        self.assertGreater(self.store.scalar("SELECT COUNT(*) FROM intel_country_hour WHERE hour = ?",
                                             (gdelt.hour_of_row(self.rows(ts)[0]),)), 0)
        # tallies-only: daily rows, file marked 'seed' (not counted as coverage by the instability index)
        ts2 = slot(20 * 24 - 1)
        cov = db.coverage_days(self.store, 30)
        self.svc.ingest_text(export_text(self.rows(ts2)), mode="tallies", ts=ts2, nbytes=123)
        self.assertEqual(self.store.one("SELECT status FROM intel_files WHERE ts = ?", (ts2,))["status"], "seed")
        self.assertEqual(db.coverage_days(self.store, 30), cov)
        self.assertEqual(self.store.scalar("SELECT rows FROM intel_country_day WHERE iso2 = 'FR' AND day = ?", (int(ts2[:8]),)), 1)
        with self.assertRaises(ValueError):
            self.svc.ingest_text(export_text(self.rows(ts2)), mode="tallies")

        # seed_file: a zip is ingested; a 404 marks the slot missing
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{slot(34)}.export.CSV", export_text(self.rows(slot(34))))
        self.net.bytes_routes[gdelt.export_url(slot(34))] = buf.getvalue()
        self.net.bytes_routes[gdelt.export_url(slot(35))] = None
        self.assertGreater(seed.seed_file(slot(34), "full"), 0)
        self.assertEqual(self.store.one("SELECT status, tallies FROM intel_files WHERE ts = ?", (slot(34),)),
                         {"status": "ok", "tallies": db.TALLIES_VERSION})
        self.assertEqual(seed.seed_file(slot(35), "full"), 0)
        self.assertEqual(self.store.one("SELECT status FROM intel_files WHERE ts = ?", (slot(35),))["status"], "missing")


class FakeStop:
    """A stop event that never sleeps: timed waits are recorded and return at once; an untimed wait (the idle
    wait after the seed is done) ends the run."""

    def __init__(self):
        self.waits: list[float] = []

    def is_set(self):
        return False

    def wait(self, timeout=None):
        if timeout is None:
            return True
        self.waits.append(timeout)
        return False


class SeedOutageTest(unittest.TestCase):
    """A network outage during the history seed backs off and is retried, never recorded as a finished seed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="intelplus-seed-"))
        self.app, self.m, self.svc, self.store, self.net = make_app(self.tmp, token=None)
        self.latest = now_ts()
        self.svc.ingest["latest"] = self.latest
        self.svc.backfill_hours = 24.0

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed_calls(self):
        return [u for u, _, _ in self.net.calls if "gdeltproject" in u]

    def test_outage_backs_off_and_is_not_finished_for_good(self):
        # every download fails like an unreachable host (FixtureNet's default)
        seed = HistorySeed(self.svc, days=2, max_mb=10)
        planned = len(seed.plan(self.latest, 24.0))
        self.assertGreater(planned, 10)
        stop = FakeStop()
        seed.run(stop)
        calls = len(self.seed_calls())
        self.assertEqual(calls, planned * 3)                                    # three passes over the failed slots
        backoffs = [w for w in stop.waits if w >= 300 and w != 1800]
        self.assertEqual(backoffs[:6], [300, 600, 1200, 2400, 3600, 3600])     # grows, then holds at an hour
        self.assertEqual(stop.waits.count(1800), 2)                             # the gap before passes 2 and 3
        self.assertNotIn(1.2, stop.waits[5:planned + 1])                            # no 1.2 s race once failing
        prog = self.store.kv_get("intel.seed")
        self.assertEqual((prog["files"], prog["bytes"], prog["errors"]), (0, 0, planned))
        note = self.app.sources["intel.gdelt_seed"]["note"]
        self.assertIn(f"{planned:,} could not be downloaded", note)

        # the next start sees the failures and tries again instead of idling
        self.net.calls.clear()
        for ts, _ in seed.plan(self.latest, 24.0):
            self.net.bytes_routes[gdelt.export_url(ts)] = None                   # GDELT answers again (404s here)
        again = HistorySeed(self.svc, days=2, max_mb=10)
        stop2 = FakeStop()
        again.run(stop2)
        self.assertEqual(len(self.seed_calls()), planned)
        self.assertTrue(all(w <= 30 for w in stop2.waits))                      # no backoff once the network is back
        prog = self.store.kv_get("intel.seed")
        self.assertEqual((prog["finished"], prog["errors"], prog["files"]), (True, 0, planned))
        self.assertEqual(again.error_count(self.latest, 24.0), 0)
        self.assertNotIn("could not be downloaded", self.app.sources["intel.gdelt_seed"]["note"])

        # and a seed with nothing failed stays idle at the next start
        self.net.calls.clear()
        HistorySeed(self.svc, days=2, max_mb=10).run(FakeStop())
        self.assertEqual(self.seed_calls(), [])

    def test_legacy_finished_record_with_failed_slots_is_retried(self):
        # a record written before failures were counted: finished, no 'errors' key, but slots left in 'error'
        seed = HistorySeed(self.svc, days=2, max_mb=10)
        todo = seed.plan(self.latest, 24.0)
        for ts, _ in todo:
            db.mark_file(self.store, ts, "missing")
        db.mark_file(self.store, todo[0][0], "error")
        db.mark_file(self.store, todo[1][0], "error")
        self.store.kv_set("intel.seed", {"bytes": 0, "files": len(todo), "days": 2, "finished": True})
        stop = FakeStop()
        HistorySeed(self.svc, days=2, max_mb=10).run(stop)
        self.assertEqual(sorted(self.seed_calls()), sorted([gdelt.export_url(todo[0][0]), gdelt.export_url(todo[1][0])] * 3))
        self.assertEqual(self.store.kv_get("intel.seed")["errors"], 2)

    def test_short_blip_does_not_back_off(self):
        # four failures in a row, then the network answers: no backoff, and one more pass picks the four up
        seed = HistorySeed(self.svc, days=2, max_mb=10)
        todo = seed.plan(self.latest, 24.0)
        for ts, _ in todo[4:]:
            self.net.bytes_routes[gdelt.export_url(ts)] = None                   # 404: GDELT answered
        stop = FakeStop()
        seed.run(stop)
        self.assertFalse([w for w in stop.waits if w >= 300 and w != 1800])
        self.assertEqual(self.store.kv_get("intel.seed")["errors"], 4)


# =============================================================================================
class SpikeDetectorTest(unittest.TestCase):
    """Synthetic tallies: 20 complete days of steady counts, then a conflict spike in country AA."""
    NAMES = {"AA": "Alphaland", "BB": "Betaland", "CC": "Gammaland", "ZZ": "Filler"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="intelplus-spikes-"))
        self.store = Store(self.tmp / "spikes.db")
        db.create(self.store)
        self.now = math.floor(time.time() / 3600) * 3600 + 1200
        self.det = spikes.SpikeDetector(self.store, logging.getLogger("meridian.test"), lambda c: self.NAMES.get(c, c),
                                        lambda c: [10.0, 20.0])

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fill(self, days: int = 20, spike: float = 1.0, volume: float = 1.0, files_window: bool = True, story: int = 0):
        """Daily tallies for ``days`` complete days before the windows; hourly tallies for the last 26 hours."""
        hour = math.floor(self.now / 3600) * 3600
        base_end = math.floor((hour - 3600 - 86400) / 86400) * 86400        # baseline days end here
        daily = {"AA": (400, 40, 4), "BB": (60, 3, 1), "CC": (900, 12, 6), "ZZ": (20000, 900, 120)}
        files, tallies = [], []
        for k in range(days):
            d0 = base_end - (k + 1) * 86400
            day = gdelt.epoch_to_day(d0)
            for s in range(96):
                files.append((gdelt.epoch_to_ts(d0 + s * 900), "seed", db.TALLIES_VERSION))
            for iso, (n, c, p) in daily.items():
                tallies.append((iso, day, n, c, 0.0, 1.0, p))
        hours = []
        for hh in range(26):
            h0 = hour - (hh + 1) * 3600
            if files_window:
                for s in range(4):
                    files.append((gdelt.epoch_to_ts(h0 + s * 900), "ok", db.TALLIES_VERSION))
            for iso, (n, c, p) in daily.items():
                cc = c * (spike if iso == "AA" else 1.0)
                hours.append((iso, int(h0), int(n * volume / 24), int(round(cc * volume / 24)), int(p / 24 * volume), 0.0, 1.0))
        with self.store.tx() as c:
            c.executemany("INSERT OR REPLACE INTO intel_files(ts, fetched_at, status, tallies) VALUES(?, 0, ?, ?)", files)
            c.executemany("INSERT INTO intel_country_day(iso2, day, rows, conflict_rows, tone_sum, tone_w, protest_rows) "
                          "VALUES(?,?,?,?,?,?,?)", tallies)
            c.executemany("INSERT INTO intel_country_hour(iso2, hour, rows, conflict_rows, protest_rows, tone_sum, tone_w) "
                          "VALUES(?,?,?,?,?,?,?)", hours)
        if story:
            ev = {"id": "gd00000000000001", "dkey": "19:1.0:2.0:1", "day": 1, "t_first": self.now - 3600, "t_last": self.now - 600,
                  "root": "19", "code": "190", "quad": 4, "goldstein": -10.0, "category": "fighting", "armed": True, "crime": False,
                  "precision": "city", "lat": 1.0, "lon": 2.0, "place": "Alpha City, Alphaland", "fips": "AA", "iso2": "AA",
                  "adm1": "", "actor1": None, "actor2": None, "actors": [], "mentions": 10, "sources": 2, "articles": 10,
                  "tone": -5.0, "reports": story, "urls": [], "gids": [], "severity": 6.0, "focus": 1.0, "url_main": None,
                  "main_mentions": 0, "urlm": {}, "actm": {}}
            self.store.execute("INSERT INTO intel_events(%s) VALUES(%s)" % (",".join(db._EVENT_COLS), ",".join("?" * len(db._EVENT_COLS))),
                               db._ev_values(ev))

    def test_building_then_flags(self):
        self.fill(days=10, spike=5.0)
        res = self.det.evaluate(self.now)
        self.assertEqual((res["state"], res["message"]), ("building", "Building the baseline: 10 of 14 days."))
        self.assertEqual(res["flags"], [])

    def test_real_spike_is_unusual(self):
        self.fill(days=20, spike=5.0)
        res = self.det.evaluate(self.now)
        self.assertEqual(res["state"], "ok")
        self.assertEqual(res["baseline_days"]["conflict"], 20)
        flags = {(f["iso2"], f["metric"]): f for f in res["flags"]}
        aa = flags[("AA", "conflict")]
        self.assertEqual(aa["level"], "unusual")
        self.assertGreaterEqual(aa["ratio"], 4.5)
        self.assertEqual(aa["title"], "Unusually many conflict reports about Alphaland")
        self.assertNotIn(("BB", "volume"), flags)                                     # steady countries stay quiet
        self.assertNotIn(("ZZ", "conflict"), flags)
        eps = db.query_spikes(self.store, since=0)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["id"], f"AA:conflict:{int(math.floor(self.now / 3600) * 3600 - 3600)}")
        self.assertEqual(eps[0]["severity"], spikes.severity(aa["ratio"]))
        # an hour later the same episode is extended, not duplicated
        later = self.now + 3600
        hour = math.floor(later / 3600) * 3600
        with self.store.tx() as c:
            c.executemany("INSERT OR REPLACE INTO intel_files(ts, fetched_at, status, tallies) VALUES(?, 0, 'ok', ?)",
                          [(gdelt.epoch_to_ts(hour - 3600 + s * 900), db.TALLIES_VERSION) for s in range(4)])
            for iso, n, cr in (("AA", 400, 200), ("BB", 60, 3), ("CC", 900, 12), ("ZZ", 20000, 900)):
                c.execute("INSERT OR REPLACE INTO intel_country_hour(iso2, hour, rows, conflict_rows, protest_rows, tone_sum, tone_w) "
                          "VALUES(?,?,?,?,0,0,1)", (iso, int(hour - 3600), n // 24, cr // 24))
        self.det.evaluate(later)
        eps2 = db.query_spikes(self.store, since=0)
        self.assertEqual(len(eps2), 1)
        self.assertEqual(eps2[0]["last_t"], hour)
        reading = self.det.country("AA", self.now)
        self.assertEqual(reading["metrics"]["conflict"]["level"], "unusual")
        self.assertEqual(len(reading["series"]), 20)

    def test_single_story_is_downgraded(self):
        self.fill(days=20, spike=5.0, story=180)                                     # 180 of ~200 rows in one event
        res = self.det.evaluate(self.now)
        aa = next(f for f in res["flags"] if f["iso2"] == "AA" and f["metric"] == "conflict")
        self.assertEqual((aa["level"], aa["reason"]), ("notable", "Mostly one story"))
        self.assertEqual(db.query_spikes(self.store, since=0), [])

    def test_outage_guards(self):
        self.fill(days=20, spike=5.0, volume=0.3)
        res = self.det.evaluate(self.now)
        self.assertEqual(res["state"], "paused")
        self.assertIn("under half its usual level", res["message"])

    def test_missing_files_pause(self):
        self.fill(days=20, spike=5.0, files_window=False)
        res = self.det.evaluate(self.now)
        self.assertEqual(res["state"], "paused")
        self.assertEqual(res["flags"], [])


if __name__ == "__main__":
    unittest.main()
