"""Ask Meridian: a map assistant that answers plain-language questions from the map's own data and
can operate the map. Claude over the Anthropic API (your key) or a local model through Ollama.

Endpoints (CONTRACT.md section 5.2):
    GET  /api/ai/status                  provider, model, tools and limits (never a secret)
    POST /api/ai/chat                    {messages, view} -> {reply, actions, trace, entities, provider, model, usage}
                                         add "async": true to get {job} back at once and poll the job instead
    GET  /api/ai/jobs/{job_id}           progress (live tool trace) and, when finished, the result
    POST /api/ai/jobs/{job_id}/cancel    stop a running answer
    GET  /api/ai/tools                   the read-only tools (no map actions), for local clients (bridge.py)
    POST /api/ai/tool                    {name, arguments} -> one read-only tool call (tools/mcp_meridian.py)
"""

from __future__ import annotations

from meridian.registry import HTTPError

from .bridge import register_bridge
from .engine import MAX_ROUNDS, TIME_BUDGET, Assistant
from .jobs import JobManager, valid_job_id
from .providers import DEFAULT_MODEL, DEFAULT_OLLAMA_URL
from .util import parse_messages, parse_view

BUSY = ("The assistant is already working on several questions. Wait for one to finish, or stop it, "
        "and try again.")


def register(m) -> None:
    from . import helper
    helper.bind(m)                       # ai.helper.complete for every module (CONTRACT 15.4)
    m.key("AI_PROVIDER", "Which assistant answers questions: auto (Claude when ANTHROPIC_API_KEY is set, "
                         "otherwise Ollama when it is running), anthropic, ollama or off.", secret=False)
    m.key("ANTHROPIC_API_KEY", "Anthropic API key for the Ask assistant (Claude). Questions, the map view and "
                               "tool results are sent to Anthropic.", "https://platform.claude.com/settings/keys",
          secret=True)
    m.key("ANTHROPIC_MODEL", f"Claude model for the assistant. Default {DEFAULT_MODEL} (most capable, $4 / $20 per "
                             "million input / output tokens). Cheaper: claude-sonnet-5-5 ($2 / $10) or "
                             "claude-haiku-5-5 ($0.10 / $0.50).",
          "https://platform.claude.com/docs/en/about-claude/models/overview", secret=False)
    m.key("ANTHROPIC_EFFORT", "How much Claude thinks before answering: low (fastest and cheapest), medium "
                              "(default), high, xhigh or max.", secret=False)
    m.key("ANTHROPIC_FALLBACK", "When Claude's safety filter declines a question, let Anthropic retry it on its "
                                "recommended fallback model: on (default) or off.",
          "https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback", secret=False)
    m.key("OLLAMA_URL", f"Local Ollama server for free, offline answers. Must be on this computer; default "
                        f"{DEFAULT_OLLAMA_URL}.", "https://ollama.com/download", secret=False)
    m.key("OLLAMA_MODEL", "Ollama model with tool support, for example qwen3:8b or llama3.1:8b. Default: the "
                          "first suitable model installed.", "https://ollama.com/search?c=tools", secret=False)

    m.source("ai.anthropic", "Anthropic Claude API (assistant answers)", "https://platform.claude.com/docs/en/api/overview",
             "Commercial API used with your own key under Anthropic's terms; answers are model output",
             refresh="on demand, one request per assistant step", needs_key="ANTHROPIC_API_KEY")
    m.source("ai.ollama", "Ollama (local models, optional)", "https://ollama.com/",
             "Ollama is MIT licensed; each model has its own licence", refresh="on demand, on this computer")

    m.net.set_host_interval("api.anthropic.com", 0.2)
    assistant = Assistant(m)
    jobs = JobManager(log=m.log)
    register_bridge(m, assistant)   # GET /api/ai/tools and POST /api/ai/tool (MCP bridge, read-only)

    @m.route("GET", "/api/ai/status")
    def status(req):
        return assistant.status(fresh=req.arg("fresh", False, type=bool))

    @m.route("POST", "/api/ai/chat", body_max=512 * 1024)
    def chat(req):
        body = req.json()
        messages = parse_messages(body.get("messages"))
        view = parse_view(body.get("view"))
        if body.get("async") is True:
            provider, _info = assistant.resolve()
            if provider is None:   # nothing to wait for: answer with the setup message straight away
                return {"job": None, "status": "done", "result": assistant.run(messages, view)}
            job = jobs.start(lambda cancel, emit: assistant.run(messages, view, cancel=cancel, on_event=emit))
            if job is None:
                raise HTTPError(429, "BUSY", BUSY)
            return {"job": job.id, "status": "running", "limits": {"max_rounds": MAX_ROUNDS, "time_budget_s": TIME_BUDGET}}
        with jobs.slot() as ok:
            if not ok:
                raise HTTPError(429, "BUSY", BUSY)
            return assistant.run(messages, view)

    @m.route("GET", "/api/ai/jobs/{job_id}")
    def job_status(req):
        job_id = req.params["job_id"]
        snap = jobs.snapshot(job_id) if valid_job_id(job_id) else None
        if snap is None:
            raise HTTPError(404, "NOT_FOUND", "That answer is no longer available. Ask again.")
        return snap

    @m.route("POST", "/api/ai/jobs/{job_id}/cancel")
    def job_cancel(req):
        job_id = req.params["job_id"]
        if not valid_job_id(job_id) or not jobs.cancel(job_id):
            raise HTTPError(404, "NOT_FOUND", "That answer is no longer running.")
        return {"ok": True, "job": job_id, "status": "cancelled"}
