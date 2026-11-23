"""HTTP routes, AI tools and discovery providers for the flights module."""

from __future__ import annotations

import re
import time

from meridian import geo
from meridian.registry import HTTPError

from .const import CLASSES, GPS_WINDOW_H, pretty_name
from .gpsjam import METHOD as GPS_METHOD
from .parse import clean_hex

_IDENT = re.compile(r"^[A-Za-z0-9-]{2,12}$")
_UID = re.compile(r"^[0-9]{1,12}$")


def _hex(value: str) -> str:
    h = clean_hex(value)
    if h is None:
        raise HTTPError(400, "BAD_ARG", "An aircraft id is a six-character ICAO hex code, like 4ca2b3.")
    return h


def register_routes(m, svc) -> None:
    @m.route("GET", "/api/flights/live")
    def live(req):
        bbox = req.bbox("bbox")
        zoom = req.arg("zoom", 6.0, type=float, min=0.0, max=24.0)
        return svc.live(bbox, zoom)

    @m.route("GET", "/api/flights/military")
    def military(req):
        return svc.military()

    @m.route("GET", "/api/flights/alerts")
    def alerts(req):
        return svc.alerts()

    @m.route("GET", "/api/flights/aircraft/{hex}")
    def aircraft(req):
        return svc.aircraft(_hex(req.params["hex"]))

    @m.route("GET", "/api/flights/trail/{hex}")
    def trail(req):
        return svc.trail(_hex(req.params["hex"]))

    @m.route("GET", "/api/flights/airport/{ident}")
    def airport(req):
        ident = req.params["ident"]
        if not _IDENT.match(ident):
            raise HTTPError(400, "BAD_ARG", "The airport id should be an ICAO or IATA code.")
        lat, lon = req.latlon()
        radius = req.arg("radius_km", 40.0, type=float, min=5.0, max=100.0)
        return svc.airport(ident, lat, lon, radius)

    @m.route("GET", "/api/flights/gpsjam")
    def gpsjam(req):
        bbox = req.bbox("bbox", (-180.0, -85.0, 180.0, 85.0))
        zoom = req.arg("zoom", 5.0, type=float, min=0.0, max=24.0)
        hours = req.arg("hours", GPS_WINDOW_H, type=int, min=1, max=48)
        return svc.gpsjam(bbox, zoom, hours)

    @m.route("GET", "/api/flights/gpsjam/cell")
    def gpsjam_cell(req):
        lat, lon = req.latlon()
        hours = req.arg("hours", GPS_WINDOW_H, type=int, min=1, max=48)
        agg = req.arg("agg", 1, type=int, choices={1, 2})
        return svc.gpsjam_cell(lat, lon, hours, agg)

    @m.route("GET", "/api/flights/sanctioned/{uid}")
    def sanctioned(req):
        uid = req.params["uid"]
        if not _UID.match(uid):
            raise HTTPError(400, "BAD_ARG", "A sanctions record id is a number.")
        out = svc.sanctioned(uid)
        if out is None:
            raise HTTPError(404, "NOT_FOUND", "That aircraft is not on the loaded OFAC list.")
        return out

    @m.route("GET", "/api/flights/status")
    def status(req):
        return svc.status()


def register_discovery(m, svc) -> None:
    def search(q: str, limit: int):
        return svc.tracker.search(q, limit, time.time())

    def nearby(lat: float, lon: float, radius_km: float, limit: int):
        now = time.time()
        out = []
        near = [t for t in svc.tracker.near(lat, lon, min(radius_km, 150.0), now, live_only=True)
                if t[1].cls != "ground"]
        for d, ac, la, lo, _est in near[:limit]:
            ent = ac.entity(now)
            alt = "on the ground" if ac.gnd else (f"{ac.alt:,} ft" if ac.alt is not None else None)
            ent["subtitle"] = ", ".join(x for x in (pretty_name(ac.desc) or ac.typ, alt) if x) or "Aircraft"
            ent["distance_km"] = round(d, 2)
            out.append(ent)
        return out

    m.search(search)
    m.nearby(nearby)
    m.events(svc.events)


# ---------------------------------------------------------------------------------------------
# AI tools (read-only, under 10 s)
# ---------------------------------------------------------------------------------------------
def _num(args: dict, key: str, lo: float, hi: float, default=None):
    v = args.get(key, default)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if not (lo <= f <= hi):
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return f


def _area(args: dict) -> tuple[float, float, float]:
    bbox = args.get("bbox")
    if bbox is not None:
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise ValueError("bbox must be [west, south, east, north]")
        w, s, e, n = geo.parse_bbox(",".join(str(float(x)) for x in bbox))
        clat = (s + n) / 2
        if w > e:
            e += 360
        clon = geo.wrap_lon((w + e) / 2)
        radius = max(geo.haversine_km(clat, clon, s, w), geo.haversine_km(clat, clon, n, geo.wrap_lon(e)))
        return clat, clon, min(500.0, radius)
    lat = _num(args, "lat", -90, 90)
    lon = _num(args, "lon", -180, 180)
    if lat is None or lon is None:
        raise ValueError("give lat and lon (with radius_km) or a bbox")
    return lat, lon, _num(args, "radius_km", 1, 500, 50.0)


def register_ai(m, svc) -> None:
    def flights_in_area(args: dict):
        lat, lon, radius = _area(args)
        mil_only = bool(args.get("military_only"))
        cls = args.get("class")
        limit = int(_num(args, "limit", 1, 50, 20))
        coverage = svc.ensure_area(lat, lon, radius, wait=6.0)
        now = time.time()
        near = svc.tracker.near(lat, lon, radius, now, live_only=False)
        if mil_only:
            near = [t for t in near if t[1].military]
        if cls in CLASSES:
            near = [t for t in near if t[1].cls == cls]
        by_class: dict[str, int] = {}
        for _, ac, *_ in near:
            by_class[ac.cls] = by_class.get(ac.cls, 0) + 1
        aircraft = []
        for d, ac, la, lo, est in near[:limit]:
            s = ac.summary(now)
            s["distance_km"] = round(d, 1)
            aircraft.append(s)
        return {"center": [round(lat, 4), round(lon, 4)], "radius_km": round(radius, 1), "count": len(near),
                "by_class": by_class, "aircraft": aircraft,
                "estimated": sum(1 for t in near if t[4]),
                "coverage": coverage,
                "note": "Live ADS-B positions (adsb.fi first). Estimated positions are dead-reckoned, not observed."}

    m.ai_tool("flights_in_area",
              "Live aircraft in an area: counts by class (airliner, cargo, business, military, helicopter, light, "
              "glider, ground, unknown) and the nearest aircraft with callsign, type, altitude and speed.",
              {"type": "object", "properties": {
                  "lat": {"type": "number"}, "lon": {"type": "number"},
                  "radius_km": {"type": "number", "minimum": 1, "maximum": 500},
                  "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                           "description": "[west, south, east, north]"},
                  "military_only": {"type": "boolean"},
                  "class": {"type": "string", "enum": sorted(CLASSES)},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
               "additionalProperties": False},
              flights_in_area)

    def aircraft_details(args: dict):
        ac = svc.find(hexid=args.get("hex"), callsign=args.get("callsign"), registration=args.get("registration"))
        if ac is None:
            hexid = clean_hex(args.get("hex") or "")
            if hexid:
                d = svc.aircraft(hexid, wait=4.0)
                return {"found": bool(d.get("known")), "live": False, "details": _compact(d)}
            return {"found": False, "note": "No live aircraft matches. It may be on the ground with its transponder "
                                            "off or outside ADS-B coverage."}
        d = svc.aircraft(ac.hex, wait=4.0)
        return {"found": True, "live": d.get("live"), "details": _compact(d)}

    m.ai_tool("aircraft_details",
              "Details of one aircraft by ICAO hex, callsign or registration: position, altitude, speed, squawk, "
              "type, operator (private owners are not named), route (unverified) and any OFAC sanctions listing.",
              {"type": "object", "properties": {
                  "hex": {"type": "string", "pattern": "^[0-9a-fA-F]{6}$"},
                  "callsign": {"type": "string", "maxLength": 8},
                  "registration": {"type": "string", "maxLength": 12}},
               "additionalProperties": False},
              aircraft_details)

    def flights_emergencies(args: dict):
        hours = _num(args, "hours", 0.5, 48, 6)
        data = svc.alerts()
        now = time.time()
        current = []
        for d in data["details"]:
            ac = svc.tracker.get(d["hex"])
            if ac is not None:
                s = ac.summary(now)
                s["emergency_label"] = d["label"]
                current.append(s)
        recent = svc.events(now - hours * 3600)
        recent = [e for e in recent if e.get("event") == "emergency"][:40]
        return {"current": current, "recent": recent, "hours": hours,
                "note": "Squawk 7500 unlawful interference, 7600 radio failure, 7700 general emergency. Brief "
                        "squawks can be mistakes; check the track before drawing conclusions."}

    m.ai_tool("flights_emergencies",
              "Aircraft worldwide squawking 7500, 7600 or 7700 (or reporting an emergency status) right now, plus "
              "emergency events seen in the last hours.",
              {"type": "object", "properties": {"hours": {"type": "number", "minimum": 0.5, "maximum": 48}},
               "additionalProperties": False},
              flights_emergencies)

    def gps_interference(args: dict):
        hours = int(_num(args, "hours", 1, 48, GPS_WINDOW_H))
        bbox = None
        if args.get("bbox") is not None or args.get("lat") is not None:
            lat, lon, radius = _area(args)
            bbox = geo.bbox_of_circle(lat, lon, radius)
        cells = svc.gps.cells(hours=hours, bbox=bbox, now=time.time())
        flagged = sorted((c for c in cells if c["level"] in ("amber", "red")), key=lambda c: -c["share"])
        out = [{"south": c["lat0"], "west": c["lon0"], "size_deg": c["size"], "affected_share": c["share"],
                "aircraft_hours": c["total"], "affected": c["bad"], "level": c["level"],
                "last_affected": c["last_bad_t"]} for c in flagged[:40]]
        return {"hours": hours, "cells_with_data": len(cells), "flagged": len(flagged), "cells": out,
                "method": GPS_METHOD}

    m.ai_tool("gps_interference",
              "GPS interference cells (0.5 degree) derived from aircraft-reported navigation accuracy over the last "
              "hours, worldwide or in an area. Amber means 2 to 10% of aircraft affected, red 10% or more.",
              {"type": "object", "properties": {
                  "lat": {"type": "number"}, "lon": {"type": "number"},
                  "radius_km": {"type": "number", "minimum": 1, "maximum": 500},
                  "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                  "hours": {"type": "integer", "minimum": 1, "maximum": 48}},
               "additionalProperties": False},
              gps_interference)


def _compact(d: dict) -> dict:
    keep = ("hex", "title", "subtitle", "live", "estimated", "callsign", "registration", "type", "description",
            "class_label", "lat", "lon", "alt_ft", "on_ground", "gs_kt", "track", "vr_fpm", "squawk", "squawk_label",
            "emergency", "category_label", "position_source", "provider", "position_age_s", "military", "operator",
            "route_text", "route_check", "sanctions", "last_seen", "privacy")
    out = {k: d.get(k) for k in keep if d.get(k) is not None}
    if d.get("route"):
        r = d["route"]
        out["route"] = {"origin": (r.get("origin") or {}).get("name"), "destination": (r.get("destination") or {}).get("name"),
                        "airline": (r.get("airline") or {}).get("name"), "verified": False}
    return out


