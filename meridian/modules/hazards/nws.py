"""US National Weather Service active alerts (api.weather.gov, US public domain).

Storm-based warnings carry their own polygons; most alerts only list forecast, county or marine
zones. Zone outlines are fetched once each from the same API and kept in SQLite.
"""

from __future__ import annotations

import re

from .common import SEVERITY_RANK, clean, clip, geometry_bbox, geometry_center, parse_time, simplify_geometry

ALERTS_URL = "https://api.weather.gov/alerts/active"
ZONE_URL = "https://api.weather.gov/zones/{type}/{id}"
HEADERS = {"Accept": "application/geo+json"}
_ZONE = re.compile(r"/zones/(forecast|county|fire|public|coastal|offshore|marine)/([A-Z]{2}[CZ]\d{3})$")
# The US, its territories and coastal waters, roughly (for "is this point NWS country?").
COVERAGE = [(-180.0, 15.0, -60.0, 72.0), (130.0, 5.0, 180.0, 25.0), (-172.0, -16.0, -168.0, -10.0),
            (-68.5, 17.5, -64.0, 19.0)]


def zone_ref(url: str) -> tuple[str, str] | None:
    m = _ZONE.search(url or "")
    if not m:
        return None
    kind = m.group(1)
    if kind in ("public", "coastal", "offshore", "marine"):
        kind = "forecast"
    return kind, m.group(2)


def in_coverage(lat: float, lon: float) -> bool:
    return any(w <= lon <= e and s <= lat <= n for w, s, e, n in COVERAGE)


def _first(params: dict, name: str) -> str | None:
    v = params.get(name)
    if isinstance(v, list) and v:
        return clean(v[0], 200) or None
    return None


_LINE_START = re.compile(r"^([*.\-\u2022]|[A-Z][A-Z /]{3,}\.\.\.)")


def unwrap(text: str | None, limit: int) -> str:
    """NWS products are hard-wrapped at about 69 characters: join wrapped lines back into
    sentences, keep paragraph breaks and the "* WHAT..." / ".TONIGHT..." lines."""
    if not text:
        return ""
    paras = re.split(r"\n\s*\n", str(text).replace("\r", "").strip())
    out = []
    for para in paras:
        merged: list[str] = []
        for line in (x.strip() for x in para.splitlines()):
            if not line:
                continue
            if merged and not _LINE_START.match(line):
                merged[-1] += " " + line
            else:
                merged.append(line)
        if merged:
            out.append("\n".join(merged))
    return clip("\n\n".join(out), limit)


def parse_alert(f: dict, now: float) -> dict | None:
    p = (f or {}).get("properties") or {}
    aid = p.get("id") or (f or {}).get("id")
    if not aid or (p.get("status") or "").lower() != "actual":
        return None
    expires = parse_time(p.get("ends")) or parse_time(p.get("expires"))
    if expires and expires < now:
        return None
    if (p.get("messageType") or "").lower() == "cancel":
        return None
    geom = simplify_geometry((f or {}).get("geometry"), 0.003, 4)
    zones = []
    for z in p.get("affectedZones") or []:
        ref = zone_ref(z)
        if ref and ref not in zones:
            zones.append(ref)
    params = p.get("parameters") or {}
    native = aid.split("urn:oid:", 1)[-1]
    center = geometry_center(geom) if geom else None
    return {
        "id": f"nws:{native}",
        "native_id": aid,
        "source": "nws",
        "event": clean(p.get("event"), 120) or "Weather alert",
        "headline": clean(p.get("headline"), 300) or None,
        "nws_headline": _first(params, "NWSheadline"),
        "severity": (p.get("severity") or "Unknown").capitalize(),
        "certainty": p.get("certainty") or None,
        "urgency": p.get("urgency") or None,
        "category": p.get("category") or None,
        "response": p.get("response") or None,
        "message_type": p.get("messageType") or None,
        "sender": clean(p.get("senderName"), 120) or None,
        "area": clean(p.get("areaDesc"), 600) or None,
        "sent": parse_time(p.get("sent")),
        "onset": parse_time(p.get("onset")) or parse_time(p.get("effective")),
        "expires": expires,
        "description": unwrap(p.get("description"), 4000) or None,
        "instruction": unwrap(p.get("instruction"), 2000) or None,
        "max_wind_gust": _first(params, "maxWindGust"),
        "max_hail_size": _first(params, "maxHailSize"),
        "tornado": _first(params, "tornadoDetection"),
        "motion": _first(params, "eventMotionDescription"),
        "geometry": geom,
        "bbox": geometry_bbox(geom) if geom else None,
        "lat": round(center[1], 5) if center else None,
        "lon": round(center[0], 5) if center else None,
        "zones": zones,
        "api_url": aid if aid.startswith("https://") else f"https://api.weather.gov/alerts/{aid}",
        "country": "US",
    }


def parse_alerts(data: dict, now: float) -> list[dict]:
    out = []
    for f in (data or {}).get("features") or []:
        a = parse_alert(f, now)
        if a:
            out.append(a)
    return out


def parse_zone(data: dict) -> dict | None:
    p = (data or {}).get("properties") or {}
    zid = p.get("id")
    if not zid:
        return None
    fine = simplify_geometry((data or {}).get("geometry"), 0.004, 4)
    coarse = simplify_geometry((data or {}).get("geometry"), 0.03, 3) if fine else None
    center = geometry_center(fine) if fine else None
    return {"id": zid, "name": clean(p.get("name"), 120) or zid, "state": p.get("state") or None,
            "fine": fine, "coarse": coarse or fine, "bbox": geometry_bbox(fine) if fine else None,
            "lat": round(center[1], 4) if center else None, "lon": round(center[0], 4) if center else None}


def forecast_page(lat: float, lon: float) -> str:
    """weather.gov point forecast page, which lists the hazards in force at that point."""
    return f"https://forecast.weather.gov/MapClick.php?lat={lat:.4f}&lon={lon:.4f}"


def rank(severity: str | None) -> int:
    return SEVERITY_RANK.get((severity or "").lower(), 0)
