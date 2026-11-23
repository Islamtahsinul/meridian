"""Country lookups for geocoding outbreak reports and labelling hazards.

Names, ISO codes and label points come from the embedded Natural Earth table
(:mod:`countries_data`, public domain). Optional simplified boundaries (the ``countries`` dataset)
add point-in-country tests; without them the nearest label point is used.
"""

from __future__ import annotations

import re
import threading

from meridian import geo

from .common import norm
from .countries_data import COUNTRIES

# Extra spellings used by WHO, GDACS, MeteoAlarm and USGS place strings.
EXTRA_NAMES = {
    "CD": ["DR Congo", "DRC", "Democratic Republic of Congo", "Congo DR", "Congo-Kinshasa"],
    "CG": ["Republic of the Congo", "Congo-Brazzaville", "Congo"],
    "CI": ["Cote d'Ivoire", "Côte d’Ivoire"],
    "CZ": ["Czech Republic", "Czechia"],
    "GB": ["United Kingdom", "United Kingdom of Great Britain and Northern Ireland", "Great Britain", "UK", "England",
           "Scotland", "Wales", "Northern Ireland"],
    "US": ["United States of America", "United States", "USA", "U.S."],
    "RU": ["the Russian Federation", "Russian Federation"],
    "IR": ["Iran (Islamic Republic of)", "Islamic Republic of Iran"],
    "SY": ["Syrian Arab Republic"],
    "LA": ["Lao People's Democratic Republic", "Lao PDR", "Laos"],
    "KR": ["Republic of Korea", "South Korea", "Korea, Republic of"],
    "KP": ["Democratic People's Republic of Korea", "North Korea", "DPRK"],
    "VN": ["Viet Nam"],
    "TZ": ["United Republic of Tanzania", "Tanzania"],
    "BO": ["Bolivia (Plurinational State of)", "Plurinational State of Bolivia"],
    "VE": ["Venezuela (Bolivarian Republic of)", "Bolivarian Republic of Venezuela"],
    "MD": ["Republic of Moldova", "Moldova"],
    "MK": ["North Macedonia", "Republic of North Macedonia"],
    "PS": ["occupied Palestinian territory", "Palestine", "Gaza", "West Bank", "State of Palestine"],
    "TR": ["Türkiye", "Turkiye", "Turkey"],
    "CV": ["Cabo Verde", "Cape Verde"],
    "SZ": ["Eswatini", "Swaziland"],
    "TL": ["Timor-Leste", "Timor Leste", "East Timor"],
    "FM": ["Micronesia (Federated States of)", "Federated States of Micronesia"],
    "MM": ["Myanmar", "Burma"],
    "BN": ["Brunei Darussalam"],
    "VA": ["Holy See"],
    "KN": ["Saint Kitts and Nevis"],
    "VC": ["Saint Vincent and the Grenadines"],
    "LC": ["Saint Lucia"],
    "ST": ["Sao Tome and Principe", "São Tomé and Príncipe"],
    "GM": ["Gambia", "The Gambia"],
    "BS": ["Bahamas", "The Bahamas"],
    "NL": ["Netherlands (Kingdom of the)", "Kingdom of the Netherlands"],
    "XK": ["Kosovo"],
    "CN": ["China", "People's Republic of China"],
    "HK": ["Hong Kong SAR", "Hong Kong Special Administrative Region"],
    "TW": ["Taiwan"],
}

# Overseas departments and territories that Natural Earth folds into their parent country.
EXTRA_PLACES = [
    ("YT", "MYT", "Mayotte", -12.83, 45.14, (44.98, -13.0, 45.3, -12.64), "Africa", "Eastern Africa", ()),
    ("RE", "REU", "Réunion", -21.11, 55.53, (55.21, -21.39, 55.84, -20.87), "Africa", "Eastern Africa", ("Reunion",)),
    ("GF", "GUF", "French Guiana", 3.93, -53.13, (-54.6, 2.11, -51.62, 5.75), "South America", "South America", ()),
    ("GP", "GLP", "Guadeloupe", 16.25, -61.58, (-61.81, 15.83, -61.0, 16.52), "North America", "Caribbean", ()),
    ("MQ", "MTQ", "Martinique", 14.64, -61.02, (-61.23, 14.39, -60.81, 14.88), "North America", "Caribbean", ()),
]

# Places named inside disease names that must not be read as the country of the report.
DISEASE_PLACES = re.compile(r"crimean[- ]congo|guinea[- ]worm|middle east respiratory|west nile|rift valley|"
                            r"japanese encephalitis|marburg|lassa|ebola|zika|nipah|hendra|bundibugyo|sudan virus|"
                            r"reston|tai forest|chapare|oropouche|mayaro|kyasanur", re.I)

# WHO regions and multi-country wording: rough centres so region-wide reports still show up.
REGIONS = [
    ("WHO African Region", "Africa", 2.0, 20.0),
    ("African Region", "Africa", 2.0, 20.0),
    ("Region of the Americas", "the Americas", 10.0, -75.0),
    ("Americas", "the Americas", 10.0, -75.0),
    ("Eastern Mediterranean", "Eastern Mediterranean", 27.0, 40.0),
    ("European Region", "Europe", 50.0, 15.0),
    ("South-East Asia", "South-East Asia", 15.0, 95.0),
    ("Western Pacific", "Western Pacific", 10.0, 125.0),
]

_lock = threading.Lock()
_by_iso: dict[str, dict] = {}
_names: list[tuple[str, str]] = []        # (lower-case name, iso2), longest first
_norm_index: dict[str, str] = {}
_shapes: dict[str, list] = {}             # iso2 -> [(bbox, geometry)] from the optional dataset
_grid = geo.GridIndex(5.0)


def _build() -> None:
    seen: set[tuple[str, str]] = set()
    names: list[tuple[str, str]] = []
    for iso2, iso3, name, lat, lon, bbox, continent, subregion, others in [*COUNTRIES, *EXTRA_PLACES]:
        _by_iso[iso2] = {"iso2": iso2, "iso3": iso3, "name": name, "lat": lat, "lon": lon, "bbox": list(bbox),
                         "continent": continent, "subregion": subregion}
        _grid.add(lat, lon, iso2)
        for n in (name, *others, *EXTRA_NAMES.get(iso2, [])):
            key = (n.lower(), iso2)
            if len(n) >= 3 and key not in seen:
                seen.add(key)
                names.append(key)
                _norm_index.setdefault(norm(n), iso2)
    for iso2, extra in EXTRA_NAMES.items():
        for n in extra:
            if len(n) >= 2:
                _norm_index.setdefault(norm(n), iso2)
    names.sort(key=lambda t: -len(t[0]))
    _names.extend(names)


_build()


def get(iso2: str | None) -> dict | None:
    return _by_iso.get((iso2 or "").upper())


def name_of(iso2: str | None) -> str:
    c = get(iso2)
    return c["name"] if c else (iso2 or "")


def lookup(name: str | None) -> dict | None:
    """Country by exact (normalised) name, alias or ISO2/ISO3 code."""
    if not name:
        return None
    s = name.strip()
    if len(s) == 2 and s.upper() in _by_iso:
        return _by_iso[s.upper()]
    if len(s) == 3:
        for c in _by_iso.values():
            if c["iso3"] == s.upper():
                return c
    iso2 = _norm_index.get(norm(s))
    return _by_iso.get(iso2) if iso2 else None


def _scan(text: str) -> list[tuple[int, int, str]]:
    """(start, end, iso2) of country names in text, longest names first so "Niger" never matches
    inside "Nigeria" and "Guinea" never inside "Papua New Guinea"."""
    low = DISEASE_PLACES.sub(lambda m: " " * len(m.group(0)), text.lower())
    taken = bytearray(len(low))
    hits: list[tuple[int, int, str]] = []
    for name, iso2 in _names:
        start = 0
        while True:
            i = low.find(name, start)
            if i < 0:
                break
            j = i + len(name)
            start = j
            if (i > 0 and low[i - 1].isalnum()) or (j < len(low) and low[j].isalnum()):
                continue
            if any(taken[i:j]):
                continue
            for k in range(i, j):
                taken[k] = 1
            hits.append((i, j, iso2))
    hits.sort()
    return hits


def find_in_text(text: str | None, limit: int = 12) -> list[dict]:
    """Countries named in free text, in order of appearance, each once."""
    if not text:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for _, _, iso2 in _scan(text):
        if iso2 not in seen:
            seen.add(iso2)
            out.append(_by_iso[iso2])
            if len(out) >= limit:
                break
    return out


def first_mention(text: str | None) -> int | None:
    """Character offset of the first country name in text (None when there is none)."""
    if not text:
        return None
    hits = _scan(text)
    return hits[0][0] if hits else None


def find_region(text: str | None) -> tuple[str, float, float] | None:
    if not text:
        return None
    low = text.lower()
    for name, label, lat, lon in REGIONS:
        if name.lower() in low:
            return label, lat, lon
    return None


def load_shapes(features: list[dict]) -> int:
    """Install simplified boundaries: [{"iso2", "bbox", "geometry"}]."""
    shapes: dict[str, list] = {}
    for f in features:
        iso2 = f.get("iso2")
        if iso2 and f.get("geometry") and f.get("bbox"):
            shapes.setdefault(iso2, []).append((f["bbox"], f["geometry"]))
    with _lock:
        _shapes.clear()
        _shapes.update(shapes)
    return len(shapes)


def shape(iso2: str) -> dict | None:
    items = _shapes.get(iso2)
    return items[0][1] if items else None


def at(lat: float, lon: float) -> dict | None:
    """The country containing a point when boundaries are loaded. Without them: among countries
    whose bounding box holds the point, the one with the nearest label point; else the nearest
    label point within 400 km. Good enough for labels, never for anything that matters legally."""
    with _lock:
        shapes = dict(_shapes)
    if shapes:
        for iso2, items in shapes.items():
            for bbox, geom in items:
                if bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3] and geo.point_in_geometry(lon, lat, geom):
                    return _by_iso.get(iso2)
    if shapes:
        # offshore: the country with the nearest outline vertex within 150 km
        best = None
        for iso2, items in shapes.items():
            for bbox, geom in items:
                if not (bbox[0] - 2 <= lon <= bbox[2] + 2 and bbox[1] - 2 <= lat <= bbox[3] + 2):
                    continue
                d = _nearest_vertex_km(lat, lon, geom)
                if d <= 150 and (best is None or d < best[0]):
                    best = (d, iso2)
        return _by_iso.get(best[1]) if best else None
    best = None
    for c in _by_iso.values():
        w, s, e, n = c["bbox"]
        if s <= lat <= n and w <= lon <= e:
            d = geo.haversine_km(lat, lon, c["lat"], c["lon"])
            if d < 1500 and (best is None or d < best[0]):
                best = (d, c)
    if best:
        return best[1]
    near = _grid.near(lat, lon, 400.0, 1)
    return _by_iso.get(near[0][1]) if near else None


def _nearest_vertex_km(lat: float, lon: float, geom: dict) -> float:
    best = 1e9
    stack = [geom.get("coordinates")]
    while stack:
        c = stack.pop()
        if not c:
            continue
        if isinstance(c[0], (int, float)):
            if abs(c[1] - lat) < 3 and abs(c[0] - lon) < 3:
                best = min(best, geo.haversine_km(lat, lon, c[1], c[0]))
        else:
            stack.extend(c)
    return best


def all_countries() -> list[dict]:
    return list(_by_iso.values())
