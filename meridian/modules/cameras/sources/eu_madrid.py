"""Madrid (Spain) city traffic cameras: the Ayuntamiento de Madrid's open data "Tráfico. Cámaras de
tráfico" (datos.madrid.es dataset 202088-0), CC BY 4.0. The city traffic centre's KML
(``informo.madrid.es/informo/tmadrid/CCTV.kml``) lists about 360 street and M-30 cameras, each with a
still on ``informo.madrid.es/cameras/`` (renewed every few minutes).

The separate Calle 30 list (dataset 212166-0) is not used: most of its cameras are already in this
KML under fuller names, and the rest are named only by internal codes. The DGT source covers the
state roads, so the two do not overlap.
"""

from __future__ import annotations

import html
import re

from ..common import FetchContext, Source, make_camera, parse_xml, read_bytes, strip_ns, title_case
from .eu_common import xml_text

KML_URL = "https://informo.madrid.es/informo/tmadrid/CCTV.kml"
_IMG = re.compile(r"<img[^>]*\bsrc=['\"]?(https://informo\.madrid\.es/cameras/[A-Za-z0-9_-]{1,40}\.jpe?g)", re.I)
_NUMBER = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
LICENCE = "CC BY 4.0 (Ayuntamiento de Madrid open data)"
LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"


def _data(pm, name: str) -> str:
    for d in pm.iter("Data"):
        if d.get("name") == name:
            return xml_text(d, "value") or xml_text(d, "Value")
    return ""


def parse_city(data: bytes, src: Source) -> tuple[list[dict], int]:
    """Placemarks of the city traffic centre's KML -> rows (the still is in the description's <img>)."""
    root = strip_ns(parse_xml(data))
    rows: list[dict] = []
    skipped = 0
    for pm in root.iter("Placemark"):
        number = _data(pm, "Numero")
        desc = html.unescape(xml_text(pm, "description", 2000))
        m = _IMG.search(desc)
        coords = xml_text(pm, ".//Point/coordinates").split(",")
        if not _NUMBER.match(number) or not m or len(coords) < 2:
            skipped += 1
            continue
        name = title_case(_data(pm, "Nombre")) or f"Camera {number}"
        row = make_camera(src, number, name, coords[1], coords[0], image=m.group(1), area="Madrid",
                          description=f"Madrid traffic camera {number}")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_city(fc: FetchContext) -> list[dict]:
    rows, skipped = parse_city(read_bytes(fc.download(KML_URL, "CCTV.kml", min_bytes=5000, timeout=60)), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="esmadrid", name="Madrid city traffic cameras", owner="Ayuntamiento de Madrid (traffic management centre)",
    url="https://datos.madrid.es/egob/catalogo/202088-0-trafico-camaras", licence=LICENCE, licence_url=LICENCE_URL,
    attribution="Ayuntamiento de Madrid, CC BY 4.0", fetch=fetch_city, hosts=("informo.madrid.es/cameras/",),
    country="ES", region="Comunidad de Madrid", refresh=300, tags=("traffic", "urban"),
    coverage="Madrid city streets",
)
