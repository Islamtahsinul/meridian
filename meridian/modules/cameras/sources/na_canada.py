"""Canadian keyless camera sources:

- City of Toronto traffic cameras (open data, Open Government Licence - Toronto). Only the live
  picture is used, never the "comparison" reference pictures in the same records.
- City of Vancouver traffic cameras. The open data lists one page per intersection; each page shows
  one picture per direction. Pages are read once a week (cached, about two a second, within the
  build budget); until a page has been read its cameras are left out.
- 511 Alberta cameras as listed by Esri Canada's 511 Alberta layer (built from the 511 Alberta API).
  511 Alberta's terms allow personal and public non-commercial reproduction. The pictures come from
  511 Alberta itself. City of Calgary cameras in that list are left out (the Calgary open data
  source has them). With ``ALBERTA511_KEY`` set, the official 511 Alberta source lists the same
  links first and these rows are dropped as duplicates.
"""

from __future__ import annotations

import os
import urllib.parse

from ..common import (FetchContext, Source, arcgis_features, arcgis_point, clean, decode_text, make_camera,
                      read_bytes, title_case)
from .na_common import html_images

# -- Toronto ----------------------------------------------------------------------------------------
TORONTO_PACKAGE = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action/package_show?id=traffic-cameras"


def toronto_resource(package) -> str | None:
    """The WGS84 GeoJSON resource of the package."""
    res = ((package or {}).get("result") or {}).get("resources") if isinstance(package, dict) else None
    for r in res or []:
        name = clean(r.get("name")).lower()
        if clean(r.get("format")).lower() == "geojson" and "4326" in name and clean(r.get("url")).startswith("https://"):
            return r["url"]
    return None


def parse_toronto(data, src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for f in (data or {}).get("features") or [] if isinstance(data, dict) else []:
        p = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "MultiPoint" and coords:
            coords = coords[0]
        lon, lat = (list(coords) + [None, None])[:2] if isinstance(coords, list) else (None, None)
        main, cross = title_case(clean(p.get("MAINROAD"))), title_case(clean(p.get("CROSSROAD")))
        name = f"{main} at {cross}" if main and cross else (main or cross)
        row = make_camera(src, p.get("REC_ID"), name, lat, lon, image=p.get("IMAGEURL"), road=main, area="Toronto")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_toronto(fc: FetchContext) -> list[dict]:
    url = toronto_resource(fc.json(TORONTO_PACKAGE, "package.json", min_bytes=200, timeout=60))
    if not url:
        raise RuntimeError("the Toronto traffic camera package has no WGS84 GeoJSON resource")
    rows, skipped = parse_toronto(fc.json(url, "cameras.geojson", min_bytes=1000, timeout=90), fc.src)
    fc.skip(skipped)
    return rows


TORONTO = Source(
    id="catoronto", name="Toronto traffic cameras", owner="City of Toronto Transportation Services",
    url="https://open.toronto.ca/dataset/traffic-cameras/", licence="Open Government Licence - Toronto",
    licence_url="https://open.toronto.ca/open-data-licence/",
    attribution="Contains information licensed under the Open Government Licence - Toronto", fetch=fetch_toronto,
    hosts=("opendata.toronto.ca/transportation/tmc/",), country="CA", region="Ontario",
    refresh=120, tags=("traffic", "urban"), coverage="City of Toronto expressways and arterial roads",
)

# -- Vancouver ----------------------------------------------------------------------------------------
VANCOUVER_LIST = ("https://opendata.vancouver.ca/api/explore/v2.1/catalog/datasets/web-cam-url-links/"
                  "exports/json")
VANCOUVER_HOST = "trafficcams.vancouver.ca"
PAGE_MAX_AGE_H = 168.0
PAGE_SPACING_S = 0.5
KEEP_FOR_S = 75.0          # stop reading new pages when this much of the build budget is left


def vancouver_views(record: dict, page_html: str, src: Source) -> list[dict]:
    """The camera rows of one intersection page (one per picture)."""
    page = clean(record.get("url"), 400)
    point = record.get("geo_point_2d") or {}
    rows = []
    images = html_images(page_html, page, path_prefix="/cameraimages/")
    base_name = clean(record.get("name"))
    for i, (url, alt) in enumerate(images):
        stem = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        name = alt or (f"{base_name}, view {i + 1}" if len(images) > 1 else base_name)
        facing = alt.rsplit(" - ", 1)[-1] if " - " in alt else None       # "Main St and 2nd Ave - North"
        row = make_camera(src, stem, name, point.get("lat"), point.get("lon"), image=url, page=page,
                          area=clean(record.get("geo_local_area")) or "Vancouver", direction=facing)
        if row:
            rows.append(row)
    return rows


def _page_file(page: str) -> str:
    stem = urllib.parse.urlsplit(page).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return "pages/" + "".join(c for c in stem if c.isalnum() or c in "-_")[:60] + ".html"


def fetch_vancouver(fc: FetchContext) -> list[dict]:
    records = fc.json(VANCOUVER_LIST, "pages.json", min_bytes=500, timeout=60)
    rows: list[dict] = []
    waiting = failed = 0
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict):
            continue
        page = clean(rec.get("url"), 400)
        parts = urllib.parse.urlsplit(page)
        if parts.scheme != "https" or (parts.hostname or "").lower() != VANCOUVER_HOST:
            fc.skip()
            continue
        name = _page_file(page)
        path = fc.raw_path(name)
        if fc.time_left() > KEEP_FOR_S:
            try:
                path = fc.download(page, name, max_age_hours=PAGE_MAX_AGE_H, min_bytes=200, timeout=20,
                                   min_interval=PAGE_SPACING_S)
            except Exception:  # one page failing keeps the rest
                failed += 1
                continue
        elif not os.path.exists(path):
            waiting += 1
            continue
        got = vancouver_views(rec, decode_text(read_bytes(path, limit=2_000_000)), fc.src)
        if not got:
            fc.skip()
        rows.extend(got)
    if waiting:
        fc.notes.append(f"{waiting} intersection pages still to read (next build)")
    if failed:
        fc.notes.append(f"{failed} intersection pages could not be read")
    return rows


VANCOUVER = Source(
    id="cavancouver", name="Vancouver traffic cameras", owner="City of Vancouver",
    url="https://opendata.vancouver.ca/explore/dataset/web-cam-url-links/", licence="Open Government Licence - Vancouver",
    licence_url="https://opendata.vancouver.ca/pages/licence/",
    attribution="Contains information licensed under the Open Government Licence - Vancouver", fetch=fetch_vancouver,
    hosts=(f"{VANCOUVER_HOST}/cameraimages/",), country="CA", region="British Columbia", refresh=120,
    tags=("traffic", "urban"), coverage="City of Vancouver intersections and bridges",
)

# -- 511 Alberta (Esri Canada listing) -----------------------------------------------------------------
ALBERTA_LAYER = "https://services.arcgis.com/wjcPoefzjpzCgffS/arcgis/rest/services/Alberta511/FeatureServer/0"
ALBERTA_IMAGES = "https://511.alberta.ca/map/Cctv/"
_AREAS = {"banff": "Banff", "parks": "Parks Canada"}


def parse_alberta(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for f in features:
        a = f.get("attributes") or {}
        source = clean(a.get("Source")).lower()
        image = clean(a.get("Url"), 400)
        if source == "calgary" or clean(a.get("Status")).lower() != "enabled" or not image.startswith(ALBERTA_IMAGES):
            skipped += 1
            continue
        direction = clean(a.get("Direction_Of_Travel"))
        lat, lon = arcgis_point(f)
        row = make_camera(src, a.get("Id"), a.get("Location"), lat, lon, image=image, area=_AREAS.get(source),
                          direction=direction if direction.lower() not in ("", "unknown", "both directions") else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_alberta(fc: FetchContext) -> list[dict]:
    feats = arcgis_features(fc, ALBERTA_LAYER, where="Status='Enabled'",
                            out_fields="Id,Location,Url,Status,Source,Direction_Of_Travel", order_by="OBJECTID",
                            prefix="cams")
    rows, skipped = parse_alberta(feats, fc.src)
    fc.skip(skipped)
    return rows


ALBERTA = Source(
    id="caab511esri", name="511 Alberta cameras (Esri Canada listing)",
    owner="Government of Alberta (511 Alberta), listed by Esri Canada",
    url="https://www.arcgis.com/home/item.html?id=092290a24e14457fad68ca404f00b477",
    licence="511 Alberta terms: personal and public non-commercial reproduction allowed",
    licence_url="https://511.alberta.ca/", attribution="511 Alberta, Government of Alberta (listing by Esri Canada)",
    fetch=fetch_alberta, hosts=("511.alberta.ca",), country="CA", region="Alberta", refresh=300,
    tags=("traffic", "weather"), optional=True, coverage="Alberta highways, Edmonton, Banff and the Rockies",
)

SOURCES = [TORONTO, VANCOUVER, ALBERTA]
