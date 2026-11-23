"""Dataset builds of the intel module (CONTRACT section 5.4). Raw downloads stay in data/raw/intel/."""

from __future__ import annotations

import json
import time

from . import countries, cyber, frontlines, ucdp

UCDP_API = "https://ucdpapi.pcr.uu.se/api/gedevents/{version}"


def _ukraine_geometry(raw_dir):
    path = raw_dir / countries.NE_RAW
    if not path.exists():
        return None
    try:
        ne = json.loads(path.read_bytes().decode("utf-8"))
    except ValueError:
        return None
    for f in ne.get("features") or []:
        if countries.iso2_of(f.get("properties") or {}) == "UA":
            return f.get("geometry")
    return None


def register_datasets(m, svc) -> None:
    # -- Natural Earth admin-0 ---------------------------------------------------------------------
    def build_countries(ds):
        path = ds.download(countries.NE_URL, countries.NE_RAW, min_bytes=1_000_000, timeout=180)
        ne = json.loads(path.read_bytes().decode("utf-8"))
        fc, n = countries.build_countries_collection(ne)
        if n < 200:
            raise RuntimeError(f"Natural Earth file looks incomplete ({n} shapes)")
        ds.write_json("countries.geojson", fc)
        ds.set_rows(n)
        ds.note(f"{n} country shapes, simplified for display")

    m.dataset("countries", build_countries, max_age_hours=24 * 30,
              description="Natural Earth admin-0 countries (1:50m), public domain")
    m.on_dataset("countries", svc.load_countries)

    # -- World Bank indicators -----------------------------------------------------------------------
    def build_worldbank(ds):
        ds.source(countries.WB_API)
        data = countries.build_worldbank(ds.net, ds.log)
        ds.write_json("worldbank.json", data)
        ds.set_rows(len(data["economies"]))
        ds.note(f"{len(data['economies'])} economies, {len(countries.INDICATORS) - len(data['failures'])} indicators")
        for f in data["failures"]:
            ds.note(f"missing: {f}")

    m.dataset("worldbank", build_worldbank, max_age_hours=168,
              description="World Bank WDI and WGI indicators (CC BY 4.0)")
    m.on_dataset("worldbank", svc.load_worldbank)

    # -- DeepStateMap front line -----------------------------------------------------------------------
    def build_frontlines(ds):
        path = ds.download(frontlines.DEEPSTATE_URL, "deepstate-last.json", min_bytes=5000, timeout=90)
        data = json.loads(path.read_bytes().decode("utf-8"))
        parsed = frontlines.parse_deepstate(data)
        if not parsed["polygons"]:
            raise RuntimeError("DeepState returned no area polygons")
        ua = _ukraine_geometry(ds.raw_dir)
        waited = 0
        while ua is None and waited < 180:   # "after" orders build starts only: wait for the countries download
            time.sleep(3)
            waited += 3
            ua = _ukraine_geometry(ds.raw_dir)
        fc, meta = frontlines.build_collection(parsed, ua)
        ds.write_json("frontlines.geojson", fc)
        ds.write_json("frontlines_meta.json", meta)
        ds.set_rows(len(fc["features"]))
        ds.note(f"{meta['occupied_km2']:,} km² occupied, front {meta['front_km']:,} km in {meta['front_pieces']} pieces")
        if not meta["masked"]:
            ds.note("Natural Earth Ukraine outline missing: every occupied-area edge is drawn as front")
        if parsed["skipped"]:
            ds.note(f"{sum(parsed['skipped'].values())} other DeepState polygons not shown")

    m.dataset("frontlines", build_frontlines, max_age_hours=3,
              description="Ukraine front line and occupied areas from DeepStateMap", after=("countries",))
    m.on_dataset("frontlines", svc.load_frontline_meta)

    # -- abuse.ch Feodo Tracker ------------------------------------------------------------------------
    def build_feodo(ds):
        path = ds.download(cyber.FEODO_URL, "feodo-ipblocklist.json", min_bytes=2, timeout=60)
        entries = cyber.parse_feodo(json.loads(path.read_bytes().decode("utf-8")))
        ds.write_json("feodo.json", cyber.feodo_snapshot(entries))
        ds.set_rows(len(entries))
        ds.note(f"{len(entries)} botnet command servers listed")

    m.dataset("feodo", build_feodo, max_age_hours=6, description="abuse.ch Feodo Tracker botnet C2 list")
    m.on_dataset("feodo", svc.load_feodo)

    # -- UCDP GED ----------------------------------------------------------------------------------------
    def build_ucdp(ds):
        html = ds.net.get_text(ucdp.DOWNLOADS, timeout=60, retries=2)
        ds.source(ucdp.DOWNLOADS)
        links = ucdp.find_links(html)
        agg = ucdp.Aggregator()
        version = None
        if links["ged"]:
            ver, url = links["ged"]
            version = f"{ver // 10}.{ver % 10}"
            name = f"ged{ver}-csv.zip"
            path = ds.download(url, name, min_bytes=1_000_000, timeout=900, max_age_hours=24 * 120)
            with ucdp.open_zip_csv(path) as stream:
                agg.add_csv(stream)
            for old in ds.raw_dir.glob("ged*-csv.zip"):
                if old.name != name:
                    try:
                        old.unlink()
                        (old.with_name(old.name + ".meta.json")).unlink(missing_ok=True)
                    except OSError:
                        pass
        elif m.config.get("UCDP_ACCESS_TOKEN"):
            version = "API"
            _ucdp_api(ds, agg, m.config.get("UCDP_ACCESS_TOKEN"))
        else:
            raise RuntimeError("UCDP downloads page lists no GED CSV and no UCDP_ACCESS_TOKEN is set")
        cands = []
        keep = set()
        for cu in links["candidates"]:
            name = cu.rsplit("/", 1)[-1]
            keep.add(name)
            try:
                cp = ds.download(cu, name, min_bytes=1000, timeout=300)
                with open(cp, encoding="utf-8", errors="replace", newline="") as fh:
                    agg.add_csv(fh)
                cands.append(name)
            except Exception as err:
                ds.note(f"candidate file {name} skipped: {err}")
        for old in ds.raw_dir.glob("GEDEvent_v*.csv"):
            if old.name not in keep:
                try:
                    old.unlink()
                    (old.with_name(old.name + ".meta.json")).unlink(missing_ok=True)
                except OSError:
                    pass
        if agg.rows < 1000:
            raise RuntimeError(f"UCDP data looks incomplete ({agg.rows} events)")
        data = agg.result(version or "?", cands)
        ds.write_json("ucdp_cells.json", data)
        ds.set_rows(agg.rows)
        ds.note(f"GED {version}: {agg.rows:,} events, {len(data['cells']):,} cells, {agg.unmapped:,} country-level events not mapped")
        if cands:
            ds.note("candidate events: " + ", ".join(cands))

    m.dataset("ucdp", build_ucdp, max_age_hours=24 * 30,
              description="UCDP Georeferenced Event Dataset, yearly 0.5° cells (CC BY 4.0)")
    m.on_dataset("ucdp", svc.load_ucdp)


def _ucdp_api(ds, agg, token: str, version: str = "26.1", years: int = 5, max_pages: int = 80) -> None:
    """Fallback when the public CSV is unavailable: page through the token API for recent years."""
    start = f"{time.gmtime().tm_year - years}-01-01"
    url = UCDP_API.format(version=version)
    page = 0
    while page < max_pages:
        payload = ds.net.get_json(url, params={"pagesize": 1000, "page": page, "StartDate": start},
                                  headers={"x-ucdp-access-token": token}, timeout=60, retries=1, min_interval=1.0)
        rows = payload.get("Result") if isinstance(payload, dict) else None
        if not rows:
            break
        for r in rows:
            agg.add({k: (str(v) if v is not None else "") for k, v in r.items()})
        page += 1
        if page >= int(payload.get("TotalPages") or 0):
            break
