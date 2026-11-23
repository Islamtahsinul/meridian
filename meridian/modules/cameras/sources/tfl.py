"""TfL JamCams: about 900 London traffic cameras with stills and short video clips."""

from __future__ import annotations

import re

from ..common import FetchContext, Source, heading_from, make_camera

URL = "https://api.tfl.gov.uk/Place/Type/JamCam"
_ROAD = re.compile(r"^((?:A|M|B)\d{1,4}(?:\(M\))?)\b")


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        props = {p.get("key"): p.get("value") for p in item.get("additionalProperties") or [] if isinstance(p, dict)}
        if str(props.get("available", "true")).strip().lower() == "false":
            skipped += 1
            continue
        native = str(item.get("id") or "").replace("JamCams_", "")
        name = item.get("commonName") or ""
        road = _ROAD.match(name)
        view = props.get("view") or ""
        row = make_camera(src, native, name, item.get("lat"), item.get("lon"),
                          image=props.get("imageUrl"), stream=props.get("videoUrl"), stream_kind="video",
                          road=road.group(1) if road else None,
                          direction=view if heading_from(view) is not None else None,
                          description=f"View: {view}" if view and view.lower() not in name.lower() else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    data = fc.json(URL, "jamcams.json", min_bytes=10_000, timeout=90)
    rows, skipped = parse(data, fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="tfl", name="TfL JamCams", owner="Transport for London", url="https://api-portal.tfl.gov.uk/",
    licence="TfL open data terms (based on the Open Government Licence v2.0)",
    licence_url="https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service",
    attribution="Powered by TfL Open Data", fetch=fetch,
    hosts=("s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/", "jamcams.tfl.gov.uk"),
    country="GB", region="London", refresh=180, tags=("traffic", "urban"), stream_cors=True,
    coverage="Greater London road network",
)
