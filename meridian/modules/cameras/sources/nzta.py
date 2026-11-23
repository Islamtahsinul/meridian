"""NZ Transport Agency Waka Kotahi traffic cameras (trafficnz.info REST service)."""

from __future__ import annotations

from ..common import FetchContext, Source, make_camera, parse_xml, read_bytes

URL = "https://trafficnz.info/service/traffic/rest/4/cameras/all"
BASE = "https://www.trafficnz.info"


def parse(data: bytes, src: Source) -> tuple[list[dict], int]:
    root = parse_xml(data)
    rows: list[dict] = []
    skipped = 0
    for cam in root.iter("camera"):
        if (cam.findtext("offline") or "").strip() == "true" or (cam.findtext("underMaintenance") or "").strip() == "true":
            skipped += 1
            continue
        image = cam.findtext("imageUrl") or ""
        view = cam.findtext("viewUrl") or ""
        row = make_camera(src, cam.findtext("id"), cam.findtext("name"), cam.findtext("latitude"),
                          cam.findtext("longitude"), image=BASE + image if image.startswith("/") else image,
                          page=BASE + view if view.startswith("/") else view or None,
                          road=cam.findtext("highway"), region=cam.findtext("region/name"),
                          direction=cam.findtext("direction"), description=cam.findtext("description"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(read_bytes(fc.download(URL, "cameras.xml", min_bytes=5000, timeout=120)), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="nz", name="NZTA traffic cameras", owner="NZ Transport Agency Waka Kotahi", url="https://www.trafficnz.info/",
    licence="CC BY 4.0", licence_url="https://creativecommons.org/licenses/by/4.0/",
    attribution="NZ Transport Agency Waka Kotahi, CC BY 4.0", fetch=fetch,
    hosts=("www.trafficnz.info", "trafficnz.info"), country="NZ", refresh=120, tags=("traffic",),
    coverage="New Zealand state highways",
)
