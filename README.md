# Meridian

Meridian is an independent, open-data situation map that runs on your own computer. One map shows
live public cameras, aircraft, ships, conflict reports and front lines, news, natural hazards,
infrastructure, listed companies, satellites, weather and satellite imagery. You can click anything
for its details, search everything, keep case files, and ask an optional AI assistant that can move
the map for you. It also works as a monitor: watchlists raise alerts, a daily briefing sums up what
changed, and a timeline replays the last 72 hours.

It only listens on `127.0.0.1` and uses public, documented data sources, each credited in the app and
in [docs/SOURCES.md](docs/SOURCES.md). No code, data or design comes from any commercial product, and
it never scrapes or calls one.

## Quick start

You need Python 3.11 or later. There is nothing to install (standard library only).

```
python server.py --open
```

On Windows, `run.bat` does the same. The map opens at http://127.0.0.1:8790/. Stop the server with
Ctrl+C or by closing the window.

On first start Meridian downloads and builds its datasets in the background: cameras, airports,
power plants, ports, cables, companies, country data and conflict history. That takes 5 to 10
minutes and about 300 MB of disk. Layers whose data is still building say so, and **Sources and
status** in the bottom bar shows progress. `python server.py --build all` (or `build_data.bat`) builds
everything up front. Data, logs and settings are kept in `data/`, which is never committed.

## What's on the map

| Group | Layers |
|---|---|
| Cameras | About 96,000 public cameras in 124 countries: traffic, river, volcano, weather and scenic. Official feeds from about 60 agencies (TfL, Caltrans, Digitraffic, DGT, Norway's Statens vegvesen, Japan's MLIT river cameras, Taiwan's highways, USGS and more), plus Windy webcams worldwide with `WINDY_WEBCAMS_KEY`. Camera grid of your favourites. |
| Flights | Live aircraft (adsb.fi, adsb.lol, OpenSky), worldwide military aircraft, emergency squawks, sanctioned aircraft (OFAC, UK, UN and EU lists), estimated positions out of coverage, trails, routes, airport arrivals and departures. US airport delays and flight restrictions (FAA), conflict zone advisories (EASA), and an air activity view of military surges, GPS interference trends and airport slowdowns. |
| Ships | Live AIS vessels, dark-ship estimates, sanctioned vessels (OFAC, UK, UN and EU lists), a straits monitor (Hormuz, Suez, Malacca and more), port activity, maritime anomalies (ship-to-ship meetings, loitering, AIS gaps, spoofing) and chokepoint transits (IMF PortWatch). Without a key coverage is the Baltic (Digitraffic). Add `AISSTREAM_API_KEY` for the whole world. |
| Conflict | Conflict events machine-coded from news (GDELT), the Ukraine front line (DeepStateMap), historical conflicts (UCDP, 1989 onwards), protests (GDELT) and GPS interference derived from aircraft navigation accuracy. |
| Intelligence | Geolocated news and theatre feeds, a country instability index with country cards (World Bank indicators), risk zones, official travel advice (FCDO and US State Department), convergences (three or more kinds of event in one place within 24 hours) and cyber: botnet servers, internet outages, and Cloudflare Radar anomalies and BGP events with a token. |
| Infrastructure | 34,936 power plants, 48,075 airports (runways, frequencies, live METAR, LiveATC links), 12,412 ports, 8,628 data centres (PeeringDB and OpenStreetMap), 12,945 dams, 714 submarine cables and their landing points. Live electricity grids for Great Britain (NESO, Elexon) and 36 European countries (Energy-Charts). |
| Companies | 11,207 listed companies at their headquarters, and a markets drawer (indices, currencies, metals, energy, food). Share prices need a free `FINNHUB_API_KEY` or `TWELVEDATA_API_KEY`. |
| Events | Earthquakes (USGS), GDACS disaster alerts, tropical storms with forecast cones, NASA FIRMS active fires, volcanoes, US and European weather warnings (UK included), tsunami messages, NASA EONET events, WHO outbreaks, England flood warnings (Environment Agency), Copernicus EMS emergency mapping and the GloFAS flood outlook. |
| Space | Satellites (CelesTrak, propagated live in the browser, Starlink optional), the ISS with visible passes, launches (Launch Library 2), and the aurora forecast and space weather now (NOAA SWPC). |
| Weather | Rain radar (RainViewer), and NASA GIBS precipitation, infrared clouds, sea and land temperature, aerosols, snow, sea ice, nitrogen and sulphur dioxide, and lightning (Meteosat). Point weather (Open-Meteo) in "What's here?". |
| Imagery | Daily true-colour satellite imagery, night lights, flood maps, Sentinel-1 radar, Meteosat over Europe and Africa, a satellite scene finder (STAC catalogues) and a before/after compare tool. |
| Saved | Your watchlist areas and My places. |

Other tools:
- **Search** with `/`. Typing searches everything loaded; Enter adds places from OpenStreetMap.
- **What's here?** (right-click) gives the place name, weather, country and everything nearby.
- **Favourites** (the star on any card).
- **Case files** with notes, show on map, and export to JSON or Markdown.
- **Basemaps**: dark, relief, light, streets, satellite and Sentinel-2.
- **Globe** (`g`) and **3D terrain** (`t`).
- **Ask** (`a`), the assistant.

Monitoring and workspaces:
- **Alerts** (toolbar): watchlists of an area (drawn, a place and radius, or the world) with rules
  such as earthquakes, aircraft emergencies or ships going dark. Fired alerts go to an inbox and, if
  you allow them, desktop notifications.
- **Briefing** (`b`): what changed since yesterday, region by region, with an optional narrative by the
  assistant's provider.
- **Timeline** (toolbar, usually `r`): replay the last 72 hours of aircraft, ships and events.
- **Workspaces and My places** (`w`): saved layer sets, your own places (which can become watchlists
  with "Watch in Alerts"), **Copy link** for deep links to the current view or card, and export of any
  layer on screen as GeoJSON, CSV or KML.

## Optional keys

Copy `.env.example` to `.env` and fill in what you want; it lists every setting. Keys stay on this
computer and never reach the browser. The useful ones:

- `WINDY_WEBCAMS_KEY` (free, api.windy.com, Webcams API): about 56,000 more webcams in nearly every country.
- `AISSTREAM_API_KEY` (free, aisstream.io): ships worldwide instead of the Baltic only.
- `ANTHROPIC_API_KEY`: the Ask assistant via Claude (default model `claude-opus-5-5`; set
  `ANTHROPIC_MODEL` for a cheaper one). Or run [Ollama](https://ollama.com) locally for a free,
  offline assistant.
- `FINNHUB_API_KEY` or `TWELVEDATA_API_KEY` (free tiers): share prices on company cards.
- `FIRMS_MAP_KEY` (free, NASA), `OPENSKY_CLIENT_ID`/`SECRET`, `WINDY_WEBCAMS_KEY`,
  `ONTARIO511_KEY`, `ALBERTA511_KEY`, `NY511_KEY`, `CLOUDFLARE_RADAR_TOKEN`: more coverage.

Restart Meridian after editing `.env`.

## Privacy and limits

- Only `127.0.0.1` can reach it, and guards stop other websites in your browser from reading or
  changing its data. Do not expose the port to a network; there is no login.
- Keys in `.env` stay on your computer and never reach the browser. Never commit `.env`.
- It doesn't track named private individuals; there is no people layer. Privately registered
  aircraft show as "Private owner".
- Cameras show the latest public frame only. Nothing is recorded and there is no recognition of any
  kind.
- Estimated positions (aircraft out of coverage, dark ships) are labelled as estimates.
- Machine-coded conflict events and news are unverified reports. The instability index is
  Meridian's own documented formula. Do not use Meridian for safety-critical decisions.
- My places stay on your computer: they are never sent to the assistant or MCP tools. Sharing alerts
  with the assistant is off until you turn it on in Alerts, Settings. The assistant only contacts a
  cloud provider if you configure one.
- Some hosts are blocked on some networks (for example Overpass, IODA, the NYC DOT cameras and
  Norway's AIS stream). Those sources are optional and the app works without them.

## Data licences

- **Code**: MIT, see [LICENSE](LICENSE). Vendored browser libraries keep their own licences, see
  [THIRD_PARTY.md](THIRD_PARTY.md).
- **Data**: the data Meridian downloads belongs to its sources and is used under their terms, listed
  per source in [docs/SOURCES.md](docs/SOURCES.md). The MIT licence does not cover it.
- **Some sources are non-commercial only** (for example TeleGeography cables, Sentinel-2 cloudless,
  ALERTCalifornia, adsb.fi and OpenSky). If you use Meridian for anything other than personal,
  non-commercial purposes, check each source's terms and switch off the ones that do not allow it.
- Test fixtures under `tests/fixtures/` are small samples of public source responses, kept only so
  the tests run offline; they remain the property of their sources.

## Map tiles and browser sources

These are loaded by the browser directly, so they are not in `docs/SOURCES.md`. Each is credited in
the map's attribution or on the card that shows it.

| Source | Used for | Licence or terms |
|---|---|---|
| [CARTO](https://carto.com/attributions) basemaps (Dark Matter, Positron, Voyager) and map fonts | Dark, relief, light and streets basemaps, labels | Map data © OpenStreetMap contributors (ODbL); CARTO basemap terms |
| [Esri World Imagery](https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer) | Satellite basemap | Esri terms of use, with attribution |
| [EOX Sentinel-2 cloudless 2020](https://s2maps.eu) | Sentinel-2 basemap | CC BY-NC-SA 4.0 (non-commercial; contains modified Copernicus Sentinel data) |
| [Mapzen terrain tiles on AWS](https://registry.opendata.aws/terrain-tiles/) | 3D terrain and hill shading | Open data from several sources, with attribution |
| [planespotters.net](https://www.planespotters.net) | Aircraft photos on cards (off by default) | Each photo is its photographer's copyright, shown with credit and a link |

## Running it day to day

[docs/OPERATIONS.md](docs/OPERATIONS.md) covers start at logon, backups and restore, moving to another
computer, exporting your own things (profile), disk housekeeping and how much history to keep
(`MERIDIAN_HISTORY_DAYS`). [docs/MCP.md](docs/MCP.md) connects an MCP client such as Claude Code to
Meridian's read-only tools.

## For developers

- [CONTRACT.md](CONTRACT.md) is the build contract: architecture, module API, browser API and
  design language. Per-module notes are in [docs/modules/](docs/modules/).
- Operations tools (`tools/autostart.py`, `backup.py`, `profile.py`, `housekeeping.py`) are described
  in [docs/OPERATIONS.md](docs/OPERATIONS.md); the MCP bridge (`tools/mcp_meridian.py`) in [docs/MCP.md](docs/MCP.md).
- Tests: `python -m unittest discover -s tests` and `node tests/<name>.mjs` (no network needed).
- Browser check: `node tools/browser_check.mjs --url "http://127.0.0.1:8790/#map=5/51/0" --shot out.png`.
- Regenerate the source list and `.env.example`: `python tools/gen_docs.py`.
- One module on another port: `python server.py --port 8791 --only flights`.
- Logs: `data/logs/`.

## Licence

MIT, Copyright (c) 2026 Islamtahsinul. See [LICENSE](LICENSE).
