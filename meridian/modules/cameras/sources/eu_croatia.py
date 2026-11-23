"""Croatia: motorway cameras of Hrvatske autoceste (HAC, with the Bina Istra concession cameras it
relays), from the Croatian National Access Point (promet-info.hr, run by Hrvatske ceste): the "HAC
Cameras" DATEX II 2.3 dataset, about 390 cameras on the A1 to A11.

The NAP gives the data free of charge, but only to registered users: register at
https://www.promet-info.hr/en/registration and ask for the "HAC Cameras" dataset. Put the NAP login
in ``CROATIA_NAP_KEY`` as ``email:password``; it is sent as HTTP Basic authentication (one of the
two mechanisms the dataset page lists) to b2b.promet-info.hr only and never written to disk. The
still images themselves are public links on HAC's and Bina Istra's camera servers.
"""

from __future__ import annotations

import base64
import re

from ..common import FetchContext, NeedsKey, Source, clean, make_camera, parse_xml, read_bytes, strip_ns
from .eu_common import datex_values, pick_lang, xml_text

KEY = "CROATIA_NAP_KEY"
DATA_URL = "https://b2b.promet-info.hr/dc/b2b.hac.cameras"
DATASET_URL = "https://www.promet-info.hr/en/datasets_details?id=504c9423-f558-4cbf-84e4-3ac9f0d892b1"
RAW_FILE = "hac_cameras.xml"
LANGS = ("en", "hr")

# "A1 5 DU 049+914  Čvor Novigrad VK01": road, section, direction code, chainage, place, camera
_TITLE = re.compile(r"^(?P<road>A\d{1,2})\s+\d+\s+(?P<dir>\S+)\s+(?P<km>\d{1,3})\+(?P<m>\d{3})\s+(?P<place>.+?)\s+VK\s?(?P<n>\d+)$")
_CCTV = re.compile(r"^CCTV\s*\d*\s*-?\s*", re.I)           # "CCTV29 - Čvor Vodnjan Jug" -> "Čvor Vodnjan Jug"
_GENERIC = {"prometna kamera", "traffic camera", "kamera"}


def credentials(fc: FetchContext) -> tuple[str, str]:
    """(user, password) from CROATIA_NAP_KEY ('email:password'); NeedsKey when unset."""
    raw = fc.key(KEY)
    if not raw:
        raise NeedsKey(KEY)
    user, sep, password = raw.partition(":")
    if not sep or not user.strip() or not password:
        raise RuntimeError(f"{KEY} must look like email:password (your promet-info.hr login)")
    return user.strip(), password


def name_parts(title: str, description: str) -> tuple[str, str, str]:
    """(name, road, place) from HAC's technical title, e.g. 'A1 5 DU 049+914  Čvor Novigrad VK01'
    -> ('Čvor Novigrad, km 49.9, camera 1', 'A1', 'Čvor Novigrad')."""
    title = clean(title)
    m = _TITLE.match(title)
    if m:
        place = clean(m.group("place"))
        km = int(m.group("km")) + int(m.group("m")) / 1000.0
        return f"{place}, km {km:.1f}, camera {int(m.group('n'))}", m.group("road"), place
    place = clean(_CCTV.sub("", title)) or clean(description)
    if description and description.lower() not in _GENERIC and description.lower() not in place.lower():
        place = f"{place}, {description}" if place else description
    return place, "", place


def _tags(text: str) -> list[str]:
    """Tags from Croatian words the shared rules do not know."""
    low = text.lower()
    tags = []
    if re.search(r"\b(most|vijadukt)\b", low):
        tags.append("bridge")
    if re.search(r"\b(granični prijelaz|gp)\b", low):
        tags.append("border")
    return tags


def parse(data: bytes, src: Source) -> tuple[list[dict], int]:
    root = strip_ns(parse_xml(data))
    rows: list[dict] = []
    skipped = 0
    for cont in root.iter("predefinedLocationContainer"):
        rec = cont.find(".//trafficCameraRecord")
        if rec is None:
            continue
        visibility = xml_text(rec, "visibility")
        if visibility and visibility != "noRestrictions":     # the publisher limits this camera
            skipped += 1
            continue
        native = (xml_text(rec, "cameraId") or cont.get("id") or "").replace("CAMERA:", "", 1).replace(":", "-")
        title = pick_lang(datex_values(rec.find("cameraTitle")), LANGS)
        desc = pick_lang(datex_values(rec.find("cameraDescription")), LANGS)
        region = " ".join(w[:1].upper() + w[1:] for w in pick_lang(datex_values(rec.find("regionName")), LANGS).split())
        name, road, place = name_parts(title, desc)
        road = road or xml_text(cont, ".//linearElementIdentifier")
        lat = xml_text(cont, ".//locationForDisplay/latitude") or xml_text(cont, ".//pointCoordinates/latitude")
        lon = xml_text(cont, ".//locationForDisplay/longitude") or xml_text(cont, ".//pointCoordinates/longitude")
        row = make_camera(src, native, name, lat, lon, image=xml_text(rec, "stillImageUrl", 2000), road=road,
                          area=place, region=region, description=title, tags=_tags(f"{title} {desc}"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    _unique_names(rows)
    return rows, skipped


def _unique_names(rows: list[dict]) -> None:
    """Cameras on the two carriageways can share place, km and number: those get HAC's carriageway
    code from the title ('Čvor Bisko, km 389.7, camera 1, DU'); any left over get ' (2)', ' (3)'."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["name"]] = counts.get(row["name"], 0) + 1
    seen: dict[str, int] = {}
    for row in rows:
        if counts[row["name"]] > 1:
            m = _TITLE.match(row.get("description") or "")
            if m:
                row["name"] = f"{row['name']}, {m.group('dir')}"
        n = seen[row["name"]] = seen.get(row["name"], 0) + 1
        if n > 1:
            row["name"] = f"{row['name']} ({n})"


def fetch(fc: FetchContext) -> list[dict]:
    user, password = credentials(fc)                 # NeedsKey before any request
    auth = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    path = fc.download(DATA_URL, RAW_FILE, headers={"Authorization": f"Basic {auth}", "Accept": "application/xml"},
                       min_bytes=500, timeout=90)
    rows, skipped = parse(read_bytes(path), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="hrhac", name="HAC motorway cameras", owner="Hrvatske autoceste (HAC), via the Croatian NAP (promet-info.hr)",
    url=DATASET_URL, licence="Croatian NAP licence, free of charge for registered users",
    licence_url=DATASET_URL, attribution="Hrvatske autoceste (HAC) via promet-info.hr", fetch=fetch,
    hosts=("*.hac.hr", "kamere.binaistra.com"), country="HR", refresh=180, tags=("traffic",),
    needs_key=KEY, optional=True, coverage="Croatian motorways A1 to A11, including the Istrian Y",
)
