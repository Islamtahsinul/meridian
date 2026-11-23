"""Loopback HTTP server: guards, API dispatch, static files, gzip and ETags (CONTRACT.md section 3)."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import socket
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import guards
from .config import WEB_DIR
from .registry import App, HTTPError, Request, Response, json_bytes

log = logging.getLogger("meridian.http")

_STATIC_CACHE: dict[str, tuple[float, int, bytes, bytes | None, str]] = {}
_STATIC_LOCK = threading.Lock()


def _parse_json_body(raw: bytes):
    def refuse(_name):
        raise ValueError("NaN and Infinity aren't JSON")

    def unique(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise ValueError("repeated key")
            obj[k] = v
        return obj

    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=refuse, object_pairs_hook=unique)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise HTTPError(400, "BAD_JSON", "The request body isn't valid JSON.") from None
    if not isinstance(value, dict):
        raise HTTPError(400, "BAD_JSON", "The request body must be a JSON object.")
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "Meridian"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 60
    app: App = None  # set by make_server

    # -- plumbing --------------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet: requests go to the debug log only
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _send(self, status: int, body: bytes, content_type: str, *, cache: str, etag: str | None = None,
              gz: bytes | None = None, compressible: bool = True, extra: dict | None = None,
              head: bool = False) -> None:
        path = guards.path_part(self.path)
        inm = self.headers.get("If-None-Match")
        if etag and inm and status == 200 and etag in [t.strip() for t in inm.split(",")]:
            self.send_response(304)
            for k, v in guards.SECURITY_HEADERS:
                self.send_header(k, v)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = body
        encoded = False
        if compressible and len(body) > 1024 and self._accepts_gzip():
            payload = gz if gz else gzip.compress(body, compresslevel=5)
            encoded = True
        self.send_response(status)
        for k, v in guards.SECURITY_HEADERS:
            self.send_header(k, v)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        if compressible:
            self.send_header("Vary", "Accept-Encoding")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not head:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _error(self, err: HTTPError, api: bool, head: bool = False) -> None:
        if api:
            body = json_bytes({"error": {"code": err.code, "message": err.message}})
            ctype = "application/json; charset=utf-8"
        else:
            body = (err.message + "\n").encode("utf-8")
            ctype = "text/plain; charset=utf-8"
        extra = {"Connection": "close"} if err.status in (400, 411, 413) else None
        if err.status in (400, 411, 413):
            self.close_connection = True
        self._send(err.status, body, ctype, cache="no-store", compressible=False, extra=extra, head=head)

    # -- main entry ------------------------------------------------------------------------------
    def _handle(self, method: str) -> None:
        app = self.app
        headers = list(self.headers.items())
        target = self.path
        bind_ip, port = app.bind
        api = guards.is_api_path(guards.path_part(target))
        head = method == "HEAD"
        problem = guards.check_request(method, target, headers, bind_ip, port)
        if problem is not None:
            self._error(problem, api, head)
            return
        try:
            if api:
                self._api(method, target, headers, head)
            else:
                self._static(target, head)
        except HTTPError as err:
            self._error(err, api, head)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            log.error("unhandled error for %s %s\n%s", method, app.config.redact(guards.path_part(target)),
                      app.config.redact(traceback.format_exc()))
            self._error(HTTPError(500, "INTERNAL", "Something went wrong on the Meridian server. "
                                                    "The details are in data/logs/meridian.log."), api, head)

    def _api(self, method: str, target: str, headers, head: bool) -> None:
        app = self.app
        split = urllib.parse.urlsplit(target)
        path = urllib.parse.unquote(split.path)
        try:
            query_all = urllib.parse.parse_qs(split.query, keep_blank_values=False, max_num_fields=200)
        except ValueError:
            raise HTTPError(400, "BAD_REQUEST", "Too many query parameters.") from None
        route, params, other = app.match(method, path)
        if route is None:
            if other:
                raise HTTPError(405, "METHOD_NOT_ALLOWED", "That kind of request isn't allowed here.")
            raise HTTPError(404, "NOT_FOUND", "There is no such API endpoint.")
        body = None
        if method == "POST":
            length = guards.content_length(headers)
            if length > route.body_max:
                raise HTTPError(413, "BODY_TOO_LARGE", "That request is too large.")
            raw = self.rfile.read(length) if length else b""
            if len(raw) != length:
                raise HTTPError(400, "BAD_REQUEST", "The request body was cut short.")
            body = _parse_json_body(raw) if raw else {}
        hdrs = {k.lower(): v for k, v in headers}
        req = Request(method if method != "HEAD" else "GET", path, params, query_all, hdrs, body)
        if route.touch:
            app.touch(route.module)
        result = route.handler(req)
        if isinstance(result, Response):
            self._send(result.status, result.body, result.content_type, cache=result.cache, etag=result.etag,
                       gz=result.gzipped, compressible=result.compressible, extra=result.headers, head=head)
        else:
            body_bytes = json_bytes(result if result is not None else {"ok": True})
            self._send(200, body_bytes, "application/json; charset=utf-8", cache="no-store", head=head)

    def _static(self, target: str, head: bool) -> None:
        split = urllib.parse.urlsplit(target)
        rel = urllib.parse.unquote(split.path)
        if rel in ("", "/"):
            rel = "/index.html"
        if "\x00" in rel or "\\" in rel:
            raise HTTPError(404, "NOT_FOUND", "Not found.")
        root = WEB_DIR.resolve()
        candidate = (root / rel.lstrip("/")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise HTTPError(404, "NOT_FOUND", "Not found.") from None
        if any(part.startswith(".") for part in candidate.relative_to(root).parts):
            raise HTTPError(404, "NOT_FOUND", "Not found.")
        if not candidate.is_file():
            raise HTTPError(404, "NOT_FOUND", "Not found.")
        ctype = guards.content_type_for(candidate.name)
        if ctype is None:
            raise HTTPError(403, "FORBIDDEN_TYPE", "That kind of file isn't served.")
        st = candidate.stat()
        key = str(candidate)
        with _STATIC_LOCK:
            item = _STATIC_CACHE.get(key)
        if item is None or item[0] != st.st_mtime or item[1] != st.st_size:
            data = candidate.read_bytes()
            compressible = any(t in ctype for t in ("text", "javascript", "json", "svg"))
            gz = gzip.compress(data, compresslevel=6) if compressible and len(data) > 1024 else None
            etag = '"' + hashlib.sha1(data).hexdigest()[:20] + '"'
            item = (st.st_mtime, st.st_size, data, gz, etag)
            with _STATIC_LOCK:
                _STATIC_CACHE[key] = item
        _, _, data, gz, etag = item
        compressible = gz is not None
        self._send(200, data, ctype, cache="no-cache", etag=etag, gz=gz, compressible=compressible, head=head)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64

    def server_bind(self):
        # On Windows, refuse to share the port with another process (no SO_REUSEADDR hijacking).
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):  # keep tracebacks out of the console
        log.debug("connection error from %s:\n%s", client_address, traceback.format_exc())


class _Server6(_Server):
    address_family = socket.AF_INET6


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    bind_ip, family = guards.resolve_bind(host)
    handler = type("MeridianHandler", (Handler,), {"app": app})
    cls = _Server6 if family == socket.AF_INET6 else _Server
    server = cls((bind_ip, port), handler)
    actual_port = server.server_address[1]
    app.bind = (bind_ip, actual_port)
    return server
