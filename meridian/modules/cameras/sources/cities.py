"""City and regional traffic cameras from open data portals: York Region (Ontario), Austin (Texas),
Calgary (Alberta) and Newfoundland and Labrador highways. One small adapter each."""

from __future__ import annotations

import urllib.parse

from ..common import (FetchContext, Source, arcgis_features, arcgis_point, clean, make_camera, socrata_rows,
                      title_case)

# -- York Region ----------------------------------------------------------------------------------
YORK_LAYER = "https://ww8.yorkmaps.ca/arcgis/rest/services/OpenData/Traffic/MapServer/0"


def parse_york(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for f in features:
        a = f.get("attributes") or {}
        lat, lon = arcgis_point(f)
        row = make_camera(src, a.get("FACILITYID") or a.get("ESRI_OID"), a.get("cameralocation"), lat, lon,
                          image=a.get("photo"), page=a.get("url"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_york(fc: FetchContext) -> list[dict]:
    rows, skipped = parse_york(arcgis_features(fc, YORK_LAYER, out_fields="FACILITYID,ESRI_OID,photo,url,cameralocation",
                                               page_size=1000, prefix="cams"), fc.src)
    fc.skip(skipped)
    return rows


YORK = Source(
    id="york", name="York Region traffic cameras", owner="The Regional Municipality of York",
    url="https://insights-york.opendata.arcgis.com/", licence="York Region Open Data Licence",
    licence_url="https://www.york.ca/york-region/plans-reports-and-strategies/open-data",
    attribution="Contains information licensed under the York Region Open Data Licence", fetch=fetch_york,
    hosts=("ww5.yorkmaps.ca",), country="CA", region="Ontario", refresh=120, tags=("traffic", "urban"),
    coverage="York Region, north of Toronto",
)

# -- Austin ---------------------------------------------------------------------------------------
AUSTIN_URL = "https://data.austintexas.gov/resource/b4k4-adkb.json"


def parse_austin(rows_in: list[dict], src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for r in rows_in:
        if (r.get("camera_status") or "") != "TURNED_ON":
            skipped += 1
            continue
        coords = (r.get("location") or {}).get("coordinates") or [None, None]
        name = title_case(clean(r.get("location_name")))
        row = make_camera(src, r.get("camera_id"), name, coords[1] if len(coords) > 1 else None, coords[0],
                          image=r.get("screenshot_address"), area="Austin", road=title_case(clean(r.get("primary_st"))))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_austin(fc: FetchContext) -> list[dict]:
    rows, skipped = parse_austin(socrata_rows(fc, AUSTIN_URL, prefix="cams"), fc.src)
    fc.skip(skipped)
    return rows


AUSTIN = Source(
    id="austin", name="Austin traffic cameras", owner="City of Austin Transportation and Public Works",
    url="https://data.austintexas.gov/d/b4k4-adkb", licence="Public domain (City of Austin open data)",
    licence_url="https://data.austintexas.gov/d/b4k4-adkb", attribution="City of Austin", fetch=fetch_austin,
    hosts=("cctv.austinmobility.io",), country="US", region="Texas", refresh=120, tags=("traffic", "urban"),
    coverage="City of Austin",
)

# -- Calgary --------------------------------------------------------------------------------------
CALGARY_URL = "https://data.calgary.ca/resource/k7p9-kppz.json"


def parse_calgary(rows_in: list[dict], src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for r in rows_in:
        image = (r.get("camera_url") or {}).get("url") if isinstance(r.get("camera_url"), dict) else r.get("camera_url")
        stem = urllib.parse.urlsplit(image or "").path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        coords = (r.get("point") or {}).get("coordinates") or [None, None]
        quadrant = clean(r.get("quadrant"))
        row = make_camera(src, stem, r.get("camera_location"), coords[1] if len(coords) > 1 else None, coords[0],
                          image=image, area=f"Calgary {quadrant}".strip() if quadrant else "Calgary")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_calgary(fc: FetchContext) -> list[dict]:
    rows, skipped = parse_calgary(socrata_rows(fc, CALGARY_URL, prefix="cams"), fc.src)
    fc.skip(skipped)
    return rows


CALGARY = Source(
    id="calgary", name="Calgary traffic cameras", owner="The City of Calgary", url="https://data.calgary.ca/d/k7p9-kppz",
    licence="Open Government Licence, City of Calgary",
    licence_url="https://data.calgary.ca/stories/s/Open-Calgary-Terms-of-Use/u45n-7awa",
    attribution="Contains information licensed under the Open Government Licence, City of Calgary",
    fetch=fetch_calgary, hosts=("trafficcam.calgary.ca",), country="CA", region="Alberta", refresh=120,
    tags=("traffic", "urban"), coverage="City of Calgary",
)

# -- Newfoundland and Labrador ----------------------------------------------------------------------
NL_LAYER = "https://services8.arcgis.com/aCyQID5qQcyrJMm2/arcgis/rest/services/TI_HighwayCamera/FeatureServer/0"


def parse_nl(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for f in features:
        a = f.get("attributes") or {}
        image = a.get("url") or ""
        parts = urllib.parse.urlsplit(image).path.strip("/").split("/")
        native = parts[-2] if len(parts) >= 2 else a.get("objectid")
        lat, lon = arcgis_point(f)
        row = make_camera(src, native, a.get("location"), lat, lon, image=image)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_nl(fc: FetchContext) -> list[dict]:
    rows, skipped = parse_nl(arcgis_features(fc, NL_LAYER, out_fields="objectid,location,url,CamType", page_size=1000,
                                             prefix="cams"), fc.src)
    fc.skip(skipped)
    return rows


NEWFOUNDLAND = Source(
    id="nl", name="Newfoundland and Labrador highway cameras",
    owner="Government of Newfoundland and Labrador, Transportation and Infrastructure",
    url="https://www.gov.nl.ca/ti/highway-cams/", licence="Open Government Licence, Newfoundland and Labrador",
    licence_url="https://opendata.gov.nl.ca/public/opendata/page/?page-id=licence",
    attribution="Contains information licensed under the Open Government Licence, Newfoundland and Labrador",
    fetch=fetch_nl, hosts=("www.gov.nl.ca/ti/highway-cams/",), country="CA", region="Newfoundland and Labrador",
    refresh=300, tags=("traffic", "weather"), coverage="Newfoundland and Labrador highways",
)
