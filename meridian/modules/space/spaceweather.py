"""Space weather from NOAA SWPC (keyless, public domain): Kp now, observed and forecast, the NOAA G, S
and R scales, GOES X-ray flares, solar wind at L1, SWPC alerts, watches and warnings in plain words,
the OVATION aurora forecast (tiles, viewing lines, "aurora chance here tonight"), an events provider
and the `space_weather_now` AI tool.

Politeness: SWPC updates most products every 1 to 5 minutes. Feeds are fetched only while the browser
uses the space weather panel (within 5 minutes) or the aurora layer and "What's here" (within 10
minutes). The events provider refreshes alerts, scales, flares and Kp on demand when they are more
than 30 minutes old, so storms and flares reach the event history without anyone looking.
"""

from __future__ import annotations

import math
import re
import threading
import time
from datetime import datetime, timezone

from meridian.registry import HTTPError, Response

from . import aurora as A
from . import sgp4 as S

SWPC = "https://services.swpc.noaa.gov"
SITE = "https://www.spaceweather.gov"
PAGES = {
    "overview": f"{SITE}/",
    "aurora": f"{SITE}/products/aurora-30-minute-forecast",
    "kp": f"{SITE}/products/planetary-k-index",
    "forecast": f"{SITE}/products/3-day-forecast",
    "xray": f"{SITE}/products/goes-x-ray-flux",
    "protons": f"{SITE}/products/goes-proton-flux",
    "wind": f"{SITE}/products/solar-wind",
    "alerts": f"{SITE}/products/alerts-watches-and-warnings",
    "scales": f"{SITE}/noaa-scales-explanation",
    "tips": f"{SITE}/content/tips-viewing-aurora",
    "storms": f"{SITE}/phenomena/geomagnetic-storms",
    "flares": f"{SITE}/phenomena/solar-flares-radio-blackouts",
    "radiation": f"{SITE}/phenomena/solar-radiation-storm",
}
LINKS = [
    {"text": "SWPC space weather overview", "href": PAGES["overview"]},
    {"text": "Aurora 30-minute forecast", "href": PAGES["aurora"]},
    {"text": "Planetary K-index", "href": PAGES["kp"]},
    {"text": "3-day forecast", "href": PAGES["forecast"]},
    {"text": "GOES X-ray flux", "href": PAGES["xray"]},
    {"text": "Real-time solar wind", "href": PAGES["wind"]},
    {"text": "Alerts, watches and warnings", "href": PAGES["alerts"]},
    {"text": "NOAA space weather scales", "href": PAGES["scales"]},
]
CREDIT = "NOAA Space Weather Prediction Center (public domain)"
PANEL_KEY = "space.spaceweather"      # activity key: the space weather panel
AURORA_KEY = "space.aurora"           # activity key: the aurora layer and "What's here"
PANEL_ACTIVE_S = 300
AURORA_ACTIVE_S = 600
IDLE_MAX_AGE = 1800                    # events provider: refresh when older than this

G_NAME = {1: "minor", 2: "moderate", 3: "strong", 4: "severe", 5: "extreme"}
S_NAME = G_NAME
R_NAME = G_NAME
_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                                       "Dec"), start=1)}
_EVENT_ID = re.compile(r"^swpc:[a-z]{3,12}:[0-9A-Za-z.T-]{1,24}$")


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def _num(v, nd: int | None = None) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return round(x, nd) if nd is not None else x


def swpc_time(text) -> float | None:
    """'2026 Nov 19 0118 UTC' (the time format inside SWPC messages) -> epoch seconds."""
    m = re.search(r"(\d{4})\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2})(\d{2})", str(text or ""))
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return datetime(int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)), int(m.group(4)), int(m.group(5)),
                        tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def iso(t: float | None) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hm(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M")


def day_label(t: float) -> str:
    d = datetime.fromtimestamp(t, tz=timezone.utc)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def g_of_kp(kp: float | None) -> int:
    """NOAA G scale from Kp (thirds): G1 = 5, G2 = 6, G3 = 7, G4 = 8 including 9-, G5 = 9."""
    if kp is None or kp < 4.5:
        return 0
    if kp >= 8.84:
        return 5
    if kp >= 7.5:
        return 4
    return max(1, min(5, int(math.floor(kp + 0.5)) - 4))


def kp_text(kp: float | None) -> str:
    return A.kp_text(kp)


def _rows_of(data, keys: tuple[str, ...]) -> list[dict]:
    """SWPC JSON comes as a list of objects (current) or a list of lists with a header row (older)."""
    if not isinstance(data, list) or not data:
        return []
    if isinstance(data[0], list):
        head = [str(h) for h in data[0]]
        return [dict(zip(head, r)) for r in data[1:] if isinstance(r, list)]
    return [r for r in data if isinstance(r, dict)]


# ---------------------------------------------------------------------------------------------
# Parsers (one per feed; each returns plain dicts and raises ValueError on nonsense)
# ---------------------------------------------------------------------------------------------
def _scale_level(v) -> int | None:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 5 else None


def _pct(v) -> int | None:
    try:
        n = int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 100 else None


def parse_scales(data) -> dict:
    """noaa-scales.json: "-1" the past 24 hours' maximum, "0" now, "1".."3" today and the next two days."""
    if not isinstance(data, dict) or "0" not in data:
        raise ValueError("NOAA scales answered without current conditions")

    def block(b: dict) -> dict:
        r, s, g = b.get("R") or {}, b.get("S") or {}, b.get("G") or {}
        return {"date": str(b.get("DateStamp") or "")[:10],
                "G": {"level": _scale_level(g.get("Scale")), "text": g.get("Text") or None},
                "S": {"level": _scale_level(s.get("Scale")), "text": s.get("Text") or None, "prob": _pct(s.get("Prob"))},
                "R": {"level": _scale_level(r.get("Scale")), "text": r.get("Text") or None,
                      "minor_prob": _pct(r.get("MinorProb")), "major_prob": _pct(r.get("MajorProb"))}}

    out = {"now": block(data["0"]), "max_24h": block(data["-1"]) if isinstance(data.get("-1"), dict) else None,
           "forecast": []}
    for k in ("1", "2", "3"):
        if isinstance(data.get(k), dict):
            out["forecast"].append(block(data[k]))
    return out


def parse_kp(data) -> list[dict]:
    """noaa-planetary-k-index.json: observed 3-hour Kp for the past week."""
    out = []
    for r in _rows_of(data, ("time_tag", "Kp")):
        t = A.iso_time(r.get("time_tag"))
        kp = _num(r.get("Kp", r.get("kp")), 2)
        if t is None or kp is None or not 0 <= kp <= 9:
            continue
        out.append({"time": t, "kp": kp, "g": g_of_kp(kp)})
    if not out:
        raise ValueError("the planetary K-index list is empty")
    return sorted(out, key=lambda s: s["time"])


def parse_kp_forecast(data) -> list[dict]:
    """noaa-planetary-k-index-forecast.json: the past week observed, then estimated and predicted slots."""
    out = []
    for r in _rows_of(data, ("time_tag", "kp", "observed", "noaa_scale")):
        t = A.iso_time(r.get("time_tag"))
        kp = _num(r.get("kp", r.get("Kp")), 2)
        if t is None or kp is None or not 0 <= kp <= 9:
            continue
        kind = str(r.get("observed") or "predicted").lower()
        kind = kind if kind in ("observed", "estimated", "predicted") else "predicted"
        scale = str(r.get("noaa_scale") or "")
        g = int(scale[1]) if re.fullmatch(r"G[1-5]", scale) else g_of_kp(kp)
        out.append({"time": t, "kp": kp, "kind": kind, "g": g})
    if not out:
        raise ValueError("the Kp forecast is empty")
    return sorted(out, key=lambda s: s["time"])


def parse_kp_now(data) -> list[dict]:
    """planetary_k_index_1m.json: SWPC's estimated Kp, every minute for the last few hours."""
    out = []
    for r in _rows_of(data, ("time_tag",)):
        t = A.iso_time(r.get("time_tag"))
        kp = _num(r.get("estimated_kp", r.get("kp_index")), 2)
        if t is None or kp is None or not 0 <= kp <= 9:
            continue
        out.append({"time": t, "kp": kp})
    if not out:
        raise ValueError("the estimated Kp list is empty")
    return sorted(out, key=lambda s: s["time"])[-240:]


_FLARE = re.compile(r"^([ABCMX])(\d+(?:\.\d+)?)$")
_BASE = {"A": 1e-8, "B": 1e-7, "C": 1e-6, "M": 1e-5, "X": 1e-4}


def class_flux(cls: str | None) -> float | None:
    m = _FLARE.match(str(cls or "").strip().upper())
    return _BASE[m.group(1)] * float(m.group(2)) if m else None


def flux_class(flux: float | None) -> str | None:
    if flux is None or flux <= 0:
        return None
    for letter in ("X", "M", "C", "B", "A"):
        if flux >= _BASE[letter] or letter == "A":
            return f"{letter}{flux / _BASE[letter]:.1f}"
    return None


def r_level(flux: float | None) -> int:
    """NOAA R scale from the peak 0.1-0.8 nm X-ray flux (W/m2)."""
    if flux is None:
        return 0
    for level, lim in ((5, 2e-3), (4, 1e-3), (3, 1e-4), (2, 5e-5), (1, 1e-5)):
        if flux >= lim * 0.9999:
            return level
    return 0


def flare_impact(r: int, letter: str) -> str:
    if r >= 1:
        return f"{R_NAME[r].capitalize()} radio blackout (R{r}) on the sunlit side of Earth"
    if letter == "C":
        return "Small flare, no effect on Earth"
    return "Background level"


def _flare(r: dict) -> dict | None:
    begin = A.iso_time(r.get("begin_time"))
    peak = A.iso_time(r.get("max_time"))
    end_raw = r.get("end_time")
    end = A.iso_time(end_raw) if end_raw not in (None, "", "Unk", "unk") else None
    cls = str(r.get("max_class") or "").strip().upper() or None
    flux = _num(r.get("max_xrlong")) or class_flux(cls)
    if cls is None or not _FLARE.match(cls):
        cls = flux_class(flux)
    if peak is None or cls is None:
        return None
    rl = r_level(flux if flux is not None else class_flux(cls))
    return {"begin": begin or peak, "max": peak, "end": end, "class": cls, "flux": flux, "r": rl,
            "impact": flare_impact(rl, cls[0])}


def parse_flares(data) -> list[dict]:
    """xray-flares-7-day.json: GOES flares with begin, peak, end and class."""
    if not isinstance(data, list):
        raise ValueError("the flare list is not a list")
    out = [f for f in (_flare(r) for r in data if isinstance(r, dict)) if f]
    return sorted(out, key=lambda f: f["max"], reverse=True)


def parse_flare_now(data) -> dict:
    """xray-flares-latest.json: the current X-ray class and the latest (possibly ongoing) flare."""
    rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    if not rows:
        raise ValueError("the latest-flare file is empty")
    r = rows[-1]
    cur = str(r.get("current_class") or "").strip().upper()
    return {"time": A.iso_time(r.get("time_tag")), "current_class": cur if _FLARE.match(cur) else None,
            "flare": _flare(r)}


def parse_wind(data) -> dict:
    """propagated-solar-wind-1-hour.json: the last hour at L1 with the time each minute reaches Earth."""
    rows = _rows_of(data, ("time_tag", "speed", "density", "bz", "bt"))
    series = []
    for r in rows:
        t = A.iso_time(r.get("time_tag"))
        if t is None:
            continue
        series.append({"time": t, "speed": _num(r.get("speed"), 1), "density": _num(r.get("density"), 2),
                       "bz": _num(r.get("bz"), 2), "by": _num(r.get("by"), 2), "bt": _num(r.get("bt"), 2),
                       "arrives": A.iso_time(r.get("propagated_time_tag"))})
    series.sort(key=lambda s: s["time"])
    good = [s for s in series if s["speed"] is not None or s["bz"] is not None]
    if not good:
        raise ValueError("the solar wind list is empty")
    last = good[-1]

    def latest(k):
        return next((s[k] for s in reversed(good) if s[k] is not None), None)

    hour = [s for s in good if s["time"] >= last["time"] - 3600 and s["bz"] is not None]
    return {"time": last["time"], "speed": latest("speed"), "density": latest("density"), "bz": latest("bz"),
            "by": latest("by"), "bt": latest("bt"), "arrives": last["arrives"],
            "series": [[int(s["time"]), s["speed"], s["bz"]] for s in good[-90:]],
            "south_minutes": sum(1 for s in hour if s["bz"] < 0), "minutes": len(hour), "source": "propagated"}


def parse_wind_summary(speed, mag) -> dict:
    """products/summary/solar-wind-speed.json and solar-wind-mag-field.json (fallback: values only)."""
    sp = (speed or [{}])[0] if isinstance(speed, list) else {}
    mg = (mag or [{}])[0] if isinstance(mag, list) else {}
    t = A.iso_time(sp.get("time_tag")) or A.iso_time(mg.get("time_tag"))
    if t is None:
        raise ValueError("the solar wind summary is empty")
    return {"time": t, "speed": _num(sp.get("proton_speed"), 1), "density": None, "bz": _num(mg.get("bz_gsm"), 2),
            "by": None, "bt": _num(mg.get("bt"), 2), "arrives": None, "series": [], "south_minutes": None,
            "minutes": 0, "source": "summary"}


# ---------------------------------------------------------------------------------------------
# Alerts, watches and warnings
# ---------------------------------------------------------------------------------------------
_HEAD = re.compile(r"^(EXTENDED WARNING|CONTINUED ALERT|CANCEL WARNING|CANCEL WATCH|CANCEL ALERT|CANCEL SUMMARY|CANCEL|"
                   r"ALERT|WARNING|WATCH|SUMMARY)\s*:\s*(.+?)\s*$")
_KV = re.compile(r"^([A-Z][A-Za-z0-9 ()/.-]{1,60}?):\s*(.*?)\s*$")
_SCALE = re.compile(r"(?i)noaa scale:\s*([GSR])([1-5])")
_PFU = {10: 1, 100: 2, 1000: 3, 10000: 4, 100000: 5}
_TYPE = {"ALERT": "alert", "CONTINUED ALERT": "alert", "WARNING": "warning", "EXTENDED WARNING": "warning",
         "WATCH": "watch", "SUMMARY": "summary"}


def _impacts(lines: list[str]) -> list[str]:
    out = []
    started = False
    for ln in lines:
        if ln.startswith("Potential Impacts:"):
            started = True
            rest = ln.split(":", 1)[1].strip()
            if rest:
                out.append(rest)
            continue
        if started:
            if not ln.strip():
                break
            out.append(ln.strip())
    return out[:6]


def _watch_days(text: str, issued: float) -> list[dict]:
    i = text.find("Highest Storm Level Predicted by Day")
    if i < 0:
        return []
    year = datetime.fromtimestamp(issued, tz=timezone.utc).year
    imon = datetime.fromtimestamp(issued, tz=timezone.utc).month
    days = []
    for m in re.finditer(r"([A-Z][a-z]{2})\s+(\d{1,2}):\s+(None|G([1-5]))", text[i:i + 600]):
        mon = _MONTHS.get(m.group(1))
        if not mon:
            continue
        y = year + 1 if mon < imon - 6 else year
        try:
            start = datetime(y, mon, int(m.group(2)), tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        days.append({"date": datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d"), "start": start,
                     "g": int(m.group(4)) if m.group(4) else 0})
    return days


def _plain(a: dict) -> tuple[str, str]:
    """(title, detail) in plain words for one SWPC message."""
    h = a["headline"]
    f = a["fields"]
    kind = a["type"]
    until = a.get("end")
    until_txt = f" until {day_label(until)} {hm(until)} UTC" if until else ""
    k = re.search(r"K-index of (\d)", h)
    if k and "Geomagnetic" in h:
        n = int(k.group(1))
        g = max(0, n - 4)
        if kind == "warning":
            title = f"Kp {n} expected{until_txt}"
            detail = (f"A {G_NAME[g]} geomagnetic storm (G{g}) is likely." if g else
                      "Active conditions, just below storm level.")
        else:
            title = f"Kp reached {n}" + (f": {G_NAME[g]} geomagnetic storm (G{g})" if g else ", just below storm level")
            detail = "Earth's magnetic field is disturbed; aurora reaches further from the poles than usual."
        return title, detail
    m = re.search(r"Geomagnetic Storm Category G([1-5])", h)
    if m and kind == "watch":
        g = int(m.group(1))
        stormy = [d for d in a.get("days") or [] if d["g"] >= 1]
        if stormy:
            parts = [f"G{d['g']} on {day_label(d['start'])}" for d in stormy]
            detail = "Expected: " + ", ".join(parts) + "."
        else:
            detail = "SWPC expects a storm in the next few days."
        return f"{G_NAME[g].capitalize()} geomagnetic storm (G{g}) watch", detail
    if "Sudden Impulse" in h:
        if kind == "warning":
            return "Solar wind shock expected", ("A jump in the solar wind, usually the front of a coronal mass ejection, "
                                                 "should hit Earth's magnetic field" + (until_txt or "") + ".")
        return "Solar wind shock arrived", "A sudden jump in the solar wind hit Earth's magnetic field; storms often follow."
    if "Electron" in h and "2MeV" in h.replace(" ", ""):
        since = swpc_time(f.get("Begin Time"))
        return ("High-energy electrons in orbit", "Satellites can build up static charge."
                + (f" Elevated since {day_label(since)}." if since else ""))
    m = re.search(r"Proton Event 10\s*MeV Integral Flux exceeded ([\d,]+)\s*pfu", h, re.I)
    if m:
        s = _PFU.get(int(m.group(1).replace(",", "")), 1)
        return (f"{S_NAME[s].capitalize()} solar radiation storm (S{s})",
                "Energetic protons from the Sun: HF radio fades over the poles and polar flights get extra radiation.")
    if re.search(r"Proton 10\s*MeV Integral Flux above 10\s*pfu expected", h, re.I):
        return "Solar radiation storm expected", "Energetic protons may pass S1 (minor)" + until_txt + "."
    if re.search(r"Proton (Event )?100\s*MeV", h, re.I):
        return ("Very high-energy protons", "Protons above 100 MeV from the Sun: a hazard to satellites and astronauts.")
    if re.search(r"X-?Ray Flux exceeded M5", h, re.I):
        return "Strong solar flare under way (M5 or more)", "Moderate radio blackout (R2) on the sunlit side of Earth."
    m = re.search(r"X-?ray Event exceeded X(\d+)", h, re.I)
    if m:
        cls = (f.get("X-ray Class") or "").strip().upper() or f"X{m.group(1)}"
        rl = r_level(class_flux(cls)) or 3
        peak = swpc_time(f.get("Maximum Time"))
        return (f"{cls} solar flare", f"{R_NAME[rl].capitalize()} radio blackout (R{rl}) on the sunlit side"
                + (f", peak {hm(peak)} UTC" if peak else "") + ".")
    if "10cm Radio Burst" in h or "10 cm Radio Burst" in h:
        return ("Solar radio burst", "A burst of radio noise from the Sun; it can briefly disturb radar, GPS and "
                                     "satellite links on the sunlit side.")
    if "Type II Radio Emission" in h:
        v = re.search(r"(\d{3,4})", f.get("Estimate Velocity") or "")
        return ("Coronal mass ejection likely", "A type II radio burst usually marks a shock leaving the Sun"
                + (f" at about {int(v.group(1)):,} km/s" if v else "") + ". SWPC will say whether it is heading for Earth.")
    if "Type IV Radio Emission" in h:
        return ("Major eruption on the Sun", "Type IV radio emission often comes with big flares and coronal mass "
                                             "ejections, and sometimes radiation storms.")
    text = h[0].upper() + h[1:].lower() if h else "SWPC message"
    return text, ""


def _family(code: str, headline: str) -> str:
    if "K-index" in headline or "Geomagnetic" in headline:
        return "geomagnetic"
    if "Proton" in headline:
        return "radiation"
    if "X-ray" in headline or "X-Ray" in headline or "Radio" in headline:
        return "radio"
    if "Electron" in headline:
        return "electrons"
    return "other"


def parse_alert(row: dict) -> dict | None:
    msg = str(row.get("message") or "").replace("\r", "")
    if not msg.strip():
        return None
    lines = [ln.rstrip() for ln in msg.split("\n")]
    fields: dict[str, str] = {}
    head_kind = head_text = None
    for ln in lines:
        hm_ = _HEAD.match(ln.strip())
        if hm_ and head_kind is None:
            head_kind, head_text = hm_.group(1), hm_.group(2)
            continue
        kv = _KV.match(ln.strip())
        if kv and kv.group(1) not in fields:
            fields[kv.group(1)] = kv.group(2)
    if head_kind is None:
        return None
    code = (fields.get("Space Weather Message Code") or str(row.get("product_id") or "")).strip()[:12]
    try:
        serial = int((fields.get("Serial Number") or "0").strip())
    except ValueError:
        serial = 0
    issued = A.iso_time(row.get("issue_datetime")) or swpc_time(fields.get("Issue Time"))
    if issued is None:
        return None
    cancel = head_kind.startswith("CANCEL")
    a = {"id": f"{code}:{serial}", "code": code, "serial": serial, "issued": issued,
         "type": "cancel" if cancel else _TYPE.get(head_kind, "alert"),
         "continued": head_kind in ("CONTINUED ALERT", "EXTENDED WARNING"),
         "headline": f"{head_kind}: {head_text}", "fields": fields, "family": _family(code, head_text)}
    sm = _SCALE.search(msg)
    a["scale"] = f"{sm.group(1).upper()}{sm.group(2)}" if sm else None
    a["level"] = int(sm.group(2)) if sm else 0
    ref = (fields.get("Extension to Serial Number") or fields.get("Continuation of Serial Number")
           or fields.get("Cancel Serial Number") or "")
    a["refs"] = int(ref) if ref.strip().isdigit() else None
    a["begin"] = (swpc_time(fields.get("Valid From")) or swpc_time(fields.get("Threshold Reached"))
                  or swpc_time(fields.get("Begin Time")) or issued)
    a["end"] = (swpc_time(fields.get("Now Valid Until")) or swpc_time(fields.get("Valid To"))
                or swpc_time(fields.get("End Time")))
    if a["type"] == "watch":
        a["days"] = _watch_days(msg, issued)
        stormy = [d for d in a["days"] if d["g"] >= 1]
        if stormy:
            a["end"] = stormy[-1]["start"] + 86400
            a["level"] = max(a["level"], max(d["g"] for d in stormy))
        m = re.search(r"Category G([1-5])", head_text)
        if m:
            a["level"] = max(a["level"], int(m.group(1)))
            a["scale"] = a["scale"] or f"G{m.group(1)}"
    k = re.search(r"K-index of (\d)", head_text)
    if k and not a["level"] and int(k.group(1)) >= 5:
        a["level"] = int(k.group(1)) - 4
        a["scale"] = f"G{a['level']}"
    p = re.search(r"exceeded ([\d,]+)\s*pfu", head_text, re.I)
    if p and "Proton" in head_text and "10MeV" in head_text.replace(" ", ""):
        lvl = _PFU.get(int(p.group(1).replace(",", "")), 0)
        if lvl and not a["level"]:
            a["level"], a["scale"] = lvl, f"S{lvl}"
    a["impacts"] = _impacts(lines)
    a["title"], a["detail"] = _plain(a)
    # the official text without SWPC's boilerplate lines, for anyone who wants the original wording
    keep = [ln for ln in lines if ln.strip() and "noaa-scales-explanation" not in ln
            and not ln.startswith("NOAA Space Weather Scale descriptions")]
    a["message"] = "\n".join(keep)[:1500]
    return a


def parse_alerts(data) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("the alerts list is not a list")
    out = [a for a in (parse_alert(r) for r in data if isinstance(r, dict)) if a]
    return sorted(out, key=lambda a: a["issued"], reverse=True)


def alert_states(alerts: list[dict], now: float) -> list[dict]:
    """Copies of the messages with `active` set: valid warnings, the newest watch until its last storm
    day, and alerts and summaries from the last 24 hours, minus anything extended, continued or cancelled."""
    replaced = {(a["family"], a["refs"]) for a in alerts if a["refs"] and a["issued"] <= now}
    newest_watch = next((a["id"] for a in alerts if a["type"] == "watch" and a["issued"] <= now), None)
    out = []
    for a in alerts:
        if a["issued"] > now + 300:
            continue
        b = dict(a)
        gone = (a["family"], a["serial"]) in replaced
        if a["type"] == "cancel":
            active = False
        elif a["type"] == "warning":
            end = a["end"] or a["issued"] + 12 * 3600
            active = not gone and (a["begin"] or a["issued"]) - 3600 <= now <= end
        elif a["type"] == "watch":
            end = a["end"] or a["issued"] + 3 * 86400
            active = not gone and a["id"] == newest_watch and now <= end
        else:
            active = not gone and now - a["issued"] <= 24 * 3600
        b["active"] = active
        out.append(b)
    return out


def alerts_section(alerts: list[dict] | None, now: float) -> dict | None:
    if alerts is None:
        return None
    states = alert_states(alerts, now)
    active = [a for a in states if a["active"]]
    # one line per kind of geomagnetic message: the highest K alert and the highest K warning
    keep = []
    best: dict[tuple[str, str], dict] = {}
    for a in active:
        k = re.search(r"K-index of (\d)", a["headline"])
        if a["family"] == "geomagnetic" and k:
            key = (a["type"], "K")
            cur = best.get(key)
            if cur is None or int(k.group(1)) > int(re.search(r"K-index of (\d)", cur["headline"]).group(1)):
                best[key] = a
            continue
        if a["code"] == "ALTEF3":
            key = ("alert", "EF3")
            if key not in best:
                best[key] = a
            continue
        keep.append(a)
    keep.extend(best.values())
    order = {"warning": 0, "watch": 1, "alert": 2, "summary": 3}
    keep.sort(key=lambda a: (-a["level"], order.get(a["type"], 4), -a["issued"]))
    shown = {a["id"] for a in keep}
    recent = [a for a in states if a["id"] not in shown and a["type"] != "cancel" and now - a["issued"] <= 7 * 86400]

    def public(a: dict) -> dict:
        return {k: a.get(k) for k in ("id", "code", "serial", "type", "title", "detail", "headline", "scale", "level",
                                      "family", "issued", "begin", "end", "days", "impacts", "message", "active")}

    return {"active": [public(a) for a in keep], "recent": [public(a) for a in recent[:20]],
            "count_active": len(keep), "url": PAGES["alerts"]}


# ---------------------------------------------------------------------------------------------
# The "now" snapshot
# ---------------------------------------------------------------------------------------------
def kp_section(kp_now: list[dict] | None, kp: list[dict] | None, fc: list[dict] | None, now: float) -> dict:
    out: dict = {"now": None, "last": None, "max_24h": None, "series": [], "forecast_max": None, "days": []}
    if kp_now:
        r = [s for s in kp_now if s["time"] <= now + 300]
        if r:
            s = r[-1]
            out["now"] = {"kp": s["kp"], "time": s["time"], "g": g_of_kp(s["kp"]), "source": "estimated each minute"}
    if kp:
        done = [s for s in kp if s["time"] <= now]
        if done:
            s = done[-1]
            out["last"] = {"kp": s["kp"], "time": s["time"], "end": s["time"] + 10800, "g": s["g"]}
            day = [x for x in done if x["time"] + 10800 > now - 86400]
            if day:
                m = max(day, key=lambda x: x["kp"])
                out["max_24h"] = {"kp": m["kp"], "time": m["time"], "g": m["g"]}
    if fc:
        out["series"] = [dict(s) for s in fc if s["time"] + 10800 > now - 36 * 3600][:48]
        ahead = [s for s in fc if s["time"] + 10800 > now and s["kind"] != "observed"]
        if ahead:
            m = max(ahead, key=lambda x: (x["kp"], -x["time"]))
            out["forecast_max"] = {"kp": m["kp"], "time": m["time"], "g": m["g"]}
            days: dict[str, dict] = {}
            for s in ahead:
                d = datetime.fromtimestamp(s["time"], tz=timezone.utc).strftime("%Y-%m-%d")
                if d not in days or s["kp"] > days[d]["kp"]:
                    days[d] = {"date": d, "kp": s["kp"], "g": s["g"], "time": s["time"]}
            out["days"] = list(days.values())[:4]
        if out["now"] is None:
            cur = [s for s in fc if s["time"] <= now < s["time"] + 10800]
            if cur:
                out["now"] = {"kp": cur[0]["kp"], "time": cur[0]["time"], "g": cur[0]["g"],
                              "source": f"{cur[0]['kind']} for this 3-hour period"}
    if out["now"] is None and out["last"] is not None:
        out["now"] = dict(out["last"], source="latest 3-hour value")
    return out


def xray_section(flares: list[dict] | None, latest: dict | None, now: float) -> dict | None:
    if flares is None and latest is None:
        return None
    fl = list(flares or [])
    lf = (latest or {}).get("flare")
    if lf and not any(abs(f["max"] - lf["max"]) < 600 for f in fl):
        fl.append(lf)
    fl.sort(key=lambda f: f["max"], reverse=True)
    week = [f for f in fl if f["max"] >= now - 7 * 86400]
    day = [f for f in week if f["max"] >= now - 86400]
    ongoing = lf if lf and lf["end"] is None and now - lf["begin"] < 6 * 3600 else None
    counts = {c: sum(1 for f in week if f["class"].startswith(c)) for c in ("C", "M", "X")}
    return {"now_class": (latest or {}).get("current_class"), "now_time": (latest or {}).get("time"),
            "ongoing": ongoing, "recent": [f for f in week if f["class"][0] in "CMX" and f["max"] >= now - 72 * 3600][:6],
            "notable": [f for f in week if f["class"][0] in "MX"][:6],
            "biggest_24h": max(day, key=lambda f: f["flux"] or 0) if day else None,
            "biggest_7d": max(week, key=lambda f: f["flux"] or 0) if week else None, "counts_7d": counts}


def summary_text(s: dict) -> str:
    """Two or three plain sentences for the panel header, the layer row and the AI."""
    parts = []
    kp = s.get("kp") or {}
    sc = (s.get("scales") or {}).get("now") or {}
    g_now = (sc.get("G") or {}).get("level")
    kpn = (kp.get("now") or {}).get("kp")
    if g_now is None:
        g_now = g_of_kp(kpn)
    if g_now and g_now >= 1:
        parts.append(f"{G_NAME[g_now].capitalize()} geomagnetic storm (G{g_now}) now"
                     + (f", Kp {kp_text(kpn)}." if kpn is not None else "."))
    elif kpn is None:
        parts.append("Kp is unavailable right now.")
    else:
        word = "Quiet" if kpn < 3 else "Unsettled" if kpn < 4 else "Active"
        parts.append(f"{word} geomagnetic field, Kp {kp_text(kpn)} now.")
    fmax = kp.get("forecast_max")
    if fmax and fmax["g"] >= 1:
        parts.append(f"{G_NAME[fmax['g']].capitalize()} storm (G{fmax['g']}) forecast for {day_label(fmax['time'])}.")
    elif fmax:
        parts.append(f"No storms forecast for the next three days (Kp up to {kp_text(fmax['kp'])}).")
    r_now = (sc.get("R") or {}).get("level") or 0
    s_now = (sc.get("S") or {}).get("level") or 0
    if r_now >= 1:
        parts.append(f"{R_NAME[r_now].capitalize()} radio blackout (R{r_now}) on the sunlit side.")
    if s_now >= 1:
        parts.append(f"{S_NAME[s_now].capitalize()} solar radiation storm (S{s_now}) in progress.")
    x = s.get("xray") or {}
    big = x.get("biggest_24h")
    if x.get("ongoing") and x["ongoing"]["class"][0] in "MX":
        parts.append(f"A {x['ongoing']['class']} flare is in progress.")
    elif big and big["class"][0] in "MX":
        parts.append(f"Largest flare in the past day: {big['class']} at {hm(big['max'])} UTC.")
    return " ".join(parts)


def build_now(d: dict, now: float) -> dict:
    out = {"time": now, "kp": kp_section(d.get("kp_now"), d.get("kp"), d.get("kp_forecast"), now),
           "scales": d.get("scales"), "xray": xray_section(d.get("flares"), d.get("flare_now"), now),
           "wind": d.get("wind"), "alerts": alerts_section(d.get("alerts"), now)}
    grid = d.get("ovation")
    out["aurora"] = None if grid is None else {
        "observed": grid.observed, "forecast": grid.forecast, "north_max": grid.north_max, "south_max": grid.south_max}
    out["summary"] = summary_text(out)
    out["links"] = LINKS
    out["source"] = CREDIT
    return out


# ---------------------------------------------------------------------------------------------
# Events: G3+ storms, X-class flares (R3+ blackouts), S2+ radiation storms, G3+ watches
# ---------------------------------------------------------------------------------------------
def _episodes(points: list[tuple[float, int, float | None]], gap_s: float) -> list[list[tuple[float, int, float | None]]]:
    eps: list[list] = []
    for p in sorted(points, key=lambda q: (q[0], q[1])):
        if eps and p[0] - eps[-1][-1][0] <= gap_s:
            eps[-1].append(p)
        else:
            eps.append([p])
    return eps


def _stamp(t: float, fmt: str = "%Y%m%d") -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime(fmt)


def build_events(d: dict, now: float) -> list[dict]:
    plat, plon = A.POLE
    out: list[dict] = []
    alerts = d.get("alerts") or []
    sc_now = ((d.get("scales") or {}).get("now") or {})

    def base(eid, etype, title, subtitle, t, sev, lat, lon, scale, url):
        return {"kind": "space_weather", "id": eid, "type": etype, "title": title, "subtitle": subtitle,
                "lat": round(lat, 5), "lon": round(lon, 5), "time": round(t), "severity": sev, "scale": scale,
                "layer": "space.spaceweather", "zoom": 2, "url": url, "source": "NOAA SWPC"}

    # geomagnetic storms, G3 or more: observed Kp, SWPC K-index alerts and the current G scale
    pts = [(s["time"], s["g"], s["kp"]) for s in d.get("kp") or [] if s["g"] >= 3 and s["time"] <= now]
    for a in alerts:
        k = re.search(r"K-index of (\d)", a["headline"])
        if a["type"] == "alert" and a["family"] == "geomagnetic" and k and int(k.group(1)) >= 7 and a["issued"] <= now:
            n = int(k.group(1))
            pts.append((a["begin"] or a["issued"], n - 4, float(n)))
    g_now = (sc_now.get("G") or {}).get("level") or 0
    if g_now >= 3:
        pts.append((math.floor(now / 10800) * 10800, g_now, None))
    for ep in _episodes(pts, 12 * 3600):
        g = max(p[1] for p in ep)
        kps = [p[2] for p in ep if p[2] is not None]
        first, last = ep[0][0], ep[-1][0]
        sub = (f"Kp up to {kp_text(max(kps))}" if kps else "Kp 7 or more") + f", from {day_label(first)} {hm(first)} UTC"
        sub += "; aurora far from the poles, power grids and satellites affected"
        out.append(base(f"swpc:gstorm:{_stamp(first)}", "geomagnetic_storm",
                        f"{G_NAME[g].capitalize()} geomagnetic storm (G{g})", sub, min(now, last + 10800),
                        {3: 6, 4: 8, 5: 9}[g], plat, plon, f"G{g}", PAGES["storms"]))

    # X-class flares (R3 or more radio blackouts), from the flare list, the latest flare and summaries
    flares = list(d.get("flares") or [])
    lf = (d.get("flare_now") or {}).get("flare")
    if lf:
        flares.append(lf)
    for a in alerts:
        m = re.search(r"X-?ray Event exceeded X", a["headline"], re.I)
        if m and a["type"] == "summary":
            cls = (a["fields"].get("X-ray Class") or "").strip().upper()
            peak = swpc_time(a["fields"].get("Maximum Time")) or a["issued"]
            begin = swpc_time(a["fields"].get("Begin Time")) or peak
            flux = class_flux(cls) or 1e-4
            flares.append({"begin": begin, "max": peak, "end": swpc_time(a["fields"].get("End Time")),
                           "class": cls or "X1.0", "flux": flux, "r": r_level(flux), "impact": ""})
    seen: list[float] = []
    for f in sorted(flares, key=lambda f: -(f["flux"] or 0)):
        if (f["flux"] or 0) < 1e-4 * 0.9999 or f["max"] > now + 600:
            continue
        if any(abs(f["max"] - t) < 900 for t in seen):
            continue
        seen.append(f["max"])
        rl = max(3, f["r"])
        mag = (f["flux"] or 1e-4) / 1e-4
        sev = 9 if rl >= 5 else 7 if rl == 4 else 6 if mag >= 5 else 5
        slat, slon = S.subsolar_point(f["max"])
        out.append(base(f"swpc:flare:{_stamp(f['begin'], '%Y%m%dT%H%M')}", "solar_flare",
                        f"{f['class']} solar flare, {R_NAME[rl]} radio blackout (R{rl})",
                        f"Peak {day_label(f['max'])} {hm(f['max'])} UTC; HF radio blacked out on the sunlit side of Earth",
                        f["max"], sev, slat, slon, f"R{rl}", PAGES["flares"]))

    # solar radiation storms, S2 or more: proton alerts and the current S scale
    pts = []
    for a in alerts:
        if a["family"] == "radiation" and a["type"] in ("alert", "summary") and a["level"] >= 2 and a["issued"] <= now:
            pts.append((a["begin"] or a["issued"], a["level"], None))
    s_now = (sc_now.get("S") or {}).get("level") or 0
    if s_now >= 2:
        pts.append((math.floor(now / 10800) * 10800, s_now, None))
    for ep in _episodes(pts, 24 * 3600):
        s = max(p[1] for p in ep)
        first, last = ep[0][0], ep[-1][0]
        out.append(base(f"swpc:sstorm:{_stamp(first)}", "radiation_storm",
                        f"{S_NAME[s].capitalize()} solar radiation storm (S{s})",
                        f"From {day_label(first)} {hm(first)} UTC; HF radio fades over the poles, extra radiation on polar flights",
                        min(now, last + 3600), {2: 4, 3: 6, 4: 8, 5: 9}[s], plat, plon, f"S{s}", PAGES["radiation"]))

    # storm watches, G3 or more (the newest watch only, while it is in force)
    for a in alert_states(alerts, now):
        if a["type"] == "watch" and a["active"] and a["level"] >= 3:
            g = a["level"]
            out.append(base(f"swpc:gwatch:{a['serial']}", "storm_watch",
                            f"{G_NAME[g].capitalize()} geomagnetic storm watch (G{g})", a["detail"] or "SWPC storm watch",
                            a["issued"], {3: 4, 4: 6, 5: 7}[g], plat, plon, f"G{g}", PAGES["alerts"]))
    return out


# ---------------------------------------------------------------------------------------------
# Feeds and the service
# ---------------------------------------------------------------------------------------------
class Feed:
    """One SWPC product with its own cache, error state and back-off (1, 2, 4 ... 30 minutes)."""

    def __init__(self, name: str, url: str, parse, ttl: float, *, timeout: float = 15.0, fetch=None):
        self.name = name
        self.url = url
        self.parse = parse
        self.ttl = ttl
        self.timeout = timeout
        self.fetch = fetch
        self.data = None
        self.fetched_at = 0.0
        self.error: str | None = None
        self.fails = 0
        self.retry_at = 0.0
        self.lock = threading.Lock()

    def age(self) -> float | None:
        return time.time() - self.fetched_at if self.data is not None else None

    def get(self, net, max_age: float):
        now = time.time()
        if self.data is not None and now - self.fetched_at <= max_age:
            return self.data
        if now < self.retry_at:
            return self.data
        with self.lock:
            now = time.time()
            if (self.data is not None and now - self.fetched_at <= max_age) or now < self.retry_at:
                return self.data
            try:
                if self.fetch is not None:
                    data = self.fetch(net, self.timeout)
                else:
                    data = self.parse(net.get_json(self.url, timeout=self.timeout, retries=1))
                self.data, self.fetched_at, self.error, self.fails, self.retry_at = data, time.time(), None, 0, 0.0
            except Exception as err:   # a dead feed only marks its part of the panel
                self.fails += 1
                self.error = (str(err) or err.__class__.__name__)[:200]
                self.retry_at = time.time() + min(1800.0, 60.0 * 2 ** (self.fails - 1))
            return self.data

    def info(self) -> dict:
        age = self.age()
        return {"ok": self.error is None and self.data is not None, "age_s": None if age is None else round(age),
                "error": self.error}


PANEL = ("scales", "kp_now", "kp", "kp_forecast", "flares", "flare_now", "wind", "alerts")
EVENT_FEEDS = ("scales", "kp", "flares", "flare_now", "alerts")


class SpaceWeather:
    def __init__(self, net, log, status=None):
        self.net = net
        self.log = log
        self.status = status or (lambda *a, **k: None)
        self.feeds = {
            "scales": Feed("scales", f"{SWPC}/products/noaa-scales.json", parse_scales, 300),
            "kp_now": Feed("kp_now", f"{SWPC}/json/planetary_k_index_1m.json", parse_kp_now, 300),
            "kp": Feed("kp", f"{SWPC}/products/noaa-planetary-k-index.json", parse_kp, 900),
            "kp_forecast": Feed("kp_forecast", f"{SWPC}/products/noaa-planetary-k-index-forecast.json",
                                parse_kp_forecast, 1800),
            "flares": Feed("flares", f"{SWPC}/json/goes/primary/xray-flares-7-day.json", parse_flares, 900),
            "flare_now": Feed("flare_now", f"{SWPC}/json/goes/primary/xray-flares-latest.json", parse_flare_now, 300),
            "wind": Feed("wind", f"{SWPC}/products/geospace/propagated-solar-wind-1-hour.json", parse_wind, 300,
                         fetch=self._fetch_wind),
            "alerts": Feed("alerts", f"{SWPC}/products/alerts.json", parse_alerts, 300),
            "ovation": Feed("ovation", f"{SWPC}/json/ovation_aurora_latest.json", A.Grid.from_json, 600, timeout=30.0),
        }
        self._bg: threading.Thread | None = None
        self._bg_lock = threading.Lock()
        self._snap: tuple | None = None

    # -- fetching --------------------------------------------------------------------------------
    def _fetch_wind(self, net, timeout: float) -> dict:
        try:
            return parse_wind(net.get_json(self.feeds["wind"].url, timeout=timeout, retries=1))
        except Exception as err:
            self.log.info("propagated solar wind unavailable (%s); using the SWPC summaries", err)
            speed = net.get_json(f"{SWPC}/products/summary/solar-wind-speed.json", timeout=timeout, retries=1)
            mag = net.get_json(f"{SWPC}/products/summary/solar-wind-mag-field.json", timeout=timeout, retries=1)
            return parse_wind_summary(speed, mag)

    def refresh(self, names, *, max_age: float | None = None, budget_s: float = 25.0) -> None:
        """Fetch the named feeds that are older than their TTL (or max_age), in parallel."""
        stale = []
        for n in names:
            f = self.feeds[n]
            age = f.age()
            limit = f.ttl if max_age is None else max_age
            if age is None or age > limit:
                stale.append((f, limit))
        if not stale:
            return
        threads = [threading.Thread(target=f.get, args=(self.net, limit), daemon=True, name=f"swpc-{f.name}")
                   for f, limit in stale]
        for t in threads:
            t.start()
        deadline = time.monotonic() + budget_s
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self.report()

    def refresh_async(self, names, *, max_age: float | None = None) -> None:
        with self._bg_lock:
            if self._bg is not None and self._bg.is_alive():
                return
            self._bg = threading.Thread(target=self._safe_refresh, args=(tuple(names), max_age), daemon=True,
                                        name="swpc-refresh")
            self._bg.start()

    def _safe_refresh(self, names, max_age) -> None:
        try:
            self.refresh(names, max_age=max_age)
        except Exception as err:   # background work never raises
            self.log.warning("space weather refresh failed: %s", err)

    def data(self, names=None) -> dict:
        return {n: f.data for n, f in self.feeds.items() if names is None or n in names}

    def report(self) -> None:
        panel = [self.feeds[n] for n in PANEL]
        have = [f for f in panel if f.data is not None]
        errs = [f"{f.name}: {f.error}" for f in panel if f.error]
        if have:
            sc = (self.feeds["scales"].data or {}).get("now") or {}
            lv = "".join(f"{k}{(sc.get(k) or {}).get('level') if (sc.get(k) or {}).get('level') is not None else '?'} "
                         for k in ("G", "S", "R")).strip()
            alerts = self.feeds["alerts"].data
            n_active = None
            if alerts is not None:
                n_active = len(alerts_section(alerts, time.time())["active"])
            self.status("space.swpc", ok=not errs or len(have) >= len(panel) - 2, count=n_active,
                        error="; ".join(errs)[:400] if errs else None,
                        note=f"{len(have)} of {len(panel)} feeds, {lv}" + (f", {n_active} alerts in force" if n_active else ""))
        elif errs:
            self.status("space.swpc", ok=False, error="; ".join(errs)[:400])
        ov = self.feeds["ovation"]
        if ov.data is not None:
            g = ov.data
            self.status("space.ovation", ok=ov.error is None, count=g.north_max,
                        note=f"up to {g.north_max}% north, {g.south_max}% south, for {iso(g.forecast)}",
                        error=ov.error)
        elif ov.error:
            self.status("space.ovation", ok=False, error=ov.error)

    # -- views -----------------------------------------------------------------------------------
    def snapshot(self, *, budget_s: float = 20.0) -> dict | None:
        self.refresh(PANEL, budget_s=budget_s)
        stamp = tuple(round(self.feeds[n].fetched_at) for n in PANEL + ("ovation",))
        now = time.time()
        if self._snap and self._snap[0] == stamp and now - self._snap[1] < 30:
            return self._snap[2]
        d = self.data()
        if all(d.get(n) is None for n in PANEL):
            return None
        snap = build_now(d, now)
        snap["feeds"] = {n: self.feeds[n].info() for n in PANEL + ("ovation",)}
        self._snap = (stamp, now, snap)
        return snap

    def grid(self, *, fetch: bool = True) -> A.Grid | None:
        f = self.feeds["ovation"]
        if fetch:
            before = (f.fetched_at, f.fails)
            f.get(self.net, f.ttl)
            if (f.fetched_at, f.fails) != before:
                self.report()
        return f.data

    def aurora_meta(self) -> dict | None:
        g = self.grid()
        if g is None:
            return None
        north, south = g.edge_range(True), g.edge_range(False)
        return {"observed": g.observed, "forecast": g.forecast, "version": g.version, "maxzoom": A.MAX_TILE_ZOOM,
                "tiles": f"/api/space/aurora/tiles/{{z}}/{{x}}/{{y}}.png?v={g.version}",
                "north_max": g.north_max, "south_max": g.south_max,
                "north_edge": None if north is None else [round(north[0], 1), round(north[1], 1)],
                "south_edge": None if south is None else [round(south[0], 1), round(south[1], 1)],
                "overhead_pct": A.OVERHEAD_PCT, "horizon_deg": A.HORIZON_DEG,
                "horizon_km": round(A.HORIZON_DEG * 111.2 / 10) * 10, "legend": A.legend_colours(), "lines": g.lines(),
                "error": self.feeds["ovation"].error, "source": f"OVATION, {CREDIT}", "url": PAGES["aurora"]}

    def here(self, lat: float, lon: float, weather_fn=None, *, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self.refresh(("kp_forecast", "kp_now"), budget_s=15.0)
        grid = self.grid()
        weather = None
        if weather_fn is not None:
            try:
                weather = weather_fn(lat, lon)
            except Exception:
                weather = None
        kp_now_rows = self.feeds["kp_now"].data
        kp_now = kp_now_rows[-1]["kp"] if kp_now_rows else None
        out = A.aurora_here(lat, lon, now, grid=grid, kp_slots=self.feeds["kp_forecast"].data or [], kp_now=kp_now,
                            weather=weather)
        out["sources"] = {"ovation": grid is not None, "kp_forecast": self.feeds["kp_forecast"].data is not None,
                          "weather": weather is not None}
        out["url"] = PAGES["tips"]
        return out

    def events(self, since: float, *, wait: bool = False) -> list[dict]:
        ages = [self.feeds[n].age() for n in EVENT_FEEDS]
        stale = any(a is None or a > IDLE_MAX_AGE for a in ages)
        if stale:
            if wait:
                self.refresh(EVENT_FEEDS, max_age=IDLE_MAX_AGE, budget_s=8.0)
            else:
                self.refresh_async(EVENT_FEEDS, max_age=IDLE_MAX_AGE)
        d = self.data(EVENT_FEEDS)
        if all(v is None for v in d.values()):
            return []
        return [e for e in build_events(d, time.time()) if e["time"] >= since]

    def maintain(self, panel_active: bool, aurora_active: bool) -> None:
        if panel_active:
            self.refresh(PANEL)
        if aurora_active:
            self.refresh(("ovation", "kp_forecast"))


# ---------------------------------------------------------------------------------------------
# The AI tool's compact answer
# ---------------------------------------------------------------------------------------------
def ai_answer(snap: dict) -> dict:
    kp = snap["kp"]
    sc = snap.get("scales") or {}
    x = snap.get("xray") or {}
    w = snap.get("wind") or {}
    al = snap.get("alerts") or {}

    def lv(block, k):
        return None if not block else (block.get(k) or {}).get("level")

    def fl(f):
        return None if not f else {"class": f["class"], "peak_utc": iso(f["max"]), "radio_blackout": f"R{f['r']}" if f["r"] else None}

    out = {
        "summary": snap["summary"], "time_utc": iso(snap["time"]),
        "kp": {"now": (kp.get("now") or {}).get("kp"), "last_3h": (kp.get("last") or {}).get("kp"),
               "max_24h": (kp.get("max_24h") or {}).get("kp"),
               "forecast_max": None if not kp.get("forecast_max") else {
                   "kp": kp["forecast_max"]["kp"], "g": f"G{kp['forecast_max']['g']}", "at_utc": iso(kp["forecast_max"]["time"])},
               "next_days": [{"date": d["date"], "max_kp": d["kp"], "g": f"G{d['g']}"} for d in kp.get("days") or []]},
        "noaa_scales": {
            "now": {k: lv(sc.get("now"), k) for k in ("G", "S", "R")},
            "max_24h": {k: lv(sc.get("max_24h"), k) for k in ("G", "S", "R")},
            "forecast": [{"date": f["date"], "G": f["G"]["level"], "S1_or_more_pct": f["S"]["prob"],
                          "R1_R2_pct": f["R"]["minor_prob"], "R3_or_more_pct": f["R"]["major_prob"]}
                         for f in sc.get("forecast") or []]},
        "xray": {"now_class": x.get("now_class"), "ongoing": fl(x.get("ongoing")),
                 "recent": [fl(f) for f in x.get("recent") or []][:5], "biggest_7d": fl(x.get("biggest_7d"))},
        "solar_wind": {"speed_km_s": w.get("speed"), "density_cm3": w.get("density"), "bz_nT": w.get("bz"),
                       "bt_nT": w.get("bt"), "measured_utc": iso(w.get("time")), "reaches_earth_utc": iso(w.get("arrives")),
                       "bz_south_minutes_last_hour": w.get("south_minutes")},
        "alerts_in_force": [{"title": a["title"], "detail": a["detail"], "type": a["type"], "scale": a["scale"],
                             "issued_utc": iso(a["issued"]), "until_utc": iso(a["end"])} for a in al.get("active") or []],
        "aurora": snap.get("aurora") and {"north_max_pct": snap["aurora"]["north_max"],
                                          "south_max_pct": snap["aurora"]["south_max"],
                                          "forecast_for_utc": iso(snap["aurora"]["forecast"])},
        "links": [l["href"] for l in LINKS[:4]], "source": CREDIT,
    }
    return out


# ---------------------------------------------------------------------------------------------
# Wiring (called from the space module's register)
# ---------------------------------------------------------------------------------------------
_SEARCH_WORDS = {
    "aurora": "aurora", "auroras": "aurora", "aurora borealis": "aurora", "aurora australis": "aurora",
    "northern lights": "aurora", "southern lights": "aurora", "ovation": "aurora",
    "space weather": "now", "kp": "now", "kp index": "now", "geomagnetic": "now", "geomagnetic storm": "now",
    "solar storm": "now", "solar flare": "now", "solar flares": "now", "flare": "now", "flares": "now",
    "x-ray flux": "now", "radio blackout": "now", "radiation storm": "now", "solar wind": "now",
    "coronal mass ejection": "now", "cme": "now", "swpc": "now", "noaa scales": "now", "noaa space weather": "now",
}


def register(m, *, weather_at=None, state: dict | None = None) -> SpaceWeather:
    svc = SpaceWeather(m.net, m.log, status=m.status)
    if state is not None:
        state["spaceweather"] = svc

    m.source("space.swpc", "NOAA SWPC space weather (Kp, NOAA scales, X-ray flares, solar wind, alerts)", PAGES["overview"],
             "Public domain (US government work); credit NOAA SWPC",
             refresh="every 5 to 30 min while the panel is open; alerts, scales, flares and Kp every 30 min for events",
             layer="space.spaceweather")
    m.source("space.ovation", "NOAA SWPC OVATION aurora forecast", PAGES["aurora"],
             "Public domain (US government work); credit NOAA SWPC", refresh="every 10 min while the aurora layer is used",
             layer="space.aurora")

    def touch(key):
        try:
            m.touch(key)
        except Exception:
            pass

    @m.route("GET", "/api/space/spaceweather")
    def r_now(req):
        touch(PANEL_KEY)
        snap = svc.snapshot()
        if snap is None:
            raise HTTPError(502, "UPSTREAM", "NOAA SWPC is not answering right now.")
        return snap

    @m.route("GET", "/api/space/spaceweather/event/{eid}")
    def r_event(req):
        eid = req.params["eid"]
        if not _EVENT_ID.match(eid):
            raise HTTPError(400, "BAD_ARG", "That is not a space weather event id.")
        for e in svc.events(0.0, wait=True):
            if e["id"] == eid:
                return e
        raise HTTPError(404, "NOT_FOUND", "That event is no longer in SWPC's recent lists (they cover about a week).")

    @m.route("GET", "/api/space/aurora")
    def r_aurora(req):
        touch(AURORA_KEY)
        meta = svc.aurora_meta()
        if meta is None:
            raise HTTPError(502, "UPSTREAM", "The OVATION aurora forecast is unavailable right now.")
        return meta

    @m.route("GET", "/api/space/aurora/tiles/{z}/{x}/{y}")
    def r_tile(req):
        try:
            z = int(req.params["z"])
            x = int(req.params["x"])
            y = int(req.params["y"].removesuffix(".png"))
        except ValueError:
            raise HTTPError(400, "BAD_ARG", "Tile coordinates are whole numbers.") from None
        if not (0 <= z <= 6 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
            raise HTTPError(400, "BAD_ARG", "Tile coordinates are out of range (zoom 0 to 6).")
        touch(AURORA_KEY)
        g = svc.grid()
        if g is None:
            raise HTTPError(502, "UPSTREAM", "The OVATION aurora forecast is unavailable right now.")
        fresh = req.arg("v", "") == g.version
        return Response(g.tile(z, x, y), content_type="image/png", cache="max-age=600" if fresh else "no-cache",
                        etag=f'"ov{g.version}-{z}-{x}-{y}"', compressible=False)

    @m.route("GET", "/api/space/aurora/here")
    def r_here(req):
        lat, lon = req.latlon()
        touch(AURORA_KEY)
        return svc.here(lat, lon, weather_at)

    def events(since_ts: float):
        return svc.events(since_ts)

    m.events(events)

    def search(q: str, limit: int):
        ql = " ".join(q.strip().lower().split())
        if len(ql) < 2:
            return []
        kinds = {}
        for word, kind in _SEARCH_WORDS.items():
            if ql == word:
                kinds[kind] = max(kinds.get(kind, 0), 0.92)
            elif len(ql) >= 4 and word.startswith(ql):
                kinds[kind] = max(kinds.get(kind, 0), 0.72)
        if not kinds:
            return []
        snap = svc._snap[2] if svc._snap else None
        out = []
        if "aurora" in kinds:
            g = svc.feeds["ovation"].data
            sub = f"OVATION, up to {g.north_max}% north and {g.south_max}% south now" if g else "OVATION, 30 to 90 minutes ahead"
            out.append({"kind": "space_weather", "id": "swpc:aurora:forecast", "title": "Aurora forecast", "subtitle": sub,
                        "layer": "space.aurora", "score": kinds["aurora"]})
        out.append({"kind": "space_weather", "id": "swpc:now:panel", "title": "Space weather now",
                    "subtitle": (snap["summary"] if snap else "Kp, storms, flares, solar wind and SWPC alerts")[:140],
                    "layer": "space.spaceweather", "score": kinds.get("now", 0.6)})
        return out[:limit]

    m.search(search)

    def num(args, name, lo, hi):
        v = args.get(name)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            raise ValueError(f"{name} must be a number between {lo} and {hi}")
        return float(v)

    def ai_now(args):
        try:
            lat = num(args, "lat", -90, 90)
            lon = num(args, "lon", -180, 180)
            if (lat is None) != (lon is None):
                raise ValueError("give both lat and lon, or neither")
        except ValueError as err:
            return {"error": str(err)}
        snap = svc.snapshot(budget_s=8.0)
        if snap is None:
            return {"error": "NOAA SWPC is not answering right now."}
        out = ai_answer(snap)
        if lat is not None:
            h = svc.here(lat, lon, weather_at)
            g = svc.feeds["ovation"].data
            if out.get("aurora") is None and g is not None:
                out["aurora"] = {"north_max_pct": g.north_max, "south_max_pct": g.south_max, "forecast_for_utc": iso(g.forecast)}
            d = h["dark"]
            out["aurora_here"] = {
                "sentence": h["sentence"], "chance": h["chance"], "cgm_latitude": h["cgm_lat"],
                "kp_needed_overhead": h.get("kp_needed_overhead"), "kp_needed_horizon": h.get("kp_needed_horizon"),
                "kp_forecast_tonight": h.get("kp_tonight"), "ovation_here_pct": (h.get("ovation") or {}).get("here_pct"),
                "dark_now": d["dark_now"], "next_dark_from_utc": iso(d["start"]), "next_dark_until_utc": iso(d["end"]),
                "cloud_pct": (h.get("cloud") or {}).get("pct"),
                "note": "Kp rule of thumb from SWPC (oval edge near 66 - 2 Kp degrees CGM latitude); seen low on the "
                        "horizon about 3 degrees further. Darkness means the Sun 12 degrees or more below the horizon."}
        return out

    m.ai_tool("space_weather_now",
              "Space weather now from NOAA SWPC: Kp now, in the last day and forecast for three days, NOAA G (geomagnetic "
              "storm), S (radiation storm) and R (radio blackout) levels now and forecast, the X-ray level and recent "
              "solar flares, solar wind speed, density and Bz, SWPC alerts, watches and warnings in force (plain words) "
              "and the OVATION aurora maximum. Give lat and lon to add the aurora chance there tonight (OVATION, "
              "geomagnetic latitude, darkness and cloud).",
              {"type": "object", "properties": {
                  "lat": {"type": "number", "minimum": -90, "maximum": 90},
                  "lon": {"type": "number", "minimum": -180, "maximum": 180}},
               "additionalProperties": False}, ai_now)

    m.layer("space.aurora", "Aurora forecast", "Space",
            description="Chance of seeing the aurora overhead (NOAA OVATION, 30 to 90 minutes ahead) for both hemispheres, "
                        "with lines where it may be seen overhead or low on the horizon.",
            source="NOAA SWPC OVATION", refresh="every 10 min",
            use="Where the aurora is tonight and how far from the poles it reaches")
    m.layer("space.spaceweather", "Space weather now", "Space",
            description="Panel with Kp now and forecast, NOAA G, S and R levels, flares, solar wind and SWPC alerts.",
            source="NOAA SWPC", refresh="every 2 min while open",
            use="Geomagnetic storms, solar flares, radio blackouts and radiation storms")

    m.task("spaceweather", lambda: svc.maintain(m.active(PANEL_KEY, PANEL_ACTIVE_S), m.active(AURORA_KEY, AURORA_ACTIVE_S)),
           every=60, initial_delay=20,
           when=lambda: m.active(PANEL_KEY, PANEL_ACTIVE_S) or m.active(AURORA_KEY, AURORA_ACTIVE_S))
    return svc
