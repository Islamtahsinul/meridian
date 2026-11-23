"""Power plants from the WRI Global Power Plant Database v1.3.0 (CC BY 4.0), about 35,000 plants."""

from __future__ import annotations

import csv
import io
import time
import zipfile

from . import countries
from .common import (Collection, compact_num, decode_text, fc, fnum, log_weight, point_feature, text, valid_point,
                     year_of)

GPPD_CSV = ("https://raw.githubusercontent.com/wri/global-power-plant-database/master/output_database/"
            "global_power_plant_database.csv")
GPPD_ZIP = "https://wri-dataportal-prod.s3.amazonaws.com/manual/global_power_plant_database_v_1_3.zip"
GPPD_PAGE = "https://datasets.wri.org/dataset/globalpowerplantdatabase"
SOURCE_ID = "infra.gppd"
MINOR_MW = 30
MIN_PLANTS = 20000   # sanity check: a shorter list means a broken download   # plants below this go in power-minor.geojson, loaded by the browser only when zoomed in

# GPPD primary_fuel -> short code used on the map (colours live in the browser legend)
FUELS = {
    "coal": "coal", "gas": "gas", "oil": "oil", "nuclear": "nuclear", "hydro": "hydro", "wind": "wind",
    "solar": "solar", "biomass": "biomass", "waste": "waste", "geothermal": "geothermal", "storage": "storage",
    "cogeneration": "cogeneration", "petcoke": "petcoke", "wave and tidal": "tidal", "other": "other",
}
FUEL_LABELS = {
    "coal": "Coal", "gas": "Gas", "oil": "Oil", "nuclear": "Nuclear", "hydro": "Hydro", "wind": "Wind",
    "solar": "Solar", "biomass": "Biomass", "waste": "Waste", "geothermal": "Geothermal", "storage": "Storage",
    "cogeneration": "Cogeneration", "petcoke": "Petcoke", "tidal": "Wave and tidal", "other": "Other",
}


def fuel_code(value: str | None) -> str:
    return FUELS.get((value or "").strip().lower(), "other")


def parse_gppd(csv_text: str) -> list[dict]:
    """GPPD CSV text -> plant records (only rows with a valid position and capacity)."""
    out = []
    for r in csv.DictReader(io.StringIO(csv_text)):
        lat, lon = fnum(r.get("latitude")), fnum(r.get("longitude"))
        pid = (r.get("gppd_idnr") or "").strip()
        if not pid or lat is None or lon is None or not valid_point(lat, lon):
            continue
        cap = fnum(r.get("capacity_mw"), 0.0) or 0.0
        fuel = fuel_code(r.get("primary_fuel"))
        others = []
        for k in ("other_fuel1", "other_fuel2", "other_fuel3"):
            f = (r.get(k) or "").strip()
            if f:
                others.append(FUEL_LABELS.get(fuel_code(f), f))
        gen_year = gen = None
        for y in range(2019, 2012, -1):
            v = fnum(r.get(f"generation_gwh_{y}"))
            if v is not None:
                gen_year, gen = y, v
                break
        est_year = est = None
        for y in range(2017, 2012, -1):
            v = fnum(r.get(f"estimated_generation_gwh_{y}"))
            if v is not None:
                est_year, est = y, v
                break
        iso2 = countries.from_alpha3(r.get("country")) or countries.from_name(r.get("country_long"))
        out.append({
            "id": pid,
            "name": text(r.get("name"), 160) or pid,
            "lat": lat, "lon": lon,
            "country": iso2,
            "country_name": text(r.get("country_long"), 80),
            "capacity_mw": compact_num(cap, 1),
            "fuel": fuel,
            "fuel_label": FUEL_LABELS.get(fuel, "Other"),
            "other_fuels": others,
            "commissioning_year": year_of(r.get("commissioning_year")),
            "owner": text(r.get("owner"), 160),
            "source": text(r.get("source"), 120),
            "source_url": (r.get("url") or "").strip()[:400] or None,
            "geolocation_source": text(r.get("geolocation_source"), 120),
            "wepp_id": text(r.get("wepp_id"), 40),
            "capacity_year": year_of(r.get("year_of_capacity_data")),
            "generation_gwh": compact_num(gen, 1) if gen is not None else None,
            "generation_year": gen_year,
            "generation_source": text(r.get("generation_data_source"), 120),
            "estimated_generation_gwh": compact_num(est, 1) if est is not None else None,
            "estimated_generation_year": est_year,
        })
    return out


def to_feature(p: dict) -> dict:
    return point_feature(p["lon"], p["lat"], {"id": p["id"], "n": p["name"][:60], "c": p["capacity_mw"],
                                              "f": p["fuel"]})


def build(ds) -> None:
    try:
        path = ds.download(GPPD_CSV, "gppd.csv", min_bytes=5_000_000, timeout=240)
        raw = decode_text(path.read_bytes())
    except Exception as err:   # the GitHub copy failed and there is no older copy: try WRI's zip
        ds.log.warning("GPPD CSV unavailable (%s); trying the WRI zip", err)
        zpath = ds.download(GPPD_ZIP, "gppd.zip", min_bytes=1_000_000, timeout=240)
        with zipfile.ZipFile(zpath) as z:
            name = next(n for n in z.namelist() if n.lower().endswith("global_power_plant_database.csv"))
            if z.getinfo(name).file_size > 200_000_000:
                raise RuntimeError("GPPD zip member is unexpectedly large")
            raw = decode_text(z.read(name))
    plants = parse_gppd(raw)
    if len(plants) < MIN_PLANTS:
        raise RuntimeError(f"GPPD looks incomplete ({len(plants)} plants)")
    plants.sort(key=lambda p: p["capacity_mw"] or 0)   # small first, so big plants draw on top
    ds.write_json("power.geojson", fc([to_feature(p) for p in plants if (p["capacity_mw"] or 0) >= MINOR_MW]))
    ds.write_json("power-minor.geojson", fc([to_feature(p) for p in plants if (p["capacity_mw"] or 0) < MINOR_MW]))
    ds.write_json("power-details.json", {"built_at": time.time(), "items": plants})
    total = sum(p["capacity_mw"] or 0 for p in plants)
    ds.set_rows(len(plants))
    ds.note(f"{len(plants):,} plants, {total / 1000:,.0f} GW")


def subtitle(p: dict) -> str:
    cap = p.get("capacity_mw") or 0
    parts = [p.get("fuel_label") or "Power plant", f"{cap:,.0f} MW" if cap >= 1 else f"{cap:g} MW"]
    if p.get("country_name"):
        parts.append(p["country_name"])
    return ", ".join(parts)


def collection(items: list[dict], built_at: float | None) -> Collection:
    rows = []
    for p in items:
        rows.append({"id": p["id"], "title": p["name"], "subtitle": subtitle(p), "lat": p["lat"], "lon": p["lon"],
                     "weight": log_weight(p.get("capacity_mw"), 1, 10000), "country": p.get("country"),
                     "text": " ".join(x for x in (p.get("owner"), p.get("fuel_label")) if x),
                     "codes": [p["id"]] + ([p["wepp_id"]] if p.get("wepp_id") else []), "detail": p})
    return Collection("power_plant", "infra.power", rows, cell_deg=1.0, zoom=11, built_at=built_at)


def country_stats(items: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in items:
        cc = p.get("country")
        if not cc:
            continue
        row = out.setdefault(cc, {"power_plants": 0, "capacity_mw": {}})
        row["power_plants"] += 1
        fuel = p.get("fuel_label") or "Other"
        row["capacity_mw"][fuel] = row["capacity_mw"].get(fuel, 0) + (p.get("capacity_mw") or 0)
    for row in out.values():
        row["capacity_mw"] = {k: round(v, 1) for k, v in sorted(row["capacity_mw"].items(), key=lambda kv: -kv[1])}
    return out
