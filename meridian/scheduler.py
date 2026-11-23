"""Background work: periodic tasks and long-running threads (CONTRACT.md section 5.3)."""

from __future__ import annotations

import logging
import random
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class TaskInfo:
    name: str
    kind: str                     # "periodic" or "thread"
    every: float = 0.0
    initial_delay: float = 0.0
    fn: Callable | None = None
    when: Callable[[], bool] | None = None
    runs: int = 0
    failures: int = 0
    skipped: int = 0
    consecutive_failures: int = 0
    last_start: float | None = None
    last_end: float | None = None
    last_ok: float | None = None
    last_error: str | None = None
    next_run: float | None = None
    running: bool = False
    thread: threading.Thread | None = field(default=None, repr=False)


class Scheduler:
    def __init__(self, log: logging.Logger | None = None, redact: Callable[[str], str] | None = None):
        self.log = log or logging.getLogger("meridian.scheduler")
        self.redact = redact or (lambda s: s)
        self.tasks: dict[str, TaskInfo] = {}
        self.stop_event = threading.Event()
        self.started = False
        self._lock = threading.Lock()

    def add_periodic(self, name: str, fn: Callable[[], Any], *, every: float, initial_delay: float = 0.0,
                     when: Callable[[], bool] | None = None) -> None:
        if every <= 0:
            raise ValueError("every must be > 0")
        with self._lock:
            if name in self.tasks:
                raise ValueError(f"task {name} registered twice")
            info = TaskInfo(name, "periodic", every=float(every), initial_delay=float(initial_delay), fn=fn,
                            when=when)
            self.tasks[name] = info
        if self.started:
            self._start(info)

    def add_thread(self, name: str, fn: Callable[[threading.Event], Any]) -> None:
        with self._lock:
            if name in self.tasks:
                raise ValueError(f"task {name} registered twice")
            info = TaskInfo(name, "thread", fn=fn)
            self.tasks[name] = info
        if self.started:
            self._start(info)

    def start(self) -> None:
        self.started = True
        for info in list(self.tasks.values()):
            self._start(info)

    def stop(self, timeout: float = 3.0) -> None:
        self.stop_event.set()
        deadline = time.monotonic() + timeout
        for info in self.tasks.values():
            if info.thread is not None:
                info.thread.join(max(0.0, deadline - time.monotonic()))

    def run_now(self, name: str) -> bool:
        info = self.tasks.get(name)
        if info is None or info.kind != "periodic" or info.running:
            return False
        threading.Thread(target=self._run_once, args=(info,), daemon=True, name=f"now:{name}").start()
        return True

    # -- internals -------------------------------------------------------------------------------
    def _start(self, info: TaskInfo) -> None:
        target = self._loop_periodic if info.kind == "periodic" else self._loop_thread
        info.thread = threading.Thread(target=target, args=(info,), daemon=True, name=info.name)
        info.thread.start()

    def _run_once(self, info: TaskInfo) -> None:
        info.running = True
        info.last_start = time.time()
        try:
            info.fn()
            info.runs += 1
            info.last_ok = time.time()
            info.last_error = None
            info.consecutive_failures = 0
        except Exception as err:
            info.runs += 1
            info.failures += 1
            info.consecutive_failures += 1
            info.last_error = self.redact(f"{type(err).__name__}: {err}")[:500]
            self.log.warning("task %s failed: %s", info.name, info.last_error)
            self.log.debug("task %s traceback:\n%s", info.name, self.redact(traceback.format_exc()))
        finally:
            info.last_end = time.time()
            info.running = False

    def _loop_periodic(self, info: TaskInfo) -> None:
        delay = info.initial_delay + random.uniform(0, min(5.0, info.every * 0.1))
        while not self.stop_event.wait(delay):
            if info.when is not None:
                try:
                    allowed = bool(info.when())
                except Exception:
                    allowed = True
                if not allowed:
                    info.skipped += 1
                    delay = min(info.every, 15.0)
                    info.next_run = time.time() + delay
                    continue
            self._run_once(info)
            delay = info.every
            if info.consecutive_failures:
                delay = min(info.every * (2 ** min(info.consecutive_failures, 4)), max(info.every, 900.0))
            delay *= random.uniform(0.95, 1.05)
            info.next_run = time.time() + delay

    def _loop_thread(self, info: TaskInfo) -> None:
        backoff = 2.0
        while not self.stop_event.is_set():
            info.last_start = time.time()
            info.running = True
            try:
                info.fn(self.stop_event)
                info.runs += 1
                info.last_ok = time.time()
                backoff = 2.0
            except Exception as err:
                info.failures += 1
                info.last_error = self.redact(f"{type(err).__name__}: {err}")[:500]
                self.log.warning("thread %s stopped with an error: %s", info.name, info.last_error)
                self.log.debug("thread %s traceback:\n%s", info.name, self.redact(traceback.format_exc()))
                backoff = min(backoff * 2, 300.0)
            finally:
                info.running = False
                info.last_end = time.time()
            if self.stop_event.wait(backoff):
                break

    def info(self) -> list[dict]:
        out = []
        for t in sorted(self.tasks.values(), key=lambda t: t.name):
            out.append({"name": t.name, "kind": t.kind, "every": t.every, "runs": t.runs, "failures": t.failures,
                        "skipped": t.skipped, "running": t.running, "last_ok": t.last_ok,
                        "last_error": t.last_error, "next_run": t.next_run})
        return out
