"""DGAC Chile aerodrome cameras: about 95 cameras at 49 Chilean aerodromes, from Arica to Puerto
Williams, Easter Island (Mataveri) and King George Island in Antarctica (Teniente Rodolfo Marsh), a
new picture every 10 minutes. Published for pilots on the Dirección General de Aeronáutica Civil's
aeronautical information site (aipchile.dgac.gob.cl/camara).

The camera list is that site's public table; each camera's page lists its recent pictures with their
times, and the newest one is looked up when the picture is requested (the page is remembered for
5 minutes). Aerodrome positions come from OurAirports (public domain). The site states no licence;
pictures are shown for personal viewing with credit to DGAC. The site is slow (2-4 s a request).
"""

from __future__ import annotations

import html
import re
import urllib.parse

from ..common import FetchContext, Source, clean, decode_text, make_camera, read_bytes

BASE = "https://aipchile.dgac.gob.cl"
LIST_URL = f"{BASE}/camara/"
PAGE_URL = f"{BASE}/camara/show/id/{{id}}"
_ROW = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_AERODROME = re.compile(r'/camara_ubicacion/show/designador/([A-Z0-9]{4})"\s+title="([^"]*)"[^>]*>.*?</a>\s*'
                        r"<div>([^<]*)</div>", re.S)
_CAMERA = re.compile(r'camara_box\s+(operativa|inoperativa)\b.*?href="/camara/show/id/(\d+)"[^>]*>([^<]*)</a>', re.S)
_ENTRY = re.compile(r"\{\s*'fecha'\s*:\s*'(\d+)'\s*,\s*'imagen'\s*:\s*'([^']+)'\s*\}")
_FLAGS = re.compile(r"\s*\((?:PUB|PVT|PRIV|MIL|INTL|ALTN INTL|ALTN)\)", re.I)
_ICAO = re.compile(r"\s*\([A-Z0-9]{4}\)\s*$")

DIRECTIONS = {"norte": (0, "north"), "n-este": (45, "north-east"), "este": (90, "east"),
              "s-este": (135, "south-east"), "sur": (180, "south"), "s-oeste": (225, "south-west"),
              "oeste": (270, "west"), "n-oeste": (315, "north-west")}

# ICAO: (lat, lon, country, elevation m), from OurAirports (public domain), checked November 2026
AERODROMES = {
    "SCAP": (-43.61149, -71.80545, "CL", 242),
    "SCAR": (-18.34850, -70.33870, "CL", 51),
    "SCAS": (-45.39920, -72.67030, "CL", 9),
    "SCAT": (-27.26120, -70.77920, "CL", 204),
    "SCBA": (-45.91597, -71.68950, "CL", 525),
    "SCCC": (-46.58310, -71.68632, "CL", 326),
    "SCCF": (-22.49820, -68.90360, "CL", 2299),
    "SCCH": (-36.58250, -72.03140, "CL", 151),
    "SCCI": (-53.00260, -70.85460, "CL", 42),
    "SCCY": (-45.59420, -72.10610, "CL", 311),
    "SCDA": (-20.53627, -70.18137, "CL", 47),
    "SCEL": (-33.39025, -70.79470, "CL", 474),
    "SCFA": (-23.44527, -70.44523, "CL", 139),
    "SCFM": (-53.25370, -70.31920, "CL", 32),
    "SCFT": (-43.18888, -71.85077, "CL", 350),
    "SCGE": (-37.40170, -72.42540, "CL", 114),
    "SCGZ": (-54.93110, -67.62630, "CL", 27),
    "SCHR": (-47.24359, -72.58815, "CL", 196),
    "SCIC": (-34.96667, -71.21639, "CL", 220),
    "SCIE": (-36.77235, -73.06283, "CL", 8),
    "SCIH": (-45.14912, -73.51312, "CL", 35),
    "SCIL": (-31.57780, -71.11080, "CL", 433),
    "SCIM": (-38.38500, -73.86890, "CL", 16),
    "SCIP": (-27.16541, -109.42103, "CL", 69),
    "SCJO": (-40.61120, -73.06100, "CL", 57),
    "SCLJ": (-43.97811, -72.41335, "CL", 37),
    "SCMA": (-43.78763, -72.95099, "CL", 3),
    "SCMK": (-43.89500, -73.73889, "CL", 11),
    "SCNK": (-31.87306, -70.61833, "CL", 1220),
    "SCNT": (-51.67067, -72.52908, "CL", 66),
    "SCOH": (-48.46921, -72.56432, "CL", 274),
    "SCON": (-43.13670, -73.63500, "CL", 5),
    "SCPC": (-39.29280, -71.91590, "CL", 260),
    "SCPD": (-33.11857, -70.68307, "CL", 670),
    "SCPF": (-41.45569, -72.91870, "CL", 112),
    "SCPQ": (-42.34039, -73.71569, "CL", 161),
    "SCQP": (-38.92590, -72.65150, "CL", 101),
    "SCRD": (-33.06810, -71.55750, "CL", 335),
    "SCRG": (-34.17370, -70.77570, "CL", 491),
    "SCRM": (-62.19103, -58.98669, "AQ", 45),
    "SCSE": (-29.91620, -71.19950, "CL", 147),
    "SCSN": (-33.65640, -71.61440, "CL", 75),
    "SCSO": (-34.12083, -71.52722, "CL", 119),
    "SCTB": (-33.45630, -70.54670, "CL", 649),
    "SCTE": (-41.44309, -73.09407, "CL", 90),
    "SCTL": (-35.37780, -71.60170, "CL", 113),
    "SCTN": (-42.78194, -72.83500, "CL", 29),
    "SCVD": (-39.65000, -73.08610, "CL", 18),
    "SCVM": (-32.94960, -71.47860, "CL", 141),
}


def aerodrome_name(title: str) -> str:
    """'Arturo Merino Benitez (INTL) (PUB) (SCEL)' -> 'Arturo Merino Benitez'."""
    return clean(_ICAO.sub("", _FLAGS.sub("", html.unescape(title or ""))))


def parse(text: str, src: Source) -> tuple[list[dict], int, set[str]]:
    """(rows, skipped, ICAO codes without a known position)."""
    rows: list[dict] = []
    skipped = 0
    unknown: set[str] = set()
    for tr in _ROW.finditer(text or ""):
        block = tr.group(0)
        ad = _AERODROME.search(block)
        if not ad:
            continue
        icao, title, city = ad.group(1), ad.group(2), clean(html.unescape(ad.group(3)))
        where = AERODROMES.get(icao)
        for status, cam_id, label in _CAMERA.findall(block):
            if status != "operativa":
                skipped += 1
                continue
            if where is None:
                unknown.add(icao)
                skipped += 1
                continue
            label = clean(html.unescape(label))
            heading, facing = DIRECTIONS.get(label.lower(), (None, label.lower()))
            name = f"{aerodrome_name(title)} aerodrome ({icao})" + (f", looking {facing}" if facing else "")
            row = make_camera(src, cam_id, name, where[0], where[1], dynamic=True, page=PAGE_URL.format(id=cam_id),
                              area=city or None, country=where[2], heading=heading, direction=label or None,
                              elevation=where[3], description=f"Aerodrome weather camera, {icao}")
            if row:
                rows.append(row)
            else:
                skipped += 1
    return rows, skipped, unknown


def fetch(fc: FetchContext) -> list[dict]:
    path = fc.download(LIST_URL, "camaras.html", min_bytes=5000, timeout=90)
    rows, skipped, unknown = parse(decode_text(read_bytes(path, limit=10_000_000)), fc.src)
    fc.skip(skipped)
    if unknown:
        fc.notes.append("aerodromes without a known position: " + ", ".join(sorted(unknown)))
    return rows


def newest(text: str) -> str | None:
    """The newest picture listed on a camera page (by its 'fecha' time, not its file name)."""
    best = None
    for stamp, url in _ENTRY.findall(text or ""):
        if best is None or int(stamp) > best[0]:
            best = (int(stamp), url)
    if best is None:
        return None
    parts = urllib.parse.urlsplit(best[1])
    if parts.hostname != "aipchile.dgac.gob.cl" or not parts.path.startswith("/camara/ftp/"):
        return None
    return best[1]


def resolve(cam: dict, net) -> str | None:
    native = str(cam.get("id", "")).split(":", 1)[-1]
    if not native.isdigit():
        return None
    return newest(net.get_text(PAGE_URL.format(id=native), ttl=300, timeout=14, retries=0))


SOURCE = Source(
    id="cldgac", name="DGAC Chile aerodrome cameras", owner="Dirección General de Aeronáutica Civil (DGAC), Chile",
    url=LIST_URL, licence="No licence stated (DGAC aeronautical information site); personal viewing with credit",
    licence_url=LIST_URL, attribution="DGAC Chile (AIP Chile)", fetch=fetch, resolve=resolve,
    hosts=("aipchile.dgac.gob.cl/camara/ftp/",), country="CL", refresh=600, tags=("airport", "weather"),
    camera_type="webcam", coverage="49 Chilean aerodromes, Easter Island and King George Island (Antarctica)",
)
