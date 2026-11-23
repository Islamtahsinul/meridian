"""Show where Meridian's disk space goes and free what is safe to free. Changes nothing unless --apply.

    python tools/housekeeping.py                   report, and list what could be deleted
    python tools/housekeeping.py --apply           delete what the report lists
    python tools/housekeeping.py --only logs,backups --apply

What can be deleted (each kind can be picked with --only):
- logs: rotated and old log files in data/logs older than --log-days (14). The logs Meridian is writing
  (meridian.log, autostart.log) are never deleted;
- partials: unfinished downloads and temporary files older than a day;
- raw: downloaded files older than --days (30) that Meridian fetched itself and can fetch again (they have a
  .meta.json download record). It downloads them again the next time it rebuilds that dataset. Never deleted:
  the Japanese river camera directory (the site asks not to be collected again), the Windy crawl pages (a new
  crawl costs about 1,800 requests), and anything without a download record (probes, crawl caches);
- devcopies: development copies in data-dev that nobody has changed for --dev-days (3) and that are not open;
- backups: all but the newest --keep-backups (7) backups, and databases kept aside by a restore once they are
  over 14 days old.

The datasets in data/build and the database itself are only reported, never deleted. Files that are in use
are skipped. Links and junctions are never followed. Exit codes: 0 done, 1 something could not be deleted,
2 bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
import ops_common as oc  # noqa: E402

KINDS = ("logs", "partials", "raw", "devcopies", "backups")
LIVE_LOGS = {"meridian.log", "autostart.log"}
ROTATED_LOG = re.compile(r"^.+\.log\.\d+$")
PARTIAL = re.compile(r"(\.part\d+|\.tmp|\.partial)$")
BACKUP = re.compile(r"^meridian-backup-\d{8}T\d{6}Z(?:-\d+)?\.zip$")
RESTORE_ASIDE = re.compile(r"^meridian-before-restore-\d{8}T\d{6}Z\.db(?:-wal|-shm|-journal)?$")
RESTORE_ASIDE_DAYS = 14
PARKED = re.compile(r"^\..+\.deleting-\d+$")       # a development copy renamed for deletion (see delete())
PARTIAL_DAYS = 1
PROTECTED = {
    ("cameras", "jpriver"): "the Japanese river camera directory: the site asks not to be collected again",
    ("cameras", "windy"): "the Windy webcam crawl: a new crawl costs about 1,800 requests",
}
SHOWN = 12
DAY = 86400.0


@dataclass
class Candidate:
    kind: str
    path: Path
    size: int
    mtime: float
    note: str = ""
    extra: tuple[Path, ...] = ()             # deleted with it (a download record)
    tree: bool = False                        # a whole folder (a development copy)


def ago(t: float, now: float) -> str:
    s = max(0.0, now - t)
    if s < 3600:
        return f"{max(1, int(s // 60))} min"
    if s < 2 * DAY:
        return f"{int(s // 3600)} h"
    return f"{int(s // DAY)} days"


def file_info(path: Path) -> tuple[int, float] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime


def db_header(db: Path) -> dict | None:
    """Page size, page count and free pages from the database file's 100-byte header: a plain read that
    takes no lock, so it is safe while Meridian runs (pages still in the write-ahead log are not counted)."""
    try:
        with open(db, "rb") as f:
            head = f.read(100)
    except OSError:
        return None
    if len(head) < 100 or not head.startswith(b"SQLite format 3\x00"):
        return None
    page = int.from_bytes(head[16:18], "big")
    page = 65536 if page == 1 else page
    return {"page_size": page, "pages": int.from_bytes(head[28:32], "big"),
            "free_pages": int.from_bytes(head[36:40], "big")}


def files_under(root: Path):
    """(path, size, mtime) of every file under root, without following links."""
    stack = [root]
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
            if oc.entry_is_link(e, st):
                continue
            if e.is_dir(follow_symlinks=False):
                stack.append(Path(e.path))
            else:
                yield Path(e.path), st.st_size, st.st_mtime


# ---------------------------------------------------------------------------------------------
# Finding candidates
# ---------------------------------------------------------------------------------------------
def find_logs(ddir: Path, days: float, now: float) -> list[Candidate]:
    logs = ddir / "logs"
    out = []
    if not logs.is_dir():
        return out
    for p, size, mtime in files_under(logs):
        if p.name in LIVE_LOGS or now - mtime < days * DAY:
            continue
        if ROTATED_LOG.match(p.name) or p.suffix == ".log":
            out.append(Candidate("logs", p, size, mtime))
    return out


def find_partials(ddir: Path, now: float, backup_dir: Path) -> list[Candidate]:
    out = []
    roots = [ddir / "raw", ddir / "build", ddir / "cache", backup_dir]
    seen = set()
    for root in roots:
        if not root.is_dir() or root in seen:
            continue
        seen.add(root)
        for p, size, mtime in files_under(root):
            if PARTIAL.search(p.name) and now - mtime >= PARTIAL_DAYS * DAY:
                out.append(Candidate("partials", p, size, mtime))
    return out


def protected_reason(raw: Path, p: Path) -> str | None:
    try:
        parts = p.relative_to(raw).parts
    except ValueError:
        return "outside the downloads folder"
    for prefix, reason in PROTECTED.items():
        if parts[:len(prefix)] == prefix:
            return reason
    return None


def find_raw(ddir: Path, days: float, now: float) -> tuple[list[Candidate], dict[str, int]]:
    """Old downloads with a download record. Also returns the sizes of the protected folders."""
    raw = ddir / "raw"
    out: list[Candidate] = []
    kept: dict[str, int] = {}
    if not raw.is_dir():
        return out, kept
    for p, size, mtime in files_under(raw):
        reason = protected_reason(raw, p)
        if reason:
            key = "/".join(p.relative_to(raw).parts[:2])
            kept[key] = kept.get(key, 0) + size
            continue
        if p.name.endswith(".meta.json") or PARTIAL.search(p.name):
            continue
        meta = p.with_name(p.name + ".meta.json")
        if not meta.is_file() or now - mtime < days * DAY:
            continue
        host = ""
        try:
            url = json.loads(meta.read_text(encoding="utf-8")).get("url") or ""
            host = re.sub(r"^[a-z]+://([^/:?#]+).*$", r"\1", url, flags=re.I) if "://" in url else ""
        except (OSError, ValueError, AttributeError):
            pass
        info = file_info(meta)
        out.append(Candidate("raw", p, size + (info[0] if info else 0), mtime,
                             note=f"from {host}" if host else "", extra=(meta,)))
    return out, kept


def find_devcopies(dev_root: Path, days: float, now: float, in_use: Path) -> tuple[list[Candidate], list[tuple]]:
    """Stale development copies, and (name, size, newest change) for every copy."""
    out, every = [], []
    if not dev_root.is_dir():
        return out, every
    for child in sorted(dev_root.iterdir()):
        if not child.is_dir() or oc.is_link_like(child):
            continue
        if PARKED.match(child.name):                  # an earlier clean-up that a locked file interrupted
            size = oc.tree_stats(child)[0]
            out.append(Candidate("devcopies", child, size, child.stat().st_mtime, note="left by an earlier clean-up",
                                 tree=True))
            continue
        if child.name.startswith("."):
            continue
        size, _files, newest = oc.tree_stats(child)
        newest = newest or child.stat().st_mtime
        every.append((child.name, size, newest))
        if now - newest < days * DAY:
            continue
        if oc.inside(in_use, child):
            continue                                  # the data folder this report is about
        if oc.file_in_use(child / "meridian.db"):
            continue
        out.append(Candidate("devcopies", child, size, newest, note="unchanged", tree=True))
    return out, every


def find_backups(ddir: Path, backup_dir: Path, keep: int, now: float) -> list[Candidate]:
    out = []
    if backup_dir.is_dir():
        found = []
        for p in backup_dir.iterdir():
            if BACKUP.match(p.name) and p.is_file() and not oc.is_link_like(p):
                info = file_info(p)
                found.append((p.name, p, info))
        found.sort(reverse=True)                      # names sort by their UTC time
        for _name, p, info in found[keep:]:
            out.append(Candidate("backups", p, info[0], info[1], note="older backup"))
    if ddir.is_dir():
        for p in ddir.iterdir():
            if RESTORE_ASIDE.match(p.name) and p.is_file():
                info = file_info(p)
                if info and now - info[1] >= RESTORE_ASIDE_DAYS * DAY:
                    out.append(Candidate("backups", p, info[0], info[1], note="kept aside by a restore"))
    return out


# ---------------------------------------------------------------------------------------------
# Report and apply
# ---------------------------------------------------------------------------------------------
def folder_breakdown(folder: Path) -> tuple[int, list[tuple[str, int]]]:
    parts = []
    total = 0
    if folder.is_dir():
        for child in folder.iterdir():
            if oc.is_link_like(child):
                continue
            size = oc.tree_stats(child)[0] if child.is_dir() else (file_info(child) or (0, 0))[0]
            parts.append((child.name, size))
            total += size
    parts.sort(key=lambda x: -x[1])
    return total, parts


def rel(p: Path, base: Path) -> str:
    try:
        return str(p.relative_to(base))
    except ValueError:
        return str(p)


def usage_report(ddir: Path, backup_dir: Path, dev_every: list[tuple], kept: dict[str, int], now: float) -> list[str]:
    lines = []
    try:
        import shutil
        du = shutil.disk_usage(ddir if ddir.exists() else ddir.parent)
        lines.append(f"Drive: {oc.human_size(du.free)} free of {oc.human_size(du.total)} "
                     f"({100 * (du.total - du.free) / max(1, du.total):.0f}% used).")
    except OSError:
        pass
    db = ddir / "meridian.db"
    if db.exists():
        wal = db.with_name(db.name + "-wal")
        head = db_header(db)
        free = (f", about {oc.human_size(head['free_pages'] * head['page_size'])} of it unused"
                if head and head["free_pages"] else "")
        wal_text = f", write-ahead log {oc.human_size(wal.stat().st_size)}" if wal.exists() else ""
        lines.append(f"  Database: {oc.human_size(db.stat().st_size)}{wal_text}{free}.")
    labels = (("build", "Datasets (build)"), ("raw", "Downloads (raw)"), ("cache", "Caches (cache)"), ("logs", "Logs"))
    counted = {"meridian.db", "meridian.db-wal", "meridian.db-shm"}
    for name, label in labels:
        total, parts = folder_breakdown(ddir / name)
        counted.add(name)
        if total or (ddir / name).exists():
            top = ", ".join(f"{n} {oc.human_size(s)}" for n, s in parts[:6] if s)
            lines.append(f"  {label}: {oc.human_size(total)}" + (f" ({top})" if top else "") + ".")
    if backup_dir.is_dir():
        n = sum(1 for p in backup_dir.iterdir() if BACKUP.match(p.name))
        size = oc.tree_stats(backup_dir)[0]
        lines.append(f"  Backups ({rel(backup_dir, ddir)}): {oc.human_size(size)}, {n} backup{'s' if n != 1 else ''}.")
        if oc.inside(backup_dir, ddir):
            counted.add(backup_dir.relative_to(ddir).parts[0])
    other = aside = 0
    if ddir.is_dir():
        for child in ddir.iterdir():
            if child.name in counted or oc.is_link_like(child):
                continue
            size = oc.tree_stats(child)[0] if child.is_dir() else (file_info(child) or (0, 0))[0]
            if RESTORE_ASIDE.match(child.name):
                aside += size
            else:
                other += size
    if aside:
        lines.append(f"  Databases kept aside by a restore: {oc.human_size(aside)} (deletable after "
                     f"{RESTORE_ASIDE_DAYS} days).")
    if other:
        lines.append(f"  Other files in the data folder: {oc.human_size(other)}.")
    for key, size in sorted(kept.items()):
        reason = PROTECTED.get(tuple(key.split("/")), "")
        lines.append(f"  Kept on purpose: raw/{key} ({oc.human_size(size)}), {reason}.")
    if dev_every:
        total = sum(s for _n, s, _t in dev_every)
        parts = ", ".join(f"{n} {oc.human_size(s)} (changed {ago(t, now)} ago)" for n, s, t in
                          sorted(dev_every, key=lambda x: -x[1]))
        lines.append(f"  Development copies (data-dev): {oc.human_size(total)}: {parts}.")
    return lines


TITLES = {
    "logs": "Old logs",
    "partials": "Unfinished downloads and temporary files",
    "raw": "Old downloads Meridian can fetch again (it does so when it next rebuilds that dataset)",
    "devcopies": "Development copies nobody has changed recently",
    "backups": "Old backups",
}


def delete(c: Candidate, roots: dict[str, list[Path]]) -> tuple[bool, str]:
    """Delete one candidate. Returns (deleted, problem)."""
    if not any(oc.inside(c.path, r) for r in roots[c.kind]) or oc.is_link_like(c.path):
        return False, "outside the folders housekeeping looks after"
    try:
        if c.tree:
            if oc.file_in_use(c.path / "meridian.db"):
                return False, "its database is open"
            parking = c.path.with_name(f".{c.path.name}.deleting-{os.getpid()}")
            os.replace(c.path, parking)               # fails while anything inside is open
            oc.safe_rmtree(parking)
        else:
            os.unlink(c.path)
            for extra in c.extra:
                try:
                    os.unlink(extra)
                except FileNotFoundError:
                    pass
    except OSError as err:
        return False, f"in use or locked ({err.strerror or err})"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/housekeeping.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Without --apply nothing is changed. More in docs/OPERATIONS.md.")
    p.add_argument("--apply", action="store_true", help="delete what the report lists (default: only report)")
    p.add_argument("--only", help=f"comma-separated kinds to look at: {', '.join(KINDS)} (default: all)")
    p.add_argument("--days", type=float, default=30, metavar="N", help="downloads older than N days (default 30)")
    p.add_argument("--log-days", type=float, default=14, metavar="N", help="logs older than N days (default 14)")
    p.add_argument("--dev-days", type=float, default=3, metavar="N",
                   help="development copies unchanged for N days (default 3)")
    p.add_argument("--keep-backups", type=int, default=7, metavar="N", help="backups to keep (default 7)")
    p.add_argument("--data-dir", help="Meridian's data folder (default: as the server uses, normally data/)")
    p.add_argument("--backup-dir", help="the backups folder (default: data/backups)")
    p.add_argument("--dev-root", help="where development copies live (default: data-dev next to server.py)")
    p.add_argument("--verbose", action="store_true", help="list every file, not just the first few of each kind")
    args = p.parse_args(argv)
    kinds = set(KINDS) if not args.only else {k.strip() for k in args.only.split(",") if k.strip()}
    unknown = kinds - set(KINDS)
    if unknown:
        print(f"Unknown kind: {', '.join(sorted(unknown))}. Choose from {', '.join(KINDS)}.", file=sys.stderr)
        return 2
    if min(args.days, args.log_days, args.dev_days) < 0 or args.keep_backups < 1:
        print("Days must not be negative and --keep-backups must be 1 or more.", file=sys.stderr)
        return 2

    config = oc.load_config()
    ddir = oc.data_dir(config, args.data_dir)
    backup_dir = Path(args.backup_dir).expanduser().resolve() if args.backup_dir else ddir / "backups"
    dev_root = Path(args.dev_root).expanduser().resolve() if args.dev_root else oc.ROOT / "data-dev"
    now = time.time()

    found: list[Candidate] = []
    if "logs" in kinds:
        found += find_logs(ddir, args.log_days, now)
    if "partials" in kinds:
        found += find_partials(ddir, now, backup_dir)
    raw_found, kept = find_raw(ddir, args.days, now)
    if "raw" in kinds:
        found += raw_found
    dev_found, dev_every = find_devcopies(dev_root, args.dev_days, now, ddir)
    if "devcopies" in kinds:
        found += dev_found
    if "backups" in kinds:
        found += find_backups(ddir, backup_dir, args.keep_backups, now)

    print(f"Meridian data folder: {ddir}")
    for line in usage_report(ddir, backup_dir, dev_every, kept, now):
        print(line)
    print()
    rules = {"logs": f"older than {args.log_days:g} days", "partials": f"older than {PARTIAL_DAYS} day",
             "raw": f"older than {args.days:g} days", "devcopies": f"unchanged for {args.dev_days:g} days",
             "backups": f"keeping the newest {args.keep_backups}"}
    print("Deleting:" if args.apply else "Could be deleted (nothing has been changed):")
    roots = {"logs": [ddir / "logs"], "partials": [ddir / "raw", ddir / "build", ddir / "cache", backup_dir],
             "raw": [ddir / "raw"], "devcopies": [dev_root], "backups": [backup_dir, ddir]}
    total = freed = 0
    problems = 0
    for kind in KINDS:
        if kind not in kinds:
            continue
        items = sorted((c for c in found if c.kind == kind), key=lambda c: (-c.size, str(c.path)))
        size = sum(c.size for c in items)
        total += size
        print(f"  {TITLES[kind]}, {rules[kind]}: " + ("none." if not items else
              f"{len(items):,} item{'s' if len(items) != 1 else ''}, {oc.human_size(size)}."))
        for i, c in enumerate(items):
            show = args.verbose or i < SHOWN
            line = f"    {rel(c.path, ddir if oc.inside(c.path, ddir) else c.path.parent)} " \
                   f"({oc.human_size(c.size)}, {c.note + ', ' if c.note else ''}{ago(c.mtime, now)} old)"
            if args.apply:
                ok, why = delete(c, roots)
                if ok:
                    freed += c.size
                else:
                    problems += 1
                    show = True
                    line += f": kept, {why}"
            if show:
                print(line)
        if len(items) > SHOWN and not args.verbose:
            print(f"    ...and {len(items) - SHOWN:,} more (--verbose lists them all).")
    print()
    if args.apply:
        print(f"Freed {oc.human_size(freed)}." + (f" {problems} item{'s were' if problems != 1 else ' was'} kept "
                                                  "because they were in use or locked." if problems else ""))
        return 1 if problems else 0
    if total:
        print(f"In all {oc.human_size(total)} could be freed. Run again with --apply to delete these.")
    else:
        print("Nothing needs deleting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
