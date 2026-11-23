"""Copernicus Emergency Management Service (CEMS) rapid mapping activations: parsers and helpers.

The public dashboard of the rapid mapping service answers JSON from two endpoints:
- the list (``public-activations-info``): code, name, countries, category, event and activation
  times, last update, closed flag, GDACS id, centroid (WKT) and AOI and product counts;
- one activation (``public-activations?code=EMSR936``): reason, sub-category, the ``sensitive``
  flag, requester, areas of interest (WKT outlines) and their map products (first estimate,
  delineation, grading), with delivery state, satellite images and download links.

This is the JSON behind the public site, not a documented API, so every field is optional here.
Terms: "users shall have a free, full and open access to Copernicus Service Information"
(reproduction, distribution and communication to the public, citing the source).
Pure functions only; the polling lives in ``mapping.py``.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .common import clean, num, parse_time, simplify_geometry, geometry_bbox, geometry_center, valid_lonlat

LIST_URL = "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations-info/"
DETAIL_URL = "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations/"
PAGE_URL = "https://mapping.emergency.copernicus.eu/activations/{code}"
TERMS_URL = "https://mapping.emergency.copernicus.eu/terms-and-conditions/"
HOST = "rapidmapping.emergency.copernicus.eu"
VIEWER_HOST = "rapidmapping-viewer.s3.eu-west-1.amazonaws.com"

CODE = re.compile(r"EMSR\d{1,5}")
GDACS_ID = re.compile(r"(EQ|TC|FL|VO|DR|WF)(\d{1,10})")

# category -> (key used by the browser for colour and icon, label)
CATEGORIES = {
    "flood": ("flood", "Flood"),
    "wildfire": ("fire", "Wildfire"),
    "forest fire": ("fire", "Wildfire"),
    "earthquake": ("earthquake", "Earthquake"),
    "storm": ("storm", "Storm"),
    "mass movement": ("landslide", "Mass movement"),
    "landslide": ("landslide", "Landslide"),
    "volcanic activity": ("volcano", "Volcanic activity"),
    "volcano": ("volcano", "Volcanic activity"),
    "tsunami": ("tsunami", "Tsunami"),
    "drought": ("drought", "Drought"),
    "industrial accident": ("industrial", "Industrial accident"),
    "humanitarian": ("humanitarian", "Humanitarian crisis"),
}
GDACS_FOR = {"flood": "FL", "fire": "WF", "earthquake": "EQ", "storm": "TC", "volcano": "VO", "drought": "DR"}

PRODUCT_TYPES = {"FEP": "First estimate", "DEL": "Delineation", "GRA": "Grading", "REF": "Reference"}
PRODUCT_ORDER = {"DEL": 0, "FEP": 1, "GRA": 2, "REF": 3}
STATUS = {"F": "Delivered", "W": "In production", "N": "Not feasible", "C": "Cancelled", "P": "Planned"}

# activation-level summary keys -> plain labels
STAT_LABELS = {"max_extent": ("Largest mapped extent", "ha")}
_STAT_KEY = re.compile(r"^\s*(.*?)\s*\[\s*([^\]]*)\s*\]\s*$")


# ---------------------------------------------------------------------------------------------
# WKT (the dashboard sends POINT, POLYGON and MULTIPOLYGON in CRS84 longitude, latitude order)
# ---------------------------------------------------------------------------------------------
_WKT = re.compile(r"^\s*(POINT|MULTIPOINT|LINESTRING|MULTILINESTRING|POLYGON|MULTIPOLYGON)\s*(?:ZM|Z|M)?\s*(\(.*\))\s*$",
                  re.I | re.S)
_TOKEN = re.compile(r"\(|\)|,|[^\s(),]+")


def _nest(text: str) -> list:
    stack: list[list] = [[]]
    coord: list[float] = []
    for tok in _TOKEN.findall(text):
        if tok == "(":
            stack.append([])
        elif tok in (",", ")"):
            if coord:
                stack[-1].append(coord[:2])
                coord = []
            if tok == ")":
                done = stack.pop()
                if not stack:
                    raise ValueError("unbalanced WKT")
                stack[-1].append(done)
        else:
            coord.append(float(tok))
    if len(stack) != 1 or len(stack[0]) != 1:
        raise ValueError("unbalanced WKT")
    return stack[0][0]


def _ring_ok(ring: Any) -> bool:
    return isinstance(ring, list) and len(ring) >= 4 and all(
        isinstance(p, list) and len(p) == 2 and valid_lonlat(p[0], p[1]) for p in ring)


def parse_wkt(text: Any) -> dict | None:
    """A GeoJSON geometry from WKT (points, lines and polygons), or None for anything else."""
    if not isinstance(text, str) or len(text) > 3_000_000:
        return None
    m = _WKT.match(text)
    if not m:
        return None
    kind = m.group(1).upper()
    try:
        c = _nest(m.group(2))
    except (ValueError, IndexError):
        return None
    try:
        if kind == "POINT":
            p = c[0]
            return {"type": "Point", "coordinates": [p[0], p[1]]} if valid_lonlat(p[0], p[1]) else None
        if kind == "POLYGON":
            rings = [r for r in c if _ring_ok(r)]
            return {"type": "Polygon", "coordinates": rings} if rings else None
        if kind == "MULTIPOLYGON":
            polys = [[r for r in poly if _ring_ok(r)] for poly in c]
            polys = [p for p in polys if p]
            if not polys:
                return None
            return {"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1 else \
                {"type": "MultiPolygon", "coordinates": polys}
        if kind == "LINESTRING":
            return {"type": "LineString", "coordinates": c} if len(c) >= 2 else None
        if kind == "MULTIPOINT":
            pts = [p[0] if isinstance(p[0], list) else p for p in c]
            return {"type": "MultiPoint", "coordinates": pts}
        if kind == "MULTILINESTRING":
            return {"type": "MultiLineString", "coordinates": [line for line in c if len(line) >= 2]}
    except (TypeError, IndexError):
        return None
    return None


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------
def valid_code(code: Any) -> str | None:
    s = str(code or "").strip().upper()
    return s if CODE.fullmatch(s) else None


def category(name: Any) -> tuple[str, str]:
    """(key, label) for a CEMS category ("Mass movement" -> ("landslide", "Mass movement"))."""
    text = clean(name, 60)
    key, label = CATEGORIES.get(text.lower(), ("other", text or "Other"))
    return key, label


def gdacs_event_id(gdacs_id: Any) -> str | None:
    """CEMS "FL1104167" -> Meridian's GDACS entity id "gdacs:FL:1104167"."""
    m = GDACS_ID.fullmatch(str(gdacs_id or "").strip().upper())
    return f"gdacs:{m.group(1)}:{int(m.group(2))}" if m else None


def https_link(url: Any) -> str | None:
    """A link for the browser: https only, no credentials, sane length."""
    if not isinstance(url, str) or len(url) > 600:
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return url.strip()


def download_link(url: Any) -> str | None:
    """Product archives live on the rapid mapping host; anything else is dropped."""
    link = https_link(url)
    if not link:
        return None
    return link if urllib.parse.urlsplit(link).hostname == HOST else None


def viewer_json(url: Any, code: str) -> str | None:
    """A product vector layer (GeoJSON) URL, only on the viewer bucket and under this activation."""
    link = https_link(url)
    if not link:
        return None
    parts = urllib.parse.urlsplit(link)
    if parts.hostname != VIEWER_HOST or parts.query or ".." in parts.path:
        return None
    if not parts.path.startswith(f"/{code}/") or not parts.path.endswith(".json"):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_/.-]+", parts.path):
        return None
    return link


def _countries(raw: Any) -> list[str]:
    out = []
    for c in raw or []:
        name = c.get("name") if isinstance(c, dict) else c
        name = clean(name, 60)
        if name and name not in out:
            out.append(name)
    return out[:30]


def _centroid(raw: Any) -> tuple[float, float] | None:
    g = parse_wkt(raw)
    if not g:
        return None
    if g["type"] == "Point":
        return g["coordinates"][0], g["coordinates"][1]
    c = geometry_center(g)
    return (c[0], c[1]) if c else None


def _requester(raw: Any) -> str:
    parts = [clean(p, 160) for p in str(raw or "").split("|")]
    return ", ".join(p for p in parts if p)[:240]


# ---------------------------------------------------------------------------------------------
# list and detail
# ---------------------------------------------------------------------------------------------
def summary(r: dict) -> dict | None:
    """One activation from the list (also the core of a parsed detail record)."""
    if not isinstance(r, dict):
        return None
    code = valid_code(r.get("code"))
    if not code:
        return None
    key, label = category(r.get("category"))
    pos = _centroid(r.get("centroid"))
    gid = str(r.get("gdacsId") or "").strip().upper() or None
    countries = _countries(r.get("countries"))
    return {
        "id": f"cems:{code}", "code": code, "name": clean(r.get("name"), 200) or f"Activation {code}",
        "countries": countries, "category": label, "cat": key,
        "event_time": parse_time(r.get("eventTime")), "activation_time": parse_time(r.get("activationTime")),
        "last_update": parse_time(r.get("lastUpdate")), "last_update_raw": clean(r.get("lastUpdate"), 40),
        "closed": bool(r.get("closed")),
        "gdacs_id": gid if gid and GDACS_ID.fullmatch(gid) else None,
        "lon": round(pos[0], 5) if pos else None, "lat": round(pos[1], 5) if pos else None,
        "n_aois": int(num(r.get("n_aois"), 0) or 0), "n_products": int(num(r.get("n_products"), 0) or 0),
        "page": PAGE_URL.format(code=code),
    }


def parse_list(data: Any) -> list[dict]:
    rows = data.get("results") if isinstance(data, dict) else data
    out = []
    seen = set()
    for r in rows or []:
        s = summary(r)
        if s and s["code"] not in seen:
            seen.add(s["code"])
            out.append(s)
    out.sort(key=lambda a: -(a["activation_time"] or 0))
    return out


def _stats(raw: Any) -> list[dict]:
    """Activation-level summary figures ({"Roads [km]": 70, ...}) as labelled rows."""
    out = []
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        value = num(v)
        if value is None:
            continue
        if k in STAT_LABELS:
            label, unit = STAT_LABELS[k]
        else:
            m = _STAT_KEY.match(str(k))
            label, unit = (m.group(1), m.group(2)) if m else (str(k).replace("_", " "), "")
            unit = "" if unit.lower() in ("no.", "no", "#", "n") else unit
        label = clean(label, 60)
        if label:
            out.append({"label": label[0].upper() + label[1:], "value": value, "unit": clean(unit, 12)})
    return out[:8]


def _product_stats(raw: Any) -> list[dict]:
    """Per-product figures ({"Flooded area": {"None": {"unit", "total", "affected"}}}) -> a few rows."""
    out = []
    if not isinstance(raw, dict):
        return out
    for group, items in raw.items():
        if not isinstance(items, dict):
            continue
        for item, vals in items.items():
            if not isinstance(vals, dict):
                continue
            affected, total = num(vals.get("affected")), num(vals.get("total"))
            if affected is None and total is None:
                continue
            name = clean(group if item in (None, "None", "") else f"{group}: {item}", 80).rstrip("*").strip()
            out.append({"label": name, "affected": affected, "total": total, "unit": clean(vals.get("unit"), 10)})
    out.sort(key=lambda r: -(r["affected"] or 0))
    return out[:10]


def _product(p: dict, code: str) -> dict | None:
    if not isinstance(p, dict):
        return None
    ptype = clean(p.get("type"), 8).upper()
    ver = p.get("version") if isinstance(p.get("version"), dict) else {}
    status_code = clean(ver.get("statusCode"), 4).upper()
    monitoring = bool(p.get("monitoring"))
    mon_n = int(num(p.get("monitoringNumber"), 0) or 0)
    label = PRODUCT_TYPES.get(ptype, ptype.title() or "Product")
    if monitoring and mon_n:
        label = f"{label}, monitoring {mon_n}"
    images = []
    for im in p.get("images") or []:
        if isinstance(im, dict):
            images.append({"sensor": clean(im.get("sensorName"), 40), "kind": clean(im.get("sensorType"), 20),
                           "resolution": clean(im.get("resolutionClass"), 12), "time": parse_time(im.get("acquisitionTime"))})
    observed = None
    for layer in p.get("layers") or []:
        if isinstance(layer, dict) and "observedevent" in str(layer.get("name") or "").lower():
            observed = viewer_json(layer.get("json"), code) or observed
    feasible = p.get("feasible")
    return {
        "id": int(num(p.get("id"), 0) or 0), "type": ptype, "label": label, "monitoring": monitoring,
        "monitoring_number": mon_n, "feasible": feasible is not False,
        "status": STATUS.get(status_code, "Not feasible" if feasible is False else ("Status " + status_code if status_code else "Requested")),
        "delivered": status_code == "F", "maps": int(num(p.get("mapsCount"), 0) or 0),
        "expected": parse_time(p.get("expectedDelivery")), "delivered_at": parse_time(ver.get("deliveryTime")),
        "updated": parse_time(ver.get("lastUpdateTime")), "version": int(num(ver.get("number"), 0) or 0),
        "download": download_link(p.get("downloadPath")), "images": images[:6],
        "stats": _product_stats(p.get("stats")), "observed": observed,
    }


def _aoi(a: dict, code: str) -> dict | None:
    if not isinstance(a, dict):
        return None
    geom = parse_wkt(a.get("extent"))
    light = simplify_geometry(geom, 0.0015, 5) if geom else None
    products = [x for x in (_product(p, code) for p in a.get("products") or []) if x]
    products.sort(key=lambda x: (PRODUCT_ORDER.get(x["type"], 9), x["monitoring_number"]))
    return {"number": int(num(a.get("number"), 0) or 0), "name": clean(a.get("name"), 120) or "Area of interest",
            "geometry": light, "bbox": geometry_bbox(light) if light else None, "products": products}


def parse_detail(data: Any) -> dict | None:
    """One activation with its areas of interest and map products."""
    rec = data
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        rec = data["results"][0] if data["results"] else None
    base = summary(rec) if isinstance(rec, dict) else None
    if not base:
        return None
    code = base["code"]
    sub = clean(rec.get("subCategory"), 80)
    aois = [x for x in (_aoi(a, code) for a in rec.get("aois") or []) if x]
    aois.sort(key=lambda a: a["number"])
    if base["lat"] is None:
        c = _centroid(rec.get("extent"))
        if c:
            base["lon"], base["lat"] = round(c[0], 5), round(c[1], 5)
    charter = clean(rec.get("charterNumber"), 12) or None
    products = [p for a in aois for p in a["products"]]
    base.update({
        "reason": clean(rec.get("reason"), 2500), "subcategory": sub if sub and sub.lower() != base["category"].lower() else None,
        "sensitive": bool(rec.get("sensitive")), "report": https_link(rec.get("reportLink")),
        "requester": _requester(rec.get("activator")), "continent": clean(rec.get("continent"), 40) or None,
        "aois": aois, "stats": _stats(rec.get("stats")),
        "charter_number": charter, "charter_url": https_link(rec.get("charterUrl")) if charter else None,
        "products_zip": download_link(rec.get("productsPath")),
        "n_aois": max(base["n_aois"], len(aois)), "n_products": max(base["n_products"], len(products)),
        "maps": sum(p["maps"] for p in products), "delivered": sum(1 for p in products if p["delivered"]),
        "pending": sum(1 for p in products if not p["delivered"] and p["feasible"]),
    })
    return base


def observed_layers(detail: dict, limit: int = 4) -> list[dict]:
    """For each area of interest, the newest delivered product that maps the observed event
    (flooded or burnt area): [{url, aoi, product, delivered_at}], newest first."""
    out = []
    for aoi in detail.get("aois") or []:
        best = None
        for p in aoi["products"]:
            if not p.get("observed") or not p.get("delivered"):
                continue
            rank = (p.get("delivered_at") or 0, p["monitoring_number"], PRODUCT_ORDER.get(p["type"], 9) == 0)
            if best is None or rank > best[0]:
                best = (rank, p)
        if best:
            p = best[1]
            out.append({"url": p["observed"], "aoi": aoi["name"], "product": p["label"],
                        "delivered_at": p.get("delivered_at")})
    out.sort(key=lambda x: -(x["delivered_at"] or 0))
    return out[:limit]
