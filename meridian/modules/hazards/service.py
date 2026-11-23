"""The hazards service: per-source polling, snapshots, derived layers and lookups.

Everything a request handler needs is precomputed here in background threads; handlers only
filter and serialise. Snapshots are replaced wholesale, so readers never see partial data.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from pathlib import Path

from meridian import geo
from meridian.net import NetError
from meridian.registry import Response, json_bytes

from . import countries, eonet, firms, gdacs, meteoalarm, nhc, nws, tsunami, usgs, volcanoes, who
from .common import (LAYER_DISASTERS, LAYER_FIRES, LAYER_NATURAL, LAYER_OUTBREAKS, LAYER_QUAKES, LAYER_STORMS,
                     LAYER_TSUNAMI, LAYER_VOLCANOES, LAYER_WARNINGS, SEVERITY_RANK, SourceState, bboxes_contain,
                     bboxes_intersect, geometry_bboxes,
                     category_label, clean, day_utc, fc, feature, geometry_bbox, point, simplify_geometry,
                     norm, storm_category, title_case_name)

ACTIVE_WINDOW = 900.0          # a layer counts as in use for 15 min after its last request
STORM_ACTIVE_HOURS = 48.0       # a cyclone without an advisory for this long is shown as ended


def storm_word(cat: int | None, lat: float, lon: float) -> str:
    """What a cyclone is called where it is: hurricane, typhoon or tropical cyclone."""
    if cat is None or cat < 0:
        return "Tropical depression" if cat == -1 else "Tropical cyclone"
    if cat == 0:
        return "Tropical storm"
    if lat > 0 and lon < 0:
        return "Hurricane"
    if lat > 0 and lon >= 100:
        return "Typhoon"
    return "Tropical cyclone"


def _err(err: Exception) -> str:
    text = str(err) or type(err).__name__
    return text[:300]


class Hazards:
    def __init__(self, m):
        self.m = m
        self.net = m.net
        self.log = m.log
        self.store = m.store
        self.lock = threading.RLock()
        self.background = m.config.get_bool("HAZARDS_BACKGROUND", True)
        self.firms_sats = firms.satellites_from_setting(m.config.get("HAZARDS_FIRMS_SATELLITES"))
        self.ma_countries = meteoalarm.countries_from_setting(m.config.get("HAZARDS_METEOALARM_COUNTRIES"))
        idle = (lambda s: s) if self.background else (lambda s: None)
        self.state = {
            "quakes": SourceState("hazards.usgs", 300, idle(1800)),
            "fires": SourceState("hazards.firms", 1800, None),
            "eonet": SourceState("hazards.eonet", 900, idle(3600)),
            "gdacs": SourceState("hazards.gdacs", 600, idle(1800)),
            "nhc": SourceState("hazards.nhc", 600, idle(1800)),
            "hans": SourceState("hazards.hans", 1800, idle(7200)),
            "nws": SourceState("hazards.nws", 300, idle(3600)),
            "zones": SourceState("hazards.nws_zones", 20, None),
            "meteoalarm": SourceState("hazards.meteoalarm", 1800, None),
            "ma_regions": SourceState("hazards.meteoalarm_regions", 6 * 3600, None),
            "tsunami": SourceState("hazards.tsunami", 300, idle(600)),
            "who": SourceState("hazards.who", 6 * 3600, idle(6 * 3600)),
        }
        # which layers make a source "in use"
        self.layers_for = {
            "quakes": (LAYER_QUAKES,), "fires": (LAYER_FIRES,), "eonet": (LAYER_NATURAL, LAYER_VOLCANOES, LAYER_STORMS, LAYER_FIRES),
            "gdacs": (LAYER_DISASTERS, LAYER_STORMS, LAYER_VOLCANOES), "nhc": (LAYER_STORMS,),
            "hans": (LAYER_VOLCANOES,), "nws": (LAYER_WARNINGS,), "zones": (LAYER_WARNINGS,),
            "meteoalarm": (LAYER_WARNINGS,), "ma_regions": (LAYER_WARNINGS,), "tsunami": (LAYER_TSUNAMI, LAYER_QUAKES),
            "who": (LAYER_OUTBREAKS,),
        }
        # snapshots
        self.quakes: dict[str, dict] = {}
        self._quake_feeds: dict[str, list] = {"day": [], "week": [], "month": []}
        self._quake_feed_at: dict[str, float] = {"day": 0.0, "week": 0.0, "month": 0.0}
        self.fire_data = firms.FireData().finish()
        self.fire_meta: dict = {"mode": "none", "sources": {}}
        self._fire_files: dict[str, tuple] = {}
        self.eonet_events: list[dict] = []
        self.gdacs_events: dict[str, dict] = {}
        self.gdacs_storms: list[dict] = []
        self._gdacs_rss_at = 0.0
        self._gdacs_rss: list[dict] = []
        self.nhc_storms: list[dict] = []
        self._nhc_layers: dict | None = None
        self._nhc_layers_at = 0.0
        self.storms: list[dict] = []
        self.volcano_list: list[dict] = []
        self.volcano_by_id: dict[str, dict] = {}
        self.volcano_index = geo.GridIndex(2.0)
        self.volcano_names: list[tuple[str, dict]] = []
        self.hans: dict[str, dict] = {}
        self.volcano_activity: dict[str, dict] = {}
        self.extra_volcanoes: list[dict] = []
        self.nws_alerts: list[dict] = []
        self.nws_full = False
        self.nws_zones: dict[str, dict] = {}
        self._zone_missing: set[str] = set()
        self.ma_warnings: dict[str, list] = {}
        self._ma_feed_at: dict[str, float] = {}
        self.ma_regions: dict = {"regions": {}, "aliases": {}}
        self.tsunami_msgs: list[dict] = []
        self.who_items: list[dict] = []
        self.versions = {"warnings": 0, "volcanoes": 0, "storms": 0, "outbreaks": 0}
        self._cache: dict[str, tuple] = {}
        self._cache_lock = threading.Lock()
        self._warn_features: dict[str, tuple] = {}
        self._threads: dict[str, threading.Thread] = {}
        # sources added by plus.py (emergency mapping, England floods, flood outlook): name -> refresh fn
        self.refreshers: dict = {}
        self.detail_handlers: dict = {}      # id prefix -> fn(svc, eid) for /api/hazards/details
        self.event_hooks: list = []          # fn(since_ts) -> [event], merged into providers.events
        self.extras = None                   # the plus.Plus instance, once registered

    # ------------------------------------------------------------------ activity and scheduling
    def in_use(self, name: str) -> bool:
        return any(self.m.active(key, within=ACTIVE_WINDOW) for key in self.layers_for.get(name, ()))

    def touch(self, layer: str) -> None:
        self.m.touch(layer)

    def tick(self, name: str) -> None:
        st = self.state[name]
        if st.due(self.in_use(name)):
            self.run(name)

    def run(self, name: str) -> bool:
        st = self.state[name]
        if not st.lock.acquire(blocking=False):
            return False
        try:
            st.attempt_at = time.time()
            fn = self.refreshers.get(name) or getattr(self, f"refresh_{name}")
            result = fn()
            count, note = result if isinstance(result, tuple) else (result, None)
            st.succeeded(st.data, note)
            self.m.status(st.sid, ok=True, count=count, note=note)
            return True
        except Exception as err:   # one failing source never affects the others
            msg = self.m.config.redact(_err(err))
            st.failed(msg)
            self.m.status(st.sid, ok=False, error=msg)
            self.log.info("hazards %s refresh failed: %s", name, msg)
            return False
        finally:
            st.lock.release()

    def kick(self, name: str, max_age: float | None = None, force: bool = False) -> None:
        """Refresh a source now (in the background) when a layer asks and the data is old."""
        st = self.state[name]
        limit = max_age if max_age is not None else st.active_every
        if st.lock.locked() or (not force and not st.stale(limit)):
            return
        if time.time() - st.attempt_at < (60 if force and st.error else 0 if force else 20):
            return
        t = self._threads.get(name)
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=self.run, args=(name,), daemon=True, name=f"hazards-kick-{name}")
        self._threads[name] = t
        t.start()

    def source_info(self, name: str) -> dict:
        return self.state[name].info()

    # ------------------------------------------------------------------ cached responses
    def cached_json(self, key: str, version, build) -> Response:
        """A JSON response rebuilt only when ``version`` changes (ETag + pre-gzipped body)."""
        with self._cache_lock:
            item = self._cache.get(key)
        if item is None or item[0] != version:
            body = json_bytes(build())
            gz = gzip.compress(body, compresslevel=5) if len(body) > 2048 else None
            etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
            item = (version, body, gz, etag)
            with self._cache_lock:
                self._cache[key] = item
                while len(self._cache) > 200:
                    self._cache.pop(next(iter(self._cache)))
        resp = Response(item[1], content_type="application/json; charset=utf-8", cache="no-cache", etag=item[3])
        resp.gzipped = item[2]
        return resp

    # ==================================================================================== quakes
    def refresh_quakes(self):
        active = self.in_use("quakes")
        now = time.time()
        plan = {"day": 300 if active else None, "week": 900 if active else 1800, "month": 1800}
        fetched = []
        for feed, every in plan.items():
            if every is None or now - self._quake_feed_at[feed] < every - 5:
                continue
            data = self.net.get_json(usgs.FEEDS[feed], timeout=20, retries=1)
            self._quake_feeds[feed] = usgs.parse_feed(data)
            self._quake_feed_at[feed] = now
            fetched.append(feed)
        merged = usgs.merge(self._quake_feeds["month"], self._quake_feeds["week"], self._quake_feeds["day"])
        cutoff = now - 31 * 86400
        self.quakes = {k: v for k, v in merged.items() if v["time"] >= cutoff}
        self.state["quakes"].data = len(self.quakes)
        day = sum(1 for q in self.quakes.values() if q["time"] >= now - 86400)
        return len(self.quakes), f"{day} in the past day, {len(self.quakes)} kept (feeds: {', '.join(fetched) or 'cached'})"

    def quake_list(self, window: str, min_mag: float) -> list[dict]:
        now = time.time()
        if window == "day":
            since, floor = now - 86400, None
        elif window == "month":
            since, floor = now - 30 * 86400, None
        else:
            since, floor = now - 7 * 86400, 2.5
        out = []
        sig_ids = {q["id"] for q in self._quake_feeds["month"]}
        for q in self.quakes.values():
            if q["time"] < since:
                continue
            mag = q["mag"] if q["mag"] is not None else -1
            if window == "month" and q["id"] not in sig_ids:
                continue
            if window == "week" and q["time"] < now - 86400 and mag < (floor or 0):
                continue
            if mag < min_mag:
                continue
            out.append(q)
        out.sort(key=lambda q: q["time"])
        return out

    def quakes_fc(self, window: str, min_mag: float) -> dict:
        items = self.quake_list(window, min_mag)
        feats = []
        for q in items:
            feats.append(feature(point(q["lon"], q["lat"]), {
                "id": q["id"], "mag": q["mag"], "t": int(q["time"]), "d": q["depth_km"], "alert": q["alert"] or "",
                "tsu": 1 if q["tsunami"] else 0, "felt": q["felt"] or 0, "sig": q["sig"], "place": q["place"],
                "type": q["type"], "mt": q["mag_type"] or ""}))
        st = self.source_info("quakes")
        biggest = max(items, key=lambda q: q["mag"] or -9, default=None)
        partial = window in ("day", "week") and not self._quake_feed_at["day"]
        return fc(feats, count=len(feats), window=window, min_mag=min_mag, source=st, partial=partial,
                  biggest={"id": biggest["id"], "mag": biggest["mag"], "place": biggest["place"]} if biggest else None)

    # ===================================================================================== fires
    def refresh_fires(self):
        key = self.m.config.get("FIRMS_MAP_KEY")
        raw = self.m.raw_dir / "firms"
        raw.mkdir(parents=True, exist_ok=True)
        texts: list[tuple[str, str]] = []
        status: dict[str, dict] = {}
        changed = False
        for sat in self.firms_sats:
            spec = firms.SATELLITES[sat]
            dest = raw / f"{sat}_24h.csv"
            mode = "file"
            try:
                if key:
                    url = firms.AREA_API.format(key=key, source=spec["api"])
                    mode = "api"
                    try:
                        self.net.download(url, dest, max_age_hours=0.45, timeout=120, min_bytes=40)
                    except NetError:
                        mode = "file"
                        self.net.download(spec["file"], dest, max_age_hours=0.45, timeout=120, min_bytes=40)
                else:
                    self.net.download(spec["file"], dest, max_age_hours=0.45, timeout=120, min_bytes=40)
            except NetError as err:
                status[sat] = {"ok": False, "error": self.m.config.redact(_err(err)), "rows": 0}
                if not dest.exists():
                    continue
            try:
                size = dest.stat().st_size
            except OSError:
                continue
            try:
                meta = json.loads(dest.with_name(dest.name + ".meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            sig = (size, meta.get("fetched_at"), meta.get("etag"))
            if self._fire_files.get(sat) != sig:
                changed = True
            self._fire_files[sat] = sig
            texts.append((sat, dest.read_text(encoding="utf-8", errors="replace")))
            status.setdefault(sat, {"ok": True, "error": None})
            status[sat]["mode"] = mode
        if not texts:
            self.fire_meta = {"mode": "fallback", "sources": status,
                              "reason": "NASA FIRMS files could not be fetched"}
            raise RuntimeError("no FIRMS file could be fetched; showing EONET wildfires instead")
        if changed or not len(self.fire_data):
            data = firms.FireData()
            for sat, text in texts:
                n = data.add_csv(text, sat)
                status[sat]["rows"] = n
                if n == 0 and status[sat].get("ok"):
                    status[sat]["note"] = "no detections published in the last 24 h"
            data.finish()
            self.fire_data = data
        else:
            for sat, _ in texts:
                status[sat]["rows"] = self.fire_data.sources.get(sat, 0)
        total = len(self.fire_data)
        self.fire_meta = {"mode": "firms" if total else "fallback", "sources": status, "keyed": bool(key),
                          "newest": self.fire_data.newest or None, "oldest": self.fire_data.oldest or None,
                          "reason": None if total else "FIRMS returned no detections"}
        labels = [f"{firms.SATELLITES[s]['label']} {status[s].get('rows', 0):,}" for s in status]
        return total, ", ".join(labels)

    def fires_payload(self, bbox, zoom: float, min_conf: int = 0) -> dict:
        data = self.fire_data
        meta = dict(self.fire_meta)
        meta["source"] = self.source_info("fires")
        st = self.state["fires"]
        if not len(data) and st.ok_at == 0 and (st.error is None or st.lock.locked()):
            meta.update(mode="pending", count=0)
            return fc([], **meta)
        if not len(data):
            # FIRMS unavailable: EONET wildfire events stand in (they come with their own layer text)
            feats = []
            for e in self.eonet_events:
                if e["category"] != "wildfires" or not geo.in_bbox(e["lat"], e["lon"], bbox):
                    continue
                feats.append(feature(point(e["lon"], e["lat"]), {
                    "id": e["id"], "kind": "event", "t": int(e["time"] or 0), "title": e["title"],
                    "mag": e["magnitude"], "unit": e["unit"] or ""}))
            meta.update(mode="fallback", count=len(feats))
            return fc(feats, **meta)
        size = firms.cell_for_zoom(zoom)
        if size is None and data.count_in(bbox) > 40000:
            size = 0.1 if 0.1 in data.agg else 0.25
        if size is not None:
            feats = []
            for c in data.grid(bbox, size):
                feats.append(feature(point(c["lon"], c["lat"]), {
                    "kind": "cell", "n": c["n"], "f": round(c["frp_max"], 1), "s": round(c["frp_sum"]),
                    "t": int(c["t"]), "w": c["cell"][0], "so": c["cell"][1], "size": size}))
            meta.update(mode="grid", cell=size, count=sum(f["properties"]["n"] for f in feats), cells=len(feats))
            return fc(feats, **meta)
        idx = data.indices_in(bbox, min_conf=min_conf)
        total = len(idx)
        if total > 20000:
            idx.sort(key=lambda i: -data.frp[i])
            idx = idx[:20000]
        feats = []
        for i in idx:
            code = data.sat_code(i)
            la, lo = round(data.lat[i], 5), round(data.lon[i], 5)
            feats.append(feature({"type": "Point", "coordinates": [lo, la]}, {
                "kind": "fire", "id": firms.fire_id(code, la, lo, data.t[i]), "f": round(data.frp[i], 1),
                "b": round(data.bright[i], 1), "c": firms.confidence_rank(data.conf[i]), "s": code,
                "t": int(data.t[i]), "nt": data.night[i]}))
        meta.update(mode="points", count=total, shown=len(feats))
        return fc(feats, **meta)

    def fire_detail(self, fid: str) -> dict | None:
        data = self.fire_data
        i = data.find(fid)
        if i is None:
            return None
        row = data.row(i)
        near = data.near(row["lat"], row["lon"], 5.0, 0)
        others = [j for _, j in near if j != i]
        row["nearby_count"] = len(others)
        row["nearby_frp"] = round(sum(data.frp[j] for j in others), 1)
        c = countries.at(row["lat"], row["lon"])
        row["country"] = c["name"] if c else None
        return row

    # ===================================================================================== EONET
    def refresh_eonet(self):
        a = self.net.get_json(eonet.API, params={"status": "open", "category": eonet.NON_FIRE}, timeout=30, retries=1)
        b = self.net.get_json(eonet.API, params={"status": "open", "category": "wildfires", "days": 60},
                              timeout=30, retries=1)
        items = eonet.parse_events(a) + eonet.parse_events(b)
        seen = set()
        uniq = []
        for e in sorted(items, key=lambda x: -(x["time"] or 0)):
            if e["id"] not in seen:
                seen.add(e["id"])
                uniq.append(e)
        self.eonet_events = uniq
        self.rebuild_volcano_activity()
        self.rebuild_storms()
        by_cat: dict[str, int] = {}
        for e in uniq:
            by_cat[e["category_label"]] = by_cat.get(e["category_label"], 0) + 1
        note = ", ".join(f"{v} {k.lower()}" for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1]))
        return len(uniq), note

    def natural_fc(self, cats: set[str] | None) -> dict:
        feats = []
        for e in self.eonet_events:
            if cats and e["category"] not in cats:
                continue
            if len(e["track"]) > 1 and e["category"] == "severeStorms":
                coords = [[p["lon"], p["lat"]] for p in e["track"]]
                feats.append(feature({"type": "LineString", "coordinates": coords},
                                     {"id": e["id"], "role": "track", "cat": e["category"]}))
            feats.append(feature(point(e["lon"], e["lat"]), {
                "id": e["id"], "role": "event", "cat": e["category"], "kind": e["kind"], "title": e["title"],
                "t": int(e["time"] or 0), "mag": e["magnitude"], "unit": eonet.unit_label(e["unit"])}))
        return fc(feats, count=sum(1 for f in feats if f["properties"]["role"] == "event"), source=self.source_info("eonet"))

    # ===================================================================================== GDACS
    def refresh_gdacs(self):
        now = time.time()
        lists = []
        errors = []
        try:
            lists.append(gdacs.parse_events(self.net.get_json(gdacs.EVENTS_URL, timeout=25, retries=1)))
        except (NetError, ValueError) as err:
            errors.append(_err(err))
        try:
            frm = day_utc(now - 30 * 86400)
            to = day_utc(now + 86400)
            lists.append(gdacs.parse_events(self.net.get_json(
                gdacs.SEARCH_URL, params={"eventlist": "EQ;TC;FL;VO;DR;WF", "fromDate": frm, "toDate": to,
                                          "alertlevel": "Orange;Red"}, timeout=25, retries=1)))
        except (NetError, ValueError) as err:
            errors.append(_err(err))
        storms_ok = True
        try:
            tc = self.net.get_json(gdacs.MAP_URL, params={"eventtype": "TC"}, timeout=25, retries=1)
            self.gdacs_storms = gdacs.parse_tc_map(tc)
            lists.append([s["event"] for s in self.gdacs_storms if s.get("event")])
        except (NetError, ValueError) as err:
            storms_ok = False
            errors.append(_err(err))
        if not lists or (self.in_use("gdacs") and now - self._gdacs_rss_at > 1800):
            try:
                self._gdacs_rss = gdacs.parse_rss(self.net.get_text(gdacs.RSS_URL, timeout=25, retries=1))
                self._gdacs_rss_at = now
            except NetError as err:
                errors.append(_err(err))
        if not lists and not self._gdacs_rss:
            raise RuntimeError("; ".join(errors) or "GDACS answered nothing")
        known = {e["id"] for lst in lists for e in lst}
        extra = self._gdacs_rss if not lists else [r for r in self._gdacs_rss if r["id"] in known]
        merged = gdacs.merge_events(*lists, extra)
        # keep current events and recent orange/red ones
        keep = {}
        for k, e in merged.items():
            end = e.get("to") or e.get("from") or 0
            if e.get("current") or (e["alert"] in ("Orange", "Red") and end >= now - 30 * 86400):
                keep[k] = e
        self.gdacs_events = keep
        self.rebuild_volcano_activity()
        self.rebuild_storms()
        counts: dict[str, int] = {}
        for e in keep.values():
            counts[e["alert"]] = counts.get(e["alert"], 0) + 1
        note = ", ".join(f"{counts.get(a, 0)} {a.lower()}" for a in ("Red", "Orange", "Green"))
        if errors:
            note += f" (partial: {errors[0][:80]})"
        if not storms_ok:
            note += ", cyclone tracks unavailable"
        return len(keep), note

    def disasters_fc(self, types: set[str] | None, min_alert: int) -> dict:
        feats = []
        now = time.time()
        for e in sorted(self.gdacs_events.values(), key=lambda x: (gdacs.ALERT_RANK.get(x["alert"].lower(), 1), x.get("to") or 0)):
            if types and e["type"] not in types:
                continue
            rank = gdacs.ALERT_RANK.get(e["alert"].lower(), 1)
            if rank < min_alert:
                continue
            end = e.get("to") or e.get("from") or 0
            feats.append(feature(point(e["lon"], e["lat"]), {
                "id": e["id"], "type": e["type"], "kind": e["kind"], "alert": e["alert"], "rank": rank,
                "cur": 1 if gdacs.ongoing(e, now) else 0, "name": e["name"], "short": gdacs.short_name(e), "t": int(end),
                "sev": e.get("severity_text") or "", "age": round((now - end) / 86400.0, 1) if end else None}))
        return fc(feats, count=len(feats), source=self.source_info("gdacs"))

    def gdacs_duplicates_usgs(self, e: dict) -> bool:
        """A GDACS earthquake alert that repeats a USGS event we already show."""
        if e.get("type") != "EQ":
            return False
        sid = e.get("source_id")
        if sid and f"usgs:{sid}" in self.quakes:
            return True
        t = e.get("from") or 0
        for q in self.quakes.values():
            if abs(q["time"] - t) < 900 and geo.haversine_km(q["lat"], q["lon"], e["lat"], e["lon"]) < 100:
                return True
        return False

    def disaster_geometry(self, eid: str) -> dict:
        e = self.gdacs_events.get(eid)
        # the feed's link is third-party data: only ever fetched on gdacs.org, and redirects aren't followed
        url = gdacs.gdacs_url(e.get("geometry_url")) if e else None
        if not url:
            parts = eid.split(":")
            if len(parts) != 3 or parts[1] not in gdacs.TYPES or not (parts[2].isascii() and parts[2].isdigit()):
                return fc([])
            params = {"eventtype": parts[1], "eventid": parts[2]}
            if e and e.get("episodeid"):
                params["episodeid"] = e["episodeid"]
            data = self.net.get_json(gdacs.GEOMETRY_URL, params=params, timeout=15, ttl=1800, allow_redirects=False)
        else:
            data = self.net.get_json(url, timeout=15, ttl=1800, allow_redirects=False)
        return gdacs.simplify_event_geometry(data, 0.01)

    # ===================================================================================== NHC
    def refresh_nhc(self):
        data = self.net.get_json(nhc.CURRENT_URL, timeout=15, retries=1)
        storms = nhc.parse_current(data)
        if storms and (self._nhc_layers is None or time.time() - self._nhc_layers_at > 86400):
            try:
                self._nhc_layers = nhc.layer_ids(self.net.get_json(nhc.MAPSERVER, params={"f": "json"}, timeout=20))
                self._nhc_layers_at = time.time()
            except (NetError, ValueError) as err:
                self.log.info("NHC map service layer list unavailable: %s", _err(err))
        for s in storms:
            ids = (self._nhc_layers or {}).get(s["bin"] or "", {})
            s["cone"] = s["track"] = s["past"] = None
            s["points"] = []
            for role in ("cone", "track", "past", "points"):
                lid = ids.get(role)
                if lid is None:
                    continue
                try:
                    gj = self.net.get_json(f"{nhc.MAPSERVER}/{lid}/query",
                                           params={"where": "1=1", "outFields": "*", "f": "geojson"},
                                           timeout=20, ttl=600)
                except (NetError, ValueError):
                    continue
                feats = nhc.parse_layer_geojson(gj)
                if role == "points":
                    s["points"] = nhc.forecast_points(feats)
                elif feats:
                    s[role] = feats[0]["geometry"]
        self.nhc_storms = storms
        self.rebuild_storms()
        return len(storms), (", ".join(f"{s['classification_label']} {s['name']}" for s in storms)
                             or "no active Atlantic or Pacific storms")

    # ===================================================================================== storms
    def rebuild_storms(self) -> None:
        """Merge NHC, GDACS and EONET cyclones into one list (NHC wins for its basins)."""
        now = time.time()
        out = []
        names = set()
        for s in self.nhc_storms:
            cat = storm_category(s["wind_kmh"])
            track = [{"lat": s["lat"], "lon": s["lon"], "t": s["updated"], "forecast": False,
                      "label": "Now", "wind_kmh": s["wind_kmh"]}]
            track += [{"lat": p["lat"], "lon": p["lon"], "t": p["t"], "forecast": True, "label": p["label"],
                       "wind_kmh": p["wind_kmh"], "tau": p["tau"]} for p in s.get("points") or [] if p.get("tau")]
            out.append({
                "id": s["id"], "source": "nhc", "source_label": "NOAA National Hurricane Center",
                "name": s["name"], "title": f"{s['classification_label']} {s['name']}",
                "basin": s["basin"], "wind_kmh": s["wind_kmh"], "wind_kt": s["wind_kt"], "category": cat,
                "category_label": category_label(cat), "pressure_mb": s["pressure_mb"],
                "movement_dir": s["movement_dir"], "movement_kt": s["movement_kt"],
                "lat": s["lat"], "lon": s["lon"], "time": s["updated"], "active": True, "alert": None,
                "countries": [], "track": track, "segments": [], "cone": s.get("cone"),
                "track_line": s.get("track"), "past_line": s.get("past"), "buffers": [],
                "links": [l for l in ({"text": "NHC public advisory", "href": s["advisory_url"]},
                                      {"text": "NHC forecast graphics", "href": s["graphics_url"]}) if l["href"]],
                "classification": s["classification"], "advisory_number": s["advisory_number"]})
            names.add(s["name"].lower())
        for g in self.gdacs_storms:
            e = g["event"]
            name = g["name"] or title_case_name(e.get("event_name") or "")
            if name.lower() in names:
                continue
            names.add(name.lower())
            cur = g.get("current") or {}
            wind = e.get("severity")
            cat = storm_category(wind)
            t = g.get("advisory_time") or e.get("to")
            active = bool(t and now - t <= STORM_ACTIVE_HOURS * 3600)
            title_kind = storm_word(cat, cur.get("lat", e["lat"]), cur.get("lon", e["lon"]))
            out.append({
                "id": e["id"], "source": "gdacs", "source_label": f"GDACS ({e.get('source') or 'JTWC'})",
                "name": name, "title": f"{title_kind} {name}", "basin": None,
                "wind_kmh": round(wind) if wind else None, "wind_kt": round(wind / 1.852) if wind else None,
                "category": cat, "category_label": category_label(cat), "pressure_mb": None,
                "movement_dir": None, "movement_kt": None,
                "lat": cur.get("lat", e["lat"]), "lon": cur.get("lon", e["lon"]), "time": t, "active": active,
                "alert": e.get("alert"), "countries": [c["name"] for c in e.get("countries") or []],
                "track": [{"lat": p["lat"], "lon": p["lon"], "t": p["t"], "forecast": p["forecast"], "label": p["label"]}
                          for p in g["points"]],
                "segments": g["segments"], "cone": g["cone"], "track_line": None, "past_line": None,
                "buffers": g["buffers"],
                "links": [{"text": "GDACS report", "href": e.get("report_url")}], "classification": None,
                "advisory_number": None, "from": e.get("from")})
        for ev in self.eonet_events:
            if ev["category"] != "severeStorms":
                continue
            short = ev["title"].lower().replace("tropical cyclone", "").replace("typhoon", "").replace(
                "hurricane", "").replace("tropical storm", "").replace("super", "").strip()
            if short in names or not short:
                continue
            names.add(short)
            mags = ev.get("magnitude")
            kmh = round(mags * 1.852) if mags and ev.get("unit") == "kts" else None
            cat = storm_category(kmh)
            active = bool(ev["time"] and now - ev["time"] <= STORM_ACTIVE_HOURS * 3600)
            out.append({
                "id": ev["id"], "source": "eonet", "source_label": "NASA EONET",
                "name": title_case_name(short), "title": ev["title"], "basin": None,
                "wind_kmh": kmh, "wind_kt": round(mags) if mags and ev.get("unit") == "kts" else None,
                "category": cat, "category_label": category_label(cat), "pressure_mb": None,
                "movement_dir": None, "movement_kt": None, "lat": ev["lat"], "lon": ev["lon"], "time": ev["time"],
                "active": active, "alert": None, "countries": [],
                "track": [{"lat": p["lat"], "lon": p["lon"], "t": p["t"], "forecast": False,
                           "label": None, "wind_kmh": round(p["mag"] * 1.852) if p["mag"] else None} for p in ev["track"]],
                "segments": [], "cone": None, "track_line": None, "past_line": None, "buffers": [],
                "links": [{"text": s["name"], "href": s["url"]} for s in ev["sources"]][:3],
                "classification": None, "advisory_number": None})
        out.sort(key=lambda s: (not s["active"], -(s["wind_kmh"] or 0)))
        self.storms = out
        self.versions["storms"] += 1

    def storms_fc(self, include_ended: bool) -> dict:
        feats = []
        shown = [s for s in self.storms if s["active"] or include_ended]
        for s in shown:
            sid = s["id"]
            act = 1 if s["active"] else 0
            if s.get("cone") and act:
                feats.append(feature(s["cone"], {"role": "cone", "sid": sid}))
            for b in s.get("buffers") or []:
                if act:
                    feats.append(feature(b["geometry"], {"role": "wind", "sid": sid, "kmh": b["kmh"]}))
            if s.get("past_line"):
                feats.append(feature(s["past_line"], {"role": "track", "sid": sid, "fc": 0, "cls": "", "act": act}))
            if s.get("track_line"):
                feats.append(feature(s["track_line"], {"role": "track", "sid": sid, "fc": 1, "cls": "", "act": act}))
            if s["segments"]:
                for seg in s["segments"]:
                    feats.append(feature({"type": "LineString", "coordinates": seg["coords"]},
                                         {"role": "track", "sid": sid, "fc": 1 if seg["forecast"] else 0,
                                          "cls": seg["class"] or "", "act": act}))
            elif len(s["track"]) > 1 and not s.get("past_line"):
                obs = [[p["lon"], p["lat"]] for p in s["track"] if not p["forecast"]]
                fcs = [[p["lon"], p["lat"]] for p in s["track"] if p["forecast"]]
                if len(obs) > 1:
                    feats.append(feature({"type": "LineString", "coordinates": obs},
                                         {"role": "track", "sid": sid, "fc": 0, "cls": "", "act": act}))
                if fcs:
                    feats.append(feature({"type": "LineString", "coordinates": (obs[-1:] + fcs)},
                                         {"role": "track", "sid": sid, "fc": 1, "cls": "", "act": act}))
            for p in s["track"]:
                feats.append(feature(point(p["lon"], p["lat"]), {
                    "role": "pt", "sid": sid, "fc": 1 if p["forecast"] else 0, "t": int(p["t"] or 0),
                    "label": p.get("label") or "", "act": act}))
            feats.append(feature(point(s["lon"], s["lat"]), {
                "role": "now", "id": sid, "sid": sid, "name": s["name"], "title": s["title"],
                "cat": s["category"] if s["category"] is not None else -2, "wind": s["wind_kmh"] or 0,
                "alert": s["alert"] or "", "act": act, "t": int(s["time"] or 0),
                "label": f"{s['name']}" + (f", cat {s['category']}" if s["category"] and s["category"] >= 1 else "")}))
        return fc(feats, count=len(shown), active=sum(1 for s in self.storms if s["active"]),
                  source={"gdacs": self.source_info("gdacs"), "nhc": self.source_info("nhc")},
                  storms=[{"id": s["id"], "name": s["name"], "title": s["title"], "active": s["active"],
                           "category": s["category"], "wind_kmh": s["wind_kmh"]} for s in shown])

    def storm(self, sid: str) -> dict | None:
        for s in self.storms:
            if s["id"] == sid:
                return s
        return None

    # ================================================================================= volcanoes
    def load_gvp(self) -> None:
        path = self.m.data_dir / "volcanoes.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = data.get("volcanoes") or []
        index = geo.GridIndex(2.0)
        for v in items:
            index.add(v["lat"], v["lon"], v)
        with self.lock:
            self.volcano_list = items
            self.volcano_by_id = {v["id"]: v for v in items}
            self.volcano_index = index
            self.volcano_names = [(norm(v["name"]), v) for v in items]
        self.rebuild_volcano_activity()
        self.m.status("hazards.gvp", ok=True, count=len(items), note=f"{len(items):,} Holocene volcanoes")

    def refresh_hans(self):
        elevated = self.net.get_json(volcanoes.HANS_ELEVATED, timeout=25, retries=1)
        try:
            cap = self.net.get_json(volcanoes.HANS_CAP, timeout=25, retries=1)
        except (NetError, ValueError):
            cap = []
        self.hans = volcanoes.parse_hans(elevated if isinstance(elevated, list) else [],
                                         cap if isinstance(cap, list) else [])
        self.rebuild_volcano_activity()
        levels = {}
        for h in self.hans.values():
            levels[h["color"] or "?"] = levels.get(h["color"] or "?", 0) + 1
        return len(self.hans), ", ".join(f"{v} {k.lower()}" for k, v in levels.items()) or "no elevated US volcanoes"

    def rebuild_volcano_activity(self) -> None:
        by_vnum = {v["vnum"]: v for v in self.volcano_list}
        act: dict[str, dict] = {}
        extra: list[dict] = []

        def slot(v):
            return act.setdefault(v["id"], {})

        for vnum, h in self.hans.items():
            v = by_vnum.get(vnum)
            if v:
                slot(v)["hans"] = h
            elif h.get("lat") is not None:
                extra.append({"id": f"gvp:{vnum}", "vnum": vnum, "name": h["name"], "lat": h["lat"], "lon": h["lon"],
                              "country": "United States", "activity": {"hans": h}})
        for e in self.eonet_events:
            if e["category"] != "volcanoes":
                continue
            v = by_vnum.get(e.get("vnum") or "") or volcanoes.match_by_name(
                volcanoes.clean_report_name(e["title"]), self._near_volcanoes(e["lat"], e["lon"]), e["lat"], e["lon"])
            info = {"id": e["id"], "title": e["title"], "time": e["time"], "start": e["start"], "link": e["link"],
                    "sources": e["sources"]}
            if v:
                slot(v)["eonet"] = info
            else:
                extra.append({"id": e["id"], "vnum": e.get("vnum"), "name": volcanoes.clean_report_name(e["title"]),
                              "lat": e["lat"], "lon": e["lon"], "country": None, "activity": {"eonet": info}})
        for g in self.gdacs_events.values():
            if g["type"] != "VO":
                continue
            v = volcanoes.match_by_name(volcanoes.clean_report_name(g["name"]), self._near_volcanoes(g["lat"], g["lon"]),
                                        g["lat"], g["lon"])
            info = {"id": g["id"], "alert": g["alert"], "name": g["name"], "time": g.get("to") or g.get("from"),
                    "report_url": g.get("report_url"), "current": g.get("current")}
            if v:
                slot(v)["gdacs"] = info
            else:
                extra.append({"id": g["id"], "vnum": None, "name": volcanoes.clean_report_name(g["name"]),
                              "lat": g["lat"], "lon": g["lon"], "country": g.get("country"), "activity": {"gdacs": info}})
        for a in act.values():
            a["rank"] = volcanoes.activity_rank(a)
        for x in extra:
            x["activity"]["rank"] = volcanoes.activity_rank(x["activity"])
        with self.lock:
            self.volcano_activity = act
            self.extra_volcanoes = extra
            self.versions["volcanoes"] += 1

    def _near_volcanoes(self, lat: float, lon: float) -> list[dict]:
        return [v for _, v in self.volcano_index.near(lat, lon, 300.0, 30)]

    def volcanoes_fc(self, scope: str) -> dict:
        feats = []
        act = self.volcano_activity
        active_n = 0
        for v in self.volcano_list:
            a = act.get(v["id"])
            rank = a["rank"] if a else 0
            if rank:
                active_n += 1
            if scope == "active" and not rank:
                continue
            feats.append(feature(point(v["lon"], v["lat"]), {
                "id": v["id"], "name": v["name"], "e": v["elevation_m"], "y": v["last_eruption"] if v["last_eruption"] is not None else -99999,
                "a": rank, "ty": v["type"] or ""}))
        for x in self.extra_volcanoes:
            active_n += 1
            feats.append(feature(point(x["lon"], x["lat"]), {
                "id": x["id"], "name": x["name"], "e": None, "y": -99999, "a": x["activity"]["rank"] or 2, "ty": ""}))
        feats.sort(key=lambda f: f["properties"]["a"])
        return fc(feats, count=len(feats), active=active_n, base=len(self.volcano_list),
                  source={"gvp": {"ok": bool(self.volcano_list)}, "hans": self.source_info("hans"),
                          "eonet": self.source_info("eonet")})

    def volcano(self, vid: str) -> dict | None:
        v = self.volcano_by_id.get(vid)
        if v:
            out = dict(v)
            out["activity"] = self.volcano_activity.get(vid) or {}
            return out
        for x in self.extra_volcanoes:
            if x["id"] == vid:
                return dict(x)
        return None

    # ====================================================================================== NWS
    def refresh_nws(self):
        active = self.in_use("nws")
        params = None if active else {"severity": "Severe,Extreme", "status": "actual"}
        data = self.net.get_json(nws.ALERTS_URL, params=params, headers=nws.HEADERS, timeout=30, retries=1)
        alerts = nws.parse_alerts(data, time.time())
        alerts.sort(key=lambda a: (-nws.rank(a["severity"]), a["onset"] or 0))
        self.nws_alerts = alerts
        self.nws_full = active
        self._zone_missing = {zid for a in alerts if not a["geometry"] for _, zid in a["zones"]
                              if zid not in self.nws_zones}
        self.versions["warnings"] += 1
        with_poly = sum(1 for a in alerts if a["geometry"])
        return len(alerts), (f"{len(alerts)} alerts, {with_poly} with storm polygons"
                             + ("" if active else " (severe and extreme only while idle)"))

    def load_zones(self) -> None:
        self.store.script("CREATE TABLE IF NOT EXISTS hazards_nws_zones (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                          "data TEXT NOT NULL, fetched_at REAL NOT NULL)")
        zones = {}
        for row in self.store.query("SELECT id, data FROM hazards_nws_zones"):
            try:
                zones[row["id"]] = json.loads(row["data"])
            except ValueError:
                continue
        self.nws_zones = zones

    def refresh_zones(self):
        """Fetch outlines for zones that current alerts name but we have never seen (most severe first)."""
        wanted = []
        for a in self.nws_alerts:
            if a["geometry"]:
                continue
            for kind, zid in a["zones"]:
                if zid not in self.nws_zones and (kind, zid) not in wanted:
                    wanted.append((kind, zid))
        if not wanted:
            self._zone_missing = set()
            return len(self.nws_zones), f"{len(self.nws_zones)} zone outlines cached"
        got = 0
        deadline = time.time() + 15
        for kind, zid in wanted[:40]:
            if time.time() > deadline:
                break
            try:
                data = self.net.get_json(nws.ZONE_URL.format(type=kind, id=zid), headers=nws.HEADERS, timeout=15, retries=0)
                zone = nws.parse_zone(data) or {"id": zid, "name": zid, "fine": None}
            except NetError as err:
                if err.status in (404, 400):
                    zone = {"id": zid, "name": zid, "fine": None}
                else:
                    raise
            zone["kind"] = kind
            self.nws_zones[zid] = zone
            self.store.execute("INSERT OR REPLACE INTO hazards_nws_zones(id, kind, data, fetched_at) VALUES(?,?,?,?)",
                               (zid, kind, json.dumps(zone, separators=(",", ":")), time.time()))
            got += 1
        self._zone_missing = {zid for _, zid in wanted if zid not in self.nws_zones}
        self.versions["warnings"] += 1
        return len(self.nws_zones), f"{len(self.nws_zones)} zone outlines cached, {len(self._zone_missing)} to fetch"

    # =============================================================================== MeteoAlarm
    def load_ma_regions(self) -> bool:
        path = self.m.data_dir / "meteoalarm_regions.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        self.ma_regions = {"regions": data.get("regions") or {}, "aliases": data.get("aliases") or {},
                           "file": data.get("file"), "built_at": data.get("built_at")}
        self.versions["warnings"] += 1
        return True

    def refresh_ma_regions(self):
        """Region outlines for EMMA_ID / NUTS geocodes (MeteoAlarm's published geocodes file; ~30 MB,
        fetched only when the warnings layer is used and the file has changed upstream)."""
        out = self.m.data_dir / "meteoalarm_regions.json"
        tree = self.net.get_json(meteoalarm.DOCS_TREE, timeout=20, retries=1)
        geo_name, alias_name = meteoalarm.latest_files(tree if isinstance(tree, list) else [])
        if not geo_name:
            raise RuntimeError("MeteoAlarm geocodes file not found in the documents repository")
        if self.ma_regions.get("file") == geo_name and out.exists():
            return len(self.ma_regions["regions"]), f"{len(self.ma_regions['regions']):,} regions ({geo_name})"
        raw = self.m.raw_dir / "meteoalarm"
        raw.mkdir(parents=True, exist_ok=True)
        geo_path = self.net.download(meteoalarm.DOCS_RAW.format(name=geo_name), raw / geo_name,
                                     max_age_hours=24 * 365, timeout=300, min_bytes=1_000_000)
        aliases = None
        if alias_name:
            try:
                ap = self.net.download(meteoalarm.DOCS_RAW.format(name=alias_name), raw / alias_name,
                                       max_age_hours=24 * 365, timeout=60, min_bytes=100)
                aliases = ap.read_text(encoding="utf-8-sig", errors="replace")
            except NetError:
                aliases = None
        data = json.loads(geo_path.read_text(encoding="utf-8"))
        built = meteoalarm.build_regions(data, aliases)
        del data
        built["file"] = geo_name
        built["built_at"] = time.time()
        self.m.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_bytes(json_bytes(built))
        tmp.replace(out)
        for p in raw.glob("MeteoAlarm_Geocodes*"):
            if p.name not in (geo_name, alias_name, geo_name + ".meta.json", f"{alias_name}.meta.json"):
                try:
                    p.unlink()
                except OSError:
                    pass
        self.ma_regions = built
        self.versions["warnings"] += 1
        return len(built["regions"]), f"{len(built['regions']):,} regions, {len(built['aliases']):,} aliases ({geo_name})"

    def refresh_meteoalarm(self):
        now = time.time()
        errors = []
        fetched = 0
        for slug in self.ma_countries:
            last = self._ma_feed_at.get(slug, 0)
            big = slug in ("germany", "switzerland")
            if now - last < (3500 if big else 1700):
                continue
            try:
                text = self.net.get_text(meteoalarm.FEED_URL.format(slug=slug), timeout=40, retries=1)
            except NetError as err:
                errors.append(f"{slug}: {_err(err)}")
                continue
            updated = dict(self.ma_warnings)
            updated[slug] = meteoalarm.parse_atom(text, slug, now)
            self.ma_warnings = updated
            self._ma_feed_at[slug] = now
            self.versions["warnings"] += 1
            fetched += 1
        # drop warnings that expired since their feed was read
        self.ma_warnings = {slug: [w for w in items if not w["expires"] or w["expires"] >= now]
                            for slug, items in self.ma_warnings.items()}
        self.versions["warnings"] += 1
        total = sum(len(v) for v in self.ma_warnings.values())
        if errors and not fetched and not total:
            raise RuntimeError("; ".join(errors[:3]))
        note = f"{total:,} warnings from {len(self.ma_warnings)} countries"
        if errors:
            note += f", {len(errors)} feeds failed"
        return total, note

    # ============================================================================ warnings layer
    def _warning_features(self, level: str) -> list[tuple]:
        """[(bbox, rank, upcoming, source, feature)] for every mapped warning area, cached per version."""
        key = f"{level}"
        cached = self._warn_features.get(key)
        version = self.versions["warnings"]
        if cached and cached[0] == version and cached[1] == int(time.time() // 300):
            return cached[2]
        now = time.time()
        out = []
        geom_key = "fine" if level == "fine" else "coarse"
        live_nws = [a for a in self.nws_alerts if not a["expires"] or a["expires"] >= now]
        # NWS: storm polygons per alert, zone fills per zone (top alert wins)
        by_zone: dict[str, list] = {}
        for a in live_nws:
            rank = nws.rank(a["severity"])
            up = 1 if (a["onset"] and a["onset"] > now + 600) else 0
            if a["geometry"]:
                out.append((geometry_bboxes(a["geometry"]), rank, up, "nws",
                            feature(a["geometry"], self._warn_props(a, [a], rank, up))))
                continue
            for _, zid in a["zones"]:
                by_zone.setdefault(zid, []).append(a)
        for zid, alerts in by_zone.items():
            z = self.nws_zones.get(zid)
            if not z or not z.get("fine"):
                continue
            top = alerts[0]
            rank = nws.rank(top["severity"])
            up = 1 if all(a["onset"] and a["onset"] > now + 600 for a in alerts) else 0
            geom = z.get(geom_key) or z.get("fine")
            props = self._warn_props(top, alerts, rank, up)
            props["area"] = z.get("name") or props["area"]
            out.append((geometry_bboxes(geom), rank, up, "nws", feature(geom, props)))
        # MeteoAlarm: polygons per warning, regions per geocode
        regions = self.ma_regions.get("regions") or {}
        aliases = self.ma_regions.get("aliases") or {}
        by_region: dict[str, list] = {}
        for slug, items in self.ma_warnings.items():
            for w in sorted(items, key=lambda x: (-SEVERITY_RANK.get(x["severity"].lower(), 0), x["onset"] or 0)):
                if w["expires"] and w["expires"] < now:
                    continue
                rank = SEVERITY_RANK.get(w["severity"].lower(), 0)
                up = 1 if (w["onset"] and w["onset"] > now + 600) else 0
                if w["polygons"]:
                    geom = {"type": "Polygon", "coordinates": [w["polygons"][0]]} if len(w["polygons"]) == 1 else \
                        {"type": "MultiPolygon", "coordinates": [[r] for r in w["polygons"]]}
                    geom = simplify_geometry(geom, 0.004 if level == "fine" else 0.03, 4 if level == "fine" else 3)
                    if geom:
                        out.append((geometry_bboxes(geom), rank, up, "meteoalarm",
                                    feature(geom, self._warn_props(w, [w], rank, up))))
                    continue
                for _, code in w["codes"]:
                    rc = code if code in regions else aliases.get(code)
                    if rc:
                        by_region.setdefault(rc, []).append(w)
        for rc, items in by_region.items():
            r = regions.get(rc)
            if not r:
                continue
            items.sort(key=lambda x: (-SEVERITY_RANK.get(x["severity"].lower(), 0), x["onset"] or 0))
            top = items[0]
            rank = SEVERITY_RANK.get(top["severity"].lower(), 0)
            up = 1 if all(w["onset"] and w["onset"] > now + 600 for w in items) else 0
            props = self._warn_props(top, items, rank, up)
            props["area"] = r.get("name") or props["area"]
            geom = r.get(geom_key) or r.get("fine")
            out.append((geometry_bboxes(geom), rank, up, "meteoalarm", feature(geom, props)))
        out.sort(key=lambda x: (x[1], -x[2]))
        self._warn_features[key] = (version, int(now // 300), out)
        return out

    @staticmethod
    def _warn_props(top: dict, alerts: list[dict], rank: int, up: int) -> dict:
        return {"id": top["id"], "ids": ",".join(a["id"] for a in alerts[:12]), "n": len(alerts),
                "src": top["source"], "sev": rank, "ev": top["event"], "lvl": top.get("level") or "",
                "area": clean(top.get("area"), 80), "up": up, "exp": int(top["expires"] or 0),
                "on": int(top["onset"] or 0)}

    def warnings_fc(self, bbox, zoom: float, min_rank: int, sources: set[str]) -> dict:
        level = "fine" if zoom >= 5 else "coarse"
        feats = []
        for fb, rank, up, src, f in self._warning_features(level):
            if rank < min_rank or src not in sources:
                continue
            if not bboxes_intersect(fb, bbox):
                continue
            feats.append(f)
        ma_total = sum(len(v) for v in self.ma_warnings.values())
        return fc(feats, count=len(feats), nws=len(self.nws_alerts), meteoalarm=ma_total,
                  ma_countries=len(self.ma_warnings), ma_wanted=len(self.ma_countries), nws_full=self.nws_full,
                  zones_pending=len(self._zone_missing), regions=len(self.ma_regions.get("regions") or {}),
                  source={"nws": self.source_info("nws"), "meteoalarm": self.source_info("meteoalarm"),
                          "regions": self.source_info("ma_regions")})

    def meteoalarm_iso(self) -> set[str]:
        return {meteoalarm.COUNTRIES[s][0] for s in self.ma_countries}

    def meteoalarm_slug(self, iso2: str) -> str | None:
        return meteoalarm.BY_ISO.get(iso2)

    def all_warnings(self) -> list[dict]:
        now = time.time()
        items = list(self.nws_alerts)
        for v in self.ma_warnings.values():
            items.extend(v)
        return [w for w in items if not w.get("expires") or w["expires"] >= now]

    def warning(self, wid: str) -> dict | None:
        if wid.startswith("nws:"):
            for a in self.nws_alerts:
                if a["id"] == wid:
                    return a
        else:
            for items in self.ma_warnings.values():
                for w in items:
                    if w["id"] == wid:
                        return w
        return None

    def warning_geometry(self, w: dict) -> dict | None:
        """The outline of one warning (its polygon, or the union of its zones or regions)."""
        if w["source"] == "nws":
            if w.get("geometry"):
                return w["geometry"]
            polys = []
            for _, zid in w["zones"]:
                g = (self.nws_zones.get(zid) or {}).get("fine")
                if g:
                    polys.extend([g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"])
            return {"type": "MultiPolygon", "coordinates": polys} if polys else None
        if w["polygons"]:
            return {"type": "MultiPolygon", "coordinates": [[r] for r in w["polygons"]]}
        regions = self.ma_regions.get("regions") or {}
        aliases = self.ma_regions.get("aliases") or {}
        polys = []
        for _, code in w["codes"]:
            r = regions.get(code) or regions.get(aliases.get(code, ""))
            if r and r.get("fine"):
                g = r["fine"]
                polys.extend([g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"])
        return {"type": "MultiPolygon", "coordinates": polys} if polys else None

    def warnings_at(self, lat: float, lon: float) -> list[dict]:
        out = []
        for fb, rank, up, src, f in self._warning_features("fine"):
            g = f["geometry"]
            if not g:
                continue
            if not bboxes_contain(fb, lat, lon):
                continue
            if geo.point_in_geometry(lon, lat, g):
                for wid in f["properties"]["ids"].split(","):
                    w = self.warning(wid)
                    if w and w not in out:
                        out.append(w)
        out.sort(key=lambda w: (-SEVERITY_RANK.get((w["severity"] or "").lower(), 0), w["onset"] or 0))
        return out

    # =================================================================================== tsunami
    def load_tsunami(self) -> None:
        self.store.script("CREATE TABLE IF NOT EXISTS hazards_tsunami (id TEXT PRIMARY KEY, data TEXT NOT NULL, "
                          "issued REAL, seen_at REAL NOT NULL)")
        rows = self.store.query("SELECT data FROM hazards_tsunami WHERE issued >= ? ORDER BY issued DESC LIMIT 200",
                                (time.time() - 60 * 86400,))
        msgs = []
        for r in rows:
            try:
                msgs.append(json.loads(r["data"]))
            except ValueError:
                continue
        self.tsunami_msgs = msgs

    def refresh_tsunami(self):
        got = 0
        errors = []
        now = time.time()
        for center, (_, url) in tsunami.FEEDS.items():
            try:
                items = tsunami.parse_atom(self.net.get_text(url, timeout=15, retries=1), center)
            except NetError as err:
                errors.append(f"{center}: {_err(err)}")
                continue
            for it in items:
                self.store.execute("INSERT OR REPLACE INTO hazards_tsunami(id, data, issued, seen_at) VALUES(?,?,?,?)",
                                   (it["id"], json.dumps(it), it["issued"] or now, now))
                got += 1
        if errors and not got:
            raise RuntimeError("; ".join(errors))
        self.load_tsunami()
        recent = [m for m in self.tsunami_msgs if (m.get("issued") or 0) >= now - 7 * 86400]
        threats = [m for m in recent if m["category"] in ("Warning", "Watch", "Advisory", "Threat")]
        return len(recent), (f"{len(threats)} warnings, watches or advisories in the past week" if threats
                             else "no tsunami threats in the past week")

    def tsunami_fc(self, days: int) -> dict:
        since = time.time() - days * 86400
        feats = []
        for msg in sorted(self.tsunami_msgs, key=lambda m: m.get("issued") or 0):
            if (msg.get("issued") or 0) < since:
                continue
            feats.append(feature(point(msg["lon"], msg["lat"]), {
                "id": msg["id"], "cat": msg["category"], "t": int(msg.get("issued") or 0), "mag": msg.get("magnitude"),
                "region": msg.get("region") or "", "center": msg["center"]}))
        return fc(feats, count=len(feats), source=self.source_info("tsunami"))

    def tsunami_msg(self, tid: str) -> dict | None:
        for msg in self.tsunami_msgs:
            if msg["id"] == tid:
                return msg
        row = self.store.one("SELECT data FROM hazards_tsunami WHERE id = ?", (tid,))
        if row:
            try:
                return json.loads(row["data"])
            except ValueError:
                return None
        return None

    # ======================================================================================= WHO
    def refresh_who(self):
        data = self.net.get_json(who.API, params=who.query_params(60), timeout=30, retries=1)
        items = who.parse_items(data)
        if not items:
            raise RuntimeError("WHO answered without any outbreak reports")
        items.sort(key=lambda x: -x["published"])
        self.who_items = items
        self.versions["outbreaks"] += 1
        active = self.active_outbreaks(who.ACTIVE_DAYS)
        return len(items), f"{len(active)} active outbreaks (reports in the past {who.ACTIVE_DAYS} days)"

    def active_outbreaks(self, days: int) -> list[dict]:
        """Latest report per outbreak (disease and places) published within ``days``."""
        since = time.time() - days * 86400
        seen = set()
        out = []
        for it in self.who_items:
            if it["published"] < since:
                continue
            key = (who.disease_key(it["disease"]), tuple(sorted(p["iso2"] or p["name"] for p in it["places"])))
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out

    def related_reports(self, item: dict) -> list[dict]:
        key = who.disease_key(item["disease"])
        return [it for it in self.who_items if it["id"] != item["id"] and who.disease_key(it["disease"]) == key][:8]

    def outbreaks_fc(self, days: int) -> dict:
        feats = []
        now = time.time()
        active = self.active_outbreaks(days)
        areas = set()
        for it in active:
            age = (now - it["published"]) / 86400.0
            for p in it["places"]:
                feats.append(feature(point(p["lon"], p["lat"]), {
                    "id": it["id"], "role": "report", "disease": it["disease"], "place": p["name"],
                    "t": int(it["published"]), "hc": 1 if it["high_consequence"] else 0,
                    "multi": len(it["places"]), "age": round(age, 1)}))
                if p.get("iso2"):
                    areas.add((p["iso2"], it["id"], 1 if it["high_consequence"] else 0, round(age, 1)))
        for iso2, oid, hc, age in sorted(areas, key=lambda a: -a[3]):
            shape = countries.shape(iso2)
            if shape:
                feats.insert(0, feature(shape, {"id": oid, "role": "area", "iso2": iso2, "hc": hc, "age": age}))
        return fc(feats, count=len(active), source=self.source_info("who"))

    def outbreak(self, oid: str) -> dict | None:
        for it in self.who_items:
            if it["id"] == oid:
                return it
        return None

    # ================================================================================ countries
    def load_country_shapes(self) -> None:
        path = self.m.data_dir / "countries.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        n = countries.load_shapes(data.get("countries") or [])
        self.versions["outbreaks"] += 1
        self.m.status("hazards.naturalearth", ok=True, count=n, note=f"{n} country outlines")

    # ============================================================================== housekeeping
    def housekeeping(self) -> None:
        now = time.time()
        self.store.execute("DELETE FROM hazards_archive WHERE seen_at < ?", (now - 180 * 86400,))
        self.store.execute("DELETE FROM hazards_tsunami WHERE issued < ?", (now - 120 * 86400,))
        self.store.execute("DELETE FROM hazards_nws_zones WHERE fetched_at < ?", (now - 180 * 86400,))
        n = self.store.scalar("SELECT COUNT(*) FROM hazards_archive") or 0
        if n > 20000:
            self.store.execute("DELETE FROM hazards_archive WHERE rowid IN (SELECT rowid FROM hazards_archive "
                               "ORDER BY seen_at ASC LIMIT ?)", (n - 20000,))

    def archive(self, kind: str, eid: str, entity: dict, data: dict) -> None:
        try:
            self.store.execute(
                "INSERT INTO hazards_archive(kind, id, title, subtitle, lat, lon, time, data, seen_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, id) DO UPDATE SET title=excluded.title, "
                "subtitle=excluded.subtitle, lat=excluded.lat, lon=excluded.lon, time=excluded.time, "
                "data=excluded.data, seen_at=excluded.seen_at",
                (kind, eid, entity.get("title") or "", entity.get("subtitle") or "", entity.get("lat"),
                 entity.get("lon"), entity.get("time"), json.dumps(data, default=str)[:200000], time.time()))
        except Exception as err:  # the archive is a convenience; never fail a request for it
            self.log.debug("hazards archive write failed: %s", err)

    def archived(self, kind: str, eid: str) -> dict | None:
        row = self.store.one("SELECT * FROM hazards_archive WHERE kind = ? AND id = ?", (kind, eid))
        if not row:
            return None
        try:
            row["data"] = json.loads(row["data"])
        except ValueError:
            row["data"] = {}
        return row

    def ensure_tables(self) -> None:
        self.store.script("CREATE TABLE IF NOT EXISTS hazards_archive (kind TEXT NOT NULL, id TEXT NOT NULL, "
                          "title TEXT, subtitle TEXT, lat REAL, lon REAL, time REAL, data TEXT, seen_at REAL NOT NULL, "
                          "PRIMARY KEY(kind, id))")
        self.load_zones()
        self.load_tsunami()


GVP_MIN = 500   # the Holocene list has about 1,200 volcanoes; fewer means a truncated answer


def gvp_dataset_build(ds) -> None:
    """Dataset: Smithsonian GVP Holocene volcanoes (WFS GeoJSON) -> volcanoes.json."""
    path = ds.download(volcanoes.WFS_URL, "gvp_holocene.json", min_bytes=50_000, timeout=120)
    data = json.loads(path.read_text(encoding="utf-8"))
    items = volcanoes.parse_gvp(data)
    if len(items) < GVP_MIN:
        raise RuntimeError(f"GVP list looks incomplete ({len(items)} volcanoes)")
    ds.write_json("volcanoes.json", {"built_at": time.time(), "volcanoes": items})
    ds.set_rows(len(items))
    ds.note(f"{len(items):,} Holocene volcanoes")


NE_110M = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson"


def countries_dataset_build(ds) -> None:
    """Dataset: Natural Earth 1:110m country outlines (public domain), simplified."""
    path = ds.download(NE_110M, "ne_110m_admin_0_countries.geojson", min_bytes=100_000, timeout=120)
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for f in data.get("features") or []:
        p = f.get("properties") or {}
        iso2 = p.get("ISO_A2_EH") or p.get("ISO_A2")
        if not iso2 or iso2 == "-99":
            continue
        geom = simplify_geometry(f.get("geometry"), 0.05, 3)
        if geom:
            out.append({"iso2": iso2, "name": p.get("NAME_EN") or p.get("NAME"), "bbox": geometry_bbox(geom),
                        "geometry": geom})
    if len(out) < 100:
        raise RuntimeError(f"Natural Earth countries look incomplete ({len(out)})")
    ds.write_json("countries.json", {"built_at": time.time(), "countries": out})
    ds.set_rows(len(out))
    ds.note(f"{len(out)} country outlines")


def file_age(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None
