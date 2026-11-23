"""Watchlist areas: validation, containment and intersection (CONTRACT.md sections 8 and 15.3).

An area is one of:

- ``polygon`` or ``rectangle``: a GeoJSON Polygon or MultiPolygon, usually from ``ctx.draw``. Shapes
  that cross the antimeridian keep continuous longitudes (for example 175 to 185); ``geo.point_in_geometry``
  handles them.
- ``circle``: a centre and a radius (``ctx.draw`` circles carry ``properties.center`` and ``radius_km``).
  Containment uses the great-circle distance, not the 72-point outline (the outline is kept for the map).
- ``place``: a named place from the geocoder plus a radius; stored as a circle.
- ``world``: everything.

Everything here is pure (no I/O), so the tests can exercise it directly.
"""

from __future__ import annotations

import math
from typing import Any

from meridian import geo

AREA_TYPES = ("polygon", "rectangle", "circle", "place", "world")
MAX_POSITIONS = 2000          # every ring of every polygon together
MAX_POLYGONS = 20
MAX_HOLES = 10
MAX_RADIUS_KM = 5000.0
MIN_RADIUS_KM = 0.1
CIRCLE_STEPS = 72
NAME_MAX = 120


class AreaError(ValueError):
    """A watchlist area that cannot be used (the message is shown to the user)."""


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------
def _num(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AreaError(f"{name} must be a number")
    v = float(value)
    if not math.isfinite(v):
        raise AreaError(f"{name} must be a finite number")
    return v


def _signed_area(ring: list) -> float:
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _unwrap_near(lon: float, ref: float) -> float:
    while lon - ref > 180.0:
        lon -= 360.0
    while lon - ref < -180.0:
        lon += 360.0
    return lon


def max_radius_km(lat: float) -> float:
    """A circle must stay off the poles: a ring cannot enclose one."""
    to_pole = math.radians(90.0 - abs(lat)) * geo.EARTH_RADIUS_KM
    return max(0.0, min(MAX_RADIUS_KM, to_pole - 1.0))


def ring_area_km2(ring: list) -> float:
    """Area of a closed ring on the sphere (Chamberlain and Duquette), km²."""
    n = len(ring)
    if n < 4:
        return 0.0
    total = 0.0
    for i in range(n):
        if i == n - 2:
            lo, mid, up = n - 2, n - 1, 0
        elif i == n - 1:
            lo, mid, up = n - 1, 0, 1
        else:
            lo, mid, up = i, i + 1, i + 2
        total += math.radians(ring[up][0] - ring[lo][0]) * math.sin(math.radians(ring[mid][1]))
    return abs(total * geo.EARTH_RADIUS_KM ** 2 / 2.0)


# ---------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------
def clean_ring(ring: Any) -> list[list[float]]:
    """Positions rounded to 5 decimals, repeats dropped, closed and counter-clockwise."""
    if not isinstance(ring, (list, tuple)) or len(ring) < 3:
        raise AreaError("each ring needs at least three positions")
    out: list[list[float]] = []
    for p in ring:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise AreaError("positions are [longitude, latitude] pairs")
        lon = _num(p[0], "longitude")
        lat = _num(p[1], "latitude")
        if not -90.0 <= lat <= 90.0:
            raise AreaError("latitudes must be between -90 and 90")
        if not -540.0 <= lon <= 540.0:
            raise AreaError("longitudes must be between -540 and 540")
        q = [round(lon, 5), round(lat, 5)]
        if not out or out[-1] != q:
            out.append(q)
    while len(out) > 1 and out[0] == out[-1]:
        out.pop()
    if len(out) < 3:
        raise AreaError("each ring needs at least three different positions")
    xs = [p[0] for p in out]
    if max(xs) - min(xs) > 360.0:
        raise AreaError("a shape may span at most 360 degrees of longitude")
    area = _signed_area(out)
    if abs(area) < 1e-10:
        raise AreaError("the shape has no area")
    if area < 0:
        out.reverse()
    out.append(list(out[0]))
    return out


def clean_geometry(value: Any) -> dict:
    """A GeoJSON Polygon or MultiPolygon (or a Feature holding one) -> a clean geometry dict."""
    g = value
    if isinstance(g, dict) and g.get("type") == "Feature":
        g = g.get("geometry")
    if not isinstance(g, dict):
        raise AreaError("the area must be a GeoJSON Polygon or MultiPolygon")
    kind = g.get("type")
    coords = g.get("coordinates")
    if kind == "Polygon":
        polys = [coords]
    elif kind == "MultiPolygon":
        polys = coords
    else:
        raise AreaError("the area must be a GeoJSON Polygon or MultiPolygon")
    if not isinstance(polys, (list, tuple)) or not polys:
        raise AreaError("the area has no coordinates")
    if len(polys) > MAX_POLYGONS:
        raise AreaError(f"an area may have at most {MAX_POLYGONS} parts")
    out = []
    total = 0
    for rings in polys:
        if not isinstance(rings, (list, tuple)) or not rings:
            raise AreaError("each polygon needs an outer ring")
        if len(rings) > MAX_HOLES + 1:
            raise AreaError(f"a polygon may have at most {MAX_HOLES} holes")
        total += sum(len(r) if isinstance(r, (list, tuple)) else 0 for r in rings)
        if total > MAX_POSITIONS + 2 * MAX_POLYGONS * (MAX_HOLES + 1):
            raise AreaError(f"an area may have at most {MAX_POSITIONS:,} points")
        out.append([clean_ring(r) for r in rings])
    if sum(len(r) for poly in out for r in poly) > MAX_POSITIONS:
        raise AreaError(f"an area may have at most {MAX_POSITIONS:,} points")
    # Keep the first vertex within ±180 (ctx.draw does the same); continuous longitudes stay.
    first = out[0][0][0][0]
    shift = 360.0 * round(first / 360.0) if abs(first) > 180.0 else 0.0
    if shift:
        out = [[[[round(x - shift, 5), y] for x, y in ring] for ring in poly] for poly in out]
    if len(out) == 1:
        return {"type": "Polygon", "coordinates": out[0]}
    return {"type": "MultiPolygon", "coordinates": out}


def circle_ring(lat: float, lon: float, radius_km: float, steps: int = CIRCLE_STEPS) -> list[list[float]]:
    """A geodesic circle as a closed, counter-clockwise ring with continuous longitudes."""
    lat = max(-89.9, min(89.9, lat))
    r = min(radius_km, max_radius_km(lat))
    pts = []
    for i in range(steps):
        plat, plon = geo.destination(lat, lon, -i * 360.0 / steps, r)
        pts.append([round(_unwrap_near(plon, lon), 5), round(plat, 5)])
    pts.append(list(pts[0]))
    return pts


def bbox_polygon(bbox: Any) -> dict | None:
    """[w, s, e, n] (west > east across the antimeridian) -> a Polygon geometry, or None."""
    try:
        w, s, e, n = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (w, s, e, n)) or s > n:
        return None
    if e < w:
        e += 360.0
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def circle_polygon(lat: float, lon: float, radius_km: float) -> dict | None:
    try:
        if not geo.valid_latlon(lat, lon) or not radius_km or radius_km <= 0:
            return None
        return {"type": "Polygon", "coordinates": [circle_ring(float(lat), float(lon), min(float(radius_km),
                                                                                           MAX_RADIUS_KM), 24)]}
    except (TypeError, ValueError):
        return None


def _clean_name(value: Any, limit: int = NAME_MAX) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AreaError("names must be text")
    return " ".join(value.split())[:limit]


def parse_area(spec: Any) -> "Area":
    """Validate an area as sent by the browser:

    ``{"type": "polygon"|"rectangle", "geometry": <Polygon|MultiPolygon or Feature>}``,
    ``{"type": "circle", "center": [lon, lat], "radius_km": r}`` (a ``geometry`` Feature with
    ``properties.center`` and ``radius_km`` also works), ``{"type": "place", "place": {"name", "lat",
    "lon"}, "radius_km": r}`` or ``{"type": "world"}``. Raises AreaError with a readable message."""
    if not isinstance(spec, dict):
        raise AreaError("the area must be an object with a type")
    kind = spec.get("type")
    if kind not in AREA_TYPES:
        raise AreaError(f"the area type must be one of {', '.join(AREA_TYPES)}")
    if kind == "world":
        return Area("world")
    if kind in ("polygon", "rectangle"):
        if spec.get("geometry") is None:
            raise AreaError("a drawn area needs its geometry")
        return Area(kind, geometry=clean_geometry(spec["geometry"]))
    # circles and places
    center = spec.get("center")
    geometry = spec.get("geometry")
    if center is None and isinstance(geometry, dict):
        center = (geometry.get("properties") or {}).get("center")
    radius = spec.get("radius_km")
    if radius is None and isinstance(geometry, dict):
        radius = (geometry.get("properties") or {}).get("radius_km")
    place = None
    if kind == "place":
        p = spec.get("place")
        if not isinstance(p, dict):
            raise AreaError("a place needs a name, lat and lon")
        lat = _num(p.get("lat"), "the place latitude")
        lon = _num(p.get("lon"), "the place longitude")
        name = _clean_name(p.get("name"))
        if not name:
            raise AreaError("a place needs a name")
        place = {"name": name, "lat": round(lat, 5), "lon": round(lon, 5)}
        center = [lon, lat]
    if not isinstance(center, (list, tuple)) or len(center) < 2:
        raise AreaError("a circle needs a centre [lon, lat]")
    clon = _num(center[0], "the centre longitude")
    clat = _num(center[1], "the centre latitude")
    if clon > 180.0 or clon < -180.0:
        clon = geo.wrap_lon(clon)
    if not geo.valid_latlon(clat, clon):
        raise AreaError("the centre is not a valid position")
    r = _num(radius, "the radius")
    if not MIN_RADIUS_KM <= r <= MAX_RADIUS_KM:
        raise AreaError(f"the radius must be between {MIN_RADIUS_KM:g} and {MAX_RADIUS_KM:,.0f} km")
    r = min(r, max(MIN_RADIUS_KM, max_radius_km(clat)))
    return Area(kind, center=(round(clat, 5), round(clon, 5)), radius_km=round(r, 3), place=place)


# ---------------------------------------------------------------------------------------------
# geometry tests
# ---------------------------------------------------------------------------------------------
def _outer_rings(geometry: Any) -> list[list]:
    """Outer rings ([[x, y], ...]) of a Polygon or MultiPolygon; [] for anything else."""
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    try:
        if kind == "Polygon":
            polys = [coords]
        elif kind == "MultiPolygon":
            polys = list(coords)
        elif kind == "GeometryCollection":
            return [r for g in geometry.get("geometries") or [] for r in _outer_rings(g)]
        else:
            return []
        out = []
        for poly in polys:
            if poly and len(poly[0]) >= 3:
                out.append([(float(p[0]), float(p[1])) for p in poly[0]])
        return out
    except (TypeError, ValueError, IndexError):
        return []


def _ring_bbox(ring: list) -> tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _boxes_overlap(a: tuple, b: tuple) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _orient(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_cross(p1, p2, q1, q2) -> bool:
    d1 = _orient(q1[0], q1[1], q2[0], q2[1], p1[0], p1[1])
    d2 = _orient(q1[0], q1[1], q2[0], q2[1], p2[0], p2[1])
    d3 = _orient(p1[0], p1[1], p2[0], p2[1], q1[0], q1[1])
    d4 = _orient(p1[0], p1[1], p2[0], p2[1], q2[0], q2[1])
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True

    def on(a, b, c, d):   # c on segment ab, given collinear
        return d == 0 and min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])

    return on(q1, q2, p1, d1) or on(q1, q2, p2, d2) or on(p1, p2, q1, d3) or on(p1, p2, q2, d4)


def _edges(ring: list, box: tuple) -> list:
    out = []
    for i in range(len(ring) - 1):
        a, b = ring[i], ring[i + 1]
        if _boxes_overlap((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), box):
            out.append((a, b))
    return out


def rings_intersect(a: list, b: list) -> bool:
    """Do two closed rings (continuous longitudes) overlap or touch? Tries b shifted by ±360."""
    if len(a) < 3 or len(b) < 3:
        return False
    abox = _ring_bbox(a)
    for shift in (0.0, 360.0, -360.0):
        bs = [(x + shift, y) for x, y in b] if shift else b
        bbox = _ring_bbox(bs)
        if not _boxes_overlap(abox, bbox):
            continue
        if any(geo.point_in_ring(x, y, a) for x, y in bs):
            return True
        if any(geo.point_in_ring(x, y, bs) for x, y in a):
            return True
        ea = _edges(a, bbox)
        eb = _edges(bs, abox)
        for p1, p2 in ea:
            for q1, q2 in eb:
                if _segments_cross(p1, p2, q1, q2):
                    return True
    return False


# ---------------------------------------------------------------------------------------------
# the area
# ---------------------------------------------------------------------------------------------
class Area:
    """A validated watchlist area. ``contains(lat, lon)`` and ``intersects(geometry)`` are the tests
    the engine uses; ``to_json()`` is what the API stores and returns."""

    def __init__(self, kind: str, *, geometry: dict | None = None, center: tuple[float, float] | None = None,
                 radius_km: float | None = None, place: dict | None = None):
        self.kind = kind
        self.place = place
        self.center = center                    # (lat, lon) for circles and places
        self.radius_km = radius_km
        if kind in ("circle", "place") and center is not None and radius_km is not None:
            geometry = {"type": "Polygon", "coordinates": [circle_ring(center[0], center[1], radius_km)]}
        self.geometry = geometry
        self.rings = _outer_rings(geometry) if geometry else []
        if kind == "world" or not self.rings:
            self.box = None                     # continuous (minx, miny, maxx, maxy)
        else:
            xs = [p[0] for r in self.rings for p in r]
            ys = [p[1] for r in self.rings for p in r]
            self.box = (min(xs), min(ys), max(xs), max(ys))
        if kind in ("circle", "place") and center is not None and radius_km is not None:
            self._dlat = math.degrees(radius_km / geo.EARTH_RADIUS_KM) + 1e-6
        else:
            self._dlat = None

    # -- tests ----------------------------------------------------------------------------------
    def contains(self, lat: Any, lon: Any) -> bool:
        if self.kind == "world":
            return geo.valid_latlon(lat, lon)
        try:
            la = float(lat)
            lo = float(lon)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(la) and math.isfinite(lo)):
            return False
        if self.center is not None and self.radius_km is not None:
            if abs(la - self.center[0]) > self._dlat:
                return False
            return geo.haversine_km(self.center[0], self.center[1], la, lo) <= self.radius_km + 1e-6
        if self.box is None:
            return False
        minx, miny, maxx, maxy = self.box
        if la < miny or la > maxy:
            return False
        if not any(minx <= x <= maxx for x in (lo, lo + 360.0, lo - 360.0)):
            return False
        return geo.point_in_geometry(lo, la, self.geometry)

    def intersects(self, geometry: Any) -> bool:
        """Does a Polygon or MultiPolygon (a storm cone, a wind area) touch this area?"""
        if self.kind == "world":
            return bool(_outer_rings(geometry))
        other = _outer_rings(geometry)
        if not other or not self.rings:
            return False
        return any(rings_intersect(a, b) for a in self.rings for b in other)

    def touches_line(self, coords: Any) -> bool:
        """Does a line ([[lon, lat], ...], e.g. a forecast track) enter or cross this area?"""
        try:
            pts = [(float(p[0]), float(p[1])) for p in coords or []]
        except (TypeError, ValueError, IndexError):
            return False
        if not pts:
            return False
        if self.kind == "world":
            return True
        if any(self.contains(y, x) for x, y in pts):
            return True
        if len(pts) < 2 or not self.rings:
            return False
        for ring in self.rings:
            rbox = _ring_bbox(ring)
            for shift in (0.0, 360.0, -360.0):
                line = [(x + shift, y) for x, y in pts]
                for i in range(len(line) - 1):
                    p1, p2 = line[i], line[i + 1]
                    seg = (min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1]))
                    if not _boxes_overlap(seg, rbox):
                        continue
                    for j in range(len(ring) - 1):
                        if _segments_cross(p1, p2, ring[j], ring[j + 1]):
                            return True
        return False

    # -- description ----------------------------------------------------------------------------
    def bbox(self) -> list[float] | None:
        """[west, south, east, north] wrapped into ±180 (west > east across the antimeridian)."""
        if self.box is None:
            return None
        minx, miny, maxx, maxy = self.box
        if maxx - minx >= 360.0:
            return [-180.0, round(miny, 5), 180.0, round(maxy, 5)]
        w = geo.wrap_lon(minx) if not -180.0 <= minx <= 180.0 else minx
        e = geo.wrap_lon(maxx) if not -180.0 <= maxx <= 180.0 else maxx
        if e == -180.0 and maxx > minx:
            e = 180.0
        return [round(w, 5), round(miny, 5), round(e, 5), round(maxy, 5)]

    def view_bbox(self) -> list[float] | None:
        """The continuous bbox (east may exceed 180): what map.fitBounds wants."""
        if self.box is None:
            return None
        return [round(v, 5) for v in self.box]

    def area_km2(self) -> float | None:
        if self.kind == "world":
            return None
        if self.center is not None and self.radius_km is not None:
            r = self.radius_km / geo.EARTH_RADIUS_KM
            return round(2 * math.pi * geo.EARTH_RADIUS_KM ** 2 * (1 - math.cos(r)), 1)
        total = 0.0
        g = self.geometry or {}
        polys = [g.get("coordinates")] if g.get("type") == "Polygon" else (g.get("coordinates") or [])
        for poly in polys:
            if not poly:
                continue
            total += ring_area_km2(poly[0]) - sum(ring_area_km2(h) for h in poly[1:])
        return round(max(0.0, total), 1)

    def summary(self) -> str:
        """Plain words: "50 km around Dover", "Polygon, 12,345 km²", "The whole world"."""
        if self.kind == "world":
            return "The whole world"
        if self.kind == "place" and self.place:
            return f"{_km(self.radius_km)} around {self.place['name']}"
        if self.kind == "circle":
            lat, lon = self.center
            return f"Circle, {_km(self.radius_km)} around {_latlon(lat, lon)}"
        size = self.area_km2() or 0
        word = "Rectangle" if self.kind == "rectangle" else "Polygon"
        return f"{word}, {_area(size)}"

    def to_json(self) -> dict:
        out: dict[str, Any] = {"type": self.kind, "summary": self.summary(), "bbox": self.bbox(),
                               "view_bbox": self.view_bbox(), "area_km2": self.area_km2()}
        if self.geometry is not None:
            out["geometry"] = self.geometry
        if self.center is not None:
            out["center"] = [self.center[1], self.center[0]]       # GeoJSON order: [lon, lat]
            out["radius_km"] = self.radius_km
        if self.place:
            out["place"] = dict(self.place)
        return out

    @staticmethod
    def from_json(data: dict) -> "Area":
        """The stored form (``to_json``) back to an Area; trusted input from our own table."""
        kind = data.get("type")
        if kind == "world":
            return Area("world")
        if kind in ("circle", "place"):
            c = data.get("center") or [0, 0]
            return Area(kind, center=(float(c[1]), float(c[0])), radius_km=float(data.get("radius_km") or 1),
                        place=data.get("place"))
        return Area(kind or "polygon", geometry=data.get("geometry"))


def _km(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:,.1f} km" if v < 10 else f"{v:,.0f} km"


def _area(km2: float) -> str:
    if km2 >= 100:
        return f"{km2:,.0f} km²"
    return f"{km2:,.1f} km²"


def _latlon(lat: float, lon: float) -> str:
    return f"{abs(lat):.2f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.2f}° {'E' if lon >= 0 else 'W'}"
