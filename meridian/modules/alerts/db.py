"""Alerts storage: watchlists, rules, dedupe records, fired alerts and settings (``alerts_`` tables).

Watchlists and rules are the user's data and are kept until deleted. Dedupe records go 35 days after
they last matched (the event history keeps 30), fired alerts after 60 days (at most 20,000).
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

from .areas import Area
from . import rules as R

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts_watchlists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    area TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#6cb6ff',
    enabled INTEGER NOT NULL DEFAULT 1,
    show_on_map INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts_rules (
    id INTEGER PRIMARY KEY,
    watchlist_id INTEGER NOT NULL REFERENCES alerts_watchlists(id) ON DELETE CASCADE,
    preset TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    watch_from REAL NOT NULL,
    baseline_at REAL,
    checked_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (watchlist_id, preset)
);
CREATE TABLE IF NOT EXISTS alerts_seen (
    rule_id INTEGER NOT NULL REFERENCES alerts_rules(id) ON DELETE CASCADE,
    module TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    severity REAL NOT NULL,
    event_time REAL,
    level INTEGER NOT NULL DEFAULT 2,
    alerted INTEGER NOT NULL DEFAULT 0,
    first_matched REAL NOT NULL,
    last_matched REAL NOT NULL,
    PRIMARY KEY (rule_id, module, kind, ref)
);
CREATE INDEX IF NOT EXISTS alerts_seen_last ON alerts_seen(last_matched);
CREATE TABLE IF NOT EXISTS alerts_alerts (
    id INTEGER PRIMARY KEY,
    watchlist_id INTEGER,
    rule_id INTEGER,
    area_name TEXT NOT NULL DEFAULT '',
    preset TEXT NOT NULL DEFAULT '',
    rule_text TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    module TEXT,
    kind TEXT,
    ref TEXT,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL DEFAULT '',
    why TEXT NOT NULL DEFAULT '',
    severity REAL NOT NULL DEFAULT 0,
    event_time REAL,
    lat REAL,
    lon REAL,
    entity TEXT,
    count INTEGER NOT NULL DEFAULT 1,
    digest_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    read_at REAL,
    cleared_at REAL
);
CREATE INDEX IF NOT EXISTS alerts_alerts_created ON alerts_alerts(created_at);
CREATE INDEX IF NOT EXISTS alerts_alerts_wl ON alerts_alerts(watchlist_id, created_at);
CREATE INDEX IF NOT EXISTS alerts_alerts_digest ON alerts_alerts(digest_id);
CREATE TABLE IF NOT EXISTS alerts_settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

MAX_WATCHLISTS = 100
NAME_MAX = 80
TITLE_MAX = 200
WHY_MAX = 300
SEEN_KEEP_S = 35 * 86400.0
ALERTS_KEEP_S = 60 * 86400.0
ALERTS_MAX_ROWS = 20000
COLORS = ("#6cb6ff", "#ffb547", "#5fd38d", "#ff5d52")      # the alert colours (CONTRACT.md 10.5)
_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_TIME = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "notify": {"on": True, "min_severity": 0},
    "quiet_hours": {"on": False, "start": "22:00", "end": "07:00", "allow_extreme": True},
    "rate_limit": 10,
    "ai_share": False,                  # opt-in: watchlist names and places can be personal
}
RATE_CHOICES = (5, 10, 20, 50)


class InputError(ValueError):
    """Bad input from the browser (the message is shown to the user)."""


def loads(text: Any, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def num(value: Any) -> Any:
    """7.0 -> 7 for JSON output."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
        return int(value)
    return value


def setup(store) -> None:
    store.script(SCHEMA)


# ---------------------------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------------------------
def clean_name(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise InputError("The name must be text.")
    text = " ".join(value.split())[:NAME_MAX]
    return text or default


def clean_color(value: Any, default: str = COLORS[0]) -> str:
    if value is None or value == "":
        return default
    if not isinstance(value, str) or not _COLOR.match(value.strip()):
        raise InputError("The colour must be a hex colour such as #6cb6ff.")
    return value.strip().lower()


def clean_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise InputError(f"{name} must be true or false.")
    return value


# ---------------------------------------------------------------------------------------------
# watchlists and rules
# ---------------------------------------------------------------------------------------------
def _rule_json(r: dict) -> dict:
    try:
        preset = R.get(r["preset"])
    except R.RuleError:
        return {"id": r["id"], "preset": r["preset"], "label": r["preset"], "summary": "Unknown rule",
                "params": {}, "enabled": False, "available": False}
    params = loads(r["params"], {}) or {}
    try:
        params = preset.clean_params(params)
    except R.RuleError:
        params = preset.clean_params({})
    return {"id": r["id"], "preset": preset.id, "label": preset.label, "summary": preset.phrase(params),
            "params": params, "enabled": bool(r["enabled"]), "watch_from": num(r["watch_from"]),
            "checked_at": num(r["checked_at"]), "baseline_done": r["baseline_at"] is not None}


def watchlist_json(w: dict, rules: list[dict], stats: dict | None = None) -> dict:
    stored = loads(w["area"], {}) or {}
    try:
        area = Area.from_json(stored).to_json()      # descriptions follow the current wording
    except (TypeError, ValueError, KeyError, IndexError):
        area = stored
    st = stats or {}
    return {"id": w["id"], "name": w["name"], "area": area, "color": w["color"], "enabled": bool(w["enabled"]),
            "show_on_map": bool(w["show_on_map"]), "created_at": num(w["created_at"]),
            "updated_at": num(w["updated_at"]), "rules": [_rule_json(r) for r in rules],
            "unread": st.get("unread", 0), "last_24h": st.get("last_24h", 0), "last_alert_at": st.get("last_at")}


def _stats(store, now: float, wid: int | None = None) -> dict[int, dict]:
    where = "WHERE watchlist_id = ?" if wid is not None else "WHERE watchlist_id IS NOT NULL"
    params: list[Any] = [now - 86400.0]
    if wid is not None:
        params.append(wid)
    rows = store.query(
        "SELECT watchlist_id AS w, "
        "SUM(CASE WHEN read_at IS NULL AND cleared_at IS NULL AND digest_id IS NULL THEN 1 ELSE 0 END) AS unread, "
        "SUM(CASE WHEN created_at >= ? AND reason != 'digest' THEN 1 ELSE 0 END) AS last_24h, "
        f"MAX(created_at) AS last_at FROM alerts_alerts {where} GROUP BY watchlist_id", params)
    return {r["w"]: {"unread": r["unread"] or 0, "last_24h": r["last_24h"] or 0, "last_at": num(r["last_at"])}
            for r in rows}


def list_watchlists(store, now: float) -> list[dict]:
    ws = store.query("SELECT * FROM alerts_watchlists ORDER BY created_at, id")
    rules = store.query("SELECT * FROM alerts_rules ORDER BY id")
    by_w: dict[int, list] = {}
    for r in rules:
        by_w.setdefault(r["watchlist_id"], []).append(r)
    stats = _stats(store, now)
    order = {p: i for i, p in enumerate(R.ORDER)}
    return [watchlist_json(w, sorted(by_w.get(w["id"], []), key=lambda r: order.get(r["preset"], 99)),
                           stats.get(w["id"])) for w in ws]


def get_watchlist(store, wid: int, now: float) -> dict | None:
    w = store.one("SELECT * FROM alerts_watchlists WHERE id = ?", (wid,))
    if w is None:
        return None
    order = {p: i for i, p in enumerate(R.ORDER)}
    rules = sorted(store.query("SELECT * FROM alerts_rules WHERE watchlist_id = ?", (wid,)),
                   key=lambda r: order.get(r["preset"], 99))
    return watchlist_json(w, rules, _stats(store, now, wid).get(wid))


def count_watchlists(store) -> int:
    return int(store.scalar("SELECT COUNT(*) FROM alerts_watchlists", default=0) or 0)


def clean_rules(value: Any) -> list[tuple[str, dict, bool]]:
    """[{preset, params?, enabled?}] -> [(preset, params, enabled)] with one entry per preset."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise InputError("rules must be a list.")
    if len(value) > len(R.ORDER):
        raise InputError("Too many rules.")
    out: dict[str, tuple[str, dict, bool]] = {}
    for item in value:
        if isinstance(item, str):
            item = {"preset": item}
        if not isinstance(item, dict):
            raise InputError("Each rule must be an object with a preset.")
        try:
            preset = R.get(item.get("preset"))
            params = preset.clean_params(item.get("params"))
        except R.RuleError as err:
            raise InputError(_sentence(str(err))) from None
        enabled = item.get("enabled", True)
        out[preset.id] = (preset.id, params, clean_bool(enabled, "enabled"))
    return list(out.values())


def create_watchlist(store, *, name: str, area: Area, color: str | None, rules: list, now: float) -> int:
    if count_watchlists(store) >= MAX_WATCHLISTS:
        raise InputError(f"You can keep at most {MAX_WATCHLISTS} watchlists.")
    if color is None:
        color = COLORS[count_watchlists(store) % len(COLORS)]
    with store.tx() as c:
        cur = c.execute("INSERT INTO alerts_watchlists(name, area, color, enabled, show_on_map, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?)", (name, dumps(area.to_json()), color, 1, 1, now, now))
        wid = cur.lastrowid
        for preset, params, enabled in rules:
            c.execute("INSERT INTO alerts_rules(watchlist_id, preset, params, enabled, watch_from, created_at, updated_at) "
                      "VALUES(?,?,?,?,?,?,?)", (wid, preset, dumps(params), int(enabled), now, now, now))
    return int(wid)


def _rebaseline(c, where: str, params: Iterable[Any], now: float) -> None:
    """Rules whose area, settings or switch changed start again from what already matches, so the
    change does not flood the inbox with events that were there all along."""
    c.execute(f"UPDATE alerts_rules SET baseline_at = NULL, checked_at = NULL, watch_from = ?, updated_at = ? "
              f"WHERE {where}", (now, now, *params))


def update_watchlist(store, wid: int, *, now: float, name: str | None = None, area: Area | None = None,
                     color: str | None = None, enabled: bool | None = None, show_on_map: bool | None = None) -> bool:
    w = store.one("SELECT * FROM alerts_watchlists WHERE id = ?", (wid,))
    if w is None:
        return False
    sets, params = ["updated_at = ?"], [now]
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if area is not None:
        sets.append("area = ?")
        params.append(dumps(area.to_json()))
    if color is not None:
        sets.append("color = ?")
        params.append(color)
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(int(enabled))
    if show_on_map is not None:
        sets.append("show_on_map = ?")
        params.append(int(show_on_map))
    with store.tx() as c:
        c.execute(f"UPDATE alerts_watchlists SET {', '.join(sets)} WHERE id = ?", (*params, wid))
        if area is not None or (enabled and not w["enabled"]):
            _rebaseline(c, "watchlist_id = ?", (wid,), now)
        if name is not None:
            c.execute("UPDATE alerts_alerts SET area_name = ? WHERE watchlist_id = ?", (name, wid))
    return True


def delete_watchlist(store, wid: int) -> int:
    """Delete a watchlist, its rules, dedupe records and alerts. Returns the number of alerts removed."""
    with store.tx() as c:
        n = c.execute("DELETE FROM alerts_alerts WHERE watchlist_id = ?", (wid,)).rowcount or 0
        c.execute("DELETE FROM alerts_seen WHERE rule_id IN (SELECT id FROM alerts_rules WHERE watchlist_id = ?)", (wid,))
        c.execute("DELETE FROM alerts_rules WHERE watchlist_id = ?", (wid,))
        c.execute("DELETE FROM alerts_watchlists WHERE id = ?", (wid,))
    return n


def set_rule(store, wid: int, preset: str, *, params: dict | None, enabled: bool | None, now: float) -> int:
    """Add or change one preset rule on a watchlist. Returns the rule id."""
    if store.one("SELECT id FROM alerts_watchlists WHERE id = ?", (wid,)) is None:
        raise LookupError("watchlist")
    p = R.get(preset)
    row = store.one("SELECT * FROM alerts_rules WHERE watchlist_id = ? AND preset = ?", (wid, p.id))
    with store.tx() as c:
        if row is None:
            clean = p.clean_params(params)
            cur = c.execute("INSERT INTO alerts_rules(watchlist_id, preset, params, enabled, watch_from, created_at, "
                            "updated_at) VALUES(?,?,?,?,?,?,?)",
                            (wid, p.id, dumps(clean), int(True if enabled is None else enabled), now, now, now))
            return int(cur.lastrowid)
        old = loads(row["params"], {}) or {}
        clean = p.clean_params(params) if params is not None else p.clean_params(old) if _valid(p, old) else p.clean_params({})
        new_enabled = bool(row["enabled"]) if enabled is None else enabled
        c.execute("UPDATE alerts_rules SET params = ?, enabled = ?, updated_at = ? WHERE id = ?",
                  (dumps(clean), int(new_enabled), now, row["id"]))
        if clean != old or (new_enabled and not row["enabled"]):
            _rebaseline(c, "id = ?", (row["id"],), now)
        return int(row["id"])


def _valid(p, params) -> bool:
    try:
        p.clean_params(params)
        return True
    except R.RuleError:
        return False


def delete_rule(store, rule_id: int) -> bool:
    with store.tx() as c:
        c.execute("DELETE FROM alerts_seen WHERE rule_id = ?", (rule_id,))
        return (c.execute("DELETE FROM alerts_rules WHERE id = ?", (rule_id,)).rowcount or 0) > 0


def active_rules(store) -> list[dict]:
    """Enabled rules of enabled watchlists, with their watchlist's fields (w_*)."""
    return store.query(
        "SELECT r.*, w.name AS w_name, w.area AS w_area, w.updated_at AS w_updated_at "
        "FROM alerts_rules r JOIN alerts_watchlists w ON w.id = r.watchlist_id "
        "WHERE r.enabled = 1 AND w.enabled = 1 ORDER BY w.id, r.id")


# ---------------------------------------------------------------------------------------------
# dedupe records
# ---------------------------------------------------------------------------------------------
def seen_for(store, rule_id: int, refs: list[str]) -> dict[tuple[str, str, str], dict]:
    out: dict[tuple[str, str, str], dict] = {}
    uniq = sorted(set(refs))
    for i in range(0, len(uniq), 400):
        chunk = uniq[i:i + 400]
        rows = store.query(f"SELECT * FROM alerts_seen WHERE rule_id = ? AND ref IN ({','.join('?' * len(chunk))})",
                           (rule_id, *chunk))
        for r in rows:
            out[(r["module"], r["kind"], r["ref"])] = r
    return out


UPSERT_SEEN = """
INSERT INTO alerts_seen(rule_id, module, kind, ref, severity, event_time, level, alerted, first_matched, last_matched)
VALUES(?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(rule_id, module, kind, ref) DO UPDATE SET severity = excluded.severity, event_time = excluded.event_time,
    level = excluded.level, alerted = MAX(alerts_seen.alerted, excluded.alerted), last_matched = excluded.last_matched
"""


# ---------------------------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------------------------
INSERT_ALERT = """
INSERT INTO alerts_alerts(watchlist_id, rule_id, area_name, preset, rule_text, reason, module, kind, ref, title,
    subtitle, why, severity, event_time, lat, lon, entity, count, digest_id, created_at, updated_at)
VALUES(:watchlist_id, :rule_id, :area_name, :preset, :rule_text, :reason, :module, :kind, :ref, :title, :subtitle,
    :why, :severity, :event_time, :lat, :lon, :entity, :count, :digest_id, :created_at, :updated_at)
"""

ALERT_DEFAULTS = {"watchlist_id": None, "rule_id": None, "area_name": "", "preset": "", "rule_text": "",
                  "reason": "new", "module": None, "kind": None, "ref": None, "title": "", "subtitle": "", "why": "",
                  "severity": 0.0, "event_time": None, "lat": None, "lon": None, "entity": None, "count": 1,
                  "digest_id": None}


def insert_alert(c, now: float, **fields) -> int:
    row = dict(ALERT_DEFAULTS)
    row.update(fields)
    row["title"] = str(row["title"] or "Alert")[:TITLE_MAX]
    row["subtitle"] = str(row["subtitle"] or "")[:TITLE_MAX]
    row["why"] = str(row["why"] or "")[:WHY_MAX]
    if isinstance(row["entity"], dict):
        row["entity"] = dumps(row["entity"])
    row["created_at"] = now
    row["updated_at"] = now
    return int(c.execute(INSERT_ALERT, row).lastrowid)


def alert_json(r: dict) -> dict:
    entity = loads(r.get("entity"), None)
    return {"id": r["id"], "time": num(round(r["created_at"], 3)), "updated_at": r["updated_at"],
            "area": r["area_name"], "rule": r["rule_text"], "watchlist_id": r["watchlist_id"], "rule_id": r["rule_id"],
            "preset": r["preset"] or None, "reason": r["reason"], "title": r["title"], "subtitle": r["subtitle"],
            "why": r["why"], "severity": num(r["severity"]), "event_time": num(r["event_time"]),
            "module": r["module"], "kind": r["kind"], "ref": r["ref"], "lat": r["lat"], "lon": r["lon"],
            "entity": entity if isinstance(entity, dict) else None, "count": r["count"],
            "digest": r["reason"] == "digest", "folded": r["digest_id"] is not None,
            "read": r["read_at"] is not None, "cleared": r["cleared_at"] is not None}


_VISIBLE = "digest_id IS NULL AND cleared_at IS NULL"


def inbox(store, *, limit: int = 50, before: int | None = None, before_t: float | None = None, after: int | None = None,
          unread: bool = False, digest: int | None = None) -> list[dict]:
    """Inbox items, most recently updated first (a growing digest rises). Page with the last item's
    (``updated_at``, ``id``) as (``before_t``, ``before``); ``after`` lists newer ids oldest first."""
    if digest is not None:
        rows = store.query("SELECT * FROM alerts_alerts WHERE digest_id = ? ORDER BY severity DESC, event_time DESC, "
                           "id DESC LIMIT ?",
                           (digest, limit))
        return [alert_json(r) for r in rows]
    where, params = [_VISIBLE], []
    if unread:
        where.append("read_at IS NULL")
    if before is not None and before_t is not None:
        where.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
        params.extend([before_t, before_t, before])
    elif before is not None:
        where.append("id < ?")
        params.append(before)
    if after is not None:
        where.append("id > ?")
        params.append(after)
    order = "id ASC" if after is not None else "updated_at DESC, id DESC"
    rows = store.query(f"SELECT * FROM alerts_alerts WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
                       (*params, limit))
    return [alert_json(r) for r in rows]


def counts(store, now: float) -> dict:
    row = store.one(
        f"SELECT COUNT(*) AS inbox, SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread, MAX(id) AS latest_id, "
        f"MAX(updated_at) AS latest_at FROM alerts_alerts WHERE {_VISIBLE}") or {}
    day = store.scalar("SELECT COUNT(*) FROM alerts_alerts WHERE created_at >= ? AND reason NOT IN ('digest', 'test')",
                       (now - 86400.0,), default=0)
    by_w = {str(k): {"unread": v["unread"], "last_24h": v["last_24h"]} for k, v in _stats(store, now).items()}
    return {"unread": row.get("unread") or 0, "inbox": row.get("inbox") or 0, "latest_id": row.get("latest_id") or 0,
            "latest_at": num(row.get("latest_at")), "last_24h": day or 0, "watchlists": count_watchlists(store),
            "by_watchlist": by_w}


def mark(store, ids: list[int], *, read: bool, now: float) -> int:
    if not ids:
        return 0
    n = 0
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        value = now if read else None
        with store.tx() as c:
            n += c.execute(f"UPDATE alerts_alerts SET read_at = ? WHERE id IN ({marks})", (value, *chunk)).rowcount or 0
            # a digest's held-back alerts follow it
            c.execute(f"UPDATE alerts_alerts SET read_at = ? WHERE digest_id IN ({marks})", (value, *chunk))
    return n


def read_all(store, now: float) -> int:
    with store.tx() as c:
        return c.execute("UPDATE alerts_alerts SET read_at = ? WHERE read_at IS NULL", (now,)).rowcount or 0


def clear(store, now: float, *, read_only: bool = False) -> int:
    """Hide alerts from the inbox. They stay in /api/alerts/recent (the briefing reads it) until pruned."""
    extra = " AND read_at IS NOT NULL" if read_only else ""
    with store.tx() as c:
        n = c.execute(f"UPDATE alerts_alerts SET cleared_at = ?, read_at = COALESCE(read_at, ?) "
                      f"WHERE cleared_at IS NULL AND digest_id IS NULL{extra}", (now, now)).rowcount or 0
        c.execute("UPDATE alerts_alerts SET cleared_at = ?, read_at = COALESCE(read_at, ?) WHERE cleared_at IS NULL "
                  "AND digest_id IN (SELECT id FROM alerts_alerts WHERE cleared_at IS NOT NULL)", (now, now))
    return n


def recent(store, *, since: float, limit: int, digests: bool = False) -> list[dict]:
    """Fired alerts newest first (CONTRACT.md 15.4). Held-back alerts are included (``folded``); digests,
    which only summarise them, and test alerts are left out unless asked for."""
    skip = "('test')" if digests else "('test', 'digest')"
    rows = store.query(f"SELECT * FROM alerts_alerts WHERE created_at >= ? AND reason NOT IN {skip} "
                       f"ORDER BY created_at DESC, id DESC LIMIT ?", (since, limit))
    return [alert_json(r) for r in rows]


def delivered_since(c, wid: int, since: float) -> int:
    return int(c.execute("SELECT COUNT(*) FROM alerts_alerts WHERE watchlist_id = ? AND created_at >= ? "
                         "AND digest_id IS NULL AND reason NOT IN ('digest', 'test')", (wid, since)).fetchone()[0] or 0)


def open_digest(c, wid: int, since: float) -> dict | None:
    row = c.execute("SELECT * FROM alerts_alerts WHERE watchlist_id = ? AND reason = 'digest' AND created_at >= ? "
                    "AND cleared_at IS NULL ORDER BY id DESC LIMIT 1", (wid, since)).fetchone()
    return dict(row) if row is not None else None


def prune(store, now: float) -> dict:
    with store.tx() as c:
        seen = c.execute("DELETE FROM alerts_seen WHERE last_matched < ?", (now - SEEN_KEEP_S,)).rowcount or 0
        old = c.execute("DELETE FROM alerts_alerts WHERE created_at < ?", (now - ALERTS_KEEP_S,)).rowcount or 0
    total = int(store.scalar("SELECT COUNT(*) FROM alerts_alerts", default=0) or 0)
    capped = 0
    if total > ALERTS_MAX_ROWS:
        with store.tx() as c:
            capped = c.execute("DELETE FROM alerts_alerts WHERE id IN (SELECT id FROM alerts_alerts "
                               "ORDER BY created_at, id LIMIT ?)", (total - ALERTS_MAX_ROWS,)).rowcount or 0
    return {"seen": seen, "alerts": old + capped}


# ---------------------------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------------------------
def settings(store) -> dict:
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    for r in store.query("SELECT k, v FROM alerts_settings"):
        value = loads(r["v"], None)
        if r["k"] in out and value is not None:
            if isinstance(out[r["k"]], dict) and isinstance(value, dict):
                out[r["k"]].update({k: v for k, v in value.items() if k in out[r["k"]]})
            elif not isinstance(out[r["k"]], dict):
                out[r["k"]] = value
    return out


def clean_settings(body: dict) -> dict:
    """Validate a partial settings change; unknown keys are refused."""
    out: dict[str, Any] = {}
    for key in body:
        if key not in DEFAULT_SETTINGS:
            raise InputError(f'Unknown setting "{key}".')
    if "notify" in body:
        v = body["notify"]
        if not isinstance(v, dict) or any(k not in ("on", "min_severity") for k in v):
            raise InputError("notify must be {on, min_severity}.")
        n: dict[str, Any] = {}
        if "on" in v:
            n["on"] = clean_bool(v["on"], "notify.on")
        if "min_severity" in v:
            s = v["min_severity"]
            if isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(float(s)) or not 0 <= s <= 10:
                raise InputError("notify.min_severity must be a number from 0 to 10.")
            n["min_severity"] = num(float(s))
        out["notify"] = n
    if "quiet_hours" in body:
        v = body["quiet_hours"]
        if not isinstance(v, dict) or any(k not in ("on", "start", "end", "allow_extreme") for k in v):
            raise InputError("quiet_hours must be {on, start, end, allow_extreme}.")
        q: dict[str, Any] = {}
        for k in ("on", "allow_extreme"):
            if k in v:
                q[k] = clean_bool(v[k], f"quiet_hours.{k}")
        for k in ("start", "end"):
            if k in v:
                if not isinstance(v[k], str) or not _TIME.match(v[k].strip()):
                    raise InputError(f"quiet_hours.{k} must be a time such as 22:00.")
                q[k] = v[k].strip()
        out["quiet_hours"] = q
    if "rate_limit" in body:
        v = body["rate_limit"]
        if isinstance(v, bool) or not isinstance(v, int) or v not in RATE_CHOICES:
            raise InputError(f"rate_limit must be one of {', '.join(str(x) for x in RATE_CHOICES)}.")
        out["rate_limit"] = v
    if "ai_share" in body:
        out["ai_share"] = clean_bool(body["ai_share"], "ai_share")
    return out


def save_settings(store, change: dict, now: float) -> dict:
    current = settings(store)
    with store.tx() as c:
        for key, value in change.items():
            if isinstance(current.get(key), dict):
                merged = dict(current[key])
                merged.update(value)
                value = merged
            c.execute("INSERT INTO alerts_settings(k, v, updated_at) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET "
                      "v = excluded.v, updated_at = excluded.updated_at", (key, dumps(value), now))
    return settings(store)


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith(".") else text + "."
