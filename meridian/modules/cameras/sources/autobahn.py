"""Autobahn GmbH des Bundes webcams (Germany), from the public verkehr.autobahn.de API.

The API lists webcams per motorway. In November 2026 every motorway returned an empty list, so
this adapter usually yields nothing; it stays enabled so cameras appear if the API publishes them.
"""

from __future__ import annotations

import re

from ..common import FetchContext, Source, make_camera

ROADS_URL = "https://verkehr.autobahn.de/o/autobahn/"
WEBCAM_URL = "https://verkehr.autobahn.de/o/autobahn/{road}/services/webcam"
_ROAD = re.compile(r"^A\d{1,3}[a-z]?$")


def parse(data, road: str, src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for cam in (data or {}).get("webcam") or [] if isinstance(data, dict) else []:
        if not isinstance(cam, dict):
            continue
        coord = cam.get("coordinate") or {}
        row = make_camera(src, cam.get("identifier"), cam.get("title") or cam.get("subtitle"),
                          coord.get("lat"), coord.get("long"), image=cam.get("imageurl"), page=cam.get("linkurl"),
                          road=road, description=cam.get("subtitle"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    roads = [r for r in (fc.json(ROADS_URL, "roads.json", timeout=60).get("roads") or [])
             if isinstance(r, str) and _ROAD.match(r.strip())]
    rows: list[dict] = []
    failed = 0
    for road in roads:
        if fc.time_left() < 20:
            fc.notes.append("stopped early (time budget)")
            break
        try:
            data = fc.json(WEBCAM_URL.format(road=road), f"webcam-{road}.json", timeout=30, min_interval=0.25)
        except Exception:
            failed += 1
            continue
        got, skipped = parse(data, road, fc.src)
        rows.extend(got)
        fc.skip(skipped)
    if roads and failed == len(roads):
        raise RuntimeError("every motorway webcam request failed")
    if not rows:
        fc.notes.append(f"no webcams published on {len(roads)} motorways")
    return rows


SOURCE = Source(
    id="autobahn", name="Autobahn webcams", owner="Die Autobahn GmbH des Bundes", url="https://autobahn.api.bund.dev/",
    licence="Datenlizenz Deutschland, Namensnennung 2.0", licence_url="https://www.govdata.de/dl-de/by-2-0",
    attribution="Die Autobahn GmbH des Bundes, dl-de/by-2-0", fetch=fetch,
    hosts=("*.autobahn.de",), country="DE", refresh=300, tags=("traffic",), coverage="German motorways",
)
