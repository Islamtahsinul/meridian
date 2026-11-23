"""Ukraine front line from DeepStateMap's public API (https://deepstatemap.live).

DeepState publishes polygons of occupied, contested and retaken areas (plus unit markers we do not
use). It has no explicit front-line geometry, so we derive one: the edges of occupied areas whose
far side is Ukrainian-held land. Two raster masks at 0.01 degrees decide each edge: "occupied" and
"inside Ukraine" (Natural Earth, eroded a few km so international borders and coasts never count).
Editorial polygons on DeepState's map (for example "East Prussia" or "Karelia") are not shown.
"""

from __future__ import annotations

import math
import re
import time

from . import geomath

DEEPSTATE_URL = "https://deepstatemap.live/api/history/last"
DEEPSTATE_SITE = "https://deepstatemap.live/en"

LABELS = {
    "occupied": "Occupied since 2022",
    "occupied_2014": "Occupied since 2014",
    "contested": "Status unclear",
    "liberated": "Retaken by Ukraine",
    "front": "Front line",
}

_BY_KEY = {
    "geojson.status.occupied": "occupied",
    "geojson.territories.crimea": "occupied_2014",
    "geojson.territories.ordlo": "occupied_2014",
    "geojson.territories.tuzla": "occupied_2014",
    "geojson.status.unknown": "contested",
    "geojson.status.dismissed": "liberated",
    "geojson.status.dismissed_at": "liberated",
}

RES = 0.01            # raster cell, degrees
PROBE_KM = 2.5        # distance of the two probe points either side of an edge
ERODE_CELLS = 4       # ~4 km: international borders and coasts never count as front
STEP_KM = 1.0         # edges are tested every ~1 km
MIN_FRONT_KM = 2.0    # drop slivers shorter than this


def parse_name(name: str) -> tuple[str, str]:
    """('Occupied', 'geojson.status.occupied') from 'Окуповано /// Occupied /// geoJSON.status.occupied'."""
    text = (name or "").replace("\xa0", " ")
    parts = [p.strip() for p in text.split("///")]
    key = ""
    for p in parts:
        if p.lower().startswith("geojson."):
            key = p.split("{{")[0].strip().lower()
    english = parts[1] if len(parts) > 1 else parts[0]
    english = re.sub(r"\s+", " ", english).strip()
    return english, key


def classify(props: dict) -> tuple[str | None, str, str]:
    english, key = parse_name(props.get("name") or "")
    kind = _BY_KEY.get(key)
    if kind is None and key.startswith("geojson.status.dismissed"):
        kind = "liberated"
    return kind, english, key


def _clean_ring(ring: list) -> list | None:
    pts = []
    for p in ring:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        pts.append([round(x, 5), round(y, 5)])
    if len(pts) < 4:
        return None
    if pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    return pts


def parse_deepstate(data: dict) -> dict:
    """Polygons by kind from a /api/history/last payload. Raises ValueError on an unexpected shape."""
    if not isinstance(data, dict) or not isinstance(data.get("map"), dict):
        raise ValueError("DeepState payload has no map")
    feats = data["map"].get("features")
    if not isinstance(feats, list):
        raise ValueError("DeepState map has no features")
    polys: list[dict] = []
    skipped: dict[str, int] = {}
    points = 0
    for f in feats:
        g = (f or {}).get("geometry") or {}
        props = (f or {}).get("properties") or {}
        if g.get("type") == "Point":
            points += 1
            continue
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        kind, english, key = classify(props)
        if kind is None:
            skipped[key or "unnamed"] = skipped.get(key or "unnamed", 0) + 1
            continue
        for poly in geomath.polygons_of(g):
            rings = [r for r in (_clean_ring(r) for r in poly) if r]
            if rings:
                polys.append({"kind": kind, "name": english, "key": key, "coords": rings})
    updated = None
    raw_id = data.get("id")
    if isinstance(raw_id, (int, float)) and 1.4e9 < raw_id < 2.6e9:
        updated = float(raw_id)
    return {"updated": updated, "label": str(data.get("datetime") or "")[:40], "polygons": polys,
            "skipped": skipped, "points_ignored": points}


def _densify(ring: list, step_km: float) -> list:
    out = [ring[0]]
    for a, b in zip(ring, ring[1:]):
        lat = (a[1] + b[1]) / 2.0
        dx = (b[0] - a[0]) * 111.32 * math.cos(math.radians(lat))
        dy = (b[1] - a[1]) * geomath.KM_PER_DEG_LAT
        n = int(math.hypot(dx, dy) / step_km)
        for i in range(1, n + 1):
            t = i / (n + 1)
            out.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
        out.append(b)
    return out


def front_lines(occupied: list[list], ukraine_geom: dict | None, *, res: float = RES, probe_km: float = PROBE_KM,
                erode: int = ERODE_CELLS, min_km: float = MIN_FRONT_KM) -> list[list]:
    """Line strings along occupied-area edges that face Ukrainian-held land.

    ``occupied`` is a list of polygons (lists of rings). Without ``ukraine_geom`` every
    occupied-area edge not shared with another occupied area is returned."""
    if not occupied:
        return []
    xs = [p[0] for poly in occupied for ring in poly for p in ring]
    ys = [p[1] for poly in occupied for ring in poly for p in ring]
    margin = 0.3
    occ = geomath.Grid(min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin, res)
    for poly in occupied:
        occ.fill_polygon(poly)
    inside = None
    if ukraine_geom:
        ua = occ.copy_empty()
        ua.fill_geometry(ukraine_geom)
        inside = ua.eroded(erode)
    lines: list[list] = []
    for poly in occupied:
        for ring in poly:
            pts = _densify(ring, STEP_KM)
            flags = []
            for a, b in zip(pts, pts[1:]):
                mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
                kx = 111.32 * math.cos(math.radians(my))
                dx, dy = (b[0] - a[0]) * kx, (b[1] - a[1]) * geomath.KM_PER_DEG_LAT
                length = math.hypot(dx, dy)
                if length == 0:
                    flags.append(False)
                    continue
                nx, ny = -dy / length, dx / length
                ox, oy = nx * probe_km / kx, ny * probe_km / geomath.KM_PER_DEG_LAT
                p1, p2 = (mx + ox, my + oy), (mx - ox, my - oy)
                o1, o2 = occ.get(*p1), occ.get(*p2)
                if o1 == o2:
                    flags.append(False)
                    continue
                far = p2 if o1 else p1
                flags.append(True if inside is None else bool(inside.get(*far)))
            # collect runs of front segments; join the wrap-around run of a closed ring
            runs, cur = [], []
            for i, f in enumerate(flags):
                if f:
                    if not cur:
                        cur = [pts[i]]
                    cur.append(pts[i + 1])
                elif cur:
                    runs.append(cur)
                    cur = []
            if cur:
                if runs and flags and flags[0] and len(runs[0]) and runs[0][0] == pts[0]:
                    runs[0] = cur + runs[0][1:]
                else:
                    runs.append(cur)
            for run in runs:
                if len(run) >= 2 and geomath.line_length_km(run) >= min_km:
                    simple = geomath.simplify(run, 0.0012)
                    lines.append([[round(x, 5), round(y, 5)] for x, y in simple])
    return lines


def build_collection(parsed: dict, ukraine_geom: dict | None) -> tuple[dict, dict]:
    """GeoJSON for the browser plus summary numbers for the card."""
    feats = []
    occ_all, occ_2014, contested = [], [], []
    for p in parsed["polygons"]:
        if p["kind"] in ("occupied", "occupied_2014"):
            occ_all.append(p["coords"])
            if p["kind"] == "occupied_2014":
                occ_2014.append(p["coords"])
        elif p["kind"] == "contested":
            contested.append(p["coords"])
        feats.append({"type": "Feature", "properties": {"kind": p["kind"], "label": LABELS[p["kind"]], "name": p["name"]},
                      "geometry": {"type": "Polygon", "coordinates": p["coords"]}})
    lines = front_lines(occ_all, ukraine_geom)
    front_km = sum(geomath.line_length_km(line) for line in lines)
    if lines:
        feats.append({"type": "Feature", "properties": {"kind": "front", "label": LABELS["front"], "name": "Front line"},
                      "geometry": {"type": "MultiLineString", "coordinates": lines}})

    def area(polys):
        if not polys:
            return 0.0
        xs = [pt[0] for poly in polys for ring in poly for pt in ring]
        ys = [pt[1] for poly in polys for ring in poly for pt in ring]
        g = geomath.Grid(min(xs) - 0.05, min(ys) - 0.05, max(xs) + 0.05, max(ys) + 0.05, RES)
        for poly in polys:
            g.fill_polygon(poly)
        return g.area_km2()

    total = area(occ_all)
    since_2014 = area(occ_2014)
    counts: dict[str, int] = {}
    for p in parsed["polygons"]:
        counts[p["kind"]] = counts.get(p["kind"], 0) + 1
    meta = {
        "updated": parsed.get("updated"), "label": parsed.get("label"), "built_at": time.time(),
        "occupied_km2": round(total), "occupied_2014_km2": round(since_2014),
        "occupied_2022_km2": round(max(0.0, total - since_2014)), "contested_km2": round(area(contested)),
        "front_km": round(front_km), "front_pieces": len(lines), "polygons": counts,
        "skipped": parsed.get("skipped", {}), "masked": ukraine_geom is not None,
        "source": "DeepStateMap", "url": DEEPSTATE_SITE,
    }
    return {"type": "FeatureCollection", "features": feats}, meta
