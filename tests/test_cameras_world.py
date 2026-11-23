"""Camera adapters for Asia, Oceania, Latin America, Africa, the Middle East and Antarctica
(``sources/ap_*``, ``la_*``, ``af_*`` and ``me_*``): each parser against a small fixture (real answers
trimmed, or synthetic records in the documented shape), keys, curated tables and shared helpers.

No network: fixtures live in tests/fixtures/cameras/world_*, fetches go through fakes.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meridian.modules.cameras import build as build_mod
from meridian.modules.cameras.common import FetchContext, NeedsKey, Source
from meridian.modules.cameras.proxy import host_allowed
from meridian.modules.cameras.sources import (af_sanral, ap_antarctic, ap_common, ap_geonet, ap_hko, ap_jma, ap_jpriver,
                                              ap_nsw, ap_qld, ap_taiwan, la_dgac, la_volcanoes)
from meridian.net import NetError

FIX = Path(__file__).parent / "fixtures" / "cameras"
WORLD_PREFIXES = ("ap_", "la_", "af_", "me_")


def fx_json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def fx_bytes(name):
    return (FIX / name).read_bytes()


def world_sources() -> list[Source]:
    from meridian.modules.cameras import sources
    return [s for s in sources.ALL if getattr(s.fetch, "__module__", "").rsplit(".", 1)[-1].startswith(WORLD_PREFIXES)]


class FakeDS:
    def __init__(self, root: Path):
        self.raw_dir = root
        self.force = False
        self.max_age_hours = 24.0

    def raw(self, name):
        p = self.raw_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


class Config:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, name, default=None):
        return self.values.get(name, default)

    def redact(self, text):
        return text


class FetchCase(unittest.TestCase):
    """A FetchContext whose downloads are answered from a dict of url-substring -> JSON value."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fc(self, src: Source, answers: dict | None = None, keys: dict | None = None) -> FetchContext:
        fc = FetchContext(FakeDS(self.tmp), src, config=Config(keys), net=mock.Mock(), log=mock.Mock())
        fc.urls = []

        def fake_json(url, filename, **kw):
            fc.urls.append(url)
            for part, value in (answers or {}).items():
                if part in url:
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise NetError("no fixture for this URL", status=404)

        fc.json = fake_json
        return fc


# ---------------------------------------------------------------------------------------------
class HelperTests(unittest.TestCase):
    def test_bilingual(self):
        b = ap_common.bilingual
        self.assertEqual(b("臺北車站", "Taipei Main Station"), "Taipei Main Station (臺北車站)")
        self.assertEqual(b("臺北車站", ""), "臺北車站")
        self.assertEqual(b("", "Taipei"), "Taipei")
        self.assertEqual(b("Osaka", "osaka"), "Osaka")
        self.assertEqual(b("Ruta 5", "Ruta 5 Norte"), "Ruta 5 Norte")      # original already inside

    def test_first_and_features(self):
        self.assertEqual(ap_common.first({"a": "", "b": None, "c": 0, "d": "x"}, "a", "b", "c", "d"), 0)
        self.assertIsNone(ap_common.first({"a": " "}, "a", "z"))
        fc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": "f1", "geometry": {"type": "Point", "coordinates": [151.2, -33.8]},
             "properties": {"title": "A"}},
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[1, 2], [3, 4]]}, "properties": {}},
            "junk"]}
        got = list(ap_common.features(fc))
        self.assertEqual(got[0], ({"title": "A", "id": "f1"}, -33.8, 151.2))
        self.assertEqual(got[1][1:], (None, None))
        self.assertEqual(len(got), 2)
        self.assertEqual(list(ap_common.features(None)), [])

    def test_toward_and_curated(self):
        self.assertEqual(ap_common.toward(0.0, 0.0, (1.0, 0.0)), 0.0)
        self.assertEqual(ap_common.toward(0.0, 0.0, (0.0, 1.0)), 90.0)
        self.assertIsNone(ap_common.toward(0.0, 0.0, None))
        src = Source(id="t", name="T", owner="o", url="u", licence="L", attribution="A", country="NZ")
        rows, skipped = ap_common.curated(src, [
            {"id": "a", "name": "A", "lat": -39.0, "lon": 175.0, "image": "https://img.test/a.jpg", "target": (-39.1, 175.0)},
            {"id": "b", "name": "No position", "lat": None, "lon": None, "image": "https://img.test/b.jpg"}])
        self.assertEqual((len(rows), skipped), (1, 1))
        self.assertEqual(rows[0]["heading"], 180.0)


# ---------------------------------------------------------------------------------------------
class OceaniaTests(FetchCase):
    def test_nsw(self):
        rows, skipped = ap_nsw.parse(fx_json("world_nsw.json"), ap_nsw.SOURCE)
        self.assertEqual((len(rows), skipped), (3, 1))           # the Victoria Pass project photo is not a camera
        first = rows[0]
        self.assertEqual(first["id"], "aunsw:023651ee-389c-4677-978e-d39b6c24c1e7")
        self.assertEqual((first["area"], first["region"], first["country"]), ("Sydney", "New South Wales", "AU"))
        self.assertEqual(first["heading"], 270)
        self.assertIn("looking west", first["description"])
        self.assertEqual([r["area"] for r in rows], ["Sydney", "Northern NSW", "Sydney"])
        self.assertEqual(rows[2]["heading"], 45)                 # "N-E"
        self.assertTrue(all(host_allowed(r["image"], ap_nsw.SOURCE) for r in rows))
        self.assertEqual(ap_nsw.area_of("REG_SOUTH"), "Southern NSW")

    def test_qld_public_key_own_key_and_quota(self):
        doc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [153.02, -27.47]},
             "properties": {"id": 12, "description": "Riverside Expressway, looking north", "direction": "North",
                            "district": "Metropolitan", "locality": "BRISBANE CITY",
                            "image_url": "https://webcams.qldtraffic.qld.gov.au/Metropolitan/riverside.jpg",
                            "url": "https://qldtraffic.qld.gov.au/webcams/12"}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [153.1, -27.5]},
             "properties": {"id": 13, "description": "Offline", "status": "Inactive",
                            "image_url": "https://webcams.qldtraffic.qld.gov.au/x.jpg"}}]}
        rows, skipped = ap_qld.parse(doc, ap_qld.SOURCE)
        self.assertEqual((len(rows), skipped), (1, 1))
        self.assertEqual((rows[0]["area"], rows[0]["region"], rows[0]["heading"]), ("Brisbane City", "Metropolitan", 0))
        self.assertTrue(host_allowed(rows[0]["image"], ap_qld.SOURCE))
        fc = self.fc(ap_qld.SOURCE, {"/v1/webcams": doc})
        self.assertEqual(len(ap_qld.fetch(fc)), 1)
        self.assertIn(f"apikey={ap_qld.PUBLIC_KEY}", fc.urls[0])                     # the key in the docs
        fc = self.fc(ap_qld.SOURCE, {"/v1/webcams": doc}, keys={"QLDTRAFFIC_KEY": "mine123456"})
        ap_qld.fetch(fc)
        self.assertIn("apikey=mine123456", fc.urls[0])
        fc = self.fc(ap_qld.SOURCE, {"/v1/webcams": NetError("api.qldtraffic.qld.gov.au: HTTP 429", status=429)})
        with self.assertRaises(RuntimeError) as cm:
            ap_qld.fetch(fc)
        self.assertIn("QLDTRAFFIC_KEY", str(cm.exception))
        self.assertNotIn(ap_qld.PUBLIC_KEY, str(cm.exception))

    def test_geonet(self):
        rows, skipped = ap_geonet.parse(fx_json("world_geonet.geojson"), ap_geonet.SOURCE)
        self.assertEqual(sorted(r["id"] for r in rows),
                         ["nzgeonet:ngauruhoe", "nzgeonet:ruapehueast", "nzgeonet:ruapehunorth", "nzgeonet:whakatane"])
        self.assertEqual(skipped, len(ap_geonet.CAMERAS) - 4)           # sites the answer does not list as open
        north = next(r for r in rows if r["id"] == "nzgeonet:ruapehunorth")
        self.assertEqual(north["area"], "Discovery Lodge")
        self.assertTrue(120 < north["heading"] < 160)                   # looking south-east at the summit
        self.assertEqual(north["type"], "webcam")
        self.assertIn("volcano", north["tags"])
        self.assertTrue(north["image"].endswith("/latest/ruapehunorth.jpg"))
        # without the site list every camera still appears at its checked position
        rows, skipped = ap_geonet.parse(None, ap_geonet.SOURCE)
        self.assertEqual((len(rows), skipped), (len(ap_geonet.CAMERAS), 0))
        fc = self.fc(ap_geonet.SOURCE, {"api.geonet.org.nz": NetError("api.geonet.org.nz: timed out")})
        self.assertEqual(len(ap_geonet.fetch(fc)), len(ap_geonet.CAMERAS))
        self.assertTrue(fc.notes)

    def test_antarctic_stations(self):
        for src, n in ((ap_antarctic.AAD, 3), (ap_antarctic.BAS, 1)):
            rows = src.fetch(self.fc(src))
            self.assertEqual(len(rows), n)
            for r in rows:
                self.assertEqual((r["country"], r["type"]), ("AQ", "webcam"))
                self.assertIn("polar", r["tags"])
                self.assertTrue(r["lat"] < -60)
                self.assertTrue(host_allowed(r["image"], src))
                self.assertTrue(r["image"].endswith("latest.jpg"))


class AfricaTests(FetchCase):
    def test_sanral_needs_key_then_parses_511_shape(self):
        with self.assertRaises(NeedsKey):
            af_sanral.fetch(self.fc(af_sanral.SOURCE))
        doc = [{"Id": 101, "Roadway": "N1", "Direction": "Northbound", "Latitude": -26.05, "Longitude": 28.0,
                "Location": "N1 at Buccleuch", "Views": [
                    {"Id": 5, "Url": "https://www.i-traffic.co.za/map/Cctv/5", "Status": "Enabled", "Description": "North"},
                    {"Id": 6, "Url": "https://www.i-traffic.co.za/map/Cctv/6", "Status": "Disabled", "Description": "South"}]}]
        fc = self.fc(af_sanral.SOURCE, {"/api/v2/get/cameras": doc}, keys={"SANRAL_ITRAFFIC_KEY": "k123456"})
        rows = af_sanral.fetch(fc)
        self.assertEqual((len(rows), fc.skipped), (1, 1))
        self.assertEqual((rows[0]["country"], rows[0]["road"], rows[0]["heading"]), ("ZA", "N1", 0))
        self.assertTrue(host_allowed(rows[0]["image"], af_sanral.SOURCE))
        self.assertIn("key=k123456", fc.urls[0])


class EastAsiaTests(FetchCase):
    def test_taiwan_highways(self):
        rows, skipped = ap_taiwan.parse_thb(fx_bytes("world_thb.xml"), ap_taiwan.THB)
        self.assertEqual((len(rows), skipped), (3, 1))           # the bureau's "hidden by default" sign camera
        names = [r["name"] for r in rows]
        self.assertEqual(names[0], "Expressway 62 km 9.02 (快速公路62號(暖暖交流道到大華系統交流道))")
        self.assertTrue(names[1].startswith("Provincial Highway 9 km 56.4 ("))
        self.assertEqual(names[2], "平鎮區延平路快速路口北")                  # a city road: no English name to add
        self.assertEqual([r["road"] for r in rows], ["Expressway 62", "Provincial Highway 9", "延平路三段"])
        self.assertEqual([r["heading"] for r in rows], [270, 180, 225])
        self.assertTrue(all(host_allowed(r["image"], ap_taiwan.THB) and r["image"].endswith("/snapshot") for r in rows))
        self.assertEqual(ap_taiwan.road_en("台2丙線"), "Provincial Highway 2C")
        self.assertEqual(ap_taiwan.km_of("6k+66"), 6.66)
        self.assertIsNone(ap_taiwan.km_of("unknown"))
        self.assertEqual(ap_taiwan.county_en("台中市"), "Taichung City")

    def test_taiwan_rivers_and_debris_flows(self):
        listing = fx_json("world_wra.json")
        rows, skipped = ap_taiwan.parse_wra(listing, ap_taiwan.WRA)
        self.assertEqual((len(rows), skipped), (2, 1))           # status 0 is off
        self.assertEqual((rows[0]["region"], rows[0]["road"], rows[0]["type"]), ("Hualien County", "馬鞍溪", "webcam"))
        # one image script serves every camera, so the link is looked up when the picture is asked for
        self.assertTrue(all(r.get("dynamic") and "image" not in r for r in rows))
        net = mock.Mock()
        net.get_json.return_value = listing
        url = ap_taiwan.resolve_wra(rows[1], net)
        self.assertEqual(url, listing[1]["imageurl"])
        self.assertTrue(host_allowed(url, ap_taiwan.WRA))
        self.assertEqual(net.get_json.call_args.kwargs["ttl"], 3600)
        self.assertIsNone(ap_taiwan.resolve_wra({"id": "twwra:nope"}, net))
        net.get_json.return_value = [{"cameraid": listing[1]["cameraid"], "imageurl": "https://elsewhere.test/a.png"}]
        self.assertIsNone(ap_taiwan.resolve_wra(rows[1], net))
        rows, skipped = ap_taiwan.parse_debris(fx_json("world_debris.json"), ap_taiwan.DEBRIS)
        self.assertEqual((len(rows), skipped), (2, 1))           # a link without station and camera numbers
        self.assertEqual((rows[0]["id"], rows[0]["region"]), ("twdebris:7-2", "New Taipei City"))
        url = ap_taiwan.resolve_debris(rows[0], None)
        self.assertEqual(url, "https://dfm.ardswc.gov.tw/debrisFinal/showone.asp?SID=7&CID=2")
        self.assertTrue(host_allowed(url, ap_taiwan.DEBRIS))
        self.assertIsNone(ap_taiwan.resolve_debris({"id": "twdebris:7"}, None))
        # distinct cameras survive the build's duplicate check (which ignores query strings)
        kept, dupes = build_mod.dedupe(rows)
        self.assertEqual((len(kept), dupes), (2, 0))

    def test_hong_kong_observatory(self):
        rows, skipped = ap_hko.parse(fx_bytes("world_hko.js").decode("utf-8"), ap_hko.SOURCE)
        self.assertEqual((len(rows), skipped), (7, 0))
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id["hkobs:hk2"]["name"], "Harbour Point (港角), looking west")
        self.assertEqual(by_id["hkobs:wl2"]["heading"], 337.5)            # from the compass code "nnw"
        # cs2 is listed at the tower's position; it takes its same-named sibling's instead
        self.assertEqual((by_id["hkobs:cs2"]["lat"], by_id["hkobs:cs2"]["lon"]),
                         (by_id["hkobs:cs1"]["lat"], by_id["hkobs:cs1"]["lon"]))
        self.assertNotEqual(by_id["hkobs:ic1"]["lat"], by_id["hkobs:cs1"]["lat"])
        self.assertEqual(by_id["hkobs:hko"]["image"], "https://www.hko.gov.hk/wxinfo/aws/hko_mica/hko/latest_HKO.jpg")
        self.assertTrue(all(host_allowed(r["image"], ap_hko.SOURCE) for r in rows))
        self.assertEqual(ap_hko.cameras_of("var x = 1;"), [])

    def test_jma_volcano_cameras(self):
        data = fx_json("world_jma.geojson")
        page = fx_bytes("world_jma_page.html").decode("utf-8")
        rows, skipped = ap_jma.parse(data, {"31401": page}, ap_jma.SOURCE)
        self.assertEqual((len(rows), skipped), (1, 2))           # cameras whose page is not read yet wait
        cam = rows[0]
        self.assertEqual(cam["name"], "Mount Fuji (富士山 萩原)")
        self.assertEqual(cam["refresh"], 120)                      # frames two minutes apart
        self.assertTrue(cam.get("dynamic") and "image" not in cam)
        self.assertEqual(ap_jma.split_name("<span>阿蘇山 <ruby><rb>車帰</rb><rt>くるまがえり</rt></ruby></span>"),
                         ("阿蘇山", "車帰"))
        self.assertEqual(ap_jma.interval_of(["20261123203001", "20261123202005"]), 596)
        # the newest frame listed on the camera's page (frame names have varying seconds)
        net = mock.Mock()
        net.get_text.return_value = page
        url = ap_jma.resolve(cam, net)
        self.assertTrue(url.endswith("/camera/314_82008084/20261123203200.jpg"), url)
        self.assertIn("VC=31401", net.get_text.call_args.args[0])
        self.assertTrue(host_allowed(url, ap_jma.SOURCE))
        self.assertIsNone(ap_jma.resolve({"id": "jpvolcano:../x"}, net))
        net.get_text.return_value = "<html>no frames</html>"
        self.assertIsNone(ap_jma.resolve(cam, net))

    def test_mlit_river_cameras(self):
        rows, skipped = ap_jpriver.parse(fx_json("world_jpriver_1301.json"), 1301, ap_jpriver.SOURCE)
        self.assertEqual((len(rows), skipped), (3, 1))           # a paused camera is left out
        direct = [r for r in rows if r.get("image")]
        looked_up = [r for r in rows if r.get("dynamic")]
        self.assertEqual((len(direct), len(looked_up)), (2, 1))
        self.assertTrue(all(r["region"] == "Tokyo" for r in rows))
        self.assertEqual(ap_jpriver.prefecture_of(102), "Hokkaido")
        self.assertEqual(ap_jpriver.pref_codes(fx_json("world_jpriver_prefs.json")), [101, 1301])
        net = mock.Mock()
        net.get_json.return_value = fx_json("world_jpriver_record.json")
        url = ap_jpriver.resolve(looked_up[0], net)
        self.assertEqual(url, "https://cam.river.go.jp/cam/now/cctv_130001_31C03351.jpg")
        self.assertEqual(net.get_json.call_args.kwargs["ttl"], 86400)       # a record is looked up once a day
        self.assertTrue(host_allowed(url, ap_jpriver.SOURCE))
        self.assertEqual(ap_jpriver.resolve(direct[0], net), direct[0]["image"])
        net.get_json.return_value = {"obsInfo": {"currProvUrl": "https://elsewhere.test/a.jpg"}}
        self.assertIsNone(ap_jpriver.resolve(looked_up[0], net))
        # the directory: index plus one file per prefecture, files reused for 90 days; a missing file is skipped
        fc = self.fc(ap_jpriver.SOURCE, {"prefarea.json": fx_json("world_jpriver_prefs.json"),
                                         "scam/101.json": NetError("www.river.go.jp: HTTP 404", status=404),
                                         "scam/1301.json": fx_json("world_jpriver_1301.json")})
        calls = []
        real = fc.json
        fc.json = lambda url, filename, **kw: (calls.append(kw), real(url, filename, **kw))[1]
        self.assertEqual(len(ap_jpriver.fetch(fc)), 3)
        self.assertTrue(all(kw["max_age_hours"] >= 90 * 24 and kw["min_interval"] >= 2.0 for kw in calls))


class LatinAmericaTests(FetchCase):
    def test_dgac_aerodromes(self):
        rows, skipped, unknown = la_dgac.parse(fx_bytes("world_dgac.html").decode("utf-8"), la_dgac.SOURCE)
        self.assertEqual(len(rows), 4)
        self.assertEqual(unknown, {"ZZZZ"})                      # an aerodrome without a known position
        self.assertEqual(skipped, 4)                             # its two cameras plus two out of service
        self.assertEqual(rows[0]["name"], "Testfield aerodrome (SCAR), looking south-west")
        self.assertEqual((rows[0]["heading"], rows[0]["country"], rows[0]["area"]), (225, "CL", "Test City"))
        self.assertTrue(all(r.get("dynamic") and "image" not in r for r in rows))
        self.assertEqual(la_dgac.aerodrome_name("Arturo Merino Benitez (INTL) (PUB) (SCEL)"), "Arturo Merino Benitez")
        self.assertEqual(la_dgac.AERODROMES["SCRM"][2], "AQ")      # King George Island, Antarctica
        page = fx_bytes("world_dgac_camera.html").decode("utf-8")
        newest = la_dgac.newest(page)
        self.assertTrue(newest.endswith("/cam01_0001.jpg"))      # by its time, not its file name
        self.assertTrue(host_allowed(newest, la_dgac.SOURCE))
        self.assertIsNone(la_dgac.newest("{'fecha':'1', 'imagen':'https://elsewhere.test/a.jpg'}"))
        net = mock.Mock()
        net.get_text.return_value = page
        self.assertEqual(la_dgac.resolve({"id": "cldgac:7"}, net), newest)
        self.assertIn("/camara/show/id/7", net.get_text.call_args.args[0])
        self.assertIsNone(la_dgac.resolve({"id": "cldgac:../x"}, net))

    def test_volcano_observatories(self):
        for src, n in ((la_volcanoes.OVSICORI, 7), (la_volcanoes.INSIVUMEH, 4), (la_volcanoes.INETER, 4)):
            rows = src.fetch(self.fc(src))
            self.assertEqual(len(rows), n, src.id)
            self.assertEqual(len({r["id"] for r in rows}), n)
            for r in rows:
                self.assertTrue(host_allowed(r["image"], src), r["image"])
                self.assertIn("volcano", r["tags"])
                self.assertEqual(r["type"], "webcam")
                self.assertIn("summit", r["description"])


# ---------------------------------------------------------------------------------------------
class SourceListTests(unittest.TestCase):
    def test_world_sources_are_complete(self):
        found = world_sources()
        self.assertGreaterEqual(len(found), 14)
        ids = {s.id for s in found}
        # "All rights reserved" feeds are not registered by default.
        self.assertNotIn("crovsicori", ids)
        self.assertNotIn("gtinsivumeh", ids)
        for s in found:
            self.assertRegex(s.id, r"^[a-z0-9]{1,16}$")
            self.assertTrue(s.name and s.owner and s.url and s.licence and s.attribution and s.licence_url, s.id)
            self.assertTrue(s.hosts, s.id)
            self.assertTrue(s.coverage, s.id)
            self.assertTrue(s.country, s.id)
            if s.needs_key:
                self.assertTrue(s.optional, s.id)
                self.assertRegex(s.needs_key, r"^[A-Z0-9_]+_KEY$")       # "KEY" in the name: redacted from logs

    def test_screened_build_keeps_world_rows(self):
        # rows from the adapters survive the build's allowlist screen unchanged
        rows, _ = ap_nsw.parse(fx_json("world_nsw.json"), ap_nsw.SOURCE)
        kept, dropped = build_mod.screen_rows(rows, ap_nsw.SOURCE)
        self.assertEqual((len(kept), dropped), (len(rows), 0))


if __name__ == "__main__":
    unittest.main()
