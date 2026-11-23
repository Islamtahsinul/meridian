"""Runs the flights browser-helper checks (tests/test_flights_web.mjs) under unittest; skipped when node is
not installed."""

import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class FlightsBrowserHelpers(unittest.TestCase):
    def test_node_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run([node, str(ROOT / "tests" / "test_flights_web.mjs")], cwd=ROOT, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("0 failed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
