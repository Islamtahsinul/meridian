"""Stored briefings: one row per briefing in ``briefing_briefings`` (kept 60 days)."""

from __future__ import annotations

import json
import re
import time

from meridian.registry import json_bytes

TABLE = "briefing_briefings"
KEEP_DAYS = 60
LIST_MAX = 200
ID_RE = re.compile(r"^\d{8}T\d{6}Z(?:-\d{1,3})?$")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id TEXT PRIMARY KEY,
    time REAL NOT NULL,
    since REAL NOT NULL,
    until REAL NOT NULL,
    trigger TEXT NOT NULL,
    local_date TEXT NOT NULL,
    items INTEGER NOT NULL DEFAULT 0,
    headline TEXT NOT NULL DEFAULT '',
    has_ai INTEGER NOT NULL DEFAULT 0,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS {TABLE}_time ON {TABLE}(time);
"""


def valid_id(value: str) -> bool:
    return isinstance(value, str) and bool(ID_RE.fullmatch(value))


class BriefingStore:
    def __init__(self, store):
        self.store = store
        store.script(SCHEMA)

    def new_id(self, now: float) -> str:
        base = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        bid, n = base, 1
        while self.store.one(f"SELECT 1 AS x FROM {TABLE} WHERE id = ?", (bid,)) is not None:
            n += 1
            bid = f"{base}-{n}"
        return bid

    def save(self, doc: dict) -> None:
        headline = ""
        for sec in doc.get("sections") or []:
            if sec.get("id") == "headline" and sec.get("items"):
                headline = str(sec["items"][0].get("title") or "")[:200]
                break
        self.store.execute(
            f"INSERT INTO {TABLE} (id, time, since, until, trigger, local_date, items, headline, has_ai, doc) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET items = excluded.items, "
            f"headline = excluded.headline, has_ai = excluded.has_ai, doc = excluded.doc",
            (doc["id"], doc["time"], doc["period"]["from"], doc["period"]["to"], doc.get("trigger") or "manual",
             doc.get("local_date") or "", int(doc.get("stats", {}).get("items") or 0), headline,
             1 if (doc.get("ai") or {}).get("text") else 0, json_bytes(doc).decode("utf-8")))

    @staticmethod
    def _doc(row: dict | None) -> dict | None:
        if not row:
            return None
        try:
            doc = json.loads(row["doc"])
        except (ValueError, TypeError):
            return None
        return doc if isinstance(doc, dict) else None

    def get(self, bid: str) -> dict | None:
        if not valid_id(bid):
            return None
        return self._doc(self.store.one(f"SELECT doc FROM {TABLE} WHERE id = ?", (bid,)))

    def latest(self, trigger: str | None = None, before: float | None = None) -> dict | None:
        where, params = [], []
        if trigger:
            where.append("trigger = ?")
            params.append(trigger)
        if before is not None:
            where.append("time < ?")
            params.append(before)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        return self._doc(self.store.one(f"SELECT doc FROM {TABLE} {clause}ORDER BY time DESC LIMIT 1", params))

    def closest(self, target: float, lo: float, hi: float) -> dict | None:
        """The briefing made nearest to ``target`` within [lo, hi] (for "yesterday's briefing")."""
        return self._doc(self.store.one(f"SELECT doc FROM {TABLE} WHERE time BETWEEN ? AND ? "
                                        f"ORDER BY ABS(time - ?) LIMIT 1", (lo, hi, target)))

    def latest_meta(self) -> dict | None:
        return self.store.one(f"SELECT id, time, trigger, local_date, items, headline, has_ai FROM {TABLE} "
                              f"ORDER BY time DESC LIMIT 1")

    def last_scheduled_date(self) -> str | None:
        row = self.store.one(f"SELECT local_date FROM {TABLE} WHERE trigger = 'scheduled' ORDER BY time DESC LIMIT 1")
        return row["local_date"] if row else None

    def list(self, limit: int = 60) -> tuple[list[dict], int]:
        rows = self.store.query(f"SELECT id, time, since, until, trigger, local_date, items, headline, has_ai "
                                f"FROM {TABLE} ORDER BY time DESC LIMIT ?", (max(1, min(LIST_MAX, int(limit))),))
        total = self.store.scalar(f"SELECT COUNT(*) FROM {TABLE}", default=0)
        out = []
        for r in rows:
            out.append({"id": r["id"], "time": r["time"], "period": {"from": r["since"], "to": r["until"]},
                        "trigger": r["trigger"], "local_date": r["local_date"], "items": r["items"],
                        "headline": r["headline"], "ai": bool(r["has_ai"])})
        return out, int(total)

    def prune(self, now: float, keep_days: float = KEEP_DAYS) -> int:
        cur = self.store.execute(f"DELETE FROM {TABLE} WHERE time < ?", (now - keep_days * 86400.0,))
        return max(0, cur.rowcount or 0)

    def count(self) -> int:
        return int(self.store.scalar(f"SELECT COUNT(*) FROM {TABLE}", default=0))
