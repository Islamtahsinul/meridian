"""Small helpers shared by the North American camera adapters (``na_*.py``). No sources live here.

- :func:`declared_key`: a key's value, after making sure the core knows the name is a secret, so it
  is listed in Settings and redacted from logs, errors and raw-file metadata (the module only
  declares the keys it knows about by name);
- :func:`ibi_source`: a key-optional source on the shared IBI "511" developer API
  (``/api/v2/get/cameras``), used by many state and provincial 511 sites;
- :func:`iteris_rows`: camera views from an Iteris ATIS map feed (sites with a ``cameras`` list);
- :func:`html_images`: ``(url, alt)`` for the ``<img>`` tags of a small HTML page;
- :func:`template_resolver`: a resolver that builds a camera's image link from its native id, for
  sources whose image links differ only in the query string (``build.dedupe`` compares links
  without the query, so those rows carry no ``image`` and set ``dynamic`` instead);
- :func:`s3_path_style`: an S3 virtual-host link rewritten to the path style;
- :data:`US_STATES`: state and territory names by postal code.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable, Iterable

from ..common import FetchContext, NeedsKey, Source, clean, heading_from, http_url, make_camera, to_float
from .ibi511 import parse as parse_ibi

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
# territories that have their own ISO country code
US_TERRITORIES = {"PR": "Puerto Rico", "GU": "Guam", "VI": "US Virgin Islands", "AS": "American Samoa",
                  "MP": "Northern Mariana Islands"}


def declared_key(config, name: str, description: str = "", url: str = "") -> str | None:
    """The value of the key ``name`` (None when unset).

    The name is declared to the core first (``Config.declare``) when it is not yet: the Settings
    table then lists it and, more importantly, its value is redacted from every log line, error
    message and raw-file record even when it is only set in the environment."""
    if config is None:
        return None
    declared = getattr(config, "declared", None)
    declare = getattr(config, "declare", None)
    if callable(declare) and isinstance(declared, dict) and name not in declared:
        try:
            declare(name, description=description, url=url, module="cameras", secret=True)
        except Exception:  # bookkeeping only; never block a build
            pass
    value = config.get(name)
    return value if isinstance(value, str) and value.strip() else None


# ---------------------------------------------------------------------------------------------
# IBI 511 developer API
# ---------------------------------------------------------------------------------------------
def ibi_source(sid: str, name: str, owner: str, base: str, key_name: str, *, country: str, region: str,
               hosts: Iterable[str], coverage: str, signup: str | None = None, licence: str | None = None,
               refresh: int = 120, tags: tuple[str, ...] = ("traffic",), note: str = "") -> Source:
    """A key-optional source on the IBI 511 API shared by many traveller-information sites. Without
    its free developer key it reports "Needs <key>"; with one it lists every enabled camera view."""
    signup = signup or f"{base}/developers/doc"

    def fetch(fc: FetchContext) -> list[dict]:
        key = declared_key(fc.config, key_name, f"{name}: free developer key ({coverage}).", signup)
        if not key:
            raise NeedsKey(key_name)
        query = urllib.parse.urlencode({"key": key, "format": "json", "lang": "en"})
        data = fc.json(f"{base}/api/v2/get/cameras?{query}", "cameras.json", min_bytes=50, timeout=60)
        if not isinstance(data, list):
            raise RuntimeError("unexpected answer from the 511 API")
        rows, skipped = parse_ibi(data, fc.src)
        fc.skip(skipped)
        if note:
            fc.notes.append(note)
        return rows

    return Source(id=sid, name=name, owner=owner, url=signup, licence=licence or f"{name.replace(' cameras', '')} developer API terms",
                  licence_url=signup, attribution=owner, fetch=fetch, hosts=tuple(hosts), country=country,
                  region=region, refresh=refresh, tags=tags, needs_key=key_name, optional=True, coverage=coverage)


# ---------------------------------------------------------------------------------------------
# Iteris ATIS map feeds
# ---------------------------------------------------------------------------------------------
_ELEV = re.compile(r"\s*\belev(?:ation)?\.?\s*(\d{3,5})\b", re.I)
_LOOKING = re.compile(r"(?:camera\s+)?(?:looking\s+|facing\s+)?((?:north|south)?\s?(?:north|south|east|west))"
                      r"(\s+horizon)?(\s+zoom(?:ed)?)?", re.I)
_LETTERS = {"n": "north", "s": "south", "e": "east", "w": "west"}


def view_label(text: str) -> str:
    """A camera view's label in sentence case: "Camera Looking North Zoom" -> "facing north, zoomed",
    "Southeast Horizon" -> "facing southeast, horizon", "Road Surface View" -> "road surface view",
    "S Intersection" -> "south intersection". Codes such as "I-29" keep their case."""
    t = clean(text)
    m = _LOOKING.fullmatch(t)
    if m:
        out = "facing " + m.group(1).lower().replace(" ", "")
        return out + (", horizon" if m.group(2) else "") + (", zoomed" if m.group(3) else "")
    words = []
    for w in t.split(" "):
        low = w.lower()
        if low in _LETTERS:
            words.append(_LETTERS[low])
        elif (w.isupper() and len(w) > 1) or any(c.isdigit() for c in w):
            words.append(w)
        else:
            words.append(low)
    return " ".join(words)


def iteris_rows(data: Any, src: Source, *, max_age_s: float = 3 * 86400, now: float | None = None,
                tags: Iterable[str] = ()) -> tuple[list[dict], int]:
    """One row per camera view of an Iteris ATIS GeoJSON (``{features: [{id, geometry, properties:
    {name, route, cameras: [{id, name, description, image, updateTime}]}}]}``). Views whose last
    picture is older than ``max_age_s`` are skipped as offline."""
    rows: list[dict] = []
    skipped = 0
    now = time.time() if now is None else now
    feats = data.get("features") if isinstance(data, dict) else None
    for feat in feats or []:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or [None, None]
        lon, lat = (coords + [None, None])[:2] if isinstance(coords, list) else (None, None)
        site_id = clean(feat.get("id") or props.get("id"), 40)
        site = clean(props.get("name"))
        route = clean(props.get("route"), 40)
        for cam in props.get("cameras") or []:
            if not isinstance(cam, dict):
                continue
            updated = to_float(cam.get("updateTime"))
            if updated and now - updated > max_age_s:
                skipped += 1
                continue
            view = clean(cam.get("name"))
            desc = clean(cam.get("description"))
            elevation = None
            m = _ELEV.search(view)
            if m:
                elevation = round(int(m.group(1)) * 0.3048)
                view = _ELEV.sub("", view).strip(" -,")
            if " - " in view:                       # "Harlowton South - Northeast": the part after the dash
                place, _, view = view.rpartition(" - ")
                site = site or place
            label = view_label(view or desc)
            name = f"{site}, {label}" if site and label else (site or label)
            native = f"{site_id}-{clean(cam.get('id'), 40)}"
            plain = _ELEV.sub("", desc).strip(" -,").lower()      # "Northwest Elev 4199" only repeats the view
            # the heading comes from the label ("facing northwest"); the label itself is in the name
            row = make_camera(src, native, name, lat, lon, image=cam.get("image"), road=route,
                              heading=heading_from(label), direction=cam.get("direction"), elevation=elevation, tags=tags,
                              description=desc if plain and plain not in ((view or "").lower(), label.lower()) else None)
            if row:
                rows.append(row)
            else:
                skipped += 1
    return rows, skipped


# ---------------------------------------------------------------------------------------------
# HTML pages and links
# ---------------------------------------------------------------------------------------------
class _ImgParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img" and len(self.images) < 200:
            a = {k.lower(): (v or "") for k, v in attrs}
            if a.get("src"):
                self.images.append((a["src"], a.get("alt") or a.get("title") or ""))


def html_images(text: str, base: str, *, path_prefix: str = "") -> list[tuple[str, str]]:
    """``(absolute url, alt text)`` for each distinct ``<img>`` on a page (only http(s) links, and
    only under ``path_prefix`` when given). Reads at most the first 500 KB."""
    parser = _ImgParser()
    try:
        parser.feed((text or "")[:500_000])
        parser.close()
    except Exception:  # malformed markup: keep what was read
        pass
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for src, alt in parser.images:
        url = http_url(src, base)
        if not url or url in seen:
            continue
        if path_prefix and not urllib.parse.urlsplit(url).path.startswith(path_prefix):
            continue
        seen.add(url)
        out.append((url, clean(alt)))
    return out


def template_resolver(template: str, pattern: str) -> Callable[[dict, Any], str | None]:
    """A resolver that fills ``{id}`` in ``template`` with the camera's native id (only when the id
    matches ``pattern`` in full). No network: the link is fixed, only the query string differs."""
    rx = re.compile(pattern)

    def resolve(cam: dict, net) -> str | None:
        native = str(cam.get("id", "")).split(":", 1)[-1]
        if not rx.fullmatch(native):
            return None
        return template.format(id=urllib.parse.quote(native, safe=""))

    return resolve


_S3_VHOST = re.compile(r"^([a-z0-9][a-z0-9-]{1,61}[a-z0-9])\.s3(?:[.-]([a-z0-9-]+))?\.amazonaws\.com$")


def s3_path_style(url: str | None) -> str | None:
    """``https://bucket.s3.amazonaws.com/key`` -> ``https://s3.amazonaws.com/bucket/key`` (same object;
    the path style also works where the bucket's own host name does not resolve)."""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    m = _S3_VHOST.match((parts.hostname or "").lower())
    if not m:
        return url
    region = m.group(2)
    host = "s3.amazonaws.com" if not region or region == "us-east-1" else f"s3.{region}.amazonaws.com"
    return urllib.parse.urlunsplit((parts.scheme, host, f"/{m.group(1)}{parts.path}", parts.query, ""))


def state_name(code: Any) -> tuple[str, str | None]:
    """``("US", "Texas")`` for "TX"; territories get their own country code (``("PR", "Puerto Rico")``)."""
    c = clean(code, 4).upper()
    if c in US_TERRITORIES:
        return c, US_TERRITORIES[c]
    return "US", US_STATES.get(c)
