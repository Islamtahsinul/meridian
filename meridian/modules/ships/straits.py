"""Straits monitor: who is inside each strait now, and transits and unique vessels in 24 hours.

Polygons are hand-drawn approximations of each waterway (GeoJSON order, lon/lat), wide enough to
catch the shipping lanes and their anchorages. ``axis`` is the bearing of the main traffic flow;
a vessel moving within 50 degrees of the axis (either way) counts as transiting in that direction.
Crossing traffic (ferries running across the axis) is counted as inside but not as a transit.
"""

from __future__ import annotations

import time
from collections import deque

from meridian import geo

STRAITS: list[dict] = [
    {"id": "gibraltar", "name": "Strait of Gibraltar", "axis": 90,
     "pos": "Into the Mediterranean", "neg": "Into the Atlantic", "label": [-5.62, 35.96],
     "geometry": {"type": "Polygon", "coordinates": [[
         [-5.95, 35.80], [-5.60, 35.85], [-5.35, 35.90], [-5.33, 36.08], [-5.60, 36.03], [-5.95, 36.12],
         [-5.95, 35.80]]]}},
    {"id": "dover", "name": "Strait of Dover", "axis": 40,
     "pos": "Towards the North Sea", "neg": "Towards the Channel", "label": [1.45, 50.98],
     "geometry": {"type": "Polygon", "coordinates": [[
         [0.98, 50.90], [1.38, 51.20], [1.95, 51.02], [1.58, 50.72], [0.98, 50.90]]]}},
    {"id": "bosphorus", "name": "Bosphorus", "axis": 20,
     "pos": "Towards the Black Sea", "neg": "Towards the Sea of Marmara", "label": [29.06, 41.10],
     "geometry": {"type": "Polygon", "coordinates": [[
         [28.970, 40.990], [28.985, 41.020], [29.015, 41.045], [29.045, 41.090], [29.055, 41.120],
         [29.058, 41.170], [29.090, 41.240], [29.150, 41.230], [29.090, 41.180], [29.085, 41.120],
         [29.075, 41.090], [29.045, 41.040], [29.030, 41.010], [29.020, 40.990], [28.970, 40.990]]]}},
    {"id": "dardanelles", "name": "Dardanelles", "axis": 45,
     "pos": "Towards the Sea of Marmara", "neg": "Towards the Aegean", "label": [26.45, 40.22],
     "geometry": {"type": "Polygon", "coordinates": [[
         [26.17, 40.05], [26.37, 40.155], [26.38, 40.20], [26.64, 40.41], [26.72, 40.38], [26.67, 40.33],
         [26.42, 40.16], [26.22, 39.99], [26.17, 40.05]]]}},
    {"id": "suez", "name": "Suez Canal", "axis": 180,
     "pos": "Southbound", "neg": "Northbound", "label": [32.40, 30.45],
     "geometry": {"type": "Polygon", "coordinates": [[
         [32.22, 31.45], [32.27, 31.25], [32.28, 30.80], [32.24, 30.55], [32.30, 30.30], [32.45, 30.05],
         [32.48, 29.80], [32.62, 29.80], [32.60, 30.05], [32.50, 30.25], [32.42, 30.45], [32.35, 30.60],
         [32.36, 30.85], [32.36, 31.25], [32.42, 31.45], [32.22, 31.45]]]}},
    {"id": "bab_el_mandeb", "name": "Bab-el-Mandeb", "axis": 330,
     "pos": "Into the Red Sea", "neg": "Towards the Gulf of Aden", "label": [43.36, 12.78],
     "geometry": {"type": "Polygon", "coordinates": [[
         [42.95, 13.15], [43.28, 13.25], [43.52, 12.70], [43.62, 12.45], [43.38, 12.38], [43.20, 12.70],
         [42.95, 13.15]]]}},
    {"id": "hormuz", "name": "Strait of Hormuz", "axis": 310,
     "pos": "Into the Gulf", "neg": "Out of the Gulf", "label": [56.45, 26.55],
     "geometry": {"type": "Polygon", "coordinates": [[
         [56.10, 26.30], [56.40, 26.42], [56.70, 26.20], [56.85, 26.55], [56.55, 26.80], [56.25, 26.75],
         [56.10, 26.30]]]}},
    {"id": "malacca", "name": "Strait of Malacca", "axis": 125,
     "pos": "Southeast-bound", "neg": "Northwest-bound", "label": [101.62, 2.32],
     "geometry": {"type": "Polygon", "coordinates": [[
         [101.30, 2.90], [102.30, 2.15], [102.05, 1.65], [100.85, 2.25], [101.30, 2.90]]]}},
    {"id": "singapore", "name": "Singapore Strait", "axis": 80,
     "pos": "Eastbound", "neg": "Westbound", "label": [104.00, 1.25],
     "geometry": {"type": "Polygon", "coordinates": [[
         [103.55, 1.10], [103.85, 1.12], [104.10, 1.17], [104.45, 1.22], [104.45, 1.37], [104.05, 1.31],
         [103.85, 1.24], [103.62, 1.23], [103.55, 1.10]]]}},
    {"id": "panama", "name": "Panama Canal", "axis": 135,
     "pos": "Towards the Pacific", "neg": "Towards the Caribbean", "label": [-79.75, 9.15],
     "geometry": {"type": "Polygon", "coordinates": [[
         [-79.98, 9.42], [-79.85, 9.42], [-79.85, 9.25], [-79.65, 9.12], [-79.55, 8.98], [-79.42, 8.85],
         [-79.55, 8.80], [-79.66, 8.96], [-79.80, 9.10], [-79.98, 9.25], [-79.98, 9.42]]]}},
    {"id": "danish_straits", "name": "Øresund and Great Belt", "axis": 0,
     "pos": "Northbound", "neg": "Southbound", "label": [12.68, 56.02],
     "geometry": {"type": "MultiPolygon", "coordinates": [
         [[[12.45, 55.35], [12.95, 55.35], [13.05, 55.62], [12.80, 55.95], [12.72, 56.12], [12.55, 56.12],
           [12.58, 55.90], [12.50, 55.60], [12.45, 55.35]]],
         [[[10.75, 54.95], [11.25, 54.95], [11.25, 55.75], [10.80, 55.75], [10.75, 54.95]]]]}},
    {"id": "taiwan", "name": "Taiwan Strait", "axis": 30,
     "pos": "Northbound", "neg": "Southbound", "label": [119.60, 24.40],
     "geometry": {"type": "Polygon", "coordinates": [[
         [118.20, 24.30], [119.85, 25.45], [121.00, 25.15], [120.15, 23.35], [118.20, 24.30]]]}},
    {"id": "gulf_of_finland", "name": "Gulf of Finland, Porkkala to Paldiski", "axis": 90,
     "pos": "Into the Gulf of Finland", "neg": "Out to the Baltic", "label": [24.25, 59.70], "baltic": True,
     "geometry": {"type": "Polygon", "coordinates": [[
         [23.95, 59.40], [24.55, 59.47], [24.55, 60.02], [23.95, 59.98], [23.95, 59.40]]]}},
    {"id": "aland_sea", "name": "Åland Sea", "axis": 0,
     "pos": "Towards the Gulf of Bothnia", "neg": "Towards the Baltic", "label": [19.25, 60.10], "baltic": True,
     "geometry": {"type": "Polygon", "coordinates": [[
         [18.95, 59.85], [19.55, 59.85], [19.60, 60.40], [18.95, 60.40], [18.95, 59.85]]]}},
]

INSIDE_FRESH_S = 1800        # "inside now" means heard inside within 30 minutes
VISIT_GAP_S = 3600           # a visit ends after an hour without a report from inside
ALIGN_DEG = 50.0
TRANSIT_MIN_SOG = 3.0


def angle_diff(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return 360.0 - d if d > 180.0 else d


class Visit:
    __slots__ = ("sid", "mmsi", "t_in", "t_last", "n", "n_moving", "dir_score", "cls")

    def __init__(self, sid: str, mmsi: int, t: float, cls: str):
        self.sid = sid
        self.mmsi = mmsi
        self.t_in = t
        self.t_last = t
        self.n = 0
        self.n_moving = 0
        self.dir_score = 0
        self.cls = cls

    @property
    def transit(self) -> bool:
        return self.n_moving > 0

    @property
    def direction(self) -> int:
        return (self.dir_score > 0) - (self.dir_score < 0)


class StraitMonitor:
    """Not thread-safe by itself: the vessel store calls it while holding its own lock."""

    def __init__(self, defs: list[dict] | None = None):
        self.defs = [dict(d) for d in (defs or STRAITS)]
        for d in self.defs:
            d["bbox"] = geo.geometry_bbox(d["geometry"])
        self.by_id = {d["id"]: d for d in self.defs}
        self.active: dict[tuple[str, int], Visit] = {}
        self.closed: deque[Visit] = deque()
        self.pending: list[Visit] = []           # closed visits not yet written to SQLite

    # -- geometry ---------------------------------------------------------------------------------
    def locate(self, lon: float, lat: float) -> str | None:
        for d in self.defs:
            w, s, e, n = d["bbox"]
            if s <= lat <= n and w <= lon <= e and geo.point_in_geometry(lon, lat, d["geometry"]):
                return d["id"]
        return None

    # -- updates (called by the store under its lock) -------------------------------------------------
    def observe(self, mmsi: int, prev_sid: str | None, lon: float, lat: float, t: float, sog: float | None,
                cog: float | None, cls: str) -> str | None:
        sid = self.locate(lon, lat)
        if prev_sid and prev_sid != sid:
            self._close((prev_sid, mmsi))
        if sid is None:
            return None
        key = (sid, mmsi)
        visit = self.active.get(key)
        if visit is not None and t - visit.t_last > VISIT_GAP_S:
            self._close(key)
            visit = None
        if visit is None:
            visit = Visit(sid, mmsi, t, cls)
            self.active[key] = visit
        visit.t_last = max(visit.t_last, t)
        visit.n += 1
        visit.cls = cls
        if sog is not None and sog >= TRANSIT_MIN_SOG and cog is not None:
            axis = self.by_id[sid]["axis"]
            diff = angle_diff(cog, axis)
            if diff <= ALIGN_DEG:
                visit.dir_score += 1
                visit.n_moving += 1
            elif diff >= 180.0 - ALIGN_DEG:
                visit.dir_score -= 1
                visit.n_moving += 1
        return sid

    def leave(self, sid: str, mmsi: int) -> None:
        self._close((sid, mmsi))

    def _close(self, key: tuple[str, int]) -> None:
        visit = self.active.pop(key, None)
        if visit is None:
            return
        self.closed.append(visit)
        self.pending.append(visit)

    def sweep(self, now: float, keep_s: float = 26 * 3600) -> None:
        for key, visit in list(self.active.items()):
            if now - visit.t_last > VISIT_GAP_S:
                self._close(key)
        while self.closed and self.closed[0].t_last < now - keep_s:
            self.closed.popleft()

    def restore(self, rows: list[dict]) -> None:
        """Closed visits from SQLite (start-up)."""
        for r in sorted(rows, key=lambda r: r["t_out"]):
            v = Visit(r["strait"], int(r["mmsi"]), float(r["t_in"]), r.get("cls") or "other")
            v.t_last = float(r["t_out"])
            v.n_moving = int(r.get("moving") or 0)
            v.dir_score = int(r.get("dir") or 0)
            self.closed.append(v)

    def take_pending(self) -> list[Visit]:
        out, self.pending = self.pending, []
        return out

    # -- statistics ------------------------------------------------------------------------------------
    def visits_since(self, sid: str, since: float) -> list[Visit]:
        out = [v for v in self.closed if v.sid == sid and v.t_last >= since]
        out.extend(v for v in self.active.values() if v.sid == sid and v.t_last >= since)
        return out

    def inside(self, sid: str, now: float) -> list[Visit]:
        return [v for v in self.active.values() if v.sid == sid and now - v.t_last <= INSIDE_FRESH_S]

    def stats(self, sid: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        day = self.visits_since(sid, now - 86400)
        transits = [v for v in day if v.transit]
        by_cls: dict[str, int] = {}
        seen: set[int] = set()
        for v in day:
            if v.mmsi in seen:
                continue
            seen.add(v.mmsi)
            by_cls[v.cls] = by_cls.get(v.cls, 0) + 1
        transit_cls: dict[str, int] = {}
        for v in transits:
            transit_cls[v.cls] = transit_cls.get(v.cls, 0) + 1
        return {
            "transits_24h": len(transits),
            "transits_pos": sum(1 for v in transits if v.direction > 0),
            "transits_neg": sum(1 for v in transits if v.direction < 0),
            "unique_24h": len(seen),
            "unique_by_class": by_cls,
            "transits_by_class": transit_cls,
        }
