"""NOAA National Hurricane Center: active Atlantic, East and Central Pacific storms
(CurrentStorms.json) with forecast cones, tracks and points from NOAA's public ArcGIS map
service. US Government public domain.
"""

from __future__ import annotations

import re

from .common import clean, integer, num, parse_time, r5, simplify_geometry, valid_lonlat

CURRENT_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
MAPSERVER = "https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/NHC_tropical_weather/MapServer"

CLASSIFICATION = {
    "TD": "Tropical depression", "TS": "Tropical storm", "HU": "Hurricane", "MH": "Major hurricane",
    "STD": "Subtropical depression", "STS": "Subtropical storm", "PTC": "Potential tropical cyclone",
    "PC": "Post-tropical cyclone", "TY": "Typhoon", "STY": "Super typhoon", "EX": "Extratropical",
    "LO": "Remnant low", "DB": "Disturbance", "WV": "Tropical wave",
}
BASINS = {"al": "Atlantic", "ep": "East Pacific", "cp": "Central Pacific"}
KT_TO_KMH = 1.852


def parse_current(data: dict) -> list[dict]:
    out = []
    for s in (data or {}).get("activeStorms") or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        lat = num(s.get("latitudeNumeric"))
        lon = num(s.get("longitudeNumeric"))
        if lat is None or lon is None:
            lat, lon = _parse_hemi(s.get("latitude")), _parse_hemi(s.get("longitude"))
        if lat is None or lon is None or not valid_lonlat(lon, lat):
            continue
        sid = str(s["id"]).lower()
        kt = integer(s.get("intensity"))
        code = (s.get("classification") or "").upper()
        adv = s.get("publicAdvisory") or {}
        out.append({
            "id": f"nhc:{sid}",
            "native_id": sid,
            "name": clean(s.get("name"), 60) or sid.upper(),
            "classification": code or None,
            "classification_label": CLASSIFICATION.get(code, code or "Tropical cyclone"),
            "wind_kt": kt,
            "wind_kmh": round(kt * KT_TO_KMH) if kt is not None else None,
            "pressure_mb": integer(s.get("pressure")),
            "lat": r5(lat), "lon": r5(lon),
            "movement_dir": integer(s.get("movementDir")),
            "movement_kt": integer(s.get("movementSpeed")),
            "updated": parse_time(s.get("lastUpdate")),
            "bin": (s.get("binNumber") or "").upper() or None,
            "basin": BASINS.get(sid[:2], "Tropics"),
            "advisory_number": (adv.get("advNum") or "").lstrip("0") or None,
            "advisory_url": adv.get("url") if isinstance(adv.get("url"), str) else None,
            "graphics_url": (s.get("forecastGraphics") or {}).get("url"),
        })
    return out


def _parse_hemi(text) -> float | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([NSEW])\s*$", str(text or ""))
    if not m:
        return None
    v = float(m.group(1))
    return -v if m.group(2) in "SW" else v


def layer_ids(service: dict) -> dict[str, dict[str, int]]:
    """{"AT1": {"cone": 8, "track": 7, "points": 6, "past": 12}, ...} from the service layer list."""
    out: dict[str, dict[str, int]] = {}
    wanted = {"Forecast Cone": "cone", "Forecast Track": "track", "Forecast Points": "points", "Past Track": "past"}
    for layer in (service or {}).get("layers") or []:
        name = layer.get("name") or ""
        m = re.match(r"^([A-Z]{2}\d)\s+(.+)$", name)
        if not m:
            continue
        role = wanted.get(m.group(2).strip())
        if role:
            out.setdefault(m.group(1), {})[role] = int(layer["id"])
    return out


def parse_layer_geojson(data: dict, tol: float = 0.02) -> list[dict]:
    feats = []
    for f in (data or {}).get("features") or []:
        g = simplify_geometry((f or {}).get("geometry"), tol, 3)
        if g:
            feats.append({"geometry": g, "properties": (f or {}).get("properties") or {}})
    return feats


def forecast_points(features: list[dict]) -> list[dict]:
    out = []
    for f in features:
        p = f["properties"]
        g = f["geometry"]
        if g.get("type") != "Point":
            continue
        lon, lat = g["coordinates"][:2]
        kt = integer(p.get("maxwind"))
        out.append({"lat": lat, "lon": lon, "tau": integer(p.get("tau")), "wind_kt": kt,
                    "wind_kmh": round(kt * KT_TO_KMH) if kt is not None else None,
                    "gust_kt": integer(p.get("gust")), "type": (p.get("stormtype") or p.get("dvlbl") or "").upper() or None,
                    "label": clean(p.get("fldatelbl") or p.get("datelbl"), 60) or None,
                    "t": parse_time(p.get("validtime")) if p.get("validtime") and not str(p.get("validtime")).isdigit()
                    else None})
    out.sort(key=lambda x: x["tau"] if x["tau"] is not None else 999)
    return out
