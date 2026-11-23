"""News spikes: unusually high GDELT volume per country against a rolling baseline.

Metrics, per country, over a rolling 24-hour window (hourly tallies from the 15-minute exports):
  volume    every row GDELT geocoded to the country (attention)
  conflict  violent-conflict rows (QuadClass 4, CAMEO roots 18-20)
  protest   protest rows (CAMEO root 14)

Baseline: complete UTC days before the window (up to 28, at least 14) that hold 90 % or more of their
96 export files (``intel_country_day``, so stored history counts from the start). Each day's count is
converted to the share of all GDELT rows that day and multiplied by the current window's total, which
removes weekday swings (Sunday has about a fifth fewer rows) and GDELT-wide drops.

Test: robust z = (ln(1 + x) - median(ln(1 + B))) / (1.4826 x MAD(ln(1 + B)) + 0.1) when the baseline
median is 5 or more; for smaller baselines the Poisson upper tail p = P(X >= x | mean(B)).
- unusual: z >= 3 (or p < 0.001), at least twice the median and at least the metric's minimum count,
  in two consecutive hourly windows: becomes an event (kind ``news_spike``);
- notable: z >= 2 (or p < 0.01) and 1.5 times the median (lists and the country card only);
- one story: 60 % or more of the rows belong to one deduplicated event, or one source domain supplies
  half of the country's rows: downgraded to notable, with the reason;
- pause: under 90 % of the window's export files, or global volume under half its usual level.
Wording stays factual ("unusually high news volume"), never "crisis".
"""

from __future__ import annotations

import math
import statistics
import time

from . import db, gdelt

METRICS = {
    "volume": {"label": "news volume", "noun": "reports", "min_unusual": 50, "min_notable": 30},
    "conflict": {"label": "conflict reports", "noun": "conflict reports", "min_unusual": 10, "min_notable": 6},
    "protest": {"label": "protest reports", "noun": "protest reports", "min_unusual": 8, "min_notable": 5},
}
WINDOW_S = 86400
FILES_PER_DAY = 86400 // gdelt.SLOT_SECONDS            # 96
MIN_FILE_SHARE = 0.9
MIN_DAYS = 14
MAX_DAYS = 28
EPISODE_GAP_S = 4 * 3600                                # a flag within 4 h of the last one extends the episode
ONE_STORY_SHARE = 0.6
ONE_SOURCE_SHARE = 0.5
Z_UNUSUAL, Z_NOTABLE = 3.0, 2.0
P_UNUSUAL, P_NOTABLE = 0.001, 0.01
RATIO_UNUSUAL, RATIO_NOTABLE = 2.0, 1.5
MEDIAN_FOR_Z = 5.0


# ---------------------------------------------------------------------------------------------
# Statistics (pure)
# ---------------------------------------------------------------------------------------------
def robust_z(x: float, baseline: list[float]) -> float:
    logs = [math.log1p(max(0.0, float(b))) for b in baseline]
    med = statistics.median(logs)
    mad = statistics.median([abs(v - med) for v in logs])
    return (math.log1p(max(0.0, float(x))) - med) / (1.4826 * mad + 0.1)


def poisson_sf(x: int, lam: float) -> float:
    """P(X >= x) for X ~ Poisson(lam)."""
    x = int(x)
    if x <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    log_lam = math.log(lam)

    def log_pmf(k: int) -> float:
        return -lam + k * log_lam - math.lgamma(k + 1)

    if x > lam:                                         # sum the (decreasing) upper tail directly
        total, k = 0.0, x
        while k < x + 2000:
            term = math.exp(log_pmf(k))
            total += term
            if term < 1e-17 * max(total, 1e-300):
                break
            k += 1
        return min(1.0, total)
    cdf = sum(math.exp(log_pmf(k)) for k in range(0, x))
    return max(0.0, min(1.0, 1.0 - cdf))


def assess(x: float, baseline: list[float], metric: str) -> dict:
    """Level ('unusual', 'notable' or None) of a count against its baseline, with the numbers behind it."""
    spec = METRICS[metric]
    med = statistics.median(baseline) if baseline else 0.0
    mean = statistics.fmean(baseline) if baseline else 0.0
    ratio = x / max(med, 0.5)
    out = {"value": int(x), "median": round(med, 1), "mean": round(mean, 2), "ratio": round(ratio, 2),
           "z": None, "p": None, "method": None, "level": None, "n": len(baseline)}
    if len(baseline) < MIN_DAYS:
        return out
    if med >= MEDIAN_FOR_Z:
        z = robust_z(x, baseline)
        out.update(z=round(z, 2), method="robust z")
        unusual = z >= Z_UNUSUAL and ratio >= RATIO_UNUSUAL and x >= spec["min_unusual"]
        notable = z >= Z_NOTABLE and ratio >= RATIO_NOTABLE and x >= spec["min_notable"]
    else:
        p = poisson_sf(int(x), mean)
        out.update(p=float(f"{p:.3g}"), method="Poisson")
        if p > 0:
            out["z"] = round(statistics.NormalDist().inv_cdf(max(1e-12, min(1 - 1e-12, 1 - p))), 2)
        else:
            out["z"] = 7.0
        unusual = p < P_UNUSUAL and ratio >= RATIO_UNUSUAL and x >= max(8, spec["min_unusual"])
        notable = p < P_NOTABLE and ratio >= RATIO_NOTABLE and x >= max(5, spec["min_notable"])
    out["level"] = "unusual" if unusual else ("notable" if notable else None)
    return out


def scaled_baseline(day_counts: list[tuple[float, float]], window_total: float) -> list[float]:
    """[(country count, global count) per valid day] -> counts expected at the window's global volume."""
    return [c / g * window_total for c, g in day_counts if g > 0]


def severity(ratio: float) -> float:
    """4 at twice the usual level, 5 at four times, 6 at eight times or more (derived signals stay <= 7)."""
    return round(4.0 + min(2.0, max(0.0, math.log2(max(ratio, 1e-9)) - 1.0)), 1)


def headline(metric: str, country: str) -> str:
    return {"volume": f"Unusually high news volume about {country}",
            "conflict": f"Unusually many conflict reports about {country}",
            "protest": f"Unusually many protest reports about {country}"}[metric]


def explanation(metric: str, country: str, r: dict, drivers: list[str], baseline_days: int) -> str:
    """'Conflict reports about Sudan are 3.4 times the usual level: 412 machine-coded reports in the last
    24 hours against a usual 120 (median of 28 days).' plus drivers and the GDELT caveat."""
    noun = METRICS[metric]["noun"]
    what = {"volume": "News volume", "conflict": "Conflict reports", "protest": "Protest reports"}[metric]
    text = (f"{what} about {country} {'is' if metric == 'volume' else 'are'} {r['ratio']:.1f} times the usual level: "
            f"{r['value']:,} machine-coded {noun} in the last 24 hours against a usual {r['median']:,.0f} "
            f"(median of {baseline_days} days, adjusted for total GDELT volume).")
    if drivers:
        text += f" Most come from {', '.join(drivers[:-1]) + ' and ' + drivers[-1] if len(drivers) > 1 else drivers[0]}."
    text += " GDELT is machine-coded from news and can spike when one story is widely repeated."
    return text


# ---------------------------------------------------------------------------------------------
# Detector (reads the tallies, keeps episodes in intel_spikes)
# ---------------------------------------------------------------------------------------------
class SpikeDetector:
    def __init__(self, store, log, country_name, label_point):
        self.store = store
        self.log = log
        self.country_name = country_name
        self.label_point = label_point
        self.result: dict = {"state": "waiting", "message": "Waiting for the first GDELT files.", "flags": [],
                             "computed_at": None}

    # -- data --------------------------------------------------------------------------------
    def _window(self, end: float) -> tuple[dict, int, float]:
        start = end - WINDOW_S
        cur = db.hour_window_totals(self.store, int(start), int(end))
        files = db.tallied_files_between(self.store, gdelt.epoch_to_ts(start), gdelt.epoch_to_ts(end))
        total = float(sum(v[0] for v in cur.values()))
        return cur, files, total

    def _baseline_days(self, before: float) -> tuple[list[int], list[int], dict, dict]:
        """Valid baseline days (all metrics, protest metric) ending at or before ``before``."""
        last_day_start = math.floor(before / 86400) * 86400 - 86400     # the last day that ends by ``before``
        first_day_start = last_day_start - (MAX_DAYS - 1) * 86400
        d0, d1 = gdelt.epoch_to_day(first_day_start), gdelt.epoch_to_day(last_day_start)
        counts = db.file_counts_by_day(self.store, gdelt.epoch_to_ts(first_day_start))
        need = math.ceil(FILES_PER_DAY * MIN_FILE_SHARE)
        days = sorted(d for d, (n, _) in counts.items() if d0 <= d <= d1 and n >= need)
        pdays = sorted(d for d, (_, v2) in counts.items() if d0 <= d <= d1 and v2 >= need)
        tallies = db.day_tallies(self.store, d0, d1)
        totals = {d: float(sum(v[0] for v in tallies.get(d, {}).values())) for d in days}
        return days, pdays, tallies, totals

    # -- evaluation ----------------------------------------------------------------------------
    def evaluate(self, now: float | None = None) -> dict:
        now = now or time.time()
        hour = math.floor(now / 3600) * 3600
        need = math.ceil(FILES_PER_DAY * MIN_FILE_SHARE)
        w0, f0, g0 = self._window(hour)
        w1, f1, g1 = self._window(hour - 3600)
        days, pdays, tallies, totals = self._baseline_days(hour - 3600 - WINDOW_S)
        usual_total = statistics.median([totals[d] for d in days]) if days else 0.0
        base = {"computed_at": now, "window": {"start": hour - WINDOW_S, "end": hour},
                "baseline_days": {"volume": len(days), "conflict": len(days), "protest": len(pdays)},
                "needed_days": MIN_DAYS, "coverage": {"files": f0, "needed": need, "of": FILES_PER_DAY},
                "global": {"rows": int(g0), "usual": round(usual_total)}, "flags": []}
        if len(days) < MIN_DAYS:
            base.update(state="building", message=f"Building the baseline: {len(days)} of {MIN_DAYS} days.")
            self.result = base
            return base
        if f0 < need or f1 < need:
            base.update(state="paused", message=f"Paused: only {f0} of {FILES_PER_DAY} GDELT files in the last 24 hours.")
            self.result = base
            return base
        if usual_total and g0 < 0.5 * usual_total:
            base.update(state="paused", message="Paused: GDELT volume is under half its usual level.")
            self.result = base
            return base
        flags = []
        isos = set(w0) | set(w1)
        for iso2 in isos:
            for k, metric in enumerate(("volume", "conflict", "protest")):
                use_days = pdays if metric == "protest" else days
                if len(use_days) < MIN_DAYS:
                    continue
                pairs = []
                for d in use_days:
                    v = (tallies.get(d) or {}).get(iso2)
                    c = v[k] if v else 0
                    pairs.append((float(c or 0), totals.get(d) or float(sum(x[0] for x in (tallies.get(d) or {}).values()))))
                x0 = (w0.get(iso2) or (0, 0, 0))[k]
                if x0 < METRICS[metric]["min_notable"]:
                    continue
                r0 = assess(x0, scaled_baseline(pairs, g0), metric)
                if not r0["level"]:
                    continue
                x1 = (w1.get(iso2) or (0, 0, 0))[k]
                r1 = assess(x1, scaled_baseline(pairs, g1), metric)
                level = "unusual" if r0["level"] == "unusual" and r1["level"] == "unusual" else "notable"
                flags.append(self._flag(iso2, metric, level, r0, hour, len(use_days)))
        flags.sort(key=lambda f: (f["level"] != "unusual", -(f["z"] or 0), -f["ratio"]))
        base.update(state="ok", message=None, flags=flags[:200])
        self._episodes(flags, hour)
        self.result = base
        return base

    def _flag(self, iso2: str, metric: str, level: str, r: dict, hour: float, ndays: int) -> dict:
        start = hour - WINDOW_S
        name = self.country_name(iso2) or iso2
        reason = None
        drivers: list[str] = []
        try:
            if metric in ("conflict", "protest"):
                table = "intel_events" if metric == "conflict" else "intel_protests"
                top, places = db.story_concentration(self.store, table, iso2, start)
                drivers = [p for p, _ in places[:3]]
                if r["value"] and top / r["value"] >= ONE_STORY_SHARE:
                    reason = "Mostly one story"
            dom, share = db.top_domain_share(self.store, iso2, int(start), int(hour))
            if reason is None and dom and share >= ONE_SOURCE_SHARE:
                reason = f"Mostly one source ({dom})"
        except Exception as err:          # guards are best effort; never lose the flag
            self.log.debug("spike guards for %s failed: %s", iso2, err)
        if reason and level == "unusual":
            level = "notable"
        pos = self.label_point(iso2)
        return {"iso2": iso2, "name": name, "metric": metric, "label": METRICS[metric]["label"], "level": level,
                "reason": reason, "value": r["value"], "baseline": r["median"], "mean": r["mean"], "ratio": r["ratio"],
                "z": r["z"], "p": r["p"], "method": r["method"], "baseline_days": ndays, "drivers": drivers,
                "title": headline(metric, name), "text": explanation(metric, name, r, drivers, ndays),
                "lat": pos[1] if pos else None, "lon": pos[0] if pos else None, "window_end": hour}

    def _episodes(self, flags: list[dict], hour: float) -> None:
        for f in flags:
            if f["level"] != "unusual" or f["lat"] is None:
                continue
            try:
                db.upsert_spike(self.store, f, hour, EPISODE_GAP_S, severity(f["ratio"]))
            except Exception as err:
                self.log.warning("spike episode for %s failed: %s", f["iso2"], err)

    # -- reading -------------------------------------------------------------------------------
    def country(self, iso2: str, now: float | None = None) -> dict:
        """Current reading of every metric for one country, with a 28-day daily series for a chart."""
        now = now or time.time()
        res = self.result or {}
        flags = [f for f in res.get("flags") or [] if f["iso2"] == iso2]
        hour = math.floor(now / 3600) * 3600
        w0, _, g0 = self._window(hour)
        days, pdays, tallies, totals = self._baseline_days(hour - 3600 - WINDOW_S)
        metrics = {}
        for k, metric in enumerate(("volume", "conflict", "protest")):
            use = pdays if metric == "protest" else days
            pairs = [(float(((tallies.get(d) or {}).get(iso2) or (0, 0, 0))[k] or 0), totals.get(d) or 0.0) for d in use]
            x0 = (w0.get(iso2) or (0, 0, 0))[k]
            r = assess(x0, scaled_baseline(pairs, g0), metric) if g0 else {"value": x0, "level": None, "n": len(use)}
            f = next((fl for fl in flags if fl["metric"] == metric), None)
            metrics[metric] = {"label": METRICS[metric]["label"], **r, "level": f["level"] if f else None,
                               "reason": f["reason"] if f else None}
        series = []
        for d in sorted(tallies):
            v = (tallies.get(d) or {}).get(iso2) or (0, 0, None)
            series.append({"day": d, "volume": int(v[0] or 0), "conflict": int(v[1] or 0),
                           "protest": int(v[2]) if v[2] is not None else None, "valid": d in days})
        return {"iso2": iso2, "state": res.get("state"), "message": res.get("message"), "metrics": metrics,
                "series": series[-MAX_DAYS:], "window": res.get("window"), "flags": flags,
                "episodes": db.query_spikes(self.store, since=now - 30 * 86400, iso2=iso2, limit=10)}
