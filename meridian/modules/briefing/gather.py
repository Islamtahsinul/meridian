"""Live snapshots from other modules for the global sections and "Your areas".

Everything goes through other modules' public surfaces, in-process: their read-only AI tools
(``m.app.call_ai_tool``) and, where the contract names an HTTP endpoint, the route handler itself
(``/api/alerts/recent``, ``/api/intel/cyber``), called without HTTP. Each look-up runs in its own
thread with a timeout; a missing module is skipped silently, a failing one becomes a short note.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from meridian.registry import HTTPError, Request, Response

TOOL_TIMEOUT = 15.0
BUDGET_S = 30.0
ALERTS_LIMIT = 100
MARKET_ITEMS = ("gold", "brent", "ttf", "spx", "n225", "fx_gbpusd", "fx_eurusd", "btc")


class Skip(Exception):
    """The module that provides this is not running: leave it out without a note."""


def _module_ok(app, name: str) -> bool:
    info = (getattr(app, "modules", None) or {}).get(name)
    return bool(info is not None and getattr(info, "ok", False))


def _tool(app, name: str, args: dict) -> Any:
    tools = {t["name"] for t in app.ai_tools()}
    if name not in tools:
        raise Skip(name)
    out = app.call_ai_tool(name, args)
    if isinstance(out, dict) and out.get("error") and len(out) <= 3:
        raise RuntimeError(str(out["error"])[:160])
    return out


def _route(app, path: str, query: dict[str, str] | None = None) -> Any:
    route, params, _other = app.match("GET", path)
    if route is None:
        raise Skip(path)
    req = Request("GET", path, params, {k: [str(v)] for k, v in (query or {}).items()}, {})
    try:
        out = route.handler(req)
    except HTTPError as err:
        raise RuntimeError(f"{err.message}"[:160]) from None
    if isinstance(out, Response):
        if out.status >= 400:
            raise RuntimeError(f"HTTP {out.status}")
        try:
            return json.loads(out.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("unreadable answer") from None
    return out


def alerts_recent(app, since: float) -> list[dict]:
    """Fired alerts from the Alerts module (CONTRACT 15.4) since ``since``; Skip when it isn't running."""
    if not _module_ok(app, "alerts"):
        raise Skip("alerts")
    out = _route(app, "/api/alerts/recent", {"since": str(int(since)), "limit": str(ALERTS_LIMIT)})
    if isinstance(out, dict):
        for key in ("alerts", "items", "recent", "results"):
            if isinstance(out.get(key), list):
                out = out[key]
                break
    if not isinstance(out, list):
        raise RuntimeError("the alerts answer had an unexpected shape")
    return [a for a in out if isinstance(a, dict)][:ALERTS_LIMIT]


def space_weather(app) -> dict:
    if not _module_ok(app, "space"):
        raise Skip("space")
    return _tool(app, "space_weather_now", {})


def cyber(app, deadline: float | None = None, sleep=time.sleep) -> dict:
    """Botnet servers and internet outage signals. The intel route starts a one-off IODA poll when its
    outage data is stale; wait briefly (within the budget) for it to land."""
    if not _module_ok(app, "intel"):
        raise Skip("intel")
    out = _route(app, "/api/intel/cyber")
    deadline = deadline if deadline is not None else time.monotonic() + 10.0
    while isinstance(out, dict) and not ((out.get("meta") or {}).get("outages_at") or {}).get("ioda") \
            and time.monotonic() + 1.0 < deadline:
        sleep(1.0)
        out = _route(app, "/api/intel/cyber")
    if not isinstance(out, dict):
        raise RuntimeError("unexpected answer")
    return out


def grid_gb(app) -> dict:
    if not _module_ok(app, "infra"):
        raise Skip("infra")
    out = _tool(app, "grid_now", {"country": "GB"})
    if isinstance(out, dict) and out.get("available") is False:
        raise RuntimeError(str(out.get("message") or "no live data")[:160])
    return out


def straits(app) -> dict:
    if not _module_ok(app, "ships"):
        raise Skip("ships")
    return _tool(app, "strait_status", {})


def markets(app, deadline: float) -> list[dict]:
    if not _module_ok(app, "infra"):
        raise Skip("infra")
    if "market_quote" not in {t["name"] for t in app.ai_tools()}:
        raise Skip("market_quote")
    results: dict[str, Any] = {}

    def one(item_id: str):
        try:
            q = app.call_ai_tool("market_quote", {"symbol": item_id})
            if isinstance(q, dict):
                q.setdefault("id", item_id)
                results[item_id] = q
        except Exception:
            pass

    threads = [threading.Thread(target=one, args=(i,), daemon=True, name=f"briefing-quote-{i}") for i in MARKET_ITEMS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    quotes = [results[i] for i in MARKET_ITEMS if i in results]
    if not any(q.get("available") for q in quotes):
        raise RuntimeError("no prices are available right now")
    return quotes


def gather(app, since: float, *, budget_s: float = BUDGET_S, log=None,
           jobs: dict[str, Callable[[], Any]] | None = None) -> dict:
    """{alerts?, space?, cyber?, grid?, straits?, markets?, errors: {name: note}}. Never raises."""
    deadline = time.monotonic() + budget_s
    if jobs is None:
        jobs = {
            "alerts": lambda: alerts_recent(app, since),
            "space": lambda: space_weather(app),
            "cyber": lambda: cyber(app, min(deadline, time.monotonic() + 12.0)),
            "grid": lambda: grid_gb(app),
            "straits": lambda: straits(app),
            "markets": lambda: markets(app, min(deadline, time.monotonic() + TOOL_TIMEOUT)),
        }
    out: dict[str, Any] = {"errors": {}}
    boxes: dict[str, dict] = {}

    def run(name: str, fn: Callable[[], Any]):
        box = boxes[name]
        try:
            box["value"] = fn()
        except Skip:
            box["skip"] = True
        except Exception as err:   # one failing module never stops the briefing
            box["error"] = str(err)[:160] or type(err).__name__

    threads = []
    for name, fn in jobs.items():
        boxes[name] = {}
        t = threading.Thread(target=run, args=(name, fn), daemon=True, name=f"briefing-{name}")
        t.start()
        threads.append((name, t))
    for name, t in threads:
        t.join(max(0.0, min(deadline, time.monotonic() + TOOL_TIMEOUT + 5.0) - time.monotonic()))
    for name, t in threads:
        box = boxes[name]
        if t.is_alive():
            out["errors"][name] = "did not answer in time"
        elif box.get("skip"):
            continue
        elif "error" in box:
            out["errors"][name] = box["error"]
            if log is not None:
                log.info("briefing: %s unavailable: %s", name, box["error"])
        else:
            out[name] = box.get("value")
    return out
