"""Tests for tools/autostart.py: the Windows shortcut it writes, install, remove and status in a temporary
"Startup" folder, the server options it accepts, and the launcher's log. Nothing is installed for real: every
test uses temporary folders (APPDATA too), and the launcher runs a stand-in server.

Run: python -m unittest tests.test_ops_autostart -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import struct
import sys
import tempfile
import types
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


auto = load_tool("autostart")
oc = auto.oc


class ShortcutFormatTests(unittest.TestCase):
    def test_round_trip(self):
        for target in (r"C:\Users\owner\AppData\Local\Programs\Python\Python311\pythonw.exe",
                       r"D:\Données\Python 3.11\pythonw.exe", "C:\\ユーザー\\pythonw.exe"):
            data = auto.build_lnk(target, '"C:\\x y\\tools\\autostart.py" run --delay 20', r"C:\x y", "Meridian: test")
            self.assertEqual(data[:4], b"\x4c\x00\x00\x00")
            self.assertEqual(data[4:20], auto.LNK_CLSID)
            self.assertEqual(data[-4:], b"\x00\x00\x00\x00")
            info = auto.parse_lnk(data)
            self.assertEqual(info["target"], target)
            self.assertEqual(info["arguments"], '"C:\\x y\\tools\\autostart.py" run --delay 20')
            self.assertEqual((info["working_dir"], info["description"], info["show"]), (r"C:\x y", "Meridian: test", 7))

    def test_item_id_list_is_stored_first_and_skipped_when_reading(self):
        fake_ids = b"\x14\x00\x1f\x50" + bytes(16) + b"\x00\x00"
        data = auto.build_lnk(r"C:\p\pythonw.exe", "run", "", "Meridian", id_list=fake_ids)
        flags = struct.unpack_from("<I", data, 20)[0]
        self.assertTrue(flags & auto.HAS_ID_LIST)
        self.assertEqual(struct.unpack_from("<H", data, 76)[0], len(fake_ids))
        self.assertEqual(data[78:78 + len(fake_ids)], fake_ids)
        self.assertEqual(auto.parse_lnk(data)["target"], r"C:\p\pythonw.exe")

    @unittest.skipUnless(os.name == "nt", "the shell's item ID list comes from Windows")
    def test_windows_describes_an_existing_file(self):
        ids = auto.shell_id_list(sys.executable)
        self.assertIsInstance(ids, bytes)
        self.assertGreater(len(ids), 20)
        self.assertEqual(ids[-2:], b"\x00\x00")
        self.assertIsNone(auto.shell_id_list(str(ROOT / "no such file.exe")))

    def test_junk_is_not_a_shortcut(self):
        for junk in (b"", b"hello" * 30, b"\x4c\x00\x00\x00" + bytes(16) + bytes(60)):
            with self.assertRaises(ValueError):
                auto.parse_lnk(junk)
        good = auto.build_lnk(r"C:\p\pythonw.exe", "run", "", "Meridian")
        with self.assertRaises(ValueError):
            auto.parse_lnk(good[:90])                                      # cut short

    def test_server_options_allowlist(self):
        for ok in ([], ["--only", "flights,ships"], ["--only=alerts"], ["--no-datasets", "--no-tasks", "--debug", "--open"]):
            self.assertIsNone(auto.check_server_args(ok), ok)
        for bad in (["--host", "0.0.0.0"], ["--host=0.0.0.0"], ["--build", "all"], ["--only"], ["--only", "a;b"],
                    ["--port", "80"], ["anything"]):
            self.assertIsNotNone(auto.check_server_args(bad), bad)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.startup = self.base / "Startup"
        self.startup.mkdir()
        self.python = self.base / "py" / "pythonw.exe"
        self.python.parent.mkdir()
        self.python.write_bytes(b"MZ")
        self.data = self.base / "data"
        self.env = mock.patch.dict(os.environ, {"APPDATA": str(self.base / "appdata"), "MERIDIAN_ENV_FILE": "none",
                                                "MERIDIAN_DATA_DIR": str(self.data), "MERIDIAN_PORT": "1"})
        self.env.start()
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        self.env.stop()
        self.tmp.cleanup()

    def run_tool(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(oc, "probe_meridian", return_value=None):
            code = auto.main([str(a) for a in argv])
        return code, out.getvalue(), err.getvalue()

    def install(self, *extra):
        return self.run_tool("install", "--startup-dir", self.startup, "--python", self.python, *extra)

    @property
    def link(self) -> Path:
        return self.startup / "Meridian.lnk"


class InstallTests(Base):
    def test_install_writes_one_shortcut_and_remove_takes_it_away(self):
        code, out, err = self.install("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("Dry run", out)
        self.assertEqual(list(self.startup.iterdir()), [])
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual([p.name for p in self.startup.iterdir()], ["Meridian.lnk"])
        info = auto.parse_lnk(self.link.read_bytes())
        self.assertEqual(info["target"], str(self.python))
        self.assertEqual(info["working_dir"], str(ROOT))
        self.assertTrue(info["description"].startswith("Meridian"))
        self.assertIn(str(auto.SCRIPT), info["arguments"])
        self.assertIn(" run --delay 20", info["arguments"])
        self.assertEqual(info["show"], 7)
        if os.name == "nt":                                             # Windows can resolve it
            self.assertTrue(struct.unpack_from("<I", self.link.read_bytes(), 20)[0] & auto.HAS_ID_LIST)
        self.assertIn("Undo with: python tools\\autostart.py remove", out)
        code, out, _ = self.run_tool("remove", "--startup-dir", self.startup, "--dry-run")
        self.assertTrue(self.link.exists())
        code, out, _ = self.run_tool("remove", "--startup-dir", self.startup)
        self.assertEqual(code, 0)
        self.assertEqual(list(self.startup.iterdir()), [])
        code, out, _ = self.run_tool("remove", "--startup-dir", self.startup)
        self.assertEqual(code, 0)
        self.assertIn("not installed", out)

    def test_options_are_baked_into_the_shortcut(self):
        env_file = self.base / "keys.env"
        env_file.write_text("X=1\n")
        code, _out, err = self.install("--port", 8780, "--delay", 45, "--data-dir", self.base / "d d", "--env-file", env_file,
                                       "--", "--only", "flights,ships", "--no-datasets")
        self.assertEqual(code, 0, err)
        args = auto.parse_lnk(self.link.read_bytes())["arguments"]
        for part in ("--delay 45", "--port 8780", f'--data-dir "{self.base / "d d"}"', f"--env-file {env_file}",
                     "-- --only flights,ships --no-datasets"):
            self.assertIn(part, args)
        self.assertEqual(auto.link_port({"arguments": args}), 8780)
        self.assertEqual(auto.link_data_dir({"arguments": args}), self.base / "d d")
        code, _out, err = self.install("--env-file", self.base / "missing.env")
        self.assertEqual(code, 1)
        self.assertIn("no settings file", err)

    def test_reinstall_updates_but_never_replaces_a_foreign_shortcut(self):
        self.install()
        code, out, _ = self.install("--delay", 5)
        self.assertEqual(code, 0)
        self.assertIn("Updated", out)
        self.assertIn("--delay 5", auto.parse_lnk(self.link.read_bytes())["arguments"])
        foreign = auto.build_lnk(r"C:\Program Files\Other\other.exe", "--start", "", "Someone else's shortcut")
        self.link.write_bytes(foreign)
        code, _out, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("not made by this tool", err)
        self.assertEqual(self.link.read_bytes(), foreign)
        code, _out, err = self.run_tool("remove", "--startup-dir", self.startup)
        self.assertEqual(code, 1)
        self.assertEqual(self.link.read_bytes(), foreign)

    def test_refusals(self):
        code, _out, err = self.install("--", "--host", "0.0.0.0")
        self.assertEqual(code, 2)
        self.assertIn("not accepted", err)
        self.assertFalse(self.link.exists())
        code, _out, _err = self.run_tool("status", "--startup-dir", self.startup, "--", "--debug")
        self.assertEqual(code, 2)
        code, _out, err = self.run_tool("install", "--startup-dir", self.base / "nope", "--python", self.python)
        self.assertEqual(code, 1)
        code, _out, err = self.run_tool("install", "--startup-dir", self.startup, "--python", self.base / "none.exe")
        self.assertEqual(code, 1)
        self.assertIn("pythonw.exe", err)
        for argv in (["install", "--delay", "-1"], ["install", "--port", "70000"]):
            code, _o, _e = self.run_tool(*argv, "--startup-dir", self.startup, "--python", self.python)
            self.assertEqual(code, 2, argv)

    def test_default_folder_is_the_startup_folder_of_appdata(self):
        if os.name != "nt":
            self.assertIsNone(auto.startup_dir(None))
            return
        self.assertEqual(auto.startup_dir(None),
                         self.base / "appdata" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")

    def test_status(self):
        code, out, _ = self.run_tool("status", "--startup-dir", self.startup)
        self.assertEqual(code, 0)
        self.assertIn("not installed", out)
        self.assertIn("127.0.0.1:1: not answering", out)
        self.install("--port", 8899)
        (self.data / "logs").mkdir(parents=True)
        (self.data / "logs" / "autostart.log").write_text("line one\nline two\n", encoding="utf-8")
        out_io = io.StringIO()
        with contextlib.redirect_stdout(out_io), mock.patch.object(
                oc, "probe_meridian", return_value={"meridian": True, "version": "0.2.0", "started_at": 0}) as probe:
            auto.main(["status", "--startup-dir", str(self.startup)])
        out = out_io.getvalue()
        probe.assert_called_with(8899)                                  # the shortcut's port
        self.assertIn("Auto-start: installed", out)
        self.assertIn("running (version 0.2.0", out)
        self.assertIn("line two", out)


class FakeServer(types.ModuleType):
    def __init__(self, behaviour):
        super().__init__("server")
        self.behaviour = behaviour
        self.calls = []

    def main(self, argv):
        self.calls.append(list(argv))
        return self.behaviour(argv)


class LauncherTests(Base):
    def launch(self, behaviour, *argv, probe=None):
        fake = FakeServer(behaviour)
        before = sys.stdout, sys.stderr
        with mock.patch.dict(sys.modules, {"server": fake}), \
                mock.patch.object(oc, "probe_meridian", return_value=probe), \
                mock.patch.dict(os.environ, {}), mock.patch.object(sys, "path", list(sys.path)):
            code = auto.main(["run", "--delay", "0", "--data-dir", str(self.data), *map(str, argv)])
            if fake.calls:                                              # the server sees a plain import path
                self.assertEqual(Path(sys.path[0]), ROOT)
                self.assertNotIn(ROOT / "tools", [Path(p).resolve() for p in sys.path if p])
        self.assertEqual((sys.stdout, sys.stderr), before)              # the console streams come back
        log = (self.data / "logs" / "autostart.log").read_text(encoding="utf-8")
        return code, fake, log

    def test_runs_the_server_and_keeps_a_short_log(self):
        def server(argv):
            print("2026-11-23 08:00:00 INFO    meridian: Meridian 0.2.0 listening on http://127.0.0.1:8847/", file=sys.stderr)
            print("2026-11-23 08:00:01 WARNING meridian.flights: adsb.fi is not answering", file=sys.stderr)
            print("Can't listen on 127.0.0.1:8847. Is Meridian already running?", file=sys.stderr)
            return 2
        code, fake, log = self.launch(server, "--port", 8847, "--", "--only", "alerts", "--no-datasets")
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [["--port", "8847", "--only", "alerts", "--no-datasets"]])
        self.assertIn("Auto-start: Meridian starts in 0 s on 127.0.0.1:8847", log)
        self.assertIn("WARNING meridian.flights", log)
        self.assertIn("Is Meridian already running?", log)
        self.assertNotIn("listening on", log)                           # routine lines stay in meridian.log
        self.assertIn("Meridian stopped (exit code 2).", log)
        self.assertEqual(os.environ.get("MERIDIAN_DATA_DIR"), str(self.data))

    def test_does_not_start_a_second_copy(self):
        code, fake, log = self.launch(lambda a: 0, probe={"meridian": True})
        self.assertEqual((code, fake.calls), (0, []))
        self.assertIn("already running", log)
        code, fake, log = self.launch(lambda a: 0, probe={"meridian": False})
        self.assertEqual((code, fake.calls), (2, []))
        self.assertIn("Another program is using port", log)

    def test_a_crash_is_recorded(self):
        def server(argv):
            raise RuntimeError("database is locked")
        code, _fake, log = self.launch(server)
        self.assertEqual(code, 1)
        self.assertIn("Meridian stopped with an error", log)
        self.assertIn("RuntimeError: database is locked", log)

    def test_refused_server_options_never_start(self):
        code, fake, log = self.launch(lambda a: 0, "--", "--host", "0.0.0.0")
        self.assertEqual((code, fake.calls), (2, []))
        self.assertIn("Not starting", log)

    def test_log_rotates(self):
        path = self.base / "rot.log"
        log = auto.LaunchLog(path, max_bytes=200)
        for i in range(20):
            log.note(f"message {i:02d} " + "x" * 20)
        log.close()
        self.assertTrue(path.with_name("rot.log.1").exists())
        self.assertLess(path.stat().st_size, 400)
        self.assertIn("message 19", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
