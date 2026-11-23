"""Road weather cameras published on Iteris-run 511 maps: Montana (MDT road weather stations) and
South Dakota (SDDOT). Each site has several views; stable "latest picture" links.

Left out: South Carolina 511 on the same platform (SCDOT's terms forbid display without written
permission), and Montana's "other cameras" layer (other agencies' cameras: Wyoming, Idaho, Alberta
and national parks, covered elsewhere)."""

from __future__ import annotations

from ..common import FetchContext, Source
from .na_common import iteris_rows

MT_URL = "https://mt.cdn.iteris-atis.com/geojson/icons/metadata/icons.rwis.geojson"
SD_URL = "https://sd.cdn.iteris-atis.com/geojson/icons/metadata/icons.cameras.geojson"


def _fetch(url: str):
    def fetch(fc: FetchContext) -> list[dict]:
        rows, skipped = iteris_rows(fc.json(url, "sites.geojson", min_bytes=500, timeout=60, max_age_hours=12), fc.src)
        fc.skip(skipped)
        return rows
    return fetch


MONTANA = Source(
    id="usmtmdt", name="Montana road weather cameras", owner="Montana Department of Transportation (511MT)",
    url="https://www.511mt.net/",
    licence="MDT terms of use: anyone may view, copy or distribute for personal or informational use, unmodified",
    licence_url="https://www.mdt.mt.gov/mdt/terms-of-use.aspx", attribution="Montana Department of Transportation",
    fetch=_fetch(MT_URL), hosts=("mt.cdn.iteris-atis.com/rwis_images/",), country="US", region="Montana",
    refresh=900, tags=("weather", "traffic"), coverage="Montana road weather stations, passes and interstates",
)

SOUTH_DAKOTA = Source(
    id="ussd511", name="South Dakota 511 cameras", owner="South Dakota Department of Transportation (SD511)",
    url="https://www.sd511.org/",
    licence="SDDOT public 511 map feed (no licence stated; credit SDDOT)",
    licence_url="https://www.sd.gov/cs?id=kb_article_view&sysparm_article=KB0010221",
    attribution="South Dakota Department of Transportation", fetch=_fetch(SD_URL),
    hosts=("sd.cdn.iteris-atis.com/camera_images/",), country="US", region="South Dakota", refresh=600,
    tags=("weather", "traffic"), coverage="South Dakota interstates and road weather sites",
)

SOURCES = [MONTANA, SOUTH_DAKOTA]
