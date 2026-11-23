"""Vessel positions for timeline replay (CONTRACT.md section 15.4).

The vessel store already writes a thinned track to ``ships_track`` (store.py: at least 2 minutes
apart, one point per 10 minutes on a straight course, more in turns, one every 2 hours when
stationary, nothing for positions that look spoofed). Its retention is 72 hours
(``store.KEEP_TRACK_S``). This file adds:

- a hard row cap (``SHIPS_HISTORY_MAX_ROWS``, default 2.5 million, oldest first), checked every
  15 minutes: with an aisstream.io key and the world subscription the track can grow far faster
  than with Digitraffic alone;
- ``GET /api/ships/history?from&to&bbox&limit`` and ``GET /api/ships/history/{mmsi}?from&to``,
  merging the stored track with points not written yet and, for windows that reach the last
  15 minutes, each vessel's latest fix (it reaches the track only at the next thinning step);
- ``GET /api/ships/history/coverage`` (stored points per hour, for the time bar).

The store keeps no class or name per point: answers use each vessel's current class and name.
"""

from __future__ import annotations

import math
import threading
import time

from meridian.registry import HTTPError

from .classify import CLASS_IDS
from .store import KEEP_TRACK_S

TABLE = "ships_track"
MAX_ROWS_DEFAULT = 2_500_000
WINDOW_MAX_S = 6 * 3600
LIMIT_DEFAULT = 20000
LIMIT_MAX = 50000
TRACK_MAX = 5000
RECENT_S = 900            # windows ending this close to now also get each vessel's latest fix
CAP_EVERY_S = 900.0
COVERAGE_TTL_S = 120.0
FIELDS = ["mmsi", "t", "lat", "lon", "sog", "cog", "cls"]


def _box_sql(bbox) -> tuple[list[str], list[int]]:
    if bbox is None:
        return [], []
    w, s, e, n = bbox
    where = ["lat BETWEEN ? AND ?"]
    params = [int(math.floor(s * 1e5)), int(math.ceil(n * 1e5))]
    if w <= e:
        if not (w <= -180.0 and e >= 180.0):
            where.append("lon BETWEEN ? AND ?")
            params += [int(math.floor(w * 1e5)), int(math.ceil(e * 1e5))]
    else:
        where.append("(lon >= ? OR lon <= ?)")
        params += [int(math.floor(w * 1e5)), int(math.ceil(e * 1e5))]
    return where, params


def _in_box(lat: float, lon: float, bbox) -> bool:
    if bbox is None:
        return True
    w, s, e, n = bbox
    if not (s <= lat <= n):
        return False
    if w <= e:
        return w <= lon <= e
    return lon >= w or lon <= e


class ShipsHistory:
    def __init__(self, store, db, *, now=time.time, max_rows: int = MAX_ROWS_DEFAULT):
        self.store = store            # the VesselStore
        self.db = db
        self.now = now
        self.max_rows = max(1000, int(max_rows))
        self.keep_s = float(KEEP_TRACK_S)
        self.lock = threading.Lock()
        self.rows: int | None = None
        self._coverage: tuple[float, dict] | None = None
        self.last_cap: dict = {}

    # -- reading -------------------------------------------------------------------------------------
    def window(self, since: float, until: float, bbox=None, limit: int = LIMIT_DEFAULT) -> tuple[list[tuple], bool]:
        """Points (mmsi, t, lat, lon, sog, cog) with since <= t <= until in the box, oldest first.
        lat/lon in degrees, sog in knots, cog in degrees (None when unknown)."""
        lo, hi = int(math.floor(since)), int(math.ceil(until))
        where, params = _box_sql(bbox)
        clause = " AND ".join(["t >= ?", "t <= ?"] + where)
        raw = self.db.conn().execute(
            f"SELECT mmsi, t, lat, lon, sog, cog FROM {TABLE} WHERE {clause} ORDER BY t, mmsi LIMIT ?",
            [lo, hi] + params + [limit + 1]).fetchall()
        rows = {(int(r[0]), int(r[1])): (int(r[0]), int(r[1]), r[2] / 1e5, r[3] / 1e5,
                                          None if r[4] is None else r[4] / 10.0,
                                          None if r[5] is None else r[5] / 10.0) for r in raw}
        now = self.now()
        with self.store.lock:
            for p in self.store.pending_track:
                if lo <= p[1] <= hi and (p[0], p[1]) not in rows:
                    lat, lon = p[2] / 1e5, p[3] / 1e5
                    if _in_box(lat, lon, bbox):
                        rows[(p[0], p[1])] = (p[0], p[1], lat, lon, None if p[4] is None else p[4] / 10.0,
                                              None if p[5] is None else p[5] / 10.0)
            if hi >= now - RECENT_S:
                for v in self.store.vessels.values():
                    if v.lat is None or not v.t or not (lo <= v.t <= hi) or v.suspect_t == v.t:
                        continue
                    key = (v.mmsi, int(v.t))
                    if key not in rows and _in_box(v.lat, v.lon, bbox):
                        rows[key] = (v.mmsi, int(v.t), round(v.lat, 5), round(v.lon, 5), v.sog, v.cog)
        out = sorted(rows.values(), key=lambda r: (r[1], r[0]))
        return out[:limit], len(out) > limit

    def idents(self, mmsis) -> dict[int, dict]:
        out = {}
        with self.store.lock:
            for mmsi in mmsis:
                v = self.store.vessels.get(int(mmsi))
                if v is None:
                    out[int(mmsi)] = {"name": None, "cls": "other", "length": None}
                else:
                    out[int(mmsi)] = {"name": v.name or None, "cls": v.cls if v.cls in CLASS_IDS else "other",
                                      "length": v.length}
        return out

    def track(self, mmsi: int, since: float, until: float) -> tuple[list[tuple], bool]:
        pts = [p for p in self.store.trail(mmsi, since) if p[0] <= until]
        return pts[:TRACK_MAX], len(pts) > TRACK_MAX

    def coverage(self, since: float, until: float) -> list[dict]:
        now = self.now()
        with self.lock:
            cached = self._coverage
        if cached is None or now - cached[0] > COVERAGE_TTL_S:
            hours = {int(h): int(n) for h, n in self.db.conn().execute(
                f"SELECT t / 3600 AS h, COUNT(*) FROM {TABLE} WHERE t >= ? GROUP BY h",
                (int(now - self.keep_s - 3600),)).fetchall()}
            with self.lock:
                self._coverage = cached = (now, hours)
        hours = dict(cached[1])
        with self.store.lock:
            for p in self.store.pending_track:
                hours[p[1] // 3600] = hours.get(p[1] // 3600, 0) + 1
        lo, hi = int(since) // 3600, int(until) // 3600
        return [{"t": h * 3600, "samples": n} for h, n in sorted(hours.items()) if lo <= h <= hi and n]

    def extent(self) -> dict:
        conn = self.db.conn()
        lo = conn.execute(f"SELECT MIN(t) FROM {TABLE}").fetchone()[0]
        hi = conn.execute(f"SELECT MAX(t) FROM {TABLE}").fetchone()[0]
        with self.store.lock:
            for p in self.store.pending_track:
                lo = p[1] if lo is None else min(lo, p[1])
                hi = p[1] if hi is None else max(hi, p[1])
        return {"from": lo, "to": hi}

    # -- bounds --------------------------------------------------------------------------------------
    def enforce_cap(self) -> dict:
        """Delete the oldest points beyond the row cap, in half-hour slices."""
        conn = self.db.conn()
        total = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] or 0)
        deleted = 0
        if total > self.max_rows:
            row = conn.execute(f"SELECT t FROM {TABLE} ORDER BY t LIMIT 1 OFFSET ?",
                               (total - self.max_rows,)).fetchone()
            cutoff = int(row[0]) if row else None
            while cutoff is not None:
                lo = conn.execute(f"SELECT MIN(t) FROM {TABLE}").fetchone()[0]
                if lo is None or lo >= cutoff:
                    break
                with self.db.tx() as c:
                    cur = c.execute(f"DELETE FROM {TABLE} WHERE t < ?", (min(cutoff, int(lo) + 1800),))
                    deleted += max(0, cur.rowcount or 0)
        self.rows = total - deleted
        self.last_cap = {"at": self.now(), "rows": self.rows, "deleted": deleted}
        return dict(self.last_cap)


# ---------------------------------------------------------------------------------------------
# Routes (CONTRACT 15.4)
# ---------------------------------------------------------------------------------------------
def _epoch(req, name: str):
    value = req.arg(name, None, type=float)
    if value is None:
        return None
    if value < 0:
        raise HTTPError(400, "BAD_ARG", f'Query parameter "{name}" must be a time in epoch seconds.')
    return value / 1000.0 if value > 1e11 else value


def _window(req, now: float, default_span: float, max_span: float) -> tuple[float, float]:
    until = _epoch(req, "to")
    since = _epoch(req, "from")
    if until is None:
        until = now if since is None else max(since, min(now, since + default_span))
    if since is None:
        since = until - default_span
    if until < since:
        raise HTTPError(400, "BAD_ARG", 'Query parameter "to" must not be before "from".')
    if until - since > max_span:
        raise HTTPError(400, "BAD_ARG", f"Ask for at most {max_span / 3600:g} hours at a time.")
    return since, until


def _mmsi(req) -> int:
    raw = str(req.params.get("mmsi", "")).strip()
    if not (raw.isascii() and raw.isdigit() and len(raw) == 9):
        raise HTTPError(400, "BAD_ARG", "An MMSI is nine digits.")
    return int(raw)


def _r(value, nd: int):
    return None if value is None else round(value, nd)


def register_history(m, svc) -> ShipsHistory:
    m.key("SHIPS_HISTORY_MAX_ROWS", "Most vessel track points kept (72 hours, for cards and timeline replay; "
          "default 2,500,000); the oldest go first.", secret=False)
    hist = ShipsHistory(svc.store, m.store, now=svc.now,
                        max_rows=m.config.get_int("SHIPS_HISTORY_MAX_ROWS", MAX_ROWS_DEFAULT))
    svc.history = hist
    m.task("history-cap", hist.enforce_cap, every=CAP_EVERY_S, initial_delay=300.0)

    def meta(since, until) -> dict:
        return {"from": since, "to": until, "available": hist.extent(), "keep_h": round(hist.keep_s / 3600),
                "note": None if svc.has_global() else "Without an aisstream.io key the history covers Finnish "
                                                      "waters and the northern Baltic (Digitraffic)."}

    def as_object(r, cls: str) -> dict:
        return {"mmsi": str(r[0]), "t": r[1], "lat": round(r[2], 5), "lon": round(r[3], 5), "sog": _r(r[4], 1),
                "cog": _r(r[5], 1), "cls": cls}

    @m.route("GET", "/api/ships/history", activity=False)        # replay reads never speed up live polling
    def r_history(req):
        since, until = _window(req, svc.now(), 3600.0, WINDOW_MAX_S)
        bbox = req.bbox("bbox", None)
        limit = req.arg("limit", LIMIT_DEFAULT, type=int, min=1, max=LIMIT_MAX)
        fmt = req.arg("format", "objects", choices={"objects", "rows"})
        rows, truncated = hist.window(since, until, bbox, limit)
        idents = hist.idents({r[0] for r in rows})
        out = meta(since, until)
        out.update({"count": len(rows), "truncated": truncated, "vessels": len(idents),
                    "idents": {str(k): v for k, v in idents.items()}})
        if fmt == "rows":
            out["fields"] = FIELDS
            out["rows"] = [[str(r[0]), r[1], round(r[2], 5), round(r[3], 5), _r(r[4], 1), _r(r[5], 1),
                            idents[r[0]]["cls"]] for r in rows]
        else:
            out["positions"] = [as_object(r, idents[r[0]]["cls"]) for r in rows]
        return out

    @m.route("GET", "/api/ships/history/coverage", activity=False)
    def r_history_coverage(req):
        since, until = _window(req, svc.now(), hist.keep_s, hist.keep_s + 86400)
        out = meta(since, until)
        out["by_hour"] = hist.coverage(since, until)
        out["rows"] = hist.rows
        return out

    @m.route("GET", "/api/ships/history/{mmsi}", activity=False)
    def r_history_vessel(req):
        mmsi = _mmsi(req)
        since, until = _window(req, svc.now(), hist.keep_s, hist.keep_s + 86400)
        pts, truncated = hist.track(mmsi, since, until)
        ident = hist.idents([mmsi])[mmsi]
        out = meta(since, until)
        out.update({"mmsi": str(mmsi), "name": ident["name"], "cls": ident["cls"], "count": len(pts),
                    "truncated": truncated,
                    "positions": [as_object((mmsi, int(p[0]), p[1], p[2], p[3], p[4]), ident["cls"]) for p in pts]})
        if not pts:
            out["note"] = "No recorded positions for this vessel in that window."
        return out

    return hist
