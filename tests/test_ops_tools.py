"""Tests for tools/gen_docs.py and tools/devcopy.py: neither may touch the live data/ folder by accident.
Temporary folders only, no network.

Run: python -m unittest tests.test_ops_tools -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f"ops_tool_{name}", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class GenDocsTests(unittest.TestCase):
    def test_builds_on_a_throwaway_folder_and_removes_it(self):
        gen_docs = load_tool("gen_docs")
        seen: dict = {}
        real_build = gen_docs.build_app
        real_mkdtemp = tempfile.mkdtemp

        def spy_build(config, **kw):
            seen["data_dir"] = config.data_dir
            return real_build(config, **kw)

        def spy_mkdtemp(*a, **kw):
            seen["scratch"] = real_mkdtemp(*a, **kw)
            return seen["scratch"]

        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as other:
            sentinel = Path(other) / "not-to-touch"
            env = dict(os.environ, MERIDIAN_DATA_DIR=str(sentinel))
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(gen_docs, "build_app", spy_build), \
                    mock.patch.object(gen_docs.tempfile, "mkdtemp", spy_mkdtemp), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gen_docs.main(Path(out)), 0)
            self.assertEqual(seen["data_dir"], Path(seen["scratch"]).resolve())
            self.assertNotEqual(seen["data_dir"], (ROOT / "data").resolve())
            self.assertFalse(sentinel.exists(), "MERIDIAN_DATA_DIR folder must not be used")
            self.assertFalse(Path(seen["scratch"]).exists(), "scratch folder must be deleted")
            self.assertIn("# Data sources", (Path(out) / "docs" / "SOURCES.md").read_text(encoding="utf-8"))
            self.assertTrue((Path(out) / ".env.example").is_file())


class DevCopyTests(unittest.TestCase):
    def setUp(self):
        self.devcopy = load_tool("devcopy")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.devcopy.ROOT = Path(self.tmp.name)       # never the real tree

    def run_main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.devcopy.main(argv)
        return code, buf.getvalue()

    def test_help_prints_usage_and_copies_nothing(self):
        for flag in ("--help", "-h"):
            code, out = self.run_main([flag])
            self.assertEqual(code, 0)
            self.assertIn("devcopy.py <team>", out)
            self.assertFalse((Path(self.tmp.name) / "data-dev").exists())

    def test_rejects_names_not_starting_with_letter_or_digit(self):
        for bad in ("-x", "--force", "a/b", "a b", "", "x" * 42):
            code, out = self.run_main([bad])
            self.assertEqual(code, 2, bad)
            self.assertIn("team names", out)
        self.assertFalse((Path(self.tmp.name) / "data-dev").exists())

    def test_valid_name_still_copies(self):
        import sqlite3
        data = Path(self.tmp.name) / "data"
        (data / "build" / "ships").mkdir(parents=True)
        (data / "build" / "ships" / "f.txt").write_text("x", encoding="utf-8")
        db = sqlite3.connect(str(data / "meridian.db"))
        db.execute("create table t(x)")
        db.commit()
        db.close()
        code, out = self.run_main(["fx4-tools"])
        self.assertEqual(code, 0)
        dst = Path(self.tmp.name) / "data-dev" / "fx4-tools"
        self.assertTrue((dst / "meridian.db").is_file())
        self.assertTrue((dst / "build" / "ships" / "f.txt").is_file())


if __name__ == "__main__":
    unittest.main()
