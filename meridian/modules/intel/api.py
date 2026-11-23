"""HTTP routes and discovery providers (search, nearby, events, AI tools) of the intel module."""

from __future__ import annotations

import re
import time

from meridian import geo
from meridian.registry import HTTPError

from . import codes, db, gdelt, news, risk

RE_EVENT = re.compile(r"gd[0-9a-f]{14}")
RE_NEWS = re.compile(r"(gn|dn)[0-9a-f]{14}")
RE_ISO2 = re.compile(r"[A-Za-z]{2}")
RE_ZONE = re.compile(r"rz:-?\d{1,2}\.\d:-?\d{1,3}\.\d")
RE_CONV = re.compile(r"cv:-?\d{1,2}\.\d:-?\d{1,3}\.\d")
RE_UCDP = re.compile(r"ucdp:-?\d{1,2}\.\d{2}:-?\d{1,3}\.\d{2}")
CAT_CODE = {"attack": "a", "fighting": "f", "assault": "s"}


def _fc(features: list) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _pt(lon, lat) -> dict:
    return {"type": "Point", "coordinates": [round(float(lon), 5), round(float(lat), 5)]}


def register_routes(m, svc) -> None:
    store = m.store

    def need(pattern: re.Pattern, value: str, what: str) -> str:
        if not pattern.fullmatch(value or ""):
            raise HTTPError(400, "BAD_ARG", f"That is not a valid {what} id.")
        return value

    @m.route("GET", "/api/intel/status")
    def r_status(req):
        return svc.status()

    # -- conflict events ---------------------------------------------------------------------------
    @m.route("GET", "/api/intel/conflict")
    def r_conflict(req):
        bbox = req.bbox("bbox", None)
        days = req.arg("days", 7.0, type=float, min=0.25, max=14.0)
        min_sev = req.arg("min_severity", 0.0, type=float, min=0.0, max=10.0)
        crime = req.arg("crime", False, type=bool)
        limit = req.arg("limit", 4000, type=int, min=1, max=8000)
        rows = db.query_event_points(store, bbox=bbox, since=time.time() - days * 86400, min_severity=min_sev,
                                     include_crime=crime, limit=limit)
        feats = [{"type": "Feature", "geometry": _pt(ev["lon"], ev["lat"]), "properties": {
            "id": ev["id"], "s": ev["severity"], "c": CAT_CODE.get(ev["category"], "s"), "k": 1 if ev["crime"] else 0,
            "p": 1 if ev["precision"] == "city" else 0, "l": codes.event_label(ev["code"], ev["root"]),
            "n": gdelt.short_place(ev["place"]), "t": int(ev["t_last"])}} for ev in rows]
        out = _fc(feats)
        out["meta"] = {"count": len(feats), "limited": len(feats) >= limit, "days": days,
                       "pending_files": svc.ingest.get("pending"), "latest_file": svc.ingest.get("latest"),
                       "error": svc.ingest.get("error")}
        return out

    @m.route("GET", "/api/intel/conflict/{eid}")
    def r_conflict_one(req):
        eid = need(RE_EVENT, req.params["eid"], "conflict event")
        ev = db.get_event(store, eid)
        if not ev:
            raise HTTPError(404, "NOT_FOUND", "This event is no longer in the 14-day window.")
        return svc.event_detail(ev)

    # -- news ------------------------------------------------------------------------------------------
    @m.route("GET", "/api/intel/news")
    def r_news(req):
        bbox = req.bbox("bbox", None)
        hours = req.arg("hours", 24.0, type=float, min=1.0, max=72.0)
        min_sev = req.arg("min_severity", 0.0, type=float, min=0.0, max=10.0)
        limit = req.arg("limit", 3000, type=int, min=1, max=6000)
        titled = req.arg("titled", True, type=bool)
        rows = db.query_news(store, bbox=bbox, since=time.time() - hours * 3600, min_severity=min_sev,
                             titled_only=titled, limit=limit)
        feats = [{"type": "Feature", "geometry": _pt(n["lon"], n["lat"]), "properties": {
            "id": n["id"], "s": n["severity"], "l": svc.news_title(n)[:90], "d": n["domain"],
            "n": gdelt.short_place(n["place"]), "t": int(n["t"])}} for n in rows]
        out = _fc(feats)
        out["meta"] = {"count": len(feats), "limited": len(feats) >= limit, "hours": hours}
        return out

    @m.route("GET", "/api/intel/news/item/{nid}")
    def r_news_one(req):
        nid = need(RE_NEWS, req.params["nid"], "news")
        item = svc.news_item(nid)
        if not item:
            raise HTTPError(404, "NOT_FOUND", "This report is no longer in the news window.")
        return item

    @m.route("GET", "/api/intel/news/feeds")
    def r_feeds(req):
        theatre = req.arg("theatre", None)
        if theatre and theatre not in news.THEATRE_BY_ID:
            raise HTTPError(400, "BAD_ARG", "Unknown theatre.")
        min_sev = req.arg("min_severity", 0.0, type=float, min=0.0, max=10.0)
        hours = req.arg("hours", 24.0, type=float, min=1.0, max=72.0)
        limit = req.arg("limit", 25, type=int, min=1, max=100)
        q = req.arg("q", None)
        if q is not None and len(q) > 80:
            raise HTTPError(400, "BAD_ARG", "The filter text is too long.")
        return svc.feeds(theatre, min_sev, q, hours, limit)

    @m.route("GET", "/api/intel/theatres")
    def r_theatres(req):
        return {"theatres": [{"id": t["id"], "name": t["name"], "lat": t["lat"], "lon": t["lon"], "zoom": t["zoom"],
                              "countries": t["countries"],
                              "bbox": news.theatre_bbox(t, lambda iso: (svc.countries.get(iso) or {}).get("bbox"))}
                             for t in news.THEATRES]}

    # -- frontlines ------------------------------------------------------------------------------------
    @m.route("GET", "/api/intel/frontlines")
    def r_front(req):
        return m.serve_file(m.data_dir / "frontlines.geojson")

    @m.route("GET", "/api/intel/frontlines/meta")
    def r_front_meta(req):
        if not svc.frontline_meta:
            raise HTTPError(503, "BUILDING", "The front-line data is still being prepared.")
        return svc.frontline_meta

    # -- countries ---------------------------------------------------------------------------------------
    @m.route("GET", "/api/intel/countries/shapes")
    def r_shapes(req):
        return m.serve_file(m.data_dir / "countries.geojson")

    @m.route("GET", "/api/intel/countries/index")
    def r_index(req):
        return svc.index_summary()

    @m.route("GET", "/api/intel/country/{iso2}")
    def r_country(req):
        iso2 = need(RE_ISO2, req.params["iso2"], "country").upper()
        prof = svc.profile(iso2)
        if not prof:
            if not svc.countries and not svc.wb:
                raise HTTPError(503, "BUILDING", "Country data is still being prepared.")
            raise HTTPError(404, "NOT_FOUND", "No country with that code.")
        return prof

    @m.route("GET", "/api/intel/country-at")
    def r_country_at(req):
        lat, lon = req.latlon()
        hit = svc.country_at(lat, lon)
        if hit is None:
            return {"lat": lat, "lon": lon, "country": None, "ready": svc.country_index is not None}
        iso2 = hit.get("iso2")
        idx = svc.index.get(iso2) if iso2 else None
        econ = svc.wb.get(iso2) if iso2 else None
        return {"lat": lat, "lon": lon, "ready": True, "country": dict(hit, flag=codes.flag_emoji(iso2) if iso2 else "",
                                                                          index=(idx or {}).get("index"), band=(idx or {}).get("band"),
                                                                          capital=(econ or {}).get("capital"))}

    # -- risk zones and convergences -------------------------------------------------------------------
    @m.route("GET", "/api/intel/risk")
    def r_risk(req):
        out = dict(svc.zones_fc)
        out["meta"] = {"zones": len(svc.zones), "computed_at": svc.zones_at, "cell_deg": risk.CELL,
                       "window_days": risk.WINDOW_DAYS}
        return out

    @m.route("GET", "/api/intel/risk/{zid}")
    def r_risk_one(req):
        zid = need(RE_ZONE, req.params["zid"], "risk zone")
        z = svc.zone_detail(zid)
        if not z:
            raise HTTPError(404, "NOT_FOUND", "This risk zone is not active any more.")
        return z

    @m.route("GET", "/api/intel/convergences")
    def r_conv(req):
        feats = []
        for cv in svc.convergences:
            feats.append({"type": "Feature", "geometry": _pt(cv["lon"], cv["lat"]), "properties": {
                "id": cv["id"], "s": cv["severity"], "k": cv["n_kinds"], "n": cv["n_events"], "title": cv["title"],
                "t": int(cv["time"]), "label": f"{cv['n_kinds']} kinds"}})
            w, s, e, n = cv["bbox"]
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[w, s], [e, s], [e, n], [w, n], [w, s]]},
                          "properties": {"id": cv["id"], "role": "box"}})
        out = _fc(feats)
        out["meta"] = {"count": len(svc.convergences), "computed_at": svc.conv_at, "inputs": svc.conv_inputs}
        out["items"] = [svc.conv_entity(cv) for cv in svc.convergences[:50]]
        return out

    @m.route("GET", "/api/intel/convergences/{cid}")
    def r_conv_one(req):
        cid = need(RE_CONV, req.params["cid"], "convergence")
        cv = svc.conv_by_id.get(cid)
        if not cv:
            raise HTTPError(404, "NOT_FOUND", "This convergence has ended (it is recomputed every 10 minutes).")
        return dict(cv, computed_at=svc.conv_at)

    # -- cyber ----------------------------------------------------------------------------------------------
    @m.route("GET", "/api/intel/cyber")
    def r_cyber(req):
        svc.request_outages()
        out = svc.cyber_fc()
        out["meta"] = {"feodo": svc.feodo_meta, "outages": {k: len(v) for k, v in svc.outages.items()},
                       "outages_at": svc.outages_at, "cloudflare_token": m.config.has("CLOUDFLARE_RADAR_TOKEN"),
                       "radar_at": svc.radar.at.get("anomalies"), "radar_errors": svc.radar.snapshot()["errors"]}
        return out

    @m.route("GET", "/api/intel/cyber/{kind}/{iso2}")
    def r_cyber_one(req):
        kind = req.params["kind"]
        if kind not in ("c2", "outage"):
            raise HTTPError(400, "BAD_ARG", "kind must be c2 or outage.")
        iso2 = need(RE_ISO2, req.params["iso2"], "country")
        d = svc.cyber_detail(kind, iso2)
        if not d:
            raise HTTPError(404, "NOT_FOUND", "Nothing is listed for that country right now.")
        return d

    # -- UCDP ------------------------------------------------------------------------------------------------
    @m.route("GET", "/api/intel/ucdp")
    def r_ucdp(req):
        if not svc.ucdp:
            raise HTTPError(503, "BUILDING", "The UCDP dataset is still being built (about 40 MB to download).")
        years = sorted(int(y) for y in svc.ucdp.get("years") or {})
        y_min, y_max = (years[0], years[-1]) if years else (1989, 2025)
        y1 = req.arg("to", y_max, type=int, min=1946, max=2100)
        y0 = req.arg("from", y1, type=int, min=1946, max=2100)
        if y0 > y1:
            y0, y1 = y1, y0
        out = dict(svc.ucdp_fc(y0, y1))
        out["meta"] = {"from": y0, "to": y1, "years": [y_min, y_max], "version": svc.ucdp.get("version"),
                       "candidates": svc.ucdp.get("candidates"), "count": len(out.get("features") or [])}
        return out

    @m.route("GET", "/api/intel/ucdp/cell/{cid}")
    def r_ucdp_cell(req):
        cid = need(RE_UCDP, req.params["cid"], "UCDP cell")
        if not svc.ucdp:
            raise HTTPError(503, "BUILDING", "The UCDP dataset is still being built.")
        _, la, lo = cid.split(":")
        lat, lon = float(la), float(lo)
        for c in svc.ucdp.get("cells") or []:
            if abs(c[0] - lat) < 1e-6 and abs(c[1] - lon) < 1e-6:
                years = {int(y): v for y, v in c[2].items()}
                return {"id": cid, "lat": lat, "lon": lon, "cell_deg": svc.ucdp.get("cell"),
                        "years": [{"year": y, "events": v[0], "deaths": v[1], "state": v[2], "nonstate": v[3], "onesided": v[4]}
                                  for y, v in sorted(years.items())],
                        "conflicts": c[3], "countries": c[4], "version": svc.ucdp.get("version")}
        raise HTTPError(404, "NOT_FOUND", "No UCDP events in that cell.")


# ---------------------------------------------------------------------------------------------
# Search, nearby, events, AI tools
# ---------------------------------------------------------------------------------------------
def register_providers(m, svc) -> None:
    store = m.store

    def country_entity(iso2: str, score: float) -> dict | None:
        p = svc.countries.get(iso2) or {}
        e = svc.wb.get(iso2) or {}
        name = p.get("name") or e.get("name")
        if not name:
            return None
        label = p.get("label")
        idx = svc.index.get(iso2) or {}
        sub = "Country"
        if idx.get("index") is not None:
            sub += f", instability {idx['index']} ({idx['band'].lower()})"
        ent = {"kind": "country", "id": iso2, "title": name, "subtitle": sub,
               "lat": label[1] if label else e.get("lat"), "lon": label[0] if label else e.get("lon"),
               "layer": "intel.countries", "score": score, "zoom": 4}
        bb = p.get("bbox")
        if bb and bb[2] - bb[0] < 60:
            ent["bbox"] = bb
        return ent

    def search(q: str, limit: int):
        ql = (q or "").strip().lower()
        if len(ql) < 2:
            return []
        out = []
        for iso2, p in list(svc.countries.items()):
            names = [n for n in ((p.get("name") or "").lower(), (p.get("short") or "").lower(),
                                 ((svc.wb.get(iso2) or {}).get("name") or "").lower()) if n]
            score = 0.0
            if ql == iso2.lower() or ql == (p.get("iso3") or "").lower() or ql in names:
                score = 0.95
            elif any(n.startswith(ql) for n in names):
                score = 0.8
            elif len(ql) >= 4 and any(ql in n for n in names):
                score = 0.55
            if score:
                ent = country_entity(iso2, score)
                if ent:
                    out.append(ent)
        for th in news.THEATRES:
            if ql in th["name"].lower() or ql in th["id"]:
                out.append({"kind": "theatre", "id": th["id"], "title": th["name"], "subtitle": "Monitored theatre, news feed",
                            "lat": th["lat"], "lon": th["lon"], "zoom": th["zoom"], "layer": "intel.news", "score": 0.7})
        if len(ql) >= 3:
            n = 0
            for p in svc.places:
                place = p.get("place") or ""
                low = place.lower()
                if low.startswith(ql) or (len(ql) >= 4 and f" {ql}" in f" {low}"):
                    out.append({"kind": "place", "id": f"intel-place:{place}",
                                "title": svc.place_display(place, svc.country_name(p.get("iso2"))),
                                "subtitle": f"{p['n']} conflict events in 7 days, highest severity {p['max_sev']:.1f}",
                                "lat": round(p["lat"], 5), "lon": round(p["lon"], 5), "layer": "intel.conflict",
                                "zoom": 9, "score": 0.45 if low.startswith(ql) else 0.35})
                    n += 1
                    if n >= 6:
                        break
            for row in db.query_news(store, since=time.time() - 3 * 86400, titled_only=True, q=q.strip()[:60],
                                     limit=min(8, limit)):
                ent = svc.news_entity(row)
                ent["score"] = 0.3
                out.append(ent)
        return out[: max(1, limit)]

    m.search(search)

    def nearby(lat: float, lon: float, radius_km: float, limit: int):
        now = time.time()
        out = []
        for d, ev in db.near_events(store, lat, lon, radius_km, since=now - 7 * 86400, limit=limit):
            ent = svc.event_entity(ev)
            ent["distance_km"] = round(d, 2)
            out.append(ent)
        bbox = geo.bbox_of_circle(lat, lon, radius_km)
        rows = db.query_news(store, bbox=bbox, since=now - 86400, titled_only=True, limit=200)
        near = []
        for n in rows:
            d = geo.haversine_km(lat, lon, n["lat"], n["lon"])
            if d <= radius_km:
                ent = svc.news_entity(n)
                ent["distance_km"] = round(d, 2)
                near.append(ent)
        near.sort(key=lambda e: e["distance_km"])
        out.extend(near[: max(1, limit // 2)])
        for z in svc.zones:
            w, s, e, n_ = z["bbox"]
            if s <= lat <= n_ and w <= lon <= e:
                ent = svc.zone_entity(z)
                ent["distance_km"] = round(geo.haversine_km(lat, lon, z["lat"], z["lon"]), 2)
                out.append(ent)
        return out

    m.nearby(nearby)

    def events(since_ts: float):
        now = time.time()
        out = []
        for ev in db.query_events(store, since=max(since_ts, now - db.EVENT_DAYS * 86400), min_severity=4.0, limit=2000):
            out.append(svc.event_entity(ev))
        for n in db.query_news(store, since=max(since_ts, now - db.NEWS_DAYS * 86400), min_severity=6.5,
                               titled_only=True, limit=300):
            out.append(svc.news_entity(n))
        for cv in list(svc.convergences):
            if cv["time"] >= since_ts:
                out.append(svc.conv_entity(cv))
        return out

    m.events(events)

    # -- AI tools ---------------------------------------------------------------------------------------
    def tool_country(args: dict):
        iso2 = svc.resolve_country(args.get("iso2") or args.get("name"))
        if not iso2:
            return {"error": "Unknown country. Give an ISO 3166-1 alpha-2 code or an English name."}
        prof = svc.profile(iso2)
        if not prof:
            return {"error": "No data for that country yet."}
        inst = prof.get("instability") or {}
        return {"iso2": prof["iso2"], "name": prof["name"], "capital": prof["capital"], "region": prof["region"],
                "income": prof["income"],
                "indicators": {k: {"value": v["value"], "year": v["year"], "label": v["label"]} for k, v in prof["indicators"].items()},
                "instability_index": inst.get("index"), "instability_band": inst.get("band"),
                "instability_parts": [{k: p.get(k) for k in ("label", "score", "weight", "detail")} for p in inst.get("parts") or []],
                "conflict_events_7d": prof["conflict"]["events_7d"],
                "recent_conflict_events": [{k: e.get(k) for k in ("id", "title", "subtitle", "severity", "lat", "lon", "time")}
                                           for e in prof["conflict"]["recent"][:6]],
                "news_reports_24h": prof["news"]["points_24h"], "theatres": prof["theatres"], "notes": prof["notes"],
                "cyber": prof["cyber"], "lat": prof["lat"], "lon": prof["lon"],
                "travel_advice": {src: ({"level": e.get("level"), "advice": e.get("label")} if e else None)
                                  for src, e in (("uk_fcdo", (prof.get("travel_advice") or {}).get("fcdo")),
                                                 ("us_state_department", (prof.get("travel_advice") or {}).get("us")))},
                "protests_7d": (prof.get("protests") or {}).get("events_7d"),
                "news_volume_24h": {k: {"value": v.get("value"), "usual": v.get("median"), "level": v.get("level")}
                                    for k, v in ((prof.get("news_spikes") or {}).get("metrics") or {}).items()},
                "caveat": "Conflict and news figures are machine-coded from news reports (GDELT) and not verified."}

    m.ai_tool("country_profile",
              "Profile of a country: World Bank indicators (population, GDP, GDP per capita, inflation, unemployment, "
              "military spending), the 0-100 instability index with its parts, recent conflict events and news counts.",
              {"type": "object", "properties": {"iso2": {"type": "string", "description": "ISO 3166-1 alpha-2 code"},
                                                "name": {"type": "string", "description": "English country name"}},
               "additionalProperties": False}, tool_country)

    def _bbox_arg(v):
        if v is None or v == "":
            return None
        if isinstance(v, (list, tuple)) and len(v) == 4:
            v = ",".join(str(x) for x in v)
        try:
            return geo.parse_bbox(str(v))
        except ValueError:
            return None

    def _num(args: dict, key: str, default: float) -> float:
        v = args.get(key)
        try:
            return float(v) if v is not None and v != "" else default
        except (TypeError, ValueError):
            return default

    def tool_conflict(args: dict):
        days = max(0.25, min(14.0, _num(args, "days", 3)))
        limit = max(1, min(100, int(_num(args, "limit", 30))))
        min_sev = max(0.0, min(10.0, _num(args, "min_severity", 4)))
        iso2 = svc.resolve_country(args.get("country")) if args.get("country") else None
        if args.get("country") and not iso2:
            return {"error": "Unknown country."}
        rows = db.query_events(store, bbox=_bbox_arg(args.get("bbox")), since=time.time() - days * 86400,
                               min_severity=min_sev, include_crime=bool(args.get("include_crime")), iso2=iso2, limit=limit)
        return {"count": len(rows), "days": days, "country": iso2,
                "events": [{"id": ev["id"], "action": codes.event_label(ev["code"], ev["root"]),
                            "category": codes.CATEGORY_LABEL.get(ev["category"]), "place": ev["place"], "country": ev["iso2"],
                            "lat": round(ev["lat"], 4), "lon": round(ev["lon"], 4), "severity": ev["severity"],
                            "reported": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ev["t_last"])),
                            "actors": ev["actors"][:4], "mentions": ev["mentions"], "sources": ev["sources"],
                            "source_url": (ev["urls"] or [None])[0]} for ev in rows],
                "caveat": "Machine-coded from news reports (GDELT), not verified."}

    m.ai_tool("conflict_events",
              "Recent conflict events (assaults, fighting, attacks) machine-coded by GDELT from news, deduplicated, "
              "with a 0-10 severity. Filter by bounding box, country and number of days (up to 14).",
              {"type": "object", "properties": {
                  "bbox": {"type": "string", "description": "west,south,east,north"},
                  "country": {"type": "string", "description": "ISO alpha-2 code or English name"},
                  "days": {"type": "number", "minimum": 0.25, "maximum": 14},
                  "min_severity": {"type": "number", "minimum": 0, "maximum": 10},
                  "include_crime": {"type": "boolean"},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
               "additionalProperties": False}, tool_conflict)

    def tool_news(args: dict):
        hours = max(1.0, min(72.0, _num(args, "hours", 24)))
        limit = max(1, min(50, int(_num(args, "limit", 20))))
        theatre = args.get("theatre")
        tid = None
        if theatre:
            tl = str(theatre).lower()
            tid = next((t["id"] for t in news.THEATRES if tl == t["id"] or tl in t["name"].lower()), None)
            if not tid:
                return {"error": "Unknown theatre.", "theatres": [t["name"] for t in news.THEATRES]}
        q = (args.get("query") or "").strip()[:80] or None
        if tid:
            feed = svc.feeds(tid, 0.0, q, hours, limit)
            items = feed["theatres"][0]["items"]
        else:
            rows = db.query_news(store, since=time.time() - hours * 3600, titled_only=True, q=q, limit=limit,
                                 order="time" if not q else "severity")
            items = [{"id": n["id"], "source": "gdelt", "title": svc.news_title(n), "url": n["url"], "domain": n["domain"],
                      "t": n["t"], "severity": n["severity"], "lat": n["lat"], "lon": n["lon"], "place": n["place"]}
                     for n in rows]
        return {"count": len(items), "theatre": tid, "items": [
            {k: it.get(k) for k in ("id", "source", "title", "url", "domain", "severity", "place", "lat", "lon")}
            | {"time": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(it["t"])) if it.get("t") else None}
            for it in items[:limit]],
            "note": "Headlines from GDELT DOC when reachable; otherwise guessed from the article link."}

    m.ai_tool("news_search",
              "Search recent geolocated news reports (GDELT) by text, or list the latest items of a monitored "
              "theatre (Ukraine and Russia, Israel Gaza and Lebanon, Red Sea and Yemen, Sudan, Central Sahel, Myanmar, "
              "Taiwan Strait and South China Sea, DR Congo).",
              {"type": "object", "properties": {"query": {"type": "string"}, "theatre": {"type": "string"},
                                                "hours": {"type": "number", "minimum": 1, "maximum": 72},
                                                "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
               "additionalProperties": False}, tool_news)

    def tool_conv(args: dict):
        items = []
        for cv in svc.convergences[:30]:
            items.append({"id": cv["id"], "title": cv["title"], "country": cv.get("country"), "lat": cv["lat"], "lon": cv["lon"],
                          "severity": cv["severity"], "kinds": cv["kinds"],
                          "latest": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(cv["time"])),
                          "members": [{k: mm.get(k) for k in ("kind", "title", "severity")} for mm in cv["members"][:8]]})
        return {"count": len(svc.convergences), "computed_at": svc.conv_at, "inputs": svc.conv_inputs, "convergences": items,
                "method": "Cells of about 1 degree where 3 or more different kinds of event (from all layers) coincide within 24 hours."}

    m.ai_tool("convergences",
              "Places where three or more different kinds of event from all Meridian layers (conflict, hazards, "
              "aviation, shipping, cyber and so on) coincide within 24 hours.",
              {"type": "object", "properties": {}, "additionalProperties": False}, tool_conv)
