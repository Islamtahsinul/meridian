"""North East England UTMC Open Data Service: Tyne and Wear and Northumberland traffic cameras, the
Tyne Tunnel cameras and road weather cameras, published by Newcastle City Council for the region's
councils under the Open Government Licence v3.

The camera list (``cctv/static``) and the latest image links (``cctv/dynamic``) need a free account
on netraveldata.co.uk, sent as HTTP Basic credentials: set ``NETRAVELDATA_KEY`` to
``username:password``. The images themselves are public. Public-safety joint-use cameras (codes
starting PS, town-centre CCTV) are left out, and so are Durham's cameras, which ``gbdurham`` lists
without a key.
"""

from __future__ import annotations

import base64
import re
import time
from datetime import datetime

from ..common import FetchContext, NeedsKey, Source, clean, make_camera, sanitize_id

KEY = "NETRAVELDATA_KEY"
STATIC_URL = "https://www.netraveldata.co.uk/api/v2/cctv/static"
DYNAMIC_URL = "https://www.netraveldata.co.uk/api/v2/cctv/dynamic"
IMAGE_URL = "https://www.netraveldata.co.uk/api/v2/cctv/images/{code}.jpg"
SIGNUP_URL = "https://www.netraveldata.co.uk/wp-login.php?action=register"
OFFLINE_AFTER_S = 3 * 24 * 3600          # no new picture for three days: treat as out of service
# system code prefix -> (area, tags, typical image cadence in seconds); see netraveldata.co.uk/?page_id=42
PREFIXES = {
    "GH": ("Gateshead", ("urban",), 180), "MC": ("Gateshead", ("urban",), 180),
    "NB": ("Northumberland", (), 180), "NC": ("Newcastle upon Tyne", ("urban",), 180),
    "NT": ("North Tyneside", ("urban",), 180), "SL": ("Sunderland", ("urban",), 180),
    "ST": ("South Tyneside", ("urban",), 180), "TT2": ("Tyne Tunnel", ("tunnel",), 60),
    "METCCTV": ("", ("weather",), 1200), "VAISALACCTV": ("", ("weather",), 1200),
}
SKIP_PREFIXES = ("PS", "DU", "DCC")       # public-safety joint use; Durham (see eu_durham.py)
_CODE = re.compile(r"^[A-Za-z0-9_-]{2,48}$")
_ROAD = re.compile(r"\b(A\d{1,4}(?:\(M\))?|B\d{3,4}|M\d{1,3})\b")


def _prefix(code: str) -> str:
    """'GH_A184B1' -> 'GH', 'TT2North1' -> 'TT2', 'METCCTV00022' -> 'METCCTV'."""
    upper = code.upper()
    for p in sorted(PREFIXES, key=len, reverse=True):
        if upper.startswith(p):
            return p
    return upper.split("_", 1)[0]


def _epoch(value) -> float | None:
    """'2017-02-20T09:01:01.000+0000' -> epoch seconds."""
    text = clean(value, 40)
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def latest_images(dynamic, now: float | None = None) -> tuple[dict[str, str], set[str]]:
    """Image links by system code from the dynamic feed, plus the codes with no picture for days."""
    now = time.time() if now is None else now
    images: dict[str, str] = {}
    stale: set[str] = set()
    for item in dynamic if isinstance(dynamic, list) else []:
        if not isinstance(item, dict):
            continue
        code = clean(item.get("systemCodeNumber"), 60)
        dyn = [d for d in item.get("dynamics") or [] if isinstance(d, dict)]
        if not code or not dyn:
            continue
        last = dyn[-1]
        when = _epoch(last.get("lastUpdated"))
        if when is not None and now - when > OFFLINE_AFTER_S:
            stale.add(code)
        image = clean(last.get("image"), 300)
        if image:
            images[code] = image
    return images, stale


def parse(static, src: Source, images: dict[str, str] | None = None, stale: set[str] | None = None
          ) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    images = images or {}
    stale = stale or set()
    for item in static if isinstance(static, list) else []:
        if not isinstance(item, dict):
            continue
        code = clean(item.get("systemCodeNumber"), 60)
        defs = [d for d in item.get("definitions") or [] if isinstance(d, dict)]
        if not code or not _CODE.match(code) or not defs:
            skipped += 1
            continue
        prefix = _prefix(code)
        if prefix in SKIP_PREFIXES or code in stale:
            skipped += 1
            continue
        d = defs[-1]
        point = d.get("point") or {}
        area, tags, cadence = PREFIXES.get(prefix, ("", (), 180))
        long_name = clean(d.get("longDescription")) or clean(d.get("shortDescription")) or code
        place, sep, rest = long_name.partition(" - ")
        if sep and place.lower() in (area.lower(), "newcastle", "gateshead", "sunderland", "metrocentre"):
            name = rest                    # "Gateshead - A184 Bus Interchange - ..." -> "A184 Bus Interchange - ..."
            area = area or place
        else:
            name = long_name
        road = _ROAD.search(name)
        image = images.get(code) or IMAGE_URL.format(code=sanitize_id(code))
        row = make_camera(src, code, name, point.get("latitude"), point.get("longitude"), image=image,
                          area=area or None, road=road.group(1) if road else None, refresh=cadence, tags=tags)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def _auth_header(key: str) -> dict:
    if ":" not in key:
        raise RuntimeError(f"{KEY} must be the netraveldata.co.uk username and password as username:password")
    token = base64.b64encode(key.encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def fetch(fc: FetchContext) -> list[dict]:
    key = fc.key(KEY)
    if not key:
        raise NeedsKey(KEY)
    headers = _auth_header(key)
    static = fc.json(STATIC_URL, "static.json", headers=headers, min_bytes=50, timeout=60, max_age_hours=20)
    images: dict[str, str] = {}
    stale: set[str] = set()
    try:
        images, stale = latest_images(fc.json(DYNAMIC_URL, "dynamic.json", headers=headers, min_bytes=50, timeout=60,
                                              max_age_hours=3))
    except Exception as err:  # the list alone still gives every camera (image links follow a fixed pattern)
        fc.notes.append(f"latest-image list unavailable ({clean(err, 100)})")
    rows, skipped = parse(static, fc.src, images, stale)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="gbnetravel", name="North East England UTMC cameras",
    owner="Tyne and Wear UTMC, Newcastle City Council (UTMC Open Data Service)",
    url="https://www.netraveldata.co.uk/?page_id=42", licence="Open Government Licence v3",
    licence_url="https://www.netraveldata.co.uk/?page_id=13",
    attribution="North East UTMC Open Data Service, © Newcastle City Council, OGL v3", fetch=fetch,
    hosts=("www.netraveldata.co.uk/api/v1/cctv/images/", "www.netraveldata.co.uk/api/v2/cctv/images/"),
    country="GB", region="North East England", refresh=180, tags=("traffic",), camera_type="traffic",
    needs_key=KEY, optional=True,
    coverage="Tyne and Wear, Northumberland, the Tyne Tunnel and North East road weather stations",
)
