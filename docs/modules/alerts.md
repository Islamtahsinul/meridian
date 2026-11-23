# Alerts module

Watchlists and alerts: named areas with rules that Meridian checks every two minutes against its own
event history (CONTRACT.md 15.3), so the map works as a monitor. Backend in `meridian/modules/alerts/`,
browser code in `web/js/layers/alerts.js` and `web/js/layers/alerts/`, styles in `web/css/alerts.css`.

Alerts are derived from other modules' events. Nothing is downloaded, no key is needed, and the module
never registers an events provider (CONTRACT.md 15.4), so alerts cannot feed back into convergences.

## Concepts

- **Watchlist**: a name, a colour (one of the four alert colours) and an area. Areas are drawn with
  `ctx.draw` (polygon, circle, rectangle), made from a place and a radius ("Place and radius": the core
  geocoder, then 5 to 1,000 km), or the whole world. Polygons that cross the antimeridian keep continuous
  longitudes; circles match by great-circle distance. Watchlists can be paused, hidden on the map,
  renamed, redrawn and deleted (deleting removes their alerts).
- **Rule**: one preset per watchlist, ticked on or off, with plain thresholds.
- **Alert**: one fired match, with the event's title, a short "why" line ("New in Baltic: magnitude
  6.5, threshold 5") and the event as an entity, so clicking it opens the card through
  `ctx.entities.open`.

## Presets

Kinds and fields were read from the live history (`/api/core/events`, `/api/core/history/events`).

| Preset | Label | Matches | Settings (default) |
|---|---|---|---|
| `quakes` | Earthquakes | `earthquake` with `magnitude` (or "M 6.5" in the title) at or above the threshold | Smallest magnitude, M 3 to M 7.5 (5) |
| `aircraft` | Aircraft emergencies | `aircraft` with `event: "emergency"` | Every emergency, or 7700 and 7500 only (every) |
| `ships_dark` | Ships going dark | `ship` with `category: "dark_ship"` | none |
| `ships_sanctioned` | Sanctioned ships seen | `ship` with `category: "sanctioned_vessel"` | none |
| `conflict` | Conflict events | `conflict_event` at or above a severity | 4 to 9 (6) |
| `storms` | Tropical storms | `storm` at or above a strength; with cones on, also when the forecast cone, forecast track or wind areas touch the area | Strength (any cyclone); cones (on) |
| `warnings` | Weather warnings | `warning` with `severity_text` Severe or Extreme | Extreme only (off) |
| `volcanoes` | Volcano alerts | `volcano` at severity 3, 6 or 9 | Any, orange or red, red (any) |
| `outbreaks` | Disease outbreaks | `outbreak`, in the area at any place the WHO report names | none |
| `gps` | GPS interference | `gps_jam` cells from flights (and ships' GNSS spoofing hotspots): only new, returning or worsening cells | Amber and red, or red only (both); include ships (on) |
| `any` | Any event | every kind at or above a severity | 3 to 10 (8) |

Storm cones and outbreak places come from `meridian.modules.hazards.get_service()` (documented by the
hazards module as readable by others), read-only and optional: without the hazards module, storms
match by centre and outbreaks by their first place. GPS cells also match when their 0.5° outline (or a
ship hotspot's radius) touches a small area.

## The engine

Every 2 minutes (first check 90 s after start-up), always on: watching is the point of an unattended
monitor, and a check only reads the local database (about 0.1 to 0.25 s for 2,700 events and 16 rules).

1. **Candidates.** `app.history.events` for the last 7 days of event time (late events can carry an old
   `time`; anything first reported more than 7 days after it happened is left out). For each rule only
   rows the history rewrote since its last check are examined: `last_seen` at or after the rule's
   watermark, which is the time of the last *completed* snapshot, read before the query. A snapshot that
   was still being written is therefore looked at again on the next check; repeats are harmless.
2. **Newness** comes from `first_seen`: first seen after the last check means "new".
3. **Area match**: `geo.point_in_geometry` (polygons, antimeridian included), great-circle distance for
   circles and places, and the storm, outbreak and GPS extensions above.
4. **Dedupe** per (watchlist, rule, module, kind, id) in `alerts_seen`:
   - The first check of a new, edited, resumed or redrawn rule takes a **baseline**: what already
     matched (first seen before the rule started watching) is recorded without alerting. Events first
     seen after the rule started alert even if they arrive before its first check.
   - After that the first match alerts: "new", "entered" for things that move (ships, aircraft, storms)
     or "matched" when a known event rises to the threshold. A known event whose own time is more than
     48 hours old (a module re-scoring old reports) only updates its record: that is not news.
   - A known event alerts again only when its severity rises by **2 or more** since its last alert
     ("escalated"), when a storm's centre reaches the area after its cone did ("closer"), or when a
     recurring entity comes back after a quiet gap ("again"): aircraft 6 h, dark ships 6 h, sanctioned
     ships 48 h, GPS cells 72 h, airport delays 12 h, straits 24 h. The history keeps one row per id,
     so this is how the same aircraft's next emergency is told apart from the current one.
   - GPS cells that repeat every day therefore stay quiet; you hear about new cells, cells back after
     three quiet days and cells whose severity rises by 2 (amber to red). Ship hotspots are folded into
     0.5° cells, since their ids follow their drifting position.
5. **Rate limit** per watchlist: at most 10 an hour by default (5, 10, 20 or 50), most severe first. The
   rest are kept, folded into one "N more alerts in <area>" digest per hour that grows (and turns unread
   again) as more arrive. The inbox expands it; `/api/alerts/recent` lists the folded alerts themselves.
6. **Quiet hours** (optional, the computer's local clock) only silence pop-ups: alerts still land in the
   inbox, and extreme alerts (severity 9 and more) can still pop up.

A failing check logs a warning, sets the `alerts.engine` source to failing and never stops the next one;
one broken watchlist never stops the others. Rules whose module is not running say so in the browser
("Needs the flights module, which is not running.") and simply never match.

## Storage and retention

| Table | Holds | Kept |
|---|---|---|
| `alerts_watchlists` | name, area JSON, colour, enabled, show on map | until deleted |
| `alerts_rules` | preset, settings, enabled, watch_from, baseline_at, checked_at (watermark) | until deleted |
| `alerts_seen` | dedupe records: reference severity, latest event time, storm level | 35 days after the last match |
| `alerts_alerts` | fired alerts, digests (`count`) and folded alerts (`digest_id`), read and cleared marks | 60 days, at most 20,000 |
| `alerts_settings` | pop-ups, quiet hours, rate limit, assistant sharing | kept |

## Endpoints

All under `/api/alerts/`, standard guards (POST needs `X-Meridian: 1`, the same Origin and JSON).
Errors are `{"error": {"code", "message"}}` with sentence-case messages.

| Method and path | What |
|---|---|
| `GET /presets` | Presets with labels, descriptions, settings (choices) and `available` |
| `GET /watchlists`, `GET /watchlists/{id}` | Watchlists with area (`type`, `summary`, `bbox`, `view_bbox`, `geometry`, `center`, `radius_km`, `place`), rules, `unread`, `last_24h` |
| `GET /watchlists/{id}/matches?limit=` | What matches now (reported in the last 25 minutes): a preview, no alerts |
| `POST /watchlists` | `{action: "create", name?, color?, area, rules?: [{preset, params?}]}`, `{action: "update", id, name?, area?, color?, enabled?, show_on_map?}`, `{action: "delete", id}` |
| `POST /rules` | `{action: "set", watchlist_id, preset, params?, enabled?}` (add or change), `{action: "delete", id}` |
| `GET /recent?since=&limit=&digests=` | CONTRACT.md 15.4: `{since, count, alerts: [{id, rule, area, title, time, severity, entity, ...}]}`, newest first. `since` in epoch s or ms (default 24 h ago), `limit` 1 to 1,000 (100). `rule` and `area` are plain text ("Earthquakes of magnitude 5 or more", "Baltic"); extra fields: `why`, `reason`, `watchlist_id`, `rule_id`, `preset`, `event_time`, `module`, `kind`, `ref`, `lat`, `lon`, `folded`, `read`. Folded (rate-limited) alerts are included; digests and test alerts are not (`digests=1` adds digests) |
| `GET /inbox?limit=&before=&after=&unread=&digest=` | Inbox items newest first (digests stand in for their folded alerts), `next_before` for paging, `after=<id>` oldest first for polling, `digest=<id>` lists a digest's folded alerts; plus the counts |
| `POST /inbox` | `{action: "read" or "unread", ids}`, `{action: "read_all"}`, `{action: "clear", read_only?}` (hides from the inbox; `/recent` keeps them), `{action: "test"}` (a test alert) |
| `GET /counts` | `{unread, inbox, latest_id, latest_at, last_24h, watchlists, by_watchlist, engine}`: what the browser polls |
| `GET /settings`, `POST /settings` | `{notify: {on, min_severity}, quiet_hours: {on, start, end, allow_extreme}, rate_limit, ai_share}`; POST takes any part |
| `GET /status` | Last check, runs, history info, preset availability |
| `POST /check` | Run a check now (at most every 10 s, 429 `BUSY` otherwise) |

## Browser

- **Toolbar**: "Alerts" with an unread badge (on the bell on narrow screens). It opens a drawer with
  three tabs: Inbox, Watchlists and Settings. The tabs follow the WAI-ARIA tabs pattern: one Tab stop,
  Left and Right arrows (wrapping), Home and End move between them.
- **Inbox**: newest first, severity tile coloured red (9 and more), amber (6 to 8) or blue, the why line,
  area and age; a dot toggles read. Clicking marks read and opens the entity. Digests expand. All or
  unread, mark all read, clear. Empty states: "No watchlists yet. Draw an area to start." and
  "No alerts yet. Meridian checks your watchlists every two minutes and lists anything new here."
- **Watchlists**: Draw an area (polygon, circle, rectangle), Place and radius, Whole world; a draft form
  (name, radius for places, colour, presets with settings) with a preview on the map; cards with the
  rules, pause switch, Edit (presets, colour, radius or redraw, "Show what matches now"), Show or hide on
  map, Fly to, Rename, Delete. Esc inside the draft or place form cancels that form only (as its Cancel
  button does) and keeps the drawer open; in the place search box Esc first clears the text.
- **Settings**: desktop notifications through `ctx.notify` (allow, on or off), pop-ups on or off and from
  which severity, quiet hours, alerts per hour, assistant sharing, Send a test alert, Check now.
- **Polling**: every 45 s while the page is open (also in the background, so desktop notifications can
  fire), and when the tab becomes visible. New alerts since the last one announced go through
  `ctx.notify.send` with tag `alerts:<id>`: at most three, most severe first, then one summary. A first
  visit announces nothing old.
- **Map**: the layer `alerts.areas` (Saved group) shows each watchlist through `ctx.draw.show` with its
  name; the draft preview uses id `alerts:draft`.

## AI tool

`alerts_recent {hours?, area?, min_severity?, limit?}`: fired alerts with UTC time, area, rule, title, why,
severity, kind, id and position, plus the watchlist names. It never returns area shapes. Sharing with the assistant
and MCP tools is off by default (watchlist names and places can be personal); the user turns it on in
Alerts, Settings. While it is off the tool says so.

## Sources and status

`alerts.engine` in Sources and status: live after each check, the count is alerts in the last 24 hours,
the note says how many watchlists, rules and events were checked.

## Known gaps and ideas

- Matching uses event points (plus the extensions above); a warning or disaster polygon that overlaps
  an area without its point inside does not match yet. The history does not keep event outlines.
- The first days of a new installation: GPS "new cell" detection needs the history to have seen the
  chronic cells once; a fresh history can call a chronic cell new the first time it appears.
- Events first reported more than 7 days after they happened are not alerted.
- Pop-ups need the page open; there is no push to phones (deliberately: see the research report).
- Ideas: mutes ("Not interesting here" for a detector in a 0.1° cell, as the research suggests), a layer
  of recent alert positions, alert rules on phase 3 categories (`new_interference`, `sts_candidate`,
  `military_surge`) once those detectors publish.
- My places (workspace module) can already create place watchlists with "Watch in Alerts".

## Tests

`python -m unittest tests.test_alerts tests.test_alerts_web -v`: areas (antimeridian, circles, places,
validation), presets, the decision function, the engine on synthetic history (baseline, late events by
`first_seen`, dedupe, escalation by 2, entering an area, recurring aircraft, GPS chronic, new,
returning and worse cells, cell outlines, ship hotspots, rate limit and digests, storm cones then centre,
outbreak places, rule and area edits, the snapshot watermark race, failures, missing modules, pruning,
the AI tool), the HTTP API with guards, and the browser logic in Node (quiet hours, notification choice,
tones, preview circles).
