"""Slovenia: motorway cameras of DARS and state-road cameras of DRSI, from the Slovenian National
Access Point (NAP, run by NCUP for the Ministry of Infrastructure): the "Cestne kamere (GeoJSON)"
dataset, about 320 cameras at 230 sites.

The NAP gives the data free of charge under CC BY-SA 4.0, but only to registered users: register
at https://www.nap.si (e-mail confirmation), then ask for the camera dataset on your profile page.
Put the NAP login in ``SLOVENIA_NAP_KEY`` as ``email:password``. The adapter swaps it for a
short-lived OAuth2 access token (password grant at ``b2b.nap.si/uc/user/token``, as the NAP's B2B
instructions describe) and fetches the GeoJSON with that token. The login and the token are only
sent to b2b.nap.si and never written to disk.
"""

from __future__ import annotations

import re
import time
import urllib.parse

from meridian.net import NetError

from ..common import FetchContext, NeedsKey, Source, clean, make_camera, read_json

KEY = "SLOVENIA_NAP_KEY"
TOKEN_URL = "https://b2b.nap.si/uc/user/token"
DATA_URL = "https://b2b.nap.si/data/b2b.cameras.geojson"
DATASET_URL = "https://www.nap.si/en/datasets_details?id=e5e4ee6a-48f3-3ca4-9d11-71596ee6154c"
RAW_FILE = "cameras.geojson"

# "A1/E61/E70, ..." or "R2-409, ..." or "A1/E57 Maribor - Ljubljana, ...": the road code(s) first
_ROAD = re.compile(r"^((?:[AHGR]\d{1,3}(?:-\d{1,4})?|E\d{1,3})(?:/(?:[AHGR]\d{1,3}(?:-\d{1,4})?|E\d{1,3}))*)(?=[\s,]|$)[\s,]*")
_TOWARDS = re.compile(r"\b(?:view )?in the direction of\b", re.I)
_SUFFIX = re.compile(r"_[A-Z]{1,3}$")              # "Prilipe_NM" -> "Prilipe"
_DASHES = (" - ", " – ", " — ")
_BYPASS = re.compile(r"\b(?:bypass|baypass|obvoznica)\b", re.I)


def credentials(fc: FetchContext) -> tuple[str, str]:
    """(user, password) from SLOVENIA_NAP_KEY ('email:password'); NeedsKey when unset."""
    raw = fc.key(KEY)
    if not raw:
        raise NeedsKey(KEY)
    user, sep, password = raw.partition(":")
    if not sep or not user.strip() or not password:
        raise RuntimeError(f"{KEY} must look like email:password (your NAP Slovenia login)")
    return user.strip(), password


def split_text(text: str) -> tuple[str, str, str]:
    """'A1/E61/E70, Ljubljana - Koper, Log pri Brezovici, rotating camera I'
    -> ('A1', 'Ljubljana - Koper', 'Log pri Brezovici, rotating camera I')."""
    text = clean(text, 300).replace(" ,", ",")
    road = ""
    m = _ROAD.match(text)
    if m:
        road = m.group(1).split("/")[0]
        text = text[m.end():]
    parts = [p.strip() for p in text.split(",") if p.strip()]
    route = ""
    if len(parts) >= 2 and (len(parts) >= 3 or any(d in parts[0] for d in _DASHES) or _BYPASS.search(parts[0])):
        route, parts = parts[0], parts[1:]
    name = ", ".join(parts)
    name = _TOWARDS.sub("towards", name)
    return road, route, clean(name, 140)


def parse(data, src: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    feats = data.get("features") if isinstance(data, dict) else None
    for feat in feats or []:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        group = props.get("group") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        items = [it for it in props.get("items") or [] if isinstance(it, dict)]
        if len(coords) < 2:
            skipped += len(items) or 1
            continue
        lon, lat = coords[0], coords[1]
        group_title = clean(group.get("title_eng") or group.get("title_slo"))
        for it in items:
            if str(it.get("active", 1)) not in ("1", "True", "true"):
                skipped += 1
                continue
            text = it.get("text_eng") or it.get("text_slo") or group_title
            road, route, name = split_text(text)
            place = _SUFFIX.sub("", clean(it.get("title_eng") or it.get("title_slo") or it.get("name")))
            if not road:
                road = split_text(group_title)[0]
            if not name or name.lower().startswith("towards "):
                name = f"{place}, {name}" if place and name else (name or place or group_title)
            name = name[:1].upper() + name[1:]
            native = it.get("LegacyId") if it.get("LegacyId") not in (None, "") else it.get("EntityId")
            row = make_camera(src, native, name, lat, lon, image=it.get("image"),
                              road=road, area=place, region=it.get("region"),
                              description=group_title or route, tags=_tags(text, it.get("text_slo")))
            if row:
                rows.append(row)
            else:
                skipped += 1
    number_repeats(rows)
    return rows, skipped


def number_repeats(rows: list[dict]) -> None:
    """Several views of one site often share a name: the second and later ones get ' (2)', ' (3)'."""
    seen: dict[str, int] = {}
    for row in rows:
        n = seen[row["name"]] = seen.get(row["name"], 0) + 1
        if n > 1:
            row["name"] = f"{row['name']} ({n})"


def _tags(text_en: str | None, text_sl: str | None) -> list[str]:
    """Extra tags from Slovene words the shared rules do not know (prelaz = pass, predor = tunnel)."""
    words = f"{text_en or ''} {text_sl or ''}".lower()
    tags = []
    if re.search(r"\bprelaz\b", words):
        tags.append("mountain")
    if re.search(r"\bpredor\b", words):
        tags.append("tunnel")
    if re.search(r"\b(mejni prehod|border crossing)\b", words):
        tags.append("border")
    return tags


def _fresh(fc: FetchContext, path) -> bool:
    if fc.force or not path.exists():
        return False
    max_age = getattr(fc.ds, "max_age_hours", 24.0) * 0.9
    return (time.time() - path.stat().st_mtime) / 3600.0 < max_age


def access_token(fc: FetchContext, user: str, password: str) -> str:
    body = urllib.parse.urlencode({"grant_type": "password", "username": user, "password": password}).encode()
    resp = fc.net.request(TOKEN_URL, method="POST", data=body, timeout=30, retries=1,
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        data = resp.json()
    except ValueError:
        data = None
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("NAP Slovenia did not issue an access token (check the login and dataset approval)")
    return token.strip()


def fetch(fc: FetchContext) -> list[dict]:
    user, password = credentials(fc)                 # NeedsKey before any request
    path = fc.raw_path(RAW_FILE)
    if not _fresh(fc, path):
        try:
            token = access_token(fc, user, password)
            fc.download(DATA_URL, RAW_FILE, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                        min_bytes=500, timeout=90, force=True)
        except (NetError, RuntimeError):
            if not path.exists():
                raise
            fc.notes.append("NAP Slovenia did not answer; using the last list")
    rows, skipped = parse(read_json(path), fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="sinap", name="Slovenia road cameras", owner="DARS (motorways) and DRSI (state roads), via NAP Slovenia",
    url=DATASET_URL, licence="CC BY-SA 4.0 (NAP Slovenia, free for registered users)",
    licence_url="https://creativecommons.org/licenses/by-sa/4.0/",
    attribution="DARS d.d. and DRSI via NAP Slovenia (nap.si), CC BY-SA 4.0", fetch=fetch,
    hosts=("*.dars.si", "www.drsc.si/kamere/"), country="SI", refresh=300, tags=("traffic",),
    needs_key=KEY, optional=True, coverage="Slovenian motorways and main state roads",
)
