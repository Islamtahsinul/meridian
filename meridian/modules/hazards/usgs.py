"""USGS earthquake feeds (GeoJSON summary feeds, event detail products, FDSN event queries).

Public domain (US Government). Feeds: https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php
"""

from __future__ import annotations

from .common import clean, integer, num, parse_time, r5, valid_lonlat

BASE = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
FEEDS = {
    "day": BASE + "all_day.geojson",            # every magnitude, past 24 h
    "week": BASE + "2.5_week.geojson",          # M2.5+, past 7 days
    "month": BASE + "significant_month.geojson",  # significant events, past 30 days
}
DETAIL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/detail/{id}.geojson"
FDSN = "https://earthquake.usgs.gov/fdsnws/event/1/query"
EVENT_PAGE = "https://earthquake.usgs.gov/earthquakes/eventpage/{id}"


def parse_feature(f: dict) -> dict | None:
    if not isinstance(f, dict):
        return None
    p = f.get("properties") or {}
    coords = (f.get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2 or not valid_lonlat(coords[0], coords[1]):
        return None
    t = parse_time(p.get("time"))
    native = f.get("id") or f"{p.get('net') or ''}{p.get('code') or ''}"
    if not native or t is None:
        return None
    depth = num(coords[2]) if len(coords) > 2 else None
    alert = (p.get("alert") or "").lower() or None
    return {
        "id": f"usgs:{native}",
        "native_id": native,
        "mag": num(p.get("mag")),
        "mag_type": p.get("magType") or None,
        "place": clean(p.get("place"), 200) or "Unknown location",
        "time": t,
        "updated": parse_time(p.get("updated")) or t,
        "lat": r5(coords[1]),
        "lon": r5(coords[0]),
        "depth_km": round(depth, 1) if depth is not None else None,
        "felt": integer(p.get("felt")),
        "cdi": num(p.get("cdi")),
        "mmi": num(p.get("mmi")),
        "alert": alert if alert in ("green", "yellow", "orange", "red") else None,
        "tsunami": bool(integer(p.get("tsunami"), 0)),
        "sig": integer(p.get("sig"), 0),
        "status": p.get("status") or None,
        "net": p.get("net") or None,
        "type": p.get("type") or "earthquake",
        "url": p.get("url") or EVENT_PAGE.format(id=native),
        "gap": num(p.get("gap")),
        "rms": num(p.get("rms")),
        "nst": integer(p.get("nst")),
        "dmin": num(p.get("dmin")),
        "sources": [s for s in (p.get("sources") or "").split(",") if s],
        "products": [s for s in (p.get("types") or "").split(",") if s],
    }


def parse_feed(data: dict) -> list[dict]:
    out = []
    for f in (data or {}).get("features") or []:
        q = parse_feature(f)
        if q:
            out.append(q)
    return out


def merge(*lists: list[dict]) -> dict[str, dict]:
    """One record per event id; the most recently updated copy wins."""
    merged: dict[str, dict] = {}
    for items in lists:
        for q in items or []:
            old = merged.get(q["id"])
            if old is None or (q.get("updated") or 0) >= (old.get("updated") or 0):
                merged[q["id"]] = q
    return merged


def _first(products: dict, name: str) -> dict | None:
    items = products.get(name)
    return items[0] if isinstance(items, list) and items else None


def parse_detail(data: dict) -> dict:
    """Card extras from the event detail feed: PAGER, ShakeMap image, DYFI, moment tensor."""
    props = (data or {}).get("properties") or {}
    products = props.get("products") or {}
    out: dict = {"products": sorted(products.keys())}
    pager = _first(products, "losspager")
    if pager:
        pp = pager.get("properties") or {}
        out["pager"] = {"alert": (pp.get("alertlevel") or "").lower() or None, "max_mmi": num(pp.get("maxmmi"))}
    shake = _first(products, "shakemap")
    if shake:
        sp = shake.get("properties") or {}
        contents = shake.get("contents") or {}
        img = (contents.get("download/intensity.jpg") or {}).get("url")
        out["shakemap"] = {"max_mmi": num(sp.get("maxmmi")), "max_pga": num(sp.get("maxpga")),
                           "image": img if isinstance(img, str) and img.startswith("https://") else None,
                           "status": sp.get("map-status")}
    dyfi = _first(products, "dyfi")
    if dyfi:
        dp = dyfi.get("properties") or {}
        out["dyfi"] = {"responses": integer(dp.get("numResp") or dp.get("num-responses")),
                       "max_cdi": num(dp.get("maxmmi"))}
    mt = _first(products, "moment-tensor")
    if mt:
        mp = mt.get("properties") or {}
        out["moment_tensor"] = {
            "magnitude": num(mp.get("derived-magnitude")), "type": mp.get("derived-magnitude-type"),
            "depth_km": num(mp.get("derived-depth")),
            "strike": num(mp.get("nodal-plane-1-strike")), "dip": num(mp.get("nodal-plane-1-dip")),
            "rake": num(mp.get("nodal-plane-1-rake"))}
    origin = _first(products, "origin")
    if origin:
        op = origin.get("properties") or {}
        out["origin"] = {"review": op.get("review-status") or op.get("evaluation-status"),
                         "horizontal_error_km": num(op.get("horizontal-error")),
                         "depth_type": op.get("depth-type")}
    for name in ("general-text", "impact-text"):
        txt = _first(products, name)
        if txt:
            body = ((txt.get("contents") or {}).get("") or {}).get("bytes")
            if isinstance(body, str) and body.strip():
                out.setdefault("texts", []).append(clean(body, 600))
    return out


def fault_kind(rake: float | None) -> str | None:
    """Rough faulting style from the rake of a nodal plane."""
    if rake is None:
        return None
    r = ((rake + 180.0) % 360.0) - 180.0
    if 45 <= r <= 135:
        return "Reverse (thrust) faulting"
    if -135 <= r <= -45:
        return "Normal faulting"
    return "Strike-slip faulting"
