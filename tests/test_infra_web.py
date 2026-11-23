"""Infra browser helpers that run without a browser: the airport card's FAA status section
(tests/test_infra_delays.mjs, run with Node when it is installed).

Run: python -m unittest tests.test_infra_web -v
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class AirportDelaySectionTests(unittest.TestCase):
    def test_node_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run([node, str(ROOT / "tests" / "test_infra_delays.mjs")], cwd=ROOT, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ok 4 tests", proc.stdout)


if __name__ == "__main__":
    unittest.main()
