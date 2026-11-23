"""Routes, AI tools and discovery for airspace and airport status (US airport delays, US temporary
flight restrictions, EASA conflict-zone bulletins)."""

from __future__ import annotations

import re
import time

from meridian import geo
from meridian.registry import HTTPError

from . import shapes
from .airspace import CZIB_LEVEL_LABELS, NAS_NAME
from .api import _area, _num

_IDENT = re.compile(r"^[A-Za-z0-9-]{2,12}$")
_NID = re.compile(r"^\d_\d{4}$")
_CZIB_ID = re.compile(r"^czib-\d{4}-\d{2}$")
NAS_NOTE = ("US airports only, from the FAA. A ground stop holds flights bound for the airport at their departure "
            "airports; a ground delay programme gives them departure slots, so they wait on the ground. Times are UTC.")


def _iso(t) -> str | None:
    if not isinstance(t, (int, float)):
        return None
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(t))


def register_airspace_routes(m, air) -> None:
    @m.route("GET", "/api/flights/airport-status")
    def airport_status_all(req):
        return air.delays_fc()

    @m.route("GET", "/api/flights/airport-status/{ident}")
    def airport_status_one(req):
        ident = req.params["ident"]
        if not _IDENT.match(ident):
            raise HTTPError(400, "BAD_ARG", "The airport id should be an ICAO, FAA or IATA code, like KSFO or SFO.")
        lat = req.arg("lat", None, type=float, min=-90.0, max=90.0)
        lon = req.arg("lon", None, type=float, min=-180.0, max=180.0)
        out = air.airport_status(ident, lat, lon)
        if out is None:
            if not air.airports_t and air.nas_feed.error:
                raise HTTPError(503, "SOURCE_DOWN", "FAA NAS Status is not answering right now, so delays are unknown.")
            raise HTTPError(404, "NOT_FOUND", "The FAA reports no delay, closure or notice for this airport right now. "
                                              "Only US airports are covered.")
        return out

    @m.route("GET", "/api/flights/tfrs")
    def tfrs(req):
        return air.tfrs_fc()

    @m.route("GET", "/api/flights/tfrs/{nid}")
    def tfr_one(req):
        nid = req.params["nid"]
        if not _NID.match(nid):
            raise HTTPError(400, "BAD_ARG", "A restriction id is the NOTAM number with an underscore, like 6_9435.")
        t = air.tfr(nid)
        if t is None:
            raise HTTPError(404, "NOT_FOUND", "That flight restriction is not on the FAA list any more. It may have "
                                              "expired or been cancelled.")
        return air.tfr_view(t)

    @m.route("GET", "/api/flights/advisories")
    def advisories(req):
        return air.czibs_fc()

    @m.route("GET", "/api/flights/advisories/{cid}")
    def advisory_one(req):
        cid = req.params["cid"].lower()
        if not _CZIB_ID.match(cid):
            raise HTTPError(400, "BAD_ARG", "A bulletin id looks like czib-2022-01.")
        c = air.czib(cid)
        if c is None:
            raise HTTPError(404, "NOT_FOUND", "EASA lists no bulletin with that number.")
        return air.czib_view(c)

    @m.route("GET", "/api/flights/airspace/at")
    def airspace_at(req):
        lat, lon = req.latlon()
        return air.at(lat, lon)

    @m.route("GET", "/api/flights/airspace/status")
    def airspace_status(req):
        return air.status()


def register_airspace_discovery(m, air) -> None:
    m.search(air.search)
    m.nearby(air.nearby)
    m.events(air.events)


# ---------------------------------------------------------------------------------------------
# AI tools (read-only, under 10 s)
# ---------------------------------------------------------------------------------------------
_TYPES = {"ground_stop", "ground_delay", "arrival_delay", "departure_delay", "closure", "any"}


def _status_row(st: dict) -> dict:
    row = {"type": st["type"], "code": st["code"], "reason": st.get("reason"), "summary": st.get("summary"),
           "avg_delay_min": st.get("avg_delay_min"), "min_delay_min": st.get("min_delay_min"),
           "max_delay_min": st.get("max_delay_min"), "trend": st.get("trend"),
           "start_utc": _iso(st.get("start")), "end_utc": _iso(st.get("end")), "advisory": st.get("advisory")}
    return {k: v for k, v in row.items() if v is not None}


def register_airspace_ai(m, air) -> None:
    def airport_delays(args: dict):
        typ = args.get("type") or "any"
        if typ not in _TYPES:
            raise ValueError(f"type must be one of {sorted(_TYPES)}")
        min_delay = _num(args, "min_delay_min", 0, 1440, None)
        limit = int(_num(args, "limit", 1, 100, 30))
        airport = args.get("airport")
        air.ensure_nas(max_age=180.0, wait=6.0)
        updated = _iso(air.airports_t)
        if airport is not None:
            if not isinstance(airport, str) or not _IDENT.match(airport):
                raise ValueError("airport must be an ICAO, FAA or IATA code")
            a = air.find_airport(airport)
            if a is None:
                return {"found": False, "airport": airport.upper(), "updated_utc": updated, "source": NAS_NAME,
                        "note": "No FAA delay, closure or notice for this airport right now. " + NAS_NOTE}
            return {"found": True, "airport": {"faa": a["faa"], "icao": a.get("icao"), "name": a["name"],
                                               "lat": a["lat"], "lon": a["lon"]},
                    "statuses": [_status_row(s) for s in a["statuses"]], "runways": a.get("runways"),
                    "updated_utc": updated, "source": NAS_NAME, "note": NAS_NOTE}
        rows = []
        counts: dict[str, int] = {}
        for a in air.airports.values():
            sts = []
            for s in a["statuses"]:
                if typ != "any" and s["code"] != typ:
                    continue
                if typ == "any" and not s["delay"]:
                    continue
                if min_delay is not None and max(s.get("max_delay_min") or 0, s.get("avg_delay_min") or 0) < min_delay \
                        and s["code"] not in ("ground_stop", "closure"):
                    continue
                sts.append(s)
            if not sts:
                continue
            for s in sts:
                counts[s["code"]] = counts.get(s["code"], 0) + 1
            rows.append((max(s["level"] for s in sts), max(s.get("max_delay_min") or s.get("avg_delay_min") or 0 for s in sts),
                         {"faa": a["faa"], "icao": a.get("icao"), "name": a["name"], "lat": a["lat"], "lon": a["lon"],
                          "statuses": [_status_row(s) for s in sts]}))
        rows.sort(key=lambda r: (-r[0], -r[1]))
        out = {"source": NAS_NAME, "updated_utc": updated, "count": len(rows), "counts_by_type": counts,
               "airports": [r[2] for r in rows[:limit]], "note": NAS_NOTE}
        if air.nas_feed.error and not air.airports_t:
            out["error"] = "FAA NAS Status is not answering right now."
        return out

    m.ai_tool("airport_delays",
              "Current US airport delays from FAA NAS Status: ground stops, ground delay programmes, arrival and "
              "departure delays and closures, with reasons, average and maximum delay, trend and times in UTC. "
              "Give an airport (ICAO, FAA or IATA code) for one airport.",
              {"type": "object", "properties": {
                  "airport": {"type": "string", "maxLength": 12, "description": "ICAO, FAA or IATA code, e.g. KSFO or SFO"},
                  "type": {"type": "string", "enum": sorted(_TYPES)},
                  "min_delay_min": {"type": "number", "minimum": 0, "maximum": 1440},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
               "additionalProperties": False},
              airport_delays)

    def airspace_restrictions(args: dict):
        kind = args.get("kind") or "all"
        if kind not in ("all", "tfr", "conflict_zone"):
            raise ValueError("kind must be all, tfr or conflict_zone")
        active_only = args.get("active_only", True)
        if not isinstance(active_only, bool):
            raise ValueError("active_only must be true or false")
        limit = int(_num(args, "limit", 1, 100, 25))
        country = args.get("country")
        if country is not None and (not isinstance(country, str) or not 2 <= len(country.strip()) <= 60):
            raise ValueError("country must be an ISO code or a country name")
        cc = country.strip() if country else None
        area = None
        if args.get("bbox") is not None or args.get("lat") is not None or args.get("lon") is not None:
            area = _area(args)
        out: dict = {}
        if kind in ("all", "tfr"):
            air.ensure_tfr(wait=4.0)
            items = []
            us = cc is None or cc.upper() in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA")
            for t in air.tfrs.values() if us else []:
                if t["status"] == "ended" or (active_only and t["status"] == "upcoming"):
                    continue
                dist = None
                if area:
                    if not t.get("center"):
                        continue
                    lat, lon, radius = area
                    dist = geo.haversine_km(lat, lon, t["center"][1], t["center"][0])
                    inside = shapes.contains(t.get("geometry"), lat, lon)
                    if not inside and dist > radius + (t.get("radius_km") or 0):
                        continue
                    dist = 0.0 if inside else round(dist, 1)
                row = {"id": t["id"], "type": t["type_label"], "place": t.get("place"), "status": t["status"],
                       "start_utc": _iso(t.get("start")), "end_utc": _iso(t.get("end")),
                       "until_further_notice": t.get("permanent") or None, "floor": t.get("lower"),
                       "ceiling": t.get("upper"), "schedule": t.get("schedule"), "rule": t.get("rule_text"),
                       "purpose": t.get("purpose"), "near_city": t.get("city"), "mapped": bool(t.get("polygons")),
                       "center": [t["center"][1], t["center"][0]] if t.get("center") else None,
                       "distance_km": dist, "link": t["link"]}
                items.append({k: v for k, v in row.items() if v is not None})
            order = {"vip": 0, "space": 1, "hazards": 2, "sports": 3, "security": 4, "drones": 5, "other": 6}
            if area:
                items.sort(key=lambda r: r.get("distance_km") or 0)
            else:
                items.sort(key=lambda r: (order.get((air.tfrs.get(r["id"]) or {}).get("category"), 9),
                                          -((air.tfrs.get(r["id"]) or {}).get("issued") or 0)))
            out["tfrs"] = {"count": len(items), "items": items[:limit],
                           "note": "US temporary flight restrictions from the FAA (tfr.faa.gov); other countries are "
                                   "not covered. Altitudes MSL unless marked above ground. Times UTC."}
            if not air.tfr_t:
                out["tfrs"]["error"] = ("tfr.faa.gov is not answering right now." if air.tfr_feed.error
                                        else "The FAA list is still loading; ask again in a moment.")
        if kind in ("all", "conflict_zone"):
            air.ensure_czib(wait=4.0)
            items = []
            box = geo.bbox_of_circle(*area) if area else None
            for c in air.active_czibs():
                if cc:
                    names = {x["name"].lower() for x in c.get("countries") or []} | {x["iso2"] for x in c.get("countries") or []}
                    if cc.lower() not in names and cc.upper() not in names:
                        continue
                if area:
                    lat, lon, _ = area
                    b = c.get("bbox")
                    if not (shapes.contains(c.get("geometry"), lat, lon) or
                            (b and box and not (b[2] < box[0] or b[0] > box[2] or b[3] < box[1] or b[1] > box[3]))):
                        continue
                reco = c.get("recommendation") or ""
                items.append({"id": c["id"], "number": c["number"], "subject": c["subject"],
                              "countries": [x["name"] for x in c.get("countries") or []],
                              "recommendation_level": CZIB_LEVEL_LABELS.get(c["level"]),
                              "altitudes": c.get("altitudes"), "covers_part_only": bool(c.get("partial")),
                              "issued_utc": _iso(c.get("issued")), "valid_until_utc": _iso(c.get("valid_until")),
                              "recommendation": reco[:900] + ("…" if len(reco) > 900 else ""),
                              "firs": c.get("firs"), "link": c.get("link")})
            out["conflict_zones"] = {"count": len(items), "items": items[:limit],
                                     "note": "EASA Conflict Zone Information Bulletins (recommendations to EU operators; "
                                             "national authorities may go further)."}
            if not air.czibs:
                out["conflict_zones"]["error"] = ("EASA is not answering right now." if air.czib_feed.error
                                                  else "The EASA list is still loading; ask again in a moment.")
        return out

    m.ai_tool("airspace_restrictions",
              "Airspace restrictions: US temporary flight restrictions (VIP movement, security, hazards such as "
              "wildfires, space operations, sporting events) with altitudes and times, and EASA conflict-zone "
              "bulletins with their recommendation and validity. Filter by area (lat/lon with radius_km, or bbox) "
              "or country.",
              {"type": "object", "properties": {
                  "kind": {"type": "string", "enum": ["all", "tfr", "conflict_zone"]},
                  "lat": {"type": "number"}, "lon": {"type": "number"},
                  "radius_km": {"type": "number", "minimum": 1, "maximum": 500},
                  "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                           "description": "[west, south, east, north]"},
                  "country": {"type": "string", "maxLength": 60, "description": "ISO code or name, e.g. UA or Ukraine"},
                  "active_only": {"type": "boolean", "description": "Leave out restrictions that start later (default true)"},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
               "additionalProperties": False},
              airspace_restrictions)
