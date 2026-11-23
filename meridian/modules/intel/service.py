"""The intel module's state and background work: GDELT ingestion, derived layers (risk zones,
convergences, instability index), DOC feeds, outages and the lookups behind every endpoint."""

from __future__ import annotations

import re
import threading
import time
from collections import Counter, OrderedDict

from meridian import geo
from meridian.net import NetError

from . import codes, convergence, countries, cyber, db, gdelt, geomath, news, protests, risk, ucdp
from .service_plus import IntelPlus

SRC_GDELT = "intel.gdelt_events"
SRC_DOC = "intel.gdelt_doc"
SRC_DEEPSTATE = "intel.deepstate"
SRC_NE = "intel.natural_earth"
SRC_WB = "intel.worldbank"
SRC_FEODO = "intel.feodo"
SRC_IODA = "intel.ioda"
SRC_CF = "intel.cloudflare_radar"
SRC_UCDP = "intel.ucdp"
SRC_DERIVED = "intel.derived"
SRC_CF_BGP = "intel.cloudflare_bgp"
SRC_FCDO = "intel.fcdo"
SRC_US = "intel.us_advisories"
SRC_SEED = "intel.gdelt_seed"

EMPTY_FC = {"type": "FeatureCollection", "features": []}
JERUSALEM = (31.778, 35.235)
NO_COUNTRY_PLACES = {"jerusalem", "east jerusalem", "west jerusalem"}


def _r5(x):
    return round(float(x), 5)


class Intel(IntelPlus):
    def __init__(self, m):
        self.m = m
        self.store = m.store
        self.net = m.net
        self.log = m.log
        self.lock = threading.RLock()
        self.backfill_hours = max(1.0, min(72.0, m.config.get_float("INTEL_BACKFILL_HOURS", 48.0)))
        # countries
        self.countries: dict[str, dict] = {}
        self.country_index: geomath.PolygonIndex | None = None
        self.crimea_geom = {"type": "Polygon", "coordinates": [countries.CRIMEA]}
        self.proper_words: set[str] = set()
        self.wb: dict[str, dict] = {}
        self.wb_meta: dict = {}
        self.index: dict[str, dict] = {}
        self.index_at: float | None = None
        self.index_coverage = 0.0
        # derived
        self.zones: list[dict] = []
        self.zones_by_id: dict[str, dict] = {}
        self.zones_fc: dict = EMPTY_FC
        self.zones_at: float | None = None
        self.convergences: list[dict] = []
        self.conv_by_id: dict[str, dict] = {}
        self.conv_at: float | None = None
        self.conv_inputs: dict = {}
        self.places: list[dict] = []
        self.gazetteers: dict[str, dict] = {}
        self.derive_error: str | None = None
        # other layers
        self.frontline_meta: dict | None = None
        self.feodo: dict[str, dict] = {}
        self.feodo_meta: dict = {}
        self.outages: dict[str, list] = {"ioda": [], "cloudflare": []}
        self.outages_at: dict[str, float | None] = {"ioda": None, "cloudflare": None}
        self.ucdp: dict | None = None
        self.ucdp_cache: OrderedDict = OrderedDict()
        # ingestion
        self.ingest = {"latest": None, "last_ok": None, "pending": None, "error": None, "files_ok": 0,
                       "files_missing": 0, "events_new": 0}
        self.derive_wake = threading.Event()
        self.doc = news.DocFeeds(m.net, m.log, status_fn=lambda **kw: m.status(SRC_DOC, **kw))
        self.init_plus()

    # =========================================================================================
    # Countries
    # =========================================================================================
    def load_countries(self) -> None:
        fc = countries.load_json(self.m.data_dir / "countries.geojson")
        items, props_by = [], {}
        proper = set()
        for f in fc.get("features") or []:
            p = f["properties"]
            iso2 = p.get("iso2") or ""
            bb = geo.geometry_bbox(f["geometry"])
            key = iso2 or f"x:{p.get('name')}"
            items.append((key, f["geometry"], {"iso2": iso2, "name": p.get("name"), "note": p.get("note") or ""}))
            if iso2:
                cur = props_by.get(iso2)
                area = (bb[2] - bb[0]) * (bb[3] - bb[1]) if bb else 0
                if cur is None or area > cur.get("_area", 0):
                    entry = dict(p)
                    entry["bbox"] = [round(v, 4) for v in bb] if bb else None
                    entry["_area"] = area
                    props_by[iso2] = entry
                for w in re.findall(r"[A-Za-z]{3,}", p.get("name") or ""):
                    proper.add(w.lower())
        idx = geomath.PolygonIndex(items)
        with self.lock:
            self.country_index = idx
            self.countries = props_by
            self.proper_words = proper
        self.m.status(SRC_NE, ok=True, count=len(props_by), note=f"{len(items)} shapes, {len(props_by)} countries")
        self.derive_wake.set()

    def load_worldbank(self) -> None:
        data = countries.load_json(self.m.data_dir / "worldbank.json")
        econ = data.get("economies") or {}
        for e in econ.values():
            e["capital"] = countries.CAPITAL_NAMES.get(e.get("capital") or "", e.get("capital"))
        with self.lock:
            self.wb = econ
            self.wb_meta = {"built_at": data.get("built_at"), "failures": data.get("failures") or [],
                            "indicators": data.get("indicators") or {}}
            for e in econ.values():
                for w in re.findall(r"[A-Za-z]{3,}", e.get("capital") or ""):
                    self.proper_words.add(w.lower())
        note = f"{len(econ)} economies"
        if data.get("failures"):
            note += f", {len(data['failures'])} indicators missing"
        self.m.status(SRC_WB, ok=True, count=len(econ), note=note)
        self.derive_wake.set()

    def iso_of(self, lat: float, lon: float, fips: str, iso2: str | None) -> str | None:
        """GDELT files Gaza and West Bank places under Israel; the polygon decides between the two.
        Jerusalem (status disputed) keeps GDELT's attribution and is shown without a country."""
        if (iso2 in ("IL", "PS") or fips in ("IS", "GZ", "WE")) and geo.haversine_km(lat, lon, *JERUSALEM) > 12.0:
            idx = self.country_index
            hit = idx.lookup(lat, lon) if idx else None
            if hit and hit[0] in ("IL", "PS"):
                return hit[0]
        return iso2

    def country_at(self, lat: float, lon: float) -> dict | None:
        idx = self.country_index
        if idx is None:
            return None
        hit = idx.lookup(lat, lon) or idx.nearest(lat, lon, 25.0)
        crimea = geo.point_in_geometry(lon, lat, self.crimea_geom)
        if not hit and not crimea:
            return None
        key, meta = hit if hit else ("RU", {"iso2": "RU", "name": "Russia", "note": ""})
        iso2 = meta.get("iso2") or None
        out = {"iso2": iso2, "name": meta.get("name"), "note": meta.get("note") or ""}
        if crimea:
            out.update({"iso2": None, "name": "Crimea", "note": countries.CRIMEA_NOTE, "related": ["UA", "RU"]})
        return out

    def country_name(self, iso2: str | None) -> str:
        if not iso2:
            return ""
        p = self.countries.get(iso2)
        if p:
            return p.get("name") or iso2
        e = self.wb.get(iso2)
        return (e or {}).get("name") or iso2

    def resolve_country(self, text: str | None) -> str | None:
        t = (text or "").strip()
        if not t:
            return None
        up = t.upper()
        if len(up) == 2 and (up in self.countries or up in self.wb):
            return up
        if len(up) == 3:
            for iso2, p in self.countries.items():
                if p.get("iso3") == up:
                    return iso2
        low = t.lower()
        best = None
        for iso2, p in self.countries.items():
            names = {(p.get("name") or "").lower(), (p.get("short") or "").lower(), (self.wb.get(iso2) or {}).get("name", "").lower()}
            if low in names:
                return iso2
            if best is None and any(n.startswith(low) for n in names if n) and len(low) >= 3:
                best = iso2
        return best

    # =========================================================================================
    # GDELT ingestion (continuous: the conflict layers need a rolling history)
    # =========================================================================================
    def gdelt_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            backlog = False
            try:
                text = self.net.get_text(gdelt.LASTUPDATE_URL, timeout=20, retries=1)
                latest = gdelt.parse_lastupdate(text)
                if not latest:
                    raise ValueError("lastupdate.txt lists no export file")
                self.ingest["latest"] = latest
                slots = gdelt.slots_back(latest, self.backfill_hours)
                states = db.file_states(self.store, slots[-1])
                now = time.time()
                todo = [ts for ts in slots if ts not in states
                        or (states[ts]["status"] == "error" and now - states[ts]["fetched_at"] > 1800)
                        or (states[ts]["status"] == "retry" and now - states[ts]["fetched_at"] > 240)]
                self.ingest["pending"] = len(todo)
                done = 0
                for ts in todo:
                    if stop.is_set():
                        return
                    self.ingest_file(ts)
                    done += 1
                    self.ingest["pending"] = len(todo) - done
                    if done % 12 == 0:
                        self.derive_wake.set()
                    if done >= 48:
                        break
                    if stop.wait(1.0):
                        return
                backlog = len(todo) > done
                if done:
                    self.derive_wake.set()
                self.ingest["error"] = None
                n = db.count_events(self.store)
                cov = db.coverage_days(self.store, 14)
                self.m.status(SRC_GDELT, ok=True, count=n,
                              note=f"{n:,} conflict events, {cov * 24:.0f} h of exports in 14 days"
                                   + (f", {len(todo) - done} files still to fetch" if backlog else ""))
            except Exception as err:
                self.ingest["error"] = str(err)[:300]
                self.m.status(SRC_GDELT, ok=False, error=str(err))
                self.log.warning("GDELT poll failed: %s", err)
            if stop.wait(5.0 if backlog else 300.0):
                return

    def ingest_file(self, ts: str) -> bool:
        url = gdelt.export_url(ts)
        try:
            data = self.net.get_bytes(url, timeout=45, retries=1, max_bytes=40_000_000)
        except NetError as err:
            if err.status == 404:
                # a file listed moments ago may not be published yet: retry recent slots, give up on old ones
                recent = time.time() - gdelt.ts_to_epoch(ts) < 2 * 3600
                db.mark_file(self.store, ts, "retry" if recent else "missing")
                if not recent:
                    self.ingest["files_missing"] += 1
                return False
            db.mark_file(self.store, ts, "error")
            self.log.info("GDELT export %s failed: %s", ts, err)
            return False
        try:
            text = gdelt.read_export_zip(data)
        except Exception as err:   # BadZipFile, odd layout, decode problems: skip this file
            db.mark_file(self.store, ts, "error")
            self.log.info("GDELT export %s unreadable: %s", ts, err)
            return False
        stats = self.ingest_text(text)
        db.mark_file(self.store, ts, "ok", stats["rows"], stats["kept"], stats["news"], len(data))
        self.ingest["files_ok"] += 1
        self.ingest["last_ok"] = time.time()
        self.ingest["events_new"] += stats["new"]
        return True

    def ingest_text(self, text: str, mode: str = "full", ts: str | None = None, nbytes: int = 0,
                    now: float | None = None) -> dict:
        """Process one export file.

        ``full`` (the normal path): conflict events, protests, news points and every tally. ``tallies`` (history
        seed, files older than the event window): the daily tallies only, with the file marked ``seed``.
        ``retally``: files ingested before protests and hourly tallies existed get just those (protest events,
        protest counts, hourly and domain tallies). The two seed modes write the tallies and the file record in
        one transaction (they need ``ts``)."""
        now = now or time.time()
        full = mode == "full"
        reports, protest_reports, items = [], [], []
        tallies: dict[tuple[str, int], list] = {}
        hours: dict[tuple[str, int], list] = {}
        dom_count: dict[tuple[str, int], Counter] = {}
        cells: dict[tuple[str, int], list] = {}
        rows = 0
        proper = self.proper_words
        all_rows = list(gdelt.iter_rows(text))
        focus = gdelt.focus_by_location(all_rows) if full else None
        news_cut = now - db.NEWS_DAYS * 86400
        cell_cut = gdelt.epoch_to_day(now - 8 * 86400)
        hour_cut = now - db.HOUR_DAYS * 86400
        dom_cut = now - db.DOMAIN_DAYS * 86400
        for row in all_rows:
            rows += 1
            iso2, day, tone, w, conflict = gdelt.tally_row(row)
            n = None
            if full:
                ev = gdelt.conflict_row(row, iso_of=self.iso_of, focus=focus)
                if ev:
                    reports.append(ev)
                pr = protests.protest_row(row, iso_of=self.iso_of)
                if pr:
                    protest_reports.append(pr)
                n = gdelt.news_row(row, iso_of=self.iso_of, proper=proper)
                if n and n["t"] >= news_cut:
                    items.append(n)
                ck = gdelt.cell_of_row(row)
                if ck and day and day >= cell_cut:
                    cv = cells.setdefault((ck, day), [0, 0])
                    cv[0] += 1
                    cv[1] += 1 if conflict else 0
            else:
                if mode == "retally":
                    pr = protests.protest_row(row, iso_of=self.iso_of)
                    if pr:
                        protest_reports.append(pr)
                if iso2 == "IL":
                    n = gdelt.news_row(row, iso_of=self.iso_of, proper=proper)
            if iso2 and day:
                if iso2 == "IL" and n:
                    iso2 = n["iso2"] or iso2
                protest = gdelt.is_protest_row(row)
                t = tallies.setdefault((iso2, day), [0, 0, 0.0, 0.0, 0])
                t[0] += 1
                t[1] += 1 if conflict else 0
                t[2] += tone * w
                t[3] += w
                t[4] += 1 if protest else 0
                hour = gdelt.hour_of_row(row) if mode != "tallies" else 0
                if hour and hour >= hour_cut:
                    hv = hours.setdefault((iso2, hour), [0, 0, 0, 0.0, 0.0])
                    hv[0] += 1
                    hv[1] += 1 if conflict else 0
                    hv[2] += 1 if protest else 0
                    hv[3] += tone * w
                    hv[4] += w
                    if hour >= dom_cut:
                        dom = gdelt.domain_of(row[gdelt.C_URL].strip())
                        if dom:
                            dom_count.setdefault((iso2, hour), Counter())[dom[:80]] += 1
        domains = {(iso2, hour, dom): k for (iso2, hour), cnt in dom_count.items() for dom, k in cnt.most_common(3)}
        if full:
            new, merged = db.upsert_events(self.store, reports)
            p_new, _ = db.upsert_events(self.store, protest_reports, table="intel_protests",
                                        severity_fn=protests.severity_of)
            nn = db.upsert_news(self.store, items)
            db.add_tallies(self.store, tallies)
            db.add_hour_tallies(self.store, hours)
            db.add_domain_tallies(self.store, domains)
            db.add_cell_tallies(self.store, cells)
            return {"rows": rows, "kept": len(reports), "new": new, "merged": merged, "news": nn,
                    "protests": len(protest_reports), "protests_new": p_new}
        if not ts:
            raise ValueError("seed modes need the file timestamp")
        p_new = 0
        if protest_reports:          # own transaction; merging skips reports already counted (GDELT event ids)
            p_new, _ = db.upsert_events(self.store, protest_reports, table="intel_protests",
                                        severity_fn=protests.severity_of)
        with self.store.tx() as c:
            if mode == "tallies":
                db.add_tallies(self.store, tallies, conn=c)
                db.mark_file(self.store, ts, "seed", rows, 0, 0, nbytes, conn=c)
            else:
                db.add_tallies(self.store, tallies, conn=c, protest_only=True)
                db.add_hour_tallies(self.store, hours, conn=c)
                db.add_domain_tallies(self.store, domains, conn=c)
                c.execute("UPDATE intel_files SET tallies = ? WHERE ts = ?", (db.TALLIES_VERSION, ts))
        return {"rows": rows, "kept": 0, "new": 0, "merged": 0, "news": 0, "protests": len(protest_reports),
                "protests_new": p_new}

    # =========================================================================================
    # Derived layers
    # =========================================================================================
    def derive_loop(self, stop: threading.Event) -> None:
        last = 0.0
        first = True
        while not stop.is_set():
            self.derive_wake.wait(25.0 if first else 600.0)
            first = False
            if stop.is_set():
                return
            self.derive_wake.clear()
            gap = time.time() - last
            if gap < 60.0:
                if stop.wait(60.0 - gap):
                    return
            last = time.time()
            self.derive()

    def derive(self) -> None:
        now = time.time()
        errors = []
        try:
            db.recompute_country_days(self.store, gdelt.epoch_to_day(now - (db.EVENT_DAYS + 1) * 86400))
            events7 = db.query_events(self.store, since=now - 7 * 86400, precise_only=True, limit=30000)
            shares = db.cell_shares(self.store, gdelt.epoch_to_day(now - risk.WINDOW_DAYS * 86400))
            zones = risk.compute_zones(events7, now, shares=shares)
            for z in zones:
                z["country"] = self.country_name(z.get("iso2"))
                z["places"] = [(self.place_display(p, self.country_name(iso)), n) for (p, iso), n in z["places"]]
                z["place"] = z["places"][0][0] if z["places"] else ""
            zones_fc = risk.zones_geojson(zones)
            places = db.event_places(self.store, now - 7 * 86400)
            gaz = {th["id"]: news.build_gazetteer(places, th) for th in news.THEATRES}
            with self.lock:
                self.zones = zones
                self.zones_by_id = {z["id"]: z for z in zones}
                self.zones_fc = zones_fc
                self.zones_at = now
                self.places = places
                self.gazetteers = gaz
        except Exception as err:
            errors.append(f"risk zones: {err}")
            self.log.warning("intel derive (zones) failed: %s", err)
        try:
            self.compute_convergences(now)
        except Exception as err:
            errors.append(f"convergences: {err}")
            self.log.warning("intel derive (convergences) failed: %s", err)
        try:
            self.compute_index(now)
        except Exception as err:
            errors.append(f"instability index: {err}")
            self.log.warning("intel derive (index) failed: %s", err)
        try:
            self.derive_plus(now)
        except Exception as err:
            errors.append(f"news spikes: {err}")
            self.log.warning("intel derive (spikes) failed: %s", err)
        self.derive_error = "; ".join(errors) or None
        self.m.status(SRC_DERIVED, ok=self.derive_error is None, count=len(self.zones),
                      error=self.derive_error,
                      note=f"{len(self.zones)} risk zones, {len(self.convergences)} convergences, "
                           f"{sum(1 for v in self.index.values() if v.get('index') is not None)} countries indexed, "
                           f"news spikes: {self.spikes_note()}")

    def compute_convergences(self, now: float) -> None:
        events = self.m.app.collect_events(now - convergence.WINDOW_HOURS * 3600)
        kinds = Counter(e.get("kind") for e in events)
        found = convergence.detect(events, now)
        for cv in found:
            c = self.country_at(cv["lat"], cv["lon"])
            cv["country"] = (c or {}).get("name") or ""
            cv["iso2"] = (c or {}).get("iso2")
            cv["subtitle"] = (f"{cv['country']}, " if cv["country"] else "") + f"{cv['n_events']} events in 24 h"
        with self.lock:
            self.convergences = found
            self.conv_by_id = {c["id"]: c for c in found}
            self.conv_at = now
            self.conv_inputs = {"events": len(events), "kinds": dict(kinds.most_common(30))}

    def compute_index(self, now: float) -> None:
        if not self.countries and not self.wb:
            return
        cov = db.coverage_days(self.store, 30, now)
        totals = db.country_totals(self.store, gdelt.epoch_to_day(now - 30 * 86400), gdelt.epoch_to_day(now - 7 * 86400))
        hazard_feed = any(name in self.m.app.modules and self.m.app.modules[name].ok for name in ("hazards", "weather"))
        hz: dict[str, list] = {}
        if hazard_feed:
            for ev in self.m.app.collect_events(now - 7 * 86400, kinds=countries.HAZARD_KINDS):
                c = self.country_at(float(ev["lat"]), float(ev["lon"]))
                if c and c.get("iso2"):
                    h = hz.setdefault(c["iso2"], [0.0, 0])
                    h[0] += float(ev.get("severity") or 0) / 10.0
                    h[1] += 1
        out = {}
        for iso2 in set(self.countries) | set(self.wb):
            e = self.wb.get(iso2) or {}
            ind = e.get("ind") or {}
            pv = ind.get("stability")
            pop = (ind.get("pop") or [None])[0] or (self.countries.get(iso2) or {}).get("pop_est")
            t = totals.get(iso2) or {"rows": 0, "conflict_rows": 0, "events": 0, "sev_sum": 0.0, "tone": None, "tone_rows": 0}
            h = hz.get(iso2, [0.0, 0])
            res = countries.instability(
                pv=pv[0] if pv else None, pv_year=pv[1] if pv else None, sev_sum=t.get("sev_sum") or 0.0,
                rows=int(t.get("rows") or 0), conflict_rows=int(t.get("conflict_rows") or 0), events=int(t.get("events") or 0),
                coverage_days=cov, population=pop, tone=t.get("tone"), tone_rows=int(t.get("tone_rows") or 0),
                hazards_weighted=h[0] if hazard_feed else None, hazards_count=h[1])
            out[iso2] = res
        with self.lock:
            self.index = out
            self.index_at = now
            self.index_coverage = cov

    def index_summary(self) -> dict:
        with self.lock:
            idx = dict(self.index)
            at = self.index_at
        values = {}
        for iso2, r in idx.items():
            if r.get("index") is None:
                continue
            parts = {p["key"]: p["score"] for p in r["parts"]}
            values[iso2] = {"i": r["index"], "b": r["band"], "g": parts.get("governance"), "c": parts.get("conflict"),
                            "t": parts.get("tone"), "h": parts.get("hazards")}
        return {"computed_at": at, "coverage_days": round(self.index_coverage, 2), "countries": values,
                "weights": countries.WEIGHTS}

    # =========================================================================================
    # DOC feeds and outages (only while the browser uses the module)
    # =========================================================================================
    def doc_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            th = None
            if self.m.active(within=900):
                th = self.doc.next_due(time.time())
            if th is not None:
                self.doc.fetch(th)
                if stop.wait(news.DOC_INTERVAL_S):
                    return
            elif stop.wait(20.0):
                return

    def request_outages(self) -> None:
        """Start a one-off outage poll in the background when the data is missing or stale (non-blocking)."""
        last = self.outages_at.get("ioda")
        if (last and time.time() - last < 1800) or getattr(self, "_outage_thread", None) is not None:
            return

        def run():
            try:
                self.poll_outages()
            finally:
                self._outage_thread = None

        self._outage_thread = threading.Thread(target=run, daemon=True, name="intel.outages-now")
        self._outage_thread.start()

    def poll_outages(self) -> None:
        now = time.time()
        try:
            payload = self.net.get_json(cyber.IODA_URL, params={"from": int(now - 86400), "until": int(now),
                                                                "entityType": "country"}, timeout=25, retries=0, ttl=900)
            items = cyber.parse_ioda(payload)
            with self.lock:
                self.outages["ioda"] = items
                self.outages_at["ioda"] = now
            self.m.status(SRC_IODA, ok=True, count=len(items), note=f"{len(items)} countries with outage signals in 24 h")
        except Exception as err:
            self.m.status(SRC_IODA, ok=False, error=f"IODA: {err}")
        self.poll_radar()          # Cloudflare Radar keeps its own cadence (radar.RadarFeed)

    # =========================================================================================
    # Dataset loaders
    # =========================================================================================
    def load_frontline_meta(self) -> None:
        meta = countries.load_json(self.m.data_dir / "frontlines_meta.json")
        with self.lock:
            self.frontline_meta = meta
        self.m.status(SRC_DEEPSTATE, ok=True, count=meta.get("front_pieces"),
                      note=f"{meta.get('occupied_km2', 0):,} km² occupied, front about {meta.get('front_km', 0):,} km")

    def load_feodo(self) -> None:
        data = countries.load_json(self.m.data_dir / "feodo.json")
        agg = cyber.aggregate_feodo(data.get("entries") or [])
        with self.lock:
            self.feodo = agg
            self.feodo_meta = {"built_at": data.get("built_at"), "count": data.get("count", 0)}
        self.m.status(SRC_FEODO, ok=True, count=data.get("count", 0),
                      note=f"{data.get('count', 0)} listed servers in {len(agg)} countries")

    def load_ucdp(self) -> None:
        data = countries.load_json(self.m.data_dir / "ucdp_cells.json")
        with self.lock:
            self.ucdp = data
            self.ucdp_cache.clear()
        years = sorted(int(y) for y in (data.get("years") or {}))
        self.m.status(SRC_UCDP, ok=True, count=data.get("events"),
                      note=f"GED {data.get('version')}: {data.get('events', 0):,} events {years[0] if years else ''}"
                           f"–{years[-1] if years else ''}, {len(data.get('cells') or []):,} cells")

    def ucdp_fc(self, y0: int, y1: int) -> dict:
        key = (y0, y1)
        with self.lock:
            if key in self.ucdp_cache:
                self.ucdp_cache.move_to_end(key)
                return self.ucdp_cache[key]
            data = self.ucdp
        if not data:
            return EMPTY_FC
        fc = ucdp.cells_geojson(data, y0, y1)
        with self.lock:
            self.ucdp_cache[key] = fc
            while len(self.ucdp_cache) > 8:
                self.ucdp_cache.popitem(last=False)
        return fc

    # =========================================================================================
    # Shapes for the API
    # =========================================================================================
    @staticmethod
    def event_title(ev: dict) -> str:
        label = codes.event_label(ev.get("code"), ev.get("root"))
        short = gdelt.short_place(ev.get("place") or "")
        return f"{label} in {short}" if short else label

    def event_entity(self, ev: dict) -> dict:
        return {"kind": "conflict_event", "id": ev["id"], "title": self.event_title(ev),
                "subtitle": f"{self.place_display(ev.get('place') or '', self.country_name(ev.get('iso2'))) or 'Unknown place'}, "
                            f"reported {time.strftime('%d %b %H:%M UTC', time.gmtime(ev['t_last']))}",
                "lat": _r5(ev["lat"]), "lon": _r5(ev["lon"]), "layer": "intel.conflict", "time": ev["t_last"],
                "severity": ev["severity"], "zoom": 9}

    def event_detail(self, ev: dict) -> dict:
        d = {k: ev.get(k) for k in ("id", "day", "t_first", "t_last", "root", "code", "goldstein", "category", "armed",
                                    "crime", "precision", "place", "fips", "iso2", "actor1", "actor2", "actors", "mentions",
                                    "sources", "articles", "tone", "reports", "urls", "severity", "focus")}
        d["lat"], d["lon"] = _r5(ev["lat"]), _r5(ev["lon"])
        d["action"] = codes.event_label(ev.get("code"), ev.get("root"))
        d["root_label"] = codes.CAMEO_ROOT.get(ev.get("root"), "")
        d["category_label"] = codes.CATEGORY_LABEL.get(ev.get("category"), "")
        d["country"] = self.country_name(ev.get("iso2"))
        d["title"] = self.event_title(ev)
        d["place_display"] = self.place_display(ev.get("place") or "", d["country"])
        d["main_url"] = ev.get("url_main")
        d["date"] = gdelt.day_to_date(ev["day"]).isoformat() if gdelt.day_to_date(ev["day"]) else None
        d["note"] = "Machine-coded from news reports, not verified."
        d["theatres"] = [news.THEATRE_BY_ID[t]["name"] for t in news.theatres_of(ev["lat"], ev["lon"], ev.get("iso2"))]
        words = self.proper_words | set(re.findall(r"[A-Za-z]+", ev.get("place") or ""))
        d["sources_list"] = [{"url": u, "domain": gdelt.domain_of(u), "title": gdelt.slug_title(u, words)} for u in ev.get("urls") or []]
        return d

    @staticmethod
    def place_display(place: str, country: str) -> str:
        """'Gaza, Israel' with our country Palestine -> 'Gaza, Palestine' (GDELT's gazetteer files Gaza under Israel)."""
        short = gdelt.short_place(place)
        if short.lower() in NO_COUNTRY_PLACES:
            return short
        if not country:
            return gdelt.brief_place(place)
        if short.lower() == country.lower():
            return country
        return f"{short}, {country}" if short else country

    @staticmethod
    def news_title(n: dict) -> str:
        return n.get("title") or f"Report from {n.get('domain') or 'an unknown site'}"

    def news_entity(self, n: dict) -> dict:
        return {"kind": "news", "id": n["id"], "title": self.news_title(n),
                "subtitle": ", ".join(x for x in (n.get("domain"), self.place_display(n.get("place") or "", self.country_name(n.get("iso2")))) if x),
                "lat": _r5(n["lat"]) if n.get("lat") is not None else None,
                "lon": _r5(n["lon"]) if n.get("lon") is not None else None,
                "layer": "intel.news", "time": n.get("t"), "severity": n.get("severity"), "zoom": 9}

    def doc_located(self, item: dict, theatre_id: str) -> dict:
        th = news.THEATRE_BY_ID.get(theatre_id) or news.THEATRES[0]
        out = dict(item)
        hit = news.locate_title(item.get("title") or "", self.gazetteers.get(theatre_id) or {})
        if hit:
            out.update({"place": hit[0], "lat": round(hit[1], 5), "lon": round(hit[2], 5), "approx": False})
        else:
            out.update({"place": th["name"], "lat": th["lat"], "lon": th["lon"], "approx": True})
        out["source"] = "doc"
        return out

    def feeds(self, theatre_id: str | None = None, min_sev: float = 0.0, q: str | None = None,
              hours: float = 24.0, limit: int = 25) -> dict:
        now = time.time()
        ths = [news.THEATRE_BY_ID[theatre_id]] if theatre_id else news.THEATRES
        self.doc.want(theatre_id)
        q = (q or "").strip()[:80] or None
        rows = db.query_news(self.store, since=now - hours * 3600, titled_only=True, min_severity=min_sev,
                             limit=6000, order="time", q=q)
        out = []
        for th in ths:
            g_items = [n for n in rows if news.in_theatre(th, n["lat"], n["lon"], n.get("iso2"))]
            doc_items, doc_state = self.doc.items(th["id"])
            merged, seen = [], set()
            for it in doc_items:
                if it["severity"] < min_sev or (q and q.lower() not in (it["title"] + " " + it["domain"]).lower()):
                    continue
                if it.get("t") and it["t"] < now - hours * 3600:
                    continue
                d = self.doc_located(it, th["id"])
                seen.add(d["url"])
                merged.append({"id": d["id"], "kind": "news", "source": "doc", "title": d["title"], "url": d["url"],
                               "domain": d["domain"], "t": d.get("t"), "severity": d["severity"], "lat": d["lat"],
                               "lon": d["lon"], "place": d["place"], "approx": d["approx"], "outlets": d.get("outlets", 1)})
            for n in g_items:
                if n["url"] in seen:
                    continue
                seen.add(n["url"])
                merged.append({"id": n["id"], "kind": "news", "source": "gdelt", "title": self.news_title(n), "url": n["url"],
                               "domain": n["domain"], "t": n["t"], "severity": n["severity"], "lat": _r5(n["lat"]),
                               "lon": _r5(n["lon"]), "place": self.place_display(n["place"], self.country_name(n.get("iso2"))),
                               "approx": False, "outlets": 1})
            merged.sort(key=lambda i: (-(i.get("t") or 0), -i["severity"]))
            out.append({"id": th["id"], "name": th["name"], "lat": th["lat"], "lon": th["lon"], "zoom": th["zoom"],
                        "bbox": news.theatre_bbox(th, lambda iso: (self.countries.get(iso) or {}).get("bbox")),
                        "count": len(merged), "doc_count": sum(1 for i in merged if i["source"] == "doc"),
                        "doc": doc_state, "items": merged[:limit]})
        return {"updated": now, "hours": hours, "theatres": out, "doc": self.doc.state()}

    def news_item(self, nid: str) -> dict | None:
        if nid.startswith("gn"):
            n = db.get_news(self.store, nid)
            if not n:
                return None
            d = dict(n)
            d["lat"], d["lon"] = _r5(n["lat"]), _r5(n["lon"])
            d["source"] = "gdelt"
            d["title"] = self.news_title(n)
            d["place"] = self.place_display(n.get("place") or "", self.country_name(n.get("iso2")))
            d["headline_from_link"] = bool(n.get("titled"))
            d["action"] = codes.event_label(n.get("code"), n.get("root"))
            d["country"] = self.country_name(n.get("iso2"))
            d["theatres"] = [news.THEATRE_BY_ID[t]["name"] for t in news.theatres_of(n["lat"], n["lon"], n.get("iso2"))]
            return d
        if nid.startswith("dn"):
            it = self.doc.get(nid)
            if not it:
                return None
            d = self.doc_located(it, it.get("theatre") or news.THEATRES[0]["id"])
            d["theatres"] = [news.THEATRE_BY_ID[it["theatre"]]["name"]] if it.get("theatre") in news.THEATRE_BY_ID else []
            return d
        return None

    # -- country profile -------------------------------------------------------------------------
    def profile(self, iso2: str) -> dict | None:
        iso2 = (iso2 or "").upper()
        props = self.countries.get(iso2)
        econ = self.wb.get(iso2)
        if not props and not econ:
            return None
        now = time.time()
        name = (props or {}).get("name") or (econ or {}).get("name") or iso2
        ind_meta = self.wb_meta.get("indicators") or {}
        indicators = {}
        for key, (value, year) in ((econ or {}).get("ind") or {}).items():
            indicators[key] = {"value": value, "year": year, "label": (ind_meta.get(key) or {}).get("label", key)}
        if "pop" not in indicators and props and props.get("pop_est"):
            indicators["pop"] = {"value": props["pop_est"], "year": props.get("pop_year"), "label": "Population (Natural Earth estimate)"}
        recent = db.query_events(self.store, iso2=iso2, since=now - 7 * 86400, limit=10)
        n7 = int(self.store.scalar("SELECT COUNT(*) FROM intel_events WHERE iso2 = ? AND t_last >= ? AND crime = 0",
                                   (iso2, now - 7 * 86400), 0))
        n_news = int(self.store.scalar("SELECT COUNT(*) FROM intel_news WHERE iso2 = ? AND t >= ?", (iso2, now - 86400), 0))
        top_news = db.query_news(self.store, iso2=iso2, since=now - 86400, titled_only=True, limit=5)
        label = (props or {}).get("label")
        lat = label[1] if label else (econ or {}).get("lat")
        lon = label[0] if label else (econ or {}).get("lon")
        notes = []
        if props and props.get("note"):
            notes.append(props["note"])
        if iso2 in ("UA", "RU"):
            notes.append(countries.CRIMEA_NOTE)
        feodo = self.feodo.get(iso2)
        outages = [o for o in self.outages.get("ioda") or [] if o["iso2"] == iso2]
        bbox = (props or {}).get("bbox")
        return {
            "iso2": iso2, "iso3": (props or {}).get("iso3") or (econ or {}).get("iso3"), "name": name,
            "flag": codes.flag_emoji(iso2), "capital": (econ or {}).get("capital") or None,
            "capital_lat": (econ or {}).get("lat"), "capital_lon": (econ or {}).get("lon"),
            "region": (econ or {}).get("region") or (props or {}).get("subregion"),
            "income": (econ or {}).get("income") or None, "continent": (props or {}).get("continent"),
            "lat": lat, "lon": lon, "bbox": bbox if bbox and bbox[2] - bbox[0] < 60 else None,
            "indicators": indicators, "instability": self.index.get(iso2), "index_at": self.index_at,
            "conflict": {"events_7d": n7, "recent": [self.event_entity(e) | {"category": e["category"]} for e in recent]},
            "news": {"points_24h": n_news, "top": [self.news_entity(n) for n in top_news]},
            "theatres": [t["name"] for t in news.THEATRES if iso2 in t["countries"]],
            "cyber": {"c2_servers": feodo["count"] if feodo else 0, "c2_online": feodo["online"] if feodo else 0,
                      "outage_signals": outages[0] if outages else None},
            "notes": notes,
            "sources": ["World Bank WDI and WGI (CC BY 4.0)", "Natural Earth (public domain)",
                        "GDELT 2.0 (machine-coded news events)"],
            **self.profile_plus(iso2, now),
        }

    # -- zones and convergences ------------------------------------------------------------------
    def zone_detail(self, zid: str) -> dict | None:
        z = self.zones_by_id.get(zid)
        if not z:
            return None
        members = []
        for eid in z.get("member_ids") or []:
            ev = db.get_event(self.store, eid)
            if ev:
                members.append(self.event_entity(ev) | {"category": ev["category"]})
        out = {k: v for k, v in z.items() if k not in ("cells", "outline", "member_ids")}
        out["members"] = members[:40]
        out["cells"] = len(z.get("cells") or [])
        out["computed_at"] = self.zones_at
        return out

    def zone_entity(self, z: dict) -> dict:
        name = z.get("short") or "Unnamed area"
        return {"kind": "risk_zone", "id": z["id"], "title": f"{name} area", "subtitle": f"{z['level']} risk, {z['events']} events in 7 days",
                "lat": z["lat"], "lon": z["lon"], "layer": "intel.risk", "bbox": z["bbox"], "time": z["latest"],
                "severity": min(10.0, round(3 + z["score"] / 4, 1))}

    def conv_entity(self, cv: dict) -> dict:
        return {"kind": "convergence", "id": cv["id"], "title": cv["title"], "subtitle": cv.get("subtitle") or "",
                "lat": cv["lat"], "lon": cv["lon"], "layer": "intel.convergences", "time": cv["time"],
                "severity": cv["severity"], "zoom": 8}

    # -- cyber -----------------------------------------------------------------------------------
    def cyber_fc(self) -> dict:
        feats = []
        with self.lock:
            feodo = dict(self.feodo)
            ioda = list(self.outages.get("ioda") or [])
            cf = list(self.outages.get("cloudflare") or [])
        for iso2, d in feodo.items():
            pos = self._label_point(iso2)
            if not pos:
                continue
            feats.append({"type": "Feature", "properties": {"role": "c2", "iso2": iso2, "name": self.country_name(iso2),
                                                            "count": d["count"], "online": d["online"]},
                          "geometry": {"type": "Point", "coordinates": pos}})
        by_cc: dict[str, dict] = {}
        for o in ioda:
            by_cc[o["iso2"]] = {"iso2": o["iso2"], "sev": o["severity"], "events": o["events"], "src": "IODA"}
        for o in cf:
            cur = by_cc.setdefault(o["iso2"], {"iso2": o["iso2"], "sev": 5.0, "events": 0, "src": "Cloudflare Radar"})
            cur["events"] += 1
            cur["sev"] = max(cur["sev"], 5.0)
            if "Cloudflare" not in cur["src"]:
                cur["src"] += ", Cloudflare Radar"
        self.radar_outage_rings(by_cc)
        for iso2, o in by_cc.items():
            pos = self._label_point(iso2)
            if not pos:
                continue
            feats.append({"type": "Feature", "properties": {"role": "outage", "iso2": iso2, "name": self.country_name(iso2),
                                                            "sev": o["sev"], "events": o["events"], "src": o["src"],
                                                            "radar": o.get("radar", 0), "active": o.get("active", 0)},
                          "geometry": {"type": "Point", "coordinates": pos}})
        feats.extend(self.bgp_features())
        return {"type": "FeatureCollection", "features": feats}

    def _label_point(self, iso2: str):
        p = self.countries.get(iso2)
        if p and p.get("label"):
            return p["label"]
        e = self.wb.get(iso2)
        if e and e.get("lat") is not None and e.get("lon") is not None:
            return [round(e["lon"], 4), round(e["lat"], 4)]
        return None

    def cyber_detail(self, kind: str, iso2: str) -> dict | None:
        iso2 = iso2.upper()
        name = self.country_name(iso2)
        pos = self._label_point(iso2)
        if kind == "c2":
            d = self.feodo.get(iso2)
            if not d:
                return None
            return {"iso2": iso2, "name": name, "flag": codes.flag_emoji(iso2), "count": d["count"], "online": d["online"],
                    "malware": d["malware"], "networks": d["networks"], "entries": d["entries"][:25],
                    "lat": pos[1] if pos else None, "lon": pos[0] if pos else None, "updated": self.feodo_meta.get("built_at")}
        if kind == "outage":
            ioda = [o for o in self.outages.get("ioda") or [] if o["iso2"] == iso2]
            cf = [o for o in self.outages.get("cloudflare") or [] if o["iso2"] == iso2]
            anomalies = self.radar_country(iso2)["anomalies"]
            if not ioda and not cf and not anomalies:
                return None
            return {"iso2": iso2, "name": name, "flag": codes.flag_emoji(iso2), "ioda": ioda[0] if ioda else None,
                    "cloudflare": cf[:10], "anomalies": anomalies[:10], "lat": pos[1] if pos else None,
                    "lon": pos[0] if pos else None,
                    "updated": max((v for v in self.outages_at.values() if v), default=None),
                    "radar_at": self.radar.at.get("anomalies"), "token": self.radar.has_token()}
        return None

    # -- status ----------------------------------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        return {
            "now": now, "ingest": dict(self.ingest), "backfill_hours": self.backfill_hours,
            "events_14d": db.count_events(self.store), "events_24h": db.count_events(self.store, now - 86400),
            "news_points": int(self.store.scalar("SELECT COUNT(*) FROM intel_news", (), 0)),
            "coverage_hours_14d": round(db.coverage_days(self.store, 14) * 24, 1),
            "zones": len(self.zones), "zones_at": self.zones_at, "convergences": len(self.convergences),
            "conv_at": self.conv_at, "conv_inputs": self.conv_inputs, "countries": len(self.countries),
            "economies": len(self.wb), "indexed": sum(1 for v in self.index.values() if v.get("index") is not None),
            "index_at": self.index_at, "frontlines": self.frontline_meta, "doc": self.doc.state(),
            "feodo": self.feodo_meta, "outages": {k: len(v) for k, v in self.outages.items()},
            "ucdp": {k: self.ucdp.get(k) for k in ("version", "events", "unmapped")} if self.ucdp else None,
            "derive_error": self.derive_error,
            **self.status_plus(),
        }

