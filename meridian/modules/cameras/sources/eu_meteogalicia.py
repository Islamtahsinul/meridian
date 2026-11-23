"""MeteoGalicia (the Xunta de Galicia's weather service) webcams: about 30 cameras on the Galician
coast, rías and hills. The list is the "JSON de cámaras de MeteoGalicia" open dataset (Xunta de
Galicia open data catalogue, CC BY-SA 4.0); each camera's latest picture is ``.../ultima.jpg``."""

from __future__ import annotations

import re
import time
from datetime import datetime

from ..common import FetchContext, Source, clean, http_url, make_camera

URL = "https://servizos.meteogalicia.gal/mgrss/observacion/jsonCamaras.action"
IMAGE_PREFIX = "https://www.meteogalicia.gal/datosred/camaras/"
_FOLDER = re.compile(r"/datosred/camaras/[A-Za-z0-9_-]{1,40}/([A-Za-z0-9_-]{1,60})/ultima\.jpg$")
STALE_DAYS = 3
# Galician place words the shared tag rules do not know
_TAGS = [("port", re.compile(r"\bporto\b", re.I)), ("coastal", re.compile(r"\b(praia|r[ií]a|cabo|illa)\b", re.I)),
         ("mountain", re.compile(r"\b(serra|monte|alto|pico)\b", re.I))]


def _updated(value) -> float | None:
    """'2026-11-23T12:24:00' (local time) -> epoch seconds, near enough for a staleness check."""
    try:
        return datetime.strptime(clean(value, 40)[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
    except ValueError:
        return None


def parse(data, src: Source, now: float | None = None) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    now = time.time() if now is None else now
    cams = data.get("listaCamaras") if isinstance(data, dict) else None
    for cam in cams if isinstance(cams, list) else []:
        if not isinstance(cam, dict):
            continue
        image = http_url(cam.get("imaxeCamara"))
        folder = _FOLDER.search(image or "")
        when = _updated(cam.get("dataUltimaAct"))
        if not folder or not image.startswith(IMAGE_PREFIX) or (when is not None and now - when > STALE_DAYS * 86400):
            skipped += 1
            continue
        name = clean(cam.get("nomeCamara"))
        town = clean(cam.get("concello"))
        tags = [t for t, rx in _TAGS if rx.search(name)]
        # the picture's folder ("Onsplaya", "Onspuerto"): two views of one station share its number
        row = make_camera(src, folder.group(1), name or town, cam.get("lat"), cam.get("lon"), image=image,
                          area=town, region=cam.get("provincia"), tags=tags,
                          description=f"MeteoGalicia camera {clean(cam.get('identificador'))}")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    rows, skipped = parse(fc.json(URL, "camaras.json", min_bytes=500, timeout=60), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="esmeteogalicia", name="MeteoGalicia webcams", owner="MeteoGalicia, Xunta de Galicia",
    url="https://abertos.xunta.gal/catalogo/medio-abiente/-/dataset/0385/json-camaras-meteogalicia",
    licence="CC BY-SA 4.0 (Xunta de Galicia open data)", licence_url="https://creativecommons.org/licenses/by-sa/4.0/",
    attribution="MeteoGalicia, Xunta de Galicia, CC BY-SA 4.0", fetch=fetch, hosts=("www.meteogalicia.gal/datosred/camaras/",),
    country="ES", region="Galicia", refresh=300, tags=("weather",), camera_type="webcam",
    coverage="Galician coast, rías and hills",
)
