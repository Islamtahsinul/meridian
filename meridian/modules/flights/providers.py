"""ADS-B providers and the machinery that keeps us polite: one priority queue per host (so a slow
background job never delays the map), cooldowns after errors and 429s, and daily request budgets
for metered sources.

- adsb.fi open data: 1 request per second, personal non-commercial use, cite adsb.fi.
- adsb.lol: ODbL. Unkeyed traffic is metered (500 requests a day per IP), so it is a fallback and
  an occasional helper (trail backfill), never the workhorse. ADSBLOL_API_KEY raises the quota.
- OpenSky Network: 400 credits a day anonymously, 4,000 with an API client (OAuth2 client
  credentials). Used when the others fail or when FLIGHTS_PROVIDERS prefers it.
- A local readsb/tar1090 receiver (LOCAL_ADSB_URL): read every few seconds, no limits.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
import urllib.parse
from typing import Any, Callable

from meridian.net import NetError

from .parse import parse_opensky, parse_readsb, parse_trace

_SEQ = itertools.count()


class BudgetExhausted(Exception):
    pass


class Unavailable(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Job queue (one worker thread per host)
# ---------------------------------------------------------------------------------------------
class Job:
    __slots__ = ("key", "fn", "priority", "created", "expires", "done", "result", "error", "started")

    def __init__(self, key: str, fn: Callable[[], Any], priority: int, expires: float | None):
        self.key = key
        self.fn = fn
        self.priority = priority
        self.created = time.time()
        self.expires = expires
        self.done = threading.Event()
        self.result = None
        self.error: BaseException | None = None
        self.started = None

    def wait(self, timeout: float) -> bool:
        return self.done.wait(max(0.0, timeout))

    @property
    def ok(self) -> bool:
        return self.done.is_set() and self.error is None


class HostQueue:
    """Priority queue (0 = most urgent) executed by one worker. Jobs with the same key coalesce."""

    def __init__(self, name: str, max_len: int = 400):
        self.name = name
        self.max_len = max_len
        self._cv = threading.Condition()
        self._heap: list = []
        self._pending: dict[str, Job] = {}
        self.busy: str | None = None
        self.stats = {"run": 0, "failed": 0, "expired": 0, "dropped": 0}

    def submit(self, key: str, fn: Callable[[], Any], *, priority: int = 5, ttl: float | None = None) -> Job | None:
        with self._cv:
            job = self._pending.get(key)
            if job is not None:
                if priority < job.priority:
                    job.priority = priority
                    heapq.heappush(self._heap, (priority, next(_SEQ), job))
                    self._cv.notify()
                return job
            if len(self._pending) >= self.max_len and priority >= 3:
                self.stats["dropped"] += 1
                return None
            job = Job(key, fn, priority, time.time() + ttl if ttl else None)
            self._pending[key] = job
            heapq.heappush(self._heap, (priority, next(_SEQ), job))
            self._cv.notify()
            return job

    def pending(self, key: str) -> Job | None:
        with self._cv:
            return self._pending.get(key)

    def size(self) -> int:
        with self._cv:
            return len(self._pending)

    def run(self, stop: threading.Event) -> None:
        """Worker body (registered with ``m.thread``)."""
        while not stop.is_set():
            job = self._next(stop)
            if job is None:
                continue
            self.run_job(job)

    def _next(self, stop: threading.Event) -> Job | None:
        with self._cv:
            while not self._heap and not stop.is_set():
                self._cv.wait(1.0)
            if stop.is_set() or not self._heap:
                return None
            _, _, job = heapq.heappop(self._heap)
            if job.done.is_set() or self._pending.get(job.key) is not job or job.started is not None:
                return None
            job.started = time.time()
            self.busy = job.key
            return job

    def run_job(self, job: Job) -> None:
        try:
            if job.expires is not None and time.time() > job.expires:
                self.stats["expired"] += 1
                raise TimeoutError("waited too long in the queue")
            job.result = job.fn()
            self.stats["run"] += 1
        except BaseException as err:  # the worker must survive any job
            job.error = err
            self.stats["failed"] += 1
        finally:
            with self._cv:
                if self._pending.get(job.key) is job:
                    del self._pending[job.key]
                self.busy = None
            job.done.set()

    def drain_once(self) -> int:
        """Run everything queued now on the calling thread (tests)."""
        n = 0
        while True:
            with self._cv:
                if not self._heap:
                    return n
                _, _, job = heapq.heappop(self._heap)
                if job.done.is_set() or self._pending.get(job.key) is not job or job.started is not None:
                    continue
                job.started = time.time()
            self.run_job(job)
            n += 1


# ---------------------------------------------------------------------------------------------
# Health and budgets
# ---------------------------------------------------------------------------------------------
class Health:
    """Cooldown after failures: 429 honours Retry-After; errors back off 15 s, 30 s ... 5 min."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cooldown_until = 0.0
        self.failures = 0
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.last_error_at: float | None = None

    def ok(self, now: float | None = None) -> None:
        with self.lock:
            self.failures = 0
            self.cooldown_until = 0.0
            self.last_ok = now or time.time()

    def fail(self, message: str, *, retry_after: float | None = None, now: float | None = None) -> None:
        now = now or time.time()
        with self.lock:
            self.failures += 1
            wait = retry_after if retry_after is not None else min(300.0, 15.0 * (2 ** min(self.failures - 1, 5)))
            self.cooldown_until = max(self.cooldown_until, now + max(1.0, wait))
            self.last_error = message[:300]
            self.last_error_at = now

    def available(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.cooldown_until

    def info(self, now: float) -> dict:
        return {"ok": self.last_ok is not None and (self.last_error_at is None or self.last_ok >= self.last_error_at),
                "last_ok": self.last_ok, "last_error": self.last_error, "last_error_at": self.last_error_at,
                "cooling_down_s": max(0, int(self.cooldown_until - now))}


class DailyBudget:
    """Requests (or credits) per rolling day: a token bucket refilled evenly over 24 h, persisted in
    the kv table so a restart does not grant a fresh day, plus the provider's own 'remaining'."""

    def __init__(self, store, key: str, per_day: float, *, burst: float = 30.0, reserve: int = 0,
                 clock=time.time):
        self.store = store
        self.key = key
        self.per_day = float(per_day)
        self.burst = float(burst)
        self.reserve = reserve
        self.clock = clock
        self.lock = threading.Lock()
        saved = store.kv_get(key, None) if store is not None else None
        now = clock()
        if isinstance(saved, dict) and isinstance(saved.get("tokens"), (int, float)) and isinstance(saved.get("t"), (int, float)):
            self.tokens = float(saved["tokens"])
            self.t = float(saved["t"])
            self.used = int(saved.get("used", 0))
            self.day = int(saved.get("day", int(now // 86400)))
        else:
            self.tokens = self.burst
            self.t = now
            self.used = 0
            self.day = int(now // 86400)
        self.remote_remaining: int | None = None
        self.remote_reset_at: float | None = None
        self._dirty = 0

    def _refill(self, now: float) -> None:
        if now > self.t:
            self.tokens = min(self.burst, self.tokens + (now - self.t) * self.per_day / 86400.0)
            self.t = now
        day = int(now // 86400)
        if day != self.day:
            self.day = day
            self.used = 0

    def can(self, cost: float = 1.0) -> bool:
        now = self.clock()
        with self.lock:
            self._refill(now)
            if self.remote_remaining is not None and self.remote_reset_at and now < self.remote_reset_at:
                if self.remote_remaining - cost < self.reserve:
                    return False
            return self.tokens >= cost

    def take(self, cost: float = 1.0) -> None:
        now = self.clock()
        with self.lock:
            self._refill(now)
            if self.remote_remaining is not None and self.remote_reset_at and now < self.remote_reset_at:
                if self.remote_remaining - cost < self.reserve:
                    raise BudgetExhausted(f"{self.key}: the provider's daily quota is nearly used up")
            if self.tokens < cost:
                raise BudgetExhausted(f"{self.key}: daily request budget used up for now")
            self.tokens -= cost
            self.used += int(cost)
            self._dirty += 1
            if self._dirty >= 5:
                self._save()

    def refund(self, cost: float = 1.0) -> None:
        with self.lock:
            self.tokens = min(self.burst, self.tokens + cost)

    def note_remote(self, remaining: int | None, reset_in: float | None) -> None:
        if remaining is None:
            return
        with self.lock:
            self.remote_remaining = remaining
            self.remote_reset_at = self.clock() + (reset_in if reset_in is not None else 3600.0)

    def _save(self) -> None:
        self._dirty = 0
        if self.store is not None:
            try:
                self.store.kv_set(self.key, {"tokens": self.tokens, "t": self.t, "used": self.used, "day": self.day})
            except Exception:
                pass

    def save(self) -> None:
        with self.lock:
            self._save()

    def info(self) -> dict:
        now = self.clock()
        with self.lock:
            self._refill(now)
            return {"per_day": self.per_day, "tokens": round(self.tokens, 1), "used_today": self.used,
                    "provider_remaining": self.remote_remaining}


def _header_int(headers: dict, *names: str) -> int | None:
    for name in names:
        value = headers.get(name.lower())
        if value is None:
            continue
        try:
            return int(str(value).split(",")[0].strip().split(";")[0])
        except ValueError:
            continue
    return None


def _retry_after(headers: dict, default: float) -> float:
    for name in ("retry-after", "x-rate-limit-retry-after-seconds"):
        v = headers.get(name)
        if v is None:
            continue
        try:
            return max(1.0, min(3600.0, float(v)))
        except ValueError:
            continue
    return default


def _lat(v: float) -> str:
    return f"{max(-90.0, min(90.0, v)):.4f}"


def _lon(v: float) -> str:
    return f"{max(-180.0, min(180.0, v)):.4f}"


# ---------------------------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------------------------
class Provider:
    id = "base"
    host = ""
    supports = frozenset()

    def __init__(self, net, log, status: Callable[..., None]):
        self.net = net
        self.log = log
        self._status = status
        self.health = Health()
        self.queue = HostQueue(self.id)
        self.last_count: int | None = None

    def enabled(self) -> bool:
        return True

    def available(self, what: str, cost: float = 1.0) -> bool:
        return what in self.supports and self.enabled() and self.health.available()

    def note_ok(self, count: int, note: str | None = None) -> None:
        self.health.ok()
        self.last_count = count
        self._status(ok=True, note=note)        # item counts are published by the service

    def note_error(self, err: BaseException, *, retry_after: float | None = None) -> None:
        message = str(err) or type(err).__name__
        self.health.fail(message, retry_after=retry_after)
        self._status(ok=False, error=message)

    def info(self, now: float) -> dict:
        out = {"id": self.id, "enabled": self.enabled(), "queue": self.queue.size(), "busy": self.queue.busy,
               "last_count": self.last_count, **self.health.info(now)}
        return out


class ReadsbApi(Provider):
    """Shared by adsb.fi and adsb.lol: readsb-style JSON answers."""
    base = ""
    budget: DailyBudget | None = None

    def headers(self) -> dict | None:
        return None

    def _get(self, path: str, *, cost: float = 1.0, timeout: float = 15.0, accept_404: bool = False) -> Any:
        if not self.health.available():
            # Jobs queued before a 429 or an outage must not keep knocking (adsb.fi counts those).
            raise Unavailable(f"{self.host} is cooling down after an error")
        if self.budget is not None:
            self.budget.take(cost)
        url = self.base + path
        try:
            resp = self.net.request(url, headers=self.headers(), timeout=timeout, retries=0,
                                    accept=(200, 404, 429, 503) if accept_404 else (200, 429, 503))
        except NetError as err:
            self.note_error(err)
            raise
        if self.budget is not None:
            self.budget.note_remote(_header_int(resp.headers, "x-ratelimit-remaining"),
                                    _header_int(resp.headers, "x-ratelimit-reset"))
        if resp.status in (429, 503):
            wait = _retry_after(resp.headers, 60.0 if resp.status == 429 else 30.0)
            err = NetError(f"{self.host}: HTTP {resp.status}, waiting {int(wait)} s", status=resp.status)
            self.note_error(err, retry_after=wait)
            raise err
        if resp.status == 404:
            return None
        try:
            return resp.json()
        except ValueError:
            err = NetError(f"{self.host}: answer is not JSON", status=resp.status)
            self.note_error(err)
            raise err from None

    def _readsb(self, path: str, **kw) -> tuple[float, list[dict]]:
        payload = self._get(path, **kw)
        if payload is None:
            return time.time(), []
        try:
            now, obs = parse_readsb(payload, received=time.time())
        except ValueError as err:
            self.note_error(NetError(f"{self.host}: unexpected answer ({err})"))
            raise
        note = None
        if self.budget is not None:
            b = self.budget.info()
            if b["provider_remaining"] is not None:
                note = f"{b['provider_remaining']} metered requests left today"
        self.note_ok(len(obs), note)
        return now, obs


class AdsbFi(ReadsbApi):
    id = "adsbfi"
    host = "opendata.adsb.fi"
    base = "https://opendata.adsb.fi/api"
    supports = frozenset({"point", "mil", "sqk", "hexes", "callsign", "reg"})
    MAX_HEXES = 40

    def point(self, lat: float, lon: float, radius_nm: int):
        return self._readsb(f"/v3/lat/{_lat(lat)}/lon/{_lon(lon)}/dist/{int(max(1, min(250, radius_nm)))}")

    def mil(self):
        return self._readsb("/v2/mil", timeout=20.0)

    def sqk(self, code: str):
        return self._readsb(f"/v2/sqk/{code}")

    def hexes(self, hexes: list[str]):
        return self._readsb("/v2/icao/" + ",".join(hexes[: self.MAX_HEXES]))

    def callsign(self, cs: str):
        return self._readsb("/v2/callsign/" + urllib.parse.quote(cs, safe=""))

    def reg(self, reg: str):
        return self._readsb("/v2/registration/" + urllib.parse.quote(reg, safe="-"))


class AdsbLol(ReadsbApi):
    id = "adsblol"
    host = "api.adsb.lol"
    base = "https://api.adsb.lol"
    supports = frozenset({"point", "mil", "sqk", "hexes", "callsign", "reg", "trace"})

    def __init__(self, net, log, status, *, store, api_key: str | None, per_day: int):
        super().__init__(net, log, status)
        self.api_key = api_key
        self.budget = DailyBudget(store, "flights.budget.adsblol", per_day, burst=25, reserve=40)

    def headers(self):
        return {"x-api-key": self.api_key} if self.api_key else None

    def available(self, what: str, cost: float = 1.0) -> bool:
        return super().available(what) and self.budget.can(cost)

    def point(self, lat, lon, radius_nm):
        return self._readsb(f"/v2/point/{_lat(lat)}/{_lon(lon)}/{int(max(1, min(250, radius_nm)))}")

    def mil(self):
        return self._readsb("/v2/mil", timeout=20.0)

    def sqk(self, code):
        return self._readsb(f"/v2/sqk/{code}")

    def hexes(self, hexes):
        return self._readsb(f"/v2/hex/{hexes[0]}")

    def callsign(self, cs):
        return self._readsb("/v2/callsign/" + urllib.parse.quote(cs, safe=""))

    def reg(self, reg):
        return self._readsb("/v2/reg/" + urllib.parse.quote(reg, safe="-"))

    def trace(self, hexid: str) -> list[tuple]:
        payload = self._get(f"/1/trace/recent/{hexid}", accept_404=True)
        if payload is None:
            return []
        points = parse_trace(payload)
        self.health.ok()
        return points


class OpenSky(Provider):
    id = "opensky"
    host = "opensky-network.org"
    base = "https://opensky-network.org/api"
    token_url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    supports = frozenset({"point", "bbox"})

    def __init__(self, net, log, status, *, store, client_id: str | None, client_secret: str | None):
        super().__init__(net, log, status)
        self.client_id = client_id
        self.client_secret = client_secret
        self.auth = bool(client_id and client_secret)
        per_day = 3600 if self.auth else 350
        self.budget = DailyBudget(store, "flights.budget.opensky" + (".auth" if self.auth else ""), per_day,
                                  burst=60 if self.auth else 20, reserve=10)
        self._token: str | None = None
        self._token_exp = 0.0
        self._token_lock = threading.Lock()
        self.auth_error: str | None = None

    @staticmethod
    def credits_for(bbox: tuple[float, float, float, float]) -> int:
        w, s, e, n = bbox
        width = (e - w) % 360.0 or (360.0 if e != w else 0.0)
        area = width * max(0.0, n - s)
        return 1 if area <= 25 else 2 if area <= 100 else 3 if area <= 400 else 4

    def available(self, what, cost: float = 1.0) -> bool:
        return super().available(what) and self.budget.can(cost)

    def _bearer(self) -> dict | None:
        if not self.auth:
            return None
        with self._token_lock:
            if self._token and time.time() < self._token_exp - 60:
                return {"Authorization": f"Bearer {self._token}"}
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                           "client_secret": self.client_secret}).encode("ascii")
            try:
                resp = self.net.request(self.token_url, method="POST", data=body, timeout=15, retries=0,
                                        headers={"Content-Type": "application/x-www-form-urlencoded"})
                data = resp.json()
                token = data.get("access_token") if isinstance(data, dict) else None
                if not isinstance(token, str) or not token:
                    raise ValueError("no access token in the answer")
                self._token = token
                self._token_exp = time.time() + float(data.get("expires_in") or 1800)
                self.auth_error = None
                return {"Authorization": f"Bearer {self._token}"}
            except (NetError, ValueError) as err:
                # Fall back to anonymous access; never echo the secret or the token.
                self.auth_error = f"OpenSky sign-in failed ({getattr(err, 'status', None) or 'no answer'}); using anonymous access"
                self._token = None
                self._token_exp = time.time() + 600
                return None

    def bbox(self, bbox: tuple[float, float, float, float]):
        w, s, e, n = bbox
        if not self.health.available():
            raise Unavailable(f"{self.host} is cooling down after an error")
        cost = self.credits_for(bbox)
        self.budget.take(cost)
        params = {"lamin": f"{s:.4f}", "lomin": f"{w:.4f}", "lamax": f"{n:.4f}", "lomax": f"{e:.4f}", "extended": 1}
        try:
            resp = self.net.request(self.base + "/states/all", params=params, headers=self._bearer(), timeout=20,
                                    retries=0, accept=(200, 429, 503))
        except NetError as err:
            self.note_error(err)
            raise
        self.budget.note_remote(_header_int(resp.headers, "x-rate-limit-remaining"), 3600.0)
        if resp.status != 200:
            wait = _retry_after(resp.headers, 300.0)
            err = NetError(f"{self.host}: HTTP {resp.status}, waiting {int(wait)} s", status=resp.status)
            self.note_error(err, retry_after=wait)
            raise err
        try:
            now, obs = parse_opensky(resp.json())
        except ValueError as err:
            self.note_error(NetError(f"{self.host}: unexpected answer ({err})"))
            raise
        remaining = self.budget.info()["provider_remaining"]
        note = ("signed in" if self.auth and not self.auth_error else "anonymous") + (
            f", {remaining} credits left" if remaining is not None else "")
        self.note_ok(len(obs), self.auth_error or note)
        return now, obs

    def point(self, lat: float, lon: float, radius_nm: int):
        from meridian import geo
        w, s, e, n = geo.bbox_of_circle(lat, lon, radius_nm * 1.852)
        if w > e:      # OpenSky boxes cannot cross the antimeridian: keep the side with the centre
            if lon >= 0:
                e = 180.0
            else:
                w = -180.0
        now, obs = self.bbox((w, s, e, n))
        r = radius_nm * 1.852
        return now, [o for o in obs if "lat" not in o or geo.haversine_km(lat, lon, o["lat"], o["lon"]) <= r]


class LocalReceiver(Provider):
    id = "local"
    host = "local"
    supports = frozenset({"local"})

    def __init__(self, net, log, status, *, url: str | None):
        super().__init__(net, log, status)
        self.url = url

    def enabled(self) -> bool:
        return bool(self.url)

    def fetch(self):
        try:
            payload = self.net.get_json(self.url, timeout=6, retries=0)
            now, obs = parse_readsb(payload, received=time.time())
        except (NetError, ValueError) as err:
            self.note_error(err if isinstance(err, NetError) else NetError(f"local receiver: {err}"))
            raise
        self.note_ok(len(obs))
        return now, obs
