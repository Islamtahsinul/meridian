"""Small helpers shared by the Asia-Pacific, Latin American, African and Middle Eastern camera adapters
(``ap_*``, ``la_*``, ``af_*`` and ``me_*``). No sources live here.

- :func:`bilingual`: "English name (原名)" when a feed gives both, else whichever it has;
- :func:`first`: the first non-empty value among several possible field names (feeds rename fields);
- :func:`features`: (properties, lat, lon) for each point of a GeoJSON FeatureCollection;
- :func:`toward`: the compass bearing from a camera to what it looks at (for curated lists);
- :func:`curated`: rows for a short, hand-checked list of cameras with fixed image links.
"""

from __future__ import annotations

from typing import Any, Iterable

from meridian import geo

from ..common import Source, clean, make_camera, to_float


def bilingual(original: Any, english: Any, limit: int = 140) -> str:
    """'Taipei Main Station (臺北車站)' from the original and an English or romanised name. When one is
    missing, or both say the same thing, the one that is there."""
    orig = clean(original, limit)
    eng = clean(english, limit)
    if not eng or eng.casefold() == orig.casefold():
        return orig
    if not orig or orig.casefold() in eng.casefold():
        return eng
    return clean(f"{eng} ({orig})", limit)


def first(record: dict, *names: str) -> Any:
    """The first value among ``names`` that is present and not blank (None when none is)."""
    if not isinstance(record, dict):
        return None
    for name in names:
        value = record.get(name)
        if value is not None and (not isinstance(value, str) or value.strip()):
            return value
    return None


def features(data: Any) -> Iterable[tuple[dict, float | None, float | None]]:
    """(properties, lat, lon) for every Point feature of a GeoJSON FeatureCollection (others skipped)."""
    feats = data.get("features") if isinstance(data, dict) else None
    for feat in feats or []:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") if isinstance(geom, dict) and geom.get("type") == "Point" else None
        props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
        if feat.get("id") is not None and "id" not in props:
            props = {**props, "id": feat.get("id")}
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            yield props, to_float(coords[1]), to_float(coords[0])
        else:
            yield props, None, None


def toward(lat: float, lon: float, target: tuple[float, float] | None) -> float | None:
    """Bearing in whole degrees from (lat, lon) to ``target`` (None without a target)."""
    if not target:
        return None
    return float(round(geo.bearing_deg(lat, lon, target[0], target[1])))


def curated(src: Source, table: Iterable[dict]) -> tuple[list[dict], int]:
    """Rows for a hand-checked list: each entry has ``id``, ``name``, ``lat``, ``lon``, ``image`` and
    optionally ``page``, ``area``, ``region``, ``country``, ``target`` (what it looks at),
    ``heading``, ``elevation``, ``tags`` and ``description``."""
    rows: list[dict] = []
    skipped = 0
    for c in table:
        heading = c.get("heading")
        if heading is None and c.get("target") and c.get("lat") is not None:
            heading = toward(c["lat"], c["lon"], c["target"])
        row = make_camera(src, c.get("id"), c.get("name"), c.get("lat"), c.get("lon"), image=c.get("image"),
                          page=c.get("page"), area=c.get("area"), region=c.get("region"),
                          country=c.get("country"), heading=heading, elevation=c.get("elevation"),
                          tags=c.get("tags") or (), description=c.get("description"))
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped
