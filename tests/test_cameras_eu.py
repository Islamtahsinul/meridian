"""European camera sources (``meridian/modules/cameras/sources/eu_*.py``): every adapter against a
small fixture, the shared helpers in ``eu_common``, and checks that apply to every European source
(ids the camera routes accept, metadata, allowlists, keyed sources asking for their key).

No network: fixtures live in tests/fixtures/cameras/eu_*, HTTP goes through fakes.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meridian.modules.cameras import build as build_mod  # noqa: F401
from meridian.modules.cameras import common
from meridian.modules.cameras import sources as sources_pkg
from meridian.modules.cameras.common import FetchContext, NeedsKey, Source, make_camera  # noqa: F401
from meridian.modules.cameras.proxy import host_allowed
from meridian.modules.cameras.sources import eu_common
from meridian.net import NetError, NetResponse

# adapters and helpers under test
import base64
import urllib.parse
from datetime import datetime
from meridian.modules.cameras.sources import (
    eu_a22, eu_basque, eu_catalonia, eu_croatia, eu_durham, eu_estonia, eu_geneva, eu_hungary,
    eu_loireatlantique, eu_luxembourg, eu_lyon, eu_madrid, eu_malaga, eu_meteogalicia, eu_meteoswiss,
    eu_northeast, eu_norway, eu_slovenia, eu_sweden)

FIX = Path(__file__).parent / "fixtures" / "cameras"
CAMERA_ID = re.compile(r"^[a-z0-9]{1,16}:[A-Za-z0-9._~-]{1,96}$")   # as cameras/__init__.py ID_RE


def fx_json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def fx_bytes(name):
    return (FIX / name).read_bytes()


def fx_text(name):
    return (FIX / name).read_text(encoding="utf-8")


def no_keys():
    """A config with no keys set."""
    return mock.Mock(get=lambda name, default=None: None, has=lambda name: False)


class FakeDS:
    """Enough of a dataset BuildContext for FetchContext (raw files under a temp folder)."""

    def __init__(self, root: Path):
        self.raw_dir = Path(root)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.force = False
        self.max_age_hours = 24.0
        self.net = None
        self.log = mock.Mock()
        self.config = None

    def raw(self, name):
        p = self.raw_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


def needs_key_without_config(test: unittest.TestCase, src: Source) -> None:
    """Assert that a keyed source raises NeedsKey (and makes no request) when its key is missing."""
    tmp = Path(tempfile.mkdtemp())
    try:
        ds = FakeDS(tmp)
        ds.net = mock.Mock(side_effect=AssertionError("no network expected"))
        fc = FetchContext(ds, src, config=no_keys(), net=ds.net)
        with test.assertRaises(NeedsKey):
            src.fetch(fc)
        ds.net.request.assert_not_called()
        ds.net.download.assert_not_called()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def eu_sources() -> list[Source]:
    """Every discovered source whose adapter lives in an ``eu_*`` module."""
    out = []
    for s in sources_pkg.ALL:
        mod = getattr(s.fetch, "__module__", "") or ""
        if mod.rsplit(".", 1)[-1].startswith("eu_"):
            out.append(s)
    return out


# ---------------------------------------------------------------------------------------------
class EuropeSourceTests(unittest.TestCase):
    """Rules every European source must follow."""

    def test_metadata_and_ids(self):
        found = eu_sources()
        self.assertTrue(found)
        seen = set()
        for s in found:
            self.assertNotIn(s.id, seen, s.id)
            seen.add(s.id)
            self.assertRegex(s.id, r"^[a-z0-9]{1,16}$")
            self.assertRegex(f"{s.id}:abc-1.2_x~y", CAMERA_ID)                 # the routes accept its camera ids
            self.assertTrue(s.name and s.owner and s.url and s.licence and s.attribution, s.id)
            self.assertTrue(s.licence_url.startswith("https://") or s.licence_url.startswith("http://"), s.id)
            self.assertTrue(s.url.startswith("https://") or s.url.startswith("http://"), s.id)
            self.assertTrue(s.hosts, s.id)
            self.assertTrue(all(h and " " not in h and not h.startswith(("http:", "https:")) for h in s.hosts), s.id)
            self.assertNotIn("*", "".join(h.lstrip("*.") for h in s.hosts), s.id)  # wildcards only as "*.domain"
            self.assertTrue(callable(s.fetch), s.id)
            self.assertRegex(s.country, r"^([A-Z]{2})?$", s.id)                # "" when rows carry their own
            self.assertTrue(15 <= s.refresh <= 3600, s.id)
            self.assertTrue(set(s.tags) <= set(common.TAGS), s.id)
            if s.needs_key:
                self.assertTrue(s.optional, f"{s.id}: keyed sources are optional")
                self.assertTrue(s.needs_key.endswith("_KEY"), f"{s.id}: key names end in _KEY so they are redacted")

    def test_keyed_sources_ask_for_their_key(self):
        for s in eu_sources():
            if s.needs_key:
                with self.subTest(source=s.id):
                    needs_key_without_config(self, s)


class EuCommonTests(unittest.TestCase):
    def test_xml_helpers(self):
        root = common.strip_ns(common.parse_xml(
            b'<d xmlns="http://datex2.eu/schema/3/common"><cam><name><values>'
            b'<value lang="no">Dovrefjell  nord</value><value lang="en">Dovrefjell north</value><value>plain</value>'
            b'</values></name><lat> 62.1 </lat></cam></d>'))
        cam = root.find("cam")
        self.assertEqual(eu_common.xml_text(cam, "lat"), "62.1")
        self.assertEqual(eu_common.xml_text(cam, "missing"), "")
        self.assertEqual(eu_common.xml_text(None, "lat"), "")
        names = eu_common.datex_values(cam.find("name"))
        self.assertEqual(names, {"no": "Dovrefjell nord", "en": "Dovrefjell north", "": "plain"})
        self.assertEqual(eu_common.datex_values(None), {})

    def test_pick_lang(self):
        self.assertEqual(eu_common.pick_lang({"de": "Brenner", "en": " Brenner Pass "}), "Brenner Pass")
        self.assertEqual(eu_common.pick_lang({"de": "Brenner", "it": "Brennero"}, ("it", "de")), "Brennero")
        self.assertEqual(eu_common.pick_lang({"de": "", "it": "Brennero"}), "Brennero")
        self.assertEqual(eu_common.pick_lang("Plain"), "Plain")
        self.assertEqual(eu_common.pick_lang(None), "")
        self.assertEqual(eu_common.pick_lang({"en": None}), "")

    def test_post_cached(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            ds = FakeDS(tmp)
            src = Source(id="t", name="T", owner="o", url="https://t.test", licence="L", attribution="A")
            calls = []

            class Net:
                fail = False

                def request(self, url, **kw):
                    calls.append((url, kw.get("method"), kw.get("data")))
                    if self.fail:
                        raise NetError("t.test: HTTP 503", status=503)
                    return NetResponse(200, {"content-type": "application/json"}, b'{"ok": true}', url)

            net = Net()
            fc = FetchContext(ds, src, config=no_keys(), net=net)
            path = eu_common.post_cached(fc, "https://t.test/q", b"<query/>", "answer.json")
            self.assertEqual(path.read_bytes(), b'{"ok": true}')
            self.assertEqual(calls, [("https://t.test/q", "POST", b"<query/>")])
            eu_common.post_cached(fc, "https://t.test/q", b"<query/>", "answer.json")
            self.assertEqual(len(calls), 1)                                   # fresh copy reused
            self.assertNotIn(b"query", path.read_bytes())                    # the request body is never stored
            fc.force = True
            net.fail = True
            again = eu_common.post_cached(fc, "https://t.test/q", b"<query/>", "answer.json")
            self.assertEqual(again.read_bytes(), b'{"ok": true}')              # older copy survives a failure
            self.assertTrue(any("older copy" in n for n in fc.notes))
            with self.assertRaises(NetError):
                eu_common.post_cached(fc, "https://t.test/q", b"<query/>", "other.json")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
# Per-region adapter tests follow.
# ---------------------------------------------------------------------------------------------


class _nordic_FixtureNet:
    """Answers downloads with fixture files by URL; anything else raises like an unreachable host."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.urls: list[str] = []

    def download(self, url, dest, **kw):
        self.urls.append(url)
        if url not in self.files:
            raise NetError("host: timed out")
        Path(dest).write_bytes((FIX / self.files[url]).read_bytes())
        return Path(dest)


class NordicTests(unittest.TestCase):
    # -- Norway: Statens vegvesen (keyless DATEX II WFS) ------------------------------------------
    def test_norway(self):
        src = eu_norway.SOURCE
        faulty = eu_norway.faulty_ids(fx_json("eu_norway_status.json"))
        self.assertEqual(faulty, {"0229020_1"})
        rows, skipped = eu_norway.parse(fx_json("eu_norway_cctv.json"), src, faulty)
        self.assertEqual((len(rows), skipped), (3, 2))              # the faulty camera and a record without data
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(set(by_id), {"nosvv:0129010_1", "nosvv:0429003_1", "nosvv:0529019_1"})
        for r in rows:
            self.assertEqual(r["country"], "NO")
            self.assertTrue(host_allowed(r["image"], src), r["image"])
            self.assertIn("weather", r["tags"])
            self.assertIn(", towards ", r["name"])
            self.assertTrue(r.get("region"))
            self.assertRegex(r["road"], r"^(E\d+|Rv\. \d+|Fv\. \d+)$")
        live = by_id["nosvv:0129010_1"]
        self.assertEqual(live["kind"], "hls")
        self.assertTrue(live["stream"].startswith("https://kamera.vegvesen.no/public/0129010_1/"))
        self.assertTrue(src.stream_cors)
        self.assertIn("bridge", by_id["nosvv:0429003_1"]["tags"])         # Mjøsbrua
        self.assertIn("mountain", by_id["nosvv:0529019_1"]["tags"])       # ...fjellet
        self.assertIn("elevation", live)
        # without the status layer nothing is trimmed (the faulty camera stays)
        rows, skipped = eu_norway.parse(fx_json("eu_norway_cctv.json"), src)
        self.assertEqual((len(rows), skipped), (4, 1))

    def test_norway_helpers(self):
        self.assertEqual(eu_norway.road_label("E6"), "E6")
        self.assertEqual(eu_norway.road_label("R7"), "Rv. 7")
        self.assertEqual(eu_norway.road_label("F884"), "Fv. 884")
        self.assertEqual(eu_norway.road_label("??"), "??")
        self.assertEqual(eu_norway.place_tags("Mekjarvik ferjekai"), ["port"])
        self.assertEqual(eu_norway.place_tags("Ebru"), [])                  # a place name, not a bridge
        self.assertEqual(eu_norway.place_tags("Smaalenenes bru"), ["bridge"])
        self.assertEqual(eu_norway.parse({"features": "nonsense"}, eu_norway.SOURCE), ([], 0))
        self.assertEqual(eu_norway.parse(None, eu_norway.SOURCE), ([], 0))

    def test_norway_fetch_survives_a_missing_status_layer(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            ds = FakeDS(tmp)
            net = _nordic_FixtureNet({eu_norway.FULL_URL: "eu_norway_cctv.json"})     # the status layer times out
            fc = FetchContext(ds, eu_norway.SOURCE, config=no_keys(), net=net)
            rows = eu_norway.fetch(fc)
            self.assertEqual(len(rows), 4)
            self.assertIn(eu_norway.STATUS_URL, net.urls)
            self.assertTrue(any("status layer" in n for n in fc.notes))
            net = _nordic_FixtureNet({eu_norway.FULL_URL: "eu_norway_cctv.json", eu_norway.STATUS_URL: "eu_norway_status.json"})
            ds.force = True
            fc = FetchContext(ds, eu_norway.SOURCE, config=no_keys(), net=net)
            self.assertEqual(len(eu_norway.fetch(fc)), 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- Estonia: Transpordiamet via Tark Tee (keyless ArcGIS, image looked up per request) -------
    def test_estonia(self):
        src = eu_estonia.SOURCE
        fx = fx_json("eu_estonia.json")
        rows, skipped = eu_estonia.parse(fx["features"], src, now=fx["now"])
        self.assertEqual((len(rows), skipped), (3, 2))                      # one silent for 4 days, one without a picture
        self.assertEqual([r["id"] for r in rows], ["eetransport:94", "eetransport:997", "eetransport:44"])
        for r in rows:
            self.assertTrue(r.get("dynamic"))
            self.assertNotIn("image", r)
            self.assertEqual(r["country"], "EE")
            self.assertIn("weather", r["tags"])
        self.assertTrue(rows[1]["description"].startswith("Nearest road weather station: "))
        self.assertNotIn("description", rows[0])                            # the station is at the camera
        self.assertEqual(eu_estonia.camera_no("94/94_202611231301.jpg"), "94")
        self.assertIsNone(eu_estonia.camera_no("../94/94_1.jpg"))
        self.assertIsNone(eu_estonia.camera_no("94/../../x.jpg"))
        self.assertIsNone(eu_estonia.camera_no(None))

    def test_estonia_resolver(self):
        net = mock.Mock()
        net.get_json.return_value = {"features": [
            {"attributes": {"image_path": "../../secret.jpg"}},
            {"attributes": {"image_path": "94/94_202611231321.jpg"}},
            {"attributes": {"image_path": None}}, "junk"]}
        url = eu_estonia.resolve({"id": "eetransport:94"}, net)
        self.assertEqual(url, "https://tarktee.ee/images/94/94_202611231321.jpg")
        self.assertTrue(host_allowed(url, eu_estonia.SOURCE))
        self.assertEqual(net.get_json.call_args.kwargs.get("ttl"), 120)        # one shared, cached lookup
        self.assertIsNone(eu_estonia.resolve({"id": "eetransport:5"}, net))      # not in the answer
        calls = net.get_json.call_count
        self.assertIsNone(eu_estonia.resolve({"id": "eetransport:../x"}, net))   # refused before any request
        self.assertEqual(net.get_json.call_count, calls)
        net.get_json.return_value = ["not", "a", "dict"]
        self.assertIsNone(eu_estonia.resolve({"id": "eetransport:94"}, net))

    # -- Sweden: Trafikverket (needs TRAFIKVERKET_KEY) --------------------------------------------
    def test_sweden_parse(self):
        # synthetic records in the documented answer shape (not verified live: no key here)
        src = eu_sweden.SOURCE
        rows, skipped = eu_sweden.parse(fx_json("eu_sweden.json"), src)
        self.assertEqual((len(rows), skipped), (3, 2))                      # one inactive, one without a position
        flow, road, bridge = rows
        self.assertEqual(flow["id"], "setrafikverket:SE_STA_CAMERA_Orion_1000001")
        self.assertTrue(flow["image"].endswith(".Jpeg?type=fullsize"))
        self.assertEqual((flow["heading"], flow["road"], flow["region"]), (350, "E4", "Stockholm"))
        self.assertEqual((flow["lat"], flow["lon"]), (59.32411, 18.00845))
        self.assertIn("weather", road["tags"])
        self.assertEqual(road["refresh"], 900)
        self.assertNotIn("weather", flow["tags"])
        self.assertIn("bridge", bridge["tags"])                             # Öresundsbron
        self.assertNotIn("?", bridge["image"])                              # no full-size photo
        for r in rows:
            self.assertEqual(r["country"], "SE")
            self.assertTrue(host_allowed(r["image"], src), r["image"])
        with self.assertRaises(RuntimeError):
            eu_sweden.parse({"RESPONSE": {"RESULT": [{"ERROR": {"SOURCE": "Security",
                                                                "MESSAGE": "Invalid authentication"}}]}}, src)
        self.assertEqual(eu_sweden.parse({}, src), ([], 0))
        self.assertEqual(eu_sweden.point({"WGS84": "POINT (18.0345 59.3247)"}), (59.3247, 18.0345))
        self.assertEqual(eu_sweden.point(None), (None, None))

    def test_sweden_needs_key(self):
        needs_key_without_config(self, eu_sweden.SOURCE)
        self.assertEqual(eu_sweden.SOURCE.needs_key, "TRAFIKVERKET_KEY")
        self.assertTrue(eu_sweden.SOURCE.optional)

    def test_sweden_key_stays_in_the_body(self):
        key = "k3y<&\"value"
        answer = fx_bytes("eu_sweden.json")
        tmp = Path(tempfile.mkdtemp())
        try:
            ds = FakeDS(tmp)
            net = mock.Mock()
            net.request.return_value = mock.Mock(status=200, body=answer, headers={})
            config = mock.Mock(get=lambda name, default=None: key if name == "TRAFIKVERKET_KEY" else default)
            fc = FetchContext(ds, eu_sweden.SOURCE, config=config, net=net)
            rows = eu_sweden.fetch(fc)
            self.assertEqual(len(rows), 3)
            args, kw = net.request.call_args
            self.assertEqual(kw.get("method"), "POST")
            self.assertNotIn("k3y", args[0])                                # not in the URL
            body = kw["data"].decode("utf-8")
            self.assertIn('authenticationkey="k3y&lt;&amp;&quot;value"', body)  # escaped inside the XML
            for path in tmp.rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"k3y", path.read_bytes(), path.name)   # never in the raw cache
            # an error answer is not kept, so the next build asks again
            net.request.return_value = mock.Mock(status=200, headers={}, body=json.dumps(
                {"RESPONSE": {"RESULT": [{"ERROR": {"MESSAGE": "Invalid authentication"}}]}}).encode())
            ds.force = True
            fc = FetchContext(ds, eu_sweden.SOURCE, config=config, net=net)
            with self.assertRaises(RuntimeError):
                eu_sweden.fetch(fc)
            self.assertFalse(fc.raw_path("cameras.json").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class UkIrelandTests(unittest.TestCase):
    """Durham County Council (keyless) and the North East UTMC Open Data Service (free account)."""

    # -- Durham -------------------------------------------------------------------------------
    def test_durham_traffic(self):
        src = eu_durham.SOURCE
        rows, skipped = eu_durham.parse_traffic(fx_json("eu_durham_traffic.json")["features"], src)
        self.assertEqual((len(rows), skipped), (3, 1))                   # "Not working" is skipped
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(sorted(by_id), ["gbdurham:dutmc_24", "gbdurham:dutmc_32", "gbdurham:dutmc_37"])
        first = by_id["gbdurham:dutmc_24"]
        self.assertEqual(first["name"], "Framwellgate Peth")
        self.assertEqual(first["image"], "https://dcc.ussgroup.co.uk/images/dutmc_24.jpg")
        self.assertEqual(first["page"], "https://www.durham.gov.uk/article/6134")
        self.assertEqual((first["country"], first["region"], first["area"]), ("GB", "County Durham", "Durham"))
        self.assertEqual(first["direction"], "View towards the City Centre")
        self.assertEqual((first["refresh"], first["type"]), (60, "traffic"))
        self.assertEqual(set(first["tags"]), {"traffic", "urban"})
        self.assertNotIn("direction", by_id["gbdurham:dutmc_32"])       # the view only repeated the name
        self.assertEqual(by_id["gbdurham:dutmc_32"]["area"], "Bishop Auckland")
        for r in rows:
            self.assertTrue(host_allowed(r["image"], src), r["image"])

    def test_durham_weather(self):
        src = eu_durham.SOURCE
        rows, skipped = eu_durham.parse_weather(fx_json("eu_durham_weather.json")["features"], src)
        self.assertEqual((len(rows), skipped), (2, 1))                   # the record without a station id
        hawthorn = rows[0]
        self.assertEqual((hawthorn["id"], hawthorn["name"], hawthorn["road"]), ("gbdurham:wx56", "B1432 Hawthorn", "B1432"))
        self.assertNotIn("image", hawthorn)              # rebuilt at request time (see resolve)
        self.assertEqual(eu_durham.resolve(hawthorn, None),
                         "https://www.durham.gov.uk/weathercameras?action=Camera&camId=56")
        self.assertIsNone(eu_durham.resolve({"id": "gbdurham:wx1x"}, None))
        self.assertIn("weather", hawthorn["tags"])
        self.assertEqual(hawthorn["refresh"], 600)
        for r in rows:
            url = eu_durham.resolve(r, None)
            self.assertTrue(host_allowed(url, src), url)
        # the weather allowlist entry is a path prefix: other pages on the council site are not images
        self.assertFalse(host_allowed("https://www.durham.gov.uk/article/6134", src))
        self.assertFalse(host_allowed("https://dcc.ussgroup.co.uk/i_dutmc_24.html", src))

    def test_durham_image_names(self):
        name = eu_durham._image_name
        self.assertEqual(name({"Cam_Link": "https://dcc.ussgroup.co.uk/i_dutmc_24.html"}), "dutmc_24")
        self.assertEqual(name({"Cam_Link": "", "USS_Camera_Number": "Dutmc_08"}), "dutmc_08")
        self.assertIsNone(name({"Cam_Link": "https://dcc.ussgroup.co.uk/x.html", "USS_Camera_Number": "../etc"}))

    def test_durham_one_layer_down_keeps_the_other(self):
        traffic = fx_bytes("eu_durham_traffic.json")

        class Net:
            def download(self, url, dest, **kw):
                if "VectorPoint" not in url:
                    raise OSError("weather layer down")
                Path(dest).write_bytes(traffic)
                return Path(dest)

        tmp = Path(tempfile.mkdtemp())
        try:
            fc = FetchContext(FakeDS(tmp), eu_durham.SOURCE, config=no_keys(), net=Net())
            rows = eu_durham.fetch(fc)
            self.assertEqual(len(rows), 3)
            self.assertTrue(any("weather layer failed" in n for n in fc.notes), fc.notes)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- North East UTMC (synthetic records in the documented API shape) -------------------------
    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc).timestamp()

    def test_northeast_parse(self):
        src = eu_northeast.SOURCE
        images, stale = eu_northeast.latest_images(fx_json("eu_netravel_dynamic.json"), now=self._now())
        self.assertEqual(stale, {"SL_A1018E1"})                         # no picture for weeks
        rows, skipped = eu_northeast.parse(fx_json("eu_netravel_static.json"), src, images, stale)
        # skipped: public-safety camera, a camera without a position, the stale one
        self.assertEqual((len(rows), skipped), (3, 3))
        by_id = {r["id"]: r for r in rows}
        gh = by_id["gbnetravel:GH_A184B1"]
        self.assertEqual(gh["name"], "A184 Bus Interchange - Nelson Street / West Street")
        self.assertEqual((gh["area"], gh["road"], gh["country"]), ("Gateshead", "A184", "GB"))
        self.assertEqual(gh["image"], "https://www.netraveldata.co.uk/api/v1/cctv/images/GH_A184B1.jpg")  # dynamic link wins
        self.assertIn("urban", gh["tags"])
        tunnel = by_id["gbnetravel:TT2North1"]
        self.assertIn("tunnel", tunnel["tags"])
        self.assertEqual(tunnel["refresh"], 60)
        weather = by_id["gbnetravel:VAISALACCTV12"]
        self.assertIn("weather", weather["tags"])
        self.assertEqual(weather["image"], "https://www.netraveldata.co.uk/api/v2/cctv/images/VAISALACCTV12.jpg")
        self.assertFalse(any(r["id"].startswith("gbnetravel:PS_") for r in rows))
        for r in rows:
            self.assertTrue(host_allowed(r["image"], src), r["image"])
            self.assertEqual(r["type"], "traffic")
        # without the dynamic feed every camera still gets the fixed image link
        rows2, _ = eu_northeast.parse(fx_json("eu_netravel_static.json"), src)
        self.assertEqual(len(rows2), 4)

    def test_northeast_needs_key(self):
        src = eu_northeast.SOURCE
        self.assertEqual((src.needs_key, src.optional), ("NETRAVELDATA_KEY", True))
        needs_key_without_config(self, src)

    def test_northeast_credentials_stay_in_a_header(self):
        self.assertEqual(eu_northeast._auth_header("user:pw")["Authorization"], "Basic dXNlcjpwdw==")
        with self.assertRaises(RuntimeError) as cm:
            eu_northeast._auth_header("secretvalue")
        self.assertNotIn("secretvalue", str(cm.exception))
        static = fx_bytes("eu_netravel_static.json")
        calls = []

        class Net:
            def download(self, url, dest, headers=None, **kw):
                calls.append((url, Path(dest).name, dict(headers or {})))
                if url.endswith("/dynamic"):
                    raise OSError("dynamic feed down")
                Path(dest).write_bytes(static)
                return Path(dest)

        tmp = Path(tempfile.mkdtemp())
        try:
            config = mock.Mock(get=lambda name, default=None: "user:pw" if name == "NETRAVELDATA_KEY" else default)
            fc = FetchContext(FakeDS(tmp), eu_northeast.SOURCE, config=config, net=Net())
            rows = eu_northeast.fetch(fc)
            self.assertEqual(len(rows), 4)                               # dynamic feed down: fixed links
            self.assertTrue(any("latest-image list unavailable" in n for n in fc.notes))
            for url, filename, headers in calls:
                self.assertNotIn("user", url)
                self.assertNotIn("pw", filename)
                self.assertEqual(headers.get("Authorization"), "Basic dXNlcjpwdw==")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sources_are_complete(self):
        import re
        for src in (eu_durham.SOURCE, eu_northeast.SOURCE):
            self.assertRegex(src.id, r"^[a-z0-9]{1,16}$")
            self.assertTrue(src.name and src.owner and src.url and src.licence and src.attribution and src.hosts)
            self.assertEqual((src.country, src.camera_type), ("GB", "traffic"))
            for pattern in src.hosts:
                self.assertTrue(re.match(r"^[a-z0-9.-]+(/.+)?$", pattern), pattern)   # exact hosts, no wildcards


class WestTests(unittest.TestCase):
    """Luxembourg (CITA), Lyon (Criter), MeteoSwiss and Loire-Atlantique."""

    def test_west_sources_are_complete(self):
        for src in (eu_luxembourg.SOURCE, eu_lyon.SOURCE, eu_meteoswiss.SOURCE, eu_loireatlantique.SOURCE):
            self.assertRegex(src.id, r"^[a-z0-9]{1,16}$")
            self.assertTrue(src.hosts and src.licence and src.licence_url and src.attribution and src.country, src.id)
            self.assertIsNone(src.needs_key)
            self.assertTrue(callable(src.fetch))

    def test_luxembourg_captions_and_headings(self):
        caps = eu_luxembourg.parse_captions(fx_text("eu_luxembourg_page.html"))
        self.assertEqual(caps["3"], ("Steinfort", "Belgium"))                  # country names in English
        self.assertEqual(caps["46"], ("Croix de Gasperich", "Tunnel Howald"))
        # the KML icon points east at 0 degrees and turns clockwise
        self.assertEqual(eu_luxembourg.heading_of("https://www.cita.lu/images/kml/cams/cctv_0.png"), 90.0)
        self.assertEqual(eu_luxembourg.heading_of("https://www.cita.lu/images/kml/cams/cctv_355.png"), 85.0)
        self.assertIsNone(eu_luxembourg.heading_of("https://www.cita.lu/images/kml/cams/other.png"))

    def test_luxembourg_parse(self):
        caps = eu_luxembourg.parse_captions(fx_text("eu_luxembourg_page.html"))
        rows, skipped = eu_luxembourg.parse(fx_bytes("eu_luxembourg.kml"), caps, eu_luxembourg.SOURCE)
        self.assertEqual((len(rows), skipped), (3, 2))                         # bad id, no position
        by_id = {r["id"]: r for r in rows}
        r = by_id["lucita:3"]
        self.assertEqual(r["name"], "A6 Steinfort, towards Belgium")
        self.assertEqual((r["road"], r["country"], r["heading"], r["kind"]), ("A6", "LU", 275.0, "image"))
        self.assertEqual(r["image"], "https://www.cita.lu/info_trafic/cameras/images/cccam_3.jpg")
        self.assertAlmostEqual(r["lat"], 49.63694, places=4)
        self.assertIn("tunnel", by_id["lucita:46"]["tags"])
        self.assertEqual(by_id["lucita:3011"]["name"], "A3, camera 3011")       # no caption: road and number
        for row in rows:
            self.assertTrue(host_allowed(row["image"], eu_luxembourg.SOURCE))
        self.assertFalse(host_allowed("https://www.cita.lu/fr/webcam/a1.html", eu_luxembourg.SOURCE))

    def test_luxembourg_fetch_survives_a_missing_page(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            ds = FakeDS(tmp)
            asked = []

            def download(url, dest, **kw):
                asked.append(url)
                if url.endswith("/a3.html"):
                    raise NetError("www.cita.lu: HTTP 500", status=500)
                body = fx_bytes("eu_luxembourg.kml") if url.endswith(".kml") else fx_bytes("eu_luxembourg_page.html")
                Path(dest).write_bytes(body)
                return Path(dest)

            ds.net = mock.Mock(download=download)
            fc = FetchContext(ds, eu_luxembourg.SOURCE, config=no_keys(), net=ds.net)
            rows = eu_luxembourg.fetch(fc)
            self.assertEqual(len(rows), 3)
            self.assertIn("https://www.cita.lu/en/webcam/a6.html", asked)
            self.assertIn("no place names for A3", "; ".join(fc.notes))
            self.assertEqual(fc.skipped, 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_lyon(self):
        rows, skipped = eu_lyon.parse(fx_json("eu_lyon.json"), eu_lyon.SOURCE)
        self.assertEqual((len(rows), skipped), (3, 1))                         # one without an image link
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id["frlyon:254"]["name"], "Pont Poincaré, towards Lyon - Villeurbanne")
        tunnel = by_id["frlyon:261"]
        self.assertEqual(tunnel["name"], "Tunnel sous Fourvière, towards M6 - Paris")
        self.assertIn("tunnel", tunnel["tags"])
        for row in rows:
            self.assertEqual((row["country"], row["region"]), ("FR", "Métropole de Lyon"))
            self.assertTrue(host_allowed(row["image"], eu_lyon.SOURCE))
            self.assertTrue(45.6 < row["lat"] < 45.9 and 4.7 < row["lon"] < 5.1)
        self.assertEqual(eu_lyon.parse({"features": "nope"}, eu_lyon.SOURCE), ([], 0))
        self.assertEqual(eu_lyon.parse([], eu_lyon.SOURCE), ([], 0))

    def test_meteoswiss(self):
        lat, lon = eu_meteoswiss.lv95_to_wgs84(2_600_000, 1_200_000)           # Bern, old observatory
        self.assertAlmostEqual(lat, 46.95108, places=3)
        self.assertAlmostEqual(lon, 7.43864, places=3)
        rows, skipped = eu_meteoswiss.parse(fx_json("eu_meteoswiss.json"), eu_meteoswiss.SOURCE)
        self.assertEqual((len(rows), skipped), (3, 1))                         # one without a camera link
        by_id = {r["id"]: r for r in rows}
        chd = by_id["chmeteo:CHD"]
        self.assertEqual(chd["name"], "Château-d'Oex")
        self.assertAlmostEqual(chd["lat"], 46.4798, places=2)
        self.assertAlmostEqual(chd["lon"], 7.1397, places=2)
        self.assertEqual(chd["elevation"], 1032)
        self.assertTrue(chd["image"].startswith("https://backend.roundshot.com/cams/") and chd["image"].endswith("/quarter"))
        self.assertIn("mountain", by_id["chmeteo:COV"]["tags"])                # Piz Corvatsch, 3,307 m
        self.assertNotIn("mountain", by_id["chmeteo:LAN"]["tags"])             # Landquart, 548 m
        self.assertTrue(all(r["country"] == "CH" and "weather" in r["tags"] for r in rows))
        src = eu_meteoswiss.SOURCE
        self.assertTrue(host_allowed(chd["image"], src))
        # the stable link redirects to the newest dated panorama on the storage host
        self.assertTrue(host_allowed("https://storage2.roundshot.com/5f5a/2026-11-23/11-10-00/x_quarter.jpg", src))
        self.assertFalse(host_allowed("https://backend.roundshot.com/other/x.jpg", src))
        self.assertFalse(host_allowed("https://storage2.roundshot.com.example.org/x.jpg", src))

    def test_loire_atlantique(self):
        data = fx_json("eu_loireatlantique.json")
        rows, skipped = eu_loireatlantique.parse(data, eu_loireatlantique.SOURCE)
        self.assertEqual((len(rows), skipped), (3, 1))                         # unknown file name skipped
        by_id = {r["id"]: r for r in rows}
        bridge = by_id["frloire:psn"]
        self.assertEqual(bridge["name"], "Pont de Saint-Nazaire")
        self.assertIn("bridge", bridge["tags"])
        self.assertAlmostEqual(bridge["lat"], 47.283, places=2)
        self.assertEqual(by_id["frloire:indre1"]["region"], "Loire-Atlantique")
        for row in rows:
            self.assertTrue(host_allowed(row["image"], eu_loireatlantique.SOURCE))
        # a file link outside the dataset's own files is never used
        data["results"][0]["file"]["url"] = "https://example.org/psn.jpg"
        rows2, skipped2 = eu_loireatlantique.parse(data, eu_loireatlantique.SOURCE)
        self.assertEqual((len(rows2), skipped2), (2, 2))


class CentralTests(unittest.TestCase):
    """Germany, Austria, Italy, San Marino, Vatican, Malta: the A22 Brenner motorway cameras."""

    def test_a22_parse(self):
        rows, skipped = eu_a22.parse(fx_json("eu_a22.json"), eu_a22.SOURCE)
        # 3 cameras; one inactive and one without a position skipped; the feratel record is ignored
        self.assertEqual((len(rows), skipped), (3, 2))
        self.assertEqual([r["id"] for r in rows], ["ita22:A22_1", "ita22:A22_77", "ita22:A22_228"])
        first = rows[0]
        self.assertEqual(first["name"], "A22 km 1, Brennero")
        self.assertEqual((first["country"], first["road"], first["region"]), ("IT", "A22", "South Tyrol"))
        self.assertEqual(rows[2]["region"], "Veneto")
        self.assertEqual(first["description"], "Brenner motorway, km 1")
        for r in rows:
            self.assertEqual(r["kind"], "image")
            self.assertTrue(r["image"].startswith("https://www.autobrennero.it/WebCamImg/km"))
            self.assertTrue(host_allowed(r["image"], eu_a22.SOURCE))
            self.assertIn("traffic", r["tags"])
            self.assertEqual(r["type"], "traffic")
            self.assertEqual(r["refresh"], 60)
        self.assertFalse(any("feratel" in (r.get("image") or "") for r in rows))
        self.assertFalse(host_allowed("https://wtvthmb.feratel.com/thumbnails/6202.jpeg", eu_a22.SOURCE))
        self.assertEqual(eu_a22.parse(None, eu_a22.SOURCE), ([], 0))
        self.assertEqual(eu_a22.parse({"Items": "nope"}, eu_a22.SOURCE), ([], 0))

    def test_a22_regions_by_kilometre(self):
        r = eu_a22.region_of
        self.assertEqual([r(1), r(150), r(240), r(272), r(313)],
                         ["South Tyrol", "Trentino", "Veneto", "Lombardy", "Emilia-Romagna"])
        self.assertIsNone(r(None))
        self.assertIsNone(r(-3))

    def test_a22_fetch_asks_only_for_a22_and_pages(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            page = fx_json("eu_a22.json")
            calls = []

            class Net:
                def download(self, url, path, **kw):
                    calls.append(url)
                    doc = dict(page, TotalPages=2, CurrentPage=len(calls))
                    Path(path).write_text(json.dumps(doc), encoding="utf-8")
                    return Path(path)

            ds = FakeDS(tmp)
            fc = FetchContext(ds, eu_a22.SOURCE, config=no_keys(), net=Net())
            rows = eu_a22.fetch(fc)
            self.assertEqual(len(calls), 2)                     # TotalPages 2: two pages, then stop
            self.assertEqual(len(rows), 6)                      # the same 3 cameras on each fake page
            self.assertEqual(fc.skipped, 4)
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)
            self.assertEqual(q["source"], ["a22"])
            self.assertEqual(q["pagenumber"], ["1"])
            self.assertIn("Mapping", q["fields"][0])
            self.assertEqual(q["referer"], ["open-data-map-client"])  # generic, nothing personal
            self.assertTrue(calls[0].startswith("https://tourism.api.opendatahub.com/v1/WebcamInfo?"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a22_source_is_complete(self):
        s = eu_a22.SOURCE
        self.assertRegex(s.id, r"^[a-z0-9]{1,16}$")
        self.assertTrue(s.name and s.owner and s.url and s.licence and s.licence_url and s.attribution)
        self.assertEqual(s.hosts, ("www.autobrennero.it",))
        self.assertEqual((s.country, s.refresh, s.needs_key, s.optional), ("IT", 60, None, False))
        self.assertTrue(callable(s.fetch))
        self.assertTrue(re.search(r"A22", s.attribution))


class IberiaCentralTests(unittest.TestCase):
    """Hungary (Útinform), Catalonia (SCT, FGC) and the Basque Country (Bizkaia via Open Data Euskadi)."""

    NOW = 1795432400          # just after the fixture's Útinform images were taken

    def test_sources_are_complete(self):
        for src in (eu_hungary.SOURCE, eu_catalonia.SCT, eu_catalonia.FGC, eu_basque.SOURCE):
            self.assertRegex(src.id, r"^[a-z0-9]{1,16}$")
            self.assertTrue(src.name and src.owner and src.licence and src.licence_url and src.attribution, src.id)
            self.assertTrue(src.hosts and all(h == h.lower() for h in src.hosts), src.id)
            self.assertIsNone(src.needs_key)
            self.assertTrue(callable(src.fetch))
        self.assertEqual([s.id for s in eu_catalonia.SOURCES], ["escat", "escatfgc"])

    def test_hungary_utinform(self):
        data = fx_json("eu_hungary.json")
        rows, skipped = eu_hungary.parse(data, eu_hungary.SOURCE, now=self.NOW)
        self.assertEqual((len(rows), skipped), (3, 2))           # the unpublished site's two views are skipped
        first = rows[0]
        self.assertEqual(first["id"], "huutinform:mcs901_1")
        self.assertEqual(first["name"], "Tesztfalu, towards Mintaváros")
        self.assertEqual((first["country"], first["road"], first["region"], first["area"]),
                         ("HU", "Road 99", "Mintamegye", "Tesztfalu"))
        self.assertEqual(first["image"], "https://cdnuiwebcams.utinform.hu/webcamimages/mcs901_1.jpg")
        self.assertEqual((first["type"], first["refresh"]), ("traffic", 300))
        self.assertTrue(all(host_allowed(r["image"], eu_hungary.SOURCE) for r in rows))
        border = [r for r in rows if r["name"].startswith("Határfalu")]
        self.assertEqual(len(border), 1)
        self.assertIn("border", border[0]["tags"])                # "határátkelő": border crossing
        self.assertEqual(border[0]["name"], "Határfalu, towards the border crossing")
        self.assertEqual(eu_hungary.view_label("Petőfi utca, Kölesd felé"), "towards Petőfi utca, Kölesd")
        self.assertEqual(eu_hungary.view_label("Jobb oldal"), "right-hand view")
        # views without a new image for a week are left out
        rows, skipped = eu_hungary.parse(data, eu_hungary.SOURCE, now=self.NOW + 8 * 86400)
        self.assertEqual((len(rows), skipped), (0, 5))
        self.assertEqual(eu_hungary.parse({"features": "x"}, eu_hungary.SOURCE), ([], 0))
        self.assertEqual(eu_hungary.parse(None, eu_hungary.SOURCE), ([], 0))

    def test_catalonia_sct(self):
        rows, skipped = eu_catalonia.parse_sct(fx_bytes("eu_catalonia_sct.xml"), eu_catalonia.SCT)
        self.assertEqual([r["id"] for r in rows], ["escat:nc85", "escat:sc29", "escat:terrassa-cam01"])
        self.assertEqual(skipped, 2)                              # the repeated camera and Andorra's
        nc85 = rows[0]
        self.assertEqual(nc85["name"], "C-58 km 1.82, Montcada i Reixac")
        self.assertEqual((nc85["road"], nc85["area"], nc85["region"], nc85["country"]),
                         ("C-58", "Montcada i Reixac", "Catalonia", "ES"))
        # the render service only answered over https when tested; its redirect stays on the same prefix
        self.assertNotIn("image", nc85)                  # rebuilt at request time (see resolve_sct)
        self.assertTrue(nc85.get("dynamic"))
        self.assertEqual(eu_catalonia.resolve_sct(nc85, None),
                         "https://mct.gencat.cat/mct2bo/RenderService?sctidcam=nc85.gif")
        self.assertIsNone(eu_catalonia.resolve_sct({"id": "escat:../x"}, None))
        self.assertTrue(host_allowed("https://mct.gencat.cat/mct2bo/TransitCamera?nom=nc85.gif&visualitzacio=imatge",
                                     eu_catalonia.SCT))
        self.assertEqual(rows[1]["name"], "B-23 km 0, Barcelona")
        terrassa = rows[2]
        self.assertEqual(terrassa["image"], "https://emap.terrassa.cat/it_terrassa/cam01.jpeg?a=1")
        self.assertIsNone(eu_catalonia.resolve_sct(terrassa, None))      # cameras with their own image keep it
        self.assertIn("urban", terrassa["tags"])
        self.assertTrue(all(host_allowed(r.get("image") or eu_catalonia.resolve_sct(r, None), eu_catalonia.SCT)
                            for r in rows))
        self.assertFalse(host_allowed("https://imgs.mobilitat.ad/prod/Bartra.gif", eu_catalonia.SCT))
        self.assertEqual(eu_catalonia._ca_title("L'HOSPITALET DE LLOBREGAT"), "L'Hospitalet de Llobregat")
        self.assertEqual(eu_catalonia._ca_title("SANT ADRIÀ DE BESÒS"), "Sant Adrià de Besòs")

    def test_catalonia_fgc(self):
        rows, skipped = eu_catalonia.parse_fgc(fx_json("eu_catalonia_fgc.json"), eu_catalonia.FGC)
        by = {r["id"]: r for r in rows}
        self.assertEqual(sorted(by), ["escatfgc:19", "escatfgc:55", "escatfgc:62"])
        self.assertEqual(skipped, 1)                              # the 360° panorama is only a web page
        self.assertEqual(by["escatfgc:55"]["name"], "La Molina, Cap de Comella Sup")
        self.assertEqual(by["escatfgc:55"]["image"], "https://media.turismefgc.cat/lamolina/comella.jpg")
        # a placeholder position (near Madrid) is replaced by its resort's position
        self.assertEqual((by["escatfgc:19"]["lat"], by["escatfgc:19"]["lon"]), (42.33504, 1.93699))
        self.assertEqual(by["escatfgc:19"]["image"], "https://api.pirineu365.cat/webcam/19")
        self.assertTrue(all(r["type"] == "webcam" and "mountain" in r["tags"] for r in rows))
        self.assertTrue(all(host_allowed(r["image"], eu_catalonia.FGC) for r in rows))
        # TV3's link redirects to its still on statics.3cat.cat
        self.assertTrue(host_allowed("https://statics.3cat.cat/meteo/beauties/v1/img/7/1/21195417.jpg", eu_catalonia.FGC))
        self.assertEqual(eu_catalonia.parse_fgc({"results": []}, eu_catalonia.FGC), ([], 0))

    def test_basque_bizkaia(self):
        cams = fx_json("eu_basque_p1.json")["cameras"] + fx_json("eu_basque_p2.json")["cameras"]
        rows, skipped, by_publisher = eu_basque.parse(cams, eu_basque.SOURCE)
        self.assertEqual([r["id"] for r in rows], ["esbizkaia:2-77", "esbizkaia:2-4"])
        self.assertEqual(skipped, 3)                              # a repeated image, Trafikoa's and Bilbao's links
        self.assertEqual(by_publisher, {"2": 1, "1": 1, "5": 1})
        first = rows[0]
        self.assertEqual(first["name"], "BI-637 km 8.5, Nudo Kukularra, towards Kurtzes")
        self.assertEqual((first["road"], first["region"], first["country"]), ("BI-637", "Bizkaia", "ES"))
        self.assertTrue(43.29 < first["lat"] < 43.32 and -2.98 < first["lon"] < -2.94)    # UTM 30N converted
        self.assertEqual(rows[1]["name"], "A8 km 105.7, El Gallo, towards Donostia")
        self.assertEqual(rows[1]["image"], "http://www.bizkaimove.com/camaras/cam86.jpg")
        self.assertTrue(all(host_allowed(r["image"], eu_basque.SOURCE) for r in rows))
        self.assertEqual(eu_basque._label("SLO_DOM_001D (18) PK 75+500"), "")
        self.assertEqual(eu_basque._label("CCTV 205 - Cámara DOMO en Nudo Asua"), "Nudo Asua")
        self.assertEqual(eu_basque._km("008+500"), "8.5")

    def test_basque_positions(self):
        self.assertEqual(eu_basque.position("43.1685", "-2.568193"), (43.1685, -2.568193))   # degrees pass through
        self.assertEqual(eu_basque.position("x", "1"), (None, None))
        # on the central meridian (3° W), 4,982,950.40 m north is 45° N (0.9996 x the 45° meridian arc)
        lat, lon = eu_basque.utm30_to_wgs84(500000.0, 0.9996 * 4984944.378)
        self.assertAlmostEqual(lat, 45.0, places=6)
        self.assertAlmostEqual(lon, -3.0, places=9)

    def test_fetch_glue(self):
        """Each fetch reads its feed through the FetchContext helpers (no network here)."""
        tmp = Path(tempfile.mkdtemp())
        try:
            # Basque: every page up to totalPages, one request each
            pages = {eu_basque.URL.format(page=1): fx_json("eu_basque_p1.json"),
                     eu_basque.URL.format(page=2): fx_json("eu_basque_p2.json")}
            fc = FetchContext(FakeDS(tmp), eu_basque.SOURCE, config=no_keys(), net=mock.Mock())
            asked = []
            fc.json = lambda url, filename, **kw: (asked.append(url), pages[url])[1]
            self.assertEqual(len(eu_basque.fetch(fc)), 2)
            self.assertEqual((len(asked), fc.skipped), (2, 3))
            self.assertTrue(any(n.startswith("left out") for n in fc.notes))
            # Catalonia: the SCT XML download and the FGC export
            fc = FetchContext(FakeDS(tmp), eu_catalonia.SCT, config=no_keys(), net=mock.Mock())
            fc.download = lambda url, filename, **kw: FIX / "eu_catalonia_sct.xml"
            self.assertEqual(len(eu_catalonia.fetch_sct(fc)), 3)
            fc = FetchContext(FakeDS(tmp), eu_catalonia.FGC, config=no_keys(), net=mock.Mock())
            fc.json = lambda url, filename, **kw: fx_json("eu_catalonia_fgc.json")
            self.assertEqual(len(eu_catalonia.fetch_fgc(fc)), 3)
            # Hungary: one list (the clock is pinned to just after the fixture's images)
            fc = FetchContext(FakeDS(tmp), eu_hungary.SOURCE, config=no_keys(), net=mock.Mock())
            fc.json = lambda url, filename, **kw: fx_json("eu_hungary.json")
            with mock.patch.object(eu_hungary.time, "time", return_value=self.NOW):
                self.assertEqual(len(eu_hungary.fetch(fc)), 3)
            self.assertEqual(fc.skipped, 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _southeast_keyed(value):
    """A config that only knows one key."""
    return mock.Mock(get=lambda name, default=None: value if name.endswith("_NAP_KEY") else default,
                     has=lambda name: name.endswith("_NAP_KEY"))


class _southeast_FakeNet:
    """Records requests: POSTs answer with a token, downloads copy a fixture into the raw file."""

    def __init__(self, fixture: str, token: bytes = b'{"access_token": "tok-123", "token_type": "bearer"}'):
        self.fixture = fixture
        self.token = token
        self.posts: list[dict] = []
        self.downloads: list[dict] = []

    def request(self, url, **kw):
        self.posts.append({"url": url, **kw})
        return NetResponse(200, {"content-type": "application/json"}, self.token, url)

    def download(self, url, dest, **kw):
        self.downloads.append({"url": url, **kw})
        Path(dest).write_bytes(fx_bytes(self.fixture))
        return Path(dest)


class SouthEastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fc(self, src, net, key):
        ds = FakeDS(self.tmp / src.id)
        ds.net = net
        return FetchContext(ds, src, config=_southeast_keyed(key), net=net)

    # -- Slovenia: DARS and DRSI cameras from NAP Slovenia ---------------------------------------
    def test_slovenia_parse(self):
        src = eu_slovenia.SOURCE
        rows, skipped = eu_slovenia.parse(fx_json("eu_slovenia.json"), src)
        self.assertEqual((len(rows), skipped), (4, 1))                    # the inactive camera is skipped
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(sorted(by_id), ["sinap:1", "sinap:2", "sinap:319", "sinap:88"])
        log = by_id["sinap:1"]
        self.assertEqual(log["name"], "Log pri Brezovici, rotating camera I")
        self.assertEqual((log["road"], log["country"], log["region"]), ("A1", "SI", "Primorska in Notranjska"))
        self.assertIn("E61", log["description"])                         # E-road numbers stay searchable
        predel = by_id["sinap:88"]
        self.assertEqual(predel["name"], "Border crossing Predel, towards Bovec")
        self.assertEqual(predel["road"], "R1-203")
        self.assertIn("border", predel["tags"])
        self.assertEqual(by_id["sinap:319"]["road"], "R2-409")
        for r in rows:
            self.assertTrue(host_allowed(r["image"], src), r["image"])
            self.assertIn("traffic", r["tags"])
            self.assertEqual(r["kind"], "image")
        self.assertEqual(len({r["name"] for r in rows}), 4)

    def test_slovenia_names(self):
        split = eu_slovenia.split_text
        self.assertEqual(split("A1/E61/E70, Ljubljana - Koper, Log pri Brezovici, rotating camera I"),
                         ("A1", "Ljubljana - Koper", "Log pri Brezovici, rotating camera I"))
        self.assertEqual(split("A1/E57 Maribor - Ljubljana, Slivnica junction"),
                         ("A1", "Maribor - Ljubljana", "Slivnica junction"))
        self.assertEqual(split("A1/E57, east Ljubljana bypass, Litijska - Malence"),
                         ("A1", "east Ljubljana bypass", "Litijska - Malence"))
        self.assertEqual(split("A2/E70, Ljubljana - Obrežje, Prilipe, view in the direction of Novo mesto"),
                         ("A2", "Ljubljana - Obrežje", "Prilipe, towards Novo mesto"))
        self.assertEqual(split("H4, Razdrto - Vrtojba, Podnanos II"), ("H4", "Razdrto - Vrtojba", "Podnanos II"))
        rows = [{"name": "Moste tunnel"}, {"name": "Moste tunnel"}, {"name": "Kozarje"}]
        eu_slovenia.number_repeats(rows)
        self.assertEqual([r["name"] for r in rows], ["Moste tunnel", "Moste tunnel (2)", "Kozarje"])

    def test_slovenia_needs_key(self):
        needs_key_without_config(self, eu_slovenia.SOURCE)
        net = _southeast_FakeNet("eu_slovenia.json")
        with self.assertRaises(RuntimeError) as ctx:                      # malformed login, no request made
            eu_slovenia.fetch(self._fc(eu_slovenia.SOURCE, net, "no-colon-here"))
        self.assertNotIn("no-colon-here", str(ctx.exception))
        self.assertEqual((net.posts, net.downloads), ([], []))

    def test_slovenia_fetch_uses_an_access_token(self):
        net = _southeast_FakeNet("eu_slovenia.json")
        fc = self._fc(eu_slovenia.SOURCE, net, "me@example.org:s3cret:with-colon")
        rows = eu_slovenia.fetch(fc)
        self.assertEqual((len(rows), fc.skipped), (4, 1))
        self.assertEqual(len(net.posts), 1)
        post = net.posts[0]
        self.assertEqual((post["url"], post["method"]), (eu_slovenia.TOKEN_URL, "POST"))
        form = urllib.parse.parse_qs(post["data"].decode())
        self.assertEqual(form, {"grant_type": ["password"], "username": ["me@example.org"],
                                "password": ["s3cret:with-colon"]})
        self.assertEqual(net.downloads[0]["url"], eu_slovenia.DATA_URL)
        self.assertEqual(net.downloads[0]["headers"]["Authorization"], "Bearer tok-123")
        self.assertNotIn("s3cret", net.downloads[0]["url"])
        # a fresh raw copy is reused: no new token, no new download
        eu_slovenia.fetch(self._fc(eu_slovenia.SOURCE, net, "me@example.org:s3cret:with-colon"))
        self.assertEqual((len(net.posts), len(net.downloads)), (1, 1))

    def test_slovenia_without_a_token_keeps_the_last_list(self):
        net = _southeast_FakeNet("eu_slovenia.json", token=b'{"error": "invalid_grant"}')
        with self.assertRaises(RuntimeError):                             # no token and nothing cached
            eu_slovenia.fetch(self._fc(eu_slovenia.SOURCE, net, "me@example.org:wrong"))
        fc = self._fc(eu_slovenia.SOURCE, net, "me@example.org:wrong")
        fc.raw_path(eu_slovenia.RAW_FILE).write_bytes(fx_bytes("eu_slovenia.json"))
        fc.force = True                                                    # stale copy: try again, fall back
        self.assertEqual(len(eu_slovenia.fetch(fc)), 4)
        self.assertTrue(any("last list" in n for n in fc.notes))

    # -- Croatia: HAC motorway cameras from the Croatian NAP -------------------------------------
    def test_croatia_parse(self):
        src = eu_croatia.SOURCE
        rows, skipped = eu_croatia.parse(fx_bytes("eu_croatia.xml"), src)
        self.assertEqual((len(rows), skipped), (4, 1))                    # the restricted camera is skipped
        by_id = {r["id"]: r for r in rows}
        self.assertNotIn("hrhac:A1-BOS-999999", by_id)
        self.assertEqual(by_id["hrhac:A1-DUG-84010"]["name"], "Čvor Zagvozd, km 425.5, camera 1, DU")
        self.assertEqual(by_id["hrhac:A1-DUG-84085"]["name"], "Čvor Zagvozd, km 425.5, camera 1, ZG")
        tunnel = by_id["hrhac:A1-MAK-94001"]
        self.assertEqual(tunnel["name"], "Tunel Mala Kapela, km 102.5, camera 3")
        self.assertIn("tunnel", tunnel["tags"])
        self.assertEqual(tunnel["road"], "A1")
        istra = by_id["hrhac:25100241666368"]
        self.assertEqual((istra["name"], istra["region"]), ("Čvor Vodnjan Jug", "Južna Istra"))
        self.assertNotIn("road", istra)
        for r in rows:
            self.assertEqual((r["country"], r["kind"]), ("HR", "image"))
            self.assertTrue(host_allowed(r["image"], src), r["image"])
        self.assertFalse(host_allowed("https://evil.example/x.jpg", src))

    def test_croatia_names(self):
        np = eu_croatia.name_parts
        self.assertEqual(np("A1 5 DU 049+914  Čvor Novigrad VK01", "Čvor Novigrad"),
                         ("Čvor Novigrad, km 49.9, camera 1", "A1", "Čvor Novigrad"))
        self.assertEqual(np("A5 5 OS 069+025 PUO Ivandvor VK2", "PUO Ivandvor")[0], "PUO Ivandvor, km 69.0, camera 2")
        self.assertEqual(np("CCTV32- Naplata Pula", "Prometna kamera")[0], "Naplata Pula")
        self.assertEqual(np("CCTV - Čvor Vranja", "Prometna kamera")[0], "Čvor Vranja")

    def test_croatia_needs_key_and_sends_basic_auth(self):
        needs_key_without_config(self, eu_croatia.SOURCE)
        net = _southeast_FakeNet("eu_croatia.xml")
        fc = self._fc(eu_croatia.SOURCE, net, "me@example.org:pa:ss")
        rows = eu_croatia.fetch(fc)
        self.assertEqual((len(rows), fc.skipped), (4, 1))
        self.assertEqual(net.posts, [])
        sent = net.downloads[0]
        self.assertEqual(sent["url"], eu_croatia.DATA_URL)
        want = "Basic " + base64.b64encode(b"me@example.org:pa:ss").decode()
        self.assertEqual(sent["headers"]["Authorization"], want)
        self.assertNotIn("pa:ss", sent["url"])

    def test_southeast_sources_are_complete(self):
        for src in (eu_slovenia.SOURCE, eu_croatia.SOURCE):
            self.assertRegex(src.id, r"^[a-z0-9]{1,16}$")
            self.assertTrue(src.name and src.owner and src.url and src.licence and src.attribution, src.id)
            self.assertTrue(src.hosts and callable(src.fetch) and src.optional and src.needs_key, src.id)
            self.assertTrue(src.needs_key.endswith("_KEY"), src.id)


class SpainGenevaTests(unittest.TestCase):
    """City and regional sources found in the European open data portal: Madrid, Málaga,
    MeteoGalicia and Geneva."""

    def test_madrid(self):
        rows, skipped = eu_madrid.parse_city(fx_bytes("eu_madrid.kml"), eu_madrid.SOURCE)
        self.assertEqual((len(rows), skipped), (2, 1))                 # the camera without a picture is skipped
        self.assertEqual([r["id"] for r in rows], ["esmadrid:06303", "esmadrid:06304"])
        self.assertEqual(rows[0]["name"], "Plaza de Castilla (Norte)")  # ALL CAPS names title-cased
        for r in rows:
            self.assertTrue(host_allowed(r["image"], eu_madrid.SOURCE))
            self.assertNotIn("?", r["image"])                           # the cache buster is dropped
            self.assertEqual((r["country"], r["area"], r["type"]), ("ES", "Madrid", "traffic"))
            self.assertIn("urban", r["tags"])

    def test_malaga(self):
        rows, skipped = eu_malaga.parse(fx_bytes("eu_malaga.kml"), eu_malaga.SOURCE)
        self.assertEqual((len(rows), skipped), (2, 1))                 # a camera of another owner is skipped
        first = rows[0]
        self.assertEqual(first["id"], "esmalaga:21611")
        self.assertEqual(first["name"], "C/ José Calderón - El Boni")
        self.assertEqual(first["description"], "El Boni - Campanillas")
        self.assertTrue(first["page"].startswith("https://movilidad.malaga.eu/"))
        self.assertTrue(all(host_allowed(r["image"], eu_malaga.SOURCE) for r in rows))

    def test_meteogalicia(self):
        now = datetime(2026, 11, 23, 13, 0).timestamp()
        rows, skipped = eu_meteogalicia.parse(fx_json("eu_meteogalicia.json"), eu_meteogalicia.SOURCE, now=now)
        self.assertEqual((len(rows), skipped), (3, 1))                 # the stale camera is skipped
        ids = [r["id"] for r in rows]
        self.assertEqual(len(set(ids)), 3)                             # two views of one station stay apart
        self.assertIn("esmeteogalicia:Onsplaya", ids)
        by_name = {r["name"]: r for r in rows}
        self.assertIn("port", by_name["Ons Porto"]["tags"])
        self.assertIn("coastal", by_name["Ons Praia"]["tags"])
        for r in rows:
            self.assertEqual(r["tags"].count("weather"), 1)
            self.assertEqual((r["type"], r["region"] in ("Pontevedra", "Lugo")), ("webcam", True))
            self.assertTrue(host_allowed(r["image"], eu_meteogalicia.SOURCE))
            self.assertTrue(r["image"].endswith("/ultima.jpg"))       # always the latest picture

    def test_geneva(self):
        rows, skipped = eu_geneva.parse(fx_json("eu_geneva.json")["features"], eu_geneva.SOURCE)
        self.assertEqual((len(rows), skipped), (3, 1))                 # the site without a picture is skipped
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(set(by_id), {"chgeneve:cam_2", "chgeneve:cam_14", "chgeneve:cam_16"})
        self.assertIn("border", by_id["chgeneve:cam_14"]["tags"])     # Bardonnex customs post
        self.assertEqual(by_id["chgeneve:cam_2"]["description"], "Towards Lausanne and France")
        self.assertEqual(by_id["chgeneve:cam_16"]["description"], "Towards Geneva and France")
        self.assertTrue(all(host_allowed(r["image"], eu_geneva.SOURCE) and r["country"] == "CH" for r in rows))

    def test_query_only_image_links_survive_dedupe(self):
        """The shared build drops rows whose image links match once the query string is removed;
        SCT and Durham weather stills differ only in the query, so their rows carry no image link and
        the source's resolver rebuilds it. Every camera survives the build's duplicate check."""
        sct, _ = eu_catalonia.parse_sct(fx_bytes("eu_catalonia_sct.xml"), eu_catalonia.SCT)
        weather, _ = eu_durham.parse_weather(fx_json("eu_durham_weather.json")["features"], eu_durham.SOURCE)
        for rows in (sct, weather):
            kept, dropped = build_mod.dedupe(list(rows))
            self.assertEqual((len(kept), dropped), (len(rows), 0))

    def test_malaga_lists_each_picture_once(self):
        data = fx_bytes("eu_malaga.kml").decode("utf-8")
        first = data[data.index("<Placemark"):data.index("</Placemark>") + len("</Placemark>")]
        copy = first.replace('<SimpleData name="ID">21611', '<SimpleData name="ID">99999')   # same picture
        twice = data.replace(first, first + "\n" + copy, 1)
        rows, skipped = eu_malaga.parse(twice.encode("utf-8"), eu_malaga.SOURCE)
        self.assertEqual((len(rows), skipped), (2, 2))                 # the repeat and the other owner's camera
        self.assertNotIn("esmalaga:99999", [r["id"] for r in rows])


if __name__ == "__main__":
    unittest.main()
