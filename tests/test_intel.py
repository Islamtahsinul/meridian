"""Unit tests for the intel module's pure functions (no network). Run: python -m unittest tests.test_intel -v"""

from __future__ import annotations

import io
import json
import math
import unittest
import zipfile
from pathlib import Path

from meridian.modules.intel import (codes, convergence, countries, cyber, frontlines, gdelt, geomath, news, risk,
                                    ucdp)

FIX = Path(__file__).parent / "fixtures" / "intel"
NOW = gdelt.ts_to_epoch("20261122121500")


def row(**kw) -> list[str]:
    """A synthetic 61-column GDELT export row."""
    d = dict(id="1001", day="20261122", a1code="RUSMIL", a1name="RUSSIAN", a1type="MIL", a2code="UKR", a2name="UKRAINE",
             a2type="", root="19", code="194", quad="4", goldstein="-10.0", mentions="10", sources="2", articles="10",
             tone="-6.0", gtype="4", gname="Pokrovsk, Donets'k, Ukraine", gcc="UP", glat="48.28", glon="37.18",
             feature="-1032000", added="20261122121500",
             url="https://example.org/2026/11/22/russian-shelling-hits-pokrovsk-market-killing-three")
    d.update(kw)
    r = [""] * 61
    r[gdelt.C_ID], r[gdelt.C_DAY] = d["id"], d["day"]
    r[gdelt.C_A1_CODE], r[gdelt.C_A1_NAME], r[gdelt.C_A1_TYPES[0]] = d["a1code"], d["a1name"], d["a1type"]
    r[gdelt.C_A2_CODE], r[gdelt.C_A2_NAME], r[gdelt.C_A2_TYPES[0]] = d["a2code"], d["a2name"], d["a2type"]
    r[gdelt.C_A1_COUNTRY] = d["a1code"][:3] if d["a1code"][:3] in ("RUS", "UKR", "USA", "ISR", "PSE") else ""
    r[gdelt.C_A2_COUNTRY] = d["a2code"][:3] if d["a2code"][:3] in ("RUS", "UKR", "USA", "ISR", "PSE") else ""
    r[gdelt.C_ROOT], r[gdelt.C_CODE], r[gdelt.C_QUAD], r[gdelt.C_GOLDSTEIN] = d["root"], d["code"], d["quad"], d["goldstein"]
    r[gdelt.C_MENTIONS], r[gdelt.C_SOURCES], r[gdelt.C_ARTICLES], r[gdelt.C_TONE] = d["mentions"], d["sources"], d["articles"], d["tone"]
    r[gdelt.C_GEO_TYPE], r[gdelt.C_GEO_NAME], r[gdelt.C_GEO_COUNTRY] = d["gtype"], d["gname"], d["gcc"]
    r[gdelt.C_GEO_LAT], r[gdelt.C_GEO_LON], r[gdelt.C_GEO_FEATURE] = d["glat"], d["glon"], d["feature"]
    r[gdelt.C_ADDED], r[gdelt.C_URL] = d["added"], d["url"]
    return r


def fixture_rows() -> list[list[str]]:
    return list(gdelt.iter_rows((FIX / "gdelt_sample.export.CSV").read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------------------------
class TestCodes(unittest.TestCase):
    def test_fips_to_iso(self):
        cases = {"UP": "UA", "RS": "RU", "IS": "IL", "GZ": "PS", "WE": "PS", "OD": "SS", "CG": "CD", "CF": "CG",
                 "KV": "XK", "UK": "GB", "GM": "DE", "NI": "NG", "NG": "NE", "SU": "SD", "BM": "MM", "TW": "TW", "OS": None}
        for fips, iso in cases.items():
            self.assertEqual(codes.fips_to_iso2(fips), iso, fips)
        self.assertIsNone(codes.fips_to_iso2(""))
        self.assertIsNone(codes.fips_to_iso2("ZZ"))

    def test_cameo_labels(self):
        self.assertEqual(codes.event_label("1952"), "Drone strike")
        self.assertEqual(codes.event_label("195"), "Air strike")
        self.assertEqual(codes.event_label("1823"), "Killing by assault")
        self.assertEqual(codes.event_label("1999", "19"), "Fighting")       # unknown detailed code -> root words
        self.assertEqual(codes.event_label("", "14"), "Protest")
        self.assertEqual(codes.event_label(None, None), "Reported event")
        self.assertGreater(codes.code_weight("1952"), codes.code_weight("190"))

    def test_actor_label_and_flag(self):
        self.assertEqual(codes.actor_label("TERRORIST", "UAF"), "Terrorist (armed group)")
        self.assertEqual(codes.actor_label("PAKISTAN", None), "Pakistan")
        self.assertEqual(codes.actor_label("", "MIL"), "Military")
        self.assertIsNone(codes.actor_label("", ""))
        self.assertEqual(codes.flag_emoji("UA"), "\U0001F1FA\U0001F1E6")
        self.assertEqual(codes.flag_emoji("u1"), "")


# ---------------------------------------------------------------------------------------------
class TestGdeltFiles(unittest.TestCase):
    def test_lastupdate(self):
        text = ("37061 311480f1 http://data.gdeltproject.org/gdeltv2/20261122211500.export.CSV.zip\n"
                "65322 209f4876 http://data.gdeltproject.org/gdeltv2/20261122211500.mentions.CSV.zip\n")
        self.assertEqual(gdelt.parse_lastupdate(text), "20261122211500")
        self.assertIsNone(gdelt.parse_lastupdate("nothing here"))
        self.assertTrue(gdelt.export_url("20261122211500").startswith("https://data.gdeltproject.org/gdeltv2/20261122211500"))

    def test_slots(self):
        slots = gdelt.slots_back("20261122211500", 1)
        self.assertEqual(slots, ["20261122211500", "20261122210000", "20261122204500", "20261122203000", "20261122201500"])
        self.assertEqual(len(gdelt.slots_back("20261122211500", 48)), 193)
        self.assertEqual(gdelt.epoch_to_ts(gdelt.ts_to_epoch("20261122211500")), "20261122211500")

    def test_read_zip_guards(self):
        good = io.BytesIO()
        with zipfile.ZipFile(good, "w") as zf:
            zf.writestr("20261122211500.export.CSV", "a\tb\n")
        self.assertEqual(gdelt.read_export_zip(good.getvalue()), "a\tb\n")
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("one.CSV", "x")
            zf.writestr("two.CSV", "y")
        with self.assertRaises(ValueError):
            gdelt.read_export_zip(bad.getvalue())
        with self.assertRaises(Exception):
            gdelt.read_export_zip(b"not a zip")

    def test_clean_place(self):
        self.assertEqual(gdelt.clean_place("Gaza, Israel (general), Israel"), "Gaza, Israel")
        self.assertEqual(gdelt.clean_place("Warsaw, (PL67), Poland"), "Warsaw, Poland")
        self.assertEqual(gdelt.short_place("Kyiv, Kyyiv, Misto, Ukraine"), "Kyiv")


class TestGdeltRows(unittest.TestCase):
    def test_fixture_filters(self):
        rows = fixture_rows()
        self.assertEqual(len(rows), 10)
        kept = [gdelt.conflict_row(r) for r in rows]
        kept = [k for k in kept if k]
        self.assertEqual(len(kept), 5)                                   # country-level and root 17 rows dropped
        self.assertEqual({k["precision"] for k in kept}, {"city", "province"})
        warrap = next(k for k in kept if k["precision"] == "province")
        self.assertEqual(warrap["iso2"], "SS")                           # FIPS OD -> ISO SS
        mumbai = [k for k in kept if k["place"].startswith("Mumbai")]
        self.assertEqual(len({k["dkey"] for k in mumbai}), 1)            # same root, place and day -> one key
        for k in kept:
            self.assertTrue(0 <= k["severity"] <= 10)
            self.assertTrue(k["id"].startswith("gd") and len(k["id"]) == 16)

    def test_synthetic_filters(self):
        self.assertIsNotNone(gdelt.conflict_row(row()))
        self.assertIsNone(gdelt.conflict_row(row(quad="3")))
        self.assertIsNone(gdelt.conflict_row(row(root="17", code="173")))
        self.assertIsNone(gdelt.conflict_row(row(gtype="1")))            # country centroid
        self.assertIsNone(gdelt.conflict_row(row(gtype="2")))            # US state centroid
        self.assertIsNone(gdelt.conflict_row(row(glat="", glon="")))
        self.assertIsNone(gdelt.conflict_row(row(glat="0", glon="0")))
        self.assertIsNone(gdelt.conflict_row(row(day="20261001")))       # retrospective mention, lag > 2 days
        self.assertIsNotNone(gdelt.conflict_row(row(day="20261121")))    # one day of lag is fine
        self.assertIsNone(gdelt.conflict_row(row(added="garbage")))
        ev = gdelt.conflict_row(row(url="javascript:alert(1)"))
        self.assertEqual(ev["url"], "")

    def test_classification(self):
        armed = gdelt.conflict_row(row())
        self.assertEqual(armed["category"], "fighting")
        self.assertTrue(armed["armed"])
        self.assertFalse(armed["crime"])
        crime = gdelt.conflict_row(row(root="18", code="182", a1code="COP", a1name="POLICE", a1type="COP", a2code="CRM",
                                       a2name="SUSPECT", a2type="CRM"))
        self.assertEqual(crime["category"], "assault")
        self.assertTrue(crime["crime"])
        self.assertFalse(crime["armed"])
        bomb = gdelt.conflict_row(row(root="18", code="1831", a1code="COP", a1type="COP", a2code="", a2type=""))
        self.assertEqual(bomb["category"], "attack")
        self.assertTrue(bomb["armed"])                                   # bombings are heavy-violence codes
        self.assertFalse(bomb["crime"])
        state = gdelt.conflict_row(row(code="190", a1code="ISR", a1name="ISRAEL", a1type="", a2code="PSECVL", a2type="CVL"))
        self.assertTrue(state["armed"])                                  # military force with a state actor

    def test_gaza_refinement(self):
        ev = gdelt.conflict_row(row(gcc="IS", gname="Gaza, Israel (general), Israel", glat="31.5", glon="34.45"),
                                iso_of=lambda lat, lon, fips, iso2: "PS")
        self.assertEqual(ev["iso2"], "PS")
        self.assertEqual(ev["place"], "Gaza, Israel")


class TestSeverity(unittest.TestCase):
    def sev(self, **kw):
        base = dict(goldstein=-10.0, root="19", code="190", mentions=10, sources=2, tone=-5.0, armed=True)
        base.update(kw)
        return gdelt.conflict_severity(**base)

    def test_bounds_and_monotonic(self):
        for kw in ({}, {"mentions": 0, "sources": 0, "tone": 0, "armed": False}, {"mentions": 10 ** 6, "sources": 10 ** 6,
                                                                                    "code": "2042", "root": "20", "reports": 999}):
            s = self.sev(**kw)
            self.assertTrue(0 <= s <= 10, s)
        self.assertLess(self.sev(mentions=1), self.sev(mentions=50))
        self.assertLess(self.sev(sources=1), self.sev(sources=20))
        self.assertLess(self.sev(code="190"), self.sev(code="195"))      # heavy weapons
        self.assertLess(self.sev(root="18", code="182"), self.sev(root="19", code="193"))
        self.assertLess(self.sev(armed=False), self.sev(armed=True))

    def test_focus_damps_datelines(self):
        self.assertGreater(self.sev(focus=1.0), self.sev(focus=0.3))
        self.assertEqual(self.sev(focus=0.0), self.sev(focus=0.25))     # focus floor
        rows = [row(id=str(i), quad="1", root="04", code="040") for i in range(8)] + [row(id="99")]
        focus = gdelt.focus_by_location(rows)
        key = gdelt.location_key(rows[0])
        self.assertAlmostEqual(focus[key], min(1.0, (1 / 9) / 0.30), places=6)


class TestDedupe(unittest.TestCase):
    def test_merge(self):
        a = gdelt.conflict_row(row(id="1", code="193", mentions="5", sources="1", tone="-4"))
        b = gdelt.conflict_row(row(id="2", code="195", mentions="15", sources="3", tone="-8",
                                   url="https://example.org/air-strike-on-pokrovsk-reported-by-officials"))
        self.assertEqual(a["dkey"], b["dkey"])
        ev = gdelt.new_event(a)
        self.assertTrue(gdelt.merge_event(ev, b))
        self.assertFalse(gdelt.merge_event(ev, b))                        # same GLOBALEVENTID counted once
        self.assertEqual(ev["mentions"], 20)
        self.assertEqual(ev["sources"], 4)
        self.assertEqual(ev["reports"], 2)
        self.assertEqual(ev["code"], "195")                               # the more severe, more specific code wins
        self.assertEqual(len(ev["urls"]), 2)
        self.assertAlmostEqual(ev["tone"], (-4 * 5 + -8 * 15) / 20, places=3)
        self.assertGreaterEqual(ev["severity"], a["severity"])

    def test_keys(self):
        a = gdelt.conflict_row(row())
        self.assertNotEqual(a["dkey"], gdelt.conflict_row(row(day="20261121"))["dkey"])          # another day
        self.assertNotEqual(a["dkey"], gdelt.conflict_row(row(root="18", code="182"))["dkey"])   # another root
        self.assertEqual(a["dkey"], gdelt.conflict_row(row(glat="48.30", glon="37.21"))["dkey"])  # same 0.1 degree place
        self.assertNotEqual(a["dkey"], gdelt.conflict_row(row(glat="48.5", glon="37.18"))["dkey"])

    def test_crime_flag_needs_all_reports(self):
        crime = gdelt.conflict_row(row(id="1", root="18", code="182", a1code="COP", a1type="COP", a2code="", a2type=""))
        other = gdelt.conflict_row(row(id="2", root="18", code="182", a1code="REB", a1type="REB", a2code="", a2type=""))
        ev = gdelt.new_event(crime)
        self.assertTrue(ev["crime"])
        gdelt.merge_event(ev, other)
        self.assertFalse(ev["crime"])
        self.assertTrue(ev["armed"])


class TestNews(unittest.TestCase):
    def test_slug_title(self):
        t = gdelt.slug_title("https://www.cp24.com/local/toronto/2026/11/22/three-16-year-olds-shot-two-killed-in-scarborough-shooting/",
                             {"scarborough"})
        self.assertEqual(t, "Three 16 year olds shot two killed in Scarborough shooting")
        self.assertIsNone(gdelt.slug_title("https://www.ynetnews.com/article/sjzfnxejqe"))
        self.assertIsNone(gdelt.slug_title("https://www.israelnationalnews.com/flashes/698160"))
        t = gdelt.slug_title("https://www.example.com/cities/test-news/sample-gang-member-held-101795332419482.html")
        self.assertEqual(t, "Sample gang member held")
        self.assertEqual(gdelt.slug_title("https://x.org/us-and-uk-talk-nato-plans"), "US and UK talk NATO plans")
        self.assertEqual(gdelt.domain_of("https://www.bbc.co.uk/news/x"), "bbc.co.uk")

    def test_news_rows_and_severity(self):
        items = [n for n in (gdelt.news_row(r) for r in fixture_rows()) if n]
        self.assertEqual(len(items), 8)
        self.assertEqual(len({n["lkey"] for n in items}), 6)              # two pairs share a place
        calm = gdelt.news_severity(1, 2.0, 1.0, 2, "Anchor tap bottle reviews")
        alarm = gdelt.news_severity(4, -10.0, -8.0, 40, "Missile strike kills five as shelling continues")
        self.assertLess(calm, 2.5)
        self.assertGreater(alarm, 8.0)
        self.assertTrue(0 <= alarm <= 10)
        self.assertGreater(gdelt.doc_severity("Drone attack kills two", 3), gdelt.doc_severity("Ministers meet in Paris"))

    def test_doc_parse(self):
        items = news.parse_doc(json.loads((FIX / "doc_sample.json").read_text(encoding="utf-8")))
        self.assertEqual(len(items), 2)                                   # near-identical headlines merged, bad link dropped
        top = items[0]
        self.assertEqual(top["outlets"], 2)
        self.assertEqual(top["also"][0]["domain"], "example.net")
        self.assertEqual(top["t"], gdelt.ts_to_epoch("20261122201500"))
        self.assertTrue(top["id"].startswith("dn"))
        self.assertEqual(news.parse_doc({}), [])
        with self.assertRaises(ValueError):
            news.parse_doc({"articles": "nope"})

    def test_theatres_and_gazetteer(self):
        ua = news.THEATRE_BY_ID["ukraine-russia"]
        self.assertTrue(news.in_theatre(ua, 48.0, 37.8, "UA"))
        self.assertTrue(news.in_theatre(ua, 50.6, 36.6, "RU"))             # Belgorod box
        self.assertFalse(news.in_theatre(ua, 55.75, 37.6, "RU"))           # Moscow is outside
        scs = news.THEATRE_BY_ID["taiwan-scs"]
        self.assertTrue(news.in_theatre(scs, 10.0, 114.0, None))           # at sea
        self.assertFalse(news.in_theatre(scs, 14.6, 120.98, "PH"))         # Manila news is not the theatre
        places = [{"place": "Kharkiv, Kharkivs'ka Oblast', Ukraine", "lat": 49.98, "lon": 36.25, "iso2": "UA", "n": 9},
                  {"place": "Pokrovsk, Donets'k, Ukraine", "lat": 48.28, "lon": 37.18, "iso2": "UA", "n": 4},
                  {"place": "Paris, France", "lat": 48.85, "lon": 2.35, "iso2": "FR", "n": 50}]
        gaz = news.build_gazetteer(places, ua)
        self.assertIn("kharkiv", gaz)
        self.assertNotIn("paris", gaz)
        hit = news.locate_title("Russian missile strike kills five in Kharkiv", gaz)
        self.assertEqual(hit[0], "Kharkiv")
        self.assertIsNone(news.locate_title("Talks resume", gaz))


# ---------------------------------------------------------------------------------------------
def ev(kind, lat, lon, t=NOW - 3600, sev=5.0, i=0):
    return {"kind": kind, "id": f"{kind}:{i}", "title": f"{kind} {i}", "lat": lat, "lon": lon, "time": t, "severity": sev,
            "module": "test"}


class TestConvergence(unittest.TestCase):
    def test_three_kinds_converge(self):
        events = [ev("earthquake", 38.10, 30.10), ev("fire", 38.30, 30.40, i=1), ev("conflict_event", 38.40, 30.20, i=2)]
        found = convergence.detect(events, NOW)
        self.assertEqual(len(found), 1)
        c = found[0]
        self.assertEqual(c["n_kinds"], 3)
        self.assertEqual(c["kind"], "convergence")
        self.assertTrue(c["id"].startswith("cv:"))
        self.assertTrue(0 <= c["severity"] <= 10)
        self.assertIn("earthquake", c["title"])

    def test_two_kinds_do_not(self):
        events = [ev("earthquake", 38.1, 30.1), ev("earthquake", 38.2, 30.2, i=1), ev("fire", 38.3, 30.3, i=2)]
        self.assertEqual(convergence.detect(events, NOW), [])

    def test_excluded_and_old(self):
        events = [ev("earthquake", 38.1, 30.1), ev("fire", 38.2, 30.2, i=1), ev("news", 38.3, 30.3, i=2),
                  ev("conflict_event", 38.3, 30.3, t=NOW - 30 * 3600, i=3), ev("convergence", 38.3, 30.3, i=4)]
        self.assertEqual(convergence.detect(events, NOW), [])

    def test_cell_edges_and_separation(self):
        straddle = [ev("earthquake", 38.49, 30.49), ev("fire", 38.51, 30.51, i=1), ev("gps_jam", 38.52, 30.48, i=2)]
        self.assertEqual(len(convergence.detect(straddle, NOW)), 1)       # 1-degree windows on a 0.5 stride
        far = straddle + [ev("storm", 10.1, 100.1, i=3), ev("flood", 10.2, 100.2, i=4), ev("outage", 10.3, 100.3, i=5)]
        found = convergence.detect(far, NOW)
        self.assertEqual(len(found), 2)
        ids = {c["id"] for c in found}
        self.assertEqual(len(ids), 2)

    def test_invalid_inputs_ignored(self):
        events = [ev("earthquake", 38.1, 30.1), ev("fire", 38.2, 30.2, i=1), {"kind": "aircraft", "lat": "x", "lon": 1, "time": NOW},
                  {"kind": "ship", "lat": 38.2, "lon": 30.2}, ev("ship", 95.0, 30.2, i=9)]
        self.assertEqual(convergence.detect(events, NOW), [])


class TestRisk(unittest.TestCase):
    def cluster(self, n, lat, lon, sev=7.0, prefix="a"):
        return [{"id": f"{prefix}{i}", "lat": lat + 0.01 * i, "lon": lon + 0.01 * i, "severity": sev, "t_last": NOW - 3600,
                 "precision": "city", "crime": False, "place": "Pokrovsk, Donets'k, Ukraine", "iso2": "UA",
                 "category": "fighting"} for i in range(n)]

    def test_zone_from_cluster(self):
        zones = risk.compute_zones(self.cluster(8, 48.1, 37.1) + self.cluster(1, 10.0, 10.0, prefix="b"), NOW)
        self.assertEqual(len(zones), 1)
        z = zones[0]
        self.assertEqual(z["events"], 8)
        self.assertEqual(z["short"], "Pokrovsk")
        self.assertEqual(z["iso2"], "UA")
        self.assertTrue(z["id"].startswith("rz:"))
        ring = z["outline"][0]
        self.assertEqual(ring[0], ring[-1])
        self.assertEqual(len(ring), 5)                                    # one cell -> a closed square
        fc = risk.zones_geojson(zones)
        roles = sorted(f["properties"]["role"] for f in fc["features"])
        self.assertEqual(roles, ["cell", "label", "outline"])

    def test_filters_and_share_weight(self):
        events = self.cluster(8, 48.1, 37.1)
        crime = [dict(e, crime=True) for e in events]
        self.assertEqual(risk.compute_zones(crime, NOW), [])
        old = [dict(e, t_last=NOW - 9 * 86400) for e in events]
        self.assertEqual(risk.compute_zones(old, NOW), [])
        key = (int(math.floor(48.1 / 0.5)), int(math.floor(37.1 / 0.5)))
        busy = risk.compute_zones(events, NOW, shares={key: (5000, 50)})  # 1 % of local news is violence: damped away
        self.assertEqual(busy, [])
        hot = risk.compute_zones(events, NOW, shares={key: (100, 60)})
        self.assertEqual(len(hot), 1)
        self.assertAlmostEqual(hot[0]["conflict_share"], 0.6)
        self.assertEqual(risk.share_weight(10, 0), 1.0)                     # too few reports to judge
        self.assertEqual(risk.level_of(3), "Elevated")
        self.assertEqual(risk.level_of(30), "Very high")


# ---------------------------------------------------------------------------------------------
class TestGeomath(unittest.TestCase):
    def test_simplify(self):
        line = [[0, 0], [1, 0.0001], [2, 0], [3, 5], [4, 0]]
        out = geomath.simplify(line, 0.01)
        self.assertEqual(out[0], [0, 0])
        self.assertEqual(out[-1], [4, 0])
        self.assertNotIn([1, 0.0001], out)
        self.assertIn([3, 5], out)

    def test_cell_outline(self):
        rings = geomath.cell_outline({(0, 0), (0, 1), (1, 0)}, 0.5)        # L shape
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0][0], rings[0][-1])
        self.assertEqual(len(rings[0]), 7)                                 # 6 corners + closing point
        self.assertEqual(len(geomath.components({(0, 0), (1, 1), (5, 5)})), 2)

    def test_grid_fill_and_erode(self):
        g = geomath.Grid(0, 0, 10, 10, 0.1)
        g.fill_polygon([[[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]], [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]])
        self.assertEqual(g.get(3, 3), 1)
        self.assertEqual(g.get(5, 5), 0)                                   # hole
        self.assertEqual(g.get(9, 9), 0)
        self.assertEqual(g.count(), 60 * 60 - 20 * 20)
        e = g.eroded(3)
        self.assertEqual(e.get(2.1, 5), 0)
        self.assertEqual(e.get(3, 3), 1)
        self.assertGreater(g.area_km2(), e.area_km2())

    def test_polygon_index(self):
        big = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]}
        small = {"type": "Polygon", "coordinates": [[[4, 4], [5, 4], [5, 5], [4, 5], [4, 4]]]}
        idx = geomath.PolygonIndex([("AA", big, {"iso2": "AA"}), ("BB", small, {"iso2": "BB"})])
        self.assertEqual(idx.lookup(4.5, 4.5)[0], "BB")                    # smallest match wins (enclaves)
        self.assertEqual(idx.lookup(1, 1)[0], "AA")
        self.assertIsNone(idx.lookup(20, 20))
        self.assertEqual(idx.nearest(10.05, 10.05, 25)[0], "AA")           # just offshore


class TestFrontlines(unittest.TestCase):
    UA = {"type": "Polygon", "coordinates": [[[0, 40], [10, 40], [10, 50], [0, 50], [0, 40]]]}

    def test_parse(self):
        data = json.loads((FIX / "deepstate_sample.json").read_text(encoding="utf-8"))
        parsed = frontlines.parse_deepstate(data)
        kinds = sorted(p["kind"] for p in parsed["polygons"])
        self.assertEqual(kinds, ["contested", "liberated", "occupied", "occupied_2014"])
        self.assertIn("geojson.territories.example", parsed["skipped"])    # unknown (editorial) polygons are not shown
        self.assertEqual(parsed["points_ignored"], 1)
        self.assertEqual(parsed["updated"], 1795291537.0)
        self.assertEqual(frontlines.parse_name("Окуповано /// Occupied /// geoJSON.status.occupied\n"),
                         ("Occupied", "geojson.status.occupied"))
        with self.assertRaises(ValueError):
            frontlines.parse_deepstate({"map": {}})

    def test_front_line_only_faces_ukraine(self):
        data = json.loads((FIX / "deepstate_sample.json").read_text(encoding="utf-8"))
        parsed = frontlines.parse_deepstate(data)
        fc, meta = frontlines.build_collection(parsed, self.UA)
        front = [f for f in fc["features"] if f["properties"]["kind"] == "front"]
        self.assertEqual(len(front), 1)
        xs = [p[0] for line in front[0]["geometry"]["coordinates"] for p in line]
        ys = [p[1] for line in front[0]["geometry"]["coordinates"] for p in line]
        self.assertTrue(all(abs(x - 6.0) < 0.05 for x in xs), "front must follow the western edge only")
        self.assertGreater(max(ys) - min(ys), 9.0)                         # lat 40-50 less the border margin
        self.assertTrue(all(40.0 < y < 50.0 for y in ys))
        self.assertTrue(meta["masked"])
        self.assertGreater(meta["occupied_km2"], 400000)
        self.assertGreater(meta["front_km"], 600)

    def test_unmasked_fallback(self):
        data = json.loads((FIX / "deepstate_sample.json").read_text(encoding="utf-8"))
        _, meta = frontlines.build_collection(frontlines.parse_deepstate(data), None)
        self.assertFalse(meta["masked"])
        self.assertGreater(meta["front_km"], 2000)                         # every outer edge counts without a mask


# ---------------------------------------------------------------------------------------------
class TestCountries(unittest.TestCase):
    def test_iso_rules(self):
        self.assertEqual(countries.iso2_of({"ISO_A2": "-99", "ISO_A2_EH": "FR", "NAME": "France"}), "FR")
        self.assertEqual(countries.iso2_of({"ISO_A2": "-99", "ISO_A2_EH": "-99", "NAME": "Somaliland"}), "SO")
        self.assertIsNone(countries.iso2_of({"ISO_A2": "-99", "ISO_A2_EH": "-99", "NAME": "Siachen Glacier"}))

    def test_collection(self):
        ne = {"features": [{"properties": {"ISO_A2": "UA", "ISO_A2_EH": "UA", "NAME": "Ukraine", "NAME_EN": "Ukraine",
                                           "ISO_A3_EH": "UKR", "LABEL_X": 32.1, "LABEL_Y": 49.7, "POP_EST": 4.4e7},
                            "geometry": {"type": "Polygon", "coordinates": [[[22, 45], [40, 45], [40, 52], [22, 52], [22, 45]]]}},
                           {"properties": {"ISO_A2": "XX", "NAME": "Speck"},
                            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0.0001, 0], [0, 0.0001], [0, 0]]]}}]}
        fc, n = countries.build_countries_collection(ne)
        self.assertEqual(n, 2)
        p = fc["features"][0]["properties"]
        self.assertEqual((p["iso2"], p["iso3"], p["label"]), ("UA", "UKR", [32.1, 49.7]))

    def test_worldbank_parsing(self):
        econ = countries.parse_wb_economies(json.loads((FIX / "wb_countries.json").read_text(encoding="utf-8")))
        self.assertEqual(sorted(econ), ["NOR", "UKR", "XKX"])               # aggregates dropped
        self.assertEqual(econ["UKR"]["capital"], "Kyiv")                    # World Bank says Kiev
        self.assertAlmostEqual(econ["UKR"]["lat"], 50.4536)
        ind = countries.parse_wb_indicator(json.loads((FIX / "wb_indicator.json").read_text(encoding="utf-8")))
        self.assertEqual(ind["UKR"], (9.834, 2021))                         # latest non-null value
        self.assertEqual(ind["NOR"], (4.1, 2025))
        self.assertNotIn("", ind)

    def test_index_formula(self):
        res = countries.instability(pv=-2.0, pv_year=2025, sev_sum=4000.0, rows=5000, conflict_rows=1000, events=600,
                                    coverage_days=30.0, population=4.0e7, tone=-5.0, tone_rows=3000, hazards_weighted=2.0,
                                    hazards_count=4)
        parts = {p["key"]: p["score"] for p in res["parts"]}
        self.assertEqual(parts["governance"], 87.5)                         # (1.5 + 2.0) / 4
        self.assertEqual(parts["tone"], 80.0)                               # (5 - 1) / 5
        self.assertEqual(parts["hazards"], 50.0)                            # 2 / 4
        self.assertGreater(parts["conflict"], 80)
        expected = sum(countries.WEIGHTS[k] * v for k, v in parts.items())
        self.assertEqual(res["index"], int(round(expected)))
        self.assertEqual(res["band"], countries.band(res["index"]))
        calm = countries.instability(pv=1.2, pv_year=2025, sev_sum=0.0, rows=400, conflict_rows=0, events=0, coverage_days=30.0,
                                     population=5e6, tone=0.5, tone_rows=400, hazards_weighted=None)
        self.assertLess(calm["index"], 10)
        self.assertEqual(calm["band"], "Low")
        self.assertIn("hazards", calm["missing"])                           # renormalised without the missing part
        none = countries.instability(pv=None, pv_year=None, sev_sum=0, rows=0, conflict_rows=0, events=0, coverage_days=0.1,
                                     population=None, tone=None, tone_rows=0, hazards_weighted=None)
        self.assertIsNone(none["index"])
        self.assertEqual(countries.band(19.9), "Low")
        self.assertEqual(countries.band(80), "Very high")

    def test_conflict_scaling(self):
        short, _ = countries.conflict_score(100.0, 0, 0, 2.0, 1e7)
        long_, _ = countries.conflict_score(1500.0, 0, 0, 30.0, 1e7)
        self.assertAlmostEqual(short, long_, places=1)                      # 2 days scaled to 30 equals 30 days


class TestCyberAndUcdp(unittest.TestCase):
    def test_feodo(self):
        entries = cyber.parse_feodo(json.loads((FIX / "feodo_sample.json").read_text(encoding="utf-8")))
        self.assertEqual(len(entries), 5)
        agg = cyber.aggregate_feodo(entries)
        self.assertEqual(agg["US"]["count"], 3)
        self.assertEqual(sum(d["online"] for d in agg.values()), 1)
        with self.assertRaises(ValueError):
            cyber.parse_feodo({"not": "a list"})

    def test_ioda(self):
        items = cyber.parse_ioda(json.loads((FIX / "ioda_sample.json").read_text(encoding="utf-8")))
        self.assertEqual([i["iso2"] for i in items], ["FI", "AZ", "NE"])     # continent entry dropped, sorted by score
        self.assertIn("network telescope", items[1]["methods"])
        self.assertGreater(items[0]["severity"], items[2]["severity"])
        self.assertEqual(cyber.ioda_severity(0), 0.0)
        with self.assertRaises(ValueError):
            cyber.parse_ioda({"data": None})

    def test_cloudflare(self):
        items = cyber.parse_cloudflare({"result": {"annotations": [{"id": "1", "locations": ["IR", "x"], "description": "Shutdown",
                                                                     "outage": {"outageCause": "GOVERNMENT_DIRECTED"}}]}})
        self.assertEqual(items[0]["iso2"], "IR")
        self.assertEqual(items[0]["cause"], "government directed")

    def test_ucdp(self):
        agg = ucdp.Aggregator()
        with open(FIX / "ucdp_sample.csv", encoding="utf-8", newline="") as fh:
            agg.add_csv(fh)
        self.assertEqual(agg.rows, 5)                                       # duplicate id counted once
        self.assertEqual(agg.unmapped, 1)                                   # where_prec 6 is country-level
        data = agg.result("26.1", [])
        fc = ucdp.cells_geojson(data, 2024, 2024)
        self.assertEqual(len(fc["features"]), 1)
        p = fc["features"][0]["properties"]
        self.assertEqual((p["deaths"], p["events"], p["type"]), (42, 2, "state"))
        self.assertEqual(len(ucdp.cells_geojson(data, 1989, 2026)["features"]), 3)
        links = ucdp.find_links('<a href="https://ucdp.uu.se/downloads/ged/ged251-csv.zip"></a>'
                                '<a href="https://ucdp.uu.se/downloads/ged/ged261-csv.zip"></a>'
                                '<a href="https://ucdp.uu.se/downloads/candidateged/GEDEvent_v26_0_10.csv"></a>'
                                '<a href="https://ucdp.uu.se/downloads/candidateged/GEDEvent_v25_0_12.csv"></a>'
                                '<a href="https://evil.example/downloads/ged/ged999-csv.zip"></a>')
        self.assertEqual(links["ged"], (261, "https://ucdp.uu.se/downloads/ged/ged261-csv.zip"))
        self.assertEqual(links["candidates"], ["https://ucdp.uu.se/downloads/candidateged/GEDEvent_v26_0_10.csv"])


if __name__ == "__main__":
    unittest.main()
