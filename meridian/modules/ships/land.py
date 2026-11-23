"""Land and coastline for the anomaly detectors: "is this position on land, and how far from the coast?".

Reads the Natural Earth 1:50m admin-0 country shapes the intel module already builds
(``data/build/intel/countries.geojson``, public domain, simplified for display: the coast is good to
about a kilometre). Read-only and optional: without the file every answer is ``None`` and the
detectors say the land check was unavailable.

Edges are bucketed into 0.25 degree cells. Whether a point is on land is answered from the cell
centre (one full point-in-polygon test per cell, cached) plus the edges between the centre and the
point; the distance to the coast is the nearest edge in the surrounding cells. Limits: country
borders count as edges, so a point inland near a border reads as close to the "coast" (the check is
conservative); large lakes are inside the country shapes and read as land, so river and lake craft are
excluded by the detectors from their own track instead.
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

CELL = 0.25
KM_LAT = 110.57
KM_LON = 111.32
RECHECK_S = 600.0


def countries_path(data_dir) -> Path:
    return Path(data_dir) / "build" / "intel" / "countries.geojson"


class LandIndex:
    def __init__(self, path: Path | None = None, *, geojson: dict | None = None):
        self.path = Path(path) if path else None
        self._geojson = geojson
        self._lock = threading.Lock()
        self._loaded = False
        self._checked = 0.0
        self._mtime = None
        self.polys: list = []
        self.edges: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
        self._centre: dict[tuple[int, int], bool] = {}
        self.error: str | None = None

    # -- loading -------------------------------------------------------------------------------
    @property
    def available(self) -> bool:
        self._ensure()
        return bool(self.polys)

    def _ensure(self) -> None:
        now = time.monotonic()
        if self._loaded and (self.path is None or self._geojson is not None or now - self._checked < RECHECK_S):
            return
        with self._lock:
            if self._loaded and (self.path is None or self._geojson is not None or now - self._checked < RECHECK_S):
                return
            self._checked = now
            if self._geojson is not None or self.path is None:
                self._build(self._geojson or {})
                self._loaded = True
                return
            try:
                st = self.path.stat()
            except OSError:
                if not self.polys:
                    self.error = "The land shapes (intel countries.geojson) are not built."
                self._loaded = True
                return
            if self._loaded and st.st_mtime == self._mtime:
                return
            try:
                if st.st_size > 60_000_000:
                    raise ValueError("unexpectedly large")
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as err:
                self.error = f"The land shapes could not be read ({type(err).__name__})."
                self._loaded = True
                return
            self._build(data)
            self._mtime = st.st_mtime
            self._loaded = True

    def _build(self, data: dict) -> None:
        polys = []
        edges: dict[tuple[int, int], list] = {}
        feats = data.get("features") if isinstance(data, dict) else None
        for f in feats or []:
            g = (f or {}).get("geometry") or {}
            if g.get("type") == "Polygon":
                parts = [g.get("coordinates") or []]
            elif g.get("type") == "MultiPolygon":
                parts = g.get("coordinates") or []
            else:
                continue
            for poly in parts:
                rings = []
                for ring in poly or []:
                    pts = [(float(p[0]), float(p[1])) for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
                    if len(pts) >= 4:
                        rings.append(pts)
                if not rings:
                    continue
                xs = [p[0] for p in rings[0]]
                ys = [p[1] for p in rings[0]]
                polys.append(((min(xs), min(ys), max(xs), max(ys)), rings))
                for ring in rings:
                    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
                        e = (x1, y1, x2, y2)
                        for key in _cells_of(x1, y1, x2, y2):
                            edges.setdefault(key, []).append(e)
        self.polys, self.edges, self._centre = polys, edges, {}
        self.error = None if polys else (self.error or "No land shapes loaded.")

    # -- queries -------------------------------------------------------------------------------
    def on_land(self, lat: float, lon: float) -> bool | None:
        self._ensure()
        if not self.polys:
            return None
        key = _key(lon, lat)
        cx, cy = (key[1] + 0.5) * CELL, (key[0] + 0.5) * CELL
        inside = self._centre.get(key)
        if inside is None:
            inside = self._full(cx, cy)
            self._centre[key] = inside
        flips = 0
        for x1, y1, x2, y2 in self.edges.get(key, ()):
            if _cross(cx, cy, lon, lat, x1, y1, x2, y2):
                flips += 1
        return inside if flips % 2 == 0 else not inside

    def coast_km(self, lat: float, lon: float, max_km: float = 30.0) -> float | None:
        """Distance to the nearest coastline edge when one is within ``max_km``, else ``None``."""
        self._ensure()
        if not self.polys:
            return None
        best = None
        cos = max(0.05, math.cos(math.radians(lat)))
        reach = int(math.ceil(max_km / (KM_LAT * CELL)))
        reach_x = min(int(math.ceil(max_km / (KM_LON * cos * CELL))), int(180 / CELL))
        r0, c0 = _key(lon, lat)
        for dr in range(-reach, reach + 1):
            for dc in range(-reach_x, reach_x + 1):
                for x1, y1, x2, y2 in self.edges.get((r0 + dr, c0 + dc), ()):
                    d = _seg_km(lon, lat, x1, y1, x2, y2, cos)
                    if best is None or d < best:
                        best = d
        return best if best is not None and best <= max_km else None

    def inland_km(self, lat: float, lon: float, max_km: float = 30.0) -> float | None:
        """0 at sea, the distance to the coast on land (``max_km`` when further), None when unknown."""
        land = self.on_land(lat, lon)
        if land is None:
            return None
        if not land:
            return 0.0
        d = self.coast_km(lat, lon, max_km)
        return max_km if d is None else d

    def _full(self, x: float, y: float) -> bool:
        for (w, s, e, n), rings in self.polys:
            if not (w <= x <= e and s <= y <= n):
                continue
            if _in_ring(x, y, rings[0]) and not any(_in_ring(x, y, h) for h in rings[1:]):
                return True
        return False


def _key(lon: float, lat: float) -> tuple[int, int]:
    return int(math.floor(lat / CELL)), int(math.floor(lon / CELL))


def _cells_of(x1: float, y1: float, x2: float, y2: float) -> list[tuple[int, int]]:
    r0, r1 = sorted((int(math.floor(y1 / CELL)), int(math.floor(y2 / CELL))))
    c0, c1 = sorted((int(math.floor(x1 / CELL)), int(math.floor(x2 / CELL))))
    return [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]


def _in_ring(x: float, y: float, ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _orient(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _cross(px, py, qx, qy, x1, y1, x2, y2) -> bool:
    """Does segment p-q cross edge (x1, y1)-(x2, y2)? Half-open at the edge ends, so a vertex exactly on
    p-q is counted once for the two edges that share it."""
    d1 = _orient(px, py, qx, qy, x1, y1)
    d2 = _orient(px, py, qx, qy, x2, y2)
    if (d1 >= 0) == (d2 >= 0):
        return False
    d3 = _orient(x1, y1, x2, y2, px, py)
    d4 = _orient(x1, y1, x2, y2, qx, qy)
    return (d3 >= 0) != (d4 >= 0)


def _seg_km(px, py, x1, y1, x2, y2, cos) -> float:
    ax, ay = (x1 - px) * KM_LON * cos, (y1 - py) * KM_LAT
    bx, by = (x2 - px) * KM_LON * cos, (y2 - py) * KM_LAT
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
    return math.hypot(ax + t * dx, ay + t * dy)
