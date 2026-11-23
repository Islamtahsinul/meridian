"""A small WebSocket client (RFC 6455) on the standard library, with permessage-deflate (RFC 7692).

Client frames are masked; server frames must not be. Fragmented messages are reassembled, control
frames (ping, pong, close) may arrive between fragments, pings are answered with pongs and the
connection sends its own pings when it has been quiet. Compressed messages are inflated with a
size cap so a hostile server can't exhaust memory. The client never fragments what it sends.

    ws = WebSocketClient("wss://stream.example/v0/stream")
    ws.connect()
    ws.send_text('{"hello": 1}')
    while True:
        msg = ws.recv(stop_event)           # (opcode, bytes) or None when stop_event is set
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import time
import urllib.parse
import zlib
from dataclasses import dataclass

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
DATA_OPS = (OP_TEXT, OP_BINARY)
CONTROL_OPS = (OP_CLOSE, OP_PING, OP_PONG)
DEFLATE_TAIL = b"\x00\x00\xff\xff"


class WebSocketError(Exception):
    """Protocol or transport problem. The connection should be dropped and retried later."""


class WebSocketClosed(WebSocketError):
    def __init__(self, code: int | None, reason: str = ""):
        super().__init__(f"connection closed ({code}{': ' + reason if reason else ''})")
        self.code = code
        self.reason = reason


# ---------------------------------------------------------------------------------------------
# Framing (pure functions, unit tested)
# ---------------------------------------------------------------------------------------------
def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def apply_mask(data: bytes, key: bytes) -> bytes:
    """XOR ``data`` with the repeating 4-byte ``key`` (masking and unmasking are the same)."""
    n = len(data)
    if not n:
        return b""
    stream = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes = b"", *, fin: bool = True, rsv1: bool = False,
                 mask: bool = True, mask_key: bytes | None = None) -> bytes:
    if opcode in CONTROL_OPS and (len(payload) > 125 or not fin):
        raise ValueError("control frames carry at most 125 bytes and are never fragmented")
    head = bytearray()
    head.append((0x80 if fin else 0) | (0x40 if rsv1 else 0) | (opcode & 0x0F))
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack("!Q", n)
    if mask:
        key = mask_key if mask_key is not None else os.urandom(4)
        if len(key) != 4:
            raise ValueError("mask key must be 4 bytes")
        head += key
        return bytes(head) + apply_mask(payload, key)
    return bytes(head) + payload


def close_payload(code: int = 1000, reason: str = "") -> bytes:
    return struct.pack("!H", code) + reason.encode("utf-8")[:120]


def parse_close(payload: bytes) -> tuple[int | None, str]:
    if len(payload) < 2:
        return None, ""
    code = struct.unpack("!H", payload[:2])[0]
    return code, payload[2:].decode("utf-8", errors="replace")


@dataclass
class Frame:
    fin: bool
    rsv1: bool
    opcode: int
    payload: bytes


class FrameParser:
    """Incremental frame parser: ``feed(bytes)`` returns the frames completed so far."""

    def __init__(self, *, max_frame: int = 8 * 1024 * 1024, allow_rsv1: bool = False, expect_masked: bool = False):
        self.buf = bytearray()
        self.max_frame = max_frame
        self.allow_rsv1 = allow_rsv1
        self.expect_masked = expect_masked   # True only when parsing client frames (tests)

    def feed(self, data: bytes) -> list[Frame]:
        if data:
            self.buf += data
        frames = []
        while True:
            frame = self._one()
            if frame is None:
                return frames
            frames.append(frame)

    def _one(self) -> Frame | None:
        buf = self.buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        fin = bool(b0 & 0x80)
        rsv1 = bool(b0 & 0x40)
        if b0 & 0x30 or (rsv1 and not self.allow_rsv1):
            raise WebSocketError("protocol error: reserved bits set")
        opcode = b0 & 0x0F
        if opcode not in (OP_CONT,) + DATA_OPS + CONTROL_OPS:
            raise WebSocketError(f"protocol error: unknown opcode {opcode}")
        masked = bool(b1 & 0x80)
        if masked != self.expect_masked:
            raise WebSocketError("protocol error: server frames must not be masked" if masked
                                 else "protocol error: frame should be masked")
        n = b1 & 0x7F
        pos = 2
        if n == 126:
            if len(buf) < 4:
                return None
            n = struct.unpack_from("!H", buf, 2)[0]
            pos = 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n = struct.unpack_from("!Q", buf, 2)[0]
            if n >> 63:
                raise WebSocketError("protocol error: bad frame length")
            pos = 10
        if opcode in CONTROL_OPS and (n > 125 or not fin):
            raise WebSocketError("protocol error: bad control frame")
        if n > self.max_frame:
            raise WebSocketError(f"frame too large ({n} bytes)")
        key = b""
        if masked:
            if len(buf) < pos + 4:
                return None
            key = bytes(buf[pos:pos + 4])
            pos += 4
        if len(buf) < pos + n:
            return None
        payload = bytes(buf[pos:pos + n])
        del buf[:pos + n]
        if masked:
            payload = apply_mask(payload, key)
        return Frame(fin, rsv1, opcode, payload)


class Inflater:
    """permessage-deflate receiver side."""

    def __init__(self, no_context_takeover: bool = False, max_size: int = 4 * 1024 * 1024):
        self.no_context_takeover = no_context_takeover
        self.max_size = max_size
        self._d = zlib.decompressobj(-zlib.MAX_WBITS)

    def inflate(self, data: bytes) -> bytes:
        if self.no_context_takeover or self._d.eof:
            self._d = zlib.decompressobj(-zlib.MAX_WBITS)
        try:
            out = self._d.decompress(data + DEFLATE_TAIL, self.max_size + 1)
        except zlib.error as err:
            raise WebSocketError(f"could not inflate a message ({err})") from None
        if len(out) > self.max_size or self._d.unconsumed_tail:
            raise WebSocketError("compressed message too large")
        return out


class MessageAssembler:
    """Turns frames into events: ("message", opcode, bytes), ("ping", payload), ("pong", payload)
    or ("close", code, reason)."""

    def __init__(self, inflater: Inflater | None = None, max_message: int = 4 * 1024 * 1024):
        self.inflater = inflater
        self.max_message = max_message
        self._op: int | None = None
        self._compressed = False
        self._parts: list[bytes] = []
        self._size = 0

    def add(self, frame: Frame):
        op = frame.opcode
        if op in CONTROL_OPS:
            if frame.rsv1:
                raise WebSocketError("protocol error: compressed control frame")
            if op == OP_PING:
                return ("ping", frame.payload)
            if op == OP_PONG:
                return ("pong", frame.payload)
            code, reason = parse_close(frame.payload)
            return ("close", code, reason)
        if op == OP_CONT:
            if self._op is None:
                raise WebSocketError("protocol error: continuation without a message")
            if frame.rsv1:
                raise WebSocketError("protocol error: RSV1 on a continuation frame")
        else:
            if self._op is not None:
                raise WebSocketError("protocol error: new message inside a fragmented one")
            if frame.rsv1 and self.inflater is None:
                raise WebSocketError("protocol error: compressed frame without the extension")
            self._op = op
            self._compressed = frame.rsv1
            self._parts = []
            self._size = 0
        self._parts.append(frame.payload)
        self._size += len(frame.payload)
        if self._size > self.max_message:
            raise WebSocketError("message too large")
        if not frame.fin:
            return None
        data = b"".join(self._parts)
        opcode = self._op
        compressed = self._compressed
        self._op = None
        self._parts = []
        self._size = 0
        if compressed:
            data = self.inflater.inflate(data)
        if opcode == OP_TEXT:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise WebSocketError("protocol error: text message is not UTF-8") from None
        return ("message", opcode, data)


# ---------------------------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------------------------
def build_handshake(host: str, path: str, key: str, *, user_agent: str, deflate: bool = True,
                    extra: dict | None = None) -> bytes:
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", f"User-Agent: {user_agent}"]
    if deflate:
        lines.append("Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits")
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def parse_extensions(value: str | None) -> dict | None:
    """The server's Sec-WebSocket-Extensions answer -> permessage-deflate parameters (or None)."""
    if not value:
        return None
    for offer in value.split(","):
        parts = [p.strip() for p in offer.split(";") if p.strip()]
        if not parts or parts[0].lower() != "permessage-deflate":
            continue
        params = {}
        for p in parts[1:]:
            k, _, v = p.partition("=")
            params[k.strip().lower()] = v.strip().strip('"') or True
        return params
    return None


def check_handshake(head: bytes, key: str) -> tuple[dict, dict | None]:
    """Validate the server's 101 answer. Returns (headers, permessage-deflate params or None)."""
    text = head.decode("iso-8859-1")
    lines = text.split("\r\n")
    status = lines[0].split(" ", 2)
    if len(status) < 2 or not status[0].startswith("HTTP/1.1") or status[1] != "101":
        code = status[1] if len(status) > 1 else "?"
        raise WebSocketError(f"handshake refused (HTTP {code})")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        k, sep, v = line.partition(":")
        if sep:
            name = k.strip().lower()
            headers[name] = (headers[name] + ", " + v.strip()) if name in headers else v.strip()
    if headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in headers.get("connection", "").lower():
        raise WebSocketError("handshake refused (no upgrade)")
    if headers.get("sec-websocket-accept") != accept_key(key):
        raise WebSocketError("handshake refused (bad accept key)")
    ext = parse_extensions(headers.get("sec-websocket-extensions"))
    return headers, ext


# ---------------------------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------------------------
class WebSocketClient:
    def __init__(self, url: str, *, user_agent: str = "open-data-map-client/1.0", timeout: float = 15.0,
                 deflate: bool = True, max_message: int = 4 * 1024 * 1024, ping_every: float = 30.0,
                 idle_timeout: float = 120.0):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise ValueError("WebSocket URLs start with ws:// or wss://")
        self.url = url
        self.secure = parts.scheme == "wss"
        self.host = parts.hostname
        self.port = parts.port or (443 if self.secure else 80)
        self.path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self.user_agent = user_agent
        self.timeout = timeout
        self.want_deflate = deflate
        self.max_message = max_message
        self.ping_every = ping_every
        self.idle_timeout = idle_timeout
        self.sock = None
        self.parser: FrameParser | None = None
        self.assembler: MessageAssembler | None = None
        self.compression = False
        self.closed = False
        self.last_rx = 0.0
        self.last_tx = 0.0
        self.bytes_in = 0
        self.messages_in = 0
        self._pending: list[tuple[int, bytes]] = []

    # -- connection ------------------------------------------------------------------------------
    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            if self.secure:
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(raw, server_hostname=self.host)
            else:
                sock = raw
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_header = self.host if self.port in (80, 443) else f"{self.host}:{self.port}"
            sock.sendall(build_handshake(host_header, self.path, key, user_agent=self.user_agent,
                                         deflate=self.want_deflate))
            buf = b""
            deadline = time.monotonic() + self.timeout
            while b"\r\n\r\n" not in buf:
                if time.monotonic() > deadline or len(buf) > 16384:
                    raise WebSocketError("handshake timed out")
                chunk = sock.recv(4096)
                if not chunk:
                    raise WebSocketError("connection closed during the handshake")
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            _, ext = check_handshake(head, key)
        except Exception:
            try:
                raw.close()
            except OSError:
                pass
            raise
        self.attach(sock, rest, ext if self.want_deflate else None)

    def attach(self, sock, leftover: bytes = b"", deflate_params: dict | None = None) -> None:
        """Use an already-upgraded socket (also how the unit tests drive the client)."""
        self.sock = sock
        self.compression = deflate_params is not None
        inflater = Inflater(bool(deflate_params.get("server_no_context_takeover")), self.max_message) \
            if deflate_params is not None else None
        self.parser = FrameParser(max_frame=self.max_message, allow_rsv1=self.compression)
        self.assembler = MessageAssembler(inflater, self.max_message)
        self.closed = False
        self.last_rx = self.last_tx = time.monotonic()
        self._pending = []
        try:
            sock.settimeout(1.0)
        except (OSError, AttributeError):
            pass
        if leftover:
            self._consume(leftover)

    # -- sending -----------------------------------------------------------------------------------
    def _send(self, opcode: int, payload: bytes) -> None:
        if self.sock is None or self.closed:
            raise WebSocketError("not connected")
        try:
            self.sock.sendall(encode_frame(opcode, payload, mask=True))
        except OSError as err:
            raise WebSocketError(f"send failed ({err})") from None
        self.last_tx = time.monotonic()

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send(OP_BINARY, data)

    def ping(self, payload: bytes = b"") -> None:
        self._send(OP_PING, payload[:125])

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.sock is None:
            return
        if not self.closed:
            try:
                self.sock.sendall(encode_frame(OP_CLOSE, close_payload(code, reason), mask=True))
            except OSError:
                pass
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None

    # -- receiving ---------------------------------------------------------------------------------
    def _consume(self, data: bytes) -> None:
        self.bytes_in += len(data)
        for frame in self.parser.feed(data):
            event = self.assembler.add(frame)
            if event is None:
                continue
            kind = event[0]
            if kind == "message":
                self.messages_in += 1
                self._pending.append((event[1], event[2]))
            elif kind == "ping":
                self._send(OP_PONG, event[1])
            elif kind == "close":
                code, reason = event[1], event[2]
                if not self.closed:
                    try:
                        self.sock.sendall(encode_frame(OP_CLOSE, close_payload(code or 1000), mask=True))
                    except OSError:
                        pass
                self.closed = True
                raise WebSocketClosed(code, reason)

    def recv(self, stop_event=None) -> tuple[int, bytes] | None:
        """Next data message, or None when ``stop_event`` is set. Raises WebSocketError on failure."""
        while True:
            if self._pending:
                return self._pending.pop(0)
            if stop_event is not None and stop_event.is_set():
                return None
            if self.sock is None or self.closed:
                raise WebSocketError("not connected")
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                now = time.monotonic()
                if now - self.last_rx > self.idle_timeout:
                    raise WebSocketError(f"no data for {int(self.idle_timeout)} s") from None
                if now - max(self.last_rx, self.last_tx) > self.ping_every:
                    self.ping(b"keepalive")
                continue
            except (OSError, ssl.SSLError) as err:
                raise WebSocketError(f"receive failed ({err})") from None
            if not data:
                self.closed = True
                raise WebSocketClosed(None, "the server dropped the connection")
            self.last_rx = time.monotonic()
            self._consume(data)
