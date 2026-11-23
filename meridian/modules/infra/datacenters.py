"""Data centres and colocation facilities: PeeringDB facilities (keyless API, used under the PeeringDB
acceptable use policy) plus optional OpenStreetMap ``telecom=data_center`` features via Overpass (ODbL)."""

from __future__ import annotations

import json
import time
import urllib.parse

from meridian import geo

from . import countries
from .common import (Collection, decode_text, fc, fnum, http_url, inum, log_weight, norm, point_feature, text,
                     valid_point)

PEERINGDB_FAC = "https://www.peeringdb.com/api/fac"
PEERINGDB_PAGE = "https://www.peeringdb.com/"
OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_QUERY = '[out:json][timeout:170];nwr["telecom"="data_center"];out center tags qt;'
MIN_FACILITIES = 1000
SOURCE_PDB = "infra.peeringdb"
SOURCE_OSM = "infra.osm_datacenters"


def parse_peeringdb(json_text: str) -> list[dict]:
    data = json.loads(json_text)
    out = []
    for f in data.get("data") or []:
        if not isinstance(f, dict) or (f.get("status") not in (None, "ok")):
            continue
        lat, lon = fnum(f.get("latitude")), fnum(f.get("longitude"))
        fid = f.get("id")
        if not isinstance(fid, int) or lat is None or lon is None or not valid_point(lat, lon):
            continue
        cc = (f.get("country") or "").strip().upper() or None
        volts = f.get("available_voltage_services")
        out.append({
            "id": f"peeringdb:{fid}", "pdb_id": fid, "name": text(f.get("name"), 120) or f"Facility {fid}",
            "aka": text(f.get("aka"), 120), "operator": text(f.get("org_name"), 120),
            "lat": lat, "lon": lon, "city": text(f.get("city"), 80), "state": text(f.get("state"), 40),
            "country": cc, "country_name": countries.name(cc), "address": text(f.get("address1"), 160),
            "zipcode": text(f.get("zipcode"), 20), "networks": inum(f.get("net_count"), 0),
            "exchanges": inum(f.get("ix_count"), 0), "carriers": inum(f.get("carrier_count"), 0),
            "website": http_url(f.get("website")), "clli": text(f.get("clli"), 12),
            "campus_id": f.get("campus_id") if isinstance(f.get("campus_id"), int) else None,
            "property": text(f.get("property"), 40), "region": text(f.get("region_continent"), 40),
            "voltage": [text(v, 20) for v in volts][:6] if isinstance(volts, list) else [],
            "substations": text(f.get("diverse_serving_substations"), 20),
            "updated": text(f.get("updated"), 30), "source": "PeeringDB",
            "notes": text(f.get("notes"), 400),
        })
    return out


def parse_overpass(json_text: str) -> list[dict]:
    data = json.loads(json_text)
    out = []
    for el in data.get("elements") or []:
        if not isinstance(el, dict):
            continue
        tags = el.get("tags") or {}
        if el.get("type") == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if not valid_point(lat, lon) or el.get("type") not in ("node", "way", "relation"):
            continue
        name = text(tags.get("name") or tags.get("name:en"), 120)
        operator = text(tags.get("operator") or tags.get("owner"), 120)
        out.append({
            "id": f"osm:{el['type']}/{el.get('id')}", "name": name or (f"{operator} data centre" if operator else "Data centre"),
            "unnamed": not name, "operator": operator, "lat": float(lat), "lon": float(lon),
            "city": text(tags.get("addr:city"), 80), "country": (text(tags.get("addr:country"), 2) or "").upper() or None,
            "address": text(" ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x), 160),
            "website": http_url(tags.get("website") or tags.get("contact:website")),
            "networks": 0, "exchanges": 0, "carriers": 0, "source": "OpenStreetMap",
            "osm_url": f"https://www.openstreetmap.org/{el['type']}/{el.get('id')}",
        })
    return out


def merge_osm(pdb: list[dict], osm: list[dict], max_km: float = 0.4) -> list[dict]:
    """OSM facilities that PeeringDB does not already list nearby with a similar name or operator."""
    grid = geo.GridIndex(0.25)
    for i, f in enumerate(pdb):
        grid.add(f["lat"], f["lon"], i)
    extra = []
    for o in osm:
        dup = False
        for d, i in grid.near(o["lat"], o["lon"], max_km):
            p = pdb[i]
            on, pn = norm(o.get("name")), norm(p.get("name"))
            oo, po = norm(o.get("operator")), norm(p.get("operator"))
            if o.get("unnamed") or (on and pn and (on in pn or pn in on)) or (oo and po and (oo in po or po in oo)) or d < 0.08:
                dup = True
                break
        if not dup:
            extra.append(o)
    return extra


def build(ds) -> None:
    pdb = parse_peeringdb(decode_text(ds.download(PEERINGDB_FAC, "peeringdb_fac.json", min_bytes=500_000, timeout=180,
                                      max_age_hours=24).read_bytes()))
    if len(pdb) < MIN_FACILITIES:
        raise RuntimeError(f"PeeringDB looks incomplete ({len(pdb)} facilities)")
    osm_extra: list[dict] = []
    osm_error = None
    osm_total = 0
    try:
        url = OVERPASS + "?" + urllib.parse.urlencode({"data": OVERPASS_QUERY})
        path = ds.download(url, "osm_datacenters.json", min_bytes=1000, timeout=120)
        osm = parse_overpass(decode_text(path.read_bytes()))
        osm_total = len(osm)
        osm_extra = merge_osm(pdb, osm)
        ds.note(f"OpenStreetMap adds {len(osm_extra):,} of {len(osm):,} mapped data centres")
    except Exception as err:
        osm_error = f"Overpass unavailable ({err}); PeeringDB only"
        ds.note(osm_error)
    items = pdb + osm_extra
    items.sort(key=lambda f: f.get("networks") or 0)
    ds.write_json("datacenters.geojson", fc([point_feature(f["lon"], f["lat"], {
        "id": f["id"], "n": f["name"][:60], "k": f.get("networks") or 0,
        "o": 1 if f["source"] == "OpenStreetMap" else None}) for f in items]))
    ds.write_json("datacenters-details.json", {"built_at": time.time(), "items": items, "osm_error": osm_error,
                                               "osm_total": osm_total})
    ds.set_rows(len(items))
    ds.note(f"{len(pdb):,} PeeringDB facilities")


def subtitle(f: dict) -> str:
    place = ", ".join(x for x in (f.get("city"), f.get("country_name") or f.get("country")) if x)
    lead = f.get("operator") or "Data centre"
    return ", ".join(x for x in (lead, place) if x)


def collection(items: list[dict], built_at: float | None) -> Collection:
    rows = []
    for f in items:
        codes = [f["id"]] + ([f["clli"]] if f.get("clli") else [])
        rows.append({"id": f["id"], "title": f["name"], "subtitle": subtitle(f), "lat": f["lat"], "lon": f["lon"],
                     "weight": log_weight((f.get("networks") or 0) + 1, 1, 600), "country": f.get("country"),
                     "codes": codes, "text": " ".join(x for x in (f.get("operator"), f.get("aka"), f.get("city")) if x),
                     "detail": f})
    return Collection("datacenter", "infra.datacenters", rows, cell_deg=0.5, zoom=14, built_at=built_at)
