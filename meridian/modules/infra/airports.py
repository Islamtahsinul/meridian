"""Airports from OurAirports (public domain) with runways, frequencies and live METAR from
aviationweather.gov (US Government, public domain)."""

from __future__ import annotations

import csv
import io
import re
import time

from meridian import geo

from . import countries
from .common import Collection, compact_num, decode_text, fc, fnum, http_url, inum, point_feature, text, valid_point

BASE = "https://davidmegginson.github.io/ourairports-data/"
AIRPORTS_CSV = BASE + "airports.csv"
RUNWAYS_CSV = BASE + "runways.csv"
FREQS_CSV = BASE + "airport-frequencies.csv"
REGIONS_CSV = BASE + "regions.csv"
SOURCE_ID = "infra.ourairports"
METAR_SOURCE_ID = "infra.metar"
METAR_URL = "https://aviationweather.gov/api/data/metar"
METAR_HOST = "aviationweather.gov"

MIN_AIRPORTS = 20000
TYPES = {"large_airport": 3, "medium_airport": 2, "small_airport": 1}
TYPE_LABELS = {3: "Large airport", 2: "Medium airport", 1: "Small airport"}
SURFACES = {
    "ASP": "Asphalt", "ASPH": "Asphalt", "CON": "Concrete", "CONC": "Concrete", "PEM": "Asphalt", "BIT": "Bitumen",
    "GRS": "Grass", "TURF": "Grass", "GRVL": "Gravel", "GRV": "Gravel", "DIRT": "Dirt", "SAND": "Sand",
    "WATER": "Water", "SNOW": "Snow", "ICE": "Ice", "CLAY": "Clay", "COR": "Coral", "LAT": "Laterite",
    "UNK": "Unknown", "MATS": "Mats",
}
FREQ_KEEP = ("TWR", "GND", "ATIS", "APP", "DEP", "CTAF", "UNIC", "AFIS", "CLD", "DEL", "A/D", "RDO", "FSS", "AWOS",
             "ASOS", "A/G", "OPS", "MULTICOM", "RAMP", "APRON", "CNTR", "TML")


def surface_label(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    key = re.split(r"[-/ ,]", s.upper())[0]
    return SURFACES.get(key) or s.title()[:30]


def parse_regions(csv_text: str) -> dict[str, str]:
    return {r["code"]: r["name"] for r in csv.DictReader(io.StringIO(csv_text)) if r.get("code") and r.get("name")}


def parse_airports(csv_text: str, regions: dict[str, str] | None = None) -> list[dict]:
    regions = regions or {}
    out = []
    for r in csv.DictReader(io.StringIO(csv_text)):
        t = TYPES.get(r.get("type") or "")
        if not t:
            continue
        lat, lon = fnum(r.get("latitude_deg")), fnum(r.get("longitude_deg"))
        ident = (r.get("ident") or "").strip()
        if not ident or lat is None or lon is None or not valid_point(lat, lon) or len(ident) > 12:
            continue
        icao = (r.get("icao_code") or "").strip().upper()
        gps = (r.get("gps_code") or "").strip().upper()
        if not icao and re.fullmatch(r"[A-Z]{4}", gps):
            icao = gps
        iata = (r.get("iata_code") or "").strip().upper()
        cc = (r.get("iso_country") or "").strip().upper() or None
        region_code = (r.get("iso_region") or "").strip()
        out.append({
            "id": ident, "name": text(r.get("name"), 120) or ident, "type": t, "type_label": TYPE_LABELS[t],
            "lat": lat, "lon": lon, "elevation_ft": inum(r.get("elevation_ft")),
            "country": cc, "country_name": countries.name(cc),
            "region": regions.get(region_code) or None, "region_code": region_code or None,
            "municipality": text(r.get("municipality"), 80),
            "scheduled": (r.get("scheduled_service") or "").strip().lower() == "yes",
            "icao": icao or None, "iata": iata if re.fullmatch(r"[A-Z0-9]{3}", iata or "") else None,
            "gps_code": gps or None, "local_code": text(r.get("local_code"), 12),
            "home": http_url(r.get("home_link")), "wikipedia": http_url(r.get("wikipedia_link")),
            "keywords": text(r.get("keywords"), 200),
        })
    return out


def parse_runways(csv_text: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in csv.DictReader(io.StringIO(csv_text)):
        ident = (r.get("airport_ident") or "").strip()
        if not ident or (r.get("closed") or "0").strip() == "1":
            continue
        le, he = (r.get("le_ident") or "").strip(), (r.get("he_ident") or "").strip()
        rw = {"name": "/".join(x for x in (le, he) if x) or None,
              "length_ft": inum(r.get("length_ft")), "width_ft": inum(r.get("width_ft")),
              "surface": surface_label(r.get("surface")), "lighted": (r.get("lighted") or "").strip() == "1",
              "heading": compact_num(fnum(r.get("le_heading_degT")), 0)}
        ends = []
        for p in ("le", "he"):
            la, lo = fnum(r.get(f"{p}_latitude_deg")), fnum(r.get(f"{p}_longitude_deg"))
            if la is not None and lo is not None and valid_point(la, lo):
                ends.append([round(lo, 5), round(la, 5)])
        if len(ends) == 2:
            rw["ends"] = ends
        out.setdefault(ident, []).append(rw)
    for rws in out.values():
        rws.sort(key=lambda x: -(x.get("length_ft") or 0))
    return out


def parse_frequencies(csv_text: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in csv.DictReader(io.StringIO(csv_text)):
        ident = (r.get("airport_ident") or "").strip()
        mhz = fnum(r.get("frequency_mhz"))
        if not ident or mhz is None or not (0.1 < mhz < 1000):
            continue
        kind = (r.get("type") or "").strip().upper()[:12]
        out.setdefault(ident, []).append({"type": kind or None, "description": text(r.get("description"), 60),
                                          "mhz": round(mhz, 3)})
    order = {k: i for i, k in enumerate(FREQ_KEEP)}
    for fs in out.values():
        fs.sort(key=lambda f: (order.get(f["type"] or "", 99), f["mhz"]))
        del fs[24:]
    return out


def to_feature(a: dict) -> dict:
    props = {"id": a["id"], "n": a["name"][:60], "t": a["type"]}
    if a.get("iata"):
        props["i"] = a["iata"]
    elif a["type"] >= 2 and a.get("icao"):
        props["i"] = a["icao"]
    if a.get("scheduled"):
        props["s"] = 1
    return point_feature(a["lon"], a["lat"], props)


def build(ds) -> None:
    regions = {}
    try:
        regions = parse_regions(decode_text(ds.download(REGIONS_CSV, "regions.csv", min_bytes=50_000).read_bytes()))
    except Exception as err:
        ds.note(f"Region names unavailable ({err})")
    airports = parse_airports(decode_text(ds.download(AIRPORTS_CSV, "airports.csv", min_bytes=3_000_000, timeout=240)
                              .read_bytes()), regions)
    if len(airports) < MIN_AIRPORTS:
        raise RuntimeError(f"OurAirports looks incomplete ({len(airports)} airports)")
    runways, freqs = {}, {}
    try:
        runways = parse_runways(decode_text(ds.download(RUNWAYS_CSV, "runways.csv", min_bytes=500_000, timeout=240)
                                .read_bytes()))
    except Exception as err:
        ds.note(f"Runways unavailable ({err})")
    try:
        freqs = parse_frequencies(decode_text(ds.download(FREQS_CSV, "airport-frequencies.csv", min_bytes=100_000, timeout=240)
                                  .read_bytes()))
    except Exception as err:
        ds.note(f"Frequencies unavailable ({err})")
    for a in airports:
        a["runways"] = runways.get(a["id"], [])[:12]
        a["frequencies"] = freqs.get(a["id"], [])
        lengths = [r["length_ft"] for r in a["runways"] if r.get("length_ft")]
        a["longest_runway_ft"] = max(lengths) if lengths else None
    airports.sort(key=lambda a: (a["type"], a.get("scheduled", False)))
    major = [to_feature(a) for a in airports if a["type"] >= 2]
    minor = [to_feature(a) for a in airports if a["type"] == 1]
    ds.write_json("airports-major.geojson", fc(major))
    ds.write_json("airports-minor.geojson", fc(minor))
    ds.write_json("airports-details.json", {"built_at": time.time(), "items": airports})
    n_large = sum(1 for a in airports if a["type"] == 3)
    ds.set_rows(len(airports))
    ds.note(f"{len(airports):,} airports ({n_large:,} large, {len(major) - n_large:,} medium, {len(minor):,} small)")


def subtitle(a: dict) -> str:
    codes = ", ".join(x for x in (a.get("iata"), a.get("icao") or a["id"]) if x)
    place = ", ".join(x for x in (a.get("municipality"), a.get("country_name")) if x)
    return ", ".join(x for x in (codes, place) if x) or a["type_label"]


def collection(items: list[dict], built_at: float | None) -> Collection:
    weights = {3: 1.0, 2: 0.55, 1: 0.15}
    rows = []
    for a in items:
        w = weights[a["type"]] + (0.1 if a.get("scheduled") else 0.0)
        codes = [c for c in (a["id"], a.get("icao"), a.get("iata"), a.get("gps_code"), a.get("local_code")) if c]
        rows.append({"id": a["id"], "title": a["name"], "subtitle": subtitle(a), "lat": a["lat"], "lon": a["lon"],
                     "weight": min(1.0, w), "country": a.get("country"), "codes": codes,
                     "text": " ".join(x for x in (a.get("municipality"), a.get("keywords"), a.get("region")) if x),
                     "detail": a})
    return Collection("airport", "infra.airports", rows, cell_deg=0.5, zoom=12, built_at=built_at)


def country_counts(items: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for a in items:
        cc = a.get("country")
        if not cc:
            continue
        row = out.setdefault(cc, {"airports": 0, "large": 0, "medium": 0, "small": 0})
        row["airports"] += 1
        row[{3: "large", 2: "medium", 1: "small"}[a["type"]]] += 1
    return out


# ---------------------------------------------------------------------------------------------
# METAR (live weather reports), cached about five minutes
# ---------------------------------------------------------------------------------------------
_CATEGORY = {"VFR": "Visual (VFR)", "MVFR": "Marginal visual (MVFR)", "IFR": "Instrument (IFR)",
             "LIFR": "Low instrument (LIFR)"}
_COVER = {"SKC": "Clear", "CLR": "Clear", "NCD": "No cloud detected", "NSC": "No significant cloud", "FEW": "Few",
          "SCT": "Scattered", "BKN": "Broken", "OVC": "Overcast", "OVX": "Sky obscured", "VV": "Vertical visibility",
          "CAVOK": "CAVOK"}


def parse_metar(item: dict) -> dict | None:
    """One aviationweather.gov JSON METAR -> compact summary for the airport card."""
    if not isinstance(item, dict) or not item.get("rawOb"):
        return None
    clouds = []
    for c in item.get("clouds") or []:
        if not isinstance(c, dict):
            continue
        cover = str(c.get("cover") or "")
        base = c.get("base")
        label = _COVER.get(cover, cover)
        clouds.append(f"{label} at {int(base):,} ft" if isinstance(base, (int, float)) else label)
    wdir, wspd, wgst = item.get("wdir"), item.get("wspd"), item.get("wgst")
    if wspd in (0, None) and wdir in (0, None):
        wind = "Calm" if wspd == 0 else None
    else:
        d = "Variable" if str(wdir).upper() == "VRB" else (f"{int(wdir):03d}°" if isinstance(wdir, (int, float)) else None)
        wind = " at ".join(x for x in (d, f"{int(wspd)} kn" if isinstance(wspd, (int, float)) else None) if x)
        if isinstance(wgst, (int, float)) and wgst:
            wind += f", gusts {int(wgst)} kn"
    vis = _visibility(item.get("visib"))
    obs = item.get("obsTime")
    return {
        "station": text(item.get("icaoId"), 8), "name": text(item.get("name"), 80), "raw": text(item.get("rawOb"), 300),
        "time": int(obs) if isinstance(obs, (int, float)) else None,
        "category": item.get("fltCat") if item.get("fltCat") in _CATEGORY else None,
        "category_label": _CATEGORY.get(item.get("fltCat") or ""),
        "temp_c": item.get("temp") if isinstance(item.get("temp"), (int, float)) else None,
        "dewpoint_c": item.get("dewp") if isinstance(item.get("dewp"), (int, float)) else None,
        "wind": wind or None, "wind_dir": wdir if isinstance(wdir, (int, float)) else None,
        "wind_kt": wspd if isinstance(wspd, (int, float)) else None,
        "gust_kt": wgst if isinstance(wgst, (int, float)) else None,
        "visibility": vis, "altimeter_hpa": item.get("altim") if isinstance(item.get("altim"), (int, float)) else None,
        "clouds": clouds[:4], "weather": text(item.get("wxString"), 60),
        "lat": item.get("lat"), "lon": item.get("lon"),
    }


def _visibility(value) -> str | None:
    """aviationweather.gov reports statute miles: 6+ (10 km or more), 3, 0.25, "1/2"."""
    if value in (None, ""):
        return None
    sv = str(value).strip()
    if sv.endswith("+"):
        return "10 km or more"
    m = re.fullmatch(r"(\d+)?\s*(\d+)/(\d+)", sv)
    miles = (int(m.group(1) or 0) + int(m.group(2)) / int(m.group(3))) if m and int(m.group(3)) else fnum(sv)
    if miles is None:
        return text(sv, 20)
    if miles >= 6:
        return "10 km or more"
    km = miles * 1.609344
    return f"{km:.1f} km" if km < 10 else f"{km:.0f} km"


def fetch_metar(net, airport: dict) -> dict:
    """Latest METAR for the airport, or the nearest report within about 40 km."""
    icao = airport.get("icao")
    reports = []
    if icao and re.fullmatch(r"[A-Z0-9]{4}", icao):
        data = net.get_json(METAR_URL, params={"ids": icao, "format": "json"}, ttl=300, timeout=12, retries=1)
        reports = [r for r in (parse_metar(x) for x in (data or [])) if r]
        if reports:
            return {"report": reports[0], "nearest": False, "distance_km": 0.0}
    lat, lon = airport["lat"], airport["lon"]
    d = 0.4
    bbox = f"{lat - d:.3f},{lon - d:.3f},{lat + d:.3f},{lon + d:.3f}"
    data = net.get_json(METAR_URL, params={"bbox": bbox, "format": "json"}, ttl=300, timeout=12, retries=1)
    best = None
    for x in data or []:
        r = parse_metar(x)
        if not r or not geo.valid_latlon(r.get("lat"), r.get("lon")):
            continue
        dist = geo.haversine_km(lat, lon, float(r["lat"]), float(r["lon"]))
        if dist <= 40 and (best is None or dist < best[0]):
            best = (dist, r)
    if best:
        return {"report": best[1], "nearest": True, "distance_km": round(best[0], 1)}
    return {"report": None, "nearest": False, "distance_km": None}
