"""A one-shot text completion for other modules (CONTRACT.md section 15.4).

    from meridian.modules.ai import helper
    helper.bind(m)                                   # once, in register(m): any module's context
    text = helper.complete(system, prompt, max_tokens=1200)   # str, or None when off or failing

It reuses the Ask assistant's settings and provider classes (``AI_PROVIDER``, ``ANTHROPIC_API_KEY``,
``ANTHROPIC_MODEL``, ``ANTHROPIC_FALLBACK``, ``OLLAMA_URL``, ``OLLAMA_MODEL``), so Claude with your key
or a local Ollama model answers, exactly as the assistant would choose. There are no tools: the model
sees only the system prompt and the prompt. Effort is ``low`` (short factual writing; on Claude Opus
5.5 thinking can't be switched off, so effort is what keeps thinking inside ``max_tokens``).

Guarantees:
- ``None`` when the assistant is off (``AI_PROVIDER=off``, no key and no Ollama, the ai module not
  loaded, nothing bound) and on any error, timeout, refusal or empty answer. It never raises.
- A hard wall-clock timeout (``timeout``, default 60 s): the call runs in a worker thread and is
  abandoned when the time is up.
- Secrets are never logged or returned: log lines carry error codes and redacted messages only.
- At most two completions run at once; another waits for a slot within its own timeout.

The binding is process-wide (one App per server process), so one ``bind`` from any module makes the
helper available to every module.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from .providers import AnthropicProvider, OllamaProvider, ProviderError

DEFAULT_TIMEOUT = 60.0
MIN_TIMEOUT = 5.0
MAX_TIMEOUT = 180.0
MAX_TOKENS_CAP = 8000
HELPER_EFFORT = "low"
MAX_INPUT_CHARS = 200_000     # system + prompt; longer input is refused (None) rather than cut silently
MAX_OUTPUT_CHARS = 20_000

_log = logging.getLogger("meridian.ai.helper")
_bind_lock = threading.Lock()
_slots = threading.BoundedSemaphore(2)
_state: dict[str, Any] = {"app": None, "assistant": None}
_last: dict[str, Any] = {}


def bind(target) -> None:
    """Remember the App (from a ModuleContext or the App itself). Idempotent; a new App replaces the old."""
    app = getattr(target, "app", None) or target
    with _bind_lock:
        if _state["app"] is not app:
            _state["app"] = app
            _state["assistant"] = None


def unbind() -> None:
    """Forget the App (tests)."""
    with _bind_lock:
        _state["app"] = None
        _state["assistant"] = None


def _assistant():
    """The ai module's Assistant (settings and provider choice), or None when nothing is bound or the
    ai module isn't loaded (or failed to load) in the bound App."""
    with _bind_lock:
        app = _state["app"]
        if app is None:
            return None
        info = (getattr(app, "modules", None) or {}).get("ai")
        ctx = (getattr(app, "contexts", None) or {}).get("ai")
        if info is None or not getattr(info, "ok", False) or ctx is None:
            return None
        if _state["assistant"] is None:
            from .engine import Assistant      # late import: the engine pulls in the tool set
            _state["assistant"] = Assistant(ctx)
        return _state["assistant"]


class _OneShotAnthropic(AnthropicProvider):
    """The assistant's Claude provider with the caller's max_tokens and no prompt caching (a one-off
    request would only pay the cache-write premium)."""

    def __init__(self, *args, max_tokens: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.one_shot_max_tokens = max_tokens

    def build(self, state: dict, final: bool):
        body, betas = super().build(state, final)
        body["max_tokens"] = self.one_shot_max_tokens
        body.pop("cache_control", None)
        body["system"] = [{"type": "text", "text": state["system"]}]
        return body, betas


class _CappedNet:
    """Wraps m.net for Ollama: adds ``options.num_predict`` (Ollama's max_tokens) to the chat request."""

    def __init__(self, net, max_tokens: int):
        self._net = net
        self._max = max_tokens

    def request(self, url, *, data=None, **kw):
        if data is not None and str(url).endswith("/api/chat"):
            try:
                body = json.loads(data.decode("utf-8"))
                body.setdefault("options", {})["num_predict"] = self._max
                data = json.dumps(body).encode("utf-8")
            except (ValueError, AttributeError, UnicodeDecodeError):
                pass
        return self._net.request(url, data=data, **kw)

    def __getattr__(self, name):
        return getattr(self._net, name)


def _provider(assistant, max_tokens: int):
    """(provider or None, info): a capped copy of the provider the assistant would use."""
    base, info = assistant.resolve()
    if base is None:
        return None, info
    s = assistant.settings()
    if base.name == "anthropic":
        return _OneShotAnthropic(assistant.net, api_key=assistant.config.get("ANTHROPIC_API_KEY") or "",
                                 model=s["model"], effort=HELPER_EFFORT, fallback=s["fallback"], log=assistant.log,
                                 redact=assistant.redact, max_tokens=max_tokens), info
    if base.name == "ollama":
        return OllamaProvider(_CappedNet(assistant.net, max_tokens), base_url=base.base, model=base.model,
                              log=assistant.log), info
    return None, {"code": "UNKNOWN_PROVIDER"}


def status() -> dict:
    """What would answer right now (never a secret): {ready, provider, provider_label, model, code, message}."""
    try:
        assistant = _assistant()
        if assistant is None:
            return {"ready": False, "provider": "off", "provider_label": None, "model": None, "code": "NOT_LOADED",
                    "message": "The ai module isn't loaded."}
        base, info = assistant.resolve()
        if base is None:
            return {"ready": False, "provider": "off", "provider_label": None, "model": None,
                    "code": info.get("code"), "message": info.get("message")}
        return {"ready": True, "provider": base.name, "provider_label": base.label, "model": base.model,
                "code": None, "message": None}
    except Exception as err:   # a status call never raises
        return {"ready": False, "provider": "off", "provider_label": None, "model": None, "code": "ERROR",
                "message": _safe(err)}


def last() -> dict:
    """The last completion's outcome (provider, model, usage, elapsed_s, code, at): no text, no secrets."""
    return dict(_last)


def _safe(err: Any) -> str:
    text = getattr(err, "message", None) or str(err)
    try:
        app = _state["app"]
        if app is not None:
            text = app.config.redact(str(text))
    except Exception:
        pass
    return str(text)[:300]


def _one_message(state: dict) -> None:
    """``provider.start`` lays out (context, question); the helper sends one plain user message."""
    msg = state["messages"][-1]
    content = msg.get("content")
    if isinstance(content, list):
        msg["content"] = [b for b in content if not (b.get("type") == "text" and not (b.get("text") or "").strip())]
    elif isinstance(content, str):
        msg["content"] = content.strip()


def complete(system: str, prompt: str, max_tokens: int = 1200, *, timeout: float = DEFAULT_TIMEOUT,
             meta: dict | None = None) -> str | None:
    """Text from the configured provider, or None when it is off, busy past ``timeout``, failing,
    refusing or empty. ``meta`` (optional dict) receives provider, model, usage, elapsed_s and code."""
    started = time.monotonic()
    out: dict[str, Any] = {"provider": "off", "model": None, "code": None, "elapsed_s": 0.0}

    def finish(text: str | None, code: str | None) -> str | None:
        out["code"] = code
        out["elapsed_s"] = round(time.monotonic() - started, 2)
        _last.clear()
        _last.update(out, at=time.time())
        if meta is not None:
            meta.update(out)
        return text

    try:
        if not isinstance(system, str) or not isinstance(prompt, str) or not prompt.strip():
            return finish(None, "BAD_INPUT")
        if len(system) + len(prompt) > MAX_INPUT_CHARS:
            return finish(None, "TOO_LONG")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 16:
            return finish(None, "BAD_INPUT")
        max_tokens = min(max_tokens, MAX_TOKENS_CAP)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        timeout = min(MAX_TIMEOUT, max(MIN_TIMEOUT, timeout))
        assistant = _assistant()
        if assistant is None:
            return finish(None, "NOT_LOADED")
        provider, info = _provider(assistant, max_tokens)
        if provider is None:
            return finish(None, info.get("code") or "OFF")
        out.update(provider=provider.name, model=provider.model)
        if not _slots.acquire(timeout=max(0.0, timeout - (time.monotonic() - started))):
            return finish(None, "BUSY")
        box: dict[str, Any] = {}

        def work():
            try:
                deadline = provider.clock() + max(3.5, timeout - (time.monotonic() - started))
                state = provider.start(system, [], [], prompt, "")
                _one_message(state)
                box["turn"] = provider.complete(state, final=True, deadline=deadline)
            except ProviderError as err:
                box["error"], box["message"] = err.code, err.message
            except Exception as err:   # a bug or an unreadable response: still None
                box["error"], box["message"] = "ERROR", _safe(err)
            finally:
                _slots.release()

        thread = threading.Thread(target=work, daemon=True, name="ai-helper")
        try:
            thread.start()
        except Exception:
            _slots.release()
            raise
        thread.join(max(0.0, timeout - (time.monotonic() - started)))
        if thread.is_alive():
            _log.warning("ai helper: %s did not answer within %.0f s", provider.name, timeout)
            return finish(None, "TIMEOUT")
        if "error" in box:
            _log.warning("ai helper: %s failed (%s): %s", provider.name, box["error"], _safe(box["message"]))
            return finish(None, box["error"])
        turn = box.get("turn")
        if turn is None:
            return finish(None, "ERROR")
        out["usage"] = dict(turn.usage or {})
        if turn.model:
            out["model"] = turn.model
        if turn.stop == "refusal":
            return finish(None, "REFUSED")
        text = (turn.text or "").strip()
        if turn.stop == "max_tokens" and text:
            # cut short: keep whole paragraphs only (a dangling half sentence can read as a fact)
            cut = text.rfind("\n\n")
            text = text[:cut].strip() if cut > 0 else ""
            if not text:
                return finish(None, "CUT_SHORT")
        if not text:
            return finish(None, "EMPTY")
        return finish(text[:MAX_OUTPUT_CHARS], None)
    except Exception as err:   # the contract: never raise
        _log.warning("ai helper: unexpected failure: %s", _safe(err))
        return finish(None, "ERROR")
