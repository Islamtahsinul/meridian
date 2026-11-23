"""Caltrans CCTV: about 3,500 California highway cameras in 12 districts, stills and HLS video."""

from __future__ import annotations

import re
import urllib.parse

from ..common import FetchContext, Source, clean, make_camera, to_float

URL = "https://cwwp2.dot.ca.gov/data/d{n}/cctv/cctvStatusD{nn:02d}.json"
DISTRICTS = tuple(range(1, 13))
_INDEX = re.compile(r"^\(\s*[\w.-]+\s*\)\s*")


def split_name(location_name: str) -> tuple[str, str]:
    """'I-110 : (196) Avenue 26 Off Ramp' -> ('I-110', 'Avenue 26 Off Ramp')."""
    text = clean(location_name)
    route, sep, rest = text.partition(" : ")
    if not sep:
        return "", _INDEX.sub("", text)
    return route.strip(), _INDEX.sub("", rest.strip()) or route.strip()


def parse(data, district: int, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    items = data.get("data") if isinstance(data, dict) else None
    for rec in items or []:
        cctv = (rec or {}).get("cctv") if isinstance(rec, dict) else None
        if not isinstance(cctv, dict):
            continue
        if str(cctv.get("inService", "true")).lower() != "true":
            skipped += 1
            continue
        loc = cctv.get("location") or {}
        img = cctv.get("imageData") or {}
        static = img.get("static") or {}
        image = static.get("currentImageURL")
        path = urllib.parse.urlsplit(image or "").path
        slug = path.rsplit("/", 1)[-1].rsplit(".", 1)[0] if path else ""
        native = f"d{district}-{slug or cctv.get('index')}"
        route, place = split_name(loc.get("locationName") or "")
        minutes = to_float(static.get("currentImageUpdateFrequency"))
        row = make_camera(src, native, place or route, loc.get("latitude"), loc.get("longitude"),
                          image=image, stream=img.get("streamingVideoURL") or None, road=route or loc.get("route"),
                          area=loc.get("nearbyPlace"), direction=loc.get("direction"),
                          refresh=int(minutes * 60) if minutes else None,
                          description=f"District {district}, postmile {loc.get('postmile')}" if loc.get("postmile") else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows: list[dict] = []
    failures = []
    for n in DISTRICTS:
        try:
            data = fc.json(URL.format(n=n, nn=n), f"d{n:02d}.json", min_bytes=500, timeout=120)
        except Exception as err:  # one district failing keeps the others
            failures.append(f"district {n}: {clean(str(err), 120)}")
            continue
        got, skipped = parse(data, n, fc.src)
        rows.extend(got)
        fc.skip(skipped)
    if failures:
        fc.notes.append("; ".join(failures))
        if len(failures) == len(DISTRICTS):
            raise RuntimeError(failures[0])
    return rows


SOURCE = Source(
    id="caltrans", name="Caltrans CCTV", owner="California Department of Transportation",
    url="https://cwwp2.dot.ca.gov/documentation/cctv/cctv.htm",
    licence="Caltrans CWWP2 public data feed (no restrictions stated; credit Caltrans)",
    licence_url="https://cwwp2.dot.ca.gov/documentation/cctv/cctv.htm",
    attribution="Caltrans", fetch=fetch, hosts=("cwwp2.dot.ca.gov",), country="US", region="California",
    refresh=120, tags=("traffic",), stream_cors=True, coverage="California state highways",
)
