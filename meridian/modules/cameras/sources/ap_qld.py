"""QLDTraffic (Queensland Department of Transport and Main Roads) web cameras, from the QLDTraffic
GeoJSON API ``/v1/webcams``.

The QLDTraffic API specification (v1.10) publishes one public API key for every developer who does
not register their own; it is used here unless ``QLDTRAFFIC_KEY`` is set. That shared key has a
global quota and is often used up (the API then answers HTTP 429 "exceeded the request quota"):
request your own key by emailing QLDTraffic@tmr.qld.gov.au (see the specification linked from
https://qldtraffic.qld.gov.au/more/Developers-and-Data/index.html).
"""

from __future__ import annotations

import urllib.parse

from meridian.net import NetError

from ..common import FetchContext, Source, clean, make_camera, title_case
from .ap_common import features, first

KEY = "QLDTRAFFIC_KEY"
# the key printed for everyone in the QLDTraffic website API specification v1.10, section 2.3.1
PUBLIC_KEY = "3e83add325cbb69ac4d8e5bf433d770b"
URL = "https://api.qldtraffic.qld.gov.au/v1/webcams"
DOCS = "https://qldtraffic.qld.gov.au/more/Developers-and-Data/index.html"


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for props, lat, lon in features(data):
        status = clean(first(props, "status", "Status")).lower()
        if status and status not in ("active", "online", "ok", "operational"):
            skipped += 1
            continue
        locality = title_case(clean(first(props, "locality", "suburb")))
        name = clean(first(props, "description", "name", "title")) or locality
        row = make_camera(src, first(props, "id", "camera_id"), name, lat, lon,
                          image=first(props, "image_url", "imageUrl", "image"),
                          page=first(props, "url", "web_link"), area=locality,
                          region=clean(first(props, "district")) or None, direction=first(props, "direction"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    key = fc.key(KEY) or PUBLIC_KEY
    url = f"{URL}?{urllib.parse.urlencode({'apikey': key})}"
    try:
        data = fc.json(url, "webcams.json", min_bytes=200, timeout=60)
    except NetError as err:
        if err.status == 429:
            who = "your QLDTRAFFIC_KEY" if fc.key(KEY) else "the shared public key"
            raise RuntimeError(f"QLDTraffic says {who} is over its request quota; request your own key "
                               "from QLDTraffic@tmr.qld.gov.au and set QLDTRAFFIC_KEY") from None
        raise
    rows, skipped = parse(data, fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="auqld", name="QLDTraffic web cameras", owner="Queensland Department of Transport and Main Roads",
    url=DOCS, licence="CC BY 4.0 (QLDTraffic GeoJSON API, Queensland Government open data)",
    licence_url="https://www.data.qld.gov.au/dataset/131940-traffic-and-travel-information-geojson-api",
    attribution="State of Queensland (Department of Transport and Main Roads), CC BY 4.0", fetch=fetch,
    hosts=("*.qldtraffic.qld.gov.au", "*.tmr.qld.gov.au"), country="AU", region="Queensland", refresh=60,
    tags=("traffic",), optional=True,
    coverage="Queensland state roads (shared public key; your own QLDTRAFFIC_KEY is more reliable)",
)
