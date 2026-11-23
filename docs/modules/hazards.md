# Hazards module

Live natural hazards and health events in the **Events** group: earthquakes, disaster alerts,
tropical storms, active fires, volcanoes, weather warnings, tsunami messages, natural events,
disease outbreaks, Copernicus emergency mapping activations, England flood warnings with river and
sea gauges, and the GloFAS flood outlook. Backend in `meridian/modules/hazards/`, browser code in
`web/js/layers/hazards.js` and `web/js/layers/hazards/`, styles in `web/css/hazards.css`.

The Programme 2 additions (Hazards+ team) live in their own files: `cems.py`, `ea.py` and
`glofas.py` (parsers), `mapping.py`, `floods.py` and `outlook.py` (services) and `plus.py`
(registration), with `mapping.js`, `floods.js` and `outlook.js` in the browser. They plug into the
module's scheduler through `Hazards.refreshers`, `detail_handlers` and `event_hooks`.

## Layers

| Layer id | Title | What it shows | Default |
|---|---|---|---|
| `hazards.quakes` | Earthquakes | USGS: past day (every size), past week (M2.5+) or past month (significant). Circles sized by magnitude, coloured by age, PAGER alert rings, magnitude labels. | On |
| `hazards.disasters` | Disaster alerts | GDACS alerts (earthquake, cyclone, flood, volcano, drought, wildfire) with green, orange and red levels; opening a card draws the affected area. | Off |
| `hazards.storms` | Tropical storms | Active cyclones: past and forecast track coloured by intensity, track points, cone of uncertainty, 60/90/120 km/h wind areas. NHC for the Atlantic and East/Central Pacific, GDACS elsewhere, NASA EONET as a last resort. | Off |
| `hazards.fires` | Active fires | NASA FIRMS detections of the past 24 h. Grid clusters below zoom 5.8 (2°, 1°, 0.5°, 0.25° cells), heat glow plus single detections closer in. Falls back to EONET wildfire events if FIRMS is unreachable, and says so. | Off |
| `hazards.volcanoes` | Volcanoes | Smithsonian GVP Holocene volcanoes as faint triangles; alert or erupting volcanoes (USGS HANS, EONET, GDACS) larger and coloured by level. | Off |
| `hazards.warnings` | Weather warnings | NWS alerts (storm polygons and forecast/county/marine zones) and MeteoAlarm warnings for 39 European countries (UK included), filled by severity; upcoming ones are lighter and dashed. | Off |
| `hazards.tsunami` | Tsunami messages | Latest bulletins from the US National and Pacific tsunami warning centres, kept as a 4-month history. | Off |
| `hazards.natural` | Natural events | NASA EONET open events: storms (with tracks), volcanoes, icebergs, floods, dust, landslides and wildfires of the past 60 days. | Off |
| `hazards.outbreaks` | Disease outbreaks | WHO Disease Outbreak News of the past 60 days, placed in the countries each report names, with country outlines. | Off |
| `hazards.mapping` | Emergency mapping | Copernicus EMS rapid mapping activations: a framed marker per activation (category glyph and colour; solid while open, faded once closed) and the areas of interest it maps (dashed outlines). Options: open only, or open plus closed in the past 30, 60 or 180 days. | Off |
| `hazards.floods` | Flood warnings (England) | Environment Agency flood alerts (yellow), flood warnings (orange) and severe flood warnings (red) with their flood-area outlines (a dot per area below zoom 8), and the river and sea gauges near them coloured by how the level compares with the normal range (from zoom 7; values from zoom 11). Options: alerts and warnings, warnings only, severe only; gauges on or off; warnings no longer in force. | Off |
| `hazards.flood_outlook` | Flood outlook | GloFAS flood summary for days 1 to 3, 4 to 10 or 11 to 15 (purple above a 20-year flood, red 5 to 20-year, yellow 2 to 5-year; darker is more likely), with the GloFAS reporting points (share of forecast runs above the flood level, trend triangles). Opacity option. A forecast signal, not a warning. | Off |

Entity kinds: `earthquake`, `fire`, `volcano`, `storm`, `flood`, `warning`, `outbreak`, `disaster`,
`tsunami`, `emergency_mapping`, `gauge`. Ids are `<source>:<native id>`: `usgs:us6000ucij`, `firms:N20:-34.40060:20.59863:202611211244`
(satellite, position, acquisition time), `eonet:EONET_26568`, `gdacs:FL:1103456`, `nhc:al142026`,
`gvp:332010`, `nws:<CAP id>`, `meteoalarm:<uuid>.<info>.<area>.<p|g><n>`, `tsunami:PAAQ:<uuid>`,
`who:2026-DON630`, `cems:EMSR936` (kind `emergency_mapping`), `ea:113WACT1B` (an England flood
warning, keyed by its flood area, kind `flood`) and `eagauge:1029TH` (an EA station, kind `gauge`).
GDACS types map to kinds EQ earthquake, TC storm, FL flood, VO volcano, WF fire,
DR disaster; EONET categories map the same way (ice, dust, landslides and the rest are `disaster`).

Every card opens from a bare `{kind, id}` (favourites, case files, search, AI) through
`/api/hazards/details`. Anything opened once is kept in `hazards_archive` for 180 days, so an
expired warning still opens with "Last seen". USGS, GDACS, EONET and WHO items are also fetched by
id when they have left the live feeds; a FIRMS id carries its own position and time.

Cards have a data block (magnitude/depth/felt/intensity; fire power/brightness/confidence; wind and
category; severity and validity; elevation and last eruption), badges, official links and, for
earthquakes, fires, storms, volcanoes, disasters and natural events, a "Compare imagery" action that
emits `imagery:compare` with sensible before/after dates (harmless without the space module).
Weather warning and disaster cards outline their area on the map while open. Right-click "What's
here?" gains a Hazards section (warnings in force at the point, nearby recent quakes, the nearest
volcano and fire detections within 15 km).

**Emergency mapping card** (`cems:<code>`): open or closed, category, the International Charter
activation when there is one, the linked GDACS alert level; block with time since activation, areas
of interest, products delivered and maps; the reason for the request, event and activation times,
countries, requester; the summary figures (roads, population, mapped extent); each area of interest
with its products (first estimate, delineation, grading, monitoring rounds), their state (delivered
when, or in production and expected when), satellites, the flooded or burnt area and a download link;
the GDACS event the activation names and same-hazard GDACS alerts nearby. Links: activation page,
story map, all products, Charter page, terms. Actions: "Show mapped area" (draws the observed flood or
burnt area from the newest delivered product of each area, simplified), zoom to the areas and
"Compare imagery". The areas of interest are outlined while the card is open. Activations marked
`sensitive` show no areas or mapped extent. GDACS disaster cards list the activations that name them.

**England flood warning card** (`ea:<area>`): the severity in plain words ("Flood warning. Flooding
is expected, immediate action required."), the official message, area description, river or sea,
county, last review and severity change times, Floodline and its quick dial code, and the gauges
near the area with their levels; the area is outlined. **Gauge card** (`eagauge:<ref>`): level,
top of the normal range, highest of recent years and reading time; a chart of the past two days
against the top of the normal range; record level; the warnings around it. Both link to GOV.UK
"Check for flooding". Right-click "What's here?" gains **Flood warnings near here** (warnings that
cover the point or lie within 5 km, and the three nearest gauges; shown only when there is a warning
nearby or a gauge above normal).

## Sources and licences

| Source id | Source | Licence / terms | Refresh |
|---|---|---|---|
| `hazards.usgs` | USGS earthquake GeoJSON feeds (`all_day`, `2.5_week`, `significant_month`), event detail feed, FDSN event service | US public domain | day feed 5 min and week feed 15 min while in use; week and month every 30 min otherwise |
| `hazards.firms` | NASA FIRMS 24 h global CSV files (NOAA-20, Suomi NPP, MODIS by default), or the FIRMS area API with `FIRMS_MAP_KEY` | NASA open data, cite NASA FIRMS | checked every 30 min while the layer is on (conditional GET, ETag) |
| `hazards.eonet` | NASA EONET v3 events | NASA open data | 15 min in use, 60 min otherwise |
| `hazards.gdacs` | GDACS `EVENTS4APP`, `SEARCH` (orange/red of the past 30 days), `MAP?eventtype=TC`, `geteventdata`, `getgeometry`, RSS | GDACS terms: free use with credit | 10 min in use, 30 min otherwise; RSS 30 min in use only |
| `hazards.nhc` | NHC `CurrentStorms.json`, NOAA `NHC_tropical_weather` ArcGIS service (cone, track, points) | US public domain | 10 min in use, 30 min otherwise |
| `hazards.gvp` | Smithsonian GVP GeoServer WFS (Holocene volcanoes) | non-commercial use with citation | dataset, monthly |
| `hazards.hans` | USGS HANS elevated volcanoes | US public domain | 30 min in use, 2 h otherwise |
| `hazards.nws` | `api.weather.gov/alerts/active` (`Accept: application/geo+json`, no `limit`) | US public domain | 5 min while the layer is on; hourly severe/extreme only otherwise |
| `hazards.nws_zones` | `api.weather.gov/zones/{type}/{id}` outlines | US public domain | once per zone, cached in SQLite for 6 months |
| `hazards.meteoalarm` | MeteoAlarm legacy Atom feeds per country, CAP messages for card text | CC BY 4.0 equivalent with MeteoAlarm redistribution terms; credit MeteoAlarm and the issuing service | 30 min while the layer is on (Germany and Switzerland hourly) |
| `hazards.meteoalarm_regions` | MeteoAlarm geocodes file and aliases (public GitLab documents repository) | MeteoAlarm terms | only when the warnings layer is used and the file name changes upstream |
| `hazards.tsunami` | tsunami.gov `PAAQAtom.xml`, `PHEBAtom.xml` | US public domain | 5 min in use, 10 min otherwise |
| `hazards.who` | WHO Disease Outbreak News OData API | WHO terms: non-commercial reuse with attribution | 6 hours |
| `hazards.naturalearth` | Natural Earth 1:110m countries (outbreak areas, country lookups) | public domain | dataset, 90 days |
| `hazards.cems` | Copernicus EMS rapid mapping dashboard JSON: `public-activations-info/?limit=60` (list) and `public-activations/?code=` (one activation); observed-event GeoJSON layers on `rapidmapping-viewer.s3.eu-west-1.amazonaws.com` on request | CEMS terms (read 23 Nov 2026): "users shall have a free, full and open access to Copernicus Service Information"; reproduction, distribution and communication to the public allowed, citing the source ("Copernicus Emergency Management Service (© 2026 European Union), EMSR936") | list every 30 min in use, 2 h otherwise; a detail when an open or recent (45 days) activation's `lastUpdate` changes |
| `hazards.ea_floods` | Environment Agency `flood-monitoring/id/floods` (conditional GET) and `id/floodAreas/{id}/polygon` | Open Government Licence v3 (in the API metadata); credit "this uses Environment Agency flood and river level data from the real-time data API (Beta)" | 15 min in use, hourly otherwise; each outline once (kept 60 days, then refreshed when warned again) |
| `hazards.ea_gauges` | `flood-monitoring/data/readings?latest&parameter=level` (one call for every level, as the API asks) | OGL v3 | 15 min, only while the layer is in use |
| `hazards.ea_stations` | `flood-monitoring/id/stations?parameter=level&_view=full` (positions, normal range, records) | OGL v3 | weekly, only once the layer is used |
| `hazards.glofas` | GloFAS WMS `ows.globalfloods.eu/glofas-ows/ows.py`: GetCapabilities (forecast date) and GetMap for `sumAL41EGE`, `sumAL42EGE`, `sumAL43EGE`, `reportingPoints` | CEMS-FLOODS datasets licence (PDF linked from the Early Warning Data Store, read 23 Nov 2026): free reproduction, distribution and communication to the public with the notice "Generated using Copernicus Emergency Management Service information 2026"; "the data does not constitute in any way a flood warning" | capabilities every 6 h while the layer is on; tiles on demand, cached 6 h |

Country names and label points for geocoding WHO reports are embedded in `countries_data.py`
(generated from Natural Earth 1:50m, public domain), so outbreaks map even when nothing else loads.

### Keys and settings

- `FIRMS_MAP_KEY` (secret, optional, free from FIRMS): uses the FIRMS area API; the public 24 h
  files are used without it, and the API falls back to the files if it fails. Never logged (the
  core redacts it from errors).
- `HAZARDS_FIRMS_SATELLITES` (default `noaa20,snpp,modis`; also `noaa21`). Each VIIRS file is about
  8 MB a day. Suomi NPP currently publishes an empty file; the layer says so.
- `HAZARDS_METEOALARM_COUNTRIES` (default all 39 feeds; feed names or ISO codes, e.g. `GB,IE,FR`).
- `HAZARDS_BACKGROUND` (default on): see below.

## Background polling (why some sources run without a browser)

Each source has a task that ticks every 20 to 30 s and decides for itself whether it is due: fast
while any of its layers was requested in the last 15 minutes (`m.touch("hazards.<layer>")` in the
handlers), slow otherwise. The slow, idle refresh keeps the **events provider** current for
convergences and the AI assistant even when no browser is open: USGS week and month feeds, GDACS,
NHC, EONET, HANS, severe/extreme NWS alerts, tsunami messages (which only ever show the latest
bulletin, so the history needs continuous polling) and WHO. FIRMS, MeteoAlarm, full NWS alerts and
NWS zone outlines are fetched only while their layer is in use. Set `HAZARDS_BACKGROUND=0` to stop
all idle polling.

The additions follow the same rule. Idle (for events, convergences and the AI): the Copernicus EMS
list every 2 hours and the Environment Agency warnings list hourly, with outlines fetched only for
flood warnings and severe warnings so their events have a position. In use: the EMS list every 30
minutes; the EA warnings every 15 minutes (the API is updated every 15 minutes) with outlines for
alerts too, fetched as soon as the layer is opened; the latest EA levels every 15 minutes (one
`readings?latest` call, as the API's crawling note asks, about 80 kB gzipped; conditional GETs answer
304 when nothing changed); the EA station list weekly (0.7 MB gzipped); the GloFAS forecast date
every 6 hours (150 kB). GloFAS tiles go through the server, which spaces requests (0.1 s per host),
caches 3,000 tiles for 6 hours and serves a blank tile when GloFAS fails. The mapped extent of an
activation (up to four GeoJSON layers, often about 1 MB each) is fetched only when a card asks for it
and kept 6 hours.

Approximate traffic (compressed where the server supports gzip): idle up to about 15 MB a day; with the
default layer only up to about 25 MB a day; fires on adds about 9 MB per FIRMS file change (files are
regenerated roughly hourly, unchanged files answer 304); weather warnings on add about 7.6 MB per
MeteoAlarm round (no gzip or ETag there) every 30 to 60 minutes, plus a one-off 31 MB geocodes file
and about 1 kB per NWS zone the first time it appears. Failing sources back off (doubling up to an
hour) and `Retry-After` is honoured by `m.net`. Per-host spacing is set for every host.

Memory: the FIRMS store keeps detections in column arrays (about 5 MB for 110,000 detections plus
grid aggregates); simplified zone and region outlines a few MB; everything else is small.

## Endpoints

All GET, JSON, under `/api/hazards/`. Layer endpoints answer GeoJSON FeatureCollections with a
`meta` object (counts, source state, notes); unchanged answers carry an ETag.

| Path | Parameters | Returns |
|---|---|---|
| `quakes` | `window=day|week|month`, `min_mag` | quake points (`id, mag, t, d, alert, tsu, felt, place, sig, type, mt`) |
| `fires` | `bbox`, `zoom`, `conf=0|1|2` | grid cells (`kind: cell, n, f, s, t, w, so, size`) or detections (`kind: fire, id, f, b, c, s, t, nt`); `meta.mode` is `grid`, `points`, `fallback` or `pending` |
| `natural` | `cats` (EONET category ids) | event points and storm tracks |
| `disasters` | `types` (EQ,TC,FL,VO,DR,WF), `min_alert=1..3` | GDACS event points (`alert, rank, cur, short, sev`) |
| `disaster-geometry` | `id=gdacs:<TYPE>:<eventid>` | simplified GDACS outline FeatureCollection |
| `storms` | `ended=1` to include storms without a recent advisory | cones, wind areas, tracks, points, current positions; `meta.storms` |
| `volcanoes` | `scope=all|active` | volcano points (`a` = activity 0..4) |
| `warnings` | `bbox`, `zoom` (below 5 simplified outlines), `min=0..4`, `src=nws,meteoalarm` | warning areas (`id, ids, n, src, sev, ev, lvl, area, up, exp, on`) |
| `tsunami` | `days=1..90` | message points |
| `outbreaks` | `days=7..365` | report points and country outlines |
| `details` | `kind`, `id` | `{kind, id, live, type, entity, data, last_seen}` for cards |
| `here` | `lat`, `lon` | warnings at the point, nearby quakes, nearest volcano, fire count |
| `recent` | `hours`, `kinds`, `bbox`, `min_severity`, `limit` | same as the `hazards_recent` AI tool |
| `summary` | | counts and the state of every source (`plus` has the emergency mapping, England flood and outlook counts) |
| `mapping` | `days=1..365` (default 60), `scope=recent|open` | activation points (`id, code, name, cat, category, open, t, age, label, gd`) and areas of interest (`role: aoi`) |
| `mapping/extent` | `id=cems:<code>` | the mapped flood or burnt area as simplified polygons (`role: extent`); `meta.note` when none |
| `floods` | `zoom` (below 9 simplified outlines, England-wide; from 9 detailed outlines for `bbox`), `min=1..3`, `gauges=0|1`, `ended=0|1` | areas (`role: area`, `lvl` 1 to 4), label points and gauges (`role: gauge`, `v`, `st`, `tr`, `sea`); `meta.counts`, `gauges_high`, `outlines_pending` |
| `floods/here` | `lat`, `lon` | warnings covering or within 5 km of the point, the three nearest gauges with fresh levels |
| `floods/gauge-history` | `id=eagauge:<ref>`, `hours=6..120` | `{points: [{t, v}], unit, typical_high, typical_low, recent_high, record_high}` |
| `flood-outlook` | | forecast date, products, legend, credit, recent tile errors |
| `flood-outlook/tile/{product}/{z}/{x}/{y}` | `product` = `days1-3`, `days4-10`, `days11-15` or `points`; `z` 0 to 10; `t` = forecast date | a 256 px PNG (`max-age` 6 h; a blank tile with `no-store` when GloFAS fails) |

Discovery: `m.search` (volcano names, storm names, outbreak diseases, quake places, GDACS events,
EONET events; activation codes, names, countries and categories; England flood areas by name, river,
county or description; gauges by station, river or town), `m.nearby` (quakes, volcanoes, strongest
fires, GDACS, storms, warnings at the point, tsunami messages, outbreaks in the country; activations
within 100 km or whose area of interest covers the point; England warnings within 25 km and gauges
within 15 km), `m.events` (CONTRACT 5.6 scale: quakes M4.5+ or with a
PAGER alert, fire clusters of 0.5° cells, GDACS non-green alerts and green floods, volcano alerts,
active storms, severe and extreme warnings, tsunami messages, outbreak reports, other EONET events;
emergency mapping activations at their activation time, severity 5, 6 with a Charter activation or
an orange GDACS alert, 7 with a red one; England flood warnings severity 6 and severe flood warnings
severity 9, timed by their last review with `first_time` the severity change. Flood alerts are not
events).

AI tools: `hazards_recent` (bbox, kinds, hours, min_severity, limit), `earthquakes` (min_magnitude,
bbox, region, hours, limit; windows over a week or below M2.5 query the USGS FDSN service),
`weather_warnings_at` (lat, lon; checks NWS live for US points), `flood_warnings` (area as a place,
county or river name, or bbox, or lat, lon and radius_km; min_severity severe, warning, alert or
ended; include_gauges; limit: England warnings with severity in plain words, message, times and
nearby gauge levels) and `emergency_mapping` (days, country, category, open_only, code, limit:
activations with areas of interest, product states, summary figures and the linked GDACS event).

## Storage

`hazards_archive` (cards opened, 180 days, at most 20,000 rows), `hazards_tsunami` (tsunami message
history, 4 months), `hazards_nws_zones` (zone outlines, 6 months), `hazards_cems` (parsed activation
records, 400 days), `hazards_ea_areas` (simplified flood-area outlines, 240 days, a few kB each),
`hazards_plus_state` (the last EMS list and EA warnings, so the layers show at start-up). Files:
`data/build/hazards/` (`volcanoes.json`, `countries.json`, `meteoalarm_regions.json`,
`ea_stations.json` about 1 MB), `data/raw/hazards/` (FIRMS CSVs,
GVP, Natural Earth and MeteoAlarm downloads, about 45 MB).

## Known gaps

- Suomi NPP's 24 h file is empty at the moment; NOAA-20 and MODIS carry the fire layer. NOAA-21 can
  be added with `HAZARDS_FIRMS_SATELLITES`.
- MeteoAlarm feeds have no gzip or ETag, so the warnings layer costs bandwidth; their newer EDR API
  needs a token (an optional key could replace the Atom feeds later). Germany publishes thousands of
  minor (frost, fog) warnings, hidden below "Moderate and up" by default.
- Some MeteoAlarm countries (for example Andorra, Cyprus, Luxembourg) had no warnings at test time,
  so their geocode coverage is untested.
- The Smithsonian weekly activity report RSS answers 403 here; GVP photos refuse cross-origin
  embedding, so cards link to the GVP page instead of showing the photo.
- WHO reports that name no country ("Global") are listed in search but not mapped.
- NHC cones are drawn only while NHC has active storms (none during testing; the code path is
  covered by synthetic fixtures). JTWC products are used through GDACS.
- GDACS cyclone point labels are dates without a year; the year nearest the advisory is used.
- Flood warnings cover England only. Scotland (SEPA) and Wales (Natural Resources Wales) publish
  their own warnings; the research report did not test them, so they are not read yet (gap).
- The EA gives warning times without a time zone. They are read as UK civil time (equal to UTC in
  winter, one hour ahead in summer); the documentation does not say, so summer times may be an hour
  off if the API in fact sends UTC.
- Gauges near a warning are stations inside the flood area or within 3 km of it (at most 12 per
  area); stations without a published normal range show "no normal range". Trends need two polls,
  so they appear 15 minutes after the layer is opened.
- The Copernicus EMS JSON is the backend of the public dashboard, not a documented API: every field
  is optional and the layer falls back to the last good list. Only the latest 60 activations are
  listed (about five months); older ones open by code (search, favourites, AI). Product download
  links are ZIP archives; the maps themselves are inside them.
- GloFAS is raster only here: its reporting points are not queryable (no GetFeatureInfo, KML
  fails), so there are no cards for forecast points. Signals are a few pixels wide at world scale;
  the reporting points carry the overview. WMS symbols can be cut at tile edges.
