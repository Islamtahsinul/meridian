"""Parsers for provider payloads. Everything downloaded is untrusted: every field is type-checked,
range-checked and length-limited before it reaches the tracker.

An *observation* is a plain dict with (all optional except ``hex``)::

    hex, lat, lon, pos_t (epoch s of the position), seen_t (epoch s of the last message),
    alt (ft, 0 on the ground), gnd (bool), alt_geom, gs (kt), trk (deg), vr (ft/min),
    cs (callsign), sq (squawk), emg (emergency status), cat (A0-C7), reg, typ, desc, ownop, year,
    db_flags, nac_p, nic, version, ptype (position source), gps_ok_before
"""

from __future__ import annotations

import math
import re
from typing import Any

HEX_RE = re.compile(r"^[0-9a-f]{6}$")
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,8}$")
SQUAWK_RE = re.compile(r"^[0-7]{4}$")
CATEGORY_RE = re.compile(r"^[A-D][0-7]$")
TYPE_RE = re.compile(r"^[A-Z0-9]{1,5}$")
REG_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,11}$")
PTYPE_RE = re.compile(r"^[a-z_]{2,20}$")
EMERGENCIES = {"none", "general", "lifeguard", "minfuel", "nordo", "unlawful", "downed", "reserved"}
_MAX_ROWS = 25000

M_TO_FT = 3.28084
MS_TO_KT = 1.943844
MS_TO_FPM = 196.850394


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str) and len(value) < 32:
        try:
            f = float(value)
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _int(value: Any) -> int | None:
    f = _num(value)
    return int(round(f)) if f is not None and abs(f) < 1e9 else None


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] if value else None


def _latlon(lat: Any, lon: Any) -> tuple[float, float] | None:
    la, lo = _num(lat), _num(lon)
    if la is None or lo is None or not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        return None
    if la == 0.0 and lo == 0.0:
        return None
    return la, lo


def clean_callsign(value: Any) -> str | None:
    text = _text(value, 12)
    if not text:
        return None
    text = text.replace(" ", "").upper()
    return text if CALLSIGN_RE.match(text) else None


def clean_hex(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if HEX_RE.match(text) else None


def clean_reg(value: Any) -> str | None:
    text = _text(value, 14)
    if not text:
        return None
    text = text.upper().replace(" ", "")
    return text if REG_RE.match(text) else None


def epoch_seconds(value: Any) -> float | None:
    """readsb ``now`` is seconds (aircraft.json, adsb.fi v2) or milliseconds (v2 API, adsb.fi v3)."""
    f = _num(value)
    if f is None or f <= 0:
        return None
    return f / 1000.0 if f > 1e11 else f


# ---------------------------------------------------------------------------------------------
# readsb JSON (adsb.fi, adsb.lol, tar1090/readsb aircraft.json)
# ---------------------------------------------------------------------------------------------
def parse_readsb(payload: Any, *, received: float | None = None) -> tuple[float, list[dict]]:
    """(``now`` in epoch seconds, observations). Accepts the ``ac`` and ``aircraft`` list forms."""
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    now = epoch_seconds(payload.get("now")) or epoch_seconds(payload.get("ctime")) or received
    if now is None:
        raise ValueError("no timestamp in the answer")
    rows = payload.get("ac")
    if not isinstance(rows, list):
        rows = payload.get("aircraft")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValueError("aircraft list missing")
    out = []
    for raw in rows[:_MAX_ROWS]:
        obs = normalise_readsb(raw, now)
        if obs is not None:
            out.append(obs)
    return now, out


def normalise_readsb(raw: Any, now: float) -> dict | None:
    if not isinstance(raw, dict):
        return None
    hexid = clean_hex(raw.get("hex"))   # "~" (non-ICAO) addresses are skipped
    if hexid is None:
        return None
    obs: dict[str, Any] = {"hex": hexid}
    seen = _num(raw.get("seen"))
    seen_pos = _num(raw.get("seen_pos"))
    pos = _latlon(raw.get("lat"), raw.get("lon"))
    if pos is None:
        last = raw.get("lastPosition")
        if isinstance(last, dict):
            pos = _latlon(last.get("lat"), last.get("lon"))
            seen_pos = _num(last.get("seen_pos"))
            if pos is not None:
                obs["stale_pos"] = True
    if seen is not None and 0 <= seen < 3600:
        obs["seen_t"] = now - seen
    else:
        obs["seen_t"] = now
    if pos is not None:
        age = seen_pos if seen_pos is not None and 0 <= seen_pos < 86400 else (seen or 0.0)
        obs["lat"], obs["lon"] = pos
        obs["pos_t"] = now - max(0.0, age)

    alt = raw.get("alt_baro")
    if alt == "ground":
        obs["gnd"] = True
        obs["alt"] = 0
    else:
        a = _int(alt)
        if a is not None and -2000 <= a <= 90000:
            obs["alt"] = a
            obs["gnd"] = False
    ag = _int(raw.get("alt_geom"))
    if ag is not None and -2000 <= ag <= 90000:
        obs["alt_geom"] = ag
        if "alt" not in obs:
            obs["alt"] = ag
            obs["gnd"] = False
    gs = _num(raw.get("gs"))
    if gs is not None and 0 <= gs < 2500:
        obs["gs"] = gs
    trk = _num(raw.get("track"))
    if trk is None:
        trk = _num(raw.get("true_heading"))
    if trk is None:
        trk = _num(raw.get("mag_heading"))
    if trk is not None and -360 <= trk <= 720:
        obs["trk"] = trk % 360.0
    vr = _int(raw.get("baro_rate"))
    if vr is None:
        vr = _int(raw.get("geom_rate"))
    if vr is not None and abs(vr) < 40000:
        obs["vr"] = vr

    cs = clean_callsign(raw.get("flight"))
    if cs:
        obs["cs"] = cs
    sq = raw.get("squawk")
    if isinstance(sq, (str, int)) and SQUAWK_RE.match(str(sq).zfill(4)):
        obs["sq"] = str(sq).zfill(4)
    emg = raw.get("emergency")
    if isinstance(emg, str) and emg.lower() in EMERGENCIES:
        obs["emg"] = emg.lower()
    cat = raw.get("category")
    if isinstance(cat, str) and CATEGORY_RE.match(cat.upper()):
        obs["cat"] = cat.upper()
    reg = clean_reg(raw.get("r"))
    if reg:
        obs["reg"] = reg
    typ = _text(raw.get("t"), 8)
    if typ and TYPE_RE.match(typ.upper()):
        obs["typ"] = typ.upper()
    desc = _text(raw.get("desc"), 80)
    if desc:
        obs["desc"] = desc
    ownop = _text(raw.get("ownOp"), 120)
    if ownop:
        obs["ownop"] = ownop
    year = raw.get("year")
    if isinstance(year, (str, int)) and re.fullmatch(r"(19|20)\d\d", str(year)):
        obs["year"] = str(year)
    flags = _int(raw.get("dbFlags"))
    if flags is not None and 0 <= flags < 1024:
        obs["db_flags"] = flags
    for key, field in (("nac_p", "nac_p"), ("nic", "nic"), ("version", "version")):
        v = _int(raw.get(field))
        if v is not None and 0 <= v <= 15:
            obs[key] = v
    ptype = raw.get("type")
    if isinstance(ptype, str) and PTYPE_RE.match(ptype):
        obs["ptype"] = ptype
    gob = _num(raw.get("gpsOkBefore"))
    if gob is not None and gob > 1e9:
        obs["gps_ok_before"] = gob
    return obs


# ---------------------------------------------------------------------------------------------
# OpenSky state vectors
# ---------------------------------------------------------------------------------------------
_OS_CATEGORY = {2: "A1", 3: "A2", 4: "A3", 5: "A4", 6: "A5", 7: "A6", 8: "A7", 9: "B1", 10: "B2", 11: "B3",
                12: "B4", 14: "B6", 15: "B7", 16: "C1", 17: "C2", 18: "C3", 19: "C4", 20: "C5"}
_OS_SOURCE = {0: "opensky_adsb", 1: "opensky_asterix", 2: "opensky_mlat", 3: "opensky_flarm"}


def parse_opensky(payload: Any) -> tuple[float, list[dict]]:
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    now = epoch_seconds(payload.get("time"))
    if now is None:
        raise ValueError("no timestamp in the answer")
    states = payload.get("states") or []
    if not isinstance(states, list):
        raise ValueError("states missing")
    out = []
    for s in states[:_MAX_ROWS]:
        if not isinstance(s, list) or len(s) < 17:
            continue
        hexid = clean_hex(s[0])
        if hexid is None:
            continue
        obs: dict[str, Any] = {"hex": hexid, "ptype": _OS_SOURCE.get(s[16] if isinstance(s[16], int) else -1, "other")}
        last_contact = epoch_seconds(s[4])
        obs["seen_t"] = last_contact or now
        pos = _latlon(s[6], s[5])
        tpos = epoch_seconds(s[3])
        if pos is not None and tpos is not None:
            obs["lat"], obs["lon"] = pos
            obs["pos_t"] = min(tpos, now)
        cs = clean_callsign(s[1])
        if cs:
            obs["cs"] = cs
        if s[8] is True:
            obs["gnd"] = True
            obs["alt"] = 0
        else:
            alt = _num(s[7])
            if alt is None:
                alt = _num(s[13]) if len(s) > 13 else None
            if alt is not None and -600 < alt < 27000:
                obs["alt"] = int(round(alt * M_TO_FT))
                obs["gnd"] = False
        gs = _num(s[9])
        if gs is not None and 0 <= gs < 1300:
            obs["gs"] = gs * MS_TO_KT
        trk = _num(s[10])
        if trk is not None:
            obs["trk"] = trk % 360.0
        vr = _num(s[11])
        if vr is not None and abs(vr) < 200:
            obs["vr"] = int(round(vr * MS_TO_FPM))
        sq = s[14]
        if isinstance(sq, str) and SQUAWK_RE.match(sq):
            obs["sq"] = sq
        if len(s) > 17 and isinstance(s[17], int) and s[17] in _OS_CATEGORY:
            obs["cat"] = _OS_CATEGORY[s[17]]
        out.append(obs)
    return now, out


# ---------------------------------------------------------------------------------------------
# readsb trace (adsb.lol /1/trace/recent/{hex})
# ---------------------------------------------------------------------------------------------
def parse_trace(payload: Any) -> list[tuple[float, float, float, int | None]]:
    """[(epoch s, lat, lon, alt ft or None)] oldest first."""
    if not isinstance(payload, dict):
        return []
    base = epoch_seconds(payload.get("timestamp"))
    rows = payload.get("trace")
    if base is None or not isinstance(rows, list):
        return []
    out = []
    for row in rows[:20000]:
        if not isinstance(row, list) or len(row) < 4:
            continue
        dt = _num(row[0])
        pos = _latlon(row[1], row[2])
        if dt is None or pos is None:
            continue
        alt = 0 if row[3] == "ground" else _int(row[3])
        out.append((base + dt, pos[0], pos[1], alt))
    out.sort(key=lambda p: p[0])
    return out
