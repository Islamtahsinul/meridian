"""Cameras, global pieces: the Windy crawl (per country, split into areas above 1,000 webcams,
cached pages, budgets, carry-over) and its compliance (fresh links from the server, never proxied,
the key only in a header), the worldwide public networks (USGS AshCam, NOAA buoys, NPS), the lean
map file and the catalog at 100,000+ cameras (bbox ranking, search, labels, "Show" counts).

No network: fixtures in tests/fixtures/cameras/ come from real answers, trimmed; HTTP goes through fakes.
"""

from __future__ import annotations

import gzip
import json
import random
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from meridian.modules.cameras import build as build_mod
from meridian.modules.cameras import common, mapfile
from meridian.modules.cameras.catalog import Catalog, brief, camera_type, show_test
from meridian.modules.cameras.common import NeedsKey, Pending, Source, make_camera
from meridian.modules.cameras.sources import gl_ashcam, gl_ndbc, gl_nps, windy
from meridian.net import NetError, NetResponse

FIX = Path(__file__).parent / "fixtures" / "cameras"
KEY = "test-windy-key-0123456789"


def fx(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeDS:
    def __init__(self, root: Path, net=None, force=False):
        self.out_dir = root / "build"
        self.raw_dir = root / "raw"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.net = net
        self.force = force
        self.max_age_hours = 24.0
        self.rows = None
        self.notes: list[str] = []
        self.sources: list[str] = []
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


class Cfg:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, name, default=None):
        return self.values.get(name, default)

    def has(self, name):
        return bool(self.values.get(name))

    def redact(self, text):
        for v in self.values.values():
            if v and len(v) >= 6:
                text = text.replace(v, "***")
        return text


def cam(wid, lat, lon, country="CH", cats=("meteo",), status="active", title=None):
    return {"webcamId": wid, "title": title or f"Place {wid}: View", "status": status, "viewCount": 10,
            "categories": [{"id": c, "name": c} for c in cats],
            "location": {"city": "Town", "region": "Region", "region_code": f"{country}.X", "country": "X",
                         "country_code": country, "latitude": lat, "longitude": lon},
            "urls": {"detail": f"https://windy.com/webcams/{wid}", "provider": "http://example.org/"}}


class WindyAPI:
    """A tiny stand-in for the Webcams API: filters by country and bbox (north,east,south,west),
    pages with limit/offset like the free tier (offset at most 1,000) and records every call."""

    def __init__(self, webcams, status=200):
        self.webcams = webcams
        self.calls: list[tuple[str, dict, dict]] = []
        self.status = status

    def request(self, url, *, params=None, headers=None, **kw):
        params = dict(params or {})
        self.calls.append((url, params, dict(headers or {})))
        if self.status != 200:
            raise NetError(f"api.windy.com: HTTP {self.status}", status=self.status)
        if url.endswith("/countries"):
            codes = sorted({c["location"]["country_code"] for c in self.webcams})
            return NetResponse(200, {}, json.dumps([{"code": c, "name": c} for c in codes]).encode(), url)
        assert int(params.get("offset", 0)) <= 1000, "offset above the free tier's 1,000"
        assert int(params.get("limit", 10)) <= 50
        rows = [c for c in self.webcams if c["location"]["country_code"] == params.get("countries")]
        if params.get("bbox"):
            n, e, s, w = map(float, params["bbox"].split(","))
            rows = [c for c in rows if s <= c["location"]["latitude"] <= n and w <= c["location"]["longitude"] <= e]
        rows.sort(key=lambda c: c["webcamId"])
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 10))
        body = {"total": len(rows), "webcams": rows[off:off + lim]}
        return NetResponse(200, {"x-ratelimit-remaining": "250"}, json.dumps(body).encode(), url)


# ---------------------------------------------------------------------------------------------
class WindyRecordTests(unittest.TestCase):
    def test_page_parsing_and_marking(self):
        rows, skipped = windy.parse(fx("windy_page.json"), windy.SOURCE)
        self.assertEqual((len(rows), skipped), (4, 2))                  # indoor and inactive left out
        r = rows[0]
        self.assertTrue(r["id"].startswith("windy:"))
        self.assertEqual((r["type"], r["kind"], r["country"]), ("webcam", "image", "LU"))
        self.assertTrue(r.get("direct") and r.get("dynamic"))
        self.assertNotIn("image", r)                                    # no picture link is ever stored
        self.assertTrue(r["page"].startswith("https://windy.com/webcams/"))
        self.assertNotIn("traffic", r["tags"])
        self.assertTrue(all(t in common.TAGS for t in r["tags"]))

    def test_compact_records_round_trip(self):
        rec = windy.compact(cam(123, 46.5, 7.5, cats=("mountain", "meteo")))
        self.assertEqual(rec[:4], [123, "Place 123: View", 46.5, 7.5])
        row = windy.row_for(rec, windy.SOURCE)
        self.assertEqual(set(row["tags"]) & {"mountain", "weather"}, {"mountain", "weather"})
        self.assertIsNone(windy.compact(cam(1, 46, 7, cats=("indoor",))))
        self.assertIsNone(windy.compact(cam(1, None, 7)))
        self.assertIsNone(windy.compact(cam(1, 46, 7, status="disabled")))
        bad = windy.compact(cam(9, 46, 7))
        bad[8] = "https://evil.example/webcams/9"                       # a page link off windy.com is dropped
        self.assertNotIn("page", windy.row_for(bad, windy.SOURCE))

    def test_boxes(self):
        self.assertEqual(windy.fmt_box((90.0, 180.0, -90.0, -180.0)), "90,180,-90,-180")
        q = windy.quarters((48.0, 11.0, 45.0, 5.0))
        self.assertEqual(len(q), 4)
        mids = {q[0][2], q[0][3]}
        self.assertTrue(all(round(m * 1e7) % 10 == 5 for m in mids))   # split lines never on a 6-decimal point
        box = windy.sample_box([cam(1, 46.0, 7.0), cam(2, 47.0, 9.0)])
        n, e, s, w = box
        self.assertTrue(n > 47.0 and s < 46.0 and e > 9.0 and w < 7.0)
        bands = windy.ring(box)
        self.assertEqual(len(bands), 4)
        self.assertEqual(windy.sample_box([]), windy.WORLD)
        self.assertEqual(windy.ring(windy.WORLD), [])


class WindyCrawlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def world(self):
        rnd = random.Random(7)
        cams = [cam(1000 + i, round(rnd.uniform(45.8, 47.8), 5), round(rnd.uniform(5.9, 10.5), 5)) for i in range(2300)]
        cams += [cam(5000 + i, round(rnd.uniform(49.4, 50.2), 5), round(rnd.uniform(5.7, 6.5), 5), country="LU")
                 for i in range(60)]
        cams.append(cam(9999, -21.1, 55.5))          # an outlier far away, still in CH: found by the ring
        return cams

    def test_crawl_splits_big_countries_and_reaches_everything(self):
        api = WindyAPI(self.world())
        ds = FakeDS(self.tmp, api)
        crawl = windy.Crawl(ds, KEY, spacing=0)
        crawl.run()
        doc = crawl.document()
        self.assertEqual(doc["stats"]["webcams"], 2361)                  # every webcam, once
        self.assertTrue(doc["complete"])
        self.assertEqual(doc["countries"]["CH"]["total"], 2301)
        self.assertEqual(doc["countries"]["CH"]["covered"], 2301)
        self.assertGreater(doc["countries"]["CH"]["areas"], 4)
        self.assertEqual(doc["countries"]["LU"]["areas"], 0)
        for url, params, headers in api.calls:
            self.assertEqual(headers.get("x-windy-api-key"), KEY)        # the key only ever in the header
            self.assertNotIn(KEY, url + json.dumps(params))
            if "offset" in params:
                self.assertEqual((params["limit"], params["sortKey"], params["sortDirection"]), (50, "createdOn", "asc"))
        n = len(api.calls)
        # a second run within the week reads the cached pages and asks nothing
        crawl2 = windy.Crawl(FakeDS(self.tmp, api), KEY, spacing=0)
        crawl2.run()
        self.assertEqual(len(api.calls), n)
        self.assertEqual(crawl2.document()["stats"]["webcams"], 2361)
        pages = list((self.tmp / "raw" / "windy" / "pages").iterdir())
        self.assertTrue(pages and all(p.read_bytes()[:2] == b"\x1f\x8b" for p in pages))   # stored gzipped

    def test_budget_stop_carries_previous_webcams(self):
        api = WindyAPI(self.world())
        crawl = windy.Crawl(FakeDS(self.tmp, api), KEY, spacing=0, max_requests=3)
        with self.assertRaises(windy.Budget):
            crawl.run()
        crawl.stopped = "request budget used"
        previous = {"countries": {"LU": {}, "CH": {}}, "webcams": [windy.compact(cam(5000, 49.6, 6.1, country="LU"))]}
        doc = crawl.document(previous)
        self.assertFalse(doc["complete"])
        self.assertIn(5000, [r[0] for r in doc["webcams"]])            # LU not reached: kept from last time

    def test_refused_key_stops_the_crawl(self):
        api = WindyAPI(self.world(), status=401)
        with self.assertRaises(windy.KeyRefused) as cm:
            windy.Crawl(FakeDS(self.tmp, api), KEY, spacing=0).run()
        self.assertNotIn(KEY, str(cm.exception))

    def test_build_crawl_weekly(self):
        api = WindyAPI(self.world()[:40] + [cam(5000, 49.6, 6.1, country="LU")])
        ds = FakeDS(self.tmp, api)
        cfg = Cfg({windy.KEY: KEY})
        doc = windy.build_crawl(ds, cfg, spacing=0)
        self.assertEqual(ds.rows, 41)
        self.assertTrue((ds.out_dir / windy.CRAWL_FILE).exists())
        calls = len(api.calls)
        self.assertIsNone(windy.build_crawl(FakeDS(self.tmp, api), cfg, spacing=0))   # a week has not passed
        self.assertEqual(len(api.calls), calls)
        with self.assertRaises(NeedsKey):
            windy.build_crawl(FakeDS(self.tmp, api), Cfg(), spacing=0)
        # the camera build reads the crawl, no network
        fc = common.FetchContext(ds, windy.SOURCE, config=cfg)
        rows = windy.fetch(fc)
        self.assertEqual(len(rows), 41)
        self.assertEqual(fc.extra["crawl_at"], doc["crawl_at"])
        self.assertIn("crawl_file_mtime", fc.extra)

    def test_fetch_without_key_or_crawl(self):
        ds = FakeDS(self.tmp)
        with self.assertRaises(NeedsKey):
            windy.fetch(common.FetchContext(ds, windy.SOURCE, config=Cfg()))
        with self.assertRaises(Pending):
            windy.fetch(common.FetchContext(ds, windy.SOURCE, config=Cfg({windy.KEY: KEY})))


class WindyDirectTests(unittest.TestCase):
    def rows(self):
        return [{"id": "windy:1900000001", "src": "windy"}, {"id": "windy:1900000002", "src": "windy"},
                {"id": "windy:42", "src": "windy"}]

    def test_fresh_links_for_the_browser(self):
        net = mock.Mock()
        net.get_json.return_value = fx("windy_direct.json")
        direct = windy.make_direct(Cfg({windy.KEY: KEY}))
        out = direct(self.rows(), net)
        url = net.get_json.call_args.args[0]
        kw = net.get_json.call_args.kwargs
        self.assertEqual(url, windy.LIST_URL)
        self.assertEqual(kw["params"]["webcamIds"], "1900000001,1900000002,42")
        self.assertEqual(kw["params"]["limit"], 3)
        self.assertIn("images", kw["params"]["include"])
        self.assertEqual(kw["headers"]["x-windy-api-key"], KEY)
        self.assertNotIn(KEY, json.dumps(kw["params"]))
        info = out["windy:1900000001"]
        self.assertTrue(info["ok"])
        self.assertTrue(info["image"].startswith("https://imgproxy.windy.com/"))
        self.assertTrue(info["link"].startswith("https://windy.com/webcams/"))
        self.assertEqual((info["width"], info["height"]), (400, 224))
        self.assertTrue(info["thumb"].startswith("https://imgproxy.windy.com/"))
        self.assertGreater(info["updated_at"], 1.7e9)
        self.assertFalse(out["windy:42"]["ok"])                           # no longer listed

    def test_unsafe_links_never_reach_the_browser(self):
        data = fx("windy_direct.json")
        data["webcams"][0]["images"]["current"]["preview"] = "https://evil.example/x.jpg"
        data["webcams"][0]["images"]["current"]["thumbnail"] = f"https://imgproxy.windy.com/x.jpg?key={KEY}"
        data["webcams"][1]["urls"]["detail"] = "javascript:alert(1)"
        data["webcams"][1]["player"]["day"] = "http://webcams.windy.com/player"     # http, not https
        net = mock.Mock()
        net.get_json.return_value = data
        out = windy.make_direct(Cfg({windy.KEY: KEY}))(self.rows()[:2], net)
        self.assertFalse(out["windy:1900000001"]["ok"])                   # neither picture link was usable
        self.assertFalse(out["windy:1900000002"]["ok"])                   # no safe link to the source
        self.assertNotIn(KEY, json.dumps(out))
        with self.assertRaises(NeedsKey):
            windy.make_direct(Cfg())(self.rows(), net)


# ---------------------------------------------------------------------------------------------
class GlobalSourceTests(unittest.TestCase):
    def test_ashcam_volcano_and_faa(self):
        data = fx("ashcam.json")
        now = data["_fixture_now"]
        vol, skipped_v = gl_ashcam.parse(data, gl_ashcam.VOLCANO, faa=False, now=now)
        faa, skipped_f = gl_ashcam.parse(data, gl_ashcam.FAA, faa=True, now=now)
        self.assertEqual(len(faa), 1)
        self.assertEqual(len(vol), 3)                                     # AVO, CVO and HVO
        self.assertEqual(skipped_v + skipped_f, 2)                        # one a month old, one not public
        ids = {r["id"] for r in vol}
        self.assertNotIn("usgsvolc:private_test", ids)
        for r in vol + faa:
            self.assertEqual(r["type"], "webcam")
            self.assertTrue(r["image"].startswith("https://avo-volcview.wr.usgs.gov/ashcam-api/images/"))
            self.assertTrue(common.Source and build_mod.host_allowed(r["image"], gl_ashcam.VOLCANO))
        cvo = next(r for r in vol if "Cascades" in r.get("description", ""))
        self.assertFalse(cvo["name"].startswith("CVO "))
        self.assertIn(cvo["region"], ("Washington", "Oregon"))
        self.assertIn("volcano", vol[0]["tags"])
        self.assertIn("weather", faa[0]["tags"])
        self.assertIsNotNone(faa[0].get("heading"))
        self.assertEqual(gl_ashcam.place(51.38, 179.3), ("US", "Alaska"))     # the Aleutians cross 180°
        self.assertEqual(gl_ashcam.place(19.4, -155.3), ("US", "Hawaii"))

    def test_ndbc_buoys(self):
        rows, skipped = gl_ndbc.parse(fx("ndbc_buoycams.json"), gl_ndbc.SOURCE)
        self.assertEqual((len(rows), skipped), (3, 1))
        r = rows[0]
        self.assertTrue(r["image"].startswith("https://www.ndbc.noaa.gov/buoycam.php?station="))
        self.assertTrue(build_mod.host_allowed(r["image"], gl_ndbc.SOURCE))
        self.assertFalse(build_mod.host_allowed("https://www.ndbc.noaa.gov/other.php", gl_ndbc.SOURCE))
        self.assertEqual(r["type"], "webcam")
        self.assertIn("coastal", r["tags"])

    def test_nps_pages_and_key(self):
        rows, skipped = gl_nps.parse(fx("nps_webcams.json"), gl_nps.SOURCE)
        self.assertEqual((len(rows), skipped), (2, 1))
        self.assertTrue(all(r["kind"] == "page" and r["page"].startswith("https://www.nps.gov/") for r in rows))
        self.assertEqual(rows[0]["region"], "California")
        tmp = Path(tempfile.mkdtemp())
        try:
            ds = FakeDS(tmp)
            with self.assertRaises(NeedsKey):
                gl_nps.fetch(common.FetchContext(ds, gl_nps.SOURCE, config=Cfg()))
            calls = []

            def json_get(url, filename, **kw):
                calls.append((url, kw))
                return fx("nps_webcams.json")

            fc = common.FetchContext(ds, gl_nps.SOURCE, config=Cfg({gl_nps.KEY: "nps-test-key-123"}))
            fc.json = json_get
            gl_nps.fetch(fc)
            self.assertNotIn("nps-test-key-123", calls[0][0])               # header, never the URL
            self.assertEqual(calls[0][1]["headers"]["X-Api-Key"], "nps-test-key-123")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_types_and_registry(self):
        from meridian.modules.cameras import sources
        by_id = sources.BY_ID
        for sid in ("usgsvolc", "faawx", "ndbcbuoy", "nps", "windy"):
            self.assertIn(sid, by_id)
            self.assertEqual(common.source_camera_type(by_id[sid]), "webcam")
            self.assertTrue(by_id[sid].hosts)
        self.assertEqual(common.source_camera_type(by_id["tfl"]), "traffic")
        self.assertEqual(common.source_camera_type(by_id["alertca"]), "webcam")
        self.assertIs(by_id["windy"].direct is not None, True)
        self.assertEqual(sources.BROKEN, [])


# ---------------------------------------------------------------------------------------------
def synthetic_rows(n, seed=3):
    rnd = random.Random(seed)
    traffic = Source(id="t", name="T", owner="o", url="u", licence="L", attribution="A", hosts=("img.t.test",))
    web = Source(id="w", name="W", owner="o", url="u", licence="L", attribution="A", hosts=("img.w.test",), tags=(),
                 camera_type="webcam")
    words = ["Lake", "Peak", "Harbour", "Bridge", "Valley", "Square", "Station", "Beach", "Pass", "Glacier"]
    rows = []
    for i in range(n):
        src = web if i % 3 else traffic
        lat, lon = rnd.uniform(-60, 70), rnd.uniform(-180, 180)
        rows.append(make_camera(src, i, f"{rnd.choice(words)} {rnd.choice(words)} {i}", lat, lon,
                                image=f"https://img.{src.id}.test/{i}.jpg", stream="https://v.t.test/a.m3u8" if i % 50 == 0 else None))
    return rows


class MapFileTests(unittest.TestCase):
    def test_lean_features(self):
        src = Source(id="w", name="W", owner="o", url="u", licence="L", attribution="A", tags=(), camera_type="webcam")
        row = make_camera(src, "1", "Zermatt: Matterhorn", 46.0207, 7.7491, image="https://x.test/1.jpg",
                          heading=200, tags=("mountain",))
        f = mapfile.to_feature(row)
        self.assertEqual(f["properties"], {"id": "w:1", "t": 1, "g": "m", "h": 200})
        self.assertEqual(f["geometry"]["coordinates"], [7.7491, 46.0207])
        traffic = make_camera(Source(id="t", name="T", owner="o", url="u", licence="L", attribution="A"), "2", "A40",
                              51.5, -0.2, image="https://x.test/2.jpg", stream="https://x.test/2.m3u8")
        self.assertEqual(mapfile.to_feature(traffic)["properties"], {"id": "t:2", "k": 1})
        rows = synthetic_rows(2000)
        fc = mapfile.map_collection(sorted(rows, key=lambda r: r["id"]), 1.0)
        size = len(json.dumps(fc, separators=(",", ":")))
        self.assertLess(size / len(rows), 120)                           # was about 197 bytes a camera
        self.assertEqual(fc["count"], 2000)

    def test_old_rows_get_a_type(self):
        self.assertEqual(camera_type({"tags": ["traffic", "urban"]}), "traffic")
        self.assertEqual(camera_type({"tags": ["mountain"]}), "webcam")
        self.assertEqual(camera_type({"type": "webcam", "tags": ["traffic"]}), "webcam")


class CatalogScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = synthetic_rows(120_000)
        t = time.time()
        cls.cat = Catalog([dict(r) for r in cls.rows], {}, time.time())
        cls.build_s = time.time() - t

    def test_build_and_counts(self):
        self.assertLess(self.build_s, 25.0)
        show = self.cat.counts()["show"]
        self.assertEqual(show["all"], 120_000)
        self.assertEqual(show["traffic"] + show["webcam"], 120_000)
        self.assertEqual(show["traffic"], 40_000)
        self.assertEqual(show["live"], 2_400)
        self.assertEqual(show["live"] + show["still"], 120_000)
        self.assertEqual(sum(1 for r in self.rows if show_test("webcam")(r)), show["webcam"])

    def test_world_bbox_is_fast_and_nearest_first(self):
        t = time.time()
        total, rows = self.cat.in_bbox((-180, -85, 180, 85), center=(48.0, 2.0), limit=144)
        took = time.time() - t
        self.assertEqual(total, 120_000)
        self.assertEqual(len(rows), 144)
        from meridian import geo
        d = [geo.haversine_km(48.0, 2.0, r["lat"], r["lon"]) for r in rows]
        self.assertEqual(d, sorted(d))
        brute = sorted(geo.haversine_km(48.0, 2.0, r["lat"], r["lon"]) for r in self.cat.rows)[:144]
        self.assertAlmostEqual(d[-1], brute[-1], places=6)               # really the 144 nearest
        self.assertLess(took, 1.5)
        total, _ = self.cat.in_bbox((-180, -85, 180, 85), center=(0, 0), limit=10, keep=show_test("traffic"))
        self.assertEqual(total, 40_000)

    def test_near_and_antimeridian(self):
        near = self.cat.near(10.0, 179.9, 400, 20)
        self.assertTrue(near)
        self.assertTrue(all(d <= 400 for d, _ in near))
        self.assertTrue(any(r["lon"] < 0 for _, r in near) or all(r["lon"] > 0 for _, r in near))

    def test_search_stays_fast_with_common_words(self):
        t = time.time()
        hits = self.cat.search("lake", 20)
        self.assertLess(time.time() - t, 0.6)
        self.assertEqual(len(hits), 20)
        self.assertTrue(all("lake" in r["name"].lower() for _, r in hits))
        hits = self.cat.search("glacier pass 17", 5)
        self.assertTrue(hits)


class YieldTests(unittest.TestCase):
    def test_aggregator_rows_yield_to_official_cameras(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            official = Source(id="dot", name="DOT", owner="o", url="u", licence="L", attribution="A",
                              hosts=("img.dot.test",))
            agg = Source(id="agg", name="Agg", owner="o", url="u", licence="L", attribution="A", hosts=("x.test",),
                         aggregates=True, tags=())
            official.fetch = lambda fc: [make_camera(official, "1", "I-5 at Main", 47.6, -122.3, image="https://img.dot.test/1.jpg")]
            agg.fetch = lambda fc: [make_camera(agg, "a", "I-5 Main St (copy)", 47.6004, -122.3, direct=True),     # 45 m away
                                    make_camera(agg, "b", "Lake view", 47.61, -122.3, direct=True)]               # 1.1 km away
            doc = build_mod.build(FakeDS(tmp), [official, agg], None, mock.Mock())
            self.assertEqual(sorted(r["id"] for r in doc["cameras"]), ["agg:b", "dot:1"])
            self.assertEqual(doc["sources"]["agg"]["yielded"], 1)
            self.assertIn("already in an official feed", doc["sources"]["agg"]["note"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_windy_types_follow_its_categories(self):
        road = windy.row_for(windy.compact(cam(1, 46, 7, cats=("traffic", "mountain"))), windy.SOURCE)
        view = windy.row_for(windy.compact(cam(2, 46, 7, cats=("landscape",))), windy.SOURCE)
        self.assertEqual((road["type"], view["type"]), ("traffic", "webcam"))
        self.assertTrue(windy.SOURCE.aggregates)


class DirectRowTests(unittest.TestCase):
    def test_brief_of_a_direct_row(self):
        rows, _ = windy.parse(fx("windy_page.json"), windy.SOURCE)
        b = brief(rows[0])
        self.assertIsNone(b["img_url"])
        self.assertTrue(b["direct"])
        self.assertEqual(b["type"], "webcam")
        self.assertTrue(b["page"].startswith("https://windy.com/"))


# ---------------------------------------------------------------------------------------------
class FakeNet:
    def __init__(self):
        self.calls = []
        self.json_calls = []

    def request(self, url, **kw):
        self.calls.append(url)
        return NetResponse(200, {"content-type": "image/jpeg"}, b"\xff\xd8\xff\xe0" + b"\x00" * 64, url)

    def get_json(self, url, **kw):
        self.json_calls.append((url, kw))
        return fx("windy_direct.json")

    def download(self, *a, **kw):
        raise AssertionError("no downloads in route tests")

    def set_host_interval(self, *a):
        pass


class GlobalRouteTests(unittest.TestCase):
    """Routes with Windy configured: the crawl joins the list without a rebuild of the others,
    direct links come from the server (cached), the proxy refuses Windy pictures, labels and
    "Show" counts."""

    @classmethod
    def setUpClass(cls):
        from meridian.config import Config
        from meridian.core_api import register_core
        from meridian.datasets import DatasetManager
        from meridian.modules import cameras
        from meridian.registry import App
        from meridian.scheduler import Scheduler
        from meridian.store import Store
        from meridian.modules.cameras.sources import tfl

        cls.tmp = Path(tempfile.mkdtemp())
        config = Config(env_file=None, overrides={"MERIDIAN_DATA_DIR": str(cls.tmp), windy.KEY: KEY})
        cls.net = FakeNet()
        cls.app = App(config, Store(cls.tmp / "meridian.db"), cls.net, mock.Mock(), Scheduler(), DatasetManager())
        register_core(cls.app)
        m = cls.app.module("cameras")
        cameras.register(m)
        cls.m = m
        tfl_rows, _ = tfl.parse(json.loads((FIX / "tfl.json").read_text(encoding="utf-8")), tfl.SOURCE)
        m.data_dir.mkdir(parents=True, exist_ok=True)
        doc = {"version": 1, "built_at": time.time(), "count": len(tfl_rows),
               "sources": {"tfl": {**build_mod.source_meta(tfl.SOURCE), "ok": True, "count": len(tfl_rows),
                                   "note": "kept"}}, "cameras": tfl_rows}
        (m.data_dir / "cameras.json").write_text(json.dumps(doc), encoding="utf-8")
        (m.data_dir / "cameras.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
        # a finished crawl: the Luxembourg page as compact records
        recs = [windy.compact(c) for c in fx("windy_page.json")["webcams"]]
        recs = [r for r in recs if r]
        recs[0][2], recs[0][3] = tfl_rows[0]["lat"] + 0.001, tfl_rows[0]["lon"]     # one webcam near a TfL camera
        crawl = {"version": 1, "crawl_at": time.time(), "complete": True, "webcams": recs, "countries": {},
                 "stats": {"countries": 1}}
        (m.data_dir / windy.CRAWL_FILE).write_text(json.dumps(crawl), encoding="utf-8")
        for fn in cls.app.datasets.items["cameras.cameras"].listeners:
            fn()
        cls.tfl_rows = tfl_rows
        deadline = time.time() + 15
        cls.windy_ids = []
        while time.time() < deadline and not cls.windy_ids:       # the crawl joins in a background thread
            time.sleep(0.05)
            status, body = cls.call("/api/cameras/list", bbox="-180,-85,180,85", type="webcam", limit=200)
            if status == 200:
                cls.windy_ids = [c["id"] for c in body["cameras"] if c["src"] == "windy"]

    @classmethod
    def tearDownClass(cls):
        cls.app.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def call(cls, path, **query):
        from meridian.registry import HTTPError, Request, Response
        route, params, _ = cls.app.match("GET", path)
        req = Request("GET", path, params, {k: [str(v)] for k, v in query.items()}, {})
        try:
            out = route.handler(req)
        except HTTPError as err:
            return err.status, {"error": {"code": err.code}}
        if isinstance(out, Response):
            return out.status, out
        return 200, out

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

    def test_crawl_joined_the_list_without_touching_other_sources(self):
        status, body = self.get("/api/cameras/summary")
        self.assertEqual(status, 200)
        by_id = {s["id"]: s for s in body["sources"]}
        self.assertEqual(by_id["windy"]["count"], 4)
        self.assertTrue(by_id["windy"]["ok"])
        self.assertEqual(by_id["tfl"]["note"], "kept")                   # TfL not re-run, nothing fetched
        cams = [c for c in fx("windy_page.json")["webcams"] if windy.compact(c)]
        windy_traffic = sum(1 for c in cams if any(k.get("id") == "traffic" for k in c.get("categories") or []))
        self.assertEqual(body["show"]["webcam"], 4 - windy_traffic)      # Windy's own traffic category counts as traffic
        self.assertEqual(body["show"]["all"], body["count"])
        self.assertEqual(body["by_type"], {"traffic": 2 + windy_traffic, "webcam": 4 - windy_traffic})
        geo = json.loads((self.m.data_dir / "cameras.geojson").read_text(encoding="utf-8"))
        self.assertEqual(len(geo["features"]), body["count"])
        props = [f["properties"] for f in geo["features"]]
        self.assertTrue(all(set(p) <= {"id", "k", "t", "g", "h"} for p in props))
        self.assertEqual(sum(1 for p in props if p.get("t") == 1), 4 - windy_traffic)

    def test_direct_links_and_no_proxy(self):
        wid = sorted(self.windy_ids)[0]
        status, body = self.get(f"/api/cameras/camera/{wid}")
        cam = body["camera"]
        self.assertTrue(cam["direct"])
        self.assertIsNone(cam["img_url"])
        self.assertEqual(cam["type"], "webcam")
        self.assertEqual(body["source"]["courtesy"][0]["text"], "Webcams provided by Windy.com")
        before = len(self.net.calls)
        status, resp = self.get(f"/api/cameras/img/{wid}")
        self.assertEqual(status, 409)
        status, resp = self.get(f"/api/cameras/img/{wid}", fallback=1)
        self.assertEqual(resp.headers["X-Camera-Error"], "DIRECT_ONLY")
        self.assertEqual(len(self.net.calls), before)                     # the proxy fetched nothing
        status, info = self.get(f"/api/cameras/direct/{wid}")
        self.assertEqual(status, 200)
        self.assertEqual(info["courtesy"][1]["href"], "https://www.windy.com/webcams/add")
        self.assertTrue(info["link"].startswith("https://windy.com/webcams/"))
        self.assertNotIn(KEY, json.dumps(info))
        n = len(self.net.json_calls)
        status, many = self.get("/api/cameras/direct", ids=",".join(self.windy_ids + ["tfl:nope"]))
        self.assertEqual(set(many["items"]), set(self.windy_ids) | {"tfl:nope"})
        self.assertFalse(many["items"]["tfl:nope"]["ok"])
        self.assertLessEqual(len(self.net.json_calls) - n, 1)            # one batched call for the rest
        status, _ = self.get(f"/api/cameras/direct/{self.tfl_rows[0]['id']}")
        self.assertEqual(status, 400)
        for url, kw in self.net.json_calls:
            self.assertNotIn(KEY, url + json.dumps(kw.get("params") or {}))

    def test_every_source_key_is_declared(self):
        from meridian.modules.cameras import sources
        declared = self.app.config.declared
        for src in sources.ALL:
            if src.needs_key:
                self.assertIn(src.needs_key, declared, src.id)
                self.assertTrue(declared[src.needs_key]["secret"], src.id)

    def test_optional_keys_read_without_needs_key_are_declared(self):
        # QLDTraffic falls back to a shared public key, so it has no needs_key, but its own key must
        # still be listed in Settings and .env.example (CONTRACT 4) and redacted as a secret
        from meridian.modules.cameras.sources import ap_qld
        self.assertIsNone(ap_qld.SOURCE.needs_key)
        declared = self.app.config.declared
        self.assertIn(ap_qld.KEY, declared)
        self.assertTrue(declared[ap_qld.KEY]["secret"])

    def test_stream_only_cameras_have_no_still(self):
        from meridian.modules.cameras.catalog import brief as brief_of
        src = Source(id="del", name="DelDOT", owner="o", url="u", licence="L", attribution="A", hosts=("v.test",),
                     stream_cors=True)
        row = make_camera(src, "1", "SR 1 at Route 9", 39.5, -75.6, stream="https://v.test/1/playlist.m3u8")
        self.assertEqual(row["kind"], "hls")
        self.assertIsNone(brief_of(row)["img_url"])
        dyn = make_camera(src, "2", "Looked up", 39.5, -75.6, dynamic=True)
        self.assertEqual(brief_of(dyn)["img_url"], "/api/cameras/img/del:2")

    def test_labels_lists_and_filters(self):
        r0 = self.tfl_rows[0]
        bbox = f"{r0['lon'] - 0.05},{r0['lat'] - 0.05},{r0['lon'] + 0.05},{r0['lat'] + 0.05}"
        status, body = self.get("/api/cameras/labels", bbox=bbox, limit=50)
        self.assertEqual(status, 200)
        names = {f["properties"]["id"]: f["properties"] for f in body["features"]}
        self.assertIn(r0["id"], names)
        self.assertEqual(names[r0["id"]]["n"], r0["name"])
        status, body = self.get("/api/cameras/labels", bbox=bbox, show="webcam")
        self.assertTrue(all(f["properties"].get("t") == 1 for f in body["features"]))
        status, body = self.get("/api/cameras/labels", bbox=bbox, show="nonsense")
        self.assertEqual(status, 400)
        status, body = self.get("/api/cameras/list", lat=r0["lat"], lon=r0["lon"], radius_km=5, type="webcam")
        self.assertTrue(body["cameras"] and all(c["type"] == "webcam" and c["direct"] for c in body["cameras"]))
        status, body = self.get("/api/cameras/list", lat=r0["lat"], lon=r0["lon"], radius_km=5, type="bogus")
        self.assertEqual(status, 400)
        out = self.app.call_ai_tool("cameras_near", {"lat": r0["lat"], "lon": r0["lon"], "radius_km": 5, "type": "webcam"})
        self.assertTrue(out["cameras"] and out["cameras"][0]["image_url"] is None)
        self.assertEqual(out["cameras"][0]["type"], "webcam")


if __name__ == "__main__":
    unittest.main()
