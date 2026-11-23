# Space module (`space`, port 8795)

Satellites and the ISS, orbital launches, rain radar, NASA GIBS weather and imagery layers (with the
Space+ extension pack: NO2, SO2, sea ice, daily night lights, flood maps, Sentinel-1 radar), Meteosat
imagery and lightning (EUMETSAT EUMETView), point weather, a before/after imagery compare tool, a
satellite scene finder (STAC catalogues), and space weather with the aurora forecast (NOAA SWPC).
Backend in `meridian/modules/space/`, browser code in `web/js/layers/space.js` and
`web/js/layers/space/*.js`, styles in `web/css/space.css`.

## Layers

| Layer id | Group | What it shows | Refresh |
|---|---|---|---|
| `space.satellites` | Space | Active satellites by CelesTrak group, moving live, coloured by family; click for orbit, ground track (±1 orbit) and footprint | positions every 1 s (≤ 2,500 named satellites), Starlink swarm every 5 s; elements every 2 h |
| `space.iss` | Space | ISS icon, next orbit (solid), last orbit (dashed), area that can see it | every second |
| `space.launches` | Space | Upcoming (and last week's) orbital launches at their pads; outer ring = within 24 h; red ring = failed | list hourly, labels every minute |
| `space.aurora` | Space | OVATION chance of seeing the aurora overhead, both hemispheres, as server-drawn palette PNG tiles (zoom 0 to 3, enlarged beyond); solid line where it may be seen overhead (10% or more), dashed line about 330 km further from the pole where it may be seen low on the horizon; works on the globe | 5 min (OVATION cached 10 min) |
| `space.spaceweather` | Space | Not a map layer: opens the "Space weather now" drawer (also the toolbar button "Space weather") | 2 min while open |
| `space.radar` | Weather | RainViewer radar, past 2 h in 10-min frames, with a drawer to play, step and scrub | frame list every 5 min |
| `space.precip` | Weather | GPM IMERG precipitation rate, 30-min, about 5 h behind (GIBS `IMERG_Precipitation_Rate_30min`, Level6, png) | 10 min |
| `space.clouds` | Weather | Cloud-top infrared, 10-min: GOES-East, GOES-West and Himawari band 13 (GIBS, Level6, png) plus Meteosat FCI 10.5 µm (EUMETView WMS, `mtg_fd_ir105_hrfi_style_02`) over Europe, Africa and the Atlantic. Each disc draws one share of the overlaps, split on tile edges at 112.5° W, 45° W and 67.5° E (two half-transparent tiles over one place would blend into a seam) | 10 min |
| `space.sst` | Weather | GHRSST MUR sea surface temperature, daily (Level7, png) | 3 h |
| `space.lst` | Weather | MODIS Terra land surface temperature, day (Level7, png) | 3 h |
| `space.aod` | Weather | MODIS combined value-added aerosol optical depth (Level6, png); only requested within 72° of the subsolar latitude for the day shown | 3 h |
| `space.snow` | Weather | MODIS Terra NDSI snow cover (Level8, png) | 3 h |
| `space.no2` | Weather | Sentinel-5P TROPOMI tropospheric NO2 (`TROPOMI_L2_Nitrogen_Dioxide_Tropospheric_Column`, Level6). About ten days behind at GIBS (newest 13 Nov on 23 Nov), and the newest listed day is still incomplete, so it opens one day earlier (`settle`) | 3 h |
| `space.so2` | Weather | OMPS NOAA-20 SO2; variants low plumes (`OMPS_NOAA20_SO2_Lower_Troposphere`) and high plumes (`..._Upper_Troposphere_and_Stratosphere`), Level6, latest complete day. Only values above about 1 DU are drawn; `raster-brightness-min` 0.2 lifts the dark blue low end off the navy map (the legend shows the lifted colours) | 3 h |
| `space.seaice` | Weather | GHRSST MUR sea ice concentration (`GHRSST_L4_MUR_Sea_Ice_Concentration`, Level7), about a day behind; open water is transparent | 3 h |
| `space.lightning` | Weather | Meteosat Lightning Imager accumulated flash area (EUMETView `li_afa`), 5-min frames about 20 min behind; "Last 30 minutes combined" stacks six frames | 5 min |
| `space.daily` | Imagery | True colour for a chosen day: MODIS Terra, Aqua; VIIRS NOAA-20, NOAA-21, Suomi NPP (Level9, jpg) | 3 h |
| `space.nightlights` | Imagery | VIIRS Black Marble 2016 (Level8, png) | static |
| `space.nightdaily` | Imagery | Night lights, daily, "useful for spotting blackouts": NOAA-20 VIIRS, variants moonlight corrected with cloud gaps filled (`VIIRS_NOAA20_GapFilled_BRDF_Corrected_DayNightBand_Radiance`, the default) or as seen that night (`VIIRS_NOAA20_DayNightBand_At_Sensor_Radiance`), Level8, opens at the latest complete night. Gap filling can hide a blackout under cloud; the raw version includes cloud, moonlight and aurora | 3 h |
| `space.flood` | Imagery | VIIRS flood map (NOAA-20 and NOAA-21), variants 2-day (default), 1-day and 3-day composites (`VIIRS_Combined_Flood_*`, Level9): flood, recurring flood, normal water, cloud | 3 h |
| `space.sar` | Imagery | Sentinel-1 radar backscatter, false colour (`OPERA_L2_Radiometric_Terrain_Corrected_SAR_Sentinel-1`, Level12). Each day holds only that day's passes, so it opens with the last six days combined (six sources, newest on top); "Latest day only" and earlier days are options. Seen on every continent on 20 and 22 Nov | 3 h |
| `space.meteosat` | Imagery | Meteosat Third Generation full disc (0°) from EUMETView WMS, 10-min frames about 20 min behind; views GeoColour (`rgb_geocolour`, default), infrared (`ir105_hrfi`, grey) and dust (`rgb_dust`) | 10 min |
| `space.scenes` | Imagery | Not a raster: opens the satellite scene finder (also the toolbar button "Scenes") and draws scene footprints (Sentinel-2 `#199e70`, Sentinel-1 `#d95926`, Landsat `#9085e9`) | on request only |

Every GIBS identifier, matrix set, format and time range was checked against the EPSG:3857 "best"
GetCapabilities (22 Nov 2026; the extension pack on 23 Nov 2026, with test tiles for each layer). The
latest available time of each GIBS layer comes from GIBS DescribeDomains
(`/wmts/epsg3857/best/1.0.0/{layer}/default/{matrix}/all/{start}--{end}.xml`, a few hundred bytes), so
date options never point at days that do not exist yet. Meteosat frames come from the EUMETView
GetCapabilities time dimension (`start/end/PT10M` or `PT5M`, about 12 KB, cached 10 min, one request
at a time). Daily swath products (true colour, night lights, SO2) open at the latest *complete* day;
5- and 10-minute geostationary products open one frame before the newest, because GIBS and EUMETView
list the newest frame before every tile is rendered.

The catalogue (`imagery.py`) supports per-product `variants` (one layer set each: the "Plume
height", "Version", "Window" and "View" options), a `composite` of the newest N times (radar six days,
lightning six frames), `settle` (open N days before the newest listed day), extra raster `paint`,
`reference` (borders and names above opaque imagery) and per-layer `layer_bounds` and
`layer_maxzoom`. Layer ids starting with `eumetsat:` are EUMETView WMS layers; the browser loads their
GetMap tiles directly with MapLibre's `{bbox-epsg-3857}` template (EUMETView sends
`Access-Control-Allow-Origin: *` and `Cache-Control: max-age=604800` for a fixed time). The generic
browser layer is `web/js/layers/space/gibs.js`; each layer fetches only its own product's times.

All rasters sit at the bottom of the core `polygons` slot, ordered among themselves (true colour,
night lights 2016, daily night lights, radar, Meteosat, flood, SST, sea ice, LST, snow, aerosols,
NO2, SO2, clouds, precipitation, RainViewer radar, lightning, aurora from bottom to top), so every
other module's areas, lines and points draw on top. Opaque imagery (true colour, night lights,
radar, Meteosat) gets copies of the basemap's own vector borders and place names above it.

Layer options: groups (one toggle per CelesTrak group, Starlink is a separate heavy toggle) and names
at zoom 5 for satellites; look-ahead, recent launches and labels for launches; opacity, snow colours
and smoothing for radar; opacity, the variant where there is one, and "Showing" (latest, combined
where offered, hours or days earlier, or a day you choose for daily products) for the GIBS and
Meteosat layers; satellite, day (latest complete, earlier, or any day you choose), opacity, borders
and place names for true colour. The layer row's count shows the actual date or time on screen (a
range for combined views, for example "18 Nov to 23 Nov").

## Cards, tools and browser integration

- Satellite card (`kind: satellite`, id = NORAD number): data block (altitude, speed, inclination,
  period), orbit class badge (LEO, MEO, GEO, HEO), sunlight or Earth's shadow, stale-elements badge,
  position, footprint radius, perigee and apogee, eccentricity, NORAD id, international designator,
  launch year, CelesTrak groups, element epoch and age, modules and docked vehicles (for the ISS and
  Tiangong; co-located objects are hidden from the map and listed here), passes over the map centre,
  Follow (re-centres every 6 s), links to the CelesTrak catalogue entry and Heavens-Above. Live
  every 2 s. Bare `{kind, id}` entities (favourites, case files) are fetched by number; objects
  outside the loaded groups are looked up once at CelesTrak and drawn as extras.
- ISS card: as above, plus visible passes over the map centre for the next 72 hours.
- Launch card (`kind: launch`, id = `ll2:<uuid>`): status badge, countdown block, window, date,
  chance, launch image (credit The Space Devs), a ticking T-minus clock, window, hold and failure
  reasons, mission, orbit, programme, crew, provider, pad, other launches from the same pad, webcast
  and info links.
- Pad card (`kind: launch_pad`, id = `ll2pad:<id>`): next launch, total launches, upcoming and recent
  launches from the pad.
- What's here: "Weather now" (Open-Meteo current conditions, 24-hour temperature line and
  precipitation columns with a crosshair readout, keyboard steppable, plus an hourly table) and
  "Next ISS passes over here" (visible passes in 72 hours). Weather is placed first.
- Compare imagery: toolbar button "Compare" and `ctx.bus.emit('imagery:compare', {lat, lon, zoom,
  before, after, source?})` (or `{lat, lon, date}`, which compares date −3 days with date +1 day).
  Full-screen modal with two synchronised MapLibre maps, side by side or with a draggable (and
  keyboard) swipe divider, date pickers, imagery choice, borders and place names, swap. The imagery
  choice holds true colour (`terra`, `aqua`, `noaa20`, `noaa21`, `snpp`) and other views:
  `nightdaily` (night lights, for blackouts; aliases `nightlights`, `night`, `blackout`), `flood`,
  `sar` (each side combines the six days up to its date; alias `radar`) and `no2`. Overlay products
  (flood, radar, NO2) get the basemap's own dark land and water fills underneath. Each product has its
  own earliest date and a credit line in the footer. From the toolbar it compares the latest complete
  day with a week earlier, centred on the current view. Dates after the latest available day are
  clamped, with a note; when changing the source clamps both dates onto the same day (NO2 ends
  days earlier than true colour), Before moves a week before After. Both maps are removed on close. Other modules can pass `source`, for example
  `nightdaily` on a conflict card or `flood` on a flood card.
- Scene finder: see the section below; What's here gains "Latest satellite scenes here" (after the
  weather section).
- Search: satellites by name, NORAD number or international designator (word starts; aliases ISS,
  Tiangong, Hubble), launches by mission, rocket or provider, pads by name. Client-side providers
  cover loaded data; the server covers every cached group and both launch lists.
- Colours: satellite families use three categorical hues validated all-pairs for colour-vision
  deficiency on the dark map (dataviz validator, dark mode, surface `#111a2c`): navigation `#d95926`,
  Earth observation and science `#199e70`, other `#9085e9`; crewed stations are chart paper and
  always labelled. Weather chart: temperature `#d95926`, precipitation `#3987e5` (validated on the
  card surface).

## Satellite scene finder (Space+)

Code: `meridian/modules/space/scenes.py` (STAC queries, parsing, cache, quicklook fetcher),
`web/js/layers/space/scenes.js` (drawer, footprints, What's here). The latest Sentinel-2, Sentinel-1
and Landsat scenes over a point or a small box, from keyless STAC catalogues:

| Mission | Catalogue (STAC item search) | Quicklook | Viewer links |
|---|---|---|---|
| Sentinel-2 L2A | Element 84 Earth Search `earth-search.aws.element84.com/v1`, collection `sentinel-2-l2a`, cloud filter with the `query` extension | `preview.jpg` beside the scene on the public `sentinel-cogs` bucket (343 px JPEG, 13 to 45 KB) | Copernicus Browser at the place and day (`datasetId=S2_L2A_CDAS`), catalogue record |
| Sentinel-1 GRD | Copernicus Data Space Ecosystem `stac.dataspace.copernicus.eu/v1`, collection `sentinel-1-grd` (COG items) | the product thumbnail from `datahub.creodias.eu`, which redirects to `zipper.creodias.eu` (297 by 190 PNG, about 96 KB) | Copernicus Browser (`datasetId=S1_CDAS_IW_VVVH` for IW scenes), catalogue record |
| Landsat C2 L2 | Microsoft Planetary Computer `planetarycomputer.microsoft.com/api/stac/v1`, collection `landsat-c2-l2`, CQL2 `filter` on cloud and the `fields` extension (1.7 KB per item) | Planetary Computer's rendered preview as a 360 px JPEG (about 37 KB) | USGS LandsatLook record (STAC browser), Planetary Computer viewer, catalogue record |

- Search area: a view no larger than 3 by 3 degrees is searched as a box ("In this view"); anything
  larger, or a view across the antimeridian, at its centre point. What's here searches its point.
- Defaults: the last 30 days, up to 60% cloud (radar has no cloud filter), four scenes per mission,
  newest first. Options in the drawer: missions, cloud (any, 60, 30, 10%) and period (14, 30, 60, 120
  days), remembered in the browser.
- Opening: the toolbar button "Scenes" (or the `space.scenes` layer row) searches the current view;
  "Search here" searches again after moving the map. What's here shows "Latest satellite scenes here"
  with a "Find scenes" button; it searches only on that click and can open the full finder.
- Each row: quicklook (click to enlarge, with the credit and catalogue name), satellite, time in UTC,
  cloud cover or the radar mode, tile, orbit direction or path and row, how long ago, "Show footprint"
  (highlights and fits the map) and the official viewer link. Hovering a row highlights its outline;
  clicking an outline on the map selects its row.
- Quicklooks are shown as delivered, small and unmodified, with "Contains modified Copernicus Sentinel
  data 2026" or "Landsat imagery courtesy of the U.S. Geological Survey" and the catalogue's name.
  Radar quicklooks are in the satellite's viewing geometry (they can look mirrored or turned), which
  the enlarged view says. Full scenes are never downloaded.
- London on 23 Nov 2026 (30 days, up to 60% cloud): 11 scenes. Sentinel-2C 21 Nov 11:16 UTC (34%
  cloud, tile 30UXC), Sentinel-2A 20 Nov (41%), Sentinel-2B 16 Nov (1%), Sentinel-2A 13 Nov (1%);
  Sentinel-1C 21 Nov 06:06 UTC (IW, VV+VH, descending), Sentinel-1D 20 Nov, Sentinel-1D 18 Nov
  (ascending), Sentinel-1C 17 Nov; Landsat 9 18 Nov (58%, path 201, row 24), Landsat 9 2 Nov (57%),
  Landsat 8 25 Oct (7%). Search 0.3 s; quicklooks 0.6 to 1.5 s the first time, 10 ms from the cache.
- Safety: the quicklook route takes a mission and a scene id (`^[A-Za-z0-9][A-Za-z0-9_.-]{4,150}$`),
  never a URL. The server takes the image address from its own search results or, after a restart,
  from the catalogue's item record; it fetches only https URLs on that mission's allowlisted hosts,
  checks each redirect hop itself (at most three) and the JPEG or PNG magic bytes, and caps images
  at 3 MB.

## Meteosat (EUMETSAT EUMETView, Space+)

Code: `meridian/modules/space/meteosat.py`. Layers: Meteosat inside `space.clouds`, `space.lightning`
and `space.meteosat`. Service: `https://view.eumetsat.int/geoserver/mtg_fd/wms` (WMS 1.3.0, EPSG:3857,
workspace `mtg_fd`, the 0° MTG-I full disc). Capabilities on 23 Nov 2026: 13 layers, among them
`ir105_hrfi` (10.5 µm, styles grayscale, style_01, style_02), `li_afa` (lightning, PT5M),
`rgb_geocolour`, `rgb_dust`, `rgb_truecolour`, `frp` and `h40b`; AccessConstraints "none", Fees "none".

**Licence verdict: allowed, no registration.** The EUMETSAT Data Policy (last amended 25 November
2025, PDF linked from `www.eumetsat.int/legal-framework/data-policy`, read 23 Nov 2026):

- Article 4 lists, as "Core" Meteosat data, "All SEVIRI, FCI, IRS and LI Advanced Image Products"
  (and "All SEVIRI, FCI, IRS and LI Derived Products").
- Annex II: "Advanced Image Product: the combination of different channels allocating a colour to
  each channel (i.e. RGB), visualisations of individual channels (e.g. used in Web Map Services) or
  the mathematical blending of several image layers. These (typically Level 2) products do not
  contain the original numerical data."
- Article 5.1: "Access to Core Data and Products is granted to all users world-wide on a Free and
  Unrestricted basis under a CC-BY-4.0 licence, regardless of when and how these are made available
  to the user."
- Article 6.1: "Users may redistribute all Core Data and Products." Article 6.3: "Users are required
  to include the following attribution to EUMETSAT in relation to EUMETSAT Data and Products, adapted
  as appropriate: "[Contains modified] EUMETSAT [Meteosat/Metop] [data/product] [Year of publication
  or distribution]"."
- Even for the non-Core "Recommended" Level 1 data, Article 12.2 gives personal users access
  "Without Charge" in real time, with no redistribution and no operational or commercial use.

The EUMETView images are visualisations of channels and RGBs served by a Web Map Service, so they are
Core: free and unrestricted under CC BY 4.0. Meridian shows them unaltered apart from map resampling
and opacity, credits "EUMETSAT Meteosat product 2026 (CC BY 4.0)" in the map attribution and the
layer source text, and never touches the numerical data.

## Space weather and the aurora (NOAA SWPC)

Code: `meridian/modules/space/spaceweather.py` (feeds, parsers, plain-language alerts, events, routes,
AI tool), `aurora.py` (OVATION grid, tiles, viewing lines, darkness, "aurora chance here tonight"),
`cgm.py` (a NASA CGM latitude table), `web/js/layers/space/spaceweather.js` (drawer, event cards) and
`web/js/layers/space/aurora.js` (layer, What's here section).

- **Space weather now** (drawer from the toolbar button or the `space.spaceweather` layer row): a
  plain summary; a data block (Kp now, solar wind speed, Bz, X-ray class); the NOAA G, S and R levels
  now and in the past 24 hours with SWPC's 3-day forecast (G level, S1+, R1 to R2 and R3+ chances);
  a Kp chart, 36 hours observed and 3 days forecast, bars coloured by storm level (amber G1 to G2,
  red G3+, each storm run labelled once at its peak) with a readout on hover, arrow-key stepping and
  a table view; SWPC alerts, watches and warnings in force, each in plain words with its validity,
  its aurora line and the original message, plus the past week; solar wind at L1 with 60-minute speed
  and Bz sparklines and the arrival time at Earth; the X-ray level and C-class-and-above flares with
  their R level; the OVATION maximum; links to the official SWPC pages (www.spaceweather.gov, where
  www.swpc.noaa.gov now redirects). Colours: observed Kp in the Space lavender `#a08ff5`, forecast
  `#6b60b6` (validated against it on the drawer surface `#16213a`), storms in the alert colours.
- **Alerts in force**: warnings inside their validity window (extensions and cancellations applied),
  the newest watch until the end of its last storm day (each watch supersedes earlier ones), alerts
  and summaries from the last 24 hours. Geomagnetic messages show the highest K alert and warning only.
- **What's here: Aurora chance here tonight** (polar and mid latitudes; hidden where the aurora needs
  more than Kp 9 and no G4 is forecast): one sentence plus OVATION now at the point and within 330 km
  towards the pole, the next dark window (Sun 12 degrees below the horizon, from the module's own sun
  maths; in the morning it looks ahead to the coming night), magnetic midnight in local time,
  Open-Meteo cloud cover over the dark hours (the cache the weather section already filled), the Kp
  forecast for those hours and the Kp needed here.
- **Tonight's rule of thumb**: SWPC puts the equatorward edge of the oval near 66 - 2 x Kp degrees of
  geomagnetic latitude; seen low on the horizon 3 degrees (about 330 km) further. Magnetic latitude is
  corrected geomagnetic (CGM) from a table of NASA's CGM model (`cgm.py`: 2.5 by 10 degree grid for
  each hemisphere, epoch 2026, fetched once from OMNIWeb while building the module). The centred
  dipole is 6 degrees out over Western Europe (London 53.4 dipole, 47.2 CGM), which is three Kp
  levels. Results: London needs about Kp 8 for the horizon, Edinburgh 5, Oslo 3, Minneapolis 5,
  Tromso and Fairbanks any Kp.
- **Events** (`m.events`, kind `space_weather`): G3+ geomagnetic storms (observed Kp, K-index alerts
  of 7+, the current G scale; one per episode, id `swpc:gstorm:YYYYMMDD`, severity G3 6, G4 8, G5 9);
  X-class flares, which are the R3+ radio blackouts (flare list, latest flare and X-ray summaries; id
  `swpc:flare:YYYYMMDDTHHMM` by start time; X1 5, X5 6, R4 7, R5 9; placed under the Sun at the
  peak); S2+ radiation storms (proton alerts and the current S scale; S2 4, S3 6, S4 8, S5 9); G3+
  storm watches while in force (4, 6, 7). Storms, radiation storms and watches are placed at the north
  geomagnetic pole (80.8 N, 72.7 W). Every event opens a card, also from favourites and case files.
- **AI tool** `space_weather_now {lat?, lon?}`: summary, Kp now, last 3 hours, past day, forecast
  maximum and daily maxima, NOAA scales now, past day and forecast, X-ray level and recent flares,
  solar wind, alerts in force, OVATION maxima; with a point, the aurora chance there tonight.
- **Search**: "aurora", "northern lights", "kp", "space weather", "solar flare", "geomagnetic storm"
  and similar open the aurora layer or the drawer.

## Backend endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/space/groups` | CelesTrak groups with title, family, count, age, error |
| `GET /api/space/satellites?group=<id>` | `{group, info, fields, sats: [[id, name, cospar, epoch, mm, ecc, inc, raan, argp, ma, bstar, mmdot, mmddot]]}`; cached gzip + ETag; fetches the group on first use |
| `GET /api/space/satellite/{norad}` | elements row, groups, family, orbit (period, perigee, apogee, inclination, eccentricity, class), current position |
| `GET /api/space/iss` | ISS now: lat, lon, alt_km, speed, sunlit, footprint radius, element age, orbit |
| `GET /api/space/passes?lat&lon&norad=25544&hours=48&min_el=10&visible=1` | passes with rise, max and set (time, ISO, azimuth, compass), duration, visibility window, magnitude (ISS) |
| `GET /api/space/launches?which=all\|upcoming\|previous` | launch records with phase and tone |
| `GET /api/space/launch/{uuid}` | one launch plus `same_pad` |
| `GET /api/space/pad/{id}` | pad plus its launches |
| `GET /api/space/weather?lat&lon` | Open-Meteo current, 24 hourly steps, summary, timezone |
| `GET /api/space/radar` | RainViewer host, frames (time, path), max zoom 7, colour scheme 2, attribution |
| `GET /api/space/gibs[?products=a,b][&meta=1]` | imagery products (GIBS and EUMETView) with per-layer provider (`gibs`, or `eumetsat` with a WMS spec), available times, latest, default and bounds, plus variants and the product default time; `meteosat_credit`; at most 30 products per call (`meta=1`: titles, variants and legends only, instant, plus the compare product list) |
| `GET /api/space/scenes?lat&lon` or `?bbox=w,s,e,n`, optional `days=30&max_cloud=60&missions=sentinel-2,sentinel-1,landsat&limit=4` | `{point, bbox, days, max_cloud, missions: {id: {ok, error, cached, scenes}}, scenes (all, newest first), credits}`; each scene `{key, mission, id, platform, time, datetime, cloud, detail, bbox, geometry, covers_point, quicklook, links}`; 400 for bad arguments, 502 only when every catalogue fails |
| `GET /api/space/scenes/quicklook/{mission}/{scene id}` | the scene's quicklook (JPEG or PNG), `max-age=86400`; 400 for an unknown mission or a malformed id, 404 for a scene the catalogue does not know, 502 when the image cannot be fetched |
| `GET /api/space/spaceweather` | the drawer's snapshot: `kp {now, last, max_24h, series, forecast_max, days}`, `scales {now, max_24h, forecast}`, `xray`, `wind`, `alerts {active, recent}`, `aurora`, `summary`, `links`, `feeds` (age and error per feed); 502 only when every feed is down |
| `GET /api/space/spaceweather/event/{id}` | one space weather event by id (404 once SWPC's lists no longer hold it) |
| `GET /api/space/aurora` | OVATION times, maxima, edge ranges, legend, versioned tile URL template and the viewing lines (GeoJSON) |
| `GET /api/space/aurora/tiles/{z}/{x}/{y}.png?v=` | 256 px palette PNG of the probability field, z 0 to 6 (the layer uses 0 to 3); `max-age=600` for the current version |
| `GET /api/space/aurora/here?lat&lon` | the "Aurora chance here tonight" data and sentence |

Errors follow the core shape; upstream failures answer 502 `UPSTREAM` and never raise elsewhere.

## AI tools, search, nearby, events

- `iss_position {}`: position, altitude, speed, sunlight, element age.
- `satellite_passes {lat, lon, hours≤72, norad_id?, min_elevation?, visible_only?}`: server-side SGP4.
- `upcoming_launches {hours≤720, limit≤40, provider?, country?}`.
- `weather_at {lat, lon}`: summary sentence, current, next 24 h, every 3 hours.
- `space_weather_now {lat?, lon?}`: see the space weather section.
- `latest_scenes {lat, lon, days≤120, max_cloud?, missions?}`: per mission the latest scenes over the
  point (satellite, time, cloud cover, tile or orbit, whether the footprint covers the point, bbox,
  viewer link), with the credits. Same cache as the finder.
- Search: entities of kinds `satellite`, `launch`, `launch_pad`. Nearby: launch pads (237 from the
  pad dataset plus pads seen in the launch lists; unnamed "Unknown Pad" entries are hidden unless
  active). Events: launches in the next 24 hours (severity 2, crewed 3) and launches since `since_ts`
  (success 2, partial failure 4, failure 5).
- Python API: `space.iss_now()`, `space.passes(lat, lon, hours, norad, visible_only)`.

## Accuracy

`meridian/modules/space/sgp4.py` is a pure-Python SGP4 written from Spacetrack Report #3 as revised
by Vallado et al. (2006), WGS-72 constants, "improved" mode.

- Near-Earth objects (period under 225 min): matches the Vallado/STR#3 verification object 88888 to
  under a millimetre over a day, and the browser's satellite.js to 0.2 m for real CelesTrak elements
  (ISS, a 16 rev/day cubesat). The elements themselves are good to about 1 km at epoch, degrading
  a few km per day.
- Deep-space objects (navigation, geostationary, Molniya-type): SDP4's lunar-solar and resonance
  terms are not implemented; these use the same secular J2/J4 and short-period terms. Measured
  against full SDP4: 27–31 km for GPS and GEO, 31–70 km for a highly elliptical orbit over 48 hours,
  i.e. well under 0.1° of sky position. The map itself uses satellite.js (full SGP4/SDP4).
- Passes: elevation ≥ 10° by default; rise and set found by bisection to about a second and the
  culmination by golden-section search. Against a 1-second brute-force search with satellite.js,
  all 9 ISS passes over London in 48 h matched (rise and set within 1.4 s, peak within 0.1°).
  "Visible" means sunlit (cylindrical Earth shadow) while the Sun is 6° or more below the observer's
  horizon (civil twilight). ISS brightness is a rough diffuse-sphere estimate (standard magnitude
  −1.8 at 1,000 km).

## Sources, licences and politeness

| Source | Licence / terms | How it is used |
|---|---|---|
| CelesTrak GP data (`celestrak.org/NORAD/elements/gp.php?GROUP=…&FORMAT=json`) | free public data; CelesTrak asks for no more than one download per group every 2 hours | each group fetched only when needed and at most every 2 h, kept in `data/raw/space/celestrak/<group>.json` (a restart never refetches); 15-min backoff after a failure; one-off `CATNR` lookups cached 2 h |
| The Space Devs Launch Library 2, 2.2.0 (free tier, about 15 requests an hour) | free for personal use, credit The Space Devs | one shared, disk-backed cache: upcoming hourly and previous every 3 h while the map uses the module; when idle, refreshed only on demand by the events feed or AI when older than 6 h; local budget of 10 requests per hour; 30-min backoff on HTTP 429; pad list as a 14-day dataset (3 requests) |
| RainViewer Weather Maps API | personal and educational use, "Weather data by RainViewer" credit mandatory | frame list cached 5 min server-side; tiles straight from `tilecache.rainviewer.com` (zoom 0–7 only; colour scheme 2 only); credit in the drawer and the map attribution |
| NASA GIBS (EOSDIS) | open data, no restrictions; credit NASA GIBS (the Sentinel-5P and OPERA layers credit Copernicus Sentinel data in their source text) | tiles straight from GIBS; DescribeDomains cached 10 min (sub-daily) or 3 h (daily); each layer asks only for its own product |
| EUMETSAT EUMETView WMS (`view.eumetsat.int/geoserver/mtg_fd`) | Core data under the EUMETSAT Data Policy: free and unrestricted, CC BY 4.0; credit "EUMETSAT Meteosat product <year>" (see the Meteosat section) | GetCapabilities about 12 KB, cached 10 min, one request at a time, 2-min back-off after a failure (the last good copy is served up to 6 h); GetMap tiles straight from the browser, a week of HTTP caching per fixed time |
| Element 84 Earth Search (Sentinel-2) | free public STAC API; Copernicus Sentinel data "free, full and open"; credit "Contains modified Copernicus Sentinel data <year>" | on request only, one search per finder search, cached 1 h per place and settings; quicklooks from the public `sentinel-cogs` bucket, cached 1 day |
| Copernicus Data Space Ecosystem STAC (Sentinel-1) | Copernicus Sentinel data "free, full and open" (CDSE terms); same credit | on request only, cached 1 h; thumbnails cached 1 day |
| Microsoft Planetary Computer STAC (Landsat) | Landsat is public domain (USGS); credit "Landsat imagery courtesy of the U.S. Geological Survey"; previews rendered by Planetary Computer | on request only, cached 1 h, `fields` keeps responses small; previews cached 1 day |
| Open-Meteo forecast API | CC BY 4.0, free for non-commercial use | on request, cached 10 min per 0.01° cell; credit under the weather section |
| NOAA SWPC products (`services.swpc.noaa.gov`): `noaa-scales.json`, `planetary_k_index_1m.json`, `noaa-planetary-k-index.json`, `noaa-planetary-k-index-forecast.json`, `goes/primary/xray-flares-7-day.json` and `-latest.json`, `geospace/propagated-solar-wind-1-hour.json` (fallback `summary/solar-wind-speed.json` and `-mag-field.json`), `alerts.json` | public domain (US government work); credit NOAA SWPC | only while the drawer was used in the last 5 minutes: every 5 min for scales, Kp now, the latest flare, wind and alerts, 15 min for Kp and the flare list, 30 min for the Kp forecast; fetched in parallel with gzip, each feed backs off 1 to 30 min after a failure; when nobody looks, the events provider refreshes alerts, scales, Kp and flares on demand once they are 30 min old (about 70 KB) |
| NOAA SWPC OVATION (`json/ovation_aurora_latest.json`, about 150 KB gzipped) | public domain; credit NOAA SWPC | only while the aurora layer or What's here was used in the last 10 minutes; cached 10 min |
| NASA GSFC OMNIWeb CGM model | public domain | 72 requests once, while building the module; the table lives in `cgm.py` |
| Basemap vector layers (CARTO / OpenStreetMap, via the core basemap) | as the core basemap | copies of border and place-name layers drawn above opaque imagery |

No keys are needed. Background tasks only run while the browser uses the module (`m.active()`).

## Storage

`data/raw/space/celestrak/*.json` (one file per group, file time = fetch time),
`data/raw/space/ll2/{upcoming,previous,pads-N}.json`, `data/build/space/pads.json`. Nothing in SQLite.
Space weather feeds and the OVATION grid live in memory only (a restart refetches about 230 KB).
The EUMETView capabilities, scene searches (at most 300, an hour each) and quicklooks (at most 48 MB,
a day each) are also memory only; after a restart a quicklook is found again from the catalogue's item
record.

## Known gaps and ideas

- GIBS has no Meteosat imagery; the infrared cloud layer now takes Europe, Africa and the Atlantic
  from EUMETView (Space+). Meteosat uses EUMETSAT's colour scale (grey and white cloud, blue to red for
  the coldest tops), so the look changes at 45° W and 67.5° E; below zoom 2.5 tiles are wider than the
  splits, so overlaps blend. The central Indian Ocean is seen only at a slant: the Indian Ocean
  service (`msg_iodc`) was not tested. GIBS `Reference_Features_15m` and `Reference_Labels_15m` were
  failing (HTTP 500) on 22 Nov 2026, so borders and names come from the basemap instead (coastlines
  only as a fallback).
- Space+ left out: OPERA surface water (`OPERA_L3_Dynamic_Surface_Water_Extent-HLS` and `-Sentinel-1`)
  paints "not water" opaque white, so as an overlay it would hide the map; it needs a small server
  proxy that makes that palette entry transparent (the tiles are palette PNGs, so only the `tRNS` chunk
  would change). The same proxy would clear the pale low end of NO2. GOES dust (`GOES-*_ABI_Dust`) is
  an opaque false-colour composite of the Americas only; Meteosat's dust view covers the Sahara.
- The Copernicus Browser deep-link parameters (`lat`, `lng`, `zoom`, `fromTime`, `toTime`,
  `datasetId`, `layerId`) are not documented; if they change, the link still opens the browser.
- USGS LandsatLook thumbnails now redirect to the USGS login page (`ers.cr.usgs.gov`), so Landsat
  quicklooks come from Planetary Computer; Earth Search's Landsat and Sentinel-1 thumbnails are
  presigned links into requester-pays buckets and are not used.
- Quicklooks are not placed on the map (Sentinel-2 previews are in UTM, Sentinel-1 in radar geometry);
  footprints show where each scene is.
- Ideas: night-light, flood and NO2 before/after actions on hazard and conflict cards (they can
  already emit `imagery:compare` with `source`); an alert when a fresh clear Sentinel-2 scene appears
  over a watched place (Alerts team); Meteosat lightning counts per region as events.
- VIIRS Suomi NPP true colour currently ends on 2 Nov 2026 at GIBS; the layer says so.
- Every MODIS aerosol product at GIBS has corrupted tiles (grey noise, fragments of other imagery)
  towards the winter pole, where no retrieval is possible anyway. The aerosol layer only requests tiles
  within 72° of the day's subsolar latitude (about 52° N in late November, the Arctic in June).
- CelesTrak no longer has `noaa` or `glonass-ops` groups: NOAA satellites are in `weather`, GLONASS
  is `glo-ops`. `geo` (the geostationary belt) was added.
- Deep-space server-side propagation is approximate (see Accuracy); a full SDP4 port would bring it
  to satellite.js parity.
- SWPC retired `/products/solar-wind/` (the plasma and mag JSON answer 404 on 23 Nov 2026): the solar
  wind comes from the propagated 1-hour product (L1 measurement plus arrival time at Earth), falling
  back to the summary values. The 1-day RTSW files (`json/rtsw/`, 3 MB) are too heavy to poll.
- SWPC removed its own aurora view line in May 2026; Meridian's lines and the Kp rule are estimates and
  say so. OVATION looks 30 to 90 minutes ahead; "tonight" relies on the Kp forecast and SWPC's rule of
  thumb (about plus or minus 1 Kp). Moonlight and light pollution are not considered.
- Kp "now" is SWPC's 1-minute estimate; the forecast file's "estimated" values for the current day come
  from the 3-day forecast, so the bar at "now" can differ from it.
- Space weather ideas: hemispheric power (`text/aurora-nowcast-hemi-power.txt`) as a trend, WSA-Enlil
  CME arrival times, the day-night terminator on the aurora layer, notifications through the Alerts team.
- Ideas: pass predictions for any satellite from its card on a chosen point, a radar nowcast
  (RainViewer's free API no longer offers one), a GIBS time-lapse for 10-minute clouds, re-entry
  predictions.

## Tests

`python -m unittest tests.test_space tests.test_space_weather tests.test_space_plus -v` (34 + 30 + 24
tests, no network, a few seconds). `test_space_plus.py` covers the extension pack (products, groups,
variants, layer providers and bounds, metadata), DescribeDomains for radar and NO2 and the EUMETView
time strings, the EUMETView capabilities parser, the mixed GIBS and Meteosat catalogue (one
capabilities request, then the cache; a Meteosat outage stays soft and backs off), STAC query
building for each catalogue (point, box, period, cloud filters, fields), search areas, the URL and
id rules, the three STAC parsers on trimmed real responses from 23 Nov 2026
(`tests/fixtures/space/plus/`), the search cache and partial failures, quicklook redirects, refused
hosts, non-images, back-off and recovery after a restart, every new route, the AI tool and the new
source and layer declarations. `test_space_weather.py` covers every SWPC parser on trimmed real files from 23 Nov 2026
(`tests/fixtures/space/swpc/`), plain-language alerts and which are in force (the real night of 19 Nov,
and a synthetic storm with a cancellation, a superseded watch and an expired warning), events and their
stable ids, the OVATION grid, lines and PNG tiles (decoded and CRC-checked), darkness, CGM latitudes,
the tonight sentences (London, Tromso, Paris, Quito, a morning, midsummer), every route, back-off and
caching, the AI tool and search. `test_space.py`: SGP4 against the
88888 object and satellite.js reference values (`tests/fixtures/space/sgp4_reference.json`), the
deep-space bound, sidereal time and the Sun, pass prediction against a brute-force reference,
visibility rules, CelesTrak/LL2/Open-Meteo/RainViewer/GIBS parsers, the group cache and backoff,
the LL2 request budget, every route through a fake network, search, nearby, events, AI tools, soft
failure, and one HTTP round trip for gzip and ETag.
