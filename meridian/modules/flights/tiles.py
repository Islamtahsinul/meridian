"""Cover a map view with point-radius queries (at most 250 nm each) on a fixed global grid, so tiles
are shared between views and cached while the map pans.

Three grid levels: 5.5, 2 and 0.75 degrees of latitude. Within a latitude band the column count is
chosen so cells are no wider (in km) than they are tall at the band's equatorward edge, which keeps
every cell's circumscribed circle under 250 nm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from meridian import geo

KM_PER_NM = 1.852
MAX_RADIUS_NM = 250
LEVELS = {0: 5.5, 1: 2.0, 2: 0.75}           # level -> latitude step in degrees
TILE_TTL = {0: 15.0, 1: 12.0, 2: 10.0, 9: 10.0}   # seconds a tile stays fresh (9 = ad-hoc circle)
WIDE_CENTRE_MIN_ZOOM = 4.5
LAT_LIMIT = 85.0


@dataclass(frozen=True)
class Tile:
    id: str
    level: int
    lat: float
    lon: float
    radius_nm: int

    @property
    def radius_km(self) -> float:
        return self.radius_nm * KM_PER_NM

    @property
    def ttl(self) -> float:
        return TILE_TTL.get(self.level, 10.0)


@dataclass
class Plan:
    mode: str                 # "tiles" or "wide"
    level: int | None
    tiles: list[Tile]
    needed: int               # tiles a full cover would have needed at the chosen level


def _ncols(step: float, row: int) -> int:
    lat0 = -90.0 + row * step
    lat1 = min(90.0, lat0 + step)
    edge = 0.0 if lat0 <= 0.0 <= lat1 else min(abs(lat0), abs(lat1))
    return max(1, int(math.ceil(360.0 * math.cos(math.radians(edge)) / step)))


def _row(step: float, lat: float) -> int:
    return int(math.floor((min(89.9999, max(-90.0, lat)) + 90.0) / step))


def tile_for(level: int, row: int, col: int) -> Tile:
    step = LEVELS[level]
    n = _ncols(step, row)
    col %= n
    lat0 = -90.0 + row * step
    lat1 = min(90.0, lat0 + step)
    lon_step = 360.0 / n
    lon0 = -180.0 + col * lon_step
    lon1 = lon0 + lon_step
    clat = (lat0 + lat1) / 2.0
    clon = geo.wrap_lon((lon0 + lon1) / 2.0)
    far = max(geo.haversine_km(clat, clon, la, lo) for la in (lat0, lat1) for lo in (lon0, lon1))
    radius_nm = int(min(MAX_RADIUS_NM, math.ceil(far * 1.02 / KM_PER_NM)))
    return Tile(f"L{level}:{row}:{col}", level, round(clat, 4), round(clon, 4), max(1, radius_nm))


def adhoc_tile(lat: float, lon: float, radius_nm: int) -> Tile:
    """A circle centred on a point (airport traffic, AI area queries)."""
    lat_r = round(lat, 2)
    lon_r = round(lon, 2)
    r = int(max(5, min(MAX_RADIUS_NM, radius_nm)))
    return Tile(f"P:{lat_r}:{lon_r}:{r}", 9, lat_r, lon_r, r)


def _col_ranges(step: float, row: int, w: float, e: float) -> list[tuple[int, int]]:
    n = _ncols(step, row)
    lon_step = 360.0 / n

    def col(lon: float) -> int:
        return min(n - 1, int(math.floor((lon + 180.0) / lon_step)))

    if e - w >= 359.999 or (w == -180.0 and e == 180.0):
        return [(0, n - 1)]
    if w <= e:
        return [(col(w), col(e))]
    return [(col(w), n - 1), (0, col(e))]


def tiles_for_bbox(level: int, bbox: tuple[float, float, float, float], limit: int = 10000) -> list[Tile]:
    w, s, e, n = bbox
    s = max(-LAT_LIMIT, s)
    n = min(LAT_LIMIT, n)
    step = LEVELS[level]
    out: list[Tile] = []
    seen = set()
    for row in range(_row(step, s), _row(step, n) + 1):
        for c0, c1 in _col_ranges(step, row, w, e):
            for c in range(c0, c1 + 1):
                t = tile_for(level, row, c)
                if t.id not in seen:
                    seen.add(t.id)
                    out.append(t)
                    if len(out) > limit:
                        return out
    return out


def count_for_bbox(level: int, bbox: tuple[float, float, float, float]) -> int:
    w, s, e, n = bbox
    step = LEVELS[level]
    total = 0
    for row in range(_row(step, max(-LAT_LIMIT, s)), _row(step, min(LAT_LIMIT, n)) + 1):
        for c0, c1 in _col_ranges(step, row, w, e):
            total += c1 - c0 + 1
    return total


def bbox_centre(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    w, s, e, n = bbox
    if w > e:
        e += 360.0
    return (s + n) / 2.0, geo.wrap_lon((w + e) / 2.0)


def plan(bbox: tuple[float, float, float, float], zoom: float, *, max_tiles: int = 6) -> Plan:
    """Smallest grid level that covers the view with at most 4 tiles; level 0 up to ``max_tiles``;
    otherwise a wide view: up to ``max_tiles`` level-0 tiles nearest the centre (from zoom 4.5)."""
    for level in (2, 1, 0):
        needed = count_for_bbox(level, bbox)
        if needed <= 4 or (level == 0 and needed <= max_tiles):
            return Plan("tiles", level, tiles_for_bbox(level, bbox), needed)
    needed = count_for_bbox(0, bbox)
    if zoom < WIDE_CENTRE_MIN_ZOOM or max_tiles <= 0:
        return Plan("wide", None, [], needed)
    clat, clon = bbox_centre(bbox)
    # Candidate tiles: a window around the centre (cheaper than enumerating a continent).
    span = LEVELS[0] * 2.5
    w, s, e, n = bbox
    window = (geo.wrap_lon(clon - span * 1.6), max(s, clat - span), geo.wrap_lon(clon + span * 1.6), min(n, clat + span))
    candidates = [t for t in tiles_for_bbox(0, window, limit=400)]
    candidates.sort(key=lambda t: geo.haversine_km(clat, clon, t.lat, t.lon))
    return Plan("wide", 0, candidates[:max_tiles], needed)


def circle_covers(tile: Tile, lat: float, lon: float, radius_km: float) -> bool:
    """True when the tile's circle contains the whole circle (lat, lon, radius_km)."""
    return geo.haversine_km(tile.lat, tile.lon, lat, lon) + radius_km <= tile.radius_km * 0.98
