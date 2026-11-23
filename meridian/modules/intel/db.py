"""SQLite tables of the intel module (all prefixed ``intel_``) and the queries over them.

intel_files        one row per GDELT 15-minute export processed (ok, missing or error)
intel_events       deduplicated conflict events (root code + place rounded to 0.1 deg + day), 14 days
intel_news         the latest geolocated source article per place, 3 days
intel_cell_day     per 0.5-degree cell and day: GDELT rows and violent-conflict rows (risk-zone weighting), 8 days
intel_country_day  per-country daily tallies (GDELT rows, conflict rows, protest rows, tone; event counts and
                   severity recomputed from intel_events while the day is inside the event window), 45 days
intel_protests     deduplicated protest events (CAMEO root 14), same columns as intel_events, 14 days
intel_country_hour per-country hourly tallies (rows, conflict rows, protest rows, tone) for the news-spike windows, 3 days
intel_domain_hour  per-country hourly row counts of the busiest source domains (single-source guard), 2 days
intel_spikes       news-spike episodes (one row per country, metric and episode), 90 days
intel_advice       latest official travel advice per source and country (FCDO, US State Department)
intel_advice_changes  detected travel-advice changes, 365 days
"""

from __future__ import annotations

import json
import time
from typing import Iterable

from meridian import geo

from . import gdelt

EVENT_DAYS = 14
NEWS_DAYS = 3
TALLY_DAYS = 45
HOUR_DAYS = 3
DOMAIN_DAYS = 2
SPIKE_DAYS = 90
ADVICE_CHANGE_DAYS = 365
# intel_files.tallies: 1 = rows counted by the first version (daily rows, conflict rows, tone);
# 2 = also protest rows (daily) and the hourly and domain tallies used by the news-spike detector.
TALLIES_VERSION = 2
EVENT_TABLES = ("intel_events", "intel_protests")

SCHEMA = """
CREATE TABLE IF NOT EXISTS intel_files (
    ts TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    status TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    kept INTEGER NOT NULL DEFAULT 0,
    news INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS intel_events (
    id TEXT PRIMARY KEY,
    dkey TEXT NOT NULL UNIQUE,
    day INTEGER NOT NULL,
    t_first REAL NOT NULL,
    t_last REAL NOT NULL,
    root TEXT NOT NULL,
    code TEXT NOT NULL,
    quad INTEGER NOT NULL,
    goldstein REAL NOT NULL,
    category TEXT NOT NULL,
    armed INTEGER NOT NULL,
    crime INTEGER NOT NULL,
    precision TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    place TEXT NOT NULL,
    fips TEXT NOT NULL,
    iso2 TEXT,
    adm1 TEXT NOT NULL,
    actor1 TEXT,
    actor2 TEXT,
    actors TEXT NOT NULL,
    mentions INTEGER NOT NULL,
    sources INTEGER NOT NULL,
    articles INTEGER NOT NULL,
    tone REAL NOT NULL,
    reports INTEGER NOT NULL,
    urls TEXT NOT NULL,
    gids TEXT NOT NULL,
    severity REAL NOT NULL,
    focus REAL NOT NULL DEFAULT 1.0,
    url_main TEXT,
    main_mentions INTEGER NOT NULL DEFAULT 0,
    urlm TEXT NOT NULL DEFAULT '{}',
    actm TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS intel_events_t ON intel_events(t_last);
CREATE INDEX IF NOT EXISTS intel_events_lat ON intel_events(lat);
CREATE INDEX IF NOT EXISTS intel_events_iso ON intel_events(iso2, day);
CREATE TABLE IF NOT EXISTS intel_news (
    id TEXT PRIMARY KEY,
    lkey TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    title TEXT,
    domain TEXT NOT NULL,
    titled INTEGER NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    place TEXT NOT NULL,
    iso2 TEXT,
    t REAL NOT NULL,
    quad INTEGER NOT NULL,
    goldstein REAL NOT NULL,
    tone REAL NOT NULL,
    mentions INTEGER NOT NULL,
    code TEXT NOT NULL,
    root TEXT NOT NULL,
    severity REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS intel_news_t ON intel_news(t);
CREATE INDEX IF NOT EXISTS intel_news_lat ON intel_news(lat);
CREATE TABLE IF NOT EXISTS intel_cell_day (
    cell TEXT NOT NULL,
    day INTEGER NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    conflict INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cell, day)
);
CREATE TABLE IF NOT EXISTS intel_country_day (
    iso2 TEXT NOT NULL,
    day INTEGER NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    conflict_rows INTEGER NOT NULL DEFAULT 0,
    tone_sum REAL NOT NULL DEFAULT 0,
    tone_w REAL NOT NULL DEFAULT 0,
    events INTEGER NOT NULL DEFAULT 0,
    sev_sum REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (iso2, day)
);
CREATE TABLE IF NOT EXISTS intel_country_hour (
    iso2 TEXT NOT NULL,
    hour INTEGER NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    conflict_rows INTEGER NOT NULL DEFAULT 0,
    protest_rows INTEGER NOT NULL DEFAULT 0,
    tone_sum REAL NOT NULL DEFAULT 0,
    tone_w REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (iso2, hour)
);
CREATE INDEX IF NOT EXISTS intel_country_hour_h ON intel_country_hour(hour);
CREATE TABLE IF NOT EXISTS intel_domain_hour (
    iso2 TEXT NOT NULL,
    hour INTEGER NOT NULL,
    domain TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (iso2, hour, domain)
);
CREATE INDEX IF NOT EXISTS intel_domain_hour_h ON intel_domain_hour(hour);
CREATE TABLE IF NOT EXISTS intel_spikes (
    id TEXT PRIMARY KEY,
    iso2 TEXT NOT NULL,
    metric TEXT NOT NULL,
    first_t REAL NOT NULL,
    last_t REAL NOT NULL,
    severity REAL NOT NULL,
    peak_ratio REAL NOT NULL,
    value INTEGER NOT NULL,
    baseline REAL NOT NULL,
    ratio REAL NOT NULL,
    z REAL,
    p REAL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS intel_spikes_t ON intel_spikes(last_t);
CREATE INDEX IF NOT EXISTS intel_spikes_iso ON intel_spikes(iso2, metric);
CREATE TABLE IF NOT EXISTS intel_advice (
    source TEXT NOT NULL,
    iso2 TEXT NOT NULL,
    level INTEGER NOT NULL,
    statuses TEXT NOT NULL DEFAULT '[]',
    label TEXT NOT NULL DEFAULT '',
    updated REAL,
    fetched REAL NOT NULL,
    slug TEXT,
    url TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source, iso2)
);
CREATE TABLE IF NOT EXISTS intel_advice_changes (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    iso2 TEXT NOT NULL,
    t REAL NOT NULL,
    old_level INTEGER,
    new_level INTEGER,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS intel_advice_changes_t ON intel_advice_changes(t);
"""

_EVENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {t} (
    id TEXT PRIMARY KEY, dkey TEXT NOT NULL UNIQUE, day INTEGER NOT NULL, t_first REAL NOT NULL, t_last REAL NOT NULL,
    root TEXT NOT NULL, code TEXT NOT NULL, quad INTEGER NOT NULL, goldstein REAL NOT NULL, category TEXT NOT NULL,
    armed INTEGER NOT NULL, crime INTEGER NOT NULL, precision TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
    place TEXT NOT NULL, fips TEXT NOT NULL, iso2 TEXT, adm1 TEXT NOT NULL, actor1 TEXT, actor2 TEXT, actors TEXT NOT NULL,
    mentions INTEGER NOT NULL, sources INTEGER NOT NULL, articles INTEGER NOT NULL, tone REAL NOT NULL,
    reports INTEGER NOT NULL, urls TEXT NOT NULL, gids TEXT NOT NULL, severity REAL NOT NULL,
    focus REAL NOT NULL DEFAULT 1.0, url_main TEXT, main_mentions INTEGER NOT NULL DEFAULT 0,
    urlm TEXT NOT NULL DEFAULT '{{}}', actm TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS {t}_t ON {t}(t_last);
CREATE INDEX IF NOT EXISTS {t}_lat ON {t}(lat);
CREATE INDEX IF NOT EXISTS {t}_iso ON {t}(iso2, day);
"""

_EVENT_COLS = ("id", "dkey", "day", "t_first", "t_last", "root", "code", "quad", "goldstein", "category", "armed",
               "crime", "precision", "lat", "lon", "place", "fips", "iso2", "adm1", "actor1", "actor2", "actors",
               "mentions", "sources", "articles", "tone", "reports", "urls", "gids", "severity", "focus", "url_main",
               "main_mentions", "urlm", "actm")
_JSON_LISTS = ("actors", "urls", "gids")
_JSON_DICTS = ("urlm", "actm")
_NEWS_COLS = ("id", "lkey", "url", "title", "domain", "titled", "lat", "lon", "place", "iso2", "t", "quad",
              "goldstein", "tone", "mentions", "code", "root", "severity")


def _columns(store, table: str) -> set[str]:
    return {r["name"] for r in store.query(f"PRAGMA table_info({table})")}


def create(store) -> None:
    store.script(SCHEMA)
    store.script(_EVENT_TABLE_SQL.format(t="intel_protests"))
    cols = _columns(store, "intel_events")
    for name, decl in (("focus", "REAL NOT NULL DEFAULT 1.0"), ("url_main", "TEXT"),
                       ("main_mentions", "INTEGER NOT NULL DEFAULT 0"), ("urlm", "TEXT NOT NULL DEFAULT '{}'"),
                       ("actm", "TEXT NOT NULL DEFAULT '{}'")):
        if name not in cols:   # tables made by an early build of this module
            store.execute(f"ALTER TABLE intel_events ADD COLUMN {name} {decl}")
    # Columns added by the Intelligence+ work: NULL protest rows mean "not counted" (days before the change).
    if "protest_rows" not in _columns(store, "intel_country_day"):
        store.execute("ALTER TABLE intel_country_day ADD COLUMN protest_rows INTEGER")
    if "tallies" not in _columns(store, "intel_files"):
        store.execute("ALTER TABLE intel_files ADD COLUMN tallies INTEGER NOT NULL DEFAULT 1")


# ---------------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------------
def mark_file(store, ts: str, status: str, rows: int = 0, kept: int = 0, news: int = 0, nbytes: int = 0,
              tallies: int = TALLIES_VERSION, conn=None) -> None:
    sql = ("INSERT INTO intel_files(ts, fetched_at, status, rows, kept, news, bytes, tallies) VALUES(?,?,?,?,?,?,?,?) "
           "ON CONFLICT(ts) DO UPDATE SET fetched_at=excluded.fetched_at, status=excluded.status, "
           "rows=excluded.rows, kept=excluded.kept, news=excluded.news, bytes=excluded.bytes, tallies=excluded.tallies")
    args = (ts, time.time(), status, rows, kept, news, nbytes, tallies)
    if conn is not None:
        conn.execute(sql, args)
    else:
        store.execute(sql, args)


def file_states(store, since_ts: str) -> dict[str, dict]:
    return {r["ts"]: r for r in store.query("SELECT * FROM intel_files WHERE ts >= ?", (since_ts,))}


def file_counts_by_day(store, since_ts: str) -> dict[int, tuple[int, int]]:
    """day -> (files whose rows are in the daily tallies, files also tallied for protests and hours)."""
    out: dict[int, tuple[int, int]] = {}
    for r in store.query("SELECT substr(ts, 1, 8) AS d, COUNT(*) AS n, SUM(tallies >= ?) AS v2 FROM intel_files "
                         "WHERE ts >= ? AND status IN ('ok', 'seed') GROUP BY d", (TALLIES_VERSION, since_ts)):
        try:
            out[int(r["d"])] = (int(r["n"] or 0), int(r["v2"] or 0))
        except (TypeError, ValueError):
            continue
    return out


def tallied_files_between(store, start_ts: str, end_ts: str) -> int:
    """Export files in [start_ts, end_ts) whose rows are in the hourly tallies."""
    return int(store.scalar("SELECT COUNT(*) FROM intel_files WHERE ts >= ? AND ts < ? AND status IN ('ok', 'seed') "
                            "AND tallies >= ?", (start_ts, end_ts, TALLIES_VERSION), 0))


def coverage_days(store, days: int = 30, now: float | None = None) -> float:
    """How many days of GDELT exports (ok files x 15 min) the last ``days`` days contain."""
    now = now or time.time()
    since = gdelt.epoch_to_ts(now - days * 86400)
    n = store.scalar("SELECT COUNT(*) FROM intel_files WHERE ts >= ? AND status = 'ok'", (since,), 0)
    return n * gdelt.SLOT_SECONDS / 86400.0


# ---------------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------------
def _ev_from_row(r: dict) -> dict:
    ev = dict(r)
    for k in _JSON_LISTS:
        try:
            ev[k] = json.loads(ev[k]) if ev.get(k) else []
        except ValueError:
            ev[k] = []
    for k in _JSON_DICTS:
        try:
            ev[k] = json.loads(ev[k]) if ev.get(k) else {}
        except ValueError:
            ev[k] = {}
    ev["armed"] = bool(ev["armed"])
    ev["crime"] = bool(ev["crime"])
    return ev


def _ev_values(ev: dict) -> tuple:
    out = []
    for k in _EVENT_COLS:
        v = ev.get(k)
        if k in _JSON_LISTS:
            v = json.dumps(v or [], ensure_ascii=False)
        elif k in _JSON_DICTS:
            v = json.dumps(v or {}, ensure_ascii=False)
        elif k == "main_mentions":
            v = int(v or 0)
        elif k in ("armed", "crime"):
            v = 1 if v else 0
        out.append(v)
    return tuple(out)


def _table(table: str) -> str:
    if table not in EVENT_TABLES:
        raise ValueError(f"unknown event table {table!r}")
    return table


def upsert_events(store, reports: Iterable[dict], table: str = "intel_events", severity_fn=None) -> tuple[int, int]:
    """Merge conflict (or protest) reports into an event table. Returns (new events, merged reports).
    ``severity_fn(ev)`` rescores a merged event (default: the conflict formula)."""
    table = _table(table)
    groups: dict[str, list[dict]] = {}
    for r in reports:
        groups.setdefault(r["dkey"], []).append(r)
    if not groups:
        return 0, 0
    new = merged = 0
    keys = list(groups)
    with store.tx() as c:
        existing: dict[str, dict] = {}
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            q = f"SELECT * FROM {table} WHERE dkey IN (%s)" % ",".join("?" * len(chunk))
            for row in c.execute(q, chunk).fetchall():
                existing[row["dkey"]] = _ev_from_row(dict(row))
        out = []
        for key, items in groups.items():
            ev = existing.get(key)
            start = 0
            if ev is None:
                ev = gdelt.new_event(items[0])
                new += 1
                start = 1
            for item in items[start:]:
                if gdelt.merge_event(ev, item, severity_fn=severity_fn):
                    merged += 1
            out.append(_ev_values(ev))
        c.executemany(f"INSERT OR REPLACE INTO {table}(%s) VALUES(%s)" % (",".join(_EVENT_COLS),
                                                                          ",".join("?" * len(_EVENT_COLS))), out)
    return new, merged


def _bbox_sql(bbox, lat="lat", lon="lon") -> tuple[str, list]:
    if not bbox:
        return "", []
    w, s, e, n = bbox
    if w <= e:
        return f" AND {lat} BETWEEN ? AND ? AND {lon} BETWEEN ? AND ?", [s, n, w, e]
    return f" AND {lat} BETWEEN ? AND ? AND ({lon} >= ? OR {lon} <= ?)", [s, n, w, e]


def query_events(store, *, bbox=None, since: float = 0.0, min_severity: float = 0.0, include_crime: bool = False,
                 iso2: str | None = None, precise_only: bool = False, limit: int = 3000,
                 order: str = "severity", table: str = "intel_events", min_support: bool = False,
                 category: str | None = None) -> list[dict]:
    """``min_support`` keeps events with 2+ sources or 3+ articles (protests)."""
    sql = f"SELECT * FROM {_table(table)} WHERE t_last >= ? AND severity >= ?"
    params: list = [since, min_severity]
    if min_support:
        sql += " AND (sources >= 2 OR articles >= 3)"
    if category:
        sql += " AND category = ?"
        params.append(category)
    if not include_crime:
        sql += " AND crime = 0"
    if precise_only:
        sql += " AND precision = 'city'"
    if iso2:
        sql += " AND iso2 = ?"
        params.append(iso2)
    extra, p2 = _bbox_sql(bbox)
    sql += extra
    params += p2
    sql += " ORDER BY " + ("t_last DESC" if order == "time" else "severity DESC, t_last DESC") + " LIMIT ?"
    params.append(int(limit))
    return [_ev_from_row(r) for r in store.query(sql, params)]


def query_event_points(store, *, bbox=None, since: float = 0.0, min_severity: float = 0.0, include_crime: bool = False,
                       limit: int = 3000, table: str = "intel_events", min_support: bool = False) -> list[dict]:
    """The few columns the map needs, without decoding the JSON columns (fast path for the layer)."""
    sql = ("SELECT id, lat, lon, severity, category, crime, precision, code, root, place, t_last, sources, articles, "
           f"reports FROM {_table(table)} WHERE t_last >= ? AND severity >= ?")
    params: list = [since, min_severity]
    if min_support:
        sql += " AND (sources >= 2 OR articles >= 3)"
    if not include_crime:
        sql += " AND crime = 0"
    extra, p2 = _bbox_sql(bbox)
    sql += extra + " ORDER BY severity DESC, t_last DESC LIMIT ?"
    params += p2 + [int(limit)]
    return store.query(sql, params)


def get_event(store, eid: str, table: str = "intel_events") -> dict | None:
    r = store.one(f"SELECT * FROM {_table(table)} WHERE id = ?", (eid,))
    return _ev_from_row(r) if r else None


def near_events(store, lat: float, lon: float, radius_km: float, *, since: float, limit: int = 15,
                include_crime: bool = False, table: str = "intel_events",
                min_support: bool = False) -> list[tuple[float, dict]]:
    bbox = geo.bbox_of_circle(lat, lon, radius_km)
    rows = query_events(store, bbox=bbox, since=since, include_crime=include_crime, limit=2000, table=table,
                        min_support=min_support)
    out = []
    for ev in rows:
        d = geo.haversine_km(lat, lon, ev["lat"], ev["lon"])
        if d <= radius_km:
            out.append((d, ev))
    out.sort(key=lambda t: (t[0], -t[1]["severity"]))
    return out[:limit]


def event_places(store, since: float, table: str = "intel_events") -> list[dict]:
    extra = " AND (sources >= 2 OR articles >= 3)" if table == "intel_protests" else ""
    return store.query("SELECT place, iso2, COUNT(*) AS n, MAX(severity) AS max_sev, MAX(t_last) AS t, "
                       f"AVG(lat) AS lat, AVG(lon) AS lon FROM {_table(table)} WHERE t_last >= ? AND crime = 0{extra} "
                       "GROUP BY place, iso2 ORDER BY n DESC LIMIT 4000", (since,))


def count_events(store, since: float = 0.0, table: str = "intel_events") -> int:
    return int(store.scalar(f"SELECT COUNT(*) FROM {_table(table)} WHERE t_last >= ?", (since,), 0))


# ---------------------------------------------------------------------------------------------
# News points
# ---------------------------------------------------------------------------------------------
def upsert_news(store, items: Iterable[dict]) -> int:
    best: dict[str, dict] = {}
    for it in items:
        cur = best.get(it["lkey"])
        if cur is None or (it["t"], it["severity"], it["titled"]) > (cur["t"], cur["severity"], cur["titled"]):
            best[it["lkey"]] = it
    if not best:
        return 0
    cols = ",".join(_NEWS_COLS)
    marks = ",".join("?" * len(_NEWS_COLS))
    updates = ",".join(f"{c}=excluded.{c}" for c in _NEWS_COLS if c not in ("id", "lkey"))
    sql = (f"INSERT INTO intel_news({cols}) VALUES({marks}) ON CONFLICT(lkey) DO UPDATE SET {updates} "
           "WHERE excluded.t > intel_news.t OR (excluded.t = intel_news.t AND "
           "(excluded.titled > intel_news.titled OR (excluded.titled = intel_news.titled AND "
           "excluded.severity > intel_news.severity)))")
    rows = [tuple((1 if it[c] else 0) if c == "titled" else it[c] for c in _NEWS_COLS) for it in best.values()]
    store.executemany(sql, rows)
    return len(rows)


def _news_from_row(r: dict) -> dict:
    n = dict(r)
    n["titled"] = bool(n["titled"])
    return n


def query_news(store, *, bbox=None, since: float = 0.0, min_severity: float = 0.0, titled_only: bool = False,
               iso2: str | None = None, limit: int = 3000, order: str = "severity", q: str | None = None) -> list[dict]:
    sql = "SELECT * FROM intel_news WHERE t >= ? AND severity >= ?"
    params: list = [since, min_severity]
    if titled_only:
        sql += " AND titled = 1"
    if iso2:
        sql += " AND iso2 = ?"
        params.append(iso2)
    if q:
        sql += " AND (title LIKE ? OR place LIKE ? OR domain LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    extra, p2 = _bbox_sql(bbox)
    sql += extra
    params += p2
    sql += " ORDER BY " + ("t DESC, severity DESC" if order == "time" else "severity DESC, t DESC") + " LIMIT ?"
    params.append(int(limit))
    return [_news_from_row(r) for r in store.query(sql, params)]


def get_news(store, nid: str) -> dict | None:
    r = store.one("SELECT * FROM intel_news WHERE id = ?", (nid,))
    return _news_from_row(r) if r else None


# ---------------------------------------------------------------------------------------------
# Per-country tallies
# ---------------------------------------------------------------------------------------------
_ADD_DAY_SQL = (
    "INSERT INTO intel_country_day(iso2, day, rows, conflict_rows, tone_sum, tone_w, protest_rows) VALUES(?,?,?,?,?,?,?) "
    "ON CONFLICT(iso2, day) DO UPDATE SET rows = rows + excluded.rows, "
    "conflict_rows = conflict_rows + excluded.conflict_rows, tone_sum = tone_sum + excluded.tone_sum, "
    "tone_w = tone_w + excluded.tone_w, protest_rows = COALESCE(protest_rows, 0) + excluded.protest_rows")
_ADD_PROTEST_DAY_SQL = (
    "INSERT INTO intel_country_day(iso2, day, protest_rows) VALUES(?,?,?) "
    "ON CONFLICT(iso2, day) DO UPDATE SET protest_rows = COALESCE(protest_rows, 0) + excluded.protest_rows")
_ADD_HOUR_SQL = (
    "INSERT INTO intel_country_hour(iso2, hour, rows, conflict_rows, protest_rows, tone_sum, tone_w) VALUES(?,?,?,?,?,?,?) "
    "ON CONFLICT(iso2, hour) DO UPDATE SET rows = rows + excluded.rows, conflict_rows = conflict_rows + "
    "excluded.conflict_rows, protest_rows = protest_rows + excluded.protest_rows, tone_sum = tone_sum + excluded.tone_sum, "
    "tone_w = tone_w + excluded.tone_w")
_ADD_DOMAIN_SQL = (
    "INSERT INTO intel_domain_hour(iso2, hour, domain, rows) VALUES(?,?,?,?) "
    "ON CONFLICT(iso2, hour, domain) DO UPDATE SET rows = rows + excluded.rows")


def add_tallies(store, tallies: dict[tuple[str, int], list], conn=None, protest_only: bool = False) -> None:
    """tallies[(iso2, day)] = [rows, conflict_rows, tone_sum, tone_w(, protest_rows)].
    ``protest_only`` adds just the protest rows (files whose other rows were counted before protests were)."""
    if not tallies:
        return
    run = conn.executemany if conn is not None else store.executemany
    if protest_only:
        run(_ADD_PROTEST_DAY_SQL, [(iso, day, (v[4] if len(v) > 4 else 0)) for (iso, day), v in tallies.items()])
        return
    run(_ADD_DAY_SQL, [(iso, day, v[0], v[1], v[2], v[3], (v[4] if len(v) > 4 else 0))
                       for (iso, day), v in tallies.items()])


def add_hour_tallies(store, tallies: dict[tuple[str, int], list], conn=None) -> None:
    """tallies[(iso2, hour)] = [rows, conflict_rows, protest_rows, tone_sum, tone_w]"""
    if tallies:
        run = conn.executemany if conn is not None else store.executemany
        run(_ADD_HOUR_SQL, [(iso, hour, v[0], v[1], v[2], v[3], v[4]) for (iso, hour), v in tallies.items()])


def add_domain_tallies(store, tallies: dict[tuple[str, int, str], int], conn=None) -> None:
    """tallies[(iso2, hour, domain)] = rows"""
    if tallies:
        run = conn.executemany if conn is not None else store.executemany
        run(_ADD_DOMAIN_SQL, [(iso, hour, dom, n) for (iso, hour, dom), n in tallies.items()])


def hour_window_totals(store, start: int, end: int) -> dict[str, tuple[int, int, int]]:
    """iso2 -> (rows, conflict rows, protest rows) for hours in [start, end)."""
    return {r["iso2"]: (int(r["n"] or 0), int(r["c"] or 0), int(r["p"] or 0)) for r in store.query(
        "SELECT iso2, SUM(rows) AS n, SUM(conflict_rows) AS c, SUM(protest_rows) AS p FROM intel_country_hour "
        "WHERE hour >= ? AND hour < ? GROUP BY iso2", (int(start), int(end)))}


def day_tallies(store, day_from: int, day_to: int) -> dict[int, dict[str, tuple]]:
    """day -> iso2 -> (rows, conflict rows, protest rows or None when not counted that day)."""
    out: dict[int, dict[str, tuple]] = {}
    for r in store.query("SELECT iso2, day, rows, conflict_rows, protest_rows FROM intel_country_day "
                         "WHERE day >= ? AND day <= ?", (day_from, day_to)):
        out.setdefault(int(r["day"]), {})[r["iso2"]] = (int(r["rows"] or 0), int(r["conflict_rows"] or 0),
                                                       r["protest_rows"])
    return out


def story_concentration(store, table: str, iso2: str, since: float) -> tuple[int, list[tuple[str, int]]]:
    """(reports in the largest deduplicated event, [(place, reports)] busiest places) since ``since``."""
    table = _table(table)
    top = int(store.scalar(f"SELECT MAX(reports) FROM {table} WHERE iso2 = ? AND t_last >= ?", (iso2, since), 0) or 0)
    places = [(gdelt.short_place(r["place"]) or r["place"], int(r["n"] or 0)) for r in store.query(
        f"SELECT place, SUM(reports) AS n FROM {table} WHERE iso2 = ? AND t_last >= ? GROUP BY place "
        "ORDER BY n DESC LIMIT 5", (iso2, since))]
    return top, [pl for pl in places if pl[0]]


def top_domain_share(store, iso2: str, start: int, end: int) -> tuple[str | None, float]:
    """The busiest source domain of a country's rows in [start, end) and its share of those rows."""
    total = store.scalar("SELECT SUM(rows) FROM intel_country_hour WHERE iso2 = ? AND hour >= ? AND hour < ?",
                         (iso2, start, end), 0) or 0
    r = store.one("SELECT domain, SUM(rows) AS n FROM intel_domain_hour WHERE iso2 = ? AND hour >= ? AND hour < ? "
                  "GROUP BY domain ORDER BY n DESC LIMIT 1", (iso2, start, end))
    if not r or not total:
        return None, 0.0
    return r["domain"], float(r["n"] or 0) / float(total)


# ---------------------------------------------------------------------------------------------
# News-spike episodes
# ---------------------------------------------------------------------------------------------
def upsert_spike(store, flag: dict, window_end: float, gap_s: float, severity: float) -> str:
    """Extend the open episode of (country, metric) or start a new one. Returns its id."""
    detail = json.dumps({k: flag.get(k) for k in ("name", "label", "drivers", "text", "title", "method", "baseline_days",
                                                   "mean", "lat", "lon", "reason")}, ensure_ascii=False)
    with store.tx() as c:
        row = c.execute("SELECT * FROM intel_spikes WHERE iso2 = ? AND metric = ? AND last_t >= ? ORDER BY last_t DESC "
                        "LIMIT 1", (flag["iso2"], flag["metric"], window_end - gap_s)).fetchone()
        if row:
            sid = row["id"]
            c.execute("UPDATE intel_spikes SET last_t = MAX(last_t, ?), severity = MAX(severity, ?), "
                      "peak_ratio = MAX(peak_ratio, ?), value = ?, baseline = ?, ratio = ?, z = ?, p = ?, detail = ? "
                      "WHERE id = ?", (window_end, severity, flag["ratio"], flag["value"], flag["baseline"],
                                       flag["ratio"], flag["z"], flag["p"], detail, sid))
        else:
            first = window_end - 3600
            sid = f"{flag['iso2']}:{flag['metric']}:{int(first)}"
            c.execute("INSERT OR IGNORE INTO intel_spikes(id, iso2, metric, first_t, last_t, severity, peak_ratio, value, "
                      "baseline, ratio, z, p, detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, flag["iso2"], flag["metric"], first, window_end, severity, flag["ratio"], flag["value"],
                       flag["baseline"], flag["ratio"], flag["z"], flag["p"], detail))
    return sid


def _json_field(d: dict, key: str) -> dict:
    try:
        d[key] = json.loads(d.get(key) or "{}")
    except ValueError:
        d[key] = {}
    return d


def query_spikes(store, *, since: float, iso2: str | None = None, metric: str | None = None,
                 limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM intel_spikes WHERE last_t >= ?"
    params: list = [since]
    if iso2:
        sql += " AND iso2 = ?"
        params.append(iso2)
    if metric:
        sql += " AND metric = ?"
        params.append(metric)
    sql += " ORDER BY last_t DESC LIMIT ?"
    params.append(int(limit))
    return [_json_field(dict(r), "detail") for r in store.query(sql, params)]


def get_spike(store, sid: str) -> dict | None:
    r = store.one("SELECT * FROM intel_spikes WHERE id = ?", (sid,))
    return _json_field(dict(r), "detail") if r else None


# ---------------------------------------------------------------------------------------------
# Travel advice
# ---------------------------------------------------------------------------------------------
def save_advice(store, source: str, iso2: str, entry: dict) -> None:
    store.execute("INSERT INTO intel_advice(source, iso2, level, statuses, label, updated, fetched, slug, url, data) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source, iso2) DO UPDATE SET level=excluded.level, "
                  "statuses=excluded.statuses, label=excluded.label, updated=excluded.updated, fetched=excluded.fetched, "
                  "slug=excluded.slug, url=excluded.url, data=excluded.data",
                  (source, iso2, int(entry.get("level") or 1), json.dumps(entry.get("statuses") or []),
                   entry.get("label") or "", entry.get("updated"), time.time(), entry.get("slug"), entry.get("url"),
                   json.dumps(entry, ensure_ascii=False)))


def load_advice(store) -> dict[str, dict[str, dict]]:
    """source -> iso2 -> stored entry."""
    out: dict[str, dict[str, dict]] = {}
    for r in store.query("SELECT * FROM intel_advice"):
        try:
            entry = json.loads(r["data"] or "{}")
        except ValueError:
            entry = {}
        entry.update({"level": r["level"], "label": r["label"], "updated": r["updated"], "fetched": r["fetched"],
                      "slug": r["slug"], "url": r["url"]})
        try:
            entry["statuses"] = json.loads(r["statuses"] or "[]")
        except ValueError:
            entry["statuses"] = []
        out.setdefault(r["source"], {})[r["iso2"]] = entry
    return out


def add_advice_change(store, change: dict) -> bool:
    cur = store.execute("INSERT OR IGNORE INTO intel_advice_changes(id, source, iso2, t, old_level, new_level, kind, "
                        "text, data) VALUES(?,?,?,?,?,?,?,?,?)",
                        (change["id"], change["source"], change["iso2"], change["t"], change.get("old_level"),
                         change.get("new_level"), change["kind"], change["text"],
                         json.dumps(change.get("data") or {}, ensure_ascii=False)))
    return bool(cur.rowcount)


def query_advice_changes(store, *, since: float, iso2: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM intel_advice_changes WHERE t >= ?"
    params: list = [since]
    if iso2:
        sql += " AND iso2 = ?"
        params.append(iso2)
    sql += " ORDER BY t DESC LIMIT ?"
    params.append(int(limit))
    return [_json_field(dict(r), "data") for r in store.query(sql, params)]


def get_advice_change(store, cid: str) -> dict | None:
    r = store.one("SELECT * FROM intel_advice_changes WHERE id = ?", (cid,))
    return _json_field(dict(r), "data") if r else None


def add_cell_tallies(store, tallies: dict[tuple[str, int], list]) -> None:
    """tallies[(cell, day)] = [rows, conflict_rows]"""
    if not tallies:
        return
    store.executemany(
        "INSERT INTO intel_cell_day(cell, day, rows, conflict) VALUES(?,?,?,?) ON CONFLICT(cell, day) DO UPDATE SET "
        "rows = rows + excluded.rows, conflict = conflict + excluded.conflict",
        [(cell, day, v[0], v[1]) for (cell, day), v in tallies.items()])


def cell_shares(store, since_day: int, min_rows: int = 1) -> dict[tuple[int, int], tuple[int, int]]:
    """(row, col) -> (rows, conflict rows) summed since ``since_day``."""
    out = {}
    for r in store.query("SELECT cell, SUM(rows) AS n, SUM(conflict) AS c FROM intel_cell_day WHERE day >= ? GROUP BY cell "
                         "HAVING SUM(rows) >= ?", (since_day, min_rows)):
        try:
            a, b = r["cell"].split(":")
            out[(int(a), int(b))] = (int(r["n"] or 0), int(r["c"] or 0))
        except ValueError:
            continue
    return out


def recompute_country_days(store, since_day: int) -> None:
    """Refresh event counts and severity sums for days still inside the event window."""
    with store.tx() as c:
        c.execute("UPDATE intel_country_day SET events = 0, sev_sum = 0 WHERE day >= ?", (since_day,))
        c.execute(
            "INSERT INTO intel_country_day(iso2, day, events, sev_sum) "
            "SELECT iso2, day, COUNT(*), SUM(severity) FROM intel_events WHERE day >= ? AND crime = 0 "
            "AND iso2 IS NOT NULL GROUP BY iso2, day "
            "ON CONFLICT(iso2, day) DO UPDATE SET events = excluded.events, sev_sum = excluded.sev_sum",
            (since_day,))


def country_totals(store, since_day: int, tone_since_day: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in store.query("SELECT iso2, SUM(rows) AS rows, SUM(conflict_rows) AS conflict_rows, SUM(events) AS events, "
                         "SUM(sev_sum) AS sev_sum FROM intel_country_day WHERE day >= ? GROUP BY iso2", (since_day,)):
        out[r["iso2"]] = {"rows": r["rows"] or 0, "conflict_rows": r["conflict_rows"] or 0,
                          "events": r["events"] or 0, "sev_sum": r["sev_sum"] or 0.0, "tone": None, "tone_rows": 0}
    for r in store.query("SELECT iso2, SUM(tone_sum) AS ts, SUM(tone_w) AS tw, SUM(rows) AS n FROM intel_country_day "
                         "WHERE day >= ? GROUP BY iso2", (tone_since_day,)):
        d = out.setdefault(r["iso2"], {"rows": 0, "conflict_rows": 0, "events": 0, "sev_sum": 0.0})
        d["tone"] = (r["ts"] / r["tw"]) if r["tw"] else None
        d["tone_rows"] = r["n"] or 0
    return out


# ---------------------------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------------------------
def prune(store, now: float | None = None) -> dict:
    now = now or time.time()
    ev_cut = now - EVENT_DAYS * 86400
    news_cut = now - NEWS_DAYS * 86400
    tally_cut = gdelt.epoch_to_day(now - TALLY_DAYS * 86400)
    file_cut = gdelt.epoch_to_ts(now - TALLY_DAYS * 86400)
    a = store.execute("DELETE FROM intel_events WHERE t_last < ?", (ev_cut,)).rowcount
    b = store.execute("DELETE FROM intel_news WHERE t < ?", (news_cut,)).rowcount
    c = store.execute("DELETE FROM intel_country_day WHERE day < ?", (tally_cut,)).rowcount
    store.execute("DELETE FROM intel_cell_day WHERE day < ?", (gdelt.epoch_to_day(now - 8 * 86400),))
    d = store.execute("DELETE FROM intel_files WHERE ts < ?", (file_cut,)).rowcount
    p = store.execute("DELETE FROM intel_protests WHERE t_last < ?", (ev_cut,)).rowcount
    store.execute("DELETE FROM intel_country_hour WHERE hour < ?", (int(now - HOUR_DAYS * 86400),))
    store.execute("DELETE FROM intel_domain_hour WHERE hour < ?", (int(now - DOMAIN_DAYS * 86400),))
    store.execute("DELETE FROM intel_spikes WHERE last_t < ?", (now - SPIKE_DAYS * 86400,))
    store.execute("DELETE FROM intel_advice_changes WHERE t < ?", (now - ADVICE_CHANGE_DAYS * 86400,))
    return {"events": a, "news": b, "tallies": c, "files": d, "protests": p}
