"""Illinois Gateway (Travel Midwest) traffic cameras, published by IDOT on ArcGIS Online.

The layer mixes snapshots hosted by Travel Midwest with Lake County PASSAGE snapshots (hosted by
the Lake County Division of Transportation). They are two sources here because the Lake County
host is unreachable from some networks: it can fail without touching the rest.
"""

from __future__ import annotations

import urllib.parse

from ..common import FetchContext, Source, arcgis_features, arcgis_point, make_camera

LAYER = "https://services2.arcgis.com/aIrBD8yn1TDTEXoz/arcgis/rest/services/TrafficCamerasTM_Public/FeatureServer/0"
FIELDS = "OBJECTID,ImgPath,CameraLocation,CameraDirection,SnapShot,TooOld,x,y"
LAKE = "lakecountypassage.com"
_DIRS = {"N": "north", "S": "south", "E": "east", "W": "west", "NE": "north-east", "NW": "north-west",
         "SE": "south-east", "SW": "south-west"}
_STATES = {"IL": "Illinois", "IN": "Indiana", "WI": "Wisconsin", "MO": "Missouri", "IA": "Iowa"}


def parse(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for f in features:
        a = f.get("attributes") or {}
        if str(a.get("TooOld", "false")).lower() == "true":
            skipped += 1
            continue
        image = a.get("SnapShot") or ""
        if (LAKE in image) != (src.id == "lakecounty"):
            continue
        page = a.get("ImgPath") or ""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(page).query)
        cam_id = (q.get("id") or [""])[0]
        direction = (a.get("CameraDirection") or q.get("direction", [""])[0] or "").upper()
        native = f"{cam_id}-{direction}" if cam_id and direction and direction != "NONE" else (cam_id or a.get("OBJECTID"))
        lat, lon = arcgis_point(f)
        if lat is None:
            lat, lon = a.get("y"), a.get("x")
        location = a.get("CameraLocation") or ""
        word = _DIRS.get(direction)
        name = f"{location}, facing {word}" if word else location
        state = _STATES.get(cam_id.split("-")[0]) if cam_id else None
        row = make_camera(src, native, name, lat, lon, image=image, page=page, region=state,
                          area="Lake County" if src.id == "lakecounty" else None,
                          direction=direction if word else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def _fetch(fc: FetchContext, where: str) -> list[dict]:
    feats = arcgis_features(fc, LAYER, where=where, out_fields=FIELDS, page_size=1000, order_by="OBJECTID",
                            prefix="cams")
    rows, skipped = parse(feats, fc.src)
    fc.skip(skipped)
    return rows


def fetch(fc: FetchContext) -> list[dict]:
    return _fetch(fc, f"SnapShot NOT LIKE '%{LAKE}%'")


def fetch_lake(fc: FetchContext) -> list[dict]:
    return _fetch(fc, f"SnapShot LIKE '%{LAKE}%'")


SOURCE = Source(
    id="il", name="Illinois Gateway cameras", owner="Illinois Department of Transportation (Travel Midwest)",
    url="https://www.travelmidwest.com/",
    licence="CC BY-SA 2.0", licence_url="https://creativecommons.org/licenses/by-sa/2.0/",
    attribution="IDOT Gateway Traveler Information (travelmidwest.com), CC BY-SA 2.0", fetch=fetch,
    hosts=("cctv.travelmidwest.com", "atmsqf.iowadot.gov"), country="US", region="Illinois", refresh=300,
    tags=("traffic",), coverage="Chicago area and Illinois expressways",
)

LAKE_COUNTY = Source(
    id="lakecounty", name="Lake County PASSAGE cameras", owner="Lake County Division of Transportation (Illinois)",
    url="https://www.lakecountypassage.com/",
    licence="CC BY-SA 2.0 (listed in the IDOT Gateway layer)", licence_url="https://creativecommons.org/licenses/by-sa/2.0/",
    attribution="Lake County PASSAGE, via IDOT Gateway Traveler Information, CC BY-SA 2.0", fetch=fetch_lake,
    hosts=("www.lakecountypassage.com",), country="US", region="Illinois", refresh=300, tags=("traffic",),
    optional=True, coverage="Lake County, Illinois (north of Chicago)",
)
