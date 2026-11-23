"""Alaska DOT&PF road weather (RWIS) cameras. Image links carry a timestamp and change every few
minutes, so the latest one is looked up when the image is requested (see ``resolve``)."""

from __future__ import annotations

import re
import urllib.parse

from ..common import FetchContext, Source, arcgis_features, arcgis_point, clean, make_camera, to_int

LAYER = "https://services.arcgis.com/r4A0V7UzH9fcLVvv/arcgis/rest/services/RWIS_Cameras_Latest_Only/FeatureServer/0"
VIEWS = 8
_NOTE = re.compile(r"\s*\((?:#|CCS ?#)\s*[\w ]+\)\s*$")


def _image_field(view: int) -> str:
    return f"cameras_{view}_latestImage"


def parse(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for f in features:
        a = f.get("attributes") or {}
        site = to_int(a.get("properties_siteId"))
        if site is None:
            continue
        lat, lon = arcgis_point(f)
        if lat is None:
            lat, lon = a.get("geometry_coordinates_1"), a.get("geometry_coordinates_0")
        desc = _NOTE.sub("", clean(a.get("properties_description")))
        views = [v for v in range(VIEWS) if a.get(_image_field(v))]
        if not views:
            skipped += 1
            continue
        for v in views:
            name = f"{desc}, view {v + 1}" if len(views) > 1 else desc
            row = make_camera(src, f"{site}-{v}", name, lat, lon, dynamic=True,
                              description=f"Road weather site {clean(a.get('properties_siteAbbr'))}".strip())
            if row:
                rows.append(row)
            else:
                skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    feats = arcgis_features(fc, LAYER, page_size=1000, order_by="objectid", prefix="sites")
    rows, skipped = parse(feats, fc.src)
    fc.skip(skipped)
    return rows


def resolve(cam: dict, net) -> str | None:
    """The current image URL for one view of one site (one small query, cached 90 s)."""
    native = str(cam.get("id", "")).split(":", 1)[-1]
    site, _, view = native.partition("-")
    if not site.isdigit() or not view.isdigit() or int(view) >= VIEWS:
        return None
    params = {"where": f"properties_siteId={int(site)}", "outFields": _image_field(int(view)), "f": "json",
              "returnGeometry": "false"}
    data = net.get_json(f"{LAYER}/query?{urllib.parse.urlencode(params)}", ttl=90, timeout=10, retries=0)
    feats = data.get("features") if isinstance(data, dict) else None
    if not feats:
        return None
    return (feats[0].get("attributes") or {}).get(_image_field(int(view)))


SOURCE = Source(
    id="alaska", name="Alaska road weather cameras", owner="Alaska Department of Transportation and Public Facilities",
    url="https://511.alaska.gov/", licence="Alaska DOT&PF public data (no licence stated; credit Alaska DOT&PF)",
    licence_url="https://dot.alaska.gov/", attribution="Alaska DOT&PF", fetch=fetch, resolve=resolve,
    hosts=("dotdeswus2prodrwissto01.blob.core.windows.net/images/",), country="US", region="Alaska",
    refresh=600, tags=("weather", "mountain", "traffic"), coverage="Alaska highways and passes",
)
