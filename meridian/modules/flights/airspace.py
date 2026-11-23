"""Airspace and airport status for the flights module.

- US airport delays from FAA NAS Status: ground stops, ground delay programmes, arrival and
  departure delays, closures, airport notices and de-icing.
- US temporary flight restrictions (TFRs) from tfr.faa.gov: the list, the polygons and each notice.
- Conflict-zone advisories from EASA's Conflict Zone Information Bulletins, drawn on FIR outlines
  (VATSpy data project) or, where a bulletin is limited to a country's territory, country shapes.

Polling is polite: each source runs on a fast cadence only while its layer (or the airport card,
or an AI tool) is in use, and on a slow cadence otherwise so the events feed stays current.
Snapshots are swapped whole, so handlers never see a half-built state.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable

from meridian import geo

from . import airspace_parse as ap
from . import shapes
from .airspace_data import MAJOR_CITIES, TFR_CATEGORY_LABELS

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights_airport_events (
    id TEXT PRIMARY KEY,
    faa TEXT NOT NULL,
    icao TEXT,
    code TEXT NOT NULL,
    first_t REAL NOT NULL,
    last_t REAL NOT NULL,
    severity INTEGER NOT NULL,
    lat REAL,
    lon REAL,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL,
    start_t REAL,
    end_t REAL
);
CREATE INDEX IF NOT EXISTS flights_airport_events_t ON flights_airport_events(last_t);
CREATE TABLE IF NOT EXISTS flights_tfr_detail (
    nid TEXT PRIMARY KEY,
    modified TEXT,
    fetched_at REAL NOT NULL,
    data TEXT NOT NULL
);
"""

NAS_URL = "https://nasstatus.faa.gov/api/airport-events"
TFR_LIST_URL = "https://tfr.faa.gov/tfrapi/exportTfrList"
TFR_WFS_URL = "https://tfr.faa.gov/geoserver/TFR/ows"
TFR_WFS_PARAMS = {"service": "WFS", "version": "1.1.0", "request": "GetFeature", "typeName": "TFR:V_TFR_LOC",
                  "maxFeatures": "1000", "outputFormat": "application/json", "srsname": "EPSG:4326"}
TFR_DETAIL_URL = "https://tfr.faa.gov/download/detail_{nid}.xml"
CZIB_CSV_URL = "https://www.easa.europa.eu/en/domains/air-operations/czibs/export?page&_format=csv"
CZIB_RSS_URL = "https://www.easa.europa.eu/en/domains/air-operations/czibs/feed.xml"

SRC_NAS = "flights.faa_nas"
SRC_TFR = "flights.faa_tfr"
SRC_CZIB = "flights.easa_czib"
LAYER_DELAYS = "flights.delays"
LAYER_TFR = "flights.tfr"
LAYER_CZIB = "flights.czib"
NAS_NAME = "FAA NAS Status"
EVENTS_KEEP_S = 14 * 86400
DETAIL_KEEP_S = 7 * 86400
CITY_KM = 15.0
LEVEL_NAMES = {3: "high", 2: "medium", 1: "low"}
CZIB_LEVEL_LABELS = {"avoid": "Do not operate", "caution": "Caution", "info": "Information"}


class Feed:
    """One upstream source: cadence while in use and otherwise, backoff after failures."""

    def __init__(self, sid: str, active_every: float, idle_every: float):
        self.sid = sid
        self.active_every = active_every
        self.idle_every = idle_every
        self.ok_at = 0.0
        self.attempt_at = 0.0
        self.retry_at = 0.0
        self.error: str | None = None
        self.failures = 0
        self.lock = threading.Lock()

    def due(self, now: float, active: bool) -> bool:
        if now < self.retry_at:
            return False
        every = self.active_every if active else self.idle_every
        return now - self.ok_at >= every - 1

    def succeeded(self, now: float) -> None:
        self.ok_at = now
        self.error = None
        self.failures = 0
        self.retry_at = 0.0

    def failed(self, now: float, error: str) -> None:
        self.failures += 1
        self.error = error[:300]
        self.retry_at = now + min(1800.0, 60.0 * 2 ** min(self.failures - 1, 5))

    def info(self, now: float) -> dict:
        return {"ok": self.error is None and self.ok_at > 0, "age_s": round(now - self.ok_at) if self.ok_at else None,
                "error": self.error, "failures": self.failures,
                "retry_in_s": round(self.retry_at - now) if self.retry_at > now else None,
                "every_s": {"in_use": self.active_every, "idle": self.idle_every}}


_KEEP_UPPER = {"NW", "NE", "SW", "SE", "AFB", "USA", "US", "DC", "D.C.", "NAS", "ARB", "JRB", "NASA", "VA"}


def _title_case(s: str | None) -> str | None:
    """'DISNEY WORLD THEME PARK, ORLANDO' -> 'Disney World Theme Park, Orlando'; 'disneyland theme park
    anaheim' -> 'Disneyland Theme Park Anaheim'. State codes and tokens with digits stay as they are."""
    if not s:
        return s
    s = re.sub(r"\b([A-Z]{2})\.(?=,|\s|$)", r"\1", s.strip())      # 'DC.' -> 'DC'
    if re.search(r"[a-z]", s) and re.search(r"[A-Z]{2,}|\b[A-Z][a-z]", s):
        return s                                                     # already mixed case
    out = []
    for w in s.split(" "):
        bare = w.rstrip(",")
        if bare.upper() in _KEEP_UPPER or re.fullmatch(r"[A-Z]{2}", bare) or any(c.isdigit() for c in w):
            out.append(w.upper() if bare.upper() in _KEEP_UPPER else w)
        elif re.fullmatch(r"[A-Za-z]\.[A-Za-z]\.,?", w):
            out.append(w.upper())
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def _same_place(a: str | None, b: str | None) -> bool:
    norm = lambda x: re.sub(r"[^a-z]", "", (x or "").lower())  # noqa: E731
    return bool(a and b) and (norm(b) in norm(a) or norm(a) in norm(b))


def _lc(label: str | None) -> str:
    """Lower-case a label for use mid-sentence, keeping acronyms: 'Ground stop' -> 'ground stop',
    'VIP movement' stays."""
    s = label or ""
    return s if s[1:2].isupper() else s[:1].lower() + s[1:]


def _err(err: BaseException) -> str:
    return str(err) or type(err).__name__


class AirspaceService:
    def __init__(self, m, *, clock: Callable[[], float] = time.time):
        self.m = m
        self.net = m.net
        self.store = m.store
        self.log = m.log
        self.clock = clock
        m.store.script(SCHEMA)
        self.net.set_host_interval("nasstatus.faa.gov", 2.0)
        self.net.set_host_interval("tfr.faa.gov", 1.5)
        self.net.set_host_interval("www.easa.europa.eu", 2.0)
        self.nas_feed = Feed(SRC_NAS, active_every=120.0, idle_every=900.0)
        self.tfr_feed = Feed(SRC_TFR, active_every=300.0, idle_every=1800.0)
        self.czib_feed = Feed(SRC_CZIB, active_every=7200.0, idle_every=43200.0)
        self.lock = threading.Lock()
        # snapshots (replaced whole)
        self.airports: dict[str, dict] = {}
        self.airports_t = 0.0
        self.tfr_list: dict[str, dict] = {}
        self.tfr_geo: dict[str, dict] = {}
        self.tfrs: dict[str, dict] = {}
        self.tfr_t = 0.0
        self.details: dict[str, dict] = {}       # nid -> {"modified", "data", "fetched_at"}
        self.czibs: list[dict] = []
        self.czib_links: list[dict] = []
        self.czib_t = 0.0
        self.czib_shapes = {"firs": 0, "countries": 0}
        self.version = {"nas": 0, "tfr": 0, "czib": 0}
        self._fc_cache: dict[str, tuple[int, Any]] = {}
        self._episodes: dict[tuple, str] = {}
        self._load_persisted()

    # =========================================================================================
    # persistence
    # =========================================================================================
    def _load_persisted(self) -> None:
        try:
            rows = self.store.query("SELECT nid, modified, fetched_at, data FROM flights_tfr_detail")
        except Exception:
            rows = []
        for r in rows:
            try:
                self.details[r["nid"]] = {"modified": r["modified"], "fetched_at": r["fetched_at"],
                                          "data": json.loads(r["data"])}
            except ValueError:
                continue
        items = self.store.kv_get("flights.czib.items")
        if isinstance(items, list) and items:
            self.czibs = [c for c in items if isinstance(c, dict) and c.get("id")]
            self.czib_links = self.store.kv_get("flights.czib.links") or []
            fetched = self.store.kv_get("flights.czib.fetched_at") or 0.0
            # Restarts keep the cadence: no new download until the stored copy is due, unless it was
            # read by an older parser (then it is shown until a fresh copy arrives).
            if self.store.kv_get("flights.czib.format") != ap.CZIB_FORMAT:
                fetched = 0.0
            self.czib_feed.ok_at = float(fetched) if isinstance(fetched, (int, float)) else 0.0
            self.czib_t = self.czib_feed.ok_at
            self.rebuild_czib_shapes()
            if self.czib_t:
                n = len(self.active_czibs())
                self.m.status(SRC_CZIB, ok=True, count=n,
                              note=f"{n} active bulletins (copy from {time.strftime('%d %b %H:%M UTC', time.gmtime(self.czib_t))})")

    # =========================================================================================
    # activity and scheduling
    # =========================================================================================
    def in_use(self, layer: str, within: float = 240.0) -> bool:
        return self.m.active(layer, within=within)

    def tick_nas(self) -> None:
        if self.nas_feed.due(self.clock(), self.in_use(LAYER_DELAYS)):
            self.refresh_nas()

    def tick_tfr(self) -> None:
        now = self.clock()
        active = self.in_use(LAYER_TFR)
        if self.tfr_feed.due(now, active):
            self.refresh_tfr()
        if self.tfrs:
            self.backfill_details(limit=8 if active else 2)

    def tick_czib(self) -> None:
        if self.czib_feed.due(self.clock(), self.in_use(LAYER_CZIB)):
            self.refresh_czib()

    @staticmethod
    def _kick(feed: Feed, fn: Callable[[], bool], name: str, wait: float) -> None:
        """Refresh in the background; with ``wait`` > 0 (nothing loaded yet) wait for an answer, also
        when another request already started the refresh."""
        if feed.lock.locked() and wait <= 0:
            return
        t = threading.Thread(target=fn, daemon=True, name=name)
        t.start()
        if wait > 0:
            deadline = time.monotonic() + wait
            t.join(wait)
            left = deadline - time.monotonic()
            if left > 0 and feed.lock.acquire(timeout=left):
                feed.lock.release()

    def ensure_nas(self, max_age: float = 180.0, wait: float = 4.0) -> None:
        """Called by handlers: refresh in the background when the snapshot is old, waiting a little
        when there is no answer yet."""
        now = self.clock()
        if now - self.nas_feed.ok_at < max_age or now < self.nas_feed.retry_at:
            return
        self._kick(self.nas_feed, self.refresh_nas, "flights-nas-now", 0.0 if self.airports_t else wait)

    def ensure_tfr(self, wait: float = 5.0) -> None:
        now = self.clock()
        if self.tfr_feed.ok_at and now - self.tfr_feed.ok_at < self.tfr_feed.active_every:
            return
        if now < self.tfr_feed.retry_at:
            return
        self._kick(self.tfr_feed, self.refresh_tfr, "flights-tfr-now", 0.0 if self.tfr_t else wait)

    def ensure_czib(self, wait: float = 6.0) -> None:
        now = self.clock()
        if self.czibs and now - self.czib_feed.ok_at < self.czib_feed.active_every:
            return
        if now < self.czib_feed.retry_at:
            return
        self._kick(self.czib_feed, self.refresh_czib, "flights-czib-now", 0.0 if self.czibs else wait)

    # =========================================================================================
    # FAA NAS Status
    # =========================================================================================
    def refresh_nas(self) -> bool:
        feed = self.nas_feed
        if not feed.lock.acquire(blocking=False):
            return False
        try:
            now = self.clock()
            feed.attempt_at = now
            try:
                data = self.net.get_json(NAS_URL, timeout=15, retries=1, ttl=50, max_bytes=8_000_000)
                airports = ap.parse_nas_events(data, now)
                if not isinstance(data, list):
                    raise ValueError("unexpected answer from NAS Status")
            except Exception as err:
                msg = self.m.config.redact(_err(err))
                feed.failed(now, msg)
                self.m.status(SRC_NAS, ok=False, error=msg)
                self.log.info("NAS Status refresh failed: %s", msg)
                return False
            snapshot = {a["faa"]: a for a in airports}
            with self.lock:
                self.airports = snapshot
                self.airports_t = now
                self.version["nas"] += 1
            try:
                self._record_airport_events(airports, now)
            except Exception as err:          # history is a nicety; the live answer stands
                self.log.warning("could not store airport events: %s", err)
            feed.succeeded(now)
            delayed = [a for a in airports if any(s["delay"] for s in a["statuses"])]
            stops = sum(1 for a in airports for s in a["statuses"] if s["code"] == "ground_stop")
            gdps = sum(1 for a in airports for s in a["statuses"] if s["code"] == "ground_delay")
            self.m.status(SRC_NAS, ok=True, count=len(delayed),
                          note=f"{len(delayed)} airports with delays or full closures ({stops} ground stops, "
                               f"{gdps} ground delay programmes), {len(airports)} with any status")
            return True
        finally:
            feed.lock.release()

    @staticmethod
    def airport_event_severity(st: dict) -> int:
        code = st["code"]
        if code in ("ground_stop", "closure"):
            return 5
        if code in ("ground_delay", "arrival_delay", "departure_delay"):
            avg = st.get("avg_delay_min") or 0
            top = st.get("max_delay_min") or 0
            if avg >= 60 or top >= 120:
                return 4
            if avg >= 30 or top >= 45:
                return 3
        return 0

    def _record_airport_events(self, airports: list[dict], now: float) -> None:
        rows = []
        for a in airports:
            for st in a["statuses"]:
                sev = self.airport_event_severity(st)
                if sev < 3:
                    continue
                key = (a["faa"], st["code"])
                eid = self._episode_id(key, st, now)
                title = f"{a['name']} ({a['faa']}): {_lc(st['type'])}"
                if st.get("avg_delay_min") and st["code"] != "ground_stop":
                    title += f", {st['avg_delay_min']} min average"
                elif st["code"] in ("arrival_delay", "departure_delay") and st.get("max_delay_min"):
                    title += f", up to {st['max_delay_min']} min"
                subtitle = ", ".join(x for x in (st.get("reason"), NAS_NAME) if x)
                first = min(now, st["start"]) if st.get("start") else now
                rows.append((eid, a["faa"], a.get("icao"), st["code"], first, now, sev, a["lat"], a["lon"], title,
                             subtitle, st.get("start"), st.get("end")))
        if rows:
            self.store.executemany(
                "INSERT INTO flights_airport_events(id, faa, icao, code, first_t, last_t, severity, lat, lon, title, "
                "subtitle, start_t, end_t) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "last_t = excluded.last_t, title = excluded.title, subtitle = excluded.subtitle, "
                "end_t = excluded.end_t, severity = MAX(severity, excluded.severity)", rows)

    def _episode_id(self, key: tuple, st: dict, now: float) -> str:
        """One event per programme: keyed by its start time when the FAA gives one, otherwise the
        open episode of the same airport and kind seen in the last 45 minutes is continued."""
        faa, code = key
        if st.get("start"):
            return f"{code}:{faa}:{int(st['start'])}"
        eid = self._episodes.get(key)
        if eid:
            row = self.store.one("SELECT last_t FROM flights_airport_events WHERE id = ?", (eid,))
            if row and now - row["last_t"] <= 2700:
                return eid
        row = self.store.one("SELECT id FROM flights_airport_events WHERE faa = ? AND code = ? AND last_t >= ? "
                             "ORDER BY last_t DESC LIMIT 1", (faa, code, now - 2700))
        eid = row["id"] if row else f"{code}:{faa}:{int(now)}"
        self._episodes[key] = eid
        return eid

    def airport_entity(self, a: dict) -> dict:
        top = a["statuses"][0]
        return {"kind": "airport_delay", "id": a.get("icao") or a["faa"], "title": a["name"],
                "subtitle": f"{a['faa']}, {_lc(top['type'])}" + (f", {top['avg_delay_min']} min average"
                                                                   if top.get("avg_delay_min") else ""),
                "lat": a["lat"], "lon": a["lon"], "layer": LAYER_DELAYS, "zoom": 9}

    def find_airport(self, ident: str, lat: float | None = None, lon: float | None = None) -> dict | None:
        faa = ap.faa_for(ident)
        snap = self.airports
        if faa and faa in snap:
            return snap[faa]
        # An ICAO code outside the contiguous US that the table does not know (PAxx, PHxx, TJxx...):
        # match by position, tightly, so a small field next to a big airport never gets its delays.
        if faa is None and lat is not None and lon is not None and re.fullmatch(r"[PT][A-Z]{3}", (ident or "").upper()):
            for a in snap.values():
                if a.get("icao") is None and geo.haversine_km(lat, lon, a["lat"], a["lon"]) <= 3.0:
                    return a
        return None

    @staticmethod
    def contract_view(st: dict) -> dict:
        """The agreed cross-module shape (CONTRACT 9.1 style) for one status."""
        return {"delay": bool(st["delay"]), "type": st["type"], "reason": st.get("reason"),
                "avg_delay_min": st.get("avg_delay_min"), "trend": st.get("trend"), "source": NAS_NAME,
                "updated": st.get("updated")}

    def airport_status(self, ident: str, lat: float | None = None, lon: float | None = None) -> dict | None:
        self.ensure_nas(max_age=180.0, wait=4.0)
        a = self.find_airport(ident, lat, lon)
        if a is None:
            return None
        top = a["statuses"][0]
        out = self.contract_view(top)
        if out["updated"] is None:
            out["updated"] = round(self.airports_t, 1) or None
        items = []
        for st in a["statuses"]:
            item = self.contract_view(st)
            item.update({"code": st["code"], "level": LEVEL_NAMES[st["level"]], "summary": st["summary"],
                         "max_delay_min": st.get("max_delay_min"), "min_delay_min": st.get("min_delay_min"),
                         "start": st.get("start"), "end": st.get("end"), "advisory": st.get("advisory")})
            items.append(item)
        out.update({"code": top["code"], "level": LEVEL_NAMES[top["level"]], "summary": top["summary"],
                    "max_delay_min": top.get("max_delay_min"), "start": top.get("start"), "end": top.get("end"),
                    "items": items, "airport": {"faa": a["faa"], "icao": a.get("icao"), "name": a["name"],
                                                "lat": a["lat"], "lon": a["lon"]},
                    "runways": a.get("runways"), "fetched_at": round(self.airports_t, 1),
                    "link": ap.NAS_SITE, "entity": self.airport_entity(a)})
        return out

    def delays_fc(self) -> dict:
        self.m.touch(LAYER_DELAYS)
        self.ensure_nas(max_age=110.0, wait=4.0)
        now = self.clock()
        cached = self._fc_cache.get("nas")
        if cached and cached[0] == self.version["nas"]:
            fc = cached[1]
        else:
            feats = []
            for a in sorted(self.airports.values(), key=lambda x: -x["statuses"][0]["level"]):
                top = a["statuses"][0]
                avg = top.get("avg_delay_min")
                feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [a["lon"], a["lat"]]},
                              "properties": {"id": a.get("icao") or a["faa"], "faa": a["faa"], "name": a["name"],
                                             "code": top["code"], "type": top["type"], "level": top["level"],
                                             "delay": 1 if any(s["delay"] for s in a["statuses"]) else 0,
                                             "avg": avg, "max": top.get("max_delay_min"),
                                             "n": len(a["statuses"]),
                                             "label": a["faa"] + (f" {avg} min" if avg else ""),
                                             "sk": top["level"] * 1000 + min(999, top.get("max_delay_min") or avg or 0)}})
            counts: dict[str, int] = {}
            for a in self.airports.values():
                for st in a["statuses"]:
                    counts[st["code"]] = counts.get(st["code"], 0) + 1
            fc = {"type": "FeatureCollection", "features": feats,
                  "meta": {"airports": len(feats), "delayed": sum(1 for f in feats if f["properties"]["delay"]),
                           "counts": counts}}
            self._fc_cache["nas"] = (self.version["nas"], fc)
        meta = dict(fc["meta"], updated=round(self.airports_t, 1) or None, now=round(now, 1),
                    stale=bool(self.airports_t and now - self.airports_t > 1800),
                    error=self.nas_feed.error, source=NAS_NAME)
        return {"type": "FeatureCollection", "features": fc["features"], "meta": meta}

    # =========================================================================================
    # FAA temporary flight restrictions
    # =========================================================================================
    def refresh_tfr(self) -> bool:
        feed = self.tfr_feed
        if not feed.lock.acquire(blocking=False):
            return False
        try:
            now = self.clock()
            feed.attempt_at = now
            try:
                listing = ap.parse_tfr_list(self.net.get_json(TFR_LIST_URL, timeout=20, retries=1, ttl=60,
                                                              max_bytes=5_000_000))
            except Exception as err:
                msg = self.m.config.redact(_err(err))
                feed.failed(now, msg)
                self.m.status(SRC_TFR, ok=False, error=msg)
                self.log.info("TFR list refresh failed: %s", msg)
                return False
            note_extra = ""
            try:
                geo_by = ap.parse_tfr_wfs(self.net.get_json(TFR_WFS_URL, params=TFR_WFS_PARAMS, timeout=30, retries=1,
                                                            ttl=60, max_bytes=40_000_000))
            except Exception as err:     # keep the previous polygons; the list still updates
                geo_by = self.tfr_geo
                note_extra = f"; polygons unavailable ({self.m.config.redact(_err(err))[:120]})"
            with self.lock:
                self.tfr_list = listing
                self.tfr_geo = geo_by
            self._merge_tfrs(now)
            feed.succeeded(now)
            mapped = sum(1 for t in self.tfrs.values() if t["polygons"])
            self.m.status(SRC_TFR, ok=True, count=len(self.tfrs),
                          note=f"{len(self.tfrs)} restrictions listed, {mapped} with a map area{note_extra}")
            return True
        finally:
            feed.lock.release()

    def _merge_tfrs(self, now: float) -> None:
        merged: dict[str, dict] = {}
        ids = list(self.tfr_list) + [i for i in self.tfr_geo if i not in self.tfr_list]
        for nid in ids:
            base = self.tfr_list.get(nid) or {}
            g = self.tfr_geo.get(nid) or {}
            det = (self.details.get(ap.nid_of(nid) or "") or {}).get("data")
            merged[nid] = self._tfr_record(nid, base, g, det, now)
        with self.lock:
            self.tfrs = merged
            self.tfr_t = now
            self.version["tfr"] += 1

    def _tfr_record(self, nid: str, base: dict, g: dict, det: dict | None, now: float) -> dict:
        typ = base.get("type") or (g.get("legal") or None)
        cat, label = ap.tfr_category(typ, (det or {}).get("rule"))
        polys = g.get("polygons") or []
        geom = shapes.make_geometry(polys)
        bbox = shapes.bbox_of(geom)
        center = None
        radius = None
        if bbox:
            center = [round((bbox[0] + bbox[2]) / 2, 5), round((bbox[1] + bbox[3]) / 2, 5)]
            radius = round(geo.haversine_km(center[1], center[0], bbox[3], bbox[2]), 1)
        place = None
        if det and (det.get("city") or det.get("state_name")):
            city = _title_case(det.get("city"))
            state = _title_case(det.get("state_name"))
            if city and re.search(r",\s*[A-Z]{2}$", city):
                state = None                                  # 'Calexico, CA' already names the state
            place = ", ".join(x for x in (city, None if _same_place(city, state) else state) if x)
        if not place:
            desc = base.get("description") or g.get("title") or ""
            place = _title_case(re.split(r",\s*(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b", desc)[0].strip(" ,")) or None
        if place and re.sub(r"[^A-Z]", "", place.upper()) in ("USA", "US", "UNITEDSTATES"):
            place = "United States (nationwide notice)"
        start = (det or {}).get("start")
        end = (det or {}).get("end")
        permanent = bool((det or {}).get("permanent"))
        if start and start > now:
            state = "upcoming"
        elif end and end <= now:
            state = "ended"
        elif det:
            state = "active"
        else:
            state = "listed"          # no notice details yet
        areas = (det or {}).get("areas") or []
        uppers = [a["upper_ft"] for a in areas if a.get("upper_ft")]
        top_area = max(areas, key=lambda a: a.get("upper_ft") or 0) if areas else None
        issued = (det or {}).get("issued") or base.get("created")
        return {
            "id": nid, "nid": ap.nid_of(nid), "type": typ, "category": cat, "type_label": label,
            "category_label": TFR_CATEGORY_LABELS.get(cat, label), "facility": base.get("facility") or (det or {}).get("facility"),
            "state_code": base.get("state") or g.get("state"), "place": place, "description": base.get("description"),
            "created": base.get("created"), "issued": issued, "modified": g.get("modified"),
            "geometry": geom, "polygons": polys, "bbox": bbox, "center": center, "radius_km": radius,
            "start": start, "end": end, "permanent": permanent, "status": state,
            "lower": (top_area or {}).get("lower"), "upper": (top_area or {}).get("upper"),
            "upper_ft": max(uppers) if uppers else None,
            "schedule": next((a["schedule"] for a in areas if a.get("schedule")), None),
            "rule": (det or {}).get("rule"), "rule_text": (det or {}).get("rule_text"),
            "purpose": ap.plain_purpose((det or {}).get("purpose")), "areas": areas, "text": (det or {}).get("text"),
            "coord_facility": (det or {}).get("coord_facility"), "has_detail": det is not None,
            "link": ap.tfr_link(nid), "city": self._near_city(center, radius, geom),
        }

    @staticmethod
    def _near_city(center, radius_km, geom) -> str | None:
        """The nearest large US city whose centre lies inside the restriction or within 15 km of its
        centre (a stadium or parade a few kilometres from downtown counts)."""
        if not center:
            return None
        lon, lat = center
        best = None
        for name, clat, clon in MAJOR_CITIES:
            d = geo.haversine_km(lat, lon, clat, clon)
            if d > (radius_km or 0) + CITY_KM:
                continue
            if (d <= CITY_KM or shapes.contains(geom, clat, clon)) and (best is None or d < best[0]):
                best = (d, name)
        return best[1] if best else None

    def _detail_wanted(self, t: dict) -> bool:
        nid = t["nid"]
        if not nid:
            return False
        have = self.details.get(nid)
        if have is None:
            return True
        if have.get("missing"):
            return self.clock() - have.get("fetched_at", 0) > 3600      # a failed fetch is retried hourly
        if (have.get("data") or {}).get("v") != ap.TFR_FORMAT:
            return True                                                  # read by an older parser
        return bool(t.get("modified") and have.get("modified") and t["modified"] != have["modified"])

    def backfill_details(self, limit: int = 3) -> int:
        """Fetch notice details for new or changed restrictions, newest first, a few per run."""
        todo = [t for t in self.tfrs.values() if self._detail_wanted(t)]
        order = {"vip": 0, "space": 1, "hazards": 2, "sports": 3, "security": 4, "drones": 5, "other": 6}
        todo.sort(key=lambda t: (order.get(t["category"], 9), -(t.get("created") or 0)))
        n = 0
        for t in todo[:limit]:
            if self.fetch_detail(t["id"]):
                n += 1
        if n:
            self._merge_tfrs(self.clock())
        self._prune_details()
        return n

    def fetch_detail(self, notam_id: str) -> bool:
        nid = ap.nid_of(notam_id)
        if not nid:
            return False
        now = self.clock()
        try:
            raw = self.net.get_bytes(TFR_DETAIL_URL.format(nid=nid), timeout=20, retries=1, ttl=300, max_bytes=5_000_000)
            data = ap.parse_tfr_detail(raw)
        except Exception as err:
            self.log.info("TFR notice %s unavailable: %s", notam_id, self.m.config.redact(_err(err)))
            # remember the miss so the backfill moves on; it is retried when the notice changes
            data = None
        modified = (self.tfrs.get(notam_id) or {}).get("modified")
        if data is None:
            self.details[nid] = {"modified": modified, "fetched_at": now, "data": None, "missing": True}
            return False
        self.details[nid] = {"modified": modified, "fetched_at": now, "data": data}
        try:
            self.store.execute("INSERT INTO flights_tfr_detail(nid, modified, fetched_at, data) VALUES(?,?,?,?) "
                               "ON CONFLICT(nid) DO UPDATE SET modified = excluded.modified, "
                               "fetched_at = excluded.fetched_at, data = excluded.data",
                               (nid, modified, now, json.dumps(data)))
        except Exception as err:
            self.log.debug("could not store a TFR notice: %s", err)
        return True

    def _prune_details(self) -> None:
        now = self.clock()
        live = {t["nid"] for t in self.tfrs.values() if t.get("nid")}
        for nid in [k for k, v in list(self.details.items()) if k not in live and now - v.get("fetched_at", 0) > 3600]:
            self.details.pop(nid, None)

    def tfr(self, nid: str, *, fetch: bool = True) -> dict | None:
        self.m.touch(LAYER_TFR)
        self.ensure_tfr()
        notam = ap.notam_of(nid)
        t = self.tfrs.get(notam or "")
        if t is None:
            return None
        if fetch and not t["has_detail"] and not (self.details.get(nid) or {}).get("missing"):
            if self.fetch_detail(notam):
                self._merge_tfrs(self.clock())
                t = self.tfrs.get(notam) or t
        return t

    def tfr_entity(self, t: dict) -> dict:
        ent = {"kind": "flight_restriction", "id": t["id"], "title": t.get("place") or f"Flight restriction {t['id']}",
               "subtitle": f"{t['type_label']}, FDC {t['id']}", "layer": LAYER_TFR, "zoom": 9}
        if t.get("center"):
            ent["lon"], ent["lat"] = t["center"]
        if t.get("bbox"):
            ent["bbox"] = t["bbox"]
        return ent

    def tfr_view(self, t: dict, *, full: bool = True) -> dict:
        out = {k: t.get(k) for k in ("id", "nid", "type", "category", "type_label", "category_label", "facility",
                                     "state_code", "place", "issued", "start", "end", "permanent", "status", "lower",
                                     "upper", "upper_ft", "schedule", "rule", "rule_text", "purpose", "bbox", "center",
                                     "radius_km", "link", "city", "has_detail", "coord_facility", "description")}
        out["mapped"] = bool(t.get("polygons"))
        if full:
            out["areas"] = t.get("areas") or []
            out["text"] = t.get("text")
            out["entity"] = self.tfr_entity(t)
        return out

    def tfrs_fc(self) -> dict:
        self.m.touch(LAYER_TFR)
        self.ensure_tfr()
        now = self.clock()
        cached = self._fc_cache.get("tfr")
        if cached and cached[0] == self.version["tfr"]:
            fc = cached[1]
        else:
            feats = []
            for t in self.tfrs.values():
                if not t["polygons"] or t["status"] == "ended":
                    continue
                props = {"id": t["id"], "nid": t["nid"], "cat": t["category"], "type": t["type_label"],
                         "place": t.get("place") or "", "status": t["status"],
                         "active": 1 if t["status"] in ("active", "listed") else 0,
                         "upper": t.get("upper") or "", "lower": t.get("lower") or ""}
                for poly in t["polygons"]:
                    feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": poly},
                                  "properties": dict(props, role="area")})
                if t.get("center"):
                    feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": t["center"]},
                                  "properties": dict(props, role="centre")})
            by_cat: dict[str, int] = {}
            for t in self.tfrs.values():
                by_cat[t["category"]] = by_cat.get(t["category"], 0) + 1
            fc = {"type": "FeatureCollection", "features": feats,
                  "meta": {"count": len(self.tfrs), "mapped": sum(1 for t in self.tfrs.values() if t["polygons"]),
                           "active": sum(1 for t in self.tfrs.values() if t["status"] in ("active", "listed")),
                           "upcoming": sum(1 for t in self.tfrs.values() if t["status"] == "upcoming"),
                           "by_category": by_cat,
                           "details_pending": sum(1 for t in self.tfrs.values() if not t["has_detail"])}}
            self._fc_cache["tfr"] = (self.version["tfr"], fc)
        meta = dict(fc["meta"], updated=round(self.tfr_t, 1) or None, now=round(now, 1), error=self.tfr_feed.error,
                    source="FAA tfr.faa.gov")
        return {"type": "FeatureCollection", "features": fc["features"], "meta": meta}

    @staticmethod
    def tfr_event_severity(t: dict) -> int:
        if t["category"] == "vip":
            return 4
        if t["category"] == "drones" or t["status"] == "ended":
            return 0
        if t["category"] == "space":
            return 3
        return 3 if t.get("city") else 0

    # =========================================================================================
    # EASA conflict zone bulletins
    # =========================================================================================
    def refresh_czib(self) -> bool:
        feed = self.czib_feed
        if not feed.lock.acquire(blocking=False):
            return False
        try:
            now = self.clock()
            feed.attempt_at = now
            try:
                raw = self.net.get_bytes(CZIB_CSV_URL, timeout=30, retries=1, max_bytes=20_000_000)
                items = ap.parse_czib_csv(raw)
                if not items:
                    raise ValueError("the bulletin list is empty")
            except Exception as err:
                msg = self.m.config.redact(_err(err))
                feed.failed(now, msg)
                self.m.status(SRC_CZIB, ok=False, error=msg)
                self.log.info("EASA CZIB refresh failed: %s", msg)
                return False
            try:
                links = ap.parse_rss_links(self.net.get_bytes(CZIB_RSS_URL, timeout=20, retries=1, max_bytes=5_000_000))
            except Exception as err:
                links = self.czib_links
                self.log.info("EASA CZIB feed unavailable, keeping the old links: %s", self.m.config.redact(_err(err)))
            for c in items:
                c["link"] = ap.match_link(c, links) or ap.CZIB_SITE
            with self.lock:
                self.czibs = items
                self.czib_links = links
                self.czib_t = now
            try:
                self.store.kv_set("flights.czib.items", items)
                self.store.kv_set("flights.czib.links", links)
                self.store.kv_set("flights.czib.fetched_at", now)
                self.store.kv_set("flights.czib.format", ap.CZIB_FORMAT)
            except Exception as err:
                self.log.debug("could not store the bulletins: %s", err)
            feed.succeeded(now)
            self.rebuild_czib_shapes()
            active = [c for c in items if c["active"]]
            self.m.status(SRC_CZIB, ok=True, count=len(active),
                          note=f"{len(active)} active bulletins, {len(items) - len(active)} withdrawn or expired")
            return True
        finally:
            feed.lock.release()

    def rebuild_czib_shapes(self) -> None:
        """Attach outlines to the active bulletins: FIRs where the bulletin names whole FIRs, the
        country where it is limited to a territory (or no FIR outline is known)."""
        firs = shapes.load(self.m.data_dir / shapes.FIRS_FILE)
        countries = shapes.load(self.m.data_dir / shapes.COUNTRIES_FILE)
        out = []
        for c in self.czibs:
            c = {k: v for k, v in c.items() if k not in ("parts", "geometry", "label", "bbox", "shape", "firs")}
            codes_all = ap.fir_codes(c.get("airspace"))
            known = [x for x in codes_all if x in firs]
            parts = []
            method = None
            if c.get("territory") or not known:
                for cc in c.get("countries") or []:
                    rec = countries.get(cc["iso2"])
                    if rec and rec.get("g"):
                        parts.append({"name": rec.get("name") or cc["name"], "code": cc["iso2"], "kind": "country",
                                      "g": rec["g"], "label": rec.get("label")})
                method = "country" if parts else None
            if not parts and known:
                for code in known:
                    g = firs[code]["g"]
                    lab = firs[code].get("label")
                    if c.get("clip"):
                        g = shapes.clip_geometry_lon(g, c["clip"][0], west=c["clip"][1])
                        lab = None
                    if g:
                        parts.append({"name": code, "code": code, "kind": "fir", "g": g, "label": lab})
                method = "fir" if parts else None
            polys = [p for part in parts for p in shapes.polygons_of(part["g"])]
            geom = shapes.make_geometry(polys)
            c.update({"parts": parts, "geometry": geom, "shape": method, "firs": codes_all,
                      "label": self._bulletin_label(parts), "bbox": shapes.bbox_of(geom)})
            out.append(c)
        with self.lock:
            self.czibs = out
            self.czib_shapes = {"firs": len(firs), "countries": len(countries)}
            self.version["czib"] += 1
        if firs:
            self.m.status("flights.vatspy_firs", ok=True, count=len(firs), note=f"{len(firs)} FIR outlines")
        if countries:
            self.m.status("flights.ne_countries", ok=True, count=len(countries), note=f"{len(countries)} country outlines")

    @staticmethod
    def _bulletin_label(parts: list[dict]) -> list | None:
        """One label per bulletin: the most central of its parts' label points (the medoid), so a
        bulletin over five FIRs is labelled in the middle rather than in its largest, remote FIR."""
        cands = []
        for p in parts:
            lab = p.get("label")
            if not lab or not shapes.contains(p["g"], lab[1], lab[0]):
                lab = shapes.label_point(p["g"])
            if lab:
                cands.append((lab, shapes.geometry_area(p["g"])))
        if not cands:
            return None
        if len(cands) <= 2:
            return max(cands, key=lambda c: c[1])[0]
        best = min(cands, key=lambda c: (sum(geo.haversine_km(c[0][1], c[0][0], o[0][1], o[0][0]) for o in cands), -c[1]))
        return best[0]

    def active_czibs(self) -> list[dict]:
        now = self.clock()
        return [c for c in self.czibs if c.get("active") and not (c.get("valid_until") and c["valid_until"] < now - 86400)]

    @staticmethod
    def short_subject(subject: str) -> str:
        """'Airspace of Pakistan – Baluchistan ... provinces' -> 'Pakistan' (the map label)."""
        s = re.split(r"\s+[–—-]\s+", subject or "", maxsplit=1)[0]
        s = re.sub(r"^(?:the\s+)?airspace of (?:the\s+)?", "", s, flags=re.I).strip()
        s = re.sub(r"\s+and neighbouring airspace$", " and neighbours", s, flags=re.I)
        return (s[:1].upper() + s[1:]) if s else (subject or "Conflict zone")

    def czib_entity(self, c: dict) -> dict:
        ent = {"kind": "airspace_advisory", "id": c["id"], "title": c["subject"],
               "subtitle": f"EASA {c['number']}, {CZIB_LEVEL_LABELS[c['level']].lower()}", "layer": LAYER_CZIB, "zoom": 4}
        if c.get("label"):
            ent["lon"], ent["lat"] = c["label"]
        if c.get("bbox"):
            ent["bbox"] = c["bbox"]
        return ent

    def czib_view(self, c: dict, *, full: bool = True) -> dict:
        keys = ("id", "number", "revision", "subject", "status", "active", "issued", "valid_until", "valid_text",
                "revised", "airspace", "countries", "countries_text", "level", "altitudes", "partial", "territory",
                "shape", "firs", "link", "bbox", "label")
        out = {k: c.get(k) for k in keys}
        out["short"] = self.short_subject(c.get("subject") or "")
        out["level_label"] = CZIB_LEVEL_LABELS.get(c.get("level") or "info")
        out["recommendation"] = c.get("recommendation")
        if full:
            out.update({"description": c.get("description"), "applicability": c.get("applicability"),
                        "references": c.get("references"), "revision_text": c.get("revision_text"),
                        "parts": [{"name": p["name"], "code": p["code"], "kind": p["kind"]} for p in c.get("parts") or []],
                        "entity": self.czib_entity(c)})
        return out

    def czib(self, cid: str) -> dict | None:
        self.m.touch(LAYER_CZIB)
        self.ensure_czib()
        cid = (cid or "").lower()
        for c in self.czibs:
            if c["id"] == cid:
                return c
        return None

    def czibs_fc(self) -> dict:
        self.m.touch(LAYER_CZIB)
        self.ensure_czib()
        cached = self._fc_cache.get("czib")
        if cached and cached[0] == self.version["czib"]:
            fc = cached[1]
        else:
            feats = []
            active = self.active_czibs()
            for c in active:
                props = {"id": c["id"], "number": c["number"], "subject": c["subject"],
                         "short": self.short_subject(c["subject"]), "level": c["level"],
                         "partial": 1 if c.get("partial") else 0, "alt": c.get("altitudes") or ""}
                for part in c.get("parts") or []:
                    feats.append({"type": "Feature", "geometry": part["g"],
                                  "properties": dict(props, role="area", part=part["name"], pkind=part["kind"])})
                if c.get("label"):
                    feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": c["label"]},
                                  "properties": dict(props, role="label")})
            fc = {"type": "FeatureCollection", "features": feats,
                  "meta": {"count": len(active), "mapped": sum(1 for c in active if c.get("parts")),
                           "by_level": {lv: sum(1 for c in active if c["level"] == lv) for lv in CZIB_LEVEL_LABELS},
                           "withdrawn": sum(1 for c in self.czibs if not c.get("active")),
                           "shapes": dict(self.czib_shapes),
                           "by_shape": {s: sum(1 for c in active if c.get("shape") == s) for s in ("fir", "country")}}}
            self._fc_cache["czib"] = (self.version["czib"], fc)
        meta = dict(fc["meta"], updated=round(self.czib_t, 1) or None, error=self.czib_feed.error, source="EASA")
        return {"type": "FeatureCollection", "features": fc["features"], "meta": meta}

    # =========================================================================================
    # lookups shared by What's here, nearby, search, events and AI tools
    # =========================================================================================
    def at(self, lat: float, lon: float) -> dict:
        tfrs = [self.tfr_view(t, full=False) | {"entity": self.tfr_entity(t)} for t in self.tfrs.values()
                if t["status"] != "ended" and t.get("geometry") and shapes.contains(t["geometry"], lat, lon)]
        czibs = [self.czib_view(c, full=False) | {"entity": self.czib_entity(c)} for c in self.active_czibs()
                 if c.get("geometry") and shapes.contains(c["geometry"], lat, lon)]
        delays = []
        for a in self.airports.values():
            d = geo.haversine_km(lat, lon, a["lat"], a["lon"])
            if d <= 30:
                delays.append(dict(self.airport_entity(a), distance_km=round(d, 1),
                                   summary=a["statuses"][0]["summary"]))
        return {"tfrs": tfrs, "advisories": czibs, "delays": sorted(delays, key=lambda x: x["distance_km"])}

    def nearby(self, lat: float, lon: float, radius_km: float, limit: int) -> list[dict]:
        out = []
        for a in self.airports.values():
            d = geo.haversine_km(lat, lon, a["lat"], a["lon"])
            if d <= radius_km:
                out.append(dict(self.airport_entity(a), distance_km=round(d, 2)))
        for t in self.tfrs.values():
            if not t.get("center") or t["status"] == "ended":
                continue
            d = geo.haversine_km(lat, lon, t["center"][1], t["center"][0])
            inside = t.get("geometry") and d <= (t.get("radius_km") or 0) + radius_km \
                and shapes.contains(t["geometry"], lat, lon)
            if d <= radius_km or inside:
                out.append(dict(self.tfr_entity(t), distance_km=0.0 if inside else round(d, 2)))
        out.sort(key=lambda e: e["distance_km"])
        return out[:limit]

    def search(self, q: str, limit: int) -> list[dict]:
        s = (q or "").strip()
        if len(s) < 2:
            return []
        u = s.upper()
        low = s.lower()
        out: list[dict] = []
        # airports with a status
        faa = ap.faa_for(u)
        wanted = None                       # "ground stop", "delays", "closed": airports with that status
        for words, codes in (("ground stop", {"ground_stop"}), ("ground delay", {"ground_delay"}),
                             ("closed", {"closure", "partial_closure"}), ("closure", {"closure", "partial_closure"}),
                             ("delay", {"ground_stop", "ground_delay", "arrival_delay", "departure_delay"})):
            if words in low:
                wanted = codes
                break
        for a in self.airports.values():
            score = 0.0
            if faa and a["faa"] == faa:
                score = 0.9
            elif len(low) >= 3 and low in a["name"].lower():
                score = 0.62
            elif wanted and any(s["code"] in wanted for s in a["statuses"]):
                score = 0.45 + 0.05 * max(s["level"] for s in a["statuses"] if s["code"] in wanted)
            if score:
                out.append(dict(self.airport_entity(a), score=round(score, 3)))
        # flight restrictions
        m = re.search(r"(\d)[/_](\d{4})", u)
        for t in self.tfrs.values():
            if t["status"] == "ended":
                continue
            score = 0.0
            if m and t["id"] == f"{m.group(1)}/{m.group(2)}":
                score = 0.96
            elif len(low) >= 4 and t.get("place") and low in t["place"].lower():
                score = 0.6
            elif low in (t["type_label"] or "").lower() and len(low) >= 3:
                score = 0.5
            elif low in ("tfr", "tfrs", "flight restriction", "flight restrictions", "temporary flight restriction"):
                score = 0.45 + (0.05 if t["category"] == "vip" else 0.0)
            if score:
                out.append(dict(self.tfr_entity(t), score=round(score, 3)))
        # conflict-zone bulletins
        for c in self.active_czibs():
            score = 0.0
            names = [x["name"].lower() for x in c.get("countries") or []]
            if low in names or any(x["iso2"] == u for x in c.get("countries") or []):
                score = 0.7
            elif u in (c.get("firs") or []):
                score = 0.85
            elif ap.czib_base_id(u) == c["id"] or u.replace(" ", "") == (c.get("number") or "").upper().replace(" ", ""):
                score = 0.95
            elif len(low) >= 4 and low in c["subject"].lower():
                score = 0.6
            elif low in ("czib", "czibs", "conflict zone", "conflict zones", "airspace advisory", "airspace advisories"):
                score = 0.45
            if score:
                out.append(dict(self.czib_entity(c), score=round(score, 3)))
        out.sort(key=lambda e: -e["score"])
        return out[:limit]

    def events(self, since: float) -> list[dict]:
        out = []
        current = {(a["faa"], s["code"]) for a in self.airports.values() for s in a["statuses"]}
        rows = self.store.query("SELECT * FROM flights_airport_events WHERE last_t >= ? ORDER BY last_t DESC LIMIT 300",
                                (since,))
        for r in rows:
            out.append({"kind": "airport_delay", "id": f"{r['icao'] or r['faa']}:{r['code']}:{int(r['first_t'])}", "title": r["title"],
                        "subtitle": r["subtitle"], "lat": r["lat"], "lon": r["lon"], "time": r["last_t"],
                        "first_time": r["first_t"], "severity": int(r["severity"]), "event": r["code"],
                        "active": (r["faa"], r["code"]) in current and self.clock() - r["last_t"] < 1900,
                        "layer": LAYER_DELAYS})
        for t in self.tfrs.values():
            sev = self.tfr_event_severity(t)
            issued = t.get("issued")
            if not sev or not issued or issued < since or not t.get("center"):
                continue
            where = t.get("place") or t["id"]
            near = f" near {t['city']}" if t.get("city") and t["category"] != "vip" else ""
            ent = self.tfr_entity(t)
            ent.update({"title": f"New flight restriction{near}: {_lc(t['type_label'])}, {where}",
                        "subtitle": ", ".join(x for x in (f"FDC {t['id']}", self._span_text(t), "FAA") if x),
                        "time": issued, "severity": sev, "event": "tfr_new"})
            out.append(ent)
        for c in self.active_czibs():
            if not c.get("label"):
                continue
            if c.get("issued") and c["issued"] >= since:
                sev, what = 4, "New EASA conflict zone bulletin"
                t = c["issued"]
            elif c.get("revised") and c["revised"] >= since and c.get("content_changed"):
                sev, what = 3, "Revised EASA conflict zone bulletin"
                t = c["revised"]
            else:
                continue
            ent = self.czib_entity(c)
            ent.update({"title": f"{what}: {c['subject']}", "time": t, "severity": sev, "event": "czib"})
            out.append(ent)
        return out

    @staticmethod
    def _span_text(t: dict) -> str | None:
        fmt = lambda x: time.strftime("%d %b %H:%M UTC", time.gmtime(x)).lstrip("0")  # noqa: E731
        if t.get("start") and t.get("end"):
            return f"{fmt(t['start'])} to {fmt(t['end'])}"
        if t.get("start") and t.get("permanent"):
            return f"from {fmt(t['start'])} until further notice"
        return None

    # =========================================================================================
    # housekeeping and diagnostics
    # =========================================================================================
    def housekeeping(self) -> None:
        now = self.clock()
        self.store.execute("DELETE FROM flights_airport_events WHERE last_t < ?", (now - EVENTS_KEEP_S,))
        live = {t["nid"] for t in self.tfrs.values() if t.get("nid")}
        if self.tfr_t:
            rows = self.store.query("SELECT nid FROM flights_tfr_detail WHERE fetched_at < ?", (now - DETAIL_KEEP_S,))
            stale = [(r["nid"],) for r in rows if r["nid"] not in live]
            if stale:
                self.store.executemany("DELETE FROM flights_tfr_detail WHERE nid = ?", stale)
        if len(self._episodes) > 2000:
            self._episodes.clear()

    def status(self) -> dict:
        now = self.clock()
        return {"nas": dict(self.nas_feed.info(now), airports=len(self.airports)),
                "tfr": dict(self.tfr_feed.info(now), listed=len(self.tfrs),
                            mapped=sum(1 for t in self.tfrs.values() if t["polygons"]),
                            details=sum(1 for t in self.tfrs.values() if t["has_detail"])),
                "czib": dict(self.czib_feed.info(now), bulletins=len(self.czibs), active=len(self.active_czibs()),
                             shapes=dict(self.czib_shapes)),
                "in_use": {k: self.in_use(k) for k in (LAYER_DELAYS, LAYER_TFR, LAYER_CZIB)}}
