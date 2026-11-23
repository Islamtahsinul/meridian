"""Regions for the briefing and a point-to-country lookup.

Five regions: Europe; Middle East and North Africa; Sub-Saharan Africa; Asia-Pacific; Americas.
Countries map to regions by ISO 3166-1 alpha-2 code (World Bank-style groupings with Turkey, the
Caucasus, Cyprus and Malta in Europe, Sudan in Sub-Saharan Africa, Central Asia in Asia-Pacific and
Russia split at 60° E, the Urals). Points at sea take the region of the nearest country within
300 km, then a coarse ocean rule.

The country shapes are the intel module's built Natural Earth file (``data/build/intel/
countries.geojson``, read-only). Without it every point falls back to coarse boxes and per-country
trends are left out.
"""

from __future__ import annotations

import json
import math
import threading
from array import array
from pathlib import Path
from typing import Any

from meridian import geo

REGIONS: list[tuple[str, str]] = [
    ("europe", "Europe"),
    ("mena", "Middle East and North Africa"),
    ("ssa", "Sub-Saharan Africa"),
    ("apac", "Asia-Pacific"),
    ("americas", "Americas"),
]
REGION_NAMES = dict(REGIONS)

_EUROPE = """AD AL AM AT AX AZ BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GE GG GI GR HR HU IE IM IS IT JE
LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SJ SK SM TR UA VA XK"""
_MENA = "AE BH DZ EG EH IL IQ IR JO KW LB LY MA OM PS QA SA SY TN YE"
_SSA = """AO BF BI BJ BW CD CF CG CI CM CV DJ ER ET GA GH GM GN GQ GW IO KE KM LR LS MG ML MR MU MW MZ NA NE
NG RE RW SC SD SH SL SN SO SS ST SZ TD TF TG TZ UG YT ZA ZM ZW"""
_APAC = """AF AS AU BD BN BT CC CK CN CX FJ FM GU HK HM ID IN JP KG KH KI KP KR KZ LA LK MH MM MN MO MP MV MY NC
NF NP NR NU NZ PF PG PH PK PN PW SB SG TH TJ TK TL TM TO TV TW UM UZ VN VU WF WS"""
_AMERICAS = """AG AI AR AW BB BL BM BO BQ BR BS BZ CA CL CO CR CU CW DM DO EC FK GD GF GL GP GS GT GY HN HT JM KN
KY LC MF MQ MS MX NI PA PE PM PR PY SR SV SX TC TT US UY VC VE VG VI"""

COUNTRY_REGION: dict[str, str] = {}
for _rid, _codes in (("europe", _EUROPE), ("mena", _MENA), ("ssa", _SSA), ("apac", _APAC), ("americas", _AMERICAS)):
    for _c in _codes.split():
        COUNTRY_REGION[_c] = _rid

# Crimea: Natural Earth draws it inside Russia; like the intel module's country lookup, the briefing
# attributes points inside this outline to neither Russia nor Ukraine (region Europe, no country).
CRIMEA = [[32.45, 45.35], [33.35, 46.15], [33.62, 46.22], [34.45, 45.95], [35.05, 45.65], [35.95, 45.62],
          [36.65, 45.45], [36.55, 45.15], [35.6, 44.95], [34.4, 44.35], [33.4, 44.35], [33.25, 44.6],
          [32.5, 45.15], [32.45, 45.35]]

NEAR_KM = 300.0          # sea points take the region of a country this close
COAST_KM = 25.0          # ... and count as that country (for per-country trends) when this close
CACHE_MAX = 50000
KM_PER_DEG_LAT = 110.57
KM_PER_DEG_LON = 111.32  # at the equator; times cos(latitude)


def wrap_lon(lon: float) -> float:
    return ((float(lon) + 180.0) % 360.0) - 180.0


def region_of_country(iso2: str | None, lat: float, lon: float) -> str | None:
    if not iso2:
        return None
    code = iso2.upper()
    if code == "RU":   # the Urals split European Russia from Siberia; Chukotka crosses 180°
        lon = wrap_lon(lon)
        return "apac" if lon >= 60.0 or lon < -160.0 else "europe"
    return COUNTRY_REGION.get(code)


def ocean_region(lat: float, lon: float) -> str:
    """A coarse region for points that are far from every country (open sea, poles)."""
    lat, lon = float(lat), wrap_lon(lon)
    if lat < -60.0:
        return "americas" if lon < -30.0 else "ssa" if lon < 60.0 else "apac"
    if lat > 66.0:
        return "europe" if -45.0 <= lon < 45.0 else "americas" if lon < -45.0 else "apac"
    if 30.0 <= lat <= 47.0 and -6.0 <= lon <= 42.0:          # Mediterranean and Black Sea
        if lat < 35.5 or (lon >= 32.0 and lat < 37.0):
            return "mena"
        return "europe"
    if 10.0 <= lat < 31.0 and 32.0 <= lon < 66.0:            # Red Sea, Gulf, Arabian Sea
        return "mena"
    if lat >= 36.0 and -45.0 <= lon < 45.0:                  # north-east Atlantic, North Sea, Baltic
        return "europe"
    if -45.0 <= lon < 20.0:                                  # eastern Atlantic, Gulf of Guinea
        if lat >= 20.0 and lon >= -20.0:
            return "mena"
        if lon >= -20.0:
            return "ssa"
        return "americas" if lat >= 0.0 or lon < -30.0 else "ssa"
    if 20.0 <= lon < 100.0:                                  # Indian Ocean
        return "ssa" if lon < 60.0 and lat < 10.0 else "apac"
    if lon >= 100.0 or lon < (-150.0 if lat >= 15.0 else -120.0):   # western Pacific, Polynesia
        return "apac"
    return "americas"                                        # eastern Pacific, Caribbean, west Atlantic


def coarse_region(lat: float, lon: float) -> str:
    """Land and sea by boxes only: used when the country shapes are not available."""
    lat, lon = float(lat), wrap_lon(lon)
    if lon < -30.0 and lat > -60.0:
        return "americas"
    if lat >= 36.0 and -30.0 <= lon < 60.0 and not (lon >= 44.0 and lat < 40.0):
        return "europe"
    # North Africa and the Middle East north of 20° N; south of it only Yemen and Oman (east of the
    # Red Sea), so the Sahel and the Horn stay in Sub-Saharan Africa
    if -20.0 <= lon < 63.0 and (20.0 <= lat < 40.0 or (12.0 <= lat < 20.0 and lon >= 42.5)):
        return "mena"
    if -20.0 <= lon < 52.0 and -36.0 <= lat < 20.0:
        return "ssa"
    if lon >= 60.0 or lon < -150.0:
        return "apac"
    return ocean_region(lat, lon)


ROW_DEG = 0.5            # latitude band of the per-polygon edge index
CELL_DEG = 2.0           # cells of the polygon index (bounding boxes)



def _cell(v: float) -> int:
    return int(math.floor(v / CELL_DEG))


class _Polygon:
    """One polygon (outer ring and holes) with its edges bucketed by latitude band, so a point test
    only looks at the edges near the point's latitude (even-odd rule, as geo.point_in_polygon), and
    a distance search only at the bands within reach."""

    __slots__ = ("bbox", "rows")

    def __init__(self, rings: list):
        outer = rings[0]
        xs = [float(p[0]) for p in outer]
        ys = [float(p[1]) for p in outer]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))
        rows: dict[int, array] = {}           # band -> x1, y1, x2, y2, x1, y1, ... (8 bytes a number)
        for ring in rings:
            for i in range(len(ring)):
                x1, y1 = float(ring[i - 1][0]), float(ring[i - 1][1])
                x2, y2 = float(ring[i][0]), float(ring[i][1])
                if x1 == x2 and y1 == y2:
                    continue                      # the closing point repeats the first
                for r in range(int(math.floor(min(y1, y2) / ROW_DEG)), int(math.floor(max(y1, y2) / ROW_DEG)) + 1):
                    band = rows.get(r)
                    if band is None:
                        band = rows[r] = array("d")
                    band.extend((x1, y1, x2, y2))
        self.rows = rows

    @staticmethod
    def _edges(band):
        it = iter(band)
        return zip(it, it, it, it)

    def contains(self, lon: float, lat: float) -> bool:
        b = self.bbox
        if not (b[0] <= lon <= b[2] and b[1] <= lat <= b[3]):
            return False
        inside = False
        band = self.rows.get(int(math.floor(lat / ROW_DEG)))
        if band is None:
            return False
        for x1, y1, x2, y2 in self._edges(band):
            # horizontal edges fail the first test, so the division never sees y1 == y2
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
        return inside

    def distance_km(self, lon: float, lat: float, limit_km: float) -> float | None:
        """Shortest distance from the point to any edge within ``limit_km`` (local flat projection, fine
        at these distances), or None when nothing is that close."""
        ky = KM_PER_DEG_LAT
        kx = KM_PER_DEG_LON * max(0.02, math.cos(math.radians(lat)))
        reach = limit_km / ky
        best = None
        for r in range(int(math.floor((lat - reach) / ROW_DEG)), int(math.floor((lat + reach) / ROW_DEG)) + 1):
            band = self.rows.get(r)
            if band is None:
                continue
            for x1, y1, x2, y2 in self._edges(band):   # an edge spanning bands repeats: same distance
                ax, ay = wrap_lon(x1 - lon) * kx, (y1 - lat) * ky
                bx, by = wrap_lon(x2 - lon) * kx, (y2 - lat) * ky
                dx, dy = bx - ax, by - ay
                ll = dx * dx + dy * dy
                t = 0.0 if ll == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / ll))
                d = math.hypot(ax + t * dx, ay + t * dy)
                if d <= limit_km and (best is None or d < best):
                    best = d
        return best


class CountryIndex:
    """Natural Earth countries in 2-degree cells: ``lookup(lat, lon)`` -> {iso2, name} or None."""

    def __init__(self, path: Path | None = None, *, features: list[dict] | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._loaded = False
        self._mtime: float | None = None
        self.items: list[tuple[str, str, list, tuple, float, list | None]] = []
        self.grid: dict[tuple[int, int], list[tuple[int, int]]] = {}
        self.by_iso: dict[str, dict] = {}
        self._cache: dict[tuple[float, float], dict | None] = {}
        if features is not None:
            self._build(features)
            self._loaded = True

    # -- loading -----------------------------------------------------------------------------------
    def _build(self, features: list[dict]) -> None:
        items, grid, by_iso = [], {}, {}
        for f in features:
            if not isinstance(f, dict):
                continue
            geom = f.get("geometry") or {}
            props = f.get("properties") or {}
            iso2 = str(props.get("iso2") or "").upper()
            bb = geo.geometry_bbox(geom) if geom else None
            if not bb or not iso2:
                continue
            polys = []
            for rings in _polygons(geom):
                if rings and len(rings[0]) >= 4:
                    try:
                        polys.append(_Polygon(rings))
                    except (TypeError, ValueError, IndexError):
                        continue
            if not polys:
                continue
            idx = len(items)
            label = props.get("label") if isinstance(props.get("label"), list) and len(props["label"]) == 2 else None
            name = str(props.get("name") or iso2)
            area = (bb[2] - bb[0]) * (bb[3] - bb[1])
            items.append((iso2, name, polys, bb, area, label))
            for pi, poly in enumerate(polys):
                pb = poly.bbox
                for r in range(_cell(pb[1]), _cell(pb[3]) + 1):
                    for c in range(_cell(pb[0]), _cell(pb[2]) + 1):
                        grid.setdefault((r, c), []).append((idx, pi))
            cur = by_iso.get(iso2)
            if cur is None or area > cur["_area"]:
                by_iso[iso2] = {"iso2": iso2, "name": name, "label": label, "bbox": [round(v, 4) for v in bb],
                                "_area": area}
        self.items, self.grid, self.by_iso = items, grid, by_iso
        self._cache = {}

    def ensure(self) -> bool:
        """Load (or reload after the intel dataset was rebuilt). True when shapes are available."""
        if self.path is None:
            return self._loaded
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._loaded
        if self._loaded and mtime == self._mtime:
            return True
        with self._lock:
            if self._loaded and mtime == self._mtime:
                return True
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._build(data.get("features") or [])
                self._loaded = True
                self._mtime = mtime
            except (OSError, ValueError, AttributeError):
                return self._loaded
        return True

    def release(self) -> None:
        """Free the shapes (they are only needed while a briefing is built); ``ensure`` loads them again."""
        with self._lock:
            self.items, self.grid, self.by_iso, self._cache = [], {}, {}, {}
            self._loaded = False
            self._mtime = None

    @property
    def available(self) -> bool:
        return self._loaded or (self.path is not None and self.path.exists())

    @property
    def ready(self) -> bool:
        return self._loaded and bool(self.items)

    # -- lookups ---------------------------------------------------------------------------------------
    def lookup(self, lat: float, lon: float) -> dict | None:
        """The country containing the point, else the nearest within NEAR_KM (``near: True``), else None."""
        if not self.ready:
            return None
        lat, lon = float(lat), wrap_lon(lon)
        key = (round(lat, 2), round(lon, 2))
        if key in self._cache:
            return self._cache[key]
        hit = self._inside(lat, lon) or self._nearest(lat, lon)
        if hit and hit["iso2"] in ("RU", "UA") and geo.point_in_ring(lon, lat, CRIMEA):
            hit = {"iso2": None, "name": "Crimea", "region": "europe"}
        if len(self._cache) >= CACHE_MAX:
            self._cache.clear()
        self._cache[key] = hit
        return hit

    def _inside(self, lat: float, lon: float) -> dict | None:
        best = None
        for i, pi in self.grid.get((_cell(lat), _cell(lon)), ()):
            iso2, name, polys, _bb, area, _label = self.items[i]
            if (best is None or area < best[0]) and polys[pi].contains(lon, lat):
                best = (area, iso2, name)      # the smallest shape wins (enclaves)
        return {"iso2": best[1], "name": best[2]} if best else None

    def _nearest(self, lat: float, lon: float) -> dict | None:
        """The country with the closest edge within NEAR_KM: candidates by bounding box, nearest box first."""
        ky = KM_PER_DEG_LAT
        kx = KM_PER_DEG_LON * max(0.02, math.cos(math.radians(lat)))
        dlat, dlon = NEAR_KM / ky, min(180.0, NEAR_KM / kx)
        keys: dict[tuple[int, int], None] = {}
        ncells = int(round(360 / CELL_DEG))
        lo_c = _cell(-180.0)
        for r in range(_cell(lat - dlat), _cell(lat + dlat) + 1):
            for c in range(_cell(lon - dlon), _cell(lon + dlon) + 1):
                for key in self.grid.get((r, (c - lo_c) % ncells + lo_c), ()):
                    keys[key] = None
        boxed = []
        for i, pi in keys:
            b = self.items[i][2][pi].bbox
            gx = 0.0 if b[0] <= lon <= b[2] else min(abs(wrap_lon(lon - b[0])), abs(wrap_lon(lon - b[2])))
            gy = 0.0 if b[1] <= lat <= b[3] else min(abs(lat - b[1]), abs(lat - b[3]))
            gap = math.hypot(gx * kx, gy * ky)
            if gap <= NEAR_KM:
                boxed.append((gap, i, pi))
        boxed.sort()
        best = None
        for gap, i, pi in boxed:
            if best is not None and gap >= best[0]:
                break
            d = self.items[i][2][pi].distance_km(lon, lat, best[0] if best else NEAR_KM)
            if d is not None and (best is None or d < best[0]):
                best = (d, i)
        if best is None:
            return None
        iso2, name = self.items[best[1]][0], self.items[best[1]][1]
        return {"iso2": iso2, "name": name, "near": True, "distance_km": round(best[0], 1)}

    def country(self, iso2: str) -> dict | None:
        """{iso2, name, label, bbox} for a code (the largest shape when a code has several)."""
        if not self.ready:
            return None
        row = self.by_iso.get((iso2 or "").upper())
        return {k: v for k, v in row.items() if not k.startswith("_")} if row else None

    def name(self, iso2: str) -> str | None:
        row = self.by_iso.get((iso2 or "").upper())
        return row["name"] if row else None


def _polygons(geom: dict) -> list:
    t = geom.get("type")
    if t == "Polygon":
        return [geom.get("coordinates") or []]
    if t == "MultiPolygon":
        return list(geom.get("coordinates") or [])
    return []


class Regionizer:
    """Point -> (region id, country or None), with the country index when it is available."""

    def __init__(self, index: CountryIndex | None):
        self.index = index

    def place(self, lat: Any, lon: Any) -> tuple[str, dict | None]:
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return "global", None
        if not geo.valid_latlon(lat, lon):
            return "global", None
        idx = self.index
        if idx is not None and idx.ready:
            hit = idx.lookup(lat, lon)
            if hit and hit.get("region") and not hit.get("iso2"):
                return hit["region"], None          # Crimea: no country attribution
            if hit:
                region = region_of_country(hit["iso2"], lat, lon)
                if region:
                    # inside a country, or just off its (simplified) coast: that country; further out
                    # at sea only the region comes from the nearest country
                    close = not hit.get("near") or (hit.get("distance_km") or 0.0) <= COAST_KM
                    return region, ({"iso2": hit["iso2"], "name": hit["name"]} if close else None)
            return ocean_region(lat, lon), None
        return coarse_region(lat, lon), None

    def country_entity(self, iso2: str) -> dict | None:
        """A country entity (CONTRACT 5.6) that opens the intel country card."""
        idx = self.index
        row = idx.country(iso2) if idx is not None else None
        if not row:
            return None
        ent = {"kind": "country", "id": row["iso2"], "title": row["name"], "layer": "intel.countries", "zoom": 4}
        if row.get("label"):
            ent["lon"], ent["lat"] = round(row["label"][0], 4), round(row["label"][1], 4)
        bb = row.get("bbox")
        if bb and bb[2] - bb[0] < 60:
            ent["bbox"] = bb
        return ent
