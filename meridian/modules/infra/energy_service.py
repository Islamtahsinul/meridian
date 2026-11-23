"""The infra module's electricity service: polling, snapshots for Great Britain and Europe, details,
the point lookup for "What's here?" and the AI tools ``grid_now`` and ``uk_carbon_intensity``.

Polling runs only while the browser uses the energy views (activity key ``infra.energy``, any energy
route marks it, 10-minute window). Great Britain: 2 NESO and 3 Elexon requests every 5 minutes.
Europe: Energy-Charts limits each endpoint to 2 requests a minute per client (20 for /signal), so one
worker walks the countries at 1 request every 4 s (/signal) and 1 every 40 s (/public_power,
/price), backs off on HTTP 429 with Retry-After, and keeps its results in data/cache/infra so a
restart does not start from nothing.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from meridian import geo
from meridian.net import NetError

from . import countries, energy_geo
from .common import read_json, write_json_atomic
from .energy import (CI_API, CI_HOST, CI_PAGE, CI_REGION_IDS, EC_API, EC_BY_ISO, EC_COUNTRIES, EC_HOST, EC_PAGE,
                     ELEXON_API, ELEXON_ATTRIBUTION, ELEXON_HOST, ELEXON_LICENCE, ELEXON_PAGE, INDEX_LABELS,
                     INTERCONNECTORS, NESO_ATTRIBUTION, POSTCODE_RE, SRC_CI, SRC_EC, SRC_ELEXON, ZONE_NAMES, ci_time,
                     half_hour_floor, intensity_now, parse_ci_regional, parse_ci_region_forecast, parse_ci_series,
                     drop_spikes, parse_ec_power, parse_ec_price, parse_ec_signal, parse_fuelinst, parse_indo, parse_mid,
                     sparkline_points, utc_date, value_at, window_extremes)

ACTIVITY = "infra.energy"
ACTIVE_WINDOW = 600.0
GB_EVERY = 300.0
JSON = {"Accept": "application/json"}

SIGNAL_EVERY = 3600.0       # re-read each country's renewable share (it holds the day's forecast)
POWER_EVERY = 3600.0
PRICE_EVERY = 12 * 3600.0
EMPTY_RETRY = 6 * 3600.0
ERROR_RETRY = 900.0
MIN_REFETCH = 900.0         # a country or zone whose series stops early is not asked again for 15 minutes
EC_HOURLY_BUDGET = 200      # normal use is about 75 an hour (36 /signal, 36 /public_power, a few /price)
CACHE_NAME = "energy-europe.json"
CACHE_MAX_AGE = 36 * 3600.0
GB_NAMES = {"gb", "uk", "united kingdom", "great britain", "britain", "england", "scotland", "wales",
            "united kingdom of great britain and northern ireland"}


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after:.0f} s")
        self.retry_after = retry_after


class Gate:
    """Spacing for one Energy-Charts endpoint, widened after a 429 and relaxed slowly afterwards."""

    def __init__(self, interval: float):
        self.base = self.interval = float(interval)
        self.next_at = 0.0
        self.blocked_until = 0.0
        self.limited_count = 0

    def ready(self, now: float) -> bool:
        return now >= self.next_at and now >= self.blocked_until

    def used(self, now: float) -> None:
        self.next_at = now + self.interval
        if self.interval > self.base:
            self.interval = max(self.base, self.interval * 0.97)

    def limited(self, now: float, retry_after: float) -> None:
        self.limited_count += 1
        self.blocked_until = now + max(30.0, min(900.0, retry_after))
        self.interval = min(self.base * 4, self.interval * 2)


DISAGREE_PP = 30.0      # /signal "now" against the latest reported mix's share of load: this far apart ...
DISAGREE_RATIO = 3.0    # ... and three times apart means the signal is not describing the same grid


def signal_disagrees(signal_pct: float, mix_pct: float) -> bool:
    """True when Energy-Charts' /signal value cannot be the renewable share its own latest mix reports
    (a few hours of real change, such as solar setting in Germany, stays well inside these bounds)."""
    lo, hi = sorted((float(signal_pct), float(mix_pct)))
    return hi - lo >= DISAGREE_PP and hi > max(lo, 0.5) * DISAGREE_RATIO


class Energy:
    def __init__(self, m, markets=None, *, cache_dir: Path | None = None):
        self.m = m
        self.net = m.net
        self.markets = markets
        self.cache_path = Path(cache_dir or m.cache_dir) / CACHE_NAME
        self._lock = threading.Lock()
        self._gate_lock = threading.Lock()
        self._gb_busy = False
        self._gb_last_try = 0.0
        self._gbp_per_eur: float | None = None
        self._rate_tried = 0.0
        self._ci: dict = {}            # {at, series, regional}
        self._elexon: dict = {}        # {fuelinst: {at, data}, indo: {...}, mid: {...}}
        self._gb_errors: dict[str, str] = {}
        self._sig: dict[str, dict] = {}
        self._pow: dict[str, dict] = {}
        self._price: dict[str, dict] = {}
        self._priority: list[str] = []
        self._region_fc: dict[str, tuple[float, dict]] = {}
        self.gates = {"signal": Gate(4.0), "power": Gate(40.0), "price": Gate(40.0)}
        self._dirty = False
        self._saved_at = 0.0
        self._worker: threading.Thread | None = None
        self.shapes: dict = {"countries": [], "regions": []}
        self.ec_requests = 0
        self._ec_recent: list[float] = []
        self._load_cache()

    # -- activity -------------------------------------------------------------------------------
    def touch(self) -> None:
        self.m.touch(ACTIVITY)

    def wanted(self) -> bool:
        return self.m.active(ACTIVITY, within=ACTIVE_WINDOW)

    # -- shapes (point lookup) --------------------------------------------------------------------
    def load_shapes(self, data_dir: Path | None = None) -> None:
        cfc, rfc = energy_geo.load(Path(data_dir or self.m.data_dir))
        pack = lambda fc: [(f["properties"], f["geometry"], geo.geometry_bbox(f["geometry"])) for f in fc.get("features") or []]
        self.shapes = {"countries": pack(cfc), "regions": pack(rfc)}
        self.m.status(energy_geo.SOURCE_NE, ok=True, count=len(self.shapes["countries"]))
        self.m.status(energy_geo.SOURCE_DNO, ok=True, count=len(self.shapes["regions"]))

    def _hit(self, kind: str, lat: float, lon: float) -> dict | None:
        for props, geom, bbox in self.shapes.get(kind) or []:
            if bbox and not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
                continue
            if geo.point_in_geometry(lon, lat, geom):
                return props
        return None

    # -- Great Britain ----------------------------------------------------------------------------
    def refresh_gb(self) -> bool:
        with self._lock:
            if self._gb_busy:
                return False
            self._gb_busy = True
        try:
            self._refresh_neso()
            self._refresh_elexon()
            self._refresh_rate()
            return True
        finally:
            self._gb_busy = False

    def _refresh_rate(self) -> None:
        """The ECB GBP rate for showing Britain's price in euros (background only, hourly; a failure keeps
        the last rate and is retried after 10 minutes, so request handlers never wait on the ECB)."""
        now = time.time()
        if self.markets is None or now - self._rate_tried < (3600 if self._gbp_per_eur else 600):
            return
        self._rate_tried = now
        try:
            rate = (self.markets.eur_rates() or {}).get("GBP")
        except Exception:
            rate = None
        if rate:
            self._gbp_per_eur = float(rate)

    def _refresh_neso(self) -> None:
        now = time.time()
        hh = half_hour_floor(now)
        try:
            raw = self.net.get_json(f"{CI_API}/intensity/{ci_time(hh - 86400)}/{ci_time(hh + 86400)}", headers=JSON,
                                    ttl=240, timeout=15, retries=1)
            series = parse_ci_series(raw)
            if not series:
                raise ValueError("the intensity answer had no half hours")
            regional = parse_ci_regional(self.net.get_json(f"{CI_API}/regional", headers=JSON, ttl=240, timeout=15,
                                                           retries=1))
            if not regional["regions"]:
                regional = (self._ci or {}).get("regional") or regional
            self._ci = {"at": now, "series": series, "regional": regional}
            self._gb_errors.pop("neso", None)
            cur = intensity_now(series, now) or {}
            self.m.status(SRC_CI, ok=True, count=len(regional["regions"]),
                          note=f"{cur.get('value', '?'):.0f} gCO2/kWh, {cur.get('index') or 'no index'}"
                          if cur.get("value") is not None else None)
        except Exception as err:
            self._gb_errors["neso"] = "NESO's Carbon Intensity API is not answering right now."
            self.m.status(SRC_CI, ok=False, error=str(err))

    def _refresh_elexon(self) -> None:
        now = time.time()
        fmt = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        calls = {
            "fuelinst": (f"{ELEXON_API}/datasets/FUELINST",
                         {"publishDateTimeFrom": fmt(now - 1500), "publishDateTimeTo": fmt(now + 60), "format": "json"},
                         parse_fuelinst),
            "indo": (f"{ELEXON_API}/demand/outturn",
                     {"settlementDateFrom": utc_date(now - 86400), "settlementDateTo": utc_date(now), "format": "json"},
                     parse_indo),
            "mid": (f"{ELEXON_API}/balancing/pricing/market-index",
                    {"from": fmt(now - 4 * 3600), "to": fmt(now + 60), "format": "json"}, parse_mid),
        }
        ok = 0
        errors = []
        for name, (url, params, parse) in calls.items():
            try:
                data = parse(self.net.get_json(url, params=params, ttl=200, timeout=15, retries=1))
                if data is None:
                    raise ValueError("no rows")
                self._elexon[name] = {"at": now, "data": data}
                ok += 1
            except Exception as err:
                errors.append(f"{name}: {err}")
        if ok:
            self._gb_errors.pop("elexon", None)
            fi = (self._elexon.get("fuelinst") or {}).get("data") or {}
            self.m.status(SRC_ELEXON, ok=True, count=len(fi.get("fuels") or []),
                          note="; ".join(errors)[:200] if errors else
                          f"{fi.get('generation_mw', 0) / 1000:.1f} GW transmission generation" if fi else None)
        else:
            self._gb_errors["elexon"] = "Elexon's Insights API is not answering right now."
            self.m.status(SRC_ELEXON, ok=False, error="; ".join(errors)[:400])

    def gb_stale(self, max_age: float = GB_EVERY) -> bool:
        ats = [self._ci.get("at") or 0] + [(v or {}).get("at") or 0 for v in self._elexon.values()]
        return not self._ci or not self._elexon or time.time() - min(ats) > max_age

    def refresh_gb_async(self) -> None:
        """On-demand refresh from a browser request; at most one attempt a minute (an outage upstream
        must not turn quick re-polls into a burst of requests)."""
        now = time.time()
        if self._gb_busy or now - self._gb_last_try < 60:
            return
        self._gb_last_try = now
        threading.Thread(target=self.refresh_gb, name="infra-energy-gb", daemon=True).start()

    def gb_price(self) -> dict | None:
        mid = (self._elexon.get("mid") or {}).get("data")
        if not mid:
            return None
        out = dict(mid)
        rate = self._gbp_per_eur
        out["eur_mwh"] = round(mid["gbp_mwh"] / rate, 2) if rate else None
        out["basis"] = "Market index price (half-hourly short-term market, APX)"
        return out

    def gb_snapshot(self) -> dict:
        now = time.time()
        ci = self._ci or {}
        series = ci.get("series") or []
        regional = ci.get("regional") or {}
        gb = (regional.get("aggregates") or {}).get("GB") or {}
        fi = (self._elexon.get("fuelinst") or {}).get("data")
        indo = (self._elexon.get("indo") or {}).get("data")
        cur = intensity_now(series, now) if series else None
        links = []
        for x in (fi or {}).get("interconnectors") or []:
            meta = INTERCONNECTORS.get(x["code"]) or {}
            links.append({**x, "gb": [meta["gb"][1], meta["gb"][0]], "far": [meta["far"][1], meta["far"][0]]})
        return {
            "updated": max([ci.get("at") or 0] + [(v or {}).get("at") or 0 for v in self._elexon.values()]) or None,
            "pending": not ci and not self._elexon, "refreshing": self._gb_busy,
            "intensity": {"now": cur, "index_label": INDEX_LABELS.get((cur or {}).get("index") or ""),
                          "spark": sparkline_points(series, now), "windows": window_extremes(series, now),
                          "unit": "gCO2/kWh"} if series else None,
            "mix": {"t": regional.get("t"), "to": regional.get("to"), "fuels": gb.get("mix") or [],
                    **{k: gb.get(k) for k in ("renewable_pct", "fossil_pct", "low_carbon_pct", "imports_pct")}}
            if gb else None,
            "generation": {"t": fi["t"], "fuels": fi["fuels"], "generation_mw": fi["generation_mw"]} if fi else None,
            "interconnectors": links,
            "flows": {k: fi[k] for k in ("net_import_mw", "imports_mw", "exports_mw")} if fi else None,
            "demand": indo, "price": self.gb_price(),
            "regions": regional.get("regions") or [],
            "nations": {k: {"intensity": v.get("intensity"), "index": v.get("index"),
                            "renewable_pct": v.get("renewable_pct")} for k, v in (regional.get("aggregates") or {}).items()
                        if k in ("England", "Scotland", "Wales")},
            "errors": dict(self._gb_errors),
            "attribution": [f"Carbon intensity and mix: NESO Carbon Intensity API (CC BY 4.0). {NESO_ATTRIBUTION}.",
                            f"Generation, demand, interconnectors and price: {ELEXON_ATTRIBUTION}."],
            "links": {"neso": CI_PAGE, "elexon": ELEXON_PAGE, "elexon_licence": ELEXON_LICENCE},
        }

    def gb_row(self) -> dict:
        """Great Britain in the shape of a European table row."""
        snap = self.gb_snapshot()
        cur = (snap.get("intensity") or {}).get("now") or {}
        mix = snap.get("mix") or {}
        price = snap.get("price") or {}
        top = (mix.get("fuels") or [{}])[0]
        return {"country": "GB", "name": "Great Britain", "source": "neso",
                "renewable_now": mix.get("renewable_pct"), "renewable_t": mix.get("t"),
                "intensity": cur.get("value"), "intensity_t": cur.get("t"), "intensity_kind": "neso",
                "index": cur.get("index"), "fossil_pct": mix.get("fossil_pct"), "low_carbon_pct": mix.get("low_carbon_pct"),
                "load_mw": (snap.get("demand") or {}).get("mw"), "top": top.get("label"), "top_pct": top.get("pct"),
                "price": price.get("eur_mwh"), "price_gbp": price.get("gbp_mwh"), "price_t": price.get("t"),
                "price_zone": "GB", "pending": snap["pending"]}

    # -- Europe: the worker -----------------------------------------------------------------------
    def _due(self, kind: str, key: str, now: float) -> bool:
        rec = {"signal": self._sig, "power": self._pow, "price": self._price}[kind].get(key)
        if rec is None:
            return True
        if now < rec.get("next", 0):
            return False
        age = now - (rec.get("at") or 0)
        if age < MIN_REFETCH:          # never ask for the same thing more often than this
            return False
        if kind == "signal":
            if rec.get("empty"):
                return True
            last = (rec.get("series") or [[0]])[-1][0]
            return age > SIGNAL_EVERY or last < now + 900
        if kind == "power":
            return rec.get("empty") or age > POWER_EVERY
        last = (rec.get("data") or {}).get("series") or [[0]]
        step = (rec.get("data") or {}).get("step") or 3600
        return rec.get("empty") or age > PRICE_EVERY or last[-1][0] + step <= now

    def _jobs(self, kind: str) -> list[str]:
        if kind == "price":
            zones = []
            for code, _iso, zone in EC_COUNTRIES:
                if zone and zone not in zones:
                    zones.append(zone)
            first = [EC_BY_ISO[c.upper()][1] for c in self._priority if EC_BY_ISO.get(c.upper(), (None, None))[1]]
            return list(dict.fromkeys(first + zones))
        codes = [code for code, _iso, _zone in EC_COUNTRIES]
        return list(dict.fromkeys([c for c in self._priority if c in codes] + codes))

    def _take(self, kind: str, now: float) -> bool:
        """Claim one request on an endpoint: its spacing allows it and the hourly budget has room."""
        with self._gate_lock:
            self._ec_recent = [t for t in self._ec_recent if now - t < 3600]
            if len(self._ec_recent) >= EC_HOURLY_BUDGET or not self.gates[kind].ready(now):
                return False
            self.gates[kind].used(now)
            self._ec_recent.append(now)
            return True

    def eu_step(self, now: float | None = None) -> bool:
        """Run at most one due Energy-Charts request. True when a request was made."""
        now = time.time() if now is None else now
        for kind in ("signal", "power", "price"):
            if not self.gates[kind].ready(now):
                continue
            key = next((k for k in self._jobs(kind) if self._due(kind, k, now)), None)
            if key is None or not self._take(kind, now):
                continue
            self._fetch(kind, key, now)
            return True
        return False

    def _ec_get(self, endpoint: str, params: dict):
        self.ec_requests += 1
        resp = self.net.request(f"{EC_API}/{endpoint}", params=params, headers=JSON, timeout=10, retries=0,
                                accept=(200, 404, 429))
        if resp.status == 404:          # "No content available": this country has no such data
            return None
        if resp.status == 429:
            try:
                wait = float(resp.headers.get("retry-after") or 120)
            except ValueError:
                wait = 120.0
            raise RateLimited(wait)
        return resp.json()

    def _fetch(self, kind: str, key: str, now: float) -> None:
        store = {"signal": self._sig, "power": self._pow, "price": self._price}[kind]
        try:
            if kind == "signal":
                data = parse_ec_signal(self._ec_get("signal", {"country": key}))
                store[key] = {"at": now, "series": data["series"], "empty": data["empty"],
                              "next": now + EMPTY_RETRY if data["empty"] else 0}
            elif kind == "power":
                data = parse_ec_power(self._ec_get("public_power", {"country": key}))
                store[key] = {"at": now, "data": data, "empty": data.get("empty", False),
                              "next": now + EMPTY_RETRY if data.get("empty") else 0}
                if data.get("unknown"):
                    self.m.log.info("energy-charts %s: production types counted as other: %s", key, data["unknown"])
            else:
                data = parse_ec_price(self._ec_get("price", {"bzn": key}))
                store[key] = {"at": now, "data": data, "empty": data["empty"],
                              "next": now + EMPTY_RETRY if data["empty"] else 0}
            self._dirty = True
            self._status_ok()
        except RateLimited as err:
            self.gates[kind].limited(now, err.retry_after)
            self.m.log.info("energy-charts /%s rate-limited; waiting %.0f s", kind, err.retry_after)
            self.m.status(SRC_EC, ok=True, note=f"Pausing /{kind} for {err.retry_after:.0f} s (rate limit)")
        except (NetError, ValueError, TypeError, KeyError) as err:
            old = store.get(key)
            fails = (old or {}).get("fails", 0) + 1
            wait = min(EMPTY_RETRY, ERROR_RETRY * 2 ** (fails - 1))      # 15 min, 30 min, 1 h ... 6 h
            if old:
                old["next"], old["fails"] = now + wait, fails
            else:
                store[key] = {"at": 0, "empty": True, "failed": True, "series": [], "data": {}, "next": now + wait,
                              "fails": fails}
            self.m.status(SRC_EC, ok=bool(self._sig), error=None if self._sig else str(err)[:300],
                          note=f"/{kind} for {key} failed; retrying in {wait / 60:.0f} minutes")
        finally:
            self._drop_priority()

    def _drop_priority(self) -> None:
        now = time.time()
        keep = []
        for code in self._priority:
            zone = EC_BY_ISO.get(code.upper(), (None, None))[1]
            if self._due("signal", code, now) or self._due("power", code, now) or (zone and self._due("price", zone, now)):
                keep.append(code)
        self._priority = keep

    def _status_ok(self) -> None:
        have = sum(1 for v in self._sig.values() if not v.get("empty"))
        mixes = sum(1 for v in self._pow.values() if not v.get("empty"))
        prices = sum(1 for v in self._price.values() if not v.get("empty"))
        now = time.time()
        paused = [f"/{k} for {g.blocked_until - now:.0f} s" for k, g in self.gates.items() if g.blocked_until > now]
        self.m.status(SRC_EC, ok=True, count=have,
                      note=f"{have} countries with a renewable share now, {mixes} with a generation mix, {prices} price zones"
                           + (f"; pausing {', '.join(paused)} after a rate limit" if paused else ""))

    def run_eu(self, stop: threading.Event) -> None:
        """Worker thread: Energy-Charts requests while the energy views are in use."""
        while not stop.is_set():
            if not self.wanted():
                self._persist()
                stop.wait(10.0)
                continue
            did = self.eu_step()
            if time.time() - self._saved_at > 60:
                self._persist()
            stop.wait(1.0 if did else 2.0)

    def ensure_worker(self) -> None:
        """With --no-tasks the scheduler never starts m.thread workers: start one on demand."""
        sched = getattr(self.m.app, "scheduler", None)
        if sched is not None and sched.started:
            return
        if self._worker is not None and self._worker.is_alive():
            return
        stop = threading.Event()

        def run():
            idle = 0.0
            while not stop.is_set():
                if not self.wanted():
                    idle += 5
                    if idle > ACTIVE_WINDOW:
                        self._persist()
                        return
                    stop.wait(5.0)
                    continue
                idle = 0.0
                did = self.eu_step()
                stop.wait(1.0 if did else 2.0)

        self._worker = threading.Thread(target=run, name="infra-energy-eu", daemon=True)
        self._worker.start()

    def prioritise(self, ec_code: str) -> None:
        if ec_code and ec_code not in self._priority:
            self._priority.insert(0, ec_code)

    # -- persistence --------------------------------------------------------------------------------
    def _persist(self) -> None:
        if not self._dirty:
            return
        try:
            write_json_atomic(self.cache_path, {"saved_at": time.time(), "signal": self._sig, "power": self._pow,
                                                "price": self._price})
            self._dirty = False
            self._saved_at = time.time()
        except OSError as err:
            self.m.log.warning("could not save the European grid cache: %s", err)

    def _load_cache(self) -> None:
        try:
            doc = read_json(self.cache_path)
        except (OSError, ValueError):
            return
        if not isinstance(doc, dict) or time.time() - float(doc.get("saved_at") or 0) > CACHE_MAX_AGE:
            return
        for name, target in (("signal", self._sig), ("power", self._pow), ("price", self._price)):
            for k, v in (doc.get(name) or {}).items():
                if isinstance(v, dict) and isinstance(v.get("at"), (int, float)):
                    v["next"] = min(float(v.get("next") or 0), time.time() + EMPTY_RETRY)   # keep back-offs across restarts
                    if name == "signal" and isinstance(v.get("series"), list):    # caches saved before drop_spikes
                        v["series"] = [list(p) for p in drop_spikes([tuple(p) for p in v["series"]
                                                                    if isinstance(p, list) and len(p) == 2])]
                    target[str(k)] = v

    # -- Europe: views ------------------------------------------------------------------------------
    def country_row(self, code: str, iso: str, zone: str | None, now: float) -> dict:
        sig = self._sig.get(code) or {}
        pw = (self._pow.get(code) or {}).get("data") or {}
        pr = (self._price.get(zone) or {}).get("data") if zone else None
        sv = value_at(sig.get("series") or [], now, 3600) if not sig.get("empty") else None
        ren_kind = "now"
        ren_note = None
        mixed = pw.get("renewable_share_load")
        if mixed is not None and now - (pw.get("t") or 0) < 12 * 3600:
            if sv is None:
                sv = (pw["t"], mixed)      # no /signal for this country: the latest reported mix
                ren_kind, ren_note = "latest", "no_signal"
            elif signal_disagrees(sv[1], mixed):
                # Switzerland's /signal forecast counts little but solar, so it falls to ~1% at night while
                # the reported mix (mostly hydro) says ~75%: show the reported share, marked as such.
                sv = (pw["t"], mixed)
                ren_kind, ren_note = "latest", "disagrees"
        pv = value_at((pr or {}).get("series") or [], now, ((pr or {}).get("step") or 3600) + 60) if pr else None
        top = (pw.get("mix") or [{}])[0] if not pw.get("empty") else {}
        return {"country": iso, "name": countries.name(iso) or iso, "source": "energy-charts", "ec": code,
                "renewable_now": sv[1] if sv else None, "renewable_t": sv[0] if sv else None,
                "renewable_kind": ren_kind if sv else None, "renewable_note": ren_note if sv else None,
                "intensity": pw.get("intensity_est"), "intensity_t": pw.get("t"), "intensity_kind": "estimate",
                "fossil_pct": pw.get("fossil_pct"), "low_carbon_pct": pw.get("low_carbon_pct"),
                "load_mw": pw.get("load_mw"), "top": top.get("label"), "top_pct": top.get("pct"),
                "price": pv[1] if pv else None, "price_t": pv[0] if pv else None, "price_zone": zone,
                "price_avg": (pr or {}).get("avg"),
                "pending": code not in self._sig or code not in self._pow or bool(zone and zone not in self._price),
                "no_data": bool(sig.get("empty") and (self._pow.get(code) or {}).get("empty"))}

    def europe(self) -> dict:
        now = time.time()
        rows = [self.gb_row()] + [self.country_row(code, iso, zone, now) for code, iso, zone in EC_COUNTRIES]
        times = [v.get("at") or 0 for d in (self._sig, self._pow, self._price) for v in d.values()]
        return {"countries": rows, "updated": max(times) if times else None,
                "pending": sum(1 for r in rows[1:] if r["pending"]), "total": len(rows),
                "rate_limited": any(g.blocked_until > now for g in self.gates.values()),
                "attribution": ["Europe: Energy-Charts.info, Fraunhofer ISE (CC BY 4.0); day-ahead prices from "
                                "Bundesnetzagentur | SMARD.de (CC BY 4.0).",
                                "Carbon intensity outside Great Britain is estimated from the latest generation mix "
                                "with NESO's emission factors (direct CO2, domestic generation only)."],
                "links": {"energy_charts": EC_PAGE}}

    def map_data(self) -> dict:
        now = time.time()
        out = {}
        rows = [self.gb_row()] + [self.country_row(code, iso, zone, now) for code, iso, zone in EC_COUNTRIES]
        for row in rows:
            out[row["country"]] = {"r": row["renewable_now"], "i": row["intensity"], "p": row["price"],
                                   "f": row["fossil_pct"], "x": row.get("index")}
        snap = self.gb_snapshot()
        regions = {str(r["id"]): {"i": r["intensity"], "x": r["index"], "r": r["renewable_pct"], "n": r["name"]}
                   for r in snap["regions"]}
        return {"countries": out, "regions": regions, "links": snap["interconnectors"],
                "updated": snap["updated"], "gb_pending": snap["pending"],
                "pending": sum(1 for r in rows[1:] if r["pending"])}      # not fetched yet (no-data countries are done)

    def country_detail(self, iso: str) -> dict | None:
        iso = iso.upper()
        if iso == "GB":
            return {"country": "GB", "name": "Great Britain", "gb": True}
        hit = EC_BY_ISO.get(iso)
        if not hit:
            return None
        code, zone = hit
        now = time.time()
        if self._due("signal", code, now) or self._due("power", code, now):
            self.prioritise(code)
        sig = self._sig.get(code) or {}
        pw = self._pow.get(code) or {}
        pr = self._price.get(zone) if zone else None
        row = self.country_row(code, iso, zone, now)
        return {**row, "zone": zone, "zone_name": ZONE_NAMES.get(zone or "", zone),
                "signal": {"series": sig.get("series") or [], "at": sig.get("at")} if sig else None,
                "power": {**(pw.get("data") or {}), "at": pw.get("at")} if pw else None,
                "prices": {**((pr or {}).get("data") or {}), "at": (pr or {}).get("at")} if pr else None,
                "page": f"{EC_PAGE}charts/power/chart.htm?l=en&c={code.upper()}"}

    # -- GB regions ---------------------------------------------------------------------------------
    def region_detail(self, rid: int) -> dict | None:
        if rid not in CI_REGION_IDS:
            return None
        snap = self.gb_snapshot()
        cur = next((r for r in snap["regions"] if r["id"] == rid), None)
        out = {"id": rid, "now": cur, "forecast": None, "forecast_error": None,
               "shape": next((p for p, _g, _b in self.shapes.get("regions") or [] if p.get("id") == rid), None)}
        key = str(rid)
        hit = self._region_fc.get(key)
        if hit and time.time() - hit[0] < 900:
            out["forecast"] = hit[1]
        else:
            try:
                start = ci_time(half_hour_floor(time.time()))
                fc = parse_ci_region_forecast(self.net.get_json(f"{CI_API}/regional/intensity/{start}/fw24h/regionid/{rid}",
                                                                headers=JSON, ttl=900, timeout=12, retries=1))
                self._region_fc[key] = (time.time(), fc)
                out["forecast"] = fc
            except Exception as err:
                out["forecast_error"] = "The regional forecast is not available right now."
                self.m.log.info("regional forecast %s failed: %s", rid, err)
        fc = out["forecast"] or {}
        if fc.get("series"):
            pts = [{"t": r["t"], "to": r["t"] + 1800, "forecast": r["forecast"], "actual": None, "index": r["index"]}
                   for r in fc["series"]]
            out["windows"] = window_extremes(pts, time.time())
        return out

    def postcode(self, pc: str) -> dict | None:
        pc = pc.strip().upper().replace(" ", "")
        if not POSTCODE_RE.fullmatch(pc):
            return None
        try:
            return parse_ci_region_forecast(self.net.get_json(f"{CI_API}/regional/postcode/{pc}", headers=JSON, ttl=900,
                                                              timeout=12, retries=0))
        except NetError as err:
            if err.status == 400:
                return {"id": None, "postcode": pc, "series": [], "error": "No GB region matches that postcode."}
            raise

    # -- point lookup ---------------------------------------------------------------------------------
    def at(self, lat: float, lon: float) -> dict:
        out: dict = {"country": None, "region": None}
        props = self._hit("countries", lat, lon)
        if props:
            iso = props.get("id")
            if iso == "GB":
                out["country"] = self.gb_row()
            elif iso in EC_BY_ISO:
                code, zone = EC_BY_ISO[iso]
                out["country"] = self.country_row(code, iso, zone, time.time())
        reg = self._hit("regions", lat, lon) if out["country"] is None or (out["country"] or {}).get("country") == "GB" else None
        if reg:
            cur = next((r for r in self.gb_snapshot()["regions"] if r["id"] == reg.get("id")), None)
            out["region"] = {"id": reg.get("id"), "name": reg.get("name"), "dno": reg.get("dno_full"),
                             "intensity": (cur or {}).get("intensity"), "index": (cur or {}).get("index"),
                             "renewable_pct": (cur or {}).get("renewable_pct")}
            if out["country"] is None:
                out["country"] = self.gb_row()
        return out

    # -- AI tools -------------------------------------------------------------------------------------
    @staticmethod
    def _iso(ts) -> str | None:
        return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts)) if isinstance(ts, (int, float)) and ts else None

    def _resolve_country(self, value: str) -> str | None:
        q = str(value or "").strip()
        if not q:
            return None
        if q.lower() in GB_NAMES:
            return "GB"
        iso = countries.resolve(q)
        return iso.upper() if iso else None

    def _ensure_gb(self, max_age: float = 600.0) -> None:
        if self.gb_stale(max_age):
            self.refresh_gb()

    def _ensure_country(self, code: str, zone: str | None, budget_s: float = 4.0) -> None:
        """Fetch what is missing for one country now, within the endpoint gates (AI tools only)."""
        deadline = time.time() + budget_s
        for kind, key in (("signal", code), ("power", code), ("price", zone)):
            if not key or time.time() > deadline:
                continue
            now = time.time()
            store = {"signal": self._sig, "power": self._pow, "price": self._price}[kind]
            if key in store and not self._due(kind, key, now):
                continue
            if self._take(kind, now):
                self._fetch(kind, key, now)

    def ai_grid_now(self, args: dict) -> dict:
        self.touch()
        q = str(args.get("country") or "GB").strip()[:60]
        iso = self._resolve_country(q)
        if iso == "GB":
            self._ensure_gb()
            snap = self.gb_snapshot()
            cur = (snap.get("intensity") or {}).get("now") or {}
            win = (snap.get("intensity") or {}).get("windows") or {}
            mix = snap.get("mix") or {}
            price = snap.get("price") or {}
            dem = snap.get("demand") or {}
            if snap["pending"]:
                return {"found": True, "country": "GB", "available": False,
                        "message": "NESO and Elexon did not answer, so there is no live data for Great Britain right now."}
            return {
                "found": True, "country": "GB", "name": "Great Britain", "available": True,
                "carbon_intensity": {"g_co2_per_kwh": cur.get("value"), "index": cur.get("index"),
                                     "period_start": self._iso(cur.get("t")), "kind": "NESO estimate for this half hour",
                                     "last_measured": cur.get("last_actual"),
                                     "last_measured_start": self._iso(cur.get("last_actual_t"))},
                "generation_mix_percent": {g["label"]: g["pct"] for g in mix.get("fuels") or []},
                "renewable_percent": mix.get("renewable_pct"), "low_carbon_percent": mix.get("low_carbon_pct"),
                "fossil_percent": mix.get("fossil_pct"),
                "demand_mw": dem.get("mw"), "demand_period_start": self._iso(dem.get("t")),
                "todays_peak_demand_mw": (dem.get("peak") or {}).get("mw"),
                "interconnectors": [{"name": x["name"], "to": x["to"], "mw": x["mw"], "direction": x["direction"]}
                                    for x in snap["interconnectors"]],
                "net_imports_mw": (snap.get("flows") or {}).get("net_import_mw"),
                "price": {"gbp_per_mwh": price.get("gbp_mwh"), "eur_per_mwh": price.get("eur_mwh"),
                          "period_start": self._iso(price.get("t")), "basis": price.get("basis")} if price else None,
                "cleanest_3h_next_24h": {"from": self._iso(win["cleanest"]["from"]), "to": self._iso(win["cleanest"]["to"]),
                                         "avg_g_co2_per_kwh": win["cleanest"]["avg"]} if win.get("cleanest") else None,
                "regions_lowest_first": [{"name": r["name"], "g_co2_per_kwh": r["intensity"], "index": r["index"]}
                                         for r in sorted(snap["regions"], key=lambda r: r["intensity"] if r["intensity"] is not None else 1e9)],
                "sources": snap["attribution"],
            }
        hit = EC_BY_ISO.get(iso or "")
        if not hit:
            covered = ", ".join(sorted(countries.name(i) or i for _c, i, _z in EC_COUNTRIES))
            return {"found": False, "message": f"No live grid data for {q!r}. Covered: Great Britain and {covered}."}
        code, zone = hit
        self._ensure_country(code, zone)
        now = time.time()
        row = self.country_row(code, iso, zone, now)
        pw = (self._pow.get(code) or {}).get("data") or {}
        pr = (self._price.get(zone) or {}).get("data") if zone else None
        return {
            "found": True, "country": iso, "name": row["name"], "available": not row["no_data"],
            "renewable_share_of_load_now_percent": row["renewable_now"], "renewable_share_time": self._iso(row["renewable_t"]),
            "generation_mix_percent": {g["label"]: g["pct"] for g in pw.get("mix") or []} or None,
            "mix_time": self._iso(pw.get("t")), "fossil_percent": pw.get("fossil_pct"),
            "low_carbon_percent": pw.get("low_carbon_pct"),
            "estimated_carbon_intensity_g_co2_per_kwh": pw.get("intensity_est"),
            "estimate_note": "Estimated from the generation mix with NESO's emission factors (direct CO2, no imports).",
            "load_mw": pw.get("load_mw"),
            "day_ahead_price_eur_per_mwh": {"now": row["price"], "today_average": (pr or {}).get("avg"),
                                            "today_min": (pr or {}).get("min"), "today_max": (pr or {}).get("max"),
                                            "zone": zone} if zone else None,
            "pending": row["pending"],
            "note": "Some figures are still loading (Energy-Charts allows few requests a minute); ask again shortly."
            if row["pending"] else None,
            "sources": ["Energy-Charts.info, Fraunhofer ISE (CC BY 4.0)",
                        "Day-ahead prices: Bundesnetzagentur | SMARD.de (CC BY 4.0)"],
        }

    def ai_uk_carbon(self, args: dict) -> dict:
        self.touch()
        self._ensure_gb()
        snap = self.gb_snapshot()
        it = snap.get("intensity") or {}
        cur = it.get("now") or {}
        if not cur:
            return {"available": False, "message": "NESO's Carbon Intensity API is not answering right now."}
        fut = (it.get("spark") or {}).get("future") or []
        out = {
            "available": True,
            "national": {"g_co2_per_kwh": cur.get("value"), "index": cur.get("index"),
                         "period_start": self._iso(cur.get("t")), "last_measured": cur.get("last_actual")},
            "forecast_next_24h": [{"start": self._iso(t), "g_co2_per_kwh": v} for t, v in fut[::4][:13]],
            "cleanest_3h": None, "dirtiest_3h": None,
            "regions": [{"id": r["id"], "name": r["name"], "g_co2_per_kwh": r["intensity"], "index": r["index"],
                         "renewable_percent": r["renewable_pct"]} for r in snap["regions"]],
            "index_bands": "NESO's index: very low, low, moderate, high, very high.",
            "source": f"NESO Carbon Intensity API (CC BY 4.0). {NESO_ATTRIBUTION}.",
        }
        win = it.get("windows") or {}
        for k in ("cleanest", "dirtiest"):
            if win.get(k):
                out[f"{k}_3h"] = {"from": self._iso(win[k]["from"]), "to": self._iso(win[k]["to"]),
                                  "avg_g_co2_per_kwh": win[k]["avg"]}
        region_q = str(args.get("region") or "").strip()[:60]
        pc = str(args.get("postcode") or "").strip().upper().replace(" ", "")[:8]
        if pc:
            if not POSTCODE_RE.fullmatch(pc):
                out["postcode"] = {"error": "Use the first half of a GB postcode, such as SW1 or M1."}
            else:
                try:
                    r = self.postcode(pc) or {}
                except Exception:
                    r = {"error": "The postcode lookup is not answering right now."}
                if r.get("series"):
                    first = r["series"][0]
                    out["postcode"] = {"postcode": pc, "region": r.get("name"), "region_id": r.get("id"),
                                       "g_co2_per_kwh": first["forecast"], "index": first["index"],
                                       "generation_mix_percent": {g["label"]: g["pct"] for g in first["mix"]}}
                else:
                    out["postcode"] = {"postcode": pc, "error": r.get("error") or "No data for that postcode."}
        if region_q:
            ql = region_q.lower()
            match = next((r for r in snap["regions"] if str(r["id"]) == ql or ql in r["name"].lower()), None)
            if match:
                det = self.region_detail(match["id"]) or {}
                fc = (det.get("forecast") or {}).get("series") or []
                out["region"] = {"id": match["id"], "name": match["name"], "g_co2_per_kwh": match["intensity"],
                                 "index": match["index"], "renewable_percent": match["renewable_pct"],
                                 "generation_mix_percent": {g["label"]: g["pct"] for g in match["mix"]},
                                 "forecast_next_24h": [{"start": self._iso(r["t"]), "g_co2_per_kwh": r["forecast"]}
                                                       for r in fc[::4][:13]]}
            else:
                out["region"] = {"error": f"No GB region matches {region_q!r}.",
                                 "regions": [r["name"] for r in snap["regions"]]}
        return out


# ---------------------------------------------------------------------------------------------
# Registration (called from the infra module's register)
# ---------------------------------------------------------------------------------------------
def register(m, markets=None) -> Energy:
    from meridian.registry import HTTPError

    en = Energy(m, markets)
    for host, secs in ((CI_HOST, 1.0), (ELEXON_HOST, 0.5), (EC_HOST, 3.5)):
        m.net.set_host_interval(host, secs)

    m.source(SRC_CI, "NESO Carbon Intensity API", CI_PAGE, "CC BY 4.0, National Energy System Operator (NESO)",
             refresh="every 5 minutes while the energy views are in use", layer="infra.grid")
    m.source(SRC_ELEXON, "Elexon Insights Solution (BMRS)", ELEXON_PAGE,
             "BMRS data licence: Contains BMRS data © Elexon Limited copyright and database right",
             refresh="every 5 minutes while the energy views are in use", layer="infra.grid")
    m.source(SRC_EC, "Energy-Charts API, Fraunhofer ISE", EC_PAGE,
             "CC BY 4.0; day-ahead prices from Bundesnetzagentur | SMARD.de (CC BY 4.0)",
             refresh="each country hourly while in use (the API allows 2 requests a minute)", layer="infra.grid")
    m.source(energy_geo.SOURCE_NE, "Natural Earth admin-0 countries (1:50m)", energy_geo.NE_PAGE, "Public domain",
             refresh="monthly", layer="infra.grid")
    m.source(energy_geo.SOURCE_DNO, "NESO GIS boundaries for GB DNO licence areas", energy_geo.DNO_PAGE,
             f"NESO Open Data Licence ({NESO_ATTRIBUTION})", refresh="monthly", layer="infra.grid")

    wanted = {iso: (countries.name(iso) or iso) for _c, iso, _z in EC_COUNTRIES}
    wanted["GB"] = "Great Britain"

    def build_shapes(ds):
        try:
            energy_geo.build(ds, wanted)
        except Exception as err:
            if en.shapes.get("countries"):
                m.status(energy_geo.SOURCE_NE, ok=True, note=f"Last refresh failed ({type(err).__name__}); serving the previous copy")
            else:
                m.status(energy_geo.SOURCE_NE, ok=False, error=str(err)[:300])
            raise

    m.dataset("energy-shapes", build_shapes, max_age_hours=24 * 30,
              description="Grid map shapes: Natural Earth countries and NESO's GB DNO areas")
    m.on_dataset("energy-shapes", en.load_shapes)

    m.task("energy-gb", en.refresh_gb, every=GB_EVERY, initial_delay=15, when=en.wanted)
    m.thread("energy-europe", en.run_eu)

    m.layer("infra.grid", "Electricity grids", "Infrastructure",
            description="Renewable share now, carbon intensity or day-ahead price for 36 European countries and "
                        "Great Britain's 14 regions, with Britain's interconnector flows.",
            source="NESO Carbon Intensity API (CC BY 4.0), Elexon BMRS, Energy-Charts (CC BY 4.0), Natural Earth, "
                   "NESO DNO areas", refresh="Great Britain every 5 minutes, Europe hourly, while in use",
            use="Where electricity is cleanest now, how Britain trades power, price differences across Europe",
            count=lambda: sum(1 for v in en.map_data()["countries"].values() if v["r"] is not None or v["i"] is not None))

    def served(fn):
        def handler(req):
            en.touch()
            return fn(req)
        return handler

    @m.route("GET", "/api/infra/energy/gb")
    @served
    def r_gb(req):
        if en.gb_stale():
            en.refresh_gb_async()
        return en.gb_snapshot()

    @m.route("GET", "/api/infra/energy/europe")
    @served
    def r_europe(req):
        if en.gb_stale():
            en.refresh_gb_async()
        en.ensure_worker()
        return en.europe()

    @m.route("GET", "/api/infra/energy/map")
    @served
    def r_map(req):
        if en.gb_stale():
            en.refresh_gb_async()
        en.ensure_worker()
        return en.map_data()

    @m.route("GET", "/api/infra/energy/country/{iso2}")
    @served
    def r_country(req):
        cc = req.params["iso2"].strip().upper()
        if len(cc) != 2 or not cc.isalpha():
            raise HTTPError(400, "BAD_ARG", "Use a two-letter ISO country code.")
        out = en.country_detail(cc)
        if out is None:
            raise HTTPError(404, "NOT_FOUND", "No live grid data for that country.")
        en.ensure_worker()
        return out

    @m.route("GET", "/api/infra/energy/gb/region/{rid}")
    @served
    def r_region(req):
        try:
            rid = int(req.params["rid"])
        except ValueError:
            raise HTTPError(400, "BAD_ARG", "Region ids are 1 to 14.") from None
        out = en.region_detail(rid)
        if out is None:
            raise HTTPError(404, "NOT_FOUND", "Region ids are 1 to 14.")
        return out

    @m.route("GET", "/api/infra/energy/at")
    @served
    def r_at(req):
        lat, lon = req.latlon()
        if en.gb_stale(900):
            en.refresh_gb_async()
        return en.at(lat, lon)

    m.ai_tool("grid_now", "The electricity grid of a country right now: carbon intensity, generation mix, renewable and "
              "fossil shares, demand, price and (for Great Britain) interconnector flows. Covers Great Britain (live, "
              "NESO and Elexon) and 36 European countries (Energy-Charts; intensity there is an estimate).",
              {"type": "object", "properties": {"country": {"type": "string",
                                                            "description": "Country name or ISO code, e.g. GB, France, DE"}},
               "required": ["country"], "additionalProperties": False}, en.ai_grid_now)
    m.ai_tool("uk_carbon_intensity", "Great Britain's electricity carbon intensity from NESO: now, the next 24 hours, the "
              "cleanest and dirtiest 3-hour windows, all 14 regions, and optionally one region or an outward postcode.",
              {"type": "object", "properties": {
                  "region": {"type": "string", "description": "Region name or id 1-14, e.g. London, North Scotland"},
                  "postcode": {"type": "string", "description": "First half of a GB postcode, e.g. SW1 or M1"}},
               "additionalProperties": False}, en.ai_uk_carbon)
    return en
