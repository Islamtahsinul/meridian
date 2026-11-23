"""Runs the Alerts browser-logic suite in Node (skipped when node is not installed):
tests/fixtures/alerts/logic_test.mjs covers quiet hours, who gets notified, tones and preview circles.

Run: python -m unittest tests.test_alerts_web -v
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class AlertsWebLogicTests(unittest.TestCase):
    def test_logic_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run([node, str(ROOT / "tests" / "fixtures" / "alerts" / "logic_test.mjs")], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("tests passed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
