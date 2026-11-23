"""USGS HIVIS: webcams at USGS stream gauges and river sites across the United States (about a
thousand live), from the USGS National Imagery Management System (NIMS) API. USGS images are in the
public domain. Each camera's newest picture has a fixed link (``<camId>_newest.jpg``); the 720-pixel
copy is used. Cameras the API hides, or whose newest picture is more than a week old, are skipped."""

from __future__ import annotations

import time
from datetime import datetime

from ..common import FetchContext, Source, clean, make_camera, to_int
from .na_common import s3_path_style, state_name

URL = "https://api.waterdata.usgs.gov/nims/cameras?enabled=true"
PAGE = "https://apps.usgs.gov/hivis/camera/{id}"
MAX_AGE_S = 7 * 86400


def _epoch(value) -> float | None:
    text = clean(value, 40)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse(data, src: Source, now: float | None = None) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    now = time.time() if now is None else now
    for cam in data if isinstance(data, list) else []:
        if not isinstance(cam, dict):
            continue
        cam_id = clean(cam.get("camId"), 96)
        newest = _epoch(cam.get("newestImageDT"))
        if not cam_id or cam.get("hideCam") is True or newest is None or now - newest > MAX_AGE_S:
            skipped += 1
            continue
        base = clean(cam.get("smallDir") or cam.get("overlayDir"), 400)
        if not base.endswith("/"):
            base += "/"
        country, region = state_name(cam.get("stateAbrv"))
        minutes = to_int((cam.get("ingest") or {}).get("intr")) if isinstance(cam.get("ingest"), dict) else None
        name = clean(cam.get("camName")) or clean(cam.get("camDesc"))
        row = make_camera(src, cam_id, name, cam.get("lat"), cam.get("lng"),
                          image=s3_path_style(f"{base}{cam_id}_newest.jpg"), page=PAGE.format(id=cam_id),
                          country=country, region=region, refresh=minutes * 60 if minutes else None,
                          description=cam.get("camDesc"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    data = fc.json(URL, "cameras.json", min_bytes=1000, timeout=90, headers={"Accept": "application/json"})
    rows, skipped = parse(data, fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="usgshivis", name="USGS river webcams (HIVIS)", owner="U.S. Geological Survey",
    url="https://apps.usgs.gov/hivis/", licence="Public domain (USGS)",
    licence_url="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
    attribution="U.S. Geological Survey (HIVIS)", fetch=fetch,
    hosts=("s3.amazonaws.com/usgs-nims-images/", "usgs-nims-images.s3.amazonaws.com"), country="US",
    refresh=900, tags=("river", "weather"), camera_type="webcam",
    coverage="Rivers and streams at USGS gauges in every US state, Puerto Rico and Guam",
)
