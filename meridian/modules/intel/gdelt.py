"""GDELT 2.0 Events: pure parsing and scoring (no network, no database).

The 15-minute export is a tab-separated file with 61 columns (GDELT 2.0 Event codebook). We keep
material conflict (QuadClass 4) with CAMEO roots 18 (assault), 19 (fight) and 20 (mass violence)
geocoded to a city or landmark (ActionGeo_Type 3 or 4) or, flagged as approximate, a province
(type 5). Country-level (1) and US-state (2) geocodes are too coarse to map and are dropped.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import hashlib
import io
import math
import re
import time
import urllib.parse
import zipfile
from typing import Iterable, Iterator

from . import codes

LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
EXPORT_URL = "https://data.gdeltproject.org/gdeltv2/{ts}.export.CSV.zip"
SLOT_SECONDS = 900
MAX_UNZIPPED = 64 * 1024 * 1024
MAX_LAG_DAYS = 2      # drop retrospective mentions of events that happened long before the report

# Column indices (GDELT 2.0 Event table)
C_ID, C_DAY = 0, 1
C_A1_CODE, C_A1_NAME, C_A1_COUNTRY, C_A1_GROUP = 5, 6, 7, 8
C_A1_TYPES = (12, 13, 14)
C_A2_CODE, C_A2_NAME, C_A2_COUNTRY, C_A2_GROUP = 15, 16, 17, 18
C_A2_TYPES = (22, 23, 24)
C_ROOT_EVENT, C_CODE, C_BASE, C_ROOT, C_QUAD, C_GOLDSTEIN = 25, 26, 27, 28, 29, 30
C_MENTIONS, C_SOURCES, C_ARTICLES, C_TONE = 31, 32, 33, 34
C_GEO_TYPE, C_GEO_NAME, C_GEO_COUNTRY, C_GEO_ADM1 = 51, 52, 53, 54
C_GEO_LAT, C_GEO_LON, C_GEO_FEATURE = 56, 57, 58
C_ADDED, C_URL = 59, 60
NCOLS = 61

CONFLICT_ROOTS = ("18", "19", "20")
_PRECISION = {"3": "city", "4": "city", "5": "province"}


# ---------------------------------------------------------------------------------------------
# File listing
# ---------------------------------------------------------------------------------------------
_RE_EXPORT = re.compile(r"/gdeltv2/(\d{14})\.export\.CSV\.zip\s*$", re.I)


def parse_lastupdate(text: str) -> str | None:
    """Timestamp (YYYYMMDDHHMMSS) of the newest export listed in lastupdate.txt."""
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            m = _RE_EXPORT.search(parts[2])
            if m:
                return m.group(1)
    return None


def export_url(ts: str) -> str:
    return EXPORT_URL.format(ts=ts)


def ts_to_epoch(ts: str) -> float:
    t = dt.datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
    return float(calendar.timegm(t.timetuple()))


def epoch_to_ts(epoch: float) -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(epoch))


def slot_floor(epoch: float) -> float:
    return math.floor(epoch / SLOT_SECONDS) * SLOT_SECONDS


def slots_back(latest_ts: str, hours: float) -> list[str]:
    """Export timestamps from ``latest_ts`` back ``hours`` (newest first, including latest)."""
    end = slot_floor(ts_to_epoch(latest_ts))
    n = int(hours * 3600 // SLOT_SECONDS) + 1
    return [epoch_to_ts(end - i * SLOT_SECONDS) for i in range(n)]


def read_export_zip(data: bytes) -> str:
    """The CSV text inside an export zip. Refuses odd archives (several members, huge sizes)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) != 1 or not infos[0].filename.lower().endswith(".csv"):
            raise ValueError("unexpected archive layout")
        if infos[0].file_size > MAX_UNZIPPED:
            raise ValueError("archive member too large")
        raw = zf.read(infos[0])
    return raw.decode("utf-8", errors="replace")


def iter_rows(text: str) -> Iterator[list[str]]:
    for row in csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(row) == NCOLS:
            yield row


# ---------------------------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------------------------
def _f(value: str, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _i(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _latlon(row: list[str]) -> tuple[float, float] | None:
    try:
        lat = float(row[C_GEO_LAT])
        lon = float(row[C_GEO_LON])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return None
    return lat, lon


def day_to_date(day: int) -> dt.date | None:
    try:
        return dt.date(day // 10000, day // 100 % 100, day % 100)
    except ValueError:
        return None


def day_to_epoch(day: int) -> float:
    d = day_to_date(day)
    return float(calendar.timegm(d.timetuple())) if d else 0.0


def epoch_to_day(epoch: float) -> int:
    t = time.gmtime(epoch)
    return t.tm_year * 10000 + t.tm_mon * 100 + t.tm_mday


def clean_place(name: str) -> str:
    """'Gaza, Israel (general), Israel' -> 'Gaza, Israel'; 'Warsaw, (PL67), Poland' -> 'Warsaw, Poland'."""
    out: list[str] = []
    for part in (name or "").split(","):
        p = part.strip()
        if not p or re.fullmatch(r"\([^)]*\)", p):
            continue
        p = re.sub(r"\s*\(general\)\s*$", "", p, flags=re.I).strip()
        if p and (not out or out[-1].lower() != p.lower()):
            out.append(p)
    return ", ".join(out)


def short_place(place: str) -> str:
    return (place or "").split(",")[0].strip()


def brief_place(place: str) -> str:
    """'Kyiv, Kyyiv, Misto, Ukraine' -> 'Kyiv, Ukraine' (first and last parts)."""
    parts = [p.strip() for p in (place or "").split(",") if p.strip()]
    if len(parts) <= 2:
        return ", ".join(parts)
    return f"{parts[0]}, {parts[-1]}"


# ---------------------------------------------------------------------------------------------
# Actors, classification and severity
# ---------------------------------------------------------------------------------------------
def _actor(row: list[str], side: int) -> dict:
    if side == 1:
        code, name, country, group, types = (row[C_A1_CODE], row[C_A1_NAME], row[C_A1_COUNTRY],
                                             row[C_A1_GROUP], [row[i] for i in C_A1_TYPES])
    else:
        code, name, country, group, types = (row[C_A2_CODE], row[C_A2_NAME], row[C_A2_COUNTRY],
                                             row[C_A2_GROUP], [row[i] for i in C_A2_TYPES])
    code = code.strip().upper()
    tset = {t.strip().upper() for t in types if t.strip()}
    tokens = {code[i:i + 3] for i in range(0, len(code), 3)} if len(code) % 3 == 0 else set()
    return {"code": code, "name": name.strip(), "country": country.strip().upper(), "group": group.strip().upper(),
            "types": tset | (tokens & (codes.ARMED_TYPES | codes.CRIME_TYPES | set(codes.ACTOR_TYPE))),
            "type": next(iter(sorted(tset)), "")}


def classify(code: str, root: str, a1: dict, a2: dict) -> tuple[str, bool, bool]:
    """(category, armed, crime). ``crime`` marks reports whose only typed actors are police,
    criminals or courts (likely crime or policing rather than armed conflict)."""
    types = a1["types"] | a2["types"]
    category = "attack" if (code in codes.ATTACK_CODES or root == "20") else ("fighting" if root == "19" else "assault")
    state = any(a["country"] and (not a["types"] or a["types"] <= {"GOV", "MIL"}) for a in (a1, a2) if a["code"])
    armed = bool(types & codes.ARMED_TYPES) or code in codes.HEAVY_CODES or (code in codes.STATE_FORCE_CODES and state)
    crime = (not armed) and bool(types & codes.CRIME_TYPES) and code not in codes.ATTACK_CODES
    return category, armed, crime


def _log_share(value: float, cap: float) -> float:
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(1.0 + value) / math.log10(1.0 + cap))


def conflict_severity(goldstein: float, root: str, code: str, mentions: int, sources: int, tone: float,
                      armed: bool, reports: int = 1, focus: float = 1.0) -> float:
    """Severity 0-10 for a (deduplicated) conflict event. See docs/modules/intel.md.

    ``focus`` (0.25-1) is the share of all GDELT reports at that place in the same export that are
    violent conflict, saturating at 30 %. It damps "dateline" places such as capitals, where unrelated
    stories pile up under one place name."""
    base = 0.2 * min(10.0, max(0.0, -goldstein))                   # up to 2.0
    s = {"18": 0.3, "19": 1.0, "20": 1.5}.get(root, 0.0)           # type of violence
    if code in codes.HEAVY_CODES:
        s += 1.0                                                     # heavy weapons, bombing, mass killing
    s += 2.5 * _log_share(mentions, 200)                            # media attention
    s += 1.5 * _log_share(sources, 30)                              # breadth of sources
    s += min(0.5, max(0.0, -tone) / 20.0)                           # strongly negative coverage
    if armed:
        s += 0.5
    s += 0.5 * _log_share(max(0, reports - 1), 20)                  # repeated reports of the same thing
    f = max(0.25, min(1.0, focus))
    return round(max(0.0, min(10.0, base + f * s)), 1)


def location_key(row: list[str]) -> str | None:
    """The place identity of a row: GDELT feature id, or the coordinates rounded to 0.01 degrees."""
    feature = row[C_GEO_FEATURE].strip()
    if feature:
        return f"f:{feature}"
    ll = _latlon(row)
    return f"p:{ll[0]:.2f},{ll[1]:.2f}" if ll else None


def focus_by_location(rows: list[list[str]], saturate: float = 0.30) -> dict[str, float]:
    """Per place: share of rows that are violent conflict (QuadClass 4, roots 18-20) / ``saturate``, capped at 1."""
    total: dict[str, int] = {}
    conflict: dict[str, int] = {}
    for r in rows:
        key = location_key(r)
        if key is None:
            continue
        total[key] = total.get(key, 0) + 1
        if r[C_QUAD].strip() == "4" and r[C_ROOT].strip() in CONFLICT_ROOTS:
            conflict[key] = conflict.get(key, 0) + 1
    return {k: min(1.0, (conflict.get(k, 0) / n) / saturate) for k, n in total.items() if conflict.get(k)}


def dedupe_key(root: str, lat: float, lon: float, day: int) -> str:
    return f"{root}:{round(lat, 1):.1f}:{round(lon, 1):.1f}:{day}"


def event_id(dkey: str) -> str:
    return "gd" + hashlib.sha1(dkey.encode("utf-8")).hexdigest()[:14]


def conflict_row(row: list[str], *, iso_of=None, focus: dict[str, float] | None = None) -> dict | None:
    """A kept conflict report as a dict, or None. ``iso_of(lat, lon, fips, iso2) -> iso2`` may refine
    the country (e.g. Gaza places that GDELT files under Israel); ``focus`` comes from
    :func:`focus_by_location` over the whole export."""
    if row[C_QUAD].strip() != "4" or row[C_ROOT].strip() not in CONFLICT_ROOTS:
        return None
    precision = _PRECISION.get(row[C_GEO_TYPE].strip())
    if precision is None:
        return None
    ll = _latlon(row)
    if ll is None:
        return None
    day = _i(row[C_DAY])
    added = row[C_ADDED].strip()
    if not re.fullmatch(r"\d{14}", added):
        return None
    t = ts_to_epoch(added)
    d_event, d_added = day_to_date(day), day_to_date(int(added[:8]))
    if d_event is None or d_added is None or (d_added - d_event).days > MAX_LAG_DAYS or d_event > d_added:
        return None
    lat, lon = ll
    code = row[C_CODE].strip()
    root = row[C_ROOT].strip()
    a1, a2 = _actor(row, 1), _actor(row, 2)
    category, armed, crime = classify(code, root, a1, a2)
    fips = row[C_GEO_COUNTRY].strip().upper()
    iso2 = codes.fips_to_iso2(fips)
    if iso_of is not None:
        try:
            iso2 = iso_of(lat, lon, fips, iso2) or iso2
        except Exception:
            pass
    url = row[C_URL].strip()
    if not url.lower().startswith(("http://", "https://")):
        url = ""
    mentions = max(0, _i(row[C_MENTIONS]))
    sources = max(0, _i(row[C_SOURCES]))
    tone = _f(row[C_TONE])
    goldstein = _f(row[C_GOLDSTEIN])
    ev = {
        "gid": row[C_ID].strip(), "day": day, "t": t, "root": root, "code": code,
        "quad": 4, "goldstein": goldstein, "category": category, "armed": armed, "crime": crime,
        "precision": precision, "lat": round(lat, 5), "lon": round(lon, 5),
        "place": clean_place(row[C_GEO_NAME]), "fips": fips, "iso2": iso2, "adm1": row[C_GEO_ADM1].strip(),
        "actor1": codes.actor_label(a1["name"], a1["type"]), "actor2": codes.actor_label(a2["name"], a2["type"]),
        "mentions": mentions, "sources": sources, "articles": max(0, _i(row[C_ARTICLES])), "tone": tone,
        "url": url,
        "focus": round(focus.get(location_key(row) or "", 1.0), 3) if focus is not None else 1.0,
    }
    ev["dkey"] = dedupe_key(root, lat, lon, day)
    ev["id"] = event_id(ev["dkey"])
    ev["severity"] = conflict_severity(goldstein, root, code, mentions, sources, tone, armed, focus=ev["focus"])
    return ev


def new_event(row_ev: dict) -> dict:
    """A stored event built from its first report."""
    ev = {k: row_ev[k] for k in ("id", "dkey", "day", "root", "code", "quad", "goldstein", "category", "armed",
                                 "crime", "precision", "lat", "lon", "place", "fips", "iso2", "adm1", "actor1",
                                 "actor2", "mentions", "sources", "articles", "tone", "severity", "focus")}
    ev["t_first"] = ev["t_last"] = row_ev["t"]
    m = max(1, row_ev["mentions"])
    ev["actm"] = {a: m for a in (row_ev["actor1"], row_ev["actor2"]) if a}
    ev["urlm"] = {row_ev["url"]: m} if row_ev["url"] else {}
    ev["url_main"] = row_ev["url"] or None
    ev["main_mentions"] = row_ev["mentions"]
    ev["gids"] = [row_ev["gid"]] if row_ev["gid"] else []
    ev["reports"] = 1
    _rank(ev)
    return ev


def _top(d: dict, n: int) -> dict:
    return dict(sorted(d.items(), key=lambda kv: -kv[1])[:n])


def _rank(ev: dict) -> None:
    """Derive the shown lists: the main report's URL first, then the most-mentioned ones."""
    ev["actm"] = _top(ev.get("actm") or {}, 12)
    ev["urlm"] = _top(ev.get("urlm") or {}, 12)
    main = [a for a in (ev.get("actor1"), ev.get("actor2")) if a]
    ev["actors"] = (main + [a for a in ev["actm"] if a not in main])[:6]
    first = [ev["url_main"]] if ev.get("url_main") else []
    ev["urls"] = (first + [u for u in ev["urlm"] if u not in first])[:5]


def merge_event(ev: dict, row_ev: dict, severity_fn=None) -> bool:
    """Fold another report into a stored event (same dedupe key). False if it was already counted.
    ``severity_fn(ev)`` rescores the merged event (default: :func:`conflict_severity`)."""
    gid = row_ev.get("gid")
    if gid and gid in ev["gids"]:
        return False
    if gid:
        ev["gids"] = (ev["gids"] + [gid])[-20:]
    w_old = max(1, ev["mentions"])
    w_new = max(1, row_ev["mentions"])
    ev["tone"] = round((ev["tone"] * w_old + row_ev["tone"] * w_new) / (w_old + w_new), 3)
    ev["focus"] = round((ev.get("focus", 1.0) * w_old + row_ev.get("focus", 1.0) * w_new) / (w_old + w_new), 3)
    ev["mentions"] += row_ev["mentions"]
    ev["sources"] += row_ev["sources"]
    ev["articles"] += row_ev["articles"]
    ev["reports"] += 1
    ev["goldstein"] = min(ev["goldstein"], row_ev["goldstein"])
    ev["t_first"] = min(ev["t_first"], row_ev["t"])
    ev["t_last"] = max(ev["t_last"], row_ev["t"])
    new_w, old_w = codes.code_weight(row_ev["code"]), codes.code_weight(ev["code"])
    if new_w > old_w or (new_w == old_w and row_ev["mentions"] > ev.get("main_mentions", 0)):
        ev["code"] = row_ev["code"]
        ev["category"] = row_ev["category"]
        ev["actor1"], ev["actor2"] = row_ev["actor1"], row_ev["actor2"]
        ev["url_main"] = row_ev["url"] or ev.get("url_main")
        ev["main_mentions"] = row_ev["mentions"]
    m = max(1, row_ev["mentions"])
    actm = ev.setdefault("actm", {a: 1 for a in ev.get("actors") or []})
    for a in (row_ev["actor1"], row_ev["actor2"]):
        if a:
            actm[a] = actm.get(a, 0) + m
    urlm = ev.setdefault("urlm", {u: 1 for u in ev.get("urls") or []})
    if row_ev["url"]:
        urlm[row_ev["url"]] = max(urlm.get(row_ev["url"], 0), m)
    _rank(ev)
    ev["armed"] = bool(ev["armed"] or row_ev["armed"])
    ev["crime"] = bool(ev["crime"] and row_ev["crime"])
    if ev["precision"] != "city" and row_ev["precision"] == "city":
        ev["precision"] = "city"
    if severity_fn is not None:
        ev["severity"] = severity_fn(ev)
    else:
        ev["severity"] = conflict_severity(ev["goldstein"], ev["root"], ev["code"], ev["mentions"], ev["sources"],
                                           ev["tone"], ev["armed"], ev["reports"], ev.get("focus", 1.0))
    return True


# ---------------------------------------------------------------------------------------------
# News points: one source article per place (latest wins)
# ---------------------------------------------------------------------------------------------
STRONG_WORDS = frozenset("""killed kills kill dead death deaths massacre airstrike airstrikes missile missiles bombing
bombings bomb bombs explosion explosions blast blasts shelling invasion offensive coup earthquake tsunami hostage
hostages terror terrorist terrorists genocide war executed beheaded""".split())
MODERATE_WORDS = frozenset("""attack attacks attacked strike strikes drone drones clash clashes fighting fight protest
protests riot riots troops military army ceasefire sanctions evacuate evacuation evacuated flood floods wildfire
outbreak shooting shot wounded injured arrested rebels militants insurgents gunmen raid raids siege refugees
displaced famine casualties frontline battle navy warship warships militia jihadist jihadists""".split())

_ACRONYMS = {"us": "US", "uk": "UK", "un": "UN", "eu": "EU", "nato": "NATO", "idf": "IDF", "rsf": "RSF",
             "drc": "DRC", "isis": "ISIS", "pla": "PLA", "m23": "M23", "usa": "USA", "uae": "UAE", "saf": "SAF",
             "ai": "AI", "who": "WHO", "icc": "ICC", "iaea": "IAEA", "fbi": "FBI", "cia": "CIA", "gdp": "GDP"}
_RE_EXT = re.compile(r"\.(s?html?|php\d?|aspx?|jsp|cms|ece|amp|story)$", re.I)
_RE_WORD = re.compile(r"[A-Za-z][A-Za-z']*|\d+")


def _id_like(token: str) -> bool:
    return len(token) >= 6 and bool(re.search(r"\d", token)) and bool(re.search(r"[a-z]", token, re.I))


def domain_of(url: str) -> str:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def slug_title(url: str, proper: Iterable[str] = ()) -> str | None:
    """A readable headline guessed from the link's slug, or None when the link has no words
    (e.g. '/article/sjzfnxejqe'). Sentence case; known acronyms and ``proper`` words capitalised."""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return None
    best: list[str] = []
    for seg in path.split("/"):
        seg = _RE_EXT.sub("", urllib.parse.unquote(seg or "")).strip()
        if not seg or "-" not in seg and "_" not in seg and "+" not in seg:
            continue
        raw = [w for w in re.split(r"[-_+.]+", seg) if w]
        words = []
        for w in raw:
            if _id_like(w):
                continue
            words.extend(_RE_WORD.findall(w))
        while words and words[-1].isdigit() and len(words[-1]) >= 4:
            words.pop()
        while words and words[0].isdigit() and len(words[0]) >= 4:
            words.pop(0)
        alpha = [w for w in words if w.isalpha() and len(w) >= 2]
        if len(alpha) >= 4 and len(words) > len(best):
            best = words
    if not best:
        return None
    proper_set = {p.lower() for p in proper if p}
    out = []
    for i, w in enumerate(best[:24]):
        lw = w.lower()
        if lw in _ACRONYMS:
            out.append(_ACRONYMS[lw])
        elif lw in proper_set:
            out.append(lw[:1].upper() + lw[1:])
        else:
            out.append(lw)
    title = " ".join(out)
    return title[:1].upper() + title[1:]


def keyword_hits(title: str | None) -> tuple[int, int]:
    words = re.findall(r"[a-z']+", (title or "").lower())
    strong = sum(1 for w in words if w in STRONG_WORDS)
    moderate = sum(1 for w in words if w in MODERATE_WORDS)
    return strong, moderate


def news_severity(quad: int, goldstein: float, tone: float, mentions: int, title: str | None) -> float:
    """Severity 0-10 for a geolocated news point. See docs/modules/intel.md."""
    s = 1.0
    s += {4: 3.0, 3: 1.8, 2: 0.5}.get(quad, 0.0)
    s += 0.15 * min(10.0, max(0.0, -goldstein))
    s += min(1.5, max(0.0, -tone) / 6.0)
    s += 1.0 * _log_share(mentions, 100)
    strong, moderate = keyword_hits(title)
    kw = (2.0 if strong >= 1 else 0.0) + (1.0 if strong >= 2 else 0.0)
    kw += (0.8 if moderate >= 1 else 0.0) + (0.4 if moderate >= 2 else 0.0)
    s += min(3.0, kw)
    return round(max(0.0, min(10.0, s)), 1)


def doc_severity(title: str | None, outlets: int = 1) -> float:
    """Severity 0-10 for a DOC API headline (no tone or geocoding available)."""
    strong, moderate = keyword_hits(title)
    s = 2.0 + 2.2 * min(2, strong) + 1.0 * min(2, moderate)
    if outlets > 1:
        s += min(1.5, 0.75 * math.log2(outlets))
    return round(max(0.0, min(10.0, s)), 1)


def news_id(lkey: str) -> str:
    return "gn" + hashlib.sha1(lkey.encode("utf-8")).hexdigest()[:14]


def news_row(row: list[str], *, iso_of=None, proper: Iterable[str] = ()) -> dict | None:
    """A news point candidate (precise geocode and an http(s) link), or None."""
    if row[C_GEO_TYPE].strip() not in ("3", "4"):
        return None
    ll = _latlon(row)
    url = row[C_URL].strip()
    added = row[C_ADDED].strip()
    if ll is None or not url.lower().startswith(("http://", "https://")) or len(url) > 1000:
        return None
    if not re.fullmatch(r"\d{14}", added):
        return None
    lat, lon = ll
    place = clean_place(row[C_GEO_NAME])
    feature = row[C_GEO_FEATURE].strip()
    lkey = f"f:{feature}" if feature else f"p:{lat:.2f},{lon:.2f}"
    words = set(proper) | set(re.findall(r"[A-Za-z]+", place))
    title = slug_title(url, words)
    quad = _i(row[C_QUAD])
    goldstein = _f(row[C_GOLDSTEIN])
    tone = _f(row[C_TONE])
    mentions = max(0, _i(row[C_MENTIONS]))
    fips = row[C_GEO_COUNTRY].strip().upper()
    iso2 = codes.fips_to_iso2(fips)
    if iso_of is not None:
        try:
            iso2 = iso_of(lat, lon, fips, iso2) or iso2
        except Exception:
            pass
    return {"id": news_id(lkey), "lkey": lkey, "url": url, "title": title, "domain": domain_of(url),
            "titled": title is not None, "lat": round(lat, 5), "lon": round(lon, 5), "place": place, "iso2": iso2,
            "t": ts_to_epoch(added), "quad": quad, "goldstein": goldstein, "tone": tone, "mentions": mentions,
            "code": row[C_CODE].strip(), "root": row[C_ROOT].strip(),
            "severity": news_severity(quad, goldstein, tone, mentions, title)}


def cell_of_row(row: list[str], cell: float = 0.5) -> str | None:
    """'r:c' of the 0.5-degree cell of a row geocoded to a city, landmark or province (not a country)."""
    if row[C_GEO_TYPE].strip() not in ("3", "4", "5"):
        return None
    ll = _latlon(row)
    if ll is None:
        return None
    return f"{int(math.floor(ll[0] / cell))}:{int(math.floor(ll[1] / cell))}"


def tally_row(row: list[str]) -> tuple[str | None, int, float, float, bool]:
    """(iso2, day, tone, weight, is_conflict) for the per-country daily tallies (any geocode)."""
    iso2 = codes.fips_to_iso2(row[C_GEO_COUNTRY])
    day = _i(row[C_ADDED][:8]) if len(row[C_ADDED]) >= 8 else 0
    weight = float(max(1, _i(row[C_ARTICLES], 1)))
    conflict = row[C_QUAD].strip() == "4" and row[C_ROOT].strip() in CONFLICT_ROOTS
    return iso2, day, _f(row[C_TONE]), weight, conflict


def hour_of_row(row: list[str]) -> int:
    """Epoch seconds of the UTC hour the row was added in (0 when DATEADDED is malformed)."""
    added = row[C_ADDED].strip()
    if not re.fullmatch(r"\d{14}", added):
        return 0
    try:
        return int(ts_to_epoch(added[:10] + "0000"))
    except ValueError:
        return 0


def is_protest_row(row: list[str]) -> bool:
    return row[C_ROOT].strip() == "14"
