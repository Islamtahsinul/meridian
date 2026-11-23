"""Japan Meteorological Agency volcano monitoring cameras: about 95 cameras on Japan's active
volcanoes (Sakurajima, Aso, Asama, Fuji, Kirishima, the Izu islands...), a new frame every 2 minutes.

The camera list is the GeoJSON behind JMA's volcano camera map. Each camera's page lists its recent
frames (named by their time, with varying seconds, so they cannot be worked out in advance): the
pages are read once a week to keep only cameras that are sending pictures and to learn their
interval (one small request per camera, about one a second, within a time budget), and a camera's
page is read again when its picture is requested, for the newest frame (remembered 90 s). JMA
content is under the Public Data License 1.0 (compatible with CC BY 4.0).
"""

from __future__ import annotations

import datetime as dt
import re
import time

from meridian.net import NetError

from ..common import FetchContext, Source, clean, decode_text, make_camera, read_bytes, sanitize_id
from .ap_common import bilingual, features

LIST_URL = "https://www.data.jma.go.jp/vois/data/obs/volcam/param/geojson/camicon.geojson"
PAGE_URL = "https://www.data.jma.go.jp/vois/data/obs/volcam/volcam.php?VC={code}"
IMAGE_URL = "https://www.data.jma.go.jp/vois/data/obs/camera/{folder}/{stamp}.jpg"
JST = dt.timezone(dt.timedelta(hours=9))
PAGE_MAX_AGE_H = 7 * 24
PAGE_BUDGET_S = 150.0
PAGE_SPACING_S = 1.0
_FRAME = re.compile(r'file_array\[\d+\]\s*=\s*"\.\./camera/([0-9A-Za-z_]+)/(\d{14})\.jpg"')
_TAG = re.compile(r"<[^>]+>")
_RUBY_READING = re.compile(r"<(rt|rp)>.*?</\1>", re.S)       # furigana: '<ruby><rb>車帰</rb><rt>くるまがえり</rt></ruby>'
_CODE = re.compile(r"^\d{5}$")

# JMA's English names for the volcanoes in the list (keyed by the Japanese name)
VOLCANOES = {
    "アトサヌプリ": "Atosanupuri", "雌阿寒岳": "Meakandake", "大雪山": "Taisetsuzan", "十勝岳": "Tokachidake",
    "樽前山": "Tarumaesan", "倶多楽": "Kuttara", "有珠山": "Usuzan", "北海道駒ヶ岳": "Hokkaido-Komagatake",
    "恵山": "Esan", "岩木山": "Iwakisan", "八甲田山": "Hakkodasan", "十和田": "Towada", "秋田焼山": "Akita-Yakeyama",
    "岩手山": "Iwatesan", "秋田駒ヶ岳": "Akita-Komagatake", "鳥海山": "Chokaisan", "栗駒山": "Kurikomayama",
    "蔵王山": "Zaozan", "吾妻山": "Azumayama", "安達太良山": "Adatarayama", "磐梯山": "Bandaisan",
    "那須岳": "Nasudake", "日光白根山": "Nikko-Shiranesan", "草津白根山": "Kusatsu-Shiranesan",
    "浅間山": "Asamayama", "新潟焼山": "Niigata-Yakeyama", "弥陀ヶ原": "Midagahara", "焼岳": "Yakedake",
    "乗鞍岳": "Norikuradake", "御嶽山": "Ontakesan", "白山": "Hakusan", "富士山": "Mount Fuji",
    "箱根山": "Hakoneyama", "伊豆東部火山群": "Izu-Tobu volcanoes", "伊豆大島": "Izu-Oshima", "新島": "Niijima",
    "神津島": "Kozushima", "三宅島": "Miyakejima", "八丈島": "Hachijojima", "青ヶ島": "Aogashima",
    "西之島": "Nishinoshima", "硫黄島": "Ioto", "鶴見岳・伽藍岳": "Tsurumidake and Garandake", "九重山": "Kujusan",
    "阿蘇山": "Asosan", "雲仙岳": "Unzendake", "霧島山": "Kirishimayama", "桜島": "Sakurajima",
    "薩摩硫黄島": "Satsuma-Iojima", "口永良部島": "Kuchinoerabujima", "中之島": "Nakanoshima",
    "諏訪之瀬島": "Suwanosejima",
}


def split_name(name: str) -> tuple[str, str]:
    """'阿蘇山 草千里' -> ('阿蘇山', '草千里'); HTML tags and furigana readings in the feed are dropped."""
    text = clean(_TAG.sub("", _RUBY_READING.sub("", name or ""))).replace("　", " ")
    volcano, _, site = text.partition(" ")
    return volcano, site.strip()


def frames_of(page: str) -> tuple[str, list[str]]:
    """(picture folder, frame stamps newest first) from a camera page."""
    found = _FRAME.findall(page or "")
    if not found:
        return "", []
    folder = found[0][0]
    return folder, sorted({stamp for f, stamp in found if f == folder}, reverse=True)


def interval_of(stamps: list[str]) -> int:
    """The camera's frame interval in seconds (the smallest gap between listed frames; 120 by default)."""
    times = sorted(parse_stamp(s) for s in stamps if parse_stamp(s))
    gaps = [int((b - a).total_seconds()) for a, b in zip(times, times[1:]) if b > a]
    return max(60, min(3600, min(gaps))) if gaps else 120


def parse_stamp(stamp: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=JST)
    except (TypeError, ValueError):
        return None


def parse(data, pages: dict[str, str], src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for props, lat, lon in features(data):
        code = clean(props.get("code"))
        folder, stamps = frames_of(pages.get(code, ""))
        if not _CODE.match(code) or not folder:
            skipped += 1
            continue
        volcano, site = split_name(props.get("name"))
        english = VOLCANOES.get(volcano, "")
        name = bilingual(f"{volcano} {site}".strip(), english)          # 'Asosan (阿蘇山 草千里)'
        row = make_camera(src, code, name, lat, lon, dynamic=True, page=PAGE_URL.format(code=code),
                          area=english or volcano, refresh=interval_of(stamps),
                          description=f"Volcano camera, {english or volcano}")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def _load_pages(fc: FetchContext, codes: list[str]) -> tuple[dict[str, str], int]:
    pages: dict[str, str] = {}
    todo: list[str] = []
    now = time.time()
    for code in codes:
        path = fc.raw_path(f"pages/{sanitize_id(code)}.html")
        if path.exists():
            try:
                pages[code] = decode_text(read_bytes(path, limit=2_000_000))
            except (OSError, ValueError):
                pass
        if code not in pages or fc.force or now - path.stat().st_mtime > PAGE_MAX_AGE_H * 3600:
            todo.append(code)
    todo.sort(key=lambda c: c in pages)                # cameras never seen first
    deadline = time.monotonic() + min(PAGE_BUDGET_S, max(10.0, fc.time_left() - 30))
    fetched = 0
    for code in todo:
        if time.monotonic() > deadline:
            break
        try:
            path = fc.download(PAGE_URL.format(code=code), f"pages/{sanitize_id(code)}.html", min_bytes=500,
                               timeout=30, max_age_hours=PAGE_MAX_AGE_H / 0.9, min_interval=PAGE_SPACING_S,
                               force=True)
            pages[code] = decode_text(read_bytes(path, limit=2_000_000))
            fetched += 1
        except (NetError, OSError, ValueError):
            continue
    return pages, fetched


def fetch(fc: FetchContext) -> list[dict]:
    data = fc.json(LIST_URL, "camicon.geojson", min_bytes=500, timeout=60)
    codes = [clean(p.get("code")) for p, _, _ in features(data)]
    pages, fetched = _load_pages(fc, [c for c in codes if _CODE.match(c)])
    missing = len(codes) - len(pages)
    if fetched or missing:
        fc.notes.append(f"camera pages: {len(pages)} of {len(codes)} cached, {fetched} refreshed"
                        + (f", {missing} still to read" if missing else ""))
    rows, skipped = parse(data, pages, fc.src)
    fc.skip(skipped)
    return rows


def newest(page: str) -> str | None:
    """The newest frame listed on a camera page."""
    folder, stamps = frames_of(page)
    return IMAGE_URL.format(folder=folder, stamp=stamps[0]) if folder and stamps else None


def resolve(cam: dict, net) -> str | None:
    code = str(cam.get("id", "")).split(":", 1)[-1]
    if not _CODE.match(code):
        return None
    return newest(net.get_text(PAGE_URL.format(code=code), ttl=90, timeout=10, retries=0))


SOURCE = Source(
    id="jpvolcano", name="JMA volcano cameras", owner="Japan Meteorological Agency",
    url="https://www.data.jma.go.jp/vois/data/obs/volcam/index.html",
    licence="Public Data License 1.0 (Japan; compatible with CC BY 4.0)",
    licence_url="https://www.jma.go.jp/jma/kishou/info/coment.html",
    attribution="出典：気象庁ホームページ (Japan Meteorological Agency)", fetch=fetch, resolve=resolve,
    hosts=("www.data.jma.go.jp/vois/data/obs/camera/",), country="JP", refresh=120,
    tags=("volcano", "mountain"), camera_type="webcam", coverage="Japan's monitored active volcanoes",
)
