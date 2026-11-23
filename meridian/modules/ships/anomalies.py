"""Maritime anomaly detectors: ship-to-ship transfer candidates, loitering, AIS gap episodes near areas
of interest and GNSS spoofing clusters (docs/modules/ships.md, "Anomalies").

Every result is a candidate, never an accusation. Each one says what was seen and when, how confident
the detector is, the receiver coverage behind it, its limits, why it may be innocent and which vessels
are involved (with any sanctions-list matches). Known benign patterns are excluded before anything is
flagged:

- ports: within the port radius plus 3 km (infra ``ports.json`` or the built-in list);
- anchorages, waiting and transfer areas: the built-in list (``aoi.USUAL_AREAS``), cells where Meridian
  has seen 8 or more large vessels stopped for an hour or more in 14 days, cells where 3 or more vessels
  loitered on 5 or more of the last 14 days, and anywhere with 6 or more other stopped vessels within 3 km;
- pilot boarding areas: a pilot vessel heard within 3 km in the last hour;
- offshore installations and floating storage: a vessel stationary for 7 days or more within 3 km (5 km
  for loitering), or either vessel itself;
- classes whose work looks like this: fishing, service craft, pleasure craft, passenger ships (scheduled
  traffic) and military vessels (training and patrol).

Events (CONTRACT 5.6) use canonical kinds plus a ``category``:

==================  ===========  ====================  ========
detector            kind         category              severity
==================  ===========  ====================  ========
ship-to-ship        ship (MMSI)  ``sts_candidate``     3 to 7
loitering           ship (MMSI)  ``loitering``         3 to 5
AIS gap episode     ship (MMSI)  ``ais_gap``           3 to 6
GNSS spoofing       gps_jam      ``gnss_interference``  4 to 6   (id ``ships:<lat>,<lon>``)
==================  ===========  ====================  ========

Threads: ``run`` is called from the ships analysis task (one thread). Readers (routes, events, AI
tools) only see ``self.items`` and ``self.by_id``, which are swapped whole after each run.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import unicodedata
from collections import Counter

from meridian import geo

from .aoi import AreasOfInterest, aoi_phrase, cell_key, cell_neighbours, usual_area
from .classify import CLASS_IDS, CLASS_LABEL
from .mid import flag_of
from .ports import builtin_ports, port_radius_km
from .store import (F_CLASS_B, F_SUSPECT, R_CLS, R_LAT, R_LEN, R_LON, R_MMSI, R_SOG, R_SRC, R_T, R_TYPE,
                    SRC_LABELS)

try:                                   # the shared detector helpers (Operations team, meridian/stats.py)
    from meridian.stats import building_text, quantile
except Exception:                      # pragma: no cover - fall back to local copies
    def building_text(n, needed, unit="days"):
        return f"Building baseline: {max(0, int(n))} of {int(needed)} {unit}"

    def quantile(values, q):
        data = sorted(values)
        pos = q * (len(data) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(data) - 1)
        return data[lo] + (data[hi] - data[lo]) * (pos - lo)

CATEGORIES = {"sts": "sts_candidate", "loitering": "loitering", "ais_gap": "ais_gap", "gnss": "gnss_interference"}
KIND_OF = {v: k for k, v in CATEGORIES.items()}
KIND_ALIASES = {"sts": "sts", "sts_candidate": "sts", "ship_to_ship": "sts", "loitering": "loitering",
                "loiter": "loitering", "ais_gap": "ais_gap", "gap": "ais_gap", "gaps": "ais_gap", "gnss": "gnss",
                "gnss_interference": "gnss", "gnss_spoofing": "gnss", "spoofing": "gnss"}

# -- ship-to-ship -------------------------------------------------------------------------------
STS_MAX_KN = 2.0
STS_RANGE_KM = 0.5
STS_ALONGSIDE_KM = 0.3
STS_MIN_S = 30 * 60
STS_LONG_S = 2 * 3600
STS_FRESH_S = 30 * 60             # positions used for a pair are at most this old
STS_TOLERANCE_S = 30 * 60         # an episode survives this long without a new observation
STS_SEPARATED_KM = 1.0
STS_MIN_LEN = 60.0
STS_OTHER_MIN_LEN = 100.0
STS_BUNKER_LEN = 80.0             # bunker barges are mostly under this; bunker tankers can be larger, so it
                                  # is only a hint, and never applied with a sanctions match or in an area of interest
STS_QUIET_S = 15 * 60             # a silence this long during an episode is evidence
STS_QUIET_SEV_S = 20 * 60         # and this long adds to the severity
SUSPECT_CLEAR_S = 2 * 3600
PORT_MARGIN_KM = 3.0
CROWD_KM = 3.0
CROWD_MIN = 6
PILOT_KM = 3.0
FIXED_S = 7 * 86400
FIXED_KM = 3.0
COAST_KM = 1.0
DRAUGHT_M = 0.8
DRAUGHT_WATCH_S = 24 * 3600
# -- loitering ----------------------------------------------------------------------------------
LOITER_EVERY_S = 600.0
LOITER_WINDOW_S = 12 * 3600
LOITER_MIN_POINTS = 8
LOITER_MIN_S = 6 * 3600
LOITER_STAY_KM = 10.0
LOITER_RG_KM = 5.0
LOITER_MIN_KN = 0.5
LOITER_MAX_KN = 6.0
LOITER_MIN_PATH_KM = 10.0
LOITER_PATH_RATIO = 3.0
LOITER_DEST_KM = 50.0              # about as far out as ports' anchorages and waiting areas reach
LOITER_DEST_SLACK_KM = 25.0        # and no further than this beyond the nearest listed port (on its approach)
LOITER_FIXED_KM = 5.0
LOITER_LEARN_DAYS = 7
LOITER_DAY_RUNS = 36               # a day counts for learning with 6 h of checks (every 10 minutes)
# -- learning (shared) ----------------------------------------------------------------------------
LEARN_EVERY_S = 900.0
LEARN_DAYS = 14
STOP_DAY_RUNS = 24                 # 6 h of stop learning (every 15 minutes)
STOP_CELL_DEG = 0.05
LOITER_CELL_DEG = 0.1
HABITUAL_STOP_VESSELS = 8
HABITUAL_LOITER_VESSELS = 3
HABITUAL_LOITER_DAYS = 5
KEEP_CELLS_DAYS = 15
# -- AIS gaps ------------------------------------------------------------------------------------
GAP_SETTLE_S = 2 * 3600
QUIET_AFTER_S = 3 * 3600
QUIET_MAX_S = 24 * 3600
GAP_COVER_KM = 30.0
GAP_NEAR_TANKER_KM = 1.0
# -- GNSS ------------------------------------------------------------------------------------------
GNSS_WINDOW_S = 1800
GNSS_HOLD_S = 6 * 3600
GNSS_JOIN_KM = 10.0
GNSS_GROUP_KM = 30.0
GNSS_SPEED_KM = 10.0
GNSS_COMMON_KM = 1.5
GNSS_JUMP_MIN = 3
GNSS_CIRCLE_MIN = 8                # distinct positions (near-duplicates per vessel merged)
GNSS_CIRCLE_VESSELS = 3
GNSS_CIRCLE_SPREAD = 0.08
GNSS_CIRCLE_GAP = 120.0            # largest angular gap allowed, so the points cover several sectors
GNSS_CIRCLE_MERGE_KM = 0.5         # one vessel's positions this close count once
GNSS_JUMPS_PER_VESSEL = 4          # latest jumps per vessel used in one run
GNSS_LAND_KM = 3.0
GNSS_LAND_MIN = 3
GNSS_DAY_CELL_DEG = 0.5
AIR_EVERY_S = 600.0
# -- output ----------------------------------------------------------------------------------------
RECENT_S = 48 * 3600               # in memory and on the layer by default
KEEP_DB_S = 30 * 86400             # ship-to-ship, loitering and gap episodes
KEEP_GNSS_S = 90 * 86400
MAX_HOURS = 30 * 24
SAVE_EVERY_S = 300.0

STS_EXCLUDED_TEXT = ("Pairs in or near ports, at pilot boarding areas, next to offshore units, in built-in or learned "
                     "anchorages (including transfer anchorages such as the Laconian Gulf, off Johor and off Fujairah) "
                     "and close inshore are not flagged, so no flags there does not mean no transfers. Inside areas of "
                     "interest such pairs are listed as unflagged items; a sanctioned vessel is not cleared by a usual "
                     "anchorage.")
NOT_CANDIDATE_CLASSES = ("fishing", "service", "pleasure", "passenger", "highspeed", "military")
LIST_NAMES = {"OFAC": "OFAC SDN list", "UK": "UK Sanctions List", "UN": "UN Security Council list",
              "EU": "EU financial sanctions list"}
SRC_PHRASE = {1: "Digitraffic", 2: "aisstream.io", 3: "Kystverket"}


# =============================================================================================
# Small helpers
# =============================================================================================
def duration_text(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min" if m and h < 10 else f"{h} h"
    d, h = divmod(s // 3600, 24)
    return f"{d} day{'s' if d != 1 else ''} {h} h" if h else f"{d} day{'s' if d != 1 else ''}"


def km_text(km: float) -> str:
    if km < 1.0:
        return f"{int(round(km * 1000 / 10.0)) * 10} m"
    if km < 10:
        return f"{km:.1f} km".replace(".0 km", " km")
    return f"{int(round(km))} km"


def utc_text(t: float, with_day: bool = False) -> str:
    g = time.gmtime(t)
    hm = f"{g.tm_hour:02d}:{g.tm_min:02d} UTC"
    if not with_day:
        return hm
    return f"{g.tm_mday} {time.strftime('%b', g)} {hm}"


def lists_text(lists) -> str:
    names = [LIST_NAMES.get(x, x) for x in (lists or [])] or ["a sanctions list"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"[^A-Z]", "", t.encode("ascii", "ignore").decode("ascii").upper())


def _mean_latlon(points) -> tuple[float, float]:
    """Mean of (lat, lon) pairs, unwrapping longitudes around the first point."""
    lat0, lon0 = points[0]
    sx = sy = 0.0
    for lat, lon in points:
        d = (lon - lon0 + 540.0) % 360.0 - 180.0
        sx += lon0 + d
        sy += lat
    n = len(points)
    return sy / n, ((sx / n + 540.0) % 360.0) - 180.0


def _local_xy(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    d = (lon - lon0 + 540.0) % 360.0 - 180.0
    return d * 111.32 * math.cos(math.radians(lat0)), (lat - lat0) * 110.57


def _from_xy(lat0: float, lon0: float, x: float, y: float) -> tuple[float, float]:
    lat = lat0 + y / 110.57
    lon = lon0 + x / (111.32 * max(0.01, math.cos(math.radians(lat0))))
    return lat, ((lon + 540.0) % 360.0) - 180.0


# =============================================================================================
# Pure detector parts (tests drive these with synthetic tracks)
# =============================================================================================
def loiter_metrics(points) -> dict | None:
    """Loiter shape of a track: [(t, lat, lon, sog, cog)], oldest first. Uses the recent stretch the vessel
    spent within 10 km of its latest position; None unless it is loiter-shaped (8 or more points over 6 h
    or more, radius of gyration 5 km or less, mean speed 0.5 to 6 kn, path at least 10 km and at least
    three times the net displacement)."""
    pts = [(float(p[0]), float(p[1]), float(p[2]), p[3]) for p in points if p[1] is not None and p[2] is not None]
    if len(pts) < LOITER_MIN_POINTS:
        return None
    last = pts[-1]
    i = len(pts) - 1
    while i > 0 and geo.haversine_km(last[1], last[2], pts[i - 1][1], pts[i - 1][2]) <= LOITER_STAY_KM:
        i -= 1
    seg = pts[i:]
    if len(seg) < LOITER_MIN_POINTS:
        return None
    span = seg[-1][0] - seg[0][0]
    if span < LOITER_MIN_S:
        return None
    clat, clon = _mean_latlon([(p[1], p[2]) for p in seg])
    dists = [geo.haversine_km(clat, clon, p[1], p[2]) for p in seg]
    rg = math.sqrt(sum(d * d for d in dists) / len(dists))
    path = sum(geo.haversine_km(a[1], a[2], b[1], b[2]) for a, b in zip(seg, seg[1:]))
    net = geo.haversine_km(seg[0][1], seg[0][2], seg[-1][1], seg[-1][2])
    mean_kn = path / geo.KM_PER_NM / (span / 3600.0)
    if rg > LOITER_RG_KM or path < LOITER_MIN_PATH_KM or path < LOITER_PATH_RATIO * max(net, 0.01):
        return None
    if not LOITER_MIN_KN <= mean_kn <= LOITER_MAX_KN:
        return None
    sogs = [float(p[3]) for p in seg if p[3] is not None]
    lo, hi = (quantile(sogs, 0.1), quantile(sogs, 0.9)) if len(sogs) >= 3 else (mean_kn, mean_kn)
    step = max(1, len(seg) // 80)
    track = [[round(p[2], 5), round(p[1], 5)] for p in seg[::step]]
    if track[-1] != [round(seg[-1][2], 5), round(seg[-1][1], 5)]:
        track.append([round(seg[-1][2], 5), round(seg[-1][1], 5)])
    return {"t0": seg[0][0], "t1": seg[-1][0], "lat": round(clat, 5), "lon": round(clon, 5), "rg_km": rg,
            "max_km": max(dists), "path_km": path, "net_km": net, "mean_kn": mean_kn, "points": len(seg),
            "sog_lo": lo, "sog_hi": hi, "track": track}


def gap_reading(dist_km: float, hours: float, sog0: float | None, stopped: bool) -> tuple[float | None, str]:
    """(ratio, reading) for a gap: distance between disappearance and reappearance against what the last
    speed would have covered, ``r = distance / (speed x hours x 1.852)`` capped at 2."""
    if stopped or sog0 is None or sog0 < 1.0:
        if dist_km < 3.0:
            return None, "stayed in the same place"
        return None, f"moved {km_text(dist_km)} although it was stopped when it went silent"
    expected = sog0 * geo.KM_PER_NM * hours
    r = min(2.0, dist_km / expected) if expected > 0.5 else None
    if r is None:
        return None, "moved little"
    if r < 0.3:
        return r, "stopped or stayed near the same place during the gap"
    if r < 0.7:
        return r, "moved less far than its last speed suggests (it slowed down or changed course)"
    if r <= 1.3:
        return r, "sailed on through a coverage hole"
    return r, "moved further than its last speed suggests (it sped up or its last speed was wrong)"


def fit_circle(points: list[tuple[float, float]]) -> tuple[float, float, float, float, float] | None:
    """Least-squares circle (Kasa) through (x, y) km points: (cx, cy, r, worst relative deviation,
    largest angular gap in degrees), or None when degenerate."""
    n = len(points)
    if n < 3:
        return None
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    syy = sum(p[1] * p[1] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    sz = [p[0] * p[0] + p[1] * p[1] for p in points]
    szx = sum(z * p[0] for z, p in zip(sz, points))
    szy = sum(z * p[1] for z, p in zip(sz, points))
    szz = sum(sz)
    # Normal equations for z + D x + E y + F = 0.
    a = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]]
    b = [-szx, -szy, -szz]
    det = _det3(a)
    if abs(det) < 1e-9:
        return None
    sol = []
    for col in range(3):
        m = [row[:] for row in a]
        for r in range(3):
            m[r][col] = b[r]
        sol.append(_det3(m) / det)
    d, e, f = sol
    cx, cy = -d / 2.0, -e / 2.0
    rr = cx * cx + cy * cy - f
    if rr <= 0:
        return None
    r = math.sqrt(rr)
    dev = max(abs(math.hypot(p[0] - cx, p[1] - cy) - r) for p in points) / r
    angles = sorted(math.degrees(math.atan2(p[1] - cy, p[0] - cx)) % 360.0 for p in points)
    gaps = [b2 - a2 for a2, b2 in zip(angles, angles[1:])] + [angles[0] + 360.0 - angles[-1]]
    return cx, cy, r, dev, max(gaps)


def _det3(m) -> float:
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def group_points(points: list[dict], km: float) -> list[list[dict]]:
    """Single-link groups of {"lat", "lon"} points within ``km`` of each other."""
    if not points:
        return []
    cell = max(0.05, km / 111.0)
    grid: dict[tuple[int, int], list[int]] = {}
    for i, p in enumerate(points):
        grid.setdefault((int(math.floor(p["lat"] / cell)), int(math.floor(p["lon"] / cell))), []).append(i)
    parent = list(range(len(points)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for (r, c), idx in grid.items():
        near = [j for dr in (-1, 0, 1) for dc in (-1, 0, 1) for j in grid.get((r + dr, c + dc), ())]
        for i in idx:
            for j in near:
                if j > i and find(i) != find(j) and geo.haversine_km(points[i]["lat"], points[i]["lon"], points[j]["lat"],
                                              points[j]["lon"]) <= km:
                    parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i, p in enumerate(points):
        groups.setdefault(find(i), []).append(p)
    return list(groups.values())


def gnss_signatures(group: list[dict], land=None) -> dict | None:
    """Signatures in one group of suspect points from the last 30 minutes. Points are
    {"mmsi", "lat", "lon", "t", "kind": "speed"|"jump", "sog", "from": (lat, lon) for jumps}.

    - speed: 2 or more vessels report impossible speeds (over 45 kn) within 10 km;
    - jump: 3 or more vessels jumped 5 km or more to within 1.5 km of a common point;
    - circle: 8 or more distinct suspect positions (one vessel's positions within 0.5 km merged) from 3 or
      more vessels lie at nearly equal distances from a centre (worst deviation under 8% of the radius,
      radius 1 to 50 km, no angular gap over 120 degrees). Fewer points fit a circle by chance;
    - land: 3 or more vessels jumped from the sea to 3 km or more inland."""
    vessels = {p["mmsi"] for p in group}
    if len(vessels) < 2:
        return None
    sigs: list[str] = []
    centre = None
    speed = [p for p in group if p["kind"] == "speed"]
    if len({p["mmsi"] for p in speed}) >= 2:
        clat, clon = _mean_latlon([(p["lat"], p["lon"]) for p in speed])
        close = {p["mmsi"] for p in speed if geo.haversine_km(clat, clon, p["lat"], p["lon"]) <= GNSS_SPEED_KM}
        if len(close) >= 2:
            sigs.append("speed")
            centre = (clat, clon)
    jumps = [p for p in group if p["kind"] == "jump"]
    best = None
    cell = GNSS_COMMON_KM / 111.0
    grid: dict[tuple[int, int], list[dict]] = {}
    for p in jumps:
        grid.setdefault((int(math.floor(p["lat"] / cell)), int(math.floor(p["lon"] / cell))), []).append(p)
    for p in jumps:
        r, c = int(math.floor(p["lat"] / cell)), int(math.floor(p["lon"] / cell))
        # Longitude cells shrink with latitude: look far enough east and west to cover 1.5 km.
        w = max(1, int(math.ceil(1.0 / max(0.05, math.cos(math.radians(p["lat"]))))))
        near = [q for dr in (-1, 0, 1) for dc in range(-w, w + 1) for q in grid.get((r + dr, c + dc), ())
                if geo.haversine_km(p["lat"], p["lon"], q["lat"], q["lon"]) <= GNSS_COMMON_KM]
        n = len({q["mmsi"] for q in near})
        if n >= GNSS_JUMP_MIN and (best is None or n > best[0]):
            best = (n, near)
    common = None
    if best:
        sigs.append("jump")
        common = _mean_latlon([(q["lat"], q["lon"]) for q in best[1]])
        centre = common
    circle = None
    pts: list[dict] = []
    kept: dict[int, list[dict]] = {}
    for p in speed + jumps:              # one vessel's repeated fix (or a speed fix that is also a jump end) once
        own = kept.setdefault(p["mmsi"], [])
        if not any(geo.haversine_km(p["lat"], p["lon"], q["lat"], q["lon"]) <= GNSS_CIRCLE_MERGE_KM for q in own):
            own.append(p)
            pts.append(p)
    if len(pts) >= GNSS_CIRCLE_MIN and len({p["mmsi"] for p in pts}) >= GNSS_CIRCLE_VESSELS:
        lat0, lon0 = _mean_latlon([(p["lat"], p["lon"]) for p in pts])
        fit = fit_circle([_local_xy(lat0, lon0, p["lat"], p["lon"]) for p in pts])
        if fit is not None:
            cx, cy, r, dev, gap = fit
            if 1.0 <= r <= 50.0 and dev < GNSS_CIRCLE_SPREAD and gap <= GNSS_CIRCLE_GAP:
                sigs.append("circle")
                circle = {"radius_km": round(r, 1), "deviation": round(dev, 3)}
                centre = _from_xy(lat0, lon0, cx, cy)
    inland: set[int] = set()
    if land is not None and jumps:
        for p in jumps:
            if p["mmsi"] in inland:
                continue
            k = land.inland_km(p["lat"], p["lon"])
            if k is None or k < GNSS_LAND_KM:
                continue
            o = land.inland_km(*p["from"]) if p.get("from") else None
            if o is not None and o < 1.0:              # it came from the sea: not a river or lake craft
                inland.add(p["mmsi"])
        if len(inland) >= GNSS_LAND_MIN:
            sigs.append("land")
    if not sigs:
        return None
    if centre is None:
        centre = _mean_latlon([(p["lat"], p["lon"]) for p in group])
    spread = max(geo.haversine_km(centre[0], centre[1], p["lat"], p["lon"]) for p in group)
    sogs = [p["sog"] for p in speed if p.get("sog") is not None]
    return {"lat": round(centre[0], 4), "lon": round(centre[1], 4), "radius_km": round(min(60.0, max(5.0, spread + 3.0)), 1),
            "vessels": len(vessels), "mmsis": sorted(vessels)[:40], "signatures": sigs,
            "max_sog": max(sogs) if sogs else None, "t": max(p["t"] for p in group),
            "t_first": min(p["t"] for p in group), "points": len(group), "jumps": len(jumps),
            "common": [round(common[0], 4), round(common[1], 4)] if common else None, "circle": circle,
            "inland_vessels": len(inland)}


# =============================================================================================
# The engine
# =============================================================================================
class AnomalyEngine:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS ships_sts (
        id TEXT PRIMARY KEY, a INTEGER, b INTEGER, t0 REAL, t1 REAL, lat REAL, lon REAL, min_m REAL,
        state TEXT, severity INTEGER, published INTEGER, info TEXT, updated REAL);
    CREATE INDEX IF NOT EXISTS ships_sts_t1 ON ships_sts(t1);
    CREATE TABLE IF NOT EXISTS ships_loiter (
        id TEXT PRIMARY KEY, mmsi INTEGER, t0 REAL, t1 REAL, lat REAL, lon REAL, rg_km REAL, path_km REAL,
        state TEXT, severity INTEGER, published INTEGER, info TEXT, updated REAL);
    CREATE INDEX IF NOT EXISTS ships_loiter_t1 ON ships_loiter(t1);
    CREATE TABLE IF NOT EXISTS ships_gnss (
        id TEXT PRIMARY KEY, first_t REAL, last_t REAL, lat REAL, lon REAL, radius_km REAL, vessels INTEGER,
        max_sog REAL, signatures TEXT, air TEXT, severity INTEGER, info TEXT);
    CREATE INDEX IF NOT EXISTS ships_gnss_last ON ships_gnss(last_t);
    CREATE TABLE IF NOT EXISTS ships_gnss_days (cell INTEGER NOT NULL, day INTEGER NOT NULL,
        PRIMARY KEY (cell, day)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS ships_stopcells (cell INTEGER NOT NULL, day INTEGER NOT NULL, mmsi INTEGER NOT NULL,
        PRIMARY KEY (cell, day, mmsi)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS ships_stopcells_day ON ships_stopcells(day);
    CREATE TABLE IF NOT EXISTS ships_loitercells (cell INTEGER NOT NULL, day INTEGER NOT NULL, mmsi INTEGER NOT NULL,
        PRIMARY KEY (cell, day, mmsi)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS ships_loitercells_day ON ships_loitercells(day);
    """
    KV_DAYS = "ships.anomalies.days"

    def __init__(self, store, *, db=None, ports=None, land=None, now=time.time, log=None, air_lookup=None):
        self.store = store                       # VesselStore
        self.db = db
        self.ports = ports or (lambda: None)     # -> GridIndex of port dicts (the service's port_grid)
        self.land = land                         # LandIndex or None
        self.aoi = AreasOfInterest(store.straits.defs if store.straits is not None else [])
        self.now = now
        self.log = log
        self.air_lookup = air_lookup             # fn(lat, lon, radius_km, since) -> {"level", "share", ...} | None
        self.sts: dict[tuple[int, int], dict] = {}
        self.loiter: dict[int, dict] = {}
        self.gaps: dict[str, dict] = {}
        self.quiet: dict[int, dict] = {}
        self.gnss: list[dict] = []
        self.items: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.habitual_stop: set[int] = set()
        self.habitual_loiter: dict[int, tuple[int, int]] = {}
        self.days: dict[str, dict[str, int]] = {}
        self.excluded = {"sts": Counter(), "loitering": Counter(), "ais_gap": Counter(), "gnss": Counter()}
        self.last = {"sts": 0.0, "loitering": 0.0, "ais_gap": 0.0, "gnss": 0.0, "learn": 0.0, "habitual": 0.0,
                     "prune": 0.0}
        self.errors: dict[str, str | None] = {}
        self.started = now()
        self._written: set[tuple[int, int, int]] = set()
        self._written_day = None
        self._builtin_ports = builtin_ports()
        self._fixed_grid = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------------------------------
    def create_tables(self) -> None:
        if self.db is not None:
            self.db.script(self.SCHEMA)

    def restore(self) -> int:
        """Episodes of the last 48 hours, so a restart continues them instead of starting again."""
        if self.db is None:
            return 0
        now = self.now()
        n = 0
        try:
            self.days = self.db.kv_get(self.KV_DAYS, {}) or {}
            if not isinstance(self.days, dict):
                self.days = {}
        except Exception:
            self.days = {}
        for r in self.db.query("SELECT info FROM ships_sts WHERE t1 >= ?", (now - RECENT_S,)):
            ep = (_loads(r["info"]) or {}).get("ep")
            if isinstance(ep, dict) and ep.get("a") and ep.get("b"):
                if ep.get("state") == "open" and now - ep.get("t1", 0) > STS_TOLERANCE_S:
                    ep["state"], ep["closed_t"] = "closed", ep.get("t1")
                self.sts[(int(ep["a"]), int(ep["b"]))] = ep
                n += 1
        for r in self.db.query("SELECT info FROM ships_loiter WHERE t1 >= ?", (now - RECENT_S,)):
            ep = (_loads(r["info"]) or {}).get("ep")
            if isinstance(ep, dict) and ep.get("mmsi"):
                if ep.get("state") == "open" and now - ep.get("seen", 0) > 2 * LOITER_EVERY_S + 600:
                    ep["state"] = "closed"
                self.loiter[int(ep["mmsi"])] = ep
                n += 1
        for r in self.db.query("SELECT info FROM ships_gaps WHERE t1 >= ? AND info IS NOT NULL", (now - RECENT_S,)):
            ep = (_loads(r["info"]) or {}).get("ep")
            if isinstance(ep, dict) and ep.get("id"):
                ep["settled"] = True
                self.gaps[ep["id"]] = ep
                n += 1
        for r in self.db.query("SELECT info FROM ships_gnss WHERE last_t >= ?", (now - RECENT_S,)):
            ep = (_loads(r["info"]) or {}).get("ep")
            if isinstance(ep, dict) and ep.get("id"):
                self.gnss.append(ep)
                n += 1
        self._sync_hotspots(now)
        self._load_habitual(now)
        self._build(now)
        return n

    def _save(self, table: str, ep: dict, item: dict | None, now: float, force: bool = False) -> None:
        if self.db is None or (not force and now - ep.get("saved_t", 0) < SAVE_EVERY_S):
            return
        ep["saved_t"] = now
        info = json.dumps({"ep": ep, "item": item}, separators=(",", ":"), default=str)
        try:
            if table == "sts":
                self.db.execute("INSERT OR REPLACE INTO ships_sts(id, a, b, t0, t1, lat, lon, min_m, state, severity, "
                                "published, info, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (ep["id"], ep["a"], ep["b"], ep["t0"], ep["t1"], ep["lat"], ep["lon"], ep["min_m"],
                                 (item or {}).get("state"), (item or {}).get("severity"),
                                 int(bool((item or {}).get("published"))), info, now))
            elif table == "loiter":
                self.db.execute("INSERT OR REPLACE INTO ships_loiter(id, mmsi, t0, t1, lat, lon, rg_km, path_km, state, "
                                "severity, published, info, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (ep["id"], ep["mmsi"], ep["t0"], ep["t1"], ep["lat"], ep["lon"], ep["rg_km"],
                                 ep["path_km"], (item or {}).get("state"), (item or {}).get("severity"),
                                 int(bool((item or {}).get("published"))), info, now))
            elif table == "gap":
                ctx = ep.get("ctx") or {}
                self.db.execute(
                    "INSERT INTO ships_gaps(mmsi, t0, t1, lat0, lon0, lat1, lon1) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(mmsi, t0) DO NOTHING",
                    (ep["mmsi"], ep["t0"], ep["t1"], ep["lat0"], ep["lon0"], ep["lat1"], ep["lon1"]))
                self.db.execute(
                    "UPDATE ships_gaps SET sog0 = ?, cog0 = ?, draught0 = ?, draught1 = ?, dest0 = ?, dest1 = ?, "
                    "name0 = ?, name1 = ?, imo0 = ?, imo1 = ?, aoi = ?, stopped = ?, severity = ?, info = ? "
                    "WHERE mmsi = ? AND t0 = ?",
                    (ctx.get("sog0"), ctx.get("cog0"), ctx.get("draught0"), ep.get("draught1"), ctx.get("dest0"),
                     ep.get("dest1"), ctx.get("name0"), ep.get("name1"), ctx.get("imo0"), ep.get("imo1"),
                     (ep.get("aoi") or {}).get("id"), int(bool(ctx.get("stopped"))), (item or {}).get("severity"),
                     info, ep["mmsi"], ep["t0"]))
            elif table == "gnss":
                self.db.execute("INSERT OR REPLACE INTO ships_gnss(id, first_t, last_t, lat, lon, radius_km, vessels, "
                                "max_sog, signatures, air, severity, info) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (ep["id"], ep["first_t"], ep["t"], ep["lat"], ep["lon"], ep["radius_km"],
                                 ep["vessels"], ep.get("max_sog"), ",".join(ep.get("signatures") or []),
                                 (ep.get("air") or {}).get("level"), (item or {}).get("severity"), info))
        except Exception as err:          # storage trouble never stops the detectors
            if self.log:
                self.log.warning("ships anomalies: could not save %s: %s", table, err)

    def prune(self, now: float | None = None) -> None:
        now = self.now() if now is None else now
        if self.db is None:
            return
        day = int(now // 86400)
        self.db.execute("DELETE FROM ships_sts WHERE t1 < ?", (now - KEEP_DB_S,))
        self.db.execute("DELETE FROM ships_loiter WHERE t1 < ?", (now - KEEP_DB_S,))
        self.db.execute("DELETE FROM ships_gnss WHERE last_t < ?", (now - KEEP_GNSS_S,))
        self.db.execute("DELETE FROM ships_gnss_days WHERE day < ?", (day - KEEP_GNSS_S // 86400,))
        self.db.execute("DELETE FROM ships_stopcells WHERE day < ?", (day - KEEP_CELLS_DAYS,))
        self.db.execute("DELETE FROM ships_loitercells WHERE day < ?", (day - KEEP_CELLS_DAYS,))

    # ------------------------------------------------------------------------------------------
    # The periodic run
    # ------------------------------------------------------------------------------------------
    def run(self, snapshot, receiving: dict | None = None, now: float | None = None) -> None:
        now = self.now() if now is None else now
        scene = self._scene(snapshot, now)
        steps = [("sts", lambda: self._run_sts(scene, now)), ("ais_gap", lambda: self._run_quiet(scene, now, receiving or {})),
                 ("ais_gap", lambda: self._run_gaps(now)), ("gnss", lambda: self._run_gnss(now))]
        if now - self.last["loitering"] >= LOITER_EVERY_S:
            steps.append(("loitering", lambda: self._run_loiter(scene, now)))
        if now - self.last["learn"] >= LEARN_EVERY_S:
            steps.append(("learn", lambda: self._learn_stops(now)))
        if now - self.last["habitual"] >= 3600:
            steps.append(("learn", lambda: self._load_habitual(now)))
        if now - self.last["prune"] >= 6 * 3600:
            steps.append(("prune", lambda: (self.prune(now), self.last.__setitem__("prune", now))))
        for name, fn in steps:
            try:
                fn()
                self.errors[name] = None
            except Exception as err:   # one detector failing never stops the others
                self.errors[name] = f"{type(err).__name__}: {err}"[:300]
                if self.log:
                    self.log.warning("ships anomalies: %s failed: %s", name, err, exc_info=True)
        self._build(now)

    # -- scene --------------------------------------------------------------------------------------
    def _scene(self, snapshot, now: float) -> dict:
        stopped = geo.GridIndex(0.05)
        pilots = geo.GridIndex(0.1)
        for r in snapshot.rows:
            age = now - r[R_T]
            if age > 3600:
                continue
            if r[R_TYPE] == 50:
                pilots.add(r[R_LAT], r[R_LON], r[R_MMSI])
            if r[R_SOG] is not None and r[R_SOG] <= 10 and CLASS_IDS[r[R_CLS]] != "pleasure":
                stopped.add(r[R_LAT], r[R_LON], r[R_MMSI])
        fixed = geo.GridIndex(0.1)
        with self.store.lock:
            for v in self.store.vessels.values():
                if v.state == "stationary" and v.lat is not None and now - v.t <= 6 * 3600 \
                        and v.state_since and now - v.state_since >= FIXED_S:
                    fixed.add(v.lat, v.lon, v.mmsi)
        self._fixed_grid = fixed
        return {"snap": snapshot, "rows": snapshot.by_mmsi, "stopped": stopped, "pilots": pilots, "fixed": fixed}

    def _infos(self, mmsis, now: float) -> dict[int, dict]:
        out = {}
        with self.store.lock:
            for m in mmsis:
                v = self.store.vessels.get(int(m))
                if v is None:
                    continue
                sanc = None
                if v.sanc:
                    strong = v.sanctioned
                    sanc = {"strength": "listed" if strong else "possible", "match": v.sanc.get("match"),
                            "lists": list(v.sanc.get("lists") or ["OFAC"]), "listed_as": v.sanc.get("name"),
                            "uid": v.sanc.get("uid")}
                out[v.mmsi] = {"mmsi": v.mmsi, "name": v.name or "", "cls": v.cls, "type": v.ship_type,
                               "length": v.length, "imo": v.imo, "draught": v.draught, "dest": v.dest or "",
                               "nav": v.nav, "sog": v.sog, "lat": v.lat, "lon": v.lon, "t": v.t, "ais": v.ais,
                               "src": v.src, "suspect_t": v.suspect_t, "state": v.state,
                               "state_since": v.state_since, "sanc": sanc, "sanc_strong": bool(v.sanctioned)}
        return out

    # -- places -------------------------------------------------------------------------------------
    def port_near(self, lat: float, lon: float) -> tuple[dict, float] | None:
        grid = self.ports()
        if grid is None:
            return None
        for d, p in grid.near(lat, lon, 15.0 + PORT_MARGIN_KM):
            if d <= port_radius_km(p.get("size")) + PORT_MARGIN_KM:
                return p, d
        return None

    def nearest_port(self, lat: float, lon: float, km: float = 150.0) -> tuple[dict, float] | None:
        grid = self.ports()
        if grid is None:
            return None
        hit = grid.near(lat, lon, km, 1)
        return (hit[0][1], hit[0][0]) if hit else None

    def _pilot_near(self, lat, lon, scene) -> bool:
        return bool(scene["pilots"].near(lat, lon, PILOT_KM, 1))

    def _fixed_near(self, lat, lon, scene, km, exclude=()) -> int | None:
        for _d, m in scene["fixed"].near(lat, lon, km):
            if m not in exclude:
                return m
        return None

    def _crowd(self, lat, lon, scene, exclude=()) -> int:
        return sum(1 for _d, m in scene["stopped"].near(lat, lon, CROWD_KM) if m not in exclude)

    def _learned_stop(self, lat, lon) -> bool:
        if not self.habitual_stop:
            return False
        return any(k in self.habitual_stop for k in cell_neighbours(cell_key(lat, lon, STOP_CELL_DEG), STOP_CELL_DEG))

    def _learned_loiter(self, lat, lon) -> tuple[int, int] | None:
        if not self.habitual_loiter:
            return None
        best = None
        for k in cell_neighbours(cell_key(lat, lon, LOITER_CELL_DEG), LOITER_CELL_DEG):
            hit = self.habitual_loiter.get(k)
            if hit and (best is None or hit[1] > best[1]):
                best = hit
        return best

    def offshore_text(self, lat, lon) -> str | None:
        """"40 km offshore" from the coast, or "35 km from Primorsk" from the nearest listed port."""
        if self.land is not None and self.land.available:
            if self.land.on_land(lat, lon):
                return "close inshore"
            d = self.land.coast_km(lat, lon, 100.0)
            return f"{km_text(d)} offshore" if d is not None else "more than 100 km offshore"
        hit = self.nearest_port(lat, lon)
        return f"{km_text(hit[1])} from {hit[0]['name']}" if hit else None

    def dest_port(self, dest: str, lat: float, lon: float) -> tuple[str, float] | None:
        """The port the vessel says it is heading for, when it is within 50 km and no more than 25 km further than
        the nearest listed port (waiting off it for a berth, not loitering somewhere else on the way)."""
        norm = _norm(dest)
        for junk in ("FORORDERS", "FORORDER", "ORDERS", "ORDER"):
            norm = norm.replace(junk, "")
        if len(norm) < 4:
            return None
        cands = []
        grid = self.ports()
        if grid is not None:
            cands.extend(grid.near(lat, lon, LOITER_DEST_KM))
        for p in self._builtin_ports:
            d = geo.haversine_km(lat, lon, p["lat"], p["lon"])
            if d <= LOITER_DEST_KM:
                cands.append((d, p))
        cands.sort(key=lambda x: x[0])
        nearest = cands[0][0] if cands else None
        for d, p in cands:
            if d > nearest + LOITER_DEST_SLACK_KM:
                break
            pid = str(p.get("id") or "")
            code = pid[7:].upper() if pid.startswith("locode:") else None
            if code and len(code) == 5 and code in norm:
                return p["name"], d
            name = _norm(p.get("name") or "")
            if len(name) >= 5 and (name in norm or (len(norm) >= 6 and norm in name)):
                return p["name"], d
        return None

    # ==========================================================================================
    # Ship-to-ship transfer candidates
    # ==========================================================================================
    def _sts_candidates(self, scene, now: float) -> list:
        out = []
        for r in scene["snap"].rows:
            if r[5] & (F_CLASS_B | F_SUSPECT) or now - r[R_T] > STS_FRESH_S:
                continue
            if r[R_SOG] is None or r[R_SOG] > STS_MAX_KN * 10:
                continue
            cls = CLASS_IDS[r[R_CLS]]
            length = r[R_LEN]
            if cls not in ("tanker", "cargo", "other"):
                continue
            if length is not None and length < STS_MIN_LEN:
                continue
            if cls == "other" and not (length and length >= STS_OTHER_MIN_LEN):
                continue
            out.append(r)
        return out

    def place_sts(self, lat, lon, a: dict, b: dict, scene) -> tuple[str, str | None]:
        """("open", None) or (reason, text) for a pair's midpoint. Reasons: port, pilot, offshore, anchorage,
        coast."""
        port = self.port_near(lat, lon)
        if port:
            return "port", f"in or near the port of {port[0]['name']}"
        if self._pilot_near(lat, lon, scene):
            return "pilot", "at a pilot boarding area (a pilot vessel is within 3 km)"
        now = self.now()
        for v in (a, b):
            if v.get("state") == "stationary" and v.get("state_since") and now - v["state_since"] >= FIXED_S:
                return "offshore", "one of them has not moved for a week (floating storage or an offshore unit)"
        if self._fixed_near(lat, lon, scene, FIXED_KM, exclude=(a["mmsi"], b["mmsi"])):
            return "offshore", "next to a vessel that has not moved for a week (an offshore installation)"
        area = usual_area(lat, lon)
        if area:
            return "anchorage", f"in {area['phrase']}"
        if self._learned_stop(lat, lon):
            return "anchorage", "in a usual anchorage Meridian has learned"
        crowd = self._crowd(lat, lon, scene, exclude=(a["mmsi"], b["mmsi"]))
        if crowd >= CROWD_MIN:
            return "anchorage", f"in an anchorage ({crowd} other stopped vessels within 3 km)"
        if self.land is not None and self.land.available:
            if self.land.on_land(lat, lon):
                return "coast", "inshore or alongside a quay"
            d = self.land.coast_km(lat, lon, COAST_KM)
            if d is not None:
                return "coast", "close inshore"
        return "open", None

    def _run_sts(self, scene, now: float) -> None:
        self.last["sts"] = now
        cands = self._sts_candidates(scene, now)
        fine: dict[tuple[int, int], list] = {}
        for r in cands:
            fine.setdefault((int(math.floor(r[R_LAT] * 100)), int(math.floor(r[R_LON] * 100))), []).append(r)
        raw = []
        for r in cands:
            ky, kx = int(math.floor(r[R_LAT] * 100)), int(math.floor(r[R_LON] * 100))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for o in fine.get((ky + dy, kx + dx), ()):
                        if o[R_MMSI] <= r[R_MMSI]:
                            continue
                        d = geo.haversine_km(r[R_LAT], r[R_LON], o[R_LAT], o[R_LON])
                        if d <= STS_RANGE_KM:
                            raw.append((r, o, d))
        infos = self._infos({p[0][R_MMSI] for p in raw} | {p[1][R_MMSI] for p in raw}, now)
        excluded = Counter()
        seen = set()
        for r, o, d in raw:
            a, b = infos.get(r[R_MMSI]), infos.get(o[R_MMSI])
            if a is None or b is None:
                continue
            if not (a["cls"] == "tanker" or b["cls"] == "tanker" or a["sanc_strong"] or b["sanc_strong"]):
                excluded["no tanker"] += 1
                continue
            if now - (a["suspect_t"] or 0) <= SUSPECT_CLEAR_S or now - (b["suspect_t"] or 0) <= SUSPECT_CLEAR_S:
                excluded["suspect positions"] += 1
                continue
            lat, lon = _mean_latlon([(r[R_LAT], r[R_LON]), (o[R_LAT], o[R_LON])])
            reason, rtext = self.place_sts(lat, lon, a, b, scene)
            usual = None
            if reason == "anchorage" and (a["sanc_strong"] or b["sanc_strong"]):
                reason = "open"                 # a usual anchorage does not clear a sanctioned vessel
            if reason != "open":
                excluded[reason] += 1
                if self.aoi.at(lat, lon) is None:
                    continue
                # Inside an area of interest an excluded pair is kept as an unflagged item with its reason,
                # so "nothing flagged" there is never mistaken for "nothing seen".
                usual = rtext
            key = (r[R_MMSI], o[R_MMSI])
            seen.add(key)
            t_obs = min(r[R_T], o[R_T])
            ep = self.sts.get(key)
            if ep is not None and ep["state"] == "closed" and t_obs - ep["t1"] > STS_TOLERANCE_S:
                ep = None
            if ep is None:
                ep = {"id": f"sts:{key[0]}-{key[1]}:{int(t_obs)}", "a": key[0], "b": key[1], "t0": t_obs, "t1": t_obs,
                      "lat": lat, "lon": lon, "n": 0, "min_m": d * 1000, "sum_m": 0.0, "last_m": d * 1000,
                      "quiet": {}, "seen_t": {}, "d0": {}, "d1": {}, "dmax": {}, "state": "open", "closed_t": None,
                      "sog_max": 0.0, "pos": {}, "saved_t": 0.0, "src": r[R_SRC]}
                self.sts[key] = ep
            ep["state"], ep["closed_t"] = "open", None
            ep["usual"] = usual
            ep["t1"] = max(ep["t1"], t_obs)
            ep["n"] += 1
            ep["sum_m"] += d * 1000
            ep["min_m"] = min(ep["min_m"], d * 1000)
            ep["last_m"] = d * 1000
            ep["lat"] += (lat - ep["lat"]) / ep["n"]
            ep["lon"] += (lon - ep["lon"]) / ep["n"]
            ep["sog_max"] = max(ep["sog_max"], (r[R_SOG] or 0) / 10.0, (o[R_SOG] or 0) / 10.0)
            for row, info in ((r, a), (o, b)):
                k = str(row[R_MMSI])
                prev = ep.setdefault("seen_t", {}).get(k)
                gap = row[R_T] - prev if prev is not None else 0.0      # silence between two of its reports
                ep["quiet"][k] = max(ep["quiet"].get(k, 0.0), gap, now - row[R_T])
                ep["seen_t"][k] = row[R_T]
                ep["pos"][k] = [round(row[R_LAT], 5), round(row[R_LON], 5)]
                if ep["d0"].get(k) is None and info["draught"] is not None:
                    ep["d0"][k] = info["draught"]
        for key, ep in self.sts.items():
            if ep["state"] != "open" or key in seen:
                continue
            ra, rb = scene["rows"].get(key[0]), scene["rows"].get(key[1])
            separated = (ra is not None and rb is not None and now - ra[R_T] <= 600 and now - rb[R_T] <= 600
                         and geo.haversine_km(ra[R_LAT], ra[R_LON], rb[R_LAT], rb[R_LON]) > STS_SEPARATED_KM)
            if separated or now - ep["t1"] > STS_TOLERANCE_S:
                ep["state"], ep["closed_t"] = "closed", now
                ep["saved_t"] = 0.0
        # draught after the episode (cargo moved?), for a day after it ends
        watch = [ep for ep in self.sts.values() if ep["t1"] - ep["t0"] >= STS_MIN_S
                 and (ep["state"] == "open" or now - (ep["closed_t"] or now) <= DRAUGHT_WATCH_S)]
        infos = self._infos({m for ep in watch for m in (ep["a"], ep["b"])}, now)
        for ep in watch:
            for m in (ep["a"], ep["b"]):
                k = str(m)
                cur = (infos.get(m) or {}).get("draught")
                if cur is None:
                    continue
                if ep["d0"].get(k) is None:
                    ep["d0"][k] = cur
                before = (ep["d1"].get(k), ep["dmax"].get(k))
                ep["d1"][k] = cur
                ep["dmax"][k] = max(ep["dmax"].get(k, 0.0), round(abs(cur - ep["d0"][k]), 1))
                if ep["state"] == "closed" and before != (ep["d1"][k], ep["dmax"][k]):
                    ep["saved_t"] = 0.0             # save the new evidence
        for key in [k for k, ep in self.sts.items() if ep["state"] == "closed" and now - ep["t1"] > RECENT_S
                    or ep["state"] == "closed" and ep["t1"] - ep["t0"] < STS_MIN_S]:
            del self.sts[key]
        self.excluded["sts"] = excluded

    def _sts_item(self, ep: dict, infos: dict, now: float) -> dict | None:
        dur = ep["t1"] - ep["t0"]
        if dur < STS_MIN_S:
            return None
        a, b = infos.get(ep["a"]), infos.get(ep["b"])
        if a is None or b is None:
            return None
        # primary: the tanker, or the larger vessel
        def rank(v):
            return (1 if v["cls"] == "tanker" else 0, v["length"] or 0, -v["mmsi"])
        primary, other = (a, b) if rank(a) >= rank(b) else (b, a)
        small = a if (a["length"] or 999) <= (b["length"] or 999) else b
        lat, lon = ep["lat"], ep["lon"]
        aoi = self.aoi.at(lat, lon)
        small_tanker = small["cls"] == "tanker" and small["length"] is not None and small["length"] < STS_BUNKER_LEN
        # The size hint alone never clears a sanctioned vessel or a pair in an area of interest.
        bunkering = small_tanker and not aoi and not (a["sanc_strong"] or b["sanc_strong"])
        usual = ep.get("usual")
        offshore = self.offshore_text(lat, lon)
        min_km = ep["min_m"] / 1000.0
        alongside = min_km <= STS_ALONGSIDE_KM
        quiet = {int(k): v for k, v in ep["quiet"].items() if v >= STS_QUIET_S}
        quiet_long = any(v >= STS_QUIET_SEV_S for v in quiet.values())
        gap_overlap = [g for g in self.gaps.values() if g["mmsi"] in (ep["a"], ep["b"])
                       and g["t0"] <= ep["t1"] + 7200 and g["t1"] >= ep["t0"] - 7200]
        dchange = {int(k): v for k, v in ep["dmax"].items() if v >= DRAUGHT_M}
        sanctioned = [v for v in (a, b) if v["sanc_strong"]]
        severity = 4 if dur >= STS_LONG_S else 3
        severity += 1 if aoi else 0
        severity += 1 if sanctioned else 0
        severity += 1 if dchange else 0
        severity += 1 if (quiet_long or gap_overlap) else 0
        severity = min(7, severity)
        state = "usual" if usual else "possible_bunkering" if bunkering else "candidate"
        if bunkering or usual:
            severity = 2
        if dur >= STS_LONG_S and alongside and ep["n"] >= 20:
            confidence = "high"
            conf_text = f"They stayed within {km_text(STS_RANGE_KM)} for {duration_text(dur)}, mostly {km_text(min_km)} or closer apart."
        elif dur >= 3600 or alongside:
            confidence = "medium"
            conf_text = f"They stayed within {km_text(STS_RANGE_KM)} for {duration_text(dur)}; the closest reports were {km_text(min_km)} apart."
        else:
            confidence = "low"
            conf_text = f"A short episode: within {km_text(STS_RANGE_KM)} for {duration_text(dur)}, closest {km_text(min_km)}."
        pa, pb = _type_word(primary), _type_word(other)
        names = f"{pa} {primary['name'] or 'MMSI ' + str(primary['mmsi'])} and {pb} {other['name'] or 'MMSI ' + str(other['mmsi'])}"
        where = offshore or "at sea"
        subtitle = f"{names[0].upper()}{names[1:]}, stopped {km_text(min_km)} apart for {duration_text(dur)}, {where}"
        if aoi:
            subtitle += f", {aoi_phrase(aoi)}"
        ongoing = ep["state"] == "open"
        observed = (f"Both vessels reported under {STS_MAX_KN:g} kn within {km_text(STS_RANGE_KM)} of each other from "
                    f"{utc_text(ep['t0'], True)} to {utc_text(ep['t1'], True)} ({duration_text(dur)}"
                    f"{', still together' if ongoing else ''}). The closest reports were {km_text(min_km)} apart"
                    f"{', which reads as probably alongside' if alongside else ''}. This is consistent with a "
                    f"ship-to-ship transfer.")
        evidence = [["Duration", duration_text(dur) + (" so far" if ongoing else "")],
                    ["Closest", km_text(min_km)], ["Average distance", km_text(ep["sum_m"] / max(1, ep["n"]) / 1000.0)],
                    ["Fastest reported speed", f"{ep['sog_max']:.1f} kn"],
                    ["Place", f"{usual[0].upper()}{usual[1:]}, {where}" if usual else f"Open water, {where}"]]
        hit = self.nearest_port(lat, lon)
        if hit:
            evidence.append(["Nearest listed port", f"{hit[0]['name']}, {km_text(hit[1])}"])
        if aoi:
            evidence.append(["Area of interest", aoi["name"]])
        for v in (a, b):
            d0, d1 = ep["d0"].get(str(v["mmsi"])), ep["d1"].get(str(v["mmsi"]))
            if d0 is not None and d1 is not None and abs(d1 - d0) >= 0.1:
                evidence.append([f"Draught, {v['name'] or v['mmsi']}", f"{d0:.1f} m to {d1:.1f} m"])
        for m, q in quiet.items():
            v = a if a["mmsi"] == m else b
            evidence.append(["Went quiet", f"{v['name'] or m} was not heard for up to {duration_text(q)} during the episode"])
        if gap_overlap:
            evidence.append(["AIS gap", f"{len(gap_overlap)} gap episode{'s' if len(gap_overlap) != 1 else ''} around the same time"])
        for v in sanctioned:
            evidence.append(["Sanctions", f"{v['name'] or v['mmsi']} is on the {lists_text(v['sanc']['lists'])} "
                                          f"(matched by {str(v['sanc']['match']).upper()})"])
        innocent = ["Ship-to-ship transfers are common and usually lawful: bunkering (refuelling), lightering a large "
                    "tanker so it can enter a shallow port, and supply runs.",
                    "Ships also wait near each other for orders, a berth or better weather.",
                    "AIS only shows that their positions are consistent with a transfer; it cannot show cargo moving."]
        if small_tanker:
            innocent.insert(0, f"The smaller vessel is a tanker under {STS_BUNKER_LEN:g} m, the size of many bunker "
                               "barges: it may be bunkering (refuelling)."
                            + ("" if bunkering else " That alone does not clear it here."))
        if usual:
            innocent.insert(0, f"They lie {usual}, where ships routinely anchor, wait or transfer cargo: not flagged, "
                               "but listed because it is inside an area of interest.")
        title = "Possible bunkering" if bunkering and not usual else "Possible ship-to-ship transfer"
        return self._item(
            id=ep["id"], kind="sts", title=title, subtitle=subtitle, lat=lat, lon=lon, first_time=ep["t0"],
            time=ep["t1"], ongoing=ongoing, severity=severity, confidence=confidence, confidence_text=conf_text,
            observed=observed, evidence=evidence, innocent=innocent,
            coverage=self._coverage_text(scene_snap=None, lat=lat, lon=lon, now=now, src=ep.get("src")),
            limits=("AIS antenna positions can put ships lying alongside 50 to 200 m apart. Meridian checks positions "
                    "once a minute and only sees vessels that transmit AIS and are heard by terrestrial receivers, so "
                    "a vessel that turned its AIS off before coming alongside is invisible."),
            vessels=[_brief(primary, "tanker or larger vessel" if primary["cls"] == "tanker" else "larger vessel"),
                     _brief(other, "other vessel")],
            state=state, published=state == "candidate", event_kind="ship", event_id=str(primary["mmsi"]),
            aoi=aoi, geometry={"type": "LineString", "coordinates": [
                [ep["pos"].get(str(primary["mmsi"]), [lat, lon])[1], ep["pos"].get(str(primary["mmsi"]), [lat, lon])[0]],
                [ep["pos"].get(str(other["mmsi"]), [lat, lon])[1], ep["pos"].get(str(other["mmsi"]), [lat, lon])[0]]]},
            block=[["Closest", km_text(min_km)], ["Together", duration_text(dur)], ["Offshore", where.split(" offshore")[0] if "offshore" in where else "–"]],
            extra={"min_m": round(ep["min_m"]), "duration_s": int(dur)})

    # ==========================================================================================
    # Loitering
    # ==========================================================================================
    def _run_loiter(self, scene, now: float) -> None:
        self.last["loitering"] = now
        cands = []
        with self.store.lock:
            for v in self.store.vessels.values():
                if v.lat is None or now - v.t > 3600 or v.ais == "B":
                    continue
                if v.cls not in ("tanker", "cargo", "other"):
                    continue
                if v.length is not None and v.length < STS_MIN_LEN:
                    continue
                if v.cls == "other" and not (v.length and v.length >= STS_MIN_LEN):
                    continue
                if v.sog is None or v.sog > LOITER_MAX_KN or v.suspect(now):
                    continue
                if v.state == "stationary" and v.stop_lat is not None and v.state_since \
                        and now - v.state_since >= LOITER_MIN_S \
                        and geo.haversine_km(v.stop_lat, v.stop_lon, v.lat, v.lon) < 1.0:
                    continue                    # at anchor, not drifting
                cands.append((v.mmsi, v.lat, v.lon))
        excluded = Counter()
        seen = set()
        day = int(now // 86400)
        learn_rows = []
        for mmsi, lat, lon in cands:
            if self.port_near(lat, lon):
                continue
            m = loiter_metrics(self.store.trail(mmsi, now - LOITER_WINDOW_S))
            if m is None:
                continue
            if self.port_near(m["lat"], m["lon"]):
                excluded["port"] += 1
                continue
            learn_rows.append((cell_key(m["lat"], m["lon"], LOITER_CELL_DEG), day, mmsi))
            if self._pilot_near(m["lat"], m["lon"], scene):
                excluded["pilot"] += 1
                continue
            seen.add(mmsi)
            ep = self.loiter.get(mmsi)
            if ep is not None and (ep["state"] == "closed" and m["t0"] > ep["t1"] + 2 * LOITER_EVERY_S):
                ep = None
            if ep is None:
                ep = {"id": f"loiter:{mmsi}:{int(m['t0'])}", "mmsi": mmsi, "t0": m["t0"], "saved_t": 0.0}
                self.loiter[mmsi] = ep
            ep.update({k: m[k] for k in ("t1", "lat", "lon", "rg_km", "max_km", "path_km", "net_km", "mean_kn",
                                         "points", "sog_lo", "sog_hi", "track")})
            ep["t0"] = min(ep["t0"], m["t0"])
            ep["state"], ep["seen"], ep["miss"] = "open", now, 0
        for mmsi, ep in list(self.loiter.items()):
            if ep["state"] == "open" and mmsi not in seen:
                ep["miss"] = ep.get("miss", 0) + 1
                if ep["miss"] >= 2:
                    ep["state"] = "closed"
                    ep["saved_t"] = 0.0
            if ep["state"] == "closed" and now - ep["t1"] > RECENT_S:
                del self.loiter[mmsi]
        if learn_rows and self.db is not None:
            self.db.executemany("INSERT OR IGNORE INTO ships_loitercells(cell, day, mmsi) VALUES(?,?,?)", learn_rows)
        self._count_day("loitering", now)
        self.excluded["loitering"] = excluded

    def _loiter_item(self, ep: dict, infos: dict, now: float) -> dict | None:
        v = infos.get(ep["mmsi"])
        if v is None:
            return None
        if v["cls"] == "military":
            return None
        lat, lon = ep["lat"], ep["lon"]
        span = ep["t1"] - ep["t0"]
        state, usual = "candidate", None
        area = usual_area(lat, lon)
        hl = self._learned_loiter(lat, lon)
        dest = self.dest_port(v["dest"], lat, lon) if v["dest"] else None
        fixed = self._fixed_near(lat, lon, {"fixed": self._fixed_grid}, LOITER_FIXED_KM, exclude=(ep["mmsi"],)) \
            if self._fixed_grid is not None else None
        aoi = self.aoi.at(lat, lon)
        dest_text = (f"It reports {dest[0]} as its destination, {km_text(dest[1])} away: ships often drift like this "
                     "while they wait for a berth.") if dest else None
        if area:
            state, usual = "usual", (f"It is in {area['phrase']}, where ships routinely "
                                     f"{'transfer cargo and bunkers' if area['kind'] == 'transfer' else 'wait'}.")
        elif self._learned_stop(lat, lon):
            state, usual = "usual", "It is in a usual anchorage Meridian has learned from two weeks of stops."
        elif hl:
            state, usual = "usual", (f"Vessels loiter here often: Meridian saw {hl[0]} vessels do this on {hl[1]} of the "
                                     f"last {LEARN_DAYS} days.")
        elif fixed:
            state, usual = "usual", "It is near a vessel that has not moved for a week (an offshore installation or "\
                                    "floating storage), where supply and standby work is routine."
        elif dest and not aoi and not v["sanc_strong"]:
            # The destination is free text the crew sets: it clears a vessel only outside areas of interest and
            # without a sanctions match. Otherwise it stays a candidate and the destination is a possible reason.
            state, usual = "usual", dest_text
        learned = self.learned_days("loitering", LOITER_DAY_RUNS)
        if state == "candidate" and learned < LOITER_LEARN_DAYS:
            state = "learning"
        severity = 3 + (1 if aoi else 0) + (1 if v["sanc_strong"] else 0)
        severity = min(5, severity) if state == "candidate" else 2
        if span >= 12 * 3600 and ep["points"] >= 20:
            confidence = "high"
        elif span >= 8 * 3600:
            confidence = "medium"
        else:
            confidence = "low"
        within = km_text(max(ep["max_km"], 0.5))
        speeds = _speed_range(ep["sog_lo"], ep["sog_hi"])
        where = self.offshore_text(lat, lon)
        hit = self.nearest_port(lat, lon)
        place = f"{km_text(hit[1])} from {hit[0]['name']}" if hit else where
        name = v["name"] or f"MMSI {v['mmsi']}"
        subtitle = f"{_type_word(v).capitalize()}, within {within} for {duration_text(span)} at {speeds}"
        if place:
            subtitle += f", {place}"
        observed = (f"{name} has stayed within {within} for {duration_text(span)} at {speeds}, covering "
                    f"{km_text(ep['path_km'])} in all but ending {km_text(ep['net_km'])} from where it started "
                    f"({ep['points']} track points from {utc_text(ep['t0'], True)} to {utc_text(ep['t1'], True)}). "
                    "That is drifting or circling, not anchoring or passing through.")
        evidence = [["Time loitering", duration_text(span) + (" so far" if ep["state"] == "open" else "")],
                    ["Area", f"within {within} (radius of gyration {km_text(ep['rg_km'])})"],
                    ["Speed", f"{speeds}, {ep['mean_kn']:.1f} kn on average"],
                    ["Track", f"{km_text(ep['path_km'])} sailed, {km_text(ep['net_km'])} net"],
                    ["Place", where or "at sea"]]
        if hit:
            evidence.append(["Nearest listed port", f"{hit[0]['name']}, {km_text(hit[1])}"])
        if v["dest"]:
            evidence.append(["Reported destination", v["dest"]])
        if aoi:
            evidence.append(["Area of interest", aoi["name"]])
        if v["sanc_strong"]:
            evidence.append(["Sanctions", f"On the {lists_text(v['sanc']['lists'])} (matched by {str(v['sanc']['match']).upper()})"])
        innocent = ["Ships often drift or circle for hours while they wait for a berth, orders, a pilot, a cargo "
                    "window or better weather; some wait outside territorial waters to save port dues.",
                    "Survey, cable and research work also keeps ships moving slowly in one area."]
        if usual:
            innocent.insert(0, usual)
        elif dest_text:
            innocent.insert(0, f"It reports {dest[0]} as its destination, {km_text(dest[1])} away: it may be waiting "
                               "for a berth. The destination is set by the crew and not checked, so it does not clear "
                               "the vessel here.")
        learn_note = building_text(learned, LOITER_LEARN_DAYS, "days").replace("Building baseline", "Learning usual waiting areas")
        conf_text = {"high": f"A long, well-tracked episode ({ep['points']} points over {duration_text(span)}).",
                     "medium": f"{ep['points']} track points over {duration_text(span)}.",
                     "low": f"Only just loiter-shaped: {ep['points']} points over {duration_text(span)}."}[confidence]
        if state == "learning":
            conf_text += f" {learn_note}: candidates are shown but not flagged yet."
        return self._item(
            id=ep["id"], kind="loitering", title=f"Loitering vessel: {name}", subtitle=subtitle, lat=lat, lon=lon,
            first_time=ep["t0"], time=ep["t1"], ongoing=ep["state"] == "open", severity=severity,
            confidence=confidence, confidence_text=conf_text, observed=observed, evidence=evidence, innocent=innocent,
            coverage=self._coverage_text(None, lat, lon, now, v.get("src")),
            limits=("Built from Meridian's thinned track (a point every 10 minutes under way, fewer when slow), so "
                    "short loops are smoothed out; terrestrial AIS only."),
            vessels=[_brief(v, "vessel")], state=state, published=state == "candidate", event_kind="ship",
            event_id=str(v["mmsi"]), aoi=aoi,
            geometry={"type": "LineString", "coordinates": ep.get("track") or [[lon, lat]]},
            block=[["Within", within], ["For", duration_text(span)], ["Speed", speeds]],
            extra={"radius_km": round(max(ep["max_km"], 0.5), 2), "usual": usual, "learning": learn_note})

    # ==========================================================================================
    # AIS gaps (moving vessels flagged dark, and stopped vessels gone quiet in areas of interest)
    # ==========================================================================================
    def _run_quiet(self, scene, now: float, receiving: dict) -> None:
        cands = []
        with self.store.lock:
            for v in self.store.vessels.values():
                if v.lat is None or not v.t:
                    continue
                silent = now - v.t
                if v.quiet_since is not None:
                    if silent > QUIET_MAX_S:
                        v.quiet_since, v.gap_ctx = None, None
                        self.quiet.pop(v.mmsi, None)
                    continue
                if v.dark_since is not None or silent <= QUIET_AFTER_S or silent > QUIET_MAX_S:
                    continue
                if v.ais == "B" or v.cls not in ("tanker", "cargo", "other"):
                    continue
                if v.cls == "other" and not v.imo:
                    continue
                if v.length is not None and v.length < STS_MIN_LEN:
                    continue
                if v.sog is None or v.sog > 1.0 or v.regular() < 6:
                    continue
                if v.t - v.suspect_t <= SUSPECT_CLEAR_S or (v.reject_t and v.reject_t > v.t):
                    continue
                since, last_ok = receiving.get(v.src, (None, 0.0))
                if since is None or since > v.t - 600 or now - last_ok > 900:
                    continue
                cands.append((v.mmsi, v.lat, v.lon, v.t))
        snap = scene["snap"]
        excluded = Counter()
        for mmsi, lat, lon, t in cands:
            aoi = self.aoi.at(lat, lon)
            if aoi is None:
                excluded["stopped and silent outside areas of interest"] += 1
                continue
            if self.port_near(lat, lon) or usual_area(lat, lon) or self._learned_stop(lat, lon):
                excluded["stopped and silent in a port or anchorage"] += 1
                continue
            n = sum(1 for _d, row in snap.grid.near(lat, lon, GAP_COVER_KM)
                    if row[R_MMSI] != mmsi and now - row[R_T] <= 20 * 60)
            if n < 3:
                excluded["no coverage proof"] += 1
                continue
            with self.store.lock:
                v = self.store.vessels.get(mmsi)
                if v is None or v.t != t or v.quiet_since is not None or v.dark_since is not None:
                    continue
                v.quiet_since = t
                v.gap_ctx = {"n_last": n, "flag_t": now}
            self.quiet[mmsi] = {"mmsi": mmsi, "t0": t, "lat": lat, "lon": lon, "aoi": aoi, "n_last": n, "flag_t": now}
        with self.store.lock:
            for mmsi in [m for m in self.quiet if (self.store.vessels.get(m) is None
                                                    or self.store.vessels[m].quiet_since is None)]:
                del self.quiet[mmsi]
        self.excluded["ais_gap"] = excluded

    def _run_gaps(self, now: float) -> None:
        self.last["ais_gap"] = now
        with self.store.lock:
            new = [dict(g) for g in self.store.recent_gaps
                   if f"gap:{g['mmsi']}:{int(g['t0'])}" not in self.gaps and g["t1"] >= now - RECENT_S]
        for g in new:
            ep = self._gap_episode(g)
            self.gaps[ep["id"]] = ep
        settling = [ep for ep in self.gaps.values() if not ep.get("settled")]
        infos = self._infos({ep["mmsi"] for ep in settling}, now)
        for ep in settling:
            v = infos.get(ep["mmsi"])
            if v is not None:
                ep["draught1"], ep["dest1"], ep["name1"], ep["imo1"] = v["draught"], v["dest"], v["name"], v["imo"]
            if now - ep["t1"] >= GAP_SETTLE_S:
                ep["settled"] = True
                ep["saved_t"] = 0.0
        for gid in [k for k, ep in self.gaps.items() if now - ep["t1"] > RECENT_S]:
            del self.gaps[gid]

    def _gap_episode(self, g: dict) -> dict:
        t0, t1 = float(g["t0"]), float(g["t1"])
        hours = max(0.0, (t1 - t0) / 3600.0)
        dist = geo.haversine_km(g["lat0"], g["lon0"], g["lat1"], g["lon1"])
        ratio, reading = gap_reading(dist, hours, g.get("sog0"), bool(g.get("stopped")))
        ep = {"id": f"gap:{g['mmsi']}:{int(t0)}", "mmsi": int(g["mmsi"]), "t0": t0, "t1": t1,
              "lat0": g["lat0"], "lon0": g["lon0"], "lat1": g["lat1"], "lon1": g["lon1"], "hours": hours,
              "dist_km": dist, "implied_kn": dist / geo.KM_PER_NM / hours if hours > 0 else None, "ratio": ratio,
              "reading": reading, "aoi": self.aoi.on_path(g["lat0"], g["lon0"], g["lat1"], g["lon1"]),
              "ctx": {k: g.get(k) for k in ("sog0", "cog0", "nav0", "draught0", "dest0", "name0", "imo0", "cls", "src0",
                                            "stopped", "n_last", "n_est", "flag_t")},
              "coverage": self._gap_coverage(g), "near_tanker": self._stopped_tanker_near(g),
              "place0": None, "settled": False, "saved_t": 0.0}
        port = self.port_near(g["lat0"], g["lon0"])
        if port:
            ep["place0"] = f"in or near the port of {port[0]['name']}"
        else:
            area = usual_area(g["lat0"], g["lon0"])
            if area:
                ep["place0"] = f"in {area['phrase']}"
        return ep

    def _gap_coverage(self, g: dict) -> dict | None:
        """Other vessels' track points within 30 km of where the vessel should have been, in up to 8 half-hour
        slices spread across the gap. Its expected position in each slice is on the straight line between
        where it went silent and where it reappeared, at the same fraction of the gap; coverage only near
        the start says nothing about the waters it sailed through."""
        if self.db is None:
            return None
        t0, t1 = float(g["t0"]), float(g["t1"])
        if t1 - t0 < 1800:
            return None
        lat1 = g.get("lat1") if g.get("lat1") is not None else g["lat0"]
        lon1 = g.get("lon1") if g.get("lon1") is not None else g["lon0"]
        dlon = (lon1 - g["lon0"] + 540.0) % 360.0 - 180.0
        slices = max(1, min(8, int((t1 - t0) // 1800)))
        covered = 0
        vessels: set[int] = set()
        for i in range(slices):
            frac = (i + 0.5) / slices
            mid = t0 + frac * (t1 - t0)
            lat = g["lat0"] + frac * (lat1 - g["lat0"])
            lon = (g["lon0"] + frac * dlon + 540.0) % 360.0 - 180.0
            w, s, e, n = geo.bbox_of_circle(lat, lon, GAP_COVER_KM)
            lon_sql, lon_params = ("AND lon BETWEEN ? AND ?", [int(w * 1e5), int(e * 1e5)]) if w <= e else ("", [])
            rows = self.db.query(f"SELECT DISTINCT mmsi FROM ships_track WHERE t >= ? AND t <= ? AND lat BETWEEN ? AND ? "
                                 f"{lon_sql} AND mmsi != ? LIMIT 300",
                                 [int(mid - 900), int(mid + 900), int(s * 1e5), int(n * 1e5)] + lon_params + [int(g["mmsi"])])
            if rows:
                covered += 1
                vessels.update(int(r["mmsi"]) for r in rows)
        return {"checks": slices, "covered": covered, "vessels": len(vessels)}

    def _stopped_tanker_near(self, g: dict) -> dict | None:
        """A stopped tanker within 1 km of where the vessel went silent ("possible dark transfer")."""
        if self.db is None:
            return None
        t0 = float(g["t0"])
        w, s, e, n = geo.bbox_of_circle(g["lat0"], g["lon0"], GAP_NEAR_TANKER_KM)
        rows = self.db.query("SELECT mmsi, lat, lon, sog FROM ships_track WHERE t >= ? AND t <= ? AND lat BETWEEN ? AND ? "
                             "AND lon BETWEEN ? AND ? AND mmsi != ? AND (sog IS NULL OR sog <= 10) LIMIT 50",
                             (int(t0 - 3600), int(t0 + 900), int(s * 1e5), int(n * 1e5), int(w * 1e5), int(e * 1e5),
                              int(g["mmsi"])))
        best = None
        for r in rows:
            d = geo.haversine_km(g["lat0"], g["lon0"], r["lat"] / 1e5, r["lon"] / 1e5)
            if d <= GAP_NEAR_TANKER_KM and (best is None or d < best[0]):
                best = (d, int(r["mmsi"]))
        if best is None:
            return None
        info = self._infos([best[1]], self.now()).get(best[1])
        if info is None or info["cls"] != "tanker":
            return None
        return {"mmsi": str(best[1]), "name": info["name"] or f"MMSI {best[1]}", "km": round(best[0], 2)}

    def _gap_item(self, ep: dict, infos: dict, now: float) -> dict | None:
        v = infos.get(ep["mmsi"])
        ctx = ep.get("ctx") or {}
        if v is None:
            v = {"mmsi": ep["mmsi"], "name": ctx.get("name0") or "", "cls": ctx.get("cls") or "other", "type": None,
                 "length": None, "imo": ctx.get("imo0"), "draught": None, "dest": "", "sanc": None, "sanc_strong": False,
                 "lat": ep["lat1"], "lon": ep["lon1"], "src": ctx.get("src0")}
        aoi = ep.get("aoi")
        hours = ep["hours"]
        stopped = bool(ctx.get("stopped")) or (ep.get("ratio") is not None and ep["ratio"] < 0.3)
        d0, d1 = ctx.get("draught0"), ep.get("draught1")
        dchange = round(d1 - d0, 1) if d0 is not None and d1 is not None else None
        changes = []
        if dchange is not None and abs(dchange) >= DRAUGHT_M:
            changes.append(f"draught {abs(dchange):.1f} m {'deeper' if dchange > 0 else 'shallower'}")
        if ep.get("dest1") is not None and ctx.get("dest0") and ep["dest1"] and _norm(ep["dest1"]) != _norm(ctx["dest0"]):
            changes.append(f"destination now {ep['dest1']} (was {ctx['dest0']})")
        if ep.get("name1") and ctx.get("name0") and _norm(ep["name1"]) != _norm(ctx["name0"]):
            changes.append(f"name now {ep['name1']} (was {ctx['name0']})")
        if ep.get("imo1") and ctx.get("imo0") and ep["imo1"] != ctx["imo0"]:
            changes.append(f"IMO now {ep['imo1']} (was {ctx['imo0']})")
        cov = ep.get("coverage")
        severity = 3 + (1 if aoi else 0) + (1 if stopped else 0) \
            + (1 if dchange is not None and abs(dchange) >= DRAUGHT_M else 0) + (1 if v.get("sanc_strong") else 0)
        severity = min(6, severity)
        n_last = ctx.get("n_last")
        if cov and cov["checks"] and cov["covered"] / cov["checks"] >= 0.8 and (n_last or 0) >= 5:
            confidence = "high"
        elif cov is None or cov["covered"] / max(1, cov["checks"]) >= 0.5:
            confidence = "medium"
        else:
            confidence = "low"
        # Reappearing where its speed puts it means it sailed on unheard: that is what a hole between receivers
        # looks like, unless coverage along its path was proven across the gap.
        hole = ep["reading"] == "sailed on through a coverage hole" and             not (cov and cov["checks"] and cov["covered"] / cov["checks"] >= 0.8)
        if hole:
            confidence = "low"
            severity = min(severity, 2)
        name = v["name"] or ctx.get("name0") or f"MMSI {ep['mmsi']}"
        tw = _type_word(v)
        title = f"AIS gap {aoi_phrase(aoi)}" if aoi else "AIS gap"
        moved = km_text(ep["dist_km"])
        subtitle = f"{tw.capitalize()} {name} silent for {duration_text(ep['t1'] - ep['t0'])}; reappeared {moved} away"
        if changes:
            subtitle += f", {changes[0]}"
        observed = (f"The {tw} went silent at {utc_text(ep['t0'], True)}"
                    f"{f' while {n_last} vessels nearby kept reporting' if n_last else ''}, and reappeared at "
                    f"{utc_text(ep['t1'], True)} {moved} away")
        if ctx.get("sog0") is not None and not ctx.get("stopped") and ctx["sog0"] >= 1:
            observed += (f" (at its last speed of {ctx['sog0']:.1f} kn it would have run "
                         f"{km_text(ctx['sog0'] * geo.KM_PER_NM * hours)})")
        observed += f". It {ep['reading']}."
        if changes:
            observed += " After the gap: " + "; ".join(changes) + "."
        evidence = [["Silent for", duration_text(ep["t1"] - ep["t0"])], ["Went silent", utc_text(ep["t0"], True)],
                    ["Reappeared", utc_text(ep["t1"], True)], ["Moved", moved]]
        if ep.get("implied_kn") is not None and ep["dist_km"] >= 1:
            evidence.append(["Average speed implied", f"{ep['implied_kn']:.1f} kn"])
        if ctx.get("sog0") is not None:
            evidence.append(["Last speed before the gap", f"{ctx['sog0']:.1f} kn"])
        if ep.get("ratio") is not None:
            evidence.append(["Distance against its last speed", f"{ep['ratio']:.2f} of the expected run"])
        if d0 is not None or d1 is not None:
            evidence.append(["Draught", f"{_m(d0)} before, {_m(d1)} after"])
        for c in changes:
            if not c.startswith("draught"):
                evidence.append(["Identity or voyage change", c])
        if aoi:
            evidence.append(["Area of interest", f"{aoi['name']} ({aoi.get('label', 'in')})"])
        if ep.get("near_tanker"):
            nt = ep["near_tanker"]
            evidence.append(["Near a stopped tanker", f"{nt['name']}, {km_text(nt['km'])} away when it went silent"])
        if v.get("sanc_strong"):
            evidence.append(["Sanctions", f"On the {lists_text(v['sanc']['lists'])} (matched by {str(v['sanc']['match']).upper()})"])
        if ep.get("place0"):
            evidence.append(["Where it went silent", ep["place0"]])
        coverage = []
        if n_last:
            coverage.append(f"When the silence was flagged, {n_last} other vessels within 30 km of its last position had "
                            "reported in the previous 20 minutes.")
        if cov:
            coverage.append(f"Across the gap, other vessels were heard within 30 km of where it should have been (on the "
                            f"straight line between where it went silent and where it reappeared) in {cov['covered']} of "
                            f"{cov['checks']} half-hour checks ({cov['vessels']} different vessels).")
        if not coverage:
            coverage.append("Coverage near its last position was checked when the silence began.")
        innocent = ["Gaps have innocent causes: receiver coverage, equipment faults, a transponder switched off at anchor "
                    "or in port, and security practice in waters where piracy is a risk."]
        if ep.get("place0"):
            innocent.insert(0, f"It went silent {ep['place0']}, where switching AIS off or losing reception is common.")
        reading = ep["reading"]
        conf_text = {"high": "Coverage held along its path across the gap, so the silence is unlikely to be a receiver "
                             "problem.",
                     "medium": "Coverage was proven when the silence began; it was only partly checked along its path.",
                     "low": "Few other vessels were heard along its path during the gap, so reception may simply have "
                            "dropped."}[confidence]
        if hole:
            conf_text = ("It reappeared where its last speed puts it and coverage along its path was not proven, so this "
                         "is most likely a hole between receivers: not flagged.")
        return self._item(
            id=ep["id"], kind="ais_gap", title=title, subtitle=subtitle, lat=ep["lat0"], lon=ep["lon0"],
            first_time=ep["t0"], time=ep["t1"], ongoing=False, severity=severity, confidence=confidence,
            confidence_text=conf_text, observed=observed, evidence=evidence, innocent=innocent,
            coverage=" ".join(coverage),
            limits=("Meridian only hears terrestrial AIS receivers, so an apparent gap can be a hole between receivers; "
                    f"the reading ({reading}) compares the two ends only, and coverage is checked along a straight line "
                    "between them, not the route it really took."),
            vessels=[_brief(v, "vessel")] + ([{"kind": "ship", "id": ep["near_tanker"]["mmsi"],
                                                "title": ep["near_tanker"]["name"], "subtitle": "Stopped tanker nearby",
                                                "layer": "ships", "role": "nearby tanker", "sanctions": None}]
                                              if ep.get("near_tanker") else []),
            state="coverage_hole" if aoi and hole else "candidate" if aoi else "outside_areas",
            published=bool(aoi) and not hole, event_kind="ship",
            event_id=str(ep["mmsi"]), aoi=aoi,
            geometry={"type": "LineString", "coordinates": [[round(ep["lon0"], 5), round(ep["lat0"], 5)],
                                                            [round(ep["lon1"], 5), round(ep["lat1"], 5)]]},
            block=[["Silent", duration_text(ep["t1"] - ep["t0"])], ["Moved", moved],
                   ["Draught", f"{dchange:+.1f} m" if dchange is not None else "–"]],
            extra={"reading": reading, "end": [round(ep["lat1"], 5), round(ep["lon1"], 5)]})

    def _quiet_item(self, q: dict, infos: dict, now: float) -> dict | None:
        v = infos.get(q["mmsi"])
        if v is None:
            return None
        silent = now - q["t0"]
        name = v["name"] or f"MMSI {v['mmsi']}"
        aoi = q["aoi"]
        severity = min(6, 4 + (1 if v["sanc_strong"] else 0))
        evidence = [["Silent for", duration_text(silent) + " so far"], ["Last report", utc_text(q["t0"], True)],
                    ["State then", "stopped"], ["Area of interest", aoi["name"]]]
        if v["sanc_strong"]:
            evidence.append(["Sanctions", f"On the {lists_text(v['sanc']['lists'])} (matched by {str(v['sanc']['match']).upper()})"])
        return self._item(
            id=f"gap:{q['mmsi']}:{int(q['t0'])}", kind="ais_gap", title=f"AIS gap {aoi_phrase(aoi)}",
            subtitle=f"{_type_word(v).capitalize()} {name}, stopped and silent for {duration_text(silent)}",
            lat=q["lat"], lon=q["lon"], first_time=q["t0"], time=now, ongoing=True, severity=severity,
            confidence="medium",
            confidence_text="Coverage was proven when the silence was flagged; the gap is still open.",
            observed=(f"The {_type_word(v)} was stopped outside any port or anchorage and reporting regularly, then went "
                      f"silent at {utc_text(q['t0'], True)} while {q['n_last']} vessels nearby kept reporting."),
            evidence=evidence,
            innocent=["Ships may switch AIS off at anchor, or lose reception; equipment faults and security practice "
                      "in some waters also cause gaps."],
            coverage=f"{q['n_last']} other vessels within 30 km reported in the 20 minutes before it was flagged.",
            limits="Terrestrial AIS only. The episode is summarised again when the vessel reappears.",
            vessels=[_brief(v, "vessel")], state="candidate", published=True, event_kind="ship",
            event_id=str(v["mmsi"]), aoi=aoi, geometry={"type": "Point", "coordinates": [round(q["lon"], 5), round(q["lat"], 5)]},
            block=[["Silent", duration_text(silent)], ["Stopped at", f"{q['lat']:.3f}, {q['lon']:.3f}"]],
            extra={"reading": "ongoing"})

    # ==========================================================================================
    # GNSS spoofing clusters
    # ==========================================================================================
    def _run_gnss(self, now: float) -> None:
        self.last["gnss"] = now
        pts = []
        with self.store.lock:
            for v in self.store.vessels.values():
                # Only a fix that itself reported an impossible speed is speed evidence, placed where it was
                # reported. Fixes that are suspect only because they sit inside a hotspot are not new
                # evidence, so a hotspot cannot keep itself alive (or follow its vessels) without it.
                if v.suspect_sog is not None and v.suspect_sog_lat is not None \
                        and now - v.suspect_sog_t <= GNSS_WINDOW_S:
                    pts.append({"mmsi": v.mmsi, "lat": v.suspect_sog_lat, "lon": v.suspect_sog_lon,
                                "t": v.suspect_sog_t, "kind": "speed", "sog": v.suspect_sog})
            jumps = [j for j in self.store.jumps if now - j[1] <= GNSS_WINDOW_S]
        # Only the latest few jumps per vessel: a spoofing event can fill the whole jump buffer in one place,
        # and the pairwise checks below would otherwise grow with the square of it.
        per: dict[int, int] = {}
        for mmsi, t, lat0, lon0, lat1, lon1, sog, _acc in reversed(jumps):
            if per.get(mmsi, 0) >= GNSS_JUMPS_PER_VESSEL:
                continue
            per[mmsi] = per.get(mmsi, 0) + 1
            pts.append({"mmsi": mmsi, "lat": lat1, "lon": lon1, "t": t, "kind": "jump", "sog": sog, "from": (lat0, lon0)})
        land = self.land if self.land is not None and self.land.available else None
        found = []
        for grp in group_points(pts, GNSS_GROUP_KM):
            h = gnss_signatures(grp, land)
            if h is not None:
                found.append(h)
        active = [h for h in self.gnss if now - h["t"] <= GNSS_HOLD_S]
        day = int(now // 86400)
        for h in found:
            target = None
            for old in active:
                if geo.haversine_km(old["lat"], old["lon"], h["lat"], h["lon"]) <= GNSS_JOIN_KM + old["radius_km"] / 2:
                    target = old
                    break
            if target is None:
                target = {"id": f"ships:{h['lat']:.2f},{h['lon']:.2f}", "first_t": h["t_first"], "mmsi_all": [],
                          "signatures": [], "saved_t": 0.0, "air": None, "air_t": 0.0, "max_vessels": 0}
                self.gnss.append(target)
                active.append(target)
            target.update({k: h[k] for k in ("lat", "lon", "radius_km", "vessels", "mmsis", "t", "points", "jumps",
                                             "common", "circle", "inland_vessels")})
            target["max_sog"] = max(x for x in (target.get("max_sog"), h["max_sog"]) if x is not None) \
                if (target.get("max_sog") is not None or h["max_sog"] is not None) else None
            target["signatures"] = sorted(set(target["signatures"]) | set(h["signatures"]),
                                          key=lambda s: ("jump", "circle", "land", "speed").index(s))
            target["mmsi_all"] = sorted(set(target["mmsi_all"]) | set(h["mmsis"]))[:60]
            target["max_vessels"] = max(target["max_vessels"], h["vessels"])
            target["last_seen"] = now
            if self.db is not None:
                self.db.execute("INSERT OR IGNORE INTO ships_gnss_days(cell, day) VALUES(?, ?)",
                                (cell_key(h["lat"], h["lon"], GNSS_DAY_CELL_DEG), day))
        for h in active:
            if self.air_lookup is not None and now - h.get("air_t", 0) >= AIR_EVERY_S:
                h["air_t"] = now
                try:
                    h["air"] = self.air_lookup(h["lat"], h["lon"], h["radius_km"], now - GNSS_HOLD_S)
                except Exception:
                    h["air"] = None
        self.gnss = [h for h in self.gnss if now - h["t"] <= RECENT_S]
        self._sync_hotspots(now)
        self.excluded["gnss"] = Counter({"suspect points": len(pts)})

    def _sync_hotspots(self, now: float) -> None:
        spots = [(h["lat"], h["lon"], h["radius_km"]) for h in self.gnss if now - h["t"] <= GNSS_HOLD_S]
        with self.store.lock:
            self.store.hotspots = spots

    def hotspots(self, now: float | None = None) -> list[dict]:
        """Active hotspots in the old ``ShipsService.hotspots`` shape (status, drawer)."""
        now = self.now() if now is None else now
        return [{"lat": h["lat"], "lon": h["lon"], "radius_km": h["radius_km"], "vessels": h["vessels"],
                 "max_sog": h.get("max_sog") or 0.0, "t": h["t"], "first_t": h["first_t"], "id": h["id"],
                 "signatures": list(h.get("signatures") or []), "mmsis": [str(m) for m in h.get("mmsis") or []]}
                for h in self.gnss if now - h["t"] <= GNSS_HOLD_S]

    def _gnss_days(self, lat: float, lon: float, now: float) -> int:
        if self.db is None:
            return 1
        day = int(now // 86400)
        cells = cell_neighbours(cell_key(lat, lon, GNSS_DAY_CELL_DEG), GNSS_DAY_CELL_DEG)
        marks = ",".join("?" for _ in cells)
        return int(self.db.scalar(f"SELECT COUNT(DISTINCT day) FROM ships_gnss_days WHERE day > ? AND cell IN ({marks})",
                                  [day - LEARN_DAYS] + cells, default=0) or 0)

    def _gnss_item(self, h: dict, infos: dict, now: float) -> dict:
        sigs = list(h.get("signatures") or [])
        n = max(h.get("vessels") or 0, h.get("max_vessels") or 0)
        air = h.get("air") or None
        # A circle is a shape drawn from the same suspect points the other signatures use, not independent
        # evidence, so it does not count towards confidence.
        independent = len([s for s in sigs if s != "circle"])
        if air and air.get("level") in ("amber", "red") or (independent >= 2 and n >= 3):
            confidence = "high"
        elif n >= 3 or independent >= 2:
            confidence = "medium"
        else:
            confidence = "low"
        severity = {"low": 4, "medium": 5, "high": 6}[confidence]
        days = self._gnss_days(h["lat"], h["lon"], now)
        since_start = int((now - self.started) // 86400) + 1
        history = (f"Seen on {days} of the last {LEARN_DAYS} days near here." if since_start >= LEARN_DAYS else
                   f"Seen on {days} day{'s' if days != 1 else ''} near here; Meridian has {min(since_start, LEARN_DAYS)} "
                   f"of {LEARN_DAYS} days of history so far.")
        sig_text = {"jump": "positions that jumped to a common point", "circle": "positions spread on a circle round one point",
                    "land": "positions that jumped from the sea onto land", "speed": "impossible speeds"}
        what = ", ".join(sig_text[s] for s in sigs) or "impossible positions"
        when = f"between {utc_text(h['first_t'])} and {utc_text(h['t'])}" if h["t"] - h["first_t"] >= 120 else f"at {utc_text(h['t'])}"
        observed = f"{n} ships reported {what} {when}"
        if h.get("max_sog"):
            observed += f" (speeds up to {h['max_sog']:.0f} kn)"
        observed += "."
        if air and air.get("level") in ("amber", "red"):
            observed += " Aircraft in this area also report degraded GPS in the same hours."
        evidence = [["Vessels", str(n)], ["Signatures", ", ".join(s.capitalize() for s in sigs)],
                    ["First seen", utc_text(h["first_t"], True)], ["Last seen", utc_text(h["t"], True)],
                    ["Area", f"about {km_text(h['radius_km'])} radius"], ["History", history]]
        if h.get("common"):
            evidence.append(["Common point", f"{h['common'][0]:.3f}, {h['common'][1]:.3f}"])
        if h.get("circle"):
            evidence.append(["Circle", f"radius {km_text(h['circle']['radius_km'])}, within {h['circle']['deviation'] * 100:.0f}%"])
        if h.get("inland_vessels"):
            evidence.append(["On land", f"{h['inland_vessels']} vessels placed 3 km or more inland"])
        if air:
            evidence.append(["Aircraft GPS", f"{air.get('level', 'none')} cell{', ' + str(air['share']) + '% affected' if air.get('share') is not None else ''}"])
        else:
            evidence.append(["Aircraft GPS", "no aircraft interference reported here (or no data)"])
        mmsis = list(h.get("mmsi_all") or h.get("mmsis") or [])
        vessels = [_brief(infos[m], "reported impossible positions") for m in mmsis[:12] if m in infos]
        title = "Possible GNSS spoofing of ships"
        subtitle = f"{n} vessels, {what}"
        if air and air.get("level") in ("amber", "red"):
            subtitle += "; aircraft GPS also degraded"
        return self._item(
            id=f"gnss:{h['lat']:.2f},{h['lon']:.2f}" if not h["id"].startswith("ships:") else "gnss:" + h["id"][6:],
            kind="gnss", title=title, subtitle=subtitle, lat=h["lat"], lon=h["lon"], first_time=h["first_t"],
            time=h["t"], ongoing=now - h["t"] <= GNSS_WINDOW_S, severity=severity, confidence=confidence,
            confidence_text={"high": "Several signatures, or aircraft corroborate it.",
                             "medium": "Three or more vessels, or more than one signature.",
                             "low": "Only two vessels with one signature."}[confidence],
            observed=observed, evidence=evidence,
            innocent=["Faulty or badly set up AIS units, two ships sharing one MMSI and data errors can also produce "
                      "impossible positions; several vessels at once makes interference more likely.",
                      "Interference is inferred from what ships and aircraft report, not measured."],
            coverage=f"From the positions {n} vessels broadcast; Meridian keeps what they reported but marks the fixes as suspect.",
            limits=("Signatures need several vessels within 30 minutes; spoofing that moves ships plausibly, or affects one "
                    "ship, is not caught. The land check uses simplified coastlines."),
            vessels=vessels, state="candidate", published=True, event_kind="gps_jam", event_id=h["id"], aoi=None,
            geometry={"type": "Point", "coordinates": [h["lon"], h["lat"]]},
            block=[["Vessels", str(n)], ["Signatures", str(len(sigs))], ["Days seen", f"{days} of {LEARN_DAYS}"]],
            extra={"radius_km": h["radius_km"], "signatures": sigs, "air": air, "days_seen": days, "hotspot_id": h["id"]})

    # ==========================================================================================
    # Learning usual anchorages and waiting areas
    # ==========================================================================================
    def _learn_stops(self, now: float) -> None:
        self.last["learn"] = now
        day = int(now // 86400)
        if self._written_day != day:
            self._written, self._written_day = set(), day
        rows = []
        with self.store.lock:
            for v in self.store.vessels.values():
                if v.state != "stationary" or v.stop_lat is None or now - v.t > 3600 or not v.state_since:
                    continue
                if now - v.state_since < 3600 or v.cls in ("pleasure", "fishing", "highspeed", "service"):
                    continue
                if v.length is not None and v.length < STS_MIN_LEN:
                    continue
                key = (cell_key(v.stop_lat, v.stop_lon, STOP_CELL_DEG), day, v.mmsi)
                if key not in self._written:
                    rows.append((key, v.stop_lat, v.stop_lon))
        out = []
        for key, lat, lon in rows:
            self._written.add(key)
            if not self.port_near(lat, lon):
                out.append(key)
        if out and self.db is not None:
            self.db.executemany("INSERT OR IGNORE INTO ships_stopcells(cell, day, mmsi) VALUES(?,?,?)", out)
        self._count_day("stops", now)

    def _load_habitual(self, now: float) -> None:
        self.last["habitual"] = now
        if self.db is None:
            return
        since = int(now // 86400) - LEARN_DAYS
        self.habitual_stop = {int(r["cell"]) for r in self.db.query(
            "SELECT cell, COUNT(DISTINCT mmsi) AS n FROM ships_stopcells WHERE day > ? GROUP BY cell HAVING n >= ?",
            (since, HABITUAL_STOP_VESSELS))}
        self.habitual_loiter = {int(r["cell"]): (int(r["v"]), int(r["d"])) for r in self.db.query(
            "SELECT cell, COUNT(DISTINCT mmsi) AS v, COUNT(DISTINCT day) AS d FROM ships_loitercells WHERE day > ? "
            "GROUP BY cell HAVING v >= ? AND d >= ?", (since, HABITUAL_LOITER_VESSELS, HABITUAL_LOITER_DAYS))}

    def _count_day(self, name: str, now: float) -> None:
        day = int(now // 86400)
        d = self.days.setdefault(name, {})
        d[str(day)] = int(d.get(str(day), 0)) + 1
        for k in [k for k in d if not k.isdigit() or int(k) < day - 30]:
            del d[k]
        if self.db is not None:
            try:
                self.db.kv_set(self.KV_DAYS, self.days)
            except Exception:
                pass

    def learned_days(self, name: str, min_runs: int, now: float | None = None) -> int:
        now = self.now() if now is None else now
        day = int(now // 86400)
        return sum(1 for k, n in (self.days.get(name) or {}).items()
                   if k.isdigit() and day - LEARN_DAYS < int(k) <= day and int(n) >= min_runs)

    # ==========================================================================================
    # Output
    # ==========================================================================================
    def _coverage_text(self, scene_snap, lat, lon, now, src=None) -> str:
        snap = self.store.snapshot
        heard = 0
        srcs = set()
        for _d, row in snap.grid.near(lat, lon, 30.0):
            if now - row[R_T] <= 3600:
                heard += 1
                srcs.add(row[R_SRC])
        names = [SRC_PHRASE[s] for s in sorted(srcs) if s in SRC_PHRASE] or ([SRC_PHRASE[src]] if src in SRC_PHRASE else [])
        via = f" through {' and '.join(names)}" if names else ""
        return f"Heard{via}; {heard:,} vessel{'s' if heard != 1 else ''} within 30 km reported in the last hour."

    @staticmethod
    def _item(**kw) -> dict:
        kind = kw.pop("kind")
        extra = kw.pop("extra", None) or {}
        aoi = kw.pop("aoi", None)
        item = {"kind": kind, "category": CATEGORIES[kind], **kw}
        item["aoi"] = {k: aoi[k] for k in ("kind", "id", "name") if k in aoi} | ({"basis": aoi["basis"]} if aoi.get("basis") else {}) \
            if aoi else None
        item["mmsis"] = [v["id"] for v in kw.get("vessels") or [] if v.get("kind") == "ship"]
        item["lat"], item["lon"] = round(kw["lat"], 5), round(kw["lon"], 5)
        item["first_time"], item["time"] = int(kw["first_time"]), int(kw["time"])
        item.update(extra)
        return item

    def _build(self, now: float) -> None:
        mmsis = set()
        for ep in self.sts.values():
            mmsis.update((ep["a"], ep["b"]))
        mmsis.update(self.loiter)
        mmsis.update(ep["mmsi"] for ep in self.gaps.values())
        mmsis.update(self.quiet)
        for h in self.gnss:
            mmsis.update(h.get("mmsi_all") or h.get("mmsis") or [])
        infos = self._infos(mmsis, now)
        items = []
        for ep in list(self.sts.values()):
            it = self._safe(self._sts_item, ep, infos, now)
            if it:
                items.append(it)
                if ep["state"] == "open" or not ep.get("saved_t"):
                    self._save("sts", ep, it, now, force=not ep.get("saved_t"))
        for ep in list(self.loiter.values()):
            it = self._safe(self._loiter_item, ep, infos, now)
            if it:
                items.append(it)
                if ep["state"] == "open" or not ep.get("saved_t"):
                    self._save("loiter", ep, it, now, force=not ep.get("saved_t"))
        quiet_ids = set()
        for q in list(self.quiet.values()):
            it = self._safe(self._quiet_item, q, infos, now)
            if it:
                items.append(it)
                quiet_ids.add(it["id"])
        for ep in list(self.gaps.values()):
            if ep["id"] in quiet_ids:
                continue
            it = self._safe(self._gap_item, ep, infos, now)
            if it:
                items.append(it)
                if ep.get("settled") and not ep.get("saved_t"):
                    self._save("gap", ep, it, now, force=True)
        for h in list(self.gnss):
            it = self._safe(self._gnss_item, h, infos, now)
            if it:
                items.append(it)
                if now - h["t"] <= GNSS_HOLD_S or not h.get("saved_t"):
                    self._save("gnss", h, it, now, force=not h.get("saved_t"))
        items.sort(key=lambda it: (not it["published"], -it["time"]))
        self.items = items
        self.by_id = {it["id"]: it for it in items}

    def _safe(self, fn, *args):
        try:
            return fn(*args)
        except Exception as err:      # a bad episode never hides the others
            if self.log:
                self.log.warning("ships anomalies: could not describe an episode: %s", err, exc_info=True)
            return None

    # -- reads --------------------------------------------------------------------------------------
    def query(self, *, bbox=None, kinds=None, hours: float = 48.0, include_all: bool = False,
              now: float | None = None) -> list[dict]:
        now = self.now() if now is None else now
        since = now - max(1.0, min(float(hours), MAX_HOURS)) * 3600
        items = list(self.items)
        if hours * 3600 > RECENT_S:
            have = {it["id"] for it in items}
            items.extend(it for it in self._stored(since) if it["id"] not in have)
        out = []
        for it in items:
            if it["time"] < since or (not include_all and not it["published"]):
                continue
            if kinds and it["kind"] not in kinds:
                continue
            if bbox is not None and not geo.in_bbox(it["lat"], it["lon"], bbox):
                continue
            out.append(it)
        out.sort(key=lambda it: (not it["published"], -it["severity"], -it["time"]))
        return out

    def _stored(self, since: float) -> list[dict]:
        if self.db is None:
            return []
        out = []
        for sql in ("SELECT info FROM ships_sts WHERE t1 >= ?", "SELECT info FROM ships_loiter WHERE t1 >= ?",
                    "SELECT info FROM ships_gaps WHERE t1 >= ? AND info IS NOT NULL",
                    "SELECT info FROM ships_gnss WHERE last_t >= ?"):
            for r in self.db.query(sql + " LIMIT 2000", (since,)):
                it = (_loads(r["info"]) or {}).get("item")
                if isinstance(it, dict) and it.get("id"):
                    out.append(dict(it, ongoing=False))
        return out

    def get(self, aid: str) -> dict | None:
        hit = self.by_id.get(aid)
        if hit is not None or self.db is None:
            return hit
        kind = aid.split(":", 1)[0]
        try:
            if kind == "sts":
                row = self.db.one("SELECT info FROM ships_sts WHERE id = ?", (aid,))
            elif kind == "loiter":
                row = self.db.one("SELECT info FROM ships_loiter WHERE id = ?", (aid,))
            elif kind == "gap":
                _k, mmsi, t0 = aid.split(":")
                row = self.db.one("SELECT info FROM ships_gaps WHERE mmsi = ? AND CAST(t0 AS INTEGER) = ?", (int(mmsi), int(t0)))
            elif kind == "gnss":
                row = self.db.one("SELECT info FROM ships_gnss WHERE id = ?", ("ships:" + aid[5:],))
            else:
                row = None
        except (ValueError, TypeError):
            row = None
        it = (_loads(row["info"]) or {}).get("item") if row else None
        return dict(it, ongoing=False) if isinstance(it, dict) else None

    def for_vessel(self, mmsi) -> list[dict]:
        m = str(mmsi)
        return [{k: it[k] for k in ("id", "kind", "category", "title", "subtitle", "state", "published", "severity",
                                    "confidence", "time", "first_time", "ongoing", "lat", "lon")}
                for it in self.items if m in it.get("mmsis", [])]

    def events(self, since: float) -> list[dict]:
        out = []
        for it in self.items:
            if not it["published"] or it["time"] < since:
                continue
            ev = {"kind": it["event_kind"], "id": it["event_id"], "title": it["title"], "subtitle": it["subtitle"],
                  "lat": it["lat"], "lon": it["lon"], "layer": "ships.anomalies", "time": it["time"],
                  "first_time": it["first_time"], "severity": it["severity"], "category": it["category"],
                  "anomaly_id": it["id"], "confidence": it["confidence"]}
            if it["kind"] == "gnss":
                ev.update(radius_km=it.get("radius_km"), signatures=it.get("signatures"))
            out.append(ev)
        return out

    def status(self, now: float | None = None) -> dict:
        now = self.now() if now is None else now
        stop_days = self.learned_days("stops", STOP_DAY_RUNS, now)
        loiter_days = self.learned_days("loitering", LOITER_DAY_RUNS, now)
        counts = Counter((it["kind"], it["published"]) for it in self.items)
        land = None if self.land is None else (self.land.available, self.land.error)

        def det(kind, state, text, **more):
            return {"state": state, "status": text, "flagged": counts[(kind, True)],
                    "unflagged": counts[(kind, False)], "excluded": dict(self.excluded.get(kind) or {}),
                    "last_run": self.last.get(kind) or None, "error": self.errors.get(kind), **more}

        return {
            "sts": det("sts", "ready" if stop_days >= LEARN_DAYS else "learning",
                       "Ready." if stop_days >= LEARN_DAYS else
                       building_text(stop_days, LEARN_DAYS).replace("Building baseline", "Learning usual anchorages")
                       + "; built-in anchorages and crowded ones are excluded meanwhile.",
                       learned_anchorages=len(self.habitual_stop), excluded_waters=STS_EXCLUDED_TEXT),
            "loitering": det("loitering", "ready" if loiter_days >= LOITER_LEARN_DAYS else "learning",
                             "Ready." if loiter_days >= LOITER_LEARN_DAYS else
                             building_text(loiter_days, LOITER_LEARN_DAYS).replace("Building baseline",
                                                                                   "Learning usual waiting areas")
                             + "; candidates are shown but not flagged until then.",
                             learned_waiting_areas=len(self.habitual_loiter)),
            "ais_gap": det("ais_gap", "ready", "Ready: gaps are flagged only near monitored straits and areas of "
                                               "interest, with coverage proven by other vessels.",
                           quiet_now=len(self.quiet)),
            "gnss": det("gnss", "ready" if land is None or land[0] else "partial",
                        "Ready." if land is None or land[0] else
                        f"Ready without the land check: {land[1] or 'land shapes unavailable'}",
                        active_hotspots=len(self.hotspots(now))),
        }


# ---------------------------------------------------------------------------------------------
def _loads(text) -> dict | None:
    try:
        out = json.loads(text) if text else None
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def _m(v) -> str:
    return "unknown" if v is None else f"{v:.1f} m"


def _speed_range(lo: float, hi: float) -> str:
    lo_r, hi_r = round(lo, 1), round(hi, 1)
    if hi_r - lo_r < 0.6:
        return f"about {max(lo_r, 0.1):.1f} kn".replace(".0 kn", " kn")
    return f"{lo_r:.0f} to {max(hi_r, lo_r + 1):.0f} kn" if hi_r >= 2 else f"{lo_r:.1f} to {hi_r:.1f} kn"


def _type_word(v: dict) -> str:
    cls = v.get("cls") or "other"
    if cls == "tanker":
        return "tanker"
    if cls == "cargo":
        return "cargo ship"
    return "vessel"


def _brief(v: dict, role: str) -> dict:
    flag = flag_of(v["mmsi"])
    bits = [CLASS_LABEL.get(v.get("cls") or "other", "Vessel") if v.get("cls") != "other" else "Vessel"]
    if flag:
        bits.append(flag[1])
    if v.get("length"):
        bits.append(f"{int(v['length'])} m")
    s = v.get("sanc")
    sanctions = None
    if s:
        if s["strength"] == "listed":
            bits.append(f"sanctioned: {', '.join(s['lists'])}")
            sanctions = {"strength": "listed", "lists": s["lists"], "match": s["match"], "listed_as": s.get("listed_as"),
                         "text": f"On the {lists_text(s['lists'])}, matched by {str(s['match']).upper()}"}
        else:
            bits.append("possible sanctions match")
            sanctions = {"strength": "possible", "lists": s["lists"], "match": s["match"], "listed_as": s.get("listed_as"),
                         "text": "A possible match on name or MMSI only; the IMO does not match a listing"}
    ent = {"kind": "ship", "id": str(v["mmsi"]), "title": v.get("name") or f"MMSI {v['mmsi']}",
           "subtitle": ", ".join(bits), "layer": "ships", "role": role, "cls": v.get("cls"), "imo": v.get("imo"),
           "flag": flag[1] if flag else None, "length": v.get("length"), "sanctions": sanctions}
    if v.get("lat") is not None:
        ent.update(lat=round(v["lat"], 5), lon=round(v["lon"], 5))
    return ent
