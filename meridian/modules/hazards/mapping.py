"""Copernicus EMS rapid mapping activations: polling, the detail cache, the map payload, entities,
details, GDACS links, events, search and the AI tool. Parsers are in ``cems.py``.

Polling: the list (about 17 KB for 60 activations) every 30 minutes while the layer is in use and
every 2 hours otherwise; one detail request (about 11 KB) per open or recent activation when its
``lastUpdate`` changes. Older activations are fetched only when a card asks for them.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

from meridian import geo
from meridian.net import NetError

from . import cems, gdacs
from .common import LAYER_DISASTERS, LAYER_MAPPING, clean, fc, feature, norm, point, simplify_geometry

DETAIL_DAYS = 45          # keep details fresh for open activations and those activated in this window
LIST_LIMIT = 60


def _date(t: float | None) -> str:
    if not t:
        return "date unknown"
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%d %b %Y").lstrip("0")


def _iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None


def entity(a: dict) -> dict:
    state = "open" if not a.get("closed") else "closed"
    return {"kind": "emergency_mapping", "id": a["id"], "title": a["name"],
            "subtitle": f"Copernicus emergency mapping, {state}, activated {_date(a.get('activation_time'))}",
            "lat": a.get("lat"), "lon": a.get("lon"), "layer": LAYER_MAPPING,
            "time": int(a.get("activation_time") or 0), "zoom": 9, "source": "Copernicus EMS"}


class Mapping:
    def __init__(self, svc):
        self.svc = svc
        self.net = svc.net
        self.store = svc.store
        self.log = svc.log
        self.items: list[dict] = []               # the list, newest activation first
        self.by_code: dict[str, dict] = {}
        self.details: dict[str, dict] = {}        # code -> parsed detail
        self._seen_update: dict[str, str] = {}    # code -> list lastUpdate when the detail was fetched
        self.version = 0
        self._extent: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._extent_lock = threading.Lock()

    # ------------------------------------------------------------------ storage
    def ensure_tables(self) -> None:
        self.store.script(
            "CREATE TABLE IF NOT EXISTS hazards_cems (code TEXT PRIMARY KEY, data TEXT NOT NULL, last_update TEXT, "
            "fetched_at REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS hazards_plus_state (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at REAL NOT NULL)")
        details = {}
        seen = {}
        for row in self.store.query("SELECT code, data, last_update FROM hazards_cems ORDER BY fetched_at DESC LIMIT 400"):
            try:
                details[row["code"]] = json.loads(row["data"])
                seen[row["code"]] = row["last_update"] or ""
            except ValueError:
                continue
        self.details, self._seen_update = details, seen
        row = self.store.one("SELECT v FROM hazards_plus_state WHERE k = 'cems_list'")
        if row:
            try:
                items = json.loads(row["v"])
                self._set_items(items if isinstance(items, list) else [])
            except ValueError:
                pass

    def _set_items(self, items: list[dict]) -> None:
        self.items = items
        self.by_code = {a["code"]: a for a in items}
        self.version += 1

    def housekeeping(self) -> None:
        self.store.execute("DELETE FROM hazards_cems WHERE fetched_at < ?", (time.time() - 400 * 86400,))

    # ------------------------------------------------------------------ polling
    def refresh(self):
        data = self.net.get_json(cems.LIST_URL, params={"limit": LIST_LIMIT}, timeout=25, retries=1)
        items = cems.parse_list(data)
        if not items:
            raise RuntimeError("Copernicus EMS answered without any activations")
        now = time.time()
        todo = [a for a in items
                if (not a["closed"] or (a["activation_time"] or 0) >= now - DETAIL_DAYS * 86400)
                and (a["code"] not in self.details or self._seen_update.get(a["code"]) != a["last_update_raw"])]
        errors = []
        deadline = time.time() + 40
        for a in todo[:12]:
            if time.time() > deadline:
                break
            try:
                self.fetch_detail(a["code"], seen_update=a["last_update_raw"])
            except (NetError, ValueError) as err:
                errors.append(f"{a['code']}: {str(err)[:80]}")
        self._set_items(items)
        try:
            self.store.execute("INSERT OR REPLACE INTO hazards_plus_state(k, v, updated_at) VALUES('cems_list', ?, ?)",
                               (json.dumps(items), now))
        except Exception as err:   # the snapshot is a convenience for the next start-up
            self.log.debug("cems list snapshot not saved: %s", err)
        open_n = sum(1 for a in items if not a["closed"])
        note = f"{open_n} open, {len(items)} listed (newest {items[0]['code']})"
        if errors:
            note += f"; {len(errors)} detail requests failed"
        return len(items), note

    def fetch_detail(self, code: str, seen_update: str | None = None, timeout: float = 20, retries: int = 1) -> dict:
        data = self.net.get_json(cems.DETAIL_URL, params={"code": code}, timeout=timeout, retries=retries)
        d = cems.parse_detail(data)
        if d is None or d["code"] != code:
            raise ValueError("no activation record in the answer")
        if seen_update is None:
            seen_update = (self.by_code.get(code) or {}).get("last_update_raw") or ""
        details = dict(self.details)
        details[code] = d
        self.details = details
        self._seen_update[code] = seen_update
        self.store.execute("INSERT OR REPLACE INTO hazards_cems(code, data, last_update, fetched_at) VALUES(?,?,?,?)",
                           (code, json.dumps(d), seen_update, time.time()))
        self.version += 1
        return d

    # ------------------------------------------------------------------ lookups
    def activation(self, code: str) -> dict | None:
        """The list record merged with the detail record (the list is fresher for state and counts)."""
        base = self.by_code.get(code)
        det = self.details.get(code)
        if base is None and det is None:
            return None
        out = dict(det or base)
        if base and det:
            for k in ("closed", "last_update", "last_update_raw", "name", "countries", "category", "cat"):
                if base.get(k) is not None:
                    out[k] = base[k]
            out["n_products"] = max(base.get("n_products") or 0, det.get("n_products") or 0)
            out["n_aois"] = max(base.get("n_aois") or 0, det.get("n_aois") or 0)
            if out.get("lat") is None:
                out["lat"], out["lon"] = base.get("lat"), base.get("lon")
        out["detail"] = det is not None
        return out

    def all_known(self) -> list[dict]:
        """List records, plus older activations known only from opened cards."""
        out = list(self.items)
        have = {a["code"] for a in out}
        for code, d in list(self.details.items()):
            if code not in have:
                out.append(d)
        return out

    def severity(self, a: dict) -> int:
        sev = 5
        if a.get("charter_number"):
            sev = 6
        gid = cems.gdacs_event_id(a.get("gdacs_id"))
        g = self.svc.gdacs_events.get(gid) if gid else None
        if g and g.get("alert") in ("Orange", "Red"):
            sev = max(sev, 6 if g["alert"] == "Orange" else 7)
        return sev

    def for_gdacs(self, e: dict) -> list[dict]:
        """Activations linked to a GDACS event by its id (shown on the GDACS card)."""
        out = []
        for a in self.all_known():
            if cems.gdacs_event_id(a.get("gdacs_id")) == e.get("id"):
                out.append(entity(self.activation(a["code"]) or a))
        return out[:4]

    def gdacs_links(self, a: dict) -> list[dict]:
        """The GDACS event this activation names, and same-hazard GDACS alerts nearby in time and space."""
        out = []
        gid = cems.gdacs_event_id(a.get("gdacs_id"))
        if gid:
            g = self.svc.gdacs_events.get(gid)
            if g:
                ent = _gdacs_entity(g)
            else:
                etype = gid.split(":")[1]
                ent = {"kind": gdacs.TYPE_KIND.get(etype, "disaster"), "id": gid,
                       "title": f"GDACS {gdacs.TYPE_NAME.get(etype, 'event').lower()} event {gid.split(':')[2]}",
                       "subtitle": "Linked by Copernicus EMS", "lat": a.get("lat"), "lon": a.get("lon"),
                       "layer": LAYER_DISASTERS}
            out.append({"match": "linked", "alert": g.get("alert") if g else None, "entity": ent})
        want = cems.GDACS_FOR.get(a.get("cat") or "")
        if want and a.get("lat") is not None:
            t0 = a.get("event_time") or a.get("activation_time") or 0
            near = []
            for g in list(self.svc.gdacs_events.values()):
                if g["id"] == gid or g.get("type") != want:
                    continue
                d = geo.haversine_km(a["lat"], a["lon"], g["lat"], g["lon"])
                if d > 400:
                    continue
                t = g.get("from") or g.get("to") or 0
                if t0 and t and abs(t - t0) > 14 * 86400:
                    continue
                near.append((d, g))
            near.sort(key=lambda x: x[0])
            for d, g in near[:3]:
                out.append({"match": "nearby", "alert": g.get("alert"), "distance_km": round(d), "entity": _gdacs_entity(g)})
        return out

    # ------------------------------------------------------------------ map payload
    def map_fc(self, days: int, open_only: bool) -> dict:
        now = time.time()
        since = now - days * 86400
        polys, pts = [], []
        open_n = 0
        for a in self.items:
            if a["closed"] and (open_only or (a["activation_time"] or 0) < since):
                continue
            full = self.activation(a["code"]) or a
            if full.get("lat") is None:
                continue
            op = 0 if a["closed"] else 1
            open_n += op
            age = round((now - (a["activation_time"] or now)) / 86400.0, 1)
            pts.append(feature(point(full["lon"], full["lat"]), {
                "id": a["id"], "role": "point", "code": a["code"], "name": a["name"], "cat": a["cat"],
                "category": a["category"], "open": op, "t": int(a["activation_time"] or 0), "age": age,
                "label": clean(a["name"], 44), "gd": 1 if a.get("gdacs_id") else 0}))
            det = self.details.get(a["code"])
            if det and not det.get("sensitive"):
                for aoi in det.get("aois") or []:
                    if aoi.get("geometry"):
                        polys.append(feature(aoi["geometry"], {"id": a["id"], "role": "aoi", "code": a["code"],
                                                               "cat": a["cat"], "open": op, "name": aoi["name"]}))
        pts.sort(key=lambda f: (f["properties"]["open"], f["properties"]["t"]))
        return fc(polys + pts, count=len(pts), open=open_n, listed=len(self.items),
                  source=self.svc.source_info("cems"))

    # ------------------------------------------------------------------ details
    def detail(self, svc, eid: str) -> dict | None:
        code = cems.valid_code(eid.split(":", 1)[1] if ":" in eid else "")
        if not code:
            return None
        if code not in self.details:
            try:
                self.fetch_detail(code, timeout=8, retries=0)
            except (NetError, ValueError):
                pass
        a = self.activation(code)
        if a is None:
            return None
        data = dict(a)
        data["open"] = not a.get("closed")
        data["gdacs"] = self.gdacs_links(a)
        data["severity"] = self.severity(a)
        year = time.gmtime(a.get("activation_time") or time.time()).tm_year
        data["credit"] = f"Copernicus Emergency Management Service (© {year} European Union), {code}"
        data["terms"] = cems.TERMS_URL
        boxes = []
        if a.get("sensitive"):
            data["aois"] = [dict(x, geometry=None, bbox=None) for x in a.get("aois") or []]
            data["has_extent"] = False
        else:
            boxes = [x["bbox"] for x in a.get("aois") or [] if x.get("bbox")]
            data["has_extent"] = bool(cems.observed_layers(a)) if a.get("detail") else False
        if boxes:
            data["bbox"] = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                            max(b[3] for b in boxes)]
        return {"type": "cems", "entity": entity(a), "data": data}

    def extent(self, code: str) -> dict:
        """The mapped flood or burnt area (observed event layers of the newest delivered products)."""
        with self._extent_lock:
            hit = self._extent.get(code)
            if hit and time.time() - hit[0] < 6 * 3600:
                return hit[1]
        a = self.activation(code)
        if a is None or not a.get("detail"):
            if a is None or code not in self.details:
                self.fetch_detail(code, timeout=8, retries=0)
            a = self.activation(code)
        if a is None or a.get("sensitive"):
            return fc([], note="No mapped area is published for this activation.")
        layers = cems.observed_layers(a)
        feats = []
        budget = 150_000
        failed = 0
        for layer in layers:
            url = cems.viewer_json(layer["url"], code)      # checked again: only the viewer bucket, this activation
            if not url:
                continue
            try:
                data = self.net.get_json(url, timeout=30, retries=0, max_bytes=30_000_000, allow_redirects=False)
            except (NetError, ValueError):
                failed += 1
                continue
            for f in (data.get("features") if isinstance(data, dict) else None) or []:
                if budget <= 0 or not isinstance(f, dict):
                    break
                g = simplify_geometry(f.get("geometry"), 0.0002, 5)
                if not g or g["type"] not in ("Polygon", "MultiPolygon"):
                    continue
                budget -= _vertices(g)
                p = f.get("properties") or {}
                feats.append(feature(g, {"role": "extent", "aoi": layer["aoi"], "product": layer["product"],
                                         "what": clean(p.get("notation") or p.get("obj_desc"), 60) or "Mapped area"}))
        out = fc(feats, count=len(feats), layers=len(layers), failed=failed, truncated=budget <= 0,
                 note=None if feats else ("The map layers could not be fetched." if failed else
                                          "No delivered map shows the affected area yet."))
        if feats or not failed:
            with self._extent_lock:
                self._extent[code] = (time.time(), out)
                while len(self._extent) > 8:
                    self._extent.popitem(last=False)
        return out

    # ------------------------------------------------------------------ discovery
    def events(self, since_ts: float) -> list[dict]:
        out = []
        for a in self.items:
            t = a.get("activation_time")
            if not t or t < since_ts:
                continue
            full = self.activation(a["code"]) or a
            if full.get("lat") is None:
                continue
            e = entity(full)
            e.update(time=int(t), severity=self.severity(full), category=a["category"], countries=a["countries"],
                     code=a["code"], open=not a["closed"])
            out.append(e)
        return out

    def search(self, q: str, limit: int) -> list[dict]:
        nq = norm(q)
        if len(nq) < 3:
            return []
        out = []
        for a in self.all_known():
            if norm(a["code"]) == nq:
                sc = 0.78
            else:
                n = norm(a["name"])
                sc = 0.5 if n.startswith(nq) else 0.4 if nq in n else None
                if sc is None and any(nq in norm(c) for c in a.get("countries") or []):
                    sc = 0.3
                if sc is None and nq in norm(a.get("category")):
                    sc = 0.26
            if sc is None:
                continue
            full = self.activation(a["code"]) or a
            if full.get("lat") is None:
                continue
            ent = entity(full)
            ent["score"] = sc + (0.08 if not a.get("closed") else 0.0)
            out.append(ent)
        out.sort(key=lambda e: -e["score"])
        return out[:limit]

    def nearby(self, lat: float, lon: float, radius_km: float, limit: int) -> list[dict]:
        out = []
        for a in self.items:
            full = self.activation(a["code"]) or a
            if full.get("lat") is None:
                continue
            d = geo.haversine_km(lat, lon, full["lat"], full["lon"])
            inside = False
            if not full.get("sensitive"):
                for aoi in full.get("aois") or []:
                    b = aoi.get("bbox")
                    if b and b[0] <= lon <= b[2] and b[1] <= lat <= b[3] and geo.point_in_geometry(lon, lat, aoi["geometry"]):
                        inside = True
                        break
            if inside or d <= max(radius_km, 100.0):
                ent = entity(full)
                ent["distance_km"] = 0.0 if inside else round(d, 2)
                out.append(ent)
        out.sort(key=lambda e: e["distance_km"])
        return out[:limit]

    def tool(self, args: dict) -> dict:
        days = _num(args.get("days"), 30, 1, 400, "days")
        country = str(args.get("country") or "").strip()[:80]
        category = str(args.get("category") or "").strip()[:40]
        open_only = bool(args.get("open_only"))
        limit = int(_num(args.get("limit"), 20, 1, 60, "limit"))
        code = cems.valid_code(args.get("code")) if args.get("code") else None
        if args.get("code") and not code:
            raise ValueError("code must look like EMSR936")
        st = self.svc.state["cems"]
        if not self.items and not st.lock.locked():
            self.svc.run("cems")
        now = time.time()
        rows = []
        if code:
            if code not in self.details:
                try:
                    self.fetch_detail(code, timeout=8, retries=0)
                except (NetError, ValueError):
                    pass
            a = self.activation(code)
            rows = [a] if a else []
        else:
            for a in self.all_known():
                if open_only and a.get("closed"):
                    continue
                if a.get("closed") and (a.get("activation_time") or 0) < now - days * 86400:
                    continue          # open activations are always listed, closed ones within the window
                if country and not any(norm(country) in norm(c) for c in a.get("countries") or []) \
                        and norm(country) not in norm(a.get("name")):
                    continue
                if category and norm(category) not in norm(a.get("category")) and norm(category) not in norm(a.get("cat")):
                    continue
                rows.append(self.activation(a["code"]) or a)
            rows.sort(key=lambda a: (a.get("closed", False), -(a.get("activation_time") or 0)))
        out = []
        for a in rows[:limit]:
            item = {"code": a["code"], "name": a["name"], "category": a.get("category"), "countries": a.get("countries"),
                    "open": not a.get("closed"), "event_time_utc": _iso(a.get("event_time")),
                    "activation_time_utc": _iso(a.get("activation_time")), "last_update_utc": _iso(a.get("last_update")),
                    "lat": a.get("lat"), "lon": a.get("lon"), "areas_of_interest": a.get("n_aois"),
                    "products": a.get("n_products"), "gdacs_event": cems.gdacs_event_id(a.get("gdacs_id")),
                    "page": a.get("page"), "entity": entity(a)}
            if a.get("detail"):
                item["reason"] = clean(a.get("reason"), 600)
                item["requested_by"] = a.get("requester")
                item["charter_activation"] = a.get("charter_number")
                item["sensitive"] = a.get("sensitive")
                item["areas"] = [{"name": x["name"], "products": [
                    {"type": p["label"], "status": p["status"], "maps": p["maps"], "delivered_utc": _iso(p.get("delivered_at")),
                     "expected_utc": None if p["delivered"] else _iso(p.get("expected"))} for p in x["products"]]}
                    for x in (a.get("aois") or [])[:12]]
                item["summary_figures"] = [f"{s['label']}: {s['value']:g}{(' ' + s['unit']) if s['unit'] else ''}"
                                           for s in a.get("stats") or []]
            out.append(item)
        return {"source": "Copernicus Emergency Management Service, rapid mapping", "count": len(out),
                "open_now": sum(1 for a in self.items if not a.get("closed")),
                "listed": len(self.items), "activations": out,
                "note": "Each activation is a request for satellite emergency maps. Products: first estimate, "
                        "delineation (affected area) and grading (damage)."}


def _gdacs_entity(g: dict) -> dict:
    from .entities import gdacs_entity
    return gdacs_entity(g)


def _vertices(g: dict) -> int:
    n = 0
    polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
    for poly in polys:
        for ring in poly:
            n += len(ring)
    return n


def _num(v, default, lo, hi, name="value"):
    if v in (None, ""):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if x != x:
        raise ValueError(f"{name} must be a number")
    return max(lo, min(hi, x))
