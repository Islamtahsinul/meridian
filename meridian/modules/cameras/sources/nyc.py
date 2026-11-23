"""NYC DOT traffic cameras (webcams.nyctmc.org). Optional: the host often times out from some
networks, in which case the previous list is kept and the layer works without it."""

from __future__ import annotations

from ..common import FetchContext, Source, make_camera

URL = "https://webcams.nyctmc.org/api/cameras"


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for cam in data if isinstance(data, list) else []:
        if not isinstance(cam, dict):
            continue
        if str(cam.get("isOnline", "true")).lower() == "false":
            skipped += 1
            continue
        cid = cam.get("id")
        image = cam.get("imageUrl") or (f"https://webcams.nyctmc.org/api/cameras/{cid}/image" if cid else None)
        row = make_camera(src, cid, cam.get("name"), cam.get("latitude"), cam.get("longitude"), image=image,
                          area=cam.get("area"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(fc.json(URL, "cameras.json", min_bytes=1000, timeout=30), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="nyc", name="NYC DOT traffic cameras", owner="New York City Department of Transportation",
    url="https://webcams.nyctmc.org/", licence="NYC DOT public traffic camera feed (credit NYC DOT)",
    licence_url="https://www.nyc.gov/home/terms-of-use.page", attribution="NYC DOT", fetch=fetch,
    hosts=("webcams.nyctmc.org",), country="US", region="New York", refresh=60, tags=("traffic", "urban"),
    optional=True, coverage="New York City",
)
