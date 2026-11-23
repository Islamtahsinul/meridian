"""Plain, sentence-case wording for alerts: the short "why" line under each title (CONTRACT.md rule 9)."""

from __future__ import annotations

from typing import Any

from .rules import Preset, _sev


def duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "a while"
    hours = seconds / 3600.0
    if hours < 1.5:
        return f"{max(1, round(seconds / 60))} minutes"
    if hours < 48:
        return f"{round(hours)} hours"
    return f"{round(hours / 24)} days"


def _n(v: Any) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def why(reason: str, preset: Preset, params: dict, ev: dict, area: str, *, level: int = 2,
        extra: float | None = None) -> str:
    """One line saying why this alert fired, e.g. "New in Pacific rim: magnitude 6.5, threshold 5"."""
    sev = _sev(ev)
    if preset.id == "storms":
        where = "centre inside the area" if level >= 2 else "forecast cone, track or wind area touches the area"
        detail = f"{where}, severity {_n(sev)}"
    else:
        detail = preset.detail(ev, params)
    if reason == "new":
        lead = f"New interference in {area}" if preset.gps else f"New in {area}"
    elif reason == "entered":
        lead = f"Moved into {area}"
    elif reason == "matched":
        lead = f"Worse in {area}" if preset.gps else f"Now meets your rule in {area}"
    elif reason == "escalated":
        word = "Got worse" if preset.gps else "Severity rose"
        return f"{word} in {area}: severity {_n(extra)} to {_n(sev)}"
    elif reason == "again":
        if preset.gps:
            return f"Back in {area} after {duration(extra)} without interference: {detail}"
        lead = f"Again in {area} after {duration(extra)}"
    elif reason == "closer":
        return f"Storm centre now inside {area}: severity {_n(sev)}"
    else:
        lead = f"In {area}"
    return f"{lead}: {detail}"


def digest_title(count: int, area: str) -> str:
    return f"{count:,} more alert{'s' if count != 1 else ''} in {area}"


def digest_why(limit: int, top_title: str | None, top_severity: float | None) -> str:
    text = f"Held back to keep this watchlist to {limit} alerts an hour."
    if top_title:
        text += f" Most severe: {top_title} (severity {_n(top_severity)})."
    return text
