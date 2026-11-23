"""Live electricity grids: constants, source tables and pure parsers (no network here).

Great Britain comes from two keyless official sources:
- NESO's Carbon Intensity API (CC BY 4.0): national and regional carbon intensity, actual and
  forecast, and the generation mix including embedded wind and solar.
- Elexon's Insights Solution (BMRS data licence, attribution required): generation by fuel every
  5 minutes (FUELINST), national demand (INDO), interconnector flows and the market index price.

Europe comes from Fraunhofer ISE's Energy-Charts API (CC BY 4.0; day-ahead prices from
Bundesnetzagentur | SMARD.de, CC BY 4.0): the renewable share of load now (/signal), public net
generation by type (/public_power) and day-ahead prices (/price).

Carbon intensity outside GB is an estimate: the latest generation mix times the emission factors
NESO publishes for GB fuels (direct CO2 only, domestic generation, no imports).
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone

from .common import fnum

# -- endpoints and source ids -------------------------------------------------------------------
CI_API = "https://api.carbonintensity.org.uk"
CI_HOST = "api.carbonintensity.org.uk"
CI_PAGE = "https://carbonintensity.org.uk/"
ELEXON_API = "https://data.elexon.co.uk/bmrs/api/v1"
ELEXON_HOST = "data.elexon.co.uk"
ELEXON_PAGE = "https://bmrs.elexon.co.uk/"
ELEXON_LICENCE = "https://www.elexon.co.uk/bsc/data/balancing-mechanism-reporting-agent/copyright-licence-bmrs-data/"
EC_API = "https://api.energy-charts.info"
EC_HOST = "api.energy-charts.info"
EC_PAGE = "https://energy-charts.info/"

SRC_CI = "infra.carbonintensity"
SRC_ELEXON = "infra.elexon"
SRC_EC = "infra.energycharts"

ELEXON_ATTRIBUTION = f"Contains BMRS data © Elexon Limited copyright and database right {time.gmtime().tm_year}"
NESO_ATTRIBUTION = "Supported by National Energy SO Open Data"

# -- Great Britain --------------------------------------------------------------------------------
INDEX_LABELS = {"very low": "Very low", "low": "Low", "moderate": "Moderate", "high": "High", "very high": "Very high"}
CI_FUELS = {"gas": "Gas", "coal": "Coal", "nuclear": "Nuclear", "wind": "Wind", "solar": "Solar", "hydro": "Hydro",
            "biomass": "Biomass", "imports": "Imports", "other": "Other"}
CI_RENEWABLE = ("wind", "solar", "hydro", "biomass")
CI_FOSSIL = ("gas", "coal")
CI_REGION_IDS = range(1, 15)            # the 14 DNO regions; 15-18 are England, Scotland, Wales, GB

ELEXON_FUELS = {   # FUELINST fuel type -> (group, label)
    "CCGT": ("gas", "Gas, combined cycle"), "OCGT": ("gas", "Gas, open cycle"), "COAL": ("coal", "Coal"),
    "OIL": ("oil", "Oil"), "NUCLEAR": ("nuclear", "Nuclear"), "WIND": ("wind", "Wind (metered)"),
    "BIOMASS": ("biomass", "Biomass"), "NPSHYD": ("hydro", "Hydro"), "PS": ("storage", "Pumped storage"),
    "OTHER": ("other", "Other"),
}
# Interconnectors (FUELINST codes). Positions are the converter stations, approximate; map lines are
# straight between them, not the cable routes.
INTERCONNECTORS = {
    "INTFR": {"name": "IFA", "to": "France", "country": "FR", "capacity_mw": 2000,
              "gb": (51.1065, 0.9752), "far": (50.8962, 1.7737)},
    "INTIFA2": {"name": "IFA2", "to": "France", "country": "FR", "capacity_mw": 1000,
                "gb": (50.8165, -1.2015), "far": (49.1495, -0.1033)},
    "INTELEC": {"name": "ElecLink", "to": "France", "country": "FR", "capacity_mw": 1000,
                "gb": (51.0960, 1.1321), "far": (50.9277, 1.8077)},
    "INTNED": {"name": "BritNed", "to": "Netherlands", "country": "NL", "capacity_mw": 1000,
               "gb": (51.4452, 0.7183), "far": (51.9654, 4.0347)},
    "INTNEM": {"name": "Nemo Link", "to": "Belgium", "country": "BE", "capacity_mw": 1000,
               "gb": (51.3076, 1.3427), "far": (51.2805, 3.2087)},
    "INTNSL": {"name": "North Sea Link", "to": "Norway", "country": "NO", "capacity_mw": 1400,
               "gb": (55.1636, -1.5297), "far": (59.5147, 6.6447)},
    "INTVKL": {"name": "Viking Link", "to": "Denmark", "country": "DK", "capacity_mw": 1400,
               "gb": (52.9265, -0.2016), "far": (55.5226, 9.1907)},
    "INTIRL": {"name": "Moyle", "to": "Northern Ireland", "country": None, "capacity_mw": 500,
               "gb": (55.0717, -4.9806), "far": (54.8532, -5.7718)},
    "INTEW": {"name": "East West", "to": "Ireland", "country": "IE", "capacity_mw": 500,
              "gb": (53.2264, -3.0752), "far": (53.4813, -6.4752)},
    "INTGRNL": {"name": "Greenlink", "to": "Ireland", "country": "IE", "capacity_mw": 504,
                "gb": (51.6745, -4.9868), "far": (52.3561, -6.9637)},
}

# NESO Carbon Intensity methodology, table 1 (gCO2/kWh, direct emissions). Fuels NESO does not list
# take the closest listed fuel: lignite and peat as coal, oil shale as oil, waste as "other".
EMISSION_FACTORS = {"coal": 937, "oil": 935, "gas": 394, "biomass": 120, "other": 300, "waste": 300,
                    "nuclear": 0, "hydro": 0, "wind": 0, "solar": 0, "storage": 0, "geothermal": 0,
                    "other_renewable": 0}

# -- Europe (Energy-Charts) ------------------------------------------------------------------------
# (Energy-Charts country code, ISO alpha-2, day-ahead bidding zone or None). Order: rough system size,
# so the biggest grids fill first. Great Britain comes from NESO and Elexon instead.
EC_COUNTRIES = [
    ("de", "DE", "DE-LU"), ("fr", "FR", "FR"), ("es", "ES", "ES"), ("it", "IT", "IT-North"), ("pl", "PL", "PL"),
    ("nl", "NL", "NL"), ("se", "SE", "SE3"), ("no", "NO", "NO1"), ("be", "BE", "BE"), ("ie", "IE", "IE(SEM)"),
    ("dk", "DK", "DK1"), ("at", "AT", "AT"), ("ch", "CH", "CH"), ("pt", "PT", "PT"), ("cz", "CZ", "CZ"),
    ("fi", "FI", "FI"), ("gr", "GR", "GR"), ("ro", "RO", "RO"), ("hu", "HU", "HU"), ("bg", "BG", "BG"),
    ("ua", "UA", "UA-IPS"), ("sk", "SK", "SK"), ("rs", "RS", "RS"), ("hr", "HR", "HR"), ("si", "SI", "SI"),
    ("lt", "LT", "LT"), ("ee", "EE", "EE"), ("lv", "LV", "LV"), ("ba", "BA", None), ("me", "ME", "ME"),
    ("mk", "MK", None), ("lu", "LU", "DE-LU"), ("cy", "CY", None), ("md", "MD", None), ("ge", "GE", None),
    ("xk", "XK", None),
]
EC_BY_ISO = {iso: (code, zone) for code, iso, zone in EC_COUNTRIES}
ZONE_NAMES = {"DE-LU": "Germany and Luxembourg", "IT-North": "northern Italy", "SE3": "Stockholm area (SE3)",
              "NO1": "Oslo area (NO1)", "DK1": "western Denmark (DK1)", "IE(SEM)": "the all-island Irish market",
              "UA-IPS": "Ukraine's main grid"}

EC_TYPES = {
    "Nuclear": "nuclear", "Hydro Run-of-River": "hydro", "Hydro water reservoir": "hydro",
    "Hydro pumped storage": "storage", "Battery": "storage", "Biomass": "biomass",
    "Fossil brown coal / lignite": "coal", "Fossil hard coal": "coal", "Fossil coal-derived gas": "coal",
    "Fossil peat": "coal", "Fossil oil": "oil", "Fossil oil shale": "oil", "Fossil gas": "gas",
    "Geothermal": "geothermal", "Waste": "waste", "Others": "other", "Other renewables": "other_renewable",
    "Wind offshore": "wind", "Wind onshore": "wind", "Solar": "solar", "Marine": "other_renewable",
}
EC_NOT_GENERATION = {"Hydro pumped storage consumption", "Battery Consumption", "Cross border electricity trading",
                     "Load", "Residual load", "Renewable share of load", "Renewable share of generation",
                     "Load (incl. self-consumption)"}
CATEGORY_LABELS = {"coal": "Coal and lignite", "gas": "Gas", "oil": "Oil", "nuclear": "Nuclear", "hydro": "Hydro",
                   "wind": "Wind", "solar": "Solar", "biomass": "Biomass", "geothermal": "Geothermal", "waste": "Waste",
                   "storage": "Storage", "other_renewable": "Other renewables", "other": "Other",
                   "imports": "Imports"}
RENEWABLE_CATS = {"hydro", "wind", "solar", "biomass", "geothermal", "other_renewable"}
FOSSIL_CATS = {"coal", "gas", "oil"}

POSTCODE_RE = re.compile(r"^[A-Z]{1,2}[0-9][0-9A-Z]?$")


# ---------------------------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------------------------
def iso_ts(value) -> int | None:
    """"2026-11-23T14:00Z" (or with seconds or an offset) -> epoch seconds."""
    s = str(value or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def ci_time(ts: float) -> str:
    """Epoch seconds -> the Carbon Intensity API's "YYYY-MM-DDTHH:MMZ" form."""
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts))


def half_hour_floor(ts: float) -> int:
    return int(ts // 1800 * 1800)


def utc_date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _num(v) -> float | None:
    x = fnum(v)
    return x if x is not None and math.isfinite(x) else None


def _r(v: float | None, d: int = 1):
    if v is None:
        return None
    out = round(float(v), d)
    return int(out) if d == 0 else out


# ---------------------------------------------------------------------------------------------
# NESO Carbon Intensity API
# ---------------------------------------------------------------------------------------------
def parse_ci_series(obj) -> list[dict]:
    """/intensity[/{from}/{to}] -> [{t, to, forecast, actual, index}] oldest first."""
    rows = obj.get("data") if isinstance(obj, dict) else None
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t, to = iso_ts(r.get("from")), iso_ts(r.get("to"))
        it = r.get("intensity") or {}
        if t is None or to is None:
            continue
        idx = str(it.get("index") or "").strip().lower() or None
        out.append({"t": t, "to": to, "forecast": _num(it.get("forecast")), "actual": _num(it.get("actual")),
                    "index": idx if idx in INDEX_LABELS else None})
    out.sort(key=lambda r: r["t"])
    return out


def parse_ci_mix(items) -> list[dict]:
    """[{fuel, perc}] -> [{fuel, label, pct}] largest first (unknown fuels dropped)."""
    out = []
    for g in items or []:
        if not isinstance(g, dict):
            continue
        fuel = str(g.get("fuel") or "").strip().lower()
        pct = _num(g.get("perc"))
        if fuel in CI_FUELS and pct is not None:
            out.append({"fuel": fuel, "label": CI_FUELS[fuel], "pct": round(max(0.0, pct), 1)})
    out.sort(key=lambda g: -g["pct"])
    return out


def mix_shares(mix: list[dict]) -> dict:
    """Renewable, fossil and low-carbon shares (percent) of a NESO mix."""
    by = {g["fuel"]: g["pct"] for g in mix}
    ren = sum(by.get(f, 0.0) for f in CI_RENEWABLE)
    fos = sum(by.get(f, 0.0) for f in CI_FOSSIL)
    return {"renewable_pct": round(ren, 1), "fossil_pct": round(fos, 1),
            "low_carbon_pct": round(ren + by.get("nuclear", 0.0), 1), "imports_pct": round(by.get("imports", 0.0), 1)}


def parse_ci_regional(obj) -> dict:
    """/regional -> {t, to, regions: [{id, name, dno, intensity, index, mix, shares...}], gb: {...}}."""
    data = obj.get("data") if isinstance(obj, dict) else None
    block = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    out = {"t": iso_ts(block.get("from")), "to": iso_ts(block.get("to")), "regions": [], "aggregates": {}}
    for r in block.get("regions") or []:
        if not isinstance(r, dict):
            continue
        try:
            rid = int(r.get("regionid"))
        except (TypeError, ValueError):
            continue
        it = r.get("intensity") or {}
        mix = parse_ci_mix(r.get("generationmix"))
        idx = str(it.get("index") or "").strip().lower()
        rec = {"id": rid, "name": str(r.get("shortname") or "").strip()[:60],
               "dno": str(r.get("dnoregion") or "").strip()[:80], "intensity": _num(it.get("forecast")),
               "index": idx if idx in INDEX_LABELS else None, "mix": mix, **mix_shares(mix)}
        if rid in CI_REGION_IDS:
            out["regions"].append(rec)
        else:
            out["aggregates"][rec["name"] or str(rid)] = rec
    out["regions"].sort(key=lambda r: r["id"])
    return out


def parse_ci_region_forecast(obj) -> dict:
    """/regional/intensity/{from}/fw24h/regionid/{id} or /regional/postcode/{pc} -> one region with its
    half-hourly forecast [{t, forecast, index, renewable_pct}]."""
    data = obj.get("data") if isinstance(obj, dict) else None
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    data = data if isinstance(data, dict) else {}
    series = []
    for r in data.get("data") or []:
        if not isinstance(r, dict):
            continue
        t = iso_ts(r.get("from"))
        it = r.get("intensity") or {}
        if t is None:
            continue
        mix = parse_ci_mix(r.get("generationmix"))
        idx = str(it.get("index") or "").strip().lower()
        series.append({"t": t, "forecast": _num(it.get("forecast")), "index": idx if idx in INDEX_LABELS else None,
                       "mix": mix, **mix_shares(mix)})
    series.sort(key=lambda r: r["t"])
    try:
        rid = int(data.get("regionid"))
    except (TypeError, ValueError):
        rid = None
    return {"id": rid, "name": str(data.get("shortname") or "").strip()[:60],
            "dno": str(data.get("dnoregion") or "").strip()[:80],
            "postcode": str(data.get("postcode") or "").strip()[:8] or None, "series": series}


def intensity_now(series: list[dict], now: float) -> dict | None:
    """The period that contains ``now`` (its forecast is NESO's current estimate) and the latest
    period with a measured ("actual") value."""
    cur = next((r for r in series if r["t"] <= now < r["to"]), None)
    if cur is None:
        past = [r for r in series if r["t"] <= now]
        cur = past[-1] if past and now - past[-1]["to"] < 3600 else None
    if cur is None:
        return None
    last_actual = next((r for r in reversed(series) if r["actual"] is not None and r["to"] <= now + 60), None)
    value = cur["actual"] if cur["actual"] is not None else cur["forecast"]
    return {"t": cur["t"], "to": cur["to"], "value": value, "forecast": cur["forecast"], "index": cur["index"],
            "last_actual": last_actual["actual"] if last_actual else None,
            "last_actual_t": last_actual["t"] if last_actual else None}


def window_extremes(series: list[dict], now: float, hours: float = 3.0, ahead_h: float = 24.0) -> dict:
    """Cleanest and dirtiest ``hours``-long windows in the forecast for the next ``ahead_h`` hours."""
    fut = [r for r in series if r["to"] > now and r["t"] < now + ahead_h * 3600 and r["forecast"] is not None]
    n = max(1, int(round(hours * 2)))
    if len(fut) < n:
        return {}
    best = worst = None
    for i in range(0, len(fut) - n + 1):
        chunk = fut[i:i + n]
        if chunk[-1]["t"] - chunk[0]["t"] > (n - 1) * 1800 + 60:   # a gap in the series
            continue
        avg = sum(r["forecast"] for r in chunk) / n
        item = {"from": chunk[0]["t"], "to": chunk[-1]["to"], "avg": round(avg)}
        if best is None or avg < best["avg"]:
            best = item
        if worst is None or avg > worst["avg"]:
            worst = item
    return {"cleanest": best, "dirtiest": worst, "hours": hours} if best else {}


def sparkline_points(series: list[dict], now: float) -> dict:
    """[[t, value]] for the past (measured where known) and the future (forecast) halves."""
    past, future = [], []
    for r in series:
        if r["to"] <= now:
            v = r["actual"] if r["actual"] is not None else r["forecast"]
            if v is not None:
                past.append([r["t"], v])
        elif r["forecast"] is not None:
            future.append([r["t"], r["forecast"]])
    return {"past": past, "future": future}


# ---------------------------------------------------------------------------------------------
# Elexon Insights Solution
# ---------------------------------------------------------------------------------------------
def parse_fuelinst(obj) -> dict | None:
    """/datasets/FUELINST rows -> the latest complete 5-minute record:
    {t, fuels: [{code, group, label, mw}], interconnectors: [{code, ..., mw}], total_mw, net_import_mw}."""
    rows = obj.get("data") if isinstance(obj, dict) else obj
    by_time: dict[int, dict[str, float]] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t = iso_ts(r.get("startTime"))
        code = str(r.get("fuelType") or "").strip().upper()
        mw = _num(r.get("generation"))
        if t is None or not code or mw is None:
            continue
        by_time.setdefault(t, {})[code] = mw     # later publications of the same interval win
    if not by_time:
        return None
    widest = max(len(v) for v in by_time.values())
    t = max(k for k, v in by_time.items() if len(v) >= widest - 1)
    vals = by_time[t]
    fuels, links = [], []
    for code, mw in vals.items():
        if code in INTERCONNECTORS:
            meta = INTERCONNECTORS[code]
            links.append({"code": code, "name": meta["name"], "to": meta["to"], "country": meta["country"],
                          "capacity_mw": meta["capacity_mw"], "mw": round(mw),
                          "direction": "import" if mw > 0.5 else ("export" if mw < -0.5 else "idle")})
        elif code in ELEXON_FUELS:
            group, label = ELEXON_FUELS[code]
            fuels.append({"code": code, "group": group, "label": label, "mw": round(mw)})
    fuels.sort(key=lambda f: -f["mw"])
    links.sort(key=lambda x: -abs(x["mw"]))
    total = sum(f["mw"] for f in fuels if f["mw"] > 0)
    net = sum(x["mw"] for x in links)
    return {"t": t, "fuels": fuels, "interconnectors": links, "generation_mw": round(total),
            "net_import_mw": round(net), "imports_mw": round(sum(x["mw"] for x in links if x["mw"] > 0)),
            "exports_mw": round(-sum(x["mw"] for x in links if x["mw"] < 0))}


def parse_indo(obj) -> dict | None:
    """/demand/outturn -> {t, mw, transmission_mw, series: [[t, mw]], peak: {t, mw}} (half-hourly INDO)."""
    rows = obj.get("data") if isinstance(obj, dict) else obj
    pts = {}
    tsd = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t = iso_ts(r.get("startTime"))
        mw = _num(r.get("initialDemandOutturn"))
        if t is None or mw is None:
            continue
        pts[t] = mw
        tsd[t] = _num(r.get("initialTransmissionSystemDemandOutturn"))
    if not pts:
        return None
    series = sorted(pts.items())
    t, mw = series[-1]
    day = utc_date(t)
    today = [(k, v) for k, v in series if utc_date(k) == day]
    peak = max(today, key=lambda kv: kv[1]) if today else (t, mw)
    return {"t": t, "mw": round(mw), "transmission_mw": _r(tsd.get(t), 0),
            "series": [[k, round(v)] for k, v in series[-96:]], "peak": {"t": peak[0], "mw": round(peak[1])}}


def parse_mid(obj) -> dict | None:
    """/balancing/pricing/market-index -> the latest half hour with traded volume (APX provider)."""
    rows = obj.get("data") if isinstance(obj, dict) else obj
    best = None
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        price, vol = _num(r.get("price")), _num(r.get("volume"))
        t = iso_ts(r.get("startTime"))
        if t is None or price is None or not vol or vol <= 0:
            continue
        if best is None or t > best["t"]:
            best = {"t": t, "gbp_mwh": round(price, 2), "volume_mwh": round(vol, 1),
                    "provider": str(r.get("dataProvider") or "")[:20]}
    return best


# ---------------------------------------------------------------------------------------------
# Energy-Charts
# ---------------------------------------------------------------------------------------------
def _series(obj, key: str) -> list[tuple[int, float]]:
    ts = obj.get("unix_seconds") if isinstance(obj, dict) else None
    vals = obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(ts, list) or not isinstance(vals, list):
        return []
    out = []
    for t, v in zip(ts, vals):
        x = _num(v)
        if isinstance(t, (int, float)) and x is not None:
            out.append((int(t), x))
    return out


def value_at(points: list, now: float, max_age: float = 3600.0):
    """(t, v) of the last point at or before ``now`` (None when the newest is older than max_age)."""
    best = None
    for t, v in points:
        if t <= now and (best is None or t > best[0]):
            best = (t, v)
    if best is None or now - best[0] > max_age:
        return None
    return best


SPIKE_RATIO = 3.0      # a share 3 times above (or below a third of) its neighbours' median ...
SPIKE_MIN_PP = 20.0    # ... and at least 20 percentage points away from it is a glitch, not the grid


def drop_spikes(pts: list, window: int = 2) -> list:
    """Leave out one- or two-step glitches (Italy's /signal jumps from about 21% to 82% and back
    within 15 minutes). Each point is compared with the median of up to ``window`` original points
    on each side, so a real ramp, which its neighbours follow, is kept."""
    out = []
    for i, (t, v) in enumerate(pts):
        near = sorted(x for _, x in pts[max(0, i - window):i] + pts[i + 1:i + 1 + window])
        if len(near) >= 2:
            mid = len(near) // 2
            med = near[mid] if len(near) % 2 else (near[mid - 1] + near[mid]) / 2
            if abs(v - med) >= SPIKE_MIN_PP and (v > med * SPIKE_RATIO or v * SPIKE_RATIO < med):
                continue
        out.append((t, v))
    return out


def parse_ec_signal(obj) -> dict:
    """/signal -> {series: [[t, share]]} (renewable share of load, the day in 15-minute steps;
    later points are Energy-Charts' forecast). Empty when the country has no data. Single-step
    glitches are left out (``drop_spikes``); ``dropped`` counts them."""
    raw = _series(obj, "share")
    pts = drop_spikes(raw)
    return {"series": [[t, round(v, 1)] for t, v in pts], "empty": not pts, "dropped": len(raw) - len(pts),
            "substitute": bool(obj.get("substitute")) if isinstance(obj, dict) else False}


def parse_ec_price(obj) -> dict:
    """/price -> {series: [[t, eur_mwh]], unit, licence, avg, min, max}."""
    pts = _series(obj, "price")
    unit = str(obj.get("unit") or "EUR / MWh")[:20] if isinstance(obj, dict) else "EUR / MWh"
    lic = str(obj.get("license_info") or "")[:200] if isinstance(obj, dict) else ""
    vals = [v for _, v in pts]
    return {"series": [[t, round(v, 2)] for t, v in pts], "unit": unit, "licence": lic, "empty": not pts,
            "avg": round(sum(vals) / len(vals), 2) if vals else None, "min": min(vals) if vals else None,
            "max": max(vals) if vals else None, "step": (pts[1][0] - pts[0][0]) if len(pts) > 1 else 3600}


def parse_ec_power(obj) -> dict:
    """/public_power -> the latest complete interval: {t, mix: [{cat, label, mw, pct}], load_mw,
    renewable_pct, fossil_pct, low_carbon_pct, intensity_est, renewable_share_load, unknown}."""
    if not isinstance(obj, dict):
        return {"empty": True}
    ts = obj.get("unix_seconds")
    types = obj.get("production_types")
    if not isinstance(ts, list) or not isinstance(types, list) or not ts:
        return {"empty": True}
    gen: dict[str, list] = {}
    extra: dict[str, list] = {}
    unknown = []
    for p in types:
        if not isinstance(p, dict) or not isinstance(p.get("data"), list):
            continue
        name = str(p.get("name") or "").strip()
        if name in EC_NOT_GENERATION:
            extra[name] = p["data"]
        elif name in EC_TYPES:
            gen[name] = p["data"]
        elif name:
            gen[name] = p["data"]
            unknown.append(name)
    if not gen:
        return {"empty": True}
    n = len(ts)
    counts = [sum(1 for vals in gen.values() if i < len(vals) and _num(vals[i]) is not None) for i in range(n)]
    widest = max(counts) if counts else 0
    if widest == 0:
        return {"empty": True}
    idx = max(i for i in range(n) if counts[i] >= max(1, widest - 1))
    by_cat: dict[str, float] = {}
    for name, vals in gen.items():
        v = _num(vals[idx]) if idx < len(vals) else None
        if v is None or v <= 0:
            continue
        cat = EC_TYPES.get(name, "other")
        by_cat[cat] = by_cat.get(cat, 0.0) + v
    total = sum(by_cat.values())
    if total <= 0:
        return {"empty": True}
    mix = [{"cat": c, "label": CATEGORY_LABELS.get(c, c), "mw": round(mw), "pct": round(mw / total * 100, 1)}
           for c, mw in by_cat.items()]
    mix.sort(key=lambda g: -g["mw"])
    ren = sum(mw for c, mw in by_cat.items() if c in RENEWABLE_CATS)
    fos = sum(mw for c, mw in by_cat.items() if c in FOSSIL_CATS)
    co2 = sum(mw * EMISSION_FACTORS.get(c, 300) for c, mw in by_cat.items())

    def at(name):
        vals = extra.get(name) or []
        return _num(vals[idx]) if idx < len(vals) else None

    load = at("Load")
    share_load = at("Renewable share of load")
    trade = at("Cross border electricity trading")
    return {"empty": False, "t": int(ts[idx]), "mix": mix, "generation_mw": round(total),
            "load_mw": _r(load, 0), "renewable_pct": round(ren / total * 100, 1),
            "fossil_pct": round(fos / total * 100, 1),
            "low_carbon_pct": round((ren + by_cat.get("nuclear", 0.0)) / total * 100, 1),
            "intensity_est": round(co2 / total), "renewable_share_load": _r(share_load, 1),
            "net_import_mw": _r(trade, 0), "unknown": sorted(set(unknown))[:10]}
