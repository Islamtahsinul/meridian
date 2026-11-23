"""SANRAL i-traffic (South African National Roads Agency) traffic cameras in Gauteng, the Western
Cape, KwaZulu-Natal and the Eastern Cape, from the i-traffic developer API (the same 511-style API
as Ontario 511). Needs a free developer key (``SANRAL_ITRAFFIC_KEY``): register for an account at
https://www.i-traffic.co.za/ and request a key on https://www.i-traffic.co.za/developers/doc.

The API allows 10 calls a minute; a build makes one. The website's own terms forbid reusing its
pages or scraping them, so only the developer API is used.
"""

from __future__ import annotations

import urllib.parse

from ..common import FetchContext, NeedsKey, Source
from .ibi511 import parse

KEY = "SANRAL_ITRAFFIC_KEY"
BASE = "https://www.i-traffic.co.za"


def fetch(fc: FetchContext) -> list[dict]:
    key = fc.key(KEY)
    if not key:
        raise NeedsKey(KEY)
    url = f"{BASE}/api/v2/get/cameras?{urllib.parse.urlencode({'key': key, 'format': 'json', 'lang': 'en'})}"
    rows, skipped = parse(fc.json(url, "cameras.json", min_bytes=100, timeout=60, min_interval=7.0), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="zasanral", name="SANRAL i-traffic cameras", owner="South African National Roads Agency (SANRAL)",
    url=f"{BASE}/developers/doc", licence="i-traffic developer API terms (key required)",
    licence_url=f"{BASE}/developers/doc", attribution="SANRAL i-traffic", fetch=fetch,
    hosts=("www.i-traffic.co.za", "i-traffic.co.za"), country="ZA", refresh=120, tags=("traffic",),
    needs_key=KEY, optional=True, coverage="Gauteng, Western Cape, KwaZulu-Natal and Eastern Cape freeways",
)
