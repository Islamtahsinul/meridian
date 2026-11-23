"""MeteoSwiss weather cameras: about 35 panoramic cameras along Switzerland's main flight paths and
mountain passes, from MeteoSwiss Open Data (CC BY 4.0, "Source: MeteoSwiss").

The list is the federal geodata layer ``ch.meteoschweiz.messnetz-webcams`` (GeoJSON in Swiss LV95
coordinates, converted here with swisstopo's approximate formulas, good to about a metre). Each
station's description links its camera on the operator's image service; the "quarter" size of the
latest panorama (about 3,400 x 500 px, under 400 KB) is used, saved every ten minutes. That link
redirects to the dated file of the newest panorama, so it always shows the latest frame.
"""

from __future__ import annotations

import re

from ..common import FetchContext, Source, clean, make_camera, to_float

URL = "https://data.geo.admin.ch/ch.meteoschweiz.messnetz-webcams/ch.meteoschweiz.messnetz-webcams_en.json"
IMAGE_URL = "https://backend.roundshot.com/cams/{cam}/quarter"
_CAM = re.compile(r"https://backend\.roundshot\.com/cams/([0-9a-f]{16,64})/", re.I)
_ALT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*m\b")
_STATION = re.compile(r"^[A-Z0-9]{2,6}$")
MOUNTAIN_M = 1500


def lv95_to_wgs84(east: float, north: float) -> tuple[float, float]:
    """Swiss LV95 (EPSG:2056) easting/northing to WGS84 (lat, lon), swisstopo's approximate formulas."""
    y = (east - 2_600_000) / 1_000_000
    x = (north - 1_200_000) / 1_000_000
    lon = 2.6779094 + 4.728982 * y + 0.791484 * y * x + 0.1306 * y * x * x - 0.0436 * y ** 3
    lat = 16.9023892 + 3.238272 * x - 0.270978 * y * y - 0.002528 * x * x - 0.0447 * y * y * x - 0.0140 * x ** 3
    return lat * 100 / 36, lon * 100 / 36


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    feats = (data or {}).get("features") or [] if isinstance(data, dict) else []
    crs = str((((data or {}).get("crs") or {}).get("properties") or {}).get("name") or "") if isinstance(data, dict) else ""
    for feat in feats:
        if not isinstance(feat, dict):
            continue
        p = feat.get("properties") or {}
        sid = clean(feat.get("id"))
        m = _CAM.search(p.get("description") or "")
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if not m or not _STATION.match(sid) or len(coords) < 2:
            skipped += 1
            continue
        east, north = to_float(coords[0]), to_float(coords[1])
        if east is None or north is None:
            skipped += 1
            continue
        if "2056" in crs or east > 1000:
            lat, lon = lv95_to_wgs84(east, north)
        else:
            lat, lon = north, east
        alt = _ALT.match(clean(p.get("altitude")))
        elevation = float(alt.group(1)) if alt else None
        tags = ("mountain",) if elevation is not None and elevation >= MOUNTAIN_M else ()
        row = make_camera(src, sid, clean(p.get("station_name")) or sid, lat, lon,
                          image=IMAGE_URL.format(cam=m.group(1).lower()), elevation=elevation, tags=tags,
                          description=f"MeteoSwiss weather camera {sid}, panorama")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(fc.json(URL, "messnetz-webcams.json", min_bytes=1000, timeout=60), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="chmeteo", name="MeteoSwiss weather cameras", owner="Federal Office of Meteorology and Climatology MeteoSwiss",
    url="https://www.meteoswiss.admin.ch/services-and-publications/service/open-data.html",
    licence="MeteoSwiss Open Data, CC BY 4.0 (Source: MeteoSwiss)",
    licence_url="https://opendatadocs.meteoswiss.ch/general/terms-of-use", attribution="Source: MeteoSwiss",
    fetch=fetch,
    hosts=("backend.roundshot.com/cams/", "storage.roundshot.com", "storage1.roundshot.com", "storage2.roundshot.com",
           "storage3.roundshot.com"),
    country="CH", refresh=600, tags=("weather",),
    coverage="Switzerland's main flight paths, Alpine passes and the Jura",
)
