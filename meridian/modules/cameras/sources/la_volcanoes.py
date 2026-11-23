"""Volcano-observatory webcams in Central America, each a fixed "latest picture" link checked by hand
(November 2026):

- ``crovsicori``: OVSICORI-UNA, Costa Rica: Poás (crater, south-west rim, from Chahuites), Irazú and
  Rincón de la Vieja (Sensoria, Curubandé, Gavilán). Turrialba's picture has been empty since
  5 November 2026 and is left out.
- ``gtinsivumeh``: INSIVUMEH, Guatemala: Fuego (south-west and south-east flanks, Barranca Ceniza)
  and Santiaguito.
- ``niineter``: INETER, Nicaragua: Masaya, San Cristóbal (two views) and Telica. Momotombo (no new
  picture since September 2025) and Concepción (a week old) are left out.

None of these observatories publishes its cameras' own positions, so each camera is placed at the
summit of the volcano it watches (positions from the Smithsonian Global Volcanism Program). None
publishes a reuse licence (OVSICORI and INSIVUMEH state "all rights reserved"); the pictures are shown
for personal viewing, with credit, as on the observatories' own pages.
"""

from __future__ import annotations

from ..common import FetchContext, Source
from .ap_common import curated

POAS = (10.200, -84.233)
IRAZU = (9.979, -83.852)
RINCON = (10.830, -85.324)
FUEGO = (14.473, -90.880)
SANTIAGUITO = (14.756, -91.552)
MASAYA = (11.984, -86.161)
SAN_CRISTOBAL = (12.702, -87.004)
TELICA = (12.606, -86.845)
AT_SUMMIT = "Shown at the summit; the camera's own position is not published"

OVSICORI_IMAGE = "https://www.ovsicori.una.ac.cr/images/stories/camaras/{slug}/camara.jpg"
OVSICORI_PAGE = "https://www.ovsicori.una.ac.cr/index.php/camaras"
OVSICORI_CAMS = (
    ("livecraterpoas", "Poás volcano, crater", POAS),
    ("livepoas", "Poás volcano, south-west of the crater", POAS),
    ("livechahuites", "Poás volcano from Chahuites", POAS),
    ("liveirazu", "Irazú volcano", IRAZU),
    ("liverincon", "Rincón de la Vieja volcano from Sensoria", RINCON),
    ("livecurubande", "Rincón de la Vieja volcano from Curubandé", RINCON),
    ("liverincon2", "Rincón de la Vieja volcano from Gavilán", RINCON),
)
INSIVUMEH_IMAGE = "https://geo.insivumeh.gob.gt/{slug}"
INSIVUMEH_PAGE = "https://geo.insivumeh.gob.gt/vulcam"
INSIVUMEH_CAMS = (
    ("fg8", "Fuego volcano, south-west flank", FUEGO),
    ("fg12", "Fuego volcano, south-east flank", FUEGO),
    ("fg14", "Fuego volcano, Barranca Ceniza", FUEGO),
    ("stpj", "Santiaguito volcano", SANTIAGUITO),
)
INETER_IMAGE = "https://www.ineter.gob.ni/webcam/{slug}/image.jpg"
INETER_PAGE = "https://www.ineter.gob.ni/volcanes-en-nicaragua/"
INETER_CAMS = (
    ("v_masaya", "Masaya volcano", MASAYA),
    ("v_sancristobal", "San Cristóbal volcano", SAN_CRISTOBAL),
    ("v_sancris2", "San Cristóbal volcano, second view", SAN_CRISTOBAL),
    ("v_telica2", "Telica volcano", TELICA),
)


def table(cams, image: str, page: str) -> list[dict]:
    return [{"id": slug, "name": name, "lat": at[0], "lon": at[1], "image": image.format(slug=slug), "page": page,
             "description": AT_SUMMIT} for slug, name, at in cams]


def _fetcher(cams, image: str, page: str):
    def fetch(fc: FetchContext) -> list[dict]:
        rows, skipped = curated(fc.src, table(cams, image, page))
        fc.skip(skipped)
        return rows
    return fetch


OVSICORI = Source(
    id="crovsicori", name="OVSICORI volcano cameras", owner="OVSICORI, Universidad Nacional (Costa Rica)",
    url=OVSICORI_PAGE, licence="© OVSICORI-UNA, all rights reserved; shown for personal viewing with credit",
    licence_url=OVSICORI_PAGE, attribution="OVSICORI-UNA", fetch=_fetcher(OVSICORI_CAMS, OVSICORI_IMAGE, OVSICORI_PAGE),
    hosts=("www.ovsicori.una.ac.cr/images/stories/camaras/",), country="CR", refresh=120,
    tags=("volcano", "mountain"), camera_type="webcam", coverage="Poás, Irazú and Rincón de la Vieja, Costa Rica",
)
INSIVUMEH = Source(
    id="gtinsivumeh", name="INSIVUMEH volcano cameras",
    owner="INSIVUMEH (Instituto Nacional de Sismología, Vulcanología, Meteorología e Hidrología), Guatemala",
    url=INSIVUMEH_PAGE, licence="© INSIVUMEH, all rights reserved; shown for personal viewing with credit",
    licence_url=INSIVUMEH_PAGE, attribution="INSIVUMEH",
    fetch=_fetcher(INSIVUMEH_CAMS, INSIVUMEH_IMAGE, INSIVUMEH_PAGE), hosts=("geo.insivumeh.gob.gt",),
    country="GT", refresh=60, tags=("volcano", "mountain"), camera_type="webcam",
    coverage="Fuego and Santiaguito, Guatemala",
)
INETER = Source(
    id="niineter", name="INETER volcano cameras", owner="INETER (Instituto Nicaragüense de Estudios Territoriales)",
    url=INETER_PAGE, licence="No licence stated (INETER); shown for personal viewing with credit",
    licence_url=INETER_PAGE, attribution="INETER Nicaragua", fetch=_fetcher(INETER_CAMS, INETER_IMAGE, INETER_PAGE),
    hosts=("www.ineter.gob.ni/webcam/",), country="NI", refresh=120, tags=("volcano", "mountain"),
    camera_type="webcam", coverage="Masaya, San Cristóbal and Telica, Nicaragua",
)
# OVSICORI and INSIVUMEH state "all rights reserved", so they are not listed by default (the rule
# that excluded Oregon TripCheck). Their adapters are kept for anyone with permission to use them.
SOURCES = [INETER]
