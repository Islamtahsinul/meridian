"""Convergences: places where at least three different kinds of event coincide within 24 hours.

Events from every module (``App.collect_events``) are binned into 0.5-degree cells. Each candidate
window is a 1 x 1 degree square (2 x 2 cells) on a 0.5-degree stride, so events either side of a cell
edge still meet. Windows with 3 or more distinct kinds qualify; overlapping windows are thinned
greedily (most kinds first, then total severity). Derived kinds (news, risk zones, convergences,
countries) never count as a kind: news comes from the same reports as conflict events. Country-level and
planet-wide signals are left out too: news spikes and travel-advice changes are placed at a country's label
point, and space weather at the geomagnetic pole or under the Sun, so they would only make coincidences of
position. Protests and conflict events are both GDELT reports (often the same articles), so together they
count as one kind.
"""

from __future__ import annotations

import math
from collections import Counter

EXCLUDED_KINDS = frozenset({"convergence", "news", "risk_zone", "country", "point", "place", "space_weather",
                            "news_spike", "advice_change", "air_activity"})
# Ongoing background patterns that would make every day a "convergence" in the same places.
EXCLUDED_CATEGORIES = frozenset({"chronic_interference"})
# Kinds that come from the same source reports count once (the first is the family name shown).
KIND_FAMILY = {"protest": "conflict_event"}
CELL = 0.5
WINDOW_HOURS = 24
MIN_KINDS = 3


_KIND_WORDS = {
    "conflict_event": "conflict events", "earthquake": "earthquakes", "fire": "fires", "volcano": "volcanic activity",
    "storm": "a tropical storm", "flood": "flooding", "warning": "weather warnings", "outbreak": "a disease outbreak",
    "disaster": "a disaster alert", "tsunami": "a tsunami message", "gps_jam": "GPS interference",
    "aircraft": "aircraft alerts", "ship": "ship alerts", "sanctioned_vessel": "sanctioned vessels",
    "sanctioned_aircraft": "sanctioned aircraft", "launch": "a launch", "cyber": "cyber activity",
    "outage": "an internet outage", "frontline": "front-line activity", "strait": "strait congestion",
    "protest": "protests",
}


def _kind_words(kind: str) -> str:
    return _KIND_WORDS.get(kind or "", (kind or "event").replace("_", " "))


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def kinds_phrase(kinds: list[str]) -> str:
    return _join([_kind_words(k) for k in kinds])


def detect(events: list[dict], now: float, *, cell: float = CELL, window_hours: float = WINDOW_HOURS,
           min_kinds: int = MIN_KINDS, max_members: int = 40) -> list[dict]:
    since = now - window_hours * 3600
    bins: dict[tuple[int, int], list[dict]] = {}
    for ev in events:
        kind = ev.get("kind")
        if not kind or kind in EXCLUDED_KINDS or ev.get("category") in EXCLUDED_CATEGORIES:
            continue
        t = ev.get("time")
        if not isinstance(t, (int, float)) or t < since or t > now + 3600:
            continue
        try:
            lat, lon = float(ev["lat"]), float(ev["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        key = (int(math.floor(lat / cell)), int(math.floor(lon / cell)))
        bins.setdefault(key, []).append(ev)
    origins = set()
    for r, c in bins:
        for dr in (-1, 0):
            for dc in (-1, 0):
                origins.add((r + dr, c + dc))
    candidates = []
    for r0, c0 in origins:
        members = []
        for dr in (0, 1):
            for dc in (0, 1):
                members.extend(bins.get((r0 + dr, c0 + dc), ()))
        kinds = {KIND_FAMILY.get(m["kind"], m["kind"]) for m in members}
        if len(kinds) >= min_kinds:
            total = sum(float(m.get("severity") or 0) for m in members)
            candidates.append((len(kinds), total, (r0, c0), members))
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
    taken: list[tuple[int, int]] = []
    out = []
    for nk, total, (r0, c0), members in candidates:
        if any(abs(r0 - r) <= 1 and abs(c0 - c) <= 1 for r, c in taken):
            continue
        taken.append((r0, c0))
        counts = Counter(m["kind"] for m in members)
        kinds = sorted(counts, key=lambda k: (-counts[k], k))
        fam = Counter(KIND_FAMILY.get(m["kind"], m["kind"]) for m in members)
        fam_kinds = sorted(fam, key=lambda k: (-fam[k], k))
        words = [("conflict and protest reports" if k == "conflict_event" and "protest" in counts else _kind_words(k))
                 for k in fam_kinds]
        sev = [float(m.get("severity") or 0) for m in members]
        wsum = sum(s + 1.0 for s in sev)
        lat = sum(float(m["lat"]) * (s + 1.0) for m, s in zip(members, sev)) / wsum
        lon = sum(float(m["lon"]) * (s + 1.0) for m, s in zip(members, sev)) / wsum
        members_sorted = sorted(members, key=lambda m: (-(m.get("severity") or 0), -(m.get("time") or 0)))
        severity = min(10.0, 1.5 * (nk - 2) + 0.5 * max(sev) + 0.3 * math.log2(len(members)))
        out.append({
            "kind": "convergence", "id": f"cv:{r0 * cell:.1f}:{c0 * cell:.1f}",
            "title": f"{nk} kinds of event: {_join(words)}",
            "lat": round(lat, 5), "lon": round(lon, 5),
            "bbox": [c0 * cell, r0 * cell, (c0 + 2) * cell, (r0 + 2) * cell],
            "time": max(float(m["time"]) for m in members), "severity": round(severity, 1),
            "kinds": [{"kind": k, "count": counts[k]} for k in kinds], "n_kinds": nk, "n_events": len(members),
            "modules": sorted({m.get("module") or "" for m in members if m.get("module")}),
            "members": [{k: m.get(k) for k in ("kind", "id", "title", "subtitle", "lat", "lon", "time", "severity",
                                                "layer", "module")} for m in members_sorted[:max_members]],
        })
    out.sort(key=lambda c: (-c["severity"], -c["time"]))
    return out
