"""Basque Country (Spain), which the DGT feed does not cover: Open Data Euskadi's traffic camera API.

``api.euskadi.eus/traffic/v1.0/cameras`` lists about 490 cameras from five publishers (Basque
Government, Bizkaia, Bilbao, Vitoria-Gasteiz, Donostia), 20 per page. In November 2026 only the
Diputación Foral de Bizkaia's images loaded when tested (the Basque Government's Trafikoa host
times out, Bilbao's links answer 404, Vitoria-Gasteiz's show "no signal" and Donostia's are relative
paths without a host), so only cameras whose image is on an allowed host are kept. Bizkaia gives
positions in UTM zone 30N (ETRS89), the others in degrees.
"""

from __future__ import annotations

import math
import re
import urllib.parse

from ..common import FetchContext, Source, clean, make_camera, to_float

URL = "https://api.euskadi.eus/traffic/v1.0/cameras?_page={page}"
MAX_PAGES = 60
IMAGE_HOSTS = ("www.bizkaimove.com",)        # publishers whose images load (see the module notes)
PUBLISHERS = {"1": "Basque Government", "2": "Bizkaia", "3": "Araba", "4": "Gipuzkoa", "5": "Bilbao",
              "6": "Vitoria-Gasteiz", "7": "Donostia"}
# leading camera codes and words: 'CCTV 300 - ', 'DOMO 96 - ', 'Cámara ', 'DOMO en '
_PREFIX = re.compile(r"^(?:(?:cctv|domo)\s*\d*\s*-?\s*|c[aá]mara\s+|en\s+)+", re.I)
_CODE = re.compile(r"^[A-Z]{3}_DOM_\w+", re.I)          # 'SLO_DOM_001D (18) PK 75+500': no place name
_KM = re.compile(r"^(\d{1,3})\s*[+.,]\s*(\d{1,3})$")


def utm30_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """ETRS89 / UTM zone 30N (EPSG:25830) to latitude and longitude in degrees (accurate to < 1 m)."""
    a, f, k0 = 6378137.0, 1 / 298.257223563, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    x = easting - 500000.0
    m = northing / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
           + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
           + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    sin, cos, tan = math.sin(phi), math.cos(phi), math.tan(phi)
    n1 = a / math.sqrt(1 - e2 * sin ** 2)
    t1 = tan ** 2
    c1 = ep2 * cos ** 2
    r1 = a * (1 - e2) / (1 - e2 * sin ** 2) ** 1.5
    d = x / (n1 * k0)
    lat = phi - (n1 * tan / r1) * (d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
                                   + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = math.radians(-3.0) + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
                                + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / cos
    return math.degrees(lat), math.degrees(lon)


def position(lat_raw, lon_raw) -> tuple[float | None, float | None]:
    """Degrees as given, or UTM 30N metres (the 'latitude' field then holds the northing)."""
    la, lo = to_float(lat_raw), to_float(lon_raw)
    if la is None or lo is None:
        return None, None
    if abs(la) <= 90 and abs(lo) <= 180:
        return la, lo
    if 4_000_000 < la < 5_500_000 and 100_000 < lo < 900_000:
        return utm30_to_wgs84(lo, la)
    return None, None


def _km(value) -> str:
    """'008+500' -> '8.5', '105+700' -> '105.7'."""
    text = clean(value)
    m = _KM.match(text)
    if not m:
        return ""
    return f"{int(m.group(1)) + int(m.group(2)) / 1000:g}"


def _label(name: str) -> str:
    """'CCTV 300 - Cámara DOMO nudo Kukularra' -> 'Nudo Kukularra'; code-only names give ''."""
    text = clean(name)
    if _CODE.match(text):
        return ""
    text = _PREFIX.sub("", text).strip(" -")
    if not text or re.fullmatch(r"\d+", text):
        return ""
    return text[:1].upper() + text[1:]


def _image(url) -> str | None:
    """The image link without its cache buster, if it is on an allowed host."""
    text = clean(url, 500)
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in IMAGE_HOSTS:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse(cams, src: Source) -> tuple[list[dict], int, dict[str, int]]:
    """Rows, skipped count and the skipped count per publisher (for the status note)."""
    rows: list[dict] = []
    skipped = 0
    by_publisher: dict[str, int] = {}
    seen_images: set[str] = set()
    for c in cams if isinstance(cams, list) else []:
        if not isinstance(c, dict):
            continue
        pub = clean(c.get("sourceId"))
        image = _image(c.get("urlImage"))
        if not image or image in seen_images:
            skipped += 1
            by_publisher[pub] = by_publisher.get(pub, 0) + 1
            continue
        lat, lon = position(c.get("latitude"), c.get("longitude"))
        road = re.sub(r"\s*-\s*", "-", clean(c.get("road")))
        km = _km(c.get("kilometer"))
        label = _label(c.get("cameraName"))
        towards = clean(c.get("address")).title()
        name = f"{road} km {km}" if road and km else (road or "Camera")
        if label:
            name = f"{name}, {label}"
        # 'address' is the place the camera looks towards (Donostia, Santander, Kurtzes...)
        same_road = towards.upper() in (road.upper(), road.replace("-", "").upper())
        if towards and not same_road and towards.lower() not in name.lower():
            name = f"{name}, towards {towards}"
        row = make_camera(src, f"{pub}-{clean(c.get('cameraId'))}", name, lat, lon, image=image, road=road,
                          region=PUBLISHERS.get(pub, "Basque Country"))
        if row:
            seen_images.add(image)
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped, by_publisher


def fetch(fc: FetchContext) -> list[dict]:
    cams: list = []
    first = fc.json(URL.format(page=1), "cameras-1.json", min_bytes=50, timeout=60)
    pages = int(first.get("totalPages") or 1) if isinstance(first, dict) else 1
    cams.extend(first.get("cameras") or [] if isinstance(first, dict) else [])
    for page in range(2, min(pages, MAX_PAGES) + 1):
        if fc.time_left() < 20:
            fc.notes.append("stopped early (time budget)")
            break
        data = fc.json(URL.format(page=page), f"cameras-{page}.json", min_bytes=50, timeout=60, min_interval=0.3)
        cams.extend(data.get("cameras") or [] if isinstance(data, dict) else [])
    rows, skipped, by_pub = parse(cams, fc.src)
    fc.skip(skipped)
    left = [f"{PUBLISHERS.get(p, p)} {n}" for p, n in sorted(by_pub.items()) if n]
    if left:
        fc.notes.append("left out (no image that loads here): " + ", ".join(left))
    return rows


SOURCE = Source(
    id="esbizkaia", name="Bizkaia road cameras", owner="Diputación Foral de Bizkaia, listed by Open Data Euskadi",
    url="https://opendata.euskadi.eus/catalogo/-/camaras-de-trafico-de-euskadi/",
    licence="Open Data Euskadi: CC BY 4.0 (credit the source, do not alter the information)",
    licence_url="https://opendata.euskadi.eus/general/-/informacion-legal-opendata/",
    attribution="Open Data Euskadi, Diputación Foral de Bizkaia", fetch=fetch, hosts=("www.bizkaimove.com/camaras/",),
    country="ES", region="Bizkaia", refresh=240, tags=("traffic",),
    coverage="Motorways and main roads around Bilbao (Bizkaia)",
)
