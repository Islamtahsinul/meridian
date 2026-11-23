"""North American camera sources (``meridian/modules/cameras/sources/na_*.py``): every adapter against
a small fixture in tests/fixtures/cameras/na_*, the keyed sources' key handling (no key leaves the
server or reaches the camera list), the shared helpers, and allowlist coverage of every row.

No network: fixtures only, HTTP goes through fakes.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from meridian.config import Config
from meridian.modules.cameras import build as build_mod
from meridian.modules.cameras import common
from meridian.modules.cameras.common import NeedsKey
from meridian.modules.cameras.proxy import check_url, host_allowed
from meridian.modules.cameras.sources import (na_canada, na_common, na_ibi, na_iteris, na_keyed, na_us,
                                              na_usgs)

FIX = Path(__file__).parent / "fixtures" / "cameras"
NOW = 1795431600.0          # 2026-11-23 10:20 UTC, matches the fixtures' picture times


def fx_json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def na_sources():
    return list(na_ibi.SOURCES) + list(na_keyed.SOURCES) + list(na_iteris.SOURCES) + list(na_canada.SOURCES) \
        + list(na_us.SOURCES) + [na_usgs.SOURCE]


class FakeDS:
    """Enough of a BuildContext for FetchContext."""

    def __init__(self, root: Path):
        self.raw_dir = root
        self.force = False
        self.max_age_hours = 24.0

    def raw(self, name):
        p = self.raw_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


class FakeNet:
    """Serves fixture files for downloads by URL substring and records what was asked."""

    def __init__(self, routes: dict[str, bytes], config=None):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []
        self.config = config

    def download(self, url, dest, **kw):
        self.calls.append((url, kw.get("headers") or {}))
        for part, body in self.routes.items():
            if part in url:
                Path(dest).write_bytes(body)
                return Path(dest)
        raise AssertionError(f"unexpected download {url}")


def assert_allowed(test: unittest.TestCase, rows: list[dict], src):
    """Every image link is on the source's allowlist (the build would drop it otherwise)."""
    for r in rows:
        if r.get("image"):
            test.assertTrue(host_allowed(r["image"], src), f"{src.id}: {r['image']} is not on the allowlist")


# ---------------------------------------------------------------------------------------------
class HelperTests(unittest.TestCase):
    def test_view_labels(self):
        v = na_common.view_label
        self.assertEqual(v("Camera Looking North Zoom"), "facing north, zoomed")
        self.assertEqual(v("Southeast Horizon"), "facing southeast, horizon")
        self.assertEqual(v("Road Surface View"), "road surface view")
        self.assertEqual(v("S Intersection"), "south intersection")
        self.assertEqual(v("I-29 at MP 5"), "I-29 at MP 5")

    def test_s3_path_style(self):
        f = na_common.s3_path_style
        self.assertEqual(f("https://usgs-nims-images.s3.amazonaws.com/720/A/A_newest.jpg"),
                         "https://s3.amazonaws.com/usgs-nims-images/720/A/A_newest.jpg")
        self.assertEqual(f("https://b-1.s3.us-west-2.amazonaws.com/k.jpg"), "https://s3.us-west-2.amazonaws.com/b-1/k.jpg")
        self.assertEqual(f("https://example.com/k.jpg"), "https://example.com/k.jpg")
        self.assertIsNone(f(None))

    def test_html_images(self):
        page = ('<img src="https://vancouver.ca/logo.png" alt="City"><IMG SRC="cameraimages/A-north.jpg" ALT="A - North">'
                '<img src="cameraimages/A-north.jpg" alt="again"><img src="javascript:x" alt="no">'
                '<img src="/cameraimages/B.jpg">')
        got = na_common.html_images(page, "https://trafficcams.vancouver.ca/a.htm", path_prefix="/cameraimages/")
        self.assertEqual(got, [("https://trafficcams.vancouver.ca/cameraimages/A-north.jpg", "A - North"),
                               ("https://trafficcams.vancouver.ca/cameraimages/B.jpg", "")])
        self.assertEqual(na_common.html_images("<img", "https://x.test/"), [])

    def test_template_resolver_only_takes_plain_ids(self):
        res = na_common.template_resolver("https://x.test/h.ashx?id={id}", r"[A-Za-z0-9_.-]{1,40}\.jpg")
        self.assertEqual(res({"id": "src:AB_1.jpg"}, None), "https://x.test/h.ashx?id=AB_1.jpg")
        for bad in ("src:../x.jpg", "src:a.jpg&x=1", "src:a.png", "src:", "nocolon"):
            self.assertIsNone(res({"id": bad}, None), bad)

    def test_state_names(self):
        self.assertEqual(na_common.state_name("tx"), ("US", "Texas"))
        self.assertEqual(na_common.state_name("PR"), ("PR", "Puerto Rico"))
        self.assertEqual(na_common.state_name("??"), ("US", None))

    def test_declared_key_makes_env_only_keys_secret(self):
        cfg = Config(env_file=None, overrides={"WSDOT_KEY": "abc123-secret-code"})
        self.assertNotIn("WSDOT_KEY", cfg.declared)
        self.assertIn("abc123", cfg.redact("AccessCode=abc123-secret-code"))         # not yet known as secret...
        self.assertEqual(na_common.declared_key(cfg, "WSDOT_KEY", "d", "https://x.test"), "abc123-secret-code")
        self.assertIn("WSDOT_KEY", cfg.declared)
        self.assertTrue(cfg.declared["WSDOT_KEY"]["secret"])
        self.assertEqual(cfg.redact("AccessCode=abc123-secret-code"), "AccessCode=***")    # ...now redacted
        self.assertIsNone(na_common.declared_key(cfg, "MISSING_KEY"))
        self.assertIsNone(na_common.declared_key(None, "WSDOT_KEY"))
        self.assertIsNone(na_common.declared_key(mock.Mock(get=lambda n, d=None: None), "X_KEY"))   # mocks are fine


# ---------------------------------------------------------------------------------------------
class AdapterTests(unittest.TestCase):
    def test_usgs_hivis(self):
        rows, skipped = na_usgs.parse(fx_json("na_usgs.json"), na_usgs.SOURCE, now=NOW)
        self.assertEqual((len(rows), skipped), (2, 2))          # one hidden, one stale
        r = rows[0]
        self.assertTrue(r["image"].startswith("https://s3.amazonaws.com/usgs-nims-images/720/"))
        self.assertTrue(r["image"].endswith("_newest.jpg"))
        self.assertTrue(r["page"].startswith("https://apps.usgs.gov/hivis/camera/"))
        self.assertIn("river", r["tags"])
        self.assertEqual(r["type"], "webcam")
        self.assertEqual(r["country"], "US")
        self.assertTrue(r["region"])
        self.assertGreaterEqual(r["refresh"], 60)
        assert_allowed(self, rows, na_usgs.SOURCE)

    def test_montana_and_south_dakota(self):
        rows, skipped = na_common.iteris_rows(fx_json("na_iteris_mt.json"), na_iteris.MONTANA, now=NOW)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(set(r["id"] for r in rows)), len(rows))
        self.assertTrue(all("Elev" not in r["name"] and ", " in r["name"] for r in rows))
        self.assertTrue(any(r.get("heading") is not None for r in rows))
        self.assertTrue(all(r.get("elevation") for r in rows))
        assert_allowed(self, rows, na_iteris.MONTANA)
        old, skipped = na_common.iteris_rows(fx_json("na_iteris_mt.json"), na_iteris.MONTANA, now=NOW + 10 * 86400)
        self.assertEqual(old, [])                                 # every picture older than 3 days: offline
        self.assertGreater(skipped, 0)
        sd, _ = na_common.iteris_rows(fx_json("na_iteris_sd.json"), na_iteris.SOUTH_DAKOTA, now=NOW)
        self.assertGreaterEqual(len(sd), 2)
        self.assertTrue(all(r["image"].endswith("/latest.jpg") for r in sd))
        self.assertTrue(any("facing" in r["name"] for r in sd))
        assert_allowed(self, sd, na_iteris.SOUTH_DAKOTA)

    def test_toronto(self):
        url = na_canada.toronto_resource(fx_json("na_toronto_package.json"))
        self.assertTrue(url.endswith("4326.geojson"))
        self.assertIsNone(na_canada.toronto_resource({"result": {"resources": [{"format": "CSV", "name": "x", "url": "https://x"}]}}))
        rows, skipped = na_canada.parse_toronto(fx_json("na_toronto.geojson"), na_canada.TORONTO)
        self.assertEqual((len(rows), skipped), (2, 0))
        self.assertIn(" at ", rows[0]["name"])
        self.assertTrue(all("/CameraImages/" in r["image"] for r in rows))      # never the comparison pictures
        assert_allowed(self, rows, na_canada.TORONTO)

    def test_vancouver_views(self):
        rec = fx_json("na_vancouver_list.json")[0]
        html = (FIX / "na_vancouver_page.html").read_text(encoding="utf-8")
        rows = na_canada.vancouver_views(rec, html, na_canada.VANCOUVER)
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(r["image"].startswith("https://trafficcams.vancouver.ca/cameraimages/") for r in rows))
        self.assertTrue(all(r["page"] == rec["url"] for r in rows))
        self.assertTrue(all(r.get("heading") is not None for r in rows))
        assert_allowed(self, rows, na_canada.VANCOUVER)

    def test_vancouver_fetch_reads_pages_within_budget(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            listing = (FIX / "na_vancouver_list.json").read_bytes()
            page = (FIX / "na_vancouver_page.html").read_bytes()
            net = FakeNet({"web-cam-url-links": listing, "trafficcams.vancouver.ca": page})
            fc = common.FetchContext(FakeDS(tmp), na_canada.VANCOUVER, config=None, net=net, budget_s=600)
            rows = na_canada.fetch_vancouver(fc)
            self.assertGreaterEqual(len(rows), 4)                    # both pages (same fixture page twice)
            page_calls = [kw for url, kw in net.calls if "trafficcams" in url]
            self.assertEqual(len(page_calls), 2)
            # out of budget: pages already on disk are still used, unread ones wait for the next build
            fc2 = common.FetchContext(FakeDS(tmp), na_canada.VANCOUVER, config=None, net=net, budget_s=10)
            self.assertEqual(len(na_canada.fetch_vancouver(fc2)), len(rows))
            shutil.rmtree(tmp / "cavancouver" / "pages")
            fc3 = common.FetchContext(FakeDS(tmp), na_canada.VANCOUVER, config=None, net=net, budget_s=10)
            self.assertEqual(na_canada.fetch_vancouver(fc3), [])
            self.assertIn("still to read", " ".join(fc3.notes))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_alberta_listing(self):
        rows, skipped = na_canada.parse_alberta(fx_json("na_alberta.json")["features"], na_canada.ALBERTA)
        self.assertEqual((len(rows), skipped), (2, 1))             # Calgary's own camera left out
        self.assertTrue(all(r["image"].startswith("https://511.alberta.ca/map/Cctv/") for r in rows))
        assert_allowed(self, rows, na_canada.ALBERTA)

    def test_kirkland(self):
        feats = fx_json("na_kirkland.json")["features"]
        wsdot = {"attributes": {"OWNER": "WSDOT", "IMAGE_": "http://images.wsdot.wa.gov/nw/405vc02095.jpg",
                                "LOCATION": "[WSDOT] I-405"}, "geometry": {"x": -122.19, "y": 47.72}}
        rows, skipped = na_us.parse_kirkland(feats + [wsdot], na_us.KIRKLAND)
        self.assertEqual((len(rows), skipped), (3, 1))
        for r in rows:                                           # ids are the picture's file name (TRAFCAM_ID is often empty)
            self.assertNotIn("?", r["image"])
            self.assertEqual(r["id"].split(":", 1)[1], r["image"].rsplit("/", 1)[1].rsplit(".", 1)[0])
        assert_allowed(self, rows, na_us.KIRKLAND)

    def test_king_county_rows_are_dynamic(self):
        feats = fx_json("na_kingcounty.json")["features"]
        extra = [{"attributes": {"Owner": "WSDOT", "CurrentStatus": "Active",
                                 "ImageURL": "http://images.wsdot.wa.gov/nw/005vc1.jpg"}, "geometry": {"x": -122.3, "y": 47.5}},
                 {"attributes": {"Owner": "King County", "CurrentStatus": "Retired",
                                 "ImageURL": "http://info.kingcounty.gov/x/Handler.ashx?id=Old.jpg"}, "geometry": {"x": -122.3, "y": 47.5}},
                 {"attributes": {"Owner": "King County", "CurrentStatus": "Active",
                                 "ImageURL": "http://info.kingcounty.gov/x/Handler.ashx?id=../../etc.jpg"}, "geometry": {"x": -122.3, "y": 47.5}}]
        rows, skipped = na_us.parse_king(feats + extra, na_us.KING_COUNTY)
        self.assertEqual((len(rows), skipped), (3, 3))
        self.assertTrue(all(r.get("dynamic") and "image" not in r for r in rows))   # links differ only in the query
        url = na_us.KING_COUNTY.resolve(rows[0], None)
        self.assertTrue(url.startswith("https://info.kingcounty.gov/transportation/kcdot/Roads/TrafficCameras/ImageHandler/"))
        self.assertTrue(host_allowed(url, na_us.KING_COUNTY))
        self.assertEqual(check_url(url, na_us.KING_COUNTY, resolver=lambda h: ["93.184.216.34"]), url)

    def test_baton_rouge(self):
        rows, skipped = na_us.parse_brla(fx_json("na_brla.json") + [{"image_view": "https://evil.test/x.jpg"}],
                                         na_us.BATON_ROUGE)
        self.assertEqual((len(rows), skipped), (3, 1))
        self.assertTrue(rows[0]["name"].startswith("LADOTD camera "))
        assert_allowed(self, rows, na_us.BATON_ROUGE)

    def test_deldot_is_live_video(self):
        rows, skipped = na_us.parse_deldot(fx_json("na_deldot.json"), na_us.DELDOT)
        self.assertEqual((len(rows), skipped), (2, 1))
        for r in rows:
            self.assertEqual(r["kind"], "hls")
            self.assertNotIn("image", r)
            self.assertTrue(r["stream"].startswith("https://video.deldot.gov"))
            self.assertNotIn("@", r["name"])
        self.assertTrue(na_us.DELDOT.stream_cors)
        self.assertEqual(na_us.deldot_name("DE 52 @ DE 100"), "DE 52 at DE 100")
        self.assertEqual(na_us.deldot_name("DELAWARE AVE @ IRIS"), "Delaware Ave at Iris")

    def test_ibi_parse_through_shared_parser(self):
        rows, skipped = na_common.parse_ibi(fx_json("na_ibi.json"), na_ibi.SOURCES[1])
        self.assertEqual((len(rows), skipped), (2, 1))              # one disabled view
        self.assertEqual(rows[0]["heading"], 0)
        self.assertTrue(rows[0]["id"].startswith("usga511:"))
        assert_allowed(self, rows, na_ibi.SOURCES[1])

    def test_wsdot(self):
        rows, skipped = na_keyed.parse_wsdot(fx_json("na_wsdot.json"), na_keyed.WSDOT)
        self.assertEqual((len(rows), skipped), (2, 2))             # Oregon's picture and an inactive camera left out
        self.assertTrue(all(r["image"].startswith("https://images.wsdot.wa.gov/") for r in rows))
        self.assertEqual([r["road"] for r in rows], ["I-5", "I-90"])
        self.assertEqual(rows[1]["lat"], 47.4245)                  # display position 0,0: true position used
        self.assertEqual(na_keyed.wsdot_road("520"), "SR 520")
        self.assertEqual(na_keyed.wsdot_road("101"), "US 101")
        assert_allowed(self, rows, na_keyed.WSDOT)

    def test_ohgo(self):
        rows, skipped = na_keyed.parse_ohgo(fx_json("na_ohgo.json"), na_keyed.OHGO)
        self.assertEqual((len(rows), skipped), (3, 0))
        self.assertEqual(rows[0]["name"], "I-70 at I-71 split, facing east")
        self.assertEqual(rows[1]["name"], "I-70 at I-71 split, view 2")      # PTZ: no fixed heading
        self.assertNotIn("heading", rows[1])
        self.assertEqual(len({r["id"] for r in rows}), 3)
        assert_allowed(self, rows, na_keyed.OHGO)
        pascal = {"Results": [{"Id": "9", "Latitude": 40.0, "Longitude": -83.0, "Location": "I-270",
                               "CameraViews": [{"Direction": "N", "LargeUrl": "https://itscameras.dot.state.oh.us/a.jpg"}]}]}
        self.assertEqual(len(na_keyed.parse_ohgo(pascal, na_keyed.OHGO)[0]), 1)

    def test_ottawa_keeps_the_certificate_server_side(self):
        rows, skipped = na_keyed.parse_ottawa(fx_json("na_ottawa.json"), na_keyed.OTTAWA)
        self.assertEqual((len(rows), skipped), (2, 1))             # the MTO camera belongs to Ontario 511
        self.assertTrue(all(r.get("dynamic") and "image" not in r for r in rows))
        cfg = Config(env_file=None, overrides={"OTTAWA_CAMERA_KEY": "CERT-0123456789"})
        net = mock.Mock(config=cfg)
        url = na_keyed.resolve_ottawa(rows[0], net)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual((q["c"], q["certificate"]), ([rows[0]["id"].split(":")[1]], ["CERT-0123456789"]))
        self.assertTrue(host_allowed(url, na_keyed.OTTAWA))
        self.assertIn("OTTAWA_CAMERA_KEY", cfg.declared)
        self.assertIsNone(na_keyed.resolve_ottawa({"id": "caottawa:12/../x"}, net))
        self.assertIsNone(na_keyed.resolve_ottawa(rows[0], mock.Mock(config=Config(env_file=None, overrides={}))))
        self.assertNotIn("CERT-0123456789", json.dumps(rows))


# ---------------------------------------------------------------------------------------------
class KeyedSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_keyed_sources_need_keys(self):
        keyed = [s for s in na_sources() if s.needs_key]
        self.assertGreaterEqual(len(keyed), 15)
        for src in keyed:
            fc = common.FetchContext(FakeDS(self.tmp), src, config=Config(env_file=None, overrides={}),
                                     net=FakeNet({}))
            with self.assertRaises(NeedsKey) as ctx:
                src.fetch(fc)
            self.assertEqual(ctx.exception.key_name, src.needs_key)
            self.assertTrue(src.optional, src.id)

    def test_ibi_fetch_sends_key_and_is_declared(self):
        src = na_ibi.SOURCES[1]                                     # Georgia 511
        cfg = Config(env_file=None, overrides={"GA511_KEY": "ga-key-0123456789"})
        net = FakeNet({"511ga.org/api/v2/get/cameras": (FIX / "na_ibi.json").read_bytes()})
        rows = src.fetch(common.FetchContext(FakeDS(self.tmp), src, config=cfg, net=net))
        self.assertEqual(len(rows), 2)
        url = net.calls[0][0]
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["key"], ["ga-key-0123456789"])
        self.assertEqual(cfg.redact(url).count("***"), 1)
        self.assertNotIn("ga-key", json.dumps(rows))

    def test_ohgo_sends_key_in_a_header(self):
        cfg = Config(env_file=None, overrides={"OHGO_KEY": "ohgo-key-0123456789"})
        net = FakeNet({"publicapi.ohgo.com": (FIX / "na_ohgo.json").read_bytes()})
        rows = na_keyed.OHGO.fetch(common.FetchContext(FakeDS(self.tmp), na_keyed.OHGO, config=cfg, net=net))
        self.assertEqual(len(rows), 3)
        url, headers = net.calls[0]
        self.assertNotIn("ohgo-key", url)
        self.assertEqual(headers.get("Authorization"), "APIKEY ohgo-key-0123456789")

    def test_wsdot_and_ottawa_fetch_with_keys(self):
        cfg = Config(env_file=None, overrides={"WSDOT_KEY": "wsdot-code-0123", "OTTAWA_CAMERA_KEY": "CERT-0123456789"})
        net = FakeNet({"HighwayCamerasREST.svc": (FIX / "na_wsdot.json").read_bytes(),
                       "traffic.ottawa.ca/map/service/camera": (FIX / "na_ottawa.json").read_bytes()})
        self.assertEqual(len(na_keyed.WSDOT.fetch(common.FetchContext(FakeDS(self.tmp), na_keyed.WSDOT, config=cfg, net=net))), 2)
        ottawa = na_keyed.OTTAWA.fetch(common.FetchContext(FakeDS(self.tmp), na_keyed.OTTAWA, config=cfg, net=net))
        self.assertEqual(len(ottawa), 2)
        self.assertNotIn("CERT", net.calls[-1][0])                  # the list is fetched without the certificate


# ---------------------------------------------------------------------------------------------
class SourceTableTests(unittest.TestCase):
    def test_every_na_source_is_complete_and_discovered(self):
        from meridian.modules.cameras import sources
        ids = [s.id for s in na_sources()]
        self.assertEqual(len(ids), len(set(ids)))
        for s in na_sources():
            self.assertRegex(s.id, r"^[a-z0-9]{1,16}$")
            self.assertTrue(s.name and s.owner and s.url and s.licence and s.attribution and s.coverage, s.id)
            self.assertTrue(s.hosts, s.id)
            self.assertTrue(callable(s.fetch), s.id)
            self.assertIn(s.country, ("US", "CA"), s.id)
            self.assertIs(sources.BY_ID.get(s.id), s, f"{s.id} is not discovered")
            for pattern in s.hosts:      # host_allowed lower-cases the pattern: a path prefix must be lower case
                self.assertEqual(pattern, pattern.lower(), s.id)
            if s.needs_key:
                self.assertTrue(re.search(r"(KEY|TOKEN|SECRET)", s.needs_key), s.needs_key)
                self.assertTrue(s.optional and s.url.startswith("https://"), s.id)
        self.assertFalse([b for b in sources.BROKEN if b[0].startswith("na_")])

    def test_rows_survive_the_build_screen(self):
        """The build drops rows whose image host is not allowed: none of ours may be dropped."""
        cases = [
            (na_usgs.SOURCE, na_usgs.parse(fx_json("na_usgs.json"), na_usgs.SOURCE, now=NOW)[0]),
            (na_canada.TORONTO, na_canada.parse_toronto(fx_json("na_toronto.geojson"), na_canada.TORONTO)[0]),
            (na_canada.ALBERTA, na_canada.parse_alberta(fx_json("na_alberta.json")["features"], na_canada.ALBERTA)[0]),
            (na_us.KIRKLAND, na_us.parse_kirkland(fx_json("na_kirkland.json")["features"], na_us.KIRKLAND)[0]),
            (na_us.BATON_ROUGE, na_us.parse_brla(fx_json("na_brla.json"), na_us.BATON_ROUGE)[0]),
            (na_keyed.WSDOT, na_keyed.parse_wsdot(fx_json("na_wsdot.json"), na_keyed.WSDOT)[0]),
            (na_iteris.SOUTH_DAKOTA, na_common.iteris_rows(fx_json("na_iteris_sd.json"), na_iteris.SOUTH_DAKOTA, now=NOW)[0]),
        ]
        for src, rows in cases:
            kept, dropped = build_mod.screen_rows(rows, src)
            self.assertEqual((len(kept), dropped), (len(rows), 0), src.id)
            self.assertTrue(rows, src.id)

    def test_dynamic_sources_dedupe_per_camera(self):
        """Links that differ only in the query string would collapse to one camera in build.dedupe."""
        rows = na_us.parse_king(fx_json("na_kingcounty.json")["features"], na_us.KING_COUNTY)[0]
        kept, dupes = build_mod.dedupe(rows)
        self.assertEqual((len(kept), dupes), (len(rows), 0))


if __name__ == "__main__":
    unittest.main()
