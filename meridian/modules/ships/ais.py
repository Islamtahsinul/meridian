"""AIS NMEA 0183 decoding (!AIVDM / !AIVDO): checksums, tag blocks, 6-bit payload armour,
multi-sentence assembly and message types 1, 2, 3 (class A position), 5 (static and voyage),
18 (class B position), 19 (extended class B) and 24 (class B static, parts A and B).

Pure functions and one small stateful assembler; no I/O. Field layouts follow ITU-R M.1371-5 and
Eric S. Raymond's "AIVDM/AIVDO protocol decoding" notes (gpsd project).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

NAV_STATUS = {
    0: "Under way using engine", 1: "At anchor", 2: "Not under command", 3: "Restricted manoeuvrability",
    4: "Constrained by her draught", 5: "Moored", 6: "Aground", 7: "Engaged in fishing",
    8: "Under way sailing", 9: "Reserved (high-speed craft)", 10: "Reserved (wing in ground)",
    11: "Towing astern", 12: "Pushing ahead or towing alongside", 13: "Reserved",
    14: "AIS-SART, MOB or EPIRB active", 15: "Not defined",
}

POSITION_TYPES = (1, 2, 3, 18, 19)
STATIC_TYPES = (5, 19, 24)


# ---------------------------------------------------------------------------------------------
# Sentence layer
# ---------------------------------------------------------------------------------------------
def nmea_checksum(body: str) -> int:
    """XOR of every character of ``body`` (the text between '!' or '$' and '*')."""
    value = 0
    for ch in body:
        value ^= ord(ch)
    return value


@dataclass
class Sentence:
    talker: str          # e.g. "AIVDM", "BSVDM"
    total: int
    number: int
    seq: str
    channel: str
    payload: str
    fill: int
    tag_time: float | None = None
    own: bool = False    # VDO (own ship) rather than VDM


def parse_tag_block(text: str) -> dict:
    """``s:r003669945,c:1241544035*4A`` -> {"s": "r003669945", "c": "1241544035"} (checksum ignored)."""
    out = {}
    body = text.split("*", 1)[0]
    for part in body.split(","):
        key, sep, value = part.partition(":")
        if sep and key:
            out[key.strip()] = value.strip()
    return out


def parse_sentence(line: str, *, verify: bool = True) -> Sentence | None:
    """One NMEA line (optionally with a ``\\tag block\\`` prefix and USCG trailing fields)."""
    line = line.strip()
    tag_time = None
    if line.startswith("\\"):
        end = line.find("\\", 1)
        if end < 0:
            return None
        tags = parse_tag_block(line[1:end])
        line = line[end + 1:]
        raw_c = tags.get("c")
        if raw_c:
            try:
                c = float(raw_c)
                tag_time = c / 1000.0 if c > 1e11 else c
            except ValueError:
                tag_time = None
    if not line or line[0] not in "!$":
        return None
    star = line.find("*")
    if star < 0:
        return None
    body = line[1:star]
    if verify:
        try:
            given = int(line[star + 1:star + 3], 16)
        except ValueError:
            return None
        if given != nmea_checksum(body):
            return None
    parts = body.split(",")
    if len(parts) < 7:
        return None
    talker = parts[0]
    if len(talker) != 5 or talker[2:] not in ("VDM", "VDO"):
        return None
    try:
        total = int(parts[1])
        number = int(parts[2])
        fill = int(parts[6] or 0)
    except ValueError:
        return None
    if not (1 <= number <= total <= 9) or not (0 <= fill <= 5):
        return None
    return Sentence(talker, total, number, parts[3], parts[4], parts[5], fill, tag_time, talker[2:] == "VDO")


# ---------------------------------------------------------------------------------------------
# Payload armour and bit fields
# ---------------------------------------------------------------------------------------------
class Bits:
    """The payload as one big integer, with field accessors by bit offset."""

    __slots__ = ("value", "n")

    def __init__(self, payload: str, fill: int = 0):
        acc = 0
        for ch in payload:
            v = ord(ch) - 48
            if v > 40:
                v -= 8
            if v < 0 or v > 63:
                raise ValueError(f"bad payload character {ch!r}")
            acc = (acc << 6) | v
        n = 6 * len(payload) - fill
        if fill:
            acc >>= fill
        self.value = acc
        self.n = n

    def uint(self, start: int, length: int) -> int | None:
        if start + length > self.n:
            return None
        return (self.value >> (self.n - start - length)) & ((1 << length) - 1)

    def int(self, start: int, length: int) -> int | None:
        v = self.uint(start, length)
        if v is None:
            return None
        if v & (1 << (length - 1)):
            v -= 1 << length
        return v

    def text(self, start: int, length: int) -> str:
        """Six-bit ASCII. '@' (0) is padding; trailing spaces and padding are stripped."""
        chars = []
        stop = min(start + length, self.n)
        pos = start
        while pos + 6 <= stop:
            v = self.uint(pos, 6)
            chars.append(chr(v + 64) if v < 32 else chr(v))
            pos += 6
        text = "".join(chars)
        cut = text.find("@")
        if cut >= 0:
            text = text[:cut]
        return text.strip()


def _lon(raw: int | None) -> float | None:
    if raw is None or raw == 0x6791AC0:        # 181 degrees: not available
        return None
    lon = raw / 600000.0
    return lon if -180.0 <= lon <= 180.0 else None


def _lat(raw: int | None) -> float | None:
    if raw is None or raw == 0x3412140:        # 91 degrees: not available
        return None
    lat = raw / 600000.0
    return lat if -90.0 <= lat <= 90.0 else None


def _sog(raw: int | None) -> float | None:
    if raw is None or raw == 1023:
        return None
    return raw / 10.0


def _cog(raw: int | None) -> float | None:
    if raw is None or raw >= 3600:
        return None
    return raw / 10.0


def _heading(raw: int | None) -> int | None:
    if raw is None or raw >= 360:
        return None
    return raw


def _rot(raw: int | None) -> float | None:
    """Rate of turn in degrees per minute (None when not available)."""
    if raw is None or raw == -128:
        return None
    if raw in (127, -127):                      # turning faster than 5 degrees per 30 s, no sensor
        return 720.0 if raw > 0 else -720.0
    sign = 1 if raw >= 0 else -1
    return round(sign * (raw / 4.733) ** 2, 1)


def eta_text(month: int | None, day: int | None, hour: int | None, minute: int | None) -> str | None:
    """AIS ETA fields -> "MM-DDTHH:MMZ" (no year), or None when not available."""
    if not month or not day or month > 12 or day > 31:
        return None
    if hour is None or hour >= 24:
        return f"{month:02d}-{day:02d}"
    if minute is None or minute >= 60:
        minute = 0
    return f"{month:02d}-{day:02d}T{hour:02d}:{minute:02d}Z"


def eta_from_packed(value: int | None) -> str | None:
    """Digitraffic packs ETA as month(4) day(5) hour(5) minute(6) bits."""
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return eta_text((v >> 16) & 0xF, (v >> 11) & 0x1F, (v >> 6) & 0x1F, v & 0x3F)


def eta_to_epoch(eta: str | None, now: float | None = None) -> float | None:
    """Resolve a year-less ETA ("MM-DDTHH:MMZ") to the nearest plausible epoch time: within the
    next 10 months, otherwise in the recent past."""
    if not eta:
        return None
    import calendar
    now = time.time() if now is None else now
    try:
        month = int(eta[0:2])
        day = int(eta[3:5])
        hour = int(eta[6:8]) if len(eta) >= 11 else 0
        minute = int(eta[9:11]) if len(eta) >= 11 else 0
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour < 24 and 0 <= minute < 60):
        return None
    year = time.gmtime(now).tm_year
    candidates = []
    for y in (year - 1, year, year + 1):
        if day > calendar.monthrange(y, month)[1]:
            continue
        candidates.append(calendar.timegm((y, month, day, hour, minute, 0)))
    if not candidates:
        return None
    plausible = [t for t in candidates if now - 60 * 86400 <= t <= now + 300 * 86400]
    return float(min(plausible or candidates, key=lambda t: abs(t - now)))


def valid_imo(imo: int | str | None) -> bool:
    """IMO ship numbers are seven digits whose last digit is a weighted checksum of the first six."""
    try:
        s = str(int(imo))
    except (TypeError, ValueError):
        return False
    if len(s) != 7 or s.startswith("0"):
        return False
    total = sum(int(d) * w for d, w in zip(s[:6], (7, 6, 5, 4, 3, 2)))
    return total % 10 == int(s[6])


def clean_text(value) -> str:
    """Normalise an AIS text field: strip '@' padding, collapse spaces, upper case."""
    if value is None:
        return ""
    text = str(value).replace("@", " ")
    text = " ".join(text.split())
    return text.upper()[:60]


# ---------------------------------------------------------------------------------------------
# Message decoding
# ---------------------------------------------------------------------------------------------
def decode_payload(payload: str, fill: int = 0) -> dict | None:
    """Decode one complete AIS payload. Returns a dict with ``type`` and ``mmsi`` plus the fields
    of the supported message types, or None for anything else or a malformed payload."""
    try:
        b = Bits(payload, fill)
    except ValueError:
        return None
    msg_type = b.uint(0, 6)
    mmsi = b.uint(8, 30)
    if msg_type is None or mmsi is None:
        return None
    out: dict = {"type": msg_type, "repeat": b.uint(6, 2), "mmsi": mmsi}
    if msg_type in (1, 2, 3):
        if b.n < 149:
            return None
        out.update({
            "cls": "A",
            "nav_status": b.uint(38, 4),
            "rot": _rot(b.int(42, 8)),
            "sog": _sog(b.uint(50, 10)),
            "accuracy": bool(b.uint(60, 1)),
            "lon": _lon(b.int(61, 28)),
            "lat": _lat(b.int(89, 27)),
            "cog": _cog(b.uint(116, 12)),
            "heading": _heading(b.uint(128, 9)),
            "second": b.uint(137, 6),
            "raim": bool(b.uint(148, 1)),
        })
        return out
    if msg_type == 5:
        if b.n < 420:
            return None
        imo = b.uint(40, 30)
        out.update({
            "cls": "A",
            "ais_version": b.uint(38, 2),
            "imo": imo if imo else None,
            "callsign": b.text(70, 42),
            "name": b.text(112, 120),
            "ship_type": b.uint(232, 8),
            "dim_a": b.uint(240, 9), "dim_b": b.uint(249, 9), "dim_c": b.uint(258, 6), "dim_d": b.uint(264, 6),
            "epfd": b.uint(270, 4),
            "eta_month": b.uint(274, 4), "eta_day": b.uint(278, 5),
            "eta_hour": b.uint(283, 5), "eta_minute": b.uint(288, 6),
            "draught": (b.uint(294, 8) or 0) / 10.0,
            "destination": b.text(302, 120),
            "dte": b.uint(422, 1) if b.n >= 423 else None,
        })
        out["eta"] = eta_text(out["eta_month"], out["eta_day"], out["eta_hour"], out["eta_minute"])
        return out
    if msg_type == 18:
        if b.n < 148:
            return None
        out.update({
            "cls": "B",
            "sog": _sog(b.uint(46, 10)),
            "accuracy": bool(b.uint(56, 1)),
            "lon": _lon(b.int(57, 28)),
            "lat": _lat(b.int(85, 27)),
            "cog": _cog(b.uint(112, 12)),
            "heading": _heading(b.uint(124, 9)),
            "second": b.uint(133, 6),
            "cs_unit": bool(b.uint(141, 1)),
            "raim": bool(b.uint(147, 1)),
        })
        return out
    if msg_type == 19:
        if b.n < 306:
            return None
        out.update({
            "cls": "B",
            "sog": _sog(b.uint(46, 10)),
            "accuracy": bool(b.uint(56, 1)),
            "lon": _lon(b.int(57, 28)),
            "lat": _lat(b.int(85, 27)),
            "cog": _cog(b.uint(112, 12)),
            "heading": _heading(b.uint(124, 9)),
            "second": b.uint(133, 6),
            "name": b.text(143, 120),
            "ship_type": b.uint(263, 8),
            "dim_a": b.uint(271, 9), "dim_b": b.uint(280, 9), "dim_c": b.uint(289, 6), "dim_d": b.uint(295, 6),
            "epfd": b.uint(301, 4),
            "raim": bool(b.uint(305, 1)),
        })
        return out
    if msg_type == 24:
        part = b.uint(38, 2)
        out["cls"] = "B"
        out["part"] = part
        if part == 0:
            if b.n < 160:
                return None
            out["name"] = b.text(40, 120)
            return out
        if part == 1:
            if b.n < 162:
                return None
            out.update({
                "ship_type": b.uint(40, 8),
                "vendor": b.text(48, 18),
                "callsign": b.text(90, 42),
            })
            if 980000000 <= mmsi < 990000000:      # auxiliary craft: mothership MMSI instead of size
                out["mothership"] = b.uint(132, 30)
            else:
                out.update({"dim_a": b.uint(132, 9), "dim_b": b.uint(141, 9),
                            "dim_c": b.uint(150, 6), "dim_d": b.uint(156, 6)})
            return out
        return None
    return None


def dimensions(msg: dict) -> tuple[float | None, float | None]:
    """(length, beam) in metres from the A/B/C/D reference-point distances (None when unknown)."""
    a, b, c, d = (msg.get("dim_a"), msg.get("dim_b"), msg.get("dim_c"), msg.get("dim_d"))
    length = (a or 0) + (b or 0)
    beam = (c or 0) + (d or 0)
    # 511 and 63 are the "more than" markers; anything absurd is treated as unknown.
    return (float(length) if 0 < length < 600 else None, float(beam) if 0 < beam < 80 else None)


# ---------------------------------------------------------------------------------------------
# Multi-sentence assembly
# ---------------------------------------------------------------------------------------------
@dataclass
class _Partial:
    total: int
    parts: list = field(default_factory=list)
    fill: int = 0
    started: float = 0.0
    tag_time: float | None = None


class Assembler:
    """Feed NMEA lines; get decoded messages back. Fragments are grouped by (talker, channel,
    sequence id) so interleaved multi-part messages on channels A and B both assemble."""

    def __init__(self, max_age: float = 10.0):
        self.max_age = max_age
        self._partials: dict[tuple, _Partial] = {}
        self.stats = {"lines": 0, "bad": 0, "decoded": 0, "unsupported": 0}

    def feed(self, line: str, now: float | None = None) -> dict | None:
        self.stats["lines"] += 1
        now = time.time() if now is None else now
        s = parse_sentence(line)
        if s is None:
            if line.strip():
                self.stats["bad"] += 1
            return None
        if s.total == 1:
            return self._decode(s.payload, s.fill, s.tag_time, s.own)
        key = (s.talker, s.channel, s.seq)
        if s.number == 1:
            self._partials[key] = _Partial(s.total, [s.payload], s.fill, now, s.tag_time)
            self._expire(now)
            return None
        part = self._partials.get(key)
        if part is None or part.total != s.total or len(part.parts) != s.number - 1:
            self._partials.pop(key, None)
            self.stats["bad"] += 1
            return None
        part.parts.append(s.payload)
        part.fill = s.fill
        if s.tag_time is not None:
            part.tag_time = s.tag_time
        if s.number < s.total:
            return None
        del self._partials[key]
        return self._decode("".join(part.parts), part.fill, part.tag_time, s.own)

    def _decode(self, payload: str, fill: int, tag_time: float | None, own: bool) -> dict | None:
        msg = decode_payload(payload, fill)
        if msg is None:
            self.stats["unsupported"] += 1
            return None
        self.stats["decoded"] += 1
        if tag_time is not None:
            msg["tag_time"] = tag_time
        if own:
            msg["own"] = True
        return msg

    def _expire(self, now: float) -> None:
        if len(self._partials) < 32:
            return
        for key in [k for k, p in self._partials.items() if now - p.started > self.max_age]:
            del self._partials[key]
