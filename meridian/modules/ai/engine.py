"""The assistant's agent loop: provider choice, the tool set, tool execution and the answer.

One question runs at most MAX_ROUNDS tool rounds inside TIME_BUDGET seconds. Every module tool from
``m.app.ai_tools()`` is offered next to Meridian's own data tools and map tools; the list is rebuilt
for every question because modules register tools as they start. Tool results are untrusted data:
they are size-capped, passed back as tool results only, and the system prompt tells the model never
to follow instructions found in them. Map actions are validated here and again in the browser.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .prompt import FINAL_ROUND_NOTE, SYSTEM_PROMPT, TIME_NOTE, view_context
from .providers import (BAD_TOOLS, DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_OLLAMA_URL, EFFORTS,
                        AnthropicProvider, OllamaProbe, OllamaProvider, ProviderError, ToolCall,
                        check_ollama_url, has_model, model_features, pick_ollama_model)
from .tools import (ACTION_TOOL_NAMES, ACTION_TOOLS, DATA_FUNCS, DATA_TOOL_NAMES, DATA_TOOLS, Actions,
                    ToolContext, ToolInputError, run_action)
from .util import TOOL_NAME_RE, collect_entities, dumps, fit_json, mentioned_entities, preview

MAX_ROUNDS = 8              # tool rounds per answer
TIME_BUDGET = 90.0          # seconds per answer, all rounds included
FINAL_RESERVE = 25.0        # stop calling tools when less than this is left, and write the answer
TOOL_TIMEOUT = 15.0         # per tool call
MAX_CALLS_PER_ROUND = 12
MAX_PAUSES = 2
SETUP_MESSAGE = "Add ANTHROPIC_API_KEY to the .env file and restart Meridian, or start Ollama."
PROVIDERS = ("auto", "anthropic", "ollama", "off")


class Assistant:
    def __init__(self, m, *, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.m = m
        self.app = m.app
        self.net = m.net
        self.config = m.config
        self.log = m.log
        self.clock = clock
        self.wall = wall
        self.sleep = sleep
        self.probe = OllamaProbe(m.net, clock)
        self._lock = threading.Lock()
        self._warned: set[str] = set()
        self.stats = {"questions": 0, "answered": 0, "failed": 0, "stopped": 0, "input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "last_at": None,
                      "last_error": None}

    def redact(self, text: str) -> str:
        try:
            return self.config.redact(text)
        except Exception:
            return text

    # -- settings and provider choice --------------------------------------------------------------
    def settings(self) -> dict:
        get = self.config.get
        provider = (get("AI_PROVIDER", "auto") or "auto").strip().lower()
        effort = (get("ANTHROPIC_EFFORT", DEFAULT_EFFORT) or DEFAULT_EFFORT).strip().lower()
        return {
            "provider": provider if provider in PROVIDERS else "auto",
            "provider_raw": provider,
            "model": (get("ANTHROPIC_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL).strip(),
            "effort": effort if effort in EFFORTS else DEFAULT_EFFORT,
            "fallback": self.config.get_bool("ANTHROPIC_FALLBACK", True),
            "ollama_url": get("OLLAMA_URL", DEFAULT_OLLAMA_URL) or DEFAULT_OLLAMA_URL,
            "ollama_model": (get("OLLAMA_MODEL") or "").strip() or None,
        }

    def resolve(self, *, fresh: bool = False) -> tuple[Any, dict]:
        """(provider or None, info). info always says why, in words the user can act on."""
        s = self.settings()
        info: dict[str, Any] = {"setting": s["provider"]}
        if s["provider_raw"] not in PROVIDERS:
            info["warning"] = f'AI_PROVIDER "{s["provider_raw"][:20]}" isn\'t one of {", ".join(PROVIDERS)}; using auto.'
        if s["provider"] == "off":
            info.update(code="DISABLED", message="The assistant is switched off (AI_PROVIDER=off in the .env file).")
            return None, info
        has_key = self.config.has("ANTHROPIC_API_KEY")
        if s["provider"] in ("auto", "anthropic") and has_key:
            provider = AnthropicProvider(self.net, api_key=self.config.get("ANTHROPIC_API_KEY") or "",
                                         model=s["model"], effort=s["effort"], fallback=s["fallback"],
                                         log=self.log, clock=self.clock, sleep=self.sleep, redact=self.redact)
            return provider, info
        if s["provider"] == "anthropic":
            info.update(code="NOT_CONFIGURED", message="AI_PROVIDER is anthropic but ANTHROPIC_API_KEY isn't set. "
                                                       "Add it to the .env file, then restart Meridian.")
            return None, info
        base, why = check_ollama_url(s["ollama_url"])
        if base is None:
            info.update(code="OLLAMA_URL_REFUSED", message=why)
            return None, info
        probe = self.probe.check(base, fresh=fresh)
        info["ollama"] = {"url": base, "reachable": probe["ok"], "models": probe["models"][:20]}
        if probe["ok"]:
            model = pick_ollama_model(probe["models"], s["ollama_model"])
            if not model:
                info.update(code="OLLAMA_NO_MODEL", message="Ollama is running but has no models. Install one with "
                                                            "tool support, for example: ollama pull qwen3:8b")
                return None, info
            if s["ollama_model"] and not has_model(probe["models"], s["ollama_model"]):
                info["warning"] = f'Ollama doesn\'t list "{s["ollama_model"]}". Install it with: ollama pull {s["ollama_model"]}'
            return OllamaProvider(self.net, base_url=base, model=model, log=self.log, clock=self.clock), info
        if s["provider"] == "ollama":
            info.update(code="OLLAMA_UNREACHABLE", message=f"Ollama isn't answering at {base}. Start it (ollama serve), "
                                                           "then try again.")
            return None, info
        info.update(code="NOT_CONFIGURED", message=SETUP_MESSAGE)
        return None, info

    def status(self, *, fresh: bool = False) -> dict:
        provider, info = self.resolve(fresh=fresh)
        s = self.settings()
        core = list(DATA_TOOL_NAMES) + list(ACTION_TOOL_NAMES)
        modules = []
        for tool in self.app.ai_tools():
            name = tool.get("name")
            if name in core:
                continue
            entry = {"name": name, "module": tool.get("module")}
            if name in BAD_TOOLS:
                entry["rejected"] = BAD_TOOLS[name]
            modules.append(entry)
        with self._lock:
            stats = dict(self.stats)
        out: dict[str, Any] = {
            "ready": provider is not None,
            "provider": provider.name if provider else "off",
            "provider_label": provider.label if provider else None,
            "model": provider.model if provider else None,
            "setting": info.get("setting"),
            "code": info.get("code"),
            "message": info.get("message"),
            "warning": info.get("warning"),
            "anthropic": {"configured": self.config.has("ANTHROPIC_API_KEY"), "model": s["model"],
                          "effort": s["effort"], "fallback": s["fallback"],
                          "features": sorted(model_features(s["model"]))},
            "ollama": info.get("ollama") or {"url": check_ollama_url(s["ollama_url"])[0], "reachable": None},
            "tools": {"core": core, "modules": modules, "count": len(core) + len(modules)},
            "limits": {"max_rounds": MAX_ROUNDS, "time_budget_s": TIME_BUDGET},
            "stats": stats,
        }
        if provider is None and info.get("code") in ("NOT_CONFIGURED", "OLLAMA_UNREACHABLE"):
            out["setup"] = SETUP_MESSAGE
        return out

    # -- tools -----------------------------------------------------------------------------------
    def toolset(self) -> tuple[list[dict], dict[str, dict]]:
        """(specs for the model, name -> how to run it). Core tools first, module tools by name."""
        specs: list[dict] = []
        registry: dict[str, dict] = {}
        for spec in DATA_TOOLS:
            specs.append(spec)
            registry[spec["name"]] = {"kind": "data", "module": "ai"}
        for spec in ACTION_TOOLS:
            specs.append(spec)
            registry[spec["name"]] = {"kind": "map", "module": "ai"}
        for tool in sorted(self.app.ai_tools(), key=lambda t: str(t.get("name"))):
            name = str(tool.get("name") or "")
            module = str(tool.get("module") or "?")
            problem = None
            schema = tool.get("input_schema")
            if name in registry:
                problem = "its name clashes with a built-in assistant tool"
            elif not TOOL_NAME_RE.fullmatch(name):
                problem = "its name isn't valid"
            elif name in BAD_TOOLS:
                problem = "the provider rejected its definition earlier"
            elif not isinstance(schema, dict) or schema.get("type") != "object":
                problem = 'its input_schema must be a JSON Schema object ({"type": "object", ...})'
            elif len(dumps(schema)) > 8000:
                problem = "its input_schema is over 8 KB"
            if problem:
                if name not in self._warned:
                    self._warned.add(name)
                    self.log.warning("ai: leaving out tool %s from module %s: %s", name, module, problem)
                continue
            desc = str(tool.get("description") or "").strip() or f"A read-only look-up from the {module} module."
            specs.append({"name": name, "description": f"{desc[:1500]} (Meridian {module} module.)",
                          "input_schema": schema})
            registry[name] = {"kind": "module", "module": module}
        return specs, registry

    def _known_layers(self, view: dict) -> set[str]:
        ids = {r["id"] for r in view.get("layers") or []}
        ids |= set(view.get("layers_on") or [])
        for spec in getattr(self.app, "layer_specs", []) or []:
            if isinstance(spec, dict) and isinstance(spec.get("id"), str):
                ids.add(spec["id"])
        return ids

    def _layer_titles(self, view: dict) -> dict[str, str]:
        titles = {}
        for spec in getattr(self.app, "layer_specs", []) or []:
            if isinstance(spec, dict) and spec.get("id") and spec.get("title"):
                titles[spec["id"]] = str(spec["title"])[:60]
        for row in view.get("layers") or []:
            if row.get("title"):
                titles[row["id"]] = row["title"]
        return titles

    def _call(self, name: str, args: dict, info: dict, ctx: ToolContext) -> Any:
        if info["kind"] == "data":
            return DATA_FUNCS[name](args, ctx)
        return self.app.call_ai_tool(name, args)

    def run_tools(self, calls: list[ToolCall], registry: dict, ctx: ToolContext, actions: Actions,
                  known_layers: set[str], limit: int, deadline: float,
                  emit: Callable[[str, dict], None]) -> tuple[list[tuple[ToolCall, str, bool]], list[dict], list[Any]]:
        """Run one round of tool calls (data and module tools in parallel). Returns
        (results for the provider, trace entries, raw results for entity chips)."""
        n = len(calls)
        results: list[Any] = [None] * n
        traces: list[dict | None] = [None] * n
        raw: list[Any] = []
        workers: list[tuple[int, threading.Thread, dict, float]] = []

        def finish(i: int, call: ToolCall, info: dict | None, value: Any, ok: bool, started: float,
                   error: str | None = None):
            ms = int((self.clock() - started) * 1000)
            text, cut = fit_json(value, limit)
            results[i] = (call, text, not ok)
            entry: dict[str, Any] = {"tool": call.name, "args": preview(call.input if isinstance(call.input, dict) else {}),
                                     "ok": ok, "ms": ms, "kind": info["kind"] if info else "unknown",
                                     "module": info["module"] if info else None, "summary": _summary(value, ok)}
            if error:
                entry["error"] = error
            if cut:
                entry["truncated"] = True
            traces[i] = entry

        for i, call in enumerate(calls):
            started = self.clock()
            info = registry.get(call.name)
            if i >= MAX_CALLS_PER_ROUND:
                finish(i, call, info, {"error": f"Too many tool calls in one turn (limit {MAX_CALLS_PER_ROUND})."},
                       False, started, "skipped")
                continue
            if info is None:
                finish(i, call, None, {"error": f"There is no tool called {call.name}."}, False, started, "unknown tool")
                continue
            if call.bad_json or not isinstance(call.input, dict):
                finish(i, call, info, {"INVALID_JSON": str(call.input)[:500],
                                       "error": "The arguments weren't a valid JSON object."}, False, started, "bad arguments")
                continue
            if info["kind"] == "map":
                try:
                    finish(i, call, info, run_action(call.name, call.input, actions, known_layers), True, started)
                except ToolInputError as err:
                    finish(i, call, info, {"error": str(err)}, False, started, str(err)[:160])
                continue
            box: dict[str, Any] = {}

            def work(name=call.name, args=dict(call.input), info=info, box=box):
                try:
                    box["value"] = self._call(name, args, info, ctx)
                except ToolInputError as err:
                    box["input_error"] = str(err)
                except Exception as err:   # a broken tool never breaks the answer
                    box["error"] = f"{type(err).__name__}: {self.redact(str(err))}"[:300]

            thread = threading.Thread(target=work, daemon=True, name=f"ai-tool-{call.name}")
            emit("tool", {"tool": call.name})
            thread.start()
            workers.append((i, thread, box, started))

        for i, thread, box, started in workers:
            call = calls[i]
            info = registry.get(call.name)
            wait = max(0.0, min(started + TOOL_TIMEOUT, deadline) - self.clock())
            thread.join(wait)
            if thread.is_alive():
                finish(i, call, info, {"error": f"The tool didn't answer within {int(TOOL_TIMEOUT)} s."}, False,
                       started, "timed out")
            elif "input_error" in box:
                finish(i, call, info, {"error": box["input_error"]}, False, started, box["input_error"][:160])
            elif "error" in box:
                self.log.warning("ai: tool %s failed: %s", call.name, box["error"])
                finish(i, call, info, {"error": f"The tool failed: {box['error']}"}, False, started, box["error"][:160])
            else:
                finish(i, call, info, box.get("value"), True, started)
                raw.append(box.get("value"))
        return [r for r in results if r is not None], [t for t in traces if t is not None], raw

    # -- the loop --------------------------------------------------------------------------------
    def run(self, messages: list[dict], view: dict, *, cancel: threading.Event | None = None,
            on_event: Callable[[str, dict], None] | None = None) -> dict:
        started = self.clock()
        deadline = started + TIME_BUDGET
        cancel = cancel or threading.Event()

        def emit(kind: str, data: dict) -> None:
            if on_event is not None:
                try:
                    on_event(kind, data)
                except Exception:
                    pass

        provider, info = self.resolve()
        if provider is None:
            message = info.get("message") or SETUP_MESSAGE
            return {"ok": False, "reply": message, "actions": [], "trace": [], "entities": [], "provider": "off",
                    "provider_label": None, "model": None, "usage": None, "rounds": 0, "stopped": None,
                    "error": {"code": info.get("code") or "NOT_CONFIGURED", "message": message},
                    "setup": SETUP_MESSAGE, "elapsed_ms": 0}

        specs, registry = self.toolset()
        history, question = messages[:-1], messages[-1]["content"]
        context = view_context(view, self.wall(), self._layer_titles(view))
        state = provider.start(SYSTEM_PROMPT, specs, history, context, question)
        ctx = ToolContext(app=self.app, net=self.net, view=view, log=self.log, wall=self.wall)
        known_layers = self._known_layers(view)
        actions = Actions()
        trace: list[dict] = []
        notes: list[str] = []
        candidates: list[dict] = []
        seen: set = set()
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 0, "requests": 0}
        rounds = 0
        pauses = 0
        final = False
        stopped: str | None = None
        reply = ""
        error: dict | None = None
        served_model = provider.model
        fallback = None
        refused = False
        last_text = ""

        while True:
            if cancel.is_set():
                stopped = "cancelled"
                break
            emit("phase", {"phase": "answer" if final else "model", "round": rounds + 1})
            try:
                turn = provider.complete(state, final=final, deadline=deadline)
            except ProviderError as err:
                error = {"code": err.code, "message": err.message}
                if err.retry_after:
                    error["retry_after"] = err.retry_after
                break
            usage["requests"] += 1
            for key, value in turn.usage.items():
                if key in usage:
                    usage[key] += value
            served_model = turn.model or served_model
            if turn.fallback:
                fallback = turn.fallback
            if turn.text:
                last_text = turn.text
            for note in turn.notes:
                notes.append(note)
                emit("note", {"text": note})
            if cancel.is_set():
                stopped = "cancelled"
                break
            if turn.stop == "refusal":
                refused = True
                stopped = "refusal"
                category = (turn.refusal or {}).get("category")
                reply = ("Claude declined to answer this question" + (f" (safety filter: {category})" if category else "")
                         + ". If that looks wrong, try asking it a different way.")
                break
            if turn.stop == "pause" and pauses < MAX_PAUSES:
                pauses += 1
                provider.add_assistant(state, turn)
                continue
            if turn.stop == "tool_use" and turn.calls and not final:
                rounds += 1
                provider.add_assistant(state, turn)
                emit("phase", {"phase": "tools", "round": rounds})
                results, entries, raw = self.run_tools(turn.calls, registry, ctx, actions, known_layers,
                                                       provider.result_limit, deadline, emit)
                if turn.notes and entries:
                    entries[0]["note"] = turn.notes[-1]
                for entry in entries:
                    entry["round"] = rounds
                    trace.append(entry)
                    emit("trace", entry)
                for value in raw:
                    collect_entities(value, candidates, seen)
                if cancel.is_set():
                    stopped = "cancelled"
                    break
                note = None
                if rounds >= MAX_ROUNDS:
                    final, stopped, note = True, "max_rounds", FINAL_ROUND_NOTE
                elif deadline - self.clock() < FINAL_RESERVE:
                    final, stopped, note = True, "time_budget", TIME_NOTE
                provider.add_results(state, results, note)
                continue
            reply = turn.text
            if turn.stop in ("max_tokens", "context"):
                stopped = stopped or turn.stop
                reply = (reply + "\n\n" if reply else "") + ("*(The answer was cut short.)*" if turn.stop == "max_tokens"
                                                            else "*(The conversation is too long: start a new chat.)*")
            break

        elapsed = self.clock() - started
        if refused or stopped == "cancelled" or error is not None:
            actions.clear()   # only a finished answer moves the map
        if not reply and error is None and stopped != "cancelled":
            reply = last_text or ("Here it is on the map." if actions.items else
                                  "I couldn't put together an answer from Meridian's data this time.")
        entities = mentioned_entities(reply, candidates, actions.entities()) if not refused and error is None else []
        ok = error is None
        result = {"ok": ok, "reply": reply, "actions": actions.list(), "trace": trace, "entities": entities,
                  "notes": notes[-6:], "provider": provider.name, "provider_label": provider.label,
                  "model": served_model, "usage": usage, "rounds": rounds, "stopped": stopped, "error": error,
                  "fallback": fallback, "elapsed_ms": int(elapsed * 1000)}
        self._record(provider, result, elapsed)
        return result

    def _record(self, provider, result: dict, elapsed: float) -> None:
        usage = result.get("usage") or {}
        with self._lock:
            self.stats["questions"] += 1
            cancelled = result.get("stopped") == "cancelled"
            self.stats["stopped" if cancelled else "answered" if result["ok"] else "failed"] += 1
            for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                self.stats[key] += int(usage.get(key) or 0)
            self.stats["last_at"] = self.wall()
            self.stats["last_error"] = result["error"]["message"] if result.get("error") else None
        sid = f"ai.{provider.name}"
        if cancelled:
            pass
        elif result["ok"]:
            self.m.status(sid, ok=True, note=f"{result['model']}, last answer in {elapsed:.1f} s")
        elif result["error"]["code"] not in ("TIMEOUT",):
            self.m.status(sid, ok=False, error=result["error"]["message"])
        self.log.info("ai: %s %s finished in %.1f s: %d rounds, %d tools, %d in / %d out tokens (%d cached)%s",
                      provider.name, result["model"], elapsed, result["rounds"], len(result["trace"]),
                      usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                      usage.get("cache_read_input_tokens", 0),
                      f", {result['error']['code']}" if result.get("error") else
                      (f", stopped: {result['stopped']}" if result.get("stopped") else ""))


def _summary(value: Any, ok: bool) -> str:
    if not ok:
        return "failed"
    if isinstance(value, dict):
        if value.get("ok") is True and isinstance(value.get("done"), str):
            return "queued for the map"
        for key in ("results", "events", "nearby", "layers", "items", "vessels", "aircraft", "cameras",
                    "features", "matches"):
            items = value.get(key)
            if isinstance(items, list):
                total = value.get("total")
                n = len(items)
                text = f"{n} result" + ("" if n == 1 else "s")
                if isinstance(total, int) and total > n:
                    text += f" of {total:,}"
                return text
        return "done"
    if isinstance(value, list):
        return f"{len(value)} item" + ("" if len(value) == 1 else "s")
    if value is None:
        return "no result"
    return "done"
