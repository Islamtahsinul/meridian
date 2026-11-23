# Cameras module

Public cameras from official open feeds: road and traffic cameras, road-weather and mountain-pass
cameras, wildfire lookouts and city cameras. One normalised list (the `cameras` dataset, rebuilt
daily), a server-side image proxy that only ever fetches the latest frame, a clustered map layer,
camera cards with live stills or video, a camera grid, search, nearby and two AI tools.

Port for testing: 8793 (`python server.py --port 8793 --only cameras`).

## What you get

- **Layer `cameras`** (group Cameras, on by default). Every camera as a clustered GeoJSON source:
  translucent cluster discs with counts at low zoom, SDF camera glyphs from zoom ~11 (cream for still
  images, peach for live video streams), viewing-direction cones from zoom 13 where the heading is
  known, and names from zoom 14.5. Several views mounted on one mast are drawn about 12 m apart on
  the map (towards the way each one faces) so each can be clicked, and only one name is shown per
  mast; every API keeps the true position. Options: Show (all, video only, roads, weather and mountains, cities, coasts and ports),
  viewing direction on or off, names on or off. Click a cluster to zoom in, a camera for its card.
- **Camera card** (entity kind `camera`, works from a bare `{kind, id}`): the latest image,
  refreshed at half the camera's cadence (at least every 15 s, at most every 5 min) and swapped in
  only once decoded, or the live HLS stream / video clip ("Play live video", remembered); data block
  with facing, update cadence, image age and elevation; badges for kind and tags, "Not answering"
  and "Earlier frame" when the camera is down; road, area, region, country, position, source,
  operator, licence (linked) and credit; four nearby cameras ("Same spot" for other views on the
  same mast); "Add to grid", "Open on source", favourite star, fly to, case file. A camera that has
  left the lists opens a card saying so (with when it was last listed, for favourites).
- **Camera grid** (toolbar button "Camera grid"): full-screen 2×2, 3×3 or 4×4 wall of "My grid",
  favourite cameras or the cameras in the current map view (nearest the centre first, up to 144).
  Tiles refresh at each camera's pace (at least 30 s apart, only while the page is visible); paging
  with the arrows or the ← → keys; click a tile for its card. Choices persist in `ctx.settings`
  (`cameras.grid`, `cameras.grid.mine`, `cameras.video`).
- **What's here**: a "Nearest cameras" section with three thumbnails within 15 km.
- **Search** (server provider): token index over name, road, area, region, country, description,
  source and tags, accent-folded, prefix matching; exact words rank above prefixes. ~15–60 ms.
- **Nearby** provider (grid index, 0.5° cells) and **AI tools** `cameras_near` (lat, lon,
  radius_km, limit, kind, tag) and `cameras_search` (query, limit). Browser AI action
  `open_camera_grid {mode}`.

## Sources (November 2026, as tested)

| id | Source | Cameras | Images | Licence / terms |
|---|---|---|---|---|
| `tfl` | TfL JamCams, London (`api.tfl.gov.uk/Place/Type/JamCam`) | 836 (53 unavailable skipped) | stills + mp4 clips | TfL open data terms, based on OGL v2.0; "Powered by TfL Open Data" |
| `caltrans` | Caltrans CCTV, 12 districts (`cwwp2.dot.ca.gov/data/dN/cctv/cctvStatusDNN.json`) | 3,441 (174 out of service skipped) | stills + HLS (CORS open) | Caltrans CWWP2 public feed, no restrictions stated; credit Caltrans |
| `iowa` | Iowa DOT traffic, RWIS and rest-area cameras (ArcGIS) | 1,252 | stills + HLS (CORS open) | CC BY 4.0 + Iowa DOT GIS data terms |
| `il` | Illinois Gateway / Travel Midwest (IDOT ArcGIS layer) | 2,279 | stills | CC BY-SA 2.0 |
| `lakecounty` | Lake County PASSAGE, Illinois (same IDOT layer) | 1,577 | stills | CC BY-SA 2.0; optional: host unreachable when tested |
| `dgt` | DGT Spain, DATEX II device publication (`nap.dgt.es`) | 1,980 | stills | DGT open data, free reuse with attribution |
| `fi` | Fintraffic Digitraffic weather cameras (`tie.digitraffic.fi`) | 2,269 presets at 810 stations | stills | CC BY 4.0, "Fintraffic / digitraffic.fi" |
| `hk` | Hong Kong Transport Department snapshots (`static.data.gov.hk`) | 1,013 | stills | DATA.GOV.HK terms, free use with attribution |
| `drivebc` | DriveBC webcams (`www.drivebc.ca/api/webcams/`) | 1,062 | stills | Open Government Licence, British Columbia |
| `alertca` | ALERTCalifornia wildfire cameras (UC San Diego), listed by Cal OES (ArcGIS) | 1,310 | stills | CC BY-NC-ND 4.0, non-commercial only |
| `is` | Vegagerðin, Iceland (`gagnaveita.vegagerdin.is`) | 499 | stills | Vegagerðin open data service; credit Vegagerðin |
| `alaska` | Alaska DOT&PF road weather cameras (ArcGIS, latest images) | 324 views | stills, image link looked up per request | Alaska DOT&PF public data, no licence stated; credit Alaska DOT&PF |
| `nz` | NZ Transport Agency Waka Kotahi (`trafficnz.info`) | 247 (66 offline skipped) | stills | CC BY 4.0 |
| `york` | York Region, Ontario (open data ArcGIS) | 394 | stills | York Region Open Data Licence |
| `austin` | City of Austin (Socrata `b4k4-adkb`) | 823 | stills | Public domain |
| `calgary` | City of Calgary (Socrata `k7p9-kppz`) | 222 | stills | Open Government Licence, City of Calgary |
| `nl` | Newfoundland and Labrador highway cameras (ArcGIS) | 47 | stills | Open Government Licence, Newfoundland and Labrador |
| `sg` | Singapore LTA via data.gov.sg (`api.data.gov.sg/v1/transport/traffic-images`) | 8 (the feed only lists 8 now) | stills, link looked up per request | Singapore Open Data Licence v1.0 |
| `autobahn` | Autobahn GmbH (`verkehr.autobahn.de/o/autobahn/{road}/services/webcam`) | 0 (all 112 motorways list none) | stills | Datenlizenz Deutschland, Namensnennung 2.0 |
| `nyc` | NYC DOT (`webcams.nyctmc.org/api/cameras`) | 0 here (times out) | stills | NYC DOT public feed; optional |
| `windy` | Windy Webcams API v3 (see "Windy webcams worldwide") | 56,414 (68,059 crawled, 11,645 already in an official feed) webcams in 122 countries with `WINDY_WEBCAMS_KEY` | stills, shown from Windy's own fresh links (never proxied) | Windy Webcams API terms: API links only, linked to Windy, credited |
| `usgsvolc` | USGS volcano webcams via AshCam (`avo-volcview.wr.usgs.gov/ashcam-api/webcamApi/webcams`) | 88 (147 without a recent picture skipped) | stills (`current.jpg`) | public domain (USGS) |
| `faawx` | FAA WeatherCams in Alaska, via the same AshCam list | 125 (104 skipped) | stills | public domain (US government) |
| `ndbcbuoy` | NOAA NDBC BuoyCAMs (`www.ndbc.noaa.gov/buoycams.php`) | 69 (17 without a recent picture skipped) | six-camera panoramas | public domain (US government) |
| `nps` | National Park Service webcams (`developer.nps.gov/api/v1/webcams`) | needs `NPS_API_KEY` (about 290) | pages only (the API has no live picture links) | public domain (NPS) |
| `on511`, `ab511`, `ny511` | Ontario 511, 511 Alberta, 511NY (`/api/v2/get/cameras`) | need `ONTARIO511_KEY`, `ALBERTA511_KEY`, `NY511_KEY` | stills | each 511 site's developer terms |

Total before the worldwide work: **19,583 cameras from 19 live sources** (2,890 with live HLS
video, 837 with video clips), in 9 countries: US 11,006, Finland 2,269, Spain 1,980, Canada 1,725,
Hong Kong 1,013, UK 836, Iceland 499, New Zealand 247, Singapore 8.

With the regional adapters, the global networks and Windy (23 November 2026, when tested):
**96,074 cameras from 63 live sources in 124 countries** (58,777 traffic cameras, 37,297 webcams;
US 40,751, Japan 12,007, Canada 5,045, Spain 3,451, Austria 3,124, Italy 3,101, Germany 2,762).

Looked at and left out: Québec 511 (open data lists 683 cameras, but images sit behind a
Cloudflare browser challenge, which we do not bypass), Oregon TripCheck (pages say "All rights
reserved"), Georgia 511 via GEMA (no licence stated; image host unreachable when tested), Kentucky KYTC
(images redirect to an HTML page), Basque Country open data (most image links dead or unreachable),
Seattle and Washington DC (no image links in the open layers), Iowa RWIS camera table (no
coordinates), QLDTraffic (shared public key exhausted; needs your own key), Taiwan freeway CCTV
(host times out here), Norway, Sweden, NSW (registration or keys; possible keyed additions).

## Dataset

`m.dataset("cameras", ..., max_age_hours=24)` writes, in `data/build/cameras/`:

- `cameras.json`: `{version, built_at, count, sources: {id: status}, cameras: [...]}`. Each camera:
  `id` (`<src>:<native id>`, path-safe), `src`, `name`, `lat`, `lon` (5 decimals), `country` (ISO2),
  `region`, `area`, `road`, `kind` (`image` | `video` | `hls` | `mjpeg` | `page`), `image`, `stream`,
  `page`, `refresh` (seconds), `tags`, `heading` (degrees) and `direction` when known, `elevation`
  (m) when known, `attribution`, `licence`, `description`, `type` (`traffic` or `webcam`, see
  below); `dynamic: true` when the image link is looked up at request time (Singapore, Alaska);
  `direct: true` when the browser loads the picture from the source itself (Windy).
- `cameras.geojson`: lean points for the map (see "Scale" below): `id`, `k` (1 live stream, 2 page
  only, absent for a still), `t` (1 webcam, absent for traffic), `g` filter-tag letters, `h` heading.
  No names: they come from `/api/cameras/labels` at street zoom.
- `windy.json` (only with `WINDY_WEBCAMS_KEY`): the latest Windy crawl, as compact records.

**Camera types.** `make_camera(..., camera_type=None)` gives every row `type`: the adapter's value,
else the source's `Source.camera_type`, else `traffic` for sources tagged `traffic` (road agencies)
and `webcam` for everything else (ALERTCalifornia, volcano, buoy and park cameras). Windy rows
follow Windy's own categories: `traffic` when Windy files the webcam under traffic (two thirds of
its list, mostly road-agency feeds it re-lists), `webcam` otherwise. The map's "Show" filter
separates them. Stream-only cameras (no still) have no `img_url`; their card starts in video.

Each adapter runs on its own (6 in parallel, 300 s budget each). Downloads go to
`data/raw/cameras/<src>/` and are reused while fresh; a failed download keeps the older copy. A
source that fails, or suddenly returns nothing, keeps its rows from the previous `cameras.json` and is
marked stale with the error. Rows whose image host is not on the source's allowlist lose the image
(or are dropped); duplicate ids and duplicate image links are removed (earlier source wins). Image
links are compared with the host in lower case, the path as it is, and the query kept (sorted, cache
busters such as `t=` or `_=` left out), so `buoycam.php?station=41004` and `?station=41009` are two
cameras. Rows of an aggregating source (`Source.aggregates`, Windy) within 100 m of another source's
camera are dropped: the official feed has the better picture and the details. Allowlist path
prefixes keep their case (`511on.ca/map/Cctv/`); host names ignore it. Every source's `needs_key`
is declared with `m.key`, so all keys are listed in Settings and redacted.
Digitraffic allows 300 requests per 5 minutes, so station details (names, municipality, preset
names) are cached for a week and refreshed at about one per second, at most 180 per build.

Raw data on disk is about 30 MB (`data/raw/cameras/`), plus about 9 MB of gzipped
Windy pages with a key.

## Windy webcams worldwide

`sources/windy.py`, optional, needs `WINDY_WEBCAMS_KEY` (free tier). Windy lists about 69,000
webcams in about 200 countries, which is the realistic route to coverage outside the countries with
open road-camera feeds. API: `https://api.windy.com/webcams/api/v3` (OpenAPI spec behind
`api.windy.com/webcams/docs`), key in the `x-windy-api-key` header. Terms:
https://api.windy.com/webcams/terms.

**Crawl (dataset `cameras.windy`, registered only when the key is set).** The free tier returns 50
webcams per call and stops at offset 1,000, so one query can list at most 1,050. The crawl asks
`/countries`, then each country with `countries=XX&sortKey=createdOn&sortDirection=asc`, paging
`offset` 0 to 950. A country with more than 1,000 webcams is split into areas: a box around the
webcams of its first page (plus a margin) is cut into four, with four bands around it for the rest
of the world (overseas territories and outliers), each queried with `countries=XX&bbox=n,e,s,w`;
any area still above 1,000 is cut into four again. Box edges sit on a seventh decimal of 5, so no
webcam (5 or 6 decimals) lies on an edge. Windy's region codes mix ISO-style codes (`CH.SZ`) with
OSM-style ids that do not name their country, so areas are boxes, not regions.

- Polite: at most one request a second (`min_interval=1.0`), a pause when `x-ratelimit-remaining`
  drops under 10 (the limit is 300 a minute), Retry-After honoured, at most 5,000 requests and 3
  hours per run.
- Every page is cached gzipped in `data/raw/cameras/windy/pages/` for 6.5 days, so an interrupted
  crawl resumes where it stopped and the weekly crawl fetches everything again. The dataset is
  checked daily but crawls only when the last complete crawl is 7 days old (a forced rebuild still
  reuses pages younger than 20 hours). Countries that fail keep their webcams from the last crawl.
- When a crawl finishes, its webcams join the camera list straight away (`build(..., only={"windy"})`:
  the other sources keep their rows, nothing else is fetched).
- Windy re-lists many official feeds (all of TfL, most of Caltrans, Fintraffic, Iowa, DriveBC...):
  a Windy webcam within 100 m of a camera from another source is left out (11,645 here),
  so new official adapters automatically take over from Windy where they overlap.
- Indoor webcams (Windy category `indoor`) and inactive ones are left out. Windy categories become
  tags (meteo: weather, mountain, beach and coast: coastal, city, square and building: urban, ...).

Measured 23 November 2026: 69,277 webcams listed by the API; the crawl took about 1,800
requests in 30 minutes and reached 68,059 active webcams in 122 countries (934 indoor or inactive
left out). Biggest: United States 31,916, Canada 3,403, Austria 3,124, Italy 3,088, Germany 2,762, France 2,151 (14 countries needed area splits, 344 areas in all; the US alone 200). The per-country totals add up to 68,993: the other 284 webcams have no country in Windy's data, so country queries cannot reach them.

**Pictures (the free tier's terms).** Windy pictures are never proxied and never stored. Windy rows
are `direct`: `/api/cameras/img/{id}` answers 409 `DIRECT_ONLY` for them. The browser asks
`/api/cameras/direct/{id}` (or `?ids=` for up to 50, one upstream call per 50), which calls
`/webcams?webcamIds=...&include=images,urls,player` server-side with the key and keeps the answer
5 minutes (Windy's links expire after 10 to 15). Links are checked before they go to the browser:
https on `*.windy.com` only, and never containing the key. The browser then:

- shows the API's own image link (`images.current.preview`, 400 × 224 on the free tier; the
  thumbnail in What's here) at its own size or smaller, never stretched (`width: auto`, max-width
  and max-height at the picture's size, `object-fit: contain`, also in grid tiles);
- links every picture to the webcam's Windy page (`urls.detail`), and the card also links the
  timelapse player (`player.day`); in grid tiles and What's here the picture opens Windy and the
  name opens the camera card;
- shows the courtesy line "Webcams provided by Windy.com, add a webcam" (both linked) in the corner
  of every Windy picture (card, grid tile, What's here), in the surrounding text size and colour;
- looks a new link up every 5 minutes while visible; the card shows the picture's age (from
  `lastUpdatedOn`) and Windy's view count. `referrerpolicy=no-referrer` on the images.

The key goes only to api.windy.com in a header; it is never in a URL, a log line, an error, the
raw-file metadata or anything sent to the browser.

## Worldwide public networks

Adapters in `sources/gl_*.py`, for networks that span remote places (the regional engineers cover
GeoNet, the Antarctic stations and Central American observatories in their own files):

| id | Network | Cameras | Licence | Notes |
|---|---|---|---|---|
| `usgsvolc` | USGS Volcano Hazards Program webcams through AshCam, the USGS webcam service (AVO, CVO, CalVO, HVO, YVO) | 88 (147 without a recent picture skipped) | public domain (USGS) | one list with position, height, bearing and a stable `current.jpg`; cameras not public, without a picture or with none in 7 days are skipped |
| `faawx` | FAA WeatherCams across Alaska, from the same AshCam list (`faaInd`) | 125 (104 skipped) | public domain (US government) | FAA's own WeatherCams API needs a login; AVO mirrors these for ash watching |
| `ndbcbuoy` | NOAA National Data Buoy Center BuoyCAMs | 69 (17 without a recent picture skipped) | public domain (US government) | six-camera panoramas from weather buoys at sea; stable `buoycam.php?station=` link |
| `nps` | US National Park Service webcams (Data API) | key needed | public domain (NPS) | `NPS_API_KEY` (free, header `X-Api-Key`); the API gives positions and pages but no live picture links, so these open on nps.gov |

Images verified through `/api/cameras/img/{id}` on 23 November 2026: 18 of 18 sampled (six per source) answered `image/jpeg`; AshCam pictures carry their time (Last-Modified).

Looked at and left out: the USGS HVO webcam API (`vsc/api/hvoWebcamsApi/cams` answers an empty
list; HVO's cameras come through AshCam instead), FAA WeatherCams' own API (401 without a login).

## Scale (100,000+ cameras)

The map file is fetched and clustered by MapLibre's worker (`ctx.api.url` as the GeoJSON source's
`data`), so the page's main thread never parses or copies it. Each point carries only `id`, and
`k`, `t`, `g`, `h` when they are not the default (see `mapfile.py`). The "Show" filter (all,
traffic cameras, webcams, live video, still images, weather and mountains, cities, coasts) is a
MapLibre expression on the source: changing it re-reads the file from the browser cache (304) and
clusters again in the worker. Counts per choice come with `/api/cameras/summary` (`show`). Names
are not in the file: from zoom 12 the browser asks `/api/cameras/labels?bbox=` (up to 400, nearest
the centre, at the fanned map positions) for hover text and street-zoom labels; other hovers look
names up in batches through `/api/cameras/list?ids=`.

| | cameras | map file raw | gzipped | first load (headless) |
|---|---|---|---|---|
| before (19 sources) | 19,584 | 3.86 MB | 446 KB | clusters drawn 3.5 to 4.0 s after navigation; the page parses 3.9 MB and copies it to the worker |
| after (all sources, Windy) | 96,074 | 12.4 MB (129 B a camera) | 1.32 MB | clusters drawn 3.6 to 5.2 s after navigation; nothing parsed on the page; JS heap 49 to 56 MB |
| synthetic 120k, old format | 119,584 | 21.5 MB | 2.56 MB | 5.8 to 6.7 s; a 750 ms main-thread stall copying it to the worker; heap 76 to 80 MB |
| synthetic 120k, new format | 119,584 | 15.5 MB | 1.71 MB | 5.2 to 6.7 s; no main-thread stall; heap 61 to 63 MB |

Server side the catalog interns repeated strings, keeps positions in flat arrays with a 0.5° cell
index, ranks big bbox queries with a planar distance before the exact one (the grid's "In map view"
at world zoom), and scores at most 4,000 search candidates (exact-word matches first). At 120,000
cameras: catalog build about 3.3 s (in the load thread; the old catalog serves until the swap), process memory 270 MB for the whole server (was 388 MB), world-bbox grid query about 90 ms for the 144 nearest of 96,074,
search for a common word about 60 ms ('lake').

## Image proxy

`GET /api/cameras/img/{id}` fetches the camera's current still server-side.

- Only URLs from the dataset (or a source's own resolver) are fetched, and only when the host (and
  path prefix) is on that source's allowlist; http(s) only, default ports unless the source allows
  another, no user info, every resolved address must be public (no loopback, private, link-local,
  CGNAT, multicast or reserved). Redirects are followed by hand (at most three) and every hop is
  checked before it is requested (`allow_redirects=False` in `m.net.request`).
- At most 3 MB; the bytes must be JPEG, PNG, GIF or WebP (sniffed); the declared type must be
  `image/*` or a generic binary type. SVG and everything else is refused.
- In-memory cache per camera for half its cadence (30–120 s), at most 500 images and 48 MB;
  conditional revalidation with ETag / Last-Modified. Never written to disk.
- Politeness: 4 concurrent requests per host, 12 overall, 50 ms spacing, one upstream request per
  camera at a time (others wait for it), cool-down after a 429 (honours Retry-After) or after three
  failures in a row on one host, negative cache for cameras answering 404 or non-images. A
  temporarily failing camera serves its last frame (up to 15 min old) marked `X-Camera-Stale: 1`.
- Answers carry `Cache-Control: private, max-age=<seconds left>`, `X-Camera-Fetched` and, when the
  source says, `X-Camera-Image-Time`. Errors are JSON with a status (404 unknown camera, 403 refused
  URL, 502 upstream error or not an image, 503 busy or cooling down with Retry-After, 504 timeout).
  With `?fallback=1` errors come back as a neutral placeholder image (200, `no-store`) with
  `X-Camera-Error`, `X-Camera-Status` and `X-Camera-Message` headers; the browser code uses this so
  a camera that is down never shows a broken image or fills the console with failed loads.
- Five upstream failures in a row for one source mark it failing in Sources and status ("Camera
  images are not loading"); the next good image clears it.

Video: Caltrans and Iowa HLS streams send `Access-Control-Allow-Origin: *` and play in the card
through hls.js; TfL clips are plain mp4 and play in a `<video>` loop. Other streams are not
proxied; when a stream fails the card falls back to the still.

## Endpoints

| Endpoint | What |
|---|---|
| `GET /api/cameras/geojson` | the map file (ETag, gzip; 503 `BUILDING` until built) |
| `GET /api/cameras/summary` | ready, building, built_at, count, live, by kind / tag / type / country, `show` (count per "Show" choice), per-source status, proxy and direct-link cache stats |
| `GET /api/cameras/sources` | per-source status, licence, attribution, coverage |
| `GET /api/cameras/camera/{id}` | the camera plus `img_url` (none for direct cameras), `direct`, `type`, `live`, `playable`, `compass`, `open_url`, latest-image facts and source (with `courtesy` for Windy); `{"missing": true, "last_seen": ...}` when no longer listed |
| `GET /api/cameras/img/{id}[?fallback=1]` | the current still (see above); 409 `DIRECT_ONLY` for Windy webcams |
| `GET /api/cameras/direct/{id}`, `GET /api/cameras/direct?ids=a,b` | for direct cameras (Windy, up to 50): `{ok, image, width, height, thumb, link, player, updated_at, views, courtesy, expires_at}`, looked up server-side, reused 5 min |
| `GET /api/cameras/labels?bbox=w,s,e,n[&limit][&show]` | names at map positions (GeoJSON, nearest the centre first, up to 600) for street-zoom labels and hover |
| `GET /api/cameras/list?ids=a,b` | cameras by id (up to 100, missing ones flagged) |
| `GET /api/cameras/list?bbox=w,s,e,n[&lat&lon][&kind=live\|still][&type=traffic\|webcam][&tag=weather][&limit]` | cameras in a box, nearest the centre first, with the total in the box |
| `GET /api/cameras/list?lat&lon&radius_km[&limit]` | cameras near a point with distances |

## Settings and keys (`.env`)

- `WINDY_WEBCAMS_KEY`: Windy Webcams API key (free tier): the whole free-tier catalogue, about
  69,000 webcams, crawled weekly (see "Windy webcams worldwide"). Restart after adding it.
- `NPS_API_KEY`: US National Park Service Data API key (free): national park webcams (pages).
- `ONTARIO511_KEY`, `ALBERTA511_KEY`, `NY511_KEY`: free developer keys from each 511 site.
- `CAMERAS_DISABLE` (not secret): source ids to skip, for example `nyc,lakecounty` on networks where
  those hosts are blocked.

Keys stay on the server: they are sent only to their own API (Windy and NPS in a header) and are
redacted from logs, errors and the raw-file metadata. Links handed to the browser are checked not
to contain a key.

## Privacy

Latest frame only. Images live in memory for at most two minutes (or 15 minutes as a stale
fallback) and are never written to disk or kept as history. Sources' "previous image" or replay
links (Caltrans reference images, DriveBC replay, Iowa and Alaska older images) are never used.
No recognition of any kind. Favourite cameras get a last-seen row (id, name, position, date) so a
removed camera's card can say when it was last listed; that table only holds current favourites.

## Polling and traffic

No background polling: the camera list is rebuilt daily, and images are fetched only when a card,
a grid tile or a What's here thumbnail asks for them. A daily build downloads about 25 MB (mostly
Caltrans and DGT), plus up to 180 Digitraffic station details. With a Windy key, the Windy
catalogue is crawled at most weekly (about 1,800 requests at one a second), and each
Windy picture shown costs one API call per 50 webcams every 5 minutes at most.

## Tests

`python -m unittest tests.test_cameras tests.test_cameras_proxy tests.test_cameras_global -v`
(89 tests, no network). `test_cameras_global` covers the Windy crawl against a fake API
(per country, area splits, the 1,000 offset cap, cached pages, budgets and carry-over, a refused
key), Windy compliance (fresh links only from the server, unsafe or key-bearing links dropped, the
proxy refusing Windy, the key only in headers), the global adapters, the lean map file, the crawl
joining the list without re-running other sources, labels, type filters, and the catalog at 120,000
cameras (bbox ranking, counts, search). The other two files cover every
adapter against a small fixture in `tests/fixtures/cameras/`, normalisation and direction parsing,
catalog search and geography, the build (independent failures keep previous rows, keys, allowlist,
de-duplication, disabled sources, fan-out), the routes on a real App with a fake network, and the
proxy (allowlist patterns, private and odd URLs, sniffing, size, cache, 304, 429 cool-down, back-off,
stale frames, hand-checked redirects, resolver, memory bounds).

## Known gaps and ideas

- Autobahn GmbH lists no webcams at present; NYC DOT and Lake County PASSAGE time out from this
  network (they may work at home).
- Singapore's feed currently lists only 8 cameras and gives no names ("Traffic camera 2701").
- Changing "Show" re-clusters in MapLibre's worker: about as long as the first load (seconds at
  100,000+ cameras); the map keeps the old clusters until then.
- Windy's free tier only gives 400 × 224 pictures and links that expire, so Windy pictures are
  not available to the AI or in the image proxy (by its terms).
- Ideas: more keyed sources (Trafikverket, Statens vegvesen, NSW, QLDTraffic, more 511 systems);
  per-camera health on the map; a "recently viewed" grid mode.

## North America additions

New adapters in `meridian/modules/cameras/sources/na_*.py` (shared helpers in `na_common.py`),
discovered automatically. Ids have no hyphens because camera ids must match `^[a-z0-9]{1,16}:`.
Counts are from the test run on 23 November 2026.

| id | Source | Cameras | Pictures | Licence / terms |
|---|---|---|---|---|
| `usgshivis` | USGS HIVIS river and stream webcams, all states, PR, GU (`api.waterdata.usgs.gov/nims/cameras`) | 954 (479 hidden or with no picture for 7 days skipped) | stills (`<camId>_newest.jpg`, 720 px) | Public domain (USGS) |
| `usmtmdt` | Montana MDT road weather stations (511MT map feed `icons.rwis.geojson`) | 426 views at 118 sites | stills | MDT terms: anyone may view, copy or distribute for personal or informational use, unmodified |
| `ussd511` | South Dakota 511 road weather cameras (map feed `icons.cameras.geojson`) | 167 views | stills | SDDOT public map feed, no licence stated; credit SDDOT |
| `catoronto` | City of Toronto traffic cameras (CKAN `traffic-cameras`) | 336 | stills | Open Government Licence - Toronto |
| `cavancouver` | City of Vancouver traffic cameras (open data `web-cam-url-links`, one page per intersection) | 846 views at 220 intersections | stills | Open Government Licence - Vancouver |
| `caab511esri` | 511 Alberta cameras as listed by Esri Canada's 511 Alberta layer (pictures from 511.alberta.ca) | 238 (Calgary's own cameras left out) | stills | 511 Alberta terms: personal and public non-commercial reproduction allowed; optional |
| `uskirkland` | City of Kirkland, Washington (city GIS layer, Kirkland-owned cameras only) | 28 | stills | City GIS layer, no licence stated; credit City of Kirkland |
| `uskingcounty` | King County, Washington (county GIS layer, active non-WSDOT cameras) | 67 | stills, link made per request | King County GIS terms |
| `usbatonrouge` | LADOTD cameras in greater Baton Rouge (Open Data BR `6z6u-ts44`) | 118 (names are camera codes; about 1 in 5 shows LADOTD's "unavailable" card) | stills | Public domain (Open Data BR) |
| `usdeldot` | DelDOT live traffic video (`tmc.deldot.gov/json/videocamera.json`) | 367 | live HLS only (CORS open), no stills | DelDOT public feed, no licence stated; credit DelDOT |

Keyed, built as optional sources (they report "Needs KEY" until the key is set):

| id | Region | Key | Free sign-up |
|---|---|---|---|
| `usnv511` | Nevada | `NV511_KEY` | https://www.nvroads.com/developers/doc |
| `usga511` | Georgia | `GA511_KEY` | https://511ga.org/developers/doc |
| `usaz511` | Arizona | `AZ511_KEY` | https://az511.com/developers/doc |
| `usla511` | Louisiana | `LA511_KEY` | https://511la.org/developers/doc |
| `usid511` | Idaho | `ID511_KEY` | https://511.idaho.gov/developers/doc |
| `usct511` | Connecticut (CTroads) | `CT511_KEY` | https://ctroads.org/developers/doc |
| `usnc511` | North Carolina (DriveNC) | `NC511_KEY` | https://www.drivenc.gov/developers/doc |
| `usut511` | Utah (UDOT Traffic; blocks non-US networks) | `UT511_KEY` | https://www.udottraffic.utah.gov/developers/doc |
| `uswi511` | Wisconsin (blocks non-US networks) | `WI511_KEY` | https://511wi.gov/developers/doc |
| `camb511` | Manitoba | `MB511_KEY` | https://www.manitoba511.ca/developers/doc |
| `canb511` | New Brunswick | `NB511_KEY` | https://511.gnb.ca/developers/doc |
| `cayt511` | Yukon | `YT511_KEY` | https://511yukon.ca/developers/doc |
| `uswawsdot` | Washington (WSDOT access code) | `WSDOT_KEY` | https://wsdot.wa.gov/traffic/api/ |
| `usohgo` | Ohio (OHGO; key sent in a header) | `OHGO_KEY` | https://publicapi.ohgo.com/accounts/registration |
| `caottawa` | City of Ottawa (image certificate) | `OTTAWA_CAMERA_KEY` | https://traffic.ottawa.ca/en/traffic-map-data-lists-and-resources/certificate-request-form |

The IBI sources share one adapter (`na_common.ibi_source`, reusing `ibi511.parse`) and make one
call per build (the API allows 10 a minute). Their pictures are `/map/Cctv/<id>` on the site itself
(checked on 511 Alberta, Manitoba 511 and 511LA when tested).

How they work:

- Keys: each keyed adapter declares its key to the core (`Config.declare`) before reading it, so
  the Settings table lists it and the value is redacted from logs, errors and raw-file metadata even
  when it only lives in the environment. OHGO's key travels in a header. Ottawa's certificate is
  added only when a picture is requested (`resolve`); the camera list and the browser never see it.
- Links that differ only in the query string (King County `Handler.ashx?id=...`, Ottawa) carry no
  `image` but `dynamic: true` and a resolver that rebuilds the link from the camera id, because
  `build.dedupe` compares links without the query string.
- USGS pictures are fetched in S3 path style (`s3.amazonaws.com/usgs-nims-images/...`): the
  bucket's own host name did not resolve when tested.
- Vancouver: intersection pages are read once a week (cached in `data/raw/cameras/cavancouver/pages/`,
  two a second, about two minutes on the first build, then seconds); pages left unread when the
  build budget runs low wait for the next build.
- Montana and South Dakota views older than three days are skipped as offline. View names read
  "Harlowton South, facing northeast" or "Saco, roadway"; elevations come from the "Elev" labels.
- DelDOT publishes only live video: cards play it with "Play live video" (the still image request
  answers `NO_IMAGE`).

Daily traffic: about 3 MB (USGS 1.4 MB, the rest small), plus up to 220 Vancouver pages a week.

Looked at and left out:

- Behind an anti-bot challenge or firewall: Maryland CHART (Cloudflare block, also behind its
  ArcGIS and open data feeds), Oklahoma OKTraffic (Cloudflare), Alabama ALGO Traffic (Incapsula),
  511NJ (network firewall), Houston TranStar (403), Lincoln, Nebraska (Akamai "Access Denied").
- Blocked from outside the US or not answering when tested: Arkansas IDriveArkansas and 511 Wisconsin
  (CloudFront country block), UDOT (403; its 1,437-camera UPLAN layer points at the same host),
  Tennessee SmartWay, the MnDOT IRIS camera list (`data.dot.state.mn.us` times out, though its
  pictures load), MoDOT streams, Rhode Island (TLS error), Vermont VTrans, Tallahassee and Ada
  County (time out), Redmond (WA) pictures.
- Terms do not allow display: South Carolina 511 (SCDOT terms forbid display without written
  permission), Oregon (TripCheck pictures, also in the Portland, Salem and WSDOT lists), Kentucky.
- Not a simple key: 511PA (PennDOT data-feed agreement), FL511 (no public developer sign-up),
  New England 511 (separate partner portal), Nova Scotia 511 and Saskatchewan (no developer page;
  Saskatchewan's API was taken down).
- No usable pictures: Ontario 511 (the keyless MTO ArcGIS layer is good, but every 511on.ca picture
  answers "No live camera feed" when tested), TxDOT ITS (snapshots come base64 inside JSON,
  which the image proxy cannot serve), Washington DC (stream host's TLS certificate does not match),
  Seattle, Bellevue, Raleigh, ITD Idaho, MassDOT, NMDOT and ADOT RWIS (layers without picture
  links), Honolulu (relative picture links that 404), Dufferin County (no names), CARS-platform 511
  sites (Minnesota, Kansas, Indiana, Nebraska, Massachusetts, Colorado: internal GraphQL behind
  reCAPTCHA).
- Not official or not agency cameras: Esri and student re-publications, lodge and resort webcams
  listed by counties (Cook County MN, NC OneMap, GEMA beach cams), police CCTV (Rochester),
  enforcement cameras (New Orleans, red-light and speed layers).
- Covered by other adapters: NOAA buoy cameras (`ndbcbuoy`), national parks (`nps`), USGS
  volcanoes and FAA weather cameras, Calgary.
- Mexico and Central America: no open camera feeds found (CENAPRED answers 403; the Panama Canal
  webcam pages are gone). USGS covers Puerto Rico.

Tests: `python -m unittest tests.test_cameras_na -v` (27 tests, fixtures `tests/fixtures/cameras/na_*`).

## Asia, Oceania, Latin America, Africa and the Middle East

New adapters in `meridian/modules/cameras/sources/ap_*.py` (Asia-Pacific and Antarctica), `la_*.py`
(Latin America) and `af_*.py` (Africa), with shared helpers in `ap_common.py`; discovered
automatically. Ids have no hyphens because camera ids must match `^[a-z0-9]{1,16}:`. Counts are from
the test network on 23 November 2026; pictures were checked through `/api/cameras/img/{id}`.

| id | Source | Cameras | Pictures (checked) | Licence / terms |
|---|---|---|---|---|
| `jpriver` | MLIT river cameras, Japan (www.river.go.jp, one GeoJSON per prefecture) | 10,775 (528 paused skipped) | stills; MLIT CCTV links looked up per request from the camera's record (7/7) | Government Standard Terms of Use 2.0 (compatible with CC BY 4.0); credit "出典：国土交通省 川の防災情報" |
| `twthb` | Highway Bureau, MOTC: Taiwan provincial highways and expressways (data.gov.tw 29817, MOTC XML) | 2,353 | live `/snapshot` stills, about 2 s each (12/12) | Open Government Data License v1.0 (Taiwan) |
| `twwra` | Water Resources Agency, Taiwan: river cameras (data.gov.tw 36687) | 131 | PNG stills, link looked up per request (7/7) | Open Government Data License v1.0 |
| `twdebris` | ARDSWC, Taiwan: debris-flow monitoring cameras (data.gov.tw 9327) | 63 | stills, slow host: 2-7 s, sometimes past the proxy's 10 s (11 of 14); optional | Open Government Data License v1.0 |
| `jpvolcano` | Japan Meteorological Agency volcano cameras | 91 (4 sending nothing skipped) | a frame every 2 min, the newest read from the camera's page (8/8) | Public Data License 1.0 (compatible with CC BY 4.0); credit "出典：気象庁ホームページ" |
| `hkobs` | Hong Kong Observatory weather photos (the station configuration script its map loads) | 34 | every 5 min (7/7) | HKO terms: non-commercial use with acknowledgement of the HKSAR Government and hko.gov.hk |
| `cldgac` | DGAC Chile aerodrome cameras (aipchile.dgac.gob.cl), positions from OurAirports | 93 at 49 aerodromes, incl. Easter Island and King George Island (Antarctica); 4 out of service skipped | every 10 min, newest looked up per request; slow site, 3-7 s (8/9: one camera's newest file is empty upstream) | no licence stated; shown for personal viewing with credit |
| `nzgeonet` | GeoNet volcano cameras (Earth Sciences New Zealand) | 11 | every 10 min (7/7) | CC BY 3.0 NZ |
| `aqaad` | Australian Antarctic Division: Casey, Davis and Mawson stations | 3 | stable `latest.jpg` (3/3) | © Commonwealth of Australia: unaltered display for personal, non-commercial use |
| `aqbas` | British Antarctic Survey: Rothera station | 1 | stable `latest.jpg` (1/1) | © NERC/BAS: personal, non-commercial use |
| `crovsicori` | OVSICORI-UNA, Costa Rica: Poás, Irazú, Rincón de la Vieja | 7 | about every minute (7/7) | © OVSICORI-UNA, all rights reserved; shown for personal viewing with credit |
| `gtinsivumeh` | INSIVUMEH, Guatemala: Fuego (three views) and Santiaguito | 4 | every minute (7/7) | © INSIVUMEH, all rights reserved; shown for personal viewing with credit |
| `niineter` | INETER, Nicaragua: Masaya, San Cristóbal (two views), Telica | 4 | about every minute (7/7) | no licence stated; shown for personal viewing with credit |
| `aunsw` | Transport for NSW Live Traffic (keyless `data.livetraffic.com/cameras/traffic-cam.json`, named in the Live Traffic NSW developer guide) | 249 | none when tested: the image host answers a "temporarily unavailable" HTML page for every path from outside Australia when tested (0/7; probably limited to Australia); optional | CC BY 4.0 |
| `auqld` | QLDTraffic `/v1/webcams`, with the public key printed in the QLDTraffic API specification v1.10 (or your own `QLDTRAFFIC_KEY`) | 0 here: the shared key is over its quota (HTTP 429 all day); optional | not seen | CC BY 4.0 |
| `zasanral` | SANRAL i-traffic developer API (the 511-style API) | needs `SANRAL_ITRAFFIC_KEY`; optional | not seen | i-traffic developer API terms (the website's own terms forbid other reuse) |

Total **13,819 cameras** (JP 10,866, TW 2,547, AU 249 without pictures here, CL 91, HK 34, NZ 11,
CR 7, AQ 6, GT 4, NI 4). Names are kept in their original script with an English name added where
the feed has one ("Provincial Highway 9 km 328.6 (台9線328K+600)", "Sakurajima (桜島 牛根)",
"Victoria Peak (太平山), looking east"); Taiwan counties and Japanese prefectures are given in English.
Volcano observatories in Central America do not publish camera positions, so those cameras sit at
the summit they watch (said on each card).

Keys (free; each source shows "Needs KEY" until set):
- `SANRAL_ITRAFFIC_KEY`: register at https://www.i-traffic.co.za/, then request a developer key
  (https://www.i-traffic.co.za/developers/doc). 10 calls a minute; a build makes one.
- `QLDTRAFFIC_KEY` (optional, replaces the shared public key): email QLDTraffic@tmr.qld.gov.au with
  organisation, contact, email and application name
  (https://qldtraffic.qld.gov.au/more/Developers-and-Data/index.html).
  The source never shows "Needs KEY" (it falls back to the shared key), so the key is declared
  separately in `cameras/__init__.py` to appear in Settings and `.env.example`.

Politeness:
- `jpriver`: river.go.jp asks that tools do not collect from it periodically (regular collectors
  should use the paid FRICS feed). So the camera directory (52 small files, 3 MB) is downloaded once
  and reused for 90 days, one file every 2 s, and pictures (plus MLIT CCTV records, remembered a day)
  are fetched only when a camera is opened. `CAMERAS_DISABLE=jpriver` turns it off.
- `jpvolcano`: camera pages re-read weekly (95 requests, one a second, 150 s budget); a picture
  costs one page (remembered 90 s) and one frame. `cldgac`: one camera page (remembered 5 min) per
  picture. `twwra`: the list, remembered an hour. Curated sources make no requests at build time.
- Raw data for these sources is about 5 MB.

Nothing open found (when tested): South Korea (ITS `openapi.its.go.kr` and `www.its.go.kr`
time out; UTIC keys are tied to approved IP ranges), mainland China, India and the rest of South Asia,
the Gulf states, Israel (Ayalon has no list; data.gov.il only lists enforcement and security
cameras), Turkey (İBB hosts time out), Thailand, Malaysia, the Philippines, Vietnam, Indonesia,
Africa beyond SANRAL, Brazil, Argentina, Colombia, Mexico, Peru, Ecuador and Uruguay, and Victoria,
South Australia, Western Australia, Tasmania, the NT and the ACT.

Looked at and left out: MLIT's national road camera system (its terms forbid direct links and
embedding), NEXCO expressway companies (no republishing), Tokyo's river and port cameras (YouTube
only), Taipei City (pictures need a signed contract), Keelung City (MJPEG streams only: the card
cannot play MJPEG and the proxy cannot cut a frame), Macau DSAT (111 HLS streams with open CORS but
no positions), TDX (Taiwan freeway and city cameras; needs a client id and secret from
https://tdx.transportdata.tw/register, not built), Singapore LTA DataMall (lists the same 8 cameras as
data.gov.sg), Bandung PELINDUNG (an undocumented backend, no terms, CORS limited to its own site),
HCMC (terms forbid reuse), Malaysia InfoBanjir (device logins in the links), Piton de la Fournaise
(OVPF photos need permission), CET São Paulo (8 cameras, no positions, a rolling frame buffer with
no pointer to the newest), SERNAGEOMIN (API key taken from the site's own script), NSW Maritime
webcams (a commercial widget), CENAPRED, DER-SP, MMDA, Jabar and SGC (anti-bot walls or 403), and
stale cameras (USAP McMurdo, Macquarie Island, Turrialba, Momotombo, Concepción, BAS Halley, Bird
Island and King Edward Point).

Shared-code notes: `build.dedupe` treats image links that differ only in their query string as one
image (the WRA and debris-flow servers give every camera from one script, so those rows carry no
image link and resolve it per request); `proxy.host_allowed` lower-cases a pattern's path prefix but
compares the URL path as is, so a prefix such as `/debrisFinal/` never matches (that source lists its
host only); MLIT answers a missing picture with a 200 PNG placeholder, which is shown as is.

Tests: `python -m unittest tests.test_cameras_world -v` (17 tests, fixtures `tests/fixtures/cameras/world_*`).

## Europe

New adapters in `meridian/modules/cameras/sources/eu_*.py` (small shared helpers in `eu_common.py`),
discovered automatically. Ids have no hyphens because camera ids must match `^[a-z0-9]{1,16}:`.
Counts are from the test network on 23 November 2026: **2,427 cameras from 16 keyless sources** in
9 countries (Norway 896, Spain 644, Hungary 493, Estonia 180, Luxembourg 94, Switzerland 45, United
Kingdom 42, France 20, Italy 13), 143 of them with live HLS video, plus 4 optional keyed sources.
For every keyless source, 8 random pictures (all 5 for `frloire`) loaded through the image proxy in
the adapter check, and 4 more through `/api/cameras/img/{id}` on a running server: all loaded.

| id | Source | Cameras | Pictures | Licence / terms |
|---|---|---|---|---|
| `nosvv` | Statens vegvesen, Norway: DATEX II 3.1 CCTV published as open WFS layers (`ogckart-sn1.atlas.vegvesen.no/datex_3_1/ows`, `Cctv` plus `CctvSimple_v2` for status) | 896 (25 with a camera fault left out) | stills about every minute, plus live HLS for 143 (CORS open, plays in the card) | NLOD 2.0, "Made with data from Statens vegvesen" |
| `huutinform` | Útinform, Magyar Közút (Hungarian Public Roads): `www.utinform.hu/api/public/webcam/all`, stills on `cdnuiwebcams.utinform.hu` | 493 views at 278 sites (143 unpublished or a week without a picture skipped) | stills about every 5 min | Útinform conditions of use: personal, non-commercial use only, credit Útinform |
| `esmadrid` | Ayuntamiento de Madrid city and M-30 traffic cameras (datos.madrid.es 202088-0, `informo.madrid.es/informo/tmadrid/CCTV.kml`) | 357 | stills every few minutes | CC BY 4.0 |
| `eetransport` | Transpordiamet, Estonia: road weather cameras (the Tark Tee map's public ArcGIS layer `tarktee.ee/tarktee/rest/services/tram/road_cameras/MapServer/0`) | 180 | stills about every 20 min; the file name is looked up per request (one shared query, cached 2 min) | CC BY 4.0 (andmed.eesti.ee "Images from road cameras") |
| `esmalaga` | Ayuntamiento de Málaga traffic cameras (datosabiertos.malaga.eu KML) | 144 (one repeated picture kept once) | stills about every 2 min | CC BY-SA 4.0 |
| `lucita` | CITA, Luxembourg motorways (`www.cita.lu/kml/cameras.kml`, listed on data.public.lu; place names from six public webcam pages, cached a week) | 94 | stills about every minute; facing from the KML icon angle | CC0 1.0 for the list; public stills on cita.lu |
| `esbizkaia` | Diputación Foral de Bizkaia cameras listed by Open Data Euskadi (`api.euskadi.eus/traffic/v1.0/cameras`, UTM converted) | 58 (431 other Basque records whose pictures did not load when tested left out) | stills about every 4 min | CC BY 4.0 (Open Data Euskadi) |
| `gbdurham` | Durham County Council traffic and road weather cameras (the council's ArcGIS layers; the list is also on Data Mill North) | 42 (33 traffic, 9 road weather) | stills: traffic every minute, weather every 10 min | OGL v3 for the list; pictures © Durham County Council, free display with credit |
| `chmeteo` | MeteoSwiss weather cameras (`data.geo.admin.ch/ch.meteoschweiz.messnetz-webcams`, LV95 converted) | 36 | panoramas every 10 min ("quarter" size, under 400 KB) | CC BY 4.0, "Source: MeteoSwiss" |
| `escat` | Servei Català de Trànsit, Catalonia (`www.gencat.cat/transit/opendata/cameres.xml`) | 36 (35 SCT around Barcelona and on the A-2, 1 Terrassa city camera) | GIF stills about every 5 min; some show SCT's own "imatges no disponibles" card | Llicència oberta d'ús d'informació - Catalunya |
| `esmeteogalicia` | MeteoGalicia webcams (Xunta de Galicia open data "JSON de cámaras", `servizos.meteogalicia.gal/mgrss/observacion/jsonCamaras.action`) | 32 | latest stills (`ultima.jpg`) about every 5 min | CC BY-SA 4.0 |
| `escatfgc` | FGC mountain resorts in the Catalan Pyrenees (`dadesobertes.fgc.cat`, `webcams-actives-tim`) | 17 (13 panorama-only pages skipped) | stills about every 10 min; some pause out of season | CC BY 4.0 |
| `frlyon` | Métropole de Lyon Criter cameras (WFS `pvo_patrimoine_voirie.pvocameracriter`, data.grandlyon.com) | 15 | stills every minute | Licence Ouverte v2.0 |
| `ita22` | Autostrada del Brennero (A22), Brenner Pass to Modena, listed on the South Tyrol Open Data Hub (`tourism.api.opendatahub.com/v1/WebcamInfo?source=a22`) | 13 | stills about every minute | hub record CC0 (not the picture); stills published by A22 for road users, credit A22 |
| `chgeneve` | Canton of Geneva transport office, motorway and main-road cameras (SITG `INFOMOB_CAMERA`, opendata.swiss) | 9 | small stills about every minute | opendata.swiss "open use" |
| `frloire` | Département de Loire-Atlantique: Saint-Nazaire bridge and the Loire ferries (data.loire-atlantique.fr) | 5 | small stills about every 5 min | Licence Ouverte v2.0 |
| `setrafikverket` | Trafikverket, Sweden (Trafikinfo API v2, `Camera`, POST query) | needs `TRAFIKVERKET_KEY` | stills; not run live (no key) | CC0 1.0 |
| `gbnetravel` | North East England UTMC Open Data Service, Newcastle City Council (`www.netraveldata.co.uk/api/v2/cctv/static`) | needs `NETRAVELDATA_KEY` | stills (public links: 3 of 4 documented samples loaded); list not run live | OGL v3 |
| `sinap` | DARS and DRSI, Slovenia, via NAP Slovenia (`b2b.nap.si/data/b2b.cameras.geojson`) | needs `SLOVENIA_NAP_KEY` (the published sample lists 321) | stills; not run live | CC BY-SA 4.0, for registered users |
| `hrhac` | Hrvatske autoceste, Croatia, via the Croatian NAP (`b2b.promet-info.hr/dc/b2b.hac.cameras`, DATEX II 2.3) | needs `CROATIA_NAP_KEY` (the published sample lists 387) | stills (8 of 8 sample links loaded) | Croatian NAP licence, free for registered users |

Keys (all optional and free):

- `TRAFIKVERKET_KEY`: register at https://data.trafikverket.se/ and create a key (guide:
  https://data.trafikverket.se/get-started). Sent only in the POST body.
- `NETRAVELDATA_KEY`: `username:password` of an account from
  https://www.netraveldata.co.uk/wp-login.php?action=register. Sent as HTTP Basic authentication.
- `SLOVENIA_NAP_KEY`: `email:password` of a NAP Slovenia account (https://www.nap.si); then request
  "Cestne kamere (GeoJSON)" on your profile page. Exchanged for a short-lived OAuth2 token.
- `CROATIA_NAP_KEY`: `email:password` of a promet-info.hr account
  (https://www.promet-info.hr/en/registration); then request "HAC Cameras". Sent as HTTP Basic.

No key or login goes into a URL, a file name or a raw file. The names end in `_KEY`, so values set
in `.env` are redacted from logs and errors.

How it works:

- `huutinform` is personal, non-commercial use only (like `alertca`); `CAMERAS_DISABLE=huutinform`
  leaves it out.
- `escat` (SCT) and the `gbdurham` weather stations: their picture links differ only in the query
  string, which the build's duplicate check ignores, so those rows carry no image link and the
  source's resolver rebuilds the same link from the camera id (no extra request, same allowlist).
- `frloire`: the feed has no coordinates, so its five known views are placed at the bridge and the
  ferry crossings (positions from OpenStreetMap).
- `chmeteo`: the links in MeteoSwiss's own open data point at the camera maker's image service
  (Roundshot), which redirects to the newest file; every hop is checked against the allowlist.
- Spain: DGT lists the state roads; Madrid, Málaga, Bizkaia, Catalonia and Galicia add city,
  regional and weather cameras that DGT does not.
- One build of all European sources takes about 25 s and keeps about 5.5 MB of raw data (Norway's
  WFS is 4 MB; the Catalan XML host is the slowest, at about 20 s).

Looked at and left out:

- **Terms do not allow reuse, or the pictures are withheld:** National Highways, England (Crown
  copyright, licensed to media partners), Traffic Scotland (no hotlinking), Traffic Wales
  (restricted server), TII Ireland (no reproduction or display without permission), Flanders'
  Verkeerscentrum (no reuse or inline linking), ASFINAG (private use; linking needs approval),
  BayernInfo (private use only), Hamburg (pictures withheld), Baden-Württemberg (withdrawn for
  security), Autostrade per l'Italia (all rights reserved), Andorra (no reuse without written
  consent), the Alpes-Maritimes, Haute-Savoie and Isère inforoute sites (prior written permission),
  HAK Croatia (picture links carry an access token), and the South Tyrol Open Data Hub's other
  ~1,450 webcams (pictures belong to commercial webcam companies, hotels and lift operators; the
  hub's CC0 covers only the records).
- **robots.txt disallows automated access:** Land Tirol, Infoviaggiando (A4), FVG Strade, ARPA
  Lombardia.
- **Positions or pages only, no open feed:** the Netherlands (camera registers without pictures),
  Wallonia (session-based site), Brussels, Denmark (no cameras in the national access point),
  Latvia, Lithuania, Tallinn, the Faroe Islands, Dublin, Sheffield, Plymouth, Nottingham, Grenoble,
  Genoa, DIR Ouest (France), Swiss cantons other than Geneva, Liechtenstein, Monaco (portal down),
  Malta, San Marino, Greece, Cyprus (locations only), İzmir, Serbia, North Macedonia, Bosnia and
  Herzegovina, Montenegro, Albania, Kosovo, Romania, Moldova, Portugal, and Madrid's separate M-30
  list (its cameras are already in the city KML; the rest are named only by codes).
- **Unreachable or blocked when tested:** GDDKiA, Poland (anti-bot challenge), ŘSD and NDIC,
  Czechia, SSC and NDS, Slovakia, Istanbul IBB, api.bg (Bulgaria), Valencia's camera host, Vigo
  (HTTP 403), the Basque Government's Trafikoa, Bilbao (404), Glasgow (403), Isle of Man (refuses
  automated clients), Piedmont river cameras.
- **Left out on purpose:** Ukraine (conflict zone).

Known gaps: Germany, Austria, Italy beyond the A22, Poland, Czechia, Slovakia, the Netherlands,
Belgium, Ireland, Denmark, Portugal, the Balkans, Greece and Turkey have no camera feed that is both
reachable when tested and open for reuse. The four keyed sources were built from each provider's
documented answers and have not run with a real key. Some Madrid and Catalan cameras show the
operator's own "not available" card.

Tests: `python -m unittest tests.test_cameras_eu -v` (54 tests, fixtures `tests/fixtures/cameras/eu_*`).
