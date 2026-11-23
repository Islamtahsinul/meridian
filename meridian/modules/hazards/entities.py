"""Entity shapes (CONTRACT 5.6) for every hazard kind, and the per-kind detail records that
cards open from a bare {kind, id}."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from meridian import geo
from meridian.net import NetError

from . import countries, eonet, firms, gdacs, usgs, who
from .common import (GDACS_LEVEL, LAYER_DISASTERS, LAYER_FIRES, LAYER_NATURAL, LAYER_OUTBREAKS, LAYER_QUAKES,
                     LAYER_STORMS, LAYER_TSUNAMI, LAYER_VOLCANOES, LAYER_WARNINGS, TSUNAMI_LEVEL, clean,
                     quake_severity, storm_severity, warning_severity)
from .volcanoes import eruption_label

# USGS ("us7000n1xz") and EONET ("EONET_6123") event ids, as they may be put into an upstream URL path
NATIVE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _utc(t: float | None) -> str:
    if not t:
        return "time unknown"
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _date(t: float | None) -> str:
    if not t:
        return "date unknown"
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%d %b %Y").lstrip("0")


def _mag(m: float | None) -> str:
    return f"{m:.1f}" if m is not None else "?"


# ---------------------------------------------------------------------------------------------
# entities
# ---------------------------------------------------------------------------------------------
def quake_entity(q: dict) -> dict:
    depth = f", depth {max(0, round(q['depth_km']))} km" if q.get("depth_km") is not None else ""
    kind_word = "Earthquake" if (q.get("type") or "earthquake") == "earthquake" else q["type"].capitalize()
    return {"kind": "earthquake", "id": q["id"], "title": f"M {_mag(q.get('mag'))}, {q['place']}",
            "subtitle": f"{kind_word} {_utc(q['time'])}{depth}", "lat": q["lat"], "lon": q["lon"],
            "layer": LAYER_QUAKES, "time": int(q["time"]), "zoom": 7, "source": "USGS"}


def fire_entity(row: dict) -> dict:
    where = f", {row['country']}" if row.get("country") else ""
    return {"kind": "fire", "id": row["id"], "title": f"Fire detection{where}",
            "subtitle": f"{row['satellite']} {row['instrument']}, {_utc(row['time'])}, {row['frp']:g} MW",
            "lat": row["lat"], "lon": row["lon"], "layer": LAYER_FIRES, "time": int(row["time"]), "zoom": 11,
            "source": "NASA FIRMS"}


def eonet_entity(e: dict) -> dict:
    return {"kind": e["kind"], "id": e["id"], "title": e["title"],
            "subtitle": f"{e['category_label']}, NASA EONET, since {_date(e['start'])}",
            "lat": e["lat"], "lon": e["lon"], "layer": LAYER_NATURAL, "time": int(e["time"] or 0), "zoom": 7,
            "source": "NASA EONET"}


def gdacs_entity(e: dict) -> dict:
    when = _date(e.get("to") or e.get("from"))
    layer = LAYER_STORMS if e["type"] == "TC" else LAYER_DISASTERS
    return {"kind": e["kind"], "id": e["id"], "title": e["name"],
            "subtitle": f"{e['alert']} alert, GDACS, {when}", "lat": e["lat"], "lon": e["lon"], "layer": layer,
            "time": int(e.get("to") or e.get("from") or 0), "zoom": 6, "source": "GDACS"}


def storm_entity(s: dict) -> dict:
    wind = f", {s['wind_kmh']} km/h" if s.get("wind_kmh") else ""
    return {"kind": "storm", "id": s["id"], "title": s["title"],
            "subtitle": f"{s['category_label']}{wind}, {s['source_label']}" + ("" if s["active"] else ", ended"),
            "lat": s["lat"], "lon": s["lon"], "layer": LAYER_STORMS, "time": int(s.get("time") or 0), "zoom": 5,
            "source": s["source_label"]}


def volcano_entity(v: dict) -> dict:
    act = v.get("activity") or {}
    status = ""
    hans = act.get("hans")
    if hans and hans.get("color"):
        status = f", alert {hans['color'].lower()}"
    elif act.get("gdacs"):
        status = f", GDACS {act['gdacs']['alert'].lower()} alert"
    elif act.get("eonet"):
        status = ", active"
    bits = [b for b in (v.get("type"), v.get("country")) if b]
    return {"kind": "volcano", "id": v["id"], "title": v["name"],
            "subtitle": (", ".join(bits) or "Volcano") + status, "lat": v["lat"], "lon": v["lon"],
            "layer": LAYER_VOLCANOES, "time": int((act.get("hans") or {}).get("sent") or 0) or None, "zoom": 10,
            "source": "Smithsonian GVP"}


def warning_entity(w: dict, lat: float | None = None, lon: float | None = None) -> dict:
    area = clean(w.get("area"), 90)
    where = w.get("country_name") or ("United States" if w["source"] == "nws" else "")
    return {"kind": "warning", "id": w["id"], "title": w["event"],
            "subtitle": ", ".join(b for b in (area, where) if b) or "Weather warning",
            "lat": lat if lat is not None else w.get("lat"), "lon": lon if lon is not None else w.get("lon"),
            "layer": LAYER_WARNINGS, "time": int(w.get("sent") or w.get("onset") or 0), "zoom": 7,
            "source": "NWS" if w["source"] == "nws" else "MeteoAlarm"}


def tsunami_entity(t: dict) -> dict:
    return {"kind": "tsunami", "id": t["id"], "title": f"Tsunami {t['category'].lower()} message",
            "subtitle": f"{t['region']}, {t['center_name']}", "lat": t["lat"], "lon": t["lon"],
            "layer": LAYER_TSUNAMI, "time": int(t.get("issued") or 0), "zoom": 5, "source": t["center_name"]}


def outbreak_entity(o: dict) -> dict:
    p = (o.get("places") or [None])[0]
    return {"kind": "outbreak", "id": o["id"], "title": o["disease"],
            "subtitle": f"{o['place_text'] or 'Location not stated'}, WHO, {_date(o['published'])}",
            "lat": p["lat"] if p else None, "lon": p["lon"] if p else None, "layer": LAYER_OUTBREAKS,
            "time": int(o["published"]), "zoom": 4, "source": "WHO"}


# ---------------------------------------------------------------------------------------------
# details: one record per kind, used by cards (and the AI)
# ---------------------------------------------------------------------------------------------
def details(svc, kind: str, eid: str) -> dict:
    prefix = eid.split(":", 1)[0]
    fn = {"usgs": _quake, "firms": _fire, "eonet": _eonet, "gdacs": _gdacs, "nhc": _storm, "gvp": _volcano,
          "nws": _warning, "meteoalarm": _warning, "tsunami": _tsunami, "who": _outbreak}.get(prefix) \
        or getattr(svc, "detail_handlers", {}).get(prefix)
    out = fn(svc, eid) if fn else None
    if out is None:
        arch = svc.archived(kind, eid)
        if arch:
            data = arch.get("data") or {}
            ent = data.get("entity") or {"kind": kind, "id": eid, "title": arch.get("title"),
                                         "subtitle": arch.get("subtitle"), "lat": arch.get("lat"), "lon": arch.get("lon")}
            return {"kind": kind, "id": eid, "live": False, "last_seen": arch.get("seen_at"),
                    "entity": ent, "data": data.get("data") or {}, "type": data.get("type")}
        return {"kind": kind, "id": eid, "live": False, "last_seen": None, "entity": {"kind": kind, "id": eid},
                "data": {}, "type": None, "missing": True}
    out.setdefault("kind", kind)
    out.setdefault("id", eid)
    out.setdefault("live", True)
    if out.get("live"):
        svc.archive(kind, eid, out["entity"], {"entity": out["entity"], "data": out["data"], "type": out.get("type")})
    return out


def _quake(svc, eid: str) -> dict | None:
    q = svc.quakes.get(eid)
    native = eid.split(":", 1)[1]
    detail = None
    if NATIVE_ID.fullmatch(native):   # the id is from the request: never spaces, "/", "?" or ".." in the URL
        try:
            detail = svc.net.get_json(usgs.DETAIL.format(id=native), timeout=8, ttl=600, retries=0)
        except (NetError, ValueError):
            detail = None
    if q is None and detail:
        q = usgs.parse_feature(detail)
    if q is None:
        return None
    data = dict(q)
    if detail:
        data.update(usgs.parse_detail(detail))
    if data.get("moment_tensor"):
        data["moment_tensor"]["style"] = usgs.fault_kind(data["moment_tensor"].get("rake"))
    data["severity"] = quake_severity(q["mag"], q.get("alert"), q.get("tsunami"))
    near = []
    for other in svc.quakes.values():
        if other["id"] == q["id"] or abs(other["time"] - q["time"]) > 7 * 86400:
            continue
        d = geo.haversine_km(q["lat"], q["lon"], other["lat"], other["lon"])
        if d <= 150:
            near.append((other["time"], d, other))
    near.sort(key=lambda x: -x[0])
    data["nearby"] = [{"entity": quake_entity(o), "mag": o["mag"], "distance_km": round(d, 1), "time": o["time"]}
                      for _, d, o in near[:8]]
    data["nearby_count"] = len(near)
    for g in svc.gdacs_events.values():
        if g["type"] == "EQ" and (g.get("source_id") == native or (
                abs((g.get("from") or 0) - q["time"]) < 600 and geo.haversine_km(g["lat"], g["lon"], q["lat"], q["lon"]) < 80)):
            data["gdacs"] = {"id": g["id"], "alert": g["alert"], "report_url": g["report_url"],
                             "population": g.get("population")}
            break
    tsu = []
    for t in svc.tsunami_msgs:
        if abs((t.get("issued") or 0) - q["time"]) < 6 * 3600 and geo.haversine_km(t["lat"], t["lon"], q["lat"], q["lon"]) < 400:
            tsu.append({"entity": tsunami_entity(t), "category": t["category"], "issued": t.get("issued")})
    data["tsunami_messages"] = tsu
    c = countries.at(q["lat"], q["lon"])
    data["country"] = c["name"] if c else None
    return {"type": "quake", "entity": quake_entity(q), "data": data}


def _fire(svc, eid: str) -> dict | None:
    row = svc.fire_detail(eid)
    if row:
        return {"type": "fire", "entity": fire_entity(row), "data": row}
    parsed = firms.parse_id(eid)
    if not parsed:
        return None
    sat = firms.SAT_NAMES.get(parsed["sat"], parsed["sat"])
    c = countries.at(parsed["lat"], parsed["lon"])
    data = {"id": eid, "lat": parsed["lat"], "lon": parsed["lon"], "time": parsed["time"], "satellite": sat,
            "instrument": firms.SAT_INSTRUMENT.get(parsed["sat"], "VIIRS"), "country": c["name"] if c else None,
            "frp": None}
    ent = {"kind": "fire", "id": eid, "title": "Fire detection" + (f", {data['country']}" if data["country"] else ""),
           "subtitle": f"{sat}, {_utc(parsed['time'])}", "lat": parsed["lat"], "lon": parsed["lon"],
           "layer": LAYER_FIRES, "time": parsed["time"]}
    return {"type": "fire", "entity": ent, "data": data, "live": False, "last_seen": parsed["time"]}


def _eonet(svc, eid: str) -> dict | None:
    e = next((x for x in svc.eonet_events if x["id"] == eid), None)
    if e is None:
        native = eid.split(":", 1)[1]
        if not NATIVE_ID.fullmatch(native):
            return None
        try:
            e = eonet.parse_event(svc.net.get_json(f"{eonet.API}/{native}", timeout=10, ttl=3600, retries=0))
        except (NetError, ValueError):
            e = None
        if e is None:
            return None
    data = dict(e)
    data["unit_label"] = eonet.unit_label(e.get("unit"))
    data["ended"] = e.get("closed") is not None
    c = countries.at(e["lat"], e["lon"])
    data["country"] = c["name"] if c else None
    return {"type": "natural", "entity": eonet_entity(e), "data": data}


def _gdacs_extras(svc, e: dict) -> dict:
    url = gdacs.gdacs_url(e.get("details_url"))   # a feed link: fetched only on gdacs.org, no redirects
    if not url:
        return {}
    try:
        return gdacs.parse_details(svc.net.get_json(url, timeout=8, ttl=1800, retries=0, allow_redirects=False))
    except (NetError, ValueError, KeyError):
        return {}


def _gdacs(svc, eid: str) -> dict | None:
    parts = eid.split(":")
    if len(parts) == 3 and parts[1] == "TC":
        s = svc.storm(eid)
        if s:
            return _storm(svc, eid)
    e = svc.gdacs_events.get(eid)
    if e is None:
        if len(parts) != 3 or parts[1] not in gdacs.TYPES or not parts[2].isdigit():
            return None
        try:
            data = svc.net.get_json(gdacs.DETAILS_URL, params={"eventtype": parts[1], "eventid": parts[2]},
                                    timeout=10, ttl=1800, retries=0)
            e = gdacs.parse_event_feature(data)
        except (NetError, ValueError):
            e = None
        if e is None:
            return None
    data = dict(e)
    data.update({k: v for k, v in _gdacs_extras(svc, e).items() if v not in (None, "")})
    data["severity_level"] = GDACS_LEVEL.get(e["alert"].lower(), 3)
    data["ongoing"] = gdacs.ongoing(e, time.time())
    data["short"] = gdacs.short_name(e)
    if e["type"] == "EQ":
        for q in svc.quakes.values():
            if q["native_id"] == e.get("source_id") or (abs(q["time"] - (e.get("from") or 0)) < 600 and
                                                        geo.haversine_km(q["lat"], q["lon"], e["lat"], e["lon"]) < 80):
                data["usgs"] = quake_entity(q)
                break
    extras = getattr(svc, "extras", None)
    if extras is not None:
        data["emergency_mapping"] = extras.mapping.for_gdacs(e)
    return {"type": "disaster", "entity": gdacs_entity(e), "data": data}


def _storm(svc, sid: str) -> dict | None:
    s = svc.storm(sid)
    if s is None:
        return None
    data = dict(s)
    data["severity"] = storm_severity(s.get("wind_kmh"), s.get("alert"))
    if s["source"] == "gdacs":
        ev = svc.gdacs_events.get(sid)
        if ev:
            extras = _gdacs_extras(svc, ev)
            data["image"] = extras.get("image")
            data["description"] = extras.get("description") or ev.get("description")
            data["population"] = ev.get("population")
    lats = [p["lat"] for p in s["track"]] or [s["lat"]]
    lons = [p["lon"] for p in s["track"]] or [s["lon"]]
    data["bbox"] = [min(lons), min(lats), max(lons), max(lats)]
    data.pop("cone", None)
    data.pop("buffers", None)
    data.pop("segments", None)
    data.pop("track_line", None)
    data.pop("past_line", None)
    return {"type": "storm", "entity": storm_entity(s), "data": data}


def _volcano(svc, vid: str) -> dict | None:
    v = svc.volcano(vid)
    if v is None:
        return None
    data = dict(v)
    data["last_eruption_label"] = eruption_label(v.get("last_eruption"))
    data["page"] = f"https://volcano.si.edu/volcano.cfm?vn={v['vnum']}" if v.get("vnum") else None
    near = []
    for q in svc.quakes.values():
        d = geo.haversine_km(v["lat"], v["lon"], q["lat"], q["lon"])
        if d <= 40 and q["time"] >= time.time() - 7 * 86400:
            near.append((q["time"], d, q))
    near.sort(key=lambda x: -x[0])
    data["recent_quakes"] = [{"entity": quake_entity(q), "mag": q["mag"], "distance_km": round(d, 1)} for _, d, q in near[:6]]
    data["recent_quakes_count"] = len(near)
    return {"type": "volcano", "entity": volcano_entity(v), "data": data}


def _warning(svc, wid: str) -> dict | None:
    w = svc.warning(wid)
    if w is None:
        return None
    data = dict(w)
    geom = svc.warning_geometry(w)
    lat, lon = w.get("lat"), w.get("lon")
    if geom:
        from .common import geometry_center, geometry_bbox, simplify_geometry
        light = simplify_geometry(geom, 0.01, 3)
        data["outline"] = light
        data["bbox"] = geometry_bbox(light) if light else None
        c = geometry_center(light) if light else None
        if c and (lat is None or lon is None):
            lon, lat = round(c[0], 5), round(c[1], 5)
    data["lat"], data["lon"] = lat, lon
    from .meteoalarm import cap_url_ok
    if w["source"] == "meteoalarm" and cap_url_ok(w.get("cap_url")):
        try:
            from .meteoalarm import parse_cap
            cap = parse_cap(svc.net.get_text(w["cap_url"], timeout=8, ttl=1800, retries=0, allow_redirects=False))
            for k, v in cap.items():
                if v and not data.get(k):
                    data[k] = v
        except NetError:
            pass
    if w["source"] == "nws":
        data["zone_names"] = [(svc.nws_zones.get(z) or {}).get("name") or z for _, z in w["zones"]][:40]
        if lat is not None:
            from .nws import forecast_page
            data["web"] = forecast_page(lat, lon)
    data.pop("geometry", None)
    data.pop("polygons", None)
    data["severity_level"] = warning_severity(w.get("severity"), w.get("urgency"))
    return {"type": "warning", "entity": warning_entity(w, lat, lon), "data": data}


def _tsunami(svc, tid: str) -> dict | None:
    t = svc.tsunami_msg(tid)
    if t is None:
        return None
    data = dict(t)
    data["severity"] = TSUNAMI_LEVEL.get(t["category"].lower(), 2)
    quake = None
    for q in svc.quakes.values():
        if abs(q["time"] - (t.get("issued") or 0)) < 3 * 3600 and geo.haversine_km(q["lat"], q["lon"], t["lat"], t["lon"]) < 300:
            if quake is None or (q["mag"] or 0) > (quake["mag"] or 0):
                quake = q
    data["quake"] = quake_entity(quake) if quake else None
    return {"type": "tsunami", "entity": tsunami_entity(t), "data": data}


def _outbreak(svc, oid: str) -> dict | None:
    o = svc.outbreak(oid)
    if o is None:
        don = oid.split(":", 1)[1].replace("'", "")
        try:
            data = svc.net.get_json(who.API, params={"$filter": f"DonId eq '{don}' or UrlName eq '{don}'",
                                                     "$select": who.FIELDS, "$top": "1"}, timeout=10, ttl=3600, retries=0)
            items = who.parse_items(data)
            o = items[0] if items else None
        except (NetError, ValueError):
            o = None
        if o is None:
            return None
    data = dict(o)
    data["related"] = [{"entity": outbreak_entity(r), "published": r["published"], "title": r["title"]}
                       for r in svc.related_reports(o)]
    age = (time.time() - o["published"]) / 86400.0
    data["active"] = age <= who.ACTIVE_DAYS
    data["age_days"] = round(age, 1)
    data["severity"] = who.severity(o, time.time())
    return {"type": "outbreak", "entity": outbreak_entity(o), "data": data}


def eruption(v: dict) -> str:
    return eruption_label(v.get("last_eruption"))
