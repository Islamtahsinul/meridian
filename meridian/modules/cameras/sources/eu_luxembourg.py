"""CITA cameras (Luxembourg's motorway traffic centre, Administration des ponts et chaussées).

The camera list is the KML published as open data (CC0) on data.public.lu ("Cita : Caméras
autoroute"); each camera's still is a public JPEG on www.cita.lu, refreshed about once a minute.
The KML only names cameras by motorway and number ("A6 - Camera 3"), so the place and direction
captions ("Steinfort: direction Belgique") come from CITA's public webcam page for each motorway (six
small pages, cached for a week). Without them a camera is named "A6, camera 3".

The KML icon is a camera symbol drawn pointing east and rotated clockwise by the angle in its file
name (``cctv_185.png``), so the viewing direction is that angle plus 90 degrees.
"""

from __future__ import annotations

import html
import re

from meridian.net import NetError

from ..common import FetchContext, Source, clean, decode_text, make_camera, parse_xml, read_bytes, strip_ns
from .eu_common import xml_text

KML_URL = "https://www.cita.lu/kml/cameras.kml"
PAGE_URL = "https://www.cita.lu/en/webcam/{road}.html"
IMAGE_URL = "https://www.cita.lu/info_trafic/cameras/images/cccam_{id}.jpg"
PAGE_MAX_AGE_H = 7 * 24
_ROAD = re.compile(r"^\s*(A\d{1,2})\b", re.I)
_ICON_ANGLE = re.compile(r"cctv_(\d{1,3})\.png", re.I)
_CAM_ID = re.compile(r"^\d{1,6}$")
_FIGURE = re.compile(r'<figure[^>]*\bid="cam-(\d{1,6})".*?<figcaption>(.*?)</figcaption>', re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
# directions that name a neighbouring country, in English
_COUNTRIES = {"belgique": "Belgium", "allemagne": "Germany", "france": "France"}


def parse_captions(text: str) -> dict[str, tuple[str, str]]:
    """{camera id: (place, towards)} from one CITA webcam page ("Bridel: <span>direction Mamer</span>")."""
    out: dict[str, tuple[str, str]] = {}
    for m in _FIGURE.finditer(text or ""):
        caption = clean(html.unescape(_TAGS.sub(" ", m.group(2))))
        place, _, rest = caption.partition(":")
        towards = re.sub(r"^direction\s+", "", clean(rest), flags=re.I)
        towards = _COUNTRIES.get(towards.lower(), towards)
        if clean(place):
            out.setdefault(m.group(1), (clean(place), clean(towards)))
    return out


def heading_of(icon: str) -> float | None:
    m = _ICON_ANGLE.search(icon or "")
    return float((int(m.group(1)) + 90) % 360) if m else None


def parse(data: bytes, captions: dict[str, tuple[str, str]], src: Source) -> tuple[list[dict], int]:
    root = strip_ns(parse_xml(data))
    rows: list[dict] = []
    skipped = 0
    for pm in root.iter("Placemark"):
        native = (pm.get("id") or "").rpartition("_")[2]
        if not _CAM_ID.match(native):
            skipped += 1
            continue
        title = xml_text(pm, "name")
        m = _ROAD.match(title)
        road = m.group(1).upper() if m else ""
        coords = xml_text(pm, ".//Point/coordinates").split(",")
        if len(coords) < 2:
            skipped += 1
            continue
        place, towards = captions.get(native, ("", ""))
        if place:
            name = f"{road} {place}".strip() + (f", towards {towards}" if towards else "")
        else:
            name = f"{road}, camera {native}" if road else f"Camera {native}"
        icon = ""
        for href in pm.iter("href"):
            icon = href.text or ""
        row = make_camera(src, native, name, coords[1], coords[0], image=IMAGE_URL.format(id=native), road=road,
                          heading=heading_of(icon), description=f"CITA camera {native}")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def _roads(data: bytes) -> list[str]:
    """Motorways named in the KML ('A1', 'A13', ...), for their webcam pages."""
    root = strip_ns(parse_xml(data))
    found = set()
    for pm in root.iter("Placemark"):
        m = _ROAD.match(xml_text(pm, "name"))
        if m:
            found.add(m.group(1).lower())
    return sorted(found)


def fetch(fc: FetchContext) -> list[dict]:
    data = read_bytes(fc.download(KML_URL, "cameras.kml", min_bytes=2000, timeout=60))
    captions: dict[str, tuple[str, str]] = {}
    missing = []
    for road in _roads(data):
        try:
            page = fc.download(PAGE_URL.format(road=road), f"webcam-{road}.html", max_age_hours=PAGE_MAX_AGE_H / 0.9,
                               min_bytes=2000, timeout=30, min_interval=1.0)
            for cid, cap in parse_captions(decode_text(read_bytes(page, limit=5_000_000))).items():
                captions.setdefault(cid, cap)
        except (NetError, OSError, ValueError):
            missing.append(road.upper())
    if missing:
        fc.notes.append("no place names for " + ", ".join(missing))
    rows, skipped = parse(data, captions, fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="lucita", name="CITA motorway cameras", owner="CITA, Administration des ponts et chaussées (Luxembourg)",
    url="https://www.cita.lu/", licence="CC0 1.0 (camera list on data.public.lu); public stills on cita.lu",
    licence_url="https://data.public.lu/fr/datasets/cita-cameras-autoroute/",
    attribution="CITA, Administration des ponts et chaussées", fetch=fetch,
    hosts=("www.cita.lu/info_trafic/cameras/images/",), country="LU", refresh=60, tags=("traffic",),
    coverage="Luxembourg motorways (A1, A3, A4, A6, A7, A13)",
)
