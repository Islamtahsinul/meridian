"""One-off, bounded GDELT history seed for the news-spike baseline (setting ``INTEL_SEED_DAYS``, default 16).

The spike detector needs 14 complete days of per-country tallies. Stored history counts from the start;
the seed fills the rest once, oldest gaps last, newest first:
- slots with no record that are older than the normal backfill window: inside the 14-day event window
  the whole file is ingested (events, protests, tallies; status ``ok``), older ones only add tallies
  (status ``seed``, which the instability index does not count as coverage, so its severity sums stay
  consistent);
- files ingested before protests and hourly tallies were counted (``tallies`` 1) are read again for
  those counts only.
Polite and bounded: one file every 1.2 s, at most ``INTEL_SEED_MAX_MB`` (default 150) MB in total, never
repeated once finished (progress in the kv store). GDELT files are about 45 KB, so 16 days is about 70 MB.
Five failed downloads in a row (network down, computer asleep, GDELT outage) pause the seed with a growing wait
(5, 10, 20, 40, then 60 minutes). Failed slots get up to three passes in a run; any still failing are counted
in the kv record and retried at the next start, so the seed only stays idle once nothing failed.
"""

from __future__ import annotations

import threading
import time

from meridian.net import NetError

from . import db, gdelt

GAP_S = 1.2
FAIL_STREAK = 5                                   # failed downloads in a row before the seed backs off
BACKOFF_S = (300, 600, 1200, 2400, 3600)          # waits while failing; the last one repeats
PASSES = 3                                        # passes over failed slots in one run
PASS_GAP_S = 1800                                 # wait before another pass over failed slots


class HistorySeed:
    def __init__(self, svc, days: int, max_mb: float):
        self.svc = svc
        self.store = svc.store
        self.log = svc.log
        self.days = max(0, min(30, int(days)))
        self.max_bytes = int(max(1.0, max_mb) * 1_000_000)
        self.state = {"days": self.days, "todo": None, "done": 0, "bytes": 0, "finished": False, "error": None,
                      "started": None, "errors": 0, "paused": 0, "streak": 0}
        self.last_failed = False

    def _status(self, finished: bool = False) -> None:
        st = self.state
        mb = st["bytes"] / 1e6
        ok = True
        if self.days <= 0:
            note = "off (INTEL_SEED_DAYS is 0)"
        elif finished:
            note = f"finished: {st['done']:,} files, {mb:.0f} MB"
            if st.get("errors"):
                note += f"; {st['errors']:,} could not be downloaded, retried at the next start"
            if st.get("error"):
                note += f"; {st['error']}"
        elif st.get("paused"):
            ok = False
            note = (f"paused after {st['streak']:,} failed downloads in a row, next try in {st['paused'] // 60:,} min; "
                    f"{st['done']:,} files, {mb:.0f} MB so far")
        else:
            note = f"{st['done']:,} files, {mb:.0f} of at most {self.max_bytes / 1e6:.0f} MB; {st.get('todo') or 0:,} to go"
        self.svc.m.status("intel.gdelt_seed", ok=ok, count=st["done"], note=note)

    def _progress(self) -> dict:
        p = self.store.kv_get("intel.seed") or {}
        return p if isinstance(p, dict) else {}

    def _save(self, used: int, done: int, finished: bool, errors: int = 0) -> None:
        rec = {"bytes": used, "files": done, "days": self.days, "finished": finished, "errors": errors}
        if finished:
            rec["at"] = time.time()
        self.store.kv_set("intel.seed", rec)

    def plan(self, latest_ts: str, backfill_hours: float, now: float | None = None) -> list[tuple[str, str]]:
        """[(slot, mode)] newest first; mode is 'full', 'tallies' or 'retally'."""
        now = now or time.time()
        slots = gdelt.slots_back(latest_ts, self.days * 24)
        if not slots:
            return []
        states = db.file_states(self.store, slots[-1])
        window_cut = gdelt.epoch_to_ts(gdelt.ts_to_epoch(latest_ts) - (backfill_hours + 1) * 3600)
        event_cut = gdelt.epoch_to_ts(now - (db.EVENT_DAYS - 0.5) * 86400)
        todo = []
        for ts in slots:
            st = states.get(ts)
            if st is None or st["status"] == "error":
                if ts >= window_cut:            # the normal GDELT loop owns these
                    continue
                todo.append((ts, "full" if ts >= event_cut else "tallies"))
            elif st["status"] == "ok" and int(st.get("tallies") or 1) < db.TALLIES_VERSION:
                todo.append((ts, "retally"))
        return todo

    def error_count(self, latest_ts: str, backfill_hours: float) -> int:
        """Seed-era slots (older than the normal loop's window) whose download failed."""
        slots = gdelt.slots_back(latest_ts, self.days * 24)
        if not slots:
            return 0
        window_cut = gdelt.epoch_to_ts(gdelt.ts_to_epoch(latest_ts) - (backfill_hours + 1) * 3600)
        return int(self.store.scalar("SELECT COUNT(*) FROM intel_files WHERE ts >= ? AND ts < ? AND status = 'error'",
                                     (slots[-1], window_cut), 0) or 0)

    def run(self, stop: threading.Event) -> None:
        """Thread body: waits for GDELT and the country shapes, seeds, then idles until shutdown."""
        prog = self._progress()
        if self.days <= 0:
            self._status(finished=True)
            stop.wait()
            return
        if prog.get("finished") and int(prog.get("days") or 0) >= self.days:
            now_ts = gdelt.epoch_to_ts(gdelt.slot_floor(time.time()))
            if not int(prog.get("errors") or 0) and not self.error_count(now_ts, self.svc.backfill_hours):
                self.state.update(finished=True, bytes=int(prog.get("bytes") or 0), done=int(prog.get("files") or 0))
                self._status(finished=True)
                stop.wait()
                return
        waited = 0.0
        while not stop.is_set() and (not self.svc.ingest.get("latest") or self.svc.country_index is None) and waited < 900:
            stop.wait(10.0)
            waited += 10.0
        latest = self.svc.ingest.get("latest")
        if stop.is_set() or not latest:
            stop.wait()
            return
        if stop.wait(30.0):                    # let the normal loop claim its window first
            return
        used = int(prog.get("bytes") or 0)
        done = int(prog.get("files") or 0)
        failed = 0
        self.state.update(started=time.time(), bytes=used, done=done)
        for pass_no in range(PASSES):
            if pass_no and stop.wait(PASS_GAP_S):   # failed slots wait a while before another pass
                return
            todo = self.plan(latest, self.svc.backfill_hours)
            if not todo:
                failed = 0
                break
            self.state.update(todo=len(todo))
            self._status()
            failed, streak, backoff = 0, 0, 0
            for ts, mode in todo:
                if stop.is_set():
                    return
                if used >= self.max_bytes:
                    self.state["error"] = f"stopped at the {self.max_bytes // 1_000_000} MB limit"
                    break
                used += self.seed_file(ts, mode)
                self.state.update(bytes=used, todo=max(0, (self.state["todo"] or 0) - 1))
                if self.last_failed:
                    failed += 1
                    streak += 1
                else:
                    done += 1
                    streak, backoff = 0, 0
                    self.state["done"] = done
                    if done % 24 == 0:
                        self._save(used, done, False)
                        self.svc.derive_wake.set()
                        self._status()
                wait = GAP_S
                if streak >= FAIL_STREAK:      # the network or GDELT is down: back off before the next try
                    wait = BACKOFF_S[min(backoff, len(BACKOFF_S) - 1)]
                    backoff += 1
                    self._save(used, done, False)
                    self.state.update(paused=int(wait), streak=streak)
                    self._status()
                if stop.wait(wait):
                    return
                if self.state.get("paused"):
                    self.state["paused"] = 0
                    self._status()
            if self.state.get("error") or not failed:
                break
        # retally failures leave no 'error' record, so the count of this run's last pass covers them
        errors = max(self.error_count(latest, self.svc.backfill_hours), failed)
        self.state.update(finished=True, errors=errors)
        self._save(used, done, True, errors)
        self.svc.derive_wake.set()
        self._status(finished=True)
        stop.wait()

    def seed_file(self, ts: str, mode: str) -> int:
        """Downloads and ingests one slot and returns its size. ``last_failed`` says whether the download failed
        (a 404 or an unreadable file is an answer from GDELT, not a failure)."""
        self.last_failed = False
        try:
            data = self.svc.net.get_bytes(gdelt.export_url(ts), timeout=45, retries=1, max_bytes=40_000_000)
        except NetError as err:
            if err.status == 404:
                if mode != "retally":
                    db.mark_file(self.store, ts, "missing")
                return 0
            self.last_failed = True
            if mode != "retally":
                db.mark_file(self.store, ts, "error")
            return 0
        try:
            text = gdelt.read_export_zip(data)
        except Exception as err:
            self.log.info("GDELT seed %s unreadable: %s", ts, err)
            if mode != "retally":
                db.mark_file(self.store, ts, "error")
            return len(data)
        if mode == "full":
            stats = self.svc.ingest_text(text, mode="full")
            db.mark_file(self.store, ts, "ok", stats["rows"], stats["kept"], stats["news"], len(data))
        else:
            self.svc.ingest_text(text, mode=mode, ts=ts, nbytes=len(data))
        return len(data)
