"""Workspace storage: ``workspace_saved`` (named map states) and ``workspace_places`` (My places).

Both are the user's own data and are kept until the user deletes them. Names of saved workspaces are
unique without regard to case, which is what lets an import replace a workspace of the same name.
"""

from __future__ import annotations

import json
from typing import Any

from .model import InputError, clean_name, clean_place, clean_state, summary

MAX_WORKSPACES = 100
MAX_PLACES = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_saved (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS workspace_saved_name ON workspace_saved(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS workspace_places (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'other',
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_km REAL NOT NULL DEFAULT 25,
    zoom REAL NOT NULL DEFAULT 11,
    notes TEXT NOT NULL DEFAULT '',
    alerts_watchlist_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class NameTaken(InputError):
    """Another saved workspace already has this name."""


def setup(store) -> None:
    store.script(SCHEMA)


def _row(r: dict) -> dict:
    try:
        state = json.loads(r["state"])
    except (TypeError, ValueError):
        state = {"v": 1}
    return {"id": r["id"], "name": r["name"], "state": state, "summary": summary(state),
            "created_at": r["created_at"], "updated_at": r["updated_at"], "builtin": False}


# ---------------------------------------------------------------------------------------------
# saved workspaces
# ---------------------------------------------------------------------------------------------
def list_workspaces(store) -> list[dict]:
    rows = store.query("SELECT * FROM workspace_saved ORDER BY updated_at DESC, id DESC")
    return [_row(r) for r in rows]


def get_workspace(store, wid: int) -> dict | None:
    r = store.one("SELECT * FROM workspace_saved WHERE id = ?", (wid,))
    return _row(r) if r else None


def find_by_name(store, name: str) -> dict | None:
    r = store.one("SELECT * FROM workspace_saved WHERE name = ? COLLATE NOCASE", (name,))
    return _row(r) if r else None


def count_workspaces(store) -> int:
    return int(store.scalar("SELECT COUNT(*) FROM workspace_saved", default=0))


def create_workspace(store, name: Any, state: Any, now: float) -> int:
    name = clean_name(name)
    state = clean_state(state)
    with store.tx() as c:
        if c.execute("SELECT COUNT(*) FROM workspace_saved").fetchone()[0] >= MAX_WORKSPACES:
            raise InputError(f"You can keep at most {MAX_WORKSPACES} workspaces. Delete one first.")
        if c.execute("SELECT 1 FROM workspace_saved WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
            raise NameTaken(f'There is already a workspace called "{name}".')
        cur = c.execute("INSERT INTO workspace_saved(name, state, created_at, updated_at) VALUES(?,?,?,?)",
                        (name, json.dumps(state, separators=(",", ":")), now, now))
        return int(cur.lastrowid)


def update_workspace(store, wid: int, now: float, *, name: Any = None, state: Any = None) -> bool:
    sets, params = [], []
    if name is not None:
        name = clean_name(name)
        sets.append("name = ?")
        params.append(name)
    if state is not None:
        sets.append("state = ?")
        params.append(json.dumps(clean_state(state), separators=(",", ":")))
    if not sets:
        raise InputError("Nothing to change: send a name or a state.")
    sets.append("updated_at = ?")
    params.extend([now, wid])
    with store.tx() as c:
        if name is not None and c.execute("SELECT 1 FROM workspace_saved WHERE name = ? COLLATE NOCASE AND id != ?",
                                          (name, wid)).fetchone():
            raise NameTaken(f'There is already a workspace called "{name}".')
        cur = c.execute(f"UPDATE workspace_saved SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def delete_workspace(store, wid: int) -> bool:
    return store.execute("DELETE FROM workspace_saved WHERE id = ?", (wid,)).rowcount > 0


def import_workspaces(store, items: Any, now: float) -> dict:
    """Merge a list of {name, state}: same name (any case) replaces, otherwise a new workspace.
    Items that can't be used are skipped with a reason; nothing is half-written."""
    if not isinstance(items, list):
        raise InputError("workspaces must be a list.")
    if len(items) > MAX_WORKSPACES:
        raise InputError(f"An import can hold at most {MAX_WORKSPACES} workspaces.")
    plan: list[tuple[str, dict]] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        try:
            if not isinstance(item, dict):
                raise InputError("not an object")
            name = clean_name(item.get("name"))
            state = clean_state(item.get("state"))
        except InputError as err:
            skipped.append({"index": i, "reason": str(err).rstrip(".")})
            continue
        key = name.casefold()
        if key in seen:
            skipped.append({"index": i, "reason": f'"{name}" appears twice in the file'})
            continue
        seen.add(key)
        plan.append((name, state))
    created = replaced = 0
    with store.tx() as c:
        count = c.execute("SELECT COUNT(*) FROM workspace_saved").fetchone()[0]
        for name, state in plan:
            text = json.dumps(state, separators=(",", ":"))
            row = c.execute("SELECT id FROM workspace_saved WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if row:
                c.execute("UPDATE workspace_saved SET state = ?, updated_at = ? WHERE id = ?", (text, now, row[0]))
                replaced += 1
            elif count >= MAX_WORKSPACES:
                skipped.append({"name": name, "reason": f"you already keep {MAX_WORKSPACES} workspaces"})
            else:
                c.execute("INSERT INTO workspace_saved(name, state, created_at, updated_at) VALUES(?,?,?,?)",
                          (name, text, now, now))
                count += 1
                created += 1
    return {"created": created, "replaced": replaced, "skipped": skipped}


# ---------------------------------------------------------------------------------------------
# places
# ---------------------------------------------------------------------------------------------
_PLACE_COLS = ("name", "type", "lat", "lon", "radius_km", "zoom", "notes")


def _place(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "type": r["type"], "lat": r["lat"], "lon": r["lon"],
            "radius_km": r["radius_km"], "zoom": r["zoom"], "notes": r["notes"],
            "alerts_watchlist_id": r["alerts_watchlist_id"], "created_at": r["created_at"], "updated_at": r["updated_at"]}


def list_places(store) -> list[dict]:
    return [_place(r) for r in store.query("SELECT * FROM workspace_places ORDER BY name COLLATE NOCASE, id")]


def get_place(store, pid: int) -> dict | None:
    r = store.one("SELECT * FROM workspace_places WHERE id = ?", (pid,))
    return _place(r) if r else None


def count_places(store) -> int:
    return int(store.scalar("SELECT COUNT(*) FROM workspace_places", default=0))


def create_place(store, raw: Any, now: float) -> int:
    p = clean_place(raw)
    with store.tx() as c:
        if c.execute("SELECT COUNT(*) FROM workspace_places").fetchone()[0] >= MAX_PLACES:
            raise InputError(f"You can keep at most {MAX_PLACES} places. Delete one first.")
        cur = c.execute("INSERT INTO workspace_places(name, type, lat, lon, radius_km, zoom, notes, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (p["name"], p["type"], p["lat"], p["lon"], p["radius_km"], p["zoom"], p.get("notes", ""), now, now))
        return int(cur.lastrowid)


def update_place(store, pid: int, raw: Any, now: float) -> bool:
    p = clean_place(raw, partial=True)
    sets = [f"{k} = ?" for k in _PLACE_COLS if k in p]
    if not sets:
        raise InputError("Nothing to change.")
    params = [p[k] for k in _PLACE_COLS if k in p] + [now, pid]
    cur = store.execute(f"UPDATE workspace_places SET {', '.join(sets)}, updated_at = ? WHERE id = ?", params)
    return cur.rowcount > 0


def set_place_watchlist(store, pid: int, watchlist_id: int | None, now: float) -> bool:
    cur = store.execute("UPDATE workspace_places SET alerts_watchlist_id = ?, updated_at = ? WHERE id = ?",
                        (watchlist_id, now, pid))
    return cur.rowcount > 0


def delete_place(store, pid: int) -> bool:
    return store.execute("DELETE FROM workspace_places WHERE id = ?", (pid,)).rowcount > 0
