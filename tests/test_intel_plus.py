"""Unit tests for the Intelligence+ additions to the intel module (pure functions, no network):
Cloudflare Radar parsing, travel-advice parsing, level normalisation and change detection, protest rows,
news-spike statistics, and the convergence exclusions. Run: python -m unittest tests.test_intel_plus -v"""

from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

from meridian.modules.intel import advice, convergence, gdelt, protests, radar, spikes

FIX = Path(__file__).parent / "fixtures" / "intel"
NOW = advice.parse_time("2026-11-23T16:00:00Z")


def fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def row(**kw) -> list[str]:
    """A synthetic 61-column GDELT export row (defaults: a demonstration in Nairobi)."""
    d = dict(id="2001", day="20261123", a1code="KENOPP", a1name="KENYA", a1type="OPP", a2code="KENGOV", a2name="KENYA",
             a2type="GOV", root="14", code="141", quad="3", goldstein="-6.5", mentions="8", sources="2", articles="8",
             tone="-3.0", gtype="4", gname="Nairobi, Nairobi Area, Kenya", gcc="KE", glat="-1.2833", glon="36.8167",
             feature="-2213232", added="20261123150000", url="https://example.org/2026/11/23/protesters-march-through-nairobi-over-taxes")
    d.update(kw)
    r = [""] * 61
    r[gdelt.C_ID], r[gdelt.C_DAY] = d["id"], d["day"]
    r[gdelt.C_A1_CODE], r[gdelt.C_A1_NAME], r[gdelt.C_A1_TYPES[0]] = d["a1code"], d["a1name"], d["a1type"]
    r[gdelt.C_A2_CODE], r[gdelt.C_A2_NAME], r[gdelt.C_A2_TYPES[0]] = d["a2code"], d["a2name"], d["a2type"]
    r[gdelt.C_ROOT], r[gdelt.C_CODE], r[gdelt.C_QUAD], r[gdelt.C_GOLDSTEIN] = d["root"], d["code"], d["quad"], d["goldstein"]
    r[gdelt.C_MENTIONS], r[gdelt.C_SOURCES], r[gdelt.C_ARTICLES], r[gdelt.C_TONE] = d["mentions"], d["sources"], d["articles"], d["tone"]
    r[gdelt.C_GEO_TYPE], r[gdelt.C_GEO_NAME], r[gdelt.C_GEO_COUNTRY] = d["gtype"], d["gname"], d["gcc"]
    r[gdelt.C_GEO_LAT], r[gdelt.C_GEO_LON], r[gdelt.C_GEO_FEATURE] = d["glat"], d["glon"], d["feature"]
    r[gdelt.C_ADDED], r[gdelt.C_URL] = d["added"], d["url"]
    return r


# ---------------------------------------------------------------------------------------------
class TestRadar(unittest.TestCase):
    def test_anomalies(self):
        items = radar.parse_anomalies(fixture("radar_anomalies.json"))
        self.assertEqual(len(items), 8)
        gabon = next(a for a in items if a["iso2"] == "GA")
        self.assertEqual((gabon["type"], gabon["status"], gabon["verified"]), ("country", "VERIFIED", True))
        cam = next(a for a in items if a["asn"] == 65545)                  # network anomaly placed by the AS country
        self.assertEqual((cam["type"], cam["iso2"], cam["as_name"]), ("network", "CM", "EXAMPLE-NETWORK-17"))
        self.assertTrue(all(a["verified"] for a in items))                # the default answer holds TP and VERIFIED
        self.assertEqual(items, sorted(items, key=lambda a: -a["start"]))
        # severity: whole country > one network; ongoing and third-party verification add; capped at 7
        ended = dict(gabon, end=gabon["start"] + 2700)
        ongoing = dict(gabon, end=None)
        self.assertEqual(radar.anomaly_severity(ended, NOW), 6.0)
        self.assertEqual(radar.anomaly_severity(ongoing, gabon["start"] + 600), 7.0)
        self.assertLess(radar.anomaly_severity(dict(cam, end=cam["start"] + 60), NOW), radar.anomaly_severity(ended, NOW))
        shutdown = {"cause": "government directed"}
        self.assertEqual(radar.anomaly_severity(dict(ongoing, start=gabon["start"]), gabon["start"] + 600, shutdown), 7.0)
        unverified = dict(ended, status="UNVERIFIED", verified=False)
        self.assertLess(radar.anomaly_severity(unverified, NOW), 5.0)
        self.assertTrue(radar.anomaly_active(ongoing, NOW))
        self.assertFalse(radar.anomaly_active(ended, ended["end"] + 3600))

    def test_annotations_use_network_countries(self):
        items = radar.parse_annotations(fixture("radar_annotations.json"))
        salt = next(a for a in items if a["id"] == "1752")
        self.assertEqual(salt["countries"], ["CH"])                       # from asnsDetails, locations is empty
        gf = next(a for a in items if a["id"] == "1753")
        self.assertEqual((gf["cause"], gf["scope_type"]), ("power outage", "regional"))
        self.assertTrue(gf["url"].startswith("https://"))

    def test_hijacks_and_leaks(self):
        hj = radar.parse_hijacks(fixture("radar_hijacks.json"))
        self.assertGreaterEqual(len(hj), 3)
        h = next(x for x in hj if x["id"] == 174249)
        self.assertEqual((h["hijacker_asn"], h["hijacker_iso2"], h["victim_iso2"], h["confidence"]), (64498, "MY", ["MY"], 12))
        self.assertEqual(h["hijacker_name"], "Example Network 3")
        self.assertTrue(all(t["score"] > 0 for t in h["tags"]))            # zero-score tags are dropped
        self.assertFalse(h["ongoing"])                                    # on_going_count 0
        self.assertEqual(hj, sorted(hj, key=lambda x: (-x["confidence"], -x["last"])))
        self.assertEqual(radar.hijack_severity(dict(h, ongoing=False, n_prefixes=1, victim_iso2=["MY"])), 5.0)
        self.assertEqual(radar.hijack_severity(dict(h, confidence=10, ongoing=True, n_prefixes=6)), 5.0)
        self.assertLessEqual(radar.hijack_severity(dict(h, confidence=12, ongoing=True, n_prefixes=9)), 6.0)
        title = radar.hijack_title(h, lambda c: {"MY": "Malaysia"}.get(c, c))
        self.assertEqual(title, "Possible BGP hijack of networks in Malaysia by AS64498 (Example Network 3)")
        lk = radar.parse_leaks(fixture("radar_leaks.json"))
        first = lk[0]
        self.assertEqual((first["leak_asn"], first["leak_iso2"]), (65538, "EE"))   # from asn_info, not the unordered list
        self.assertEqual([s["asn"] for s in first["segment"]], [65537, 65538, 65536])
        self.assertEqual(lk, sorted(lk, key=lambda x: (-x["peers"], -x["last"])))

    def test_errors_raise(self):
        with self.assertRaises(ValueError):
            radar.parse_anomalies({"success": False, "errors": [{"message": "Authentication error"}]})
        with self.assertRaises(ValueError):
            radar.parse_hijacks({"success": True, "result": {}})
        with self.assertRaises(ValueError):
            radar.parse_leaks([])

    def test_great_circle(self):
        pts = radar.great_circle(-0.1, 51.5, 139.7, 35.7, 8)
        self.assertEqual(pts[0], [-0.1, 51.5])
        self.assertEqual(pts[-1], [139.7, 35.7])
        self.assertGreater(max(p[1] for p in pts), 60)                    # the great circle bows north
        wrap = radar.great_circle(170.0, 0.0, -170.0, 0.0, 4)             # across the antimeridian: continuous longitudes
        self.assertTrue(all(abs(b[0] - a[0]) < 30 for a, b in zip(wrap, wrap[1:])))
        self.assertAlmostEqual(wrap[-1][0], 190.0, places=3)


# ---------------------------------------------------------------------------------------------
class TestAdviceParsing(unittest.TestCase):
    def setUp(self):
        self.pages = fixture("fcdo_pages.json")

    def page(self, slug):
        return advice.parse_fcdo_country(self.pages[slug], slug)

    def test_index(self):
        idx = advice.parse_fcdo_index(fixture("fcdo_index.json"), min_countries=1)
        self.assertEqual(sorted(e["slug"] for e in idx), ["chad", "djibouti", "france", "sudan", "ukraine"])
        self.assertTrue(all(e["updated"] and e["url"].startswith("https://www.gov.uk/") for e in idx))
        with self.assertRaises(ValueError):
            advice.parse_fcdo_index(fixture("fcdo_index.json"))            # five countries is not a whole index
        with self.assertRaises(ValueError):
            advice.parse_fcdo_index({"links": {}})

    def test_levels_from_codes_and_text(self):
        sudan = self.page("sudan")                     # coded "parts", but "FCDO advises against all travel to Sudan"
        self.assertEqual((sudan["statuses"], sudan["level"], sudan["basis"]), (["avoid_all_travel_to_parts"], 4, "text"))
        self.assertEqual(sudan["label"], "Against all travel to most of the country")
        self.assertTrue(sudan["scope"]["aat_whole"])
        chad = self.page("chad")                       # "all but essential travel to the rest of Chad"
        self.assertEqual((chad["level"], chad["basis"]), (3, "text"))
        self.assertEqual(chad["label"], "Against all but essential travel, and all travel to parts")
        ukraine = self.page("ukraine")                 # "all travel to all other regions of Ukraine"
        self.assertEqual((ukraine["level"], ukraine["basis"]), (4, "text"))
        djibouti = self.page("djibouti")               # "the Djibouti-Eritrea border" is not the whole country
        self.assertEqual((djibouti["level"], djibouti["basis"]), (2, "codes"))
        self.assertEqual(djibouti["label"], "Against all travel to parts")
        france = self.page("france")
        self.assertEqual((france["level"], france["label"], france["statuses"]), (1, "No advice against travel", []))
        self.assertTrue(sudan["sentences"][0].startswith("FCDO advises against all travel to Sudan"))
        self.assertTrue(sudan["sentences"][1].startswith("This does not include"))      # exceptions keep their context
        self.assertIn("in the following regions: Volyn, Rivne, Zhytomyr.", " ".join(ukraine["sentences"]))   # lists joined on
        self.assertTrue(sudan["history"] and sudan["updated"])

    def test_text_scope_rules(self):
        t = ("Areas where FCDO advises against travel. FCDO advises against all travel to within 30km of Chad's borders. "
             "FCDO advises against all travel to the Kenya border areas. FCDO advises against all travel to the Djibouti-Eritrea border.")
        for name in ("Chad", "Kenya", "Djibouti"):
            self.assertFalse(any(advice.text_scope(t, name).values()), name)
        self.assertTrue(advice.text_scope("FCDO advises against all travel to the whole of the Central African Republic, "
                                          "except to the capital, Bangui.", "Central African Republic")["aat_whole"])
        sc = advice.text_scope("The FCDO advises against all but essential travel to the remaining areas of Venezuela.", "Venezuela")
        self.assertTrue(sc["remainder_aabe"])
        self.assertTrue(advice.text_scope("FCDO advises against all travel to Myanmar.", "Myanmar (Burma)")["aat_whole"])
        self.assertTrue(advice.text_scope("FCDO advises against all travel to Somalia, including Somaliland, except for the "
                                          "regions of Awdal.", "Somalia")["except"])

    def test_level_rules(self):
        whole = {"aat_whole": True}
        self.assertEqual(advice.fcdo_level([], whole), (1, "codes"))         # text never adds advice the codes lack
        self.assertEqual(advice.fcdo_level([advice.AAT_WHOLE], {}), (4, "codes"))
        self.assertEqual(advice.fcdo_level([advice.AABE_WHOLE], {"aat_whole": False}), (3, "codes"))
        self.assertEqual(advice.fcdo_level([advice.AABE_PARTS], {"remainder_aabe": True}), (3, "text"))
        self.assertEqual(advice.fcdo_level([advice.AAT_WHOLE], {"remainder_aabe": True}), (4, "codes"))   # never lowered
        self.assertEqual(advice.fcdo_level(["something_new"], whole), (1, "codes"))
        self.assertEqual(advice.fcdo_label(2, [advice.AAT_PARTS, advice.AABE_PARTS]),
                         "Against all travel and all but essential travel to parts")
        self.assertEqual(advice.fcdo_label(4, [advice.AAT_WHOLE]), "Against all travel")

    def test_us(self):
        items = advice.parse_us(fixture("us_advisories.json"), min_items=1)
        by = {i["place"]: i for i in items if i["place"] != "Tonga"}
        self.assertEqual((by["Yemen"]["level"], by["Yemen"]["iso3"], by["Yemen"]["fips"]), (4, "YEM", ["YM"]))
        self.assertEqual((by["Bahrain"]["fips"], by["Bahrain"]["iso3"]), (["BA"], "BHR"))     # FIPS, not ISO
        self.assertEqual((by["Mexico"]["level"], by["Mexico"]["words"]), (2, "Exercise increased caution"))
        self.assertIsNone(by["French West Indies"]["iso3"])
        self.assertTrue(all(i["updated"] for i in items))
        self.assertNotIn("<", by["Iran"]["summary"])
        self.assertEqual(sum(1 for i in items if i["place"] == "Tonga"), 2)    # duplicates are resolved by the poller
        with self.assertRaises(ValueError):
            advice.parse_us({"not": "a list"})

    def test_slug_table(self):
        self.assertEqual(advice.FCDO_SLUG_ISO2["sudan"], ("SD",))
        self.assertEqual(advice.FCDO_SLUG_ISO2["cook-islands-tokelau-and-niue"], ("CK", "TK", "NU"))
        self.assertEqual(len(advice.FCDO_SLUG_ISO2), 226)
        self.assertTrue(all(len(c) == 2 and c.isupper() for v in advice.FCDO_SLUG_ISO2.values() for c in v))

    def test_time_and_html(self):
        self.assertEqual(advice.parse_time("2026-10-22T20:00:00-04:00"), advice.parse_time("2026-10-23T00:00:00Z"))
        self.assertEqual(advice.parse_time("2026-11-23T15:27:48.689"), advice.parse_time("2026-11-23T15:27:48Z") + 0.0)
        self.assertIsNone(advice.parse_time("yesterday"))
        self.assertEqual(advice.html_text("<p>Do not travel<b> to Iran</b>&nbsp;now.</p>"), "Do not travel to Iran now")


class TestAdviceChanges(unittest.TestCase):
    def test_detect(self):
        old = {"level": 2, "statuses": [advice.AAT_PARTS], "updated": 100.0, "change": "Updated information."}
        self.assertIsNone(advice.detect_fcdo_change(None, old))                            # first sight is not a change
        same = dict(old, updated=200.0, change="Updated information on airports.")
        self.assertIsNone(advice.detect_fcdo_change(old, same))                            # routine update
        areas = dict(old, updated=200.0, change="FCDO now advises against all travel to the northern border.")
        self.assertEqual(advice.detect_fcdo_change(old, areas), "areas")
        self.assertIsNone(advice.detect_fcdo_change(areas, dict(areas)))                    # same update seen again
        raised = dict(old, level=4, statuses=[advice.AAT_WHOLE], updated=300.0)
        self.assertEqual(advice.detect_fcdo_change(old, raised), "level")
        recoded = dict(old, statuses=[advice.AAT_PARTS, advice.AABE_PARTS])               # same level, new codes
        self.assertEqual(advice.detect_fcdo_change(old, recoded), "level")

    def test_wording(self):
        self.assertEqual(advice.fcdo_change_text("Sudan", {"level": 2, "statuses": [advice.AAT_PARTS]},
                                                 {"level": 4, "statuses": [advice.AAT_PARTS], "scope": {"aat_whole": True}}),
                         "FCDO now advises against all travel to most of Sudan (previously against all travel to parts)")
        self.assertEqual(advice.fcdo_change_text("Niger", {"level": 3, "statuses": [advice.AABE_WHOLE]},
                                                 {"level": 4, "statuses": [advice.AAT_WHOLE]}),
                         "FCDO now advises against all travel to Niger (previously against all but essential travel)")
        self.assertEqual(advice.fcdo_change_text("Peru", {"level": 2, "statuses": [advice.AABE_PARTS]}, {"level": 1, "statuses": []}),
                         "FCDO no longer advises against travel to Peru (previously against all but essential travel to parts)")
        self.assertEqual(advice.us_change_text("Venezuela", 3, 4),
                         "US State Department raises Venezuela to Level 4: do not travel (previously Level 3)")
        self.assertEqual(advice.us_change_text("Cuba", 3, 2),
                         "US State Department lowers Cuba to Level 2: exercise increased caution (previously Level 3)")

    def test_areas_text(self):
        note = ("FCDO now advises against all but essential travel to East Borena and Arsi in Oromia Region. FCDO continues "
                "to advise against all travel to other parts of Ethiopia ('Warnings and insurance' page).")
        self.assertEqual(advice.areas_change_text("Ethiopia", note),
                         "Ethiopia: FCDO now advises against all but essential travel to East Borena and Arsi in Oromia Region.")
        self.assertEqual(advice.areas_change_text("Kenya", ""), "Kenya: FCDO changed the areas it advises against")
        self.assertLessEqual(len(advice.first_sentence("x" * 500)), 180)

    def test_notes_and_severity(self):
        self.assertTrue(advice.is_advice_note("FCDO now advises against all but essential travel to East Borena."))
        self.assertTrue(advice.is_advice_note("FCDO no longer advises against all travel to the city."))
        self.assertFalse(advice.is_advice_note("FCDO continues to advise against all travel to the border."))
        self.assertFalse(advice.is_advice_note("Updated information about demonstrations."))
        self.assertEqual(advice.change_severity(4, 3, "level"), 6.0)
        self.assertEqual(advice.change_severity(4, 2, "level"), 6.5)                      # a jump of two levels
        self.assertEqual(advice.change_severity(2, 4, "level"), 3.0)                      # lowered
        self.assertEqual(advice.change_severity(3, None, "areas"), 3.5)
        self.assertEqual(advice.change_severity(2, None, "areas", "FCDO now advises against all travel to X."), 4.0)
        self.assertEqual(advice.change_severity(2, None, "areas", "FCDO no longer advises against travel to X."), 3.0)
        self.assertLessEqual(advice.change_severity(4, 1, "level"), 7.0)


# ---------------------------------------------------------------------------------------------
class TestProtests(unittest.TestCase):
    def test_rows(self):
        demo = protests.protest_row(row())
        self.assertIsNotNone(demo)
        self.assertEqual((demo["root"], demo["quad"], demo["category"], demo["iso2"]), ("14", 3, "demonstration", "KE"))
        self.assertTrue(demo["id"].startswith("gp") and len(demo["id"]) == 16)
        self.assertEqual(protests.title(demo), "Demonstration in Nairobi")
        self.assertIsNone(protests.protest_row(row(root="19", code="190", quad="4")))      # conflict, not protest
        self.assertIsNone(protests.protest_row(row(gtype="1")))                            # country-level geocode
        self.assertEqual(protests.protest_row(row(gtype="5"))["precision"], "province")
        self.assertIsNone(protests.protest_row(row(day="20261115")))                       # retrospective report
        self.assertEqual(protests.protest_row(row(url="ftp://x"))["url"], "")
        riot = protests.protest_row(row(code="1452", mentions="80", sources="9"))
        self.assertEqual((riot["category"], protests.label("1452")), ("riot", "Riot or violent protest for policy change"))
        self.assertGreater(riot["severity"], demo["severity"])
        self.assertEqual(protests.protest_row(row(code="1431"))["category"], "strike")
        self.assertEqual(protests.label("999"), "Protest")
        self.assertEqual(demo["dkey"], protests.protest_row(row(id="2002", glat="-1.29"))["dkey"])  # same place and day

    def test_severity_bounds(self):
        top = protests.severity("145", -10.0, 100000, 1000, -50.0, 500)
        self.assertLessEqual(top, 10.0)
        self.assertGreaterEqual(protests.severity("140", 0.0, 0, 0, 5.0), 0.0)
        merged = {"code": "141", "goldstein": -6.5, "mentions": 30, "sources": 6, "tone": -3.0, "reports": 4}
        self.assertGreater(protests.severity_of(merged), protests.severity("141", -6.5, 8, 2, -3.0))


# ---------------------------------------------------------------------------------------------
class TestSpikeStatistics(unittest.TestCase):
    def test_robust_z(self):
        base = [100] * 10 + [110] * 10 + [90] * 8
        self.assertAlmostEqual(spikes.robust_z(100, base), 0.0)
        self.assertGreater(spikes.robust_z(300, base), 3)
        self.assertAlmostEqual(spikes.robust_z(20, [10] * 20), (math.log1p(20) - math.log1p(10)) / 0.1)   # MAD 0: the floor

    def test_poisson(self):
        self.assertAlmostEqual(spikes.poisson_sf(8, 2.0), 0.0010967189678585676, places=12)
        self.assertAlmostEqual(spikes.poisson_sf(3, 2.0), 0.3233235838169365, places=12)
        self.assertEqual(spikes.poisson_sf(0, 1.0), 1.0)
        self.assertEqual(spikes.poisson_sf(5, 0.0), 0.0)
        self.assertTrue(0 < spikes.poisson_sf(40, 3.0) < 1e-25)            # tail summed directly, no underflow to 0

    def test_assess(self):
        steady = [200.0 + (i % 5) * 4 for i in range(20)]
        self.assertIsNone(spikes.assess(206, steady, "volume")["level"])
        real = spikes.assess(700, steady, "volume")
        self.assertEqual(real["level"], "unusual")
        self.assertEqual(real["method"], "robust z")
        self.assertGreater(real["z"], 3)
        self.assertGreaterEqual(real["ratio"], 2)
        self.assertEqual(spikes.assess(330, steady, "volume")["level"], "notable")          # 1.6 times, z 2+
        short = spikes.assess(700, steady[:10], "volume")                                   # 10 days: still building
        self.assertIsNone(short["level"])
        self.assertEqual(short["n"], 10)
        small = [1.0, 2.0, 0.0, 1.0, 3.0, 1.0, 2.0, 1.0, 0.0, 2.0, 1.0, 1.0, 2.0, 1.0]      # median under 5: Poisson
        p = spikes.assess(12, small, "conflict")
        self.assertEqual((p["method"], p["level"]), ("Poisson", "unusual"))
        self.assertLess(p["p"], 0.001)
        self.assertNotEqual(spikes.assess(7, small, "conflict")["level"], "unusual")       # below the 10-report minimum
        self.assertIsNone(spikes.assess(3, small, "protest")["level"])

    def test_weekly_cycle_is_normalised(self):
        # A country with a steady share of a volume that drops 20 % on Sundays: no spike on a quiet Sunday.
        days = [(40.0 * (0.8 if d % 7 == 6 else 1.0), 10000.0 * (0.8 if d % 7 == 6 else 1.0)) for d in range(21)]
        base = spikes.scaled_baseline(days, 8000.0)
        self.assertTrue(all(abs(b - 32.0) < 1e-9 for b in base))
        self.assertIsNone(spikes.assess(33, base, "volume")["level"])

    def test_severity_and_words(self):
        self.assertEqual(spikes.severity(2.0), 4.0)
        self.assertEqual(spikes.severity(4.0), 5.0)
        self.assertEqual(spikes.severity(8.0), 6.0)
        self.assertEqual(spikes.severity(50.0), 6.0)
        self.assertEqual(spikes.headline("volume", "Sudan"), "Unusually high news volume about Sudan")
        text = spikes.explanation("conflict", "Sudan", {"ratio": 3.44, "value": 412, "median": 120.0}, ["El Fasher", "Khartoum"], 28)
        self.assertTrue(text.startswith("Conflict reports about Sudan are 3.4 times the usual level: 412 machine-coded "
                                        "conflict reports in the last 24 hours against a usual 120 (median of 28 days"))
        self.assertIn("Most come from El Fasher and Khartoum.", text)
        for metric in spikes.METRICS:
            self.assertNotIn("crisis", spikes.headline(metric, "X").lower())


# ---------------------------------------------------------------------------------------------
class TestConvergenceKinds(unittest.TestCase):
    NOW = 1_795_400_000.0

    def ev(self, kind, lat=10.2, lon=20.2, **kw):
        return dict({"kind": kind, "lat": lat, "lon": lon, "time": self.NOW - 600, "severity": 5}, **kw)

    def test_space_weather_and_country_level_kinds_excluded(self):
        for kind in ("space_weather", "news_spike", "advice_change"):
            self.assertIn(kind, convergence.EXCLUDED_KINDS)
            evs = [self.ev("earthquake"), self.ev("fire"), self.ev(kind)]
            self.assertEqual(convergence.detect(evs, self.NOW), [], kind)
        evs = [self.ev("earthquake"), self.ev("fire"), self.ev("storm")]
        self.assertEqual(len(convergence.detect(evs, self.NOW)), 1)

    def test_protests_and_conflict_count_once(self):
        evs = [self.ev("conflict_event"), self.ev("protest"), self.ev("fire")]
        self.assertEqual(convergence.detect(evs, self.NOW), [])
        found = convergence.detect(evs + [self.ev("outage")], self.NOW)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["n_kinds"], 3)
        self.assertIn("conflict and protest reports", found[0]["title"])
        self.assertEqual({k["kind"] for k in found[0]["kinds"]}, {"conflict_event", "protest", "fire", "outage"})


if __name__ == "__main__":
    unittest.main()


class ConvergenceExclusionsLeadTests(unittest.TestCase):
    """Air activity and chronic GPS cells never make convergences (lead, 23 Nov)."""

    def test_air_activity_and_chronic_gps_are_ignored(self):
        from meridian.modules.intel import convergence as cv
        now = 1_800_000_000.0
        base = {"lat": 59.5, "lon": 24.5, "time": now - 600, "severity": 5}
        events = [dict(base, kind="earthquake", id="a"), dict(base, kind="air_activity", id="b"),
                  dict(base, kind="gps_jam", id="c", category="chronic_interference")]
        self.assertEqual(cv.detect(events, now), [])
        events.append(dict(base, kind="gps_jam", id="d", category="new_interference"))
        events.append(dict(base, kind="fire", id="e"))
        self.assertEqual(len(cv.detect(events, now)), 1)
