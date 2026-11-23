"""Model providers, standard library only (no SDKs).

- :class:`AnthropicProvider`: the Claude Messages API, ``POST https://api.anthropic.com/v1/messages``
  over ``m.net`` with ``x-api-key`` and ``anthropic-version``.
- :class:`OllamaProvider`: a local Ollama server's ``/api/chat`` with tools. Loopback addresses only.

Each provider keeps its own message list (``state``) and turns a response into a neutral
:class:`Turn`, so the agent loop in ``engine.py`` doesn't care which one runs. Within one answer the
history is append-only (assistant content is echoed back unchanged, thinking blocks included) and
the system prompt and tool list are fixed, which keeps Claude's preserved thinking valid and the
prompt cache warm. API keys are only ever placed in request headers: never logged, never returned.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from meridian.net import NetError

from .util import dumps

# -- Anthropic ------------------------------------------------------------------------------------
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_HOST = "api.anthropic.com"
ANTHROPIC_VERSION = "2026-10-21"
DEFAULT_MODEL = "claude-opus-5-5"
CHEAPER_MODELS = ("claude-sonnet-5-5", "claude-haiku-5-5")
MAX_TOKENS = 16000             # non-streaming: room for adaptive thinking plus a short answer
REQUEST_TIMEOUT = 60.0         # per request (socket read); the whole answer has its own budget
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "medium"
BETA_FALLBACK = "server-side-fallback-2026-07-01"
BETA_UPDATES = "thinking-display-updates-2026-08-18"
INTERRUPTED = "This part of the response was interrupted before it finished."
_ACCEPT = (200, 400, 401, 402, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504, 520, 522, 524, 529)
_RETRYABLE = (408, 409, 429, 500, 502, 503, 504, 520, 522, 524, 529)

# Which request features a model takes (from Anthropic's model documentation). Unknown or older
# models get a plain request; anything a model still rejects is dropped after its first 400.
_ADAPTIVE_PREFIXES = ("claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-4-6",
                      "claude-sonnet-5", "claude-haiku-5", "claude-fable", "claude-mythos")
_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2",
                              "claude-fable-5-1", "claude-fable-5", "claude-mythos-5-2", "claude-mythos-5-1"})
_UPDATES_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1",
                             "claude-fable-5", "claude-mythos-5-2", "claude-mythos-5-1"})

# -- Ollama ---------------------------------------------------------------------------------------
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_NUM_CTX = 16384
OLLAMA_TIMEOUT = 80.0
TOOL_FAMILIES = ("qwen3", "qwen2.5", "llama3.3", "llama3.2", "llama3.1", "gpt-oss", "mistral-small", "mistral-nemo",
                 "mistral", "granite3", "command-r", "hermes3", "phi4-mini", "smollm2", "firefunction")

_lock = threading.Lock()
_disabled: dict[str, set[str]] = {}      # model -> request features it rejected (process lifetime)
BAD_TOOLS: dict[str, str] = {}           # tool name -> why the provider rejected its definition


class ProviderError(Exception):
    """A provider failure with a user-facing (already redacted) message."""

    def __init__(self, code: str, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after


@dataclass
class ToolCall:
    id: str
    name: str
    input: Any
    bad_json: bool = False


@dataclass
class Turn:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    stop: str = "end"          # end | tool_use | max_tokens | context | refusal | pause
    notes: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    model: str | None = None
    refusal: dict | None = None
    fallback: dict | None = None
    assistant: Any = None      # the provider message that goes back into the history


def model_features(model: str) -> set[str]:
    m = (model or "").strip().lower()
    feats = {"auto_cache"}
    if m.startswith(_ADAPTIVE_PREFIXES):
        feats |= {"thinking", "effort"}
    elif m.startswith("claude-opus-4-5"):
        feats.add("effort")
    if m in _FALLBACK_MODELS:
        feats.add("fallbacks")
    if m in _UPDATES_MODELS:
        feats.add("updates")
    with _lock:
        return feats - _disabled.get(m, set())


def _disable(model: str, feature: str) -> None:
    with _lock:
        _disabled.setdefault((model or "").strip().lower(), set()).add(feature)


def effort_for(model: str, effort: str) -> str:
    m = (model or "").lower()
    if m.startswith("claude-opus-4-5") and effort in ("xhigh", "max"):
        return "high"
    if m.startswith(("claude-opus-4-6", "claude-sonnet-4-6")) and effort == "xhigh":
        return "high"
    return effort


def retry_after_seconds(value: str | None, default: float) -> float:
    try:
        return max(0.0, float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _error_info(body: bytes, redact: Callable[[str], str]) -> dict:
    info = {"type": None, "message": "", "request_id": None}
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        text = body.decode("utf-8", errors="replace").strip()
        info["message"] = redact(text)[:300]
        return info
    if isinstance(data, dict):
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        info["type"] = err.get("type")
        info["message"] = redact(str(err.get("message") or data.get("message") or ""))[:300]
        info["request_id"] = data.get("request_id")
    return info


def _usage(raw: Any) -> dict:
    out = {}
    if isinstance(raw, dict):
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            v = raw.get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                out[key] = v
    return out


def echo_content(content: list[dict]) -> list[dict]:
    """The assistant content to send back. After a server-side refusal fallback, blocks before the
    last ``fallback`` block other than text are omitted (the fallback block itself stays in place)."""
    last = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    if last < 0:
        return content
    return [b for i, b in enumerate(content) if i >= last or b.get("type") in ("text", "fallback")]


class AnthropicProvider:
    name = "anthropic"
    label = "Claude"

    def __init__(self, net, *, api_key: str, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
                 fallback: bool = True, log=None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, redact: Callable[[str], str] = lambda s: s):
        self.net = net
        self._key = api_key
        self.model = (model or DEFAULT_MODEL).strip()
        self.effort = effort if effort in EFFORTS else DEFAULT_EFFORT
        self.fallback = fallback
        self.log = log
        self.clock = clock
        self.sleep = sleep
        self.redact = redact
        self.result_limit = 12_000

    def __repr__(self) -> str:   # never show the key, even in a debugger
        return f"AnthropicProvider(model={self.model!r})"

    # -- conversation state ----------------------------------------------------------------------
    def start(self, system: str, tools: list[dict], history: list[dict], context: str, question: str) -> dict:
        messages: list[dict] = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": [{"type": "text", "text": context},
                                                     {"type": "text", "text": question}]})
        specs = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools]
        return {"system": system, "tools": specs, "messages": messages, "dropped": set()}

    def add_assistant(self, state: dict, turn: Turn) -> None:
        state["messages"].append(turn.assistant)

    def add_results(self, state: dict, results: list[tuple[ToolCall, str, bool]], note: str | None = None) -> None:
        blocks: list[dict] = []
        for call, text, is_error in results:
            block = {"type": "tool_result", "tool_use_id": call.id, "content": text}
            if is_error:
                block["is_error"] = True
            blocks.append(block)
        if note:
            blocks.append({"type": "text", "text": note})
        state["messages"].append({"role": "user", "content": blocks})

    # -- the request -----------------------------------------------------------------------------
    def build(self, state: dict, final: bool) -> tuple[dict, list[str]]:
        feats = model_features(self.model)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            # One explicit cache breakpoint at the end of the static prefix (tools + system) ...
            "system": [{"type": "text", "text": state["system"], "cache_control": {"type": "ephemeral"}}],
            "messages": state["messages"],
        }
        tools = [t for t in state["tools"] if t["name"] not in state["dropped"]]
        if tools:
            body["tools"] = tools
            if final:
                body["tool_choice"] = {"type": "none"}
        if "auto_cache" in feats:
            body["cache_control"] = {"type": "ephemeral"}   # ... plus automatic caching of the growing tail
        if "thinking" in feats:
            thinking: dict[str, Any] = {"type": "adaptive"}
            if "updates" in feats:
                thinking["display"] = "updates"             # short progress notes between tool calls
            body["thinking"] = thinking
        if "effort" in feats:
            body["output_config"] = {"effort": effort_for(self.model, self.effort)}
        betas = []
        if self.fallback and "fallbacks" in feats:
            body["fallbacks"] = "default"                   # a safety-filter decline is retried server-side
            betas.append(BETA_FALLBACK)
        if "updates" in feats and "thinking" in feats:
            betas.append(BETA_UPDATES)
        return body, betas

    def complete(self, state: dict, *, final: bool, deadline: float) -> Turn:
        degraded = 0
        retried = False
        while True:
            body, betas = self.build(state, final)
            headers = {"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION,
                       "content-type": "application/json", "accept": "application/json"}
            if betas:
                headers["anthropic-beta"] = ",".join(betas)
            remaining = deadline - self.clock()
            if remaining < 3.0:
                raise ProviderError("TIMEOUT", "The answer ran out of time waiting for Claude.")
            try:
                resp = self.net.request(ANTHROPIC_URL, method="POST", data=dumps(body).encode("utf-8"),
                                        headers=headers, timeout=min(REQUEST_TIMEOUT, remaining), retries=0,
                                        accept=_ACCEPT, max_bytes=32 * 1024 * 1024)
            except NetError as err:
                reason = self.redact(str(err)).replace(f"{ANTHROPIC_HOST}: ", "")
                if "timed out" in reason.lower():
                    raise ProviderError("TIMEOUT", "Claude took too long to answer. Try again, or ask a simpler "
                                                   "question.") from None
                raise ProviderError("PROVIDER_UNREACHABLE", f"Meridian couldn't reach Claude at {ANTHROPIC_HOST} "
                                                            f"({reason[:160]}). Check the internet connection.") from None
            if resp.status == 200:
                return self.parse(resp.body)
            info = _error_info(resp.body, self.redact)
            if self.log:
                self.log.warning("ai: Claude answered HTTP %s (%s) request_id=%s: %s", resp.status,
                                 info["type"], info["request_id"], info["message"][:200])
            if resp.status == 400 and degraded < 4 and self.degrade(info["message"], state):
                degraded += 1
                continue
            if resp.status in _RETRYABLE and not retried:
                wait = retry_after_seconds(resp.headers.get("retry-after"), 2.0)
                if wait <= 8.0 and deadline - self.clock() - wait >= 15.0:
                    retried = True
                    self.sleep(wait)
                    continue
            raise self.error(resp.status, info, resp.headers.get("retry-after"))

    def degrade(self, message: str, state: dict) -> bool:
        """After a 400, drop the optional feature (or the tool) the message names. True to retry."""
        msg = (message or "").lower()
        feats = model_features(self.model)

        def drop(feature: str) -> bool:
            _disable(self.model, feature)
            if self.log:
                self.log.warning("ai: %s rejected the %s option; retrying without it", self.model, feature)
            return True

        if "anthropic-beta" in msg:
            if BETA_UPDATES in msg and "updates" in feats:
                return drop("updates")
            if BETA_FALLBACK in msg and "fallbacks" in feats:
                return drop("fallbacks")
            for feature in ("updates", "fallbacks"):
                if feature in feats:
                    return drop(feature)
        if "fallback" in msg and "fallbacks" in feats:
            return drop("fallbacks")
        if "display" in msg and "updates" in feats:
            return drop("updates")
        if ("effort" in msg or "output_config" in msg) and "effort" in feats:
            return drop("effort")
        if "thinking" in msg and "updates" in feats:
            return drop("updates")
        if "thinking" in msg and "thinking" in feats:
            return drop("thinking")
        match = re.search(r"\btools\.(\d+)", msg)
        if match:
            sent = [t for t in state["tools"] if t["name"] not in state["dropped"]]
            index = int(match.group(1))
            if index < len(sent):
                name = sent[index]["name"]
                state["dropped"].add(name)
                with _lock:
                    BAD_TOOLS[name] = message[:200]
                if self.log:
                    self.log.warning("ai: Claude rejected the definition of tool %s; leaving it out", name)
                return True
        if "cache_control" in msg and "auto_cache" in feats:
            return drop("auto_cache")
        return False

    def error(self, status: int, info: dict, retry_after: str | None) -> ProviderError:
        detail = info.get("message") or ""
        if status == 401:
            return ProviderError("PROVIDER_AUTH", "Claude didn't accept the API key. Check ANTHROPIC_API_KEY in the "
                                                  ".env file, then restart Meridian.", status=status)
        if status == 402:
            return ProviderError("PROVIDER_BILLING", "Anthropic reports a billing problem on this API account. Check "
                                                     "the plan and credit at platform.claude.com.", status=status)
        if status == 403:
            return ProviderError("PROVIDER_FORBIDDEN", f"This API key isn't allowed to do that ({detail or 'forbidden'}).",
                                 status=status)
        if status == 404:
            return ProviderError("PROVIDER_MODEL", f'Claude doesn\'t know the model "{self.model}". Check ANTHROPIC_MODEL '
                                                   "in the .env file (the default is claude-opus-5-5).", status=status)
        if status == 413:
            return ProviderError("TOO_LONG", "This conversation is too long for Claude. Start a new chat.", status=status)
        if status == 429:
            wait = retry_after_seconds(retry_after, 0.0)
            text = "Claude is rate-limiting this API key"
            text += f"; try again in {int(wait + 0.99)} s." if wait else ". Try again in a minute."
            return ProviderError("PROVIDER_RATE_LIMIT", text, status=status, retry_after=wait or None)
        if status in _RETRYABLE:
            return ProviderError("PROVIDER_UNAVAILABLE", "Claude is overloaded or unavailable right now. Try again "
                                                         "in a minute.", status=status)
        if status in (400, 422):
            return ProviderError("PROVIDER_BAD_REQUEST", f"Claude rejected the request: {detail or 'invalid request'}",
                                 status=status)
        return ProviderError("PROVIDER_ERROR", f"Claude answered with HTTP {status}: {detail or 'no details'}",
                             status=status)

    # -- the response ----------------------------------------------------------------------------
    def parse(self, raw: bytes) -> Turn:
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ProviderError("PROVIDER_ERROR", "Claude sent a reply Meridian couldn't read.") from None
        if not isinstance(data, dict):
            raise ProviderError("PROVIDER_ERROR", "Claude sent a reply Meridian couldn't read.")
        content = [b for b in (data.get("content") or []) if isinstance(b, dict)]
        echo = echo_content(content)
        turn = Turn(model=data.get("model") or self.model, usage=_usage(data.get("usage")))
        turn.assistant = {"role": "assistant", "content": echo}
        texts = []
        for block in echo:
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif kind == "tool_use":
                turn.calls.append(ToolCall(id=str(block.get("id") or ""), name=str(block.get("name") or ""),
                                           input=block.get("input") if block.get("input") is not None else {}))
            elif kind == "thinking":
                note = (block.get("thinking") or "").strip() if isinstance(block.get("thinking"), str) else ""
                if note and note != INTERRUPTED:
                    turn.notes.append(note[:400])
        turn.text = "\n\n".join(t.strip() for t in texts if t.strip())
        switches = [b for b in content if b.get("type") == "fallback"]
        iterations = (data.get("usage") or {}).get("iterations") if isinstance(data.get("usage"), dict) else None
        if switches:
            last = switches[-1]
            turn.fallback = {"from": (last.get("from") or {}).get("model"), "to": (last.get("to") or {}).get("model")}
        elif isinstance(iterations, list) and any(isinstance(i, dict) and i.get("type") == "fallback_message"
                                                  for i in iterations):
            turn.fallback = {"from": self.model, "to": data.get("model")}
        stop = data.get("stop_reason")
        if stop == "tool_use":
            turn.stop = "tool_use" if turn.calls else "end"
        elif stop == "max_tokens":
            turn.stop = "max_tokens"
        elif stop == "model_context_window_exceeded":
            turn.stop = "context"
        elif stop == "pause_turn":
            turn.stop = "pause"
        elif stop == "refusal":
            turn.stop = "refusal"
            details = data.get("stop_details") if isinstance(data.get("stop_details"), dict) else {}
            turn.refusal = {"category": details.get("category"), "explanation": details.get("explanation")}
        else:
            turn.stop = "end"
        if turn.stop != "tool_use":
            turn.calls = []   # never run tools from a cut-off, refused or paused turn
        return turn


# -- Ollama -----------------------------------------------------------------------------------------
def check_ollama_url(raw: str | None) -> tuple[str | None, str | None]:
    """(normalised base URL, None) when ``raw`` points at this computer; (None, reason) otherwise.

    Only loopback addresses are accepted (127.0.0.0/8, ::1, or the name localhost, which is mapped to
    127.0.0.1 and never looked up), so a setting can't turn the server into a relay (no SSRF)."""
    text = (raw or "").strip() or DEFAULT_OLLAMA_URL
    refused = "OLLAMA_URL must point at this computer: use http://127.0.0.1:11434, localhost or [::1]."
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        return None, "OLLAMA_URL isn't a valid URL."
    if parts.scheme not in ("http", "https"):
        return None, "OLLAMA_URL must start with http:// or https://."
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None, "OLLAMA_URL must not contain a user name or password."
    if parts.query or parts.fragment:
        return None, "OLLAMA_URL must not have a query or fragment."
    host = (parts.hostname or "").lower()
    if host == "localhost":
        ip = "127.0.0.1"
    else:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return None, refused
        if not addr.is_loopback:
            return None, refused
        ip = addr.compressed
    path = parts.path.rstrip("/")
    if path and not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", path):
        return None, "OLLAMA_URL has an unusual path."
    netloc = f"[{ip}]" if ":" in ip else ip
    if port is not None:
        netloc += f":{port}"
    return f"{parts.scheme}://{netloc}{path}", None


def is_loopback_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def pick_ollama_model(installed: list[str], wanted: str | None) -> str | None:
    if wanted:
        return wanted
    for family in TOOL_FAMILIES:
        for name in installed:
            if name.lower().startswith(family):
                return name
    return installed[0] if installed else None


def has_model(installed: list[str], wanted: str) -> bool:
    names = {n.lower() for n in installed}
    w = wanted.lower()
    return w in names or f"{w}:latest" in names or (w.endswith(":latest") and w[:-7] in names)


class OllamaProbe:
    """Is Ollama answering at a URL, and which models does it have? Cached briefly."""

    def __init__(self, net, clock: Callable[[], float] = time.monotonic):
        self.net = net
        self.clock = clock
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def check(self, base: str, *, fresh: bool = False) -> dict:
        now = self.clock()
        with self._lock:
            hit = self._cache.get(base)
        if hit and hit[0] > now and not fresh:
            return dict(hit[1])
        result: dict[str, Any] = {"ok": False, "models": [], "error": None}
        try:
            # no redirects: the request must never leave this computer, not even for one hop
            resp = self.net.request(base + "/api/tags", timeout=2.0, retries=0, max_bytes=4 * 1024 * 1024,
                                    allow_redirects=False)
            if not is_loopback_url(resp.url):
                raise NetError("Ollama redirected away from this computer")
            data = json.loads(resp.body.decode("utf-8", errors="replace"))
            models = [str(m.get("name") or m.get("model")) for m in (data.get("models") or [])
                      if isinstance(m, dict) and (m.get("name") or m.get("model"))]
            result.update(ok=True, models=models[:50])
        except (NetError, ValueError, AttributeError) as err:
            result["error"] = str(err)[:200]
        with self._lock:
            self._cache[base] = (now + (20.0 if result["ok"] else 60.0), dict(result))   # "Check again" bypasses this
        return result


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


class OllamaProvider:
    name = "ollama"
    label = "Ollama"

    def __init__(self, net, *, base_url: str, model: str, log=None, clock: Callable[[], float] = time.monotonic):
        self.net = net
        self.base = base_url.rstrip("/")
        self.model = model
        self.log = log
        self.clock = clock
        self.result_limit = 8_000    # local models have small context windows

    def start(self, system: str, tools: list[dict], history: list[dict], context: str, question: str) -> dict:
        messages = [{"role": "system", "content": system}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": f"{context}\n\n{question}"})
        specs = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                   "parameters": t["input_schema"]}} for t in tools]
        return {"messages": messages, "tools": specs}

    def add_assistant(self, state: dict, turn: Turn) -> None:
        state["messages"].append(turn.assistant)

    def add_results(self, state: dict, results: list[tuple[ToolCall, str, bool]], note: str | None = None) -> None:
        for call, text, _is_error in results:
            state["messages"].append({"role": "tool", "tool_name": call.name, "content": text})
        if note:
            state["messages"].append({"role": "user", "content": note})

    def complete(self, state: dict, *, final: bool, deadline: float) -> Turn:
        body: dict[str, Any] = {"model": self.model, "messages": state["messages"], "stream": False,
                                "keep_alive": "15m", "options": {"num_ctx": OLLAMA_NUM_CTX}}
        if state["tools"] and not final:
            body["tools"] = state["tools"]
        remaining = deadline - self.clock()
        if remaining < 3.0:
            raise ProviderError("TIMEOUT", "The answer ran out of time waiting for Ollama.")
        try:
            resp = self.net.request(self.base + "/api/chat", method="POST", data=dumps(body).encode("utf-8"),
                                    headers={"Content-Type": "application/json"}, retries=0,
                                    timeout=min(OLLAMA_TIMEOUT, remaining), accept=(200, 400, 404, 500, 503),
                                    max_bytes=16 * 1024 * 1024, allow_redirects=False)
        except NetError as err:
            if "timed out" in str(err).lower():
                raise ProviderError("TIMEOUT", f"The local model ({self.model}) took too long to answer.") from None
            raise ProviderError("PROVIDER_UNREACHABLE", f"Ollama isn't answering at {self.base}. Is it running?") from None
        if not is_loopback_url(resp.url):
            raise ProviderError("PROVIDER_ERROR", "Ollama redirected the request away from this computer, so it was dropped.")
        try:
            data = json.loads(resp.body.decode("utf-8", errors="replace"))
        except ValueError:
            data = {}
        if resp.status == 404:
            raise ProviderError("PROVIDER_MODEL", f'Ollama doesn\'t have the model "{self.model}". Install it with: '
                                                  f"ollama pull {self.model}", status=404)
        if resp.status != 200 or not isinstance(data, dict):
            detail = str(data.get("error") if isinstance(data, dict) else "")[:200] or f"HTTP {resp.status}"
            raise ProviderError("PROVIDER_ERROR", f"Ollama couldn't answer: {detail}", status=resp.status)
        msg = data.get("message") if isinstance(data.get("message"), dict) else {}
        raw_calls = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
        turn = Turn(model=data.get("model") or self.model,
                    usage={"input_tokens": int(data.get("prompt_eval_count") or 0),
                           "output_tokens": int(data.get("eval_count") or 0)})
        for i, tc in enumerate(raw_calls):
            fn = tc.get("function") if isinstance(tc, dict) and isinstance(tc.get("function"), dict) else {}
            args = fn.get("arguments")
            bad = False
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    bad = True
            if args is None:
                args = {}
            turn.calls.append(ToolCall(id=str(tc.get("id") or f"call_{len(state['messages'])}_{i}"),
                                       name=str(fn.get("name") or ""), input=args, bad_json=bad))
        turn.text = _THINK_RE.sub("", str(msg.get("content") or "")).strip()
        if turn.calls and not final:
            turn.stop = "tool_use"
        else:
            turn.calls = []
            turn.stop = "max_tokens" if data.get("done_reason") == "length" else "end"
        assistant: dict[str, Any] = {"role": "assistant", "content": str(msg.get("content") or "")}
        if raw_calls:
            assistant["tool_calls"] = raw_calls
        turn.assistant = assistant
        return turn
