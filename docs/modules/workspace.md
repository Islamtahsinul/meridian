# Workspaces, My places, deep links and exports (module `workspace`)

Programme 2, phase 3b (Workspaces and MCP team). One toolbar button, **Workspaces** (shortcut `w`),
opens a drawer with three tabs and a **Copy link** tool:

- **Workspaces**: save the map as it is (view, background map, globe or flat, 3D terrain, exactly which
  layers are on and their settings), apply, update, rename, delete, export and import; five built-in
  presets.
- **My places**: your own named places (home, work, family...), kept on this computer, with quick fly-to,
  a map layer, search, and an optional hand-off to Alerts as a place watchlist.
- **Export**: "Export what's on screen" for any layer that is on, as GeoJSON, CSV or KML, made in the
  browser.

Deep links (the URL hash carries the view, the layers that are on and one open card) are part of the
core: `web/js/core/deeplink.js`, wired in `web/js/core/app.js` and offered to modules as `ctx.deeplink`.

| Files | |
|---|---|
| `meridian/modules/workspace/` | `__init__.py` routes, `model.py` validation and presets, `db.py` storage |
| `web/js/layers/workspace.js` | Browser entry: toolbar button, shortcut, `ctx.workspace` |
| `web/js/layers/workspace/` | `panel.js` the drawer, `places.js` My places, `exporter.js` reading the map, `logic.js` pure helpers |
| `web/js/core/deeplink.js` | The deep-link format (core file, owned by this team) |
| `web/css/workspace.css` | Styles, all prefixed `workspace-` |
| `tests/test_workspace.py`, `tests/test_workspace_web.py`, `tests/fixtures/workspace/` | Tests |

Nothing is downloaded and no key is needed. There is no events provider, no search provider on the
server and no AI tool: places in particular are never offered to the assistant or to MCP tools.

## Saved workspaces

A workspace is a named state (version 1), stored in `workspace_saved` in Meridian's database so it
survives browser changes and moves with the data folder:

```json
{"v": 1,
 "view": {"center": [24.9, 59.6], "zoom": 6.2, "bearing": 0, "pitch": 0},
 "basemap": "dark", "projection": "mercator", "terrain": false,
 "layers": {"ships": {"on": true, "options": {"types": "cargo,tanker"}}, "ships.dark": {"on": true}},
 "notes": ""}
```

- Every part is optional. Values that can't be used are dropped, not refused (so a file from an older
  or newer Meridian still loads); a state that isn't an object, has another version or is over 48 KB is
  refused. Layer ids are `[A-Za-z0-9_.:-]{1,80}` (at most 150), option ids `[A-Za-z0-9_.-]{1,60}` (at
  most 40 per layer), option values are booleans, finite numbers or text up to 300 characters.
- **Saving** captures the view, the display settings and every layer that is on with its declared
  options (toggles, selects, ranges as the layer declares them).
- **Applying** sets the display settings, flies to the view, sets the options (checked against each
  layer's own declarations), turns on exactly the workspace's layers and turns the others off. Layers
  this page doesn't have are left out with a note ("2 of its layers aren't available here"). The
  message has **Undo**, which puts back the map as it was, including the settings the workspace changed
  on layers that were off (in the layer and in its saved options).
- Names are unique without regard to case; at most 100 workspaces. Saving under a name you already use
  offers to replace that workspace.
- **Export** writes `meridian-workspaces-YYYYMMDD.json` (one or all); **Import** reads such a file:
  a workspace with the same name as one you have replaces it, the others are added, anything unusable is
  skipped with a reason. The file:

  ```json
  {"kind": "meridian-workspaces", "v": 1, "exported_at": "2026-11-23T17:00:00Z", "count": 1,
   "workspaces": [{"name": "Baltic watch", "state": {"v": 1, "...": "..."}}]}
  ```

### Built-in presets

| Preset | View | Layers |
|---|---|---|
| Middle East watch | 45 E, 27.5 N, zoom 4.3 | `intel.conflict` (3 days, severity 4+), `intel.protests`, `intel.advice`, `flights.military`, `flights.gpsjam` (24 h), `flights.czib`, `ships` (cargo and tankers), `ships.straits`, `ships.dark` |
| UK weather day | 2.6 W, 54.3 N, zoom 5.4 | `space.radar`, `hazards.warnings` (MeteoAlarm), `hazards.floods`, `cameras` (traffic), `infra.grid` |
| Maritime chokepoints | 52 E, 18 N, zoom 2.8 | `ships` (cargo and tankers), `ships.straits`, `ships.dark`, `ships.sanctioned`, `infra.ports` |
| Space and aurora tonight | 20 W, 62 N, zoom 2.3, globe | `space.aurora`, `space.spaceweather`, `space.iss`, `space.launches` |
| Disaster response | 20 E, 15 N, zoom 1.8 | `hazards.quakes` (past week), `hazards.disasters`, `hazards.storms`, `hazards.fires`, `hazards.volcanoes`, `hazards.tsunami`, `hazards.mapping`, `hazards.warnings` (severe and extreme) |

All use the dark map. A test checks that every preset layer and option exists in the browser code.
Presets are starting points: apply one, change it, then save it under your own name.

## My places

Named places with a type (home, work, family, travel, other), a position, a radius (used if you watch
the place in Alerts), a zoom for flying there and optional notes. At most 50.

- **Local only.** Places are kept in `workspace_places` in Meridian's database on this computer. They are
  never sent to the assistant, MCP tools or any website. Adding a place doesn't geocode anything: you
  add the centre of the map, or draw a circle (`ctx.draw`), after moving the map yourself.
- **Quick fly**: the list's Fly to button, the search box (local results, "Home, My place"), and the
  **My places** layer (Saved group) with a card per place. Places use their own entity kind, `my_place`
  (id = the place's number), so search results and deep links (`e=my_place:3`) open their card without
  touching the core's geocoder `place` results.
- **No star, no case file.** A place card carries no core entity, so it has no favourite star and no
  **Add to case file**: favourites are searched by the assistant's `search_entities` (and so by MCP
  tools) with their exact position. The card has its own **Fly to**, the list of what Meridian has
  nearby, and no "What's nearby" (that card asks OpenStreetMap and the weather source about the point).
  For the same reason the address bar and **Copy link** never name an open place card.
- **Watch in Alerts** (only when the alerts module runs): after a confirmation that explains it, the
  browser calls Alerts' own API, `POST /api/alerts/watchlists` with `{action: "create", name, area:
  {type: "place", place: {name, lat, lon}, radius_km}, rules}` and the same starting rules Alerts uses
  (earthquakes, tropical storms, weather warnings, volcano alerts, those Alerts can run). Alerts then
  keeps its own copy; no Alerts file or table is touched. The place remembers the watchlist id (shown as
  "In Alerts"); when that watchlist is deleted in Alerts the place can be offered again. The
  confirmation says that a watchlist's name and alerts can reach the assistant when Alerts shares them.

## Deep links

```
http://127.0.0.1:8790/#map=8/57.6/11.2&l=ships,ships.straits&e=strait:ships:danish_straits
```

| Key | Value | Rules |
|---|---|---|
| `map` | `zoom/lat/lon[/bearing[/pitch]]` | MapLibre's own key: it restores it at start-up and keeps it current |
| `l` | layer ids on, comma-separated; `l=` (empty) means none | At most 32; ids must be registered; a list with no known id is ignored; turning on more than 8 asks first |
| `e` | `kind:id` of one entity card; the kind ends at the first colon (camera ids contain colons) | Kind `[a-z][a-z0-9_]{1,39}` with a card handler; id at most 120 characters from `[A-Za-z0-9:._~@+,=-]`, percent-encoded where needed |

- **Live.** The hash follows the page: `l=` changes as layers are switched, `e=` names the topmost card
  that has an entity whose kind has a handler (My places cards have none). Copying the address bar gives a
  working link; **Copy link** builds one from the current view (with a fallback box when the browser won't
  copy).
- **Opening a link** in a new tab or browser: the layers start as the link says (nothing else is switched
  on just to be switched off), the card opens without flying (the link's view wins), and a message
  "Opened a shared view" offers **Undo**. Pasting another link into an open tab applies it the same way.
- **For that tab only.** A link's layers are applied with `persist: false`: the saved layer set (what
  Meridian opens with next time) stays as it was until you switch a layer yourself, which saves what is
  on, as always. While the modules load, the link's layers stand in for the saved set so that they start
  as the link says; the saved set goes back once every module has registered, or if the page closes
  first. A link that would turn on more than 8 layers, the assistant's limit for one `set_layers` action,
  asks first ("This link turns on 10 layers: ..."), and is applied once the modules have loaded.
- **Reloading** a tab doesn't re-apply the layers its own hash carries (sessionStorage remembers what the
  tab wrote), so the saved layer set wins as before, including layers the Timeline restores after a
  replay. The card in `e=` reopens.
- **Hostile links** are harmless: every value is validated, unknown keys are ignored, nothing is evaluated
  or turned into HTML, and a link never sends a POST: it only switches layers as the user could (each
  layer then fetches its own data, at most 8 newly on without asking) and opens one card, and it never
  changes the saved layer set. Links only work where Meridian runs (this PC, or a tunnel to the same port).
- `ctx.deeplink`: `link({entity})` (absolute URL for the current view), `copy()`, `parse(hash)` and
  `build({map, layers, entity})`. The MCP bridge's `map_link` tool builds the same format in Python.

## Export what's on screen

Pick a layer that is on and a format; **Download** writes the file in the browser (nothing is uploaded).

- **What goes in**: what that layer has drawn inside the current view, as it is now. It is not the whole
  dataset; the drawer and the file say so (`note` in GeoJSON, the KML description, and `on-screen` in the
  file name: `meridian-hazards-quakes-on-screen-20261123-1702.csv`).
- **How**: `queryRenderedFeatures` on the layer's own `ov-` map layers (so filters and visibility count),
  then each drawn feature is matched back to its source feature (`GeoJSONSource.getData()`, matched on its
  scalar properties) for exact positions and whole lines and shapes rather than tile pieces. Clusters are
  expanded to their points (`getClusterLeaves`). Unmatched features keep their drawn geometry.
- **Which map layers** belong to a layer: `ov-<module>-` ids whose next word names the layer, with
  abbreviations (`ov-infra-ap-*` is `infra.airports`, `ov-space-sats` is `space.satellites`). A few map
  layers draw several layers at once (all aircraft layers share one source); exporting one of those
  includes everything that source draws, and the result says so.
- **Once each**: sources are read in style order, and a point with the same stable id (`id`, `hex` or
  `mmsi`) as a point already taken from another source, within about 11 m, is left out. That is how a
  camera and its street-zoom name (`ov-cameras-names`) become one row; lines, shapes and the parts of
  one thing in a single source are never dropped this way. The drawer's count follows the same rule.
- **Formats**: GeoJSON (RFC 7946; positions to 5 decimals; `name`, `attribution`, `licence`,
  `exported_at`, `note`, `layer` and `bbox` at the top); CSV (UTF-8 with a byte-order mark for Excel,
  RFC 4180 quoting, CRLF lines; columns `layer, kind, id, title, lat, lon, time_utc, geometry` (the title
  is always text, never a count; an unnamed ship is titled `MMSI <number>`), then the
  layer's own fields, then `source`; shapes give the centre of their extent; any text starting with
  `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading apostrophe so a spreadsheet won't run it);
  KML 2.2 (one style in the layer colour, placemarks with escaped names, `TimeStamp` when a time is known,
  every geometry type, no remote icons, credits in the document description).
- **Credits**: the layer's source text, plus the licences of the sources declared for that layer
  (`/api/core/status`).
- **Never exported**: Windy webcams (their terms allow API links only); aircraft in the LADD or PIA
  privacy programmes (checked against the flights module's own answers; if that check can't be made, no
  aircraft at all); fields named owner or registrant; ship and aircraft trails, selections and hover
  helpers. Camera exports carry ids, positions and names (for up to 600 cameras in view), never pictures.
  Aircraft rows gain callsign, registration, type, altitude, speed and track from the flights module.
- **Limits**: 50,000 features and 25 MB per file. Picture layers (map tiles such as radar or imagery)
  have no features and say so.

## Endpoints

All under `/api/workspace/`, JSON, standard guards on every POST (Origin, `X-Meridian`, JSON body).

| Endpoint | |
|---|---|
| `GET /api/workspace/workspaces` | `{workspaces: [{id, name, state, summary, created_at, updated_at, builtin}], presets: [{id, name, description, state, builtin}], max}` |
| `GET /api/workspace/workspaces/{id}` | One saved workspace, or a preset (`builtin:maritime`). 404 otherwise |
| `POST /api/workspace/workspaces` | `{action: "create", name, state}`, `{action: "update", id, state, name?}`, `{action: "rename", id, name}`, `{action: "delete", id}`, `{action: "import", workspaces: [...]}` or `{action: "import", file: <export>}` -> `{ok, ...}`. 409 `NAME_TAKEN`, 400 `BAD_FIELD`, 404 `NOT_FOUND`. Body up to 512 KB |
| `GET /api/workspace/export` | Every saved workspace as one importable document (above) |
| `GET /api/workspace/places` | `{places: [{id, name, type, lat, lon, radius_km, zoom, notes, alerts_watchlist_id, created_at, updated_at}], max}` |
| `POST /api/workspace/places` | `{action: "create", place}`, `{action: "update", id, place}` (partial), `{action: "delete", id}`, `{action: "watchlist", id, watchlist_id}` (or `null`). Body up to 16 KB |

## Storage

`workspace_saved(id, name UNIQUE NOCASE, state, created_at, updated_at)` and `workspace_places(id, name,
type, lat, lon, radius_km, zoom, notes, alerts_watchlist_id, created_at, updated_at)`. The user's own
data: kept until the user deletes it (include both tables in profile exports and backups). Layer
options are applied through the core's own per-layer storage (`meridian.layers.opt.<id>`), as the layer
panel does.

## Tests

- `python -m unittest tests.test_workspace -v`: state and place validation (hostile values), the presets
  (and that their layers and options exist in the browser code), storage (CRUD, unique names, limits,
  import merge), and the HTTP routes through the real server and its guards.
- `python -m unittest tests.test_workspace_web -v` runs three Node suites:
  `tests/fixtures/workspace/deeplink_test.mjs` (deep-link encode and decode, hostile values, MapLibre
  precision, links from the MCP bridge), `tests/fixtures/workspace/logic_test.mjs` (states, layer
  attribution, matching, privacy rules, GeoJSON, CSV and KML) and
  `tests/fixtures/workspace/browser_test.mjs` (with a fake page: place cards without a star or case file,
  Undo after applying a workspace, exports without repeated cameras, and deep links that never change the
  saved layer set and ask before turning on more than 8 layers).

## Known gaps and ideas

- Workspaces don't keep open cards, drawn shapes or the replay time yet, and links have no `t=` (replay
  time) or `ws=` (open a saved workspace) key; both are easy additions to the formats above.
- Exports depend on map-layer naming for a few layers; a per-layer export hook (below) would let each
  module export its own fields and terms.
- Places can't be imported or exported (kept local on purpose); the profile backup will carry them.
- Ideas: a quick switcher on `w` with the workspace list; per-place briefings and watched airports
  (research P4) through `GET /api/workspace/places`; "Copy link" on every card.

## Core changes I'd ask for

1. `web/js/core/layers.js`: `ctx.layers.setOption(id, name, value)` that validates, saves, calls
   `onOption` and refreshes once. Today `panel.js` does what the layer row does
   (`entry.options[name] = value`, saving `meridian.layers.opt.<id>`), which ties it to the core's
   internals; an expanded layer row also shows the old value until it redraws.
2. Layer specs: a `mapLayers: [...]` list (or the research's `export({bounds, options})` hook), so
   "Export what's on screen" needn't infer map layers from their names.
3. `web/js/core/cards.js`: a "Copy link" button on every card with an entity (`ctx.deeplink.link()`).
4. CONTRACT.md: add `ctx.deeplink` to section 10 and the link format to 15, and the browser-only entity
   kind `my_place` to the kinds in 5.6 (its cards carry no entity, so it never reaches favourites or case
   files).
5. `meridian/core_api.py` `search_saved`: leave out kind `my_place`, as a second guard. No browser path
   saves a place as a favourite any more, but a favourite saved by an earlier build would still reach
   `search_entities`, and so the assistant and MCP tools, with its exact position.
