"""Tests for tools/housekeeping.py: the disk report and safe deletion of old logs, unfinished downloads,
re-downloadable caches, stale development copies and old backups. Temporary folders only, no network.

Run: python -m unittest tests.test_ops_housekeeping -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
DAY = 86400.0


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f"ops_tool_{name}", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                     # dataclasses look their module up while it loads
    spec.loader.exec_module(mod)
    return mod


hk = load_tool("housekeeping")


def put(path: Path, size: int = 100, age_days: float = 0.0, data: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if data is not None else b"x" * size)
    t = time.time() - age_days * DAY
    os.utime(path, (t, t))
    return path


def download(path: Path, age_days: float, url: str = "https://example.org/data/file.csv") -> Path:
    put(path, 1000, age_days)
    put(path.with_name(path.name + ".meta.json"), age_days=age_days,
        data=json.dumps({"url": url, "etag": None, "fetched_at": 0, "bytes": 1000}).encode())
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        d = self.data = self.base / "data"
        self.dev = self.base / "data-dev"
        self.backups = d / "backups"
        # the database and datasets: reported, never deleted
        d.mkdir()
        con = sqlite3.connect(str(d / "meridian.db"))
        con.execute("CREATE TABLE t(x)")
        con.commit()
        con.close()
        os.utime(d / "meridian.db", (time.time() - 90 * DAY,) * 2)
        put(d / "build" / "infra" / "airports.json", 5000, 90)
        # logs
        self.live_log = put(d / "logs" / "meridian.log", 300, 60)
        put(d / "logs" / "autostart.log", 300, 60)
        self.old_rotated = put(d / "logs" / "meridian.log.1", 400, 20)
        self.old_dev_log = put(d / "logs" / "meridian-flights.log", 500, 20)
        self.new_log = put(d / "logs" / "meridian-ai.log", 600, 2)
        # downloads
        self.old_download = download(d / "raw" / "hazards" / "geocodes.json", 45, "https://raw.githubusercontent.com/x/y")
        self.new_download = download(d / "raw" / "intel" / "events.csv", 3)
        self.jpriver = download(d / "raw" / "cameras" / "jpriver" / "scam-1001.json", 300)
        self.windy = put(d / "raw" / "cameras" / "windy" / "pages" / "c-AD-o0.json.gz", 2000, 100)
        self.probe = put(d / "raw" / "flights" / "probe" / "lol_mil.json", 700, 100)       # no download record
        self.part = put(d / "raw" / "infra" / "gppd.csv.part1234", 800, 3)
        self.new_part = put(d / "raw" / "infra" / "ports.csv.part99", 800, 0)
        self.tmpfile = put(d / "build" / "infra" / "cables.json.tmp", 900, 2)
        # backups and restore leftovers
        for day in range(1, 10):
            put(self.backups / f"meridian-backup-202611{day:02d}T000000Z.zip", 1000, 30 - day)
        put(self.backups / "notes.txt", 10, 100)
        self.old_aside = put(d / "meridian-before-restore-20261101T000000Z.db", 3000, 20)
        self.new_aside = put(d / "meridian-before-restore-20261120T000000Z.db", 3000, 1)
        # development copies
        put(self.dev / "stale" / "meridian.db", 4000, 10)
        put(self.dev / "stale" / "logs" / "meridian-x.log", 100, 10)
        put(self.dev / "fresh" / "meridian.db", 4000, 0.1)
        self.env = mock.patch.dict(os.environ, {"MERIDIAN_ENV_FILE": "none", "MERIDIAN_DATA_DIR": str(self.data)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_tool(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hk.main(["--data-dir", str(self.data), "--dev-root", str(self.dev), *map(str, argv)])
        return code, out.getvalue(), err.getvalue()

    def everything(self) -> set[str]:
        return {str(p.relative_to(self.base)) for p in self.base.rglob("*") if p.is_file()}


class ReportTests(Base):
    def test_dry_run_reports_and_changes_nothing(self):
        before = self.everything()
        code, out, err = self.run_tool()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.everything(), before)
        self.assertIn("Could be deleted (nothing has been changed)", out)
        self.assertIn("Run again with --apply", out)
        for words in ("Database:", "Datasets (build)", "Downloads (raw)", "Logs:", "Backups (backups): ",
                      "Databases kept aside by a restore", "Development copies (data-dev)"):
            self.assertIn(words, out)
        self.assertIn("Kept on purpose: raw/cameras/jpriver", out)
        self.assertIn("Kept on purpose: raw/cameras/windy", out)
        self.assertIn("1,800 requests", out)
        self.assertIn("from raw.githubusercontent.com", out)
        listed = {Path(line.strip().split(" (")[0]).name for line in out.splitlines() if line.startswith("    ")
                  and not line.strip().startswith("...")}
        self.assertEqual(listed, {"meridian.log.1", "meridian-flights.log", "geocodes.json", "gppd.csv.part1234",
                                  "cables.json.tmp", "meridian-backup-20261101T000000Z.zip",
                                  "meridian-backup-20261102T000000Z.zip", "meridian-before-restore-20261101T000000Z.db",
                                  "stale"})

    def test_database_header_is_read_without_sqlite(self):
        head = hk.db_header(self.data / "meridian.db")
        self.assertEqual(head["page_size"], 4096)
        self.assertGreaterEqual(head["pages"], 1)
        self.assertIsNone(hk.db_header(self.old_rotated))
        self.assertIsNone(hk.db_header(self.base / "missing.db"))

    def test_bad_arguments(self):
        for argv in (["--only", "logs,cats"], ["--keep-backups", "0"], ["--days", "-1"]):
            code, _out, err = self.run_tool(*argv)
            self.assertEqual(code, 2, argv)
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as cm:
            hk.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--apply", out.getvalue())


class ApplyTests(Base):
    def test_apply_deletes_exactly_what_was_listed(self):
        code, out, err = self.run_tool("--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Freed", out)
        gone = [self.old_rotated, self.old_dev_log, self.old_download, self.old_download.with_name("geocodes.json.meta.json"),
                self.part, self.tmpfile, self.old_aside, self.backups / "meridian-backup-20261101T000000Z.zip",
                self.backups / "meridian-backup-20261102T000000Z.zip", self.dev / "stale"]
        kept = [self.live_log, self.data / "logs" / "autostart.log", self.new_log, self.new_download, self.jpriver,
                self.jpriver.with_name("scam-1001.json.meta.json"), self.windy, self.probe, self.new_part,
                self.data / "meridian.db", self.data / "build" / "infra" / "airports.json", self.new_aside,
                self.backups / "notes.txt", self.dev / "fresh" / "meridian.db"]
        for p in gone:
            self.assertFalse(p.exists(), p)
        for p in kept:
            self.assertTrue(p.exists(), p)
        self.assertEqual(len(list(self.backups.glob("meridian-backup-*.zip"))), 7)
        self.assertEqual([p.name for p in self.dev.iterdir()], ["fresh"])
        code, out, _ = self.run_tool()
        self.assertIn("Nothing needs deleting", out)

    def test_only_some_kinds(self):
        code, out, _ = self.run_tool("--only", "logs", "--apply")
        self.assertEqual(code, 0)
        self.assertFalse(self.old_rotated.exists())
        self.assertTrue(self.old_download.exists())
        self.assertTrue((self.dev / "stale").exists())
        self.assertNotIn("Old backups", out)
        self.run_tool("--only", "raw", "--days", "400", "--apply")
        self.assertTrue(self.old_download.exists())                       # not 400 days old
        self.run_tool("--only", "raw", "--days", "40", "--apply")
        self.assertFalse(self.old_download.exists())
        self.assertTrue(self.jpriver.exists())                            # protected whatever its age

    def test_files_in_use_are_kept_and_reported(self):
        if os.name != "nt":
            self.skipTest("open files can be deleted on this system")
        with open(self.old_rotated, "rb"):
            code, out, _ = self.run_tool("--only", "logs", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("kept, in use or locked", out)
        self.assertTrue(self.old_rotated.exists())
        self.assertFalse(self.old_dev_log.exists())

    @unittest.skipUnless(os.name == "nt", "the open-database check is Windows-only")
    def test_an_open_development_copy_is_left_alone(self):
        con = sqlite3.connect(str(self.dev / "stale" / "meridian.db.real"))
        con.execute("CREATE TABLE t(x)")
        con.commit()
        con.close()
        os.replace(self.dev / "stale" / "meridian.db.real", self.dev / "stale" / "meridian.db")
        for p in (self.dev / "stale").rglob("*"):
            os.utime(p, (time.time() - 10 * DAY,) * 2)
        con = sqlite3.connect(str(self.dev / "stale" / "meridian.db"))
        con.execute("SELECT * FROM t").fetchall()
        try:
            code, out, _ = self.run_tool("--only", "devcopies", "--apply")
        finally:
            con.close()
        self.assertTrue((self.dev / "stale" / "meridian.db").exists())
        self.assertIn("Development copies nobody has changed recently, unchanged for 3 days: none", out)

    def test_an_interrupted_clean_up_is_finished_later(self):
        parked = put(self.dev / ".stale2.deleting-4242" / "logs" / "x.log", 100, 0)
        put(self.dev / ".hidden-but-not-ours" / "keep.txt", 100, 30)
        code, out, _ = self.run_tool("--only", "devcopies")
        self.assertIn("left by an earlier clean-up", out)
        code, out, _ = self.run_tool("--only", "devcopies", "--apply")
        self.assertEqual(code, 0)
        self.assertFalse(parked.parent.parent.exists())
        self.assertTrue((self.dev / ".hidden-but-not-ours" / "keep.txt").exists())

    def test_the_data_folder_in_use_is_never_a_stale_copy(self):
        inner = self.dev / "stale"                                        # housekeeping run on that copy itself
        with contextlib.redirect_stdout(io.StringIO()):
            code = hk.main(["--data-dir", str(inner), "--dev-root", str(self.dev), "--only", "devcopies", "--apply"])
        self.assertEqual(code, 0)
        self.assertTrue(inner.exists())

    @unittest.skipUnless(os.name == "nt", "directory junctions are a Windows feature")
    def test_links_and_junctions_are_never_followed(self):
        import _winapi
        if not hasattr(_winapi, "CreateJunction"):
            self.skipTest("no junction support in this Python")
        outside = self.base / "precious"
        put(outside / "keep-me.txt", 50, 100)
        _winapi.CreateJunction(str(outside), str(self.dev / "stale" / "link"))
        _winapi.CreateJunction(str(outside), str(self.data / "raw" / "hazards" / "link"))
        download(outside / "old.csv", 100)                                # looks like an old download, behind a link
        code, out, err = self.run_tool("--apply")
        self.assertEqual(code, 0, out + err)
        self.assertFalse((self.dev / "stale").exists())
        self.assertTrue((outside / "keep-me.txt").exists())
        self.assertTrue((outside / "old.csv").exists())
        self.assertNotIn("old.csv", out)

    def test_ages_read_plainly(self):
        now = time.time()
        self.assertEqual(hk.ago(now - 120, now), "2 min")
        self.assertEqual(hk.ago(now - 5 * 3600, now), "5 h")
        self.assertEqual(hk.ago(now - 20 * DAY, now), "20 days")


if __name__ == "__main__":
    unittest.main()
