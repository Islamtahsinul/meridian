"""Build a briefing document from history events and a few live snapshots. Pure: no I/O.

The document (CONTRACT.md section 15.4, extended):

    {id, time, period: {from, to, hours, basis, label, previous: {from, to, label}}, trigger, local_date,
     sections: [{id, region, group, summary, items: [item], trends: [trend], note}], text, ai,
     stats, coverage, metrics, sources}

An item is ``{kind, module, title, line, time, severity, source, entity, unverified?, new?, count?}``;
a trend is ``{metric, title, line, value, previous, change, notable, severity, entity?, source}``.
"""

from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from typing import Any, Callable

from . import trends as T
from .regions import REGIONS

SECTION_MAX = 8
HEADLINE_MAX = 5
HEADLINE_MIN = 3
HEADLINE_UNVERIFIED_MAX = 2
UNVERIFIED_PENALTY = 1.5      # machine-coded news ranks below verified data of the same severity
SIGNAL_PENALTY = {"outage": 1.5}   # automated measurements (IODA outage scores) rank the same way
MIN_ITEM_SEVERITY = 3.0
TEXT_MAX = 220

GLOBAL_SECTIONS = [("space", "Space weather"), ("cyber", "Cyber and internet"), ("markets", "Markets and energy")]
GLOBAL_KIND = {"space_weather": "space", "cyber": "cyber", "outage": "cyber"}
SUMMARISED_KINDS = {"gps_jam"}            # flights GPS cells become one trend row per region
ONGOING_KINDS = {"storm", "volcano", "outbreak", "disaster", "flood", "warning", "fire", "ship", "gps_jam", "strait",
                 "tfr", "airport_delay", "airspace_advisory", "aircraft"}
PER_KIND_CAP = {"conflict_event": 3, "news": 2, "warning": 2, "fire": 2, "flood": 2, "airport_delay": 2, "tfr": 1,
                "convergence": 1, "launch": 1, "airspace_advisory": 1}
UNVERIFIED = {("intel", "conflict_event"), ("intel", "news")}
SOURCES = {
    ("intel", "conflict_event"): "GDELT, machine-coded from news",
    ("intel", "news"): "GDELT news, machine-coded",
    ("intel", "convergence"): "Meridian convergences (derived)",
    ("flights", "aircraft"): "ADS-B (adsb.lol, adsb.fi)",
    ("flights", "gps_jam"): "ADS-B navigation accuracy (derived)",
    ("flights", "airport_delay"): "FAA NAS Status",
    ("flights", "tfr"): "FAA",
    ("flights", "flight_restriction"): "FAA",
    ("flights", "airspace_advisory"): "EASA conflict zone bulletins",
    ("ships", "ship"): "AIS",
    ("ships", "strait"): "AIS (derived)",
    ("ships", "gps_jam"): "AIS (derived)",
    ("space", "launch"): "Launch Library 2",
    ("space", "space_weather"): "NOAA SWPC",
}
UNVERIFIED_NOTE = ("Conflict and news items are machine-coded from news reports by GDELT: unverified, and one "
                   "widely repeated story can inflate a count.")
MARKET_THRESHOLDS = {"index": 1.5, "fx": 0.8, "crypto": 5.0, "commodity": 2.5}
YESTERDAY = (12 * 3600.0, 36 * 3600.0)    # a previous briefing this old counts as "yesterday's"


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def severity_word(s: float | None) -> str:
    s = float(s or 0)
    return "extreme" if s >= 9 else "serious" if s >= 6 else "notable" if s >= 3 else "routine"


def utc(t: float | None, date: bool = True) -> str:
    if t is None:
        return ""
    fmt = "%d %b %H:%M UTC" if date else "%H:%M UTC"
    return time.strftime(fmt, time.gmtime(float(t))).lstrip("0")


def period_text(since: float, until: float) -> str:
    a, b = time.gmtime(since), time.gmtime(until)
    if (a.tm_year, a.tm_yday) == (b.tm_year, b.tm_yday):
        return f"{utc(since)[:-4]} to {utc(until, date=False)}"
    return f"{utc(since)[:-4]} to {utc(until)}"


def clean(text: Any, limit: int = TEXT_MAX) -> str:
    if text is None:
        return ""
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _round(v: float | None, d: int = 1) -> float | None:
    return None if v is None else round(v, d)


def prev_label(hours: float) -> str:
    if 20 <= hours <= 28:
        return "the previous day"
    return f"the previous {hours:.0f} hours" if hours >= 2 else "the previous hour"


def entity_of(ev: dict) -> dict | None:
    """A CONTRACT 5.6 entity from a history event (what the browser opens with ctx.entities.open)."""
    if not ev.get("kind") or ev.get("id") in (None, ""):
        return None
    ent = {"kind": ev["kind"], "id": str(ev["id"]), "title": clean(ev.get("title"), 160),
           "subtitle": clean(ev.get("subtitle"), 160)}
    lat, lon = _num(ev.get("lat")), _num(ev.get("lon"))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        ent["lat"], ent["lon"] = round(lat, 5), round(lon, 5)
    if ev.get("layer"):
        ent["layer"] = str(ev["layer"])[:80]
    zoom = _num(ev.get("zoom"))
    if zoom is not None and 0 <= zoom <= 20:
        ent["zoom"] = zoom
    bb = ev.get("bbox")
    if isinstance(bb, list) and len(bb) == 4 and all(_num(x) is not None for x in bb):
        ent["bbox"] = [round(float(x), 5) for x in bb]
    return ent


# Short source ids some modules put on their events, with the name a reader should see.
SOURCE_NAMES = {
    ("intel", "fcdo"): "FCDO travel advice",
    ("intel", "us"): "US State Department travel advisory",
}
# CONTRACT 5.7: the sanctions index merges several lists (OFAC, UK, UN, EU); each item's subtitle names its own.
SANCTIONS_SOURCE = "AIS with sanctions lists"


def source_of(ev: dict) -> str:
    src = ev.get("source")
    module, kind = ev.get("module"), ev.get("kind")
    if isinstance(src, str) and src.strip():
        return SOURCE_NAMES.get((module, src.strip().lower())) or clean(src, 60)
    if module == "ships" and ev.get("category") == "sanctioned_vessel":
        return SANCTIONS_SOURCE
    return SOURCES.get((module, kind)) or (f"Meridian {module} module" if module else "Meridian")


def event_item(ev: dict, *, new: bool = False, count: int = 1) -> dict:
    kind = str(ev.get("kind") or "")
    item = {"kind": kind, "module": ev.get("module"), "title": clean(ev.get("title"), 160) or kind.replace("_", " "),
            "line": clean(ev.get("subtitle")), "time": _num(ev.get("time")), "severity": _num(ev.get("severity")) or 0.0,
            "source": source_of(ev), "entity": entity_of(ev)}
    if kind == "earthquake" and ev.get("tsunami") is True:
        item["line"] = clean(f"{item['line']}; USGS tsunami flag set (check official warnings)")
    if (ev.get("module"), kind) in UNVERIFIED:
        item["unverified"] = True
    if new:
        item["new"] = True
    if count > 1:
        item["count"] = count
        noun = "reports" if kind in ("news", "conflict_event") else "alerts" if kind == "warning" else "events"
        item["line"] = clean(f"{item['line']} ({count} similar {noun})")
    return item


def _dedupe_key(ev: dict) -> tuple:
    kind = ev.get("kind")
    if kind == "news":
        return ("news", _norm(ev.get("title"))[:90])
    if kind == "conflict_event":
        return ("conflict_event", _norm(ev.get("title")), round(float(ev["lat"]), 1), round(float(ev["lon"]), 1))
    if kind == "warning":
        return ("warning", _norm(ev.get("title")), _norm(ev.get("subtitle"))[:40])
    return (ev.get("module"), kind, str(ev.get("id")))


def dedupe(evs: list[dict]) -> list[tuple[dict, int]]:
    groups: dict[tuple, list] = {}
    for ev in evs:
        key = _dedupe_key(ev)
        g = groups.get(key)
        if g is None:
            groups[key] = [ev, 1]
            continue
        g[1] += 1
        if (ev.get("severity") or 0, ev.get("time") or 0) > (g[0].get("severity") or 0, g[0].get("time") or 0):
            g[0] = ev
    return [(ev, n) for ev, n in groups.values()]


def select(items: list[dict], max_items: int = SECTION_MAX) -> list[dict]:
    """The most severe items with a cap per kind, so one feed cannot fill a section."""
    items = sorted(items, key=lambda it: (-(it.get("severity") or 0), -int(bool(it.get("new"))), -(it.get("time") or 0)))
    out, per = [], defaultdict(int)
    for it in items:
        cap = PER_KIND_CAP.get(it["kind"], 3)
        if per[it["kind"]] >= cap:
            continue
        out.append(it)
        per[it["kind"]] += 1
        if len(out) >= max_items:
            break
    return out


def _trend(metric: str, title: str, value: int, previous: int | None, *, unit: str | tuple[str, str], covered: bool,
           plabel: str, source: str, entity: dict | None = None, extra: str = "", scale: float = 1.0,
           base_sev: float = 5.0, unverified: bool = False, no_compare_note: str = "") -> dict:
    """A count with its comparison. ``covered`` False: the history does not reach back far enough.
    ``unit`` is one word for both numbers or (singular, plural)."""
    if isinstance(unit, tuple):
        unit = unit[0] if value == 1 else unit[1]
    row = {"metric": metric, "title": title, "value": value, "previous": previous if covered else None,
           "change": None, "notable": False, "severity": 2.0, "source": source}
    if covered and previous is not None:
        ch = T.compare(value, previous, scale=scale)
        row["change"] = ch
        row["notable"] = ch["notable"]
        row["line"] = f"{value:,} {unit}, {T.words(ch, prev_label=plabel)}"
        if ch["notable"]:
            ratio = (value + 0.5) / (ch["expected"] + 0.5)
            row["severity"] = round(min(base_sev + 2.0, base_sev + abs(math.log2(max(ratio, 1e-6))) / 2), 1)
    else:
        row["line"] = f"{value:,} {unit}" + (f"; {no_compare_note}" if no_compare_note else "")
    if extra:
        row["line"] += f". {extra}"
    row["line"] = clean(row["line"], 260)
    if entity:
        row["entity"] = entity
    if unverified:
        row["unverified"] = True
    return row


# ---------------------------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------------------------
class Builder:
    def __init__(self, *, since: float, until: float, now: float, place: Callable[[float, float], tuple],
                 country_entity: Callable[[str], dict | None] | None = None, countries_ready: bool = True):
        self.since, self.until, self.now = float(since), float(until), float(now)
        self.hours = max(0.25, (self.until - self.since) / 3600.0)
        self.prev_since = self.since - (self.until - self.since)
        self.plabel = prev_label(self.hours)
        self.place = place
        self.country_entity = country_entity or (lambda iso2: None)
        self.countries_ready = countries_ready

    # -- placement and coverage ----------------------------------------------------------------------
    def _located(self, evs: list[dict]) -> list[dict]:
        out = []
        for ev in evs:
            if not isinstance(ev, dict) or ev.get("lat") is None or ev.get("lon") is None:
                continue
            kind = ev.get("kind")
            if kind in GLOBAL_KIND:
                region, country = GLOBAL_KIND[kind], None
            else:
                region, country = self.place(ev["lat"], ev["lon"])
            e = dict(ev)
            e["_region"] = region
            e["_country"] = country
            out.append(e)
        return out

    def _coverage(self, cur: list[dict], prev: list[dict]) -> dict:
        seen = [e["first_seen"] for e in cur + prev if _num(e.get("first_seen")) is not None]
        history_from = min(seen) if seen else None
        oldest: dict[str, float] = {}
        for e in cur + prev:
            t = _num(e.get("first_time")) or _num(e.get("time"))
            k = e.get("kind")
            if t is not None and (k not in oldest or t < oldest[k]):
                oldest[k] = t
        full = history_from is not None and history_from <= self.prev_since + 3600
        return {"history_from": history_from, "full": full, "oldest": oldest,
                "new_ok": history_from is not None and history_from <= self.since - 3600}

    def covered(self, cov: dict, kind: str) -> bool:
        if cov["full"]:
            return True
        if kind in ONGOING_KINDS:
            return False
        t = cov["oldest"].get(kind)
        return t is not None and t <= self.prev_since + 7200

    # -- the document ------------------------------------------------------------------------------
    def build(self, events: list[dict], prev_events: list[dict], extras: dict | None = None,
              previous: dict | None = None, *, bid: str, trigger: str, basis: str, local_date: str,
              truncated: bool = False) -> dict:
        extras = extras or {}
        cur = self._located(events)
        prev = self._located(prev_events)
        cov = self._coverage(cur, prev)
        yesterday = previous if previous and YESTERDAY[0] <= self.now - float(previous.get("time") or 0) <= YESTERDAY[1] \
            else None
        metrics: dict[str, Any] = {}

        by_region: dict[str, list[dict]] = defaultdict(list)
        prev_by_region: dict[str, list[dict]] = defaultdict(list)
        for e in cur:
            by_region[e["_region"]].append(e)
        for e in prev:
            prev_by_region[e["_region"]].append(e)

        # conflict volume worldwide, for the per-country comparison
        conf_cur = sum(1 for e in cur if e.get("kind") == "conflict_event")
        conf_prev = sum(1 for e in prev if e.get("kind") == "conflict_event")
        scale = T.volume_scale(conf_cur, conf_prev)

        sections: list[dict] = []
        regional: list[dict] = []
        straits = _straits_by_region(extras.get("straits"), self.place)
        y_straits = ((yesterday or {}).get("metrics") or {}).get("straits") or {}
        for rid, name in REGIONS:
            sec = self.region_section(rid, name, by_region.get(rid, []), prev_by_region.get(rid, []), cov, scale,
                                      straits.get(rid, []), y_straits)
            regional.append(sec)
        metrics["straits"] = {s["id"]: {"inside": s.get("inside_now"), "transits_24h": s.get("transits_24h")}
                              for lst in straits.values() for s in lst}
        metrics["conflict_total"] = conf_cur

        glob = [self.space_section(by_region.get("space", []), extras.get("space"), yesterday, metrics),
                self.cyber_section(by_region.get("cyber", []), extras.get("cyber"), yesterday, metrics),
                self.markets_section(extras.get("markets"), extras.get("grid"), yesterday, metrics)]

        areas = self.areas_section(extras.get("alerts"))
        headline = self.headline_section(regional, glob)
        sections.append(headline)
        if areas is not None:
            sections.append(areas)
        sections.extend(regional)
        sections.extend(glob)

        total = len(cur)
        serious = sum(1 for e in cur if (e.get("severity") or 0) >= 6)
        by_kind: dict[str, int] = defaultdict(int)
        for e in cur:
            by_kind[e.get("kind") or "?"] += 1
        notes = []
        if not cov["full"]:
            hf = cov["history_from"]
            notes.append("The event history " + (f"starts at {utc(hf)}" if hf else "is empty") +
                         ", so some comparisons with " + self.plabel + " are left out.")
        if not self.countries_ready:
            notes.append("Country shapes are not built yet (intel module), so regions are approximate and "
                         "per-country counts are left out.")
        if truncated:
            notes.append("The period had more events than one briefing reads; the oldest were left out.")
        for key, err in sorted((extras.get("errors") or {}).items()):
            notes.append(f"{_EXTRA_NAMES.get(key, key)}: {err}")
        doc = {
            "id": bid, "time": self.now, "trigger": trigger, "local_date": local_date,
            "period": {"from": self.since, "to": self.until, "hours": round(self.hours, 2), "basis": basis,
                       "label": _basis_label(basis, self.hours),
                       "previous": {"from": self.prev_since, "to": self.since, "label": self.plabel}},
            "sections": sections,
            "stats": {"events": total, "serious": serious, "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
                      "items": sum(len(s["items"]) + len(s.get("trends") or []) for s in sections if s["id"] != "headline"),
                      "truncated": truncated},
            "coverage": {"history_from": cov["history_from"], "full": cov["full"], "notes": notes},
            "metrics": metrics,
            "ai": None,
            "sources": _sources(cur, extras),
        }
        doc["text"] = deterministic_text(doc)
        return doc

    # -- regional sections ------------------------------------------------------------------------
    def region_section(self, rid: str, name: str, evs: list[dict], prev: list[dict], cov: dict, scale: float,
                       straits: list[dict], y_straits: dict) -> dict:
        items = []
        for ev, n in dedupe([e for e in evs if e.get("kind") not in SUMMARISED_KINDS]):
            sev = _num(ev.get("severity")) or 0.0
            if sev < MIN_ITEM_SEVERITY:
                continue
            new = (cov["new_ok"] and ev.get("kind") in ONGOING_KINDS
                   and (_num(ev.get("first_seen")) or 0) >= self.since)
            items.append(event_item(ev, new=new, count=n))
        chosen = select(items)
        trends = self.region_trends(rid, evs, prev, cov, scale, straits, y_straits)
        serious = sum(1 for e in evs if (e.get("severity") or 0) >= 6)
        summary = f"{len(evs):,} event{'s' if len(evs) != 1 else ''}"
        if serious:
            summary += f", {serious:,} serious or extreme"
        if chosen:
            summary += f". Most severe: {chosen[0]['title']}"
        conflict = [t for t in trends if t["metric"] == "conflict_reports"]
        if conflict:
            summary += ". Conflict reports: " + ", ".join(
                f"{t['title']} {t['value']:,}" + (f" ({_short_change(t['change'])})" if t.get("change") else "")
                for t in conflict[:3])
        sec = {"id": rid, "region": name, "group": "regions", "summary": clean(summary + ".", 400),
               "items": chosen, "trends": trends, "counts": {"events": len(evs), "serious": serious}}
        if not evs and not trends:
            sec["note"] = "Nothing notable recorded here in this period."
        return sec

    def region_trends(self, rid: str, evs: list[dict], prev: list[dict], cov: dict, scale: float,
                      straits: list[dict], y_straits: dict) -> list[dict]:
        out: list[dict] = []
        # conflict reports by country (GDELT)
        if self.countries_ready:
            cur_c, prev_c = _count_by_country(evs, "conflict_event"), _count_by_country(prev, "conflict_event")
            ok = self.covered(cov, "conflict_event")
            if ok:
                ranked = T.rank_countries({k: v[0] for k, v in cur_c.items()}, {k: v[0] for k, v in prev_c.items()},
                                          top=3, extra=2, scale=scale)
            else:
                ranked = [(k, None) for k, _ in sorted(cur_c.items(), key=lambda kv: -kv[1][0])[:3]]
            for iso2, _ch in ranked:
                value = cur_c.get(iso2, (0, ""))[0]
                cname = cur_c.get(iso2, prev_c.get(iso2, (0, iso2)))[1] or iso2
                out.append(_trend("conflict_reports", cname, value, prev_c.get(iso2, (0, ""))[0],
                                  unit=("conflict report", "conflict reports"),
                                  covered=ok, plabel=self.plabel, source=SOURCES[("intel", "conflict_event")],
                                  entity=self.country_entity(iso2), scale=scale, base_sev=6.0, unverified=True,
                                  no_compare_note="no comparison yet"))
        # earthquakes of magnitude 5 or more
        q_cur = [e for e in evs if e.get("kind") == "earthquake" and (_num(e.get("magnitude")) or 0) >= 5]
        q_prev = [e for e in prev if e.get("kind") == "earthquake" and (_num(e.get("magnitude")) or 0) >= 5]
        if q_cur or q_prev:
            big = max(q_cur, key=lambda e: _num(e.get("magnitude")) or 0, default=None)
            out.append(_trend("quakes_m5", "Earthquakes of magnitude 5 or more", len(q_cur), len(q_prev),
                              unit="recorded", covered=self.covered(cov, "earthquake"), plabel=self.plabel,
                              source="USGS", entity=entity_of(big) if big else None, base_sev=4.0,
                              extra=f"Largest: {clean(big.get('title'), 80)}" if big else "",
                              no_compare_note="no comparison yet"))
        # tropical storms
        s_cur = [e for e in evs if e.get("kind") == "storm"]
        if s_cur:
            new = [e for e in s_cur if cov["new_ok"] and (_num(e.get("first_seen")) or 0) >= self.since]
            strongest = max(s_cur, key=lambda e: (_num(e.get("severity")) or 0))
            extra = f"{len(new)} new" if new else ""
            row = _trend("storms", "Tropical storms active", len(s_cur), sum(1 for e in prev if e.get("kind") == "storm"),
                         unit="active", covered=self.covered(cov, "storm"), plabel=self.plabel, source="GDACS, NHC",
                         entity=entity_of(strongest), base_sev=4.0, extra=extra)
            out.append(row)
        # ships: dark and sanctioned
        for metric, cat, title, unit in (("dark_ships", "dark_ship", "Dark ships",
                                          ("vessel silent on AIS", "vessels silent on AIS")),
                                         ("sanctioned_ships", "sanctioned_vessel", "Sanctioned vessels seen",
                                          ("vessel on a sanctions list", "vessels on sanctions lists"))):
            c = [e for e in evs if e.get("kind") == "ship" and e.get("category") == cat]
            p = [e for e in prev if e.get("kind") == "ship" and e.get("category") == cat]
            if c or p:
                top = max(c, key=lambda e: (_num(e.get("severity")) or 0, _num(e.get("time")) or 0), default=None)
                out.append(_trend(metric, title, len(c), len(p), unit=unit, covered=self.covered(cov, "ship"),
                                  plabel=self.plabel, source="AIS" if cat == "dark_ship" else SANCTIONS_SOURCE,
                                  entity=entity_of(top) if top else None, base_sev=4.0,
                                  no_compare_note="no comparison yet"))
        # straits with vessels inside now (live snapshot, compared with yesterday's briefing)
        for s in [s for s in straits if (s.get("inside_now") or 0) > 0 or s.get("congestion")][:3]:
            y = y_straits.get(s["id"]) or {}
            transits = s.get("transits_24h")
            line_extra = f"{transits:,} transits in 24 h" if isinstance(transits, int) else ""
            if isinstance(transits, int) and isinstance(y.get("transits_24h"), int):
                ch = T.compare(transits, y["transits_24h"])
                line_extra += f", {T.words(ch, prev_label='in yesterday’s briefing')}"
            ent = {"kind": "strait", "id": f"ships:{s['id']}", "title": s["name"], "layer": "ships.straits"}
            lab = s.get("label")
            if isinstance(lab, list) and len(lab) == 2:
                ent["lon"], ent["lat"] = lab[0], lab[1]
            row = {"metric": "strait", "title": s["name"], "value": s.get("inside_now"), "previous": None,
                   "change": None, "notable": bool(s.get("congestion")), "severity": 5.0 if s.get("congestion") else 2.0,
                   "source": "AIS (derived)", "entity": ent,
                   "line": clean(f"{s.get('inside_now') or 0:,} vessels inside now"
                                 + (f", {s['congestion']}x the usual" if s.get("congestion") else "")
                                 + (f"; {line_extra}" if line_extra else ""), 260)}
            out.append(row)
        # aircraft emergencies
        e_cur = [e for e in evs if e.get("kind") == "aircraft" and e.get("event") == "emergency"]
        e_prev = [e for e in prev if e.get("kind") == "aircraft" and e.get("event") == "emergency"]
        if e_cur or e_prev:
            out.append(_trend("emergencies", "Aircraft emergency squawks", len(e_cur), len(e_prev), unit="aircraft",
                              covered=self.covered(cov, "aircraft"), plabel=self.plabel,
                              source=SOURCES[("flights", "aircraft")], base_sev=4.0,
                              entity=entity_of(max(e_cur, key=lambda e: _num(e.get("time")) or 0)) if e_cur else None,
                              no_compare_note="no comparison yet"))
        # GPS interference cells (flights)
        g_cur = [e for e in evs if e.get("kind") == "gps_jam" and e.get("module") == "flights"]
        g_prev = [e for e in prev if e.get("kind") == "gps_jam" and e.get("module") == "flights"]
        if g_cur or g_prev:
            worst = max(g_cur, key=lambda e: (_num(e.get("severity")) or 0, _num(e.get("time")) or 0), default=None)
            new = [e for e in g_cur if cov["new_ok"] and (_num(e.get("first_seen")) or 0) >= self.since]
            extra = []
            if new:
                extra.append(f"{len(new)} new")
            if worst:
                where = (worst.get("_country") or {}).get("name")
                extra.append(f"worst: {clean(worst.get('title'), 70).replace('GPS interference, ', '')}"
                             + (f" near {where}" if where else ""))
            text = "; ".join(extra)
            out.append(_trend("gps_cells", "GPS interference", len(g_cur), len(g_prev),
                              unit=("affected area (half-degree cell)", "affected areas (half-degree cells)"),
                              covered=self.covered(cov, "gps_jam"), plabel=self.plabel,
                              source=SOURCES[("flights", "gps_jam")], entity=entity_of(worst) if worst else None,
                              base_sev=4.0, extra=text[:1].upper() + text[1:], no_compare_note="no comparison yet"))
        return out

    # -- global sections ----------------------------------------------------------------------------
    def space_section(self, evs: list[dict], now: Any, yesterday: dict | None, metrics: dict) -> dict:
        items = [event_item(ev, count=n) for ev, n in dedupe(evs)]
        items = select(items, 6)
        trends: list[dict] = []
        summary = ""
        if isinstance(now, dict) and not now.get("error"):
            kp = now.get("kp") or {}
            sc = now.get("noaa_scales") or {}
            kmax = _num(kp.get("max_24h"))
            metrics["kp_max_24h"] = kmax
            ykp = _num(((yesterday or {}).get("metrics") or {}).get("kp_max_24h"))
            levels = sc.get("max_24h") or {}
            lv = ", ".join(f"{k}{levels.get(k) or 0}" for k in ("G", "S", "R"))
            line = f"Kp now {_fmt_kp(kp.get('now'))}, up to {_fmt_kp(kmax)} in the past day; NOAA scales in the past day {lv}"
            if ykp is not None and kmax is not None:
                line += f"; yesterday's briefing had Kp up to {_fmt_kp(ykp)}"
            sev = 2.0 + max([(levels.get(k) or 0) for k in ("G", "S", "R")] + [0]) * 1.5
            trends.append({"metric": "kp", "title": "Geomagnetic activity", "value": kmax, "value_text": _fmt_kp(kmax),
                           "previous": ykp, "change": None, "notable": sev >= 5, "severity": round(min(9.0, sev), 1),
                           "source": "NOAA SWPC", "line": clean(line, 260),
                           "entity": {"kind": "space_weather", "id": "swpc:now:panel", "title": "Space weather now",
                                      "layer": "space.spaceweather"}})
            flares = [f for f in ((now.get("xray") or {}).get("recent") or [])
                      if isinstance(f, dict) and str(f.get("class") or "")[:1] in ("M", "X")]
            in_period = [f for f in flares if (_iso_ts(f.get("peak_utc")) or 0) >= self.since]
            in_period.sort(key=lambda f: _flare_rank(f.get("class")), reverse=True)
            if in_period:
                big = in_period[0]
                trends.append({"metric": "flares", "title": "Solar flares", "value": len(in_period), "previous": None,
                               "change": None, "notable": str(big.get("class") or "").startswith("X"),
                               "severity": 6.0 if str(big.get("class") or "").startswith("X") else 3.0,
                               "source": "NOAA SWPC GOES X-ray",
                               "line": clean(f"{len(in_period)} M-class or larger in this period; largest "
                                             f"{big.get('class')} at {utc(_iso_ts(big.get('peak_utc')))}", 200)})
            aur = now.get("aurora") or {}
            if _num(aur.get("north_max_pct")) is not None:
                trends.append({"metric": "aurora", "title": "Aurora forecast", "value": aur.get("north_max_pct"),
                               "previous": None, "change": None, "notable": False, "severity": 1.0,
                               "source": "NOAA SWPC OVATION",
                               "line": f"Up to {aur['north_max_pct']:.0f}% chance overhead in the north and "
                                       f"{_num(aur.get('south_max_pct')) or 0:.0f}% in the south now",
                               "entity": {"kind": "space_weather", "id": "swpc:aurora:forecast", "title": "Aurora forecast",
                                          "layer": "space.aurora"}})
            summary = clean(now.get("summary") or "", 300)
        elif not items:
            summary = "Space weather data is not available right now."
        if items and not summary:
            summary = f"{len(items)} space weather event{'s' if len(items) != 1 else ''}: {items[0]['title']}."
        elif items:
            summary = f"{summary} Events: {items[0]['title']}."
        return {"id": "space", "region": "Space weather", "group": "global", "summary": clean(summary, 400),
                "items": items, "trends": trends}

    def cyber_section(self, evs: list[dict], cyber: Any, yesterday: dict | None, metrics: dict) -> dict:
        items = [event_item(ev, count=n) for ev, n in dedupe(evs)]
        trends: list[dict] = []
        summary_bits = []
        if isinstance(cyber, dict) and isinstance(cyber.get("features"), list):
            outs, c2_total, c2_online, c2_by = [], 0, 0, []
            for f in cyber["features"]:
                p = (f or {}).get("properties") or {}
                coords = ((f or {}).get("geometry") or {}).get("coordinates")
                if not (isinstance(coords, list) and len(coords) == 2):
                    coords = [None, None]
                if p.get("role") == "outage":
                    outs.append((p, coords))
                elif p.get("role") == "c2":
                    c2_total += int(p.get("count") or 0)
                    c2_online += int(p.get("online") or 0)
                    c2_by.append(p)
            outs.sort(key=lambda o: -(_num(o[0].get("sev")) or 0))
            for p, coords in outs[:6]:
                iso2 = str(p.get("iso2") or "")
                ent = {"kind": "outage", "id": f"ioda:{iso2}", "title": f"Internet outage signals in {p.get('name') or iso2}",
                       "layer": "intel.cyber", "zoom": 4}
                if _num(coords[1]) is not None and _num(coords[0]) is not None:
                    ent["lat"], ent["lon"] = round(coords[1], 4), round(coords[0], 4)
                sev = _num(p.get("sev")) or 0.0
                items.append({"kind": "outage", "module": "intel", "title": ent["title"],
                              "line": clean(f"{p.get('src') or 'IODA'}: {int(p.get('events') or 0)} outage event"
                                            f"{'s' if int(p.get('events') or 0) != 1 else ''} in the last day"),
                              "time": None, "severity": sev, "source": p.get("src") or "IODA", "entity": ent})
            meta = cyber.get("meta") or {}
            if not (meta.get("outages_at") or {}).get("ioda"):
                summary_bits.append("Internet outage signals were still loading")
            if c2_by:
                c2_by.sort(key=lambda p: -int(p.get("count") or 0))
                top = c2_by[0]
                metrics["c2_listed"] = c2_total
                y = ((yesterday or {}).get("metrics") or {}).get("c2_listed")
                ch = T.compare(c2_total, int(y)) if isinstance(y, (int, float)) else None
                line = f"{c2_total:,} listed, {c2_online:,} online; most in {top.get('name') or top.get('iso2')} ({int(top.get('count') or 0):,})"
                if ch:
                    line += f"; {T.words(ch, prev_label='in yesterday’s briefing')}"
                ent = {"kind": "cyber", "id": f"feodo:{top.get('iso2')}", "title": f"Botnet servers hosted in {top.get('name')}",
                       "layer": "intel.cyber", "zoom": 4}
                trends.append({"metric": "c2", "title": "Botnet command servers", "value": c2_total,
                               "previous": int(y) if isinstance(y, (int, float)) else None, "change": ch,
                               "notable": bool(ch and ch["notable"]), "severity": 3.0, "source": "abuse.ch Feodo Tracker",
                               "line": clean(line, 260), "entity": ent})
        items = select(items, 6)
        loaded = isinstance(cyber, dict) and bool(((cyber.get("meta") or {}).get("outages_at") or {}).get("ioda"))
        if items:
            summary_bits.insert(0, f"Most severe: {items[0]['title']}")
        elif loaded:
            summary_bits.insert(0, "No internet outage signals in the latest data")
        if trends:
            summary_bits.append(f"Botnet command servers: {trends[0]['line'].split(';')[0]}")
        if not items and not trends and not summary_bits:
            summary_bits.append("No cyber or internet data in this period")
        return {"id": "cyber", "region": "Cyber and internet", "group": "global",
                "summary": clean(". ".join(summary_bits) + "." if summary_bits else "", 400),
                "items": items, "trends": trends}

    def markets_section(self, quotes: Any, grid: Any, yesterday: dict | None, metrics: dict) -> dict:
        items, trends = [], []
        ym = ((yesterday or {}).get("metrics") or {})
        prices = {}
        for q in quotes or []:
            if not isinstance(q, dict) or not q.get("available") or _num(q.get("price")) is None:
                continue
            qid = str(q.get("id") or q.get("symbol") or "")
            price = float(q["price"])
            prices[qid] = price
            chg = _num(q.get("change_pct"))
            klass = _market_class(qid)
            thr = MARKET_THRESHOLDS[klass]
            monthly = q.get("delay") == "monthly average"
            q_time = _num(q.get("time"))
            fresh = q_time is None or self.now - q_time <= 5 * 86400     # weekends and holidays
            # a move is a daily or live change; monthly averages and stale prices are context only
            notable = chg is not None and abs(chg) >= thr and not monthly and fresh
            sev = 2.0 if not notable else round(min(6.0, 4.0 + (abs(chg) / thr - 1.0)), 1)
            basis = q.get("change_basis") or ("the month before" if monthly else "the previous value")
            line = f"{_fmt_price(price, q)}"
            if chg is not None:
                line += (f", unchanged on {basis}" if abs(chg) < 0.05 else
                         f", {'up' if chg > 0 else 'down'} {abs(chg):.1f}% on {basis}")
            when = q.get("as_of") or (utc(_num(q.get("time"))) if _num(q.get("time")) else "")
            if q.get("delay") or when:
                line += f" ({', '.join(x for x in (q.get('delay'), when) if x)})"
            row = {"metric": f"market:{qid}", "title": str(q.get("name") or qid), "value": price,
                   "value_text": _fmt_number(price, klass), "previous": None,
                   "change": {"pct": chg} if chg is not None else None, "notable": notable, "severity": sev,
                   "source": str(q.get("source") or "Meridian markets"), "line": clean(line, 220)}
            fly = q.get("fly") if isinstance(q.get("fly"), dict) else None
            if fly and _num(fly.get("lat")) is not None and _num(fly.get("lon")) is not None:
                row["entity"] = {"kind": "place", "id": f"market:{qid}", "title": str(fly.get("label") or row["title"]),
                                 "subtitle": row["title"], "lat": fly["lat"], "lon": fly["lon"], "zoom": fly.get("zoom") or 13}
            trends.append(row)
            if notable:
                items.append({"kind": "market_move", "module": "infra", "title": f"{row['title']} "
                              f"{'up' if chg > 0 else 'down'} {abs(chg):.1f}%", "line": row["line"],
                              "time": _num(q.get("time")), "severity": sev, "source": row["source"],
                              "entity": row.get("entity")})
        if prices:
            metrics["markets"] = prices
        if isinstance(grid, dict) and grid.get("available") and isinstance(grid.get("carbon_intensity"), dict):
            ci = grid["carbon_intensity"]
            g = _num(ci.get("g_co2_per_kwh"))
            ren = _num(grid.get("renewable_percent"))
            dem = _num(grid.get("demand_mw"))
            metrics["grid_gb"] = {"intensity": g, "renewable": ren}
            y = (ym.get("grid_gb") or {}).get("intensity")
            bits = []
            if g is not None:
                bits.append(f"{g:.0f} g CO2 per kWh" + (f" ({ci.get('index')})" if ci.get("index") else ""))
            if ren is not None:
                bits.append(f"{ren:.0f}% renewable")
            if dem is not None:
                bits.append(f"demand {dem / 1000:.1f} GW")
            price = (grid.get("price") or {}).get("gbp_per_mwh") if isinstance(grid.get("price"), dict) else None
            if _num(price) is not None:
                bits.append(f"£{float(price):.0f} per MWh")
            line = ", ".join(bits)
            if g is not None and _num(y) is not None:
                line += f"; {float(y):.0f} g in yesterday’s briefing"
            trends.append({"metric": "grid_gb", "title": "Great Britain electricity now", "value": g,
                           "previous": _num(y), "change": None, "notable": False, "severity": 2.0,
                           "source": "NESO and Elexon", "line": clean(line, 260),
                           "entity": {"kind": "point", "id": "grid:GB", "title": "Great Britain electricity grid",
                                      "lat": 54.0, "lon": -2.5, "zoom": 5, "layer": "infra.grid"}})
        items = select(items, 6)
        if items:
            summary = "Largest moves: " + ", ".join(i["title"] for i in items[:3]) + "."
        elif trends:
            summary = "No large market moves in the latest prices."
        else:
            summary = "Market and grid data is not available right now."
        g_row = next((t for t in trends if t["metric"] == "grid_gb"), None)
        if g_row:
            summary += f" Great Britain grid: {g_row['line'].split(';')[0]}."
        return {"id": "markets", "region": "Markets and energy", "group": "global", "summary": clean(summary, 400),
                "items": items, "trends": trends}

    # -- your areas (alerts) ----------------------------------------------------------------------
    def areas_section(self, alerts: Any) -> dict | None:
        if alerts is None:
            return None                       # the alerts module is not running: skip silently
        items = []
        for a in alerts:
            if not isinstance(a, dict) or not a.get("title"):
                continue
            t = _num(a.get("time"))
            if t is not None and t > 1e11:
                t /= 1000.0
            if t is not None and t < self.since:
                continue
            area = _label(a.get("area"))
            rule = _label(a.get("rule"))
            why = clean(a.get("why") or a.get("subtitle"), 140)
            ent = a.get("entity") if isinstance(a.get("entity"), dict) and a["entity"].get("kind") and \
                a["entity"].get("id") is not None else None
            if ent:
                ent = {k: ent[k] for k in ("kind", "id", "title", "subtitle", "lat", "lon", "layer", "zoom", "bbox")
                       if k in ent}
                ent["id"] = str(ent["id"])
                if _num(ent.get("lat")) is None and _num(a.get("lat")) is not None and _num(a.get("lon")) is not None:
                    ent["lat"], ent["lon"] = round(float(a["lat"]), 5), round(float(a["lon"]), 5)
            line = ", ".join(x for x in (area, rule) if x)
            if why:
                line = f"{line}: {why}" if line else why
            items.append({"kind": "alert", "module": "alerts", "title": clean(a.get("title"), 160),
                          "line": clean(line), "time": t, "severity": _num(a.get("severity")) or 0.0,
                          "source": "Your alerts", "entity": ent, "area": area})
        items.sort(key=lambda i: (-(i["severity"] or 0), -(i["time"] or 0)))
        areas = sorted({i["area"] for i in items if i.get("area")})
        summary = (f"{len(items)} alert{'s' if len(items) != 1 else ''}" +
                   (f" in {len(areas)} of your areas" if len(areas) > 1 else " in one of your areas" if areas else "") + ".") if items \
            else "No alerts fired in your areas in this period."
        return {"id": "areas", "region": "Your areas", "group": "areas", "summary": summary, "items": items[:12],
                "trends": []}

    # -- headline -----------------------------------------------------------------------------------
    def headline_section(self, regional: list[dict], glob: list[dict]) -> dict:
        def score(row: dict) -> float:
            s = float(row.get("severity") or 0) + (0.5 if row.get("new") else 0.0)
            s -= SIGNAL_PENALTY.get(str(row.get("kind")), 0.0)
            return s - (UNVERIFIED_PENALTY if row.get("unverified") else 0.0)

        cands = []
        for sec in regional + glob:
            for it in sec["items"]:
                cands.append((score(it), sec, it, "item"))
            for tr in sec.get("trends") or []:
                # market moves and space weather events are already items; the Kp row only stands in
                # for them when the section has no events
                if not tr.get("notable") or str(tr.get("metric")).startswith("market:"):
                    continue
                if tr.get("metric") in ("kp", "flares") and sec["items"]:
                    continue
                cands.append((score(tr), sec, tr, "trend"))
        cands.sort(key=lambda c: (-c[0], -(c[2].get("time") or 0)))
        picked, per_region, per_kind, titles = [], defaultdict(int), defaultdict(int), set()
        unverified = 0
        for floor in (5.0, 3.5):
            for sc, sec, it, what in cands:
                if len(picked) >= HEADLINE_MAX or (floor == 3.5 and len(picked) >= HEADLINE_MIN):
                    break
                if sc < floor:
                    break
                key = it.get("metric") or it.get("kind")
                tkey = _norm(it.get("title"))
                if tkey in titles or per_region[sec["id"]] >= 2 or per_kind[key] >= 1:
                    continue
                if it.get("unverified") and unverified >= HEADLINE_UNVERIFIED_MAX:
                    continue
                unverified += 1 if it.get("unverified") else 0
                titles.add(tkey)
                per_region[sec["id"]] += 1
                per_kind[key] += 1
                row = {k: v for k, v in it.items() if k not in ("change",)}
                row["section"] = sec["id"]
                row["region"] = sec["region"]
                if what == "trend":
                    row["kind"] = "trend"
                    row["title"] = _trend_headline(it)
                picked.append(row)
        if not picked:
            picked.append({"kind": "note", "title": "A quiet period", "line": "No serious events were recorded.",
                           "time": None, "severity": 0.0, "source": "Meridian", "entity": None, "section": None,
                           "region": ""})
        summary = f"{len(picked)} thing{'s' if len(picked) != 1 else ''} to know" if picked[0]["kind"] != "note" \
            else "A quiet period."
        return {"id": "headline", "region": "Headline", "group": "headline", "summary": summary, "items": picked,
                "trends": []}


# ---------------------------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------------------------
def deterministic_text(doc: dict) -> str:
    """Short paragraphs of plain sentences built from the document (no AI)."""
    p = doc["period"]
    st = doc["stats"]
    paras = [f"Briefing for {period_text(p['from'], p['to'])} ({p['label'].lower()}). Meridian recorded "
             f"{st['events']:,} notable event{'s' if st['events'] != 1 else ''}, {st['serious']:,} of them serious or extreme."]
    secs = {s["id"]: s for s in doc["sections"]}
    head = secs.get("headline")
    if head and head["items"] and head["items"][0]["kind"] != "note":
        paras.append("Headline: " + "; ".join(f"{i['title']} ({i['region']})" for i in head["items"]) + ".")
    elif head:
        paras.append("A quiet period: no serious events were recorded.")
    if "areas" in secs:
        paras.append(f"Your areas: {secs['areas']['summary']}")
    paras += [f"{secs[rid]['region']}: {secs[rid]['summary']}" for rid, _ in REGIONS if rid in secs]
    paras += [f"{secs[gid]['region']}: {secs[gid]['summary']}" for gid, _ in GLOBAL_SECTIONS
              if gid in secs and secs[gid]["summary"]]
    if any(i.get("unverified") for s in doc["sections"] for i in s["items"] + (s.get("trends") or [])):
        paras.append(UNVERIFIED_NOTE)
    return "\n\n".join(paras)


# ---------------------------------------------------------------------------------------------
# Odds and ends
# ---------------------------------------------------------------------------------------------
_EXTRA_NAMES = {"alerts": "Your alerts", "space": "Space weather", "cyber": "Cyber and internet",
                "markets": "Markets", "grid": "Great Britain grid", "straits": "Straits"}


def _basis_label(basis: str, hours: float) -> str:
    if basis == "since_last":
        return f"Since the previous briefing ({hours:.0f} h)" if hours >= 1.5 else "Since the previous briefing"
    if 23.5 <= hours <= 24.5:
        return "Last 24 hours"
    return f"Last {hours:.0f} hours"


def _count_by_country(evs: list[dict], kind: str) -> dict[str, tuple[int, str]]:
    out: dict[str, list] = {}
    for e in evs:
        c = e.get("_country")
        if e.get("kind") != kind or not c or not c.get("iso2"):
            continue
        row = out.setdefault(c["iso2"], [0, c.get("name") or c["iso2"]])
        row[0] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def _straits_by_region(straits: Any, place) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    rows = straits.get("straits") if isinstance(straits, dict) else None
    for s in rows if isinstance(rows, list) else []:
        if not isinstance(s, dict) or not s.get("coverage") or not s.get("id"):
            continue
        lab = s.get("label")
        if not (isinstance(lab, list) and len(lab) == 2):
            continue
        region, _ = place(lab[1], lab[0])
        out[region].append(s)
    for lst in out.values():
        lst.sort(key=lambda s: (-(1 if s.get("congestion") else 0), -(s.get("inside_now") or 0)))
    return out


def _short_change(ch: dict | None) -> str:
    if not ch:
        return ""
    d = ch.get("direction")
    if d in ("up", "down") and ch.get("pct") is not None:
        return f"{d} {abs(ch['pct']):.0f}%"
    return {"steady": "about the same", "new": "none before", "gone": "none now"}.get(d, "")


def _trend_headline(tr: dict) -> str:
    ch = tr.get("change") or {}
    if tr.get("metric") == "conflict_reports":
        word = "rose" if (ch.get("direction") in ("up", "new")) else "fell"
        return f"Conflict reports about {tr['title']} {word} to {tr['value']:,}"
    if tr.get("metric") == "strait":
        return f"Congestion in {tr['title']}"
    return f"{tr['title']}: {tr['value']:,}" if isinstance(tr.get("value"), int) else str(tr["title"])


def _label(v: Any) -> str:
    if isinstance(v, dict):
        v = v.get("name") or v.get("title") or v.get("label") or v.get("id")
    return clean(v, 80) if v not in (None, "") else ""


def _iso_ts(v: Any) -> float | None:
    if not isinstance(v, str) or not v:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _flare_rank(cls: Any) -> float:
    """'X1.2' > 'M9.9' > 'M1.0': class letter then magnitude."""
    text = str(cls or "").upper()
    base = {"X": 3, "M": 2, "C": 1}.get(text[:1], 0)
    try:
        return base * 100 + float(text[1:] or 0)
    except ValueError:
        return base * 100.0


def _fmt_kp(v: Any) -> str:
    f = _num(v)
    return "unknown" if f is None else f"{f:.1f}".rstrip("0").rstrip(".") if f % 1 else f"{f:.0f}"


def _market_class(qid: str) -> str:
    q = qid.lower()
    if q.startswith("fx_") or re.fullmatch(r"[a-z]{6}", q):
        return "fx"
    if q in ("btc", "eth"):
        return "crypto"
    if q in ("spx", "dji", "ixic", "n225", "ftse", "dax", "hsi", "china", "india"):
        return "index"
    return "commodity"


def _fmt_number(v: float, klass: str) -> str:
    """A short number for the value column (the line carries the unit and currency)."""
    if klass == "fx":
        return f"{v:,.4f}" if v < 20 else f"{v:,.2f}"
    return f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"


def _fmt_price(price: float, q: dict) -> str:
    cur = q.get("currency")
    unit = q.get("unit") or ""
    if unit == "index":
        return f"{price:,.0f}"
    if _market_class(str(q.get("id") or q.get("symbol") or "")) == "fx":
        return f"{price:,.4f}" if price < 20 else f"{price:,.2f}"
    text = f"{price:,.2f}" if price < 1000 else f"{price:,.0f}"
    if cur == "USD":
        text = f"${text}"
    elif cur:
        text = f"{text} {cur}"
    return f"{text} {unit}".strip()


def _sources(cur: list[dict], extras: dict) -> list[str]:
    names = set()
    for e in cur:
        names.add(source_of(e))
    if extras.get("space"):
        names.add("NOAA SWPC")
    if extras.get("cyber"):
        names.update({"abuse.ch Feodo Tracker", "IODA"})
    if extras.get("grid"):
        names.add("NESO and Elexon")
    for q in extras.get("markets") or []:
        if isinstance(q, dict) and q.get("available") and q.get("source"):
            names.add(str(q["source"]))
    if extras.get("alerts"):
        names.add("Your alerts")
    return sorted(n for n in names if n)


def _tool_entity(e: dict | None) -> dict | None:
    if not e:
        return None
    out = {k: e[k] for k in ("kind", "id", "title", "layer") if e.get(k) not in (None, "")}
    if isinstance(e.get("lat"), (int, float)) and isinstance(e.get("lon"), (int, float)):
        out["lat"], out["lon"] = round(e["lat"], 3), round(e["lon"], 3)
    return out or None


def _tool_item(it: dict) -> dict:
    row = {"title": it["title"], "line": it.get("line"), "time_utc": utc(it.get("time")) or None,
           "severity": it.get("severity"), "source": it.get("source"), "entity": _tool_entity(it.get("entity"))}
    if it.get("unverified"):
        row["unverified"] = True
    if it.get("new"):
        row["new"] = True
    if it.get("region"):
        row["region"] = it["region"]
    return row


def compact(doc: dict, *, section: str | None = None, include_areas: bool = False) -> dict:
    """The AI tool's answer, sized for a tool result (about 8 KB): without ``section`` an overview (the
    plain text, the narrative, the headline with entities and what each section holds); with one, that
    section in full. "Your areas" only when asked for by name (``include_areas``)."""
    out = {"id": doc["id"], "time_utc": utc(doc["time"]), "period": doc["period"]["label"],
           "period_utc": period_text(doc["period"]["from"], doc["period"]["to"]),
           "ai_narrative": (doc.get("ai") or {}).get("text"), "notes": (doc.get("coverage") or {}).get("notes")}
    secs = [s for s in doc["sections"] if s["id"] != "areas" or include_areas]
    if section is None:
        out["text"] = doc.get("text")
        head = next((s for s in secs if s["id"] == "headline"), None)
        out["headline"] = [_tool_item(i) for i in head["items"]] if head else []
        out["sections"] = [{"id": s["id"], "region": s["region"], "items": len(s["items"]),
                            "trends": len(s.get("trends") or []),
                            "notable": [t["title"] for t in s.get("trends") or [] if t.get("notable")][:4]}
                           for s in secs if s["id"] != "headline"]
        out["more"] = "Ask again with section set to one of the section ids for its items, trends and map entities."
        if not include_areas and any(s["id"] == "areas" for s in doc["sections"]):
            out["your_areas"] = "Left out: ask for section your_areas to include the user's own alerts."
        return out
    for s in secs:
        if s["id"] != section:
            continue
        out["section"] = {"id": s["id"], "region": s["region"], "summary": s.get("summary"),
                          "trends": [{"title": t["title"], "line": t.get("line"), "notable": bool(t.get("notable")),
                                      "source": t.get("source"), "entity": _tool_entity(t.get("entity"))}
                                     for t in (s.get("trends") or [])[:10]],
                          "items": [_tool_item(i) for i in s["items"][:8]]}
    return out
