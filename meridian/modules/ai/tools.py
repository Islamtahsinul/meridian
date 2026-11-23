"""The assistant's own tools.

Data tools look across every module through the App (search, nearby, events, layers) plus
OpenStreetMap Nominatim for place names. Map tools do nothing on the server: they are validated,
collected and returned to the browser, which runs them through ``ctx.ai.run`` after the answer.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from meridian import geo
from meridian.core_api import NOMINATIM

from .util import (LAYER_ID_RE, as_float, as_int, clamp, compact_entity, iso_utc, parse_bbox,
                   str_list)

BASEMAPS = ("dark", "relief", "light", "streets", "satellite", "sentinel")
NOMINATIM_SOURCE = "OpenStreetMap Nominatim (ODbL)"


class ToolInputError(ValueError):
    """Bad arguments from the model: reported back to it as an error result."""


@dataclass
class ToolContext:
    app: Any
    net: Any
    view: dict = field(default_factory=dict)
    log: Any = None
    wall: Callable[[], float] = time.time


def _obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


_LATLON = {"lat": {"type": "number", "description": "Latitude in decimal degrees (-90 to 90)."},
           "lon": {"type": "number", "description": "Longitude in decimal degrees (-180 to 180)."}}
_KINDS = {"type": "array", "items": {"type": "string"},
          "description": 'Optional entity kinds to keep, for example ["aircraft", "ship", "camera"].'}

DATA_TOOLS: list[dict] = [
    {"name": "search_entities",
     "description": ("Search everything Meridian has loaded by name or identifier: aircraft (callsign, "
                     "registration, ICAO hex), ships (name, MMSI, IMO), cameras, airports, ports, power plants, "
                     "companies, sanctioned vessels and aircraft, saved favourites and more. Call it when the user "
                     "names a specific thing. Returns entities with kind, id, title, position and the layer that "
                     "shows them. It does not geocode towns or regions: use find_place for those."),
     "input_schema": _obj({"query": {"type": "string", "description": "Name or identifier, at least 2 characters."},
                           "kinds": _KINDS,
                           "limit": {"type": "integer", "description": "Maximum results, 1 to 25 (default 10)."}},
                          ("query",))},
    {"name": "find_place",
     "description": ("Find the position and extent of a named place (town, region, country, sea, strait, "
                     "airport, landmark) with OpenStreetMap Nominatim. Use it when you need coordinates or a "
                     "bounding box to fly there or to search an area and you aren't sure of them. One place per call."),
     "input_schema": _obj({"query": {"type": "string", "description": "Place name, for example \"Strait of Hormuz\"."},
                           "limit": {"type": "integer", "description": "Maximum results, 1 to 5 (default 3)."}},
                          ("query",))},
    {"name": "whats_here",
     "description": ("Describe a point: its OpenStreetMap place name and everything Meridian's running modules "
                     "have near it (aircraft, ships, cameras, airports, ports, plants, events...), nearest first, "
                     "with distances in km. Use it for 'what is near X', 'which cameras are near Heathrow' or "
                     "'what is at these coordinates'."),
     "input_schema": _obj({**_LATLON,
                           "radius_km": {"type": "number", "description": "Search radius in km, 0.1 to 500 (default 25)."},
                           "kinds": _KINDS,
                           "limit": {"type": "integer", "description": "Maximum entities, 1 to 50 (default 25)."}},
                          ("lat", "lon"))},
    {"name": "recent_events",
     "description": ("Notable recent events from every running module, with severity 0 to 10: earthquakes, fires, "
                     "storms, floods, warnings, outbreaks, conflict events, news, outages, GPS jamming, emergency "
                     "squawks, dark or sanctioned ships and more. Use it for 'what's happening', 'any strong "
                     "earthquakes today' or 'recent incidents near X'. Filter by area (bbox, or in_view for the "
                     "user's current view), kinds, time window and minimum severity."),
     "input_schema": _obj({"hours": {"type": "number", "description": "How far back to look, 0.25 to 720 hours (default 24)."},
                           "kinds": _KINDS,
                           "bbox": {"type": "array", "items": {"type": "number"},
                                    "description": "Area as [west, south, east, north] in degrees."},
                           "in_view": {"type": "boolean", "description": "Only events inside the user's current map view."},
                           "min_severity": {"type": "number", "description": "Lowest severity to include, 0 to 10."},
                           "sort": {"type": "string", "enum": ["severity", "time"],
                                    "description": "Most severe first (default) or newest first."},
                           "limit": {"type": "integer", "description": "Maximum events, 1 to 100 (default 40)."}})},
    {"name": "list_layers",
     "description": ("List the map layers: id, title, group, whether each is on, its item count and status, and "
                     "its data source. Use it before set_layers when you aren't sure of a layer id, or to say which "
                     "source a layer uses."),
     "input_schema": _obj({"group": {"type": "string", "description": "Optional group, for example \"Ships\" or \"Events\"."}})},
]

_BROWSER = " Runs in the user's browser after your answer arrives."

ACTION_TOOLS: list[dict] = [
    {"name": "fly_to",
     "description": ("Move the map to a point so the user can see it. zoom: about 3 for a continent, 5 for a "
                     "country, 7 to 9 for a strait or region, 10 to 12 for a city or airport, 14 for streets." + _BROWSER),
     "input_schema": _obj({**_LATLON, "zoom": {"type": "number", "description": "Zoom level, 1 to 18."}}, ("lat", "lon"))},
    {"name": "fit_bounds",
     "description": "Fit the map to an area: a sea, a country, or a spread of results." + _BROWSER,
     "input_schema": _obj({"west": {"type": "number"}, "south": {"type": "number"},
                           "east": {"type": "number"}, "north": {"type": "number"}},
                          ("west", "south", "east", "north"))},
    {"name": "set_layers",
     "description": ("Turn map layers on or off by id (see list_layers). Turn on the layers that show your "
                     "answer." + _BROWSER),
     "input_schema": _obj({"on": {"type": "array", "items": {"type": "string"}, "description": "Layer ids to turn on."},
                           "off": {"type": "array", "items": {"type": "string"}, "description": "Layer ids to turn off."}})},
    {"name": "set_basemap",
     "description": ("Change the background map: dark (default), relief (hill shading), light, streets, "
                     "satellite (Esri imagery) or sentinel (Sentinel-2 2020 mosaic)." + _BROWSER),
     "input_schema": _obj({"id": {"type": "string", "enum": list(BASEMAPS)}}, ("id",))},
    {"name": "set_projection",
     "description": "Show the map as a globe or as a flat (Mercator) map." + _BROWSER,
     "input_schema": _obj({"projection": {"type": "string", "enum": ["globe", "mercator"]}}, ("projection",))},
    {"name": "set_terrain",
     "description": "Turn 3D terrain on or off (useful for mountains, valleys and coasts)." + _BROWSER,
     "input_schema": _obj({"on": {"type": "boolean"}}, ("on",))},
    {"name": "open_entity",
     "description": ("Open the information card for one entity from a tool result, with its kind, id, title, "
                     "lat, lon and layer exactly as returned. Use it when the answer is about one specific "
                     "thing." + _BROWSER),
     "input_schema": _obj({"entity": {"type": "object", "description": "The entity as a tool returned it.",
                                      "properties": {"kind": {"type": "string"}, "id": {"type": "string"},
                                                     "title": {"type": "string"}, "lat": {"type": "number"},
                                                     "lon": {"type": "number"}, "layer": {"type": "string"}},
                                      "required": ["kind", "id"]}}, ("entity",))},
    {"name": "highlight",
     "description": "Draw a highlight ring at a point on the map." + _BROWSER,
     "input_schema": _obj(dict(_LATLON), ("lat", "lon"))},
    {"name": "whats_here_card",
     "description": "Open Meridian's \"What's here?\" card at a point, listing everything nearby." + _BROWSER,
     "input_schema": _obj(dict(_LATLON), ("lat", "lon"))},
]

DATA_TOOL_NAMES = tuple(t["name"] for t in DATA_TOOLS)
ACTION_TOOL_NAMES = tuple(t["name"] for t in ACTION_TOOLS)
ACTION_TYPES = {"fly_to": "fly_to", "fit_bounds": "fit_bounds", "set_layers": "set_layers",
                "set_basemap": "set_basemap", "set_projection": "set_projection", "set_terrain": "set_terrain",
                "open_entity": "open_entity", "highlight": "highlight", "whats_here_card": "whats_here"}


# ---------------------------------------------------------------------------------------------
# Data tools
# ---------------------------------------------------------------------------------------------
def _query(args: dict, key: str = "query", max_len: int = 200) -> str:
    q = args.get(key)
    if not isinstance(q, str) or len(q.strip()) < 2:
        raise ToolInputError(f'"{key}" needs at least 2 characters.')
    return q.strip()[:max_len]


def _latlon(args: dict) -> tuple[float, float]:
    lat = as_float(args.get("lat"), None, -90, 90)
    lon = as_float(args.get("lon"), None, -180, 180)
    if lat is None or lon is None:
        raise ToolInputError("lat must be -90 to 90 and lon -180 to 180 (decimal degrees).")
    return lat, lon


def search_entities(args: dict, ctx: ToolContext) -> dict:
    q = _query(args, max_len=120)
    limit = as_int(args.get("limit"), 10, 1, 25)
    kinds = set(str_list(args.get("kinds")))
    items = ctx.app.run_search(q, min(100, limit * 4) if kinds else limit)
    out = []
    for item in items:
        if kinds and item.get("kind") not in kinds:
            continue
        ent = compact_entity(item)
        if ent is not None:
            out.append(ent)
        if len(out) >= limit:
            break
    result: dict[str, Any] = {"query": q, "count": len(out), "results": out}
    if not out:
        result["note"] = ("Nothing loaded in Meridian matches. Search only covers the running modules' data; "
                          "for a town, region or landmark use find_place.")
    return result


def _nominatim_json(ctx: ToolContext, path: str, params: dict) -> Any:
    # Same parameters and headers as the core's /api/core/geocode and /reverse, so the cache is shared
    # and the core's 1 request per second spacing for Nominatim applies.
    resp = ctx.net.request(f"{NOMINATIM}/{path}", params=params, headers={"Accept-Language": "en"},
                           ttl=86400, timeout=10)
    return json.loads(resp.body.decode("utf-8-sig", errors="replace"))


def find_place(args: dict, ctx: ToolContext) -> dict:
    q = _query(args)
    limit = as_int(args.get("limit"), 3, 1, 5)
    rows = _nominatim_json(ctx, "search", {"q": q, "format": "jsonv2", "limit": 8, "addressdetails": 0})
    results = []
    for r in rows if isinstance(rows, list) else []:
        lat = as_float(r.get("lat"), None, -90, 90)
        lon = as_float(r.get("lon"), None, -180, 180)
        if lat is None or lon is None:
            continue
        item: dict[str, Any] = {"name": str(r.get("name") or r.get("display_name") or "")[:120],
                                "display_name": str(r.get("display_name") or "")[:240],
                                "lat": round(lat, 5), "lon": round(lon, 5),
                                "type": r.get("type"), "category": r.get("category")}
        bb = r.get("boundingbox")
        if isinstance(bb, list) and len(bb) == 4:
            s, n, w, e = (as_float(x) for x in bb)
            if None not in (s, n, w, e):
                item["bbox"] = [round(w, 5), round(s, 5), round(e, 5), round(n, 5)]
        results.append(item)
        if len(results) >= limit:
            break
    out: dict[str, Any] = {"query": q, "source": NOMINATIM_SOURCE, "results": results}
    if not results:
        out["note"] = "OpenStreetMap has no place by that name."
    return out


def _reverse(ctx: ToolContext, lat: float, lon: float, zoom: int) -> dict:
    try:
        r = _nominatim_json(ctx, "reverse", {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "format": "jsonv2", "zoom": zoom})
    except Exception as err:  # the place name is a nicety: never fail the tool for it
        if ctx.log:
            ctx.log.info("ai: reverse geocoding failed: %s", err)
        return {"name": None, "note": "place name lookup unavailable"}
    if not isinstance(r, dict) or r.get("error"):
        return {"name": None, "note": "open water or an unnamed area"}
    addr = r.get("address") if isinstance(r.get("address"), dict) else {}
    return {"name": str(r.get("display_name") or "")[:300] or None,
            "country_code": (str(addr.get("country_code") or "").upper() or None)}


def whats_here(args: dict, ctx: ToolContext) -> dict:
    lat, lon = _latlon(args)
    radius = clamp(as_float(args.get("radius_km"), 25.0) or 25.0, 0.1, 500.0)
    limit = as_int(args.get("limit"), 25, 1, 50)
    kinds = set(str_list(args.get("kinds")))
    zoom = 14 if radius <= 3 else 12 if radius <= 25 else 10 if radius <= 120 else 8
    place = _reverse(ctx, lat, lon, zoom)
    items = ctx.app.run_nearby(lat, lon, radius, max(limit, 50))
    found = []
    for item in items:
        if kinds and item.get("kind") not in kinds:
            continue
        d = item.get("distance_km")
        if isinstance(d, (int, float)) and d > radius * 1.05:
            continue
        ent = compact_entity(item)
        if ent is not None:
            found.append(ent)
    counts = Counter(e["kind"] for e in found)
    out: dict[str, Any] = {"lat": round(lat, 5), "lon": round(lon, 5), "radius_km": round(radius, 2),
                           "place": place, "place_source": NOMINATIM_SOURCE, "total": len(found),
                           "counts": dict(counts.most_common()), "nearby": found[:limit]}
    if not found:
        out["note"] = "Nothing from Meridian's running modules is this close (only loaded data is searched)."
    return out


def recent_events(args: dict, ctx: ToolContext) -> dict:
    hours = clamp(as_float(args.get("hours"), 24.0) or 24.0, 0.25, 720.0)
    kinds = set(str_list(args.get("kinds"))) or None
    bbox = parse_bbox(args.get("bbox")) if args.get("bbox") is not None else None
    if bbox is None and args.get("in_view") is True and ctx.view.get("bounds"):
        bbox = tuple(ctx.view["bounds"])
    min_sev = clamp(as_float(args.get("min_severity"), 0.0) or 0.0, 0.0, 10.0)
    limit = as_int(args.get("limit"), 40, 1, 100)
    sort = args.get("sort") if args.get("sort") in ("severity", "time") else "severity"
    now = ctx.wall()
    since = now - hours * 3600.0
    items = ctx.app.collect_events(since, kinds)
    kept = []
    for item in items:
        try:
            la, lo = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if bbox and not geo.in_bbox(la, lo, bbox):
            continue
        sev = as_float(item.get("severity"), 0.0) or 0.0
        if sev < min_sev:
            continue
        kept.append(item)

    def t(item):
        v = as_float(item.get("time"), 0.0) or 0.0
        return v / 1000.0 if v > 1e11 else v

    if sort == "time":
        kept.sort(key=lambda i: -t(i))
    else:
        kept.sort(key=lambda i: (-(as_float(i.get("severity"), 0.0) or 0.0), -t(i)))
    by_kind = Counter(str(i.get("kind")) for i in kept)
    events = []
    for item in kept[:limit]:
        ent = compact_entity(item)
        if ent is None:
            continue
        if ent.get("time") is None and isinstance(item.get("time"), (int, float)):
            ent["time"] = iso_utc(item["time"])
        events.append(ent)
    out: dict[str, Any] = {"window_hours": hours, "since": iso_utc(since), "now": iso_utc(now),
                           "area": [round(x, 4) for x in bbox] if bbox else "worldwide",
                           "total": len(kept), "by_kind": dict(by_kind.most_common()), "events": events}
    if not kept:
        out["note"] = ("No notable events from Meridian's running modules match. Only modules that publish events "
                       "are covered, so this does not mean nothing happened.")
    return out


def list_layers(args: dict, ctx: ToolContext) -> dict:
    group = args.get("group") if isinstance(args.get("group"), str) else None
    browser = {r["id"]: r for r in ctx.view.get("layers") or []}
    on = set(ctx.view.get("layers_on") or [])
    specs = {}
    for spec in list(getattr(ctx.app, "layer_specs", []) or []):
        if isinstance(spec, dict) and isinstance(spec.get("id"), str):
            specs.setdefault(spec["id"], spec)
    ids = list(browser) + [i for i in specs if i not in browser]
    rows = []
    for lid in ids:
        b = browser.get(lid, {})
        s = specs.get(lid, {})
        row: dict[str, Any] = {"id": lid, "title": b.get("title") or s.get("title") or lid,
                               "group": b.get("group") or s.get("group") or "Other",
                               "on": lid in on or bool(b.get("on"))}
        count = b.get("count")
        if count is None and callable(s.get("_count")):
            try:
                count = s["_count"]()
            except Exception:
                count = None
        if count is not None:
            row["count"] = count
        if b.get("status") and b.get("status") not in ("idle", "ok"):
            row["status"] = b["status"]
            if b.get("message"):
                row["status_message"] = b["message"]
        for key, size in (("description", 160), ("source", 120), ("refresh", 60)):
            v = s.get(key)
            if isinstance(v, str) and v.strip():
                row[key] = v.strip()[:size]
        if group and row["group"].casefold() != group.casefold():
            continue
        rows.append(row)
    out: dict[str, Any] = {"count": len(rows), "on": sorted(i for i in on), "layers": rows}
    if not rows:
        out["note"] = "No layers match." if group else "No layers are registered yet."
    return out


DATA_FUNCS: dict[str, Callable[[dict, ToolContext], Any]] = {
    "search_entities": search_entities, "find_place": find_place, "whats_here": whats_here,
    "recent_events": recent_events, "list_layers": list_layers,
}


# ---------------------------------------------------------------------------------------------
# Map actions
# ---------------------------------------------------------------------------------------------
class Actions:
    """Map actions collected during one answer, normalised for the browser.

    Camera moves (fly_to / fit_bounds) and display settings keep only the latest; set_layers calls
    merge into one; at most three entity cards; at most twelve actions in all."""

    MAX = 12

    def __init__(self):
        self.items: list[dict] = []

    def add(self, action: dict) -> None:
        kind = action["type"]
        if kind in ("fly_to", "fit_bounds"):
            self.items = [a for a in self.items if a["type"] not in ("fly_to", "fit_bounds")]
        elif kind in ("set_basemap", "set_projection", "set_terrain", "highlight", "whats_here"):
            self.items = [a for a in self.items if a["type"] != kind]
        elif kind == "set_layers":
            for existing in self.items:
                if existing["type"] == "set_layers":
                    on = [i for i in existing["on"] if i not in action["off"]]
                    off = [i for i in existing["off"] if i not in action["on"]]
                    existing["on"] = on + [i for i in action["on"] if i not in on]
                    existing["off"] = off + [i for i in action["off"] if i not in off]
                    return
        elif kind == "open_entity":
            key = (action.get("kind"), action.get("id"))
            self.items = [a for a in self.items if not (a["type"] == "open_entity" and (a.get("kind"), a.get("id")) == key)]
            cards = [a for a in self.items if a["type"] == "open_entity"]
            if len(cards) >= 3:
                self.items.remove(cards[0])
        if len(self.items) < self.MAX:
            self.items.append(action)

    def list(self) -> list[dict]:
        return [dict(a) for a in self.items]

    def entities(self) -> list[dict]:
        out = []
        for a in self.items:
            if a["type"] == "open_entity":
                ent = compact_entity({k: v for k, v in a.items() if k != "type"})
                if ent:
                    out.append(ent)
        return out

    def clear(self) -> None:
        self.items = []


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "on", "yes", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "off", "no", "0"):
        return False
    return None


def run_action(name: str, args: dict, actions: Actions, known_layers: set[str]) -> dict:
    """Validate one map-action call and queue it. Raises ToolInputError for bad arguments."""
    if name in ("fly_to", "highlight", "whats_here_card"):
        lat, lon = _latlon(args)
        action: dict[str, Any] = {"type": ACTION_TYPES[name], "lat": round(lat, 5), "lon": round(lon, 5)}
        if name == "fly_to":
            zoom = as_float(args.get("zoom"))
            if zoom is not None:
                action["zoom"] = round(clamp(zoom, 1.0, 18.0), 2)
        actions.add(action)
        where = f"{lat:.4f}, {lon:.4f}"
        done = {"fly_to": f"The map will fly to {where}" + (f" at zoom {action['zoom']:g}" if "zoom" in action else ""),
                "highlight": f"A highlight ring will be drawn at {where}",
                "whats_here_card": f"The What's here card will open at {where}"}[name]
        return {"ok": True, "done": done + " when your answer is shown."}
    if name == "fit_bounds":
        w = as_float(args.get("west"), None, -180, 180)
        e = as_float(args.get("east"), None, -180, 180)
        s = as_float(args.get("south"), None, -90, 90)
        n = as_float(args.get("north"), None, -90, 90)
        if None in (w, s, e, n) or s >= n:
            raise ToolInputError("Give west and east in -180..180 and south < north in -90..90.")
        s, n = clamp(s, -85.0, 85.0), clamp(n, -85.0, 85.0)
        actions.add({"type": "fit_bounds", "west": round(w, 5), "south": round(s, 5), "east": round(e, 5), "north": round(n, 5)})
        return {"ok": True, "done": f"The map will fit west {w:g}, south {s:g}, east {e:g}, north {n:g} when your answer is shown."}
    if name == "set_layers":
        on = [i for i in str_list(args.get("on")) if LAYER_ID_RE.fullmatch(i)]
        off = [i for i in str_list(args.get("off")) if LAYER_ID_RE.fullmatch(i) and i not in on]
        unknown = [i for i in on + off if known_layers and i not in known_layers]
        on = [i for i in on if i not in unknown]
        off = [i for i in off if i not in unknown]
        if not on and not off:
            hint = f" Known layer ids: {', '.join(sorted(known_layers)[:60])}." if known_layers else ""
            raise ToolInputError("No valid layer ids." + (f" Unknown: {', '.join(unknown)}." if unknown else "") + hint)
        actions.add({"type": "set_layers", "on": on, "off": off})
        result: dict[str, Any] = {"ok": True, "done": "Layers will change when your answer is shown.", "on": on, "off": off}
        if unknown:
            result["unknown_ids_ignored"] = unknown
        return result
    if name == "set_basemap":
        bid = args.get("id")
        if bid not in BASEMAPS:
            raise ToolInputError(f"id must be one of {', '.join(BASEMAPS)}.")
        actions.add({"type": "set_basemap", "id": bid})
        return {"ok": True, "done": f"The basemap will switch to {bid} when your answer is shown."}
    if name == "set_projection":
        proj = str(args.get("projection") or args.get("type") or "").strip().lower()
        proj = {"flat": "mercator", "2d": "mercator", "map": "mercator"}.get(proj, proj)
        if proj not in ("globe", "mercator"):
            raise ToolInputError('projection must be "globe" or "mercator".')
        actions.add({"type": "set_projection", "projection": proj})
        return {"ok": True, "done": f"The map will switch to {proj} when your answer is shown."}
    if name == "set_terrain":
        on_flag = _bool(args.get("on"))
        if on_flag is None:
            raise ToolInputError('"on" must be true or false.')
        actions.add({"type": "set_terrain", "on": on_flag})
        return {"ok": True, "done": f"3D terrain will turn {'on' if on_flag else 'off'} when your answer is shown."}
    if name == "open_entity":
        raw = args.get("entity") if isinstance(args.get("entity"), dict) else args
        ent = compact_entity(raw)
        if ent is None:
            raise ToolInputError("entity needs the kind and id exactly as a tool returned them.")
        if ent.get("layer") and known_layers and ent["layer"] not in known_layers:
            ent.pop("layer")
        ent.pop("module", None)
        actions.add({"type": "open_entity", **ent})
        return {"ok": True, "done": f"The card for {ent.get('title') or ent['id']} will open when your answer is shown."}
    raise ToolInputError(f"Unknown map action {name}.")
