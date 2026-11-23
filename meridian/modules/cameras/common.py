"""Shared helpers for the camera adapters: one normalised camera row, text and direction cleaning,
tag inference, safe XML and JSON parsing, and paged ArcGIS / Socrata readers.

Every adapter turns its source's records into rows made by :func:`make_camera`. Rows are plain
dicts so they serialise straight into ``data/build/cameras/cameras.json``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

KINDS = ("image", "video", "hls", "mjpeg", "page")
LIVE_KINDS = ("video", "hls", "mjpeg")
TAGS = ("traffic", "urban", "coastal", "mountain", "weather", "port", "airport", "bridge", "tunnel",
        "border", "wildfire", "river", "volcano", "polar")
# What a camera is for, so the map's "Show" filter can separate them: road and traffic cameras run by
# road agencies, or webcams (scenic, weather, city, nature, volcano and research-station cameras).
CAMERA_TYPES = ("traffic", "webcam")

COUNTRY_NAMES = {
    "AU": "Australia", "CA": "Canada", "DE": "Germany", "ES": "Spain", "FI": "Finland", "GB": "United Kingdom",
    "HK": "Hong Kong", "IS": "Iceland", "NZ": "New Zealand", "SG": "Singapore", "US": "United States",
    "SE": "Sweden", "NO": "Norway", "IE": "Ireland", "FR": "France", "IT": "Italy", "AT": "Austria",
    "CH": "Switzerland", "JP": "Japan", "MX": "Mexico", "PT": "Portugal", "NL": "Netherlands",
    "DK": "Denmark", "PL": "Poland", "CZ": "Czechia", "SI": "Slovenia", "HR": "Croatia", "GR": "Greece",
    "TW": "Taiwan", "KR": "South Korea", "ZA": "South Africa", "BR": "Brazil", "AR": "Argentina",
    "CL": "Chile", "BE": "Belgium", "LU": "Luxembourg", "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania",
    # the rest of ISO 3166-1 (plus XK Kosovo), so worldwide sources get country names too
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan", "AG": "Antigua and Barbuda",
    "AI": "Anguilla", "AL": "Albania", "AM": "Armenia", "AO": "Angola", "AQ": "Antarctica", "AS": "American Samoa",
    "AW": "Aruba", "AX": "Åland Islands", "AZ": "Azerbaijan", "BA": "Bosnia and Herzegovina", "BB": "Barbados",
    "BD": "Bangladesh", "BF": "Burkina Faso", "BG": "Bulgaria", "BH": "Bahrain", "BI": "Burundi", "BJ": "Benin",
    "BL": "Saint Barthélemy", "BM": "Bermuda", "BN": "Brunei", "BO": "Bolivia", "BQ": "Caribbean Netherlands",
    "BS": "Bahamas", "BT": "Bhutan", "BV": "Bouvet Island", "BW": "Botswana", "BY": "Belarus", "BZ": "Belize",
    "CC": "Cocos (Keeling) Islands", "CD": "DR Congo", "CF": "Central African Republic", "CG": "Congo",
    "CI": "Côte d'Ivoire", "CK": "Cook Islands", "CM": "Cameroon", "CN": "China", "CO": "Colombia",
    "CR": "Costa Rica", "CU": "Cuba", "CV": "Cape Verde", "CW": "Curaçao", "CX": "Christmas Island",
    "CY": "Cyprus", "DJ": "Djibouti", "DM": "Dominica", "DO": "Dominican Republic", "DZ": "Algeria",
    "EC": "Ecuador", "EG": "Egypt", "EH": "Western Sahara", "ER": "Eritrea", "ET": "Ethiopia", "FJ": "Fiji",
    "FK": "Falkland Islands", "FM": "Micronesia", "FO": "Faroe Islands", "GA": "Gabon", "GD": "Grenada",
    "GE": "Georgia", "GF": "French Guiana", "GG": "Guernsey", "GH": "Ghana", "GI": "Gibraltar", "GL": "Greenland",
    "GM": "Gambia", "GN": "Guinea", "GP": "Guadeloupe", "GQ": "Equatorial Guinea",
    "GS": "South Georgia and the South Sandwich Islands", "GT": "Guatemala", "GU": "Guam", "GW": "Guinea-Bissau",
    "GY": "Guyana", "HM": "Heard Island and McDonald Islands", "HN": "Honduras", "HT": "Haiti", "HU": "Hungary",
    "ID": "Indonesia", "IL": "Israel", "IM": "Isle of Man", "IN": "India", "IO": "British Indian Ocean Territory",
    "IQ": "Iraq", "IR": "Iran", "JE": "Jersey", "JM": "Jamaica", "JO": "Jordan", "KE": "Kenya",
    "KG": "Kyrgyzstan", "KH": "Cambodia", "KI": "Kiribati", "KM": "Comoros", "KN": "Saint Kitts and Nevis",
    "KP": "North Korea", "KW": "Kuwait", "KY": "Cayman Islands", "KZ": "Kazakhstan", "LA": "Laos",
    "LB": "Lebanon", "LC": "Saint Lucia", "LI": "Liechtenstein", "LK": "Sri Lanka", "LR": "Liberia",
    "LS": "Lesotho", "LY": "Libya", "MA": "Morocco", "MC": "Monaco", "MD": "Moldova", "ME": "Montenegro",
    "MF": "Saint Martin", "MG": "Madagascar", "MH": "Marshall Islands", "MK": "North Macedonia", "ML": "Mali",
    "MM": "Myanmar", "MN": "Mongolia", "MO": "Macao", "MP": "Northern Mariana Islands", "MQ": "Martinique",
    "MR": "Mauritania", "MS": "Montserrat", "MT": "Malta", "MU": "Mauritius", "MV": "Maldives", "MW": "Malawi",
    "MY": "Malaysia", "MZ": "Mozambique", "NA": "Namibia", "NC": "New Caledonia", "NE": "Niger",
    "NF": "Norfolk Island", "NG": "Nigeria", "NI": "Nicaragua", "NP": "Nepal", "NR": "Nauru", "NU": "Niue",
    "OM": "Oman", "PA": "Panama", "PE": "Peru", "PF": "French Polynesia", "PG": "Papua New Guinea",
    "PH": "Philippines", "PK": "Pakistan", "PM": "Saint Pierre and Miquelon", "PN": "Pitcairn Islands",
    "PR": "Puerto Rico", "PS": "Palestine", "PW": "Palau", "PY": "Paraguay", "QA": "Qatar", "RE": "Réunion",
    "RO": "Romania", "RS": "Serbia", "RU": "Russia", "RW": "Rwanda", "SA": "Saudi Arabia",
    "SB": "Solomon Islands", "SC": "Seychelles", "SD": "Sudan", "SH": "Saint Helena", "SJ": "Svalbard and Jan Mayen",
    "SK": "Slovakia", "SL": "Sierra Leone", "SM": "San Marino", "SN": "Senegal", "SO": "Somalia", "SR": "Suriname",
    "SS": "South Sudan", "ST": "São Tomé and Príncipe", "SV": "El Salvador", "SX": "Sint Maarten", "SY": "Syria",
    "SZ": "Eswatini", "TC": "Turks and Caicos Islands", "TD": "Chad", "TF": "French Southern Territories",
    "TG": "Togo", "TH": "Thailand", "TJ": "Tajikistan", "TK": "Tokelau", "TL": "Timor-Leste", "TM": "Turkmenistan",
    "TN": "Tunisia", "TO": "Tonga", "TR": "Türkiye", "TT": "Trinidad and Tobago", "TV": "Tuvalu", "TZ": "Tanzania",
    "UA": "Ukraine", "UG": "Uganda", "UM": "US Minor Outlying Islands", "UY": "Uruguay", "UZ": "Uzbekistan",
    "VA": "Vatican City", "VC": "Saint Vincent and the Grenadines", "VE": "Venezuela", "VG": "British Virgin Islands",
    "VI": "US Virgin Islands", "VN": "Vietnam", "VU": "Vanuatu", "WF": "Wallis and Futuna", "WS": "Samoa",
    "XK": "Kosovo", "YE": "Yemen", "YT": "Mayotte", "ZM": "Zambia", "ZW": "Zimbabwe",
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_SPACES = re.compile(r"\s+")
_ID_BAD = re.compile(r"[^A-Za-z0-9._~-]+")


# ---------------------------------------------------------------------------------------------
# Text and numbers
# ---------------------------------------------------------------------------------------------
def clean(value: Any, limit: int = 160) -> str:
    """Single-line, trimmed, control-free text (never None)."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _CONTROL.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def title_case(text: str) -> str:
    """'SANTA CRUZ DE TENERIFE' -> 'Santa Cruz de Tenerife' (only for all-caps or all-lower text)."""
    text = clean(text)
    if not text or (text != text.upper() and text != text.lower()):
        return text
    small = {"de", "del", "la", "las", "los", "y", "el", "of", "and", "the", "da", "do", "di"}
    words = []
    for i, w in enumerate(text.lower().split(" ")):
        words.append(w if (i and w in small) else w[:1].upper() + w[1:])
    out = " ".join(words)
    return re.sub(r"([-/(])([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)


def to_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def to_int(value: Any) -> int | None:
    f = to_float(value)
    return int(f) if f is not None else None


def sanitize_id(native: Any) -> str:
    """A path-safe native id: letters, digits and ``._~-`` only (other runs become ``_``)."""
    text = _ID_BAD.sub("_", clean(native, 120)).strip("_")
    return text[:96]


def fold(text: str) -> str:
    """Lower case without accents, for search tokens."""
    norm = unicodedata.normalize("NFKD", text or "")
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return norm.lower().replace("ð", "d").replace("þ", "th").replace("æ", "ae").replace("ø", "o").replace("ß", "ss")


def http_url(value: Any, base: str | None = None) -> str | None:
    """The URL if it is absolute http(s) (after joining ``base`` for relative paths), else None."""
    text = clean(value, 2000)
    if not text:
        return None
    if base and not re.match(r"^[a-z][a-z0-9+.-]*:", text, re.I):
        text = urllib.parse.urljoin(base, text)
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, parts.path.replace(" ", "%20"),
                                    parts.query.replace(" ", "%20"), ""))


# ---------------------------------------------------------------------------------------------
# Directions
# ---------------------------------------------------------------------------------------------
_COMPASS = {
    "n": 0, "nne": 22.5, "ne": 45, "ene": 67.5, "e": 90, "ese": 112.5, "se": 135, "sse": 157.5, "s": 180,
    "ssw": 202.5, "sw": 225, "wsw": 247.5, "w": 270, "wnw": 292.5, "nw": 315, "nnw": 337.5,
}
# whole words only, so street names such as "Eastcheap" or "Austurvegur" never give a heading
_WORD_RX = re.compile(r"\b(?:(north|south)[ -]?)?(north|south|east|west)(?:bound|wards?|erly)?\b")
_WORD_DEG = {"north": 0, "south": 180, "east": 90, "west": 270, "northeast": 45, "northwest": 315,
             "southeast": 135, "southwest": 225}
_ICE_RX = re.compile(r"\b(norð|suð)?(austur|vestur|ur)s?\b")
_ICE_DEG = {"norður": 0, "suður": 180, "austur": 90, "vestur": 270, "norðaustur": 45, "norðvestur": 315,
            "suðaustur": 135, "suðvestur": 225}
COMPASS_LABELS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def heading_from(value: Any) -> float | None:
    """Degrees from a number, a compass code (N, NE, NB, SB, EB...) or words (Northbound, looking west)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        f = float(value)
        return round(f % 360.0, 1) if math.isfinite(f) else None
    text = clean(value, 80).lower()
    if not text:
        return None
    num = to_float(text)
    if num is not None:
        return round(num % 360.0, 1)
    code = re.sub(r"[^a-z]", "", text)
    if code in _COMPASS:
        return float(_COMPASS[code])
    if len(code) == 2 and code.endswith("b") and code[0] in "nsew":      # NB, SB, EB, WB
        return float(_COMPASS[code[0]])
    for m in _ICE_RX.finditer(text):
        word = (m.group(1) or "") + m.group(2)
        if word in _ICE_DEG:
            return float(_ICE_DEG[word])
    for m in _WORD_RX.finditer(text):
        first, second = m.group(1), m.group(2)
        word = (first + second) if first and second in ("east", "west") else second if not first else first
        if word in _WORD_DEG:
            return float(_WORD_DEG[word])
    return None


def compass_label(deg: float | None) -> str | None:
    if deg is None:
        return None
    return COMPASS_LABELS[int(((deg % 360) + 22.5) // 45) % 8]


# ---------------------------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------------------------
_TAG_RULES = [
    ("airport", re.compile(r"\b(airport|aeropuerto|aeroport|flughafen|lentoasema|flugv[oö]ll|terminal \d)", re.I)),
    ("port", re.compile(r"\b(port of|harbou?r|seaport|ferry terminal|marina|wharf|pier|puerto|satama|höfn)\b", re.I)),
    ("coastal", re.compile(r"\b(beach|coast|coastal|bay|seafront|esplanade|playa|ranta|cliffs?|ocean|lighthouse)\b", re.I)),
    ("bridge", re.compile(r"\b(bridge|viaduct|puente|silta|brücke|brucke)\b", re.I)),
    ("tunnel", re.compile(r"\b(tunnel|túnel|tunel|tunneli|göng)\b", re.I)),
    ("mountain", re.compile(r"\b(pass|summit|mtn|mountain|peak|ridge|col de|heiði|heidi|tunturi|grade)\b", re.I)),
    ("border", re.compile(r"\b(border|frontier|customs|aduana|frontera)\b", re.I)),
    ("river", re.compile(r"\b(river|creek|joki)\b", re.I)),
    ("volcano", re.compile(r"\b(volcano|volcanic|volc[aá]n|vulkan|volcan|crater|caldera)\b", re.I)),
]


def infer_tags(text: str, base: Iterable[str] = ()) -> list[str]:
    tags = [t for t in base if t in TAGS]
    for tag, rx in _TAG_RULES:
        if tag not in tags and rx.search(text or ""):
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------
def valid_coord(lat: Any, lon: Any) -> tuple[float, float] | None:
    la, lo = to_float(lat), to_float(lon)
    if la is None or lo is None:
        return None
    if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        return None
    if abs(la) < 0.01 and abs(lo) < 0.01:      # "null island" placeholders
        return None
    return round(la, 5), round(lo, 5)


def source_camera_type(src: "Source") -> str:
    """The source's camera type: its ``camera_type`` when set, else "traffic" for road-agency feeds
    (``traffic`` among its tags) and "webcam" for everything else."""
    declared = (getattr(src, "camera_type", "") or "").lower()
    if declared in CAMERA_TYPES:
        return declared
    return "traffic" if "traffic" in (getattr(src, "tags", ()) or ()) else "webcam"


def make_camera(src: "Source", native_id: Any, name: Any, lat: Any, lon: Any, *, image: Any = None,
                stream: Any = None, stream_kind: str | None = None, page: Any = None, region: Any = None,
                area: Any = None, road: Any = None, country: str | None = None, heading: Any = None,
                direction: Any = None, refresh: Any = None, tags: Iterable[str] = (), elevation: Any = None,
                dynamic: bool = False, description: Any = None, camera_type: str | None = None,
                direct: bool = False) -> dict | None:
    """One normalised camera row, or None when it is unusable (no position, no picture and no page).

    ``camera_type`` ("traffic" or "webcam") defaults to the source's (see :func:`source_camera_type`).
    ``direct=True`` marks a camera whose picture the browser loads straight from the source with a
    fresh link from ``Source.direct`` (never proxied); such rows need no ``image``."""
    nid = sanitize_id(native_id)
    if not nid:
        return None
    coord = valid_coord(lat, lon)
    if coord is None:
        return None
    image_url = http_url(image)
    stream_url = http_url(stream)
    page_url = http_url(page)
    if direct:
        dynamic = True
    if not (image_url or stream_url or page_url or dynamic):
        return None
    kind = "image"
    if stream_url:
        kind = stream_kind if stream_kind in LIVE_KINDS else "hls" if ".m3u8" in stream_url.lower() else "video"
    elif not image_url and not dynamic:
        kind = "page"
    title = clean(name, 140) or f"Camera {nid}"
    road_t = clean(road, 80)
    area_t = clean(area, 100)
    region_t = clean(region, 80)
    desc_t = clean(description, 200)
    dir_t = clean(direction, 60)
    head = heading_from(heading) if heading is not None and heading != "" else heading_from(direction)
    cadence = to_int(refresh) or src.refresh
    cadence = max(15, min(3600, cadence))
    row = {
        "id": f"{src.id}:{nid}",
        "src": src.id,
        "name": title,
        "lat": coord[0],
        "lon": coord[1],
        "country": (country or src.country or "").upper()[:2] or None,
        "region": region_t or src.region or None,
        "area": area_t or None,
        "road": road_t or None,
        "kind": kind,
        "image": image_url,
        "stream": stream_url,
        "page": page_url,
        "refresh": cadence,
        "tags": infer_tags(" ".join([title, area_t, road_t, desc_t]), list(src.tags) + list(tags)),
        "heading": head,
        "direction": dir_t or None,
        "attribution": src.attribution,
        "licence": src.licence,
        "type": (camera_type or "").lower() if (camera_type or "").lower() in CAMERA_TYPES else source_camera_type(src),
    }
    if desc_t and desc_t != title:
        row["description"] = desc_t
    elev = to_float(elevation)
    if elev is not None and -500 < elev < 9000:
        row["elevation"] = round(elev)
    if dynamic:
        row["dynamic"] = True
    if direct:
        row["direct"] = True
    return {k: v for k, v in row.items() if v is not None and v != [] and v != ""}


# ---------------------------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------------------------
class NeedsKey(Exception):
    """Raised by an adapter whose source needs an API key that is not configured."""

    def __init__(self, key_name: str):
        super().__init__(f"needs {key_name}")
        self.key_name = key_name


class Pending(Exception):
    """Raised by an adapter whose list is not ready yet (for example a first crawl still running).
    The source keeps its previous rows and is shown as waiting, not failed."""


@dataclass
class Source:
    """A camera source and its adapter. ``fetch(fc)`` returns camera rows; ``resolve`` (optional)
    returns the current image URL for sources whose image links change every few minutes.

    ``direct`` (optional) is for sources whose terms say the browser must load each picture from
    the source itself, with a link that only the source can give (Windy): ``direct(rows, net)``
    returns ``{camera id: {"image", "width", "height", "link", ...}}`` and the image proxy never
    fetches those cameras. ``courtesy`` is the credit line such sources ask for, as
    ``((text, url), ...)``, shown with every picture."""

    id: str
    name: str
    owner: str
    url: str
    licence: str
    attribution: str
    fetch: Callable[["FetchContext"], list[dict]] | None = None
    resolve: Callable[[dict, Any], str | None] | None = None
    hosts: tuple[str, ...] = ()          # allowed image hosts: "host", "*.domain" or "host/path-prefix"
    ports: tuple[int, ...] = ()          # extra allowed ports for images (default ports always allowed)
    country: str = ""
    region: str = ""
    refresh: int = 120                   # typical image cadence, seconds
    tags: tuple[str, ...] = ("traffic",)
    stream_cors: bool = False            # the browser can play its streams directly
    needs_key: str | None = None
    optional: bool = False               # often blocked or keyed: failures are expected
    licence_url: str = ""
    coverage: str = ""                   # where it covers, for the docs and status
    camera_type: str = ""                # "traffic" or "webcam"; empty: "traffic" when tagged traffic
    direct: Callable[[list[dict], Any], dict[str, dict]] | None = None
    direct_ttl: float = 300.0            # how long a ``direct`` answer may be reused, seconds
    courtesy: tuple[tuple[str, str], ...] = ()
    aggregates: bool = False             # re-lists other feeds' cameras: yields to any camera of another
                                         # source within ``build.YIELD_M`` metres (Windy)
    image_timeout: float = 10.0          # seconds the image proxy waits for this source's host (3-30)


class FetchContext:
    """What an adapter's ``fetch`` receives: raw downloads under data/raw/cameras/<source>/.
    ``extra`` holds small facts an adapter wants kept in its source status (JSON values)."""

    def __init__(self, ds, src: Source, *, config=None, net=None, log=None, budget_s: float = 240.0):
        self.ds = ds
        self.src = src
        self.config = config if config is not None else getattr(ds, "config", None)
        self.net = net if net is not None else getattr(ds, "net", None)
        self.log = log if log is not None else getattr(ds, "log", None)
        self.notes: list[str] = []
        self.extra: dict = {}
        self.skipped = 0
        self.started = time.monotonic()
        self.budget_s = budget_s
        self.force = bool(getattr(ds, "force", False))

    def key(self, name: str) -> str | None:
        return self.config.get(name) if self.config is not None else None

    def time_left(self) -> float:
        return self.budget_s - (time.monotonic() - self.started)

    def raw_path(self, filename: str) -> Path:
        return self.ds.raw(f"{self.src.id}/{filename}")

    def download(self, url: str, filename: str, *, max_age_hours: float | None = None, min_bytes: int = 2,
                 timeout: float = 60.0, headers: dict | None = None, min_interval: float | None = None,
                 force: bool | None = None) -> Path:
        """Fetch ``url`` into the raw cache (skipped while fresh; an older copy survives a failure)."""
        path = self.raw_path(filename)
        age = (max_age_hours if max_age_hours is not None else getattr(self.ds, "max_age_hours", 24.0)) * 0.9
        out = self.net.download(url, path, max_age_hours=age, force=self.force if force is None else force,
                                timeout=timeout, headers=headers, min_bytes=min_bytes, min_interval=min_interval)
        try:
            hours = (time.time() - out.stat().st_mtime) / 3600.0
            if hours > max(26.0, age * 1.5):
                self.notes.append(f"using a cached copy {hours:.0f} h old")
        except OSError:
            pass
        return out

    def json(self, url: str, filename: str, **kw) -> Any:
        return read_json(self.download(url, filename, **kw))

    def xml(self, url: str, filename: str, **kw) -> ET.Element:
        return parse_xml(read_bytes(self.download(url, filename, **kw)))

    def skip(self, n: int = 1) -> None:
        self.skipped += n


# ---------------------------------------------------------------------------------------------
# Parsing untrusted downloads
# ---------------------------------------------------------------------------------------------
def gunzip_limited(data: bytes, limit: int) -> bytes:
    """gzip.decompress that stops at ``limit`` bytes of output, so a small hostile file (a "gzip
    bomb") can't inflate to gigabytes in memory before the size check. Handles multi-member files."""
    out = bytearray()
    rest = data
    while rest:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out += d.decompress(rest, limit + 1 - len(out))
        if len(out) > limit or d.unconsumed_tail:
            raise ValueError("download is unexpectedly large")
        if not d.eof:
            raise EOFError("compressed file ended before the end-of-stream marker was reached")
        rest = d.unused_data.lstrip(b"\x00")
    return bytes(out)


def read_bytes(path: Path, limit: int = 120_000_000) -> bytes:
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError("download is unexpectedly large")
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gunzip_limited(data, limit)
    if len(data) > limit:
        raise ValueError("download is unexpectedly large")
    return data


def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def read_json(path: Path) -> Any:
    return json.loads(decode_text(read_bytes(path)))


def parse_json_bytes(data: bytes, limit: int = 120_000_000) -> Any:
    if data[:2] == b"\x1f\x8b":
        data = gunzip_limited(data, limit)
    return json.loads(decode_text(data))


def parse_xml(data: bytes) -> ET.Element:
    """XML without DTDs or entities (refused outright), so no entity expansion tricks."""
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data[:200_000].lower():
        raise ValueError("XML with a DTD or entities is refused")
    return ET.fromstring(data)


def strip_ns(root: ET.Element) -> ET.Element:
    """Drop namespaces in place so ``find('a/b')`` works on DATEX-style documents."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        if el.attrib:
            for k in list(el.attrib):
                if "}" in k:
                    el.attrib[k.split("}", 1)[1]] = el.attrib.pop(k)
    return root


# ---------------------------------------------------------------------------------------------
# Paged readers
# ---------------------------------------------------------------------------------------------
def arcgis_features(fc: FetchContext, layer_url: str, *, where: str = "1=1", out_fields: str = "*",
                    page_size: int = 1000, max_pages: int = 30, prefix: str = "page",
                    order_by: str | None = None, geometry: bool = True) -> list[dict]:
    """Every feature of an ArcGIS FeatureServer/MapServer layer (WGS84), paged by ``resultOffset``."""
    feats: list[dict] = []
    tag = hashlib.sha1(f"{layer_url}|{where}|{out_fields}|{order_by}|{page_size}".encode()).hexdigest()[:8]
    for page in range(max_pages):
        params = {"where": where, "outFields": out_fields, "f": "json", "outSR": "4326",
                  "returnGeometry": "true" if geometry else "false", "resultOffset": page * page_size,
                  "resultRecordCount": page_size}
        if order_by:
            params["orderByFields"] = order_by
        url = f"{layer_url.rstrip('/')}/query?{urllib.parse.urlencode(params)}"
        data = fc.json(url, f"{prefix}-{tag}-{page}.json", timeout=90)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"ArcGIS error: {clean(data['error'].get('message'), 200)}")
        if not isinstance(data, dict):
            raise RuntimeError("unexpected ArcGIS response")
        batch = data.get("features") or []
        feats.extend(f for f in batch if isinstance(f, dict))
        if not batch or not data.get("exceededTransferLimit"):
            break
    return feats


def arcgis_point(feature: dict) -> tuple[float | None, float | None]:
    g = feature.get("geometry") or {}
    return to_float(g.get("y")), to_float(g.get("x"))


def socrata_rows(fc: FetchContext, url: str, *, limit: int = 5000, max_pages: int = 10,
                 prefix: str = "page", where: str | None = None) -> list[dict]:
    rows: list[dict] = []
    for page in range(max_pages):
        params = {"$limit": limit, "$offset": page * limit, "$order": ":id"}
        if where:
            params["$where"] = where
        data = fc.json(f"{url}?{urllib.parse.urlencode(params)}", f"{prefix}-{page}.json", timeout=90)
        if not isinstance(data, list):
            raise RuntimeError("unexpected Socrata response")
        rows.extend(r for r in data if isinstance(r, dict))
        if len(data) < limit:
            break
    return rows
