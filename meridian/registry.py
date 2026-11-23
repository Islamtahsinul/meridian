"""The module API (CONTRACT.md section 5): routes, tasks, datasets, layers, search, nearby, events,
AI tools, keys and source status. Every module receives a :class:`ModuleContext` in ``register(m)``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import geo
from .config import WEB_DIR

_MISSING = object()


# ---------------------------------------------------------------------------------------------
# Requests, responses and errors
# ---------------------------------------------------------------------------------------------
class HTTPError(Exception):
    """Raise from a handler to answer ``{"error": {"code", "message"}}`` with ``status``."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _clean(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_clean(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def json_bytes(obj: Any) -> bytes:
    """Compact UTF-8 JSON. NaN and Infinity become ``null`` (browsers reject them)."""
    try:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)
    except ValueError:
        text = json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)
    return text.encode("utf-8")


class Request:
    def __init__(self, method: str, path: str, params: dict[str, str], query_all: dict[str, list[str]],
                 headers: dict[str, str], body: Any = None):
        self.method = method
        self.path = path
        self.params = params
        self.query_all = query_all
        self.query = {k: v[0] for k, v in query_all.items() if v}
        self.headers = headers
        self.body = body

    def arg(self, name: str, default: Any = _MISSING, *, type: Callable = str, min: Any = None,
            max: Any = None, choices: Any = None) -> Any:
        raw = self.query.get(name)
        if raw is None or raw == "":
            if default is _MISSING:
                raise HTTPError(400, "MISSING_ARG", f'Missing query parameter "{name}".')
            return default
        try:
            if type is bool:
                value: Any = raw.lower() in ("1", "true", "yes", "on")
            elif type is float:
                value = float(raw)
                if not math.isfinite(value):
                    raise ValueError
            elif type is int:
                value = int(raw)
            else:
                value = type(raw)
        except (TypeError, ValueError):
            tname = getattr(type, "__name__", "value")
            raise HTTPError(400, "BAD_ARG", f'Query parameter "{name}" is not a valid {tname}.') from None
        if min is not None and value < min:
            raise HTTPError(400, "BAD_ARG", f'Query parameter "{name}" must be at least {min}.')
        if max is not None and value > max:
            raise HTTPError(400, "BAD_ARG", f'Query parameter "{name}" must be at most {max}.')
        if choices is not None and value not in choices:
            raise HTTPError(400, "BAD_ARG", f'Query parameter "{name}" must be one of {sorted(choices)}.')
        return value

    def bbox(self, name: str = "bbox", default: Any = _MISSING) -> tuple[float, float, float, float] | Any:
        raw = self.query.get(name)
        if not raw:
            if default is _MISSING:
                raise HTTPError(400, "MISSING_ARG", f'Missing query parameter "{name}" (west,south,east,north).')
            return default
        try:
            return geo.parse_bbox(raw)
        except ValueError as err:
            raise HTTPError(400, "BAD_ARG", f'Query parameter "{name}": {err}.') from None

    def latlon(self, lat: str = "lat", lon: str = "lon") -> tuple[float, float]:
        la = self.arg(lat, type=float, min=-90.0, max=90.0)
        lo = self.arg(lon, type=float, min=-180.0, max=180.0)
        return la, lo

    def json(self) -> dict:
        return self.body if isinstance(self.body, dict) else {}

    def field(self, name: str, default: Any = _MISSING, *, type: Callable | None = None,
              max_len: int | None = None) -> Any:
        """A field of the JSON body with light validation (400 on a missing or wrong-typed field)."""
        body = self.json()
        if name not in body or body[name] is None:
            if default is _MISSING:
                raise HTTPError(400, "MISSING_FIELD", f'Missing field "{name}".')
            return default
        value = body[name]
        if type is not None:
            if type is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if not isinstance(value, type) or (type is int and isinstance(value, bool)):
                raise HTTPError(400, "BAD_FIELD", f'Field "{name}" has the wrong type.')
            if type is float and not math.isfinite(value):
                raise HTTPError(400, "BAD_FIELD", f'Field "{name}" must be a finite number.')
        if max_len is not None and isinstance(value, (str, list, dict)) and len(value) > max_len:
            raise HTTPError(400, "BAD_FIELD", f'Field "{name}" is too long.')
        return value


class Response:
    """A non-JSON (or pre-encoded) response. Handlers may instead return any JSON-able value."""

    def __init__(self, body: bytes = b"", *, content_type: str = "application/octet-stream", status: int = 200,
                 headers: dict[str, str] | None = None, cache: str = "no-store", etag: str | None = None,
                 compressible: bool | None = None):
        self.body = body
        self.content_type = content_type
        self.status = status
        self.headers = dict(headers or {})
        self.cache = cache
        self.etag = etag
        if compressible is None:
            ct = content_type.lower()
            compressible = any(t in ct for t in ("json", "text", "javascript", "xml", "svg", "csv"))
        self.compressible = compressible
        self.gzipped: bytes | None = None   # pre-compressed body, if the producer has one

    @staticmethod
    def json(obj: Any, *, status: int = 200, cache: str = "no-store", etag: bool = False) -> "Response":
        body = json_bytes(obj)
        tag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"' if etag else None
        return Response(body, content_type="application/json; charset=utf-8", status=status, cache=cache, etag=tag)

    @staticmethod
    def text(text: str, *, content_type: str = "text/plain; charset=utf-8", status: int = 200,
             cache: str = "no-store") -> "Response":
        return Response(text.encode("utf-8"), content_type=content_type, status=status, cache=cache)


@dataclass
class Route:
    method: str
    pattern: str
    regex: re.Pattern
    handler: Callable[[Request], Any]
    module: str
    body_max: int
    touch: bool = True


_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::(path))?\}")


def compile_pattern(pattern: str) -> re.Pattern:
    out = []
    pos = 0
    for m in _PARAM.finditer(pattern):
        out.append(re.escape(pattern[pos:m.start()]))
        out.append(f"(?P<{m.group(1)}>.+)" if m.group(2) else f"(?P<{m.group(1)}>[^/]+)")
        pos = m.end()
    out.append(re.escape(pattern[pos:]))
    return re.compile("^" + "".join(out) + "$")


# ---------------------------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------------------------
@dataclass
class ModuleInfo:
    name: str
    ok: bool = False
    error: str | None = None
    loaded_at: float | None = None
    description: str = ""


class App:
    def __init__(self, config, store, net, log: logging.Logger, scheduler, datasets):
        self.config = config
        self.store = store
        self.net = net
        self.log = log
        self.scheduler = scheduler
        self.datasets = datasets
        self.routes: list[Route] = []
        self.modules: dict[str, ModuleInfo] = {}
        self.contexts: dict[str, "ModuleContext"] = {}
        self.layer_specs: list[dict] = []
        self.sources: dict[str, dict] = {}
        self._search: list[tuple[str, Callable]] = []
        self._nearby: list[tuple[str, Callable]] = []
        self._events: list[tuple[str, Callable]] = []
        self._ai_tools: dict[str, dict] = {}
        self._activity: dict[str, float] = {}
        self._lock = threading.RLock()
        self.started_at = time.time()
        self.bind: tuple[str, int] = ("127.0.0.1", 0)
        self.only: set[str] | None = None

    # -- modules -------------------------------------------------------------------------------
    def module(self, name: str) -> "ModuleContext":
        with self._lock:
            if name not in self.contexts:
                self.contexts[name] = ModuleContext(self, name)
                self.modules[name] = ModuleInfo(name)
            return self.contexts[name]

    # -- routing -------------------------------------------------------------------------------
    def add_route(self, method: str, pattern: str, handler: Callable, module: str, body_max: int,
                  touch: bool = True) -> None:
        method = method.upper()
        if method not in ("GET", "POST"):
            raise ValueError("routes are GET or POST")
        with self._lock:
            for r in self.routes:
                if r.method == method and r.pattern == pattern:
                    raise ValueError(f"route {method} {pattern} registered twice")
            self.routes.append(Route(method, pattern, compile_pattern(pattern), handler, module, body_max, touch))
            # Static segments before parameters: longer literal prefixes first.
            self.routes.sort(key=lambda r: (-len(r.pattern.split("{")[0]), r.pattern))

    def match(self, method: str, path: str) -> tuple[Route | None, dict[str, str], bool]:
        """(route, params, path_exists_for_other_method)."""
        other = False
        lookup = "GET" if method == "HEAD" else method
        for route in self.routes:
            m = route.regex.match(path)
            if not m:
                continue
            if route.method == lookup:
                return route, m.groupdict(), False
            other = True
        return None, {}, other

    def call_route(self, method: str, path: str, query: dict | None = None, body: Any = None) -> Any:
        """Call another module's route in-process (no HTTP, no guards) and return its result.

        For server-side collaboration between modules (CONTRACT 9.1), e.g. the briefing reading
        ``/api/alerts/recent``. ``query`` values may be strings or lists of strings. Raises
        ``HTTPError(404)`` when no route matches and passes on the handler's own ``HTTPError``.
        Does not mark the target module active."""
        route, params, _other = self.match(method.upper(), path)
        if route is None:
            raise HTTPError(404, "NOT_FOUND", f"No route for {method.upper()} {path}.")
        query_all = {str(k): ([str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)])
                     for k, v in (query or {}).items() if v is not None}
        req = Request(method.upper(), path, params, query_all, {}, body if body is not None else ({} if method.upper() == "POST" else None))
        return route.handler(req)

    # -- activity ------------------------------------------------------------------------------
    def touch(self, key: str) -> None:
        self._activity[key] = time.time()

    def last_active(self, key: str) -> float:
        return self._activity.get(key, 0.0)

    # -- sources ---------------------------------------------------------------------------------
    def declare_source(self, module: str, sid: str, **meta) -> None:
        with self._lock:
            row = self.sources.setdefault(sid, {"id": sid, "module": module, "ok": None, "count": None,
                                                "error": None, "note": None, "updated_at": None})
            row.update(meta)
            row["module"] = module

    def set_status(self, module: str, sid: str, *, ok: bool | None = True, count: int | None = None,
                   error: str | None = None, note: str | None = None) -> None:
        with self._lock:
            row = self.sources.setdefault(sid, {"id": sid, "module": module, "name": sid})
            row["ok"] = ok
            if count is not None:
                row["count"] = count
            row["error"] = self.config.redact(error)[:500] if error else None
            if note is not None:
                row["note"] = note
            row["updated_at"] = time.time()
            if ok:
                row["last_ok_at"] = row["updated_at"]

    # -- providers -----------------------------------------------------------------------------
    def add_search(self, module: str, fn: Callable) -> None:
        self._search.append((module, fn))

    def add_nearby(self, module: str, fn: Callable) -> None:
        self._nearby.append((module, fn))

    def add_events(self, module: str, fn: Callable) -> None:
        self._events.append((module, fn))

    def add_ai_tool(self, module: str, name: str, description: str, input_schema: dict, fn: Callable) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", name):
            raise ValueError(f"AI tool name {name!r} must be snake_case")
        with self._lock:
            if name in self._ai_tools:
                raise ValueError(f"AI tool {name!r} registered twice")
            self._ai_tools[name] = {"module": module, "name": name, "description": description,
                                    "input_schema": input_schema, "fn": fn}

    def ai_tools(self) -> list[dict]:
        with self._lock:
            return [dict(t) for t in self._ai_tools.values()]

    def call_ai_tool(self, name: str, args: dict) -> Any:
        tool = self._ai_tools.get(name)
        if tool is None:
            raise KeyError(name)
        return tool["fn"](args or {})

    def _run_providers(self, providers, call, label: str, budget_s: float = 6.0) -> list[dict]:
        results: list[dict] = []
        lock = threading.Lock()

        def run(module, fn):
            try:
                items = call(fn) or []
                for item in items:
                    if isinstance(item, dict):
                        item.setdefault("module", module)
                with lock:
                    results.extend(i for i in items if isinstance(i, dict))
            except Exception as err:  # one bad provider never breaks the rest
                self.log.warning("%s provider of %s failed: %s", label, module, err)

        threads = [threading.Thread(target=run, args=p, daemon=True) for p in providers]
        for t in threads:
            t.start()
        deadline = time.monotonic() + budget_s
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        with lock:
            return list(results)

    def run_search(self, q: str, limit: int = 20) -> list[dict]:
        items = self._run_providers(self._search, lambda fn: fn(q, limit), "search")
        seen = set()
        out = []
        for item in sorted(items, key=lambda i: -(i.get("score") or 0.0)):
            key = (item.get("module"), item.get("kind"), item.get("id"))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out[: max(1, limit)]

    def run_nearby(self, lat: float, lon: float, radius_km: float, limit: int = 15) -> list[dict]:
        items = self._run_providers(self._nearby, lambda fn: fn(lat, lon, radius_km, limit), "nearby")
        for item in items:
            if "distance_km" not in item and geo.valid_latlon(item.get("lat"), item.get("lon")):
                item["distance_km"] = round(geo.haversine_km(lat, lon, float(item["lat"]), float(item["lon"])), 2)
        items.sort(key=lambda i: i.get("distance_km") if i.get("distance_km") is not None else 1e9)
        return items

    def collect_events(self, since_ts: float, kinds: set[str] | None = None) -> list[dict]:
        items = self._run_providers(self._events, lambda fn: fn(since_ts), "events", budget_s=10.0)
        out = []
        for item in items:
            if kinds and item.get("kind") not in kinds:
                continue
            if not geo.valid_latlon(item.get("lat"), item.get("lon")):
                continue
            t = item.get("time")
            if isinstance(t, (int, float)) and t < since_ts:
                continue
            out.append(item)
        out.sort(key=lambda i: -(i.get("time") or 0) if isinstance(i.get("time"), (int, float)) else 0)
        return out

    # -- event history (CONTRACT.md section 15.3) ------------------------------------------------
    @property
    def history(self):
        """The event history (:class:`meridian.history.EventHistory`), created on first use.
        ``server.py`` schedules its snapshots; tests and tools can call it directly."""
        hist = self.__dict__.get("_history")
        if hist is None:
            with self._lock:
                hist = self.__dict__.get("_history")
                if hist is None:
                    from .history import EventHistory
                    hist = EventHistory(self.store, self.collect_events, log=logging.getLogger("meridian.history"))
                    self._history = hist
        return hist

    # -- manifest ------------------------------------------------------------------------------
    def frontend_assets(self) -> dict[str, dict]:
        assets: dict[str, dict] = {}
        layers_dir = WEB_DIR / "js" / "layers"
        css_dir = WEB_DIR / "css"
        if layers_dir.is_dir():
            for p in sorted(layers_dir.glob("*.js")):
                assets.setdefault(p.stem, {})["js"] = f"/js/layers/{p.name}"
        if css_dir.is_dir():
            for p in sorted(css_dir.glob("*.css")):
                if p.stem != "core":
                    assets.setdefault(p.stem, {})["css"] = f"/css/{p.name}"
        if self.only:
            assets = {k: v for k, v in assets.items() if k in self.only}
        return assets

    def manifest(self) -> dict:
        from . import __version__, APP_NAME
        assets = self.frontend_assets()
        names = sorted(set(self.modules) | set(assets))
        modules = []
        for name in names:
            info = self.modules.get(name)
            modules.append({
                "name": name,
                "backend": info is not None,
                "ok": info.ok if info else True,
                "error": info.error if info else None,
                "js": assets.get(name, {}).get("js"),
                "css": assets.get(name, {}).get("css"),
            })
        return {
            "app": {"name": APP_NAME, "version": __version__, "started_at": self.started_at},
            "modules": modules,
            "layers": self.layer_specs,
            "keys": self.config.key_status(),
            "ai_tools": [t["name"] for t in self.ai_tools()],
            "only": sorted(self.only) if self.only else None,
        }


# ---------------------------------------------------------------------------------------------
# What a module sees
# ---------------------------------------------------------------------------------------------
class ModuleContext:
    """Passed to ``register(m)``. Everything a module registers is tagged with its name."""

    def __init__(self, app: App, name: str):
        self.app = app
        self.name = name
        self.config = app.config
        self.store = app.store
        self.net = app.net
        self.log = logging.getLogger(f"meridian.{name}")
        self.data_dir = app.config.data_dir / "build" / name
        self.raw_dir = app.config.data_dir / "raw" / name
        self.cache_dir = app.config.data_dir / "cache" / name

    # -- HTTP ----------------------------------------------------------------------------------
    def route(self, method: str, path: str, *, body_max: int = 65536, activity: bool = True) -> Callable:
        """Register a handler. ``activity=False`` keeps calls from marking the module active (for
        reads such as replay history that must not speed up live polling)."""
        prefix = f"/api/{self.name}"
        if not (path == prefix or path.startswith(prefix + "/")):
            raise ValueError(f"module {self.name} routes must live under {prefix}/ (got {path})")

        def decorator(fn: Callable) -> Callable:
            self.app.add_route(method, path, fn, self.name, body_max, activity)
            return fn

        return decorator

    def json_response(self, obj: Any, *, cache: str = "no-store", etag: bool = False) -> Response:
        return Response.json(obj, cache=cache, etag=etag)

    def serve_file(self, path: Path, content_type: str = "application/json; charset=utf-8") -> Response:
        """Serve a built file (usually JSON/GeoJSON) with an ETag and cached gzip. 503 while missing."""
        return _FILE_CACHE.response(Path(path), content_type)

    # -- background work -----------------------------------------------------------------------
    def task(self, name: str, fn: Callable[[], Any], *, every: float, initial_delay: float = 0.0,
             when: Callable[[], bool] | None = None) -> None:
        """Run ``fn()`` every ``every`` seconds in a worker thread. ``when()`` False skips a run."""
        self.app.scheduler.add_periodic(f"{self.name}.{name}", fn, every=every,
                                        initial_delay=initial_delay, when=when)

    def thread(self, name: str, fn: Callable[[threading.Event], Any]) -> None:
        """Run ``fn(stop_event)`` in its own thread; restarted with backoff if it returns or raises."""
        self.app.scheduler.add_thread(f"{self.name}.{name}", fn)

    def dataset(self, name: str, build: Callable, *, max_age_hours: float = 168.0, description: str = "",
                after: tuple[str, ...] = ()) -> None:
        """Register a static dataset. ``build(ds)`` writes files into ``ds.out_dir`` (data/build/<module>/)."""
        self.app.datasets.register(self, name, build, max_age_hours=max_age_hours,
                                   description=description, after=after)

    def dataset_ready(self, name: str) -> bool:
        return self.app.datasets.ready(f"{self.name}.{name}")

    def dataset_built_at(self, name: str) -> float | None:
        return self.app.datasets.built_at(f"{self.name}.{name}")

    def on_dataset(self, name: str, fn: Callable[[], Any]) -> None:
        """Call ``fn()`` after the dataset is (re)built, and once at start-up if it is already built."""
        self.app.datasets.on_built(f"{self.name}.{name}", fn)

    # -- discovery -------------------------------------------------------------------------------
    def layer(self, id: str, title: str, group: str, *, description: str = "", source: str = "",
              refresh: str = "", use: str = "", count: Callable[[], int | None] | None = None) -> None:
        """Describe a layer for the layer table. The browser registers the layer itself."""
        spec = {"id": id, "module": self.name, "title": title, "group": group, "description": description,
                "source": source, "refresh": refresh, "use": use}
        if count is not None:
            spec["_count"] = count
        self.app.layer_specs.append(spec)

    def search(self, fn: Callable[[str, int], list[dict]]) -> None:
        self.app.add_search(self.name, fn)

    def nearby(self, fn: Callable[[float, float, float, int], list[dict]]) -> None:
        self.app.add_nearby(self.name, fn)

    def events(self, fn: Callable[[float], list[dict]]) -> None:
        self.app.add_events(self.name, fn)

    def ai_tool(self, name: str, description: str, input_schema: dict, fn: Callable[[dict], Any]) -> None:
        self.app.add_ai_tool(self.name, name, description, input_schema, fn)

    def key(self, env_name: str, description: str, url: str = "", *, secret: bool = True) -> None:
        self.config.declare(env_name, description=description, url=url, module=self.name, secret=secret)

    def source(self, sid: str, name: str, url: str, licence: str, *, refresh: str = "", layer: str = "",
               needs_key: str | None = None, optional: bool = False) -> None:
        """Declare a data source. ``optional=True`` (blocked here, needs a key, off by default) keeps
        its failures out of the status-bar warning count."""
        self.app.declare_source(self.name, sid, name=name, url=url, licence=licence, refresh=refresh,
                                layer=layer, needs_key=needs_key, optional=bool(optional))

    def status(self, sid: str, *, ok: bool | None = True, count: int | None = None, error: str | None = None,
               note: str | None = None) -> None:
        self.app.set_status(self.name, sid, ok=ok, count=count, error=error, note=note)

    # -- activity --------------------------------------------------------------------------------
    def touch(self, key: str | None = None) -> None:
        self.app.touch(key or self.name)

    def active(self, key: str | None = None, within: float = 180.0) -> bool:
        """True when the browser used this module (any /api/<module>/ call) in the last ``within`` s."""
        return time.time() - self.app.last_active(key or self.name) <= within


# ---------------------------------------------------------------------------------------------
# Built-file serving with ETag + gzip cache
# ---------------------------------------------------------------------------------------------
class _FileCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, int, bytes, bytes, str]] = {}

    def response(self, path: Path, content_type: str) -> Response:
        import gzip
        try:
            st = path.stat()
        except FileNotFoundError:
            return Response.json({"error": {"code": "BUILDING", "message": "This dataset is still being built. "
                                             "Try again in a minute.", "retry_after": 30}}, status=503)
        key = str(path)
        with self._lock:
            item = self._items.get(key)
        if item is None or item[0] != st.st_mtime or item[1] != st.st_size:
            body = path.read_bytes()
            gz = gzip.compress(body, compresslevel=6) if len(body) > 1024 else b""
            etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
            item = (st.st_mtime, st.st_size, body, gz, etag)
            with self._lock:
                self._items[key] = item
                if len(self._items) > 64:
                    self._items.pop(next(iter(self._items)))
        resp = Response(item[2], content_type=content_type, cache="no-cache", etag=item[4])
        resp.gzipped = item[3] or None
        return resp


_FILE_CACHE = _FileCache()
