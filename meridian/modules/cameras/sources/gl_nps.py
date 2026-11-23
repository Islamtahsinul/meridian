"""US National Park Service webcams (optional, needs a free NPS_API_KEY): about 290 cameras in
national parks, monuments and seashores, from the NPS Data API (``/api/v1/webcams``).

The API gives each webcam's position, park and page, but not a live picture link (its ``images``
are occasional still photos of the view), so these cameras open their nps.gov page; the map shows
them as "opens on its source". The key goes in the ``X-Api-Key`` header, never in a URL.
"""

from __future__ import annotations

import urllib.parse

from ..common import FetchContext, NeedsKey, Source, clean, make_camera, to_int

KEY = "NPS_API_KEY"
URL = "https://developer.nps.gov/api/v1/webcams"
PAGE = 100
MAX_PAGES = 10
_STATES = {"AK": "Alaska", "AZ": "Arizona", "CA": "California", "CO": "Colorado", "FL": "Florida", "HI": "Hawaii",
           "ID": "Idaho", "ME": "Maine", "MT": "Montana", "NC": "North Carolina", "NM": "New Mexico", "NV": "Nevada",
           "OR": "Oregon", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VA": "Virginia",
           "WA": "Washington", "WY": "Wyoming", "MI": "Michigan", "MN": "Minnesota", "NY": "New York",
           "MA": "Massachusetts", "PA": "Pennsylvania", "WV": "West Virginia", "KY": "Kentucky", "AR": "Arkansas",
           "VI": "US Virgin Islands", "PR": "Puerto Rico", "GU": "Guam", "AS": "American Samoa"}
_TERRITORIES = {"VI": "VI", "PR": "PR", "GU": "GU", "AS": "AS"}


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    items = (data or {}).get("data") if isinstance(data, dict) else None
    for cam in items if isinstance(items, list) else []:
        if not isinstance(cam, dict):
            continue
        if (cam.get("status") or "Active").lower() != "active":
            skipped += 1
            continue
        parks = [p for p in cam.get("relatedParks") or [] if isinstance(p, dict)]
        park = parks[0] if parks else {}
        state = clean(park.get("states")).split(",")[0].strip().upper()
        row = make_camera(src, cam.get("id"), cam.get("title"), cam.get("latitude"), cam.get("longitude"),
                          page=cam.get("url"), area=park.get("fullName"), region=_STATES.get(state),
                          country=_TERRITORIES.get(state, "US"), description=cam.get("description"),
                          tags=[clean(t).lower() for t in cam.get("tags") or [] if isinstance(t, str)],
                          camera_type="webcam")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    key = fc.key(KEY)
    if not key:
        raise NeedsKey(KEY)
    rows: list[dict] = []
    for page in range(MAX_PAGES):
        params = {"limit": PAGE, "start": page * PAGE}
        data = fc.json(f"{URL}?{urllib.parse.urlencode(params)}", f"webcams-{page}.json", min_bytes=50, timeout=60,
                       headers={"X-Api-Key": key, "Accept": "application/json"}, min_interval=1.0)
        got, skipped = parse(data, fc.src)
        rows.extend(got)
        fc.skip(skipped)
        total = to_int((data or {}).get("total")) if isinstance(data, dict) else None
        if not (data or {}).get("data") or total is None or (page + 1) * PAGE >= total:
            break
    return rows


SOURCE = Source(
    id="nps", name="National Park Service webcams", owner="US National Park Service",
    url="https://www.nps.gov/subjects/developer/api-documentation.htm",
    licence="Public domain (US National Park Service data, unless a webcam credits another owner)",
    licence_url="https://www.nps.gov/aboutus/disclaimer.htm", attribution="National Park Service", fetch=fetch,
    hosts=("www.nps.gov",), country="US", refresh=900, tags=(), needs_key=KEY, optional=True, camera_type="webcam",
    coverage="US national parks, monuments and seashores (pages only: the API has no live picture links)",
)
