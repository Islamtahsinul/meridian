"""AIS providers. Each one is independent, fails softly and feeds the shared vessel store.

- Digitraffic (Fintraffic, Finland): keyless REST, polled every minute. The first poll takes the
  default 24-hour window of latest positions (the whole fleet they track, about 800 vessels in the
  northern Baltic); later polls ask only for positions newer than the last one we have (``from``).
- aisstream.io: a WebSocket stream (free key). permessage-deflate is negotiated because the service
  limits the bandwidth of uncompressed connections.
- Kystverket (Norwegian Coastal Administration): raw NMEA over TCP, optional (KYSTVERKET_AIS=1).
"""

from __future__ import annotations

import json
import random
import re
import socket
import time
from calendar import timegm

from . import ais as aislib
from .ws import WebSocketClient, WebSocketClosed, WebSocketError

DT_BASE = "https://meri.digitraffic.fi/api/ais/v1"
DT_HEADERS = {"Digitraffic-User": "open-data-map-client"}
AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
AISSTREAM_TYPES = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport",
                   "ShipStaticData", "StaticDataReport"]
KYSTVERKET_HOST, KYSTVERKET_PORT = "153.44.253.27", 5631

SRC_DT, SRC_AIS, SRC_KV = 1, 2, 3


# ---------------------------------------------------------------------------------------------
# Digitraffic
# ---------------------------------------------------------------------------------------------
def parse_dt_locations(data) -> list[dict]:
    out = []
    feats = data.get("features") if isinstance(data, dict) else None
    for f in feats or []:
        try:
            props = f.get("properties") or {}
            lon, lat = f["geometry"]["coordinates"][:2]
            mmsi = int(props.get("mmsi") or f.get("mmsi"))
            ts = props.get("timestampExternal")
            t = float(ts) / 1000.0 if ts else None
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        sog = props.get("sog")
        cog = props.get("cog")
        hdg = props.get("heading")
        rot = props.get("rot")
        out.append({
            "mmsi": mmsi, "lat": lat, "lon": lon, "t": t,
            "sog": sog if isinstance(sog, (int, float)) and sog < 102.2 else None,
            "cog": cog if isinstance(cog, (int, float)) and cog < 360 else None,
            "hdg": hdg if isinstance(hdg, (int, float)) and hdg < 360 else None,
            "nav": props.get("navStat"),
            "rot": aislib._rot(int(rot)) if isinstance(rot, (int, float)) and -128 <= rot <= 127 else None,
        })
    return out


def parse_dt_vessels(data) -> list[dict]:
    out = []
    for r in data if isinstance(data, list) else []:
        try:
            mmsi = int(r["mmsi"])
        except (KeyError, TypeError, ValueError):
            continue
        dims = {"dim_a": r.get("referencePointA"), "dim_b": r.get("referencePointB"),
                "dim_c": r.get("referencePointC"), "dim_d": r.get("referencePointD")}
        length, beam = aislib.dimensions(dims)
        draught = r.get("draught")
        ts = r.get("timestamp")
        out.append({
            "mmsi": mmsi, "name": r.get("name"), "imo": r.get("imo"), "callsign": r.get("callSign"),
            "ship_type": r.get("shipType"), "dest": r.get("destination"),
            "eta": aislib.eta_from_packed(r.get("eta")),
            "draught": draught / 10.0 if isinstance(draught, (int, float)) and draught > 0 else None,
            "length": length, "beam": beam, "t": float(ts) / 1000.0 if isinstance(ts, (int, float)) else None,
        })
    return out


class DigitrafficPoller:
    def __init__(self, net, store, status, log):
        self.net = net
        self.store = store
        self.status = status          # status(ok, count=None, error=None, note=None)
        self.log = log
        self.last_pos_ms: int | None = None
        self.last_static_fetch = 0.0
        self.static_since_ms: int | None = None
        self.receiving_since: float | None = None
        self.last_ok = 0.0
        self.last_run = 0.0
        self.vessels_seen: dict[int, float] = {}
        self.stats = {"polls": 0, "positions": 0, "statics": 0, "errors": 0, "bytes_hint": 0}

    def poll(self) -> None:
        now = time.time()
        self.last_run = now
        try:
            if now - self.last_static_fetch > 600 or self.static_since_ms is None:
                self._fetch_static(now)
            params = None
            if self.last_pos_ms is not None and now - self.last_ok < 1800:
                params = {"from": max(int((now - 86400) * 1000), self.last_pos_ms - 90_000)}
            data = self.net.get_json(f"{DT_BASE}/locations", params=params, headers=DT_HEADERS, timeout=30,
                                     retries=1)
        except Exception as err:
            self.stats["errors"] += 1
            if now - self.last_ok > 900:
                self.receiving_since = None
            self.status(False, error=str(err))
            raise
        rows = parse_dt_locations(data)
        newest = self.last_pos_ms or 0
        applied = 0
        for r in rows:
            if self.store.position(r["mmsi"], r["t"], r["lat"], r["lon"], sog=r["sog"], cog=r["cog"], hdg=r["hdg"],
                                   nav=r["nav"], rot=r["rot"], src=SRC_DT):
                applied += 1
            if r["t"]:
                newest = max(newest, int(r["t"] * 1000))
                self.vessels_seen[r["mmsi"]] = r["t"]
        self.last_pos_ms = newest or None
        if self.receiving_since is None or now - self.last_ok > 900:
            self.receiving_since = now
        self.last_ok = now
        self.stats["polls"] += 1
        self.stats["positions"] += applied
        cutoff = now - 3600
        for mmsi in [m for m, t in self.vessels_seen.items() if t < now - 86400]:
            del self.vessels_seen[mmsi]
        heard = sum(1 for t in self.vessels_seen.values() if t >= cutoff)
        self.status(True, count=heard, note=f"{heard:,} vessels heard in the last hour, "
                                            f"{len(self.vessels_seen):,} in 24 hours")

    def _fetch_static(self, now: float) -> None:
        params = None
        if self.static_since_ms is not None:
            params = {"from": self.static_since_ms - 300_000}
        data = self.net.get_json(f"{DT_BASE}/vessels", params=params, headers=DT_HEADERS, timeout=30, retries=1)
        rows = parse_dt_vessels(data)
        newest = self.static_since_ms or 0
        for r in rows:
            t = r.pop("t")
            mmsi = r.pop("mmsi")
            self.store.static(mmsi, t, src=SRC_DT, **r)
            if t:
                newest = max(newest, int(t * 1000))
        self.static_since_ms = newest or int(now * 1000)
        self.last_static_fetch = now
        self.stats["statics"] += len(rows)


# ---------------------------------------------------------------------------------------------
# Shared: decoded AIS (NMEA or aisstream) -> store
# ---------------------------------------------------------------------------------------------
def apply_decoded(store, msg: dict, src: int, t: float) -> None:
    mtype = msg.get("type")
    mmsi = msg.get("mmsi")
    if mtype in aislib.POSITION_TYPES and msg.get("lat") is not None and msg.get("lon") is not None:
        store.position(mmsi, t, msg["lat"], msg["lon"], sog=msg.get("sog"), cog=msg.get("cog"),
                       hdg=msg.get("heading"), nav=msg.get("nav_status"), rot=msg.get("rot"), src=src,
                       ais=msg.get("cls"))
    if mtype == 5:
        length, beam = aislib.dimensions(msg)
        store.static(mmsi, t, src=src, name=msg.get("name"), imo=msg.get("imo"), callsign=msg.get("callsign"),
                     ship_type=msg.get("ship_type"), dest=msg.get("destination"), eta=msg.get("eta"),
                     draught=msg.get("draught"), length=length, beam=beam, ais="A")
    elif mtype == 19:
        length, beam = aislib.dimensions(msg)
        store.static(mmsi, t, src=src, name=msg.get("name"), ship_type=msg.get("ship_type"), length=length,
                     beam=beam, ais="B")
    elif mtype == 24:
        if msg.get("part") == 0:
            store.static(mmsi, t, src=src, name=msg.get("name"), ais="B")
        else:
            length, beam = aislib.dimensions(msg)
            store.static(mmsi, t, src=src, ship_type=msg.get("ship_type"), callsign=msg.get("callsign"),
                         length=length, beam=beam, ais="B")


# ---------------------------------------------------------------------------------------------
# aisstream.io
# ---------------------------------------------------------------------------------------------
_TIME_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(\.\d+)?")


def parse_time_utc(text: str | None, now: float) -> float:
    if text:
        m = _TIME_RE.search(str(text))
        if m:
            y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
            try:
                t = timegm((y, mo, d, h, mi, s)) + (float(m.group(7)) if m.group(7) else 0.0)
            except (ValueError, OverflowError):
                t = None
            if t is not None and now - 6 * 3600 <= t <= now + 60:
                return t
    return now


def _ci(obj: dict, *names):
    """Case-insensitive field lookup (aisstream has used both "latitude" and "Latitude")."""
    for n in names:
        if n in obj:
            return obj[n]
    lower = {k.lower(): v for k, v in obj.items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def _dims(d) -> tuple[float | None, float | None]:
    if not isinstance(d, dict):
        return None, None
    return aislib.dimensions({"dim_a": d.get("A"), "dim_b": d.get("B"), "dim_c": d.get("C"), "dim_d": d.get("D")})


def parse_aisstream(obj: dict, now: float) -> dict | None:
    """One aisstream envelope -> a decoded-message dict like ais.decode_payload() returns, or None.
    Error envelopes come back as {"error": "..."}; subscription confirmations as {"confirmed": ...}."""
    if not isinstance(obj, dict):
        return None
    if obj.get("error"):
        return {"error": str(obj.get("error"))[:200]}
    mtype = obj.get("MessageType")
    body = (obj.get("Message") or {}).get(mtype) if isinstance(obj.get("Message"), dict) else None
    if mtype == "SubscriptionConfirmation":
        msg = obj.get("Message") or {}
        conf = msg.get("SubscriptionConfirmation", msg) if isinstance(msg, dict) else {}
        return {"confirmed": True, "compression": bool((conf or {}).get("CompressionEnabled"))}
    if not isinstance(body, dict):
        return None
    meta = obj.get("MetaData") or {}
    mmsi = body.get("UserID") or _ci(meta, "MMSI")
    try:
        mmsi = int(mmsi)
    except (TypeError, ValueError):
        return None
    if body.get("Valid") is False:
        return None
    t = parse_time_utc(_ci(meta, "time_utc"), now)
    if mtype in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
        lat = body.get("Latitude")
        lon = body.get("Longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or abs(lat) > 90 or abs(lon) > 180:
            return None
        sog = body.get("Sog")
        cog = body.get("Cog")
        hdg = body.get("TrueHeading")
        out = {"type": 1 if mtype == "PositionReport" else (19 if mtype.startswith("Extended") else 18),
               "mmsi": mmsi, "t": t, "lat": float(lat), "lon": float(lon),
               "sog": sog if isinstance(sog, (int, float)) and sog < 102.2 else None,
               "cog": cog if isinstance(cog, (int, float)) and cog < 360 else None,
               "heading": hdg if isinstance(hdg, (int, float)) and hdg < 360 else None,
               "cls": "A" if mtype == "PositionReport" else "B"}
        if mtype == "PositionReport":
            out["nav_status"] = body.get("NavigationalStatus")
            rot = body.get("RateOfTurn")
            out["rot"] = aislib._rot(int(rot)) if isinstance(rot, (int, float)) and -128 <= rot <= 127 else None
        if mtype == "ExtendedClassBPositionReport":
            out["name"] = body.get("Name")
            out["ship_type"] = body.get("Type")
            length, beam = _dims(body.get("Dimension"))
            out.update({"length": length, "beam": beam})
        return out
    if mtype == "ShipStaticData":
        eta = body.get("Eta") or {}
        length, beam = _dims(body.get("Dimension"))
        imo = body.get("ImoNumber")
        draught = body.get("MaximumStaticDraught")
        return {"type": 5, "mmsi": mmsi, "t": t, "name": body.get("Name"), "imo": imo or None,
                "callsign": body.get("CallSign"), "ship_type": body.get("Type"), "destination": body.get("Destination"),
                "eta": aislib.eta_text(eta.get("Month"), eta.get("Day"), eta.get("Hour"), eta.get("Minute"))
                if isinstance(eta, dict) else None,
                "draught": draught if isinstance(draught, (int, float)) else None, "length": length, "beam": beam,
                "cls": "A"}
    if mtype == "StaticDataReport":
        part_b = bool(body.get("PartNumber"))
        if not part_b:
            rep = body.get("ReportA") or {}
            if rep.get("Valid") is False:
                return None
            return {"type": 24, "part": 0, "mmsi": mmsi, "t": t, "name": rep.get("Name"), "cls": "B"}
        rep = body.get("ReportB") or {}
        if rep.get("Valid") is False:
            return None
        length, beam = _dims(rep.get("Dimension"))
        return {"type": 24, "part": 1, "mmsi": mmsi, "t": t, "ship_type": rep.get("ShipType"),
                "callsign": rep.get("CallSign"), "length": length, "beam": beam, "cls": "B"}
    return None


def apply_aisstream(store, msg: dict, src: int = SRC_AIS) -> None:
    t = msg.get("t")
    mtype = msg.get("type")
    if mtype in (1, 18, 19):
        store.position(msg["mmsi"], t, msg["lat"], msg["lon"], sog=msg.get("sog"), cog=msg.get("cog"),
                       hdg=msg.get("heading"), nav=msg.get("nav_status"), rot=msg.get("rot"), src=src,
                       ais=msg.get("cls"))
        if mtype == 19 and (msg.get("name") or msg.get("ship_type")):
            store.static(msg["mmsi"], t, src=src, name=msg.get("name"), ship_type=msg.get("ship_type"),
                         length=msg.get("length"), beam=msg.get("beam"), ais="B")
    elif mtype == 5:
        store.static(msg["mmsi"], t, src=src, name=msg.get("name"), imo=msg.get("imo"), callsign=msg.get("callsign"),
                     ship_type=msg.get("ship_type"), dest=msg.get("destination"), eta=msg.get("eta"),
                     draught=msg.get("draught"), length=msg.get("length"), beam=msg.get("beam"), ais="A")
    elif mtype == 24:
        if msg.get("part") == 0:
            store.static(msg["mmsi"], t, src=src, name=msg.get("name"), ais="B")
        else:
            store.static(msg["mmsi"], t, src=src, ship_type=msg.get("ship_type"), callsign=msg.get("callsign"),
                         length=msg.get("length"), beam=msg.get("beam"), ais="B")


def parse_area(setting: str | None, straits: list[dict]) -> tuple[list, str]:
    """AISSTREAM_AREA -> (aisstream BoundingBoxes, description)."""
    text = (setting or "world").strip().lower()
    if text in ("", "world", "global"):
        return [[[-90.0, -180.0], [90.0, 180.0]]], "the whole world"
    if text == "straits":
        boxes = []
        for s in straits:
            w, so, e, n = s["bbox"]
            boxes.append([[max(-90.0, so - 0.75), max(-180.0, w - 0.75)], [min(90.0, n + 0.75), min(180.0, e + 0.75)]])
        return boxes, "the monitored straits"
    boxes = []
    for part in text.split(";"):
        nums = [p.strip() for p in part.split(",")]
        if len(nums) != 4:
            continue
        try:
            w, s, e, n = (float(x) for x in nums)
        except ValueError:
            continue
        if -180 <= w <= 180 and -180 <= e <= 180 and -90 <= s < n <= 90:
            boxes.append([[s, w], [n, e]])
    if boxes:
        return boxes[:50], f"{len(boxes)} custom area{'s' if len(boxes) > 1 else ''}"
    return [[[-90.0, -180.0], [90.0, 180.0]]], "the whole world (AISSTREAM_AREA was not understood)"


class AisStreamClient:
    """Runs in m.thread(): connect, subscribe, read, reconnect with backoff and jitter."""

    def __init__(self, key: str, boxes: list, store, status, log, *, user_agent: str, area_note: str = ""):
        self._key = key
        self.boxes = boxes
        self.area_note = area_note
        self.store = store
        self.status = status
        self.log = log
        self.user_agent = user_agent
        self.connected_since: float | None = None
        self.receiving_since: float | None = None
        self.last_message = 0.0
        self.compression = None
        self.stats = {"messages": 0, "positions": 0, "statics": 0, "errors": 0, "connects": 0, "bytes": 0}
        self._last_pos: dict[int, float] = {}
        self._minute: list[float] = []

    def subscription(self) -> str:
        return json.dumps({"APIKey": self._key, "BoundingBoxes": self.boxes, "FilterMessageTypes": AISSTREAM_TYPES})

    def run(self, stop_event) -> None:
        backoff = 5.0
        silent_drops = 0
        while not stop_event.is_set():
            ws = WebSocketClient(AISSTREAM_URL, user_agent=self.user_agent, timeout=30, deflate=True,
                                 ping_every=25, idle_timeout=120)
            refused = False
            heard = False
            subscribed_at = None
            try:
                ws.connect()
                ws.send_text(self.subscription())
                subscribed_at = time.monotonic()
                self.stats["connects"] += 1
                self.connected_since = time.time()
                self.status(None, note=f"Connected, waiting for data ({self.area_note})")
                last_status = 0.0
                while not stop_event.is_set():
                    item = ws.recv(stop_event)
                    if item is None:
                        break
                    _, data = item
                    heard = True
                    self.stats["bytes"] = ws.bytes_in
                    now = time.time()
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    msg = parse_aisstream(obj, now)
                    if msg is None:
                        continue
                    if "error" in msg:
                        refused = True
                        raise WebSocketError(f"aisstream.io refused the subscription: {msg['error']}")
                    if msg.get("confirmed"):
                        self.compression = msg.get("compression") or ws.compression
                        continue
                    self._handle(msg, now)
                    if time.monotonic() - subscribed_at > 60:
                        backoff = 5.0
                        silent_drops = 0
                    if now - last_status > 30:
                        last_status = now
                        self._report(now, ws)
            except (WebSocketClosed, WebSocketError, OSError, ValueError) as err:
                self.stats["errors"] += 1
                self.receiving_since = None
                text = str(err)
                # aisstream.io drops a connection with a bad key or subscription without a word.
                if not heard and subscribed_at is not None and time.monotonic() - subscribed_at < 20:
                    silent_drops += 1
                    refused = refused or silent_drops >= 2
                    text = ("aisstream.io closed the connection without confirming the subscription. "
                            "Check AISSTREAM_API_KEY and AISSTREAM_AREA.")
                elif refused:
                    text += ". Check AISSTREAM_API_KEY."
                self.status(False, error=text[:300])
                self.log.info("aisstream: %s", text[:300])
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
                self.connected_since = None
            if stop_event.is_set():
                return
            wait = min(1800.0, 300.0 * max(1, silent_drops)) if refused else backoff
            wait *= random.uniform(0.8, 1.25)
            backoff = min(backoff * 2, 600.0)
            if stop_event.wait(wait):
                return

    def _handle(self, msg: dict, now: float) -> None:
        self.stats["messages"] += 1
        self._minute.append(now)
        self.last_message = now
        if self.receiving_since is None:
            self.receiving_since = now
        mtype = msg.get("type")
        if mtype in (1, 18, 19):
            last = self._last_pos.get(msg["mmsi"], 0.0)
            if now - last < 10 and mtype != 19:
                return                            # a fix every 10 s per vessel is plenty
            self._last_pos[msg["mmsi"]] = now
            if len(self._last_pos) > 400_000:
                self._last_pos.clear()
            self.stats["positions"] += 1
        else:
            self.stats["statics"] += 1
        apply_aisstream(self.store, msg)

    def _report(self, now: float, ws) -> None:
        self._minute = [t for t in self._minute if now - t <= 60]
        heard = sum(1 for t in self._last_pos.values() if now - t <= 3600)
        comp = "compressed" if ws.compression else "uncompressed"
        self.status(True, count=heard, note=f"{len(self._minute):,} messages a minute ({comp}), "
                                            f"{heard:,} vessels in the last hour, area: {self.area_note}")


# ---------------------------------------------------------------------------------------------
# Kystverket (raw NMEA over TCP)
# ---------------------------------------------------------------------------------------------
class KystverketClient:
    def __init__(self, store, status, log, host: str = KYSTVERKET_HOST, port: int = KYSTVERKET_PORT):
        self.store = store
        self.status = status
        self.log = log
        self.host = host
        self.port = port
        self.assembler = aislib.Assembler()
        self.receiving_since: float | None = None
        self.last_message = 0.0
        self.stats = {"lines": 0, "messages": 0, "errors": 0, "connects": 0}
        self._minute: list[float] = []
        self._seen: dict[int, float] = {}

    def run(self, stop_event) -> None:
        backoff = 10.0
        while not stop_event.is_set():
            sock = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=15)
                sock.settimeout(2.0)
                self.stats["connects"] += 1
                self.status(None, note="Connected, waiting for data")
                buf = b""
                last_rx = time.monotonic()
                last_status = 0.0
                while not stop_event.is_set():
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        if time.monotonic() - last_rx > 120:
                            raise OSError("no data for 2 minutes")
                        continue
                    if not chunk:
                        raise OSError("the server closed the connection")
                    last_rx = time.monotonic()
                    buf += chunk
                    if len(buf) > 1_000_000:
                        buf = buf[-100_000:]
                    *lines, buf = buf.split(b"\n")
                    now = time.time()
                    for raw in lines:
                        self._line(raw.decode("ascii", errors="replace"), now)
                    if now - last_status > 30:
                        last_status = now
                        self._report(now)
                    backoff = 10.0
            except OSError as err:
                self.stats["errors"] += 1
                self.receiving_since = None
                self.status(False, error=f"{self.host}:{self.port}: {err}"[:300])
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if stop_event.wait(backoff * random.uniform(0.8, 1.25)):
                return
            backoff = min(backoff * 2, 900.0)

    def _line(self, line: str, now: float) -> None:
        self.stats["lines"] += 1
        msg = self.assembler.feed(line, now)
        if msg is None or msg.get("own"):
            return
        self.stats["messages"] += 1
        self._minute.append(now)
        self.last_message = now
        if self.receiving_since is None:
            self.receiving_since = now
        t = msg.get("tag_time") or now
        if not (now - 3600 <= t <= now + 60):
            t = now
        self._seen[msg["mmsi"]] = now
        apply_decoded(self.store, msg, SRC_KV, t)

    def _report(self, now: float) -> None:
        self._minute = [t for t in self._minute if now - t <= 60]
        for mmsi in [m for m, t in self._seen.items() if now - t > 86400]:
            del self._seen[mmsi]
        heard = sum(1 for t in self._seen.values() if now - t <= 3600)
        self.status(True, count=heard, note=f"{len(self._minute):,} messages a minute, {heard:,} vessels in the last hour")
