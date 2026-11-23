"""Small helpers shared by the European camera adapters (``eu_*.py``). No sources live here.

- :func:`xml_text`: cleaned text of the first element at a path (namespaces already stripped);
- :func:`datex_values`: a DATEX II multilingual ``<values><value lang="en">...</value></values>``
  block as ``{lang: text}``;
- :func:`pick_lang`: the best text from a ``{lang: text}`` mapping (English first by default);
- :func:`post_cached`: a POST answer kept in the raw cache like ``FetchContext.download`` (reused
  while fresh, an older copy survives a failure), for APIs that only answer POST queries.
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

from ..common import FetchContext, clean


def xml_text(el: ET.Element | None, path: str = ".", limit: int = 160) -> str:
    """Cleaned text at ``path`` under ``el`` ('' when missing)."""
    if el is None:
        return ""
    found = el if path in (".", "") else el.find(path)
    return clean(found.text, limit) if found is not None and found.text else ""


def datex_values(el: ET.Element | None) -> dict[str, str]:
    """``<x><values><value lang="no">A</value><value lang="en">B</value></values></x>`` -> {'no': 'A', 'en': 'B'}.
    Also accepts the ``values`` element itself. Values without a language are stored under ''."""
    out: dict[str, str] = {}
    if el is None:
        return out
    for v in el.iter("value"):
        text = clean(v.text)
        if text:
            out.setdefault((v.get("lang") or "").lower(), text)
    return out


def pick_lang(value: Any, prefs: Iterable[str] = ("en",)) -> str:
    """The first non-empty text for the preferred languages, else any text ('' when none)."""
    if isinstance(value, str):
        return clean(value)
    if not isinstance(value, dict):
        return ""
    for lang in prefs:
        text = clean(value.get(lang))
        if text:
            return text
    for text in value.values():
        if isinstance(text, str) and clean(text):
            return clean(text)
    return ""


def post_cached(fc: FetchContext, url: str, body: bytes, filename: str, *, max_age_hours: float = 20.0,
                headers: dict | None = None, timeout: float = 60.0, min_bytes: int = 2,
                max_bytes: int = 100_000_000) -> Path:
    """POST ``body`` to ``url`` and keep the answer at ``raw/<source>/<filename>``.

    A copy younger than ``max_age_hours`` is reused (unless the build is forced). When the request
    fails and an older copy exists, the older copy is returned and a note is added. The request body
    (which may hold a key) is never written to disk; errors are redacted by ``m.net``.
    """
    path = fc.raw_path(filename)
    if path.exists() and not fc.force:
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        if age_h < max_age_hours:
            return path
    try:
        resp = fc.net.request(url, method="POST", data=body, headers=headers, timeout=timeout, retries=1,
                              max_bytes=max_bytes)
        if len(resp.body) < min_bytes:
            raise ValueError(f"answer too small ({len(resp.body)} bytes)")
        tmp = path.with_name(path.name + f".part{os.getpid()}")
        tmp.write_bytes(resp.body)
        os.replace(tmp, path)
        return path
    except Exception:
        if path.exists():
            fc.notes.append("using an older copy (the query failed)")
            return path
        raise
