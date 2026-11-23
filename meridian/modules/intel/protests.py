"""Protests: GDELT 2.0 rows with CAMEO root 14 (dissent, demonstrations, hunger strikes, strikes and
boycotts, blockades, riots) from the 15-minute exports the module already downloads (no extra traffic).

Rows must be geocoded to a city or landmark (province: kept, flagged as approximate) and reported within
2 days of the event. GDELT gives these rows QuadClass 3 (verbal conflict), so only the root code is
checked. Deduplicated like conflict events (root + place to 0.1 degree + event day); the map and the
events need 2 or more sources or 3 or more articles after merging. Machine-coded, not verified.
"""

from __future__ import annotations

import math
import re

from . import codes, gdelt

ROOT = "14"

PROTEST_EVENT = {
    "140": "Political dissent", "141": "Demonstration", "142": "Hunger strike", "143": "Strike or boycott",
    "144": "Blockade or obstruction", "145": "Riot or violent protest",
}
PROTEST_AIM = {"1": "for leadership change", "2": "for policy change", "3": "for rights",
               "4": "for change of institutions or regime"}
CATEGORY = {"140": "demonstration", "141": "demonstration", "142": "strike", "143": "strike", "144": "blockade",
            "145": "riot"}
CATEGORY_LABEL = {"demonstration": "Demonstration or dissent", "strike": "Strike, boycott or hunger strike",
                  "blockade": "Blockade or obstruction", "riot": "Riot or violent protest"}
_TYPE_WEIGHT = {"140": 0.5, "141": 1.0, "142": 1.0, "143": 1.2, "144": 1.5, "145": 2.5}


def label(code: str | None) -> str:
    """'1452' -> 'Riot or violent protest for policy change'; unknown -> 'Protest'."""
    c = (code or "").strip()
    base = PROTEST_EVENT.get(c[:3])
    if not base:
        return "Protest"
    aim = PROTEST_AIM.get(c[3:4]) if len(c) == 4 else None
    return f"{base} {aim}" if aim else base


def category_of(code: str | None) -> str:
    return CATEGORY.get((code or "")[:3], "demonstration")


def _log_share(value: float, cap: float) -> float:
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(1.0 + value) / math.log10(1.0 + cap))


def severity(code: str, goldstein: float, mentions: int, sources: int, tone: float, reports: int = 1) -> float:
    """Severity 0-10 for a (deduplicated) protest. See docs/modules/intel.md."""
    s = 0.2 * min(10.0, max(0.0, -goldstein))                  # up to 2 (protest codes sit at about -6.5)
    s += _TYPE_WEIGHT.get((code or "")[:3], 0.5)                 # riots weigh most
    s += 2.5 * _log_share(mentions, 200)
    s += 1.5 * _log_share(sources, 30)
    s += min(0.5, max(0.0, -tone) / 20.0)
    s += 0.5 * _log_share(max(0, reports - 1), 20)
    return round(max(0.0, min(10.0, s)), 1)


def severity_of(ev: dict) -> float:
    """Rescore a merged protest (used by db.upsert_events)."""
    return severity(ev["code"], ev["goldstein"], ev["mentions"], ev["sources"], ev["tone"], ev.get("reports", 1))


def dedupe_key(lat: float, lon: float, day: int) -> str:
    return gdelt.dedupe_key(ROOT, lat, lon, day)


def protest_id(dkey: str) -> str:
    return "gp" + gdelt.event_id(dkey)[2:]


def is_protest(row: list[str]) -> bool:
    return row[gdelt.C_ROOT].strip() == ROOT


def protest_row(row: list[str], *, iso_of=None) -> dict | None:
    """A kept protest report as a dict (the same fields as a conflict report), or None."""
    if not is_protest(row):
        return None
    precision = {"3": "city", "4": "city", "5": "province"}.get(row[gdelt.C_GEO_TYPE].strip())
    if precision is None:
        return None
    ll = gdelt._latlon(row)
    if ll is None:
        return None
    added = row[gdelt.C_ADDED].strip()
    if not re.fullmatch(r"\d{14}", added):
        return None
    day = gdelt._i(row[gdelt.C_DAY])
    d_event, d_added = gdelt.day_to_date(day), gdelt.day_to_date(int(added[:8]))
    if d_event is None or d_added is None or (d_added - d_event).days > gdelt.MAX_LAG_DAYS or d_event > d_added:
        return None
    lat, lon = ll
    code = row[gdelt.C_CODE].strip()
    a1, a2 = gdelt._actor(row, 1), gdelt._actor(row, 2)
    fips = row[gdelt.C_GEO_COUNTRY].strip().upper()
    iso2 = codes.fips_to_iso2(fips)
    if iso_of is not None:
        try:
            iso2 = iso_of(lat, lon, fips, iso2) or iso2
        except Exception:
            pass
    url = row[gdelt.C_URL].strip()
    if not url.lower().startswith(("http://", "https://")) or len(url) > 1000:
        url = ""
    mentions = max(0, gdelt._i(row[gdelt.C_MENTIONS]))
    sources = max(0, gdelt._i(row[gdelt.C_SOURCES]))
    tone = gdelt._f(row[gdelt.C_TONE])
    goldstein = gdelt._f(row[gdelt.C_GOLDSTEIN])
    ev = {
        "gid": row[gdelt.C_ID].strip(), "day": day, "t": gdelt.ts_to_epoch(added), "root": ROOT, "code": code,
        "quad": gdelt._i(row[gdelt.C_QUAD], 3), "goldstein": goldstein, "category": category_of(code),
        "armed": False, "crime": False, "precision": precision, "lat": round(lat, 5), "lon": round(lon, 5),
        "place": gdelt.clean_place(row[gdelt.C_GEO_NAME]), "fips": fips, "iso2": iso2,
        "adm1": row[gdelt.C_GEO_ADM1].strip(),
        "actor1": codes.actor_label(a1["name"], a1["type"]), "actor2": codes.actor_label(a2["name"], a2["type"]),
        "mentions": mentions, "sources": sources, "articles": max(0, gdelt._i(row[gdelt.C_ARTICLES])), "tone": tone,
        "url": url, "focus": 1.0,
    }
    ev["dkey"] = dedupe_key(lat, lon, day)
    ev["id"] = protest_id(ev["dkey"])
    ev["severity"] = severity(code, goldstein, mentions, sources, tone)
    return ev


def title(ev: dict) -> str:
    short = gdelt.short_place(ev.get("place") or "")
    lab = label(ev.get("code"))
    return f"{lab} in {short}" if short else lab
