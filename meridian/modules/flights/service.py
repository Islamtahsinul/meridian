"""The flights service: tile cache and fetch scheduling, worldwide pollers (military, emergency
squawks, sanctioned aircraft, GPS hotspots, local receiver), persistence, and the answers behind
every endpoint, AI tool, search, nearby and events provider."""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from meridian import geo

from . import tiles as tiling
from .const import (CATEGORY_LABELS, CLASSES, DB_LADD, DB_PIA, EMERGENCY_SEVERITY, EMERGENCY_SQUAWKS,
                    EMERGENCY_STATUS, FIELDS, HOTSPOTS, MAX_ROWS, POSITION_SOURCES, PROVIDER_NAMES,
                    SPECIAL_SQUAWKS, pretty_name)
from .gpsjam import METHOD as GPS_METHOD
from .gpsjam import GpsJam
from .lookup import Lookups, operator_view, route_check, route_text
from .parse import HEX_RE, clean_callsign, clean_hex, clean_reg
from .providers import AdsbFi, AdsbLol, HostQueue, Job, LocalReceiver, OpenSky
from .tracker import Aircraft, Tracker, norm_reg

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights_events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    hex TEXT NOT NULL,
    first_t REAL NOT NULL,
    last_t REAL NOT NULL,
    severity INTEGER NOT NULL,
    lat REAL,
    lon REAL,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL,
    code TEXT
);
CREATE INDEX IF NOT EXISTS flights_events_t ON flights_events(last_t);
CREATE TABLE IF NOT EXISTS flights_seen (
    hex TEXT PRIMARY KEY,
    callsign TEXT,
    reg TEXT,
    type TEXT,
    descr TEXT,
    lat REAL,
    lon REAL,
    alt INTEGER,
    t REAL NOT NULL,
    source TEXT,
    cls TEXT
);
CREATE INDEX IF NOT EXISTS flights_seen_t ON flights_seen(t);
CREATE TABLE IF NOT EXISTS flights_sanctioned_hex (
    hex TEXT PRIMARY KEY,
    reg TEXT,
    uid TEXT,
    learned_at REAL NOT NULL
);
"""

FIRST_PAINT_WAIT = 2.5
# gps_jam event category from the cell's 7-day trend (activity_rules.GPS_CATEGORY, kept here so the
# service does not import the detectors)
GPS_TREND_CATEGORY = {"new": "new_interference", "worse": "new_interference", "chronic": "chronic_interference",
                      "recurring": "chronic_interference", "easing": "chronic_interference"}
SEEN_KEEP_S = 48 * 3600
EVENTS_KEEP_S = 14 * 86400
_INDIVIDUAL = re.compile(r"^[A-Z][A-Z' .-]+, [A-Z][a-z]")
SDN_LINK = "https://sanctionssearch.ofac.treas.gov/"


@dataclass
class TileState:
    tile: tiling.Tile
    fetched_at: float = 0.0
    ok: bool = False
    provider: str | None = None
    count: int = 0
    error: str | None = None
    failed_at: float = 0.0
    failed_with: str | None = None
    wanted_at: float = 0.0
    job: Job | None = None

    def fresh(self, now: float) -> bool:
        return self.ok and now - self.fetched_at < self.tile.ttl

    def pending(self) -> bool:
        return self.job is not None and not self.job.done.is_set()


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def programmes(text: str | None) -> list[str]:
    """OFAC CSV keeps programmes as 'SDGT] [NPWMD] [IFSR'."""
    if not text:
        return []
    return [p.strip(" []") for p in re.split(r"\]\s*\[", text) if p.strip(" []")]


def is_individual(name: str | None) -> bool:
    return bool(name and _INDIVIDUAL.match(name))


class FlightsService:
    def __init__(self, m, *, clock: Callable[[], float] = time.time):
        self.m = m
        self.clock = clock
        self.log = m.log
        cfg = m.config
        net = m.net
        self.max_tiles = _clamp(cfg.get_int("FLIGHTS_MAX_TILES", 6), 1, 8)
        order = [p.strip().lower().replace(".", "").replace("-", "") for p in
                 (cfg.get("FLIGHTS_PROVIDERS", "adsbfi,adsblol,opensky") or "").split(",")]
        self.order = [p for p in order if p in ("adsbfi", "adsblol", "opensky")] or ["adsbfi", "adsblol", "opensky"]
        self.gps_sampler = cfg.get_bool("FLIGHTS_GPSJAM_SAMPLER", True)
        self.gps_interval = _clamp(cfg.get_float("FLIGHTS_GPSJAM_INTERVAL", 300.0), 120.0, 3600.0)

        net.set_host_interval("opendata.adsb.fi", 1.1)
        net.set_host_interval("api.adsb.lol", 1.0)
        net.set_host_interval("opensky-network.org", 5.0)
        net.set_host_interval("api.adsbdb.com", 0.35)

        def status(sid):
            return lambda **kw: m.status(sid, **kw)

        api_key = cfg.get("ADSBLOL_API_KEY")
        lol_budget = cfg.get_int("ADSBLOL_DAILY_BUDGET", 900 if api_key else 300)
        self.adsbfi = AdsbFi(net, self.log, status("flights.adsbfi"))
        self.adsblol = AdsbLol(net, self.log, status("flights.adsblol"), store=m.store, api_key=api_key,
                               per_day=_clamp(lol_budget, 0, 400000))
        self.opensky = OpenSky(net, self.log, status("flights.opensky"), store=m.store,
                               client_id=cfg.get("OPENSKY_CLIENT_ID"), client_secret=cfg.get("OPENSKY_CLIENT_SECRET"))
        self.local = LocalReceiver(net, self.log, status("flights.local"), url=cfg.get("LOCAL_ADSB_URL"))
        self.providers = {"adsbfi": self.adsbfi, "adsblol": self.adsblol, "opensky": self.opensky}
        self.lookup_queue = HostQueue("adsbdb")
        self.lookups = Lookups(net, m.store, self.log, self.lookup_queue, status("flights.adsbdb"))
        self.tracker = Tracker(sanctions_lookup=self._sanctions_lookup, clock=clock)
        self.gps = GpsJam(m.store, clock=clock, log=self.log)
        m.store.script(SCHEMA)

        self.tiles: dict[str, TileState] = {}
        self.tiles_lock = threading.Lock()
        self.watch: dict[str, float] = {}
        self.hex_lookup_t: dict[str, float] = {}
        self.trace_t: dict[str, float] = {}
        self.mil_t = 0.0
        self.mil_src: str | None = None
        self.mil_hexes: set[str] = set()
        self.mil_job: Job | None = None
        self.alerts_t = 0.0
        self.alert_jobs: list[Job] = []
        self.sqk_hexes: dict[str, set] = {}
        self.hotspot_t: dict[str, float] = {}
        self.local_t = 0.0
        self.seen_flush_t = clock() - 300
        self.hourly_t = 0.0
        self.sanc_known: set[str] = set()
        self.sanc_known_t = 0.0
        self.sanc_poll_t = 0.0
        self.sanc_disc_t = 0.0
        self.learned: dict[str, str] = {}
        self.emg_ids: dict[str, str] = {}
        self.activity = None           # air activity detectors (activity.py), set by register
        self._load_learned()

    # =========================================================================================
    # background workers
    # =========================================================================================
    def queues(self) -> list[HostQueue]:
        return [self.adsbfi.queue, self.adsblol.queue, self.opensky.queue, self.lookup_queue]

    def _sanctions_lookup(self, hexid: str, reg: str | None) -> dict | None:
        try:
            from meridian.modules import sanctions
        except Exception:
            return None
        if not sanctions.ready():
            return False            # not loaded yet: the tracker asks again next time
        return sanctions.lookup_aircraft(hex=hexid, reg=reg)

    def _pick(self, what: str, cost: float = 1.0, *, allow: tuple[str, ...] | None = None):
        for pid in self.order:
            if allow and pid not in allow:
                continue
            p = self.providers[pid]
            if p.available(what, cost):
                return p
        return None

    def _absorb(self, obs: list[dict], source: str, fetched_at: float, *, circle=None,
                set_pred: Callable[[Aircraft], bool] | None = None, slow: bool = False, history: str = "all"):
        res = self.tracker.ingest(obs, source, slow=slow)
        self.gps.observe(obs)
        if getattr(self, "history", None) is not None:   # timeline replay samples (history.py)
            self.history.sample(self.tracker, res.hexes, notable_only=history == "notable")
        if circle is not None:
            self.tracker.mark_dropouts(circle[0], circle[1], circle[2], res.hexes, fetched_at, source=source)
            if self.activity is not None and source == "adsbfi":   # airport snapshots (activity.py), one network
                try:
                    self.activity.observe_area(circle, res.hexes, fetched_at)
                except Exception as err:                  # a detector never breaks the live feed
                    self.log.warning("air activity: airport snapshot failed: %s", err)
        if set_pred is not None:
            self.tracker.mark_set_dropouts(set_pred, res.hexes, fetched_at, source=source)
        if res.events:
            self._record_events(res.events)
        self._learn_sanctioned(res.hexes)
        return res

    # -- tiles -----------------------------------------------------------------------------------
    def _tile_state(self, tile: tiling.Tile) -> TileState:
        with self.tiles_lock:
            ts = self.tiles.get(tile.id)
            if ts is None:
                if len(self.tiles) > 800:
                    for key in sorted(self.tiles, key=lambda k: self.tiles[k].wanted_at)[:200]:
                        if not self.tiles[key].pending():
                            del self.tiles[key]
                ts = self.tiles[tile.id] = TileState(tile)
            return ts

    def schedule_tile(self, ts: TileState, priority: int = 1, now: float | None = None) -> Job | None:
        now = self.clock() if now is None else now
        if ts.pending():
            return ts.job
        cost = 1.0
        prov = self._pick("point", cost)
        if prov is None:
            ts.error = "No live ADS-B source is available right now."
            return None
        if now - ts.failed_at < 8.0 and prov.id == ts.failed_with:
            return None              # back off, unless another source can take it now
        job = prov.queue.submit(f"tile:{ts.tile.id}", lambda: self._run_tile(ts, prov), priority=priority, ttl=20)
        if job is not None:
            ts.job = job
        return job

    def _run_tile(self, ts: TileState, prov) -> int:
        t0 = self.clock()
        try:
            _, obs = prov.point(ts.tile.lat, ts.tile.lon, ts.tile.radius_nm)
        except Exception as err:
            ts.failed_at = self.clock()
            ts.failed_with = prov.id
            ts.error = str(err)[:200]
            raise
        self._absorb(obs, prov.id, t0, circle=(ts.tile.lat, ts.tile.lon, ts.tile.radius_km))
        ts.fetched_at = t0
        ts.ok = True
        ts.provider = prov.id
        ts.count = len(obs)
        ts.error = None
        return len(obs)

    def refresh_wanted_tiles(self) -> int:
        """Keep tiles of views requested in the last 25 s fresh between browser polls."""
        now = self.clock()
        with self.tiles_lock:
            wanted = [ts for ts in self.tiles.values() if now - ts.wanted_at <= 25.0]
        wanted.sort(key=lambda ts: -ts.wanted_at)
        n = 0
        for ts in wanted[: self.max_tiles * 2]:
            if not ts.fresh(now) and not ts.pending():
                if self.schedule_tile(ts, 1, now):
                    n += 1
        return n

    # -- worldwide sets ----------------------------------------------------------------------------
    def poll_military(self, *, background: bool = False) -> Job | None:
        """The worldwide military list. ``background`` (the air activity sampler) uses adsb.fi only,
        so the metered adsb.lol allowance is kept for the map."""
        if self.mil_job is not None and not self.mil_job.done.is_set():
            return self.mil_job
        prov = self._pick("mil", allow=("adsbfi",) if background else ("adsbfi", "adsblol"))
        if prov is None:
            return None
        self.mil_job = prov.queue.submit("mil", lambda: self._run_mil(prov), priority=3, ttl=60)
        return self.mil_job

    def _run_mil(self, prov) -> int:
        t0 = self.clock()
        _, obs = prov.mil()
        previous = set(self.mil_hexes)
        res = self._absorb(obs, prov.id, t0, set_pred=lambda ac: ac.hex in previous, slow=True)
        self.mil_hexes = res.hexes
        self.mil_t = t0
        self.mil_src = prov.id
        # Military surge snapshots (activity.py) from adsb.fi only: another network's list has other feeders,
        # so mixing them would move the baseline.
        if self.activity is not None and prov.id == "adsbfi":
            try:
                self.activity.observe_mil(res.hexes, t0)
            except Exception as err:
                self.log.warning("air activity: military snapshot failed: %s", err)
        return len(obs)

    def poll_alerts(self, *, force: bool = False) -> list[Job]:
        now = self.clock()
        active = self.m.active()
        interval = 60.0 if active else 180.0
        if not force and now - self.alerts_t < interval - 1:
            return []
        if any(not j.done.is_set() for j in self.alert_jobs):
            return self.alert_jobs
        prov = self._pick("sqk", allow=("adsbfi", "adsblol") if active else ("adsbfi",))
        if prov is None:
            return []
        self.alerts_t = now
        jobs = []
        for code in ("7700", "7600", "7500"):
            job = prov.queue.submit(f"sqk:{code}", lambda c=code: self._run_sqk(prov, c), priority=2, ttl=60)
            if job is not None:
                jobs.append(job)
        self.alert_jobs = jobs
        return jobs

    def _run_sqk(self, prov, code: str) -> int:
        t0 = self.clock()
        _, obs = prov.sqk(code)
        previous = set(self.sqk_hexes.get(code, ()))
        res = self._absorb(obs, prov.id, t0, set_pred=lambda ac: ac.hex in previous, slow=True)
        self.sqk_hexes[code] = res.hexes
        return len(obs)

    # -- GPS hotspots ------------------------------------------------------------------------------
    def sample_hotspot(self) -> Job | None:
        if not self.gps_sampler:
            return None
        now = self.clock()
        interval = self.gps_interval if self.m.active() else max(600.0, self.gps_interval)
        due = [h for h in HOTSPOTS if now - self.hotspot_t.get(h[0], 0.0) >= interval]
        if not due or not self.adsbfi.available("point") or self.adsbfi.queue.size() > 6:
            return None
        spot = min(due, key=lambda h: self.hotspot_t.get(h[0], 0.0))
        self.hotspot_t[spot[0]] = now
        return self.adsbfi.queue.submit(f"hotspot:{spot[0]}", lambda: self._run_hotspot(spot), priority=5, ttl=120)

    def _run_hotspot(self, spot) -> int:
        sid, name, lat, lon, radius_nm = spot
        t0 = self.clock()
        _, obs = self.adsbfi.point(lat, lon, radius_nm)
        self._absorb(obs, "adsbfi", t0, circle=(lat, lon, radius_nm * 1.852), history="notable")
        if self.activity is not None:                     # all-traffic counts: receiver-coverage check
            self.activity.observe_traffic(sid, t0, len(obs))
        summary = self.gps.summary(t0)
        self.m.status("flights.gpsjam", ok=True, count=summary["cells"],
                      note=f"{summary['by_level']['red']} red and {summary['by_level']['amber']} amber cells; "
                           f"last sample {name}")
        return len(obs)

    # -- watched aircraft (open cards) -------------------------------------------------------------
    def poll_watch(self) -> Job | None:
        now = self.clock()
        for h in [h for h, t in self.watch.items() if now - t > 600]:
            self.watch.pop(h, None)
        stale = []
        for h, t in list(self.watch.items()):
            if now - t > 40:
                continue
            ac = self.tracker.get(h)
            if ac is None or now - ac.pos_t > 15:
                stale.append(h)
        if not stale or not self.adsbfi.available("hexes"):
            return None
        chunk = stale[: AdsbFi.MAX_HEXES]
        wanted = set(chunk)
        return self.adsbfi.queue.submit("watch:" + ",".join(sorted(chunk)), lambda: self._run_hexes(chunk, wanted),
                                        priority=0, ttl=15)

    def _run_hexes(self, hexes: list[str], members: set | None = None) -> int:
        t0 = self.clock()
        _, obs = self.adsbfi.hexes(hexes)
        members = members if members is not None else set(hexes)
        self._absorb(obs, "adsbfi", t0, set_pred=lambda ac: ac.hex in members)
        return len(obs)

    # -- sanctioned aircraft -----------------------------------------------------------------------
    def _load_learned(self) -> None:
        try:
            rows = self.m.store.query("SELECT hex, reg FROM flights_sanctioned_hex")
            self.learned = {r["hex"]: r["reg"] or "" for r in rows}
        except Exception:
            self.learned = {}

    def _learn_sanctioned(self, hexes: set) -> None:
        for h in hexes:
            if h in self.learned:
                continue
            ac = self.tracker.get(h)
            if ac is None or not ac.sanc or ac.sanc.get("match") != "registration":
                continue
            self.learned[h] = ac.reg or ""
            try:
                self.m.store.execute("INSERT OR IGNORE INTO flights_sanctioned_hex(hex, reg, uid, learned_at) "
                                     "VALUES(?,?,?,?)", (h, ac.reg or "", str(ac.sanc.get("uid") or ""), self.clock()))
            except Exception as err:
                self.log.debug("could not store a sanctioned hex: %s", err)

    def sanctions_watch(self) -> list[Job]:
        """Poll known sanctioned airframes worldwide every 3 min and, once a minute, look one listed
        registration up to learn its transponder code (only while the map is in use)."""
        try:
            from meridian.modules import sanctions
        except Exception:
            return []
        if not sanctions.ready() or not self.adsbfi.available("hexes"):
            return []
        now = self.clock()
        jobs = []
        records = sanctions.aircraft()
        if now - self.sanc_known_t > 600:
            known = {str(a.get("mode_s")).lower() for a in records if a.get("mode_s")}
            self.sanc_known = {h for h in known | set(self.learned) if HEX_RE.match(h)}
            self.sanc_known_t = now
            self.tracker.refresh_sanctions()
        if self.sanc_known and now - self.sanc_poll_t >= 180:
            self.sanc_poll_t = now
            known = sorted(self.sanc_known)
            for i in range(0, len(known), AdsbFi.MAX_HEXES):
                chunk = known[i:i + AdsbFi.MAX_HEXES]
                job = self.adsbfi.queue.submit(f"sanc:{i}", lambda c=chunk: self._run_hexes(c), priority=4, ttl=120)
                if job:
                    jobs.append(job)
        if now - self.sanc_disc_t >= 60:
            self.sanc_disc_t = now
            learned_regs = {norm_reg(r) for r in self.learned.values()}
            regs = sorted({r for r in (clean_reg(a.get("registration")) for a in records)
                           if r and 3 <= len(r) <= 10 and norm_reg(r) not in learned_regs})
            if regs:
                cursor = int(self.m.store.kv_get("flights.sanctions.cursor", 0) or 0) % len(regs)
                reg = regs[cursor]
                self.m.store.kv_set("flights.sanctions.cursor", cursor + 1)
                job = self.adsbfi.queue.submit(f"sreg:{reg}", lambda r=reg: self._run_reg(r), priority=6, ttl=120)
                if job:
                    jobs.append(job)
        return jobs

    def _run_reg(self, reg: str) -> int:
        t0 = self.clock()
        _, obs = self.adsbfi.reg(reg)
        self._absorb(obs, "adsbfi", t0)
        return len(obs)

    # -- local receiver ----------------------------------------------------------------------------
    def poll_local(self) -> int:
        if not self.local.enabled():
            return 0
        now = self.clock()
        interval = 5.0 if self.m.active() else 30.0
        if now - self.local_t < interval - 0.5:
            return 0
        self.local_t = now
        try:
            _, obs = self.local.fetch()
        except Exception:
            return 0
        self._absorb(obs, "local", now)
        return len(obs)

    # -- housekeeping ------------------------------------------------------------------------------
    def housekeeping(self) -> None:
        now = self.clock()
        self.tracker.prune(now, keep=set(self.watch))
        with self.tiles_lock:
            for key in [k for k, ts in self.tiles.items()
                        if now - ts.wanted_at > 900 and now - ts.fetched_at > 900 and not ts.pending()]:
                del self.tiles[key]
        for d in (self.hex_lookup_t, self.trace_t):
            for k in [k for k, t in d.items() if now - t > 3600]:
                d.pop(k, None)
        if now - self.seen_flush_t >= 120:
            rows = self.tracker.snapshot_seen(self.seen_flush_t - 1)
            self.seen_flush_t = now
            if rows:
                self.m.store.executemany(
                    "INSERT INTO flights_seen(hex, callsign, reg, type, descr, lat, lon, alt, t, source, cls) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(hex) DO UPDATE SET callsign = excluded.callsign, "
                    "reg = excluded.reg, type = excluded.type, descr = excluded.descr, lat = excluded.lat, "
                    "lon = excluded.lon, alt = excluded.alt, t = excluded.t, source = excluded.source, cls = excluded.cls",
                    rows)
        if now - self.hourly_t >= 3600:
            self.hourly_t = now
            self.m.store.execute("DELETE FROM flights_seen WHERE t < ?", (now - SEEN_KEEP_S,))
            self.m.store.execute("DELETE FROM flights_events WHERE last_t < ?", (now - EVENTS_KEEP_S,))
            self.lookups.prune()
            self.gps.prune(now)
        self.adsblol.budget.save()
        self.opensky.budget.save()
        self._publish_counts(now)

    def _publish_counts(self, now: float) -> None:
        """Sources table: live aircraft currently shown from each source."""
        counts: dict[str, int] = {}
        for ac, *_ in self.tracker.displayed(now):
            counts[ac.pos_src] = counts.get(ac.pos_src, 0) + 1
        for pid, p in (*self.providers.items(), ("local", self.local)):
            h = p.health
            if h.last_ok is None and h.last_error is None:
                continue
            info = h.info(now)
            self.m.status(f"flights.{pid}", ok=info["ok"], count=counts.get(pid, 0),
                          error=None if info["ok"] else info["last_error"])

    def flush_gps(self) -> None:
        self.gps.flush()

    # =========================================================================================
    # events
    # =========================================================================================
    def _record_events(self, events: list) -> None:
        for kind, hexid, t, _new in events:
            ac = self.tracker.get(hexid)
            if ac is None or ac.lat is None:
                continue
            what = ", ".join(x for x in (pretty_name(ac.desc) or ac.typ, ac.reg,
                                          "on the ground" if ac.gnd else (f"{ac.alt:,} ft" if ac.alt is not None else None)) if x)
            if kind == "emergency":
                code = ac.emg_code or ac.emergency_code() or ""
                shown = emergency_label(code)
                sev = EMERGENCY_SEVERITY.get(code, 5) - (1 if ac.gnd else 0)
                eid = self._episode_id(hexid, ac, t)
                title = f"{ac.title()}: {shown}"
            else:
                code = None
                sev = 5
                eid = f"sanc:{hexid}:{int(t // 21600)}"
                prog = ", ".join(programmes((ac.sanc or {}).get("program"))) or "OFAC"
                title = f"{ac.title()}: OFAC-listed aircraft seen ({prog})"
            try:
                self.m.store.execute(
                    "INSERT INTO flights_events(id, kind, hex, first_t, last_t, severity, lat, lon, title, subtitle, code) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_t = excluded.last_t, "
                    "lat = excluded.lat, lon = excluded.lon, subtitle = excluded.subtitle, "
                    "severity = MAX(severity, excluded.severity)",
                    (eid, kind, hexid, t, t, sev, round(ac.lat, 5), round(ac.lon, 5), title, what or "Aircraft", code))
            except Exception as err:
                self.log.warning("could not store a flights event: %s", err)

    def _episode_id(self, hexid: str, ac: Aircraft, t: float) -> str:
        """One event per emergency episode, also across restarts: reuse the stored event of the same
        aircraft seen in the last 30 minutes."""
        eid = self.emg_ids.get(hexid)
        if eid is None:
            row = self.m.store.one("SELECT id FROM flights_events WHERE kind = 'emergency' AND hex = ? AND last_t >= ? "
                                   "ORDER BY last_t DESC LIMIT 1", (hexid, t - 1800))
            eid = row["id"] if row else f"emg:{hexid}:{int(ac.emg_since or t)}"
            self.emg_ids[hexid] = eid
            if len(self.emg_ids) > 2000:
                self.emg_ids.pop(next(iter(self.emg_ids)))
        return eid

    def events(self, since: float) -> list[dict]:
        rows = self.m.store.query("SELECT * FROM flights_events WHERE last_t >= ? ORDER BY last_t DESC LIMIT 500",
                                  (since,))
        out = []
        for r in rows:
            out.append({"kind": "aircraft", "id": r["hex"], "title": r["title"], "subtitle": r["subtitle"],
                        "lat": r["lat"], "lon": r["lon"], "time": r["last_t"], "first_time": r["first_t"],
                        "severity": int(r["severity"]), "event": r["kind"],
                        "layer": "flights.alerts" if r["kind"] == "emergency" else "flights"})
        now = self.clock()
        hours = int(_clamp(math.ceil((now - since) / 3600.0), 1, 48))
        cells = [c for c in self.gps.cells(hours=hours, now=now) if c["level"] in ("amber", "red")
                 and c["last_bad_t"] and c["last_bad_t"] >= since]
        cells.sort(key=lambda c: -c["share"])
        for c in cells[:200]:
            out.append(self.gps_entity(c, hours))
        return out

    @staticmethod
    def gps_severity(cell: dict) -> int:
        if cell["level"] == "red":
            return 6 if cell["share"] >= 0.3 else 5
        return 4

    def gps_entity(self, c: dict, hours: int) -> dict:
        clat = c["lat0"] + c["size"] / 2
        clon = c["lon0"] + c["size"] / 2
        pct = round(c["share"] * 100)
        ent = {"kind": "gps_jam", "id": f"gpsjam:{c['lat0']:g}:{c['lon0']:g}:{c['size']:g}",
               "title": f"GPS interference, {pct}% of aircraft affected",
               "subtitle": f"Last {hours} h, {c['total']} aircraft-hours, derived from reported navigation accuracy",
               "lat": round(clat, 5), "lon": round(clon, 5), "time": c["last_bad_t"],
               "severity": self.gps_severity(c), "layer": "flights.gpsjam",
               "bbox": [c["lon0"], c["lat0"], c["lon0"] + c["size"], c["lat0"] + c["size"]]}
        # The cell's trend against its own last 7 days (activity.py): consumers such as convergences can
        # skip "chronic_interference" cells, which repeat every day. Severity and ids are unchanged.
        trend = self.activity.gps_trend_of(c["row"], c["col"]) if self.activity is not None else None
        if trend is not None:
            ent["trend"] = trend["label"]
            ent["days_affected_7d"] = trend.get("days_affected_any", trend["days_affected"])   # at any hour
            category = GPS_TREND_CATEGORY.get(trend["label"])
            if category:
                ent["category"] = category
        return ent

    # =========================================================================================
    # answers
    # =========================================================================================
    def sources_info(self, now: float) -> dict:
        out = {pid: p.info(now) for pid, p in self.providers.items()}
        out["local"] = self.local.info(now)
        return out

    def _all_down(self, now: float) -> bool:
        return not any(p.available("point") for p in self.providers.values())

    def live(self, bbox: tuple, zoom: float) -> dict:
        now = self.clock()
        self.m.touch("flights.live")
        plan = tiling.plan(bbox, zoom, max_tiles=self.max_tiles)
        clat, clon = tiling.bbox_centre(bbox)
        jobs = []
        states = []
        for t in sorted(plan.tiles, key=lambda t: geo.haversine_km(clat, clon, t.lat, t.lon)):   # centre first
            ts = self._tile_state(t)
            ts.wanted_at = now
            states.append(ts)
            if not ts.fresh(now):
                job = self.schedule_tile(ts, 1, now)
                if job is not None:
                    jobs.append(job)
        if plan.mode == "wide":
            # Zoomed out, the worldwide sets carry the picture: keep military fresh as well.
            self.m.touch("flights.mil")
            if now - self.mil_t > 75:
                job = self.poll_military()
                if job is not None and self.mil_t == 0:
                    jobs.append(job)
        recently = any(now - ts.fetched_at < 90 for ts in states)
        if jobs and not recently:
            deadline = time.monotonic() + FIRST_PAINT_WAIT
            for job in jobs:
                job.wait(deadline - time.monotonic())
        now = self.clock()
        items = self.tracker.displayed(now, bbox=_pad_bbox(bbox, 0.05))
        rows, truncated = self.tracker.rows(items, now, MAX_ROWS)
        fresh = sum(1 for ts in states if ts.fresh(now))
        pending = sum(1 for ts in states if ts.pending())
        errors = [ts.error for ts in states if ts.error and not ts.fresh(now)]
        est = sum(1 for it in items if it[3])
        note = None
        if plan.mode == "wide":
            note = ("Zoomed out: showing military aircraft, emergencies and traffic already loaded"
                    + (" plus live traffic near the centre" if plan.tiles else "") + ". Zoom in to see all traffic.")
        error = None
        if plan.tiles and fresh == 0 and pending == 0 and (errors or self._all_down(now)):
            error = "Live ADS-B sources are not answering right now (adsb.fi, adsb.lol, OpenSky). Retrying shortly."
        used = sorted({ts.provider for ts in states if ts.provider and ts.fresh(now)})
        return {"now": round(now, 1), "mode": plan.mode, "level": plan.level, "zoom": round(zoom, 2),
                "tiles": {"wanted": len(plan.tiles), "fresh": fresh, "pending": pending, "needed": plan.needed},
                "fields": FIELDS, "ac": rows, "truncated": truncated,
                "counts": {"shown": len(rows), "estimated": est, "tracked": len(self.tracker)},
                "providers": [PROVIDER_NAMES.get(p, p) for p in used], "note": note, "error": error}

    def military(self) -> dict:
        now = self.clock()
        self.m.touch("flights.mil")
        if now - self.mil_t > 75:          # stale (first use, or the layer was off for a while)
            job = self.poll_military()
            if job is not None:
                job.wait(4.0)
        now = self.clock()
        items = self.tracker.displayed(now, where=lambda ac: ac.military)
        rows, truncated = self.tracker.rows(items, now, MAX_ROWS)
        error = None
        if self.mil_t == 0 and not (self.mil_job and not self.mil_job.done.is_set()):
            err = self.mil_job.error if self.mil_job is not None else None
            error = f"The military aircraft list is unavailable ({err})." if err else None
        return {"now": round(now, 1), "fields": FIELDS, "ac": rows, "truncated": truncated, "count": len(rows),
                "age": round(now - self.mil_t, 1) if self.mil_t else None,
                "source": PROVIDER_NAMES.get(self.mil_src or "", None), "error": error}

    def alerts(self) -> dict:
        now = self.clock()
        self.m.touch("flights.alerts")
        if now - self.alerts_t > 75:
            jobs = self.poll_alerts(force=True)
            if jobs:
                deadline = time.monotonic() + 3.5
                for j in jobs:
                    j.wait(deadline - time.monotonic())
        now = self.clock()
        items = self.tracker.displayed(now, where=lambda ac: ac.emergency_code() is not None)
        rows, _ = self.tracker.rows(items, now, 500)
        details = []
        for ac, lat, lon, est in items:
            code = ac.emergency_code()
            details.append({"hex": ac.hex, "code": code, "label": emergency_label(code), "since": ac.emg_since})
        recent = self.m.store.query("SELECT * FROM flights_events WHERE kind = 'emergency' AND last_t >= ? "
                                    "ORDER BY last_t DESC LIMIT 40", (now - 12 * 3600,))
        return {"now": round(now, 1), "fields": FIELDS, "ac": rows, "count": len(rows), "details": details,
                "recent": [{"hex": r["hex"], "title": r["title"], "subtitle": r["subtitle"], "lat": r["lat"],
                            "lon": r["lon"], "first_t": r["first_t"], "last_t": r["last_t"], "code": r["code"],
                            "severity": r["severity"]} for r in recent],
                "age": round(now - self.alerts_t, 1) if self.alerts_t else None}

    # -- one aircraft --------------------------------------------------------------------------------
    def aircraft(self, hexid: str, *, wait: float = 3.0) -> dict:
        now = self.clock()
        self.watch[hexid] = now
        ac = self.tracker.get(hexid)
        job = None
        if (ac is None or now - ac.pos_t > 20) and self.adsbfi.available("hexes") \
                and now - self.hex_lookup_t.get(hexid, 0.0) > 15:
            self.hex_lookup_t[hexid] = now
            job = self.adsbfi.queue.submit(f"hex:{hexid}", lambda: self._run_hexes([hexid]), priority=0, ttl=15)
        cs = ac.cs if ac else None
        info_status, info = self.lookups.aircraft(hexid)
        route_status, route = self.lookups.route(cs) if cs else ("none", None)
        deadline = time.monotonic() + wait
        if job is not None:
            job.wait(deadline - time.monotonic())
        ac = self.tracker.get(hexid)
        if ac is not None and ac.cs and ac.cs != cs:
            route_status, route = self.lookups.route(ac.cs)
        if info_status == "pending":
            info_status, info = self.lookups.aircraft(hexid, wait=max(0.0, deadline - time.monotonic()))
        if route_status == "pending" and ac is not None and ac.cs:
            route_status, route = self.lookups.route(ac.cs, wait=max(0.0, deadline - time.monotonic()))
        return self.details(hexid, ac, info_status, info, route_status, route, self.clock())

    def details(self, hexid, ac: Aircraft | None, info_status, info, route_status, route, now) -> dict:
        links = [{"text": "Open on adsb.lol", "href": f"https://adsb.lol/?icao={hexid}"},
                 {"text": "Open on adsb.fi", "href": f"https://globe.adsb.fi/?icao={hexid}"}]
        base: dict[str, Any] = {"hex": hexid, "links": links, "attribution": None, "now": round(now, 1)}
        last = None
        if ac is None or ac.lat is None:
            row = self.m.store.one("SELECT * FROM flights_seen WHERE hex = ?", (hexid,))
            if row:
                last = {"t": row["t"], "lat": row["lat"], "lon": row["lon"], "alt_ft": row["alt"],
                        "callsign": row["callsign"] or None, "registration": row["reg"] or None,
                        "type": row["type"] or None, "description": row["descr"] or None,
                        "source": PROVIDER_NAMES.get(row["source"] or "", row["source"])}
        info = info or {}
        db_flags = ac.db_flags if ac else 0
        if ac is None:
            reg = info.get("registration") or (last or {}).get("registration")
            typ = info.get("icao_type") or (last or {}).get("type")
            title = (last or {}).get("callsign") or reg or hexid.upper()
            desc = info.get("type") or (last or {}).get("description")
            op_text, op_kind = operator_view(owner=info.get("owner"), flag_code=info.get("operator_flag"),
                                             db_flags=db_flags)
            base.update({"live": False, "estimated": False, "known": bool(last or info), "title": title,
                         "subtitle": ", ".join(x for x in (desc or typ, reg) if x) or "Aircraft",
                         "registration": reg, "type": typ, "description": desc,
                         "operator": {"text": op_text, "kind": op_kind} if op_text else None,
                         "manufacturer": info.get("manufacturer"), "owner_country": info.get("owner_country"),
                         "info_status": info_status, "route": None, "route_status": "none",
                         "last_seen": last, "class": "unknown", "class_label": CLASSES["unknown"]})
            if last:
                base["lat"], base["lon"] = last["lat"], last["lon"]
            return base

        disp = ac.display(now)
        live = disp is not None and not disp[2]
        est = disp is not None and disp[2]
        lat, lon = (disp[0], disp[1]) if disp else (ac.lat, ac.lon)
        code = ac.emergency_code()
        route_airline = ((route or {}).get("airline") or {}).get("name") if route else None
        owner = info.get("owner") or ac.ownop
        op_text, op_kind = operator_view(owner=owner, flag_code=info.get("operator_flag"), db_flags=db_flags,
                                         route_airline=route_airline, military=ac.military)
        desc = ac.desc or info.get("type")
        reg = ac.reg or info.get("registration")
        typ = ac.typ or info.get("icao_type")
        sq_label = None
        if ac.sq:
            sq_label = EMERGENCY_SQUAWKS.get(ac.sq) or SPECIAL_SQUAWKS.get(ac.sq)
        privacy = "LADD" if db_flags & DB_LADD else ("PIA" if db_flags & DB_PIA else None)
        if not live and not est and last is None and ac.lat is not None:
            last = {"t": ac.pos_t, "lat": round(ac.lat, 5), "lon": round(ac.lon, 5), "alt_ft": ac.alt,
                    "callsign": ac.cs, "registration": reg, "type": typ, "description": desc,
                    "source": PROVIDER_NAMES.get(ac.pos_src or "", ac.pos_src)}
        gps_ok = None
        if ac.nac_p is not None or ac.gps_ok_before:
            gps_ok = not (ac.gps_ok_before or (ac.nac_p is not None and ac.nac_p < 8))
        base.update({
            "live": live, "estimated": est, "known": True,
            "title": ac.title(), "subtitle": ", ".join(x for x in (pretty_name(desc) or typ, reg) if x) or "Aircraft",
            "class": ac.cls, "class_label": CLASSES.get(ac.cls, "Unknown"), "shape": ac.shp, "size": ac.sz,
            "callsign": ac.cs, "registration": reg, "type": typ, "description": desc,
            "manufacturer": info.get("manufacturer"), "type_name": info.get("type"), "year": ac.year,
            "owner_country": info.get("owner_country"),
            "operator": {"text": op_text, "kind": op_kind} if op_text else None,
            "lat": None if lat is None else round(lat, 5), "lon": None if lon is None else round(lon, 5),
            "alt_ft": ac.alt, "alt_geom_ft": ac.alt_geom,
            "on_ground": ac.gnd, "gs_kt": None if ac.gs is None else round(ac.gs),
            "track": None if ac.trk is None else round(ac.trk), "vr_fpm": ac.vr,
            "squawk": ac.sq, "squawk_label": sq_label,
            "emergency": {"code": code, "label": emergency_label(code), "since": ac.emg_since,
                          "squawk": code in EMERGENCY_SQUAWKS} if code else None,
            "priority": "Medical priority flight" if ac.emg == "lifeguard" else None,
            "category": ac.cat, "category_label": CATEGORY_LABELS.get(ac.cat or ""),
            "position_source": POSITION_SOURCES.get(ac.ptype or "", ac.ptype),
            "provider": PROVIDER_NAMES.get(ac.pos_src or "", ac.pos_src),
            "position_age_s": round(now - ac.pos_t, 1), "pos_t": ac.pos_t, "seen_t": ac.seen_t,
            "nac_p": ac.nac_p, "nic": ac.nic, "adsb_version": ac.version, "gps_ok": gps_ok,
            "military": ac.military, "privacy": privacy,
            "route": route, "route_status": route_status, "route_text": route_text(route),
            "route_check": route_check(route, lat, lon) if route and not ac.gnd else None,
            "info_status": info_status,
            "sanctions": self.sanctions_view(ac.sanc) if ac.sanc else None,
            "estimate": {"from_lat": round(ac.lat, 5), "from_lon": round(ac.lon, 5), "age_s": round(now - ac.pos_t),
                         "track": None if ac.trk is None else round(ac.trk),
                         "gs_kt": None if ac.gs is None else round(ac.gs)} if est else None,
            "last_seen": last if not live and not est else None,
            "trail_points": len(ac.trail) // 4,
            "sources_seen": sorted(PROVIDER_NAMES.get(s, s) for s, t in ac.sources.items() if now - t < 3600),
            "attribution": PROVIDER_NAMES.get(ac.pos_src or "", ac.pos_src),
        })
        return base

    @staticmethod
    def sanctions_view(rec: dict | None) -> dict | None:
        """The SDN record without names of private individuals (golden rule 2)."""
        if not rec:
            return None
        linked = [x for x in rec.get("linked_to") or [] if x]
        companies = [x for x in linked if not is_individual(x)]
        people = len(linked) - len(companies)
        operator = rec.get("operator")
        if is_individual(operator):
            operator = None
            people += 1
        match = rec.get("match")
        return {"uid": rec.get("uid"), "programs": programmes(rec.get("program")),
                "program": ", ".join(programmes(rec.get("program"))), "registration": rec.get("registration"),
                "model": rec.get("model"), "operator": operator, "linked_to": companies[:6],
                "linked_individuals": people, "msn": rec.get("msn"), "mode_s": rec.get("mode_s"),
                "built": rec.get("built"),
                "match": {"mode_s": "Mode S transponder code", "registration": "registration",
                          "msn": "serial number"}.get(match, match), "link": SDN_LINK}

    def sanctioned(self, uid: str) -> dict | None:
        try:
            from meridian.modules import sanctions
        except Exception:
            return None
        rec = next((a for a in sanctions.aircraft() if str(a.get("uid")) == uid), None)
        if rec is None:
            return None
        now = self.clock()
        live = None
        with self.tracker.lock:
            for ac in self.tracker.aircraft.values():
                if ac.sanc and str(ac.sanc.get("uid")) == uid and ac.display(now):
                    live = ac.entity(now)
                    break
        hexes = [h for h, r in self.learned.items() if norm_reg(r) == norm_reg(rec.get("registration"))]
        if rec.get("mode_s"):
            hexes.append(str(rec["mode_s"]).lower())
        last = None
        for h in hexes:
            row = self.m.store.one("SELECT * FROM flights_seen WHERE hex = ?", (h,))
            if row and (last is None or row["t"] > last["t"]):
                last = {"hex": h, "t": row["t"], "lat": row["lat"], "lon": row["lon"], "alt_ft": row["alt"],
                        "callsign": row["callsign"] or None}
        view = self.sanctions_view(dict(rec, match=None))
        view["name"] = rec.get("name")
        return {"record": view, "live": live, "last_seen": last, "hexes": sorted(set(hexes)),
                "ready": sanctions.ready()}

    def trail(self, hexid: str) -> dict:
        now = self.clock()
        ac = self.tracker.get(hexid)
        if ac is None:
            return {"hex": hexid, "points": [], "estimated": None, "note": "No recent positions for this aircraft."}
        pts = ac.trail_points()
        span = pts[-1][0] - pts[0][0] if len(pts) > 1 else 0.0
        note = None
        if (len(pts) < 3 or span < 300) and not ac.gnd and now - self.trace_t.get(hexid, 0.0) > 600 \
                and self.adsblol.available("trace"):
            self.trace_t[hexid] = now
            job = self.adsblol.queue.submit(f"trace:{hexid}", lambda: self._run_trace(hexid), priority=1, ttl=10)
            if job is not None:
                job.wait(4.0)
                if job.ok and job.result:
                    note = "Earlier part of the trail from adsb.lol."
            pts = ac.trail_points()
        disp = ac.display(now)
        est = [round(disp[1], 5), round(disp[0], 5)] if disp and disp[2] else None
        return {"hex": hexid, "points": [[round(p[2], 5), round(p[1], 5), p[3], int(p[0])] for p in pts],
                "estimated": est, "from": int(pts[0][0]) if pts else None, "to": int(pts[-1][0]) if pts else None,
                "note": note}

    def _run_trace(self, hexid: str) -> int:
        points = self.adsblol.trace(hexid)
        ac = self.tracker.get(hexid)
        if ac is None or not points:
            return 0
        with self.tracker.lock:
            return ac.merge_trail(points, self.clock())

    # -- airport traffic ---------------------------------------------------------------------------
    def airport(self, ident: str, lat: float, lon: float, radius_km: float = 40.0) -> dict:
        now = self.clock()
        self.m.touch("flights.live")
        covered = False
        with self.tiles_lock:
            for ts in self.tiles.values():
                if now - ts.fetched_at < 45 and ts.ok and tiling.circle_covers(ts.tile, lat, lon, radius_km):
                    covered = True
                    break
        waited = False
        if not covered:
            tile = tiling.adhoc_tile(lat, lon, int(radius_km / 1.852) + 6)
            ts = self._tile_state(tile)
            ts.wanted_at = now
            if not ts.fresh(now):
                job = self.schedule_tile(ts, 0, now)
                if job is not None:
                    job.wait(3.0)
                    waited = True
            covered = ts.fresh(self.clock())
        now = self.clock()
        near = self.tracker.near(lat, lon, radius_km, now, live_only=True)
        ident_u = ident.upper()
        arrivals, departures = [], []
        overhead = on_ground = 0
        prefetch = []
        for d, ac, la, lo, _est in near:
            rstatus, route = self.lookups.cached_route(ac.cs)
            if rstatus == "missing" and ac.cs:
                prefetch.append(ac.cs)
            phase = flight_phase(ac, ident_u, route, d, lat, lon)
            if phase == "ground":
                on_ground += 1
                continue
            if phase == "overhead":
                overhead += 1
                continue
            item = ac.entity(now)
            item.update({"alt_ft": ac.alt, "route": route_text(route), "distance_km": round(d, 1),
                         "vr_fpm": ac.vr, "gs_kt": None if ac.gs is None else round(ac.gs), "phase": phase})
            (arrivals if phase == "arrival" else departures).append(item)
        for cs in prefetch[:12]:
            self.lookups.route(cs, priority=5)
        if covered or near:
            note = (f"Live aircraft within {radius_km:g} km. Descending below 12,000 ft counts as arriving and "
                    "climbing as departing. Routes from adsbdb are unverified.")
        else:
            note = "No live ADS-B coverage of this airport right now."
        return {"ident": ident_u, "lat": lat, "lon": lon, "radius_km": radius_km, "time": round(now, 1),
                "arrivals": arrivals[:20], "departures": departures[:20], "overhead": overhead,
                "on_ground": on_ground, "covered": bool(covered or near), "waited": waited, "note": note}

    # -- GPS interference --------------------------------------------------------------------------
    def gpsjam(self, bbox, zoom: float, hours: int) -> dict:
        now = self.clock()
        self.m.touch("flights.gpsjam")
        agg = 2 if zoom < 4.5 else 1
        cells = self.gps.cells(hours=hours, bbox=bbox, agg=agg, now=now)
        lv = {"green": 1, "amber": 2, "red": 3}
        rows = [[c["lat0"], c["lon0"], c["size"], c["total"], c["bad"], lv[c["level"]], c["last_bad_t"]] for c in cells]
        counts = {"green": 0, "amber": 0, "red": 0}
        for c in cells:
            counts[c["level"]] += 1
        return {"now": round(now, 1), "hours": hours, "agg": agg,
                "fields": ["lat0", "lon0", "size", "total", "bad", "level", "last_bad_t"], "cells": rows,
                "counts": counts, "method": GPS_METHOD, "sampler": {"on": self.gps_sampler,
                                                                     "hotspots": len(HOTSPOTS),
                                                                     "interval_s": self.gps_interval}}

    def gpsjam_cell(self, lat: float, lon: float, hours: int, agg: int = 1) -> dict:
        now = self.clock()
        out = self.gps.cell_detail(lat, lon, hours=hours, agg=agg, now=now)
        if self.activity is not None:                     # 7-day trend for the card (activity.py)
            try:
                from .gpsjam import cell_of
                row, col = cell_of(lat, lon)
                out["trend"] = self.activity.cell_trend(row, col, agg, now)
            except Exception as err:
                self.log.warning("GPS cell trend failed: %s", err)
        return out

    # -- AI and search helpers ---------------------------------------------------------------------
    def ensure_area(self, lat: float, lon: float, radius_km: float, wait: float = 6.0) -> str:
        """Fetch tiles covering a circle (for AI tools), waiting up to ``wait`` seconds."""
        now = self.clock()
        bbox = geo.bbox_of_circle(lat, lon, radius_km)
        plan = tiling.plan(bbox, 8.0, max_tiles=self.max_tiles)
        if plan.mode == "wide" and not plan.tiles:
            return "partial"
        jobs = []
        for t in plan.tiles:
            ts = self._tile_state(t)
            ts.wanted_at = max(ts.wanted_at, now - 20)
            if not ts.fresh(now):
                job = self.schedule_tile(ts, 1, now)
                if job is not None:
                    jobs.append(job)
        deadline = time.monotonic() + wait
        for job in jobs:
            job.wait(deadline - time.monotonic())
        return "full" if plan.mode == "tiles" else "partial"

    def find(self, *, hexid=None, callsign=None, registration=None, wait: float = 5.0) -> Aircraft | None:
        hexid = clean_hex(hexid) if hexid else None
        cs = clean_callsign(callsign) if callsign else None
        reg = clean_reg(registration) if registration else None
        now = self.clock()

        def local():
            if hexid:
                return self.tracker.get(hexid)
            with self.tracker.lock:
                for ac in self.tracker.aircraft.values():
                    if now - max(ac.pos_t, ac.seen_t) > 1200:
                        continue
                    if cs and ac.cs == cs:
                        return ac
                    if reg and ac.reg and norm_reg(ac.reg) == norm_reg(reg):
                        return ac
            return None

        ac = local()
        if (ac is None or now - ac.pos_t > 30) and self.adsbfi.available("hexes"):
            if hexid:
                job = self.adsbfi.queue.submit(f"hex:{hexid}", lambda: self._run_hexes([hexid]), priority=0, ttl=15)
            elif cs:
                job = self.adsbfi.queue.submit(f"cs:{cs}", lambda: self._absorb(self.adsbfi.callsign(cs)[1], "adsbfi",
                                                                                 self.clock()), priority=0, ttl=15)
            elif reg:
                job = self.adsbfi.queue.submit(f"reg:{reg}", lambda: self._run_reg(reg), priority=0, ttl=15)
            else:
                job = None
            if job is not None:
                job.wait(wait)
            ac = local()
        return ac

    def status(self) -> dict:
        now = self.clock()
        with self.tiles_lock:
            tiles = list(self.tiles.values())
        return {"now": round(now, 1), "aircraft": self.tracker.counts(now),
                "providers": self.sources_info(now),
                "budgets": {"adsblol": self.adsblol.budget.info(), "opensky": self.opensky.budget.info()},
                "queues": {q.name: {"size": q.size(), "busy": q.busy, **q.stats} for q in self.queues()},
                "tiles": {"cached": len(tiles), "wanted": sum(1 for t in tiles if now - t.wanted_at <= 25),
                          "fresh": sum(1 for t in tiles if t.fresh(now))},
                "military": {"age_s": round(now - self.mil_t) if self.mil_t else None, "count": len(self.mil_hexes)},
                "alerts": {"age_s": round(now - self.alerts_t) if self.alerts_t else None},
                "gpsjam": {"sampler": self.gps_sampler, "interval_s": self.gps_interval,
                           "hotspots_sampled": len(self.hotspot_t), **self.gps.summary(now)},
                "sanctions": {"known_hexes": len(self.sanc_known), "learned": len(self.learned)},
                "watching": len([h for h, t in self.watch.items() if now - t <= 40]),
                "local": {"configured": self.local.enabled()}}


def emergency_label(code: str | None) -> str:
    """'General emergency (7700)' for squawks; statuses without a squawk say so."""
    if not code:
        return "Emergency"
    if code in EMERGENCY_SQUAWKS:
        return f"{EMERGENCY_SQUAWKS[code]} ({code})"
    return f"Emergency status reported: {EMERGENCY_STATUS.get(code, 'emergency').lower()}, no emergency squawk"


def flight_phase(ac: Aircraft, ident: str, route: dict | None, dist_km: float, alat: float, alon: float) -> str:
    """arrival, departure, overhead or ground, for an aircraft near an airport."""
    if ac.gnd:
        return "ground"
    alt = ac.alt
    if route:
        dest = route.get("destination") or {}
        orig = route.get("origin") or {}
        if ident in (dest.get("icao"), dest.get("iata")) and (alt is None or alt < 15000):
            return "arrival"
        if ident in (orig.get("icao"), orig.get("iata")) and (alt is None or alt < 15000):
            return "departure"
    if alt is not None and alt < 12000:
        vr = ac.vr or 0
        if vr <= -250:
            return "arrival"
        if vr >= 250:
            return "departure"
        if alt < 4000 and dist_km < 20 and ac.trk is not None and ac.lat is not None:
            to_apt = geo.bearing_deg(ac.lat, ac.lon, alat, alon)
            diff = abs((ac.trk - to_apt + 180.0) % 360.0 - 180.0)
            if diff < 60:
                return "arrival"
            if diff > 120:
                return "departure"
    return "overhead"


def _pad_bbox(bbox, frac: float):
    w, s, e, n = bbox
    width = (e - w) % 360.0 if w > e else e - w
    if width >= 300:
        return (-180.0, max(-90.0, s - (n - s) * frac), 180.0, min(90.0, n + (n - s) * frac))
    pw = width * frac
    ph = (n - s) * frac
    return (geo.wrap_lon(w - pw), max(-90.0, s - ph), geo.wrap_lon(e + pw), min(90.0, n + ph))
