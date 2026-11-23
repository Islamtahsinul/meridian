"""GPS interference, derived from the navigation accuracy that aircraft report about themselves.

Method (an independent implementation of the well-known approach): every airborne ADS-B aircraft seen with
fresh accuracy data is a sample in the 0.5 degree grid cell under it. A sample is *affected* when
its position accuracy category is degraded (NACp below 8, or NIC below 7 when NACp is missing), or
when the receiver network reports that its GPS stopped being usable (``gpsOkBefore``). Samples are
counted as distinct aircraft per cell per hour ("aircraft-hours") so one circling aircraft cannot
dominate a cell. Over a rolling window the affected share colours the cell:

    under 2 %: green;  2 to 10 %: amber;  10 % or more: red
    (cells need 12 aircraft-hours; amber needs 2 affected, red 3)

This measures what aircraft report, not radio signals: it cannot see spoofing that keeps the
reported accuracy high, and some older avionics report low accuracy all the time.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Iterable

from .const import (GPS_AMBER, GPS_AMBER_MIN_BAD, GPS_CELL_DEG, GPS_HISTORY_H, GPS_KEEP_H, GPS_MIN_SAMPLES,
                    GPS_NACP_BAD, GPS_NIC_BAD, GPS_RED, GPS_RED_MIN_BAD, GPS_WINDOW_H)

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights_gpsjam (
    hour INTEGER NOT NULL,
    row INTEGER NOT NULL,
    col INTEGER NOT NULL,
    total INTEGER NOT NULL,
    bad INTEGER NOT NULL,
    PRIMARY KEY (hour, row, col)
);
CREATE INDEX IF NOT EXISTS flights_gpsjam_cell ON flights_gpsjam(row, col);
"""

METHOD = ("Derived from the navigation accuracy aircraft report about themselves (ADS-B NACp and NIC), "
          "not a direct measurement of radio interference. A cell is coloured by the share of aircraft "
          "passing through it with degraded accuracy over the window: green under 2%, amber 2 to 10%, "
          "red 10% or more. Spoofing that keeps reported accuracy high is not detected.")

ROWS = int(round(180.0 / GPS_CELL_DEG))
COLS = int(round(360.0 / GPS_CELL_DEG))


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    row = min(ROWS - 1, max(0, int(math.floor((lat + 90.0) / GPS_CELL_DEG))))
    col = min(COLS - 1, max(0, int(math.floor((lon + 180.0) / GPS_CELL_DEG))))
    return row, col


def cell_origin(row: int, col: int, agg: int = 1) -> tuple[float, float, float]:
    """(south, west, size in degrees) of a (possibly aggregated) cell."""
    size = GPS_CELL_DEG * agg
    return -90.0 + row * size, -180.0 + col * size, size


def sample(o: dict, now: float) -> bool | None:
    """True (affected), False (normal) or None (not a usable sample) for one observation."""
    if o.get("gnd") or "lat" not in o or o.get("stale_pos"):
        return None
    alt = o.get("alt")
    if alt is not None and alt < 1000:
        return None
    pos_t = o.get("pos_t")
    if pos_t is None or now - pos_t > 30.0:
        return None
    ptype = o.get("ptype") or "adsb_icao"
    if ptype not in ("adsb_icao", "adsr_icao", "mlat"):
        return None                       # TIS-B, ADS-C, ground stations, OpenSky (no accuracy data)
    nacp = o.get("nac_p")
    nic = o.get("nic")
    version = o.get("version")
    adsb_capable = (version is not None and version >= 1) or nacp is not None
    if not adsb_capable:
        return None                       # Mode S only (MLAT reports nic 0 for these) or ADS-B v0
    if o.get("gps_ok_before"):
        return True
    if nacp is not None:
        return nacp < GPS_NACP_BAD
    if nic is not None:
        return nic < GPS_NIC_BAD
    return None


def level(total: int, bad: int, *, min_samples: int = GPS_MIN_SAMPLES) -> str:
    if total < min_samples:
        return "none"
    share = bad / total if total else 0.0
    if share >= GPS_RED and bad >= GPS_RED_MIN_BAD:
        return "red"
    if share >= GPS_AMBER and bad >= GPS_AMBER_MIN_BAD:
        return "amber"
    return "green"


class GpsJam:
    def __init__(self, store, *, clock=time.time, log=None):
        self.store = store
        self.clock = clock
        self.log = log
        self.lock = threading.Lock()
        self.buckets: dict[tuple[int, int, int], tuple[set, set]] = {}
        self.dirty: set[tuple[int, int, int]] = set()
        self._cache: dict[int, tuple[float, list]] = {}
        self.stats = {"samples": 0, "affected": 0, "flushed_rows": 0}
        store.script(SCHEMA)

    # -- collecting ------------------------------------------------------------------------------
    def observe(self, observations: Iterable[dict], now: float | None = None) -> tuple[int, int]:
        now = self.clock() if now is None else now
        n = bad = 0
        with self.lock:
            for o in observations:
                s = sample(o, now)
                if s is None:
                    continue
                hour = int(o["pos_t"] // 3600)
                row, col = cell_of(o["lat"], o["lon"])
                key = (hour, row, col)
                bucket = self.buckets.get(key)
                if bucket is None:
                    bucket = self.buckets[key] = (set(), set())
                seen, affected = bucket
                before = (len(seen), len(affected))
                seen.add(o["hex"])
                if s:
                    affected.add(o["hex"])
                    bad += 1
                if (len(seen), len(affected)) != before:
                    self.dirty.add(key)
                n += 1
            self.stats["samples"] += n
            self.stats["affected"] += bad
        return n, bad

    def flush(self, now: float | None = None) -> int:
        """Write changed hour buckets (MAX keeps partial counts after a restart). Buckets older than
        the previous hour are complete and leave memory."""
        now = self.clock() if now is None else now
        current = int(now // 3600)
        with self.lock:
            rows = [(k[0], k[1], k[2], len(self.buckets[k][0]), len(self.buckets[k][1]))
                    for k in self.dirty if k in self.buckets]
            self.dirty.clear()
            for key in [k for k in self.buckets if k[0] < current - 1]:
                del self.buckets[key]
        if rows:
            self.store.executemany(
                "INSERT INTO flights_gpsjam(hour, row, col, total, bad) VALUES(?,?,?,?,?) "
                "ON CONFLICT(hour, row, col) DO UPDATE SET total = MAX(total, excluded.total), "
                "bad = MAX(bad, excluded.bad)", rows)
            self.stats["flushed_rows"] += len(rows)
            self._cache.clear()
        return len(rows)

    def prune(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        self.store.execute("DELETE FROM flights_gpsjam WHERE hour < ?", (int(now // 3600) - GPS_HISTORY_H,))

    # -- reading ---------------------------------------------------------------------------------
    def _window(self, hours: int, now: float) -> list[tuple[int, int, int, int, int]]:
        hours = int(max(1, min(GPS_KEEP_H, hours)))
        cached = self._cache.get(hours)
        if cached and now - cached[0] < 60:
            return cached[1]
        since = int(now // 3600) - hours + 1
        # "+row, +col" keeps SQLite on the primary key's hour range: grouping by the bare columns makes it
        # scan the whole table (14 days) through the (row, col) index to save a sort
        rows = self.store.query(
            "SELECT row, col, SUM(total) AS total, SUM(bad) AS bad, "
            "MAX(CASE WHEN bad > 0 THEN hour END) AS last_bad FROM flights_gpsjam "
            "WHERE hour >= ? GROUP BY +row, +col", (since,))
        out = [(r["row"], r["col"], int(r["total"]), int(r["bad"]), r["last_bad"]) for r in rows]
        self._cache[hours] = (now, out)
        return out

    def cells(self, *, hours: int = GPS_WINDOW_H, bbox=None, agg: int = 1, now: float | None = None,
              min_samples: int = GPS_MIN_SAMPLES) -> list[dict]:
        """Cells with enough samples: [{row, col, lat0, lon0, size, total, bad, share, level, last_bad_t}]."""
        now = self.clock() if now is None else now
        rows = self._window(hours, now)
        if agg > 1:
            merged: dict[tuple[int, int], list] = {}
            for row, col, total, bad, last_bad in rows:
                key = (row // agg, col // agg)
                m = merged.get(key)
                if m is None:
                    merged[key] = [total, bad, last_bad]
                else:
                    m[0] += total
                    m[1] += bad
                    if last_bad is not None and (m[2] is None or last_bad > m[2]):
                        m[2] = last_bad
            rows = [(k[0], k[1], v[0], v[1], v[2]) for k, v in merged.items()]
        out = []
        for row, col, total, bad, last_bad in rows:
            lvl = level(total, bad, min_samples=min_samples * (1 if agg == 1 else 2))
            if lvl == "none":
                continue
            lat0, lon0, size = cell_origin(row, col, agg)
            if bbox is not None and not _cell_in_bbox(lat0, lon0, size, bbox):
                continue
            out.append({"row": row, "col": col, "lat0": lat0, "lon0": lon0, "size": size, "total": total,
                        "bad": bad, "share": round(bad / total, 4) if total else 0.0, "level": lvl,
                        "last_bad_t": (last_bad + 1) * 3600 if last_bad is not None else None})
        return out

    def cell_detail(self, lat: float, lon: float, *, hours: int = GPS_WINDOW_H, agg: int = 1,
                    now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        row, col = cell_of(lat, lon)
        hours = int(max(1, min(GPS_KEEP_H, hours)))
        since = int(now // 3600) - hours + 1
        if agg > 1:
            r0, c0 = (row // agg) * agg, (col // agg) * agg
            where = "row >= ? AND row < ? AND col >= ? AND col < ?"
            args = (r0, r0 + agg, c0, c0 + agg)
            lat0, lon0, size = cell_origin(row // agg, col // agg, agg)
        else:
            where = "row = ? AND col = ?"
            args = (row, col)
            lat0, lon0, size = cell_origin(row, col)
        hourly = self.store.query(
            f"SELECT hour, SUM(total) AS total, SUM(bad) AS bad FROM flights_gpsjam WHERE hour >= ? AND {where} "
            "GROUP BY hour ORDER BY hour", (since, *args))
        total = sum(int(h["total"]) for h in hourly)
        bad = sum(int(h["bad"]) for h in hourly)
        last_bad = max((h["hour"] for h in hourly if h["bad"]), default=None)
        return {"lat0": lat0, "lon0": lon0, "size": size, "hours": hours, "total": total, "bad": bad,
                "share": round(bad / total, 4) if total else None, "level": level(total, bad),
                "last_bad_t": (last_bad + 1) * 3600 if last_bad is not None else None,
                "hourly": [[int(h["hour"]) * 3600, int(h["total"]), int(h["bad"])] for h in hourly],
                "min_samples": GPS_MIN_SAMPLES, "method": METHOD}

    def summary(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        cells = self.cells(now=now)
        by = {"green": 0, "amber": 0, "red": 0}
        for c in cells:
            by[c["level"]] += 1
        with self.lock:
            pending = len(self.buckets)
        return {"cells": len(cells), "by_level": by, "buckets_in_memory": pending, **self.stats}


def _cell_in_bbox(lat0: float, lon0: float, size: float, bbox) -> bool:
    w, s, e, n = bbox
    if lat0 > n or lat0 + size < s:
        return False
    lon1 = lon0 + size
    if w <= e:
        return not (lon0 > e or lon1 < w)
    return lon1 >= w or lon0 <= e
