"""Tests for the shared AI helper ``meridian.modules.ai.helper.complete`` (CONTRACT.md 15.4), used by
the briefing's narrative. A fake HTTP layer stands in for the Anthropic API and Ollama: no network.

Run: python -m unittest tests.test_briefing_helper -v
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from meridian.config import Config
from meridian.core_api import register_core
from meridian.datasets import DatasetManager
from meridian.modules import load_all
from meridian.modules.ai import helper
from meridian.modules.ai import providers as ai_providers
from meridian.net import NetError, NetResponse
from meridian.registry import App
from meridian.scheduler import Scheduler
from meridian.store import Store

KEY = "test-only-briefing-helper-key-0123456789abcdef"
MASK = {"ANTHROPIC_API_KEY": "", "AI_PROVIDER": "", "ANTHROPIC_MODEL": "", "ANTHROPIC_EFFORT": "",
        "ANTHROPIC_FALLBACK": "", "OLLAMA_URL": "", "OLLAMA_MODEL": ""}
logging.getLogger("meridian").addHandler(logging.NullHandler())


class FakeNet:
    def __init__(self):
        self.calls: list[dict] = []
        self.anthropic: list = []          # (status, body) | Exception | callable returning either
        self.tags: object = NetError("127.0.0.1: connection refused")
        self.ollama: list = []

    def set_host_interval(self, host, seconds):
        pass

    def request(self, url, *, method="GET", params=None, headers=None, data=None, json_body=None, timeout=20.0,
                retries=1, ttl=0.0, min_interval=None, max_bytes=0, accept=(200,), cache_key=None, allow_redirects=True):
        body = json.loads(data.decode("utf-8")) if data else None
        self.calls.append({"url": url, "headers": dict(headers or {}), "body": body, "timeout": timeout})
        host = urllib.parse.urlsplit(url).hostname
        if host == "api.anthropic.com":
            item = self.anthropic.pop(0)
            if callable(item):
                item = item()
            if isinstance(item, Exception):
                raise item
            status, payload = item
            raw = json.dumps(payload).encode("utf-8")
            if status not in accept:
                raise NetError(f"{host}: HTTP {status}", status=status, url=url, body=raw)
            return NetResponse(status, {}, raw, url)
        if host == "127.0.0.1" and url.endswith("/api/tags"):
            if isinstance(self.tags, Exception):
                raise self.tags
            return NetResponse(200, {}, json.dumps(self.tags).encode("utf-8"), url)
        if host == "127.0.0.1" and url.endswith("/api/chat"):
            status, payload = self.ollama.pop(0)
            return NetResponse(status, {}, json.dumps(payload).encode("utf-8"), url)
        raise AssertionError(f"unexpected request to {url}")

    def bodies(self, host="api.anthropic.com"):
        return [c for c in self.calls if host in c["url"]]


def message(content, stop="end_turn", model="claude-opus-5-5"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop, "usage": {"input_tokens": 50, "output_tokens": 20}}


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class HelperBase(unittest.TestCase):
    overrides: dict = {}
    modules: set = {"ai"}

    def setUp(self):
        with ai_providers._lock:
            ai_providers._disabled.clear()
        self.tmp = tempfile.mkdtemp(prefix="meridian-helper-test-")
        cfg = Config(None, overrides={**MASK, **self.overrides})
        self.net = FakeNet()
        log = logging.getLogger("meridian.test.helper")
        self.app = App(cfg, Store(Path(self.tmp) / "t.db"), self.net, log, Scheduler(log), DatasetManager(log))
        register_core(self.app)
        if self.modules:
            load_all(self.app, self.modules)
        helper.bind(self.app)
        self.capture = Capture()
        logging.getLogger("meridian").addHandler(self.capture)
        logging.getLogger("meridian").setLevel(logging.DEBUG)

    def tearDown(self):
        logging.getLogger("meridian").removeHandler(self.capture)
        helper.unbind()
        self.app.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def no_secret_logged(self):
        for line in self.capture.lines:
            self.assertNotIn(KEY, line)


class OffTests(HelperBase):
    def test_unbound(self):
        helper.unbind()
        meta = {}
        self.assertIsNone(helper.complete("s", "p", meta=meta))
        self.assertEqual(meta["code"], "NOT_LOADED")
        self.assertEqual(self.net.calls, [])

    def test_no_key_and_no_ollama(self):
        meta = {}
        self.assertIsNone(helper.complete("system", "prompt", meta=meta))
        self.assertEqual(meta["code"], "NOT_CONFIGURED")
        self.assertEqual([c["url"] for c in self.net.calls], ["http://127.0.0.1:11434/api/tags"])
        st = helper.status()
        self.assertEqual((st["ready"], st["provider"]), (False, "off"))

    def test_bad_input(self):
        self.app.config._overrides["ANTHROPIC_API_KEY"] = KEY
        self.assertIsNone(helper.complete("s", "   "))
        self.assertIsNone(helper.complete("s", "p", max_tokens=0))
        self.assertIsNone(helper.complete("s", "p", max_tokens=True))
        self.assertIsNone(helper.complete(None, "p"))
        self.assertIsNone(helper.complete("s", "x" * (helper.MAX_INPUT_CHARS + 1)))
        self.assertEqual(self.net.bodies(), [])


class DisabledTests(HelperBase):
    overrides = {"AI_PROVIDER": "off", "ANTHROPIC_API_KEY": KEY}

    def test_off_setting_wins_over_a_key(self):
        meta = {}
        self.assertIsNone(helper.complete("system", "prompt", meta=meta))
        self.assertEqual(meta["code"], "DISABLED")
        self.assertEqual(self.net.calls, [])


class NotLoadedTests(HelperBase):
    overrides = {"ANTHROPIC_API_KEY": KEY}
    modules = set()                  # the ai module is not part of this app

    def test_none_without_the_ai_module(self):
        self.assertIsNone(helper.complete("system", "prompt"))
        self.assertEqual(self.net.calls, [])
        self.assertEqual(helper.status()["code"], "NOT_LOADED")


class AnthropicTests(HelperBase):
    overrides = {"ANTHROPIC_API_KEY": KEY}

    def test_request_shape_and_answer(self):
        self.net.anthropic.append((200, message([{"type": "thinking", "thinking": "", "signature": "s"},
                                                  {"type": "text", "text": "  The narrative.  "}])))
        meta = {}
        out = helper.complete("You write briefings.", "Items: ...", meta=meta)
        self.assertEqual(out, "The narrative.")
        self.assertEqual((meta["provider"], meta["model"], meta["code"]), ("anthropic", "claude-opus-5-5", None))
        self.assertEqual(meta["usage"]["output_tokens"], 20)
        [call] = self.net.bodies()
        body = call["body"]
        self.assertEqual(body["max_tokens"], 1200)
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("cache_control", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["system"], [{"type": "text", "text": "You write briefings."}])
        self.assertEqual(body["messages"], [{"role": "user", "content": [{"type": "text", "text": "Items: ..."}]}])
        self.assertEqual(call["headers"]["x-api-key"], KEY)            # the key goes in the header only
        self.assertNotIn(KEY, json.dumps(body))
        self.assertNotIn(KEY, json.dumps(helper.last()))
        self.assertNotIn(KEY, json.dumps(helper.status()))
        self.assertEqual(helper.status()["provider"], "anthropic")
        self.no_secret_logged()

    def test_errors_return_none_without_leaking_the_key(self):
        self.net.anthropic.append((401, {"type": "error", "error": {"type": "authentication_error",
                                                                     "message": f"invalid x-api-key {KEY}"}}))
        meta = {}
        self.assertIsNone(helper.complete("s", "p", meta=meta))
        self.assertEqual(meta["code"], "PROVIDER_AUTH")
        self.net.anthropic.append(NetError("api.anthropic.com: connection reset"))
        self.assertIsNone(helper.complete("s", "p", meta=meta))
        self.assertEqual(meta["code"], "PROVIDER_UNREACHABLE")
        self.no_secret_logged()

    def test_refusal_and_empty_answers(self):
        self.net.anthropic.append((200, message([], stop="refusal")))
        meta = {}
        self.assertIsNone(helper.complete("s", "p", meta=meta))
        self.assertEqual(meta["code"], "REFUSED")
        self.net.anthropic.append((200, message([{"type": "text", "text": "   "}])))
        self.assertIsNone(helper.complete("s", "p", meta=meta))
        self.assertEqual(meta["code"], "EMPTY")

    def test_cut_short_keeps_whole_paragraphs(self):
        self.net.anthropic.append((200, message([{"type": "text", "text": "First paragraph.\n\nSecond one, cut of"}],
                                                stop="max_tokens")))
        self.assertEqual(helper.complete("s", "p"), "First paragraph.")
        self.net.anthropic.append((200, message([{"type": "text", "text": "Only half a sente"}], stop="max_tokens")))
        meta = {}
        self.assertIsNone(helper.complete("s", "p", meta=meta))
        self.assertEqual(meta["code"], "CUT_SHORT")

    def test_hard_timeout(self):
        gate = threading.Event()
        self.net.anthropic.append(lambda: (gate.wait(10), (200, message([{"type": "text", "text": "late"}])))[1])
        meta = {}
        started = time.monotonic()
        with mock.patch.object(helper, "MIN_TIMEOUT", 0.2):
            self.assertIsNone(helper.complete("s", "p", timeout=0.5, meta=meta))
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(meta["code"], "TIMEOUT")
        gate.set()


class OllamaTests(HelperBase):
    def test_local_model_with_a_token_cap(self):
        self.net.tags = {"models": [{"name": "qwen3:8b"}]}
        self.net.ollama.append((200, {"model": "qwen3:8b", "message": {"role": "assistant",
                                                                       "content": "<think>x</think>Local answer."},
                                      "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 5}))
        meta = {}
        self.assertEqual(helper.complete("sys", "prompt", max_tokens=900, meta=meta), "Local answer.")
        self.assertEqual((meta["provider"], meta["model"]), ("ollama", "qwen3:8b"))
        [chat] = [c for c in self.net.calls if c["url"].endswith("/api/chat")]
        self.assertEqual(chat["body"]["options"]["num_predict"], 900)
        self.assertNotIn("tools", chat["body"])
        self.assertEqual([m["role"] for m in chat["body"]["messages"]], ["system", "user"])
        self.assertEqual(chat["body"]["messages"][1]["content"], "prompt")


if __name__ == "__main__":
    unittest.main()
