"""Background answers: the browser starts a question as a job, polls its progress (live tool trace)
and can stop it. Stopping takes effect between steps, so no further model or tool calls are made
(a request already in flight finishes, and its result is thrown away)."""

from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

JOB_ID_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"


class Job:
    def __init__(self, job_id: str, now: float):
        self.id = job_id
        self.created = now
        self.finished: float | None = None
        self.status = "running"            # running | done | cancelled | error
        self.phase = "model"
        self.round = 0
        self.tool: str | None = None
        self.trace: list[dict] = []
        self.notes: list[str] = []
        self.result: dict | None = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()

    def on_event(self, kind: str, data: dict) -> None:
        with self.lock:
            if kind == "phase":
                self.phase = data.get("phase", self.phase)
                self.round = data.get("round", self.round)
                self.tool = None
            elif kind == "tool":
                self.tool = data.get("tool")
            elif kind == "trace":
                self.trace.append(dict(data))
            elif kind == "note":
                self.notes.append(str(data.get("text") or "")[:400])
                self.notes = self.notes[-4:]


class JobManager:
    def __init__(self, *, max_running: int = 3, keep_s: float = 600.0,
                 clock: Callable[[], float] = time.monotonic, log=None):
        self._sem = threading.BoundedSemaphore(max_running)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.keep_s = keep_s
        self.clock = clock
        self.log = log

    @contextmanager
    def slot(self) -> Iterator[bool]:
        """A concurrency slot for a synchronous answer: yields False when all are busy."""
        got = self._sem.acquire(blocking=False)
        try:
            yield got
        finally:
            if got:
                self._sem.release()

    def start(self, target: Callable[[threading.Event, Callable[[str, dict], None]], dict]) -> Job | None:
        self._gc()
        if not self._sem.acquire(blocking=False):
            return None
        job = Job(secrets.token_urlsafe(12), self.clock())
        with self._lock:
            self._jobs[job.id] = job

        def runner():
            try:
                result = target(job.cancel, job.on_event)
                with job.lock:
                    job.result = result
                    job.status = "cancelled" if job.cancel.is_set() else "done"
            except Exception as err:   # never leave a job hanging
                if self.log:
                    self.log.error("ai: job failed: %s", err, exc_info=True)
                with job.lock:
                    job.status = "error"
                    job.result = {"ok": False, "reply": "", "actions": [], "trace": list(job.trace), "entities": [],
                                  "error": {"code": "INTERNAL", "message": "The assistant hit an internal error. "
                                                                           "The details are in the Meridian log."}}
            finally:
                job.finished = self.clock()
                self._sem.release()

        threading.Thread(target=runner, daemon=True, name=f"ai-job-{job.id[:6]}").start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str) -> dict | None:
        job = self.get(job_id)
        if job is None:
            return None
        now = self.clock()
        with job.lock:
            status = job.status
            if status == "running" and job.cancel.is_set():
                status = "cancelled"
            out: dict[str, Any] = {"job": job.id, "status": status, "phase": job.phase, "round": job.round,
                                   "tool": job.tool, "elapsed_ms": int(((job.finished or now) - job.created) * 1000),
                                   "trace": list(job.trace), "notes": list(job.notes)}
            if job.status != "running":
                out["result"] = job.result
        return out

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.cancel.set()
        return True

    def running(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status == "running")

    def _gc(self) -> None:
        now = self.clock()
        with self._lock:
            for jid in [jid for jid, j in self._jobs.items()
                        if (j.finished is not None and now - j.finished > self.keep_s) or now - j.created > 3 * self.keep_s]:
                self._jobs.pop(jid, None)


def valid_job_id(value: str) -> bool:
    return 8 <= len(value) <= 64 and all(c in JOB_ID_CHARS for c in value)
