"""Core API routes (CONTRACT.md sections 9 and 15.3): manifest, status, layer table, search,
geocoding, "What's here?", events, event history, favourites and case files."""

from __future__ import annotations

import json
import time

from . import geo, history
from .registry import App, HTTPError, Request

NOMINATIM = "https://nominatim.openstreetmap.org"
KIND_MAX = 40
TEXT_MAX = 300
HISTORY_LIMIT_MAX = 10000       # events per /api/core/history/events response


def _text(value, limit=TEXT_MAX) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value[:limit]


def register_core(app: App) -> None:
    m = app.module("core")
    m.app.modules["core"].ok = True
    net = app.net
    net.set_host_interval("nominatim.openstreetmap.org", 1.1)
    m.source("core.nominatim", "OpenStreetMap Nominatim (place search)", "https://nominatim.org/",
             "ODbL, OpenStreetMap contributors", refresh="on demand, max 1 request per second")

    # -- manifest and status -----------------------------------------------------------------------
    @m.route("GET", "/api/core/manifest")
    def manifest(req: Request):
        return app.manifest()

    @m.route("GET", "/api/core/status")
    def status(req: Request):
        sources = sorted(app.sources.values(), key=lambda s: (s.get("module") or "", s.get("id") or ""))
        return {
            "now": time.time(),
            "started_at": app.started_at,
            "modules": [{"name": i.name, "ok": i.ok, "error": i.error} for i in app.modules.values()],
            "sources": sources,
            "tasks": app.scheduler.info(),
            "datasets": app.datasets.info(),
            "net": dict(app.net.stats),
            "keys": app.config.key_status(),
            "history": app.history.info(),
        }

    @m.route("GET", "/api/core/layers")
    def layers(req: Request):
        out = []
        for spec in app.layer_specs:
            row = {k: v for k, v in spec.items() if not k.startswith("_")}
            fn = spec.get("_count")
            if fn is not None:
                try:
                    row["count"] = fn()
                except Exception:
                    row["count"] = None
            out.append(row)
        return {"layers": out}

    # -- search ------------------------------------------------------------------------------------
    @m.route("GET", "/api/core/search")
    def search(req: Request):
        q = req.arg("q").strip()
        if len(q) < 2:
            return {"q": q, "results": []}
        limit = req.arg("limit", 25, type=int, min=1, max=100)
        return {"q": q, "results": app.run_search(q[:120], limit)}

    @m.route("GET", "/api/core/geocode")
    def geocode(req: Request):
        """Forward geocoding via Nominatim. Only on explicit submit (their policy forbids type-ahead)."""
        q = req.arg("q").strip()[:200]
        if len(q) < 2:
            return {"q": q, "results": []}
        try:
            rows = net.get_json(f"{NOMINATIM}/search", params={"q": q, "format": "jsonv2", "limit": 8,
                                                                "addressdetails": 0},
                                headers={"Accept-Language": "en"}, ttl=86400, timeout=15)
            m.status("core.nominatim", ok=True)
        except Exception as err:
            m.status("core.nominatim", ok=False, error=str(err))
            raise HTTPError(502, "GEOCODER_UNAVAILABLE", "Place search is unavailable right now.") from None
        results = []
        for r in rows or []:
            try:
                lat = float(r["lat"])
                lon = float(r["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            bbox = None
            if r.get("boundingbox") and len(r["boundingbox"]) == 4:
                try:
                    s, n, w, e = (float(x) for x in r["boundingbox"])
                    bbox = [w, s, e, n]
                except ValueError:
                    bbox = None
            results.append({"kind": "place", "id": f"osm:{r.get('osm_type', '')}{r.get('osm_id', '')}",
                            "module": "core", "title": _text(r.get("name") or r.get("display_name"), 120),
                            "subtitle": _text(r.get("display_name"), 200), "lat": lat, "lon": lon,
                            "bbox": bbox, "category": r.get("category"), "type": r.get("type")})
        return {"q": q, "results": results}

    @m.route("GET", "/api/core/reverse")
    def reverse(req: Request):
        lat, lon = req.latlon()
        zoom = req.arg("zoom", 12, type=int, min=3, max=18)
        return _reverse(lat, lon, zoom)

    def _reverse(lat: float, lon: float, zoom: int = 12) -> dict:
        try:
            r = net.get_json(f"{NOMINATIM}/reverse",
                             params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "format": "jsonv2", "zoom": zoom},
                             headers={"Accept-Language": "en"}, ttl=86400, timeout=15)
            m.status("core.nominatim", ok=True)
        except Exception as err:
            m.status("core.nominatim", ok=False, error=str(err))
            return {"ok": False, "name": None, "address": {}}
        if not isinstance(r, dict) or r.get("error"):
            return {"ok": True, "name": None, "address": {}, "note": "open water or unnamed area"}
        addr = r.get("address") or {}
        return {"ok": True, "name": _text(r.get("display_name"), 300), "address": {k: _text(v, 120) for k, v in addr.items()},
                "country_code": (addr.get("country_code") or "").upper() or None}

    # -- "What's here?" ----------------------------------------------------------------------------
    @m.route("GET", "/api/core/whatshere")
    def whatshere(req: Request):
        lat, lon = req.latlon()
        radius = req.arg("radius_km", 10.0, type=float, min=0.1, max=500.0)
        limit = req.arg("limit", 12, type=int, min=1, max=50)
        place = _reverse(lat, lon, req.arg("zoom", 12, type=int, min=3, max=18)) if req.arg("geocode", True, type=bool) else None
        nearby = app.run_nearby(lat, lon, radius, limit)
        return {"lat": lat, "lon": lon, "radius_km": radius, "place": place, "nearby": nearby[: limit * 8]}

    @m.route("GET", "/api/core/nearby")
    def nearby(req: Request):
        lat, lon = req.latlon()
        radius = req.arg("radius_km", 25.0, type=float, min=0.1, max=1000.0)
        limit = req.arg("limit", 15, type=int, min=1, max=100)
        kind = req.arg("kind", None)
        items = app.run_nearby(lat, lon, radius, limit)
        if kind:
            kinds = set(kind.split(","))
            items = [i for i in items if i.get("kind") in kinds]
        return {"lat": lat, "lon": lon, "radius_km": radius, "results": items}

    # -- events (convergences, AI) -----------------------------------------------------------------
    @m.route("GET", "/api/core/events")
    def events(req: Request):
        hours = req.arg("hours", 24.0, type=float, min=0.25, max=24 * 30)
        kinds = req.arg("kinds", None)
        kind_set = set(kinds.split(",")) if kinds else None
        items = app.collect_events(time.time() - hours * 3600.0, kind_set)
        bbox = req.bbox("bbox", None)
        if bbox:
            items = [i for i in items if geo.in_bbox(float(i["lat"]), float(i["lon"]), bbox)]
        limit = req.arg("limit", 5000, type=int, min=1, max=50000)
        return {"hours": hours, "count": len(items), "events": items[:limit]}

    # -- event history (CONTRACT 15.3) -------------------------------------------------------------
    def _epoch_arg(req: Request, name: str, default):
        value = req.arg(name, None, type=float)
        if value is None:
            return default
        try:
            return history.epoch_seconds(value, name)
        except ValueError:
            raise HTTPError(400, "BAD_ARG", f'Query parameter "{name}" must be a time in epoch seconds.') from None

    def _history_window(req: Request) -> tuple[float, float | None]:
        """from (default: 24 hours ago) and to (default: no upper bound), epoch seconds."""
        since = _epoch_arg(req, "from", None)
        if since is None:
            since = time.time() - 86400.0
        until = _epoch_arg(req, "to", None)
        if until is not None and until < since:
            raise HTTPError(400, "BAD_ARG", 'Query parameter "to" must not be before "from".')
        return since, until

    @m.route("GET", "/api/core/history/events")
    def history_events(req: Request):
        since, until = _history_window(req)
        try:
            kinds = history.clean_kinds(req.arg("kinds", None))
        except ValueError as err:
            raise HTTPError(400, "BAD_ARG", f'Query parameter "kinds": {err}.') from None
        bbox = req.bbox("bbox", None)
        min_severity = req.arg("min_severity", None, type=float, min=0.0, max=10.0)
        limit = req.arg("limit", 5000, type=int, min=1, max=HISTORY_LIMIT_MAX)
        seen_since = req.arg("seen_since", None, type=float, min=0.0)
        items = app.history.events(since, until, kinds=kinds, bbox=bbox, min_severity=min_severity, limit=limit + 1,
                                   seen_since=seen_since)
        return {"from": since, "to": until, "count": min(len(items), limit), "truncated": len(items) > limit,
                "events": items[:limit]}

    @m.route("GET", "/api/core/history/summary")
    def history_summary(req: Request):
        since, until = _history_window(req)
        out = app.history.summary(since, until, bbox=req.bbox("bbox", None))
        return {"from": out.pop("since"), "to": out.pop("until"), **out}

    # -- favourites ----------------------------------------------------------------------------------
    @m.route("GET", "/api/core/favourites")
    def favourites(req: Request):
        kind = req.arg("kind", None)
        if kind:
            rows = app.store.query("SELECT * FROM favourites WHERE kind = ? ORDER BY created_at DESC", (kind,))
        else:
            rows = app.store.query("SELECT * FROM favourites ORDER BY kind, created_at DESC")
        for r in rows:
            r["payload"] = _loads(r["payload"])
        return {"favourites": rows}

    @m.route("POST", "/api/core/favourites", body_max=32768)
    def favourites_change(req: Request):
        action = req.field("action", type=str)
        kind = _text(req.field("kind", type=str), KIND_MAX)
        ref = _text(req.field("ref", type=str), 200)
        if not kind or not ref:
            raise HTTPError(400, "BAD_FIELD", "kind and ref are required.")
        if action == "remove":
            app.store.execute("DELETE FROM favourites WHERE kind = ? AND ref = ?", (kind, ref))
            return {"ok": True}
        if action != "add":
            raise HTTPError(400, "BAD_FIELD", 'action must be "add" or "remove".')
        lat = req.field("lat", None, type=float)
        lon = req.field("lon", None, type=float)
        if lat is not None and not geo.valid_latlon(lat, lon if lon is not None else 0):
            raise HTTPError(400, "BAD_FIELD", "lat/lon out of range.")
        payload = req.field("payload", {}, type=dict)
        app.store.execute(
            "INSERT INTO favourites(kind, ref, title, subtitle, lat, lon, layer, payload, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, ref) DO UPDATE SET title=excluded.title, "
            "subtitle=excluded.subtitle, lat=excluded.lat, lon=excluded.lon, layer=excluded.layer, "
            "payload=excluded.payload",
            (kind, ref, _text(req.field("title", "", type=str)), _text(req.field("subtitle", "", type=str)),
             lat, lon, _text(req.field("layer", "", type=str), 80), json.dumps(payload)[:20000], time.time()))
        return {"ok": True}

    # -- case files ----------------------------------------------------------------------------------
    @m.route("GET", "/api/core/casefiles")
    def casefiles(req: Request):
        rows = app.store.query(
            "SELECT c.*, (SELECT COUNT(*) FROM casefile_items i WHERE i.casefile_id = c.id) AS items "
            "FROM casefiles c ORDER BY c.updated_at DESC")
        return {"casefiles": rows}

    @m.route("GET", "/api/core/casefiles/{cid}")
    def casefile(req: Request):
        cid = _int(req.params["cid"])
        row = app.store.one("SELECT * FROM casefiles WHERE id = ?", (cid,))
        if row is None:
            raise HTTPError(404, "NOT_FOUND", "No such case file.")
        items = app.store.query("SELECT * FROM casefile_items WHERE casefile_id = ? ORDER BY added_at", (cid,))
        for it in items:
            it["payload"] = _loads(it["payload"])
        row["items"] = items
        return row

    @m.route("POST", "/api/core/casefiles", body_max=262144)
    def casefiles_change(req: Request):
        action = req.field("action", type=str)
        now = time.time()
        if action == "create":
            title = _text(req.field("title", type=str), 200).strip() or "Untitled case"
            cur = app.store.execute("INSERT INTO casefiles(title, notes, created_at, updated_at) VALUES(?,?,?,?)",
                                    (title, "", now, now))
            return {"ok": True, "id": cur.lastrowid}
        cid = _int(req.field("id"))
        if app.store.one("SELECT id FROM casefiles WHERE id = ?", (cid,)) is None:
            raise HTTPError(404, "NOT_FOUND", "No such case file.")
        if action == "rename":
            title = _text(req.field("title", type=str), 200).strip() or "Untitled case"
            app.store.execute("UPDATE casefiles SET title = ?, updated_at = ? WHERE id = ?", (title, now, cid))
        elif action == "notes":
            notes = _text(req.field("notes", type=str), 100000)
            app.store.execute("UPDATE casefiles SET notes = ?, updated_at = ? WHERE id = ?", (notes, now, cid))
        elif action == "delete":
            app.store.execute("DELETE FROM casefiles WHERE id = ?", (cid,))
        elif action == "add_item":
            item = req.field("item", type=dict)
            kind = _text(item.get("kind"), KIND_MAX)
            ref = _text(item.get("ref") or item.get("id"), 200)
            if not kind or not ref:
                raise HTTPError(400, "BAD_FIELD", "item.kind and item.ref are required.")
            lat, lon = item.get("lat"), item.get("lon")
            if not geo.valid_latlon(lat, lon):
                lat = lon = None
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            cur = app.store.execute(
                "INSERT INTO casefile_items(casefile_id, kind, ref, title, subtitle, lat, lon, layer, payload, note, added_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cid, kind, ref, _text(item.get("title")), _text(item.get("subtitle")), lat, lon,
                 _text(item.get("layer"), 80), json.dumps(payload)[:50000], _text(item.get("note"), 5000), now))
            app.store.execute("UPDATE casefiles SET updated_at = ? WHERE id = ?", (now, cid))
            return {"ok": True, "item_id": cur.lastrowid}
        elif action == "remove_item":
            app.store.execute("DELETE FROM casefile_items WHERE id = ? AND casefile_id = ?",
                              (_int(req.field("item_id")), cid))
            app.store.execute("UPDATE casefiles SET updated_at = ? WHERE id = ?", (now, cid))
        elif action == "item_note":
            app.store.execute("UPDATE casefile_items SET note = ? WHERE id = ? AND casefile_id = ?",
                              (_text(req.field("note", type=str), 5000), _int(req.field("item_id")), cid))
            app.store.execute("UPDATE casefiles SET updated_at = ? WHERE id = ?", (now, cid))
        else:
            raise HTTPError(400, "BAD_FIELD", "Unknown action.")
        return {"ok": True}

    # -- core search provider: favourites and case files ---------------------------------------------
    def search_saved(q: str, limit: int):
        like = f"%{q}%"
        rows = app.store.query("SELECT * FROM favourites WHERE kind != 'my_place' AND (title LIKE ? OR subtitle LIKE ?) LIMIT ?",
                               (like, like, limit))
        out = []
        for r in rows:
            out.append({"kind": r["kind"], "id": r["ref"], "title": r["title"], "subtitle": "Favourite",
                        "lat": r["lat"], "lon": r["lon"], "layer": r["layer"], "score": 0.55,
                        "module": "core", "payload": _loads(r["payload"])})
        return out

    m.search(search_saved)


def _int(value) -> int:
    """A database id: an int, an integral float or ASCII digits, within SQLite's integer range."""
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError
            value = int(value)
        elif isinstance(value, str):
            text = value.strip()
            if not (text.isascii() and text.lstrip("-").isdigit()) or len(text) > 19:
                raise ValueError
            value = int(text)
        elif not isinstance(value, int):
            raise ValueError
        if not -(2 ** 63) < value < 2 ** 63:
            raise ValueError
        return value
    except (TypeError, ValueError, OverflowError):
        raise HTTPError(400, "BAD_FIELD", "Expected a whole number id.") from None


def _loads(text):
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}
