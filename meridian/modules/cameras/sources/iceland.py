"""Vegagerðin (Icelandic Road and Coastal Administration) road and weather webcams."""

from __future__ import annotations

import urllib.parse

from ..common import FetchContext, Source, clean, compass_label, heading_from, make_camera

URL = "https://gagnaveita.vegagerdin.is/api/vefmyndavelar2014_1"
_WORDS = {"N": "north", "NE": "north-east", "E": "east", "SE": "south-east", "S": "south", "SW": "south-west",
          "W": "west", "NW": "north-west"}


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for cam in data if isinstance(data, list) else []:
        if not isinstance(cam, dict):
            continue
        image = cam.get("Slod") or ""
        stem = urllib.parse.urlsplit(image).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        native = stem or f"{cam.get('Maelist_nr')}"
        place = clean(cam.get("Myndavel"))
        desc = clean(cam.get("Skyring"))
        head = heading_from(desc)
        word = _WORDS.get(compass_label(head) or "")
        name = f"{place}, facing {word}" if place and word else (place or desc)
        road_no = clean(cam.get("NrVegur"))
        road_name = clean(cam.get("Vegheiti"))
        road = f"Road {road_no} ({road_name})" if road_no and road_name else (f"Road {road_no}" if road_no else road_name)
        row = make_camera(src, native, name, cam.get("Breidd"), cam.get("Lengd"), image=image, road=road,
                          heading=head, description=desc)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(fc.json(URL, "vefmyndavelar.json", min_bytes=2000, timeout=90), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="is", name="Vegagerðin webcams", owner="Vegagerðin, Icelandic Road and Coastal Administration",
    url="https://gagnaveita.vegagerdin.is/", licence="Vegagerðin open data service (credit Vegagerðin)",
    licence_url="https://gagnaveita.vegagerdin.is/", attribution="Vegagerðin", fetch=fetch,
    hosts=("www.vegagerdin.is",), country="IS", region="Iceland", refresh=300,
    tags=("weather", "mountain", "traffic"), coverage="Iceland's road network and mountain passes",
)
