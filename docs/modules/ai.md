# Ask Meridian (module `ai`)

A map assistant. You ask a plain-language question ("Which cameras are near Heathrow?"); it looks the
answer up in Meridian's own data through tools, writes a short answer that says where each fact came
from, and moves the map to show it. It runs on Claude over the Anthropic API with your own key, or on a
local model through Ollama, free and offline.

Files: `meridian/modules/ai/` (backend), `web/js/layers/ai.js` and `web/js/layers/ai/*.js` (drawer,
Markdown renderer, map actions), `web/css/ai.css`, `tests/test_ai.py`, `tests/test_ai_markdown.mjs`,
`tests/fixtures/ai/`.

## Settings (`.env`)

| Setting | Default | What it does |
|---|---|---|
| `AI_PROVIDER` | `auto` | `auto` (Claude when the key is set, else Ollama when it is running, else off), `anthropic`, `ollama` or `off`. |
| `ANTHROPIC_API_KEY` | none (secret) | Your Anthropic API key. Create one at https://platform.claude.com/settings/keys. |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Most capable ($4 / $20 per million input / output tokens). Cheaper: `claude-sonnet-5-5` ($2 / $10) or `claude-haiku-5-5` ($0.10 / $0.50). |
| `ANTHROPIC_EFFORT` | `medium` | How much Claude thinks: `low` is fastest and cheapest; `high`, `xhigh`, `max` think longer. |
| `ANTHROPIC_FALLBACK` | `on` | When Claude's safety filter declines a question, Anthropic retries it on its recommended fallback model (server-side `fallbacks: "default"`). Set `off` to disable. |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Must be on this computer: 127.0.0.0/8, `::1` or `localhost` (mapped to 127.0.0.1, never looked up). Anything else is refused. |
| `OLLAMA_MODEL` | first suitable installed model | A model with tool support, for example `qwen3:8b` or `llama3.1:8b`. |

The key is read once at start-up, so restart Meridian after adding it. Ollama is found without a
restart ("Check again" in the drawer).

## Sources

| Source | Licence and terms | Refresh |
|---|---|---|
| Anthropic Claude API (`ai.anthropic`) | Commercial API used with your own key under Anthropic's terms. | One request per assistant step, only when you ask. |
| Ollama (`ai.ollama`) | Ollama is MIT licensed; each model has its own licence. | On demand, on this computer. |
| OpenStreetMap Nominatim (the core's `core.nominatim`) | ODbL. Used by the `find_place` and `whats_here` tools with the same parameters as the core, so its 24 h cache and 1 request per second spacing are shared. | On demand. |

**Privacy.** With Claude, your question, the recent conversation, a summary of your map view (time,
centre, visible area, layers on) and the tool results are sent to Anthropic. With Ollama nothing
leaves the computer. The drawer says which applies.

## Endpoints

| Endpoint | |
|---|---|
| `GET /api/ai/status` | Provider, model, readiness and the reason when it isn't ready, settings that aren't secret, the tool list (core and module tools, and any a provider rejected), limits and token counts since start. `?fresh=1` re-checks Ollama. Never a secret. |
| `POST /api/ai/chat` | `{messages: [{role, content}], view: {bounds, zoom, center, layers_on, layers, basemap, projection, terrain}}` -> `{ok, reply, actions, trace, entities, notes, provider, model, usage, rounds, stopped, error, fallback, elapsed_ms}`. Add `"async": true` to get `{job}` back at once. Body up to 512 KB. When no provider is set up it answers 200 with `ok: false`, `error.code: "NOT_CONFIGURED"` and the setup message as `reply`. |
| `GET /api/ai/jobs/{job}` | Progress of an async answer: `status` (running, done, cancelled, error), `phase`, `round`, the current `tool`, the live `trace`, the model's progress `notes`, and `result` when finished. Jobs are kept 10 minutes. |
| `POST /api/ai/jobs/{job}/cancel` | Stop an answer. No further model or tool calls are made; a model request already in flight finishes and is thrown away. |
| `GET /api/ai/tools` | The read-only tools for local clients such as the MCP bridge: `{version, count, tools: [{name, module, kind, description, input_schema}], note, excluded: {map_actions}, limits, stats}`. Built with the assistant's own `toolset()` (same name and schema checks); map actions are left out. |
| `POST /api/ai/tool` | `{name, arguments}` (body up to 32 KB) -> `{ok: true, tool, module, kind, ms, truncated, result}` or `{ok: false, ..., error: {code, message}}` with codes `BAD_INPUT`, `TOOL_FAILED`, `TIMEOUT`. 15 s per call, results fitted into 24,000 characters with `fit_json`, at most 4 at once and 60 a minute (429 `BUSY` or `RATE_LIMITED` with `Retry-After`). Unknown tools are 404 `UNKNOWN_TOOL`, map actions 400 `MAP_ACTION`. One log line per call. |

At most three answers run at once (429 `BUSY` otherwise). Bad input is a 400; provider failures are a
200 with `ok: false` and `error: {code, message}` (codes: `PROVIDER_AUTH`, `PROVIDER_BILLING`,
`PROVIDER_FORBIDDEN`, `PROVIDER_MODEL`, `PROVIDER_RATE_LIMIT`, `PROVIDER_UNAVAILABLE`,
`PROVIDER_UNREACHABLE`, `PROVIDER_BAD_REQUEST`, `TIMEOUT`, `TOO_LONG`), so the trace still comes back.

### MCP bridge for Claude Code

`meridian/modules/ai/bridge.py` adds the two tool routes above, and `tools/mcp_meridian.py` is a stdio MCP
server that Claude Code starts and that calls them on this computer. Neither needs a key in Meridian,
and no new socket listens anywhere. Setup, security notes and troubleshooting: [docs/MCP.md](../MCP.md).

## How an answer runs

1. The tool list is rebuilt for every question: Meridian's data tools, its map tools, then every
   module tool from `m.app.ai_tools()` sorted by name (a module tool whose name clashes, or whose
   `input_schema` isn't a JSON Schema object, is left out with one warning in the log).
2. The question goes to the model with the system prompt (`prompt.py`) and a `<map_view>` block (UTC
   time, centre, zoom, visible area and its width in km, layers on). The system prompt never changes,
   so it and the tool list are prompt-cached; the per-question context lives in the user turn.
3. Up to 8 tool rounds within 90 s. Each round's tools run in parallel (15 s each); results are
   capped at 12 KB for Claude and 8 KB for Ollama by cutting long lists and strings, wrapped with a note
   saying what was dropped. When the rounds or the time run out (25 s are kept for the answer), the last
   call is made with `tool_choice: none` and a note asking for the answer now.
4. Map tools are validated and collected, never run on the server: camera moves keep only the last,
   `set_layers` calls merge, at most three entity cards. Unknown layer ids go back to the model as
   errors with the list of real ids.
5. The browser shows the answer, then validates the actions again and runs them, and offers Undo.

**Data tools:** `search_entities` (`m.app.run_search`), `find_place` (Nominatim search), `whats_here`
(Nominatim reverse plus `m.app.run_nearby`), `recent_events` (`m.app.collect_events` with hours,
kinds, bbox or `in_view`, minimum severity, sort), `list_layers` (`m.app.layer_specs` merged with the
browser's layer list and on/off state).

**Map tools** (action types match `registerCoreActions` in `web/js/core/app.js`): `fly_to`,
`fit_bounds`, `set_layers`, `set_basemap`, `set_projection`, `set_terrain`, `open_entity`, `highlight`,
`whats_here_card` (action `whats_here`). In the browser they run in this order: display settings,
layers (in the background, so they never hold up the camera), cards, then the camera, so an explicit
camera move wins over the move a card makes on its own; a card and a `fly_to` to the same spot become
one move at the zoom that was asked for.

### Claude request details

Raw HTTPS through `m.net` (no SDK): `POST https://api.anthropic.com/v1/messages`, headers `x-api-key`,
`anthropic-version: 2026-10-21`. Requests use adaptive thinking with `output_config.effort`, progress
notes between tool calls (`thinking.display: "updates"`, beta `thinking-display-updates-2026-08-18`,
shown live in the drawer), server-side refusal fallbacks (`fallbacks: "default"`, beta
`server-side-fallback-2026-07-01`), a cache breakpoint on the system prompt plus top-level automatic
caching, and `max_tokens` 16000 (not streamed). Options are only sent to models that take them (for
example none to `claude-haiku-4-5`), and if a model still rejects one with a 400 it is dropped for the
rest of the run and the request retried. Within an answer the history is append-only (assistant content,
thinking blocks included, is echoed back unchanged; after a fallback, blocks before the fallback
boundary are trimmed as Anthropic documents). Earlier turns are sent as text only. `stop_reason`
`refusal` ends the answer with an explanation and no map changes; `pause_turn` is resumed; `max_tokens`
answers are marked as cut short and their tool calls are not run. 429 and 5xx/529 are retried once when
`retry-after` is 8 s or less and time allows.

## Safety

- Tool output is untrusted: it only ever reaches the model as tool results, the system prompt says
  never to follow instructions found in it, and the only effects it could steer are map moves, which
  are validated twice and undoable.
- The renderer builds DOM nodes only (no HTML string APIs; a test checks the source); links must be
  absolute http(s) and pass `ctx.dom.safeUrl`, open in a new tab and show their full address on hover.
- People are not targets: the system prompt declines finding, following or profiling named private
  individuals (their aircraft, yacht, car, phone, home), and any number-plate or face identification.
- The key only goes into the request header. Errors and logs pass through the config's redaction, the
  status endpoint reports only whether a key is set, and the tests check all three.
- Ollama: loopback only, checked before any request, and requests are sent with `allow_redirects=False`,
  so a redirect is refused rather than followed.

## Known gaps and ideas

- Answers are not streamed token by token; the drawer shows live tool progress and Claude's progress
  notes instead. Streaming would need a chunked response from the core server.
- Earlier turns carry only the answer text, not their tool results, so a follow-up that needs earlier
  data looks it up again.
- No cost estimate in money, only token counts (hover the line under an answer; totals in the status).
- Ideas: a "send to case file" button for answers; per-question effort ("think harder"); images of
  camera stills for vision questions (with the people rule kept).

## Core actions used

The core map actions `set_projection {projection}` and `open_entity {entity}` (or the entity's fields at the
top level) work as CONTRACT 10.13 describes. The drawer still calls `ctx.map2.setProjection` directly for
`set_projection` (`web/js/layers/ai/actions.js`); it could use the core action instead.
