"""Outlines for conflict-zone advisories: FIR boundaries from the VATSpy data project (CC BY-SA 4.0)
and country shapes from Natural Earth (public domain), plus the small amount of polygon geometry
the airspace layers need (simplify, clip at a meridian, label points, point lookups)."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from meridian import geo

VATSPY_URL = "https://raw.githubusercontent.com/vatsimnetwork/vatspy-data-project/master/Boundaries.geojson"
VATSPY_SITE = "https://github.com/vatsimnetwork/vatspy-data-project"
NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"
NE_SITE = "https://www.naturalearthdata.com/"
FIRS_FILE = "firs.json"
COUNTRIES_FILE = "countries.json"
_FIR_ID = re.compile(r"^[A-Z]{4}$")
# Natural Earth units without an ISO code, assigned to the country the ISO standard lists.
_NE_NAME_ISO2 = {"Somaliland": "SO", "N. Cyprus": "CY", "Kosovo": "XK"}


# ---------------------------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------------------------
def _pt(p, digits: int):
    try:
        x, y = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        return None
    if not (math.isfinite(x) and math.isfinite(y) and -180.5 <= x <= 180.5 and -90.0 <= y <= 90.0):
        return None
    return [round(x, digits), round(y, digits)]


def clean_ring(ring, digits: int = 5) -> list | None:
    """Valid, rounded, closed ring without repeated points, or None when degenerate."""
    if not isinstance(ring, (list, tuple)):
        return None
    out: list = []
    for p in ring:
        q = _pt(p, digits)
        if q is not None and (not out or q != out[-1]):
            out.append(q)
    if len(out) >= 2 and out[0] == out[-1]:
        out.pop()
    if len(out) < 3:
        return None
    return out + [out[0]]


def _dp(points: list, tol: float) -> list:
    """Douglas-Peucker on an open polyline (iterative)."""
    n = len(points)
    if n < 3:
        return points[:]
    keep = [False] * n
    keep[0] = keep[-1] = True
    tol2 = tol * tol
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        ax, ay = points[i]
        bx, by = points[j]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            px, py = points[k]
            if l2 == 0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
                d2 = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d2 > best:
                best, bi = d2, k
        if bi > 0 and best > tol2:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [p for p, k in zip(points, keep) if k]


def simplify_ring(ring: list, tol: float, digits: int = 3) -> list | None:
    ring = clean_ring(ring, 6)
    if ring is None:
        return None
    pts = ring[:-1]
    if tol > 0 and len(pts) > 8:
        first = pts[0]
        far = max(range(len(pts)), key=lambda k: (pts[k][0] - first[0]) ** 2 + (pts[k][1] - first[1]) ** 2)
        a = _dp(pts[: far + 1], tol)
        b = _dp(pts[far:] + [first], tol)
        pts = a[:-1] + b[:-1]
    return clean_ring(pts, digits)


def polygons_of(geom: dict | None) -> list:
    """GeoJSON Polygon or MultiPolygon -> list of polygons (each a list of rings)."""
    if not isinstance(geom, dict):
        return []
    coords = geom.get("coordinates") or []
    if geom.get("type") == "Polygon":
        return [coords] if coords else []
    if geom.get("type") == "MultiPolygon":
        return [p for p in coords if p]
    return []


def make_geometry(polys: list) -> dict | None:
    polys = [p for p in polys if p]
    if not polys:
        return None
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def simplify_geometry(geom: dict | None, tol: float, digits: int = 3) -> dict | None:
    out = []
    for poly in polygons_of(geom):
        outer = simplify_ring(poly[0], tol, digits)
        if outer is None:
            continue
        holes = [h for h in (simplify_ring(r, tol, digits) for r in poly[1:]) if h is not None]
        out.append([outer] + holes)
    return make_geometry(out)


def clean_geometry(geom: dict | None, digits: int = 5) -> dict | None:
    out = []
    for poly in polygons_of(geom):
        outer = clean_ring(poly[0], digits)
        if outer is None:
            continue
        out.append([outer] + [h for h in (clean_ring(r, digits) for r in poly[1:]) if h is not None])
    return make_geometry(out)


def _clip_ring(ring: list, limit: float, west: bool) -> list | None:
    """Sutherland-Hodgman against the meridian ``limit``: keep the west (or east) side."""
    pts = ring[:-1] if ring and ring[0] == ring[-1] else ring

    def inside(p):
        return p[0] <= limit if west else p[0] >= limit

    def cross(a, b):
        t = (limit - a[0]) / ((b[0] - a[0]) or 1e-12)
        return [limit, round(a[1] + t * (b[1] - a[1]), 5)]

    out: list = []
    for i, cur in enumerate(pts):
        prev = pts[i - 1]
        if inside(cur):
            if not inside(prev):
                out.append(cross(prev, cur))
            out.append(cur)
        elif inside(prev):
            out.append(cross(prev, cur))
    return clean_ring(out, 5)


def clip_geometry_lon(geom: dict | None, limit: float, *, west: bool = True) -> dict | None:
    out = []
    for poly in polygons_of(geom):
        outer = _clip_ring(poly[0], limit, west)
        if outer is None:
            continue
        out.append([outer] + [h for h in (_clip_ring(r, limit, west) for r in poly[1:]) if h is not None])
    return make_geometry(out)


def ring_area(ring: list) -> float:
    s = 0.0
    for i in range(len(ring) - 1):
        s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(s) / 2.0


def geometry_area(geom: dict | None) -> float:
    return sum(ring_area(p[0]) - sum(ring_area(h) for h in p[1:]) for p in polygons_of(geom))


def label_point(geom: dict | None) -> list | None:
    """A point inside the largest polygon: its centroid when that falls inside, otherwise the middle
    of the widest inside stretch along the centroid's latitude."""
    polys = polygons_of(geom)
    if not polys:
        return None
    poly = max(polys, key=lambda p: ring_area(p[0]))
    ring = poly[0]
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        c = x0 * y1 - x1 * y0
        a += c
        cx += (x0 + x1) * c
        cy += (y0 + y1) * c
    if abs(a) < 1e-12:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return [round((min(xs) + max(xs)) / 2, 4), round((min(ys) + max(ys)) / 2, 4)]
    cx /= 3 * a
    cy /= 3 * a
    if geo.point_in_polygon(cx, cy, poly):
        return [round(cx, 4), round(cy, 4)]
    xs = []
    for i in range(len(ring) - 1):
        (x0, y0), (x1, y1) = ring[i], ring[i + 1]
        if (y0 > cy) != (y1 > cy):
            xs.append(x0 + (cy - y0) * (x1 - x0) / (y1 - y0))
    xs.sort()
    best = None
    for k in range(0, len(xs) - 1, 2):
        if best is None or xs[k + 1] - xs[k] > best[1] - best[0]:
            best = (xs[k], xs[k + 1])
    if best is None:
        return [round(ring[0][0], 4), round(ring[0][1], 4)]
    return [round((best[0] + best[1]) / 2, 4), round(cy, 4)]


def bbox_of(geom: dict | None) -> list | None:
    b = geo.geometry_bbox(geom) if geom else None
    return [round(v, 4) for v in b] if b else None


def contains(geom: dict | None, lat: float, lon: float) -> bool:
    return bool(geom) and geo.point_in_geometry(lon, lat, geom)


# ---------------------------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------------------------
def parse_vatspy(fc: dict, tol: float = 0.01) -> dict:
    """VATSpy Boundaries.geojson -> {FIR id: {"g": geometry, "label": [lon, lat], "oceanic": bool}}.
    Sector splits ("EGTT-N") and non-ICAO ids are left out: bulletins name whole FIRs."""
    out: dict = {}
    for f in (fc or {}).get("features") or []:
        if not isinstance(f, dict):
            continue
        p = f.get("properties") or {}
        fid = str(p.get("id") or "").strip().upper()
        if not _FIR_ID.match(fid):
            continue
        g = simplify_geometry(f.get("geometry"), tol, 3)
        if g is None:
            continue
        lab = _pt((p.get("label_lon"), p.get("label_lat")), 3)
        if lab is None or not geo.point_in_geometry(lab[0], lab[1], g):
            lab = label_point(g)
        if fid in out:   # the same FIR in two pieces: merge
            g = make_geometry(polygons_of(out[fid]["g"]) + polygons_of(g))
        out[fid] = {"g": g, "label": lab, "oceanic": str(p.get("oceanic") or "0") == "1"}
    return out


def _iso2(props: dict) -> tuple[str | None, bool]:
    """(ISO2, whether the unit carries the code itself)."""
    for key in ("ISO_A2_EH", "ISO_A2"):
        v = props.get(key)
        if isinstance(v, str) and len(v) == 2 and v.isalpha():
            return v.upper(), True
    return _NE_NAME_ISO2.get(str(props.get("NAME") or "")), False


def parse_natural_earth(fc: dict, tol: float = 0.02) -> dict:
    """Natural Earth admin-0 -> {ISO2: {"name", "g"}} (pieces without a code are merged into the
    country the ISO standard assigns them to, keeping that country's name)."""
    out: dict = {}
    named: set = set()
    for f in (fc or {}).get("features") or []:
        if not isinstance(f, dict):
            continue
        p = f.get("properties") or {}
        iso, own = _iso2(p)
        if not iso:
            continue
        g = simplify_geometry(f.get("geometry"), tol, 3) or simplify_geometry(f.get("geometry"), 0.0, 3)
        if g is None:
            continue
        name = str(p.get("NAME_EN") or p.get("NAME") or p.get("ADMIN") or iso).strip()[:80]
        if iso in out:
            out[iso]["g"] = make_geometry(polygons_of(out[iso]["g"]) + polygons_of(g))
            if own and iso not in named:
                out[iso]["name"] = name
        else:
            out[iso] = {"name": name, "g": g}
        if own:
            named.add(iso)
    for rec in out.values():
        rec["label"] = label_point(rec["g"])
    return out


def build_firs(ds) -> None:
    path = ds.download(VATSPY_URL, "vatspy-boundaries.geojson", min_bytes=500_000, timeout=120)
    items = parse_vatspy(json.loads(path.read_bytes().decode("utf-8-sig")))
    if len(items) < 150:
        raise RuntimeError(f"the VATSpy boundary file looks incomplete ({len(items)} FIRs)")
    ds.write_json(FIRS_FILE, {"built_at": time.time(), "source": VATSPY_SITE, "licence": "CC BY-SA 4.0",
                              "items": items})
    ds.set_rows(len(items))
    ds.note(f"{len(items)} FIR outlines from the VATSpy data project")


def build_countries(ds) -> None:
    path = ds.download(NE_URL, "ne_50m_admin_0_countries.geojson", min_bytes=1_000_000, timeout=180)
    items = parse_natural_earth(json.loads(path.read_bytes().decode("utf-8-sig")))
    if len(items) < 150:
        raise RuntimeError(f"Natural Earth looks incomplete ({len(items)} countries)")
    ds.write_json(COUNTRIES_FILE, {"built_at": time.time(), "source": NE_SITE, "licence": "Public domain",
                                   "items": items})
    ds.set_rows(len(items))
    ds.note(f"{len(items)} country outlines from Natural Earth (1:50m)")


def load(path: Path) -> dict:
    """The ``items`` of a built shapes file, or {} when it is missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, dict) else {}
