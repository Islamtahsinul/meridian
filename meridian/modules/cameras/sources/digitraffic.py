"""Fintraffic Digitraffic (Finland) road weather cameras: about 800 stations, 2,000+ camera presets.

The station list is fetched daily. Station details (English names, municipality, province, road and
preset names) change rarely, so each is cached for a week and fetched within a time budget; a
station without details still appears, named from its short code ("kt51_Inkoo" -> "Road 51 Inkoo").
"""

from __future__ import annotations

import re
import time

from meridian.net import NetError

from ..common import FetchContext, Source, clean, make_camera, read_json, sanitize_id

LIST_URL = "https://tie.digitraffic.fi/api/weathercam/v1/stations"
DETAIL_URL = "https://tie.digitraffic.fi/api/weathercam/v1/stations/{id}"
IMAGE_URL = "https://weathercam.digitraffic.fi/{preset}.jpg"
HEADERS = {"Accept-Encoding": "gzip", "Accept": "application/json"}
DETAIL_MAX_AGE_H = 7 * 24
DETAIL_BUDGET_S = 200.0
DETAIL_PER_BUILD = 180
DETAIL_SPACING_S = 1.05
_CODE = re.compile(r"^(vt|kt|st|mt|yt)(\d+)_?(.*)$", re.I)


def name_from_code(code: str) -> tuple[str, str]:
    """('kt51_Inkoo') -> ('Road 51 Inkoo', 'Road 51')."""
    text = clean(code).replace("_", " ")
    m = _CODE.match(code or "")
    if not m:
        return text, ""
    rest = clean(m.group(3).replace("_", " "))
    road = f"Road {int(m.group(2))}"
    return (f"{road} {rest}".strip(), road)


def parse(stations: dict, details: dict[str, dict], src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    for feat in (stations or {}).get("features") or []:
        props = feat.get("properties") or {}
        sid = props.get("id") or feat.get("id")
        if not sid:
            continue
        if props.get("collectionStatus") not in (None, "GATHERING"):
            skipped += len(props.get("presets") or []) or 1
            continue
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            skipped += 1
            continue
        lon, lat = coords[0], coords[1]
        detail = (details.get(sid) or {}).get("properties") or {}
        names = detail.get("names") or {}
        code_name, code_road = name_from_code(props.get("name") or sid)
        base = clean(names.get("en") or names.get("fi")) or code_name
        road_no = (detail.get("roadAddress") or {}).get("roadNumber")
        road = f"Road {road_no}" if road_no else code_road
        presets = detail.get("presets") or props.get("presets") or []
        interval = detail.get("collectionInterval")
        for p in presets:
            pid = p.get("id")
            if not pid or p.get("inCollection") is False:
                continue
            view = clean(p.get("presentationName"))
            title = f"{base}, {view}" if view else f"{base}, view {pid[-2:]}"
            row = make_camera(src, pid, title, lat, lon, image=p.get("imageUrl") or IMAGE_URL.format(preset=sanitize_id(pid)),
                              region=detail.get("province"), area=detail.get("municipality"), road=road,
                              refresh=interval, description=f"Station {sid}")
            if row:
                rows.append(row)
            else:
                skipped += 1
    return rows, skipped


def _load_details(fc: FetchContext, ids: list[str]) -> tuple[dict[str, dict], int]:
    details: dict[str, dict] = {}
    todo: list[str] = []
    now = time.time()
    for sid in ids:
        path = fc.raw_path(f"stations/{sanitize_id(sid)}.json")
        fresh = path.exists() and (now - path.stat().st_mtime) < DETAIL_MAX_AGE_H * 3600
        if path.exists():
            try:
                details[sid] = read_json(path)
            except (ValueError, OSError):
                fresh = False
        if not fresh or fc.force:
            todo.append(sid)
    # Digitraffic allows 300 requests per 5 minutes per client: go one at a time, about one per
    # second, at most DETAIL_PER_BUILD per build, and stop at the first 429. Stations still without
    # details are named from their codes and filled in by later builds.
    deadline = time.monotonic() + min(DETAIL_BUDGET_S, max(10.0, fc.time_left() - 30))
    todo.sort(key=lambda sid: sid in details)          # never-fetched stations first
    fetched = 0
    for sid in todo[:DETAIL_PER_BUILD]:
        if time.monotonic() > deadline:
            break
        try:
            path = fc.download(DETAIL_URL.format(id=sanitize_id(sid)), f"stations/{sanitize_id(sid)}.json",
                               max_age_hours=DETAIL_MAX_AGE_H / 0.9, headers=HEADERS, timeout=30,
                               min_interval=DETAIL_SPACING_S, force=True)
            details[sid] = read_json(path)
            fetched += 1
        except NetError as err:
            if err.status == 429:
                fc.notes.append("station details paused (rate limit)")
                break
        except (ValueError, OSError):
            continue
    return details, fetched


def fetch(fc: FetchContext) -> list[dict]:
    stations = fc.json(LIST_URL, "stations.json", headers=HEADERS, min_bytes=1000, timeout=90)
    ids = [f.get("id") or (f.get("properties") or {}).get("id") for f in stations.get("features") or []]
    ids = [i for i in ids if i]
    details, fetched = _load_details(fc, ids)
    missing = len(ids) - len(details)
    if fetched or missing:
        fc.notes.append(f"station details: {len(details)} of {len(ids)} cached, {fetched} refreshed"
                        + (f", {missing} named from codes" if missing else ""))
    rows, skipped = parse(stations, details, fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="fi", name="Digitraffic weather cameras", owner="Fintraffic (Finland)",
    url="https://www.digitraffic.fi/en/road-traffic/", licence="CC BY 4.0",
    licence_url="https://creativecommons.org/licenses/by/4.0/",
    attribution="Fintraffic / digitraffic.fi, CC BY 4.0", fetch=fetch, hosts=("weathercam.digitraffic.fi",),
    country="FI", refresh=600, tags=("weather", "traffic"), coverage="Finnish road network",
)
