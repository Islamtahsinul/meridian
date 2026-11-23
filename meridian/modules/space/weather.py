"""Point weather from Open-Meteo (keyless, CC BY 4.0, free for non-commercial use)."""

from __future__ import annotations

import math
import time

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT = ("temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,cloud_cover,"
           "wind_speed_10m,wind_direction_10m,wind_gusts_10m,is_day,pressure_msl")
HOURLY = ("temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,cloud_cover,"
          "wind_speed_10m,wind_direction_10m,wind_gusts_10m,is_day")
TTL_S = 600

# WMO weather interpretation codes as documented by Open-Meteo
WMO = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Light freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Light freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers", 85: "Light snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}
COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def describe(code) -> str:
    try:
        return WMO.get(int(code), "Unknown conditions")
    except (TypeError, ValueError):
        return "Unknown conditions"


def compass(deg) -> str | None:
    try:
        d = float(deg)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(d):
        return None
    return COMPASS[int((d % 360.0) / 22.5 + 0.5) % 16]


def _f(v, nd=1):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def params(lat: float, lon: float) -> dict:
    return {"latitude": f"{lat:.2f}", "longitude": f"{lon:.2f}", "current": CURRENT, "hourly": HOURLY,
            "forecast_hours": 24, "timezone": "auto", "timeformat": "unixtime", "wind_speed_unit": "kmh"}


def parse(data: dict, lat: float, lon: float) -> dict:
    """Open-Meteo forecast JSON -> current conditions plus the next 24 hourly steps."""
    if not isinstance(data, dict) or not isinstance(data.get("current"), dict):
        reason = data.get("reason") if isinstance(data, dict) else None
        raise ValueError(f"Open-Meteo returned no current conditions{': ' + str(reason)[:120] if reason else ''}")
    cur = data["current"]
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []

    def col(name):
        values = hourly.get(name) or []
        return [values[i] if i < len(values) else None for i in range(len(times))]

    t2, feels, pprob, precip, code, cloud, wind, wdir, gust, isday = (
        col("temperature_2m"), col("apparent_temperature"), col("precipitation_probability"), col("precipitation"),
        col("weather_code"), col("cloud_cover"), col("wind_speed_10m"), col("wind_direction_10m"),
        col("wind_gusts_10m"), col("is_day"))
    hours = []
    for i, t in enumerate(times[:24]):
        try:
            ts = int(t)
        except (TypeError, ValueError):
            continue
        hours.append({"time": ts, "temp_c": _f(t2[i]), "feels_c": _f(feels[i]), "precip_mm": _f(precip[i], 2),
                      "precip_prob": _f(pprob[i], 0), "code": code[i], "text": describe(code[i]),
                      "cloud_pct": _f(cloud[i], 0), "wind_kmh": _f(wind[i]), "wind_dir": _f(wdir[i], 0),
                      "wind_from": compass(wdir[i]), "gust_kmh": _f(gust[i]), "is_day": bool(isday[i])})
    temps = [h["temp_c"] for h in hours if h["temp_c"] is not None]
    rain = [h["precip_mm"] or 0.0 for h in hours]
    gusts = [h["gust_kmh"] for h in hours if h["gust_kmh"] is not None]
    wet = [h for h in hours if (h["precip_mm"] or 0) >= 0.1]
    current = {
        "time": int(cur.get("time") or time.time()),
        "temp_c": _f(cur.get("temperature_2m")),
        "feels_c": _f(cur.get("apparent_temperature")),
        "humidity_pct": _f(cur.get("relative_humidity_2m"), 0),
        "precip_mm": _f(cur.get("precipitation"), 2),
        "code": cur.get("weather_code"),
        "text": describe(cur.get("weather_code")),
        "cloud_pct": _f(cur.get("cloud_cover"), 0),
        "wind_kmh": _f(cur.get("wind_speed_10m")),
        "wind_dir": _f(cur.get("wind_direction_10m"), 0),
        "wind_from": compass(cur.get("wind_direction_10m")),
        "gust_kmh": _f(cur.get("wind_gusts_10m")),
        "is_day": bool(cur.get("is_day")),
        "pressure_hpa": _f(cur.get("pressure_msl")),
    }
    summary = {
        "min_c": min(temps) if temps else None,
        "max_c": max(temps) if temps else None,
        "precip_total_mm": round(sum(rain), 1),
        "max_gust_kmh": max(gusts) if gusts else None,
        "first_rain": wet[0]["time"] if wet else None,
    }
    return {
        "lat": round(lat, 5), "lon": round(lon, 5),
        "grid_lat": _f(data.get("latitude"), 4), "grid_lon": _f(data.get("longitude"), 4),
        "elevation_m": _f(data.get("elevation"), 0),
        "timezone": str(data.get("timezone") or "UTC")[:60],
        "timezone_abbreviation": str(data.get("timezone_abbreviation") or "")[:12],
        "utc_offset_s": int(data.get("utc_offset_seconds") or 0),
        "current": current, "hourly": hours, "summary": summary,
        "source": "Open-Meteo (CC BY 4.0)",
    }


def text_summary(w: dict) -> str:
    """One sentence for the AI and for tooltips."""
    c = w["current"]
    parts = [c["text"]]
    if c["temp_c"] is not None:
        parts.append(f"{c['temp_c']:.0f} °C" + (f" (feels like {c['feels_c']:.0f} °C)" if c["feels_c"] is not None else ""))
    if c["wind_kmh"] is not None:
        wind = f"wind {c['wind_kmh']:.0f} km/h"
        if c["wind_from"]:
            wind += f" from {c['wind_from']}"
        if c["gust_kmh"] is not None:
            wind += f", gusts {c['gust_kmh']:.0f}"
        parts.append(wind)
    s = w["summary"]
    if s["min_c"] is not None:
        parts.append(f"next 24 h {s['min_c']:.0f} to {s['max_c']:.0f} °C, {s['precip_total_mm']:.1f} mm precipitation")
    return ", ".join(parts)
