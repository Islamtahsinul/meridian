"""Timeline replay: scrub or play back the last 72 hours of aircraft, ships and events.

The time bar and the replay layers live in the browser (``web/js/layers/timeline.js``). They read
the core event history (``/api/core/history/*``, CONTRACT 15.3) and the sampled positions the
flights and ships modules keep for replay (``/api/flights/history``, ``/api/ships/history``,
CONTRACT 15.4). This half adds the ``history_near`` AI tool. It polls nothing and registers no
events provider (replayed events must not feed back into convergences).
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from meridian import geo
from meridian import history as core_history

RADIUS_DEFAULT_KM = 100.0
RADIUS_MAX_KM = 1000.0
SPAN_DEFAULT_S = 24 * 3600
SPAN_MAX_S = 30 * 86400          # the core keeps 30 days of events
LIMIT_DEFAULT = 25
LIMIT_MAX = 50
SCAN_MAX = 5000                  # events fetched from the box before the distance filter
EXTRA_KEYS = 6                   # small provider fields kept per event (magnitude, level, ...)

_BASE_KEYS = ("module", "kind", "id", "title", "subtitle", "lat", "lon", "layer", "time", "first_time", "severity",
              "first_seen", "last_seen")


def parse_time(value, name: str) -> float | None:
    """Epoch seconds from a number (seconds or milliseconds), a numeric string or an ISO 8601 time
    (UTC when no zone is given). None stays None; anything else raises ValueError."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a time")
    if isinstance(value, (int, float)):
        return core_history.epoch_seconds(value, name)
    if isinstance(value, str):
        text = value.strip()
        try:
            return core_history.epoch_seconds(float(text), name)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise ValueError(f"{name} must be epoch seconds or an ISO 8601 time like 2026-11-23T14:00:00Z") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError(f"{name} must be a time")


def _number(args: dict, key: str, lo: float, hi: float, default):
    value = args.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if not math.isfinite(f) or not lo <= f <= hi:
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
    return f


def _iso(t: float | None) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compact(ev: dict, dist: float) -> dict:
    out = {k: ev.get(k) for k in _BASE_KEYS if ev.get(k) not in (None, "")}
    out["distance_km"] = round(dist, 1)
    out["time_utc"] = _iso(ev.get("time"))
    if ev.get("first_time") is not None and ev.get("first_time") != ev.get("time"):
        out["first_time_utc"] = _iso(ev.get("first_time"))
    extra = 0
    for k, v in ev.items():
        if k in out or k in _BASE_KEYS or extra >= EXTRA_KEYS:
            continue
        if isinstance(v, bool) or isinstance(v, (int, float)) or (isinstance(v, str) and len(v) <= 120):
            out[k] = v
            extra += 1
    return out


def history_near(app, args: dict, *, now: float | None = None) -> dict:
    """Events Meridian recorded near a point in a time window (CONTRACT 15.3 event history)."""
    args = args or {}
    lat = _number(args, "lat", -90.0, 90.0, None)
    lon = _number(args, "lon", -180.0, 180.0, None)
    if lat is None or lon is None:
        raise ValueError("give lat and lon")
    radius = _number(args, "radius_km", 1.0, RADIUS_MAX_KM, RADIUS_DEFAULT_KM)
    limit = int(_number(args, "limit", 1, LIMIT_MAX, LIMIT_DEFAULT))
    min_sev = _number(args, "min_severity", 0.0, 10.0, None)
    now = time.time() if now is None else now
    until = parse_time(args.get("to"), "to")
    since = parse_time(args.get("from"), "from")
    if until is None:
        until = now if since is None else min(now, since + SPAN_DEFAULT_S)
    if since is None:
        since = until - SPAN_DEFAULT_S
    if until < since:
        raise ValueError("to must not be before from")
    if until - since > SPAN_MAX_S:
        raise ValueError("ask for at most 30 days at a time")
    kinds = core_history.clean_kinds(args.get("kinds"))
    box = geo.bbox_of_circle(lat, lon, radius)
    items = app.history.events(since, until, kinds=kinds, bbox=box, min_severity=min_sev, limit=SCAN_MAX)
    near = []
    for ev in items:
        try:
            d = geo.haversine_km(lat, lon, float(ev["lat"]), float(ev["lon"]))
        except (TypeError, ValueError, KeyError):
            continue
        if d <= radius:
            near.append((d, ev))
    by_kind: dict[str, dict] = {}
    for _d, ev in near:
        k = by_kind.setdefault(ev.get("kind") or "other", {"count": 0, "max_severity": 0})
        k["count"] += 1
        k["max_severity"] = max(k["max_severity"], ev.get("severity") or 0)
    near.sort(key=lambda p: (-(p[1].get("severity") or 0), -(p[1].get("time") or 0), p[0]))
    return {
        "center": [round(lat, 5), round(lon, 5)], "radius_km": radius,
        "from": round(since), "to": round(until), "from_utc": _iso(since), "to_utc": _iso(until),
        "count": len(near), "truncated": len(items) >= SCAN_MAX,
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1]["count"])),
        "events": [_compact(ev, d) for d, ev in near[:limit]],
        "note": "Events Meridian recorded in its event history (a snapshot every 5 minutes while it runs, kept "
                "30 days), most severe first. An event matches when its active span overlaps the window. "
                "Nothing is known for times Meridian was not running.",
    }


def register(m) -> None:
    m.ai_tool(
        "history_near",
        "Events Meridian recorded near a place during a past time window (earthquakes, fires, storms, warnings, "
        "conflict events, news, aircraft emergencies, dark or sanctioned ships, GPS interference and more), "
        "most severe first, with counts by kind. Use it for questions like 'what happened near Gdansk "
        "yesterday afternoon?'. Times are UTC.",
        {"type": "object", "properties": {
            "lat": {"type": "number", "description": "Latitude in decimal degrees (-90 to 90)."},
            "lon": {"type": "number", "description": "Longitude in decimal degrees (-180 to 180)."},
            "from": {"type": "string", "description": "Start of the window: an ISO 8601 UTC time such as "
                                                      "2026-11-23T14:00:00Z, or epoch seconds. Default: 24 hours "
                                                      "before 'to'."},
            "to": {"type": "string", "description": "End of the window, same forms. Default: now (or 24 hours "
                                                    "after 'from')."},
            "radius_km": {"type": "number", "description": "Search radius in km, 1 to 1000 (default 100)."},
            "kinds": {"type": "array", "items": {"type": "string"},
                      "description": "Optional entity kinds, for example earthquake, fire, conflict_event, news."},
            "min_severity": {"type": "number", "description": "Lowest severity to include, 0 to 10."},
            "limit": {"type": "integer", "description": "Maximum events, 1 to 50 (default 25)."}},
         "required": ["lat", "lon"], "additionalProperties": False},
        lambda args: history_near(m.app, args))
