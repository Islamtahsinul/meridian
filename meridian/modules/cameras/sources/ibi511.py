"""511 traveller-information systems on the common 511 API (Ontario, Alberta, New York). Each needs
a free developer key from its 511 site; without one the source is listed as needing a key."""

from __future__ import annotations

import urllib.parse

from ..common import FetchContext, NeedsKey, Source, clean, make_camera


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for cam in data if isinstance(data, list) else []:
        if not isinstance(cam, dict):
            continue
        views = cam.get("Views")
        if not isinstance(views, list):        # older single-view shape
            views = [{"Id": cam.get("ID") or cam.get("Id"), "Url": cam.get("Url"), "Status": cam.get("Status"),
                      "Description": cam.get("Name")}]
        road = cam.get("Roadway") or cam.get("RoadwayName")
        direction = cam.get("Direction") or cam.get("DirectionOfTravel")
        base = clean(cam.get("Location") or cam.get("Name"))
        for v in views:
            if not isinstance(v, dict):
                continue
            if str(v.get("Status") or "Enabled").lower() not in ("enabled", "active", "1", "true"):
                skipped += 1
                continue
            desc = clean(v.get("Description"))
            name = desc if desc and desc.lower() not in ("", base.lower()) and len(views) > 1 else base or desc
            native = f"{cam.get('Id') or cam.get('ID')}-{v.get('Id')}" if len(views) > 1 else (v.get("Id") or cam.get("Id"))
            row = make_camera(src, native, name, cam.get("Latitude"), cam.get("Longitude"), image=v.get("Url"),
                              road=road, direction=direction if direction and direction.lower() not in ("unknown", "none") else None)
            if row:
                rows.append(row)
            else:
                skipped += 1
    return rows, skipped


def _make(sid: str, name: str, owner: str, base: str, key_name: str, country: str, region: str, licence: str,
          licence_url: str, hosts: tuple[str, ...]) -> Source:
    def fetch(fc: FetchContext) -> list[dict]:
        key = fc.key(key_name)
        if not key:
            raise NeedsKey(key_name)
        url = f"{base}/api/v2/get/cameras?{urllib.parse.urlencode({'key': key, 'format': 'json', 'lang': 'en'})}"
        rows, skipped = parse(fc.json(url, "cameras.json", min_bytes=100, timeout=60), fc.src)
        fc.skip(skipped)
        return rows

    return Source(id=sid, name=name, owner=owner, url=f"{base}/developers/doc", licence=licence,
                  licence_url=licence_url, attribution=owner, fetch=fetch, hosts=hosts, country=country,
                  region=region, refresh=120, tags=("traffic",), needs_key=key_name, optional=True,
                  coverage=f"{region} highways")


ONTARIO = _make("on511", "Ontario 511 cameras", "Ontario Ministry of Transportation (511ON)", "https://511on.ca",
                "ONTARIO511_KEY", "CA", "Ontario", "Ontario 511 developer terms",
                "https://511on.ca/developers/doc", ("511on.ca",))
ALBERTA = _make("ab511", "511 Alberta cameras", "Alberta Transportation and Economic Corridors (511 Alberta)",
                "https://511.alberta.ca", "ALBERTA511_KEY", "CA", "Alberta", "511 Alberta developer terms",
                "https://511.alberta.ca/developers/doc", ("511.alberta.ca",))
NEW_YORK = _make("ny511", "511NY cameras", "New York State DOT (511NY)", "https://511ny.org", "NY511_KEY", "US",
                 "New York", "511NY developer terms", "https://511ny.org/developers/doc", ("511ny.org",))
