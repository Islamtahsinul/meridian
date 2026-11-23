"""Volcanoes: the Smithsonian Global Volcanism Program Holocene list (Volcanoes of the World,
via the GVP GeoServer WFS) as base points, plus current activity from USGS HANS (US volcano
alert levels), NASA EONET and GDACS.

GVP data: Global Volcanism Program, Smithsonian Institution (cite "Venzke, E. (ed.), Volcanoes of
the World"); free for non-commercial use with attribution. USGS HANS: US public domain.
"""

from __future__ import annotations

from .common import clean, integer, norm, num, parse_time, r5, valid_lonlat

WFS_URL = ("https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows?service=WFS&version=2.0.0"
           "&request=GetFeature&typeName=GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes"
           "&outputFormat=application/json")
GVP_PAGE = "https://volcano.si.edu/volcano.cfm?vn={vnum}"
HANS_ELEVATED = "https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes"
HANS_CAP = "https://volcanoes.usgs.gov/hans-public/api/volcano/getCapElevated"

HANS_COLOR = {"GREEN": 1, "YELLOW": 2, "ORANGE": 3, "RED": 4}
HANS_LEVEL_TEXT = {"NORMAL": "Normal", "ADVISORY": "Advisory", "WATCH": "Watch", "WARNING": "Warning"}


def parse_gvp(data: dict) -> list[dict]:
    out = []
    for f in (data or {}).get("features") or []:
        p = (f or {}).get("properties") or {}
        vnum = integer(p.get("Volcano_Number"))
        lat = num(p.get("Latitude"))
        lon = num(p.get("Longitude"))
        if lat is None or lon is None:
            c = ((f or {}).get("geometry") or {}).get("coordinates") or []
            if len(c) >= 2:
                lon, lat = num(c[0]), num(c[1])
        if vnum is None or lat is None or lon is None or not valid_lonlat(lon, lat):
            continue
        photo = p.get("Primary_Photo_Link")
        out.append({
            "id": f"gvp:{vnum}",
            "vnum": str(vnum),
            "name": clean(p.get("Volcano_Name"), 120),
            "lat": r5(lat), "lon": r5(lon),
            "elevation_m": integer(p.get("Elevation")),
            "type": clean(p.get("Primary_Volcano_Type"), 80) or None,
            "landform": clean(p.get("Volcanic_Landform"), 80) or None,
            "country": clean(p.get("Country"), 80) or None,
            "region": clean(p.get("Region"), 120) or None,
            "subregion": clean(p.get("Subregion"), 120) or None,
            "last_eruption": integer(p.get("Last_Eruption_Year")),
            "evidence": clean(p.get("Evidence_Category"), 80) or None,
            "rock": clean(p.get("Major_Rock_Type"), 80) or None,
            "tectonic": clean(p.get("Tectonic_Setting"), 120) or None,
            "summary": clean(p.get("Geological_Summary"), 4000) or None,
            "photo": photo if isinstance(photo, str) and photo.startswith("https://") else None,
            "photo_caption": clean(p.get("Primary_Photo_Caption"), 300) or None,
            "photo_credit": clean(p.get("Primary_Photo_Credit"), 200) or None,
        })
    return out


def eruption_label(year: int | None) -> str:
    if year is None:
        return "Unknown"
    if year < 0:
        return f"{-year:,} BCE"
    return str(year)


def parse_hans(elevated: list, cap: list | None = None) -> dict[str, dict]:
    """Elevated US volcanoes keyed by GVP number, with positions from the CAP list when present."""
    pos = {}
    for c in cap or []:
        if isinstance(c, dict) and c.get("vnum"):
            lat, lon = num(c.get("latitude")), num(c.get("longitude"))
            if lat is not None and lon is not None and valid_lonlat(lon, lat):
                pos[str(c["vnum"])] = (r5(lat), r5(lon), integer(c.get("elevation_meters")))
    out = {}
    for v in elevated or []:
        if not isinstance(v, dict) or not v.get("vnum"):
            continue
        vnum = str(v["vnum"])
        color = (v.get("color_code") or "").upper() or None
        level = (v.get("alert_level") or "").upper() or None
        p = pos.get(vnum)
        out[vnum] = {
            "vnum": vnum, "name": clean(v.get("volcano_name"), 120), "color": color,
            "level": HANS_LEVEL_TEXT.get(level, level.capitalize() if level else None),
            "sent": parse_time(v.get("sent_unixtime")) or parse_time(v.get("sent_utc")),
            "observatory": clean(v.get("obs_fullname"), 120) or None,
            "notice_url": v.get("notice_url") if str(v.get("notice_url") or "").startswith("https://") else None,
            "lat": p[0] if p else None, "lon": p[1] if p else None,
        }
    return out


def activity_rank(act: dict) -> int:
    """0 quiet .. 4 red, across HANS colour codes, GDACS alerts and EONET open events."""
    rank = 0
    hans = act.get("hans")
    if hans:
        rank = max(rank, HANS_COLOR.get(hans.get("color") or "", 1))
    gd = act.get("gdacs")
    if gd:
        rank = max(rank, {"Green": 2, "Orange": 3, "Red": 4}.get(gd.get("alert"), 2))
    if act.get("eonet"):
        rank = max(rank, 2)
    return rank


def match_by_name(name: str, candidates: list[dict], lat: float | None = None, lon: float | None = None,
                  max_km: float = 60.0) -> dict | None:
    """The GVP volcano for an activity report, by normalised name then by distance."""
    from meridian import geo
    key = norm(name)
    for v in candidates:
        if key and (norm(v["name"]) == key or (len(key) >= 4 and key in norm(v["name"]))):
            if lat is None or geo.haversine_km(lat, lon, v["lat"], v["lon"]) < 300:
                return v
    if lat is not None and lon is not None:
        best = None
        for v in candidates:
            d = geo.haversine_km(lat, lon, v["lat"], v["lon"])
            if d <= max_km and (best is None or d < best[0]):
                best = (d, v)
        return best[1] if best else None
    return None


def clean_report_name(text: str) -> str:
    """"Nevados del Chillan Volcano, Chile" -> "Nevados del Chillan"; "Eruption  Kanlaon" -> "Kanlaon"."""
    s = (text or "").strip()
    s = s.split(",")[0]
    for word in ("Eruption", "Volcano", "volcano", "Volcanic activity at", "Activity at"):
        s = s.replace(word, " ")
    return " ".join(s.split())
