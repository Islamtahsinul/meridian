"""Routes, providers and AI tools of the Intelligence+ additions: Cloudflare Radar anomalies and BGP events,
official travel advice, protests and news spikes. See docs/modules/intel.md."""

from __future__ import annotations

import re
import time

from meridian import geo
from meridian.registry import HTTPError

from . import advice, codes, db, gdelt, protests, radar
from .spikes import METRICS

RE_PROTEST = re.compile(r"gp[0-9a-f]{14}")
RE_ISO2 = re.compile(r"[A-Za-z]{2}")
RE_UUID = re.compile(r"[0-9a-f-]{8,64}")
RE_NUM = re.compile(r"\d{1,12}")
RE_CHANGE = re.compile(r"(fcdo|us):[A-Z]{2}:\d{1,12}")
RE_SPIKE = re.compile(r"[A-Z]{2}:(volume|conflict|protest):\d{1,12}")
CAT_CODE = {"riot": "r", "blockade": "b", "strike": "s", "demonstration": "d"}
DISCLAIMER = "Machine-coded from news reports (GDELT), not verified."


def _fc(features: list) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _pt(lon, lat) -> dict:
    return {"type": "Point", "coordinates": [round(float(lon), 5), round(float(lat), 5)]}


def _utc(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t else None


def register_routes(m, svc) -> None:
    store = m.store

    def need(pattern: re.Pattern, value: str, what: str) -> str:
        if not pattern.fullmatch(value or ""):
            raise HTTPError(400, "BAD_ARG", f"That is not a valid {what} id.")
        return value

    # -- Cloudflare Radar ---------------------------------------------------------------------------
    @m.route("GET", "/api/intel/radar")
    def r_radar(req):
        iso = req.arg("country", None)
        if iso is not None and not RE_ISO2.fullmatch(iso):
            raise HTTPError(400, "BAD_ARG", "country must be an ISO 3166-1 alpha-2 code.")
        snap = svc.radar.snapshot()
        now = time.time()
        if iso:
            return dict(svc.radar_country(iso.upper(), now), country=iso.upper())
        return {"token": svc.radar.has_token(), "at": snap["at"], "errors": snap["errors"],
                "anomalies": [svc.anomaly_public(a, now) for a in snap["anomalies"]],
                "hijacks": [svc.hijack_public(h) for h in snap["hijacks"][:60]],
                "leaks": [svc.leak_public(x) for x in snap["leaks"][:30]],
                "counts": {k: len(snap[k]) for k in ("anomalies", "annotations", "hijacks", "leaks")}}

    @m.route("GET", "/api/intel/radar/anomaly/{uid}")
    def r_anomaly(req):
        uid = need(RE_UUID, req.params["uid"].lower(), "traffic anomaly")
        a = next((x for x in svc.radar.snapshot()["anomalies"] if x["id"] == uid), None)
        if not a:
            raise HTTPError(404, "NOT_FOUND", "This anomaly is no longer in Cloudflare Radar's 7-day window.")
        out = svc.anomaly_public(a)
        pos = svc._label_point(a["iso2"])
        out.update(lat=pos[1] if pos else None, lon=pos[0] if pos else None, flag=codes.flag_emoji(a["iso2"]))
        return out

    @m.route("GET", "/api/intel/radar/bgp/{kind}/{eid}")
    def r_bgp(req):
        kind = req.params["kind"]
        if kind not in ("hijack", "leak", "pair"):
            raise HTTPError(400, "BAD_ARG", "kind must be hijack, leak or pair.")
        snap = svc.radar.snapshot()
        if kind == "pair":
            pair = req.params["eid"].upper()
            if not re.fullmatch(r"[A-Z]{2}-[A-Z]{2}", pair):
                raise HTTPError(400, "BAD_ARG", "A pair looks like TR-GB.")
            a, b = pair.split("-")
            items = [svc.hijack_public(h) for h in snap["hijacks"]
                     if h.get("hijacker_iso2") == a and (b in (h.get("victim_iso2") or []) or (a == b and not h.get("victim_iso2")))]
            return {"pair": pair, "from": a, "to": b, "from_name": svc.country_name(a), "to_name": svc.country_name(b),
                    "hijacks": items, "at": snap["at"].get("hijacks")}
        eid = int(need(RE_NUM, req.params["eid"], "BGP event"))
        pool = snap["hijacks"] if kind == "hijack" else snap["leaks"]
        item = next((x for x in pool if x["id"] == eid), None)
        if not item:
            raise HTTPError(404, "NOT_FOUND", "This BGP event is no longer in the last day of Cloudflare Radar data.")
        out = svc.hijack_public(item) if kind == "hijack" else svc.leak_public(item)
        iso = (item.get("victim_iso2") or [item.get("hijacker_iso2")])[0] if kind == "hijack" else item.get("leak_iso2")
        pos = svc._label_point(iso) if iso else None
        out.update(lat=pos[1] if pos else None, lon=pos[0] if pos else None, at=snap["at"].get(kind + "s"))
        return out

    # -- travel advice --------------------------------------------------------------------------------
    @m.route("GET", "/api/intel/advice")
    def r_advice(req):
        return svc.advice.summary()

    @m.route("GET", "/api/intel/advice/changes")
    def r_advice_changes(req):
        days = req.arg("days", 30.0, type=float, min=1.0, max=365.0)
        items = svc.advice.changes(time.time() - days * 86400, limit=200)
        return {"days": days, "count": len(items), "changes": [dict(c, entity=svc.advice_entity(c)) for c in items]}

    @m.route("GET", "/api/intel/advice/change/{cid}")
    def r_advice_change(req):
        cid = need(RE_CHANGE, req.params["cid"], "travel advice change")
        ch = db.get_advice_change(store, cid)
        if not ch:
            raise HTTPError(404, "NOT_FOUND", "This change is not on record (changes are kept for a year).")
        out = dict(ch, entity=svc.advice_entity(ch), name=svc.country_name(ch["iso2"]), flag=codes.flag_emoji(ch["iso2"]))
        out["current"] = svc.advice.country(ch["iso2"])
        return out

    @m.route("GET", "/api/intel/advice/{iso2}")
    def r_advice_country(req):
        iso2 = need(RE_ISO2, req.params["iso2"], "country").upper()
        out = svc.advice.country(iso2)
        out.update(name=svc.country_name(iso2), flag=codes.flag_emoji(iso2))
        return out

    # -- protests ------------------------------------------------------------------------------------
    @m.route("GET", "/api/intel/protests")
    def r_protests(req):
        bbox = req.bbox("bbox", None)
        days = req.arg("days", 3.0, type=float, min=0.25, max=14.0)
        min_sev = req.arg("min_severity", 0.0, type=float, min=0.0, max=10.0)
        single = req.arg("single", False, type=bool)
        limit = req.arg("limit", 3000, type=int, min=1, max=6000)
        rows = db.query_event_points(store, bbox=bbox, since=time.time() - days * 86400, min_severity=min_sev,
                                     limit=limit, table="intel_protests", min_support=not single)
        feats = [{"type": "Feature", "geometry": _pt(ev["lon"], ev["lat"]), "properties": {
            "id": ev["id"], "s": ev["severity"], "c": CAT_CODE.get(ev["category"], "d"),
            "p": 1 if ev["precision"] == "city" else 0, "l": protests.label(ev["code"]),
            "n": gdelt.short_place(ev["place"]), "t": int(ev["t_last"]), "k": int(ev["sources"] or 0)}} for ev in rows]
        out = _fc(feats)
        out["meta"] = {"count": len(feats), "limited": len(feats) >= limit, "days": days,
                       "pending_files": svc.ingest.get("pending"), "error": svc.ingest.get("error")}
        return out

    @m.route("GET", "/api/intel/protests/{pid}")
    def r_protest(req):
        pid = need(RE_PROTEST, req.params["pid"], "protest")
        ev = db.get_event(store, pid, table="intel_protests")
        if not ev:
            raise HTTPError(404, "NOT_FOUND", "This protest report is no longer in the 14-day window.")
        return svc.protest_detail(ev)

    # -- news spikes ----------------------------------------------------------------------------------
    @m.route("GET", "/api/intel/spikes")
    def r_spikes(req):
        days = req.arg("days", 7.0, type=float, min=0.5, max=90.0)
        min_z = req.arg("min_z", 0.0, type=float, min=0.0, max=20.0)
        res = dict(svc.spikes.result or {})
        flags = [f for f in res.get("flags") or [] if (f.get("z") or 0) >= min_z]
        res["flags"] = flags
        res["episodes"] = [dict(e, entity=svc.spike_entity(e)) for e in db.query_spikes(store, since=time.time() - days * 86400)]
        res["metrics"] = {k: v["label"] for k, v in METRICS.items()}
        return res

    @m.route("GET", "/api/intel/spikes/country/{iso2}")
    def r_spikes_country(req):
        iso2 = need(RE_ISO2, req.params["iso2"], "country").upper()
        return dict(svc.spikes.country(iso2), name=svc.country_name(iso2))

    @m.route("GET", "/api/intel/spikes/{sid}")
    def r_spike(req):
        sid = need(RE_SPIKE, req.params["sid"], "news spike")
        sp = db.get_spike(store, sid)
        if not sp:
            raise HTTPError(404, "NOT_FOUND", "This news spike is not on record (episodes are kept for 90 days).")
        out = dict(sp, entity=svc.spike_entity(sp), name=svc.country_name(sp["iso2"]), flag=codes.flag_emoji(sp["iso2"]))
        out["country_now"] = svc.spikes.country(sp["iso2"])
        return out


# ---------------------------------------------------------------------------------------------
# Search, nearby, events, AI tools
# ---------------------------------------------------------------------------------------------
def register_providers(m, svc) -> None:
    store = m.store

    def search(q: str, limit: int):
        ql = (q or "").strip().lower()
        out = []
        if len(ql) >= 3:
            n = 0
            for p in svc.protest_places:
                place = p.get("place") or ""
                low = place.lower()
                if low.startswith(ql) or (len(ql) >= 4 and f" {ql}" in f" {low}"):
                    out.append({"kind": "place", "id": f"intel-protest-place:{place}",
                                "title": f"Protests: {svc.place_display(place, svc.country_name(p.get('iso2')))}",
                                "subtitle": f"{p['n']} protest reports in 7 days, highest severity {p['max_sev']:.1f}",
                                "lat": round(p["lat"], 5), "lon": round(p["lon"], 5), "layer": "intel.protests",
                                "zoom": 9, "score": 0.4 if low.startswith(ql) else 0.3})
                    n += 1
                    if n >= 4:
                        break
        asn = re.fullmatch(r"as\s?(\d{1,10})", ql)
        if asn:
            num = int(asn.group(1))
            snap = svc.radar.snapshot()
            for h in snap["hijacks"]:
                if num == h.get("hijacker_asn") or num in (h.get("victim_asns") or []):
                    ent = svc.hijack_entity(h)
                    if ent:
                        out.append(dict(ent, score=0.75))
            for lk in snap["leaks"]:
                if num == lk.get("leak_asn"):
                    ent = svc.leak_entity(lk)
                    if ent:
                        out.append(dict(ent, score=0.7))
            for a in snap["anomalies"]:
                if num == a.get("asn"):
                    ent = svc.anomaly_entity(a)
                    if ent:
                        out.append(dict(ent, score=0.8))
        return out[: max(1, limit)]

    m.search(search)

    def nearby(lat: float, lon: float, radius_km: float, limit: int):
        out = []
        for d, ev in db.near_events(store, lat, lon, radius_km, since=time.time() - 7 * 86400, limit=max(1, limit // 2),
                                    table="intel_protests", min_support=True):
            ent = svc.protest_entity(ev)
            ent["distance_km"] = round(d, 2)
            out.append(ent)
        return out

    m.nearby(nearby)

    def events(since_ts: float):
        out = []
        for part in (svc.protest_events, svc.spike_events, svc.advice_events, svc.radar_events):
            try:
                out.extend(part(since_ts))
            except Exception as err:          # one failing part never hides the others
                m.log.warning("intel events (%s) failed: %s", part.__name__, err)
        return out

    m.events(events)

    def _num(args: dict, key: str, default: float) -> float:
        v = args.get(key)
        try:
            return float(v) if v is not None and v != "" else default
        except (TypeError, ValueError):
            return default

    def _country(args: dict, key: str = "country"):
        if not args.get(key):
            return None, None
        iso2 = svc.resolve_country(str(args.get(key))[:80])
        if not iso2:
            return None, {"error": "Unknown country. Give an ISO 3166-1 alpha-2 code or an English name."}
        return iso2, None

    # -- travel_advice ---------------------------------------------------------------------------------
    def tool_advice(args: dict):
        iso2, err = _country(args)
        if err:
            return err
        levels = advice.LEVEL_NAMES
        if iso2:
            c = svc.advice.country(iso2)
            f, u = c["fcdo"], c["us"]
            return {"country": svc.country_name(iso2), "iso2": iso2,
                    "uk_fcdo": {"level": f["level"], "level_name": levels.get(f["level"]), "advice": f["label"],
                                "status_codes": f["statuses"], "level_from": "advice text" if f.get("basis") == "text" else "status codes",
                                "key_sentences": (f.get("sentences") or [])[:4], "latest_update": f.get("change"),
                                "updated": _utc(f.get("updated")), "url": f.get("url")} if f else None,
                    "us_state_department": {"level": u["level"], "wording": u["label"], "summary": (u.get("summary") or "")[:400],
                                            "updated": _utc(u.get("updated")), "url": u.get("url")} if u else None,
                    "recent_changes": [{"source": ch["source"], "time": _utc(ch["t"]), "text": ch["text"]}
                                       for ch in c["changes"][:6]],
                    "scale": "Common scale 1-4: 1 normal precautions, 2 some areas advised against or increased caution, "
                             "3 reconsider travel (FCDO against all but essential travel), 4 do not travel.",
                    "licence": "FCDO: Open Government Licence v3.0. US State Department: US Government work (public domain)."}
        summ = svc.advice.summary()

        def at(src: str, lvl: int) -> list[str]:
            return sorted(svc.country_name(k) for k, v in summ[src].items() if v["l"] == lvl)
        changes = svc.advice.changes(time.time() - max(1.0, min(90.0, _num(args, "days", 14))) * 86400, limit=30)
        return {"uk_fcdo_level_4": at("fcdo", 4), "uk_fcdo_level_3": at("fcdo", 3), "us_level_4": at("us", 4),
                "us_level_3": at("us", 3), "counts": {"fcdo": len(summ["fcdo"]), "us": len(summ["us"])},
                "recent_changes": [{"source": ch["source"], "country": svc.country_name(ch["iso2"]), "time": _utc(ch["t"]),
                                    "text": ch["text"]} for ch in changes],
                "note": "Ask with a country for the full wording."}

    m.ai_tool("travel_advice",
              "Official travel advice for a country from the UK FCDO and the US State Department: a level on a common "
              "1-4 scale with the original wording, key sentences and recent changes. Without a country: the countries at "
              "the top two levels and recent changes.",
              {"type": "object", "properties": {"country": {"type": "string", "description": "ISO alpha-2 code or English name"},
                                                "days": {"type": "number", "minimum": 1, "maximum": 90,
                                                         "description": "Change window when no country is given"}},
               "additionalProperties": False}, tool_advice)

    # -- internet_anomalies ----------------------------------------------------------------------------
    def tool_internet(args: dict):
        iso2, err = _country(args)
        if err:
            return err
        if not svc.radar.has_token():
            out = {"note": "Cloudflare Radar needs CLOUDFLARE_RADAR_TOKEN; only IODA outage signals are available."}
        else:
            out = {}
        now = time.time()
        snap = svc.radar.snapshot()
        an = [svc.anomaly_public(a, now) for a in snap["anomalies"] if not iso2 or a["iso2"] == iso2]
        hj = [svc.hijack_public(h) for h in snap["hijacks"]
              if not iso2 or h.get("hijacker_iso2") == iso2 or iso2 in (h.get("victim_iso2") or [])]
        min_conf = int(max(8, min(12, _num(args, "min_confidence", 10))))
        hj = [h for h in hj if h["confidence"] >= min_conf]
        ioda = [o for o in svc.outages.get("ioda") or [] if not iso2 or o["iso2"] == iso2]
        out.update({"country": svc.country_name(iso2) if iso2 else None,
                    "traffic_anomalies_7d": [{"title": a["title"], "status": a["status_words"], "start": _utc(a["start"]),
                                              "end": _utc(a.get("end")) if a.get("end") else "ongoing",
                                              "cause": (a.get("annotation") or {}).get("cause") or None,
                                              "description": (a.get("annotation") or {}).get("description") or None}
                                             for a in an[:20]],
                    "bgp_hijacks_24h": [{"title": h["title"], "confidence": h["confidence"], "ongoing": h["ongoing"],
                                         "prefixes": h["prefixes"][:4], "tags": [t["name"] for t in h["tags"]][:4],
                                         "start": _utc(h["start"])} for h in hj[:20]],
                    "ioda_outage_signals_24h": [{"country": o["name"], "events": o["events"], "signals": o["methods"]}
                                                for o in ioda[:15]],
                    "caveat": "BGP detections include false positives; confidence runs to 12. Traffic anomalies can come "
                              "from power cuts, cable faults or shutdowns.",
                    "licence": "Cloudflare Radar CC BY-NC 4.0; IODA research use with credit."})
        return out

    m.ai_tool("internet_anomalies",
              "Internet disruptions: Cloudflare Radar verified traffic anomalies (7 days) and possible BGP hijacks (24 hours), "
              "plus IODA outage signals, worldwide or for one country.",
              {"type": "object", "properties": {"country": {"type": "string", "description": "ISO alpha-2 code or English name"},
                                                "min_confidence": {"type": "integer", "minimum": 8, "maximum": 12}},
               "additionalProperties": False}, tool_internet)

    # -- protests --------------------------------------------------------------------------------------
    def tool_protests(args: dict):
        iso2, err = _country(args)
        if err:
            return err
        days = max(0.25, min(14.0, _num(args, "days", 3)))
        limit = max(1, min(100, int(_num(args, "limit", 30))))
        min_sev = max(0.0, min(10.0, _num(args, "min_severity", 3)))
        cat = args.get("kind")
        if cat and cat not in protests.CATEGORY_LABEL:
            return {"error": "kind must be one of: " + ", ".join(protests.CATEGORY_LABEL)}
        bbox = None
        if args.get("bbox"):
            try:
                bbox = geo.parse_bbox(str(args["bbox"]))
            except ValueError:
                return {"error": "bbox must be west,south,east,north"}
        rows = db.query_events(store, bbox=bbox, since=time.time() - days * 86400, min_severity=min_sev, iso2=iso2,
                               table="intel_protests", min_support=True, category=cat, limit=limit)
        return {"count": len(rows), "days": days, "country": iso2,
                "protests": [{"id": ev["id"], "action": protests.label(ev["code"]),
                              "type": protests.CATEGORY_LABEL.get(ev["category"]), "place": ev["place"],
                              "country": ev["iso2"], "lat": round(ev["lat"], 4), "lon": round(ev["lon"], 4),
                              "severity": ev["severity"], "reported": _utc(ev["t_last"]), "actors": ev["actors"][:4],
                              "sources": ev["sources"], "articles": ev["articles"],
                              "source_url": (ev["urls"] or [None])[0]} for ev in rows],
                "caveat": DISCLAIMER + " Only reports carried by 2 or more sources or 3 or more articles are listed."}

    m.ai_tool("protests",
              "Recent protests, strikes, blockades and riots machine-coded by GDELT from news (CAMEO root 14), "
              "deduplicated by place and day, with a 0-10 severity. Filter by country, bounding box, kind and days (up to 14).",
              {"type": "object", "properties": {
                  "country": {"type": "string", "description": "ISO alpha-2 code or English name"},
                  "bbox": {"type": "string", "description": "west,south,east,north"},
                  "kind": {"type": "string", "enum": list(protests.CATEGORY_LABEL)},
                  "days": {"type": "number", "minimum": 0.25, "maximum": 14},
                  "min_severity": {"type": "number", "minimum": 0, "maximum": 10},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
               "additionalProperties": False}, tool_protests)

    # -- news_spikes -----------------------------------------------------------------------------------
    def tool_spikes(args: dict):
        iso2, err = _country(args)
        if err:
            return err
        res = svc.spikes.result or {}
        flags = [f for f in res.get("flags") or [] if not iso2 or f["iso2"] == iso2]
        days = max(1.0, min(90.0, _num(args, "days", 7)))
        eps = db.query_spikes(store, since=time.time() - days * 86400, iso2=iso2, limit=50)
        return {"state": res.get("state"), "message": res.get("message"), "baseline_days": res.get("baseline_days"),
                "current": [{k: f.get(k) for k in ("name", "metric", "level", "reason", "value", "baseline", "ratio", "z", "p",
                                                   "text")} for f in flags[:30]],
                "episodes": [{"country": svc.country_name(e["iso2"]), "metric": e["metric"], "from": _utc(e["first_t"]),
                              "to": _utc(e["last_t"]), "peak_ratio": e["peak_ratio"], "severity": e["severity"]} for e in eps],
                "method": "Per-country GDELT rows in the last 24 hours against up to 28 earlier days, adjusted for total "
                          "GDELT volume (robust z, or Poisson for small counts). Unusual = z 3+ and twice the usual level.",
                "caveat": "Unusually high news volume is not evidence of a crisis; one widely repeated story can cause it."}

    m.ai_tool("news_spikes",
              "Countries whose GDELT news volume, conflict reports or protest reports are unusually high against their own "
              "baseline, now and recently (episodes).",
              {"type": "object", "properties": {"country": {"type": "string"},
                                                "days": {"type": "number", "minimum": 1, "maximum": 90}},
               "additionalProperties": False}, tool_spikes)
