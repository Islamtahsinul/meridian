"""The browser's map file (``cameras.geojson``) and the display-only positions behind it.

The map file has to stay small with 100,000+ cameras, so each point carries only what the map
needs to draw and filter it; names, places and everything else come from the API on demand
(``/api/cameras/labels`` for street-zoom names, ``/api/cameras/camera/{id}`` on click).

Per-feature properties (short keys, defaults left out):

- ``id``  camera id (``<source>:<native id>``), the only way back to the camera;
- ``k``   1 live video stream, 2 page only (absent: a still image, or a still plus a recorded clip);
- ``t``   1 webcam (scenic, weather, city, nature) (absent: a road or traffic camera);
- ``g``   letters for the tags the "Show" filter uses (see :data:`FILTER_TAGS`), when any;
- ``h``   viewing direction in whole degrees, when known.

Co-located views (several cameras on one mast) are fanned out a few metres on the map so each
can be clicked; :func:`fan_out` gives those positions and one shared label per mast.
"""

from __future__ import annotations

import math
import os
from typing import Iterable

TAG_CODES = {"traffic": "t", "urban": "u", "coastal": "c", "mountain": "m", "weather": "w", "port": "p",
             "airport": "a", "bridge": "b", "tunnel": "n", "border": "d", "wildfire": "f", "river": "r",
             "volcano": "v", "polar": "i"}
# tags the map's "Show" filter uses; the others are only in the API
FILTER_TAGS = ("urban", "coastal", "port", "mountain", "weather", "wildfire", "volcano", "polar")
FAN_M = 12.0     # co-located views are drawn this far apart on the map (display only)
LIVE_STREAMS = ("hls", "mjpeg")


def tag_code(tags: Iterable[str]) -> str:
    return "".join(sorted(TAG_CODES[t] for t in tags if t in TAG_CODES))


def filter_code(tags: Iterable[str]) -> str:
    return "".join(sorted(TAG_CODES[t] for t in tags if t in FILTER_TAGS))


def kind_code(row: dict) -> int:
    """0 still image (or a still with a recorded clip), 1 live stream, 2 page only."""
    kind = row.get("kind")
    return 1 if kind in LIVE_STREAMS else (2 if kind == "page" else 0)


def is_webcam(row: dict) -> bool:
    return row.get("type") == "webcam"


def group_label(names: list[str]) -> str:
    """The shared start of several views' names, cut at a word: 'Hobson / Woodridge, facing north',
    'Hobson / Woodridge, facing east' -> 'Hobson / Woodridge'."""
    prefix = os.path.commonprefix(names)
    if prefix != names[0]:
        cut = max(prefix.rfind(" "), prefix.rfind(","), prefix.rfind("-"), prefix.rfind("("))
        prefix = prefix[:cut] if cut > 0 else ""
    prefix = prefix.rstrip(" ,-/(:")
    if prefix.lower().endswith(" facing"):
        prefix = prefix[:-7].rstrip(" ,-")
    return prefix if len(prefix) >= 4 else names[0]


def fan_out(rows: list[dict]) -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
    """Map positions for cameras that share one spot (several views of one mast): each is nudged
    ``FAN_M`` metres towards the way it faces (or evenly around the spot), so every view can be
    clicked at street zoom. Only the map uses these; every API keeps the true position. Also
    returns labels: the mast's shared name on its first view, "" on the others."""
    groups: dict[tuple[float, float], list[dict]] = {}
    for r in rows:
        groups.setdefault((round(r["lat"], 4), round(r["lon"], 4)), []).append(r)
    out: dict[str, tuple[float, float]] = {}
    labels: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda r: r["id"])
        labels[ordered[0]["id"]] = group_label([m["name"] for m in ordered])
        for m in ordered[1:]:
            labels[m["id"]] = ""          # one name per mast on the map; the cones show the views
        lat0 = sum(m["lat"] for m in members) / len(members)
        lon0 = sum(m["lon"] for m in members) / len(members)
        coslat = max(0.05, math.cos(math.radians(lat0)))
        used: list[float] = []
        for i, m in enumerate(ordered):
            angle = m.get("heading")
            if angle is None or any(abs(((angle - u + 180) % 360) - 180) < 25 for u in used):
                angle = (i * 360.0 / len(members) + 15.0) % 360.0
            used.append(angle)
            rad = math.radians(angle)
            dlat = FAN_M * math.cos(rad) / 111_320.0
            dlon = FAN_M * math.sin(rad) / (111_320.0 * coslat)
            out[m["id"]] = (round(lat0 + dlat, 6), round(lon0 + dlon, 6))
    return out, labels


def to_feature(row: dict, at: tuple[float, float] | None = None, label: str | None = None) -> dict:
    """One lean map point (see the module docstring). ``label`` is accepted for compatibility and
    ignored: names are served by ``/api/cameras/labels``."""
    props: dict = {"id": row["id"]}
    k = kind_code(row)
    if k:
        props["k"] = k
    if is_webcam(row):
        props["t"] = 1
    g = filter_code(row.get("tags") or [])
    if g:
        props["g"] = g
    if row.get("heading") is not None:
        props["h"] = round(float(row["heading"])) % 360
    lat, lon = at if at else (row["lat"], row["lon"])
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
            "properties": props}


def map_collection(rows: list[dict], built_at: float) -> dict:
    """The whole map file for ``rows`` (already sorted and de-duplicated)."""
    spots, _labels = fan_out(rows)
    return {"type": "FeatureCollection", "built_at": built_at, "count": len(rows),
            "features": [to_feature(r, spots.get(r["id"])) for r in rows]}
