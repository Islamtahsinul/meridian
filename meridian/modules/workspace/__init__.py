"""Workspaces and My places: named map states with built-in presets, and the user's own places.

Backend half of the Workspaces team's module (Programme 2, phase 3b). Saved workspaces and places live
in this module's own tables (``workspace_saved``, ``workspace_places``) in Meridian's database on this
computer. The browser half (web/js/layers/workspace.js) captures and applies map states, keeps the
deep-link format in step (web/js/core/deeplink.js), offers places to Alerts through Alerts' own API and
exports what is on screen. Nothing here downloads anything or needs a key. Places are never given to
the assistant or to MCP tools. See docs/modules/workspace.md.

Endpoints (standard guards on every POST: Origin, X-Meridian, JSON):
    GET  /api/workspace/workspaces            {workspaces, presets, max}
    GET  /api/workspace/workspaces/{wid}      one saved workspace, or a preset ("builtin:maritime")
    POST /api/workspace/workspaces            {action: create | update | rename | delete | import, ...}
    GET  /api/workspace/export                every saved workspace as one importable JSON document
    GET  /api/workspace/places                {places, max}
    POST /api/workspace/places                {action: create | update | delete | watchlist, ...}
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from meridian.registry import HTTPError

from . import db
from .model import PRESETS, InputError, preset

EXPORT_KIND = "meridian-workspaces"


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


def _bad(err: Exception) -> HTTPError:
    if isinstance(err, db.NameTaken):
        return HTTPError(409, "NAME_TAKEN", str(err))
    text = str(err).strip() or "That request is not valid."
    return HTTPError(400, "BAD_FIELD", text if text.endswith(".") else text + ".")


def register(m) -> None:
    store = m.store
    db.setup(store)
    clock = time.time

    m.layer("workspace.places", "My places", "Saved",
            description="Your own named places (home, work, family), kept in Meridian's database on this computer. "
                        "Add them in Workspaces, My places.",
            source="Your places, stored on this computer only", refresh="when you change them",
            use="Fly to a place, or watch it in Alerts.")

    def workspace_or_404(wid: int) -> dict:
        w = db.get_workspace(store, wid)
        if w is None:
            raise HTTPError(404, "NOT_FOUND", "No such workspace.")
        return w

    def place_or_404(pid: int) -> dict:
        p = db.get_place(store, pid)
        if p is None:
            raise HTTPError(404, "NOT_FOUND", "No such place.")
        return p

    # -- workspaces ------------------------------------------------------------------------------
    @m.route("GET", "/api/workspace/workspaces")
    def r_workspaces(req):
        return {"workspaces": db.list_workspaces(store), "presets": [dict(p) for p in PRESETS],
                "max": db.MAX_WORKSPACES}

    @m.route("GET", "/api/workspace/workspaces/{wid}")
    def r_workspace(req):
        raw = req.params["wid"]
        if raw.startswith("builtin:"):
            p = preset(raw)
            if p is None:
                raise HTTPError(404, "NOT_FOUND", "No such preset.")
            return dict(p)
        return workspace_or_404(_int(raw))

    @m.route("POST", "/api/workspace/workspaces", body_max=512 * 1024)
    def r_workspaces_change(req):
        action = req.field("action", type=str)
        now = clock()
        try:
            if action == "create":
                wid = db.create_workspace(store, req.field("name"), req.field("state", type=dict), now)
                return {"ok": True, "id": wid, "workspace": db.get_workspace(store, wid)}
            if action == "import":
                body = req.json()
                items = body.get("workspaces")
                doc = body.get("file")
                if items is None and isinstance(doc, dict):
                    if doc.get("kind") not in (EXPORT_KIND, None):
                        raise InputError("This file is not a Meridian workspaces export.")
                    items = doc.get("workspaces")
                out = db.import_workspaces(store, items, now)
                return {"ok": True, **out, "workspaces": db.list_workspaces(store)}
            if action in ("update", "rename", "delete"):
                wid = _int(req.field("id"))
                workspace_or_404(wid)
                if action == "delete":
                    db.delete_workspace(store, wid)
                    return {"ok": True}
                if action == "rename":
                    db.update_workspace(store, wid, now, name=req.field("name"))
                else:
                    db.update_workspace(store, wid, now, name=req.field("name", None),
                                        state=req.field("state", type=dict))
                return {"ok": True, "workspace": db.get_workspace(store, wid)}
        except InputError as err:
            raise _bad(err) from None
        raise HTTPError(400, "BAD_FIELD", 'action must be "create", "update", "rename", "delete" or "import".')

    @m.route("GET", "/api/workspace/export")
    def r_export(req):
        items = [{"name": w["name"], "state": w["state"]} for w in reversed(db.list_workspaces(store))]
        return {"kind": EXPORT_KIND, "v": 1,
                "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "count": len(items), "workspaces": items}

    # -- places ----------------------------------------------------------------------------------
    @m.route("GET", "/api/workspace/places")
    def r_places(req):
        return {"places": db.list_places(store), "max": db.MAX_PLACES}

    @m.route("POST", "/api/workspace/places", body_max=16 * 1024)
    def r_places_change(req):
        action = req.field("action", type=str)
        now = clock()
        try:
            if action == "create":
                pid = db.create_place(store, req.field("place", type=dict), now)
                return {"ok": True, "id": pid, "place": db.get_place(store, pid)}
            if action in ("update", "delete", "watchlist"):
                pid = _int(req.field("id"))
                place_or_404(pid)
                if action == "delete":
                    db.delete_place(store, pid)
                    return {"ok": True}
                if action == "update":
                    db.update_place(store, pid, req.field("place", type=dict), now)
                else:
                    raw = req.field("watchlist_id", None)
                    db.set_place_watchlist(store, pid, None if raw is None else _int(raw, "watchlist_id"), now)
                return {"ok": True, "place": db.get_place(store, pid)}
        except InputError as err:
            raise _bad(err) from None
        raise HTTPError(400, "BAD_FIELD", 'action must be "create", "update", "delete" or "watchlist".')
