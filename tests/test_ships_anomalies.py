"""Maritime anomaly detectors on synthetic tracks (meridian/modules/ships/anomalies.py), with the benign
patterns each must ignore, plus the land index and the Phase 3b follow-ups (sanctions keys, list wording,
re-matching on sanctions.version(), history routes that do not mark the module active). No network."""

import logging
import math
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from meridian import geo
from meridian.modules.ships import analysis
from meridian.modules.ships.anomalies import (AnomalyEngine, cell_key, fit_circle, gap_reading, gnss_signatures,
                                              loiter_metrics)
from meridian.modules.ships.aoi import AreasOfInterest, usual_area
from meridian.modules.ships.land import LandIndex
from meridian.modules.ships.service import ShipsService
from meridian.modules.ships.store import VesselStore
from meridian.modules.ships.straits import StraitMonitor

T0 = 1795380000.0          # 2026-11-22 20:40 UTC
LOG = logging.getLogger("test.ships.anomalies")
OPEN = (45.5, -6.0)        # Bay of Biscay, far from any test port
BANNED = ("illegal", "smuggl", "evasion", "confirmed", "suspicious", "attack")
A, B = 636000001, 538000002


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def fake_lookup(imo=None, mmsi=None, name=None):
    if imo == "9000003":
        return {"match": "imo", "uid": "1", "name": "LISTED TANKER", "program": "RUSSIA-EO14024] [UK: Russia",
                "lists": ["OFAC", "UK"], "programmes": {"OFAC": ["RUSSIA-EO14024"], "UK": ["Russia"]},
                "listings": [{"list": "OFAC", "id": "1"}, {"list": "UK", "id": "RUS0001"}], "listed_on": "2024-05-09",
                "imo": "9000003"}
    if imo == "9000039":
        return {"match": "imo", "uid": "uk-RUS2176", "name": "UK ONLY", "program": "UK: Russia", "lists": ["UK"],
                "programmes": {"UK": ["Russia"]}, "listings": [{"list": "UK", "id": "RUS2176"}], "imo": "9000039"}
    return None


def ports_grid(*ports):
    grid = geo.GridIndex(0.5)
    for p in ports:
        grid.add(p["lat"], p["lon"], p)
    return grid


def engine(clock, *, db=None, ports=None, land=None, lookup=fake_lookup):
    store = VesselStore(db, now=clock, lookup=lookup, straits=StraitMonitor())
    if db is not None:
        store.create_tables()
    eng = AnomalyEngine(store, db=db, ports=lambda: ports, land=land, now=clock, log=LOG)
    if db is not None:
        eng.create_tables()
    return eng, store


def texts(item) -> str:
    parts = [item.get("title"), item.get("subtitle"), item.get("observed"), item.get("coverage"), item.get("limits"),
             item.get("confidence_text")]
    parts += [" ".join(str(x) for x in row) for row in item.get("evidence") or []]
    parts += item.get("innocent") or []
    return " ".join(str(p) for p in parts if p).lower()


def square(lat0, lon0, lat1, lon1):
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {
        "type": "Polygon", "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}}]}


# =============================================================================================
# Ship-to-ship transfer candidates
# =============================================================================================
class ShipToShip(unittest.TestCase):
    def pair(self, minutes=40, *, at=OPEN, sep_km=0.2, a=(80, 180.0), b=(80, 120.0), b_imo=None, sog=(0.2, 0.3),
             ports=None, land=None, extra=None, before=None, ais=None, db=None):
        clock = Clock(T0)
        eng, store = engine(clock, ports=ports, land=land, db=db)
        store.static(A, T0 - 600, ship_type=a[0], length=a[1], name="TANKER ONE", draught=8.0)
        store.static(B, T0 - 600, ship_type=b[0], length=b[1], name="TANKER TWO", imo=b_imo, draught=6.0)
        if before:
            before(store, clock)
        blat, blon = geo.destination(at[0], at[1], 90, sep_km)
        for i in range(minutes + 1):
            clock.t = T0 + i * 60
            store.position(A, clock.t - 5, at[0], at[1], sog=sog[0], cog=0.0, nav=0, ais=ais)
            store.position(B, clock.t - 5, blat, blon, sog=sog[1], cog=0.0, nav=0, ais=ais)
            if extra:
                extra(store, clock.t)
            eng.run(store.build_snapshot(), {}, now=clock.t)
        return eng, store, clock

    def sts(self, eng):
        return [i for i in eng.items if i["kind"] == "sts"]

    def test_open_water_pair_is_a_candidate(self):
        eng, store, clock = self.pair(40)
        items = self.sts(eng)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertTrue(it["published"])
        self.assertEqual(it["state"], "candidate")
        self.assertEqual(it["category"], "sts_candidate")
        self.assertEqual(it["severity"], 3)                  # under 2 hours, nothing else
        self.assertEqual(it["confidence"], "medium")         # within 300 m: probably alongside
        self.assertIn("probably alongside", it["observed"])
        self.assertIn("consistent with a ship-to-ship transfer", it["observed"])
        self.assertEqual([v["id"] for v in it["vessels"]], [str(A), str(B)])     # the larger tanker first
        self.assertTrue(it["innocent"] and it["coverage"] and it["limits"])
        for word in BANNED:
            self.assertNotIn(word, texts(it))
        ev = [e for e in eng.events(T0) if e["category"] == "sts_candidate"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["kind"], ev[0]["id"], ev[0]["layer"]), ("ship", str(A), "ships.anomalies"))
        self.assertEqual(ev[0]["first_time"], int(T0 - 5))
        self.assertIn(it["id"], [x["id"] for x in eng.for_vessel(B)])

    def test_sanctioned_vessel_adds_severity_and_badge(self):
        eng, *_ = self.pair(40, b_imo=9000003)
        it = self.sts(eng)[0]
        self.assertEqual(it["severity"], 4)
        listed = next(v for v in it["vessels"] if v["id"] == str(B))
        self.assertEqual(listed["sanctions"]["strength"], "listed")
        self.assertEqual(listed["sanctions"]["lists"], ["OFAC", "UK"])
        self.assertIn("sanctioned: OFAC, UK", listed["subtitle"])
        self.assertIn("OFAC SDN list and UK Sanctions List", texts(it).replace("ofac sdn list", "OFAC SDN list")
                      .replace("uk sanctions list", "UK Sanctions List"))

    def test_long_episode_and_draught_change(self):
        def load(store, t):
            if t >= T0 + 100 * 60:
                store.static(B, t, draught=8.4)              # 2.4 m deeper: cargo came aboard
        eng, *_ = self.pair(130, extra=load)
        it = self.sts(eng)[0]
        self.assertEqual(it["confidence"], "high")
        self.assertEqual(it["severity"], 5)                  # 4 for two hours or more, +1 draught
        self.assertTrue(any(r[0].startswith("Draught") and "6.0 m to 8.4 m" in r[1] for r in it["evidence"]))

    def test_too_short(self):
        eng, *_ = self.pair(20)
        self.assertEqual(self.sts(eng), [])

    def test_port_is_excluded(self):
        port = {"id": "locode:XXTST", "name": "Testport", "lat": OPEN[0] + 0.05, "lon": OPEN[1], "size": "medium"}
        eng, *_ = self.pair(40, ports=ports_grid(port))
        self.assertEqual(self.sts(eng), [])
        self.assertGreater(eng.excluded["sts"]["port"], 0)

    def test_crowded_anchorage_is_excluded(self):
        def crowd(store, t):
            for i in range(6):
                lat, lon = geo.destination(OPEN[0], OPEN[1], i * 60, 1.5)
                store.position(230100000 + i, t - 10, lat, lon, sog=0.1, nav=1)
        eng, *_ = self.pair(40, extra=crowd)
        self.assertEqual(self.sts(eng), [])
        self.assertGreater(eng.excluded["sts"]["anchorage"], 0)

    def test_builtin_waiting_area_off_ust_luga_is_excluded(self):
        self.assertEqual(usual_area(59.95, 28.60)["id"], "gof_east_waiting")
        eng, *_ = self.pair(40, at=(59.95, 28.60))
        # inside an area of interest (the eastern Gulf of Finland) the pair is kept, unflagged, with its reason
        items = self.sts(eng)
        self.assertEqual([(i["state"], i["published"], i["severity"]) for i in items], [("usual", False, 2)])
        self.assertIn("not flagged", items[0]["innocent"][0])
        self.assertEqual(eng.query(), [])
        self.assertGreater(eng.excluded["sts"]["anchorage"], 0)
        self.assertIn("does not mean no transfers", eng.status()["sts"]["excluded_waters"])

    def test_transfer_anchorage_in_an_area_of_interest_is_visible_and_sanctions_are_not_cleared(self):
        at = (36.50, 22.80)                                    # the Laconian Gulf
        self.assertEqual(usual_area(*at)["id"], "gr_laconian")
        eng, *_ = self.pair(40, at=at)
        it = self.sts(eng)[0]
        self.assertEqual((it["state"], it["published"]), ("usual", False))
        self.assertIn("Laconian Gulf transfer anchorage", " ".join(it["innocent"]))
        self.assertEqual(len(eng.query(include_all=True, kinds={"sts"})), 1)
        # an OFAC/UK-listed tanker is not cleared by a usual anchorage
        eng, *_ = self.pair(40, at=at, b_imo=9000003)
        it = self.sts(eng)[0]
        self.assertEqual((it["state"], it["published"]), ("candidate", True))

    def test_learned_anchorage_is_excluded(self):
        def learn(store, clock):
            pass
        clock = Clock(T0)
        eng, store = engine(clock)
        eng.habitual_stop = {cell_key(OPEN[0], OPEN[1], 0.05)}
        self.assertTrue(eng._learned_stop(OPEN[0] + 0.03, OPEN[1]))          # a neighbouring cell counts too
        self.assertFalse(eng._learned_stop(OPEN[0] + 0.5, OPEN[1]))

    def test_offshore_installation_is_excluded(self):
        def fso(store, t):
            if t == T0:
                store.position(230200000, T0 - 30, OPEN[0] + 0.01, OPEN[1], sog=0.0, nav=5)
                store.static(230200000, T0, ship_type=89, length=280.0, name="FLOATING STORAGE")
            v = store.vessels[230200000]
            v.state, v.state_since = "stationary", T0 - 30 * 86400
            store.position(230200000, t - 2, OPEN[0] + 0.01, OPEN[1], sog=0.0, nav=5)
        eng, *_ = self.pair(40, extra=fso)
        self.assertEqual(self.sts(eng), [])
        self.assertGreater(eng.excluded["sts"]["offshore"], 0)

    def test_pilot_area_is_excluded(self):
        def pilot(store, t):
            if t == T0:
                store.static(230300000, T0, ship_type=50, length=20.0, name="PILOT")
            store.position(230300000, t - 3, OPEN[0] + 0.015, OPEN[1], sog=4.0, cog=10.0)
        eng, *_ = self.pair(40, extra=pilot)
        self.assertEqual(self.sts(eng), [])
        self.assertGreater(eng.excluded["sts"]["pilot"], 0)

    def test_inshore_is_excluded(self):
        land = LandIndex(geojson=square(OPEN[0] + 0.004, OPEN[1] - 1, OPEN[0] + 1, OPEN[1] + 1))   # coast 400 m away
        eng, *_ = self.pair(40, land=land)
        self.assertEqual(self.sts(eng), [])
        self.assertGreater(eng.excluded["sts"]["coast"], 0)
        far = LandIndex(geojson=square(OPEN[0] + 0.5, OPEN[1] - 1, OPEN[0] + 1, OPEN[1] + 1))       # 55 km away
        eng, *_ = self.pair(40, land=far)
        it = self.sts(eng)[0]
        self.assertIn("km offshore", it["subtitle"])

    def test_bunkering_is_labelled_not_flagged(self):
        eng, *_ = self.pair(40, a=(70, 200.0), b=(80, 70.0))
        it = self.sts(eng)[0]
        self.assertEqual(it["state"], "possible_bunkering")
        self.assertFalse(it["published"])
        self.assertEqual(it["severity"], 2)
        self.assertEqual(eng.events(T0), [])
        self.assertEqual(eng.query(), [])
        self.assertEqual(len(eng.query(include_all=True)), 1)
        self.assertIn("may be bunkering", it["innocent"][0])
        self.assertNotIn("most likely", " ".join(it["innocent"]))

    def test_small_tanker_does_not_clear_sanctions_or_areas_of_interest(self):
        # a 75 m tanker on the lists, alongside a 180 m tanker: a candidate, not "bunkering"
        eng, *_ = self.pair(40, a=(80, 180.0), b=(80, 75.0), b_imo=9000003)
        it = self.sts(eng)[0]
        self.assertEqual((it["state"], it["published"], it["title"]), ("candidate", True, "Possible ship-to-ship transfer"))
        self.assertGreaterEqual(it["severity"], 4)
        self.assertIn("That alone does not clear it here", it["innocent"][0])
        # the same small tanker, unlisted, in an area of interest in open water (Yellow Sea west of Nampo)
        eng, *_ = self.pair(40, at=(38.6, 124.4), a=(80, 180.0), b=(80, 75.0))
        it = self.sts(eng)[0]
        self.assertTrue(it["aoi"])
        self.assertEqual((it["state"], it["published"]), ("candidate", True))

    def test_classes_speed_and_transponders_that_never_pair(self):
        for kw in ({"a": (30, 60.0)}, {"b": (60, 150.0)}, {"a": (52, 30.0)}, {"b": (37, 20.0)}, {"a": (35, 150.0)},
                   {"sog": (3.0, 0.3)}, {"ais": "B"}, {"a": (70, 180.0), "b": (70, 150.0)},
                   {"b": (90, 80.0)}):
            eng, *_ = self.pair(40, **kw)
            self.assertEqual([i for i in self.sts(eng) if i["published"]], [], kw)

    def test_suspect_positions_are_excluded(self):
        def spoofed(store, clock):
            store.position(A, T0 - 3000, OPEN[0], OPEN[1], sog=60.0, cog=0.0)       # a 60 kn fix 50 min ago
        eng, *_ = self.pair(40, before=spoofed)
        self.assertEqual(self.sts(eng), [])
        self.assertGreater(eng.excluded["sts"]["suspect positions"], 0)

    def test_episode_tolerates_a_quiet_vessel_and_closes_when_they_part(self):
        clock = Clock(T0)
        eng, store = engine(clock)
        store.static(A, T0 - 600, ship_type=80, length=180.0, name="TANKER ONE")
        store.static(B, T0 - 600, ship_type=80, length=120.0, name="TANKER TWO")
        blat, blon = geo.destination(OPEN[0], OPEN[1], 90, 0.25)
        for i in range(61):
            clock.t = T0 + i * 60
            store.position(A, clock.t - 5, OPEN[0], OPEN[1], sog=0.2, nav=0)
            if not 20 <= i < 40:                             # B not heard for 20 minutes
                store.position(B, clock.t - 5, blat, blon, sog=0.2, nav=0)
            eng.run(store.build_snapshot(), {}, now=clock.t)
        it = [i for i in eng.items if i["kind"] == "sts"]
        self.assertEqual(len(it), 1)
        self.assertGreaterEqual(it[0]["duration_s"], 59 * 60)
        self.assertTrue(any(r[0] == "Went quiet" for r in it[0]["evidence"]))
        self.assertEqual(it[0]["severity"], 4)               # +1: one vessel went quiet during the episode
        # B sails away: the episode closes at once
        for i in range(61, 64):
            clock.t = T0 + i * 60
            store.position(A, clock.t - 5, OPEN[0], OPEN[1], sog=0.2, nav=0)
            far = geo.destination(OPEN[0], OPEN[1], 90, 0.25 + (i - 60) * 1.0)
            store.position(B, clock.t - 5, far[0], far[1], sog=9.0, cog=90.0, nav=0)
            eng.run(store.build_snapshot(), {}, now=clock.t)
        ep = next(iter(eng.sts.values()))
        self.assertEqual(ep["state"], "closed")
        self.assertFalse(next(i for i in eng.items if i["kind"] == "sts")["ongoing"])

    def test_episodes_survive_a_restart(self):
        from meridian.store import Store
        tmp = tempfile.mkdtemp()
        try:
            db = Store(Path(tmp) / "t.db")
            eng, store, clock = self.pair(40, db=db)
            aid = self.sts(eng)[0]["id"]
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM ships_sts"), 1)
            eng2 = AnomalyEngine(store, db=db, ports=lambda: None, now=clock, log=LOG)
            self.assertGreaterEqual(eng2.restore(), 1)
            self.assertIn(aid, eng2.by_id)
            self.assertEqual(eng2.sts[(min(A, B), max(A, B))]["state"], "open")
            self.assertEqual(eng2.get(aid)["id"], aid)
            self.assertIsNotNone(eng2.get(aid))
            db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# =============================================================================================
# Loitering
# =============================================================================================
def circle_track(store, mmsi, center, *, hours=8.0, radius_km=2.0, kn=1.5, step_s=300, t_end=T0):
    """A vessel circling ``center`` at ``kn`` for ``hours`` (reports every ``step_s``)."""
    n = int(hours * 3600 / step_s)
    circumference = 2 * math.pi * radius_km
    for i in range(n + 1):
        t = t_end - (n - i) * step_s
        dist = kn * geo.KM_PER_NM * (t - (t_end - n * step_s)) / 3600.0
        ang = (dist / circumference) * 360.0
        lat, lon = geo.destination(center[0], center[1], ang, radius_km)
        store.position(mmsi, t, lat, lon, sog=kn, cog=(ang + 90) % 360, nav=0)


class Loitering(unittest.TestCase):
    M = 636000100

    def run_loiter(self, *, at=OPEN, ship_type=80, length=180.0, dest="", learned=True, ports=None, setup=None,
                   hours=8.0, kn=1.5):
        clock = Clock(T0)
        eng, store = engine(clock, ports=ports)
        store.static(self.M, T0 - 9 * 3600, ship_type=ship_type, length=length, name="DRIFTER", dest=dest)
        circle_track(store, self.M, at, hours=hours, kn=kn)
        if learned:
            day = int(T0 // 86400)
            eng.days["loitering"] = {str(day - i): 100 for i in range(7)}
        if setup:
            setup(eng, store)
        eng.run(store.build_snapshot(), {}, now=T0)
        return eng, [i for i in eng.items if i["kind"] == "loitering"]

    def test_metrics_shapes(self):
        clock = Clock(T0)
        _eng, store = engine(clock)
        circle_track(store, self.M, OPEN)
        m = loiter_metrics(store.trail(self.M, T0 - 12 * 3600))
        self.assertIsNotNone(m)
        self.assertGreaterEqual(m["points"], 8)
        self.assertLess(m["rg_km"], 3.0)
        self.assertGreater(m["path_km"], 3 * m["net_km"])
        straight = [(T0 + i * 600, OPEN[0] + i * 0.02, OPEN[1], 3.0, 0.0) for i in range(50)]
        self.assertIsNone(loiter_metrics(straight))                     # passing through
        anchored = [(T0 + i * 600, OPEN[0] + (i % 2) * 0.001, OPEN[1], 0.1, 0.0) for i in range(50)]
        self.assertIsNone(loiter_metrics(anchored))                     # swinging at anchor
        short = [(T0 + i * 300, *geo.destination(OPEN[0], OPEN[1], i * 30, 2.0), 1.5, 0.0) for i in range(30)]
        self.assertIsNone(loiter_metrics(short))                         # 2.5 hours only

    def test_open_water_loiterer_is_a_candidate(self):
        eng, items = self.run_loiter()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it["state"], it["published"], it["severity"]), ("candidate", True, 3))
        self.assertIn("has stayed within", it["observed"])
        self.assertIn("drifting or circling", it["observed"])
        for word in BANNED:
            self.assertNotIn(word, texts(it))
        ev = [e for e in eng.events(T0 - 86400) if e["category"] == "loitering"]
        self.assertEqual(ev[0]["id"], str(self.M))

    def test_says_learning_until_seven_days(self):
        eng, items = self.run_loiter(learned=False)
        it = items[0]
        self.assertEqual((it["state"], it["published"]), ("learning", False))
        self.assertIn("Learning usual waiting areas: 0 of 7 days", it["confidence_text"])
        self.assertEqual(eng.status()["loitering"]["state"], "learning")
        self.assertEqual(eng.events(T0 - 86400), [])

    def test_tankers_waiting_off_primorsk_and_ust_luga_never_flag(self):
        for dest, at in (("RUULU", (59.95, 28.60)), ("", (59.93, 28.65)), ("PRIMORSK", (60.30, 28.70))):
            eng, items = self.run_loiter(at=at, dest=dest)
            self.assertEqual(len(items), 1, at)
            self.assertEqual(items[0]["state"], "usual", at)
            self.assertFalse(items[0]["published"], at)
            self.assertTrue(items[0]["usual"], at)
            self.assertEqual(eng.events(T0 - 86400), [], at)

    def test_waiting_near_its_destination(self):
        eng, items = self.run_loiter(at=(38.0, -9.3), dest="SINES")         # 38 km off Sines (built-in port)
        self.assertEqual(items[0]["state"], "usual")
        self.assertIn("Sines as its destination", items[0]["usual"])
        # 110 km out is not waiting off the port
        eng, items = self.run_loiter(at=(38.5, -10.0), dest="SINES")
        self.assertEqual(items[0]["state"], "candidate")
        self.assertIsNone(eng.dest_port("SINES", 38.5, -10.0))

    def test_destination_does_not_clear_in_areas_of_interest_or_with_sanctions(self):
        at = (35.25, 35.75)                                                  # off Baniyas, an area of interest
        port = {"id": "locode:SYTTS", "name": "Tartus", "lat": 35.10, "lon": 35.75, "kind": "port"}
        eng, items = self.run_loiter(at=at, dest="TARTUS", ports=ports_grid(port))
        self.assertIsNotNone(eng.dest_port("TARTUS", *at))
        it = items[0]
        self.assertEqual(it["state"], "candidate")
        self.assertTrue(it["published"])
        self.assertIn("does not clear the vessel here", it["innocent"][0])
        # outside an area of interest the same destination clears an unlisted vessel, but not a listed one
        y = geo.destination(OPEN[0], OPEN[1], 180, 20.0)
        home = {"id": "locode:XXHOM", "name": "Homeport", "lat": y[0], "lon": y[1], "kind": "port"}
        _e, items = self.run_loiter(dest="HOMEPORT", ports=ports_grid(home))
        self.assertEqual(items[0]["state"], "usual")

        def listed(eng, store):
            store.static(self.M, T0, imo=9000003)
        _e, items = self.run_loiter(dest="HOMEPORT", ports=ports_grid(home), setup=listed)
        self.assertEqual(items[0]["state"], "candidate")
        self.assertIn("does not clear the vessel here", items[0]["innocent"][0])
        # a port more than 25 km further than the nearest listed port is not the one it is waiting off
        x = geo.destination(OPEN[0], OPEN[1], 0, 45.0)
        y = geo.destination(OPEN[0], OPEN[1], 180, 5.0)
        far = {"id": "locode:XXFAR", "name": "Farport", "lat": x[0], "lon": x[1], "kind": "port"}
        near = {"id": "locode:XXNEA", "name": "Nearport", "lat": y[0], "lon": y[1], "kind": "port"}
        eng2, _ = self.run_loiter(ports=ports_grid(near, far))
        self.assertIsNone(eng2.dest_port("FARPORT", *OPEN))
        self.assertEqual(eng2.dest_port("NEARPORT", *OPEN)[0], "Nearport")

    def test_learned_waiting_area(self):
        def learned(eng, store):
            eng.habitual_loiter = {cell_key(OPEN[0], OPEN[1], 0.1): (4, 6)}
        eng, items = self.run_loiter(setup=learned)
        self.assertEqual(items[0]["state"], "usual")
        self.assertIn("4 vessels do this on 6 of the last 14 days", items[0]["usual"])

    def test_excluded_classes_and_ports(self):
        for kw in ({"ship_type": 30, "length": 60.0}, {"ship_type": 60, "length": 150.0},
                   {"ship_type": 35, "length": 120.0}, {"ship_type": 52, "length": 30.0},
                   {"ship_type": 70, "length": 40.0}, {"kn": 0.2}):
            _eng, items = self.run_loiter(**kw)
            self.assertEqual(items, [], kw)
        port = {"id": "locode:XXTST", "name": "Testport", "lat": OPEN[0], "lon": OPEN[1] + 0.05, "size": "large"}
        _eng, items = self.run_loiter(ports=ports_grid(port))
        self.assertEqual(items, [])

    def test_area_of_interest_adds_severity(self):
        eng, items = self.run_loiter(at=(35.25, 35.60))          # off Baniyas, outside any usual area
        it = items[0]
        self.assertEqual(it["aoi"]["id"], "baniyas")
        self.assertEqual(it["severity"], 4)
        self.assertIn("Sanctions Risks Related to Petroleum Shipments involving Iran and Syria", it["aoi"]["basis"])


# =============================================================================================
# AIS gap episodes
# =============================================================================================
def regular(store, mmsi, t_end, lat, lon, *, sog=12.0, cog=90.0, n=10, step=120):
    for i in range(n):
        t = t_end - (n - 1 - i) * step
        back = sog * geo.KM_PER_NM * (t_end - t) / 3600.0
        plat, plon = geo.destination(lat, lon, (cog + 180) % 360, back)
        store.position(mmsi, t, plat, plon, sog=sog, cog=cog, nav=0, src=1)


class Gaps(unittest.TestCase):
    M = 636000200

    def dark_then_back(self, *, at=(35.25, 35.75), back_km=6.0, draught_after=10.3, sog=12.0, sanctioned=False):
        clock = Clock(T0)
        eng, store = engine(clock)
        regular(store, self.M, T0, at[0], at[1], sog=sog)
        store.static(self.M, T0, name="QUIET TANKER", ship_type=80, length=220.0, imo=9000003 if sanctioned else 9000065,
                     draught=8.2, dest="PORT SAID")
        clock.t = T0 + 9.5 * 3600
        est = geo.destination(at[0], at[1], 90, min(sog * geo.KM_PER_NM * 3, 300))
        for i in range(4):
            store.position(230000100 + i, clock.t - 60, at[0] + 0.02 * i, at[1] + 0.05, sog=8.0, cog=270)
        for i in range(3):
            store.position(230000200 + i, clock.t - 60, est[0] - 0.02 * i, est[1], sog=9.0, cog=270)
        snap = store.build_snapshot()
        dark = analysis.find_dark(store, snap, clock.t, {1: (T0 - 30000, clock.t - 30)})
        self.assertEqual([d["mmsi"] for d in dark], [str(self.M)])
        self.assertEqual(store.vessels[self.M].gap_ctx["n_last"], 4)
        back = geo.destination(at[0], at[1], 45, back_km)
        store.position(self.M, clock.t, back[0], back[1], sog=11.0, cog=90.0, nav=0, src=1)
        store.static(self.M, clock.t + 60, draught=draught_after)
        clock.t += 120
        eng.run(store.build_snapshot(), {}, now=clock.t)
        return eng, store, clock

    def gap(self, eng):
        return [i for i in eng.items if i["kind"] == "ais_gap"]

    def test_reading(self):
        self.assertEqual(gap_reading(5, 9.5, 12.0, False)[1], "stopped or stayed near the same place during the gap")
        r, text = gap_reading(12 * 1.852 * 3, 3, 12.0, False)
        self.assertAlmostEqual(r, 1.0, places=3)
        self.assertEqual(text, "sailed on through a coverage hole")
        self.assertEqual(gap_reading(1.0, 4, 0.2, True)[1], "stayed in the same place")
        self.assertEqual(gap_reading(500, 1, 10.0, False)[0], 2.0)              # capped

    def test_gap_in_an_area_of_interest_stopping_and_loading(self):
        eng, store, clock = self.dark_then_back()
        items = self.gap(eng)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertTrue(it["published"])
        self.assertEqual(it["aoi"]["id"], "baniyas")
        self.assertEqual(it["severity"], 6)                                 # 3, +1 area, +1 stopped, +1 draught
        self.assertIn("draught 2.1 m deeper", it["subtitle"])
        self.assertIn("4 vessels nearby kept reporting", it["observed"])
        self.assertIn("stopped or stayed near the same place", it["observed"])
        self.assertTrue(it["innocent"])
        for word in BANNED:
            self.assertNotIn(word, texts(it))
        ev = [e for e in eng.events(T0) if e["category"] == "ais_gap"]
        self.assertEqual(ev[0]["id"], str(self.M))

    def test_gap_elsewhere_is_kept_but_not_flagged(self):
        eng, *_ = self.dark_then_back(at=OPEN)
        it = self.gap(eng)[0]
        self.assertEqual((it["state"], it["published"]), ("outside_areas", False))
        self.assertEqual(eng.events(T0), [])

    def test_sailed_on_through_a_coverage_hole(self):
        eng, *_ = self.dark_then_back(back_km=12 * 1.852 * 9.5, draught_after=8.2)
        it = self.gap(eng)[0]
        self.assertEqual(it["reading"], "sailed on through a coverage hole")
        # Reappearing where its speed puts it, with coverage along its path not proven, is what a hole between
        # receivers looks like: kept, but low confidence and not flagged.
        self.assertEqual((it["state"], it["published"], it["confidence"]), ("coverage_hole", False, "low"))
        self.assertLessEqual(it["severity"], 2)
        self.assertNotIn("unlikely to be a receiver problem", it["confidence_text"])
        self.assertEqual([e for e in eng.events(T0) if e["category"] == "ais_gap"], [])
        # with coverage proven along its path the same gap is flagged
        ep = next(iter(eng.gaps.values()))
        ep["coverage"] = {"checks": 8, "covered": 8, "vessels": 6}
        ep["ctx"]["n_last"] = 5
        it = eng._gap_item(ep, eng._infos({ep["mmsi"]}, eng.now()), eng.now())
        self.assertEqual((it["state"], it["published"], it["confidence"], it["severity"]), ("candidate", True, "high", 4))

    def test_coverage_is_checked_along_the_path_not_only_at_the_start(self):
        from meridian.store import Store
        tmp = tempfile.mkdtemp()
        try:
            db = Store(Path(tmp) / "t.db")
            eng, _store = engine(Clock(T0), db=db)
            start = (36.0, -6.2)                                      # just west of the Strait of Gibraltar
            end = geo.destination(start[0], start[1], 270, 211.0)
            rows = [(230000500 + k, int(T0 + i * 600), int((start[0] + 0.02 * k) * 1e5), int(start[1] * 1e5), 80, 900)
                    for i in range(60) for k in range(5)]
            db.executemany("INSERT INTO ships_track(mmsi, t, lat, lon, sog, cog) VALUES(?,?,?,?,?,?)", rows)
            g = {"mmsi": self.M, "t0": T0, "t1": T0 + 9.5 * 3600, "lat0": start[0], "lon0": start[1],
                 "lat1": end[0], "lon1": end[1]}
            cov = eng._gap_coverage(g)
            self.assertEqual(cov["checks"], 8)
            self.assertLessEqual(cov["covered"], 2)                   # only the first slices are near the start
            # vessels heard along the line between the two ends prove it
            line = []
            for i in range(60):
                frac = (i * 600 + 300) / (9.5 * 3600)
                lat, lon = start[0] + frac * (end[0] - start[0]), start[1] + frac * (end[1] - start[1])
                line.append((230000600, int(T0 + i * 600), int(lat * 1e5), int(lon * 1e5), 90, 900))
            db.executemany("INSERT INTO ships_track(mmsi, t, lat, lon, sog, cog) VALUES(?,?,?,?,?,?)", line)
            self.assertEqual(eng._gap_coverage(g)["covered"], 8)
            db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rejected_jumps_mean_still_transmitting_not_dark(self):
        clock = Clock(T0)
        _eng, store = engine(clock)
        regular(store, self.M, T0, OPEN[0], OPEN[1])
        store.static(self.M, T0, name="JUMPY", ship_type=70, length=150.0, imo=9000065)
        clock.t = T0 + 300
        self.assertFalse(store.position(self.M, T0 + 300, OPEN[0] + 0.5, OPEN[1], sog=12.0, cog=90.0, src=1))
        self.assertGreater(store.vessels[self.M].reject_t, store.vessels[self.M].t)
        clock.t = T0 + 3 * 3600
        for i in range(4):
            store.position(230000100 + i, clock.t - 60, OPEN[0] + 0.02 * i, OPEN[1] + 0.05, sog=8.0, cog=270)
        self.assertEqual(analysis.find_dark(store, store.build_snapshot(), clock.t, {1: (T0 - 30000, clock.t)}), [])

    def test_stopped_vessel_going_quiet_in_an_area_of_interest(self):
        clock = Clock(T0)
        eng, store = engine(clock)
        at = (35.25, 35.70)
        for i in range(10):
            store.position(self.M, T0 - (9 - i) * 150, at[0], at[1], sog=0.1, nav=0, src=1)
        store.static(self.M, T0, name="STILL TANKER", ship_type=80, length=200.0, imo=9000065)
        clock.t = T0 + 4 * 3600
        for i in range(4):
            store.position(230000300 + i, clock.t - 60, at[0] + 0.03 * i, at[1] + 0.04, sog=7.0, cog=0)
        receiving = {1: (T0 - 30000, clock.t - 30)}
        eng.run(store.build_snapshot(), receiving, now=clock.t)
        it = self.gap(eng)
        self.assertEqual(len(it), 1)
        self.assertTrue(it[0]["ongoing"] and it[0]["published"])
        self.assertIn("stopped and silent", it[0]["subtitle"])
        self.assertEqual(store.vessels[self.M].quiet_since, T0)
        # it comes back where it was: a closed episode that stayed in place
        clock.t += 60
        store.position(self.M, clock.t, at[0] + 0.001, at[1], sog=0.1, nav=0, src=1)
        clock.t += 120
        eng.run(store.build_snapshot(), receiving, now=clock.t)
        it = self.gap(eng)
        self.assertEqual(len(it), 1)
        self.assertFalse(it[0]["ongoing"])
        self.assertEqual(it[0]["reading"], "stayed in the same place")
        # the same silence outside any area of interest is not watched
        clock2 = Clock(T0)
        eng2, store2 = engine(clock2)
        for i in range(10):
            store2.position(self.M, T0 - (9 - i) * 150, OPEN[0], OPEN[1], sog=0.1, nav=0, src=1)
        store2.static(self.M, T0, ship_type=80, length=200.0, imo=9000065)
        clock2.t = T0 + 4 * 3600
        for i in range(4):
            store2.position(230000300 + i, clock2.t - 60, OPEN[0] + 0.03 * i, OPEN[1] + 0.04, sog=7.0, cog=0)
        eng2.run(store2.build_snapshot(), receiving, now=clock2.t)
        self.assertEqual(self.gap(eng2), [])
        self.assertIsNone(store2.vessels[self.M].quiet_since)

    def test_coverage_is_proven_from_the_stored_track(self):
        from meridian.store import Store
        tmp = tempfile.mkdtemp()
        try:
            db = Store(Path(tmp) / "t.db")
            clock = Clock(T0)
            eng, store = engine(clock, db=db)
            at = (35.25, 35.75)
            rows = [(230000400 + k, int(T0 + i * 1200), int((at[0] + 0.05 * k) * 1e5), int(at[1] * 1e5), 80, 900)
                    for i in range(30) for k in range(3)]
            db.executemany("INSERT INTO ships_track(mmsi, t, lat, lon, sog, cog) VALUES(?,?,?,?,?,?)", rows)
            cov = eng._gap_coverage({"mmsi": self.M, "t0": T0, "t1": T0 + 9 * 3600, "lat0": at[0], "lon0": at[1]})
            self.assertEqual(cov, {"checks": 8, "covered": 8, "vessels": 3})
            db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# =============================================================================================
# GNSS spoofing clusters
# =============================================================================================
class Gnss(unittest.TestCase):
    def test_circle_fit(self):
        pts = [(5 * math.cos(math.radians(a)), 5 * math.sin(math.radians(a)) + 2) for a in range(0, 360, 45)]
        cx, cy, r, dev, gap = fit_circle(pts)
        self.assertAlmostEqual(r, 5.0, places=6)
        self.assertAlmostEqual(cy, 2.0, places=6)
        self.assertLess(dev, 1e-6)
        self.assertLessEqual(gap, 45.0001)
        self.assertIsNone(fit_circle([(0, 0), (1, 1), (2, 2)]))           # collinear

    def test_jump_to_a_common_point(self):
        clock = Clock(T0)
        eng, store = engine(clock)
        target = (59.95, 26.60)
        for k in range(3):
            m = 273000010 + k
            start = geo.destination(target[0], target[1], k * 120, 25.0)
            store.position(m, T0 - 120, start[0], start[1], sog=10.0, cog=90.0)
            dest = geo.destination(target[0], target[1], k * 90, 0.4)
            self.assertFalse(store.position(m, T0 - 60, dest[0], dest[1], sog=10.0, cog=90.0))   # dropped, but kept
        self.assertEqual(len(store.jumps), 3)
        eng.run(store.build_snapshot(), {}, now=T0)
        g = [i for i in eng.items if i["kind"] == "gnss"]
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["signatures"], ["jump"])
        self.assertEqual((g[0]["confidence"], g[0]["severity"]), ("medium", 5))
        self.assertLess(geo.haversine_km(g[0]["lat"], g[0]["lon"], *target), 1.0)
        self.assertIn("jumped to a common point", g[0]["observed"])
        ev = [e for e in eng.events(T0 - 60) if e["kind"] == "gps_jam"]
        self.assertEqual((ev[0]["category"], ev[0]["id"][:6]), ("gnss_interference", "ships:"))
        self.assertTrue(store.in_hotspot(*target))
        for word in BANNED:
            self.assertNotIn(word, texts(g[0]))

    def test_circle_and_speed(self):
        group = []
        for k in range(8):
            lat, lon = geo.destination(43.0, 30.0, k * 45 + 10, 6.0)
            group.append({"mmsi": 271000000 + k, "lat": lat, "lon": lon, "t": T0, "kind": "speed", "sog": 70.0})
        h = gnss_signatures(group)
        self.assertEqual(h["signatures"], ["speed", "circle"])
        self.assertLess(geo.haversine_km(h["lat"], h["lon"], 43.0, 30.0), 0.5)
        self.assertAlmostEqual(h["circle"]["radius_km"], 6.0, delta=0.2)
        # four points on a circle are too few: three parameters fit four points by chance
        self.assertEqual(gnss_signatures(group[::2])["signatures"], ["speed"])

    def test_no_circle_from_scattered_points_or_clusters(self):
        import random
        rnd = random.Random(7)
        hits = 0
        for _trial in range(200):
            group = []
            for k in range(rnd.choice((4, 6, 8, 10))):
                lat, lon = geo.destination(60.45, 26.95, rnd.uniform(0, 360), 8.0 * math.sqrt(rnd.random()))
                group.append({"mmsi": 273100000 + k % 4, "lat": lat, "lon": lon, "t": T0, "kind": "speed", "sog": 60.0})
            hits += "circle" in ((gnss_signatures(group) or {}).get("signatures") or [])
        self.assertLessEqual(hits, 2)
        # two vessels whose bad fixes repeat at two spots: duplicates merge, too few vessels
        for km in (3.0, 8.0, 15.0):
            for trial in range(50):
                group = []
                for k in range(2):
                    spot = geo.destination(60.45, 26.95, 90 + 180 * k, km / 2)
                    for i in range(3):
                        lat, lon = geo.destination(spot[0], spot[1], rnd.uniform(0, 360), 0.03 * rnd.random())
                        group.append({"mmsi": 273200000 + k, "lat": lat, "lon": lon, "t": T0 + i, "kind": "jump",
                                      "sog": 10.0, "from": (60.0, 26.0)})
                self.assertIsNone(gnss_signatures(group))
        # one fix counted twice (a speed point and the same jump end) counts once
        group = []
        for k in range(4):
            lat, lon = geo.destination(43.0, 30.0, k * 90 + 10, 6.0)
            group.append({"mmsi": 271000000 + k, "lat": lat, "lon": lon, "t": T0, "kind": "speed", "sog": 70.0})
            group.append({"mmsi": 271000000 + k, "lat": lat, "lon": lon, "t": T0, "kind": "jump", "sog": 70.0,
                          "from": (43.5, 30.0)})
        self.assertNotIn("circle", gnss_signatures(group)["signatures"])

    def test_circle_does_not_count_for_confidence(self):
        clock = Clock(T0)
        eng, store = engine(clock)
        for k in range(8):
            lat, lon = geo.destination(43.0, 30.0, k * 45 + 10, 6.0)
            store.position(271000100 + k, T0 - 30, lat, lon, sog=70.0, cog=10.0)
        eng.run(store.build_snapshot(), {}, now=T0)
        g = [i for i in eng.items if i["kind"] == "gnss"][0]
        self.assertEqual(g["signatures"], ["circle", "speed"])
        self.assertEqual((g["confidence"], g["severity"]), ("medium", 5))

    def test_hotspot_expires_after_its_last_real_evidence(self):
        # Two moored ships each send one impossible-speed glitch, then ordinary fixes for 12 h, with a third
        # ship that never glitched nearby. Fixes inside the hotspot are suspect, but they are not new
        # evidence: the hotspot must lapse 6 h after the glitches, and the ships get trails again.
        clock = Clock(T0)
        eng, store = engine(clock)
        a, b, c = 273300001, 273300002, 273300003
        spots = {a: (60.45, 26.95), b: (60.455, 26.96), c: (60.46, 26.97)}
        for m in (a, b, c):
            store.static(m, T0 - 600, ship_type=70, length=120.0)
        store.position(a, T0 - 60, *spots[a], sog=60.0, cog=10.0)
        store.position(b, T0 - 50, *spots[b], sog=62.0, cog=10.0)
        eng.run(store.build_snapshot(), {}, now=T0)
        self.assertEqual(len(eng.hotspots(T0)), 1)
        t = T0
        seen = {}
        while t < T0 + 12 * 3600:
            t += 60
            clock.t = t
            for m, (lat, lon) in spots.items():
                store.position(m, t, lat, lon, sog=0.1, cog=10.0, nav=5)
            if int(t - T0) % 1800 == 0:
                eng.run(store.build_snapshot(), {}, now=t)
                seen[round((t - T0) / 3600, 1)] = len(eng.hotspots(t))
        self.assertEqual(seen[5.5], 1)
        self.assertEqual(seen[6.5], 0)
        self.assertEqual(seen[12.0], 0)
        self.assertFalse(store.vessels[c].suspect(t))
        self.assertFalse(store.vessels[a].suspect(t))
        g = [i for i in eng.items if i["kind"] == "gnss"]
        self.assertTrue(all(not i["ongoing"] for i in g))
        self.assertLessEqual(max(i["time"] for i in g), T0)

    def test_speed_point_sits_where_the_speed_was_reported(self):
        clock = Clock(T0)
        eng, store = engine(clock)
        store.position(273400001, T0 - 600, 57.67, 32.53, sog=80.0, cog=10.0)
        store.position(273400002, T0 - 590, 57.68, 32.52, sog=81.0, cog=10.0)
        # both return to their real berth 400 km away and report ordinary speeds
        store.position(273400001, T0 - 60, 60.336, 28.700, sog=0.8, cog=10.0)
        store.position(273400002, T0 - 50, 60.337, 28.702, sog=0.5, cog=10.0)
        eng.run(store.build_snapshot(), {}, now=T0)
        spots = eng.hotspots(T0)
        self.assertEqual(len(spots), 1)
        self.assertLess(geo.haversine_km(spots[0]["lat"], spots[0]["lon"], 57.675, 32.525), 5.0)
        self.assertFalse(store.in_hotspot(60.336, 28.700))

    def test_full_jump_buffer_stays_cheap(self):
        clock = Clock(T0)
        eng, store = engine(clock)
        for i in range(4000):
            m = 273500000 + i % 200
            lat, lon = geo.destination(59.9, 26.6, (i * 37) % 360, 1.0 + (i * 7) % 19)
            store.jumps.append((m, T0 - 1200 + i * 0.25, 59.6, 26.0, lat, lon, 10.0, True))
        started = time.perf_counter()
        eng._run_gnss(T0)
        self.assertLess(time.perf_counter() - started, 3.0)
        self.assertEqual(eng.excluded["gnss"]["suspect points"], 800)

    def test_jumps_onto_land_from_the_sea(self):
        land = LandIndex(geojson=square(60.0, 30.0, 61.0, 31.0))
        group = []
        for k in range(3):
            dest = (60.3 + 0.1 * k, 30.3 + 0.1 * k)                   # inland, 10 km apart
            group.append({"mmsi": 273000100 + k, "lat": dest[0], "lon": dest[1], "t": T0, "kind": "jump", "sog": 11.0,
                          "from": (59.7, 30.2 + 0.1 * k)})               # at sea, south of the land
        h = gnss_signatures(group, land)
        self.assertEqual(h["signatures"], ["land"])
        self.assertEqual(h["inland_vessels"], 3)
        # river and lake craft: the jump starts inland as well
        river = [dict(p, **{"from": (p["lat"] + 0.05, p["lon"])}) for p in group]
        self.assertIsNone(gnss_signatures(river, land))

    def test_benign_patterns(self):
        # one vessel (two ships sharing an MMSI) jumping back and forth
        one = [{"mmsi": 273000500, "lat": 59.9 + (i % 2) * 0.3, "lon": 26.0, "t": T0 + i, "kind": "jump", "sog": 10.0,
                "from": (59.9, 26.0)} for i in range(6)]
        self.assertIsNone(gnss_signatures(one))
        # two vessels jumping to one point is not enough
        two = [{"mmsi": 273000600 + k, "lat": 59.9, "lon": 26.0 + 0.001 * k, "t": T0, "kind": "jump", "sog": 10.0,
                "from": (59.5, 26.0)} for k in range(2)]
        self.assertIsNone(gnss_signatures(two))
        # high-speed craft at 40 kn are not suspect at all
        clock = Clock(T0)
        eng, store = engine(clock)
        for k in range(3):
            store.static(230500000 + k, T0 - 100, ship_type=40, length=40.0)
            store.position(230500000 + k, T0 - 30, 60.1 + 0.01 * k, 24.9, sog=40.0, cog=10.0)
        eng.run(store.build_snapshot(), {}, now=T0)
        self.assertEqual([i for i in eng.items if i["kind"] == "gnss"], [])

    def test_aircraft_corroboration_and_persistence(self):
        from meridian.store import Store
        tmp = tempfile.mkdtemp()
        try:
            db = Store(Path(tmp) / "t.db")
            clock = Clock(T0)
            eng, store = engine(clock, db=db)
            calls = []

            def air(lat, lon, radius_km, since):
                calls.append((round(lat, 2), round(lon, 2)))
                return {"level": "red", "severity": 6, "share": 41, "id": "gpsjam:59.5:28:0.5"}

            eng.air_lookup = air
            store.position(273000006, T0 - 30, 59.67, 28.53, sog=55.0, cog=10.0)
            store.position(273000007, T0 - 30, 59.68, 28.52, sog=61.0, cog=200.0)
            eng.run(store.build_snapshot(), {}, now=T0)
            g = [i for i in eng.items if i["kind"] == "gnss"][0]
            self.assertEqual((g["confidence"], g["severity"]), ("high", 6))
            self.assertIn("aircraft GPS also degraded", g["subtitle"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM ships_gnss"), 1)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM ships_gnss_days"), 1)
            store2 = VesselStore(db, now=clock)
            eng2 = AnomalyEngine(store2, db=db, ports=lambda: None, now=clock, log=LOG)
            eng2.restore()
            self.assertTrue(store2.in_hotspot(59.675, 28.525))
            self.assertEqual(len(eng2.hotspots()), 1)
            db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# =============================================================================================
# Places: areas of interest, land
# =============================================================================================
class Places(unittest.TestCase):
    def test_areas_of_interest(self):
        aoi = AreasOfInterest(StraitMonitor().defs)
        self.assertEqual(aoi.at(26.55, 56.45)["id"], "hormuz")
        self.assertEqual(aoi.at(36.6, 22.75)["id"], "laconian")
        self.assertIsNone(aoi.at(*OPEN))
        hit = aoi.on_path(36.0, -7.0, 36.0, -4.0)            # a line through the Strait of Gibraltar
        self.assertEqual(hit["id"], "gibraltar")
        from meridian.modules.ships.aoi import AREAS_OF_INTEREST, USUAL_AREAS
        for a in AREAS_OF_INTEREST + USUAL_AREAS:
            self.assertTrue(a["basis"], a["id"])

    def test_land_index(self):
        land = LandIndex(geojson=square(60.0, 30.0, 61.0, 31.0))
        self.assertTrue(land.on_land(60.5, 30.5))
        self.assertFalse(land.on_land(59.9, 30.5))
        self.assertAlmostEqual(land.coast_km(59.99, 30.5, 10), 1.1, delta=0.1)
        self.assertGreater(land.inland_km(60.5, 30.5), 20)
        self.assertEqual(land.inland_km(59.5, 30.5), 0.0)
        missing = LandIndex(Path(tempfile.gettempdir()) / "no-such-countries.geojson")
        self.assertIsNone(missing.on_land(60.5, 30.5))
        self.assertFalse(missing.available)


# =============================================================================================
# Service, AI tool and follow-ups
# =============================================================================================
class ServiceAndFollowUps(unittest.TestCase):
    def test_sanctions_keys_are_copied(self):
        store = VesselStore(None, now=Clock(), lookup=fake_lookup)
        store.static(A, T0, imo=9000003, name="X")
        s = store.vessels[A].sanc
        self.assertEqual(s["lists"], ["OFAC", "UK"])
        self.assertEqual(s["programmes"], {"OFAC": ["RUSSIA-EO14024"], "UK": ["Russia"]})
        self.assertEqual([x["list"] for x in s["listings"]], ["OFAC", "UK"])
        self.assertEqual(s["listed_on"], "2024-05-09")

    def test_event_subtitles_name_the_lists(self):
        clock = Clock(T0)
        svc = ShipsService(now=clock, lookup=fake_lookup, log=LOG)
        svc.store.position(A, T0 - 30, 59.7, 24.2, sog=12.0, cog=85.0)
        svc.store.static(A, T0, name="UK LISTED", imo=9000039, ship_type=80)
        svc.store.position(B, T0 - 30, 59.8, 24.2, sog=12.0, cog=85.0)
        svc.store.static(B, T0, name="BOTH", imo=9000003, ship_type=80)
        svc.refresh_snapshot()
        svc.run_analysis()
        subs = {e["id"]: e["subtitle"] for e in svc.events(T0 - 3600) if e.get("category") == "sanctioned_vessel"}
        self.assertEqual(subs[str(A)], "On the UK Sanctions List (UK: Russia), matched by IMO")
        self.assertNotIn("OFAC", subs[str(A)])
        self.assertIn("OFAC SDN list and UK Sanctions List", subs[str(B)])

    def test_ai_tool_and_kinds(self):
        clock = Clock(T0)
        svc = ShipsService(now=clock, lookup=fake_lookup, log=LOG)
        self.assertEqual(svc.parse_kinds("sts,gap"), {"sts", "ais_gap"})
        self.assertEqual(svc.parse_kinds(["gnss_interference"]), {"gnss"})
        self.assertIsNone(svc.parse_kinds("all"))
        with self.assertRaises(ValueError):
            svc.parse_kinds("piracy")
        svc.store.position(273000006, T0 - 30, 57.67, 32.53, sog=55.0, cog=10.0)
        svc.store.position(273000007, T0 - 30, 57.68, 32.52, sog=61.0, cog=200.0)
        svc.refresh_snapshot()
        svc.run_analysis()
        r = svc.ai_ship_anomalies({"kinds": ["gnss"], "bbox": [30, 55, 35, 60]})
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["anomalies"][0]["category"], "gnss_interference")
        self.assertTrue(r["anomalies"][0]["why_innocent"])
        self.assertIn("Candidates, not accusations", r["note"])
        self.assertEqual(svc.ai_ship_anomalies({"kinds": ["sts"]})["count"], 0)
        self.assertEqual(svc.ai_ship_anomalies({"bbox": [0, 0, 1, 1]})["count"], 0)
        self.assertIn("error", svc.ai_ship_anomalies({"bbox": [1, 2, 3]}))
        self.assertIn("error", svc.ai_ship_anomalies({"kinds": ["nope"]}))
        self.assertEqual(svc.status()["gnss_hotspots"][0]["signatures"], ["speed"])
        self.assertIn("anomalies", svc.status())


class NoNet:
    def set_host_interval(self, host, seconds):
        pass

    def get_json(self, *a, **kw):
        raise AssertionError("no network in tests")


class RegisteredRoutes(unittest.TestCase):
    """The real App with the ships module: new routes, the history routes' activity flag, wording, and
    re-matching when sanctions.version() changes even though the number of listed vessels does not."""

    @classmethod
    def setUpClass(cls):
        from meridian.config import Config
        from meridian.datasets import DatasetManager
        from meridian.registry import App
        from meridian.scheduler import Scheduler
        from meridian.store import Store
        from meridian.modules import sanctions, ships

        cls.tmp = tempfile.mkdtemp()
        cfg = Config(env_file=None, overrides={"MERIDIAN_DATA_DIR": cls.tmp, "AISSTREAM_API_KEY": "",
                                               "KYSTVERKET_AIS": "0", "SHIPS_PORTWATCH": "0"})
        cls.store = Store(Path(cls.tmp) / "meridian.db")
        cls.app = App(cfg, cls.store, NoNet(), LOG, Scheduler(LOG), DatasetManager(LOG))
        cls.m = cls.app.module("ships")
        sanctions._reset()
        ships.register(cls.m)
        cls.svc = cls.m.service
        cls.sanctions = sanctions

    @classmethod
    def tearDownClass(cls):
        cls.sanctions._reset()
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, **query):
        from meridian.registry import Request
        route, params, _ = self.app.match("GET", path)
        self.assertIsNotNone(route, path)
        return route.handler(Request("GET", path, params, {k: [str(v)] for k, v in query.items()}, {}))

    def status_of(self, path, **query):
        from meridian.registry import HTTPError
        try:
            self.get(path, **query)
        except HTTPError as err:
            return err.status
        return 200

    def test_history_routes_do_not_mark_the_module_active(self):
        touch = {r.pattern: r.touch for r in self.app.routes if r.module == "ships"}
        for path in ("/api/ships/history", "/api/ships/history/coverage", "/api/ships/history/{mmsi}"):
            self.assertFalse(touch[path], path)
        self.assertTrue(touch["/api/ships/live"])
        self.assertTrue(touch["/api/ships/anomalies"])

    def test_wording_without_ofac_only(self):
        spec = next(s for s in self.app.layer_specs if s["id"] == "ships.sanctioned")
        self.assertIn("sanctions lists", spec["description"])
        self.assertIn("Sanctions lists (OFAC, UK, UN, EU)", spec["source"])
        self.assertNotIn("OFAC SDN list.", spec["description"])
        S = self.sanctions
        S._reset()
        self.assertIn("against the sanctions lists", self.get("/api/ships/sanctioned")["note"])
        S._set_list("OFAC", {"vessels": [], "aircraft": [], "built_at": time.time()})
        self.assertIn("against the OFAC SDN list.", self.get("/api/ships/sanctioned")["note"])   # the only list
        S._set_list("UK", {"vessels": [], "aircraft": [], "built_at": time.time()})
        self.assertIn("against the OFAC SDN list and UK Sanctions List", self.get("/api/ships/sanctioned")["note"])
        S._reset()
        from meridian.registry import HTTPError
        with self.assertRaises(HTTPError) as cm:
            self.get("/api/ships/sdn/999999")
        self.assertEqual(cm.exception.message, "No vessel with that id on the current sanctions lists.")

    def test_anomaly_routes(self):
        out = self.get("/api/ships/anomalies")
        self.assertEqual(out["count"], 0)
        self.assertEqual(set(out["detectors"]), {"sts", "loitering", "ais_gap", "gnss"})
        self.assertIn("Candidates, not accusations", out["note"])
        self.assertEqual(self.status_of("/api/ships/anomalies", kinds="piracy"), 400)
        self.assertEqual(self.status_of("/api/ships/anomalies", hours=0), 400)
        self.assertEqual(self.status_of("/api/ships/anomalies/sts:1-2:3"), 404)
        self.assertEqual(self.status_of("/api/ships/anomalies/<script>"), 400)
        self.assertEqual(self.get("/api/ships/chokepoints")["enabled"], False)
        layer_ids = {s["id"] for s in self.app.layer_specs}
        self.assertIn("ships.anomalies", layer_ids)
        tools = {t["name"] for t in self.app.ai_tools()}
        self.assertTrue({"ship_anomalies", "chokepoint_transits"} <= tools)
        self.assertIn("error", self.app.call_ai_tool("chokepoint_transits", {}))

    def test_rematch_when_the_version_changes(self):
        S = self.sanctions
        st = self.svc.store
        st.position(636000777, time.time() - 30, 59.7, 24.2, sog=10.0, cog=90.0)
        st.static(636000777, time.time(), imo=9000041, name="SOME SHIP", ship_type=80)
        run = self.app.scheduler.tasks["ships.analysis"].fn
        S._set_list("OFAC", {"vessels": [{"uid": "77", "name": "OTHER", "imo": "9000053", "program": "IRAN"}],
                             "aircraft": [], "built_at": time.time()})
        run()
        self.assertFalse(st.vessels[636000777].sanctioned)
        # Same number of listed vessels, a different list: only version() notices.
        S._set_list("OFAC", {"vessels": [{"uid": "78", "name": "SOME SHIP", "imo": "9000041", "program": "IRAN"}],
                             "aircraft": [], "built_at": time.time()})
        run()
        self.assertTrue(st.vessels[636000777].sanctioned)
        self.assertEqual(st.vessels[636000777].sanc["lists"], ["OFAC"])


if __name__ == "__main__":
    unittest.main()
