"""Parsers for airspace and airport status sources. Pure functions, no network:

- FAA NAS Status airport events (JSON): ground stops, ground delay programmes, arrival and
  departure delays, closures, airport notices and de-icing, with airport positions.
- FAA temporary flight restrictions: the list (JSON), the polygons (GeoServer WFS GeoJSON) and one
  notice in the XNOTAM XML format (times, altitudes, purpose, text).
- EASA Conflict Zone Information Bulletins: the CSV export of the bulletin list and its RSS feed
  (for the page links).
"""

from __future__ import annotations

import calendar
import csv
import io
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from . import shapes
from .airspace_data import (COUNTRY_ISO2, ICAO_TO_FAA, NONCONUS_ICAO, NOTAM_WORDS, TFR_CATEGORY_LABELS,
                            TFR_RULES, TFR_TYPES)

NAS_SITE = "https://nasstatus.faa.gov/"
TFR_SITE = "https://tfr.faa.gov/"
CZIB_SITE = "https://www.easa.europa.eu/en/domains/air-operations/czibs"
# Bumped when a parser's output changes, so stored copies are fetched again.
TFR_FORMAT = 1
CZIB_FORMAT = 2

_WS = re.compile(r"\s+")
_FAA_ID = re.compile(r"^[A-Z0-9]{3,4}$")
_NOTAM_ID = re.compile(r"^(\d)/(\d{4})$")
_PHONE = re.compile(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b")


def text(value, limit: int = 300) -> str | None:
    if value is None:
        return None
    s = _WS.sub(" ", str(value).replace("\xa0", " ")).strip()
    return s[:limit] if s else None


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def iso_ts(value) -> float | None:
    """'2026-11-23T14:38:08Z', '...T17:00:00.000+00:00' or a bare '2026-11-23T14:00:00' (UTC)."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def notam_ts(value: str) -> float | None:
    """NOTAM validity 'YYMMDDHHMM' (always UTC)."""
    if not value or not re.fullmatch(r"\d{10}", value):
        return None
    try:
        return calendar.timegm(time.strptime("20" + value, "%Y%m%d%H%M"))
    except ValueError:
        return None


def dmy_ts(value, end_of_day: bool = False) -> float | None:
    """'31/01/2027' -> epoch seconds (UTC midnight, or the last second of that day)."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(value or ""))
    if not m:
        return None
    try:
        t = calendar.timegm((int(m.group(3)), int(m.group(2)), int(m.group(1)), 0, 0, 0))
    except (ValueError, OverflowError):
        return None
    return t + 86399 if end_of_day else t


def minutes_of(value) -> int | None:
    """'1 hour and 20 minutes' -> 80, '30 minutes' -> 30, '15' or 15.0 -> 15."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(round(value)) if math.isfinite(value) and 0 <= value < 10000 else None
    s = str(value or "").lower()
    if re.fullmatch(r"\s*\d+(\.\d+)?\s*", s):
        return int(round(float(s)))
    h = re.search(r"(\d+)\s*(?:hours?|hrs?)\b", s)
    m = re.search(r"(\d+)\s*(?:minutes?|mins?)\b", s)
    if not h and not m:
        return None
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def when_text(t: float | None, now: float) -> str | None:
    """'15:30 UTC' today, '27 Jan 23:00 UTC' otherwise."""
    if t is None:
        return None
    a, b = time.gmtime(t), time.gmtime(now)
    if a[:3] == b[:3]:
        return time.strftime("%H:%M UTC", a)
    return f"{a.tm_mday} {time.strftime('%b %H:%M UTC', a)}"


# ---------------------------------------------------------------------------------------------
# airport codes
# ---------------------------------------------------------------------------------------------
def icao_for(faa: str | None) -> str | None:
    if not faa:
        return None
    if faa in NONCONUS_ICAO:
        return NONCONUS_ICAO[faa]
    return "K" + faa if re.fullmatch(r"[A-Z]{3}", faa) else None


def faa_for(ident: str | None) -> str | None:
    """FAA location id for an ICAO code (KSFO, PHNL), an FAA or IATA code (SFO) or None."""
    s = (ident or "").strip().upper()
    if not s or not re.fullmatch(r"[A-Z0-9-]{2,12}", s):
        return None
    if s in ICAO_TO_FAA:
        return ICAO_TO_FAA[s]
    if re.fullmatch(r"K[A-Z0-9]{3}", s):
        return s[1:]
    if re.fullmatch(r"[A-Z0-9]{3}", s):
        return s
    return None


# ---------------------------------------------------------------------------------------------
# FAA NAS Status: airport events
# ---------------------------------------------------------------------------------------------
STATUS_LABELS = {
    "ground_stop": "Ground stop", "ground_delay": "Ground delay programme", "arrival_delay": "Arrival delays",
    "departure_delay": "Departure delays", "closure": "Airport closed", "partial_closure": "Closed to some traffic",
    "notice": "Airport notice", "deicing": "De-icing",
}
# 3 = red (stops and closures), 2 = amber (delays), 1 = blue (information)
STATUS_LEVEL = {"ground_stop": 3, "closure": 3, "ground_delay": 2, "arrival_delay": 2, "departure_delay": 2,
                "partial_closure": 1, "notice": 1, "deicing": 1}
DELAY_CODES = {"ground_stop", "ground_delay", "arrival_delay", "departure_delay", "closure"}
_REASON_PREFIX = {"WX": "Weather", "VOL": "Volume", "RWY": "Runway", "EQ": "Equipment", "TM": "Traffic management",
                  "STAFF": "Staffing", "OTHER": "Other", "SEC": "Security", "NAVAID": "Navigation aid"}
_EXTENSION = {"LOW": "Low", "MEDIUM": "Medium", "HIGH": "High"}


def _words(words: str) -> str:
    """Lower-case words, keeping short acronyms (VIP, MIT) and codes with digits (RWY 27L)."""
    return " ".join(w if (w.isupper() and len(w) <= 4) or any(c.isdigit() for c in w) else w.lower()
                    for w in words.split())


def _sentence(words: str) -> str:
    s = _words(words)
    return s[:1].upper() + s[1:]


def reason_text(raw) -> str | None:
    """'WX:Low Ceilings' -> 'Weather: low ceilings'; 'airport volume' -> 'Airport volume'."""
    s = text(raw, 160)
    if not s:
        return None
    prefix, sep, rest = s.partition(":")
    key = prefix.strip().upper().split(" ")[0]
    if sep and key in _REASON_PREFIX and rest.strip():
        return f"{_REASON_PREFIX[key]}: {_words(rest.strip())}"
    return _sentence(s)


def _trend(raw) -> str | None:
    s = (text(raw, 20) or "").lower()
    return {"increasing": "Increasing", "decreasing": "Decreasing", "steady": "Steady", "stable": "Steady"}.get(s)


def plain_notice(qualifier: str | None) -> str | None:
    """'TO NON SKED TRANSIENT GA ACFT EXC 24HR PPR 702-...' -> 'Closed to non-scheduled transient general
    aviation aircraft, except with 24 hours' prior permission'."""
    q = text(qualifier, 400)
    if not q:
        return None
    q = re.split(r"\bCTC\b", q)[0]
    q = _PHONE.split(q)[0].strip(" ,.")
    words = []
    for w in q.split():
        words.append(NOTAM_WORDS.get(w.upper(), w.lower() if w.isupper() and len(w) > 3 else w))
    s = " ".join(words)
    s = re.sub(r"\bnon scheduled\b", "non-scheduled", s)
    s = re.sub(r"\bexcept (\d+ hours' )?prior permission\b", lambda m: f"except with {m.group(1) or ''}prior permission", s)
    s = re.sub(r"\s+,", ",", s.replace(" except", ", except")).strip()
    if not s.lower().startswith("closed"):
        s = "Closed " + s
    return s[:1].upper() + s[1:]


_PURPOSE_WORDS = {"ACFT": "aircraft", "OPS": "operations", "TFC": "traffic", "OPR": "operating", "MIL": "military",
                  "VCY": "vicinity", "AUTH": "authorised", "FLT": "flight", "HEL": "helicopter"}


def plain_purpose(value) -> str | None:
    """'TO PROVIDE A SAFE ENVIRONMENT FOR FIRE FIGHTING ACFT OPS' -> 'To provide a safe environment for
    fire fighting aircraft operations'. Mixed-case text is kept as written."""
    s = text(value, 300)
    if not s:
        return None
    if re.search(r"[a-z]", s):
        return s
    words = [_PURPOSE_WORDS.get(w, w.lower()) for w in s.split()]
    out = " ".join(words)
    return out[:1].upper() + out[1:]


def _url(value) -> str | None:
    """An http(s) link; FAA advisory links carry raw spaces in their query, which are encoded."""
    s = (text(value, 600) or "").replace(" ", "%20")
    if s and re.match(r"^https?://[^\s<>\"'\\]+$", s):
        return s
    return None


def _facilities(value) -> list[str] | None:
    if not isinstance(value, list):
        return None
    out = [str(v).strip().upper()[:6] for v in value if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9]{3,5}", v.strip())]
    return out[:80] or None


def _status(code: str, **kw) -> dict:
    d = {"code": code, "type": STATUS_LABELS[code], "delay": code in DELAY_CODES, "level": STATUS_LEVEL[code],
         "reason": None, "avg_delay_min": None, "min_delay_min": None, "max_delay_min": None, "trend": None,
         "start": None, "end": None, "updated": None, "advisory": None, "scope": None, "extension": None,
         "text": None, "summary": None}
    d.update({k: v for k, v in kw.items() if v is not None})
    return d


def _summary(st: dict, faa: str, now: float) -> str:
    code = st["code"]
    until = when_text(st.get("end"), now)
    if code == "ground_stop":
        s = f"Flights bound for {faa} are held at their departure airports"
        s += f" until {until}" if until else ""
        if st.get("extension"):
            s += f" (chance of an extension: {st['extension'].lower()})"
        return s + "."
    if code == "ground_delay":
        s = f"Flights bound for {faa} wait on the ground before departure"
        if st.get("avg_delay_min") is not None:
            s += f", {st['avg_delay_min']} min on average"
            if st.get("max_delay_min"):
                s += f" and up to {st['max_delay_min']} min"
        start = when_text(st.get("start"), now)
        if start and until:
            s += f", {start} to {until}"
        elif until:
            s += f", until {until}"
        return s + "."
    if code in ("arrival_delay", "departure_delay"):
        who = "Arrivals" if code == "arrival_delay" else "Departures"
        lo, hi = st.get("min_delay_min"), st.get("max_delay_min")
        if lo is not None and hi is not None:
            s = f"{who} are delayed {lo} to {hi} min"
        elif st.get("avg_delay_min") is not None:
            s = f"{who} are delayed about {st['avg_delay_min']} min"
        else:
            s = f"{who} are delayed"
        if st.get("trend") and st["trend"] != "Steady":
            s += f", {st['trend'].lower()}"
        return s + "."
    if code == "closure":
        return "The airport is closed" + (f" until {until}" if until else "") + "."
    if code == "partial_closure":
        return (st.get("summary") or "Closed to some traffic") + (f" until {until}" if until else "") + "."
    if code == "deicing":
        since = when_text(st.get("start"), now)
        return "Aircraft are de-iced before departure" + (f" (since {since})" if since else "") + "."
    return st.get("summary") or "Airport notice."


def parse_nas_events(data, now: float | None = None) -> list[dict]:
    """FAA NAS Status /api/airport-events -> one record per airport that has a current status."""
    now = time.time() if now is None else now
    out = []
    for a in data if isinstance(data, list) else []:
        if not isinstance(a, dict):
            continue
        faa = (text(a.get("airportId"), 8) or "").upper()
        lat, lon = _num(a.get("latitude")), _num(a.get("longitude"))
        if not _FAA_ID.match(faa) or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        sts = []
        gs = a.get("groundStop")
        if isinstance(gs, dict):
            sts.append(_status("ground_stop", reason=reason_text(gs.get("impactingCondition")),
                               start=iso_ts(gs.get("startTime")), end=iso_ts(gs.get("endTime")),
                               updated=iso_ts(gs.get("updatedAt")), advisory=_url(gs.get("advisoryUrl")),
                               scope=_facilities(gs.get("includedFacilities")),
                               extension=_EXTENSION.get(str(gs.get("probabilityOfExtension") or "").upper())))
        gd = a.get("groundDelay")
        if isinstance(gd, dict):
            sts.append(_status("ground_delay", reason=reason_text(gd.get("impactingCondition")),
                               avg_delay_min=minutes_of(gd.get("avgDelay")), max_delay_min=minutes_of(gd.get("maxDelay")),
                               start=iso_ts(gd.get("startTime")), end=iso_ts(gd.get("endTime")),
                               updated=iso_ts(gd.get("updatedAt")), advisory=_url(gd.get("advisoryUrl")),
                               scope=_facilities(gd.get("includedFacilities"))))
        ac = a.get("airportClosure")
        if isinstance(ac, dict):
            qual = text(ac.get("text"), 400)
            partial = bool(qual and re.search(r"NON SKED|TRANSIENT|PPR|EXC\b", qual))
            sts.append(_status("partial_closure" if partial else "closure", text=qual,
                               summary=plain_notice(qual) if partial else None,
                               start=iso_ts(ac.get("startTime")), end=iso_ts(ac.get("endTime")),
                               updated=iso_ts(ac.get("updatedAt"))))
        ff = a.get("freeForm")
        if isinstance(ff, dict):
            full = text(ff.get("simpleText"), 600) or ""
            qual = text(ff.get("text"), 400)
            if re.search(r"\bCLSD\b", full):
                code = "partial_closure" if qual else "closure"
            else:
                code = "notice"
            sts.append(_status(code, text=full or qual, summary=plain_notice(qual) if code != "closure" and qual else None,
                               start=iso_ts(ff.get("startTime")), end=iso_ts(ff.get("endTime")),
                               updated=iso_ts(ff.get("updatedAt") or ff.get("issuedDate"))))
        for key, code in (("arrivalDelay", "arrival_delay"), ("departureDelay", "departure_delay")):
            d = a.get(key)
            if not isinstance(d, dict):
                continue
            ad = d.get("arrivalDeparture") if isinstance(d.get("arrivalDeparture"), dict) else {}
            sts.append(_status(code, reason=reason_text(d.get("reason")), avg_delay_min=minutes_of(d.get("averageDelay")),
                               min_delay_min=minutes_of(ad.get("min")), max_delay_min=minutes_of(ad.get("max")),
                               trend=_trend(ad.get("trend") or d.get("trend")), updated=iso_ts(d.get("updateTime"))))
        di = a.get("deicing")
        if isinstance(di, dict):
            sts.append(_status("deicing", start=iso_ts(di.get("eventTime")), end=iso_ts(di.get("expTime")),
                               updated=iso_ts(di.get("updatedAt") or di.get("createdAt"))))
        if not sts:
            continue
        for st in sts:
            st["summary"] = _summary(st, faa, now)
        sts.sort(key=lambda s: (-s["level"], -(s.get("max_delay_min") or s.get("avg_delay_min") or 0)))
        cfg = a.get("airportConfig") if isinstance(a.get("airportConfig"), dict) else {}
        rate = cfg.get("arrivalRate")
        out.append({
            "faa": faa, "icao": icao_for(faa), "name": text(a.get("airportLongName"), 100) or faa,
            "lat": round(lat, 5), "lon": round(lon, 5), "statuses": sts,
            "runways": {"arrival": text(cfg.get("arrivalRunwayConfig"), 40),
                        "departure": text(cfg.get("departureRunwayConfig"), 40),
                        "arrival_rate": int(rate) if isinstance(rate, (int, float)) and 0 < rate < 400 else None},
        })
    return out


# ---------------------------------------------------------------------------------------------
# FAA temporary flight restrictions
# ---------------------------------------------------------------------------------------------
def tfr_category(type_text: str | None, rule: str | None = None) -> tuple[str, str]:
    t = (type_text or "").strip().upper()
    if t in TFR_TYPES:
        return TFR_TYPES[t]
    r = rule or ""
    if r.startswith("91.141"):
        return "vip", "VIP movement"
    if r.startswith("91.143"):
        return "space", "Space operations"
    if r.startswith("91.145"):
        return "sports", "Air show or sporting event"
    if r.startswith("91.137") or r.startswith("91.138") or r.startswith("91.139"):
        return "hazards", "Hazard"
    if r.startswith("99.7"):
        return "security", "Security"
    if r.startswith("44812"):
        return "drones", "Drone restriction for a public gathering"
    return "other", (t.title() if t else TFR_CATEGORY_LABELS["other"])


def rule_text(rule: str | None) -> str | None:
    r = (rule or "").strip()
    for key, label in TFR_RULES:
        if r.startswith(key):
            return label
    return f"Section {r}" if r else None


def nid_of(notam_id: str | None) -> str | None:
    m = _NOTAM_ID.match((notam_id or "").strip())
    return f"{m.group(1)}_{m.group(2)}" if m else None


def notam_of(nid: str | None) -> str | None:
    m = re.fullmatch(r"(\d)_(\d{4})", (nid or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def tfr_link(notam_id: str) -> str:
    nid = nid_of(notam_id) or ""
    return f"https://tfr.faa.gov/tfr3/?page=detail_{nid}"


def parse_tfr_list(data) -> dict[str, dict]:
    """tfr.faa.gov exportTfrList -> {notam id: record}."""
    out: dict[str, dict] = {}
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict):
            continue
        nid = (text(r.get("notam_id"), 10) or "")
        if not _NOTAM_ID.match(nid):
            continue
        typ = (text(r.get("type"), 40) or "").upper()
        cat, label = tfr_category(typ)
        created = None
        m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", text(r.get("creation_date"), 12) or "")
        if m:
            created = calendar.timegm((int(m.group(3)), int(m.group(1)), int(m.group(2)), 0, 0, 0))
        out[nid] = {"id": nid, "type": typ or None, "category": cat, "type_label": label,
                    "facility": text(r.get("facility"), 8), "state": text(r.get("state"), 6),
                    "description": text(r.get("description"), 300), "created": created}
    return out


def parse_tfr_wfs(fc) -> dict[str, dict]:
    """GeoServer V_TFR_LOC GeoJSON -> {notam id: {"polygons": [...], "modified", "title", "legal", "state"}}."""
    out: dict[str, dict] = {}
    for f in (fc or {}).get("features") or [] if isinstance(fc, dict) else []:
        if not isinstance(f, dict):
            continue
        p = f.get("properties") or {}
        key = text(p.get("NOTAM_KEY"), 40) or ""
        nid = key.split("-")[0] if key else str(f.get("id") or "").split(".")[-1]
        if not _NOTAM_ID.match(nid):
            continue
        geom = shapes.clean_geometry(f.get("geometry"), 5)
        if geom is None:
            continue
        rec = out.setdefault(nid, {"polygons": [], "modified": None, "title": None, "legal": None, "state": None})
        rec["polygons"].extend(shapes.polygons_of(geom))
        mod = text(p.get("LAST_MODIFICATION_DATETIME"), 14) or ""
        if re.fullmatch(r"\d{12}", mod):
            rec["modified"] = mod
        rec["title"] = rec["title"] or text(p.get("TITLE"), 200)
        rec["legal"] = rec["legal"] or (text(p.get("LEGAL"), 40) or "").upper() or None
        rec["state"] = rec["state"] or text(p.get("STATE"), 6)
    return out


def _safe_xml(raw: bytes | str) -> ET.Element:
    s = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    s = s.lstrip("\ufeff \t\r\n")
    if re.search(r"<!(DOCTYPE|ENTITY)", s[:4000], re.I):
        raise ValueError("XML with a document type declaration is not accepted")
    return ET.fromstring(s)


def _find(el, path: str) -> str | None:
    node = el.find(path) if el is not None else None
    return text(node.text, 4000) if node is not None and node.text else None


def _altitude(code: str | None, value: str | None, uom: str | None) -> tuple[str | None, int | None]:
    """('ALT', '17999', 'FT') -> ('17,999 ft MSL', 17999); HEI = above ground; STD = flight level."""
    v = _num(value)
    if v is None:
        return None, None
    code = (code or "").upper()
    uom = (uom or "FT").upper()
    if uom == "FL" or code == "STD":
        fl = int(v if uom == "FL" else v / 100)
        return f"FL{fl:03d}", fl * 100
    ft = int(v * 3.28084) if uom == "M" else int(v)
    if ft <= 0:
        return "Surface", 0
    return f"{ft:,} ft {'above ground' if code == 'HEI' else 'MSL'}", ft


def parse_tfr_detail(raw: bytes | str) -> dict:
    """One XNOTAM notice -> times (UTC), purpose, place, areas with altitudes and schedules, text."""
    root = _safe_xml(raw)
    n = root.find(".//Not")
    if n is None:
        raise ValueError("no notice in the XML")
    issued = iso_ts(_find(n, "NotUid/dateIssued"))
    start = iso_ts(_find(n, "dateEffective"))
    end = iso_ts(_find(n, "dateExpire"))
    usns = _find(n, "txtDescrUSNS") or ""
    trad = _find(n, "txtDescrTraditional") or ""
    perm = False
    m = re.search(r"\b(\d{10})-(\d{10}|PERM)\b", usns or trad)
    if m:
        start = start or notam_ts(m.group(1))
        if m.group(2) == "PERM":
            perm = True
        else:
            end = end or notam_ts(m.group(2))
    rule = _find(n, "TfrNot/codeType")
    areas = []
    for a in n.iter("aseTFRArea"):
        upper, upper_ft = _altitude(_find(a, "codeDistVerUpper"), _find(a, "valDistVerUpper"), _find(a, "uomDistVerUpper"))
        lower, lower_ft = _altitude(_find(a, "codeDistVerLower"), _find(a, "valDistVerLower"), _find(a, "uomDistVerLower"))
        if upper_ft == 0 and lower_ft == 0:
            upper = lower = None            # notices that list locations elsewhere carry 0-0
        sched = None
        sg = a.find("ScheduleGroup")
        if sg is not None and (_find(sg, "isTimeSeparate") or "").upper() == "TRUE":
            t0, t1 = iso_ts(_find(sg, "startTime")), iso_ts(_find(sg, "endTime"))
            if t0 is not None and t1 is not None:
                day = (_find(a, "dayCode") or "Daily").strip().capitalize()
                sched = f"{day}, {time.strftime('%H:%M', time.gmtime(t0))} to {time.strftime('%H:%M', time.gmtime(t1))} UTC"
        areas.append({"name": text(_find(a, "txtName"), 60), "lower": lower, "upper": upper, "lower_ft": lower_ft,
                      "upper_ft": upper_ft, "schedule": sched})
    city = _find(n, "AffLocGroup/txtNameCity")
    state = _find(n, "AffLocGroup/txtNameUSState")
    return {
        "v": TFR_FORMAT, "issued": issued, "start": start, "end": end, "permanent": perm and end is None,
        "time_zone": text(_find(n, "codeTimeZone"), 8), "rule": text(rule, 20), "rule_text": rule_text(rule),
        "purpose": text(_find(n, "txtDescrPurpose"), 300), "city": text(city, 120), "state_name": text(state, 40),
        "facility": text(_find(n, "codeFacility"), 8), "coord_facility": text(_find(n, "txtNameCoordFacility"), 60),
        "areas": areas[:12], "text": text(trad or usns, 6000),
    }


# ---------------------------------------------------------------------------------------------
# EASA Conflict Zone Information Bulletins
# ---------------------------------------------------------------------------------------------
_CZIB_NUM = re.compile(r"(\d{4})\s*[-_ ]\s*(\d{2})")
_REV = re.compile(r"R\s*(\d{1,3})\s*$", re.I)
_AVOID = re.compile(r"\b(?:should\s+not|not\s+to|do\s+not|recommends\s+not\s+to|not)\s+(?:operate|fly)\b", re.I)
_CAUTION = re.compile(r"exercise\s+caution|take\s+(?:the\s+)?(?:potential\s+)?risks?\s+into\s+account|with\s+caution", re.I)
# Words that say a bulletin covers only part of the airspace it names. In the affected-airspace text
# any of them counts; in the recommendation only phrases about area (not "monitor the region").
_PARTIAL_AREA = re.compile(r"\b(portions?|parts? of|west of|east of|north of|south of|except|provinces?|region|"
                           r"governorate|over the waters|within \d+\s*NM)\b", re.I)
_PARTIAL_RECO = re.compile(r"\b(portions? of|west of the line|east of the line|north of|south of|over the waters|"
                           r"remainder of|maritime part)\b", re.I)
_TERRITORY = re.compile(r"\b(within|over) the territory\b", re.I)
_BELOW_FL = re.compile(r"(?:at or )?below\s+(?:flight\s+level|FL)\s*\(?F?L?\)?\s*(\d{2,3})", re.I)
_ALL_LEVELS = re.compile(r"all\s+(?:altitudes|flight\s+levels)", re.I)
_LON_CLIP = re.compile(r"\b(west|east)\s+of\s+(?:longitude\s+)?(\d{1,3}(?:\.\d+)?)\s*(?:°|º|deg(?:rees)?)?\s*(east|west|E|W)\b", re.I)


def clean_easa(value, limit: int = 4000) -> str | None:
    """EASA's export carries replacement characters where dashes, degree signs and apostrophes were."""
    if value is None:
        return None
    s = str(value).replace("\xa0", " ").replace("\r", "")
    s = re.sub("(\\d)\\s?\ufffd", "\\1\u00b0", s)
    s = re.sub("(\\w)\ufffd(\\w)", "\\1\u2019\\2", s)
    s = s.replace("\ufffd", "\u2013")
    lines = [_WS.sub(" ", ln).strip() for ln in s.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    s = "\n".join(out).strip()
    return s[:limit] if s else None


def applies_to(value: str | None) -> str | None:
    """'Applies to operators\\nEUROCONTROL Network Manager' -> 'Operators, EUROCONTROL Network Manager'."""
    if not value:
        return None
    parts = [re.sub(r"^applies to\s+", "", p.strip(), flags=re.I) for p in value.split("\n") if p.strip()]
    parts = [p[:1].upper() + p[1:] for p in parts if p]
    return ", ".join(parts) or None


def czib_base_id(number: str | None) -> str | None:
    m = _CZIB_NUM.search(number or "")
    return f"czib-{m.group(1)}-{m.group(2)}" if m else None


def _norm_number(s: str) -> str:
    s = (s or "").lower().replace("czib", "").replace("sib", "")
    return re.sub(r"[^a-z0-9]", "", s)


def recommendation_level(reco: str | None) -> str:
    s = reco or ""
    if _AVOID.search(s):
        return "avoid"
    if _CAUTION.search(s):
        return "caution"
    return "info"


def altitude_limit(reco: str | None, airspace: str | None) -> str | None:
    for s in (reco or "", airspace or ""):
        limits = sorted({int(x) for x in _BELOW_FL.findall(s)})
        every = bool(_ALL_LEVELS.search(s))
        if limits and every:
            return "Varies by area"
        if limits:
            return f"Below FL{limits[0]:03d}"
        if every:
            return "All altitudes"
    return None


def lon_clip(airspace: str | None) -> tuple[float, bool] | None:
    """'west of longitude 60° East' -> (60.0, True): keep the part west of 60°E."""
    m = _LON_CLIP.search(airspace or "")
    if not m:
        return None
    v = float(m.group(2))
    if m.group(3).lower().startswith("w"):
        v = -v
    if not -180 <= v <= 180:
        return None
    return v, m.group(1).lower() == "west"


def fir_codes(airspace: str | None, known=None) -> list[str]:
    """ICAO codes the bulletin gives in brackets: 'FIR LVIV (UKLV)', '(Bahrain FIR – OBBB)'."""
    out = []
    for group in re.findall(r"\(([^()]{4,60})\)", airspace or ""):
        for code in re.findall(r"\b([A-Z]{4})\b", group):
            if code not in out and (known is None or code in known):
                out.append(code)
    return out


def is_partial(airspace: str | None, reco: str | None) -> bool:
    area = _LON_CLIP.sub(" ", airspace or "")      # a meridian limit is drawn exactly, so it is not partial
    return bool(_PARTIAL_AREA.search(area) or _PARTIAL_RECO.search(reco or ""))


def country_codes(names: str | None) -> list[tuple[str, str]]:
    out = []
    for part in re.split(r",|;|\band\b", names or ""):
        name = part.strip().strip(".")
        iso = COUNTRY_ISO2.get(name.lower())
        if iso and iso not in [c for c, _ in out]:
            out.append((iso, name))
    return out


def parse_czib_csv(raw: bytes | str) -> list[dict]:
    """EASA CZIB list export -> bulletins (active and withdrawn)."""
    s = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    out = []
    for r in csv.DictReader(io.StringIO(s)):
        number = clean_easa(r.get("CZIB number"), 40)
        base = czib_base_id(number)
        subject = clean_easa(r.get("Subject"), 160)
        if not base or not subject:
            continue
        rev = _REV.search(number or "")
        airspace = clean_easa(r.get("Affected Airspace"), 1500)
        reco = clean_easa(r.get("Recommendation(s)"), 4000)
        status = (clean_easa(r.get("Status"), 30) or "").capitalize()
        revision = clean_easa(r.get("Revision"), 600)
        out.append({
            "id": base, "number": number, "revision": int(rev.group(1)) if rev else 0, "subject": subject,
            "status": status, "active": status == "Active",
            "issued": dmy_ts(r.get("Issue date")), "valid_until": dmy_ts(r.get("Valid until"), end_of_day=True),
            "valid_text": clean_easa(r.get("Valid until description"), 200),
            "revised": dmy_ts(revision) if revision else None, "revision_text": revision,
            "airspace": airspace, "countries_text": clean_easa(r.get("Affected Country"), 300),
            "countries": [{"iso2": c, "name": n} for c, n in country_codes(clean_easa(r.get("Affected Country"), 300))],
            "applicability": applies_to(clean_easa(r.get("Applicability"), 200)),
            "description": clean_easa(r.get("Description"), 6000), "recommendation": reco,
            "references": clean_easa(r.get("Referenced publication(s)"), 1200),
            "level": recommendation_level(reco), "altitudes": altitude_limit(reco, airspace),
            "partial": is_partial(airspace, reco),
            "territory": bool(_TERRITORY.search(airspace or "") or _TERRITORY.search(reco or "")),
            "clip": lon_clip(airspace),
            "content_changed": not bool(revision and re.search(r"no changes? (?:were made )?to the content", revision, re.I)),
        })
    return out


def parse_rss_links(raw: bytes | str) -> list[dict]:
    """EASA's CZIB RSS feed -> [{"title", "link", "key"}] (key = the bulletin number from the URL)."""
    root = _safe_xml(raw)
    out = []
    for item in root.iter("item"):
        title = text(item.findtext("title"), 200)
        link = text(item.findtext("link"), 400)
        if not link or not re.match(r"^https://www\.easa\.europa\.eu/[\w/.-]+$", link):
            continue
        slug = link.rstrip("/").rsplit("/", 1)[-1]
        out.append({"title": title, "link": link, "key": _norm_number(slug)})
    return out


def match_link(bulletin: dict, links: list[dict]) -> str | None:
    key = _norm_number(bulletin.get("number") or "")
    for item in links:
        if key and item["key"] == key:
            return item["link"]
    base = _norm_number((bulletin.get("id") or "").replace("czib-", ""))
    cands = [i for i in links if i["key"].startswith(base) and i.get("title") == bulletin.get("subject")]
    return cands[0]["link"] if cands else None
