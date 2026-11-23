"""Environment Agency real-time flood-monitoring API (England): flood warnings and alerts, their
flood-area polygons, and river and sea level gauges. Pure parsers; the polling is in ``floods.py``.

Licence: Open Government Licence v3 (in the API's own metadata). The API asks for this credit:
"this uses Environment Agency flood and river level data from the real-time data API (Beta)".
The warnings list is updated every 15 minutes and the docs ask clients to read the latest levels
with one ``/data/readings?latest`` call every 15 minutes rather than crawling stations.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .common import clean, num, parse_time, valid_lonlat

API = "https://environment.data.gov.uk/flood-monitoring"
FLOODS_URL = API + "/id/floods"
POLYGON_URL = API + "/id/floodAreas/{id}/polygon"
STATIONS_URL = API + "/id/stations"
READINGS_URL = API + "/data/readings"
MEASURE_READINGS_URL = API + "/id/measures/{id}/readings"
DOCS_URL = API + "/doc/reference"
AREA_PAGE = "https://check-for-flooding.service.gov.uk/target-area/{id}"
STATION_PAGE = "https://check-for-flooding.service.gov.uk/station/{rloi}"
FLOODLINE = "0345 988 1188"
CREDIT = "This uses Environment Agency flood and river level data from the real-time data API (Beta)"

AREA_ID = re.compile(r"[A-Za-z0-9_]{3,30}")
STATION_ID = re.compile(r"[A-Za-z0-9_]{1,30}")
MEASURE_ID = re.compile(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+){1,8}")

# England, generously (the warnings and gauges are English; Welsh and Scottish services are separate)
ENGLAND_BBOX = (-6.5, 49.8, 2.1, 55.9)

LEVELS = {
    1: {"name": "Severe flood warning", "short": "Severe flood warning", "meaning": "Severe flooding, danger to life.",
        "tone": "red", "severity": 9},
    2: {"name": "Flood warning", "short": "Flood warning", "meaning": "Flooding is expected, immediate action required.",
        "tone": "amber", "severity": 6},
    3: {"name": "Flood alert", "short": "Flood alert", "meaning": "Flooding is possible, be prepared.",
        "tone": "amber", "severity": 3},
    4: {"name": "Warning no longer in force", "short": "No longer in force",
        "meaning": "The warning is no longer in force.", "tone": "grey", "severity": 1},
}

# gauge state -> (label, rank for sorting and colour)
GAUGE_STATES = {
    "highest": ("At or above the highest level of recent years", 4),
    "high": ("Above the normal range", 3),
    "normal": ("Within the normal range", 2),
    "low": ("Below the normal range", 1),
    "unknown": ("No normal range published", 0),
}
QUALIFIER_ORDER = {"Stage": 0, "Tidal Level": 1, "Downstream Stage": 2}


# ---------------------------------------------------------------------------------------------
# time: the warnings carry no zone; they are read as UK local time (GMT in winter, BST in summer)
# ---------------------------------------------------------------------------------------------
def _last_sunday(year: int, month: int) -> datetime:
    nxt = datetime(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def uk_time(value: Any) -> float | None:
    """Epoch seconds from an EA time. Times with a zone (readings use "Z") are taken as given; naive
    ones are UK civil time: BST from 01:00 UTC on the last Sunday in March to 01:00 UTC on the last
    Sunday in October."""
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith(("Z", "z")) or re.search(r"[+-]\d{2}:?\d{2}$", s):
        return parse_time(s)
    try:
        local = datetime.fromisoformat(s)
    except ValueError:
        return parse_time(s)
    start = _last_sunday(local.year, 3).replace(hour=1)
    end = _last_sunday(local.year, 10).replace(hour=1)
    offset = 1 if start <= local < end else 0
    return (local - timedelta(hours=offset)).replace(tzinfo=timezone.utc).timestamp()


def _first(v: Any) -> Any:
    """Some EA fields come as a list when a record was merged (labels, coordinates, RLOI ids)."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


_KEEP_UPPER = {"STW", "WTW", "PS", "GS", "UK", "EA", "II", "III"}


def tidy_name(text: Any, limit: int = 80) -> str:
    """Station labels arrive in mixed styles ("ANGARRACK", "Rose in Vale"): capitalise the shouted ones."""
    s = clean(_first(text), limit)
    if not s or not s.isupper():
        return s
    words = []
    for w in s.split(" "):
        words.append(w if w in _KEEP_UPPER else "-".join(p[:1] + p[1:].lower() for p in w.split("-")))
    out = " ".join(words)
    for small in (" And ", " Of ", " At ", " On ", " The ", " In "):
        out = out.replace(small, small.lower())
    return out


def _last_segment(uri: Any) -> str:
    return str(_first(uri) or "").rstrip("/").rsplit("/", 1)[-1]


def level_info(level: int | None) -> dict:
    return LEVELS.get(level or 0, {"name": "Flood message", "short": "Flood message", "meaning": "", "tone": "grey",
                                   "severity": 1})


# ---------------------------------------------------------------------------------------------
# warnings
# ---------------------------------------------------------------------------------------------
def _message(text: Any) -> str:
    s = str(text or "").replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in s.split("\n")]
    out = "\n".join(line for line in lines if line)
    return out[:4000]


def parse_floods(data: Any) -> list[dict]:
    """Current warnings and alerts (and those no longer in force), one per flood area."""
    items = data.get("items") if isinstance(data, dict) else None
    out: dict[str, dict] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        area = it.get("floodArea") if isinstance(it.get("floodArea"), dict) else {}
        aid = str(it.get("floodAreaID") or area.get("notation") or "").strip()
        if not AREA_ID.fullmatch(aid):
            continue
        level = num(it.get("severityLevel"))
        level = int(level) if level is not None and 1 <= level <= 4 else None
        if level is None:
            continue
        info = LEVELS[level]
        w = {
            "id": f"ea:{aid}", "area_id": aid, "level": level, "severity": info["name"], "meaning": info["meaning"],
            "name": clean(it.get("description"), 160) or clean(_first(area.get("label")), 160) or aid,
            "ea_area": clean(it.get("eaAreaName"), 80) or None, "county": clean(area.get("county"), 160) or None,
            "river_sea": clean(area.get("riverOrSea"), 160) or None, "tidal": bool(it.get("isTidal")),
            "message": _message(it.get("message")), "raised": uk_time(it.get("timeRaised")),
            "message_changed": uk_time(it.get("timeMessageChanged")),
            "severity_changed": uk_time(it.get("timeSeverityChanged")),
        }
        w["time"] = w["severity_changed"] or w["raised"] or w["message_changed"]
        prev = out.get(aid)
        if prev is None or (level, -(w["time"] or 0)) < (prev["level"], -(prev["time"] or 0)):
            out[aid] = w
    return sorted(out.values(), key=lambda w: (w["level"], -(w["time"] or 0)))


def parse_polygon(data: Any) -> dict | None:
    """The flood area's outline and the descriptive fields that come with it."""
    feats = data.get("features") if isinstance(data, dict) else None
    if not feats:
        if isinstance(data, dict) and data.get("type") in ("Polygon", "MultiPolygon"):
            feats = [{"geometry": data, "properties": {}}]
        else:
            return None
    polys: list = []
    props: dict = {}
    for f in feats:
        if not isinstance(f, dict):
            continue
        g = f.get("geometry") or {}
        if g.get("type") == "Polygon":
            polys.append(g.get("coordinates") or [])
        elif g.get("type") == "MultiPolygon":
            polys.extend(g.get("coordinates") or [])
        if not props and isinstance(f.get("properties"), dict):
            props = f["properties"]
    polys = [p for p in polys if p and p[0] and len(p[0]) >= 4]
    if not polys:
        return None
    geom = {"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1 else {"type": "MultiPolygon", "coordinates": polys}
    return {"geometry": geom, "name": clean(props.get("TA_NAME"), 160) or None,
            "description": clean(props.get("DESCRIP"), 1200) or None, "local_authority": clean(props.get("LA_NAME"), 200) or None,
            "quick_dial": clean(props.get("QDIAL"), 12) or None, "river_sea": clean(props.get("RIVER_SEA"), 160) or None,
            "ea_area": clean(props.get("AREA"), 80) or None}


# ---------------------------------------------------------------------------------------------
# gauges
# ---------------------------------------------------------------------------------------------
def _reading(r: Any) -> dict | None:
    if not isinstance(r, dict):
        return None
    v = num(_first(r.get("value")))
    if v is None:
        return None
    return {"value": round(v, 3), "time": uk_time(r.get("dateTime"))}


def parse_stations(data: Any) -> list[dict]:
    """River and sea level stations (groundwater and rainfall left out), compact."""
    items = data.get("items") if isinstance(data, dict) else None
    out = []
    seen = set()
    for s in items or []:
        if not isinstance(s, dict):
            continue
        ref = str(_first(s.get("stationReference")) or _first(s.get("notation")) or "").strip()
        if not STATION_ID.fullmatch(ref) or ref in seen:
            continue
        lat, lon = num(_first(s.get("lat"))), num(_first(s.get("long")))
        if lat is None or lon is None or not valid_lonlat(lon, lat):
            continue
        status = _last_segment(s.get("status"))
        if status in ("statusClosed", "statusSuspended"):
            continue
        best = None
        for m in s.get("measures") or []:
            if not isinstance(m, dict) or m.get("parameter") != "level":
                continue
            q = str(m.get("qualifier") or "")
            if q not in QUALIFIER_ORDER:
                continue
            mid = _last_segment(m.get("@id"))
            if not MEASURE_ID.fullmatch(mid):
                continue
            if best is None or QUALIFIER_ORDER[q] < QUALIFIER_ORDER[best[0]]:
                best = (q, mid, clean(m.get("unitName"), 12))
        if best is None:
            continue
        scale = s.get("stageScale") if isinstance(s.get("stageScale"), dict) else {}
        rloi = str(_first(s.get("RLOIid")) or "").strip()
        seen.add(ref)
        out.append({
            "ref": ref, "label": tidy_name(s.get("label")) or ref, "river": tidy_name(s.get("riverName")) or None,
            "town": tidy_name(s.get("town")) or None, "catchment": tidy_name(s.get("catchmentName")) or None,
            "lat": round(lat, 5), "lon": round(lon, 5), "rloi": rloi if rloi.isdigit() else None,
            "measure": best[1], "qualifier": best[0], "unit": best[2] or "m",
            "kind": "sea" if best[0] == "Tidal Level" else "river",
            "typical_high": num(scale.get("typicalRangeHigh")), "typical_low": num(scale.get("typicalRangeLow")),
            "recent_high": _reading(scale.get("highestRecent")), "record_high": _reading(scale.get("maxOnRecord")),
        })
    return out


def parse_readings(data: Any) -> dict[str, tuple[float, float | None]]:
    """{measure id: (value, epoch seconds)} from /data/readings?latest (bad values skipped)."""
    items = data.get("items") if isinstance(data, dict) else None
    out: dict[str, tuple[float, float | None]] = {}
    for r in items or []:
        if not isinstance(r, dict):
            continue
        mid = _last_segment(r.get("measure"))
        v = num(_first(r.get("value")))
        if not mid or v is None or abs(v) > 10000:
            continue
        t = uk_time(r.get("dateTime"))
        old = out.get(mid)
        if old is None or (t or 0) > (old[1] or 0):
            out[mid] = (round(v, 3), t)
    return out


def parse_history(data: Any) -> list[dict]:
    """[{t, v}] from a measure's readings, oldest first."""
    pts = {}
    for r in (data.get("items") if isinstance(data, dict) else None) or []:
        if not isinstance(r, dict):
            continue
        v = num(_first(r.get("value")))
        t = uk_time(r.get("dateTime"))
        if v is None or t is None or abs(v) > 10000:
            continue
        pts[int(t)] = round(v, 3)
    return [{"t": t, "v": pts[t]} for t in sorted(pts)]


def gauge_state(station: dict, value: float | None) -> str:
    if value is None:
        return "unknown"
    recent = (station.get("recent_high") or {}).get("value")
    high, low = station.get("typical_high"), station.get("typical_low")
    if recent is not None and high is not None and recent > high and value >= recent:
        return "highest"
    if high is not None and value > high:
        return "high"
    if low is not None and value < low:
        return "low"
    if high is not None:
        return "normal"
    return "unknown"


def trend(prev: tuple | None, cur: tuple | None) -> str | None:
    """"rising", "falling" or "steady" between two (value, time) readings, None when unknown."""
    if not prev or not cur or prev[1] is None or cur[1] is None or cur[1] <= prev[1]:
        return None
    if cur[1] - prev[1] > 6 * 3600:
        return None
    d = cur[0] - prev[0]
    if d > 0.01:
        return "rising"
    if d < -0.01:
        return "falling"
    return "steady"


def in_england(lat: float, lon: float) -> bool:
    w, s, e, n = ENGLAND_BBOX
    return w <= lon <= e and s <= lat <= n
