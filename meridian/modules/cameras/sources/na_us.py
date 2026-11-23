"""US keyless city, county and state camera sources:

- City of Kirkland, Washington (city GIS layer). Only Kirkland's own cameras: the layer also lists
  WSDOT's (the WSDOT source has them) and two neighbours' (Bellevue has no picture links; Redmond's
  host did not answer when tested).
- King County, Washington (county GIS layer, active county and city cameras, not WSDOT's). The
  picture links differ only in the query string, so the link is made from the camera's file name
  when the picture is requested (``resolve``, no lookup).
- Baton Rouge, Louisiana: LADOTD cameras listed by the City-Parish open data portal (public domain).
  The list gives no names, only the camera code. With ``LA511_KEY`` set, the Louisiana 511 source
  lists the same links (with names) first and these rows are dropped as duplicates.
- DelDOT, Delaware: live video only (HLS, playable in the browser); DelDOT publishes no stills.
"""

from __future__ import annotations

import re
import urllib.parse

from ..common import FetchContext, Source, arcgis_features, arcgis_point, clean, make_camera, socrata_rows, title_case
from .na_common import template_resolver

# -- Kirkland --------------------------------------------------------------------------------------
KIRKLAND_LAYER = "https://services2.arcgis.com/loGMwowmR0OPlOQb/arcgis/rest/services/TRN_TrafficCams_10032022/FeatureServer/0"
KIRKLAND_IMAGES = "its.kirklandwa.gov"


def parse_kirkland(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for f in features:
        a = f.get("attributes") or {}
        image = clean(a.get("IMAGE_"), 400).split("?", 1)[0]
        if clean(a.get("OWNER")) != "CoK" or (urllib.parse.urlsplit(image).hostname or "").lower() != KIRKLAND_IMAGES:
            skipped += 1
            continue
        lat, lon = arcgis_point(f)
        stem = urllib.parse.urlsplit(image).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]     # TRAFCAM_ID is often empty
        row = make_camera(src, stem, a.get("LOCATION"), lat if lat is not None else a.get("LAT"),
                          lon if lon is not None else a.get("LON"), image=image, area="Kirkland")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_kirkland(fc: FetchContext) -> list[dict]:
    feats = arcgis_features(fc, KIRKLAND_LAYER, where="OWNER='CoK'", out_fields="TRAFCAM_ID,LOCATION,OWNER,IMAGE_,LAT,LON",
                            order_by="OBJECTID", prefix="cams")
    rows, skipped = parse_kirkland(feats, fc.src)
    fc.skip(skipped)
    return rows


KIRKLAND = Source(
    id="uskirkland", name="Kirkland traffic cameras", owner="City of Kirkland, Washington",
    url="https://www.arcgis.com/home/item.html?id=7fc8ba9051684d0aabc228038758c10b",
    licence="City of Kirkland public GIS layer (no licence stated; credit City of Kirkland)",
    licence_url="https://www.arcgis.com/home/item.html?id=7fc8ba9051684d0aabc228038758c10b",
    attribution="City of Kirkland", fetch=fetch_kirkland, hosts=(KIRKLAND_IMAGES,), country="US",
    region="Washington", refresh=120, tags=("traffic", "urban"), coverage="City of Kirkland, east of Seattle",
)

# -- King County -------------------------------------------------------------------------------------
KING_LAYER = "https://services.arcgis.com/Ej0PsM5Aw677QF1W/arcgis/rest/services/TRAFFICCAMERA_POINT_2029/FeatureServer/0"
KING_IMAGE = "https://info.kingcounty.gov/transportation/kcdot/Roads/TrafficCameras/ImageHandler/Handler.ashx?id={id}"
_KING_FILE = r"[A-Za-z0-9_.-]{1,80}\.jpe?g"


def parse_king(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for f in features:
        a = f.get("attributes") or {}
        parts = urllib.parse.urlsplit(clean(a.get("ImageURL"), 400))
        file_id = (urllib.parse.parse_qs(parts.query).get("id") or [""])[0]
        if (clean(a.get("CurrentStatus")).lower() != "active" or clean(a.get("Owner")).upper() == "WSDOT"
                or (parts.hostname or "").lower() != "info.kingcounty.gov" or not re.fullmatch(_KING_FILE, file_id)):
            skipped += 1
            continue
        lat, lon = arcgis_point(f)
        owner = clean(a.get("Owner"))
        region = clean(a.get("CamRegion"))
        row = make_camera(src, file_id, a.get("Location"), lat, lon, dynamic=True,
                          area=clean(a.get("Jurisdiction")) or (f"{region} King County" if region else "King County"),
                          description=f"Camera owned by {owner}" if owner and owner != "King County" else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_king(fc: FetchContext) -> list[dict]:
    feats = arcgis_features(fc, KING_LAYER, where="CurrentStatus='Active'",
                            out_fields="AssetID,Location,ImageURL,CamRegion,Owner,Jurisdiction,CurrentStatus",
                            order_by="OBJECTID", prefix="cams")
    rows, skipped = parse_king(feats, fc.src)
    fc.skip(skipped)
    return rows


KING_COUNTY = Source(
    id="uskingcounty", name="King County traffic cameras", owner="King County Department of Local Services, Roads",
    url="https://www.arcgis.com/home/item.html?id=488167659ce544f3a525d0eb0bb9301c",
    licence="King County GIS data terms and conditions (credit King County)",
    licence_url="https://kingcounty.gov/en/dept/kcit/data-information-services/gis-center/about/terms-conditions-copyrights",
    attribution="King County", fetch=fetch_king, resolve=template_resolver(KING_IMAGE, _KING_FILE),
    hosts=("info.kingcounty.gov/transportation/kcdot/",), country="US", region="Washington",
    refresh=120, tags=("traffic",), coverage="Unincorporated King County and nearby cities, Washington",
)

# -- Baton Rouge ---------------------------------------------------------------------------------------
BRLA_URL = "https://data.brla.gov/resource/6z6u-ts44.json"
BRLA_IMAGES = "https://511la.org/map/Cctv/"


def parse_brla(rows_in: list[dict], src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for r in rows_in:
        image = clean(r.get("image_view") if isinstance(r.get("image_view"), str) else (r.get("image_view") or {}).get("url"), 400)
        coords = (r.get("the_geom") or {}).get("coordinates") or [None, None]
        if not image.startswith(BRLA_IMAGES):
            skipped += 1
            continue
        code = image[len(BRLA_IMAGES):].split("--", 1)[0]
        row = make_camera(src, image[len(BRLA_IMAGES):], f"LADOTD camera {code.upper()}", coords[1] if len(coords) > 1 else None,
                          coords[0], image=image, area="Baton Rouge")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_brla(fc: FetchContext) -> list[dict]:
    rows, skipped = parse_brla(socrata_rows(fc, BRLA_URL, prefix="cams"), fc.src)
    fc.skip(skipped)
    return rows


BATON_ROUGE = Source(
    id="usbatonrouge", name="Baton Rouge traffic cameras", owner="City of Baton Rouge and Parish of East Baton Rouge (LADOTD cameras)",
    url="https://data.brla.gov/d/6z6u-ts44", licence="Public domain (Open Data BR)",
    licence_url="https://data.brla.gov/d/6z6u-ts44", attribution="LADOTD via Open Data BR", fetch=fetch_brla,
    hosts=("511la.org",), country="US", region="Louisiana", refresh=120, tags=("traffic",),
    coverage="Greater Baton Rouge, Louisiana",
)

# -- DelDOT ---------------------------------------------------------------------------------------------
DELDOT_URL = "https://tmc.deldot.gov/json/videocamera.json"
DELDOT_VIDEO = "video.deldot.gov"
_ROUTE = re.compile(r"\b(de|us|sr|i|rt)(\s*-?\s*)(\d)", re.I)


def deldot_name(title: str) -> str:
    """'DE 52 @ DE 100' -> 'DE 52 at DE 100' (sentence words title-cased, route prefixes kept)."""
    text = title_case(clean(title)).replace(" @ ", " at ")
    return _ROUTE.sub(lambda m: m.group(1).upper() + m.group(2) + m.group(3), text)


def parse_deldot(data, src: Source) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    cams = data.get("videoCameras") if isinstance(data, dict) else None
    for cam in cams or []:
        if not isinstance(cam, dict):
            continue
        stream = clean((cam.get("urls") or {}).get("m3u8s"), 400)
        if (cam.get("enabled") is False or clean(cam.get("status")).lower() != "active"
                or (urllib.parse.urlsplit(stream).hostname or "").lower() != DELDOT_VIDEO):
            skipped += 1
            continue
        county = clean(cam.get("county"))
        row = make_camera(src, cam.get("id"), deldot_name(cam.get("title")), cam.get("lat"), cam.get("lon"),
                          stream=stream, stream_kind="hls", area=f"{county} County" if county else None,
                          page="https://deldot.gov/map/index.shtml")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_deldot(fc: FetchContext) -> list[dict]:
    rows, skipped = parse_deldot(fc.json(DELDOT_URL, "cameras.json", min_bytes=1000, timeout=60), fc.src)
    fc.skip(skipped)
    return rows


DELDOT = Source(
    id="usdeldot", name="DelDOT live traffic video", owner="Delaware Department of Transportation",
    url="https://deldot.gov/map/index.shtml", licence="DelDOT public traffic camera feed (no licence stated; credit DelDOT)",
    licence_url="https://deldot.gov/", attribution="DelDOT", fetch=fetch_deldot, hosts=(DELDOT_VIDEO,), country="US",
    region="Delaware", refresh=60, tags=("traffic",), stream_cors=True,
    coverage="Delaware highways (live video only, no still pictures)",
)

SOURCES = [KIRKLAND, KING_COUNTY, BATON_ROUGE, DELDOT]
