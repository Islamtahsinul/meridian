"""Runs the core browser-logic suites in Node (skipped when node is not installed):
tests/test_core_dom.mjs (URL safety, kind labels), tests/test_core_draw.mjs (drawing geometry,
the replay clock and notification rules, CONTRACT.md section 15.3) and tests/test_core_layers.mjs
(layers.resume restores layers together).

Run: python -m unittest tests.test_core_web -v
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class CoreBrowserLogic(unittest.TestCase):
    def run_suite(self, name: str) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run([node, str(ROOT / "tests" / name)], cwd=ROOT, capture_output=True, text=True,
                              timeout=120, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, f"{name} failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("ok ", proc.stdout)

    def test_dom_suite(self):
        self.run_suite("test_core_dom.mjs")

    def test_draw_time_notify_suite(self):
        self.run_suite("test_core_draw.mjs")

    def test_layers_suite(self):
        self.run_suite("test_core_layers.mjs")


if __name__ == "__main__":
    unittest.main()
