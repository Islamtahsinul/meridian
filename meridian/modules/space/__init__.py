"""Space, weather and imagery: satellites (CelesTrak + SGP4), the ISS and its passes, launches
(The Space Devs), rain radar (RainViewer), NASA GIBS weather and imagery layers, Meteosat imagery and
lightning (EUMETSAT EUMETView, see meteosat.py), point weather (Open-Meteo), the satellite scene
finder (STAC catalogues, see scenes.py), and space weather and the aurora forecast (NOAA SWPC, see
spaceweather.py).

Python API for other modules (import lazily inside functions)::

    from meridian.modules import space
    space.iss_now()                                   # {"lat", "lon", "alt_km", ...} or None
    space.passes(51.5, -0.13, hours=24)               # ISS passes over a point (list of dicts)
"""

from __future__ import annotations

import gzip
import hashlib
import math
import re
import threading
import time
from datetime import datetime, timezone

from meridian import geo
from meridian.registry import HTTPError, Response, json_bytes

from . import catalog as C
from . import imagery as I
from . import launches as L
from . import meteosat as MS
from . import scenes as SC
from . import sgp4 as S
from . import spaceweather as SW
from . import weather as W

ISS = 25544
ISS_STD_MAG = -1.8          # intrinsic brightness used for the rough magnitude estimate
TWILIGHT_DEG = -6.0
_STATE: dict = {}           # filled by register(); used by the small Python API below


def _iso(t: float | None) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _r5(x: float) -> float:
    return round(float(x), 5)


# ---------------------------------------------------------------------------------------------
# Python API
# ---------------------------------------------------------------------------------------------
def iss_now() -> dict | None:
    fn = _STATE.get("iss_now")
    return fn() if fn else None


def passes(lat: float, lon: float, hours: float = 24.0, norad: int = ISS, visible_only: bool = True) -> list[dict]:
    fn = _STATE.get("passes")
    return fn(lat, lon, hours, norad, visible_only) if fn else []


# ---------------------------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------------------------
def register(m) -> None:
    net = m.net
    net.set_host_interval("ll.thespacedevs.com", 4.0)
    net.set_host_interval("celestrak.org", 1.0)
    net.set_host_interval("view.eumetsat.int", 1.0)                   # capabilities only; tiles go browser-direct
    net.set_host_interval("earth-search.aws.element84.com", 0.3)
    net.set_host_interval("stac.dataspace.copernicus.eu", 0.5)
    net.set_host_interval("planetarycomputer.microsoft.com", 0.3)

    def status_for(sid):
        return lambda **kw: m.status(sid, **kw)

    cat = C.Catalog(net, m.raw_dir, m.log, status=status_for("space.celestrak"))
    launches = L.Launches(net, m.raw_dir, m.log, status=status_for("space.ll2"))
    meteosat = MS.Meteosat(net, m.log, status=status_for("space.eumetsat"))
    gibs = I.Gibs(net, m.log, status=status_for("space.gibs"), wms=meteosat)
    scene_sources = {"sentinel-2": "space.earthsearch", "sentinel-1": "space.cdse", "landsat": "space.planetary"}
    scenes = SC.Scenes(net, m.log, status=lambda mission, **kw: m.status(scene_sources[mission], **kw))
    pads_lock = threading.Lock()
    pads_state: dict = {"pads": {}, "index": geo.GridIndex(2.0)}

    # -- sources --------------------------------------------------------------------------------
    m.source("space.celestrak", "CelesTrak GP element sets (satellite orbits)", "https://celestrak.org/NORAD/elements/",
             "Free public data (US Space Force catalogue via CelesTrak); each group fetched at most every 2 hours",
             refresh="each group at most every 2 h, only while used", layer="space.satellites")
    m.source("space.ll2", "The Space Devs Launch Library 2 (launches)", "https://thespacedevs.com/llapi",
             "Free tier (about 15 requests an hour); credit The Space Devs", refresh="hourly while used",
             layer="space.launches")
    m.source("space.rainviewer", "RainViewer weather radar", "https://www.rainviewer.com/api.html",
             "Free for personal and educational use; credit 'Weather data by RainViewer'",
             refresh="frames every 10 min, list cached 5 min", layer="space.radar")
    m.source("space.gibs", "NASA GIBS (Global Imagery Browse Services)", "https://www.earthdata.nasa.gov/gibs",
             "NASA EOSDIS open data, no restrictions; credit NASA GIBS", refresh="daily or every 10 to 30 min by product",
             layer="space.daily")
    m.source("space.openmeteo", "Open-Meteo forecast (point weather)", "https://open-meteo.com/",
             "CC BY 4.0, free for non-commercial use", refresh="on request, cached 10 min")
    m.source("space.eumetsat", "EUMETSAT EUMETView (Meteosat Third Generation imagery and lightning)", MS.POLICY_URL,
             "EUMETSAT Data Policy: Meteosat image products are Core data, free and unrestricted under CC BY 4.0; "
             "credit 'EUMETSAT Meteosat product <year>'",
             refresh="frames every 5 to 10 min; capabilities cached 10 min, tiles straight from EUMETView",
             layer="space.meteosat")
    m.source("space.earthsearch", "Element 84 Earth Search (Sentinel-2 scenes)", SC.MISSIONS["sentinel-2"]["catalogue_url"],
             "Free public STAC API; Copernicus Sentinel data free, full and open; credit 'Contains modified "
             "Copernicus Sentinel data <year>'", refresh="on request only, cached 1 h", layer="space.scenes")
    m.source("space.cdse", "Copernicus Data Space Ecosystem STAC (Sentinel-1 scenes)",
             SC.MISSIONS["sentinel-1"]["catalogue_url"],
             "Copernicus Sentinel data: free, full and open; credit 'Contains modified Copernicus Sentinel data <year>'",
             refresh="on request only, cached 1 h", layer="space.scenes")
    m.source("space.planetary", "Microsoft Planetary Computer STAC (Landsat scenes)",
             SC.MISSIONS["landsat"]["catalogue_url"],
             "Landsat: public domain (USGS); previews rendered by Planetary Computer; credit 'Landsat imagery courtesy "
             "of the U.S. Geological Survey'", refresh="on request only, cached 1 h", layer="space.scenes")

    # -- start-up: everything we already have on disk -----------------------------------------
    try:
        n = cat.load_disk()
        if n:
            cat.report()
    except Exception as err:  # a bad cache file never stops the module
        m.log.warning("could not load the satellite cache: %s", err)
    try:
        launches.load_disk()
    except Exception as err:
        m.log.warning("could not load the launch cache: %s", err)

    # -- pads dataset (weekly) -------------------------------------------------------------------
    def build_pads(ds):
        import json as _json
        first = ds.download(f"{L.PADS_URL}?limit=100", "ll2/pads-1.json", min_bytes=200, timeout=60)
        bodies = [first.read_bytes()]
        total = int((_json.loads(bodies[0].decode("utf-8-sig")) or {}).get("count") or 0)
        pages = min(6, max(1, math.ceil(total / 100)))
        for n in range(2, pages + 1):
            p = ds.download(f"{L.PADS_URL}?limit=100&offset={(n - 1) * 100}", f"ll2/pads-{n}.json",
                            min_bytes=50, timeout=60)
            bodies.append(p.read_bytes())
        pads = L.parse_pads_pages(bodies)
        if len(pads) < 20:
            raise RuntimeError(f"only {len(pads)} launch pads came back")
        ds.write_json("pads.json", {"built_at": time.time(), "pads": pads})
        ds.set_rows(len(pads))
        ds.note(f"{len(pads)} launch pads")

    m.dataset("pads", build_pads, max_age_hours=24 * 14, description="Launch pads from Launch Library 2")

    def load_pads():
        import json as _json
        data = _json.loads((m.data_dir / "pads.json").read_text(encoding="utf-8"))
        idx = geo.GridIndex(2.0)
        pads = {}
        for p in data.get("pads") or []:
            pads[p["id"]] = p
            idx.add(p["lat"], p["lon"], p["id"])
        with pads_lock:
            pads_state["pads"] = pads
            pads_state["index"] = idx

    m.on_dataset("pads", load_pads)

    def all_pads() -> dict[int, dict]:
        with pads_lock:
            out = dict(pads_state["pads"])
        for pid, p in launches.pads().items():
            out.setdefault(pid, p)
        return out

    # -- satellites: helpers -------------------------------------------------------------------
    def sat_state(rec: dict, t: float | None = None) -> dict | None:
        t = time.time() if t is None else t
        try:
            st = S.state_at(cat.satrec(rec), t)
        except (S.PropagationError, ValueError, ZeroDivisionError, OverflowError):
            return None
        return {"time": round(t, 1), "lat": _r5(st.lat), "lon": _r5(st.lon), "alt_km": round(st.alt_km, 1),
                "speed_km_s": round(st.speed_km_s, 3), "sunlit": st.sunlit,
                "footprint_radius_km": round(S.footprint_radius_km(st.alt_km), 0)}

    def sat_detail(rec: dict) -> dict:
        s = cat.satrec(rec)
        info = S.orbit_info(s)
        now = time.time()
        return {
            "id": rec["id"], "name": rec["name"], "cospar": rec["cospar"], "epoch": rec["epoch"],
            "epoch_unix": rec["epoch_unix"], "elements_age_h": round((now - rec["epoch_unix"]) / 3600.0, 1),
            "row": C.row(rec), "fields": C.ROW_FIELDS, "groups": list(cat.groups_of(rec["id"])),
            "family": cat.family_of(rec["id"]),
            "orbit": {"period_min": round(info["period_min"], 2), "perigee_km": round(info["perigee_km"], 1),
                      "apogee_km": round(info["apogee_km"], 1), "inclination_deg": round(info["inclination_deg"], 3),
                      "eccentricity": round(info["eccentricity"], 7), "orbit_class": info["orbit_class"],
                      "deep_space": info["deep_space"]},
            "position": sat_state(rec, now),
        }

    def sat_entity(rec: dict, state: dict | None, score: float | None = None) -> dict:
        info = S.orbit_info(cat.satrec(rec))
        sub = f"NORAD {rec['id']}, {info['orbit_class']}"
        if state:
            sub += f", {state['alt_km']:,.0f} km up"
        e = {"kind": "satellite", "id": str(rec["id"]), "title": rec["name"], "subtitle": sub,
             "layer": "space.iss" if rec["id"] == ISS else "space.satellites", "time": round(time.time()), "zoom": 3}
        if state:
            e["lat"], e["lon"] = state["lat"], state["lon"]
        if score is not None:
            e["score"] = round(score, 3)
        return e

    def iss_record(fetch: bool = True) -> dict | None:
        rec = cat.lookup(ISS)
        age = cat.age("stations")
        if fetch and (rec is None or age is None or age > 6 * 3600):
            cat.ensure("stations", max_age=6 * 3600, block=rec is None)
            rec = cat.lookup(ISS)
        if rec is None and fetch:
            rec = cat.fetch_catnr(ISS)
        return rec

    def iss_now_fn() -> dict | None:
        rec = iss_record()
        if rec is None:
            return None
        st = sat_state(rec)
        if st is None:
            return None
        st.update({"norad_id": ISS, "name": rec["name"], "elements_epoch": rec["epoch"],
                   "elements_age_h": round((time.time() - rec["epoch_unix"]) / 3600.0, 1),
                   "speed_kmh": round(st["speed_km_s"] * 3600.0)})
        return st

    pass_cache: dict = {}
    pass_lock = threading.Lock()

    def compute_passes(lat: float, lon: float, hours: float, norad: int, visible_only: bool,
                       min_el: float = 10.0) -> tuple[dict | None, list[dict]]:
        rec = iss_record() if norad == ISS else (cat.lookup(norad) or cat.fetch_catnr(norad))
        if rec is None:
            return None, []
        key = (norad, rec["epoch_unix"], round(lat, 2), round(lon, 2), round(hours, 1), round(min_el, 1),
               bool(visible_only), int(time.time() // 300))
        with pass_lock:
            hit = pass_cache.get(key)
        if hit is not None:
            return rec, hit
        s = cat.satrec(rec)
        out = S.find_passes(s, lat, lon, time.time(), hours, min_el=min_el, visible_only=visible_only,
                            twilight_deg=TWILIGHT_DEG, std_mag=ISS_STD_MAG if norad == ISS else None, max_passes=40)
        for p in out:
            for k in ("rise", "max", "set"):
                p[k]["iso"] = _iso(p[k]["time"])
            if p.get("visible_from"):
                p["visible_from_iso"] = _iso(p["visible_from"])
                p["visible_to_iso"] = _iso(p["visible_to"])
        with pass_lock:
            pass_cache[key] = out
            while len(pass_cache) > 300:
                pass_cache.pop(next(iter(pass_cache)))
        return rec, out

    _STATE["iss_now"] = iss_now_fn
    _STATE["passes"] = lambda lat, lon, hours, norad, vis: compute_passes(lat, lon, hours, norad, vis)[1]

    # -- routes: satellites ----------------------------------------------------------------------
    group_payloads: dict[str, tuple] = {}

    def group_response(group: str) -> Response:
        info = cat.info(group)
        key = (info["fetched_at"], info["count"])
        hit = group_payloads.get(group)
        if hit and hit[0] == key:
            body, gz, etag = hit[1], hit[2], hit[3]
        else:
            recs = cat.records([group])
            payload = {"group": group, "info": info, "fields": C.ROW_FIELDS, "sats": [C.row(r) for r in recs]}
            body = json_bytes(payload)
            gz = gzip.compress(body, compresslevel=6) if len(body) > 1024 else None
            etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
            group_payloads[group] = (key, body, gz, etag)
        resp = Response(body, content_type="application/json; charset=utf-8", cache="no-cache", etag=etag)
        resp.gzipped = gz
        return resp

    @m.route("GET", "/api/space/groups")
    def r_groups(req):
        return {"groups": [dict(C.GROUP_BY_ID[g], **cat.info(g)) for g in C.GROUP_IDS],
                "families": C.FAMILY_TITLES}

    @m.route("GET", "/api/space/satellites")
    def r_satellites(req):
        group = req.arg("group", None)
        if group is None:
            raise HTTPError(400, "MISSING_ARG", 'Missing query parameter "group" (one CelesTrak group id).')
        if group not in C.GROUP_BY_ID:
            raise HTTPError(400, "BAD_ARG", f"Unknown satellite group. Choose one of {', '.join(C.GROUP_IDS)}.")
        cat.ensure(group, block=True)
        if not cat.has(group):
            err = cat.info(group).get("error") or "CelesTrak is not answering right now."
            raise HTTPError(502, "UPSTREAM", f"Satellite group {group} is unavailable: {err}")
        return group_response(group)

    @m.route("GET", "/api/space/satellite/{norad}")
    def r_satellite(req):
        raw = req.params["norad"]
        # ASCII digits only: str.isdigit() also accepts "²" or Arabic-Indic digits, and int() then fails (a 500)
        if not (raw.isascii() and raw.isdigit() and len(raw) <= 7) or not (0 < int(raw) < 10_000_000):
            raise HTTPError(400, "BAD_ARG", "A NORAD catalogue number is a positive whole number.")
        norad = int(raw)
        rec = cat.lookup(norad) or cat.fetch_catnr(norad)
        if rec is None:
            raise HTTPError(404, "NOT_FOUND", f"No current orbit for NORAD {norad} at CelesTrak "
                                              "(it may have decayed or be classified).")
        return sat_detail(rec)

    @m.route("GET", "/api/space/iss")
    def r_iss(req):
        now = iss_now_fn()
        if now is None:
            raise HTTPError(502, "UPSTREAM", "The ISS orbit is unavailable right now (CelesTrak is not answering).")
        rec = cat.lookup(ISS)
        if rec is not None:
            now["orbit"] = sat_detail(rec)["orbit"]
        return now

    @m.route("GET", "/api/space/passes")
    def r_passes(req):
        lat, lon = req.latlon()
        hours = req.arg("hours", 48.0, type=float, min=1.0, max=72.0)
        norad = req.arg("norad", ISS, type=int, min=1, max=9_999_999)
        min_el = req.arg("min_el", 10.0, type=float, min=0.0, max=80.0)
        visible = req.arg("visible", True, type=bool)
        rec, out = compute_passes(lat, lon, hours, norad, visible, min_el)
        if rec is None:
            raise HTTPError(404 if norad != ISS else 502, "NOT_FOUND" if norad != ISS else "UPSTREAM",
                            "No orbit is available for that satellite right now.")
        return {"norad_id": norad, "name": rec["name"], "lat": _r5(lat), "lon": _r5(lon), "hours": hours,
                "min_elevation": min_el, "visible_only": visible, "twilight_deg": TWILIGHT_DEG,
                "elements_epoch": rec["epoch"], "elements_age_h": round((time.time() - rec["epoch_unix"]) / 3600, 1),
                "deep_space": cat.satrec(rec).deep, "passes": out,
                "note": "SGP4 on CelesTrak elements. Visible means sunlit while the Sun is at least 6° below "
                        "your horizon. Times are good to about a minute for a few days."}

    # -- routes: launches --------------------------------------------------------------------
    def launch_json(rec: dict) -> dict:
        out = dict(rec)
        out["phase"] = L.phase(rec)
        out["net_iso"] = _iso(rec["net"])
        out["tone"] = L.STATUS_TONE.get(rec["status"], "grey")
        return out

    @m.route("GET", "/api/space/launches")
    def r_launches(req):
        which = req.arg("which", "all", choices={"all", "upcoming", "previous"})
        launches.ensure("upcoming")
        if which in ("all", "previous"):
            launches.ensure("previous")
        recs = launches.all()
        now = time.time()
        if which == "upcoming":
            recs = [r for r in recs if L.phase(r, now) != "past"]
        elif which == "previous":
            recs = [r for r in recs if L.phase(r, now) == "past"]
        info = launches.info()
        if not recs and info["upcoming"]["error"]:
            raise HTTPError(502, "UPSTREAM", f"Launch Library is unavailable: {info['upcoming']['error']}")
        return {"now": now, "info": info, "launches": [launch_json(r) for r in recs]}

    @m.route("GET", "/api/space/launch/{lid}")
    def r_launch(req):
        lid = req.params["lid"]
        rec = launches.get(lid) or launches.fetch_one(lid)
        if rec is None:
            raise HTTPError(404, "NOT_FOUND", "That launch is not in the Launch Library lists right now.")
        out = launch_json(rec)
        out["same_pad"] = [launch_json(r) for r in launches.all() if r["pad"]["id"] == rec["pad"]["id"]
                           and r["id"] != rec["id"]][:8]
        return out

    @m.route("GET", "/api/space/pad/{pid}")
    def r_pad(req):
        raw = req.params["pid"].replace("ll2pad:", "")
        if not (raw.isascii() and raw.isdigit() and len(raw) <= 9):
            raise HTTPError(400, "BAD_ARG", "A pad id is a whole number.")
        pid = int(raw)
        pad = all_pads().get(pid)
        if pad is None:
            raise HTTPError(404, "NOT_FOUND", "That launch pad is not known.")
        at_pad = [launch_json(r) for r in launches.all() if r["pad"]["id"] == pid]
        return {"pad": pad, "launches": at_pad}

    # -- routes: weather, radar, GIBS ----------------------------------------------------------
    def weather_at(lat: float, lon: float) -> dict:
        try:
            data = net.get_json(W.FORECAST_URL, params=W.params(lat, lon), ttl=W.TTL_S, timeout=12, retries=1)
            out = W.parse(data, lat, lon)
            m.status("space.openmeteo", ok=True)
            return out
        except Exception as err:
            m.status("space.openmeteo", ok=False, error=str(err))
            raise

    @m.route("GET", "/api/space/weather")
    def r_weather(req):
        lat, lon = req.latlon()
        try:
            return weather_at(lat, lon)
        except Exception:
            raise HTTPError(502, "UPSTREAM", "Open-Meteo is not answering right now.") from None

    @m.route("GET", "/api/space/radar")
    def r_radar(req):
        try:
            data = net.get_json(I.RAINVIEWER_URL, ttl=300, timeout=10, retries=1)
            out = I.parse_rainviewer(data)
            if not out["frames"]:
                raise ValueError("RainViewer listed no radar frames")
            m.status("space.rainviewer", ok=True, count=len(out["frames"]),
                     note=f"{len(out['frames'])} frames, newest {_iso(out['frames'][-1]['time'])}")
            return out
        except Exception as err:
            m.status("space.rainviewer", ok=False, error=str(err))
            raise HTTPError(502, "UPSTREAM", "RainViewer is not answering right now.") from None

    @m.route("GET", "/api/space/gibs")
    def r_gibs(req):
        if req.arg("meta", False, type=bool):
            # titles, periods, variants and legends only (instant): the browser registers its layers from this
            return {"base": I.GIBS_BASE, "credit": I.GIBS_CREDIT, "meteosat_credit": MS.credit(),
                    "compare": I.COMPARE, "products": {pid: I.product_meta(pid) for pid in I.PRODUCTS}}
        raw = req.arg("products", None)
        products = None
        if raw:
            products = [p for p in raw.split(",") if p]
            bad = [p for p in products if p not in I.PRODUCTS]
            if bad or len(products) > 30:
                raise HTTPError(400, "BAD_ARG", f"Unknown GIBS product. Choose from {', '.join(I.PRODUCTS)}.")
        out = gibs.catalogue(products)
        out["meteosat_credit"] = MS.credit()
        return out

    # -- routes: satellite scene finder (on request only) ----------------------------------------
    def scene_error(err: SC.SceneError) -> HTTPError:
        code = {400: "BAD_ARG", 404: "NOT_FOUND"}.get(err.status, "UPSTREAM")
        return HTTPError(err.status, code, str(err))

    @m.route("GET", "/api/space/scenes")
    def r_scenes(req):
        bbox = req.bbox("bbox", None)
        point = req.latlon() if (req.arg("lat", None) is not None or req.arg("lon", None) is not None) else None
        if bbox is None and point is None:
            raise HTTPError(400, "MISSING_ARG", "Give lat and lon, or a bbox (west,south,east,north).")
        days = req.arg("days", SC.DEFAULT_DAYS, type=int, min=1, max=SC.MAX_DAYS)
        max_cloud = req.arg("max_cloud", float(SC.DEFAULT_CLOUD), type=float, min=0.0, max=100.0)
        limit = req.arg("limit", SC.DEFAULT_LIMIT, type=int, min=1, max=SC.MAX_LIMIT)
        missions = [x.strip() for x in req.arg("missions", ",".join(SC.ORDER)).split(",") if x.strip()]
        if not missions or len(missions) > len(SC.ORDER) or any(x not in SC.MISSIONS for x in missions):
            raise HTTPError(400, "BAD_ARG", "missions is a comma-separated list of sentinel-2, sentinel-1 and landsat.")
        out = scenes.search(point=point, bbox=bbox, days=days, max_cloud=max_cloud, missions=missions, limit=limit)
        if not any(r["ok"] for r in out["missions"].values()):
            raise HTTPError(502, "UPSTREAM", "None of the scene catalogues is answering right now.")
        return out

    @m.route("GET", "/api/space/scenes/quicklook/{mission}/{item_id}")
    def r_quicklook(req):
        try:
            body, kind = scenes.quicklook(req.params["mission"], req.params["item_id"])
        except SC.SceneError as err:
            raise scene_error(err) from None
        return Response(body, content_type=kind, cache="max-age=86400", compressible=False)

    # -- search, nearby, events ----------------------------------------------------------------
    def search(q: str, limit: int):
        out = []
        for score, rec in cat.search(q, limit=min(limit, 12)):
            out.append(sat_entity(rec, sat_state(rec), score))
        ql = q.strip().lower()
        if len(ql) >= 3:
            now = time.time()
            word = re.compile(r"(?:^|[^a-z0-9])" + re.escape(ql))
            for rec in launches.all():
                hay = " ".join((rec["mission"], rec["name"], rec["rocket"], rec["provider"])).lower()
                if not word.search(hay):
                    continue
                e = L.entity(rec, now)
                upcoming = L.phase(rec, now) != "past"
                if rec["rocket"].lower().startswith(ql):
                    base = 0.8
                elif rec["mission"].lower().startswith(ql):
                    base = 0.75
                else:
                    base = 0.45
                e["score"] = round(base + (0.08 if upcoming else 0.0), 3)
                when = _iso(rec["net"]).replace("T", " ")[:16] + " UTC"
                e["subtitle"] = f"{e['subtitle']}, {'NET' if upcoming else 'launched'} {when}"
                out.append(e)
            for pad in all_pads().values():
                if pad["name"].lower().startswith("unknown"):
                    continue
                if word.search(pad["name"].lower()) or word.search((pad.get("location") or "").lower()):
                    out.append(pad_entity(pad, 0.4))
        out.sort(key=lambda e: -(e.get("score") or 0))
        return out[:limit]

    def pad_entity(pad: dict, score: float | None = None) -> dict:
        nxt = next((r for r in launches.all() if r["pad"]["id"] == pad["id"] and L.phase(r) != "past"), None)
        sub = pad.get("location") or "Launch pad"
        if nxt:
            sub += f", next: {nxt['mission'] or nxt['name']} {(_iso(nxt['net']) or '')[:10]}"
        e = {"kind": "launch_pad", "id": f"ll2pad:{pad['id']}", "title": pad["name"], "subtitle": sub,
             "lat": pad["lat"], "lon": pad["lon"], "layer": "space.launches", "zoom": 11}
        if score is not None:
            e["score"] = score
        return e

    def nearby(lat: float, lon: float, radius_km: float, limit: int):
        pads = all_pads()
        with pads_lock:
            idx = pads_state["index"]
        found = {}
        for d, pid in idx.near(lat, lon, radius_km, limit * 2):
            if pid in pads:
                found[pid] = d
        for pid, p in pads.items():   # pads only known from the launch lists
            if pid not in found:
                d = geo.haversine_km(lat, lon, p["lat"], p["lon"])
                if d <= radius_km:
                    found[pid] = d
        out = []
        busy = {r["pad"]["id"] for r in launches.all()}
        ranked = [(pid, d) for pid, d in sorted(found.items(), key=lambda kv: kv[1])
                  if pid in busy or not pads[pid]["name"].lower().startswith("unknown")]
        for pid, d in ranked[:limit]:
            e = pad_entity(pads[pid])
            e["distance_km"] = round(d, 2)
            out.append(e)
        return out

    def events(since_ts: float):
        now = time.time()
        if (launches.age("upcoming") or 1e9) > L.MAX_AGE_IDLE["upcoming"]:
            launches.refresh_async()
        out = []
        for rec in launches.all():
            ph = L.phase(rec, now)
            t = rec["net"]
            if t < since_ts:
                continue
            if ph != "past" and t <= now + 24 * 3600:
                sev = 3 if rec["crewed"] else 2
                e = L.entity(rec, now)
                e.update({"severity": sev, "subtitle": f"{e['subtitle']}, {rec['status_name'] or rec['status']}"})
                out.append(e)
            elif ph == "past" and t <= now:
                sev = 5 if rec["status"] == "Failure" else 4 if rec["status"] == "Partial Failure" else 2
                e = L.entity(rec, now)
                e.update({"severity": sev, "subtitle": f"{e['subtitle']}, {rec['status_name'] or rec['status']}"})
                out.append(e)
        return out

    m.search(search)
    m.nearby(nearby)
    m.events(events)

    # -- AI tools -------------------------------------------------------------------------------
    def _num_arg(args, name, default, lo, hi):
        v = args.get(name, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{name} must be a number")
        if not (lo <= v <= hi):
            raise ValueError(f"{name} must be between {lo} and {hi}")
        return float(v)

    def ai_iss(args):
        now = iss_now_fn()
        if now is None:
            return {"error": "The ISS orbit is unavailable right now."}
        now["time_iso"] = _iso(now["time"])
        now["note"] = "Position from SGP4 on CelesTrak elements; accurate to about a kilometre."
        return now

    def ai_passes(args):
        try:
            lat = _num_arg(args, "lat", None, -90, 90)
            lon = _num_arg(args, "lon", None, -180, 180)
            hours = _num_arg(args, "hours", 24, 1, 72)
            min_el = _num_arg(args, "min_elevation", 10, 0, 80)
            norad = args.get("norad_id", ISS)
            if isinstance(norad, float) and norad.is_integer():
                norad = int(norad)
            if isinstance(norad, str) and norad.strip().isdigit():
                norad = int(norad.strip())
            if isinstance(norad, bool) or not isinstance(norad, int) or not (0 < norad < 10_000_000):
                raise ValueError("norad_id must be a positive whole number")
            visible = args.get("visible_only", True)
            if not isinstance(visible, bool):
                raise ValueError("visible_only must be true or false")
        except ValueError as err:
            return {"error": str(err)}
        rec, out = compute_passes(lat, lon, hours, norad, visible, min_el)
        if rec is None:
            return {"error": f"No orbit is available for NORAD {norad}."}
        return {"satellite": {"norad_id": norad, "name": rec["name"]}, "observer": {"lat": lat, "lon": lon},
                "hours": hours, "min_elevation": min_el, "visible_only": visible, "count": len(out),
                "passes": out[:20],
                "accuracy": "SGP4 on CelesTrak elements (elements "
                            f"{round((time.time() - rec['epoch_unix']) / 3600, 1)} h old). Near-Earth satellites: rise "
                            "and set within about a minute for a few days. Deep-space orbits use a simplified model "
                            "(no lunar-solar terms), still well within a degree of sky position."}

    def ai_launches(args):
        try:
            hours = _num_arg(args, "hours", 168, 1, 720)
            limit = int(_num_arg(args, "limit", 10, 1, 40))
        except ValueError as err:
            return {"error": str(err)}
        provider = str(args.get("provider") or "").strip().lower()[:80]
        country = str(args.get("country") or "").strip().upper()[:3]
        launches.ensure("upcoming")
        now = time.time()
        out = []
        for rec in launches.all():
            if L.phase(rec, now) == "past" or rec["net"] > now + hours * 3600:
                continue
            if provider and provider not in rec["provider"].lower() and provider not in rec["provider_abbrev"].lower():
                continue
            if country and country not in (rec["provider_country"].upper(), rec["pad"]["country_code"].upper()):
                continue
            out.append({"id": rec["id"], "mission": rec["mission"], "rocket": rec["rocket"], "provider": rec["provider"],
                        "net_utc": _iso(rec["net"]), "net_precision": rec["net_precision"], "status": rec["status_name"],
                        "probability": rec["probability"], "pad": rec["pad"]["name"], "location": rec["pad"]["location"],
                        "lat": rec["pad"]["lat"], "lon": rec["pad"]["lon"], "orbit": rec["orbit"],
                        "mission_type": rec["mission_type"], "crewed": rec["crewed"],
                        "webcast": rec["videos"][0]["url"] if rec["videos"] else None})
            if len(out) >= limit:
                break
        return {"now_utc": _iso(now), "count": len(out), "launches": out, "source": "The Space Devs Launch Library 2"}

    def ai_weather(args):
        try:
            lat = _num_arg(args, "lat", None, -90, 90)
            lon = _num_arg(args, "lon", None, -180, 180)
        except ValueError as err:
            return {"error": str(err)}
        try:
            w = weather_at(lat, lon)
        except Exception:
            return {"error": "Open-Meteo is not answering right now."}
        hourly = [{"time_utc": _iso(h["time"]), "temp_c": h["temp_c"], "precip_mm": h["precip_mm"],
                   "precip_prob": h["precip_prob"], "wind_kmh": h["wind_kmh"], "gust_kmh": h["gust_kmh"],
                   "text": h["text"]} for h in w["hourly"][::3]]
        return {"summary": W.text_summary(w), "current": w["current"], "next_24h": w["summary"],
                "every_3_hours": hourly, "timezone": w["timezone"], "source": w["source"]}

    def ai_scenes(args):
        try:
            lat = _num_arg(args, "lat", None, -90, 90)
            lon = _num_arg(args, "lon", None, -180, 180)
            days = int(_num_arg(args, "days", SC.DEFAULT_DAYS, 1, SC.MAX_DAYS))
            max_cloud = _num_arg(args, "max_cloud", SC.DEFAULT_CLOUD, 0, 100)
            raw = args.get("missions", list(SC.ORDER))
            missions = [x.strip() for x in raw.split(",")] if isinstance(raw, str) else raw
            if not isinstance(missions, list) or not missions or any(m_ not in SC.MISSIONS for m_ in missions):
                raise ValueError("missions must be a list of sentinel-2, sentinel-1 and landsat")
        except ValueError as err:
            return {"error": str(err)}
        res = scenes.search(point=(lat, lon), days=days, max_cloud=max_cloud, missions=missions, limit=4)
        out = {}
        for mission, r in res["missions"].items():
            out[mission] = {"ok": r["ok"], "error": r["error"], "count": len(r["scenes"]), "catalogue": SC.MISSIONS[mission]["catalogue"],
                            "scenes": [{"id": s_["id"], "satellite": s_["platform"], "time_utc": s_["datetime"],
                                        "cloud_cover_pct": s_["cloud"], "detail": s_["detail"],
                                        "covers_point": s_.get("covers_point"), "bbox": s_["bbox"],
                                        "viewer": s_["links"][0]["href"] if s_["links"] else None}
                                       for s_ in r["scenes"]]}
        return {"point": {"lat": round(lat, 4), "lon": round(lon, 4)}, "days": days,
                "max_cloud_pct": max_cloud if max_cloud < 100 else None, "missions": out,
                "note": "Latest scenes first. Cloud cover is for the whole scene, not the point. Sentinel-1 is radar "
                        "and sees through cloud. Quicklooks and footprints are in the map's scene finder (toolbar: Scenes).",
                "credits": [c["text"] for c in res["credits"]]}

    m.ai_tool("iss_position",
              "Where the International Space Station is right now: latitude, longitude, altitude (km), speed, "
              "whether it is in sunlight, and the age of the orbital elements.",
              {"type": "object", "properties": {}, "additionalProperties": False}, ai_iss)
    m.ai_tool("satellite_passes",
              "Predict passes of a satellite over a point (default the ISS, NORAD 25544): rise, highest point and set "
              "times in UTC with directions and elevation. visible_only keeps passes where the satellite is sunlit while "
              "the observer is in darkness (Sun 6 degrees or more below the horizon).",
              {"type": "object", "properties": {
                  "lat": {"type": "number", "minimum": -90, "maximum": 90},
                  "lon": {"type": "number", "minimum": -180, "maximum": 180},
                  "hours": {"type": "number", "minimum": 1, "maximum": 72, "default": 24},
                  "norad_id": {"type": "integer", "minimum": 1, "description": "NORAD catalogue number"},
                  "min_elevation": {"type": "number", "minimum": 0, "maximum": 80, "default": 10},
                  "visible_only": {"type": "boolean", "default": True}},
               "required": ["lat", "lon"], "additionalProperties": False}, ai_passes)
    m.ai_tool("upcoming_launches",
              "Orbital launches coming up (The Space Devs Launch Library 2): mission, rocket, provider, pad and "
              "position, NET time in UTC, status, probability and webcast link.",
              {"type": "object", "properties": {
                  "hours": {"type": "number", "minimum": 1, "maximum": 720, "default": 168},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 40, "default": 10},
                  "provider": {"type": "string", "maxLength": 80, "description": "e.g. SpaceX, Rocket Lab, CASC"},
                  "country": {"type": "string", "maxLength": 3, "description": "ISO 3166-1 alpha-3, e.g. USA, CHN"}},
               "additionalProperties": False}, ai_launches)
    m.ai_tool("weather_at",
              "Current weather and the next 24 hours at a point (Open-Meteo): temperature, feels-like, wind and gusts, "
              "precipitation, cloud cover and a plain description.",
              {"type": "object", "properties": {
                  "lat": {"type": "number", "minimum": -90, "maximum": 90},
                  "lon": {"type": "number", "minimum": -180, "maximum": 180}},
               "required": ["lat", "lon"], "additionalProperties": False}, ai_weather)

    m.ai_tool("latest_scenes",
              "The latest Sentinel-2 (optical, 10 m), Sentinel-1 (radar, sees through cloud) and Landsat (optical, 30 m) "
              "scenes covering a point, from keyless STAC catalogues: satellite, time in UTC, cloud cover, tile or orbit, "
              "and a link to the official viewer. Searches on request and caches for an hour.",
              {"type": "object", "properties": {
                  "lat": {"type": "number", "minimum": -90, "maximum": 90},
                  "lon": {"type": "number", "minimum": -180, "maximum": 180},
                  "days": {"type": "integer", "minimum": 1, "maximum": SC.MAX_DAYS, "default": SC.DEFAULT_DAYS},
                  "max_cloud": {"type": "number", "minimum": 0, "maximum": 100, "default": SC.DEFAULT_CLOUD,
                                "description": "highest scene cloud cover in percent for optical scenes; 100 for any"},
                  "missions": {"type": "array", "items": {"type": "string", "enum": list(SC.ORDER)}}},
               "required": ["lat", "lon"], "additionalProperties": False}, ai_scenes)

    # -- layer table ------------------------------------------------------------------------------
    m.layer("space.satellites", "Satellites", "Space",
            description="Live positions of active satellites by CelesTrak group, propagated in your browser with SGP4.",
            source="CelesTrak GP elements, satellite.js", refresh="positions every 1 to 3 s, elements every 2 h",
            use="Which satellites are overhead, orbit types, ground tracks and footprints", count=cat.count)
    m.layer("space.iss", "International Space Station", "Space",
            description="The ISS with its ground track, footprint and visible passes.", source="CelesTrak, SGP4",
            refresh="every second", use="Spotting the ISS from where you are")
    m.layer("space.launches", "Launches", "Space", description="Upcoming and recent orbital launches at their pads.",
            source="The Space Devs Launch Library 2", refresh="hourly", use="What is launching, when and from where",
            count=lambda: len(launches.all()))
    m.layer("space.radar", "Rain radar", "Weather", description="Radar mosaic for the past two hours with animation.",
            source="RainViewer", refresh="every 10 min", use="Where it is raining now and which way it is moving")
    refresh_text = {"PT5M": "every 5 min", "PT10M": "every 10 min", "PT30M": "every 30 min"}
    uses = {"no2": "Industrial activity, shutdowns and pollution", "so2": "Volcanic eruptions and smelter plumes",
            "seaice": "Arctic and Antarctic ice for shipping routes", "lightning": "Where thunderstorms are active now",
            "nightdaily": "Blackouts after storms, strikes or grid failures", "flood": "Flood extent after heavy rain",
            "sar": "Floods, ships and changes seen through cloud",
            "meteosat": "Live clouds, storms and dust over Europe and Africa"}
    for pid in I.WEATHER + I.IMAGERY:
        p = I.PRODUCTS[pid]
        m.layer(f"space.{pid}", p["title"], p.get("group", "Weather"), description=p["about"], source=p["source"],
                refresh=refresh_text.get(p["period"], "daily"),
                use=uses.get(pid, "Context for weather, smoke and climate questions"))
    m.layer("space.daily", "Daily true colour", "Imagery",
            description="What the planet looked like on a chosen day (MODIS and VIIRS).", source="NASA GIBS",
            refresh="daily", use="Before and after views of floods, fires, smoke, snow and ice")
    m.layer("space.nightlights", "Night lights", "Imagery", description="VIIRS Black Marble night lights, 2016.",
            source="NASA GIBS", refresh="static", use="Where people and industry are")
    m.layer("space.scenes", "Satellite scenes", "Imagery",
            description="Finder for the latest Sentinel-2, Sentinel-1 and Landsat scenes at the map centre or a clicked "
                        "point, with cloud cover, quicklooks, footprints and links to the official viewers.",
            source="Element 84 Earth Search, Copernicus Data Space Ecosystem, Microsoft Planetary Computer",
            refresh="on request only, cached 1 h",
            use="Which satellite pictures exist of a place, how cloudy they are and where to open them")

    # -- background work -----------------------------------------------------------------------
    m.task("celestrak", lambda: cat.refresh_wanted(), every=900, initial_delay=90, when=lambda: m.active())
    m.task("launches", lambda: launches.maintain(True), every=600, initial_delay=30, when=lambda: m.active())

    # -- space weather and the aurora forecast (NOAA SWPC): routes, events, AI tool, layers, task ----
    SW.register(m, weather_at=weather_at, state=_STATE)
