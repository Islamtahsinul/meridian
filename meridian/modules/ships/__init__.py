"""Ships: live AIS vessel tracking with dark-ship estimates, sanctions matches, a straits monitor,
port activity and maritime anomaly candidates (ship-to-ship transfers, loitering, AIS gaps, GNSS
spoofing), merged from Digitraffic (keyless, Baltic), aisstream.io (key, global) and Kystverket
(optional raw NMEA stream, Norway), with IMF PortWatch's daily chokepoint transits beside the
straits monitor.

Ingestion runs continuously (not only while the browser is open) because dark-ship detection,
strait transit counts, port calls and the anomaly detectors need an unbroken history; Digitraffic is
polled every 60 s while the map is in use and every 2 minutes otherwise. PortWatch is weekly data:
about 30 small requests a week whether or not the map is open (portwatch.py).
"""

from __future__ import annotations

import re
import time

from meridian.net import USER_AGENT
from meridian.registry import HTTPError

from . import analysis
from . import portwatch as pwlib
from .anomalies import lists_text
from .classify import CLASS_IDS, CLASSES
from .history import register_history
from .providers import (SRC_AIS, SRC_DT, SRC_KV, AisStreamClient, DigitrafficPoller, KystverketClient, parse_area)
from .service import ShipsService

SOURCE_DT = "ships.digitraffic"
SOURCE_AIS = "ships.aisstream"
SOURCE_KV = "ships.kystverket"
SOURCE_PW = pwlib.SOURCE_ID
_ANOMALY_ID = re.compile(r"^(sts|loiter|gap|gnss):[0-9A-Za-z.,:_-]{1,80}$")
_CHOKEPOINT_ID = re.compile(r"^chokepoint[0-9]{1,3}$")
_SHARE = re.compile(r"([0-9]+)% of aircraft")


def _mmsi(req) -> int:
    raw = str(req.params.get("mmsi", "")).strip()
    if not (raw.isascii() and raw.isdigit() and len(raw) == 9):   # "²" passes isdigit() but not int()
        raise HTTPError(400, "BAD_ARG", "An MMSI is nine digits.")
    return int(raw)


def register(m) -> None:
    from meridian.modules import sanctions

    cfg = m.config
    m.key("AISSTREAM_API_KEY", "aisstream.io API key. Adds live AIS from community receivers worldwide "
          "(without it, ships cover Finnish waters and the northern Baltic only).", "https://aisstream.io/apikeys")
    m.key("AISSTREAM_AREA", "What to subscribe to on aisstream.io: world (default), straits (only the monitored "
          "straits, far less traffic) or your own boxes as west,south,east,north;west,south,east,north.",
          "https://aisstream.io/documentation", secret=False)
    m.key("KYSTVERKET_AIS", "Set to 1 to read the Norwegian Coastal Administration's open AIS stream "
          "(raw NMEA on TCP 153.44.253.27:5631, Norwegian waters). Some networks block it.",
          "https://www.kystverket.no/en/navigation-and-monitoring/ais/access-to-ais-data/", secret=False)

    m.key("SHIPS_PORTWATCH", "Set to 0 to stop fetching IMF PortWatch's daily chokepoint transits (about 30 small "
          "requests a week to services9.arcgis.com).", "https://portwatch.imf.org/", secret=False)

    ais_key = cfg.get("AISSTREAM_API_KEY")
    kv_on = cfg.get_bool("KYSTVERKET_AIS", False)
    pw_on = cfg.get_bool("SHIPS_PORTWATCH", True)

    m.source(SOURCE_DT, "Digitraffic marine AIS (Fintraffic): Finnish waters and the northern Baltic",
             "https://www.digitraffic.fi/en/marine-traffic/", "CC BY 4.0, Fintraffic / digitraffic.fi",
             refresh="every 60 s (2 min when the map is closed)", layer="ships")
    m.source(SOURCE_AIS, "aisstream.io live AIS (community receivers, global)", "https://aisstream.io/",
             "aisstream.io terms of service; personal use with a free key", refresh="streaming", layer="ships",
             needs_key=None if ais_key else "AISSTREAM_API_KEY")
    m.source(SOURCE_KV, "Kystverket open AIS stream (Norwegian waters)",
             "https://www.kystverket.no/en/navigation-and-monitoring/ais/access-to-ais-data/",
             "NLOD 2.0, Norwegian Coastal Administration", refresh="streaming", layer="ships",
             needs_key=None if kv_on else "KYSTVERKET_AIS=1")
    m.source(SOURCE_PW, "IMF PortWatch daily chokepoint transits (satellite AIS)", pwlib.SITE,
             "IMF terms: reuse with attribution to the IMF; small filtered requests only (no bulk download)",
             refresh="weekly, about a week behind", layer="ships.straits", optional=True,
             needs_key=None if pw_on else "SHIPS_PORTWATCH=1")

    providers: dict[str, object] = {}

    def has_global() -> bool:
        c = providers.get("aisstream")
        return bool(c is not None and time.time() - getattr(c, "last_message", 0.0) < 900)

    svc = ShipsService(db=m.store, data_dir=m.config.data_dir, lookup=sanctions.lookup_vessel, log=m.log,
                       has_global=has_global)
    svc.providers = providers
    try:
        svc.store.create_tables()
        loaded = svc.store.load()
        m.log.info("ships: restored %d vessels from the database", loaded)
    except Exception as err:   # a damaged table never stops the module: start empty
        m.log.warning("ships: could not restore history: %s", err)
    svc.refresh_snapshot()
    svc.anomalies.air_lookup = _air_lookup(m)
    try:
        svc.anomalies.create_tables()
        m.log.info("ships: restored %d anomaly episodes", svc.anomalies.restore())
    except Exception as err:
        m.log.warning("ships: could not restore anomaly episodes: %s", err)
    if pw_on:
        svc.portwatch = pwlib.PortWatch(m.store, m.net, log=m.log,
                                        status=lambda **kw: m.status(SOURCE_PW, **kw))
        try:
            svc.portwatch.create_tables()
        except Exception as err:
            m.log.warning("ships: PortWatch tables unavailable: %s", err)
            svc.portwatch = None
    else:
        m.status(SOURCE_PW, ok=None, note="Off (SHIPS_PORTWATCH=0).")

    # -- providers ------------------------------------------------------------------------------------
    def status_cb(sid):
        def cb(ok, count=None, error=None, note=None):
            m.status(sid, ok=ok, count=count, error=error, note=note)
        return cb

    poller = DigitrafficPoller(m.net, svc.store, status_cb(SOURCE_DT), m.log)
    providers["digitraffic"] = poller
    m.net.set_host_interval("meri.digitraffic.fi", 2.0)
    m.task("digitraffic", poller.poll, every=60, initial_delay=2,
           when=lambda: m.active() or time.time() - poller.last_run >= 115)

    if ais_key:
        boxes, area_note = parse_area(cfg.get("AISSTREAM_AREA"), svc.store.straits.defs)
        client = AisStreamClient(ais_key, boxes, svc.store, status_cb(SOURCE_AIS), m.log, user_agent=USER_AGENT,
                                 area_note=area_note)
        providers["aisstream"] = client
        m.thread("aisstream", client.run)
    if kv_on:
        kv = KystverketClient(svc.store, status_cb(SOURCE_KV), m.log)
        providers["kystverket"] = kv
        m.thread("kystverket", kv.run)
    else:
        m.status(SOURCE_KV, ok=None, note="Off. Set KYSTVERKET_AIS=1 in .env to read the Norwegian stream "
                                          "(needs outbound TCP port 5631).")

    # -- periodic work ---------------------------------------------------------------------------------
    try:   # resume the hourly strait samples where the last run left off (restarts must not add extra)
        last_sample = float(m.store.scalar("SELECT MAX(t) FROM ships_samples", default=0.0) or 0.0)
    except Exception:
        last_sample = 0.0
    state = {"last_sample": last_sample, "sanctions_version": None}

    def receiving() -> dict:
        out = {SRC_DT: (poller.receiving_since, poller.last_ok)}
        if "aisstream" in providers:
            c = providers["aisstream"]
            out[SRC_AIS] = (c.receiving_since, c.last_message)
        if "kystverket" in providers:
            k = providers["kystverket"]
            out[SRC_KV] = (k.receiving_since, k.last_message)
        return out

    def samples_for(sid: str) -> list[int]:
        rows = m.store.query("SELECT inside FROM ships_samples WHERE strait = ? AND t >= ?",
                             (sid, time.time() - 7 * 86400))
        return [int(r["inside"] or 0) for r in rows]

    def run_analysis():
        version = (sanctions.ready(), sanctions.version())       # changes whenever any list reloads
        if version != state["sanctions_version"]:
            state["sanctions_version"] = version
            strong = svc.store.recheck_sanctions(version)
            if version[0]:
                m.log.info("ships: sanctions list loaded, %d vessels with an IMO or MMSI match", strong)
        svc.receiving = receiving()
        now = time.time()
        sample_now = now - state["last_sample"] >= 3600
        svc.run_analysis(samples_loader=samples_for if (sample_now or not svc.baselines) else None)
        if sample_now:
            state["last_sample"] = now
            rows = [(s["id"], now, s["inside_now"], s["inside_moving"]) for s in svc.straits_cache if s["coverage"]]
            if rows:
                m.store.executemany("INSERT OR REPLACE INTO ships_samples(strait, t, inside, moving) VALUES(?,?,?,?)",
                                    rows)

    m.task("snapshot", svc.refresh_snapshot, every=10, initial_delay=3)
    m.task("analysis", run_analysis, every=60, initial_delay=20)
    m.task("flush", svc.store.flush, every=60, initial_delay=40)
    m.task("prune", svc.store.prune, every=900, initial_delay=240)
    if svc.portwatch is not None:
        m.net.set_host_interval(pwlib.HOST, pwlib.HOST_INTERVAL_S)
        m.task("portwatch", svc.portwatch.step, every=pwlib.EVERY_S, initial_delay=90)
        m.task("strait-days", lambda: svc.portwatch.tally_strait_days([d["id"] for d in svc.store.straits.defs]),
               every=3600, initial_delay=300)

    # -- layers (for the layer table) -------------------------------------------------------------------
    m.layer("ships", "Vessels (AIS)", "Ships",
            description="Live ship positions from AIS: arrows for moving vessels, dots for stationary ones, "
                        "coloured by type.",
            source="Digitraffic (CC BY 4.0), aisstream.io with a key, Kystverket (NLOD) optional",
            refresh="every 30 s", use="Traffic around a coast or port; open any vessel for identity, voyage, "
                                      "sanctions checks and its 24-hour trail.",
            count=lambda: len(svc.store.snapshot.rows))
    m.layer("ships.dark", "Dark ships (estimated)", "Ships",
            description="Vessels that stopped transmitting mid-voyage while others nearby kept reporting, with a "
                        "dead-reckoning estimate and a growing uncertainty circle.",
            source="Derived from the vessel layer", refresh="every 60 s",
            use="Spotting AIS gaps worth a closer look. A heuristic: gaps have innocent causes too.",
            count=lambda: len(svc.dark))
    m.layer("ships.sanctioned", "Sanctioned vessels", "Ships",
            description="Live vessels whose IMO or MMSI is on the sanctions lists Meridian loads: the US OFAC SDN list, "
                        "the UK Sanctions List, the UN Security Council list and the EU financial sanctions list.",
            source="Sanctions lists (OFAC, UK, UN, EU) matched against AIS identities", refresh="every 60 s",
            use="Where listed vessels are now. Name-only matches are shown as possible, never as listed.",
            count=lambda: sum(1 for d in svc.sanctioned() if d["live"]))
    m.layer("ships.straits", "Straits monitor", "Ships",
            description="Fourteen chokepoints with vessels inside now and transits in the last 24 hours.",
            source="Derived from the vessel layer", refresh="every 60 s",
            use="Traffic through Gibraltar, Dover, the Bosphorus, Suez, Hormuz, Malacca and more "
                "(live counts need coverage there).",
            count=lambda: len(svc.straits_cache))
    m.layer("ships.anomalies", "Maritime anomalies", "Ships",
            description="Candidates, not accusations: possible ship-to-ship transfers in open water, vessels loitering "
                        "away from anchorages, AIS gaps near straits and areas of interest, and possible GNSS spoofing "
                        "of ships. Each card says what was seen, how confident it is and why it may be innocent.",
            source="Derived from the AIS feeds by Meridian; land shapes from Natural Earth (intel module)",
            refresh="every 60 s (loitering every 10 minutes)",
            use="A prompt to look closer at unusual vessel behaviour. Ports, anchorages, waiting areas, pilot areas "
                "and offshore installations are excluded.",
            count=lambda: sum(1 for i in svc.anomalies.items if i["published"]))

    # -- routes ------------------------------------------------------------------------------------------
    @m.route("GET", "/api/ships/live")
    def r_live(req):
        bbox = req.bbox("bbox", None)
        zoom = req.arg("zoom", 3.0, type=float, min=0.0, max=24.0)
        types = req.arg("types", None)
        classes = None
        if types and types != "all":
            classes = {t.strip() for t in types.split(",") if t.strip()}
            bad = classes - set(CLASS_IDS)
            if bad:
                raise HTTPError(400, "BAD_ARG", f"Unknown vessel type {sorted(bad)[0]!r}.")
        moving = req.arg("moving", False, type=bool)
        return svc.live(bbox, zoom, classes, moving)

    @m.route("GET", "/api/ships/vessel/{mmsi}")
    def r_vessel(req):
        return svc.vessel(_mmsi(req))

    @m.route("GET", "/api/ships/trail/{mmsi}")
    def r_trail(req):
        hours = req.arg("hours", 24.0, type=float, min=0.5, max=24.0)
        return svc.trail(_mmsi(req), hours)

    @m.route("GET", "/api/ships/straits")
    def r_straits(req):
        geometry = req.arg("geometry", True, type=bool)
        items = [dict(s, portwatch=svc.portwatch_brief(s["id"])) for s in svc.straits_cache]
        if not geometry:
            items = [{k: v for k, v in s.items() if k != "geometry"} for s in items]
        return {"straits": items, "updated_at": svc.straits_at, "has_global": has_global(),
                "note": None if has_global() else "Without an aisstream.io key only the two Baltic straits have "
                                                  "live coverage (Digitraffic)."}

    @m.route("GET", "/api/ships/straits/{sid}")
    def r_strait(req):
        sid = req.params["sid"]
        if sid not in svc.store.straits.by_id:
            raise HTTPError(404, "NOT_FOUND", "No such strait.")
        items = analysis.strait_status(svc.store, svc.store.snapshot, time.time(), svc.baselines,
                                       has_global=has_global(), sid=sid)
        out = items[0]
        try:
            out["portwatch"] = svc.portwatch_for(sid, strait=out)
        except Exception as err:          # PortWatch trouble never breaks the strait card
            m.log.warning("ships: PortWatch summary failed: %s", err)
            out["portwatch"] = None
        return out

    @m.route("GET", "/api/ships/port-activity")
    def r_port(req):
        lat, lon = req.latlon()
        radius = req.arg("radius_km", 10.0, type=float, min=0.5, max=100.0)
        limit = req.arg("limit", 20, type=int, min=1, max=100)
        return analysis.port_activity(svc.store, svc.store.snapshot, lat, lon, radius, time.time(),
                                      has_global=has_global(), limit=limit)

    @m.route("GET", "/api/ships/ports")
    def r_ports(req):
        limit = req.arg("limit", 20, type=int, min=1, max=100)
        return {"ports": svc.busiest[:limit], "source": svc.ports_source, "updated_at": svc.busiest_at,
                "note": "Counts are vessels inside each port's radius now, plus stops of 20 minutes or more that "
                        "began in the last 24 hours."}

    @m.route("GET", "/api/ships/dark")
    def r_dark(req):
        bbox = req.bbox("bbox", None)
        items = svc.dark
        if bbox:
            from meridian import geo
            items = [d for d in items if geo.in_bbox(d["last"]["lat"], d["last"]["lon"], bbox)
                     or geo.in_bbox(d["est"]["lat"], d["est"]["lon"], bbox)]
        return {"dark": items, "updated_at": svc.dark_at, "count": len(items),
                "rule": "Silent for over 2 h after at least 6 reports in 30 min while under way at 3 kn or more, "
                        "with other vessels still heard near both its last and its estimated position."}

    @m.route("GET", "/api/ships/sanctioned")
    def r_sanctioned(req):
        possible = req.arg("possible", False, type=bool)
        loaded = [x["id"] for x in sanctions.summary().get("lists", []) if x.get("loaded")] if sanctions.ready() else []
        return {"vessels": svc.sanctioned(include_possible=possible), "ready": sanctions.ready(), "lists": loaded,
                "note": f"Matched on IMO or MMSI against the {lists_text(loaded) if loaded else 'sanctions lists'}. "
                        "Name-only matches are possible matches, never listings."}

    @m.route("GET", "/api/ships/sdn/{uid}")
    def r_sdn(req):
        """One listed vessel (entity kind sanctioned_vessel, any list's id) plus the live vessel it matches."""
        uid = str(req.params["uid"])[:40]
        rec = sanctions.record(uid)
        if rec is None or rec.get("kind") not in (None, "sanctioned_vessel"):
            rec = next((v for v in sanctions.vessels() if str(v.get("uid")) == uid), None)
        if rec is None:
            raise HTTPError(404, "NOT_FOUND", "No vessel with that id on the current sanctions lists.")
        programs = [p.strip(" []") for p in re.split(r"\]\s*\[", rec.get("program") or "") if p.strip(" []")]
        live = None
        with svc.store.lock:
            cand = None
            if rec.get("mmsi") and str(rec["mmsi"]).isdigit():
                v = svc.store.vessels.get(int(rec["mmsi"]))
                if v is not None and (not v.imo or not rec.get("imo") or str(v.imo) == str(rec["imo"])):
                    cand = v
            if cand is None and rec.get("imo"):
                cand = next((v for v in svc.store.vessels.values() if v.imo and str(v.imo) == str(rec["imo"])), None)
            if cand is not None and cand.lat is not None:
                live = {"mmsi": str(cand.mmsi), "title": cand.name or f"MMSI {cand.mmsi}", "lat": round(cand.lat, 5),
                        "lon": round(cand.lon, 5), "t": cand.t}
        links = []
        if rec.get("imo"):
            links.append({"text": "VesselFinder", "href": f"https://www.vesselfinder.com/vessels/details/{rec['imo']}"})
            links.append({"text": "MarineTraffic", "href": f"https://www.marinetraffic.com/en/ais/details/ships/imo:{rec['imo']}"})
        record = {k: rec.get(k) for k in ("uid", "name", "imo", "mmsi", "call_sign", "flag", "vessel_type", "aliases",
                                          "linked_to", "built", "owner", "lists", "programmes", "listings", "listed_on")}
        record["lists"] = list(rec.get("lists") or ["OFAC"])
        record["programs"] = programs
        return {"record": record, "live": live, "links": links,
                "note": "Not heard by Meridian's AIS sources in the last 7 days." if live is None else None}

    @m.route("GET", "/api/ships/anomalies")
    def r_anomalies(req):
        bbox = req.bbox("bbox", None)
        hours = req.arg("hours", 48.0, type=float, min=1.0, max=720.0)
        include_all = req.arg("all", False, type=bool)
        try:
            kinds = svc.parse_kinds(req.arg("kinds", None) or req.arg("categories", None))
        except ValueError as err:
            raise HTTPError(400, "BAD_ARG", str(err))
        return svc.anomalies_answer(bbox=bbox, kinds=kinds, hours=hours, include_all=include_all)

    @m.route("GET", "/api/ships/anomalies/{aid}")
    def r_anomaly(req):
        aid = str(req.params["aid"])
        if not _ANOMALY_ID.match(aid):
            raise HTTPError(400, "BAD_ARG", "That is not an anomaly id.")
        item = svc.anomalies.get(aid)
        if item is None:
            raise HTTPError(404, "NOT_FOUND", "No such anomaly in the last 30 days.")
        return item

    @m.route("GET", "/api/ships/chokepoints")
    def r_chokepoints(req):
        series = req.arg("series", 0, type=int, min=0, max=90)
        if svc.portwatch is None:
            return {"chokepoints": [], "enabled": False, "note": "IMF PortWatch is turned off (SHIPS_PORTWATCH=0)."}
        items = svc.chokepoints(series_days=series)
        meta = svc.portwatch.meta()
        return {"chokepoints": items, "enabled": True, "source": pwlib.CREDIT, "link": pwlib.SITE,
                "last_ok": meta.get("last_ok_t"), "error": meta.get("last_error"),
                "note": "Daily transits from IMF PortWatch (satellite AIS), published weekly about a week behind, "
                        "beside Meridian's own terrestrial AIS counts where it monitors the strait."}

    @m.route("GET", "/api/ships/chokepoints/{pid}")
    def r_chokepoint(req):
        pid = str(req.params["pid"])
        if svc.portwatch is None:
            raise HTTPError(404, "NOT_FOUND", "IMF PortWatch is turned off.")
        if not _CHOKEPOINT_ID.match(pid):
            raise HTTPError(400, "BAD_ARG", "A PortWatch chokepoint id looks like chokepoint6.")
        sid = pwlib.STRAIT_OF.get(pid)
        if sid:
            out = svc.portwatch_for(sid)
        else:
            out = svc.portwatch.summary(pid, 90)
            if out is not None:
                out["cross_check"] = pwlib.cross_check(out, None, None, svc.portwatch.meta().get("last_error"))
                out["credit"] = pwlib.CREDIT
        if out is None or not out.get("name"):
            raise HTTPError(404, "NOT_FOUND", "No such chokepoint in IMF PortWatch's list.")
        return out

    @m.route("GET", "/api/ships/status")
    def r_status(req):
        out = svc.status()
        out["sources"] = {sid: {k: v for k, v in (m.app.sources.get(sid) or {}).items()
                                if k in ("name", "ok", "count", "error", "note", "updated_at", "needs_key", "licence")}
                          for sid in (SOURCE_DT, SOURCE_AIS, SOURCE_KV, SOURCE_PW)}
        if svc.portwatch is not None:
            out["portwatch"] = {"requests": svc.portwatch.requests} | {
                k: v for k, v in svc.portwatch.meta().items() if k in ("points_t", "stats_t", "last_ok_t", "last_error")}
        out["digitraffic"] = dict(poller.stats)
        if "aisstream" in providers:
            out["aisstream"] = dict(providers["aisstream"].stats) | {"compression": providers["aisstream"].compression}
        if "kystverket" in providers:
            out["kystverket"] = dict(providers["kystverket"].stats)
        out["classes"] = [{"id": c[0], "label": c[1], "shape": c[2]} for c in CLASSES]
        return out

    # -- discovery ---------------------------------------------------------------------------------------
    m.search(svc.search)
    m.nearby(svc.nearby)
    m.events(svc.events)

    m.ai_tool("ships_in_area", "Vessels currently reported by AIS in an area: counts by type and the most notable "
              "ships (sanctioned first, then largest). Give bbox [west, south, east, north] or lat, lon and radius_km.",
              {"type": "object", "properties": {
                  "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                  "lat": {"type": "number"}, "lon": {"type": "number"}, "radius_km": {"type": "number"},
                  "type": {"type": "string", "enum": CLASS_IDS},
                  "moving": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
               "additionalProperties": False}, svc.ai_ships_in_area)
    m.ai_tool("vessel_details", "Identity, voyage, position, sanctions matches, dark-ship status and recent port calls "
              "for one vessel by MMSI (best), IMO or name.",
              {"type": "object", "properties": {"mmsi": {"type": "string"}, "imo": {"type": "string"},
                                                "name": {"type": "string"}}, "additionalProperties": False},
              svc.ai_vessel_details)
    m.ai_tool("strait_status", "Traffic in monitored straits (Gibraltar, Dover, Bosphorus, Dardanelles, Suez, "
              "Bab-el-Mandeb, Hormuz, Malacca, Singapore, Panama, Øresund and Great Belt, Taiwan, Gulf of Finland, "
              "Åland Sea): vessels inside now, transits and unique vessels in 24 h, congestion and coverage.",
              {"type": "object", "properties": {"strait": {"type": "string"}}, "additionalProperties": False},
              svc.ai_strait_status)
    m.ai_tool("dark_ships", "Vessels that went silent on AIS mid-voyage while others nearby kept reporting, with "
              "estimated positions. Optional bbox [west, south, east, north].",
              {"type": "object", "properties": {
                  "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}},
               "additionalProperties": False}, svc.ai_dark_ships)

    m.ai_tool("ship_anomalies", "Maritime anomaly candidates (not accusations): possible ship-to-ship transfers in open "
              "water, vessels loitering away from anchorages, AIS gaps near straits and areas of interest, and possible "
              "GNSS spoofing of ships. Each gives what was seen, confidence, coverage, why it may be innocent and the "
              "vessels involved with any sanctions-list matches. Optional bbox [west, south, east, north], kinds "
              "(sts, loitering, ais_gap, gnss), hours (default 48).",
              {"type": "object", "properties": {
                  "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                  "kinds": {"type": "array", "items": {"type": "string", "enum": ["sts", "loitering", "ais_gap", "gnss"]}},
                  "hours": {"type": "number", "minimum": 1, "maximum": 168},
                  "include_unflagged": {"type": "boolean"}},
               "additionalProperties": False}, svc.ai_ship_anomalies)
    m.ai_tool("chokepoint_transits", "IMF PortWatch daily transits for 28 chokepoints (satellite AIS, about a week "
              "behind) beside Meridian's own terrestrial AIS counts, with a plain cross-check that says when either "
              "source has coverage gaps (for example the Strait of Hormuz). Optional chokepoint name and days (1 to 90).",
              {"type": "object", "properties": {"chokepoint": {"type": "string"},
                                                "days": {"type": "integer", "minimum": 1, "maximum": 90}},
               "additionalProperties": False}, svc.ai_chokepoint_transits)

    register_history(m, svc)   # 72 h track: /api/ships/history (timeline replay, CONTRACT 15.4)
    m.service = svc       # handy for debugging and tests


def _air_lookup(m):
    """Aircraft GPS interference near a point, from the core event history (flights' gps_jam cells), for the
    ship GNSS cross-check. Read-only; returns None when there is no history or nothing nearby."""
    import math

    def lookup(lat: float, lon: float, radius_km: float, since: float) -> dict | None:
        pad = radius_km / 111.0 + 0.5
        padx = min(170.0, pad / max(0.05, math.cos(math.radians(lat))))
        try:
            evs = m.app.history.events(since, kinds=["gps_jam"], limit=200,
                                       bbox=(max(-180.0, lon - padx), max(-90.0, lat - pad),
                                             min(180.0, lon + padx), min(90.0, lat + pad)))
        except Exception:
            return None
        best = None
        for ev in evs:
            if ev.get("module") != "flights":
                continue
            sev = float(ev.get("severity") or 0)
            if sev < 4:
                continue
            b = ev.get("bbox")
            if isinstance(b, (list, tuple)) and len(b) == 4:
                near = b[1] - pad <= lat <= b[3] + pad and b[0] - padx <= lon <= b[2] + padx
            else:
                near = True
            if not near:
                continue
            share = _SHARE.search(str(ev.get("title") or ""))
            item = {"level": "red" if sev >= 5 else "amber", "severity": sev, "id": ev.get("id"),
                    "share": int(share.group(1)) if share else None, "time": ev.get("time")}
            if best is None or item["severity"] > best["severity"]:
                best = item
        return best

    return lookup
