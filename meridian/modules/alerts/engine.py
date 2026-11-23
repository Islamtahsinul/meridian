"""The alerts engine: every two minutes, match watchlist rules against the event history.

How an event becomes an alert (one rule of one watchlist at a time):

1. Candidates come from ``app.history.events`` (CONTRACT.md 15.3) for the last 7 days of event time.
   Only rows the history rewrote since the rule's last check are looked at (``last_seen`` at or after
   the watermark, which is the time of the last *completed* snapshot read before the query, so a
   snapshot still being written is looked at again next time). Newness comes from ``first_seen``:
   late events can carry an old ``time``.
2. A candidate matches when the preset's test passes and it lies in the area
   (``geo.point_in_geometry``, great-circle distance for circles; storm cones, tracks and wind areas,
   GPS cell outlines and every place an outbreak names also count).
3. Dedupe per (watchlist, rule, module, kind, id) in ``alerts_seen``. The first check of a new or
   changed rule absorbs what already matched (events first seen before the rule started) without
   alerting. After that, the first match alerts ("new", or "entered" for things that move); a known
   event alerts again only when its severity rises by 2 or more, when a storm's centre reaches the
   area after its cone did, or when a recurring entity (an aircraft, a ship, a GPS cell) comes back
   after a quiet gap (``rules.episode_gap_s``). GPS cells that repeat every day therefore stay quiet.
4. At most ``rate_limit`` alerts an hour per watchlist (10 by default, most severe first); the rest are
   kept, folded into one "N more alerts" digest per hour.

The engine reads events and never publishes any (CONTRACT.md 15.4: alerts are not an events provider).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable

from . import db
from . import rules as R
from . import wording
from .areas import Area, bbox_polygon, circle_polygon

CHECK_EVERY_S = 120.0
LOOKBACK_S = 7 * 86400.0
QUERY_LIMIT = 50000
RATE_WINDOW_S = 3600.0
ACTIVE_WINDOW_S = 1500.0          # "matching now": reported within the last 25 minutes
STALE_S = 48 * 3600.0             # a known event that changes after this long is not news
WATERMARK_SLACK_S = 1.0           # history returns last_seen rounded to whole seconds

log = logging.getLogger("meridian.alerts")


def _float(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def entity_of(ev: dict) -> dict:
    """The entity an alert opens (ctx.entities.open): the event's own fields."""
    e = {"kind": ev.get("kind"), "id": str(ev.get("id")), "title": ev.get("title") or "",
         "subtitle": ev.get("subtitle") or "", "lat": ev.get("lat"), "lon": ev.get("lon"),
         "layer": ev.get("layer") or None, "time": ev.get("time")}
    zoom = _float(ev.get("zoom"))
    if zoom is not None and 1 <= zoom <= 18:
        e["zoom"] = zoom
    bbox = ev.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4 and all(_float(v) is not None for v in bbox):
        e["bbox"] = bbox
    return e


def decide(ev: dict, rec: dict | None, *, baseline: bool, watch_from: float, last_check: float | None,
           level: int, now: float | None = None) -> tuple[str | None, dict, float | None]:
    """(reason or None, the dedupe record to store, extra) for one matching event.

    ``extra`` is the previous severity for "escalated" and the quiet gap in seconds for "again". With
    ``now``, a known event whose own time is more than 48 hours old (a re-scored old report) updates its
    record quietly instead of alerting "matched" or "escalated"; first reports always alert."""
    sev = _float(ev.get("severity")) or 0.0
    t = _float(ev.get("time"))
    fs = _float(ev.get("first_seen")) or 0.0
    stale = now is not None and t is not None and now - t > STALE_S
    if rec is None:
        new = {"severity": sev, "event_time": t, "level": level, "alerted": 0}
        if baseline and fs <= watch_from:
            return None, new, None                       # already there when the rule started
        ref = last_check if last_check is not None else watch_from
        if fs > ref:
            new["alerted"] = 1
            return "new", new, None
        if ev.get("kind") in R.MOVING_KINDS:
            new["alerted"] = 1
            return "entered", new, None
        if stale:
            return None, new, None
        new["alerted"] = 1
        return "matched", new, None
    prev_sev = _float(rec.get("severity")) or 0.0
    prev_t = _float(rec.get("event_time"))
    prev_level = int(rec.get("level") or 0)
    reason, extra = None, None
    gap = R.episode_gap_s(ev)
    if gap and t is not None and prev_t is not None and t - prev_t >= gap:
        reason, extra = "again", t - prev_t
    elif sev >= prev_sev + R.ESCALATE_BY - 1e-9:
        if stale:
            times = [x for x in (t, prev_t) if x is not None]
            return None, {"severity": sev, "event_time": max(times) if times else None,
                          "level": max(level, prev_level), "alerted": int(rec.get("alerted") or 0)}, None
        reason, extra = "escalated", prev_sev
    elif level > prev_level:
        reason = "closer"
    times = [x for x in (t, prev_t) if x is not None]
    new = {"severity": sev if reason else prev_sev, "event_time": max(times) if times else None,
           "level": max(level, prev_level), "alerted": 1 if reason else int(rec.get("alerted") or 0)}
    return reason, new, extra


# ---------------------------------------------------------------------------------------------
# optional reads of the hazards module (storm cones, outbreak places); never required
# ---------------------------------------------------------------------------------------------
def _hazards_service(app) -> Any:
    info = getattr(app, "modules", {}).get("hazards")
    if info is None or not getattr(info, "ok", False):
        return None
    try:
        from meridian.modules import hazards as hz       # documented: get_service() may be read by others
        return hz.get_service()
    except Exception:
        return None


def hazards_storm(app, ev: dict) -> dict | None:
    """Forecast geometry of a storm event: {cone, buffers, track, points} (any may be empty)."""
    svc = _hazards_service(app)
    if svc is None:
        return None
    try:
        s = svc.storm(str(ev.get("id")))
        if not s:
            return None
        pts = [[p["lon"], p["lat"]] for p in s.get("track") or [] if p.get("forecast")]
        line = s.get("track_line") or {}
        coords = line.get("coordinates") if isinstance(line, dict) and line.get("type") == "LineString" else None
        return {"cone": s.get("cone"), "buffers": [b.get("geometry") for b in s.get("buffers") or [] if isinstance(b, dict)],
                "points": pts, "track": coords or []}
    except Exception:
        return None


def hazards_outbreak_places(app, ev: dict) -> list[tuple[float, float]]:
    """Every place a WHO report names (the event itself carries only the first one)."""
    svc = _hazards_service(app)
    if svc is None:
        return []
    try:
        o = svc.outbreak(str(ev.get("id")))
        return [(float(p["lat"]), float(p["lon"])) for p in (o or {}).get("places") or []
                if p.get("lat") is not None and p.get("lon") is not None]
    except Exception:
        return []


class Engine:
    def __init__(self, m, *, clock: Callable[[], float] = time.time):
        self.m = m
        self.store = m.store
        self.clock = clock
        self.lock = threading.Lock()
        self.last: dict = {}
        self.runs = 0
        self._areas: dict[int, tuple[Any, Area]] = {}
        self.storm_geometry: Callable[[dict], dict | None] = lambda ev: hazards_storm(m.app, ev)
        self.outbreak_places: Callable[[dict], list] = lambda ev: hazards_outbreak_places(m.app, ev)

    # -- public ----------------------------------------------------------------------------------
    def run(self, now: float | None = None) -> dict:
        """One check of every enabled rule. Never raises; returns a summary (also kept in ``last``)."""
        if not self.lock.acquire(blocking=False):
            return {"ok": True, "skipped": True, "reason": "A check is already running."}
        started = time.monotonic()
        try:
            now = self.clock() if now is None else float(now)
            try:
                out = self._run(now)
                out.update(ok=True, at=now, duration_s=round(time.monotonic() - started, 3))
                self.last = out
                day = self.store.scalar("SELECT COUNT(*) FROM alerts_alerts WHERE created_at >= ? AND reason NOT IN "
                                        "('digest', 'test')", (now - 86400.0,), default=0)
                note = (f"{out['watchlists']} watchlist{'s' if out['watchlists'] != 1 else ''}, {out['rules']} "
                        f"rule{'s' if out['rules'] != 1 else ''}; {out['scanned']:,} events checked")
                self.m.status("alerts.engine", ok=True, count=int(day or 0), note=note)
            except Exception as err:   # a broken check must never stop the next one
                log.warning("alerts check failed: %s", err, exc_info=True)
                self.last = {"ok": False, "at": now, "error": str(err)[:300],
                             "duration_s": round(time.monotonic() - started, 3)}
                self.m.status("alerts.engine", ok=False, error=f"The check failed: {err}")
            self.runs += 1
            return dict(self.last)
        finally:
            self.lock.release()

    def area_for(self, wid: int, version: Any, area_json: str) -> Area:
        cached = self._areas.get(wid)
        if cached and cached[0] == version:
            return cached[1]
        area = Area.from_json(db.loads(area_json, {}) or {"type": "world"})
        self._areas[wid] = (version, area)
        if len(self._areas) > 4 * db.MAX_WATCHLISTS:
            self._areas.pop(next(iter(self._areas)))
        return area

    def matches_now(self, watchlist: dict, now: float, limit: int = 30) -> dict:
        """Events reported in the last 25 minutes that the watchlist's enabled rules match (a preview)."""
        rules = [r for r in watchlist.get("rules") or [] if r.get("enabled") and r.get("preset") in R.PRESETS]
        area = Area.from_json(watchlist.get("area") or {"type": "world"})
        if not rules:
            return {"count": 0, "events": [], "note": "No rules are switched on."}
        kinds = self._kinds([R.get(r["preset"]) for r in rules])
        events = self.m.app.history.events(now - LOOKBACK_S, kinds=kinds, limit=QUERY_LIMIT)
        snap = self._snapshot_at()
        ref = snap if snap is not None else max((e.get("last_seen") or 0 for e in events), default=now)
        live = [e for e in events if (e.get("last_seen") or 0) >= ref - ACTIVE_WINDOW_S]
        found: dict[tuple, dict] = {}
        cache: dict[tuple, bool] = {}
        for r in rules:
            preset = R.get(r["preset"])
            params = preset.clean_params(r.get("params") or {})
            for ev in live:
                if not preset.match(ev, params):
                    continue
                level = self._level(preset, params, ev, area, cache)
                if not level:
                    continue
                key = (ev.get("module"), ev.get("kind"), ev.get("id"))
                if key not in found:
                    found[key] = {"entity": entity_of(ev), "title": ev.get("title") or "", "subtitle": ev.get("subtitle") or "",
                                  "severity": ev.get("severity"), "time": ev.get("time"), "rule": preset.label,
                                  "cone": level == 1}
        items = sorted(found.values(), key=lambda x: (-(x["severity"] or 0), -(x["time"] or 0)))
        return {"count": len(items), "events": items[:limit]}

    # -- internals -------------------------------------------------------------------------------
    def _snapshot_at(self) -> float | None:
        try:
            last = getattr(self.m.app.history, "last", None) or {}
        except Exception:
            return None
        return _float(last.get("at"))

    @staticmethod
    def _kinds(presets: list) -> list[str] | None:
        kinds: set[str] = set()
        for p in presets:
            if p.kinds is None:
                return None
            kinds.update(p.kinds)
        return sorted(kinds)

    def _level(self, preset: R.Preset, params: dict, ev: dict, area: Area, cache: dict) -> int:
        """2 = the event is in the area; 1 = only its storm cone, track or wind area touches it; 0 = no."""
        key = (ev.get("module"), ev.get("kind"), ev.get("id"))
        inside = cache.get(key)
        if inside is None:
            inside = area.contains(ev.get("lat"), ev.get("lon"))
            cache[key] = inside
        if inside:
            return 2
        if area.kind == "world":
            return 0
        if preset.id == "outbreaks":
            if any(area.contains(lat, lon) for lat, lon in self.outbreak_places(ev)):
                return 2
        elif preset.id == "gps":
            shape = bbox_polygon(ev.get("bbox")) if ev.get("bbox") is not None else None
            if shape is None and _float(ev.get("radius_km")):
                shape = circle_polygon(ev.get("lat"), ev.get("lon"), float(ev["radius_km"]))
            if shape is not None and area.intersects(shape):
                return 2
        elif preset.id == "storms" and params.get("cones"):
            g = self.storm_geometry(ev)
            if g and (area.intersects(g.get("cone")) or any(area.intersects(b) for b in g.get("buffers") or [])
                      or area.touches_line(g.get("track")) or area.touches_line(g.get("points"))):
                return 1
        return 0

    def _run(self, now: float) -> dict:
        rules = db.active_rules(self.store)
        settings = db.settings(self.store)
        summary = {"watchlists": len({r["watchlist_id"] for r in rules}), "rules": len(rules), "scanned": 0,
                   "alerts": 0, "held_back": 0, "absorbed": 0}
        if not rules:
            return summary
        known = [r for r in rules if r["preset"] in R.PRESETS]
        snap_at = self._snapshot_at()          # read before the query (see the module docstring)
        events = self.m.app.history.events(now - LOOKBACK_S, kinds=self._kinds([R.get(r["preset"]) for r in known]),
                                           limit=QUERY_LIMIT)
        summary["scanned"] = len(events)
        by_w: dict[int, list[dict]] = {}
        for r in known:
            by_w.setdefault(r["watchlist_id"], []).append(r)
        limit = int(settings.get("rate_limit") or 10)
        for wid, wrules in by_w.items():
            try:
                res = self._watchlist(wid, wrules, events, now, snap_at, limit)
            except Exception as err:     # one bad watchlist never stops the others
                log.warning("alerts: watchlist %s failed: %s", wid, err, exc_info=True)
                continue
            for k in ("alerts", "held_back", "absorbed"):
                summary[k] += res[k]
        return summary

    def _watchlist(self, wid: int, rules: list[dict], events: list[dict], now: float, snap_at: float | None,
                   limit: int) -> dict:
        name = rules[0]["w_name"]
        area = self.area_for(wid, rules[0]["w_updated_at"], rules[0]["w_area"])
        cache: dict[tuple, bool] = {}
        pending: list[dict] = []
        seen_rows: list[tuple] = []
        marks: list[tuple] = []
        absorbed = 0
        for r in rules:
            preset = R.get(r["preset"])
            try:
                params = preset.clean_params(db.loads(r["params"], {}) or {})
            except R.RuleError:
                params = preset.clean_params({})
            baseline = r["baseline_at"] is None
            last_check = _float(r["checked_at"])
            watch_from = _float(r["watch_from"]) or 0.0
            if baseline or last_check is None:
                cands = events
            else:
                cands = [e for e in events if (_float(e.get("last_seen")) or 0.0) >= last_check - WATERMARK_SLACK_S]
            matched = []
            for ev in cands:
                if not preset.match(ev, params):
                    continue
                level = self._level(preset, params, ev, area, cache)
                if level:
                    matched.append((ev, level, R.dedupe_ref(ev)))
            recs = db.seen_for(self.store, r["id"], [m[2] for m in matched]) if matched else {}
            done: set[tuple] = set()
            for ev, level, ref in matched:
                key = (str(ev.get("module") or ""), str(ev.get("kind") or ""), ref)
                if key in done:
                    continue
                done.add(key)
                reason, rec, extra = decide(ev, recs.get(key), baseline=baseline, watch_from=watch_from,
                                            last_check=last_check, level=level, now=now)
                seen_rows.append((r["id"], key[0], key[1], key[2], rec["severity"], rec["event_time"], rec["level"],
                                  rec["alerted"], now, now))
                if reason is None:
                    if key not in recs:
                        absorbed += 1
                    continue
                pending.append(self._alert(wid, name, r, preset, params, ev, level, reason, extra))
            checked = snap_at if snap_at is not None else last_check
            marks.append((now if baseline else r["baseline_at"], checked, r["id"], r["updated_at"]))
        delivered, held = self._write(wid, name, pending, seen_rows, marks, now, limit)
        return {"alerts": delivered, "held_back": held, "absorbed": absorbed}

    def _alert(self, wid, name, rule, preset, params, ev, level, reason, extra) -> dict:
        kind = ev.get("kind")
        return {"watchlist_id": wid, "rule_id": rule["id"], "area_name": name, "preset": preset.id,
                "rule_text": preset.phrase(params), "reason": reason, "module": ev.get("module"), "kind": kind,
                "ref": str(ev.get("id")), "title": ev.get("title") or R.KIND_WORDS.get(kind, "Event").capitalize(),
                "subtitle": ev.get("subtitle") or "",
                "why": wording.why(reason, preset, params, ev, name, level=level, extra=extra),
                "severity": _float(ev.get("severity")) or 0.0, "event_time": _float(ev.get("time")),
                "lat": _float(ev.get("lat")), "lon": _float(ev.get("lon")), "entity": entity_of(ev)}

    def _write(self, wid: int, name: str, pending: list[dict], seen_rows: list[tuple], marks: list[tuple],
               now: float, limit: int) -> tuple[int, int]:
        pending.sort(key=lambda a: (-(a["severity"] or 0), -(a["event_time"] or 0)))
        delivered = held = 0
        with self.store.tx() as c:
            if c.execute("SELECT 1 FROM alerts_watchlists WHERE id = ?", (wid,)).fetchone() is None:
                return 0, 0                                     # deleted while we were checking
            live_rules = {row[0] for row in c.execute("SELECT id FROM alerts_rules WHERE watchlist_id = ?", (wid,))}
            seen_rows = [s for s in seen_rows if s[0] in live_rules]
            pending = [a for a in pending if a["rule_id"] in live_rules]
            if seen_rows:
                c.executemany(db.UPSERT_SEEN, seen_rows)
            if pending:
                used = db.delivered_since(c, wid, now - RATE_WINDOW_S)
                room = max(0, limit - used)
                for a in pending[:room]:
                    db.insert_alert(c, now, **a)
                    delivered += 1
                fold = pending[room:]
                if fold:
                    held = len(fold)
                    dg = db.open_digest(c, wid, now - RATE_WINDOW_S)
                    if dg is None:
                        did = db.insert_alert(c, now, watchlist_id=wid, area_name=name, reason="digest",
                                              rule_text="Rate limit", title=wording.digest_title(len(fold), name),
                                              severity=max(a["severity"] for a in fold), count=len(fold))
                        total = len(fold)
                    else:
                        did = dg["id"]
                        total = int(dg["count"] or 0) + len(fold)
                    for a in fold:
                        db.insert_alert(c, now, **a, digest_id=did)
                    top = c.execute("SELECT title, severity FROM alerts_alerts WHERE digest_id = ? "
                                    "ORDER BY severity DESC, event_time DESC, id DESC LIMIT 1", (did,)).fetchone()
                    c.execute("UPDATE alerts_alerts SET count = ?, title = ?, why = ?, severity = MAX(severity, ?), "
                              "updated_at = ?, read_at = NULL WHERE id = ?",
                              (total, wording.digest_title(total, name),
                               wording.digest_why(limit, top[0] if top else None, top[1] if top else None),
                               max(a["severity"] for a in fold), now, did))
            for baseline_at, checked_at, rid, version in marks:
                # Only if the rule did not change while we were checking (a change asks for a new baseline).
                c.execute("UPDATE alerts_rules SET baseline_at = ?, checked_at = ? WHERE id = ? AND updated_at = ?",
                          (baseline_at, checked_at, rid, version))
        return delivered, held

    def test_alert(self, now: float) -> int:
        """A test alert for the Settings tab: opens the latest notable event, if there is one."""
        ev = None
        try:
            items = self.m.app.history.events(now - 6 * 3600.0, min_severity=5, limit=1)
            ev = items[0] if items else None
        except Exception:
            ev = None
        with self.store.tx() as c:
            return db.insert_alert(
                c, now, reason="test", area_name="Test", rule_text="Test alert", title="Test alert",
                subtitle=(ev or {}).get("title") or "",
                why=("This is how alerts arrive. Show opens the latest notable event: " + (ev.get("title") or "")
                     if ev else "This is how alerts arrive. Desktop notifications show while Meridian is in the background."),
                severity=0, entity=entity_of(ev) if ev else None,
                lat=_float((ev or {}).get("lat")), lon=_float((ev or {}).get("lon")))
