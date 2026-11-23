"""Back up and restore Meridian's database, safely while it runs.

    python tools/backup.py create [--dest DIR] [--keep N] [--include-env] [--dry-run]
    python tools/backup.py list [--dest DIR]
    python tools/backup.py verify [FILE | --all] [--dest DIR]
    python tools/backup.py restore FILE|latest [--port N] [--restore-env] [--yes] [--dry-run]
    python tools/backup.py prune [--keep N] [--dest DIR] [--dry-run]

A backup is one zip, meridian-backup-<UTC time>.zip, in data/backups unless --dest says otherwise. It holds:
- meridian.db: a consistent copy taken with SQLite's online backup API (Meridian can keep running), checked
  with PRAGMA integrity_check before it is zipped;
- settings.json: the non-secret settings from .env and the names (never the values) of the keys set there;
- manifest.json: when, what version, the database checksum and row counts;
- env/.env only with --include-env, after a warning and an explicit yes: the file then holds your keys.

Datasets (data/build, data/raw) are not backed up: Meridian rebuilds them in 5 to 10 minutes.
restore checks the backup; when there is a database to replace, it refuses while Meridian answers on its port
or anything has that database open, and keeps it beside the new one (meridian-before-restore-<time>.db). Only
then does it put the backup in place. Restoring into an empty folder (a practice restore) needs no stop.
--restore-env writes the backup's .env as the .env in use only when there is none and the restore went into
the data folder Meridian uses; otherwise it saves it as .env in a new env-from-backup-<time> folder inside the
folder restored into. It never overwrites a file.
Exit codes: 0 done, 1 refused or failed, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
import ops_common as oc  # noqa: E402

FORMAT = "meridian-backup"
VERSION = 1
DB_NAME = "meridian.db"
DB_MEMBER = "meridian.db"
MANIFEST = "manifest.json"
SETTINGS = "settings.json"
ENV_MEMBER = "env/.env"
DEFAULT_KEEP = 7
NAME_RE = re.compile(r"^meridian-backup-(\d{8}T\d{6}Z)(?:-(\d+))?\.zip$")
CORE_TABLES = ("kv", "favourites", "casefiles", "casefile_items")
COUNTED = ("favourites", "casefiles", "casefile_items", "alerts_watchlists", "alerts_rules", "core_event_history")
MARGIN = 32 * 1024 * 1024
MANIFEST_MAX = 1_000_000
_CORE_SETTINGS = {"MERIDIAN_HOST", "MERIDIAN_PORT", "MERIDIAN_DATA_DIR", "MERIDIAN_HISTORY_DAYS"}
_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL", "AUTH")


class BackupError(Exception):
    """A problem with a plain message for the user."""


# ---------------------------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------------------------
def default_dest(ddir: Path) -> Path:
    return ddir / "backups"


def sidecars(db: Path) -> list[Path]:
    return [db.with_name(db.name + suffix) for suffix in ("-wal", "-shm", "-journal")]


def remove_db_files(db: Path) -> None:
    for p in [db, *sidecars(db)]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def integrity(db: Path, quick: bool = False) -> str:
    """'ok' or the first problems SQLite reports."""
    try:
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute("PRAGMA quick_check" if quick else "PRAGMA integrity_check").fetchmany(5)
        finally:
            con.close()
    except sqlite3.DatabaseError as err:
        return f"not a readable database ({err})"
    text = "; ".join(str(r[0]) for r in rows)
    return "ok" if text == "ok" else text


def table_names(db: Path) -> set[str]:
    con = sqlite3.connect(str(db))
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        con.close()


def snapshot(src: Path, dst: Path) -> dict:
    """A consistent copy of ``src`` in ``dst`` through the online backup API, in rollback-journal mode
    (one self-contained file), checked with integrity_check. Returns what the manifest records."""
    remove_db_files(dst)
    source = sqlite3.connect(str(src), timeout=30)
    try:
        source.execute("PRAGMA query_only = 1")
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)                       # one step: a single consistent read transaction
            target.execute("PRAGMA journal_mode = DELETE")
            check = target.execute("PRAGMA integrity_check").fetchmany(5)
            tables = {r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            counts = {t: target.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in COUNTED if t in tables}
            page_size = target.execute("PRAGMA page_size").fetchone()[0]
            pages = target.execute("PRAGMA page_count").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()
    text = "; ".join(str(r[0]) for r in check)
    if text != "ok":
        raise BackupError(f"The copy failed SQLite's integrity check ({text}). The database itself may be damaged; "
                          "nothing was saved.")
    return {"integrity": "ok", "tables": len(tables), "counts": counts, "page_size": page_size, "pages": pages}


def _looks_secret(name: str, value: str) -> bool:
    upper = name.upper()
    return any(w in upper for w in _SECRET_WORDS) or bool(re.search(r"://[^/\s]*@", value or ""))


def known_settings() -> tuple[set[str], set[str]]:
    """(non-secret names, secret names) as .env.example lists them (tools/gen_docs.py writes it from what
    every module declares: keys as ``NAME=``, other settings commented out as ``# NAME=``)."""
    plain, secret = set(_CORE_SETTINGS), set()
    try:
        text = (oc.ROOT / ".env.example").read_text(encoding="utf-8")
    except OSError:
        return plain, secret
    for line in text.splitlines():
        m = re.match(r"^#\s*([A-Z][A-Z0-9_]*)=", line)
        if m:
            plain.add(m.group(1))
            continue
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            secret.add(m.group(1))
    return plain - secret, secret


def settings_summary(config) -> dict:
    """Non-secret settings from the .env in use, and the names of everything else set there. Values of
    keys, and of anything not known to be harmless, are never recorded."""
    from meridian.config import parse_env_file
    env = config.env_file
    values = parse_env_file(Path(env)) if env else {}
    plain, _ = known_settings()
    kept = {k: v for k, v in sorted(values.items()) if v and k in plain and not _looks_secret(k, v)}
    return {"env_file": Path(env).name if env else None, "settings": kept,
            "keys_set": sorted(k for k, v in values.items() if v and k not in kept)}


def backups_in(dest: Path) -> list[Path]:
    """Backups in ``dest``, newest first (by the time in the name)."""
    if not dest.is_dir():
        return []
    found = []
    for p in dest.iterdir():
        m = NAME_RE.match(p.name)
        if m and p.is_file():
            found.append(((m.group(1), int(m.group(2) or 1)), p))
    return [p for _key, p in sorted(found, reverse=True)]


def read_manifest(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo(MANIFEST)
            if info.file_size > MANIFEST_MAX:
                raise BackupError(f"{path.name} has an oversized manifest.")
            manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
            manifest["_members"] = zf.namelist()
    except KeyError:
        raise BackupError(f"{path.name} is not a Meridian backup (no manifest).") from None
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, ValueError, EOFError, zlib.error) as err:
        raise BackupError(f"{path.name} cannot be read ({err}).") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError(f"{path.name} is not a Meridian backup.")
    version = manifest.get("version")
    if not isinstance(version, int) or version < 1:
        raise BackupError(f"{path.name} has an unknown backup format version.")
    if version > VERSION:
        raise BackupError(f"{path.name} was made by a newer Meridian (backup format {version}). Update Meridian first.")
    db = manifest.get("database")
    if not isinstance(db, dict) or not isinstance(db.get("sha256"), str) or not isinstance(db.get("bytes"), int):
        raise BackupError(f"{path.name} has a damaged manifest.")
    return manifest


def check_backup(path: Path, extract_to: Path) -> dict:
    """Full check of a backup: zip checksums, the database's SHA-256 and SQLite's integrity check. The
    database is left in ``extract_to`` (the caller removes it). Raises BackupError."""
    manifest = read_manifest(path)
    expected = manifest["database"]
    try:
        with zipfile.ZipFile(path) as zf:
            if DB_MEMBER not in zf.namelist():
                raise BackupError(f"{path.name} has no database in it.")
            info = zf.getinfo(DB_MEMBER)
            if info.file_size != expected["bytes"]:
                raise BackupError(f"{path.name} is damaged: the database size does not match the manifest.")
            need = info.file_size + MARGIN
            if oc.free_space(extract_to.parent) < need:
                raise BackupError(f"Not enough free space to check {path.name}: about {oc.human_size(need)} is "
                                  f"needed in {extract_to.parent}.")
            remove_db_files(extract_to)
            h = hashlib.sha256()
            with zf.open(info) as src, open(extract_to, "wb") as out:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    h.update(chunk)
                    out.write(chunk)
    except (zipfile.BadZipFile, OSError, EOFError, zlib.error) as err:
        raise BackupError(f"{path.name} is damaged ({err}).") from None
    if h.hexdigest() != expected["sha256"]:
        raise BackupError(f"{path.name} is damaged: the database does not match its checksum.")
    result = integrity(extract_to)
    if result != "ok":
        raise BackupError(f"The database in {path.name} failed SQLite's integrity check ({result}).")
    missing = [t for t in CORE_TABLES if t not in table_names(extract_to)]
    if missing:
        raise BackupError(f"The database in {path.name} is missing Meridian's tables ({', '.join(missing)}).")
    return manifest


def describe(manifest: dict) -> str:
    counts = manifest.get("database", {}).get("counts", {})
    parts = [f"database {oc.human_size(manifest.get('database', {}).get('bytes', 0))}"]
    for key, one, many in (("favourites", "favourite", "favourites"), ("casefiles", "case file", "case files"),
                           ("alerts_watchlists", "watchlist", "watchlists")):
        if key in counts:
            n = counts[key]
            parts.append(f"{n:,} {one if n == 1 else many}")
    if manifest.get("env_included"):
        parts.append(".env included")
    return ", ".join(parts)


def created_text(manifest: dict) -> str:
    t = manifest.get("created_ts")
    return oc.local_time(t) if isinstance(t, (int, float)) else str(manifest.get("created_at", "?"))


def resolve_backup(name: str, dest: Path) -> Path:
    if name == "latest":
        found = backups_in(dest)
        if not found:
            raise BackupError(f"There are no backups in {dest}.")
        return found[0]
    p = Path(name)
    if p.is_file():
        return p
    if (dest / name).is_file():
        return dest / name
    raise BackupError(f"No backup called {name} (looked in the current folder and {dest}).")


def prune_list(dest: Path, keep: int) -> list[Path]:
    return backups_in(dest)[keep:]


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------
def cmd_create(args) -> int:
    config = oc.load_config()
    ddir = oc.data_dir(config, args.data_dir)
    db = ddir / DB_NAME
    if not db.is_file():
        return oc.error(f"There is no database at {db}. Has Meridian run here yet?")
    dest = Path(args.dest).expanduser().resolve() if args.dest else default_dest(ddir)
    env = Path(config.env_file) if config.env_file else None
    if args.include_env:
        if env is None or not env.is_file():
            return oc.error("There is no .env file in use, so there is nothing to include.")
        print("Warning: with --include-env the backup holds your .env file, with your API keys in plain text. "
              "Anyone who gets the file can use your keys. Keep it out of cloud-synced folders and email, "
              "and delete it when you no longer need it.")
        if not args.dry_run and not oc.confirm("Include .env in this backup?", assume_yes=args.yes):
            print("Nothing was saved.")
            return 1
    size = db.stat().st_size + sum(p.stat().st_size for p in sidecars(db) if p.exists())
    need = 2 * size + MARGIN
    free = oc.free_space(dest)
    stamp = oc.utc_stamp()
    name = f"meridian-backup-{stamp}.zip"
    n = 2
    while (dest / name).exists():
        name = f"meridian-backup-{stamp}-{n}.zip"
        n += 1
    final = dest / name
    if free < need:
        return oc.error(f"Not enough free space in {dest}: {oc.human_size(free)} free, about {oc.human_size(need)} "
                        "needed. Free some space (python tools/housekeeping.py) or use --dest on another drive.")
    if args.dry_run:
        print(f"Dry run: would back up {db} ({oc.human_size(size)}) to {final}"
              f"{' with .env' if args.include_env else ', without .env'}. Free space there: {oc.human_size(free)}.")
        if args.keep:
            for p in prune_list(dest, max(0, args.keep - 1)):
                print(f"Dry run: would then delete the older backup {p.name}.")
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    tmp_db = dest / f".{name}.db.tmp"
    partial = dest / f"{name}.partial"
    try:
        started = time.monotonic()
        info = snapshot(db, tmp_db)
        digest = sha256_file(tmp_db)
        now = time.time()
        manifest = {
            "format": FORMAT, "version": VERSION, "app_version": oc.app_version(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "created_ts": round(now, 3),
            "database": {"file": DB_MEMBER, "bytes": tmp_db.stat().st_size, "sha256": digest, **info},
            "settings": SETTINGS, "env_included": bool(args.include_env),
            "note": "Datasets are not included; Meridian rebuilds them after a restore.",
        }
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr(MANIFEST, json.dumps(manifest, indent=2))
            zf.write(tmp_db, DB_MEMBER)
            zf.writestr(SETTINGS, json.dumps(settings_summary(config), indent=2))
            if args.include_env:
                zf.writestr(ENV_MEMBER, env.read_bytes())
        try:
            with zipfile.ZipFile(partial) as zf:
                bad = zf.testzip()
        except (zipfile.BadZipFile, EOFError, zlib.error) as err:
            bad = str(err)
        if bad is not None:
            raise BackupError(f"The new backup could not be read back ({bad}); nothing was saved.")
        with open(partial, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(partial, final)
    except BackupError as err:
        return oc.error(str(err))
    except (OSError, sqlite3.Error) as err:
        return oc.error(f"The backup failed: {err}. Nothing was saved.")
    finally:
        remove_db_files(tmp_db)
        if partial.exists():
            partial.unlink()
    print(f"Backed up {db} to {final} ({oc.human_size(final.stat().st_size)}; {describe(manifest)}; "
          f"{time.monotonic() - started:.1f} s).")
    if args.keep:
        for p in prune_list(dest, args.keep):
            p.unlink()
            print(f"Deleted the older backup {p.name}.")
    return 0


def cmd_list(args) -> int:
    config = oc.load_config()
    dest = Path(args.dest).expanduser().resolve() if args.dest else default_dest(oc.data_dir(config, args.data_dir))
    found = backups_in(dest)
    if not found:
        print(f"There are no backups in {dest}. Make one with: python tools/backup.py create")
        return 0
    total = sum(p.stat().st_size for p in found)
    print(f"{len(found)} backup{'s' if len(found) != 1 else ''} in {dest} ({oc.human_size(total)} in all), newest first:")
    for p in found:
        try:
            m = read_manifest(p)
            print(f"  {p.name}  {created_text(m)}  {oc.human_size(p.stat().st_size)}  {describe(m)}")
        except BackupError as err:
            print(f"  {p.name}  unreadable: {err}")
    print("Check one with: python tools/backup.py verify NAME")
    return 0


def cmd_verify(args) -> int:
    config = oc.load_config()
    dest = Path(args.dest).expanduser().resolve() if args.dest else default_dest(oc.data_dir(config, args.data_dir))
    try:
        targets = backups_in(dest) if args.all else [resolve_backup(args.file or "latest", dest)]
    except BackupError as err:
        return oc.error(str(err))
    if not targets:
        return oc.error(f"There are no backups in {dest}.")
    failed = 0
    for path in targets:
        tmp = path.parent / f".verify-{os.getpid()}-{path.stem}.db.tmp"
        try:
            m = check_backup(path, tmp)
            print(f"{path.name}: good. Made {created_text(m)} by Meridian {m.get('app_version', '?')}; {describe(m)}.")
        except BackupError as err:
            failed += 1
            print(f"{path.name}: {err}", file=sys.stderr)
        finally:
            remove_db_files(tmp)
    return 1 if failed else 0


def cmd_prune(args) -> int:
    config = oc.load_config()
    dest = Path(args.dest).expanduser().resolve() if args.dest else default_dest(oc.data_dir(config, args.data_dir))
    found = backups_in(dest)
    doomed = found[args.keep:]
    if not doomed:
        print(f"Nothing to prune: {len(found)} backup{'s' if len(found) != 1 else ''} in {dest}, keeping up to {args.keep}.")
        return 0
    freed = 0
    for p in doomed:
        size = p.stat().st_size
        if args.dry_run:
            print(f"Dry run: would delete {p.name} ({oc.human_size(size)}).")
        else:
            p.unlink()
            print(f"Deleted {p.name} ({oc.human_size(size)}).")
        freed += size
    print(f"{'Would free' if args.dry_run else 'Freed'} {oc.human_size(freed)}; kept the newest {args.keep}.")
    return 0


def cmd_restore(args) -> int:
    config = oc.load_config()
    ddir = oc.data_dir(config, args.data_dir)
    dest = Path(args.dest).expanduser().resolve() if args.dest else default_dest(ddir)
    target = ddir / DB_NAME
    try:
        path = resolve_backup(args.file, dest)
    except BackupError as err:
        return oc.error(str(err))
    ddir.mkdir(parents=True, exist_ok=True)
    stamp = oc.utc_stamp()
    tmp = ddir / f".restore-{stamp}.db.tmp"
    try:
        print(f"Checking {path.name}...")
        try:
            manifest = check_backup(path, tmp)
        except BackupError as err:
            return oc.error(f"{err} Nothing was changed.")
        print(f"The backup is good: made {created_text(manifest)}; {describe(manifest)}.")
        current = [p for p in [target, *sidecars(target)] if p.exists()]
        if current:                                  # something to overwrite: is a Meridian using it?
            port = oc.configured_port(config, args.port)
            who = oc.probe_meridian(port)
            if who is not None:
                what = "Meridian" if who.get("meridian") else "Another program"
                return oc.error(f"{what} is answering on 127.0.0.1:{port}. Stop Meridian first (close its window, or "
                                f"end pythonw.exe in Task Manager if it starts at logon), then run restore again. "
                                f"Use --port if your Meridian uses another port. Nothing was changed.")
            if oc.file_in_use(target):
                return oc.error(f"{target} is open in another program (probably a Meridian on another port). Stop it, "
                                "then run restore again. Nothing was changed.")
        aside = ddir / f"meridian-before-restore-{stamp}.db"
        if current:
            print(f"This replaces {target} ({oc.human_size(sum(p.stat().st_size for p in current))}). The current "
                  f"database is kept as {aside.name}.")
        else:
            print(f"There is no database at {target} yet; the backup becomes it.")
        if args.dry_run:
            print("Dry run: nothing was changed.")
            return 0
        if not oc.confirm("Restore this backup?", assume_yes=args.yes):
            print("Nothing was changed.")
            return 1
        moved: list[tuple[Path, Path]] = []
        placed = False
        try:
            for p in current:
                dst = aside.with_name(aside.name + p.name[len(target.name):])
                os.replace(p, dst)                  # fails on Windows if anything still has it open
                moved.append((p, dst))
            os.replace(tmp, target)
            placed = True
            result = integrity(target, quick=True)
            if result != "ok":
                raise BackupError(f"the restored database did not pass a check ({result})")
        except (OSError, BackupError) as err:
            if placed:
                remove_db_files(target)
            for original, dst in reversed(moved):
                os.replace(dst, original)
            return oc.error(f"The restore failed ({err}). The database was left as it was.")
        print(f"Restored {path.name} into {target}.")
        if moved:
            print(f"The previous database is kept as {aside}. Delete it once you are happy with the restore.")
        env_ok = restore_env(args, config, ddir, path, stamp)
        print("Start Meridian again. It rebuilds any missing datasets in the background (5 to 10 minutes).")
        return 0 if env_ok else 1
    finally:
        remove_db_files(tmp)


def same_folder(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:                                  # one of them does not exist
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def write_new(path: Path, data: bytes) -> bool:
    """Write a file that does not exist yet (owner-only where the system has such modes). False, with
    nothing written, when something is already there: this never overwrites anything."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return True


def new_folder(parent: Path, name: str) -> Path:
    """A folder made now, so nothing in it can be overwritten: name, else name-2, name-3 and so on."""
    parent.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        folder = parent / (name if n == 1 else f"{name}-{n}")
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            continue
    raise FileExistsError(f"too many {name} folders in {parent}")


def restore_env(args, config, ddir: Path, path: Path, stamp: str) -> bool:
    """After a restore: name the keys the backup's .env had or, with --restore-env, give its .env back.
    The .env in use is written only when there is none yet and the restore went into the data folder
    Meridian uses. Otherwise the copy goes into a new folder, env-from-backup-<time>, in the data folder
    restored into (a practice restore keeps it in the practice folder). The copy is always named .env,
    so the repository's ignore rule for .env covers it wherever that folder is. Nothing is ever
    overwritten. False when the copy could not be written."""
    settings = {}
    try:
        with zipfile.ZipFile(path) as zf:
            if SETTINGS in zf.namelist():
                settings = json.loads(zf.read(SETTINGS).decode("utf-8"))
            env_bytes = zf.read(ENV_MEMBER) if ENV_MEMBER in zf.namelist() else None
    except (zipfile.BadZipFile, OSError, KeyError, ValueError, EOFError, zlib.error):
        env_bytes = None
    if not args.restore_env:
        keys = settings.get("keys_set") if isinstance(settings, dict) else None
        if keys:
            print(f"Keys are not restored. The backup's .env had: {', '.join(keys)}. Copy your .env by hand, or "
                  "use --restore-env with a backup made with --include-env.")
        return True
    if env_bytes is None:
        print("This backup has no .env (backups leave it out unless made with --include-env).")
        return True
    env = Path(config.env_file) if config.env_file else None
    if env is None:
        print("No .env is in use (MERIDIAN_ENV_FILE is none), so .env was not restored.")
        return True
    live = same_folder(ddir, config.data_dir)
    try:
        if live and write_new(env, env_bytes):
            print(f"Restored {env}.")
            return True
        folder = new_folder(ddir, f"env-from-backup-{stamp}")
        out = folder / ".env"
        try:
            write_new(out, env_bytes)
        except OSError:
            try:
                folder.rmdir()
            except OSError:
                pass
            raise
    except OSError as err:
        oc.error(f"The database was restored, but the backup's .env could not be saved ({err}).")
        return False
    if live:
        print(f"{env} already exists, so the backup's copy was saved as {out}. Compare the two and keep what you "
              "need, then delete the copy: it holds your keys.")
    else:
        print(f"This restore went into {ddir}, not the data folder Meridian uses, so {env} was left alone and the "
              f"backup's copy was saved as {out}. It holds your keys: delete it when you no longer need it.")
    return True


# ---------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python tools/backup.py", description=__doc__.split("\n\n")[0],
                                epilog="Run a command with --help for its options. More in docs/OPERATIONS.md.",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def common(sp, dest=True):
        sp.add_argument("--data-dir", help="Meridian's data folder (default: as the server uses, normally data/)")
        if dest:
            sp.add_argument("--dest", help="folder holding the backups (default: data/backups)")

    c = sub.add_parser("create", help="make a backup now (safe while Meridian runs)")
    common(c)
    c.add_argument("--keep", type=int, metavar="N", help="afterwards keep only the newest N backups")
    c.add_argument("--include-env", action="store_true", help="also store .env, with your keys (asks first)")
    c.add_argument("--yes", action="store_true", help="answer yes to the --include-env question")
    c.add_argument("--dry-run", action="store_true", help="say what would happen, change nothing")
    c.set_defaults(fn=cmd_create)

    ls = sub.add_parser("list", help="list the backups, newest first")
    common(ls)
    ls.set_defaults(fn=cmd_list)

    v = sub.add_parser("verify", help="check a backup's checksums and SQLite integrity")
    common(v)
    v.add_argument("file", nargs="?", help="a backup file or name (default: the newest)")
    v.add_argument("--all", action="store_true", help="check every backup")
    v.set_defaults(fn=cmd_verify)

    r = sub.add_parser("restore", help="replace the database with a backup (Meridian must be stopped)")
    common(r)
    r.add_argument("file", help='a backup file or name, or "latest"')
    r.add_argument("--port", type=int, help="the port your Meridian uses (default: MERIDIAN_PORT or 8790)")
    r.add_argument("--restore-env", action="store_true",
                   help="also give back .env from a backup made with --include-env (never overwrites a file)")
    r.add_argument("--yes", action="store_true", help="do not ask before replacing the database")
    r.add_argument("--dry-run", action="store_true", help="check everything, change nothing")
    r.set_defaults(fn=cmd_restore)

    pr = sub.add_parser("prune", help="delete all but the newest N backups")
    common(pr)
    pr.add_argument("--keep", type=int, default=DEFAULT_KEEP, metavar="N", help=f"how many to keep (default {DEFAULT_KEEP})")
    pr.add_argument("--dry-run", action="store_true", help="say what would be deleted, delete nothing")
    pr.set_defaults(fn=cmd_prune)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "keep", None) is not None and args.keep < 1:
        print("--keep must be 1 or more.", file=sys.stderr)
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
