"""Shared statistics for anomaly detectors: rolling baselines, standard library only.

Detectors (ships, flights, intel and later ones) compare what they see now with what is usual for the
same place and time. This module is the arithmetic they share, so every detector uses the same formulas:

- Robust z-scores: ``robust_z`` (median and MAD) and ``robust_z_counts``, the same on ``log1p`` counts:
  ``z = (log1p(x) - median(log1p(B))) / (1.4826 * MAD(log1p(B)) + 0.1)``. The 0.1 floor stops
  near-constant baselines from producing huge values.
- Poisson tails for small counts: ``poisson_sf`` (P(X >= k)), ``poisson_cdf`` (P(X <= k)),
  ``poisson_surprise`` (-log10 of a tail; 3 means a one-in-a-thousand chance) and ``p_to_z``. They work in
  log space, so extreme tails do not underflow.
- EWMA: ``ewma`` (a smoothed series) and ``EWMA`` (streaming mean and variance, kept in ``kv`` with
  ``to_dict`` and ``from_dict``).
- Seasonal baselines: ``hour_of_week``, ``hour_of_day``, ``seasonal_windows``, ``window_sample`` (with an
  uptime mask, so hours when Meridian was not running are not read as quiet), ``SeasonalBaseline`` and
  ``SeasonalEWMA`` (one EWMA per hour-of-week slot).
- ``assess_count``: chooses the robust z (baseline median 5 or more) or the Poisson tail (smaller
  baselines), says "Building baseline: 9 of 14 days" until it has enough samples, and returns the numbers
  a card needs. Thresholds ("z 3 or more and twice the usual") stay with each detector.

Conventions: times are epoch seconds (UTC); hourly tallies are mappings ``{hour_start: value}``; uptime is
the fraction (0 to 1) of an hour that Meridian was running, as a mapping or a function. ``direction`` is
"high" (more than usual) or "low" (fewer than usual). Bad input raises ValueError. Nothing here does I/O
or keeps global state, so everything is safe to call from any thread.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Callable, Iterable, Mapping

MAD_SCALE = 1.4826            # scales the MAD to the standard deviation for normally distributed data
Z_FLOOR = 0.1                 # added to the scaled MAD (research formula)
POISSON_BELOW = 5.0           # assess_count uses the Poisson tail when the baseline median is below this
SURPRISE_MAX = 300.0          # poisson_surprise is capped here (a tail probability below 1e-300)
HOUR_S = 3600
DAY_S = 86400
WEEK_S = 7 * DAY_S
HOURS_PER_WEEK = 168
_MONDAY_SHIFT_S = 3 * DAY_S   # 1970-01-01 was a Thursday: shifting by 3 days puts Monday 00:00 at slot 0
_NORMAL = statistics.NormalDist()
_LN10 = math.log(10.0)
_MAX_ITER = 1_000_000


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------
def _finite(value: Any, name: str = "value") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} must be a finite number")
    return out


def _values(values: Iterable[Any], name: str = "values") -> list[float]:
    if values is None or isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a sequence of numbers")
    return [_finite(v, name) for v in values]


def _whole(value: Any, name: str = "k") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError(f"{name} must be a whole number")


def _rate(value: Any) -> float:
    lam = _finite(value, "the expected count")
    if lam < 0:
        raise ValueError("the expected count must not be negative")
    return lam


def _count(value: Any, name: str = "count") -> float:
    v = _finite(value, name)
    if v < 0:
        raise ValueError(f"{name} must not be negative")
    return v


def _direction(direction: str) -> str:
    if direction not in ("high", "low"):
        raise ValueError('direction must be "high" or "low"')
    return direction


# ---------------------------------------------------------------------------------------------
# Location and spread
# ---------------------------------------------------------------------------------------------
def median(values: Iterable[float]) -> float:
    """The median of a non-empty sample."""
    data = _values(values)
    if not data:
        raise ValueError("the sample is empty")
    return float(statistics.median(data))


def mad(values: Iterable[float], center: float | None = None) -> float:
    """Median absolute deviation from ``center`` (the median by default), not scaled."""
    data = _values(values)
    if not data:
        raise ValueError("the sample is empty")
    c = float(statistics.median(data)) if center is None else _finite(center, "center")
    return float(statistics.median([abs(v - c) for v in data]))


def quantile(values: Iterable[float], q: float) -> float:
    """The q-quantile (0 to 1) with linear interpolation between order statistics."""
    data = sorted(_values(values))
    if not data:
        raise ValueError("the sample is empty")
    q = _finite(q, "q")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be between 0 and 1")
    pos = q * (len(data) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def clip(x: float, lo: float, hi: float) -> float:
    """``x`` limited to [lo, hi]."""
    x, lo, hi = _finite(x, "x"), _finite(lo, "lo"), _finite(hi, "hi")
    if lo > hi:
        raise ValueError("lo must not be above hi")
    return min(hi, max(lo, x))


# ---------------------------------------------------------------------------------------------
# Robust z-scores
# ---------------------------------------------------------------------------------------------
def robust_z(x: float, baseline: Iterable[float], *, floor: float = Z_FLOOR) -> float:
    """``(x - median(B)) / (1.4826 * MAD(B) + floor)``: positive when ``x`` is above the usual level.

    With ``floor=0`` and a constant baseline the spread is zero: the result is 0 when ``x`` equals the
    baseline and plus or minus infinity otherwise."""
    data = _values(baseline, "baseline")
    if not data:
        raise ValueError("robust_z needs a baseline")
    x = _finite(x, "x")
    floor = _finite(floor, "floor")
    if floor < 0:
        raise ValueError("floor must not be negative")
    med = float(statistics.median(data))
    spread = MAD_SCALE * mad(data, med) + floor
    if spread <= 0:
        return 0.0 if x == med else math.copysign(math.inf, x - med)
    return (x - med) / spread


def robust_z_counts(x: float, baseline: Iterable[float], *, floor: float = Z_FLOOR) -> float:
    """``robust_z`` on ``log1p`` of non-negative counts, the formula every detector shares. Logs make the
    score multiplicative: twice the usual reads about the same for 10 against 5 as for 1,000 against 500."""
    logs = [math.log1p(_count(b, "baseline counts")) for b in _values(baseline, "baseline")]
    return robust_z(math.log1p(_count(x, "x")), logs, floor=floor)


# ---------------------------------------------------------------------------------------------
# EWMA
# ---------------------------------------------------------------------------------------------
def ewma_alpha(alpha: float | None = None, *, halflife: float | None = None, span: float | None = None) -> float:
    """The smoothing factor from exactly one of ``alpha`` (0 < alpha <= 1), ``halflife`` (steps until an
    old value's weight halves: ``1 - 0.5 ** (1 / halflife)``) or ``span`` (``2 / (span + 1)``)."""
    if sum(v is not None for v in (alpha, halflife, span)) != 1:
        raise ValueError("give exactly one of alpha, halflife or span")
    if alpha is not None:
        a = _finite(alpha, "alpha")
    elif halflife is not None:
        h = _finite(halflife, "halflife")
        if h <= 0:
            raise ValueError("halflife must be positive")
        a = 1.0 - 0.5 ** (1.0 / h)
    else:
        s = _finite(span, "span")
        if s < 1:
            raise ValueError("span must be at least 1")
        a = 2.0 / (s + 1.0)
    if not 0.0 < a <= 1.0:
        raise ValueError("alpha must be more than 0 and at most 1")
    return a


class EWMA:
    """Streaming exponentially weighted mean and variance (Finch, "Incremental calculation of weighted
    mean and variance", 2009).

    ``update(x)`` folds in one observation. ``update(x, steps=n)`` folds it in ``n`` steps (for example
    hours) after the previous one, so a gap such as Meridian being off decays the old level as if the
    missing steps had passed: the new value's weight is ``1 - (1 - alpha) ** steps``. The first
    observation sets the mean (variance 0).

        e = EWMA(halflife=24)              # hourly values, a day's half-life
        e.update(count)
        e.mean, e.std, e.z(count)
        store.kv_set("mymodule.ewma", e.to_dict()); EWMA.from_dict(store.kv_get("mymodule.ewma"))
    """

    __slots__ = ("alpha", "mean", "var", "n")

    def __init__(self, alpha: float | None = None, *, halflife: float | None = None, span: float | None = None):
        self.alpha = ewma_alpha(alpha, halflife=halflife, span=span)
        self.mean: float | None = None
        self.var = 0.0
        self.n = 0

    def update(self, x: float, steps: float = 1) -> float:
        """Fold in ``x``; returns the new mean."""
        x = _finite(x, "x")
        steps = _finite(steps, "steps")
        if steps <= 0:
            raise ValueError("steps must be positive")
        if self.mean is None:
            self.mean, self.var = x, 0.0
        else:
            a = 1.0 - (1.0 - self.alpha) ** steps
            diff = x - self.mean
            incr = a * diff
            self.mean += incr
            self.var = (1.0 - a) * (self.var + diff * incr)
        self.n += 1
        return self.mean

    @property
    def std(self) -> float:
        return math.sqrt(max(0.0, self.var))

    def z(self, x: float, *, floor: float = 0.0) -> float | None:
        """``(x - mean) / (std + floor)``; None before the first value or while the spread is zero."""
        x = _finite(x, "x")
        floor = _finite(floor, "floor")
        if self.mean is None:
            return None
        spread = self.std + max(0.0, floor)
        if spread <= 0:
            return None
        return (x - self.mean) / spread

    def to_dict(self) -> dict:
        return {"alpha": self.alpha, "mean": self.mean, "var": self.var, "n": self.n}

    @classmethod
    def from_dict(cls, data: Any) -> "EWMA":
        """Rebuild from ``to_dict`` output (for example after a restart). Raises ValueError if damaged."""
        if not isinstance(data, dict):
            raise ValueError("EWMA state must be an object")
        e = cls(_finite(data.get("alpha"), "alpha"))
        mean = data.get("mean")
        e.mean = None if mean is None else _finite(mean, "mean")
        var = _finite(data.get("var", 0.0), "var")
        if var < 0:
            raise ValueError("var must not be negative")
        e.var = 0.0 if e.mean is None else var
        e.n = _whole(data.get("n", 0), "n")
        if e.n < 0:
            raise ValueError("n must not be negative")
        return e

    def __repr__(self) -> str:
        return f"EWMA(alpha={self.alpha:.4g}, mean={self.mean}, std={self.std:.4g}, n={self.n})"


def ewma(values: Iterable[float | None], alpha: float | None = None, *, halflife: float | None = None,
         span: float | None = None) -> list[float | None]:
    """The smoothed series, one output per input. ``None`` entries are gaps: the output repeats the last
    level (None before the first value) and the next value is folded in with the gap's extra steps."""
    e = EWMA(alpha, halflife=halflife, span=span)
    out: list[float | None] = []
    steps = 1
    for v in values:
        if v is None:
            out.append(e.mean)
            if e.mean is not None:
                steps += 1
            continue
        e.update(v, steps=steps)
        steps = 1
        out.append(e.mean)
    return out


# ---------------------------------------------------------------------------------------------
# Poisson tails
# ---------------------------------------------------------------------------------------------
def _log_gamma_tails(a: float, x: float) -> tuple[float, float]:
    """(log P(a, x), log Q(a, x)): the regularised lower and upper incomplete gamma functions for a > 0 and
    x > 0, by the series (x < a + 1) or Lentz's continued fraction (Numerical Recipes, 6.2). The tail that
    is computed directly stays accurate in log space far below the smallest float."""
    log_front = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1.0:
        term = total = 1.0 / a
        ap = a
        for _ in range(_MAX_ITER):
            ap += 1.0
            term *= x / ap
            total += term
            if term < total * 1e-17:
                break
        log_p = log_front + math.log(total)
        p = math.exp(log_p)
        return log_p, (math.log1p(-p) if p < 1.0 else -math.inf)
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, _MAX_ITER):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    log_q = log_front + math.log(h)
    q = math.exp(log_q)
    return (math.log1p(-q) if q < 1.0 else -math.inf), log_q


def _log_poisson_sf(k: Any, lam: Any) -> float:
    k, lam = _whole(k), _rate(lam)
    if k <= 0:
        return 0.0
    if lam == 0:
        return -math.inf
    return _log_gamma_tails(float(k), lam)[0]              # P(X >= k) = P(k, lam)


def _log_poisson_cdf(k: Any, lam: Any) -> float:
    k, lam = _whole(k), _rate(lam)
    if k < 0:
        return -math.inf
    if lam == 0:
        return 0.0
    return _log_gamma_tails(float(k + 1), lam)[1]          # P(X <= k) = Q(k + 1, lam)


def poisson_pmf(k: int, lam: float) -> float:
    """P(X = k) for X ~ Poisson(lam)."""
    k, lam = _whole(k), _rate(lam)
    if k < 0:
        return 0.0
    if lam == 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k): the chance of seeing ``k`` or more when ``lam`` are expected (the upper tail used for
    "more than usual" on small counts)."""
    return math.exp(_log_poisson_sf(k, lam))


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k): the chance of seeing ``k`` or fewer when ``lam`` are expected (for deficits)."""
    return math.exp(_log_poisson_cdf(k, lam))


def poisson_tail(k: int, lam: float, direction: str = "high") -> float:
    """``poisson_sf`` for direction "high", ``poisson_cdf`` for "low"."""
    return poisson_sf(k, lam) if _direction(direction) == "high" else poisson_cdf(k, lam)


def poisson_surprise(k: int, lam: float, direction: str = "high") -> float:
    """``-log10`` of the Poisson tail: 0 is unremarkable, 2 is a one-in-a-hundred count, 3 one in a
    thousand. "high" uses P(X >= k), "low" P(X <= k). Capped at ``SURPRISE_MAX`` (also when the count is
    impossible, such as 3 seen when 0 are expected)."""
    lp = _log_poisson_sf(k, lam) if _direction(direction) == "high" else _log_poisson_cdf(k, lam)
    if lp == -math.inf:
        return SURPRISE_MAX
    return min(SURPRISE_MAX, max(0.0, -lp / _LN10))


def p_to_z(p: float) -> float:
    """The one-sided normal score with the same tail probability, P(Z >= z) = p: 0.5 gives 0, 0.00135
    about 3, 1e-300 about 37. Lets Poisson results sit next to robust z values."""
    p = _finite(p, "p")
    if not 0.0 <= p <= 1.0:
        raise ValueError("p must be between 0 and 1")
    return -_NORMAL.inv_cdf(min(max(p, 1e-300), 1.0 - 1e-16))


def expected_count(baseline_mean: float, volume_now: float | None = None,
                   volume_baseline: float | None = None) -> float:
    """``lambda = mean(B) * volume_now / volume_baseline``: the usual count at today's overall volume (for
    example all GDELT rows), so a quiet Sunday does not read as a drop. Without volumes, the mean itself."""
    mean = _count(baseline_mean, "baseline_mean")
    if volume_now is None and volume_baseline is None:
        return mean
    if volume_now is None or volume_baseline is None:
        raise ValueError("give both volumes or neither")
    now, base = _count(volume_now, "volume_now"), _count(volume_baseline, "volume_baseline")
    if base == 0:
        raise ValueError("volume_baseline must be positive")
    return mean * now / base


def scale_to_volume(counts: Iterable[float], volumes: Iterable[float], volume_now: float) -> list[float]:
    """Each baseline count at the current overall volume, ``count / volume * volume_now``; pairs whose
    volume is 0 (no data that day) are left out."""
    cs, vs = _values(counts, "counts"), _values(volumes, "volumes")
    if len(cs) != len(vs):
        raise ValueError("counts and volumes must have the same length")
    now = _count(volume_now, "volume_now")
    return [_count(c, "counts") / v * now for c, v in zip(cs, vs) if _count(v, "volumes") > 0]


# ---------------------------------------------------------------------------------------------
# Assessing a count against its baseline
# ---------------------------------------------------------------------------------------------
def building_text(n: int, needed: int, unit: str = "days") -> str:
    """The wording detectors show until a baseline is ready: "Building baseline: 9 of 14 days"."""
    return f"Building baseline: {max(0, int(n))} of {int(needed)} {unit}"


def assess_count(x: float, baseline: Iterable[float], *, min_samples: int = 14, unit: str = "days",
                 direction: str = "high", poisson_below: float = POISSON_BELOW, floor: float = Z_FLOOR,
                 expected: float | None = None, exposure: float = 1.0) -> dict:
    """Compare a count ``x`` with its baseline sample.

    Until the baseline has ``min_samples`` values the result has ``ready`` False, a ``status`` such as
    "Building baseline: 9 of 14 days" and no score: a detector must not flag in the meantime.

    When ready and the baseline median is ``poisson_below`` or more, ``method`` is "robust z" and ``z`` is
    ``robust_z_counts``. Otherwise ``method`` is "Poisson": ``p`` is P(X >= x) ("high") or P(X <= x)
    ("low") with lambda = ``expected`` (for example from ``expected_count``) or the baseline mean,
    ``surprise`` is -log10 p and ``z`` is ``p_to_z(p)``. ``z`` is signed the same way in both cases:
    positive above the usual level, negative below it.

    ``exposure`` is the share of a full window that was observed (0 to 1; for example 0.9 when Meridian
    ran for 90% of it): the robust z compares ``x / exposure`` with the baseline and the Poisson rate is
    scaled by it, so a partly covered window does not look quiet.

    Always returned: ``value``, ``n``, ``needed``, ``median``, ``mean``, ``ratio`` (value per usual,
    ``x / exposure / max(median, 0.5)``), ``direction``, ``unit``, ``exposure``."""
    sample = _values(baseline, "baseline")
    for v in sample:
        _count(v, "baseline counts")
    x = _count(x, "x")
    direction = _direction(direction)
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
        raise ValueError("min_samples must be a whole number of at least 1")
    exposure = _finite(exposure, "exposure")
    if not 0.0 < exposure <= 1.0:
        raise ValueError("exposure must be more than 0 and at most 1")
    n = len(sample)
    med = float(statistics.median(sample)) if sample else None
    mean = float(statistics.fmean(sample)) if sample else None
    out: dict[str, Any] = {
        "ready": False, "status": building_text(n, min_samples, unit), "value": x, "n": n,
        "needed": min_samples, "median": med, "mean": mean,
        "ratio": (x / exposure) / max(med, 0.5) if med is not None else None,
        "method": None, "z": None, "p": None, "surprise": None, "expected": None,
        "direction": direction, "unit": unit, "exposure": exposure,
    }
    if n < min_samples or n == 0:
        return out
    out["ready"] = True
    out["status"] = f"Baseline of {n} {unit}"
    if med >= poisson_below:
        out["method"] = "robust z"
        out["z"] = robust_z_counts(x / exposure, sample, floor=floor)
        return out
    lam = (_rate(expected) if expected is not None else mean) * exposure
    k = int(round(x))
    log_p = _log_poisson_sf(k, lam) if direction == "high" else _log_poisson_cdf(k, lam)
    p = math.exp(log_p)
    out["method"] = "Poisson"
    out["expected"] = lam
    out["p"] = p
    out["surprise"] = SURPRISE_MAX if log_p == -math.inf else min(SURPRISE_MAX, max(0.0, -log_p / _LN10))
    z = p_to_z(p)
    out["z"] = z if direction == "high" else -z
    return out


# ---------------------------------------------------------------------------------------------
# Seasonal windows and uptime
# ---------------------------------------------------------------------------------------------
def hour_start(t: float) -> int:
    """The start of the UTC hour containing ``t`` (epoch seconds)."""
    return int(math.floor(_finite(t, "t") / HOUR_S)) * HOUR_S


def hour_of_day(t: float, *, utc_offset_s: float = 0) -> int:
    """0 to 23; ``utc_offset_s`` shifts to a local clock (3600 for UTC+1)."""
    local = _finite(t, "t") + _finite(utc_offset_s, "utc_offset_s")
    return int(math.floor(local / HOUR_S)) % 24


def hour_of_week(t: float, *, utc_offset_s: float = 0) -> int:
    """0 (Monday 00:00 to 01:00) to 167 (Sunday 23:00 to 24:00), in UTC unless ``utc_offset_s`` is given."""
    local = _finite(t, "t") + _finite(utc_offset_s, "utc_offset_s")
    return int(math.floor((local + _MONDAY_SHIFT_S) / HOUR_S)) % HOURS_PER_WEEK


def seasonal_windows(end: float, *, window_s: float = HOUR_S, period_s: float = WEEK_S,
                     periods: int = 4) -> list[tuple[float, float]]:
    """The same window in each of the previous ``periods`` periods, most recent first, as ``(start, end)``.

    The current window is ``[end - window_s, end)``; window k (1 to ``periods``) is shifted back by
    ``k * period_s``. Examples: hour of the week over 4 weeks, ``seasonal_windows(h)``; the same 6 hours
    of the day over 21 days, ``seasonal_windows(h, window_s=6 * HOUR_S, period_s=DAY_S, periods=21)``."""
    end = _finite(end, "end")
    window_s, period_s = _finite(window_s, "window_s"), _finite(period_s, "period_s")
    if window_s <= 0 or period_s <= 0:
        raise ValueError("window_s and period_s must be positive")
    if window_s > period_s:
        raise ValueError("a window must not be longer than its period (windows would overlap)")
    if isinstance(periods, bool) or not isinstance(periods, int) or periods < 1:
        raise ValueError("periods must be a whole number of at least 1")
    return [(end - k * period_s - window_s, end - k * period_s) for k in range(1, periods + 1)]


def _uptime_fn(uptime: Mapping[Any, float] | Callable[[int], float] | None) -> Callable[[int], float]:
    if uptime is None:
        return lambda h: 1.0
    if callable(uptime):
        return lambda h: clip(_finite(uptime(h), "uptime"), 0.0, 1.0)
    if isinstance(uptime, Mapping):
        return lambda h: clip(_finite(uptime.get(h, 0.0), "uptime"), 0.0, 1.0)
    raise ValueError("uptime must be a mapping, a function or None")


def _hours(start: float, end: float) -> range:
    first = int(math.ceil(start / HOUR_S)) * HOUR_S
    return range(first, int(math.ceil(end / HOUR_S)) * HOUR_S, HOUR_S)


def window_total(hourly: Mapping[Any, float], start: float, end: float, *,
                 uptime: Mapping[Any, float] | Callable[[int], float] | None = None) -> tuple[float, float]:
    """``(total, coverage)`` of the hourly tallies whose hour starts in [start, end). Missing hours count as
    0 (nothing seen). ``coverage`` is the mean uptime of the window's hours (1 when ``uptime`` is None;
    hours missing from an uptime mapping count as not running)."""
    up = _uptime_fn(uptime)
    hours = _hours(_finite(start, "start"), _finite(end, "end"))
    if len(hours) == 0:
        raise ValueError("the window holds no whole hour")
    total = sum(_finite(hourly.get(h, 0.0), "hourly value") for h in hours)
    return total, sum(up(h) for h in hours) / len(hours)


def window_sample(hourly: Mapping[Any, float], windows: Iterable[tuple[float, float]], *,
                  uptime: Mapping[Any, float] | Callable[[int], float] | None = None,
                  min_coverage: float = 0.8, scale: bool = True) -> tuple[list[float], int]:
    """Window totals for a baseline, leaving out windows when Meridian was not running enough.

    Windows whose coverage (see ``window_total``) is under ``min_coverage`` are dropped: a quiet period is
    evidence only if Meridian was running. The others are scaled up by ``1 / coverage`` when ``scale`` is
    true, so a window that was 90% covered counts as a full one. Returns ``(values, dropped)``."""
    min_coverage = _finite(min_coverage, "min_coverage")
    if not 0.0 < min_coverage <= 1.0:
        raise ValueError("min_coverage must be more than 0 and at most 1")
    values: list[float] = []
    dropped = 0
    for start, end in windows:
        total, cov = window_total(hourly, start, end, uptime=uptime)
        if cov < min_coverage:
            dropped += 1
            continue
        values.append(total / cov if scale else total)
    return values, dropped


def coverage_by_hour(timestamps: Iterable[float], *, every_s: float = 300.0) -> dict[int, float]:
    """Hourly uptime from heartbeat times (one every ``every_s`` seconds while running): the share of the
    hour's heartbeat slots that were seen, at most 1. Hours with none are absent, which reads as 0."""
    every_s = _finite(every_s, "every_s")
    if not 0 < every_s <= HOUR_S:
        raise ValueError("every_s must be more than 0 and at most 3600")
    slots: dict[int, set[int]] = {}
    for t in timestamps:
        t = _finite(t, "timestamp")
        slots.setdefault(hour_start(t), set()).add(int(math.floor(t / every_s)))
    per_hour = HOUR_S / every_s
    return {h: min(1.0, len(s) / per_hour) for h, s in slots.items()}


class SeasonalBaseline:
    """A same-time-slot baseline over hourly tallies, aware of uptime.

    ``period="week"`` compares an hour with the same hour of the week in earlier weeks (default 4 weeks,
    at least 3 valid); ``period="day"`` with the same hours of the day on earlier days (default 28 days, at
    least 14). ``window_hours`` sums several hours, ``periods`` sets how far back to look and
    ``min_samples`` how many valid windows are needed before it says anything.

        b = SeasonalBaseline(period="day", window_hours=6, periods=21, min_samples=10)
        r = b.assess(hourly, end=hour_start(now), uptime=up)       # value: the latest 6 hours
        if r["ready"] and r["z"] >= 3 and r["ratio"] >= 2 and r["value"] >= 8: ...
    """

    def __init__(self, *, period: str = "week", window_hours: int = 1, periods: int | None = None,
                 min_samples: int | None = None, min_coverage: float = 0.8, scale: bool = True):
        if period == "week":
            self.period_s, default_periods, default_min, self.unit = WEEK_S, 4, 3, "weeks"
        elif period == "day":
            self.period_s, default_periods, default_min, self.unit = DAY_S, 28, 14, "days"
        else:
            raise ValueError('period must be "week" or "day"')
        if isinstance(window_hours, bool) or not isinstance(window_hours, int) or window_hours < 1:
            raise ValueError("window_hours must be a whole number of at least 1")
        self.period = period
        self.window_s = window_hours * HOUR_S
        if self.window_s > self.period_s:
            raise ValueError("the window must not be longer than the period")
        self.periods = default_periods if periods is None else periods
        if isinstance(self.periods, bool) or not isinstance(self.periods, int) or self.periods < 1:
            raise ValueError("periods must be a whole number of at least 1")
        self.min_samples = min(default_min, self.periods) if min_samples is None else min_samples
        if isinstance(self.min_samples, bool) or not isinstance(self.min_samples, int) or self.min_samples < 1:
            raise ValueError("min_samples must be a whole number of at least 1")
        if self.min_samples > self.periods:
            raise ValueError("min_samples must not exceed periods")
        self.min_coverage = _finite(min_coverage, "min_coverage")
        if not 0.0 < self.min_coverage <= 1.0:
            raise ValueError("min_coverage must be more than 0 and at most 1")
        self.scale = bool(scale)
        seasonal_windows(0, window_s=self.window_s, period_s=self.period_s, periods=self.periods)  # validates

    def windows(self, end: float) -> list[tuple[float, float]]:
        return seasonal_windows(end, window_s=self.window_s, period_s=self.period_s, periods=self.periods)

    def sample(self, hourly: Mapping[Any, float], end: float, *, uptime=None) -> tuple[list[float], int]:
        """``(values, dropped)`` for the earlier windows (see ``window_sample``)."""
        return window_sample(hourly, self.windows(end), uptime=uptime, min_coverage=self.min_coverage,
                             scale=self.scale)

    def assess(self, hourly: Mapping[Any, float], end: float, *, value: float | None = None, uptime=None,
               direction: str = "high", expected: float | None = None) -> dict:
        """``assess_count`` for the window ending at ``end`` (its total unless ``value`` is given) against the
        earlier windows. Adds ``coverage`` and ``dropped``; when the current window itself was covered less
        than ``min_coverage`` the result is not ready ("Not enough uptime in this window")."""
        total, cov = window_total(hourly, end - self.window_s, end, uptime=uptime)
        x = total if value is None else value
        sample, dropped = self.sample(hourly, end, uptime=uptime)
        if cov < self.min_coverage:
            out = assess_count(x, sample, min_samples=self.min_samples, unit=self.unit, direction=direction,
                               expected=expected)
            out.update(ready=False, status="Not enough uptime in this window", method=None, z=None, p=None,
                       surprise=None, expected=None)
        else:
            out = assess_count(x, sample, min_samples=self.min_samples, unit=self.unit, direction=direction,
                               expected=expected, exposure=cov if self.scale else 1.0)
        out["coverage"] = cov
        out["dropped"] = dropped
        return out


class SeasonalEWMA:
    """One EWMA per time slot: a compact seasonal profile (hour of the week by default, ``slots=24`` for
    hour of the day) that a detector updates once an hour and keeps in ``kv`` instead of months of rows.

        prof = SeasonalEWMA(alpha=0.3)         # each hour-of-week slot sees one value a week
        prof.update(t, count)                   # once per hour; repeats for an hour already seen are ignored
        prof.expected(t), prof.z(t, count), prof.seen(t)
        store.kv_set("mymodule.profile", prof.to_dict())

    A slot not updated for several periods (Meridian was off) decays its old level by the missed periods.
    """

    def __init__(self, alpha: float | None = None, *, halflife: float | None = None, span: float | None = None,
                 slots: int = HOURS_PER_WEEK, utc_offset_s: float = 0):
        if slots not in (24, HOURS_PER_WEEK):
            raise ValueError("slots must be 24 (hour of the day) or 168 (hour of the week)")
        self.alpha = ewma_alpha(alpha, halflife=halflife, span=span)
        self.slots = slots
        self.utc_offset_s = _finite(utc_offset_s, "utc_offset_s")
        self._period_s = DAY_S if slots == 24 else WEEK_S
        self._ewma: dict[int, EWMA] = {}
        self._last: dict[int, int] = {}

    def slot(self, t: float) -> int:
        if self.slots == 24:
            return hour_of_day(t, utc_offset_s=self.utc_offset_s)
        return hour_of_week(t, utc_offset_s=self.utc_offset_s)

    def update(self, t: float, value: float) -> float:
        """Fold in the value for the hour containing ``t``; returns the slot's mean."""
        value = _finite(value, "value")
        s, h = self.slot(t), hour_start(t)
        e = self._ewma.get(s)
        if e is None:
            e = self._ewma[s] = EWMA(self.alpha)
            e.update(value)
        else:
            last = self._last[s]
            if h <= last:
                return e.mean                       # this hour (or a later one) is already in the slot
            e.update(value, steps=max(1, round((h - last) / self._period_s)))
        self._last[s] = h
        return e.mean

    def expected(self, t: float) -> float | None:
        e = self._ewma.get(self.slot(t))
        return None if e is None else e.mean

    def seen(self, t: float) -> int:
        e = self._ewma.get(self.slot(t))
        return 0 if e is None else e.n

    def z(self, t: float, value: float, *, floor: float = 0.0) -> float | None:
        e = self._ewma.get(self.slot(t))
        return None if e is None else e.z(value, floor=floor)

    def to_dict(self) -> dict:
        return {"alpha": self.alpha, "slots": self.slots, "utc_offset_s": self.utc_offset_s,
                "state": {str(s): {**e.to_dict(), "last": self._last[s]} for s, e in sorted(self._ewma.items())}}

    @classmethod
    def from_dict(cls, data: Any) -> "SeasonalEWMA":
        if not isinstance(data, dict) or not isinstance(data.get("state", {}), dict):
            raise ValueError("SeasonalEWMA state must be an object")
        prof = cls(_finite(data.get("alpha"), "alpha"), slots=_whole(data.get("slots", HOURS_PER_WEEK), "slots"),
                   utc_offset_s=_finite(data.get("utc_offset_s", 0), "utc_offset_s"))
        for key, item in data.get("state", {}).items():
            s = _whole(int(key) if isinstance(key, str) and key.isdigit() else key, "slot")
            if not 0 <= s < prof.slots or not isinstance(item, dict):
                raise ValueError("bad slot in SeasonalEWMA state")
            e = EWMA.from_dict(item)
            if e.mean is None:
                continue
            prof._ewma[s] = e
            prof._last[s] = _whole(item.get("last"), "last")
        return prof
