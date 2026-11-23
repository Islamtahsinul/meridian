"""Event history (CONTRACT.md section 15.3). Every five minutes the core snapshots
``App.collect_events`` into ``core_event_history`` so the timeline, alerts and briefings can ask
what happened, where and when, for the last 30 days (``MERIDIAN_HISTORY_DAYS``, 1 to 90, changes it;
it is declared as a core setting, so the Settings table lists it).

One row per (module, kind, id), upserted on every snapshot:

- ``time`` and ``first_time`` are the latest and earliest event times the module reported. They are
  equal for one-off events (a quake, a news report) and differ for ongoing ones (a storm, a tracked
  ship, a GPS jamming cell), so ``[first_time, time]`` is the span the event was active.
- ``first_seen`` and ``last_seen`` are when a snapshot first and last saw the event. ``last_seen`` is
  refreshed at most every ``LAST_SEEN_STEP_S`` seconds unless something else changed.
- ``severity`` is the highest severity seen; title, subtitle, position, layer and payload are the
  latest reported. ``payload`` keeps the module's extra fields (magnitude, alert level, ...).

Queries match events whose active span overlaps the window: ``time >= since`` and
``first_time <= until``. For one-off events that is simply ``since <= time <= until``.
A failing or slow events provider never breaks a snapshot (``App.collect_events`` isolates them and
a bad event is skipped); memory is bounded by the providers' own output and the query limits.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable

from . import geo
from .registry import json_bytes

TABLE = "core_event_history"
SNAPSHOT_EVERY_S = 300.0
PRUNE_EVERY_S = 86400.0
RETENTION_DAYS = 30.0           # default; MERIDIAN_HISTORY_DAYS changes it within the bounds below
RETENTION_SETTING = "MERIDIAN_HISTORY_DAYS"
RETENTION_MIN_DAYS = 1.0
RETENTION_MAX_DAYS = 90.0
LAST_SEEN_STEP_S = 900          # an unchanged event's last_seen is rewritten at most this often
MAX_PER_SNAPSHOT = 20000        # most severe, then most recent, beyond this
MAX_ROWS = 300000               # hard cap on the table (oldest last_seen go first)
QUERY_LIMIT_MAX = 50000
PAYLOAD_MAX = 8000              # bytes of JSON kept per event; the largest fields are dropped first
CHUNK = 500                     # rows per write transaction (keeps the store's write lock short)
KIND_MAX = 40
MODULE_MAX = 40
ID_MAX = 200
TEXT_MAX = 300
LAYER_MAX = 80
MAX_KINDS = 100
FUTURE_MAX_S = 366 * 86400      # event times further ahead than this are refused
EARLIEST = 1e8                  # 1973: anything older is a unit or parsing mistake
SUMMARY_CACHE_S = 60.0          # a summary answer is reused this long while no snapshot or prune ran
SUMMARY_CACHE_MAX = 32

# Fields stored in their own columns (or computed here); everything else goes into the payload.
_CORE_FIELDS = frozenset({"module", "kind", "id", "title", "subtitle", "lat", "lon", "time", "first_time",
                          "severity", "layer", "first_seen", "last_seen"})

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    module TEXT NOT NULL,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    subtitle TEXT NOT NULL DEFAULT '',
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    time REAL NOT NULL,
    first_time REAL NOT NULL,
    severity REAL NOT NULL DEFAULT 0,
    layer TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{{}}',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (module, kind, id)
);
CREATE INDEX IF NOT EXISTS {TABLE}_time ON {TABLE}(time);
CREATE INDEX IF NOT EXISTS {TABLE}_last_seen ON {TABLE}(last_seen);
CREATE INDEX IF NOT EXISTS {TABLE}_span ON {TABLE}(time, first_time, severity, lat, lon, kind, module);
"""

_UPSERT = f"""
INSERT INTO {TABLE} (module, kind, id, title, subtitle, lat, lon, time, first_time, severity, layer, payload,
                     first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(module, kind, id) DO UPDATE SET
    title = excluded.title, subtitle = excluded.subtitle, lat = excluded.lat, lon = excluded.lon,
    time = MAX({TABLE}.time, excluded.time),
    first_time = MIN({TABLE}.first_time, excluded.first_time),
    severity = MAX({TABLE}.severity, excluded.severity),
    layer = excluded.layer, payload = excluded.payload, last_seen = excluded.last_seen
WHERE {TABLE}.title IS NOT excluded.title OR {TABLE}.subtitle IS NOT excluded.subtitle
   OR {TABLE}.lat IS NOT excluded.lat OR {TABLE}.lon IS NOT excluded.lon
   OR excluded.time > {TABLE}.time OR excluded.first_time < {TABLE}.first_time
   OR excluded.severity > {TABLE}.severity OR {TABLE}.layer IS NOT excluded.layer
   OR {TABLE}.payload IS NOT excluded.payload
   OR excluded.last_seen - {TABLE}.last_seen >= {int(LAST_SEEN_STEP_S)}
"""


# ---------------------------------------------------------------------------------------------
# Small validators (shared with the HTTP routes in core_api)
# ---------------------------------------------------------------------------------------------
def epoch_seconds(value: Any, name: str = "time") -> float:
    """A finite epoch time in seconds. Milliseconds (> 1e11) are converted. ValueError otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number of epoch seconds")
    t = float(value)
    if not math.isfinite(t) or t < 0:
        raise ValueError(f"{name} must be a finite, non-negative number of epoch seconds")
    return t / 1000.0 if t > 1e11 else t


def clean_kinds(kinds: Any) -> list[str] | None:
    """None, a comma-separated string or an iterable of strings -> sorted unique kinds (None = all)."""
    if kinds is None:
        return None
    if isinstance(kinds, str):
        parts = kinds.split(",")
    elif isinstance(kinds, (list, tuple, set, frozenset)):
        parts = list(kinds)
    else:
        raise ValueError("kinds must be a list of strings or a comma-separated string")
    out = set()
    for k in parts:
        if not isinstance(k, str):
            raise ValueError("kinds must be strings")
        k = k.strip()
        if not k:
            continue
        if len(k) > KIND_MAX:
            raise ValueError(f"a kind is at most {KIND_MAX} characters")
        out.add(k)
    if len(out) > MAX_KINDS:
        raise ValueError(f"at most {MAX_KINDS} kinds")
    return sorted(out) or None


def clean_bbox(bbox: Any) -> tuple[float, float, float, float] | None:
    """None, ``"w,s,e,n"`` or a 4-sequence -> (w, s, e, n); west > east crosses the antimeridian."""
    if bbox is None:
        return None
    if isinstance(bbox, str):
        return geo.parse_bbox(bbox)
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in bbox):
            raise ValueError("bbox values must be numbers")
        return geo.parse_bbox(",".join(repr(float(v)) for v in bbox))
    raise ValueError("bbox must be (west, south, east, north)")


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value[:limit]


def _event_time(value: Any) -> float | None:
    """Epoch seconds from a provider value: number, numeric string, ISO 8601 or epoch ms."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        t = float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            t = float(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                return None
            t = dt.timestamp()
    else:
        return None
    if not math.isfinite(t):
        return None
    return t / 1000.0 if t > 1e11 else t


def _severity(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        s = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(s):
        return 0.0
    return round(min(10.0, max(0.0, s)), 2)


def _payload_json(payload: dict) -> str:
    try:
        text = json_bytes(payload).decode("utf-8")
    except Exception:
        return "{}"
    if len(text) <= PAYLOAD_MAX:
        return text
    try:
        sizes = sorted(((len(json_bytes(v)), k) for k, v in payload.items()), reverse=True)
        kept = dict(payload)
        for _size, key in sizes:
            kept.pop(key, None)
            text = json_bytes({**kept, "payload_truncated": True}).decode("utf-8")
            if len(text) <= PAYLOAD_MAX:
                return text
    except Exception:
        pass
    return '{"payload_truncated":true}'


def _num(value: Any) -> Any:
    """7.0 -> 7 for JSON output; other values unchanged."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
        return int(value)
    return value


def _loads(text: str) -> dict:
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def retention_days_setting(config: Any, log: logging.Logger | None = None) -> float:
    """Days of history to keep: ``MERIDIAN_HISTORY_DAYS`` from the config (default 30), held between 1
    and 90. A value that is not a number keeps the default; both cases are logged as warnings."""
    raw = config.get(RETENTION_SETTING) if config is not None else None
    if raw is None:
        return RETENTION_DAYS
    log = log or logging.getLogger("meridian.history")
    try:
        days = float(raw)
    except (TypeError, ValueError):
        days = math.nan
    if not math.isfinite(days):
        log.warning("%s is not a number of days; keeping %g days", RETENTION_SETTING, RETENTION_DAYS)
        return RETENTION_DAYS
    kept = min(RETENTION_MAX_DAYS, max(RETENTION_MIN_DAYS, days))
    if kept != days:
        log.warning("%s=%g is outside %g to %g; keeping %g days", RETENTION_SETTING, days,
                    RETENTION_MIN_DAYS, RETENTION_MAX_DAYS, kept)
    return kept


def _app_config(collect: Any) -> Any:
    """The App's Config when ``collect`` is ``App.collect_events`` (how ``App.history`` builds this)."""
    config = getattr(getattr(collect, "__self__", None), "config", None)
    if callable(getattr(config, "get", None)) and callable(getattr(config, "declare", None)):
        return config
    return None


# ---------------------------------------------------------------------------------------------
# The history
# ---------------------------------------------------------------------------------------------
class EventHistory:
    """``app.history``. ``collect(since_ts)`` returns events (``App.collect_events``).

    ``retention_days`` defaults to the ``MERIDIAN_HISTORY_DAYS`` setting of ``config`` (the App's config
    when ``collect`` is ``App.collect_events``), or 30 days without a config; a value passed here is used
    as given."""

    def __init__(self, store, collect: Callable[[float], list[dict]], *, log: logging.Logger | None = None,
                 retention_days: float | None = None, clock: Callable[[], float] = time.time,
                 config: Any = None):
        self.store = store
        self.collect = collect
        self.log = log or logging.getLogger("meridian.history")
        config = config if config is not None else _app_config(collect)
        if config is not None:
            config.declare(RETENTION_SETTING, module="core", secret=False,
                           description=f"Days of event history to keep, {RETENTION_MIN_DAYS:g} to "
                                       f"{RETENTION_MAX_DAYS:g} (default {RETENTION_DAYS:g}). The timeline, alerts "
                                       "and briefings read this history; longer windows give anomaly baselines "
                                       "more to compare with.")
        if retention_days is None:
            retention_days = retention_days_setting(config, self.log)
        self.retention_s = float(retention_days) * 86400.0
        self.clock = clock
        self._lock = threading.Lock()            # one writer (snapshot or prune) at a time
        self._scheduled = False
        self.rows: int | None = None             # cached after each snapshot / prune
        self.last: dict = {}                     # the last snapshot's summary
        self.last_prune: dict = {}
        self._version = 0                        # bumped by every snapshot and prune (summary cache key)
        self._summary_cache: dict[tuple, tuple[float, dict]] = {}
        self._cache_lock = threading.Lock()
        store.script(SCHEMA)

    # -- background tasks -----------------------------------------------------------------------
    def schedule(self, scheduler) -> None:
        """Register the snapshot (every 5 min) and prune (daily) tasks. Called by server.py's normal
        start-up only, so ``--build`` and ``--no-tasks`` never snapshot."""
        if self._scheduled:
            return
        self._scheduled = True
        scheduler.add_periodic("core.history", self.snapshot, every=SNAPSHOT_EVERY_S, initial_delay=60.0)
        scheduler.add_periodic("core.history_prune", self.prune, every=PRUNE_EVERY_S, initial_delay=240.0)

    # -- writing --------------------------------------------------------------------------------
    def _row(self, item: Any, now: float) -> tuple | None:
        if not isinstance(item, dict):
            return None
        kind = _text(item.get("kind"), KIND_MAX).strip()
        ident = item.get("id")
        if not kind or ident is None or isinstance(ident, (dict, list, bool)):
            return None
        ident = _text(ident, ID_MAX).strip()
        if not ident:
            return None
        module = _text(item.get("module"), MODULE_MAX).strip() or "unknown"
        lat, lon = item.get("lat"), item.get("lon")
        if isinstance(lat, bool) or isinstance(lon, bool) or not geo.valid_latlon(lat, lon):
            return None
        t = _event_time(item.get("time"))
        if t is None:
            if item.get("time") is not None:      # present but unreadable
                return None
            t = now                               # the contract makes time required; be lenient
        if t < EARLIEST or t > now + FUTURE_MAX_S:
            return None
        ft = _event_time(item.get("first_time"))
        ft = t if ft is None or ft < EARLIEST else min(ft, t)
        payload = {k: v for k, v in item.items() if k not in _CORE_FIELDS}
        seen = round(now, 3)
        return (module, kind, ident, _text(item.get("title"), TEXT_MAX), _text(item.get("subtitle"), TEXT_MAX),
                round(float(lat), 5), round(float(lon), 5), t, ft, _severity(item.get("severity")),
                _text(item.get("layer"), LAYER_MAX), _payload_json(payload), seen, seen)

    def snapshot(self, now: float | None = None) -> dict:
        """Collect every module's current events and upsert them. Returns a summary of the run."""
        if not self._lock.acquire(blocking=False):
            return {"skipped": True, "reason": "a snapshot or prune is already running"}
        try:
            now = self.clock() if now is None else float(now)
            started = time.monotonic()
            try:
                items = self.collect(now - self.retention_s) or []
            except Exception as err:   # App.collect_events isolates providers; a stand-in might not
                self.log.warning("event history: collecting events failed: %s", err)
                items = []
            rows: dict[tuple, tuple] = {}
            skipped = 0
            for item in items:
                try:
                    row = self._row(item, now)
                except Exception:
                    row = None
                if row is None:
                    skipped += 1
                    continue
                key = row[:3]
                prev = rows.get(key)
                if prev is not None:
                    # Two events with one key in a snapshot (e.g. a ship both dark and sanctioned):
                    # keep the most severe one (then the latest), with the widest time span.
                    best = row if (row[9], row[7]) > (prev[9], prev[7]) else prev
                    row = best[:7] + (max(row[7], prev[7]), min(row[8], prev[8])) + best[9:]
                rows[key] = row
            batch = list(rows.values())
            dropped = 0
            if len(batch) > MAX_PER_SNAPSHOT:
                batch.sort(key=lambda r: (r[9], r[7]), reverse=True)
                dropped = len(batch) - MAX_PER_SNAPSHOT
                batch = batch[:MAX_PER_SNAPSHOT]
            before = self.store.scalar(f"SELECT COUNT(*) FROM {TABLE}", default=0)
            written = 0
            for i in range(0, len(batch), CHUNK):
                with self.store.tx() as c:
                    cur = c.executemany(_UPSERT, batch[i:i + CHUNK])
                    written += max(0, cur.rowcount or 0)
            after = self.store.scalar(f"SELECT COUNT(*) FROM {TABLE}", default=0)
            self.rows = after
            self._version += 1
            self.last = {"at": now, "events": len(batch), "new": max(0, after - before),
                         "updated": max(0, written - max(0, after - before)), "skipped": skipped,
                         "dropped": dropped, "duration_s": round(time.monotonic() - started, 3)}
            return dict(self.last)
        finally:
            self._lock.release()

    def prune(self, now: float | None = None) -> dict:
        """Delete events whose time and last sighting are both older than the retention window,
        then enforce the row cap. Runs daily (and a few minutes after start-up)."""
        with self._lock:
            now = self.clock() if now is None else float(now)
            cutoff = now - self.retention_s
            deleted = 0
            while True:
                with self.store.tx() as c:
                    cur = c.execute(f"DELETE FROM {TABLE} WHERE rowid IN (SELECT rowid FROM {TABLE} "
                                    f"WHERE last_seen < ? AND time < ? LIMIT 5000)", (cutoff, cutoff))
                    n = cur.rowcount or 0
                deleted += n
                if n < 5000:
                    break
            total = self.store.scalar(f"SELECT COUNT(*) FROM {TABLE}", default=0)
            capped = 0
            if total > MAX_ROWS:
                with self.store.tx() as c:
                    cur = c.execute(f"DELETE FROM {TABLE} WHERE rowid IN (SELECT rowid FROM {TABLE} "
                                    f"ORDER BY last_seen, time LIMIT ?)", (total - MAX_ROWS,))
                    capped = cur.rowcount or 0
                total -= capped
            self.rows = total
            self._version += 1
            self.last_prune = {"at": now, "deleted": deleted, "capped": capped}
            return dict(self.last_prune)

    # -- reading --------------------------------------------------------------------------------
    @staticmethod
    def _where(since: Any, until: Any, kinds: Any, bbox: Any, min_severity: Any) -> tuple[list[str], list[Any], float, float | None]:
        since_f = epoch_seconds(since, "since")
        until_f = None if until is None else epoch_seconds(until, "until")
        if until_f is not None and until_f < since_f:
            raise ValueError("until must not be before since")
        where = ["time >= ?"]
        params: list[Any] = [since_f]
        if until_f is not None:
            where.append("first_time <= ?")
            params.append(until_f)
        kind_list = clean_kinds(kinds)
        if kind_list:
            where.append(f"kind IN ({','.join('?' * len(kind_list))})")
            params.extend(kind_list)
        box = clean_bbox(bbox)
        if box is not None:
            w, s, e, n = box
            where.append("lat BETWEEN ? AND ?")
            params.extend([s, n])
            if w <= e:
                where.append("lon BETWEEN ? AND ?")
                params.extend([w, e])
            else:
                where.append("(lon >= ? OR lon <= ?)")
                params.extend([w, e])
        if min_severity is not None:
            if isinstance(min_severity, bool) or not isinstance(min_severity, (int, float)) \
                    or not math.isfinite(float(min_severity)):
                raise ValueError("min_severity must be a number from 0 to 10")
            where.append("severity >= ?")
            params.append(min(10.0, max(0.0, float(min_severity))))
        return where, params, since_f, until_f

    @staticmethod
    def _event(row: dict) -> dict:
        ev = _loads(row.get("payload") or "")
        ev.update({
            "module": row["module"], "kind": row["kind"], "id": row["id"], "title": row["title"],
            "subtitle": row["subtitle"], "lat": row["lat"], "lon": row["lon"], "layer": row["layer"] or None,
            "time": _num(row["time"]), "first_time": _num(row["first_time"]), "severity": _num(row["severity"]),
            "first_seen": _num(round(row["first_seen"])), "last_seen": _num(round(row["last_seen"])),
        })
        return ev

    def events(self, since: float, until: float | None = None, kinds: Iterable[str] | str | None = None,
               bbox: Any = None, min_severity: float | None = None, limit: int = 5000,
               seen_since: float | None = None) -> list[dict]:
        """Events active in [since, until] (epoch seconds; until None = no upper bound), most recent
        first. Each is an Event (CONTRACT 5.6) plus ``module``, ``first_time``, ``first_seen`` and
        ``last_seen``. ``seen_since`` keeps only rows a snapshot wrote at or after that time (new or
        changed events; unchanged ones are rewritten at most every 15 minutes), using the last_seen
        index. Raises ValueError for bad arguments."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive whole number")
        where, params, _s, _u = self._where(since, until, kinds, bbox, min_severity)
        if seen_since is not None:
            if isinstance(seen_since, bool) or not isinstance(seen_since, (int, float)) or seen_since != seen_since:
                raise ValueError("seen_since must be a number of epoch seconds")
            seen = float(seen_since) / 1000.0 if seen_since > 1e11 else float(seen_since)
            where = list(where) + ["last_seen >= ?"]
            params = list(params) + [seen]
        rows = self.store.query(f"SELECT * FROM {TABLE} WHERE {' AND '.join(where)} "
                                f"ORDER BY time DESC, severity DESC LIMIT ?", params + [min(limit, QUERY_LIMIT_MAX)])
        return [self._event(r) for r in rows]

    def summary(self, since: float, until: float | None = None, bbox: Any = None) -> dict:
        """Counts of the events active in [since, until]: ``by_kind`` and ``by_module`` map a name to
        ``{count, max_severity}`` (largest first); ``by_hour`` lists ``{t, count, max_severity}`` for
        each UTC hour that has events (``t`` = hour start, epoch s; an event counts in the hour of its
        latest time, clamped into the window).

        One pass over the ``_span`` covering index (no table reads), folded here; the answer is cached
        for ``SUMMARY_CACHE_S`` seconds until the next snapshot or prune."""
        where, params, since_f, until_f = self._where(since, until, None, bbox, None)
        cache_key = (since_f, until_f, clean_bbox(bbox), self._version)
        now = time.monotonic()
        with self._cache_lock:
            hit = self._summary_cache.get(cache_key)
            if hit is not None and hit[0] > now:
                return copy.deepcopy(hit[1])
        total = 0
        max_sev = None
        kinds: dict[str, list] = {}
        modules: dict[str, list] = {}
        hours: dict[int, list] = {}
        cur = self.store.conn().execute(
            f"SELECT kind, module, time, severity FROM {TABLE} INDEXED BY {TABLE}_span "
            f"WHERE {' AND '.join(where)}", params)
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for kind, module, t, sev in rows:
                total += 1
                if max_sev is None or sev > max_sev:
                    max_sev = sev
                if t < since_f:
                    t = since_f
                if until_f is not None and t > until_f:
                    t = until_f
                for bucket, key in ((kinds, kind), (modules, module), (hours, int(t) // 3600)):
                    acc = bucket.get(key)
                    if acc is None:
                        bucket[key] = [1, sev]
                    else:
                        acc[0] += 1
                        if sev > acc[1]:
                            acc[1] = sev

        def ranked(bucket: dict) -> dict:
            return {k: {"count": v[0], "max_severity": _num(v[1])}
                    for k, v in sorted(bucket.items(), key=lambda kv: (-kv[1][0], kv[0]))}

        out = {"since": _num(since_f), "until": _num(until_f), "total": total, "max_severity": _num(max_sev),
               "by_kind": ranked(kinds), "by_module": ranked(modules),
               "by_hour": [{"t": h * 3600, "count": v[0], "max_severity": _num(v[1])}
                           for h, v in sorted(hours.items())]}
        with self._cache_lock:
            if len(self._summary_cache) >= SUMMARY_CACHE_MAX:
                self._summary_cache = {k: v for k, v in self._summary_cache.items() if v[0] > now}
                while len(self._summary_cache) >= SUMMARY_CACHE_MAX:
                    self._summary_cache.pop(next(iter(self._summary_cache)))
            self._summary_cache[cache_key] = (now + SUMMARY_CACHE_S, copy.deepcopy(out))
        return out

    def info(self) -> dict:
        """State for /api/core/status (no database query)."""
        return {"table": TABLE, "every_s": _num(SNAPSHOT_EVERY_S), "retention_days": _num(self.retention_s / 86400.0),
                "rows": self.rows, "scheduled": self._scheduled, "last_snapshot": dict(self.last) or None,
                "last_prune": dict(self.last_prune) or None}
