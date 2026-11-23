"""Satellite scene finder: the latest Sentinel-2, Sentinel-1 and Landsat scenes over a point or a small
box, from keyless STAC catalogues, with quicklooks through a small allowlisted image proxy.

Catalogues (all keyless; searched and fetched while building the module, 23 Nov 2026):
- Sentinel-2 L2A: Element 84 Earth Search (``earth-search.aws.element84.com/v1``). Quicklook: the
  ``preview.jpg`` beside each scene on the public ``sentinel-cogs`` bucket (AWS open data).
- Sentinel-1 GRD: Copernicus Data Space Ecosystem STAC (``stac.dataspace.copernicus.eu/v1``). Quicklook:
  the product thumbnail from ``datahub.creodias.eu`` (it redirects to ``zipper.creodias.eu``).
- Landsat Collection 2 Level-2: Microsoft Planetary Computer STAC. Quicklook: Planetary Computer's
  rendered preview. USGS LandsatLook thumbnails now redirect to the USGS login page, and Earth Search's
  Landsat and Sentinel-1 thumbnails are presigned links into requester-pays buckets, so neither is used.

Politeness: searches run only when the user (or the AI tool) asks, one request per catalogue, cached
for an hour per place and settings. Quicklooks are small (13 to 100 KB), fetched once and cached for a
day. Nothing polls and full scenes are never downloaded.

Safety: the quicklook route takes a mission and a scene id, never a URL. The server looks the image
address up from its own search results (or the catalogue's item record), and fetches only https URLs on
each mission's allowlisted hosts, checking every redirect hop and the image's magic bytes.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from meridian import geo
from meridian.net import NetError

MISSIONS: dict[str, dict] = {
    "sentinel-2": {
        "title": "Sentinel-2", "kind": "optical", "collection": "sentinel-2-l2a",
        "search": "https://earth-search.aws.element84.com/v1/search",
        "item": "https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/{id}",
        "sort": "-properties.datetime", "cloud": "query",
        "hosts": ("sentinel-cogs.s3.us-west-2.amazonaws.com",),
        "catalogue": "Element 84 Earth Search", "catalogue_url": "https://element84.com/earth-search/",
        "licence": "Copernicus Sentinel data: free, full and open (Sentinel data legal notice)",
        "credit": "Contains modified Copernicus Sentinel data {year}",
        "about": "10 m optical imagery from Sentinel-2A, 2B and 2C, a pass every few days; cloud hides the ground.",
    },
    "sentinel-1": {
        "title": "Sentinel-1", "kind": "radar", "collection": "sentinel-1-grd",
        "search": "https://stac.dataspace.copernicus.eu/v1/search",
        "item": "https://stac.dataspace.copernicus.eu/v1/collections/sentinel-1-grd/items/{id}",
        "sort": "-datetime", "cloud": None,
        "hosts": ("datahub.creodias.eu", "zipper.creodias.eu"),
        "catalogue": "Copernicus Data Space Ecosystem", "catalogue_url": "https://dataspace.copernicus.eu/",
        "licence": "Copernicus Sentinel data: free, full and open (CDSE terms and conditions)",
        "credit": "Contains modified Copernicus Sentinel data {year}",
        "about": "Radar from Sentinel-1C and 1D: sees through cloud and at night. Quicklooks are in the "
                 "satellite's viewing geometry, so they can look mirrored or turned.",
    },
    "landsat": {
        "title": "Landsat", "kind": "optical", "collection": "landsat-c2-l2",
        "search": "https://planetarycomputer.microsoft.com/api/stac/v1/search",
        "item": "https://planetarycomputer.microsoft.com/api/stac/v1/collections/landsat-c2-l2/items/{id}",
        "sort": "-datetime", "cloud": "filter",
        "fields": ",".join(["id", "bbox", "geometry", "links", "properties.datetime", "properties.eo:cloud_cover",
                            "properties.platform", "properties.landsat:wrs_path", "properties.landsat:wrs_row",
                            "properties.landsat:scene_id", "properties.landsat:collection_category"]),
        "hosts": ("planetarycomputer.microsoft.com",),
        "catalogue": "Microsoft Planetary Computer", "catalogue_url": "https://planetarycomputer.microsoft.com/",
        "licence": "Landsat: public domain (USGS Landsat data policy)",
        "credit": "Landsat imagery courtesy of the U.S. Geological Survey",
        "about": "30 m optical imagery from Landsat 8 and 9, each place every 8 days between them.",
    },
}
ORDER = ("sentinel-2", "sentinel-1", "landsat")
PC_PREVIEW = "https://planetarycomputer.microsoft.com/api/data/v1/item/preview.jpg"
# Planetary Computer's own rendering of landsat-c2-l2 (its rendered_preview asset), as a small JPEG
PC_PREVIEW_PARAMS = [("assets", "red"), ("assets", "green"), ("assets", "blue"),
                     ("color_formula", "gamma RGB 2.7, saturation 1.5, sigmoidal RGB 15 0.55"), ("max_size", "360")]
CDSE_BROWSER = "https://browser.dataspace.copernicus.eu/"

DEFAULT_DAYS, MAX_DAYS = 30, 120
DEFAULT_LIMIT, MAX_LIMIT = 4, 8
DEFAULT_CLOUD = 60
MAX_BOX_DEG = 3.0              # a larger view is searched at its centre point instead
SEARCH_TTL = 3600.0
QUICKLOOK_TTL = 86400.0
QUICKLOOK_MAX_BYTES = 3_000_000
QUICKLOOK_CACHE_BYTES = 48_000_000
FAIL_TTL = 600.0
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{4,150}$")
_REDIRECTS = (301, 302, 303, 307, 308)


class SceneError(Exception):
    """A scene request that cannot be answered (bad id, unknown scene, upstream failure)."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(text) -> float | None:
    if not isinstance(text, str) or len(text) < 19:
        return None
    try:
        base = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    frac = re.match(r"\.(\d+)", text[19:])
    return base.timestamp() + (float("0." + frac.group(1)) if frac else 0.0)


def valid_id(item_id: str) -> bool:
    return bool(_ID.match(item_id or "")) and ".." not in item_id


def allowed_url(mission: str, url: str) -> bool:
    """https on one of the mission's quicklook hosts, default port, no credentials."""
    if not isinstance(url, str) or len(url) > 2000 or any(c in url for c in "\r\n\t\\ "):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port                       # raises ValueError for a malformed port
    except ValueError:
        return False
    return (parts.scheme == "https" and not parts.username and not parts.password and port in (None, 443)
            and (parts.hostname or "").lower() in MISSIONS[mission]["hosts"])


def area(point=None, bbox=None) -> tuple[tuple[float, float] | None, tuple[float, float, float, float] | None]:
    """Normalise a search area: a small box is kept, a large or antimeridian-crossing one becomes its centre."""
    if bbox is not None:
        w, s, e, n = bbox
        if w <= e and (e - w) <= MAX_BOX_DEG and (n - s) <= MAX_BOX_DEG:
            return None, (round(w, 3), round(s, 3), round(e, 3), round(n, 3))
        if point is None:
            lon = (w + e) / 2 if w <= e else geo.wrap_lon((w + e + 360) / 2)
            point = ((s + n) / 2, lon)
    if point is None:
        raise ValueError("a point or a box is needed")
    lat, lon = point
    return (round(lat, 4), round(lon, 4)), None


def search_params(mission: str, *, point=None, bbox=None, days: int = DEFAULT_DAYS, max_cloud: float | None = None,
                  limit: int = DEFAULT_LIMIT, now: float | None = None) -> tuple[str, dict]:
    """The STAC item search (GET) for one mission: (url, query parameters)."""
    m = MISSIONS[mission]
    start = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc) - timedelta(days=days)
    start = start.replace(minute=0, second=0, microsecond=0)       # hour steps keep the cache key stable
    params: dict = {"collections": m["collection"], "limit": str(limit), "sortby": m["sort"],
                    "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/.."}
    if bbox is not None:
        params["bbox"] = ",".join(f"{v:g}" for v in bbox)
    else:
        lat, lon = point
        params["intersects"] = json.dumps({"type": "Point", "coordinates": [lon, lat]}, separators=(",", ":"))
    if max_cloud is not None and max_cloud < 100 and m["cloud"] == "query":
        params["query"] = json.dumps({"eo:cloud_cover": {"lte": max_cloud}}, separators=(",", ":"))
    elif max_cloud is not None and max_cloud < 100 and m["cloud"] == "filter":
        params["filter-lang"] = "cql2-text"
        params["filter"] = f"eo:cloud_cover <= {max_cloud:g}"
    if m.get("fields"):
        params["fields"] = m["fields"]
    return m["search"], params


def _round_geometry(geom, max_points: int = 240):
    if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    polys = [geom.get("coordinates")] if geom["type"] == "Polygon" else geom.get("coordinates")
    out = []
    try:
        for poly in polys or []:
            rings = []
            for ring in poly or []:
                pts = [[round(float(p[0]), 5), round(float(p[1]), 5)] for p in ring]
                if len(pts) > max_points:                        # keep it light: thin long rings evenly
                    step = len(pts) / max_points
                    pts = [pts[int(i * step)] for i in range(max_points)] + [pts[-1]]
                if len(pts) >= 4 and all(-180.0 <= x <= 180.0 and -90.0 <= y <= 90.0 for x, y in pts):
                    rings.append(pts)
            if rings:
                out.append(rings)
    except (TypeError, ValueError, IndexError):
        return None
    if not out:
        return None
    return {"type": "Polygon", "coordinates": out[0]} if len(out) == 1 else {"type": "MultiPolygon", "coordinates": out}


def _platform(raw) -> str:
    text = str(raw or "").strip()
    m = re.fullmatch(r"(?i)sentinel-?(\d)([a-z])", text)
    if m:
        return f"Sentinel-{m.group(1)}{m.group(2).upper()}"
    m = re.fullmatch(r"(?i)landsat[-_ ]?(\d+)", text)
    if m:
        return f"Landsat {m.group(1)}"
    return text[:40] or "Unknown satellite"


def _link(links, rel: str, host: str | None = None) -> str | None:
    for l in links or []:
        if not isinstance(l, dict) or l.get("rel") != rel:
            continue
        href = str(l.get("href") or "")
        parts = urllib.parse.urlsplit(href)
        if parts.scheme == "https" and (host is None or (parts.hostname or "").lower() == host):
            return href
    return None


def _browser_link(dataset: str | None, lat: float, lon: float, t: float) -> str:
    day = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
    params = {"zoom": "10", "lat": f"{lat:.4f}", "lng": f"{lon:.4f}",
              "fromTime": f"{day}T00:00:00.000Z", "toTime": f"{day}T23:59:59.999Z"}
    if dataset:
        params["datasetId"] = dataset
    if dataset == "S2_L2A_CDAS":
        params["layerId"] = "1_TRUE_COLOR"
    return CDSE_BROWSER + "?" + urllib.parse.urlencode(params)


def quicklook_href(mission: str, feature: dict) -> str | None:
    """Where the quicklook of a STAC item lives, or None when it is not on an allowed host."""
    item_id = str(feature.get("id") or "")
    if mission == "landsat":
        if not valid_id(item_id):
            return None
        q = [("collection", MISSIONS["landsat"]["collection"]), ("item", item_id)] + PC_PREVIEW_PARAMS
        return PC_PREVIEW + "?" + urllib.parse.urlencode(q)
    asset = ((feature.get("assets") or {}).get("thumbnail") or {})
    href = str(asset.get("href") or "")
    return href if allowed_url(mission, href) else None


def parse_items(mission: str, data, point=None, limit: int = MAX_LIMIT) -> list[dict]:
    """A STAC FeatureCollection -> compact scene records (newest first), skipping anything malformed."""
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        raise ValueError("the catalogue did not send a STAC item collection")
    m = MISSIONS[mission]
    out = []
    for f in data["features"]:
        if not isinstance(f, dict):
            continue
        item_id = str(f.get("id") or "")
        props = f.get("properties") if isinstance(f.get("properties"), dict) else {}
        t = _parse_time(props.get("datetime") or props.get("start_datetime"))
        geom = _round_geometry(f.get("geometry"))
        if not valid_id(item_id) or t is None or geom is None:
            continue
        bbox = geo.geometry_bbox(geom)
        if bbox is None:
            continue
        cloud = props.get("eo:cloud_cover")
        cloud = round(float(cloud), 1) if isinstance(cloud, (int, float)) and not isinstance(cloud, bool) \
            and 0 <= cloud <= 100 else None
        clat, clon = (point if point else ((bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2))
        links: list[dict] = []
        detail = ""
        if mission == "sentinel-2":
            tile = str(props.get("grid:code") or props.get("s2:tile_id") or "").replace("MGRS-", "")
            detail = f"Tile {tile[:12]}" if tile else ""
            links.append({"text": "Open in Copernicus Browser", "href": _browser_link("S2_L2A_CDAS", clat, clon, t)})
        elif mission == "sentinel-1":
            mode = str(props.get("sar:instrument_mode") or "").upper()[:4]
            pols = props.get("sar:polarizations") if isinstance(props.get("sar:polarizations"), list) else []
            pol = "+".join(str(p)[:2].upper() for p in pols[:4])
            orbit = str(props.get("sat:orbit_state") or "").lower()
            bits = [b for b in (mode, pol, orbit if orbit in ("ascending", "descending") else "") if b]
            detail = ", ".join(bits)
            links.append({"text": "Open in Copernicus Browser",
                          "href": _browser_link("S1_CDAS_IW_VVVH" if mode == "IW" else None, clat, clon, t)})
        else:
            path, row = props.get("landsat:wrs_path"), props.get("landsat:wrs_row")
            if path and row:
                detail = f"Path {str(path).lstrip('0') or '0'}, row {str(row).lstrip('0') or '0'}"
            via = _link(f.get("links"), "via", "landsatlook.usgs.gov")
            if via and "/stac-server/collections/landsat-c2l2-sr/" in via:
                links.append({"text": "USGS LandsatLook record", "href": via.replace("/stac-server/", "/stac-browser/", 1)})
            links.append({"text": "Planetary Computer viewer", "href": "https://planetarycomputer.microsoft.com/api/data/v1/item/map?"
                          + urllib.parse.urlencode({"collection": m["collection"], "item": item_id})})
        self_link = _link(f.get("links"), "self") or m["item"].format(id=urllib.parse.quote(item_id))
        links.append({"text": "Catalogue record", "href": self_link})
        rec = {
            "key": f"{mission}:{item_id}", "mission": mission, "mission_title": m["title"], "kind": m["kind"],
            "id": item_id, "collection": m["collection"], "platform": _platform(props.get("platform")),
            "time": round(t), "datetime": _iso(t), "cloud": cloud, "detail": detail,
            "bbox": [round(v, 5) for v in bbox], "geometry": geom, "links": links,
            "quicklook": f"/api/space/scenes/quicklook/{mission}/{urllib.parse.quote(item_id)}",
            "_quicklook_href": quicklook_href(mission, f),
        }
        if point:
            rec["covers_point"] = geo.point_in_geometry(point[1], point[0], geom)
        out.append(rec)
    out.sort(key=lambda r: -r["time"])
    return out[:limit]


def _sniff(body: bytes) -> str | None:
    if body[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


class Scenes:
    """Searches (cached an hour) and quicklooks (cached a day), shared by the route and the AI tool."""

    def __init__(self, net, log, status=None):
        self.net = net
        self.log = log
        self._status = status or (lambda mission, **kw: None)
        self._lock = threading.Lock()
        self._searches: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._hrefs: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._images: OrderedDict[tuple[str, str], tuple[float, bytes, str]] = OrderedDict()
        self._image_bytes = 0
        self._failed: dict[tuple[str, str], float] = {}
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="space-scenes")

    # -- searching -----------------------------------------------------------------------------
    def _search_one(self, mission: str, point, bbox, days: int, max_cloud, limit: int) -> dict:
        url, params = search_params(mission, point=point, bbox=bbox, days=days, max_cloud=max_cloud, limit=limit)
        try:
            data = self.net.get_json(url, params=params, timeout=15, retries=1, max_bytes=8_000_000,
                                     headers={"Accept": "application/geo+json, application/json"})
            scenes = parse_items(mission, data, point=point, limit=limit)
        except Exception as err:
            msg = str(err)[:200] or err.__class__.__name__
            self.log.info("scene search %s failed: %s", mission, msg)
            self._status(mission, ok=False, error=msg)
            return {"ok": False, "error": f"{MISSIONS[mission]['catalogue']} is not answering right now.", "scenes": []}
        with self._lock:
            for s in scenes:
                href = s.pop("_quicklook_href", None)
                if href:
                    self._hrefs[(mission, s["id"])] = href
                    self._hrefs.move_to_end((mission, s["id"]))
                else:
                    s["quicklook"] = None
            while len(self._hrefs) > 4000:
                self._hrefs.popitem(last=False)
        self._status(mission, ok=True, count=len(scenes))
        return {"ok": True, "error": None, "scenes": scenes}

    def search(self, *, point=None, bbox=None, days: int = DEFAULT_DAYS, max_cloud: float | None = DEFAULT_CLOUD,
               missions=ORDER, limit: int = DEFAULT_LIMIT) -> dict:
        point, bbox = area(point, bbox)
        missions = [m for m in ORDER if m in set(missions)]
        if not missions:
            raise ValueError("choose at least one of sentinel-2, sentinel-1, landsat")
        now = time.time()
        results: dict[str, dict] = {}
        jobs = {}
        cached = []
        for mission in missions:
            key = (mission, point, bbox, int(days), None if max_cloud is None else float(max_cloud), int(limit))
            with self._lock:
                hit = self._searches.get(key)
            if hit and now - hit[0] < SEARCH_TTL:
                results[mission] = dict(hit[1], cached=True)
                cached.append(mission)
            else:
                jobs[mission] = (key, self._pool.submit(self._search_one, mission, point, bbox, int(days), max_cloud,
                                                        int(limit)))
        for mission, (key, job) in jobs.items():
            try:
                res = job.result(timeout=25)
            except Exception:
                res = {"ok": False, "error": f"{MISSIONS[mission]['catalogue']} did not answer in time.", "scenes": []}
            if res["ok"]:
                with self._lock:
                    self._searches[key] = (now, res)
                    while len(self._searches) > 300:
                        self._searches.popitem(last=False)
            results[mission] = dict(res, cached=False)
        merged = sorted((s for m in missions for s in results[m]["scenes"]), key=lambda s: -s["time"])
        return {"point": {"lat": point[0], "lon": point[1]} if point else None, "bbox": list(bbox) if bbox else None,
                "days": int(days), "max_cloud": max_cloud, "missions": results, "scenes": merged,
                "searched_at": round(now), "credits": credits(missions)}

    # -- quicklooks ------------------------------------------------------------------------------
    def _recover_href(self, mission: str, item_id: str) -> str | None:
        """After a restart the search results are gone: ask the catalogue for the item record."""
        url = MISSIONS[mission]["item"].format(id=urllib.parse.quote(item_id))
        data = self.net.get_json(url, timeout=12, retries=1, max_bytes=4_000_000)
        if not isinstance(data, dict) or str(data.get("id") or "") != item_id:
            return None
        return quicklook_href(mission, data)

    def _fetch_image(self, mission: str, url: str) -> tuple[bytes, str]:
        for _ in range(4):                                  # the first request plus up to three redirects
            if not allowed_url(mission, url):
                raise SceneError("The quicklook is not on an allowed host.", 502)
            resp = self.net.request(url, timeout=10, retries=1, max_bytes=QUICKLOOK_MAX_BYTES,
                                    accept=(200,) + _REDIRECTS, allow_redirects=False,
                                    headers={"Accept": "image/jpeg, image/png, */*;q=0.5"})
            if resp.status in _REDIRECTS:
                loc = (resp.headers or {}).get("location") or ""
                url = urllib.parse.urljoin(url, loc)
                continue
            kind = _sniff(resp.body)
            if not kind:
                raise SceneError("The catalogue sent something that is not a JPEG or PNG image.", 502)
            return resp.body, kind
        raise SceneError("Too many redirects for the quicklook.", 502)

    def quicklook(self, mission: str, item_id: str) -> tuple[bytes, str]:
        if mission not in MISSIONS:
            raise SceneError("Unknown mission. Choose sentinel-2, sentinel-1 or landsat.", 400)
        if not valid_id(item_id):
            raise SceneError("That is not a scene id.", 400)
        key = (mission, item_id)
        now = time.time()
        with self._lock:
            hit = self._images.get(key)
            if hit and now - hit[0] < QUICKLOOK_TTL:
                self._images.move_to_end(key)
                return hit[1], hit[2]
            failed = self._failed.get(key, 0.0)
            href = self._hrefs.get(key)
        if now - failed < FAIL_TTL:
            raise SceneError("The quicklook failed a few minutes ago; it will be tried again later.", 502)
        try:
            if href is None:
                try:
                    href = self._recover_href(mission, item_id)
                except NetError as err:
                    if err.status in (400, 404):
                        raise SceneError("That scene is not in the catalogue.", 404) from None
                    raise
                if href is None:
                    raise SceneError("No quicklook is listed for that scene.", 404)
                with self._lock:
                    self._hrefs[key] = href
            body, kind = self._fetch_image(mission, href)
        except SceneError as err:
            if err.status >= 500 or err.status == 404:
                with self._lock:
                    self._failed[key] = now
            raise
        except Exception as err:
            with self._lock:
                self._failed[key] = now
                if len(self._failed) > 2000:
                    self._failed.clear()
            self.log.info("quicklook %s %s failed: %s", mission, item_id, err)
            raise SceneError("The quicklook could not be fetched right now.", 502) from None
        with self._lock:
            old = self._images.pop(key, None)
            if old:
                self._image_bytes -= len(old[1])
            self._images[key] = (now, body, kind)
            self._image_bytes += len(body)
            while self._image_bytes > QUICKLOOK_CACHE_BYTES and self._images:
                _, (_, b, _) = self._images.popitem(last=False)
                self._image_bytes -= len(b)
        return body, kind


def credits(missions=ORDER) -> list[dict]:
    year = datetime.now(timezone.utc).year
    out = []
    for m in missions:
        info = MISSIONS[m]
        out.append({"mission": m, "text": info["credit"].format(year=year), "catalogue": info["catalogue"],
                    "url": info["catalogue_url"], "licence": info["licence"]})
    return out
