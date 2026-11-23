"""Geometry helpers for the intel module (pure Python, no dependencies): line simplification,
scanline raster masks, grid-cell outlines, areas, lengths and a country point-in-polygon index."""

from __future__ import annotations

import math
from typing import Iterable

from meridian import geo

KM_PER_DEG_LAT = 110.574


# ---------------------------------------------------------------------------------------------
# Simplification and rounding
# ---------------------------------------------------------------------------------------------
def _perp(p, a, b) -> float:
    x, y, x1, y1, x2, y2 = p[0], p[1], a[0], a[1], b[0], b[1]
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def simplify(points: list, tolerance: float) -> list:
    """Douglas-Peucker on [x, y] points (degrees). Keeps the end points."""
    n = len(points)
    if n < 3 or tolerance <= 0:
        return [list(p[:2]) for p in points]
    keep = bytearray(n)
    keep[0] = keep[n - 1] = 1
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        best, idx = -1.0, -1
        a, b = points[i], points[j]
        for k in range(i + 1, j):
            d = _perp(points[k], a, b)
            if d > best:
                best, idx = d, k
        if best > tolerance:
            keep[idx] = 1
            stack.append((i, idx))
            stack.append((idx, j))
    return [list(points[k][:2]) for k in range(n) if keep[k]]


def simplify_ring(ring: list, tolerance: float, digits: int = 4) -> list | None:
    if len(ring) < 4:
        return None
    pts = simplify(ring, tolerance)
    pts = [[round(x, digits), round(y, digits)] for x, y in pts]
    dedup = [pts[0]]
    for p in pts[1:]:
        if p != dedup[-1]:
            dedup.append(p)
    if dedup[0] != dedup[-1]:
        dedup.append(list(dedup[0]))
    return dedup if len(dedup) >= 4 else None


def simplify_geometry(geom: dict, tolerance: float, digits: int = 4) -> dict | None:
    """Simplify a Polygon or MultiPolygon. Tiny rings vanish; returns None if nothing is left."""
    kind = geom.get("type")
    polys = [geom.get("coordinates")] if kind == "Polygon" else (geom.get("coordinates") or [])
    out = []
    for poly in polys:
        if not poly:
            continue
        outer = simplify_ring(poly[0], tolerance, digits)
        if outer is None:
            continue
        holes = [h for h in (simplify_ring(r, tolerance, digits) for r in poly[1:]) if h]
        out.append([outer] + holes)
    if not out:
        return None
    return {"type": "Polygon", "coordinates": out[0]} if len(out) == 1 else {"type": "MultiPolygon", "coordinates": out}


def polygons_of(geom: dict) -> list:
    kind = geom.get("type")
    if kind == "Polygon":
        return [geom.get("coordinates") or []]
    if kind == "MultiPolygon":
        return geom.get("coordinates") or []
    return []


# ---------------------------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------------------------
def ring_area_km2(ring: list) -> float:
    """Approximate area of a ring (equirectangular around its mean latitude)."""
    if len(ring) < 4:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    kx = 111.320 * math.cos(math.radians(lat0))
    s = 0.0
    for p, q in zip(ring, ring[1:]):
        s += (p[0] * kx) * (q[1] * KM_PER_DEG_LAT) - (q[0] * kx) * (p[1] * KM_PER_DEG_LAT)
    return abs(s) / 2.0


def line_length_km(coords: list) -> float:
    return sum(geo.haversine_km(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:]))


def centroid(points: Iterable) -> tuple[float, float] | None:
    xs = ys = 0.0
    n = 0
    for p in points:
        xs += p[0]
        ys += p[1]
        n += 1
    return (xs / n, ys / n) if n else None


# ---------------------------------------------------------------------------------------------
# Raster masks
# ---------------------------------------------------------------------------------------------
def _runs(buf, value: int = 1):
    """(start, end) of runs of ``value`` in a bytes-like buffer (C-speed scans)."""
    other = 0 if value else 1
    pos, n = 0, len(buf)
    while pos < n:
        a = buf.find(value, pos)
        if a < 0:
            return
        b = buf.find(other, a)
        if b < 0:
            b = n
        yield a, b
        pos = b


class Grid:
    """A byte mask over a lon/lat box with square cells of ``res`` degrees."""

    def __init__(self, west: float, south: float, east: float, north: float, res: float):
        self.w, self.s, self.res = float(west), float(south), float(res)
        self.nx = max(1, int(math.ceil((east - west) / res)))
        self.ny = max(1, int(math.ceil((north - south) / res)))
        self.cells = bytearray(self.nx * self.ny)

    def copy_empty(self) -> "Grid":
        g = Grid.__new__(Grid)
        g.w, g.s, g.res, g.nx, g.ny = self.w, self.s, self.res, self.nx, self.ny
        g.cells = bytearray(self.nx * self.ny)
        return g

    def fill_polygon(self, polygon: list, value: int = 1) -> None:
        """Even-odd scanline fill of one polygon (outer ring plus holes) at cell centres."""
        rows: dict[int, list[float]] = {}
        s, w, res, ny, nx = self.s, self.w, self.res, self.ny, self.nx
        for ring in polygon:
            for p, q in zip(ring, ring[1:]):
                x1, y1, x2, y2 = p[0], p[1], q[0], q[1]
                if y1 == y2:
                    continue
                lo, hi = (y1, y2) if y1 < y2 else (y2, y1)
                r0 = max(0, int(math.ceil((lo - s) / res - 0.5)))
                r1 = min(ny - 1, int(math.ceil((hi - s) / res - 0.5)) - 1)
                if r1 < r0:
                    continue
                slope = (x2 - x1) / (y2 - y1)
                for r in range(r0, r1 + 1):
                    yc = s + (r + 0.5) * res
                    rows.setdefault(r, []).append(x1 + (yc - y1) * slope)
        fill = bytes([value])
        for r, xs in rows.items():
            xs.sort()
            base = r * nx
            for i in range(0, len(xs) - 1, 2):
                c0 = max(0, int(math.ceil((xs[i] - w) / res - 0.5)))
                c1 = min(nx - 1, int(math.ceil((xs[i + 1] - w) / res - 0.5)) - 1)
                if c1 >= c0:
                    self.cells[base + c0: base + c1 + 1] = fill * (c1 - c0 + 1)

    def fill_geometry(self, geom: dict, value: int = 1) -> None:
        for poly in polygons_of(geom):
            self.fill_polygon(poly, value)

    def index(self, lon: float, lat: float) -> int:
        c = int((lon - self.w) / self.res)
        r = int((lat - self.s) / self.res)
        if 0 <= c < self.nx and 0 <= r < self.ny:
            return r * self.nx + c
        return -1

    def get(self, lon: float, lat: float) -> int:
        i = self.index(lon, lat)
        return self.cells[i] if i >= 0 else 0

    def count(self) -> int:
        return self.cells.count(1)

    def area_km2(self) -> float:
        total = 0.0
        nx = self.nx
        for r in range(self.ny):
            n = self.cells[r * nx:(r + 1) * nx].count(1)
            if n:
                lat = self.s + (r + 0.5) * self.res
                total += n * (self.res * KM_PER_DEG_LAT) * (self.res * 111.320 * math.cos(math.radians(lat)))
        return total

    def eroded(self, k: int) -> "Grid":
        """Square erosion by ``k`` cells (a cell survives if every cell within k is set)."""
        if k <= 0:
            g = self.copy_empty()
            g.cells[:] = self.cells
            return g
        nx, ny = self.nx, self.ny
        horiz = bytearray(nx * ny)
        one = b"\x01"
        for r in range(ny):
            row = self.cells[r * nx:(r + 1) * nx]
            for a, b in _runs(row):   # runs touching the grid edge shrink too (edge = outside)
                a2, b2 = a + k, b - k
                if b2 > a2:
                    horiz[r * nx + a2: r * nx + b2] = one * (b2 - a2)
        out = self.copy_empty()
        for c in range(nx):
            col = horiz[c::nx]
            newcol = bytearray(ny)
            for a, b in _runs(col):
                a2, b2 = a + k, b - k
                if b2 > a2:
                    newcol[a2:b2] = one * (b2 - a2)
            out.cells[c::nx] = newcol
        return out


# ---------------------------------------------------------------------------------------------
# Grid-cell union outlines (risk zones)
# ---------------------------------------------------------------------------------------------
def cell_outline(cells: set[tuple[int, int]], size: float) -> list[list[list[float]]]:
    """Closed boundary rings ([[lon, lat], ...]) of a set of (row, col) cells of ``size`` degrees.
    Row r spans lat [r*size, (r+1)*size]; col c spans lon [c*size, (c+1)*size]."""
    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for r, c in cells:
        # counter-clockwise around the cell; keep edges whose neighbour is outside the set
        if (r - 1, c) not in cells:
            edges.setdefault((c, r), []).append((c + 1, r))
        if (r, c + 1) not in cells:
            edges.setdefault((c + 1, r), []).append((c + 1, r + 1))
        if (r + 1, c) not in cells:
            edges.setdefault((c + 1, r + 1), []).append((c, r + 1))
        if (r, c - 1) not in cells:
            edges.setdefault((c, r + 1), []).append((c, r))
    rings = []
    while edges:
        start = next(iter(edges))
        ring = [start]
        cur = start
        while True:
            nxt_list = edges.get(cur)
            if not nxt_list:
                break
            nxt = nxt_list.pop()
            if not nxt_list:
                del edges[cur]
            ring.append(nxt)
            cur = nxt
            if cur == start:
                break
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(_drop_collinear(ring))
    return [[[round(x * size, 5), round(y * size, 5)] for x, y in ring] for ring in rings]


def _drop_collinear(ring: list[tuple[int, int]]) -> list[tuple[int, int]]:
    pts = ring[:-1]
    out = []
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) != 0:
            out.append(b)
    if not out:
        return ring
    out.append(out[0])
    return out


def components(cells: set[tuple[int, int]]) -> list[set[tuple[int, int]]]:
    """8-connected components of a set of grid cells."""
    left = set(cells)
    out = []
    while left:
        seed = left.pop()
        comp = {seed}
        stack = [seed]
        while stack:
            r, c = stack.pop()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    n = (r + dr, c + dc)
                    if n in left:
                        left.discard(n)
                        comp.add(n)
                        stack.append(n)
        out.append(comp)
    return out


# ---------------------------------------------------------------------------------------------
# Country point-in-polygon index
# ---------------------------------------------------------------------------------------------
class PolygonIndex:
    """Features with bboxes bucketed into 1-degree cells; ``lookup`` returns the smallest match."""

    def __init__(self, items: list[tuple[str, dict, dict]], cell: float = 1.0):
        # items: (key, geometry, meta)
        self.cell = cell
        self.items = []
        self.grid: dict[tuple[int, int], list[int]] = {}
        for key, geom, meta in items:
            bb = geo.geometry_bbox(geom)
            if not bb:
                continue
            idx = len(self.items)
            area = (bb[2] - bb[0]) * (bb[3] - bb[1])
            self.items.append((key, geom, meta, bb, area))
            for r in range(int(math.floor(bb[1] / cell)), int(math.floor(bb[3] / cell)) + 1):
                for c in range(int(math.floor(bb[0] / cell)), int(math.floor(bb[2] / cell)) + 1):
                    self.grid.setdefault((r, c), []).append(idx)

    def __len__(self) -> int:
        return len(self.items)

    def lookup(self, lat: float, lon: float) -> tuple[str, dict] | None:
        cand = self.grid.get((int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell))), [])
        hits = []
        for i in cand:
            key, geom, meta, bb, area = self.items[i]
            if bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3] and geo.point_in_geometry(lon, lat, geom):
                hits.append((area, key, meta))
        if not hits:
            return None
        hits.sort(key=lambda h: h[0])
        return hits[0][1], hits[0][2]

    def nearest(self, lat: float, lon: float, max_km: float = 30.0) -> tuple[str, dict] | None:
        """The feature with a vertex closest to the point (coastal points just offshore)."""
        hit = self.lookup(lat, lon)
        if hit:
            return hit
        best = None
        r0, c0 = int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell))
        seen = set()
        for r in (r0 - 1, r0, r0 + 1):
            for c in (c0 - 1, c0, c0 + 1):
                for i in self.grid.get((r, c), []):
                    if i in seen:
                        continue
                    seen.add(i)
                    key, geom, meta, bb, area = self.items[i]
                    for poly in polygons_of(geom):
                        for x, y in poly[0][::2]:
                            if abs(y - lat) > 0.5 or abs(x - lon) > 0.8:
                                continue
                            d = geo.haversine_km(lat, lon, y, x)
                            if d <= max_km and (best is None or d < best[0]):
                                best = (d, key, meta)
        return (best[1], best[2]) if best else None
