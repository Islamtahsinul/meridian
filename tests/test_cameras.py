"""Cameras module: adapters (one small fixture each), normalisation, catalog search and geography,
the dataset build (independent failures, allowlists, de-duplication) and the HTTP routes.

No network: fixtures live in tests/fixtures/cameras/, HTTP goes through fakes.
"""

from __future__ import annotations

import gzip
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from meridian.modules.cameras import build as build_mod
from meridian.modules.cameras import common
from meridian.modules.cameras.catalog import Catalog, brief, entity, subtitle
from meridian.modules.cameras.common import NeedsKey, Source, make_camera
from meridian.modules.cameras.sources import (alaska, alertca, autobahn, caltrans, cities, dgt, digitraffic, drivebc,
                                              hongkong, ibi511, iceland, illinois, iowa, nyc, nzta, singapore, tfl,
                                              windy)
from meridian.net import NetResponse

FIX = Path(__file__).parent / "fixtures" / "cameras"


def fx_json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def fx_bytes(name):
    return (FIX / name).read_bytes()


def ids(rows):
    return [r["id"] for r in rows]


class FakeDS:
    """Enough of a BuildContext for build.build()."""

    def __init__(self, root: Path):
        self.out_dir = root / "build"
        self.raw_dir = root / "raw"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.force = False
        self.max_age_hours = 24.0
        self.rows = None
        self.notes: list[str] = []
        self.sources: list[str] = []
        self.net = None
        self.log = mock.Mock()
        self.config = None

    def raw(self, name):
        p = self.raw_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_json(self, name, obj):
        p = self.out_dir / name
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p

    def set_rows(self, n):
        self.rows = n

    def note(self, text):
        self.notes.append(text)

    def source(self, url):
        self.sources.append(url)


# ---------------------------------------------------------------------------------------------
class CommonTests(unittest.TestCase):
    def test_clean_and_ids(self):
        self.assertEqual(common.clean("  a\tb\n c "), "a b c")
        self.assertEqual(common.clean(None), "")
        self.assertEqual(common.sanitize_id("JamCams 00001/06514?x"), "JamCams_00001_06514_x")
        self.assertNotIn("/", common.sanitize_id("../../etc/passwd"))
        self.assertEqual(common.title_case("SANTA CRUZ DE TENERIFE"), "Santa Cruz de Tenerife")
        self.assertEqual(common.title_case("lassen"), "Lassen")
        self.assertEqual(common.title_case("Already Mixed"), "Already Mixed")

    def test_headings(self):
        h = common.heading_from
        self.assertEqual(h("N"), 0)
        self.assertEqual(h("SB"), 180)
        self.assertEqual(h("Westbound"), 270)
        self.assertEqual(h("looking north-east"), 45)
        self.assertEqual(h("séð til vesturs"), 270)
        self.assertEqual(h(123), 123)
        self.assertIsNone(h("Eastcheap"))
        self.assertIsNone(h("Austurvegur"))
        self.assertIsNone(h("Piccadilly Circus"))
        self.assertEqual(common.compass_label(44), "NE")
        self.assertEqual(common.compass_label(359), "N")

    def test_http_url(self):
        self.assertEqual(common.http_url("/camera/1.jpg", "https://x.test"), "https://x.test/camera/1.jpg")
        self.assertIsNone(common.http_url("javascript:alert(1)"))
        self.assertIsNone(common.http_url("file:///etc/passwd"))
        self.assertIsNone(common.http_url("https://user:pw@x.test/a.jpg"))
        self.assertEqual(common.http_url("https://x.test/a b.jpg"), "https://x.test/a%20b.jpg")

    def test_make_camera_rules(self):
        src = Source(id="t", name="T", owner="o", url="u", licence="L", attribution="A", country="GB", refresh=60)
        self.assertIsNone(make_camera(src, "1", "x", None, 1, image="https://a/1.jpg"))       # no position
        self.assertIsNone(make_camera(src, "1", "x", 0, 0, image="https://a/1.jpg"))          # null island
        self.assertIsNone(make_camera(src, "1", "x", 91, 1, image="https://a/1.jpg"))         # out of range
        self.assertIsNone(make_camera(src, "1", "x", 51, 0.1))                                # nothing to show
        row = make_camera(src, "1", "Harbour Bridge North", 51.123456789, -0.1, image="https://a/1.jpg",
                          direction="Northbound", refresh=5)
        self.assertEqual(row["id"], "t:1")
        self.assertEqual(row["lat"], 51.12346)
        self.assertEqual(row["kind"], "image")
        self.assertEqual(row["heading"], 0)
        self.assertEqual(row["refresh"], 15)                    # clamped
        self.assertIn("port", row["tags"])
        self.assertIn("bridge", row["tags"])
        self.assertEqual(row["licence"], "L")
        hls = make_camera(src, "2", "x", 51, 0.1, image="https://a/2.jpg", stream="https://a/2/playlist.m3u8")
        self.assertEqual(hls["kind"], "hls")
        page = make_camera(src, "3", "x", 51, 0.1, page="https://a/3")
        self.assertEqual(page["kind"], "page")

    def test_xml_with_dtd_is_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
        with self.assertRaises(ValueError):
            common.parse_xml(bomb)

    def test_gzip_bomb_is_stopped_at_the_limit(self):
        # ~10 KB on disk that would inflate to 10 MB: refused without inflating it all into memory
        bomb = gzip.compress(b"\x00" * 10_000_000)
        with self.assertRaises(ValueError):
            common.gunzip_limited(bomb, 1_000_000)
        with self.assertRaises(ValueError):
            common.parse_json_bytes(bomb, limit=1_000_000)
        tmp = Path(tempfile.mkdtemp())
        try:
            path = tmp / "cams.json.gz"
            path.write_bytes(bomb)
            with self.assertRaises(ValueError):
                common.read_bytes(path, limit=1_000_000)
            path.write_bytes(b"x" * 2000)                        # plain files over the limit too
            with self.assertRaises(ValueError):
                common.read_bytes(path, limit=1000)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gzip_files_still_read(self):
        doc = {"cameras": [1, 2, 3]}
        raw = json.dumps(doc).encode()
        self.assertEqual(common.parse_json_bytes(gzip.compress(raw)), doc)
        self.assertEqual(common.gunzip_limited(gzip.compress(b"ab") + gzip.compress(b"cd"), 100), b"abcd")   # two members
        with self.assertRaises(EOFError):
            common.gunzip_limited(gzip.compress(raw)[:-12], 1000)                                    # truncated
        tmp = Path(tempfile.mkdtemp())
        try:
            path = tmp / "cams.json"
            path.write_bytes(gzip.compress(raw))
            self.assertEqual(common.read_json(path), doc)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
class AdapterTests(unittest.TestCase):
    def test_tfl(self):
        rows, skipped = tfl.parse(fx_json("tfl.json"), tfl.SOURCE)
        self.assertEqual((len(rows), skipped), (2, 1))
        r = rows[0]
        self.assertTrue(r["id"].startswith("tfl:0000"))
        self.assertEqual((r["kind"], r["country"], r["region"]), ("video", "GB", "London"))
        self.assertTrue(r["image"].endswith(".jpg") and r["stream"].endswith(".mp4"))
        self.assertIn("urban", r["tags"])

    def test_caltrans(self):
        rows, skipped = caltrans.parse(fx_json("caltrans_d07.json"), 7, caltrans.SOURCE)
        self.assertEqual((len(rows), skipped), (2, 1))
        kinds = sorted(r["kind"] for r in rows)
        self.assertEqual(kinds, ["hls", "image"])
        self.assertTrue(all(r["id"].startswith("caltrans:d7-") for r in rows))
        self.assertTrue(all("previous" not in r["image"] for r in rows))   # never history images
        self.assertEqual(caltrans.split_name("I-110 : (196) Avenue 26 Off Ramp"), ("I-110", "Avenue 26 Off Ramp"))

    def test_digitraffic(self):
        stations = fx_json("fi_stations.json")
        detail = fx_json("fi_detail.json")
        sid = stations["features"][0]["id"]
        rows, _ = digitraffic.parse(stations, {sid: detail}, digitraffic.SOURCE)
        first = [r for r in rows if r["description"] == f"Station {sid}"]
        self.assertTrue(first)
        self.assertTrue(first[0]["image"].startswith("https://weathercam.digitraffic.fi/"))
        self.assertIn("weather", first[0]["tags"])
        self.assertEqual(digitraffic.name_from_code("kt51_Inkoo"), ("Road 51 Inkoo", "Road 51"))
        # without details, names come from the station code
        rows2, _ = digitraffic.parse(stations, {}, digitraffic.SOURCE)
        self.assertTrue(all(r["name"].startswith("Road ") or r["name"] for r in rows2))

    def test_hongkong(self):
        rows, _ = hongkong.parse(fx_bytes("hk.xml"), hongkong.SOURCE)
        self.assertEqual(len(rows), 2)
        self.assertNotIn("[", rows[0]["name"])
        self.assertEqual(rows[0]["country"], "HK")

    def test_nzta(self):
        rows, skipped = nzta.parse(fx_bytes("nz.xml"), nzta.SOURCE)
        self.assertEqual((len(rows), skipped), (2, 1))
        self.assertTrue(rows[0]["image"].startswith("https://www.trafficnz.info/camera/"))
        self.assertIn("heading", rows[0])

    def test_dgt(self):
        rows, _ = dgt.parse(fx_bytes("dgt.xml"), dgt.SOURCE)
        self.assertEqual(len(rows), 2)
        self.assertIn(" km ", rows[0]["name"])
        self.assertTrue(rows[0]["image"].startswith("https://etraffic.dgt.es/"))
        self.assertEqual(rows[0]["country"], "ES")

    def test_illinois_and_lake_county(self):
        feats = fx_json("illinois.json")["features"] + fx_json("lakecounty.json")["features"]
        il_rows, _ = illinois.parse(feats, illinois.SOURCE)
        lake_rows, _ = illinois.parse(feats, illinois.LAKE_COUNTY)
        self.assertEqual(len(il_rows), 3)
        self.assertEqual(len(lake_rows), 2)
        self.assertTrue(all("lakecountypassage" not in r["image"] for r in il_rows))
        self.assertTrue(all("lakecountypassage" in r["image"] for r in lake_rows))
        self.assertTrue(il_rows[0]["page"].startswith("https://travelmidwest.com/"))

    def test_iowa(self):
        rows, _ = iowa.parse(fx_json("iowa.json")["features"], iowa.SOURCE)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(set(ids(rows))), 4)            # RWIS views do not collide
        rwis = [r for r in rows if "weather" in r["tags"]]
        self.assertEqual(len(rwis), 2)
        self.assertIn(", ", rwis[0]["name"])

    def test_drivebc(self):
        rows, skipped = drivebc.parse(fx_json("drivebc.json"), drivebc.SOURCE)
        self.assertEqual((len(rows), skipped), (2, 1))
        self.assertNotIn("?", rows[0]["image"])              # cache buster removed
        self.assertTrue(rows[0]["road"].startswith("Highway"))

    def test_alertca(self):
        rows, _ = alertca.parse(fx_json("alertca.json")["features"], alertca.SOURCE)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["image"].endswith("latest-frame.jpg"))
        self.assertIn("wildfire", rows[0]["tags"])

    def test_iceland(self):
        rows, _ = iceland.parse(fx_json("iceland.json"), iceland.SOURCE)
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0]["road"].startswith("Road "))
        self.assertIn("mountain", rows[0]["tags"])

    def test_alaska_is_dynamic(self):
        rows, skipped = alaska.parse(fx_json("alaska.json")["features"], alaska.SOURCE)
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(skipped, 1)
        self.assertTrue(all(r.get("dynamic") and "image" not in r for r in rows))

        class Net:
            def get_json(self, url, **kw):
                self.url = url
                return {"features": [{"attributes": {"cameras_0_latestImage": "https://dotdeswus2prodrwissto01.blob.core.windows.net/images/x.jpg"}}]}

        net = Net()
        site = rows[0]["id"].split(":")[1].split("-")[0]
        url = alaska.resolve({"id": f"alaska:{site}-0"}, net)
        self.assertTrue(url.endswith("/x.jpg"))
        self.assertIn(f"properties_siteId%3D{site}", net.url)
        self.assertIsNone(alaska.resolve({"id": "alaska:../../x"}, net))

    def test_cities(self):
        self.assertEqual(len(cities.parse_york(fx_json("york.json")["features"], cities.YORK)[0]), 2)
        rows, skipped = cities.parse_austin(fx_json("austin.json"), cities.AUSTIN)
        self.assertEqual((len(rows), skipped), (2, 1))
        self.assertEqual(len(cities.parse_calgary(fx_json("calgary.json"), cities.CALGARY)[0]), 2)
        nl_rows = cities.parse_nl(fx_json("nl.json")["features"], cities.NEWFOUNDLAND)[0]
        self.assertEqual(len(nl_rows), 2)
        self.assertTrue(all(r["image"].startswith("https://www.gov.nl.ca/ti/highway-cams/") for r in nl_rows))

    def test_singapore_resolver(self):
        data = fx_json("singapore.json")
        rows, _ = singapore.parse(data, singapore.SOURCE)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].get("dynamic"))
        net = mock.Mock()
        net.get_json.return_value = data
        want = singapore.cameras_of(data)[1]
        self.assertEqual(singapore.resolve({"id": f"sg:{want['camera_id']}"}, net), want["image"])
        self.assertIsNone(singapore.resolve({"id": "sg:nope"}, net))

    def test_autobahn_nyc_and_511_shapes(self):
        # synthetic records in each API's documented shape
        ab = {"webcam": [{"identifier": "V0VCQ0FNX19OUldfX0ExX18x", "title": "A1 | AS Hagen-Nord", "subtitle": "Blickrichtung Dortmund",
                          "coordinate": {"lat": "51.40", "long": "7.48"}, "imageurl": "https://www.autobahn.de/cam/1.jpg",
                          "linkurl": "https://www.autobahn.de/cam/1"}]}
        rows, _ = autobahn.parse(ab, "A1", autobahn.SOURCE)
        self.assertEqual(rows[0]["road"], "A1")
        ny = [{"id": "053e8995-f8cb-4d02-a659-70ac7c7da5db", "name": "1 Ave @ 110 St", "latitude": 40.79, "longitude": -73.94,
               "area": "Manhattan", "isOnline": "true"}, {"id": "x", "name": "Off", "latitude": 40.7, "longitude": -73.9, "isOnline": "false"}]
        rows, skipped = nyc.parse(ny, nyc.SOURCE)
        self.assertEqual((len(rows), skipped), (1, 1))
        self.assertTrue(rows[0]["image"].endswith("/image"))
        ibi = [{"Id": 7, "Roadway": "Highway 401", "Direction": "Eastbound", "Latitude": 43.7, "Longitude": -79.4,
                "Location": "401 at Keele", "Views": [{"Id": 11, "Url": "https://511on.ca/map/Cctv/11", "Status": "Enabled", "Description": "East"},
                                                    {"Id": 12, "Url": "https://511on.ca/map/Cctv/12", "Status": "Disabled", "Description": "West"}]}]
        rows, skipped = ibi511.parse(ibi, ibi511.ONTARIO)
        self.assertEqual((len(rows), skipped), (1, 1))
        self.assertEqual(rows[0]["heading"], 90)

    def test_keyed_sources_need_keys(self):
        ds = FakeDS(Path(tempfile.mkdtemp()))
        try:
            for src in (windy.SOURCE, ibi511.ONTARIO, ibi511.ALBERTA, ibi511.NEW_YORK):
                fc = common.FetchContext(ds, src, config=mock.Mock(get=lambda name, default=None: None))
                with self.assertRaises(NeedsKey):
                    src.fetch(fc)
        finally:
            shutil.rmtree(ds.out_dir.parent, ignore_errors=True)

    def test_every_source_is_complete(self):
        from meridian.modules.cameras import sources
        seen = set()
        for s in sources.ALL:
            self.assertNotIn(s.id, seen)
            seen.add(s.id)
            self.assertRegex(s.id, r"^[a-z0-9]{1,16}$")
            self.assertTrue(s.name and s.owner and s.url and s.licence and s.attribution, s.id)
            self.assertTrue(s.hosts, s.id)
            self.assertTrue(callable(s.fetch), s.id)
        self.assertGreaterEqual(len([s for s in sources.ALL if not s.needs_key]), 18)


# ---------------------------------------------------------------------------------------------
def sample_rows():
    s = Source(id="t", name="Test feed", owner="Owner", url="https://t.test", licence="CC BY 4.0", attribution="Owner",
               country="GB", region="London", hosts=("img.t.test",), refresh=60, tags=("traffic", "urban"))
    rows = [
        make_camera(s, "1", "Piccadilly Circus", 51.5096, -0.13484, image="https://img.t.test/1.jpg"),
        make_camera(s, "2", "Piccadilly / Down St", 51.5065, -0.1446, image="https://img.t.test/2.jpg", heading="W"),
        make_camera(s, "3", "A40 Westway", 51.52, -0.2, image="https://img.t.test/3.jpg", road="A40"),
        make_camera(s, "4", "A406 Ace Cafe", 51.54, -0.27, image="https://img.t.test/4.jpg", road="A406"),
        make_camera(s, "5", "Hellisheiði, facing west", 64.0183, -21.3426, image="https://img.t.test/5.jpg",
                    country="IS", region="Iceland", tags=("mountain",)),
        make_camera(s, "6", "Bay Bridge", 37.8, -122.37, image="https://img.t.test/6.jpg", stream="https://v.t.test/6.m3u8",
                    country="US", region="California"),
    ]
    return s, rows


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.src, rows = sample_rows()
        self.cat = Catalog(rows, {"t": {"name": "Test feed"}}, time.time())

    def test_search_ranking(self):
        hits = self.cat.search("piccadilly", 5)
        self.assertEqual(hits[0][1]["name"], "Piccadilly Circus")
        self.assertEqual(len(hits), 2)
        a40 = self.cat.search("A40", 5)
        self.assertEqual(a40[0][1]["id"], "t:3")                     # exact token beats the A406 prefix
        self.assertEqual(self.cat.search("hellisheidi", 3)[0][1]["id"], "t:5")   # accents folded
        self.assertEqual(self.cat.search("iceland", 3)[0][1]["id"], "t:5")       # country names indexed
        self.assertEqual(self.cat.search("camera", 3), [])
        self.assertEqual(self.cat.search("x", 3), [])

    def test_geography(self):
        near = self.cat.near(51.5096, -0.135, 2, 10)
        self.assertEqual([r["id"] for _, r in near][:1], ["t:1"])
        live_only = self.cat.near(37.8, -122.37, 5, 10, keep=lambda r: r["kind"] == "hls")
        self.assertEqual(len(live_only), 1)
        total, rows = self.cat.in_bbox((-0.3, 51.4, 0.0, 51.6), center=(51.5096, -0.13484), limit=2)
        self.assertEqual(total, 4)
        self.assertEqual(rows[0]["id"], "t:1")
        total, _ = self.cat.in_bbox((170.0, -50.0, -170.0, 70.0))   # across the antimeridian
        self.assertEqual(total, 0)

    def test_entity_shapes(self):
        row = self.cat.get("t:6")
        e = entity(row, score=0.5)
        self.assertEqual((e["kind"], e["layer"], e["id"]), ("camera", "cameras", "t:6"))
        self.assertIn("live video", e["subtitle"])
        b = brief(self.cat.get("t:2"))
        self.assertEqual((b["compass"], b["img_url"]), ("W", "/api/cameras/img/t:2"))
        self.assertEqual(subtitle(self.cat.get("t:3")), "London, United Kingdom")
        counts = self.cat.counts()
        self.assertEqual(counts["by_kind"], {"image": 5, "hls": 1})


# ---------------------------------------------------------------------------------------------
class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ds = FakeDS(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _src(self, sid, fetch, **kw):
        return Source(id=sid, name=sid.upper(), owner="o", url=f"https://{sid}.test", licence="L", attribution="A",
                      fetch=fetch, hosts=(f"img.{sid}.test",), country="GB", **kw)

    def test_independent_failures_keep_previous_rows(self):
        good = self._src("good", None)
        good.fetch = lambda fc: [make_camera(good, "1", "One", 51.5, -0.1, image="https://img.good.test/1.jpg"),
                                 make_camera(good, "2", "Two", 51.5, -0.2, image="https://img.good.test/2.jpg")]
        flaky = self._src("flaky", None)
        flaky.fetch = lambda fc: [make_camera(flaky, "9", "Nine", 52.0, 1.0, image="https://img.flaky.test/9.jpg")]
        keyed = self._src("keyed", None, needs_key="KEYED_KEY")

        def needs(fc):
            raise NeedsKey("KEYED_KEY")

        keyed.fetch = needs
        build_mod.build(self.ds, [good, flaky, keyed], None, mock.Mock())
        first = json.loads((self.ds.out_dir / "cameras.json").read_text())
        self.assertEqual(first["count"], 3)

        def broken(fc):
            raise RuntimeError("upstream down")

        flaky.fetch = broken
        doc = build_mod.build(self.ds, [good, flaky, keyed], None, mock.Mock())
        self.assertEqual(doc["count"], 3)                                   # flaky kept its previous row
        st = doc["sources"]
        self.assertTrue(st["good"]["ok"])
        self.assertFalse(st["flaky"]["ok"])
        self.assertTrue(st["flaky"]["stale"])
        self.assertIn("upstream down", st["flaky"]["error"])
        self.assertIsNone(st["keyed"]["ok"])
        self.assertEqual(st["keyed"]["note"], "Needs KEYED_KEY")
        geo = json.loads((self.ds.out_dir / "cameras.geojson").read_text())
        self.assertEqual(len(geo["features"]), 3)
        self.assertEqual(self.ds.rows, 3)

    def test_allowlist_dedupe_and_empty(self):
        a = self._src("a", None)
        b = self._src("b", None)
        a.fetch = lambda fc: [make_camera(a, "1", "One", 51.5, -0.1, image="https://img.a.test/1.jpg"),
                              make_camera(a, "2", "Evil", 51.5, -0.1, image="http://169.254.169.254/latest.jpg")]
        b.fetch = lambda fc: [make_camera(b, "1", "Copy", 51.5, -0.1, image="https://img.a.test/1.jpg")]
        b.hosts = ("img.a.test",)
        doc = build_mod.build(self.ds, [a, b], None, mock.Mock())
        self.assertEqual([r["id"] for r in doc["cameras"]], ["a:1"])     # bad host dropped, duplicate image dropped
        empty = self._src("e", lambda fc: [])
        with self.assertRaises(RuntimeError):
            build_mod.build(FakeDS(self.tmp / "other"), [empty], None, mock.Mock())

    def test_disabled_sources_and_fan_out(self):
        a = self._src("a", None)
        a.fetch = lambda fc: [make_camera(a, str(i), f"View {i}", 64.0, -21.0, image=f"https://img.a.test/{i}.jpg",
                                          heading=h) for i, h in enumerate((90, 270, None))]
        off = self._src("off", lambda fc: self.fail("disabled source ran"))
        cfg = mock.Mock()
        cfg.get = lambda name, default=None: "off" if name == "CAMERAS_DISABLE" else default
        cfg.redact = lambda t: t
        doc = build_mod.build(self.ds, [a, off], cfg, mock.Mock())
        self.assertIn("CAMERAS_DISABLE", doc["sources"]["off"]["note"])
        geo = json.loads((self.ds.out_dir / "cameras.geojson").read_text())
        pts = {f["properties"]["id"]: f["geometry"]["coordinates"] for f in geo["features"]}
        self.assertEqual(len(set(map(tuple, pts.values()))), 3)          # co-located views fanned out
        self.assertGreater(pts["a:0"][0], pts["a:1"][0])                 # east-facing view drawn east of west-facing
        self.assertTrue(all(r["lat"] == 64.0 for r in doc["cameras"]))  # true positions untouched

    def test_dedupe_keeps_query_strings_that_name_a_picture(self):
        a = self._src("a", None)
        a.hosts = ("img.a.test",)
        rows = [make_camera(a, "1", "Buoy 1", 30.0, -80.0, image="https://img.a.test/buoycam.php?station=41004"),
                make_camera(a, "2", "Buoy 2", 31.0, -80.0, image="https://img.a.test/buoycam.php?station=41009"),
                make_camera(a, "3", "Same as 1, cache buster", 30.0, -80.0, image="https://IMG.a.test/buoycam.php?station=41004&t=17954"),
                make_camera(a, "4", "Other case path", 32.0, -80.0, image="https://img.a.test/Buoycam.php?station=41004")]
        kept, dropped = build_mod.dedupe(rows)
        self.assertEqual([r["id"] for r in kept], ["a:1", "a:2", "a:4"])
        self.assertEqual(dropped, 1)
        self.assertEqual(build_mod.image_key("https://X.test/a.jpg?b=2&a=1&_=99#frag"), "https://x.test/a.jpg?a=1&b=2")

    def test_group_labels(self):
        self.assertEqual(build_mod.group_label(["Hobson / Woodridge, facing north", "Hobson / Woodridge, facing east"]),
                         "Hobson / Woodridge")
        self.assertEqual(build_mod.group_label(["Road 51 Inkoo, Hankoon", "Road 51 Inkoo, Inkooseen"]), "Road 51 Inkoo")
        self.assertEqual(build_mod.group_label(["Coquihalla Summit - N", "Coquihalla Summit - S"]), "Coquihalla Summit")
        self.assertEqual(build_mod.group_label(["A", "B"]), "A")

    def test_short_errors(self):
        err = Exception("webcams.nyctmc.org: [WinError 10060] A connection attempt failed because...")
        self.assertEqual(build_mod.short_error(err), "webcams.nyctmc.org: timed out")


# ---------------------------------------------------------------------------------------------
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class FakeNet:
    """Records requests; answers image URLs with a JPEG, everything else with 404."""

    def __init__(self):
        self.calls = []
        self.stats = {}

    def request(self, url, **kw):
        self.calls.append(url)
        return NetResponse(200, {"content-type": "image/jpeg", "last-modified": "Sun, 22 Nov 2026 21:16:54 GMT"},
                           JPEG, url)

    def get_json(self, url, **kw):
        raise AssertionError("no JSON lookups expected")

    def download(self, *a, **kw):
        raise AssertionError("no downloads in route tests")

    def set_host_interval(self, *a):
        pass


class RouteTests(unittest.TestCase):
    """The module's HTTP routes, search, nearby and AI tools on a real App with a fake network."""

    @classmethod
    def setUpClass(cls):
        from meridian.config import Config
        from meridian.core_api import register_core
        from meridian.datasets import DatasetManager
        from meridian.modules import cameras
        from meridian.registry import App
        from meridian.scheduler import Scheduler
        from meridian.store import Store

        cls.tmp = Path(tempfile.mkdtemp())
        config = Config(env_file=None, overrides={"MERIDIAN_DATA_DIR": str(cls.tmp)})
        cls.net = FakeNet()
        cls.app = App(config, Store(cls.tmp / "meridian.db"), cls.net, mock.Mock(), Scheduler(), DatasetManager())
        register_core(cls.app)
        m = cls.app.module("cameras")
        cameras.register(m)
        # a tiny built dataset from the TfL fixture
        rows, _ = tfl.parse(fx_json("tfl.json"), tfl.SOURCE)
        m.data_dir.mkdir(parents=True, exist_ok=True)
        doc = {"version": 1, "built_at": time.time(), "count": len(rows),
               "sources": {"tfl": {**build_mod.source_meta(tfl.SOURCE), "ok": True, "count": len(rows)}}, "cameras": rows}
        (m.data_dir / "cameras.json").write_text(json.dumps(doc), encoding="utf-8")
        (m.data_dir / "cameras.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
        for fn in cls.app.datasets.items["cameras.cameras"].listeners:
            fn()
        cls.rows = rows

    @classmethod
    def tearDownClass(cls):
        cls.app.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, **query):
        from meridian.registry import HTTPError, Request, Response
        route, params, _ = self.app.match("GET", path)
        self.assertIsNotNone(route, path)
        req = Request("GET", path, params, {k: [str(v)] for k, v in query.items()}, {})
        try:
            out = route.handler(req)
        except HTTPError as err:
            return err.status, {"error": {"code": err.code}}
        if isinstance(out, Response):
            return out.status, out
        return 200, out

    def test_camera_detail_and_missing(self):
        cid = self.rows[0]["id"]
        status, body = self.get(f"/api/cameras/camera/{cid}")
        self.assertEqual(status, 200)
        self.assertEqual(body["camera"]["img_url"], f"/api/cameras/img/{cid}")
        self.assertTrue(body["camera"]["playable"])
        self.assertEqual(body["source"]["name"], "TfL JamCams")
        status, body = self.get("/api/cameras/camera/tfl:gone")
        self.assertEqual((status, body["missing"]), (200, True))
        status, body = self.get("/api/cameras/camera/not an id")
        self.assertEqual(status, 400)

    def test_image_proxy_route(self):
        cid = self.rows[0]["id"]
        with mock.patch("meridian.modules.cameras.proxy.resolve_host", return_value=["93.184.216.34"]):
            status, resp = self.get(f"/api/cameras/img/{cid}")
            self.assertEqual(status, 200)
            self.assertEqual(resp.content_type, "image/jpeg")
            self.assertIn("max-age=", resp.cache)
            self.assertIn("X-Camera-Image-Time", resp.headers)
            calls = len(self.net.calls)
            self.get(f"/api/cameras/img/{cid}")
            self.assertEqual(len(self.net.calls), calls)           # served from the short cache
        status, _ = self.get("/api/cameras/img/tfl:gone")
        self.assertEqual(status, 404)
        status, _ = self.get("/api/cameras/img/..")
        self.assertEqual(status, 400)
        # fallback=1: a placeholder image with the reason in headers instead of an error status
        status, resp = self.get("/api/cameras/img/tfl:gone", fallback=1)
        self.assertEqual((status, resp.content_type), (200, "image/svg+xml"))
        self.assertEqual(resp.headers["X-Camera-Error"], "NOT_FOUND")
        self.assertEqual(resp.headers["X-Camera-Status"], "404")
        self.assertEqual(resp.cache, "no-store")
        self.assertNotIn(b"<script", resp.body)

    def test_lists_summary_search_nearby_ai(self):
        status, body = self.get("/api/cameras/list", ids=",".join([self.rows[0]["id"], "tfl:gone"]))
        self.assertEqual([c.get("missing", False) for c in body["cameras"]], [False, True])
        status, body = self.get("/api/cameras/list", bbox="-0.6,51.2,0.4,51.8", limit=1)
        self.assertEqual((body["count"], body["total"]), (1, 2))
        status, body = self.get("/api/cameras/list", lat=51.5, lon=-0.1, radius_km=50)
        self.assertEqual(body["count"], 2)
        self.assertIn("distance_km", body["cameras"][0])
        status, body = self.get("/api/cameras/list", bbox="-1,51,1,52", kind="nonsense")
        self.assertEqual(status, 400)
        status, body = self.get("/api/cameras/summary")
        self.assertTrue(body["ready"])
        self.assertEqual(body["count"], 2)
        by_id = {s["id"]: s for s in body["sources"]}
        self.assertEqual(by_id["tfl"]["count"], 2)
        self.assertEqual(by_id["windy"]["note"], "Needs WINDY_WEBCAMS_KEY")
        name = self.rows[0]["name"]
        found = self.app.run_search(name, 5)
        self.assertEqual(found[0]["kind"], "camera")
        near = self.app.run_nearby(self.rows[0]["lat"], self.rows[0]["lon"], 2, 5)
        self.assertEqual(near[0]["id"], self.rows[0]["id"])
        out = self.app.call_ai_tool("cameras_near", {"lat": self.rows[0]["lat"], "lon": self.rows[0]["lon"], "radius_km": 5})
        self.assertGreaterEqual(out["count"], 1)
        self.assertIn("image_url", out["cameras"][0])
        self.assertIn("error", self.app.call_ai_tool("cameras_near", {"lat": "x"}))
        out = self.app.call_ai_tool("cameras_search", {"query": name})
        self.assertEqual(out["cameras"][0]["id"], self.rows[0]["id"])
        self.assertIn("error", self.app.call_ai_tool("cameras_search", {"query": "a"}))


if __name__ == "__main__":
    unittest.main()
