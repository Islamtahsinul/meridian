"""Sanctions lists: vessels and aircraft on the OFAC SDN list, the UK Sanctions List, the UN Security
Council consolidated list and the EU consolidated financial sanctions list, merged into one index.
Shared by the flights and ships modules.

Python API for other modules (import lazily inside functions)::

    from meridian.modules import sanctions
    sanctions.lookup_vessel(imo="9187629")          # -> record dict with "match" key, or None
    sanctions.lookup_vessel(mmsi="572469210")
    sanctions.lookup_aircraft(hex="730f1c", reg="EP-GOL")
    sanctions.ready()                               # False until a list is loaded
    sanctions.version()                             # changes whenever the merged index changes
    sanctions.summary()                             # counts and refresh times per list

Records keep the OFAC record shape and add ``lists`` (e.g. ["OFAC", "UK"]), ``programmes`` per list
(``{"OFAC": ["RUSSIA-EO14024"], "UK": ["Russia"]}``), ``listings`` (one entry per list, with its own id
and listing date), ``listed_on`` and ``imos`` (vessels). ``program`` stays one OFAC-style string; other
lists' programmes follow with the list name in front ("RUSSIA-EO14024] [UK: Russia").

Name-only vessel matches are weak (many ships share names) and come back with ``match == "name"``.
Individuals on these lists are counted, never kept (CONTRACT 1.2).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter

from meridian.registry import HTTPError

from .merge import LIST_ORDER, merge_records
from .parsers import (clean, imo_digits, norm_name, norm_reg, parse_eu, parse_sdn, parse_uk, parse_un,
                      split_programs)

__all__ = ["lookup_vessel", "lookup_aircraft", "vessels", "aircraft", "ready", "version", "summary", "record",
           "parse_sdn", "parse_uk", "parse_un", "parse_eu", "norm_name", "norm_reg"]

SDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
UK_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml"
UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
# The token is the public one published with the dataset on data.europa.eu, not a secret.
EU_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"
SOURCE_ID = "sanctions.ofac_sdn"
# Where each build downloads from, read at build time (tests point these at a local server).
URLS = {"OFAC": SDN_URL, "UK": UK_URL, "UN": UN_URL, "EU": EU_URL}

LISTS: dict[str, dict] = {
    "OFAC": {"name": "OFAC Specially Designated Nationals list", "short": "OFAC SDN list",
             "publisher": "US Treasury, Office of Foreign Assets Control", "source_id": SOURCE_ID,
             "source_name": "OFAC Specially Designated Nationals list (vessels and aircraft)",
             "url": "https://ofac.treasury.gov/specially-designated-nationals-and-blocked-persons-list-sdn-human-readable-lists",
             "search": "https://sanctionssearch.ofac.treas.gov/",
             "licence": "US Government public domain", "refresh": "daily", "dataset": "sdn", "file": "sdn.json"},
    "UK": {"name": "UK Sanctions List", "short": "UK Sanctions List",
           "publisher": "Foreign, Commonwealth and Development Office", "source_id": "sanctions.uk",
           "source_name": "UK Sanctions List (FCDO): designated ships",
           "url": "https://www.gov.uk/government/publications/the-uk-sanctions-list",
           "search": "https://search-uk-sanctions-list.service.gov.uk/",
           "licence": "Open Government Licence v3.0",
           "refresh": "daily (downloaded again only when the list has changed)", "dataset": "uk", "file": "uk.json"},
    "UN": {"name": "UN Security Council consolidated list", "short": "UN Security Council list",
           "publisher": "United Nations Security Council", "source_id": "sanctions.un",
           "source_name": "UN Security Council consolidated sanctions list (vessels)",
           "url": "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list",
           "search": "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list",
           "licence": "UN terms of use: personal, non-commercial use, no resale or redistribution",
           "refresh": "daily", "dataset": "un", "file": "un.json"},
    "EU": {"name": "EU consolidated financial sanctions list", "short": "EU financial sanctions list",
           "publisher": "European Commission (FISMA)", "source_id": "sanctions.eu",
           "source_name": "EU consolidated financial sanctions list (vessels)",
           "url": "https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions",
           "search": "https://www.sanctionsmap.eu/",
           "licence": "European Commission reuse notice (Decision 2011/833/EU): reuse with acknowledgement",
           "refresh": "daily (about 3.5 MB compressed; the server ignores conditional requests)",
           "dataset": "eu", "file": "eu.json"},
}

log = logging.getLogger("meridian.sanctions")
_lock = threading.RLock()
_lists: dict[str, dict] = {}        # list id -> {"vessels", "aircraft", "built_at", "generated", "counts", ...}
_version = 0


def _empty_index() -> dict:
    return {"vessels": [], "aircraft": [], "by_imo": {}, "by_mmsi": {}, "by_vname": {}, "by_reg": {},
            "by_hex": {}, "by_msn": {}, "by_uid": {}, "vsearch": [], "asearch": [], "stats": {},
            "loaded": False, "built_at": None, "version": 0}


_index: dict = _empty_index()       # swapped whole (never mutated) so readers need no lock


# -- loading and merging ----------------------------------------------------------------------------
def _normalise(lid: str, rec: dict, kind: str) -> dict:
    """Give every record its list fields (OFAC build files predate them)."""
    rec = dict(rec)
    rec.setdefault("kind", "sanctioned_vessel" if kind == "vessel" else "sanctioned_aircraft")
    rec["list"] = lid
    rec.setdefault("list_id", rec.get("uid"))
    if not rec.get("programmes"):
        rec["programmes"] = split_programs(rec.get("program"))
    rec.setdefault("listed_on", None)
    return rec


def _set_list(lid: str, data: dict) -> None:
    """Install one list's parsed data (a build file's content) and rebuild the merged index."""
    if lid not in LISTS:
        raise ValueError(f"unknown list {lid!r}")
    entry = {"vessels": [_normalise(lid, v, "vessel") for v in data.get("vessels") or []],
             "aircraft": [_normalise(lid, a, "aircraft") for a in data.get("aircraft") or []],
             "built_at": data.get("built_at"), "generated": data.get("generated"),
             "source_modified": data.get("source_modified"), "counts": dict(data.get("counts") or {}),
             "loaded_at": time.time()}
    with _lock:
        _lists[lid] = entry
        _rebuild()


def _load_list(lid: str, path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("vessels"), list) \
            or not isinstance(data.get("aircraft", []), list):
        raise ValueError(f"{path.name} is not a sanctions build file")
    _set_list(lid, data)


def _load(path) -> None:
    """Load an OFAC build file (``sdn.json``); the other lists stay as they are."""
    _load_list("OFAC", path)


def _drop_list(lid: str) -> None:
    with _lock:
        _lists.pop(lid, None)
        _rebuild()


def _reset() -> None:
    """Forget every list (tests)."""
    global _index
    with _lock:
        _lists.clear()
        _index = _empty_index()


def _rebuild() -> None:
    global _index, _version
    with _lock:
        vessels_ = merge_records({lid: d["vessels"] for lid, d in _lists.items()}, "vessel")
        aircraft_ = merge_records({lid: d["aircraft"] for lid, d in _lists.items()}, "aircraft")
        _version += 1
        idx = _build_index(vessels_, aircraft_)
        idx.update({"loaded": bool(_lists), "version": _version,
                    "built_at": max((d.get("built_at") or 0 for d in _lists.values()), default=None) or None})
        _index = idx


def _build_index(vessels_: list[dict], aircraft_: list[dict]) -> dict:
    idx = _empty_index()
    idx["vessels"], idx["aircraft"] = vessels_, aircraft_
    by_imo, by_mmsi, by_vname, by_reg, by_hex, by_msn, by_uid = (idx[k] for k in (
        "by_imo", "by_mmsi", "by_vname", "by_reg", "by_hex", "by_msn", "by_uid"))

    def put(index, key, rec):
        # OFAC records keep the original "last one wins" behaviour; other lists never displace them.
        if rec["lists"][0] == "OFAC":
            index[key] = rec
        else:
            index.setdefault(key, rec)

    stats = {lid: Counter() for lid in LIST_ORDER}
    for v in vessels_:
        for imo in v.get("imos") or ([v["imo"]] if v.get("imo") else []):
            put(by_imo, imo, v)
        if v.get("mmsi"):
            put(by_mmsi, str(v["mmsi"]), v)
        names = []
        for name in [v.get("name")] + list(v.get("aliases") or []):
            key = norm_name(name)
            if key:
                by_vname.setdefault(key, []).append(v)
                if key not in names:
                    names.append(key)
        idx["vsearch"].append((tuple(names), v))
        for entry in v["listings"]:
            by_uid.setdefault(str(entry.get("uid")), v)
            s = stats.setdefault(entry["list"], Counter())
            s["vessels"] += 1
            s["vessels_with_imo"] += 1 if entry.get("imo") else 0
            s["vessels_not_on_ofac"] += 0 if "OFAC" in v["lists"] else 1
            s["vessels_only_here"] += 1 if len(v["lists"]) == 1 else 0
        by_uid[str(v["uid"])] = v
    for a in aircraft_:
        for reg in {a.get("registration"), a.get("name")}:
            if reg and norm_reg(reg):
                put(by_reg, norm_reg(reg), a)
        if a.get("mode_s"):
            put(by_hex, str(a["mode_s"]).lower(), a)
        if a.get("msn"):
            put(by_msn, str(a["msn"]).upper(), a)
        idx["asearch"].append((norm_reg(a.get("registration")), norm_name(a.get("operator")), a))
        for entry in a["listings"]:
            by_uid.setdefault(str(entry.get("uid")), a)
            s = stats.setdefault(entry["list"], Counter())
            s["aircraft"] += 1
            s["aircraft_not_on_ofac"] += 0 if "OFAC" in a["lists"] else 1
        by_uid[str(a["uid"])] = a
    idx["stats"] = {lid: dict(c) for lid, c in stats.items()}
    idx["stats"]["merged"] = {"vessels": len(vessels_), "aircraft": len(aircraft_),
                              "vessels_on_several_lists": sum(1 for v in vessels_ if len(v["lists"]) > 1),
                              "aircraft_on_several_lists": sum(1 for a in aircraft_ if len(a["lists"]) > 1)}
    return idx


# -- public Python API --------------------------------------------------------------------------
def ready() -> bool:
    return bool(_index.get("loaded"))


def version() -> int:
    """A number that changes whenever any list (re)loads: re-check matches when it changes."""
    return int(_index.get("version") or 0)


def vessels() -> list[dict]:
    return list(_index["vessels"])


def aircraft() -> list[dict]:
    return list(_index["aircraft"])


def record(uid) -> dict | None:
    """A merged record by its uid or by any list's own uid (e.g. "uk-RUS2176")."""
    hit = _index["by_uid"].get(str(uid or "").strip())
    return dict(hit) if hit else None


def _ident(value, digits: int) -> str:
    raw = str(value).strip()[:100]
    only = "".join(ch for ch in raw if ch.isdigit())
    return only if len(only) == digits and len(raw) <= digits + 6 else raw


def lookup_vessel(imo: str | int | None = None, mmsi: str | int | None = None,
                  name: str | None = None) -> dict | None:
    idx = _index
    if imo:
        hit = idx["by_imo"].get(_ident(imo, 7))
        if hit:
            return dict(hit, match="imo")
    if mmsi:
        hit = idx["by_mmsi"].get(_ident(mmsi, 9))
        if hit:
            return dict(hit, match="mmsi")
    if name:
        hits = idx["by_vname"].get(norm_name(str(name)[:200]))
        if hits:
            return dict(hits[0], match="name")
    return None


def lookup_aircraft(hex: str | None = None, reg: str | None = None, msn: str | None = None) -> dict | None:
    idx = _index
    if hex:
        hit = idx["by_hex"].get(str(hex).strip().lower()[:20])
        if hit:
            return dict(hit, match="mode_s")
    if reg:
        hit = idx["by_reg"].get(norm_reg(str(reg)[:40]))
        if hit:
            return dict(hit, match="registration")
    if msn:
        hit = idx["by_msn"].get(str(msn).strip().upper()[:40])
        if hit:
            return dict(hit, match="msn")
    return None


def _date_text(ts: float | None) -> str | None:
    return time.strftime("%d %b %Y", time.gmtime(ts)).lstrip("0") if ts else None


def summary(datasets=None, module: str = "sanctions") -> dict:
    """Counts per list, the merged totals and when each list was last refreshed."""
    idx = _index
    with _lock:
        loaded = dict(_lists)
    now = time.time()
    rows = []
    for lid, meta in LISTS.items():
        info = loaded.get(lid)
        st = idx["stats"].get(lid) or {}
        item = datasets.items.get(f"{module}.{meta['dataset']}") if datasets is not None else None
        counts = (info or {}).get("counts") or {}
        built_at = (info or {}).get("built_at")
        row = {"id": lid, "name": meta["name"], "publisher": meta["publisher"], "url": meta["url"],
               "search": meta["search"], "licence": meta["licence"], "refresh": meta["refresh"],
               "source_id": meta["source_id"], "loaded": info is not None,
               "vessels": st.get("vessels", 0), "aircraft": st.get("aircraft", 0),
               "vessels_with_imo": st.get("vessels_with_imo", 0),
               "vessels_not_on_ofac": st.get("vessels_not_on_ofac", 0) if lid != "OFAC" else None,
               "aircraft_not_on_ofac": st.get("aircraft_not_on_ofac", 0) if lid != "OFAC" else None,
               "vessels_only_here": st.get("vessels_only_here", 0),
               "designations": counts.get("designations"),
               "generated": (info or {}).get("generated"), "source_modified": (info or {}).get("source_modified"),
               "built_at": built_at, "loaded_at": (info or {}).get("loaded_at"),
               "stale": bool(built_at and now - built_at > 2 * 86400),
               "last_attempt": getattr(item, "last_attempt", None), "last_error": getattr(item, "last_error", None),
               "building": bool(getattr(item, "building", False))}
        row["state"] = ("error" if row["last_error"] else "ok" if row["loaded"]
                        else "building" if row["building"] else "waiting")
        rows.append(row)
    return {"ready": ready(), "version": version(), "lists": rows, "merged": idx["stats"].get("merged") or
            {"vessels": 0, "aircraft": 0, "vessels_on_several_lists": 0, "aircraft_on_several_lists": 0},
            "note": "One record per vessel or aircraft across the lists, merged on IMO and MMSI (vessels) or Mode S "
                    "and registration (aircraft). Individuals on these lists are never used."}


def matched_lists(*records) -> list[str]:
    out = []
    for r in records:
        for lid in (r or {}).get("lists") or []:
            if lid not in out:
                out.append(lid)
    return sorted(out, key=lambda x: LIST_ORDER.index(x) if x in LIST_ORDER else 99)


def _search(q: str, limit: int) -> list[dict]:
    idx = _index
    if not idx["loaded"]:
        return []
    nq = norm_name(q)
    if len(nq) < 3:
        return []
    hits = []
    for names, v in idx["vsearch"]:
        if nq in (v.get("imos") or ()) or nq == (v.get("imo") or "") or nq == str(v.get("mmsi") or ""):
            score = 0.5
        elif names and nq == names[0]:
            score = 0.5
        elif nq in names:
            score = 0.45
        elif any(nq in n for n in names):
            score = 0.35
        else:
            continue
        hits.append((score, 0, v))
    for reg, operator, a in idx["asearch"]:
        if nq in reg or (operator and nq in operator):
            hits.append((0.5 if nq == reg else 0.35, 1, a))
    hits.sort(key=lambda h: -h[0])
    out = []
    for score, is_aircraft, r in hits[:max(1, limit)]:
        lists_ = ", ".join(r.get("lists") or ["OFAC"])
        if is_aircraft:
            out.append({"kind": "sanctioned_aircraft", "id": r["uid"], "title": r.get("registration") or r["name"],
                        "subtitle": f"Sanctioned aircraft ({lists_}), {r.get('model') or 'model unknown'}"
                                    + (f", {r['operator']}" if r.get("operator") else ""),
                        "score": score, "layer": "flights"})
        else:
            out.append({"kind": "sanctioned_vessel", "id": r["uid"],
                        "title": r.get("name") or (f"IMO {r['imo']}" if r.get("imo") else "Sanctioned vessel"),
                        "subtitle": f"Sanctioned vessel ({lists_}), {r.get('vessel_type') or 'type unknown'}"
                                    + (f", IMO {r['imo']}" if r.get("imo") else ""),
                        "score": score, "layer": "ships"})
    return out


# -- dataset builds -----------------------------------------------------------------------------------
class _DownloadWarnings(logging.Handler):
    """Catches the core's "download failed, keeping the older copy" warning for one file."""

    def __init__(self, filename: str, logger):
        super().__init__(logging.WARNING)
        self.filename = filename
        self.logger = logger if isinstance(logger, logging.Logger) else None
        self.message: str | None = None

    def emit(self, record):
        try:
            args = record.args if isinstance(record.args, tuple) else ()
            if len(args) >= 2 and args[0] == self.filename:
                self.message = str(args[1])
        except Exception:
            pass

    def __enter__(self):
        if self.logger is not None:
            self.logger.addHandler(self)
        return self

    def __exit__(self, *exc):
        if self.logger is not None:
            self.logger.removeHandler(self)
        return False


def _fetch(ds, url: str, filename: str, *, min_bytes: int, timeout: float, headers: dict | None = None):
    """Download through the dataset (ETag / If-Modified-Since when the copy exists). Returns
    ``(path, failure)``: ``failure`` is the reason when the download failed and the older raw copy came back."""
    dest = ds.raw(filename)
    before = dest.stat().st_mtime if dest.exists() else None
    expect = bool(ds.force or before is None or (time.time() - before) / 3600.0 >= ds.max_age_hours * 0.9)
    kw = {"min_bytes": min_bytes, "timeout": timeout}
    if headers:
        kw["headers"] = headers
    with _DownloadWarnings(dest.name, getattr(ds.net, "log", None)) as caught:
        path = ds.download(url, filename, **kw)
    failed = expect and before is not None and path.stat().st_mtime == before
    return path, ((caught.message or "the download failed") if failed else None)


def _raw_meta(path) -> dict:
    try:
        return json.loads(path.with_name(path.name + ".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _check_fetch(ds, lid: str, failure: str | None) -> None:
    """After a failed download: keep the list's last good build in service (by failing this build), or,
    when there is none yet, carry on with the older raw copy."""
    if not failure:
        return
    out = ds.out_dir / LISTS[lid]["file"]
    if out.exists():
        built = _date_text(out.stat().st_mtime)
        raise RuntimeError(f"{failure}; keeping the {LISTS[lid]['short']} built on {built}")
    ds.note(f"Download failed ({failure}); built from the older downloaded copy.")


def _build_ofac(ds) -> None:
    path, failure = _fetch(ds, URLS["OFAC"], "SDN.CSV", min_bytes=500_000, timeout=180)
    _check_fetch(ds, "OFAC", failure)
    text = path.read_bytes().decode("cp1252", errors="replace")
    vessels_, aircraft_ = parse_sdn(text)
    if len(vessels_) < 100 or len(aircraft_) < 20:
        raise RuntimeError(f"SDN list looks incomplete ({len(vessels_)} vessels, {len(aircraft_)} aircraft)")
    ds.write_json("sdn.json", {"built_at": time.time(), "vessels": vessels_, "aircraft": aircraft_,
                               "source_modified": _raw_meta(path).get("last_modified")})
    ds.set_rows(len(vessels_) + len(aircraft_))
    ds.note(f"{len(vessels_)} vessels, {len(aircraft_)} aircraft")


def _xml_build(lid: str, raw_name: str, parse, *, min_bytes: int, timeout: float,
               min_designations: int, min_vessels: int, headers: dict | None = None):
    def build(ds) -> None:
        path, failure = _fetch(ds, URLS[lid], raw_name, min_bytes=min_bytes, timeout=timeout, headers=headers)
        _check_fetch(ds, lid, failure)
        parsed = parse(path)
        counts = parsed["counts"]
        if counts.get("designations", 0) < min_designations or len(parsed["vessels"]) < min_vessels:
            raise RuntimeError(f"the {LISTS[lid]['short']} looks incomplete ({counts.get('designations', 0)} "
                               f"designations, {len(parsed['vessels'])} vessels)")
        ds.write_json(LISTS[lid]["file"], {
            "built_at": time.time(), "list": lid, "generated": parsed.get("generated"),
            "source_modified": _raw_meta(path).get("last_modified"), "counts": counts,
            "vessels": parsed["vessels"], "aircraft": parsed["aircraft"]})
        ds.set_rows(len(parsed["vessels"]) + len(parsed["aircraft"]))
        nv, na = len(parsed["vessels"]), len(parsed["aircraft"])
        ds.note(f"{nv} {'vessel' if nv == 1 else 'vessels'} and {na} aircraft among "
                f"{counts.get('designations', 0)} designations (list of {parsed.get('generated') or 'unknown date'})")
    return build


_build_uk = _xml_build("UK", "UK-Sanctions-List.xml", parse_uk, min_bytes=2_000_000, timeout=300,
                       min_designations=1000, min_vessels=100)
_build_un = _xml_build("UN", "un-consolidated.xml", parse_un, min_bytes=300_000, timeout=180,
                       min_designations=300, min_vessels=0)
# Asking for gzip cuts the EU download from about 26 MB to 3.5 MB; the parser reads either form.
_build_eu = _xml_build("EU", "eu-fsf-1.1.xml", parse_eu, min_bytes=300_000, timeout=300,
                       min_designations=1000, min_vessels=0, headers={"Accept-Encoding": "gzip"})


# -- registration -------------------------------------------------------------------------------------
def _keep_note(info: dict | None) -> str:
    if not info:
        return "Not loaded yet."
    when = _date_text(info.get("built_at"))
    return f"Keeping the copy from {when}." if when else "Keeping the last good copy."


def _list_note(lid: str, idx: dict) -> str:
    st = idx["stats"].get(lid) or {}
    info = _lists.get(lid) or {}
    nv, na = st.get("vessels", 0), st.get("aircraft", 0)
    if lid == "OFAC":
        return f"{nv} vessels, {na} aircraft"
    when = info.get("generated")
    note = f"{nv} {'vessel' if nv == 1 else 'vessels'}, {st.get('vessels_not_on_ofac', 0)} not on the OFAC list"
    if na:
        note += f", {na} aircraft"
    return note + (f"; list of {when}" if when else "")


def _arg(req, name: str):
    value = req.arg(name, None)
    return str(value)[:100] if value is not None else None


def register(m) -> None:
    for meta in LISTS.values():
        m.source(meta["source_id"], meta["source_name"], meta["url"], meta["licence"], refresh=meta["refresh"])

    def refresh_status() -> None:
        idx = _index
        for lid, meta in LISTS.items():
            item = m.app.datasets.items.get(f"{m.name}.{meta['dataset']}")
            err = getattr(item, "last_error", None)
            info = _lists.get(lid)
            st = idx["stats"].get(lid) or {}
            if err:
                m.status(meta["source_id"], ok=False, error=err, note=_keep_note(info))
            elif info is not None:
                m.status(meta["source_id"], ok=True, count=st.get("vessels", 0) + st.get("aircraft", 0),
                         note=_list_note(lid, idx))

    def guarded(lid: str, fn):
        def build(ds) -> None:
            try:
                fn(ds)
            except Exception as err:
                m.status(LISTS[lid]["source_id"], ok=False, error=f"{type(err).__name__}: {err}"[:400],
                         note=_keep_note(_lists.get(lid)))
                raise
        return build

    def loader(lid: str):
        def load() -> None:
            path = m.data_dir / LISTS[lid]["file"]
            try:
                _load_list(lid, path)
            except Exception as err:
                m.status(LISTS[lid]["source_id"], ok=False, error=f"Could not load {path.name}: {err}"[:400],
                         note=_keep_note(_lists.get(lid)))
                raise
            refresh_status()
        return load

    builds = {"OFAC": _build_ofac, "UK": _build_uk, "UN": _build_un, "EU": _build_eu}
    descriptions = {"OFAC": "OFAC SDN vessels and aircraft", "UK": "UK Sanctions List ships (FCDO)",
                    "UN": "UN Security Council consolidated list: vessels",
                    "EU": "EU consolidated financial sanctions list: vessels"}
    for lid, meta in LISTS.items():
        m.dataset(meta["dataset"], guarded(lid, builds[lid]), max_age_hours=24, description=descriptions[lid])
        m.on_dataset(meta["dataset"], loader(lid))

    def list_filter(req):
        value = req.arg("list", None)
        if value is None:
            return None
        lid = str(value).strip().upper()
        if lid not in LISTS:
            raise HTTPError(400, "BAD_ARG", f"list must be one of {', '.join(LISTS)}.")
        return lid

    def by_list(records: list[dict]) -> dict:
        c = Counter(lid for r in records for lid in r.get("lists") or [])
        return {lid: c.get(lid, 0) for lid in LISTS}

    @m.route("GET", "/api/sanctions/vessels")
    def r_vessels(req):
        lid = list_filter(req)
        items = [v for v in vessels() if lid is None or lid in v["lists"]]
        return {"ready": ready(), "vessels": items, "lists": [x for x in LISTS if x in _lists],
                "counts": by_list(items)}

    @m.route("GET", "/api/sanctions/aircraft")
    def r_aircraft(req):
        lid = list_filter(req)
        items = [a for a in aircraft() if lid is None or lid in a["lists"]]
        return {"ready": ready(), "aircraft": items, "lists": [x for x in LISTS if x in _lists],
                "counts": by_list(items)}

    @m.route("GET", "/api/sanctions/check")
    def r_check(req):
        vessel = lookup_vessel(_arg(req, "imo"), _arg(req, "mmsi"), _arg(req, "name"))
        plane = lookup_aircraft(_arg(req, "hex"), _arg(req, "reg"), _arg(req, "msn"))
        return {"ready": ready(), "vessel": vessel, "aircraft": plane, "lists": matched_lists(vessel, plane)}

    @m.route("GET", "/api/sanctions/lists")
    def r_lists(req):
        return summary(m.app.datasets, m.name)

    m.search(_search)

    def ai_lookup(a: dict) -> dict:
        def val(key):
            v = a.get(key)
            return str(v)[:100] if v not in (None, "") else None

        vessel = lookup_vessel(val("imo"), val("mmsi"), val("vessel_name"))
        plane = lookup_aircraft(val("mode_s_hex"), val("registration"), val("msn"))
        out = {"ready": ready(), "vessel": vessel, "aircraft": plane, "lists": matched_lists(vessel, plane),
               "checked": [{"list": lid, "name": LISTS[lid]["name"], "published": info.get("generated"),
                            "loaded": _date_text(info.get("built_at"))}
                           for lid, info in sorted(dict(_lists).items(), key=lambda kv: LIST_ORDER.index(kv[0]))]}
        if vessel and vessel.get("match") == "name":
            out["note"] = "The vessel matched by name only: a weak match, not a confirmation. Check its IMO number."
        return out

    m.ai_tool("sanctions_lookup",
              "Check the OFAC SDN list, the UK Sanctions List, the UN Security Council consolidated list and the EU "
              "consolidated financial sanctions list for a vessel (IMO, MMSI or name) or aircraft (registration, "
              "Mode S hex or MSN). Returns the matching record or null; 'lists' says which lists name it and "
              "'programmes' gives each list's programmes or regimes. A vessel matched by name only is weak.",
              {"type": "object", "properties": {
                  "imo": {"type": "string"}, "mmsi": {"type": "string"}, "vessel_name": {"type": "string"},
                  "registration": {"type": "string"}, "mode_s_hex": {"type": "string"}, "msn": {"type": "string"}},
               "additionalProperties": False},
              ai_lookup)
