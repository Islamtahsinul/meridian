"""Trafikverket (Swedish Transport Administration) road cameras, from the open Trafikinfo API.

Needs a free key (``TRAFIKVERKET_KEY``) from Trafikverket's data exchange portal,
https://data.trafikverket.se/ (register, then create a key; guide: https://data.trafikverket.se/get-started).
Without it the source is listed as needing a key. The camera data set ("Camera": road condition
cameras from the VViS road weather system and traffic flow cameras) is published under CC0 1.0.

The API only answers POST queries. The key travels in the query body (never in a URL, a file name
or the raw cache, which holds only the answer).
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

from ..common import FetchContext, NeedsKey, Source, clean, make_camera, parse_json_bytes, read_bytes, to_float
from .eu_common import post_cached

KEY = "TRAFIKVERKET_KEY"
API_URL = "https://api.trafikinfo.trafikverket.se/v2/data.json"
QUERY = ('<REQUEST><LOGIN authenticationkey="{key}"/>'
         '<QUERY objecttype="Camera" schemaversion="1" limit="10000">'
         '<FILTER><EQ name="Active" value="true"/><EQ name="Deleted" value="false"/></FILTER>'
         '</QUERY></REQUEST>')
COUNTIES = {1: "Stockholm", 3: "Uppsala", 4: "Södermanland", 5: "Östergötland", 6: "Jönköping", 7: "Kronoberg",
            8: "Kalmar", 9: "Gotland", 10: "Blekinge", 12: "Skåne", 13: "Halland", 14: "Västra Götaland",
            17: "Värmland", 18: "Örebro", 19: "Västmanland", 20: "Dalarna", 21: "Gävleborg", 22: "Västernorrland",
            23: "Jämtland", 24: "Västerbotten", 25: "Norrbotten"}
ROAD_CONDITION = "väglagskamera"           # VViS road weather cameras (the rest are traffic flow cameras)
_POINT = re.compile(r"POINT\s*\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", re.I)
_ROAD = re.compile(r"^(E\s?\d{1,2})\b")
# Swedish place-name parts that say what a camera looks at (whole words or word endings)
_TAGS = [
    ("bridge", re.compile(r"\w*bron\b|\bbro\b", re.I)),
    ("tunnel", re.compile(r"\w*tunneln?\b", re.I)),
    ("port", re.compile(r"\w*(färjeläge|färjeläget|hamn|hamnen)\b", re.I)),
    ("mountain", re.compile(r"\w*(fjäll|fjället)\b", re.I)),
]


def place_tags(text: str) -> list[str]:
    return [tag for tag, rx in _TAGS if rx.search(text or "")]


def point(geometry) -> tuple[float | None, float | None]:
    """{'WGS84': 'POINT (18.0345 59.3247)'} -> (59.3247, 18.0345)."""
    text = (geometry or {}).get("WGS84") if isinstance(geometry, dict) else None
    m = _POINT.search(text or "")
    return (to_float(m.group(2)), to_float(m.group(1))) if m else (None, None)


def cameras_of(doc) -> list[dict]:
    """The Camera records of an API answer; raises on an error answer (e.g. a refused key)."""
    results = ((doc or {}).get("RESPONSE") or {}).get("RESULT") or [] if isinstance(doc, dict) else []
    out: list[dict] = []
    for res in results:
        if not isinstance(res, dict):
            continue
        if res.get("ERROR"):
            err = res["ERROR"] if isinstance(res["ERROR"], dict) else {}
            raise RuntimeError(f"Trafikverket API: {clean(err.get('MESSAGE') or err.get('SOURCE') or 'error', 120)}")
        out.extend(c for c in res.get("Camera") or [] if isinstance(c, dict))
    return out


def photo_url(cam: dict) -> str | None:
    """The full-size still when there is one, else the standard still."""
    full = clean(cam.get("PhotoUrlFullsize"), 500)
    if full:
        return full
    base = clean(cam.get("PhotoUrl"), 500)
    if base and cam.get("HasFullSizePhoto") and "?" not in base:
        return base + "?type=fullsize"
    return base or None


def parse(doc, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for cam in cameras_of(doc):
        if cam.get("Active") is False or cam.get("Deleted") is True:
            skipped += 1
            continue
        lat, lon = point(cam.get("Geometry"))
        name = clean(cam.get("Name"))
        location = clean(cam.get("Location"))
        road_condition = ROAD_CONDITION in clean(cam.get("Type")).lower()
        counties = cam.get("CountyNo") or []
        county = COUNTIES.get(counties[0]) if isinstance(counties, list) and counties else None
        m = _ROAD.match(name) or _ROAD.match(location)
        row = make_camera(src, cam.get("Id"), name or location, lat, lon, image=photo_url(cam),
                          area=location if location and location.lower() not in name.lower() else None,
                          region=county, road=m.group(1).replace(" ", "") if m else None,
                          heading=cam.get("Direction"), description=cam.get("Description"),
                          refresh=900 if road_condition else 300,
                          tags=(["weather"] if road_condition else []) + place_tags(f"{name} {location}"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    key = fc.key(KEY)
    if not key:
        raise NeedsKey(KEY)
    body = QUERY.format(key=escape(key, {'"': "&quot;", "'": "&apos;"})).encode("utf-8")
    path = post_cached(fc, API_URL, body, "cameras.json", headers={"Content-Type": "text/xml; charset=utf-8",
                                                                    "Accept": "application/json"},
                       timeout=90, min_bytes=50)
    try:
        rows, skipped = parse(parse_json_bytes(read_bytes(path)), fc.src)
    except (RuntimeError, ValueError):
        path.unlink(missing_ok=True)          # never reuse an error answer (say, a refused key)
        raise
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="setrafikverket", name="Trafikverket road cameras", owner="Trafikverket (Swedish Transport Administration)",
    url="https://data.trafikverket.se/", licence="CC0 1.0 (Trafikverket open data)",
    licence_url="https://creativecommons.org/publicdomain/zero/1.0/", attribution="Trafikverket",
    # path prefixes stay lower case: the proxy lower-cases each pattern but not the URL path, so only
    # a lower-case prefix such as "/v1/" can match ".../v1/Images/..."
    fetch=fetch, hosts=("api.trafikinfo.trafikverket.se/v1/", "api.trafikinfo.trafikverket.se/v2/"),
    country="SE", refresh=600, tags=("traffic",), needs_key=KEY, optional=True,
    coverage="Swedish state roads (road condition and traffic flow cameras)",
)
