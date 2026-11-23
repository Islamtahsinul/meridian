"""Runs the Workspaces browser-logic suites in Node (skipped when node is not installed):
tests/fixtures/workspace/deeplink_test.mjs (the deep-link format, web/js/core/deeplink.js),
tests/fixtures/workspace/logic_test.mjs (workspace states, layer attribution, privacy rules, GeoJSON,
CSV and KML, web/js/layers/workspace/logic.js) and tests/fixtures/workspace/browser_test.mjs (with a
fake page: My places cards, Undo after applying a workspace, exports without duplicates, and deep links
that never change the saved layer set).

Run: python -m unittest tests.test_workspace_web -v
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "workspace"


class WorkspaceBrowserLogic(unittest.TestCase):
    def run_suite(self, name: str) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run([node, str(FIX / name)], cwd=ROOT, capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, f"{name} failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("tests passed", proc.stdout)
        self.assertTrue(proc.stdout.strip().splitlines()[-1].startswith("ok "), proc.stdout)

    def test_deeplink_suite(self):
        self.run_suite("deeplink_test.mjs")

    def test_logic_suite(self):
        self.run_suite("logic_test.mjs")

    def test_browser_suite(self):
        self.run_suite("browser_test.mjs")


if __name__ == "__main__":
    unittest.main()
