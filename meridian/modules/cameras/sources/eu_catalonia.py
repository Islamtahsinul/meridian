"""Catalonia (Spain), which the DGT feed does not cover: two Generalitat open datasets.

- ``escat``: Servei Català de Trànsit road cameras ("Càmeres de trànsit a les carreteres de
  Catalunya", WFS XML at gencat.cat/transit/opendata). 35 cameras on the main roads around Barcelona
  and on the A-2 to Lleida, plus one Terrassa city camera; a camera that is off shows SCT's own
  "imatges no disponibles" card. The list repeats some cameras (kept once). It also links one camera
  of Andorra's road service; Andorra's own terms forbid reuse without written consent, so it is left out.
- ``escatfgc``: webcams at the mountain resorts run by FGC (Ferrocarrils de la Generalitat de
  Catalunya: La Molina, Vall de Núria, Espot, Port Ainé, Boí Taüll, Vallter), from FGC's open data
  portal. Only webcams with a still image are kept (the 360° panoramas are web pages). A few records
  carry placeholder coordinates outside Catalonia; they are placed at their resort's other webcams.
"""

from __future__ import annotations

import re
import urllib.parse

from ..common import FetchContext, Source, clean, make_camera, parse_xml, read_bytes, strip_ns, title_case, to_float
from .eu_common import xml_text

# --- Servei Català de Trànsit --------------------------------------------------------------------------
SCT_URL = "https://www.gencat.cat/transit/opendata/cameres.xml"
SCT_LICENCE_URL = "https://administraciodigital.gencat.cat/ca/dades/dades-obertes/informacio-practica/llicencies/"
_SCT_ID = re.compile(r"sctidcam=([A-Za-z0-9_-]{1,40})\.(?:gif|jpe?g|png)", re.I)
SCT_RENDER = "https://mct.gencat.cat/mct2bo/RenderService?sctidcam={id}.gif"
_SCT_NATIVE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_LEFT_OUT = {"andorra"}               # publishers whose terms do not allow reuse


def _ca_title(text: str) -> str:
    """'MONTCADA I REIXAC' -> 'Montcada i Reixac', "L'HOSPITALET" -> "L'Hospitalet" (Catalan place names)."""
    out = title_case(text)
    out = re.sub(r"(?<=\S) (I|De|Del|Dels|La|Les|D'|L')(?= )", lambda m: " " + m.group(1).lower(), out)
    return re.sub(r"\b([LDld])'([a-zà-ÿ])", lambda m: m.group(1).upper() + "'" + m.group(2).upper(), out)


def _sct_image(link: str) -> str:
    """The SCT render service answered over https only when tested (http timed out)."""
    if link.startswith("http://mct.gencat.cat/"):
        return "https://" + link[len("http://"):]
    return link


def parse_sct(data: bytes, src: Source) -> tuple[list[dict], int]:
    root = strip_ns(parse_xml(data))
    rows: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    for cam in root.iter("cameres"):
        link = xml_text(cam, "link", 500)
        font = xml_text(cam, "font")
        if font.lower() in _LEFT_OUT:
            skipped += 1
            continue
        coords = xml_text(cam, ".//coordinates").split(",")
        lon, lat = (coords + [None, None])[:2]
        m = _SCT_ID.search(link)
        if m:
            native = m.group(1)
        else:
            stem = urllib.parse.urlsplit(link).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            native = f"{font.lower()}-{stem}" if stem else ""
        road = xml_text(cam, "carretera")
        town = _ca_title(xml_text(cam, "municipi"))
        km = to_float(xml_text(cam, "pk"))
        name = road or "Camera"
        if km is not None and m:
            name = f"{name} km {km:g}"
        if town:
            name = f"{name}, {town}"
        # SCT stills all come from one render service and differ only in the query string, which the
        # shared build's duplicate check ignores; so they carry no image link and ``resolve_sct``
        # rebuilds it from the camera id (same URL, same allowlist check in the proxy).
        low = link.lower()
        render = (bool(m) and low.split("://", 1)[-1].startswith("mct.gencat.cat/mct2bo/renderservice?")
                  and low.endswith(f"sctidcam={native.lower()}.gif"))
        row = make_camera(src, native, name, lat, lon, image=None if render else _sct_image(link),
                          dynamic=render, road=road if m else None, area=town, tags=("urban",) if not m else ())
        if row and row["id"] not in seen:          # the list repeats some cameras
            seen.add(row["id"])
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_sct(fc: FetchContext) -> list[dict]:
    data = read_bytes(fc.download(SCT_URL, "cameres.xml", min_bytes=2000, timeout=60))
    rows, skipped = parse_sct(data, fc.src)
    fc.skip(skipped)
    return rows


def resolve_sct(cam: dict, net) -> str | None:
    """The render-service still of an SCT camera (no network); None for cameras with their own image."""
    if cam.get("image"):
        return None
    native = str(cam.get("id", "")).split(":", 1)[-1]
    return SCT_RENDER.format(id=native) if _SCT_NATIVE.match(native) else None


SCT = Source(
    id="escat", name="Servei Català de Trànsit cameras", owner="Servei Català de Trànsit, Generalitat de Catalunya",
    url="https://analisi.transparenciacatalunya.cat/d/3tzz-6b9y",
    licence="Llicència oberta d'ús d'informació - Catalunya (free reuse with attribution)",
    licence_url=SCT_LICENCE_URL,
    attribution="Generalitat de Catalunya, Departament d'Interior i Seguretat Pública, Servei Català de Trànsit",
    fetch=fetch_sct, resolve=resolve_sct, hosts=("mct.gencat.cat/mct2bo/", "emap.terrassa.cat/it_terrassa/"),
    country="ES", region="Catalonia", refresh=300, tags=("traffic",), coverage="Main roads in Catalonia",
)

# --- FGC mountain resort webcams ------------------------------------------------------------------------
FGC_URL = "https://dadesobertes.fgc.cat/api/explore/v2.1/catalog/datasets/webcams-actives-tim/exports/json"
# Catalonia's bounding box: anything outside is a placeholder position
_CAT_BOX = (40.4, 0.1, 42.95, 3.4)
_STILL = re.compile(r"\.(?:jpe?g|png|gif)$", re.I)
_TV3 = re.compile(r"^https://api\.pirineu365\.cat/webcam/\d{1,6}$")


def _in_catalonia(lat, lon) -> bool:
    la, lo = to_float(lat), to_float(lon)
    return la is not None and lo is not None and _CAT_BOX[0] <= la <= _CAT_BOX[2] and _CAT_BOX[1] <= lo <= _CAT_BOX[3]


def _fgc_image(rec: dict) -> str | None:
    """The webcam's own still: a direct .jpg link, or TV3's webcam link (it redirects to the still)."""
    url = clean(rec.get("url"), 500)
    path = urllib.parse.urlsplit(url).path if url.startswith("https://") else ""
    if path and _STILL.search(path):
        return url.split("?", 1)[0]
    if _TV3.match(url):
        return url
    return None


def parse_fgc(data, src: Source) -> tuple[list[dict], int]:
    recs = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    # a resort's position, from its webcams with real coordinates (for records with placeholders)
    resort: dict[str, tuple[float, float]] = {}
    for r in recs:
        c = r.get("coordenadas") or {}
        if _in_catalonia(c.get("lat"), c.get("lon")):
            resort.setdefault(clean(r.get("business_unit")), (float(c["lat"]), float(c["lon"])))
    rows: list[dict] = []
    skipped = 0
    for r in recs:
        image = _fgc_image(r)
        if str(r.get("is_active")) not in ("1", "True", "true") or not image:
            skipped += 1
            continue
        c = r.get("coordenadas") or {}
        lat, lon = c.get("lat"), c.get("lon")
        if not _in_catalonia(lat, lon):
            lat, lon = resort.get(clean(r.get("business_unit")), (None, None))
        place = clean(r.get("name_bu"))
        view = clean(r.get("nom_servei"))
        name = f"{place}, {view}" if place and view and place.lower() not in view.lower() else (view or place)
        row = make_camera(src, r.get("id"), name, lat, lon, image=image, area=place, camera_type="webcam")
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch_fgc(fc: FetchContext) -> list[dict]:
    data = fc.json(FGC_URL, "webcams.json", min_bytes=500, timeout=60, headers={"Accept": "application/json"})
    rows, skipped = parse_fgc(data, fc.src)
    fc.skip(skipped)
    return rows


FGC = Source(
    id="escatfgc", name="FGC mountain resort webcams", owner="Ferrocarrils de la Generalitat de Catalunya (FGC)",
    url="https://dadesobertes.fgc.cat/explore/dataset/webcams-actives-tim/", licence="CC BY 4.0",
    licence_url="https://creativecommons.org/licenses/by/4.0/",
    attribution="Ferrocarrils de la Generalitat de Catalunya (FGC), CC BY 4.0",
    fetch=fetch_fgc,
    hosts=("media.turismefgc.cat", "app.projecte4estacions.com/snapshots/", "api.pirineu365.cat/webcam/",
           "statics.3cat.cat/meteo/"),
    country="ES", region="Catalonia", refresh=600, tags=("mountain", "weather"), camera_type="webcam",
    coverage="FGC ski and mountain resorts in the Catalan Pyrenees",
)

SOURCES = [SCT, FGC]
