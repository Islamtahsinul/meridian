"""ALERTCalifornia wildfire and mountain cameras (UC San Diego), from the Cal OES webcam layer."""

from __future__ import annotations

import urllib.parse

from ..common import FetchContext, Source, arcgis_features, arcgis_point, make_camera, title_case

LAYER = "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/CalOES_California_Webcams/FeatureServer/0"
WHERE = "Source = 'Alert California'"


def parse(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for f in features:
        a = f.get("attributes") or {}
        if (a.get("Source") or "") != "Alert California":
            continue
        image = a.get("Thumbnail_Url") or ""
        path = urllib.parse.urlsplit(image).path.strip("/").split("/")
        native = path[1] if len(path) >= 3 and path[0] == "public-camera-data" else (a.get("Camera_ID") or a.get("OBJECTID"))
        lat, lon = arcgis_point(f)
        if lat is None:
            lat, lon = a.get("Latitude"), a.get("Longitude")
        county = title_case((a.get("County") or "").replace("_", " "))
        row = make_camera(src, native, a.get("Location"), lat, lon, image=image, page=a.get("Webcam_URL"),
                          area=f"{county} County" if county else None,
                          elevation=(a.get("Elevation_Ft") or 0) * 0.3048 if isinstance(a.get("Elevation_Ft"), (int, float)) else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    feats = arcgis_features(fc, LAYER, where=WHERE, out_fields="OBJECTID,Location,Source,Webcam_URL,Thumbnail_Url,County,Camera_ID,Elevation_Ft,Latitude,Longitude",
                            page_size=2000, order_by="OBJECTID", prefix="cams")
    rows, skipped = parse(feats, fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="alertca", name="ALERTCalifornia cameras", owner="ALERTCalifornia, UC San Diego (listed by Cal OES)",
    url="https://alertcalifornia.org/", licence="CC BY-NC-ND 4.0 (non-commercial use only)",
    licence_url="https://alertcalifornia.org/terms/", attribution="ALERTCalifornia, UC San Diego, CC BY-NC-ND 4.0",
    fetch=fetch, hosts=("cameras.alertcalifornia.org",), country="US", region="California", refresh=60,
    tags=("mountain", "wildfire", "weather"), coverage="Wildfire lookout cameras across California",
)
