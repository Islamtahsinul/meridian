"""GeoNet volcano cameras (Earth Sciences New Zealand): Ruapehu, Ngauruhoe, Tongariro, Taranaki,
Whakaari / White Island and Raoul Island, with a new still every 10 minutes.

GeoNet's network API lists every camera site with its position but not the name of its picture, so
the eleven public pictures are matched to their sites here (checked against geonet.org.nz in November
2026). Each build reads the site list once, so positions follow the API and a site that has been
closed drops out; without the API the checked positions below are used.
"""

from __future__ import annotations

from ..common import FetchContext, Source, clean, make_camera
from .ap_common import features, toward

STATIONS_URL = "https://api.geonet.org.nz/network/station?sensorType=13&endDate=9999-01-01"
IMAGE = "https://images.geonet.org.nz/volcano/cameras/latest/{cam}.jpg"
PAGE = "https://www.geonet.org.nz/volcano/cameras/{cam}"

RUAPEHU = (-39.2817, 175.5685)
NGAURUHOE = (-39.1569, 175.6321)
TONGARIRO = (-39.1306, 175.6418)
TE_MAARI = (-39.1077, 175.6726)
TARANAKI = (-39.2968, 174.0634)
WHAKAARI = (-37.5208, 177.1814)

# picture name, site code, sensor location, title, what it looks at, fallback position (lat, lon)
CAMERAS = (
    ("ruapehunorth", "DISC", "01", "Ruapehu from the north", RUAPEHU, (-39.16732, 175.48055)),
    ("ngauruhoe", "DISC", "02", "Ngauruhoe from the west", NGAURUHOE, (-39.16732, 175.48055)),
    ("tongariro", "KAKA", "01", "Tongariro from the north", TONGARIRO, (-38.9749, 175.69483)),
    ("ruapehueast", "KMTP", "02", "Ruapehu from the east", RUAPEHU, (-39.29642, 175.7663)),
    ("ngauruhoetongariro", "KMTP", "03", "Ngauruhoe and Tongariro from the south-east", NGAURUHOE,
     (-39.29642, 175.7663)),
    ("ruapehusouth", "MTSR", "01", "Ruapehu from the south", RUAPEHU, (-39.38461, 175.47041)),
    ("tongarirotemaaricrater", "TOKR", "01", "Tongariro, Te Maari craters", TE_MAARI, (-39.09444, 175.6412)),
    ("taranaki", "TEMO", "01", "Taranaki Maunga from New Plymouth", TARANAKI, (-39.06243, 174.07338)),
    ("tekaha", "TKAH", "01", "Whakaari / White Island from Te Kaha", WHAKAARI, (-37.74842, 177.69217)),
    ("whakatane", "WHOH", "01", "Whakaari / White Island from Whakatāne", WHAKAARI, (-37.9569, 177.00076)),
    ("raoulisland", "RIMK", "01", "Rangitāhua / Raoul Island", None, (-29.26733, -177.90724)),
)


def sites_of(data) -> dict[tuple[str, str], tuple[float, float, str]]:
    """{(code, location): (lat, lon, site name)} for the open camera sites in the API answer."""
    out: dict[tuple[str, str], tuple[float, float, str]] = {}
    for props, lat, lon in features(data):
        code = clean(props.get("Code")).upper()
        loc = clean(props.get("Location")) or "01"
        if code and lat is not None and lon is not None and str(props.get("End") or "9999").startswith("9999"):
            out.setdefault((code, loc), (lat, lon, clean(props.get("Name"))))
    return out


def parse(data, src: Source) -> tuple[list[dict], int]:
    sites = sites_of(data) if data else {}
    rows: list[dict] = []
    skipped = 0
    for cam, code, loc, title, target, fallback in CAMERAS:
        site = sites.get((code, loc))
        if sites and site is None:          # the API no longer lists this site as open
            skipped += 1
            continue
        lat, lon, site_name = site if site else (fallback[0], fallback[1], "")
        row = make_camera(src, cam, title, lat, lon, image=IMAGE.format(cam=cam), page=PAGE.format(cam=cam),
                          heading=toward(lat, lon, target), area=site_name or None,
                          description=f"GeoNet camera site {code}")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    try:
        data = fc.json(STATIONS_URL, "camera-sites.geojson", min_bytes=500, timeout=60)
    except Exception as err:  # the pictures still work without the site list
        fc.notes.append(f"site list unavailable ({clean(err, 80)}); using checked positions")
        data = None
    rows, skipped = parse(data, fc.src)
    if skipped:
        fc.notes.append(f"{skipped} camera sites no longer open")
    return rows


SOURCE = Source(
    id="nzgeonet", name="GeoNet volcano cameras", owner="GeoNet (Earth Sciences New Zealand)",
    url="https://www.geonet.org.nz/volcano/cameras",
    licence="CC BY 3.0 NZ (GeoNet content, Earth Sciences New Zealand)",
    licence_url="https://www.geonet.org.nz/policy", attribution="GeoNet / Earth Sciences New Zealand, CC BY 3.0 NZ",
    fetch=fetch, hosts=("images.geonet.org.nz",), country="NZ", refresh=600,
    tags=("volcano", "mountain", "weather"), camera_type="webcam",
    coverage="New Zealand's active volcanoes and Raoul Island",
)
