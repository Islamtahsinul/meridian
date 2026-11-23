"""Countries: Natural Earth admin-0 shapes, World Bank indicators and the instability index."""

from __future__ import annotations

import json
import math
import time

from . import geomath

NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"
NE_RAW = "ne_50m_admin_0_countries.geojson"
NE_SITE = "https://www.naturalearthdata.com/"
WB_API = "https://api.worldbank.org/v2"
WB_SITE = "https://data.worldbank.org/"

# Natural Earth units without an ISO code, assigned to the country that the ISO standard lists.
NAME_TO_ISO2 = {"Somaliland": "SO", "N. Cyprus": "CY"}
REGION_NOTES = {
    "Somaliland": "Somaliland region (self-declared state, internationally recognised as part of Somalia)",
    "N. Cyprus": "Northern Cyprus (recognised only by Turkey; internationally part of Cyprus)",
    "Siachen Glacier": "Siachen Glacier (disputed between India and Pakistan)",
}
# Crimea: Natural Earth draws it inside Russia. Lookups inside this outline get a neutral note.
CRIMEA = [[32.45, 45.35], [33.35, 46.15], [33.62, 46.22], [34.45, 45.95], [35.05, 45.65], [35.95, 45.62],
          [36.65, 45.45], [36.55, 45.15], [35.6, 44.95], [34.4, 44.35], [33.4, 44.35], [33.25, 44.6],
          [32.5, 45.15], [32.45, 45.35]]
CRIMEA_NOTE = ("Crimea: controlled by Russia since 2014 and internationally recognised as part of Ukraine "
               "(UN General Assembly resolution 68/262). Natural Earth draws it inside Russia.")

# World Bank capital names that differ from the current official English spelling.
CAPITAL_NAMES = {"Kiev": "Kyiv"}

INDICATORS = [
    # key, World Bank code, source id (2 = WDI, 3 = WGI), label
    ("pop", "SP.POP.TOTL", 2, "Population"),
    ("gdp", "NY.GDP.MKTP.CD", 2, "GDP (current US$)"),
    ("gdp_pc", "NY.GDP.PCAP.CD", 2, "GDP per capita (current US$)"),
    ("inflation", "FP.CPI.TOTL.ZG", 2, "Inflation, consumer prices (annual %)"),
    ("unemployment", "SL.UEM.TOTL.ZS", 2, "Unemployment (% of labour force, ILO estimate)"),
    ("military", "MS.MIL.XPND.GD.ZS", 2, "Military expenditure (% of GDP)"),
    ("stability", "GOV_WGI_PV.EST", 3, "Political stability and absence of violence (WGI estimate, -2.5 to 2.5)"),
]

BANDS = [(20, "Low"), (40, "Moderate"), (60, "Elevated"), (80, "High"), (101, "Very high")]
WEIGHTS = {"governance": 0.35, "conflict": 0.40, "tone": 0.15, "hazards": 0.10}
HAZARD_KINDS = {"earthquake", "fire", "volcano", "storm", "flood", "warning", "outbreak", "disaster", "tsunami"}


def _valid_code(v) -> str | None:
    if isinstance(v, str) and len(v) in (2, 3) and v.isalpha() and v != "-99":
        return v.upper()
    return None


def iso2_of(props: dict) -> str | None:
    return (_valid_code(props.get("ISO_A2_EH")) or _valid_code(props.get("ISO_A2"))
            or NAME_TO_ISO2.get(props.get("NAME") or ""))


def display_name(props: dict) -> str:
    name = props.get("NAME_EN") or props.get("NAME") or props.get("ADMIN") or ""
    return str(name).strip()


# ---------------------------------------------------------------------------------------------
# Natural Earth dataset
# ---------------------------------------------------------------------------------------------
def build_countries_collection(ne: dict, tolerance: float = 0.004) -> tuple[dict, int]:
    feats = []
    for f in ne.get("features") or []:
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        geom = geomath.simplify_geometry(g, tolerance, digits=3) if g else None
        if geom is None and g:
            geom = geomath.simplify_geometry(g, 0.0, digits=4)   # tiny islands: keep them unsimplified
        if geom is None:
            continue
        iso2 = iso2_of(p)
        name = display_name(p)
        lx, ly = p.get("LABEL_X"), p.get("LABEL_Y")
        label = [round(float(lx), 4), round(float(ly), 4)] if isinstance(lx, (int, float)) and isinstance(ly, (int, float)) else None
        props = {
            "iso2": iso2 or "", "name": name, "short": str(p.get("NAME") or name), "iso3": _valid_code(p.get("ISO_A3_EH")) or "",
            "continent": p.get("CONTINENT") or "", "subregion": p.get("SUBREGION") or "",
            "pop_est": p.get("POP_EST"), "pop_year": p.get("POP_YEAR"), "label": label,
            "note": REGION_NOTES.get(p.get("NAME") or "") or "",
        }
        feats.append({"type": "Feature", "properties": props, "geometry": geom})
    return {"type": "FeatureCollection", "features": feats}, len(feats)


# ---------------------------------------------------------------------------------------------
# World Bank dataset
# ---------------------------------------------------------------------------------------------
def parse_wb_economies(payload) -> dict[str, dict]:
    """iso3 -> economy (aggregates removed) from /v2/country?format=json."""
    out = {}
    rows = payload[1] if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list) else []
    for c in rows:
        region = ((c.get("region") or {}).get("value") or "").strip()
        if region == "Aggregates" or not c.get("iso2Code") or not c.get("id"):
            continue

        def num(v):
            try:
                f = float(v)
                return f if math.isfinite(f) else None
            except (TypeError, ValueError):
                return None

        out[c["id"].upper()] = {"iso2": c["iso2Code"].upper(), "iso3": c["id"].upper(), "name": (c.get("name") or "").strip(),
                                "capital": CAPITAL_NAMES.get((c.get("capitalCity") or "").strip(), (c.get("capitalCity") or "").strip()),
                                "lat": num(c.get("latitude")),
                                "lon": num(c.get("longitude")), "region": region,
                                "income": ((c.get("incomeLevel") or {}).get("value") or "").strip()}
    return out


def parse_wb_indicator(payload) -> dict[str, tuple[float, int]]:
    """key (iso3, or the country id when iso3 is blank) -> (latest non-null value, year)."""
    rows = payload[1] if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list) else []
    out: dict[str, tuple[float, int]] = {}
    for r in rows:
        v = r.get("value")
        if v is None:
            continue
        try:
            v = float(v)
            year = int(r.get("date"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        key = (r.get("countryiso3code") or "").upper() or ((r.get("country") or {}).get("id") or "").upper()
        if not key:
            continue
        if key not in out or year > out[key][1]:
            out[key] = (v, year)
    return out


def build_worldbank(net, log=None) -> dict:
    econ = parse_wb_economies(net.get_json(f"{WB_API}/country", params={"format": "json", "per_page": 400},
                                           timeout=60, retries=2))
    if len(econ) < 150:
        raise RuntimeError(f"World Bank country list looks incomplete ({len(econ)} economies)")
    by_iso2 = {e["iso2"]: dict(e, ind={}) for e in econ.values()}
    iso3_to_iso2 = {k: v["iso2"] for k, v in econ.items()}
    failures = []
    for key, code, source, _label in INDICATORS:
        params = {"format": "json", "mrv": 5, "per_page": 20000}
        if source != 2:
            params["source"] = source
        try:
            data = parse_wb_indicator(net.get_json(f"{WB_API}/country/all/indicator/{code}", params=params,
                                                   timeout=90, retries=2))
        except Exception as err:  # one missing indicator does not sink the dataset
            failures.append(f"{code}: {err}")
            if log:
                log.warning("World Bank indicator %s failed: %s", code, err)
            continue
        for k3, (value, year) in data.items():
            iso2 = iso3_to_iso2.get(k3) or (k3 if len(k3) == 2 else None)
            if iso2 and iso2 in by_iso2:
                by_iso2[iso2]["ind"][key] = [value, year]
    if len(failures) >= 4:
        raise RuntimeError("World Bank indicators unavailable: " + "; ".join(failures[:3]))
    return {"built_at": time.time(), "economies": by_iso2, "failures": failures,
            "indicators": {k: {"code": c, "label": lab} for k, c, _s, lab in INDICATORS}}


# ---------------------------------------------------------------------------------------------
# Instability index (0-100)
# ---------------------------------------------------------------------------------------------
def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _log_share(value: float, cap: float) -> float:
    if not value or value <= 0:
        return 0.0
    return _clamp01(math.log10(1.0 + value) / math.log10(1.0 + cap))


def band(index: float | None) -> str:
    if index is None:
        return "No data"
    for top, name in BANDS:
        if index < top:
            return name
    return BANDS[-1][1]


def governance_score(pv: float | None) -> float | None:
    """WGI political stability estimate (about -2.5 to +2.5) -> 0 (stable) .. 100 (unstable)."""
    if pv is None:
        return None
    return round(100.0 * _clamp01((1.5 - pv) / 4.0), 1)


def conflict_score(sev_sum: float, rows: int, conflict_rows: int, coverage_days: float,
                   population: float | None) -> tuple[float | None, dict]:
    """Severity-weighted conflict events over 30 days, their share of the country's coverage and
    the rate per million people. Returns (score, detail)."""
    if coverage_days < 0.25:
        return None, {"reason": "not enough GDELT history yet"}
    scale = 30.0 / min(30.0, coverage_days)
    weighted = (sev_sum or 0.0) / 10.0 * scale
    parts = {"absolute": (0.3, _log_share(weighted, 1500))}
    per_m = None
    if population and population > 0:
        per_m = weighted / (population / 1e6)
        parts["per_capita"] = (0.2, _log_share(per_m, 20))
    share = None
    if rows and rows >= 200:
        share = conflict_rows / rows
        parts["share"] = (0.5, _clamp01(share / 0.20))
    wsum = sum(w for w, _ in parts.values())
    score = 100.0 * sum(w * v for w, v in parts.values()) / wsum
    return round(score, 1), {"weighted_events_30d": round(weighted, 1), "per_million_30d": round(per_m, 2) if per_m is not None else None,
                             "coverage_share": round(share, 4) if share is not None else None,
                             "scaled_from_days": round(min(30.0, coverage_days), 2)}


def tone_score(tone: float | None, tone_rows: int) -> float | None:
    if tone is None or tone_rows < 30:
        return None
    return round(100.0 * _clamp01((-tone - 1.0) / 5.0), 1)


def hazard_score(weighted: float | None) -> float | None:
    if weighted is None:
        return None
    return round(100.0 * _clamp01(weighted / 4.0), 1)


def instability(*, pv: float | None, pv_year: int | None, sev_sum: float, rows: int, conflict_rows: int,
                events: int, coverage_days: float, population: float | None, tone: float | None, tone_rows: int,
                hazards_weighted: float | None, hazards_count: int = 0) -> dict:
    g = governance_score(pv)
    c, c_detail = conflict_score(sev_sum, rows, conflict_rows, coverage_days, population)
    t = tone_score(tone, tone_rows)
    h = hazard_score(hazards_weighted)
    parts = [
        {"key": "governance", "label": "Political stability (World Bank WGI)", "score": g, "weight": WEIGHTS["governance"],
         "detail": (f"Estimate {pv:+.2f} ({pv_year})" if pv is not None else "No WGI estimate")},
        {"key": "conflict", "label": "Conflict events, 30 days (GDELT)", "score": c, "weight": WEIGHTS["conflict"],
         "detail": (f"{events:,} events, {c_detail['weighted_events_30d']:,} severity-weighted"
                    + (f", {c_detail['coverage_share'] * 100:.1f}% of coverage" if c_detail.get("coverage_share") is not None else "")
                    if c is not None else c_detail.get("reason", "No data")),
         "inputs": c_detail},
        {"key": "tone", "label": "News tone, 7 days (GDELT)", "score": t, "weight": WEIGHTS["tone"],
         "detail": (f"Average tone {tone:.2f} over {tone_rows:,} reports" if t is not None else "Too few reports")},
        {"key": "hazards", "label": "Active hazards, 7 days (other layers)", "score": h, "weight": WEIGHTS["hazards"],
         "detail": (f"{hazards_count} hazard events" if h is not None else "No hazard feed loaded")},
    ]
    avail = [p for p in parts if p["score"] is not None]
    if not any(p["key"] in ("governance", "conflict") for p in avail):
        return {"index": None, "band": band(None), "parts": parts}
    wsum = sum(p["weight"] for p in avail)
    index = int(round(sum(p["weight"] * p["score"] for p in avail) / wsum))
    return {"index": index, "band": band(index), "parts": parts,
            "missing": [p["key"] for p in parts if p["score"] is None]}


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))
