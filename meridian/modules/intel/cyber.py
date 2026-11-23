"""Cyber: botnet command servers (abuse.ch Feodo Tracker) by hosting country, plus internet
connectivity outages from IODA (keyless, optional) and Cloudflare Radar (optional token)."""

from __future__ import annotations

import math
import time
from collections import Counter

FEODO_URL = "https://feodotracker.abuse.ch/downloads/ipblocklist.json"
FEODO_SITE = "https://feodotracker.abuse.ch/"
IODA_URL = "https://api.ioda.inetintel.cc.gatech.edu/v2/outages/summary"
IODA_SITE = "https://ioda.inetintel.cc.gatech.edu/"
CF_URL = "https://api.cloudflare.com/client/v4/radar/annotations/outages"
CF_SITE = "https://radar.cloudflare.com/outage-center"

IODA_METHODS = {
    "bgp": "BGP routing", "ping-slash24": "active probing", "merit-nt": "network telescope",
    "gtr": "Google traffic", "gtr-norm": "Google traffic",
}


def parse_feodo(payload) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError("Feodo list is not a JSON array")
    out = []
    for e in payload:
        if not isinstance(e, dict) or not e.get("ip_address"):
            continue
        cc = (e.get("country") or "").strip().upper()
        out.append({"ip": str(e.get("ip_address"))[:64], "port": e.get("port"),
                    "status": (e.get("status") or "unknown").lower(), "as_number": e.get("as_number"),
                    "as_name": (e.get("as_name") or "")[:120], "country": cc if len(cc) == 2 and cc.isalpha() else None,
                    "first_seen": (e.get("first_seen") or "")[:19], "last_online": (e.get("last_online") or "")[:19],
                    "malware": (e.get("malware") or "unknown")[:60]})
    return out


def aggregate_feodo(entries: list[dict]) -> dict[str, dict]:
    by: dict[str, dict] = {}
    for e in entries:
        cc = e.get("country")
        if not cc:
            continue
        d = by.setdefault(cc, {"iso2": cc, "count": 0, "online": 0, "malware": Counter(), "networks": Counter(),
                               "entries": []})
        d["count"] += 1
        d["online"] += 1 if e["status"] == "online" else 0
        d["malware"][e["malware"]] += 1
        if e.get("as_name"):
            d["networks"][f"{e['as_name']} (AS{e['as_number']})" if e.get("as_number") else e["as_name"]] += 1
        d["entries"].append(e)
    for d in by.values():
        d["malware"] = d["malware"].most_common()
        d["networks"] = d["networks"].most_common(8)
        d["entries"].sort(key=lambda e: (e["status"] != "online", e["last_online"] or ""), reverse=False)
    return by


def parse_ioda(payload) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("IODA summary has no data list")
    out = []
    for item in data:
        ent = (item or {}).get("entity") or {}
        code = (ent.get("code") or "").upper()
        if ent.get("type") != "country" or len(code) != 2:
            continue
        scores = item.get("scores") or {}
        try:
            overall = float(scores.get("overall") or 0)
        except (TypeError, ValueError):
            overall = 0.0
        methods = sorted({IODA_METHODS.get(k.split(".")[0], k.split(".")[0]) for k in scores if k != "overall"})
        out.append({"iso2": code, "name": (ent.get("name") or code)[:80], "events": int(item.get("event_cnt") or 0),
                    "score": overall, "methods": methods, "severity": ioda_severity(overall)})
    out.sort(key=lambda o: -o["score"])
    return out


def ioda_severity(score: float) -> float:
    if not score or score <= 0:
        return 0.0
    return round(min(9.0, 1.6 * math.log10(1.0 + score)), 1)


def parse_cloudflare(payload) -> list[dict]:
    res = (payload or {}).get("result") if isinstance(payload, dict) else None
    anns = (res or {}).get("annotations") if isinstance(res, dict) else None
    if not isinstance(anns, list):
        raise ValueError("Cloudflare Radar response has no annotations")
    out = []
    for a in anns:
        locs = [str(x).upper() for x in (a.get("locations") or []) if isinstance(x, str) and len(x) == 2]
        outage = a.get("outage") or {}
        for cc in locs or [None]:
            if not cc:
                continue
            out.append({"iso2": cc, "id": str(a.get("id") or "")[:40], "description": (a.get("description") or "")[:300],
                        "cause": (outage.get("outageCause") or "").replace("_", " ").lower(),
                        "type": (outage.get("outageType") or "").replace("_", " ").lower(),
                        "start": a.get("startDate"), "end": a.get("endDate"), "scope": (a.get("scope") or "")[:120]})
    return out


def feodo_snapshot(entries: list[dict], built_at: float | None = None) -> dict:
    return {"built_at": built_at or time.time(), "entries": entries, "count": len(entries)}
