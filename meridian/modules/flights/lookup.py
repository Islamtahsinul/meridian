"""Routes and aircraft details from adsbdb (cached for a day in memory and SQLite), the operator
privacy rule, and a plausibility check for routes.

Privacy: airline and company operators are shown. Owners that look like a private person are
shown as "Private owner", and aircraft in the LADD or PIA privacy programmes never show an owner.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from meridian import geo
from meridian.net import NetError

from .parse import CALLSIGN_RE, HEX_RE

ADSBDB = "https://api.adsbdb.com/v0"
ROUTE_TTL = 86400.0
ROUTE_NEG_TTL = 6 * 3600.0
AIRCRAFT_TTL = 7 * 86400.0
AIRCRAFT_NEG_TTL = 86400.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights_lookup (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    t REAL NOT NULL
);
"""

# -- operator privacy -----------------------------------------------------------------------------
_ORG_WORDS = re.compile(
    r"\b(?:AIR|AIRLINES?|AIRWAYS|AIRLINK|AVIATION|AVIACION|AVIATIK|AVIA|AERO|AERONAUTICA|AEROSPACE|JETS?"
    r"|CHARTERS?|FLIGHTS?|FLYING|LEASING|LEASE|LEASECO|CAPITAL|FINANCE|FINANCIAL|BANK|BANCO|BANQUE"
    r"|LLC|LTD|LIMITED|INC|INCORPORATED|CORP|CORPORATION|COMPANY|PLC|GMBH|LLP|PTY|PTE|BHD|KFT|SRO|OYJ"
    r"|HOLDINGS?|GROUP|SERVICES?|SYSTEMS|SOLUTIONS|INDUSTRIES|ENTERPRISES?|INVESTMENTS?|PARTNERS"
    r"|INTERNATIONAL|NATIONAL|MINISTRY|GOVERNMENT|GOVT|POLICE|NAVY|ARMY|FORCE|GUARD|COAST|MARINES?"
    r"|DEPARTMENT|AGENCY|AUTHORITY|COUNTY|UNIVERSITY|COLLEGE|SCHOOL|ACADEMY|INSTITUTE|CLUB"
    r"|ASSOCIATION|SOCIETY|FOUNDATION|AMBULANCE|HOSPITAL|MEDICAL|RESCUE|HEALTH|EXPRESS|CARGO|LOGISTICS"
    r"|TRANSPORT|AIRCRAFT|HELICOPTERS?|TRUST\s+CO|TRUST\s+COMPANY)\b|L\.L\.C|\bCO\.|\bN\.A\.", re.I)
# Short legal-form suffixes only count at the end of the name (elsewhere they may be initials).
_ORG_SUFFIX = re.compile(r"\b(?:AB|AG|AS|ASA|BV|B\.V\.|KG|LP|NA|NV|N\.V\.|OY|SA|S\.A\.|SAS|SPA|S\.P\.A\.|SRL"
                         r"|S\.R\.L\.|SE|CO)\.?$", re.I)
_PERSONAL_TRUST = re.compile(r"\b(?:LIVING|FAMILY|REVOCABLE|IRREVOCABLE)\s+TRUST\b|\bTRUSTEES?\s+OF\s+THE\b", re.I)


def operator_view(*, owner: str | None, flag_code: str | None, db_flags: int = 0, route_airline: str | None = None,
                  military: bool = False) -> tuple[str | None, str]:
    """(text to show, kind) where kind is airline, company, government, private, withheld or unknown."""
    if db_flags & 12:     # LADD (8) or PIA (4)
        return "Withheld (privacy programme)", "withheld"
    name = (owner or "").strip()
    if route_airline and not name:
        return route_airline, "airline"
    if not name:
        return None, "unknown"
    if flag_code and re.fullmatch(r"[A-Z]{3}", flag_code or ""):
        return name, "airline"
    if _PERSONAL_TRUST.search(name):
        return "Private owner", "private"
    if military or re.search(r"\b(?:AIR FORCE|NAVY|ARMY|MINISTRY|GOVERNMENT|POLICE|COAST GUARD|STATE OF)\b", name, re.I):
        return name, "government"
    if _ORG_WORDS.search(name) or _ORG_SUFFIX.search(name):
        return name, "company"
    return "Private owner", "private"


# -- route plausibility ----------------------------------------------------------------------------
def route_check(route: dict | None, lat: float | None, lon: float | None) -> str | None:
    """'plausible' when the aircraft is near the great circle between origin and destination,
    'doubtful' when it is clearly not, None when it cannot be judged."""
    if not route or lat is None or lon is None:
        return None
    o = route.get("origin") or {}
    d = route.get("destination") or {}
    try:
        lat1, lon1, lat2, lon2 = float(o["lat"]), float(o["lon"]), float(d["lat"]), float(d["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    total = geo.haversine_km(lat1, lon1, lat2, lon2)
    if total < 30:
        return None
    r = geo.EARTH_RADIUS_KM
    d13 = geo.haversine_km(lat1, lon1, lat, lon) / r
    t13 = math.radians(geo.bearing_deg(lat1, lon1, lat, lon))
    t12 = math.radians(geo.bearing_deg(lat1, lon1, lat2, lon2))
    xt = math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(t13 - t12)))) * r
    cos_ratio = math.cos(d13) / max(1e-9, math.cos(xt / r))
    at = math.acos(max(-1.0, min(1.0, cos_ratio))) * r
    if math.cos(t13 - t12) < 0:
        at = -at
    slack = max(250.0, 0.2 * total)
    if abs(xt) > slack or at < -slack or at > total + slack:
        return "doubtful"
    return "plausible"


def _airport(a: Any) -> dict | None:
    if not isinstance(a, dict):
        return None
    out = {"icao": _s(a.get("icao_code"), 4), "iata": _s(a.get("iata_code"), 3), "name": _s(a.get("name"), 90),
           "city": _s(a.get("municipality"), 60), "country": _s(a.get("country_name"), 60),
           "country_iso": _s(a.get("country_iso_name"), 2)}
    try:
        out["lat"] = round(float(a["latitude"]), 5)
        out["lon"] = round(float(a["longitude"]), 5)
    except (KeyError, TypeError, ValueError):
        pass
    return out if out["icao"] or out["iata"] or out["name"] else None


def _s(v: Any, n: int) -> str | None:
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    return v[:n] or None


def parse_route(payload: Any) -> dict | None:
    resp = payload.get("response") if isinstance(payload, dict) else None
    fr = resp.get("flightroute") if isinstance(resp, dict) else None
    if not isinstance(fr, dict):
        return None
    airline = fr.get("airline") if isinstance(fr.get("airline"), dict) else {}
    origin = _airport(fr.get("origin"))
    dest = _airport(fr.get("destination"))
    if not origin or not dest:
        return None
    return {"callsign": _s(fr.get("callsign"), 10), "callsign_iata": _s(fr.get("callsign_iata"), 10),
            "airline": {"name": _s(airline.get("name"), 80), "icao": _s(airline.get("icao"), 4),
                        "iata": _s(airline.get("iata"), 3), "country": _s(airline.get("country"), 60)}
            if airline else None,
            "origin": origin, "destination": dest}


def parse_aircraft(payload: Any) -> dict | None:
    resp = payload.get("response") if isinstance(payload, dict) else None
    ac = resp.get("aircraft") if isinstance(resp, dict) else None
    if not isinstance(ac, dict):
        return None
    return {"type": _s(ac.get("type"), 80), "icao_type": _s(ac.get("icao_type"), 6),
            "manufacturer": _s(ac.get("manufacturer"), 80), "registration": _s(ac.get("registration"), 14),
            "owner": _s(ac.get("registered_owner"), 120), "owner_country": _s(ac.get("registered_owner_country_name"), 60),
            "owner_country_iso": _s(ac.get("registered_owner_country_iso_name"), 2),
            "operator_flag": _s(ac.get("registered_owner_operator_flag_code"), 4)}


def route_text(route: dict | None) -> str | None:
    if not route:
        return None
    o, d = route.get("origin") or {}, route.get("destination") or {}
    a = o.get("iata") or o.get("icao") or "?"
    b = d.get("iata") or d.get("icao") or "?"
    return f"{a} to {b}"


class Lookups:
    def __init__(self, net, store, log, queue, status):
        self.net = net
        self.store = store
        self.log = log
        self.queue = queue
        self._status = status
        self.lock = threading.Lock()
        self.mem: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.max_mem = 6000
        self.errors = 0
        store.script(SCHEMA)

    # -- cache ---------------------------------------------------------------------------------
    def _get_cached(self, key: str, ttl: float, neg_ttl: float) -> tuple[bool, Any]:
        now = time.time()
        with self.lock:
            item = self.mem.get(key)
        if item is None:
            row = self.store.one("SELECT v, t FROM flights_lookup WHERE k = ?", (key,))
            if row is not None:
                try:
                    item = (float(row["t"]), json.loads(row["v"]))
                except ValueError:
                    item = None
                if item is not None:
                    with self.lock:
                        self.mem[key] = item
        if item is None:
            return False, None
        t, value = item
        if now - t > (ttl if value is not None else neg_ttl):
            return False, None
        with self.lock:
            if key in self.mem:
                self.mem.move_to_end(key)
        return True, value

    def _put(self, key: str, value: Any) -> None:
        now = time.time()
        with self.lock:
            self.mem[key] = (now, value)
            self.mem.move_to_end(key)
            while len(self.mem) > self.max_mem:
                self.mem.popitem(last=False)
        try:
            self.store.execute("INSERT INTO flights_lookup(k, v, t) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET "
                               "v = excluded.v, t = excluded.t", (key, json.dumps(value), now))
        except Exception as err:
            self.log.debug("lookup cache write failed: %s", err)

    def prune(self) -> None:
        self.store.execute("DELETE FROM flights_lookup WHERE t < ?", (time.time() - AIRCRAFT_TTL,))

    # -- routes ----------------------------------------------------------------------------------
    def cached_route(self, callsign: str | None) -> tuple[str, dict | None]:
        """('ok'|'unknown'|'missing', route)."""
        if not callsign or not CALLSIGN_RE.match(callsign):
            return "unknown", None
        hit, value = self._get_cached("route:" + callsign, ROUTE_TTL, ROUTE_NEG_TTL)
        if not hit:
            return "missing", None
        return ("ok", value) if value else ("unknown", None)

    def route(self, callsign: str | None, *, wait: float = 0.0, priority: int = 2) -> tuple[str, dict | None]:
        status, value = self.cached_route(callsign)
        if status != "missing":
            return status, value
        job = self.queue.submit("route:" + callsign, lambda: self._fetch_route(callsign), priority=priority, ttl=60)
        if job is None:
            return "pending", None
        if wait > 0:
            job.wait(wait)
        if job.done.is_set():
            if job.error is not None:
                return "unavailable", None
            status, value = self.cached_route(callsign)
            return (status, value) if status != "missing" else ("unavailable", None)
        return "pending", None

    def _fetch_route(self, callsign: str) -> dict | None:
        try:
            resp = self.net.request(f"{ADSBDB}/callsign/{callsign}", timeout=10, retries=0, accept=(200, 404))
        except NetError as err:
            self.errors += 1
            self._status(ok=False, error=str(err))
            raise
        route = parse_route(resp.json()) if resp.status == 200 else None
        self._put("route:" + callsign, route)
        self._status(ok=True, note="routes and aircraft details on request")
        return route

    # -- aircraft --------------------------------------------------------------------------------
    def cached_aircraft(self, hexid: str) -> tuple[str, dict | None]:
        if not HEX_RE.match(hexid or ""):
            return "unknown", None
        hit, value = self._get_cached("ac:" + hexid, AIRCRAFT_TTL, AIRCRAFT_NEG_TTL)
        if not hit:
            return "missing", None
        return ("ok", value) if value else ("unknown", None)

    def aircraft(self, hexid: str, *, wait: float = 0.0, priority: int = 2) -> tuple[str, dict | None]:
        status, value = self.cached_aircraft(hexid)
        if status != "missing":
            return status, value
        job = self.queue.submit("ac:" + hexid, lambda: self._fetch_aircraft(hexid), priority=priority, ttl=60)
        if job is None:
            return "pending", None
        if wait > 0:
            job.wait(wait)
        if job.done.is_set():
            if job.error is not None:
                return "unavailable", None
            status, value = self.cached_aircraft(hexid)
            return (status, value) if status != "missing" else ("unavailable", None)
        return "pending", None

    def _fetch_aircraft(self, hexid: str) -> dict | None:
        try:
            resp = self.net.request(f"{ADSBDB}/aircraft/{hexid}", timeout=10, retries=0, accept=(200, 404))
        except NetError as err:
            self.errors += 1
            self._status(ok=False, error=str(err))
            raise
        info = parse_aircraft(resp.json()) if resp.status == 200 else None
        self._put("ac:" + hexid, info)
        self._status(ok=True, note="routes and aircraft details on request")
        return info
