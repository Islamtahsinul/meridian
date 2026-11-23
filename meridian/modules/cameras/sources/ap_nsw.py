"""Transport for NSW Live Traffic cameras: about 250 traffic cameras in Sydney and regional New South
Wales, from the keyless GeoJSON file named in the Live Traffic NSW developer guide (the same records
the Open Data Hub's keyed ``/v1/live/cameras`` API returns).

From outside Australia the image host may answer every request with a "temporarily unavailable"
HTML page instead of a picture (seen from outside Australia in November 2026); the proxy then shows the camera
as not answering. The list itself loads from anywhere.
"""

from __future__ import annotations

import re
import urllib.parse

from ..common import FetchContext, Source, clean, make_camera
from .ap_common import features

URL = "https://data.livetraffic.com/cameras/traffic-cam.json"
IMAGE_HOST = "webcams.transport.nsw.gov.au"
_SUBURB = re.compile(r"\(([^()]+)\)\s*$")


def area_of(region: str) -> str:
    """'SYD_MET', 'Sydney West' -> 'Sydney'; 'REG_NORTH', 'Regional North' -> 'Northern NSW'."""
    key = re.sub(r"[^a-z]", "", (region or "").lower())
    if key.startswith("syd"):
        return "Sydney"
    for word, label in (("north", "Northern NSW"), ("south", "Southern NSW"), ("west", "Western NSW")):
        if word in key:
            return label
    return ""


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for props, lat, lon in features(data):
        image = clean(props.get("href"), 600)
        # project photos (for example while a camera is switched off for roadworks) are not cameras
        if (urllib.parse.urlsplit(image).hostname or "").lower() != IMAGE_HOST:
            skipped += 1
            continue
        title = clean(props.get("title"))
        suburb = _SUBURB.search(title)
        row = make_camera(src, props.get("id"), title, lat, lon, image=image,
                          area=area_of(clean(props.get("region"))) or (suburb.group(1) if suburb else None),
                          region="New South Wales", direction=props.get("direction"),
                          description=props.get("view"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(fc.json(URL, "traffic-cam.json", min_bytes=1000, timeout=60), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="aunsw", name="Live Traffic NSW cameras", owner="Transport for NSW",
    url="https://opendata.transport.nsw.gov.au/dataset/live-traffic-cameras",
    licence="CC BY 4.0 (Transport for NSW Open Data)", licence_url="https://creativecommons.org/licenses/by/4.0/",
    attribution="Transport for NSW, CC BY 4.0", fetch=fetch, hosts=(IMAGE_HOST,), country="AU",
    region="New South Wales", refresh=60, tags=("traffic",), optional=True,
    coverage="Sydney and regional New South Wales (images may not load from outside Australia)",
)
