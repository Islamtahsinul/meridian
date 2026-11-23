"""The vessel store: one record per MMSI merged from every provider, kept in memory and persisted
to SQLite (``ships_`` tables) so a restart keeps history.

Bounded by design: vessels unseen for 7 days are pruned; the position history keeps about 72 hours
with at least 2 minutes between points (one point per 10 minutes on a straight course, more in
turns, one every 2 hours when stationary); port calls and strait visits keep 7 days, AIS gap episodes
30 days (the anomaly detectors read them).
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from meridian import geo

from . import ais as aislib
from .classify import CLASS_INDEX, classify, size_class
from .mid import flag_of, is_ship_mmsi
from .straits import StraitMonitor

SRC_NAMES = {1: "digitraffic", 2: "aisstream", 3: "kystverket"}
SRC_LABELS = {1: "Digitraffic", 2: "aisstream.io", 3: "Kystverket"}
SRC_IDS = {v: k for k, v in SRC_NAMES.items()}

KEEP_VESSELS_S = 7 * 86400
KEEP_TRACK_S = 72 * 3600      # also timeline replay (history.py, capped by SHIPS_HISTORY_MAX_ROWS)
KEEP_EVENTS_S = 7 * 86400
KEEP_GAPS_S = 30 * 86400      # AIS gap episodes (anomalies.py)
CALLS_IN_MEMORY_S = 48 * 3600
TRAIL_MIN_S = 120
TRAIL_STRAIGHT_S = 600
TRAIL_STATIONARY_S = 7200
CALL_MIN_S = 20 * 60
MOVING_KN = 1.0
STOPPED_KN = 0.5
MAX_PLAUSIBLE_KN = 80.0
LIVE_MOVING_S = 3600          # moving vessels drop off the live map after an hour without a report
LIVE_STATIONARY_S = 6 * 3600  # stationary ones after six hours
STALE_MOVING_S = 900
STALE_STATIONARY_S = 3600

# Row layout of the live snapshot (tuples are cheap to build every few seconds).
R_MMSI, R_LAT, R_LON, R_DIR, R_CLS, R_FLAGS, R_SOG, R_SIZE, R_NAME, R_T, R_MOVING, R_SRC, R_NAV, R_STRAIT, \
    R_LEN, R_TYPE = range(16)
F_SANCTIONED, F_POSSIBLE, F_CLASS_B, F_STALE, F_SUSPECT = 1, 2, 4, 8, 16

# A reported speed above this is impossible for anything but high-speed craft: the position is
# almost certainly corrupted, often by GNSS interference (spoofing).
SUSPECT_KN = 45.0
SUSPECT_HOLD_S = 1800
# Position jumps of 5 km or more at an impossible implied speed are kept for a while (accepted or not):
# several vessels jumping to one point, in a circle or onto land is a GNSS spoofing signature.
JUMP_KM = 5.0
JUMPS_KEPT = 4000


class Vessel:
    __slots__ = ("mmsi", "name", "imo", "callsign", "ship_type", "dest", "eta", "draught", "length", "beam",
                 "ais", "lat", "lon", "sog", "cog", "hdg", "nav", "rot", "t", "t_static", "first_seen", "src",
                 "srcs", "cls", "reports", "state", "state_since", "stop_lat", "stop_lon", "stop_nav", "stop_seen",
                 "trail_t", "trail_lat", "trail_lon", "trail_cog", "trail_sog", "sanc", "sanc_key", "strait",
                 "dark_since", "reg", "suspect_t", "suspect_sog", "suspect_sog_t", "suspect_sog_lat", "suspect_sog_lon",
                 "reject_t", "quiet_since", "gap_ctx")

    def __init__(self, mmsi: int, t: float):
        self.mmsi = mmsi
        self.name = ""
        self.imo = None
        self.callsign = ""
        self.ship_type = None
        self.dest = ""
        self.eta = None
        self.draught = None
        self.length = None
        self.beam = None
        self.ais = None
        self.lat = None
        self.lon = None
        self.sog = None
        self.cog = None
        self.hdg = None
        self.nav = None
        self.rot = None
        self.t = 0.0
        self.t_static = 0.0
        self.first_seen = t
        self.src = 0
        self.srcs = 0
        self.cls = "other"
        self.reports: list[float] = []
        self.state = None
        self.state_since = 0.0
        self.stop_lat = None
        self.stop_lon = None
        self.stop_nav = None
        self.stop_seen = False
        self.trail_t = None
        self.trail_lat = None
        self.trail_lon = None
        self.trail_cog = None
        self.trail_sog = None
        self.sanc = None
        self.sanc_key = None
        self.strait = None
        self.dark_since = None
        self.reg = 0
        self.suspect_t = 0.0
        self.suspect_sog = None         # last impossible reported speed, with when and where it was reported
        self.suspect_sog_t = 0.0        # (only real speed evidence: fixes suspect because they sit in a hotspot
        self.suspect_sog_lat = None     # do not refresh these)
        self.suspect_sog_lon = None
        self.reject_t = 0.0             # last fix dropped as an impossible jump (the vessel is still transmitting)
        self.quiet_since = None         # silent while stopped in an area of interest (anomalies.py)
        self.gap_ctx = None             # what the detector knew when it started watching a silence

    # -- derived ---------------------------------------------------------------------------------
    def suspect(self, now: float) -> bool:
        return now - self.suspect_t <= SUSPECT_HOLD_S
    @property
    def moving(self) -> bool:
        return self.sog is not None and self.sog >= MOVING_KN

    @property
    def sanctioned(self) -> bool:
        return bool(self.sanc and self.sanc.get("match") in ("imo", "mmsi"))

    @property
    def possible_sanction(self) -> bool:
        return bool(self.sanc and self.sanc.get("match") not in ("imo", "mmsi"))

    def regular(self) -> int:
        """Reports in the 30 minutes before the latest one (a proxy for "was reporting regularly")."""
        if not self.reports:
            return self.reg
        last = self.reports[-1]
        return sum(1 for t in self.reports if last - t <= 1800)

    def port_state(self) -> str:
        if self.moving:
            return "underway"
        if self.nav == 1:
            return "anchored"
        return "moored"


@dataclass
class Call:
    mmsi: int
    arr_t: float
    dep_t: float | None
    lat: float
    lon: float
    nav: int | None
    seen: bool                       # the arrival itself was observed (not just a vessel found stopped)


@dataclass
class Snapshot:
    t: float
    rows: list
    grid: geo.GridIndex
    by_mmsi: dict
    seq: int
    total: int
    counts: dict = field(default_factory=dict)


def _norm_name(text: str | None) -> str:
    return "".join(ch for ch in (text or "").upper() if ch.isalnum())


class VesselStore:
    def __init__(self, db=None, *, now: Callable[[], float] = time.time, lookup: Callable | None = None,
                 straits: StraitMonitor | None = None, log=None):
        self.db = db
        self.now = now
        self.lookup = lookup            # sanctions.lookup_vessel(imo=, mmsi=, name=)
        self.straits = straits if straits is not None else StraitMonitor()
        self.log = log
        self.lock = threading.RLock()
        self.vessels: dict[int, Vessel] = {}
        self.dirty: set[int] = set()
        self.pending_track: list[tuple] = []
        self.pending_calls: list[Call] = []
        self.pending_gaps: list[tuple] = []
        self.recent_gaps: deque[dict] = deque(maxlen=1000)     # gap episodes with context, for anomalies.py
        self.jumps: deque[tuple] = deque(maxlen=JUMPS_KEPT)    # (mmsi, t, lat0, lon0, lat1, lon1, sog, accepted)
        self.calls: deque[Call] = deque()
        self.sanctions_version = None
        self.snapshot: Snapshot = Snapshot(0.0, [], geo.GridIndex(1.0), {}, 0, 0)
        self._seq = 0
        self.source_last: dict[int, float] = {}
        self.hotspots: list[tuple[float, float, float]] = []    # (lat, lon, radius_km) of suspected spoofing
        self.stats = {"positions": 0, "statics": 0, "rejected_old": 0, "rejected_jump": 0, "rejected_bad": 0,
                      "track_points": 0, "calls": 0, "gaps": 0, "suspect": 0, "jumps": 0}

    def in_hotspot(self, lat: float, lon: float) -> bool:
        for hlat, hlon, radius in self.hotspots:
            if abs(lat - hlat) < 0.25 and geo.haversine_km(lat, lon, hlat, hlon) <= radius:
                return True
        return False

    # ------------------------------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------------------------------
    def position(self, mmsi: int, t: float, lat, lon, *, sog=None, cog=None, hdg=None, nav=None, rot=None,
                 src: int = 0, ais: str | None = None) -> bool:
        try:
            mmsi = int(mmsi)
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            self.stats["rejected_bad"] += 1
            return False
        if not is_ship_mmsi(mmsi) or not geo.valid_latlon(lat, lon) or (abs(lat) < 0.01 and abs(lon) < 0.01):
            self.stats["rejected_bad"] += 1
            return False
        now = self.now()
        if t is None or t > now + 60:
            t = now
        if t < now - 2 * 86400:
            self.stats["rejected_old"] += 1
            return False
        sog = _num(sog, 0.0, 102.2)
        cog = _num(cog, 0.0, 359.99)
        hdg = int(hdg) if isinstance(hdg, (int, float)) and 0 <= hdg < 360 else None
        nav = int(nav) if isinstance(nav, (int, float)) and 0 <= nav <= 15 else None
        with self.lock:
            v = self.vessels.get(mmsi)
            if v is None:
                v = Vessel(mmsi, t)
                self.vessels[mmsi] = v
                self._check_sanctions(v)
            elif v.t and t <= v.t + 0.5:
                self.stats["rejected_old"] += 1
                return False
            fast = sog is not None and sog > SUSPECT_KN and v.cls != "highspeed"
            suspect = fast or self.in_hotspot(lat, lon)
            prev_t = v.t
            prev_suspect = bool(prev_t) and v.suspect_t == prev_t
            if prev_t and v.lat is not None:
                dt = t - prev_t
                d = geo.haversine_km(v.lat, v.lon, lat, lon)
                implied = d / max(dt, 1.0) * 1943.84
                if d >= JUMP_KM and dt <= 1800 and implied > SUSPECT_KN \
                        and (v.cls != "highspeed" or implied > MAX_PLAUSIBLE_KN):
                    accepted = suspect or prev_suspect or dt >= 600 or implied <= MAX_PLAUSIBLE_KN
                    self.jumps.append((mmsi, t, v.lat, v.lon, lat, lon, sog, accepted))
                    self.stats["jumps"] += 1
                if not suspect and not prev_suspect and d > 2.0 and implied > MAX_PLAUSIBLE_KN:
                    if dt < 600:
                        # An impossible jump between two ordinary fixes: drop the new one. Jumps to or
                        # from a suspect fix are let through (flagged) so a vessel can recover from spoofing.
                        self.stats["rejected_jump"] += 1
                        v.reject_t = t
                        return False
                    suspect = True          # kept after a pause, but no ship moves that fast: flag it
            if (v.dark_since is not None or v.quiet_since is not None) and not suspect:
                self._record_gap(v, t, lat, lon)
            v.first_seen = min(v.first_seen, t)
            v.lat, v.lon, v.t = lat, lon, t
            v.sog, v.cog, v.hdg = sog, cog, hdg
            if nav is not None:
                v.nav = nav
            v.rot = rot if isinstance(rot, (int, float)) else None
            v.src = src
            v.srcs |= 1 << src
            if ais:
                v.ais = ais
            if not v.reports or t - v.reports[-1] >= 5:
                v.reports.append(t)
                if len(v.reports) > 10:
                    del v.reports[0]
            if suspect:
                # Keep what was reported (the card shows it) but don't let a corrupted fix create
                # port calls, trail points or strait transits.
                v.suspect_t = t
                if fast:
                    v.suspect_sog, v.suspect_sog_t = sog, t
                    v.suspect_sog_lat, v.suspect_sog_lon = lat, lon
                if v.strait is not None and self.straits is not None:
                    self.straits.leave(v.strait, mmsi)
                    v.strait = None
                self.stats["suspect"] = self.stats.get("suspect", 0) + 1
            else:
                self._update_state(v, t)
                self._maybe_trail(v, t)
                if self.straits is not None:
                    v.strait = self.straits.observe(mmsi, v.strait, lon, lat, t, sog, cog, v.cls)
            self.dirty.add(mmsi)
            self.source_last[src] = now
            self.stats["positions"] += 1
        return True

    def static(self, mmsi: int, t: float | None = None, *, src: int = 0, name=None, imo=None, callsign=None,
               ship_type=None, dest=None, eta=None, draught=None, length=None, beam=None, ais=None) -> bool:
        try:
            mmsi = int(mmsi)
        except (TypeError, ValueError):
            return False
        if not is_ship_mmsi(mmsi):
            return False
        now = self.now()
        t = now if t is None or t > now + 60 else t
        with self.lock:
            v = self.vessels.get(mmsi)
            if v is None:
                v = Vessel(mmsi, t)
                self.vessels[mmsi] = v
            changed_identity = False
            name = aislib.clean_text(name) if name is not None else None
            if name:
                if name != v.name:
                    changed_identity = True
                v.name = name
            if imo is not None:
                try:
                    imo_i = int(imo)
                except (TypeError, ValueError):
                    imo_i = 0
                if aislib.valid_imo(imo_i) and imo_i != v.imo:
                    v.imo = imo_i
                    changed_identity = True
            if callsign is not None:
                cs = aislib.clean_text(callsign).replace(" ", "")
                if cs:
                    v.callsign = cs[:10]
            if isinstance(ship_type, (int, float)) and 0 < int(ship_type) < 100:
                v.ship_type = int(ship_type)
                v.cls = classify(v.ship_type)
            if dest is not None:
                d = aislib.clean_text(dest)
                v.dest = d[:30]
            if eta is not None:
                v.eta = eta or None
            # 25.5 m is the field's ceiling ("25.5 m or more"); in practice it means "not set".
            if isinstance(draught, (int, float)) and 0 < draught < 25.45:
                v.draught = round(float(draught), 1)
            if isinstance(length, (int, float)) and 0 < length < 600:
                v.length = float(length)
            if isinstance(beam, (int, float)) and 0 < beam < 80:
                v.beam = float(beam)
            if ais:
                v.ais = ais
            v.t_static = max(v.t_static, t)
            v.srcs |= 1 << src
            if changed_identity or v.sanc_key is None:
                self._check_sanctions(v)
            self.dirty.add(mmsi)
            self.stats["statics"] += 1
        return True

    # -- motion state and port calls ----------------------------------------------------------------
    def _update_state(self, v: Vessel, t: float) -> None:
        sog = v.sog
        moving = sog is not None and sog >= MOVING_KN
        stopped = (sog is not None and sog < STOPPED_KN) or (sog is None and v.nav in (1, 5))
        if v.state is None:
            if moving:
                v.state, v.state_since = "underway", t
            elif stopped:
                v.state, v.state_since = "stationary", t
                v.stop_lat, v.stop_lon, v.stop_nav, v.stop_seen = v.lat, v.lon, v.nav, False
            return
        if v.state == "underway":
            if stopped:
                v.state, v.state_since = "stationary", t
                v.stop_lat, v.stop_lon, v.stop_nav, v.stop_seen = v.lat, v.lon, v.nav, True
            return
        # stationary
        if v.nav in (1, 5):
            v.stop_nav = v.nav
        moved = geo.haversine_km(v.stop_lat, v.stop_lon, v.lat, v.lon) if v.stop_lat is not None else 0.0
        if moving and (moved > 0.3 or (sog or 0) >= 2.0):
            if t - v.state_since >= CALL_MIN_S and v.stop_lat is not None:
                call = Call(v.mmsi, v.state_since, t, v.stop_lat, v.stop_lon, v.stop_nav, v.stop_seen)
                self.calls.append(call)
                self.pending_calls.append(call)
                self.stats["calls"] += 1
            v.state, v.state_since = "underway", t
            v.stop_lat = v.stop_lon = v.stop_nav = None
            v.stop_seen = False

    def _maybe_trail(self, v: Vessel, t: float) -> None:
        if v.trail_t is not None:
            dt = t - v.trail_t
            if dt < TRAIL_MIN_S:
                return
            d = geo.haversine_km(v.trail_lat, v.trail_lon, v.lat, v.lon)
            if v.moving:
                turned = v.cog is not None and v.trail_cog is not None and _angle(v.cog, v.trail_cog) >= 12
                sped = v.sog is not None and v.trail_sog is not None and abs(v.sog - v.trail_sog) >= 3
                if not (dt >= TRAIL_STRAIGHT_S or (d >= 0.3 and (turned or sped)) or d >= 15):
                    return
            elif not (dt >= TRAIL_STATIONARY_S or d >= 0.5):
                return
        v.trail_t, v.trail_lat, v.trail_lon, v.trail_cog, v.trail_sog = t, v.lat, v.lon, v.cog, v.sog
        self.pending_track.append((v.mmsi, int(t), round(v.lat * 1e5), round(v.lon * 1e5),
                                   None if v.sog is None else int(round(v.sog * 10)),
                                   None if v.cog is None else int(round(v.cog * 10))))
        self.stats["track_points"] += 1

    def _record_gap(self, v: Vessel, t: float, lat: float, lon: float) -> None:
        t0 = v.dark_since if v.dark_since is not None else v.quiet_since
        gap = (v.mmsi, t0, t, v.lat, v.lon, lat, lon)
        self.pending_gaps.append(gap)
        ctx = dict(v.gap_ctx or {})
        self.recent_gaps.append({
            "mmsi": v.mmsi, "t0": t0, "t1": t, "lat0": v.lat, "lon0": v.lon, "lat1": lat, "lon1": lon,
            "sog0": v.sog, "cog0": v.cog, "nav0": v.nav, "draught0": v.draught, "dest0": v.dest or "",
            "name0": v.name or "", "imo0": v.imo, "cls": v.cls, "src0": v.src,
            "stopped": v.dark_since is None, **{k: ctx[k] for k in ("n_last", "n_est", "flag_t") if k in ctx}})
        self.stats["gaps"] += 1
        v.dark_since = None
        v.quiet_since = None
        v.gap_ctx = None

    # -- sanctions -------------------------------------------------------------------------------------
    def _check_sanctions(self, v: Vessel) -> None:
        key = (v.imo, v.name, self.sanctions_version)
        if v.sanc_key == key:
            return
        v.sanc_key = key
        v.sanc = None
        if self.lookup is None:
            return
        try:
            hit = None
            if v.imo:
                hit = self.lookup(imo=str(v.imo))
            if hit is None:
                hit = self.lookup(mmsi=str(v.mmsi))
                if hit is not None and hit.get("imo") and v.imo and str(hit.get("imo")) != str(v.imo):
                    hit = dict(hit, match="mmsi_conflict")
            if hit is None and len(_norm_name(v.name)) >= 4:
                hit = self.lookup(name=v.name)
        except Exception as err:  # the sanctions module must never break ingestion
            if self.log:
                self.log.debug("sanctions lookup failed: %s", err)
            return
        if hit:
            v.sanc = {k: hit.get(k) for k in ("match", "uid", "name", "aliases", "linked_to", "imo", "mmsi", "flag",
                                               "vessel_type", "call_sign", "owner", "built", "tonnage", "grt",
                                               "lists", "programmes", "listings", "listed_on", "imos")}
            v.sanc["lists"] = list(hit.get("lists") or ["OFAC"])
            # The SDN programme field joins several programmes as "A] [B".
            programs = [p.strip(" []") for p in re.split(r"\]\s*\[", hit.get("program") or "") if p.strip(" []")]
            v.sanc["programs"] = programs
            v.sanc["program"] = ", ".join(programs)
            v.sanc["remarks"] = (hit.get("remarks") or "")[:700]

    def recheck_sanctions(self, version) -> int:
        """Re-match every vessel after the sanctions list (re)loads. Returns strong matches."""
        with self.lock:
            self.sanctions_version = version
            strong = 0
            for v in self.vessels.values():
                self._check_sanctions(v)
                strong += v.sanctioned
            return strong

    # ------------------------------------------------------------------------------------------
    # Snapshot for the live map, nearby and port activity
    # ------------------------------------------------------------------------------------------
    def build_snapshot(self) -> Snapshot:
        now = self.now()
        rows = []
        grid = geo.GridIndex(1.0)
        by_mmsi = {}
        counts = {"moving": 0, "stationary": 0}
        with self.lock:
            total = len(self.vessels)
            for v in self.vessels.values():
                if v.lat is None:
                    continue
                age = now - v.t
                moving = v.moving
                if age > (LIVE_MOVING_S if moving else LIVE_STATIONARY_S):
                    continue
                flags = 0
                if v.sanc:
                    flags |= F_SANCTIONED if v.sanctioned else F_POSSIBLE
                if v.ais == "B":
                    flags |= F_CLASS_B
                if age > (STALE_MOVING_S if moving else STALE_STATIONARY_S):
                    flags |= F_STALE
                if v.suspect(now):
                    flags |= F_SUSPECT
                direction = -1
                if moving:
                    if v.cog is not None:
                        direction = int(round(v.cog)) % 360
                    elif v.hdg is not None:
                        direction = v.hdg
                row = (v.mmsi, v.lat, v.lon, direction, CLASS_INDEX.get(v.cls, CLASS_INDEX["other"]), flags,
                       None if v.sog is None else int(round(v.sog * 10)), size_class(v.length), v.name, v.t,
                       moving, v.src, v.nav, v.strait, v.length, v.ship_type)
                rows.append(row)
                counts["moving" if moving else "stationary"] += 1
        for row in rows:
            grid.add(row[R_LAT], row[R_LON], row)
            by_mmsi[row[R_MMSI]] = row
        self._seq += 1
        snap = Snapshot(now, rows, grid, by_mmsi, self._seq, total, counts)
        self.snapshot = snap
        return snap

    # ------------------------------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------------------------------
    def get(self, mmsi: int) -> Vessel | None:
        with self.lock:
            return self.vessels.get(int(mmsi))

    def record(self, mmsi: int) -> dict | None:
        """A plain copy of one vessel's fields (taken under the lock)."""
        with self.lock:
            v = self.vessels.get(int(mmsi))
            if v is None:
                return None
            return {s: getattr(v, s) for s in Vessel.__slots__ if s not in ("reports", "sanc_key")} | \
                {"reports": list(v.reports), "regular": v.regular(), "sanctioned": v.sanctioned,
                 "possible_sanction": v.possible_sanction, "moving": v.moving, "port_state": v.port_state()}

    def recent_calls(self, since: float) -> list[Call]:
        with self.lock:
            return [c for c in self.calls if (c.dep_t or c.arr_t) >= since]

    def trail(self, mmsi: int, since: float) -> list[tuple]:
        """[(t, lat, lon, sog, cog)] oldest first: SQLite history, unsaved points and the latest fix."""
        mmsi = int(mmsi)
        points: dict[int, tuple] = {}
        if self.db is not None:
            for r in self.db.query("SELECT t, lat, lon, sog, cog FROM ships_track WHERE mmsi = ? AND t >= ? "
                                   "ORDER BY t LIMIT 5000", (mmsi, int(since))):
                points[int(r["t"])] = (int(r["t"]), r["lat"] / 1e5, r["lon"] / 1e5,
                                       None if r["sog"] is None else r["sog"] / 10.0,
                                       None if r["cog"] is None else r["cog"] / 10.0)
        with self.lock:
            for p in self.pending_track:
                if p[0] == mmsi and p[1] >= since:
                    points[p[1]] = (p[1], p[2] / 1e5, p[3] / 1e5, None if p[4] is None else p[4] / 10.0,
                                    None if p[5] is None else p[5] / 10.0)
            v = self.vessels.get(mmsi)
            if v is not None and v.lat is not None and v.t >= since:
                points[int(v.t)] = (int(v.t), v.lat, v.lon, v.sog, v.cog)
        return [points[k] for k in sorted(points)]

    def calls_for(self, mmsi: int, limit: int = 8) -> list[dict]:
        out: dict[float, dict] = {}
        if self.db is not None:
            for r in self.db.query("SELECT * FROM ships_calls WHERE mmsi = ? ORDER BY arr_t DESC LIMIT ?",
                                   (int(mmsi), limit)):
                out[r["arr_t"]] = r
        with self.lock:
            for c in self.calls:
                if c.mmsi == int(mmsi):
                    out[c.arr_t] = {"mmsi": c.mmsi, "arr_t": c.arr_t, "dep_t": c.dep_t, "lat": c.lat, "lon": c.lon,
                                    "nav": c.nav, "seen": int(c.seen)}
        return sorted(out.values(), key=lambda r: -r["arr_t"])[:limit]

    def gaps_for(self, mmsi: int, limit: int = 6) -> list[dict]:
        rows = {}
        if self.db is not None:
            for r in self.db.query("SELECT * FROM ships_gaps WHERE mmsi = ? ORDER BY t0 DESC LIMIT ?",
                                   (int(mmsi), limit)):
                rows[r["t0"]] = r
        with self.lock:
            for g in self.pending_gaps:
                if g[0] == int(mmsi):
                    rows[g[1]] = {"mmsi": g[0], "t0": g[1], "t1": g[2], "lat0": g[3], "lon0": g[4], "lat1": g[5],
                                  "lon1": g[6]}
        return sorted(rows.values(), key=lambda r: -r["t0"])[:limit]

    # ------------------------------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------------------------------
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS ships_vessels (
        mmsi INTEGER PRIMARY KEY, name TEXT, imo INTEGER, callsign TEXT, ship_type INTEGER, dest TEXT,
        eta TEXT, draught REAL, length REAL, beam REAL, ais TEXT, lat REAL, lon REAL, sog REAL, cog REAL,
        hdg INTEGER, nav INTEGER, t REAL, t_static REAL, first_seen REAL, src INTEGER, srcs INTEGER,
        state TEXT, state_since REAL, stop_lat REAL, stop_lon REAL, stop_nav INTEGER, stop_seen INTEGER,
        reg INTEGER, dark_since REAL);
    CREATE INDEX IF NOT EXISTS ships_vessels_t ON ships_vessels(t);
    CREATE TABLE IF NOT EXISTS ships_track (
        mmsi INTEGER NOT NULL, t INTEGER NOT NULL, lat INTEGER NOT NULL, lon INTEGER NOT NULL,
        sog INTEGER, cog INTEGER, PRIMARY KEY (mmsi, t)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS ships_track_t ON ships_track(t);
    CREATE TABLE IF NOT EXISTS ships_calls (
        mmsi INTEGER NOT NULL, arr_t REAL NOT NULL, dep_t REAL, lat REAL, lon REAL, nav INTEGER, seen INTEGER,
        PRIMARY KEY (mmsi, arr_t));
    CREATE INDEX IF NOT EXISTS ships_calls_dep ON ships_calls(dep_t);
    CREATE TABLE IF NOT EXISTS ships_gaps (
        mmsi INTEGER NOT NULL, t0 REAL NOT NULL, t1 REAL NOT NULL, lat0 REAL, lon0 REAL, lat1 REAL, lon1 REAL,
        PRIMARY KEY (mmsi, t0));
    CREATE INDEX IF NOT EXISTS ships_gaps_t1 ON ships_gaps(t1);
    CREATE TABLE IF NOT EXISTS ships_visits (
        strait TEXT NOT NULL, mmsi INTEGER NOT NULL, t_in REAL NOT NULL, t_out REAL NOT NULL, cls TEXT,
        moving INTEGER, dir INTEGER, PRIMARY KEY (strait, mmsi, t_in));
    CREATE INDEX IF NOT EXISTS ships_visits_t ON ships_visits(t_out);
    CREATE TABLE IF NOT EXISTS ships_samples (
        strait TEXT NOT NULL, t REAL NOT NULL, inside INTEGER, moving INTEGER, PRIMARY KEY (strait, t));
    """
    _COLS = ("mmsi", "name", "imo", "callsign", "ship_type", "dest", "eta", "draught", "length", "beam", "ais",
             "lat", "lon", "sog", "cog", "hdg", "nav", "t", "t_static", "first_seen", "src", "srcs", "state",
             "state_since", "stop_lat", "stop_lon", "stop_nav", "stop_seen", "reg", "dark_since")

    # Gap episode context (anomalies.py), added to older databases with ALTER TABLE.
    GAP_COLUMNS = {"sog0": "REAL", "cog0": "REAL", "draught0": "REAL", "draught1": "REAL", "dest0": "TEXT",
                   "dest1": "TEXT", "name0": "TEXT", "name1": "TEXT", "imo0": "INTEGER", "imo1": "INTEGER",
                   "aoi": "TEXT", "stopped": "INTEGER", "severity": "INTEGER", "info": "TEXT"}

    def create_tables(self) -> None:
        if self.db is not None:
            self.db.script(self.SCHEMA)
            have = {r["name"] for r in self.db.query("PRAGMA table_info(ships_gaps)")}
            for col, kind in self.GAP_COLUMNS.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE ships_gaps ADD COLUMN {col} {kind}")

    def load(self) -> int:
        """Vessels seen in the last 7 days, port calls of the last 48 hours and strait visits of the
        last 26 hours (the rest stays in SQLite for cards)."""
        if self.db is None:
            return 0
        now = self.now()
        rows = self.db.query("SELECT * FROM ships_vessels WHERE t >= ? OR t_static >= ?",
                             (now - KEEP_VESSELS_S, now - KEEP_VESSELS_S))
        calls = self.db.query("SELECT * FROM ships_calls WHERE arr_t >= ? OR dep_t >= ? ORDER BY dep_t",
                              (now - CALLS_IN_MEMORY_S, now - CALLS_IN_MEMORY_S))
        visits = self.db.query("SELECT * FROM ships_visits WHERE t_out >= ?", (now - 26 * 3600,))
        with self.lock:
            for r in rows:
                v = Vessel(int(r["mmsi"]), r["first_seen"] or r["t"] or now)
                for col in self._COLS[1:]:
                    if col in ("stop_seen",):
                        v.stop_seen = bool(r[col])
                    elif r.get(col) is not None:
                        setattr(v, col, r[col])
                v.t = float(r["t"] or 0.0)
                v.t_static = float(r["t_static"] or 0.0)
                v.src = int(r["src"] or 0)
                v.srcs = int(r["srcs"] or 0)
                v.reg = int(r["reg"] or 0)
                v.cls = classify(v.ship_type)
                if v.lat is not None and v.lon is not None:
                    v.trail_t, v.trail_lat, v.trail_lon, v.trail_cog, v.trail_sog = v.t, v.lat, v.lon, v.cog, v.sog
                    if self.straits is not None:
                        v.strait = self.straits.locate(v.lon, v.lat)
                self._check_sanctions(v)
                self.vessels[v.mmsi] = v
            for r in calls:
                if r["dep_t"] is None:
                    continue
                self.calls.append(Call(int(r["mmsi"]), r["arr_t"], r["dep_t"], r["lat"], r["lon"], r["nav"],
                                       bool(r["seen"])))
            if self.straits is not None:
                self.straits.restore(visits)
        return len(rows)

    def flush(self) -> dict:
        if self.db is None:
            return {}
        with self.lock:
            ids = list(self.dirty)
            self.dirty.clear()
            vrows = []
            for mmsi in ids:
                v = self.vessels.get(mmsi)
                if v is None:
                    continue
                v.reg = v.regular()
                vrows.append(tuple((1 if v.stop_seen else 0) if c == "stop_seen" else getattr(v, c)
                                   for c in self._COLS))
            track, self.pending_track = self.pending_track, []
            calls, self.pending_calls = self.pending_calls, []
            gaps, self.pending_gaps = self.pending_gaps, []
            visits = self.straits.take_pending() if self.straits is not None else []
        cols = ", ".join(self._COLS)
        marks = ", ".join("?" for _ in self._COLS)
        update = ", ".join(f"{c} = excluded.{c}" for c in self._COLS[1:])
        if vrows:
            self.db.executemany(f"INSERT INTO ships_vessels({cols}) VALUES({marks}) "
                                f"ON CONFLICT(mmsi) DO UPDATE SET {update}", vrows)
        if track:
            self.db.executemany("INSERT OR REPLACE INTO ships_track(mmsi, t, lat, lon, sog, cog) VALUES(?,?,?,?,?,?)",
                                track)
        if calls:
            self.db.executemany("INSERT OR REPLACE INTO ships_calls(mmsi, arr_t, dep_t, lat, lon, nav, seen) "
                                "VALUES(?,?,?,?,?,?,?)",
                                [(c.mmsi, c.arr_t, c.dep_t, c.lat, c.lon, c.nav, int(c.seen)) for c in calls])
        if gaps:
            self.db.executemany("INSERT INTO ships_gaps(mmsi, t0, t1, lat0, lon0, lat1, lon1) VALUES(?,?,?,?,?,?,?) "
                                "ON CONFLICT(mmsi, t0) DO UPDATE SET t1 = excluded.t1, lat0 = excluded.lat0, "
                                "lon0 = excluded.lon0, lat1 = excluded.lat1, lon1 = excluded.lon1", gaps)
        if visits:
            self.db.executemany("INSERT OR REPLACE INTO ships_visits(strait, mmsi, t_in, t_out, cls, moving, dir) "
                                "VALUES(?,?,?,?,?,?,?)",
                                [(v.sid, v.mmsi, v.t_in, v.t_last, v.cls, v.n_moving, v.dir_score) for v in visits])
        return {"vessels": len(vrows), "track": len(track), "calls": len(calls), "gaps": len(gaps),
                "visits": len(visits)}

    def prune(self) -> dict:
        now = self.now()
        removed = 0
        with self.lock:
            for mmsi in [m for m, v in self.vessels.items() if max(v.t, v.t_static) < now - KEEP_VESSELS_S]:
                del self.vessels[mmsi]
                self.dirty.discard(mmsi)
                removed += 1
            while self.calls and (self.calls[0].dep_t or self.calls[0].arr_t) < now - CALLS_IN_MEMORY_S:
                self.calls.popleft()
            if self.straits is not None:
                self.straits.sweep(now)
        out = {"vessels_removed": removed}
        if self.db is not None:
            self.db.execute("DELETE FROM ships_vessels WHERE t < ? AND t_static < ?",
                            (now - KEEP_VESSELS_S, now - KEEP_VESSELS_S))
            self.db.execute("DELETE FROM ships_track WHERE t < ?", (int(now - KEEP_TRACK_S),))
            self.db.execute("DELETE FROM ships_calls WHERE COALESCE(dep_t, arr_t) < ?", (now - KEEP_EVENTS_S,))
            self.db.execute("DELETE FROM ships_gaps WHERE t1 < ?", (now - KEEP_GAPS_S,))
            self.db.execute("DELETE FROM ships_visits WHERE t_out < ?", (now - KEEP_EVENTS_S - 86400,))
            self.db.execute("DELETE FROM ships_samples WHERE t < ?", (now - KEEP_EVENTS_S - 86400,))
        return out

    # ------------------------------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------------------------------
    def search(self, q: str, limit: int = 20) -> list[tuple[float, Vessel]]:
        q = (q or "").strip()
        if len(q) < 2:
            return []
        digits = q.replace(" ", "")
        out: list[tuple[float, Vessel]] = []
        with self.lock:
            if digits.isascii() and digits.isdigit():
                if len(digits) == 9:
                    v = self.vessels.get(int(digits))
                    if v is not None:
                        out.append((0.95, v))
                if len(digits) == 7:
                    imo = int(digits)
                    out.extend((0.9, v) for v in self.vessels.values() if v.imo == imo)
                if len(digits) >= 5 and not out:
                    out.extend((0.5, v) for v in self.vessels.values() if str(v.mmsi).startswith(digits))
                return sorted(out, key=lambda p: -p[0])[:limit]
            nq = _norm_name(q)
            if len(nq) < 2:
                return []
            live_after = self.now() - 3600
            for v in self.vessels.values():
                if not v.name and not v.callsign:
                    continue
                nn = _norm_name(v.name)
                score = 0.0
                if v.callsign and v.callsign.upper() == q.upper().replace(" ", ""):
                    score = 0.85
                elif nn == nq:
                    score = 0.82
                elif nn.startswith(nq):
                    score = 0.7
                elif len(nq) >= 3 and nq in nn:
                    score = 0.55
                if score:
                    if v.t >= live_after:          # live vessels first
                        score += 0.05
                    out.append((score, v))
        out.sort(key=lambda p: (-p[0], -p[1].t))
        return out[:limit]


def _num(value, lo: float, hi: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < lo or value > hi:
        return None
    return float(value)


def _angle(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return 360.0 - d if d > 180.0 else d


def flag_name(mmsi: int) -> tuple[str | None, str | None]:
    f = flag_of(mmsi)
    return (f[0], f[1]) if f else (None, None)
