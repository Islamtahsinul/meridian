"""When the daily briefing is due: once per local calendar day, at the first check after
``BRIEFING_TIME`` (local machine time, default 07:00). Pure functions with an injectable clock."""

from __future__ import annotations

import re
import time
from typing import Callable

DEFAULT_TIME = "07:00"
CHECK_EVERY_S = 600.0
MAX_ATTEMPTS_PER_DAY = 3
_TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*$")


def parse_time(text: str | None) -> tuple[int, int, bool]:
    """'07:00' -> (7, 0, True). Anything unreadable -> the default with False."""
    m = _TIME_RE.match(text or "")
    if not m:
        h, mi = (int(x) for x in DEFAULT_TIME.split(":"))
        return h, mi, text is None or str(text).strip() == ""
    return int(m.group(1)), int(m.group(2)), True


def local_day(now: float, localtime: Callable = time.localtime) -> tuple[str, int]:
    """(local date 'YYYY-MM-DD', minutes since local midnight)."""
    t = localtime(now)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}", t.tm_hour * 60 + t.tm_min


def is_due(now: float, last_date: str | None, briefing_time: str | None = DEFAULT_TIME,
           localtime: Callable = time.localtime) -> bool:
    """True once the local time has passed ``briefing_time`` on a day without a scheduled briefing."""
    h, m, _ = parse_time(briefing_time)
    date, minutes = local_day(now, localtime)
    return minutes >= h * 60 + m and last_date != date


def next_due(now: float, last_date: str | None, briefing_time: str | None = DEFAULT_TIME,
             localtime: Callable = time.localtime, mktime: Callable = time.mktime) -> float:
    """Epoch seconds of the next scheduled briefing (``now`` when one is due)."""
    if is_due(now, last_date, briefing_time, localtime):
        return now
    h, m, _ = parse_time(briefing_time)
    t = localtime(now)
    date, minutes = local_day(now, localtime)
    day_offset = 0 if (minutes < h * 60 + m and last_date != date) else 1
    # mktime normalises day overflow (31 + 1) and applies the local DST rules of that day
    return float(mktime((t.tm_year, t.tm_mon, t.tm_mday + day_offset, h, m, 0, 0, 0, -1)))


class Attempts:
    """Failed scheduled runs per local day, so a broken generation is not retried every 10 minutes forever."""

    def __init__(self, limit: int = MAX_ATTEMPTS_PER_DAY):
        self.limit = limit
        self.day: str | None = None
        self.count = 0

    def allowed(self, date: str) -> bool:
        if date != self.day:
            self.day, self.count = date, 0
        return self.count < self.limit

    def failed(self, date: str) -> None:
        if date != self.day:
            self.day, self.count = date, 0
        self.count += 1
