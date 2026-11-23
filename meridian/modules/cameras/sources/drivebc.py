"""DriveBC highway webcams (British Columbia, about 1,000)."""

from __future__ import annotations

import urllib.parse

from ..common import FetchContext, Source, clean, make_camera

URL = "https://www.drivebc.ca/api/webcams/"
BASE = "https://www.drivebc.ca"


def _image(path: str) -> str | None:
    if not path:
        return None
    parts = urllib.parse.urlsplit(urllib.parse.urljoin(BASE, path))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))   # drop the ?t= cache buster


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for cam in data if isinstance(data, list) else (data or {}).get("webcams", []) if isinstance(data, dict) else []:
        if not isinstance(cam, dict):
            continue
        if cam.get("is_on") is False or cam.get("should_appear") is False:
            skipped += 1
            continue
        coords = ((cam.get("location") or {}).get("coordinates") or [None, None])
        hw = clean(cam.get("highway_display") or cam.get("highway"))
        region = clean(cam.get("region_name"))
        row = make_camera(src, cam.get("id"), cam.get("name_override") or cam.get("name"),
                          coords[1] if len(coords) > 1 else None, coords[0] if coords else None,
                          image=_image((cam.get("links") or {}).get("imageDisplay") or ""),
                          road=f"Highway {hw}" if hw and hw[0].isdigit() else hw,
                          area=f"{region} region" if region else None, direction=cam.get("orientation"),
                          elevation=cam.get("elevation"), refresh=cam.get("update_period_mean"),
                          description=cam.get("caption_override") or cam.get("caption"), tags=("weather",))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(fc.json(URL, "webcams.json", min_bytes=5000, timeout=90), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="drivebc", name="DriveBC webcams", owner="BC Ministry of Transportation and Transit",
    url="https://www.drivebc.ca/", licence="Open Government Licence, British Columbia",
    licence_url="https://www2.gov.bc.ca/gov/content/data/policy-standards/open-data/open-government-licence-bc",
    attribution="DriveBC, Province of British Columbia", fetch=fetch, hosts=("www.drivebc.ca", "images.drivebc.ca"),
    country="CA", region="British Columbia", refresh=900, tags=("traffic",),
    coverage="British Columbia highways and mountain passes",
)
