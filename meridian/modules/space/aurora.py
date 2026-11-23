"""Aurora: the NOAA SWPC OVATION grid as probability tiles and viewing lines, plus "aurora chance here
tonight" from OVATION now, corrected geomagnetic latitude, darkness, cloud and the Kp forecast.

Everything here is pure computation on already-fetched data (no network), so it is unit tested
directly. The viewing lines and the Kp rule are Meridian's estimates, not SWPC products:

- Overhead line: the equatorward edge of the area where OVATION gives a 10% or higher chance of
  seeing aurora (smoothed over a few degrees of longitude).
- Horizon line: that edge moved HORIZON_DEG (about 330 km) further from the pole. Bright aurora can be
  seen from up to 1,000 km away (SWPC), faint aurora from much less; 3 degrees matches what people
  report at a given Kp.
- Tonight: SWPC's rule of thumb puts the equatorward edge of the oval at about 66 - 2 * Kp degrees of
  geomagnetic latitude. We use corrected geomagnetic (CGM) latitude from a NASA table (see cgm.py),
  because the simple dipole is up to 6 degrees out over Europe.
"""

from __future__ import annotations

import math
import struct
import threading
import zlib
from datetime import datetime, timezone

from . import cgm
from . import sgp4 as S

OVERHEAD_PCT = 10          # OVATION chance treated as "may be seen overhead"
HORIZON_DEG = 3.0          # about 330 km further from the pole: "may be seen low on the horizon"
FIELD_MIN_PCT = 5          # below this the probability field is transparent (polar cap and dayside haze)
TILE = 256
SCALE = 2.5                # palette steps per percent (251 entries): fine enough that the field shows no bands
MAX_TILE_ZOOM = 3          # 1-degree data: MapLibre enlarges z3 tiles smoothly beyond that
DARK_SUN_DEG = -12.0       # nautical twilight: dark enough for aurora
EDGE_SMOOTH = 3            # running mean over +-3 degrees of longitude
POLE = (80.8, -72.7)       # IGRF 2025 north geomagnetic (dipole) pole, used to place events


def iso_time(text) -> float | None:
    """'2026-11-23T14:32:00Z' (Z optional, fractions ignored) -> epoch seconds."""
    if not text:
        return None
    s = str(text).strip().replace(" ", "T").replace("_", "T")
    s = s[:-1] if s.endswith("Z") else s
    s = s.split(".")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------------------------
# The OVATION grid
# ---------------------------------------------------------------------------------------------
class Grid:
    """One OVATION forecast: 181 latitude rows (-90..90) of 360 longitudes (0..359 east), in percent.
    Immutable once built; the tile and line caches are filled lazily under a lock."""

    def __init__(self, rows: list[list[int]], observed: float | None, forecast: float | None):
        self.rows = rows
        self.observed = observed
        self.forecast = forecast
        self.rowmax = [max(r) for r in rows]
        self.north_max = max(self.rowmax[91:])
        self.south_max = max(self.rowmax[:90])
        self.version = str(int(observed or forecast or 0))
        self._lock = threading.Lock()
        self._lines: dict | None = None
        self._edges: dict[bool, list] = {}
        self._tiles: dict[tuple[int, int, int], bytes] = {}

    @classmethod
    def from_json(cls, data) -> "Grid":
        if not isinstance(data, dict) or not isinstance(data.get("coordinates"), list):
            raise ValueError("OVATION answered without a coordinate grid")
        rows = [[0] * 360 for _ in range(181)]
        n = 0
        for item in data["coordinates"]:
            try:
                i = int(item[0]) % 360
                j = int(item[1]) + 90
                v = float(item[2])
            except (TypeError, ValueError, IndexError):
                continue
            if 0 <= j <= 180 and math.isfinite(v):
                rows[j][i] = max(0, min(100, int(round(v))))
                n += 1
        if n < 60000:
            raise ValueError(f"the OVATION grid is incomplete ({n} of 65,160 points)")
        return cls(rows, iso_time(data.get("Observation Time")), iso_time(data.get("Forecast Time")))

    def value(self, lat: float, lon: float) -> float:
        """Bilinear chance (percent) at a point."""
        gy = max(-90.0, min(90.0, lat)) + 90.0
        j0 = min(179, int(gy))
        fy = gy - j0
        gx = lon % 360.0
        i0 = int(gx) % 360
        fx = gx - int(gx)
        i1 = (i0 + 1) % 360
        r0, r1 = self.rows[j0], self.rows[j0 + 1]
        a = r0[i0] + (r0[i1] - r0[i0]) * fx
        b = r1[i0] + (r1[i1] - r1[i0]) * fx
        return a + (b - a) * fy

    def poleward_max(self, lat: float, lon: float, span_deg: float = HORIZON_DEG) -> float:
        """Highest chance between the point and span_deg of latitude towards its (geographic) pole."""
        sign = 1.0 if lat >= 0 else -1.0
        best = 0.0
        steps = max(1, int(span_deg / 0.5))
        for k in range(steps + 1):
            la = max(-90.0, min(90.0, lat + sign * span_deg * k / steps))
            best = max(best, self.value(la, lon))
        return best

    # -- viewing lines ---------------------------------------------------------------------------
    def edge(self, north: bool, threshold: int = OVERHEAD_PCT) -> list[float | None]:
        """Equatorward edge latitude of the >= threshold area for each longitude 0..359 (None: no aurora)."""
        with self._lock:
            hit = self._edges.get(north)
        if hit is not None and threshold == OVERHEAD_PCT:
            return hit
        out: list[float | None] = []
        order = range(90, 181) if north else range(90, -1, -1)
        rows = self.rows
        for i in range(360):
            prev_v = None
            prev_lat = 0.0
            found = None
            for j in order:
                v = rows[j][i]
                lat = float(j - 90)
                if v >= threshold:
                    if prev_v is None or v == prev_v:
                        found = lat
                    else:
                        found = prev_lat + (lat - prev_lat) * (threshold - prev_v) / (v - prev_v)
                    break
                prev_v, prev_lat = v, lat
            out.append(found)
        out = smooth_circular(out, EDGE_SMOOTH)
        if threshold == OVERHEAD_PCT:
            with self._lock:
                self._edges[north] = out
        return out

    def lines(self) -> dict:
        """GeoJSON viewing lines for both hemispheres (MultiLineStrings, -180..180, gaps where the
        chance stays below 10%)."""
        with self._lock:
            if self._lines is not None:
                return self._lines
        feats = []
        for north in (True, False):
            e = self.edge(north)
            sign = 1.0 if north else -1.0
            hemi = "north" if north else "south"
            for kind, shift in (("overhead", 0.0), ("horizon", HORIZON_DEG)):
                segs: list[list[list[float]]] = []
                cur: list[list[float]] = []
                for k in range(361):
                    v = e[(180 + k) % 360]
                    if v is None:
                        if len(cur) >= 3:
                            segs.append(cur)
                        cur = []
                        continue
                    lat = max(-89.5, min(89.5, v - sign * shift))
                    cur.append([float(k - 180), round(lat, 2)])
                if len(cur) >= 3:
                    segs.append(cur)
                if not segs:
                    continue
                label = ("Aurora may be seen overhead" if kind == "overhead"
                         else f"May be seen low on the {'northern' if north else 'southern'} horizon")
                feats.append({"type": "Feature", "geometry": {"type": "MultiLineString", "coordinates": segs},
                              "properties": {"kind": kind, "hemisphere": hemi, "label": label}})
        fc = {"type": "FeatureCollection", "features": feats}
        with self._lock:
            self._lines = fc
        return fc

    def edge_range(self, north: bool) -> tuple[float, float] | None:
        vals = [v for v in self.edge(north) if v is not None]
        if not vals:
            return None
        return (min(vals), max(vals)) if north else (max(vals), min(vals))

    # -- tiles -----------------------------------------------------------------------------------
    def tile(self, z: int, x: int, y: int) -> bytes:
        key = (z, x, y)
        with self._lock:
            hit = self._tiles.get(key)
        if hit is not None:
            return hit
        png = render_tile(self, z, x, y)
        with self._lock:
            if len(self._tiles) < 400:
                self._tiles[key] = png
        return png


def smooth_circular(values: list[float | None], k: int) -> list[float | None]:
    """Running mean over +-k neighbours (wrapping), only where there is a value; gaps stay gaps."""
    n = len(values)
    out: list[float | None] = []
    for i, v in enumerate(values):
        if v is None:
            out.append(None)
            continue
        acc = 0.0
        cnt = 0
        for d in range(-k, k + 1):
            w = values[(i + d) % n]
            if w is not None:
                acc += w
                cnt += 1
        out.append(acc / cnt)
    return out


# ---------------------------------------------------------------------------------------------
# PNG tiles (palette PNG with transparency; the browser decodes them like any raster tile)
# ---------------------------------------------------------------------------------------------
# One hue (aurora green), lighter and more opaque with probability: low chances stay see-through so the
# map shows, like the module's other weather rasters. Validated as an ordinal ramp on the map ink #111a2c.
_RAMP = [(0, (29, 107, 72), 0.0), (FIELD_MIN_PCT, (29, 107, 72), 0.04), (10, (36, 135, 90), 0.34),
         (20, (46, 165, 108), 0.48), (35, (76, 195, 130), 0.60), (55, (126, 220, 158), 0.70),
         (80, (189, 242, 204), 0.78), (100, (214, 248, 222), 0.80)]


def _palette() -> tuple[bytes, bytes]:
    rgb = bytearray()
    alpha = bytearray()
    for i in range(int(100 * SCALE) + 1):
        p = i / SCALE
        if p < FIELD_MIN_PCT:
            rgb += bytes((0, 0, 0))
            alpha.append(0)
            continue
        for (p0, c0, a0), (p1, c1, a1) in zip(_RAMP, _RAMP[1:]):
            if p0 <= p <= p1:
                f = 0.0 if p1 == p0 else (p - p0) / (p1 - p0)
                rgb += bytes(int(round(c0[k] + (c1[k] - c0[k]) * f)) for k in range(3))
                alpha.append(int(round(255 * (a0 + (a1 - a0) * f))))
                break
    return bytes(rgb), bytes(alpha)


PAL_RGB, PAL_ALPHA = _palette()
LEGEND = [(10, "10%"), (30, "30%"), (50, "50%"), (80, "80% or more")]


def legend_colours() -> list[dict]:
    """Legend swatches as CSS colours (the tile colour at full opacity of the swatch)."""
    out = []
    for p, label in LEGEND:
        i = int(p * SCALE)
        r, g, b = PAL_RGB[i * 3:i * 3 + 3]
        out.append({"pct": p, "label": label, "color": f"#{r:02x}{g:02x}{b:02x}", "alpha": round(PAL_ALPHA[i] / 255, 2)})
    return out


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png_indexed(width: int, height: int, rows: list[bytes], palette: bytes = PAL_RGB, alpha: bytes = PAL_ALPHA) -> bytes:
    raw = b"".join(b"\x00" + r for r in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"PLTE", palette) + _chunk(b"tRNS", alpha)
            + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b""))


EMPTY_TILE = png_indexed(TILE, TILE, [bytes(TILE)] * TILE)


def render_tile(grid: Grid, z: int, x: int, y: int, size: int = TILE) -> bytes:
    """Web Mercator tile z/x/y of the probability field, bilinear between the 1-degree cells; each pixel is a
    palette index (percent x SCALE)."""
    n = 1 << z
    i0s, i1s, fxs = [], [], []
    for px in range(size):
        lon = (x * size + px + 0.5) / (size * n) * 360.0 - 180.0
        gx = lon % 360.0
        i0 = int(gx)
        fxs.append(gx - i0)
        i0s.append(i0 % 360)
        i1s.append((i0 + 1) % 360)
    zero = bytes(size)
    cache: dict[int, list[float]] = {}
    rows_src = grid.rows

    def colrow(j: int) -> list[float]:
        r = cache.get(j)
        if r is None:
            src = rows_src[j]
            r = [(src[a] + (src[b] - src[a]) * f) * SCALE for a, b, f in zip(i0s, i1s, fxs)]
            cache[j] = r
        return r

    out: list[bytes] = []
    any_data = False
    for py in range(size):
        yy = (y * size + py + 0.5) / (size * n)
        lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * yy))))
        gy = lat + 90.0
        j0 = min(179, int(gy))
        fy = gy - j0
        if grid.rowmax[j0] < FIELD_MIN_PCT and grid.rowmax[j0 + 1] < FIELD_MIN_PCT:
            out.append(zero)
            continue
        a = colrow(j0)
        b = colrow(j0 + 1)
        row = bytes([int(p + (q - p) * fy + 0.5) for p, q in zip(a, b)])
        if not any_data and max(row) >= FIELD_MIN_PCT * SCALE:
            any_data = True
        out.append(row)
    if not any_data:
        return EMPTY_TILE
    return png_indexed(size, size, out)


# ---------------------------------------------------------------------------------------------
# Darkness (the module's sun maths)
# ---------------------------------------------------------------------------------------------
def sun_elevation(lat: float, lon: float, t: float) -> float:
    return S.sun_elevation(S.Observer(lat, lon), t)


def dark_window(lat: float, lon: float, now: float, *, depression: float = DARK_SUN_DEG,
                hours: float = 48.0, step: float = 600.0) -> dict:
    """The current or next stretch of time with the Sun at least |depression| degrees below the horizon.

    Returns {dark_now, sun_elevation, start, end, polar_night, returns}: start/end are epoch seconds
    (end None when the night lasts beyond the search), returns is the first dark night when there is
    none in the next `hours` (midsummer near the poles), polar_night when it stays dark throughout."""
    obs = S.Observer(lat, lon)

    def f(t: float) -> float:
        return S.sun_elevation(obs, t) - depression

    def cross(a: float, b: float) -> float:
        fa_dark = f(a) < 0
        while b - a > 30.0:
            mid = (a + b) / 2.0
            if (f(mid) < 0) == fa_dark:
                a = mid
            else:
                b = mid
        return (a + b) / 2.0

    elev = S.sun_elevation(obs, now)
    n = int(hours * 3600 / step)
    ts = [now + k * step for k in range(n + 1)]
    dark = [f(t) < 0 for t in ts]
    changes = [(cross(ts[k - 1], ts[k]), dark[k]) for k in range(1, len(ts)) if dark[k] != dark[k - 1]]
    out = {"dark_now": dark[0], "sun_elevation": round(elev, 1), "start": None, "end": None,
           "polar_night": False, "returns": None}
    if dark[0]:
        start = None
        t = now
        while t > now - 24 * 3600:          # when did tonight begin?
            t2 = t - step
            if f(t2) >= 0:
                start = cross(t2, t)
                break
            t = t2
        out["start"] = start if start is not None else now
        out["end"] = next((t for t, d in changes if not d), None)
        out["polar_night"] = start is None and out["end"] is None
        return out
    first = next((k for k, (_, d) in enumerate(changes) if d), None)
    if first is not None:
        out["start"] = changes[first][0]
        out["end"] = next((t for t, d in changes[first + 1:] if not d), None)
        return out
    # no darkness for two days: find the first night (at local solar midnight) that gets dark
    day0 = math.floor(now / 86400.0) * 86400.0
    for d in range(2, 200):
        t = day0 + d * 86400.0 + ((24.0 - lon / 15.0) % 24.0) * 3600.0
        if f(t) < 0:
            out["returns"] = t
            break
    return out


# ---------------------------------------------------------------------------------------------
# Aurora chance here tonight
# ---------------------------------------------------------------------------------------------
def kp_needed(abs_mlat: float, offset: float = 0.0) -> float:
    """Kp at which SWPC's rule of thumb (edge at 66 - 2 Kp degrees) reaches this magnetic latitude."""
    return max(0.0, (66.0 - offset - abs_mlat) / 2.0)


def kp_text(kp: float | None) -> str:
    if kp is None:
        return "–"
    s = f"{kp:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _about(kp: float) -> str:
    return str(int(math.floor(kp + 0.5)))


def _cloud_words(pct: float | None) -> str | None:
    if pct is None:
        return None
    if pct >= 75:
        return "cloudy"
    if pct >= 40:
        return "partly cloudy"
    return "mostly clear"


def _cloud_clause(pct: float | None) -> str:
    """Cloud in the dark hours, as the end of a sentence."""
    if pct is None:
        return ""
    if pct >= 75:
        return f", but cloud ({int(round(pct))}% cover) will probably hide it"
    if pct >= 40:
        return f", if the cloud ({int(round(pct))}% cover) breaks"
    return "; the sky should be mostly clear"


def _cloud_now(pct: float | None) -> str:
    """Cloud right now, as the end of a sentence that says it is dark."""
    if pct is None:
        return ""
    if pct >= 75:
        return f", but cloud ({int(round(pct))}% cover) will probably hide it"
    if pct >= 40:
        return f", with some cloud ({int(round(pct))}% cover)"
    return " and mostly clear"


def window_kp(slots: list[dict], start: float, end: float) -> tuple[float | None, float | None]:
    """Highest Kp over the 3-hour slots overlapping [start, end]: (kp, slot start)."""
    best = None
    when = None
    for s in slots or []:
        t = s.get("time")
        if t is None or t + 3 * 3600 <= start or t >= end:
            continue
        if best is None or s["kp"] > best:
            best, when = s["kp"], t
    return best, when


def window_cloud(weather: dict | None, start: float, end: float) -> tuple[float | None, int]:
    if not weather:
        return None, 0
    vals = [h["cloud_pct"] for h in weather.get("hourly") or []
            if h.get("cloud_pct") is not None and start - 1800 <= h["time"] <= end + 1800]
    if not vals:
        return None, 0
    return sum(vals) / len(vals), len(vals)


def _fmt_date(t: float) -> str:
    d = datetime.fromtimestamp(t, tz=timezone.utc)
    return f"{d.day} {d.strftime('%b')}"


def aurora_here(lat: float, lon: float, now: float, *, grid: Grid | None, kp_slots: list[dict],
                kp_now: float | None, weather: dict | None) -> dict:
    """Everything the "Aurora chance here tonight" section needs, with one honest sentence."""
    mlat = cgm.lat(lat, lon)
    north = (mlat if mlat is not None else lat) >= 0
    side = "northern" if north else "southern"
    out: dict = {"lat": round(lat, 5), "lon": round(lon, 5), "cgm_lat": None if mlat is None else round(mlat, 1),
                 "hemisphere": "north" if north else "south", "kp_now": kp_now}
    dark = dark_window(lat, lon, now)
    out["dark"] = dark
    off = (weather or {}).get("utc_offset_s")
    out["utc_offset_s"] = off
    out["timezone"] = (weather or {}).get("timezone")
    mid = cgm.midnight_ut(lat, lon)
    out["magnetic_midnight_ut"] = None if mid is None else round(mid, 2)

    future = [s for s in kp_slots or [] if s.get("time") is not None and s["time"] + 3 * 3600 > now]
    fmax = max((s["kp"] for s in future[:16]), default=None)          # next two days
    if mlat is None or abs(mlat) < 30:
        out.update(relevant=bool(fmax is not None and fmax >= 8.67), chance="none",
                   kp_needed_overhead=None, kp_needed_horizon=None,
                   sentence="Aurora is almost never seen this close to the equator.")
        return out
    am = abs(mlat)
    need_over = kp_needed(am)
    need_hor = kp_needed(am, HORIZON_DEG)
    out["kp_needed_overhead"] = round(need_over, 1)
    out["kp_needed_horizon"] = round(need_hor, 1)
    # Beyond Kp 9 the rule of thumb stops; only extreme storms (well past G4) reach further.
    out["relevant"] = need_hor <= 9.0 or (fmax is not None and fmax >= 7.67)

    if grid is not None:
        out["ovation"] = {"time": grid.forecast, "observed": grid.observed,
                          "here_pct": round(grid.value(lat, lon), 1),
                          "poleward_pct": round(grid.poleward_max(lat, lon), 1)}
    else:
        out["ovation"] = None

    # "Tonight" is the current dark spell, or the next one when this one ends within 3 hours (morning)
    night = dark
    if dark["dark_now"] and dark["end"] is not None and dark["end"] - now < 3 * 3600:
        nxt = dark_window(lat, lon, dark["end"] + 900)
        if nxt["start"] is not None:
            night = nxt
    out["night"] = {"start": night["start"], "end": night["end"], "current": night is dark}
    win_start = night["start"]
    win_end = night["end"] if night["end"] is not None else (win_start + 12 * 3600 if win_start else None)
    if win_start is None:
        out["kp_tonight"] = None
        out["cloud"] = None
        out["chance"] = "none"
        out["sentence"] = (f"No aurora viewing here until about {_fmt_date(dark['returns'])}: the sky does not get dark "
                           "enough before then." if dark["returns"] else
                           "The sky does not get dark enough here at this time of year to see the aurora.")
        return out
    kp_t, kp_t_at = window_kp(kp_slots, win_start, win_end)
    if kp_t is None:
        kp_t = kp_now
    out["kp_tonight"] = None if kp_t is None else round(kp_t, 2)
    out["kp_tonight_time"] = kp_t_at
    cloud, n_cloud = window_cloud(weather, win_start, win_end)
    out["cloud"] = None if cloud is None else {"pct": round(cloud), "text": _cloud_words(cloud), "hours": n_cloud,
                                               "source": "Open-Meteo"}

    # 1. right now, from OVATION (its forecast is 30 to 90 minutes ahead)
    ov = out["ovation"]
    cloud_now = None
    if weather and weather.get("current"):
        cloud_now = weather["current"].get("cloud_pct")
    if dark["dark_now"] and ov:
        if ov["here_pct"] >= OVERHEAD_PCT:
            out["chance"] = "good"
            out["sentence"] = (f"Aurora may be overhead right now: OVATION gives {int(round(ov['here_pct']))}% here and "
                               f"the sky is dark{_cloud_now(cloud_now)}.")
            return out
        if ov["poleward_pct"] >= OVERHEAD_PCT:
            out["chance"] = "possible"
            out["sentence"] = (f"Aurora may be visible low on the {side} horizon right now (OVATION gives "
                               f"{int(round(ov['poleward_pct']))}% within about 330 km){_cloud_now(cloud_now)}.")
            return out

    # 2. tonight, from the Kp forecast and SWPC's rule of thumb
    when = "tonight" if win_start - now < 20 * 3600 else "on the next dark night"
    if kp_t is None:
        out["chance"] = "unknown"
        out["sentence"] = f"The Kp forecast is unavailable, so there is no outlook for {when}."
        return out
    if need_over <= 0.0:
        out["chance"] = "good" if kp_t >= 2 else "possible"
        where = f", often low in the {'southern' if north else 'northern'} sky" if am >= 74 else ""
        out["sentence"] = (f"Aurora is common here on dark nights{where}; Kp is forecast to reach {kp_text(kp_t)} "
                           f"{when}{_cloud_clause(cloud)}.")
    elif kp_t >= need_over:
        out["chance"] = "good"
        out["sentence"] = (f"Aurora could be overhead {when}: Kp is forecast to reach {kp_text(kp_t)}, and about Kp "
                           f"{_about(need_over)} is enough here{_cloud_clause(cloud)}.")
    elif kp_t >= need_hor:
        out["chance"] = "possible"
        out["sentence"] = (f"Aurora is possible low on the {side} horizon {when}: Kp is forecast to reach "
                           f"{kp_text(kp_t)}, and about Kp {_about(need_hor)} is enough here{_cloud_clause(cloud)}.")
    elif kp_t >= need_hor - 1.0:
        out["chance"] = "low"
        out["sentence"] = (f"A long shot {when}: Kp is forecast to reach {kp_text(kp_t)}, just short of the Kp "
                           f"{_about(need_hor)} needed to see aurora low on the {side} horizon from here.")
    elif need_hor > 9.0:
        out["chance"] = "unlikely"
        out["sentence"] = (f"Very unlikely {when}: only an extreme storm brings aurora this far from the poles, and the "
                           f"forecast peaks at Kp {kp_text(kp_t)}.")
    else:
        out["chance"] = "unlikely"
        out["sentence"] = (f"Unlikely {when}: aurora needs about Kp {_about(need_hor)} to be seen from here, and the "
                           f"forecast peaks at Kp {kp_text(kp_t)}.")
    return out

