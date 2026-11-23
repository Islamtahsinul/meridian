"""Tests for tools/backup.py: online backups, list, verify, prune and restore. Temporary folders only, no
network (the "is Meridian running" checks talk to stand-in servers on 127.0.0.1).

Run: python -m unittest tests.test_ops_backup -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f"ops_tool_{name}", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                     # dataclasses look their module up while it loads
    spec.loader.exec_module(mod)
    return mod


backup = load_tool("backup")
oc = backup.oc
SECRET = "sk-test-not-a-real-key-7f3a9c"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StandInServer:
    """A loopback HTTP server that answers /api/core/manifest like Meridian, or like another program."""

    def __init__(self, meridian: bool = True):
        body = json.dumps({"app": {"name": "Meridian", "version": "test", "started_at": time.time()}}
                          if meridian else {"hello": "world"}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_db(path: Path, favourites: int = 3) -> None:
    from meridian.store import Store
    store = Store(path)
    for i in range(favourites):
        store.execute("INSERT INTO favourites(kind, ref, title, created_at) VALUES(?,?,?,?)",
                      ("camera", f"x:{i}", f"Camera {i}", 1.7e9 + i))
    cid = store.execute("INSERT INTO casefiles(title, notes, created_at, updated_at) VALUES(?,?,?,?)",
                        ("Case", "notes", 1.7e9, 1.7e9)).lastrowid
    store.execute("INSERT INTO casefile_items(casefile_id, kind, ref, added_at) VALUES(?,?,?,?)", (cid, "ship", "1", 1.7e9))
    store.close()


def count(db: Path, table: str = "favourites") -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.data = self.base / "data"
        self.data.mkdir()
        self.db = self.data / "meridian.db"
        make_db(self.db)
        self.dest = self.data / "backups"
        self.port = free_port()
        self.env = mock.patch.dict(os.environ, {"MERIDIAN_ENV_FILE": "none", "MERIDIAN_DATA_DIR": str(self.data),
                                                "MERIDIAN_PORT": str(self.port)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_tool(self, *argv, answer: str | None = None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("builtins.input", side_effect=lambda prompt="": answer if answer is not None else ""):
            code = backup.main([str(a) for a in argv])
        return code, out.getvalue(), err.getvalue()

    def create(self, *extra) -> Path:
        code, out, err = self.run_tool("create", *extra)
        self.assertEqual(code, 0, out + err)
        return backup.backups_in(self.dest)[0]


class CreateTests(Base):
    def test_create_list_and_verify(self):
        path = self.create()
        self.assertRegex(path.name, r"^meridian-backup-\d{8}T\d{6}Z\.zip$")
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(names, {"manifest.json", "meridian.db", "settings.json"})
        self.assertEqual((manifest["format"], manifest["version"], manifest["env_included"]), ("meridian-backup", 1, False))
        self.assertEqual(manifest["database"]["counts"]["favourites"], 3)
        self.assertEqual(manifest["database"]["integrity"], "ok")
        code, out, _ = self.run_tool("list")
        self.assertEqual(code, 0)
        self.assertIn(path.name, out)
        self.assertIn("3 favourites", out)
        code, out, err = self.run_tool("verify")
        self.assertEqual(code, 0, err)
        self.assertIn("good", out)
        self.assertEqual(list(self.data.glob(".*.tmp")) + list(self.dest.glob("*.partial")), [])

    def test_backup_copy_is_one_consistent_file(self):
        path = self.create()
        out = self.base / "check.db"
        with zipfile.ZipFile(path) as zf:
            out.write_bytes(zf.read("meridian.db"))
        con = sqlite3.connect(str(out))
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "delete")   # no -wal needed
            self.assertEqual(con.execute("SELECT COUNT(*) FROM casefile_items").fetchone()[0], 1)
        finally:
            con.close()

    def test_online_backup_while_writing(self):
        from meridian.store import Store
        store = Store(self.db)
        stop = threading.Event()
        written = [0]

        def writer():
            while not stop.is_set():
                store.execute("INSERT INTO kv(k, v, updated_at) VALUES(?, '1', 0)", (f"k{written[0]}",))
                written[0] += 1

        t = threading.Thread(target=writer)
        t.start()
        try:
            time.sleep(0.2)
            before = written[0]
            path = self.create()
            after = written[0]
        finally:
            stop.set()
            t.join()
            store.close()
        code, _out, err = self.run_tool("verify", path)
        self.assertEqual(code, 0, err)
        with zipfile.ZipFile(path) as zf:
            snap = self.base / "snap.db"
            snap.write_bytes(zf.read("meridian.db"))
        rows = count(snap, "kv")
        self.assertGreaterEqual(rows, before)
        self.assertLessEqual(rows, after + 1)
        self.assertGreater(after, before)                             # the writer kept going meanwhile

    def test_env_left_out_and_key_values_never_recorded(self):
        env = self.base / ".env"
        env.write_text(f"AISSTREAM_API_KEY={SECRET}\nAISSTREAM_AREA=50,0,60,10\nMERIDIAN_HISTORY_DAYS=45\n"
                       f"SOME_NEW_SETTING=unknown-value\nOLLAMA_URL=http://user:{SECRET}@127.0.0.1:11434\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"MERIDIAN_ENV_FILE": str(env)}):
            code, out, err = self.run_tool("create")
        self.assertEqual(code, 0, err)
        self.assertNotIn(SECRET, out + err)
        path = backup.backups_in(self.dest)[0]
        with zipfile.ZipFile(path) as zf:
            self.assertNotIn("env/.env", zf.namelist())
            for name in zf.namelist():
                self.assertNotIn(SECRET.encode(), zf.read(name), name)
            settings = json.loads(zf.read("settings.json"))
        self.assertEqual(settings["settings"], {"AISSTREAM_AREA": "50,0,60,10", "MERIDIAN_HISTORY_DAYS": "45"})
        self.assertEqual(settings["keys_set"], ["AISSTREAM_API_KEY", "OLLAMA_URL", "SOME_NEW_SETTING"])

    def test_include_env_needs_an_explicit_yes(self):
        env = self.base / ".env"
        env.write_bytes(f"AISSTREAM_API_KEY={SECRET}\n".encode())
        with mock.patch.dict(os.environ, {"MERIDIAN_ENV_FILE": str(env)}):
            code, out, _ = self.run_tool("create", "--include-env", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("Warning", out)
            self.assertEqual(backup.backups_in(self.dest), [])
            code, out, _ = self.run_tool("create", "--include-env", answer="no")
            self.assertEqual(code, 1)
            self.assertIn("Nothing was saved", out)
            self.assertEqual(backup.backups_in(self.dest), [])
            code, out, _ = self.run_tool("create", "--include-env", answer="yes")
            self.assertEqual(code, 0)
            self.assertIn("API keys in plain text", out)
            self.assertNotIn(SECRET, out)
            path = backup.backups_in(self.dest)[0]
            with zipfile.ZipFile(path) as zf:
                self.assertEqual(zf.read("env/.env").decode(), f"AISSTREAM_API_KEY={SECRET}\n")
                self.assertTrue(json.loads(zf.read("manifest.json"))["env_included"])
            code, out, _ = self.run_tool("list")
            self.assertIn(".env included", out)
        code, _out, err = self.run_tool("create", "--include-env", "--yes")            # MERIDIAN_ENV_FILE=none
        self.assertEqual(code, 1)
        self.assertIn("no .env file in use", err)

    def test_refusals(self):
        with mock.patch.object(oc, "free_space", return_value=1000):
            code, _out, err = self.run_tool("create")
        self.assertEqual(code, 1)
        self.assertIn("Not enough free space", err)
        code, _out, err = self.run_tool("create", "--data-dir", self.base / "empty")
        self.assertEqual(code, 1)
        self.assertIn("no database", err)
        self.assertEqual(backup.backups_in(self.dest), [])
        code, _out, _err = self.run_tool("prune", "--keep", "0")
        self.assertEqual(code, 2)

    def test_create_can_prune_afterwards(self):
        for _ in range(3):
            self.create()
        self.assertEqual(len(backup.backups_in(self.dest)), 3)
        self.create("--keep", "2")
        self.assertEqual(len(backup.backups_in(self.dest)), 2)

    def test_help(self):
        for cmd in ([], ["create"], ["restore"], ["verify"], ["list"], ["prune"]):
            with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as cm:
                backup.main(cmd + ["--help"])
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("usage", out.getvalue())


class PruneTests(Base):
    def test_prune_keeps_the_newest_and_ignores_other_files(self):
        real = self.create()
        names = ["meridian-backup-20261101T000000Z.zip", "meridian-backup-20261102T000000Z.zip",
                 "meridian-backup-20261102T000000Z-2.zip", "meridian-backup-20261103T000000Z.zip"]
        for n in names:
            (self.dest / n).write_bytes(real.read_bytes())
        (self.dest / "notes.txt").write_text("mine")
        (self.dest / "meridian-backup-latest.zip").write_bytes(b"not ours")
        order = [p.name for p in backup.backups_in(self.dest)]
        self.assertEqual(order[1:], ["meridian-backup-20261103T000000Z.zip", "meridian-backup-20261102T000000Z-2.zip",
                                     "meridian-backup-20261102T000000Z.zip", "meridian-backup-20261101T000000Z.zip"])
        code, out, _ = self.run_tool("prune", "--keep", "2", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would delete", out)
        self.assertEqual(len(backup.backups_in(self.dest)), 5)
        code, out, _ = self.run_tool("prune", "--keep", "2")
        self.assertEqual(code, 0)
        self.assertEqual([p.name for p in backup.backups_in(self.dest)], order[:2])
        self.assertTrue((self.dest / "notes.txt").exists())
        self.assertTrue((self.dest / "meridian-backup-latest.zip").exists())
        code, out, _ = self.run_tool("prune", "--keep", "2")
        self.assertIn("Nothing to prune", out)


class VerifyTests(Base):
    def rewrite(self, src: Path, dst: Path, change) -> Path:
        with zipfile.ZipFile(src) as zin:
            members = {n: zin.read(n) for n in zin.namelist()}
        change(members)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for n, data in members.items():
                zout.writestr(n, data)
        return dst

    def verify(self, path: Path) -> tuple[int, str]:
        code, out, err = self.run_tool("verify", path)
        return code, out + err

    def test_damage_is_found(self):
        good = self.create()
        # flip one byte inside the compressed database: the zip's CRC catches it
        raw = bytearray(good.read_bytes())
        with zipfile.ZipFile(good) as zf:
            info = zf.getinfo("meridian.db")
        raw[info.header_offset + 30 + len(info.filename) + 40] ^= 0xFF
        flipped = self.base / "flipped.zip"
        flipped.write_bytes(bytes(raw))
        code, text = self.verify(flipped)
        self.assertEqual(code, 1)
        self.assertIn("damaged", text)

        def wrong_sum(m):
            man = json.loads(m["manifest.json"])
            man["database"]["sha256"] = "0" * 64
            m["manifest.json"] = json.dumps(man).encode()
        code, text = self.verify(self.rewrite(good, self.base / "sum.zip", wrong_sum))
        self.assertIn("does not match its checksum", text)

        def newer(m):
            man = json.loads(m["manifest.json"])
            man["version"] = 99
            m["manifest.json"] = json.dumps(man).encode()
        code, text = self.verify(self.rewrite(good, self.base / "newer.zip", newer))
        self.assertIn("newer Meridian", text)

        code, text = self.verify(self.rewrite(good, self.base / "nomanifest.zip", lambda m: m.pop("manifest.json")))
        self.assertIn("not a Meridian backup", text)

        junk = b"this is not a database" * 100
        import hashlib

        def not_sqlite(m):
            m["meridian.db"] = junk
            man = json.loads(m["manifest.json"])
            man["database"].update(bytes=len(junk), sha256=hashlib.sha256(junk).hexdigest())
            m["manifest.json"] = json.dumps(man).encode()
        code, text = self.verify(self.rewrite(good, self.base / "junk.zip", not_sqlite))
        self.assertIn("integrity check", text)

        other = self.base / "other.db"
        con = sqlite3.connect(str(other))
        con.execute("CREATE TABLE t(x)")
        con.commit()
        con.close()
        data = other.read_bytes()

        def foreign(m):
            m["meridian.db"] = data
            man = json.loads(m["manifest.json"])
            man["database"].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            m["manifest.json"] = json.dumps(man).encode()
        code, text = self.verify(self.rewrite(good, self.base / "foreign.zip", foreign))
        self.assertIn("missing Meridian's tables", text)

        (self.base / "plain.zip").write_bytes(b"PK not really")
        code, text = self.verify(self.base / "plain.zip")
        self.assertEqual(code, 1)
        code, text = self.verify(self.base / "missing.zip")
        self.assertIn("No backup called", text)

    def test_verify_all(self):
        self.create()
        (self.dest / "meridian-backup-20200101T000000Z.zip").write_bytes(b"broken")
        code, out, err = self.run_tool("verify", "--all")
        self.assertEqual(code, 1)
        self.assertIn("good", out)
        self.assertIn("meridian-backup-20200101T000000Z.zip", err)


class RestoreTests(Base):
    def add_favourite(self):
        con = sqlite3.connect(str(self.db))
        con.execute("INSERT INTO favourites(kind, ref, title, created_at) VALUES('camera', 'new', 'New', 1.8e9)")
        con.commit()
        con.close()

    def test_restore_puts_the_backup_back_and_keeps_the_current_database(self):
        self.create()
        self.add_favourite()
        self.assertEqual(count(self.db), 4)
        code, out, err = self.run_tool("restore", "latest", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(count(self.db), 3)
        aside = list(self.data.glob("meridian-before-restore-*.db"))
        self.assertEqual(len(aside), 1)
        self.assertEqual(count(aside[0]), 4)
        self.assertIn("kept as", out)
        self.assertEqual(list(self.data.glob(".restore-*")), [])

    def test_dry_run_and_declining_change_nothing(self):
        self.create()
        self.add_favourite()
        code, out, _ = self.run_tool("restore", "latest", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", out)
        code, out, _ = self.run_tool("restore", "latest", answer="no")
        self.assertEqual(code, 1)
        self.assertEqual(count(self.db), 4)
        self.assertEqual(list(self.data.glob("meridian-before-restore-*")), [])

    def test_refuses_while_meridian_answers_on_the_port(self):
        self.create()
        self.add_favourite()
        for meridian, words in ((True, "Meridian is answering"), (False, "Another program is answering")):
            server = StandInServer(meridian=meridian)
            try:
                code, _out, err = self.run_tool("restore", "latest", "--yes", "--port", server.port)
            finally:
                server.close()
            self.assertEqual(code, 1)
            self.assertIn(words, err)
            self.assertIn("Nothing was changed", err)
            self.assertEqual(count(self.db), 4)

    @unittest.skipUnless(os.name == "nt", "the open-file check is Windows-only")
    def test_refuses_while_the_database_is_open(self):
        self.create()
        con = sqlite3.connect(str(self.db))
        con.execute("SELECT COUNT(*) FROM favourites").fetchone()
        try:
            self.assertTrue(oc.file_in_use(self.db))
            code, _out, err = self.run_tool("restore", "latest", "--yes")
        finally:
            con.close()
        self.assertEqual(code, 1)
        self.assertIn("is open in another program", err)
        self.assertFalse(oc.file_in_use(self.db))

    def test_restore_into_a_fresh_data_folder(self):
        path = self.create()
        fresh = self.base / "newpc" / "data"
        server = StandInServer(meridian=True)          # a practice restore while Meridian runs: nothing to overwrite
        try:
            code, out, err = self.run_tool("restore", path, "--yes", "--data-dir", fresh, "--port", server.port)
        finally:
            server.close()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(count(fresh / "meridian.db"), 3)
        self.assertIn("no database", out)

    def test_a_failed_check_after_the_swap_puts_everything_back(self):
        self.create()
        self.add_favourite()
        real = backup.integrity
        with mock.patch.object(backup, "integrity", side_effect=lambda db, quick=False: "bad page" if quick else real(db)):
            code, _out, err = self.run_tool("restore", "latest", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("left as it was", err)
        self.assertEqual(count(self.db), 4)
        self.assertEqual(list(self.data.glob("meridian-before-restore-*")), [])

    def test_restore_env_only_on_request_and_never_over_an_existing_file(self):
        env = self.base / ".env"
        env.write_bytes(f"AISSTREAM_API_KEY={SECRET}\n".encode())
        with mock.patch.dict(os.environ, {"MERIDIAN_ENV_FILE": str(env)}):
            self.create("--include-env", "--yes")
            env.unlink()
            code, out, _ = self.run_tool("restore", "latest", "--yes")
            self.assertEqual(code, 0)
            self.assertFalse(env.exists())                           # not without --restore-env
            self.assertIn("Keys are not restored", out)
            self.assertNotIn(SECRET, out)
            # into the data folder in use, with no .env yet: the .env comes back
            code, out, _ = self.run_tool("restore", "latest", "--yes", "--restore-env")
            self.assertEqual(code, 0)
            self.assertEqual(env.read_bytes(), f"AISSTREAM_API_KEY={SECRET}\n".encode())
            # with a .env there it is left alone, and the copy goes into a new folder inside the data folder
            # (git-ignored), never beside the .env and never over an earlier copy
            env.write_text("MINE=1\n", encoding="utf-8")
            earlier = self.data / "env-from-backup-20261123T120000Z"
            earlier.mkdir()
            (earlier / ".env").write_text("AN EARLIER COPY\n", encoding="utf-8")
            with mock.patch.object(oc, "utc_stamp", return_value="20261123T120000Z"):
                code, out, err = self.run_tool("restore", "latest", "--yes", "--restore-env")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(env.read_text(encoding="utf-8"), "MINE=1\n")
            self.assertEqual((earlier / ".env").read_text(encoding="utf-8"), "AN EARLIER COPY\n")
            copy = self.data / "env-from-backup-20261123T120000Z-2" / ".env"
            self.assertEqual(copy.read_bytes(), f"AISSTREAM_API_KEY={SECRET}\n".encode())
            self.assertIn(f"saved as {copy}", out)
            self.assertNotIn(SECRET, out + err)
            self.assertEqual(sorted(p.name for p in self.base.iterdir()), [".env", "data"])   # no .env.from-backup

    def test_a_practice_restore_keeps_the_env_copy_in_the_practice_folder(self):
        env = self.base / ".env"
        env.write_bytes(f"AISSTREAM_API_KEY={SECRET}\n".encode())
        practice = self.base / "practice"
        with mock.patch.dict(os.environ, {"MERIDIAN_ENV_FILE": str(env)}):
            path = self.create("--include-env", "--yes")
            env.unlink()                   # even with no .env in use, a practice restore must not write one
            code, out, err = self.run_tool("restore", path, "--yes", "--restore-env", "--data-dir", practice)
            self.assertEqual(code, 0, out + err)
            self.assertFalse(env.exists())
            copies = list(practice.glob("env-from-backup-*/.env"))
            self.assertEqual(len(copies), 1)
            # named .env, so the repository's ignore rule for .env covers it wherever the folder is
            self.assertEqual(copies[0].name, ".env")
            self.assertEqual(copies[0].read_bytes(), f"AISSTREAM_API_KEY={SECRET}\n".encode())
            self.assertIn("left alone", out)
            self.assertNotIn(SECRET, out + err)
            self.assertEqual(count(practice / "meridian.db"), 3)
            self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["data", "practice"])
            # a copy that cannot be written is reported; the restored database stays
            with mock.patch.object(backup, "write_new", side_effect=OSError("disk full")):
                code, _out, err = self.run_tool("restore", path, "--yes", "--restore-env",
                                                "--data-dir", self.base / "practice2")
            self.assertEqual(code, 1)
            self.assertIn("The database was restored, but the backup's .env could not be saved (disk full)", err)
            self.assertEqual(count(self.base / "practice2" / "meridian.db"), 3)
            self.assertEqual(list((self.base / "practice2").glob("env-from-backup-*")), [])   # no empty folder left

    def test_bad_backup_is_refused_before_anything_changes(self):
        (self.dest).mkdir(exist_ok=True)
        bad = self.dest / "meridian-backup-20261101T000000Z.zip"
        bad.write_bytes(b"broken")
        code, _out, err = self.run_tool("restore", bad.name, "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was changed", err)
        self.assertEqual(count(self.db), 3)


if __name__ == "__main__":
    unittest.main()
