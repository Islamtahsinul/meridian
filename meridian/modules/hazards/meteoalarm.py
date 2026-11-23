"""MeteoAlarm (EUMETNET): official weather warnings for 38 European countries, from the public
Atom/CAP feeds. Licensed under terms equivalent to CC BY 4.0 with MeteoAlarm's redistribution
terms (credit MeteoAlarm and the issuing national service).

Some services put polygons in their feeds (UK, Norway, Switzerland); the rest name regions by
EMMA_ID or NUTS code. Region outlines come from MeteoAlarm's published geocodes file.
"""

from __future__ import annotations

import csv
import io
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .common import (clean, geometry_bbox, geometry_center, parse_time, simplify_geometry, strip_html,
                     valid_lonlat)

FEED_URL = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-{slug}"
DOCS_TREE = "https://gitlab.com/api/v4/projects/meteoalarm-pm-group%2Fdocuments/repository/tree?per_page=100"
DOCS_RAW = "https://gitlab.com/meteoalarm-pm-group/documents/-/raw/master/{name}"
WEB_REGION = "https://meteoalarm.org/en/live/region/{iso2}"
CAP_HOSTS = ("feeds.meteoalarm.org",)
_URL_BAD = re.compile(r"[\x00-\x20\x7f\\]")


def cap_url_ok(value) -> bool:
    """A warning's CAP link comes from the feed and is fetched by the server when its card opens, so
    it must be https on MeteoAlarm's feed host: never an internal address or another site."""
    if not isinstance(value, str) or not value or len(value) > 1000 or _URL_BAD.search(value):
        return False
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme.lower() == "https" and "@" not in parts.netloc and port in (None, 443)
            and (parts.hostname or "").lower() in CAP_HOSTS)

# feed slug -> (ISO 3166-1 alpha-2, display name)
COUNTRIES = {
    "andorra": ("AD", "Andorra"), "austria": ("AT", "Austria"), "belgium": ("BE", "Belgium"),
    "bosnia-herzegovina": ("BA", "Bosnia and Herzegovina"), "bulgaria": ("BG", "Bulgaria"),
    "croatia": ("HR", "Croatia"), "cyprus": ("CY", "Cyprus"), "czechia": ("CZ", "Czechia"),
    "denmark": ("DK", "Denmark"), "estonia": ("EE", "Estonia"), "finland": ("FI", "Finland"),
    "france": ("FR", "France"), "germany": ("DE", "Germany"), "greece": ("GR", "Greece"),
    "hungary": ("HU", "Hungary"), "iceland": ("IS", "Iceland"), "ireland": ("IE", "Ireland"),
    "israel": ("IL", "Israel"), "italy": ("IT", "Italy"), "latvia": ("LV", "Latvia"),
    "lithuania": ("LT", "Lithuania"), "luxembourg": ("LU", "Luxembourg"), "malta": ("MT", "Malta"),
    "moldova": ("MD", "Moldova"), "montenegro": ("ME", "Montenegro"), "netherlands": ("NL", "Netherlands"),
    "norway": ("NO", "Norway"), "poland": ("PL", "Poland"), "portugal": ("PT", "Portugal"),
    "republic-of-north-macedonia": ("MK", "North Macedonia"), "romania": ("RO", "Romania"),
    "serbia": ("RS", "Serbia"), "slovakia": ("SK", "Slovakia"), "slovenia": ("SI", "Slovenia"),
    "spain": ("ES", "Spain"), "sweden": ("SE", "Sweden"), "switzerland": ("CH", "Switzerland"),
    "ukraine": ("UA", "Ukraine"), "united-kingdom": ("GB", "United Kingdom"),
}
BY_ISO = {iso: slug for slug, (iso, _) in COUNTRIES.items()}

NS_ATOM = "{http://www.w3.org/2005/Atom}"
NS_CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"
_GEOCODES_FILE = re.compile(r"^MeteoAlarm_Geocodes_(\d{4})_(\d{2})_(\d{2})\.json$")
_ALIASES_FILE = re.compile(r"^MeteoAlarm_Geocodes-Aliases_(\d{4})_(\d{2})_(\d{2})\.csv$")
_LEVEL = re.compile(r"\b(yellow|orange|red|green)\b", re.I)


def countries_from_setting(text: str | None) -> list[str]:
    if not text or text.strip().lower() in ("all", "*"):
        return list(COUNTRIES)
    out = []
    for part in text.split(","):
        key = part.strip().lower().replace(" ", "-")
        if key in COUNTRIES:
            out.append(key)
        elif key.upper() in BY_ISO:
            out.append(BY_ISO[key.upper()])
    return list(dict.fromkeys(out)) or list(COUNTRIES)


def parse_polygon(text: str) -> list | None:
    """CAP "lat,lon lat,lon ..." -> a closed GeoJSON ring of [lon, lat]."""
    pts = []
    for pair in (text or "").split():
        bits = pair.split(",")
        if len(bits) != 2:
            continue
        try:
            lat, lon = float(bits[0]), float(bits[1])
        except ValueError:
            continue
        if valid_lonlat(lon, lat):
            pts.append([lon, lat])
    if len(pts) < 3:
        return None
    if pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    return pts if len(pts) >= 4 else None


def _text(entry, tag: str) -> str:
    el = entry.find(NS_CAP + tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def awareness_level(event: str, severity: str) -> str:
    m = _LEVEL.search(event or "")
    if m:
        return m.group(1).lower()
    return {"minor": "green", "moderate": "yellow", "severe": "orange", "extreme": "red"}.get(
        (severity or "").lower(), "yellow")


def parse_atom(text: str, slug: str, now: float) -> list[dict]:
    """Warnings of one country feed; expired and cancelled ones are dropped."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    iso2, cname = COUNTRIES.get(slug, ("", slug))
    out = []
    for entry in root.iter(NS_ATOM + "entry"):
        status = _text(entry, "status").lower()
        mtype = _text(entry, "message_type").lower()
        if status and status != "actual":
            continue
        if mtype == "cancel":
            continue
        expires = parse_time(_text(entry, "expires"))
        if expires and expires < now:
            continue
        polygons = []
        for el in entry.findall(NS_CAP + "polygon"):
            ring = parse_polygon(el.text or "")
            if ring:
                polygons.append(ring)
        codes = []
        for el in entry.findall(NS_CAP + "geocode"):
            name = value = ""
            for child in el:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "valueName":
                    name = (child.text or "").strip()
                elif tag == "value":
                    value = (child.text or "").strip()
            if value:
                codes.append((name or "EMMA_ID", value))
        web = None
        cap_link = None
        for link in entry.findall(NS_ATOM + "link"):
            href = link.get("href") or ""
            if link.get("type") == "application/cap+xml":
                cap_link = href
            elif link.get("hreflang") and "meteoalarm.org?" in href and not link.get("rel"):
                web = href
        entry_id = (entry.findtext(NS_ATOM + "id") or "").strip()
        m = re.search(r"/([0-9a-f-]{36})\?index_info=(\d+)&index_area=(\d+)&index_(polygon|geocode)=(\d+)", entry_id)
        if m:
            wid = f"meteoalarm:{m.group(1)}.{m.group(2)}.{m.group(3)}.{m.group(4)[0]}{m.group(5)}"
        else:
            ident = _text(entry, "identifier") or entry_id
            if not ident:
                continue
            wid = "meteoalarm:" + re.sub(r"[^A-Za-z0-9._-]", "", ident)[:120] + "." + str(len(out))
        event = clean(_text(entry, "event"), 120) or "Weather warning"
        severity = (_text(entry, "severity") or "Unknown").capitalize()
        out.append({
            "id": wid,
            "source": "meteoalarm",
            "country": iso2,
            "country_name": cname,
            "feed": slug,
            "event": event[:1].upper() + event[1:],
            "level": awareness_level(event, severity),
            "headline": clean(entry.findtext(NS_ATOM + "title"), 300) or None,
            "severity": severity,
            "certainty": _text(entry, "certainty") or None,
            "urgency": _text(entry, "urgency") or None,
            "message_type": _text(entry, "message_type") or None,
            "area": clean(_text(entry, "areaDesc"), 300) or None,
            "sent": parse_time(_text(entry, "sent")),
            "onset": parse_time(_text(entry, "onset")) or parse_time(_text(entry, "effective")),
            "expires": expires,
            "identifier": _text(entry, "identifier") or None,
            "polygons": polygons,
            "codes": codes,
            "web": web if web and web.startswith("https://") else None,
            "cap_url": cap_link if cap_url_ok(cap_link) else None,
            "sender": f"MeteoAlarm, {cname}",
        })
    return out


def latest_files(tree: list) -> tuple[str | None, str | None]:
    """Newest geocodes JSON and aliases CSV names from the GitLab tree listing."""
    best_geo = best_alias = None
    for item in tree or []:
        name = (item or {}).get("name") or ""
        g = _GEOCODES_FILE.match(name)
        a = _ALIASES_FILE.match(name)
        if g and (best_geo is None or g.groups() > best_geo[0]):
            best_geo = (g.groups(), name)
        if a and (best_alias is None or a.groups() > best_alias[0]):
            best_alias = (a.groups(), name)
    return (best_geo[1] if best_geo else None), (best_alias[1] if best_alias else None)


def build_regions(geocodes: dict, aliases_csv: str | None) -> dict:
    """Simplified region outlines keyed by EMMA_ID, plus alias codes (NUTS etc.) -> EMMA_ID."""
    regions = {}
    for f in (geocodes or {}).get("features") or []:
        p = (f or {}).get("properties") or {}
        code = (p.get("code") or "").strip()
        geom = (f or {}).get("geometry")
        if not code or not geom:
            continue
        fine = simplify_geometry(geom, 0.006, 4)
        if not fine:
            continue
        coarse = simplify_geometry(geom, 0.04, 3) or fine
        center = geometry_center(fine)
        regions[code] = {"name": clean(p.get("name"), 120) or code, "country": p.get("country") or code[:2],
                         "fine": fine, "coarse": coarse, "bbox": geometry_bbox(fine),
                         "lat": round(center[1], 4) if center else None, "lon": round(center[0], 4) if center else None}
    aliases = {}
    if aliases_csv:
        for row in csv.DictReader(io.StringIO(aliases_csv)):
            code = (row.get("CODE") or "").strip()
            alias = (row.get("ALIAS_CODE") or "").strip()
            if code and alias and code in regions:
                aliases[alias] = code
    return {"regions": regions, "aliases": aliases}


def parse_cap(text: str) -> dict:
    """Description, instruction and issuer from one CAP message (English when offered)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    infos = root.findall(NS_CAP + "info")
    if not infos:
        return {}

    def lang(i):
        return (i.findtext(NS_CAP + "language") or "").lower()

    info = next((i for i in infos if lang(i).startswith("en")), infos[0])
    params = {}
    for p in info.findall(NS_CAP + "parameter"):
        name = (p.findtext(NS_CAP + "valueName") or "").strip()
        value = (p.findtext(NS_CAP + "value") or "").strip()
        if name and value:
            params[name] = value
    web = (info.findtext(NS_CAP + "web") or "").strip()
    kind = params.get("awareness_type", "")
    return {
        "headline": clean(info.findtext(NS_CAP + "headline"), 300) or None,
        "description": strip_html(info.findtext(NS_CAP + "description"), 3000) or None,
        "instruction": strip_html(info.findtext(NS_CAP + "instruction"), 2000) or None,
        "issuer": clean(info.findtext(NS_CAP + "senderName"), 120) or None,
        "web": web if web.startswith("https://") else None,
        "hazard": kind.split(";", 1)[-1].strip().replace("-", " ").capitalize() if kind else None,
        "language": lang(info) or None,
    }
