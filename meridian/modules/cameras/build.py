"""The ``cameras`` dataset build: run every adapter on its own (in parallel), keep a failed source's
previous rows, drop rows whose image host is not on the source's allowlist, de-duplicate, then write

- ``cameras.json``: every camera with all its fields plus per-source status (the server's copy);
- ``cameras.geojson``: lean points for the map (see :mod:`.mapfile`: id, kind, type, filter tags,
  heading; names come from the API).

``build(..., only={ids})`` re-runs just those sources and keeps every other source's rows and
status from the previous ``cameras.json`` (no network): used when the weekly Windy crawl finishes.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import FetchContext, NeedsKey, Pending, Source, clean
from .mapfile import FAN_M, fan_out, group_label, map_collection, to_feature  # noqa: F401 (re-exported)
from .proxy import host_allowed

VERSION = 1
WORKERS = 6
SOURCE_BUDGET_S = 300.0


def load_previous(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sources": {}, "by_src": {}}
    by_src: dict[str, list[dict]] = {}
    for row in data.get("cameras") or []:
        if isinstance(row, dict) and row.get("src"):
            by_src.setdefault(row["src"], []).append(row)
    return {"sources": data.get("sources") or {}, "by_src": by_src, "built_at": data.get("built_at")}


def disabled_sources(config, sources: list[Source] | None = None) -> set[str]:
    """Ids turned off by CAMERAS_DISABLE, plus everything outside CAMERAS_ONLY when that is set."""
    raw = (config.get("CAMERAS_DISABLE") if config is not None else None) or ""
    off = {s.strip().lower() for s in raw.split(",") if s.strip()}
    only_raw = (config.get("CAMERAS_ONLY") if config is not None else None) or ""
    only = {s.strip().lower() for s in only_raw.split(",") if s.strip()}
    if only and sources:
        off |= {s.id for s in sources if s.id not in only}
    return off


def source_meta(src: Source) -> dict:
    return {"id": src.id, "name": src.name, "owner": src.owner, "url": src.url, "licence": src.licence,
            "licence_url": src.licence_url, "attribution": src.attribution, "coverage": src.coverage,
            "needs_key": src.needs_key, "optional": src.optional, "refresh": src.refresh,
            "country": src.country or None, "playable": src.stream_cors}


def screen_rows(rows: list[dict], src: Source) -> tuple[list[dict], int]:
    """Rows whose image (if any) is on the source's allowlist; others lose the image or are dropped."""
    out, dropped = [], 0
    for row in rows:
        if not isinstance(row, dict) or row.get("src") != src.id:
            dropped += 1
            continue
        if row.get("image") and not host_allowed(row["image"], src):
            row = dict(row)
            row.pop("image", None)
            if row.get("kind") == "image":
                row["kind"] = "page" if row.get("page") else "image"
            if not (row.get("stream") or row.get("page") or row.get("dynamic")):
                dropped += 1
                continue
        out.append(row)
    return out, dropped


def run_source(ds, src: Source, config, log, previous: dict) -> tuple[list[dict], dict]:
    prev_rows = previous["by_src"].get(src.id) or []
    prev_meta = previous["sources"].get(src.id) or {}
    meta = source_meta(src)
    started = time.time()
    fc = FetchContext(ds, src, config=config, budget_s=SOURCE_BUDGET_S)
    try:
        if src.fetch is None:
            raise RuntimeError("no adapter")
        rows = src.fetch(fc)
        rows, dropped = screen_rows(rows or [], src)
        if dropped:
            fc.notes.append(f"{dropped} rows dropped (unexpected image host or invalid)")
        if not rows and prev_rows and src.id != "autobahn":
            raise RuntimeError("returned no cameras")
        meta.update(ok=True, count=len(rows), skipped=fc.skipped, stale=False, error=None,
                    fetched_at=time.time(), last_ok_at=time.time(), duration_s=round(time.time() - started, 1))
        note = [f"{len(rows):,} cameras"]
        if fc.skipped:
            note.append(f"{fc.skipped:,} offline or unusable skipped")
        note.extend(fc.notes)
        meta["note"] = "; ".join(note)
        _merge_extra(meta, fc.extra)
        return rows, meta
    except NeedsKey as err:
        meta.update(ok=None, count=0, stale=False, error=None, note=f"Needs {err.key_name}", fetched_at=time.time())
        return [], meta
    except Pending as err:      # not ready yet (a first crawl still running): waiting, not failed
        meta.update(ok=None, count=len(prev_rows), stale=bool(prev_rows), error=None, fetched_at=time.time(),
                    last_ok_at=prev_meta.get("last_ok_at"), note=clean(str(err), 200) or "waiting for its first list")
        _merge_extra(meta, {k: v for k, v in prev_meta.items() if k not in meta})
        _merge_extra(meta, fc.extra)
        return list(prev_rows), meta
    except Exception as err:  # this source failed: keep what it gave us last time
        message = short_error(err)
        if config is not None:
            message = config.redact(message)
        log.warning("camera source %s failed: %s", src.id, message)
        meta.update(ok=False, count=len(prev_rows), stale=bool(prev_rows), error=message, fetched_at=time.time(),
                    last_ok_at=prev_meta.get("last_ok_at"), duration_s=round(time.time() - started, 1))
        if prev_rows:
            meta["note"] = f"kept {len(prev_rows):,} cameras from the last good list"
        elif src.optional:
            meta["note"] = "optional source, not reachable from this network right now"
        return list(prev_rows), meta


def _merge_extra(meta: dict, extra: dict) -> None:
    """Adapter facts (``fc.extra``) join the status, without overwriting the core fields."""
    for key, value in (extra or {}).items():
        if isinstance(key, str) and key not in ("id", "ok", "count", "error", "note", "stale"):
            meta.setdefault(key, value)


def short_error(err: Exception) -> str:
    """'NetError: host: [WinError 10060] A connection attempt failed...' -> 'host: timed out'."""
    text = str(err)
    host = text.split(":", 1)[0] if ":" in text else ""
    low = text.lower()
    if "10060" in low or "timed out" in low or "timeout" in low:
        reason = "timed out"
    elif "getaddrinfo" in low or "name or service" in low or "nodename" in low or "11001" in low:
        reason = "host name not found"
    elif "10061" in low or "refused" in low:
        reason = "connection refused"
    elif "certificate" in low or "ssl" in low:
        reason = "TLS error"
    else:
        return clean(f"{type(err).__name__}: {text}", 300)
    return f"{host}: {reason}" if host and " " not in host else reason


YIELD_M = 100.0     # an aggregator's camera this close to another source's camera is the same camera


def yield_to_official(rows: list[dict], aggregators: set[str]) -> tuple[list[dict], dict[str, int]]:
    """Drop rows of aggregator sources (Windy re-lists road-agency and other feeds) that sit within
    ``YIELD_M`` of a camera from any other source: the official feed has the better picture (often
    video) and the details. Returns the kept rows and how many each aggregator lost."""
    if not aggregators:
        return rows, {}
    import math

    from meridian import geo
    cell = 0.002                                     # about 220 m north-south
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for r in rows:
        if r["src"] not in aggregators:
            grid.setdefault((math.floor(r["lat"] / cell), math.floor(r["lon"] / cell)), []).append((r["lat"], r["lon"]))
    kept, lost = [], {}
    for r in rows:
        if r["src"] in aggregators:
            cy, cx = math.floor(r["lat"] / cell), math.floor(r["lon"] / cell)
            near = any(geo.haversine_km(r["lat"], r["lon"], la, lo) * 1000.0 <= YIELD_M
                       for dy in (-1, 0, 1) for dx in (-1, 0, 1) for la, lo in grid.get((cy + dy, cx + dx), ()))
            if near:
                lost[r["src"]] = lost.get(r["src"], 0) + 1
                continue
        kept.append(r)
    return kept, lost


# query parameters that only defeat caches; two links differing only in these show the same picture
CACHE_BUSTERS = frozenset({"_", "t", "ts", "time", "timestamp", "rand", "random", "r", "nocache", "cachebust",
                           "cachebuster", "cb", "dummy", "nc", "_t", "_ts", "rnd", "unique"})


def image_key(url: str | None) -> str:
    """A picture link for de-duplication: scheme and host lower case, the path as it is (paths are
    case-sensitive), the query kept but sorted and without cache busters (``?station=41004`` names
    a different picture; ``?t=1795...`` does not), no fragment."""
    if not url:
        return ""
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    query = sorted((k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                   if k.lower() not in CACHE_BUSTERS)
    return urllib.parse.urlunsplit((parts.scheme.lower(), (parts.netloc or "").lower(), parts.path,
                                    urllib.parse.urlencode(query), ""))


def dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    seen_ids: set[str] = set()
    seen_images: set[str] = set()
    out = []
    dropped = 0
    for row in rows:
        img = image_key(row.get("image"))
        if row["id"] in seen_ids or (img and img in seen_images):
            dropped += 1
            continue
        seen_ids.add(row["id"])
        if img:
            seen_images.add(img)
        out.append(row)
    return out, dropped


def build(ds, sources: list[Source], config, log, only: set[str] | None = None) -> dict:
    """Run the adapters and write the camera files. With ``only``, just those sources run; every
    other source keeps its rows and status from the previous ``cameras.json``."""
    previous = load_previous(ds.out_dir / "cameras.json")
    off = disabled_sources(config, sources)
    active = [s for s in sources if s.id not in off and (not only or s.id in only)]
    results: dict[str, tuple[list[dict], dict]] = {}
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="cameras-build") as pool:
        futures = {s.id: pool.submit(run_source, ds, s, config, log, previous) for s in active}
        for sid, fut in futures.items():
            results[sid] = fut.result()
    rows: list[dict] = []
    statuses: dict[str, dict] = {}
    for s in sources:
        if s.id in off:
            statuses[s.id] = {**source_meta(s), "ok": None, "count": 0, "note": "turned off (CAMERAS_DISABLE or CAMERAS_ONLY)"}
            continue
        if s.id not in results:          # not re-run this time (``only``): keep the previous list as it was
            got = list(previous["by_src"].get(s.id) or [])
            meta = dict(previous["sources"].get(s.id) or {**source_meta(s), "ok": None, "count": 0})
        else:
            got, meta = results[s.id]
        rows.extend(got)
        statuses[s.id] = meta
        if hasattr(ds, "source") and s.url:
            ds.source(s.url)
    rows, dupes = dedupe(rows)
    rows, yielded = yield_to_official(rows, {s.id for s in sources if getattr(s, "aggregates", False)})
    for sid, n in yielded.items():
        meta = statuses.get(sid)
        if meta is not None:
            meta["yielded"] = n
            meta["note"] = (meta.get("note") or "") + f"; {n:,} left out as already in an official feed"
    if not rows:
        raise RuntimeError("no cameras from any source")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["src"]] = counts.get(r["src"], 0) + 1
    for sid, meta in statuses.items():
        if meta.get("count"):
            meta["count"] = counts.get(sid, 0)
    rows.sort(key=lambda r: r["id"])
    now = time.time()
    doc = {"version": VERSION, "built_at": now, "count": len(rows), "sources": statuses, "cameras": rows}
    # the map file first: whoever sees the new cameras.json can already fetch the matching map
    ds.write_json("cameras.geojson", map_collection(rows, now))
    ds.write_json("cameras.json", doc)
    live = [sid for sid, m in statuses.items() if m.get("ok")]
    failed = [sid for sid, m in statuses.items() if m.get("ok") is False]
    ds.set_rows(len(rows))
    ds.note(f"{len(rows):,} cameras from {len(live)} live sources"
            + (f", {dupes} duplicates removed" if dupes else ""))
    if failed:
        ds.note("failed: " + ", ".join(f"{sid} ({statuses[sid].get('error') or 'error'})" for sid in failed)[:280])
    return doc
