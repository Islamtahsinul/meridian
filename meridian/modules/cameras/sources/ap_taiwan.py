"""Taiwan government cameras from the national open data platform (data.gov.tw), all under the Open
Government Data License, version 1.0:

- ``twthb``: Highway Bureau (MOTC) provincial-highway and expressway cameras, about 2,350, from the
  daily XML in the MOTC traffic data standard (dataset 29817). Each has a live snapshot link.
- ``twwra``: Water Resources Agency river cameras, about 130 (dataset 36687).
- ``twdebris``: Agency of Rural Development and Soil and Water Conservation debris-flow monitoring
  cameras in the mountains, about 60 (dataset 9327). Its server is slow (often 5-8 s a picture).

Names are in Chinese; where the feed allows it an English road name and kilometre are added
("Provincial Highway 9 km 328.6 (台9線328K+600)") and county names are given in English.
The river and debris-flow servers give every camera's picture from one script with the camera in
the query string; the build treats image links that differ only in their query as duplicates, so
those rows carry no image link and the link is made (debris flows) or looked up in the cached list
(rivers) when the picture is requested.

The Freeway Bureau's own list (tisvcloud.freeway.gov.tw) does not answer from outside Taiwan, and
freeway and city cameras need a TDX key, so they are not included.
"""

from __future__ import annotations

import re
import urllib.parse

from ..common import FetchContext, Source, clean, make_camera, parse_xml, read_bytes, strip_ns, to_float
from .ap_common import bilingual

OGDL = "Open Government Data License, version 1.0 (Taiwan)"
OGDL_URL = "https://data.gov.tw/license"
GZIP = {"Accept-Encoding": "gzip"}        # the highway list is 1.7 MB plain, 120 KB gzipped

COUNTIES = {
    "臺北市": "Taipei City", "新北市": "New Taipei City", "桃園市": "Taoyuan City", "臺中市": "Taichung City",
    "臺南市": "Tainan City", "高雄市": "Kaohsiung City", "基隆市": "Keelung City", "新竹市": "Hsinchu City",
    "嘉義市": "Chiayi City", "新竹縣": "Hsinchu County", "苗栗縣": "Miaoli County", "彰化縣": "Changhua County",
    "南投縣": "Nantou County", "雲林縣": "Yunlin County", "嘉義縣": "Chiayi County", "屏東縣": "Pingtung County",
    "宜蘭縣": "Yilan County", "花蓮縣": "Hualien County", "臺東縣": "Taitung County", "澎湖縣": "Penghu County",
    "金門縣": "Kinmen County", "連江縣": "Lienchiang County",
}


def county_en(name: str) -> str:
    """'台中市' or '臺中市' -> 'Taichung City' ('' when unknown)."""
    return COUNTIES.get(clean(name).replace("台", "臺"), "")


# -- Highway Bureau -------------------------------------------------------------------------------
THB_URL = "https://cctv-maintain.thb.gov.tw/opendataCCTVs.xml"
_ROAD = re.compile(r"^台(\d+)([甲乙丙丁戊己庚辛壬癸]?)線$")
_BRANCH = dict(zip("甲乙丙丁戊己庚辛壬癸", "ABCDEFGHIJ"))
_MILE = re.compile(r"^(\d+)\s*[Kk]\s*\+?\s*(\d{1,3})")
_DIR_SUFFIX = re.compile(r"\s*\((?:N|S|E|W|NE|NW|SE|SW)\)\s*$")
HIDDEN = "預設不顯示"


def road_en(name: str, road_class: str = "") -> str:
    """'台9線' -> 'Provincial Highway 9', '台2丙線' -> 'Provincial Highway 2C', class 1 -> 'Expressway 62'."""
    m = _ROAD.match(clean(name))
    if not m:
        return ""
    number = f"{int(m.group(1))}{_BRANCH.get(m.group(2), '')}"
    return f"Expressway {number}" if clean(road_class) == "1" else f"Provincial Highway {number}"


def km_of(mile: str) -> float | None:
    """'328K+600' -> 328.6; '6k+660' -> 6.66."""
    m = _MILE.match(clean(mile))
    if not m:
        return None
    return round(int(m.group(1)) + int(m.group(2).ljust(3, "0")) / 1000.0, 3)


def parse_thb(data: bytes, src: Source) -> tuple[list[dict], int]:
    root = strip_ns(parse_xml(data))
    rows: list[dict] = []
    skipped = 0
    for cam in root.iter("CCTV"):
        text = lambda tag: clean(cam.findtext(tag))       # noqa: E731
        if HIDDEN in text("SurveillanceDescription"):      # the bureau's own "hidden by default" (a sign camera)
            skipped += 1
            continue
        road = road_en(text("RoadName"), text("RoadClass"))
        km = km_of(text("LocationMile"))
        english = f"{road} km {km:g}" if road and km is not None else road
        original = _DIR_SUFFIX.sub("", text("SurveillanceDescription"))
        direction = text("RoadDirection")
        row = make_camera(src, text("CCTVID"), bilingual(original, english), text("PositionLat"),
                          text("PositionLon"), image=text("VideoImageURL"), road=road or text("RoadName"),
                          direction=direction if direction in ("N", "S", "E", "W", "NE", "NW", "SE", "SW") else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_thb(fc: FetchContext) -> list[dict]:
    path = fc.download(THB_URL, "cctvs.xml", min_bytes=5000, timeout=120, headers=GZIP)
    rows, skipped = parse_thb(read_bytes(path), fc.src)
    fc.skip(skipped)
    return rows


THB = Source(
    id="twthb", name="Taiwan provincial highway cameras", owner="Highway Bureau, MOTC (Taiwan)",
    url="https://data.gov.tw/dataset/29817", licence=OGDL, licence_url=OGDL_URL,
    attribution="Highway Bureau, MOTC (Taiwan), Open Government Data License v1.0", fetch=fetch_thb,
    hosts=("*.thb.gov.tw",), country="TW", refresh=60, tags=("traffic",),
    coverage="Taiwan's provincial highways and expressways (slow host: about 2 s a picture)",
)


# -- Water Resources Agency -------------------------------------------------------------------------
WRA_URL = "https://opendata.wra.gov.tw/api/v2/f71b74eb-cbe5-42c6-8be5-7500450e7db0?format=JSON"
WRA_IMAGE_PREFIX = "https://fmg.wra.gov.tw/109wraweb/getImage.aspx?"


def parse_wra(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict) or clean(r.get("status")) == "0":
            skipped += 1
            continue
        if clean(r.get("coordinate")).upper() not in ("", "WGS84"):
            skipped += 1
            continue
        basin = clean(r.get("basinname"))
        tributary = clean(r.get("tributary"))
        river = tributary if tributary and tributary != basin else basin
        if not wra_image(r.get("imageurl")):
            skipped += 1
            continue
        row = make_camera(src, r.get("cameraid"), r.get("cameraname") or r.get("videosurveillancestationname"),
                          r.get("latitude_4326"), r.get("longitude_4326"), dynamic=True,
                          region=county_en(r.get("countiesandcitieswherethemonitoringpointsarelocated")) or None,
                          area=clean(r.get("administrativedistrictwherethemonitoringpointislocated")) or None,
                          road=river or None, description=f"River camera, {basin} basin" if basin else None)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def wra_image(url) -> str | None:
    """The picture link if it is the agency's image script, else None."""
    text = clean(url, 600)
    return text if text.startswith(WRA_IMAGE_PREFIX) else None


def resolve_wra(cam: dict, net) -> str | None:
    """The camera's picture link from the river camera list (kept in memory for an hour)."""
    want = str(cam.get("id", "")).split(":", 1)[-1]
    data = net.get_json(WRA_URL, ttl=3600, timeout=15, retries=0, headers=GZIP)
    for r in data if isinstance(data, list) else []:
        if isinstance(r, dict) and clean(r.get("cameraid")) == want:
            return wra_image(r.get("imageurl"))
    return None


def fetch_wra(fc: FetchContext) -> list[dict]:
    rows, skipped = parse_wra(fc.json(WRA_URL, "river-cameras.json", min_bytes=500, timeout=90, headers=GZIP), fc.src)
    fc.skip(skipped)
    return rows


WRA = Source(
    id="twwra", name="Taiwan river cameras", owner="Water Resources Agency, MOEA (Taiwan)",
    url="https://data.gov.tw/dataset/36687", licence=OGDL, licence_url=OGDL_URL,
    attribution="Water Resources Agency (Taiwan), Open Government Data License v1.0", fetch=fetch_wra,
    resolve=resolve_wra,
    hosts=("fmg.wra.gov.tw/109wraweb/",), country="TW", refresh=120, tags=("river",), camera_type="webcam",
    coverage="Rivers across Taiwan",
)


# -- Debris-flow monitoring ---------------------------------------------------------------------------
DEBRIS_URL = "https://data.moa.gov.tw/Service/OpenData/DataFileService.aspx?UnitId=111&IsTransData=1"
DEBRIS_IMAGE = "https://dfm.ardswc.gov.tw/debrisFinal/showone.asp?SID={sid}&CID={cid}"


def parse_debris(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict):
            continue
        link = clean(r.get("影像連結網址"), 400)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        sid = (query.get("StationID") or [""])[0]
        cid = (query.get("CCDId") or [""])[0]
        if not (sid.isdigit() and cid.isdigit()):
            skipped += 1
            continue
        row = make_camera(src, f"{sid}-{cid}", r.get("攝影機名稱"), to_float(r.get("緯度")), to_float(r.get("經度")),
                          dynamic=True, page=link,
                          region=county_en(r.get("縣市")) or None, area=clean(r.get("行政區")) or None,
                          description=r.get("架設或拍攝地點"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def resolve_debris(cam: dict, net) -> str | None:
    """'twdebris:7-2' -> the picture of station 7, camera 2 (no lookup needed)."""
    sid, _, cid = str(cam.get("id", "")).split(":", 1)[-1].partition("-")
    return DEBRIS_IMAGE.format(sid=sid, cid=cid) if sid.isdigit() and cid.isdigit() else None


def fetch_debris(fc: FetchContext) -> list[dict]:
    rows, skipped = parse_debris(fc.json(DEBRIS_URL, "debris-cameras.json", min_bytes=500, timeout=90), fc.src)
    fc.skip(skipped)
    return rows


DEBRIS = Source(
    id="twdebris", name="Taiwan debris-flow monitoring cameras",
    owner="Agency of Rural Development and Soil and Water Conservation, MOA (Taiwan)",
    url="https://data.gov.tw/dataset/9327", licence=OGDL, licence_url=OGDL_URL,
    attribution="ARDSWC, Ministry of Agriculture (Taiwan), Open Government Data License v1.0", fetch=fetch_debris,
    resolve=resolve_debris,
    # host only: the allowlist compares path prefixes in lower case, and this path is "/debrisFinal/"
    hosts=("dfm.ardswc.gov.tw",), country="TW", refresh=300, tags=("mountain", "river"),
    camera_type="webcam", optional=True,
    coverage="Debris-flow streams in Taiwan's mountains (slow host: 2-7 s a picture, sometimes over the 10 s limit)",
)

SOURCES = [THB, WRA, DEBRIS]
