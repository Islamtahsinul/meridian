"""Singapore LTA traffic images via data.gov.sg. Image links change every minute, so the current
one is looked up from the (cached) API answer when the image is requested."""

from __future__ import annotations

from ..common import FetchContext, Source, make_camera

URL = "https://api.data.gov.sg/v1/transport/traffic-images"


def cameras_of(data) -> list[dict]:
    items = (data or {}).get("items") if isinstance(data, dict) else None
    if not items:
        return []
    return [c for c in (items[0] or {}).get("cameras") or [] if isinstance(c, dict)]


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for cam in cameras_of(data):
        loc = cam.get("location") or {}
        cid = str(cam.get("camera_id") or "")
        row = make_camera(src, cid, f"Traffic camera {cid}", loc.get("latitude"), loc.get("longitude"), dynamic=True)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(fc.json(URL, "traffic-images.json", min_bytes=200, timeout=60, max_age_hours=6), fc.src)
    fc.skip(skipped)
    return rows


def resolve(cam: dict, net) -> str | None:
    want = str(cam.get("id", "")).split(":", 1)[-1]
    data = net.get_json(URL, ttl=50, timeout=10, retries=0)
    for c in cameras_of(data):
        if str(c.get("camera_id")) == want:
            return c.get("image")
    return None


SOURCE = Source(
    id="sg", name="Singapore traffic images", owner="Land Transport Authority (via data.gov.sg)",
    url="https://data.gov.sg/datasets/d_6cdb6b405b25aaaacbaf7689bcc6fae0/view",
    licence="Singapore Open Data Licence v1.0", licence_url="https://data.gov.sg/open-data-licence",
    attribution="Contains information from data.gov.sg made available under the Singapore Open Data Licence v1.0",
    fetch=fetch, resolve=resolve, hosts=("images.data.gov.sg",), country="SG", region="Singapore", refresh=60,
    tags=("traffic", "urban"), coverage="Singapore expressways and checkpoints",
)
