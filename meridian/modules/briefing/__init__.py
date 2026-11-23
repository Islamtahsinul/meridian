"""Daily briefing: what changed since yesterday, per region, with links into the map.

Endpoints (CONTRACT.md sections 5.2 and 15.4):
    GET  /api/briefing/latest                 the latest briefing {id, time, period, sections, text, ai, ...}
    GET  /api/briefing/list?limit=            stored briefings, newest first (kept 60 days)
    GET  /api/briefing/status                 generator, schedule and AI narrative state
    GET  /api/briefing/{id}                   one briefing
    GET  /api/briefing/{id}/markdown          Markdown export (id may be "latest")
    POST /api/briefing/generate               {period: "24h" | "since_last" | "hours", hours?} -> starts one
"""

from __future__ import annotations

from meridian.registry import HTTPError, Response

from .build import compact
from .gather import _route
from .markdown import filename, to_markdown
from .service import SRC_AI, SRC_MAIN, BriefingService
from .store import KEEP_DAYS, LIST_MAX, valid_id

SECTION_IDS = ("headline", "your_areas", "europe", "mena", "ssa", "apac", "americas", "space", "cyber", "markets")
NOT_SHARED = "The user has switched off sharing alerts with the assistant (Alerts, Settings)."
AREAS_PARA = "Your areas: "
NOT_YET = ("No briefing has been made yet. Generate one now, or wait for the daily one after the briefing time "
           "(BRIEFING_TIME, default 07:00 local time).")


def without_areas(text: str) -> str:
    """The briefing's plain text without its "Your areas: ..." paragraph."""
    return "\n\n".join(p for p in text.split("\n\n") if not p.startswith(AREAS_PARA))


def register(m) -> None:
    from meridian.modules.ai import helper
    m.key("BRIEFING_TIME", "Local time (24-hour HH:MM) after which the daily briefing is made, once a day. "
                           "Default 07:00.", secret=False)
    m.source(SRC_MAIN, "Daily briefing (Meridian's event history and live module data)", "",
             "Derived from each layer's own sources, credited in every briefing",
             refresh="checked every 10 minutes, made once a day after BRIEFING_TIME, and on request")
    m.source(SRC_AI, "AI narrative for the briefing (optional)", "https://platform.claude.com/docs/en/api/overview",
             "Model output from the Ask assistant's provider: Claude with your own key, or local Ollama",
             refresh="once per briefing, when a provider is set up", optional=True)
    helper.bind(m)                       # makes ai.helper.complete available to every module
    svc = BriefingService(m)

    def not_found(bid: str):
        raise HTTPError(404, "NOT_FOUND", NOT_YET if bid == "latest" else "No briefing with that id (briefings are "
                                                                          f"kept {KEEP_DAYS} days).")

    def doc_for(bid: str) -> dict:
        if bid == "latest":
            doc = svc.db.latest()
        elif valid_id(bid):
            doc = svc.db.get(bid)
        else:
            raise HTTPError(400, "BAD_ARG", "That is not a briefing id.")
        if doc is None:
            not_found(bid)
        return doc

    @m.route("GET", "/api/briefing/latest")
    def r_latest(req):
        return doc_for("latest")

    @m.route("GET", "/api/briefing/list")
    def r_list(req):
        limit = req.arg("limit", 60, type=int, min=1, max=LIST_MAX)
        items, total = svc.db.list(limit)
        return {"briefings": items, "total": total, "keep_days": KEEP_DAYS}

    @m.route("GET", "/api/briefing/status")
    def r_status(req):
        return svc.status()

    @m.route("POST", "/api/briefing/generate", body_max=4096)
    def r_generate(req):
        basis = req.field("period", "24h", type=str, max_len=20)
        if basis not in ("24h", "since_last", "hours"):
            raise HTTPError(400, "BAD_FIELD", 'period must be "24h", "since_last" or "hours".')
        hours = req.field("hours", None, type=int)
        if basis == "hours" and (hours is None or not 1 <= hours <= 168):
            raise HTTPError(400, "BAD_FIELD", "hours must be a whole number from 1 to 168.")
        return svc.start("manual", basis, hours)

    @m.route("GET", "/api/briefing/{bid}")
    def r_one(req):
        return doc_for(req.params["bid"])

    @m.route("GET", "/api/briefing/{bid}/markdown")
    def r_markdown(req):
        doc = doc_for(req.params["bid"])
        text = to_markdown(doc, base_url=svc.base_url())
        return Response(text.encode("utf-8"), content_type="text/markdown; charset=utf-8", cache="no-store",
                        headers={"Content-Disposition": f'attachment; filename="{filename(doc)}"'})

    m.task("schedule", svc.tick, every=600, initial_delay=180)

    def alerts_shared() -> bool:
        """CONTRACT 15.5: the user's alerts reach the assistant and MCP tools only while Alerts, Settings,
        sharing (ai_share) is on. Read in-process; anything unexpected counts as off."""
        try:
            out = _route(m.app, "/api/alerts/settings")
        except Exception:                  # noqa: BLE001 - not running or broken: the gate stays shut
            return False
        return isinstance(out, dict) and out.get("ai_share") is True

    def tool_latest(args: dict):
        section = args.get("section")
        bid = args.get("id")
        if section is not None and section not in SECTION_IDS:
            return {"error": f"section must be one of {', '.join(SECTION_IDS)}."}
        if bid is not None and (not isinstance(bid, str) or not valid_id(bid)):
            return {"error": "id must be a briefing id such as 20261123T070012Z (see the briefing list)."}
        doc = svc.db.get(bid) if bid else svc.db.latest()
        if doc is None:
            return {"available": False, "message": NOT_YET}
        if section == "your_areas" and not alerts_shared():
            return {"available": True, "id": doc["id"], "message": NOT_SHARED}
        want = "areas" if section == "your_areas" else section
        out = compact(doc, section=want, include_areas=section == "your_areas")
        if not alerts_shared():
            out.pop("your_areas", None)          # no hint pointing at alerts the user has not shared
            if isinstance(out.get("text"), str):
                out["text"] = without_areas(out["text"])
        if want and "section" not in out:
            out["message"] = "That section is not in this briefing." if want != "areas" else \
                "There is no Your areas section (the alerts module is not running)."
        out["available"] = True
        return out

    m.ai_tool("briefing_latest",
              "The latest daily briefing (or one by id): what changed in the period, per region (Europe, Middle East "
              "and North Africa, Sub-Saharan Africa, Asia-Pacific, Americas) and globally (space weather, cyber and "
              "internet, markets and energy), with headline items, trends against the previous period and an AI "
              "narrative when one was written. Conflict and news items are machine-coded by GDELT and unverified. "
              "The user's own alert areas are left out unless section is your_areas, which works only while the user "
              "shares alerts with the assistant (Alerts, Settings).",
              {"type": "object", "properties": {
                  "section": {"type": "string", "enum": list(SECTION_IDS)},
                  "id": {"type": "string", "description": "A briefing id; default the latest"}},
               "additionalProperties": False}, tool_latest)
    m.service = svc
