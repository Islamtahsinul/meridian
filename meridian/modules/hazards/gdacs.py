"""GDACS (Global Disaster Alert and Coordination System, European Commission JRC and UN OCHA):
earthquakes, tropical cyclones, floods, volcanoes, droughts and wildfires with Green, Orange and
Red alert levels. Terms: https://www.gdacs.org/About/termofuse.aspx (free use, credit GDACS).
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone

from .common import (circle_ring_center, clean, geometry_bbox, num, parse_time, r5, simplify_geometry,
                     strip_html, title_case_name, valid_lonlat)

API = "https://www.gdacs.org/gdacsapi/api/events/"
EVENTS_URL = API + "geteventlist/EVENTS4APP"
SEARCH_URL = API + "geteventlist/SEARCH"
MAP_URL = API + "geteventlist/MAP"
DETAILS_URL = API + "geteventdata"
GEOMETRY_URL = "https://www.gdacs.org/gdacsapi/api/polygons/getgeometry"
RSS_URL = "https://www.gdacs.org/xml/rss.xml"
REPORT_URL = "https://www.gdacs.org/report.aspx?eventid={eventid}&episodeid={episodeid}&eventtype={type}"

TYPES = ("EQ", "TC", "FL", "VO", "DR", "WF")
TYPE_KIND = {"EQ": "earthquake", "TC": "storm", "FL": "flood", "VO": "volcano", "WF": "fire", "DR": "disaster"}
TYPE_NAME = {"EQ": "Earthquake", "TC": "Tropical cyclone", "FL": "Flood", "VO": "Volcanic eruption",
             "WF": "Wildfire", "DR": "Drought"}
ALERTS = ("Green", "Orange", "Red")
ALERT_RANK = {"green": 1, "orange": 2, "red": 3}
HOSTS = ("www.gdacs.org", "gdacs.org")
_URL_BAD = re.compile(r"[\x00-\x20\x7f\\]")


def gdacs_url(value) -> str | None:
    """``value`` if it is an https link on gdacs.org, else None. The geometry and details links come
    from the event feed (third-party data) and are fetched by the server, so a link elsewhere (an
    internal address, another site) must never be followed: callers fall back to a URL built here."""
    if not isinstance(value, str) or not value or len(value) > 1000 or _URL_BAD.search(value):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or "@" in parts.netloc or host not in HOSTS or port not in (None, 443):
        return None
    return value

NS = {"gdacs": "http://www.gdacs.org", "georss": "http://www.georss.org/georss",
      "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#", "dc": "http://purl.org/dc/elements/1.1/"}


def _alert(value) -> str | None:
    v = (value or "").strip().capitalize()
    return v if v in ALERTS else None


def _countries(p: dict) -> list[dict]:
    out = []
    for c in p.get("affectedcountries") or []:
        if isinstance(c, dict) and (c.get("countryname") or c.get("iso2")):
            out.append({"iso2": (c.get("iso2") or "").upper() or None, "iso3": c.get("iso3"),
                        "name": clean(c.get("countryname"), 80)})
    return out


def parse_event_feature(f: dict) -> dict | None:
    """One event from an EVENTS4APP / SEARCH / MAP list (centroid features only)."""
    if not isinstance(f, dict):
        return None
    p = f.get("properties") or {}
    cls = p.get("Class") or "Point_Centroid"
    if cls != "Point_Centroid":
        return None
    etype = (p.get("eventtype") or "").upper()
    eid = p.get("eventid")
    g = f.get("geometry") or {}
    c = g.get("coordinates") or []
    if etype not in TYPES or eid is None or g.get("type") != "Point" or len(c) < 2 or not valid_lonlat(c[0], c[1]):
        return None
    sev = p.get("severitydata") or {}
    url = p.get("url") or {}
    episode = p.get("episodeid")
    name = clean(p.get("name"), 200) or TYPE_NAME[etype]
    countries = _countries(p)
    return {
        "id": f"gdacs:{etype}:{eid}",
        "type": etype,
        "kind": TYPE_KIND[etype],
        "type_name": TYPE_NAME[etype],
        "eventid": eid,
        "episodeid": episode,
        "name": name,
        "event_name": clean(p.get("eventname"), 60) or None,
        "description": strip_html(p.get("htmldescription") or p.get("description"), 400) or name,
        "alert": _alert(p.get("alertlevel")) or "Green",
        "alert_score": num(p.get("alertscore")),
        "episode_alert": _alert(p.get("episodealertlevel")),
        "current": str(p.get("iscurrent")).lower() == "true",
        "temporary": str(p.get("istemporary")).lower() == "true",
        "country": clean(p.get("country"), 300) or (", ".join(c["name"] for c in countries) or None),
        "countries": countries,
        "iso3": p.get("iso3") or None,
        "from": parse_time(p.get("fromdate")),
        "to": parse_time(p.get("todate")),
        "modified": parse_time(p.get("datemodified")),
        "source": p.get("source") or None,
        "source_id": p.get("sourceid") or None,
        "glide": p.get("glide") or None,
        "severity": num(sev.get("severity")),
        "severity_text": clean(sev.get("severitytext"), 160) or None,
        "severity_unit": sev.get("severityunit") or None,
        "lat": r5(c[1]),
        "lon": r5(c[0]),
        "bbox": f.get("bbox") if isinstance(f.get("bbox"), list) and len(f.get("bbox")) == 4 else None,
        "report_url": gdacs_url(url.get("report")) or REPORT_URL.format(eventid=eid, episodeid=episode, type=etype),
        "geometry_url": gdacs_url(url.get("geometry")),
        "details_url": gdacs_url(url.get("details"))
        or f"{DETAILS_URL}?{urllib.parse.urlencode({'eventtype': etype, 'eventid': eid})}",
        "population": None,
    }


def parse_events(data: dict) -> list[dict]:
    out = []
    for f in (data or {}).get("features") or []:
        ev = parse_event_feature(f)
        if ev:
            out.append(ev)
    return out


def parse_rss(text: str) -> list[dict]:
    """The GDACS RSS feed: a fallback list, and the "population affected" wording for cards."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        etype = (item.findtext("gdacs:eventtype", namespaces=NS) or "").upper()
        eid = item.findtext("gdacs:eventid", namespaces=NS)
        point = (item.findtext("georss:point", namespaces=NS) or "").split()
        if etype not in TYPES or not eid or len(point) != 2:
            continue
        try:
            lat, lon = float(point[0]), float(point[1])
            eid_i = int(eid)
        except ValueError:
            continue
        if not valid_lonlat(lon, lat):
            continue
        sev = item.find("gdacs:severity", NS)
        pop = item.find("gdacs:population", NS)
        episode = item.findtext("gdacs:episodeid", namespaces=NS)
        name = clean(item.findtext("title"), 200)
        out.append({
            "id": f"gdacs:{etype}:{eid_i}", "type": etype, "kind": TYPE_KIND[etype], "type_name": TYPE_NAME[etype],
            "eventid": eid_i, "episodeid": int(episode) if (episode or "").isdigit() else None,
            "name": name, "event_name": clean(item.findtext("gdacs:eventname", namespaces=NS), 60) or None,
            "description": strip_html(item.findtext("description"), 400),
            "alert": _alert(item.findtext("gdacs:alertlevel", namespaces=NS)) or "Green",
            "alert_score": num(item.findtext("gdacs:alertscore", namespaces=NS)),
            "episode_alert": _alert(item.findtext("gdacs:episodealertlevel", namespaces=NS)),
            "current": (item.findtext("gdacs:iscurrent", namespaces=NS) or "").lower() == "true",
            "temporary": (item.findtext("gdacs:temporary", namespaces=NS) or "").lower() == "true",
            "country": clean(item.findtext("gdacs:country", namespaces=NS), 300) or None,
            "countries": [], "iso3": item.findtext("gdacs:iso3", namespaces=NS) or None,
            "from": parse_time(item.findtext("gdacs:fromdate", namespaces=NS)),
            "to": parse_time(item.findtext("gdacs:todate", namespaces=NS)),
            "modified": parse_time(item.findtext("gdacs:datemodified", namespaces=NS)),
            "source": None, "source_id": None, "glide": item.findtext("gdacs:glide", namespaces=NS) or None,
            "severity": num(sev.get("value")) if sev is not None else None,
            "severity_text": clean(sev.text, 160) if sev is not None and sev.text else None,
            "severity_unit": sev.get("unit") if sev is not None else None,
            "lat": r5(lat), "lon": r5(lon), "bbox": None,
            "report_url": REPORT_URL.format(eventid=eid_i, episodeid=episode, type=etype),
            "geometry_url": None, "details_url": f"{DETAILS_URL}?eventtype={etype}&eventid={eid_i}",
            "population": clean(pop.text, 160) if pop is not None and pop.text else None,
        })
    return out


def ongoing(e: dict, now: float) -> bool:
    """GDACS marks long droughts and floods as not current while they are still being updated:
    treat an event as ongoing when it is current or its last date is within the past week."""
    end = e.get("to") or e.get("from") or 0
    return bool(e.get("current")) or end >= now - 7 * 86400


def short_name(e: dict) -> str:
    """Map label: "Drought in Albania and 24 more", "Cyclone Xavier", "Earthquake in Vanuatu"."""
    name = re.sub(r"^(Green|Orange|Red)\s+", "", e.get("name") or "", flags=re.I).strip()
    name = re.sub(r"^Tropical Cyclone\s+", "Cyclone ", name, flags=re.I)
    if e.get("event_name") and e.get("type") == "TC":
        name = "Cyclone " + title_case_name(e["event_name"])
    m = re.match(r"^(.*?\bin)\s+(.+)$", name)
    if m:
        places = [p.strip() for p in re.split(r",\s*", m.group(2)) if p.strip()]
        if len(places) > 2:
            return f"{m.group(1)} {places[0]} and {len(places) - 1} more"
    return name[:48]


def merge_events(*lists: list[dict]) -> dict[str, dict]:
    """Union by id. Later lists only fill gaps (RSS adds population wording)."""
    merged: dict[str, dict] = {}
    for items in lists:
        for ev in items or []:
            old = merged.get(ev["id"])
            if old is None:
                merged[ev["id"]] = dict(ev)
                continue
            if (ev.get("modified") or 0) > (old.get("modified") or 0) and ev.get("geometry_url"):
                keep_pop = old.get("population")
                old.update(ev)
                old["population"] = old.get("population") or keep_pop
            for k, v in ev.items():
                if old.get(k) in (None, "", []) and v not in (None, "", []):
                    old[k] = v
    return merged


# ---------------------------------------------------------------------------------------------
# tropical cyclones: tracks, cones and wind buffers from the MAP list
# ---------------------------------------------------------------------------------------------
_LABEL_TIME = re.compile(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})")
WIND_BUFFERS = {"Poly_Green": 60, "Poly_Orange": 90, "Poly_Red": 120}


def _label_time(label: str, ref: float | None) -> float | None:
    """"22/11 12:00 UTC" -> epoch, picking the year that puts it closest to ``ref``."""
    m = _LABEL_TIME.search(label or "")
    if not m or ref is None:
        return None
    d, mo, hh, mm = (int(x) for x in m.groups())
    year = datetime.fromtimestamp(ref, tz=timezone.utc).year
    best = None
    for y in (year - 1, year, year + 1):
        try:
            t = datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if best is None or abs(t - ref) < abs(best - ref):
            best = t
    return best


def parse_tc_map(data: dict) -> list[dict]:
    """Group the TC MAP features by event into storms with track points, segments, the cone and
    wind buffers."""
    by_event: dict[int, list[dict]] = defaultdict(list)
    for f in (data or {}).get("features") or []:
        p = (f or {}).get("properties") or {}
        if (p.get("eventtype") or "").upper() == "TC" and p.get("eventid") is not None:
            by_event[p["eventid"]].append(f)
    storms = []
    for eid, feats in by_event.items():
        base = None
        for f in feats:
            if (f["properties"].get("Class") or "") == "Point_Centroid":
                base = f
                break
        event = parse_event_feature(base) if base else None
        props0 = (base or feats[0])["properties"]
        advisory = max((parse_time(f["properties"].get("polygondate")) or 0 for f in feats), default=0) or None
        ref = advisory or parse_time(props0.get("todate"))
        points, segments, cone, buffers = [], [], None, []
        for f in feats:
            p = f["properties"]
            cls = p.get("Class") or ""
            g = f.get("geometry") or {}
            if cls.startswith("Point_Polygon_Point_") and g.get("type") == "Polygon":
                try:
                    n = int(cls.rsplit("_", 1)[1])
                except ValueError:
                    continue
                c = circle_ring_center(g["coordinates"][0])
                if c and valid_lonlat(*c):
                    points.append({"n": n, "lon": r5(c[0]), "lat": r5(c[1]), "t": _label_time(p.get("polygonlabel"), ref),
                                   "label": clean(p.get("polygonlabel"), 40)})
            elif cls.startswith("Line_Line_") and g.get("type") == "LineString":
                try:
                    n = int(cls.rsplit("_", 1)[1])
                except ValueError:
                    continue
                coords = [[r5(x), r5(y)] for x, y, *_ in g.get("coordinates") or [] if valid_lonlat(x, y)]
                if len(coords) >= 2:
                    segments.append({"n": n, "coords": coords, "class": (p.get("polygonlabel") or "").upper()[:4] or None})
            elif cls == "Poly_Cones":
                cone = simplify_geometry(g, 0.02, 3)
            elif cls in WIND_BUFFERS:
                geom = simplify_geometry(g, 0.02, 3)
                if geom:
                    buffers.append({"kmh": WIND_BUFFERS[cls], "geometry": geom})
        points.sort(key=lambda x: x["n"])
        segments.sort(key=lambda x: x["n"])
        for pt in points:
            pt["forecast"] = bool(advisory and pt["t"] and pt["t"] > advisory + 60)
        by_n = {pt["n"]: pt for pt in points}
        for seg in segments:
            end = by_n.get(seg["n"] + 1)
            seg["forecast"] = bool(end and end["forecast"])
        observed = [pt for pt in points if not pt["forecast"]]
        current = observed[-1] if observed else (points[0] if points else None)
        if event is None:
            c = current or {}
            if not c:
                continue
            event = {"id": f"gdacs:TC:{eid}", "type": "TC", "kind": "storm", "type_name": TYPE_NAME["TC"],
                     "eventid": eid, "episodeid": props0.get("episodeid"), "name": clean(props0.get("name"), 200),
                     "event_name": props0.get("eventname"), "alert": _alert(props0.get("alertlevel")) or "Green",
                     "lat": c["lat"], "lon": c["lon"], "countries": _countries(props0),
                     "country": clean(props0.get("country"), 300) or None,
                     "from": parse_time(props0.get("fromdate")), "to": parse_time(props0.get("todate")),
                     "severity": num((props0.get("severitydata") or {}).get("severity")),
                     "severity_text": clean((props0.get("severitydata") or {}).get("severitytext"), 160),
                     "source": props0.get("source"), "current": str(props0.get("iscurrent")).lower() == "true",
                     "report_url": REPORT_URL.format(eventid=eid, episodeid=props0.get("episodeid"), type="TC")}
        storms.append({
            "event": event,
            "name": title_case_name(event.get("event_name") or "") or event.get("name"),
            "advisory_time": advisory,
            "points": points,
            "segments": segments,
            "cone": cone,
            "buffers": buffers,
            "current": current,
        })
    return storms


def simplify_event_geometry(data: dict, tol: float = 0.01) -> dict:
    """The getgeometry answer as a light FeatureCollection (affected areas, outlines)."""
    feats = []
    for f in (data or {}).get("features") or []:
        p = (f or {}).get("properties") or {}
        g = simplify_geometry((f or {}).get("geometry"), tol, 4)
        if not g:
            continue
        cls = p.get("Class") or ""
        if cls == "Point_Centroid":
            continue
        feats.append({"type": "Feature", "geometry": g,
                      "properties": {"role": cls, "label": clean(p.get("polygonlabel"), 60),
                                     "alert": _alert(p.get("alertlevel")) or "Green"}})
    return {"type": "FeatureCollection", "features": feats[:400],
            "bbox": geometry_bbox({"type": "GeometryCollection", "geometries": [f["geometry"] for f in feats]})
            if feats else None}


def parse_details(data: dict) -> dict:
    """Card extras from geteventdata: images, impacts and links."""
    p = (data or {}).get("properties") or {}
    images = p.get("images") or {}
    url = p.get("url") or {}
    out = {}
    for key in ("overviewmap", "thumbnailmap_cached", "populationmap_cached"):
        v = images.get(key)
        if isinstance(v, str) and v.startswith("https://"):
            out.setdefault("image", v)
    if gdacs_url(url.get("report")):
        out["report_url"] = url["report"]
    sev = p.get("severitydata") or {}
    if sev.get("severitytext"):
        out["severity_text"] = clean(sev.get("severitytext"), 160)
    out["episodes"] = len(p.get("episodes") or [])
    out["description"] = strip_html(p.get("htmldescription"), 400) or None
    return out
