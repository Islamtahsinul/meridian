"""Durham County Council (England) traffic and road weather cameras.

Two of the council's own map layers list them: "Traffic Web Cameras" on its ArcGIS server (names,
the view, the area and the image number its camera pages use) and "Road Weather Cams" on its
ArcGIS Online organisation (weather station cameras beside A and B roads). The camera list is
also published as open data (Open Government Licence v3) on Data Mill North.
"""

from __future__ import annotations

import re

from ..common import FetchContext, Source, arcgis_features, arcgis_point, clean, make_camera, title_case, to_int

TRAFFIC_LAYER = "https://spatial.durham.gov.uk/arcgis/rest/services/External/VectorPoint/MapServer/30"
WEATHER_LAYER = "https://services5.arcgis.com/GaeCJQk3ti34eqpL/arcgis/rest/services/Road_Weather_Cams/FeatureServer/0"
TRAFFIC_IMAGE = "https://dcc.ussgroup.co.uk/images/{name}.jpg"
WEATHER_IMAGE = "https://www.durham.gov.uk/weathercameras?action=Camera&camId={station}"
WEATHER_PAGE = "https://www.durham.gov.uk/weathercameras?action=detail&camera={station}"
_FRAME = re.compile(r"/i_([a-z0-9_]{1,40})\.html?$", re.I)
_IMAGE_NAME = re.compile(r"^[a-z0-9_]{1,40}$")
_ROAD = re.compile(r"\b(A\d{1,4}(?:\(M\))?|B\d{3,4}|M\d{1,3})\b")


def _image_name(attrs: dict) -> str | None:
    """'https://dcc.ussgroup.co.uk/i_dutmc_24.html' (or the number 'Dutmc_24') -> 'dutmc_24'."""
    m = _FRAME.search(clean(attrs.get("Cam_Link"), 300))
    name = (m.group(1) if m else clean(attrs.get("USS_Camera_Number"))).lower()
    return name if _IMAGE_NAME.match(name) else None


def parse_traffic(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for f in features:
        a = (f.get("attributes") or {}) if isinstance(f, dict) else {}
        name = _image_name(a)
        if not name or clean(a.get("Status")).lower() not in ("live", ""):
            skipped += 1
            continue
        lat, lon = arcgis_point(f)
        title = title_case(a.get("Camera_Nam"))
        road = _ROAD.search(title)
        view = clean(a.get("Camera_Vie"), 60)
        if view.lower() == title.lower():          # some records repeat the name as the view
            view = ""
        row = make_camera(src, name, title, lat, lon, image=TRAFFIC_IMAGE.format(name=name), page=a.get("Link"),
                          area=a.get("Camera_Are"), road=road.group(1) if road else None,
                          direction=view or None, refresh=60, tags=("urban",))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def parse_weather(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for f in features:
        a = (f.get("attributes") or {}) if isinstance(f, dict) else {}
        station = to_int(a.get("Station_ID"))
        if station is None or station <= 0:
            skipped += 1
            continue
        lat, lon = arcgis_point(f)
        road = clean(a.get("Road_No"))
        place = title_case(a.get("Location"))
        # The weather stills differ only in the query string (camId=...), which the shared build's
        # duplicate check ignores, so these rows carry no image link: ``resolve`` rebuilds it from
        # the camera id (same URL, same allowlist check in the proxy).
        row = make_camera(src, f"wx{station}", " ".join(p for p in (road, place) if p) or f"Weather station {station}",
                          lat, lon, dynamic=True, page=WEATHER_PAGE.format(station=station),
                          area=place, road=road, refresh=600, tags=("weather",),
                          description=f"Road weather station {station}")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows: list[dict] = []
    failed = []
    for label, layer, parser, prefix in (("traffic", TRAFFIC_LAYER, parse_traffic, "traffic"),
                                         ("weather", WEATHER_LAYER, parse_weather, "weather")):
        try:
            got, skipped = parser(arcgis_features(fc, layer, page_size=500, max_pages=4, prefix=prefix,
                                                  order_by="OBJECTID"), fc.src)
        except Exception as err:  # one layer down: keep the other, say so
            failed.append(f"{label} layer failed ({clean(err, 120)})")
            continue
        rows.extend(got)
        fc.skip(skipped)
    if len(failed) == 2:
        raise RuntimeError("; ".join(failed))
    fc.notes.extend(failed)
    return rows


def resolve(cam: dict, net) -> str | None:
    """A road weather station's still (no network); None for traffic cameras, which have their own."""
    if cam.get("image"):
        return None
    native = str(cam.get("id", "")).split(":", 1)[-1]
    return WEATHER_IMAGE.format(station=int(native[2:])) if re.fullmatch(r"wx\d{1,6}", native) else None


SOURCE = Source(
    id="gbdurham", name="Durham traffic and road weather cameras", owner="Durham County Council",
    url="https://www.durham.gov.uk/trafficcameras",
    licence="Open Government Licence v3 (camera list); images © Durham County Council, free display with credit",
    licence_url="https://datamillnorth.org/dataset/traffic-web-cameras-2kq9x",
    attribution="Durham County Council", fetch=fetch, resolve=resolve,
    hosts=("dcc.ussgroup.co.uk/images/", "www.durham.gov.uk/weathercameras"),
    country="GB", region="County Durham", refresh=60, tags=("traffic",), camera_type="traffic",
    coverage="Durham City and Bishop Auckland traffic cameras, County Durham road weather stations",
)
