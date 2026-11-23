"""Outbound HTTP for modules (CONTRACT.md section 6): polite, cached, retried and redacted.

Rules every caller gets for free:
- one generic User-Agent (no personal or project details are ever sent);
- per-host minimum spacing between requests (``min_interval`` or :meth:`Net.set_host_interval`);
- retries with backoff on timeouts, 429 and 5xx (``Retry-After`` honoured up to 30 s);
- an in-memory TTL cache for GET requests (``ttl`` seconds);
- secrets are redacted from every error message and log line.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import logging
import os
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

USER_AGENT = "open-data-map-client/1.0 (personal, non-commercial)"
_RETRY_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 524}


class NetError(Exception):
    """A request that failed after its retries. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None,
                 body: bytes | None = None):
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body


@dataclass
class NetResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    from_cache: bool = False
    elapsed: float = 0.0
    extra: dict = field(default_factory=dict)

    def text(self, encoding: str | None = None) -> str:
        enc = encoding or _charset(self.headers.get("content-type", "")) or "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        text = self.body.decode("utf-8-sig", errors="replace")
        return json.loads(text)


def _charset(content_type: str) -> str | None:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip('"').strip()
    return None


class _TTLCache:
    def __init__(self, max_entries: int = 384, max_entry_bytes: int = 32_000_000,
                 max_total_bytes: int = 400_000_000):
        self._data: OrderedDict[str, tuple[float, NetResponse]] = OrderedDict()
        self._lock = threading.Lock()
        self._total = 0
        self.max_entries = max_entries
        self.max_entry_bytes = max_entry_bytes
        self.max_total_bytes = max_total_bytes

    def get(self, key: str) -> NetResponse | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, resp = item
            if expires < time.time():
                self._data.pop(key, None)
                self._total -= len(resp.body)
                return None
            self._data.move_to_end(key)
            return resp

    def put(self, key: str, resp: NetResponse, ttl: float) -> None:
        size = len(resp.body)
        if size > self.max_entry_bytes:
            return
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._total -= len(old[1].body)
            self._data[key] = (time.time() + ttl, resp)
            self._total += size
            while self._data and (len(self._data) > self.max_entries or self._total > self.max_total_bytes):
                _, (_, evicted) = self._data.popitem(last=False)
                self._total -= len(evicted.body)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._total = 0


_CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_CREDENTIAL_PARTS = ("key", "token", "secret", "auth", "session", "password")


def _is_credential_header(name: str) -> bool:
    low = name.lower()
    return low in _CREDENTIAL_HEADERS or any(part in low for part in _CREDENTIAL_PARTS)


class _NoRedirectToFile(urllib.request.HTTPRedirectHandler):
    """Follow normal http(s) redirects only (never file:, ftp: or other schemes), never from https
    down to plain http, and drop credential headers (Authorization, Cookie, X-Api-Key, ...) when the
    redirect leaves the original host, so a key sent to one API never reaches another host
    (CONTRACT rule 4)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_parts = urllib.parse.urlsplit(newurl)
        old_parts = urllib.parse.urlsplit(req.full_url)
        scheme = new_parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-http scheme refused", headers, fp)
        if old_parts.scheme.lower() == "https" and scheme != "https":
            raise urllib.error.HTTPError(newurl, code, "redirect from https to http refused", headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (old_parts.hostname or "").lower() != (new_parts.hostname or "").lower():
            for name in [k for k in new.headers if _is_credential_header(k)]:
                del new.headers[name]
            for name in [k for k in new.unredirected_hdrs if _is_credential_header(k)]:
                del new.unredirected_hdrs[name]
        return new


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Used with allow_redirects=False: a 3xx comes back as an HTTP error status instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Net:
    def __init__(self, config=None, log: logging.Logger | None = None):
        self.config = config
        self.log = log or logging.getLogger("meridian.net")
        self.cache = _TTLCache()
        self._lock = threading.Lock()
        self._host_next: dict[str, float] = {}
        self._host_interval: dict[str, float] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), _NoRedirectToFile())
        self._opener_noredirect = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), _RefuseRedirect())
        self.stats = {"requests": 0, "errors": 0, "cache_hits": 0, "bytes": 0}

    # -- helpers -------------------------------------------------------------------------------
    def redact(self, text: str) -> str:
        return self.config.redact(text) if self.config is not None else text

    def set_host_interval(self, host: str, seconds: float) -> None:
        """Minimum spacing between requests to ``host`` (e.g. 1.0 for Nominatim)."""
        with self._lock:
            self._host_interval[host.lower()] = float(seconds)

    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = self._host_locks[host] = threading.Lock()
            return lock

    def _wait_turn(self, host: str, min_interval: float | None) -> None:
        interval = min_interval
        if interval is None:
            with self._lock:
                interval = self._host_interval.get(host)
        if not interval:
            return
        with self._host_lock(host):
            with self._lock:
                next_ok = self._host_next.get(host, 0.0)
            delay = next_ok - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, 60.0))
            with self._lock:
                self._host_next[host] = time.monotonic() + interval

    @staticmethod
    def build_url(url: str, params: dict | None) -> str:
        if not params:
            return url
        clean = {k: v for k, v in params.items() if v is not None}
        query = urllib.parse.urlencode(clean, doseq=True)
        joiner = "&" if urllib.parse.urlsplit(url).query else "?"
        return f"{url}{joiner}{query}" if query else url

    # -- the request ---------------------------------------------------------------------------
    def request(self, url: str, *, method: str = "GET", params: dict | None = None,
                headers: dict | None = None, data: bytes | None = None, json_body: Any = None,
                timeout: float = 20.0, retries: int = 1, ttl: float = 0.0,
                min_interval: float | None = None, max_bytes: int = 256 * 1024 * 1024,
                accept: tuple[int, ...] = (200,), cache_key: str | None = None,
                allow_redirects: bool = True) -> NetResponse:
        full = self.build_url(url, params)
        parts = urllib.parse.urlsplit(full)
        if parts.scheme not in ("http", "https"):
            raise NetError(f"Only http(s) URLs can be fetched, not {parts.scheme!r}.", url=self.redact(full))
        host = (parts.hostname or "").lower()
        body = data
        send_headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            send_headers["Content-Type"] = "application/json"
        if headers:
            send_headers.update(headers)

        key = None
        if ttl > 0 and (method == "GET" or cache_key):
            digest = hashlib.sha1(body).hexdigest() if body else ""
            key = cache_key or f"{method} {full} {digest} {sorted(send_headers.items())}"
            cached = self.cache.get(key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return NetResponse(cached.status, cached.headers, cached.body, cached.url, True, 0.0)

        attempts = max(1, retries + 1)
        last_error: NetError | None = None
        for attempt in range(attempts):
            self._wait_turn(host, min_interval)
            started = time.monotonic()
            status = 0
            resp_headers: dict[str, str] = {}
            raw = b""
            try:
                req = urllib.request.Request(full, data=body, headers=send_headers, method=method)
                self.stats["requests"] += 1
                opener = self._opener if allow_redirects else self._opener_noredirect
                with opener.open(req, timeout=timeout) as resp:
                    status = resp.status
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    raw = resp.read(max_bytes + 1)
                    final_url = resp.geturl()
            except urllib.error.HTTPError as err:
                status = err.code
                resp_headers = {k.lower(): v for k, v in (err.headers or {}).items()}
                try:
                    raw = err.read(min(max_bytes, 2_000_000))
                except Exception:
                    raw = b""
                final_url = full
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError,
                    OSError, http.client.HTTPException, ValueError) as err:
                reason = getattr(err, "reason", err)
                last_error = NetError(f"{host}: {self.redact(str(reason))}", url=self.redact(full))
                self.stats["errors"] += 1
                if attempt + 1 < attempts:
                    time.sleep(_backoff(attempt))
                    continue
                raise last_error from None

            if len(raw) > max_bytes:
                raise NetError(f"{host}: response larger than {max_bytes} bytes", status=status,
                               url=self.redact(full))
            try:
                raw = _decode(raw, resp_headers.get("content-encoding", ""), max_bytes)
            except (OSError, zlib.error, EOFError) as err:
                raise NetError(f"{host}: could not decode response ({err})", status=status,
                               url=self.redact(full)) from None
            elapsed = time.monotonic() - started
            self.stats["bytes"] += len(raw)
            result = NetResponse(status, resp_headers, raw, final_url, False, elapsed)
            if status in accept:
                if key is not None:
                    self.cache.put(key, result, ttl)
                self.log.debug("%s %s -> %s (%d bytes, %.2fs)", method, self.redact(full), status,
                               len(raw), elapsed)
                return result
            self.stats["errors"] += 1
            last_error = NetError(f"{host}: HTTP {status}", status=status, url=self.redact(full), body=raw[:2000])
            if status in _RETRY_STATUS and attempt + 1 < attempts:
                time.sleep(_retry_after(resp_headers.get("retry-after"), attempt))
                continue
            raise last_error
        raise last_error or NetError(f"{host}: request failed", url=self.redact(full))

    def get_json(self, url: str, **kw) -> Any:
        resp = self.request(url, **kw)
        try:
            return resp.json()
        except ValueError as err:
            raise NetError(f"{urllib.parse.urlsplit(url).hostname}: response is not JSON ({err})",
                           status=resp.status, url=self.redact(url)) from None

    def get_text(self, url: str, encoding: str | None = None, **kw) -> str:
        return self.request(url, **kw).text(encoding)

    def get_bytes(self, url: str, **kw) -> bytes:
        return self.request(url, **kw).body

    def post_json(self, url: str, payload: Any, **kw) -> Any:
        resp = self.request(url, method="POST", json_body=payload, **kw)
        return resp.json()

    # -- downloads -----------------------------------------------------------------------------
    def download(self, url: str, dest: Path, *, max_age_hours: float = 24.0, force: bool = False,
                 timeout: float = 300.0, headers: dict | None = None, min_bytes: int = 1,
                 min_interval: float | None = None, max_bytes: int = 1_500_000_000) -> Path:
        """Fetch ``url`` to ``dest`` (atomic). Skips the fetch while ``dest`` is younger than
        ``max_age_hours``; uses ETag/Last-Modified when it has them. If the fetch fails and an
        older copy exists, the older copy is kept and returned (with a warning)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        meta_path = dest.with_name(dest.name + ".meta.json")
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        if dest.exists() and not force:
            age_h = (time.time() - dest.stat().st_mtime) / 3600.0
            if age_h < max_age_hours:
                return dest
        send = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if headers:
            send.update(headers)
        if dest.exists() and not force:
            if meta.get("etag"):
                send["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                send["If-Modified-Since"] = meta["last_modified"]
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        self._wait_turn(host, min_interval)
        tmp = dest.with_name(dest.name + f".part{os.getpid()}")
        try:
            req = urllib.request.Request(url, headers=send)
            self.stats["requests"] += 1
            try:
                with self._opener.open(req, timeout=timeout) as resp, open(tmp, "wb") as out:
                    total = 0
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        total += len(chunk)
                        if total > max_bytes:
                            raise NetError(f"{host}: download larger than {max_bytes} bytes", url=self.redact(url))
                    etag = resp.headers.get("ETag")
                    last_modified = resp.headers.get("Last-Modified")
            except urllib.error.HTTPError as err:
                if err.code == 304 and dest.exists():
                    os.utime(dest, None)
                    return dest
                raise NetError(f"{host}: HTTP {err.code}", status=err.code, url=self.redact(url)) from None
            if total < min_bytes:
                raise NetError(f"{host}: download too small ({total} bytes)", url=self.redact(url))
            os.replace(tmp, dest)
            meta = {"url": self.redact(url), "etag": etag, "last_modified": last_modified,
                    "fetched_at": time.time(), "bytes": total}
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            self.stats["bytes"] += total
            self.log.info("downloaded %s (%d bytes)", self.redact(url), total)
            return dest
        except (NetError, urllib.error.URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException,
                ValueError) as err:
            self.stats["errors"] += 1
            message = err if isinstance(err, NetError) else NetError(
                f"{host}: {self.redact(str(getattr(err, 'reason', err)))}", url=self.redact(url))
            if dest.exists():
                self.log.warning("download failed, keeping the older copy of %s: %s", dest.name, message)
                return dest
            raise message from None
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


class DecodedTooLarge(OSError):
    """A compressed response inflated past the caller's limit (a "gzip bomb")."""


def _inflate(raw: bytes, wbits: int, max_bytes: int) -> bytes:
    d = zlib.decompressobj(wbits)
    out = d.decompress(raw, max_bytes + 1)
    if len(out) > max_bytes or d.unconsumed_tail:
        raise DecodedTooLarge(f"response inflates past {max_bytes} bytes")
    out += d.flush()
    if len(out) > max_bytes:
        raise DecodedTooLarge(f"response inflates past {max_bytes} bytes")
    return out


def _decode(raw: bytes, encoding: str, max_bytes: int = 256 * 1024 * 1024) -> bytes:
    """Undo Content-Encoding without ever producing more than ``max_bytes``."""
    enc = (encoding or "").lower().strip()
    if not raw:
        return raw
    if enc in ("gzip", "x-gzip"):
        return _inflate(raw, 16 + zlib.MAX_WBITS, max_bytes)
    if enc == "deflate":
        try:
            return _inflate(raw, zlib.MAX_WBITS, max_bytes)
        except zlib.error:
            return _inflate(raw, -zlib.MAX_WBITS, max_bytes)
    return raw


def _backoff(attempt: int) -> float:
    return min(20.0, (2 ** attempt) * 1.5) + random.uniform(0, 0.5)


def _retry_after(value: str | None, attempt: int) -> float:
    if value:
        try:
            return max(0.5, min(30.0, float(value)))
        except ValueError:
            try:
                when = parsedate_to_datetime(value).timestamp()
                return max(0.5, min(30.0, when - time.time()))
            except (TypeError, ValueError):
                pass
    return _backoff(attempt)
