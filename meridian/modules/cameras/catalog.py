"""The in-memory camera catalog: lookup by id, a grid index for nearby, bbox and label queries and a
small token index for search. Built once per dataset load and swapped in whole (immutable snapshot).

Sized for 100,000+ cameras: repeated strings are interned, positions live in flat arrays, bbox
queries rank with a cheap planar distance before the exact one, and search scores only the
strongest candidates when a short query matches tens of thousands of cameras."""

from __future__ import annotations

import bisect
import heapq
import math
import re
import sys
from array import array
from typing import Iterable

from meridian import geo

from .common import CAMERA_TYPES, COUNTRY_NAMES, LIVE_KINDS, compass_label, fold
from .mapfile import FILTER_TAGS, TAG_CODES, fan_out, tag_code  # noqa: F401 (tag_code re-exported)

_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = {"the", "at", "and", "of", "on", "to", "in", "near", "camera", "cameras", "cam", "webcam", "webcams",
             "view", "facing", "towards"}
CELL_DEG = 0.5
SEARCH_SCORE_MAX = 4000        # candidates scored in full; beyond this, exact-word matches are kept first
_INTERN_KEYS = ("src", "kind", "country", "region", "area", "road", "attribution", "licence", "type", "direction")

# the map's "Show" choices, also counted for the layer row (keep in step with maplayer.js)
SHOW_TAGS = {"weather": ("weather", "mountain", "wildfire", "volcano", "polar"), "urban": ("urban",),
             "coastal": ("coastal", "port")}


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(fold(text or ""))


def is_live(row: dict) -> bool:
    return row.get("kind") in LIVE_KINDS


def camera_type(row: dict) -> str:
    """"traffic" or "webcam" (rows from before camera types existed: by their tags)."""
    t = row.get("type")
    if t in CAMERA_TYPES:
        return t
    return "traffic" if "traffic" in (row.get("tags") or ()) else "webcam"


def subtitle(row: dict, src_name: str | None = None) -> str:
    """'A40, Ealing, London, United Kingdom' (parts already in the name are left out)."""
    name = (row.get("name") or "").lower()
    parts = []
    for key in ("road", "area", "region"):
        value = row.get(key)
        if not value or value in parts or (key != "region" and value.lower() in name):
            continue
        parts.append(value)
    country = COUNTRY_NAMES.get(row.get("country") or "", row.get("country") or "")
    if country and country not in parts:
        parts.append(country)
    return ", ".join(parts)[:120]


def entity(row: dict, *, score: float | None = None, distance_km: float | None = None) -> dict:
    sub = subtitle(row)
    if is_live(row):
        sub = f"{sub}, live video" if row.get("kind") == "hls" else f"{sub}, video"
    e = {"kind": "camera", "id": row["id"], "title": row["name"], "subtitle": sub.strip(", "),
         "lat": row["lat"], "lon": row["lon"], "layer": "cameras", "zoom": 15}
    if score is not None:
        e["score"] = round(max(0.0, min(1.0, score)), 3)
    if distance_km is not None:
        e["distance_km"] = round(distance_km, 2)
    return e


def brief(row: dict) -> dict:
    """Compact camera summary for lists, grids and the AI. Direct cameras (Windy) have no
    ``img_url``: the browser gets their picture from ``/api/cameras/direct``."""
    direct = bool(row.get("direct"))
    has_still = bool(row.get("image") or (row.get("dynamic") and not direct))   # stream-only cameras have none
    out = {"id": row["id"], "name": row["name"], "subtitle": subtitle(row), "lat": row["lat"], "lon": row["lon"],
           "kind": row["kind"], "live": is_live(row), "refresh": row.get("refresh"), "src": row["src"],
           "tags": list(row.get("tags") or []), "type": camera_type(row),
           "img_url": f"/api/cameras/img/{row['id']}" if has_still else None}
    if direct:
        out["direct"] = True
        if row.get("page"):
            out["page"] = row["page"]
    if row.get("heading") is not None:
        out["heading"] = row["heading"]
        out["compass"] = compass_label(row["heading"])
    return out


def show_test(show: str | None):
    """A row predicate for a map "Show" choice (None for everything)."""
    if show in (None, "", "all"):
        return None
    if show in CAMERA_TYPES:
        return lambda r: camera_type(r) == show
    if show == "live":
        return is_live
    if show == "still":
        return lambda r: not is_live(r) and r.get("kind") != "page"
    tags = SHOW_TAGS.get(show)
    if tags:
        return lambda r: any(t in tags for t in (r.get("tags") or ()))
    raise ValueError(f"unknown show choice {show!r}")


SHOW_CHOICES = ("all", "traffic", "webcam", "live", "still", "weather", "urban", "coastal")


class Catalog:
    def __init__(self, rows: Iterable[dict], sources: dict | None = None, built_at: float | None = None):
        self.rows: list[dict] = []
        self.by_id: dict[str, dict] = {}
        pool: dict[str, str] = {}
        for r in rows:
            if not isinstance(r, dict) or not r.get("id") or r["id"] in self.by_id:
                continue
            for k in _INTERN_KEYS:                 # the same few strings repeat across 100,000 rows
                v = r.get(k)
                if isinstance(v, str):
                    r[k] = pool.setdefault(v, sys.intern(v) if len(v) < 40 else v)
            tags = r.get("tags")
            if isinstance(tags, list):
                r["tags"] = tuple(sys.intern(t) for t in tags if isinstance(t, str))
            self.rows.append(r)
            self.by_id[r["id"]] = r
        self.sources = sources or {}
        self.built_at = built_at
        n = len(self.rows)
        self.lat = array("d", (r["lat"] for r in self.rows))
        self.lon = array("d", (r["lon"] for r in self.rows))
        cells: dict[tuple[int, int], list[int]] = {}
        for i in range(n):
            cells.setdefault((math.floor(self.lat[i] / CELL_DEG), math.floor(self.lon[i] / CELL_DEG)), []).append(i)
        self.cells = {k: array("I", v) for k, v in cells.items()}
        # search: token -> row numbers; name tokens kept per row for scoring
        postings: dict[str, list[int]] = {}
        self.name_tokens: list[tuple[str, ...]] = []
        src_tokens: dict[str, list[str]] = {}
        for i, r in enumerate(self.rows):
            name_toks = tokens(r.get("name", ""))
            self.name_tokens.append(tuple(sorted(set(name_toks))))
            sid = r["src"]
            if sid not in src_tokens:
                src_tokens[sid] = tokens((self.sources.get(sid) or {}).get("name", ""))
            text = " ".join(str(r.get(k) or "") for k in ("road", "area", "region", "description", "direction"))
            text += " " + COUNTRY_NAMES.get(r.get("country") or "", "") + " " + " ".join(r.get("tags") or ())
            for t in set(name_toks) | set(tokens(text)) | set(src_tokens[sid]):
                postings.setdefault(t, []).append(i)
        self.postings: dict[str, array] = {t: array("I", v) for t, v in postings.items()}
        self.vocab = sorted(self.postings)
        # display positions (co-located views fanned out) and the one name drawn per mast
        spots, labels = fan_out(self.rows)
        self.map_pos = spots
        self.map_label = labels
        self._counts: dict | None = None

    # -- basics -----------------------------------------------------------------------------------
    @property
    def count(self) -> int:
        return len(self.rows)

    def get(self, cam_id: str) -> dict | None:
        return self.by_id.get(cam_id)

    def counts(self) -> dict:
        if self._counts is None:
            self._counts = self._count()
        return self._counts

    def _count(self) -> dict:
        by_kind: dict[str, int] = {}
        by_tag: dict[str, int] = {}
        by_country: dict[str, int] = {}
        by_src: dict[str, int] = {}
        by_type: dict[str, int] = {}
        show = {c: 0 for c in SHOW_CHOICES}
        for r in self.rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
            by_src[r["src"]] = by_src.get(r["src"], 0) + 1
            c = r.get("country") or "?"
            by_country[c] = by_country.get(c, 0) + 1
            tags = r.get("tags") or ()
            for t in tags:
                by_tag[t] = by_tag.get(t, 0) + 1
            ct = camera_type(r)
            by_type[ct] = by_type.get(ct, 0) + 1
            show[ct] += 1
            if is_live(r):
                show["live"] += 1
            elif r["kind"] != "page":
                show["still"] += 1
            for choice, want in SHOW_TAGS.items():
                if any(t in want for t in tags):
                    show[choice] += 1
        show["all"] = len(self.rows)
        return {"by_kind": by_kind, "by_tag": by_tag, "by_country": by_country, "by_src": by_src,
                "by_type": by_type, "show": show}

    # -- geography --------------------------------------------------------------------------------
    def _cells_in(self, bbox: tuple[float, float, float, float]) -> Iterable[tuple[array, bool]]:
        """(row numbers, wholly inside) for every grid cell that touches the bbox."""
        w, s, e, n = bbox
        r0, r1 = math.floor(s / CELL_DEG), math.floor(n / CELL_DEG)
        spans = [(w, e)] if w <= e else [(w, 180.0), (-180.0, e)]
        for sw, se in spans:
            c0, c1 = math.floor(sw / CELL_DEG), math.floor(se / CELL_DEG)
            if (r1 - r0 + 1) * (c1 - c0 + 1) > len(self.cells):
                # a big box: walking the occupied cells is cheaper than walking the box
                keys = [k for k in self.cells if r0 <= k[0] <= r1 and c0 <= k[1] <= c1]
            else:
                keys = [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1) if (r, c) in self.cells]
            for r, c in keys:
                inside = (r * CELL_DEG >= s and (r + 1) * CELL_DEG <= n and c * CELL_DEG >= sw
                          and (c + 1) * CELL_DEG <= se)
                yield self.cells[(r, c)], inside

    def indexes_in_bbox(self, bbox: tuple[float, float, float, float]) -> list[int]:
        out: list[int] = []
        lat, lon = self.lat, self.lon
        for idx, inside in self._cells_in(bbox):
            if inside:
                out.extend(idx)
            else:
                out.extend(i for i in idx if geo.in_bbox(lat[i], lon[i], bbox))
        return out

    def near(self, lat: float, lon: float, radius_km: float, limit: int = 20,
             keep=None) -> list[tuple[float, dict]]:
        dlat = math.degrees(radius_km / geo.EARTH_RADIUS_KM)
        coslat = max(0.01, math.cos(math.radians(lat)))
        dlon = min(180.0, dlat / coslat)
        s, n = max(-90.0, lat - dlat), min(90.0, lat + dlat)
        if dlon >= 180.0:
            box = (-180.0, s, 180.0, n)
        else:
            w, e = geo.wrap_lon(lon - dlon), geo.wrap_lon(lon + dlon)
            box = (w, s, e, n)
        found: list[tuple[float, int]] = []
        for i in self.indexes_in_bbox(box):
            d = geo.haversine_km(lat, lon, self.lat[i], self.lon[i])
            if d <= radius_km:
                found.append((d, i))
        found.sort()
        out = []
        for d, i in found:
            row = self.rows[i]
            if keep is None or keep(row):
                out.append((d, row))
                if len(out) >= limit:
                    break
        return out

    def in_bbox(self, bbox: tuple[float, float, float, float], *, center: tuple[float, float] | None = None,
                limit: int = 100, keep=None) -> tuple[int, list[dict]]:
        idx = self.indexes_in_bbox(bbox)
        if keep is not None:
            idx = [i for i in idx if keep(self.rows[i])]
        total = len(idx)
        if center is not None and idx:
            clat, clon = center
            if total > limit * 4:
                # rank with a planar distance first (cheap), then exactly for the few kept
                k = math.cos(math.radians(clat))
                lat, lon = self.lat, self.lon

                def planar(i: int) -> float:
                    dx = (lon[i] - clon + 540.0) % 360.0 - 180.0
                    return (dx * k) ** 2 + (lat[i] - clat) ** 2

                idx = heapq.nsmallest(limit * 2, idx, key=planar)
            idx.sort(key=lambda i: geo.haversine_km(clat, clon, self.lat[i], self.lon[i]))
        return total, [self.rows[i] for i in idx[:limit]]

    def map_position(self, row: dict) -> tuple[float, float]:
        return self.map_pos.get(row["id"]) or (row["lat"], row["lon"])

    # -- search -----------------------------------------------------------------------------------
    def _prefix(self, tok: str, cap: int = 400) -> set[int]:
        hits: set[int] = set()
        i = bisect.bisect_left(self.vocab, tok)
        n = 0
        while i < len(self.vocab) and self.vocab[i].startswith(tok) and n < cap:
            hits.update(self.postings[self.vocab[i]])
            i += 1
            n += 1
        return hits

    def search(self, q: str, limit: int = 20, keep=None) -> list[tuple[float, dict]]:
        qt = [t for t in tokens(q) if len(t) >= 2 or t.isdigit()]
        meaningful = [t for t in qt if t not in STOPWORDS] or qt
        if not meaningful:
            return []
        sets = sorted((self._prefix(t) for t in meaningful), key=len)
        cand = set(sets[0])
        for s in sets[1:]:
            cand &= s
            if not cand:
                break
        partial = False
        if not cand and len(sets) >= 2:
            # nothing has every word: fall back to rows with all but one, ranked lower
            hits: dict[int, int] = {}
            for s in sets:
                for i in s:
                    hits[i] = hits.get(i, 0) + 1
            cand = {i for i, n in hits.items() if n >= len(sets) - 1}
            partial = True
        if not cand:
            return []
        if len(cand) > SEARCH_SCORE_MAX:
            # a short query matching tens of thousands: whole-word matches in the name rank highest,
            # so score those first, topped up with the rest in row order
            exact = [i for i in cand if all(t in self.name_tokens[i] for t in meaningful)]
            if len(exact) < SEARCH_SCORE_MAX:
                rest = sorted(cand.difference(exact))[:SEARCH_SCORE_MAX - len(exact)]
                exact.extend(rest)
            cand = exact[:SEARCH_SCORE_MAX]
        qfold = " ".join(tokens(q))
        scored = []
        for i in cand:
            row = self.rows[i]
            if keep is not None and not keep(row):
                continue
            name_t = self.name_tokens[i]
            exact = sum(1 for t in meaningful if t in name_t)
            prefix = sum(1 for t in meaningful if t not in name_t and any(n.startswith(t) for n in name_t))
            name_fold = " ".join(tokens(row["name"]))
            score = 0.42 + (0.28 * exact + 0.16 * prefix) / len(meaningful)
            if name_fold.startswith(qfold):
                score += 0.12
            if name_fold == qfold:
                score += 0.1
            if partial:
                score -= 0.15
            scored.append((min(score, 0.92), row))
        scored.sort(key=lambda t: (-t[0], len(t[1]["name"]), t[1]["id"]))
        return scored[:limit]
