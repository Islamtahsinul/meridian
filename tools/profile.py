"""Export and import your own things as one portable JSON file, to move them between computers.

    python tools/profile.py export [FILE] [--data-dir DIR]
    python tools/profile.py import FILE [--mode merge|replace] [--dry-run] [--yes] [--create] [--data-dir DIR]

A profile holds what you made in Meridian, nothing it downloaded:
- favourites;
- case files, with their notes and items;
- alerts: watchlists (their areas and rules) and the alert settings;
- workspaces: saved workspaces and My places (the workspace module's tables).

export reads the database without changing it, so Meridian can keep running; the file goes to
data/backups/meridian-profile-<UTC time>.json unless you name one. It holds your own data (case notes,
watchlist areas such as home), so keep it private.

import checks the whole file first (format, schema version and every record, with the same rules as the
app: the alerts and workspace modules' own code checks watchlists, rules, settings, workspaces and places)
and changes nothing if any of it is wrong. It refuses when the data folder has no database (a mistyped
--data-dir, or a Meridian that has never started there) unless --create asks for a new one. Then, in one
transaction:
- merge (the default) adds what is missing and never changes or removes anything already here. Case files
  and watchlists count as the same when their name and creation time match; their missing items and rules
  are added. Running the same import twice adds nothing the second time;
- replace makes your things match the profile exactly. It asks first (or --yes), and saves your current
  things to data/backups/meridian-profile-before-import-<time>.json before it starts.
Imported alert rules start watching from the moment of the import, so old events do not flood the inbox.
--dry-run does the whole import on a temporary copy of the database and reports what it would do; the
database itself is only read. Reload Meridian in the browser afterwards. Exit codes: 0 done, 1 refused or
failed, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import math
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
import ops_common as oc  # noqa: E402

FORMAT = "meridian-profile"
SCHEMA = 1
FILE_MAX = 64 * 1024 * 1024
KIND_MAX, REF_MAX, TEXT_MAX, LAYER_MAX = 40, 200, 300, 80            # as core_api accepts them
CASE_TITLE_MAX, NOTES_MAX, NOTE_MAX = 200, 100_000, 5_000
FAV_PAYLOAD_MAX, ITEM_PAYLOAD_MAX = 20_000, 50_000
MAX_FAVOURITES, MAX_CASEFILES, MAX_ITEMS_PER_CASE, MAX_ITEMS = 50_000, 5_000, 10_000, 100_000
WORKSPACE_TABLE = re.compile(r"^workspaces?_[a-z0-9_]{1,60}$")
WATCHLIST_LINK = "alerts_watchlist_id"            # a workspace column that points at an alerts watchlist
# The workspace module's tables (meridian/modules/workspace/db.py) and the columns an import writes. Rows
# are checked by that module's own code; other workspace tables cannot be checked, so they are skipped.
WORKSPACE_COLUMNS = {
    "workspace_saved": ("id", "name", "state", "created_at", "updated_at"),
    "workspace_places": ("id", "name", "type", "lat", "lon", "radius_km", "zoom", "notes", WATCHLIST_LINK,
                         "created_at", "updated_at"),
}
PLACE_FIELDS = ("name", "type", "lat", "lon", "radius_km", "zoom", "notes")    # what model.clean_place reads
SETUP_MODULES = ("alerts", "workspace")           # modules whose tables an import fills, made by their setup()
EARLIEST = 1e8                       # 1973: older times are mistakes
SAME_TIME = 1e-6                     # creation times within this many seconds are the same record
PROBLEMS_SHOWN = 12


class ProfileError(Exception):
    """A profile that cannot be imported; ``problems`` lists what is wrong."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []


# ---------------------------------------------------------------------------------------------
# Reading the database
# ---------------------------------------------------------------------------------------------
def _connect_read(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = 1")
    return con


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(con, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def _loads(text: Any, default: Any) -> Any:
    if not isinstance(text, str) or not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _num(v: Any) -> Any:
    return int(v) if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 53 else v


def _cell(v: Any) -> Any:
    if isinstance(v, bytes):
        return {"$base64": base64.b64encode(v).decode("ascii")}
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def export_profile(db: Path) -> dict:
    """The profile of the database at ``db`` as a dict (read only; safe while Meridian runs)."""
    con = _connect_read(db)
    try:
        tables = _tables(con)
        favourites = []
        if "favourites" in tables:
            for r in con.execute("SELECT * FROM favourites ORDER BY created_at, id"):
                favourites.append({"kind": r["kind"], "ref": r["ref"], "title": r["title"], "subtitle": r["subtitle"],
                                   "lat": r["lat"], "lon": r["lon"], "layer": r["layer"],
                                   "payload": _loads(r["payload"], {}), "created_at": _num(r["created_at"])})
        casefiles = []
        if "casefiles" in tables:
            items: dict[int, list] = {}
            if "casefile_items" in tables:
                for r in con.execute("SELECT * FROM casefile_items ORDER BY added_at, id"):
                    items.setdefault(r["casefile_id"], []).append({
                        "kind": r["kind"], "ref": r["ref"], "title": r["title"], "subtitle": r["subtitle"],
                        "lat": r["lat"], "lon": r["lon"], "layer": r["layer"], "payload": _loads(r["payload"], {}),
                        "note": r["note"], "added_at": _num(r["added_at"])})
            for r in con.execute("SELECT * FROM casefiles ORDER BY created_at, id"):
                casefiles.append({"title": r["title"], "notes": r["notes"], "created_at": _num(r["created_at"]),
                                  "updated_at": _num(r["updated_at"]), "items": items.get(r["id"], [])})
        alerts: dict[str, Any] = {"watchlists": [], "settings": {}}
        if "alerts_watchlists" in tables:
            rules: dict[int, list] = {}
            if "alerts_rules" in tables:
                for r in con.execute("SELECT * FROM alerts_rules ORDER BY id"):
                    rules.setdefault(r["watchlist_id"], []).append({
                        "preset": r["preset"], "params": _loads(r["params"], {}), "enabled": bool(r["enabled"]),
                        "created_at": _num(r["created_at"]), "updated_at": _num(r["updated_at"])})
            for r in con.execute("SELECT * FROM alerts_watchlists ORDER BY created_at, id"):
                alerts["watchlists"].append({
                    "id": r["id"],                    # only so links to it (workspace places) can follow it
                    "name": r["name"], "area": _loads(r["area"], {}), "color": r["color"],
                    "enabled": bool(r["enabled"]), "show_on_map": bool(r["show_on_map"]),
                    "created_at": _num(r["created_at"]), "updated_at": _num(r["updated_at"]),
                    "rules": rules.get(r["id"], [])})
        if "alerts_settings" in tables:
            for r in con.execute("SELECT k, v FROM alerts_settings ORDER BY k"):
                value = _loads(r["v"], None)
                if value is not None:
                    alerts["settings"][r["k"]] = value
        workspaces: dict[str, Any] = {}
        for t in sorted(x for x in tables if WORKSPACE_TABLE.match(x)):
            cols = _columns(con, t)
            rows = [[_cell(v) for v in row] for row in con.execute(f'SELECT * FROM "{t}" ORDER BY rowid')]
            workspaces[t] = {"columns": cols, "rows": rows}
    finally:
        con.close()
    now = time.time()
    out: dict[str, Any] = {
        "format": FORMAT, "schema": SCHEMA, "app_version": oc.app_version(),
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "exported_ts": round(now, 3),
        "counts": {"favourites": len(favourites), "casefiles": len(casefiles),
                   "casefile_items": sum(len(c["items"]) for c in casefiles),
                   "watchlists": len(alerts["watchlists"]),
                   "rules": sum(len(w["rules"]) for w in alerts["watchlists"]),
                   "alert_settings": len(alerts["settings"]),
                   "workspace_rows": sum(len(t["rows"]) for t in workspaces.values())},
        "favourites": favourites, "casefiles": casefiles, "alerts": alerts,
    }
    if workspaces:
        out["workspaces"] = {"tables": workspaces}
    return out


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------
class _Check:
    """Collects problems with their place in the file ("favourites[3].lat")."""

    def __init__(self, now: float):
        self.now = now
        self.problems: list[str] = []

    def bad(self, where: str, what: str) -> None:
        self.problems.append(f"{where}: {what}")

    def text(self, value: Any, where: str, limit: int, *, required: bool = False) -> str:
        if value is None:
            value = ""
        if not isinstance(value, str):
            self.bad(where, "must be text")
            return ""
        if len(value) > limit:
            self.bad(where, f"is longer than {limit:,} characters")
        if required and not value.strip():
            self.bad(where, "is required")
        return value[:limit]

    def time(self, value: Any, where: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) \
                or not EARLIEST <= float(value) <= self.now + 2 * 86400:
            self.bad(where, "must be a time in epoch seconds")
            return self.now
        return float(value)

    def flag(self, value: Any, where: str) -> bool:
        if not isinstance(value, bool):
            self.bad(where, "must be true or false")
            return True
        return value

    def position(self, lat: Any, lon: Any, where: str) -> tuple[float | None, float | None]:
        if lat is None and lon is None:
            return None, None
        from meridian import geo
        if isinstance(lat, bool) or isinstance(lon, bool) or not geo.valid_latlon(lat, lon):
            self.bad(where, "lat and lon must be a valid position or both empty")
            return None, None
        return float(lat), float(lon)

    def payload(self, value: Any, where: str, limit: int) -> str:
        if value is None:
            value = {}
        if not isinstance(value, dict):
            self.bad(where, "must be an object")
            return "{}"
        try:
            text = json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            self.bad(where, "must be plain JSON")
            return "{}"
        if len(text) > limit:
            self.bad(where, f"is larger than {limit:,} characters")
        return text

    def items(self, value: Any, where: str, limit: int) -> list:
        if value is None:
            return []
        if not isinstance(value, list):
            self.bad(where, "must be a list")
            return []
        if len(value) > limit:
            self.bad(where, f"has more than {limit:,} entries")
            return []
        return value


def _entity(chk: _Check, raw: Any, where: str, payload_max: int) -> dict | None:
    if not isinstance(raw, dict):
        chk.bad(where, "must be an object")
        return None
    lat, lon = chk.position(raw.get("lat"), raw.get("lon"), where)
    return {"kind": chk.text(raw.get("kind"), f"{where}.kind", KIND_MAX, required=True),
            "ref": chk.text(raw.get("ref"), f"{where}.ref", REF_MAX, required=True),
            "title": chk.text(raw.get("title"), f"{where}.title", TEXT_MAX),
            "subtitle": chk.text(raw.get("subtitle"), f"{where}.subtitle", TEXT_MAX),
            "lat": lat, "lon": lon, "layer": chk.text(raw.get("layer"), f"{where}.layer", LAYER_MAX),
            "payload": chk.payload(raw.get("payload"), f"{where}.payload", payload_max)}


def _alerts_code():
    """The alerts module's own validators, so imported watchlists follow the app's rules exactly."""
    from meridian.modules.alerts import areas, db, rules
    return areas, db, rules


def _workspace_code():
    """The workspace module's own validators and limits, so imported workspaces and places follow the
    app's rules exactly."""
    from meridian.modules.workspace import db, model
    return db, model


def _row_id(chk: _Check, value: Any, where: str, seen: set) -> int | None:
    """A row's own id (kept, so it stays the same on the other computer), or None to let SQLite pick one."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 53:
        chk.bad(where, "must be a whole number from 1, or null")
        return None
    if value in seen:
        chk.bad(where, f"repeats the id {value}")
    seen.add(value)
    return value


def _saved_row(chk: _Check, where: str, rec: dict, model, ids: set, names: set) -> list | None:
    """A saved workspace, checked by the workspace module: its name, and its state as the app stores it."""
    rid = _row_id(chk, rec.get("id"), f"{where}.id", ids)
    try:
        name = model.clean_name(rec.get("name"))
        state = rec.get("state")
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except (ValueError, RecursionError):
                raise model.InputError("The state is not valid JSON.") from None
        state = model.clean_state(state)
    except model.InputError as err:
        chk.bad(where, str(err))
        return None
    if name.casefold() in names:
        chk.bad(f"{where}.name", f'repeats the workspace name "{name}"')
    names.add(name.casefold())
    created = chk.time(rec.get("created_at"), f"{where}.created_at")
    updated = chk.time(rec.get("updated_at", created), f"{where}.updated_at")
    return [rid, name, json.dumps(state, separators=(",", ":")), created, updated]


def _place_row(chk: _Check, where: str, rec: dict, model, ids: set, names: set) -> list | None:
    """A place, checked by the workspace module: name, type, position, radius, zoom and notes."""
    rid = _row_id(chk, rec.get("id"), f"{where}.id", ids)
    try:
        p = model.clean_place({k: rec[k] for k in PLACE_FIELDS if k in rec})
    except model.InputError as err:
        chk.bad(where, str(err))
        return None
    link = rec.get(WATCHLIST_LINK)
    if isinstance(link, float) and link.is_integer():
        link = int(link)
    if link is not None and (isinstance(link, bool) or not isinstance(link, int) or not 0 < link < 2 ** 53):
        chk.bad(f"{where}.{WATCHLIST_LINK}", "must be a watchlist id or null")
        link = None
    created = chk.time(rec.get("created_at"), f"{where}.created_at")
    updated = chk.time(rec.get("updated_at", created), f"{where}.updated_at")
    return [rid, p["name"], p["type"], p["lat"], p["lon"], p["radius_km"], p["zoom"], p.get("notes", ""), link,
            created, updated]


def _check_workspaces(chk: _Check, tables: dict, out: dict) -> None:
    """Saved workspaces and places, row by row, with the workspace module's own checks and limits. Tables
    and columns this Meridian's workspace module does not have are skipped with a note."""
    names = []
    for name in tables:
        if isinstance(name, str) and WORKSPACE_TABLE.match(name):
            names.append(name)
        else:
            chk.bad("workspaces.tables", f"{name!r} is not a workspace table name")
    unknown = sorted(n for n in names if n not in WORKSPACE_COLUMNS)
    if unknown:
        out["notes"].append(f"Workspace tables this Meridian does not know were skipped: {', '.join(unknown)}.")
    known = [n for n in names if n in WORKSPACE_COLUMNS]
    if not known:
        return
    try:
        wdb, model = _workspace_code()
    except Exception as err:                          # a broken or missing workspace module
        out["notes"].append(f"Workspaces were skipped: the workspace module cannot be loaded here ({err}).")
        return
    for name in known:
        spec = tables[name]
        where = f"workspaces.tables.{name}"
        cols = spec.get("columns") if isinstance(spec, dict) else None
        rows = spec.get("rows") if isinstance(spec, dict) else None
        if not isinstance(cols, list) or not cols or not all(
                isinstance(c, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,60}", c) for c in cols) \
                or len(set(cols)) != len(cols):
            chk.bad(where, "needs a list of column names")
            continue
        if not isinstance(rows, list):
            chk.bad(where, "needs a list of rows")
            continue
        extra = [c for c in cols if c not in WORKSPACE_COLUMNS[name]]
        if extra:
            out["notes"].append(f"Workspace columns this Meridian does not know were skipped: "
                                f"{', '.join(f'{name}.{c}' for c in extra)}.")
        places = name == "workspace_places"
        limit = wdb.MAX_PLACES if places else wdb.MAX_WORKSPACES
        if len(rows) > limit:
            chk.bad(where, f"Meridian keeps at most {limit} {'places' if places else 'saved workspaces'}")
            continue
        clean_row = _place_row if places else _saved_row
        clean, ids, seen_names = [], set(), set()
        for j, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(cols):
                chk.bad(f"{where}.rows[{j}]", f"must have {len(cols)} values")
                continue
            cleaned = clean_row(chk, f"{where}.rows[{j}]", dict(zip(cols, row)), model, ids, seen_names)
            if cleaned is not None:
                clean.append(cleaned)
        clean.sort(key=lambda r: r[0] is None)        # rows with their own id first: new ids never take theirs
        out["workspaces"][name] = {"columns": list(WORKSPACE_COLUMNS[name]), "rows": clean}


def validate(doc: Any, now: float | None = None) -> dict:
    """Check a parsed profile and return it cleaned (text, times, areas and rules as the app stores them).
    Raises ProfileError listing every problem."""
    now = time.time() if now is None else now
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ProfileError("This is not a Meridian profile.")
    schema = doc.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema < 1:
        raise ProfileError("The profile has no valid schema version.")
    if schema > SCHEMA:
        raise ProfileError(f"This profile was made by a newer Meridian (schema {schema}; this one reads up to "
                           f"{SCHEMA}). Update Meridian first.")
    chk = _Check(now)
    out: dict[str, Any] = {"favourites": [], "casefiles": [], "watchlists": [], "settings": {}, "workspaces": {},
                           "notes": [], "max_watchlists": None,
                           "exported_at": doc.get("exported_at"), "app_version": doc.get("app_version")}
    seen_favs = set()
    for i, raw in enumerate(chk.items(doc.get("favourites"), "favourites", MAX_FAVOURITES)):
        fav = _entity(chk, raw, f"favourites[{i}]", FAV_PAYLOAD_MAX)
        if fav is None:
            continue
        fav["created_at"] = chk.time(raw.get("created_at"), f"favourites[{i}].created_at")
        if (fav["kind"], fav["ref"]) in seen_favs:
            continue                                  # the table keeps one per kind and ref
        seen_favs.add((fav["kind"], fav["ref"]))
        out["favourites"].append(fav)
    total_items = 0
    for i, raw in enumerate(chk.items(doc.get("casefiles"), "casefiles", MAX_CASEFILES)):
        where = f"casefiles[{i}]"
        if not isinstance(raw, dict):
            chk.bad(where, "must be an object")
            continue
        case = {"title": chk.text(raw.get("title"), f"{where}.title", CASE_TITLE_MAX).strip() or "Untitled case",
                "notes": chk.text(raw.get("notes"), f"{where}.notes", NOTES_MAX),
                "created_at": chk.time(raw.get("created_at"), f"{where}.created_at"), "items": []}
        case["updated_at"] = chk.time(raw.get("updated_at", case["created_at"]), f"{where}.updated_at")
        for j, item_raw in enumerate(chk.items(raw.get("items"), f"{where}.items", MAX_ITEMS_PER_CASE)):
            item = _entity(chk, item_raw, f"{where}.items[{j}]", ITEM_PAYLOAD_MAX)
            if item is None:
                continue
            item["note"] = chk.text(item_raw.get("note"), f"{where}.items[{j}].note", NOTE_MAX)
            item["added_at"] = chk.time(item_raw.get("added_at"), f"{where}.items[{j}].added_at")
            case["items"].append(item)
        total_items += len(case["items"])
        out["casefiles"].append(case)
    if total_items > MAX_ITEMS:
        chk.bad("casefiles", f"hold more than {MAX_ITEMS:,} items in all")
    alerts = doc.get("alerts")
    if alerts is not None and not isinstance(alerts, dict):
        chk.bad("alerts", "must be an object")
        alerts = None
    alerts = alerts or {}
    watchlists = chk.items(alerts.get("watchlists"), "alerts.watchlists", 1000)
    settings = alerts.get("settings") or {}
    if watchlists or settings:
        try:
            areas, adb, rules = _alerts_code()
        except Exception as err:                      # a broken or missing alerts module
            raise ProfileError(f"The profile has alerts, but the alerts module cannot be loaded here ({err}). "
                               "Nothing was imported.") from None
        out["max_watchlists"] = adb.MAX_WATCHLISTS
        if len(watchlists) > adb.MAX_WATCHLISTS:
            chk.bad("alerts.watchlists", f"Meridian keeps at most {adb.MAX_WATCHLISTS} watchlists")
        unknown_presets: set[str] = set()
        for i, raw in enumerate(watchlists):
            where = f"alerts.watchlists[{i}]"
            if not isinstance(raw, dict):
                chk.bad(where, "must be an object")
                continue
            try:
                area = areas.parse_area(raw.get("area"))
            except (areas.AreaError, TypeError, ValueError) as err:
                chk.bad(f"{where}.area", str(err))
                continue
            try:
                name = adb.clean_name(raw.get("name"), "")
                color = adb.clean_color(raw.get("color"), adb.COLORS[0])
            except adb.InputError as err:
                chk.bad(where, str(err))
                continue
            if not name:
                name = area.place["name"] if area.place else ("World" if area.kind == "world" else "New area")
            source_id = raw.get("id")
            wl = {"name": name, "area": adb.dumps(area.to_json()), "color": color,
                  "source_id": source_id if isinstance(source_id, int) and not isinstance(source_id, bool) else None,
                  "enabled": chk.flag(raw.get("enabled", True), f"{where}.enabled"),
                  "show_on_map": chk.flag(raw.get("show_on_map", True), f"{where}.show_on_map"),
                  "created_at": chk.time(raw.get("created_at"), f"{where}.created_at"), "rules": []}
            wl["updated_at"] = chk.time(raw.get("updated_at", wl["created_at"]), f"{where}.updated_at")
            presets = set()
            for j, rr in enumerate(chk.items(raw.get("rules"), f"{where}.rules", len(rules.ORDER))):
                rw = f"{where}.rules[{j}]"
                if not isinstance(rr, dict):
                    chk.bad(rw, "must be an object")
                    continue
                if isinstance(rr.get("preset"), str) and rr["preset"] not in rules.PRESETS \
                        and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", rr["preset"]):
                    unknown_presets.add(rr["preset"])         # from a newer Meridian: skipped, not an error
                    continue
                try:
                    preset = rules.get(rr.get("preset"))
                    params = preset.clean_params(rr.get("params"))
                except rules.RuleError as err:
                    chk.bad(rw, str(err))
                    continue
                if preset.id in presets:
                    chk.bad(rw, f"repeats the {preset.id} rule")
                    continue
                presets.add(preset.id)
                created = chk.time(rr.get("created_at", wl["created_at"]), f"{rw}.created_at")
                wl["rules"].append({"preset": preset.id, "params": adb.dumps(params),
                                    "enabled": chk.flag(rr.get("enabled", True), f"{rw}.enabled"),
                                    "created_at": created,
                                    "updated_at": chk.time(rr.get("updated_at", created), f"{rw}.updated_at")})
            out["watchlists"].append(wl)
        if unknown_presets:
            out["notes"].append(f"Rules this Meridian does not know were skipped: {', '.join(sorted(unknown_presets))}.")
        if settings:
            if not isinstance(settings, dict):
                chk.bad("alerts.settings", "must be an object")
            else:
                unknown = sorted(k for k in settings if k not in adb.DEFAULT_SETTINGS)
                if unknown:
                    out["notes"].append(f"Alert settings this Meridian does not know were skipped: {', '.join(map(str, unknown))}.")
                try:
                    cleaned = adb.clean_settings({k: v for k, v in settings.items() if k in adb.DEFAULT_SETTINGS})
                    out["settings"] = {k: adb.dumps(v) for k, v in cleaned.items()}
                except adb.InputError as err:
                    chk.bad("alerts.settings", str(err))
    ws = doc.get("workspaces")
    if ws is not None:
        tables = ws.get("tables") if isinstance(ws, dict) else None
        if not isinstance(tables, dict):
            chk.bad("workspaces", "must be an object with tables")
        else:
            _check_workspaces(chk, tables, out)
    if chk.problems:
        raise ProfileError(f"The profile has {len(chk.problems)} problem{'s' if len(chk.problems) != 1 else ''}; "
                           "nothing was imported.", chk.problems)
    return out


def load_profile(path: Path) -> dict:
    try:
        size = path.stat().st_size
    except OSError:
        raise ProfileError(f"There is no file at {path}.") from None
    if size > FILE_MAX:
        raise ProfileError(f"{path.name} is larger than {oc.human_size(FILE_MAX)}; that is not a profile.")
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as err:
        raise ProfileError(f"{path.name} is not valid JSON ({err}).") from None
    return validate(doc)


# ---------------------------------------------------------------------------------------------
# Importing
# ---------------------------------------------------------------------------------------------
def _same(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= SAME_TIME


def apply_profile(c: sqlite3.Connection, prof: dict, mode: str, now: float) -> dict:
    """Write a validated profile inside an open transaction on ``c``. Returns what changed."""
    s: dict[str, Any] = {k: 0 for k in ("fav_added", "fav_kept", "fav_removed", "case_added", "case_kept",
                                         "case_removed", "items_added", "wl_added", "wl_kept", "wl_removed",
                                         "rules_added", "set_added", "set_kept", "set_removed", "ws_rows")}
    s["ws_note"] = None
    present = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    alerts_here = {"alerts_watchlists", "alerts_rules", "alerts_settings"} <= present
    if mode == "replace":
        s["fav_removed"] = c.execute("DELETE FROM favourites").rowcount or 0
        c.execute("DELETE FROM casefile_items")
        s["case_removed"] = c.execute("DELETE FROM casefiles").rowcount or 0
        if alerts_here:
            ids = [r[0] for r in c.execute("SELECT id FROM alerts_watchlists")]
            for i in range(0, len(ids), 400):        # as deleting a watchlist in the app does
                chunk = ids[i:i + 400]
                marks = ",".join("?" * len(chunk))
                if "alerts_alerts" in present:
                    c.execute(f"DELETE FROM alerts_alerts WHERE watchlist_id IN ({marks})", chunk)
                if "alerts_seen" in present:
                    c.execute(f"DELETE FROM alerts_seen WHERE rule_id IN "
                              f"(SELECT id FROM alerts_rules WHERE watchlist_id IN ({marks}))", chunk)
                c.execute(f"DELETE FROM alerts_rules WHERE watchlist_id IN ({marks})", chunk)
            s["wl_removed"] = c.execute("DELETE FROM alerts_watchlists").rowcount or 0
            s["set_removed"] = c.execute("DELETE FROM alerts_settings").rowcount or 0

    for f in prof["favourites"]:
        cur = c.execute("INSERT INTO favourites(kind, ref, title, subtitle, lat, lon, layer, payload, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, ref) DO NOTHING",
                        (f["kind"], f["ref"], f["title"], f["subtitle"], f["lat"], f["lon"], f["layer"], f["payload"],
                         f["created_at"]))
        if cur.rowcount:
            s["fav_added"] += 1
        else:
            s["fav_kept"] += 1

    existing_cases = [dict(r) for r in c.execute("SELECT id, title, created_at FROM casefiles")]
    for case in prof["casefiles"]:
        match = next((e for e in existing_cases if e["title"] == case["title"] and _same(e["created_at"], case["created_at"])), None)
        if match is None:
            cid = c.execute("INSERT INTO casefiles(title, notes, created_at, updated_at) VALUES(?,?,?,?)",
                            (case["title"], case["notes"], case["created_at"], case["updated_at"])).lastrowid
            existing_cases.append({"id": cid, "title": case["title"], "created_at": case["created_at"]})
            have = []
            s["case_added"] += 1
        else:
            cid = match["id"]
            have = [dict(r) for r in c.execute("SELECT kind, ref, added_at FROM casefile_items WHERE casefile_id = ?", (cid,))]
            s["case_kept"] += 1
        added = 0
        for it in case["items"]:
            if any(h["kind"] == it["kind"] and h["ref"] == it["ref"] and _same(h["added_at"], it["added_at"]) for h in have):
                continue
            c.execute("INSERT INTO casefile_items(casefile_id, kind, ref, title, subtitle, lat, lon, layer, payload, note, "
                      "added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (cid, it["kind"], it["ref"], it["title"], it["subtitle"], it["lat"], it["lon"], it["layer"],
                       it["payload"], it["note"], it["added_at"]))
            have.append({"kind": it["kind"], "ref": it["ref"], "added_at": it["added_at"]})
            added += 1
        s["items_added"] += added
        if match is not None and added:
            c.execute("UPDATE casefiles SET updated_at = MAX(updated_at, ?) WHERE id = ?", (now, cid))

    if (prof["watchlists"] or prof["settings"]) and not alerts_here:
        raise ProfileError("This database has no alerts tables, so the profile's alerts cannot be imported. "
                           "Nothing was imported.")
    existing_wls = [dict(r) for r in c.execute("SELECT id, name, created_at FROM alerts_watchlists")] if alerts_here else []
    watchlist_ids: dict[int, int] = {}               # the profile's watchlist id -> its id here
    for wl in prof["watchlists"]:
        match = next((e for e in existing_wls if e["name"] == wl["name"] and _same(e["created_at"], wl["created_at"])), None)
        if match is None:
            wid = c.execute("INSERT INTO alerts_watchlists(name, area, color, enabled, show_on_map, created_at, updated_at) "
                            "VALUES(?,?,?,?,?,?,?)", (wl["name"], wl["area"], wl["color"], int(wl["enabled"]),
                                                      int(wl["show_on_map"]), wl["created_at"], wl["updated_at"])).lastrowid
            existing_wls.append({"id": wid, "name": wl["name"], "created_at": wl["created_at"]})
            presets: set[str] = set()
            s["wl_added"] += 1
        else:
            wid = match["id"]
            presets = {r[0] for r in c.execute("SELECT preset FROM alerts_rules WHERE watchlist_id = ?", (wid,))}
            s["wl_kept"] += 1
        if wl["source_id"] is not None:
            watchlist_ids[wl["source_id"]] = wid
        for rule in wl["rules"]:
            if rule["preset"] in presets:
                continue
            # watch_from = now and no baseline yet: the engine first records what already matches, quietly
            c.execute("INSERT INTO alerts_rules(watchlist_id, preset, params, enabled, watch_from, baseline_at, checked_at, "
                      "created_at, updated_at) VALUES(?,?,?,?,?,NULL,NULL,?,?)",
                      (wid, rule["preset"], rule["params"], int(rule["enabled"]), now, rule["created_at"], rule["updated_at"]))
            presets.add(rule["preset"])
            s["rules_added"] += 1
    limit = prof.get("max_watchlists")
    if s["wl_added"] and limit:
        total = c.execute("SELECT COUNT(*) FROM alerts_watchlists").fetchone()[0]
        if total > limit:
            raise ProfileError(f"After the import there would be {total} watchlists; Meridian keeps at most {limit}. "
                               "Delete some first, or use --mode replace. Nothing was imported.")

    have_settings = {r[0] for r in c.execute("SELECT k FROM alerts_settings")} if alerts_here else set()
    for key, value in prof["settings"].items():
        if key in have_settings:
            s["set_kept"] += 1
            continue
        c.execute("INSERT INTO alerts_settings(k, v, updated_at) VALUES(?,?,?)", (key, value, now))
        s["set_added"] += 1

    if prof["workspaces"]:
        missing = [t for t in prof["workspaces"] if t not in present]
        if missing:                                  # import_into makes them; only a database it did not prepare
            s["ws_note"] = (f"This database has no {', '.join(missing)} table{'s' if len(missing) != 1 else ''}, "
                            "so workspaces were not imported.")
        else:
            bad_cols = [t for t, spec in prof["workspaces"].items()
                        if not set(spec["columns"]) <= set(_columns(c, t))]
            busy = [t for t in prof["workspaces"] if c.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone()]
            if bad_cols:
                s["ws_note"] = f"The workspace tables here differ from the profile's ({', '.join(bad_cols)}), so workspaces were not imported."
            elif busy and mode == "merge":
                s["ws_note"] = ("This Meridian already has workspaces, so the profile's were not merged into them. "
                                "Use --mode replace to take the profile's workspaces instead.")
            else:
                c.execute("PRAGMA defer_foreign_keys = ON")
                unlinked = 0
                for t, spec in prof["workspaces"].items():
                    if mode == "replace":
                        c.execute(f'DELETE FROM "{t}"')
                    cols = ", ".join(f'"{x}"' for x in spec["columns"])
                    marks = ", ".join("?" * len(spec["columns"]))
                    # links to watchlists follow the ids the watchlists got here; a link to a watchlist
                    # that is not here is cleared rather than left pointing at another one
                    link = spec["columns"].index(WATCHLIST_LINK) if WATCHLIST_LINK in spec["columns"] else None
                    for row in spec["rows"]:
                        if link is not None and row[link] is not None:
                            row = list(row)
                            row[link] = watchlist_ids.get(row[link])
                            unlinked += row[link] is None
                        c.execute(f'INSERT INTO "{t}" ({cols}) VALUES ({marks})', row)
                        s["ws_rows"] += 1
                if unlinked:
                    s["ws_note"] = (f"{_plural(s['ws_rows'], 'workspace row')} added; {_plural(unlinked, 'place')} lost "
                                    "its link to a watchlist that was not in the profile.")
    return s


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def report(s: dict, mode: str) -> list[str]:
    lines = []
    removed = mode == "replace"
    fav = f"Favourites: {s['fav_added']:,} added"
    fav += f", {s['fav_removed']:,} removed first" if removed else f", {s['fav_kept']:,} already here"
    lines.append(fav + ".")
    case = f"Case files: {s['case_added']:,} added"
    case += f", {s['case_removed']:,} removed first" if removed else f", {s['case_kept']:,} already here"
    lines.append(case + f"; {_plural(s['items_added'], 'item')} added.")
    wl = f"Watchlists: {s['wl_added']:,} added"
    wl += f", {s['wl_removed']:,} removed first" if removed else f", {s['wl_kept']:,} already here"
    lines.append(wl + f"; {_plural(s['rules_added'], 'rule')} added.")
    st = f"Alert settings: {s['set_added']:,} set"
    st += f", {s['set_removed']:,} removed first" if removed else f", {s['set_kept']:,} kept as they were here"
    lines.append(st + ".")
    if s["ws_note"]:
        lines.append(f"Workspaces: {s['ws_note']}")
    elif s["ws_rows"]:
        lines.append(f"Workspaces: {_plural(s['ws_rows'], 'row')} added.")
    return lines


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------
def write_profile(doc: dict, out: Path) -> None:
    oc.atomic_write(out, json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def copy_database(db: Path, to: Path) -> None:
    """A consistent copy of ``db`` through SQLite's online backup API. Only reads ``db``."""
    src = _connect_read(db)
    try:
        dst = sqlite3.connect(str(to))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def import_into(db: Path, prof: dict, mode: str, now: float) -> dict:
    """Open the database at ``db`` (made when there is none), give it the tables a profile fills the way
    the alerts and workspace modules make them (their own setup), then apply the profile in one
    transaction. Returns what changed."""
    from meridian.store import Store
    setups = []
    for name in SETUP_MODULES:
        try:
            setups.append(importlib.import_module(f"meridian.modules.{name}.db").setup)
        except Exception:                            # module missing or broken: validate noted or refused it
            pass
    store = Store(db)
    try:
        for setup in setups:
            setup(store)
        with store.tx() as c:
            return apply_profile(c, prof, mode, now)
    finally:
        store.close()


def cmd_export(args) -> int:
    config = oc.load_config()
    ddir = oc.data_dir(config, args.data_dir)
    db = ddir / "meridian.db"
    if not db.is_file():
        return oc.error(f"There is no database at {db}. Has Meridian run here yet?")
    out = Path(args.file).expanduser() if args.file else ddir / "backups" / f"meridian-profile-{oc.utc_stamp()}.json"
    try:
        doc = export_profile(db)
        write_profile(doc, out)
    except (OSError, sqlite3.Error, ValueError) as err:
        return oc.error(f"The export failed: {err}")
    n = doc["counts"]
    print(f"Exported {_plural(n['favourites'], 'favourite')}, {_plural(n['casefiles'], 'case file')} "
          f"({_plural(n['casefile_items'], 'item')}), {_plural(n['watchlists'], 'watchlist')} "
          f"({_plural(n['rules'], 'rule')}), {_plural(n['alert_settings'], 'alert setting')}"
          + (f" and {_plural(n['workspace_rows'], 'workspace row')}" if n["workspace_rows"] else "")
          + f" to {out.resolve()}.")
    print("The file holds your own data (case notes, watchlist areas), so keep it private.")
    return 0


def cmd_import(args) -> int:
    config = oc.load_config()
    ddir = oc.data_dir(config, args.data_dir)
    db = ddir / "meridian.db"
    path = Path(args.file).expanduser()
    try:
        prof = load_profile(path)
    except ProfileError as err:
        print(str(err), file=sys.stderr)
        for p in err.problems[:PROBLEMS_SHOWN]:
            print(f"  {p}", file=sys.stderr)
        if len(err.problems) > PROBLEMS_SHOWN:
            print(f"  ...and {len(err.problems) - PROBLEMS_SHOWN:,} more.", file=sys.stderr)
        return 1
    print(f"{path.name} is a valid profile (exported {prof.get('exported_at') or 'at an unknown time'} by Meridian "
          f"{prof.get('app_version') or '?'}): {_plural(len(prof['favourites']), 'favourite')}, "
          f"{_plural(len(prof['casefiles']), 'case file')}, {_plural(len(prof['watchlists']), 'watchlist')}.")
    exists = db.is_file()
    if not exists and not args.create:
        return oc.error(f"There is no database at {db}. Check --data-dir, or start Meridian once with this data "
                        "folder first. To import into a new, empty database there, add --create. Nothing was changed.")
    if args.mode == "replace" and not args.dry_run:
        ws = [w for t, w in (("workspace_saved", "saved workspaces"), ("workspace_places", "places"))
              if t in prof["workspaces"]]
        print("Replace removes your current favourites, case files, watchlists (with their alerts), alert settings"
              f"{', ' + ' and '.join(ws) if ws else ''}, and puts the profile's in their place.")
        if not oc.confirm("Replace your things with the profile's?", assume_yes=args.yes):
            print("Nothing was changed.")
            return 1
    safety = None
    now = time.time()
    try:
        if args.dry_run:
            # the whole import, tables and all, on a throwaway copy: the database itself is only read
            with tempfile.TemporaryDirectory(prefix="meridian-profile-dry-run-", ignore_cleanup_errors=True) as tmp:
                work = Path(tmp) / "meridian.db"
                if exists:
                    copy_database(db, work)
                summary = import_into(work, prof, args.mode, now)
        else:
            if args.mode == "replace" and exists:
                safety = ddir / "backups" / f"meridian-profile-before-import-{oc.utc_stamp()}.json"
                write_profile(export_profile(db), safety)
            summary = import_into(db, prof, args.mode, now)
    except ProfileError as err:
        return oc.error(str(err))
    except (OSError, sqlite3.Error) as err:
        what = "dry run" if args.dry_run else "import"
        return oc.error(f"The {what} failed ({err}). Nothing was {'changed' if exists or args.dry_run else 'imported'}.")
    if args.dry_run:
        into = "" if exists else f" (into a new database at {db})"
        print(f"Dry run ({args.mode}): this is what the import would do{into}. Nothing was changed.")
    else:
        print(f"Imported ({args.mode}){'' if exists else f' into a new database at {db}'}:")
        if safety:
            print(f"  Your things as they were are saved in {safety}.")
    for line in report(summary, args.mode) + prof["notes"]:
        print(f"  {line}")
    if not args.dry_run:
        print("Reload Meridian in your browser to see the changes.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python tools/profile.py", description=__doc__.split("\n\n")[0],
                                epilog="Run a command with --help for its options. More in docs/OPERATIONS.md.")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    e = sub.add_parser("export", help="write your favourites, case files and alerts to a JSON file")
    e.add_argument("file", nargs="?", help="where to write (default: data/backups/meridian-profile-<time>.json)")
    e.add_argument("--data-dir", help="Meridian's data folder (default: as the server uses, normally data/)")
    e.set_defaults(fn=cmd_export)
    i = sub.add_parser("import", help="bring a profile in, merging (default) or replacing")
    i.add_argument("file", help="a profile written by export")
    i.add_argument("--mode", choices=("merge", "replace"), default="merge",
                   help="merge adds what is missing (default); replace makes your things match the profile")
    i.add_argument("--dry-run", action="store_true", help="check and report what would change, change nothing")
    i.add_argument("--yes", action="store_true", help="do not ask before replacing")
    i.add_argument("--create", action="store_true",
                   help="make a new database when the data folder has none (normally Meridian makes it on first start)")
    i.add_argument("--data-dir", help="Meridian's data folder (default: as the server uses, normally data/)")
    i.set_defaults(fn=cmd_import)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
