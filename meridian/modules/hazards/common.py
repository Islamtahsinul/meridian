"""Shared helpers for the hazards module: time parsing, text cleaning, geometry simplification,
centroids and the per-source state holder. Pure functions except :class:`SourceState`."""

from __future__ import annotations

import html
import math
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

LAYER_QUAKES = "hazards.quakes"
LAYER_FIRES = "hazards.fires"
LAYER_NATURAL = "hazards.natural"
LAYER_DISASTERS = "hazards.disasters"
LAYER_STORMS = "hazards.storms"
LAYER_VOLCANOES = "hazards.volcanoes"
LAYER_WARNINGS = "hazards.warnings"
LAYER_TSUNAMI = "hazards.tsunami"
LAYER_OUTBREAKS = "hazards.outbreaks"
LAYER_MAPPING = "hazards.mapping"          # Copernicus EMS rapid mapping activations
LAYER_FLOODS = "hazards.floods"            # Environment Agency flood warnings and gauges (England)
LAYER_OUTLOOK = "hazards.flood_outlook"    # GloFAS flood forecast overlay

KINDS = ("earthquake", "fire", "volcano", "storm", "flood", "warning", "outbreak", "disaster", "tsunami",
         "emergency_mapping", "gauge")


# ---------------------------------------------------------------------------------------------
# numbers and time
# ---------------------------------------------------------------------------------------------
def num(value: Any, default: float | None = None) -> float | None:
    """A finite float or ``default``. Accepts numbers and numeric strings ("12.5", " 7 ")."""
    if value is None or isinstance(value, bool):
        return default
    try:
        v = float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def integer(value: Any, default: int | None = None) -> int | None:
    v = num(value)
    return int(round(v)) if v is not None else default


def r5(value: float) -> float:
    return round(float(value), 5)


def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


_TSUNAMI_TIME = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?")


def parse_time(value: Any) -> float | None:
    """Epoch seconds from ISO 8601 (with Z, an offset or no zone = UTC), RFC 822 dates, the
    tsunami.gov "2026.11.21 15:04:37 UTC" form, or epoch seconds / milliseconds."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if not math.isfinite(v):
            return None
        return v / 1000.0 if abs(v) > 1e11 else v
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return parse_time(float(s))
    m = _TSUNAMI_TIME.match(s)
    if m:
        y, mo, d, hh, mm, ss = (int(x) if x else 0 for x in m.groups())
        try:
            return datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
    iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)   # +0000 -> +00:00
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso_utc(t: float | None) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_utc(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table)\b[^>]*>", re.I)
_WS = re.compile(r"[ \t\r\f\v]+")


def strip_html(text: Any, limit: int | None = None) -> str:
    """Plain text from an HTML fragment: block tags become line breaks, entities are decoded."""
    if not text:
        return ""
    s = str(text)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s).replace(" ", " ")
    lines = [_WS.sub(" ", line).strip() for line in s.splitlines()]
    out = "\n".join(line for line in lines if line)
    return clip(out, limit) if limit else out


_MOJIBAKE = ("\u00e2\u20ac", "\u00c3", "\u00c2")


def repair_mojibake(s: str) -> str:
    """Undo UTF-8 that was decoded as Windows-1252 somewhere upstream ("Capeâ€¦" -> "Cape…").
    Only applied when the round trip is exact, so correct text is never changed."""
    if not s or not any(m in s for m in _MOJIBAKE):
        return s
    try:
        fixed = s.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return fixed


def clean(text: Any, limit: int = 300) -> str:
    """One-line text: collapsed whitespace, repaired encoding, clipped."""
    if text is None:
        return ""
    s = _WS.sub(" ", repair_mojibake(str(text)).replace("\n", " ")).strip()
    return clip(s, limit)


def clip(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)].rstrip()
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def norm(text: Any) -> str:
    """Lower-case letters and digits only, for matching names ("Nevados de Chillán" -> "nevadosdechillan")."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def title_case_name(name: str) -> str:
    """"XAVIER-26" -> "Xavier", "ONE-26" -> "One"."""
    base = re.sub(r"-\d{2}$", "", (name or "").strip())
    return " ".join(w.capitalize() for w in re.split(r"[\s_]+", base) if w)


# ---------------------------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------------------------
def valid_lonlat(lon: Any, lat: Any) -> bool:
    lo, la = num(lon), num(lat)
    return lo is not None and la is not None and -180.0 <= lo <= 180.0 and -90.0 <= la <= 90.0


def _dp(points: list, tol: float) -> list:
    """Douglas-Peucker on a list of [x, y] (iterative, so long rings never hit the recursion limit)."""
    n = len(points)
    if n < 3 or tol <= 0:
        return list(points)
    keep = bytearray(n)
    keep[0] = keep[n - 1] = 1
    stack = [(0, n - 1)]
    tol2 = tol * tol
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = points[a][0], points[a][1]
        bx, by = points[b][0], points[b][1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        best, best_i = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i][0], points[i][1]
            if seg2 == 0.0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / seg2
                t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                d2 = (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2
            if d2 > best:
                best, best_i = d2, i
        if best > tol2 and best_i > 0:
            keep[best_i] = 1
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [points[i] for i in range(n) if keep[i]]


def _round_pts(points: Iterable, nd: int) -> list:
    out = []
    last = None
    for p in points:
        q = [round(float(p[0]), nd), round(float(p[1]), nd)]
        if q != last:
            out.append(q)
            last = q
    return out


def simplify_ring(ring: list, tol: float, nd: int = 4) -> list | None:
    """A simplified closed ring, or None when it collapses (smaller than the tolerance)."""
    if not ring or len(ring) < 4:
        return None
    pts = _round_pts(_dp([[float(p[0]), float(p[1])] for p in ring], tol), nd)
    if pts and pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    if len(pts) < 4:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        if max(xs) - min(xs) < tol and max(ys) - min(ys) < tol:
            return None
        pts = _round_pts(ring, nd)
        if pts[0] != pts[-1]:
            pts.append(list(pts[0]))
        if len(pts) < 4:
            return None
    return pts


def simplify_geometry(geom: dict | None, tol: float, nd: int = 4) -> dict | None:
    """Simplify (Douglas-Peucker) and round any GeoJSON geometry. Collapsed parts are dropped."""
    if not geom or not isinstance(geom, dict):
        return None
    kind = geom.get("type")
    coords = geom.get("coordinates")
    try:
        if kind == "Point":
            return {"type": "Point", "coordinates": [round(float(coords[0]), nd), round(float(coords[1]), nd)]}
        if kind == "MultiPoint":
            return {"type": "MultiPoint", "coordinates": _round_pts(coords, nd)}
        if kind == "LineString":
            pts = _round_pts(_dp([[float(p[0]), float(p[1])] for p in coords], tol), nd)
            return {"type": "LineString", "coordinates": pts} if len(pts) >= 2 else None
        if kind == "MultiLineString":
            lines = []
            for line in coords:
                pts = _round_pts(_dp([[float(p[0]), float(p[1])] for p in line], tol), nd)
                if len(pts) >= 2:
                    lines.append(pts)
            return {"type": "MultiLineString", "coordinates": lines} if lines else None
        if kind == "Polygon":
            poly = _simplify_polygon(coords, tol, nd)
            return {"type": "Polygon", "coordinates": poly} if poly else None
        if kind == "MultiPolygon":
            polys = [p for p in (_simplify_polygon(c, tol, nd) for c in coords) if p]
            if not polys:
                return None
            if len(polys) == 1:
                return {"type": "Polygon", "coordinates": polys[0]}
            return {"type": "MultiPolygon", "coordinates": polys}
        if kind == "GeometryCollection":
            parts = [g for g in (simplify_geometry(x, tol, nd) for x in geom.get("geometries") or []) if g]
            return {"type": "GeometryCollection", "geometries": parts} if parts else None
    except (TypeError, ValueError, IndexError):
        return None
    return None


def _simplify_polygon(rings: list, tol: float, nd: int) -> list | None:
    if not rings:
        return None
    outer = simplify_ring(rings[0], tol, nd)
    if outer is None:
        return None
    holes = [h for h in (simplify_ring(r, tol, nd) for r in rings[1:]) if h]
    return [outer] + holes


def ring_area_centroid(ring: list) -> tuple[float, float, float]:
    """(signed area, cx, cy) of a ring with the shoelace formula (planar, degrees)."""
    a = cx = cy = 0.0
    n = len(ring)
    for i in range(n - 1):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[i + 1][0], ring[i + 1][1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    a *= 0.5
    if abs(a) < 1e-12:
        xs = [p[0] for p in ring] or [0.0]
        ys = [p[1] for p in ring] or [0.0]
        return 0.0, sum(xs) / len(xs), sum(ys) / len(ys)
    return a, cx / (6.0 * a), cy / (6.0 * a)


def inside_point(polygon: list, x: float, y: float) -> tuple[float, float]:
    """(x, y) if it lies inside the polygon (outer ring minus holes); otherwise the middle of the
    widest inside stretch along the line of latitude through y (a point on the surface)."""
    from meridian import geo
    try:
        if geo.point_in_polygon(x, y, polygon):
            return x, y
        crossings = []
        for ring in polygon:
            n = len(ring)
            for i in range(n):
                x1, y1 = float(ring[i][0]), float(ring[i][1])
                x2, y2 = float(ring[(i + 1) % n][0]), float(ring[(i + 1) % n][1])
                if (y1 > y) != (y2 > y):
                    crossings.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        crossings.sort()
        best = None
        for a, b in zip(crossings[0::2], crossings[1::2]):
            if best is None or b - a > best[1] - best[0]:
                best = (a, b)
        if best:
            return (best[0] + best[1]) / 2.0, y
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        pass
    return x, y


def geometry_center(geom: dict | None) -> tuple[float, float] | None:
    """(lon, lat) for labels and entity positions: the area centroid of the largest polygon,
    the middle vertex of a line, or the point itself."""
    if not geom:
        return None
    kind = geom.get("type")
    c = geom.get("coordinates")
    try:
        if kind == "Point":
            return float(c[0]), float(c[1])
        if kind in ("MultiPoint", "LineString"):
            p = c[len(c) // 2]
            return float(p[0]), float(p[1])
        if kind == "MultiLineString":
            longest = max(c, key=len)
            p = longest[len(longest) // 2]
            return float(p[0]), float(p[1])
        if kind == "Polygon":
            _, x, y = ring_area_centroid(c[0])
            return inside_point(c, x, y)
        if kind == "MultiPolygon":
            best = None
            for poly in c:
                a, x, y = ring_area_centroid(poly[0])
                if best is None or abs(a) > best[0]:
                    best = (abs(a), x, y, poly)
            return inside_point(best[3], best[1], best[2]) if best else None
        if kind == "GeometryCollection":
            for g in geom.get("geometries") or []:
                r = geometry_center(g)
                if r:
                    return r
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None
    return None


def geometry_bbox(geom: dict | None) -> list[float] | None:
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

    if not geom:
        return None
    if geom.get("type") == "GeometryCollection":
        for g in geom.get("geometries") or []:
            b = geometry_bbox(g)
            if b:
                xs.extend([b[0], b[2]])
                ys.extend([b[1], b[3]])
    else:
        walk(geom.get("coordinates"))
    if not xs:
        return None
    return [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]


def geometry_bboxes(geom: dict | None) -> list[list[float]]:
    """One bbox per polygon or line part, so shapes split at the antimeridian (Aleutian marine
    zones, Pacific cones) are not indexed as covering the whole world."""
    if not geom:
        return []
    kind = geom.get("type")
    if kind in ("MultiPolygon", "MultiLineString"):
        out = []
        for part in geom.get("coordinates") or []:
            b = geometry_bbox({"type": "Polygon" if kind == "MultiPolygon" else "LineString", "coordinates": part})
            if b:
                out.append(b)
        return out
    if kind == "GeometryCollection":
        out = []
        for g in geom.get("geometries") or []:
            out.extend(geometry_bboxes(g))
        return out
    b = geometry_bbox(geom)
    return [b] if b else []


def bboxes_intersect(parts: list | None, view: tuple | None) -> bool:
    if not parts or view is None:
        return True
    return any(bbox_intersects(b, view) for b in parts)


def bboxes_contain(parts: list | None, lat: float, lon: float) -> bool:
    if not parts:
        return True
    return any(b[0] <= lon <= b[2] and b[1] <= lat <= b[3] for b in parts)


def bbox_intersects(a: list | tuple | None, view: tuple | None) -> bool:
    """Does feature bbox ``a`` (w, s, e, n; never crossing 180) touch the view bbox (which may)?"""
    if a is None or view is None:
        return True
    w, s, e, n = view
    if a[3] < s or a[1] > n:
        return False
    if w <= e:
        return not (a[2] < w or a[0] > e)
    return a[2] >= w or a[0] <= e


def circle_ring_center(ring: list) -> tuple[float, float] | None:
    """Mean of a ring's vertices (fine for the small circles GDACS uses as track markers)."""
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    if not pts:
        return None
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def feature(geometry: dict, props: dict) -> dict:
    return {"type": "Feature", "geometry": geometry, "properties": props}


def point(lon: float, lat: float) -> dict:
    return {"type": "Point", "coordinates": [r5(lon), r5(lat)]}


def fc(features: list, **meta) -> dict:
    out = {"type": "FeatureCollection", "features": features}
    if meta:
        out["meta"] = meta
    return out


# ---------------------------------------------------------------------------------------------
# severity scales (CONTRACT 5.6: 0-2 routine, 3-5 notable, 6-8 serious, 9-10 extreme)
# ---------------------------------------------------------------------------------------------
PAGER_FLOOR = {"green": 0, "yellow": 6, "orange": 8, "red": 9}
GDACS_LEVEL = {"green": 3, "orange": 6, "red": 9}


def quake_severity(mag: float | None, alert: str | None = None, tsunami: bool = False) -> int:
    m = mag if mag is not None else 0.0
    sev = int(round(clamp(2.0 * m - 6.0, 0.0, 10.0)))
    sev = max(sev, PAGER_FLOOR.get((alert or "").lower(), 0))
    if tsunami and m >= 6.5:
        sev = min(10, sev + 1)
    return sev


# Saffir-Simpson on one-minute sustained wind (km/h); GDACS and NHC both give the maximum wind.
SS_KMH = [(252, 5), (209, 4), (178, 3), (154, 2), (119, 1), (63, 0)]


def storm_category(wind_kmh: float | None) -> int | None:
    """5..1 hurricane categories, 0 tropical storm, -1 depression, None unknown."""
    if wind_kmh is None:
        return None
    for limit, cat in SS_KMH:
        if wind_kmh >= limit:
            return cat
    return -1


def category_label(cat: int | None) -> str:
    if cat is None:
        return "Unknown strength"
    if cat >= 1:
        return f"Category {cat}"
    return "Tropical storm" if cat == 0 else "Tropical depression"


def storm_severity(wind_kmh: float | None, alert: str | None = None) -> int:
    cat = storm_category(wind_kmh)
    base = {None: 3, -1: 3, 0: 4, 1: 5, 2: 6, 3: 7, 4: 8, 5: 9}[cat]
    return max(base, GDACS_LEVEL.get((alert or "").lower(), 0))


WARNING_LEVEL = {"minor": 2, "moderate": 4, "severe": 6, "extreme": 9}
SEVERITY_RANK = {"unknown": 0, "minor": 1, "moderate": 2, "severe": 3, "extreme": 4}


def warning_severity(severity: str | None, urgency: str | None = None) -> int:
    sev = WARNING_LEVEL.get((severity or "").lower(), 2)
    if (urgency or "").lower() == "immediate" and sev >= 4:
        sev += 1
    return min(10, sev)


TSUNAMI_LEVEL = {"warning": 9, "threat": 8, "watch": 7, "advisory": 5, "information": 2, "cancellation": 1}


# ---------------------------------------------------------------------------------------------
# per-source state
# ---------------------------------------------------------------------------------------------
class SourceState:
    """Timing, health and the latest parsed snapshot of one upstream source.

    ``data`` is replaced wholesale on each successful refresh (readers never see a half-built
    snapshot). ``lock`` makes sure one refresh runs at a time."""

    def __init__(self, sid: str, active_every: float, idle_every: float | None = None):
        self.sid = sid
        self.active_every = float(active_every)
        self.idle_every = float(idle_every) if idle_every else None
        self.data: Any = None
        self.attempt_at = 0.0
        self.ok_at = 0.0
        self.error: str | None = None
        self.failures = 0
        self.note: str | None = None
        self.lock = threading.Lock()
        self.version = 0

    def interval(self, active: bool) -> float | None:
        every = self.active_every if active else self.idle_every
        if every is None:
            return None
        if self.failures:
            every = min(max(every, 60.0) * (2 ** min(self.failures - 1, 5)), 3600.0)
        return every

    def due(self, active: bool, now: float | None = None) -> bool:
        every = self.interval(active)
        if every is None:
            return False
        return (now or time.time()) - self.attempt_at >= every

    def stale(self, max_age: float, now: float | None = None) -> bool:
        return (now or time.time()) - self.ok_at > max_age

    def succeeded(self, data: Any, note: str | None = None) -> None:
        self.data = data
        self.ok_at = time.time()
        self.error = None
        self.failures = 0
        self.note = note
        self.version += 1

    def failed(self, error: str) -> None:
        self.error = error
        self.failures += 1

    def info(self) -> dict:
        return {"ok": self.error is None and self.ok_at > 0, "updated_at": self.ok_at or None,
                "error": self.error, "note": self.note, "pending": self.ok_at == 0 and self.error is None}
