"""Request guards (CONTRACT.md section 3).

What they stop: other websites reaching Meridian through the browser (DNS rebinding, cross-site
writes and reads of the API), requests relayed by proxies, and request smuggling. They are not
authentication: any process on this PC can reach the loopback port. Meridian never mints a secret
to pretend otherwise. Order: Host, proxy headers, method, Origin, X-Meridian, Sec-Fetch-Site, framing.
"""

from __future__ import annotations

import os
import re
import socket

from .registry import HTTPError

_BINDS = {
    "127.0.0.1": ("127.0.0.1", socket.AF_INET),
    "localhost": ("127.0.0.1", socket.AF_INET),   # mapped, never looked up in DNS
    "::1": ("::1", socket.AF_INET6),
}


class BindRefused(SystemExit):
    def __init__(self, message: str):
        super().__init__(2)
        self.message = message

    def __str__(self) -> str:
        return self.message


def resolve_bind(host: str) -> tuple[str, int]:
    """Only loopback binds. Anything else (wildcards, LAN addresses, names) exits with code 2."""
    text = host.strip() if isinstance(host, str) else ""
    if text in _BINDS:
        return _BINDS[text]
    shown = repr(text[:60]) if text else "empty"
    raise BindRefused(f"MERIDIAN_HOST is {shown}. Meridian only listens on this computer: use 127.0.0.1, "
                      "localhost or ::1 (or leave it unset).")


def allowed_hosts(bind_ip: str, port: int) -> frozenset[str]:
    if bind_ip == "127.0.0.1":
        return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
    if bind_ip == "::1":
        return frozenset({f"[::1]:{port}"})
    return frozenset()


def allowed_origins(bind_ip: str, port: int) -> frozenset[str]:
    return frozenset(f"http://{h}" for h in allowed_hosts(bind_ip, port))


_SINGLE = ("host", "origin", "content-length", "content-type", "x-meridian", "sec-fetch-site")
_CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,9}")
_FETCH_SITES = frozenset({"same-origin", "none"})
_OWS = " \t"


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def path_part(target: str) -> str:
    return target.split("?", 1)[0].split("#", 1)[0]


def _json_media_type(value: str) -> bool:
    parts = [p.strip(_OWS) for p in value.split(";")]
    if parts[0].lower() != "application/json":
        return False
    params = parts[1:]
    if not params:
        return True
    if len(params) != 1:
        return False
    key, sep, val = params[0].partition("=")
    return bool(sep) and key.strip(_OWS).lower() == "charset" and val.strip(_OWS).strip('"').lower() == "utf-8"


def check_request(method: str, target: str, headers: list[tuple[str, str]], bind_ip: str,
                  port: int) -> HTTPError | None:
    names = [(k.lower(), v) for k, v in headers]

    def values(name: str) -> list[str]:
        return [v for k, v in names if k == name]

    target = target if isinstance(target, str) else ""
    api = is_api_path(path_part(target))

    hosts = values("host")
    if len(hosts) != 1 or hosts[0].strip(_OWS).lower() not in allowed_hosts(bind_ip, port):
        return HTTPError(403, "FORBIDDEN_HOST", "Meridian only answers requests made to its own address on this computer.")

    for key, _v in names:
        if key in ("forwarded", "x-real-ip", "via") or key.startswith("x-forwarded-"):
            return HTTPError(403, "PROXY_HEADER", "Meridian doesn't accept requests passed on by a proxy.")

    if method not in ("GET", "HEAD", "POST") or (method == "POST" and not api):
        return HTTPError(405, "METHOD_NOT_ALLOWED", "That kind of request isn't allowed.")

    if method == "POST":
        origins = values("origin")
        if len(origins) != 1 or origins[0].strip(_OWS).lower() not in allowed_origins(bind_ip, port):
            return HTTPError(403, "FORBIDDEN_ORIGIN", "This change didn't come from a Meridian page, so it was refused.")
        marks = values("x-meridian")
        if len(marks) != 1 or marks[0].strip(_OWS) != "1":
            return HTTPError(403, "MISSING_CSRF_HEADER", "This change didn't come from a Meridian page, so it was refused.")

    if method == "POST" or api:
        sites = values("sec-fetch-site")
        if len(sites) > 1 or (sites and sites[0].strip(_OWS).lower() not in _FETCH_SITES):
            return HTTPError(403, "CROSS_SITE", "Other websites can't read or change Meridian's data.")

    for name in _SINGLE:
        if len(values(name)) > 1:
            return HTTPError(400, "BAD_REQUEST", "That request isn't valid.")
    if values("expect") or values("transfer-encoding"):
        return HTTPError(400, "BAD_REQUEST", "That request isn't valid.")
    for _k, v in names:
        if "\r" in v or "\n" in v:
            return HTTPError(400, "BAD_REQUEST", "That request isn't valid.")
    if not target.startswith("/") or target.startswith("//"):
        return HTTPError(400, "BAD_REQUEST", "That request isn't valid.")
    if method == "POST":
        types = values("content-type")
        if not types or not _json_media_type(types[0]):
            return HTTPError(415, "UNSUPPORTED_MEDIA_TYPE", "Send the request body as JSON.")
        lengths = values("content-length")
        if not lengths:
            return HTTPError(411, "LENGTH_REQUIRED", "The request needs a Content-Length header.")
        if not _CONTENT_LENGTH_RE.fullmatch(lengths[0].strip(_OWS)):
            return HTTPError(400, "BAD_REQUEST", "That request isn't valid.")
    elif values("content-length") and values("content-length")[0].strip(_OWS) not in ("", "0"):
        return HTTPError(400, "BAD_REQUEST", "GET requests can't carry a body.")
    return None


def content_length(headers: list[tuple[str, str]]) -> int:
    for k, v in headers:
        if k.lower() == "content-length":
            return int(v.strip(_OWS) or 0)
    return 0


# Content Security Policy. Map tiles, styles and glyphs come from public tile servers (connect-src
# https:), camera stills are proxied through /api (img-src 'self') but some cards show public
# https images, MapLibre and hls.js use blob: workers, and live camera video can stream over https.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob: https:; connect-src 'self' https:; font-src 'self' data:; "
       "worker-src 'self' blob:; child-src 'self' blob:; media-src 'self' blob: https:; frame-src https:; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(self), payment=(), usb=()"),
)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".geojson": "application/geo+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".pbf": "application/x-protobuf",
}


def content_type_for(path: str) -> str | None:
    """Fixed type by extension. ``mimetypes`` is never consulted (Windows can map .js to text/plain)."""
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower())
