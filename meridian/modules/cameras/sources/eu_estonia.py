"""Transpordiamet (Estonian Transport Administration) road weather cameras on state roads.

The camera list comes from the public ArcGIS map service behind Tark Tee (tarktee.ee), the
Transport Administration's road information map. The same data is published as open data under
CC BY 4.0 ("Location of road cameras" and "Images from road cameras", andmed.eesti.ee); the
documented machine channel is the Tark Tee DATEX II gateway, which needs a reviewed registration.

Each picture is a new file named after its time ("94/94_202611231301.jpg"), so the current file
is looked up when an image is asked for (``resolve``): one small query for every camera at once,
cached for two minutes and shared by all cameras. Pictures are taken about every 20 minutes.
"""

from __future__ import annotations

import re
import time
import urllib.parse

from ..common import FetchContext, Source, arcgis_features, arcgis_point, clean, make_camera, to_float

LAYER = "https://tarktee.ee/tarktee/rest/services/tram/road_cameras/MapServer/0"
IMAGE_BASE = "https://tarktee.ee/images/"
FIELDS = "objectid,site_name,closest_weather_station,distance,image_time,image_path"
STALE_DAYS = 3                     # a camera whose last picture is older than this is left out
_PATH = re.compile(r"^(\d{1,6})/[A-Za-z0-9_-]{1,80}\.jpe?g$")
LOOKUP_URL = f"{LAYER}/query?" + urllib.parse.urlencode({"where": "1=1", "outFields": "image_path",
                                                         "returnGeometry": "false", "f": "json"})


def camera_no(path) -> str | None:
    """'94/94_202611231301.jpg' -> '94' (the camera's folder, which never changes)."""
    m = _PATH.match(clean(path, 200))
    return m.group(1) if m else None


def parse(features: list[dict], src: Source, now: float | None = None) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    now = time.time() if now is None else now
    for f in features or []:
        a = (f or {}).get("attributes") or {} if isinstance(f, dict) else {}
        no = camera_no(a.get("image_path"))
        shot = to_float(a.get("image_time"))
        if no is None or shot is None or now - shot / 1000.0 > STALE_DAYS * 86400:
            skipped += 1
            continue
        lat, lon = arcgis_point(f)
        site = clean(a.get("site_name"))
        station = clean(a.get("closest_weather_station"))
        distance = to_float(a.get("distance"))
        desc = None
        if station and (station != site or (distance or 0) > 0.05):
            desc = f"Nearest road weather station: {station}" + (f" ({distance:.1f} km)" if distance else "")
        row = make_camera(src, no, site or f"Road camera {no}", lat, lon, dynamic=True, description=desc)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    feats = arcgis_features(fc, LAYER, out_fields=FIELDS, order_by="objectid", prefix="cameras", page_size=1000)
    rows, skipped = parse(feats, fc.src)
    fc.skip(skipped)
    return rows


def resolve(cam: dict, net) -> str | None:
    """The current picture of one camera, from one shared query of every camera's latest file."""
    no = str(cam.get("id", "")).split(":", 1)[-1]
    if not no.isdigit():
        return None
    data = net.get_json(LOOKUP_URL, ttl=120, timeout=10, retries=0)
    for f in (data or {}).get("features") or [] if isinstance(data, dict) else []:
        path = clean(((f or {}).get("attributes") or {}).get("image_path"), 200) if isinstance(f, dict) else ""
        if camera_no(path) == no:
            return IMAGE_BASE + path
    return None


SOURCE = Source(
    id="eetransport", name="Transpordiamet road cameras", owner="Transpordiamet (Estonian Transport Administration)",
    url="https://tarktee.ee/", licence="CC BY 4.0 (Transpordiamet open data, andmed.eesti.ee)",
    licence_url="https://andmed.eesti.ee/datasets/teekaamerate-pildid",
    attribution="Transpordiamet, Tark Tee (CC BY 4.0)", fetch=fetch, resolve=resolve,
    hosts=("tarktee.ee/images/",), country="EE", refresh=1200, tags=("weather", "traffic"),
    coverage="Estonian state roads (road weather station cameras)",
)
