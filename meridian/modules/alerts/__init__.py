"""Watchlists and alerts: named areas with rules, matched against the event history every two minutes.

Backend half of the Alerts feature (Programme 2, phase 2). Watchlists, rules, dedupe records and fired
alerts live in ``alerts_`` tables; the engine reads ``app.history.events`` and never registers an events
provider (CONTRACT.md 15.4). See docs/modules/alerts.md.
"""

from __future__ import annotations

import time

from . import db
from .api import ai_recent, register_routes
from .engine import CHECK_EVERY_S, Engine

_engine: Engine | None = None


def get_engine() -> Engine | None:
    """The running engine (tests and tools); other modules should use /api/alerts/recent."""
    return _engine


def register(m) -> None:
    global _engine
    db.setup(m.store)
    engine = Engine(m)
    _engine = engine

    m.source("alerts.engine", "Watchlist alerts, matched against Meridian's own event history", "",
             "Derived from each module's sources; nothing is downloaded", refresh="every 2 min, always",
             layer="alerts.areas")
    m.layer("alerts.areas", "Watchlist areas", "Saved",
            description="The areas your alerts watch. Open Alerts in the toolbar to add or change them.",
            source="Your watchlists", refresh="when you change them",
            use="Draw an area, pick a place and a radius, or watch the whole world.",
            count=lambda: db.count_watchlists(m.store))

    register_routes(m, engine)

    # Always on, unlike polling tasks: alerts are the point of an unattended monitor. The check reads
    # the local database only (no network), about 0.1 s every two minutes.
    m.task("check", engine.run, every=CHECK_EVERY_S, initial_delay=90)
    m.task("prune", lambda: db.prune(m.store, time.time()), every=6 * 3600, initial_delay=900)

    m.ai_tool(
        "alerts_recent",
        "Alerts fired by the user's watchlists (named areas with rules such as earthquakes M5+, aircraft "
        "emergencies, ships going dark, sanctioned ships, conflict events, tropical storms, severe weather "
        "warnings, volcano alerts, disease outbreaks and new GPS interference). Each alert has the area, the "
        "rule, the event title, a short reason, severity 0-10, the time (UTC) and the event's kind, id and "
        "position. Use it for questions like 'anything new in my areas?'.",
        {"type": "object", "properties": {
            "hours": {"type": "number", "description": "Look back this many hours (default 24, max 720)"},
            "area": {"type": "string", "description": "Only watchlists whose name contains this text (optional)"},
            "min_severity": {"type": "number", "description": "Lowest severity 0-10 (optional)"},
            "limit": {"type": "integer", "description": "Most alerts to return (default 20, max 100)"}},
         "additionalProperties": False},
        lambda args: ai_recent(m, args))
