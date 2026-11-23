"""Infra electricity tests: British National Grid conversion, the shape builds, the NESO, Elexon and
Energy-Charts parsers, and the grid service (polling gates, 429 back-off, persistence, views, the
point lookup and the AI tools). No network: fixtures in tests/fixtures/infra/energy_*.json are
trimmed real answers (NESO CC BY 4.0, Elexon BMRS data licence, Energy-Charts CC BY 4.0).

Run: python -m unittest tests.test_infra_energy -v
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from meridian.config import Config
from meridian.modules.infra import energy, energy_geo, energy_service

from tests.test_infra import FIX, FakeNet, FakeResponse

NOW = 1795446600          # 2026-11-23 15:10 UTC, inside every fixture's window
ROUTES = {
    "regional/intensity": "energy_ci_region13.json", "regional/postcode/RG1": "energy_ci_postcode.json",
    "carbonintensity.org.uk/regional": "energy_ci_regional.json",
    "carbonintensity.org.uk/intensity/": "energy_ci_intensity.json",
    "datasets/FUELINST": "energy_elexon_fuelinst.json", "demand/outturn": "energy_elexon_indo.json",
    "market-index": "energy_elexon_mid.json",
    "energy-charts.info/signal?country=xk": "energy_ec_signal_empty.json",
    "energy-charts.info/signal": "energy_ec_signal_fr.json", "public_power": "energy_ec_power_de.json",
    "energy-charts.info/price": "energy_ec_price_fr.json",
}


def fx(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def square(lon: float, lat: float, d: float) -> list:
    return [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]


class LimitedNet(FakeNet):
    """Answers HTTP 429 with Retry-After for Energy-Charts endpoints named in ``limited``."""

    def __init__(self, routes, limited=()):
        super().__init__(routes)
        self.limited = set(limited)

    def request(self, url, *, params=None, headers=None, **kw):
        from meridian.net import Net
        full = Net.build_url(url, params)
        if any(f"energy-charts.info/{name}" in full for name in self.limited):
            self.calls.append((full, kw))
            resp = FakeResponse(b'{"detail": "Too Many Requests"}', "application/json", status=429)
            resp.headers["retry-after"] = "120"
            return resp
        return super().request(url, params=params, headers=headers, **kw)


class FakeMarkets:
    def eur_rates(self):
        return {"EUR": 1.0, "GBP": 0.84, "USD": 1.1}


def make_env(tmp: Path, net=None):
    statuses: dict[str, dict] = {}
    activity: dict[str, float] = {}
    m = SimpleNamespace(
        name="infra", net=net or FakeNet(ROUTES), data_dir=tmp / "build", cache_dir=tmp / "cache", raw_dir=tmp / "raw",
        log=logging.getLogger("test.energy"), config=Config(env_file=None), app=SimpleNamespace(scheduler=None),
        status=lambda sid, **kw: statuses.__setitem__(sid, kw),
        touch=lambda key=None: activity.__setitem__(key or "infra", time.time()),
        active=lambda key=None, within=180.0: time.time() - activity.get(key or "infra", 0) <= within)
    return m, statuses


# ---------------------------------------------------------------------------------------------
class GridGeometryTests(unittest.TestCase):
    def test_national_grid_to_wgs84(self):
        # Ordnance Survey worked example (A guide to coordinate systems in Great Britain, annex C).
        lat, lon = energy_geo.bng_to_osgb36(651409.903, 313177.270)
        self.assertAlmostEqual(lat, 52.65757030, places=7)
        self.assertAlmostEqual(lon, 1.71792158, places=7)
        # Elizabeth Tower, London: about 51.5007 N, 0.1246 W in WGS84 (Helmert shift ~5 m).
        lat, lon = energy_geo.bng_to_wgs84(530268, 179640)
        self.assertAlmostEqual(lat, 51.5007, delta=0.0006)
        self.assertAlmostEqual(lon, -0.1246, delta=0.0006)

    def test_simplify_and_label_point(self):
        ring = [[x / 10.0, 0.0] for x in range(0, 101)] + [[10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]
        simple = energy_geo.simplify_ring(ring, 0.01)
        self.assertLess(len(simple), 10)
        self.assertEqual(simple[0], simple[-1])
        # A "C" shape whose centroid falls in the gap: the label point must still be inside.
        c = [[0, 0], [10, 0], [10, 2], [2, 2], [2, 8], [10, 8], [10, 10], [0, 10], [0, 0]]
        x, y = energy_geo.interior_point(c)
        self.assertTrue(energy_geo._in_ring(x, y, c))

    def test_build_countries_drops_overseas_parts_and_northern_ireland(self):
        ne = {"type": "FeatureCollection", "features": [
            {"properties": {"ISO_A2": "-99", "ISO_A2_EH": "FR", "NAME": "France", "LABEL_X": 2.5, "LABEL_Y": 46.7},
             "geometry": {"type": "MultiPolygon", "coordinates": [[square(2.5, 46.5, 3)], [square(-53.0, 4.0, 1)]]}},
            {"properties": {"ISO_A2": "GB", "ISO_A2_EH": "GB", "NAME": "United Kingdom", "LABEL_X": -2.1, "LABEL_Y": 53.9},
             "geometry": {"type": "MultiPolygon", "coordinates": [[square(-2.0, 53.5, 2)], [square(-6.7, 54.6, 0.6)]]}},
            {"properties": {"ISO_A2": "RU", "ISO_A2_EH": "RU", "NAME": "Russia"},
             "geometry": {"type": "Polygon", "coordinates": [square(40.0, 55.0, 2)]}},
        ]}
        fc = energy_geo.build_countries(ne, {"FR": "France", "GB": "Great Britain"})
        by = {f["id"]: f for f in fc["features"]}
        self.assertEqual(set(by), {"FR", "GB"})
        self.assertEqual(by["FR"]["geometry"]["type"], "Polygon")       # French Guiana dropped
        self.assertEqual(by["GB"]["geometry"]["type"], "Polygon")       # Northern Ireland dropped
        self.assertEqual(by["FR"]["properties"]["label"], [2.5, 46.7])
        self.assertEqual(by["GB"]["properties"]["name"], "Great Britain")

    def test_build_regions_reprojects_and_maps_ids(self):
        e, n = 530000.0, 180000.0          # central London in National Grid metres
        ring = [[e - 5000, n - 5000], [e + 5000, n - 5000], [e + 5000, n + 5000], [e - 5000, n + 5000], [e - 5000, n - 5000]]
        dno = {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::27700"}},
               "features": [{"properties": {"ID": 12, "Name": "_C", "DNO": "UKPN", "Area": "London", "DNO_Full": "UK Power Networks"},
                             "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]}},
                            {"properties": {"ID": 99}, "geometry": {"type": "Polygon", "coordinates": [ring]}}]}
        fc = energy_geo.build_regions(dno)
        self.assertEqual(len(fc["features"]), 1)
        f = fc["features"][0]
        self.assertEqual((f["id"], f["properties"]["dno_id"], f["properties"]["name"]), (13, 12, "London"))
        lon, lat = f["properties"]["label"]
        self.assertAlmostEqual(lat, 51.51, delta=0.02)
        self.assertAlmostEqual(lon, -0.12, delta=0.02)


# ---------------------------------------------------------------------------------------------
class NesoParserTests(unittest.TestCase):
    def test_series_now_and_windows(self):
        s = energy.parse_ci_series(fx("energy_ci_intensity.json"))
        self.assertEqual(len(s), 97)
        cur = energy.intensity_now(s, NOW)
        self.assertEqual((cur["value"], cur["index"]), (213.0, "high"))
        self.assertEqual(cur["last_actual"], 218.0)
        self.assertLessEqual(cur["last_actual_t"], NOW)
        w = energy.window_extremes(s, NOW)
        self.assertLess(w["cleanest"]["avg"], w["dirtiest"]["avg"])
        self.assertEqual(w["cleanest"]["to"] - w["cleanest"]["from"], 3 * 3600)
        fut = [r for r in s if r["to"] > NOW and r["t"] < NOW + 86400]
        best = min(sum(r["forecast"] for r in fut[i:i + 6]) / 6 for i in range(len(fut) - 5))
        self.assertEqual(w["cleanest"]["avg"], round(best))
        sp = energy.sparkline_points(s, NOW)
        self.assertTrue(all(t < NOW for t, _ in sp["past"]))
        self.assertTrue(sp["future"][0][0] <= NOW < sp["future"][0][0] + 1800)
        self.assertEqual(energy.parse_ci_series({"data": [{"from": "nonsense"}]}), [])

    def test_regional_and_shares(self):
        r = energy.parse_ci_regional(fx("energy_ci_regional.json"))
        self.assertEqual([x["id"] for x in r["regions"]], [1, 7, 13])
        self.assertEqual(set(r["aggregates"]), {"England", "Scotland", "Wales", "GB"})
        gb = r["aggregates"]["GB"]
        self.assertEqual(gb["mix"][0]["fuel"], "gas")
        self.assertAlmostEqual(gb["renewable_pct"], 6.4 + 0 + 5.5 + 19.4, places=1)
        self.assertAlmostEqual(gb["low_carbon_pct"], gb["renewable_pct"] + 13.2, places=1)
        london = r["regions"][2]
        self.assertEqual((london["name"], london["index"]), ("London", "very high"))

    def test_region_forecast_and_postcode(self):
        fc = energy.parse_ci_region_forecast(fx("energy_ci_region13.json"))
        self.assertEqual((fc["id"], fc["name"], len(fc["series"])), (13, "London", 12))
        pc = energy.parse_ci_region_forecast(fx("energy_ci_postcode.json"))
        self.assertEqual((pc["id"], pc["postcode"], pc["series"][0]["forecast"]), (12, "RG1", 268.0))
        self.assertTrue(energy.POSTCODE_RE.fullmatch("SW1A"))
        self.assertFalse(energy.POSTCODE_RE.fullmatch("SW1A 1AA"))


class ElexonParserTests(unittest.TestCase):
    def test_fuelinst_signed_flows(self):
        d = energy.parse_fuelinst(fx("energy_elexon_fuelinst.json"))
        self.assertEqual(time.strftime("%H:%M", time.gmtime(d["t"])), "14:25")
        links = {x["code"]: x for x in d["interconnectors"]}
        self.assertEqual(len(links), 10)
        self.assertEqual((links["INTNED"]["mw"], links["INTNED"]["direction"]), (-350, "export"))
        self.assertEqual(links["INTFR"]["direction"], "import")
        self.assertEqual(d["net_import_mw"], sum(x["mw"] for x in d["interconnectors"]))
        self.assertEqual(d["exports_mw"], 350)
        self.assertEqual(d["fuels"][0]["code"], "CCGT")
        self.assertNotIn("INTFR", {f["code"] for f in d["fuels"]})
        self.assertIsNone(energy.parse_fuelinst({"data": []}))

    def test_demand_and_price(self):
        d = energy.parse_indo(fx("energy_elexon_indo.json"))
        self.assertEqual(d["mw"], 37778)
        self.assertGreaterEqual(d["peak"]["mw"], d["mw"])
        p = energy.parse_mid(fx("energy_elexon_mid.json"))
        self.assertEqual((p["gbp_mwh"], p["provider"]), (217.07, "APXMIDP"))   # the zero-volume N2EX row is skipped


class EnergyChartsParserTests(unittest.TestCase):
    def test_signal_and_price(self):
        sig = energy.parse_ec_signal(fx("energy_ec_signal_fr.json"))
        self.assertEqual(len(sig["series"]), 96)
        self.assertEqual(energy.value_at(sig["series"], NOW), (1795446000, 20.2))
        self.assertIsNone(energy.value_at(sig["series"], NOW + 3 * 86400))
        self.assertTrue(energy.parse_ec_signal(fx("energy_ec_signal_empty.json"))["empty"])
        pr = energy.parse_ec_price(fx("energy_ec_price_fr.json"))
        self.assertEqual((pr["step"], pr["unit"]), (900, "EUR / MWh"))
        self.assertIn("SMARD", pr["licence"])
        self.assertEqual(energy.value_at(pr["series"], NOW)[1], 271.31)

    def test_signal_drops_one_and_two_step_spikes(self):
        # Italy's /signal on 23 Nov: 82% and 83%, 76% for single 15-minute steps inside a run of about 21%.
        vals = [22.8, 21.5, 82.0, 21.3, 21.5, 21.2, 83.4, 76.1, 18.9, 19.0, 19.1]
        obj = {"unix_seconds": [NOW + i * 900 for i in range(len(vals))], "share": vals}
        sig = energy.parse_ec_signal(obj)
        self.assertEqual([v for _, v in sig["series"]], [22.8, 21.5, 21.3, 21.5, 21.2, 18.9, 19.0, 19.1])
        self.assertEqual(sig["dropped"], 3)
        # Real ramps are kept: Switzerland's evening drop and a solar morning from near zero.
        ramp = [27.6, 51.9, 76.2, 82.2, 80.4, 77.2, 14.6, 6.5, 3.8, 2.8, 1.0, 0.8, 0.8, 1.0, 15.0, 40.0, 60.0]
        self.assertEqual(energy.drop_spikes(list(enumerate(ramp))), list(enumerate(ramp)))
        self.assertEqual(energy.drop_spikes([(0, 5.0), (1, 90.0)]), [(0, 5.0), (1, 90.0)])   # too few neighbours to judge
        self.assertEqual(energy.drop_spikes([(0, 80.0), (1, 81.0), (2, 2.0), (3, 80.5), (4, 79.0)]),
                         [(0, 80.0), (1, 81.0), (3, 80.5), (4, 79.0)])                          # a dip down, too

    def test_public_power_mix_and_estimate(self):
        d = energy.parse_ec_power(fx("energy_ec_power_de.json"))
        raw = fx("energy_ec_power_de.json")
        self.assertEqual(d["t"], raw["unix_seconds"][-2])          # the newest, incomplete interval is skipped
        mw = {g["cat"]: g["mw"] for g in d["mix"]}
        self.assertIn("coal", mw)                                   # hard coal, lignite and coal gas together
        total = sum(mw.values())
        est = sum(v * energy.EMISSION_FACTORS[c] for c, v in mw.items()) / total
        self.assertAlmostEqual(d["intensity_est"], est, delta=2)
        self.assertAlmostEqual(d["fossil_pct"], (mw.get("coal", 0) + mw.get("gas", 0) + mw.get("oil", 0)) / total * 100, delta=0.2)
        self.assertGreater(d["load_mw"], 0)
        self.assertTrue(energy.parse_ec_power({"unix_seconds": [], "production_types": []})["empty"])


# ---------------------------------------------------------------------------------------------
class GridServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.m, self.statuses = make_env(self.root)
        self.en = energy_service.Energy(self.m, FakeMarkets())
        self.clock = mock.patch.object(energy_service.time, "time", lambda: NOW)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.tmp.cleanup()

    def test_gb_snapshot(self):
        self.assertTrue(self.en.gb_snapshot()["pending"])
        self.assertTrue(self.en.refresh_gb())
        snap = self.en.gb_snapshot()
        self.assertFalse(snap["pending"])
        self.assertEqual(snap["intensity"]["now"]["value"], 213.0)
        self.assertEqual(snap["intensity"]["index_label"], "High")
        self.assertEqual(snap["mix"]["fuels"][0]["fuel"], "gas")
        self.assertEqual(len(snap["interconnectors"]), 10)
        link = next(x for x in snap["interconnectors"] if x["code"] == "INTNSL")
        self.assertEqual(link["far"], [6.6447, 59.5147])                  # [lon, lat] for the map
        self.assertEqual(snap["price"]["eur_mwh"], round(217.07 / 0.84, 2))
        self.assertEqual(snap["demand"]["mw"], 37778)
        self.assertTrue(any("Elexon" in a for a in snap["attribution"]))
        self.assertTrue(self.statuses[energy.SRC_CI]["ok"])
        self.assertTrue(self.statuses[energy.SRC_ELEXON]["ok"])
        row = self.en.gb_row()
        self.assertEqual((row["country"], row["intensity"], row["intensity_kind"]), ("GB", 213.0, "neso"))

    def test_gb_failure_keeps_errors_plain(self):
        m, statuses = make_env(self.root, FakeNet({}))
        en = energy_service.Energy(m)
        en.refresh_gb()
        snap = en.gb_snapshot()
        self.assertTrue(snap["pending"])
        self.assertIn("not answering", snap["errors"]["neso"])
        self.assertFalse(statuses[energy.SRC_CI]["ok"])
        self.assertFalse(statuses[energy.SRC_ELEXON]["ok"])

    def test_europe_worker_respects_gates(self):
        self.assertTrue(self.en.eu_step(NOW))          # /signal for the first country
        self.assertTrue(self.en.eu_step(NOW))          # /public_power (signal waits 4 s)
        self.assertTrue(self.en.eu_step(NOW))          # /price
        self.assertFalse(self.en.eu_step(NOW))         # every endpoint is pacing now
        urls = [u for u, _ in self.m.net.calls]
        self.assertTrue(urls[0].startswith("https://api.energy-charts.info/signal?country=de"))
        self.assertIn("public_power?country=de", urls[1])
        self.assertIn("price?bzn=DE-LU", urls[2])
        self.assertTrue(self.en.eu_step(NOW + 4.1))    # the next /signal after its 4 s spacing
        self.assertIn("signal?country=fr", self.m.net.calls[-1][0])
        self.assertFalse(self.en.eu_step(NOW + 5))
        rows = {r["country"]: r for r in self.en.europe()["countries"]}
        self.assertEqual(list(rows)[0], "GB")
        self.assertEqual(rows["DE"]["renewable_now"], 20.2)     # (the fixture is France's signal)
        self.assertIsNotNone(rows["DE"]["intensity"])
        self.assertEqual(rows["LU"]["price"], rows["DE"]["price"])   # one DE-LU zone for both
        self.assertTrue(rows["IT"]["pending"])

    def test_rate_limit_backs_off(self):
        m, statuses = make_env(self.root, LimitedNet(ROUTES, limited=("signal",)))
        en = energy_service.Energy(m)
        self.assertTrue(en.eu_step(NOW))
        gate = en.gates["signal"]
        self.assertGreaterEqual(gate.blocked_until, NOW + 120)
        self.assertEqual(gate.interval, 8.0)
        self.assertNotIn("de", en._sig)                 # nothing stored, retried after the pause
        self.assertTrue(en.eu_step(NOW))                # other endpoints carry on
        self.assertIn("public_power", m.net.calls[-1][0])
        self.assertIn("rate limit", statuses[energy.SRC_EC]["note"])

    def test_empty_country_waits_and_cache_survives_restart(self):
        self.en.prioritise("xk")
        self.assertTrue(self.en.eu_step(NOW))
        self.assertIn("signal?country=xk", self.m.net.calls[0][0])
        self.assertTrue(self.en._sig["xk"]["empty"])
        self.assertFalse(self.en._due("signal", "xk", NOW + 3600))
        self.assertTrue(self.en._due("signal", "xk", NOW + 7 * 3600))
        self.en._persist()
        again = energy_service.Energy(self.m)
        self.assertIn("xk", again._sig)
        self.assertFalse(again._due("signal", "xk", NOW + 3600))     # the back-off survives the restart

    def test_short_series_is_not_asked_again_at_once(self):
        # A country whose series stops before now (it happens upstream) waits 15 minutes, not 4 seconds.
        later = NOW + 12 * 3600
        self.assertTrue(self.en.eu_step(later))
        self.assertIn("de", self.en._sig)
        self.assertFalse(self.en._due("signal", "de", later + 60))
        self.assertTrue(self.en._due("signal", "de", later + 901))
        self.en.eu_step(later + 5)
        self.assertNotIn("signal?country=de", self.m.net.calls[-1][0])

    def test_country_without_signal_uses_latest_mix(self):
        power = energy.parse_ec_power(fx("energy_ec_power_de.json"))
        self.en._sig["ie"] = {"at": NOW, "series": [], "empty": True, "next": NOW + 3600}
        self.en._pow["ie"] = {"at": NOW, "data": power, "empty": False, "next": 0}
        row = self.en.country_row("ie", "IE", "IE(SEM)", NOW)
        self.assertEqual((row["renewable_now"], row["renewable_kind"], row["renewable_t"]),
                         (power["renewable_share_load"], "latest", power["t"]))
        self.assertIsNone(self.en.country_row("ie", "IE", "IE(SEM)", NOW + 13 * 3600)["renewable_now"])   # too old

    def test_signal_that_contradicts_the_mix_gives_way_to_it(self):
        # Switzerland on 23 Nov: /signal said 2.8% "now" (its forecast counts little but solar) while the
        # latest reported mix, mostly hydro, gave a renewable share of load of 77.2%.
        power = energy.parse_ec_power(fx("energy_ec_power_de.json"))
        power = {**power, "renewable_share_load": 77.2, "t": NOW - 4 * 3600}
        self.en._sig["ch"] = {"at": NOW, "series": [[NOW - 4 * 3600, 77.2], [NOW - 900, 3.8], [NOW, 2.8]], "empty": False, "next": 0}
        self.en._pow["ch"] = {"at": NOW, "data": power, "empty": False, "next": 0}
        row = self.en.country_row("ch", "CH", "CH", NOW)
        self.assertEqual((row["renewable_now"], row["renewable_kind"], row["renewable_note"], row["renewable_t"]),
                         (77.2, "latest", "disagrees", NOW - 4 * 3600))
        # A signal close to the mix (or a real change of a few hours) stays the live value.
        self.en._sig["ch"]["series"] = [[NOW, 60.0]]
        row = self.en.country_row("ch", "CH", "CH", NOW)
        self.assertEqual((row["renewable_now"], row["renewable_kind"], row["renewable_note"]), (60.0, "now", None))
        self.assertFalse(energy_service.signal_disagrees(30.0, 70.0))     # Germany after sunset: 2.3 times, kept
        self.assertTrue(energy_service.signal_disagrees(0.0, 40.0))
        self.assertFalse(energy_service.signal_disagrees(0.0, 20.0))      # under 30 points apart
        # A mix older than 12 hours is not used to overrule the signal.
        self.en._sig["ch"]["series"] = [[NOW + 13 * 3600, 2.8]]
        row = self.en.country_row("ch", "CH", "CH", NOW + 13 * 3600)
        self.assertEqual((row["renewable_now"], row["renewable_kind"]), (2.8, "now"))

    def test_cached_signal_is_cleaned_on_load(self):
        self.en._sig["it"] = {"at": time.time(), "series": [[NOW + i * 900, v] for i, v in enumerate([21.0, 21.5, 82.0, 21.3, 21.5])],
                              "empty": False, "next": 0}
        self.en._dirty = True
        self.en._persist()
        again = energy_service.Energy(self.m)
        self.assertEqual([v for _, v in again._sig["it"]["series"]], [21.0, 21.5, 21.3, 21.5])

    def test_no_content_is_empty_and_errors_back_off(self):
        class NoContent(FakeNet):
            def request(self, url, *, params=None, headers=None, **kw):
                from meridian.net import Net
                full = Net.build_url(url, params)
                self.calls.append((full, kw))
                if "public_power" in full:
                    return FakeResponse(b"No content available", "text/plain", status=404)
                return FakeResponse(b"<html>not json</html>", "text/html")
        m, _ = make_env(self.root, NoContent({}))
        en = energy_service.Energy(m)
        en.gates["signal"].next_at = NOW + 60          # only /public_power and /price this step
        en.eu_step(NOW)
        self.assertTrue(en._pow["de"]["empty"])
        self.assertGreaterEqual(en._pow["de"]["next"], NOW + energy_service.EMPTY_RETRY)
        en.eu_step(NOW)                                 # /price answers HTML: an error, retried later
        first = en._price["DE-LU"]["next"]
        self.assertEqual(first, NOW + energy_service.ERROR_RETRY)
        en.gates["price"].next_at = 0
        en.gates["signal"].next_at = en.gates["power"].next_at = first + 100   # let only /price run
        en.eu_step(first + 1)
        self.assertEqual(en._price["DE-LU"]["next"], first + 1 + 2 * energy_service.ERROR_RETRY)

    def test_countries_without_data_are_not_pending(self):
        empty = {"at": NOW, "series": [], "data": {"empty": True}, "empty": True, "next": NOW + 3600}
        for code, _iso, zone in energy.EC_COUNTRIES:
            self.en._sig[code] = dict(empty)
            self.en._pow[code] = dict(empty)
            if zone:
                self.en._price[zone] = dict(empty)
        data = self.en.map_data()
        self.assertEqual(data["pending"], 0)            # nothing left to fetch, so the layer stops waiting
        self.assertIsNone(data["countries"]["UA"]["r"])
        self.assertTrue(next(r for r in self.en.europe()["countries"] if r["country"] == "UA")["no_data"])

    def test_hourly_budget_caps_requests(self):
        self.en._ec_recent = [NOW - 10] * energy_service.EC_HOURLY_BUDGET
        self.assertFalse(self.en.eu_step(NOW))
        self.assertEqual(self.m.net.calls, [])
        self.assertTrue(self.en.eu_step(NOW + 3600))          # the hour has passed

    def test_views_lookup_and_detail(self):
        self.en.refresh_gb()
        for _ in range(3):
            self.en.eu_step(NOW)
        data = self.en.map_data()
        self.assertEqual(set(data["countries"]["GB"]), {"r", "i", "p", "f", "x"})
        self.assertEqual(data["regions"]["13"]["i"], 233.0)
        self.assertEqual(self.en.country_detail("gb"), {"country": "GB", "name": "Great Britain", "gb": True})
        self.assertIsNone(self.en.country_detail("US"))
        fr = self.en.country_detail("FR")
        self.assertEqual(fr["zone"], "FR")
        de = self.en.country_detail("DE")
        self.assertIsInstance(de["price"], float)                    # the number now; the series is "prices"
        self.assertEqual(de["prices"]["unit"], "EUR / MWh")
        self.assertIn("fr", self.en._priority)           # asked for, so it jumps the queue
        countries = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"id": "GB", "name": "Great Britain", "label": [-2, 53.5]},
             "geometry": {"type": "Polygon", "coordinates": [square(-2, 53.5, 3)]}},
            {"type": "Feature", "properties": {"id": "DE", "name": "Germany", "label": [10, 51]},
             "geometry": {"type": "Polygon", "coordinates": [square(10, 51, 3)]}}]}
        regions = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"id": 13, "name": "London", "dno_full": "UK Power Networks", "label": [-0.1, 51.5]},
             "geometry": {"type": "Polygon", "coordinates": [square(-0.1, 51.5, 0.3)]}}]}
        build = self.root / "build"
        build.mkdir(parents=True, exist_ok=True)
        (build / energy_geo.COUNTRIES_FILE).write_text(json.dumps(countries), encoding="utf-8")
        (build / energy_geo.REGIONS_FILE).write_text(json.dumps(regions), encoding="utf-8")
        self.en.load_shapes(build)
        hit = self.en.at(51.5, -0.1)
        self.assertEqual((hit["country"]["country"], hit["region"]["name"], hit["region"]["intensity"]), ("GB", "London", 233.0))
        self.assertEqual(self.en.at(51.0, 10.0)["country"]["country"], "DE")
        self.assertEqual(self.en.at(0.0, 0.0), {"country": None, "region": None})
        reg = self.en.region_detail(13)
        self.assertEqual(reg["forecast"]["name"], "London")
        self.assertEqual(reg["shape"]["dno_full"], "UK Power Networks")
        self.assertIn("cleanest", reg["windows"])
        self.assertIsNone(self.en.region_detail(15))

    def test_activity_gates_polling(self):
        self.clock.stop()
        try:
            self.assertFalse(self.en.wanted())
            self.en.touch()
            self.assertTrue(self.en.wanted())
        finally:
            self.clock.start()

    def test_ai_tools(self):
        gb = self.en.ai_grid_now({"country": "United Kingdom"})
        self.assertEqual((gb["country"], gb["available"]), ("GB", True))
        self.assertEqual(gb["carbon_intensity"]["g_co2_per_kwh"], 213.0)
        self.assertEqual(gb["regions_lowest_first"][0]["name"], "North Scotland")
        self.assertEqual(gb["price"]["gbp_per_mwh"], 217.07)
        self.assertTrue(gb["cleanest_3h_next_24h"]["from"].endswith("Z"))
        fr = self.en.ai_grid_now({"country": "France"})
        self.assertTrue(fr["found"])
        self.assertEqual(fr["renewable_share_of_load_now_percent"], 20.2)
        self.assertIn("NESO", fr["estimate_note"])
        nope = self.en.ai_grid_now({"country": "Atlantis"})
        self.assertFalse(nope["found"])
        self.assertIn("Great Britain", nope["message"])
        uk = self.en.ai_uk_carbon({"region": "london", "postcode": "rg1"})
        self.assertEqual(uk["national"]["index"], "high")
        self.assertEqual(uk["region"]["name"], "London")
        self.assertTrue(uk["region"]["forecast_next_24h"])
        self.assertEqual((uk["postcode"]["region"], uk["postcode"]["g_co2_per_kwh"]), ("South England", 268.0))
        bad = self.en.ai_uk_carbon({"postcode": "not a postcode"})
        self.assertIn("error", bad["postcode"])
        json.dumps(uk)                                    # JSON-ready for the AI module


if __name__ == "__main__":
    unittest.main()
