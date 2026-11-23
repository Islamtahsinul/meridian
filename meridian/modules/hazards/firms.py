"""NASA FIRMS active fire detections (VIIRS on NOAA-20, NOAA-21 and Suomi NPP; MODIS on Terra and
Aqua). NASA open data; FIRMS asks users to cite "NASA FIRMS" (https://firms.modaps.eosdis.nasa.gov/).

Tens of thousands of detections a day live in compact column arrays with precomputed grid
aggregates, so view queries stay fast and memory stays a few MB.
"""

from __future__ import annotations

import csv
import io
import math
import re
from array import array
from datetime import datetime, timezone

from meridian import geo

from .common import clamp

FILES = "https://firms.modaps.eosdis.nasa.gov/data/active_fire/"
AREA_API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/world/1"

SATELLITES = {
    "noaa20": {"label": "NOAA-20", "instrument": "VIIRS", "api": "VIIRS_NOAA20_NRT",
               "file": FILES + "noaa-20-viirs-c2/csv/J1_VIIRS_C2_Global_24h.csv"},
    "noaa21": {"label": "NOAA-21", "instrument": "VIIRS", "api": "VIIRS_NOAA21_NRT",
               "file": FILES + "noaa-21-viirs-c2/csv/J2_VIIRS_C2_Global_24h.csv"},
    "snpp": {"label": "Suomi NPP", "instrument": "VIIRS", "api": "VIIRS_SNPP_NRT",
             "file": FILES + "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_24h.csv"},
    "modis": {"label": "Terra and Aqua", "instrument": "MODIS", "api": "MODIS_NRT",
              "file": FILES + "modis-c6.1/csv/MODIS_C6_1_Global_24h.csv"},
}
DEFAULT_SATELLITES = ("noaa20", "snpp", "modis")

SAT_NAMES = {"N": "Suomi NPP", "N20": "NOAA-20", "N21": "NOAA-21", "1": "NOAA-20", "2": "NOAA-21",
             "J1": "NOAA-20", "J2": "NOAA-21", "T": "Terra", "A": "Aqua", "Terra": "Terra", "Aqua": "Aqua"}
SAT_INSTRUMENT = {"T": "MODIS", "A": "MODIS", "Terra": "MODIS", "Aqua": "MODIS"}

# grid aggregate cell sizes (degrees) served at low zoom
AGG_CELLS = (2.0, 1.0, 0.5, 0.25)
INDEX_CELL = 1.0
VIIRS_CONF = {"l": -1, "low": -1, "n": -2, "nominal": -2, "h": -3, "high": -3}
CONF_TEXT = {-1: "Low", -2: "Nominal", -3: "High"}


def satellites_from_setting(text: str | None) -> list[str]:
    if not text:
        return list(DEFAULT_SATELLITES)
    wanted = [s.strip().lower().replace("-", "").replace("_", "") for s in text.split(",")]
    aliases = {"noaa20": "noaa20", "n20": "noaa20", "j1": "noaa20", "noaa21": "noaa21", "n21": "noaa21",
               "j2": "noaa21", "snpp": "snpp", "suominpp": "snpp", "npp": "snpp", "modis": "modis"}
    out = []
    for w in wanted:
        key = aliases.get(w)
        if key and key not in out:
            out.append(key)
    return out or list(DEFAULT_SATELLITES)


def confidence_text(code: int) -> str:
    if code < 0:
        return CONF_TEXT.get(code, "Unknown")
    return f"{code}%"


def confidence_rank(code: int) -> int:
    """0 low, 1 nominal, 2 high on one scale for both instruments."""
    if code < 0:
        return {-1: 0, -2: 1, -3: 2}.get(code, 1)
    return 0 if code < 30 else 2 if code >= 80 else 1


def _acq_time(date_s: str, hhmm: str) -> int | None:
    try:
        hhmm = (hhmm or "0").strip().zfill(4)
        dt = datetime.strptime(f"{date_s.strip()} {hhmm[:2]}:{hhmm[2:4]}", "%Y-%m-%d %H:%M")
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def fire_id(sat: str, lat: float, lon: float, t: int) -> str:
    stamp = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y%m%d%H%M")
    return f"firms:{sat}:{lat:.5f}:{lon:.5f}:{stamp}"


_ID = re.compile(r"^firms:([A-Za-z0-9]{1,6}):(-?\d{1,2}\.\d+):(-?\d{1,3}\.\d+):(\d{12})$")


def parse_id(fid: str) -> dict | None:
    m = _ID.match(fid or "")
    if not m:
        return None
    sat, lat, lon, stamp = m.groups()
    try:
        t = int(datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc).timestamp())
        la, lo = float(lat), float(lon)
    except ValueError:
        return None
    if not geo.valid_latlon(la, lo):
        return None
    return {"sat": sat, "lat": la, "lon": lo, "time": t}


class FireData:
    """Column store of detections with a 1-degree cell index and grid aggregates."""

    def __init__(self):
        self.lat = array("f")
        self.lon = array("f")
        self.frp = array("f")
        self.bright = array("f")
        self.bright2 = array("f")
        self.scan = array("f")
        self.track = array("f")
        self.conf = array("b")
        self.sat = array("B")
        self.t = array("q")
        self.night = array("B")
        self.sat_codes: list[str] = []
        self.cells: dict[tuple[int, int], array] = {}
        self.agg: dict[float, dict[tuple[int, int], list]] = {}
        self.sources: dict[str, int] = {}      # satellite key -> rows
        self.newest = 0
        self.oldest = 0

    def __len__(self) -> int:
        return len(self.lat)

    # -- loading ------------------------------------------------------------------------------
    def add_csv(self, text: str, key: str = "") -> int:
        """Append rows of a FIRMS CSV (file or area API flavour). Returns rows added."""
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header:
            return 0
        col = {h.strip().lower(): i for i, h in enumerate(header)}
        need = ("latitude", "longitude", "acq_date", "acq_time")
        if not all(n in col for n in need):
            return 0
        i_lat, i_lon, i_date, i_time = (col[n] for n in need)
        i_frp = col.get("frp")
        i_b = col.get("bright_ti4", col.get("brightness"))
        i_b2 = col.get("bright_ti5", col.get("bright_t31"))
        i_scan, i_track = col.get("scan"), col.get("track")
        i_sat, i_conf, i_dn = col.get("satellite"), col.get("confidence"), col.get("daynight")
        added = 0
        width = len(header)

        def f(row, i):
            if i is None or i >= len(row):
                return 0.0
            try:
                v = float(row[i])
            except ValueError:
                return 0.0
            return v if math.isfinite(v) else 0.0

        for row in reader:
            if len(row) < min(width, 4):
                continue
            try:
                la = float(row[i_lat])
                lo = float(row[i_lon])
            except (ValueError, IndexError):
                continue
            if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
                continue
            t = _acq_time(row[i_date], row[i_time])
            if t is None:
                continue
            sat_code = (row[i_sat].strip() if i_sat is not None and i_sat < len(row) else "") or "?"
            try:
                si = self.sat_codes.index(sat_code)
            except ValueError:
                self.sat_codes.append(sat_code)
                si = len(self.sat_codes) - 1
            raw_conf = row[i_conf].strip().lower() if i_conf is not None and i_conf < len(row) else ""
            if raw_conf in VIIRS_CONF:
                conf = VIIRS_CONF[raw_conf]
            else:
                try:
                    conf = int(clamp(float(raw_conf), 0, 100))
                except ValueError:
                    conf = -2
            self.lat.append(la)
            self.lon.append(lo)
            self.frp.append(f(row, i_frp))
            self.bright.append(f(row, i_b))
            self.bright2.append(f(row, i_b2))
            self.scan.append(f(row, i_scan))
            self.track.append(f(row, i_track))
            self.conf.append(conf)
            self.sat.append(si)
            self.t.append(t)
            dn = row[i_dn].strip().upper() if i_dn is not None and i_dn < len(row) else "D"
            self.night.append(1 if dn == "N" else 0)
            added += 1
        if key:
            self.sources[key] = self.sources.get(key, 0) + added
        return added

    def finish(self) -> "FireData":
        """Build the cell index and grid aggregates. Call once after the last add_csv."""
        cells: dict[tuple[int, int], array] = {}
        aggs: dict[float, dict] = {c: {} for c in AGG_CELLS}
        lat, lon, frp, t = self.lat, self.lon, self.frp, self.t
        for i in range(len(lat)):
            la, lo = lat[i], lon[i]
            key = (int(math.floor(la / INDEX_CELL)), int(math.floor(lo / INDEX_CELL)))
            bucket = cells.get(key)
            if bucket is None:
                bucket = cells[key] = array("l")
            bucket.append(i)
            fr = frp[i]
            ti = t[i]
            for size, table in aggs.items():
                k = (int(math.floor(la / size)), int(math.floor(lo / size)))
                a = table.get(k)
                if a is None:
                    table[k] = [1, fr, fr, ti, la, lo, i]
                else:
                    a[0] += 1
                    a[1] += fr
                    a[4] += la
                    a[5] += lo
                    if fr > a[2]:
                        a[2] = fr
                        a[6] = i
                    if ti > a[3]:
                        a[3] = ti
        self.cells = cells
        self.agg = aggs
        if len(t):
            self.newest = max(t)
            self.oldest = min(t)
        return self

    # -- reading --------------------------------------------------------------------------------
    def sat_code(self, i: int) -> str:
        return self.sat_codes[self.sat[i]]

    def row(self, i: int) -> dict:
        code = self.sat_code(i)
        la, lo = round(self.lat[i], 5), round(self.lon[i], 5)
        return {
            "id": fire_id(code, la, lo, self.t[i]),
            "lat": la, "lon": lo,
            "frp": round(self.frp[i], 1),
            "brightness": round(self.bright[i], 1),
            "brightness2": round(self.bright2[i], 1) or None,
            "scan": round(self.scan[i], 2) or None,
            "track": round(self.track[i], 2) or None,
            "confidence": confidence_text(self.conf[i]),
            "confidence_rank": confidence_rank(self.conf[i]),
            "satellite": SAT_NAMES.get(code, code),
            "satellite_code": code,
            "instrument": SAT_INSTRUMENT.get(code, "VIIRS"),
            "time": int(self.t[i]),
            "night": bool(self.night[i]),
        }

    def _cell_range(self, bbox):
        w, s, e, n = bbox
        r0, r1 = int(math.floor(s / INDEX_CELL)), int(math.floor(n / INDEX_CELL))
        if w <= e:
            cols = [(int(math.floor(w / INDEX_CELL)), int(math.floor(e / INDEX_CELL)))]
        else:
            cols = [(int(math.floor(w / INDEX_CELL)), int(math.floor(180.0 / INDEX_CELL))),
                    (int(math.floor(-180.0 / INDEX_CELL)), int(math.floor(e / INDEX_CELL)))]
        for c0, c1 in cols:
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    bucket = self.cells.get((r, c))
                    if bucket:
                        yield bucket

    def indices_in(self, bbox, min_conf: int = 0, since: int = 0) -> list[int]:
        out = []
        lat, lon, conf, t = self.lat, self.lon, self.conf, self.t
        for bucket in self._cell_range(bbox):
            for i in bucket:
                if since and t[i] < since:
                    continue
                if min_conf and confidence_rank(conf[i]) < min_conf:
                    continue
                if geo.in_bbox(lat[i], lon[i], bbox):
                    out.append(i)
        return out

    def count_in(self, bbox) -> int:
        return sum(len(b) for b in self._cell_range(bbox))

    def grid(self, bbox, size: float) -> list[dict]:
        table = self.agg.get(size) or {}
        out = []
        for (r, c), a in table.items():
            s = r * size
            w = c * size
            cell = (w, s, w + size, s + size)
            if not _bbox_touch(cell, bbox):
                continue
            n = a[0]
            out.append({"n": n, "frp_sum": a[1], "frp_max": a[2], "t": a[3],
                        "lat": a[4] / n, "lon": a[5] / n, "cell": cell, "top": a[6]})
        return out

    def near(self, lat: float, lon: float, km: float, limit: int = 10) -> list[tuple[float, int]]:
        bbox = geo.bbox_of_circle(lat, lon, km)
        found = []
        for i in self.indices_in(bbox):
            d = geo.haversine_km(lat, lon, self.lat[i], self.lon[i])
            if d <= km:
                found.append((d, i))
        found.sort()
        return found[:limit] if limit else found

    def find(self, fid: str) -> int | None:
        want = parse_id(fid)
        if not want:
            return None
        bbox = (want["lon"] - 0.001, want["lat"] - 0.001, want["lon"] + 0.001, want["lat"] + 0.001)
        for i in self.indices_in(bbox):
            if self.t[i] == want["time"] and self.sat_code(i) == want["sat"] \
                    and abs(self.lat[i] - want["lat"]) < 2e-4 and abs(self.lon[i] - want["lon"]) < 2e-4:
                return i
        return None

    def clusters(self, size: float = 0.5, min_n: int = 15, min_frp: float = 800.0, limit: int = 150) -> list[dict]:
        """Notable fire clusters for the events provider: grid cells with many detections or
        a lot of radiative power."""
        table = self.agg.get(size) or {}
        out = []
        for (r, c), a in table.items():
            if a[0] >= min_n or a[1] >= min_frp:
                n = a[0]
                out.append({"key": f"{r}:{c}", "n": n, "frp_sum": a[1], "frp_max": a[2], "t": a[3],
                            "lat": a[4] / n, "lon": a[5] / n, "top": a[6], "size": size})
        out.sort(key=lambda x: -x["frp_sum"])
        return out[:limit]


def _bbox_touch(cell, view) -> bool:
    w, s, e, n = view
    cw, cs, ce, cn = cell
    if cn < s or cs > n:
        return False
    if w <= e:
        return not (ce < w or cw > e)
    return ce >= w or cw <= e


def cell_for_zoom(zoom: float) -> float | None:
    """Grid size for a map zoom, or None when single detections should be drawn."""
    if zoom < 2.0:
        return 2.0
    if zoom < 3.5:
        return 1.0
    if zoom < 4.6:
        return 0.5
    if zoom < 5.8:
        return 0.25
    return None


def cluster_severity(frp_sum: float, n: int) -> int:
    if frp_sum >= 20000 or n >= 600:
        return 8
    if frp_sum >= 8000 or n >= 250:
        return 6
    if frp_sum >= 2500 or n >= 80:
        return 5
    if frp_sum >= 1000 or n >= 30:
        return 4
    return 3
