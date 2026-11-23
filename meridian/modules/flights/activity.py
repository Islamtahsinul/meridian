"""Air activity: three detectors over data the flights module receives, published as events of kind
``air_activity`` with a ``category`` (CONTRACT 5.6):

- ``military_surge``: more military aircraft than usual over a watched region (activity_data.REGIONS).
- ``gps_interference``: GPS interference cells that are new, or clearly worse than the same clock hours
  on each of their own previous 7 days. Most cells repeat every day; those keep their ``gps_jam``
  events (now tagged ``category: chronic_interference``) but raise no ``air_activity`` event.
- ``airport_drop``: far fewer arrivals and departures than usual at a major airport.

Measurement. Military aircraft and airport movements are counted from *snapshots*: the first answer
in each 10-minute slot (at most 6 an hour), whatever else is polling. So hours when the map is open
(military polled every 60 s, tiles every 10 s) and idle hours (background sampling) are measured the
same way, and the number of snapshots in an hour is also its uptime: hours with under 80% of their
snapshots are left out of every baseline, so a quiet period counts only if Meridian was running.

Background sampling (CONTRACT rule 8, history is the point): one worldwide military list request
every 10 minutes (144 small adsb.fi requests a day; ``FLIGHTS_MIL_SAMPLER=0`` turns it off; the
cadence is fixed, because the baselines need 5 of the 6 snapshots in an hour). Airports
inside the GPS sampler's 16 circles get snapshots from it for free; favourited airports (up to 6) get
one point query every 10 minutes (``FLIGHTS_AIRPORT_WATCH=0`` turns that off); any other airport is
counted while you view it. The first start seeds the military baseline from the replay history
(``flights_history``, 72 h) for the slots in which the worldwide list was being polled.

Rules and wording live in activity_rules.py; routes and AI tools in activity_api.py.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any, Callable, Iterable

from meridian import geo

from . import activity_data as D
from . import activity_rules as R
from .const import FLAG_GND, FLAG_MIL, GPS_CELL_DEG, HOTSPOTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights_mil_hour (
    region TEXT NOT NULL,
    hour INTEGER NOT NULL,
    n INTEGER NOT NULL,
    roles TEXT NOT NULL DEFAULT '{}',
    hexes TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (region, hour)
);
CREATE INDEX IF NOT EXISTS flights_mil_hour_h ON flights_mil_hour(hour);
CREATE TABLE IF NOT EXISTS flights_mil_cov (
    hour INTEGER PRIMARY KEY,
    mask INTEGER NOT NULL,
    world_sum INTEGER NOT NULL,
    seeded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS flights_traffic_hour (
    spot TEXT NOT NULL,
    hour INTEGER NOT NULL,
    polls INTEGER NOT NULL,
    total INTEGER NOT NULL,
    PRIMARY KEY (spot, hour)
);
CREATE TABLE IF NOT EXISTS flights_apt_hour (
    ident TEXT NOT NULL,
    hour INTEGER NOT NULL,
    mask INTEGER NOT NULL,
    arr INTEGER NOT NULL,
    dep INTEGER NOT NULL,
    gnd INTEGER NOT NULL,
    high INTEGER NOT NULL,
    sets TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ident, hour)
);
CREATE INDEX IF NOT EXISTS flights_apt_hour_h ON flights_apt_hour(hour);
CREATE TABLE IF NOT EXISTS flights_activity (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    ref TEXT NOT NULL,
    first_t REAL NOT NULL,
    last_t REAL NOT NULL,
    severity INTEGER NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS flights_activity_t ON flights_activity(last_t);
"""

HOUR = R.HOUR
DAY = R.DAY
SLOT_S = R.SLOT_S
LAYER = "flights.activity"
SOURCE = "flights.activity"
CATEGORIES = ("military_surge", "gps_interference", "airport_drop")
CATEGORY_LABELS = {"military_surge": "Military surge", "gps_interference": "GPS interference trend",
                   "airport_drop": "Airport movement drop"}
MIL_KEEP_H = 90 * 24
MIL_HEXES_KEEP_H = 48
TRAFFIC_KEEP_H = 35 * 24
APT_KEEP_H = 35 * 24
EPISODES_KEEP_S = 30 * DAY
GRACE_S = {"military_surge": 3 * HOUR, "gps_interference": 3 * HOUR, "airport_drop": 2 * HOUR}
LOOKBACK_H = 29 * 24             # baselines read 28 days (4 weeks) back
DETECT_EVERY_S = 300.0
GPS_EVERY_S = 600.0
WATCH_MAX = 6
SEED_KEY = "flights.activity.seed"
SEED_MIN_OUTSIDE = 40            # military aircraft outside the GPS circles in a slot: the worldwide list was polled
POS_FRESH_S = 180.0              # a snapshot uses positions at most this old
APT_POS_FRESH_S = 60.0
GROUND_KM = 6.0
HIGH_FT = 25000
EXCLUDED_CLASSES = frozenset({"ground", "glider", "helicopter"})


def _json(text: str | None, default: Any) -> Any:
    try:
        v = json.loads(text) if text else default
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


class ActivityService:
    """``m.flights.activity``: tallies, samplers, assessments, episodes and the events provider."""

    def __init__(self, m, svc, air=None, *, clock: Callable[[], float] | None = None):
        self.m = m
        self.svc = svc
        self.air = air
        self.store = m.store
        self.clock = clock or svc.clock
        self.log: logging.Logger = m.log
        cfg = m.config
        self.mil_sampler = cfg.get_bool("FLIGHTS_MIL_SAMPLER", True)
        # One snapshot per 10-minute slot, fixed: baselines leave out hours with under 5 of their 6
        # snapshots, so a slower cadence would keep every region learning (the only option is off).
        self.mil_every = float(SLOT_S)
        self.watch_on = cfg.get_bool("FLIGHTS_AIRPORT_WATCH", True)
        self.lock = threading.RLock()
        self.detect_lock = threading.Lock()   # one assessment at a time (the task, or a first request)
        self.store.script(SCHEMA)
        # in memory: the current and previous hour only (the database has the rest)
        self.mil_cur: dict[int, dict[str, dict[str, str]]] = {}
        self.mil_cov: dict[int, list[int]] = {}
        self.apt_cur: dict[str, dict[int, dict]] = {}
        self.airports: dict[str, dict] = {}
        for ident, iata, name, lat, lon, cc, radius in D.AIRPORTS:
            self.airports[ident] = {"ident": ident, "iata": iata, "name": name, "lat": lat, "lon": lon,
                                    "country": cc, "radius_km": float(radius), "watched": False, "listed": True}
        self.watched: list[str] = []
        self.watch_t = 0.0
        self.watch_jobs: dict[str, Any] = {}
        self.mil_job_t = 0.0
        # caches (swapped whole; handlers read them without locks)
        self.region_states: dict[str, dict] = {}
        self.airport_states: dict[str, dict] = {}
        self.gps_trends: dict[tuple[int, int], dict] = {}
        self.gps_cluster_list: list[dict] = []
        self.gps_counts: dict[str, int] = {}
        self.gps_t = 0.0
        self.detect_t = 0.0
        self.prune_t = 0.0
        self.stats = {"mil_snapshots": 0, "apt_snapshots": 0, "seeded_hours": 0, "detect_runs": 0, "errors": 0}
        self._restore()

    # =========================================================================================
    # restore
    # =========================================================================================
    def _restore(self) -> None:
        now = self.clock()
        h = int(now // HOUR)
        try:
            for r in self.store.query("SELECT region, hour, hexes FROM flights_mil_hour WHERE hour >= ?", (h - 1,)):
                members = {}
                for tok in (r["hexes"] or "").split():
                    hx, _, code = tok.partition(":")
                    if len(hx) == 6 and code in D.CODE_ROLES:
                        members[hx] = code
                self.mil_cur.setdefault(int(r["hour"]), {})[r["region"]] = members
            for r in self.store.query("SELECT hour, mask, world_sum, seeded FROM flights_mil_cov WHERE hour >= ?",
                                      (h - 1,)):
                self.mil_cov[int(r["hour"])] = [int(r["mask"]), int(r["world_sum"])]
            for r in self.store.query("SELECT ident, hour, mask, sets FROM flights_apt_hour WHERE hour >= ? AND sets != ''",
                                      (h - 1,)):
                sets = _json(r["sets"], {})
                self.apt_cur.setdefault(r["ident"], {})[int(r["hour"])] = {
                    "mask": int(r["mask"]), "arr": set(sets.get("a") or ()), "dep": set(sets.get("d") or ()),
                    "gnd": set(sets.get("g") or ()), "high": set(sets.get("h") or ())}
        except Exception as err:            # a damaged table never stops the module
            self.log.warning("air activity: could not restore the current hour: %s", err)

    # =========================================================================================
    # military snapshots
    # =========================================================================================
    def observe_mil(self, hexes: Iterable[str], t0: float) -> bool:
        """After a worldwide military list answer (any cadence): the first one in each 10-minute slot
        becomes a snapshot. Returns True when it was recorded."""
        tracker = self.svc.tracker
        items = []
        with tracker.lock:
            for h in hexes:
                ac = tracker.aircraft.get(h)
                if ac is None or ac.lat is None or not ac.military or ac.private or ac.gnd:
                    continue
                if t0 - ac.pos_t > POS_FRESH_S:
                    continue
                items.append((h, ac.lat, ac.lon, R.role_of(ac.typ, ac.cls, ac.shp)))
        return self.add_mil_snapshot(t0, items)

    def add_mil_snapshot(self, t: float, items: list[tuple[str, float, float, str]], *, seeded: bool = False) -> bool:
        slot = int(t // SLOT_S)
        hour = int(t // HOUR)
        bit = 1 << (slot % R.SLOTS_PER_HOUR)
        with self.lock:
            cov = self.mil_cov.get(hour)
            if cov is None:
                cov = self.mil_cov[hour] = [0, 0]
            if cov[0] & bit:
                return False
            cov[0] |= bit
            cov[1] += len(items)
            regs = self.mil_cur.setdefault(hour, {})
            for h, lat, lon, role in items:
                for rid in R.regions_at(lat, lon):
                    regs.setdefault(rid, {})[h] = D.ROLE_CODES.get(role, "O")
            rows = [(rid, hour, *self._mil_row(members)) for rid, members in regs.items()]
            cov_row = (hour, cov[0], cov[1], 1 if seeded else 0)
            for table in (self.mil_cur, self.mil_cov):      # memory holds this hour and the one before
                for old in [k for k in table if k < hour - 1]:
                    table.pop(old, None)
            self.stats["mil_snapshots"] += 1
        try:
            with self.store.tx() as c:
                c.executemany("INSERT INTO flights_mil_hour(region, hour, n, roles, hexes) VALUES(?,?,?,?,?) "
                              "ON CONFLICT(region, hour) DO UPDATE SET n = excluded.n, roles = excluded.roles, "
                              "hexes = excluded.hexes", rows)
                c.execute("INSERT INTO flights_mil_cov(hour, mask, world_sum, seeded) VALUES(?,?,?,?) "
                          "ON CONFLICT(hour) DO UPDATE SET mask = excluded.mask, world_sum = excluded.world_sum, "
                          "seeded = excluded.seeded", cov_row)
        except Exception as err:
            self.log.warning("air activity: could not store a military snapshot: %s", err)
        return True

    @staticmethod
    def _mil_row(members: dict[str, str]) -> tuple[int, str, str]:
        roles: dict[str, int] = {}
        for code in members.values():
            role = D.CODE_ROLES.get(code, "other")
            roles[role] = roles.get(role, 0) + 1
        n = sum(v for k, v in roles.items() if R.counted(k))
        hexes = " ".join(f"{h}:{c}" for h, c in sorted(members.items()))
        return n, json.dumps(roles, sort_keys=True, separators=(",", ":")), hexes

    def sample_mil(self):
        """Background task (every 30 s): make sure each 10-minute slot gets one worldwide snapshot."""
        if not self.mil_sampler:
            return None
        now = self.clock()
        slot = int(now // SLOT_S)
        hour = int(now // HOUR)
        with self.lock:
            cov = self.mil_cov.get(hour)
            if cov and cov[0] & (1 << (slot % R.SLOTS_PER_HOUR)):
                return None
        if now - self.mil_job_t < 60:
            return None
        job = self.svc.poll_military(background=True)
        if job is not None:
            self.mil_job_t = now
        return job

    # =========================================================================================
    # regional traffic (receiver-coverage check), from the GPS sampler's circles
    # =========================================================================================
    def observe_traffic(self, spot: str, t: float, n: int) -> None:
        try:
            self.store.execute("INSERT INTO flights_traffic_hour(spot, hour, polls, total) VALUES(?,?,1,?) "
                               "ON CONFLICT(spot, hour) DO UPDATE SET polls = polls + 1, total = total + excluded.total",
                               (spot, int(t // HOUR), int(n)))
        except Exception as err:
            self.log.debug("air activity: traffic tally failed: %s", err)

    # =========================================================================================
    # airport snapshots
    # =========================================================================================
    def observe_area(self, circle: tuple[float, float, float], hexes: Iterable[str], t0: float) -> int:
        """After a point-radius answer (map tile, GPS sampler circle, watched airport, airport card):
        a snapshot for each airport whose whole counting circle lies inside, once per slot."""
        clat, clon, crad = circle
        slot = int(t0 // SLOT_S)
        hour = int(t0 // HOUR)
        bit = 1 << (slot % R.SLOTS_PER_HOUR)
        due = []
        with self.lock:
            for a in self.airports.values():
                if geo.haversine_km(clat, clon, a["lat"], a["lon"]) + a["radius_km"] > crad * 0.98:
                    continue
                st = self.apt_cur.get(a["ident"], {}).get(hour)
                if st is not None and st["mask"] & bit:
                    continue
                due.append(a)
        if not due:
            return 0
        hexes = list(hexes)
        tracker = self.svc.tracker
        from .service import flight_phase          # late import: service imports nothing from here
        done = 0
        for a in due:
            sets = {"arr": set(), "dep": set(), "gnd": set(), "high": set()}
            dlat = a["radius_km"] / 111.0 + 0.05
            with tracker.lock:
                for h in hexes:
                    ac = tracker.aircraft.get(h)
                    if ac is None or ac.lat is None or ac.private or ac.cls in EXCLUDED_CLASSES:
                        continue
                    if abs(ac.lat - a["lat"]) > dlat or t0 - ac.pos_t > APT_POS_FRESH_S:
                        continue
                    d = geo.haversine_km(a["lat"], a["lon"], ac.lat, ac.lon)
                    if d > a["radius_km"]:
                        continue
                    if ac.gnd:
                        if d <= GROUND_KM:
                            sets["gnd"].add(h)
                        continue
                    if ac.alt is not None and ac.alt >= HIGH_FT:
                        sets["high"].add(h)
                        continue
                    phase = flight_phase(ac, a["ident"], None, d, a["lat"], a["lon"])
                    if phase == "arrival":
                        sets["arr"].add(h)
                    elif phase == "departure":
                        sets["dep"].add(h)
            if self.add_apt_snapshot(a["ident"], t0, sets):
                done += 1
        return done

    def add_apt_snapshot(self, ident: str, t: float, sets: dict[str, set]) -> bool:
        slot = int(t // SLOT_S)
        hour = int(t // HOUR)
        bit = 1 << (slot % R.SLOTS_PER_HOUR)
        with self.lock:
            hours = self.apt_cur.setdefault(ident, {})
            if hours and hour < max(hours):
                return False                     # a late answer for a closed hour never rewrites it
            st = hours.get(hour)
            if st is None:
                st = hours[hour] = {"mask": 0, "arr": set(), "dep": set(), "gnd": set(), "high": set()}
            if st["mask"] & bit:
                return False
            st["mask"] |= bit
            for k in ("arr", "dep", "gnd", "high"):
                st[k] |= sets.get(k) or set()
            closed = [k for k in hours if k < hour]
            for k in closed:
                hours.pop(k, None)
            row = (ident, hour, st["mask"], len(st["arr"]), len(st["dep"]), len(st["gnd"]), len(st["high"]),
                   json.dumps({"a": sorted(st["arr"]), "d": sorted(st["dep"]), "g": sorted(st["gnd"]),
                               "h": sorted(st["high"])}, separators=(",", ":")))
            self.stats["apt_snapshots"] += 1
        try:
            with self.store.tx() as c:
                c.execute("INSERT INTO flights_apt_hour(ident, hour, mask, arr, dep, gnd, high, sets) "
                          "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(ident, hour) DO UPDATE SET mask = excluded.mask, "
                          "arr = excluded.arr, dep = excluded.dep, gnd = excluded.gnd, high = excluded.high, "
                          "sets = excluded.sets", row)
                if closed:
                    c.execute("UPDATE flights_apt_hour SET sets = '' WHERE ident = ? AND hour < ? AND sets != ''",
                              (ident, hour))
        except Exception as err:
            self.log.warning("air activity: could not store an airport snapshot: %s", err)
        return True

    # -- watched (favourited) airports ---------------------------------------------------------------
    def _refresh_watched(self, now: float) -> None:
        if now - self.watch_t < 300 and self.watch_t:
            return
        self.watch_t = now
        try:
            rows = self.store.query("SELECT ref, title, lat, lon FROM favourites WHERE kind = 'airport' "
                                    "ORDER BY created_at DESC LIMIT ?", (WATCH_MAX,))
        except Exception:
            rows = []
        watched = []
        with self.lock:
            for a in self.airports.values():
                a["watched"] = False
            for r in rows:
                ident = str(r["ref"] or "").upper()[:12]
                if not ident or not geo.valid_latlon(r["lat"], r["lon"]):
                    continue
                a = self.airports.get(ident)
                if a is None:
                    a = self.airports[ident] = {"ident": ident, "iata": None, "name": str(r["title"] or ident)[:80],
                                                "lat": float(r["lat"]), "lon": float(r["lon"]), "country": None,
                                                "radius_km": 35.0, "watched": True, "listed": False}
                a["watched"] = True
                watched.append(ident)
            # favourites removed: forget airports that are neither listed nor watched
            for ident in [k for k, a in self.airports.items() if not a["listed"] and not a["watched"]]:
                self.airports.pop(ident, None)
        self.watched = watched

    def _hotspot_covers(self, a: dict) -> bool:
        if not self.svc.gps_sampler:
            return False
        for _sid, _name, lat, lon, radius_nm in HOTSPOTS:
            if geo.haversine_km(lat, lon, a["lat"], a["lon"]) + a["radius_km"] <= radius_nm * 1.852 * 0.98:
                return True
        return False

    def sample_airports(self) -> int:
        """Background task (every 60 s): one point query per slot for each favourited airport that
        the GPS sampler does not already cover (at most two new requests a minute)."""
        now = self.clock()
        self._refresh_watched(now)
        if not self.watch_on or not self.watched:
            return 0
        if not self.svc.adsbfi.available("point") or self.svc.adsbfi.queue.size() > 6:
            return 0
        slot = int(now // SLOT_S)
        hour = int(now // HOUR)
        bit = 1 << (slot % R.SLOTS_PER_HOUR)
        sent = 0
        for ident in self.watched:
            a = self.airports.get(ident)
            if a is None or self._hotspot_covers(a):
                continue
            st = self.apt_cur.get(ident, {}).get(hour)
            if st is not None and st["mask"] & bit:
                continue
            job = self.watch_jobs.get(ident)
            if job is not None and not job.done.is_set():
                continue
            radius_nm = int(math.ceil((a["radius_km"] + 12.0) / 1.852))
            job = self.svc.adsbfi.queue.submit(f"apt:{ident}", lambda a=a, r=radius_nm: self._run_airport(a, r),
                                               priority=6, ttl=120)
            if job is not None:
                self.watch_jobs[ident] = job
                sent += 1
            if sent >= 2:
                break
        return sent

    def _run_airport(self, a: dict, radius_nm: int) -> int:
        t0 = self.clock()
        _, obs = self.svc.adsbfi.point(a["lat"], a["lon"], radius_nm)
        self.svc._absorb(obs, "adsbfi", t0, circle=(a["lat"], a["lon"], radius_nm * 1.852), history="notable")
        return len(obs)

    # =========================================================================================
    # seeding the military baseline from the replay history
    # =========================================================================================
    def seed_from_history(self) -> dict | None:
        """Once: tally the military aircraft in ``flights_history`` (72 h) for the 10-minute slots in
        which the worldwide military list was evidently being polled (40 or more military aircraft
        outside the GPS circles), before the sampler's own first snapshot."""
        if self.store.kv_get(SEED_KEY):
            return None
        hist = getattr(self.svc, "history", None)
        now = self.clock()
        result = {"at": now, "hours": 0, "slots": 0, "covered_slots": 0}
        if hist is None:
            self.store.kv_set(SEED_KEY, dict(result, note="no replay history"))
            return result
        first_live = self.store.scalar("SELECT MIN(hour) FROM flights_mil_cov WHERE seeded = 0")
        until = int(first_live) * HOUR if first_live is not None else int(now // SLOT_S) * SLOT_S
        since = max(int(now - 72 * HOUR), 0)
        if until <= since:
            self.store.kv_set(SEED_KEY, dict(result, note="nothing before the sampler"))
            return result
        from .history import CLASS_IDS
        conn = self.store.conn()
        try:
            types = {}
            for hx, typ in conn.execute("SELECT hex, typ FROM flights_history_ident WHERE last_t >= ? AND typ != ''",
                                        (since,)):
                types[int(hx)] = typ
            slots: dict[int, dict[int, tuple]] = {}
            for t, hx, lat, lon, cls, flags in conn.execute(
                    "SELECT t, hex, lat, lon, cls, flags FROM flights_history WHERE t >= ? AND t < ? AND (flags & ?) != 0",
                    (since, until, FLAG_MIL)):
                if flags & FLAG_GND:
                    continue
                slots.setdefault(int(t) // SLOT_S, {})[int(hx)] = (lat / 1e5, lon / 1e5, cls)
        except Exception as err:
            self.log.warning("air activity: could not read the replay history: %s", err)
            return None
        existing = {int(r["hour"]) for r in self.store.query("SELECT hour FROM flights_mil_cov WHERE hour >= ?",
                                                             (since // HOUR,))}
        circles = [(lat, lon, radius_nm * 1.852) for _s, _n, lat, lon, radius_nm in HOTSPOTS]
        hours_done = set()
        for slot in sorted(slots):
            members = slots[slot]
            outside = 0
            for lat, lon, _cls in members.values():
                if not any(geo.haversine_km(lat, lon, clat, clon) <= r for clat, clon, r in circles):
                    outside += 1
            result["slots"] += 1
            if outside < SEED_MIN_OUTSIDE:
                continue
            hour = (slot * SLOT_S) // HOUR
            if hour in existing:
                continue
            items = []
            for hx, (lat, lon, cls) in members.items():
                cls_name = CLASS_IDS[cls] if 0 <= cls < len(CLASS_IDS) else "unknown"
                items.append((f"{hx:06x}", lat, lon, R.role_of(types.get(hx), cls_name, None)))
            if self.add_mil_snapshot(slot * SLOT_S + 1, items, seeded=True):
                result["covered_slots"] += 1
                hours_done.add(hour)
        with self.lock:                      # seeded hours never linger in memory
            for h in hours_done:
                if h < int(now // HOUR) - 1:
                    self.mil_cur.pop(h, None)
                    self.mil_cov.pop(h, None)
        result["hours"] = len(hours_done)
        self.stats["seeded_hours"] = len(hours_done)
        self.store.kv_set(SEED_KEY, result)
        self.log.info("air activity: seeded %d hours of military counts from the replay history", len(hours_done))
        return result

    # =========================================================================================
    # assessment
    # =========================================================================================
    def detect(self, now: float | None = None, *, force_gps: bool = False) -> dict:
        """Background task: recompute GPS trends (every 10 min), region and airport states, episodes."""
        with self.detect_lock:
            return self._detect(now, force_gps=force_gps)

    def _detect(self, now: float | None, *, force_gps: bool) -> dict:
        now = self.clock() if now is None else float(now)
        out = {}
        try:
            if force_gps or now - self.gps_t >= GPS_EVERY_S - 1:
                self.refresh_gps(now)
            self.refresh_regions(now)
            self.refresh_airports(now)
            out = self.update_episodes(now)
            self.detect_t = now
            self.stats["detect_runs"] += 1
            if now - self.prune_t >= HOUR:
                self.prune(now)
            self._publish_status(now)
        except Exception as err:
            self.stats["errors"] += 1
            self.log.warning("air activity: detection failed: %s", err, exc_info=True)
            self.m.status(SOURCE, ok=False, error=f"Detection failed: {err}"[:300])
        return out

    # -- military ---------------------------------------------------------------------------------
    def _coverage(self, since_h: int) -> tuple[dict, dict, set]:
        uptime, world, seeded = {}, {}, set()
        for r in self.store.query("SELECT hour, mask, world_sum, seeded FROM flights_mil_cov WHERE hour >= ?",
                                  (since_h,)):
            k = R.popcount(r["mask"])
            hs = int(r["hour"]) * HOUR
            uptime[hs] = k / R.SLOTS_PER_HOUR
            world[hs] = (r["world_sum"] / k) if k else 0.0
            if r["seeded"]:
                seeded.add(hs)
        return uptime, world, seeded

    def _traffic(self, since_h: int) -> dict[str, tuple[dict, dict]]:
        """Per region: ({hour_start: mean aircraft per poll over its circles}, uptime)."""
        per_spot: dict[str, dict[int, tuple[int, int]]] = {}
        for r in self.store.query("SELECT spot, hour, polls, total FROM flights_traffic_hour WHERE hour >= ?", (since_h,)):
            per_spot.setdefault(r["spot"], {})[int(r["hour"]) * HOUR] = (int(r["polls"]), int(r["total"]))
        out = {}
        for reg in R.REGIONS:
            if not reg.hotspots:
                continue
            series, up = {}, {}
            hours = set()
            for s in reg.hotspots:
                hours |= set(per_spot.get(s, {}))
            for hs in hours:
                vals = [per_spot.get(s, {}).get(hs) for s in reg.hotspots]
                if any(v is None or v[0] <= 0 for v in vals):
                    continue
                series[hs] = sum(v[1] / v[0] for v in vals)
                up[hs] = min(1.0, min(v[0] for v in vals) / 3.0)
            out[reg.id] = (series, up)
        return out

    def refresh_regions(self, now: float) -> dict[str, dict]:
        end = int(now // HOUR) * HOUR
        since_h = end // HOUR - LOOKBACK_H
        hourly: dict[str, dict[int, int]] = {r.id: {} for r in R.REGIONS}
        roles: dict[str, dict[int, dict]] = {r.id: {} for r in R.REGIONS}
        for r in self.store.query("SELECT region, hour, n, roles FROM flights_mil_hour WHERE hour >= ?", (since_h,)):
            if r["region"] not in hourly:
                continue
            hs = int(r["hour"]) * HOUR
            hourly[r["region"]][hs] = int(r["n"])
            roles[r["region"]][hs] = _json(r["roles"], {})
        uptime, world, seeded = self._coverage(since_h)
        traffic = self._traffic(since_h)
        window = {}
        for r in self.store.query("SELECT region, hexes FROM flights_mil_hour WHERE hour >= ? AND hour < ?",
                                  (end // HOUR - R.MIL_WINDOW_H, end // HOUR)):
            ids = window.setdefault(r["region"], set())
            for tok in (r["hexes"] or "").split():
                hx, _, code = tok.partition(":")
                if R.counted(D.CODE_ROLES.get(code, "other")):
                    ids.add(hx)
        hour_now = int(now // HOUR)
        with self.lock:
            cur = {rid: dict(m) for rid, m in self.mil_cur.get(hour_now, {}).items()}
            cur_snaps = R.popcount((self.mil_cov.get(hour_now) or [0, 0])[0])
        states = {}
        for reg in R.REGIONS:
            series = hourly[reg.id]
            rh = roles[reg.id]
            roles_now = {}
            for k in range(R.MIL_WINDOW_H):
                for role, n in (rh.get(end - (k + 1) * HOUR) or {}).items():
                    roles_now[role] = roles_now.get(role, 0) + int(n)
            roles_usual = {}
            for role in D.ROLES:                # the same profile as the region's own usual
                role_series = {hs: int(v.get(role, 0)) for hs, v in rh.items()}
                usual, _profile, _n = R.surge_usual(role_series, end, uptime=uptime)
                if usual is not None:
                    roles_usual[role] = usual
            tr = traffic.get(reg.id)
            st = R.assess_surge(series, uptime, end, distinct=len(window.get(reg.id, ())), roles_now=roles_now,
                                roles_usual=roles_usual, world=world, world_uptime=uptime,
                                traffic=tr[0] if tr else None, traffic_uptime=tr[1] if tr else None,
                                has_traffic_check=bool(reg.hotspots), seeded=seeded)
            members = cur.get(reg.id, {})
            st.update(id=reg.id, name=reg.name, label=reg.label, bbox=list(reg.bbox),
                      drivers=R.drivers_text(st["roles"]),
                      this_hour={"aircraft": sum(1 for c in members.values() if R.counted(D.CODE_ROLES.get(c, "other"))),
                                 "snapshots": cur_snaps, "since": hour_now * HOUR})
            states[reg.id] = st
        self.region_states = states
        return states

    # -- airports ---------------------------------------------------------------------------------
    def refresh_airports(self, now: float) -> dict[str, dict]:
        end = int(now // HOUR) * HOUR
        since_h = end // HOUR - LOOKBACK_H
        moves: dict[str, dict] = {}
        high: dict[str, dict] = {}
        up: dict[str, dict] = {}
        for r in self.store.query("SELECT ident, hour, mask, arr, dep, high FROM flights_apt_hour WHERE hour >= ?",
                                  (since_h,)):
            hs = int(r["hour"]) * HOUR
            ident = r["ident"]
            moves.setdefault(ident, {})[hs] = int(r["arr"]) + int(r["dep"])
            high.setdefault(ident, {})[hs] = int(r["high"])
            up.setdefault(ident, {})[hs] = R.popcount(r["mask"]) / R.SLOTS_PER_HOUR
        states = {}
        hour_now = int(now // HOUR)
        with self.lock:
            airports = [dict(a) for a in self.airports.values()]
            cur = {ident: dict(h.get(hour_now) or {}) for ident, h in self.apt_cur.items()}
        for a in airports:
            ident = a["ident"]
            st = R.assess_airport(moves.get(ident, {}), high.get(ident, {}), up.get(ident, {}), end)
            c = cur.get(ident) or {}
            st.update(ident=ident, iata=a.get("iata"), name=a["name"], lat=a["lat"], lon=a["lon"],
                      country=a.get("country"), radius_km=a["radius_km"], watched=a["watched"],
                      background=self._hotspot_covers(a) or (a["watched"] and self.watch_on),
                      hours_seen=sum(1 for v in up.get(ident, {}).values() if v >= R.MIN_COVERAGE),
                      this_hour={"arrivals": len(c.get("arr") or ()), "departures": len(c.get("dep") or ()),
                                 "on_ground": len(c.get("gnd") or ()), "overflights": len(c.get("high") or ()),
                                 "snapshots": R.popcount(c.get("mask") or 0), "since": hour_now * HOUR})
            st["faa"] = self._faa(ident, a)
            if st["state"] in ("learning", "no_coverage") and not st["background"] and not st["hours_seen"]:
                st["status"] = "No snapshots yet: counted only while you view the area, or when you favourite it"
            states[ident] = st
        self.airport_states = states
        return states

    def _faa(self, ident: str, a: dict) -> dict | None:
        """FAA NAS Status for US airports: the link, and the current status when there is one."""
        if a.get("country") not in (None, "US") or self.air is None:
            return None
        try:
            from . import airspace_parse as ap
            if ap.faa_for(ident) is None:
                return None
            found = self.air.find_airport(ident, a["lat"], a["lon"])
        except Exception:
            return None
        out = {"link": "https://nasstatus.faa.gov/", "statuses": []}
        if found:
            for s in found.get("statuses") or []:
                out["statuses"].append({"type": s.get("type"), "reason": s.get("reason"),
                                        "avg_delay_min": s.get("avg_delay_min"), "code": s.get("code")})
        return out

    # -- GPS interference -------------------------------------------------------------------------
    @staticmethod
    def _gps_span(now: float) -> tuple[int, int, int]:
        """(first hour of the recent window, the hour after it, first hour of the baseline): the last 6
        complete hours, so the window and the same clock hours on earlier days match exactly."""
        h_now = int(now // HOUR)
        recent_from = h_now - R.GPS_RECENT_H
        return recent_from, h_now, recent_from - R.GPS_BASE_DAYS * 24

    def _gps_sampled(self, row: int, col: int, agg: int = 1) -> bool:
        if not self.svc.gps_sampler:
            return False
        size = GPS_CELL_DEG * agg
        return R.in_sampler_circle(-90.0 + (row // agg + 0.5) * size, -180.0 + (col // agg + 0.5) * size)

    def refresh_gps(self, now: float) -> list[dict]:
        """Each cell's last 6 complete hours against the same clock hours on each of the 7 days before
        (and the 7 whole days, for cells that repeat daily at other hours)."""
        recent_from, recent_to, base_from = self._gps_span(now)
        # "+row, +col": without the unary plus SQLite scans the whole table through the (row, col)
        # index to save a sort, instead of reading the hour range from the primary key
        recent = {(r["row"], r["col"]): (int(r["t"]), int(r["b"]), int(r["hb"])) for r in self.store.query(
            "SELECT row, col, SUM(total) AS t, SUM(bad) AS b, SUM(bad >= 2) AS hb FROM flights_gpsjam "
            "WHERE hour >= ? AND hour < ? GROUP BY +row, +col", (recent_from, recent_to))}
        n = R.GPS_BASE_DAYS
        windows: dict[tuple[int, int], list[list[int]]] = {}
        days: dict[tuple[int, int], list[list[int]]] = {}
        # k: 0 for the 24 hours just before the recent window; w: the first 6 hours of each such day are
        # the same clock hours as the recent window
        for r in self.store.query(
                "SELECT row, col, (? - 1 - hour) / 24 AS k, (hour - ? + ?) % 24 < ? AS w, SUM(total) AS t, "
                "SUM(bad) AS b FROM flights_gpsjam WHERE hour >= ? AND hour < ? GROUP BY +row, +col, k, w",
                (recent_from, recent_from, n * 24, R.GPS_RECENT_H, base_from, recent_from)):
            k = int(r["k"])
            if not 0 <= k < n:
                continue
            key = (r["row"], r["col"])
            t, b = int(r["t"]), int(r["b"])
            d = days.setdefault(key, [[0, 0] for _ in range(n)])
            d[k][0] += t
            d[k][1] += b
            if r["w"]:
                windows.setdefault(key, [[0, 0] for _ in range(n)])[k] = [t, b]
        trends = {}
        empty = [(0, 0)] * n
        for key in set(recent) | set(days):
            t, b, hb = recent.get(key, (0, 0, 0))
            tr = R.classify_gps(t, b, windows.get(key, empty), days.get(key, empty), shared_hours=hb,
                                sampled=self._gps_sampled(*key))
            if tr["label"] not in ("new", "worse"):
                tr.pop("windows", None)          # only clusters need them (each cluster's earlier days)
            trends[key] = tr
        clusters = R.gps_clusters(trends, GPS_CELL_DEG)
        counts: dict[str, int] = {}
        try:
            flagged = [c for c in self.svc.gps.cells(hours=24, now=now) if c["level"] in ("amber", "red")]
        except Exception:
            flagged = []
        for c in flagged:
            label = (trends.get((c["row"], c["col"])) or {}).get("label", "learning")
            counts[label] = counts.get(label, 0) + 1
        self.gps_trends = trends
        self.gps_cluster_list = clusters
        self.gps_counts = {"amber_or_red_24h": len(flagged), **counts}
        self.gps_t = now
        return clusters

    def gps_trend_of(self, row: int, col: int) -> dict | None:
        return self.gps_trends.get((row, col))

    def cell_trend(self, row: int, col: int, agg: int, now: float) -> dict:
        """The trend of one 0.5 degree cell (agg 1) or a 1 degree block (agg 2), for the card: judged as
        in ``refresh_gps``, plus one column per UTC day for the chart."""
        h_now = int(now // HOUR)
        recent_from, recent_to, base_from = self._gps_span(now)
        n = R.GPS_BASE_DAYS
        day_now = h_now // 24
        if agg > 1:
            r0, c0 = (row // agg) * agg, (col // agg) * agg
            where, args = "row >= ? AND row < ? AND col >= ? AND col < ?", (r0, r0 + agg, c0, c0 + agg)
        else:
            where, args = "row = ? AND col = ?", (row, col)
        rows = self.store.query(f"SELECT hour, SUM(total) AS t, SUM(bad) AS b FROM flights_gpsjam WHERE hour >= ? "
                                f"AND {where} GROUP BY hour", (min(base_from, (day_now - 6) * 24), *args))
        recent_t = recent_b = shared = 0
        windows = [[0, 0] for _ in range(n)]
        blocks = [[0, 0] for _ in range(n)]
        days: dict[int, list[int]] = {}
        for r in rows:
            hr, t, b = int(r["hour"]), int(r["t"]), int(r["b"])
            if recent_from <= hr < recent_to:
                recent_t += t
                recent_b += b
                shared += 1 if b >= 2 else 0
            elif base_from <= hr < recent_from:
                k = (recent_from - 1 - hr) // 24
                blocks[k][0] += t
                blocks[k][1] += b
                if (hr - recent_from + n * 24) % 24 < R.GPS_RECENT_H:
                    windows[k][0] += t
                    windows[k][1] += b
            d = hr // 24
            if day_now - 6 <= d <= day_now:
                cell = days.setdefault(d, [0, 0])
                cell[0] += t
                cell[1] += b
        trend = R.classify_gps(recent_t, recent_b, [tuple(x) for x in windows], [tuple(x) for x in blocks],
                               shared_hours=shared, sampled=self._gps_sampled(row, col, agg))
        trend["window"] = {"from": recent_from * HOUR, "to": recent_to * HOUR}
        trend["text"] = R.trend_text(trend)
        trend["category"] = R.GPS_CATEGORY.get(trend["label"])
        trend["days"] = [[d * DAY, days.get(d, [0, 0])[0], days.get(d, [0, 0])[1]] for d in range(day_now - 6, day_now + 1)]
        trend["recent_hours"] = R.GPS_RECENT_H
        return trend

    # =========================================================================================
    # episodes and events
    # =========================================================================================
    def _open_episodes(self, category: str, now: float) -> list[dict]:
        rows = self.store.query("SELECT * FROM flights_activity WHERE category = ? AND last_t >= ?",
                                (category, now - GRACE_S[category]))
        for r in rows:
            r["data"] = _json(r["data"], {})
        return rows

    def _save_episode(self, eid: str, category: str, ref: str, now: float, severity: int, lat: float, lon: float,
                      title: str, subtitle: str, data: dict, first_t: float | None = None) -> None:
        self.store.execute(
            "INSERT INTO flights_activity(id, category, ref, first_t, last_t, severity, lat, lon, title, subtitle, data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_t = excluded.last_t, "
            "severity = MAX(severity, excluded.severity), lat = excluded.lat, lon = excluded.lon, "
            "title = excluded.title, subtitle = excluded.subtitle, data = excluded.data",
            (eid, category, ref, first_t if first_t is not None else now, now, int(severity), round(lat, 5),
             round(lon, 5), title[:300], subtitle[:300], json.dumps(data, separators=(",", ":"), default=str)))

    def update_episodes(self, now: float) -> dict:
        opened = {c: 0 for c in CATEGORIES}
        # military surges
        open_mil = {r["ref"]: r for r in self._open_episodes("military_surge", now)}
        for rid, st in self.region_states.items():
            if st["state"] != "unusual":
                continue
            ep = open_mil.get(rid)
            eid = ep["id"] if ep else f"flights:surge:{rid}:{int(now)}"
            if ep is None:
                opened["military_surge"] += 1
            title, sub = R.surge_words(st, R.REGION_BY_ID[rid].over)
            data = {"ratio": _round(st["ratio"], 2), "z": _round(st["z"], 2), "value": st["value"],
                    "usual": _round(st["usual"], 2), "per_hour": _round(st["per_hour"], 2),
                    "usual_per_hour": _round(st["usual_per_hour"], 2), "distinct": st["distinct"],
                    "confidence": st["confidence"], "profile": st["profile"], "bbox": st["bbox"],
                    "window": st["window"], "drivers": st.get("drivers"), "baseline_days": st["baseline_days"],
                    "peak_ratio": max(_round(st["ratio"], 2) or 0, (ep or {}).get("data", {}).get("peak_ratio") or 0)}
            self._save_episode(eid, "military_surge", rid, now, st["severity"], st["label"][1], st["label"][0],
                               title, sub, data)
            st["episode"] = eid
        # airport drops
        open_apt = {r["ref"]: r for r in self._open_episodes("airport_drop", now)}
        for ident, st in self.airport_states.items():
            if st["state"] not in ("drop", "drop_unverified"):
                continue
            ep = open_apt.get(ident)
            eid = ep["id"] if ep else f"flights:airport:{ident}:{int(now)}"
            if ep is None:
                opened["airport_drop"] += 1
            if st["state"] == "drop":
                title = f"Far fewer movements than usual at {st['name']} ({ident})"
            else:
                title = f"Far fewer aircraft than usual around {st['name']} ({ident})"
            sub = (f"{R.num_text(st['value'], 0)} arrivals and departures in the last hour, usually "
                   f"{R.num_text(st['usual'], 0)}; " + ("overflights normal" if st["receivers"] == "ok" else
                                                         "overflights down too" if st["receivers"] == "low" else
                                                         "receiver coverage not checked"))
            data = {"share": _round(st["share"], 3), "value": st["value"], "usual": _round(st["usual"], 1),
                    "receivers": st["receivers"], "confidence": st["confidence"], "state": st["state"],
                    "faa": bool(st.get("faa")), "radius_km": st["radius_km"]}
            self._save_episode(eid, "airport_drop", ident, now, st["severity"], st["lat"], st["lon"], title, sub, data)
            st["episode"] = eid
        # GPS interference: clusters continue an open episode when they share or touch one of its cells
        open_gps = self._open_episodes("gps_interference", now)
        used: set[str] = set()                   # clusters come largest first; each episode goes to one of them
        for cl in self.gps_cluster_list:
            cells = {tuple(c) for c in cl["cells"]}
            ring = {(r + dr, c + dc) for r, c in cells for dr in (-1, 0, 1) for dc in (-1, 0, 1)}
            ep = next((e for e in open_gps if e["id"] not in used
                       and ring & {tuple(c) for c in e["data"].get("cells") or ()}), None)
            if ep is None:
                r0, c0 = cl["cells"][0]
                lat0 = -90.0 + r0 * GPS_CELL_DEG
                lon0 = -180.0 + c0 * GPS_CELL_DEG
                eid = f"flights:gps:{lat0:g}:{lon0:g}:{int(now)}"
                opened["gps_interference"] += 1
                ep = {"id": eid, "data": {}}
                open_gps.append(ep)
            used.add(ep["id"])
            title, sub = R.gps_cluster_words(cl)
            data = {"cells": [list(c) for c in cl["cells"]][:150], "n": cl["n"], "bbox": cl["bbox"],
                    "change": cl["change"], "share_now": _round(cl["share_now"], 4),
                    "share_base": _round(cl["share_base"], 4), "recent_total": cl["recent_total"],
                    "recent_bad": cl["recent_bad"], "base_total": cl["base_total"],
                    "days_with_data": cl["days_with_data"], "max_daily_share": _round(cl["max_daily_share"], 4),
                    "confidence": cl["confidence"], "confidence_reasons": cl["confidence_reasons"],
                    "place": cl["place"],
                    "peak_share": max(_round(cl["share_max"], 4) or 0, ep["data"].get("peak_share") or 0)}
            ep["data"] = data
            ref = ":".join(ep["id"].split(":")[2:4])
            self._save_episode(ep["id"], "gps_interference", ref, now, cl["severity"], cl["lat"], cl["lon"], title,
                               sub, data)
            cl["episode"] = ep["id"]
        return opened

    def events(self, since: float) -> list[dict]:
        rows = self.store.query("SELECT * FROM flights_activity WHERE last_t >= ? ORDER BY last_t DESC LIMIT 300",
                                (since,))
        return [self.episode_event(r) for r in rows]

    @staticmethod
    def episode_event(r: dict) -> dict:
        data = r["data"] if isinstance(r.get("data"), dict) else _json(r.get("data"), {})
        ev = {"kind": "air_activity", "id": r["id"], "category": r["category"], "title": r["title"],
              "subtitle": r["subtitle"], "lat": r["lat"], "lon": r["lon"], "time": r["last_t"],
              "first_time": r["first_t"], "severity": int(r["severity"]), "layer": LAYER, "ref": r["ref"],
              "confidence": data.get("confidence")}
        if data.get("bbox"):
            ev["bbox"] = data["bbox"]
        if r["category"] == "military_surge":
            ev.update(ratio=data.get("ratio"), zoom=5)
        elif r["category"] == "airport_drop":
            ev.update(share=data.get("share"), zoom=9)
        else:
            ev.update(change=data.get("change"), cells=data.get("n"), zoom=7)
        return ev

    # =========================================================================================
    # housekeeping and status
    # =========================================================================================
    def prune(self, now: float | None = None) -> None:
        now = self.clock() if now is None else float(now)
        self.prune_t = now
        h = int(now // HOUR)
        with self.store.tx() as c:
            c.execute("DELETE FROM flights_mil_hour WHERE hour < ?", (h - MIL_KEEP_H,))
            c.execute("UPDATE flights_mil_hour SET hexes = '' WHERE hour < ? AND hexes != ''", (h - MIL_HEXES_KEEP_H,))
            c.execute("DELETE FROM flights_mil_cov WHERE hour < ?", (h - MIL_KEEP_H,))
            c.execute("DELETE FROM flights_traffic_hour WHERE hour < ?", (h - TRAFFIC_KEEP_H,))
            c.execute("DELETE FROM flights_apt_hour WHERE hour < ?", (h - APT_KEEP_H,))
            c.execute("UPDATE flights_apt_hour SET sets = '' WHERE hour < ? AND sets != ''", (h - 1,))
            c.execute("DELETE FROM flights_activity WHERE last_t < ?", (now - EPISODES_KEEP_S,))

    def this_hour(self, now: float) -> tuple[dict[str, dict], dict[str, dict]]:
        """Live 'this hour so far' readings from memory: ({region: ...}, {airport: ...})."""
        hour = int(now // HOUR)
        since = hour * HOUR
        with self.lock:
            snaps = R.popcount((self.mil_cov.get(hour) or [0, 0])[0])
            regions = {rid: {"aircraft": sum(1 for c in members.values() if R.counted(D.CODE_ROLES.get(c, "other"))),
                             "snapshots": snaps, "since": since}
                       for rid, members in self.mil_cur.get(hour, {}).items()}
            airports = {}
            for ident, hours in self.apt_cur.items():
                c = hours.get(hour)
                if c:
                    airports[ident] = {"arrivals": len(c["arr"]), "departures": len(c["dep"]), "on_ground": len(c["gnd"]),
                                       "overflights": len(c["high"]), "snapshots": R.popcount(c["mask"]), "since": since}
        empty_r = {"aircraft": 0, "snapshots": snaps, "since": since}
        empty_a = {"arrivals": 0, "departures": 0, "on_ground": 0, "overflights": 0, "snapshots": 0, "since": since}
        return ({rid: regions.get(rid, dict(empty_r)) for rid in R.REGION_BY_ID},
                {ident: airports.get(ident, dict(empty_a)) for ident in list(self.airports)})

    def flags(self) -> int:
        return (sum(1 for s in self.region_states.values() if s["state"] == "unusual")
                + sum(1 for s in self.airport_states.values() if s["state"] in ("drop", "drop_unverified"))
                + len(self.gps_cluster_list))

    def sampler_info(self, now: float) -> dict:
        since_h = int(now // HOUR) - 24
        rows = self.store.query("SELECT hour, mask, seeded FROM flights_mil_cov WHERE hour >= ?", (since_h,))
        snaps = sum(R.popcount(r["mask"]) for r in rows if not r["seeded"])
        days = self.store.scalar("SELECT COUNT(DISTINCT hour / 24) FROM flights_mil_cov", default=0)
        return {"military": {"on": self.mil_sampler, "every_s": self.mil_every, "snapshots_24h": snaps,
                             "expected_24h": int(24 * HOUR // self.mil_every), "days_with_data": int(days or 0),
                             "seeded_hours": sum(1 for r in rows if r["seeded"]),
                             "requests_per_day": int(DAY // self.mil_every) if self.mil_sampler else 0},
                "airports": {"watch_on": self.watch_on, "watched": list(self.watched), "watch_max": WATCH_MAX,
                             "requests_per_day_max": WATCH_MAX * int(DAY // SLOT_S) if self.watch_on else 0},
                "gps": {"sampler_on": bool(self.svc.gps_sampler), "updated": self.gps_t or None}}

    def _publish_status(self, now: float) -> None:
        info = self.sampler_info(now)["military"]
        learning = sum(1 for s in self.region_states.values() if s["state"] == "learning")
        note = (f"Military snapshots: {info['snapshots_24h']} of {info['expected_24h']} in 24 h"
                if self.mil_sampler else "Military sampler off (FLIGHTS_MIL_SAMPLER=0)")
        if learning:
            note += f"; {learning} regions learning"
        self.m.status(SOURCE, ok=True, count=self.flags(), note=note)


def _round(v, digits):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return round(float(v), digits)
