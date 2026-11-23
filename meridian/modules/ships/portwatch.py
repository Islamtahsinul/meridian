"""IMF PortWatch daily chokepoint transits beside Meridian's own strait counts (docs/modules/ships.md).

Source: IMF PortWatch (https://portwatch.imf.org/), two public ArcGIS feature services:

- ``PortWatch_chokepoints_database``: the 28 chokepoints (name, position, page id);
- ``Daily_Chokepoints_Data``: transits per chokepoint and day by ship type (container, dry bulk, general
  cargo, ro-ro, tanker), derived by the IMF from satellite AIS. Published weekly, about a week behind.

Terms (IMF, read by the research team): data may be downloaded, used and redistributed with attribution
to the IMF, but "The IMF prohibits bulk download of information by automated technology without explicit
permission". So Meridian only makes small, filtered requests, at most one every three minutes:

- the chokepoint list once a month (one request, about 20 KB);
- the 1-year average per chokepoint once a week (one server-side statistics query, 28 rows);
- each chokepoint's latest days once a week (one filtered query; the first fill reads 120 days, about
  6 KB). In all about 30 small requests a week, whether or not the map is open (the data changes weekly).

The cross-check compares the two sources plainly and says when either has a coverage gap: PortWatch
can lose ships when satellite AIS is degraded (its Strait of Hormuz counts fell from about 70 a day in
November 2025 to 1 to 6 a day in November 2026, against a 1-year average of 22), and Meridian only hears
terrestrial receivers, which do not reach Hormuz, Bab-el-Mandeb, Suez or Malacca.
"""

from __future__ import annotations

import calendar
import json
import re
import threading
import time

from meridian.net import NetError

BASE = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
POINTS_URL = BASE + "/PortWatch_chokepoints_database/FeatureServer/0/query"
DAILY_URL = BASE + "/Daily_Chokepoints_Data/FeatureServer/0/query"
HOST = "services9.arcgis.com"
SITE = "https://portwatch.imf.org/"
SOURCE_ID = "ships.portwatch"
CREDIT = "IMF PortWatch (International Monetary Fund), transits derived from satellite AIS"

# Meridian strait -> PortWatch chokepoint (same waterway). PortWatch's Oresund counts the Sound only, not the
# Great Belt; Dardanelles, Singapore Strait and the two Baltic straits have no PortWatch counterpart.
MATCH = {"suez": "chokepoint1", "panama": "chokepoint2", "bosphorus": "chokepoint3", "bab_el_mandeb": "chokepoint4",
         "malacca": "chokepoint5", "hormuz": "chokepoint6", "gibraltar": "chokepoint8", "dover": "chokepoint9",
         "danish_straits": "chokepoint10", "taiwan": "chokepoint11"}
MATCH_NOTE = {"danish_straits": "PortWatch's Oresund chokepoint covers the Sound only, not the Great Belt."}
STRAIT_OF = {v: k for k, v in MATCH.items()}

EVERY_S = 180.0
POINTS_EVERY_S = 30 * 86400
STATS_EVERY_S = 7 * 86400
DAILY_EVERY_S = 7 * 86400
FIRST_FILL_DAYS = 120
OVERLAP_DAYS = 14
KEEP_DAYS = 400
TIMEOUT_S = 60.0
HOST_INTERVAL_S = 20.0
BACKOFF_S = 900.0
FAR_BELOW = 0.35
FAR_ABOVE = 2.5
DAILY_FIELDS = "date,portid,n_total,n_tanker,n_container,n_dry_bulk,n_general_cargo,n_roro,n_cargo,capacity"
TYPES = ("tanker", "container", "dry_bulk", "general_cargo", "roro")

UNAVAILABLE = ("PortWatch's daily chokepoint service is not public right now (it asks for a sign-in token; it may be "
               "being republished). Meridian does not sign in and tries again later")

_PORTID = re.compile(r"^chokepoint\d{1,3}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def day_of(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def epoch_of(day: str) -> float:
    return float(calendar.timegm(time.strptime(day, "%Y-%m-%d")))


def backoff_s(fails: int) -> float:
    """Seconds to wait after ``fails`` failures in a row: 15 minutes, doubling, at most 6 hours."""
    return min(6 * 3600.0, BACKOFF_S * 2 ** max(0, min(fails, 20) - 1))


def wait_text(s: float) -> str:
    """'15 min', '1 h', '6 h' (whole hours from an hour up)."""
    return f"{round(s / 60)} min" if s < 3600 else f"{round(s / 3600)} h"


def day_text(day: str) -> str:
    g = time.strptime(day, "%Y-%m-%d")
    return f"{g.tm_mday} {time.strftime('%b', g)}"


def _int(v, hi: int = 1_000_000_000) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    v = int(v)
    return v if 0 <= v <= hi else None


def valid_day(day) -> bool:
    """A real calendar day as "YYYY-MM-DD" (the pattern alone lets "2026-11-31" through)."""
    if not isinstance(day, str) or not _DAY.match(day) or len(day) != 10:
        return False
    try:
        time.strptime(day, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _day(v) -> str | None:
    """PortWatch dates are date-only strings ("2026-11-15"); older layers send epoch milliseconds."""
    if isinstance(v, str) and valid_day(v[:10]):
        return v[:10]
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 4e12:
        return day_of(v / 1000.0)
    return None


def parse_points(data) -> list[dict]:
    out = []
    for f in (data or {}).get("features") or [] if isinstance(data, dict) else []:
        a = (f or {}).get("attributes") or {}
        pid = str(a.get("portid") or "")
        if not _PORTID.match(pid):
            continue
        try:
            lat, lon = float(a["lat"]), float(a["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        out.append({"portid": pid, "name": str(a.get("portname") or a.get("fullname") or pid)[:80], "lat": lat,
                    "lon": lon, "annual": _int(a.get("vessel_count_total")),
                    "pageid": str(a.get("pageid") or "")[:40] if re.fullmatch(r"[0-9a-f]{8,40}", str(a.get("pageid") or "")) else None})
    return out


def parse_daily(data) -> list[dict]:
    out = []
    for f in (data or {}).get("features") or [] if isinstance(data, dict) else []:
        a = (f or {}).get("attributes") or {}
        pid = str(a.get("portid") or "")
        day = _day(a.get("date"))
        total = _int(a.get("n_total"), 100_000)
        if not _PORTID.match(pid) or day is None or total is None:
            continue
        out.append({"portid": pid, "day": day, "n_total": total,
                    **{f"n_{t}": _int(a.get(f"n_{t}"), 100_000) for t in TYPES},
                    "n_cargo": _int(a.get("n_cargo"), 100_000), "capacity": _int(a.get("capacity"))})
    return out


def parse_stats(data) -> dict[str, dict]:
    out = {}
    for f in (data or {}).get("features") or [] if isinstance(data, dict) else []:
        a = (f or {}).get("attributes") or {}
        pid = str(a.get("portid") or "")
        avg = a.get("avg_total")
        if not _PORTID.match(pid) or isinstance(avg, bool) or not isinstance(avg, (int, float)) or not 0 <= avg < 100_000:
            continue
        tank = a.get("avg_tanker")
        out[pid] = {"avg_1y": float(avg), "avg_1y_tanker": float(tank) if isinstance(tank, (int, float)) else None,
                    "days_1y": _int(a.get("days"), 400)}
    return out


class PortWatch:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS ships_pw_daily (
        portid TEXT NOT NULL, day TEXT NOT NULL, n_total INTEGER, n_tanker INTEGER, n_container INTEGER,
        n_dry_bulk INTEGER, n_general_cargo INTEGER, n_roro INTEGER, n_cargo INTEGER, capacity INTEGER,
        PRIMARY KEY (portid, day)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS ships_pw_points (
        portid TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL, annual INTEGER, pageid TEXT, avg_1y REAL,
        avg_1y_tanker REAL, days_1y INTEGER, stats_to TEXT, fetched_t REAL, updated_t REAL);
    CREATE TABLE IF NOT EXISTS ships_strait_days (
        strait TEXT NOT NULL, day TEXT NOT NULL, transits INTEGER, transits_ct INTEGER, vessels INTEGER,
        covered_hours INTEGER, PRIMARY KEY (strait, day)) WITHOUT ROWID;
    """
    KV = "ships.portwatch.meta"

    def __init__(self, db, net=None, *, now=time.time, log=None, status=None):
        self.db = db
        self.net = net
        self.now = now
        self.log = log
        self.status_cb = status or (lambda **kw: None)
        self.lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None
        self.requests = 0

    def create_tables(self) -> None:
        self.db.script(self.SCHEMA)

    # -- state ----------------------------------------------------------------------------------
    def meta(self) -> dict:
        m = self.db.kv_get(self.KV, {}) or {}
        return m if isinstance(m, dict) else {}

    def _set_meta(self, **changes) -> dict:
        m = self.meta()
        m.update(changes)
        self.db.kv_set(self.KV, m)
        return m

    def points(self) -> list[dict]:
        return self.db.query("SELECT * FROM ships_pw_points ORDER BY CAST(SUBSTR(portid, 11) AS INTEGER)")

    # -- fetching (one request per call at most) --------------------------------------------------
    def step(self) -> str:
        """Make at most one request: the list, then the weekly averages, then whichever chokepoint is due."""
        now = self.now()
        meta = self.meta()
        fails = int(meta.get("fail_n") or 0)
        if fails and now - float(meta.get("fail_t") or 0) < backoff_s(fails):
            return "backoff"
        what = "idle"
        try:
            pts = self.points()
            if not pts or now - float(meta.get("points_t") or 0) >= POINTS_EVERY_S:
                what = "points"
                self._fetch_points(now)
            elif now - float(meta.get("stats_t") or 0) >= STATS_EVERY_S:
                what = "stats"
                self._fetch_stats(now)
            else:
                due = [p for p in pts if now - float(p.get("fetched_t") or 0) >= DAILY_EVERY_S]
                if due:
                    what = "daily"
                    # Oldest first; on the first fill Hormuz (the cross-check the research asked for), then the
                    # chokepoints Meridian also monitors, so the strait cards fill first.
                    due.sort(key=lambda p: (float(p.get("fetched_t") or 0),
                                            0 if p["portid"] == MATCH["hormuz"] else 1 if p["portid"] in STRAIT_OF else 2))
                    self._fetch_daily(due[0]["portid"], now)
            if what != "idle":
                self._set_meta(fail_n=0, fail_t=None, last_error=None, last_ok_t=now)
                self._cache = None
                self._report()
        except (NetError, ValueError, KeyError, TypeError) as err:
            msg = str(err)[:300] or type(err).__name__
            self._set_meta(fail_n=fails + 1, fail_t=now, last_error=msg)
            # UNAVAILABLE already explains itself; anything else is wrapped once. The note replaces the
            # "being fetched" one, so the status never says fetching goes on while it is backing off.
            error = msg if msg == UNAVAILABLE else f"IMF PortWatch did not answer ({msg})"
            self.status_cb(ok=False, error=error, note=self._fail_note(backoff_s(fails + 1)))
            if self.log:
                self.log.info("ships: PortWatch %s failed: %s", what, msg)
            return "error"
        return what

    def _get(self, url: str, params: dict) -> dict:
        self.requests += 1
        data = self.net.get_json(url, params=params, timeout=TIMEOUT_S, retries=1)
        if not isinstance(data, dict):
            raise ValueError("unexpected answer")
        if data.get("error"):
            err = data["error"]
            if err.get("code") in (498, 499):
                # The service has stopped answering publicly (secured, moved or being republished). Meridian
                # never signs in (CONTRACT rule 1): it says so and tries again later.
                raise ValueError(UNAVAILABLE)
            raise ValueError(f"ArcGIS error {err.get('code', '')}: {str(err.get('message') or '')[:120]}".strip())
        return data

    def _fetch_points(self, now: float) -> None:
        data = self._get(POINTS_URL, {"where": "1=1", "returnGeometry": "false", "f": "json",
                                      "outFields": "portid,portname,fullname,lat,lon,vessel_count_total,pageid"})
        rows = parse_points(data)
        if not rows:
            raise ValueError("no chokepoints in the list")
        with self.db.tx() as c:
            for p in rows:
                c.execute("INSERT INTO ships_pw_points(portid, name, lat, lon, annual, pageid, updated_t) "
                          "VALUES(?,?,?,?,?,?,?) ON CONFLICT(portid) DO UPDATE SET name = excluded.name, "
                          "lat = excluded.lat, lon = excluded.lon, annual = excluded.annual, pageid = excluded.pageid, "
                          "updated_t = excluded.updated_t",
                          (p["portid"], p["name"], p["lat"], p["lon"], p["annual"], p["pageid"], now))
        self._set_meta(points_t=now)

    def _latest_day(self, portid: str | None = None) -> str | None:
        """The latest stored real day (a malformed stored day is passed over, not trusted)."""
        sql = "SELECT DISTINCT day FROM ships_pw_daily" + (" WHERE portid = ?" if portid else "") + " ORDER BY day DESC LIMIT 20"
        for r in self.db.query(sql, (portid,) if portid else ()):
            if valid_day(r["day"]):
                return r["day"]
        return None

    def _fetch_stats(self, now: float) -> None:
        latest = self._latest_day() or day_of(now - 8 * 86400)
        end = epoch_of(latest)
        start = day_of(end - 364 * 86400)
        stats = [{"statisticType": "avg", "onStatisticField": "n_total", "outStatisticFieldName": "avg_total"},
                 {"statisticType": "avg", "onStatisticField": "n_tanker", "outStatisticFieldName": "avg_tanker"},
                 {"statisticType": "count", "onStatisticField": "n_total", "outStatisticFieldName": "days"}]
        data = self._get(DAILY_URL, {"where": f"date >= DATE '{start}' AND date <= DATE '{latest}'",
                                     "outStatistics": json.dumps(stats, separators=(",", ":")),
                                     "groupByFieldsForStatistics": "portid", "f": "json"})
        rows = parse_stats(data)
        if not rows:
            raise ValueError("no averages in the answer")
        with self.db.tx() as c:
            for pid, s in rows.items():
                c.execute("UPDATE ships_pw_points SET avg_1y = ?, avg_1y_tanker = ?, days_1y = ?, stats_to = ? "
                          "WHERE portid = ?", (s["avg_1y"], s["avg_1y_tanker"], s["days_1y"], latest, pid))
        self._set_meta(stats_t=now)

    def _fetch_daily(self, portid: str, now: float) -> None:
        if not _PORTID.match(portid):
            raise ValueError("bad chokepoint id")
        latest = self._latest_day(portid)
        floor = day_of(now - FIRST_FILL_DAYS * 86400)
        start = max(floor, day_of(epoch_of(latest) - OVERLAP_DAYS * 86400)) if latest else floor
        data = self._get(DAILY_URL, {"where": f"portid = '{portid}' AND date >= DATE '{start}'",
                                     "outFields": DAILY_FIELDS, "returnGeometry": "false", "f": "json"})
        rows = [r for r in parse_daily(data) if r["portid"] == portid]
        with self.db.tx() as c:
            c.executemany("INSERT OR REPLACE INTO ships_pw_daily(portid, day, n_total, n_tanker, n_container, n_dry_bulk, "
                          "n_general_cargo, n_roro, n_cargo, capacity) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          [(r["portid"], r["day"], r["n_total"], r["n_tanker"], r["n_container"], r["n_dry_bulk"],
                            r["n_general_cargo"], r["n_roro"], r["n_cargo"], r["capacity"]) for r in rows])
            c.execute("UPDATE ships_pw_points SET fetched_t = ? WHERE portid = ?", (now, portid))
            c.execute("DELETE FROM ships_pw_daily WHERE day < ?", (day_of(now - KEEP_DAYS * 86400),))

    def _report(self) -> None:
        # Only called right after a request succeeded (fail_n is back to 0), so fetching really goes on.
        sums = self.summaries()
        with_data = [s for s in sums.values() if s.get("latest")]
        latest = max((s["latest"]["day"] for s in with_data), default=None)
        note = (f"{len(with_data)} of {len(sums)} chokepoints have daily counts, up to {day_text(latest)}."
                if latest else f"{len(sums)} chokepoints listed; daily counts are being fetched (one every few minutes).")
        self.status_cb(ok=True, count=len(with_data), note=note)

    def _fail_note(self, wait_s: float) -> str:
        """What is stored, and that fetching is paused until the next try (said plainly, no "being fetched")."""
        try:
            sums = self.summaries()
        except Exception:      # the note is a courtesy; never let it hide the failure itself
            sums = {}
        with_data = [s for s in sums.values() if s.get("latest")]
        latest = max((s["latest"]["day"] for s in with_data), default=None)
        if latest:
            have = f"{len(with_data)} of {len(sums)} chokepoints have daily counts, up to {day_text(latest)}. "
        elif sums:
            have = f"{len(sums)} chokepoints listed; no daily counts yet. "
        else:
            have = ""
        return f"{have}Fetching is paused after the failure; next try in about {wait_text(wait_s)}."

    # -- summaries ------------------------------------------------------------------------------
    def summaries(self, *, series_days: int = 0) -> dict[str, dict]:
        now = self.now()
        with self.lock:
            cached = self._cache
        if series_days == 0 and cached is not None and now - cached[0] < 600:
            return cached[1]
        pts = self.points()
        rows = self.db.query("SELECT * FROM ships_pw_daily WHERE day >= ? ORDER BY day",
                             (day_of(now - FIRST_FILL_DAYS * 86400),))
        by: dict[str, list[dict]] = {}
        for r in rows:
            by.setdefault(r["portid"], []).append(r)
        out = {}
        for p in pts:
            out[p["portid"]] = self._summary(p, by.get(p["portid"], []), now, series_days)
        if series_days == 0:
            with self.lock:
                self._cache = (now, out)
        return out

    def summary(self, portid: str, series_days: int = 90) -> dict | None:
        if not _PORTID.match(str(portid)):
            return None
        p = self.db.one("SELECT * FROM ships_pw_points WHERE portid = ?", (portid,))
        if p is None:
            return None
        now = self.now()
        rows = self.db.query("SELECT * FROM ships_pw_daily WHERE portid = ? AND day >= ? ORDER BY day",
                             (portid, day_of(now - max(series_days, 28) * 86400 - 30 * 86400)))
        return self._summary(p, rows, now, series_days)

    def _summary(self, p: dict, rows: list[dict], now: float, series_days: int) -> dict:
        sid = STRAIT_OF.get(p["portid"])
        out = {"id": p["portid"], "name": p["name"], "lat": round(p["lat"], 4), "lon": round(p["lon"], 4),
               "strait": sid, "match_note": MATCH_NOTE.get(sid) if sid else None,
               "mean_1y": round(p["avg_1y"], 1) if p.get("avg_1y") is not None else None,
               "mean_1y_tanker": round(p["avg_1y_tanker"], 1) if p.get("avg_1y_tanker") is not None else None,
               "days_1y": p.get("days_1y"), "latest": None, "lag_days": None, "mean_7d": None, "mean_28d": None,
               "change_1y_pct": None, "missing_28d": None, "zero_days_28d": None, "by_type_7d": None, "flags": [],
               "fetched_at": p.get("fetched_t"), "link": SITE}
        rows = [r for r in rows if valid_day(r.get("day"))]       # a stored bad day must not break every summary
        if not rows:
            out["flags"].append("no_data")
            return out
        last = rows[-1]
        last_t = epoch_of(last["day"])
        out["latest"] = {"day": last["day"], "total": last["n_total"], **{t: last.get(f"n_{t}") for t in TYPES}}
        out["lag_days"] = int((now - last_t) // 86400)
        days = {r["day"]: r for r in rows}

        def window(n):
            want = [day_of(last_t - i * 86400) for i in range(n)]
            return [days[d] for d in want if d in days], n - sum(1 for d in want if d in days)

        w7, _m7 = window(7)
        w28, m28 = window(28)
        if w7:
            out["mean_7d"] = round(sum(r["n_total"] for r in w7) / len(w7), 1)
            out["by_type_7d"] = {t: sum(r.get(f"n_{t}") or 0 for r in w7) for t in TYPES}
        if w28:
            out["mean_28d"] = round(sum(r["n_total"] for r in w28) / len(w28), 1)
            out["zero_days_28d"] = sum(1 for r in w28 if not r["n_total"])
        out["missing_28d"] = m28
        avg = out["mean_1y"]
        if avg and out["mean_7d"] is not None and avg > 0:
            out["change_1y_pct"] = int(round((out["mean_7d"] - avg) / avg * 100))
            if avg >= 3 and out["mean_7d"] < FAR_BELOW * avg:
                out["flags"].append("far_below")
            elif avg >= 3 and out["mean_7d"] > FAR_ABOVE * avg:
                out["flags"].append("far_above")
        if m28 >= 3:
            out["flags"].append("missing_days")
        if out["lag_days"] > 21:
            out["flags"].append("stale")
        if series_days:
            first = day_of(last_t - (series_days - 1) * 86400)
            out["series"] = [[r["day"], r["n_total"], r.get("n_tanker")] for r in rows if r["day"] >= first]
        return out

    # -- Meridian's own daily strait counts (for same-day comparisons) ----------------------------
    def tally_strait_days(self, strait_ids, now: float | None = None) -> int:
        """Complete UTC days from the strait visits and hourly samples Meridian keeps (8 days).

        An hour counts as covered only when Meridian heard vessels inside the strait itself: an hourly sample
        with vessels inside, or a visit that began in that hour. The samples are written whenever anything
        is heard within about 55 km of the strait, which alone says nothing about the strait."""
        now = self.now() if now is None else now
        today = epoch_of(day_of(now))
        n = 0
        rows = []
        for back in range(1, 8):
            start = today - back * 86400
            day = day_of(start)
            for sid in strait_ids:
                v = self.db.one("SELECT COUNT(*) AS visits, SUM(CASE WHEN moving > 0 THEN 1 ELSE 0 END) AS transits, "
                                "SUM(CASE WHEN moving > 0 AND cls IN ('cargo', 'tanker') THEN 1 ELSE 0 END) AS ct, "
                                "COUNT(DISTINCT mmsi) AS vessels FROM ships_visits WHERE strait = ? AND t_in >= ? AND t_in < ?",
                                (sid, start, start + 86400)) or {}
                heard = {int((r["t"] - start) // 3600) for r in self.db.query(
                    "SELECT t FROM ships_samples WHERE strait = ? AND t >= ? AND t < ? AND inside > 0",
                    (sid, start, start + 86400))}
                heard |= {int((r["t_in"] - start) // 3600) for r in self.db.query(
                    "SELECT t_in FROM ships_visits WHERE strait = ? AND t_in >= ? AND t_in < ?", (sid, start, start + 86400))}
                hours = len(heard)
                sampled = self.db.scalar("SELECT COUNT(*) FROM ships_samples WHERE strait = ? AND t >= ? AND t < ?",
                                         (sid, start, start + 86400), default=0) or 0
                if not v.get("visits") and not hours and not sampled:
                    continue
                rows.append((sid, day, int(v.get("transits") or 0), int(v.get("ct") or 0), int(v.get("vessels") or 0),
                             int(min(24, hours))))
                n += 1
        if rows:
            self.db.executemany("INSERT OR REPLACE INTO ships_strait_days(strait, day, transits, transits_ct, vessels, "
                                "covered_hours) VALUES(?,?,?,?,?,?)", rows)
        self.db.execute("DELETE FROM ships_strait_days WHERE day < ?", (day_of(now - KEEP_DAYS * 86400),))
        return n

    def strait_days(self, sid: str, since_day: str) -> dict[str, dict]:
        return {r["day"]: r for r in self.db.query("SELECT * FROM ships_strait_days WHERE strait = ? AND day >= ?",
                                                    (sid, since_day))}


# ---------------------------------------------------------------------------------------------
# The cross-check, in plain words
# ---------------------------------------------------------------------------------------------
def cross_check(pw: dict | None, strait: dict | None, own_days: dict[str, dict] | None,
                unavailable: str | None = None) -> dict:
    """Compare PortWatch with Meridian for one waterway. ``strait`` is Meridian's strait status (or None when
    PortWatch has no Meridian counterpart); ``own_days`` Meridian's daily tallies by day; ``unavailable`` the
    reason PortWatch's last answer failed, if it did."""
    lines: list[str] = []
    pw_gap = meridian_gap = False
    overlap = []
    if not pw or not pw.get("latest"):
        if unavailable:
            lines.append(f"IMF PortWatch has no daily counts here: {unavailable.rstrip('.')}.")
        else:
            lines.append("IMF PortWatch has no daily counts here yet (they are fetched one chokepoint every few "
                         "minutes, then weekly).")
        pw_gap = True
    else:
        lat = pw["latest"]
        line = f"PortWatch counted {lat['total']:,} transit{'s' if lat['total'] != 1 else ''} on {day_text(lat['day'])}"
        if pw.get("mean_7d") is not None:
            line += f"; {pw['mean_7d']:g} a day over its latest 7 days"
        if pw.get("mean_1y") is not None:
            line += f", against {pw['mean_1y']:g} a day over the past year"
        lines.append(line + ".")
        if "far_below" in pw["flags"]:
            pw_gap = True
            lines.append(f"That is {abs(pw['change_1y_pct'])}% below PortWatch's own 1-year average. PortWatch does not "
                         "say why: traffic may really have fallen, or its satellite AIS may be missing ships (GNSS "
                         "interference, for example, corrupts the positions ships broadcast).")
        elif "far_above" in pw["flags"]:
            lines.append(f"That is {pw['change_1y_pct']}% above PortWatch's own 1-year average.")
        if "missing_days" in pw["flags"]:
            pw_gap = True
            lines.append(f"PortWatch has no data for {pw['missing_28d']} of its latest 28 days.")
        if pw.get("zero_days_28d"):
            pw_gap = pw_gap or pw["zero_days_28d"] >= 3
            lines.append(f"It reports no transits at all on {pw['zero_days_28d']} of its latest 28 days.")
        if "stale" in pw["flags"]:
            pw_gap = True
            lines.append(f"Its latest day is {pw['lag_days']} days old.")
            if unavailable:
                lines.append(f"Newer days could not be fetched: {unavailable.rstrip('.')}.")
        elif pw.get("lag_days") is not None:
            lines.append(f"PortWatch runs about a week behind: its latest day is {pw['lag_days']} days old.")
    if strait is None:
        lines.append("Meridian does not monitor this chokepoint itself, so the figures cannot be checked here.")
        return {"lines": lines, "pw_gap": pw_gap, "meridian_gap": True, "overlap": overlap}
    own_days = own_days or {}
    if pw and pw.get("latest"):
        for day in sorted(own_days, reverse=True):
            if day > pw["latest"]["day"]:
                continue
            overlap.append({"day": day, **{k: own_days[day][k] for k in ("transits", "transits_ct", "covered_hours")}})
            if len(overlap) >= 7:
                break
    covered_days = [d for d in own_days.values() if (d.get("covered_hours") or 0) > 0]
    if not strait.get("coverage") and not covered_days:
        meridian_gap = True
        lines.append("Meridian hears no terrestrial AIS here, so it cannot check PortWatch's figures: neither source "
                     "can be confirmed against the other.")
    else:
        if overlap:
            o = overlap[0]
            if not o["covered_hours"]:
                meridian_gap = True
                lines.append(f"On {day_text(o['day'])} Meridian heard no vessels inside the strait itself (only ships "
                             "nearby), so it has no count to set against PortWatch's.")
            else:
                lines.append(f"On {day_text(o['day'])} Meridian counted {o['transits_ct']:,} cargo and tanker transits "
                             f"({o['transits']:,} of all types) while it heard vessels inside the strait in "
                             f"{o['covered_hours']} of 24 hours.")
            if 0 < o["covered_hours"] < 20:
                meridian_gap = True
                lines.append("Meridian's coverage that day was partial, so its count is a minimum.")
            pw_day = None
            if pw and pw.get("latest") and o["day"] == pw["latest"]["day"]:
                pw_day = pw["latest"]["total"]
            if pw_day is not None and o["covered_hours"] >= 20 and max(pw_day, o["transits_ct"]) >= 5:
                ratio = o["transits_ct"] / max(1, pw_day)
                lines.append(("The two counts are close" if 0.67 <= ratio <= 1.5 else "The two counts differ")
                             + f" ({o['transits_ct']} against {pw_day}). They are measured differently: PortWatch counts "
                               "commercial ships crossing the chokepoint in satellite AIS; Meridian counts vessels moving "
                               "along its own outline as heard by terrestrial receivers.")
        elif strait.get("coverage") and not any(strait.get(k) for k in ("unique_24h", "inside_now", "transits_24h")):
            meridian_gap = True
            lines.append("Meridian heard no vessels inside the strait itself in the last 24 hours (only ships nearby), "
                         "so it cannot check PortWatch's figures. Its daily counts do not overlap PortWatch's days yet.")
        else:
            if not strait.get("coverage"):
                meridian_gap = True
            lines.append(f"Meridian counted {strait.get('transits_24h', 0):,} transits of all types in the last 24 hours"
                         + ("" if strait.get("coverage") else ", but has no live coverage right now")
                         + ". Its daily counts do not overlap PortWatch's days yet (Meridian keeps them from now on).")
    if pw and pw.get("match_note"):
        lines.append(pw["match_note"])
    return {"lines": lines, "pw_gap": pw_gap, "meridian_gap": meridian_gap, "overlap": overlap}
