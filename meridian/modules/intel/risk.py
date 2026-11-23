"""Risk zones: grid cells where conflict events cluster over the last 7 days (density x severity).

Each precisely located, non-crime conflict event adds (severity / 10) x 0.5^(age / 3.5 days) to its
0.5-degree cell. The cell total is then multiplied by its conflict-share weight: the fraction of all
GDELT reports from that cell over the window that are violent conflict, divided by 25 %, capped at 1
and raised to the power 1.5.
Busy news hubs (capitals, entertainment cities) where violent stories are a small part of the news
fade; places where most news is about fighting keep full weight. Cells with at least 4 events and a
weighted score of 2 or more are hot; 8-connected hot cells form a zone.
"""

from __future__ import annotations

import math
from collections import Counter

from . import geomath

CELL = 0.5
HALF_LIFE_DAYS = 3.5
WINDOW_DAYS = 7
MIN_EVENTS = 4
MIN_SCORE = 2.0
SHARE_FULL = 0.25     # conflict share of a cell's coverage that earns full weight
MIN_SHARE_ROWS = 30   # below this many reports the share is not trusted (weight 1)


def share_weight(rows: int, conflict: int) -> float:
    if rows < MIN_SHARE_ROWS:
        return 1.0
    return max(0.02, min(1.0, (conflict / rows) / SHARE_FULL) ** 1.5)
LEVELS = [(6.0, "Elevated"), (15.0, "High"), (math.inf, "Very high")]


def level_of(score: float) -> str:
    for top, name in LEVELS:
        if score < top:
            return name
    return LEVELS[-1][1]


def compute_zones(events: list[dict], now: float, *, cell: float = CELL, half_life: float = HALF_LIFE_DAYS,
                  min_events: int = MIN_EVENTS, min_score: float = MIN_SCORE, window_days: float = WINDOW_DAYS,
                  max_members: int = 60, shares: dict | None = None) -> list[dict]:
    cells: dict[tuple[int, int], dict] = {}
    since = now - window_days * 86400
    for ev in events:
        t = ev.get("t_last") or ev.get("time") or 0
        if t < since or ev.get("crime") or ev.get("precision", "city") != "city":
            continue
        age = max(0.0, (now - t) / 86400.0)
        w = (float(ev.get("severity") or 0) / 10.0) * 0.5 ** (age / half_life)
        key = (int(math.floor(ev["lat"] / cell)), int(math.floor(ev["lon"] / cell)))
        c = cells.setdefault(key, {"score": 0.0, "n": 0, "events": []})
        c["score"] += w
        c["n"] += 1
        c["events"].append(ev)
    for k, c in cells.items():
        rows, conflict = (shares or {}).get(k, (0, 0))
        c["raw"] = c["score"]
        c["share"] = (conflict / rows) if rows else None
        c["score"] *= share_weight(rows, conflict) if shares is not None else 1.0
    hot = {k for k, c in cells.items() if c["n"] >= min_events and c["score"] >= min_score}
    zones = []
    for comp in geomath.components(hot):
        members = [ev for k in comp for ev in cells[k]["events"]]
        score = sum(cells[k]["score"] for k in comp)
        top = max(comp, key=lambda k: cells[k]["score"])
        wsum = sum(cells[k]["score"] for k in comp) or 1.0
        lat = sum((k[0] + 0.5) * cell * cells[k]["score"] for k in comp) / wsum
        lon = sum((k[1] + 0.5) * cell * cells[k]["score"] for k in comp) / wsum
        places = Counter((ev.get("place") or "", ev.get("iso2")) for ev in members if ev.get("place"))
        countries = Counter(ev.get("iso2") for ev in members if ev.get("iso2"))
        place = places.most_common(1)[0][0][0] if places else ""
        members.sort(key=lambda e: (-(e.get("severity") or 0), -(e.get("t_last") or 0)))
        rows = [k[0] for k in comp]
        cols = [k[1] for k in comp]
        bbox = [min(cols) * cell, min(rows) * cell, (max(cols) + 1) * cell, (max(rows) + 1) * cell]
        cats = Counter(ev.get("category") for ev in members)
        zones.append({
            "id": f"rz:{top[0] * cell:.1f}:{top[1] * cell:.1f}",
            "score": round(score, 2), "level": level_of(score), "events": len(members),
            "max_severity": max((ev.get("severity") or 0) for ev in members),
            "latest": max((ev.get("t_last") or 0) for ev in members),
            "lat": round(lat, 4), "lon": round(lon, 4), "bbox": [round(v, 4) for v in bbox],
            "place": place, "short": place.split(",")[0].strip() if place else "",
            "iso2": countries.most_common(1)[0][0] if countries else None,
            "places": places.most_common(8), "categories": dict(cats),
            "cells": [{"r": k[0], "c": k[1], "score": round(cells[k]["score"], 3), "n": cells[k]["n"]} for k in sorted(comp)],
            "conflict_share": _share(comp, shares),
            "outline": geomath.cell_outline(comp, cell),
            "area_km2": round(sum((cell * geomath.KM_PER_DEG_LAT) * (cell * 111.32 * math.cos(math.radians((k[0] + 0.5) * cell)))
                                  for k in comp)),
            "member_ids": [ev["id"] for ev in members[:max_members] if ev.get("id")],
        })
    zones.sort(key=lambda z: -z["score"])
    return zones


def _share(comp, shares) -> float | None:
    if not shares:
        return None
    rows = sum((shares.get(k) or (0, 0))[0] for k in comp)
    conflict = sum((shares.get(k) or (0, 0))[1] for k in comp)
    return round(conflict / rows, 4) if rows else None


def zones_geojson(zones: list[dict], cell: float = CELL) -> dict:
    feats = []
    top = max((c["score"] for z in zones for c in z["cells"]), default=1.0) or 1.0
    for z in zones:
        for c in z["cells"]:
            x0, y0 = c["c"] * cell, c["r"] * cell
            ring = [[x0, y0], [x0 + cell, y0], [x0 + cell, y0 + cell], [x0, y0 + cell], [x0, y0]]
            feats.append({"type": "Feature", "properties": {"role": "cell", "zone": z["id"], "level": z["level"],
                                                            "w": round(min(1.0, math.sqrt(c["score"] / top)), 3), "n": c["n"]},
                          "geometry": {"type": "Polygon", "coordinates": [[[round(x, 4), round(y, 4)] for x, y in ring]]}})
        feats.append({"type": "Feature", "properties": {"role": "outline", "zone": z["id"], "level": z["level"]},
                      "geometry": {"type": "MultiLineString", "coordinates": z["outline"]}})
        name = z["short"] or "Unnamed area"
        feats.append({"type": "Feature", "properties": {"role": "label", "zone": z["id"], "level": z["level"],
                                                        "name": f"{name} area", "events": z["events"], "score": z["score"]},
                      "geometry": {"type": "Point", "coordinates": [z["lon"], z["lat"]]}})
    return {"type": "FeatureCollection", "features": feats}
