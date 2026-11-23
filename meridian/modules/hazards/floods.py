"""England flood warnings (Environment Agency): warnings and alerts with their flood-area outlines,
river and sea gauges near them, the map payload, cards, "What's here", events, search and the AI
tool. Parsers are in ``ea.py``.

Polling follows the API's guidance: the warnings list (about 5 KB, conditional GET) every 15
minutes while the layer is in use and hourly otherwise (for events); the latest levels with one
``/data/readings?latest`` call (about 80 KB gzipped) every 15 minutes, only while the layer is in
use; the station list (about 0.7 MB gzipped) at most weekly, only once the layer is used. Flood
area outlines are fetched once per area and kept in SQLite.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone

from meridian import geo
from meridian.net import NetError
from meridian.registry import json_bytes

from . import ea
from .common import (LAYER_FLOODS, bbox_intersects, clean, fc, feature, geometry_bbox, geometry_center, norm, point,
                     simplify_geometry)

AREA_TTL = 60 * 86400          # refetch an outline when it is older than this and in use again
STATION_TTL = 7 * 86400
MIN_STATIONS = 300             # England has about 3,400 river and sea level stations; fewer means a cut answer


def _iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None


def _date(t: float | None) -> str:
    if not t:
        return "time unknown"
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC").lstrip("0")


def warning_entity(w: dict, rec: dict | None) -> dict:
    where = ", ".join(b for b in (w.get("river_sea"), w.get("county")) if b)
    return {"kind": "flood", "id": w["id"], "title": w["name"],
            "subtitle": clean(", ".join(b for b in (w["severity"], where) if b), 140),
            "lat": rec.get("lat") if rec else None, "lon": rec.get("lon") if rec else None, "layer": LAYER_FLOODS,
            "time": int(w.get("time") or 0), "zoom": 11, "source": "Environment Agency"}


def gauge_entity(g: dict) -> dict:
    reading = f"{g['value']:.2f} m" if g.get("value") is not None else "no recent reading"
    bits = [g.get("river") or ("Sea level" if g.get("kind") == "sea" else None), reading,
            ea.GAUGE_STATES[g.get("state") or "unknown"][0].lower() if g.get("value") is not None else None]
    return {"kind": "gauge", "id": f"eagauge:{g['ref']}", "title": g["label"],
            "subtitle": ", ".join(b for b in bits if b), "lat": g["lat"], "lon": g["lon"], "layer": LAYER_FLOODS,
            "time": int(g.get("time") or 0) or None, "zoom": 13, "source": "Environment Agency"}


def ordered_gauges(items: list[dict]) -> list[dict]:
    """Gauges with a reading first, the highest state first, then by name."""
    return sorted((g for g in items if g), key=lambda g: (g.get("value") is None, -ea.GAUGE_STATES[g["state"]][1],
                                                         g["label"].lower()))


def _min_vertex_km(lat: float, lon: float, geom: dict) -> float:
    best = 1e9
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    for poly in polys:
        for ring in poly:
            for p in ring:
                d = geo.haversine_km(lat, lon, p[1], p[0])
                if d < best:
                    best = d
    return best


class Floods:
    def __init__(self, svc):
        self.svc = svc
        self.m = svc.m
        self.net = svc.net
        self.store = svc.store
        self.log = svc.log
        self.lock = threading.RLock()
        self.loaded = False                       # a warnings list has been read (network or snapshot)
        self.warnings: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.areas: dict[str, dict] = {}          # area id -> outline record (areas with a current item)
        self._failed: dict[str, float] = {}
        self._outline_thread: threading.Thread | None = None
        self.stations: list[dict] = []
        self.station_by_ref: dict[str, dict] = {}
        self.station_index = geo.GridIndex(0.25)
        self.readings: dict[str, tuple] = {}
        self.prev: dict[str, tuple] = {}
        self.readings_at = 0.0
        self.near: dict[str, list[str]] = {}       # area id -> gauge refs, closest first
        self.version = 0
        self.floods_at = 0.0
        self._lm: dict[str, str | None] = {"floods": None, "readings": None}

    # ------------------------------------------------------------------ storage
    def ensure_tables(self) -> None:
        self.store.script(
            "CREATE TABLE IF NOT EXISTS hazards_ea_areas (id TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS hazards_plus_state (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at REAL NOT NULL)")
        row = self.store.one("SELECT v, updated_at FROM hazards_plus_state WHERE k = 'ea_floods'")
        if row:
            try:
                items = json.loads(row["v"])
                if isinstance(items, list):
                    self._set_warnings(items)
                    self.floods_at = row["updated_at"] or 0.0
            except ValueError:
                pass
        self.load_station_file()

    def housekeeping(self) -> None:
        self.store.execute("DELETE FROM hazards_ea_areas WHERE fetched_at < ?", (time.time() - 240 * 86400,))

    def _load_area(self, aid: str) -> dict | None:
        row = self.store.one("SELECT data FROM hazards_ea_areas WHERE id = ?", (aid,))
        if not row:
            return None
        try:
            return json.loads(row["data"])
        except ValueError:
            return None

    def _set_warnings(self, items: list[dict]) -> None:
        areas = {}
        for w in items:
            rec = self.areas.get(w["area_id"]) or self._load_area(w["area_id"])
            if rec:
                areas[w["area_id"]] = rec
        with self.lock:
            self.warnings = items
            self.by_id = {w["id"]: w for w in items}
            self.areas = areas
            self.loaded = True
            self.version += 1

    # ------------------------------------------------------------------ warnings
    def refresh_warnings(self):
        headers = {"If-Modified-Since": self._lm["floods"]} if self._lm["floods"] and self.loaded else None
        resp = self.net.request(ea.FLOODS_URL, timeout=20, retries=1, headers=headers, accept=(200, 304))
        now = time.time()
        if resp.status == 200:
            items = ea.parse_floods(resp.json())
            self._lm["floods"] = resp.headers.get("last-modified")
            self._set_warnings(items)
            try:
                self.store.execute("INSERT OR REPLACE INTO hazards_plus_state(k, v, updated_at) VALUES('ea_floods', ?, ?)",
                                   (json.dumps(items), now))
            except Exception as err:
                self.log.debug("EA warnings snapshot not saved: %s", err)
        self.floods_at = now
        self.fetch_outlines(active=self.svc.in_use("ea_floods"))
        self.rebuild()
        c = self.counts()
        if c["severe"] or c["warning"] or c["alert"]:
            parts = [f"{c['severe']} severe" if c["severe"] else None, f"{c['warning']} warnings" if c["warning"] else None,
                     f"{c['alert']} alerts" if c["alert"] else None]
            note = ", ".join(p for p in parts if p) + " in force"
        else:
            note = "no flood warnings or alerts in force"
        missing = self.missing_outlines(3 if self.svc.in_use("ea_floods") else 2)
        if missing:
            note += f"; {missing} outlines to fetch"
        return c["severe"] + c["warning"] + c["alert"], note

    def counts(self) -> dict:
        c = {"severe": 0, "warning": 0, "alert": 0, "ended": 0}
        for w in self.warnings:
            c[{1: "severe", 2: "warning", 3: "alert"}.get(w["level"], "ended")] += 1
        return c

    def wanted_outlines(self, top: int) -> list[str]:
        now = time.time()
        want = []
        for w in self.warnings:
            aid = w["area_id"]
            if w["level"] > top or now - self._failed.get(aid, 0) < 3600:
                continue
            rec = self.areas.get(aid)
            if rec is None or (now - (rec.get("fetched_at") or 0) > AREA_TTL and w["level"] <= 2):
                want.append(aid)
        return want

    def kick_outlines(self) -> None:
        """The layer is in use: fetch missing alert outlines now rather than at the next list poll."""
        if not self.wanted_outlines(3):
            return
        t = self._outline_thread
        if t is not None and t.is_alive():
            return

        def work():
            try:
                if self.fetch_outlines(active=True):
                    self.rebuild()
            except Exception as err:   # a background convenience; the list poll retries later
                self.log.info("EA outlines failed: %s", str(err)[:200])

        self._outline_thread = threading.Thread(target=work, daemon=True, name="hazards-ea-outlines")
        self._outline_thread.start()

    def fetch_outlines(self, active: bool, budget_s: float = 25.0) -> int:
        """Outlines for areas that have a warning (every level while the layer is used, warnings and
        severe warnings otherwise, so events have a position)."""
        top = 3 if active else 2
        want = self.wanted_outlines(top)
        got = 0
        deadline = time.time() + budget_s
        for aid in want[:40]:
            if time.time() > deadline:
                break
            try:
                data = self.net.get_json(ea.POLYGON_URL.format(id=aid), timeout=20, retries=0)
            except (NetError, ValueError) as err:
                self._failed[aid] = time.time()
                self.log.info("EA outline %s unavailable: %s", aid, str(err)[:120])
                continue
            rec = self.outline_record(aid, data)
            if rec is None:
                self._failed[aid] = time.time()
                continue
            self.store.execute("INSERT OR REPLACE INTO hazards_ea_areas(id, data, fetched_at) VALUES(?,?,?)",
                               (aid, json.dumps(rec, separators=(",", ":")), rec["fetched_at"]))
            with self.lock:
                areas = dict(self.areas)
                areas[aid] = rec
                self.areas = areas
                self.version += 1
            got += 1
        return got

    def missing_outlines(self, top: int) -> int:
        return sum(1 for w in self.warnings if w["level"] <= top and w["area_id"] not in self.areas)

    @staticmethod
    def outline_record(aid: str, data) -> dict | None:
        p = ea.parse_polygon(data)
        if not p:
            return None
        fine = simplify_geometry(p["geometry"], 0.0003, 5)
        if not fine:
            return None
        coarse = simplify_geometry(p["geometry"], 0.0025, 4) or fine
        c = geometry_center(fine) or geometry_center(coarse)
        if not c:
            return None
        return {"id": aid, "name": p["name"], "description": p["description"], "local_authority": p["local_authority"],
                "quick_dial": p["quick_dial"], "river_sea": p["river_sea"], "ea_area": p["ea_area"],
                "fine": fine, "coarse": coarse, "bbox": geometry_bbox(fine), "lat": round(c[1], 5), "lon": round(c[0], 5),
                "fetched_at": time.time()}

    # ------------------------------------------------------------------ stations and readings
    def station_file(self):
        return self.m.data_dir / "ea_stations.json"

    def load_station_file(self) -> bool:
        path = self.station_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            return False
        items = data.get("stations") if isinstance(data, dict) else None
        if not items:
            return False
        self._set_stations(items)
        st = self.svc.state.get("ea_stations")
        if st is not None:
            st.ok_at = st.attempt_at = mtime
            st.data = len(items)
            st.note = f"{len(items):,} river and sea level stations"
            self.m.status(st.sid, ok=True, count=len(items), note=st.note)
        return True

    def _set_stations(self, items: list[dict]) -> None:
        index = geo.GridIndex(0.25)
        for s in items:
            for k in ("label", "river", "town", "catchment"):
                if s.get(k):
                    s[k] = ea.tidy_name(s[k])
            index.add(s["lat"], s["lon"], s)
        with self.lock:
            self.stations = items
            self.station_by_ref = {s["ref"]: s for s in items}
            self.station_index = index
            self.version += 1

    def refresh_stations(self):
        data = self.net.get_json(ea.STATIONS_URL, params={"parameter": "level", "_view": "full"}, timeout=120, retries=1)
        items = ea.parse_stations(data)
        if len(items) < MIN_STATIONS:
            raise RuntimeError(f"the EA station list looks incomplete ({len(items)} stations)")
        path = self.station_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(json_bytes({"built_at": time.time(), "stations": items}))
        tmp.replace(path)
        self._set_stations(items)
        self.rebuild()
        sea = sum(1 for s in items if s["kind"] == "sea")
        return len(items), f"{len(items):,} river and sea level stations ({sea} tidal)"

    def refresh_readings(self):
        if not self.stations:
            if not self.svc.run("ea_stations") or not self.stations:
                raise RuntimeError("the EA station list is not available yet")
        headers = {"If-Modified-Since": self._lm["readings"]} if self._lm["readings"] and self.readings else None
        resp = self.net.request(ea.READINGS_URL + "?latest&parameter=level", timeout=40, retries=1, headers=headers,
                                accept=(200, 304))
        if resp.status == 200:
            new = ea.parse_readings(resp.json())
            if not new:
                raise RuntimeError("the EA latest readings were empty")
            prev = dict(self.prev)
            for mid, cur in new.items():
                old = self.readings.get(mid)
                if old and cur[1] and old[1] and cur[1] > old[1]:
                    prev[mid] = old
            with self.lock:
                self.readings = new
                self.prev = prev
            self._lm["readings"] = resp.headers.get("last-modified")
        self.readings_at = time.time()
        self.rebuild()
        near = {r for refs in self.near.values() for r in refs}
        high = sum(1 for r in near if (self.gauge(r) or {}).get("state") in ("high", "highest"))
        return len(self.readings), (f"{len(self.readings):,} level readings; {len(near)} gauges near warnings"
                                    + (f", {high} above normal" if high else ""))

    def gauge(self, ref: str) -> dict | None:
        st = self.station_by_ref.get(ref)
        if st is None:
            return None
        cur = self.readings.get(st["measure"])
        value, t = cur if cur else (None, None)
        state = ea.gauge_state(st, value)
        out = dict(st)
        out.update(value=value, time=t, state=state, state_label=ea.GAUGE_STATES[state][0],
                   trend=ea.trend(self.prev.get(st["measure"]), cur),
                   stale=bool(t and time.time() - t > 6 * 3600),
                   page=ea.STATION_PAGE.format(rloi=st["rloi"]) if st.get("rloi") else None)
        return out

    # ------------------------------------------------------------------ gauges near warnings
    def rebuild(self) -> None:
        near = {}
        if self.stations:
            for w in self.warnings:
                if w["level"] > 3:
                    continue
                rec = self.areas.get(w["area_id"])
                if rec:
                    near[w["area_id"]] = self.stations_near(rec)
        with self.lock:
            self.near = near
            self.version += 1

    def stations_near(self, rec: dict, max_km: float = 3.0, limit: int = 12) -> list[str]:
        w, s, e, n = rec["bbox"]
        clat, clon = (s + n) / 2.0, (w + e) / 2.0
        radius = geo.haversine_km(s, w, n, e) / 2.0 + max_km
        geom = rec.get("coarse") or rec["fine"]
        pad_lat = max_km / 111.0
        pad_lon = pad_lat / max(0.2, abs(math.cos(math.radians(clat))))
        out = []
        for _, st in self.station_index.near(clat, clon, radius):
            la, lo = st["lat"], st["lon"]
            if not (w - pad_lon <= lo <= e + pad_lon and s - pad_lat <= la <= n + pad_lat):
                continue
            if geo.point_in_geometry(lo, la, rec["fine"]):
                out.append((0.0, st["ref"]))
                continue
            d = _min_vertex_km(la, lo, geom)
            if d <= max_km:
                out.append((d, st["ref"]))
        out.sort()
        return [ref for _, ref in out[:limit]]

    # ------------------------------------------------------------------ map payload
    def map_fc(self, zoom: float, bbox, min_level: int, ended: bool, gauges: bool) -> dict:
        level = "fine" if zoom >= 9 else "coarse"
        areas, labels, pts = [], [], []
        shown = []
        for w in self.warnings:
            if w["level"] > min_level and not (ended and w["level"] == 4):
                continue
            rec = self.areas.get(w["area_id"])
            if not rec:
                continue
            if level == "fine" and bbox is not None and not bbox_intersects(rec["bbox"], bbox):
                continue
            props = {"id": w["id"], "lvl": w["level"], "name": w["name"], "sev": w["severity"],
                     "river": w.get("river_sea") or "", "t": int(w.get("time") or 0), "tidal": 1 if w.get("tidal") else 0}
            areas.append(feature(rec.get(level) or rec["fine"], dict(props, role="area")))
            labels.append(feature(point(rec["lon"], rec["lat"]), dict(props, role="label")))
            shown.append(w)
        seen = set()
        if gauges:
            for w in shown:
                for ref in self.near.get(w["area_id"], ()):
                    if ref in seen:
                        continue
                    seen.add(ref)
                    g = self.gauge(ref)
                    if not g or g["value"] is None:
                        continue          # no level to show (offline or not yet read)
                    if level == "fine" and bbox is not None and not geo.in_bbox(g["lat"], g["lon"], bbox):
                        continue
                    pts.append(feature(point(g["lon"], g["lat"]), {
                        "id": f"eagauge:{ref}", "role": "gauge", "name": g["label"], "river": g.get("river") or "",
                        "v": g["value"], "unit": g["unit"], "st": g["state"], "rank": ea.GAUGE_STATES[g["state"]][1],
                        "tr": g["trend"] or "", "t": int(g["time"] or 0), "sea": 1 if g["kind"] == "sea" else 0,
                        "old": 1 if g["stale"] else 0}))
        areas.sort(key=lambda f: -f["properties"]["lvl"])
        c = self.counts()
        in_force = sum(1 for w in self.warnings if w["level"] <= min_level)     # England-wide, not just the view
        return fc(areas + labels + pts, count=in_force, shown=len(shown), counts=c, gauges=len(pts),
                  gauges_high=sum(1 for f in pts if f["properties"]["st"] in ("high", "highest")),
                  outlines_pending=self.missing_outlines(min_level), stations=len(self.stations),
                  readings_at=self.readings_at or None,
                  updated_at=self.floods_at or None, level=level, credit=ea.CREDIT,
                  source={"floods": self.svc.source_info("ea_floods"), "gauges": self.svc.source_info("ea_gauges"),
                          "stations": self.svc.source_info("ea_stations")})

    # ------------------------------------------------------------------ cards
    def detail(self, svc, eid: str) -> dict | None:
        aid = eid.split(":", 1)[1] if ":" in eid else ""
        if not ea.AREA_ID.fullmatch(aid):
            return None
        w = self.by_id.get(f"ea:{aid}")
        if w is None:
            return None
        self.wake()
        rec = self.areas.get(aid)
        if rec is None:
            try:
                rec = self.outline_record(aid, self.net.get_json(ea.POLYGON_URL.format(id=aid), timeout=8, retries=0))
            except (NetError, ValueError):
                rec = None
            if rec:
                self.store.execute("INSERT OR REPLACE INTO hazards_ea_areas(id, data, fetched_at) VALUES(?,?,?)",
                                   (aid, json.dumps(rec, separators=(",", ":")), rec["fetched_at"]))
                with self.lock:
                    areas = dict(self.areas)
                    areas[aid] = rec
                    self.areas = areas
        info = ea.level_info(w["level"])
        data = dict(w)
        data.update(meaning=info["meaning"], tone=info["tone"], severity_level=info["severity"],
                    page=ea.AREA_PAGE.format(id=aid), floodline=ea.FLOODLINE, credit=ea.CREDIT,
                    api_url=f"{ea.FLOODS_URL}/{aid}")
        if rec:
            data.update(description=rec.get("description"), local_authority=rec.get("local_authority"),
                        quick_dial=rec.get("quick_dial"), outline=rec["fine"], bbox=rec["bbox"], lat=rec["lat"],
                        lon=rec["lon"])
            refs = self.near.get(aid)
            if refs is None and self.stations:
                refs = self.stations_near(rec)
            data["gauges"] = ordered_gauges([self.gauge(r) for r in refs or []])
        else:
            data["gauges"] = []
        data["readings_at"] = self.readings_at or None
        data["stations_loaded"] = bool(self.stations)
        return {"type": "ea_flood", "entity": warning_entity(w, rec), "data": data}

    def gauge_detail(self, svc, eid: str) -> dict | None:
        ref = eid.split(":", 1)[1] if ":" in eid else ""
        if not ea.STATION_ID.fullmatch(ref):
            return None
        self.wake()
        g = self.gauge(ref)
        if g is None:
            return None
        data = dict(g)
        data["warnings"] = []
        for w in self.warnings:
            if ref in self.near.get(w["area_id"], ()):
                data["warnings"].append(dict(warning_entity(w, self.areas.get(w["area_id"])), level=w["level"]))
        data["credit"] = ea.CREDIT
        data["readings_at"] = self.readings_at or None
        return {"type": "ea_gauge", "entity": gauge_entity(g), "data": data}

    def history(self, ref: str, hours: float) -> dict:
        st = self.station_by_ref.get(ref)
        if st is None:
            return {"points": [], "note": "This gauge is not in the station list (yet)."}
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
        # the measure id comes from the EA station list (checked by the parser), never from the request
        data = self.net.get_json(ea.MEASURE_READINGS_URL.format(id=st["measure"]), params={"since": since, "_limit": 2000},
                                 timeout=15, retries=0, ttl=900)
        pts = ea.parse_history(data)
        return {"ref": ref, "measure": st["measure"], "unit": st["unit"], "hours": hours, "points": pts,
                "typical_high": st.get("typical_high"), "typical_low": st.get("typical_low"),
                "recent_high": st.get("recent_high"), "record_high": st.get("record_high")}

    def wake(self) -> None:
        """A card or the AI asked: make sure the data is not stale (refreshed in the background)."""
        self.svc.touch(LAYER_FLOODS)
        self.svc.kick("ea_floods")
        if self.stations or not self.svc.state["ea_stations"].error:
            self.svc.kick("ea_gauges")

    # ------------------------------------------------------------------ what's here, nearby, search, events
    def warnings_near(self, lat: float, lon: float, max_km: float) -> list[tuple[float, dict, dict]]:
        out = []
        pad = max_km / 111.0 * 1.8
        for w in self.warnings:
            rec = self.areas.get(w["area_id"])
            if not rec:
                continue
            b = rec["bbox"]
            if not (b[0] - pad <= lon <= b[2] + pad and b[1] - pad / 1.8 <= lat <= b[3] + pad / 1.8):
                continue
            if geo.point_in_geometry(lon, lat, rec["fine"]):
                out.append((0.0, w, rec))
                continue
            d = _min_vertex_km(lat, lon, rec.get("coarse") or rec["fine"])
            if d <= max_km:
                out.append((d, w, rec))
        out.sort(key=lambda x: (x[1]["level"], x[0]))
        return out

    def here(self, lat: float, lon: float) -> dict:
        england = ea.in_england(lat, lon)
        if england and time.time() - self.floods_at > 1200:
            self.svc.kick("ea_floods")
        found = self.warnings_near(lat, lon, 5.0) if england else []
        gauges = []
        if england and self.stations and time.time() - self.readings_at < 3600:
            for d, st in self.station_index.near(lat, lon, 10.0, 3):
                g = self.gauge(st["ref"])
                if g and g.get("value") is not None:
                    gauges.append(dict(gauge_entity(g), distance_km=round(d, 1), value=g["value"], state=g["state"],
                                       state_label=g["state_label"], trend=g["trend"], river=g.get("river")))
        return {"england": england, "updated_at": self.floods_at or None,
                "warnings": [dict(warning_entity(w, rec), level=w["level"], severity=w["severity"], meaning=w["meaning"],
                                  distance_km=round(d, 1), inside=d == 0.0) for d, w, rec in found[:6] if w["level"] <= 3],
                "ended": sum(1 for d, w, rec in found if w["level"] == 4),
                "gauges": gauges, "credit": ea.CREDIT}

    def nearby(self, lat: float, lon: float, radius_km: float, limit: int) -> list[dict]:
        out = []
        for d, w, rec in self.warnings_near(lat, lon, min(radius_km, 25.0)):
            if w["level"] <= 3:
                out.append(dict(warning_entity(w, rec), distance_km=round(d, 2)))
        if self.stations:
            for d, st in self.station_index.near(lat, lon, min(radius_km, 15.0), min(limit, 5)):
                g = self.gauge(st["ref"])
                if g:
                    out.append(dict(gauge_entity(g), distance_km=round(d, 2)))
        return out[: limit * 2]

    def search(self, q: str, limit: int) -> list[dict]:
        nq = norm(q)
        if len(nq) < 3:
            return []
        out = []
        for w in self.warnings:
            rec = self.areas.get(w["area_id"])
            if not rec:
                continue
            n = norm(w["name"])
            sc = 0.52 if n.startswith(nq) else 0.42 if nq in n else None
            if sc is None:
                for text in (w.get("river_sea"), w.get("county"), w.get("ea_area"), rec.get("description")):
                    if text and nq in norm(text):
                        sc = 0.3
                        break
            if sc is None:
                continue
            ent = warning_entity(w, rec)
            ent["score"] = sc + {1: 0.15, 2: 0.1, 3: 0.04}.get(w["level"], 0.0)
            out.append(ent)
        if self.stations:
            hits = []
            for st in self.stations:
                n = norm(st["label"])
                if n.startswith(nq) or (st.get("river") and norm(st["river"]).startswith(nq)) or (
                        st.get("town") and norm(st["town"]).startswith(nq)):
                    hits.append(st)
                    if len(hits) >= 6:
                        break
            for st in hits:
                g = self.gauge(st["ref"]) or st
                ent = gauge_entity(dict(g, state=g.get("state") or "unknown", value=g.get("value")))
                ent["score"] = 0.24 if norm(st["label"]) == nq else 0.2
                out.append(ent)
        out.sort(key=lambda e: -e["score"])
        return out[:limit]

    def events(self, since_ts: float) -> list[dict]:
        out = []
        for w in self.warnings:
            if w["level"] > 2:
                continue
            rec = self.areas.get(w["area_id"])
            if not rec:
                continue
            t = max(x for x in (w.get("raised"), w.get("severity_changed"), w.get("message_changed"), 0) if x is not None)
            if not t or t < since_ts:
                continue
            e = warning_entity(w, rec)
            e.update(time=int(t), severity=ea.LEVELS[w["level"]]["severity"], first_time=int(w.get("severity_changed") or t),
                     severity_text=w["severity"], meaning=w["meaning"], river_or_sea=w.get("river_sea"),
                     county=w.get("county"))
            out.append(e)
        return out

    # ------------------------------------------------------------------ AI tool
    def tool(self, args: dict) -> dict:
        lat, lon = args.get("lat"), args.get("lon")
        point_q = lat is not None or lon is not None
        if point_q:
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                raise ValueError("lat and lon must both be numbers") from None
            if not geo.valid_latlon(lat, lon):
                raise ValueError("lat/lon out of range")
        radius = _num(args.get("radius_km"), 10.0, 0.5, 100.0, "radius_km")
        bbox = None
        if args.get("bbox"):
            try:
                bbox = geo.parse_bbox(str(args["bbox"]))
            except ValueError:
                raise ValueError("bbox must be west,south,east,north in degrees") from None
        area = str(args.get("area") or "").strip()[:80]
        floor = {"severe": 1, "warning": 2, "alert": 3, "ended": 4}.get(str(args.get("min_severity") or "alert").lower())
        if floor is None:
            raise ValueError("min_severity must be severe, warning, alert or ended")
        with_gauges = args.get("include_gauges", True) is not False
        limit = int(_num(args.get("limit"), 25, 1, 100, "limit"))
        if not self.loaded or time.time() - self.floods_at > 1800:
            self.svc.kick("ea_floods")
            t = self.svc._threads.get("ea_floods")
            if t is not None and not self.loaded:
                t.join(8.0)
        rows = []
        if point_q:
            rows = [(d, w, rec) for d, w, rec in self.warnings_near(lat, lon, radius)]
        else:
            for w in self.warnings:
                rec = self.areas.get(w["area_id"])
                if bbox is not None and (not rec or not bbox_intersects(rec["bbox"], bbox)):
                    continue
                if area:
                    na = norm(area)
                    # not the EA's own region name ("Devon, Cornwall and the Isles of Scilly"): too broad
                    texts = (w["name"], w.get("county"), w.get("river_sea"),
                             (rec or {}).get("description"), (rec or {}).get("local_authority"))
                    if not any(t and na in norm(t) for t in texts):
                        continue
                rows.append((None, w, rec))
        rows = [r for r in rows if r[1]["level"] <= floor]
        rows.sort(key=lambda r: (r[1]["level"], r[0] if r[0] is not None else 0, -(r[1].get("time") or 0)))
        c = self.counts()
        items = []
        for d, w, rec in rows[:limit]:
            item = {"id": w["id"], "severity": w["severity"], "severity_level": w["level"], "meaning": w["meaning"],
                    "area": w["name"], "river_or_sea": w.get("river_sea"), "county": w.get("county"),
                    "tidal": w.get("tidal"), "raised_utc": _iso(w.get("raised")),
                    "severity_changed_utc": _iso(w.get("severity_changed")), "message": clean(w.get("message"), 700),
                    "lat": (rec or {}).get("lat"), "lon": (rec or {}).get("lon"),
                    "page": ea.AREA_PAGE.format(id=w["area_id"]), "entity": warning_entity(w, rec)}
            if d is not None:
                item["distance_km"] = round(d, 1)
            if with_gauges and rec:
                refs = self.near.get(w["area_id"]) or (self.stations_near(rec) if self.stations else [])
                gs = ordered_gauges([self.gauge(r) for r in refs])[:5]
                item["gauges"] = [{"name": g["label"], "river": g.get("river"), "level_m": g["value"],
                                   "state": g["state_label"] if g["value"] is not None else "no recent reading",
                                   "trend": g["trend"], "time_utc": _iso(g["time"])} for g in gs]
            items.append(item)
        notes = ["England only: Scotland (SEPA) and Wales (Natural Resources Wales) publish their own warnings, "
                 "which Meridian does not read yet."]
        if with_gauges and not self.readings:
            notes.append("Gauge levels load while the flood warnings layer is on; they are missing now.")
        return {"source": "Environment Agency real-time flood-monitoring API (OGL v3)", "updated_utc": _iso(self.floods_at),
                "in_force": {"severe_flood_warnings": c["severe"], "flood_warnings": c["warning"], "flood_alerts": c["alert"]},
                "count": len(items), "warnings": items, "notes": notes,
                "levels": {"1": "Severe flood warning: severe flooding, danger to life",
                           "2": "Flood warning: flooding is expected, immediate action required",
                           "3": "Flood alert: flooding is possible, be prepared"}}


def _num(v, default, lo, hi, name):
    if v in (None, ""):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if x != x:
        raise ValueError(f"{name} must be a number")
    return max(lo, min(hi, x))
