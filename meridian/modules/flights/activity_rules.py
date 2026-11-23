"""Pure rules for the air activity detectors (activity.py): no I/O, no clock, so every rule can be
tested on synthetic series.

- Roles of military aircraft from ICAO type designators, and which roles a surge counts.
- Regions: point-in-outline lookups.
- ``assess_surge``: military aircraft-hours over the last 3 complete hours of a region against the
  same hours on earlier days (hour of day, 7 to 28 days) or, from two weeks, the same hours on the
  same weekday alone (hour of week, which learns weekly schedules such as training days and compares
  weekends with weekends). In the second week the same window a week earlier holds a surge back.
- ``classify_gps`` and ``gps_clusters``: interference cells that are new or clearly worse than the
  same clock hours on each of their own previous 7 days.
- ``assess_airport``: movements in the last two complete hours against the same hour on earlier days
  (or, from two weeks, the same weekday), with an overflight check for receiver coverage.

Statistics come from the shared ``meridian.stats`` helpers (robust z on log counts, Poisson tails,
seasonal windows with an uptime mask). Hours when Meridian was not sampling are left out of every
baseline, so a quiet period is evidence only when Meridian was running.
"""

from __future__ import annotations

import functools
import math
import time
from typing import Any, Iterable

from meridian import geo, stats

from . import activity_data as D
from .classify import HELI_TYPES
from .const import HOTSPOTS

HOUR = 3600
DAY = 86400
SLOT_S = 600                     # one snapshot per 10-minute slot
SLOTS_PER_HOUR = HOUR // SLOT_S
MIN_COVERAGE = 0.8               # windows with fewer snapshots than this share are left out
WEEKDAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")


def popcount(mask: int) -> int:
    return bin(int(mask) & 0x3F).count("1")


def learning_text(n: int, needed: int) -> str:
    """'Learning baseline: 4 of 7 days' (the shared helper's wording, in this module's terms)."""
    return stats.building_text(n, needed).replace("Building baseline", "Learning baseline")


def utc_hm(t: float) -> str:
    return time.strftime("%H:%M", time.gmtime(t))


def num_text(v: float | None, digits: int = 1) -> str:
    """1.0 -> '1', 2.46 -> '2.5', 12.4 -> '12'."""
    if v is None or not math.isfinite(v):
        return "unknown"
    if abs(v) >= 10:
        return f"{v:,.0f}"
    text = f"{v:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def times_text(ratio: float | None) -> str:
    if ratio is None:
        return ""
    return f"{num_text(ratio)} times the usual"


# ---------------------------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------------------------
def role_of(typ: str | None, cls: str | None = None, shape: str | None = None) -> str:
    """Role of a military aircraft: tanker, isr, transport, fighter, bomber, other, trainer or
    helicopter. Trainers and helicopters fly to base timetables and are not part of surge counts."""
    t = (typ or "").upper()
    if cls == "helicopter" or shape == "heli" or t in HELI_TYPES:
        return "helicopter"
    if t in D.TRAINER_TYPES:
        return "trainer"
    if t in D.TANKER_TYPES:
        return "tanker"
    if t in D.ISR_TYPES or shape == "uav":
        return "isr"
    if t in D.BOMBER_TYPES:
        return "bomber"
    if t in D.FIGHTER_TYPES or shape == "fighter":
        return "fighter"
    if t in D.TRANSPORT_TYPES or shape in ("heavy", "jet", "bizjet", "turboprop"):
        return "transport"
    return "other"


def counted(role: str) -> bool:
    return role in D.COUNTED_ROLES


# ---------------------------------------------------------------------------------------------
# regions
# ---------------------------------------------------------------------------------------------
class Region:
    __slots__ = ("id", "name", "over", "ring", "bbox", "label", "hotspots")

    def __init__(self, spec: dict):
        self.id = spec["id"]
        self.name = spec["name"]
        self.over = spec.get("over") or f"over the {spec['name']}"
        self.ring = [[float(x), float(y)] for x, y in spec["ring"]]
        xs = [p[0] for p in self.ring]
        ys = [p[1] for p in self.ring]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))
        self.label = [float(spec["label"][0]), float(spec["label"][1])]
        self.hotspots = tuple(spec.get("hotspots") or ())

    def contains(self, lat: float, lon: float) -> bool:
        w, s, e, n = self.bbox
        if not (s <= lat <= n and w <= lon <= e):
            return False
        return geo.point_in_ring(lon, lat, self.ring)

    def geometry(self) -> dict:
        return {"type": "Polygon", "coordinates": [self.ring]}


REGIONS: tuple[Region, ...] = tuple(Region(r) for r in D.REGIONS)
REGION_BY_ID = {r.id: r for r in REGIONS}


def regions_at(lat: float, lon: float) -> list[str]:
    return [r.id for r in REGIONS if r.contains(lat, lon)]


def place_name(lat: float, lon: float) -> str:
    """A readable name for a spot: a GPS-sampler area, a watched region, or the coordinates."""
    best = None
    for sid, name, hlat, hlon, radius_nm in HOTSPOTS:
        d = geo.haversine_km(lat, lon, hlat, hlon)
        if d <= radius_nm * 1.852 and (best is None or d < best[0]):
            best = (d, name)
    if best:
        return best[1]
    for r in REGIONS:
        if r.contains(lat, lon):
            return r.name
    ns = f"{abs(lat):.1f}° {'N' if lat >= 0 else 'S'}"
    ew = f"{abs(lon):.1f}° {'E' if lon >= 0 else 'W'}"
    return f"{ns}, {ew}"


# ---------------------------------------------------------------------------------------------
# military surges
# ---------------------------------------------------------------------------------------------
MIL_WINDOW_H = 3
MIL_MIN_DAYS = 7
MIL_DAY = stats.SeasonalBaseline(period="day", window_hours=MIL_WINDOW_H, periods=28, min_samples=MIL_MIN_DAYS,
                                 min_coverage=MIN_COVERAGE, scale=False)
MIL_WEEK = stats.SeasonalBaseline(period="week", window_hours=MIL_WINDOW_H, periods=4, min_samples=2,
                                  min_coverage=MIN_COVERAGE, scale=False)
FEED_LOW, FEED_HIGH = 0.6, 1.6           # worldwide military count outside this share of usual: the feed changed
TRAFFIC_HIGH, TRAFFIC_LOW = 1.4, 0.6     # regional all-traffic share: receivers gained / traffic or receivers lost
MIL_MULTIPLE_MIN_USUAL = 1.0             # aircraft-hours in the 3-hour window: below this no multiple is given
MIL_WEEK_VETO = 2.0                      # second week: under twice the same window a week ago is not a surge
MIL_INNOCENT = (
    "Exercises, which are often announced in advance and can bring dozens of aircraft for days.",
    "Deployments in transit: fighters crossing with tankers, or transports moving equipment.",
    "Air shows, flypasts and ceremonies.",
    "Receiver coverage: a new volunteer receiver near a base makes more aircraft visible.",
    "Most military aircraft never broadcast ADS-B, so this count is never the whole picture.",
)
MIL_LIMITS = ("Only military aircraft that broadcast ADS-B and are heard by volunteer receivers (adsb.fi), "
              "flagged military in the community database. Trainers and helicopters are left out: their flying "
              "follows base timetables. Counts are aircraft-hours from one snapshot every 10 minutes.")


def _level_check(hourly: dict, uptime: dict | None, end: float) -> dict | None:
    """The same window on earlier days for a control series (worldwide feed size, regional traffic)."""
    if not hourly:
        return None
    r = MIL_DAY.assess(hourly, end, uptime=uptime)
    if not r["ready"] or not r["median"]:
        return None
    share = r["value"] / r["median"] if r["median"] > 0 else None
    return {"value": r["value"], "usual": r["median"], "share": share, "days": r["n"]}


def surge_usual(hourly: dict, end: float, *, uptime=None) -> tuple[float | None, str | None, int]:
    """The usual aircraft-hours for the window [end - 3 h, end) as ``assess_surge`` uses it, for cards
    and charts: ``(usual, profile, n)`` with the median of the same window on the same weekday once 2
    such weeks exist ("week"), otherwise on the previous 7 to 28 days ("day"); ``(None, None, n)``
    while the day baseline is still learning."""
    day, _ = MIL_DAY.sample(hourly, end, uptime=uptime)
    if len(day) < MIL_MIN_DAYS:
        return None, None, len(day)
    week, _ = MIL_WEEK.sample(hourly, end, uptime=uptime)
    if len(week) >= MIL_WEEK.min_samples:
        return stats.median(week), "week", len(week)
    return stats.median(day), "day", len(day)


def assess_surge(hourly: dict, uptime: dict, end: float, *, distinct: int = 0,
                 roles_now: dict | None = None, roles_usual: dict | None = None,
                 world: dict | None = None, world_uptime: dict | None = None,
                 traffic: dict | None = None, traffic_uptime: dict | None = None,
                 has_traffic_check: bool = True, seeded: Iterable[int] = ()) -> dict:
    """Military aircraft-hours in [end - 3 h, end) for one region against its own history.

    ``hourly`` {hour_start: counted aircraft that hour}; ``uptime`` {hour_start: snapshots / 6};
    ``world`` the worldwide military count per snapshot (feed-health check); ``traffic`` the mean
    all-traffic count in the region's GPS-sampler circles (receiver-coverage check); ``seeded``
    hour starts that came from the replay history rather than the sampler.

    Baseline: the same window on the previous 7 to 28 days; from two weeks, the same window on the same
    weekday on its own (like with like: a weekly training day is usual on its weekday, and a Saturday is
    compared with Saturdays, not with busier weekdays). In the second week, when only one same-weekday
    window exists, a value under twice that window is held back to "notable" (a weekly schedule).
    ``ratio`` is None when the usual is under one aircraft-hour in the window: a multiple of almost
    nothing says little, so the wording gives the count instead."""
    day = MIL_DAY.assess(hourly, end, uptime=uptime)
    week = MIL_WEEK.assess(hourly, end, uptime=uptime)
    value = float(day["value"])
    out: dict[str, Any] = {
        "window": {"from": end - MIL_WINDOW_H * HOUR, "to": end, "hours": MIL_WINDOW_H},
        "value": value, "per_hour": value / MIL_WINDOW_H, "distinct": int(distinct),
        "coverage": round(day["coverage"], 3), "baseline_days": day["n"], "needed": MIL_MIN_DAYS,
        "dropped": day["dropped"], "usual": None, "usual_per_hour": None, "ratio": None, "z": None,
        "surprise": None, "method": None, "profile": None, "profile_text": None, "last_week": None, "state": "learning",
        "severity": 0, "confidence": None, "confidence_reasons": [], "checks": {}, "roles": {},
        "status": "", "evidence": "", "innocent": list(MIL_INNOCENT), "limits": MIL_LIMITS,
    }
    for role in D.ROLES:
        now_n = (roles_now or {}).get(role, 0)
        usual_n = (roles_usual or {}).get(role)
        if now_n or usual_n:
            out["roles"][role] = {"label": D.ROLE_LABELS[role], "now": now_n, "usual": usual_n,
                                  "counted": counted(role)}
    if day["n"] < MIL_MIN_DAYS:
        out["status"] = learning_text(day["n"], MIL_MIN_DAYS)
        now_text = (f"{num_text(out['per_hour'])} military aircraft an hour on average over the last 3 hours. "
                    if day["coverage"] >= MIN_COVERAGE else "")
        out["evidence"] = f"{now_text}{out['status']}: nothing is flagged until a week of history exists."
        return out
    if day["coverage"] < MIN_COVERAGE:
        out["state"] = "no_data"
        out["status"] = "Not enough snapshots in the last 3 hours (Meridian was off or the military feed failed)"
        out["evidence"] = out["status"] + "."
        return out

    # like with like: the same weekday on its own once two weeks exist, otherwise every earlier day
    base = week if week["ready"] else day
    usual, z, ratio, method = base["median"], base["z"], base["ratio"], base["method"]
    if week["ready"]:
        profile = "week"
        wd = WEEKDAYS[int(((end - HOUR) // DAY + 3) % 7)]
        profile_text = f"this time on {wd} (median of {week['n']} weeks)"
    else:
        profile = "day"
        profile_text = f"this time of day (median of {day['n']} days)"
    robust = method == "robust z"
    surprise = None
    if not robust:
        # Quasi-Poisson: an aircraft present for 2 hours counts twice, so scale both sides by the
        # window's aircraft-hours per aircraft before the Poisson tail (otherwise it is overconfident).
        d = max(1.0, value / distinct) if distinct else 1.0
        lam = max(1e-6, (base["mean"] or 0.0) / d)
        surprise = stats.poisson_surprise(int(round(value / d)), lam, "high")
        z = stats.p_to_z(10 ** (-surprise)) if surprise < 300 else 37.0
    # ``ratio`` (from the shared helper) is taken against at least 0.5: fine for the rules, but no
    # multiple is published when the usual is under one aircraft-hour in the window
    multiple = ratio if usual >= MIL_MULTIPLE_MIN_USUAL else None
    out.update(usual=usual, usual_per_hour=usual / MIL_WINDOW_H, ratio=multiple, z=z, surprise=surprise,
               method=method, profile=profile, profile_text=profile_text)

    if robust:
        unusual = value >= 8 and distinct >= 4 and ratio >= 2.0 and z >= 3.0
        notable = value >= 6 and ratio >= 1.5 and z >= 2.0
    else:
        unusual = value >= 10 and distinct >= 5 and ratio >= 3.0 and surprise >= 3.0
        notable = value >= 6 and ratio >= 2.0 and surprise >= 2.0

    # Second week: no same-weekday baseline yet, but the same window a week ago can show a weekly
    # schedule (a training day). Under twice that level, a surge is held back to "busier than usual".
    held_back = False
    if not week["ready"]:
        lw_from, lw_to = MIL_WEEK.windows(end)[0]
        lw_total, lw_cov = stats.window_total(hourly, lw_from, lw_to, uptime=uptime)
        if lw_cov >= MIN_COVERAGE:
            out["last_week"] = float(lw_total)
            if unusual and value < MIL_WEEK_VETO * max(float(lw_total), 0.5):
                held_back = True
                unusual, notable = False, True

    # coverage controls
    feed = _level_check(world or {}, world_uptime, end) if world else None
    traffic_chk = _level_check(traffic or {}, traffic_uptime, end) if traffic else None
    out["checks"] = {"feed": feed, "receivers": traffic_chk, "receivers_available": bool(has_traffic_check)}
    suppressed = None
    if feed and feed["share"] is not None and not (FEED_LOW <= feed["share"] <= FEED_HIGH):
        suppressed = ("feed_changed", f"The worldwide military feed is {num_text(feed['share'] * 100, 0)}% of its "
                                      "usual size, so regional counts are not comparable right now")
    elif traffic_chk and traffic_chk["share"] is not None and traffic_chk["share"] > TRAFFIC_HIGH:
        suppressed = ("coverage_changed", f"All traffic in this region is {num_text(traffic_chk['share'] * 100, 0)}% "
                                          "of usual: receiver coverage may have grown, which also shows more "
                                          "military aircraft")

    seeded_hours = set(seeded or ())
    seeded_share = 0.0
    if seeded_hours:
        base_hours = [int(s) + k * HOUR for s, _e in MIL_DAY.windows(end) for k in range(MIL_WINDOW_H)]
        seeded_share = sum(1 for h in base_hours if h in seeded_hours) / max(1, len(base_hours))

    if unusual and suppressed is None:
        state = "unusual"
    elif (unusual or notable) and suppressed is not None:
        state = suppressed[0]
    elif notable:
        state = "notable"
    else:
        state = "normal"
    out["state"] = state

    # confidence
    level = 2
    reasons = ([f"same weekday over {week['n']} weeks ({day['n']} days of history)"] if profile == "week"
               else [f"{day['n']} days of baseline"])
    strong = (z is not None and z >= 4.0) if robust else (surprise is not None and surprise >= 5.0)
    if (day["n"] >= 14 or profile == "week") and strong:
        level = 3
    if profile == "week" and week["n"] < 3:
        level = min(level, 2)
        reasons.append("the same-weekday baseline has only 2 weeks")
    if profile != "week":
        reasons.append("compared with every day; weekly patterns such as training days are learned after 2 weeks")
    if held_back:
        reasons.append("the same 3 hours a week ago were at a similar level, which suggests a weekly schedule")
    if seeded_share > 0.5:
        level = min(level, 2)
        reasons.append("most of the baseline comes from earlier map use, not the sampler")
    if not has_traffic_check:
        level = min(level, 2)
        reasons.append("no receiver-coverage check for this region")
    elif traffic_chk is None:
        level = min(level, 2)
        reasons.append("the receiver-coverage check is still learning")
    else:
        share_pct = num_text(traffic_chk["share"] * 100, 0)
        if traffic_chk["share"] < TRAFFIC_LOW:
            reasons.append(f"civil traffic is {share_pct}% of usual (airspace closures or receivers offline)")
        elif traffic_chk["share"] <= TRAFFIC_HIGH:
            reasons.append("receiver coverage looks normal")
    if not robust:
        level = min(level, 2)
        reasons.append("small numbers")
    if distinct < 6:
        level = min(level, 1)
        reasons.append(f"only {distinct} different aircraft")
    out["confidence"] = {3: "high", 2: "medium", 1: "low"}[level]
    out["confidence_reasons"] = reasons

    if state == "unusual":
        sev = 4
        if ratio >= 3.0 and ((robust and z >= 4.0) or (not robust and surprise >= 5.0)):
            sev = 5
        if ratio >= 4.0 and value >= 20 and ((robust and z >= 5.0) or (not robust and surprise >= 6.0)):
            sev = 6
        out["severity"] = sev

    span = f"{utc_hm(end - MIL_WINDOW_H * HOUR)} to {utc_hm(end)} UTC"
    per_hour = num_text(out["per_hour"])
    if multiple is None:
        usual_words = "none" if usual <= 0 else "under 1 aircraft-hour in these 3 hours"
        more = f"{per_hour} an hour where usually there are {'none' if usual <= 0 else 'almost none'}"
    else:
        usual_words = f"{num_text(out['usual_per_hour'])} an hour"
        more = times_text(multiple)
    out["evidence"] = (f"{per_hour} military aircraft an hour on average over the last 3 hours "
                       f"({span}), {distinct} different aircraft. Usual for {profile_text} is {usual_words}"
                       + (f", so {times_text(multiple)}" if multiple is not None and multiple >= 1.15 else "") + ".")
    if out["last_week"] is not None and (held_back or state == "unusual"):
        out["evidence"] += (f" The same 3 hours a week ago had {num_text(out['last_week'] / MIL_WINDOW_H)} an hour"
                            + (", so this may be a weekly schedule (a same-weekday baseline needs 2 weeks)."
                               if held_back else "."))
    if state == "unusual":
        out["status"] = f"Unusual: {more}"
    elif state == "notable":
        out["status"] = ("Busier than usual for the time of day, but like the same hours a week ago" if held_back
                         else f"Busier than usual: {more}")
    elif suppressed is not None and state == suppressed[0]:
        out["status"] = suppressed[1]
    elif multiple is None:
        out["status"] = "Usual levels" if value < MIL_MULTIPLE_MIN_USUAL else "Within the usual range"
    else:
        out["status"] = "Usual levels" if multiple < 1.15 else f"Within the usual range ({times_text(multiple)})"
    return out


def surge_words(st: dict, over: str) -> tuple[str, str]:
    """(title, subtitle) of a surge event, led by the count: 'More military aircraft than usual over
    the Baltic Sea: 16 an hour, usually 4.5' and '3.6 times the usual over the last 3 hours, ...'."""
    if st.get("ratio") is not None:
        usual = f"usually {num_text(st['usual_per_hour'])}"
        sub = f"{times_text(st['ratio'])} over the last 3 hours"
    else:
        usual = "usually none" if not st.get("usual") else "usually almost none"
        sub = f"{num_text(st['value'], 0)} aircraft-hours over the last 3 hours"
    title = f"More military aircraft than usual {over}: {num_text(st['per_hour'])} an hour, {usual}"
    sub += f", {st['distinct']} different aircraft; confidence {st['confidence']}"
    return title, sub


def region_label_text(st: dict, level: int) -> str:
    """Map label of a region: its name, plus for flagged regions the count an hour and the multiple."""
    if level < 2:
        return st["name"]
    text = f"{st['name']} {num_text(st['per_hour'])}/h"
    return text + (f" ({num_text(st['ratio'])}×)" if st.get("ratio") is not None else "")


def drivers_text(roles: dict) -> str | None:
    """'Most are tankers (6, usually 1) and transports (4, usually 3).'"""
    items = [(r, v) for r, v in roles.items() if v.get("counted") and v.get("now")]
    if not items:
        return None
    items.sort(key=lambda kv: -((kv[1]["now"] or 0) - (kv[1]["usual"] or 0)))
    parts = []
    for role, v in items[:3]:
        usual = v.get("usual")
        word = D.ROLE_LABELS[role].lower()
        parts.append(f"{word} {v['now']}" + (f" (usually {num_text(usual)})" if usual is not None else ""))
    return "Aircraft-hours by role: " + ", ".join(parts) + "."


# ---------------------------------------------------------------------------------------------
# GPS interference trends
# ---------------------------------------------------------------------------------------------
GPS_RECENT_H = 6                 # the recent window: the last 6 complete hours
GPS_BASE_DAYS = 7                # compared with the same 6 clock hours on each of the 7 days before it
# Of those 7 windows, this many need samples before "new" or "worse" can be said: a cell affected on 4
# days a week shows 5 quiet days in a row by chance about 1.5% of the time (3 days: 8%). Two missing
# days (restarts, nights with too few aircraft) are allowed.
GPS_MIN_DAYS = 5
GPS_DAY_MIN_TOTAL = 12           # aircraft-hours for an earlier window (or a whole day) to count
GPS_RECENT_MIN_TOTAL = 12
GPS_NEW_SHARE = 0.10             # recent share that counts as interference
GPS_QUIET_SHARE = 0.02           # share under which a window or a day was quiet
GPS_MIN_BAD = 4
GPS_MIN_SHARED_HOURS = 2         # hours of the recent window with 2 or more affected aircraft
GPS_CHRONIC_DAYS = 4
GPS_WORSE_FACTOR = 2.0           # worse: at least twice the highest share in the same hours on any earlier day
GPS_WORSE_MARGIN = 0.05          # ... and at least 5 points above it
GPS_INNOCENT = (
    "Some older avionics always report low accuracy; one such aircraft flying a regular route can move a cell.",
    "Announced GPS testing and military exercises (often published as navigation warnings).",
    "Strong geomagnetic storms degrade GPS at high latitudes.",
    "A few receivers misreport accuracy fields.",
)
GPS_LIMITS = ("Inferred from the navigation accuracy aircraft report about themselves (ADS-B NACp and NIC), not "
              "a measurement of radio signals. Spoofing that keeps reported accuracy high is not seen, and "
              "cells far from GPS-sampler circles only have data while you view them.")


@functools.lru_cache(maxsize=65536)
def in_sampler_circle(lat: float, lon: float) -> bool:
    """True when the point lies inside one of the GPS sampler's circles, which are sampled in the
    background whether or not the map shows them."""
    return any(geo.haversine_km(lat, lon, hlat, hlon) <= radius_nm * 1.852 for _s, _n, hlat, hlon, radius_nm in HOTSPOTS)


def _gps_affected(total: int, bad: int) -> bool:
    """An earlier window or day is affected at the layer's amber level (2% or more of 12 or more
    aircraft-hours, at least 2 affected)."""
    return total >= GPS_DAY_MIN_TOTAL and bad >= 2 and bad / total >= GPS_QUIET_SHARE


def classify_gps(recent_total: int, recent_bad: int, windows: list[tuple[int, int]],
                 days: list[tuple[int, int]] | None = None, *, shared_hours: int | None = None,
                 sampled: bool | None = None) -> dict:
    """Trend of one cell against its own previous 7 days at the same time of day.

    ``windows``: (aircraft-hours, affected) in the same 6 clock hours as the recent window on each of
    the 7 days before it, most recent first (missing as (0, 0)). ``days``: the same for the 7 whole
    24-hour blocks before the recent window (each starts with its window), so that a cell affected every
    day at other hours still reads as repeating daily (``None``: the windows stand in). ``shared_hours``:
    hours of the recent window with 2 or more affected aircraft (``None``: not checked). ``sampled``:
    True when the GPS sampler covers the cell in the background, False when its data comes only from
    the hours the map showed it (sets the confidence of new and worse cells).

    Affected now: 10% or more of 12 or more aircraft-hours, 4 or more affected, with 2 or more affected
    aircraft in the same hour in at least 2 hours (one aircraft with degraded avionics circling a quiet
    cell cannot do it on its own).
    new: affected now after a quiet week: under 2% in the same hours over at least 5 days with data,
    and no window and no whole day affected. worse: affected now, at least twice the highest share in the
    same hours on any earlier day and 5 points above it, and more than even that day's share would give
    by chance (Poisson, 1 in 1,000): judged against the worst day, so the spread between days counts,
    not single aircraft-hours. chronic: the same hours affected on 4 or more of the days, or the cell
    affected (at any hour) on 4 or more of the 7 days; a chronic cell is only "worse" at twice its worst
    day. recurring: affected on 1 to 3 days. easing: chronic in these hours but under 2% now (with
    enough samples to say so). learning: fewer than 5 days with data in these hours, and no daily
    pattern to report. With too few samples now, the label comes from the week alone."""
    windows = [(int(t), int(b)) for t, b in list(windows)[:GPS_BASE_DAYS]]
    wdays = [(t, b) for t, b in windows if t >= GPS_DAY_MIN_TOTAL]
    base_total = sum(t for t, _ in windows)
    base_bad = sum(b for _, b in windows)
    share_base = base_bad / base_total if base_total else None
    days_affected = sum(1 for t, b in wdays if _gps_affected(t, b))
    days_red = sum(1 for t, b in wdays if b >= 3 and b / t >= GPS_NEW_SHARE)
    max_share = max((b / t for t, b in wdays), default=None)
    if days is None:
        any_with, any_affected = len(wdays), days_affected
    else:
        whole = [(int(t), int(b)) for t, b in list(days)[:GPS_BASE_DAYS]]
        any_with = sum(1 for t, _ in whole if t >= GPS_DAY_MIN_TOTAL)
        any_affected = sum(1 for t, b in whole if _gps_affected(t, b))
    share_now = recent_bad / recent_total if recent_total else None
    judged_now = recent_total >= GPS_RECENT_MIN_TOTAL
    heavy = judged_now and share_now is not None and share_now >= GPS_NEW_SHARE and recent_bad >= GPS_MIN_BAD
    single = heavy and shared_hours is not None and shared_hours < GPS_MIN_SHARED_HOURS
    affected_now = heavy and not single
    enough = len(wdays) >= GPS_MIN_DAYS
    out = {"recent_total": int(recent_total), "recent_bad": int(recent_bad), "share_now": share_now,
           "shared_hours": shared_hours, "one_aircraft_at_a_time": single,
           "base_total": int(base_total), "base_bad": int(base_bad), "share_base": share_base,
           "windows": [[t, b] for t, b in windows],
           "days_with_data": len(wdays), "days_affected": days_affected, "days_red": days_red,
           "max_daily_share": max_share, "days_any_with_data": any_with, "days_affected_any": any_affected,
           "surprise": None, "judged_now": judged_now, "label": "quiet", "confidence": None,
           "confidence_reasons": []}
    if affected_now and enough:
        ref = max(max_share or 0.0, share_base or 0.0)
        out["surprise"] = stats.poisson_surprise(int(recent_bad), recent_total * max(ref, 0.005), "high")
        quiet_week = ((share_base or 0.0) < GPS_QUIET_SHARE and days_affected == 0 and any_affected == 0
                      and base_total >= 30)
        worse = (share_now >= GPS_WORSE_FACTOR * ref and share_now >= (max_share or 0.0) + GPS_WORSE_MARGIN
                 and out["surprise"] >= 3.0)
        if quiet_week:
            out["label"] = "new"
        elif worse:
            out["label"] = "worse"
        elif days_affected >= GPS_CHRONIC_DAYS or any_affected >= GPS_CHRONIC_DAYS:
            out["label"] = "chronic"
        else:
            out["label"] = "recurring"
    elif days_affected >= GPS_CHRONIC_DAYS:
        out["label"] = "easing" if judged_now and (share_now or 0.0) < GPS_QUIET_SHARE else "chronic"
    elif any_affected >= GPS_CHRONIC_DAYS:
        out["label"] = "chronic"
    elif not enough and any_with < GPS_MIN_DAYS:
        out["label"] = "learning"
    elif days_affected or any_affected:
        out["label"] = "recurring"
    elif not enough:
        out["label"] = "learning"
    if out["label"] in ("new", "worse"):
        level = 3 if len(wdays) >= GPS_BASE_DAYS else 2
        reasons = [f"{len(wdays)} of {GPS_BASE_DAYS} days with enough aircraft in these hours "
                   f"({base_total:,} aircraft-hours)"]
        if sampled is True:
            reasons.append("inside a GPS-sampler circle, sampled in the background every 5 to 10 minutes")
        elif sampled is False:
            level -= 1
            reasons.append("outside the GPS-sampler circles: data only from the hours the map showed this area")
        else:
            level = min(level, 2)
        out["confidence"] = {3: "high", 2: "medium", 1: "low"}[level]
        out["confidence_reasons"] = reasons
    return out


GPS_CATEGORY = {"new": "new_interference", "worse": "new_interference", "chronic": "chronic_interference",
                "recurring": "chronic_interference", "easing": "chronic_interference"}


def trend_text(t: dict) -> str:
    """One sentence for a cell's trend label. ``t["window"]`` ({from, to} epoch seconds), when given,
    names the recent hours."""
    label = t.get("label")
    now = t.get("share_now")
    base = t.get("share_base")
    pct = lambda v: f"{num_text((v or 0) * 100)}%"  # noqa: E731
    days = t.get("days_with_data") or 0
    hit = t.get("days_affected") or 0
    hit_any = t.get("days_affected_any") or 0
    win = t.get("window") or {}
    span = f" ({utc_hm(win['from'])} to {utc_hm(win['to'])} UTC)" if win.get("from") is not None else ""
    recent = f"the last {GPS_RECENT_H} complete hours{span}"
    same = (f"the same hours on {days} earlier {'day' if days == 1 else 'days'} with data "
            f"({t.get('base_total') or 0:,} aircraft-hours)")
    one = (" The affected aircraft-hours come from one aircraft at a time, which older avionics can explain, so they "
           "are not counted as interference." if t.get("one_aircraft_at_a_time") else "")
    if label == "learning":
        return f"Learning: {days} of {GPS_MIN_DAYS} days with enough aircraft in these hours to judge a trend.{one}"
    if label == "new":
        return f"New: {pct(now)} of aircraft affected in {recent}, against {pct(base)} in {same}; no day was affected."
    if label == "worse":
        text = (f"Worse: {pct(now)} affected in {recent}, against {pct(base)} in {same}, at most "
                f"{pct(t.get('max_daily_share'))} on any of them.")
        if hit_any and not hit:
            text += f" It was affected at other hours on {hit_any} of the last {GPS_BASE_DAYS} days."
        return text
    if label == "chronic":
        if hit >= GPS_CHRONIC_DAYS:
            return (f"Repeats daily: affected in these hours on {hit} of the last {days} days. Chronic cells raise no "
                    f"new events.{one}")
        where = f", {hit} of them in these hours" if hit else ", at other hours than now"
        return (f"Repeats daily: affected on {hit_any} of the last {GPS_BASE_DAYS} days{where}. Chronic cells raise no "
                f"new events.{one}")
    if label == "recurring":
        if hit:
            return f"Comes and goes: affected in these hours on {hit} of the last {days} days.{one}"
        return f"Comes and goes: affected on {hit_any} of the last {GPS_BASE_DAYS} days, at other hours than now.{one}"
    if label == "easing":
        return f"Easing: {pct(now)} affected in {recent}, against {pct(base)} in {same}."
    return f"Quiet: {pct(now)} affected in {recent}, {pct(base)} in {same}.{one}"


def gps_clusters(trends: dict[tuple[int, int], dict], cell_deg: float) -> list[dict]:
    """Group adjacent new and worse cells (8-neighbour) into clusters, largest first."""
    flagged = {k for k, t in trends.items() if t.get("label") in ("new", "worse")}
    seen: set[tuple[int, int]] = set()
    out = []
    for start in sorted(flagged):
        if start in seen:
            continue
        stack = [start]
        members = []
        seen.add(start)
        while stack:
            r, c = stack.pop()
            members.append((r, c))
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nb = (r + dr, c + dc)
                    if nb in flagged and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        members.sort()
        total = sum(trends[m]["recent_total"] for m in members)
        bad = sum(trends[m]["recent_bad"] for m in members)
        btot = sum(trends[m]["base_total"] for m in members)
        bbad = sum(trends[m]["base_bad"] for m in members)
        w = sum(max(1, trends[m]["recent_bad"]) for m in members)
        lat = sum((-90.0 + (m[0] + 0.5) * cell_deg) * max(1, trends[m]["recent_bad"]) for m in members) / w
        lon = sum((-180.0 + (m[1] + 0.5) * cell_deg) * max(1, trends[m]["recent_bad"]) for m in members) / w
        rows = [m[0] for m in members]
        cols = [m[1] for m in members]
        bbox = [-180.0 + min(cols) * cell_deg, -90.0 + min(rows) * cell_deg,
                -180.0 + (max(cols) + 1) * cell_deg, -90.0 + (max(rows) + 1) * cell_deg]
        labels = {trends[m]["label"] for m in members}
        change = "new" if labels == {"new"} else ("worse" if labels == {"worse"} else "new and worse")
        share_max = max(trends[m]["share_now"] or 0.0 for m in members)
        # the cluster's own earlier days: the members' same-hours windows added up day by day
        wins = [trends[m].get("windows") for m in members]
        if all(wins):
            per_day = [(sum(w[k][0] for w in wins), sum(w[k][1] for w in wins)) for k in range(GPS_BASE_DAYS)]
            with_data = [(t, b) for t, b in per_day if t >= GPS_DAY_MIN_TOTAL]
            days_with = len(with_data)
            day_max = max((b / t for t, b in with_data), default=None)
        else:
            days_with = max(trends[m].get("days_with_data") or 0 for m in members)
            day_max = max((trends[m].get("max_daily_share") or 0.0) for m in members)
        # confidence: the best-founded member cell (thin or viewed-only baselines lower it)
        best = max(members, key=lambda m: (CONFIDENCE_LEVEL.get(trends[m].get("confidence"), 0), trends[m]["recent_bad"]))
        conf = trends[best].get("confidence")
        sev = 5
        if conf == "high" and share_max >= 0.3 and bad >= 8:
            sev = 6
        elif conf == "low":
            sev = 4
        out.append({"cells": members, "n": len(members), "lat": round(lat, 5), "lon": round(lon, 5),
                    "bbox": [round(v, 5) for v in bbox], "change": change, "recent_total": total, "recent_bad": bad,
                    "share_now": bad / total if total else 0.0, "share_max": share_max,
                    "share_base": bbad / btot if btot else None, "base_total": btot, "days_with_data": days_with,
                    "max_daily_share": day_max, "confidence": conf,
                    "confidence_reasons": list(trends[best].get("confidence_reasons") or ()), "severity": sev,
                    "place": place_name(lat, lon)})
    out.sort(key=lambda c: (-c["n"], -c["recent_bad"]))
    return out


CONFIDENCE_LEVEL = {"high": 3, "medium": 2, "low": 1}


def gps_cluster_words(c: dict) -> tuple[str, str]:
    """(title, subtitle) for a cluster, with the real number of earlier days and aircraft-hours."""
    word = {"new": "New", "worse": "Worse", "new and worse": "New or worse"}[c["change"]]
    title = f"{word} GPS interference: {c['place']}"
    cells = "1 cell" if c["n"] == 1 else f"{c['n']} cells"
    base = c.get("share_base")
    days = c.get("days_with_data") or 0
    sub = (f"{num_text(c['share_now'] * 100)}% of aircraft affected in the last {GPS_RECENT_H} h ({cells}); "
           f"{num_text((base or 0) * 100)}% in the same hours on {days} earlier {'day' if days == 1 else 'days'} "
           f"with data ({c.get('base_total') or 0:,} aircraft-hours)")
    if c.get("confidence"):
        sub += f"; confidence {c['confidence']}"
    return title, sub


# ---------------------------------------------------------------------------------------------
# airport movement drops
# ---------------------------------------------------------------------------------------------
APT_MIN_DAYS = 7
APT_DAY = stats.SeasonalBaseline(period="day", window_hours=1, periods=14, min_samples=APT_MIN_DAYS,
                                 min_coverage=MIN_COVERAGE, scale=False)
APT_WEEK = stats.SeasonalBaseline(period="week", window_hours=1, periods=4, min_samples=2,
                                  min_coverage=MIN_COVERAGE, scale=False)
APT_MIN_USUAL = 10               # movements an hour: quieter hours are never judged
APT_DROP_SHARE = 0.4             # observed at or under this share of usual
APT_NEAR_ZERO = 0.15
APT_HIGH_MIN = 3                 # overflights an hour needed for the receiver check
APT_INNOCENT = (
    "Schedule changes, public holidays and seasonal timetables.",
    "Runway works, a change of runway in use or night restrictions.",
    "Weather that diverts or delays flights.",
    "A receiver near the airport going offline: low aircraft are only seen by nearby receivers.",
)
APT_LIMITS = ("Counted from ADS-B snapshots every 10 minutes within the airport's circle: aircraft descending "
              "below 12,000 ft count as arriving and climbing as departing, so a busy hour reads about three "
              "quarters of its real movements. Aircraft on the ground or below about 3,000 ft are only seen if a "
              "receiver is close by. Airports have background coverage only inside the GPS-sampler circles, or "
              "when you favourite them; elsewhere only while you view them.")


def airport_usual(moves: dict, end: float, *, uptime=None) -> float | None:
    """The usual movements for the hour [end - 1 h, end) as ``assess_airport`` uses it (the same
    weekday once 2 weeks exist, otherwise the same hour on 7 to 14 earlier days); None while learning."""
    day, _ = APT_DAY.sample(moves, end, uptime=uptime)
    if len(day) < APT_MIN_DAYS:
        return None
    week, _ = APT_WEEK.sample(moves, end, uptime=uptime)
    if len(week) >= APT_WEEK.min_samples:
        return stats.median(week)
    return stats.median(day)


def _apt_hour(moves: dict, high: dict, uptime: dict, end: float) -> dict:
    """One complete hour [end - 1 h, end), against the same hour on the same weekday once two weeks
    exist (like with like), otherwise on every earlier day."""
    d = APT_DAY.assess(moves, end, uptime=uptime, direction="low")
    w = APT_WEEK.assess(moves, end, uptime=uptime, direction="low")
    hd = APT_DAY.assess(high, end, uptime=uptime) if high else None
    obs = float(d["value"])
    out = {"t": end - HOUR, "value": obs, "coverage": d["coverage"], "days": d["n"], "usual": None, "share": None,
           "z": None, "p": None, "profile": "day", "profile_n": d["n"], "state": "learning", "high": None,
           "high_usual": None, "receivers": "unavailable"}
    if hd is not None:
        out["high"] = hd["value"]
        if hd["ready"]:
            out["high_usual"] = hd["median"]
    if d["n"] < APT_MIN_DAYS:
        return out
    if d["coverage"] < MIN_COVERAGE:
        out["state"] = "no_coverage"
        return out
    base = w if w["ready"] else d
    usual, z = base["median"], base["z"]
    if w["ready"]:
        out["profile"] = "week"
        out["profile_n"] = w["n"]
    out["usual"] = usual
    out["z"] = z
    out["share"] = obs / usual if usual else None
    if usual < APT_MIN_USUAL:
        out["state"] = "quiet_hour"
        return out
    p = stats.poisson_cdf(int(round(obs)), usual)
    out["p"] = p
    if out["high_usual"] is not None and out["high_usual"] >= APT_HIGH_MIN:
        out["receivers"] = "ok" if (out["high"] or 0) >= 0.5 * out["high_usual"] else "low"
    low = out["share"] is not None and out["share"] <= APT_DROP_SHARE and p < 0.01 and (z is None or z <= -2.5)
    out["state"] = "low" if low else "normal"
    return out


def assess_airport(moves: dict, high: dict, uptime: dict, end: float) -> dict:
    """Movements in the last two complete hours (ending at ``end``, an hour start) against usual."""
    h1 = _apt_hour(moves, high, uptime, end)
    h2 = _apt_hour(moves, high, uptime, end - HOUR)
    out: dict[str, Any] = {"hours": [h2, h1], "state": h1["state"], "severity": 0, "confidence": None,
                           "value": h1["value"], "usual": h1["usual"], "share": h1["share"],
                           "coverage": h1["coverage"], "days": h1["days"], "needed": APT_MIN_DAYS,
                           "profile": h1["profile"], "receivers": h1["receivers"], "status": "", "evidence": "",
                           "innocent": list(APT_INNOCENT), "limits": APT_LIMITS}
    pct = lambda v: f"{num_text((v or 0) * 100, 0)}%"  # noqa: E731
    if h1["state"] == "no_coverage":
        out["status"] = "Not enough snapshots in the last hour to judge"
    elif h1["state"] == "learning":
        out["status"] = learning_text(h1["days"], APT_MIN_DAYS)
    elif h1["state"] == "quiet_hour":
        out["status"] = "A quiet hour here: too few usual movements to judge"
        out["state"] = "normal"
    elif h1["state"] == "normal":
        out["status"] = "Usual movements" if (h1["share"] or 0) >= 0.7 else f"{pct(h1['share'])} of the usual movements"
    else:
        both = h2["state"] == "low"
        receivers_ok = h1["receivers"] == "ok" and (not both or h2["receivers"] == "ok")
        if both and receivers_ok:
            out["state"] = "drop"
            near_zero = (h1["share"] or 0) <= APT_NEAR_ZERO and (h2["share"] or 0) <= APT_NEAR_ZERO
            out["severity"] = 5 if near_zero else 4
            out["confidence"] = "high" if near_zero and h1["profile"] == "week" else "medium"
            out["status"] = f"Far fewer movements than usual: {pct(h1['share'])} of usual for 2 hours"
        elif both:
            out["state"] = "drop_unverified"
            out["severity"] = 3
            out["confidence"] = "low"
            out["status"] = ("Far fewer aircraft than usual, and fewer overflights too: the airspace may be closed or "
                             "the receivers covering it offline" if h1["receivers"] == "low" else
                             "Far fewer movements than usual; receiver coverage here cannot be checked")
        else:
            out["state"] = "notable"
            out["status"] = f"Fewer movements than usual in the last hour: {pct(h1['share'])} of usual"
    if h1["usual"] is not None:
        if h1["profile"] == "week":
            prof = f"this hour on {WEEKDAYS[int((h1['t'] // DAY + 3) % 7)]} (median of {h1['profile_n']} weeks)"
        else:
            prof = f"this hour of the day (median of {h1['profile_n']} days)"
        out["evidence"] = (f"{num_text(h1['value'], 0)} arrivals and departures seen in the last complete hour "
                           f"({utc_hm(h1['t'])} to {utc_hm(h1['t'] + HOUR)} UTC); usual for {prof} is "
                           f"{num_text(h1['usual'], 0)}.")
        if h1["receivers"] == "ok":
            out["evidence"] += " Overflights look normal, so receivers are working."
        elif h1["receivers"] == "low":
            out["evidence"] += " Overflights are also far below usual."
    else:
        out["evidence"] = out["status"] + "."
    return out
