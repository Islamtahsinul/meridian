"""Small geodesy helpers shared by every module (CONTRACT.md section 8). Pure functions only."""

from __future__ import annotations

import math
from typing import Any, Iterable, Iterator

EARTH_RADIUS_KM = 6371.0088
KM_PER_NM = 1.852


def valid_latlon(lat: Any, lon: Any) -> bool:
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat_f) and math.isfinite(lon_f) and -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0


def wrap_lon(lon: float) -> float:
    return ((lon + 180.0) % 360.0) - 180.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def destination(lat: float, lon: float, bearing: float, dist_km: float) -> tuple[float, float]:
    """Point reached from (lat, lon) travelling ``dist_km`` on initial ``bearing`` (degrees)."""
    d = dist_km / EARTH_RADIUS_KM
    b = math.radians(bearing)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), wrap_lon(math.degrees(l2))


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    """``"west,south,east,north"`` -> floats. West may exceed east (crossing the antimeridian)."""
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 4:
        raise ValueError("bbox needs four numbers: west,south,east,north")
    w, s, e, n = (float(p) for p in parts)
    for v in (w, s, e, n):
        if not math.isfinite(v):
            raise ValueError("bbox values must be finite")
    if not (-90.0 <= s <= 90.0 and -90.0 <= n <= 90.0 and s <= n):
        raise ValueError("bbox latitudes must satisfy -90 <= south <= north <= 90")
    if e - w >= 360.0:
        w, e = -180.0, 180.0
    w = wrap_lon(w) if not -180.0 <= w <= 180.0 else w
    e = wrap_lon(e) if not -180.0 <= e <= 180.0 else e
    return w, s, e, n


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    w, s, e, n = bbox
    if not (s <= lat <= n):
        return False
    if w <= e:
        return w <= lon <= e
    return lon >= w or lon <= e


def bbox_of_circle(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    dlat = math.degrees(radius_km / EARTH_RADIUS_KM)
    coslat = max(0.01, math.cos(math.radians(lat)))
    dlon = min(180.0, dlat / coslat)
    return (wrap_lon(lon - dlon), max(-90.0, lat - dlat), wrap_lon(lon + dlon), min(90.0, lat + dlat))


def point_in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _lon_candidates(lon: float, ring: list) -> list[float]:
    """``lon``, plus ``lon ± 360`` when the ring itself runs past ±180. Shapes drawn across the
    antimeridian keep continuous longitudes (e.g. 175 to 185, CONTRACT.md section 15.3)."""
    out = [lon]
    try:
        xs = [float(p[0]) for p in ring]
        if max(xs) > 180.0:
            out.append(lon + 360.0)
        if min(xs) < -180.0:
            out.append(lon - 360.0)
    except (TypeError, ValueError, IndexError):
        pass
    return out


def point_in_polygon(lon: float, lat: float, polygon: list) -> bool:
    """GeoJSON Polygon coordinates: outer ring then holes. Rings that cross the antimeridian with
    continuous longitudes (beyond ±180) are handled."""
    if not polygon:
        return False
    for x in _lon_candidates(lon, polygon[0]):
        if point_in_ring(x, lat, polygon[0]) and not any(point_in_ring(x, lat, hole) for hole in polygon[1:]):
            return True
    return False


def point_in_geometry(lon: float, lat: float, geometry: dict) -> bool:
    if not geometry:
        return False
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return point_in_polygon(lon, lat, coords)
    if kind == "MultiPolygon":
        return any(point_in_polygon(lon, lat, poly) for poly in coords)
    if kind == "GeometryCollection":
        return any(point_in_geometry(lon, lat, g) for g in geometry.get("geometries") or [])
    return False


def geometry_bbox(geometry: dict) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def walk(c):
        if not c:
            return
        if isinstance(c[0], (int, float)):
            xs.append(float(c[0]))
            ys.append(float(c[1]))
        else:
            for item in c:
                walk(item)

    if geometry.get("type") == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            b = geometry_bbox(g)
            if b:
                xs.extend([b[0], b[2]])
                ys.extend([b[1], b[3]])
    else:
        walk(geometry.get("coordinates"))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


class GridIndex:
    """Bucket points into ``cell_deg`` cells for fast radius and bbox queries."""

    def __init__(self, cell_deg: float = 1.0):
        self.cell = float(cell_deg)
        self.cells: dict[tuple[int, int], list[tuple[float, float, Any]]] = {}
        self.size = 0

    def _key(self, lat: float, lon: float) -> tuple[int, int]:
        return int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell))

    def add(self, lat: float, lon: float, item: Any) -> None:
        self.cells.setdefault(self._key(lat, lon), []).append((lat, lon, item))
        self.size += 1

    def extend(self, rows: Iterable[tuple[float, float, Any]]) -> None:
        for lat, lon, item in rows:
            self.add(lat, lon, item)

    def near(self, lat: float, lon: float, radius_km: float, limit: int | None = None) -> list[tuple[float, Any]]:
        dlat = math.degrees(radius_km / EARTH_RADIUS_KM)
        coslat = max(0.01, math.cos(math.radians(lat)))
        dlon = min(180.0, dlat / coslat)
        r0 = int(math.floor((lat - dlat) / self.cell))
        r1 = int(math.floor((lat + dlat) / self.cell))
        c0 = int(math.floor((lon - dlon) / self.cell))
        c1 = int(math.floor((lon + dlon) / self.cell))
        ncols = int(round(360.0 / self.cell))
        found: list[tuple[float, Any]] = []
        seen_cols = set()
        for c in range(c0, c1 + 1):
            col = ((c + ncols // 2) % ncols) - ncols // 2 if ncols else c
            if col in seen_cols:
                continue
            seen_cols.add(col)
            for r in range(r0, r1 + 1):
                for plat, plon, item in self.cells.get((r, col), ()):
                    d = haversine_km(lat, lon, plat, plon)
                    if d <= radius_km:
                        found.append((d, item))
        found.sort(key=lambda t: t[0])
        return found[:limit] if limit else found

    def in_bbox(self, bbox: tuple[float, float, float, float]) -> Iterator[Any]:
        w, s, e, n = bbox
        r0 = int(math.floor(s / self.cell))
        r1 = int(math.floor(n / self.cell))
        if w <= e:
            col_ranges = [(int(math.floor(w / self.cell)), int(math.floor(e / self.cell)))]
        else:
            col_ranges = [(int(math.floor(w / self.cell)), int(math.floor(180.0 / self.cell))),
                          (int(math.floor(-180.0 / self.cell)), int(math.floor(e / self.cell)))]
        for c0, c1 in col_ranges:
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    for plat, plon, item in self.cells.get((r, c), ()):
                        if in_bbox(plat, plon, bbox):
                            yield item
