"""The camera image proxy: fetches the current still of a camera from the dataset, server-side.

Safety rules (CONTRACT.md rules 2 and 7):
- Only URLs that come from the camera dataset (or a source's own resolver) are fetched, and only
  when their host is on that source's allowlist. http(s) only, default ports unless the source
  allows another, no user info, and every resolved address must be public (no loopback, private,
  link-local or reserved ranges). Redirects are followed by hand (at most three) and each hop is
  checked the same way before it is requested.
- At most ``max_bytes`` (3 MB); the bytes must really be a JPEG, PNG, GIF or WebP image (SVG and
  anything else is refused) and the upstream content type must be image/* or a generic binary type.
- Latest frame only: a short in-memory cache per camera, never written to disk, bounded in entries
  and bytes. Nothing is kept once it expires or is evicted.
- Politeness: at most a few concurrent requests per host, a small spacing between requests,
  conditional revalidation (ETag / Last-Modified), cool-downs after 429s and repeated failures, and
  a short negative cache for cameras that answer 404 or with something that is not an image.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from meridian.net import NetError

from .common import Source

MAX_BYTES = 3_000_000
MAX_REDIRECTS = 3
REDIRECTS = (301, 302, 303, 307, 308)
DEFAULT_PORTS = {"http": 80, "https": 443}
GENERIC_TYPES = ("application/octet-stream", "binary/octet-stream", "application/binary", "")
# spaces, control characters, non-ASCII and backslashes: http.client can't send them (500s) and some
# servers read a backslash as "/", which would walk out of a "host/path-prefix" allowlist entry
_UNSAFE_URL_CHARS = re.compile(r"[^\x21-\x7e]|\\")


class UrlRefused(Exception):
    pass


class ProxyError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


# ---------------------------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------------------------
def host_allowed(url: str, src: Source) -> bool:
    """Does ``url`` match one of the source's patterns ("host", "*.domain" or "host/path-prefix")?"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path or "/"
    for pattern in src.hosts:
        pat_host, slash, prefix = pattern.partition("/")
        pat_host = pat_host.lower()                 # host names are case-insensitive; paths are not
        prefix = "/" + prefix if slash else ""
        if pat_host.startswith("*."):
            base = pat_host[2:]
            ok = host == base or host.endswith("." + base)
        else:
            ok = host == pat_host
        if ok and (not prefix or path.startswith(prefix)):
            return True
    return False


_DNS_CACHE: dict[str, tuple[float, list[str]]] = {}
_DNS_LOCK = threading.Lock()
DNS_TTL = 600.0


def resolve_host(host: str) -> list[str]:
    now = time.time()
    with _DNS_LOCK:
        hit = _DNS_CACHE.get(host)
        if hit and hit[0] > now:
            return hit[1]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as err:
        raise UrlRefused(f"{host} does not resolve ({err.__class__.__name__})") from None
    ips = sorted({info[4][0] for info in infos})
    with _DNS_LOCK:
        _DNS_CACHE[host] = (now + DNS_TTL, ips)
        if len(_DNS_CACHE) > 512:
            _DNS_CACHE.pop(next(iter(_DNS_CACHE)))
    return ips


def public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return bool(addr.is_global) and not (addr.is_multicast or addr.is_reserved or addr.is_loopback
                                         or addr.is_link_local or addr.is_private or addr.is_unspecified)


def has_dot_segments(path: str) -> bool:
    """True when the path has "." or ".." segments, also spelled %2e or with a backslash. Servers
    resolve those, so "/bucket/../other/x.jpg" would pass a "/bucket/" prefix check and then leave it."""
    text = urllib.parse.unquote(path or "").replace("\\", "/")
    return any(seg in (".", "..") for seg in text.split("/"))


def check_url(url: Any, src: Source, resolver: Callable[[str], list[str]] | None = None) -> str:
    """The URL if it is safe to fetch for this source; raises :class:`UrlRefused` otherwise."""
    if not isinstance(url, str) or not url or len(url) > 2000:
        raise UrlRefused("no image address")
    if _UNSAFE_URL_CHARS.search(url):
        raise UrlRefused("the address has spaces, control or non-ASCII characters or a backslash")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlRefused("malformed address") from None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise UrlRefused("only http and https addresses are fetched")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlRefused("addresses with user information are refused")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlRefused("address has no host")
    if port is not None and port != DEFAULT_PORTS[scheme] and port not in src.ports:
        raise UrlRefused("non-standard port")
    if has_dot_segments(parts.path):
        raise UrlRefused("the address has . or .. path segments")
    if not host_allowed(url, src):
        raise UrlRefused(f"{host} is not an image host of {src.name}")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    ips = [str(literal)] if literal is not None else (resolver or resolve_host)(host)
    if not ips or not all(public_ip(ip) for ip in ips):
        raise UrlRefused(f"{host} resolves to a non-public address")
    return url


# ---------------------------------------------------------------------------------------------
# Image checks
# ---------------------------------------------------------------------------------------------
def sniff(body: bytes) -> str | None:
    if body[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_type(declared: str, body: bytes) -> str:
    """The served content type, or raises ProxyError when this is not a raster image."""
    ctype = (declared or "").split(";", 1)[0].strip().lower()
    if "svg" in ctype:
        raise ProxyError(502, "NOT_IMAGE", "The camera answered with an SVG document, which is refused.")
    if not (ctype.startswith("image/") or ctype in GENERIC_TYPES):
        raise ProxyError(502, "NOT_IMAGE", "The camera answered with something that is not an image.")
    found = sniff(body)
    if found is None:
        raise ProxyError(502, "NOT_IMAGE", "The camera answered with something that is not an image.")
    return found


def _http_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


# ---------------------------------------------------------------------------------------------
# The proxy
# ---------------------------------------------------------------------------------------------
@dataclass
class Entry:
    body: bytes
    ctype: str
    fetched_at: float
    expires: float
    etag: str | None = None
    last_modified: str | None = None
    upstream_time: float | None = None
    stale: bool = False


@dataclass
class HostState:
    failures: int = 0
    until: float = 0.0
    last_error: str | None = None
    rate_limited: bool = False


class ImageProxy:
    def __init__(self, net, log=None, *, max_bytes: int = MAX_BYTES, max_entries: int = 500,
                 max_total: int = 48_000_000, per_host: int = 4, global_limit: int = 12,
                 resolver: Callable[[str], list[str]] | None = None, min_ttl: float = 30.0,
                 max_ttl: float = 120.0, stale_for: float = 900.0, spacing: float = 0.05):
        self.net = net
        self.log = log
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.max_total = max_total
        self.per_host = per_host
        self.resolver = resolver or (lambda host: resolve_host(host))   # looked up at call time
        self.min_ttl = min_ttl
        self.max_ttl = max_ttl
        self.stale_for = stale_for
        self.spacing = spacing
        self._cache: OrderedDict[str, Entry] = OrderedDict()
        self._total = 0
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._host_sems: dict[str, threading.BoundedSemaphore] = {}
        self._hosts: dict[str, HostState] = {}
        self._negative: dict[str, tuple[float, ProxyError]] = {}
        self._global = threading.BoundedSemaphore(global_limit)
        self._meta: OrderedDict[str, dict] = OrderedDict()   # small per-camera fetch facts (no images)
        self.stats = {"hits": 0, "fetches": 0, "not_modified": 0, "errors": 0, "refused": 0, "bytes": 0}

    # -- cache helpers ---------------------------------------------------------------------------
    def ttl_for(self, cam: dict) -> float:
        refresh = float(cam.get("refresh") or 60)
        return max(self.min_ttl, min(self.max_ttl, refresh / 2.0))

    def _get(self, key: str) -> Entry | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                self._cache.move_to_end(key)
            return entry

    def _put(self, key: str, entry: Entry) -> None:
        with self._lock:
            old = self._cache.pop(key, None)
            if old is not None:
                self._total -= len(old.body)
            self._cache[key] = entry
            self._total += len(entry.body)
            while self._cache and (len(self._cache) > self.max_entries or self._total > self.max_total):
                _, evicted = self._cache.popitem(last=False)
                self._total -= len(evicted.body)

    def _note(self, key: str, **facts) -> None:
        with self._lock:
            row = self._meta.pop(key, {})
            row.update(facts)
            self._meta[key] = row
            while len(self._meta) > 4000:
                self._meta.popitem(last=False)

    def meta(self, key: str) -> dict | None:
        with self._lock:
            row = self._meta.get(key)
            return dict(row) if row else None

    def cache_info(self) -> dict:
        with self._lock:
            return {**self.stats, "entries": len(self._cache), "cached_bytes": self._total,
                    "fetched_bytes": self.stats["bytes"]}

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._total = 0

    def _host_sem(self, host: str) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._host_sems.get(host)
            if sem is None:
                sem = self._host_sems[host] = threading.BoundedSemaphore(self.per_host)
            return sem

    def _host_state(self, host: str) -> HostState:
        with self._lock:
            st = self._hosts.get(host)
            if st is None:
                st = self._hosts[host] = HostState()
            return st

    # -- the fetch ---------------------------------------------------------------------------------
    def get(self, cam: dict, src: Source, resolve: Callable[[dict, Any], str | None] | None = None) -> Entry:
        """The camera's latest image (cached briefly). Raises :class:`ProxyError`."""
        key = cam["id"]
        now = time.time()
        entry = self._get(key)
        if entry is not None and entry.expires > now:
            self.stats["hits"] += 1
            return entry
        neg = self._negative.get(key)
        if neg and neg[0] > now:
            raise neg[1]
        # one upstream request per camera at a time; the others wait for it
        with self._lock:
            event = self._inflight.get(key)
            leader = event is None
            if leader:
                event = self._inflight[key] = threading.Event()
        if not leader:
            event.wait(max(15.0, image_timeout(src) + 5.0))
            entry = self._get(key)
            if entry is not None and entry.fetched_at >= now - 1:
                return entry
            neg = self._negative.get(key)
            if neg and neg[0] > time.time():
                raise neg[1]
            if entry is not None and entry.fetched_at > time.time() - self.stale_for:
                return entry
            raise ProxyError(504, "UPSTREAM_TIMEOUT", "The camera did not answer in time.")
        try:
            return self._fetch(cam, src, resolve, entry)
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()

    def _fail(self, key: str, err: ProxyError, entry: Entry | None, *, negative_s: float = 0.0) -> Entry:
        self.stats["errors"] += 1
        self._note(key, ok=False, error=err.message, failed_at=time.time())
        if negative_s:
            self._negative[key] = (time.time() + negative_s, err)
            if len(self._negative) > 5000:
                self._negative = {k: v for k, v in self._negative.items() if v[0] > time.time()}
        if entry is not None and entry.fetched_at > time.time() - self.stale_for:
            return Entry(entry.body, entry.ctype, entry.fetched_at, time.time() + 15, entry.etag,
                         entry.last_modified, entry.upstream_time, stale=True)
        raise err

    def _fetch(self, cam: dict, src: Source, resolve, entry: Entry | None) -> Entry:
        key = cam["id"]
        try:
            url = cam.get("image")
            if resolve is not None:
                try:
                    url = resolve(cam, self.net) or url
                except NetError as err:
                    return self._fail(key, ProxyError(502, "LOOKUP_FAILED",
                                                      f"{src.name} did not say where the latest image is ({err})."), entry)
                except (http.client.HTTPException, ValueError, TypeError, AttributeError, KeyError, IndexError):
                    # an answer of an unexpected shape, or broken HTTP framing: fail soft, never a 500
                    return self._fail(key, ProxyError(502, "LOOKUP_FAILED",
                                                      f"{src.name} gave an unexpected answer about the latest image."),
                                      entry)
            if not url:
                return self._fail(key, ProxyError(404, "NO_IMAGE", "This camera has no still image."), entry,
                                  negative_s=120)
            url = check_url(url, src, self.resolver)
        except UrlRefused as err:
            self.stats["refused"] += 1
            return self._fail(key, ProxyError(403, "URL_REFUSED", f"Refused to fetch this image: {err}."), entry,
                              negative_s=300)
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        state = self._host_state(host)
        if state.until > time.time():
            wait = max(1, int(state.until - time.time()))
            why = "asked us to slow down" if state.rate_limited else "is not answering"
            return self._fail(key, ProxyError(503, "HOST_COOLING_DOWN",
                                              f"{src.name} {why}; trying again in {wait} s.", wait), entry)
        if not self._global.acquire(timeout=8.0):
            return self._fail(key, ProxyError(503, "BUSY", "Too many camera images are loading; try again shortly.", 5),
                              entry)
        try:
            sem = self._host_sem(host)
            if not sem.acquire(timeout=12.0):
                return self._fail(key, ProxyError(503, "BUSY", "Too many images from this camera's server; try again.", 5),
                                  entry)
            try:
                return self._request(cam, src, url, host, state, entry)
            finally:
                sem.release()
        finally:
            self._global.release()

    def _request(self, cam: dict, src: Source, url: str, host: str, state: HostState, entry: Entry | None) -> Entry:
        key = cam["id"]
        headers = {"Accept": "image/webp,image/jpeg,image/png,image/*;q=0.8"}
        if entry is not None and entry.etag:
            headers["If-None-Match"] = entry.etag
        if entry is not None and entry.last_modified:
            headers["If-Modified-Since"] = entry.last_modified
        started = time.time()
        current = url
        try:
            # Redirects are followed here, one hop at a time, and every hop is checked *before* it is
            # requested (host allowlist, public address), so a redirect can never reach elsewhere.
            for _hop in range(MAX_REDIRECTS + 1):
                self.stats["fetches"] += 1
                resp = self.net.request(current, timeout=image_timeout(src), retries=0, max_bytes=self.max_bytes,
                                        accept=(200, 304, 429) + REDIRECTS, headers=headers,
                                        min_interval=self.spacing, allow_redirects=False)
                if resp.status not in REDIRECTS:
                    break
                location = resp.headers.get("location") or ""
                try:
                    current = check_url(urllib.parse.urljoin(current, location.strip()), src, self.resolver)
                except UrlRefused as err:
                    self.stats["refused"] += 1
                    return self._fail(key, ProxyError(502, "REDIRECT_REFUSED",
                                                      f"The camera redirected somewhere it should not: {err}."),
                                      entry, negative_s=300)
            else:
                return self._fail(key, ProxyError(502, "TOO_MANY_REDIRECTS", "The camera redirected too many times."),
                                  entry, negative_s=300)
        except NetError as err:
            status = err.status
            message = str(err)
            if "larger than" in message:
                return self._fail(key, ProxyError(502, "TOO_LARGE", "The camera image is larger than 3 MB."), entry,
                                  negative_s=600)
            if status in (401, 403, 404, 410):
                return self._fail(key, ProxyError(502, "UPSTREAM_MISSING",
                                                  f"{src.name} has no current image for this camera (HTTP {status})."),
                                  entry, negative_s=180)
            state.failures += 1
            state.last_error = message[:200]
            state.rate_limited = False
            if state.failures >= 3:
                state.until = time.time() + min(300.0, 30.0 * 2 ** (state.failures - 3))
            code = "UPSTREAM_TIMEOUT" if status is None and "timed out" in message.lower() else "UPSTREAM_ERROR"
            text = "did not answer in time" if code == "UPSTREAM_TIMEOUT" else (
                f"answered with HTTP {status}" if status else "could not be reached")
            return self._fail(key, ProxyError(504 if code == "UPSTREAM_TIMEOUT" else 502, code,
                                              f"{src.name} {text}."), entry)
        except (http.client.HTTPException, ValueError) as err:
            # broken status line, headers or chunked body (http.client raises these past m.net): fail soft
            state.failures += 1
            state.last_error = type(err).__name__
            state.rate_limited = False
            if state.failures >= 3:
                state.until = time.time() + min(300.0, 30.0 * 2 ** (state.failures - 3))
            return self._fail(key, ProxyError(502, "UPSTREAM_ERROR", f"{src.name} sent a broken answer."), entry)
        if resp.status == 429:
            retry = retry_after_seconds(resp.headers.get("retry-after"))
            state.until = time.time() + retry
            state.last_error = "HTTP 429"
            state.rate_limited = True
            return self._fail(key, ProxyError(503, "HOST_COOLING_DOWN",
                                              f"{src.name} asked us to slow down; trying again in {int(retry)} s.",
                                              retry), entry)
        # belt and braces: the address the response came from must be as acceptable as the request
        if resp.url and resp.url != current:
            try:
                check_url(resp.url, src, self.resolver)
            except UrlRefused as err:
                self.stats["refused"] += 1
                return self._fail(key, ProxyError(502, "REDIRECT_REFUSED", f"The camera redirected elsewhere: {err}."),
                                  entry, negative_s=300)
        state.failures = 0
        now = time.time()
        ttl = self.ttl_for(cam)
        if resp.status == 304 and entry is not None:
            self.stats["not_modified"] += 1
            fresh = Entry(entry.body, entry.ctype, now, now + ttl, entry.etag, entry.last_modified, entry.upstream_time)
            self._put(key, fresh)
            self._note(key, ok=True, error=None, fetched_at=now, upstream_time=entry.upstream_time,
                       elapsed=round(now - started, 2))
            return fresh
        try:
            ctype = image_type(resp.headers.get("content-type", ""), resp.body)
        except ProxyError as err:
            return self._fail(key, err, entry, negative_s=180)
        upstream = (_http_time(resp.headers.get("x-amz-meta-last-modified"))
                    or _http_time(resp.headers.get("last-modified")))
        fresh = Entry(resp.body, ctype, now, now + ttl, resp.headers.get("etag"), resp.headers.get("last-modified"),
                      upstream)
        self._put(key, fresh)
        self.stats["bytes"] += len(resp.body)
        self._negative.pop(key, None)
        self._note(key, ok=True, error=None, fetched_at=now, upstream_time=upstream, bytes=len(resp.body),
                   elapsed=round(now - started, 2))
        return fresh


def image_timeout(src: Source) -> float:
    """The source's image timeout (``Source.image_timeout``), kept between 3 and 30 seconds."""
    try:
        value = float(getattr(src, "image_timeout", 10.0) or 10.0)
    except (TypeError, ValueError):
        value = 10.0
    return max(3.0, min(30.0, value))


def retry_after_seconds(value: str | None, default: float = 60.0) -> float:
    """Seconds from a Retry-After header (delta or HTTP date), kept between 5 s and 15 min."""
    if value:
        try:
            return max(5.0, min(900.0, float(value)))
        except ValueError:
            when = _http_time(value)
            if when is not None:
                return max(5.0, min(900.0, when - time.time()))
    return default
