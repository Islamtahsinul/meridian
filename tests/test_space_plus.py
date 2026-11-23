"""Space+ tests: the GIBS extension pack, Meteosat (EUMETView WMS) and the satellite scene finder.

Run: python -m unittest tests.test_space_plus -v      (no network: fixtures in tests/fixtures/space/plus/)

The fixtures are trimmed real responses from 23 Nov 2026: Element 84 Earth Search (Sentinel-2 over
London), Copernicus Data Space Ecosystem STAC (Sentinel-1), Microsoft Planetary Computer (Landsat, with
the fields extension), the EUMETView mtg_fd capabilities and two GIBS DescribeDomains documents.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.net import NetError, NetResponse
from meridian.registry import App, HTTPError, Request, Response
from meridian.scheduler import Scheduler
from meridian.store import Store
from meridian.modules.space import imagery as I
from meridian.modules.space import meteosat as MS
from meridian.modules.space import scenes as SC

FIX = Path(__file__).parent / "fixtures" / "space"
PLUS = FIX / "plus"
LONDON = (51.5072, -0.1276)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def plus(name: str):
    text = (PLUS / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


class FakeNet:
    """Answers the hosts Space+ talks to from fixtures; records every request."""

    def __init__(self):
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.lock = threading.Lock()
        self.images: dict[str, tuple[int, dict, bytes]] = {}

    def set_host_interval(self, host, seconds):
        pass

    def _answer(self, url: str, params: dict | None) -> tuple[int, dict, bytes]:
        full = url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        with self.lock:
            self.calls.append(full)
        for key in self.fail:
            if key in full:
                raise NetError(f"{key}: HTTP 503", status=503)
        if url in self.images:
            return self.images[url]
        host = urllib.parse.urlsplit(url).hostname or ""
        if host == "earth-search.aws.element84.com" and url.endswith("/search"):
            return 200, {}, json.dumps(plus("stac_e84_s2.json")).encode()
        if host == "stac.dataspace.copernicus.eu" and url.endswith("/search"):
            return 200, {}, json.dumps(plus("stac_cdse_s1.json")).encode()
        if host == "planetarycomputer.microsoft.com" and url.endswith("/search"):
            return 200, {}, json.dumps(plus("stac_pc_landsat.json")).encode()
        if host == "stac.dataspace.copernicus.eu" and "/items/" in url:
            item_id = urllib.parse.unquote(url.rsplit("/", 1)[1])
            for f in plus("stac_cdse_s1.json")["features"]:
                if f["id"] == item_id:
                    return 200, {}, json.dumps(f).encode()
            return 404, {}, b"{}"
        if host == "view.eumetsat.int":
            return 200, {}, plus("eumetview_caps.xml").encode()
        if host == "gibs.earthdata.nasa.gov":
            if "OPERA_L2_Radiometric" in url:
                return 200, {}, plus("gibs_domain_sar.xml").encode()
            if "TROPOMI" in url:
                return 200, {}, plus("gibs_domain_no2.xml").encode()
            if "Clean_Infrared" in url:
                return 200, {}, (FIX / "gibs_domain_ir.xml").read_bytes()
            return 200, {}, (FIX / "gibs_domain_daily.xml").read_bytes()
        return 404, {}, b""

    def request(self, url, *, params=None, accept=(200,), allow_redirects=True, **kw):
        status, headers, body = self._answer(url, params)
        if status not in accept:
            raise NetError(f"HTTP {status}", status=status)
        return NetResponse(status, headers, body, url)

    def get_bytes(self, url, **kw):
        return self.request(url, params=kw.get("params")).body

    def get_json(self, url, **kw):
        return json.loads(self.get_bytes(url, **kw))

    def count(self, needle: str) -> int:
        with self.lock:
            return sum(1 for c in self.calls if needle in c)


# ---------------------------------------------------------------------------------------------
# GIBS extension pack and Meteosat
# ---------------------------------------------------------------------------------------------
class ExtensionPackTests(unittest.TestCase):
    def test_new_products_and_groups(self):
        for pid in ("no2", "so2", "seaice", "lightning"):
            self.assertIn(pid, I.WEATHER)
            self.assertEqual(I.PRODUCTS[pid].get("group", "Weather"), "Weather")
        for pid in ("nightdaily", "flood", "sar", "meteosat"):
            self.assertIn(pid, I.IMAGERY)
            self.assertEqual(I.PRODUCTS[pid]["group"], "Imagery")
        self.assertIn("blackouts", I.PRODUCTS["nightdaily"]["subtitle"])
        self.assertTrue(set(I.COMPARE) >= {"terra", "nightdaily", "flood", "sar", "no2"})
        self.assertNotIn("meteosat", I.COMPARE)          # 10-minute frames: not a before/after product
        for pid, p in I.PRODUCTS.items():
            for v in p.get("variants") or []:
                self.assertTrue(v["id"] and v["label"] and v["layers"], pid)
                self.assertTrue(set(v["layers"]) <= set(I.all_layers(p)), pid)
            for layer in (p.get("layer_bounds") or {}):
                self.assertIn(layer, I.all_layers(p), pid)
            self.assertTrue(p.get("about") or pid in I.TRUE_COLOUR or pid in ("features", "labels", "nightlights"), pid)

    def test_variants_and_layer_info(self):
        so2 = I.PRODUCTS["so2"]
        self.assertEqual(I.variant_layers(so2), ["OMPS_NOAA20_SO2_Lower_Troposphere"])
        self.assertEqual(I.variant_layers(so2, "high"), ["OMPS_NOAA20_SO2_Upper_Troposphere_and_Stratosphere"])
        self.assertEqual(I.variant_layers(so2, "nope"), I.variant_layers(so2))
        self.assertEqual(I.variant_layers(I.PRODUCTS["sst"]), ["GHRSST_L4_MUR_Sea_Surface_Temperature"])
        info = I.layer_info(I.PRODUCTS["clouds"], "eumetsat:ir105_hrfi")
        self.assertEqual(info["provider"], "eumetsat")
        self.assertEqual(info["wms"], {"url": MS.WMS_URL, "layers": "mtg_fd:ir105_hrfi",
                                       "styles": "mtg_fd_ir105_hrfi_style_02", "format": "image/png"})
        self.assertEqual(info["bounds"], [-45, -81, 67.5, 81])
        self.assertEqual(I.layer_info(I.PRODUCTS["meteosat"], "eumetsat:ir105_hrfi")["wms"]["styles"],
                         "mtg_fd_ir105_hrfi_grayscale")
        self.assertEqual(I.layer_info(I.PRODUCTS["sst"], "GHRSST_L4_MUR_Sea_Surface_Temperature"), {"provider": "gibs"})
        meta = I.product_meta("meteosat")
        self.assertEqual([l["layer"] for l in meta["layers"]],
                         ["eumetsat:rgb_geocolour", "eumetsat:ir105_hrfi", "eumetsat:rgb_dust"])
        self.assertNotIn("wms_styles", meta)
        self.assertEqual(meta["variants"][0]["id"], "geocolour")

    def test_domains_and_wms_times(self):
        sar = I.parse_domain(I.domain_text_from_xml(plus("gibs_domain_sar.xml").encode()), daily=True)
        self.assertEqual((sar[0], sar[-1], len(sar)), ("2026-11-03", "2026-11-23", 21))
        no2 = I.parse_domain(I.domain_text_from_xml(plus("gibs_domain_no2.xml").encode()), daily=True)
        self.assertEqual(no2[-1], "2026-11-13")                   # about ten days behind, as the layer says
        frames = I.parse_domain("2024-09-23T00:00:00.000Z/2026-11-23T15:50:00.000Z/PT10M", daily=False, limit=3)
        self.assertEqual(frames, ["2026-11-23T15:30:00Z", "2026-11-23T15:40:00Z", "2026-11-23T15:50:00Z"])
        self.assertEqual(len(I.fallback_times("PT5M", now=0)), 144)
        self.assertTrue(I.fallback_times("PT5M", now=1795400000)[-1].endswith(("0:00Z", "5:00Z")))

    def test_capabilities_parser(self):
        caps = MS.parse_capabilities(plus("eumetview_caps.xml").encode())
        self.assertEqual(set(caps), {"ir105_hrfi", "li_afa", "rgb_geocolour", "rgb_dust"})
        li = caps["li_afa"]
        self.assertEqual(li["time"], "2025-05-30T15:00:00.000Z/2026-11-23T16:00:00.000Z/PT5M")
        self.assertEqual(li["default"], "2026-11-23T16:00:00Z")
        self.assertEqual(li["bbox"], [-70.0, -70.0, 70.0, 70.0])
        with self.assertRaises(ValueError):
            MS.parse_capabilities(b"<WMS_Capabilities xmlns='http://www.opengis.net/wms'/>")
        self.assertIn("EUMETSAT Meteosat product 2026", MS.credit(2026))

    def test_catalogue_mixes_gibs_and_meteosat(self):
        net = FakeNet()
        log = logging.getLogger("meridian.test.spaceplus.gibs")
        wms = MS.Meteosat(net, log)
        g = I.Gibs(net, log, wms=wms)
        cat = g.catalogue(["clouds", "lightning", "so2", "sar", "no2", "meteosat"])
        clouds = cat["products"]["clouds"]
        self.assertEqual([l["provider"] for l in clouds["layers"]], ["gibs", "gibs", "gibs", "eumetsat"])
        mtg = clouds["layers"][3]
        self.assertEqual(mtg["latest"], "2026-11-23T15:50:00Z")
        self.assertFalse(mtg["estimated"])
        light = cat["products"]["lightning"]["layers"][0]
        self.assertEqual(light["latest"], "2026-11-23T16:00:00Z")
        self.assertEqual(len(light["times"]), 48)
        self.assertEqual(light["times"][-2], "2026-11-23T15:55:00Z")
        so2 = cat["products"]["so2"]
        self.assertEqual(len(so2["layers"]), 2)                      # both variants have times
        self.assertEqual(cat["products"]["no2"]["layers"][0]["latest"], "2026-11-13")
        self.assertEqual(cat["products"]["no2"]["default_time"], "2026-11-12")     # newest day still incomplete
        self.assertEqual(cat["products"]["sar"]["layers"][0]["times"][-6:][0], "2026-11-18")
        self.assertEqual(cat["products"]["meteosat"]["layers"][1]["wms"]["styles"], "mtg_fd_ir105_hrfi_grayscale")
        self.assertEqual(net.count("view.eumetsat.int"), 1)          # one capabilities read for all Meteosat layers
        g.catalogue(["lightning", "meteosat"])
        self.assertEqual(net.count("view.eumetsat.int"), 1)          # cached for ten minutes

    def test_meteosat_down_is_soft(self):
        net = FakeNet()
        net.fail.add("view.eumetsat.int")
        seen = []
        wms = MS.Meteosat(net, logging.getLogger("meridian.test.spaceplus.wms"), status=lambda **kw: seen.append(kw))
        cat = I.Gibs(net, logging.getLogger("meridian.test.spaceplus.wms"), wms=wms).catalogue(["lightning", "clouds"])
        le = cat["products"]["lightning"]["layers"][0]
        self.assertTrue(le["estimated"])
        self.assertTrue(le["times"])
        self.assertFalse(cat["products"]["clouds"]["layers"][0]["estimated"])     # GIBS still fine
        self.assertTrue(seen and seen[-1]["ok"] is False)
        n = net.count("view.eumetsat.int")
        I.Gibs(net, logging.getLogger("x"), wms=wms).catalogue(["lightning"])
        self.assertEqual(net.count("view.eumetsat.int"), n)          # backs off for two minutes after a failure


# ---------------------------------------------------------------------------------------------
# Scene finder
# ---------------------------------------------------------------------------------------------
class SceneQueryTests(unittest.TestCase):
    NOW = datetime(2026, 11, 23, 16, 12, 40, tzinfo=timezone.utc).timestamp()

    def test_point_queries_per_catalogue(self):
        url, p = SC.search_params("sentinel-2", point=LONDON, days=30, max_cloud=30, limit=4, now=self.NOW)
        self.assertEqual(url, "https://earth-search.aws.element84.com/v1/search")
        self.assertEqual(p["collections"], "sentinel-2-l2a")
        self.assertEqual(json.loads(p["intersects"]), {"type": "Point", "coordinates": [-0.1276, 51.5072]})
        self.assertEqual(p["datetime"], "2026-10-24T16:00:00Z/..")    # floored to the hour: stable cache keys
        self.assertEqual(p["sortby"], "-properties.datetime")
        self.assertEqual(json.loads(p["query"]), {"eo:cloud_cover": {"lte": 30}})
        self.assertNotIn("bbox", p)
        url, p = SC.search_params("sentinel-1", point=LONDON, days=10, max_cloud=30, limit=4, now=self.NOW)
        self.assertEqual(url, "https://stac.dataspace.copernicus.eu/v1/search")
        self.assertEqual((p["collections"], p["sortby"]), ("sentinel-1-grd", "-datetime"))
        self.assertNotIn("query", p)                                  # radar: no cloud filter
        self.assertNotIn("filter", p)
        url, p = SC.search_params("landsat", point=LONDON, days=60, max_cloud=60, limit=3, now=self.NOW)
        self.assertEqual(url, "https://planetarycomputer.microsoft.com/api/stac/v1/search")
        self.assertEqual((p["filter-lang"], p["filter"]), ("cql2-text", "eo:cloud_cover <= 60"))
        self.assertIn("properties.landsat:wrs_path", p["fields"])
        self.assertEqual(p["limit"], "3")
        _, p = SC.search_params("landsat", point=LONDON, max_cloud=100, now=self.NOW)
        self.assertNotIn("filter", p)                                 # 100% means any cloud

    def test_areas(self):
        self.assertEqual(SC.area(bbox=(-0.5, 51.2, 0.3, 51.8)), (None, (-0.5, 51.2, 0.3, 51.8)))
        self.assertEqual(SC.area(bbox=(-10.0, 40.0, 10.0, 60.0)), ((50.0, 0.0), None))   # too big: the centre
        point, box = SC.area(bbox=(179.0, -17.0, -179.0, -16.0))                         # antimeridian
        self.assertIsNone(box)
        self.assertEqual(point, (-16.5, -180.0))
        _, p = SC.search_params("sentinel-2", bbox=(-0.5, 51.2, 0.3, 51.8), now=self.NOW)
        self.assertEqual(p["bbox"], "-0.5,51.2,0.3,51.8")
        self.assertNotIn("intersects", p)
        with self.assertRaises(ValueError):
            SC.area()

    def test_url_and_id_rules(self):
        ok = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/30/U/XC/2026/11/x/preview.jpg"
        self.assertTrue(SC.allowed_url("sentinel-2", ok))
        for bad in (ok.replace("https:", "http:"), ok.replace("sentinel-cogs.s3", "evil.s3"),
                    "https://user:pw@sentinel-cogs.s3.us-west-2.amazonaws.com/x.jpg",
                    "https://sentinel-cogs.s3.us-west-2.amazonaws.com:8443/x.jpg",
                    "https://sentinel-cogs.s3.us-west-2.amazonaws.com:x/x.jpg",
                    "https://sentinel-cogs.s3.us-west-2.amazonaws.com\\@evil.example/x.jpg", None):
            self.assertFalse(SC.allowed_url("sentinel-2", bad), bad)
        self.assertFalse(SC.allowed_url("sentinel-1", ok))            # each mission has its own hosts
        self.assertTrue(SC.allowed_url("sentinel-1", "https://zipper.creodias.eu/odata/v1/Assets(a-b)/$value"))
        self.assertTrue(SC.valid_id("S2B_30UXC_20261123_0_L2A"))
        for bad in ("../../etc/passwd", "a/b", "abc", "x" * 200, "S2B..X_20261123", ""):
            self.assertFalse(SC.valid_id(bad), bad)


class SceneParserTests(unittest.TestCase):
    def test_sentinel2(self):
        out = SC.parse_items("sentinel-2", plus("stac_e84_s2.json"), point=LONDON)
        self.assertEqual([s["id"] for s in out], ["S2B_30UXC_20261123_0_L2A", "S2C_30UXC_20261121_0_L2A"])
        s = out[0]
        self.assertEqual((s["platform"], s["cloud"], s["detail"]), ("Sentinel-2B", 67.3, "Tile 30UXC"))
        self.assertEqual(s["datetime"], "2026-11-23T11:06:53Z")
        self.assertTrue(s["covers_point"])
        self.assertEqual(s["geometry"]["type"], "Polygon")
        self.assertEqual(len(s["bbox"]), 4)
        self.assertTrue(s["_quicklook_href"].endswith("/S2B_30UXC_20261123_0_L2A/preview.jpg"))
        self.assertEqual(s["quicklook"], "/api/space/scenes/quicklook/sentinel-2/S2B_30UXC_20261123_0_L2A")
        viewer = urllib.parse.urlsplit(s["links"][0]["href"])
        q = urllib.parse.parse_qs(viewer.query)
        self.assertEqual((viewer.hostname, q["datasetId"][0], q["fromTime"][0][:10]),
                         ("browser.dataspace.copernicus.eu", "S2_L2A_CDAS", "2026-11-23"))
        self.assertTrue(s["links"][-1]["href"].startswith("https://earth-search.aws.element84.com/v1/collections/"))

    def test_sentinel1(self):
        out = SC.parse_items("sentinel-1", plus("stac_cdse_s1.json"), point=LONDON)
        self.assertEqual(len(out), 3)
        first = out[0]
        self.assertEqual((first["platform"], first["detail"], first["cloud"]), ("Sentinel-1C", "IW, VV+VH, descending", None))
        self.assertTrue(first["_quicklook_href"].startswith("https://datahub.creodias.eu/odata/v1/Assets("))
        evil = next(s for s in out if s["id"].endswith("_EVIL"))
        self.assertIsNone(evil["_quicklook_href"])                     # thumbnail on a host that is not allowed
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(first["links"][0]["href"]).query)
        self.assertEqual(q["datasetId"], ["S1_CDAS_IW_VVVH"])

    def test_landsat(self):
        out = SC.parse_items("landsat", plus("stac_pc_landsat.json"), point=LONDON)
        s = out[0]
        self.assertEqual((s["id"], s["platform"], s["cloud"], s["detail"]),
                         ("LC09_L2SP_201024_20261118_02_T1", "Landsat 9", 57.9, "Path 201, row 24"))
        href = urllib.parse.urlsplit(s["_quicklook_href"])
        q = urllib.parse.parse_qs(href.query)
        self.assertEqual((href.hostname, href.path), ("planetarycomputer.microsoft.com", "/api/data/v1/item/preview.jpg"))
        self.assertEqual((q["item"], q["collection"], q["max_size"]), ([s["id"]], ["landsat-c2-l2"], ["360"]))
        texts = [l["text"] for l in s["links"]]
        self.assertEqual(texts, ["USGS LandsatLook record", "Planetary Computer viewer", "Catalogue record"])
        self.assertIn("/stac-browser/collections/landsat-c2l2-sr/items/", s["links"][0]["href"])

    def test_bad_documents(self):
        with self.assertRaises(ValueError):
            SC.parse_items("sentinel-2", {"type": "error", "description": "Invalid query"})
        self.assertEqual(SC.parse_items("sentinel-2", {"features": [None, 3, {"id": "x"}]}), [])


class SceneServiceTests(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.status = []
        self.svc = SC.Scenes(self.net, logging.getLogger("meridian.test.spaceplus.scenes"),
                             status=lambda mission, **kw: self.status.append((mission, kw.get("ok"))))

    def test_search_merges_and_caches(self):
        res = self.svc.search(point=LONDON, days=30, max_cloud=60)
        self.assertEqual(set(res["missions"]), set(SC.ORDER))
        self.assertTrue(all(r["ok"] for r in res["missions"].values()))
        times = [s["time"] for s in res["scenes"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(res["scenes"][0]["id"], "S2B_30UXC_20261123_0_L2A")
        self.assertNotIn("_quicklook_href", res["scenes"][0])           # server-side only
        evil = next(s for s in res["scenes"] if s["id"].endswith("_EVIL"))
        self.assertIsNone(evil["quicklook"])
        self.assertEqual(len(res["credits"]), 3)
        n = len(self.net.calls)
        again = self.svc.search(point=LONDON, days=30, max_cloud=60)
        self.assertEqual(len(self.net.calls), n)                        # an hour's cache per place and settings
        self.assertTrue(all(r["cached"] for r in again["missions"].values()))
        self.svc.search(point=LONDON, days=30, max_cloud=30)
        self.assertEqual(len(self.net.calls), n + 3)                    # new settings: one request per catalogue

    def test_one_catalogue_down(self):
        self.net.fail.add("stac.dataspace.copernicus.eu")
        res = self.svc.search(point=LONDON, missions=["sentinel-1", "landsat"])
        self.assertEqual(list(res["missions"]), ["sentinel-1", "landsat"])
        self.assertFalse(res["missions"]["sentinel-1"]["ok"])
        self.assertIn("Copernicus Data Space Ecosystem", res["missions"]["sentinel-1"]["error"])
        self.assertTrue(res["missions"]["landsat"]["ok"])
        self.assertIn(("sentinel-1", False), self.status)
        self.net.fail.clear()
        res = self.svc.search(point=LONDON, missions=["sentinel-1"])
        self.assertTrue(res["missions"]["sentinel-1"]["ok"])            # failures are not cached

    def test_quicklook_redirect_chain_and_cache(self):
        res = self.svc.search(point=LONDON, missions=["sentinel-1"])
        s = res["scenes"][0]
        href = s["links"] and self.svc._hrefs[("sentinel-1", s["id"])]
        zipper = href.replace("datahub.creodias.eu", "zipper.creodias.eu")
        self.net.images[href] = (301, {"location": zipper}, b"<html>moved</html>")
        self.net.images[zipper] = (200, {"content-type": "application/octet-stream"}, PNG)
        body, kind = self.svc.quicklook("sentinel-1", s["id"])
        self.assertEqual((body, kind), (PNG, "image/png"))
        n = len(self.net.calls)
        self.svc.quicklook("sentinel-1", s["id"])
        self.assertEqual(len(self.net.calls), n)                        # cached for a day

    def test_quicklook_refuses_other_hosts_and_non_images(self):
        res = self.svc.search(point=LONDON, missions=["sentinel-1"])
        a, b = res["scenes"][0]["id"], res["scenes"][1]["id"]
        href_a = self.svc._hrefs[("sentinel-1", a)]
        self.net.images[href_a] = (302, {"location": "https://evil.example/steal"}, b"")
        with self.assertRaises(SC.SceneError) as ctx:
            self.svc.quicklook("sentinel-1", a)
        self.assertEqual(ctx.exception.status, 502)
        self.assertFalse(any("evil.example" in c for c in self.net.calls))   # never requested
        href_b = self.svc._hrefs[("sentinel-1", b)]
        self.net.images[href_b] = (200, {}, b"<html>login</html>")
        with self.assertRaises(SC.SceneError):
            self.svc.quicklook("sentinel-1", b)
        n = len(self.net.calls)
        with self.assertRaises(SC.SceneError):
            self.svc.quicklook("sentinel-1", b)                         # failed recently: no new request
        self.assertEqual(len(self.net.calls), n)
        for mission, item in (("mars", "S2B_30UXC_20261123_0_L2A"), ("sentinel-2", "../x/y")):
            with self.assertRaises(SC.SceneError) as ctx:
                self.svc.quicklook(mission, item)
            self.assertEqual(ctx.exception.status, 400)

    def test_quicklook_after_restart_uses_the_item_record(self):
        item = plus("stac_cdse_s1.json")["features"][0]
        href = item["assets"]["thumbnail"]["href"]
        self.net.images[href] = (200, {}, PNG)
        body, kind = self.svc.quicklook("sentinel-1", item["id"])         # no search in this process yet
        self.assertEqual(kind, "image/png")
        self.assertEqual(self.net.count("/collections/sentinel-1-grd/items/"), 1)
        with self.assertRaises(SC.SceneError) as ctx:
            self.svc.quicklook("sentinel-1", "S1C_IW_GRDH_1SDV_20991121T060600_UNKNOWN_COG")
        self.assertEqual(ctx.exception.status, 404)


# ---------------------------------------------------------------------------------------------
# The module end to end (routes, AI tool, declarations)
# ---------------------------------------------------------------------------------------------
class SpacePlusModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MERIDIAN_DATA_DIR"] = cls.tmp.name
        log = logging.getLogger("meridian.test.spaceplus.module")
        cls.net = FakeNet()
        app = App(Config(None), Store(Path(cls.tmp.name) / "t.db"), cls.net, log, Scheduler(log), DatasetManager(log))
        from meridian.core_api import register_core
        from meridian.modules import load_all
        register_core(app)
        load_all(app, {"space"})
        assert app.modules["space"].ok, app.modules["space"].error
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        cls.app.store.close()
        os.environ.pop("MERIDIAN_DATA_DIR", None)
        try:
            cls.tmp.cleanup()
        except OSError:
            pass

    def call(self, path, **query):
        route, params, _ = self.app.match("GET", path)
        self.assertIsNotNone(route, path)
        return route.handler(Request("GET", path, params, {k: [str(v)] for k, v in query.items()}, {}))

    def get(self, path, **query):
        out = self.call(path, **query)
        return json.loads(out.body) if isinstance(out, Response) else json.loads(json.dumps(out))

    def status(self, path, **query):
        try:
            self.call(path, **query)
        except HTTPError as err:
            return err.status
        return 200

    def test_scenes_route(self):
        out = self.get("/api/space/scenes", lat=LONDON[0], lon=LONDON[1], days=30, max_cloud=60)
        self.assertEqual(out["point"], {"lat": 51.5072, "lon": -0.1276})
        self.assertEqual(len(out["scenes"]), 2 + 3 + 2)
        self.assertTrue(all(s["quicklook"] is None or s["quicklook"].startswith("/api/space/scenes/quicklook/")
                            for s in out["scenes"]))
        box = self.get("/api/space/scenes", bbox="-0.5,51.2,0.3,51.8", missions="sentinel-2")
        self.assertEqual((box["bbox"], box["point"]), ([-0.5, 51.2, 0.3, 51.8], None))
        self.assertEqual(list(box["missions"]), ["sentinel-2"])
        self.assertEqual(self.status("/api/space/scenes"), 400)
        self.assertEqual(self.status("/api/space/scenes", lat=95, lon=0), 400)
        self.assertEqual(self.status("/api/space/scenes", lat=1, lon=1, missions="sentinel-3"), 400)
        self.assertEqual(self.status("/api/space/scenes", lat=1, lon=1, days=500), 400)
        self.assertEqual(self.status("/api/space/scenes", lat=1, lon=1, bbox="1,2,3"), 400)

    def test_all_catalogues_down_is_502(self):
        self.net.fail.update({"earth-search", "stac.dataspace", "planetarycomputer"})
        try:
            self.assertEqual(self.status("/api/space/scenes", lat=10.123, lon=20.456), 502)
        finally:
            self.net.fail.clear()

    def test_quicklook_route(self):
        item = plus("stac_e84_s2.json")["features"][0]
        self.net.images[item["assets"]["thumbnail"]["href"]] = (200, {"content-type": "image/jpeg"}, JPEG)
        self.get("/api/space/scenes", lat=LONDON[0], lon=LONDON[1], missions="sentinel-2")
        resp = self.call(f"/api/space/scenes/quicklook/sentinel-2/{item['id']}")
        self.assertIsInstance(resp, Response)
        self.assertEqual((resp.content_type, resp.cache, resp.body), ("image/jpeg", "max-age=86400", JPEG))
        self.assertEqual(self.status("/api/space/scenes/quicklook/sentinel-9/S2B_30UXC_20261123_0_L2A"), 400)
        self.assertEqual(self.status("/api/space/scenes/quicklook/sentinel-2/..%2f"), 400)

    def test_gibs_meta_and_catalogue_routes(self):
        meta = self.get("/api/space/gibs", meta=1)
        self.assertIn("compare", meta)
        self.assertEqual(meta["products"]["lightning"]["layers"][0]["provider"], "eumetsat")
        self.assertTrue(meta["meteosat_credit"].startswith("EUMETSAT Meteosat product"))
        cat = self.get("/api/space/gibs", products="lightning,flood")
        self.assertEqual(cat["products"]["lightning"]["layers"][0]["latest"], "2026-11-23T16:00:00Z")
        self.assertEqual([v["id"] for v in cat["products"]["flood"]["variants"]], ["d2", "d1", "d3"])
        many = ",".join(list(I.PRODUCTS)[:25])
        self.assertEqual(self.status("/api/space/gibs", products=many), 200)

    def test_ai_tool(self):
        out = self.app.call_ai_tool("latest_scenes", {"lat": LONDON[0], "lon": LONDON[1], "days": 30})
        self.assertEqual(set(out["missions"]), set(SC.ORDER))
        s2 = out["missions"]["sentinel-2"]["scenes"][0]
        self.assertEqual((s2["satellite"], s2["time_utc"], s2["cloud_cover_pct"]),
                         ("Sentinel-2B", "2026-11-23T11:06:53Z", 67.3))
        self.assertTrue(s2["viewer"].startswith("https://browser.dataspace.copernicus.eu/"))
        self.assertTrue(out["credits"])
        one = self.app.call_ai_tool("latest_scenes", {"lat": 10, "lon": 10, "missions": "landsat"})
        self.assertEqual(list(one["missions"]), ["landsat"])
        for bad in ({"lat": 100, "lon": 0}, {"lat": 1}, {"lat": 1, "lon": 1, "missions": ["spot"]},
                    {"lat": 1, "lon": 1, "days": 0}):
            self.assertIn("error", self.app.call_ai_tool("latest_scenes", bad), bad)

    def test_sources_and_layers(self):
        srcs = {s["id"]: s for s in self.app.sources.values() if s.get("module") == "space"}
        for sid in ("space.eumetsat", "space.earthsearch", "space.cdse", "space.planetary"):
            self.assertTrue(srcs[sid]["licence"], sid)
        self.assertIn("CC BY 4.0", srcs["space.eumetsat"]["licence"])
        layers = {l["id"]: l for l in self.app.layer_specs if l["module"] == "space"}
        for lid in ("space.no2", "space.so2", "space.seaice", "space.lightning"):
            self.assertEqual(layers[lid]["group"], "Weather", lid)
        for lid in ("space.nightdaily", "space.flood", "space.sar", "space.meteosat", "space.scenes"):
            self.assertEqual(layers[lid]["group"], "Imagery", lid)
        self.assertEqual(layers["space.lightning"]["refresh"], "every 5 min")


if __name__ == "__main__":
    unittest.main()
