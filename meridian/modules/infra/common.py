"""Shared helpers for the infra module: parsing, an in-memory searchable collection per dataset,
Wikidata SPARQL access and small JSON caches."""

from __future__ import annotations

import bisect
import csv
import io
import json
import math
import os
import re
import threading
import time
import unicodedata
import urllib.parse
from array import array
from pathlib import Path
from typing import Any, Iterable

from meridian import geo

WDQS = "https://query.wikidata.org/sparql"
WIKIDATA_HOST = "query.wikidata.org"


# ---------------------------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------------------------
_NON_WORD = re.compile(r"[^\w]+")


def norm(text: Any) -> str:
    """Search form: accents stripped, case folded, punctuation as single spaces."""
    s = str(text or "")
    if not s.isascii():
        s = unicodedata.normalize("NFKD", s)
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("&", " and ").replace("'", "").replace("’", "")
    return _NON_WORD.sub(" ", s).strip()


def fnum(value: Any, default: float | None = None) -> float | None:
    """Tolerant float: "1,234.5", " 12 ", "" and "nan" all handled."""
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return v if math.isfinite(v) else default
    s = str(value).strip().replace(",", "")
    if not s:
        return default
    try:
        v = float(s)
    except ValueError:
        return default
    return v if math.isfinite(v) else default


def inum(value: Any, default: int | None = None) -> int | None:
    v = fnum(value)
    return int(round(v)) if v is not None else default


def r5(x: float) -> float:
    return round(float(x), 5)


def text(value: Any, limit: int = 200) -> str | None:
    """Trimmed single-line text or None."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s[:limit] if s else None


def http_url(value: Any) -> str | None:
    """Only absolute http(s) URLs survive (data is untrusted)."""
    s = (str(value or "")).strip()
    if not s:
        return None
    if s.startswith("www."):
        s = "https://" + s
    try:
        parts = urllib.parse.urlsplit(s)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc or len(s) > 500:
        return None
    if any(c in s for c in "\"'<>\\ \t\n"):
        return None
    return s


def compact_num(v: float | None, digits: int = 1) -> float | int | None:
    """Round numbers for compact JSON: ints when whole, else ``digits`` decimals."""
    if v is None:
        return None
    if abs(v - round(v)) < 1e-9 or abs(v) >= 100:
        return int(round(v))
    return round(v, digits)


def valid_point(lat: Any, lon: Any) -> bool:
    if not geo.valid_latlon(lat, lon):
        return False
    return not (abs(float(lat)) < 1e-9 and abs(float(lon)) < 1e-9)   # (0, 0) is "unknown" in many sources


def decode_text(raw: bytes) -> str:
    """Decode a downloaded text file: UTF-8 (with or without BOM) when it is valid UTF-8, otherwise
    Windows-1252 (a superset of the ISO 8859-1 that UNECE and NGA files often use)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def read_text_file(path: Path) -> str:
    return decode_text(Path(path).read_bytes())


def ascii_json(obj: Any) -> bytes:
    """Compact JSON with non-ASCII characters escaped, so any reader decodes it the same way."""
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def read_csv_rows(path: Path, encoding: str = "utf-8-sig") -> list[dict]:
    with open(path, encoding=encoding, newline="", errors="replace") as fh:
        return list(csv.DictReader(fh))


def year_of(value: Any) -> int | None:
    """Year from "1998", "1998.0", "1998-05-01T00:00:00Z" or "+1998-..."."""
    if value is None:
        return None
    m = re.match(r"^\s*\+?(-?\d{1,4})(?:\.\d+)?(?:[-T ]|$)", str(value))
    if not m:
        return None
    y = int(m.group(1))
    return y if 1000 <= y <= 2100 else None


def wd_id(uri: Any) -> str | None:
    """"http://www.wikidata.org/entity/Q42" -> "Q42"."""
    m = re.search(r"(Q\d+)$", str(uri or ""))
    return m.group(1) if m else None


def wkt_point(value: Any) -> tuple[float, float] | None:
    """"Point(lon lat)" -> (lat, lon)."""
    m = re.match(r"^\s*Point\(\s*(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s*\)\s*$", str(value or ""))
    if not m:
        return None
    lon, lat = float(m.group(1)), float(m.group(2))
    return (lat, lon) if valid_point(lat, lon) else None


def fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def point_feature(lon: float, lat: float, props: dict) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r5(lon), r5(lat)]},
            "properties": {k: v for k, v in props.items() if v is not None}}


def log_weight(value: float | None, lo: float, hi: float) -> float:
    """0..1 importance from a value on a log scale between lo and hi."""
    if not value or value <= 0:
        return 0.0
    a, b = math.log10(lo), math.log10(hi)
    return max(0.0, min(1.0, (math.log10(value) - a) / (b - a)))


# ---------------------------------------------------------------------------------------------
# An immutable, searchable snapshot of one dataset
# ---------------------------------------------------------------------------------------------
class Collection:
    """Entities of one kind with radius search, text search and details by id.

    Rows are dicts with: id, title, subtitle, lat, lon, weight (0..1), codes (exact-match strings),
    text (extra search text), country (alpha-2) and detail (the full record, kept as JSON text).
    """

    def __init__(self, kind: str, layer: str, rows: Iterable[dict], *, cell_deg: float = 1.0,
                 zoom: int = 10, built_at: float | None = None, meta: dict | None = None):
        self.kind = kind
        self.layer = layer
        self.zoom = zoom
        self.built_at = built_at
        self.meta = meta or {}
        self.ids: list[str] = []
        self.titles: list[str] = []
        self.subtitles: list[str] = []
        self.lat = array("d")
        self.lon = array("d")
        self.weight = array("f")
        self.country: list[str | None] = []
        self.texts: list[str] = []          # normalised title + extra text
        self.title_norm: list[str] = []
        self.codes: dict[str, list[int]] = {}
        self.index: dict[str, int] = {}
        self.details: list[str] = []
        self.grid = geo.GridIndex(cell_deg)
        for row in rows:
            self._add(row)
        # One joined text for C-speed substring scans: "\x00 <title> <extra>\x00 <title>..."
        self._starts = array("l")
        parts = []
        pos = 0
        for t in self.texts:
            self._starts.append(pos + 1)
            parts.append("\x00" + t)
            pos += 1 + len(t)
        self._blob = "".join(parts)

    def _add(self, row: dict) -> None:
        rid = str(row["id"])
        if rid in self.index:
            return
        i = len(self.ids)
        self.index[rid] = i
        self.ids.append(rid)
        title = str(row.get("title") or rid)
        self.titles.append(title)
        self.subtitles.append(str(row.get("subtitle") or ""))
        lat, lon = float(row["lat"]), float(row["lon"])
        self.lat.append(lat)
        self.lon.append(lon)
        self.weight.append(float(row.get("weight") or 0.0))
        self.country.append(row.get("country"))
        tn = norm(title)
        self.title_norm.append(tn)
        extra = norm(row.get("text") or "")
        self.texts.append(" " + f"{tn} {extra}".strip())   # leading space: word-prefix tests need no copy
        for code in row.get("codes") or ():
            c = norm(code).replace(" ", "")
            if c:
                self.codes.setdefault(c, []).append(i)
        detail = row.get("detail")
        self.details.append(json.dumps(detail, ensure_ascii=False, separators=(",", ":")) if detail is not None else "")
        self.grid.add(lat, lon, i)

    def __len__(self) -> int:
        return len(self.ids)

    def has(self, rid: str) -> bool:
        return rid in self.index

    def detail(self, rid: str) -> dict | None:
        i = self.index.get(rid)
        if i is None:
            return None
        raw = self.details[i]
        return json.loads(raw) if raw else {}

    def entity(self, i: int, **extra) -> dict:
        e = {"kind": self.kind, "id": self.ids[i], "title": self.titles[i], "subtitle": self.subtitles[i],
             "lat": r5(self.lat[i]), "lon": r5(self.lon[i]), "layer": self.layer, "zoom": self.zoom}
        e.update(extra)
        return e

    def entity_for(self, rid: str, **extra) -> dict | None:
        i = self.index.get(rid)
        return self.entity(i, **extra) if i is not None else None

    def near(self, lat: float, lon: float, radius_km: float, limit: int = 20) -> list[dict]:
        out = []
        for d, i in self.grid.near(lat, lon, radius_km, limit):
            out.append(self.entity(i, distance_km=round(d, 2)))
        return out

    def near_indices(self, lat: float, lon: float, radius_km: float, limit: int | None = None):
        return self.grid.near(lat, lon, radius_km, limit)

    def search(self, q: str, limit: int = 20, max_hits: int = 4000) -> list[dict]:
        """Exact codes, then title prefixes, word prefixes and (4+ characters) substrings."""
        qn = norm(q)
        if not qn:
            return []
        scored: dict[int, float] = {}
        code = qn.replace(" ", "")
        for i in self.codes.get(code, ()):
            scored[i] = 0.97 + 0.02 * self.weight[i]
        # Short queries only match the start of a title; longer ones match anywhere.
        needle = ("\x00 " + qn) if len(qn) < 3 else qn
        blob, starts = self._blob, self._starts
        pos = blob.find(needle)
        hits = 0
        while pos >= 0 and hits < max_hits:
            hits += 1
            i = bisect.bisect_right(starts, pos) - 1
            if i >= 0 and i not in scored:
                tn = self.title_norm[i]
                w = self.weight[i]
                if tn == qn:
                    scored[i] = 0.80 + 0.18 * w
                elif tn.startswith(qn):
                    scored[i] = 0.62 + 0.30 * w
                elif (" " + qn) in self.texts[i]:
                    scored[i] = 0.55 + 0.30 * w
                elif len(qn) >= 4:
                    scored[i] = 0.40 + 0.20 * w
            pos = blob.find(needle, pos + 1)
        best = sorted(scored.items(), key=lambda kv: -kv[1])[:limit]
        return [self.entity(i, score=round(s, 3)) for i, s in best]

    def by_country(self, iso2: str) -> list[int]:
        return [i for i, c in enumerate(self.country) if c == iso2]


EMPTY = Collection("none", "", [])


class Holder:
    """Thread-safe swap of immutable snapshots: ``holder.get('power')`` never blocks a reader."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, Any] = {}
        self.version = 0

    def get(self, name: str, default: Any = None) -> Any:
        return self._items.get(name, default)

    def set(self, name: str, value: Any) -> None:
        with self._lock:
            items = dict(self._items)
            items[name] = value
            self._items = items
            self.version += 1


# ---------------------------------------------------------------------------------------------
# Wikidata SPARQL (CSV results, POST so large VALUES lists fit)
# ---------------------------------------------------------------------------------------------
def sparql_csv(net, query: str, *, timeout: float = 75.0, retries: int = 1) -> list[dict]:
    """Run a SPARQL query on the Wikidata Query Service and return CSV rows as dicts."""
    body = urllib.parse.urlencode({"query": query}).encode("utf-8")
    resp = net.request(WDQS, method="POST", data=body, timeout=timeout, retries=retries,
                       headers={"Accept": "text/csv", "Content-Type": "application/x-www-form-urlencoded"})
    text_ = resp.body.decode("utf-8", errors="replace")
    if text_.lstrip().startswith("<"):
        raise ValueError("Wikidata answered with HTML instead of CSV")
    return list(csv.DictReader(io.StringIO(text_)))


def values_clause(var: str, qids: Iterable[str]) -> str:
    return "VALUES ?%s { %s }" % (var, " ".join("wd:" + q for q in qids if re.fullmatch(r"Q\d+", q)))


def batched(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def sparql_batches(net, ids: list[str], make_query, size: int, *, log=None, label: str = "query",
                   min_size: int = 100) -> tuple[list[dict], int]:
    """Run ``make_query(chunk)`` for batches of ids. A batch that times out is split in half and
    retried (down to ``min_size``); a rate-limited batch waits and is tried once more.
    Returns (rows, number of ids whose batch finally failed)."""
    rows: list[dict] = []
    failed = 0
    queue: list[tuple[list[str], int]] = [(list(c), 0) for c in batched(ids, size)]
    while queue:
        chunk, tries = queue.pop(0)
        try:
            rows.extend(sparql_csv(net, make_query(chunk), retries=0))
        except Exception as err:
            status = getattr(err, "status", None)
            if status == 429 and tries == 0:
                time.sleep(15)
                queue.insert(0, (chunk, 1))
            elif len(chunk) > min_size:
                half = len(chunk) // 2
                queue[0:0] = [(chunk[:half], tries), (chunk[half:], tries)]
            else:
                failed += len(chunk)
                if log is not None:
                    log.warning("%s: a batch of %d items failed: %s", label, len(chunk), err)
    return rows, failed


# ---------------------------------------------------------------------------------------------
# Small JSON file caches (raw downloads stay in data/raw/infra, never executed)
# ---------------------------------------------------------------------------------------------
def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_age_hours(path: Path) -> float | None:
    try:
        return (time.time() - Path(path).stat().st_mtime) / 3600.0
    except OSError:
        return None
