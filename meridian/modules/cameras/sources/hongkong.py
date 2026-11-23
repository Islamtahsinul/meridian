"""Hong Kong Transport Department traffic snapshot cameras (about 1,000)."""

from __future__ import annotations

import re

from ..common import FetchContext, Source, make_camera, parse_xml, read_bytes

URL = "https://static.data.gov.hk/td/traffic-snapshot-images/code/Traffic_Camera_Locations_En.xml"
_KEY = re.compile(r"\s*\[[A-Z0-9]+\]\s*$")


def parse(data: bytes, src: Source) -> tuple[list[dict], int]:
    root = parse_xml(data)
    rows: list[dict] = []
    skipped = 0
    for img in root.iter("image"):
        key = (img.findtext("key") or "").strip()
        desc = _KEY.sub("", img.findtext("description") or "")
        row = make_camera(src, key, desc, img.findtext("latitude"), img.findtext("longitude"),
                          image=img.findtext("url"), region=img.findtext("region"), area=img.findtext("district"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(read_bytes(fc.download(URL, "locations.xml", min_bytes=5000, timeout=90)), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="hk", name="Hong Kong traffic cameras", owner="Transport Department, HKSAR Government",
    url="https://data.gov.hk/en-data/dataset/hk-td-tis_2-traffic-snapshot-images",
    licence="DATA.GOV.HK terms of use (free use with attribution)",
    licence_url="https://data.gov.hk/en/terms-and-conditions", attribution="Transport Department, HKSAR",
    fetch=fetch, hosts=("tdcctv.data.one.gov.hk",), country="HK", refresh=120, tags=("traffic", "urban"),
    coverage="Hong Kong Island, Kowloon and the New Territories",
)
