"""Sampled aircraft positions for timeline replay (CONTRACT.md section 15.4).

Every observation the module already receives passes through :meth:`FlightsHistory.sample` from
one hook in ``FlightsService._absorb``: map tiles for the area on screen, the worldwide military and
emergency sets, watched cards, sanctioned aircraft, discovery look-ups and a local receiver. The
history makes no requests of its own.

Sampling, per aircraft:

- never more often than ``MIN_STEP_S`` (60 s, CONTRACT 15.4) and at least every ``MAX_STEP_S``
  (5 min) while the aircraft is being received;
- in between, as soon as the track has turned by ``TURN_DEG`` or the altitude has changed by
  ``CLIMB_FT`` since the last sample, or its flags change (on the ground, emergency, military, OFAC
  listing), so straight-line interpolation between samples stays within about a kilometre;
- the last observation before an aircraft stops arriving (it left coverage, landed, or nobody is
  looking at that area any more) is written once ``TAIL_AFTER_S`` has passed, so replay ends where
  the live map lost it.

Not kept: estimated (dead-reckoned) positions; aircraft in the LADD or PIA privacy programmes
(golden rule 2: the live map shows them as the feeds do, but Meridian keeps no movement history of
them); and from the GPS-interference sampler's 16 hotspot circles only military, emergency and
OFAC-listed aircraft. The rest of the hotspot traffic would add an estimated 15,000 to 30,000 rows
an hour around the clock (16 circles of about 150 aircraft, 6 to 12 passes an hour: 1 to 2 million
rows, 60 to 130 MB, per 72 h) of 5-to-10-minute snapshots of circles that aircraft cross between
samples: tracks too coarse to replay well, for areas nobody may look at, and the sampler exists for
the interference statistics.

Bounds: ``KEEP_S`` (72 h) retention, a hard row cap (``FLIGHTS_HISTORY_MAX_ROWS``, default 2
million, the oldest go first), and caps on the in-memory buffer and per-aircraft state.

Storage: ``flights_history`` is a WITHOUT ROWID table clustered on (t, hex) with a (hex, t) index,
integers only (hex as a 24-bit number, coordinates in 1e-5 degrees). ``flights_history_ident``
keeps one row per aircraft and callsign spell (callsign, registration, type) so replay can label
aircraft as they were.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable, Iterable

from meridian import history as core_history
from meridian.registry import HTTPError

from .const import FLAG_EMG, FLAG_GND, FLAG_LOCAL, FLAG_MIL, FLAG_MLAT, FLAG_SANC
from .parse import clean_hex

TABLE = "flights_history"
IDENT = "flights_history_ident"

MIN_STEP_S = 60           # CONTRACT 15.4: at least 60 s between samples of one aircraft
MAX_STEP_S = 300          # ... and one at least every 5 minutes while it is received
TURN_DEG = 8.0            # a turn this large since the last sample asks for a new one
CLIMB_FT = 1000           # ... and so does a climb or descent of this much
TAIL_AFTER_S = 90         # not observed for this long: write its newest unsaved observation
FORGET_S = 1800           # per-aircraft state is dropped this long after the last observation
KEEP_S = 72 * 3600        # retention
MAX_ROWS_DEFAULT = 2_000_000
PENDING_MAX = 200_000     # unwritten samples held between writes (oldest dropped beyond)
STATE_MAX = 60_000        # aircraft with sampling state
WRITE_CHUNK = 5000
TICK_S = 20.0             # write interval
PRUNE_EVERY_S = 900.0
WINDOW_MAX_S = 6 * 3600   # /api/flights/history spans at most this
LIMIT_DEFAULT = 20000
LIMIT_MAX = 50000
TRACK_MAX = 5000          # points in one /api/flights/history/{hex} answer

# Stored class numbers. Fixed here (append only) so stored rows keep their meaning.
CLASS_IDS = ("airliner", "cargo", "business", "military", "helicopter", "light", "glider", "ground", "unknown")
CLASS_INDEX = {c: i for i, c in enumerate(CLASS_IDS)}
UNKNOWN = CLASS_INDEX["unknown"]

KEEP_FLAGS = FLAG_MIL | FLAG_EMG | FLAG_SANC | FLAG_GND | FLAG_MLAT | FLAG_LOCAL
NOTABLE = FLAG_MIL | FLAG_EMG | FLAG_SANC
FIELDS = ["hex", "t", "lat", "lon", "alt", "trk", "cls", "flags"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    t INTEGER NOT NULL,
    hex INTEGER NOT NULL,
    lat INTEGER NOT NULL,
    lon INTEGER NOT NULL,
    alt INTEGER,
    trk INTEGER,
    cls INTEGER NOT NULL,
    flags INTEGER NOT NULL,
    PRIMARY KEY (t, hex)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS {TABLE}_hex ON {TABLE}(hex, t);
CREATE TABLE IF NOT EXISTS {IDENT} (
    hex INTEGER NOT NULL,
    first_t INTEGER NOT NULL,
    last_t INTEGER NOT NULL,
    cs TEXT NOT NULL DEFAULT '',
    reg TEXT NOT NULL DEFAULT '',
    typ TEXT NOT NULL DEFAULT '',
    descr TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (hex, first_t)
);
CREATE INDEX IF NOT EXISTS {IDENT}_last ON {IDENT}(last_t);
"""

_INSERT = f"INSERT OR IGNORE INTO {TABLE}(t, hex, lat, lon, alt, trk, cls, flags) VALUES (?,?,?,?,?,?,?,?)"
_UPSERT_IDENT = (f"INSERT INTO {IDENT}(hex, first_t, last_t, cs, reg, typ, descr) VALUES (?,?,?,?,?,?,?) "
                 f"ON CONFLICT(hex, first_t) DO UPDATE SET last_t = MAX({IDENT}.last_t, excluded.last_t), "
                 f"cs = excluded.cs, reg = excluded.reg, typ = excluded.typ, descr = excluded.descr")


def _turned(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return False
    return abs((a - b + 180) % 360 - 180) >= TURN_DEG


def _climbed(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    return abs(a - b) >= CLIMB_FT


def _box_sql(bbox, lat_col: str = "lat", lon_col: str = "lon") -> tuple[list[str], list[int]]:
    """WHERE clauses for a (w, s, e, n) box over integer 1e-5 degree columns."""
    if bbox is None:
        return [], []
    w, s, e, n = bbox
    where = [f"{lat_col} BETWEEN ? AND ?"]
    params = [int(math.floor(s * 1e5)), int(math.ceil(n * 1e5))]
    if w <= e:
        if not (w <= -180.0 and e >= 180.0):
            where.append(f"{lon_col} BETWEEN ? AND ?")
            params += [int(math.floor(w * 1e5)), int(math.ceil(e * 1e5))]
    else:                                   # crossing the antimeridian
        where.append(f"({lon_col} >= ? OR {lon_col} <= ?)")
        params += [int(math.floor(w * 1e5)), int(math.ceil(e * 1e5))]
    return where, params


def _in_box(lat_i: int, lon_i: int, bbox) -> bool:
    if bbox is None:
        return True
    w, s, e, n = bbox
    lat, lon = lat_i / 1e5, lon_i / 1e5
    if not (s <= lat <= n):
        return False
    if w <= e:
        return w <= lon <= e
    return lon >= w or lon <= e


def position(row: tuple) -> dict:
    """A stored row (t, hex, lat, lon, alt, trk, cls, flags) as the CONTRACT 15.4 object."""
    t, hx, lat, lon, alt, trk, cls, flags = row
    return {"hex": f"{hx:06x}", "t": t, "lat": lat / 1e5, "lon": lon / 1e5, "alt": alt, "trk": trk,
            "cls": CLASS_IDS[cls] if 0 <= cls < len(CLASS_IDS) else "unknown", "flags": flags}


def compact(row: tuple) -> list:
    """The same as a list in FIELDS order (format=rows)."""
    t, hx, lat, lon, alt, trk, cls, flags = row
    return [f"{hx:06x}", t, lat / 1e5, lon / 1e5, alt, trk,
            CLASS_IDS[cls] if 0 <= cls < len(CLASS_IDS) else "unknown", flags]


class FlightsHistory:
    """``svc.history``: sampling state, a write buffer and the queries behind the endpoints."""

    def __init__(self, store, *, clock: Callable[[], float] = time.time, log: logging.Logger | None = None,
                 max_rows: int = MAX_ROWS_DEFAULT, keep_s: float = KEEP_S):
        self.store = store
        self.clock = clock
        self.log = log or logging.getLogger("meridian.flights")
        self.max_rows = max(1000, int(max_rows))
        self.keep_s = float(keep_s)
        self.lock = threading.Lock()
        self.state: dict[str, list] = {}     # hex -> [saved_t, trk, alt, flags, tail, seen_at]
        self.spells: dict[str, list] = {}    # hex -> [first_t, last_t, cs, reg, typ, desc] (current spell)
        self.pending: list[tuple] = []
        self.pending_idents: dict[tuple, tuple] = {}
        self.rows: int | None = None         # cached row count (None until the first prune)
        self.hours: dict[int, int] | None = None
        self.last_prune = 0.0
        self.last_write: dict = {}
        self.stats = {"observed": 0, "written": 0, "tails": 0, "private": 0, "dropped": 0, "pruned": 0,
                      "capped": 0, "restored": 0}
        store.script(SCHEMA)
        self._restore()

    def _restore(self) -> None:
        """Pick up the sampling state of the last half hour from SQLite, so a restart keeps the 60 s
        spacing and continues callsign spells instead of starting every aircraft afresh."""
        now = self.clock()
        since = int(now - FORGET_S)
        try:
            conn = self.store.conn()
            rows = conn.execute(f"SELECT hex, t, alt, trk, flags FROM {TABLE} WHERE t >= ? ORDER BY t", (since,)).fetchall()
            spells = conn.execute(f"SELECT hex, first_t, last_t, cs, reg, typ, descr FROM {IDENT} WHERE last_t >= ?",
                                  (since,)).fetchall()
        except Exception as err:          # a damaged table never stops the module
            self.log.warning("flights history: could not restore the sampling state: %s", err)
            return
        for hx, t, alt, trk, flags in rows:
            self.state[f"{hx:06x}"] = [int(t), trk, alt, flags, None, now]
        for hx, first_t, last_t, cs, reg, typ, desc in spells:
            h = f"{hx:06x}"
            cur = self.spells.get(h)
            if cur is None or first_t > cur[0]:
                self.spells[h] = [int(first_t), int(last_t), cs or "", reg or "", typ or "", desc or ""]
        self.stats["restored"] = len(self.state)

    def _stored_near(self, h: str, t: int) -> bool:
        """Is a sample of this aircraft already stored within a minute of t? (First sight of an old
        position, older than the restored state reaches.)"""
        row = self.store.conn().execute(f"SELECT 1 FROM {TABLE} WHERE hex = ? AND t > ? AND t < ? LIMIT 1",
                                        (int(h, 16), t - MIN_STEP_S, t + MIN_STEP_S)).fetchone()
        return row is not None

    # -- sampling ---------------------------------------------------------------------------------
    def sample(self, tracker, hexes: Iterable[str], *, notable_only: bool = False) -> int:
        """Called after the tracker merged a batch of observations. Returns samples taken."""
        if not hexes:
            return 0
        now = self.clock()
        obs = []
        private = 0
        with tracker.lock:
            for h in hexes:
                ac = tracker.aircraft.get(h)
                if ac is None or ac.lat is None or not ac.pos_t:
                    continue
                if ac.private:
                    private += 1
                    continue
                flags = ac.flags(False) & KEEP_FLAGS
                if notable_only and not flags & NOTABLE:
                    continue
                obs.append((h, ac.pos_t, ac.lat, ac.lon, ac.alt, ac.trk, ac.cls, flags, ac.cs, ac.reg, ac.typ,
                            ac.desc))
        if not obs and not private:
            return 0
        taken = 0
        with self.lock:
            self.stats["private"] += private
            self.stats["observed"] += len(obs)
            for h, pos_t, lat, lon, alt, trk, cls, flags, cs, reg, typ, desc in obs:
                t = int(pos_t)
                if t > now + 60 or t < now - self.keep_s:
                    continue
                trk_i = None if trk is None else int(round(trk)) % 360
                alt_i = None if alt is None else int(alt)
                row = (t, int(h, 16), int(round(lat * 1e5)), int(round(lon * 1e5)), alt_i, trk_i,
                       CLASS_INDEX.get(cls, UNKNOWN), flags)
                ident = (cs, reg, typ, desc)
                st = self.state.get(h)
                if st is None:
                    if t < now - FORGET_S and self._stored_near(h, t):
                        continue                # an old position next to one already kept
                    if len(self.state) >= STATE_MAX:
                        self._trim_state()
                    self.state[h] = [t, trk_i, alt_i, flags, None, now]
                    self._take(h, row, ident)
                    taken += 1
                    continue
                st[5] = now
                if t <= st[0] or (st[4] is not None and t <= st[4][0][0]):
                    continue                    # nothing newer than what we have
                dt = t - st[0]
                if dt >= MIN_STEP_S and (dt >= MAX_STEP_S or flags != st[3] or _turned(trk_i, st[1])
                                         or _climbed(alt_i, st[2])):
                    st[0], st[1], st[2], st[3], st[4] = t, trk_i, alt_i, flags, None
                    self._take(h, row, ident)
                    taken += 1
                else:
                    st[4] = (row, ident)        # the newest unsaved observation (a tail candidate)
        return taken

    def _take(self, h: str, row: tuple, ident: tuple) -> None:
        self.pending.append(row)
        if len(self.pending) > PENDING_MAX:
            extra = len(self.pending) - PENDING_MAX
            del self.pending[:extra]
            self.stats["dropped"] += extra
        cs, reg, typ, desc = ident
        t = row[0]
        spell = self.spells.get(h)
        if spell is None or spell[2] != (cs or ""):
            spell = [t, t, cs or "", reg or "", typ or "", desc or ""]
            self.spells[h] = spell
        else:
            spell[1] = max(spell[1], t)
            spell[3] = reg or spell[3]
            spell[4] = typ or spell[4]
            spell[5] = desc or spell[5]
        self.pending_idents[(row[1], spell[0])] = (row[1], spell[0], spell[1], spell[2], spell[3][:12],
                                                   spell[4][:8], spell[5][:80])

    def _trim_state(self) -> None:
        """Drop the tenth of aircraft observed longest ago (their tails are written first)."""
        oldest = sorted(self.state.items(), key=lambda kv: kv[1][5])[: max(1, STATE_MAX // 10)]
        for h, st in oldest:
            self._flush_tail(h, st)
            self.state.pop(h, None)
            self.spells.pop(h, None)

    def _flush_tail(self, h: str, st: list) -> bool:
        tail = st[4]
        st[4] = None
        if tail is None:
            return False
        row, ident = tail
        if row[0] - st[0] < MIN_STEP_S:
            return False                        # within a minute of the last sample: not worth one
        st[0], st[1], st[2], st[3] = row[0], row[5], row[4], row[7]
        self._take(h, row, ident)
        self.stats["tails"] += 1
        return True

    def _tails(self, now: float) -> None:
        gone = []
        for h, st in self.state.items():
            idle = now - st[5]
            if st[4] is not None and idle >= TAIL_AFTER_S:
                self._flush_tail(h, st)
            if idle >= FORGET_S:
                gone.append(h)
        for h in gone:
            self.state.pop(h, None)
            self.spells.pop(h, None)

    # -- writing and pruning ------------------------------------------------------------------------
    def tick(self, now: float | None = None) -> dict:
        """Background task: write tails and buffered samples; prune every 15 minutes."""
        now = self.clock() if now is None else float(now)
        with self.lock:
            self._tails(now)
            rows, self.pending = self.pending, []
            idents = list(self.pending_idents.values())
            self.pending_idents = {}
        written = 0
        for i in range(0, len(rows), WRITE_CHUNK):
            chunk = rows[i:i + WRITE_CHUNK]
            with self.store.tx() as c:
                cur = c.executemany(_INSERT, chunk)
                written += max(0, cur.rowcount or 0)
        for i in range(0, len(idents), WRITE_CHUNK):
            with self.store.tx() as c:
                c.executemany(_UPSERT_IDENT, idents[i:i + WRITE_CHUNK])
        with self.lock:
            self.stats["written"] += written
            if self.rows is not None:
                self.rows += written
            if self.hours is not None and written:
                for r in rows:
                    self.hours[r[0] // 3600] = self.hours.get(r[0] // 3600, 0) + 1
        self.last_write = {"at": now, "rows": written, "idents": len(idents)}
        out = dict(self.last_write)
        if now - self.last_prune >= PRUNE_EVERY_S:
            out["prune"] = self.prune(now)
        return out

    def prune(self, now: float | None = None) -> dict:
        """Delete samples older than the retention window, then enforce the row cap (oldest first).
        Deletes run in half-hour slices so the store's write lock is held briefly."""
        now = self.clock() if now is None else float(now)
        self.last_prune = now
        cutoff = int(now - self.keep_s)
        deleted = self._delete_before(cutoff)
        with self.store.tx() as c:
            c.execute(f"DELETE FROM {IDENT} WHERE last_t < ?", (cutoff,))
        if self.rows is None or self.hours is None:
            self._count()
        else:
            with self.lock:
                self.rows = max(0, self.rows - deleted)
                for hr in [hr for hr in self.hours if hr < cutoff // 3600]:
                    del self.hours[hr]
        capped = 0
        if self.rows is not None and self.rows > self.max_rows:
            excess = self.rows - self.max_rows
            row = self.store.conn().execute(f"SELECT t FROM {TABLE} ORDER BY t LIMIT 1 OFFSET ?",
                                            (excess,)).fetchone()
            if row is not None:
                capped = self._delete_before(int(row[0]))
                self._count()
        with self.lock:
            self.stats["pruned"] += deleted
            self.stats["capped"] += capped
        return {"deleted": deleted, "capped": capped, "rows": self.rows}

    def _delete_before(self, cutoff: int) -> int:
        deleted = 0
        conn = self.store.conn()
        while True:
            lo = conn.execute(f"SELECT MIN(t) FROM {TABLE}").fetchone()[0]
            if lo is None or lo >= cutoff:
                break
            hi = min(cutoff, int(lo) + 1800)
            with self.store.tx() as c:
                cur = c.execute(f"DELETE FROM {TABLE} WHERE t < ?", (hi,))
                deleted += max(0, cur.rowcount or 0)
        return deleted

    def _count(self) -> None:
        hours = {int(h): int(n) for h, n in self.store.conn().execute(
            f"SELECT t / 3600 AS h, COUNT(*) FROM {TABLE} GROUP BY h").fetchall()}
        with self.lock:
            self.hours = hours
            self.rows = sum(hours.values())

    # -- reading --------------------------------------------------------------------------------------
    def _pending_rows(self, since: int, until: int, bbox=None, hexint: int | None = None) -> list[tuple]:
        with self.lock:
            return [r for r in self.pending if since <= r[0] <= until and (hexint is None or r[1] == hexint)
                    and _in_box(r[2], r[3], bbox)]

    def window(self, since: float, until: float, bbox=None, limit: int = LIMIT_DEFAULT) -> tuple[list[tuple], bool]:
        """Samples with since <= t <= until (in the box), oldest first: (rows, truncated)."""
        lo, hi = int(math.floor(since)), int(math.ceil(until))
        where, params = _box_sql(bbox)
        clause = " AND ".join(["t >= ?", "t <= ?"] + where)
        rows = self.store.conn().execute(
            f"SELECT t, hex, lat, lon, alt, trk, cls, flags FROM {TABLE} WHERE {clause} ORDER BY t, hex LIMIT ?",
            [lo, hi] + params + [limit + 1]).fetchall()
        rows = [tuple(r) for r in rows]
        extra = self._pending_rows(lo, hi, bbox)
        if extra:
            seen = {(r[0], r[1]) for r in rows}
            rows += [r for r in extra if (r[0], r[1]) not in seen]
            rows.sort(key=lambda r: (r[0], r[1]))
        truncated = len(rows) > limit
        return rows[:limit], truncated

    def track(self, hexid: str, since: float, until: float) -> tuple[list[tuple], bool]:
        hx = int(hexid, 16)
        lo, hi = int(math.floor(since)), int(math.ceil(until))
        rows = [tuple(r) for r in self.store.conn().execute(
            f"SELECT t, hex, lat, lon, alt, trk, cls, flags FROM {TABLE} WHERE hex = ? AND t >= ? AND t <= ? "
            f"ORDER BY t LIMIT ?", (hx, lo, hi, TRACK_MAX + 1)).fetchall()]
        extra = self._pending_rows(lo, hi, None, hx)
        if extra:
            seen = {r[0] for r in rows}
            rows = sorted(rows + [r for r in extra if r[0] not in seen])
        return rows[:TRACK_MAX], len(rows) > TRACK_MAX

    def spells_for(self, hexints: set[int], since: float, until: float) -> dict[int, list[dict]]:
        """Callsign spells overlapping [since, until] for these aircraft, oldest first."""
        if not hexints:
            return {}
        out: dict[int, dict[int, dict]] = {}

        def add(hx, first_t, last_t, cs, reg, typ, desc):
            if hx in hexints and last_t >= since and first_t <= until:
                out.setdefault(hx, {})[first_t] = {"cs": cs or None, "reg": reg or None, "typ": typ or None,
                                                   "desc": desc or None, "from": first_t, "to": last_t}

        if len(hexints) <= 400:
            marks = ",".join("?" * len(hexints))
            rows = self.store.conn().execute(
                f"SELECT hex, first_t, last_t, cs, reg, typ, descr FROM {IDENT} WHERE hex IN ({marks}) "
                f"AND last_t >= ? AND first_t <= ?", [*hexints, int(since), int(until) + 1]).fetchall()
        else:
            rows = self.store.conn().execute(
                f"SELECT hex, first_t, last_t, cs, reg, typ, descr FROM {IDENT} WHERE last_t >= ? AND first_t <= ?",
                (int(since), int(until) + 1)).fetchall()
        for r in rows:
            add(*r)
        with self.lock:
            for row in self.pending_idents.values():
                add(*row)
        return {hx: [d[k] for k in sorted(d)] for hx, d in out.items()}

    def coverage(self, since: float, until: float) -> list[dict]:
        if self.hours is None:
            self._count()
        lo, hi = int(since) // 3600, int(until) // 3600
        with self.lock:
            hours = dict(self.hours or {})
            for r in self.pending:
                hours[r[0] // 3600] = hours.get(r[0] // 3600, 0) + 1
        return [{"t": h * 3600, "samples": n} for h, n in sorted(hours.items()) if lo <= h <= hi and n]

    def extent(self) -> dict:
        conn = self.store.conn()
        lo = conn.execute(f"SELECT MIN(t) FROM {TABLE}").fetchone()[0]
        hi = conn.execute(f"SELECT MAX(t) FROM {TABLE}").fetchone()[0]
        with self.lock:
            if self.pending:
                p_lo = min(r[0] for r in self.pending)
                p_hi = max(r[0] for r in self.pending)
                lo = p_lo if lo is None else min(lo, p_lo)
                hi = p_hi if hi is None else max(hi, p_hi)
        return {"from": lo, "to": hi}

    def info(self) -> dict:
        with self.lock:
            return {"rows": self.rows, "pending": len(self.pending), "aircraft": len(self.state),
                    "max_rows": self.max_rows, "keep_h": round(self.keep_s / 3600), "last_write": self.last_write,
                    "stats": dict(self.stats),
                    "sampling": {"min_step_s": MIN_STEP_S, "max_step_s": MAX_STEP_S, "turn_deg": TURN_DEG,
                                 "climb_ft": CLIMB_FT}}


# ---------------------------------------------------------------------------------------------
# Routes (CONTRACT 15.4)
# ---------------------------------------------------------------------------------------------
def _epoch(req, name: str, default):
    value = req.arg(name, None, type=float)
    if value is None:
        return default
    try:
        return core_history.epoch_seconds(value, name)
    except ValueError:
        raise HTTPError(400, "BAD_ARG", f'Query parameter "{name}" must be a time in epoch seconds.') from None


def window_args(req, now: float, *, default_span: float, max_span: float) -> tuple[float, float]:
    """from and to (epoch seconds; milliseconds accepted). Without either: the last default_span
    seconds. With only from: up to default_span after it (not past now)."""
    until = _epoch(req, "to", None)
    since = _epoch(req, "from", None)
    if until is None:
        until = now if since is None else max(since, min(now, since + default_span))
    if since is None:
        since = until - default_span
    if until < since:
        raise HTTPError(400, "BAD_ARG", 'Query parameter "to" must not be before "from".')
    if until - since > max_span:
        raise HTTPError(400, "BAD_ARG", f"Ask for at most {max_span / 3600:g} hours at a time.")
    return since, until


def register_history(m, svc) -> FlightsHistory | None:
    """Create the history, hook it into the service, and register its task and routes."""
    m.key("FLIGHTS_HISTORY", "Set to 0 to keep no aircraft history (timeline replay then shows no aircraft).",
          secret=False)
    m.key("FLIGHTS_HISTORY_MAX_ROWS", "Most aircraft samples kept for timeline replay (default 2,000,000, "
          "about 100 MB); the oldest go first.", secret=False)
    m.source("flights.history", "Aircraft history for timeline replay", "",
             "Derived from the live sources above; kept 72 hours on this computer only", layer="flights",
             refresh="sampled from what Meridian already receives (no extra requests), written every 20 s",
             optional=True)
    enabled = m.config.get_bool("FLIGHTS_HISTORY", True)
    max_rows = m.config.get_int("FLIGHTS_HISTORY_MAX_ROWS", MAX_ROWS_DEFAULT)
    hist = FlightsHistory(m.store, clock=svc.clock, log=m.log, max_rows=max_rows)
    svc.history = hist if enabled else None

    def tick():
        if svc.history is None:
            return None
        out = hist.tick()
        if hist.rows is not None:
            m.status("flights.history", ok=True, count=hist.rows,
                     note=f"{len(hist.state):,} aircraft sampled in the last half hour")
        return out

    m.task("history", tick, every=TICK_S, initial_delay=25.0)

    # Replay reads never mark the module active (CONTRACT 5.2): looking back must not speed up live polling.

    def answer_meta(since: float, until: float) -> dict:
        return {"from": since, "to": until, "available": hist.extent(), "keep_h": round(hist.keep_s / 3600),
                "sampling": {"min_step_s": MIN_STEP_S, "max_step_s": MAX_STEP_S},
                "note": None if enabled else "Aircraft history is off (FLIGHTS_HISTORY=0)."}

    @m.route("GET", "/api/flights/history", activity=False)
    def history_window(req):
        now = svc.clock()
        since, until = window_args(req, now, default_span=3600.0, max_span=WINDOW_MAX_S)
        bbox = req.bbox("bbox", None)
        limit = req.arg("limit", LIMIT_DEFAULT, type=int, min=1, max=LIMIT_MAX)
        fmt = req.arg("format", "objects", choices={"objects", "rows"})
        rows, truncated = hist.window(since, until, bbox, limit)
        hexes = {r[1] for r in rows}
        spells = hist.spells_for(hexes, since, until)
        idents = {}
        for hx, items in spells.items():
            last = items[-1]
            idents[f"{hx:06x}"] = {k: last[k] for k in ("cs", "reg", "typ", "desc")}
        out = answer_meta(since, until)
        out.update({"count": len(rows), "truncated": truncated, "aircraft": len(hexes), "idents": idents})
        if fmt == "rows":
            out["fields"] = FIELDS
            out["rows"] = [compact(r) for r in rows]
        else:
            out["positions"] = [position(r) for r in rows]
        return out

    @m.route("GET", "/api/flights/history/coverage", activity=False)
    def history_coverage(req):
        now = svc.clock()
        since, until = window_args(req, now, default_span=hist.keep_s, max_span=hist.keep_s + 86400)
        out = answer_meta(since, until)
        out.update({"by_hour": hist.coverage(since, until), "rows": hist.rows})
        return out

    @m.route("GET", "/api/flights/history/{hex}", activity=False)
    def history_aircraft(req):
        hexid = clean_hex(req.params["hex"])
        if hexid is None:
            raise HTTPError(400, "BAD_ARG", "An aircraft id is a six-character ICAO hex code, like 4ca2b3.")
        now = svc.clock()
        since, until = window_args(req, now, default_span=hist.keep_s, max_span=hist.keep_s + 86400)
        rows, truncated = hist.track(hexid, since, until)
        out = answer_meta(since, until)
        out.update({"hex": hexid, "count": len(rows), "truncated": truncated,
                    "positions": [position(r) for r in rows],
                    "idents": hist.spells_for({int(hexid, 16)}, since, until).get(int(hexid, 16), [])})
        if not rows:
            out["note"] = out["note"] or "No recorded positions for this aircraft in that window."
        return out

    return hist
