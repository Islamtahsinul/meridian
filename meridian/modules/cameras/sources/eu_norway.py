"""Statens vegvesen (Norwegian Public Roads Administration) road cameras, keyless.

The DATEX II 3.1 CCTV publication is also served as open WFS layers ("NPRA publishes Datex II
WFS/WMS pull services that are open available", datex2-spesifications 3.1 wfs-wms README):

- ``datex_3_1:Cctv``: every camera with county, road, height, still-image cadence and the HLS
  stream when there is one (about 3 MB of GeoJSON);
- ``datex_3_1:CctvSimple_v2``: the same cameras with their current status (about 0.7 MB), used to
  leave out cameras whose picture has not updated for 20 minutes ("camera fault").

Stills come from ``kamera.atlas.vegvesen.no/api/images/<id>``; streams from
``kamera.vegvesen.no/public/<id>/manifest.m3u8`` answer with ``Access-Control-Allow-Origin: *``.
Licence: Norwegian Licence for Open Government Data (NLOD 2.0); credit Statens vegvesen.
"""

from __future__ import annotations

import re
import urllib.parse

from ..common import FetchContext, Source, clean, make_camera, to_float

WFS = "https://ogckart-sn1.atlas.vegvesen.no/datex_3_1/ows"
FULL_URL = WFS + "?" + urllib.parse.urlencode({"service": "WFS", "version": "1.0.0", "request": "GetFeature",
                                               "typeName": "datex_3_1:Cctv", "outputFormat": "application/json"})
STATUS_URL = WFS + "?" + urllib.parse.urlencode({"service": "WFS", "version": "1.0.0", "request": "GetFeature",
                                                 "typeName": "datex_3_1:CctvSimple_v2",
                                                 "outputFormat": "application/json"})
FAULT = "videoOrImagesUnavailableDueToCameraFault"
_ROAD = re.compile(r"^([ERFKPS])V?\s*(\d+)$", re.I)
_ROAD_PREFIX = {"E": "E", "R": "Rv. ", "F": "Fv. ", "K": "Kv. ", "P": "Pv. ", "S": "Sv. "}
# Norwegian place-name parts that say what a camera looks at (whole words or word endings)
_TAGS = [
    ("mountain", re.compile(r"\w*(fjell|fjellet|fjellstue|vidda|skaret|heia|heii)\b", re.I)),
    ("bridge", re.compile(r"\bbru\b|\w+brua\b|\w+brui\b|\bbrua\b", re.I)),
    ("tunnel", re.compile(r"\w*tunnel(en)?\b", re.I)),
    ("port", re.compile(r"\w*(ferjekai|ferjeleie|ferjeleiet|havn)\b|\bkai\b", re.I)),
]


def _first_value(block) -> str:
    """'{"values": {"value": [{"value": "Prestebakke", "lang": "nob"}]}}' -> 'Prestebakke'."""
    values = ((block or {}).get("values") or {}).get("value") if isinstance(block, dict) else None
    if isinstance(values, dict):
        values = [values]
    for v in values or []:
        text = clean(v.get("value")) if isinstance(v, dict) else ""
        if text:
            return text
    return ""


def road_label(number: str) -> str:
    """'E6' -> 'E6', 'R7' -> 'Rv. 7', 'F884' -> 'Fv. 884' (Norwegian road categories)."""
    m = _ROAD.match(clean(number))
    if not m:
        return clean(number)
    prefix = _ROAD_PREFIX[m.group(1).upper()]
    return f"{prefix}{int(m.group(2))}"


def place_tags(text: str) -> list[str]:
    return [tag for tag, rx in _TAGS if rx.search(text or "")]


def faulty_ids(status_doc) -> set[str]:
    """Camera ids whose still has not updated for 20 minutes, from the CctvSimple_v2 layer."""
    out: set[str] = set()
    for feat in (status_doc or {}).get("features") or [] if isinstance(status_doc, dict) else []:
        props = feat.get("properties") or {} if isinstance(feat, dict) else {}
        if props.get("STATUS_STILL_IMAGE_AVAILABILITY") == FAULT or props.get("STATUS_STILL_IMAGE_SERVICE_LEVEL") == 0:
            cid = clean(props.get("CAMERA_ID"))
            if cid:
                out.add(cid)
    return out


def parse(doc, src: Source, faulty: set[str] | frozenset = frozenset()) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for feat in (doc or {}).get("features") or [] if isinstance(doc, dict) else []:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        data = props.get("data") or []
        dx = (data[0] or {}).get("datex") if data and isinstance(data[0], dict) else None
        if not isinstance(dx, dict):
            skipped += 1
            continue
        cid = clean(dx.get("cctvCameraIdentification") or props.get("recordId"))
        if not cid or cid in faulty:
            skipped += 1
            continue
        loc = dx.get("cctvCameraLocation") or {}
        point = ((loc.get("pointByCoordinates") or {}).get("pointCoordinates") or {})
        lat, lon = point.get("latitude"), point.get("longitude")
        if lat is None or lon is None:
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            lon, lat = (coords[0], coords[1]) if len(coords) >= 2 else (None, None)
        heights = point.get("heightCoordinate") or []
        height = to_float((heights[0] or {}).get("heightValue")) if heights and isinstance(heights[0], dict) else None
        sup = loc.get("supplementaryPositionalDescription") or {}
        roads = sup.get("roadInformation") or []
        road = road_label((roads[0] or {}).get("roadNumber") or "") if roads and isinstance(roads[0], dict) else ""
        county = _first_value((sup.get("namedArea") or {}).get("areaName"))
        site = _first_value(dx.get("cctvCameraSiteLocalDescription"))
        towards = clean(dx.get("cctvCameraOrientationDescription"))
        name = f"{site}, towards {towards}" if site and towards and towards.lower() != site.lower() else (site or towards)
        still = next((s for s in dx.get("cctvStillImageService") or [] if isinstance(s, dict)), {})
        image = (still.get("stillImageUrl") or {}).get("urlLinkAddress") if still.get("cctvStillImageServiceLevel") != 0 else None
        video = next((v for v in dx.get("cctvVideoService") or [] if isinstance(v, dict)), {})
        stream = (video.get("videoUrl") or {}).get("urlLinkAddress") if video.get("cctvVideoServiceLevel") == 1 else None
        row = make_camera(src, cid, name, lat, lon, image=image, stream=stream or None, stream_kind="hls",
                          region=county, road=road, refresh=still.get("cctvStillImageUpdateFrequency"),
                          elevation=height, tags=place_tags(f"{site} {towards}"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    doc = fc.json(FULL_URL, "cctv.json", min_bytes=10_000, timeout=120)
    faulty: set[str] = set()
    try:
        faulty = faulty_ids(fc.json(STATUS_URL, "cctv-status.json", min_bytes=1000, timeout=90))
    except Exception:          # the status layer only trims faulty cameras; carry on without it
        fc.notes.append("status layer unavailable, faulty cameras kept")
    rows, skipped = parse(doc, fc.src, faulty)
    fc.skip(skipped)
    if faulty:
        fc.notes.append(f"{len(faulty)} with a camera fault left out")
    return rows


SOURCE = Source(
    id="nosvv", name="Statens vegvesen road cameras", owner="Statens vegvesen (Norwegian Public Roads Administration)",
    url="https://dataut.vegvesen.no/en/dataset/webkamera",
    licence="Norwegian Licence for Open Government Data (NLOD 2.0)", licence_url="https://data.norge.no/nlod/en/2.0",
    attribution="Made with data from Statens vegvesen (NLOD 2.0)", fetch=fetch,
    hosts=("kamera.atlas.vegvesen.no/api/images/",), country="NO", refresh=60, tags=("traffic", "weather"),
    stream_cors=True, coverage="Norwegian national and county roads, mountain passes and ferry quays",
)
