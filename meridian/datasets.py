"""Static datasets (CONTRACT.md section 5.4): built in the background, refreshed when stale.

A dataset is a ``build(ds)`` function that downloads raw files into ``ds.raw_dir`` and writes its
outputs into ``ds.out_dir`` (``data/build/<module>/``). Success writes ``<name>.meta.json``; a failed
rebuild keeps the previous outputs in service and records the error.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .registry import json_bytes

MAX_PARALLEL_BUILDS = 3
RECHECK_SECONDS = 1800.0
# After a failed build the background rounds wait before trying again: 30 min, 1 h, 2 h, ... up to
# 12 h, reset by the next success (CONTRACT rule 8). ``python server.py --build`` ignores the wait.
BACKOFF_FIRST_S = 1800.0
BACKOFF_MAX_S = 12 * 3600.0
BACKOFF_GRACE_S = 60.0          # rounds start a little early or late; don't skip one by seconds


class DatasetDeferred(Exception):
    """Raise from a build to say "not now, on purpose" (e.g. the module keeps its own retry marker).
    The manager logs it at INFO instead of WARNING; it still backs off like a failure."""


def backoff_seconds(failures: int) -> float:
    """Wait after ``failures`` failed builds in a row (1 -> 30 min, 2 -> 1 h, ... capped at 12 h)."""
    if failures <= 0:
        return 0.0
    return min(BACKOFF_MAX_S, BACKOFF_FIRST_S * (2 ** min(failures - 1, 16)))


class BuildContext:
    """What a dataset build function receives."""

    def __init__(self, module_ctx, name: str, max_age_hours: float, force: bool = False):
        self.module = module_ctx.name
        self.name = name
        self.out_dir: Path = module_ctx.data_dir
        self.raw_dir: Path = module_ctx.raw_dir
        self.net = module_ctx.net
        self.log = module_ctx.log
        self.store = module_ctx.store
        self.config = module_ctx.config
        self.max_age_hours = max_age_hours
        self.force = force
        self.rows: int | None = None
        self.notes: list[str] = []
        self.sources: list[str] = []
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def path(self, filename: str) -> Path:
        p = self.out_dir / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def raw(self, filename: str) -> Path:
        p = self.raw_dir / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def download(self, url: str, filename: str, *, max_age_hours: float | None = None, **kw) -> Path:
        """Download into ``raw_dir`` (skipped while the copy is fresh; forced on --force)."""
        self.source(url)
        age = self.max_age_hours if max_age_hours is None else max_age_hours
        return self.net.download(url, self.raw(filename), max_age_hours=age * 0.9, force=self.force, **kw)

    def write_json(self, filename: str, obj: Any) -> Path:
        p = self.path(filename)
        tmp = p.with_name(p.name + f".tmp{os.getpid()}")
        tmp.write_bytes(json_bytes(obj))
        os.replace(tmp, p)
        return p

    def write_bytes(self, filename: str, data: bytes) -> Path:
        p = self.path(filename)
        tmp = p.with_name(p.name + f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return p

    def set_rows(self, n: int) -> None:
        self.rows = int(n)

    def note(self, text: str) -> None:
        self.notes.append(str(text)[:300])

    def source(self, url: str) -> None:
        red = self.config.redact(url)
        if red not in self.sources:
            self.sources.append(red)


@dataclass
class Dataset:
    key: str
    module_ctx: Any
    name: str
    build: Callable
    max_age_hours: float
    description: str
    after: tuple[str, ...]
    building: bool = False
    last_error: str | None = None
    last_attempt: float | None = None
    failures: int = 0                   # failed builds in a row (reset on success)
    next_attempt: float | None = None   # background rounds skip the dataset until then
    listeners: list = field(default_factory=list)

    @property
    def meta_path(self) -> Path:
        return self.module_ctx.data_dir / f"{self.name}.meta.json"


class DatasetManager:
    def __init__(self, log: logging.Logger | None = None, redact: Callable[[str], str] | None = None):
        self.log = log or logging.getLogger("meridian.datasets")
        self.redact = redact or (lambda s: s)
        self.items: dict[str, Dataset] = {}
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._pending_listeners: dict[str, list] = {}
        self.thread: threading.Thread | None = None

    # -- registration ----------------------------------------------------------------------------
    def register(self, module_ctx, name: str, build: Callable, *, max_age_hours: float, description: str,
                 after: tuple[str, ...]) -> None:
        key = f"{module_ctx.name}.{name}"
        deps = tuple(d if "." in d else f"{module_ctx.name}.{d}" for d in after)
        with self._lock:
            if key in self.items:
                raise ValueError(f"dataset {key} registered twice")
            ds = Dataset(key, module_ctx, name, build, float(max_age_hours), description, deps)
            ds.listeners.extend(self._pending_listeners.pop(key, []))
            self.items[key] = ds

    def on_built(self, key: str, fn: Callable[[], Any]) -> None:
        with self._lock:
            ds = self.items.get(key)
            if ds is None:
                self._pending_listeners.setdefault(key, []).append(fn)
            else:
                ds.listeners.append(fn)

    # -- state -----------------------------------------------------------------------------------
    def meta(self, key: str) -> dict | None:
        ds = self.items.get(key)
        if ds is None:
            return None
        try:
            return json.loads(ds.meta_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def ready(self, key: str) -> bool:
        meta = self.meta(key)
        return bool(meta and meta.get("ok"))

    def built_at(self, key: str) -> float | None:
        meta = self.meta(key)
        return meta.get("built_at") if meta and meta.get("ok") else None

    def stale(self, key: str) -> bool:
        ds = self.items[key]
        built = self.built_at(key)
        return built is None or (time.time() - built) > ds.max_age_hours * 3600.0

    # -- building --------------------------------------------------------------------------------
    def build(self, key: str, force: bool = False) -> bool:
        ds = self.items[key]
        with self._lock:
            if ds.building:
                return False
            ds.building = True
            ds.last_attempt = time.time()
        ctx = BuildContext(ds.module_ctx, ds.name, ds.max_age_hours, force=force)
        started = time.time()
        ok = False
        try:
            self.log.info("building dataset %s", key)
            ds.build(ctx)
            meta = {"ok": True, "built_at": time.time(), "duration_s": round(time.time() - started, 2),
                    "rows": ctx.rows, "notes": ctx.notes, "sources": ctx.sources, "description": ds.description}
            tmp = ds.meta_path.with_name(ds.meta_path.name + ".tmp")
            ds.meta_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(tmp, ds.meta_path)
            ds.last_error = None
            ds.failures = 0
            ds.next_attempt = None
            ok = True
            self.log.info("dataset %s built in %.1fs (%s rows)", key, meta["duration_s"], ctx.rows)
        except Exception as err:
            error = self.redact(f"{type(err).__name__}: {err}")[:500]
            repeated = ds.failures > 0 and error == ds.last_error
            ds.last_error = error
            ds.failures += 1
            wait = backoff_seconds(ds.failures)
            ds.next_attempt = time.time() + wait
            # A new or changed error is a warning; the same one again (or a deliberate deferral) is INFO.
            level = logging.INFO if repeated or isinstance(err, DatasetDeferred) else logging.WARNING
            self.log.log(level, "dataset %s failed (%d in a row, next try in %s): %s", key, ds.failures,
                         _duration(wait), ds.last_error)
            self.log.debug("dataset %s traceback:\n%s", key, self.redact(traceback.format_exc()))
        finally:
            with self._cond:
                ds.building = False
                self._cond.notify_all()
        if ok:
            self._notify(ds)
        return ok

    def _notify(self, ds: Dataset) -> None:
        for fn in list(ds.listeners):
            try:
                fn()
            except Exception as err:
                self.log.warning("on_dataset listener for %s failed: %s", ds.key, self.redact(str(err)))
                self.log.debug("%s", self.redact(traceback.format_exc()))

    def _deps_ready(self, ds: Dataset) -> bool:
        return all(self.ready(d) or d not in self.items for d in ds.after)

    def _dep_settled(self, dep: str, finished: set[str], in_flight: set[str]) -> bool:
        """A dependency is settled when it is unknown, fresh, or its build this round has *finished*
        (successfully or not). A dependency that is building, or stale and not yet tried, holds its
        dependants back."""
        item = self.items.get(dep)
        if item is None:
            return True
        if dep in in_flight or item.building:
            return False
        if dep in finished:
            return True
        return self.ready(dep) and not self.stale(dep)

    @staticmethod
    def _backing_off(ds: Dataset, now: float) -> bool:
        return ds.next_attempt is not None and now + BACKOFF_GRACE_S < ds.next_attempt

    def build_all(self, *, force: bool = False, only: set[str] | None = None) -> dict[str, bool]:
        """Synchronous build (CLI ``--build``), dependencies first."""
        results: dict[str, bool] = {}
        todo = [k for k in self.items if not only or k in only or k.split(".")[0] in only]
        progress = True
        while todo and progress:
            progress = False
            for key in list(todo):
                ds = self.items[key]
                if any(d in todo for d in ds.after):
                    continue
                todo.remove(key)
                progress = True
                if force or self.stale(key):
                    results[key] = self.build(key, force=force)
                else:
                    results[key] = True
                    self._notify(ds)
        for key in todo:  # dependency cycle or unknown dependency
            results[key] = self.build(key, force=force)
        return results

    def start(self, stop_event: threading.Event) -> None:
        self.thread = threading.Thread(target=self._run, args=(stop_event,), daemon=True, name="datasets")
        self.thread.start()

    def _run(self, stop_event: threading.Event) -> None:
        for ds in list(self.items.values()):
            if self.ready(ds.key):
                self._notify(ds)
        while not stop_event.is_set():
            self._build_round(stop_event)
            if stop_event.wait(RECHECK_SECONDS):
                return

    def _build_round(self, stop_event: threading.Event) -> None:
        attempted: set[str] = set()     # started this round
        finished: set[str] = set()      # build thread returned this round (success or failure)
        in_flight: set[str] = set()
        workers: list[threading.Thread] = []

        def run(key: str) -> None:
            try:
                self.build(key)
            finally:
                with self._cond:
                    in_flight.discard(key)
                    finished.add(key)
                    self._cond.notify_all()

        while not stop_event.is_set():
            with self._lock:
                # Missing datasets before stale ones; "after" waits for dependencies to finish.
                now = time.time()
                candidates = [ds for ds in self.items.values() if ds.key not in attempted and not ds.building
                              and not self._backing_off(ds, now) and self.stale(ds.key)]
                candidates.sort(key=lambda d: (self.built_at(d.key) is not None, d.key))
                runnable = [d for d in candidates if all(self._dep_settled(dep, finished, in_flight)
                                                         for dep in d.after)]
                running = len(in_flight) + sum(1 for d in self.items.values() if d.building and d.key not in in_flight)
            if not candidates:
                break
            if not runnable or running >= MAX_PARALLEL_BUILDS:
                with self._cond:
                    self._cond.wait(5.0)
                if not runnable and running == 0:
                    for d in candidates:      # dependency cycle: give up on these this round
                        attempted.add(d.key)
                continue
            ds = runnable[0]
            with self._lock:
                attempted.add(ds.key)
                in_flight.add(ds.key)
            t = threading.Thread(target=run, args=(ds.key,), daemon=True, name=f"build:{ds.key}")
            workers.append(t)
            t.start()
        for t in workers:
            t.join()

    def info(self) -> list[dict]:
        out = []
        for key, ds in sorted(self.items.items()):
            meta = self.meta(key) or {}
            out.append({"key": key, "description": ds.description, "ready": bool(meta.get("ok")),
                        "built_at": meta.get("built_at"), "rows": meta.get("rows"),
                        "duration_s": meta.get("duration_s"), "building": ds.building,
                        "max_age_hours": ds.max_age_hours, "last_error": ds.last_error,
                        "failures": ds.failures, "next_attempt": ds.next_attempt,
                        "notes": meta.get("notes") or [], "sources": meta.get("sources") or []})
        return out


def _duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:g} h"
    return f"{seconds / 60:g} min"
