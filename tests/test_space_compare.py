"""Space compare tool: the date pair after a change of imagery source, run in Node
(tests/test_space_compare_web.mjs, skipped when node is not installed)."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CompareDates(unittest.TestCase):
    def test_source_change_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run([node, str(ROOT / "tests" / "test_space_compare_web.mjs")], cwd=ROOT,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("passed", proc.stdout)
        self.assertIn(" 0 failed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
