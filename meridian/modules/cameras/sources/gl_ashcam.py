"""USGS volcano webcams and FAA aviation weather cameras from the USGS AshCam service.

AshCam (``avo-volcview.wr.usgs.gov/ashcam-api``) is the USGS Volcano Hazards Program's public
webcam service: one list with every camera's position, height, bearing and a stable ``current``
picture. It carries the volcano cameras of the Alaska (AVO), Cascades (CVO), California (CalVO),
Hawaiian (HVO) and Yellowstone (YVO) observatories, and the FAA WeatherCams that AVO watches for
ash across Alaska. Two sources share the list: the volcano cameras and the FAA cameras.

Only cameras marked public (or not marked) with a picture from the last week are listed; the rest
(about half the list: retired, seasonal or broken cameras) are skipped.
"""

from __future__ import annotations

import time

from ..common import FetchContext, Source, clean, http_url, make_camera, to_float, to_int

LIST_URL = "https://avo-volcview.wr.usgs.gov/ashcam-api/webcamApi/webcams"
HOSTS = ("avo-volcview.wr.usgs.gov/ashcam-api/images/",)
MAX_AGE_S = 7 * 86400
_OBSERVATORIES = {"CVO": "Cascades Volcano Observatory", "CalVO": "California Volcano Observatory",
                  "HVO": "Hawaiian Volcano Observatory", "YVO": "Yellowstone Volcano Observatory",
                  "AVO": "Alaska Volcano Observatory"}


def is_faa(cam: dict) -> bool:
    return cam.get("faaInd") == "Y" or (cam.get("organization") or "").upper() == "FAA"


def place(lat: float, lon: float) -> tuple[str | None, str | None]:
    """(country, region) for the places AshCam covers."""
    if lat > 50.5 and (lon < -129.0 or lon > 165.0):
        return "US", "Alaska"
    if 18.0 < lat < 23.5 and -161.0 < lon < -154.0:
        return "US", "Hawaii"
    if 13.0 < lat < 21.0 and 144.0 < lon < 147.0:
        return "MP", "Northern Mariana Islands"
    if lat > 45.55 and lon < -116.9:
        return "US", "Washington"
    if 41.99 < lat <= 46.3 and lon < -116.4:
        return "US", "Oregon"
    if 32.5 < lat <= 42.0 and lon < -114.1:
        return "US", "California"
    if 44.1 < lat < 45.1 and -111.2 < lon < -109.8:
        return "US", "Wyoming"
    return "US", None


def parse(data, src: Source, *, faa: bool, now: float | None = None) -> tuple[list[dict], int]:
    now = time.time() if now is None else now
    rows: list[dict] = []
    skipped = 0
    cams = (data or {}).get("webcams") if isinstance(data, dict) else None
    for cam in cams if isinstance(cams, list) else []:
        if not isinstance(cam, dict) or is_faa(cam) != faa:
            continue
        last = to_float(cam.get("lastImageTimestamp")) or 0.0
        image = http_url(cam.get("currentImageUrl"))
        if cam.get("isPublic") == "N" or cam.get("hasImages") == "N" or not image or now - last > MAX_AGE_S:
            skipped += 1
            continue
        lat, lon = to_float(cam.get("latitude")), to_float(cam.get("longitude"))
        if lat is None or lon is None:
            skipped += 1
            continue
        code = clean(cam.get("webcamCode"), 96)
        name = clean(cam.get("webcamName"), 140) or code
        observatory = None
        first = name.split(" ", 1)[0]
        if first in _OBSERVATORIES and " " in name:
            observatory = _OBSERVATORIES[first]
            name = name.split(" ", 1)[1]
        country, region = place(lat, lon)
        volcano = clean(cam.get("vName"), 60) or None
        if faa:
            tags = ["weather", "airport"] if "airport" in name.lower() else ["weather"]
            desc = "FAA aviation weather camera" + (f", watched for ash from {volcano}" if volcano else "")
        else:
            tags = ["volcano", "mountain"]
            desc = (observatory or _OBSERVATORIES["AVO"]) + " webcam"
        page = http_url(cam.get("externalUrl"))
        row = make_camera(src, code, name, lat, lon, image=image, page=page, heading=to_int(cam.get("bearingDeg")),
                          elevation=cam.get("elevationM"), area=volcano if not faa else None, region=region,
                          country=country, tags=tags, description=desc, camera_type="webcam")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def _fetch(faa: bool):
    def fetch(fc: FetchContext) -> list[dict]:
        data = fc.json(LIST_URL, "webcams.json", min_bytes=1000, timeout=90)
        rows, skipped = parse(data, fc.src, faa=faa)
        fc.skip(skipped)
        return rows
    return fetch


VOLCANO = Source(
    id="usgsvolc", name="USGS volcano webcams", owner="U.S. Geological Survey Volcano Hazards Program (AVO, CVO, "
    "CalVO, HVO, YVO)", url="https://avo-volcview.wr.usgs.gov/ashcam-api/webcamApi/",
    licence="Public domain (U.S. Geological Survey data; credit USGS)",
    licence_url="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
    attribution="USGS Volcano Hazards Program", fetch=_fetch(False), hosts=HOSTS, country="US", refresh=300,
    tags=("volcano", "mountain"), camera_type="webcam",
    coverage="Volcanoes of Alaska and the Aleutians, the Cascades, California, Hawaii and Yellowstone",
)

FAA = Source(
    id="faawx", name="FAA aviation weather cameras", owner="Federal Aviation Administration WeatherCams, via USGS AshCam",
    url="https://weathercams.faa.gov/", licence="Public domain (U.S. government work; credit FAA WeatherCams)",
    licence_url="https://www.faa.gov/web_policies", attribution="FAA WeatherCams", fetch=_fetch(True), hosts=HOSTS,
    country="US", region="Alaska", refresh=600, tags=("weather",), camera_type="webcam",
    coverage="Airports, villages and mountain passes across Alaska",
)

SOURCES = [VOLCANO, FAA]
