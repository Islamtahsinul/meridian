"""NOAA National Data Buoy Center BuoyCAMs: six-camera panoramas from weather buoys far out at sea
(Atlantic, Gulf of Mexico, Pacific, Great Lakes and around Hawaii), roughly every hour in daylight.

The list (``buoycams.php``) gives each buoy's position and its latest picture; buoys without a
recent picture (``img`` empty) are skipped. The picture has a stable address per buoy
(``buoycam.php?station=<id>``), so it is fetched through the image proxy like any still.
"""

from __future__ import annotations

from ..common import FetchContext, Source, clean, make_camera, sanitize_id

LIST_URL = "https://www.ndbc.noaa.gov/buoycams.php"
IMAGE = "https://www.ndbc.noaa.gov/buoycam.php?station={id}"
PAGE = "https://www.ndbc.noaa.gov/station_page.php?station={id}"


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for buoy in data if isinstance(data, list) else []:
        if not isinstance(buoy, dict):
            continue
        sid = sanitize_id(buoy.get("id"))
        if not sid or not buoy.get("img"):
            skipped += 1
            continue
        name = clean(buoy.get("name"), 140)
        row = make_camera(src, sid, f"Buoy {sid}, {name}" if name else f"Buoy {sid}", buoy.get("lat"), buoy.get("lng"),
                          image=IMAGE.format(id=sid), page=PAGE.format(id=sid),
                          description="Six-camera panorama from a NOAA weather buoy", camera_type="webcam")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(fc.json(LIST_URL, "buoycams.json", min_bytes=500, timeout=60), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="ndbcbuoy", name="NOAA buoy cameras", owner="NOAA National Data Buoy Center",
    url="https://www.ndbc.noaa.gov/buoycams.shtml", licence="Public domain (U.S. government work; credit NOAA NDBC)",
    licence_url="https://www.weather.gov/disclaimer", attribution="NOAA National Data Buoy Center", fetch=fetch,
    hosts=("www.ndbc.noaa.gov/buoycam.php",), refresh=1800, tags=("coastal", "weather"), camera_type="webcam",
    coverage="Weather buoys off the US coasts, in the Gulf of Mexico, the Great Lakes and the Pacific",
)
