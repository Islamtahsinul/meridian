"""Cloudflare Radar (needs CLOUDFLARE_RADAR_TOKEN): verified internet traffic anomalies, outage annotations,
BGP hijack detections and route leaks.

Pure parsing and scoring plus a small poller (``RadarFeed``). Licence: Cloudflare Radar data is CC BY-NC 4.0.

Endpoints (``Authorization: Bearer <token>``, JSON):
- ``traffic_anomalies?dateRange=7d``: anomalies per country (type LOCATION) or network (type AS). Status
  UNVERIFIED (automatic), TP (verified by Radar) or VERIFIED (by Radar and a third-party source); the
  default answer holds TP and VERIFIED only.
- ``annotations/outages?dateRange=7d``: Radar's Outage Center entries (cause, scope, description).
- ``bgp/hijacks/events?dateRange=1d&minConfidence=8``: possible hijacks, confidence 0-12 with tags.
- ``bgp/leaks/events?dateRange=1d&sortBy=PEERS``: route leaks, the most widely seen first.
"""

from __future__ import annotations

import math
import re
import threading
import time

from .advice import parse_time

API = "https://api.cloudflare.com/client/v4/radar/"
ANOMALIES_URL = API + "traffic_anomalies"
ANNOTATIONS_URL = API + "annotations/outages"
HIJACKS_URL = API + "bgp/hijacks/events"
LEAKS_URL = API + "bgp/leaks/events"
SITE = "https://radar.cloudflare.com/"
OUTAGE_CENTER = "https://radar.cloudflare.com/outage-center"
ROUTING_SITE = "https://radar.cloudflare.com/routing"

ACTIVE_POLL_S = 1800.0        # while the browser uses the module
IDLE_POLL_S = 7200.0          # otherwise (events feed alerts, the briefing and the timeline)
HIJACK_EVENT_MIN_CONF = 10
LEAK_MAP_MIN_PEERS = 20

STATUS_WORDS = {"VERIFIED": "Verified by Radar and a third-party source", "TP": "Verified by Radar",
                "UNVERIFIED": "Detected automatically, not yet verified"}
_RE_UUID = re.compile(r"[0-9a-fA-F-]{8,64}")


def _cc(v) -> str | None:
    s = str(v or "").strip().upper()
    return s if len(s) == 2 and s.isalpha() else None


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _text(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _result(payload, key: str):
    if not isinstance(payload, dict) or payload.get("success") is False:
        errs = (payload or {}).get("errors") if isinstance(payload, dict) else None
        msg = (errs[0] or {}).get("message") if isinstance(errs, list) and errs else None
        raise ValueError(f"Cloudflare Radar answered with an error{': ' + str(msg)[:120] if msg else ''}")
    res = payload.get("result")
    items = res.get(key) if isinstance(res, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"Cloudflare Radar response has no {key} list")
    return res, items


# ---------------------------------------------------------------------------------------------
# Traffic anomalies and outage annotations
# ---------------------------------------------------------------------------------------------
def parse_anomalies(payload) -> list[dict]:
    _, items = _result(payload, "trafficAnomalies")
    out = []
    for a in items:
        if not isinstance(a, dict):
            continue
        uid = str(a.get("uuid") or "")
        if not _RE_UUID.fullmatch(uid):
            continue
        typ = str(a.get("type") or "").upper()
        loc = a.get("locationDetails") or {}
        asn = a.get("asnDetails") or {}
        if typ == "LOCATION":
            iso2, cname = _cc(loc.get("code")), _text(loc.get("name"), 80)
        elif typ == "AS":
            aloc = asn.get("location") or {}
            iso2, cname = _cc(aloc.get("code")), _text(aloc.get("name"), 80)
        else:                         # ORIGIN (cloud regions) has no country: not mapped
            continue
        if not iso2:
            continue
        start = parse_time(a.get("startDate"))
        if start is None:
            continue
        status = str(a.get("status") or "UNVERIFIED").upper()[:16]
        out.append({"id": uid.lower(), "type": "country" if typ == "LOCATION" else "network", "status": status,
                    "verified": status in ("TP", "VERIFIED"), "iso2": iso2, "country": cname,
                    "asn": _int(asn.get("asn")) or None, "as_name": _text(asn.get("name"), 80) or None,
                    "start": start, "end": parse_time(a.get("endDate")),
                    "outage_id": _text(a.get("outageId"), 20) or None,
                    "sources": [_text(s, 24) for s in a.get("visibleInDataSources") or [] if isinstance(s, str)][:6]})
    out.sort(key=lambda x: -x["start"])
    return out


def parse_annotations(payload) -> list[dict]:
    """Outage Center entries with every country they touch (from locations or from the networks' countries)."""
    _, items = _result(payload, "annotations")
    out = []
    for a in items:
        if not isinstance(a, dict):
            continue
        ccs = [_cc(x) for x in a.get("locations") or []]
        ccs += [_cc(x.get("code")) for x in a.get("locationsDetails") or [] if isinstance(x, dict)]
        ccs += [_cc((x.get("location") or {}).get("code")) for x in a.get("asnsDetails") or [] if isinstance(x, dict)]
        countries = sorted({c for c in ccs if c})
        outage = a.get("outage") or {}
        link = str(a.get("linkedUrl") or "")
        out.append({"id": _text(a.get("id"), 20), "description": _text(a.get("description"), 400),
                    "cause": _text(outage.get("outageCause"), 40).replace("_", " ").lower(),
                    "scope_type": _text(outage.get("outageType"), 40).replace("_", " ").lower(),
                    "start": parse_time(a.get("startDate")), "end": parse_time(a.get("endDate")),
                    "countries": countries, "asns": [_int(x) for x in a.get("asns") or [] if _int(x)][:10],
                    "url": link if link.startswith("https://") else None})
    return out


def anomaly_active(a: dict, now: float, grace_s: float = 900.0) -> bool:
    return a.get("end") is None or a["end"] >= now - grace_s


def anomaly_severity(a: dict, now: float, annotation: dict | None = None) -> float:
    """4 for a network, 5 for a whole country; +1 third-party verified, +1 ongoing, +0.5 for 6 h or more,
    +1 for a government-directed shutdown. Capped at 7."""
    s = 5.0 if a.get("type") == "country" else 4.0
    if a.get("status") == "VERIFIED":
        s += 1.0
    elif not a.get("verified"):
        s -= 1.5
    if anomaly_active(a, now):
        s += 1.0
    end = a.get("end") or now
    if end - a["start"] >= 6 * 3600:
        s += 0.5
    if annotation and "government" in (annotation.get("cause") or ""):
        s += 1.0
    return round(max(1.0, min(7.0, s)), 1)


# ---------------------------------------------------------------------------------------------
# BGP
# ---------------------------------------------------------------------------------------------
def _asn_names(res: dict) -> dict[int, dict]:
    out = {}
    for a in (res or {}).get("asn_info") or []:
        if isinstance(a, dict) and _int(a.get("asn")):
            out[_int(a["asn"])] = {"name": _text(a.get("org_name"), 80), "iso2": _cc(a.get("country_code"))}
    return out


def parse_hijacks(payload) -> list[dict]:
    res, items = _result(payload, "events")
    names = _asn_names(res)
    out = []
    for e in items:
        if not isinstance(e, dict) or not _int(e.get("id")):
            continue
        h_asn = _int(e.get("hijacker_asn")) or None
        v_asns = [_int(x) for x in e.get("victim_asns") or [] if _int(x)][:12]
        tags = [{"name": _text(t.get("name"), 40), "score": _int(t.get("score"))}
                for t in e.get("tags") or [] if isinstance(t, dict)]
        start = parse_time(e.get("min_hijack_ts"))
        last = parse_time(e.get("max_hijack_ts")) or parse_time(e.get("max_msg_ts")) or start
        if start is None:
            continue
        stale = bool(e.get("is_stale"))
        out.append({"id": _int(e["id"]), "confidence": _int(e.get("confidence_score")),
                    "hijacker_asn": h_asn, "hijacker_name": (names.get(h_asn) or {}).get("name") if h_asn else None,
                    "hijacker_iso2": _cc(e.get("hijacker_country")) or (names.get(h_asn) or {}).get("iso2"),
                    "victim_asns": v_asns, "victim_names": [(names.get(a) or {}).get("name") or "" for a in v_asns],
                    "victim_iso2": sorted({c for c in (_cc(x) for x in e.get("victim_countries") or []) if c})[:8],
                    "prefixes": [_text(p, 50) for p in e.get("prefixes") or [] if isinstance(p, str)][:12],
                    "n_prefixes": len(e.get("prefixes") or []), "start": start, "last": last,
                    "duration_s": _int(e.get("duration")), "ongoing": _int(e.get("on_going_count")) > 0 and not stale,
                    "stale": stale, "peers": _int(e.get("peer_ip_count")) or len(e.get("peer_asns") or []),
                    "tags": [t for t in tags if t["score"] > 0][:8]})
    out.sort(key=lambda h: (-h["confidence"], -h["last"]))
    return out


def parse_leaks(payload) -> list[dict]:
    res, items = _result(payload, "events")
    names = _asn_names(res)
    out = []
    for e in items:
        if not isinstance(e, dict) or not _int(e.get("id")):
            continue
        asn = _int(e.get("leak_asn")) or None
        ccs = [_cc(x) for x in e.get("countries") or []]
        # "countries" is not in AS-path order, so the leaking network's own country comes from asn_info
        leak_cc = (names.get(asn) or {}).get("iso2") or (ccs[1] if len(ccs) >= 3 else None)
        detected = parse_time(e.get("detected_ts")) or parse_time(e.get("min_ts"))
        if detected is None:
            continue
        seg = [_int(x) for x in e.get("leak_seg") or [] if _int(x)][:6]
        out.append({"id": _int(e["id"]), "leak_asn": asn, "leak_name": (names.get(asn) or {}).get("name") if asn else None,
                    "leak_iso2": leak_cc, "countries": sorted({c for c in ccs if c}),
                    "segment": [{"asn": a, "name": (names.get(a) or {}).get("name") or "", "iso2": (names.get(a) or {}).get("iso2")}
                                for a in seg],
                    "prefixes": _int(e.get("prefix_count")), "origins": _int(e.get("origin_count")),
                    "peers": _int(e.get("peer_count")), "leaks": _int(e.get("leak_count")),
                    "start": detected, "last": parse_time(e.get("max_ts")) or detected, "finished": bool(e.get("finished"))})
    out.sort(key=lambda x: (-x["peers"], -x["last"]))
    return out


def hijack_severity(h: dict) -> float:
    """Confidence 10 -> 4, 12 -> 5; +0.5 when ongoing, +0.5 when it touches 5+ prefixes or 2+ victim countries; max 6."""
    s = 4.0 + max(0, h.get("confidence", 0) - 10) / 2.0
    if h.get("ongoing"):
        s += 0.5
    if h.get("n_prefixes", 0) >= 5 or len(h.get("victim_iso2") or []) >= 2:
        s += 0.5
    return round(max(1.0, min(6.0, s)), 1)


def asn_label(asn: int | None, name: str | None) -> str:
    if not asn:
        return name or "an unknown network"
    return f"AS{asn} ({name})" if name else f"AS{asn}"


def hijack_title(h: dict, country_name) -> str:
    victims = ", ".join(country_name(c) for c in h.get("victim_iso2") or []) or "unknown countries"
    who = asn_label(h.get("hijacker_asn"), h.get("hijacker_name"))
    return f"Possible BGP hijack of networks in {victims} by {who}"


def leak_title(lk: dict, country_name) -> str:
    where = country_name(lk["leak_iso2"]) if lk.get("leak_iso2") else "an unknown country"
    return f"Route leak by {asn_label(lk.get('leak_asn'), lk.get('leak_name'))}, {where}"


def great_circle(lon1: float, lat1: float, lon2: float, lat2: float, n: int = 24) -> list[list[float]]:
    """Points along the great circle (for arcs that look right on the globe); longitudes kept continuous."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    if d < 1e-9:
        return [[round(lon1, 4), round(lat1, 4)], [round(lon2, 4), round(lat2, 4)]]
    pts, prev = [], None
    for i in range(n + 1):
        f = i / n
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
        y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
        z = a * math.sin(p1) + b * math.sin(p2)
        lat = math.degrees(math.atan2(z, math.sqrt(x * x + y * y)))
        lon = math.degrees(math.atan2(y, x))
        if prev is not None:
            while lon - prev > 180:
                lon -= 360
            while lon - prev < -180:
                lon += 360
        prev = lon
        pts.append([round(lon, 4), round(lat, 4)])
    return pts


# ---------------------------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------------------------
class RadarFeed:
    """Polls the four Radar endpoints (every 30 min while in use, every 2 h otherwise) and keeps snapshots."""

    def __init__(self, net, log, token_fn, status_fn, active_fn):
        self.net = net
        self.log = log
        self.token_fn = token_fn
        self.status = status_fn          # status(source_key, ok=..., count=..., error=..., note=...)
        self.active = active_fn
        self.lock = threading.Lock()
        self.anomalies: list[dict] = []
        self.annotations: list[dict] = []
        self.hijacks: list[dict] = []
        self.leaks: list[dict] = []
        self.at: dict[str, float | None] = {"anomalies": None, "annotations": None, "hijacks": None, "leaks": None}
        self.errors: dict[str, str | None] = {}
        self.last_poll = 0.0

    def has_token(self) -> bool:
        return bool(self.token_fn())

    def due(self, now: float) -> bool:
        if not self.has_token():
            return False
        gap = ACTIVE_POLL_S if self.active() else IDLE_POLL_S
        return now - self.last_poll >= gap - 60

    def _get(self, url: str, params: dict, token: str):
        return self.net.get_json(url, params=params, headers={"Authorization": f"Bearer {token}"},
                                 timeout=25, retries=0, ttl=600)

    def poll(self, force: bool = False) -> None:
        token = self.token_fn()
        if not token:
            return
        now = time.time()
        if not force and not self.due(now):
            return
        self.last_poll = now
        jobs = (("anomalies", ANOMALIES_URL, {"dateRange": "7d", "limit": 100, "format": "json"}, parse_anomalies),
                ("annotations", ANNOTATIONS_URL, {"dateRange": "7d", "limit": 100, "format": "json"}, parse_annotations),
                ("hijacks", HIJACKS_URL, {"dateRange": "1d", "minConfidence": 8, "per_page": 100, "format": "json"},
                 parse_hijacks),
                ("leaks", LEAKS_URL, {"dateRange": "1d", "per_page": 50, "sortBy": "PEERS", "sortOrder": "DESC",
                                      "format": "json"}, parse_leaks))
        for key, url, params, parse in jobs:
            try:
                items = parse(self._get(url, params, token))
                with self.lock:
                    setattr(self, key, items)
                    self.at[key] = now
                    self.errors[key] = None
            except Exception as err:      # one failing endpoint keeps the others and the old snapshot
                with self.lock:
                    self.errors[key] = str(err)[:200]
                self.log.info("Cloudflare Radar %s failed: %s", key, err)
        with self.lock:
            e_out = [self.errors.get(k) for k in ("anomalies", "annotations")]
            e_bgp = [self.errors.get(k) for k in ("hijacks", "leaks")]
            n_out, n_bgp = len(self.anomalies), len(self.hijacks)
        if all(e_out):
            self.status("outages", ok=False, error=f"Cloudflare Radar: {e_out[0]}")
        else:
            self.status("outages", ok=True, count=n_out,
                        note=f"{n_out} verified traffic anomalies and {len(self.annotations)} outage reports in 7 days")
        if all(e_bgp):
            self.status("bgp", ok=False, error=f"Cloudflare Radar BGP: {e_bgp[0]}")
        else:
            self.status("bgp", ok=True, count=n_bgp,
                        note=f"{n_bgp} possible hijacks (confidence 8+) and {len(self.leaks)} route leaks in 24 h")

    def snapshot(self) -> dict:
        with self.lock:
            return {"anomalies": list(self.anomalies), "annotations": list(self.annotations), "hijacks": list(self.hijacks),
                    "leaks": list(self.leaks), "at": dict(self.at), "errors": dict(self.errors)}

    def annotation_of(self, outage_id: str | None) -> dict | None:
        if not outage_id:
            return None
        with self.lock:
            return next((a for a in self.annotations if a["id"] == outage_id), None)
