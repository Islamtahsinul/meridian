# Meridian build contract

Meridian is a personal, single-user situation map. It puts live cameras, aircraft, ships, conflict
data, hazards, infrastructure, companies, space and weather on one map. It is an **independent, open-data
situation map**. It does not copy, scrape or call any commercial service. Every
feature comes from public sources used within their terms.

This file is the source of truth for everyone building a module. Read all of it before writing code.
When this file and your instinct disagree, this file wins. When this file is silent, choose the
simplest thing that keeps the rules in sections 1–3.

---

## 1. Golden rules

1. **Open data only, used fairly.** Use public, documented sources within their terms and rate
   limits, and credit each one (`m.source(...)` plus the layer's `source` text). Never scrape a
   paywalled service, never bypass a login or paywall, never reuse another product's code, data,
   names or look. Personal, non-commercial use is the licence assumption (some sources are NC).
2. **People are not targets.** No tracking of named private individuals (no "billionaire jets"
   lists, no people layer). No face or number-plate recognition. Cameras show the latest frame only:
   no recording or history.
3. **Loopback only.** The server binds 127.0.0.1. Nothing in a module opens another listening
   socket. Outbound connections only.
4. **Secrets stay on the server.** API keys come from `.env` / environment through `m.config`.
   Never send a key to the browser, never log it, never put it in an error message, never commit it.
5. **Python standard library only** (Python 3.11). No pip installs. Browser code is vanilla ES
   modules with the libraries already in `web/vendor/` (MapLibre GL JS 5.24.1, hls.js 1.6.15,
   satellite.js 7.2.0). Need something else? Say so in your report; don't add it.
6. **Stay in your lane.** Edit only the files you own (section 13). Never edit core files
   (`meridian/*.py`, `meridian/modules/__init__.py`, `meridian/modules/sanctions/`, `web/index.html`,
   `web/css/core.css`, `web/js/core/*`, `server.py`, `tools/`). If the core blocks you, work around
   it inside your module and describe the exact core change you'd want in your final report.
7. **Fail soft.** A dead source makes its layer show an error state; it never crashes the server,
   the page or another module. Every network call has a timeout. Every handler validates input.
8. **Be a polite client.** Cache, back off, respect `Retry-After`, keep total traffic modest. Poll
   live sources only while the browser is using your module (`m.active()`) unless you need a
   continuous history (say why in your module doc).
9. **Plain, sentence-case copy.** Interface text is short, specific and in sentence case. No ALL-CAPS
   labels, no "A · B · C" strings (use commas or separate rows), no arrows on link text.

## 2. Architecture

```
server.py                    entry point: python server.py [--port N] [--only mod1,mod2] [--build all]
meridian/                    core (do not edit): config, net, store, geo, registry, scheduler,
                             datasets, guards, httpserver, core_api, log
meridian/modules/<name>/     one package per module, with register(m)
web/index.html               shell (do not edit)
web/js/core/*.js             browser core (do not edit)
web/js/layers/<name>.js      a module's browser entry, auto-loaded; exports register(ctx)
web/css/<name>.css           optional module styles, auto-linked; class names prefixed <name>-
data/                        gitignored: meridian.db, build/<module>/, raw/<module>/, cache/, logs/
tests/test_<name>.py         unittest, no network (fixtures in tests/fixtures/<name>/)
docs/modules/<name>.md       sources, licences, refresh, endpoints, known gaps
```

The browser loads `/api/core/manifest`, builds the map and shell, then imports every
`web/js/layers/*.js` (alphabetically) and calls `register(ctx)`. A module can be backend-only,
browser-only or both; backend and browser halves share the module name.

## 3. Server rules (already enforced by the core)

- Bind 127.0.0.1 (port 8790 by default). Host header must be `127.0.0.1:<port>` or `localhost:<port>`.
- Only GET, HEAD and POST. POST only under `/api/`, only `application/json`, and only with
  `Origin: http://127.0.0.1:<port>` and `X-Meridian: 1` (the browser `api.post` adds these).
- `/api/*` GETs from other sites are refused via `Sec-Fetch-Site`.
- CSP: scripts from self only (no inline scripts, no `eval`, no inline event handlers). Images,
  map tiles and media may come from any https origin. Workers may be blob:.
- JSON errors look like `{"error": {"code": "BAD_ARG", "message": "..."}}`.

## 4. Configuration

- `m.config.get(NAME, default)`, `get_int`, `get_float`, `get_bool`, `has(NAME)`.
- Declare every key or setting your module reads with `m.key(NAME, description, url, secret=True)`
  so the Settings table lists it. Non-secret settings use `secret=False`.
- Modules must work with no keys at all (reduced coverage is fine; explain it in the layer text).

## 5. Backend module API

```python
# meridian/modules/<name>/__init__.py
def register(m):          # m is a ModuleContext
    ...
```

### 5.1 Context attributes
`m.name`, `m.config`, `m.store` (SQLite), `m.net` (HTTP client), `m.log` (logger),
`m.data_dir` (`data/build/<name>/`), `m.raw_dir` (`data/raw/<name>/`), `m.cache_dir`,
`m.app` (the App, for `m.app.collect_events`, `m.app.run_search`, `m.app.run_nearby`,
`m.app.ai_tools()`, `m.app.call_ai_tool`).

### 5.2 Routes
```python
@m.route("GET", "/api/<name>/things/{thing_id}")       # {param} = one segment, {rest:path} = the rest
def thing(req):
    lat, lon = req.latlon()                             # validated floats (400 otherwise)
    bbox = req.bbox("bbox", None)                       # (w, s, e, n), w > e when crossing 180°
    limit = req.arg("limit", 100, type=int, min=1, max=1000)
    req.params["thing_id"]                              # path parameter (string)
    return {"ok": True}                                 # any JSON value, or a Response
```
- `@m.route(..., activity=False)` keeps calls from marking the module active (history reads, exports).
- Paths must start with `/api/<name>/`. POST handlers read `req.field(name, default, type=..., max_len=...)`
  and `req.json()`; set `body_max` on the route when bodies can exceed 64 KB.
- Raise `HTTPError(status, CODE, message)` (from `meridian.registry`) for client errors.
- `Response(body_bytes, content_type=..., cache="no-store"|"no-cache"|"max-age=60")` for non-JSON
  (camera images, etc.). `m.serve_file(path)` serves a built JSON file with ETag and gzip and
  answers 503 `BUILDING` while it does not exist yet.
- Handlers run on server threads: keep them fast (usually < 300 ms). Do heavy work in tasks and
  serve cached snapshots. Protect shared state with a lock or swap immutable snapshots.

### 5.3 Background work
```python
m.task("poll", fn, every=30, initial_delay=5, when=lambda: m.active())  # periodic; when() False skips
m.thread("stream", fn)                                                 # fn(stop_event); restarted on error
```
- A browser call to any `/api/<name>/...` route marks the module active; `m.active(within=180)`.
- Tasks report health with `m.status(source_id, ok=True, count=n)` or `ok=False, error="..."`.

### 5.4 Datasets (static or slowly changing data)
```python
def build(ds):
    path = ds.download(URL, "raw-file.csv", min_bytes=1000)   # cached in data/raw/<name>/, atomic
    rows = ...parse...
    ds.write_json("things.geojson", feature_collection)        # data/build/<name>/things.geojson
    ds.set_rows(len(rows)); ds.note("12,345 things")
m.dataset("things", build, max_age_hours=168, description="...", after=("other_dataset",))
m.on_dataset("things", load_into_memory)     # runs after each build, and at start-up if already built
m.dataset_ready("things")
```
- Builds run in the background at start-up (missing first, then stale), up to 3 at a time.
  `python server.py --build <name> [--force]` builds synchronously.
- A failed rebuild keeps the previous files in service. Keep raw downloads under ~300 MB per module.

### 5.5 Discovery hooks
```python
m.layer(id, title, group, description=..., source=..., refresh=..., use=..., count=lambda: n)
m.source(source_id, name, url, licence, refresh="every 30 s", layer="flights", needs_key="X" or None,
         optional=False)   # optional=True: failures don't count as warnings in the status bar
m.search(fn)     # fn(q: str, limit: int) -> [entity]            (fast: < 300 ms, in-memory)
m.nearby(fn)     # fn(lat, lon, radius_km, limit) -> [entity]     (fast)
m.events(fn)     # fn(since_ts: float) -> [event]                 (for convergences and the AI)
m.ai_tool(name, description, input_schema, fn)  # fn(args: dict) -> JSON; read-only; < 10 s
```

### 5.6 Shared shapes
**Entity** (search, nearby, lists, favourites, case files, AI):
```json
{"kind": "aircraft", "id": "4ca2b3", "title": "RYR8NE", "subtitle": "Boeing 737-8AS, EI-DCL",
 "lat": 51.47, "lon": -0.45, "layer": "flights", "score": 0.8, "time": 1795380000,
 "zoom": 10, "bbox": [w, s, e, n]}
```
- `id` is stable and unique within `kind`. Canonical ids: aircraft = ICAO 24-bit hex, lower case;
  ship = MMSI string; camera = `<source>:<native id>`; airport = OurAirports `ident` (ICAO where it
  exists); port = `wpi:<n>` or `locode:<code>`; power_plant = GPPD `gppd_idnr`; country = ISO 3166-1
  alpha-2 upper case; satellite = NORAD catalogue number; others `<source>:<native id>`.
- `layer` is the browser layer id that shows it. `time` is epoch seconds. `score` 0..1 (search only).

**Canonical kinds:** aircraft, ship, camera, airport, port, power_plant, dam, datacenter, cable,
landing_point, company, instrument, earthquake, fire, volcano, storm, flood, warning, outbreak,
disaster, tsunami, conflict_event, frontline, news, risk_zone, country, convergence, cyber, outage,
satellite, launch, strait, gps_jam, sanctioned_vessel, sanctioned_aircraft, place, point.
Added in Programme 2: space_weather, airport_delay, flight_restriction, airspace_advisory, protest,
news_spike, advice_change, emergency_mapping, gauge, launch_pad, air_activity (`category` one of `military_surge`,
`gps_interference`, `airport_drop`; `gps_jam` events carry `category` `new_interference` or `chronic_interference`,
`trend` and `days_affected_7d`; consumers skip chronic cells unless they want background) and ship anomalies as kind `ship` with categories
(and alert, which is never an events-provider kind).

**Event** (`m.events`): an entity plus `time` (epoch s, required) and `severity` (0–10):
0–2 routine, 3–5 notable, 6–8 serious, 9–10 extreme. Return only notable things (not every
aircraft): e.g. M4.5+ quakes, fire clusters, active warnings, emergency squawks, GPS jamming cells,
dark or sanctioned ships, conflict events, high-severity news.

### 5.7 Shared Python services
- `from meridian import geo`: `haversine_km`, `bearing_deg`, `destination`, `in_bbox`,
  `bbox_of_circle`, `point_in_geometry`, `geometry_bbox`, `GridIndex(cell_deg)` with `.add`,
  `.near(lat, lon, km, limit)`, `.in_bbox(bbox)`.
- `from meridian.modules import sanctions`: `lookup_vessel(imo=, mmsi=, name=)`,
  `lookup_aircraft(hex=, reg=, msn=)`, `vessels()`, `aircraft()`, `ready()`, `version()` (changes when any
  list reloads: re-match on it), `summary()`, `record(uid)`. One merged index of OFAC, UK, UN and EU lists:
  records keep every original key and add `lists` (e.g. `["OFAC", "UK"]`), `programmes`, `listings`,
  `listed_on` and `imos`. Name-only vessel matches return `match == "name"`: treat them as weak and never
  show them as confirmed. Say "sanctions lists" (or the list names), not "OFAC", unless OFAC is the only list.
- `m.net`: `get_json(url, params=, headers=, timeout=, ttl=, retries=, min_interval=)`, `get_text`,
  `get_bytes`, `post_json`, `request(...)` (returns `.status .headers .body`; `allow_redirects=False`
  turns a 3xx into an error status), `download(url, dest)`,
  `set_host_interval(host, seconds)`. Raises `meridian.net.NetError` (`.status`). Use it for all
  HTTP. Raw sockets (WebSocket/TCP streams) may use `socket`/`ssl` directly.
- `m.store`: `execute`, `executemany`, `query` (list of dicts), `one`, `scalar`, `script`,
  `tx()` context manager, `kv_get`, `kv_set`. Table names start with `<name>_`.

## 6. HTTP politeness

- The generic User-Agent is set for you; never add names, emails or project details to requests.
- Default timeouts 20 s; tile/image proxies 10 s. `retries=1` for polling, 2 for dataset downloads.
- Use `ttl=` for anything the browser can ask for repeatedly. Set `m.net.set_host_interval(host, s)`
  for hosts with published limits (adsb.fi 1/s, Nominatim 1/s, GDELT DOC 1 per 5 s, ...).

## 7. Storage

One SQLite file (`data/meridian.db`, WAL). Core tables: `kv`, `favourites`, `casefiles`,
`casefile_items` (read-only for modules). Create your own with `m.store.script("CREATE TABLE IF NOT
EXISTS <name>_...")`. Keep history bounded (delete rows older than your window in a task).

## 8. Geography

Coordinates are WGS84 decimal degrees; GeoJSON order is `[lon, lat]`. bbox strings are
`west,south,east,north`. Round coordinates to 5 decimals in API output. Distances in km in APIs.

## 9. Core endpoints (for reference)

`/api/core/manifest`, `/api/core/status`, `/api/core/layers`, `/api/core/search?q=`,
`/api/core/geocode?q=`, `/api/core/reverse?lat&lon`, `/api/core/whatshere?lat&lon&radius_km`,
`/api/core/nearby?lat&lon&radius_km&kind=`, `/api/core/events?hours=&kinds=&bbox=`,
`/api/core/favourites` (GET, POST add/remove), `/api/core/casefiles` (GET, POST actions),
`/api/sanctions/check?imo&mmsi&hex&reg`, `/api/sanctions/vessels?list=`, `/api/sanctions/aircraft?list=`,
`/api/sanctions/lists`.

### 9.1 Cross-module endpoints (agreed between modules)
| Owner | Endpoint | Returns | Used by |
|---|---|---|---|
| flights | `GET /api/flights/airport/{ident}?lat&lon` | `{arrivals: [...], departures: [...], overhead: n, note}` (live aircraft near the airport: descending = arriving, climbing = departing; each item an aircraft entity plus `alt_ft`, `route`) | infra airport card |
| ships | `GET /api/ships/port-activity?lat&lon&radius_km` | `{moored, anchored, underway, arrivals_24h, departures_24h, vessels: [...top 20 entities], coverage}` | infra port card |
| infra | `GET /api/infra/country/{iso2}` | `{cables: n, cable_names: [...], landing_points: n, power_plants: n, capacity_mw: {fuel: mw}, airports: n, ports: n, datacenters: n}` | intel country card |
| intel | `GET /api/intel/country/{iso2}` | country profile (World Bank indicators, instability index, recent events) | anyone; entity kind `country` |
| infra | `data/build/infra/ports.json` | `[{"id", "name", "country", "lat", "lon", "size"}]` | ships (read-only, optional) |

Server-side, call another module's route in-process with `m.app.call_route('GET', '/api/alerts/recent', {'since': t})`
(no HTTP, raises `HTTPError` 404 when the route is absent; catch it and degrade). Also used today: the briefing reads
`/api/alerts/recent` and `/api/intel/cyber`, and the file `data/build/intel/countries.geojson`.
Browser cross-module calls must check `ctx.moduleOk('<owner>')` first and degrade silently.
Browser events: `ctx.bus.emit('imagery:compare', {lat, lon, zoom, before: 'YYYY-MM-DD', after: 'YYYY-MM-DD'})`
opens the space module's before/after imagery tool (hazard and conflict cards may offer it).

## 10. Browser API (`ctx` passed to `register(ctx)`)

### 10.1 Overview
```js
export async function register(ctx) { ... }   // web/js/layers/<name>.js
```
`ctx.map` (MapLibre map), `ctx.maplibregl`, `ctx.manifest`, `ctx.api`, `ctx.bus`, `ctx.h`, `ctx.fmt`,
`ctx.dom`, `ctx.layers`, `ctx.cards`, `ctx.entities`, `ctx.search`, `ctx.whatshere`, `ctx.favourites`,
`ctx.casefiles`, `ctx.ui`, `ctx.map2`, `ctx.ai`, `ctx.settings`, `ctx.moduleOk(name)`.
`window.__meridian` is the same object (used by tests).

### 10.2 DOM and formatting (`ctx.h`, `ctx.fmt`, `ctx.dom`)
- Build all DOM with `ctx.h(tag, attrs, ...children)`. Strings become text nodes. `href`/`src` are
  checked (http(s), relative, blob:, data:image only). External links open in a new tab with
  `noopener noreferrer`. **Never use `innerHTML`, `insertAdjacentHTML` or `outerHTML` with data.**
- `ctx.fmt`: `int`, `num(n, d)`, `fixed`, `compact`, `pct`, `km`, `ft`, `kt`, `deg`, `money(n, cur)`,
  `latlon(lat, lon, d)`, `utc(t)`, `clock(t)`, `ago(t)` (t = epoch s or ms, ISO string or Date).
- `ctx.dom`: `clear`, `append`, `svgIcon(pathD)`, `debounce`, `throttle`, `copyText`, `download`,
  `toMs`, `validLatLon`, `haversineKm`, `safeUrl`, `esc`.

### 10.3 Events (`ctx.bus.on(evt, fn)` returns an unsubscribe function)
`map:ready`, `map:moveend {bounds, zoom}`, `map:click-empty {lngLat}`, `layer:toggled {id, on}`,
`card:opened {key}`, `card:closed {key}`, `basemap:changed {id}`, `projection:changed {type}`,
`terrain:changed {on}`, `favourites:changed`, `casefiles:changed`, `entity:opened {entity}`,
`app:ready`, `imagery:compare {...}`.

### 10.4 API client
`await ctx.api.get(path, params, {signal})`, `await ctx.api.post(path, body)`,
`ctx.api.url(path, params)` (absolute URL; use it for MapLibre GeoJSON `data` URLs).
Errors are `ctx.api.ApiError` with `.status`, `.code`, `.message`, `.retryAfter`.

### 10.5 Map helpers (`ctx.map2`)
- Overlay ids: sources and layers start with `ov-<name>-`; icons with `<name>-`.
- `addSource(id, spec)` (idempotent; geojson re-adds call setData), `addLayer(spec, slot)` where
  slot is one of `polygons`, `lines`, `points`, `moving`, `top` (z-order bands, bottom to top),
  `removeLayer(id)`, `removeSource(id)` (removes its layers first), `setVisible(id, on)`,
  `setData(sourceId, data)`, `emptyFC()`.
- `icon(name, draw(ctx2d, size), {size: 32, sdf: true})` registers a canvas-drawn icon; SDF icons
  are tinted with `icon-color`. Draw white shapes for SDF.
- `onClick(layerIds, (feature, lngLat, event, all) => ...)`: the topmost feature under the
  pointer across all modules wins. `onHover(layerIds, feature => 'tooltip text' | null)`.
- `flyTo(lat, lon, zoom)`, `fitBbox([w, s, e, n])`, `viewBounds()`, `highlight(lat, lon, color)`,
  `clearHighlight()`, `setBasemap(id)`, `setProjection('globe'|'mercator')`, `setTerrain(bool)`,
  `viewState()`, `reducedMotion()`.
- The map can be in globe projection: avoid assumptions that only hold in Mercator; use
  `viewBounds()` for queries (it returns the whole world on a zoomed-out globe).
- Text fonts available on the glyph server: `Open Sans Regular`, `Open Sans Bold`,
  `Open Sans Italic`, `Montserrat Medium`, `Montserrat SemiBold`, `Noto Sans Regular`.
- Group colours for paint properties (hex): Cameras `#efe3c2`, Flights `#e05aa8`, Ships `#4fa8e8`,
  Conflict `#e5534b`, Intelligence `#43c4b2`, Infrastructure `#c9b458`, Companies `#a7b6ff`,
  Events `#f0a43a`, Space `#a08ff5`, Weather `#86d6f7`, Imagery `#9cc29a`. Alerts: red `#ff5d52`,
  amber `#ffb547`, green `#5fd38d`, blue `#6cb6ff`. Halo/ink colour `#111a2c`.

### 10.6 Layers (`ctx.layers`)
```js
const layer = ctx.layers.register({
  id: 'flights', group: 'Flights', title: 'Aircraft', subtitle: 'ADS-B from adsb.lol and adsb.fi',
  description: 'Live aircraft positions in the area you are viewing.', source: 'adsb.lol (ODbL), adsb.fi',
  color: '#e05aa8', symbol: 'icon',          // 'point' | 'line' | 'area' | 'icon' (legend swatch)
  defaultOn: false, refreshMs: 10000, viewDependent: true, minzoom: null,
  async enable() {...}, async disable() {...}, async refresh({bounds, zoom, center, signal, reason, options}) {...},
  options: [{id: 'military', label: 'Military only', type: 'toggle', default: false},
            {id: 'minAlt', label: 'Lowest altitude', type: 'range', min: 0, max: 40000, step: 1000, default: 0, format: v => `${v} ft`},
            {id: 'kind', label: 'Show', type: 'select', choices: [{value: 'all', label: 'Everything'}], default: 'all'}],
  onOption(id, value, all) {...},
  legend: [{color: '#ff5d52', label: 'Emergency'}, {color: '#e05aa8', label: 'Airliner', shape: 'line'}],
});
layer.setCount(1234); layer.setStatus('error', 'adsb.lol is not answering');  // 'loading'|'ok'|'warn'|'error'|'idle'
layer.option('military'); layer.refresh();
await ctx.layers.suspend(['flights', 'ships']);   // off for a while, never saved; ctx.layers.resume() restores
```
- Groups (panel order): Cameras, Flights, Ships, Conflict, Intelligence, Infrastructure, Companies,
  Events, Space, Weather, Imagery, Saved.
- Layer ids: `<name>` or `<name>.<sub>`. Turn things on in `enable`, remove them in `disable`
  (`ctx.map2.removeSource`). Never recreate sources on refresh: call `setData`.
- `refresh` must honour `signal` (pass it to `ctx.api.get`). Thrown errors show on the layer row.
- `defaultOn: true` only for the few layers that make a good first impression and are cheap
  (agreed: `cameras`, `flights`, `ships`, `hazards.quakes`).

### 10.7 Cards (`ctx.cards.open(spec)`)
```js
const card = ctx.cards.open({
  key: 'aircraft:4ca2b3', kind: 'aircraft', title: 'RYR8NE', subtitle: 'Boeing 737-8AS, EI-DCL',
  icon: '✈', accent: '#e05aa8',
  badges: [{text: 'Emergency 7700', tone: 'red'}],              // tones: red amber green blue grey
  block: [{label: 'Altitude', value: '36,000', unit: 'ft'}, {label: 'Speed', value: '452', unit: 'kn'}],
  media: {type: 'image', src: '/api/cameras/img/tfl:00001.06514', refreshMs: 60000, credit: 'TfL'},
  sections: [{title: 'Flight', rows: [['Callsign', 'RYR8NE'], ['Squawk', '7700']]},
             {title: 'Route', el: someNode}, {title: 'About', text: '...'},
             {title: 'Recent', list: [{title, subtitle, detail, entity}]},
             {title: 'Nearby', nearby: {lat, lon, radius_km: 25, exclude: entity}}],
  links: [{text: 'Open on adsb.lol', href: 'https://...'}],
  actions: [{text: 'Show trail', onClick: (card) => {...}}],
  entity: {kind: 'aircraft', id: '4ca2b3', title: 'RYR8NE', lat, lon, layer: 'flights'},
  live: {everyMs: 10000, update: async (card) => ({block: [...], entity: {...}})},
  foot: 'Position from adsb.lol, 4 s ago', onClose() {...},
});
card.update({badges: [...]}); card.close();
```
- The block is the card's signature: 2–4 key readings in large numerals (keep values short).
- `entity` with lat/lon adds Fly to, What's nearby, Add to case file and the favourite star.
- Media types: `image` (auto-refresh with `refreshMs`), `mjpeg`, `hls`, `video`, `iframe` (sandboxed).

### 10.8 UI (`ctx.ui`)
`toast(msg, tone)`, `openMenu(anchorEl, items)`, `contextMenu(x, y, items)`,
`modal({title, body, wide, full, foot, onClose}) -> {close, body, setBody}`,
`confirm(msg, {ok, danger}) -> Promise<bool>`, `prompt(title, {label, value}) -> Promise<string|null>`,
`drawer({id, title, el, width, onClose, tools}) -> {close, body}` (floating panel bottom-left over
the map), `getDrawer(id)`, `toolbarButton({id, title, label, icon: svgPathD, onClick, order})`,
`shortcut(key, description, fn)`. Reserved shortcuts: `/ g l t ?`.

### 10.9 Entities (`ctx.entities`)
`ctx.entities.handler(kind, async (entity) => openCardFor(entity))` for every kind you own.
The handler must cope with a bare `{kind, id}` (from favourites and case files): fetch details by
id from your backend; if the thing is no longer live, open a card that says when it was last seen.
`ctx.entities.open(entity)` turns on `entity.layer`, flies there and calls the handler.

### 10.10 Search (`ctx.search.provider(q => [entities])`)
For data already loaded in the browser (e.g. live aircraft by callsign). Server search
(`m.search`) covers the rest. Keep both fast.

### 10.11 Favourites and case files
`ctx.favourites.isFav(kind, id)`, `add(entity, payload)`, `remove(kind, id)`, `toggle`, `list(kind)`,
`onChange(fn)`. `ctx.casefiles.addItemMenu(anchor, entity, payload)`. Cards with `entity` get both.

### 10.12 What's here (`ctx.whatshere.section(async (lat, lon) => section | null)`)
Add a section to the right-click "What's here?" card (weather, country, nearest port, ...).
Return null fast when you have nothing.

### 10.13 AI map actions (`ctx.ai`)
`ctx.ai.register(type, fn)` adds a map action; `ctx.ai.run({type, ...})`. Core actions: `fly_to
{lat, lon, zoom}`, `fit_bounds {west, south, east, north}`, `set_layers {on: [], off: []}`,
`set_basemap {id}`, `set_projection {projection: 'globe'|'mercator'}`, `set_terrain {on}`,
`open_entity {entity}` (or the entity's fields at the top level),
`whats_here {lat, lon}`, `highlight {lat, lon}`.

## 11. Design language

Plotting-table navy panels, chart-paper text, chart-symbology colours per group (10.5). Type is
Bahnschrift (DIN road-sign lineage), semi-condensed for chrome. Numbers use tabular figures. Map
symbols: circles for static points, rotated arrow or silhouette icons for moving things, thin
lines for networks, translucent fills (opacity ≤ 0.35) for areas. Alerts use the alert colours
only. Keep motion for user actions. Cards: one accent per group, a data block of 2–4 readings.

## 12. Testing (required before you report)

1. `python -m unittest tests.test_<name> -v` passes (no network: use small fixtures).
2. Start your own instance on your assigned port: `python server.py --port <port> --only <name>`
   (run it in the background, log goes to `data/logs/meridian-<name>.log`). Hit every endpoint with
   curl and check the JSON.
3. Browser check: `node tools/browser_check.mjs --url "http://127.0.0.1:<port>/#map=<z>/<lat>/<lon>" --wait 12000
   --shot .shots/<name>-1.png --eval "<js that turns your layers on and returns counts/state>"`.
   `window.__meridian.layers.setOn(id, true)` and `.list()` help. **Open the screenshot and look
   at it.** Fix console errors and exceptions from your code.
4. Stop your server when done (`netstat -ano | grep :<port>` then `taskkill //PID <pid> //F`).

## 13. Ownership

| Module | Owns | Port |
|---|---|---|
| core (lead) | everything in rule 6 | 8790 |
| flights | `meridian/modules/flights/`, `web/js/layers/flights.js`, `web/css/flights.css`, tests, docs | 8791 |
| ships | `meridian/modules/ships/`, `web/js/layers/ships.js`, `web/css/ships.css`, tests, docs | 8792 |
| cameras | `meridian/modules/cameras/`, `web/js/layers/cameras.js`, `web/css/cameras.css`, tests, docs | 8793 |
| hazards | `meridian/modules/hazards/`, `web/js/layers/hazards.js`, `web/css/hazards.css`, tests, docs | 8794 |
| space | `meridian/modules/space/`, `web/js/layers/space.js`, `web/css/space.css`, tests, docs | 8795 |
| intel | `meridian/modules/intel/`, `web/js/layers/intel.js`, `web/css/intel.css`, tests, docs | 8796 |
| infra | `meridian/modules/infra/`, `web/js/layers/infra.js`, `web/css/infra.css`, tests, docs | 8797 |
| ai | `meridian/modules/ai/`, `web/js/layers/ai.js`, `web/css/ai.css`, tests, docs | 8798 |

"tests, docs" means `tests/test_<name>*.py`, `tests/fixtures/<name>/` and `docs/modules/<name>.md`.
A module may split its browser code into `web/js/layers/<name>/*.js` files imported by
`web/js/layers/<name>.js` (only top-level `web/js/layers/*.js` files are auto-loaded).

## 14. Final report (what each builder sends back)

1. What works (features, layers, endpoints, AI tools, entity kinds), with real counts seen.
2. Sources: which are live when tested, which failed and why, which need a key.
3. Known gaps compared with the brief, and ideas for later.
4. Any core change you need (file, function, exact change) and why.
5. Test results (unit tests, endpoints, browser check: console errors left, screenshot path).

---

## 15. Programme 2 (v0.2): divisions, phases and shared additions

Everything in sections 1–14 still applies.

### 15.1 Phases
| Phase | Divisions and teams | Output |
|---|---|---|
| 1 | Platform (core APIs 15.3); Data division: Space weather, Energy, Aviation; Research division: Data sources, Analytics and product | New core APIs, three data layer sets, two research reports |
| 2 | Features division: Alerts, Timeline, Briefing | Watchlists and alerts, timeline replay, daily briefing |
| 3 | Augmentation division: teams picked from the research reports | Further features |
| 4 | Quality division: Security, Experience; lead integration | Reviewed, documented release |

### 15.2 Development rules (every team)
- Work in an isolated copy: `python tools/devcopy.py <team> [module ...]`, then `export MERIDIAN_DATA_DIR=<printed path>` and
  `export MERIDIAN_ENV_FILE=none` (keyless). Never write to the main `data/` folder and never touch port 8790 (the live app).
- Use only your assigned port. Run `--only <modules>` and `--no-tasks` unless you need live polling. Set `FLIGHTS_GPSJAM_SAMPLER=0`
  whenever the flights module runs in a dev instance.
- Browser checks: at most about 12 screenshots per team. Prefer `--eval` checks, and read every screenshot you take.
- Remove your dev copy when done (`rm -rf data-dev/<team>` after stopping your server). Never delete anything you did not create.

### 15.3 New core APIs (Platform team, phase 1)
**Event history (server).** The core snapshots `App.collect_events` every 5 minutes into `core_event_history`, keyed by module,
kind and id, with first seen and last seen. Retention is `MERIDIAN_HISTORY_DAYS` (default 30, 1–90).
- Python: `m.app.history.events(since, until=None, kinds=None, bbox=None, min_severity=None, limit=5000, seen_since=None) -> [event]`
  (`seen_since`: only rows written by a snapshot since then, i.e. new or changed) and
  `m.app.history.summary(since, until=None, bbox=None) -> {by_kind, by_module, by_hour}`.
- HTTP: `GET /api/core/history/events?from=&to=&kinds=&bbox=&min_severity=&limit=&seen_since=` and `GET /api/core/history/summary?from=&to=&bbox=`
  (from and to in epoch seconds).
- Each event is an Event (5.6) plus `module`, `first_time` and `time` (the earliest and latest event time its module reported:
  equal for one-off events, a span for ongoing ones such as storms, tracked ships or jamming cells; a provider may send its own
  `first_time`), `first_seen` and `last_seen` (snapshot times; `last_seen` is rewritten at most every 15 minutes when nothing
  else changed). `severity` is the highest seen; title, position, layer and extra fields are the latest. One row per key: a ship
  that is both dark and sanctioned is one row, the most severe report winning.
- An event matches when its span overlaps the window: `time >= since` and `first_time <= until` (`until=None` means no upper
  bound, so scheduled launches appear). Most recent first. `kinds` is a list or a comma-separated string, `bbox` is
  `(w, s, e, n)` (west > east crosses 180°). Bad arguments raise `ValueError`; `limit` is capped at 50,000.
- `summary` returns `{since, until, total, max_severity, by_kind: {kind: {count, max_severity}}, by_module: {...},
  by_hour: [{t, count, max_severity}]}`: largest first, hours with no events left out, `t` the UTC hour start; an event counts in
  the hour of its `time`, clamped into the window.
- HTTP: `from` defaults to 24 hours ago, `to` to no upper bound, milliseconds are accepted, `limit` is at most 10,000. The events
  answer is `{from, to, count, truncated, events}` and the summary `{from, to, total, max_severity, by_kind, by_module, by_hour}`.
  Invalid values give 400 `BAD_ARG`. `/api/core/status` reports `history` (rows, last snapshot, last prune).
- Snapshots start with the normal server (first one a minute after start-up, pruning a few minutes after and then daily), never
  under `--build` or `--no-tasks`. Tests can call `app.history.snapshot(now=...)` and `app.history.prune(now=...)` directly.

**Drawing (browser), `ctx.draw`.**
- `await ctx.draw.start({mode: 'polygon'|'circle'|'rectangle', color})` resolves to a GeoJSON Polygon Feature, or `null` if
  cancelled. Circles carry `properties.center [lon, lat]` and `properties.radius_km`.
- `ctx.draw.cancel()`, `ctx.draw.active()`.
- Keys: Esc cancels, Enter finishes a polygon, Backspace removes the last point. A hint bar explains what to do.
- `ctx.draw.show(id, feature, {color})` and `ctx.draw.hide(id)` display saved shapes.
- The Feature is `{type, bbox: [w, s, e, n], geometry: {type: 'Polygon', coordinates: [ring]}, properties: {mode, area_km2, ...}}`
  with a closed, counter-clockwise ring rounded to 5 decimals. Rectangles are longitude/latitude boxes (the shorter way round);
  circles are 72-point geodesic polygons, radius at most 5,000 km and kept off the poles.
- Shapes that cross the antimeridian keep continuous longitudes (for example 175 to 185) and a wrapped `bbox` (west > east).
  `ctx.draw.contains(feature, lat, lon)` and Python `geo.point_in_geometry` handle such shapes.
- Polygons: click to add points; double-click, Enter or a click on the first point finishes. Circles and rectangles: press and
  drag, or click twice (in these modes a drag draws instead of panning). Clicks off the globe are ignored. While drawing, map
  clicks, double-clicks and right-clicks never reach MapLibre or modules (no cards, no zoom, no "What's here?") and the core
  tooltips pause. Starting a new drawing cancels the current one. The hint bar has Undo, Finish and Cancel buttons for touch.
- `show` takes a Polygon or MultiPolygon (Feature or geometry) and an optional `label`; the same id replaces the shape. Layers:
  `ov-core-shapes-fill` (polygons slot), `ov-core-shapes-line` (lines), `ov-core-shapes-label` (top); the drawing preview is
  `ov-core-draw-*` in the top slot.

**Notifications (browser), `ctx.notify`.**
- `ctx.notify.permission()`, `await ctx.notify.request()`, `ctx.notify.enabled()`, `ctx.notify.setEnabled(bool)`.
- `ctx.notify.send({title, body, tag, tone, entity})`: a desktop notification when allowed and enabled (clicking it focuses
  Meridian and opens the entity), always an in-app toast, and the same `tag` never repeats within 10 minutes.
- `permission()` is `'granted'`, `'denied'`, `'default'` or `'unsupported'`. Call `request()` from a click; it resolves to the
  permission. `enabled()` is true when permission is granted and the setting is on (on by default once granted). The status bar
  has a "Notifications" switch that does the same for the user.
- `send` throws `TypeError` without a title and returns `{toast, desktop, suppressed}`. Tones: red, amber, green, blue, grey.
  Desktop notifications show only while Meridian is not the focused tab (the toast covers that case), at most six a minute. The
  tag rule covers the toast too and holds across reloads and tabs. With an entity the toast gets a Show button.
- `ctx.ui.toast(message, tone, ms, {title, action: {text, onClick}})` gained that optional fourth argument. Bus event:
  `notify:changed {permission, enabled}`.

**Replay clock (browser), `ctx.time`.**
- `ctx.time.mode()` returns `'live'` or `'replay'`. `ctx.time.now()` returns ms, the replay time when replaying.
- `ctx.time.setReplay(ms)` enters or moves replay. `ctx.time.live()` returns to live.
- Events: `time:mode {mode}` and `time:changed {t}`. Live layers may ignore replay; the Timeline team draws replay layers.
- `setReplay` also accepts seconds, ISO strings and Dates, clamps the future to now, returns the replay time in ms and throws
  `TypeError` for anything else. `time:changed` (payload `{t, mode}`) fires for each `setReplay` that moves the time and once on
  returning to live; live time does not tick on the bus. The top-bar clock shows the replay time in amber. A reload is live.

### 15.4 Cross-team endpoints (phase 2)
| Owner | Endpoint | Returns |
|---|---|---|
| timeline (in flights) | `GET /api/flights/history?from&to&bbox&limit`, `GET /api/flights/history/{hex}?from&to` | Sampled positions `{hex, t, lat, lon, alt, trk, cls, flags}`, at least 60 s apart per aircraft, kept 72 h |
| timeline (in ships) | `GET /api/ships/history?from&to&bbox&limit`, `GET /api/ships/history/{mmsi}?from&to` | Sampled positions `{mmsi, t, lat, lon, sog, cog, cls}`, kept 72 h |
| alerts | `GET /api/alerts/recent?since=&limit=` | Fired alerts `{id, rule, area, title, time, severity, entity}` (the briefing uses these) |
| briefing | `GET /api/briefing/latest`, `GET /api/briefing/{id}` | Briefings `{id, time, period, sections: [{region, items}], text, ai}` |
| ai (helper) | Python `meridian.modules.ai.helper.complete(system, prompt, max_tokens=1200) -> str or None` | Text from the configured provider, or None when off |

Alerts must not register an events provider: alerts are derived from events and would feed back into convergences.

### 15.5 Later additions (Programme 2)
- **Deep links** (`web/js/core/deeplink.js`): the URL hash carries `map=zoom/lat/lon/bearing/pitch`, `l=` (layers on)
  and `e=kind:id` (open entity). Links never overwrite the saved layer set; turning on more than 8 layers asks first.
  `linkFor({entity})` and `copyLink()` build and copy a link to the current view.
- **Layers**: `ctx.layers.setOption(id, option, value)`, `ctx.layers.suspend(ids)` and `ctx.layers.resume(ids?)`.
- **Kinds**: `my_place` is browser-only (never stored as a favourite, never in server search, never sent to the
  assistant or MCP tools).
- **Alerts and AI**: sharing alerts with the assistant and MCP tools is off until the user turns it on in Alerts
  settings (watchlist names and places can be personal).
- **MCP bridge**: `tools/mcp_meridian.py` (stdio, loopback only) over `GET /api/ai/tools` and `POST /api/ai/tool`.
  See `docs/MCP.md`.
