"""Workspace states, places and the built-in presets: validation only, no I/O (the tests use it directly).

A workspace state is what the browser needs to put the map back as it was (version 1):

    {"v": 1,
     "view": {"center": [lon, lat], "zoom": 6.2, "bearing": 0, "pitch": 0},
     "basemap": "dark", "projection": "mercator", "terrain": false,
     "layers": {"ships": {"on": true, "options": {"types": "cargo,tanker"}}, "ships.straits": {"on": true}},
     "notes": ""}

Every part is optional. Values that can't be used are dropped rather than refused, so an import from
an older or newer Meridian still loads; only a state that is not an object, has another version or is
too large is an error. Layer and option ids are checked for shape only: the browser applies the ones
it knows and ignores the rest.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

BASEMAPS = ("dark", "relief", "light", "streets", "satellite", "sentinel")
PROJECTIONS = ("mercator", "globe")
PLACE_TYPES = ("home", "work", "family", "travel", "other")
LAYER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
OPTION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")

NAME_MAX = 80
NOTES_MAX = 2000
PLACE_NOTES_MAX = 500
MAX_LAYERS = 150
MAX_OPTIONS = 40
OPTION_STR_MAX = 300
STATE_MAX_BYTES = 48 * 1024
MIN_RADIUS_KM = 1.0
MAX_RADIUS_KM = 1000.0
DEFAULT_RADIUS_KM = 25.0


class InputError(ValueError):
    """Something the user sent that can't be stored (the message is shown to them)."""


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------
def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def clean_text(value: Any, limit: int) -> str:
    """Text without control or direction-override characters, whitespace runs kept as typed."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InputError("Expected text.")
    return _CONTROL.sub("", value.replace("\r\n", "\n"))[:limit]


def clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise InputError("The name must be text.")
    name = " ".join(_CONTROL.sub("", value).split())[:NAME_MAX]
    if not name:
        raise InputError("The name can't be empty.")
    return name


def wrap_lon(lon: float) -> float:
    return ((lon + 180.0) % 360.0 + 360.0) % 360.0 - 180.0


# ---------------------------------------------------------------------------------------------
# workspace states
# ---------------------------------------------------------------------------------------------
def clean_view(raw: Any) -> dict | None:
    """{"center": [lon, lat], "zoom", "bearing", "pitch"} or None when there is no usable centre."""
    if not isinstance(raw, dict):
        return None
    center = raw.get("center")
    lon = lat = None
    if isinstance(center, (list, tuple)) and len(center) == 2:
        lon, lat = _finite(center[0]), _finite(center[1])
    elif isinstance(center, dict):
        lon, lat = _finite(center.get("lon", center.get("lng"))), _finite(center.get("lat"))
    if lon is None or lat is None or not -90.0 <= lat <= 90.0 or not -540.0 <= lon <= 540.0:
        return None
    view: dict[str, Any] = {"center": [round(wrap_lon(lon) if abs(lon) > 180 else lon, 5), round(lat, 5)]}
    zoom = _finite(raw.get("zoom"))
    view["zoom"] = round(min(22.0, max(0.0, zoom)), 2) if zoom is not None else 3.0
    bearing = _finite(raw.get("bearing"))
    b = wrap_lon(bearing) if bearing is not None else 0.0
    view["bearing"] = round(b, 1) if b != -180.0 else 180.0
    pitch = _finite(raw.get("pitch"))
    view["pitch"] = round(min(85.0, max(0.0, pitch)), 1) if pitch is not None else 0.0
    return view


def _option_value(value: Any) -> Any:
    """Layer options are toggles, selects and ranges: booleans, numbers and short text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        f = _finite(value)
        if f is None:
            return None
        return int(f) if isinstance(value, int) and abs(f) < 2 ** 53 else f
    if isinstance(value, str):
        return _CONTROL.sub("", value)[:OPTION_STR_MAX]
    return None


def clean_layers(raw: Any) -> dict:
    """{"id": {"on": bool, "options": {...}}} (a plain list of ids means those layers on)."""
    out: dict[str, dict] = {}
    if isinstance(raw, (list, tuple)):
        raw = {lid: {"on": True} for lid in raw if isinstance(lid, str)}
    if not isinstance(raw, dict):
        return out
    for lid, entry in raw.items():
        if len(out) >= MAX_LAYERS:
            break
        if not isinstance(lid, str) or not LAYER_ID_RE.match(lid):
            continue
        if isinstance(entry, bool):
            entry = {"on": entry}
        if not isinstance(entry, dict):
            continue
        item: dict[str, Any] = {"on": entry.get("on") is True}
        opts = entry.get("options")
        if isinstance(opts, dict):
            clean_opts = {}
            for key, value in opts.items():
                if len(clean_opts) >= MAX_OPTIONS:
                    break
                if not isinstance(key, str) or not OPTION_ID_RE.match(key):
                    continue
                v = _option_value(value)
                if v is not None:
                    clean_opts[key] = v
            if clean_opts:
                item["options"] = clean_opts
        out[lid] = item
    return out


def clean_state(raw: Any) -> dict:
    """A storable workspace state. Raises InputError for a non-object, another version or an oversized state."""
    if not isinstance(raw, dict):
        raise InputError("A workspace must be an object.")
    version = raw.get("v", 1)
    if isinstance(version, bool) or version not in (1, 1.0):
        raise InputError("This workspace comes from a newer Meridian (unknown version).")
    out: dict[str, Any] = {"v": 1}
    view = clean_view(raw.get("view"))
    if view:
        out["view"] = view
    if raw.get("basemap") in BASEMAPS:
        out["basemap"] = raw["basemap"]
    if raw.get("projection") in PROJECTIONS:
        out["projection"] = raw["projection"]
    if isinstance(raw.get("terrain"), bool):
        out["terrain"] = raw["terrain"]
    if "layers" in raw:
        out["layers"] = clean_layers(raw.get("layers"))
    notes = raw.get("notes")
    if isinstance(notes, str) and notes.strip():
        out["notes"] = clean_text(notes, NOTES_MAX)
    if len(json.dumps(out, separators=(",", ":"))) > STATE_MAX_BYTES:
        raise InputError("This workspace is too large to keep.")
    return out


def summary(state: dict) -> str:
    """Plain words for a list row: "6 layers, dark map, globe, terrain"."""
    on = [lid for lid, v in (state.get("layers") or {}).items() if v.get("on")]
    parts = [f"{len(on)} layer{'' if len(on) == 1 else 's'}" if "layers" in state else "layers unchanged"]
    if state.get("basemap"):
        parts.append(f"{state['basemap']} map")
    if state.get("projection") == "globe":
        parts.append("globe")
    if state.get("terrain"):
        parts.append("3D terrain")
    return ", ".join(parts)


# ---------------------------------------------------------------------------------------------
# places
# ---------------------------------------------------------------------------------------------
def clean_place(raw: Any, *, partial: bool = False) -> dict:
    """A named place: name, type, lat, lon, radius_km (for alerts), zoom (for flying there), notes."""
    if not isinstance(raw, dict):
        raise InputError("A place must be an object.")
    out: dict[str, Any] = {}
    if "name" in raw or not partial:
        out["name"] = clean_name(raw.get("name"))
    if "type" in raw or not partial:
        kind = raw.get("type", "other")
        if kind not in PLACE_TYPES:
            raise InputError(f"The type must be one of {', '.join(PLACE_TYPES)}.")
        out["type"] = kind
    if "lat" in raw or "lon" in raw or not partial:
        lat, lon = _finite(raw.get("lat")), _finite(raw.get("lon"))
        if lat is None or lon is None or not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
            raise InputError("The position must be a latitude from -90 to 90 and a longitude from -180 to 180.")
        out["lat"], out["lon"] = round(lat, 5), round(lon, 5)
    if "radius_km" in raw or not partial:
        radius = _finite(raw.get("radius_km", DEFAULT_RADIUS_KM))
        if radius is None or not MIN_RADIUS_KM <= radius <= MAX_RADIUS_KM:
            raise InputError(f"The radius must be from {MIN_RADIUS_KM:g} to {MAX_RADIUS_KM:,.0f} km.")
        out["radius_km"] = round(radius, 2)
    if "zoom" in raw or not partial:
        zoom = _finite(raw.get("zoom", 11))
        out["zoom"] = round(min(18.0, max(2.0, zoom)), 2) if zoom is not None else 11.0
    if "notes" in raw:
        out["notes"] = clean_text(raw.get("notes"), PLACE_NOTES_MAX)
    return out


# ---------------------------------------------------------------------------------------------
# built-in presets
# ---------------------------------------------------------------------------------------------
def _preset(pid: str, name: str, description: str, state: dict) -> dict:
    return {"id": f"builtin:{pid}", "name": name, "description": description, "builtin": True,
            "state": clean_state({"v": 1, **state})}


PRESETS: tuple[dict, ...] = (
    _preset("middle-east", "Middle East watch",
            "Conflict and protest reports, military aircraft, GPS interference, conflict-zone advisories, "
            "straits and ships from the eastern Mediterranean to the Gulf.",
            {"view": {"center": [45.0, 27.5], "zoom": 4.3}, "basemap": "dark", "projection": "mercator",
             "terrain": False,
             "layers": {"intel.conflict": {"on": True, "options": {"days": "3", "minSev": 4}},
                        "intel.protests": {"on": True}, "intel.advice": {"on": True},
                        "flights.military": {"on": True}, "flights.gpsjam": {"on": True, "options": {"hours": "24"}},
                        "flights.czib": {"on": True}, "ships": {"on": True, "options": {"types": "cargo,tanker"}},
                        "ships.straits": {"on": True}, "ships.dark": {"on": True}}}),
    _preset("uk-weather", "UK weather day",
            "Rain radar, Met Office warnings through MeteoAlarm, England flood warnings, traffic cameras and "
            "the live electricity grid.",
            {"view": {"center": [-2.6, 54.3], "zoom": 5.4}, "basemap": "dark", "projection": "mercator",
             "terrain": False,
             "layers": {"space.radar": {"on": True}, "hazards.warnings": {"on": True, "options": {"src": "meteoalarm"}},
                        "hazards.floods": {"on": True}, "cameras": {"on": True, "options": {"show": "traffic"}},
                        "infra.grid": {"on": True}}}),
    _preset("maritime", "Maritime chokepoints",
            "Cargo ships and tankers, the straits monitor, dark and sanctioned vessels and ports, centred on "
            "Suez, Bab-el-Mandeb and Hormuz.",
            {"view": {"center": [52.0, 18.0], "zoom": 2.8}, "basemap": "dark", "projection": "mercator",
             "terrain": False,
             "layers": {"ships": {"on": True, "options": {"types": "cargo,tanker"}}, "ships.straits": {"on": True},
                        "ships.dark": {"on": True}, "ships.sanctioned": {"on": True}, "infra.ports": {"on": True}}}),
    _preset("space-aurora", "Space and aurora tonight",
            "The aurora forecast and viewing lines, space weather now, the ISS and upcoming launches, on a globe "
            "over the North Atlantic.",
            {"view": {"center": [-20.0, 62.0], "zoom": 2.3}, "basemap": "dark", "projection": "globe",
             "terrain": False,
             "layers": {"space.aurora": {"on": True}, "space.spaceweather": {"on": True}, "space.iss": {"on": True},
                        "space.launches": {"on": True}}}),
    _preset("disaster", "Disaster response",
            "Earthquakes, GDACS disaster alerts, tropical storms, fires, volcanoes, tsunami messages, Copernicus "
            "emergency mapping and severe weather warnings worldwide.",
            {"view": {"center": [20.0, 15.0], "zoom": 1.8}, "basemap": "dark", "projection": "mercator",
             "terrain": False,
             "layers": {"hazards.quakes": {"on": True, "options": {"window": "week"}}, "hazards.disasters": {"on": True},
                        "hazards.storms": {"on": True}, "hazards.fires": {"on": True}, "hazards.volcanoes": {"on": True},
                        "hazards.tsunami": {"on": True}, "hazards.mapping": {"on": True},
                        "hazards.warnings": {"on": True, "options": {"min": "3"}}}}),
)


def preset(pid: str) -> dict | None:
    for p in PRESETS:
        if p["id"] == pid:
            return p
    return None
