"""ShipsService: the module's state and every read the routes, search, events and AI tools need."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from meridian import geo

from . import analysis
from . import ais as aislib
from .anomalies import KIND_ALIASES, AnomalyEngine, lists_text
from .classify import CLASS_IDS, CLASS_INDEX, CLASS_LABEL, CLASSES, type_label
from .land import LandIndex, countries_path
from .mid import flag_of, station_kind
from .ports import load_ports, ports_path
from .store import (F_SANCTIONED, R_CLS, R_DIR, R_FLAGS, R_LAT, R_LEN, R_LON, R_MMSI, R_MOVING, R_NAME, R_SIZE,
                    R_SOG, R_T, SRC_LABELS, VesselStore)
from .straits import StraitMonitor

MAX_LIVE_ROWS = 6000
THIN_ABOVE = 2500
ANOMALY_NOTE = ("Candidates, not accusations: each says what was seen, how confident the detector is, the receiver "
                "coverage behind it and why it may be innocent. Meridian hears terrestrial AIS only, so coverage is thin "
                "far from shore and missing in parts of the Gulf, the Red Sea and Malacca.")


def links_for(mmsi: int | str, imo: int | None) -> list[dict]:
    out = [{"text": "MarineTraffic", "href": f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}"}]
    out.append({"text": "VesselFinder", "href": f"https://www.vesselfinder.com/vessels/details/{imo or mmsi}"})
    if imo:
        out.append({"text": "MarineTraffic by IMO", "href": f"https://www.marinetraffic.com/en/ais/details/ships/imo:{imo}"})
    return out


def _cell_deg(zoom: float) -> float:
    """About 5 screen pixels at this zoom (MapLibre's world is 512 px wide at zoom 0)."""
    return 3.5 / (2 ** max(0.0, zoom))


def _priority(row) -> tuple:
    return (1 if row[R_FLAGS] & F_SANCTIONED else 0, 1 if row[R_MOVING] else 0, row[R_SIZE], row[R_T])


class ShipsService:
    def __init__(self, *, db=None, data_dir=None, now=time.time, lookup=None, log=None, has_global=lambda: False):
        self.now = now
        self.log = log
        self.store = VesselStore(db, now=now, lookup=lookup, straits=StraitMonitor(), log=log)
        self.has_global = has_global
        self.data_dir = data_dir
        self.dark: list[dict] = []
        self.dark_at = 0.0
        self.straits_cache: list[dict] = []
        self.straits_at = 0.0
        self.baselines: dict[str, float | None] = {}
        self.busiest: list[dict] = []
        self.busiest_at = 0.0
        self.ports, self.ports_source = ([], "builtin")
        self.port_grid = geo.GridIndex(0.5)
        self._ports_mtime = None
        self.reload_ports()
        self.receiving: dict[int, tuple[float | None, float]] = {}
        self.congestion: dict[str, dict] = {}
        self.providers: dict[str, object] = {}
        land = LandIndex(countries_path(data_dir)) if data_dir is not None else None
        self.anomalies = AnomalyEngine(self.store, db=db, ports=lambda: self.port_grid, land=land, now=now, log=log)
        self.portwatch = None          # portwatch.PortWatch, set by register() (it needs the network)
        self._live_cache: OrderedDict = OrderedDict()
        self._live_lock = threading.Lock()

    # ------------------------------------------------------------------------------------------
    # Periodic work
    # ------------------------------------------------------------------------------------------
    def reload_ports(self) -> None:
        """Load the port list, again whenever the infra module rebuilds ports.json."""
        if self.data_dir is None:
            return
        try:
            mtime = ports_path(self.data_dir).stat().st_mtime
        except OSError:
            mtime = None
        if self.ports and mtime == self._ports_mtime:
            return
        ports, source = load_ports(self.data_dir)
        grid = geo.GridIndex(0.5)
        for p in ports:
            grid.add(p["lat"], p["lon"], p)
        self.ports, self.ports_source, self.port_grid, self._ports_mtime = ports, source, grid, mtime
        self.busiest_at = 0.0

    def refresh_snapshot(self):
        snap = self.store.build_snapshot()
        with self._live_lock:
            self._live_cache.clear()
        return snap

    def run_analysis(self, samples_loader=None) -> None:
        now = self.now()
        snap = self.store.snapshot
        with self.store.lock:
            self.store.straits.sweep(now)
        self.dark = analysis.find_dark(self.store, snap, now, self.receiving)
        self.dark_at = now
        if samples_loader is not None:
            for d in self.store.straits.defs:
                try:
                    self.baselines[d["id"]] = analysis.baseline(samples_loader(d["id"]))
                except Exception:
                    self.baselines[d["id"]] = None
        self.straits_cache = analysis.strait_status(self.store, snap, now, self.baselines,
                                                    has_global=self.has_global(), with_geometry=True)
        self.straits_at = now
        for s in self.straits_cache:
            if s.get("congestion"):
                prev = self.congestion.get(s["id"])
                self.congestion[s["id"]] = {"t": prev["t"] if prev else now, "ratio": s["congestion"],
                                            "inside": s["inside_now"], "median": s["baseline_median"]}
            else:
                self.congestion.pop(s["id"], None)
        self.reload_ports()
        if now - self.busiest_at > 300 or not self.busiest:
            self.busiest = analysis.busiest_ports(self.store, snap, self.ports, now)
            self.busiest_at = now
        # Ship-to-ship candidates, loitering, AIS gap episodes and GNSS spoofing clusters (anomalies.py).
        self.anomalies.run(snap, self.receiving, now)

    @property
    def hotspots(self) -> list[dict]:
        """Active GNSS spoofing hotspots (kept 6 hours after vessels last misbehaved there)."""
        return self.anomalies.hotspots()

    # ------------------------------------------------------------------------------------------
    # Live layer
    # ------------------------------------------------------------------------------------------
    def live(self, bbox, zoom: float, classes: set[str] | None = None, moving_only: bool = False) -> dict:
        snap = self.store.snapshot
        key = (snap.seq, tuple(round(x, 2) for x in bbox) if bbox else None, int(zoom),
               tuple(sorted(classes)) if classes else None, moving_only)
        with self._live_lock:
            hit = self._live_cache.get(key)
            if hit is not None:
                self._live_cache.move_to_end(key)
                return hit
        rows = list(snap.grid.in_bbox(bbox)) if bbox else list(snap.rows)
        if classes:
            idx = {CLASS_INDEX[c] for c in classes if c in CLASS_INDEX}
            rows = [r for r in rows if r[R_CLS] in idx]
        if moving_only:
            rows = [r for r in rows if r[R_MOVING]]
        in_view = len(rows)
        counts = [1] * len(rows)
        thinned = False
        if in_view > THIN_ABOVE:
            cell = _cell_deg(zoom)
            while True:
                buckets: dict[tuple[int, int], list] = {}
                for r in rows:
                    k = (int(r[R_LAT] // cell), int(r[R_LON] // cell))
                    b = buckets.get(k)
                    if b is None:
                        buckets[k] = [r, 1]
                    else:
                        b[1] += 1
                        if _priority(r) > _priority(b[0]):
                            b[0] = r
                if len(buckets) <= MAX_LIVE_ROWS:
                    break
                cell *= 1.6
            kept = list(buckets.values())
            thinned = len(kept) < in_view
            rows = [b[0] for b in kept]
            counts = [b[1] for b in kept]
        now = self.now()
        nd = 5 if zoom >= 8 else 4
        out_rows = []
        for r, n in zip(rows, counts):
            out_rows.append([r[R_MMSI], round(r[R_LON], nd), round(r[R_LAT], nd), r[R_DIR], r[R_CLS], r[R_FLAGS],
                             r[R_SOG], r[R_SIZE], r[R_NAME], int(now - r[R_T]), n])
        result = {
            "t": now, "zoom": round(zoom, 2), "in_view": in_view, "shown": len(out_rows), "thinned": thinned,
            "total": len(snap.rows), "tracked": snap.total,
            "fields": ["mmsi", "lon", "lat", "dir", "cls", "flags", "sog10", "size", "name", "age", "n"],
            "classes": CLASS_IDS, "rows": out_rows,
        }
        if not in_view:
            result["note"] = self.coverage_note(bbox)
        with self._live_lock:
            self._live_cache[key] = result
            while len(self._live_cache) > 48:
                self._live_cache.popitem(last=False)
        return result

    def coverage_note(self, bbox=None) -> str:
        if self.has_global():
            return "No vessels reported in this area right now."
        return ("No AIS coverage here. Without a key Meridian receives Digitraffic, which covers Finnish waters "
                "and the northern Baltic; add an aisstream.io key for global coverage.")

    # ------------------------------------------------------------------------------------------
    # Vessel card
    # ------------------------------------------------------------------------------------------
    def vessel(self, mmsi: int) -> dict:
        now = self.now()
        flag = flag_of(mmsi)
        base = {"mmsi": str(mmsi), "flag": flag[0] if flag else None, "flag_name": flag[1] if flag else None,
                "station": station_kind(mmsi), "links": links_for(mmsi, None), "now": now}
        rec = self.store.record(mmsi)
        if rec is None:
            base.update({"known": False, "title": f"MMSI {mmsi}",
                         "note": "No AIS reports from this MMSI in the last 7 days."})
            return base
        moving = rec["moving"]
        has_pos = rec["lat"] is not None
        age = now - rec["t"] if rec["t"] else None
        live = bool(has_pos and age is not None and age <= (3600 if moving else 6 * 3600))
        stale = bool(live and age > (900 if moving else 3600))
        dark = next((d for d in self.dark if d["mmsi"] == str(mmsi)), None)
        sanc = rec.get("sanc")
        strength = None
        if sanc:
            strength = "confirmed" if sanc.get("match") in ("imo", "mmsi") else "possible"
        strait = None
        if rec.get("strait"):
            d = self.store.straits.by_id.get(rec["strait"])
            if d:
                strait = {"id": d["id"], "name": d["name"]}
        calls = []
        for c in self.store.calls_for(mmsi):
            calls.append({"arr_t": c["arr_t"], "dep_t": c["dep_t"], "lat": c["lat"], "lon": c["lon"],
                          "kind": "anchored" if c.get("nav") == 1 else "moored", "arrival_seen": bool(c.get("seen")),
                          "port": self.nearest_port(c["lat"], c["lon"])})
        if rec["state"] == "stationary" and rec["stop_lat"] is not None and rec["stop_seen"] \
                and now - rec["state_since"] >= 20 * 60:
            calls.insert(0, {"arr_t": rec["state_since"], "dep_t": None, "lat": rec["stop_lat"], "lon": rec["stop_lon"],
                             "kind": "anchored" if rec.get("stop_nav") == 1 else "moored",
                             "arrival_seen": bool(rec["stop_seen"]), "port": self.nearest_port(rec["stop_lat"], rec["stop_lon"]),
                             "ongoing": True})
        gaps = [{"t0": g["t0"], "t1": g["t1"], "hours": round((g["t1"] - g["t0"]) / 3600.0, 1),
                 "moved_km": round(geo.haversine_km(g["lat0"], g["lon0"], g["lat1"], g["lon1"]), 1)
                 if g.get("lat0") is not None and g.get("lat1") is not None else None}
                for g in self.store.gaps_for(mmsi)]
        eta_t = aislib.eta_to_epoch(rec.get("eta"), now)
        sources = [SRC_LABELS[s] for s in SRC_LABELS if rec["srcs"] & (1 << s)]
        suspect = None
        if has_pos and rec["suspect_t"] and now - rec["suspect_t"] <= 1800:
            with self.store.lock:
                in_spot = self.store.in_hotspot(rec["lat"], rec["lon"])
            fresh_sog = rec.get("suspect_sog_t") and now - rec["suspect_sog_t"] <= 1800
            suspect = {"t": rec["suspect_t"], "sog": rec["suspect_sog"] if fresh_sog else None, "hotspot": in_spot}
        title = rec["name"] or f"MMSI {mmsi}"
        cls = rec["cls"]
        out = dict(base)
        out.update({
            "known": True, "title": title, "name": rec["name"] or None,
            "imo": rec["imo"], "callsign": rec["callsign"] or None,
            "ship_type": rec["ship_type"], "type_label": type_label(rec["ship_type"]),
            "cls": cls, "cls_label": CLASS_LABEL.get(cls, "Other or unknown"),
            "length": rec["length"], "beam": rec["beam"], "ais_class": rec["ais"],
            "destination": rec["dest"] or None, "eta": rec.get("eta"), "eta_t": eta_t,
            "eta_past": bool(eta_t and eta_t < now - 3600),
            "draught": rec["draught"], "nav_status": rec["nav"],
            "nav_label": aislib.NAV_STATUS.get(rec["nav"]) if rec["nav"] not in (None, 15) else None,
            "position": {"lat": round(rec["lat"], 5), "lon": round(rec["lon"], 5), "t": rec["t"], "age_s": int(age),
                         "sog": rec["sog"], "cog": rec["cog"], "heading": rec["hdg"], "rot": rec["rot"]}
            if has_pos else None,
            "state": rec["port_state"] if has_pos else None,
            "state_since": (rec["state_since"] or None) if (moving or rec["stop_seen"]) else None,
            "moving": moving, "live": live, "stale": stale,
            "source": SRC_LABELS.get(rec["src"]), "sources": sources,
            "first_seen": rec["first_seen"], "static_t": rec["t_static"] or None,
            "strait": strait, "dark": dark, "suspect": suspect,
            "sanctions": sanc, "sanctions_strength": strength,
            "calls": calls[:8], "gaps": gaps,
            "links": links_for(mmsi, rec["imo"]),
            "regular_reports_30min": rec["regular"],
            "anomalies": self.anomalies.for_vessel(mmsi),
        })
        return out

    def nearest_port(self, lat: float | None, lon: float | None) -> str | None:
        if lat is None or lon is None:
            return None
        hits = self.port_grid.near(lat, lon, 15.0, 1)
        return hits[0][1]["name"] if hits else None

    def trail(self, mmsi: int, hours: float) -> dict:
        now = self.now()
        pts = self.store.trail(mmsi, now - hours * 3600)
        coords = [[round(p[2], 5), round(p[1], 5)] for p in pts]
        return {"mmsi": str(mmsi), "hours": hours, "count": len(pts),
                "points": [[round(p[2], 5), round(p[1], 5), p[0], p[3]] for p in pts],
                "geometry": {"type": "LineString", "coordinates": coords} if len(coords) >= 2 else None,
                "from": pts[0][0] if pts else None, "to": pts[-1][0] if pts else None}

    # ------------------------------------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------------------------------------
    def sanctioned(self, include_possible: bool = False) -> list[dict]:
        now = self.now()
        out = []
        with self.store.lock:
            for v in self.store.vessels.values():
                if not v.sanc or (not v.sanctioned and not include_possible):
                    continue
                if v.lat is None:
                    continue
                out.append({"mmsi": str(v.mmsi), "name": v.name or f"MMSI {v.mmsi}", "imo": v.imo,
                            "lat": round(v.lat, 5), "lon": round(v.lon, 5), "t": v.t, "age_s": int(now - v.t),
                            "live": now - v.t <= (3600 if v.moving else 6 * 3600), "cls": v.cls,
                            "match": v.sanc.get("match"), "strength": "confirmed" if v.sanctioned else "possible",
                            "program": v.sanc.get("program"), "programs": list(v.sanc.get("programs") or []),
                            "lists": list(v.sanc.get("lists") or ["OFAC"]), "listed_name": v.sanc.get("name"),
                            "flag": (flag_of(v.mmsi) or (None, None))[1]})
        out.sort(key=lambda d: (d["strength"] != "confirmed", -d["t"]))
        return out

    def status(self) -> dict:
        snap = self.store.snapshot
        st = dict(self.store.stats)
        return {"tracked": len(self.store.vessels), "live": len(snap.rows), "moving": snap.counts.get("moving", 0),
                "stationary": snap.counts.get("stationary", 0), "dark": len(self.dark),
                "gnss_hotspots": [{k: h[k] for k in ("id", "lat", "lon", "radius_km", "vessels", "max_sog", "t", "signatures")}
                                  for h in self.hotspots],
                "anomalies": self.anomalies.status(),
                "sanctioned": sum(1 for d in self.sanctioned() if d["live"]), "snapshot_t": snap.t,
                "stats": st, "has_global": self.has_global(), "ports_source": self.ports_source}

    # ------------------------------------------------------------------------------------------
    # Discovery hooks
    # ------------------------------------------------------------------------------------------
    def search(self, q: str, limit: int) -> list[dict]:
        out = []
        now = self.now()
        for score, v in self.store.search(q, limit):
            flag = flag_of(v.mmsi)
            age = now - v.t if v.t else None
            bits = [analysis.short_type(v.cls, v.ship_type)]
            if flag:
                bits.append(flag[1])
            if v.imo:
                bits.append(f"IMO {v.imo}")
            bits.append(f"MMSI {v.mmsi}")
            if age is not None and age > 3600:
                bits.append(f"last seen {_ago(age)}")
            ent = {"kind": "ship", "id": str(v.mmsi), "title": v.name or f"MMSI {v.mmsi}", "subtitle": ", ".join(bits),
                   "layer": "ships", "score": round(min(1.0, score), 3), "zoom": 11}
            if v.lat is not None:
                ent.update({"lat": round(v.lat, 5), "lon": round(v.lon, 5), "time": int(v.t)})
            out.append(ent)
        return out

    def nearby(self, lat: float, lon: float, radius_km: float, limit: int) -> list[dict]:
        snap = self.store.snapshot
        return [analysis.vessel_entity(row, distance_km=d) for d, row in snap.grid.near(lat, lon, radius_km, limit)]

    def events(self, since: float) -> list[dict]:
        out = []
        for d in self.dark:
            t = d["last"]["t"] + analysis.DARK_AFTER_S
            if t < since:
                continue
            hours = d["silent_s"] / 3600.0
            out.append({"kind": "ship", "id": d["mmsi"], "title": f"Dark ship: {d['name'] or 'MMSI ' + d['mmsi']}",
                        "subtitle": f"{CLASS_LABEL.get(d['cls'], 'Vessel')}, AIS silent for {hours:.1f} h while "
                                    f"{d['neighbours_last']} vessels nearby still report",
                        "lat": d["last"]["lat"], "lon": d["last"]["lon"], "layer": "ships.dark", "time": int(t),
                        "severity": d["severity"], "category": "dark_ship"})
        for s in self.sanctioned():
            if s["t"] < since or not s["live"]:
                continue
            programmes = ", ".join(s.get("programs") or [])
            out.append({"kind": "ship", "id": s["mmsi"], "title": f"Sanctioned vessel seen: {s['name']}",
                        "subtitle": f"On the {lists_text(s.get('lists'))}{f' ({programmes})' if programmes else ''}, "
                                    f"matched by {s['match'].upper()}",
                        "lat": s["lat"], "lon": s["lon"], "layer": "ships.sanctioned", "time": int(s["t"]),
                        "severity": 6, "category": "sanctioned_vessel", "lists": s.get("lists")})
        out.extend(self.anomalies.events(since))
        by_id = {s["id"]: s for s in self.straits_cache}
        for sid, c in self.congestion.items():
            s = by_id.get(sid)
            if not s or c["t"] < since:
                continue
            out.append({"kind": "strait", "id": f"ships:{sid}", "title": f"Unusual congestion: {s['name']}",
                        "subtitle": f"{c['inside']} vessels inside, {c['ratio']}x the usual {c['median']:.0f}",
                        "lat": s["label"][1], "lon": s["label"][0], "layer": "ships.straits", "time": int(c["t"]),
                        "severity": 5 if c["ratio"] >= 2 else 4, "category": "strait_congestion"})
        return out

    # ------------------------------------------------------------------------------------------
    # AI tools
    # ------------------------------------------------------------------------------------------
    def ai_ships_in_area(self, args: dict) -> dict:
        bbox = None
        center = None
        if args.get("bbox") is not None:
            raw = args["bbox"]
            text = ",".join(str(x) for x in raw) if isinstance(raw, (list, tuple)) else str(raw)
            try:
                bbox = geo.parse_bbox(text)
            except ValueError as err:
                return {"error": f"bbox: {err}"}
        elif args.get("lat") is not None and args.get("lon") is not None:
            try:
                lat, lon = float(args["lat"]), float(args["lon"])
                radius = min(500.0, max(0.5, float(args.get("radius_km") or 25)))
            except (TypeError, ValueError):
                return {"error": "lat, lon and radius_km must be numbers"}
            if not geo.valid_latlon(lat, lon):
                return {"error": "lat/lon out of range"}
            center = (lat, lon, radius)
        else:
            return {"error": "Give either bbox [west, south, east, north] or lat, lon and radius_km."}
        limit = max(1, min(50, int(args.get("limit") or 20)))
        want = args.get("type")
        moving_only = bool(args.get("moving"))
        snap = self.store.snapshot
        if center:
            items = [(d, r) for d, r in snap.grid.near(center[0], center[1], center[2])]
        else:
            items = [(None, r) for r in snap.grid.in_bbox(bbox)]
        if want in CLASS_INDEX:
            items = [(d, r) for d, r in items if r[R_CLS] == CLASS_INDEX[want]]
        if moving_only:
            items = [(d, r) for d, r in items if r[R_MOVING]]
        by_class: dict[str, int] = {}
        moving = 0
        for _, r in items:
            by_class[CLASS_IDS[r[R_CLS]]] = by_class.get(CLASS_IDS[r[R_CLS]], 0) + 1
            moving += 1 if r[R_MOVING] else 0
        items.sort(key=lambda it: (0 if it[1][R_FLAGS] & F_SANCTIONED else 1, -(it[1][R_LEN] or 0)))
        vessels = [analysis.vessel_entity(r, distance_km=d) for d, r in items[:limit]]
        note = None if items else self.coverage_note()
        return {"count": len(items), "moving": moving, "stationary": len(items) - moving, "by_class": by_class,
                "vessels": vessels, "note": note}

    def ai_vessel_details(self, args: dict) -> dict:
        mmsi = str(args.get("mmsi") or "").strip()
        imo = str(args.get("imo") or "").strip()
        name = str(args.get("name") or "").strip()
        if mmsi:
            if not (mmsi.isascii() and mmsi.isdigit() and len(mmsi) == 9):
                return {"error": "mmsi must be nine digits"}
            return self._slim_vessel(self.vessel(int(mmsi)))
        q = imo or name
        if not q:
            return {"error": "Give mmsi, imo or name."}
        hits = self.store.search(q, 5)
        if not hits:
            return {"found": 0, "note": "No vessel by that IMO or name in the last 7 days."}
        if len(hits) == 1 or hits[0][0] >= 0.9:
            return self._slim_vessel(self.vessel(hits[0][1].mmsi))
        return {"found": len(hits), "matches": self.search(q, 5)}

    def _slim_vessel(self, d: dict) -> dict:
        keep = ("mmsi", "known", "title", "imo", "callsign", "flag_name", "type_label", "cls_label", "length", "beam",
                "destination", "eta", "draught", "nav_label", "position", "state", "live", "source", "strait",
                "sanctions_strength", "note")
        out = {k: d.get(k) for k in keep if d.get(k) is not None}
        if d.get("sanctions"):
            s = d["sanctions"]
            out["sanctions"] = {k: s.get(k) for k in ("match", "name", "program", "aliases", "linked_to", "imo", "mmsi")}
        if d.get("dark"):
            dk = d["dark"]
            out["dark"] = {"silent_hours": round(dk["silent_s"] / 3600, 1), "estimated": dk["est"],
                           "radius_km": dk["radius_km"]}
        if d.get("calls"):
            out["recent_calls"] = [{"port": c.get("port"), "arrived": c["arr_t"], "left": c["dep_t"]} for c in d["calls"][:4]]
        return out

    def ai_strait_status(self, args: dict) -> dict:
        want = str(args.get("strait") or "").strip().lower()
        items = [dict(s) for s in self.straits_cache]
        for s in items:
            s.pop("geometry", None)
            pw = self.portwatch_brief(s["id"])
            if pw:
                s["portwatch"] = pw
        if want:
            items = [s for s in items if want in s["id"].replace("_", " ") or want in s["name"].lower()]
            if not items:
                return {"error": "Unknown strait.", "known": [s["name"] for s in self.straits_cache]}
        return {"straits": items, "note": None if self.has_global() else
                "Only the Baltic straits have live coverage without an aisstream.io key."}

    def ai_dark_ships(self, args: dict) -> dict:
        items = list(self.dark)
        if args.get("bbox") is not None:
            raw = args["bbox"]
            text = ",".join(str(x) for x in raw) if isinstance(raw, (list, tuple)) else str(raw)
            try:
                bbox = geo.parse_bbox(text)
            except ValueError as err:
                return {"error": f"bbox: {err}"}
            items = [d for d in items if geo.in_bbox(d["last"]["lat"], d["last"]["lon"], bbox)]
        return {"count": len(items), "dark_ships": items[:30],
                "method": "Silent for over 2 h after regular reports while under way, with other vessels still heard "
                          "near its last and estimated positions. Positions are dead-reckoning estimates.",
                "checked_at": self.dark_at}

    # ------------------------------------------------------------------------------------------
    # Anomalies (anomalies.py) and IMF PortWatch (portwatch.py)
    # ------------------------------------------------------------------------------------------
    @staticmethod
    def parse_kinds(raw) -> set[str] | None:
        """Anomaly kinds from "sts,loitering" or a list; category names are accepted too. ValueError if unknown."""
        if raw is None or raw == "" or raw == "all":
            return None
        items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        out = set()
        for k in items:
            k = str(k).strip().lower()
            if not k:
                continue
            if k not in KIND_ALIASES:
                raise ValueError(f"Unknown anomaly kind {k[:30]!r}: use sts, loitering, ais_gap or gnss.")
            out.add(KIND_ALIASES[k])
        return out or None

    def anomalies_answer(self, *, bbox=None, kinds=None, hours: float = 48.0, include_all: bool = False) -> dict:
        items = self.anomalies.query(bbox=bbox, kinds=kinds, hours=hours, include_all=include_all)
        return {"anomalies": items, "count": len(items), "flagged": sum(1 for i in items if i["published"]),
                "hours": hours, "detectors": self.anomalies.status(), "updated_at": self.anomalies.last.get("sts") or None,
                "note": ANOMALY_NOTE, "has_global": self.has_global()}

    def ai_ship_anomalies(self, args: dict) -> dict:
        bbox = None
        if args.get("bbox") is not None:
            raw = args["bbox"]
            text = ",".join(str(x) for x in raw) if isinstance(raw, (list, tuple)) else str(raw)
            try:
                bbox = geo.parse_bbox(text)
            except ValueError as err:
                return {"error": f"bbox: {err}"}
        try:
            kinds = self.parse_kinds(args.get("kinds"))
            hours = float(args.get("hours") or 48)
        except (TypeError, ValueError) as err:
            return {"error": str(err)}
        hours = max(1.0, min(168.0, hours))
        items = self.anomalies.query(bbox=bbox, kinds=kinds, hours=hours,
                                     include_all=bool(args.get("include_unflagged")))
        slim = []
        for it in items[:25]:
            slim.append({k: it.get(k) for k in ("id", "category", "title", "subtitle", "lat", "lon", "first_time", "time",
                                                "ongoing", "severity", "confidence", "state", "published", "observed",
                                                "coverage", "limits")}
                        | {"why_innocent": it.get("innocent", [])[:3],
                           "area_of_interest": (it.get("aoi") or {}).get("name"),
                           "vessels": [{"mmsi": v.get("id"), "name": v.get("title"), "type": v.get("subtitle"),
                                        "role": v.get("role"),
                                        "sanctions": (v.get("sanctions") or {}).get("text")} for v in it.get("vessels") or []]})
        det = self.anomalies.status()
        return {"count": len(items), "anomalies": slim, "truncated": len(items) > len(slim),
                "detectors": {k: {"state": d["state"], "status": d["status"], "flagged": d["flagged"],
                                  "not_flagged_by_place": d.get("excluded") or {},
                                  **({"excluded_waters": d["excluded_waters"]} if d.get("excluded_waters") else {})}
                              for k, d in det.items()},
                "note": ANOMALY_NOTE}

    def portwatch_brief(self, sid: str) -> dict | None:
        if self.portwatch is None:
            return None
        from .portwatch import MATCH
        pid = MATCH.get(sid)
        if not pid:
            return None
        try:
            s = self.portwatch.summaries().get(pid)
        except Exception as err:          # an odd stored row must not take the straits layer down with it
            if self.log:
                self.log.info("ships: PortWatch summary for %s failed: %s", sid, err)
            return None
        if not s:
            return None
        return {k: s.get(k) for k in ("id", "name", "latest", "mean_7d", "mean_1y", "change_1y_pct", "lag_days", "flags")}

    def _strait_items(self) -> dict[str, dict]:
        """Meridian's strait status by id: the analysis cache, or worked out now (just after a restart)."""
        items = {s["id"]: s for s in self.straits_cache}
        if not items:
            items = {s["id"]: s for s in analysis.strait_status(self.store, self.store.snapshot, self.now(), self.baselines,
                                                                 has_global=self.has_global(), with_geometry=False)}
        return items

    def portwatch_for(self, sid: str, series_days: int = 90, strait: dict | None = None) -> dict | None:
        """PortWatch's figures for one of Meridian's straits, with the plain-words cross-check."""
        if self.portwatch is None:
            return None
        from .portwatch import CREDIT, MATCH, cross_check, day_of
        pid = MATCH.get(sid)
        if not pid:
            return None
        pw = self.portwatch.summary(pid, series_days)
        if strait is None:
            strait = self._strait_items().get(sid)
        own = self.portwatch.strait_days(sid, day_of(self.now() - 40 * 86400))
        out = dict(pw or {"id": pid, "latest": None, "flags": ["no_data"]})
        out["cross_check"] = cross_check(pw, strait, own, self.portwatch.meta().get("last_error"))
        out["own_days"] = [own[d] | {"day": d} for d in sorted(own)][-30:]
        out["credit"] = CREDIT
        return out

    def chokepoints(self, series_days: int = 0) -> list[dict]:
        if self.portwatch is None:
            return []
        from .portwatch import cross_check, day_of
        sums = self.portwatch.summaries(series_days=series_days)
        by_strait = self._strait_items()
        failing = self.portwatch.meta().get("last_error")
        out = []
        for pid, s in sums.items():
            item = dict(s)
            sid = s.get("strait")
            strait = by_strait.get(sid) if sid else None
            if strait is not None:
                # Heard near the strait is not heard in it: the count means something only with vessels inside.
                inside = bool(strait.get("inside_now") or strait.get("unique_24h"))
                item["meridian"] = {"strait": sid, "name": strait["name"], "coverage": strait["coverage"],
                                    "heard_inside": bool(strait["coverage"]) and inside,
                                    "transits_24h": strait.get("transits_24h"), "inside_now": strait.get("inside_now")}
            own = self.portwatch.strait_days(sid, day_of(self.now() - 40 * 86400)) if sid else {}
            item["cross_check"] = cross_check(s, strait, own, failing)
            out.append(item)
        out.sort(key=lambda x: int(x["id"][10:]) if x["id"][10:].isdigit() else 999)
        return out

    def ai_chokepoint_transits(self, args: dict) -> dict:
        from .portwatch import CREDIT
        if self.portwatch is None:
            return {"error": "IMF PortWatch is turned off (SHIPS_PORTWATCH=0)."}
        want = str(args.get("chokepoint") or "").strip().lower()
        try:
            days = max(1, min(90, int(args.get("days") or 14)))
        except (TypeError, ValueError):
            return {"error": "days must be a whole number from 1 to 90"}
        items = self.chokepoints(series_days=days)
        if want:
            items = [c for c in items if want in c["name"].lower() or want == c["id"]
                     or want in (c.get("strait") or "").replace("_", " ")
                     or want in ((c.get("meridian") or {}).get("name") or "").lower()]
            if not items:
                return {"error": "Unknown chokepoint.", "known": [c["name"] for c in self.chokepoints()]}
        slim = []
        for c in items:
            slim.append({k: c.get(k) for k in ("id", "name", "latest", "lag_days", "mean_7d", "mean_28d", "mean_1y",
                                               "change_1y_pct", "missing_28d", "by_type_7d", "flags", "meridian")}
                        | {"daily": c.get("series"), "cross_check": c["cross_check"]["lines"]})
        return {"chokepoints": slim, "source": CREDIT,
                "note": "Daily transits from satellite AIS, published weekly about a week behind. Meridian's own counts "
                        "are terrestrial AIS and only exist where it has coverage; the cross-check says plainly when "
                        "either source has gaps."}


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / 86400:.0f} days ago"


__all__ = ["ShipsService", "CLASSES", "links_for"]
