"""The infra module's in-memory state: dataset loaders, details by id, search, nearby, the country
summary (CONTRACT 9.1) and the AI tools. Snapshots are immutable and swapped atomically."""

from __future__ import annotations

import collections
import json
import re
import time
from pathlib import Path

from meridian import geo

from . import airports, cables, companies, countries, dams, datacenters, ports, power
from .common import EMPTY, Collection, Holder, norm, r5, read_json, text

# kind -> holder key
KIND_COLLECTION = {"power_plant": "power", "airport": "airports", "port": "ports", "datacenter": "datacenters",
                   "dam": "dams", "cable": "cables", "landing_point": "landings", "company": "companies"}
SEARCH_ORDER = ("airports", "ports", "power", "companies", "cables", "landings", "dams", "datacenters")
NEARBY_ORDER = ("airports", "ports", "power", "datacenters", "dams", "landings", "companies")
NEARBY_KINDS = {"airport": "airports", "port": "ports", "power_plant": "power", "datacenter": "datacenters",
                "dam": "dams", "landing_point": "landings", "company": "companies"}
ID_RE = re.compile(r"^[A-Za-z0-9:_./\-]{1,80}$")


class State:
    def __init__(self, m):
        self.m = m
        self.data_dir: Path = m.data_dir
        self.cache_dir: Path = m.cache_dir
        self.holder = Holder()
        self._country_cache: tuple[int, dict] = (-1, {})
        self.markets = None   # set by register()
        self.energy = None    # the live-grid service (energy_service.Energy), set by register()

    # -- access ----------------------------------------------------------------------------------
    def coll(self, name: str) -> Collection:
        return self.holder.get(name, EMPTY)

    def counts(self) -> dict[str, int]:
        return {k: len(self.coll(k)) for k in ("power", "airports", "ports", "datacenters", "dams", "cables",
                                               "landings", "companies")}

    # -- loaders (run after each build and at start-up) -------------------------------------------
    def _doc(self, name: str) -> dict:
        return read_json(self.data_dir / name)

    def load_power(self) -> None:
        doc = self._doc("power-details.json")
        items = doc["items"]
        self.holder.set("power", power.collection(items, doc.get("built_at")))
        self.holder.set("power_stats", power.country_stats(items))
        gw = sum(p.get("capacity_mw") or 0 for p in items) / 1000
        self.m.status(power.SOURCE_ID, ok=True, count=len(items), note=f"{len(items):,} plants, {gw:,.0f} GW")

    def load_airports(self) -> None:
        doc = self._doc("airports-details.json")
        items = doc["items"]
        self.holder.set("airports", airports.collection(items, doc.get("built_at")))
        self.holder.set("airport_stats", airports.country_counts(items))
        n3 = sum(1 for a in items if a["type"] == 3)
        n2 = sum(1 for a in items if a["type"] == 2)
        self.m.status(airports.SOURCE_ID, ok=True, count=len(items),
                      note=f"{n3:,} large, {n2:,} medium, {len(items) - n3 - n2:,} small airports")

    def load_ports(self) -> None:
        if not (self.data_dir / "ports-details.json").exists():
            return
        doc = self._doc("ports-details.json")
        items = doc["items"]
        aliases = {}
        for p in items:
            if p["id"].startswith("wpi:") and p.get("locode"):
                aliases["locode:" + p["locode"].replace(" ", "").upper()] = p["id"]
        self.holder.set("ports", ports.collection(items, doc.get("built_at"), aliases))
        if doc.get("source") == "wpi":
            self.m.status(ports.SOURCE_WPI, ok=True, count=sum(1 for p in items if p["id"].startswith("wpi:")))
        else:
            self.m.status(ports.SOURCE_WPI, ok=False, error=doc.get("wpi_error") or "msi.nga.mil did not answer; "
                          "using UN/LOCODE port locations instead")
        self.m.status(ports.SOURCE_LOCODE, ok=True, count=sum(1 for p in items if p["id"].startswith("locode:")))
        if doc.get("ne_matched") is not None:
            self.m.status(ports.SOURCE_NE, ok=doc["ne_matched"] > 0, count=doc["ne_matched"],
                          note="Natural Earth ports matched for size and position")

    def load_datacenters(self) -> None:
        doc = self._doc("datacenters-details.json")
        items = doc["items"]
        self.holder.set("datacenters", datacenters.collection(items, doc.get("built_at")))
        n_pdb = sum(1 for f in items if f["source"] == "PeeringDB")
        self.m.status(datacenters.SOURCE_PDB, ok=True, count=n_pdb)
        if doc.get("osm_error"):
            self.m.status(datacenters.SOURCE_OSM, ok=False, error=doc["osm_error"])
        else:
            self.m.status(datacenters.SOURCE_OSM, ok=True, count=len(items) - n_pdb,
                          note=f"{doc.get('osm_total', 0):,} mapped, {len(items) - n_pdb:,} not already in PeeringDB")

    def load_dams(self) -> None:
        doc = self._doc("dams-details.json")
        items = doc["items"]
        self.holder.set("dams", dams.collection(items, doc.get("built_at")))
        self.m.status(dams.SOURCE_ID, ok=True, count=len(items))

    def load_cables(self) -> None:
        index = self._doc("cables-index.json")
        geo_doc = self._doc("cables.geojson")
        mids = cables.cable_midpoints(geo_doc)
        self.holder.set("cable_index", index)
        self.holder.set("cables", cables.cable_collection(index, mids, index.get("built_at")))
        self.holder.set("landings", cables.landing_collection(index, index.get("built_at")))
        low = sum(1 for k, v in index["countries"].items() if len(k) == 2 and 0 < len(v["cables"]) <= 2)
        self.m.status(cables.SOURCE_ID, ok=True, count=len(index["cables"]),
                      note=f"{len(index['cables']):,} cables, {len(index['landings']):,} landing points, "
                           f"{low} countries with 2 or fewer cables")

    def load_companies(self) -> None:
        doc = self._doc("companies-details.json")
        items = doc["items"]
        self.holder.set("companies", companies.collection(items, doc.get("built_at"), doc.get("exchanges") or {}))
        self.m.status(companies.SOURCE_ID, ok=True, count=len(items))

    # -- details ---------------------------------------------------------------------------------
    def _lookup(self, name: str, rid: str) -> tuple[Collection, int] | None:
        c = self.coll(name)
        i = c.index.get(rid)
        if i is None:
            alias = (c.meta.get("aliases") or {}).get(rid)
            if alias:
                i = c.index.get(alias)
        if i is None:
            return None
        return c, i

    def _with_entity(self, name: str, rid: str) -> dict | None:
        hit = self._lookup(name, rid)
        if not hit:
            return None
        c, i = hit
        rec = c.detail(c.ids[i]) or {}
        rec["entity"] = c.entity(i)
        rec["built_at"] = c.built_at
        return rec

    def power_detail(self, pid: str) -> dict | None:
        return self._with_entity("power", pid)

    def airport_by_code(self, code: str) -> str | None:
        c = self.coll("airports")
        if code in c.index:
            return code
        key = norm(code).replace(" ", "")
        hits = c.codes.get(key) or []
        if not hits:
            return None
        best = max(hits, key=lambda i: c.weight[i])
        return c.ids[best]

    def airport_detail(self, ident: str) -> dict | None:
        rid = self.airport_by_code(ident)
        return self._with_entity("airports", rid) if rid else None

    def port_detail(self, pid: str) -> dict | None:
        return self._with_entity("ports", pid)

    def datacenter_detail(self, fid: str) -> dict | None:
        rec = self._with_entity("datacenters", fid)
        if rec and rec.get("campus_id"):
            c = self.coll("datacenters")
            same = []
            for d, i in c.near_indices(rec["lat"], rec["lon"], 15.0, 60):
                other = c.detail(c.ids[i]) or {}
                if other.get("campus_id") == rec["campus_id"] and c.ids[i] != fid:
                    same.append(c.entity(i, distance_km=round(d, 2)))
            rec["campus"] = same[:12]
        return rec

    def dam_detail(self, did: str) -> dict | None:
        return self._with_entity("dams", did)

    def company_detail(self, cid: str) -> dict | None:
        rec = self._with_entity("companies", cid)
        if not rec:
            return None
        exchanges = self.coll("companies").meta.get("exchanges") or {}
        for lst in rec.get("listings") or []:
            ex = exchanges.get(lst.get("exchange_qid") or "")
            if ex and ex.get("lat") is not None:
                lst["exchange_lat"], lst["exchange_lon"] = ex["lat"], ex["lon"]
            lst["quote_symbol"] = companies.quote_symbol(lst)
        return rec

    def cable_detail(self, cid: str, *, fetch: bool = True) -> dict | None:
        cid = cid[3:] if cid.startswith("tg:") else cid
        index = self.holder.get("cable_index") or {}
        c = (index.get("cables") or {}).get(cid)
        if not c:
            return None
        lands = index.get("landings") or {}
        ctry = index.get("countries") or {}
        out = {"id": f"tg:{cid}", "tg_id": cid, "name": c["name"], "color": c.get("color"),
               "rfs_year": c.get("rfs_year"), "is_planned": c.get("is_planned"),
               "page": f"https://www.submarinecablemap.com/submarine-cable/{cid}"}
        lps = []
        for lid in c.get("landing_points") or []:
            lp = lands.get(lid)
            if lp:
                lps.append({"id": f"tg:{lid}", "name": lp["name"], "country": lp.get("cc"),
                            "lat": r5(lp["lat"]), "lon": r5(lp["lon"])})
        lps.sort(key=lambda x: (x["country"] or "", x["name"]))
        out["landing_points"] = lps
        cc_list = []
        for cc in c.get("countries") or []:
            row = ctry.get(cc)
            if not row or len(cc) != 2:
                continue
            n = len(row.get("cables") or [])
            cc_list.append({"country": cc, "name": row.get("name"), "cables": n, "low": 0 < n <= 2})
        cc_list.sort(key=lambda x: x["cables"])
        out["countries"] = cc_list
        if lps:
            lats = [p["lat"] for p in lps]
            lons = [p["lon"] for p in lps]
            if max(lons) - min(lons) <= 180:
                out["bbox"] = [min(lons), min(lats), max(lons), max(lats)]
        ent = self.coll("cables").entity_for(f"tg:{cid}")
        out["entity"] = ent
        out["details"] = None
        out["details_error"] = None
        if fetch:
            try:
                out["details"] = cables.fetch_details(self.m.net, cid, self.cache_dir / "cables")
                self.m.status(cables.SOURCE_ID, ok=True)
            except Exception as err:
                out["details_error"] = "TeleGeography's cable page is not answering right now."
                self.m.log.info("cable details for %s failed: %s", cid, err)
        return out

    def landing_detail(self, lid: str) -> dict | None:
        lid = lid[3:] if lid.startswith("tg:") else lid
        index = self.holder.get("cable_index") or {}
        lp = (index.get("landings") or {}).get(lid)
        if not lp:
            return None
        cab = index.get("cables") or {}
        cabs = []
        for cid in lp.get("cables") or []:
            c = cab.get(cid)
            if c:
                cabs.append({"id": f"tg:{cid}", "name": c["name"], "rfs_year": c.get("rfs_year"),
                             "is_planned": c.get("is_planned"), "color": c.get("color")})
        cabs.sort(key=lambda x: (bool(x["is_planned"]), -(x["rfs_year"] or 0)))
        row = (index.get("countries") or {}).get(lp.get("cc") or "") or {}
        n = len(row.get("cables") or [])
        out = {"id": f"tg:{lid}", "name": lp["name"], "place": lp.get("place"), "country": lp.get("cc"),
               "country_name": row.get("name") or countries.name(lp.get("cc")), "lat": r5(lp["lat"]),
               "lon": r5(lp["lon"]), "cables": cabs, "is_tbd": lp.get("is_tbd"),
               "country_cables": n, "country_planned": len(row.get("planned") or []),
               "country_landing_points": len(row.get("landing_points") or []), "low_dependency": 0 < n <= 2,
               "page": f"https://www.submarinecablemap.com/landing-point/{lid}"}
        out["entity"] = self.coll("landings").entity_for(f"tg:{lid}")
        return out

    # -- country summary (CONTRACT 9.1) ----------------------------------------------------------
    def _country_counts(self) -> dict:
        version = self.holder.version
        if self._country_cache[0] == version:
            return self._country_cache[1]
        out = {}
        for name in ("ports", "datacenters", "dams", "companies"):
            out[name] = collections.Counter(c for c in self.coll(name).country if c)
        self._country_cache = (version, out)
        return out

    def country(self, iso2: str) -> dict:
        cc = iso2.upper()
        counts = self._country_counts()
        pw = (self.holder.get("power_stats") or {}).get(cc) or {}
        ap = (self.holder.get("airport_stats") or {}).get(cc) or {}
        index = self.holder.get("cable_index") or {}
        row = (index.get("countries") or {}).get(cc) or {}
        cab = index.get("cables") or {}
        names = sorted(cab[c]["name"] for c in row.get("cables") or [] if c in cab)
        planned = sorted(cab[c]["name"] for c in row.get("planned") or [] if c in cab)
        n = len(names)
        return {
            "country": cc, "name": countries.name(cc),
            "cables": n, "cable_names": names, "planned_cables": planned,
            "landing_points": len(row.get("landing_points") or []),
            "cable_dependency": None if not index else ("none" if n == 0 else "high" if n <= 2 else "moderate" if n <= 4 else "low"),
            "power_plants": pw.get("power_plants", 0), "capacity_mw": pw.get("capacity_mw", {}),
            "capacity_total_mw": round(sum((pw.get("capacity_mw") or {}).values()), 1),
            "airports": ap.get("airports", 0), "airports_by_type": {k: ap.get(k, 0) for k in ("large", "medium", "small")},
            "ports": counts["ports"].get(cc, 0), "datacenters": counts["datacenters"].get(cc, 0),
            "dams": counts["dams"].get(cc, 0), "companies": counts["companies"].get(cc, 0),
            "ready": {k: len(self.coll(k)) > 0 for k in ("power", "airports", "ports", "datacenters", "dams", "cables",
                                                         "companies")},
        }

    def countries_table(self) -> list[dict]:
        index = self.holder.get("cable_index") or {}
        cab = index.get("cables") or {}
        out = []
        for cc, row in (index.get("countries") or {}).items():
            if len(cc) != 2:
                continue
            n = len(row.get("cables") or [])
            out.append({"country": cc, "name": row.get("name") or countries.name(cc), "cables": n,
                        "planned": len(row.get("planned") or []), "landing_points": len(row.get("landing_points") or []),
                        "cable_names": sorted(cab[c]["name"] for c in row.get("cables") or [] if c in cab)[:40],
                        "low": 0 < n <= 2})
        out.sort(key=lambda r: (r["cables"], r["name"] or ""))
        return out

    # -- search and nearby -----------------------------------------------------------------------
    def search(self, q: str, limit: int) -> list[dict]:
        out: list[dict] = []
        for name in SEARCH_ORDER:
            out.extend(self.coll(name).search(q, limit))
        out.sort(key=lambda e: -(e.get("score") or 0))
        return out[:limit]

    def nearby(self, lat: float, lon: float, radius_km: float, limit: int, kinds: set[str] | None = None) -> list[dict]:
        floor = 0.0 if radius_km <= 25 else (0.2 if radius_km <= 100 else 0.35)
        out = []
        for kind, name in NEARBY_KINDS.items():
            if kinds and kind not in kinds:
                continue
            c = self.coll(name)
            got = 0
            for d, i in c.near_indices(lat, lon, radius_km, limit * 6):
                if c.weight[i] < floor and name in ("airports", "power", "ports", "dams"):
                    continue
                out.append(c.entity(i, distance_km=round(d, 2)))
                got += 1
                if got >= limit:
                    break
        return out

    # -- AI tools --------------------------------------------------------------------------------
    def ai_near(self, args: dict) -> dict:
        lat, lon = args.get("lat"), args.get("lon")
        if not geo.valid_latlon(lat, lon):
            return {"error": "lat and lon are required (decimal degrees)."}
        radius = min(500.0, max(1.0, float(args.get("radius_km") or 50)))
        kinds = args.get("kinds") or None
        kind_set = {k for k in kinds if k in NEARBY_KINDS} if isinstance(kinds, list) else None
        items = self.nearby(float(lat), float(lon), radius, 15, kind_set)
        items.sort(key=lambda e: e["distance_km"])
        by_kind = collections.Counter(e["kind"] for e in items)
        return {"lat": lat, "lon": lon, "radius_km": radius, "counts": dict(by_kind),
                "results": [{k: e[k] for k in ("kind", "id", "title", "subtitle", "lat", "lon", "distance_km", "layer")}
                            for e in items[:60]]}

    def ai_airport(self, args: dict) -> dict:
        code = str(args.get("code") or "").strip()[:12]
        rid = self.airport_by_code(code) if code else None
        if not rid:
            hits = self.coll("airports").search(code, 5) if code else []
            if not hits:
                return {"found": False, "message": f"No airport matches {code!r}."}
            rid = hits[0]["id"]
        rec = self.airport_detail(rid) or {}
        out = {"found": True, **{k: rec.get(k) for k in ("id", "name", "type_label", "icao", "iata", "municipality",
                                                         "country_name", "region", "elevation_ft", "scheduled",
                                                         "lat", "lon", "wikipedia", "home")}}
        out["runways"] = [{k: r.get(k) for k in ("name", "length_ft", "width_ft", "surface", "lighted")}
                          for r in rec.get("runways") or []]
        out["frequencies"] = rec.get("frequencies") or []
        out["entity"] = rec.get("entity")
        try:
            met = airports.fetch_metar(self.m.net, rec)
            out["metar"] = met.get("report")
            if met.get("nearest"):
                out["metar_note"] = f"Nearest report, {met['distance_km']} km away"
        except Exception:
            out["metar"] = None
            out["metar_note"] = "aviationweather.gov is not answering"
        if rec.get("icao"):
            out["liveatc"] = f"https://www.liveatc.net/search/?icao={rec['icao']}"
        return out

    def _find_cable(self, query: str) -> str | None:
        q = query.strip()
        if q.startswith("tg:"):
            q = q[3:]
        index = self.holder.get("cable_index") or {}
        if q in (index.get("cables") or {}):
            return q
        hits = self.coll("cables").search(query, 1)
        return hits[0]["id"][3:] if hits else None

    def ai_cable(self, args: dict) -> dict:
        query = str(args.get("query") or args.get("name") or args.get("id") or "").strip()[:120]
        if not query:
            return {"found": False, "message": "Give a cable name or id."}
        cid = self._find_cable(query)
        if not cid:
            return {"found": False, "message": f"No submarine cable matches {query!r}."}
        d = self.cable_detail(cid) or {}
        det = d.get("details") or {}
        return {"found": True, "id": d.get("id"), "name": d.get("name"), "ready_for_service": d.get("rfs_year"),
                "planned": d.get("is_planned"), "length": det.get("length"), "owners": det.get("owners"),
                "suppliers": det.get("suppliers"), "website": det.get("url"),
                "landing_points": [f"{p['name']}" for p in d.get("landing_points") or []],
                "countries": d.get("countries"), "entity": d.get("entity"), "source": "TeleGeography, CC BY-NC-SA 3.0"}

    def ai_company(self, args: dict) -> dict:
        query = str(args.get("query") or args.get("name") or args.get("ticker") or "").strip()[:120]
        if not query:
            return {"found": False, "message": "Give a company name or ticker."}
        hits = self.coll("companies").search(query, 6)
        if not hits:
            return {"found": False, "message": f"No listed company matches {query!r}."}
        rec = self.company_detail(hits[0]["id"]) or {}
        keep = ("id", "name", "hq", "country_name", "industries", "employees", "employees_year", "revenue",
                "market_cap", "website", "isins", "founded", "listings", "wikipedia", "lat", "lon")
        out = {"found": True, **{k: rec.get(k) for k in keep}, "entity": rec.get("entity"),
               "other_matches": [{"id": h["id"], "title": h["title"], "subtitle": h["subtitle"]} for h in hits[1:]]}
        if self.markets is not None:
            sym = next((x["quote_symbol"] for x in rec.get("listings") or [] if x.get("quote_symbol")), None)
            if sym:
                out["quote"] = self.markets.quote(sym)
        return out

    def ai_quote(self, args: dict) -> dict:
        sym = str(args.get("symbol") or "").strip()[:32]
        if not sym:
            return {"available": False, "reason": "Give a symbol."}
        if self.markets is None:
            return {"available": False, "reason": "Markets are not ready."}
        q = self.markets.quote(sym)
        if not q.get("available") and not re.search(r"[:.]", sym):
            hits = self.coll("companies").search(sym, 1)
            if hits:
                rec = self.company_detail(hits[0]["id"]) or {}
                alt = next((x["quote_symbol"] for x in rec.get("listings") or [] if x.get("quote_symbol")), None)
                if alt and alt != sym:
                    q2 = self.markets.quote(alt)
                    q2["company"] = rec.get("name")
                    return q2
        return q


def safe_id(value: str) -> str | None:
    v = (value or "").strip()
    return v if ID_RE.fullmatch(v) else None
