"""A22 Brenner motorway cameras (Autostrada del Brennero, Italy), listed on the Open Data Hub of
South Tyrol (``tourism.api.opendatahub.com/v1/WebcamInfo?source=a22``).

The hub publishes each record under CC0 (``LicenseInfo``); its licensing notes say that licence
covers the record, not the linked image, so only the motorway operator's own stills
(``www.autobrennero.it/WebCamImg/km<n>.jpg``, published for road users and renewed about
once a minute) are used. The hub's other webcams are left out on purpose: almost all of their
images belong to commercial webcam companies or private hotels and lift operators.
"""

from __future__ import annotations

import urllib.parse

from ..common import FetchContext, Source, clean, make_camera, to_float, to_int
from .eu_common import pick_lang

LIST_URL = "https://tourism.api.opendatahub.com/v1/WebcamInfo"
FIELDS = "Id,Active,Shortname,Webcamname,GpsPoints,Webcamurl,ImageGallery,LicenseInfo,Source,Mapping"
PAGE_SIZE = 100
MAX_PAGES = 5
# the regions the motorway crosses, by kilometre from the Brenner Pass (approximate borders)
_REGIONS = ((110, "South Tyrol"), (189, "Trentino"), (250, "Veneto"), (281, "Lombardy"), (400, "Emilia-Romagna"))


def region_of(km: int | None) -> str | None:
    if km is None or km < 0:
        return None
    for limit, name in _REGIONS:
        if km < limit:
            return name
    return None


def _items(data) -> list:
    if isinstance(data, dict):
        data = data.get("Items")
    return [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for it in _items(data):
        if clean(it.get("Source")).lower() != "a22":
            continue                                    # another provider's record: never used
        licence = it.get("LicenseInfo") or {}
        if it.get("Active") is False or licence.get("ClosedData") is True:
            skipped += 1
            continue
        pos = ((it.get("GpsPoints") or {}).get("position") or {})
        gallery = [g.get("ImageUrl") for g in it.get("ImageGallery") or [] if isinstance(g, dict) and g.get("ImageUrl")]
        image = gallery[0] if gallery else it.get("Webcamurl")
        km = to_int(((it.get("Mapping") or {}).get("a22") or {}).get("km"))
        place = pick_lang(it.get("Webcamname"), ("en", "it", "de")) or clean(it.get("Shortname"))
        name = f"A22 km {km}, {place}" if km is not None and place else (place or f"A22 km {km}")
        row = make_camera(src, it.get("Id"), name, pos.get("Latitude"), pos.get("Longitude"), image=image,
                          road="A22", region=region_of(km), elevation=to_float(pos.get("Altitude")),
                          description=f"Brenner motorway, km {km}" if km is not None else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows: list[dict] = []
    skipped = 0
    for page in range(1, MAX_PAGES + 1):
        # the hub asks API users for a referer; this is the same generic name as the User-Agent
        params = {"source": "a22", "pagesize": PAGE_SIZE, "pagenumber": page, "fields": FIELDS,
                  "referer": "open-data-map-client"}
        data = fc.json(f"{LIST_URL}?{urllib.parse.urlencode(params)}", f"webcams-{page}.json", min_bytes=50,
                       timeout=60)
        got, skip = parse(data, fc.src)
        rows.extend(got)
        skipped += skip
        pages = to_int(data.get("TotalPages")) if isinstance(data, dict) else None
        if not pages or page >= pages:
            break
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="ita22", name="A22 Brenner motorway cameras", owner="Autostrada del Brennero S.p.A. (A22), Italy",
    url="https://www.autobrennero.it/it/viabilita/webcam/",
    licence="Open Data Hub record CC0; stills published by Autostrada del Brennero for road users (credit A22)",
    licence_url="https://docs.opendatahub.com/licensing/",
    attribution="Autostrada del Brennero (A22), via the Open Data Hub of South Tyrol", fetch=fetch,
    # exact host only: the proxy lowercases a "host/path" pattern but not the path it checks, so the
    # mixed-case /WebCamImg/ prefix could never match
    hosts=("www.autobrennero.it",), country="IT", refresh=60, tags=("traffic",),
    coverage="A22 Brenner motorway, from the Brenner Pass to Modena",
)
