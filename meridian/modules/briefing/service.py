"""The briefing service: generation (one at a time), the daily schedule, pruning and status."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import gather as G
from . import narrative, schedule
from .build import YESTERDAY, Builder, utc
from .regions import CountryIndex, Regionizer
from .store import KEEP_DAYS, BriefingStore

SRC_MAIN = "briefing.generator"
SRC_AI = "briefing.narrative"
EVENT_LIMIT = 20000
MAX_HOURS = 168
AI_STATUS_TTL = 60.0


class Busy(Exception):
    pass


class BriefingService:
    def __init__(self, m, *, clock: Callable[[], float] = time.time, localtime: Callable = time.localtime,
                 mktime: Callable = time.mktime, gather_fn: Callable[[float], dict] | None = None,
                 complete_fn: Callable[..., Any] | None = None, ai_status_fn: Callable[[], dict] | None = None,
                 countries_path=None):
        from meridian.modules.ai import helper     # the shared AI helper (CONTRACT 15.4)
        self.m = m
        self.app = m.app
        self.log = m.log
        self.db = BriefingStore(m.store)
        path = countries_path if countries_path is not None else m.config.data_dir / "build" / "intel" / "countries.geojson"
        self.index = CountryIndex(path)
        self.regionizer = Regionizer(self.index)
        self.clock = clock
        self.localtime = localtime
        self.mktime = mktime
        self.gather_fn = gather_fn or (lambda since: G.gather(self.app, since, log=self.log))
        self.complete_fn = complete_fn or helper.complete
        self.ai_status_fn = ai_status_fn or helper.status
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ai_cache: tuple[float, dict] | None = None
        self.attempts = schedule.Attempts()
        self.last_prune = 0.0
        self.state: dict[str, Any] = {"running": False, "phase": None, "started_at": None, "trigger": None,
                                      "last": None}

    # -- settings -----------------------------------------------------------------------------------
    def briefing_time(self) -> tuple[str, bool]:
        raw = self.m.config.get("BRIEFING_TIME", schedule.DEFAULT_TIME)
        h, mi, ok = schedule.parse_time(raw)
        return f"{h:02d}:{mi:02d}", ok

    def ai_status(self, fresh: bool = False) -> dict:
        """The AI provider's state. ``fresh`` asks now (generation, in its own thread); otherwise the cached
        answer comes back at once and a stale one is refreshed in the background, because asking can
        take seconds (on Windows a refused loopback connection to Ollama takes about 2 s)."""
        hit = self._ai_cache
        if fresh or hit is None:
            return self._ask_ai() if fresh else (self._refresh_ai() or {"ready": False, "provider": None,
                                                                      "message": "Checking the AI provider…"})
        if time.monotonic() - hit[0] >= AI_STATUS_TTL:
            self._refresh_ai()
        return hit[1]

    def _ask_ai(self) -> dict:
        try:
            st = self.ai_status_fn() or {}
        except Exception:
            st = {"ready": False}
        st = {k: st.get(k) for k in ("ready", "provider", "provider_label", "model", "code", "message")}
        self._ai_cache = (time.monotonic(), st)
        return st

    def _refresh_ai(self) -> None:
        with self._state_lock:
            if self.state.get("_ai_refreshing"):
                return None
            self.state["_ai_refreshing"] = True

        def run():
            try:
                self._ask_ai()
            finally:
                with self._state_lock:
                    self.state["_ai_refreshing"] = False

        threading.Thread(target=run, daemon=True, name="briefing-ai-status").start()
        return None

    def base_url(self) -> str | None:
        host, port = getattr(self.app, "bind", ("127.0.0.1", 0)) or ("127.0.0.1", 0)
        if not port:
            return None
        shown = f"[{host}]" if ":" in str(host) else str(host)
        return f"http://{shown}:{port}"

    # -- the period ------------------------------------------------------------------------------------
    def period(self, basis: str, hours: int | None, now: float) -> tuple[float, float, str]:
        if basis == "hours" and hours:
            return now - min(MAX_HOURS, max(1, int(hours))) * 3600.0, now, "hours"
        if basis in ("since_last", "auto"):
            prev = self.db.latest(before=now)
            if prev:
                to = float((prev.get("period") or {}).get("to") or prev.get("time") or 0)
                gap = now - to
                lo, hi = (18 * 3600.0, 36 * 3600.0) if basis == "auto" else (900.0, MAX_HOURS * 3600.0)
                if lo <= gap <= hi:
                    return to, now, "since_last"
        return now - 86400.0, now, "24h"

    # -- generation ------------------------------------------------------------------------------------
    def _phase(self, phase: str | None) -> None:
        with self._state_lock:
            self.state["phase"] = phase

    def generate(self, trigger: str = "manual", basis: str = "24h", hours: int | None = None,
                 now: float | None = None) -> dict:
        """Make, store and return a briefing. Raises Busy when one is already being made."""
        if not self._lock.acquire(blocking=False):
            raise Busy()
        started = time.monotonic()
        with self._state_lock:
            self.state.update(running=True, phase="history", started_at=self.clock(), trigger=trigger)
        try:
            now = self.clock() if now is None else float(now)
            since, until, basis = self.period(basis, hours, now)
            self.index.ensure()
            cur = self.app.history.events(since, until, limit=EVENT_LIMIT + 1)
            truncated = len(cur) > EVENT_LIMIT
            cur = cur[:EVENT_LIMIT]
            prev = self.app.history.events(since - (until - since), since, limit=EVENT_LIMIT)
            self._phase("live data")
            extras = self.gather_fn(since) or {}
            # "yesterday's briefing" for live snapshots: the one made nearest 24 hours ago (12 to 36 h)
            previous = self.db.closest(now - 86400.0, now - YESTERDAY[1], now - YESTERDAY[0])
            self._phase("writing")
            date, _ = schedule.local_day(now, self.localtime)
            builder = Builder(since=since, until=until, now=now, place=self.regionizer.place,
                              country_entity=self.regionizer.country_entity, countries_ready=self.index.ready)
            doc = builder.build(cur, prev, extras, previous, bid=self.db.new_id(now), trigger=trigger, basis=basis,
                                local_date=date, truncated=truncated)
            self.index.release()          # about 15 MB of shapes, needed again only for the next briefing
            ai = self.ai_status(fresh=True)
            doc["ai_status"] = "pending" if ai.get("ready") else "off"
            doc["build_s"] = round(time.monotonic() - started, 2)
            self.db.save(doc)
            self.m.status(SRC_MAIN, ok=True, count=doc["stats"]["items"],
                          note=f"Last briefing {utc(doc['time'])}: {doc['stats']['events']:,} events, "
                               f"{doc['stats']['items']:,} items")
            self.log.info("briefing: %s made (%s, %d events, %d items, %.1f s)", doc["id"], trigger,
                          doc["stats"]["events"], doc["stats"]["items"], doc["build_s"])
            with self._state_lock:
                self.state["last"] = {"id": doc["id"], "time": doc["time"], "ok": True, "error": None}
            if ai.get("ready"):
                self._phase("narrative")
                self._narrate(doc)
            return doc
        except Busy:
            raise
        except Exception as err:
            msg = self.m.config.redact(f"{type(err).__name__}: {err}")[:300]
            self.m.status(SRC_MAIN, ok=False, error=msg)
            with self._state_lock:
                self.state["last"] = {"id": None, "time": self.clock(), "ok": False, "error": msg}
            raise
        finally:
            self.index.release()
            with self._state_lock:
                self.state.update(running=False, phase=None)
            self._lock.release()

    def _narrate(self, doc: dict) -> None:
        try:
            ai = narrative.write(doc, self.complete_fn, clock=self.clock)
        except Exception as err:   # the deterministic briefing stands on its own
            self.log.warning("briefing: the AI narrative failed: %s", self.m.config.redact(str(err))[:200])
            ai = None
        doc["ai"] = ai
        doc["ai_status"] = "done" if ai else "failed"
        self.db.save(doc)
        if ai:
            self.m.status(SRC_AI, ok=True, note=f"{ai.get('provider_label') or ai.get('provider')} "
                                                f"{ai.get('model') or ''}, {ai.get('elapsed_s') or 0:.0f} s".strip())
        else:
            self.m.status(SRC_AI, ok=False, error="The AI provider did not write a narrative (off, busy or failing).")

    def start(self, trigger: str = "manual", basis: str = "24h", hours: int | None = None) -> dict:
        """Generate in the background. {status: "started"} or {status: "running"} when one is going."""
        if self._lock.locked():
            return {"ok": True, "status": "running", **self.status_brief()}
        done = threading.Event()
        box: dict[str, Any] = {}

        def run():
            try:
                box["doc"] = self.generate(trigger, basis, hours)
            except Busy:
                box["busy"] = True
            except Exception as err:
                self.log.warning("briefing: generation failed: %s", self.m.config.redact(str(err))[:300])
            finally:
                done.set()

        threading.Thread(target=run, daemon=True, name="briefing-generate").start()
        done.wait(0.05)
        return {"ok": True, "status": "running" if not box.get("busy") else "busy", **self.status_brief()}

    # -- schedule --------------------------------------------------------------------------------------
    def tick(self, now: float | None = None) -> dict | None:
        """The 10-minute check: prune daily; make the scheduled briefing when it is due."""
        now = self.clock() if now is None else float(now)
        if now - self.last_prune >= 86400.0:
            self.last_prune = now
            n = self.db.prune(now)
            if n:
                self.log.info("briefing: pruned %d briefings older than %d days", n, KEEP_DAYS)
        when, _ok = self.briefing_time()
        date, _ = schedule.local_day(now, self.localtime)
        if not schedule.is_due(now, self.db.last_scheduled_date(), when, self.localtime):
            return None
        if not self.attempts.allowed(date):
            return None
        try:
            return self.generate("scheduled", "auto", now=now)
        except Busy:
            return None
        except Exception:
            self.attempts.failed(date)
            return None

    # -- status ----------------------------------------------------------------------------------------
    def status_brief(self) -> dict:
        with self._state_lock:
            st = dict(self.state)
        return {"running": st["running"], "phase": st["phase"], "started_at": st["started_at"],
                "trigger": st["trigger"], "last": st["last"]}

    def status(self) -> dict:
        now = self.clock()
        when, ok = self.briefing_time()
        last_date = self.db.last_scheduled_date()
        latest = self.db.latest_meta()
        tasks = any(t.name == f"{self.m.name}.schedule" for t in getattr(self.app.scheduler, "tasks", {}).values()) \
            and bool(getattr(self.app.scheduler, "started", False))
        ai = self.ai_status()
        return {**self.status_brief(),
                "latest": latest,
                "count": self.db.count(), "keep_days": KEEP_DAYS,
                "schedule": {"briefing_time": when, "valid": ok, "local_date_done": last_date,
                             "next_due": schedule.next_due(now, last_date, when, self.localtime, self.mktime),
                             "running": tasks, "check_every_s": schedule.CHECK_EVERY_S},
                "narrative": {"ready": bool(ai.get("ready")), "provider": ai.get("provider"),
                              "provider_label": ai.get("provider_label"), "model": ai.get("model"),
                              "message": None if ai.get("ready") else ai.get("message")},
                "countries": self.index.available}
