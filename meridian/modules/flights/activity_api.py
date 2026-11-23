"""Routes and AI tools for the air activity detectors (activity.py). Every route here reads stored
tallies or cached assessments and is registered with ``activity=False``: opening the panel or a card
never speeds up live polling."""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from meridian import geo
from meridian.registry import HTTPError

from . import activity_rules as R
from .activity import CATEGORY_LABELS, DETECT_EVERY_S, HOUR, LAYER, ActivityService
from .api import _area
from .const import GPS_CELL_DEG

_IDENT = re.compile(r"^[A-Za-z0-9-]{2,12}$")
_RID = re.compile(r"^[a-z_]{2,40}$")
_EID = re.compile(r"^flights:(surge|gps|airport):[A-Za-z0-9_.:-]{1,80}$")
LEVEL = {"unusual": 3, "drop": 3, "notable": 2, "drop_unverified": 2, "coverage_changed": 1, "feed_changed": 1,
         "normal": 1, "learning": 0, "no_data": 0, "no_coverage": 0}
ORDER = {"unusual": 0, "drop": 0, "drop_unverified": 1, "notable": 1, "coverage_changed": 2, "feed_changed": 2,
         "normal": 3, "learning": 4, "no_data": 5, "no_coverage": 5}
METHOD = {
    "military_surge": ("Military aircraft that broadcast ADS-B (adsb.fi's worldwide military list) are counted in each "
                       "region from one snapshot every 10 minutes, as aircraft-hours over the last 3 complete hours. "
                       "That is compared with the same 3 hours on each of the previous 7 to 28 days or, after two "
                       "weeks, on the same weekday only (in the second week, a level like the same hours a week "
                       "earlier is held back). Unusual means at least twice the usual and a robust z of 3 or more "
                       "(for small numbers, a Poisson test of 1 in 1,000) with at least 4 different aircraft; no "
                       "multiple is given when the usual is under one aircraft-hour. Trainers and helicopters are "
                       "left out; hours when Meridian was not sampling are never used."),
    "gps_interference": ("Each half-degree cell's last 6 complete hours are compared with the same clock hours on each "
                         "of its previous 7 days (at least 5 with enough aircraft). New: 10% or more of aircraft "
                         "reported degraded GPS accuracy, with 2 or more affected aircraft in the same hour more than "
                         "once, after a quiet week (under 2% in those hours, no day affected). Worse: at least twice "
                         "its highest share in those hours on any earlier day and more than that day's share would "
                         "give by chance. Cells affected on most days, at any hour, are chronic: they stay on the GPS "
                         "layer but raise no new events."),
    "airport_drop": ("Arrivals (descending below 12,000 ft) and departures (climbing) within the airport's circle "
                     "are counted from one ADS-B snapshot every 10 minutes and compared with the same hour on the "
                     "previous 7 to 14 days (and weekdays after two weeks). A drop means 40% of usual or less for 2 "
                     "hours in an hour that usually has 10 movements or more, while overflights stay normal."),
}
CAVEATS = ("These are candidates for a closer look, not findings: each lists its evidence, its coverage and "
           "common innocent explanations.",
           "Meridian sees only aircraft that broadcast ADS-B and are heard by volunteer receivers.")


def _r(v, d=2):
    if v is None:
        return None
    try:
        return round(float(v), d)
    except (TypeError, ValueError):
        return None


class ActivityApi:
    """Read side: briefs, details, map features and AI answers built from ``ActivityService``."""

    def __init__(self, act: ActivityService):
        self.act = act
        self.lock = threading.Lock()

    def ensure(self) -> None:
        """Assess once if the background task has not run yet (first seconds after start-up)."""
        act = self.act
        if act.detect_t and act.clock() - act.detect_t < DETECT_EVERY_S * 3:
            return
        with self.lock:
            if act.detect_t and act.clock() - act.detect_t < DETECT_EVERY_S * 3:
                return
            act.detect(force_gps=not act.gps_t)

    # -- briefs -----------------------------------------------------------------------------------
    @staticmethod
    def region_brief(s: dict) -> dict:
        return {"id": s["id"], "name": s["name"], "label": s["label"], "bbox": s["bbox"], "state": s["state"],
                "level": LEVEL.get(s["state"], 0), "status": s["status"], "severity": s["severity"],
                "confidence": s["confidence"], "ratio": _r(s["ratio"]), "per_hour": _r(s["per_hour"], 1),
                "usual_per_hour": _r(s["usual_per_hour"], 1), "distinct": s["distinct"],
                "baseline_days": s["baseline_days"], "needed": s["needed"], "coverage": s["coverage"],
                "profile": s["profile"], "this_hour": s["this_hour"], "episode": s.get("episode"),
                "entity": region_entity(s)}

    @staticmethod
    def airport_brief(s: dict) -> dict:
        return {"ident": s["ident"], "iata": s.get("iata"), "name": s["name"], "lat": s["lat"], "lon": s["lon"],
                "country": s.get("country"), "state": s["state"], "level": LEVEL.get(s["state"], 0),
                "status": s["status"], "severity": s["severity"], "confidence": s["confidence"],
                "value": s["value"], "usual": _r(s["usual"], 1), "share": _r(s["share"], 3),
                "coverage": _r(s["coverage"], 2), "days": s["days"], "needed": s["needed"],
                "receivers": s["receivers"], "background": s["background"], "watched": s["watched"],
                "hours_seen": s["hours_seen"], "this_hour": s["this_hour"], "faa": bool(s.get("faa")),
                "episode": s.get("episode"), "entity": airport_entity(s)}

    @staticmethod
    def cluster_brief(c: dict) -> dict:
        title, sub = R.gps_cluster_words(c)
        return {"title": title, "subtitle": sub, "change": c["change"], "cells": c["n"], "lat": c["lat"],
                "lon": c["lon"], "bbox": c["bbox"], "share_now": _r(c["share_now"], 4),
                "share_base": _r(c["share_base"], 4), "recent_total": c["recent_total"],
                "recent_bad": c["recent_bad"], "base_total": c.get("base_total"),
                "days_with_data": c.get("days_with_data"), "confidence": c.get("confidence"),
                "confidence_reasons": c.get("confidence_reasons") or [], "severity": c["severity"],
                "place": c["place"], "episode": c.get("episode"),
                "entity": {"kind": "air_activity", "id": c.get("episode") or f"flights:gps:{c['lat']}:{c['lon']}",
                           "title": title, "subtitle": sub, "lat": c["lat"], "lon": c["lon"], "layer": LAYER,
                           "bbox": c["bbox"], "zoom": 7}}

    def episodes(self, *, days: float = 7.0, category: str | None = None, ref: str | None = None,
                 limit: int = 40) -> list[dict]:
        where, args = ["last_t >= ?"], [self.act.clock() - days * 86400]
        if category:
            where.append("category = ?")
            args.append(category)
        if ref:
            where.append("ref = ?")
            args.append(ref)
        rows = self.act.store.query(f"SELECT * FROM flights_activity WHERE {' AND '.join(where)} "
                                    f"ORDER BY last_t DESC LIMIT ?", (*args, int(limit)))
        return [self.act.episode_event(r) for r in rows]

    def summary(self) -> dict:
        self.ensure()
        act = self.act
        now = act.clock()
        regions = sorted(act.region_states.values(), key=lambda s: (ORDER.get(s["state"], 9), -(s["ratio"] or 0),
                                                                  s["name"]))
        airports = sorted(act.airport_states.values(),
                          key=lambda s: (ORDER.get(s["state"], 9), not s["background"], -s["hours_seen"], s["name"]))
        live_r, live_a = act.this_hour(now)
        return {"now": round(now, 1), "updated": round(act.detect_t, 1) if act.detect_t else None,
                "flags": act.flags(), "sampler": act.sampler_info(now),
                "regions": [dict(self.region_brief(s), this_hour=live_r.get(s["id"], s["this_hour"])) for s in regions],
                "gps": {"clusters": [self.cluster_brief(c) for c in act.gps_cluster_list[:40]],
                        "counts": dict(act.gps_counts), "updated": round(act.gps_t, 1) if act.gps_t else None},
                "airports": [dict(self.airport_brief(s), this_hour=live_a.get(s["ident"], s["this_hour"])) for s in airports],
                "episodes": self.episodes(days=7.0, limit=40), "categories": CATEGORY_LABELS, "method": METHOD,
                "caveats": list(CAVEATS)}

    # -- details ----------------------------------------------------------------------------------
    def region_detail(self, rid: str) -> dict:
        self.ensure()
        act = self.act
        reg = R.REGION_BY_ID.get(rid)
        st = act.region_states.get(rid)
        if reg is None or st is None:
            raise KeyError(rid)
        now = act.clock()
        end = int(now // HOUR) * HOUR
        since_h = end // HOUR - 48 - 28 * 24 - R.MIL_WINDOW_H
        n = {int(r["hour"]) * HOUR: int(r["n"]) for r in act.store.query(
            "SELECT hour, n FROM flights_mil_hour WHERE region = ? AND hour >= ?", (rid, since_h))}
        uptime, _world, _seeded = act._coverage(since_h)
        series = []
        block_s = R.MIL_WINDOW_H * HOUR
        usual_of: dict[int, float | None] = {}
        for k in range(48, -1, -1):
            hs = end - k * HOUR
            # The detector's own usual, an hour, for the 3-hour block this hour belongs to (blocks line up
            # with the compared window [end - 3 h, end)): the same window and profile the status was, or
            # would have been, judged against; None while the baseline is learning.
            block_end = end - ((end - hs - 1) // block_s) * block_s
            if block_end not in usual_of:
                usual, _profile, _n = R.surge_usual(n, block_end, uptime=uptime)
                usual_of[block_end] = None if usual is None else round(usual / R.MIL_WINDOW_H, 2)
            cov = uptime.get(hs, 0.0)
            series.append([hs, n.get(hs, 0) if cov else None, round(cov, 2), usual_of[block_end]])
        aircraft = []
        tracker = act.svc.tracker
        for ac, lat, lon, est in tracker.displayed(now, where=lambda a: a.military and not a.private):
            if est or not reg.contains(lat, lon):
                continue
            ent = ac.entity(now, layer="flights.military")
            role = R.role_of(ac.typ, ac.cls, ac.shp)
            ent["subtitle"] = ", ".join(x for x in (R.D.ROLE_LABELS[role].rstrip("s") if role != "isr" else "Surveillance",
                                                    ac.typ, f"{ac.alt:,} ft" if ac.alt is not None and not ac.gnd else
                                                    ("on the ground" if ac.gnd else None)) if x)
            ent["role"] = role
            aircraft.append(ent)
        aircraft.sort(key=lambda e: (not R.counted(e["role"]), e["title"]))
        out = dict(self.region_brief(st))
        out["this_hour"] = act.this_hour(now)[0].get(rid, st["this_hour"])
        out.update({"evidence": st["evidence"], "drivers": st.get("drivers"), "roles": st["roles"],
                    "checks": st["checks"], "confidence_reasons": st["confidence_reasons"],
                    "profile_text": st["profile_text"], "z": _r(st["z"]), "surprise": _r(st["surprise"]),
                    "method_used": st["method"], "value": st["value"], "usual": _r(st["usual"]),
                    "window": st["window"], "innocent": st["innocent"], "limits": st["limits"],
                    "method": METHOD["military_surge"], "series": series, "series_fields": ["t", "n", "coverage", "usual"],
                    "aircraft": aircraft[:60], "aircraft_note": (
                        # Shown on the card only when the list is empty, so it says so (the description alone
                        # read as if aircraft were listed).
                        ("Military aircraft in the region now, from the latest worldwide list" if aircraft
                         else "None in the latest worldwide list")
                        + ("" if act.mil_sampler else " (the background sampler is off, so this is only current while "
                                                      "the military layer is on)") + "."),
                    "geometry": reg.geometry(), "episodes": self.episodes(days=30, category="military_surge", ref=rid,
                                                                          limit=20)})
        return out

    def airport_detail(self, ident: str) -> dict:
        self.ensure()
        act = self.act
        ident = ident.upper()
        st = act.airport_states.get(ident)
        if st is None:
            return {"ident": ident, "monitored": False, "state": "not_monitored",
                    "status": "Meridian does not count movements at this airport",
                    "note": ("Movements are counted at about 50 major airports and at airports you favourite (up to "
                             "6). Favourite this airport to start; a baseline needs 7 days."),
                    "method": METHOD["airport_drop"]}
        now = act.clock()
        end = int(now // HOUR) * HOUR
        since_h = end // HOUR - 48 - 28 * 24          # the same-weekday baseline reads 4 weeks back
        rows = {int(r["hour"]) * HOUR: r for r in act.store.query(
            "SELECT hour, mask, arr, dep, gnd, high FROM flights_apt_hour WHERE ident = ? AND hour >= ?", (ident, since_h))}
        moves = {hs: int(r["arr"]) + int(r["dep"]) for hs, r in rows.items()}
        up = {hs: R.popcount(r["mask"]) / R.SLOTS_PER_HOUR for hs, r in rows.items()}
        series = []
        for k in range(48, -1, -1):
            hs = end - k * HOUR
            r = rows.get(hs)
            # the detector's own usual for this hour (same profile, None while learning)
            usual = R.airport_usual(moves, hs + HOUR, uptime=up)
            if r is None:
                series.append([hs, None, None, None, None, 0.0, usual])
            else:
                series.append([hs, int(r["arr"]), int(r["dep"]), int(r["gnd"]), int(r["high"]),
                               round(R.popcount(r["mask"]) / R.SLOTS_PER_HOUR, 2), usual])
        out = dict(self.airport_brief(st))
        out["this_hour"] = act.this_hour(now)[1].get(ident, st["this_hour"])
        out.update({"monitored": True, "radius_km": st["radius_km"], "evidence": st["evidence"],
                    "hours": [{k: (_r(v, 3) if isinstance(v, float) else v) for k, v in h.items()} for h in st["hours"]],
                    "innocent": st["innocent"], "limits": st["limits"], "method": METHOD["airport_drop"],
                    "faa": st.get("faa"), "series": series,
                    "series_fields": ["t", "arrivals", "departures", "on_ground", "overflights", "coverage", "usual"],
                    "coverage_note": coverage_note(st),
                    "episodes": self.episodes(days=30, category="airport_drop", ref=ident, limit=20)})
        return out

    def episode(self, eid: str) -> dict:
        self.ensure()
        act = self.act
        row = act.store.one("SELECT * FROM flights_activity WHERE id = ?", (eid,))
        if row is None:
            raise KeyError(eid)
        ev = act.episode_event(row)
        data = R_json(row["data"])
        now = act.clock()
        ev["active"] = now - row["last_t"] <= DETECT_EVERY_S * 3
        ev["data"] = {k: v for k, v in data.items() if k != "cells"}
        if row["category"] == "gps_interference":
            cells = [tuple(c) for c in data.get("cells") or []]
            ev["cells"] = []
            for r, c in cells[:80]:
                t = act.gps_trend_of(r, c) or {}
                lat0, lon0 = -90.0 + r * GPS_CELL_DEG, -180.0 + c * GPS_CELL_DEG
                ev["cells"].append({"lat0": lat0, "lon0": lon0, "size": GPS_CELL_DEG, "label": t.get("label"),
                                    "share_now": _r(t.get("share_now"), 4), "share_base": _r(t.get("share_base"), 4)})
            ev["days"] = self._cluster_days(cells, now)
            ev["method"] = METHOD["gps_interference"]
            ev["innocent"] = list(R.GPS_INNOCENT)
            ev["limits"] = R.GPS_LIMITS
        elif row["category"] == "military_surge":
            st = act.region_states.get(row["ref"])
            ev["region"] = self.region_brief(st) if st else None
            ev["method"] = METHOD["military_surge"]
            ev["innocent"] = list(R.MIL_INNOCENT)
            ev["limits"] = R.MIL_LIMITS
        else:
            st = act.airport_states.get(row["ref"])
            ev["airport"] = self.airport_brief(st) if st else None
            ev["method"] = METHOD["airport_drop"]
            ev["innocent"] = list(R.APT_INNOCENT)
            ev["limits"] = R.APT_LIMITS
        return ev

    def _cluster_days(self, cells: list[tuple[int, int]], now: float) -> list[list]:
        if not cells:
            return []
        h_now = int(now // HOUR)
        day_now = h_now // 24
        rows = [r for r, _ in cells]
        cols = [c for _, c in cells]
        want = set(cells)
        days = {d: [0, 0] for d in range(day_now - 6, day_now + 1)}
        for r in self.act.store.query(
                "SELECT hour, row, col, total, bad FROM flights_gpsjam WHERE hour >= ? AND row BETWEEN ? AND ? "
                "AND col BETWEEN ? AND ?", ((day_now - 6) * 24, min(rows), max(rows), min(cols), max(cols))):
            if (r["row"], r["col"]) not in want:
                continue
            d = int(r["hour"]) // 24
            if d in days:
                days[d][0] += int(r["total"])
                days[d][1] += int(r["bad"])
        return [[d * 86400, v[0], v[1]] for d, v in sorted(days.items())]

    # -- map --------------------------------------------------------------------------------------
    def map_fc(self) -> dict:
        self.ensure()
        act = self.act
        feats = []
        for s in act.region_states.values():
            reg = R.REGION_BY_ID[s["id"]]
            lvl = LEVEL.get(s["state"], 0)
            feats.append({"type": "Feature", "geometry": reg.geometry(),
                          "properties": {"kind": "region", "id": s["id"], "name": s["name"], "state": s["state"],
                                         "level": lvl, "status": s["status"]}})
            text = R.region_label_text(s, lvl)       # led by the count an hour; a multiple only of a real usual
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": s["label"]},
                          "properties": {"kind": "region_label", "id": s["id"], "text": text, "level": lvl}})
        for c in act.gps_cluster_list:
            polys = []
            for r, col in c["cells"][:200]:
                lat0, lon0 = -90.0 + r * GPS_CELL_DEG, -180.0 + col * GPS_CELL_DEG
                polys.append([[[lon0, lat0], [lon0 + GPS_CELL_DEG, lat0], [lon0 + GPS_CELL_DEG, lat0 + GPS_CELL_DEG],
                               [lon0, lat0 + GPS_CELL_DEG], [lon0, lat0]]])
            title, sub = R.gps_cluster_words(c)
            feats.append({"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": polys},
                          "properties": {"kind": "gps", "id": c.get("episode") or "", "change": c["change"],
                                         "level": 3, "title": title, "share": round(c["share_now"], 3),
                                         "lat": c["lat"], "lon": c["lon"]}})
        for s in act.airport_states.values():
            lvl = LEVEL.get(s["state"], 0)
            if lvl < 2 and not (s["background"] or s["hours_seen"]):
                continue
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                          "properties": {"kind": "airport", "id": s["ident"], "name": s["name"], "state": s["state"],
                                         "level": lvl, "status": s["status"],
                                         "label": s["ident"] + (f" {R.num_text((s['share'] or 0) * 100, 0)}%"
                                                                if lvl >= 2 and s["share"] is not None else "")}})
        return {"type": "FeatureCollection", "features": feats,
                "meta": {"flags": act.flags(), "updated": round(act.detect_t, 1) if act.detect_t else None}}

    # -- AI ---------------------------------------------------------------------------------------
    def ai_activity(self, args: dict) -> dict:
        self.ensure()
        act = self.act
        region_q = args.get("region")
        if region_q is not None and (not isinstance(region_q, str) or not 2 <= len(region_q.strip()) <= 60):
            raise ValueError("region must be a region id or name, e.g. baltic or Korean peninsula")
        box = None
        if args.get("bbox") is not None or args.get("lat") is not None or args.get("lon") is not None:
            lat, lon, radius = _area(args)
            box = geo.bbox_of_circle(lat, lon, radius) if args.get("bbox") is None else _bbox(args["bbox"])
        cats = args.get("categories")
        if cats is not None:
            if not isinstance(cats, list) or any(c not in CATEGORY_LABELS for c in cats):
                raise ValueError(f"categories must be a list of {sorted(CATEGORY_LABELS)}")
        cats = set(cats or CATEGORY_LABELS)
        regions = list(act.region_states.values())
        if region_q:
            q = region_q.strip().lower().replace(" ", "_")
            exact = [s for s in regions if s["id"] == q]
            regions = exact or [s for s in regions if region_q.strip().lower() in s["name"].lower()
                                or q in s["id"]]
            if not regions:
                return {"found": False, "regions_known": [{"id": s["id"], "name": s["name"]} for s in act.region_states.values()],
                        "note": "No watched region matches. Use one of regions_known, or give a bbox."}
        if box is not None:
            regions = [s for s in regions if _boxes_meet(s["bbox"], box)]
        out: dict[str, Any] = {"updated_utc": _iso(act.detect_t), "caveats": list(CAVEATS)}
        if "military_surge" in cats:
            out["military"] = {
                "regions": [dict(self.region_brief(s), evidence=s["evidence"], drivers=s.get("drivers"),
                                 confidence_reasons=s["confidence_reasons"]) for s in
                            sorted(regions, key=lambda s: (ORDER.get(s["state"], 9), s["name"]))],
                "method": METHOD["military_surge"], "why_innocent": list(R.MIL_INNOCENT)}
        # GPS cells, airports and episodes: in the area given, else in the boxes of every matched region
        # (a name such as "gulf" can match several), else worldwide; "scope" says which
        if box is not None:
            areas = [box]
            out["scope"] = {"by": "area", "bbox": [round(v, 4) for v in box],
                            "note": "GPS interference, airports and recent episodes are limited to the area given."}
        elif region_q and regions:
            areas = [tuple(s["bbox"]) for s in regions]
            names = [s["name"] for s in regions]
            out["scope"] = {"by": "regions", "regions": [s["id"] for s in regions],
                            "note": ("GPS interference, airports and recent episodes are limited to the boxes around "
                                     + (names[0] if len(names) == 1 else
                                        f"the {len(names)} regions matching '{region_q.strip()}': " + "; ".join(names))
                                     + ".")}
        else:
            areas = None
            out["scope"] = {"by": "worldwide", "note": "Nothing was filtered by place."}

        def inside(lat, lon) -> bool:
            return areas is None or any(geo.in_bbox(lat, lon, a) for a in areas)

        if "gps_interference" in cats:
            clusters = [c for c in act.gps_cluster_list if inside(c["lat"], c["lon"])]
            out["gps"] = {"new_or_worse": [self.cluster_brief(c) for c in clusters[:20]],
                          "counts_worldwide": dict(act.gps_counts), "method": METHOD["gps_interference"]}
        if "airport_drop" in cats:
            apts = [s for s in act.airport_states.values() if inside(s["lat"], s["lon"])]
            apts = [s for s in apts if s["state"] not in ("learning", "no_coverage") or s["background"]]
            apts.sort(key=lambda s: (ORDER.get(s["state"], 9), s["name"]))
            out["airports"] = {"items": [dict(self.airport_brief(s), evidence=s["evidence"]) for s in apts[:30]],
                               "method": METHOD["airport_drop"]}
        recent = [e for e in self.episodes(days=7.0, limit=60) if e["category"] in cats and inside(e["lat"], e["lon"])]
        out["recent_episodes"] = recent[:30]
        return out

    def ai_airport(self, args: dict) -> dict:
        ident = args.get("ident")
        if not isinstance(ident, str) or not _IDENT.match(ident.strip()):
            raise ValueError("ident must be an ICAO airport code such as EFHK or KJFK")
        d = self.airport_detail(ident.strip())
        if not d.get("monitored"):
            return d
        keep = ("ident", "iata", "name", "state", "status", "severity", "confidence", "value", "usual", "share",
                "coverage", "days", "needed", "receivers", "background", "watched", "evidence", "this_hour",
                "faa", "coverage_note", "innocent", "method", "episodes")
        out = {k: d.get(k) for k in keep}
        out["last_24h"] = [{"t_utc": _iso(r[0]), "arrivals": r[1], "departures": r[2], "on_ground": r[3],
                            "overflights": r[4], "coverage": r[5], "usual": r[6]} for r in d["series"][-24:]]
        out["updated_utc"] = _iso(self.act.detect_t)
        return out


def R_json(text: str | None) -> dict:
    import json
    try:
        v = json.loads(text) if text else {}
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def region_entity(s: dict) -> dict:
    return {"kind": "air_activity", "id": s.get("episode") or f"flights:surge:{s['id']}",
            "title": f"Military activity: {s['name']}", "subtitle": s["status"], "lat": s["label"][1],
            "lon": s["label"][0], "layer": LAYER, "bbox": s["bbox"], "zoom": 5}


def airport_entity(s: dict) -> dict:
    return {"kind": "air_activity", "id": s.get("episode") or f"flights:airport:{s['ident']}",
            "title": f"Airport movements: {s['name']} ({s['ident']})", "subtitle": s["status"], "lat": s["lat"],
            "lon": s["lon"], "layer": LAYER, "zoom": 9}


def coverage_note(s: dict) -> str:
    if s["background"]:
        how = ("sampled every 10 minutes in the background" + (" (a favourite)" if s["watched"] else
                                                               " inside a GPS-sampler circle"))
    else:
        how = "counted only while you view the area (favourite it for background sampling)"
    hours = s.get("hours_seen") or 0
    return f"This airport is {how}; {hours} well-covered hours in the last 4 weeks."


def _iso(t) -> str | None:
    if not t:
        return None
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(t))


def _bbox(v) -> tuple:
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise ValueError("bbox must be [west, south, east, north]")
    return geo.parse_bbox(",".join(str(float(x)) for x in v))


def _boxes_meet(a, b) -> bool:
    aw, as_, ae, an = a
    bw, bs, be, bn = b
    if an < bs or as_ > bn:
        return False
    if bw > be:                                     # b crosses the antimeridian
        return ae >= bw or aw <= be
    return not (ae < bw or aw > be)


def register_activity(m, act: ActivityService) -> ActivityApi:
    api = ActivityApi(act)
    m.flights.activity_api = api

    @m.route("GET", "/api/flights/activity", activity=False)
    def activity_summary(req):
        return api.summary()

    @m.route("GET", "/api/flights/activity/map", activity=False)
    def activity_map(req):
        return api.map_fc()

    @m.route("GET", "/api/flights/activity/region/{rid}", activity=False)
    def activity_region(req):
        rid = req.params["rid"]
        if not _RID.match(rid):
            raise HTTPError(400, "BAD_ARG", "A region id is a short lower-case name, like baltic.")
        try:
            return api.region_detail(rid)
        except KeyError:
            raise HTTPError(404, "NOT_FOUND", "There is no watched region with that id.") from None

    @m.route("GET", "/api/flights/activity/episode", activity=False)
    def activity_episode(req):
        eid = req.arg("id", "")
        if not _EID.match(eid or ""):
            raise HTTPError(400, "BAD_ARG", "An air activity id looks like flights:surge:baltic:1795400000.")
        try:
            return api.episode(eid)
        except KeyError:
            raise HTTPError(404, "NOT_FOUND", "No air activity episode with that id (episodes are kept 30 days).") from None

    @m.route("GET", "/api/flights/airport/{ident}/movements", activity=False)
    def airport_movements(req):
        ident = req.params["ident"]
        if not _IDENT.match(ident):
            raise HTTPError(400, "BAD_ARG", "The airport id should be an ICAO code, like EFHK.")
        return api.airport_detail(ident)

    m.ai_tool("air_activity",
              "Unusual air activity from Meridian's own baselines: military aircraft surges by region (Baltic, Black "
              "Sea, eastern Mediterranean, Gulf, Taiwan Strait, Korean peninsula and more) with the multiple of usual "
              "and confidence, new or worsening GPS interference cells, and airport movement drops. Filter by region "
              "(id or name), an area (bbox, or lat/lon with radius_km) or categories. Results are candidates with "
              "evidence and innocent explanations, not findings; 'learning' means the baseline is not ready.",
              {"type": "object", "properties": {
                  "region": {"type": "string", "maxLength": 60, "description": "Region id or name, e.g. baltic"},
                  "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                           "description": "[west, south, east, north]"},
                  "lat": {"type": "number"}, "lon": {"type": "number"},
                  "radius_km": {"type": "number", "minimum": 1, "maximum": 500},
                  "categories": {"type": "array", "items": {"type": "string", "enum": sorted(CATEGORY_LABELS)}}},
               "additionalProperties": False},
              api.ai_activity)

    m.ai_tool("airport_movements",
              "Hourly arrivals and departures counted from ADS-B at one airport (ICAO code) for the last 24 hours, "
              "against the usual for each hour, with coverage, overflight check, any unusual drop, and FAA status "
              "for US airports. Covers about 50 major airports and favourited ones.",
              {"type": "object", "properties": {"ident": {"type": "string", "maxLength": 12,
                                                          "description": "ICAO code, e.g. EFHK"}},
               "required": ["ident"], "additionalProperties": False},
              api.ai_airport)
    return api
