"""Intelligence+ additions to the intel service (mixed into ``service.Intel``): Cloudflare Radar anomalies and
BGP events, official travel advice, protests and news spikes. Entity and event shapes live here too."""

from __future__ import annotations

import re
import time

from . import advice, codes, db, gdelt, news, protests, radar
from .advice_service import TravelAdvice
from .seed import HistorySeed
from .spikes import METRICS, SpikeDetector, headline

PROTEST_EVENT_MIN_SEV = 6.5        # single protests worth an event (others stay on the map)
SPIKE_KIND = "news_spike"
ADVICE_KIND = "advice_change"


def _r5(x):
    return round(float(x), 5)


def _utc(t: float | None) -> str:
    return time.strftime("%d %b %H:%M UTC", time.gmtime(t)) if t else ""


class IntelPlus:
    # =========================================================================================
    # Set-up
    # =========================================================================================
    def init_plus(self) -> None:
        m = self.m
        self.protest_places: list[dict] = []
        self.radar = radar.RadarFeed(m.net, m.log, token_fn=lambda: m.config.get("CLOUDFLARE_RADAR_TOKEN"),
                                     status_fn=self._radar_status, active_fn=lambda: m.active(within=1800))
        self.advice = TravelAdvice(m, self.resolve_country, self.iso3_to_iso2, codes.fips_to_iso2)
        self.spikes = SpikeDetector(m.store, m.log, self.country_name, self._label_point)
        self.seed = HistorySeed(self, m.config.get_int("INTEL_SEED_DAYS", 16), m.config.get_float("INTEL_SEED_MAX_MB", 150.0))

    def _radar_status(self, key: str, **kw) -> None:
        self.m.status("intel.cloudflare_radar" if key == "outages" else "intel.cloudflare_bgp", **kw)

    def iso3_to_iso2(self, iso3: str | None) -> str | None:
        up = (iso3 or "").upper()
        if len(up) != 3:
            return None
        for iso2, p in self.countries.items():
            if p.get("iso3") == up:
                return iso2
        for iso2, e in self.wb.items():
            if (e.get("iso3") or "").upper() == up:
                return iso2
        return None

    def spikes_note(self) -> str:
        res = self.spikes.result or {}
        if res.get("state") == "ok":
            n = sum(1 for f in res.get("flags") or [] if f["level"] == "unusual")
            return f"{n} unusual, {len(res.get('flags') or []) - n} notable"
        return (res.get("message") or "waiting").rstrip(".").lower()

    def derive_plus(self, now: float) -> None:
        places = db.event_places(self.store, now - 7 * 86400, table="intel_protests")
        with self.lock:
            self.protest_places = places
        self.spikes.evaluate(now)

    # =========================================================================================
    # Cloudflare Radar
    # =========================================================================================
    def poll_radar(self) -> None:
        if not self.radar.has_token():
            return
        self.radar.poll()
        snap = self.radar.snapshot()
        rows = []
        for a in snap["annotations"]:
            for cc in a["countries"]:
                rows.append({"iso2": cc, "id": a["id"], "description": a["description"], "cause": a["cause"],
                             "type": a["scope_type"], "start": _iso(a["start"]), "end": _iso(a["end"]), "scope": "",
                             "url": a["url"]})
        with self.lock:
            self.outages["cloudflare"] = rows
            self.outages_at["cloudflare"] = snap["at"].get("annotations")

    def anomaly_public(self, a: dict, now: float | None = None) -> dict:
        now = now or time.time()
        ann = self.radar.annotation_of(a.get("outage_id"))
        where = self.country_name(a["iso2"])
        if (not where or where == a["iso2"]) and a.get("country"):      # territories Natural Earth folds into a country
            where = a["country"]
        if a["type"] == "network":
            title = f"Traffic anomaly on {radar.asn_label(a.get('asn'), a.get('as_name'))}, {where}"
        else:
            title = f"Internet traffic anomaly in {where}"
        out = dict(a)
        out.update({"title": title, "name": where, "active": radar.anomaly_active(a, now),
                    "severity": radar.anomaly_severity(a, now, ann), "status_words": radar.STATUS_WORDS.get(a["status"], a["status"]),
                    "annotation": ann, "url": f"{radar.SITE}traffic/{a['iso2'].lower()}" if a["type"] == "country"
                    else (f"{radar.SITE}as{a['asn']}" if a.get("asn") else radar.OUTAGE_CENTER)})
        return out

    def anomaly_entity(self, a: dict, now: float | None = None) -> dict | None:
        pub = self.anomaly_public(a, now)
        pos = self._label_point(a["iso2"])
        if not pos:
            return None
        span = f"{_utc(a['start'])} to {_utc(a['end'])}" if a.get("end") else f"since {_utc(a['start'])}, ongoing"
        return {"kind": "outage", "id": f"cfr:{a['id']}", "title": pub["title"],
                "subtitle": f"Cloudflare Radar, {pub['status_words'].lower()}; {span}", "lat": pos[1], "lon": pos[0],
                "layer": "intel.cyber", "zoom": 4, "time": (a.get("end") or now or time.time()), "first_time": a["start"],
                "severity": pub["severity"], "iso2": a["iso2"], "source": "Cloudflare Radar"}

    def hijack_public(self, h: dict) -> dict:
        out = dict(h)
        out.update({"title": radar.hijack_title(h, self.country_name),
                    "hijacker_country": self.country_name(h.get("hijacker_iso2")) if h.get("hijacker_iso2") else None,
                    "victim_countries": [self.country_name(c) for c in h.get("victim_iso2") or []],
                    "severity": radar.hijack_severity(h),
                    "url": f"{radar.ROUTING_SITE}/as{h['hijacker_asn']}" if h.get("hijacker_asn") else radar.ROUTING_SITE})
        return out

    def hijack_entity(self, h: dict) -> dict | None:
        iso = (h.get("victim_iso2") or [None])[0] or h.get("hijacker_iso2")
        pos = self._label_point(iso) if iso else None
        if not pos:
            return None
        pub = self.hijack_public(h)
        return {"kind": "cyber", "id": f"cfbgp:{h['id']}", "title": pub["title"],
                "subtitle": f"Cloudflare Radar BGP detection, confidence {h['confidence']} of 12"
                            + (", ongoing" if h.get("ongoing") else ""),
                "lat": pos[1], "lon": pos[0], "layer": "intel.cyber", "zoom": 4, "time": h["last"], "first_time": h["start"],
                "severity": pub["severity"], "iso2": iso, "source": "Cloudflare Radar"}

    def leak_public(self, lk: dict) -> dict:
        out = dict(lk)
        out.update({"title": radar.leak_title(lk, self.country_name),
                    "country": self.country_name(lk.get("leak_iso2")) if lk.get("leak_iso2") else None,
                    "url": f"{radar.ROUTING_SITE}/as{lk['leak_asn']}" if lk.get("leak_asn") else radar.ROUTING_SITE})
        return out

    def leak_entity(self, lk: dict) -> dict | None:
        pos = self._label_point(lk["leak_iso2"]) if lk.get("leak_iso2") else None
        if not pos:
            return None
        return {"kind": "cyber", "id": f"cfleak:{lk['id']}", "title": radar.leak_title(lk, self.country_name),
                "subtitle": f"Cloudflare Radar route leak, seen by {lk['peers']} peers", "lat": pos[1], "lon": pos[0],
                "layer": "intel.cyber", "zoom": 4, "time": lk["last"], "severity": 3.0, "iso2": lk["leak_iso2"]}

    def radar_country(self, iso2: str, now: float | None = None) -> dict:
        now = now or time.time()
        snap = self.radar.snapshot()
        an = [self.anomaly_public(a, now) for a in snap["anomalies"] if a["iso2"] == iso2]
        hj = [self.hijack_public(h) for h in snap["hijacks"]
              if h.get("hijacker_iso2") == iso2 or iso2 in (h.get("victim_iso2") or [])]
        lk = [self.leak_public(x) for x in snap["leaks"] if x.get("leak_iso2") == iso2 or iso2 in (x.get("countries") or [])]
        return {"token": self.radar.has_token(), "at": snap["at"], "errors": snap["errors"], "anomalies": an,
                "hijacks": hj[:20], "hijacks_as_victim": sum(1 for h in hj if iso2 in (h.get("victim_iso2") or [])),
                "hijacks_as_origin": sum(1 for h in hj if h.get("hijacker_iso2") == iso2), "leaks": lk[:10],
                "leaks_count": len(lk)}

    def radar_outage_rings(self, by_cc: dict) -> None:
        """Merge Radar traffic anomalies of the last 24 hours (and ongoing ones) into the outage rings."""
        now = time.time()
        for a in self.radar.snapshot()["anomalies"]:
            if not a["verified"] or ((a.get("end") or now) < now - 86400):
                continue
            cur = by_cc.setdefault(a["iso2"], {"iso2": a["iso2"], "sev": 0.0, "events": 0, "src": "Cloudflare Radar"})
            if "Cloudflare" not in cur["src"]:
                cur["src"] += ", Cloudflare Radar"
            cur["radar"] = cur.get("radar", 0) + 1
            cur["sev"] = max(cur["sev"], radar.anomaly_severity(a, now, self.radar.annotation_of(a.get("outage_id"))))
            if radar.anomaly_active(a, now):
                cur["active"] = 1

    def bgp_features(self) -> list[dict]:
        """Possible hijacks (24 h, confidence 8+) as arcs from the announcing network's country to the affected
        one (rings when both are the same), and widely seen route leaks (20+ peers) as rings."""
        snap = self.radar.snapshot()
        groups: dict[tuple[str, str], dict] = {}
        for h in snap["hijacks"]:
            a = h.get("hijacker_iso2")
            for b in (h.get("victim_iso2") or ([a] if a else [])):
                if not a or not b:
                    continue
                g = groups.setdefault((a, b), {"n": 0, "n10": 0, "n12": 0, "conf": 0, "ongoing": 0})
                g["n"] += 1
                g["n10"] += 1 if h["confidence"] >= 10 else 0
                g["n12"] += 1 if h["confidence"] >= 12 else 0
                g["conf"] = max(g["conf"], h["confidence"])
                g["ongoing"] += 1 if h.get("ongoing") else 0
        feats = []
        for (a, b), g in groups.items():
            pa, pb = self._label_point(a), self._label_point(b)
            props = {"a": a, "b": b, "an": self.country_name(a), "bn": self.country_name(b), "n": g["n"], "n10": g["n10"],
                     "n12": g["n12"], "conf": g["conf"], "ongoing": g["ongoing"], "pair": f"{a}-{b}"}
            if a == b or not pa or not pb:
                p = pb or pa
                if p:
                    feats.append({"type": "Feature", "properties": dict(props, role="bgp-local", iso2=b),
                                  "geometry": {"type": "Point", "coordinates": p}})
                continue
            feats.append({"type": "Feature", "properties": dict(props, role="bgp"),
                          "geometry": {"type": "LineString", "coordinates": radar.great_circle(pa[0], pa[1], pb[0], pb[1])}})
        leaks: dict[str, dict] = {}
        for lk in snap["leaks"]:
            if lk["peers"] < radar.LEAK_MAP_MIN_PEERS or not lk.get("leak_iso2"):
                continue
            g = leaks.setdefault(lk["leak_iso2"], {"n": 0, "peers": 0})
            g["n"] += 1
            g["peers"] = max(g["peers"], lk["peers"])
        for iso2, g in leaks.items():
            p = self._label_point(iso2)
            if p:
                feats.append({"type": "Feature", "properties": {"role": "leak", "iso2": iso2, "name": self.country_name(iso2),
                                                                "n": g["n"], "peers": g["peers"]},
                              "geometry": {"type": "Point", "coordinates": p}})
        return feats

    def radar_events(self, since: float) -> list[dict]:
        now = time.time()
        snap = self.radar.snapshot()
        out = []
        for a in snap["anomalies"]:
            if not a["verified"] or (a.get("end") or now) < since:
                continue
            ent = self.anomaly_entity(a, now)
            if ent:
                out.append(ent)
        for h in snap["hijacks"]:
            if h["confidence"] < radar.HIJACK_EVENT_MIN_CONF or not h.get("ongoing") or h["last"] < since:
                continue
            ent = self.hijack_entity(h)
            if ent:
                out.append(ent)
        return out

    # =========================================================================================
    # Protests
    # =========================================================================================
    def protest_entity(self, ev: dict) -> dict:
        return {"kind": "protest", "id": ev["id"], "title": protests.title(ev),
                "subtitle": f"{self.place_display(ev.get('place') or '', self.country_name(ev.get('iso2'))) or 'Unknown place'}, "
                            f"reported {_utc(ev['t_last'])}",
                "lat": _r5(ev["lat"]), "lon": _r5(ev["lon"]), "layer": "intel.protests", "time": ev["t_last"],
                "severity": ev["severity"], "zoom": 9, "category": ev.get("category")}

    def protest_detail(self, ev: dict) -> dict:
        d = {k: ev.get(k) for k in ("id", "day", "t_first", "t_last", "code", "goldstein", "category", "precision", "place",
                                    "iso2", "actor1", "actor2", "actors", "mentions", "sources", "articles", "tone",
                                    "reports", "urls", "severity")}
        d["lat"], d["lon"] = _r5(ev["lat"]), _r5(ev["lon"])
        d["action"] = protests.label(ev.get("code"))
        d["category_label"] = protests.CATEGORY_LABEL.get(ev.get("category"), "Protest")
        d["country"] = self.country_name(ev.get("iso2"))
        d["title"] = protests.title(ev)
        d["place_display"] = self.place_display(ev.get("place") or "", d["country"])
        d["main_url"] = ev.get("url_main")
        day = gdelt.day_to_date(ev["day"])
        d["date"] = day.isoformat() if day else None
        d["note"] = "Machine-coded from news reports, not verified."
        d["single_source"] = not (ev.get("sources", 0) >= 2 or ev.get("articles", 0) >= 3)
        words = self.proper_words | set(re.findall(r"[A-Za-z]+", ev.get("place") or ""))
        d["sources_list"] = [{"url": u, "domain": gdelt.domain_of(u), "title": gdelt.slug_title(u, words)}
                             for u in ev.get("urls") or []]
        d["theatres"] = [news.THEATRE_BY_ID[t]["name"] for t in news.theatres_of(ev["lat"], ev["lon"], ev.get("iso2"))]
        return d

    def protest_events(self, since: float) -> list[dict]:
        now = time.time()
        rows = db.query_events(self.store, since=max(since, now - db.EVENT_DAYS * 86400), min_severity=PROTEST_EVENT_MIN_SEV,
                               table="intel_protests", min_support=True, limit=500)
        return [self.protest_entity(ev) for ev in rows if ev.get("sources", 0) >= 3 or ev.get("articles", 0) >= 5]

    # =========================================================================================
    # News spikes
    # =========================================================================================
    def spike_entity(self, sp: dict) -> dict | None:
        det = sp.get("detail") or {}
        pos = self._label_point(sp["iso2"])
        lat, lon = (pos[1], pos[0]) if pos else (det.get("lat"), det.get("lon"))
        if lat is None or lon is None:
            return None
        name = self.country_name(sp["iso2"]) or det.get("name") or sp["iso2"]
        metric = sp["metric"]
        return {"kind": SPIKE_KIND, "id": sp["id"], "title": headline(metric, name),
                "subtitle": f"{sp['value']:,} {METRICS[metric]['noun']} in 24 hours, usual {sp['baseline']:,.0f} "
                            f"({sp['ratio']:.1f} times); GDELT, machine-coded",
                "lat": lat, "lon": lon, "layer": "intel.countries", "zoom": 4, "time": sp["last_t"],
                "first_time": sp["first_t"], "severity": sp["severity"], "iso2": sp["iso2"], "category": metric,
                "ratio": sp["ratio"], "value": sp["value"], "baseline": sp["baseline"]}

    def spike_events(self, since: float) -> list[dict]:
        out = []
        for sp in db.query_spikes(self.store, since=max(since, time.time() - 30 * 86400), limit=300):
            ent = self.spike_entity(sp)
            if ent:
                out.append(ent)
        return out

    # =========================================================================================
    # Travel advice
    # =========================================================================================
    def advice_entity(self, ch: dict) -> dict | None:
        pos = self._label_point(ch["iso2"])
        if not pos:
            return None
        src = "FCDO travel advice" if ch["source"] == "fcdo" else "US State Department travel advisory"
        return {"kind": ADVICE_KIND, "id": ch["id"], "title": ch["text"][:200],
                "subtitle": f"{src}, {_utc(ch['t'])}", "lat": pos[1], "lon": pos[0], "layer": "intel.advice", "zoom": 4,
                "time": ch["t"], "severity": advice.change_severity(ch.get("new_level"), ch.get("old_level"), ch["kind"],
                                                                   (ch.get("data") or {}).get("note")),
                "iso2": ch["iso2"], "source": ch["source"], "category": ch["kind"],
                "old_level": ch.get("old_level"), "new_level": ch.get("new_level")}

    def advice_events(self, since: float) -> list[dict]:
        out = []
        for ch in self.advice.changes(max(since, time.time() - 30 * 86400), limit=300):
            ent = self.advice_entity(ch)
            if ent:
                out.append(ent)
        return out

    # =========================================================================================
    # Country profile and status
    # =========================================================================================
    def profile_plus(self, iso2: str, now: float) -> dict:
        adv = self.advice.country(iso2)
        recent = db.query_events(self.store, iso2=iso2, since=now - 7 * 86400, table="intel_protests", min_support=True,
                                 limit=8)
        n7 = int(self.store.scalar("SELECT COUNT(*) FROM intel_protests WHERE iso2 = ? AND t_last >= ? AND "
                                   "(sources >= 2 OR articles >= 3)", (iso2, now - 7 * 86400), 0))
        try:
            sp = self.spikes.country(iso2, now)
        except Exception as err:          # the card must still open
            self.log.warning("spike reading for %s failed: %s", iso2, err)
            sp = {"state": "error", "message": "Unavailable right now.", "metrics": {}, "series": []}
        rc = self.radar_country(iso2, now)
        return {"travel_advice": {"fcdo": adv["fcdo"], "us": adv["us"], "changes": adv["changes"][:6],
                                  "levels": adv["levels"]},
                "internet": {"token": rc["token"], "at": rc["at"].get("anomalies"), "anomalies": rc["anomalies"][:8],
                             "hijacks": rc["hijacks"][:6], "hijacks_as_victim": rc["hijacks_as_victim"],
                             "hijacks_as_origin": rc["hijacks_as_origin"], "leaks": rc["leaks"][:4],
                             "leaks_count": rc["leaks_count"]},
                "protests": {"events_7d": n7, "recent": [self.protest_entity(e) for e in recent]},
                "news_spikes": sp}

    def status_plus(self) -> dict:
        snap = self.radar.snapshot()
        res = self.spikes.result or {}
        return {"radar": {"token": self.radar.has_token(), "at": snap["at"], "errors": snap["errors"],
                          "anomalies": len(snap["anomalies"]), "hijacks": len(snap["hijacks"]), "leaks": len(snap["leaks"])},
                "advice": {"fcdo": dict(self.advice.state["fcdo"]), "us": dict(self.advice.state["us"]),
                           "fcdo_countries": len(self.advice.fcdo), "us_countries": len(self.advice.us)},
                "spikes": {"state": res.get("state"), "message": res.get("message"), "computed_at": res.get("computed_at"),
                           "flags": len(res.get("flags") or []), "baseline_days": res.get("baseline_days"),
                           "coverage": res.get("coverage")},
                "seed": dict(self.seed.state),
                "protests_14d": db.count_events(self.store, time.time() - db.EVENT_DAYS * 86400, table="intel_protests")}


def _iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None
