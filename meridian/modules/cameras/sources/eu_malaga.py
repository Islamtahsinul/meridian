"""Málaga (Spain) municipal traffic cameras: the Ayuntamiento de Málaga's open data "Cámaras de
tráfico" (datosabiertos.malaga.eu), CC BY-SA 4.0. The KML lists about 145 cameras, each with its
mobility-service page and a still on ``ctraficomovilidad.malaga.eu``."""

from __future__ import annotations

import re

from ..common import FetchContext, Source, clean, make_camera, parse_xml, read_bytes, strip_ns, title_case

URL = "https://datosabiertos.malaga.eu/recursos/transporte/trafico/da_camarasTrafico-4326.kml"
IMAGE_PREFIX = "https://ctraficomovilidad.malaga.eu/recursos/movilidad/camaras_trafico/"
_CODE = re.compile(r"^\s*TV\s*-?\s*\d{1,4}\s*-\s*", re.I)          # "TV87-EL BONI - CAMPANILLAS" -> "EL BONI - ..."
_ID = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


def parse(data: bytes, src: Source) -> tuple[list[dict], int]:
    root = strip_ns(parse_xml(data))
    rows: list[dict] = []
    skipped = 0
    seen: set[str] = set()
    for pm in root.iter("Placemark"):
        f = {sd.get("name"): clean(sd.text, 300) for sd in pm.iter("SimpleData")}
        image = f.get("URLIMAGEN") or ""
        coords = clean(pm.findtext(".//coordinates")).split(",")
        if not _ID.match(f.get("ID") or "") or not image.startswith(IMAGE_PREFIX) or len(coords) < 2:
            skipped += 1
            continue
        if (f.get("TITULARIDAD") or "MUNICIPAL").upper() != "MUNICIPAL" or image in seen:
            skipped += 1                       # only the city's own cameras, each picture once
            continue
        seen.add(image)
        place = title_case(_CODE.sub("", f.get("NOMBRE") or ""))
        street = f.get("DESCRIPCION") or ""
        name = street or place or f"Camera {f['ID']}"
        row = make_camera(src, f["ID"], name, coords[1], coords[0], image=image, page=f.get("URL"), area="Málaga",
                          description=place if place and place.lower() not in name.lower() else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(read_bytes(fc.download(URL, "camaras.kml", min_bytes=5000, timeout=60)), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="esmalaga", name="Málaga traffic cameras", owner="Ayuntamiento de Málaga (Área de Movilidad)",
    url="https://datosabiertos.malaga.eu/dataset/camaras-de-trafico", licence="CC BY-SA 4.0 (Ayuntamiento de Málaga open data)",
    licence_url="https://creativecommons.org/licenses/by-sa/4.0/", attribution="Ayuntamiento de Málaga, CC BY-SA 4.0",
    fetch=fetch, hosts=("ctraficomovilidad.malaga.eu/recursos/movilidad/camaras_trafico/",), country="ES",
    region="Andalusia", refresh=120, tags=("traffic", "urban"), coverage="Málaga city streets",
)
