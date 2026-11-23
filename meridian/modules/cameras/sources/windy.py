"""Windy Webcams API v3 (optional, needs WINDY_WEBCAMS_KEY): about 69,000 webcams in nearly every
country, listed by windy.com. Terms: https://api.windy.com/webcams/terms

The free tier lists at most 50 webcams per call and at most 1,050 per query (offset 1,000), and its
image links expire after 10 to 15 minutes. So this source works differently from the others:

- **Catalogue**: a polite crawl in its own dataset (``cameras.windy``, checked daily, crawled at most
  weekly) so it never holds up the other sources. One query per country; a country with more than
  1,000 webcams is split into areas: bounding boxes around where its webcams are, split in four
  until each holds at most 1,000, plus four bands around them so nothing outside is missed. One
  request a second at most, every page cached on disk (gzipped) for a week, so an interrupted crawl
  resumes where it stopped. The result is ``windy.json``; the camera build reads it (no network).
- **Pictures**: never proxied. The browser asks ``/api/cameras/direct`` for fresh image links
  (looked up here, server-side, with the key; reused for five minutes), shows the image itself at
  its own size or smaller, links it to the webcam's Windy page and shows the courtesy line
  "Webcams provided by Windy.com, add a webcam". The key is only ever sent to api.windy.com, in the
  ``x-windy-api-key`` header; links handed to the browser never contain it.
- Indoor webcams are left out (Meridian shows places and weather, not people's rooms).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

from meridian.net import NetError

from ..common import (FetchContext, NeedsKey, Pending, Source, clean, decode_text, http_url, make_camera,
                      parse_json_bytes, read_json, to_float, to_int)

KEY = "WINDY_WEBCAMS_KEY"
API = "https://api.windy.com/webcams/api/v3"
LIST_URL = f"{API}/webcams"
DETAIL_URL = f"{API}/webcams/{{id}}"
COUNTRIES_URL = f"{API}/countries"
PAGE = 50                     # most webcams per call
CAP = 1000                    # a query with more than this is split (the free tier stops at offset 1,000)
SPACING_S = 1.0               # at most about one crawl request a second
PAGE_MAX_AGE_H = 6.5 * 24     # cached pages are reused for a week, so the weekly crawl re-fetches them
FORCE_MAX_AGE_H = 20.0        # even a forced rebuild reuses pages from the last 20 hours
RECRAWL_DAYS = 7
MAX_REQUESTS = 5000           # per crawl run; the whole catalogue needs about 1,500
MAX_SECONDS = 3 * 3600
MAX_DEPTH = 14
INCLUDE = "location,categories,urls"
CRAWL_FILE = "windy.json"
WORLD = (90.0, 180.0, -90.0, -180.0)        # north, east, south, west (Windy's bbox order)
FIELDS = ["id", "title", "lat", "lon", "city", "region", "country", "categories", "page"]
SKIP_CATEGORIES = {"indoor"}
_CATEGORY_TAGS = {"beach": "coastal", "coast": "coastal", "port": "port", "harbor": "port", "airport": "airport",
                  "mountain": "mountain", "city": "urban", "square": "urban", "building": "urban",
                  "traffic": "traffic", "meteo": "weather", "river": "river"}
COURTESY = (("Webcams provided by Windy.com", "https://windy.com"),
            ("add a webcam", "https://www.windy.com/webcams/add"))


class KeyRefused(Exception):
    """Windy refused the key (HTTP 401 or 403): the crawl stops."""


class Budget(Exception):
    """The crawl used its request or time budget for this run; it resumes next time."""


# ---------------------------------------------------------------------------------------------
# Webcam records
# ---------------------------------------------------------------------------------------------
def compact(cam: dict) -> list | None:
    """One active, outdoor webcam from an API page as a small record (see ``FIELDS``), or None."""
    if not isinstance(cam, dict) or (cam.get("status") or "active") != "active":
        return None
    wid = to_int(cam.get("webcamId"))
    loc = cam.get("location") if isinstance(cam.get("location"), dict) else {}
    lat, lon = to_float(loc.get("latitude")), to_float(loc.get("longitude"))
    if wid is None or wid <= 0 or lat is None or lon is None:
        return None
    cats = sorted({c.get("id") for c in cam.get("categories") or [] if isinstance(c, dict) and isinstance(c.get("id"), str)})
    if SKIP_CATEGORIES & set(cats):
        return None
    urls = cam.get("urls") if isinstance(cam.get("urls"), dict) else {}
    return [wid, clean(cam.get("title"), 140), round(lat, 5), round(lon, 5), clean(loc.get("city"), 100),
            clean(loc.get("region"), 80), clean(loc.get("country_code"))[:2].upper(), ",".join(cats)[:120],
            clean(urls.get("detail"), 300)]


def row_for(rec: list, src: Source) -> dict | None:
    """A camera row (``make_camera``) from a compact record."""
    if not isinstance(rec, list) or len(rec) < len(FIELDS):
        return None
    wid, title, lat, lon, city, region, country, cats, page = rec[:len(FIELDS)]
    categories = [c for c in str(cats or "").split(",") if c]
    tags = [_CATEGORY_TAGS[c] for c in categories if c in _CATEGORY_TAGS]
    page_url = page if isinstance(page, str) and _windy_url(page) else None
    # Windy's own "traffic" category (two thirds of its list, mostly road-agency feeds it re-lists)
    # marks road cameras; everything else is a webcam (scenic, weather, city)
    return make_camera(src, wid, title or f"Webcam {wid}", lat, lon, direct=True, page=page_url,
                       country=(country or None), region=region or None, area=city or None, tags=tags,
                       camera_type="traffic" if "traffic" in categories else "webcam")


def parse(data, src: Source) -> tuple[list[dict], int]:
    """Camera rows from one API page (``{"total", "webcams": [...]}``)."""
    rows, skipped = [], 0
    cams = (data or {}).get("webcams") if isinstance(data, dict) else None
    for cam in cams if isinstance(cams, list) else []:
        rec = compact(cam)
        row = row_for(rec, src) if rec else None
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def rows_from_crawl(doc: dict, src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for rec in (doc or {}).get("webcams") or []:
        row = row_for(rec, src)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def _windy_url(url: str) -> bool:
    """An https link on windy.com (pages, players, images)."""
    from ..proxy import host_allowed     # here, so importing the adapter stays light
    checked = http_url(url)
    return bool(checked and checked.startswith("https://") and host_allowed(checked, SOURCE))


# ---------------------------------------------------------------------------------------------
# The crawl
# ---------------------------------------------------------------------------------------------
def _odd(x: float) -> float:
    """Box edges get a seventh decimal of 5, so a webcam (5 or 6 decimals) never sits on one."""
    return round(x, 5) + (0.0000005 if x < 179.99 else -0.0000005)


def fmt_box(box: tuple[float, float, float, float]) -> str:
    return ",".join(f"{v:.7f}".rstrip("0").rstrip(".") for v in box)


def quarters(box: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    n, e, s, w = box
    mlat, mlon = _odd((n + s) / 2), _odd((e + w) / 2)
    return [(n, e, mlat, mlon), (n, mlon, mlat, w), (mlat, e, s, mlon), (mlat, mlon, s, w)]


def sample_box(cams: list[dict]) -> tuple[float, float, float, float]:
    """A box around the webcams seen so far, with a margin; the world when there are none."""
    pts = [(to_float((c.get("location") or {}).get("latitude")), to_float((c.get("location") or {}).get("longitude")))
           for c in cams if isinstance(c, dict)]
    pts = [(la, lo) for la, lo in pts if la is not None and lo is not None]
    if not pts:
        return WORLD
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    mlat = max(0.5, 0.15 * (max(lats) - min(lats)))
    mlon = max(0.5, 0.15 * (max(lons) - min(lons)))
    n, s = min(89.5, max(lats) + mlat), max(-89.5, min(lats) - mlat)
    e, w = min(179.5, max(lons) + mlon), max(-179.5, min(lons) - mlon)
    return (_odd(n), _odd(e), _odd(s), _odd(w))


def ring(box: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    """The rest of the world around ``box``, as up to four boxes (north, south, west, east bands)."""
    n, e, s, w = box
    out = []
    if n < 90:
        out.append((90.0, 180.0, n, -180.0))
    if s > -90:
        out.append((s, 180.0, -90.0, -180.0))
    if w > -180:
        out.append((n, w, s, -180.0))
    if e < 180:
        out.append((n, 180.0, s, e))
    return out


def _age_hours(path: Path) -> float | None:
    try:
        return (time.time() - path.stat().st_mtime) / 3600.0
    except OSError:
        return None


class Crawl:
    """One crawl run. ``get`` is the only place that talks to Windy."""

    def __init__(self, ds, key: str, *, log=None, net=None, spacing: float = SPACING_S,
                 max_requests: int = MAX_REQUESTS, max_seconds: float = MAX_SECONDS):
        self.ds = ds
        self.key = key
        self.log = log
        self.net = net if net is not None else ds.net
        self.spacing = spacing
        self.force = bool(getattr(ds, "force", False))
        self.max_age_h = FORCE_MAX_AGE_H if self.force else PAGE_MAX_AGE_H
        self.max_requests = max_requests
        self.max_seconds = max_seconds
        self.webcams: dict[int, list] = {}
        self.countries: dict[str, dict] = {}
        self.requests = 0
        self.cached = 0
        self.areas = 0
        self.errors: list[str] = []
        self.stopped: str | None = None
        self.started = time.monotonic()

    # -- pages ------------------------------------------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.ds.raw(f"windy/pages/{name}.json.gz")

    def get(self, url: str, params: dict, name: str):
        path = self._path(name)
        age = _age_hours(path)
        if age is not None and age < self.max_age_h:
            try:
                data = parse_json_bytes(path.read_bytes())
                self.cached += 1
                return data
            except (OSError, ValueError, EOFError):
                pass                              # a damaged cache file: fetch it again
        if self.requests >= self.max_requests:
            raise Budget(f"request budget of {self.max_requests} used")
        if time.monotonic() - self.started > self.max_seconds:
            raise Budget("time budget used")
        try:
            resp = self.net.request(url, params=params, headers={"x-windy-api-key": self.key,
                                                                 "Accept": "application/json"},
                                    timeout=30, retries=2, min_interval=self.spacing, accept=(200,))
        except NetError as err:
            if err.status in (401, 403):
                raise KeyRefused(f"Windy refused the API key (HTTP {err.status})") from None
            raise
        finally:
            self.requests += 1
        data = json.loads(decode_text(resp.body))
        self._pause_if_limited(resp.headers)
        tmp = path.with_name(path.name + f".part{os.getpid()}")
        tmp.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), 6))
        os.replace(tmp, path)
        return data

    def _pause_if_limited(self, headers: dict) -> None:
        """Windy allows 300 calls a minute; we stay near 60, but back off if the window runs low."""
        remaining = to_int((headers or {}).get("x-ratelimit-remaining"))
        if remaining is not None and remaining < 10:
            time.sleep(max(1.0, min(60.0, float(to_int(headers.get("x-ratelimit-reset")) or 30))))

    def query(self, filters: dict, name: str) -> tuple[int, list[dict], bool]:
        """(total, webcams, complete): every webcam of the query when it has at most ``CAP``,
        otherwise just the first page."""
        base = {**filters, "limit": PAGE, "include": INCLUDE, "lang": "en", "sortKey": "createdOn",
                "sortDirection": "asc"}
        first = self.get(LIST_URL, {**base, "offset": 0}, f"{name}-o0")
        if not isinstance(first, dict):
            raise ValueError("unexpected answer from the Windy API")
        total = to_int(first.get("total")) or 0
        cams = [c for c in first.get("webcams") or [] if isinstance(c, dict)]
        if total > CAP:
            return total, cams, False
        offset = PAGE
        while offset < total and offset < CAP:
            page = self.get(LIST_URL, {**base, "offset": offset}, f"{name}-o{offset}")
            got = [c for c in (page.get("webcams") if isinstance(page, dict) else None) or [] if isinstance(c, dict)]
            if not got:
                break
            cams.extend(got)
            offset += PAGE
        return total, cams, True

    def add(self, cams: list[dict]) -> None:
        for cam in cams:
            rec = compact(cam)
            if rec:
                self.webcams[rec[0]] = rec

    # -- countries and areas ----------------------------------------------------------------------
    def country_codes(self) -> list[str]:
        data = self.get(COUNTRIES_URL, {"lang": "en"}, "countries")
        codes = sorted({clean(c.get("code")).upper() for c in data if isinstance(c, dict)}
                       if isinstance(data, list) else set())
        return [c for c in codes if len(c) == 2 and c.isalpha()]

    def crawl_country(self, code: str) -> None:
        before = self.requests
        total, cams, complete = self.query({"countries": code}, f"c-{code}")
        self.add(cams)
        stat = {"total": total, "areas": 0}
        if not complete:
            covered = 0
            around = sample_box(cams)
            for box in quarters(around) + ring(around):
                covered += self.area(code, box, 1, stat)
            stat["covered"] = covered
        stat["requests"] = self.requests - before
        self.countries[code] = stat

    def area(self, code: str, box: tuple[float, float, float, float], depth: int, stat: dict) -> int:
        tag = hashlib.sha1(fmt_box(box).encode()).hexdigest()[:10]
        total, cams, complete = self.query({"countries": code, "bbox": fmt_box(box)}, f"c-{code}-a{tag}")
        stat["areas"] += 1
        self.areas += 1
        if total == 0:
            return 0
        self.add(cams)
        if complete:
            return total
        if depth >= MAX_DEPTH:
            self.errors.append(f"{code}: an area still has {total} webcams at the deepest split")
            return min(total, CAP)
        return sum(self.area(code, child, depth + 1, stat) for child in quarters(box))

    def run(self, codes: list[str] | None = None) -> None:
        for code in codes if codes is not None else self.country_codes():
            try:
                self.crawl_country(code)
            except (KeyRefused, Budget):
                raise
            except (NetError, ValueError, OSError) as err:
                self.errors.append(f"{code}: {clean(str(err), 160)}")
                self.countries[code] = {"error": clean(str(err), 160)}

    def document(self, previous: dict | None = None) -> dict:
        """The crawl result; webcams of countries that failed this time are carried over."""
        carried = 0
        failed = {c for c, st in self.countries.items() if st.get("error")}
        if self.stopped:
            failed |= {c for c in (previous or {}).get("countries") or {} if c not in self.countries}
        if previous and failed:
            for rec in previous.get("webcams") or []:
                if isinstance(rec, list) and len(rec) >= len(FIELDS) and rec[6] in failed and rec[0] not in self.webcams:
                    self.webcams[rec[0]] = rec
                    carried += 1
        listed: dict[str, int] = {}
        for rec in self.webcams.values():
            listed[rec[6]] = listed.get(rec[6], 0) + 1
        for code, st in self.countries.items():
            st["listed"] = listed.get(code, 0)
        complete = not self.stopped and not failed
        return {"version": 1, "crawl_at": time.time(), "complete": complete, "fields": FIELDS,
                "webcams": sorted(self.webcams.values(), key=lambda r: r[0]),
                "countries": self.countries,
                "stats": {"webcams": len(self.webcams), "countries": sum(1 for v in listed.values() if v),
                          "requests": self.requests, "cached_pages": self.cached, "areas": self.areas,
                          "carried_over": carried, "errors": self.errors[:50], "stopped": self.stopped,
                          "listed_total": sum(st.get("total") or 0 for st in self.countries.values()),
                          "duration_s": round(time.monotonic() - self.started, 1)}}


def build_crawl(ds, config, log=None, *, spacing: float = SPACING_S) -> dict | None:
    """The ``cameras.windy`` dataset: crawl when the last complete crawl is a week old (or never
    finished), otherwise keep it. Returns the document written, or None when nothing changed."""
    key = config.get(KEY) if config is not None else None
    if not key:
        raise NeedsKey(KEY)
    path = Path(ds.out_dir) / CRAWL_FILE
    previous = None
    if path.exists():
        try:
            previous = read_json(path)
        except (OSError, ValueError):
            previous = None
    if previous and previous.get("complete") and not getattr(ds, "force", False):
        age_days = (time.time() - float(previous.get("crawl_at") or 0)) / 86400.0
        if age_days < RECRAWL_DAYS:
            n = len(previous.get("webcams") or [])
            ds.set_rows(n)
            ds.note(f"Windy catalogue from {datetime.fromtimestamp(previous['crawl_at']).strftime('%Y-%m-%d')} "
                    f"is current ({n:,} webcams); next crawl in {RECRAWL_DAYS - age_days:.1f} days")
            return None
    crawl = Crawl(ds, key, log=log, spacing=spacing)
    try:
        crawl.run()
    except Budget as err:
        crawl.stopped = str(err)
    doc = crawl.document(previous)
    if not doc["webcams"]:
        raise RuntimeError("the Windy crawl found no webcams" + (f" ({crawl.errors[0]})" if crawl.errors else ""))
    ds.write_json(CRAWL_FILE, doc)
    st = doc["stats"]
    ds.set_rows(st["webcams"])
    ds.note(f"{st['webcams']:,} webcams in {st['countries']} countries; {st['requests']} requests, "
            f"{st['cached_pages']} cached pages, {st['areas']} areas"
            + (f"; stopped early: {crawl.stopped}" if crawl.stopped else "")
            + (f"; {len(crawl.errors)} errors" if crawl.errors else ""))
    if hasattr(ds, "source"):
        ds.source(LIST_URL)
    return doc


# ---------------------------------------------------------------------------------------------
# The camera build (reads the crawl) and the fresh image links
# ---------------------------------------------------------------------------------------------
def fetch(fc: FetchContext) -> list[dict]:
    key = fc.key(KEY)
    if not key:
        raise NeedsKey(KEY)
    path = Path(fc.ds.out_dir) / CRAWL_FILE
    if not path.exists():
        raise Pending("first crawl of the Windy catalogue still running (about 30 minutes)")
    mtime = path.stat().st_mtime
    doc = read_json(path)
    rows, skipped = rows_from_crawl(doc, fc.src)
    fc.skip(skipped)
    when = to_float(doc.get("crawl_at"))
    st = doc.get("stats") or {}
    fc.extra.update(crawl_at=when, crawl_file_mtime=mtime, crawl_complete=bool(doc.get("complete")),
                    countries=st.get("countries"), listed_total=st.get("listed_total"))
    if when:
        fc.notes.append(f"catalogue crawled {datetime.fromtimestamp(when).strftime('%Y-%m-%d')}, "
                        f"{st.get('countries') or 0} countries" + ("" if doc.get("complete") else ", partly"))
    return rows


def _iso_time(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def direct_info(cam: dict, safe) -> dict:
    """What the browser needs to show one webcam's current picture, from an API answer that
    included images, urls and player. ``safe(url)`` -> the URL if it may go to the browser."""
    if (cam.get("status") or "active") != "active":
        return {"ok": False, "error": "This webcam is not active on Windy right now."}
    images = cam.get("images") if isinstance(cam.get("images"), dict) else {}
    current = images.get("current") if isinstance(images.get("current"), dict) else {}
    sizes = images.get("sizes") if isinstance(images.get("sizes"), dict) else {}
    urls = cam.get("urls") if isinstance(cam.get("urls"), dict) else {}
    player = cam.get("player") if isinstance(cam.get("player"), dict) else {}

    def size(name):
        s = sizes.get(name) if isinstance(sizes.get(name), dict) else {}
        return to_int(s.get("width")), to_int(s.get("height"))

    image, thumb = safe(current.get("preview")), safe(current.get("thumbnail"))
    link = safe(urls.get("detail")) or safe(player.get("day"))
    if not (image or thumb) or not link:
        return {"ok": False, "error": "Windy has no current picture for this webcam."}
    w, h = size("preview")
    tw, th = size("thumbnail")
    return {"ok": True, "image": image or thumb, "width": w if image else tw, "height": h if image else th,
            "thumb": thumb or image, "thumb_width": tw if thumb else w, "thumb_height": th if thumb else h,
            "link": link, "player": safe(player.get("day")), "updated_at": _iso_time(cam.get("lastUpdatedOn")),
            "views": to_int(cam.get("viewCount")), "title": clean(cam.get("title"), 140)}


def make_direct(config):
    """``Source.direct`` for Windy: fresh image links for up to 50 webcams per API call."""

    def safe(url):
        if not isinstance(url, str) or not _windy_url(url):
            return None
        if config is not None and config.redact(url) != url:     # never hand the key to the browser
            return None
        return http_url(url)

    def direct(rows: list[dict], net) -> dict[str, dict]:
        key = config.get(KEY) if config is not None else None
        if not key:
            raise NeedsKey(KEY)
        want: dict[str, str] = {}
        for row in rows:
            wid = str(row.get("id", "")).split(":", 1)[-1]
            if wid.isdigit():
                want[wid] = row["id"]
        out: dict[str, dict] = {}
        ids = list(want)
        for i in range(0, len(ids), PAGE):
            chunk = ids[i:i + PAGE]
            data = net.get_json(LIST_URL, params={"webcamIds": ",".join(chunk), "limit": len(chunk),
                                                  "include": "images,urls,player", "lang": "en"},
                                headers={"x-windy-api-key": key, "Accept": "application/json"},
                                timeout=12, retries=1, min_interval=0.25)
            for cam in (data.get("webcams") if isinstance(data, dict) else None) or []:
                if isinstance(cam, dict) and str(cam.get("webcamId")) in want:
                    out[want[str(cam.get("webcamId"))]] = direct_info(cam, safe)
        for wid, cid in want.items():
            out.setdefault(cid, {"ok": False, "error": "Windy no longer lists this webcam."})
        return out

    return direct


SOURCE = Source(
    id="windy", name="Windy webcams", owner="Windy.com webcams", url="https://api.windy.com/webcams",
    licence="Windy Webcams API terms (free tier): images only from API links, linked to Windy, credited",
    licence_url="https://api.windy.com/webcams/terms", attribution="Webcams provided by Windy.com",
    fetch=fetch, hosts=("*.windy.com",), refresh=600, tags=(), needs_key=KEY, optional=True,
    coverage="Worldwide: the whole free-tier catalogue (about 69,000 webcams in about 200 countries)",
    camera_type="webcam", direct_ttl=300.0, courtesy=COURTESY, aggregates=True,
)
