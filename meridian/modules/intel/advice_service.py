"""Travel advice poller: the FCDO index every 3 hours (country pages only when their update time changed)
and the US State Department list every 6 hours. Runs in the background whether or not the browser is
open, so alerts, the briefing and the timeline see changes (CONTRACT rule 8). Current advice lives in
``intel_advice``; detected changes in ``intel_advice_changes``."""

from __future__ import annotations

import threading
import time

from meridian.net import NetError

from . import advice, db

FCDO_EVERY_S = 3 * 3600
US_EVERY_S = 6 * 3600
HISTORY_SEED_DAYS = 30          # FCDO change notes this recent seed change events the first time a page is read


class TravelAdvice:
    def __init__(self, m, resolve_country, iso3_to_iso2, fips_to_iso2):
        self.m = m
        self.store = m.store
        self.net = m.net
        self.log = m.log
        self.resolve_country = resolve_country
        self.iso3_to_iso2 = iso3_to_iso2
        self.fips_to_iso2 = fips_to_iso2
        self.lock = threading.Lock()
        self.fcdo: dict[str, dict] = {}
        self.us: dict[str, dict] = {}
        self.state = {"fcdo": {"checked": None, "error": None, "pages": 0, "pending": 0},
                      "us": {"checked": None, "error": None, "items": 0}}
        self.version = 0                         # bumped on every change of the current advice (browser cache key)
        self.min_fcdo = 100                      # a shorter index or list is treated as a broken answer
        self.min_us = 100

    # -- persistence ---------------------------------------------------------------------------
    def load(self) -> None:
        stored = db.load_advice(self.store)
        with self.lock:
            self.fcdo = stored.get("fcdo", {})
            self.us = stored.get("us", {})
            self.version += 1
        kv = self.store.kv_get("intel.advice.state") or {}
        for src in ("fcdo", "us"):
            if isinstance(kv.get(src), dict):
                self.state[src].update({k: kv[src].get(k) for k in ("checked",) if kv[src].get(k)})

    def _save_state(self) -> None:
        self.store.kv_set("intel.advice.state", {s: {"checked": v.get("checked")} for s, v in self.state.items()})

    # -- loop ------------------------------------------------------------------------------------
    def loop(self, stop: threading.Event) -> None:
        if stop.wait(20.0):
            return
        while not stop.is_set():
            now = time.time()
            if now - (self.state["fcdo"]["checked"] or 0) >= FCDO_EVERY_S:
                self.poll_fcdo(stop)
            if stop.is_set():
                return
            if now - (self.state["us"]["checked"] or 0) >= US_EVERY_S:
                self.poll_us()
            if stop.wait(120.0):
                return

    def _iso2s_for_slug(self, slug: str, name: str) -> tuple[str, ...]:
        codes = advice.FCDO_SLUG_ISO2.get(slug)
        if codes:
            return codes
        iso2 = self.resolve_country(name)
        return (iso2,) if iso2 else ()

    def poll_fcdo(self, stop: threading.Event | None = None) -> None:
        st = self.state["fcdo"]
        try:
            index = advice.parse_fcdo_index(self.net.get_json(advice.FCDO_INDEX_URL, timeout=30, retries=1),
                                            min_countries=self.min_fcdo)
        except Exception as err:
            st["error"] = str(err)[:200]
            st["checked"] = time.time() - FCDO_EVERY_S + 1800       # try again in 30 minutes
            self.m.status("intel.fcdo", ok=False, error=f"GOV.UK travel advice: {err}")
            return
        with self.lock:
            known = {e.get("slug"): e for e in self.fcdo.values() if e.get("slug")}
        todo = [e for e in index if e["slug"] not in known or (e["updated"] or 0) > (known[e["slug"]].get("updated") or 0)
                or known[e["slug"]].get("pv") != advice.PARSER_VERSION]
        st["pending"] = len(todo)
        failures = 0
        for e in todo:
            if stop is not None and stop.is_set():
                return
            try:
                page = self.net.get_json(advice.FCDO_API + e["slug"], timeout=30, retries=1)
                parsed = advice.parse_fcdo_country(page, e["slug"])
            except (NetError, ValueError) as err:
                failures += 1
                self.log.info("FCDO page %s failed: %s", e["slug"], err)
                if failures >= 5 and failures * 2 > len(todo):
                    break
                continue
            parsed["updated"] = parsed.get("updated") or e["updated"]
            parsed["change"] = parsed.get("change") or e["change"]
            self.apply_fcdo(parsed, e["name"])
            st["pending"] = max(0, st["pending"] - 1)
        st.update(checked=time.time(), error=None if failures < len(todo) or not todo else "country pages failed",
                  pages=len(self.fcdo))
        self._save_state()
        levels = [v["level"] for v in self.fcdo.values()]
        self.m.status("intel.fcdo", ok=True, count=len(self.fcdo),
                      note=f"{len(self.fcdo)} countries: {levels.count(4)} against all travel, {levels.count(3)} against all "
                           f"but essential travel, {levels.count(2)} with areas advised against")

    def apply_fcdo(self, parsed: dict, index_name: str | None = None, now: float | None = None) -> list[dict]:
        """Store one parsed FCDO page; returns the change records it produced."""
        now = now or time.time()
        changes = []
        for iso2 in self._iso2s_for_slug(parsed["slug"], parsed["name"] or index_name or ""):
            with self.lock:
                old = self.fcdo.get(iso2)
            entry = {k: parsed.get(k) for k in ("pv", "slug", "name", "statuses", "level", "basis", "scope", "label",
                                                "sentences", "change", "updated", "reviewed", "url")}
            kind = advice.detect_fcdo_change(old, entry)
            if kind == "level" and old is not None and old.get("pv") != entry.get("pv") \
                    and sorted(old.get("statuses") or []) == sorted(entry.get("statuses") or []):
                kind = None          # only our normalisation changed (a newer parser), not FCDO's advice
            if kind:
                t = min(now, entry.get("updated") or now)
                text = advice.fcdo_change_text(entry["name"], old, entry) if kind == "level" else \
                    advice.areas_change_text(entry["name"], entry.get("change"))
                ch = {"id": f"fcdo:{iso2}:{int(t)}", "source": "fcdo", "iso2": iso2, "t": t,
                      "old_level": old.get("level") if old else None, "new_level": entry["level"], "kind": kind,
                      "text": advice.clip(text, 300),
                      "data": {"note": entry.get("change"), "url": entry.get("url"), "name": entry["name"],
                               "old_label": old.get("label") if old else None, "new_label": entry.get("label")}}
                if db.add_advice_change(self.store, ch):
                    changes.append(ch)
            elif old is None:
                changes += self._seed_history(iso2, parsed, now)
            db.save_advice(self.store, "fcdo", iso2, entry)
            with self.lock:
                self.fcdo[iso2] = dict(entry, fetched=now)
                self.version += 1
        return changes

    def _seed_history(self, iso2: str, parsed: dict, now: float) -> list[dict]:
        """At first sight of a page, FCDO's own recent notes about changed areas become change events."""
        out = []
        for h in parsed.get("history") or []:
            if now - h["t"] > HISTORY_SEED_DAYS * 86400 or not advice.is_advice_note(h["note"]):
                continue
            ch = {"id": f"fcdo:{iso2}:{int(h['t'])}", "source": "fcdo", "iso2": iso2, "t": min(now, h["t"]),
                  "old_level": None, "new_level": parsed["level"], "kind": "areas",
                  "text": advice.areas_change_text(parsed["name"], h["note"]),
                  "data": {"note": h["note"], "url": parsed.get("url"), "name": parsed["name"], "from_history": True,
                           "new_label": parsed.get("label")}}
            if db.add_advice_change(self.store, ch):
                out.append(ch)
        return out

    def poll_us(self) -> None:
        st = self.state["us"]
        try:
            items = advice.parse_us(self.net.get_json(advice.US_URL, timeout=40, retries=1), min_items=self.min_us)
        except Exception as err:
            st["error"] = str(err)[:200]
            st["checked"] = time.time() - US_EVERY_S + 1800
            self.m.status("intel.us_advisories", ok=False, error=f"US State Department advisories: {err}")
            return
        self.apply_us(items)
        st.update(checked=time.time(), error=None, items=len(items))
        self._save_state()
        levels = [v["level"] for v in self.us.values()]
        self.m.status("intel.us_advisories", ok=True, count=len(self.us),
                      note=f"{len(self.us)} countries: level 4 {levels.count(4)}, level 3 {levels.count(3)}, "
                           f"level 2 {levels.count(2)}, level 1 {levels.count(1)}")

    def map_us(self, it: dict) -> str | None:
        iso2 = self.iso3_to_iso2(it.get("iso3")) if it.get("iso3") else None
        if not iso2:
            for f in it.get("fips") or []:
                iso2 = self.fips_to_iso2(f)
                if iso2:
                    break
        return iso2 or self.resolve_country(it.get("place"))

    def apply_us(self, items: list[dict], now: float | None = None) -> list[dict]:
        now = now or time.time()
        best: dict[str, dict] = {}
        for it in items:
            iso2 = self.map_us(it)
            if iso2 and (iso2 not in best or (it.get("updated") or 0) > (best[iso2].get("updated") or 0)):
                best[iso2] = it
        changes = []
        for iso2, it in best.items():
            with self.lock:
                old = self.us.get(iso2)
            entry = {"level": it["level"], "label": f"Level {it['level']}: {it['words']}", "words": it["words"],
                     "title": it["title"], "place": it["place"], "summary": it["summary"], "updated": it["updated"],
                     "url": it["url"], "statuses": [], "slug": it.get("iso3")}
            if old is not None and old.get("level") != it["level"]:
                t = min(now, it.get("updated") or now)
                if now - t > 3 * 86400:      # the list's own time is old: date the change by when it was seen
                    t = now
                ch = {"id": f"us:{iso2}:{int(t)}", "source": "us", "iso2": iso2, "t": t, "old_level": old.get("level"),
                      "new_level": it["level"], "kind": "level",
                      "text": advice.us_change_text(it["place"], old.get("level"), it["level"]),
                      "data": {"url": it["url"], "name": it["place"], "old_label": old.get("label"),
                               "new_label": entry["label"]}}
                if db.add_advice_change(self.store, ch):
                    changes.append(ch)
            if old is None or any(old.get(k) != entry[k] for k in ("level", "updated", "summary", "title", "url")):
                db.save_advice(self.store, "us", iso2, entry)
                with self.lock:
                    self.us[iso2] = dict(entry, fetched=now)
                    self.version += 1
        return changes

    # -- reading ---------------------------------------------------------------------------------
    def summary(self) -> dict:
        with self.lock:
            fc = {k: {"l": v.get("level"), "b": v.get("basis"), "t": v.get("updated")} for k, v in self.fcdo.items()}
            us = {k: {"l": v.get("level"), "t": v.get("updated")} for k, v in self.us.items()}
            version = self.version
        return {"fcdo": fc, "us": us, "version": version,
                "checked": {"fcdo": self.state["fcdo"]["checked"], "us": self.state["us"]["checked"]},
                "pending": self.state["fcdo"].get("pending") or 0,
                "errors": {"fcdo": self.state["fcdo"]["error"], "us": self.state["us"]["error"]},
                "levels": advice.LEVEL_NAMES}

    def country(self, iso2: str) -> dict:
        with self.lock:
            fc = dict(self.fcdo.get(iso2) or {}) or None
            us = dict(self.us.get(iso2) or {}) or None
        return {"iso2": iso2, "fcdo": fc, "us": us,
                "changes": db.query_advice_changes(self.store, since=time.time() - 365 * 86400, iso2=iso2, limit=10),
                "levels": advice.LEVEL_NAMES}

    def changes(self, since: float, limit: int = 200) -> list[dict]:
        return db.query_advice_changes(self.store, since=since, limit=limit)
