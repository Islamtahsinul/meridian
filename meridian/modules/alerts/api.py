"""Alerts HTTP routes (all under /api/alerts/) and the ``alerts_recent`` AI tool."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Callable

from meridian.registry import HTTPError

from . import db
from . import rules as R
from .areas import AreaError, parse_area
from .engine import Engine

RECENT_LIMIT_MAX = 1000
INBOX_LIMIT_MAX = 200
CHECK_MIN_GAP_S = 10.0


def _int(value: Any, name: str = "id") -> int:
    """A whole-number id from JSON or a path segment."""
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError
            value = int(value)
        elif isinstance(value, str):
            text = value.strip()
            if not (text.isascii() and text.isdigit()) or len(text) > 15:
                raise ValueError
            value = int(text)
        elif not isinstance(value, int):
            raise ValueError
        if not 0 < value < 2 ** 53:
            raise ValueError
        return value
    except (TypeError, ValueError, OverflowError):
        raise HTTPError(400, "BAD_FIELD", f'"{name}" must be a whole number.') from None


def _bad(err: Exception, code: str = "BAD_FIELD") -> HTTPError:
    text = str(err).strip() or "That request is not valid."
    text = text[0].upper() + text[1:]
    if not text.endswith("."):
        text += "."
    return HTTPError(400, code, text)


def _epoch(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value) or value < 0:
        raise HTTPError(400, "BAD_ARG", f'Query parameter "{name}" must be a time in epoch seconds.')
    return value / 1000.0 if value > 1e11 else value


def _utc(t: float | None) -> str | None:
    if t is None:
        return None
    try:
        return datetime.fromtimestamp(float(t), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return None


def preset_list(app) -> list[dict]:
    out = []
    for p in R.PRESETS.values():
        missing = [name for name in p.modules if not getattr(app.modules.get(name), "ok", False)]
        item = p.to_json()
        item["available"] = not missing
        item["note"] = (f"Needs the {missing[0]} module, which is not running." if missing else None)
        out.append(item)
    return out


def register_routes(m, engine: Engine, clock: Callable[[], float] = time.time) -> None:
    store = m.store
    state = {"last_manual": 0.0}

    def watchlist_or_404(wid: int) -> dict:
        w = db.get_watchlist(store, wid, clock())
        if w is None:
            raise HTTPError(404, "NOT_FOUND", "No such watchlist.")
        return w

    # -- presets and watchlists ---------------------------------------------------------------------
    @m.route("GET", "/api/alerts/presets")
    def r_presets(req):
        return {"presets": preset_list(m.app), "escalate_by": R.ESCALATE_BY}

    @m.route("GET", "/api/alerts/watchlists")
    def r_watchlists(req):
        return {"watchlists": db.list_watchlists(store, clock()), "max": db.MAX_WATCHLISTS}

    @m.route("GET", "/api/alerts/watchlists/{wid}")
    def r_watchlist(req):
        return watchlist_or_404(_int(req.params["wid"]))

    @m.route("GET", "/api/alerts/watchlists/{wid}/matches")
    def r_matches(req):
        w = watchlist_or_404(_int(req.params["wid"]))
        limit = req.arg("limit", 30, type=int, min=1, max=100)
        try:
            return engine.matches_now(w, clock(), limit)
        except ValueError as err:
            raise _bad(err) from None

    @m.route("POST", "/api/alerts/watchlists", body_max=262144)
    def r_watchlists_change(req):
        action = req.field("action", type=str)
        if action not in ("create", "update", "delete"):
            raise HTTPError(400, "BAD_FIELD", 'action must be "create", "update" or "delete".')
        now = clock()
        try:
            if action == "create":
                area = parse_area(req.field("area", type=dict))
                name = db.clean_name(req.field("name", None), "")
                if not name:
                    name = area.place["name"] if area.place else ("World" if area.kind == "world" else "New area")
                color = db.clean_color(req.field("color", None), None) if req.field("color", None) else None
                rule_list = db.clean_rules(req.field("rules", None))
                wid = db.create_watchlist(store, name=name, area=area, color=color, rules=rule_list, now=now)
                return {"ok": True, "id": wid, "watchlist": db.get_watchlist(store, wid, now)}
            wid = _int(req.field("id"))
            if action == "update":
                area = parse_area(req.field("area", type=dict)) if req.field("area", None) is not None else None
                name = req.field("name", None)
                if name is not None:
                    name = db.clean_name(name, "")
                    if not name:
                        raise db.InputError("The name cannot be empty.")
                color = db.clean_color(req.field("color"), None) if req.field("color", None) is not None else None
                enabled = db.clean_bool(req.field("enabled"), "enabled") if req.field("enabled", None) is not None else None
                show = (db.clean_bool(req.field("show_on_map"), "show_on_map")
                        if req.field("show_on_map", None) is not None else None)
                if not db.update_watchlist(store, wid, now=now, name=name, area=area, color=color, enabled=enabled,
                                           show_on_map=show):
                    raise HTTPError(404, "NOT_FOUND", "No such watchlist.")
                return {"ok": True, "watchlist": db.get_watchlist(store, wid, now)}
            if action == "delete":
                if db.get_watchlist(store, wid, now) is None:
                    raise HTTPError(404, "NOT_FOUND", "No such watchlist.")
                removed = db.delete_watchlist(store, wid)
                return {"ok": True, "alerts_removed": removed}
        except (AreaError, db.InputError, R.RuleError) as err:
            raise _bad(err) from None
        raise HTTPError(400, "BAD_FIELD", 'action must be "create", "update" or "delete".')

    @m.route("POST", "/api/alerts/rules")
    def r_rules_change(req):
        action = req.field("action", type=str)
        if action not in ("set", "delete"):
            raise HTTPError(400, "BAD_FIELD", 'action must be "set" or "delete".')
        now = clock()
        try:
            if action == "set":
                wid = _int(req.field("watchlist_id"), "watchlist_id")
                preset = req.field("preset", type=str)
                params = req.field("params", None)
                if params is not None and not isinstance(params, dict):
                    raise db.InputError("params must be an object.")
                enabled = req.field("enabled", None)
                if enabled is not None:
                    enabled = db.clean_bool(enabled, "enabled")
                try:
                    rid = db.set_rule(store, wid, preset, params=params, enabled=enabled, now=now)
                except LookupError:
                    raise HTTPError(404, "NOT_FOUND", "No such watchlist.") from None
                return {"ok": True, "id": rid, "watchlist": db.get_watchlist(store, wid, now)}
            if action == "delete":
                if not db.delete_rule(store, _int(req.field("id"))):
                    raise HTTPError(404, "NOT_FOUND", "No such rule.")
                return {"ok": True}
        except (db.InputError, R.RuleError) as err:
            raise _bad(err) from None
        raise HTTPError(400, "BAD_FIELD", 'action must be "set" or "delete".')

    # -- fired alerts (CONTRACT.md 15.4) ------------------------------------------------------------
    @m.route("GET", "/api/alerts/recent")
    def r_recent(req):
        now = clock()
        since = _epoch(req.arg("since", None, type=float), "since")
        if since is None:
            since = now - 86400.0
        limit = req.arg("limit", 100, type=int, min=1, max=RECENT_LIMIT_MAX)
        digests = req.arg("digests", False, type=bool)
        items = db.recent(store, since=since, limit=limit, digests=digests)
        return {"since": db.num(round(since, 3)), "count": len(items), "alerts": items}

    # -- inbox ----------------------------------------------------------------------------------------
    @m.route("GET", "/api/alerts/inbox")
    def r_inbox(req):
        limit = req.arg("limit", 50, type=int, min=1, max=INBOX_LIMIT_MAX)
        before = req.arg("before", None, type=int, min=1)
        before_t = req.arg("before_t", None, type=float, min=0.0)
        after = req.arg("after", None, type=int, min=0)
        digest = req.arg("digest", None, type=int, min=1)
        unread = req.arg("unread", False, type=bool)
        items = db.inbox(store, limit=limit, before=before, before_t=before_t, after=after, unread=unread, digest=digest)
        out = {"alerts": items, **db.counts(store, clock())}
        if digest is None and after is None and len(items) == limit:
            out["next_before"] = items[-1]["id"]
            out["next_before_t"] = items[-1]["updated_at"]
        return out

    @m.route("POST", "/api/alerts/inbox")
    def r_inbox_change(req):
        action = req.field("action", type=str)
        now = clock()
        if action in ("read", "unread"):
            ids = req.field("ids", type=list, max_len=1000)
            n = db.mark(store, [_int(i, "ids") for i in ids], read=action == "read", now=now)
            return {"ok": True, "changed": n, **db.counts(store, now)}
        if action == "read_all":
            n = db.read_all(store, now)
            return {"ok": True, "changed": n, **db.counts(store, now)}
        if action == "clear":
            read_only = req.field("read_only", False, type=bool)
            n = db.clear(store, now, read_only=read_only)
            return {"ok": True, "changed": n, **db.counts(store, now)}
        if action == "test":
            aid = engine.test_alert(now)
            return {"ok": True, "id": aid, **db.counts(store, now)}
        raise HTTPError(400, "BAD_FIELD", 'action must be "read", "unread", "read_all", "clear" or "test".')

    @m.route("GET", "/api/alerts/counts")
    def r_counts(req):
        out = db.counts(store, clock())
        last = engine.last or {}
        out["engine"] = {"ok": last.get("ok"), "at": last.get("at"), "error": last.get("error")}
        return out

    # -- settings and status ----------------------------------------------------------------------
    @m.route("GET", "/api/alerts/settings")
    def r_settings(req):
        return db.settings(store)

    @m.route("POST", "/api/alerts/settings")
    def r_settings_change(req):
        body = req.json()
        try:
            change = db.clean_settings(body)
        except db.InputError as err:
            raise _bad(err) from None
        return db.save_settings(store, change, clock())

    @m.route("GET", "/api/alerts/status")
    def r_status(req):
        hist = None
        try:
            hist = m.app.history.info()
        except Exception:
            hist = None
        return {"engine": engine.last or None, "runs": engine.runs, "every_s": 120, "history": hist,
                "watchlists": db.count_watchlists(store), "presets": preset_list(m.app)}

    @m.route("POST", "/api/alerts/check")
    def r_check(req):
        now = clock()
        if now - state["last_manual"] < CHECK_MIN_GAP_S:
            raise HTTPError(429, "BUSY", "A check ran a moment ago. Try again in a few seconds.")
        state["last_manual"] = now
        out = engine.run()
        return {"ok": bool(out.get("ok")), "result": out, **db.counts(store, clock())}


def ai_recent(m, args: dict, clock: Callable[[], float] = time.time) -> dict:
    """``alerts_recent``: what the user's watchlists fired, newest first."""
    settings = db.settings(m.store)
    if not settings.get("ai_share", False):
        return {"error": "The user has switched off sharing alerts with the assistant (Alerts, Settings)."}
    try:
        hours = float(args.get("hours") if args.get("hours") is not None else 24)
        limit = int(args.get("limit") if args.get("limit") is not None else 20)
        min_sev = float(args.get("min_severity")) if args.get("min_severity") is not None else None
    except (TypeError, ValueError):
        return {"error": "hours, limit and min_severity must be numbers."}
    if not math.isfinite(hours):
        return {"error": "hours must be a number."}
    hours = min(720.0, max(0.25, hours))
    limit = min(100, max(1, limit))
    area = args.get("area")
    if area is not None and not isinstance(area, str):
        return {"error": "area must be text."}
    needle = (area or "").strip().lower()[:80]
    since = clock() - hours * 3600.0
    items = db.recent(m.store, since=since, limit=RECENT_LIMIT_MAX)
    out = []
    for a in items:
        if needle and needle not in (a["area"] or "").lower():
            continue
        if min_sev is not None and (a["severity"] or 0) < min_sev:
            continue
        ent = a.get("entity") or {}
        out.append({"time_utc": _utc(a["time"]), "area": a["area"], "rule": a["rule"], "title": a["title"],
                    "why": a["why"], "severity": a["severity"], "kind": a["kind"], "id": ent.get("id") or a["ref"],
                    "lat": a["lat"], "lon": a["lon"], "layer": ent.get("layer"), "held_back": a["folded"]})
        if len(out) >= limit:
            break
    names = [w["name"] for w in db.list_watchlists(m.store, clock())]
    return {"since_utc": _utc(since), "count": len(out), "watchlists": names, "alerts": out,
            "note": None if names else "The user has no watchlists yet."}
