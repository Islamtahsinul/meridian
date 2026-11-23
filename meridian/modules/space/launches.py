"""The Space Devs Launch Library 2 (free tier): upcoming and recent launches, and launch pads.

The free tier allows about 15 requests an hour per address. This module makes at most a handful:
one shared, disk-backed cache for "upcoming" (refreshed hourly while the map is in use, otherwise
every six hours so the events feed stays current) and "previous" (every three hours while in use),
plus a weekly pad list as a dataset. A local budget keeps us under 10 requests in any hour.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

LL2 = "https://ll.thespacedevs.com/2.2.0"
UPCOMING_URL = f"{LL2}/launch/upcoming/"
PREVIOUS_URL = f"{LL2}/launch/previous/"
PADS_URL = f"{LL2}/pad/"
LIMITS = {"upcoming": 40, "previous": 30}
MAX_AGE_ACTIVE = {"upcoming": 3600.0, "previous": 3 * 3600.0}
MAX_AGE_IDLE = {"upcoming": 6 * 3600.0, "previous": 24 * 3600.0}
HOURLY_BUDGET = 10
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Status ids used by LL2 2.2.0
STATUS_TONE = {"Go": "green", "TBD": "grey", "TBC": "amber", "Hold": "amber", "In Flight": "blue",
               "Success": "green", "Failure": "red", "Partial Failure": "amber"}


def parse_time(text) -> float | None:
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _s(value, limit: int = 200) -> str:
    if value is None:
        return ""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(value)).strip()[:limit]


def _https(url) -> str | None:
    u = _s(url, 600)
    return u if u.startswith("https://") else None


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_pad(pad: dict) -> dict | None:
    if not isinstance(pad, dict):
        return None
    lat = _num(pad.get("latitude"))
    lon = _num(pad.get("longitude"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    loc = pad.get("location") or {}
    try:
        pid = int(pad.get("id"))
    except (TypeError, ValueError):
        return None
    return {"id": pid, "name": _s(pad.get("name"), 120) or "Launch pad", "lat": round(lat, 5), "lon": round(lon, 5),
            "location": _s(loc.get("name"), 160), "country_code": _s(pad.get("country_code") or loc.get("country_code"), 8),
            "timezone": _s(loc.get("timezone_name"), 60), "wiki_url": _https(pad.get("wiki_url")),
            "map_url": _https(pad.get("map_url")),
            "total_launch_count": int(_num(pad.get("total_launch_count")) or 0)}


def _links(items, limit=4, video=False) -> list[dict]:
    out = []
    for it in sorted((i for i in items or [] if isinstance(i, dict)), key=lambda i: -(_num(i.get("priority")) or 0)):
        url = _https(it.get("url"))
        if not url or any(o["url"] == url for o in out):
            continue
        entry = {"url": url, "title": _s(it.get("title"), 140), "source": _s(it.get("source"), 60)}
        if video:
            entry["publisher"] = _s(it.get("publisher"), 80)
            entry["live"] = bool(it.get("live"))
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def parse_launch(r: dict) -> dict | None:
    """One LL2 launch (normal or detailed mode) -> the module's launch record."""
    if not isinstance(r, dict) or not _UUID.match(str(r.get("id") or "")):
        return None
    pad = parse_pad(r.get("pad") or {})
    net = parse_time(r.get("net"))
    if pad is None or net is None:
        return None
    status = r.get("status") or {}
    lsp = r.get("launch_service_provider") or {}
    rocket = (r.get("rocket") or {}).get("configuration") or {}
    mission = r.get("mission") or {}
    orbit = mission.get("orbit") or {}
    name = _s(r.get("name"), 200)
    mission_name = _s(mission.get("name"), 160) or (name.split("|", 1)[1].strip() if "|" in name else name)
    spacecraft = (r.get("rocket") or {}).get("spacecraft_stage") or {}
    crew = spacecraft.get("launch_crew") if isinstance(spacecraft, dict) else None
    mission_type = _s(mission.get("type"), 60)
    crewed = bool(crew) or mission_type.lower().startswith("human")
    videos = _links(r.get("vidURLs") or mission.get("vid_urls"), video=True)
    info = _links(r.get("infoURLs") or mission.get("info_urls"))
    probability = _num(r.get("probability"))
    np_ = r.get("net_precision") or {}
    return {
        "id": f"ll2:{r['id']}",
        "ll2_id": str(r["id"]),
        "name": name,
        "mission": mission_name,
        "rocket": _s(rocket.get("full_name") or rocket.get("name"), 120),
        "rocket_family": _s(rocket.get("family"), 60),
        "rocket_url": _https(rocket.get("wiki_url") or rocket.get("info_url")),
        "provider": _s(lsp.get("name"), 120),
        "provider_abbrev": _s(lsp.get("abbrev"), 20),
        "provider_type": _s(lsp.get("type"), 40),
        "provider_country": _s(lsp.get("country_code"), 20),
        "provider_url": _https(lsp.get("info_url") or lsp.get("wiki_url")),
        "status": _s(status.get("abbrev"), 30) or "TBD",
        "status_name": _s(status.get("name"), 80),
        "status_text": _s(status.get("description"), 300),
        "net": net,
        "net_precision": _s(np_.get("name"), 30),
        "window_start": parse_time(r.get("window_start")),
        "window_end": parse_time(r.get("window_end")),
        "probability": int(probability) if probability is not None and 0 <= probability <= 100 else None,
        "weather_concerns": _s(r.get("weather_concerns"), 300),
        "holdreason": _s(r.get("holdreason"), 300),
        "failreason": _s(r.get("failreason"), 300),
        "mission_type": mission_type,
        "mission_description": _s(mission.get("description"), 900),
        "orbit": _s(orbit.get("name"), 60),
        "orbit_abbrev": _s(orbit.get("abbrev"), 12),
        "pad": pad,
        "image": _https(r.get("image")),
        "webcast_live": bool(r.get("webcast_live")),
        "videos": videos,
        "info": info,
        "programs": [_s(p.get("name"), 80) for p in (r.get("program") or []) if isinstance(p, dict)][:3],
        "crewed": crewed,
        "updated": parse_time(r.get("last_updated")),
    }


def parse_page(body: bytes | str) -> list[dict]:
    text = body.decode("utf-8-sig", errors="replace") if isinstance(body, bytes) else body
    data = json.loads(text)
    out = []
    for item in (data.get("results") if isinstance(data, dict) else None) or []:
        rec = parse_launch(item)
        if rec is not None:
            out.append(rec)
    return out


def phase(rec: dict, now: float | None = None) -> str:
    """'upcoming', 'flying' or 'past' for display."""
    now = time.time() if now is None else now
    if rec["status"] in ("Success", "Failure", "Partial Failure"):
        return "past"
    if rec["status"] == "In Flight":
        return "flying"
    return "upcoming" if rec["net"] >= now - 3600 else "past"


def entity(rec: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    when = rec["net"]
    subtitle = ", ".join(x for x in (rec["rocket"], rec["provider"]) if x)
    return {"kind": "launch", "id": rec["id"], "title": rec["mission"] or rec["name"], "subtitle": subtitle,
            "lat": rec["pad"]["lat"], "lon": rec["pad"]["lon"], "layer": "space.launches", "time": round(when),
            "zoom": 9}


class Launches:
    def __init__(self, net, raw_dir: Path, log, status=None):
        self.net = net
        self.dir = Path(raw_dir) / "ll2"
        self.log = log
        self._status = status or (lambda **kw: None)
        self._lock = threading.RLock()
        self._fetch_lock = threading.Lock()
        self._data: dict[str, dict] = {}        # which -> {"records", "fetched_at", "error", "failed_at"}
        self._single: dict[str, tuple[float, dict | None]] = {}
        self._requests: deque = deque()
        self._backoff_until = 0.0
        self._refreshing = False

    # -- disk ------------------------------------------------------------------------------------
    def path(self, which: str) -> Path:
        return self.dir / f"{which}.json"

    def load_disk(self) -> None:
        for which in ("upcoming", "previous"):
            p = self.path(which)
            try:
                st = p.stat()
                recs = parse_page(p.read_bytes())
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as err:
                self.log.warning("ignoring cached launches %s: %s", which, err)
                continue
            with self._lock:
                self._data[which] = {"records": recs, "fetched_at": st.st_mtime, "error": None, "failed_at": 0.0}
        self._report()

    # -- budget ----------------------------------------------------------------------------------
    def _spend(self) -> bool:
        now = time.time()
        with self._lock:
            while self._requests and now - self._requests[0] > 3600:
                self._requests.popleft()
            if now < self._backoff_until or len(self._requests) >= HOURLY_BUDGET:
                return False
            self._requests.append(now)
            return True

    def budget_left(self) -> int:
        now = time.time()
        with self._lock:
            recent = sum(1 for t in self._requests if now - t <= 3600)
            return max(0, HOURLY_BUDGET - recent) if now >= self._backoff_until else 0

    # -- fetching --------------------------------------------------------------------------------
    def age(self, which: str) -> float | None:
        with self._lock:
            d = self._data.get(which)
            return time.time() - d["fetched_at"] if d and d["fetched_at"] else None

    def refresh(self, which: str) -> bool:
        if which not in LIMITS:
            raise KeyError(which)
        with self._fetch_lock:
            with self._lock:
                d = self._data.get(which)
                if d and d.get("failed_at") and time.time() - d["failed_at"] < 600:
                    return False
            if not self._spend():
                return False
            url = UPCOMING_URL if which == "upcoming" else PREVIOUS_URL
            try:
                resp = self.net.request(url, params={"limit": LIMITS[which], "mode": "detailed"}, timeout=30,
                                        retries=0, accept=(200,))
                recs = parse_page(resp.body)
            except Exception as err:
                status = getattr(err, "status", None)
                if status == 429:
                    self._backoff_until = time.time() + 1800
                with self._lock:
                    d = self._data.setdefault(which, {"records": [], "fetched_at": 0.0, "error": None, "failed_at": 0.0})
                    d["error"] = ("The Space Devs rate limit was reached; using the cached list."
                                  if status == 429 else str(err))[:300]
                    d["failed_at"] = time.time()
                self.log.warning("Launch Library %s failed: %s", which, err)
                self._report()
                return False
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                p = self.path(which)
                tmp = p.with_name(p.name + f".part{os.getpid()}")
                tmp.write_bytes(resp.body)
                os.replace(tmp, p)
            except OSError as err:
                self.log.warning("could not cache launches %s: %s", which, err)
            with self._lock:
                self._data[which] = {"records": recs, "fetched_at": time.time(), "error": None, "failed_at": 0.0}
            self.log.info("Launch Library %s: %d launches", which, len(recs))
            self._report()
            return True

    def maintain(self, active: bool) -> None:
        """The periodic task: refresh what is stale for the current activity level."""
        for which in ("upcoming", "previous"):
            limit = MAX_AGE_ACTIVE[which] if active else MAX_AGE_IDLE[which]
            age = self.age(which)
            if age is None or age > limit:
                self.refresh(which)

    def ensure(self, which: str) -> None:
        """Synchronous first load when nothing is cached; background refresh when stale."""
        age = self.age(which)
        if age is None:
            self.refresh(which)
        elif age > MAX_AGE_ACTIVE[which]:
            self.refresh_async()

    def refresh_async(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True

        def run():
            try:
                self.maintain(True)
            except Exception as err:
                self.log.warning("launch refresh failed: %s", err)
            finally:
                with self._lock:
                    self._refreshing = False

        threading.Thread(target=run, daemon=True, name="space-ll2").start()

    # -- reading ---------------------------------------------------------------------------------
    def info(self) -> dict:
        with self._lock:
            out = {}
            for which in ("upcoming", "previous"):
                d = self._data.get(which)
                out[which] = {"count": len(d["records"]) if d else 0,
                              "fetched_at": (d or {}).get("fetched_at") or None,
                              "error": (d or {}).get("error")}
            return out

    def all(self) -> list[dict]:
        """Every known launch, upcoming list first (it is fresher), de-duplicated, sorted by time."""
        with self._lock:
            seen = {}
            for which in ("upcoming", "previous"):
                for rec in (self._data.get(which) or {}).get("records", ()):
                    seen.setdefault(rec["id"], rec)
        return sorted(seen.values(), key=lambda r: r["net"])

    def get(self, lid: str) -> dict | None:
        lid = lid if lid.startswith("ll2:") else f"ll2:{lid}"
        for rec in self.all():
            if rec["id"] == lid:
                return rec
        with self._lock:
            hit = self._single.get(lid)
            return hit[1] if hit else None

    def fetch_one(self, lid: str) -> dict | None:
        """A launch not in the cached lists (an old favourite). One request, cached for an hour."""
        raw = lid[4:] if lid.startswith("ll2:") else lid
        if not _UUID.match(raw):
            return None
        rec = self.get(lid)
        if rec is not None:
            return rec
        key = f"ll2:{raw}"
        with self._lock:
            hit = self._single.get(key)
            if hit and time.time() - hit[0] < 3600:
                return hit[1]
        if not self._spend():
            return None
        try:
            data = self.net.get_json(f"{LL2}/launch/{raw}/", params={"mode": "detailed"}, timeout=20, retries=0)
            rec = parse_launch(data)
        except Exception as err:
            self.log.info("Launch Library lookup %s failed: %s", raw, err)
            rec = None
        with self._lock:
            self._single[key] = (time.time(), rec)
            while len(self._single) > 200:
                self._single.pop(next(iter(self._single)))
        return rec

    def pads(self) -> dict[int, dict]:
        out = {}
        for rec in self.all():
            out.setdefault(rec["pad"]["id"], rec["pad"])
        return out

    def _report(self) -> None:
        info = self.info()
        up = info["upcoming"]
        prev = info["previous"]
        errs = [e for e in (up["error"], prev["error"]) if e]
        count = up["count"] + prev["count"]
        note = f"{up['count']} upcoming, {prev['count']} recent"
        if up["fetched_at"]:
            note += f", list {int((time.time() - up['fetched_at']) / 60)} min old"
        if count == 0 and errs:
            self._status(ok=False, count=0, error=errs[0], note=note)
        else:
            self._status(ok=True, count=count, note=note + (f" ({errs[0]})" if errs else ""))


def parse_pads_pages(bodies: list[bytes]) -> list[dict]:
    pads = {}
    for body in bodies:
        data = json.loads(body.decode("utf-8-sig", errors="replace"))
        for item in (data.get("results") if isinstance(data, dict) else None) or []:
            p = parse_pad(item)
            if p is not None:
                pads[p["id"]] = p
    return sorted(pads.values(), key=lambda p: p["id"])
