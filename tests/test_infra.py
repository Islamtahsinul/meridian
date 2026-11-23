"""Infra module tests: parsers, dataset builds against a fake network, the in-memory service
(search, nearby, details, the country summary from CONTRACT 9.1) and the AI tools.

Run: python -m unittest tests.test_infra -v      (no network: fixtures in tests/fixtures/infra/)
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from meridian.config import Config
from meridian.datasets import BuildContext
from meridian.modules.infra import (airports, cables, common, companies, countries, dams, datacenters, ports,
                                    power, service)

FIX = Path(__file__).parent / "fixtures" / "infra"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# A fake network that serves fixtures, and a real BuildContext around it
# ---------------------------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json", status: int = 200):
        self.body = body
        self.status = status
        self.headers = {"content-type": content_type}

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def text(self, encoding=None):
        return self.body.decode("utf-8")


class FakeNet:
    """Maps URL substrings to fixture files. Unknown URLs answer like a single-page app (HTML 200)."""

    def __init__(self, routes: dict[str, str]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def _find(self, url: str):
        for key, name in self.routes.items():
            if key in url:
                return name
        return None

    def download(self, url, dest, **kw):
        self.calls.append((url, kw))
        name = self._find(url)
        if name is None:
            from meridian.net import NetError
            raise NetError("fake: HTTP 503", status=503, url=url)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIX / name, dest)
        return dest

    def request(self, url, *, params=None, headers=None, **kw):
        from meridian.net import Net
        url = Net.build_url(url, params)
        self.calls.append((url, {"params": params, "headers": headers, **kw}))
        name = self._find(url)
        if name is None:
            return FakeResponse(b"<!DOCTYPE html><html></html>", "text/html; charset=UTF-8")
        ctype = "application/json" if name.endswith(".json") else ("text/xml" if name.endswith(".xml") else "text/csv")
        return FakeResponse((FIX / name).read_bytes(), ctype)

    def get_json(self, url, **kw):
        return self.request(url, **kw).json()

    def get_text(self, url, **kw):
        return self.request(url, **kw).text()


ROUTES = {
    "global_power_plant_database.csv": "gppd_sample.csv",
    "airports.csv": "airports_sample.csv", "runways.csv": "runways_sample.csv",
    "airport-frequencies.csv": "frequencies_sample.csv", "regions.csv": "regions_sample.csv",
    "code-list.csv": "locode_sample.csv", "ne_10m_ports": "ne_ports_sample.geojson",
    "api/fac": "peeringdb_sample.json", "overpass-api.de": "overpass_sample.json",
    "cable-geo.json": "cable_geo_sample.json", "landing-point-geo.json": "landing_geo_sample.json",
    "country/france.json": "tg_country_france.json", "country/tunisia.json": "tg_country_tunisia.json",
    "country/tonga.json": "tg_country_tonga.json", "cable/alpha.json": "tg_cable_alpha.json",
    "ids=EGLL": "metar_egll.json", "bbox=": "metar_bbox.json",
}


class Env:
    """Temporary data dirs, a fake module context and BuildContexts for each dataset."""

    def __init__(self, routes=None):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.net = FakeNet(routes or ROUTES)
        self.statuses: dict[str, dict] = {}
        self.m = SimpleNamespace(
            name="infra", data_dir=root / "build", raw_dir=root / "raw", cache_dir=root / "cache", net=self.net,
            log=logging.getLogger("test.infra"), store=None, config=Config(env_file=None),
            status=lambda sid, **kw: self.statuses.__setitem__(sid, kw))

    def ds(self, name: str, force: bool = False) -> BuildContext:
        return BuildContext(self.m, name, 24.0, force=force)

    def close(self):
        self.tmp.cleanup()


# ---------------------------------------------------------------------------------------------
class CountryTests(unittest.TestCase):
    def test_codes_and_names(self):
        self.assertEqual(countries.from_alpha3("GBR"), "GB")
        self.assertEqual(countries.from_alpha3("KOS"), "XK")
        self.assertEqual(countries.from_name("Congo, Dem. Rep."), "CD")
        self.assertEqual(countries.from_name("Côte d'Ivoire"), "CI")
        self.assertEqual(countries.from_name("Virgin Islands (U.S.)"), "VI")
        self.assertEqual(countries.from_name("United States of America"), "US")
        self.assertEqual(countries.resolve("United Kingdom"), "GB")
        self.assertEqual(countries.resolve("br"), "BR")
        self.assertIsNone(countries.resolve("Atlantis"))

    def test_split_place(self):
        self.assertEqual(countries.split_place("Muanda, Congo, Dem. Rep."), ("Muanda", "CD"))
        self.assertEqual(countries.split_place("Pacific City, OR, United States"), ("Pacific City, OR", "US"))
        self.assertEqual(countries.split_place("North Salina, Bonaire, Sint Eustatius and Saba")[1], "BQ")
        self.assertEqual(countries.split_place("Kralendijk, Bonaire, Netherlands")[1], "NL")
        self.assertEqual(countries.split_place("Nowhere, Atlantis"), ("Nowhere, Atlantis", None))


class CommonTests(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(common.norm("São Paulo–Guarulhos"), "sao paulo guarulhos")
        self.assertEqual(common.fnum("1,234.5"), 1234.5)
        self.assertIsNone(common.fnum("nan"))
        self.assertEqual(common.year_of("1974.5"), 1974)
        self.assertEqual(common.year_of("+2003-01-01T00:00:00Z"), 2003)
        self.assertIsNone(common.year_of("garbage"))
        self.assertEqual(common.wkt_point("Point(-0.46 51.47)"), (51.47, -0.46))
        self.assertIsNone(common.wkt_point("Point(0 0)"))
        self.assertEqual(common.http_url("www.example.org"), "https://www.example.org")
        self.assertIsNone(common.http_url("javascript:alert(1)"))
        self.assertIsNone(common.http_url('https://x.example/"><script>'))

    def test_collection_search_and_near(self):
        rows = [{"id": "EGLL", "title": "London Heathrow Airport", "lat": 51.47, "lon": -0.46, "weight": 1.0,
                 "codes": ["EGLL", "LHR"], "text": "London", "country": "GB", "detail": {"x": 1}},
                {"id": "EGLC", "title": "London City Airport", "lat": 51.505, "lon": 0.055, "weight": 0.6,
                 "codes": ["EGLC", "LCY"], "text": "London", "country": "GB", "detail": {}},
                {"id": "KJFK", "title": "John F Kennedy International Airport", "lat": 40.64, "lon": -73.78,
                 "weight": 1.0, "codes": ["KJFK", "JFK"], "detail": {}}]
        c = common.Collection("airport", "infra.airports", rows)
        self.assertEqual(c.search("lhr")[0]["id"], "EGLL")
        self.assertGreaterEqual(c.search("LHR")[0]["score"], 0.97)
        self.assertEqual([e["id"] for e in c.search("london", 5)], ["EGLL", "EGLC"])
        self.assertEqual(c.search("kennedy")[0]["id"], "KJFK")
        self.assertEqual(c.search("zz"), [])
        near = c.near(51.5, -0.1, 50)
        self.assertEqual(near[0]["id"], "EGLC")
        self.assertEqual(len(near), 2)
        self.assertEqual(c.detail("EGLL"), {"x": 1})
        self.assertIsNone(c.detail("NOPE"))
        e = c.entity_for("EGLL")
        for key in ("kind", "id", "title", "lat", "lon", "layer"):
            self.assertIn(key, e)


class PowerTests(unittest.TestCase):
    def test_parse(self):
        plants = {p["id"]: p for p in power.parse_gppd(fixture("gppd_sample.csv"))}
        self.assertEqual(set(plants), {"GBR0000001", "GBR0000002", "WRI1000003", "USA0000005"})
        coal = plants["GBR0000001"]
        self.assertEqual((coal["capacity_mw"], coal["fuel"], coal["country"]), (3906, "coal", "GB"))
        self.assertEqual(coal["other_fuels"], ["Biomass"])
        self.assertEqual(coal["commissioning_year"], 1974)
        self.assertEqual((coal["generation_gwh"], coal["generation_year"]), (15000, 2019))
        wind = plants["GBR0000002"]
        self.assertEqual((wind["estimated_generation_gwh"], wind["estimated_generation_year"]), (34.5, 2017))
        self.assertEqual(plants["WRI1000003"]["country"], "XK")
        self.assertEqual(plants["USA0000005"]["fuel"], "tidal")

    def test_build_split_and_stats(self):
        env = Env()
        try:
            with mock.patch.object(power, "MIN_PLANTS", 1):
                power.build(env.ds("power"))
            major = json.loads((env.m.data_dir / "power.geojson").read_text(encoding="utf-8"))
            minor = json.loads((env.m.data_dir / "power-minor.geojson").read_text(encoding="utf-8"))
            self.assertEqual(sorted(f["properties"]["id"] for f in major["features"]), ["GBR0000001", "WRI1000003"])
            self.assertEqual(len(minor["features"]), 2)
            props = major["features"][-1]["properties"]
            self.assertEqual(set(props), {"id", "n", "c", "f"})
            items = json.loads((env.m.data_dir / "power-details.json").read_text(encoding="utf-8"))["items"]
            stats = power.country_stats(items)
            self.assertEqual(stats["GB"]["power_plants"], 2)
            self.assertEqual(stats["GB"]["capacity_mw"], {"Coal": 3906, "Wind": 12.5})
        finally:
            env.close()


class AirportTests(unittest.TestCase):
    def test_parse(self):
        regions = airports.parse_regions(fixture("regions_sample.csv"))
        aps = {a["id"]: a for a in airports.parse_airports(fixture("airports_sample.csv"), regions)}
        self.assertEqual(set(aps), {"EGLL", "EGKK", "EGLC", "GB-0001"})   # closed, heliport, (0, 0) dropped
        self.assertEqual((aps["EGLL"]["iata"], aps["EGLL"]["type"], aps["EGLL"]["region"]), ("LHR", 3, "England"))
        self.assertEqual(aps["EGLC"]["icao"], "EGLC")      # from gps_code when icao_code is empty
        self.assertIsNone(aps["EGLC"]["home"])             # javascript: link refused
        rws = airports.parse_runways(fixture("runways_sample.csv"))
        self.assertEqual([r["name"] for r in rws["EGLL"]], ["09L/27R", "09R/27L"])
        self.assertEqual(len(rws["EGLL"][0]["ends"]), 2)
        self.assertEqual(len(rws["GB-0001"]), 1)           # the closed runway is dropped
        self.assertEqual(rws["GB-0001"][0]["surface"], "Grass")
        fq = airports.parse_frequencies(fixture("frequencies_sample.csv"))
        self.assertEqual([f["type"] for f in fq["EGLL"]], ["TWR", "GND", "ATIS", "APP"])
        self.assertEqual(len(fq["GB-0001"]), 1)

    def test_metar(self):
        r = airports.parse_metar(json.loads(fixture("metar_egll.json"))[0])
        self.assertEqual(r["station"], "EGLL")
        self.assertEqual(r["category"], "VFR")
        self.assertEqual(r["wind"], "030° at 4 kn")
        self.assertEqual(r["visibility"], "10 km or more")
        self.assertEqual(r["temp_c"], 5)
        r2 = airports.parse_metar(json.loads(fixture("metar_bbox.json"))[0])
        self.assertEqual(r2["visibility"], "4.8 km")
        self.assertEqual(r2["wind"], "Variable at 3 kn")
        self.assertEqual(r2["clouds"], ["Broken at 800 ft"])
        self.assertIsNone(airports.parse_metar({"icaoId": "X"}))

    def test_fetch_metar_nearest(self):
        net = FakeNet(ROUTES)
        direct = airports.fetch_metar(net, {"icao": "EGLL", "lat": 51.47, "lon": -0.46})
        self.assertFalse(direct["nearest"])
        near = airports.fetch_metar(net, {"icao": None, "lat": 51.6, "lon": -0.8})
        self.assertTrue(near["nearest"])
        self.assertEqual(near["report"]["station"], "EGWU")
        self.assertLess(near["distance_km"], 40)


class PortTests(unittest.TestCase):
    def test_locode_and_natural_earth(self):
        lat, lon = ports.parse_locode_coords("5054N 00124W")
        self.assertAlmostEqual(lat, 50.9, places=3)
        self.assertAlmostEqual(lon, -1.4, places=3)
        self.assertIsNone(ports.parse_locode_coords("bad"))
        locode, missing = ports.parse_locode(fixture("locode_sample.csv"), with_missing=True)
        self.assertEqual({p["id"] for p in locode},
                         {"locode:GBFXT", "locode:GBLON", "locode:BRSSZ", "locode:NZWLG", "locode:EEKKM"})
        self.assertEqual({p["id"] for p in missing}, {"locode:GBSOU", "locode:GBZZZ"})   # no coordinates listed
        ne = ports.parse_ne_ports(fixture("ne_ports_sample.geojson"))
        stats = {}
        self.assertEqual(ports.merge_ne(locode, ne, missing, stats=stats), 1)
        self.assertEqual(stats, {"sized": 1, "placed": 1, "added": 1})
        by = {p["id"]: p for p in locode}
        self.assertEqual(by["locode:GBSOU"]["size"], "large")          # placed at the Natural Earth point
        self.assertEqual((by["locode:GBSOU"]["lat"], by["locode:GBSOU"]["lon"]), (50.9, -1.4))
        self.assertEqual(by["locode:BRSSZ"]["size"], "medium")          # sized and moved to the precise point
        self.assertIsNone(by["locode:GBFXT"]["size"])
        self.assertEqual(by["ne:3"]["source"], "Natural Earth")         # Natural Earth only, country unknown
        self.assertIsNone(by["ne:3"]["country"])

    def test_accented_names_survive_any_encoding(self):
        # UTF-8 source (datasets/un-locode) and Latin-1 source (UNECE's own files) both decode.
        utf8 = ports.parse_locode(common.decode_text((FIX / "locode_sample.csv").read_bytes()))
        self.assertIn("Kakumäe", {p["name"] for p in utf8})
        latin = ports.parse_locode(common.decode_text((FIX / "locode_latin1_sample.csv").read_bytes()))
        self.assertEqual({p["name"] for p in latin}, {"Kakumäe", "Sète"})
        self.assertEqual(common.decode_text(b"\xef\xbb\xbfS\xc3\xa8te"), "Sète")   # BOM stripped
        env = Env({**ROUTES, "code-list.csv": "locode_latin1_sample.csv"})
        try:
            with mock.patch.object(ports, "MIN_LOCODE", 1):
                ports.build(env.ds("ports"))
            raw = (env.m.data_dir / "ports.json").read_bytes()
            self.assertTrue(raw.isascii())                       # escaped, so the reader's code page cannot matter
            self.assertIn(b"Kakum\\u00e4e", raw)
            for enc in ("utf-8", "cp1252", "latin-1"):
                names = {r["name"] for r in json.loads(raw.decode(enc))}
                self.assertIn("Kakumäe", names)
                self.assertIn("Sète", names)
            self.assertNotIn("�", (env.m.data_dir / "ports-details.json").read_text(encoding="utf-8"))
        finally:
            env.close()

    def test_wpi(self):
        wpi = {p["id"]: p for p in ports.parse_wpi(fixture("wpi_sample.csv"))}
        self.assertEqual(set(wpi), {"wpi:34230", "wpi:40000"})
        sou = wpi["wpi:34230"]
        self.assertEqual((sou["size"], sou["harbour_type"], sou["shelter"], sou["country"]),
                         ("large", "Coastal natural", "Good", "GB"))
        self.assertEqual(sou["facilities"], ["Container", "Oil terminal", "Dry dock", "Railway"])
        self.assertEqual(wpi["wpi:40000"]["size"], "large")
        self.assertEqual(wpi["wpi:40000"]["shelter"], "Excellent")

    def test_build_ports_json_contract_and_wpi_merge(self):
        env = Env()
        try:
            with mock.patch.object(ports, "MIN_LOCODE", 1):
                ports.build(env.ds("ports"))
            served = json.loads((env.m.data_dir / "ports.json").read_text(encoding="utf-8"))
            self.assertEqual(len(served), 7)
            for row in served:   # CONTRACT 9.1 shape for the ships module
                self.assertEqual(set(row), {"id", "name", "country", "lat", "lon", "size"})
            doc = json.loads((env.m.data_dir / "ports-details.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["source"], "locode")
            self.assertTrue(doc["wpi_error"])
            # The World Port Index arrives later: it replaces the LOCODE entries it covers.
            env.net.routes["UpdatedPub150"] = "wpi_sample.csv"
            with mock.patch.object(ports, "MIN_WPI", 1):
                ports.build_wpi(env.ds("wpi"), delays=(0.0,))
            doc = json.loads((env.m.data_dir / "ports-details.json").read_text(encoding="utf-8"))
            ids = {p["id"] for p in doc["items"]}
            self.assertEqual(doc["source"], "wpi")
            self.assertIn("wpi:34230", ids)
            self.assertNotIn("locode:GBSOU", ids)
            self.assertIn("locode:GBFXT", ids)
        finally:
            env.close()

    def test_wpi_failure_cools_down(self):
        env = Env()
        try:
            ds = env.ds("wpi")
            with self.assertRaises(RuntimeError):
                ports.build_wpi(ds, delays=(0.0,))
            calls = len(env.net.calls)
            with self.assertRaises(RuntimeError) as ctx:
                ports.build_wpi(ds, delays=(0.0,))
            self.assertIn("6 hours", str(ctx.exception))
            self.assertEqual(len(env.net.calls), calls)   # no second request inside the cool-down
        finally:
            env.close()


class DatacenterTests(unittest.TestCase):
    def test_parse_and_merge(self):
        pdb = datacenters.parse_peeringdb(fixture("peeringdb_sample.json"))
        self.assertEqual([f["id"] for f in pdb], ["peeringdb:1", "peeringdb:2"])
        self.assertIsNone(pdb[1]["website"])
        self.assertNotIn("tech_email", pdb[0])
        self.assertFalse(any("@" in json.dumps(f) for f in pdb))   # no contact details kept
        osm = datacenters.parse_overpass(fixture("overpass_sample.json"))
        self.assertEqual(len(osm), 3)
        extra = datacenters.merge_osm(pdb, osm)
        self.assertEqual({o["id"] for o in extra}, {"osm:node/200", "osm:relation/300"})

    def test_build(self):
        env = Env()
        try:
            with mock.patch.object(datacenters, "MIN_FACILITIES", 1):
                datacenters.build(env.ds("datacenters"))
            doc = json.loads((env.m.data_dir / "datacenters-details.json").read_text(encoding="utf-8"))
            self.assertEqual(len(doc["items"]), 4)
            self.assertIsNone(doc["osm_error"])
        finally:
            env.close()


class DamTests(unittest.TestCase):
    def test_assemble(self):
        rows = [
            {"d": "http://www.wikidata.org/entity/Q1", "l": "Example Dam", "coord": "Point(111.0 30.8)", "cc": "CN",
             "river": "Yangtze", "inc": "1994-01-01T00:00:00Z", "open": "", "res": "Example Reservoir", "op": "Example Corp",
             "len": "2335", "use": "hydroelectricity"},
            {"d": "http://www.wikidata.org/entity/Q1", "l": "Example Dam", "coord": "Point(111.0 30.8)", "cc": "CN",
             "river": "Yangtze", "inc": "1994-01-01T00:00:00Z", "open": "2003-01-01T00:00:00Z", "use": "flood control"},
            {"d": "http://www.wikidata.org/entity/Q2", "coord": "Point(14.4 48.7)", "cc": "CZ"},
            {"d": "http://www.wikidata.org/entity/Q3", "coord": "not a point"},
        ]
        out = {d["qid"]: d for d in dams.assemble(rows, {"Q2": "Lipno"}, {"Q1": 181.0}, {"Q1": 27.2e6},
                                                   {"Q1": 22500e6}, {"Q1": "https://en.wikipedia.org/wiki/Example"},
                                                   {"Q1": 39.3e9})}
        self.assertEqual(set(out), {"Q1", "Q2"})
        q1 = out["Q1"]
        self.assertEqual((q1["height_m"], q1["reservoir_mcm"], q1["power_mw"], q1["year"]), (181, 39300, 22500, 1994))
        self.assertEqual(q1["volume_mcm"], 27.2)          # the dam item's own volume, kept apart
        self.assertEqual(q1["uses"], ["hydroelectricity", "flood control"])
        self.assertEqual(q1["country_name"], "China")
        self.assertEqual(out["Q2"]["name"], "Lipno")
        self.assertEqual(out["Q2"]["id"], "wikidata:Q2")


class CompanyTests(unittest.TestCase):
    E = "http://www.wikidata.org/entity/"
    DEP = "http://wikiba.se/ontology#DeprecatedRank"
    PREF = "http://wikiba.se/ontology#PreferredRank"
    NORMAL = "http://wikiba.se/ontology#NormalRank"

    def test_exchanges_and_listings(self):
        ex = companies.parse_exchanges([
            {"ex": self.E + "Q82059", "l": "Nasdaq", "mic": "XNAS", "coord": "Point(-73.98 40.75)", "cc": "US"},
            {"ex": self.E + "Q171240", "l": "London Stock Exchange", "mic": "", "hqcoord": "Point(-0.099 51.515)", "cc": "GB"},
            {"ex": self.E + "Q845397", "l": "A film", "mic": ""},
        ])
        self.assertEqual(set(ex), {"Q82059", "Q171240"})
        lst = companies.parse_listings([
            {"c": self.E + "Q1", "ex": self.E + "Q171240", "ticker": "abc", "rank": self.NORMAL},
            {"c": self.E + "Q1", "ex": self.E + "Q82059", "ticker": "ABCD", "rank": self.NORMAL},
            {"c": self.E + "Q1", "ex": self.E + "Q82059", "ticker": "ABCD", "rank": self.NORMAL},
            {"c": self.E + "Q1", "ex": self.E + "Q82059", "ticker": "OLD", "rank": self.DEP},
            {"c": self.E + "Q1", "ex": self.E + "Q845397", "ticker": "X", "rank": self.NORMAL},
        ], ex)
        self.assertEqual([(x["ticker"], x["mic"]) for x in lst["Q1"]], [("ABCD", "XNAS"), ("ABC", None)])
        self.assertEqual(companies.quote_symbol(lst["Q1"][0]), "ABCD:XNAS")

    def test_hq_and_money(self):
        hq = companies.pick_hq([
            {"c": self.E + "Q1", "hq": self.E + "Q60", "coord": "Point(-74.0 40.7)", "qcoord": "", "rank": self.NORMAL},
            {"c": self.E + "Q1", "hq": self.E + "Q60", "coord": "Point(-74.0 40.7)", "qcoord": "Point(-73.98 40.76)", "rank": self.NORMAL},
            {"c": self.E + "Q2", "hq": self.E + "Q90", "coord": "Point(2.35 48.85)", "rank": self.DEP},
        ])
        self.assertEqual(set(hq), {"Q1"})
        self.assertTrue(hq["Q1"]["precise"])
        money = companies.pick_money([
            {"c": self.E + "Q1", "k": "emp", "amt": "100", "t": "2020-01-01T00:00:00Z", "rank": self.NORMAL},
            {"c": self.E + "Q1", "k": "emp", "amt": "150", "t": "2024-01-01T00:00:00Z", "rank": self.NORMAL},
            {"c": self.E + "Q1", "k": "cap", "amt": "5e9", "cur": "EUR", "t": "2023-06-01T00:00:00Z", "rank": self.NORMAL},
            {"c": self.E + "Q1", "k": "cap", "amt": "7e9", "cur": "", "t": "2025-06-01T00:00:00Z", "rank": self.NORMAL},
            {"c": self.E + "Q1", "k": "rev", "amt": "2e9", "cur": "USD", "t": "2019-01-01T00:00:00Z", "rank": self.PREF},
            {"c": self.E + "Q1", "k": "rev", "amt": "3e9", "cur": "USD", "t": "2024-01-01T00:00:00Z", "rank": self.NORMAL},
        ])["Q1"]
        self.assertEqual(money["emp"]["amount"], 150)
        self.assertEqual((money["cap"]["amount"], money["cap"]["currency"]), (5e9, "EUR"))   # no currency, no value
        self.assertEqual(money["rev"]["amount"], 2e9)                                         # preferred rank wins
        rates = {"USD": 1.1, "GBP": 0.85, "EUR": 1.0}
        self.assertAlmostEqual(companies.to_usd(100, "EUR", rates), 110)
        self.assertAlmostEqual(companies.to_usd(85, "GBP", rates), 110)
        self.assertIsNone(companies.to_usd(100, "XYZ", rates))


class CableTests(unittest.TestCase):
    def test_slugs(self):
        self.assertEqual(cables.tg_slug("Congo, Dem. Rep."), "congo-dem-rep-")
        self.assertEqual(cables.tg_slug("Côte d'Ivoire"), "cte-divoire")
        self.assertEqual(cables.tg_slug("United States"), "united-states")
        self.assertEqual(cables.country_candidates("Muanda, Congo, Dem. Rep."), ["Dem. Rep.", "Congo, Dem. Rep."])

    def test_parse_and_index(self):
        cab = cables.parse_cable_geo(json.loads(fixture("cable_geo_sample.json")))
        self.assertEqual(sorted({c["id"] for c in cab}), ["alpha", "beta", "delta", "gamma"])
        self.assertEqual(next(c for c in cab if c["id"] == "gamma")["color"], "#9aa5c4")
        land = cables.parse_landing_geo(json.loads(fixture("landing_geo_sample.json")))
        docs = {"france": json.loads(fixture("tg_country_france.json")), "tunisia": json.loads(fixture("tg_country_tunisia.json")),
                "tonga": json.loads(fixture("tg_country_tonga.json"))}
        idx = cables.build_index(cab, land, docs, year_now=2026)
        cty = idx["countries"]
        self.assertEqual(cty["FR"]["cables"], ["alpha", "beta"])
        self.assertEqual((cty["TN"]["cables"], cty["TN"]["planned"]), (["alpha"], ["gamma"]))
        self.assertEqual((cty["IT"]["cables"], cty["IT"]["planned"]), (["beta"], ["gamma"]))   # no document: by name
        self.assertEqual(cty["FJ"]["cables"], ["delta"])
        self.assertEqual(cty["CD"]["cables"], [])
        self.assertTrue(idx["cables"]["gamma"]["is_planned"])
        self.assertEqual(idx["landings"]["marseille-france"]["cables"], ["alpha", "beta"])
        mids = cables.cable_midpoints({"type": "FeatureCollection", "features": [
            {"properties": {"id": "delta"}, "geometry": {"coordinates": [[[184.8, -21.13], [181.5, -19.5], [178.44, -18.14]]]}}]})
        self.assertAlmostEqual(mids["delta"][1], -178.5)   # longitudes past 180 are wrapped

    def test_build_and_details(self):
        env = Env()
        try:
            with mock.patch.object(cables, "MIN_CABLES", 1), mock.patch.object(cables, "MIN_LANDINGS", 1):
                cables.build(env.ds("cables"))
            geo = json.loads((env.m.data_dir / "cables.geojson").read_text(encoding="utf-8"))
            self.assertEqual(len(geo["features"]), 5)
            lands = json.loads((env.m.data_dir / "landings.geojson").read_text(encoding="utf-8"))
            mar = next(f for f in lands["features"] if f["properties"]["id"] == "marseille-france")["properties"]
            self.assertEqual((mar["k"], mar["d"]), (2, 2))
            requested = [u for u, _ in env.net.calls if "/country/" in u]
            self.assertIn("https://www.submarinecablemap.com/api/v3/country/congo-dem-rep-.json", requested)
            cache = env.m.cache_dir / "cables"
            d = cables.fetch_details(env.net, "alpha", cache)
            self.assertEqual(d["owners"], "Example Telecom, Other Carrier")
            n = len(env.net.calls)
            cables.fetch_details(env.net, "alpha", cache)
            self.assertEqual(len(env.net.calls), n)       # served from the 30-day cache
            self.assertIsNone(cables.fetch_details(env.net, "../etc", cache))
        finally:
            env.close()


# ---------------------------------------------------------------------------------------------
class ServiceTests(unittest.TestCase):
    """Builds every fixture dataset into a temp dir, loads it and checks the public behaviour."""

    @classmethod
    def setUpClass(cls):
        cls.env = Env()
        e = cls.env
        with mock.patch.multiple(power, MIN_PLANTS=1), mock.patch.multiple(airports, MIN_AIRPORTS=1), \
                mock.patch.multiple(ports, MIN_LOCODE=1), mock.patch.multiple(datacenters, MIN_FACILITIES=1), \
                mock.patch.multiple(cables, MIN_CABLES=1, MIN_LANDINGS=1):
            power.build(e.ds("power"))
            airports.build(e.ds("airports"))
            ports.build(e.ds("ports"))
            datacenters.build(e.ds("datacenters"))
            cables.build(e.ds("cables"))
        dam = dams.assemble([{"d": "http://www.wikidata.org/entity/Q9", "l": "Example Dam", "coord": "Point(-0.5 51.4)",
                              "cc": "GB"}], {}, {"Q9": 30.0}, {}, {}, {})
        common.write_json_atomic(e.m.data_dir / "dams-details.json", {"built_at": time.time(), "items": dam})
        co = [{"id": "wikidata:Q100", "qid": "Q100", "name": "Example Airlines plc", "lat": 51.47, "lon": -0.45,
               "hq_precise": True, "hq": "London", "country": "GB", "country_name": "United Kingdom",
               "listings": [{"exchange_qid": "Q171240", "exchange": "London Stock Exchange", "mic": "XLON", "ticker": "EXA",
                             "preferred": False}], "industries": ["airline"], "employees": 30000, "employees_year": 2024,
               "revenue": None, "market_cap": None, "website": None, "isins": ["GB0000000001"], "founded": 1990,
               "wikipedia": None, "wikidata": "https://www.wikidata.org/wiki/Q100"}]
        common.write_json_atomic(e.m.data_dir / "companies-details.json",
                                 {"built_at": time.time(), "items": co,
                                  "exchanges": {"Q171240": {"qid": "Q171240", "name": "London Stock Exchange", "mic": "XLON",
                                                            "lat": 51.515, "lon": -0.099, "country": "GB"}}})
        st = service.State(e.m)
        for fn in (st.load_power, st.load_airports, st.load_ports, st.load_datacenters, st.load_dams, st.load_cables,
                   st.load_companies):
            fn()
        cls.st = st

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def test_counts_and_status(self):
        self.assertEqual(self.st.counts(), {"power": 4, "airports": 4, "ports": 7, "datacenters": 4, "dams": 1,
                                            "cables": 4, "landings": 6, "companies": 1})
        self.assertTrue(self.env.statuses["infra.gppd"]["ok"])
        self.assertFalse(self.env.statuses["infra.wpi"]["ok"])

    def test_search(self):
        hit = self.st.search("LHR", 10)[0]
        self.assertEqual((hit["kind"], hit["id"], hit["layer"]), ("airport", "EGLL", "infra.airports"))
        self.assertEqual(self.st.search("heathrow", 5)[0]["id"], "EGLL")
        self.assertEqual(self.st.search("southampton", 5)[0]["kind"], "port")
        self.assertEqual(self.st.search("Alpha Cable", 5)[0]["id"], "tg:alpha")
        self.assertEqual(self.st.search("EXA", 5)[0]["kind"], "company")
        self.assertEqual(self.st.search("Example Coal", 5)[0]["kind"], "power_plant")

    def test_nearby(self):
        items = self.st.nearby(51.47, -0.46, 20, 10)
        kinds = {i["kind"] for i in items}
        self.assertIn("airport", kinds)
        self.assertIn("company", kinds)
        self.assertTrue(all(i["distance_km"] <= 20 for i in items))
        wide = self.st.nearby(51.47, -0.46, 150, 10)
        self.assertNotIn("GB-0001", {i["id"] for i in wide})   # small airfields skipped at wide radius

    def test_country_summary_contract(self):
        gb = self.st.country("GB")
        for key in ("cables", "cable_names", "landing_points", "power_plants", "capacity_mw", "airports", "ports",
                    "datacenters"):
            self.assertIn(key, gb)
        self.assertEqual((gb["power_plants"], gb["airports"], gb["ports"]), (2, 4, 3))
        self.assertEqual(gb["capacity_mw"], {"Coal": 3906, "Wind": 12.5})
        fr = self.st.country("FR")
        self.assertEqual((fr["cables"], fr["cable_names"], fr["landing_points"]), (2, ["Alpha Cable", "Beta Link"], 1))
        self.assertEqual(fr["cable_dependency"], "high")
        self.assertEqual(self.st.country("TN")["planned_cables"], ["Gamma Planned"])
        table = {r["country"]: r for r in self.st.countries_table()}
        self.assertTrue(table["FR"]["low"])
        self.assertFalse(table["CD"]["low"])

    def test_details(self):
        a = self.st.airport_detail("LHR")
        self.assertEqual(a["id"], "EGLL")
        self.assertEqual(a["entity"]["kind"], "airport")
        self.assertEqual(len(a["runways"]), 2)
        self.assertIsNone(self.st.airport_detail("NOPE"))
        self.assertEqual(self.st.power_detail("GBR0000001")["fuel"], "coal")
        dc = self.st.datacenter_detail("peeringdb:1")
        self.assertEqual([c["id"] for c in dc["campus"]], ["peeringdb:2"])
        c = self.st.cable_detail("tg:alpha", fetch=False)
        self.assertEqual([p["id"] for p in c["landing_points"]], ["tg:marseille-france", "tg:bizerte-tunisia"])
        self.assertTrue(all(x["low"] for x in c["countries"]))
        self.assertIn("bbox", c)
        lp = self.st.landing_detail("tg:marseille-france")
        self.assertEqual((lp["country_cables"], lp["low_dependency"]), (2, True))
        co = self.st.company_detail("wikidata:Q100")
        self.assertEqual(co["listings"][0]["quote_symbol"], "EXA:XLON")
        self.assertEqual(co["listings"][0]["exchange_lat"], 51.515)

    def test_ai_tools(self):
        near = self.st.ai_near({"lat": 51.47, "lon": -0.46, "radius_km": 30, "kinds": ["airport"]})
        self.assertTrue(near["results"])
        self.assertTrue(all(r["kind"] == "airport" for r in near["results"]))
        self.assertIn("error", self.st.ai_near({"lat": 200, "lon": 0}))
        ap = self.st.ai_airport({"code": "LHR"})
        self.assertTrue(ap["found"])
        self.assertEqual(ap["metar"]["station"], "EGLL")
        self.assertEqual(ap["liveatc"], "https://www.liveatc.net/search/?icao=EGLL")
        cab = self.st.ai_cable({"query": "alpha"})
        self.assertEqual(cab["owners"], "Example Telecom, Other Carrier")
        self.assertFalse(self.st.ai_cable({"query": "no such cable at all"})["found"])
        co = self.st.ai_company({"query": "example airlines"})
        self.assertEqual(co["name"], "Example Airlines plc")


if __name__ == "__main__":
    unittest.main()
