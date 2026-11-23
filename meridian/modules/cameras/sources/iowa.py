"""Iowa DOT traffic, road weather and rest-area cameras, with stills and HLS video."""

from __future__ import annotations

import re
import urllib.parse

from ..common import FetchContext, Source, arcgis_features, arcgis_point, make_camera

LAYER = "https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services/Traffic_Cameras_View/FeatureServer/0"
_PREFIX = re.compile(r"^[A-Z]{2,4}\s*-\s*")
_VIEW = re.compile(r"^R\d+:\s*")


def _stem(url: str) -> str:
    return urllib.parse.urlsplit(url or "").path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def parse(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for f in features:
        a = f.get("attributes") or {}
        lat, lon = arcgis_point(f)
        if lat is None:
            lat, lon = a.get("latitude"), a.get("longitude")
        kind = (a.get("Type") or "").strip()
        tags = ["weather"] if kind == "RWIS" else []
        if kind == "RWIS":
            # road weather sites have several views: "RWIS - US 30 MM 311 @ De Witt" + "R19: US 61 North"
            site = _PREFIX.sub("", (a.get("Desc_") or "").strip())
            view = _VIEW.sub("", (a.get("ImageName") or "").strip())
            name = f"{site}, {view}" if site and view else (site or view)
        else:
            name = _PREFIX.sub("", (a.get("ImageName") or a.get("Desc_") or "").strip())
        native = _stem(a.get("ImageURL") or "") or a.get("COMMON_ID") or a.get("device_id")
        row = make_camera(src, native, name, lat, lon,
                          image=a.get("ImageURL"), stream=a.get("VideoURL") or None, stream_kind="hls",
                          road=a.get("Route"), area=a.get("REGION"), tags=tags,
                          description=f"{kind} camera" if kind and kind != "Iowa DOT" else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    feats = arcgis_features(fc, LAYER, out_fields="device_id,COMMON_ID,Desc_,ImageName,ImageURL,VideoURL,Route,REGION,Type,latitude,longitude",
                            page_size=2000, order_by="FID", prefix="cams")
    rows, skipped = parse(feats, fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="iowa", name="Iowa DOT cameras", owner="Iowa Department of Transportation",
    url="https://data.iowadot.gov/", licence="CC BY 4.0 (Iowa DOT GIS data terms)",
    licence_url="https://creativecommons.org/licenses/by/4.0/", attribution="Iowa DOT, CC BY 4.0",
    fetch=fetch, hosts=("atmsqf.iowadot.gov",), country="US", region="Iowa", refresh=120, tags=("traffic",),
    stream_cors=True, coverage="Iowa interstates, highways, rest areas and road weather sites",
)
