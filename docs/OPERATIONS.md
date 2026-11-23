# Running Meridian at home

Meridian is most useful when it runs all the time: alerts, the event history and the anomaly baselines
that later detectors need only build up while it is running. This guide covers the home operation kit:
installing on the computer that will run it, starting at logon, backups and restores, moving between computers, keeping the
disk tidy and choosing how much history to keep.

Every tool is standard-library Python, has `--help`, and says in plain words what it did. The ones that
change anything have a dry run. None of them opens a network port: Meridian keeps listening on
`127.0.0.1` only, and the tools' only network use is a check on `127.0.0.1` to see whether Meridian is
running.

| Tool | What it does |
|---|---|
| `tools/autostart.py` | Starts Meridian at logon, with no console window and no administrator rights |
| `tools/backup.py` | Backs up the database while Meridian runs; lists, checks, prunes and restores backups |
| `tools/profile.py` | Exports and imports your own things (favourites, case files, alerts, workspaces) as one JSON file |
| `tools/housekeeping.py` | Shows where disk space goes and frees what is safe to free |

Commands below are for the Windows command prompt, run from the Meridian folder (for example
`C:\Users\you\meridian`).

## 1. Install on the computer that will run it

1. Install Python 3.11 or later, from python.org or the Microsoft Store. There is nothing else to install.
2. Copy the Meridian folder to that computer, for example to `C:\Users\you\meridian`.
3. Copy your `.env` (your keys and settings) by hand, or start from `.env.example`. Keys never travel in
   backups unless you ask (see section 3).
4. Start it once with `run.bat` and open http://127.0.0.1:8790/. On first start Meridian downloads and
   builds its datasets in the background: about 5 to 10 minutes and 300 MB.
5. Stop sleep while the PC is on mains power (Settings, System, Power, "When plugged in, put my device to
   sleep after: Never"), or accept gaps: detectors know when Meridian was not running.

Keep one primary instance. Two copies both streaming worldwide AIS double the traffic (about 2.8 GB a day
each) and split the history between them.

## 2. Start at logon

```
python tools\autostart.py install
python tools\autostart.py status
python tools\autostart.py remove
```

`install` puts one shortcut, `Meridian.lnk`, in your Startup folder
(`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`). It changes nothing else, so `remove`
undoes it completely. At logon Windows opens the shortcut, which runs `pythonw.exe` (Python without a
console window). The launcher waits 20 seconds so logging on stays quick, checks that Meridian is not
already running, and starts the server. Meridian is normally answering within a minute of logon.

Options:
- `--port 8780` runs this Meridian on another port (the default is `MERIDIAN_PORT` from `.env`, or 8790).
- `--delay 60` waits longer after logon.
- `--data-dir E:\meridian-data` keeps the data somewhere else (the same as `MERIDIAN_DATA_DIR`).
- Server options go after `--`, for example `python tools\autostart.py install -- --only flights,ships`.
  Only `--only`, `--no-tasks`, `--no-datasets`, `--debug` and `--open` are accepted.
- `--dry-run` shows the shortcut without writing it.

Logs: the server writes `data\logs\meridian.log` as always. The launcher adds `data\logs\autostart.log`
with starts, stops, warnings, errors and crash traces only (at most about 2 MB). `status` shows whether the
shortcut is there, what it runs, whether Meridian is answering (and for how long) and the last lines of
`autostart.log`.

To stop a Meridian started this way, find its process and end it:

```
netstat -ano | findstr :8790
taskkill /PID 12345
```

(or Task Manager, Details tab, `pythonw.exe`, shown as `pythonw3.11.exe` with the Microsoft Store Python,
End task). It starts again at the next logon unless you run `remove`. A second copy can never start by
accident: the port is taken, and the launcher checks first.

## 3. Backups

```
python tools\backup.py create
python tools\backup.py list
python tools\backup.py verify
python tools\backup.py prune --keep 7
```

`create` is safe while Meridian runs: it copies the database through SQLite's online backup API, checks
the copy with SQLite's integrity check, and writes `data\backups\meridian-backup-<UTC time>.zip`.
A 27 MB database becomes an 11 MB zip in about 3 seconds. Each backup holds:
- `meridian.db`: everything Meridian stores, including favourites, case files, alerts and the event history;
- `settings.json`: your non-secret settings from `.env` (such as `AISSTREAM_AREA` or
  `MERIDIAN_HISTORY_DAYS`) and the names of the keys you set there, never their values;
- `manifest.json`: when it was made, by which version, the database's checksum and row counts.

Datasets (`data\build`, `data\raw`) are not in backups: Meridian rebuilds them after a restore.

Options: `--dest E:\Backups` writes somewhere else (another drive is better), `--keep 7` prunes older
backups afterwards, `--dry-run` says what would happen. `create` refuses when there is not enough free
space and says how much it needs.

`.env` is left out on purpose. `--include-env` adds it, after a warning and an explicit "yes" (or `--yes`):
the backup then holds your API keys in plain text, so keep it out of cloud-synced folders and email, and
delete it when you no longer need it.

`verify` checks a backup completely: the zip's own checksums, the database's SHA-256 against the
manifest, and SQLite's integrity check. `verify --all` checks every backup. Check one now and then, and
practise a restore once in a while into an empty folder, which works while Meridian runs because nothing is
replaced: `python tools\backup.py restore latest --data-dir C:\temp\restore-test --yes`.

A daily backup can run from Task Scheduler (this usually needs no administrator rights for your own
account). Use the full paths on your PC:

```
schtasks /Create /SC DAILY /ST 03:30 /TN "Meridian backup" /TR "\"C:\path\to\pythonw.exe\" \"C:\Users\you\meridian\tools\backup.py\" create --keep 7"
```

`python tools\autostart.py status` prints the `pythonw.exe` path to use. Remove the task with
`schtasks /Delete /TN "Meridian backup"`.

## 4. Restore

1. Stop Meridian (section 2).
2. Run:

```
python tools\backup.py restore latest
python tools\backup.py restore meridian-backup-20261123T170512Z.zip --dry-run
```

`restore` first checks the backup completely. When there is a database to replace, it refuses while
anything answers on Meridian's port (use `--port` if yours is not 8790) or while another program has the
database open, and it asks before it replaces anything (`--yes` skips the question). The current database is kept beside it as
`data\meridian-before-restore-<time>.db`; delete that once you are happy. If anything goes wrong during the
swap, the old database is put back. Start Meridian again afterwards; it rebuilds missing datasets in the
background.

Keys are not restored. `restore` lists the names of the keys the backup's `.env` had, so you know what to
copy. With a backup made with `--include-env`, `--restore-env` gives the backup's `.env` back, and it
never overwrites a file:
- restoring into the data folder Meridian uses when you have no `.env` yet, it becomes your `.env`;
- otherwise (you already have a `.env`, or this is a practice restore) it is saved as `.env` in a new
  folder, `env-from-backup-<time>`, inside the folder you restored into, for example
  `data\env-from-backup-<time>\.env`. It never goes next to your own `.env`, and git ignores files called
  `.env` wherever they are. Compare it with yours, keep what you need and delete the folder: it holds your
  keys.

## 5. Moving to another PC

Everything (recommended when moving to another computer):
1. On the old PC: `python tools\backup.py create --dest E:\` (Meridian can keep running).
2. Copy the zip and your `.env` to the new PC, by hand.
3. On the new PC: install Meridian (section 1, without starting it), then
   `python tools\backup.py restore E:\meridian-backup-....zip`, then start it. Copying `data\build` as well
   skips the dataset rebuild.

Only your own things (for example to keep a second computer in step):
1. On one PC: `python tools\profile.py export E:\my-meridian.json`.
2. On the other: `python tools\profile.py import E:\my-meridian.json`.

## 6. Your own things: profile export and import

```
python tools\profile.py export [FILE]
python tools\profile.py import FILE [--mode merge|replace] [--dry-run] [--create]
```

A profile holds what you made, nothing Meridian downloaded: favourites, case files with their notes and
items, alert watchlists with their areas and rules, alert settings, and workspaces (saved workspaces and My
places). It has a schema version; a newer Meridian's profile is refused with a plain message rather than
half read. It holds your own data, such as case notes and watchlist areas around home, so keep it private.

`import` checks the whole file first, with the same rules as the app, and imports nothing if anything is
wrong: it lists each problem and where it is. Areas, rules and settings are checked by the alerts module's
own code, and saved workspaces and places by the workspace module's (names, place types, positions, radii,
workspace states, and the app's limits on how many you can keep). It also refuses when the data folder has
no database, which usually means a mistyped `--data-dir` or a Meridian that has never started there;
`--create` imports into a new, empty database instead. Then, in one transaction:
- `merge` (the default) adds what is missing and never changes or removes anything already there. Case files
  and watchlists count as the same when their name and creation time match, and their missing items and
  rules are added. Importing the same file twice adds nothing the second time. Workspaces are merged only
  into a Meridian that has none yet.
- `replace` makes your things match the profile. It asks first, and saves your current things to
  `data\backups\meridian-profile-before-import-<time>.json` before it starts. Replacing watchlists also
  clears their alerts from the inbox, as deleting a watchlist in Meridian does.

Imported alert rules start watching from the moment of the import, so events that were already in the
history do not flood the inbox. Links from places to watchlists follow the watchlists' new ids. Rules,
settings, workspace tables and workspace columns from a newer Meridian are skipped with a note.
`--dry-run` does the whole import on a temporary copy of the database, reports exactly what it would do and
throws the copy away: the database itself is only read. Reload Meridian in the browser afterwards.

Browser preferences (layers switched on, basemap, panel sizes) live in the browser, not in the database,
so neither backups nor profiles carry them.

## 7. Disk housekeeping

```
python tools\housekeeping.py
python tools\housekeeping.py --apply
python tools\housekeeping.py --only logs,backups --apply
```

Without `--apply` it changes nothing: it shows the free space on the drive, the size of the database
(with the free pages inside it), datasets, downloads, caches, logs, backups and development copies, and
lists what could be deleted. `--apply` deletes exactly that list; files in use are kept and reported.

| Kind | What can go | Option |
|---|---|---|
| `logs` | Rotated and old logs in `data\logs` (never `meridian.log` or `autostart.log`) | `--log-days 14` |
| `partials` | Unfinished downloads and temporary files older than a day | |
| `raw` | Downloads Meridian made itself (they have a `.meta.json` download record) and can fetch again; it does so when it next rebuilds that dataset | `--days 30` |
| `devcopies` | Development copies in `data-dev` nobody has changed recently and that are not open | `--dev-days 3` |
| `backups` | All but the newest backups, and databases a restore kept aside once they are 14 days old | `--keep-backups 7` |

Never deleted: the database, the datasets in `data\build`, the Japanese river camera directory
(`data\raw\cameras\jpriver`: the site asks not to be collected again), the Windy crawl pages
(`data\raw\cameras\windy`: a new crawl costs about 1,800 requests), and anything without a download record
(probes, crawl caches such as the cable pages). Links and junctions are never followed.

A source that is blocked on the network you are on (some networks block some sources) cannot be fetched
again while you are there, so leave `raw` alone on such a network, or raise `--days`.

## 8. How much history to keep

The event history (every module's notable events, snapshotted every 5 minutes) keeps 30 days by default.
Set `MERIDIAN_HISTORY_DAYS` in `.env` to keep between 1 and 90 days, then restart Meridian:

```
MERIDIAN_HISTORY_DAYS=60
```

Values outside 1 to 90 are held at the nearest limit and a value that is not a number keeps 30 days; both
are noted in `meridian.log`. Sources and status, Optional keys, lists the setting and its value, and the
Event history line there says how many days are kept. The timeline, alerts and briefings read this history;
anomaly baselines will want 28 days or more. An event row takes a few hundred bytes, and the table is capped
at 300,000 rows (the oldest go first), which keeps it to roughly 100 to 150 MB even at 90 days.

Other modules keep their own tables for their own windows (listed in each module's notes in
`docs\modules\`); this setting does not change them.

## 9. Troubleshooting

| Problem | What to do |
|---|---|
| Meridian did not start at logon | Run `python tools\autostart.py status`. It shows any problem with the shortcut and the last lines of `autostart.log`. After moving the folder or reinstalling Python, run `install` again |
| `autostart.log` says another program is using the port | Something else has port 8790. Install with `--port 8780` (any free port) |
| A console window opens at logon | The shortcut runs `python.exe` instead of `pythonw.exe`. Run `install` again, or pass `--python` with the path to `pythonw.exe` |
| It stops when the PC sleeps | Change the power settings (section 1). After waking it carries on by itself |
| `restore` says Meridian is answering | Stop it first (section 2). If yours runs on another port, add `--port` |
| `restore` says the database is open in another program | A second Meridian (for example on another port) is using it. Stop it |
| `create` says there is not enough free space | Run `python tools\housekeeping.py`, or use `--dest` on another drive |
| `verify` says a backup is damaged | Use another backup; delete the damaged one |
| `profile.py import` lists problems | The file was changed or cut short. Export it again from the other PC |
| `profile.py import` says there is no database | Check `--data-dir`. On a new PC, start Meridian once first, or add `--create` |
| Database errors in `meridian.log` after a crash or power cut | Stop Meridian, run `python tools\backup.py verify`, then restore the newest good backup |
| Where are the logs? | `data\logs\meridian.log` (the server) and `data\logs\autostart.log` (the launcher) |

## 10. For developers: shared statistics

`meridian/stats.py` holds the shared arithmetic for anomaly detectors, so they all use the same formulas:
robust z-scores on log counts, Poisson tails and
surprise for small counts, EWMA, hour-of-week and hour-of-day baselines with an uptime mask, and
`assess_count`, which says "Building baseline: 9 of 14 days" until a baseline is ready. Thresholds stay with
each detector. Every function has a docstring; `tests/test_core_stats.py` shows them in use.

```python
from meridian import stats

b = stats.SeasonalBaseline(period="day", window_hours=6, periods=21, min_samples=10)
r = b.assess(hourly_counts, end=stats.hour_start(now), uptime=hourly_uptime)
if r["ready"] and r["z"] >= 3 and r["ratio"] >= 2 and r["value"] >= 8:
    ...  # unusual: r has value, median, ratio, z, method, coverage and status for the card
```
