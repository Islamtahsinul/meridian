"""Ships module tests: flags, classes, providers' parsing, the vessel store, straits, dark ships,
port activity, the service layer and the registered routes. No network (fixtures and fakes)."""

import copy
import json
import logging
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from meridian import geo
from meridian.modules.ships import analysis, classify, mid, providers, straits
from meridian.modules.ships.service import MAX_LIVE_ROWS, THIN_ABOVE, ShipsService
from meridian.modules.ships.store import F_SANCTIONED, F_SUSPECT, R_FLAGS, VesselStore
from meridian.modules.ships.straits import StraitMonitor

FIXTURES = Path(__file__).parent / "fixtures" / "ships"
T0 = 1795380000.0          # 2026-11-22 20:40 UTC


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def fake_lookup(imo=None, mmsi=None, name=None):
    if imo == "9000003":
        return {"match": "imo", "uid": "1", "name": "OLD NAME", "program": "RUSSIA-EO14024] [UKRAINE-EO13662",
                "aliases": ["ALIAS ONE"], "linked_to": ["SOME SHIPPING LLC"], "imo": "9000003", "mmsi": "273111111",
                "remarks": "Vessel Registration Identification IMO 9000003"}
    if mmsi == "636000777":
        return {"match": "mmsi", "uid": "2", "name": "LISTED", "program": "IRAN", "imo": "9000039", "mmsi": "636000777"}
    if name and name.upper() == "SHADOW":
        return {"match": "name", "uid": "3", "name": "SHADOW", "program": "RUSSIA-EO14024", "imo": "9000041"}
    return None


def regular_track(store, mmsi, t_end, lat, lon, *, sog=12.0, cog=90.0, n=10, step=120, src=1, ais=None):
    """n reports, ``step`` seconds apart, ending at t_end, moving along cog at sog."""
    pts = []
    for i in range(n):
        t = t_end - (n - 1 - i) * step
        back_km = sog * geo.KM_PER_NM * (t_end - t) / 3600.0
        plat, plon = geo.destination(lat, lon, (cog + 180) % 360, back_km)
        store.position(mmsi, t, plat, plon, sog=sog, cog=cog, nav=0, src=src, ais=ais)
        pts.append((plat, plon))
    return pts


# ---------------------------------------------------------------------------------------------
class FlagsAndClasses(unittest.TestCase):
    def test_flags(self):
        self.assertEqual(mid.flag_of(230982380), ("FI", "Finland"))
        self.assertEqual(mid.flag_of("366123456"), ("US", "United States"))
        self.assertEqual(mid.flag_of(273357360)[0], "RU")
        self.assertEqual(mid.flag_of(461000254)[1], "Oman")
        self.assertEqual(mid.flag_of(636012345)[1], "Liberia")
        self.assertEqual(mid.flag_of("002300001"), ("FI", "Finland"))     # coast station 00MID
        self.assertEqual(mid.flag_of(992301234), ("FI", "Finland"))       # aid to navigation 99MID
        self.assertEqual(mid.flag_of(111230123), ("FI", "Finland"))       # SAR aircraft 111MID
        self.assertIsNone(mid.flag_of(970123456))                         # AIS-SART
        self.assertIsNone(mid.flag_of("12345"))
        self.assertGreater(len(mid.MID), 280)

    def test_station_kind(self):
        self.assertEqual(mid.station_kind(230982380), "ship")
        self.assertEqual(mid.station_kind("992301234"), "aton")
        self.assertEqual(mid.station_kind("002300001"), "coast")
        self.assertTrue(mid.is_ship_mmsi(230982380))
        self.assertFalse(mid.is_ship_mmsi(992301234))
        self.assertFalse(mid.is_ship_mmsi(111230123))

    def test_classes(self):
        cases = {70: "cargo", 79: "cargo", 80: "tanker", 89: "tanker", 60: "passenger", 69: "passenger", 30: "fishing",
                 31: "service", 32: "service", 50: "service", 52: "service", 55: "service", 59: "service",
                 35: "military", 36: "pleasure", 37: "pleasure", 40: "highspeed", 49: "highspeed", 33: "other",
                 90: "other", 0: "other", None: "other", 20: "other"}
        for code, want in cases.items():
            self.assertEqual(classify.classify(code), want, code)
        self.assertEqual(len(classify.CLASSES), 9)

    def test_type_labels(self):
        self.assertEqual(classify.type_label(84), "Tanker, hazardous category D")
        self.assertEqual(classify.type_label(70), "Cargo ship")
        self.assertEqual(classify.type_label(52), "Tug")
        self.assertEqual(classify.type_label(0), "Type not reported")
        self.assertEqual(classify.size_class(300), 4)
        self.assertEqual(classify.size_class(None), 0)


# ---------------------------------------------------------------------------------------------
class ProviderParsing(unittest.TestCase):
    def test_digitraffic_locations(self):
        rows = providers.parse_dt_locations(load_fixture("digitraffic_locations.json"))
        self.assertEqual(len(rows), 8)
        moored = next(r for r in rows if r["mmsi"] == 230000002)
        self.assertIsNone(moored["hdg"])          # 511
        self.assertIsNone(moored["rot"])          # -128
        self.assertEqual(moored["nav"], 5)
        self.assertEqual(moored["t"], 1795379960.0)
        self.assertEqual(providers.parse_dt_locations({"oops": 1}), [])

    def test_digitraffic_vessels(self):
        rows = providers.parse_dt_vessels(load_fixture("digitraffic_vessels.json"))
        cargo = next(r for r in rows if r["mmsi"] == 230000001)
        self.assertEqual(cargo["draught"], 8.2)
        self.assertEqual(cargo["eta"], "11-25T06:30Z")
        self.assertEqual((cargo["length"], cargo["beam"]), (150.0, 23.0))
        self.assertEqual(providers.parse_dt_vessels("nope"), [])

    def test_digitraffic_into_store(self):
        store = VesselStore(None, now=Clock(1795380010.0))
        for r in providers.parse_dt_locations(load_fixture("digitraffic_locations.json")):
            store.position(r["mmsi"], r["t"], r["lat"], r["lon"], sog=r["sog"], cog=r["cog"], hdg=r["hdg"],
                           nav=r["nav"], rot=r["rot"], src=1)
        for r in providers.parse_dt_vessels(load_fixture("digitraffic_vessels.json")):
            t = r.pop("t")
            store.static(r.pop("mmsi"), t, src=1, **r)
        self.assertNotIn(992300005, store.vessels)         # aid to navigation
        self.assertNotIn(230000008, store.vessels)         # latitude 91: not available
        cargo = store.vessels[230000001]
        self.assertEqual(cargo.name, "TEST CARGO")
        self.assertEqual(cargo.cls, "cargo")
        self.assertEqual(cargo.callsign, "OJAB")
        self.assertEqual(cargo.dest, "KOTKA")
        self.assertEqual(store.vessels[276000003].imo, None)   # 1234568 fails the IMO checksum
        self.assertEqual(store.vessels[265000004].cls, "pleasure")
        self.assertTrue(store.vessels[273000006].suspect(1795380010.0))   # 55 kn cargo: corrupted fix

    def test_aisstream_messages(self):
        now = 1795380000.0
        lines = (FIXTURES / "aisstream_messages.jsonl").read_text(encoding="utf-8").splitlines()
        msgs = [providers.parse_aisstream(json.loads(ln), now) for ln in lines]
        self.assertEqual(msgs[0], {"confirmed": True, "compression": True})
        pos = msgs[1]
        self.assertEqual((pos["type"], pos["mmsi"], pos["cls"]), (1, 538000101, "A"))
        self.assertAlmostEqual(pos["t"], 1795379990.123, places=2)
        self.assertEqual(pos["heading"], 93)
        stat = msgs[2]
        self.assertEqual((stat["type"], stat["imo"], stat["eta"], stat["draught"]), (5, 9000027, "11-25T06:30Z", 14.1))
        self.assertEqual((stat["length"], stat["beam"]), (250.0, 42.0))
        b = msgs[3]
        self.assertEqual((b["type"], b["cls"], b["heading"]), (18, "B", None))
        self.assertEqual(msgs[4]["name"], "BLUE PELICAN")
        self.assertEqual((msgs[5]["ship_type"], msgs[5]["callsign"]), (37, "EA1234"))
        self.assertIsNone(msgs[6])                                     # Valid: false
        self.assertEqual(msgs[7], {"error": "Api Key Is Not Valid"})

        store = VesselStore(None, now=Clock(now))
        for m in msgs:
            if m and "type" in m:
                providers.apply_aisstream(store, m)
        v = store.vessels[538000101]
        self.assertEqual((v.name, v.cls, v.ais, v.imo, v.callsign, v.dest), ("OCEAN TEST", "tanker", "A", 9000027,
                                                                             "V7AB1", "PIRAEUS"))
        b = store.vessels[224000202]
        self.assertEqual((b.name, b.cls, b.ais, b.length), ("BLUE PELICAN", "pleasure", "B", 12.0))

    def test_time_parsing(self):
        now = 1795380000.0
        self.assertAlmostEqual(providers.parse_time_utc("2026-11-22 20:39:50.5 +0000 UTC", now), now - 9.5, places=3)
        self.assertEqual(providers.parse_time_utc("garbage", now), now)
        self.assertEqual(providers.parse_time_utc("2020-01-01 00:00:00 +0000 UTC", now), now)   # implausible

    def test_area_setting(self):
        mon = StraitMonitor()
        self.assertEqual(providers.parse_area(None, mon.defs)[0], [[[-90.0, -180.0], [90.0, 180.0]]])
        boxes, note = providers.parse_area("straits", mon.defs)
        self.assertEqual(len(boxes), len(straits.STRAITS))
        boxes, note = providers.parse_area("10,50,12,52;bad", mon.defs)
        self.assertEqual(boxes, [[[50.0, 10.0], [52.0, 12.0]]])
        boxes, note = providers.parse_area("nonsense", mon.defs)
        self.assertIn("not understood", note)

    def test_subscription_message(self):
        c = providers.AisStreamClient("KEY123", [[[1.0, 2.0], [3.0, 4.0]]], None, lambda *a, **k: None,
                                      logging.getLogger("t"), user_agent="ua")
        sub = json.loads(c.subscription())
        self.assertEqual(sub["APIKey"], "KEY123")
        self.assertEqual(sub["BoundingBoxes"], [[[1.0, 2.0], [3.0, 4.0]]])
        self.assertIn("ShipStaticData", sub["FilterMessageTypes"])

    def test_nmea_into_store(self):
        from meridian.modules.ships import ais
        store = VesselStore(None, now=Clock(1241544100.0))
        asm = ais.Assembler()
        line = "\\c:1241544035*00\\!AIVDM,1,1,,B,15N4cJ`005Jrek0H@9n`DW5608EP,0*13"
        msg = asm.feed(line)
        providers.apply_decoded(store, msg, providers.SRC_KV, msg["tag_time"])
        v = store.vessels[367078250]
        self.assertEqual(v.t, 1241544035.0)
        self.assertEqual(v.src, providers.SRC_KV)


# ---------------------------------------------------------------------------------------------
class StreamLoops(unittest.TestCase):
    """The aisstream and Kystverket loops, driven by fake transports."""

    def test_aisstream_loop(self):
        import threading
        from meridian.modules.ships import ws as wsmod
        lines = (FIXTURES / "aisstream_messages.jsonl").read_text(encoding="utf-8").splitlines()[:6]
        sent = []

        class FakeWS:
            compression = True
            bytes_in = 0

            def __init__(self, *a, **k):
                self.queue = [(wsmod.OP_BINARY, ln.encode()) for ln in lines]

            def connect(self):
                pass

            def send_text(self, text):
                sent.append(json.loads(text))

            def recv(self, stop):
                if self.queue:
                    return self.queue.pop(0)
                raise wsmod.WebSocketClosed(1001, "going away")

            def close(self):
                pass

        statuses = []
        stop = threading.Event()

        def status(ok, count=None, error=None, note=None):
            statuses.append((ok, error, note))
            if ok is False:
                stop.set()

        store = VesselStore(None, now=Clock(1795380000.0))
        client = providers.AisStreamClient("KEY", [[[-90, -180], [90, 180]]], store, status, logging.getLogger("t"),
                                           user_agent="ua", area_note="the whole world")
        orig = providers.WebSocketClient
        providers.WebSocketClient = FakeWS
        try:
            client.run(stop)
        finally:
            providers.WebSocketClient = orig
        self.assertEqual(sent[0]["APIKey"], "KEY")
        self.assertIn(538000101, store.vessels)
        self.assertEqual(store.vessels[224000202].name, "BLUE PELICAN")
        self.assertTrue(client.compression)
        self.assertEqual(statuses[-1][0], False)
        self.assertIn("going away", statuses[-1][1])

    def test_aisstream_silent_drop_means_check_the_key(self):
        import threading
        from meridian.modules.ships import ws as wsmod

        class DropWS:
            compression = False
            bytes_in = 0

            def __init__(self, *a, **k):
                pass

            def connect(self):
                pass

            def send_text(self, text):
                pass

            def recv(self, stop):
                raise wsmod.WebSocketClosed(None, "the server dropped the connection")

            def close(self):
                pass

        errors = []
        stop = threading.Event()

        def status(ok, count=None, error=None, note=None):
            if ok is False:
                errors.append(error)
                stop.set()

        client = providers.AisStreamClient("BAD", [], VesselStore(None), status, logging.getLogger("t"), user_agent="ua")
        orig = providers.WebSocketClient
        providers.WebSocketClient = DropWS
        try:
            client.run(stop)
        finally:
            providers.WebSocketClient = orig
        self.assertIn("Check AISSTREAM_API_KEY", errors[0])

    def test_kystverket_loop(self):
        import socket as socketmod
        import threading
        payload = "\n".join(ln for ln in (FIXTURES / "nmea_samples.txt").read_text(encoding="utf-8").splitlines()
                            if ln.startswith("!")).encode() + b"\n"

        class FakeSock:
            def __init__(self):
                self.chunks = [payload[:100], payload[100:], b""]

            def settimeout(self, t):
                pass

            def recv(self, n):
                return self.chunks.pop(0)

            def close(self):
                pass

        errors = []
        stop = threading.Event()

        def status(ok, count=None, error=None, note=None):
            if ok is False:
                errors.append(error)
                stop.set()

        store = VesselStore(None, now=Clock(time.time()))
        client = providers.KystverketClient(store, status, logging.getLogger("t"))
        orig = socketmod.create_connection
        providers.socket.create_connection = lambda *a, **k: FakeSock()
        try:
            client.run(stop)
        finally:
            providers.socket.create_connection = orig
        self.assertGreaterEqual(client.stats["messages"], 10)
        self.assertEqual(store.vessels[351759000].name, "EVER DIADEM")       # type 5 across two chunks
        self.assertEqual(store.vessels[371798000].src, providers.SRC_KV)
        self.assertIn("closed the connection", errors[0])


# ---------------------------------------------------------------------------------------------
class StoreBehaviour(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = VesselStore(None, now=self.clock, lookup=fake_lookup)

    def test_validation(self):
        s = self.store
        self.assertFalse(s.position(992300005, T0, 60.0, 22.0))       # not a ship station
        self.assertFalse(s.position(230000001, T0, 91.0, 22.0))       # bad latitude
        self.assertFalse(s.position(230000001, T0, 0.0, 0.0))         # null island
        self.assertFalse(s.position("abc", T0, 60.0, 22.0))
        self.assertTrue(s.position(230000001, T0 - 10, 60.0, 22.0, sog=5.0))
        self.assertFalse(s.position(230000001, T0 - 20, 60.0, 22.0))  # older than what we have
        self.assertTrue(s.position(230000001, T0 + 3600, 60.01, 22.0))  # future: clamped to now
        self.assertEqual(s.vessels[230000001].t, T0)
        self.assertFalse(s.position(230000002, T0 - 3 * 86400, 60.0, 22.0))  # too old

    def test_jump_filter(self):
        s = self.store
        s.position(230000001, T0 - 60, 60.0, 22.0, sog=10.0)
        self.assertFalse(s.position(230000001, T0, 60.5, 22.0, sog=10.0))   # 55 km in a minute
        self.assertEqual(s.stats["rejected_jump"], 1)

    def test_port_call_cycle(self):
        s = self.store
        mmsi = 230000001
        t = T0 - 7200
        s.position(mmsi, t, 60.0, 24.0, sog=10.0, cog=0.0, nav=0)
        s.position(mmsi, t + 600, 60.05, 24.0, sog=0.1, cog=0.0, nav=5)     # stops
        v = s.vessels[mmsi]
        self.assertEqual(v.state, "stationary")
        self.assertTrue(v.stop_seen)
        s.position(mmsi, t + 3000, 60.0501, 24.0, sog=0.0, nav=5)
        s.position(mmsi, t + 4200, 60.06, 24.0, sog=8.0, cog=0.0, nav=0)    # leaves after 1 h
        calls = s.recent_calls(T0 - 86400)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].arr_t, t + 600)
        self.assertEqual(calls[0].dep_t, t + 4200)
        self.assertTrue(calls[0].seen)
        self.assertEqual(calls[0].nav, 5)
        self.assertEqual(v.state, "underway")

    def test_short_stop_is_not_a_call(self):
        s = self.store
        s.position(230000001, T0 - 3000, 60.0, 24.0, sog=10.0)
        s.position(230000001, T0 - 2400, 60.03, 24.0, sog=0.0)
        s.position(230000001, T0 - 2000, 60.04, 24.0, sog=9.0)
        self.assertEqual(s.recent_calls(T0 - 86400), [])

    def test_trail_spacing(self):
        s = self.store
        mmsi = 230000001
        start = T0 - 3600
        lat, lon = 60.0, 22.0
        for i in range(0, 1801, 30):                # straight course for 30 minutes, a fix every 30 s
            plat, plon = geo.destination(lat, lon, 90, 12 * geo.KM_PER_NM * i / 3600.0)
            s.position(mmsi, start + i, plat, plon, sog=12.0, cog=90.0)
        times = [p[1] for p in s.pending_track]
        self.assertEqual(times, [int(start), int(start + 600), int(start + 1200), int(start + 1800)])
        # now a hard turn: a point after 2 minutes, never sooner
        base = start + 1800
        plat, plon = geo.destination(lat, lon, 90, 12 * geo.KM_PER_NM * 0.5)
        s.position(mmsi, base + 60, plat + 0.002, plon, sog=12.0, cog=0.0)
        s.position(mmsi, base + 150, plat + 0.008, plon, sog=12.0, cog=0.0)
        times = [p[1] for p in s.pending_track]
        self.assertEqual(times[-1], int(base + 150))
        self.assertTrue(all(b - a >= 120 for a, b in zip(times, times[1:])))

    def test_recovers_from_a_spoofed_first_fix(self):
        s = self.store
        s.position(273000006, T0 - 120, 57.67, 32.53, sog=55.0, cog=10.0)      # spoofed, inland
        self.assertTrue(s.position(273000006, T0 - 60, 60.30, 28.60, sog=11.0, cog=250.0))   # real again
        v = s.vessels[273000006]
        self.assertEqual((v.lat, v.lon), (60.30, 28.60))
        self.assertFalse(s.position(273000006, T0, 61.30, 28.60, sog=11.0, cog=250.0))      # ordinary jump

    def test_suspect_and_hotspot(self):
        s = self.store
        s.position(273000006, T0, 57.67, 32.53, sog=55.0, cog=10.0)
        v = s.vessels[273000006]
        self.assertTrue(v.suspect(T0))
        self.assertEqual(s.pending_track, [])                 # corrupted fixes never become trail points
        s.hotspots = [(57.67, 32.53, 5.0)]
        s.position(273000007, T0, 57.672, 32.531, sog=7.0, cog=10.0)
        self.assertTrue(s.vessels[273000007].suspect(T0))
        snap = s.build_snapshot()
        self.assertTrue(snap.by_mmsi[273000006][R_FLAGS] & F_SUSPECT)

    def test_sanctions_matching(self):
        s = self.store
        s.position(230000001, T0, 60.0, 24.0, sog=5.0)
        s.static(230000001, T0, imo=9000003, name="NEW NAME")
        v = s.vessels[230000001]
        self.assertTrue(v.sanctioned)
        self.assertEqual(v.sanc["programs"], ["RUSSIA-EO14024", "UKRAINE-EO13662"])
        self.assertEqual(v.sanc["name"], "OLD NAME")
        s.static(230000009, T0, name="Shadow")
        self.assertTrue(s.vessels[230000009].possible_sanction)
        self.assertFalse(s.vessels[230000009].sanctioned)
        # MMSI on the list but a different IMO: weak, not confirmed
        s.static(636000777, T0, imo=9000053)
        self.assertEqual(s.vessels[636000777].sanc["match"], "mmsi_conflict")
        self.assertFalse(s.vessels[636000777].sanctioned)
        snap = s.build_snapshot()
        self.assertTrue(snap.by_mmsi[230000001][R_FLAGS] & F_SANCTIONED)

    def test_search(self):
        s = self.store
        s.position(230000001, T0, 60.0, 24.0, sog=5.0)
        s.static(230000001, T0, name="BALTIC QUEEN", imo=9000003, callsign="OJAB")
        s.static(230000002, T0, name="QUEEN OF THE SEA")
        self.assertEqual(s.search("230000001")[0][1].mmsi, 230000001)
        self.assertEqual(s.search("9000003")[0][1].mmsi, 230000001)
        self.assertEqual(s.search("ojab")[0][1].mmsi, 230000001)
        names = [v.name for _, v in s.search("queen")]
        self.assertEqual(names[0], "QUEEN OF THE SEA")          # prefix beats substring
        self.assertIn("BALTIC QUEEN", names)
        self.assertEqual(s.search("q"), [])

    def test_persistence_roundtrip(self):
        from meridian.store import Store
        tmp = tempfile.mkdtemp()
        try:
            db = Store(Path(tmp) / "t.db")
            s = VesselStore(db, now=self.clock, lookup=fake_lookup)
            s.create_tables()
            regular_track(s, 230000001, T0 - 60, 59.7, 24.2, cog=90.0)
            s.static(230000001, T0, name="TEST CARGO", imo=9000003, ship_type=70)
            s.position(230000002, T0, 60.15, 24.95, sog=0.0, nav=5)
            out = s.flush()
            self.assertEqual(out["vessels"], 2)
            self.assertGreaterEqual(out["track"], 2)
            s2 = VesselStore(db, now=self.clock, lookup=fake_lookup)
            self.assertEqual(s2.load(), 2)
            v = s2.vessels[230000001]
            self.assertEqual((v.name, v.imo, v.cls), ("TEST CARGO", 9000003, "cargo"))
            self.assertTrue(v.sanctioned)
            self.assertEqual(v.strait, "gulf_of_finland")
            self.assertGreaterEqual(len(s2.trail(230000001, T0 - 86400)), 2)
            # pruning: a vessel unseen for 8 days goes, track older than 24 h goes
            self.clock.t = T0 + 8 * 86400
            s2.prune()
            self.assertEqual(len(s2.vessels), 0)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM ships_track"), 0)
            db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
class Straits(unittest.TestCase):
    def test_label_points_inside(self):
        mon = StraitMonitor()
        for d in mon.defs:
            lon, lat = d["label"]
            self.assertEqual(mon.locate(lon, lat), d["id"], d["id"])
        self.assertIsNone(mon.locate(0.0, 0.0))
        self.assertEqual(len(mon.defs), 14)

    def test_transit_and_crossing(self):
        clock = Clock()
        store = VesselStore(None, now=clock)
        # eastbound through the Gulf of Finland chokepoint
        for i in range(8):
            store.position(230000001, T0 - 1800 + i * 240, 59.7, 23.98 + i * 0.07, sog=12.0, cog=88.0)
        # a ferry crossing north-south through the same polygon
        for i in range(6):
            store.position(276000003, T0 - 1500 + i * 240, 59.45 + i * 0.09, 24.3, sog=18.0, cog=355.0)
        st = store.straits.stats("gulf_of_finland", T0)
        self.assertEqual(st["transits_24h"], 1)
        self.assertEqual(st["transits_pos"], 1)
        self.assertEqual(st["unique_24h"], 2)
        self.assertEqual(st["unique_by_class"], {"other": 2})
        inside = store.straits.inside("gulf_of_finland", T0)
        self.assertEqual({v.mmsi for v in inside}, {276000003} | ({230000001} if store.vessels[230000001].strait else set()))
        store.straits.sweep(T0 + 7200)
        self.assertEqual(store.straits.active, {})
        self.assertGreaterEqual(len(store.straits.take_pending()), 1)

    def test_angle_diff(self):
        self.assertEqual(straits.angle_diff(350, 10), 20)
        self.assertEqual(straits.angle_diff(90, 270), 180)


# ---------------------------------------------------------------------------------------------
class DarkShips(unittest.TestCase):
    def build(self, *, neighbours_at_estimate=True, ais=None, nav=0, receiving_since=T0 - 30000, regular=True,
              cls_type=70):
        clock = Clock(T0)
        store = VesselStore(None, now=clock)
        last_lat, last_lon = 59.8, 22.0
        if regular:
            regular_track(store, 230000001, T0, last_lat, last_lon, sog=12.0, cog=90.0, ais=ais)
        else:
            store.position(230000001, T0 - 3000, last_lat, last_lon - 0.3, sog=12.0, cog=90.0)
            store.position(230000001, T0, last_lat, last_lon, sog=12.0, cog=90.0)
        store.static(230000001, T0, name="QUIET ONE", ship_type=cls_type, imo=9000065)
        store.vessels[230000001].nav = nav
        clock.t = T0 + 3 * 3600
        est_lat, est_lon = geo.destination(last_lat, last_lon, 90, 12 * geo.KM_PER_NM * 3)
        for i in range(4):                                     # still heard near the last position
            store.position(230000100 + i, clock.t - 60, last_lat + 0.02 * i, last_lon + 0.05, sog=8.0, cog=270)
        if neighbours_at_estimate:
            for i in range(3):
                store.position(230000200 + i, clock.t - 60, est_lat - 0.02 * i, est_lon, sog=9.0, cog=270)
        snap = store.build_snapshot()
        return store, snap, clock, {1: (receiving_since, clock.t - 30)}

    def test_flags_dark_vessel(self):
        store, snap, clock, receiving = self.build()
        dark = analysis.find_dark(store, snap, clock.t, receiving)
        self.assertEqual([d["mmsi"] for d in dark], ["230000001"])
        d = dark[0]
        self.assertEqual(d["name"], "QUIET ONE")
        self.assertAlmostEqual(d["silent_s"], 3 * 3600, delta=1)
        run_km = geo.haversine_km(d["last"]["lat"], d["last"]["lon"], d["est"]["lat"], d["est"]["lon"])
        self.assertAlmostEqual(run_km, 12 * geo.KM_PER_NM * 3, delta=0.5)
        self.assertAlmostEqual(d["radius_km"], 2 + 0.35 * 12 * geo.KM_PER_NM * 3, delta=0.2)
        self.assertGreaterEqual(d["neighbours_last"], 3)
        self.assertEqual(d["severity"], 4)
        # reappearing records an AIS gap
        store.position(230000001, clock.t, d["est"]["lat"], d["est"]["lon"], sog=12.0, cog=90.0)
        self.assertEqual(len(store.pending_gaps), 1)

    def test_dead_reckoning_is_capped(self):
        store, snap, clock, receiving = self.build()
        clock.t = T0 + 6 * 3600
        for row in snap.rows:                       # keep the neighbours fresh at the new time
            store.position(row[0], clock.t - 30, row[1], row[2], sog=8.0, cog=270)
        far_lat, far_lon = geo.destination(59.8, 22.0, 90, 12 * geo.KM_PER_NM * 3)
        snap = store.build_snapshot()
        dark = analysis.find_dark(store, snap, clock.t, {1: (T0 - 30000, clock.t)})
        self.assertEqual(len(dark), 1)
        self.assertTrue(dark[0]["capped"])
        self.assertLess(geo.haversine_km(dark[0]["est"]["lat"], dark[0]["est"]["lon"], far_lat, far_lon), 1.0)
        self.assertEqual(dark[0]["severity"], 5)    # silent for 6 h

    def test_not_dark_when_left_coverage(self):
        store, snap, clock, receiving = self.build(neighbours_at_estimate=False)
        self.assertEqual(analysis.find_dark(store, snap, clock.t, receiving), [])

    def test_not_dark_class_b_or_anchored_or_irregular(self):
        for kwargs in ({"ais": "B"}, {"nav": 1}, {"regular": False}, {"cls_type": 37}):
            store, snap, clock, receiving = self.build(**kwargs)
            self.assertEqual(analysis.find_dark(store, snap, clock.t, receiving), [], kwargs)

    def test_not_dark_when_receiver_was_down(self):
        store, snap, clock, _ = self.build()
        self.assertEqual(analysis.find_dark(store, snap, clock.t, {1: (T0 + 3600, clock.t)}), [])
        self.assertEqual(analysis.find_dark(store, snap, clock.t, {1: (None, 0.0)}), [])


# ---------------------------------------------------------------------------------------------
class PortActivity(unittest.TestCase):
    def test_counts(self):
        clock = Clock(T0)
        store = VesselStore(None, now=clock)
        store.position(230000001, T0 - 60, 60.16, 24.95, sog=0.0, nav=5, src=1)
        store.position(230000002, T0 - 60, 60.15, 24.96, sog=0.2, nav=1, src=1)
        store.position(230000003, T0 - 60, 60.14, 24.90, sog=11.0, cog=180.0, src=1)
        store.position(230000004, T0 - 60, 61.50, 21.00, sog=0.0, nav=5)        # far away
        # a call that ended two hours ago
        store.position(230000005, T0 - 9000, 60.155, 24.95, sog=6.0)
        store.position(230000005, T0 - 8100, 60.157, 24.951, sog=0.0, nav=5)
        store.position(230000005, T0 - 7200, 60.165, 24.951, sog=7.0)
        snap = store.build_snapshot()
        pa = analysis.port_activity(store, snap, 60.155, 24.95, 5.0, T0)
        self.assertEqual((pa["moored"], pa["anchored"], pa["underway"]), (1, 1, 1))
        self.assertEqual(pa["departures_24h"], 0)                  # 15 minutes stopped, under the minimum
        self.assertTrue(pa["coverage_ok"])
        self.assertIn("Digitraffic", pa["coverage"])
        self.assertEqual(len(pa["vessels"]), 3)
        self.assertEqual(pa["vessels"][0]["kind"], "ship")
        store.position(230000006, T0 - 9000, 60.155, 24.95, sog=6.0)
        store.position(230000006, T0 - 8000, 60.156, 24.95, sog=0.0, nav=5)
        store.position(230000006, T0 - 4000, 60.17, 24.95, sog=7.0)
        pa = analysis.port_activity(store, store.build_snapshot(), 60.155, 24.95, 5.0, T0)
        self.assertEqual((pa["arrivals_24h"], pa["departures_24h"]), (1, 1))

    def test_busiest_ports_count_each_vessel_once(self):
        store = VesselStore(None, now=Clock(T0))
        for i in range(6):
            store.position(276000100 + i, T0 - 60, 59.445 + i * 0.002, 24.765, sog=0.0, nav=5)
        store.position(230000200, T0 - 60, 60.16, 24.95, sog=0.0, nav=5)
        snap = store.build_snapshot()
        ports = [{"id": "locode:EEVAN", "name": "Vanasadam", "lat": 59.446, "lon": 24.766, "size": "unknown"},
                 {"id": "locode:EETLL", "name": "Tallinn", "lat": 59.44, "lon": 24.75, "size": "large"},
                 {"id": "locode:FIHEL", "name": "Helsinki", "lat": 60.16, "lon": 24.95, "size": "large"},
                 {"id": "x", "name": "Nowhere", "lat": 10.0, "lon": 10.0, "size": "large"}]
        out = analysis.busiest_ports(store, snap, ports, T0)
        self.assertEqual([p["name"] for p in out], ["Tallinn", "Helsinki"])
        self.assertEqual(out[0]["moored"], 6)

    def test_no_coverage(self):
        store = VesselStore(None, now=Clock(T0))
        pa = analysis.port_activity(store, store.build_snapshot(), 1.26, 103.84, 10.0, T0)
        self.assertEqual(pa["total"], 0)
        self.assertFalse(pa["coverage_ok"])
        self.assertIn("aisstream.io key", pa["coverage"])


# ---------------------------------------------------------------------------------------------
class Service(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(T0)
        self.svc = ShipsService(now=self.clock, lookup=fake_lookup, log=logging.getLogger("t"))
        self.store = self.svc.store

    def test_live_rows_and_filters(self):
        self.store.position(230000001, T0 - 30, 59.7, 24.2, sog=12.0, cog=85.0)
        self.store.static(230000001, T0, name="TEST CARGO", ship_type=70, length=150)
        self.store.position(230000002, T0 - 30, 60.15, 24.95, sog=0.0, nav=5)
        self.svc.refresh_snapshot()
        out = self.svc.live((20.0, 58.0, 30.0, 62.0), 8)
        self.assertEqual(out["in_view"], 2)
        row = next(r for r in out["rows"] if r[0] == 230000001)
        self.assertEqual(row[1:4], [24.2, 59.7, 85])
        self.assertEqual(out["classes"][row[4]], "cargo")
        self.assertEqual(row[6], 120)
        self.assertEqual(row[8], "TEST CARGO")
        still = next(r for r in out["rows"] if r[0] == 230000002)
        self.assertEqual(still[3], -1)
        self.assertEqual(self.svc.live((20.0, 58.0, 30.0, 62.0), 8, moving_only=True)["in_view"], 1)
        self.assertEqual(self.svc.live((20.0, 58.0, 30.0, 62.0), 8, classes={"tanker"})["in_view"], 0)
        empty = self.svc.live((-10.0, 30.0, 0.0, 40.0), 6)
        self.assertEqual(empty["in_view"], 0)
        self.assertIn("aisstream.io key", empty["note"])

    def test_antimeridian_bbox(self):
        self.store.position(412000001, T0 - 30, 10.0, 179.5, sog=10.0, cog=90.0)
        self.store.position(412000002, T0 - 30, 10.0, -179.5, sog=10.0, cog=90.0)
        self.store.position(412000003, T0 - 30, 10.0, 0.0, sog=10.0, cog=90.0)
        self.svc.refresh_snapshot()
        out = self.svc.live((179.0, 5.0, -179.0, 15.0), 6)
        self.assertEqual(sorted(r[0] for r in out["rows"]), [412000001, 412000002])

    def test_thinning(self):
        n = THIN_ABOVE + 500
        for i in range(n):
            self.store.position(230000000 + i, T0 - 30, 59.0 + (i % 60) * 0.01, 20.0 + (i // 60) * 0.01,
                                sog=5.0 + i % 3, cog=90.0)
        self.svc.refresh_snapshot()
        out = self.svc.live(None, 3)
        self.assertTrue(out["thinned"])
        self.assertLessEqual(out["shown"], MAX_LIVE_ROWS)
        self.assertLess(out["shown"], n)
        self.assertEqual(sum(r[10] for r in out["rows"]), n)
        full = self.svc.live(None, 14)
        self.assertGreater(full["shown"], out["shown"])

    def test_vessel_detail(self):
        unknown = self.svc.vessel(230999999)
        self.assertFalse(unknown["known"])
        self.assertEqual(unknown["flag_name"], "Finland")
        self.assertIn("7 days", unknown["note"])
        regular_track(self.store, 230000001, T0 - 60, 59.7, 24.2, cog=90.0)
        self.store.static(230000001, T0, name="TEST CARGO", imo=9000003, ship_type=70, length=150, beam=23,
                          dest="KOTKA", eta="11-25T06:30Z", draught=8.2, callsign="OJAB")
        d = self.svc.vessel(230000001)
        self.assertTrue(d["known"])
        self.assertTrue(d["live"])
        self.assertEqual(d["type_label"], "Cargo ship")
        self.assertEqual(d["sanctions_strength"], "confirmed")
        self.assertEqual(d["strait"]["id"], "gulf_of_finland")
        self.assertEqual(d["eta_t"], 1795588200.0)
        self.assertEqual(d["state"], "underway")
        hrefs = [link["href"] for link in d["links"]]
        self.assertIn("https://www.marinetraffic.com/en/ais/details/ships/mmsi:230000001", hrefs)
        self.assertIn("https://www.vesselfinder.com/vessels/details/9000003", hrefs)
        tr = self.svc.trail(230000001, 24)
        self.assertGreaterEqual(tr["count"], 2)
        self.assertEqual(tr["geometry"]["type"], "LineString")

    def test_search_nearby_events(self):
        self.store.position(230000001, T0 - 30, 59.7, 24.2, sog=12.0, cog=85.0)
        self.store.static(230000001, T0, name="TEST CARGO", imo=9000003, ship_type=70)
        self.svc.refresh_snapshot()
        hits = self.svc.search("test car", 10)
        self.assertEqual(hits[0]["kind"], "ship")
        self.assertEqual(hits[0]["id"], "230000001")
        self.assertEqual(hits[0]["layer"], "ships")
        near = self.svc.nearby(59.7, 24.21, 5, 10)
        self.assertEqual(near[0]["id"], "230000001")
        self.assertIn("distance_km", near[0])
        self.svc.run_analysis()
        events = self.svc.events(T0 - 86400)
        sanctioned = [e for e in events if e.get("category") == "sanctioned_vessel"]
        self.assertEqual(len(sanctioned), 1)
        self.assertEqual(sanctioned[0]["severity"], 6)
        for e in events:
            for key in ("kind", "id", "title", "lat", "lon", "time", "severity"):
                self.assertIn(key, e)

    def test_hotspot_event(self):
        self.store.position(273000006, T0 - 30, 57.67, 32.53, sog=55.0, cog=10.0)
        self.store.position(273000007, T0 - 30, 57.68, 32.52, sog=61.0, cog=200.0)
        self.svc.refresh_snapshot()
        self.svc.run_analysis()
        self.assertEqual(len(self.svc.hotspots), 1)
        ev = [e for e in self.svc.events(T0 - 3600) if e["kind"] == "gps_jam"]
        self.assertEqual(len(ev), 1)
        # Two vessels with one signature (impossible speeds) is a low-confidence cluster: severity 4
        # (tests/test_ships_anomalies.py covers the medium and high cases).
        self.assertEqual(ev[0]["severity"], 4)
        self.assertEqual(ev[0]["category"], "gnss_interference")
        self.assertEqual(ev[0]["signatures"], ["speed"])
        self.assertTrue(ev[0]["id"].startswith("ships:"))
        self.assertTrue(self.store.in_hotspot(57.675, 32.525))

    def test_ai_tools(self):
        self.store.position(230000001, T0 - 30, 59.7, 24.2, sog=12.0, cog=85.0)
        self.store.static(230000001, T0, name="TEST CARGO", imo=9000003, ship_type=70)
        self.svc.refresh_snapshot()
        self.svc.run_analysis()
        r = self.svc.ai_ships_in_area({"bbox": [20, 58, 30, 62]})
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["by_class"], {"cargo": 1})
        r = self.svc.ai_ships_in_area({"lat": 59.7, "lon": 24.2, "radius_km": 10, "type": "tanker"})
        self.assertEqual(r["count"], 0)
        self.assertIn("error", self.svc.ai_ships_in_area({}))
        self.assertIn("error", self.svc.ai_ships_in_area({"bbox": [1, 2, 3]}))
        d = self.svc.ai_vessel_details({"mmsi": "230000001"})
        self.assertEqual(d["title"], "TEST CARGO")
        self.assertEqual(d["sanctions"]["match"], "imo")
        self.assertEqual(self.svc.ai_vessel_details({"name": "TEST CARGO"})["mmsi"], "230000001")
        self.assertIn("error", self.svc.ai_vessel_details({"mmsi": "12"}))
        st = self.svc.ai_strait_status({"strait": "gulf of finland"})
        self.assertEqual(st["straits"][0]["id"], "gulf_of_finland")
        self.assertNotIn("geometry", st["straits"][0])
        self.assertIn("error", self.svc.ai_strait_status({"strait": "atlantis"}))
        self.assertEqual(self.svc.ai_dark_ships({})["count"], 0)


# ---------------------------------------------------------------------------------------------
class FakeNet:
    """Answers the two Digitraffic endpoints from the fixtures, with times shifted to now."""

    def __init__(self):
        self.calls = []
        now_ms = time.time() * 1000
        self.locations = load_fixture("digitraffic_locations.json")
        newest = max(f["properties"]["timestampExternal"] for f in self.locations["features"])
        for f in self.locations["features"]:
            f["properties"]["timestampExternal"] += now_ms - newest - 5000
        self.vessels = load_fixture("digitraffic_vessels.json")
        for v in self.vessels:
            v["timestamp"] = now_ms - 60000

    def set_host_interval(self, host, seconds):
        pass

    def get_json(self, url, params=None, headers=None, **kw):
        self.calls.append((url, params, headers))
        if url.endswith("/locations"):
            return copy.deepcopy(self.locations)
        if url.endswith("/vessels"):
            return copy.deepcopy(self.vessels)
        raise AssertionError(url)


class RegisteredModule(unittest.TestCase):
    """The real App, Store and registry with a fake network: every route, task and hook."""

    @classmethod
    def setUpClass(cls):
        from meridian.config import Config
        from meridian.datasets import DatasetManager
        from meridian.registry import App
        from meridian.scheduler import Scheduler
        from meridian.store import Store
        from meridian.modules import ships

        cls.tmp = tempfile.mkdtemp()
        cfg = Config(env_file=None, overrides={"MERIDIAN_DATA_DIR": cls.tmp, "AISSTREAM_API_KEY": "",
                                               "KYSTVERKET_AIS": "0", "AISSTREAM_AREA": ""})
        cls.store = Store(Path(cls.tmp) / "meridian.db")
        cls.net = FakeNet()
        log = logging.getLogger("meridian.test")
        cls.app = App(cfg, cls.store, cls.net, log, Scheduler(log), DatasetManager(log))
        cls.m = cls.app.module("ships")
        ships.register(cls.m)
        cls.svc = cls.m.service
        tasks = cls.app.scheduler.tasks
        tasks["ships.digitraffic"].fn()
        tasks["ships.snapshot"].fn()
        tasks["ships.analysis"].fn()
        tasks["ships.flush"].fn()
        tasks["ships.prune"].fn()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, **query):
        from meridian.registry import Request
        route, params, _ = self.app.match("GET", path)
        self.assertIsNotNone(route, path)
        req = Request("GET", path, params, {k: [str(v)] for k, v in query.items()}, {})
        return route.handler(req)

    def assertHTTPError(self, status, path, **query):
        from meridian.registry import HTTPError
        with self.assertRaises(HTTPError) as cm:
            self.get(path, **query)
        self.assertEqual(cm.exception.status, status)

    def test_declarations(self):
        for sid in ("ships.digitraffic", "ships.aisstream", "ships.kystverket"):
            self.assertIn(sid, self.app.sources)
        self.assertEqual(self.app.sources["ships.aisstream"]["needs_key"], "AISSTREAM_API_KEY")
        self.assertTrue(self.app.sources["ships.digitraffic"]["ok"])
        keys = self.app.config.declared
        self.assertTrue(keys["AISSTREAM_API_KEY"]["secret"])
        self.assertFalse(keys["AISSTREAM_AREA"]["secret"])
        self.assertFalse(keys["KYSTVERKET_AIS"]["secret"])
        layer_ids = {s["id"] for s in self.app.layer_specs}
        self.assertTrue({"ships", "ships.dark", "ships.sanctioned", "ships.straits"} <= layer_ids)
        tools = {t["name"] for t in self.app.ai_tools()}
        self.assertTrue({"ships_in_area", "vessel_details", "strait_status", "dark_ships"} <= tools)
        self.assertNotIn("ships.aisstream", self.app.scheduler.tasks)

    def test_digitraffic_headers_and_incremental_from(self):
        url, params, headers = self.net.calls[0]
        self.assertEqual(headers, {"Digitraffic-User": "open-data-map-client"})
        self.app.scheduler.tasks["ships.digitraffic"].fn()
        loc_calls = [c for c in self.net.calls if c[0].endswith("/locations")]
        self.assertIsNone(loc_calls[0][1])              # first poll: the default 24-hour window
        self.assertIn("from", loc_calls[-1][1])         # later polls: only what is new

    def test_live_route(self):
        out = self.get("/api/ships/live", bbox="15,57,33,66", zoom=6)
        self.assertGreaterEqual(out["in_view"], 4)
        self.assertEqual(out["fields"][0], "mmsi")
        self.assertHTTPError(400, "/api/ships/live", types="submarine")
        self.assertHTTPError(400, "/api/ships/live", bbox="1,2,3")

    def test_vessel_routes(self):
        d = self.get("/api/ships/vessel/230000001")
        self.assertTrue(d["known"])
        self.assertEqual(d["name"], "TEST CARGO")
        self.assertEqual(d["destination"], "KOTKA")
        self.assertHTTPError(400, "/api/ships/vessel/12345")
        tr = self.get("/api/ships/trail/230000001", hours=24)
        self.assertGreaterEqual(tr["count"], 1)
        self.assertHTTPError(400, "/api/ships/trail/230000001", hours=99)
        # nine "²" (or Arabic-Indic digits) pass str.isdigit() but int() rejects them: 400, not a 500
        for raw in ("²" * 9, "١٢٣٤٥٦٧٨٩"):
            self.assertHTTPError(400, f"/api/ships/vessel/{raw}")
            self.assertHTTPError(400, f"/api/ships/trail/{raw}")
        self.assertIn("error", self.app.call_ai_tool("vessel_details", {"mmsi": "²" * 9}))
        search = next(fn for mod, fn in self.app._search if mod == "ships")
        self.assertEqual(search("²" * 9, 5), [])

    def test_strait_routes(self):
        all_ = self.get("/api/ships/straits")
        self.assertEqual(len(all_["straits"]), 14)
        self.assertIn("geometry", all_["straits"][0])
        gof = self.get("/api/ships/straits/gulf_of_finland")
        self.assertTrue(gof["coverage"])
        self.assertIn("vessels", gof)
        self.assertHTTPError(404, "/api/ships/straits/atlantis")

    def test_port_activity_contract(self):
        pa = self.get("/api/ships/port-activity", lat=60.158, lon=24.955, radius_km=5)
        for key in ("moored", "anchored", "underway", "arrivals_24h", "departures_24h", "vessels", "coverage"):
            self.assertIn(key, pa)
        self.assertIsInstance(pa["coverage"], str)
        self.assertEqual(pa["moored"], 1)
        self.assertHTTPError(400, "/api/ships/port-activity", lat=95, lon=0)

    def test_lists_and_status(self):
        self.assertIn("ports", self.get("/api/ships/ports"))
        self.assertIn("dark", self.get("/api/ships/dark"))
        self.assertIn("vessels", self.get("/api/ships/sanctioned"))
        st = self.get("/api/ships/status")
        self.assertEqual(st["sources"]["ships.digitraffic"]["ok"], True)
        self.assertEqual(len(st["classes"]), 9)

    def test_search_and_events_through_the_app(self):
        hits = self.app.run_search("TEST CARGO", 10)
        self.assertTrue(any(h["kind"] == "ship" and h["id"] == "230000001" for h in hits))
        near = self.app.run_nearby(60.158, 24.955, 3, 10)
        self.assertTrue(any(h["id"] == "230000002" for h in near))
        self.app.collect_events(time.time() - 86400)        # must not raise
        r = self.app.call_ai_tool("ships_in_area", {"lat": 60.158, "lon": 24.955, "radius_km": 5})
        self.assertGreaterEqual(r["count"], 1)

    def test_persisted(self):
        self.assertGreaterEqual(self.store.scalar("SELECT COUNT(*) FROM ships_vessels"), 6)


if __name__ == "__main__":
    unittest.main()
