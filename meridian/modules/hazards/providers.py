"""Search, nearby, events (CONTRACT 5.5/5.6) and the AI tools of the hazards module.
All read in-memory snapshots, so they answer in milliseconds."""

from __future__ import annotations

import time

from meridian import geo
from meridian.net import NetError

from . import countries, firms, nws, usgs, who
from .common import (GDACS_LEVEL, KINDS, SEVERITY_RANK, TSUNAMI_LEVEL, clamp, geometry_center, norm, quake_severity,
                     storm_severity, warning_severity)
from .entities import (eonet_entity, fire_entity, gdacs_entity, outbreak_entity, quake_entity, storm_entity,
                       tsunami_entity, volcano_entity, warning_entity)

EONET_SEVERITY = {"floods": 4, "landslides": 4, "tempExtremes": 4, "dustHaze": 3, "drought": 3, "snow": 3,
                  "manmade": 3, "wildfires": 3, "earthquakes": 3}


# ---------------------------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------------------------
def search(svc, q: str, limit: int) -> list[dict]:
    nq = norm(q)
    if len(nq) < 3:
        return []
    out: list[dict] = []

    def score(name: str, base: float) -> float | None:
        n = norm(name)
        if not n:
            return None
        if n == nq:
            return base + 0.25
        if n.startswith(nq):
            return base + 0.12
        if nq in n:
            return base
        return None

    for n, v in svc.volcano_names:
        if nq in n:
            act = svc.volcano_activity.get(v["id"])
            s = 0.62 if n == nq else 0.5 if n.startswith(nq) else 0.38
            ent = volcano_entity(dict(v, activity=act or {}))
            ent["score"] = s + (0.1 if act and act.get("rank") else 0.0)
            out.append(ent)
    for s in svc.storms:
        sc = score(s["name"], 0.45) or score(s["title"], 0.4)
        if sc:
            ent = storm_entity(s)
            ent["score"] = sc + (0.1 if s["active"] else 0)
            out.append(ent)
    seen_diseases = set()
    for o in svc.active_outbreaks(365):
        dk = who.disease_key(o["disease"])
        if dk in seen_diseases:
            continue
        sc = score(o["disease"], 0.35) or score(o["title"], 0.3)
        if sc is None:
            for p in o["places"]:
                sc = score(p["name"], 0.28)
                if sc:
                    break
        if sc:
            seen_diseases.add(dk)
            ent = outbreak_entity(o)
            ent["score"] = sc
            out.append(ent)
    now = time.time()
    for qk in svc.quakes.values():
        if (qk["mag"] or 0) < 4.0 and qk["time"] < now - 86400:
            continue
        sc = score(qk["place"], 0.3)
        if sc:
            ent = quake_entity(qk)
            ent["score"] = sc + min(0.15, (qk["mag"] or 0) / 60.0)
            out.append(ent)
    for e in svc.gdacs_events.values():
        sc = score(e["name"], 0.32) or (score(e["country"] or "", 0.26) if e.get("country") else None)
        if sc:
            ent = gdacs_entity(e)
            ent["score"] = sc + 0.03 * (GDACS_LEVEL.get(e["alert"].lower(), 3) / 3)
            out.append(ent)
    for e in svc.eonet_events:
        if e["category"] in ("wildfires", "severeStorms", "volcanoes"):
            continue
        sc = score(e["title"], 0.3)
        if sc:
            ent = eonet_entity(e)
            ent["score"] = sc
            out.append(ent)
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# ---------------------------------------------------------------------------------------------
# nearby
# ---------------------------------------------------------------------------------------------
def nearby(svc, lat: float, lon: float, radius_km: float, limit: int) -> list[dict]:
    out: list[dict] = []
    now = time.time()

    def add(ent, d):
        ent = dict(ent)
        ent["distance_km"] = round(d, 2)
        out.append(ent)

    quakes = []
    for q in svc.quakes.values():
        if q["time"] < now - 7 * 86400:
            continue
        d = geo.haversine_km(lat, lon, q["lat"], q["lon"])
        if d <= radius_km:
            quakes.append((d, q))
    quakes.sort(key=lambda x: x[0])
    for d, q in quakes[:limit]:
        add(quake_entity(q), d)
    for d, v in svc.volcano_index.near(lat, lon, radius_km, limit):
        add(volcano_entity(dict(v, activity=svc.volcano_activity.get(v["id"]) or {})), d)
    if len(svc.fire_data):
        best = svc.fire_data.near(lat, lon, min(radius_km, 50.0), 200)
        best.sort(key=lambda x: (-svc.fire_data.frp[x[1]], x[0]))
        for d, i in best[:min(limit, 5)]:
            row = svc.fire_data.row(i)
            row["country"] = None
            add(fire_entity(row), d)
    for e in svc.gdacs_events.values():
        if svc.gdacs_duplicates_usgs(e):
            continue
        d = geo.haversine_km(lat, lon, e["lat"], e["lon"])
        if d <= max(radius_km, 50):
            add(gdacs_entity(e), d)
    for s in svc.storms:
        d = geo.haversine_km(lat, lon, s["lat"], s["lon"])
        if d <= max(radius_km, 300) and s["active"]:
            add(storm_entity(s), d)
    for w in svc.warnings_at(lat, lon)[:limit]:
        add(warning_entity(w, lat, lon), 0.0)
    for t in svc.tsunami_msgs:
        if (t.get("issued") or 0) < now - 7 * 86400:
            continue
        d = geo.haversine_km(lat, lon, t["lat"], t["lon"])
        if d <= radius_km:
            add(tsunami_entity(t), d)
    c = countries.at(lat, lon)
    if c:
        for o in svc.active_outbreaks(who.ACTIVE_DAYS):
            for p in o["places"]:
                if p.get("iso2") == c["iso2"]:
                    ent = outbreak_entity(o)
                    ent["lat"], ent["lon"] = p["lat"], p["lon"]
                    add(ent, geo.haversine_km(lat, lon, p["lat"], p["lon"]))
                    break
    for e in svc.eonet_events:
        if e["category"] in ("wildfires", "volcanoes", "severeStorms"):
            continue
        d = geo.haversine_km(lat, lon, e["lat"], e["lon"])
        if d <= radius_km:
            add(eonet_entity(e), d)
    out.sort(key=lambda x: x["distance_km"])
    return out[: max(limit * 3, limit)]


# ---------------------------------------------------------------------------------------------
# events (notable things only)
# ---------------------------------------------------------------------------------------------
def events(svc, since_ts: float) -> list[dict]:
    out: list[dict] = []
    now = time.time()

    def add(ent, t, sev, **extra):
        if t is None or t < since_ts or sev < 3:
            return
        e = dict(ent)
        e["time"] = int(t)
        e["severity"] = int(clamp(sev, 0, 10))
        e.update(extra)
        out.append(e)

    usgs_ids = set()
    for q in svc.quakes.values():
        mag = q["mag"] or 0
        if mag >= 4.5 or q.get("alert") in ("yellow", "orange", "red") or (q.get("tsunami") and mag >= 5):
            usgs_ids.add(q["native_id"])
            add(quake_entity(q), q["time"], quake_severity(mag, q.get("alert"), q.get("tsunami")),
                magnitude=q["mag"], depth_km=q["depth_km"], alert=q.get("alert"), tsunami=q.get("tsunami"))
    data = svc.fire_data
    if len(data):
        for c in data.clusters():
            row = data.row(c["top"])
            ent = fire_entity(row)
            ent["title"] = f"Fire cluster, {c['n']:,} detections"
            ent["subtitle"] = f"{c['frp_sum']:,.0f} MW total radiative power in a {c['size']}° cell, NASA FIRMS"
            ent["lat"], ent["lon"] = round(c["lat"], 4), round(c["lon"], 4)
            add(ent, c["t"], firms.cluster_severity(c["frp_sum"], c["n"]), detections=c["n"],
                frp_total_mw=round(c["frp_sum"]))
    for e in svc.gdacs_events.values():
        if e["type"] == "TC":
            continue
        level = e["alert"].lower()
        if level == "green" and e["type"] in ("EQ", "WF", "DR"):
            continue
        if e["type"] == "EQ" and (e.get("source_id") in usgs_ids or svc.gdacs_duplicates_usgs(e)):
            continue
        sev = GDACS_LEVEL.get(level, 3)
        add(gdacs_entity(e), e.get("modified") or e.get("to") or e.get("from"), sev, alert=e["alert"],
            hazard=e["type_name"])
    for s in svc.storms:
        if not s["active"]:
            continue
        add(storm_entity(s), s.get("time") or now, storm_severity(s.get("wind_kmh"), s.get("alert")),
            wind_kmh=s.get("wind_kmh"), category=s.get("category"))
    for v in svc.volcano_list:
        act = svc.volcano_activity.get(v["id"])
        if not act or act.get("rank", 0) < 2:
            continue
        t = max([x for x in ((act.get("hans") or {}).get("sent"), (act.get("eonet") or {}).get("time"),
                             (act.get("gdacs") or {}).get("time")) if x] or [now])
        add(volcano_entity(dict(v, activity=act)), t, {2: 3, 3: 6, 4: 9}.get(act["rank"], 3))
    for x in svc.extra_volcanoes:
        act = x["activity"]
        t = max([y for y in ((act.get("eonet") or {}).get("time"), (act.get("gdacs") or {}).get("time")) if y] or [now])
        add(volcano_entity(x), t, {2: 3, 3: 6, 4: 9}.get(act.get("rank"), 3))
    for w in svc.all_warnings():
        rank = SEVERITY_RANK.get((w.get("severity") or "").lower(), 0)
        if rank < 3:
            continue
        lat, lon = w.get("lat"), w.get("lon")
        if lat is None:
            g = svc.warning_geometry(w)
            if g:
                c = geometry_center(g)
                if c:
                    lon, lat = round(c[0], 4), round(c[1], 4)
        if lat is None:
            continue
        add(warning_entity(w, lat, lon), w.get("sent") or w.get("onset"), warning_severity(w["severity"], w.get("urgency")),
            expires=w.get("expires"), severity_text=w["severity"])
    for t in svc.tsunami_msgs:
        sev = TSUNAMI_LEVEL.get(t["category"].lower(), 2)
        if t["category"] == "Information" and (t.get("magnitude") or 0) >= 7:
            sev = 4
        add(tsunami_entity(t), t.get("issued"), sev, category=t["category"])
    for o in svc.active_outbreaks(who.ACTIVE_DAYS):
        ent = outbreak_entity(o)
        if ent["lat"] is None:
            continue
        add(ent, o["published"], who.severity(o, now), places=[p["name"] for p in o["places"]])
    have_fires = len(data) > 0
    for e in svc.eonet_events:
        if e["category"] in ("volcanoes", "severeStorms", "seaLakeIce"):
            continue
        if e["category"] == "wildfires" and have_fires:
            continue
        sev = EONET_SEVERITY.get(e["category"], 3)
        if e["category"] == "wildfires" and (e.get("magnitude") or 0) >= 1000:
            sev = 4
        add(eonet_entity(e), e["time"], sev)
    # emergency mapping activations and England flood warnings (plus.py)
    for hook in getattr(svc, "event_hooks", ()):
        try:
            extra = hook(since_ts) or []
        except Exception as err:   # an add-on never breaks the rest of the events
            svc.log.warning("hazards event hook failed: %s", err)
            continue
        for e in extra:
            add({k: v for k, v in e.items() if k not in ("time", "severity")}, e.get("time"), e.get("severity", 0))
    out.sort(key=lambda e: (-e["severity"], -e["time"]))
    return out[:3000]


# ---------------------------------------------------------------------------------------------
# AI tools
# ---------------------------------------------------------------------------------------------
def _bbox_arg(value):
    if value in (None, "", []):
        return None
    try:
        if isinstance(value, (list, tuple)):
            return geo.parse_bbox(",".join(str(float(v)) for v in value))
        return geo.parse_bbox(str(value))
    except (ValueError, TypeError):
        raise ValueError("bbox must be west,south,east,north in degrees") from None


def _num_arg(args: dict, name: str, default, lo, hi):
    v = args.get(name, default)
    if v in (None, ""):
        return default
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    return clamp(v, lo, hi)


def tool_recent(svc, args: dict) -> dict:
    hours = _num_arg(args, "hours", 48, 0.5, 720)
    bbox = _bbox_arg(args.get("bbox"))
    kinds = args.get("kinds") or []
    if isinstance(kinds, str):
        kinds = [k.strip() for k in kinds.split(",") if k.strip()]
    kinds = {k for k in kinds if k in KINDS}
    min_sev = int(_num_arg(args, "min_severity", 3, 0, 10))
    limit = int(_num_arg(args, "limit", 40, 1, 200))
    items = events(svc, time.time() - hours * 3600)
    out = []
    for e in items:
        if kinds and e["kind"] not in kinds:
            continue
        if e["severity"] < min_sev:
            continue
        if bbox and not geo.in_bbox(float(e["lat"]), float(e["lon"]), bbox):
            continue
        out.append(e)
    out.sort(key=lambda e: (-e["severity"], -e["time"]))
    return {"hours": hours, "count": len(out), "events": out[:limit],
            "note": "Severity 0-10 (3-5 notable, 6-8 serious, 9-10 extreme). Weather warnings only severe and extreme."}


def tool_earthquakes(svc, args: dict) -> dict:
    min_mag = _num_arg(args, "min_magnitude", 4.5, -1, 10)
    hours = _num_arg(args, "hours", 168, 1, 24 * 365)
    bbox = _bbox_arg(args.get("bbox"))
    region = str(args.get("region") or "").strip()[:80]
    limit = int(_num_arg(args, "limit", 30, 1, 200))
    now = time.time()
    rows = None
    source = "USGS feeds (cached)"
    need_api = hours > 168 or (min_mag < 2.5 and hours > 24)
    if need_api:
        params = {"format": "geojson", "starttime": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - hours * 3600)),
                  "minmagnitude": min_mag, "orderby": "magnitude", "limit": 200}
        if bbox:
            w, s, e, n = bbox
            if w <= e:
                params.update(minlatitude=s, maxlatitude=n, minlongitude=w, maxlongitude=e)
            else:
                params.update(minlatitude=s, maxlatitude=n, minlongitude=w, maxlongitude=e + 360)
        try:
            data = svc.net.get_json(usgs.FDSN, params=params, timeout=9, ttl=600, retries=0)
            rows = usgs.parse_feed(data)
            source = "USGS FDSN event service"
        except (NetError, ValueError):
            rows = None
    if rows is None:
        rows = [q for q in svc.quakes.values() if q["time"] >= now - hours * 3600]
    out = []
    for q in rows:
        if (q["mag"] if q["mag"] is not None else -9) < min_mag:
            continue
        if bbox and not geo.in_bbox(q["lat"], q["lon"], bbox):
            continue
        if region and norm(region) not in norm(q["place"]):
            c = countries.lookup(region)
            if not c or not (c["bbox"][1] <= q["lat"] <= c["bbox"][3] and c["bbox"][0] <= q["lon"] <= c["bbox"][2]):
                continue
        out.append(q)
    out.sort(key=lambda q: (-(q["mag"] or 0), -q["time"]))
    return {"source": source, "hours": hours, "min_magnitude": min_mag, "count": len(out), "earthquakes": [
        {"id": q["id"], "magnitude": q["mag"], "magnitude_type": q["mag_type"], "place": q["place"],
         "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(q["time"])), "lat": q["lat"], "lon": q["lon"],
         "depth_km": q["depth_km"], "pager_alert": q["alert"], "tsunami_flag": q["tsunami"], "felt_reports": q["felt"],
         "url": q["url"], "entity": quake_entity(q)} for q in out[:limit]]}


def tool_warnings_at(svc, args: dict) -> dict:
    try:
        lat = float(args.get("lat"))
        lon = float(args.get("lon"))
    except (TypeError, ValueError):
        raise ValueError("lat and lon are required numbers") from None
    if not geo.valid_latlon(lat, lon):
        raise ValueError("lat/lon out of range")
    found = list(svc.warnings_at(lat, lon))
    notes = []
    if nws.in_coverage(lat, lon) and (svc.state["nws"].stale(900) or not svc.in_use("nws")):
        try:
            data = svc.net.get_json(nws.ALERTS_URL, params={"point": f"{lat:.4f},{lon:.4f}"}, headers=nws.HEADERS,
                                    timeout=8, ttl=300, retries=0)
            live = nws.parse_alerts(data, time.time())
            have = {w["id"] for w in found}
            found.extend(a for a in live if a["id"] not in have)
            notes.append("NWS checked live for this point")
        except NetError as err:
            if err.status != 400:
                notes.append("NWS point check failed")
    c = countries.at(lat, lon)
    if c and c["iso2"] in svc.meteoalarm_iso() and not svc.ma_warnings.get(svc.meteoalarm_slug(c["iso2"]) or ""):
        notes.append(f"MeteoAlarm warnings for {c['name']} load when the weather warnings layer is used")
    found.sort(key=lambda w: (-SEVERITY_RANK.get((w.get("severity") or "").lower(), 0), w.get("onset") or 0))
    now = time.time()
    return {"lat": lat, "lon": lon, "country": c["name"] if c else None, "count": len(found), "notes": notes,
            "warnings": [{"id": w["id"], "source": "NWS" if w["source"] == "nws" else "MeteoAlarm",
                          "event": w["event"], "severity": w.get("severity"), "certainty": w.get("certainty"),
                          "urgency": w.get("urgency"), "area": w.get("area"), "headline": w.get("headline"),
                          "onset_utc": _iso(w.get("onset")), "expires_utc": _iso(w.get("expires")),
                          "in_force": not w.get("onset") or w["onset"] <= now, "entity": warning_entity(w, lat, lon)}
                         for w in found[:25]]}


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None


def here(svc, lat: float, lon: float) -> dict:
    """Compact summary for the "What's here?" card."""
    now = time.time()
    warnings = svc.warnings_at(lat, lon)[:6]
    quakes = []
    for q in svc.quakes.values():
        if q["time"] < now - 7 * 86400 or (q["mag"] or 0) < 2.5:
            continue
        d = geo.haversine_km(lat, lon, q["lat"], q["lon"])
        if d <= 250:
            quakes.append((d, q))
    quakes.sort(key=lambda x: (-(x[1]["mag"] or 0)))
    volc = svc.volcano_index.near(lat, lon, 60.0, 1)
    fires = svc.fire_data.near(lat, lon, 15.0, 0) if len(svc.fire_data) else []
    return {
        "warnings": [dict(warning_entity(w, lat, lon), severity=w.get("severity"), level=w.get("level"),
                          expires=w.get("expires"), onset=w.get("onset")) for w in warnings],
        "quakes": [dict(quake_entity(q), distance_km=round(d, 1), magnitude=q["mag"]) for d, q in quakes[:3]],
        "quakes_count": len(quakes),
        "volcano": dict(volcano_entity(dict(volc[0][1], activity=svc.volcano_activity.get(volc[0][1]["id"]) or {})),
                        distance_km=round(volc[0][0], 1)) if volc else None,
        "fires": {"count": len(fires), "frp_max": round(max((svc.fire_data.frp[i] for _, i in fires), default=0), 1),
                  "nearest_km": round(fires[0][0], 1) if fires else None,
                  "entity": fire_entity(svc.fire_data.row(fires[0][1])) if fires else None},
    }


def parse_kinds(text: str | None) -> set[str] | None:
    if not text:
        return None
    return {k for k in (s.strip() for s in text.split(",")) if k in KINDS} or None

