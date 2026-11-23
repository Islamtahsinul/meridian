"""Tests for meridian/stats.py, the shared baseline helpers for anomaly detectors.

Run: python -m unittest tests.test_core_stats -v   (no network)
"""

from __future__ import annotations

import json
import math
import random
import statistics
import unittest

from meridian import stats as S

H = S.HOUR_S
DAY = S.DAY_S
WEEK = S.WEEK_S
MONDAY = 1_795_392_000          # 2026-11-23 00:00 UTC, a Monday


def upper_tail(k: int, lam: float) -> float:
    """P(X >= k) by direct summation of the upper tail (no cancellation)."""
    return sum(math.exp(-lam + i * math.log(lam) - math.lgamma(i + 1)) for i in range(k, k + 500))


def lower_tail(k: int, lam: float) -> float:
    return sum(math.exp(-lam + i * math.log(lam) - math.lgamma(i + 1)) for i in range(0, k + 1))


class BasicsTests(unittest.TestCase):
    def test_median_mad_quantile(self):
        self.assertEqual(S.median([3, 1, 2]), 2.0)
        self.assertEqual(S.median([4, 1, 2, 3]), 2.5)
        self.assertEqual(S.mad([1, 2, 3, 4, 100]), 1.0)            # robust to the outlier
        self.assertEqual(S.mad([5, 5, 5]), 0.0)
        self.assertEqual(S.mad([1, 2, 3], center=0), 2.0)
        self.assertEqual(S.quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(S.quantile([10, 0, 5], 0), 0)
        self.assertEqual(S.quantile([10, 0, 5], 1), 10)
        self.assertAlmostEqual(S.quantile(range(101), 0.9), 90.0)
        self.assertEqual(S.clip(5, 0, 1), 1)
        self.assertEqual(S.clip(-5, 0, 1), 0)

    def test_bad_input_raises_value_error(self):
        for fn, args in [(S.median, ([],)), (S.mad, ([],)), (S.quantile, ([], 0.5)), (S.quantile, ([1], 2)),
                         (S.median, ([1, float("nan")],)), (S.median, ([1, "2"],)), (S.median, ([True],)),
                         (S.median, ("123",)), (S.median, (None,)), (S.clip, (1, 2, 0))]:
            with self.assertRaises(ValueError, msg=f"{fn.__name__}{args}"):
                fn(*args)


class RobustZTests(unittest.TestCase):
    def test_formula(self):
        base = [8, 10, 12, 9, 11, 10, 10]
        med = statistics.median(base)
        mad = statistics.median([abs(b - med) for b in base])
        self.assertAlmostEqual(S.robust_z(16, base), (16 - med) / (1.4826 * mad + 0.1))
        self.assertLess(S.robust_z(4, base), 0)
        self.assertEqual(S.robust_z(med, base), 0.0)

    def test_counts_formula_matches_the_research_report(self):
        base = [5, 7, 6, 9, 4, 6, 8, 7, 5, 6, 7, 6, 8, 5]
        logs = [math.log1p(b) for b in base]
        med = statistics.median(logs)
        mad = statistics.median([abs(v - med) for v in logs])
        expected = (math.log1p(30) - med) / (1.4826 * mad + 0.1)
        self.assertAlmostEqual(S.robust_z_counts(30, base), expected, places=12)

    def test_floor_keeps_constant_baselines_finite(self):
        z = S.robust_z_counts(40, [10] * 14)
        self.assertAlmostEqual(z, (math.log1p(40) - math.log1p(10)) / 0.1)
        self.assertTrue(math.isfinite(z))
        self.assertEqual(S.robust_z(10, [10, 10, 10], floor=0), 0.0)
        self.assertEqual(S.robust_z(11, [10, 10, 10], floor=0), math.inf)
        self.assertEqual(S.robust_z(9, [10, 10, 10], floor=0), -math.inf)

    def test_log_counts_are_multiplicative(self):
        # Twice the usual level scores about the same whatever the size of the counts.
        small = S.robust_z_counts(200, [100, 90, 110, 95, 105])
        large = S.robust_z_counts(20000, [10000, 9000, 11000, 9500, 10500])
        self.assertAlmostEqual(small, large, delta=0.1)

    def test_errors(self):
        for args in [(1, []), ("x", [1]), (1, [1, None]), (float("inf"), [1])]:
            with self.assertRaises(ValueError, msg=repr(args)):
                S.robust_z(*args)
        with self.assertRaises(ValueError):
            S.robust_z_counts(1, [])
        with self.assertRaises(ValueError):
            S.robust_z_counts(-1, [1, 2, 3])
        with self.assertRaises(ValueError):
            S.robust_z_counts(1, [1, -2, 3])
        with self.assertRaises(ValueError):
            S.robust_z(1, [1, 2], floor=-0.1)


class EwmaTests(unittest.TestCase):
    def test_alpha_from_halflife_and_span(self):
        self.assertEqual(S.ewma_alpha(0.25), 0.25)
        self.assertAlmostEqual(S.ewma_alpha(halflife=1), 0.5)
        self.assertAlmostEqual((1 - S.ewma_alpha(halflife=10)) ** 10, 0.5)
        self.assertAlmostEqual(S.ewma_alpha(span=9), 0.2)
        self.assertEqual(S.ewma_alpha(span=1), 1.0)
        for kw in [{}, {"alpha": 0.5, "span": 3}, {"alpha": 0}, {"alpha": 1.5}, {"halflife": 0}, {"span": 0.5},
                   {"alpha": float("nan")}]:
            with self.assertRaises(ValueError, msg=repr(kw)):
                S.ewma_alpha(**kw)

    def test_streaming_mean_and_variance_match_the_weighted_formulas(self):
        rng = random.Random(7)
        xs = [rng.uniform(0, 50) for _ in range(40)]
        a = 0.15
        e = S.EWMA(a)
        for x in xs:
            e.update(x)
        n = len(xs)
        w = [(1 - a) ** (n - 1)] + [a * (1 - a) ** (n - 1 - i) for i in range(1, n)]
        self.assertAlmostEqual(sum(w), 1.0)
        mean = sum(wi * xi for wi, xi in zip(w, xs))
        var = sum(wi * (xi - mean) ** 2 for wi, xi in zip(w, xs))
        self.assertAlmostEqual(e.mean, mean, places=9)
        self.assertAlmostEqual(e.var, var, places=7)
        self.assertAlmostEqual(e.std, math.sqrt(var), places=7)
        self.assertEqual(e.n, 40)

    def test_first_value_and_z(self):
        e = S.EWMA(0.5)
        self.assertIsNone(e.mean)
        self.assertIsNone(e.z(3))
        self.assertEqual(e.update(10), 10)
        self.assertIsNone(e.z(12))                                 # no spread yet
        self.assertEqual(e.z(12, floor=1), 2.0)
        e.update(20)
        self.assertEqual(e.mean, 15)
        self.assertAlmostEqual(e.var, 0.5 * (0 + 10 * 5))
        self.assertAlmostEqual(e.z(15 + e.std), 1.0)

    def test_steps_decay_the_old_level_across_a_gap(self):
        gap = S.EWMA(0.2)
        gap.update(100)
        gap.update(0, steps=3)                                     # two hours missed
        stepwise = S.EWMA(0.2)
        stepwise.update(100)
        expected = 100 * 0.8 ** 3                                  # weight of the new value: 1 - 0.8 ** 3
        self.assertAlmostEqual(gap.mean, expected)
        with self.assertRaises(ValueError):
            gap.update(1, steps=0)
        with self.assertRaises(ValueError):
            gap.update(float("inf"))

    def test_round_trip_through_json(self):
        e = S.EWMA(halflife=24)
        for x in (5, 7, 6, 30, 8):
            e.update(x)
        back = S.EWMA.from_dict(json.loads(json.dumps(e.to_dict())))
        self.assertEqual((back.alpha, back.mean, back.var, back.n), (e.alpha, e.mean, e.var, e.n))
        self.assertEqual(back.update(9), e.update(9))
        empty = S.EWMA.from_dict(S.EWMA(0.3).to_dict())
        self.assertIsNone(empty.mean)
        for bad in (None, [], {"alpha": 2}, {"alpha": 0.3, "mean": "x"}, {"alpha": 0.3, "mean": 1, "var": -1},
                    {"alpha": 0.3, "n": 1.5}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                S.EWMA.from_dict(bad)
        self.assertIn("EWMA(alpha=", repr(e))

    def test_series_with_gaps(self):
        out = S.ewma([None, 10, 20, None, None, 0], 0.5)
        self.assertEqual(out[:4], [None, 10, 15, 15])
        self.assertEqual(out[4], 15)
        self.assertAlmostEqual(out[5], 15 * 0.5 ** 3)              # three steps since the last value
        self.assertEqual(S.ewma([], span=3), [])
        self.assertEqual(S.ewma([1, 2, 3], 1.0), [1, 2, 3])


class PoissonTests(unittest.TestCase):
    def test_pmf_sums_to_one(self):
        for lam in (0.3, 4, 25):
            self.assertAlmostEqual(sum(S.poisson_pmf(k, lam) for k in range(200)), 1.0, places=12)
        self.assertEqual(S.poisson_pmf(0, 0), 1.0)
        self.assertEqual(S.poisson_pmf(2, 0), 0.0)
        self.assertEqual(S.poisson_pmf(-1, 3), 0.0)

    def test_tails_match_direct_sums(self):
        for lam in (0.1, 0.5, 1, 3, 7.5, 20, 60):
            for k in range(1, 120):
                self.assertAlmostEqual(S.poisson_sf(k, lam) / upper_tail(k, lam), 1.0, places=9, msg=(k, lam))
            for k in range(0, int(lam) + 1):
                self.assertAlmostEqual(S.poisson_cdf(k, lam) / lower_tail(k, lam), 1.0, places=9, msg=(k, lam))

    def test_tails_are_complementary(self):
        for k in range(30):
            self.assertAlmostEqual(S.poisson_cdf(k, 7.3) + S.poisson_sf(k + 1, 7.3), 1.0, places=12)
            self.assertAlmostEqual(S.poisson_sf(k, 7.3) - S.poisson_sf(k + 1, 7.3), S.poisson_pmf(k, 7.3), places=12)

    def test_edges(self):
        self.assertEqual(S.poisson_sf(0, 5), 1.0)
        self.assertEqual(S.poisson_sf(-3, 5), 1.0)
        self.assertEqual(S.poisson_sf(1, 0), 0.0)
        self.assertEqual(S.poisson_cdf(-1, 5), 0.0)
        self.assertEqual(S.poisson_cdf(0, 0), 1.0)
        self.assertEqual(S.poisson_sf(3.0, 2), S.poisson_sf(3, 2))            # whole floats are fine
        for args in [(2.5, 1), (1, -1), ("3", 1), (True, 1), (1, float("nan"))]:
            with self.assertRaises(ValueError, msg=repr(args)):
                S.poisson_sf(*args)

    def test_large_rates_behave_like_the_normal_approximation(self):
        lam = 10_000
        p = S.poisson_sf(lam + 300, lam)                           # three standard deviations up
        self.assertAlmostEqual(p, 0.00135, delta=0.0003)
        self.assertAlmostEqual(S.poisson_cdf(lam - 300, lam), 0.00135, delta=0.0003)

    def test_extreme_tails_do_not_underflow(self):
        s = S.poisson_surprise(100, 1.0)
        exact = -(-1.0 - math.lgamma(101)) / math.log(10)          # the k = 100 term dominates
        self.assertAlmostEqual(s, exact, delta=0.01)
        self.assertGreater(S.poisson_surprise(0, 500, "low"), 200)
        self.assertEqual(S.poisson_surprise(3, 0), S.SURPRISE_MAX)
        self.assertEqual(S.poisson_surprise(5000, 1), S.SURPRISE_MAX)
        self.assertEqual(S.poisson_surprise(0, 3), 0.0)
        self.assertAlmostEqual(S.poisson_surprise(8, 2), -math.log10(S.poisson_sf(8, 2)))
        self.assertEqual(S.poisson_tail(8, 2, "low"), S.poisson_cdf(8, 2))
        with self.assertRaises(ValueError):
            S.poisson_surprise(3, 1, "up")

    def test_p_to_z(self):
        self.assertAlmostEqual(S.p_to_z(0.5), 0.0)
        self.assertAlmostEqual(S.p_to_z(0.0013499), 3.0, places=3)
        self.assertAlmostEqual(S.p_to_z(1e-300), 37.05, places=1)
        self.assertEqual(S.p_to_z(0.0), S.p_to_z(1e-300))
        self.assertLess(S.p_to_z(1.0), -8)
        with self.assertRaises(ValueError):
            S.p_to_z(1.5)

    def test_volume_normalisation(self):
        self.assertEqual(S.expected_count(4.0), 4.0)
        self.assertEqual(S.expected_count(4.0, 80_000, 100_000), 3.2)        # a quieter day overall
        with self.assertRaises(ValueError):
            S.expected_count(4.0, 80_000)
        with self.assertRaises(ValueError):
            S.expected_count(4.0, 80_000, 0)
        self.assertEqual(S.scale_to_volume([10, 20, 5], [100, 200, 0], 150), [15.0, 15.0])
        with self.assertRaises(ValueError):
            S.scale_to_volume([1], [1, 2], 3)


class AssessTests(unittest.TestCase):
    def test_building_until_enough_samples(self):
        r = S.assess_count(40, [10] * 9)
        self.assertFalse(r["ready"])
        self.assertEqual(r["status"], "Building baseline: 9 of 14 days")
        self.assertIsNone(r["z"])
        self.assertEqual(r["median"], 10)
        self.assertEqual(r["ratio"], 4.0)
        r = S.assess_count(3, [], min_samples=4, unit="weeks")
        self.assertEqual((r["ready"], r["status"], r["median"], r["ratio"]),
                         (False, "Building baseline: 0 of 4 weeks", None, None))
        self.assertEqual(S.building_text(3, 14), "Building baseline: 3 of 14 days")

    def test_robust_z_path_for_larger_baselines(self):
        base = [100, 120, 90, 110, 105, 95, 115, 100, 98, 102, 107, 93, 111, 99]
        r = S.assess_count(412, base)
        self.assertTrue(r["ready"])
        self.assertEqual(r["method"], "robust z")
        self.assertEqual(r["status"], "Baseline of 14 days")
        self.assertAlmostEqual(r["z"], S.robust_z_counts(412, base))
        self.assertGreater(r["z"], 3)
        self.assertAlmostEqual(r["ratio"], 412 / statistics.median(base))
        self.assertIsNone(r["p"])
        low = S.assess_count(20, base, direction="low")
        self.assertLess(low["z"], -3)

    def test_poisson_path_for_small_baselines(self):
        base = [1, 0, 2, 1, 0, 1, 3, 1, 0, 2, 1, 1, 0, 2]
        r = S.assess_count(9, base)
        self.assertEqual(r["method"], "Poisson")
        lam = statistics.fmean(base)
        self.assertAlmostEqual(r["expected"], lam)
        self.assertAlmostEqual(r["p"], S.poisson_sf(9, lam))
        self.assertLess(r["p"], 0.001)
        self.assertAlmostEqual(r["surprise"], -math.log10(r["p"]))
        self.assertAlmostEqual(r["z"], S.p_to_z(r["p"]))
        quiet = S.assess_count(1, base)
        self.assertGreater(quiet["p"], 0.3)
        scaled = S.assess_count(9, base, expected=S.expected_count(lam, 50_000, 100_000))
        self.assertLess(scaled["p"], r["p"])                       # half the usual volume today
        low = S.assess_count(0, [6, 4, 5, 3, 4, 4, 5], min_samples=7, poisson_below=10, direction="low")
        self.assertEqual(low["method"], "Poisson")
        self.assertAlmostEqual(low["p"], S.poisson_cdf(0, statistics.fmean([6, 4, 5, 3, 4, 4, 5])))
        self.assertLess(low["z"], 0)

    def test_exposure_corrects_partly_covered_windows(self):
        base = [100] * 14
        full = S.assess_count(100, base)
        part = S.assess_count(90, base, exposure=0.9)              # 90 seen in 90% of the window
        self.assertAlmostEqual(part["z"], full["z"])
        self.assertAlmostEqual(part["ratio"], 1.0)
        small = S.assess_count(2, [2] * 14, exposure=0.5)
        self.assertAlmostEqual(small["expected"], 1.0)

    def test_errors(self):
        for kw in [{"min_samples": 0}, {"direction": "up"}, {"exposure": 0}, {"exposure": 1.5}, {"min_samples": 2.5}]:
            with self.assertRaises(ValueError, msg=repr(kw)):
                S.assess_count(1, [1, 2, 3], **kw)
        with self.assertRaises(ValueError):
            S.assess_count(-1, [1, 2, 3])
        with self.assertRaises(ValueError):
            S.assess_count(1, [1, -2, 3])


class SeasonalTests(unittest.TestCase):
    def test_hour_of_day_and_week(self):
        self.assertEqual(S.hour_of_week(MONDAY), 0)
        self.assertEqual(S.hour_of_week(MONDAY + 5 * H + 59), 5)
        self.assertEqual(S.hour_of_week(MONDAY - 1), 167)            # Sunday 23:59
        self.assertEqual(S.hour_of_week(MONDAY + 3 * DAY + 14 * H), 3 * 24 + 14)
        self.assertEqual(S.hour_of_week(0), 72)                     # 1970-01-01 was a Thursday
        self.assertEqual(S.hour_of_week(MONDAY - H, utc_offset_s=H), 0)   # UTC+1: already Monday
        self.assertEqual(S.hour_of_day(MONDAY + 13 * H + 1), 13)
        self.assertEqual(S.hour_of_day(MONDAY, utc_offset_s=-5 * H), 19)
        self.assertEqual(S.hour_start(MONDAY + 3599.9), MONDAY)
        self.assertEqual(S.hour_start(-1), -H)

    def test_windows(self):
        self.assertEqual(S.seasonal_windows(MONDAY, periods=2),
                         [(MONDAY - WEEK - H, MONDAY - WEEK), (MONDAY - 2 * WEEK - H, MONDAY - 2 * WEEK)])
        w = S.seasonal_windows(MONDAY, window_s=6 * H, period_s=DAY, periods=21)
        self.assertEqual(len(w), 21)
        self.assertEqual(w[0], (MONDAY - DAY - 6 * H, MONDAY - DAY))
        self.assertEqual(w[-1], (MONDAY - 21 * DAY - 6 * H, MONDAY - 21 * DAY))
        for kw in [{"periods": 0}, {"window_s": 0}, {"window_s": 2 * DAY, "period_s": DAY}, {"periods": True}]:
            with self.assertRaises(ValueError, msg=repr(kw)):
                S.seasonal_windows(MONDAY, **kw)

    def test_window_total_and_uptime_mask(self):
        hourly = {MONDAY: 5, MONDAY + H: 7}                          # the third hour saw nothing
        self.assertEqual(S.window_total(hourly, MONDAY, MONDAY + 3 * H), (12, 1.0))
        up = {MONDAY: 1.0, MONDAY + H: 0.5}                         # third hour: no heartbeat at all
        total, cov = S.window_total(hourly, MONDAY, MONDAY + 3 * H, uptime=up)
        self.assertEqual(total, 12)
        self.assertAlmostEqual(cov, 0.5)
        self.assertAlmostEqual(S.window_total(hourly, MONDAY, MONDAY + 2 * H, uptime=lambda h: 0.9)[1], 0.9)
        self.assertEqual(S.window_total(hourly, MONDAY, MONDAY + 2 * H, uptime=lambda h: 7)[1], 1.0)  # clipped
        with self.assertRaises(ValueError):
            S.window_total(hourly, MONDAY + 10, MONDAY + 20)        # no whole hour
        with self.assertRaises(ValueError):
            S.window_total(hourly, MONDAY, MONDAY + H, uptime="always")

    def test_window_sample_drops_downtime_and_scales_partial_windows(self):
        hourly = {MONDAY - k * WEEK - H: 10 for k in range(1, 5)}
        up = {MONDAY - WEEK - H: 1.0, MONDAY - 2 * WEEK - H: 0.9, MONDAY - 3 * WEEK - H: 0.5}   # week 4: off
        values, dropped = S.window_sample(hourly, S.seasonal_windows(MONDAY), uptime=up)
        self.assertEqual(dropped, 2)
        self.assertEqual(len(values), 2)
        self.assertAlmostEqual(values[0], 10)
        self.assertAlmostEqual(values[1], 10 / 0.9)
        raw, _ = S.window_sample(hourly, S.seasonal_windows(MONDAY), uptime=up, scale=False)
        self.assertEqual(raw, [10, 10])
        with self.assertRaises(ValueError):
            S.window_sample(hourly, [], min_coverage=0)

    def test_coverage_by_hour(self):
        beats = [MONDAY + i * 300 for i in range(12)]                       # a full hour
        beats += [MONDAY + H + i * 300 for i in range(6)]                   # half the next one
        beats += [MONDAY + 10, MONDAY + 20]                                 # duplicates in one slot
        cov = S.coverage_by_hour(beats)
        self.assertEqual(cov, {MONDAY: 1.0, MONDAY + H: 0.5})
        self.assertNotIn(MONDAY + 2 * H, cov)
        with self.assertRaises(ValueError):
            S.coverage_by_hour(beats, every_s=0)

    def _weekly_series(self, weeks=5, base=20, amplitude=15, seed=3):
        """Hourly counts with a daily cycle and quieter weekends, for `weeks` weeks ending at MONDAY."""
        rng = random.Random(seed)
        out = {}
        start = MONDAY - weeks * WEEK
        for h in range(weeks * 168):
            t = start + h * H
            how = S.hour_of_week(t)
            level = base + amplitude * math.sin(2 * math.pi * (how % 24) / 24) - (8 if how >= 120 else 0)
            out[t] = max(0, round(level + rng.uniform(-2, 2)))
        return out

    def test_seasonal_baseline_steady_cycle_and_spike(self):
        hourly = self._weekly_series()
        b = S.SeasonalBaseline(period="week", periods=4, min_samples=3)
        end = MONDAY                                                     # assess Sunday 23:00 to Monday 00:00
        steady = b.assess(hourly, end)
        self.assertTrue(steady["ready"])
        self.assertEqual((steady["n"], steady["dropped"], steady["coverage"]), (4, 0, 1.0))
        self.assertLess(abs(steady["z"]), 2.5)
        hourly[MONDAY - H] = 90                                          # a real spike in the last hour
        spike = b.assess(hourly, end)
        self.assertGreater(spike["z"], 3)
        self.assertGreater(spike["ratio"], 2)

    def test_seasonal_baseline_daily_windows_with_outage(self):
        hourly = self._weekly_series(weeks=4)
        up = {t: 1.0 for t in hourly}
        for t in list(up):                                               # the PC slept for three days
            if MONDAY - 10 * DAY <= t < MONDAY - 7 * DAY:
                up[t] = 0.0
        b = S.SeasonalBaseline(period="day", window_hours=6, periods=21, min_samples=10)
        r = b.assess(hourly, MONDAY, uptime=up)
        self.assertEqual(r["dropped"], 3)
        self.assertEqual(r["n"], 18)
        self.assertTrue(r["ready"])
        self.assertEqual(r["unit"], "days")
        strict = S.SeasonalBaseline(period="day", window_hours=6, periods=21, min_samples=20)
        self.assertEqual(strict.assess(hourly, MONDAY, uptime=up)["status"], "Building baseline: 18 of 20 days")

    def test_current_window_needs_uptime(self):
        hourly = self._weekly_series()
        up = {t: 1.0 for t in hourly}
        up[MONDAY - H] = 0.2                                             # Meridian was off for most of it
        r = S.SeasonalBaseline().assess(hourly, MONDAY, uptime=up)
        self.assertFalse(r["ready"])
        self.assertEqual(r["status"], "Not enough uptime in this window")
        self.assertIsNone(r["z"])
        self.assertAlmostEqual(r["coverage"], 0.2)

    def test_partial_current_window_is_scaled(self):
        hourly = {MONDAY - k * DAY - H: 100 for k in range(1, 15)}
        hourly[MONDAY - H] = 90
        up = lambda h: 0.9 if h == MONDAY - H else 1.0
        r = S.SeasonalBaseline(period="day", periods=14).assess(hourly, MONDAY, uptime=up)
        self.assertTrue(r["ready"])
        self.assertAlmostEqual(r["ratio"], 1.0)                          # 90 in 90% of the hour = usual
        self.assertAlmostEqual(r["exposure"], 0.9)
        unscaled = S.SeasonalBaseline(period="day", periods=14, scale=False).assess(hourly, MONDAY, uptime=up)
        self.assertAlmostEqual(unscaled["ratio"], 0.9)

    def test_constructor_validation(self):
        for kw in [{"period": "month"}, {"window_hours": 0}, {"period": "day", "window_hours": 25},
                   {"periods": 0}, {"min_samples": 0}, {"periods": 3, "min_samples": 4}, {"min_coverage": 0},
                   {"periods": 2.5}]:
            with self.assertRaises(ValueError, msg=repr(kw)):
                S.SeasonalBaseline(**kw)
        self.assertEqual(S.SeasonalBaseline(periods=2).min_samples, 2)
        self.assertEqual(S.SeasonalBaseline(period="day").min_samples, 14)


class SeasonalEwmaTests(unittest.TestCase):
    def test_slots_learn_their_own_level(self):
        prof = S.SeasonalEWMA(alpha=0.5)
        for week in range(6):
            for how in (0, 12):
                t = MONDAY + week * WEEK + how * H
                prof.update(t, 50 if how == 12 else 5)
        self.assertAlmostEqual(prof.expected(MONDAY + 7 * WEEK + 12 * H), 50)
        self.assertAlmostEqual(prof.expected(MONDAY + 7 * WEEK), 5)
        self.assertIsNone(prof.expected(MONDAY + 3 * H))
        self.assertEqual(prof.seen(MONDAY + 12 * H), 6)
        self.assertEqual(prof.seen(MONDAY + 3 * H), 0)
        self.assertIsNone(prof.z(MONDAY + 3 * H, 9))

    def test_repeats_and_old_hours_are_ignored(self):
        prof = S.SeasonalEWMA(alpha=0.5)
        prof.update(MONDAY, 10)
        self.assertEqual(prof.update(MONDAY + 60, 1000), 10)         # same hour again: ignored
        self.assertEqual(prof.update(MONDAY - WEEK, 1000), 10)       # older: ignored
        self.assertEqual(prof.seen(MONDAY), 1)

    def test_missed_weeks_decay_the_slot(self):
        prof = S.SeasonalEWMA(alpha=0.5)
        prof.update(MONDAY, 100)
        prof.update(MONDAY + 3 * WEEK, 0)                            # two weekly values missed
        self.assertAlmostEqual(prof.expected(MONDAY), 100 * 0.5 ** 3)

    def test_hour_of_day_profile_and_round_trip(self):
        prof = S.SeasonalEWMA(span=7, slots=24, utc_offset_s=H)
        for d in range(10):
            prof.update(MONDAY + d * DAY + 8 * H, 30 + d)
            prof.update(MONDAY + d * DAY + 20 * H, 3)
        self.assertEqual(prof.slot(MONDAY + 8 * H), 9)
        back = S.SeasonalEWMA.from_dict(json.loads(json.dumps(prof.to_dict())))
        t = MONDAY + 11 * DAY + 8 * H
        self.assertAlmostEqual(back.expected(t), prof.expected(t))
        self.assertAlmostEqual(back.z(t, 50), prof.z(t, 50))
        self.assertEqual(back.update(t, 40), prof.update(t, 40))
        for bad in [None, {"alpha": 0.3, "state": []}, {"alpha": 0.3, "state": {"999": {"alpha": 0.3, "mean": 1, "last": 0}}},
                    {"alpha": 0.3, "slots": 12}, {"alpha": 0.3, "state": {"x": {}}}]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                S.SeasonalEWMA.from_dict(bad)
        with self.assertRaises(ValueError):
            S.SeasonalEWMA(alpha=0.3, slots=48)


if __name__ == "__main__":
    unittest.main()
