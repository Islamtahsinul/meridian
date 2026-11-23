"""NASA EONET v3 natural events (wildfires, severe storms, volcanoes, floods, ice, dust, ...).

NASA open data: https://eonet.gsfc.nasa.gov/docs/v3
"""

from __future__ import annotations

import re

from .common import clean, geometry_center, num, parse_time, r5, valid_lonlat

API = "https://eonet.gsfc.nasa.gov/api/v3/events"
CATEGORIES = {
    "wildfires": ("Wildfire", "fire"),
    "severeStorms": ("Severe storm", "storm"),
    "volcanoes": ("Volcano", "volcano"),
    "floods": ("Flood", "flood"),
    "seaLakeIce": ("Sea and lake ice", "disaster"),
    "landslides": ("Landslide", "disaster"),
    "dustHaze": ("Dust and haze", "disaster"),
    "drought": ("Drought", "disaster"),
    "snow": ("Snow", "disaster"),
    "tempExtremes": ("Temperature extreme", "disaster"),
    "earthquakes": ("Earthquake", "earthquake"),
    "manmade": ("Manmade event", "disaster"),
    "waterColor": ("Water colour", "disaster"),
}
NON_FIRE = ",".join(c for c in CATEGORIES if c != "wildfires")
SOURCE_NAMES = {
    "IRWIN": "IRWIN (US wildfire incidents)", "GDACS": "GDACS", "JTWC": "Joint Typhoon Warning Center",
    "NOAA_NHC": "NOAA National Hurricane Center", "SIVolcano": "Smithsonian Global Volcanism Program",
    "NATICE": "US National Ice Center", "BYU_ICE": "BYU Antarctic iceberg tracking", "EO": "NASA Earth Observatory",
    "CEMS": "Copernicus Emergency Management", "ReliefWeb": "ReliefWeb", "InciWeb": "InciWeb", "PDC": "Pacific Disaster Center",
    "IDC": "International Charter", "NASA_DISP": "NASA Disasters Program", "Earthdata": "NASA Earthdata",
    "MRR": "LANCE rapid response", "AVO": "Alaska Volcano Observatory", "HDDS": "USGS HDDS", "CALFIRE": "CAL FIRE",
    "BCWILDFIRE": "BC Wildfire Service", "ABFIRE": "Alberta Wildfire", "AU_BOM": "Australian Bureau of Meteorology",
    "UNISYS": "Unisys Weather", "USGS_EHP": "USGS Earthquake Hazards", "USGS_CMT": "USGS",
}
_VNUM = re.compile(r"[?&]vn=(\d{6})")


def parse_events(data: dict) -> list[dict]:
    out = []
    for e in (data or {}).get("events") or []:
        item = parse_event(e)
        if item:
            out.append(item)
    return out


def parse_event(e: dict) -> dict | None:
    if not isinstance(e, dict) or not e.get("id"):
        return None
    cats = e.get("categories") or []
    cat = (cats[0].get("id") if cats and isinstance(cats[0], dict) else None) or "manmade"
    label, kind = CATEGORIES.get(cat, (cats[0].get("title") if cats else "Event", "disaster"))
    track = []
    for g in e.get("geometry") or []:
        if not isinstance(g, dict):
            continue
        t = parse_time(g.get("date"))
        if g.get("type") == "Point":
            c = g.get("coordinates") or []
            if len(c) < 2 or not valid_lonlat(c[0], c[1]):
                continue
            lon, lat = float(c[0]), float(c[1])
        else:
            center = geometry_center({"type": g.get("type"), "coordinates": g.get("coordinates")})
            if not center or not valid_lonlat(*center):
                continue
            lon, lat = center
        track.append({"t": t, "lat": r5(lat), "lon": r5(lon), "mag": num(g.get("magnitudeValue")),
                      "unit": g.get("magnitudeUnit") or None})
    if not track:
        return None
    track.sort(key=lambda p: p["t"] or 0)
    last = track[-1]
    sources = []
    vnum = None
    for s in e.get("sources") or []:
        if isinstance(s, dict) and s.get("url"):
            sources.append({"id": s.get("id"), "name": SOURCE_NAMES.get(s.get("id"), s.get("id")), "url": s["url"]})
            m = _VNUM.search(s["url"])
            if m:
                vnum = m.group(1)
    mags = [p["mag"] for p in track if p["mag"] is not None]
    return {
        "id": f"eonet:{e['id']}",
        "native_id": e["id"],
        "title": clean(e.get("title"), 160) or label,
        "category": cat,
        "category_label": label,
        "kind": kind,
        "description": clean(e.get("description"), 600) or None,
        "closed": parse_time(e.get("closed")),
        "link": e.get("link"),
        "sources": sources,
        "vnum": vnum,
        "track": track,
        "lat": last["lat"],
        "lon": last["lon"],
        "time": last["t"],
        "start": track[0]["t"],
        "magnitude": last["mag"],
        "magnitude_max": max(mags) if mags else None,
        "unit": last["unit"],
    }


def unit_label(unit: str | None) -> str:
    return {"kts": "kn", "acres": "acres", "NM^2": "NM²", "km^2": "km²", "hectares": "ha"}.get(unit or "", unit or "")
