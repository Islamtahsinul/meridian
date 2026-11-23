"""Derived views over the vessel store: dark ships, port activity, strait status, busiest ports.

Dark-ship heuristic (also in docs/modules/ships.md):
  A vessel counts as "dark" when all of these hold:
  1. it is a ship station that AIS rules oblige to transmit (class A, not sailing or pleasure craft);
  2. it was under way (at least 3 kn, not anchored, moored or aground) at its last report;
  3. it was reporting regularly: at least 6 reports in the 30 minutes before it went quiet;
  4. nothing has been heard from it for more than 2 hours (and at most 24 hours);
  5. its last source has been receiving continuously since before that last report;
  6. coverage is proven: at least 3 other vessels within 30 km of its last position, and 2 within
     the uncertainty circle around its estimated position, were heard in the last 20 minutes.
  The estimated position is dead reckoning along the last course at the last speed, for at most
  3 hours; the uncertainty radius grows with time (2 km + 35% of the distance it could have run).
"""

from __future__ import annotations

import statistics
import time

from meridian import geo

from .classify import CLASS_IDS, CLASS_LABEL, classify, type_label
from .mid import ISO_NAMES, flag_of
from .ports import SIZE_RANK, port_radius_km
from .store import (R_CLS, R_FLAGS, R_LAT, R_LEN, R_LON, R_MMSI, R_MOVING, R_NAME, R_NAV, R_SOG, R_SRC, R_T,
                    R_TYPE, F_SANCTIONED, SRC_LABELS, Snapshot, VesselStore)

DARK_AFTER_S = 2 * 3600
DARK_MAX_S = 24 * 3600
DR_CAP_S = 3 * 3600
DARK_MIN_SOG = 3.0
DARK_MIN_REPORTS = 6
NEIGHBOUR_KM = 30.0
NEIGHBOUR_FRESH_S = 20 * 60
DARK_EXCLUDED_CLASSES = ("pleasure",)


def _fresh_neighbours(snapshot: Snapshot, lat: float, lon: float, km: float, now: float, exclude: int) -> int:
    n = 0
    for _, row in snapshot.grid.near(lat, lon, km):
        if row[R_MMSI] != exclude and now - row[R_T] <= NEIGHBOUR_FRESH_S:
            n += 1
    return n


def find_dark(store: VesselStore, snapshot: Snapshot, now: float, receiving: dict[int, tuple[float | None, float]]) -> list[dict]:
    """``receiving`` maps source id -> (receiving_since, last_ok) for each provider."""
    candidates = []
    with store.lock:
        for v in store.vessels.values():
            if v.lat is None or not v.t:
                continue
            silent = now - v.t
            if silent <= DARK_AFTER_S or silent > DARK_MAX_S:
                if v.dark_since is not None and silent <= DARK_AFTER_S:
                    v.dark_since = None
                continue
            if v.ais == "B" or v.cls in DARK_EXCLUDED_CLASSES:
                continue
            if v.cls == "other" and not v.imo:
                continue
            if v.sog is None or v.sog < DARK_MIN_SOG or v.nav in (1, 5, 6):
                continue
            if v.regular() < DARK_MIN_REPORTS:
                continue
            if v.t - v.suspect_t <= 2 * 3600:
                continue                # corrupted fixes before the silence: not a reliable track
            if v.reject_t and v.reject_t > v.t:
                continue                # still transmitting, but its positions jump impossibly (GNSS trouble)
            since, last_ok = receiving.get(v.src, (None, 0.0))
            if since is None or since > v.t - 600 or now - last_ok > 900:
                continue
            candidates.append((v.mmsi, v.name, v.cls, v.ship_type, v.length, v.lat, v.lon, v.sog, v.cog, v.hdg, v.t,
                               v.src, v.sanctioned, v.imo, v.dest))
    out = []
    for (mmsi, name, cls, ship_type, length, lat, lon, sog, cog, hdg, t, src, sanctioned, imo, dest) in candidates:
        n_last = _fresh_neighbours(snapshot, lat, lon, NEIGHBOUR_KM, now, mmsi)
        if n_last < 3:
            continue
        silent = now - t
        course = cog if cog is not None else hdg
        run_h = min(silent, DR_CAP_S) / 3600.0
        dist = (sog * geo.KM_PER_NM * run_h) if course is not None else 0.0
        est_lat, est_lon = geo.destination(lat, lon, course, dist) if course is not None else (lat, lon)
        radius = min(150.0, 2.0 + 0.35 * sog * geo.KM_PER_NM * silent / 3600.0)
        n_est = _fresh_neighbours(snapshot, est_lat, est_lon, max(NEIGHBOUR_KM, radius), now, mmsi)
        if n_est < 2:
            continue                    # it has probably sailed out of coverage
        severity = 4
        if cls == "tanker" or (length or 0) >= 150:
            severity += 1
        if silent >= 6 * 3600 or sanctioned:
            severity += 1
        severity = min(severity, 6)
        flag = flag_of(mmsi)
        out.append({
            "mmsi": str(mmsi), "name": name or "", "cls": cls, "type": ship_type, "imo": imo, "dest": dest,
            "flag": flag[0] if flag else None, "flag_name": flag[1] if flag else None,
            "last": {"lat": round(lat, 5), "lon": round(lon, 5), "t": t, "sog": sog, "cog": cog},
            "est": {"lat": round(est_lat, 5), "lon": round(est_lon, 5)},
            "radius_km": round(radius, 1), "silent_s": int(silent), "capped": silent > DR_CAP_S,
            "neighbours_last": n_last, "neighbours_est": n_est, "severity": severity, "sanctioned": sanctioned,
            "source": SRC_LABELS.get(src, "unknown"),
        })
    with store.lock:
        for d in out:
            v = store.vessels.get(int(d["mmsi"]))
            if v is not None and v.dark_since is None:
                v.dark_since = v.t
                # The coverage proof at the moment the silence was flagged, kept for the gap episode.
                v.gap_ctx = {"n_last": d["neighbours_last"], "n_est": d["neighbours_est"], "flag_t": now}
    out.sort(key=lambda d: (-d["severity"], -d["silent_s"]))
    return out


# GNSS spoofing hotspots moved to anomalies.py (gnss_signatures), which adds jump, circle and land signatures.


# ---------------------------------------------------------------------------------------------
# Port activity (CONTRACT 9.1)
# ---------------------------------------------------------------------------------------------
def coverage_text(n_heard: int, sources: list[str], has_global: bool) -> str:
    if n_heard:
        src = f" from {' and '.join(sources)}" if sources else ""
        return f"Live AIS{src}: {n_heard:,} vessel{'s' if n_heard != 1 else ''} heard nearby in the last hour."
    if has_global:
        return "No AIS reports near here in the last hour, so counts may be incomplete."
    return ("No AIS coverage here. Meridian receives Digitraffic (Finnish waters and the northern Baltic) "
            "without a key; add an aisstream.io key for global coverage.")


def coverage_near(snapshot: Snapshot, lat: float, lon: float, km: float, now: float) -> tuple[int, list[str]]:
    heard = 0
    srcs: set[int] = set()
    for _, row in snapshot.grid.near(lat, lon, km):
        if now - row[R_T] <= 3600:
            heard += 1
            srcs.add(row[R_SRC])
    return heard, [SRC_LABELS[s] for s in sorted(srcs) if s in SRC_LABELS]


def short_type(cls: str, ship_type: int | None) -> str:
    """A short type for subtitles: the class name, or the specific AIS type for service craft and
    other vessels ("Pilot vessel", "Tug", "Dredger or underwater operations")."""
    if cls in ("service", "other"):
        label = type_label(ship_type)
        if label not in ("Type not reported", "Other type") and not label.startswith(("Reserved", "Type ")):
            return label
        return "Service vessel" if cls == "service" else "Vessel"
    return CLASS_LABEL.get(cls, "Vessel")


def vessel_entity(row, *, distance_km: float | None = None, state: str | None = None) -> dict:
    mmsi = row[R_MMSI]
    cls = CLASS_IDS[row[R_CLS]] if row[R_CLS] < len(CLASS_IDS) else "other"
    flag = flag_of(mmsi)
    parts = [short_type(cls, row[R_TYPE])]
    if flag:
        parts.append(flag[1])
    if state:
        parts.append(state)
    elif row[R_MOVING] and row[R_SOG] is not None:
        parts.append(f"{row[R_SOG] / 10:.1f} kn")
    ent = {"kind": "ship", "id": str(mmsi), "title": row[R_NAME] or f"MMSI {mmsi}", "subtitle": ", ".join(parts),
           "lat": round(row[R_LAT], 5), "lon": round(row[R_LON], 5), "layer": "ships", "time": int(row[R_T]),
           "zoom": 12, "cls": cls}
    if row[R_FLAGS] & F_SANCTIONED:
        ent["sanctioned"] = True
    if distance_km is not None:
        ent["distance_km"] = round(distance_km, 2)
    return ent


def _row_state(row) -> str:
    if row[R_MOVING]:
        return "underway"
    return "anchored" if row[R_NAV] == 1 else "moored"


def port_activity(store: VesselStore, snapshot: Snapshot, lat: float, lon: float, radius_km: float, now: float,
                  *, has_global: bool = False, limit: int = 20) -> dict:
    counts = {"moored": 0, "anchored": 0, "underway": 0}
    found = []
    for d, row in snapshot.grid.near(lat, lon, radius_km):
        state = _row_state(row)
        counts[state] += 1
        found.append((d, row, state))
    since = now - 86400
    arrivals: set[int] = set()
    departures: set[int] = set()
    w, s, e, n = geo.bbox_of_circle(lat, lon, radius_km)
    for c in store.recent_calls(since):
        if not (s <= c.lat <= n) or not geo.in_bbox(c.lat, c.lon, (w, s, e, n)):
            continue
        if geo.haversine_km(lat, lon, c.lat, c.lon) > radius_km:
            continue
        if c.dep_t and c.dep_t >= since:
            departures.add(c.mmsi)
        if c.seen and c.arr_t >= since:
            arrivals.add(c.mmsi)
    with store.lock:
        for _, row, state in found:
            if state == "underway":
                continue
            v = store.vessels.get(row[R_MMSI])
            if v is not None and v.state == "stationary" and v.stop_seen and v.state_since >= since \
                    and now - v.state_since >= 20 * 60:
                arrivals.add(v.mmsi)

    def rank(item):
        d, row, state = item
        size = row[R_LEN] or 0
        return (0 if row[R_FLAGS] & F_SANCTIONED else 1, 0 if state != "underway" else 1, -size, d)

    found.sort(key=rank)
    vessels = [vessel_entity(row, distance_km=d, state={"underway": "under way", "anchored": "at anchor",
                                                         "moored": "moored"}[state]) | {"state": state}
               for d, row, state in found[:limit]]
    heard, sources = coverage_near(snapshot, lat, lon, radius_km + 20.0, now)
    by_class: dict[str, int] = {}
    for _, row, _ in found:
        cls = CLASS_IDS[row[R_CLS]]
        by_class[cls] = by_class.get(cls, 0) + 1
    return {
        "lat": lat, "lon": lon, "radius_km": radius_km,
        "moored": counts["moored"], "anchored": counts["anchored"], "underway": counts["underway"],
        "arrivals_24h": len(arrivals), "departures_24h": len(departures),
        "total": len(found), "by_class": by_class,
        "vessels": vessels,
        "coverage": coverage_text(heard, sources, has_global),
        "coverage_ok": heard > 0, "sources": sources, "heard_1h": heard,
        "note": "Arrivals and departures are stops of 20 minutes or more seen by Meridian in the last 24 hours.",
    }


# ---------------------------------------------------------------------------------------------
# Straits
# ---------------------------------------------------------------------------------------------
def strait_status(store: VesselStore, snapshot: Snapshot, now: float, baselines: dict[str, float | None],
                  *, has_global: bool, with_geometry: bool = True, sid: str | None = None) -> list[dict]:
    mon = store.straits
    out = []
    with store.lock:
        for d in mon.defs:
            if sid and d["id"] != sid:
                continue
            inside = mon.inside(d["id"], now)
            moving = 0
            inside_cls: dict[str, int] = {}
            inside_list = []
            for visit in inside:
                v = store.vessels.get(visit.mmsi)
                if v is None:
                    continue
                inside_cls[v.cls] = inside_cls.get(v.cls, 0) + 1
                if v.moving:
                    moving += 1
                inside_list.append((v.mmsi, v.name, v.cls, v.lat, v.lon, v.sog, v.t, v.moving, v.length or 0, v.ship_type))
            st = mon.stats(d["id"], now)
            w, s, e, n = d["bbox"]
            heard = 0
            srcs: set[int] = set()
            for row in snapshot.grid.in_bbox((w - 0.5, max(-90, s - 0.5), e + 0.5, min(90, n + 0.5))):
                if now - row[R_T] <= 3600:
                    heard += 1
                    srcs.add(row[R_SRC])
            base = baselines.get(d["id"])
            congestion = None
            if base is not None and base > 0 and len(inside) >= 8 and len(inside) >= 1.5 * base:
                congestion = round(len(inside) / base, 1)
            item = {
                "id": d["id"], "name": d["name"], "axis": d["axis"], "pos_label": d["pos"], "neg_label": d["neg"],
                "label": d["label"], "bbox": [round(x, 4) for x in d["bbox"]],
                "inside_now": len(inside), "inside_moving": moving, "inside_stationary": len(inside) - moving,
                "inside_by_class": inside_cls,
                "coverage": heard > 0, "heard_1h": heard,
                "sources": [SRC_LABELS[x] for x in sorted(srcs) if x in SRC_LABELS],
                "baseline_median": base, "congestion": congestion,
                **st,
            }
            if not heard:
                item["coverage_note"] = ("No AIS reports from this area in the last hour. "
                                         + ("Digitraffic only covers Finnish waters and the northern Baltic; "
                                            "add an aisstream.io key for global coverage." if not has_global
                                            else "Community receivers may not cover it right now."))
            if with_geometry:
                item["geometry"] = d["geometry"]
            if sid:
                inside_list.sort(key=lambda x: (not x[7], -x[8]))
                flag = None
                item["vessels"] = []
                for (mmsi, name, cls, lat, lon, sog, t, mv, _, stype) in inside_list[:40]:
                    flag = flag_of(mmsi)
                    item["vessels"].append({
                        "kind": "ship", "id": str(mmsi), "title": name or f"MMSI {mmsi}",
                        "subtitle": ", ".join(x for x in [short_type(cls, stype), flag[1] if flag else None,
                                                          f"{sog:.1f} kn" if mv and sog is not None else "stationary"] if x),
                        "lat": round(lat, 5), "lon": round(lon, 5), "layer": "ships", "time": int(t), "cls": cls})
            out.append(item)
    return out


def baseline(samples: list[int]) -> float | None:
    """Median of hourly "inside now" samples over the last week (None until a day of samples)."""
    if len(samples) < 24:
        return None
    return float(statistics.median(samples))


# ---------------------------------------------------------------------------------------------
# Busiest ports
# ---------------------------------------------------------------------------------------------
def busiest_ports(store: VesselStore, snapshot: Snapshot, ports: list[dict], now: float, limit: int = 25) -> list[dict]:
    """Ports ranked by vessels in port plus arrivals in 24 h. Each vessel and each stop counts for one
    port only: bigger ports claim first, so a city port absorbs its own sub-harbours."""
    if not snapshot.rows:
        return []
    occupied = {(int(r[R_LAT] // 1), int(r[R_LON] // 1)) for r in snapshot.rows}
    candidates = []
    for p in ports:
        lat, lon = p["lat"], p["lon"]
        cell = (int(lat // 1), int(lon // 1))
        if not any((cell[0] + dy, cell[1] + dx) in occupied for dy in (-1, 0, 1) for dx in (-1, 0, 1)):
            continue
        r = port_radius_km(p.get("size"))
        near = snapshot.grid.near(lat, lon, r)
        if near:
            candidates.append((p, r, near))
    candidates.sort(key=lambda c: (-SIZE_RANK.get(c[0].get("size"), 0), -len(c[2])))
    calls = store.recent_calls(now - 86400)
    claimed: set[int] = set()
    claimed_calls: set[int] = set()
    out = []
    for p, r, near in candidates:
        mine = [row for _, row in near if row[R_MMSI] not in claimed]
        if not mine:
            continue
        claimed.update(row[R_MMSI] for row in mine)
        states = [_row_state(row) for row in mine]
        lat, lon = p["lat"], p["lon"]
        arr = dep = 0
        for i, c in enumerate(calls):
            if i in claimed_calls or abs(c.lat - lat) > 0.3 or geo.haversine_km(lat, lon, c.lat, c.lon) > r:
                continue
            claimed_calls.add(i)
            arr += 1 if c.seen and c.arr_t >= now - 86400 else 0
            dep += 1 if c.dep_t and c.dep_t >= now - 86400 else 0
        moored, anchored = states.count("moored"), states.count("anchored")
        out.append({"id": p["id"], "name": p["name"], "country": p.get("country"),
                    "country_name": ISO_NAMES.get((p.get("country") or "").upper()), "lat": lat, "lon": lon,
                    "size": p.get("size"), "radius_km": r, "moored": moored, "anchored": anchored,
                    "underway": states.count("underway"), "in_port": moored + anchored, "arrivals_24h": arr,
                    "departures_24h": dep, "source": p.get("source")})
    out.sort(key=lambda x: (-(x["in_port"] + x["arrivals_24h"]), -x["underway"]))
    return out[:limit]


def class_of(ship_type) -> str:
    return classify(ship_type)
