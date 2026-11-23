# Meridian in Claude Code (MCP bridge)

Claude Code can use Meridian's read-only tools through a small MCP server: ask "which vessels are in
the Danish straits?", "any strong earthquakes near Japan today?" or "what's at 51.47, -0.45?" and Claude
Code answers from the Meridian running on this computer. Meridian's own assistant can stay off: the
bridge needs no key in Meridian.

| Part | What it is |
|---|---|
| `tools/mcp_meridian.py` | The MCP server. Claude Code starts it and talks to it over stdin and stdout (JSON-RPC 2.0, one message per line). Python standard library only. |
| `GET /api/ai/tools` | The tool list it mirrors: the Ask assistant's data tools and every module's AI tool, without map actions. |
| `POST /api/ai/tool` | One tool call, `{name, arguments}`, with the assistant's limits. |

Nothing new listens anywhere: the script opens no socket of its own and only connects to Meridian on
`127.0.0.1`, as the browser does.

## Register it (the user does this once)

Meridian must be running (`run.bat`) when you use the tools. Registration itself works either way.

1. Find the Python you run Meridian with, as a full path. Bare commands registered by tools have failed
   on this machine before, so the full path is the reliable choice:

   ```
   py -3.11 -c "import sys; print(sys.executable)"
   ```

2. Add the server to Claude Code for your user (all projects). Everything after `--` is the command
   Claude Code runs:

   ```
   claude mcp add meridian --scope user -- "C:\Path\To\python.exe" -I C:\path\to\meridian\tools\mcp_meridian.py
   ```

   The short form also works when `python` is on the PATH that Claude Code sees:

   ```
   claude mcp add meridian -- python C:\path\to\meridian\tools\mcp_meridian.py
   ```

   `-I` (isolated mode) is optional; it stops Python using `PYTHON*` environment variables, your user
   site-packages and the script's own folder as an import path, which suits a tool that runs in many
   places. `MERIDIAN_URL` still works with it.

3. Check it: `claude mcp list` should show `meridian` as connected. In a Claude Code session, `/mcp`
   lists its tools; they are named like `mcp__meridian__strait_status`.

To remove it: `claude mcp remove meridian` (add `--scope user` if you registered it that way).

### Another port

If Meridian runs on another port (for example an instance on 8780 reached through an SSH tunnel),
add `--port 8780` after the script path:

```
claude mcp add meridian --scope user -- "C:\Path\To\python.exe" -I C:\path\to\meridian\tools\mcp_meridian.py --port 8780
```

`--url http://localhost:8780` works the same way, and so does the `MERIDIAN_URL` environment variable
(Claude Code's `--env` option sets it for the server). The address must be `http://` to `127.0.0.1`,
`localhost` or `[::1]` with a port.
Anything else (another host, https, a user name, a path) stops the script at once with exit code 2 and a
message on stderr. `localhost` is never looked up in DNS; it connects to 127.0.0.1.

## The tools

`tools/list` mirrors `GET /api/ai/tools`, so new module tools appear without changing the bridge. On a
full Meridian there are about 50 (51 today, plus the bridge's own `map_link`), for example:

- the assistant's data tools: `search_entities`, `find_place` (OpenStreetMap Nominatim), `whats_here`,
  `recent_events`, `list_layers` (without the browser there is no "in view" or on/off state);
- module tools such as `strait_status`, `ships_in_area`, `vessel_details`, `dark_ships`,
  `flights_in_area`, `aircraft_details`, `flights_emergencies`, `gps_interference`, `earthquakes`,
  `hazards_recent`, `weather_warnings_at`, `conflict_events`, `news_search`, `country_profile`,
  `convergences`, `infra_near`, `airport_info`, `cable_info`, `sanctions_lookup`, `iss_position`,
  `satellite_passes`, `upcoming_launches`, `space_weather_now`, `alerts_recent` and more;
- `map_link`, added by the bridge itself: it returns a link that opens Meridian at a view, with chosen
  layers and one entity card (the deep-link format in [docs/modules/workspace.md](modules/workspace.md)).
  It contacts nothing and works even when Meridian is stopped. Opening the link shows its layers in that
  tab only, and Meridian asks first when it would turn on more than 8.

Never offered: the map actions (`fly_to`, `set_layers` and the rest: there is no page to run them in,
so the bridge answers "use map_link"), anything that writes (favourites, case files, alerts, workspaces),
My places, and camera pictures.

Every tool is marked `readOnlyHint: true`, `destructiveHint: false`; the data tools are
`openWorldHint: true` because several fetch live data.

## Limits

The server side applies the assistant's own limits to every call: 15 seconds (a late answer is
dropped), results fitted into 24,000 characters (long lists cut and long text trimmed, with a note),
at most 4 calls at once and 60 a minute (then "Meridian is busy"). The script runs up to 4 calls side
by side, so a slow call doesn't hold up a quick one, and drops the answer to a call Claude Code has
cancelled. Each call writes one line to `data/logs/meridian.log` (`ai: tool call ...`).

## Protocol

| Message | Answer |
|---|---|
| `initialize` | Protocol versions 2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05: the client's if it is one of these, otherwise the newest. Capabilities `{tools: {listChanged: false}}`, server info and short instructions. |
| `notifications/initialized`, `notifications/cancelled` | No answer (a cancelled call's answer is dropped). |
| `ping` | `{}` |
| `tools/list` | Every tool, with `title` and `annotations` from 2025-06-18 and 2025-03-26 on. |
| `tools/call` | `content` (the result as JSON text) and, from 2025-06-18, `structuredContent`. A tool that fails, gets bad arguments or times out answers `isError: true` with the reason. |
| Unknown tool or a map action | JSON-RPC error -32602. |
| Any other method | -32601. Bad JSON -32700, batches and malformed requests -32600. |

If Meridian is not running the script still starts: `tools/list` offers `map_link` only, and other calls
answer `isError: true` with "Meridian isn't running at http://127.0.0.1:8790. Start it with run.bat".

A scripted check (what the tests do, by hand):

```
python tools/mcp_meridian.py --port 8790
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"me","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"strait_status","arguments":{}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"map_link","arguments":{"lat":57.6,"lon":11.2,"zoom":8,"layers":["ships","ships.straits"]}}}
```

## Security and privacy

- **Prompt injection is the main risk.** Results carry text from public feeds that anyone can set: AIS
  ship names and destinations, news headlines, camera names. Claude Code can run commands and edit files.
  The bridge tells Claude in its instructions and in every tool description to treat results as data,
  never as instructions, strips control and direction-override characters, and caps sizes. Keep Claude
  Code's permission prompts on for shell commands and file edits in sessions that use Meridian. Allowing
  the `mcp__meridian` tools without a prompt is reasonable, because every one of them is read-only.
- **What leaves this computer.** Whatever Claude Code reads goes to the model provider your Claude Code
  uses, as with the Ask assistant on Claude. My places are never available to these tools: a place
  can't be starred or put in a case file, because `search_entities` searches favourites.
  `alerts_recent` returns your watchlists' names and their alerts only while Alerts shares them with the
  assistant (Alerts, Settings): a watchlist made from a place called "Home" would show that name.
- **Who can call the API.** The tool routes sit behind Meridian's standard guards (loopback Host;
  Origin and `X-Meridian` on every POST; `Sec-Fetch-Site`), so web pages can neither call nor read them.
  Any program running as you on this computer can, as with every Meridian route; the bridge adds no new
  way in. Meridian deliberately mints no token for it: a secret that same-user programs can read would not
  stop them. The real controls are scope (read-only tools only), the limits and the log.
- The script reads no secrets, writes no files, uses `http.client` directly (never `urllib`, which can
  pick up Windows proxy settings) and talks to nothing but Meridian on this computer.

## Troubleshooting

- `claude mcp list` says failed: run the exact command from the registration by hand. It should print
  `meridian-mcp: ready; Meridian at http://127.0.0.1:8790` on stderr and then wait for input
  (Ctrl+C to stop). "python is not recognised" means you need the full path to `python.exe`.
- Every call says Meridian isn't running: start it (`run.bat`), or check the port (`--port`).
- Only `map_link` is listed: Claude Code connected while Meridian was stopped, and the tool list is
  fixed at connection. Start Meridian, then reconnect the server in `/mcp` (or start a new session).
- Calls say Meridian is busy: more than 60 calls in a minute, or 4 slow calls at once. Wait a moment.
- A tool you expect is missing: its module isn't loaded (`--only`), or it failed to start (Sources and
  status in Meridian). Tool names come from `GET http://127.0.0.1:8790/api/ai/tools`.
