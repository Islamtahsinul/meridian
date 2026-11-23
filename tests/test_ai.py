"""Tests for the ai module (the Ask assistant). No network: a fake HTTP layer stands in for the
Anthropic API, Ollama and Nominatim.

Run: python -m unittest tests.test_ai -v     (also runs tests/test_ai_markdown.mjs when node is installed)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from meridian.config import Config
from meridian.core_api import register_core
from meridian.datasets import DatasetManager
from meridian.httpserver import make_server
from meridian.modules import load_all, sanctions
from meridian.modules.ai import engine as ai_engine
from meridian.modules.ai import providers as ai_providers
from meridian.modules.ai.engine import Assistant
from meridian.modules.ai.prompt import SYSTEM_PROMPT, TIME_NOTE, view_context
from meridian.modules.ai.providers import (AnthropicProvider, ProviderError, check_ollama_url, echo_content,
                                           pick_ollama_model)
from meridian.modules.ai.tools import Actions, ToolInputError, run_action
from meridian.modules.ai.util import (fit_json, mentioned_entities, parse_messages, parse_view)
from meridian.net import NetError, NetResponse
from meridian.registry import App, HTTPError
from meridian.scheduler import Scheduler
from meridian.store import Store

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "ai"
KEY = "test-only-fake-anthropic-key-do-not-leak-0123456789"
logging.getLogger("meridian").addHandler(logging.NullHandler())   # keep test output quiet
MASK = {"ANTHROPIC_API_KEY": "", "AI_PROVIDER": "", "ANTHROPIC_MODEL": "", "ANTHROPIC_EFFORT": "",
        "ANTHROPIC_FALLBACK": "", "OLLAMA_URL": "", "OLLAMA_MODEL": ""}


def fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


class FakeNet:
    """Scripted stand-in for meridian.net.Net: records every request, never touches the network."""

    def __init__(self, clock: FakeClock | None = None):
        self.clock = clock
        self.calls: list[dict] = []
        self.anthropic: list = []           # (status, body, headers, latency) or an exception
        self.ollama: list = []
        self.tags: object = NetError("127.0.0.1: [WinError 10061] connection refused")
        self.chat_url_override: str | None = None
        self.gate: threading.Event | None = None
        self.stats = {"requests": 0, "errors": 0, "cache_hits": 0, "bytes": 0}
        self.lock = threading.Lock()

    def set_host_interval(self, host, seconds):
        pass

    def _resp(self, url, status, body, headers=None, accept=(200,)):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        if status not in accept:
            raise NetError(f"{urllib.parse.urlsplit(url).hostname}: HTTP {status}", status=status, url=url, body=raw)
        return NetResponse(status, {k.lower(): v for k, v in (headers or {}).items()}, raw, url)

    def request(self, url, *, method="GET", params=None, headers=None, data=None, json_body=None, timeout=20.0,
                retries=1, ttl=0.0, min_interval=None, max_bytes=0, accept=(200,), cache_key=None,
                allow_redirects=True):
        body = None
        if data is not None:
            body = json.loads(data.decode("utf-8"))
        elif json_body is not None:
            body = json_body
        with self.lock:
            self.calls.append({"url": url, "method": method, "params": params, "headers": dict(headers or {}),
                               "body": body, "timeout": timeout, "retries": retries,
                               "allow_redirects": allow_redirects})
        host = urllib.parse.urlsplit(url).hostname
        if host == "api.anthropic.com":
            if self.gate is not None:
                self.gate.wait(10)
            if not self.anthropic:
                raise AssertionError("unexpected Anthropic request")
            item = self.anthropic.pop(0)
            if isinstance(item, Exception):
                raise item
            status, payload, hdrs, latency = (list(item) + [None, None, 0.0])[:4]
            if self.clock is not None and latency:
                self.clock.t += latency
            return self._resp(url, status, payload, hdrs, accept)
        if host == "nominatim.openstreetmap.org":
            name = "nominatim_reverse.json" if url.endswith("/reverse") else "nominatim_search.json"
            return self._resp(url, 200, fixture(name), None, accept)
        if host in ("127.0.0.1", "::1"):
            if url.endswith("/api/tags"):
                if isinstance(self.tags, Exception):
                    raise self.tags
                return self._resp(url, 200, self.tags, None, accept)
            if url.endswith("/api/chat"):
                if not self.ollama:
                    raise AssertionError("unexpected Ollama request")
                status, payload = self.ollama.pop(0)
                resp = self._resp(url, status, payload, None, accept)
                if self.chat_url_override:
                    resp.url = self.chat_url_override
                return resp
        raise AssertionError(f"unexpected request to {url}")

    def get_json(self, url, **kw):
        return self.request(url, **kw).json()

    def anthropic_bodies(self) -> list[dict]:
        return [c["body"] for c in self.calls if "api.anthropic.com" in c["url"]]

    def anthropic_headers(self) -> list[dict]:
        return [c["headers"] for c in self.calls if "api.anthropic.com" in c["url"]]


def msg(content, stop="end_turn", model="claude-opus-5-5", usage=None, **extra):
    out = {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": content,
           "stop_reason": stop, "usage": usage or {"input_tokens": 100, "output_tokens": 20}}
    out.update(extra)
    return out


def text(t):
    return {"type": "text", "text": t}


def use(tid, name, args):
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


def think(t="", sig="sig-abc"):
    return {"type": "thinking", "thinking": t, "signature": sig}


VIEW = {"bounds": [-1.2, 51.2, 0.3, 51.8], "zoom": 9.5, "center": {"lat": 51.5, "lon": -0.45},
        "projection": "mercator", "basemap": "dark", "terrain": False, "layers_on": ["flights"],
        "layers": [{"id": "flights", "title": "Aircraft", "group": "Flights", "on": True, "count": 120},
                   {"id": "cameras", "title": "Traffic cameras", "group": "Cameras", "on": False},
                   {"id": "ships", "title": "Ships", "group": "Ships", "on": False}]}
QUESTION = [{"role": "user", "content": "Which cameras are near Heathrow?"}]


def make_app(overrides: dict | None = None, clock: FakeClock | None = None):
    tmp = tempfile.mkdtemp(prefix="meridian-ai-test-")
    os.environ["MERIDIAN_DATA_DIR"] = tmp
    cfg = Config(None, overrides={**MASK, **(overrides or {})})
    log = logging.getLogger("meridian.test.ai")
    net = FakeNet(clock)
    app = App(cfg, Store(Path(tmp) / "t.db"), net, log, Scheduler(log), DatasetManager(log))
    register_core(app)
    load_all(app, {"ai"})
    assert app.modules["ai"].ok, app.modules["ai"].error
    return app, net, tmp


def reset_provider_state():
    with ai_providers._lock:
        ai_providers._disabled.clear()
        ai_providers.BAD_TOOLS.clear()


class Base(unittest.TestCase):
    overrides: dict = {"ANTHROPIC_API_KEY": KEY}

    def setUp(self):
        reset_provider_state()
        self.clock = FakeClock()
        self.app, self.net, self.tmp = make_app(self.overrides, self.clock)
        self.m = self.app.contexts["ai"]
        self.ai = Assistant(self.m, clock=self.clock, wall=lambda: 1795380000.0, sleep=self.clock.sleep)
        sanctions._load(FIX / "sdn.json")

    def tearDown(self):
        self.app.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_chat(self, messages=None, view=None, **kw):
        return self.ai.run(parse_messages(messages or QUESTION), parse_view(view if view is not None else VIEW), **kw)


# ---------------------------------------------------------------------------------------------
# Provider choice and status
# ---------------------------------------------------------------------------------------------
class ProviderChoiceTests(Base):
    overrides = {}

    def test_off_without_key_or_ollama(self):
        status = self.ai.status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["provider"], "off")
        self.assertEqual(status["code"], "NOT_CONFIGURED")
        self.assertIn("Add ANTHROPIC_API_KEY to the .env file and restart Meridian, or start Ollama.", status["setup"])
        result = self.run_chat()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_CONFIGURED")
        self.assertEqual(result["reply"], "Add ANTHROPIC_API_KEY to the .env file and restart Meridian, or start Ollama.")
        self.assertEqual(self.net.anthropic_bodies(), [])

    def test_auto_prefers_anthropic_when_key_set(self):
        self.app.config._overrides["ANTHROPIC_API_KEY"] = KEY
        provider, _ = self.ai.resolve()
        self.assertEqual(provider.name, "anthropic")
        self.assertEqual(provider.model, "claude-opus-5-5")
        self.assertNotIn(KEY, repr(provider))

    def test_explicit_off_and_anthropic_without_key(self):
        self.app.config._overrides["AI_PROVIDER"] = "off"
        provider, info = self.ai.resolve()
        self.assertIsNone(provider)
        self.assertEqual(info["code"], "DISABLED")
        self.app.config._overrides["AI_PROVIDER"] = "anthropic"
        provider, info = self.ai.resolve()
        self.assertIsNone(provider)
        self.assertIn("ANTHROPIC_API_KEY", info["message"])

    def test_auto_uses_running_ollama_and_picks_a_tool_model(self):
        self.net.tags = {"models": [{"name": "nomic-embed-text:latest"}, {"name": "llama3.1:8b"}, {"name": "qwen3:8b"}]}
        provider, info = self.ai.resolve(fresh=True)
        self.assertEqual(provider.name, "ollama")
        self.assertEqual(provider.model, "qwen3:8b")
        self.assertTrue(info["ollama"]["reachable"])

    def test_non_loopback_ollama_url_is_refused_without_a_request(self):
        self.app.config._overrides.update(AI_PROVIDER="ollama", OLLAMA_URL="http://10.0.0.20:11434")
        provider, info = self.ai.resolve()
        self.assertIsNone(provider)
        self.assertEqual(info["code"], "OLLAMA_URL_REFUSED")
        self.assertFalse([c for c in self.net.calls if "10.0.0.20" in c["url"]])

    def test_status_has_tools_and_no_secret(self):
        self.app.config._overrides["ANTHROPIC_API_KEY"] = KEY
        status = self.ai.status()
        self.assertTrue(status["ready"])
        self.assertIn("search_entities", status["tools"]["core"])
        self.assertIn("sanctions_lookup", [t["name"] for t in status["tools"]["modules"]])
        self.assertNotIn(KEY, json.dumps(status))
        self.assertNotIn(KEY, json.dumps(self.app.config.key_status()))


class OllamaUrlTests(unittest.TestCase):
    def test_loopback_only(self):
        ok = {"http://127.0.0.1:11434": "http://127.0.0.1:11434", "http://localhost:11434/": "http://127.0.0.1:11434",
              "http://[::1]:11434": "http://[::1]:11434", "http://127.0.0.2:8080": "http://127.0.0.2:8080",
              "": "http://127.0.0.1:11434", "https://127.0.0.1:11434/ollama": "https://127.0.0.1:11434/ollama"}
        for raw, want in ok.items():
            self.assertEqual(check_ollama_url(raw), (want, None), raw)
        for bad in ("http://10.20.30.5:11434", "http://example.com:11434", "http://0.0.0.0:11434",
                    "file:///etc/passwd", "http://user:pw@127.0.0.1:11434", "http://127.0.0.1:11434@evil.example",
                    "http://localhost.evil.example", "http://127.0.0.1.nip.io", "http://2130706433", "http://0x7f000001",
                    "http://[::ffff:8.8.8.8]", "gopher://127.0.0.1", "http://127.0.0.1:99999", "http://127.0.0.1/?x=1",
                    "http://10.0.0.1"):
            base, why = check_ollama_url(bad)
            self.assertIsNone(base, bad)
            self.assertTrue(why)

    def test_pick_model(self):
        self.assertEqual(pick_ollama_model(["nomic-embed-text", "mistral:7b", "llama3.2:3b"], None), "llama3.2:3b")
        self.assertEqual(pick_ollama_model(["phi3"], None), "phi3")
        self.assertEqual(pick_ollama_model([], "qwen3:8b"), "qwen3:8b")
        self.assertIsNone(pick_ollama_model([], None))


# ---------------------------------------------------------------------------------------------
# The Claude tool loop
# ---------------------------------------------------------------------------------------------
class AnthropicLoopTests(Base):
    def test_tool_loop_collects_actions_and_echoes_history(self):
        self.net.anthropic = [
            (200, fixture("anthropic_tool_use.json")),
            (200, msg([think("", "sig-2"), use("toolu_02", "sanctions_lookup", {"imo": "9187629"})], "tool_use")),
            (200, fixture("anthropic_final.json")),
        ]
        result = self.run_chat()
        self.assertTrue(result["ok"], result)
        bodies = self.net.anthropic_bodies()
        self.assertEqual(len(bodies), 3)
        # Headers: raw HTTP to the Messages API with the current version and the fallback beta.
        hdrs = self.net.anthropic_headers()[0]
        self.assertEqual(hdrs["x-api-key"], KEY)
        self.assertEqual(hdrs["anthropic-version"], "2026-10-21")
        self.assertIn("server-side-fallback-2026-07-01", hdrs["anthropic-beta"])
        first = bodies[0]
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["thinking"]["type"], "adaptive")
        self.assertEqual(first["output_config"], {"effort": "medium"})
        self.assertEqual(first["fallbacks"], "default")
        self.assertNotIn("tool_choice", first)
        self.assertNotIn("temperature", first)
        self.assertEqual(first["system"][0]["text"], SYSTEM_PROMPT)
        self.assertEqual(first["system"][0]["cache_control"], {"type": "ephemeral"})
        names = [t["name"] for t in first["tools"]]
        self.assertIn("sanctions_lookup", names)
        self.assertIn("fly_to", names)
        # The question carries the map view as its own block.
        q = first["messages"][-1]
        self.assertEqual(q["role"], "user")
        self.assertTrue(q["content"][0]["text"].startswith("<map_view>"))
        self.assertEqual(q["content"][1]["text"], "Which cameras are near Heathrow?")
        # Append-only history: system and tools identical, assistant content echoed unchanged.
        for b in bodies[1:]:
            self.assertEqual(b["system"], first["system"])
            self.assertEqual(b["tools"], first["tools"])
            self.assertEqual(b["messages"][: len(first["messages"])], first["messages"])
        second = bodies[1]["messages"]
        self.assertEqual(second[-2], {"role": "assistant", "content": fixture("anthropic_tool_use.json")["content"]})
        results = second[-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["toolu_01A", "toolu_01B", "toolu_01C"])
        self.assertTrue(all(r["type"] == "tool_result" and not r.get("is_error") for r in results))
        search = json.loads(results[0]["content"])
        self.assertEqual(search["query"], "Heathrow")
        sanc = json.loads(bodies[2]["messages"][-1]["content"][0]["content"])
        self.assertEqual(sanc["vessel"]["name"], "OCEAN TEST")
        # The result for the browser.
        self.assertEqual(result["actions"], [{"type": "fly_to", "lat": 51.47, "lon": -0.4543, "zoom": 12.0},
                                             {"type": "set_layers", "on": ["cameras"], "off": []}])
        self.assertEqual([t["tool"] for t in result["trace"]], ["search_entities", "fly_to", "set_layers", "sanctions_lookup"])
        self.assertTrue(all(t["ok"] for t in result["trace"]))
        self.assertTrue(all(isinstance(t["ms"], int) for t in result["trace"]))
        self.assertEqual(result["trace"][0]["note"], "Looking up Heathrow and the cameras near it.")
        self.assertEqual(result["trace"][3]["module"], "sanctions")
        self.assertIn("London Heathrow Airport", result["reply"])
        self.assertEqual(result["provider"], "anthropic")
        self.assertEqual(result["model"], "claude-opus-5-5")
        self.assertEqual(result["usage"]["input_tokens"], 3210 + 100 + 410)
        self.assertEqual(result["usage"]["cache_read_input_tokens"], 3100)
        self.assertEqual(result["rounds"], 2)
        self.assertIsNone(result["stopped"])
        self.assertNotIn(KEY, json.dumps(result))

    def test_bad_action_arguments_go_back_as_errors(self):
        self.net.anthropic = [
            (200, msg([use("t1", "fly_to", {"lat": 200, "lon": 0}), use("t2", "set_layers", {"on": ["nope"]}),
                       use("t3", "set_basemap", {"id": "neon"}), use("t4", "no_such_tool", {})], "tool_use")),
            (200, msg([text("Sorry.")])),
        ]
        result = self.run_chat()
        results = self.net.anthropic_bodies()[1]["messages"][-1]["content"]
        self.assertTrue(all(r.get("is_error") for r in results))
        self.assertIn("Known layer ids", results[1]["content"])
        self.assertEqual(result["actions"], [])
        self.assertEqual([t["ok"] for t in result["trace"]], [False, False, False, False])

    def test_tool_exceptions_and_timeouts_are_contained(self):
        def boom(args):
            raise RuntimeError(f"database exploded {KEY}")
        self.app.add_ai_tool("test", "broken_tool", "Always fails.", {"type": "object", "properties": {}}, boom)
        self.net.anthropic = [(200, msg([use("t1", "broken_tool", {})], "tool_use")),
                              (200, msg([text("The tool failed, so I can't say.")]))]
        result = self.run_chat()
        self.assertTrue(result["ok"])
        sent = self.net.anthropic_bodies()[1]["messages"][-1]["content"][0]
        self.assertTrue(sent["is_error"])
        self.assertIn("RuntimeError", sent["content"])
        self.assertNotIn(KEY, sent["content"])
        self.assertNotIn(KEY, json.dumps(result))
        self.assertFalse(result["trace"][0]["ok"])

    def test_large_results_are_truncated_with_a_note(self):
        rows = [{"kind": "ship", "id": str(i), "title": f"Vessel {i}", "lat": 26.0, "lon": 56.0, "note": "x" * 80}
                for i in range(4000)]
        self.app.add_ai_tool("test", "big_tool", "Returns a lot.", {"type": "object", "properties": {}},
                             lambda a: {"total": len(rows), "results": rows})
        self.net.anthropic = [(200, msg([use("t1", "big_tool", {})], "tool_use")), (200, msg([text("Many ships.")]))]
        result = self.run_chat()
        content = self.net.anthropic_bodies()[1]["messages"][-1]["content"][0]["content"]
        self.assertLessEqual(len(content), 12_000)
        parsed = json.loads(content)
        self.assertIn("truncated", parsed)
        self.assertIn("more items not shown", json.dumps(parsed))
        self.assertTrue(result["trace"][0]["truncated"])

    def test_max_rounds_forces_a_final_answer(self):
        self.net.anthropic = [(200, msg([use(f"t{i}", "list_layers", {})], "tool_use")) for i in range(8)]
        self.net.anthropic.append((200, msg([text("Here is what I found.")])))
        result = self.run_chat()
        bodies = self.net.anthropic_bodies()
        self.assertEqual(len(bodies), 9)
        self.assertEqual(bodies[-1]["tool_choice"], {"type": "none"})
        self.assertNotIn("tool_choice", bodies[-2])
        self.assertEqual(result["rounds"], 8)
        self.assertEqual(result["stopped"], "max_rounds")
        self.assertEqual(result["reply"], "Here is what I found.")

    def test_time_budget_forces_the_answer_and_bounds_timeouts(self):
        self.net.anthropic = [(200, msg([use(f"t{i}", "list_layers", {})], "tool_use"), None, 20.0) for i in range(4)]
        self.net.anthropic.append((200, msg([text("Quick answer.")]), None, 5.0))
        result = self.run_chat()
        bodies = self.net.anthropic_bodies()
        self.assertEqual(len(bodies), 5)
        self.assertEqual(bodies[-1]["tool_choice"], {"type": "none"})
        self.assertEqual(bodies[-1]["messages"][-1]["content"][-1], {"type": "text", "text": TIME_NOTE})
        self.assertEqual(result["stopped"], "time_budget")
        self.assertEqual(result["reply"], "Quick answer.")
        timeouts = [c["timeout"] for c in self.net.calls if "anthropic" in c["url"]]
        self.assertTrue(all(t <= 60 for t in timeouts))
        self.assertAlmostEqual(timeouts[-1], 10.0)
        self.assertTrue(all(c["retries"] == 0 for c in self.net.calls if "anthropic" in c["url"]))

    def test_refusal_drops_actions(self):
        self.net.anthropic = [
            (200, msg([use("t1", "fly_to", {"lat": 10, "lon": 10})], "tool_use")),
            (200, msg([], "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None})),
        ]
        result = self.run_chat()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped"], "refusal")
        self.assertIn("declined", result["reply"])
        self.assertIn("cyber", result["reply"])
        self.assertEqual(result["actions"], [])

    def test_tools_in_a_cut_off_turn_are_not_run(self):
        self.net.anthropic = [(200, msg([text("Partial answer"), use("t1", "fly_to", {"lat": 1, "lon": 2})], "max_tokens"))]
        result = self.run_chat()
        self.assertEqual(len(self.net.anthropic_bodies()), 1)
        self.assertEqual(result["trace"], [])
        self.assertEqual(result["actions"], [])
        self.assertIn("cut short", result["reply"])
        self.assertEqual(result["stopped"], "max_tokens")

    def test_pause_turn_is_resumed(self):
        paused = [text("Working on it")]
        self.net.anthropic = [(200, msg(paused, "pause_turn")), (200, msg([text("Done.")]))]
        result = self.run_chat()
        bodies = self.net.anthropic_bodies()
        self.assertEqual(bodies[1]["messages"][-1], {"role": "assistant", "content": paused})
        self.assertEqual(result["reply"], "Done.")

    def test_refusal_fallback_block_is_echoed_in_place(self):
        content = [{"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"}},
                   use("t1", "list_layers", {})]
        self.net.anthropic = [(200, msg(content, "tool_use", model="claude-opus-4-8")),
                              (200, msg([text("Answer.")], model="claude-opus-4-8"))]
        result = self.run_chat()
        self.assertEqual(self.net.anthropic_bodies()[1]["messages"][-2]["content"], content)
        self.assertEqual(result["fallback"], {"from": "claude-opus-5-5", "to": "claude-opus-4-8"})
        self.assertEqual(result["model"], "claude-opus-4-8")

    def test_echo_content_drops_blocks_before_the_fallback_boundary(self):
        fb = {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}}
        content = [think("x"), text("kept"), use("t0", "fly_to", {}), fb, think("y"), use("t1", "x", {})]
        self.assertEqual(echo_content(content), [text("kept"), fb, think("y"), use("t1", "x", {})])
        self.assertEqual(echo_content([text("a")]), [text("a")])

    def test_history_is_text_only_and_merged(self):
        history = [{"role": "assistant", "content": "Hello"},   # leading assistant turn is dropped
                   {"role": "user", "content": "Where is the Strait of Hormuz?"},
                   {"role": "assistant", "content": "Between Iran and Oman."},
                   {"role": "user", "content": "Show ships there"}, {"role": "user", "content": "please"}]
        self.net.anthropic = [(200, msg([text("OK.")]))]
        self.run_chat(history)
        sent = self.net.anthropic_bodies()[0]["messages"]
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "user"])
        self.assertEqual(sent[0]["content"], "Where is the Strait of Hormuz?")
        self.assertEqual(sent[2]["content"][1]["text"], "Show ships there\n\nplease")

    def test_cheaper_model_and_effort_settings(self):
        self.app.config._overrides.update(ANTHROPIC_MODEL="claude-haiku-4-5", ANTHROPIC_EFFORT="low",
                                          ANTHROPIC_FALLBACK="off")
        self.net.anthropic = [(200, msg([text("Hi.")], model="claude-haiku-4-5"))]
        self.run_chat()
        body = self.net.anthropic_bodies()[0]
        self.assertEqual(body["model"], "claude-haiku-4-5")
        self.assertNotIn("thinking", body)          # Haiku 4.5 has no adaptive thinking
        self.assertNotIn("output_config", body)
        self.assertNotIn("fallbacks", body)
        self.assertNotIn("anthropic-beta", self.net.anthropic_headers()[0])
        self.app.config._overrides.update(ANTHROPIC_MODEL="claude-sonnet-5-5", ANTHROPIC_FALLBACK="on")
        self.net.anthropic = [(200, msg([text("Hi.")], model="claude-sonnet-5-5"))]
        self.run_chat()
        body = self.net.anthropic_bodies()[-1]
        self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertEqual(body["thinking"], {"type": "adaptive", "display": "updates"})
        self.assertEqual(body["fallbacks"], "default")

    def test_entity_chips_follow_the_reply(self):
        self.app.add_search("test", lambda q, limit: [
            {"kind": "airport", "id": "EGLL", "title": "London Heathrow Airport", "lat": 51.47, "lon": -0.4543,
             "layer": "airports", "score": 0.9},
            {"kind": "camera", "id": "tfl:1", "title": "A4 Bath Road", "lat": 51.48, "lon": -0.44, "layer": "cameras"},
            {"kind": "camera", "id": "tfl:2", "title": "M25 J14", "lat": 51.46, "lon": -0.51, "layer": "cameras"}])
        self.net.anthropic = [(200, fixture("anthropic_tool_use.json")), (200, fixture("anthropic_final.json"))]
        self.net.anthropic.insert(1, (200, msg([use("t9", "list_layers", {})], "tool_use")))
        result = self.run_chat()
        self.assertEqual([e["id"] for e in result["entities"]], ["EGLL", "tfl:1"])


class ProviderErrorTests(Base):
    def test_bad_key_is_explained_and_never_echoed(self):
        self.net.anthropic = [(401, {"type": "error", "error": {"type": "authentication_error",
                                                                "message": f"invalid x-api-key {KEY}"}})]
        with self.assertLogs("meridian", level="WARNING") as logs:
            result = self.run_chat()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PROVIDER_AUTH")
        self.assertIn("ANTHROPIC_API_KEY", result["error"]["message"])
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn(KEY, "\n".join(logs.output))

    def test_network_failures(self):
        self.net.anthropic = [NetError("api.anthropic.com: timed out")]
        self.assertEqual(self.run_chat()["error"]["code"], "TIMEOUT")
        self.net.anthropic = [NetError("api.anthropic.com: [Errno 11001] getaddrinfo failed")]
        result = self.run_chat()
        self.assertEqual(result["error"]["code"], "PROVIDER_UNREACHABLE")
        self.assertIn("api.anthropic.com", result["error"]["message"])

    def test_rate_limit_is_retried_once_after_retry_after(self):
        self.net.anthropic = [(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
                               {"retry-after": "3"}),
                              (200, msg([text("Hi.")]))]
        result = self.run_chat()
        self.assertTrue(result["ok"])
        self.assertEqual(self.clock.slept, [3.0])

    def test_overloaded_twice_gives_up(self):
        err = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        self.net.anthropic = [(529, err), (529, err)]
        result = self.run_chat()
        self.assertEqual(result["error"]["code"], "PROVIDER_UNAVAILABLE")
        self.assertEqual(len(self.net.anthropic_bodies()), 2)

    def test_long_retry_after_is_not_waited_for(self):
        self.net.anthropic = [(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "x"}},
                               {"retry-after": "40"})]
        result = self.run_chat()
        self.assertEqual(result["error"]["code"], "PROVIDER_RATE_LIMIT")
        self.assertIn("40 s", result["error"]["message"])
        self.assertEqual(self.clock.slept, [])

    def test_rejected_beta_is_dropped_and_retried(self):
        self.net.anthropic = [(400, {"type": "error", "error": {"type": "invalid_request_error", "message":
                                     "Unexpected value(s) `server-side-fallback-2026-07-01` for the `anthropic-beta` header."}}),
                              (200, msg([text("Hi.")]))]
        result = self.run_chat()
        self.assertTrue(result["ok"])
        retry = self.net.anthropic_bodies()[1]
        self.assertNotIn("fallbacks", retry)
        self.assertNotIn("server-side-fallback", self.net.anthropic_headers()[1].get("anthropic-beta", ""))
        self.assertIn("thinking-display-updates", self.net.anthropic_headers()[1].get("anthropic-beta", ""))

    def test_a_rejected_module_tool_is_left_out(self):
        self.app.add_ai_tool("test", "zz_weird_tool", "Odd schema.", {"type": "object", "properties": {"x": {"type": "nonsense"}}},
                             lambda a: {})
        names = [s["name"] for s in self.ai.toolset()[0]]
        bad_index = names.index("zz_weird_tool")
        self.net.anthropic = [(400, {"type": "error", "error": {"type": "invalid_request_error",
                                     "message": f"tools.{bad_index}.custom.input_schema: JSON schema is invalid"}}),
                              (200, msg([text("Hi.")]))]
        result = self.run_chat()
        self.assertTrue(result["ok"])
        self.assertNotIn("zz_weird_tool", [t["name"] for t in self.net.anthropic_bodies()[1]["tools"]])
        self.assertNotIn("zz_weird_tool", [s["name"] for s in self.ai.toolset()[0]])
        rejected = [t for t in self.ai.status()["tools"]["modules"] if t["name"] == "zz_weird_tool"]
        self.assertIn("rejected", rejected[0])

    def test_unusable_module_tool_schemas_are_skipped(self):
        self.app.add_ai_tool("test", "no_schema_tool", "x", {"properties": {}}, lambda a: {})
        self.assertNotIn("no_schema_tool", [s["name"] for s in self.ai.toolset()[0]])


# ---------------------------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------------------------
class OllamaTests(Base):
    overrides = {"AI_PROVIDER": "ollama"}

    def setUp(self):
        super().setUp()
        self.net.tags = {"models": [{"name": "qwen3:8b"}]}

    def test_tool_loop(self):
        self.net.ollama = [(200, fixture("ollama_chat_tool.json")), (200, fixture("ollama_chat_final.json"))]
        result = self.run_chat()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(result["model"], "qwen3:8b")
        self.assertEqual(result["reply"], "London Heathrow Airport is west of London.")
        chats = [c for c in self.net.calls if c["url"].endswith("/api/chat")]
        self.assertEqual(chats[0]["url"], "http://127.0.0.1:11434/api/chat")
        first = chats[0]["body"]
        self.assertEqual(first["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})
        self.assertTrue(first["messages"][-1]["content"].startswith("<map_view>"))
        self.assertEqual(first["tools"][0]["type"], "function")
        self.assertFalse(first["stream"])
        second = chats[1]["body"]["messages"]
        self.assertEqual(second[-2]["tool_calls"][0]["function"]["name"], "search_entities")
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_name"], "search_entities")
        self.assertEqual(result["usage"]["input_tokens"], 3200)
        # Ollama requests never follow a redirect (checking the final URL afterwards is too late)
        local = [c for c in self.net.calls if c["url"].startswith("http://127.0.0.1:11434/")]
        self.assertTrue(local)
        self.assertTrue(all(c["allow_redirects"] is False for c in local), local)

    def test_string_arguments_and_bad_json(self):
        call = {"model": "qwen3:8b", "message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "fly_to", "arguments": "{\"lat\": 26.5, \"lon\": 56.3, \"zoom\": 7}"}},
            {"function": {"name": "set_layers", "arguments": "{not json"}}]}, "done_reason": "stop"}
        self.net.ollama = [(200, call), (200, {"model": "qwen3:8b", "message": {"role": "assistant", "content": "There."}})]
        result = self.run_chat()
        self.assertEqual(result["actions"], [{"type": "fly_to", "lat": 26.5, "lon": 56.3, "zoom": 7.0}])
        self.assertEqual([t["ok"] for t in result["trace"]], [True, False])

    def test_missing_model_says_how_to_install_it(self):
        self.net.ollama = [(404, {"error": "model 'qwen3:8b' not found"})]
        result = self.run_chat()
        self.assertEqual(result["error"]["code"], "PROVIDER_MODEL")
        self.assertIn("ollama pull qwen3:8b", result["error"]["message"])

    def test_redirect_away_from_loopback_is_dropped(self):
        self.net.chat_url_override = "http://evil.example/api/chat"
        self.net.ollama = [(200, fixture("ollama_chat_final.json"))]
        result = self.run_chat()
        self.assertFalse(result["ok"])
        self.assertIn("redirected", result["error"]["message"])

    def test_unreachable(self):
        self.net.tags = NetError("127.0.0.1: connection refused")
        provider, info = self.ai.resolve(fresh=True)
        self.assertIsNone(provider)
        self.assertEqual(info["code"], "OLLAMA_UNREACHABLE")


# ---------------------------------------------------------------------------------------------
# The data tools and map actions
# ---------------------------------------------------------------------------------------------
class ToolTests(Base):
    def call(self, name, args, view=None):
        self.net.anthropic = [(200, msg([use("t1", name, args)], "tool_use")), (200, msg([text("ok")]))]
        result = self.run_chat(view=view)
        sent = self.net.anthropic_bodies()[-1]["messages"][-1]["content"][0]
        return json.loads(sent["content"]), sent.get("is_error", False), result

    def test_search_entities(self):
        self.app.add_search("test", lambda q, limit: [
            {"kind": "aircraft", "id": "4ca2b3", "title": "RYR8NE", "lat": 51.47, "lon": -0.45, "layer": "flights", "score": 0.9},
            {"kind": "ship", "id": "235000001", "title": "RYR BOAT", "lat": 50.0, "lon": -1.0, "layer": "ships", "score": 0.5}])
        data, err, _ = self.call("search_entities", {"query": "RYR", "kinds": ["aircraft"]})
        self.assertFalse(err)
        self.assertEqual([r["id"] for r in data["results"]], ["4ca2b3"])
        data, err, _ = self.call("search_entities", {"query": "x"})
        self.assertTrue(err)

    def test_whats_here_uses_nearby_and_nominatim(self):
        self.app.add_nearby("test", lambda lat, lon, r, limit: [
            {"kind": "camera", "id": "tfl:1", "title": "A4 Bath Road", "lat": 51.48, "lon": -0.44, "layer": "cameras"},
            {"kind": "camera", "id": "far:1", "title": "Far away", "lat": 55.0, "lon": -3.0, "layer": "cameras"}])
        data, err, _ = self.call("whats_here", {"lat": 51.47, "lon": -0.4543, "radius_km": 10})
        self.assertFalse(err)
        self.assertIn("Heathrow", data["place"]["name"])
        self.assertEqual(data["counts"], {"camera": 1})
        self.assertEqual(data["nearby"][0]["title"], "A4 Bath Road")
        reverse = [c for c in self.net.calls if c["url"].endswith("/reverse")][0]
        self.assertEqual(reverse["params"]["format"], "jsonv2")
        self.assertEqual(reverse["headers"], {"Accept-Language": "en"})

    def test_recent_events_filters(self):
        now = 1795380000.0
        events = [
            {"kind": "earthquake", "id": "us1", "title": "M6.1 Japan", "lat": 36.0, "lon": 140.0, "time": now - 3600, "severity": 7},
            {"kind": "earthquake", "id": "us2", "title": "M4.6 Fiji", "lat": -17.8, "lon": 179.5, "time": now - 600, "severity": 4},
            {"kind": "fire", "id": "f1", "title": "Fire cluster", "lat": -17.5, "lon": -179.8, "time": now - 60, "severity": 5},
            {"kind": "earthquake", "id": "old", "title": "Old", "lat": 0, "lon": 0, "time": now - 90000, "severity": 9}]
        self.app.add_events("test", lambda since: [e for e in events if e["time"] >= since])
        data, _, _ = self.call("recent_events", {"hours": 24, "kinds": ["earthquake"], "min_severity": 5})
        self.assertEqual([e["id"] for e in data["events"]], ["us1"])
        self.assertEqual(data["events"][0]["time"], "2026-11-22T19:40:00Z")
        data, _, _ = self.call("recent_events", {"bbox": [170, -25, -170, -10], "sort": "time"})   # across 180°
        self.assertEqual([e["id"] for e in data["events"]], ["f1", "us2"])
        view = dict(VIEW, bounds=[130, 30, 150, 45])
        data, _, _ = self.call("recent_events", {"in_view": True}, view=view)
        self.assertEqual([e["id"] for e in data["events"]], ["us1"])
        self.assertEqual(data["by_kind"], {"earthquake": 1})

    def test_list_layers_merges_browser_and_server(self):
        self.app.layer_specs.append({"id": "ships", "module": "ships", "title": "Ships", "group": "Ships",
                                     "description": "AIS positions", "source": "AISStream", "refresh": "live",
                                     "use": "", "_count": lambda: 42})
        self.app.layer_specs.append({"id": "hazards.quakes", "module": "hazards", "title": "Earthquakes",
                                     "group": "Events", "description": "", "source": "USGS", "refresh": "1 min", "use": ""})
        data, _, _ = self.call("list_layers", {})
        rows = {r["id"]: r for r in data["layers"]}
        self.assertTrue(rows["flights"]["on"])
        self.assertEqual(rows["ships"]["source"], "AISStream")
        self.assertEqual(rows["hazards.quakes"]["group"], "Events")
        data, _, _ = self.call("list_layers", {"group": "events"})
        self.assertEqual([r["id"] for r in data["layers"]], ["hazards.quakes"])

    def test_find_place(self):
        data, err, _ = self.call("find_place", {"query": "Strait of Hormuz"})
        self.assertFalse(err)
        self.assertEqual(data["results"][0]["bbox"], [55.6, 25.9, 57.2, 27.1])
        self.assertIn("Nominatim", data["source"])

    def test_actions_normalise(self):
        acts = Actions()
        known = {"ships", "flights", "cameras"}
        run_action("set_layers", {"on": ["ships"]}, acts, known)
        run_action("fly_to", {"lat": 1, "lon": 2}, acts, known)
        run_action("set_layers", {"on": ["flights"], "off": ["ships"]}, acts, known)
        run_action("fit_bounds", {"west": 50, "south": 20, "east": 60, "north": 30}, acts, known)
        run_action("set_projection", {"projection": "globe"}, acts, known)
        run_action("open_entity", {"entity": {"kind": "ship", "id": "235000001", "title": "X", "lat": 50, "lon": -1,
                                              "layer": "nope"}}, acts, known)
        run_action("whats_here_card", {"lat": 1, "lon": 2}, acts, known)
        out = acts.list()
        self.assertEqual(out[0], {"type": "set_layers", "on": ["flights"], "off": ["ships"]})
        self.assertEqual(out[1]["type"], "fit_bounds")
        self.assertEqual(out[2], {"type": "set_projection", "projection": "globe"})
        self.assertEqual(out[3], {"type": "open_entity", "kind": "ship", "id": "235000001", "title": "X",
                                  "lat": 50.0, "lon": -1.0})
        self.assertEqual(out[4], {"type": "whats_here", "lat": 1.0, "lon": 2.0})
        with self.assertRaises(ToolInputError):
            run_action("fit_bounds", {"west": 0, "south": 10, "east": 1, "north": 5}, acts, known)
        with self.assertRaises(ToolInputError):
            run_action("set_terrain", {"on": "maybe"}, acts, known)


class UtilTests(unittest.TestCase):
    def test_fit_json(self):
        small = {"a": 1}
        self.assertEqual(fit_json(small, 100), ('{"a":1}', False))
        big = {"items": [{"name": "n" * 50, "i": i} for i in range(2000)], "text": "y" * 50000}
        out, cut = fit_json(big, 12_000)
        self.assertTrue(cut)
        self.assertLessEqual(len(out), 12_000)
        self.assertIn("truncated", json.loads(out))
        out, cut = fit_json({"nan": float("nan"), "set": {1}}, 1000)
        self.assertEqual(json.loads(out), {"nan": None, "set": [1]})

    def test_parse_messages(self):
        for bad in (None, [], [{"role": "system", "content": "x"}], [{"role": "user", "content": 3}],
                    [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
                    [{"role": "user", "content": "x" * 12_001}]):
            with self.assertRaises(HTTPError):
                parse_messages(bad)
        long_history = [{"role": r, "content": f"{r} {i}"} for i in range(40) for r in ("user", "assistant")]
        long_history.append({"role": "user", "content": "last"})
        kept = parse_messages(long_history)
        self.assertLessEqual(len(kept), 24)
        self.assertEqual(kept[0]["role"], "user")
        self.assertEqual(kept[-1]["content"], "last")

    def test_parse_view(self):
        v = parse_view({"bounds": [1, 2, 3], "zoom": 99, "center": {"lat": 95, "lon": 0}, "layers_on": ["ok.id", "bad id!"],
                        "projection": "<script>", "terrain": "yes"})
        self.assertEqual(v, {"layers_on": ["ok.id"]})
        v = parse_view(VIEW)
        self.assertEqual(v["bounds"], [-1.2, 51.2, 0.3, 51.8])
        self.assertEqual(v["center"], {"lat": 51.5, "lon": -0.45})
        self.assertEqual(v["layers"][0]["count"], 120)

    def test_view_context(self):
        ctx = view_context(parse_view(VIEW), 1795380000.0, {"flights": "Aircraft"})
        self.assertIn("Current time: 2026-11-22 20:40 UTC (Sunday)", ctx)
        self.assertIn("Layers on: flights (Aircraft)", ctx)
        self.assertIn("km across", ctx)

    def test_mentioned_entities(self):
        cands = [{"kind": "ship", "id": "1", "title": "Ever Given"}, {"kind": "ship", "id": "2", "title": "Ever"},
                 {"kind": "aircraft", "id": "4ca2b3", "title": "RYR8NE"}]
        got = mentioned_entities("RYR8NE climbed past **Ever Given** and Everest.", cands, [])
        self.assertEqual([e["id"] for e in got], ["4ca2b3", "1"])


# ---------------------------------------------------------------------------------------------
# End to end over HTTP (the real loopback server and its guards)
# ---------------------------------------------------------------------------------------------
class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_provider_state()
        cls.app, cls.net, cls.tmp = make_app({})
        cls.server = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.app.bind[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.app.config._overrides.update(MASK)
        self.net.anthropic = []
        self.net.gate = None

    def req(self, method, path, body=None, headers=None):
        base = f"http://127.0.0.1:{self.port}"
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if method == "POST":
            hdrs.update({"Origin": base, "X-Meridian": "1", "Content-Type": "application/json"})
        hdrs.update(headers or {})
        r = urllib.request.Request(base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def test_off_state_is_a_clear_answer(self):
        status, data = self.req("GET", "/api/ai/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["provider"], "off")
        status, data = self.req("POST", "/api/ai/chat", {"messages": QUESTION, "view": VIEW})
        self.assertEqual(status, 200)
        self.assertEqual(data["error"]["code"], "NOT_CONFIGURED")
        self.assertIn("restart Meridian", data["reply"])
        status, data = self.req("POST", "/api/ai/chat", {"messages": QUESTION, "async": True})
        self.assertEqual((status, data["job"], data["result"]["error"]["code"]), (200, None, "NOT_CONFIGURED"))

    def test_bad_requests(self):
        self.assertEqual(self.req("POST", "/api/ai/chat", {"messages": []})[0], 400)
        self.assertEqual(self.req("POST", "/api/ai/chat", {"messages": [{"role": "user", "content": 5}]})[0], 400)
        self.assertEqual(self.req("GET", "/api/ai/jobs/nope")[0], 404)
        self.assertEqual(self.req("GET", "/api/ai/jobs/abcdefghijkl")[0], 404)
        self.assertEqual(self.req("POST", "/api/ai/jobs/abcdefghijkl/cancel", {})[0], 404)
        status, _ = self.req("POST", "/api/ai/chat", {"messages": QUESTION}, headers={"X-Meridian": "0"})
        self.assertEqual(status, 403)

    def test_sync_answer(self):
        self.app.config._overrides["ANTHROPIC_API_KEY"] = KEY
        self.net.anthropic = [(200, msg([use("t1", "fly_to", {"lat": 26.57, "lon": 56.25, "zoom": 7})], "tool_use")),
                              (200, msg([text("The **Strait of Hormuz** is on the map.")]))]
        status, data = self.req("POST", "/api/ai/chat", {"messages": QUESTION, "view": VIEW})
        self.assertEqual(status, 200, data)
        for key in ("reply", "actions", "trace", "provider", "model", "usage"):
            self.assertIn(key, data)
        self.assertEqual(data["actions"][0]["type"], "fly_to")
        self.assertEqual(data["trace"][0]["tool"], "fly_to")
        _, status_data = self.req("GET", "/api/ai/status")
        self.assertNotIn(KEY, json.dumps(status_data))

    def test_async_job_and_cancel(self):
        self.app.config._overrides["ANTHROPIC_API_KEY"] = KEY
        self.net.anthropic = [(200, msg([use("t1", "list_layers", {})], "tool_use")), (200, msg([text("Done.")]))]
        status, data = self.req("POST", "/api/ai/chat", {"messages": QUESTION, "view": VIEW, "async": True})
        self.assertEqual(status, 200, data)
        job = data["job"]
        for _ in range(100):
            _, snap = self.req("GET", f"/api/ai/jobs/{job}")
            if snap["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["result"]["reply"], "Done.")
        self.assertEqual(snap["trace"][0]["tool"], "list_layers")
        # Stop: the job is gated inside its first model call, cancelled, then released.
        self.net.gate = threading.Event()
        self.net.anthropic = [(200, msg([use("t1", "fly_to", {"lat": 1, "lon": 1})], "tool_use")),
                              (200, msg([text("never sent")]))]
        _, data = self.req("POST", "/api/ai/chat", {"messages": QUESTION, "view": VIEW, "async": True})
        job = data["job"]
        status, data = self.req("POST", f"/api/ai/jobs/{job}/cancel", {})
        self.assertEqual((status, data["status"]), (200, "cancelled"))
        self.net.gate.set()
        for _ in range(100):
            _, snap = self.req("GET", f"/api/ai/jobs/{job}")
            if snap.get("result"):
                break
            time.sleep(0.05)
        self.assertEqual(snap["status"], "cancelled")
        self.assertEqual(snap["result"]["stopped"], "cancelled")
        self.assertEqual(snap["result"]["actions"], [])
        self.assertEqual(len(self.net.anthropic), 1)   # the second model call never happened


# ---------------------------------------------------------------------------------------------
# The browser's Markdown renderer (Node)
# ---------------------------------------------------------------------------------------------
class MarkdownRendererTests(unittest.TestCase):
    def test_node_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run([node, str(ROOT / "tests" / "test_ai_markdown.mjs")], cwd=ROOT, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
