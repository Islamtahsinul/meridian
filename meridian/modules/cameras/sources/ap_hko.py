"""Hong Kong Observatory weather photos: about 34 cameras on the Observatory's weather stations
(Victoria Peak, Tai Mo Shan, Waglan Island, the harbour, the islands), a new picture every 5 minutes.

There is no open dataset of these cameras; the list (codes, English and Chinese names, positions and
directions) comes from the station configuration file the Observatory's own map loads. HKO's terms
allow non-commercial use with acknowledgement of the Government of the HKSAR as owner and of
hko.gov.hk as the source (https://www.hko.gov.hk/en/appweb/applink.htm).
"""

from __future__ import annotations

import re

from ..common import FetchContext, Source, clean, decode_text, make_camera, read_bytes, to_float
from .ap_common import bilingual

CONFIG_URL = "https://www.hko.gov.hk/en/wxinfo/awsgis/files/irwip-station-config-aws.js"
IMAGE = "https://www.hko.gov.hk/wxinfo/aws/hko_mica/{code}/latest_{CODE}.jpg"
PAGE = "https://www.hko.gov.hk/en/wxinfo/ts/index_webcam.htm"
_BLOCK = re.compile(r"webcam\s*:\s*\[(.*?)\]", re.S)
_OBJECT = re.compile(r"\{(.*?)\}", re.S)
_PAIR = re.compile(r"(\w+)\s*:\s*\"([^\"]*)\"")
_CODE = re.compile(r"^[a-z0-9]{2,8}$")


def cameras_of(text: str) -> list[dict]:
    """The webcam entries of the station configuration script, de-duplicated by code."""
    seen: dict[str, dict] = {}
    for block in _BLOCK.finditer(text or ""):
        for obj in _OBJECT.finditer(block.group(1)):
            entry = dict(_PAIR.findall(obj.group(1)))
            code = clean(entry.get("code")).lower()
            if _CODE.match(code):
                seen.setdefault(code, entry)
    return list(seen.values())


def fix_positions(cams: list[dict]) -> None:
    """A camera listed at another station's position (cs2 'Cheung Sha' sits at the ICC) takes the
    position of its same-named sibling, when it has one."""
    by_pos: dict[tuple[str, str], set[str]] = {}
    for c in cams:
        by_pos.setdefault((c.get("latitude", ""), c.get("longitude", "")), set()).add(clean(c.get("name_EN")))
    for c in cams:
        names = by_pos.get((c.get("latitude", ""), c.get("longitude", "")), set())
        if len(names) < 2:
            continue
        twin = next((o for o in cams if o is not c and clean(o.get("name_EN")) == clean(c.get("name_EN"))
                     and len(by_pos.get((o.get("latitude", ""), o.get("longitude", "")), set())) == 1), None)
        if twin:
            c["latitude"], c["longitude"] = twin.get("latitude", ""), twin.get("longitude", "")


def parse(text: str, src: Source) -> tuple[list[dict], int]:
    cams = cameras_of(text)
    fix_positions(cams)
    rows: list[dict] = []
    skipped = 0
    for c in cams:
        code = clean(c.get("code")).lower()
        place = bilingual(c.get("name_TC"), c.get("name_EN"))
        facing = clean(c.get("direction_EN"))
        remark = clean(c.get("remark_EN")).lstrip(" ,")
        row = make_camera(src, code, f"{place}, looking {facing}" if facing else place,
                          to_float(c.get("latitude")), to_float(c.get("longitude")),
                          image=IMAGE.format(code=code, CODE=code.upper()), page=PAGE,
                          heading=clean(c.get("angle")) or None, direction=facing or None,
                          description=f"Looking {facing} {remark}".strip() if remark else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    path = fc.download(CONFIG_URL, "station-config.js", min_bytes=1000, timeout=60, headers={"Accept-Encoding": "gzip"})
    rows, skipped = parse(decode_text(read_bytes(path, limit=5_000_000)), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="hkobs", name="Hong Kong Observatory weather photos", owner="Hong Kong Observatory",
    url=PAGE, licence="HKO terms: non-commercial use with acknowledgement (© Government of the HKSAR)",
    licence_url="https://www.hko.gov.hk/en/appweb/applink.htm",
    attribution="Hong Kong Observatory, hko.gov.hk (© The Government of the Hong Kong SAR)", fetch=fetch,
    hosts=("www.hko.gov.hk/wxinfo/aws/hko_mica/",), country="HK", region="Hong Kong", refresh=300,
    tags=("weather",), camera_type="webcam", coverage="Hong Kong Observatory weather stations",
)
