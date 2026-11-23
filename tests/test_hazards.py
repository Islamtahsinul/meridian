"""Hazards module tests: parsers, helpers and scales (no network; fixtures in tests/fixtures/hazards/).

Run: python -m unittest tests.test_hazards -v   (also runs tests/test_hazards_service.py)
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from meridian.modules.hazards import (common, countries, eonet, firms, gdacs, meteoalarm, nhc, nws, tsunami, usgs,
                                      volcanoes, who)

FIX = Path(__file__).resolve().parent / "fixtures" / "hazards"
NOW = datetime(2026, 11, 22, 21, 10, tzinfo=timezone.utc).timestamp()


def fixture(name: str):
    path = FIX / name
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if name.endswith((".json", ".geojson")) else text


class CommonTests(unittest.TestCase):
    def test_parse_time_forms(self):
        z = datetime(2026, 11, 22, 21, 18, tzinfo=timezone.utc).timestamp()
        self.assertEqual(common.parse_time("2026-11-22T21:18:00Z"), z)
        self.assertEqual(common.parse_time("2026-11-22T15:18:00-06:00"), z)
        self.assertEqual(common.parse_time("2026-11-22T21:18:00"), z)          # no zone means UTC
        self.assertEqual(common.parse_time("2026-11-22 21:18:00"), z)
        self.assertEqual(common.parse_time("Sun, 22 Nov 2026 21:18:00 GMT"), z)
        self.assertEqual(common.parse_time("2026.11.22 21:18:00 UTC"), z)      # tsunami.gov
        self.assertEqual(common.parse_time(z * 1000), z)                     # epoch milliseconds
        self.assertEqual(common.parse_time(str(int(z))), z)
        for bad in (None, "", "not a date", float("nan"), True):
            self.assertIsNone(common.parse_time(bad))

    def test_text_helpers(self):
        self.assertEqual(common.strip_html("<p>One&nbsp;two</p><p>Three &amp; four</p>"), "One two\nThree & four")
        self.assertEqual(common.clip("alpha beta gamma delta", 12), "alpha beta…")
        self.assertEqual(common.norm("Nevados de Chillán"), "nevadosdechillan")
        self.assertEqual(common.title_case_name("XAVIER-26"), "Xavier")
        self.assertIsNone(common.num("x"))
        self.assertEqual(common.num(" 7.5 "), 7.5)

    def test_simplify_keeps_rings_closed(self):
        ring = [[x / 10.0, (x % 7) / 1000.0] for x in range(0, 101)] + [[10, 5], [0, 5], [0, 0]]
        g = common.simplify_geometry({"type": "Polygon", "coordinates": [ring]}, 0.05, 3)
        out = g["coordinates"][0]
        self.assertLess(len(out), len(ring) / 4)
        self.assertEqual(out[0], out[-1])
        self.assertIsNone(common.simplify_geometry({"type": "Polygon", "coordinates": [[[0, 0], [1e-5, 0], [0, 1e-5], [0, 0]]]}, 0.01))
        multi = {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]], [[[5, 5], [5.000001, 5], [5, 5.000001], [5, 5]]]]}
        self.assertEqual(common.simplify_geometry(multi, 0.01)["type"], "Polygon")     # tiny island dropped

    def test_centres_and_boxes(self):
        sq = {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 2], [0, 2], [0, 0]]]}
        self.assertEqual(common.geometry_center(sq), (2.0, 1.0))
        self.assertEqual(common.geometry_bbox(sq), [0, 0, 4, 2])
        split = {"type": "MultiPolygon", "coordinates": [[[[178, 50], [180, 50], [180, 52], [178, 50]]],
                                                         [[[-180, 50], [-178, 50], [-180, 52], [-180, 50]]]]}
        parts = common.geometry_bboxes(split)
        self.assertEqual(len(parts), 2)
        self.assertFalse(common.bboxes_intersect(parts, (-10, 40, 10, 60)))   # not "the whole world"
        self.assertTrue(common.bboxes_intersect(parts, (170, 40, -170, 60)))  # a view across 180
        self.assertTrue(common.bboxes_contain(parts, 51, 179))

    def test_scales(self):
        self.assertEqual(common.quake_severity(4.5), 3)
        self.assertEqual(common.quake_severity(6.0), 6)
        self.assertEqual(common.quake_severity(5.0, "orange"), 8)
        self.assertEqual(common.quake_severity(8.4, "red", True), 10)
        self.assertEqual(common.storm_category(241), 4)
        self.assertEqual(common.storm_category(80), 0)
        self.assertEqual(common.storm_category(40), -1)
        self.assertEqual(common.category_label(-1), "Tropical depression")
        self.assertEqual(common.storm_severity(260), 9)
        self.assertEqual(common.warning_severity("Severe", "Immediate"), 7)
        self.assertEqual(common.warning_severity("Minor"), 2)

    def test_source_state_backoff(self):
        st = common.SourceState("x", 300, 1800)
        self.assertTrue(st.due(True, now=1000.0))
        st.attempt_at = 1000.0
        self.assertFalse(st.due(True, now=1100.0))
        self.assertTrue(st.due(True, now=1301.0))
        st.failed("down")
        st.failed("down")
        self.assertFalse(st.due(True, now=1301.0))        # backed off to 600 s
        self.assertTrue(st.due(True, now=1601.0))
        self.assertIsNone(common.SourceState("y", 60, None).interval(False))
        self.assertTrue(st.info()["error"])


class CountryTests(unittest.TestCase):
    def test_find_in_text_prefers_longest_names(self):
        found = [c["iso2"] for c in countries.find_in_text("Dengue - Niger, Nigeria, Guinea and Papua New Guinea")]
        self.assertEqual(found, ["NE", "NG", "GN", "PG"])
        self.assertEqual([c["iso2"] for c in countries.find_in_text("Crimean-Congo haemorrhagic fever - Iraq")], ["IQ"])
        self.assertEqual([c["iso2"] for c in countries.find_in_text("Sudan virus disease - Uganda")], ["UG"])
        self.assertEqual([c["iso2"] for c in countries.find_in_text("Chikungunya - Mayotte and Réunion")], ["YT", "RE"])
        self.assertEqual(countries.find_in_text("Influenza in the Indian Ocean"), [])

    def test_lookup_and_regions(self):
        self.assertEqual(countries.lookup("Kingdom of Saudi Arabia")["iso2"], "SA")
        self.assertEqual(countries.lookup("Viet Nam")["iso2"], "VN")
        self.assertEqual(countries.lookup("gb")["name"], "United Kingdom")
        self.assertEqual(countries.lookup("KEN")["iso2"], "KE")
        self.assertEqual(countries.find_region("Yellow fever - WHO African Region")[0], "Africa")

    def test_point_lookup_without_and_with_shapes(self):
        self.assertEqual(countries.at(51.5, -0.12)["iso2"], "GB")
        self.assertEqual(countries.at(-7.7, 127.5)["iso2"], "TL")
        self.assertEqual(countries.first_mention("Cholera in the Kingdom of Saudi Arabia"), 15)
        square = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
        countries.load_shapes([{"iso2": "ZZ", "bbox": [0, 0, 1, 1], "geometry": square}])
        try:
            self.assertIsNone(countries.at(0.5, 0.5))          # unknown code: shape known, country not in table
            self.assertIsNone(countries.at(-40, -120))          # open ocean, far from every outline
        finally:
            countries.load_shapes([])


class UsgsTests(unittest.TestCase):
    def test_feed(self):
        quakes = usgs.parse_feed(fixture("usgs_feed.json"))
        self.assertEqual(len(quakes), 4)
        q = quakes[0]
        self.assertTrue(q["id"].startswith("usgs:"))
        self.assertIsInstance(q["time"], float)
        self.assertLess(q["time"], 1e11)                        # seconds, not milliseconds
        self.assertIn(q["alert"], (None, "green", "yellow", "orange", "red"))
        blast = [x for x in quakes if x["type"] == "quarry blast"][0]
        self.assertEqual(blast["depth_km"], -0.3)
        older = dict(q, updated=q["updated"] - 100, mag=1.0)
        self.assertEqual(usgs.merge([older], [q])[q["id"]]["mag"], q["mag"])

    def test_detail_products(self):
        d = usgs.parse_detail(fixture("usgs_detail.json"))
        self.assertEqual(d["pager"]["alert"], "green")
        self.assertTrue(d["shakemap"]["image"].startswith("https://"))
        self.assertEqual(d["dyfi"]["responses"], 41)
        self.assertEqual(usgs.fault_kind(d["moment_tensor"]["rake"]), "Reverse (thrust) faulting")
        self.assertEqual(usgs.fault_kind(-90), "Normal faulting")
        self.assertEqual(usgs.fault_kind(5), "Strike-slip faulting")


class FirmsTests(unittest.TestCase):
    def build(self):
        d = firms.FireData()
        self.assertEqual(d.add_csv(fixture("firms_viirs.csv"), "noaa20"), 8)
        self.assertEqual(d.add_csv(fixture("firms_modis.csv"), "modis"), 3)
        self.assertEqual(d.add_csv(fixture("firms_empty.csv"), "snpp"), 0)
        self.assertEqual(d.add_csv(fixture("firms_api.csv"), "noaa21"), 2)
        self.assertEqual(d.add_csv("garbage\n1,2,3\n"), 0)
        return d.finish()

    def test_parse_and_index(self):
        d = self.build()
        self.assertEqual(len(d), 13)
        self.assertEqual(d.sources["noaa20"], 8)
        world = (-180.0, -85.0, 180.0, 85.0)
        self.assertEqual(sum(c["n"] for c in d.grid(world, 2.0)), 13)
        cape = (20.0, -35.0, 21.0, -34.0)
        idx = d.indices_in(cape)
        self.assertEqual(len(idx), 4)
        row = d.row(idx[0])
        self.assertEqual(row["satellite"], "NOAA-20")
        self.assertEqual(row["confidence"], "Nominal")
        self.assertEqual(d.find(row["id"]), idx[0])
        self.assertIsNone(d.find("firms:N20:1.00000:2.00000:202601010000"))
        api = d.indices_in((30.0, -11.0, 31.0, -10.0))
        self.assertEqual(len(api), 2)
        self.assertEqual({d.row(i)["confidence"] for i in api}, {"High", "Low"})
        self.assertEqual(len(d.indices_in((30.0, -11.0, 31.0, -10.0), min_conf=2)), 1)
        near = d.near(-34.418, 20.54, 5.0)
        self.assertEqual(len(near), 4)
        self.assertEqual(d.count_in(cape), 4)

    def test_ids_zoom_and_settings(self):
        fid = firms.fire_id("N20", -34.41748, 20.53745, 1795350540)
        parsed = firms.parse_id(fid)
        self.assertEqual((parsed["sat"], parsed["lat"], parsed["time"]), ("N20", -34.41748, 1795350540))
        self.assertIsNone(firms.parse_id("firms:N20:x:y:z"))
        self.assertEqual(firms.cell_for_zoom(1.5), 2.0)
        self.assertEqual(firms.cell_for_zoom(5.0), 0.25)
        self.assertIsNone(firms.cell_for_zoom(7))
        self.assertEqual(firms.satellites_from_setting("NOAA-21, modis, bogus"), ["noaa21", "modis"])
        self.assertEqual(firms.satellites_from_setting(""), list(firms.DEFAULT_SATELLITES))
        self.assertEqual(firms.cluster_severity(25000, 10), 8)
        self.assertEqual(firms.cluster_severity(100, 10), 3)
        self.assertEqual(firms.confidence_text(-3), "High")
        self.assertEqual(firms.confidence_text(87), "87%")

    def test_clusters(self):
        d = self.build()
        self.assertEqual(d.clusters(min_n=4, min_frp=1e9), [c for c in d.clusters(min_n=4, min_frp=1e9)])
        top = d.clusters(min_n=2, min_frp=1e9)
        self.assertTrue(top and top[0]["n"] >= 2)


class EonetTests(unittest.TestCase):
    def test_events(self):
        items = eonet.parse_events(fixture("eonet.json"))
        cats = {e["category"]: e for e in items}
        self.assertEqual(set(cats), {"wildfires", "severeStorms", "volcanoes", "seaLakeIce"})
        self.assertEqual(cats["wildfires"]["kind"], "fire")
        self.assertEqual(cats["seaLakeIce"]["kind"], "disaster")
        storm = cats["severeStorms"]
        self.assertEqual(len(storm["track"]), 3)
        self.assertEqual((storm["lat"], storm["lon"]), (storm["track"][-1]["lat"], storm["track"][-1]["lon"]))
        self.assertLessEqual(storm["start"], storm["time"])
        self.assertRegex(cats["volcanoes"]["vnum"], r"^\d{6}$")
        self.assertEqual(eonet.unit_label("kts"), "kn")
        self.assertIsNone(eonet.parse_event({"id": "x", "geometry": []}))


class GdacsTests(unittest.TestCase):
    def test_event_list_and_rss(self):
        events = gdacs.parse_events(fixture("gdacs_events.json"))
        self.assertEqual([e["type"] for e in events], ["EQ", "DR", "FL"])     # the Poly_Circle part is skipped
        eq = events[0]
        self.assertEqual(eq["kind"], "earthquake")
        self.assertTrue(eq["report_url"].startswith("https://www.gdacs.org/report.aspx"))
        rss = gdacs.parse_rss(fixture("gdacs_rss.xml"))
        self.assertEqual(len(rss), 2)
        self.assertTrue(rss[0]["population"])
        merged = gdacs.merge_events(events, rss)
        self.assertGreaterEqual(len(merged), 3)
        self.assertEqual(gdacs.parse_rss("<not xml"), [])

    def test_labels_and_ongoing(self):
        dr = {"name": "Orange Drought in Albania, Austria, Bosnia and Herzegovina, Belgium", "type": "DR"}
        self.assertEqual(gdacs.short_name(dr), "Drought in Albania and 3 more")
        self.assertEqual(gdacs.short_name({"name": "Tropical Cyclone XAVIER-26", "event_name": "XAVIER-26", "type": "TC"}),
                         "Cyclone Xavier")
        self.assertTrue(gdacs.ongoing({"current": False, "to": NOW - 2 * 86400}, NOW))
        self.assertFalse(gdacs.ongoing({"current": False, "to": NOW - 20 * 86400}, NOW))

    def test_cyclone_geometry(self):
        storms = gdacs.parse_tc_map(fixture("gdacs_tc_map.json"))
        self.assertEqual(len(storms), 1)
        s = storms[0]
        self.assertEqual(s["name"], "Xavier")
        self.assertTrue(s["cone"])
        self.assertEqual(sorted(b["kmh"] for b in s["buffers"]), [60, 120])
        self.assertEqual(len(s["points"]), 5)
        forecast = [p for p in s["points"] if p["forecast"]]
        self.assertEqual(len(forecast), 2)
        self.assertFalse(s["current"]["forecast"])
        self.assertTrue(all(p["t"] for p in s["points"]))
        self.assertTrue(any(seg["forecast"] for seg in s["segments"]))
        self.assertGreater(s["event"]["severity"], 200)

    def test_event_geometry_simplified(self):
        fc = gdacs.simplify_event_geometry(fixture("gdacs_tc_map.json"))
        self.assertTrue(fc["features"])
        self.assertTrue(all(f["properties"]["role"] != "Point_Centroid" for f in fc["features"]))

    def test_feed_links_are_kept_on_gdacs_org(self):
        # the server fetches the geometry and details links, so a poisoned feed must not point it elsewhere
        ok = ("https://www.gdacs.org/gdacsapi/api/polygons/getgeometry?eventtype=EQ&eventid=1",
              "https://gdacs.org/report.aspx?eventid=1", "https://WWW.GDACS.ORG:443/x")
        bad = ("http://www.gdacs.org/x", "https://127.0.0.1:8790/api/core/status", "http://169.254.169.254/latest",
               "https://www.gdacs.org.evil.test/x", "https://evil.test/?u=https://www.gdacs.org/",
               "https://www.gdacs.org@evil.test/x", "https://www.gdacs.org:8443/x", "https://www.gdacs.org/a b",
               "javascript:alert(1)", "", None, 42, ["https://www.gdacs.org/"])
        for url in ok:
            self.assertEqual(gdacs.gdacs_url(url), url)
        for url in bad:
            self.assertIsNone(gdacs.gdacs_url(url), repr(url))
        data = fixture("gdacs_events.json")
        props = data["features"][0]["properties"]
        props["url"] = {"geometry": "http://127.0.0.1:8790/api/core/favourites",
                        "details": "https://intranet.example/admin?x=1", "report": "javascript:alert(1)"}
        ev = gdacs.parse_events(data)[0]
        self.assertIsNone(ev["geometry_url"])
        self.assertTrue(ev["details_url"].startswith(gdacs.DETAILS_URL + "?"))
        self.assertTrue(ev["report_url"].startswith("https://www.gdacs.org/report.aspx"))
        self.assertNotIn("report_url", gdacs.parse_details({"properties": {"url": {"report": "https://evil.test/"}}}))


class NhcTests(unittest.TestCase):
    def test_current_and_layers(self):
        storms = nhc.parse_current(fixture("nhc_current.json"))
        self.assertEqual(len(storms), 1)
        s = storms[0]
        self.assertEqual((s["id"], s["classification_label"], s["wind_kmh"], s["basin"]),
                         ("nhc:al142026", "Hurricane", 194, "Atlantic"))
        self.assertEqual(nhc.parse_current({"activeStorms": []}), [])
        ids = nhc.layer_ids(fixture("nhc_service.json"))
        self.assertEqual(ids["AT4"], {"points": 84, "track": 85, "cone": 86, "past": 90})
        pts = nhc.forecast_points(nhc.parse_layer_geojson(fixture("nhc_points.geojson")))
        self.assertEqual([p["tau"] for p in pts], [0, 12, 24])
        self.assertEqual(pts[1]["wind_kmh"], 204)


class VolcanoTests(unittest.TestCase):
    def test_gvp_and_hans(self):
        items = volcanoes.parse_gvp(fixture("gvp_wfs.json"))
        names = {v["name"]: v for v in items}
        self.assertIn("Kilauea", names)
        self.assertEqual(names["Kilauea"]["id"], "gvp:332010")
        hans = volcanoes.parse_hans(fixture("hans_elevated.json"), fixture("hans_cap.json"))
        self.assertIn("332010", hans)
        self.assertIn(hans["332010"]["color"], ("YELLOW", "ORANGE", "RED"))
        self.assertGreaterEqual(volcanoes.activity_rank({"hans": hans["332010"]}), 2)
        self.assertEqual(volcanoes.activity_rank({"gdacs": {"alert": "Red"}}), 4)
        self.assertEqual(volcanoes.activity_rank({}), 0)
        self.assertEqual(volcanoes.eruption_label(-8300), "8,300 BCE")
        self.assertEqual(volcanoes.eruption_label(None), "Unknown")
        self.assertEqual(volcanoes.clean_report_name("Eruption  Kanlaon"), "Kanlaon")
        self.assertEqual(volcanoes.clean_report_name("Etna Volcano, Italy"), "Etna")
        self.assertEqual(volcanoes.match_by_name("Kilauea", items)["id"], "gvp:332010")
        self.assertEqual(volcanoes.match_by_name("Somewhere", items, 19.42, -155.29)["id"], "gvp:332010")


class NwsTests(unittest.TestCase):
    def test_alerts(self):
        alerts = nws.parse_alerts(fixture("nws_alerts.json"), NOW)
        self.assertEqual(len(alerts), 2)                       # the test message and the expired one are dropped
        poly = [a for a in alerts if a["geometry"]][0]
        zoned = [a for a in alerts if not a["geometry"]][0]
        self.assertTrue(poly["id"].startswith("nws:"))
        self.assertEqual(poly["severity"], "Severe")
        self.assertTrue(poly["lat"])
        self.assertEqual(len(zoned["zones"]), 2)
        self.assertEqual(zoned["zones"][0][0], "forecast")
        self.assertEqual(nws.zone_ref("https://api.weather.gov/zones/county/ARC053"), ("county", "ARC053"))
        self.assertIsNone(nws.zone_ref("https://example.org/x"))
        zone = nws.parse_zone(fixture("nws_zone.json"))
        self.assertEqual(zone["id"], "ARC053")
        self.assertLessEqual(len(zone["coarse"]["coordinates"][0]), len(zone["fine"]["coordinates"][0]))
        self.assertTrue(nws.in_coverage(34.3, -92.5))
        self.assertFalse(nws.in_coverage(51.5, -0.1))
        self.assertIn("lat=34.3000", nws.forecast_page(34.3, -92.5))
        text = "Rain will\nspread east.\n\n* WHAT...Heavy freezing\nspray.\n* WHERE...Flaxman Island."
        self.assertEqual(nws.unwrap(text, 500), "Rain will spread east.\n\n* WHAT...Heavy freezing spray.\n* WHERE...Flaxman Island.")


class MeteoAlarmTests(unittest.TestCase):
    def test_feeds(self):
        uk = meteoalarm.parse_atom(fixture("meteoalarm_uk.xml"), "united-kingdom", NOW)
        self.assertEqual(len(uk), 1)
        w = uk[0]
        self.assertEqual((w["country"], w["severity"], w["level"]), ("GB", "Moderate", "yellow"))
        self.assertGreater(len(w["polygons"][0]), 100)
        self.assertTrue(w["id"].startswith("meteoalarm:") and w["id"].endswith(".p0"))
        es = meteoalarm.parse_atom(fixture("meteoalarm_spain.xml"), "spain", NOW)
        self.assertTrue(es and all(x["codes"] and x["codes"][0][0] == "EMMA_ID" for x in es))
        self.assertTrue(all(x["id"].endswith(".g0") for x in es))
        self.assertEqual(meteoalarm.parse_atom(fixture("meteoalarm_spain.xml"), "spain", NOW + 30 * 86400), [])
        self.assertEqual(meteoalarm.parse_atom("<broken", "spain", NOW), [])
        self.assertTrue(all(x["cap_url"].startswith("https://feeds.meteoalarm.org/") for x in es if x.get("cap_url")))

    def test_cap_links_only_on_the_feed_host(self):
        # the CAP link is fetched by the server when a warning card opens
        self.assertTrue(meteoalarm.cap_url_ok("https://feeds.meteoalarm.org/api/v1/warnings/feeds-spain/7d8b"))
        for url in ("https://127.0.0.1/cap", "https://intranet.example/cap", "http://feeds.meteoalarm.org/x",
                    "https://feeds.meteoalarm.org.evil.test/x", "https://feeds.meteoalarm.org@evil.test/x",
                    "https://feeds.meteoalarm.org:8443/x", "https://feeds.meteoalarm.org/a b", "", None):
            self.assertFalse(meteoalarm.cap_url_ok(url), repr(url))
        text = fixture("meteoalarm_spain.xml").replace("https://feeds.meteoalarm.org/api/v1/warnings/",
                                                       "https://10.0.0.1/api/v1/warnings/")
        es = meteoalarm.parse_atom(text, "spain", NOW)
        self.assertTrue(es)
        self.assertTrue(all(x.get("cap_url") is None for x in es))

    def test_regions_and_cap(self):
        built = meteoalarm.build_regions(fixture("meteoalarm_geocodes.json"), fixture("meteoalarm_aliases.csv"))
        self.assertIn("ES191", built["regions"])
        self.assertEqual(built["aliases"]["ES511"], "ES191")
        r = built["regions"]["ES191"]
        self.assertTrue(r["fine"] and r["coarse"] and r["lat"])
        tree = [{"name": "MeteoAlarm_Geocodes_2026_02_20.json"}, {"name": "MeteoAlarm_Geocodes_2026_11_13.json"},
                {"name": "MeteoAlarm_Geocodes-Aliases_2026_11_13.csv"}, {"name": "README.md"}]
        self.assertEqual(meteoalarm.latest_files(tree), ("MeteoAlarm_Geocodes_2026_11_13.json",
                                                        "MeteoAlarm_Geocodes-Aliases_2026_11_13.csv"))
        cap = meteoalarm.parse_cap(fixture("meteoalarm_cap.xml"))
        self.assertEqual(cap["issuer"], "UK Met Office")
        self.assertEqual(cap["hazard"], "Rain")
        self.assertIn("rain", cap["description"].lower())
        self.assertEqual(meteoalarm.parse_polygon("51.0,-4.3 51.1,-4.2 51.2,-4.4"),
                         [[-4.3, 51.0], [-4.2, 51.1], [-4.4, 51.2], [-4.3, 51.0]])
        self.assertIsNone(meteoalarm.parse_polygon("1,2 3,4"))
        self.assertEqual(meteoalarm.countries_from_setting("GB, ireland, xx"), ["united-kingdom", "ireland"])
        self.assertEqual(len(meteoalarm.countries_from_setting("all")), 39)
        self.assertEqual(meteoalarm.awareness_level("Orange wind warning", "Severe"), "orange")
        self.assertEqual(meteoalarm.awareness_level("fog", "Minor"), "green")


class TsunamiTests(unittest.TestCase):
    def test_feeds(self):
        paaq = tsunami.parse_atom(fixture("tsunami_PAAQAtom.xml"), "PAAQ")
        pheb = tsunami.parse_atom(fixture("tsunami_PHEBAtom.xml"), "PHEB")
        self.assertEqual(len(paaq), 1)
        self.assertEqual(len(pheb), 1)
        m = paaq[0]
        self.assertEqual((m["category"], m["magnitude"], m["magnitude_type"]), ("Information", 4.0, "Ml"))
        self.assertIn("NO tsunami danger", m["note"])
        self.assertTrue(m["bulletin_url"].endswith(".txt"))
        self.assertTrue(m["id"].startswith("tsunami:PAAQ:"))
        self.assertEqual(pheb[0]["center_name"], "Pacific Tsunami Warning Center")
        self.assertEqual(tsunami.parse_fields("Category: Warning Note: leave the coast"),
                         {"Category": "Warning", "Note": "leave the coast"})


class WhoTests(unittest.TestCase):
    def test_reports(self):
        items = who.parse_items(fixture("who_don.json"))
        self.assertEqual(len(items), 4)
        first = items[0]
        self.assertTrue(first["id"].startswith("who:"))
        self.assertTrue(first["places"])
        self.assertTrue(first["url"].startswith("https://www.who.int/emergencies/disease-outbreak-news/item/"))
        self.assertEqual(who.split_title("Chikungunya virus disease - Switzerland"), ("Chikungunya virus disease", "Switzerland"))
        self.assertEqual(who.split_title("Ebola disease caused by Bundibugyo virus, Democratic Republic of the Congo & Uganda"),
                         ("Ebola disease caused by Bundibugyo virus", "Democratic Republic of the Congo & Uganda"))
        self.assertEqual(who.split_title("Middle East respiratory syndrome (MERS) � Qatar")[1], "Qatar")
        places, region = who.locate("Yellow fever - WHO African Region", "Cases were reported in Ghana and Togo.")
        self.assertEqual([p["iso2"] for p in places], ["GH", "TG"])
        self.assertEqual(region, "Africa")
        places, _ = who.locate("Yellow fever - Global", "")
        self.assertEqual(places, [])
        self.assertEqual(who.disease_key("Ebola disease caused by Bundibugyo virus"), "ebola bundibugyo")
        self.assertGreaterEqual(who.severity(first, first["published"] + 86400), 5)
        self.assertIsNone(who.parse_item({"Title": "x"}))


def load_tests(loader, standard_tests, pattern):
    # `python -m unittest tests.test_hazards` also runs the offline end-to-end service tests;
    # discovery (a pattern is given) finds that file by itself.
    if pattern is None:
        from tests import test_hazards_service
        standard_tests.addTests(loader.loadTestsFromModule(test_hazards_service))
    return standard_tests


if __name__ == "__main__":
    unittest.main()
