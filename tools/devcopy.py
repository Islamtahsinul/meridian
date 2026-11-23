"""Make an isolated development copy of Meridian's data for one team.

    python tools/devcopy.py <team> [module ...]

Creates data-dev/<team>/ with a consistent copy of data/meridian.db (SQLite online backup, safe while
the main server runs) and data/build/<module>/ for each module named (all modules when none are given),
then prints the environment to use. Development copies run keyless (MERIDIAN_ENV_FILE=none).
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    team, modules = argv[0], argv[1:]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,40}", team):
        print("team names are letters, digits and hyphens, starting with a letter or digit")
        return 2
    src_data = ROOT / "data"
    dst = ROOT / "data-dev" / team
    (dst / "build").mkdir(parents=True, exist_ok=True)
    src_db = sqlite3.connect(str(src_data / "meridian.db"))
    dst_db = sqlite3.connect(str(dst / "meridian.db"))
    with dst_db:
        src_db.backup(dst_db)
    src_db.close()
    dst_db.close()
    names = modules or [p.name for p in (src_data / "build").iterdir() if p.is_dir()]
    for name in names:
        s = src_data / "build" / name
        if s.is_dir():
            shutil.copytree(s, dst / "build" / name, dirs_exist_ok=True)
    print(f"Copied the database and build/{', build/'.join(names)} into {dst}")
    print("Use it with:")
    print(f'  export MERIDIAN_DATA_DIR="{dst.as_posix()}"')
    print("  export MERIDIAN_ENV_FILE=none")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
