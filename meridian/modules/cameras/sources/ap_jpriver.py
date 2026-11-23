"""MLIT river cameras (国土交通省 川の防災情報): river-monitoring cameras across Japan run by MLIT
river offices, prefectures and cities, as published on www.river.go.jp. Names are in Japanese.

Terms: the site's usage rules follow the Government Standard Terms of Use 2.0 (compatible with
CC BY 4.0; credit "出典：国土交通省 川の防災情報"). They also ask that tools do not collect data from
the site periodically (regular collectors should use the paid FRICS feed), so this adapter keeps
its footprint to what a person browsing the map would cause:

- the camera directory (one small GeoJSON per prefecture, about 0.5 MB in all) is downloaded once,
  one file every 2 seconds, and never refreshed automatically (only by a forced rebuild:
  ``python server.py --build cameras --force``);
- pictures are fetched only when you open a camera (card, grid or "What's here"), never in the
  background. Cameras of the MLIT CCTV system have their picture link in a per-camera record, which
  is looked up at that moment (and remembered for a day).

When a camera has no current picture the site answers with a Japanese "image could not be
retrieved" placeholder, which is shown as is.
"""

from __future__ import annotations

from meridian.net import NetError

from ..common import FetchContext, Source, clean, make_camera, to_int
from .ap_common import features

PREFS_URL = "https://www.river.go.jp/kawabou/file/files/map/pref/prefarea.json"
LIST_URL = "https://www.river.go.jp/kawabou/file/gjson/scam/{pref}.json"
RECORD_URL = "https://www.river.go.jp/kawabou/file/files/master/obs/scam/{id}.json"
IMAGE_URL = "https://cam.river.go.jp/cam/now/{id}.jpg"
DIRECTORY_MAX_AGE_H = 24 * 365 * 20   # effectively never: the site asks not to be collected on a schedule
SPACING_S = 2.0
DIRECT_SYSTEMS = (1, 3)        # these systems' pictures sit at cam/now/{id}.jpg; others need the record

PREFECTURES = ("Hokkaido", "Aomori", "Iwate", "Miyagi", "Akita", "Yamagata", "Fukushima", "Ibaraki", "Tochigi",
               "Gunma", "Saitama", "Chiba", "Tokyo", "Kanagawa", "Niigata", "Toyama", "Ishikawa", "Fukui",
               "Yamanashi", "Nagano", "Gifu", "Shizuoka", "Aichi", "Mie", "Shiga", "Kyoto", "Osaka", "Hyogo",
               "Nara", "Wakayama", "Tottori", "Shimane", "Okayama", "Hiroshima", "Yamaguchi", "Tokushima",
               "Kagawa", "Ehime", "Kochi", "Fukuoka", "Saga", "Nagasaki", "Kumamoto", "Oita", "Miyazaki",
               "Kagoshima", "Okinawa")


def prefecture_of(pref_cd) -> str:
    """1301 -> 'Tokyo'; 102 (a part of Hokkaido) -> 'Hokkaido'."""
    n = to_int(pref_cd)
    if n is None:
        return ""
    idx = n // 100
    return PREFECTURES[idx - 1] if 1 <= idx <= len(PREFECTURES) else ""


def pref_codes(data) -> list[int]:
    prefs = data.get("prefs") if isinstance(data, dict) else None
    out = []
    for p in prefs or []:
        n = to_int(p.get("prefCd")) if isinstance(p, dict) else None
        if n is not None and 100 <= n <= 4799 and n not in out:
            out.append(n)
    return out


def parse(data, pref_cd, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    region = prefecture_of(pref_cd) or None
    for props, lat, lon in features(data):
        cid = to_int(props.get("id"))
        if cid is None or to_int(props.get("pause")) == 1:
            skipped += 1
            continue
        direct = to_int(props.get("sys_id")) in DIRECT_SYSTEMS
        row = make_camera(src, cid, props.get("name"), lat, lon, image=IMAGE_URL.format(id=cid) if direct else None,
                          dynamic=not direct, region=region, description="River camera")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    prefs = pref_codes(fc.json(PREFS_URL, "prefarea.json", min_bytes=200, timeout=60,
                               max_age_hours=DIRECTORY_MAX_AGE_H, min_interval=SPACING_S))
    if not prefs:
        raise RuntimeError("the prefecture index lists no prefectures")
    rows: list[dict] = []
    missing = 0
    for pref in prefs:
        try:
            data = fc.json(LIST_URL.format(pref=pref), f"scam-{pref}.json", min_bytes=20, timeout=60,
                           max_age_hours=DIRECTORY_MAX_AGE_H, min_interval=SPACING_S)
        except NetError as err:
            if err.status == 404:          # some index entries have no camera file (Hokkaido's 101)
                continue
            missing += 1
            continue
        except ValueError:
            missing += 1
            continue
        got, skipped = parse(data, pref, fc.src)
        rows.extend(got)
        fc.skip(skipped)
    if missing:
        fc.notes.append(f"{missing} prefecture lists could not be read")
    return rows


def resolve(cam: dict, net) -> str | None:
    """Systems 1 and 3: the fixed picture link. Others: the link in the camera's record (cached a day)."""
    if cam.get("image"):
        return cam["image"]
    native = str(cam.get("id", "")).split(":", 1)[-1]
    if not native.isdigit():
        return None
    data = net.get_json(RECORD_URL.format(id=native), ttl=86400, timeout=10, retries=0)
    info = data.get("obsInfo") if isinstance(data, dict) else None
    url = clean((info or {}).get("currProvUrl"), 400)
    return url if url.startswith("https://cam.river.go.jp/cam/now/") else None


SOURCE = Source(
    id="jpriver", name="Japan river cameras (MLIT)", owner="Ministry of Land, Infrastructure, Transport and Tourism (MLIT)",
    url="https://www.river.go.jp/", licence="Government Standard Terms of Use 2.0 (Japan; compatible with CC BY 4.0)",
    licence_url="https://www.river.go.jp/kawabou/kwb_apend/html/caution.html",
    attribution="出典：国土交通省 川の防災情報 (MLIT river information, river.go.jp)", fetch=fetch, resolve=resolve,
    hosts=("cam.river.go.jp/cam/now/",), country="JP", refresh=600, tags=("river",), camera_type="webcam",
    coverage="Rivers across Japan (pictures fetched only when opened, as the site asks)",
)
