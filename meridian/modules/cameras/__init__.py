"""Public cameras: official traffic, weather, mountain and city cameras from open feeds, worldwide
webcams from Windy (optional key) and public volcano, polar and park cameras, with a server-side
image proxy (latest frame only, never stored).

Endpoints (all GET):
    /api/cameras/geojson              lean points for the map (ETag + gzip; see mapfile.py)
    /api/cameras/summary              counts (per "Show" choice too), build time and per-source status
    /api/cameras/sources              per-source status, licence and attribution
    /api/cameras/camera/{id}          one camera with source, licence and latest-image facts
    /api/cameras/img/{id}             the camera's current still (proxied, cached 30-120 s)
    /api/cameras/direct/{id}          fresh picture link for a direct camera (Windy), never proxied
    /api/cameras/direct?ids=a,b       the same for up to 50 cameras at once
    /api/cameras/labels?bbox=         names at their map positions, for street zoom and hover
    /api/cameras/list?ids=|bbox=      cameras by id list, or in a bbox nearest the centre first
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict

from meridian import geo
from meridian.net import NetError
from meridian.registry import HTTPError, Response

from . import build as build_mod
from . import sources as sources_mod
from .catalog import SHOW_CHOICES, Catalog, brief, camera_type, entity, is_live, show_test, subtitle
from .common import CAMERA_TYPES, COUNTRY_NAMES, LIVE_KINDS, TAGS, NeedsKey, compass_label
from .mapfile import kind_code
from .proxy import ImageProxy, ProxyError
from .sources import windy

ID_RE = re.compile(r"^[a-z0-9]{1,16}:[A-Za-z0-9._~-]{1,96}$")
# shown instead of a broken image when a camera cannot be fetched (fixed markup, no data in it)
PLACEHOLDER_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180" width="320" height="180">'
    b'<rect width="320" height="180" fill="#05070c"/>'
    b'<g fill="none" stroke="#3a5080" stroke-width="3" stroke-linejoin="round">'
    b'<path d="M136 74h12l5-8h22l5 8h12v38h-56z"/><circle cx="164" cy="93" r="10"/>'
    b'<path d="M128 124l64-64" stroke="#75839f"/></g></svg>')
LAYER = "cameras"
LABELS_MAX = 600


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.build_lock = threading.Lock()       # one writer of cameras.json / cameras.geojson at a time
        self.catalog = Catalog([])
        self.loaded_at: float | None = None
        self.load_error: str | None = None


class DirectCache:
    """Fresh picture links for direct cameras, reused for the source's ``direct_ttl`` (5 min for
    Windy, whose links expire after 10 to 15). Failures are remembered for a minute so a source
    that is down is not asked again for every tile."""

    def __init__(self, max_entries: int = 5000):
        self.lock = threading.Lock()
        self.items: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self.max_entries = max_entries
        self.stats = {"hits": 0, "lookups": 0, "errors": 0}

    def get_many(self, rows: list[dict], src, net, redact) -> dict[str, dict]:
        now = time.time()
        out: dict[str, dict] = {}
        miss: list[dict] = []
        with self.lock:
            for row in rows:
                hit = self.items.get(row["id"])
                if hit and hit[0] > now:
                    out[row["id"]] = hit[1]
                    self.stats["hits"] += 1
                else:
                    miss.append(row)
        if not miss:
            return out
        self.stats["lookups"] += 1
        try:
            got = src.direct(miss, net) if src.direct else {}
            failed = None
        except NeedsKey as err:
            got, failed = {}, f"{src.name} need {err.key_name}."
        except (NetError, ValueError, TypeError, KeyError, AttributeError) as err:
            self.stats["errors"] += 1
            got = {}
            status = getattr(err, "status", None)
            failed = (f"{src.name} did not give a picture link" + (f" (HTTP {status})." if status else ".")
                      if isinstance(err, NetError) else f"{src.name} gave an unexpected answer.")
            failed = redact(failed)
        fetched = time.time()
        with self.lock:
            for row in miss:
                info = got.get(row["id"]) if not failed else None
                if info is None:
                    info = {"ok": False, "error": failed or f"{src.name} has no picture for this camera now."}
                info = dict(info, fetched_at=fetched)
                ttl = src.direct_ttl if info.get("ok") else 60.0
                info["expires_at"] = fetched + ttl
                self.items[row["id"]] = (fetched + ttl, info)
                self.items.move_to_end(row["id"])
                out[row["id"]] = info
            while len(self.items) > self.max_entries:
                self.items.popitem(last=False)
        return out


def _human_seconds(s: int) -> str:
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    return f"{round(s / 3600)} h"


def courtesy(src) -> list[dict]:
    return [{"text": text, "href": href} for text, href in (getattr(src, "courtesy", ()) or ())]


def register(m) -> None:
    sources_mod.bind_config(m.config)
    state = State()
    proxy = ImageProxy(m.net, m.log)
    direct_cache = DirectCache()
    for name, error in sources_mod.BROKEN:
        m.log.warning("camera adapter %s was skipped: %s", name, m.config.redact(error))

    # -- keys and settings ------------------------------------------------------------------------
    m.key("WINDY_WEBCAMS_KEY", "Windy Webcams API key (free tier): adds about 69,000 webcams worldwide "
          "(catalogue crawled weekly, pictures loaded from Windy).", "https://api.windy.com/keys")
    m.key("NPS_API_KEY", "US National Park Service API key (free): adds national park webcams.",
          "https://www.nps.gov/subjects/developer/get-started.htm")
    m.key("ONTARIO511_KEY", "Ontario 511 developer key: adds Ontario highway cameras.", "https://511on.ca/developers/doc")
    m.key("ALBERTA511_KEY", "511 Alberta developer key: adds Alberta highway cameras.",
          "https://511.alberta.ca/developers/doc")
    m.key("NY511_KEY", "511NY developer key: adds New York State traffic cameras.", "https://511ny.org/developers/doc")
    # QLDTraffic works without a key (a shared public key, often over quota), so the source has no
    # needs_key and the loop below would skip it; declared here so Settings and .env.example list it
    # (literal name: sources load adapters defensively, so this must not import sources.ap_qld)
    m.key("QLDTRAFFIC_KEY", "QLDTraffic API key (optional): replaces the shared public key, which is often over "
          "its quota, for Queensland road cameras. Request one from QLDTraffic@tmr.qld.gov.au.",
          "https://qldtraffic.qld.gov.au/more/Developers-and-Data/index.html")
    m.key("CAMERAS_DISABLE", "Camera sources to skip, as comma-separated ids (for example nyc,autobahn).", "",
          secret=False)
    m.key("CAMERAS_ONLY", "Build only these camera sources (comma-separated ids). For development; leave unset.", "",
          secret=False)

    # every other source's key too, so all of them are listed in Settings and redacted everywhere
    for src in sources_mod.ALL:
        if src.needs_key and src.needs_key not in m.config.declared:
            m.key(src.needs_key, f"{src.name} key: adds {src.coverage or src.name}.", src.url)

    for src in sources_mod.ALL:
        refresh = ("catalogue weekly, pictures from the source on demand" if src.direct else
                   f"camera list daily, images on demand (about every {_human_seconds(src.refresh)})")
        m.source(f"cameras.{src.id}", src.name, src.url, src.licence, refresh=refresh,
                 layer=LAYER, needs_key=src.needs_key, optional=bool(getattr(src, "optional", False)))

    # -- datasets ---------------------------------------------------------------------------------
    windy_on = m.config.has(windy.KEY) and windy.SOURCE.id not in build_mod.disabled_sources(m.config, sources_mod.ALL)
    if windy_on:
        # registered first so `--build cameras` crawls before it builds the camera list
        def build_windy(ds):
            windy.build_crawl(ds, m.config, m.log)

        m.dataset("windy", build_windy, max_age_hours=24,
                  description="Windy webcams catalogue: checked daily, crawled at most weekly (about 1 request/s)")

    def build(ds):
        with state.build_lock:
            return build_mod.build(ds, sources_mod.ALL, m.config, m.log)

    m.dataset("cameras", build, max_age_hours=24,
              description="Public traffic, weather, mountain and city cameras from official open feeds, plus webcams")

    def load():
        path = m.data_dir / "cameras.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            state.load_error = str(err)
            m.log.warning("camera list could not be loaded: %s", err)
            return
        cat = Catalog(doc.get("cameras") or [], doc.get("sources") or {}, doc.get("built_at"))
        cat.counts()                      # computed here, not on the first summary request
        try:                              # and the map file's gzip is made now, not on the first map load
            m.serve_file(m.data_dir / "cameras.geojson", "application/geo+json; charset=utf-8")
        except Exception as err:  # a missing map file shows as BUILDING on the route
            m.log.debug("map file not cached: %s", err)
        with state.lock:
            state.catalog = cat
            state.loaded_at = time.time()
            state.load_error = None
        proxy.clear()
        for sid, meta in cat.sources.items():
            m.status(f"cameras.{sid}", ok=meta.get("ok"), count=meta.get("count") or 0,
                     error=meta.get("error"), note=meta.get("note"))
        _remember_favourites(cat)
        m.log.info("camera catalog loaded: %d cameras", cat.count)
        if windy_on:
            threading.Thread(target=sync_windy, name="cameras-windy-sync", daemon=True).start()

    def sync_windy():
        """When a Windy crawl finished after the camera list was built, put its webcams in the list
        now (the other sources keep their rows; nothing else is fetched)."""
        try:
            crawl = m.data_dir / windy.CRAWL_FILE
            with state.build_lock:
                try:
                    stamp = crawl.stat().st_mtime
                except OSError:
                    return
                current = (state.catalog.sources.get(windy.SOURCE.id) or {}).get("crawl_file_mtime")
                if stamp == current or state.catalog.count == 0:
                    return
                from meridian.datasets import BuildContext
                ctx = BuildContext(m, "cameras", 24.0)
                build_mod.build(ctx, sources_mod.ALL, m.config, m.log, only={windy.SOURCE.id})
            m.log.info("camera list updated with the Windy crawl")
            load()
        except Exception as err:  # bookkeeping: the next daily build picks the crawl up anyway
            m.log.warning("adding the Windy crawl to the camera list failed: %s", m.config.redact(str(err)))

    m.on_dataset("cameras", load)
    if windy_on:
        m.on_dataset("windy", lambda: threading.Thread(target=sync_windy, name="cameras-windy-sync",
                                                       daemon=True).start())

    # last-seen facts for favourite cameras, so a removed camera's card can say when it was listed
    m.store.script("CREATE TABLE IF NOT EXISTS cameras_lastseen (id TEXT PRIMARY KEY, name TEXT, lat REAL, "
                   "lon REAL, src TEXT, last_seen REAL NOT NULL)")

    def _remember_favourites(cat: Catalog) -> None:
        try:
            favs = m.store.query("SELECT ref FROM favourites WHERE kind = 'camera'")
            now = time.time()
            rows = []
            for f in favs:
                row = cat.get(f["ref"])
                if row:
                    rows.append((row["id"], row["name"], row["lat"], row["lon"], row["src"], now))
            if rows:
                m.store.executemany("INSERT INTO cameras_lastseen(id, name, lat, lon, src, last_seen) VALUES(?,?,?,?,?,?) "
                                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, lat=excluded.lat, lon=excluded.lon, "
                                    "src=excluded.src, last_seen=excluded.last_seen", rows)
            m.store.execute("DELETE FROM cameras_lastseen WHERE id NOT IN (SELECT ref FROM favourites WHERE kind = 'camera')")
        except Exception as err:  # bookkeeping only; never block a load
            m.log.debug("lastseen update failed: %s", err)

    # -- helpers ----------------------------------------------------------------------------------
    def catalog() -> Catalog:
        return state.catalog

    def ready_or_503() -> Catalog:
        cat = state.catalog
        if cat.count == 0:
            raise HTTPError(503, "BUILDING", "The camera list is still being built. Try again in a minute.")
        return cat

    def cam_or_404(raw: str) -> dict:
        if not ID_RE.match(raw or ""):
            raise HTTPError(400, "BAD_ID", "That is not a camera id.")
        cat = ready_or_503()
        row = cat.get(raw)
        if row is None:
            raise HTTPError(404, "NOT_FOUND", "There is no camera with that id in the current list.")
        return row

    def source_of(row: dict):
        return sources_mod.BY_ID.get(row["src"])

    def keep_filter(kind: str | None, tag: str | None, ctype: str | None = None):
        if kind not in (None, "", "any", "all", "live", "still") or (tag and tag not in TAGS) or \
                (ctype not in (None, "", "any", "all") and ctype not in CAMERA_TYPES):
            raise HTTPError(400, "BAD_ARG", "kind must be any, live or still; tag must be a known tag; "
                                            "type must be traffic or webcam.")

        def keep(r: dict) -> bool:
            if kind == "live" and r.get("kind") not in LIVE_KINDS:
                return False
            if kind == "still" and r.get("kind") in LIVE_KINDS:
                return False
            if tag and tag not in (r.get("tags") or ()):
                return False
            if ctype in CAMERA_TYPES and camera_type(r) != ctype:
                return False
            return True

        plain = kind in (None, "", "any", "all") and not tag and ctype in (None, "", "any", "all")
        return None if plain else keep

    def detail(row: dict) -> dict:
        src = source_of(row)
        meta = (catalog().sources.get(row["src"]) or {})
        cam = dict(row)
        cam["tags"] = list(row.get("tags") or [])
        cam["type"] = camera_type(row)
        is_direct = bool(row.get("direct") and src and src.direct)
        # a still exists when the row has an image link or looks one up; stream-only cameras have none
        has_still = bool(row.get("image") or (row.get("dynamic") and not is_direct))
        cam["img_url"] = f"/api/cameras/img/{row['id']}" if has_still else None
        cam["direct"] = is_direct
        if is_direct:
            cam["direct_url"] = f"/api/cameras/direct/{row['id']}"
        cam["live"] = is_live(row)
        cam["playable"] = bool(src and src.stream_cors and row.get("stream"))
        cam["country_name"] = COUNTRY_NAMES.get(row.get("country") or "", row.get("country"))
        cam["subtitle"] = subtitle(row)
        if row.get("heading") is not None:
            cam["compass"] = compass_label(row["heading"])
        cam["open_url"] = row.get("page") or row.get("image") or (src.url if src else None)
        facts = proxy.meta(row["id"])
        image = None
        if facts:
            image = {"ok": facts.get("ok"), "error": facts.get("error"), "fetched_at": facts.get("fetched_at"),
                     "upstream_time": facts.get("upstream_time"), "failed_at": facts.get("failed_at")}
        source = {"id": row["src"], "name": meta.get("name") or (src.name if src else row["src"]),
                  "owner": meta.get("owner") or (src.owner if src else None),
                  "url": meta.get("url") or (src.url if src else None), "licence": row.get("licence"),
                  "licence_url": meta.get("licence_url") or (src.licence_url if src else None),
                  "attribution": row.get("attribution"), "ok": meta.get("ok"), "stale": meta.get("stale"),
                  "list_fetched_at": meta.get("fetched_at"), "courtesy": courtesy(src)}
        return {"camera": cam, "image": image, "source": source, "built_at": catalog().built_at}

    # -- routes -----------------------------------------------------------------------------------
    @m.route("GET", "/api/cameras/geojson")
    def r_geojson(req):
        return m.serve_file(m.data_dir / "cameras.geojson", "application/geo+json; charset=utf-8")

    def status_rows() -> list[dict]:
        cat = catalog()
        by_src = cat.counts()["by_src"] if cat.count else {}
        out = []
        for src in sources_mod.ALL:
            meta = dict(cat.sources.get(src.id) or build_mod.source_meta(src))
            meta.setdefault("ok", None)
            meta.setdefault("count", 0)
            meta["count"] = by_src.get(src.id, 0) if cat.count else meta.get("count", 0)
            if src.needs_key and not m.config.has(src.needs_key):
                meta["note"] = f"Needs {src.needs_key}"
            out.append(meta)
        return out

    @m.route("GET", "/api/cameras/summary")
    def r_summary(req):
        cat = catalog()
        building = any(d.get("building") for d in m.app.datasets.info() if d.get("key") == "cameras.cameras")
        counts = cat.counts()
        live = sum(n for k, n in counts["by_kind"].items() if k in LIVE_KINDS)
        return {"ready": cat.count > 0, "building": building, "built_at": cat.built_at, "count": cat.count,
                "live": live, "by_kind": counts["by_kind"], "by_tag": counts["by_tag"], "by_type": counts["by_type"],
                "show": counts["show"], "by_country": counts["by_country"], "sources": status_rows(),
                "proxy": proxy.cache_info(), "direct": dict(direct_cache.stats)}

    @m.route("GET", "/api/cameras/sources")
    def r_sources(req):
        return {"sources": status_rows()}

    @m.route("GET", "/api/cameras/camera/{cam_id}")
    def r_camera(req):
        raw = req.params["cam_id"]
        if not ID_RE.match(raw or ""):
            raise HTTPError(400, "BAD_ID", "That is not a camera id.")
        cat = ready_or_503()
        row = cat.get(raw)
        if row is None:
            seen = m.store.one("SELECT name, lat, lon, src, last_seen FROM cameras_lastseen WHERE id = ?", (raw,))
            return {"camera": None, "missing": True, "id": raw, "last_seen": seen, "built_at": cat.built_at}
        return detail(row)

    # images that keep failing for one source (host down or blocked from this network) show up in
    # Sources and status; the next good image clears it again
    health: dict[str, int] = {}
    HOST_ERRORS = {"UPSTREAM_TIMEOUT", "UPSTREAM_ERROR", "HOST_COOLING_DOWN", "LOOKUP_FAILED"}

    def image_health(src, row: dict, err: ProxyError | None) -> None:
        sid = f"cameras.{src.id}"
        meta = catalog().sources.get(src.id) or {}
        if err is None:
            if health.get(src.id, 0) >= 5:
                m.status(sid, ok=meta.get("ok", True), count=meta.get("count"), error=meta.get("error"),
                         note=meta.get("note"))
            health[src.id] = 0
            return
        if err.code not in HOST_ERRORS:
            return
        health[src.id] = health.get(src.id, 0) + 1
        if health[src.id] == 5:
            m.status(sid, ok=False, count=meta.get("count"), error=f"Camera images are not loading: {err.message}",
                     note=meta.get("note"))

    def failure(status: int, code: str, message: str, retry_after: float | None, fallback: bool) -> Response:
        """An error answer: JSON with the status, or (``fallback=1``, for <img> tags and the browser's
        own picture code) a neutral placeholder image with the error in headers, so that a camera
        that is down does not show a broken image or fill the console with failed loads."""
        headers = {"Retry-After": str(int(retry_after))} if retry_after else {}
        if fallback:
            headers.update({"X-Camera-Error": code, "X-Camera-Status": str(status),
                            "X-Camera-Message": json.dumps(message, ensure_ascii=True)})
            return Response(PLACEHOLDER_SVG, content_type="image/svg+xml", status=200, cache="no-store",
                            headers=headers, compressible=False)
        resp = Response.json({"error": {"code": code, "message": message}}, status=status, cache="no-store")
        resp.headers.update(headers)
        return resp

    @m.route("GET", "/api/cameras/img/{cam_id}")
    def r_img(req):
        fallback = req.arg("fallback", False, type=bool)
        try:
            row = cam_or_404(req.params["cam_id"])
        except HTTPError as err:
            if not fallback:
                raise
            return failure(err.status, err.code, err.message, 30 if err.status == 503 else None, True)
        src = source_of(row)
        if src is None:
            return failure(404, "NOT_FOUND", "This camera's source is not available.", None, fallback)
        if row.get("direct") and src.direct:
            # the source's terms: pictures only from its own links, shown by the browser (never proxied)
            return failure(409, "DIRECT_ONLY", f"{src.name} pictures load straight from {src.owner}; "
                                               "use /api/cameras/direct for the link.", None, fallback)
        if row["kind"] == "page" and not row.get("dynamic"):
            return failure(404, "NO_IMAGE", "This camera only has a page on its source website.", None, fallback)
        try:
            entry = proxy.get(row, src, src.resolve)
        except ProxyError as err:
            image_health(src, row, err)
            return failure(err.status, err.code, err.message, err.retry_after, fallback)
        image_health(src, row, None)
        left = max(1, int(entry.expires - time.time()))
        headers = {"X-Camera-Fetched": str(int(entry.fetched_at))}
        if entry.upstream_time:
            headers["X-Camera-Image-Time"] = str(int(entry.upstream_time))
        if entry.stale:
            headers["X-Camera-Stale"] = "1"
        return Response(entry.body, content_type=entry.ctype, cache=f"private, max-age={min(left, 120)}",
                        headers=headers, compressible=False)

    def direct_answer(rows: list[dict]) -> dict[str, dict]:
        """Fresh links for direct cameras, grouped by source (one upstream call per 50 cameras)."""
        out: dict[str, dict] = {}
        by_src: dict[str, list[dict]] = {}
        for row in rows:
            by_src.setdefault(row["src"], []).append(row)
        for sid, group in by_src.items():
            src = sources_mod.BY_ID.get(sid)
            if src is None or not src.direct:
                for row in group:
                    out[row["id"]] = {"ok": False, "error": "This camera's picture comes through /api/cameras/img."}
                continue
            got = direct_cache.get_many(group, src, m.net, m.config.redact)
            credit = courtesy(src)
            for row in group:
                info = dict(got.get(row["id"]) or {"ok": False, "error": "No picture link right now."})
                info["id"] = row["id"]
                info["courtesy"] = credit
                if not info.get("link") and row.get("page"):
                    info["link"] = row["page"]
                out[row["id"]] = info
        return out

    @m.route("GET", "/api/cameras/direct/{cam_id}")
    def r_direct_one(req):
        row = cam_or_404(req.params["cam_id"])
        if not row.get("direct"):
            raise HTTPError(400, "NOT_DIRECT", "This camera's picture comes through /api/cameras/img.")
        return direct_answer([row])[row["id"]]

    @m.route("GET", "/api/cameras/direct")
    def r_direct_many(req):
        cat = ready_or_503()
        raw = req.arg("ids", "")
        want = [i for i in raw.split(",") if i][:50]
        if not want:
            raise HTTPError(400, "BAD_ARG", "ids is a comma-separated list of up to 50 camera ids.")
        rows, missing = [], []
        for cid in want:
            row = cat.get(cid) if ID_RE.match(cid) else None
            if row is not None and row.get("direct"):
                rows.append(row)
            else:
                missing.append(cid)
        items = direct_answer(rows)
        for cid in missing:
            items[cid] = {"ok": False, "id": cid, "error": "Not a direct camera in the current list."}
        return {"items": items}

    @m.route("GET", "/api/cameras/labels")
    def r_labels(req):
        """Names at map positions (co-located views fanned out, one name per mast), nearest the
        centre first: the map draws them at street zoom and uses them for hover text."""
        cat = ready_or_503()
        bbox = req.bbox("bbox")
        limit = req.arg("limit", 300, type=int, min=1, max=LABELS_MAX)
        show = req.arg("show", "all", choices=SHOW_CHOICES)
        w, s, e, n = bbox
        clon = (w + e) / 2 if w <= e else geo.wrap_lon((w + e + 360) / 2)
        total, rows = cat.in_bbox(bbox, center=((s + n) / 2, clon), limit=limit, keep=show_test(show))
        feats = []
        for r in rows:
            lat, lon = cat.map_position(r)
            props = {"id": r["id"], "n": r["name"]}
            label = cat.map_label.get(r["id"])
            if label is not None and label != r["name"]:
                props["l"] = label
            place = r.get("area") or r.get("road") or r.get("region") or ""
            if place and place.lower() not in r["name"].lower():
                props["a"] = place[:48]
            k = kind_code(r)
            if k:
                props["k"] = k
            if camera_type(r) == "webcam":
                props["t"] = 1
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
                          "properties": props})
        return {"type": "FeatureCollection", "total": total, "truncated": total > len(feats), "features": feats,
                "built_at": cat.built_at}

    @m.route("GET", "/api/cameras/list")
    def r_list(req):
        cat = ready_or_503()
        keep = keep_filter(req.arg("kind", None), req.arg("tag", None), req.arg("type", None))
        limit = req.arg("limit", 60, type=int, min=1, max=200)
        ids = req.arg("ids", None)
        if ids:
            want = [i for i in ids.split(",") if i][:100]
            items = []
            for cid in want:
                row = cat.get(cid) if ID_RE.match(cid) else None
                items.append(brief(row) if row else {"id": cid, "missing": True})
            return {"count": len(items), "total": len(items), "cameras": items}
        bbox = req.bbox("bbox", None)
        if bbox is None:
            lat, lon = req.latlon()
            radius = req.arg("radius_km", 25.0, type=float, min=0.1, max=500.0)
            found = cat.near(lat, lon, radius, limit, keep)
            items = [dict(brief(r), distance_km=round(d, 2)) for d, r in found]
            return {"count": len(items), "total": len(items), "cameras": items}
        w, s, e, n = bbox
        center = None
        if req.arg("lat", None) is not None:
            center = req.latlon()
        else:
            clon = (w + e) / 2 if w <= e else geo.wrap_lon((w + e + 360) / 2)
            center = ((s + n) / 2, clon)
        total, rows = cat.in_bbox(bbox, center=center, limit=limit, keep=keep)
        return {"count": len(rows), "total": total, "cameras": [brief(r) for r in rows]}

    # -- search, nearby and the AI ----------------------------------------------------------------
    def search(q: str, limit: int):
        cat = catalog()
        if cat.count == 0:
            return []
        return [entity(r, score=s) for s, r in cat.search(q, limit)]

    def nearby(lat: float, lon: float, radius_km: float, limit: int):
        cat = catalog()
        if cat.count == 0:
            return []
        return [entity(r, distance_km=d) for d, r in cat.near(lat, lon, min(radius_km, 300.0), max(1, min(limit, 50)))]

    m.search(search)
    m.nearby(nearby)

    def ai_row(row: dict, distance: float | None = None) -> dict:
        direct = bool(row.get("direct"))
        out = {"id": row["id"], "name": row["name"], "road": row.get("road"), "area": row.get("area"),
               "region": row.get("region"), "country": row.get("country"), "lat": row["lat"], "lon": row["lon"],
               "kind": row["kind"], "type": camera_type(row), "live_video": is_live(row),
               "tags": list(row.get("tags") or []),
               "image_url": f"/api/cameras/img/{row['id']}" if (row.get("image") or (row.get("dynamic") and not direct))
               else None, "page": row.get("page"),
               "refresh_s": row.get("refresh"), "source": row.get("attribution"), "licence": row.get("licence")}
        if direct:
            out["image_note"] = "The picture shows on the camera's card (loaded from the source, linked to it)."
        if distance is not None:
            out["distance_km"] = round(distance, 2)
        if row.get("heading") is not None:
            out["facing"] = compass_label(row["heading"])
        return out

    def ai_near(args: dict):
        cat = catalog()
        try:
            lat = float(args.get("lat"))
            lon = float(args.get("lon"))
        except (TypeError, ValueError):
            return {"error": "lat and lon are required numbers"}
        if not geo.valid_latlon(lat, lon):
            return {"error": "lat/lon out of range"}
        radius = max(0.1, min(300.0, float(args.get("radius_km") or 10.0)))
        limit = max(1, min(50, int(args.get("limit") or 10)))
        try:
            keep = keep_filter(args.get("kind"), args.get("tag"), args.get("type"))
        except HTTPError as err:
            return {"error": err.message}
        found = cat.near(lat, lon, radius, limit, keep)
        return {"ready": cat.count > 0, "count": len(found), "radius_km": radius,
                "cameras": [ai_row(r, d) for d, r in found],
                "note": "Images are the latest frame only; open a camera with its id to see it."}

    def ai_search(args: dict):
        cat = catalog()
        q = str(args.get("query") or "").strip()[:120]
        if len(q) < 2:
            return {"error": "query needs at least 2 characters"}
        limit = max(1, min(50, int(args.get("limit") or 10)))
        found = cat.search(q, limit)
        return {"ready": cat.count > 0, "count": len(found), "cameras": [ai_row(r) for _, r in found]}

    m.ai_tool("cameras_near",
              "List public cameras near a point, nearest first: road and traffic cameras, and webcams (scenic, "
              "weather, city, volcano, polar). Returns id, name, place, kind (image or live video), type (traffic "
              "or webcam), tags, distance and an image URL where the picture is proxied.",
              {"type": "object", "properties": {
                  "lat": {"type": "number", "minimum": -90, "maximum": 90},
                  "lon": {"type": "number", "minimum": -180, "maximum": 180},
                  "radius_km": {"type": "number", "minimum": 0.1, "maximum": 300, "default": 10},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                  "kind": {"type": "string", "enum": ["any", "live", "still"]},
                  "type": {"type": "string", "enum": ["any", "traffic", "webcam"]},
                  "tag": {"type": "string", "enum": list(TAGS)}},
               "required": ["lat", "lon"], "additionalProperties": False},
              ai_near)
    m.ai_tool("cameras_search",
              "Search public cameras and webcams by name, road, city, region or country (for example 'A40', "
              "'Hellisheidi', 'Golden Gate', 'Zermatt'). Returns matching cameras with id, place, kind and type.",
              {"type": "object", "properties": {
                  "query": {"type": "string", "minLength": 2, "maxLength": 120},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
               "required": ["query"], "additionalProperties": False},
              ai_search)

    # -- layer table ------------------------------------------------------------------------------
    m.layer(LAYER, "Public cameras", "Cameras",
            description="Official public traffic, weather, mountain and city cameras, webcams worldwide (Windy, with "
                        "a key) and volcano, polar and park cameras. Click one for its latest image.",
            source="TfL, Caltrans, IDOT, Iowa DOT, DGT, Fintraffic, HK Transport Department, DriveBC, ALERTCalifornia, "
                   "Vegagerðin, Alaska DOT&PF, NZTA, York Region, Austin, Calgary, Newfoundland and Labrador, "
                   "data.gov.sg, USGS, GeoNet, Australian Antarctic Division, Windy.com (optional key) and others",
            refresh="camera list daily, Windy catalogue weekly; images on demand at each camera's cadence",
            use="Check road, weather and visibility conditions anywhere the feeds cover",
            count=lambda: catalog().count)
