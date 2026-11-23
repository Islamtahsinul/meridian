"""Métropole de Lyon: the Criter traffic system's web cameras ("Caméras Web Criter"), open data on
data.grandlyon.com under Licence Ouverte v2.0. One still per camera, taken from the video every
minute (the pictures behind Onlymoov); the list comes from the metropolis's WFS as GeoJSON."""

from __future__ import annotations

import re

from ..common import FetchContext, Source, clean, make_camera

URL = ("https://data.grandlyon.com/geoserver/metropole-de-lyon/ows?SERVICE=WFS&VERSION=2.0.0&request=GetFeature"
       "&typename=metropole-de-lyon:pvo_patrimoine_voirie.pvocameracriter&outputFormat=application/json"
       "&SRSNAME=EPSG:4326")
_DIRECTION = re.compile(r"^\s*direction\s*:?\s*", re.I)


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for feat in (data or {}).get("features") or [] if isinstance(data, dict) else []:
        if not isinstance(feat, dict):
            continue
        p = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2 or not p.get("url"):
            skipped += 1
            continue
        place = clean(p.get("libellelong"))
        towards = clean(_DIRECTION.sub("", clean(p.get("observation")))) or clean(p.get("nom"))
        name = f"{place}, towards {towards}" if place and towards else (place or towards)
        native = p.get("identifiant") or p.get("gid") or p.get("numeromaintenance")
        code = clean(p.get("numeromaintenance"))
        row = make_camera(src, native, name, coords[1], coords[0], image=p.get("url"),
                          description=f"Criter camera {code}" if code else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(fc.json(URL, "cameras.geojson", min_bytes=200, timeout=60), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="frlyon", name="Lyon Criter traffic cameras", owner="Métropole de Lyon, Direction de la voirie (Criter)",
    url="https://data.grandlyon.com/", licence="Licence Ouverte v2.0 (Etalab)",
    licence_url="https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    attribution="Métropole de Lyon (data.grandlyon.com), Licence Ouverte v2.0", fetch=fetch,
    hosts=("download.data.grandlyon.com/files/rdata/pvo_patrimoine_voirie.pvocameracriter/",), country="FR",
    region="Métropole de Lyon", refresh=60, tags=("traffic", "urban"),
    coverage="Main roads, bridges and tunnels of the Lyon metropolis",
)
