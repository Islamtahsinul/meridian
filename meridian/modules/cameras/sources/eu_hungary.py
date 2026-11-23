"""Útinform road cameras (Hungary), from Magyar Közút Nonprofit Zrt., the state road operator.

The Útinform traveller-information site lists its camera sites as GeoJSON (``/api/public/webcam/all``):
about 350 sites on motorways and main roads, each with one to four views named after the town the
view looks towards. The latest still of each view sits at a stable CDN address
(``webcamimages/<site>_<view>.jpg``, refreshed every few minutes). Sites the operator has not
published yet, and views without an image for a week, are skipped.

Terms: Útinform's conditions of use allow personal, non-commercial use with Útinform named as the
source; commercial use is not allowed.
"""

from __future__ import annotations

import re
import time

from ..common import FetchContext, Source, clean, make_camera, sanitize_id, to_float, to_int

LIST_URL = "https://www.utinform.hu/api/public/webcam/all"
IMAGE_URL = "https://cdnuiwebcams.utinform.hu/webcamimages/{site}_{view}.jpg"
STALE_S = 7 * 86400
_SITE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# Hungarian words for things common.infer_tags only knows in English
_TAGS = (("border", re.compile(r"határ", re.I)), ("bridge", re.compile(r"\bhíd|hídfő|felüljáró", re.I)),
         ("tunnel", re.compile(r"alagút", re.I)), ("port", re.compile(r"\bkikötő|\bkomp", re.I)))


# views named with a common word rather than a town
_VIEW_WORDS = {"országhatár": "towards the border", "határátkelő": "towards the border crossing",
               "városközpont": "towards the town centre", "centrum": "towards the town centre",
               "központ": "towards the town centre", "vasútállomás": "towards the railway station",
               "híd": "towards the bridge", "kamionparkoló": "truck parking", "jobb oldal": "right-hand view",
               "bal oldal": "left-hand view"}


def view_label(view: str) -> str:
    """'Debrecen' -> 'towards Debrecen', 'Országhatár' -> 'towards the border',
    'Kölesd felé' -> 'towards Kölesd'."""
    view = clean(view)
    if not view:
        return ""
    word = _VIEW_WORDS.get(view.lower())
    if word:
        return word
    if view.lower().endswith(" felé"):                 # "... felé" is Hungarian for "towards ..."
        view = view[:-5].rstrip(" ,")
    return f"towards {view}" if view else ""


def _road(number: str) -> str:
    """'M1' -> 'M1', '42' -> 'Road 42'."""
    number = clean(number).upper()
    if not number:
        return ""
    return number if number.startswith("M") else f"Road {number}"


def parse(data, src: Source, now: float | None = None) -> tuple[list[dict], int]:
    now = time.time() if now is None else now
    rows: list[dict] = []
    skipped = 0
    feats = data.get("features") if isinstance(data, dict) else None
    for feat in feats if isinstance(feats, list) else []:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        views = [w for w in props.get("webcams") or [] if isinstance(w, dict)]
        site = clean(props.get("id"))
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if (not props.get("published") or props.get("active") is False or not _SITE.match(site)
                or len(coords) < 2):
            skipped += len(views) or 1
            continue
        lon, lat = coords[0], coords[1]
        place = clean(props.get("placeName"))
        town = clean(place.split(",", 1)[0])
        road = _road(props.get("roadNumber"))
        for w in views:
            num = to_int(w.get("cameraNum"))
            last = to_float(w.get("lastImage"))
            if num is None or w.get("published") is False or not last or now - last / 1000.0 > STALE_S:
                skipped += 1
                continue
            view = clean(w.get("cameraView"))
            name = place or road or f"Camera {site}"
            if view and view.lower() not in name.lower():
                name = f"{name}, {view_label(view)}"
            desc = clean(w.get("longDescription"), 200)
            text = " ".join([name, desc])
            tags = [tag for tag, rx in _TAGS if rx.search(text)]
            image = IMAGE_URL.format(site=sanitize_id(site), view=num)
            row = make_camera(src, f"{site}_{num}", name, lat, lon, image=image, road=road, area=town,
                              region=clean(props.get("county")), description=desc, tags=tags)
            if row:
                rows.append(row)
            else:
                skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    data = fc.json(LIST_URL, "webcams.json", min_bytes=10_000, timeout=90, headers={"Accept": "application/json"})
    rows, skipped = parse(data, fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="huutinform", name="Útinform road cameras", owner="Magyar Közút Nonprofit Zrt. (Hungarian Public Roads)",
    url="https://www.utinform.hu/",
    licence="Útinform conditions of use: personal, non-commercial use only, with Útinform named as the source",
    licence_url="https://www.utinform.hu/en/static/jogi-nyilatkozat",
    attribution="Útinform, Magyar Közút Nonprofit Zrt.", fetch=fetch,
    hosts=("cdnuiwebcams.utinform.hu/webcamimages/",),
    country="HU", refresh=300, tags=("traffic",), coverage="Hungarian motorways and main roads",
)
