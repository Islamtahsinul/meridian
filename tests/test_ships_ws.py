"""WebSocket client tests (RFC 6455 framing, RFC 7692 permessage-deflate). No network: the client
is driven through a scripted fake socket."""

import socket
import struct
import threading
import unittest
import zlib

from meridian.modules.ships import ws


def server_frame(opcode, payload=b"", fin=True, rsv1=False):
    """A frame as a server sends it: unmasked."""
    return ws.encode_frame(opcode, payload, fin=fin, rsv1=rsv1, mask=False)


def deflate_message(data: bytes, compressor=None) -> bytes:
    c = compressor or zlib.compressobj(wbits=-15)
    out = c.compress(data) + c.flush(zlib.Z_SYNC_FLUSH)
    assert out.endswith(ws.DEFLATE_TAIL)
    return out[:-4]


class FakeSocket:
    """recv() hands out scripted chunks, then raises socket.timeout; sendall() records frames."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.sent = bytearray()
        self.closed = False
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        if self.chunks:
            chunk = self.chunks.pop(0)
            if isinstance(chunk, Exception):
                raise chunk
            return chunk
        raise socket.timeout()

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True

    def sent_frames(self):
        return ws.FrameParser(expect_masked=True).feed(bytes(self.sent))


class Framing(unittest.TestCase):
    def test_accept_key_rfc_example(self):
        self.assertEqual(ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_mask_roundtrip(self):
        key = b"\x37\xfa\x21\x3d"
        data = b"Hello"
        masked = ws.apply_mask(data, key)
        self.assertEqual(masked, bytes([0x7f, 0x9f, 0x4d, 0x51, 0x58]))   # RFC 6455 section 5.7
        self.assertEqual(ws.apply_mask(masked, key), data)
        self.assertEqual(ws.apply_mask(b"", key), b"")

    def test_rfc_masked_hello(self):
        frame = ws.encode_frame(ws.OP_TEXT, b"Hello", mask=True, mask_key=b"\x37\xfa\x21\x3d")
        self.assertEqual(frame, bytes([0x81, 0x85, 0x37, 0xfa, 0x21, 0x3d, 0x7f, 0x9f, 0x4d, 0x51, 0x58]))

    def test_length_encodings(self):
        for n in (0, 1, 125, 126, 127, 65535, 65536, 70000):
            payload = bytes(i % 251 for i in range(n))
            frame = ws.encode_frame(ws.OP_BINARY, payload, mask=True)
            frames = ws.FrameParser(expect_masked=True).feed(frame)
            self.assertEqual(len(frames), 1, n)
            self.assertEqual(frames[0].payload, payload, n)
            if n < 126:
                self.assertEqual(frame[1] & 0x7F, n)
            elif n < 65536:
                self.assertEqual(frame[1] & 0x7F, 126)
            else:
                self.assertEqual(frame[1] & 0x7F, 127)

    def test_incremental_parse(self):
        frame = server_frame(ws.OP_TEXT, b"x" * 300)
        p = ws.FrameParser()
        got = []
        for i in range(0, len(frame), 7):
            got.extend(p.feed(frame[i:i + 7]))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].payload, b"x" * 300)

    def test_server_frames_must_not_be_masked(self):
        with self.assertRaises(ws.WebSocketError):
            ws.FrameParser().feed(ws.encode_frame(ws.OP_TEXT, b"hi", mask=True))

    def test_reserved_bits_and_opcodes(self):
        with self.assertRaises(ws.WebSocketError):
            ws.FrameParser().feed(bytes([0x81 | 0x40, 0x00]))      # RSV1 without the extension
        with self.assertRaises(ws.WebSocketError):
            ws.FrameParser().feed(bytes([0x83, 0x00]))             # opcode 3 is reserved

    def test_control_frame_limits(self):
        with self.assertRaises(ValueError):
            ws.encode_frame(ws.OP_PING, b"x" * 126)
        with self.assertRaises(ws.WebSocketError):
            ws.FrameParser().feed(bytes([0x09, 0x00]))             # fragmented ping

    def test_frame_size_cap(self):
        head = bytes([0x82, 127]) + struct.pack("!Q", 10 ** 9)
        with self.assertRaises(ws.WebSocketError):
            ws.FrameParser(max_frame=1 << 20).feed(head)

    def test_close_payload(self):
        self.assertEqual(ws.parse_close(ws.close_payload(1001, "going away")), (1001, "going away"))
        self.assertEqual(ws.parse_close(b""), (None, ""))


class Messages(unittest.TestCase):
    def test_fragmented_with_interleaved_ping(self):
        a = ws.MessageAssembler()
        events = [a.add(f) for f in ws.FrameParser().feed(
            server_frame(ws.OP_TEXT, b"Hel", fin=False) + server_frame(ws.OP_PING, b"p1")
            + server_frame(ws.OP_CONT, b"lo ", fin=False) + server_frame(ws.OP_CONT, b"world", fin=True))]
        self.assertEqual(events[0], None)
        self.assertEqual(events[1], ("ping", b"p1"))
        self.assertEqual(events[2], None)
        self.assertEqual(events[3], ("message", ws.OP_TEXT, b"Hello world"))

    def test_continuation_without_start(self):
        with self.assertRaises(ws.WebSocketError):
            ws.MessageAssembler().add(ws.Frame(True, False, ws.OP_CONT, b"x"))

    def test_new_message_inside_fragmented(self):
        a = ws.MessageAssembler()
        a.add(ws.Frame(False, False, ws.OP_TEXT, b"a"))
        with self.assertRaises(ws.WebSocketError):
            a.add(ws.Frame(True, False, ws.OP_TEXT, b"b"))

    def test_invalid_utf8_text(self):
        with self.assertRaises(ws.WebSocketError):
            ws.MessageAssembler().add(ws.Frame(True, False, ws.OP_TEXT, b"\xff\xfe"))

    def test_message_cap(self):
        a = ws.MessageAssembler(max_message=10)
        a.add(ws.Frame(False, False, ws.OP_BINARY, b"x" * 8))
        with self.assertRaises(ws.WebSocketError):
            a.add(ws.Frame(True, False, ws.OP_CONT, b"x" * 8))

    def test_close_event(self):
        ev = ws.MessageAssembler().add(ws.Frame(True, False, ws.OP_CLOSE, ws.close_payload(1008, "policy")))
        self.assertEqual(ev, ("close", 1008, "policy"))


class Deflate(unittest.TestCase):
    def test_context_takeover_across_messages(self):
        comp = zlib.compressobj(wbits=-15)
        inf = ws.Inflater()
        a = ws.MessageAssembler(inf)
        texts = [b'{"MessageType":"PositionReport","n":1}', b'{"MessageType":"PositionReport","n":2}']
        for t in texts:
            payload = deflate_message(t, comp)
            ev = a.add(ws.Frame(True, True, ws.OP_BINARY, payload))
            self.assertEqual(ev, ("message", ws.OP_BINARY, t))

    def test_no_context_takeover(self):
        inf = ws.Inflater(no_context_takeover=True)
        for t in (b"first message", b"second message"):
            self.assertEqual(inf.inflate(deflate_message(t)), t)

    def test_fragmented_compressed_message(self):
        payload = deflate_message(b"A" * 5000)
        a = ws.MessageAssembler(ws.Inflater())
        self.assertIsNone(a.add(ws.Frame(False, True, ws.OP_BINARY, payload[:5])))
        ev = a.add(ws.Frame(True, False, ws.OP_CONT, payload[5:]))
        self.assertEqual(ev[2], b"A" * 5000)

    def test_inflate_bomb_capped(self):
        payload = deflate_message(b"\0" * 2_000_000)
        with self.assertRaises(ws.WebSocketError):
            ws.Inflater(max_size=100_000).inflate(payload)

    def test_compressed_without_extension(self):
        with self.assertRaises(ws.WebSocketError):
            ws.MessageAssembler(None).add(ws.Frame(True, True, ws.OP_BINARY, b"x"))

    def test_parse_extensions(self):
        p = ws.parse_extensions("permessage-deflate; server_no_context_takeover; client_max_window_bits=15")
        self.assertEqual(p, {"server_no_context_takeover": True, "client_max_window_bits": "15"})
        self.assertIsNone(ws.parse_extensions("x-webkit-deflate-frame"))
        self.assertIsNone(ws.parse_extensions(None))


class Handshake(unittest.TestCase):
    KEY = "dGhlIHNhbXBsZSBub25jZQ=="

    def test_build(self):
        req = ws.build_handshake("stream.example", "/v0/stream", self.KEY, user_agent="ua/1").decode("ascii")
        self.assertTrue(req.startswith("GET /v0/stream HTTP/1.1\r\n"))
        self.assertIn("Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n", req)
        self.assertIn("Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n", req)
        self.assertTrue(req.endswith("\r\n\r\n"))

    def test_check_ok(self):
        head = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n"
                "Sec-WebSocket-Extensions: permessage-deflate; server_no_context_takeover").encode()
        headers, ext = ws.check_handshake(head, self.KEY)
        self.assertEqual(headers["upgrade"], "websocket")
        self.assertEqual(ext, {"server_no_context_takeover": True})

    def test_check_refusals(self):
        with self.assertRaises(ws.WebSocketError):
            ws.check_handshake(b"HTTP/1.1 400 Bad Request\r\n", self.KEY)
        with self.assertRaises(ws.WebSocketError):
            ws.check_handshake(b"HTTP/1.1 101 OK\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               b"Sec-WebSocket-Accept: wrong", self.KEY)


class Client(unittest.TestCase):
    def client(self, chunks, deflate=None):
        c = ws.WebSocketClient("wss://stream.example/v0/stream", ping_every=0.0, idle_timeout=3600)
        sock = FakeSocket(chunks)
        c.attach(sock, b"", deflate)
        return c, sock

    def test_receive_and_answer_ping(self):
        c, sock = self.client([server_frame(ws.OP_PING, b"are you there"), server_frame(ws.OP_BINARY, b'{"a":1}')])
        self.assertEqual(c.recv(), (ws.OP_BINARY, b'{"a":1}'))
        frames = sock.sent_frames()
        self.assertEqual(frames[0].opcode, ws.OP_PONG)
        self.assertEqual(frames[0].payload, b"are you there")

    def test_client_frames_are_masked(self):
        c, sock = self.client([])
        c.send_text('{"APIKey":"x"}')
        self.assertTrue(sock.sent[1] & 0x80)
        self.assertEqual(sock.sent_frames()[0].payload, b'{"APIKey":"x"}')

    def test_leftover_after_handshake(self):
        c = ws.WebSocketClient("ws://example/")
        sock = FakeSocket([])
        c.attach(sock, server_frame(ws.OP_TEXT, b"early"), None)
        self.assertEqual(c.recv(), (ws.OP_TEXT, b"early"))

    def test_compressed_stream(self):
        comp = zlib.compressobj(wbits=-15)
        msgs = [b'{"n":%d}' % i for i in range(3)]
        chunks = [server_frame(ws.OP_BINARY, deflate_message(m, comp), rsv1=True) for m in msgs]
        c, _ = self.client(chunks, deflate={})
        self.assertTrue(c.compression)
        self.assertEqual([c.recv()[1] for _ in msgs], msgs)

    def test_server_close_is_echoed(self):
        c, sock = self.client([server_frame(ws.OP_CLOSE, ws.close_payload(1000, "bye"))])
        with self.assertRaises(ws.WebSocketClosed) as cm:
            c.recv()
        self.assertEqual(cm.exception.code, 1000)
        self.assertEqual(sock.sent_frames()[-1].opcode, ws.OP_CLOSE)

    def test_eof(self):
        c, _ = self.client([b""])
        with self.assertRaises(ws.WebSocketClosed):
            c.recv()

    def test_stop_event_returns_none(self):
        c, _ = self.client([])
        stop = threading.Event()
        stop.set()
        self.assertIsNone(c.recv(stop))

    def test_keepalive_ping_when_quiet(self):
        c, sock = self.client([socket.timeout(), server_frame(ws.OP_TEXT, b"ok")])
        c.last_rx -= 10
        c.last_tx -= 10
        self.assertEqual(c.recv(), (ws.OP_TEXT, b"ok"))
        self.assertEqual(sock.sent_frames()[0].opcode, ws.OP_PING)

    def test_idle_timeout(self):
        c = ws.WebSocketClient("ws://example/", idle_timeout=1.0)
        c.attach(FakeSocket([socket.timeout()]), b"", None)
        c.last_rx -= 5
        with self.assertRaises(ws.WebSocketError):
            c.recv()

    def test_close_sends_close_frame(self):
        c, sock = self.client([])
        c.close(1000, "done")
        self.assertTrue(sock.closed)
        f = ws.FrameParser(expect_masked=True).feed(bytes(sock.sent))[0]
        self.assertEqual(f.opcode, ws.OP_CLOSE)
        self.assertEqual(ws.parse_close(f.payload), (1000, "done"))

    def test_bad_url(self):
        with self.assertRaises(ValueError):
            ws.WebSocketClient("https://example/")


if __name__ == "__main__":
    unittest.main()
