"""Small helpers for the assistant: safe JSON, fitting tool results into a size budget, UTC times,
entity shapes and the request validation for ``POST /api/ai/chat``."""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from meridian import geo
from meridian.registry import HTTPError

KIND_RE = re.compile(r"[a-z][a-z0-9_]{1,39}")
LAYER_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
TOOL_NAME_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")

MAX_MESSAGES = 60          # accepted from the browser
KEEP_MESSAGES = 24         # sent to the model (most recent)
KEEP_CHARS = 60_000        # history budget sent to the model
USER_MAX_CHARS = 12_000
ASSISTANT_MAX_CHARS = 24_000


# ---------------------------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------------------------
def _clean(obj: Any, depth: int = 0) -> Any:
    if depth > 40:
        return None
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_clean(v, depth + 1) for v in obj]
    return obj


def dumps(obj: Any) -> str:
    """Compact JSON that never fails on NaN, sets, Paths or other odd values."""
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)
    except (ValueError, RecursionError):
        return json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)


def _shrink(obj: Any, max_items: int, max_str: int, depth: int = 0) -> Any:
    if depth > 10:
        return "..."
    if isinstance(obj, dict):
        items = list(obj.items())
        out = {str(k): _shrink(v, max_items, max_str, depth + 1) for k, v in items[:80]}
        if len(items) > 80:
            out["_more_keys"] = len(items) - 80
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        seq = list(obj)
        out = [_shrink(v, max_items, max_str, depth + 1) for v in seq[:max_items]]
        if len(seq) > max_items:
            out.append(f"... {len(seq) - max_items} more items not shown")
        return out
    if isinstance(obj, str) and len(obj) > max_str:
        return obj[:max_str] + "..."
    return obj


_SHRINK_STEPS = ((60, 600), (30, 400), (15, 250), (8, 160), (4, 120), (2, 80), (1, 60))


def fit_json(obj: Any, limit: int = 12_000) -> tuple[str, bool]:
    """Serialise ``obj`` into at most ``limit`` characters. Returns (text, truncated).

    Oversized results are shrunk structurally (long lists cut, long strings shortened) so the
    model still gets valid JSON, wrapped with a note saying what was dropped. Only when even that
    fails is the text cut hard, with a note at the end."""
    text = dumps(obj)
    if len(text) <= limit:
        return text, False
    full = len(text)
    for max_items, max_str in _SHRINK_STEPS:
        shrunk = _shrink(obj, max_items, max_str)
        note = (f"Result shortened to fit: lists cut to {max_items} items and long text trimmed "
                f"(the full result was {full:,} characters). Ask a narrower question for more.")
        candidate = dumps({"truncated": note, "result": shrunk})
        if len(candidate) <= limit:
            return candidate, True
    tail = f" ... [cut: the full result was {full:,} characters; ask a narrower question]"
    return text[: max(0, limit - len(tail))] + tail, True


def preview(obj: Any, limit: int = 300) -> Any:
    """A small copy of tool arguments for the trace shown to the user."""
    text = dumps(obj)
    if len(text) <= limit:
        return obj
    return {"_preview": text[: limit - 3] + "..."}


# ---------------------------------------------------------------------------------------------
# Time and numbers
# ---------------------------------------------------------------------------------------------
def iso_utc(t: Any) -> str | None:
    """Epoch seconds or milliseconds (or an ISO string) -> ``2026-11-22T09:14:05Z``."""
    if t is None or isinstance(t, bool):
        return None
    if isinstance(t, (int, float)):
        if not math.isfinite(t) or t <= 0:
            return None
        if t > 1e11:
            t = t / 1000.0
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(t, str) and t.strip():
        return t.strip()[:40]
    return None


def as_float(value: Any, default: float | None = None, lo: float | None = None,
             hi: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    if lo is not None and f < lo:
        return default
    if hi is not None and f > hi:
        return default
    return f


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def as_int(value: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, n))


def str_list(value: Any, limit: int = 40, max_len: int = 80) -> list[str]:
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\s]+", value) if v]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for v in value[:limit]:
        if isinstance(v, str) and v.strip():
            out.append(v.strip()[:max_len])
    return out


def parse_bbox(value: Any) -> tuple[float, float, float, float] | None:
    """[w, s, e, n] (list, dict or "w,s,e,n") -> validated tuple, or None."""
    if isinstance(value, dict):
        value = [value.get("west"), value.get("south"), value.get("east"), value.get("north")]
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    nums = [as_float(v) for v in value]
    if any(v is None for v in nums):
        return None
    try:
        return geo.parse_bbox(",".join(str(v) for v in nums))
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------------------------
def compact_entity(e: Any) -> dict | None:
    """The canonical entity fields (CONTRACT 5.6), validated and trimmed. None if not an entity."""
    if not isinstance(e, dict):
        return None
    kind = e.get("kind")
    eid = e.get("id")
    if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
        return None
    if isinstance(eid, bool) or not isinstance(eid, (str, int)) or str(eid).strip() == "":
        return None
    out: dict[str, Any] = {"kind": kind, "id": str(eid).strip()[:200]}
    for key, size in (("title", 160), ("subtitle", 200)):
        v = e.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()[:size]
    if geo.valid_latlon(e.get("lat"), e.get("lon")):
        out["lat"] = round(float(e["lat"]), 5)
        out["lon"] = round(float(e["lon"]), 5)
    layer = e.get("layer")
    if isinstance(layer, str) and LAYER_ID_RE.fullmatch(layer):
        out["layer"] = layer
    module = e.get("module")
    if isinstance(module, str) and KIND_RE.fullmatch(module):
        out["module"] = module
    when = iso_utc(e.get("time"))
    if when:
        out["time"] = when
    for key, lo, hi in (("severity", 0, 10), ("distance_km", 0, 40_100), ("zoom", 0, 22)):
        v = as_float(e.get(key), None, lo, hi)
        if v is not None:
            out[key] = round(v, 2)
    bbox = parse_bbox(e.get("bbox")) if e.get("bbox") is not None else None
    if bbox:
        out["bbox"] = [round(x, 5) for x in bbox]
    return out


def collect_entities(obj: Any, out: list, seen: set, depth: int = 0, cap: int = 400) -> None:
    """Walk a tool result and gather entity-shaped dicts (for the clickable chips)."""
    if depth > 7 or len(out) >= cap:
        return
    if isinstance(obj, dict):
        ent = compact_entity(obj)
        if ent is not None and ent.get("title"):
            key = (ent["kind"], ent["id"])
            if key not in seen:
                seen.add(key)
                out.append(ent)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                collect_entities(v, out, seen, depth + 1, cap)
    elif isinstance(obj, list):
        for v in obj[:600]:
            if isinstance(v, (dict, list)):
                collect_entities(v, out, seen, depth + 1, cap)


def mentioned_entities(reply: str, candidates: list[dict], pinned: list[dict], limit: int = 8) -> list[dict]:
    """Entities whose exact title (or long id) appears in the reply as a whole word, in order of
    first mention, plus any the model opened on the map. Longer titles claim their words first, so
    "Ever" doesn't also match inside "Ever Given"."""
    text = (reply or "").casefold()
    hits: list[tuple[int, dict]] = []
    seen: set = set()
    taken: list[tuple[int, int]] = []
    for ent in pinned:
        key = (ent["kind"], ent["id"])
        if key not in seen:
            seen.add(key)
            hits.append((-1, ent))
    for ent in sorted(candidates, key=lambda e: -len(e.get("title") or "")):
        key = (ent["kind"], ent["id"])
        if key in seen:
            continue
        needles = []
        title = (ent.get("title") or "").casefold()
        if len(title) >= 3:
            needles.append(title)
        if len(ent["id"]) >= 5:
            needles.append(ent["id"].casefold())
        for needle in needles:
            pos = _find_word(text, needle, taken)
            if pos >= 0:
                taken.append((pos, pos + len(needle)))
                seen.add(key)
                hits.append((pos, ent))
                break
    hits.sort(key=lambda p: p[0])
    return [e for _, e in hits[:limit]]


def _find_word(text: str, needle: str, taken: list[tuple[int, int]]) -> int:
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return -1
        end = i + len(needle)
        before = text[i - 1] if i > 0 else " "
        after = text[end] if end < len(text) else " "
        free = all(end <= a or i >= b for a, b in taken)
        if free and not (before.isalnum() or after.isalnum()):
            return i
        start = i + 1


# ---------------------------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------------------------
def parse_messages(raw: Any) -> list[dict]:
    """Browser history -> [{role, content}] ending with a user message (400 otherwise)."""
    if not isinstance(raw, list) or not raw:
        raise HTTPError(400, "BAD_FIELD", 'Send "messages": a list of {role, content} ending with your question.')
    if len(raw) > MAX_MESSAGES:
        raw = raw[-MAX_MESSAGES:]
    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPError(400, "BAD_FIELD", f"Message {i + 1} must be an object with role and content.")
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant"):
            raise HTTPError(400, "BAD_FIELD", f'Message {i + 1} needs role "user" or "assistant".')
        if not isinstance(content, str):
            raise HTTPError(400, "BAD_FIELD", f"Message {i + 1} needs text content.")
        limit = USER_MAX_CHARS if role == "user" else ASSISTANT_MAX_CHARS
        if len(content) > limit:
            raise HTTPError(400, "BAD_FIELD", f"Message {i + 1} is too long ({len(content):,} characters, "
                                              f"the limit is {limit:,}).")
        content = content.replace("\x00", "").strip()
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
    if not out or out[-1]["role"] != "user":
        raise HTTPError(400, "BAD_FIELD", "The last message must be your question (role user).")
    # Keep the most recent turns within the history budget, starting on a user turn.
    kept = out[-KEEP_MESSAGES:]
    while len(kept) > 1 and sum(len(m["content"]) for m in kept) > KEEP_CHARS:
        kept = kept[1:]
    while kept and kept[0]["role"] != "user":
        kept = kept[1:]
    return kept


def parse_view(raw: Any) -> dict:
    """The browser's map view, validated. Unknown or bad fields are dropped, never fatal."""
    view: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return view
    bounds = parse_bbox(raw.get("bounds"))
    if bounds:
        view["bounds"] = [round(x, 5) for x in bounds]
    zoom = as_float(raw.get("zoom"), None, 0, 24)
    if zoom is not None:
        view["zoom"] = round(zoom, 2)
    center = raw.get("center")
    if isinstance(center, dict):
        lat, lon = center.get("lat"), center.get("lon", center.get("lng"))
    elif isinstance(center, (list, tuple)) and len(center) == 2:
        lon, lat = center
    else:
        lat = lon = None
    if geo.valid_latlon(lat, lon):
        view["center"] = {"lat": round(float(lat), 5), "lon": round(float(lon), 5)}
    for key in ("basemap", "projection"):
        v = raw.get(key)
        if isinstance(v, str) and re.fullmatch(r"[a-z0-9_-]{1,30}", v):
            view[key] = v
    if isinstance(raw.get("terrain"), bool):
        view["terrain"] = raw["terrain"]
    on = [v for v in str_list(raw.get("layers_on"), limit=200) if LAYER_ID_RE.fullmatch(v)]
    layers = []
    if isinstance(raw.get("layers"), list):
        for item in raw["layers"][:300]:
            if not isinstance(item, dict):
                continue
            lid = item.get("id")
            if not isinstance(lid, str) or not LAYER_ID_RE.fullmatch(lid):
                continue
            row: dict[str, Any] = {"id": lid}
            for key, size in (("title", 80), ("group", 40), ("status", 20), ("message", 160)):
                v = item.get(key)
                if isinstance(v, str) and v.strip():
                    row[key] = v.strip()[:size]
            if isinstance(item.get("on"), bool):
                row["on"] = item["on"]
            count = item.get("count")
            if isinstance(count, (int, float)) and not isinstance(count, bool) and math.isfinite(count):
                row["count"] = int(count)
            elif isinstance(count, str) and count.strip():
                row["count"] = count.strip()[:20]
            layers.append(row)
    if layers:
        view["layers"] = layers
        if not on:
            on = [r["id"] for r in layers if r.get("on")]
    view["layers_on"] = on
    return view
