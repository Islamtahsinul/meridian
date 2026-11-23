"""Start Meridian when you log on to Windows, with no administrator rights and no console window.

    python tools/autostart.py install [--port N] [--delay S] [--data-dir DIR] [--dry-run] [-- SERVER OPTIONS]
    python tools/autostart.py status [--port N]
    python tools/autostart.py remove [--dry-run]

install puts one shortcut, Meridian.lnk, in your Startup folder
(%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup). At logon Windows opens it: pythonw.exe
(Python without a console window) runs this script's "run" command, which waits --delay seconds
(default 20, so logging on stays quick), checks that Meridian is not already running, and starts the
server. The server writes data/logs/meridian.log as usual; the launcher adds data/logs/autostart.log
(starts, stops, warnings and errors only, at most about 2 MB).

remove deletes that shortcut and nothing else: install changes nothing else on the computer. status says
whether the shortcut is there, what it runs and whether Meridian is answering. --startup-dir uses another
folder instead of the Startup folder (for testing). To stop a Meridian started this way, end pythonw.exe in
Task Manager (Details tab), or log off.

Server options after "--" are passed to server.py; only --only, --no-tasks, --no-datasets, --debug and
--open are accepted. Meridian listens on 127.0.0.1 only, whatever you pass. Exit codes: 0 done, 1 refused
or failed, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import faulthandler
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
import ops_common as oc  # noqa: E402

SCRIPT = Path(__file__).resolve()
LINK_NAME = "Meridian.lnk"
DESCRIPTION = "Meridian: starts the server at logon (tools\\autostart.py)"
DEFAULT_DELAY = 20.0
LOG_NAME = "autostart.log"
LOG_MAX = 1_000_000
SERVER_FLAGS = {"--no-tasks", "--no-datasets", "--debug", "--open"}
ONLY_RE = re.compile(r"^[a-z0-9_]+(,[a-z0-9_]+)*$")

# ---------------------------------------------------------------------------------------------
# Windows shortcuts (.lnk, [MS-SHLLINK]): just enough to write one and read it back
# ---------------------------------------------------------------------------------------------
LNK_CLSID = bytes.fromhex("0114020000000000c000000000000046")
HAS_ID_LIST, HAS_LINK_INFO, HAS_NAME, HAS_RELATIVE_PATH = 0x1, 0x2, 0x4, 0x8
HAS_WORKING_DIR, HAS_ARGUMENTS, HAS_ICON_LOCATION, IS_UNICODE, FORCE_NO_LINK_INFO = 0x10, 0x20, 0x40, 0x80, 0x100
SW_SHOWMINNOACTIVE = 7
DRIVE_FIXED = 3


def _ansi(text: str) -> bytes:
    try:
        return text.encode("mbcs")            # the Windows ANSI code page
    except (LookupError, UnicodeEncodeError):
        return text.encode("cp1252", "replace")


def _volume(path: str) -> tuple[int, int]:
    """(drive type, serial number) of the drive holding ``path``; (fixed, 0) when unknown."""
    if os.name != "nt" or not re.match(r"^[A-Za-z]:\\", path):
        return DRIVE_FIXED, 0
    import ctypes
    root = path[:3]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kind = k32.GetDriveTypeW(root) or DRIVE_FIXED
    serial = ctypes.c_uint32(0)
    if not k32.GetVolumeInformationW(root, None, 0, ctypes.byref(serial), None, None, None, 0):
        serial.value = 0
    return int(kind), int(serial.value)


def shell_id_list(path: str) -> bytes | None:
    """The shell's item ID list for an existing ``path``, as Explorer stores it in a shortcut (Windows runs a
    shortcut through this list and ignores a bare path). From shell32 through ctypes; None elsewhere."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILGetSize.argtypes = [ctypes.c_void_p]
    shell32.ILGetSize.restype = wintypes.UINT
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.ILFree.restype = None
    pidl = shell32.ILCreateFromPathW(path)
    if not pidl:
        return None
    try:
        return ctypes.string_at(pidl, shell32.ILGetSize(pidl))    # the items and the 2-byte terminator
    finally:
        shell32.ILFree(pidl)


def build_lnk(target: str, arguments: str = "", working_dir: str = "", description: str = "",
              id_list: bytes | None = None) -> bytes:
    """A shell link to ``target`` (an absolute path): the item ID list when given (``shell_id_list``), a
    LinkInfo block with the path, Unicode strings and a minimised, non-activating show command."""
    flags = HAS_LINK_INFO | IS_UNICODE | (HAS_ID_LIST if id_list else 0)
    strings = b""
    for flag, text in ((HAS_NAME, description), (HAS_WORKING_DIR, working_dir), (HAS_ARGUMENTS, arguments)):
        if text:
            data = text.encode("utf-16-le")
            if len(data) // 2 > 0xFFFF:
                raise ValueError("shortcut text is too long")
            flags |= flag
            strings += struct.pack("<H", len(data) // 2) + data
    header = struct.pack("<I16sIIQQQIiIHHII", 0x4C, LNK_CLSID, flags, 0x20, 0, 0, 0, 0, 0, SW_SHOWMINNOACTIVE,
                         0, 0, 0, 0)
    kind, serial = _volume(target)
    volume = struct.pack("<IIII", 17, kind, serial, 0x10) + b"\x00"
    local_ansi, suffix_ansi = _ansi(target) + b"\x00", b"\x00"
    local_uni, suffix_uni = target.encode("utf-16-le") + b"\x00\x00", b"\x00\x00"
    head = 0x24
    vol_off = head
    local_off = vol_off + len(volume)
    suffix_off = local_off + len(local_ansi)
    pad = b"\x00" * ((suffix_off + len(suffix_ansi)) % 2)          # keep the UTF-16 strings 2-byte aligned
    local_uni_off = suffix_off + len(suffix_ansi) + len(pad)
    suffix_uni_off = local_uni_off + len(local_uni)
    size = suffix_uni_off + len(suffix_uni)
    link_info = struct.pack("<9I", size, head, 1, vol_off, local_off, 0, suffix_off, local_uni_off, suffix_uni_off) \
        + volume + local_ansi + suffix_ansi + pad + local_uni + suffix_uni
    ids = struct.pack("<H", len(id_list)) + id_list if id_list else b""
    return header + ids + link_info + strings + b"\x00\x00\x00\x00"


def _utf16z(data: bytes, pos: int) -> str:
    end = pos
    while end + 1 < len(data) and data[end:end + 2] != b"\x00\x00":
        end += 2
    return data[pos:end].decode("utf-16-le", "replace")


def _cstr(data: bytes, pos: int) -> str:
    end = data.index(b"\x00", pos)
    raw = data[pos:end]
    try:
        return raw.decode("mbcs")
    except LookupError:
        return raw.decode("cp1252", "replace")


def parse_lnk(data: bytes) -> dict:
    """``{target, arguments, working_dir, description, show}`` of a shell link. ValueError if it is not one."""
    if len(data) < 76 or data[:4] != b"\x4c\x00\x00\x00" or data[4:20] != LNK_CLSID:
        raise ValueError("not a Windows shortcut")
    try:
        flags = struct.unpack_from("<I", data, 20)[0]
        out: dict = {"target": None, "show": struct.unpack_from("<I", data, 60)[0]}
        pos = 76
        if flags & HAS_ID_LIST:
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        if flags & HAS_LINK_INFO:
            size, head, li_flags, _vol, local_off, _net, suffix_off = struct.unpack_from("<7I", data, pos)
            if li_flags & 1 and not flags & FORCE_NO_LINK_INFO:
                if head >= 0x24:
                    local_uni_off, suffix_uni_off = struct.unpack_from("<2I", data, pos + 28)
                    out["target"] = _utf16z(data, pos + local_uni_off) + _utf16z(data, pos + suffix_uni_off)
                else:
                    out["target"] = _cstr(data, pos + local_off) + _cstr(data, pos + suffix_off)
            pos += size
        for flag, key in ((HAS_NAME, "description"), (HAS_RELATIVE_PATH, "relative_path"),
                          (HAS_WORKING_DIR, "working_dir"), (HAS_ARGUMENTS, "arguments"),
                          (HAS_ICON_LOCATION, "icon")):
            if flags & flag:
                n = struct.unpack_from("<H", data, pos)[0]
                pos += 2
                if flags & IS_UNICODE:
                    out[key] = data[pos:pos + 2 * n].decode("utf-16-le", "replace")
                    pos += 2 * n
                else:
                    out[key] = data[pos:pos + n].decode("cp1252", "replace")
                    pos += n
    except (struct.error, ValueError, IndexError) as err:
        raise ValueError(f"a damaged shortcut ({err})") from None
    for key in ("arguments", "working_dir", "description"):
        out.setdefault(key, "")
    return out


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def startup_dir(override: str | None) -> Path | None:
    if override:
        return Path(override).expanduser().resolve()
    appdata = os.environ.get("APPDATA")
    if os.name != "nt" or not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def find_pythonw(explicit: str | None = None) -> Path | None:
    """pythonw.exe: --python, else next to the running Python (the Microsoft Store alias works), else PATH."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if os.path.lexists(p) else None
    exe = Path(sys.executable)
    for name in ("pythonw.exe", "pythonw"):
        cand = exe.with_name(name)
        if os.path.lexists(cand):
            return cand
    found = shutil.which("pythonw")
    return Path(found) if found else None


def read_link(path: Path) -> dict | None:
    try:
        return parse_lnk(path.read_bytes())
    except (OSError, ValueError):
        return None


def is_ours(info: dict | None) -> bool:
    """A shortcut this tool made: Meridian's description and the launcher in its arguments."""
    if not info:
        return False
    args = info.get("arguments") or ""
    return (info.get("description") or "").startswith("Meridian") and "autostart.py" in args and " run" in args


def check_server_args(extra: list[str]) -> str | None:
    """None when the options after "--" are allowed, otherwise what is wrong."""
    i = 0
    while i < len(extra):
        a = extra[i]
        if a in SERVER_FLAGS:
            i += 1
        elif a == "--only" and i + 1 < len(extra) and ONLY_RE.match(extra[i + 1]):
            i += 2
        elif a.startswith("--only=") and ONLY_RE.match(a[7:]):
            i += 1
        else:
            return (f'"{a}" is not accepted here. Server options allowed after "--": --only MODULES, '
                    f"{', '.join(sorted(SERVER_FLAGS))}. Use --port for the port.")
    return None


def link_port(info: dict | None) -> int | None:
    m = re.search(r"--port\s+(\d+)", (info or {}).get("arguments") or "")
    return int(m.group(1)) if m else None


def link_data_dir(info: dict | None) -> Path | None:
    m = re.search(r'--data-dir\s+(?:"([^"]+)"|(\S+))', (info or {}).get("arguments") or "")
    return Path(m.group(1) or m.group(2)) if m else None


def uptime_text(started_at) -> str:
    if not isinstance(started_at, (int, float)):
        return ""
    s = max(0, int(time.time() - started_at))
    if s < 3600:
        return f", up {s // 60} min"
    if s < 2 * 86400:
        return f", up {s // 3600} h {s % 3600 // 60} min"
    return f", up {s // 86400} days"


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------
def cmd_install(args, extra: list[str]) -> int:
    problem = check_server_args(extra)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    folder = startup_dir(args.startup_dir)
    if folder is None:
        return oc.error("Auto-start is for Windows. Use --startup-dir to write the shortcut somewhere else.")
    if not folder.is_dir():
        return oc.error(f"The folder {folder} does not exist.")
    pyw = find_pythonw(args.python)
    if pyw is None:
        return oc.error("pythonw.exe (Python without a console window) was not found next to this Python. "
                        "Pass its path with --python.")
    if args.env_file and args.env_file.strip().lower() not in ("none", "off") and not Path(args.env_file).is_file():
        return oc.error(f"There is no settings file at {args.env_file}.")
    lnk = folder / LINK_NAME
    existing = read_link(lnk) if lnk.exists() else None
    if lnk.exists() and not is_ours(existing):
        return oc.error(f"{lnk} exists and was not made by this tool. Rename or delete it first; nothing was changed.")
    run_args = [str(SCRIPT), "run", "--delay", f"{args.delay:g}"]
    if args.port is not None:
        run_args += ["--port", str(args.port)]
    if args.data_dir:
        run_args += ["--data-dir", str(Path(args.data_dir).expanduser().resolve())]
    if args.env_file:
        run_args += ["--env-file", args.env_file if args.env_file.lower() in ("none", "off")
                     else str(Path(args.env_file).resolve())]
    if extra:
        run_args += ["--", *extra]
    arguments = subprocess.list2cmdline(run_args)
    ids = shell_id_list(str(pyw))
    if ids is None and os.name == "nt":
        return oc.error(f"Windows could not describe {pyw} for a shortcut, so nothing was written. Pass another "
                        "pythonw.exe with --python.")
    data = build_lnk(str(pyw), arguments, str(oc.ROOT), DESCRIPTION, id_list=ids)
    action = "replace" if existing else "create"
    if args.dry_run:
        print(f"Dry run: would {action} {lnk}")
        print(f"  It would run: {pyw} {arguments}")
        print(f"  Starting in: {oc.ROOT}")
        return 0
    oc.atomic_write(lnk, data)
    print(f"{'Updated' if existing else 'Installed'} {lnk}.")
    print(f"  It runs: {pyw} {arguments}")
    print(f"Meridian will start about {args.delay:g} s after you log on, with no window. Logs: data\\logs\\meridian.log "
          f"and data\\logs\\{LOG_NAME}. Undo with: python tools\\autostart.py remove")
    return 0


def cmd_remove(args) -> int:
    folder = startup_dir(args.startup_dir)
    if folder is None:
        return oc.error("Auto-start is for Windows. Use --startup-dir to look somewhere else.")
    lnk = folder / LINK_NAME
    if not lnk.exists():
        print(f"Auto-start is not installed (no {LINK_NAME} in {folder}). Nothing to remove.")
        return 0
    if not is_ours(read_link(lnk)):
        return oc.error(f"{lnk} was not made by this tool, so it was left alone.")
    if args.dry_run:
        print(f"Dry run: would delete {lnk}.")
        return 0
    lnk.unlink()
    print(f"Removed {lnk}. Meridian will no longer start at logon. If it is running now, it keeps running until "
          "you end pythonw.exe in Task Manager or log off.")
    return 0


def cmd_status(args) -> int:
    folder = startup_dir(args.startup_dir)
    config = oc.load_config()
    info = None
    if folder is None:
        print("Auto-start: not available (this is not Windows).")
    else:
        lnk = folder / LINK_NAME
        info = read_link(lnk) if lnk.exists() else None
        if not lnk.exists():
            print(f"Auto-start: not installed (no {LINK_NAME} in {folder}). Install with: python tools\\autostart.py install")
        elif not is_ours(info):
            print(f"Auto-start: {lnk} exists but was not made by this tool.")
            info = None
        else:
            print(f"Auto-start: installed, {lnk}")
            print(f"  It runs: {info['target']} {info['arguments']}")
            print(f"  Starting in: {info['working_dir']}")
            if not info["target"] or not os.path.lexists(info["target"]):
                print("  Problem: the Python it points to is missing. Run install again.")
            if str(SCRIPT).lower() not in (info.get("arguments") or "").lower():
                print(f"  Problem: it starts a launcher elsewhere, not {SCRIPT}. Run install again from this copy.")
    pyw = find_pythonw(None)
    print(f"Python without a console (pythonw.exe): {pyw if pyw else 'not found'}")
    port = args.port if args.port is not None else (link_port(info) or oc.configured_port(config))
    who = oc.probe_meridian(port)
    if who is None:
        print(f"Meridian on 127.0.0.1:{port}: not answering.")
    elif who.get("meridian"):
        print(f"Meridian on 127.0.0.1:{port}: running (version {who.get('version')}{uptime_text(who.get('started_at'))}).")
    else:
        print(f"Meridian on 127.0.0.1:{port}: another program is using this port.")
    log = (link_data_dir(info) or oc.data_dir(config)) / "logs" / LOG_NAME
    if log.exists():
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        except OSError:
            lines = []
        print(f"Auto-start log ({log}), last lines:")
        for line in lines:
            print(f"  {line}")
    return 0


class LaunchLog:
    """The launcher's stdout and stderr: a file in data/logs that keeps the launcher's notes, warnings,
    errors and tracebacks, and drops the server's routine DEBUG and INFO console lines (meridian.log has
    them). Rotated once (autostart.log.1) past about 1 MB."""

    ROUTINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (DEBUG|INFO)\s")
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, path: Path, max_bytes: int = LOG_MAX):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._buf = ""
        if self.path.exists() and self.path.stat().st_size > max_bytes:
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        self.raw = open(self.path, "a", encoding="utf-8", errors="replace")

    def write(self, text) -> int:
        text = text if isinstance(text, str) else str(text)
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if not self.ROUTINE.match(line):
                    self._emit(line + "\n")
        return len(text)

    def _emit(self, line: str) -> None:
        try:
            if self.raw.tell() and self.raw.tell() + len(line.encode("utf-8", "replace")) > self.max_bytes:
                self.raw.close()                  # rotate first, so the newest lines are always in the log
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                self.raw = open(self.path, "a", encoding="utf-8", errors="replace")
                if faulthandler.is_enabled():
                    faulthandler.enable(self.raw)
            self.raw.write(line)
            self.raw.flush()
        except (OSError, ValueError):
            pass

    def note(self, message: str) -> None:
        self.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    def flush(self) -> None:
        with self._lock:
            try:
                self.raw.flush()
            except (OSError, ValueError):
                pass

    def fileno(self) -> int:
        return self.raw.fileno()

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        with self._lock:
            if self._buf and not self.ROUTINE.match(self._buf):
                self._emit(self._buf + "\n")
            self._buf = ""
            try:
                self.raw.close()
            except OSError:
                pass


def cmd_run(args, extra: list[str]) -> int:
    """What the shortcut starts (under pythonw.exe, so there is no console and sys.stdout is None)."""
    if args.env_file:
        os.environ["MERIDIAN_ENV_FILE"] = args.env_file
    if args.data_dir:
        os.environ["MERIDIAN_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    config = oc.load_config()
    logs = oc.data_dir(config) / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        log = LaunchLog(logs / LOG_NAME)
    except OSError:
        import tempfile
        log = LaunchLog(Path(tempfile.gettempdir()) / f"meridian-{LOG_NAME}")
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = log
    own_faulthandler = not faulthandler.is_enabled()
    if own_faulthandler:
        faulthandler.enable(log)                  # a hard crash still leaves a trace in the log
    try:
        return _launch(args, extra, config, log)
    finally:
        if own_faulthandler:
            faulthandler.disable()
        sys.stdout, sys.stderr = saved
        log.close()


def _launch(args, extra: list[str], config, log: LaunchLog) -> int:
    problem = check_server_args(extra)
    if problem:
        log.note(f"Not starting: {problem}")
        return 2
    port = oc.configured_port(config, args.port)
    log.note(f"Auto-start: Meridian starts in {args.delay:g} s on 127.0.0.1:{port} (data folder {oc.data_dir(config)}).")
    if args.delay > 0:
        time.sleep(args.delay)
    who = oc.probe_meridian(port)
    if who is not None:
        if who.get("meridian"):
            log.note(f"Meridian is already running on port {port}; not starting a second copy.")
            return 0
        log.note(f"Another program is using port {port}, so Meridian cannot start. Free the port or install with --port.")
        return 2
    server_args = (["--port", str(args.port)] if args.port is not None else []) + list(extra)
    try:
        os.chdir(oc.ROOT)
        # Run the server with the import path "python server.py" would have: tools/ (first when this script
        # runs) holds profile.py, which would hide the standard library's profile module.
        tools = SCRIPT.parent
        sys.path[:] = [p for p in sys.path if not p or Path(p).resolve() != tools]
        if str(oc.ROOT) in sys.path:
            sys.path.remove(str(oc.ROOT))
        sys.path.insert(0, str(oc.ROOT))
        import server
        rc = server.main(server_args)
    except SystemExit as err:
        rc = err.code if isinstance(err.code, int) else 1
    except BaseException:                          # anything at all: record it, the console is gone
        log.note("Meridian stopped with an error:")
        log.write(traceback.format_exc())
        rc = 1
    log.note(f"Meridian stopped (exit code {rc}).")
    return rc if isinstance(rc, int) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python tools/autostart.py", description=__doc__.split("\n\n")[0],
                                epilog="Run a command with --help for its options. More in docs/OPERATIONS.md.")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def folder(sp):
        sp.add_argument("--startup-dir", help="use this folder instead of your Startup folder (for testing)")

    i = sub.add_parser("install", help="start Meridian at logon (a shortcut in your Startup folder)")
    folder(i)
    i.add_argument("--port", type=int, help="port for this Meridian (default: MERIDIAN_PORT or 8790)")
    i.add_argument("--delay", type=float, default=DEFAULT_DELAY, metavar="S",
                   help=f"seconds to wait after logon (default {DEFAULT_DELAY:g})")
    i.add_argument("--data-dir", help="data folder for this Meridian (default: MERIDIAN_DATA_DIR or data/)")
    i.add_argument("--env-file", help='settings file to use, or "none" to run without keys (default: .env)')
    i.add_argument("--python", help="path to pythonw.exe (default: the one next to this Python)")
    i.add_argument("--dry-run", action="store_true", help="show the shortcut, write nothing")
    i.set_defaults(fn=cmd_install, takes_extra=True)

    r = sub.add_parser("remove", help="stop starting Meridian at logon (deletes the shortcut)")
    folder(r)
    r.add_argument("--dry-run", action="store_true", help="say what would be deleted, delete nothing")
    r.set_defaults(fn=cmd_remove, takes_extra=False)

    s = sub.add_parser("status", help="is auto-start installed, and is Meridian running?")
    folder(s)
    s.add_argument("--port", type=int, help="port to check (default: the shortcut's, else MERIDIAN_PORT or 8790)")
    s.set_defaults(fn=cmd_status, takes_extra=False)

    run = sub.add_parser("run", help="what the shortcut runs: wait, then start the server (logs to data/logs)")
    run.add_argument("--port", type=int, help="port (default: MERIDIAN_PORT or 8790)")
    run.add_argument("--delay", type=float, default=DEFAULT_DELAY, metavar="S", help="seconds to wait first")
    run.add_argument("--data-dir", help="data folder (sets MERIDIAN_DATA_DIR)")
    run.add_argument("--env-file", help='settings file, or "none" (sets MERIDIAN_ENV_FILE)')
    run.set_defaults(fn=cmd_run, takes_extra=True)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, extra = argv[:cut], argv[cut + 1:]
    args = build_parser().parse_args(argv)
    if extra and not args.takes_extra:
        print(f'{args.command} takes no server options after "--".', file=sys.stderr)
        return 2
    if getattr(args, "delay", 0) is not None and getattr(args, "delay", 0) < 0:
        print("--delay must not be negative.", file=sys.stderr)
        return 2
    if getattr(args, "port", None) is not None and not 1 <= args.port <= 65535:
        print("--port must be between 1 and 65535.", file=sys.stderr)
        return 2
    return args.fn(args, extra) if args.takes_extra else args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
