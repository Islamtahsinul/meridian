"""Submarine cables and landing points from TeleGeography's Submarine Cable Map (CC BY-NC-SA 3.0,
attribution required, non-commercial). Per-cable details (owners, length, suppliers) are fetched on
demand and cached for 30 days."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import countries
from .common import (Collection, decode_text, fc, file_age_hours, point_feature, read_json, text, valid_point,
                     write_json_atomic)

API = "https://www.submarinecablemap.com/api/v3"
HOST = "www.submarinecablemap.com"
PAGE = "https://www.submarinecablemap.com/"
CABLE_GEO = f"{API}/cable/cable-geo.json"
LANDING_GEO = f"{API}/landing-point/landing-point-geo.json"
CABLE_LIST = f"{API}/cable/all.json"
SOURCE_ID = "infra.telegeography"
LICENCE = "CC BY-NC-SA 3.0, TeleGeography"
MIN_CABLES = 300
MIN_LANDINGS = 500
DETAIL_MAX_AGE_H = 24 * 30
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")


def tg_slug(name: str) -> str:
    """TeleGeography's URL slug: "Congo, Dem. Rep." -> "congo-dem-rep-", "Côte d'Ivoire" -> "cte-divoire"."""
    s = str(name or "").lower().replace("'", "").replace("’", "")
    s = s.encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.lstrip("-")


def country_candidates(landing_name: str) -> list[str]:
    """Possible TeleGeography country names for "City, [Region,] Country" (shortest suffix first)."""
    parts = [p.strip() for p in str(landing_name or "").split(",") if p.strip()]
    out = []
    for take in (1, 2, 3):
        if len(parts) > take:
            out.append(", ".join(parts[-take:]))
    return out


def parse_cable_geo(data: dict) -> list[dict]:
    feats = []
    for f in data.get("features") or []:
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        cid = str(p.get("id") or "")
        if not ID_RE.match(cid) or g.get("type") not in ("MultiLineString", "LineString"):
            continue
        lines = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
        clean = []
        for line in lines:
            pts = [[round(float(x), 4), round(float(y), 4)] for x, y, *_ in line
                   if isinstance(x, (int, float)) and isinstance(y, (int, float)) and abs(y) <= 90 and abs(x) <= 540]
            if len(pts) >= 2:
                clean.append(pts)
        if not clean:
            continue
        color = str(p.get("color") or "")
        feats.append({"id": cid, "name": text(p.get("name"), 120) or cid,
                      "color": color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "#9aa5c4", "lines": clean})
    return feats


def parse_landing_geo(data: dict) -> list[dict]:
    out = []
    for f in data.get("features") or []:
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        lid = str(p.get("id") or "")
        coords = g.get("coordinates") or []
        if not ID_RE.match(lid) or g.get("type") != "Point" or len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        if not valid_point(lat, lon):
            continue
        out.append({"id": lid, "name": text(p.get("name"), 120) or lid, "lat": float(lat), "lon": float(lon),
                    "is_tbd": bool(p.get("is_tbd"))})
    return out


def build_index(cables: list[dict], landings: list[dict], country_docs: dict[str, dict],
                year_now: int | None = None) -> dict:
    """Cables, landing points and countries cross-referenced from TeleGeography's country documents."""
    year_now = year_now or time.gmtime().tm_year
    cab: dict[str, dict] = {}
    for c in cables:
        e = cab.setdefault(c["id"], {"name": c["name"], "color": c["color"], "rfs_year": None, "is_planned": None,
                                     "landing_points": set(), "countries": set()})
        e["name"] = e["name"] or c["name"]
    land: dict[str, dict] = {}
    for lp in landings:
        place, cc = countries.split_place(lp["name"])
        land[lp["id"]] = {"name": lp["name"], "place": place or lp["name"], "cc": cc, "country": None,
                          "lat": lp["lat"], "lon": lp["lon"], "cables": set(), "is_tbd": lp["is_tbd"]}
    ctry: dict[str, dict] = {}
    for slug, doc in country_docs.items():
        tg_name = text(doc.get("name"), 80) or slug
        cc = countries.from_name(tg_name)
        key = cc or f"tg:{slug}"
        row = ctry.setdefault(key, {"name": countries.name(cc) or tg_name, "tg_name": tg_name, "slug": slug,
                                    "cables": set(), "planned": set(), "landing_points": set()})
        for lid in doc.get("landing_points_in_country") or []:
            if lid in land:
                land[lid]["country"] = tg_name
                if cc:
                    land[lid]["cc"] = cc
                row["landing_points"].add(lid)
        for c in doc.get("cables") or []:
            cid = str(c.get("id") or "")
            if cid not in cab:
                continue
            e = cab[cid]
            if isinstance(c.get("rfs_year"), int):
                e["rfs_year"] = c["rfs_year"]
            if isinstance(c.get("is_planned"), bool):
                e["is_planned"] = c["is_planned"]
            e["landing_points"].update(x for x in c.get("landing_points") or [] if x in land)
            e["countries"].add(key)
            live = not e["is_planned"] and (e["rfs_year"] is None or e["rfs_year"] <= year_now)
            (row["cables"] if live else row["planned"]).add(cid)
    for cid, e in cab.items():
        for lid in e["landing_points"]:
            land[lid]["cables"].add(cid)
            if land[lid]["cc"]:
                e["countries"].add(land[lid]["cc"])
    # Landing points the country documents did not cover still count for their country (by name).
    for lid, lp in land.items():
        cc = lp["cc"]
        if cc and cc not in ctry:
            ctry[cc] = {"name": countries.name(cc) or cc, "tg_name": None, "slug": None, "cables": set(),
                        "planned": set(), "landing_points": set()}
        if cc:
            ctry[cc]["landing_points"].add(lid)
            for cid in lp["cables"]:
                e = cab[cid]
                live = not e["is_planned"] and (e["rfs_year"] is None or e["rfs_year"] <= year_now)
                (ctry[cc]["cables"] if live else ctry[cc]["planned"]).add(cid)
    for row in ctry.values():
        row["planned"] -= row["cables"]

    def lst(s):
        return sorted(s)

    return {
        "built_at": time.time(), "year": year_now,
        "cables": {k: {**v, "landing_points": lst(v["landing_points"]), "countries": lst(v["countries"])} for k, v in cab.items()},
        "landings": {k: {**v, "cables": lst(v["cables"])} for k, v in land.items()},
        "countries": {k: {**v, "cables": lst(v["cables"]), "planned": lst(v["planned"]),
                          "landing_points": lst(v["landing_points"])} for k, v in ctry.items()},
    }


def _fetch_country(ds, slug: str, cache_dir: Path) -> dict | None:
    """One TeleGeography country document, cached on disk. None for slugs that are not countries."""
    path = cache_dir / f"{slug}.json"
    bad = cache_dir / f"{slug}.notfound"
    max_age = ds.max_age_hours * 0.9
    if not ds.force:
        age = file_age_hours(path)
        if age is not None and age < max_age:
            try:
                return read_json(path)
            except ValueError:
                pass
        bage = file_age_hours(bad)
        if bage is not None and bage < max_age * 4:
            return None
    try:
        resp = ds.net.request(f"{API}/country/{slug}.json", timeout=30, retries=1)
    except Exception:
        if path.exists():
            try:
                return read_json(path)
            except ValueError:
                return None
        raise
    if "json" not in resp.headers.get("content-type", ""):
        bad.write_text("not a country", encoding="utf-8")
        return None
    try:
        doc = json.loads(resp.body.decode("utf-8", errors="replace"))
    except ValueError:
        bad.write_text("not json", encoding="utf-8")
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("cables"), list):
        bad.write_text("unexpected", encoding="utf-8")
        return None
    write_json_atomic(path, doc)
    return doc


def build(ds) -> None:
    cables = parse_cable_geo(json.loads(decode_text(ds.download(CABLE_GEO, "cable-geo.json", min_bytes=100_000, timeout=120)
                                        .read_bytes())))
    landings = parse_landing_geo(json.loads(decode_text(ds.download(LANDING_GEO, "landing-point-geo.json", min_bytes=50_000,
                                                        timeout=120).read_bytes())))
    if len({c["id"] for c in cables}) < MIN_CABLES or len(landings) < MIN_LANDINGS:
        raise RuntimeError(f"Cable map looks incomplete ({len(cables)} features, {len(landings)} landing points)")
    cache_dir = ds.raw("tg_countries/x").parent
    cache_dir.mkdir(parents=True, exist_ok=True)
    docs: dict[str, dict] = {}
    tried: set[str] = set()
    failures = 0
    for lp in landings:
        for cand in country_candidates(lp["name"]):
            slug = tg_slug(cand)
            if not slug or slug in docs:
                break
            if slug in tried:
                continue
            tried.add(slug)
            try:
                doc = _fetch_country(ds, slug, cache_dir)
            except Exception as err:
                failures += 1
                ds.log.warning("cables: country %s failed: %s", slug, err)
                if failures > 25:
                    raise RuntimeError("too many TeleGeography country requests failed") from None
                continue
            if doc is not None:
                docs[slug] = doc
                break
    index = build_index(cables, landings, docs)
    feats = []
    for c in cables:
        e = index["cables"][c["id"]]
        feats.append({"type": "Feature", "geometry": {"type": "MultiLineString", "coordinates": c["lines"]},
                      "properties": {"id": c["id"], "n": c["name"][:80], "c": c["color"],
                                     "p": 1 if e.get("is_planned") else 0, "y": e.get("rfs_year")}})
    ds.write_json("cables.geojson", fc(feats))
    lfeats = []
    for lid, lp in index["landings"].items():
        cty = index["countries"].get(lp["cc"] or "") or {}
        lfeats.append(point_feature(lp["lon"], lp["lat"], {"id": lid, "n": lp["name"][:80], "k": len(lp["cables"]),
                                                           "d": len(cty.get("cables") or []) if cty else None}))
    ds.write_json("landings.geojson", fc(lfeats))
    ds.write_json("cables-index.json", index)
    low = sum(1 for k, v in index["countries"].items() if len(k) == 2 and 0 < len(v["cables"]) <= 2)
    ds.set_rows(len(index["cables"]) + len(index["landings"]))
    ds.note(f"{len(index['cables']):,} cables, {len(index['landings']):,} landing points, "
            f"{len(docs)} country documents; {low} countries with 2 or fewer cables")


# ---------------------------------------------------------------------------------------------
# Collections and details
# ---------------------------------------------------------------------------------------------
def cable_collection(index: dict, geo_lines: dict[str, tuple[float, float]], built_at) -> Collection:
    rows = []
    for cid, c in index["cables"].items():
        pos = geo_lines.get(cid)
        if not pos:
            continue
        status = "planned" if c.get("is_planned") else (f"ready {c['rfs_year']}" if c.get("rfs_year") else "")
        sub = ", ".join(x for x in ("Submarine cable", status, f"{len(c['landing_points'])} landing points") if x)
        rows.append({"id": f"tg:{cid}", "title": c["name"], "subtitle": sub, "lat": pos[0], "lon": pos[1],
                     "weight": min(1.0, len(c["landing_points"]) / 20.0), "codes": [cid, f"tg:{cid}"],
                     "text": " ".join(countries.name(x) or "" for x in c.get("countries") or [] if len(x) == 2),
                     "detail": None})
    return Collection("cable", "infra.cables", rows, cell_deg=2.0, zoom=4, built_at=built_at)


def landing_collection(index: dict, built_at) -> Collection:
    rows = []
    for lid, lp in index["landings"].items():
        n = len(lp["cables"])
        sub = f"Cable landing point, {n} cable{'s' if n != 1 else ''}"
        rows.append({"id": f"tg:{lid}", "title": lp["name"], "subtitle": sub, "lat": lp["lat"], "lon": lp["lon"],
                     "weight": min(1.0, n / 10.0), "country": lp.get("cc"), "codes": [f"tg:{lid}"],
                     "text": lp.get("country") or "", "detail": None})
    return Collection("landing_point", "infra.cables", rows, cell_deg=1.0, zoom=10, built_at=built_at)


def cable_midpoints(geojson: dict) -> dict[str, tuple[float, float]]:
    """A representative (lat, lon) per cable: the middle vertex of its longest line."""
    best: dict[str, tuple[int, tuple[float, float]]] = {}
    for f in geojson.get("features") or []:
        cid = (f.get("properties") or {}).get("id")
        for line in f["geometry"]["coordinates"]:
            if len(line) >= 2 and (cid not in best or len(line) > best[cid][0]):
                x, y = line[len(line) // 2]
                x = ((x + 180.0) % 360.0) - 180.0
                best[cid] = (len(line), (y, x))
    return {k: v[1] for k, v in best.items()}


def fetch_details(net, cid: str, cache_dir: Path) -> dict | None:
    """TeleGeography's page data for one cable (owners, length, suppliers, ready-for-service), cached."""
    if not ID_RE.match(cid):
        return None
    path = cache_dir / f"{cid}.json"
    age = file_age_hours(path)
    if age is not None and age < DETAIL_MAX_AGE_H:
        try:
            return read_json(path)
        except ValueError:
            pass
    try:
        resp = net.request(f"{API}/cable/{cid}.json", timeout=15, retries=1, ttl=3600)
        if "json" not in resp.headers.get("content-type", ""):
            return None
        doc = json.loads(resp.body.decode("utf-8", errors="replace"))
    except Exception:
        if path.exists():
            try:
                return read_json(path)
            except ValueError:
                return None
        raise
    if not isinstance(doc, dict) or doc.get("id") != cid:
        return None
    keep = {k: doc.get(k) for k in ("id", "name", "length", "owners", "suppliers", "rfs", "rfs_year", "is_planned",
                                    "url", "notes")}
    keep["landing_points"] = [{"id": x.get("id"), "name": x.get("name"), "country": x.get("country")}
                              for x in doc.get("landing_points") or [] if isinstance(x, dict)]
    keep["fetched_at"] = time.time()
    write_json_atomic(path, keep)
    return keep
