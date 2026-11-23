"""Markdown export of a briefing: the deterministic text, the AI narrative (labelled) when there is
one, every section with its trends and items, map links and credits."""

from __future__ import annotations

import re

from .build import UNVERIFIED_NOTE, period_text, severity_word, utc

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|])")


def esc(text) -> str:
    """Escape Markdown and HTML specials so feed text cannot add links, images or markup."""
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(text or "").split()))


def map_link(entity: dict | None, base_url: str | None) -> str | None:
    """``http://127.0.0.1:<port>/#map=<zoom>/<lat>/<lon>`` (MapLibre's own hash) for an entity with a position."""
    if not base_url or not isinstance(entity, dict):
        return None
    lat, lon = entity.get("lat"), entity.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or isinstance(lat, bool):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    zoom = entity.get("zoom") if isinstance(entity.get("zoom"), (int, float)) else 8
    return f"{base_url.rstrip('/')}/#map={float(zoom):g}/{lat:.4f}/{lon:.4f}"


def _row(title: str, line: str, meta: list[str], link: str | None) -> str:
    head = f"**{esc(title)}**"
    if link:
        head += f" ([map]({link}))"
    text = f"- {head}"
    if line:
        text += f": {esc(line)}"
    if meta:
        text += f" _{esc(', '.join(m for m in meta if m))}_"
    return text


def to_markdown(doc: dict, *, base_url: str | None = None) -> str:
    p = doc["period"]
    lines = [f"# Meridian briefing, {utc(doc['time'])}", "",
             f"Period: {period_text(p['from'], p['to'])} ({esc(p.get('label') or '')}). "
             f"Made {'automatically' if doc.get('trigger') == 'scheduled' else 'on request'} at {utc(doc['time'])}.", ""]
    for note in (doc.get("coverage") or {}).get("notes") or []:
        lines.append(f"> {esc(note)}")
    if (doc.get("coverage") or {}).get("notes"):
        lines.append("")
    lines += ["## Summary", ""]
    for para in str(doc.get("text") or "").split("\n\n"):
        if para.strip():
            lines += [esc(para), ""]
    ai = doc.get("ai") or {}
    if ai.get("text"):
        who = " ".join(x for x in (ai.get("provider_label"), f"({ai['model']})" if ai.get("model") else None) if x)
        lines += ["## AI narrative", "", f"_Written by {esc(who or 'the AI assistant')} from the items below. It may "
                  f"contain mistakes; check the items and their sources._", ""]
        for para in str(ai["text"]).split("\n\n"):
            if para.strip():
                lines += [esc(para), ""]
    for sec in doc.get("sections") or []:
        lines += [f"## {esc(sec['region'])}", ""]
        if sec.get("summary") and sec["id"] != "headline":
            lines += [esc(sec["summary"]), ""]
        trends = sec.get("trends") or []
        if trends:
            if sec["id"] != "headline":
                lines += ["What changed:", ""]
            for tr in trends:
                meta = [tr.get("source") or ""]
                if tr.get("unverified"):
                    meta.append("unverified")
                if tr.get("notable"):
                    meta.append("notable change")
                lines.append(_row(tr["title"], tr.get("line") or "", meta, map_link(tr.get("entity"), base_url)))
            lines.append("")
        items = sec.get("items") or []
        if items:
            if trends and sec["id"] != "headline":
                lines += ["Items:", ""]
            for it in items:
                meta = []
                if sec["id"] == "headline" and it.get("region"):
                    meta.append(it["region"])
                if it.get("time"):
                    meta.append(utc(it["time"]))
                if it.get("severity") is not None and it.get("kind") not in ("note", "trend"):
                    meta.append(f"severity {it['severity']:g} ({severity_word(it['severity'])})")
                if it.get("source"):
                    meta.append(it["source"])
                if it.get("unverified"):
                    meta.append("unverified, machine-coded news")
                if it.get("new"):
                    meta.append("new")
                lines.append(_row(it["title"], it.get("line") or "", meta, map_link(it.get("entity"), base_url)))
            lines.append("")
        if not trends and not items:
            lines += [esc(sec.get("note") or "Nothing to report."), ""]
    lines += ["## Sources", ""]
    srcs = doc.get("sources") or []
    lines += [esc(", ".join(srcs)) + "." if srcs else "Meridian's event history.", "",
              esc(UNVERIFIED_NOTE), "",
              "_Built by Meridian from its own event history and live data. Severity runs from 0 (routine) to 10 "
              "(extreme). Times are UTC._", ""]
    return "\n".join(lines)


def filename(doc: dict) -> str:
    return f"meridian-briefing-{doc['id'].replace('T', '-').rstrip('Z')[:15]}.md"
