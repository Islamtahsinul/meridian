"""In-memory aircraft state: merge every provider's observations by ICAO hex (freshest position
wins), keep bounded trails, flag coverage dropouts and dead-reckon them, and answer queries.

Thread safety: one re-entrant lock guards the aircraft table. Every public method takes it.
"""

from __future__ import annotations

import math
import re
import threading
import time
from array import array
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from meridian import geo

from .classify import classify
from .const import pretty_name
from .const import (AIRCRAFT_MAX, DB_LADD, DB_MILITARY, DB_PIA, EMERGENCY_SQUAWKS, EMERGENCY_STATUS_CODES,
                    EST_MAX_AGE, FLAG_EMG,
                    FLAG_EST, FLAG_GND, FLAG_LOCAL, FLAG_MIL, FLAG_MLAT, FLAG_PRIV, FLAG_SANC, KEEP_AFTER_LAST,
                    LIVE_MAX_AGE, LIVE_MAX_AGE_SLOW, TRAIL_MAX_AGE, TRAIL_MAX_POINTS, TRAIL_MIN_STEP,
                    TRAIL_POINTS_MAX_TOTAL)

_STATIC = ("cat", "reg", "typ", "desc", "ownop", "year", "db_flags")
_REG_NORM = re.compile(r"[^A-Z0-9]")


class Aircraft:
    __slots__ = ("hex", "lat", "lon", "alt", "alt_geom", "gnd", "gs", "trk", "vr", "pos_t", "seen_t", "pos_src",
                 "ptype", "cs", "sq", "emg", "cat", "reg", "typ", "desc", "ownop", "year", "db_flags", "nac_p",
                 "nic", "version", "gps_ok_before", "first_t", "sources", "cls", "shp", "sz", "sanc",
                 "sanc_checked", "sanc_event_t", "trail", "trail_t0", "trail_last_trk", "dropout_t",
                 "emg_since", "emg_last", "emg_event_t", "emg_code", "static_sig", "updated_t", "slow")

    def __init__(self, hexid: str, now: float):
        self.hex = hexid
        self.lat = self.lon = None
        self.alt = self.alt_geom = None
        self.gnd = False
        self.gs = self.trk = self.vr = None
        self.pos_t = 0.0
        self.seen_t = 0.0
        self.pos_src = None
        self.ptype = None
        self.cs = self.sq = self.emg = None
        self.cat = self.reg = self.typ = self.desc = self.ownop = self.year = None
        self.db_flags = 0
        self.nac_p = self.nic = self.version = None
        self.gps_ok_before = None
        self.first_t = now
        self.sources: dict[str, float] = {}
        self.cls, self.shp, self.sz = "unknown", "generic", 0.74
        self.sanc = None
        self.sanc_checked = 0.0
        self.sanc_event_t = 0.0
        self.trail = array("f")
        self.trail_t0 = 0.0
        self.trail_last_trk = None
        self.dropout_t = 0.0
        self.emg_since = None
        self.emg_last = 0.0
        self.emg_event_t = 0.0
        self.emg_code = None
        self.static_sig = None
        self.updated_t = now
        self.slow = False          # last position came from a once-a-minute worldwide poll

    # -- derived state ---------------------------------------------------------------------------
    @property
    def military(self) -> bool:
        return bool(self.db_flags & DB_MILITARY) or self.cls == "military"

    @property
    def private(self) -> bool:
        return bool(self.db_flags & (DB_LADD | DB_PIA))

    def emergency_code(self) -> str | None:
        """'7700' / 'general' / ... while an emergency squawk or status is being reported."""
        if self.sq in EMERGENCY_SQUAWKS:
            return self.sq
        if self.emg in EMERGENCY_STATUS_CODES:
            return self.emg
        return None

    def title(self) -> str:
        return self.cs or self.reg or self.hex.upper()

    def estimable(self) -> bool:
        if self.gnd or self.gs is None or self.trk is None or self.gs < 80:
            return False
        if self.alt is not None:
            if self.alt < 3000:
                return False
            if self.alt < 12000 and (self.vr or 0) < -500:
                return False   # probably landing, not lost
        return True

    def display(self, now: float) -> tuple[float, float, bool] | None:
        """(lat, lon, estimated) for the map, or None when the aircraft should not be drawn."""
        if self.lat is None:
            return None
        age = now - self.pos_t
        if age <= (LIVE_MAX_AGE_SLOW if self.slow else LIVE_MAX_AGE):
            return self.lat, self.lon, False
        if age <= EST_MAX_AGE and self.dropout_t > self.pos_t and self.estimable():
            lat, lon = estimate(self.lat, self.lon, self.trk, self.gs, age)
            return lat, lon, True
        return None

    def flags(self, est: bool) -> int:
        f = 0
        if est:
            f |= FLAG_EST
        if self.military:
            f |= FLAG_MIL
        if self.emergency_code():
            f |= FLAG_EMG
        if self.sanc:
            f |= FLAG_SANC
        if self.gnd:
            f |= FLAG_GND
        if self.ptype == "mlat" or self.ptype == "opensky_mlat":
            f |= FLAG_MLAT
        if self.private:
            f |= FLAG_PRIV
        if self.pos_src == "local":
            f |= FLAG_LOCAL
        return f

    def row(self, lat: float, lon: float, est: bool, now: float) -> list:
        return [self.hex, round(lat, 5), round(lon, 5),
                int(round(self.trk)) % 360 if self.trk is not None and self.shp not in ("balloon",) else None,
                self.alt, int(round(self.gs)) if self.gs is not None else None, self.vr, self.cs or "",
                self.reg or "", self.typ or "", self.cls, self.shp, self.sz, self.flags(est),
                int(max(0.0, now - self.pos_t)), self.sq or ""]

    def entity(self, now: float, *, layer: str = "flights") -> dict:
        disp = self.display(now)
        lat, lon = (disp[0], disp[1]) if disp else (self.lat, self.lon)
        sub = ", ".join(x for x in (pretty_name(self.desc) or self.typ, self.reg) if x) or "Aircraft"
        ent = {"kind": "aircraft", "id": self.hex, "title": self.title(), "subtitle": sub, "layer": layer,
               "time": round(self.pos_t or self.seen_t)}
        if lat is not None:
            ent["lat"] = round(lat, 5)
            ent["lon"] = round(lon, 5)
        return ent

    def summary(self, now: float) -> dict:
        """Compact JSON for AI tools and search detail."""
        disp = self.display(now)
        out = {"hex": self.hex, "callsign": self.cs, "registration": self.reg, "type": self.typ,
               "description": self.desc, "class": self.cls, "military": self.military,
               "altitude_ft": self.alt, "on_ground": self.gnd, "ground_speed_kt": _r(self.gs),
               "track_deg": _r(self.trk), "vertical_rate_fpm": self.vr, "squawk": self.sq,
               "emergency": self.emergency_code(), "position_age_s": int(now - self.pos_t) if self.pos_t else None,
               "source": self.pos_src}
        if disp:
            out.update({"lat": round(disp[0], 5), "lon": round(disp[1], 5), "estimated": disp[2]})
        if self.sanc:
            out["sanctions"] = {"program": self.sanc.get("program"), "operator": self.sanc.get("operator"),
                                "match": self.sanc.get("match")}
        return out

    # -- trail -----------------------------------------------------------------------------------
    def trail_points(self) -> list[tuple[float, float, float, int | None]]:
        t = self.trail
        out = []
        for i in range(0, len(t), 4):
            alt = t[i + 3]
            out.append((self.trail_t0 + t[i], t[i + 1], t[i + 2], None if math.isnan(alt) else int(alt)))
        return out

    def add_trail_point(self, ts: float, lat: float, lon: float, alt: int | None, trk: float | None,
                        force: bool = False) -> bool:
        t = self.trail
        n = len(t) // 4
        if n == 0:
            self.trail_t0 = ts
        else:
            last_t = self.trail_t0 + t[-4]
            if ts <= last_t + 0.5:
                return False
            if not force:
                turned = (trk is not None and self.trail_last_trk is not None
                          and abs(((trk - self.trail_last_trk) + 180.0) % 360.0 - 180.0) >= 8.0)
                if ts - last_t < TRAIL_MIN_STEP and not (turned and ts - last_t >= 4.0):
                    return False
        t.extend((ts - self.trail_t0, lat, lon, float("nan") if alt is None else float(alt)))
        self.trail_last_trk = trk
        if n + 1 > TRAIL_MAX_POINTS:
            del t[:4 * (n + 1 - TRAIL_MAX_POINTS)]
        return True

    def prune_trail(self, now: float) -> None:
        t = self.trail
        if not t:
            return
        cutoff = now - TRAIL_MAX_AGE - self.trail_t0
        k = 0
        while k < len(t) and t[k] < cutoff:
            k += 4
        if k:
            del t[:k]
        if t and t[0] > 600.0:          # re-base so float32 offsets stay precise
            shift = t[0]
            self.trail_t0 += shift
            for i in range(0, len(t), 4):
                t[i] -= shift

    def merge_trail(self, points: Iterable[tuple[float, float, float, int | None]], now: float) -> int:
        """Prepend older points (from a provider trace) before the first recorded point."""
        existing = self.trail_points()
        first = existing[0][0] if existing else float("inf")
        older = [p for p in points if now - TRAIL_MAX_AGE <= p[0] < first - 1.0]
        if not older:
            return 0
        self.trail = array("f")
        self.trail_t0 = older[0][0]
        self.trail_last_trk = None
        added = 0
        for p in older:
            if self.add_trail_point(p[0], p[1], p[2], p[3], None, force=False):
                added += 1
        for p in existing:
            self.add_trail_point(p[0], p[1], p[2], p[3], None, force=True)
        return added

    # -- merge -----------------------------------------------------------------------------------
    def update(self, o: dict, source: str, now: float, slow: bool = False) -> bool:
        """Apply an observation. Returns True when the position moved forward in time."""
        moved = False
        pos_t = o.get("pos_t")
        if "lat" in o and pos_t is not None and pos_t > self.pos_t + 0.2 and pos_t <= now + 30:
            self.lat = o["lat"]
            self.lon = o["lon"]
            self.pos_t = pos_t
            self.pos_src = source
            self.slow = slow
            self.ptype = o.get("ptype", self.ptype)
            if "alt" in o:
                self.alt = o["alt"]
                self.gnd = bool(o.get("gnd", False))
            if "alt_geom" in o:
                self.alt_geom = o["alt_geom"]
            self.gs = o.get("gs", self.gs)
            self.trk = o.get("trk", self.trk)
            self.vr = o.get("vr", self.vr)
            for key in ("nac_p", "nic", "version"):
                if key in o:
                    setattr(self, key, o[key])
            self.gps_ok_before = o.get("gps_ok_before")
            if not o.get("stale_pos"):
                self.add_trail_point(pos_t, self.lat, self.lon, self.alt, self.trk)
            moved = True
        seen_t = o.get("seen_t") or pos_t or now
        if seen_t >= self.seen_t - 0.5:
            self.seen_t = max(self.seen_t, seen_t)
            if "sq" in o:
                self.sq = o["sq"]
            if "emg" in o:
                self.emg = o["emg"]
            if o.get("cs"):
                self.cs = o["cs"]
            if "lat" not in o and "alt" in o and not moved:
                self.alt = o["alt"]
                self.gnd = bool(o.get("gnd", False))
        for key in _STATIC:
            v = o.get(key)
            if v:
                setattr(self, key, v)
        self.sources[source] = now
        self.updated_t = now
        return moved

    def reclassify(self) -> bool:
        sig = (self.cat, self.typ, self.desc, self.db_flags, self.cs, self.reg, self.ownop)
        if sig == self.static_sig:
            return False
        self.static_sig = sig
        self.cls, self.shp, self.sz = classify(category=self.cat, typ=self.typ, desc=self.desc,
                                               db_flags=self.db_flags, callsign=self.cs, reg=self.reg,
                                               ownop=self.ownop)
        return True


def _r(v):
    return None if v is None else round(v, 1)


def estimate(lat: float, lon: float, trk: float, gs_kt: float, age_s: float) -> tuple[float, float]:
    """Dead reckoning along the last track at the last ground speed."""
    return geo.destination(lat, lon, trk, gs_kt * 1.852 * age_s / 3600.0)


def norm_reg(text: str | None) -> str:
    return _REG_NORM.sub("", (text or "").upper())


@dataclass
class IngestResult:
    hexes: set = field(default_factory=set)
    events: list = field(default_factory=list)
    new: int = 0


class Tracker:
    def __init__(self, *, sanctions_lookup: Callable[[str, str | None], dict | None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.lock = threading.RLock()
        self.aircraft: dict[str, Aircraft] = {}
        self.sanctions_lookup = sanctions_lookup or (lambda hexid, reg: None)
        self.clock = clock
        self.sanctions_epoch = 0.0     # bump to force sanctions re-checks
        self.stats = {"ingested": 0, "pruned": 0}

    # -- writes ----------------------------------------------------------------------------------
    def ingest(self, observations: Iterable[dict], source: str, now: float | None = None, *,
               slow: bool = False) -> IngestResult:
        now = self.clock() if now is None else now
        res = IngestResult()
        with self.lock:
            for o in observations:
                hexid = o.get("hex")
                if not hexid:
                    continue
                ac = self.aircraft.get(hexid)
                if ac is None:
                    if len(self.aircraft) >= AIRCRAFT_MAX:
                        self._evict(now)
                    ac = self.aircraft[hexid] = Aircraft(hexid, now)
                    res.new += 1
                ac.update(o, source, now, slow)
                res.hexes.add(hexid)
                changed = ac.reclassify()
                if changed or ac.sanc_checked < self.sanctions_epoch or now - ac.sanc_checked > 1800:
                    self._check_sanctions(ac, now)
                self._check_events(ac, now, res.events)
            self.stats["ingested"] += len(res.hexes)
        return res

    def _check_sanctions(self, ac: Aircraft, now: float) -> None:
        try:
            hit = self.sanctions_lookup(ac.hex, ac.reg)
        except Exception:
            hit = None
        if hit is False:            # the list is not loaded yet
            return
        ac.sanc_checked = now
        ac.sanc = hit or None

    def _check_events(self, ac: Aircraft, now: float, events: list) -> None:
        code = ac.emergency_code()
        if code and ac.lat is not None and now - ac.pos_t <= LIVE_MAX_AGE:
            if ac.emg_since is None or code != ac.emg_code:
                ac.emg_since = now
                ac.emg_code = code
                ac.emg_event_t = now
                events.append(("emergency", ac.hex, now, True))
            elif now - ac.emg_event_t >= 60:
                ac.emg_event_t = now
                events.append(("emergency", ac.hex, now, False))
            ac.emg_last = now
        elif ac.emg_since is not None and now - ac.emg_last > 600:
            ac.emg_since = None
            ac.emg_code = None
        if ac.sanc and ac.lat is not None and now - ac.pos_t <= LIVE_MAX_AGE and now - ac.sanc_event_t > 6 * 3600:
            ac.sanc_event_t = now
            events.append(("sanctioned", ac.hex, now, True))

    def mark_dropouts(self, lat: float, lon: float, radius_km: float, present: set, fetched_at: float,
                      source: str | None = None) -> int:
        """After a successful circle query: aircraft last seen inside the circle that the answer did
        not include have left coverage (or landed, or switched off). Only aircraft last reported by
        the same network count: feeder networks differ, so another network's miss proves nothing."""
        r = radius_km * 0.95
        n = 0
        with self.lock:
            dlat = math.degrees(r / geo.EARTH_RADIUS_KM)
            for ac in self.aircraft.values():
                if ac.hex in present or ac.lat is None or ac.pos_t >= fetched_at - 5:
                    continue
                if source is not None and ac.pos_src != source:
                    continue
                if abs(ac.lat - lat) > dlat:
                    continue
                if geo.haversine_km(lat, lon, ac.lat, ac.lon) <= r:
                    if ac.dropout_t < fetched_at:
                        ac.dropout_t = fetched_at
                        n += 1
        return n

    def mark_set_dropouts(self, predicate: Callable[[Aircraft], bool], present: set, fetched_at: float,
                          source: str | None = None) -> int:
        """After a worldwide set (military, squawk) answer: members that vanished have left coverage."""
        n = 0
        with self.lock:
            for ac in self.aircraft.values():
                if ac.hex in present or ac.lat is None or ac.pos_t >= fetched_at - 5:
                    continue
                if source is not None and ac.pos_src != source:
                    continue
                if predicate(ac) and ac.dropout_t < fetched_at:
                    ac.dropout_t = fetched_at
                    n += 1
        return n

    def refresh_sanctions(self) -> None:
        with self.lock:
            self.sanctions_epoch = self.clock()

    def prune(self, now: float | None = None, keep: set | None = None) -> int:
        now = self.clock() if now is None else now
        removed = 0
        total_points = 0
        with self.lock:
            for hexid in list(self.aircraft):
                ac = self.aircraft[hexid]
                last = max(ac.pos_t, ac.seen_t)
                if now - last > KEEP_AFTER_LAST and not (keep and hexid in keep):
                    del self.aircraft[hexid]
                    removed += 1
                    continue
                ac.prune_trail(now)
                total_points += len(ac.trail) // 4
            if total_points > TRAIL_POINTS_MAX_TOTAL:
                # Drop the trails of the aircraft updated longest ago until under the cap.
                for ac in sorted(self.aircraft.values(), key=lambda a: a.updated_t):
                    if total_points <= TRAIL_POINTS_MAX_TOTAL * 0.8:
                        break
                    total_points -= len(ac.trail) // 4
                    ac.trail = array("f")
            self.stats["pruned"] += removed
        return removed

    def _evict(self, now: float) -> None:
        oldest = sorted(self.aircraft.values(), key=lambda a: max(a.pos_t, a.seen_t))[: max(1, AIRCRAFT_MAX // 20)]
        for ac in oldest:
            self.aircraft.pop(ac.hex, None)

    # -- reads -----------------------------------------------------------------------------------
    def get(self, hexid: str) -> Aircraft | None:
        with self.lock:
            return self.aircraft.get(hexid)

    def __len__(self) -> int:
        return len(self.aircraft)

    def displayed(self, now: float, *, bbox: tuple | None = None,
                  where: Callable[[Aircraft], bool] | None = None) -> list[tuple[Aircraft, float, float, bool]]:
        out = []
        with self.lock:
            for ac in self.aircraft.values():
                if ac.lat is None:
                    continue
                if where is not None and not where(ac):
                    continue
                disp = ac.display(now)
                if disp is None:
                    continue
                lat, lon, est = disp
                if bbox is not None and not geo.in_bbox(lat, lon, bbox):
                    continue
                out.append((ac, lat, lon, est))
        return out

    def rows(self, items: list[tuple[Aircraft, float, float, bool]], now: float, limit: int) -> tuple[list, bool]:
        def priority(item):
            ac, _, _, est = item
            p = 0
            if ac.emergency_code():
                p -= 4
            if ac.sanc:
                p -= 3
            if ac.military:
                p -= 1
            if est:
                p += 1
            return p, now - ac.pos_t
        truncated = len(items) > limit
        if truncated:
            items = sorted(items, key=priority)[:limit]
        with self.lock:
            # draw order: higher (later) rows on top; emergencies and sanctioned last
            items = sorted(items, key=lambda it: (bool(it[0].emergency_code()) or bool(it[0].sanc),
                                                  it[0].alt if it[0].alt is not None else -1))
            return [ac.row(lat, lon, est, now) for ac, lat, lon, est in items], truncated

    def near(self, lat: float, lon: float, radius_km: float, now: float, *, live_only: bool = True
             ) -> list[tuple[float, Aircraft, float, float, bool]]:
        dlat = math.degrees(radius_km / geo.EARTH_RADIUS_KM) + 0.01
        out = []
        with self.lock:
            for ac in self.aircraft.values():
                if ac.lat is None or abs(ac.lat - lat) > dlat + 3:
                    continue
                disp = ac.display(now)
                if disp is None or (live_only and disp[2]):
                    continue
                d = geo.haversine_km(lat, lon, disp[0], disp[1])
                if d <= radius_km:
                    out.append((d, ac, disp[0], disp[1], disp[2]))
        out.sort(key=lambda t: t[0])
        return out

    def search(self, q: str, limit: int, now: float) -> list[dict]:
        text = (q or "").strip().upper()
        if len(text) < 2:
            return []
        compact = norm_reg(text)
        hex_q = text.lower() if re.fullmatch(r"[0-9A-F]{6}", text) else None
        hits = []
        with self.lock:
            for ac in self.aircraft.values():
                if now - max(ac.pos_t, ac.seen_t) > EST_MAX_AGE:
                    continue
                score = 0.0
                if hex_q and ac.hex == hex_q:
                    score = 0.95
                elif ac.cs and ac.cs == compact:
                    score = 0.9
                elif ac.reg and norm_reg(ac.reg) == compact:
                    score = 0.9
                elif len(compact) >= 3 and ac.cs and ac.cs.startswith(compact):
                    score = 0.7
                elif len(compact) >= 3 and ac.reg and norm_reg(ac.reg).startswith(compact):
                    score = 0.6
                elif ac.typ and ac.typ == text and len(text) >= 3:
                    score = 0.45
                if score:
                    hits.append((score, ac))
            hits.sort(key=lambda t: (-t[0], t[1].cs or ""))
            out = []
            for score, ac in hits[:limit]:
                ent = ac.entity(now)
                alt = "on the ground" if ac.gnd else (f"{ac.alt:,} ft" if ac.alt is not None else None)
                ent["subtitle"] = ", ".join(x for x in (pretty_name(ac.desc) or ac.typ, ac.reg, alt) if x) or "Aircraft"
                ent["score"] = score
                ent["zoom"] = 9
                out.append(ent)
        return out

    def counts(self, now: float) -> dict:
        live = est = mil = emg = sanc = 0
        with self.lock:
            for ac in self.aircraft.values():
                disp = ac.display(now)
                if disp is None:
                    continue
                if disp[2]:
                    est += 1
                else:
                    live += 1
                if ac.military:
                    mil += 1
                if ac.emergency_code():
                    emg += 1
                if ac.sanc:
                    sanc += 1
            total = len(self.aircraft)
        return {"tracked": total, "live": live, "estimated": est, "military": mil, "emergency": emg,
                "sanctioned": sanc}

    def snapshot_seen(self, since: float) -> list[tuple]:
        """Rows for the last-seen table: aircraft whose position changed since ``since``."""
        out = []
        with self.lock:
            for ac in self.aircraft.values():
                if ac.lat is None or ac.pos_t <= since:
                    continue
                out.append((ac.hex, ac.cs or "", ac.reg or "", ac.typ or "", ac.desc or "", round(ac.lat, 5),
                            round(ac.lon, 5), ac.alt, ac.pos_t, ac.pos_src or "", ac.cls))
        return out


def static_info(ac: Aircraft) -> dict[str, Any]:
    return {"registration": ac.reg, "type": ac.typ, "description": ac.desc, "year": ac.year,
            "db_owner": ac.ownop, "db_flags": ac.db_flags}
