"""Département de Loire-Atlantique: live webcams of the Saint-Nazaire bridge and of the queues at the
Loire ferries (Basse-Indre - Indret, Couëron - Le Pellerin), open data under Licence Ouverte v2.0.

The dataset is a list of image files whose links stay the same while the picture is refreshed. It
has no coordinates, so each known file name is placed at the crossing it shows (positions taken from
OpenStreetMap; the two views of each ferry crossing share a spot between the landings). Unknown
file names are skipped.
"""

from __future__ import annotations

from ..common import FetchContext, Source, clean, http_url, make_camera

URL = ("https://data.loire-atlantique.fr/api/explore/v2.1/catalog/datasets/"
       "224400028_webcams-du-pont-de-saint-nazaire-et-des-bacs-de-loire-rt/records?limit=50")
FILE_PREFIX = ("https://data.loire-atlantique.fr/api/explore/v2.1/catalog/datasets/"
               "224400028_webcams-du-pont-de-saint-nazaire-et-des-bacs-de-loire-rt/files/")
# file name -> (name, area, road, lat, lon, tags)
PLACES = {
    "psn": ("Pont de Saint-Nazaire", "Saint-Nazaire", "D213", 47.28305, -2.16884, ("bridge", "river")),
    "indre1": ("Bac de Loire Basse-Indre - Indret, view 1", "Indre", None, 47.1957, -1.6758, ("port", "river")),
    "indre2": ("Bac de Loire Basse-Indre - Indret, view 2", "Indre", None, 47.1957, -1.6758, ("port", "river")),
    "coueron1": ("Bac de Loire Couëron - Le Pellerin, view 1", "Couëron", None, 47.2075, -1.7505, ("port", "river")),
    "coueron2": ("Bac de Loire Couëron - Le Pellerin, view 2", "Couëron", None, 47.2075, -1.7505, ("port", "river")),
}


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for rec in (data or {}).get("results") or [] if isinstance(data, dict) else []:
        f = (rec or {}).get("file") if isinstance(rec, dict) else None
        if not isinstance(f, dict):
            skipped += 1
            continue
        stem = clean(f.get("filename")).lower().rsplit(".", 1)[0]
        url = http_url(f.get("url"))
        place = PLACES.get(stem)
        if place is None or not url or not url.startswith(FILE_PREFIX):
            skipped += 1
            continue
        name, area, road, lat, lon, tags = place
        row = make_camera(src, stem, name, lat, lon, image=url, area=area, road=road, tags=tags,
                          description="Traffic on the bridge" if stem == "psn" else "Queue at the ferry landing")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(fc.json(URL, "records.json", min_bytes=200, timeout=60), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="frloire", name="Loire-Atlantique bridge and ferry webcams", owner="Département de Loire-Atlantique",
    url="https://data.loire-atlantique.fr/", licence="Licence Ouverte v2.0 (Etalab)",
    licence_url="https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    attribution="Département de Loire-Atlantique, Licence Ouverte v2.0", fetch=fetch,
    hosts=("data.loire-atlantique.fr/api/explore/v2.1/catalog/datasets/"
           "224400028_webcams-du-pont-de-saint-nazaire-et-des-bacs-de-loire-rt/files/",),
    country="FR", region="Loire-Atlantique", refresh=300, tags=("traffic",),
    coverage="Saint-Nazaire bridge and the Loire ferries near Nantes",
)
