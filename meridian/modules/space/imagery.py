"""NASA GIBS products (keyless WMTS, EOSDIS open data), Meteosat imagery and lightning (EUMETSAT
EUMETView WMS, see meteosat.py) and RainViewer radar frames.

Every GIBS identifier, tile matrix set, format and period below was checked against the
EPSG:3857 "best" GetCapabilities document (22 Nov 2026; the extension pack on 23 Nov 2026). The
latest available time of each GIBS layer comes from GIBS's small DescribeDomains documents (a few
hundred bytes each), cached for 10 minutes for sub-daily products and 3 hours for daily ones; the
Meteosat frames come from the EUMETView capabilities (cached 10 minutes).

A product may offer ``variants`` (one layer set each, chosen in the layer options), a
``composite`` (the newest N times drawn together, newest on top), ``paint`` (extra MapLibre raster
paint), ``reference`` (opaque imagery that wants borders and place names above it) and per-layer
``layer_bounds`` and ``layer_maxzoom``. Layer ids starting with ``eumetsat:`` are EUMETView WMS layers.
"""

from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import meteosat as M

GIBS_BASE = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"
GIBS_CREDIT = "NASA EOSDIS GIBS"
RAINVIEWER_URL = "https://api.rainviewer.com/public/weather-maps.json"
RAINVIEWER_MAX_ZOOM = 7          # tiles above zoom 7 come back as a "Zoom level not supported" image
RAINVIEWER_COLOUR = 2            # the only colour scheme the free API serves ("Universal Blue")

L6, L7, L8, L9, L12, L13 = ("GoogleMapsCompatible_Level6", "GoogleMapsCompatible_Level7", "GoogleMapsCompatible_Level8",
                            "GoogleMapsCompatible_Level9", "GoogleMapsCompatible_Level12", "GoogleMapsCompatible_Level13")
MTG_DISC = [-81.3, -81.3, 81.3, 81.3]   # the 0 degree Meteosat disc (EUMETView capabilities)

# Legend stops sampled from the GIBS colour maps (gibs.earthdata.nasa.gov/colormaps/v1.3/...).
PRODUCTS: dict[str, dict] = {
    "precip": {
        "title": "Precipitation rate", "layers": ["IMERG_Precipitation_Rate_30min"], "matrix": L6, "maxzoom": 6,
        "ext": "png", "period": "PT30M", "opacity": 0.85,
        "about": "Half-hourly rain and snow rate from the GPM IMERG early run, about five hours behind real time.",
        "source": "NASA GPM IMERG via GIBS",
        "legend": [["#00764e", "0.1 mm/h"], ["#45c000", "0.5"], ["#ff980f", "2.4"], ["#da0000", "11"],
                   ["#330000", "53 or more"], ["#3f3dd5", "Snow"]],
    },
    "clouds": {
        "title": "Clouds, infrared", "matrix": L6, "maxzoom": 6, "ext": "png", "period": "PT10M", "opacity": 0.6,
        "layers": ["Himawari_AHI_Band13_Clean_Infrared", "GOES-West_ABI_Band13_Clean_Infrared",
                   "GOES-East_ABI_Band13_Clean_Infrared", "eumetsat:ir105_hrfi"],
        "about": "Cloud-top temperature every 10 minutes from GOES-East, GOES-West, Himawari and, over Europe, "
                 "Africa and the Atlantic, Meteosat. Meteosat uses EUMETSAT's own colours: grey and white cloud, "
                 "blue to red only for the coldest storm tops. The central Indian Ocean is seen only at a slant.",
        "source": "NOAA GOES ABI and JMA Himawari AHI band 13 via GIBS; EUMETSAT Meteosat FCI 10.5 µm",
        "wms_styles": {"eumetsat:ir105_hrfi": "mtg_fd_ir105_hrfi_style_02"},
        # Each satellite draws one share of the overlaps, split near the midpoints between sub-satellite
        # points (GOES-West 137.2 W, GOES-East 75.2 W, Meteosat 0, Himawari 140.7 E) on tile edges (multiples
        # of 22.5 degrees): bounds only filter whole tiles, and two half-transparent tiles over one place
        # would blend into a visible seam. From zoom 2.5 every place comes from one satellite.
        "layer_bounds": {"Himawari_AHI_Band13_Clean_Infrared": [67.5, -81, 180, 81],
                         "GOES-West_ABI_Band13_Clean_Infrared": [-180, -81, -112.5, 81],
                         "GOES-East_ABI_Band13_Clean_Infrared": [-112.5, -81, -45, 81],
                         "eumetsat:ir105_hrfi": [-45, -81, 67.5, 81]},
        "layer_maxzoom": {"eumetsat:ir105_hrfi": 7},
        "legend": [["#e6e6e6", "−80 °C"], ["#4d0000", "−68"], ["#ff8000", "−56"], ["#4dff00", "−44"],
                   ["#00ffff", "−20"], ["#989898", "0 °C or warmer"]],
    },
    "sst": {
        "title": "Sea surface temperature", "layers": ["GHRSST_L4_MUR_Sea_Surface_Temperature"], "matrix": L7,
        "maxzoom": 7, "ext": "png", "period": "P1D", "opacity": 0.8,
        "about": "Daily gap-free foundation temperature (GHRSST MUR L4), about a day behind.",
        "source": "NASA JPL GHRSST MUR via GIBS",
        "legend": [["#2b001a", "below 0 °C"], ["#1e104d", "8"], ["#2ea3ef", "16"], ["#ffaf00", "24"],
                   ["#6b0200", "32 °C"]],
    },
    "lst": {
        "title": "Land surface temperature, day", "layers": ["MODIS_Terra_Land_Surface_Temp_Day"], "matrix": L7,
        "maxzoom": 7, "ext": "png", "period": "P1D", "opacity": 0.8, "swath": True,
        "about": "Daytime skin temperature of the ground from MODIS on Terra. Gaps are cloud or swath edges.",
        "source": "NASA MODIS Terra via GIBS",
        "legend": [["#0047ff", "−36 °C"], ["#66ff12", "2"], ["#ffbe00", "40"], ["#ff0100", "77 °C"]],
    },
    "aod": {
        "title": "Aerosols (smoke, dust, haze)", "layers": ["MODIS_Combined_Value_Added_AOD"], "matrix": L6,
        "maxzoom": 6, "ext": "png", "period": "P1D", "opacity": 0.75,
        "about": "Aerosol optical depth from MODIS Terra and Aqua combined. Higher means thicker smoke, dust or haze. "
                 "Shown only where the Sun is high enough for MODIS to measure aerosols that day; towards the "
                 "winter pole GIBS serves unusable tiles.",
        "source": "NASA MODIS value-added AOD via GIBS", "solar_bounds": 72,
        "legend": [["#fffdcd", "0"], ["#ffcb4e", "0.2"], ["#ff701e", "0.4"], ["#e1100b", "0.6"], ["#7d000e", "5"]],
    },
    "snow": {
        "title": "Snow cover", "layers": ["MODIS_Terra_NDSI_Snow_Cover"], "matrix": L8, "maxzoom": 8, "ext": "png",
        "period": "P1D", "opacity": 0.85, "swath": True,
        "about": "Daily snow cover index from MODIS on Terra. Cloud hides the ground, so gaps are normal.",
        "source": "NASA MODIS Terra via GIBS",
        "legend": [["#f0f089", "Some snow"], ["#f0b489", "Half"], ["#ff0000", "Full cover"]],
    },
    # -- extension pack (Space+): checked against the capabilities and tiles on 23 Nov 2026 -------------
    "no2": {
        "title": "Nitrogen dioxide", "subtitle": "Sentinel-5P, daily, about 10 days behind",
        "layers": ["TROPOMI_L2_Nitrogen_Dioxide_Tropospheric_Column"], "matrix": L6, "maxzoom": 6, "ext": "png",
        "period": "P1D", "opacity": 0.55,
        "settle": 1,     # GIBS lists the newest day before all its orbits are in (13 Nov: 1 to 9 KB tiles over Europe)
        "about": "Nitrogen dioxide in the lower atmosphere, mostly from traffic, power stations, industry and shipping: "
                 "look for orange and red; the pale speckle is low NO2 and noise. Compare days to see activity drop or "
                 "return after shutdowns. Cloud leaves gaps, and NASA GIBS publishes it about ten days late, so the "
                 "layer opens a day before the newest listed day, which is usually still incomplete.",
        "source": "Copernicus Sentinel-5P TROPOMI via NASA GIBS",
        "legend": [["#fffcc7", "Low"], ["#ffd861", "5"], ["#ff9f2c", "10"], ["#fd3312", "17"],
                   ["#e8160b", "20 or more (10¹⁵ molecules/cm²)"]],
    },
    "so2": {
        "title": "Sulphur dioxide", "subtitle": "OMPS on NOAA-20, daily",
        "layers": ["OMPS_NOAA20_SO2_Lower_Troposphere", "OMPS_NOAA20_SO2_Upper_Troposphere_and_Stratosphere"],
        "variants": [
            {"id": "low", "label": "Low plumes (industry, degassing volcanoes)",
             "layers": ["OMPS_NOAA20_SO2_Lower_Troposphere"]},
            {"id": "high", "label": "High plumes (large eruptions)",
             "layers": ["OMPS_NOAA20_SO2_Upper_Troposphere_and_Stratosphere"]}],
        "matrix": L6, "maxzoom": 6, "ext": "png", "period": "P1D", "opacity": 0.9, "swath": True,   # today still filling
        "paint": {"raster-brightness-min": 0.2},     # the dark blue low end is invisible on the navy map otherwise
        "about": "Sulphur dioxide from volcanoes, smelters and coal-fired power stations. Only amounts above about "
                 "1 Dobson unit are drawn, so most of the map stays clear. Choose high plumes for eruptions that "
                 "reach the stratosphere.",
        "source": "NASA OMPS on NOAA-20 via NASA GIBS",
        # GIBS colours as drawn here (lifted by raster-brightness-min 0.2)
        "legend": [["#395383", "1 DU"], ["#4575a9", "5"], ["#6da8d1", "12"], ["#afd1e9", "20"],
                   ["#f8fbfe", "32 DU or more"]],
    },
    "seaice": {
        "title": "Sea ice", "subtitle": "GHRSST MUR concentration, daily",
        "layers": ["GHRSST_L4_MUR_Sea_Ice_Concentration"], "matrix": L7, "maxzoom": 7, "ext": "png",
        "period": "P1D", "opacity": 0.8,
        "about": "How much of the sea is covered by ice, from the daily GHRSST MUR analysis, about a day behind. "
                 "Useful for Arctic shipping routes and the yearly freeze and melt.",
        "source": "NASA JPL GHRSST MUR via NASA GIBS",
        "legend": [["#c600c6", "12%"], ["#0073ff", "36"], ["#1eb400", "60"], ["#f8fd00", "72"], ["#ff4000", "84"],
                   ["#ffaaaa", "96% or more"]],
    },
    "lightning": {
        "title": "Lightning", "subtitle": "Meteosat Lightning Imager, every 5 minutes",
        "layers": ["eumetsat:li_afa"], "matrix": None, "maxzoom": 7, "ext": "png", "period": "PT5M",
        "opacity": 0.95, "composite": 6, "composite_label": "Last 30 minutes combined",
        "layer_bounds": {"eumetsat:li_afa": MTG_DISC},
        "about": "Where Meteosat's Lightning Imager saw flashes: the cloud-top area lit by lightning around the time "
                 "shown, pale yellow for a single flash to dark red for many. Covers Europe, Africa, the Middle East "
                 "and the Atlantic, about 20 minutes behind.",
        "source": "EUMETSAT Meteosat MTG Lightning Imager (accumulated flash area), EUMETView",
        "legend": [["#fef6b5", "One flash"], ["#fd8d3c", "Several"], ["#b10126", "Many flashes"]],
    },
    # imagery group
    "nightdaily": {
        "title": "Night lights, daily", "subtitle": "VIIRS, useful for spotting blackouts", "group": "Imagery",
        "layers": ["VIIRS_NOAA20_GapFilled_BRDF_Corrected_DayNightBand_Radiance",
                   "VIIRS_NOAA20_DayNightBand_At_Sensor_Radiance"],
        "variants": [
            {"id": "corrected", "label": "Moonlight corrected, gaps filled",
             "layers": ["VIIRS_NOAA20_GapFilled_BRDF_Corrected_DayNightBand_Radiance"]},
            {"id": "raw", "label": "As seen that night", "layers": ["VIIRS_NOAA20_DayNightBand_At_Sensor_Radiance"]}],
        "matrix": L8, "maxzoom": 8, "ext": "png", "period": "P1D", "swath": True, "opacity": 1.0, "reference": True,
        "about": "The Earth at night as VIIRS on NOAA-20 saw it, one picture per night. Useful for spotting blackouts "
                 "after storms, strikes or grid failures: compare with a night before (the compare tool can). The "
                 "corrected version removes moonlight and fills cloudy spots from earlier nights, so a blackout under "
                 "cloud can stay hidden; the version as seen keeps cloud, moonlight and aurora.",
        "source": "NASA Black Marble and VIIRS day/night band on NOAA-20 via NASA GIBS",
        "legend": [["#070707", "Dark"], ["#767676", "4"], ["#bababa", "13"], ["#fcfcfc", "37 nW/cm²/sr or more"]],
    },
    "flood": {
        "title": "Flood map", "subtitle": "VIIRS on NOAA-20 and NOAA-21, daily", "group": "Imagery",
        "layers": ["VIIRS_Combined_Flood_2-Day", "VIIRS_Combined_Flood_1-Day", "VIIRS_Combined_Flood_3-Day"],
        "variants": [
            {"id": "d2", "label": "2-day composite", "layers": ["VIIRS_Combined_Flood_2-Day"]},
            {"id": "d1", "label": "1 day", "layers": ["VIIRS_Combined_Flood_1-Day"]},
            {"id": "d3", "label": "3-day composite", "layers": ["VIIRS_Combined_Flood_3-Day"]}],
        "matrix": L9, "maxzoom": 9, "ext": "png", "period": "P1D", "opacity": 0.85,
        "about": "Water mapped from VIIRS: red is flooding, yellow is land that floods most years, blue is normal "
                 "water. Grey means cloud or no clear view, which is common during floods; the 2-day and 3-day "
                 "composites fill more cloud gaps but mix in older water.",
        "source": "NASA LANCE VIIRS flood product via NASA GIBS",
        "legend": [["#fa1e24", "Flood"], ["#ffff00", "Recurring flood"], ["#32d2f5", "Normal water"],
                   ["#afafaf", "Cloud or no data"]],
    },
    "sar": {
        "title": "Radar, Sentinel-1", "subtitle": "OPERA radar backscatter, sees through cloud", "group": "Imagery",
        "layers": ["OPERA_L2_Radiometric_Terrain_Corrected_SAR_Sentinel-1"], "matrix": L12, "maxzoom": 12,
        "ext": "png", "period": "P1D", "opacity": 0.95, "reference": True,
        "composite": 6, "composite_label": "Last 6 days combined", "composite_default": True,
        "about": "Sentinel-1 radar sees through cloud and darkness. In this false colour, water and smooth ground are "
                 "dark, towns are bright, forest is green and fields are pink. Each day holds only that day's passes, "
                 "so the layer starts with the last six days combined, the newest on top.",
        "source": "NASA JPL OPERA RTC-S1 from Copernicus Sentinel-1 via NASA GIBS",
        "legend": [["#2b1238", "Water, smooth ground"], ["#efe8f0", "Towns"], ["#7bb27a", "Forest"],
                   ["#c98ac4", "Fields, bare ground"]],
    },
    "meteosat": {
        "title": "Meteosat, Europe and Africa", "subtitle": "Meteosat Third Generation, every 10 minutes",
        "group": "Imagery",
        "layers": ["eumetsat:rgb_geocolour", "eumetsat:ir105_hrfi", "eumetsat:rgb_dust"],
        "variants": [
            {"id": "geocolour", "label": "GeoColour (true colour by day)", "layers": ["eumetsat:rgb_geocolour"]},
            {"id": "infrared", "label": "Infrared cloud tops", "layers": ["eumetsat:ir105_hrfi"]},
            {"id": "dust", "label": "Dust (pink)", "layers": ["eumetsat:rgb_dust"]}],
        "matrix": None, "maxzoom": 8, "ext": "png", "period": "PT10M", "opacity": 1.0, "reference": True,
        "wms_styles": {"eumetsat:ir105_hrfi": "mtg_fd_ir105_hrfi_grayscale"},
        "layer_bounds": {"eumetsat:rgb_geocolour": MTG_DISC, "eumetsat:ir105_hrfi": MTG_DISC, "eumetsat:rgb_dust": MTG_DISC},
        "about": "The full Meteosat disc centred on 0°: Europe, Africa, the Middle East and the Atlantic every 10 "
                 "minutes, about 20 minutes behind. GeoColour is true colour by day and infrared cloud over city "
                 "lights at night; infrared shows cloud tops day and night (white is cold and high); dust shows "
                 "dust storms in pink and magenta.",
        "source": "EUMETSAT Meteosat MTG FCI, EUMETView",
    },
    # true colour (imagery group and the compare tool)
    "terra": {"title": "MODIS Terra", "layers": ["MODIS_Terra_CorrectedReflectance_TrueColor"], "matrix": L9,
              "maxzoom": 9, "ext": "jpg", "period": "P1D", "swath": True, "pass": "morning pass, about 10:30 local"},
    "aqua": {"title": "MODIS Aqua", "layers": ["MODIS_Aqua_CorrectedReflectance_TrueColor"], "matrix": L9,
             "maxzoom": 9, "ext": "jpg", "period": "P1D", "swath": True, "pass": "afternoon pass, about 13:30 local"},
    "noaa20": {"title": "VIIRS NOAA-20", "layers": ["VIIRS_NOAA20_CorrectedReflectance_TrueColor"], "matrix": L9,
               "maxzoom": 9, "ext": "jpg", "period": "P1D", "swath": True, "pass": "afternoon pass, no gaps between swaths"},
    "noaa21": {"title": "VIIRS NOAA-21", "layers": ["VIIRS_NOAA21_CorrectedReflectance_TrueColor"], "matrix": L9,
               "maxzoom": 9, "ext": "jpg", "period": "P1D", "swath": True, "pass": "afternoon pass"},
    "snpp": {"title": "VIIRS Suomi NPP", "layers": ["VIIRS_SNPP_CorrectedReflectance_TrueColor"], "matrix": L9,
             "maxzoom": 9, "ext": "jpg", "period": "P1D", "swath": True, "pass": "afternoon pass"},
    # static overlays
    "features": {"title": "Coastlines and borders", "layers": ["Reference_Features_15m"], "matrix": L13, "maxzoom": 13,
                 "ext": "png", "period": None},
    "labels": {"title": "Place labels", "layers": ["Reference_Labels_15m"], "matrix": L13, "maxzoom": 13,
               "ext": "png", "period": None},
    "nightlights": {"title": "Night lights 2016", "layers": ["VIIRS_Black_Marble"], "matrix": L8, "maxzoom": 8,
                    "ext": "png", "period": "P1Y", "fixed": "2016-01-01"},
}
TRUE_COLOUR = ["terra", "aqua", "noaa20", "noaa21", "snpp"]
WEATHER = ["precip", "clouds", "sst", "lst", "aod", "snow", "no2", "so2", "seaice", "lightning"]
IMAGERY = ["nightdaily", "flood", "sar", "meteosat"]          # product layers in the Imagery group
COMPARE = TRUE_COLOUR + ["nightdaily", "flood", "sar", "no2"]  # products the before/after tool offers
SUB_DAILY = ("PT5M", "PT10M", "PT30M")

_DOMAIN_TTL = {"sub": 600.0, "day": 3 * 3600.0}
_SAFE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}Z)?$")
_LAYER_KEYS = ("layers", "wms_styles", "layer_bounds", "layer_maxzoom")


def all_layers(product: dict) -> list[str]:
    """Every layer a product can show: its own list plus each variant's, in order, without repeats."""
    out: list[str] = []
    for layer in list(product.get("layers") or []) + [l for v in product.get("variants") or [] for l in v["layers"]]:
        if layer not in out:
            out.append(layer)
    return out


def variant_layers(product: dict, variant: str | None = None) -> list[str]:
    """The layers shown for a variant id (the first variant when unknown or not given)."""
    variants = product.get("variants") or []
    if not variants:
        return list(product["layers"])
    chosen = next((v for v in variants if v["id"] == variant), variants[0])
    return list(chosen["layers"])


def layer_info(product: dict, layer: str) -> dict:
    """Provider details for one layer: GIBS WMTS or EUMETView WMS, plus optional bounds and zoom limit."""
    if layer.startswith(M.PREFIX):
        name = layer[len(M.PREFIX):]
        out = {"provider": "eumetsat", "wms": M.wms_spec(name, (product.get("wms_styles") or {}).get(layer, ""))}
    else:
        out = {"provider": "gibs"}
    bounds = (product.get("layer_bounds") or {}).get(layer)
    if bounds:
        out["bounds"] = list(bounds)
    maxzoom = (product.get("layer_maxzoom") or {}).get(layer)
    if maxzoom:
        out["maxzoom"] = maxzoom
    return out


def product_meta(pid: str) -> dict:
    """Static description of a product (titles, options, legends, layer providers) with no times."""
    p = PRODUCTS[pid]
    entry = {k: v for k, v in p.items() if k not in _LAYER_KEYS}
    entry.update({"id": pid, "default_time": None,
                  "layers": [dict(layer_info(p, l), layer=l, times=[], latest=None, estimated=True, default=None)
                             for l in all_layers(p)]})
    return entry


def tile_url(layer: str, matrix: str, ext: str, time_value: str | None) -> str:
    t = time_value or "default"
    return f"{GIBS_BASE}/{layer}/default/{t}/{matrix}/{{z}}/{{y}}/{{x}}.{ext}"


def _parse_iso(s: str) -> datetime:
    s = s.strip()
    if len(s) == 10:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    s = s.rstrip("Z").split(".", 1)[0]          # EUMETView writes 2026-11-23T15:40:00.000Z
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _period_seconds(p: str) -> float | None:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", p or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + s)


def parse_domain(text: str, *, daily: bool, limit: int = 2000) -> list[str]:
    """DescribeDomains ``<Domain>`` text (comma list of start/end/period or single values) -> times."""
    values: list[str] = []
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split("/")
        try:
            if len(parts) == 1:
                values.append(_fmt(_parse_iso(parts[0]), daily))
                continue
            start, end = _parse_iso(parts[0]), _parse_iso(parts[1])
            step = _period_seconds(parts[2] if len(parts) > 2 else "P1D") or 86400.0
        except ValueError:
            continue
        if step >= 28 * 86400:   # monthly or yearly products: keep the end points only
            values.extend([_fmt(start, daily), _fmt(end, daily)])
            continue
        n = int((end - start).total_seconds() // step)
        first = max(0, n - limit)
        for i in range(first, n + 1):
            values.append(_fmt(start + timedelta(seconds=i * step), daily))
    out = sorted(set(values))
    return out[-limit:]


def _fmt(dt: datetime, daily: bool) -> str:
    return dt.strftime("%Y-%m-%d") if daily else dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def domain_text_from_xml(body: bytes) -> str:
    root = ET.fromstring(body)
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "Domain":
            return (el.text or "").strip()
    return ""


def fallback_times(period: str | None, now: float | None = None, swath: bool = False) -> list[str]:
    """Sensible guesses when GIBS cannot be asked: yesterday for daily, typical latency for sub-daily."""
    now_dt = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
    if period == "PT5M":
        last = now_dt - timedelta(minutes=25)
        last = last.replace(minute=last.minute - last.minute % 5, second=0, microsecond=0)
        return [_fmt(last - timedelta(minutes=5 * i), False) for i in range(143, -1, -1)]
    if period == "PT10M":
        last = now_dt - timedelta(minutes=50)
        last = last.replace(minute=last.minute - last.minute % 10, second=0, microsecond=0)
        return [_fmt(last - timedelta(minutes=10 * i), False) for i in range(143, -1, -1)]
    if period == "PT30M":
        last = now_dt - timedelta(hours=6)
        last = last.replace(minute=0 if last.minute < 30 else 30, second=0, microsecond=0)
        return [_fmt(last - timedelta(minutes=30 * i), False) for i in range(47, -1, -1)]
    days = 1 if swath else 2
    last = (now_dt - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return [_fmt(last - timedelta(days=i), True) for i in range(13, -1, -1)]


class Gibs:
    """Per-layer availability from DescribeDomains (GIBS) or the EUMETView capabilities (Meteosat),
    cached; never raises to callers."""

    def __init__(self, net, log, status=None, wms=None):
        self.net = net
        self.log = log
        self.wms = wms                      # meteosat.Meteosat, or None (Meteosat layers then use estimates)
        self._status = status or (lambda **kw: None)
        self._lock = threading.Lock()
        self._domains: dict[str, tuple[float, list[str], bool]] = {}   # layer -> (fetched, times, estimated)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="space-gibs")

    def _fetch_domain(self, product: dict, layer: str) -> tuple[list[str], bool]:
        period = product.get("period")
        daily = period not in SUB_DAILY
        now = datetime.now(timezone.utc)
        if daily:
            span = f"{(now - timedelta(days=20)):%Y-%m-%d}--{(now + timedelta(days=2)):%Y-%m-%d}"
        else:
            span = f"{(now - timedelta(hours=30)):%Y-%m-%dT%H:%M:%SZ}--{(now + timedelta(hours=1)):%Y-%m-%dT%H:%M:%SZ}"
        url = f"{GIBS_BASE}/1.0.0/{layer}/default/{product['matrix']}/all/{span}.xml"
        try:
            body = self.net.get_bytes(url, timeout=12, retries=1, max_bytes=2_000_000)
            times = parse_domain(domain_text_from_xml(body), daily=daily)
            if not times:
                raise ValueError("empty time domain")
            return times, False
        except Exception as err:
            self.log.info("GIBS domain for %s unavailable: %s", layer, err)
            return fallback_times(period, swath=bool(product.get("swath"))), True

    def _wms_times(self, product: dict, layer: str) -> tuple[list[str], bool]:
        caps = self.wms.layers_or_none() if self.wms is not None else None
        info = (caps or {}).get(layer[len(M.PREFIX):])
        if info:
            times = parse_domain(info["time"], daily=False, limit=300)
            if times:
                return times, False
        return fallback_times(product.get("period")), True

    def times(self, product_id: str, layer: str) -> tuple[list[str], bool]:
        product = PRODUCTS[product_id]
        if not product.get("period") or product.get("fixed"):
            return ([product["fixed"]] if product.get("fixed") else []), False
        if layer.startswith(M.PREFIX):
            return self._wms_times(product, layer)     # the capabilities are cached by the client
        ttl = _DOMAIN_TTL["sub" if product["period"] in SUB_DAILY else "day"]
        with self._lock:
            hit = self._domains.get(layer)
        if hit and time.time() - hit[0] < (ttl if not hit[2] else 120.0):
            return hit[1], hit[2]
        times, estimated = self._fetch_domain(product, layer)
        with self._lock:
            self._domains[layer] = (time.time(), times, estimated)
        return times, estimated

    def catalogue(self, products: list[str] | None = None) -> dict:
        wanted = products or list(PRODUCTS)
        jobs = {}
        for pid in wanted:
            for layer in all_layers(PRODUCTS[pid]):
                jobs[(pid, layer)] = self._pool.submit(self.times, pid, layer)
        out = {}
        ok = 0
        est = 0
        for pid in wanted:
            p = PRODUCTS[pid]
            layers = []
            for layer in all_layers(p):
                try:
                    times, estimated = jobs[(pid, layer)].result(timeout=20)
                except Exception:
                    times, estimated = fallback_times(p.get("period"), swath=bool(p.get("swath"))), True
                if not layer.startswith(M.PREFIX):      # Meteosat reports its own status
                    ok += 0 if estimated else 1
                    est += 1 if estimated else 0
                keep = times[-48:] if p.get("period") in SUB_DAILY else times[-16:]
                le = {"layer": layer, "times": keep, "latest": keep[-1] if keep else None, "estimated": estimated}
                le["default"] = default_time(p, [le])
                le.update(layer_info(p, layer))
                layers.append(le)
            first = variant_layers(p)[0] if p.get("layers") else None
            entry = {k: v for k, v in p.items() if k not in _LAYER_KEYS}
            entry.update({"id": pid, "layers": layers,
                          "default_time": next((l["default"] for l in layers if l["layer"] == first), None)})
            out[pid] = entry
        if est and not ok:
            self._status(ok=False, error="GIBS time domains are unavailable; showing estimated dates",
                         count=len(out))
        else:
            self._status(ok=True, count=len(out), note=f"{len(out)} products" + (f", {est} estimated" if est else ""))
        return {"base": GIBS_BASE, "credit": GIBS_CREDIT, "products": out, "now": time.time()}


def default_time(product: dict, layers: list[dict]) -> str | None:
    """The time a layer opens at: the latest complete day for swath products, else the latest."""
    if product.get("fixed"):
        return product["fixed"]
    times = layers[0]["times"] if layers else []
    if not times:
        return None
    if product.get("settle"):                  # the newest listed days are still filling: step back
        return times[-1 - min(int(product["settle"]), len(times) - 1)]
    if product.get("period") == "P1D" and product.get("swath"):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        complete = [t for t in times if t < today]
        return complete[-1] if complete else times[-1]
    return times[-1]


def valid_time(text: str) -> bool:
    return bool(_SAFE_TIME.match(text or ""))


# ---------------------------------------------------------------------------------------------
# RainViewer
# ---------------------------------------------------------------------------------------------
_HOST = re.compile(r"^https://[A-Za-z0-9.-]+$")
_PATH = re.compile(r"^/v2/radar/[A-Za-z0-9_-]{2,64}$")


def parse_rainviewer(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("RainViewer sent something that is not a JSON object")
    host = str(data.get("host") or "")
    if not _HOST.match(host):
        raise ValueError("RainViewer sent an unexpected tile host")
    frames = []
    for f in ((data.get("radar") or {}).get("past") or []):
        try:
            t = int(f["time"])
            path = str(f["path"])
        except (KeyError, TypeError, ValueError):
            continue
        if _PATH.match(path):
            frames.append({"time": t, "path": path})
    frames.sort(key=lambda f: f["time"])
    return {"generated": int(data.get("generated") or 0), "host": host, "frames": frames[-24:],
            "max_zoom": RAINVIEWER_MAX_ZOOM, "colour": RAINVIEWER_COLOUR,
            "attribution": "Weather data by RainViewer", "attribution_url": "https://www.rainviewer.com/"}
