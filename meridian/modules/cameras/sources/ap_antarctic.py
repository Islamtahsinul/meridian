"""Antarctic research-station webcams: the Australian Antarctic Division's Casey, Davis and Mawson
stations and the British Antarctic Survey's Rothera station. Each station publishes one stable
"latest" picture; the short list is checked by hand (November 2026). Macquarie Island's camera
(stale since October 2026), US stations (pictures years old), Halley VI, Bird Island and King Edward
Point (image scripts failing) are left out.
"""

from __future__ import annotations

from ..common import FetchContext, Source
from .ap_common import curated

AAD_PAGE = "https://www.antarctica.gov.au/antarctic-operations/webcams/{station}/"
AAD_IMAGE = "https://images.antarctica.gov.au/webcams/{station}/latest.jpg"

AAD_STATIONS = (
    {"id": "casey", "name": "Casey station", "lat": -66.2823, "lon": 110.5267},
    {"id": "davis", "name": "Davis station", "lat": -68.5766, "lon": 77.9675},
    {"id": "mawson", "name": "Mawson station", "lat": -67.6033, "lon": 62.8742},
)
BAS_STATIONS = (
    {"id": "rothera", "name": "Rothera research station", "lat": -67.5686, "lon": -68.1297,
     "image": "https://files.bas.ac.uk/webcams/pub/rothera/latest/latest.jpg",
     "page": "https://www.bas.ac.uk/data/our-data/images/webcams/rothera-webcam/",
     "area": "Adelaide Island"},
)


def aad_table() -> list[dict]:
    return [dict(s, image=AAD_IMAGE.format(station=s["id"]), page=AAD_PAGE.format(station=s["id"]))
            for s in AAD_STATIONS]


def fetch_aad(fc: FetchContext) -> list[dict]:
    rows, skipped = curated(fc.src, aad_table())
    fc.skip(skipped)
    return rows


def fetch_bas(fc: FetchContext) -> list[dict]:
    rows, skipped = curated(fc.src, BAS_STATIONS)
    fc.skip(skipped)
    return rows


AAD = Source(
    id="aqaad", name="Australian Antarctic station webcams", owner="Australian Antarctic Division",
    url="https://www.antarctica.gov.au/antarctic-operations/webcams/",
    licence="© Commonwealth of Australia: unaltered display for personal, non-commercial use",
    licence_url="https://www.antarctica.gov.au/about-us/copyright/", attribution="Australian Antarctic Division",
    fetch=fetch_aad, hosts=("images.antarctica.gov.au",), country="AQ", region="Australian Antarctic Territory",
    refresh=600, tags=("polar", "weather", "coastal"), camera_type="webcam",
    coverage="Casey, Davis and Mawson stations, East Antarctica",
)
BAS = Source(
    id="aqbas", name="British Antarctic Survey webcams", owner="British Antarctic Survey (NERC)",
    url="https://www.bas.ac.uk/data/our-data/images/webcams/",
    licence="© NERC / British Antarctic Survey: personal, non-commercial use",
    licence_url="https://www.bas.ac.uk/about-this-site/copyright-statement/", attribution="British Antarctic Survey",
    fetch=fetch_bas, hosts=("files.bas.ac.uk",), country="AQ", region="British Antarctic Territory",
    refresh=900, tags=("polar", "weather", "coastal"), camera_type="webcam",
    coverage="Rothera research station, Antarctic Peninsula",
)
SOURCES = [AAD, BAS]
