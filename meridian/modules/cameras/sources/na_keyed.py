"""Keyed North American sources that are not on the IBI 511 API, each with a free key:

- WSDOT (Washington State), Traveler Information API, free access code;
- OHGO (Ohio DOT) public API, free API key (sent in a header);
- City of Ottawa traffic cameras: the camera list is open, but the open-data image service needs a
  free certificate. The certificate is only ever added server-side, when the image is requested
  (``resolve``), so it never reaches the camera list, the browser or the logs.
"""

from __future__ import annotations

import re
import urllib.parse

from ..common import FetchContext, NeedsKey, Source, clean, make_camera, to_float
from .na_common import declared_key

# -- WSDOT ----------------------------------------------------------------------------------------
WSDOT_KEY = "WSDOT_KEY"
WSDOT_SIGNUP = "https://wsdot.wa.gov/traffic/api/"
WSDOT_URL = "https://www.wsdot.wa.gov/Traffic/api/HighwayCameras/HighwayCamerasREST.svc/GetCamerasAsJson"
WSDOT_IMAGES = "images.wsdot.wa.gov"
_WSDOT_REGIONS = {"NW": "Northwest region", "NC": "North Central region", "OL": "Olympic region",
                  "SC": "South Central region", "SW": "Southwest region", "ER": "Eastern region"}
_WA_INTERSTATES = {5, 82, 90, 182, 205, 405, 705}
_WA_US_ROUTES = {2, 12, 97, 101, 195, 197, 395, 730}


def wsdot_road(name) -> str | None:
    """WSDOT road numbers ("005", "520") as signed routes ("I-5", "SR 520")."""
    text = clean(name)
    if not text.isdigit():
        return text or None
    n = int(text)
    return f"I-{n}" if n in _WA_INTERSTATES else f"US {n}" if n in _WA_US_ROUTES else f"SR {n}"


def parse_wsdot(data, src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for cam in data if isinstance(data, list) else []:
        if not isinstance(cam, dict):
            continue
        image = clean(cam.get("ImageURL"), 400)
        host = (urllib.parse.urlsplit(image).hostname or "").lower()
        if cam.get("IsActive") is False or host != WSDOT_IMAGES:
            skipped += 1          # inactive, or another agency's picture (Oregon's are not reusable)
            continue
        loc = cam.get("CameraLocation") or {}
        lat = to_float(cam.get("DisplayLatitude")) or to_float(loc.get("Latitude"))
        lon = to_float(cam.get("DisplayLongitude")) or to_float(loc.get("Longitude"))
        owner = clean(cam.get("CameraOwner"))
        row = make_camera(src, cam.get("CameraID"), cam.get("Title") or loc.get("Description"), lat, lon,
                          image=image.replace("http://", "https://", 1), road=wsdot_road(loc.get("RoadName")),
                          area=_WSDOT_REGIONS.get(clean(cam.get("Region")).upper()),
                          direction=loc.get("Direction") if clean(loc.get("Direction")).upper() not in ("B", "") else None,
                          description=f"Camera owned by {owner}" if owner and "wsdot" not in owner.lower() else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_wsdot(fc: FetchContext) -> list[dict]:
    code = declared_key(fc.config, WSDOT_KEY, "WSDOT Traveler Information API access code: adds Washington "
                        "State highway cameras.", WSDOT_SIGNUP)
    if not code:
        raise NeedsKey(WSDOT_KEY)
    data = fc.json(f"{WSDOT_URL}?{urllib.parse.urlencode({'AccessCode': code})}", "cameras.json", min_bytes=100,
                   timeout=60)
    rows, skipped = parse_wsdot(data, fc.src)
    fc.skip(skipped)
    return rows


WSDOT = Source(
    id="uswawsdot", name="WSDOT highway cameras", owner="Washington State Department of Transportation",
    url=WSDOT_SIGNUP, licence="WSDOT Traveler Information API terms (free access code)",
    licence_url=WSDOT_SIGNUP, attribution="WSDOT", fetch=fetch_wsdot, hosts=(WSDOT_IMAGES,), country="US",
    region="Washington", refresh=120, tags=("traffic",), needs_key=WSDOT_KEY, optional=True,
    coverage="Washington State highways, passes and ferry terminals",
)

# -- OHGO (Ohio) --------------------------------------------------------------------------------------
OHGO_KEY = "OHGO_KEY"
OHGO_SIGNUP = "https://publicapi.ohgo.com/accounts/registration"
OHGO_URL = "https://publicapi.ohgo.com/api/v1/cameras"
_WORDS = {"N": "north", "S": "south", "E": "east", "W": "west", "NB": "north", "SB": "south", "EB": "east",
          "WB": "west", "NE": "north-east", "NW": "north-west", "SE": "south-east", "SW": "south-west"}


def _pick(d: dict, name: str):
    """OHGO answers in camelCase; its documentation shows PascalCase. Accept both."""
    return d.get(name) if name in d else d.get(name[:1].upper() + name[1:])


def parse_ohgo(data, src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    results = _pick(data, "results") if isinstance(data, dict) else data
    for site in results if isinstance(results, list) else []:
        if not isinstance(site, dict):
            continue
        views = [v for v in (_pick(site, "cameraViews") or []) if isinstance(v, dict)]
        location = clean(_pick(site, "location"))
        for i, view in enumerate(views):
            direction = clean(_pick(view, "direction")).upper()
            word = _WORDS.get(direction)
            name = f"{location}, facing {word}" if word else (f"{location}, view {i + 1}" if len(views) > 1 else location)
            native = f"{_pick(site, 'id')}-{i}" if len(views) > 1 else _pick(site, "id")
            row = make_camera(src, native, name, _pick(site, "latitude"), _pick(site, "longitude"),
                              image=_pick(view, "largeUrl") or _pick(view, "smallUrl"),
                              road=_pick(view, "mainRoute"), direction=direction if word else None,
                              description=_pick(site, "description"))
            if row:
                rows.append(row)
            else:
                skipped += 1
    return rows, skipped


def fetch_ohgo(fc: FetchContext) -> list[dict]:
    key = declared_key(fc.config, OHGO_KEY, "OHGO public API key (Ohio DOT): adds Ohio traffic cameras.", OHGO_SIGNUP)
    if not key:
        raise NeedsKey(OHGO_KEY)
    data = fc.json(f"{OHGO_URL}?page-all=true", "cameras.json", min_bytes=100, timeout=90,
                   headers={"Authorization": f"APIKEY {key}", "Accept": "application/json"})
    rows, skipped = parse_ohgo(data, fc.src)
    fc.skip(skipped)
    return rows


OHGO = Source(
    id="usohgo", name="OHGO cameras", owner="Ohio Department of Transportation (OHGO)", url="https://publicapi.ohgo.com/",
    licence="OHGO public API terms of use (free key)", licence_url="https://publicapi.ohgo.com/docs/terms-of-use",
    attribution="Ohio Department of Transportation (OHGO)", fetch=fetch_ohgo,
    hosts=("*.dot.state.oh.us", "*.ohgo.com", "*.ohio.gov"), country="US", region="Ohio", refresh=60,
    tags=("traffic",), needs_key=OHGO_KEY, optional=True, coverage="Ohio interstates and city freeways",
)

# -- City of Ottawa -----------------------------------------------------------------------------------
OTTAWA_KEY = "OTTAWA_CAMERA_KEY"
OTTAWA_SIGNUP = "https://traffic.ottawa.ca/en/traffic-map-data-lists-and-resources/certificate-request-form"
OTTAWA_LIST = "https://traffic.ottawa.ca/map/service/camera"
OTTAWA_IMAGE = "https://traffic.ottawa.ca/opendata/camera"
_NUMBER = re.compile(r"\d{1,5}")


def parse_ottawa(data, src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    cams = data.get("cameras") if isinstance(data, dict) else data
    for cam in cams if isinstance(cams, list) else []:
        if not isinstance(cam, dict):
            continue
        number = clean(cam.get("camera_number"), 8)
        if clean(cam.get("cameraOwner")).upper() != "CITY" or not _NUMBER.fullmatch(number):
            skipped += 1          # MTO cameras belong to Ontario 511
            continue
        row = make_camera(src, number, cam.get("name") or cam.get("name_french"), cam.get("latitude"),
                          cam.get("longitude"), dynamic=True, area="Ottawa")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_ottawa(fc: FetchContext) -> list[dict]:
    if not declared_key(fc.config, OTTAWA_KEY, "City of Ottawa traffic camera certificate (free): adds Ottawa "
                        "city traffic cameras.", OTTAWA_SIGNUP):
        raise NeedsKey(OTTAWA_KEY)
    rows, skipped = parse_ottawa(fc.json(OTTAWA_LIST, "cameras.json", min_bytes=100, timeout=60), fc.src)
    fc.skip(skipped)
    return rows


def resolve_ottawa(cam: dict, net) -> str | None:
    """The image link with the certificate, made only at request time (never stored or sent out)."""
    number = str(cam.get("id", "")).split(":", 1)[-1]
    if not _NUMBER.fullmatch(number):
        return None
    cert = declared_key(getattr(net, "config", None), OTTAWA_KEY)
    if not cert:
        return None
    return f"{OTTAWA_IMAGE}?{urllib.parse.urlencode({'c': number, 'certificate': cert})}"


OTTAWA = Source(
    id="caottawa", name="Ottawa traffic cameras", owner="City of Ottawa",
    url="https://traffic.ottawa.ca/en/traffic-map-data-lists-and-resources/about-the-data",
    licence="City of Ottawa open data terms (third-party use allowed; free certificate for images)",
    licence_url="https://traffic.ottawa.ca/en/traffic-map-data-lists-and-resources/faq",
    attribution="City of Ottawa", fetch=fetch_ottawa, resolve=resolve_ottawa, hosts=("traffic.ottawa.ca/opendata/",),
    country="CA", region="Ontario", refresh=120, tags=("traffic", "urban"), needs_key=OTTAWA_KEY, optional=True,
    coverage="City of Ottawa",
)

SOURCES = [WSDOT, OHGO, OTTAWA]

