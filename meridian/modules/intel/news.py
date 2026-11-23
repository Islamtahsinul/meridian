"""News: monitored theatres, GDELT DOC 2.0 headline feeds and the geolocated GDELT reports.

DOC API politeness: at most one request per 5 s (we space them 6 s apart), each theatre cached for
20 minutes, polled only while the browser uses the module, and a 429 backs the whole feed off for
10 minutes, doubling up to 2 hours. When DOC is unavailable, theatre feeds still list the
geolocated GDELT event reports inside each theatre (headlines guessed from the link).
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time

from . import gdelt

DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
DOC_SITE = "https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/"
DOC_HOST = "api.gdeltproject.org"
DOC_INTERVAL_S = 6.0
DOC_TTL_S = 1200.0
DOC_BACKOFF_S = 600.0
DOC_BACKOFF_MAX_S = 7200.0
DOC_MAX_RECORDS = 50

# boxes: [west, south, east, north, allowed iso2 list or None for any]; None in the list = at sea
THEATRES = [
    {"id": "ukraine-russia", "name": "Ukraine and Russia", "lat": 48.6, "lon": 35.5, "zoom": 5.3,
     "countries": ["UA"], "boxes": [[33.5, 50.0, 40.8, 53.6, ["RU"]], [30.0, 43.0, 40.0, 47.5, [None, "RU", "UA"]]],
     "query": "(Ukraine OR Ukrainian OR Kyiv OR Kharkiv OR Donbas OR Zaporizhzhia) (Russia OR Russian OR attack OR strike OR "
              "drone OR missile OR shelling OR frontline) sourcelang:english"},
    {"id": "israel-gaza-lebanon", "name": "Israel, Gaza and Lebanon", "lat": 32.3, "lon": 35.2, "zoom": 6.8,
     "countries": ["IL", "PS", "LB"], "boxes": [[35.6, 32.4, 36.6, 33.5, ["SY"]]],
     "query": "(Gaza OR Israel OR Israeli OR Hezbollah OR Lebanon OR \"West Bank\") (strike OR attack OR killed OR military "
              "OR ceasefire OR hostages OR troops) sourcelang:english"},
    {"id": "red-sea-yemen", "name": "Red Sea and Yemen", "lat": 15.5, "lon": 43.0, "zoom": 5.0,
     "countries": ["YE"], "boxes": [[32.0, 11.5, 44.5, 30.0, [None]], [43.0, 10.5, 54.0, 16.0, [None]]],
     "query": "(Yemen OR Houthi OR Houthis OR \"Red Sea\" OR \"Gulf of Aden\" OR Hodeidah OR \"Bab el-Mandeb\") sourcelang:english"},
    {"id": "sudan", "name": "Sudan", "lat": 14.5, "lon": 30.0, "zoom": 5.0, "countries": ["SD"], "boxes": [],
     "query": "(Sudan OR Sudanese OR Khartoum OR Darfur OR \"El Fasher\" OR Kordofan OR RSF) sourcelang:english"},
    {"id": "sahel", "name": "Central Sahel", "lat": 15.0, "lon": 0.5, "zoom": 4.6, "countries": ["ML", "BF", "NE"],
     "boxes": [],
     "query": "(Mali OR \"Burkina Faso\" OR Niger OR Sahel OR JNIM) (attack OR jihadist OR jihadists OR army OR militants OR "
              "killed OR junta) sourcelang:english"},
    {"id": "myanmar", "name": "Myanmar", "lat": 20.8, "lon": 96.0, "zoom": 5.2, "countries": ["MM"], "boxes": [],
     "query": "(Myanmar OR Burma OR \"Arakan Army\" OR Rakhine OR \"Shan State\" OR Tatmadaw) sourcelang:english"},
    {"id": "taiwan-scs", "name": "Taiwan Strait and South China Sea", "lat": 18.5, "lon": 116.0, "zoom": 4.4,
     "countries": ["TW"], "boxes": [[109.5, 4.0, 121.0, 21.5, [None]], [117.5, 21.5, 122.5, 26.5, [None]]],
     "query": "(\"Taiwan Strait\" OR \"South China Sea\" OR Taiwan OR \"Scarborough Shoal\" OR \"Second Thomas Shoal\" OR "
              "Spratly) (China OR Chinese OR PLA OR navy OR \"coast guard\" OR military) sourcelang:english"},
    {"id": "dr-congo", "name": "DR Congo", "lat": -1.8, "lon": 28.8, "zoom": 6.0, "countries": ["CD"], "boxes": [],
     "query": "(Congo OR DRC OR Goma OR Kivu OR M23 OR Bukavu OR Ituri) sourcelang:english"},
]
THEATRE_BY_ID = {t["id"]: t for t in THEATRES}

_GENERIC_PLACES = {"north", "south", "east", "west", "central", "city", "region", "province", "state", "district",
                   "national", "republic", "new", "port", "the", "gulf", "strait", "island", "islands", "sea"}


def theatre_bbox(th: dict, countries_bbox) -> list[float] | None:
    boxes = [b[:4] for b in th.get("boxes") or []]
    for iso in th.get("countries") or []:
        bb = countries_bbox(iso) if countries_bbox else None
        if bb:
            boxes.append(list(bb))
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def in_theatre(th: dict, lat: float, lon: float, iso2: str | None) -> bool:
    if iso2 and iso2 in th.get("countries", ()):
        return True
    for w, s, e, n, allowed in th.get("boxes") or ():
        if s <= lat <= n and w <= lon <= e and (allowed is None or iso2 in allowed):
            return True
    return False


def theatres_of(lat: float, lon: float, iso2: str | None) -> list[str]:
    return [t["id"] for t in THEATRES if in_theatre(t, lat, lon, iso2)]


# ---------------------------------------------------------------------------------------------
# DOC API
# ---------------------------------------------------------------------------------------------
def doc_params(th: dict) -> dict:
    return {"query": th["query"], "mode": "artlist", "format": "json", "maxrecords": DOC_MAX_RECORDS,
            "timespan": "1d", "sort": "datedesc"}


def parse_seen(value: str | None) -> float | None:
    m = re.fullmatch(r"(\d{8})T(\d{6})Z", (value or "").strip())
    if not m:
        return None
    try:
        return gdelt.ts_to_epoch(m.group(1) + m.group(2))
    except ValueError:
        return None


def _norm_title(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return " ".join(words[:9])


def doc_item_id(url: str) -> str:
    return "dn" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:14]


def parse_doc(payload) -> list[dict]:
    """Articles from a DOC 2.0 artlist JSON response (deduplicated by headline)."""
    arts = payload.get("articles") if isinstance(payload, dict) else None
    if arts is None:
        return []
    if not isinstance(arts, list):
        raise ValueError("DOC response has no article list")
    groups: dict[str, list[dict]] = {}
    for a in arts:
        if not isinstance(a, dict):
            continue
        url = (a.get("url") or "").strip()
        title = re.sub(r"\s+", " ", (a.get("title") or "")).strip()
        if not url.lower().startswith(("http://", "https://")) or not title:
            continue
        item = {"id": doc_item_id(url), "url": url[:1000], "title": title[:300],
                "domain": (a.get("domain") or gdelt.domain_of(url))[:120], "t": parse_seen(a.get("seendate")),
                "language": (a.get("language") or "")[:40], "source_country": (a.get("sourcecountry") or "")[:60]}
        groups.setdefault(_norm_title(title), []).append(item)
    out = []
    for items in groups.values():
        items.sort(key=lambda i: i["t"] or 0)
        first = dict(items[0])
        first["outlets"] = len({i["domain"] for i in items})
        first["also"] = [{"domain": i["domain"], "url": i["url"]} for i in items[1:6]]
        first["severity"] = gdelt.doc_severity(first["title"], first["outlets"])
        out.append(first)
    out.sort(key=lambda i: -(i["t"] or 0))
    return out


def build_gazetteer(places: list[dict], th: dict) -> dict[str, tuple[float, float, int]]:
    """name -> (lat, lon, weight) from recent event and news places inside a theatre."""
    gaz: dict[str, tuple[float, float, int]] = {}
    for p in places:
        lat, lon = p.get("lat"), p.get("lon")
        if lat is None or lon is None or not in_theatre(th, lat, lon, p.get("iso2")):
            continue
        name = (p.get("place") or "").split(",")[0].strip()
        key = name.lower()
        if len(key) < 4 or key in _GENERIC_PLACES or not re.fullmatch(r"[a-z' .-]+", key):
            continue
        n = int(p.get("n") or 1)
        if key not in gaz or n > gaz[key][2]:
            gaz[key] = (float(lat), float(lon), n)
    return gaz


def locate_title(title: str, gaz: dict[str, tuple[float, float, int]]) -> tuple[str, float, float] | None:
    low = " " + re.sub(r"[^a-z' .-]+", " ", (title or "").lower()) + " "
    best = None
    for name, (lat, lon, n) in gaz.items():
        if f" {name} " in low or f" {name}'s " in low:
            score = (len(name.split()), n, len(name))
            if best is None or score > best[0]:
                best = (score, name, lat, lon)
    if not best:
        return None
    return best[1].title(), best[2], best[3]


class DocFeeds:
    """Cached DOC headlines per theatre with a shared back-off. Thread-safe."""

    def __init__(self, net, log, status_fn=None):
        self.net = net
        self.log = log
        self.status = status_fn or (lambda **kw: None)
        self.lock = threading.Lock()
        self.cache: dict[str, dict] = {}            # theatre id -> {"t": fetched, "items": [...], "error": str|None}
        self.backoff_until = 0.0
        self.backoff = DOC_BACKOFF_S
        self.last_error: str | None = None
        self.reason: str | None = None              # rate_limited | unavailable | bad_response
        self.items_by_id: dict[str, dict] = {}
        self.wanted: dict[str, float] = {}          # theatre id -> last time the browser asked

    def want(self, theatre_id: str | None) -> None:
        with self.lock:
            now = time.time()
            if theatre_id:
                self.wanted[theatre_id] = now
            else:
                for t in THEATRES:
                    self.wanted.setdefault(t["id"], now)

    def next_due(self, now: float) -> dict | None:
        with self.lock:
            if now < self.backoff_until:
                return None
            due = []
            for th in THEATRES:
                c = self.cache.get(th["id"])
                age = now - c["t"] if c else math.inf
                if age >= DOC_TTL_S:
                    due.append((-(self.wanted.get(th["id"], 0)), -age, th["id"]))
            if not due:
                return None
            due.sort()
            return THEATRE_BY_ID[due[0][2]]

    def fetch(self, th: dict) -> bool:
        from meridian.net import NetError
        try:
            payload = self.net.get_json(DOC_URL, params=doc_params(th), timeout=25, retries=0)
            items = parse_doc(payload)
        except NetError as err:
            with self.lock:
                if err.status == 429:
                    self.backoff_until = time.time() + self.backoff
                    self.last_error = "GDELT DOC is rate limiting this network (HTTP 429)"
                    self.reason = "rate_limited"
                    self.backoff = min(DOC_BACKOFF_MAX_S, self.backoff * 2)
                else:
                    self.backoff_until = time.time() + 300
                    self.last_error = f"GDELT DOC unavailable: {err}"
                    self.reason = "unavailable"
                # keep older items but mark the attempt so other theatres get their turn
                old = self.cache.get(th["id"]) or {"items": []}
                self.cache[th["id"]] = {"t": time.time() - DOC_TTL_S / 2, "items": old["items"], "error": self.last_error}
            self.status(ok=False, error=self.last_error)
            return False
        except ValueError as err:
            with self.lock:
                self.last_error = f"GDELT DOC returned an unexpected response ({err})"
                self.reason = "bad_response"
                old = self.cache.get(th["id"]) or {"items": []}
                self.cache[th["id"]] = {"t": time.time(), "items": old["items"], "error": self.last_error}
            self.status(ok=False, error=self.last_error)
            return False
        with self.lock:
            for it in items:
                it["theatre"] = th["id"]
            self.cache[th["id"]] = {"t": time.time(), "items": items, "error": None}
            self.backoff = DOC_BACKOFF_S
            self.last_error = None
            self.reason = None
            self.items_by_id = {it["id"]: it for c in self.cache.values() for it in c["items"]}
            total = sum(len(c["items"]) for c in self.cache.values())
        self.status(ok=True, count=total)
        return True

    def items(self, theatre_id: str) -> tuple[list[dict], dict | None]:
        with self.lock:
            c = self.cache.get(theatre_id)
            return (list(c["items"]) if c else []), (dict(t=c["t"], error=c["error"]) if c else None)

    def get(self, item_id: str) -> dict | None:
        with self.lock:
            it = self.items_by_id.get(item_id)
            return dict(it) if it else None

    def state(self) -> dict:
        with self.lock:
            now = time.time()
            return {"backoff_s": max(0, round(self.backoff_until - now)), "error": self.last_error, "reason": self.reason,
                    "fetched": sum(1 for v in self.cache.values() if v["items"]),
                    "cached": {k: {"age_s": round(now - v["t"]), "items": len(v["items"])} for k, v in self.cache.items()}}

