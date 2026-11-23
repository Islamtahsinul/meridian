"""Shared helpers for the operations tools (autostart, backup, profile, housekeeping). Not a command.

Everything here is standard library. Paths and settings are resolved the way ``server.py`` resolves them:
``MERIDIAN_ENV_FILE`` (``none`` for no .env, or a path), then ``MERIDIAN_DATA_DIR`` and ``MERIDIAN_PORT``
from the environment or the .env file. Network checks only ever connect to 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PORT = 8790
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DIRECTORY = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)


# ---------------------------------------------------------------------------------------------
# Settings and paths
# ---------------------------------------------------------------------------------------------
def load_config():
    """The server's Config: honours MERIDIAN_ENV_FILE exactly as server.py does."""
    from meridian.config import Config
    choice = os.environ.get("MERIDIAN_ENV_FILE")
    if choice is not None and choice.strip().lower() in ("", "none", "off"):
        return Config(env_file=None)
    if choice:
        return Config(env_file=Path(choice))
    return Config()


def data_dir(config, override: str | os.PathLike | None = None) -> Path:
    """``--data-dir`` when given, otherwise the server's data folder (MERIDIAN_DATA_DIR or data/)."""
    if override:
        return Path(override).expanduser().resolve()
    return Path(config.data_dir)


def configured_port(config, override: int | None = None) -> int:
    if override is not None:
        return int(override)
    return config.get_int("MERIDIAN_PORT", DEFAULT_PORT)


def app_version() -> str:
    try:
        from meridian import __version__
        return str(__version__)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------------------------
# Is Meridian running?
# ---------------------------------------------------------------------------------------------
def port_open(port: int, timeout: float = 1.0) -> bool:
    """True when something accepts TCP connections on 127.0.0.1:port."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def probe_meridian(port: int, timeout: float = 2.0) -> dict | None:
    """What answers on 127.0.0.1:port: None when nothing listens; ``{"meridian": True, "version",
    "started_at"}`` for Meridian; ``{"meridian": False}`` for anything else. Never uses a proxy."""
    if not port_open(port, timeout=min(timeout, 1.0)):
        return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{int(port)}/api/core/manifest", timeout=timeout) as resp:
            body = json.loads(resp.read(2_000_000) or b"{}")
        app = body.get("app") if isinstance(body, dict) else None
        if isinstance(app, dict) and app.get("name") == "Meridian":
            return {"meridian": True, "version": app.get("version"), "started_at": app.get("started_at")}
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return {"meridian": False}


def file_in_use(path: str | os.PathLike) -> bool | None:
    """True when another handle has the file open. On Windows an exclusive open (share mode 0) fails with
    a sharing violation while SQLite, a log handler or anything else holds the file; the check changes
    nothing. False when the file is free or missing; None when it cannot tell (not Windows)."""
    p = Path(path)
    if not p.exists():
        return False
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                       wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = create(str(p), 0x80000000, 0, None, 3, 0x80, None)      # GENERIC_READ, no sharing, OPEN_EXISTING
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return True if ctypes.get_last_error() in (32, 33) else None  # sharing or lock violation
    k32.CloseHandle(handle)
    return False


# ---------------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------------
def human_size(n: float) -> str:
    """1536 -> '1.5 KB' (binary units, as Windows Explorer shows them)."""
    n = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{int(n):,} bytes" if n != 1 else "1 byte"
            return f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:,.1f} TB"


def utc_stamp(t: float | None = None) -> str:
    """'20261123T170512Z'."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if t is None else t))


def local_time(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def free_space(path: str | os.PathLike) -> int:
    """Free bytes on the drive holding ``path`` (or its nearest existing parent)."""
    p = Path(path).resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def atomic_write(path: str | os.PathLike, data: bytes) -> None:
    """Write ``data`` to a temporary file next to ``path``, flush it to disk, then rename it into place."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()


def is_link_like(path: str | os.PathLike) -> bool:
    """A symlink, junction or other reparse point: never followed by these tools."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def entry_is_link(entry: os.DirEntry, st: os.stat_result) -> bool:
    return entry.is_symlink() or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def tree_stats(path: str | os.PathLike) -> tuple[int, int, float]:
    """(bytes, files, newest file mtime) of a folder, without following links."""
    total = files = 0
    newest = 0.0
    stack = [os.fspath(path)]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if entry_is_link(e, st):
                continue
            if e.is_dir(follow_symlinks=False):
                stack.append(e.path)
            else:
                total += st.st_size
                files += 1
                newest = max(newest, st.st_mtime)
    return total, files, newest


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)          # a read-only file on Windows
        os.unlink(path)


def _remove_link(path: str) -> None:
    st = os.lstat(path)
    is_dir = stat.S_ISDIR(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _DIRECTORY)
    if is_dir:
        os.rmdir(path)                         # removes the junction or link itself, never its target
    else:
        os.unlink(path)


def safe_rmtree(path: str | os.PathLike) -> None:
    """Delete a folder tree. Links and junctions inside are removed themselves; what they point to is
    never touched. Raises OSError (for example a file in use) and leaves the rest in place."""
    p = os.fspath(path)
    if is_link_like(p):
        _remove_link(p)
        return
    with os.scandir(p) as it:
        entries = list(it)
    for e in entries:
        if is_link_like(e.path):
            _remove_link(e.path)
        elif e.is_dir(follow_symlinks=False):
            safe_rmtree(e.path)
        else:
            _unlink(e.path)
    os.rmdir(p)


def inside(path: str | os.PathLike, root: str | os.PathLike) -> bool:
    """True when ``path`` resolves to ``root`` or somewhere under it."""
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


# ---------------------------------------------------------------------------------------------
# Talking to the user
# ---------------------------------------------------------------------------------------------
def confirm(question: str, *, assume_yes: bool = False, input_fn: Callable[[str], str] | None = None) -> bool:
    """Ask for an explicit "yes". ``assume_yes`` (a --yes flag) skips the question; no answer (for
    example no keyboard in a scheduled task) is a no."""
    if assume_yes:
        return True
    try:
        answer = (input_fn or input)(f"{question} Type yes to continue: ")
    except (EOFError, KeyboardInterrupt, OSError, RuntimeError):
        print()
        return False
    return answer.strip().lower() == "yes"


def error(message: str) -> int:
    """Print a problem in plain words and return the exit code for it (1)."""
    print(message, file=sys.stderr)
    return 1
