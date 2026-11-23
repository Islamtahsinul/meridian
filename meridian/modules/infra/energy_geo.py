"""Map shapes for the electricity grid layer: European countries (Natural Earth, public domain) and
the 14 GB distribution network (DNO) licence areas that NESO's regional carbon intensity uses
(NESO data portal, NESO Open Data Licence). NESO publishes the areas in British National Grid
(EPSG:27700); they are converted to WGS84 here with the Ordnance Survey formulas (transverse
Mercator on Airy 1830, then a 7-parameter Helmert shift, accurate to about 5 m).
"""

from __future__ import annotations

import json
import math
import time

from .common import read_json

NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"
NE_RAW = "ne_50m_admin_0_countries.geojson"
NE_PAGE = "https://www.naturalearthdata.com/"
DNO_PAGE = "https://www.neso.energy/data-portal/gis-boundaries-gb-dno-license-areas"
DNO_URL = ("https://api.neso.energy/dataset/0e377f16-95e9-4c15-a1fc-49e06a39cfa0/resource/"
           "1c6a7dc0-1b6c-443a-bc67-5f7125649434/download/gb-dno-license-areas-20240503-as-geojson.geojson")
DNO_RAW = "gb-dno-license-areas-20240503.geojson"
SOURCE_NE = "infra.ne_countries"
SOURCE_DNO = "infra.neso_dno"

COUNTRIES_FILE = "energy-countries.geojson"
REGIONS_FILE = "energy-gb-regions.geojson"

# Mainland and near islands of the European grids. Overseas parts (French Guiana, the Canaries,
# the Azores, Madeira, Svalbard) are dropped: they are not on the grids the figures describe.
EUROPE_BOX = (-11.0, 34.0, 47.0, 71.6)
# Northern Ireland belongs to the all-island Irish market, not the GB grid: dropped from the GB shape.
NI_BOX = (-8.3, 53.9, -5.3, 55.5)

# DNO licence area (GSP group) id -> Carbon Intensity API region id, name, short name.
DNO_REGIONS = {
    17: (1, "North Scotland", "N Scotland"), 18: (2, "South Scotland", "S Scotland"),
    16: (3, "North West England", "NW England"), 15: (4, "North East England", "NE England"),
    23: (5, "Yorkshire", "Yorkshire"), 13: (6, "North Wales and Merseyside", "N Wales"),
    21: (7, "South Wales", "S Wales"), 14: (8, "West Midlands", "W Midlands"),
    11: (9, "East Midlands", "E Midlands"), 10: (10, "East England", "E England"),
    22: (11, "South West England", "SW England"), 20: (12, "South England", "S England"),
    12: (13, "London", "London"), 19: (14, "South East England", "SE England"),
}
MIN_REGIONS = 14


# ---------------------------------------------------------------------------------------------
# British National Grid (OSGB36) -> WGS84
# ---------------------------------------------------------------------------------------------
_AIRY = (6377563.396, 6356256.909)
_GRS80 = (6378137.0, 6356752.314245)
_F0 = 0.9996012717
_LAT0 = math.radians(49.0)
_LON0 = math.radians(-2.0)
_E0, _N0 = 400000.0, -100000.0
# OSGB36 -> WGS84 Helmert parameters (metres, ppm, arc seconds): the Ordnance Survey's published set.
_TX, _TY, _TZ = 446.448, -125.157, 542.060
_S = -20.4894e-6
_RX, _RY, _RZ = (math.radians(v / 3600.0) for v in (0.1502, 0.2470, 0.8421))


def bng_to_osgb36(easting: float, northing: float) -> tuple[float, float]:
    """Inverse transverse Mercator: National Grid metres -> OSGB36 (lat, lon) in degrees."""
    a, b = _AIRY
    e2 = 1 - (b * b) / (a * a)
    n = (a - b) / (a + b)
    n2, n3 = n * n, n * n * n
    lat = _LAT0
    m = 0.0
    for _ in range(20):
        lat = (northing - _N0 - m) / (a * _F0) + lat
        dl, sl = lat - _LAT0, lat + _LAT0
        m = b * _F0 * ((1 + n + 1.25 * n2 + 1.25 * n3) * dl
                       - (3 * n + 3 * n2 + 2.625 * n3) * math.sin(dl) * math.cos(sl)
                       + (1.875 * n2 + 1.875 * n3) * math.sin(2 * dl) * math.cos(2 * sl)
                       - (35.0 / 24.0) * n3 * math.sin(3 * dl) * math.cos(3 * sl))
        if abs(northing - _N0 - m) < 1e-5:
            break
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    nu = a * _F0 / math.sqrt(1 - e2 * sin_lat * sin_lat)
    rho = a * _F0 * (1 - e2) / (1 - e2 * sin_lat * sin_lat) ** 1.5
    eta2 = nu / rho - 1
    t = math.tan(lat)
    t2, t4, t6 = t * t, t ** 4, t ** 6
    sec = 1 / cos_lat
    vii = t / (2 * rho * nu)
    viii = t / (24 * rho * nu ** 3) * (5 + 3 * t2 + eta2 - 9 * t2 * eta2)
    ix = t / (720 * rho * nu ** 5) * (61 + 90 * t2 + 45 * t4)
    x = sec / nu
    xi = sec / (6 * nu ** 3) * (nu / rho + 2 * t2)
    xii = sec / (120 * nu ** 5) * (5 + 28 * t2 + 24 * t4)
    xiia = sec / (5040 * nu ** 7) * (61 + 662 * t2 + 1320 * t4 + 720 * t6)
    de = easting - _E0
    lat_out = lat - vii * de ** 2 + viii * de ** 4 - ix * de ** 6
    lon_out = _LON0 + x * de - xi * de ** 3 + xii * de ** 5 - xiia * de ** 7
    return math.degrees(lat_out), math.degrees(lon_out)


def _to_cartesian(lat: float, lon: float, ellipsoid) -> tuple[float, float, float]:
    a, b = ellipsoid
    e2 = 1 - (b * b) / (a * a)
    phi, lam = math.radians(lat), math.radians(lon)
    nu = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    return (nu * math.cos(phi) * math.cos(lam), nu * math.cos(phi) * math.sin(lam), (1 - e2) * nu * math.sin(phi))


def _from_cartesian(x: float, y: float, z: float, ellipsoid) -> tuple[float, float]:
    a, b = ellipsoid
    e2 = 1 - (b * b) / (a * a)
    p = math.hypot(x, y)
    phi = math.atan2(z, p * (1 - e2))
    for _ in range(10):
        nu = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
        nxt = math.atan2(z + e2 * nu * math.sin(phi), p)
        if abs(nxt - phi) < 1e-12:
            phi = nxt
            break
        phi = nxt
    return math.degrees(phi), math.degrees(math.atan2(y, x))


def osgb36_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    x, y, z = _to_cartesian(lat, lon, _AIRY)
    s1 = 1 + _S
    x2 = _TX + s1 * x - _RZ * y + _RY * z
    y2 = _TY + _RZ * x + s1 * y - _RX * z
    z2 = _TZ - _RY * x + _RX * y + s1 * z
    return _from_cartesian(x2, y2, z2, _GRS80)


def bng_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """National Grid easting and northing (metres) -> WGS84 (lat, lon) in degrees."""
    return osgb36_to_wgs84(*bng_to_osgb36(easting, northing))


# ---------------------------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------------------------
def simplify_ring(points: list, tolerance: float) -> list:
    """Douglas-Peucker on a closed ring (iterative, so long coastlines cannot hit the recursion
    limit). The first and last points stay; tolerance is in the coordinates' own units."""
    n = len(points)
    if n <= 4 or tolerance <= 0:
        return list(points)
    keep = [False] * n
    keep[0] = keep[-1] = True
    # Split a closed ring at its farthest point so both halves have distinct end points.
    x0, y0 = points[0][0], points[0][1]
    far = max(range(1, n - 1), key=lambda i: (points[i][0] - x0) ** 2 + (points[i][1] - y0) ** 2)
    keep[far] = True
    stack = [(0, far), (far, n - 1)]
    tol2 = tolerance * tolerance
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = points[i][0], points[i][1]
        bx, by = points[j][0], points[j][1]
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            px, py = points[k][0], points[k][1]
            if seg == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, k
        if best > tol2:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(points, keep) if k]


def ring_area(ring: list) -> float:
    """Signed shoelace area in squared coordinate units."""
    s = 0.0
    for i in range(len(ring) - 1):
        s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return s / 2.0


def ring_centroid(ring: list) -> tuple[float, float]:
    a = ring_area(ring)
    if abs(a) < 1e-12:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)
    cx = cy = 0.0
    for i in range(len(ring) - 1):
        f = ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
        cx += (ring[i][0] + ring[i + 1][0]) * f
        cy += (ring[i][1] + ring[i + 1][1]) * f
    return cx / (6 * a), cy / (6 * a)


def _in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi:
            inside = not inside
        j = i
    return inside


def interior_point(ring: list) -> tuple[float, float]:
    """A label point inside the ring: its centroid when that is inside, otherwise the middle of
    the widest inside stretch along the centroid's latitude."""
    cx, cy = ring_centroid(ring)
    if _in_ring(cx, cy, ring):
        return cx, cy
    xs = []
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i][:2], ring[i + 1][:2]
        if (y1 > cy) != (y2 > cy):
            xs.append(x1 + (cy - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    best = None
    for k in range(0, len(xs) - 1, 2):
        w = xs[k + 1] - xs[k]
        if best is None or w > best[0]:
            best = (w, (xs[k] + xs[k + 1]) / 2)
    return (best[1], cy) if best else (cx, cy)


def _polygons(geom: dict) -> list:
    if not geom:
        return []
    if geom.get("type") == "Polygon":
        return [geom.get("coordinates") or []]
    if geom.get("type") == "MultiPolygon":
        return list(geom.get("coordinates") or [])
    return []


def _clean_polygon(poly: list, tolerance: float, digits: int) -> list | None:
    out = []
    for k, ring in enumerate(poly):
        if len(ring) < 4:
            continue
        simple = simplify_ring(ring, tolerance)
        if len(simple) < 4:
            if k == 0:
                simple = ring if len(ring) <= 64 else simplify_ring(ring, tolerance / 4)
            else:
                continue
        rounded = []
        for p in simple:
            q = [round(p[0], digits), round(p[1], digits)]
            if not rounded or q != rounded[-1]:
                rounded.append(q)
        if rounded[0] != rounded[-1]:
            rounded.append(rounded[0])
        if len(rounded) >= 4:
            out.append(rounded)
        elif k == 0:
            return None
    return out or None


def _geometry(polys: list) -> dict | None:
    polys = [p for p in polys if p]
    if not polys:
        return None
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def _box_has(box, x: float, y: float) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


# ---------------------------------------------------------------------------------------------
# Builders (pure: dict in, dict out; unit-tested)
# ---------------------------------------------------------------------------------------------
def build_countries(ne: dict, wanted: dict[str, str], *, tolerance: float = 0.006, digits: int = 3,
                    min_part_area: float = 0.002) -> dict:
    """Natural Earth admin-0 -> the European grid countries. ``wanted`` maps ISO alpha-2 to the
    display name. Overseas parts and Northern Ireland (from the UK shape) are dropped."""
    feats = []
    for f in ne.get("features") or []:
        p = f.get("properties") or {}
        iso2 = None
        for key in ("ISO_A2_EH", "ISO_A2"):
            v = p.get(key)
            if isinstance(v, str) and len(v) == 2 and v.isalpha():
                iso2 = v.upper()
                break
        if iso2 not in wanted:
            continue
        parts = []
        for poly in _polygons(f.get("geometry")):
            if not poly or len(poly[0]) < 4:
                continue
            cx, cy = ring_centroid(poly[0])
            if not _box_has(EUROPE_BOX, cx, cy):
                continue
            if iso2 == "GB" and _box_has(NI_BOX, cx, cy):
                continue
            parts.append((abs(ring_area(poly[0])), poly))
        if not parts:
            continue
        parts.sort(key=lambda t: -t[0])
        largest = parts[0][1]
        kept = [poly for area, poly in parts if area >= min_part_area or poly is largest]
        polys = [_clean_polygon(poly, tolerance, digits) for poly in kept]
        geom = _geometry(polys)
        if geom is None:
            continue
        lx, ly = p.get("LABEL_X"), p.get("LABEL_Y")
        if iso2 == "GB" or not isinstance(lx, (int, float)) or not isinstance(ly, (int, float)) \
                or not _box_has(EUROPE_BOX, lx, ly):
            lx, ly = interior_point(largest[0])
        feats.append({"type": "Feature", "id": iso2,
                      "properties": {"id": iso2, "name": wanted[iso2], "label": [round(lx, 3), round(ly, 3)]},
                      "geometry": geom})
    feats.sort(key=lambda f: f["id"])
    return {"type": "FeatureCollection", "features": feats}


def build_regions(dno: dict, *, tolerance: float = 0.004, digits: int = 4, min_part_area: float = 0.0008) -> dict:
    """NESO DNO licence areas (EPSG:27700) -> WGS84 features keyed by Carbon Intensity region id."""
    crs = json.dumps(dno.get("crs") or {})
    projected = "27700" in crs
    feats = []
    for f in dno.get("features") or []:
        p = f.get("properties") or {}
        try:
            dno_id = int(p.get("ID"))
        except (TypeError, ValueError):
            continue
        meta = DNO_REGIONS.get(dno_id)
        if not meta:
            continue
        region_id, name, short = meta
        parts = []
        for poly in _polygons(f.get("geometry")):
            rings = []
            for ring in poly:
                if projected:
                    pts = [_lonlat(pt) for pt in ring if len(pt) >= 2]
                else:
                    pts = [[float(pt[0]), float(pt[1])] for pt in ring if len(pt) >= 2]
                if len(pts) >= 4:
                    rings.append(pts)
            if rings:
                parts.append((abs(ring_area(rings[0])), rings))
        if not parts:
            continue
        parts.sort(key=lambda t: -t[0])
        largest = parts[0][1]
        kept = [rings for area, rings in parts if area >= min_part_area or rings is largest]
        geom = _geometry([_clean_polygon(r, tolerance, digits) for r in kept])
        if geom is None:
            continue
        lx, ly = interior_point(largest[0])
        feats.append({"type": "Feature", "id": region_id,
                      "properties": {"id": region_id, "dno_id": dno_id, "name": name, "short": short,
                                     "dno": str(p.get("DNO") or "").strip()[:20],
                                     "dno_full": str(p.get("DNO_Full") or "").strip()[:80],
                                     "label": [round(lx, 4), round(ly, 4)]},
                      "geometry": geom})
    feats.sort(key=lambda f: f["id"])
    return {"type": "FeatureCollection", "features": feats}


def _lonlat(pt) -> list:
    lat, lon = bng_to_wgs84(float(pt[0]), float(pt[1]))
    return [lon, lat]


# ---------------------------------------------------------------------------------------------
# The dataset
# ---------------------------------------------------------------------------------------------
def build(ds, wanted: dict[str, str]) -> None:
    """Dataset ``energy-shapes``: both shape files, from two downloads (about 6 MB in data/raw/infra)."""
    ne_path = ds.download(NE_URL, NE_RAW, min_bytes=1_000_000, timeout=180)
    countries = build_countries(json.loads(ne_path.read_bytes().decode("utf-8")), wanted)
    if len(countries["features"]) < len(wanted) - 3:
        raise RuntimeError(f"Natural Earth gave only {len(countries['features'])} of {len(wanted)} countries")
    dno_path = ds.download(DNO_URL, DNO_RAW, min_bytes=500_000, timeout=180)
    regions = build_regions(json.loads(dno_path.read_bytes().decode("utf-8")))
    if len(regions["features"]) < MIN_REGIONS:
        raise RuntimeError(f"the NESO DNO file gave only {len(regions['features'])} regions")
    countries["built_at"] = regions["built_at"] = time.time()
    ds.write_json(COUNTRIES_FILE, countries)
    ds.write_json(REGIONS_FILE, regions)
    ds.set_rows(len(countries["features"]) + len(regions["features"]))
    ds.note(f"{len(countries['features'])} country shapes and {len(regions['features'])} GB DNO regions")


def load(data_dir) -> tuple[dict, dict]:
    """Both built files (raises when missing)."""
    return read_json(data_dir / COUNTRIES_FILE), read_json(data_dir / REGIONS_FILE)
