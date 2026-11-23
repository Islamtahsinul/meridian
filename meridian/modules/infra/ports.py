"""Ports: NGA World Port Index (public domain) when msi.nga.mil answers, otherwise UN/LOCODE port
locations (function 1) with sizes and precise positions from Natural Earth ports (public domain).

Also writes ``ports.json`` (CONTRACT 9.1) for the ships module: ``[{id, name, country, lat, lon, size}]``.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time

from meridian import geo

from . import countries
from .common import Collection, ascii_json, compact_num, decode_text, fc, fnum, norm, point_feature, text, valid_point

WPI_CSV = "https://msi.nga.mil/api/publications/download?key=16694622/SFH00000/UpdatedPub150.csv"
WPI_PAGE = "https://msi.nga.mil/Publications/WPI"
LOCODE_CSV = "https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv"
LOCODE_PAGE = "https://unece.org/trade/cefact/unlocode-code-list-country-and-territory"
NE_PORTS = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_ports.geojson"
NE_PAGE = "https://www.naturalearthdata.com/downloads/10m-cultural-vectors/ports/"
SOURCE_WPI = "infra.wpi"
SOURCE_LOCODE = "infra.unlocode"
SOURCE_NE = "infra.ne_ports"

# The world's largest cargo ports by UN/LOCODE. Natural Earth's display rank is only a rough size proxy
# (it rates Rotterdam "medium"), so these are always "large" when the World Port Index is unavailable.
MAJOR_PORTS = frozenset("""
CNSHA CNSGH SGSIN CNNGB CNSZX CNYTN CNTAO CNCAN KRPUS CNTXG CNTSN HKHKG NLRTM AEJEA MYPKG BEANR CNXMN MYTPP TWKHH
USLAX USLGB DEHAM THLCH USNYC IDTPP LKCMB VNSGN DEBRV ESVLC GRPIR ESALG SAJED USSAV INMUN INNSA GBFXT MAPTM BRSSZ
MXZLO JPTYO JPYOK JPUKB JPNGO USHOU USSEA CAVAN FRLEH FRMRS ITGOA EGPSD OMSLL PKKHI AUMEL AUSYD ZADUR NGAPP TRMER
GBSOU GBLGP PLGDN RUNVS RULED USORF USOAK PAONX PABLB QAHMD
""".split())
MIN_LOCODE = 3000
MIN_WPI = 1000
SIZES = ("large", "medium", "small", "very small")
SIZE_WEIGHT = {"large": 1.0, "medium": 0.65, "small": 0.4, "very small": 0.25, None: 0.2}
_WPI_SIZE = {"L": "large", "LARGE": "large", "M": "medium", "MEDIUM": "medium", "S": "small", "SMALL": "small",
             "V": "very small", "VERY SMALL": "very small"}
_HARBOUR_TYPES = {"CN": "Coastal natural", "CB": "Coastal breakwater", "CT": "Coastal tide gate",
                  "RN": "River natural", "RB": "River basin", "RT": "River tide gate", "LC": "Lake or canal",
                  "OR": "Open roadstead", "TH": "Typhoon harbour"}
_SHELTER = {"E": "Excellent", "G": "Good", "F": "Fair", "P": "Poor", "N": "None"}
_FUNCTIONS = {"1": "Port", "2": "Rail terminal", "3": "Road terminal", "4": "Airport", "5": "Postal exchange",
              "6": "Multimodal", "7": "Fixed transport (pipeline)", "B": "Border crossing"}
_STATUS = {"AA": "Approved by a government agency", "AC": "Approved by customs", "AF": "Approved by a national "
           "facilitation body", "AI": "Adopted by an international organisation", "AM": "Approved by the UN/LOCODE "
           "maintenance agency", "AQ": "Entry approved, functions not verified", "AS": "Approved by a national "
           "standardisation body", "RL": "Recognised location", "RN": "Requested by a national authority",
           "RQ": "Request under consideration", "RR": "Request rejected", "QQ": "Original entry not verified",
           "UR": "Entry under review", "XX": "Entry to be removed"}


# ---------------------------------------------------------------------------------------------
# UN/LOCODE
# ---------------------------------------------------------------------------------------------
def parse_locode_coords(value: str | None) -> tuple[float, float] | None:
    """"5054N 00124W" -> (50.9, -1.4)."""
    m = re.match(r"^\s*(\d{2})(\d{2})([NS])\s+(\d{3})(\d{2})([EW])\s*$", value or "")
    if not m:
        return None
    lat = int(m.group(1)) + int(m.group(2)) / 60.0
    lon = int(m.group(4)) + int(m.group(5)) / 60.0
    if m.group(3) == "S":
        lat = -lat
    if m.group(6) == "W":
        lon = -lon
    return (lat, lon) if valid_point(lat, lon) else None


def parse_locode(csv_text: str, with_missing: bool = False):
    """UN/LOCODE entries with function 1 (port). Codes listed twice keep their current entry.
    With ``with_missing`` also returns the port entries that have no coordinates (about a third)."""
    out: dict[str, dict] = {}
    missing: dict[str, dict] = {}
    for r in csv.DictReader(io.StringIO(csv_text)):
        func = (r.get("Function") or "").strip()
        change = (r.get("Change") or "").strip()
        if not func.startswith("1") or change == "X" or (r.get("Status") or "") == "XX":
            continue
        cc = (r.get("Country") or "").strip().upper()
        loc = (r.get("Location") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", cc) or not re.fullmatch(r"[A-Z0-9]{3}", loc):
            continue
        pos = parse_locode_coords(r.get("Coordinates"))
        pid = f"locode:{cc}{loc}"
        target = out if pos else missing
        if pid in target and not (target[pid].get("_change") and not change):
            continue
        funcs = [_FUNCTIONS[ch] for ch in func if ch in _FUNCTIONS]
        target[pid] = {"id": pid, "locode": f"{cc} {loc}", "name": text(r.get("Name"), 100) or loc,
                       "name_ascii": text(r.get("NameWoDiacritics"), 100), "lat": pos[0] if pos else None,
                       "lon": pos[1] if pos else None, "country": cc, "country_name": countries.name(cc),
                       "subdivision": text(r.get("Subdivision"), 8), "functions": funcs,
                       "status": _STATUS.get((r.get("Status") or "").strip()), "iata": text(r.get("IATA"), 4),
                       "remarks": text(r.get("Remarks"), 200), "size": None, "source": "UN/LOCODE",
                       "position_note": "UN/LOCODE position (to the nearest minute)", "_change": change}
    for d in (out, missing):
        for v in d.values():
            v.pop("_change", None)
    ports = list(out.values())
    if with_missing:
        return ports, [v for k, v in missing.items() if k not in out]
    return ports


# ---------------------------------------------------------------------------------------------
# Natural Earth ports (sizes and precise positions for about 1,000 major ports)
# ---------------------------------------------------------------------------------------------
def parse_ne_ports(geojson_text: str) -> list[dict]:
    data = json.loads(geojson_text)
    out = []
    for f in data.get("features") or []:
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        lon, lat = (g.get("coordinates") or [None, None])[:2]
        if not valid_point(lat, lon):
            continue
        rank = p.get("scalerank")
        size = "large" if isinstance(rank, (int, float)) and rank <= 4 else (
            "medium" if isinstance(rank, (int, float)) and rank <= 6 else "small")
        out.append({"ne_id": p.get("ne_id"), "name": text(p.get("name"), 100), "lat": float(lat), "lon": float(lon),
                    "size": size, "website": p.get("website")})
    return out


def _name_key(s: str | None) -> str:
    n = norm(s)
    n = re.sub(r"\b(port|of|the|harbou?r|puerto|porto|port of)\b", " ", n)
    return re.sub(r"\s+", "", n)


def merge_ne(ports: list[dict], ne: list[dict], missing: list[dict] | None = None, max_km: float = 30.0,
             stats: dict | None = None) -> int:
    """Natural Earth ports (precise positions, a size) folded into the UN/LOCODE list:

    1. a LOCODE port nearby with the same name gets the size and the precise position;
    2. otherwise a LOCODE port with that name but no coordinates is placed at the Natural Earth point
       (name clashes are settled by which country's ports lie nearest);
    3. otherwise a nearby LOCODE port whose name contains it (a terminal) is sized instead;
    4. otherwise the Natural Earth port is added as ``ne:<id>``, its country taken from the nearest port.

    Ports are appended to ``ports`` in place. Returns how many existing LOCODE ports were sized.
    """
    grid = geo.GridIndex(0.5)
    for i, p in enumerate(ports):
        grid.add(p["lat"], p["lon"], i)
    by_name: dict[str, list[dict]] = {}
    for m in missing or []:
        for key in {_name_key(m["name"]), _name_key(m.get("name_ascii"))} - {""}:
            by_name.setdefault(key, []).append(m)
    matched = placed = added = 0
    used: set[int] = set()
    placed_ids: set[str] = set()
    base = len(ports)
    for n in ne:
        key = _name_key(n["name"])
        best = fallback = None
        for d, i in grid.near(n["lat"], n["lon"], max_km):   # nearest first; an exact name beats a partial one
            if i in used or i >= base or not key:
                continue
            pk = _name_key(ports[i]["name"])
            pk2 = _name_key(ports[i].get("name_ascii"))
            if key == pk or key == pk2:
                best = i
                break
            if fallback is None and len(key) >= 4 and (key in pk or pk in key):
                fallback = i
        def size_existing(i):
            used.add(i)
            p = ports[i]
            p["size"] = n["size"]
            p["lat"], p["lon"] = n["lat"], n["lon"]
            p["position_note"] = "Position from Natural Earth"

        if best is not None:                       # 1. same name nearby, with coordinates
            size_existing(best)
            matched += 1
            continue
        cands = [c for c in by_name.get(key, []) if c["id"] not in placed_ids]
        choice = None
        if len(cands) == 1:
            choice = cands[0]
        elif cands:
            near: dict[str, float] = {}
            for d, i in grid.near(n["lat"], n["lon"], 300.0):
                near.setdefault(ports[i]["country"], d)
            ranked = sorted((near[c["country"]], c["country"]) for c in cands if c["country"] in near)
            if ranked:
                cc = ranked[0][1]
                same = [c for c in cands if c["country"] == cc]
                choice = same[0] if len(same) == 1 else None
        if choice is not None:                     # 2. same name, but UN/LOCODE gives no position
            placed_ids.add(choice["id"])
            p = dict(choice)
            p.update({"lat": n["lat"], "lon": n["lon"], "size": n["size"],
                      "position_note": "Position from Natural Earth (UN/LOCODE lists none)"})
            ports.append(p)
            placed += 1
            continue
        if fallback is not None:                   # 3. a nearby port whose name contains it
            size_existing(fallback)
            matched += 1
            continue
        cc = None
        for d, i in grid.near(n["lat"], n["lon"], 150.0, 1):
            cc = ports[i]["country"]
        ports.append({"id": f"ne:{n['ne_id']}", "locode": None, "name": n["name"], "name_ascii": None,
                      "lat": n["lat"], "lon": n["lon"], "country": cc, "country_name": countries.name(cc),
                      "functions": ["Port"], "size": n["size"], "source": "Natural Earth",
                      "position_note": "Position from Natural Earth" + ("" if cc else "; country unknown")})
        added += 1
    if stats is not None:
        stats.update({"sized": matched, "placed": placed, "added": added})
    return matched


# ---------------------------------------------------------------------------------------------
# World Port Index (CSV from msi.nga.mil; columns matched by name so edition changes are tolerated)
# ---------------------------------------------------------------------------------------------
def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def parse_wpi(csv_text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        return []
    idx = {_key(h): i for i, h in enumerate(header)}

    def col(row, *names):
        for n in names:
            i = idx.get(_key(n))
            if i is not None and i < len(row):
                v = row[i].strip()
                if v:
                    return v
        return None

    out = []
    for row in reader:
        lat = fnum(col(row, "Latitude", "LATITUDE", "Lat"))
        lon = fnum(col(row, "Longitude", "LONGITUDE", "Lon"))
        num = col(row, "World Port Index Number", "INDEX_NO", "Index Number")
        name = col(row, "Main Port Name", "PORT_NAME", "Port Name")
        if lat is None or lon is None or not num or not name or not valid_point(lat, lon):
            continue
        cc_raw = col(row, "Country Code", "COUNTRY", "Country")
        cc = countries.resolve(cc_raw)
        size_raw = (col(row, "Harbor Size", "HARBORSIZE", "Harbour Size") or "").upper()
        htype = col(row, "Harbor Type", "HARBORTYPE", "Harbour Type")
        shelter = col(row, "Shelter Afforded", "SHELTER")
        facilities = []
        for label, names in (("Container", ("Facilities - Container",)), ("Solid bulk", ("Facilities - Solid Bulk",)),
                             ("Liquid bulk", ("Facilities - Liquid Bulk",)), ("Oil terminal", ("Facilities - Oil Terminal",)),
                             ("LNG terminal", ("Facilities - LNG Terminal",)), ("Ro-ro", ("Facilities - Ro-Ro",)),
                             ("Breakbulk", ("Facilities - Breakbulk",)), ("Dry dock", ("Dry Dock",)),
                             ("Railway", ("Railway",))):
            v = (col(row, *names) or "").upper()
            if v in ("Y", "YES") or (label in ("Dry dock", "Railway") and v in ("S", "M", "L", "SMALL", "MEDIUM", "LARGE")):
                facilities.append(label)
        locode = col(row, "UN/LOCODE", "UNLOCODE")
        out.append({
            "id": f"wpi:{inum_str(num)}", "wpi": inum_str(num), "name": text(name, 100),
            "alt_name": text(col(row, "Alternate Port Name"), 100), "lat": lat, "lon": lon,
            "country": cc, "country_name": countries.name(cc) or text(cc_raw, 60),
            "region": text(col(row, "Region Name"), 80), "water_body": text(col(row, "World Water Body"), 80),
            "locode": text(locode, 8), "size": _WPI_SIZE.get(size_raw), "size_raw": size_raw or None,
            "harbour_type": _HARBOUR_TYPES.get((htype or "").upper(), text(htype, 40)),
            "harbour_use": text(col(row, "Harbor Use"), 40),
            "shelter": _SHELTER.get((shelter or "").upper(), text(shelter, 20)),
            "channel_depth_m": compact_num(fnum(col(row, "Channel Depth (m)")), 1),
            "cargo_pier_depth_m": compact_num(fnum(col(row, "Cargo Pier Depth (m)")), 1),
            "max_vessel_length_m": compact_num(fnum(col(row, "Maximum Vessel Length (m)")), 0),
            "max_vessel_draft_m": compact_num(fnum(col(row, "Maximum Vessel Draft (m)")), 1),
            "tidal_range_m": compact_num(fnum(col(row, "Tidal Range (m)")), 1),
            "facilities": facilities, "source": "World Port Index",
        })
    return out


def inum_str(value: str) -> str:
    v = fnum(value)
    return str(int(v)) if v is not None else re.sub(r"[^0-9A-Za-z]", "", value)[:12]


def merge_wpi_locode(wpi: list[dict], locode: list[dict]) -> list[dict]:
    """WPI ports first; LOCODE ports that WPI already covers (same code) are dropped."""
    covered = {p["locode"].replace(" ", "").upper() for p in wpi if p.get("locode")}
    grid = geo.GridIndex(0.5)
    for i, p in enumerate(wpi):
        grid.add(p["lat"], p["lon"], i)
    rest = []
    for p in locode:
        code = (p.get("locode") or "").replace(" ", "").upper()
        if code and code in covered:
            continue
        if not code:   # a Natural Earth-only port: drop it when the WPI lists the same port nearby
            key = _name_key(p["name"])
            if any(_name_key(wpi[i]["name"]) == key for _, i in grid.near(p["lat"], p["lon"], 25.0)):
                continue
        rest.append(p)
    return wpi + rest


# ---------------------------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------------------------
def write_outputs(ds, wpi_error: str | None = None) -> int:
    """Merge the processed UN/LOCODE list with the World Port Index (when built) into the served files."""
    try:
        loc_doc = json.loads(ds.path("ports-locode.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    locode = loc_doc["items"]
    wpi: list[dict] = []
    try:
        wpi = json.loads(ds.path("wpi.json").read_text(encoding="utf-8"))["items"]
    except (FileNotFoundError, ValueError, KeyError):
        wpi = []
    ports = merge_wpi_locode(wpi, locode) if wpi else list(locode)
    for p in ports:
        if p["id"].startswith("locode:") and p["id"][7:] in MAJOR_PORTS:
            p["size"] = "large"
    order = {"large": 4, "medium": 3, "small": 2, "very small": 1, None: 0}
    ports.sort(key=lambda p: order.get(p.get("size"), 0))
    ds.write_json("ports.geojson", fc([point_feature(p["lon"], p["lat"], {
        "id": p["id"], "n": p["name"][:60], "z": order.get(p.get("size"), 0)}) for p in ports]))
    # CONTRACT 9.1 file for the ships module. ascii_json() escapes every non-ASCII character, so a reader that opens it
    # with the Windows default code page still gets "Kakumäe", not mojibake.
    ds.write_bytes("ports.json", ascii_json([{"id": p["id"], "name": p["name"], "country": p.get("country"),
                                              "lat": round(p["lat"], 5), "lon": round(p["lon"], 5),
                                              "size": p.get("size")} for p in ports]))
    ds.write_json("ports-details.json", {"built_at": time.time(), "items": ports, "source": "wpi" if wpi else "locode",
                                         "wpi_error": None if wpi else (wpi_error or loc_doc.get("wpi_error")),
                                         "ne_matched": loc_doc.get("ne_matched")})
    return len(ports)


def build(ds) -> None:
    """UN/LOCODE ports (function 1) sized from Natural Earth, merged with the World Port Index if built."""
    locode, missing = parse_locode(decode_text(ds.download(LOCODE_CSV, "unlocode.csv", min_bytes=1_000_000, timeout=240)
                                   .read_bytes()), with_missing=True)
    if len(locode) < MIN_LOCODE:
        raise RuntimeError(f"UN/LOCODE looks incomplete ({len(locode)} ports)")
    matched = None
    stats: dict = {}
    try:
        ne = parse_ne_ports(decode_text(ds.download(NE_PORTS, "ne_10m_ports.geojson", min_bytes=50_000)
                            .read_bytes()))
        matched = merge_ne(locode, ne, missing, stats=stats)
        ds.note(f"Natural Earth: {stats.get('sized', 0)} ports sized, {stats.get('placed', 0)} placed "
                f"(no UN/LOCODE position), {stats.get('added', 0)} added")
    except Exception as err:
        ds.note(f"Natural Earth ports unavailable ({err})")
    wpi_error = None
    if not ds.path("wpi.json").exists():
        wpi_error = "World Port Index not built yet (msi.nga.mil did not answer); using UN/LOCODE port locations"
    ds.write_json("ports-locode.json", {"built_at": time.time(), "items": locode, "ne_matched": matched,
                                        "wpi_error": wpi_error})
    n = write_outputs(ds, wpi_error)
    ds.set_rows(n)
    ds.note(f"{len(locode):,} UN/LOCODE ports, {matched or 0:,} sized from Natural Earth; {n:,} ports served")


def build_wpi(ds, url: str = WPI_CSV, delays: tuple[float, ...] = (0.0, 20.0, 60.0)) -> None:
    """The World Port Index, retried with backoff. A failure leaves the dataset missing, so the
    dataset manager tries again on its next round (about every 30 minutes)."""
    marker = ds.raw("wpi.failed")
    if not ds.force and marker.exists() and time.time() - marker.stat().st_mtime < 6 * 3600:
        raise RuntimeError(f"World Port Index unavailable ({marker.read_text(encoding='utf-8')[:200]}); "
                           "next try within 6 hours")
    last: Exception | None = None
    wpi: list[dict] = []
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            path = ds.net.download(url, ds.raw("wpi.csv"), max_age_hours=ds.max_age_hours * 0.9, force=ds.force,
                                   timeout=120, min_bytes=200_000)
            wpi = parse_wpi(decode_text(path.read_bytes()))
            if len(wpi) >= MIN_WPI:
                break
            last = RuntimeError(f"only {len(wpi)} ports parsed")
            wpi = []
        except Exception as err:
            last = err
    ds.source(url)
    if not wpi:
        marker.write_text(str(last)[:200], encoding="utf-8")
        raise RuntimeError(f"World Port Index unavailable ({last})")
    if marker.exists():
        marker.unlink()
    ds.write_json("wpi.json", {"built_at": time.time(), "items": wpi})
    n = write_outputs(ds)
    ds.set_rows(len(wpi))
    ds.note(f"{len(wpi):,} World Port Index ports; {n:,} ports served with UN/LOCODE")


def subtitle(p: dict) -> str:
    size = p.get("size")
    lead = f"{size.capitalize()} port" if size else "Port"
    return ", ".join(x for x in (lead, p.get("locode"), p.get("country_name")) if x)


def collection(items: list[dict], built_at: float | None, aliases: dict[str, str] | None = None) -> Collection:
    rows = []
    for p in items:
        codes = [p["id"]]
        if p.get("locode"):
            codes.append(p["locode"].replace(" ", ""))
        rows.append({"id": p["id"], "title": p["name"], "subtitle": subtitle(p), "lat": p["lat"], "lon": p["lon"],
                     "weight": SIZE_WEIGHT.get(p.get("size"), 0.2), "country": p.get("country"), "codes": codes,
                     "text": " ".join(x for x in (p.get("alt_name"), p.get("name_ascii"), p.get("country_name")) if x),
                     "detail": p})
    return Collection("port", "infra.ports", rows, cell_deg=0.5, zoom=11, built_at=built_at,
                      meta={"aliases": aliases or {}})
