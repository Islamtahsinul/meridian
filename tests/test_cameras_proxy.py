"""Cameras image proxy: URL safety (no SSRF), image type checks, size limit, cache, revalidation,
cool-downs, negative caching, hand-followed redirects and memory bounds. No network."""

from __future__ import annotations

import http.client
import unittest

from meridian.modules.cameras.common import Source
from meridian.modules.cameras.proxy import (ImageProxy, ProxyError, UrlRefused, check_url, host_allowed, image_type,
                                            public_ip, retry_after_seconds, sniff)
from meridian.net import NetError, NetResponse

JPEG = b"\xff\xd8\xff\xe0" + b"\x10" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
SRC = Source(id="t", name="Test cams", owner="o", url="https://t.test", licence="L", attribution="A",
             hosts=("img.t.test", "*.cdn.test", "s3.test/bucket/"), ports=(8443,), refresh=60)

PUBLIC = {"img.t.test": ["93.184.216.34"], "a.cdn.test": ["2606:2800:220:1:248:1893:25c8:1946"],
          "s3.test": ["52.95.0.1"], "evil.t.test": ["10.0.0.7"], "img2.cdn.test": ["127.0.0.1"]}


def resolver(host):
    if host not in PUBLIC:
        raise UrlRefused(f"{host} does not resolve")
    return PUBLIC[host]


def cam(cid="t:1", url="https://img.t.test/1.jpg", refresh=60):
    return {"id": cid, "image": url, "refresh": refresh}


class FakeNet:
    """Scripted answers per URL. Honours ``accept`` like the real client (other statuses raise)."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls: list[tuple[str, dict]] = []

    def request(self, url, **kw):
        self.calls.append((url, kw))
        answers = self.script.get(url)
        if answers is None:
            raise NetError("t: HTTP 404", status=404)
        item = answers.pop(0) if isinstance(answers, list) and len(answers) > 1 else (answers[0] if isinstance(answers, list) else answers)
        if isinstance(item, Exception):
            raise item
        status, headers, body = item
        if status not in kw.get("accept", (200,)):
            raise NetError(f"t: HTTP {status}", status=status)
        return NetResponse(status, {k.lower(): v for k, v in headers.items()}, body, url)


def proxy(net, **kw):
    return ImageProxy(net, resolver=resolver, spacing=0, **kw)


class UrlSafetyTests(unittest.TestCase):
    def test_allowlist_patterns(self):
        self.assertTrue(host_allowed("https://img.t.test/x.jpg", SRC))
        self.assertTrue(host_allowed("https://a.cdn.test/x.jpg", SRC))
        self.assertTrue(host_allowed("https://cdn.test/x.jpg", SRC))
        self.assertTrue(host_allowed("https://s3.test/bucket/x.jpg", SRC))
        self.assertFalse(host_allowed("https://s3.test/other/x.jpg", SRC))       # path prefix enforced
        self.assertFalse(host_allowed("https://img.t.test.evil.test/x.jpg", SRC))
        self.assertFalse(host_allowed("https://xcdn.test/x.jpg", SRC))

    def test_path_prefixes_keep_their_case(self):
        src = Source(id="on", name="511", owner="o", url="u", licence="L", attribution="A", hosts=("511on.ca/map/Cctv/",))
        self.assertTrue(host_allowed("https://511on.ca/map/Cctv/123", src))
        self.assertTrue(host_allowed("https://511ON.CA/map/Cctv/123", src))         # host names ignore case
        self.assertFalse(host_allowed("https://511on.ca/map/cctv/123", src))        # paths do not
        self.assertFalse(host_allowed("https://511on.ca/other/123", src))

    def test_refusals(self):
        bad = [
            "file:///etc/passwd", "ftp://img.t.test/x.jpg", "gopher://img.t.test/", "//img.t.test/x.jpg",
            "https://user:pw@img.t.test/x.jpg", "https://img.t.test@evil.test/x.jpg",
            "https://img.t.test:22/x.jpg", "https://other.test/x.jpg", "https://evil.t.test/x.jpg",
            "https://img2.cdn.test/x.jpg", "http://127.0.0.1/x.jpg", "http://169.254.169.254/latest/meta-data",
            "http://[::1]/x.jpg", "", None, 42, "https://" + "a" * 3000,
        ]
        for url in bad:
            with self.assertRaises(UrlRefused, msg=repr(url)):
                check_url(url, SRC, resolver)

    def test_accepted(self):
        for url in ("https://img.t.test/x.jpg", "http://img.t.test/x.jpg?t=1", "https://img.t.test:443/x.jpg",
                    "https://img.t.test:8443/x.jpg", "https://a.cdn.test/x.jpg", "https://s3.test/bucket/x/y.jpg",
                    "https://img.t.test/a..b/x.jpg?d=../x", "https://img.t.test/x%20y.jpg"):
            self.assertEqual(check_url(url, SRC, resolver), url)

    def test_path_prefix_cannot_be_escaped_with_dot_segments(self):
        # the prefix check looks at the raw path; servers resolve dot segments (also %2e and "\")
        for url in ("https://s3.test/bucket/../other/x.jpg", "https://s3.test/bucket/%2e%2e/other/x.jpg",
                    "https://s3.test/bucket/.%2E/other/x.jpg", "https://s3.test/bucket/..%5Cother/x.jpg",
                    "https://s3.test/bucket/..%2Fother/x.jpg", "https://s3.test/bucket/./x.jpg"):
            with self.assertRaises(UrlRefused, msg=url):
                check_url(url, SRC, resolver)

    def test_unsendable_characters_are_refused_not_crashed_on(self):
        # http.client raises InvalidURL / UnicodeEncodeError for these, which m.net doesn't turn into NetError
        for url in ("https://img.t.test/a b.jpg", "https://img.t.test/x.jpg\r\nX-Evil: 1", "https://img.t.test/\x00.jpg",
                    "https://img.t.test/café.jpg", "https://img.t.test\\@evil.test/x.jpg",
                    "https://img.t.test/x.jpg\t", "https://img.t.test/x\x7f.jpg"):
            with self.assertRaises(UrlRefused, msg=repr(url)):
                check_url(url, SRC, resolver)

    def test_public_ip(self):
        for ip in ("10.1.2.3", "192.168.0.1", "172.16.0.1", "127.0.0.1", "169.254.1.1", "100.64.0.1", "0.0.0.0",
                   "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1"):
            self.assertFalse(public_ip(ip), ip)
        for ip in ("8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"):
            self.assertTrue(public_ip(ip), ip)


class ImageTypeTests(unittest.TestCase):
    def test_sniff_and_types(self):
        self.assertEqual(sniff(JPEG), "image/jpeg")
        self.assertEqual(sniff(PNG), "image/png")
        self.assertEqual(sniff(b"GIF89a....."), "image/gif")
        self.assertEqual(sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertIsNone(sniff(b"<html>"))
        self.assertEqual(image_type("application/octet-stream", JPEG), "image/jpeg")   # data.gov.sg does this
        self.assertEqual(image_type("image/png; charset=binary", PNG), "image/png")
        self.assertEqual(image_type("image/jpeg", PNG), "image/png")                     # bytes win
        for ctype, body in (("text/html", b"<html>"), ("image/svg+xml", b"<svg onload=x>"), ("image/jpeg", b"<html>"),
                            ("text/html", JPEG), ("application/json", b"{}")):
            with self.assertRaises(ProxyError):
                image_type(ctype, body)

    def test_retry_after(self):
        self.assertEqual(retry_after_seconds("120"), 120)
        self.assertEqual(retry_after_seconds("1"), 5)
        self.assertEqual(retry_after_seconds("99999"), 900)
        self.assertEqual(retry_after_seconds(None), 60)


class ProxyTests(unittest.TestCase):
    URL = "https://img.t.test/1.jpg"

    def test_fetch_then_cache(self):
        net = FakeNet({self.URL: [(200, {"Content-Type": "image/jpeg", "ETag": '"a"',
                                         "Last-Modified": "Sun, 22 Nov 2026 21:16:54 GMT"}, JPEG)]})
        p = proxy(net)
        e = p.get(cam(), SRC)
        self.assertEqual((e.ctype, e.body), ("image/jpeg", JPEG))
        self.assertIsNotNone(e.upstream_time)
        p.get(cam(), SRC)
        self.assertEqual(len(net.calls), 1)                       # second call hit the cache
        kw = net.calls[0][1]
        self.assertEqual(kw["max_bytes"], 3_000_000)
        self.assertFalse(kw["allow_redirects"])
        self.assertEqual(kw["retries"], 0)
        self.assertLessEqual(kw["timeout"], 10)
        self.assertTrue(p.meta("t:1")["ok"])

    def test_revalidation_304(self):
        net = FakeNet({self.URL: [(200, {"Content-Type": "image/jpeg", "ETag": '"a"'}, JPEG), (304, {}, b"")]})
        p = proxy(net, min_ttl=0, max_ttl=0)
        p.get(cam(), SRC)
        e = p.get(cam(), SRC)
        self.assertEqual(e.body, JPEG)
        self.assertEqual(net.calls[1][1]["headers"].get("If-None-Match"), '"a"')
        self.assertEqual(p.stats["not_modified"], 1)

    def test_not_an_image_and_negative_cache(self):
        net = FakeNet({self.URL: [(200, {"Content-Type": "text/html"}, b"<html>login</html>")]})
        p = proxy(net)
        with self.assertRaises(ProxyError) as ctx:
            p.get(cam(), SRC)
        self.assertEqual(ctx.exception.code, "NOT_IMAGE")
        with self.assertRaises(ProxyError):
            p.get(cam(), SRC)
        self.assertEqual(len(net.calls), 1)                       # negative cache: not asked again

    def test_too_large(self):
        net = FakeNet({self.URL: NetError("img.t.test: response larger than 3000000 bytes", status=200)})
        with self.assertRaises(ProxyError) as ctx:
            proxy(net).get(cam(), SRC)
        self.assertEqual(ctx.exception.code, "TOO_LARGE")

    def test_rate_limit_cools_the_host_down(self):
        net = FakeNet({self.URL: [(429, {"Retry-After": "120"}, b"")], "https://img.t.test/2.jpg": [(200, {}, JPEG)]})
        p = proxy(net)
        with self.assertRaises(ProxyError) as ctx:
            p.get(cam(), SRC)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (503, "HOST_COOLING_DOWN"))
        self.assertGreaterEqual(ctx.exception.retry_after, 100)
        with self.assertRaises(ProxyError):
            p.get(cam("t:2", "https://img.t.test/2.jpg"), SRC)   # same host waits too
        self.assertEqual(len(net.calls), 1)

    def test_repeated_failures_back_off(self):
        net = FakeNet({f"https://img.t.test/{i}.jpg": NetError("img.t.test: timed out") for i in range(5)})
        p = proxy(net)
        for i in range(4):
            with self.assertRaises(ProxyError):
                p.get(cam(f"t:{i}", f"https://img.t.test/{i}.jpg"), SRC)
        self.assertEqual(len(net.calls), 3)                      # the fourth is refused during the cool-down

    def test_stale_frame_is_served_when_the_camera_stops(self):
        net = FakeNet({self.URL: [(200, {}, JPEG), NetError("img.t.test: timed out")]})
        p = proxy(net, min_ttl=0, max_ttl=0)
        p.get(cam(), SRC)
        e = p.get(cam(), SRC)
        self.assertTrue(e.stale)
        self.assertEqual(e.body, JPEG)

    def test_redirects_are_checked_before_following(self):
        net = FakeNet({
            self.URL: [(302, {"Location": "https://a.cdn.test/1.jpg"}, b"")],
            "https://a.cdn.test/1.jpg": [(200, {"Content-Type": "image/jpeg"}, JPEG)],
            "https://img.t.test/2.jpg": [(301, {"Location": "http://169.254.169.254/latest/meta-data"}, b"")],
            "https://img.t.test/3.jpg": [(302, {"Location": "/3.jpg"}, b"")],
        })
        p = proxy(net)
        self.assertEqual(p.get(cam(), SRC).body, JPEG)
        with self.assertRaises(ProxyError) as ctx:
            p.get(cam("t:2", "https://img.t.test/2.jpg"), SRC)
        self.assertEqual(ctx.exception.code, "REDIRECT_REFUSED")
        self.assertNotIn("http://169.254.169.254/latest/meta-data", [u for u, _ in net.calls])   # never requested
        with self.assertRaises(ProxyError) as ctx:
            p.get(cam("t:3", "https://img.t.test/3.jpg"), SRC)
        self.assertEqual(ctx.exception.code, "TOO_MANY_REDIRECTS")

    def test_dataset_url_on_a_foreign_host_is_refused(self):
        net = FakeNet()
        with self.assertRaises(ProxyError) as ctx:
            proxy(net).get(cam(url="http://127.0.0.1:8790/api/core/status"), SRC)
        self.assertEqual(ctx.exception.code, "URL_REFUSED")
        self.assertEqual(net.calls, [])

    def test_resolver_for_dynamic_sources(self):
        net = FakeNet({"https://img.t.test/now.jpg": [(200, {}, JPEG)]})
        p = proxy(net)
        e = p.get({"id": "t:9", "refresh": 60}, SRC, resolve=lambda c, n: "https://img.t.test/now.jpg")
        self.assertEqual(e.body, JPEG)
        with self.assertRaises(ProxyError) as ctx:
            p.get({"id": "t:8", "refresh": 60}, SRC, resolve=lambda c, n: "http://10.0.0.1/x.jpg")
        self.assertEqual(ctx.exception.code, "URL_REFUSED")

    def test_broken_http_from_the_camera_fails_soft(self):
        # a bad chunked body or status line escapes m.net as http.client.HTTPException: a 502, not a 500
        net = FakeNet({self.URL: http.client.IncompleteRead(b""),
                       "https://img.t.test/2.jpg": http.client.BadStatusLine("garbage")})
        p = proxy(net)
        for c in (cam(), cam("t:2", "https://img.t.test/2.jpg")):
            with self.assertRaises(ProxyError) as ctx:
                p.get(c, SRC)
            self.assertEqual((ctx.exception.status, ctx.exception.code), (502, "UPSTREAM_ERROR"))

    def test_resolver_with_an_odd_answer_fails_soft(self):
        def odd(cam_row, net):
            return [][0]          # IndexError, as from an empty or reshaped API answer
        with self.assertRaises(ProxyError) as ctx:
            proxy(FakeNet()).get({"id": "t:7", "refresh": 60}, SRC, resolve=odd)
        self.assertEqual(ctx.exception.code, "LOOKUP_FAILED")

    def test_resolver_url_with_spaces_is_refused_before_any_request(self):
        net = FakeNet()
        with self.assertRaises(ProxyError) as ctx:
            proxy(net).get({"id": "t:6", "refresh": 60}, SRC, resolve=lambda c, n: "https://img.t.test/now 1.jpg")
        self.assertEqual(ctx.exception.code, "URL_REFUSED")
        self.assertEqual(net.calls, [])

    def test_cache_is_bounded(self):
        script = {f"https://img.t.test/{i}.jpg": [(200, {}, JPEG)] for i in range(10)}
        p = proxy(FakeNet(script), max_entries=3, max_total=10_000_000)
        for i in range(10):
            p.get(cam(f"t:{i}", f"https://img.t.test/{i}.jpg"), SRC)
        info = p.cache_info()
        self.assertEqual(info["entries"], 3)
        p2 = proxy(FakeNet(script), max_entries=100, max_total=len(JPEG) * 2)
        for i in range(10):
            p2.get(cam(f"t:{i}", f"https://img.t.test/{i}.jpg"), SRC)
        self.assertLessEqual(p2.cache_info()["cached_bytes"], len(JPEG) * 2)

    def test_slow_hosts_get_their_own_timeout(self):
        from meridian.modules.cameras.proxy import image_timeout
        slow = Source(id="s", name="Slow", owner="o", url="u", licence="L", attribution="A", hosts=("img.t.test",),
                      image_timeout=25.0)
        net = FakeNet({"https://img.t.test/1.jpg": (200, {"content-type": "image/jpeg"}, JPEG)})
        proxy(net).get(cam(), slow)
        self.assertEqual(net.calls[-1][1]["timeout"], 25.0)
        proxy(net).get(cam("t:2", "https://img.t.test/1.jpg"), SRC)
        self.assertEqual(net.calls[-1][1]["timeout"], 10.0)                     # the default
        self.assertEqual(image_timeout(Source(id="x", name="x", owner="o", url="u", licence="L", attribution="A",
                                              image_timeout=500)), 30.0)          # kept within 3-30 s

    def test_ttl_follows_cadence(self):
        p = proxy(FakeNet())
        self.assertEqual(p.ttl_for({"refresh": 10}), 30)
        self.assertEqual(p.ttl_for({"refresh": 120}), 60)
        self.assertEqual(p.ttl_for({"refresh": 3600}), 120)


if __name__ == "__main__":
    unittest.main()
