"""Trend maths for the briefing: counts in this period against the previous one.

A change is *notable* only when it is unlikely to be noise. Counts are compared with a Poisson
expectation (the previous count, scaled by the overall change in volume when one is given, so a quiet
Sunday for every country does not read as a fall everywhere), and must also move by at least
``min_delta`` and by a factor of ``min_ratio``. Below an expectation of 5 the test follows the
analytics report's rule for small counts: p < 0.001 and at least 8 now.
"""

from __future__ import annotations

import math

ALPHA = 0.01
SMALL_BASE = 5.0          # below this expectation: the analytics report's stricter rule
SMALL_ALPHA = 0.001
SMALL_MIN = 8
MIN_DELTA = 5
MIN_RATIO = 1.5
STEADY_PCT = 10.0


def _log_pmf(k: int, lam: float) -> float:
    return -lam + k * math.log(lam) - math.lgamma(k + 1)


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(lam)."""
    if k < 0:
        return 0.0
    if lam <= 0:
        return 1.0
    if k >= lam:
        return max(0.0, 1.0 - poisson_sf(k + 1, lam))
    total = 0.0
    for i in range(k + 1):              # terms increase up to k < lam: no cancellation
        total += math.exp(_log_pmf(i, lam))
    return min(1.0, total)


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    if k <= lam:
        return max(0.0, 1.0 - poisson_cdf(k - 1, lam))
    total = 0.0
    i = k
    while True:                          # terms fall away above the mean
        term = math.exp(_log_pmf(i, lam))
        total += term
        if term < 1e-17 * max(total, 1e-300) or i - k > 100000:
            break
        i += 1
    return min(1.0, total)


def pct_change(cur: float, prev: float) -> float | None:
    if prev is None or cur is None or prev == 0:
        return None
    return round((cur - prev) / prev * 100.0, 1)


def compare(cur: int, prev: int, *, scale: float = 1.0, min_delta: int = MIN_DELTA, min_ratio: float = MIN_RATIO,
            alpha: float = ALPHA) -> dict:
    """{cur, prev, pct, direction, notable, p} for a count against the previous period.

    direction: "up", "down", "steady" (within 10%), "new" (nothing before), "gone" (nothing now) or
    "none" (zero both times). ``scale`` is the overall volume ratio (this period / previous)."""
    cur = max(0, int(cur or 0))
    prev = max(0, int(prev or 0))
    pct = pct_change(cur, prev)
    if cur == 0 and prev == 0:
        direction = "none"
    elif prev == 0:
        direction = "new"
    elif cur == 0:
        direction = "gone"
    elif pct is not None and abs(pct) < STEADY_PCT:
        direction = "steady"
    else:
        direction = "up" if cur > prev else "down"
    if not math.isfinite(scale) or scale <= 0:
        scale = 1.0
    expected = max(prev * scale, 0.5)
    notable = False
    p = None
    if cur > expected:
        p = poisson_sf(cur, expected)
        small = expected < SMALL_BASE
        notable = (p < (min(alpha, SMALL_ALPHA) if small else alpha) and cur - expected >= min_delta
                   and cur >= min_ratio * expected and (not small or cur >= SMALL_MIN))
    elif cur < expected:
        p = poisson_cdf(cur, expected)
        notable = p < alpha and expected - cur >= min_delta and cur * min_ratio <= expected
    # the words describe the raw counts, so a flag must agree with them: a steady count is never
    # "notable", nor is a rise that is only a fall against the overall volume (or the reverse)
    if direction in ("steady", "none") or (cur > expected and cur <= prev) or (cur < expected and cur >= prev):
        notable = False
    return {"cur": cur, "prev": prev, "pct": pct, "direction": direction, "notable": bool(notable),
            "p": None if p is None else float(f"{p:.3g}"), "expected": round(expected, 1)}


def words(change: dict, *, prev_label: str = "the previous day") -> str:
    """Plain words for a comparison: "up from 31 the previous day (+74%)"."""
    d = change["direction"]
    prev = change["prev"]
    pct = change.get("pct")
    if d == "none":
        return f"none, as {prev_label}"
    if d == "new":
        return f"none {prev_label}"
    if d == "gone":
        return f"down from {prev:,} {prev_label}"
    if d == "steady":
        return f"about the same as {prev_label} ({prev:,})"
    sign = "+" if (pct or 0) > 0 else ""
    return f"{'up' if d == 'up' else 'down'} from {prev:,} {prev_label} ({sign}{pct:.0f}%)"


def rank_countries(cur: dict[str, int], prev: dict[str, int], *, top: int = 3, extra: int = 2,
                   scale: float = 1.0, min_count: int = 3) -> list[tuple[str, dict]]:
    """The ``top`` countries by count now, plus up to ``extra`` more with a notable change (largest
    first). Countries under ``min_count`` both times are left out."""
    rows = []
    for code in set(cur) | set(prev):
        c, p = cur.get(code, 0), prev.get(code, 0)
        if max(c, p) < min_count:
            continue
        rows.append((code, compare(c, p, scale=scale)))
    rows.sort(key=lambda r: (-r[1]["cur"], -r[1]["prev"], r[0]))
    chosen = rows[:top]
    rest = [r for r in rows[top:] if r[1]["notable"]]
    rest.sort(key=lambda r: (-abs(r[1]["cur"] - r[1]["expected"]), r[0]))
    return chosen + rest[:extra]


def volume_scale(cur_total: int, prev_total: int) -> float:
    """This period's overall volume over the previous one's (1.0 when either is tiny)."""
    if cur_total < 20 or prev_total < 20:
        return 1.0
    return cur_total / prev_total
