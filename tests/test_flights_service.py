"""Flights module end to end without network: a fake HTTP client serves fixtures, the real module
registers on a real App, and the routes, AI tools, search, nearby and events are exercised.

Run: python -m unittest tests.test_flights_service -v
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

from meridian.config import Config
from meridian.modules.flights.parse import parse_readsb
from meridian.datasets import DatasetManager
from meridian.net import Net, NetError, NetResponse
from meridian.registry import App, HTTPError, Request
from meridian.scheduler import Scheduler
from meridian.store import Store

FIX = Path(__file__).parent / "fixtures" / "flights"


def fixture(name: str, *, now: float | None = None) -> bytes:
    """Fixture bytes with the answer's timestamp moved to ``now`` (so positions are live)."""
    data = json.loads((FIX / name).read_text(encoding="utf-8"))
    if now is not None and isinstance(data, dict):
        if "now" in data:
            data["now"] = now * 1000 if data["now"] > 1e11 else now
        if "ctime" in data:
            data["ctime"] = now * 1000
        if "time" in data:
            data["time"] = int(now)
            for s in data.get("states") or []:
                if s[3]:
                    s[3] = int(now)
                s[4] = int(now)
        if "timestamp" in data:
            data["timestamp"] = now - 400
    return json.dumps(data).encode("utf-8")


class FakeNet:
    """Answers by URL pattern. ``routes``: [(regex, fn(url) -> (status, body, headers))]."""

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
            if re.search(pattern, full):
                status, body, hdrs = fn(full)
                if status in accept:
                    return NetResponse(status, hdrs or {}, body, full)
                raise NetError(f"{urllib.parse.urlsplit(full).hostname}: HTTP {status}", status=status, url=full)
        raise NetError(f"{urllib.parse.urlsplit(full).hostname}: no route in the fake", url=full)

    def get_json(self, url, **kw):
        return self.request(url, **kw).json()

    def count(self, pattern: str) -> int:
        with self.lock:
            return sum(1 for c in self.calls if re.search(pattern, c))


class FlightsEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MERIDIAN_DATA_DIR"] = self.tmp.name
        self.net = FakeNet()
        now = lambda: time.time()      # noqa: E731
        self.net.routes = [
            (r"opendata\.adsb\.fi/api/v3/lat/", lambda u: (200, fixture("adsbfi_v3_point.json", now=now()), {})),
            (r"opendata\.adsb\.fi/api/v2/sqk/7700", lambda u: (200, fixture("adsbfi_sqk7700.json", now=now()), {})),
            (r"opendata\.adsb\.fi/api/v2/(sqk|icao|registration|callsign)/", lambda u: (200, fixture("adsbfi_empty.json", now=now()), {})),
            (r"opendata\.adsb\.fi/api/v2/mil", lambda u: (200, fixture("adsbfi_v3_point.json", now=now()), {})),
            (r"api\.adsb\.lol/1/trace/recent/", lambda u: (200, fixture("adsblol_trace.json", now=now()),
                                                         {"x-ratelimit-remaining": "480", "x-ratelimit-reset": "3600"})),
            (r"api\.adsbdb\.com/v0/(aircraft/4cacb5|callsign/SAS87J)", lambda u: (200, (FIX / "adsbdb_both.json").read_bytes(), {})),
            (r"api\.adsbdb\.com/v0/", lambda u: (404, b'{"response": "unknown aircraft"}', {})),
        ]
        cfg = Config(None, overrides={"FLIGHTS_GPSJAM_SAMPLER": "0"})
        log = logging.getLogger("meridian.test")
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.app = App(cfg, self.store, self.net, log, Scheduler(log), DatasetManager(log))
        self.m = self.app.module("flights")
        from meridian.modules import flights
        flights.register(self.m)
        self.app.modules["flights"].ok = True
        self.svc = self.m.flights
        self.stop = threading.Event()
        for q in self.svc.queues():
            threading.Thread(target=q.run, args=(self.stop,), daemon=True).start()

    def tearDown(self):
        self.stop.set()
        time.sleep(0.05)
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

    def live_london(self):
        return self.get("/api/flights/live?bbox=-1.2,51.2,0.4,51.8&zoom=9.5")

    # -- tests -------------------------------------------------------------------------------------
    def test_live_view(self):
        res = self.live_london()
        self.assertEqual(res["mode"], "tiles")
        self.assertEqual(res["level"], 2)
        self.assertEqual(res["fields"][0], "hex")
        hexes = {r[0] for r in res["ac"]}
        self.assertIn("4cacb5", hexes)
        self.assertNotIn("730abc", hexes)          # over Iran, outside the view
        self.assertGreaterEqual(res["tiles"]["fresh"], 1)
        self.assertIsNone(res["error"])
        self.assertEqual(res["providers"], ["adsb.fi"])
        before = self.net.count(r"/v3/lat/")
        self.live_london()                         # cached tiles: no new requests
        self.assertEqual(self.net.count(r"/v3/lat/"), before)

    def test_wide_view_and_errors(self):
        res = self.get("/api/flights/live?bbox=-180,-85,180,85&zoom=2")
        self.assertEqual(res["mode"], "wide")
        self.assertIn("Zoom in", res["note"])
        with self.assertRaises(HTTPError):
            self.get("/api/flights/live?bbox=1,2,3&zoom=5")
        with self.assertRaises(HTTPError):
            self.get("/api/flights/live?zoom=5")

    def test_fallback_when_adsbfi_rate_limits(self):
        self.net.routes.insert(0, (r"opendata\.adsb\.fi/api/v3/lat/", lambda u: (429, b"", {"retry-after": "120"})))
        self.net.routes.insert(0, (r"api\.adsb\.lol/v2/point/", lambda u: (200, fixture("adsbfi_v3_point.json", now=time.time()),
                                                                          {"x-ratelimit-remaining": "470"})))
        res = self.live_london()
        self.assertIsNotNone(res)
        deadline = time.time() + 5
        while time.time() < deadline and not self.net.count(r"api\.adsb\.lol/v2/point/"):
            time.sleep(0.2)
            self.svc.refresh_wanted_tiles()
        self.assertGreater(self.net.count(r"api\.adsb\.lol/v2/point/"), 0)
        self.assertFalse(self.svc.adsbfi.available("point"))
        self.assertGreater(self.svc.adsblol.budget.info()["used_today"], 0)

    def test_aircraft_card_and_trail(self):
        self.live_london()
        d = self.get("/api/flights/aircraft/4cacb5")
        self.assertTrue(d["live"])
        self.assertEqual(d["title"], "SAS87J")
        self.assertEqual(d["operator"], {"text": "Scandinavian Airlines Connect", "kind": "airline"})
        self.assertEqual(d["route_status"], "ok")
        self.assertEqual(d["route_text"], "ARN to LHR")
        self.assertEqual(d["category_label"], "Large (34 to 136 t)")
        self.assertTrue(any("adsb.lol" in l["href"] for l in d["links"]))
        lad = self.get("/api/flights/aircraft/a0f00d")
        self.assertEqual(lad["privacy"], "LADD")
        self.assertEqual(lad["operator"]["kind"], "withheld")
        t = self.get("/api/flights/trail/4cacb5")
        self.assertGreaterEqual(len(t["points"]), 3)         # backfilled from the adsb.lol trace
        self.assertEqual(len(t["points"][0]), 4)
        with self.assertRaises(HTTPError):
            self.get("/api/flights/aircraft/zzzzzz")
        gone = self.get("/api/flights/aircraft/abcdef")
        self.assertFalse(gone["live"])
        self.assertFalse(gone["known"])

    def test_last_seen_after_restartable_store(self):
        self.live_london()
        self.svc.seen_flush_t = 0
        self.svc.housekeeping()
        row = self.store.one("SELECT * FROM flights_seen WHERE hex = '4cacb5'")
        self.assertEqual(row["callsign"], "SAS87J")
        del self.svc.tracker.aircraft["4cacb5"]
        d = self.get("/api/flights/aircraft/4cacb5")
        self.assertFalse(d["live"])
        self.assertEqual(d["last_seen"]["callsign"], "SAS87J")

    def test_military_alerts_events(self):
        mil = self.get("/api/flights/military")
        self.assertIn("ae0805", {r[0] for r in mil["ac"]})
        self.assertNotIn("4cacb5", {r[0] for r in mil["ac"]})
        al = self.get("/api/flights/alerts")
        self.assertEqual([r[0] for r in al["ac"]], ["400a0e"])
        self.assertEqual(al["details"][0]["code"], "7700")
        ev = self.app.collect_events(time.time() - 3600)
        emg = [e for e in ev if e.get("event") == "emergency"]
        self.assertEqual(emg[0]["id"], "400a0e")
        self.assertEqual(emg[0]["severity"], 7)
        self.assertIn("General emergency (7700)", emg[0]["title"])

    def test_emergency_episode_survives_restart(self):
        from meridian.modules.flights.service import FlightsService, emergency_label
        self.get("/api/flights/alerts")
        n1 = self.store.scalar("SELECT COUNT(*) FROM flights_events WHERE kind = 'emergency'")
        self.assertEqual(n1, 1)
        again = FlightsService(self.m)                     # a restart: fresh memory, same database
        _, obs = parse_readsb(json.loads(fixture("adsbfi_sqk7700.json", now=time.time())))
        again._absorb(obs, "adsbfi", time.time())
        self.assertEqual(self.store.scalar("SELECT COUNT(*) FROM flights_events WHERE kind = 'emergency'"), 1)
        self.assertIn("no emergency squawk", emergency_label("downed"))
        self.assertEqual(emergency_label("7600"), "Radio failure (7600)")

    def test_sanctions_match_hides_individuals(self):
        rec = {"uid": "99999", "program": "IRAN] [SDGT", "registration": "EP-TST", "model": "A300", "operator": "Example Air",
               "linked_to": ["EXAMPLE AIR", "DOE, John"], "match": "mode_s", "mode_s": "730abc"}
        self.svc.tracker.sanctions_lookup = lambda h, r: dict(rec) if h == "730abc" else None
        self.get("/api/flights/live?bbox=48,34,52,38&zoom=8")
        d = self.get("/api/flights/aircraft/730abc")
        s = d["sanctions"]
        self.assertEqual(s["programs"], ["IRAN", "SDGT"])
        self.assertEqual(s["linked_to"], ["EXAMPLE AIR"])
        self.assertEqual(s["linked_individuals"], 1)
        self.assertNotIn("DOE", json.dumps(d))
        ev = [e for e in self.app.collect_events(time.time() - 600) if e.get("event") == "sanctioned"]
        self.assertEqual(ev[0]["severity"], 5)

    def test_airport_traffic(self):
        res = self.get("/api/flights/airport/EGLL?lat=51.4706&lon=-0.4619")
        self.assertTrue(res["covered"])
        arrivals = {a["id"]: a for a in res["arrivals"]}
        self.assertIn("400a0e", arrivals)           # descending through 1,450 ft
        self.assertEqual(arrivals["400a0e"]["kind"], "aircraft")
        self.assertEqual(arrivals["400a0e"]["alt_ft"], 1450)
        self.assertGreaterEqual(res["on_ground"], 1)
        self.assertIn("unverified", res["note"])
        with self.assertRaises(HTTPError):
            self.get("/api/flights/airport/E<GLL?lat=51.47&lon=-0.46")

    def test_search_nearby_ai(self):
        self.live_london()
        found = self.app.run_search("SAS87J", 5)
        self.assertEqual(found[0]["id"], "4cacb5")
        near = self.app.run_nearby(51.47, -0.45, 20, 10)
        self.assertTrue(near)
        self.assertNotIn("42592e", {n["id"] for n in near})      # ground vehicles are left out
        area = self.app.call_ai_tool("flights_in_area", {"lat": 51.5, "lon": -0.5, "radius_km": 60})
        self.assertGreater(area["count"], 3)
        self.assertIn("airliner", area["by_class"])
        det = self.app.call_ai_tool("aircraft_details", {"callsign": "SAS87J"})
        self.assertTrue(det["found"])
        self.assertEqual(det["details"]["registration"], "EI-SIB")
        emg = self.app.call_ai_tool("flights_emergencies", {"hours": 2})
        self.assertEqual(emg["current"][0]["hex"], "400a0e")
        gps = self.app.call_ai_tool("gps_interference", {"lat": 55, "lon": 20, "radius_km": 300})
        self.assertIn("method", gps)
        with self.assertRaises(ValueError):
            self.app.call_ai_tool("flights_in_area", {"lat": 200, "lon": 0})

    def test_gpsjam_endpoints(self):
        obs = [{"hex": f"b{i:05x}", "lat": 54.6, "lon": 19.7, "pos_t": time.time() - 1, "alt": 35000, "gnd": False,
                "ptype": "adsb_icao", "version": 2, "nic": 8, "nac_p": 0 if i < 10 else 9} for i in range(40)]
        self.svc.gps.observe(obs)
        self.svc.gps.flush()
        res = self.get("/api/flights/gpsjam?bbox=10,50,30,60&zoom=6&hours=24")
        self.assertEqual(res["counts"]["red"], 1)
        self.assertEqual(res["cells"][0][:5], [54.5, 19.5, 0.5, 40, 10])
        cell = self.get("/api/flights/gpsjam/cell?lat=54.7&lon=19.8")
        self.assertEqual(cell["level"], "red")
        ev = [e for e in self.app.collect_events(time.time() - 7200) if e["kind"] == "gps_jam"]
        self.assertEqual(ev[0]["severity"], 5)
        self.assertEqual(ev[0]["layer"], "flights.gpsjam")
        with self.assertRaises(HTTPError):
            self.get("/api/flights/gpsjam?hours=99")

    def test_status_and_layers(self):
        self.live_london()
        st = self.get("/api/flights/status")
        self.assertIn("adsbfi", st["providers"])
        self.assertGreater(st["aircraft"]["tracked"], 5)
        ids = [s["id"] for s in self.app.layer_specs]
        self.assertEqual(ids, ["flights", "flights.military", "flights.alerts", "flights.gpsjam", "flights.delays",
                               "flights.tfr", "flights.czib", "flights.activity"])
        names = [t["name"] for t in self.app.ai_tools()]
        for n in ("flights_in_area", "aircraft_details", "flights_emergencies", "gps_interference", "airport_delays",
                  "airspace_restrictions", "air_activity", "airport_movements"):
            self.assertIn(n, names)
        keys = {k["name"]: k for k in self.app.config.key_status()}
        self.assertTrue(keys["OPENSKY_CLIENT_SECRET"]["secret"])
        self.assertFalse(keys["LOCAL_ADSB_URL"]["secret"])
        self.assertIn("flights.adsbfi", self.app.sources)


if __name__ == "__main__":
    unittest.main()
