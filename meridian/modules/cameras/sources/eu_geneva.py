"""Geneva (Switzerland) motorway cameras of the canton's transport office: the open dataset
"Infomobilité - caméras de surveillance du trafic" (SITG, opendata.swiss, open use). Nine sites on
the motorway and main roads around Geneva, from Vengeron to the Bardonnex customs post; each still
shows both directions, renewed about once a minute.
"""

from __future__ import annotations

import re

from ..common import FetchContext, Source, arcgis_features, arcgis_point, clean, http_url, make_camera

LAYER = "https://vector.sitg.ge.ch/arcgis/rest/services/INFOMOB_CAMERA/FeatureServer/0"
IMAGE_PREFIX = "https://app2.ge.ch/tercameras/"
_FILE = re.compile(r"/tercameras/(CAM_\d{1,4})\.jpe?g$", re.I)
_TOWARDS = {"tolausanne": "Lausanne", "tofrance": "France", "togeneva": "Geneva", "togeneve": "Geneva"}


def _towards(value) -> str:
    text = clean(value)
    return _TOWARDS.get(text.lower().replace(" ", ""), text)


def parse(features: list[dict], src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    seen: set[str] = set()
    for f in features or []:
        a = (f.get("attributes") or {}) if isinstance(f, dict) else {}
        lat, lon = arcgis_point(f)
        name = clean(a.get("NOM"))
        views = [(a.get("IMAGE_ALLER"), a.get("DIRECTION_ALLER")), (a.get("IMAGE_RETOUR"), a.get("DIRECTION_RETOUR"))]
        heads = [_towards(d) for _, d in views if clean(d)]
        for image, _direction in views:
            url = http_url(image)
            m = _FILE.search(url or "")
            if not url or not url.startswith(IMAGE_PREFIX) or not m or url in seen:
                continue
            seen.add(url)
            both = " and ".join(dict.fromkeys(heads))
            tags = ("border",) if "douane" in name.lower() else ()
            row = make_camera(src, m.group(1).lower(), name or m.group(1), lat, lon, image=url, area="Geneva",
                              description=f"Towards {both}" if both else None, tags=tags)
            if row:
                rows.append(row)
            else:
                skipped += 1
        if not any(http_url(i) for i, _ in views):
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    feats = arcgis_features(fc, LAYER, out_fields="NOM,DIRECTION_ALLER,DIRECTION_RETOUR,IMAGE_ALLER,IMAGE_RETOUR",
                            order_by="OBJECTID", prefix="cameras", max_pages=2)
    rows, skipped = parse(feats, fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="chgeneve", name="Geneva motorway cameras", owner="Office cantonal des transports, République et canton de Genève",
    url="https://opendata.swiss/en/dataset/infomob_camera-canton-geneve",
    licence="Open use (opendata.swiss terms_open), SITG Geneva", licence_url="https://opendata.swiss/en/terms-of-use",
    attribution="État de Genève, Office cantonal des transports (SITG)", fetch=fetch, hosts=("app2.ge.ch/tercameras/",),
    country="CH", region="Geneva", refresh=120, tags=("traffic",), coverage="Motorway and main roads around Geneva",
)
