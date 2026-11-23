# Infra module (Infrastructure, Companies and live electricity grids)

Owner files: `meridian/modules/infra/`, `web/js/layers/infra.js`, `web/js/layers/infra/*.js`,
`web/css/infra.css`, `tests/test_infra*.py`, `tests/fixtures/infra/`. Test port 8797.

## Layers

| Layer id | Title | Group | What it shows |
|---|---|---|---|
| `infra.power` | Power plants | Infrastructure | WRI Global Power Plant Database, circles sized by capacity, coloured by primary fuel. Plants under 30 MW load only from zoom 5 (`power-minor`). Fuel and minimum-size options. |
| `infra.airports` | Airports | Infrastructure | OurAirports large (ring), medium (dot) and small airfields (from zoom 6.5, lazily loaded). IATA codes from zoom 4, names from zoom 7.5. Runway lines for the open card from zoom 10. |
| `infra.ports` | Ports | Infrastructure | UN/LOCODE ports (function 1) with Natural Earth sizes; World Port Index ports when msi.nga.mil answers. |
| `infra.datacenters` | Data centres | Infrastructure | PeeringDB facilities sized by networks present; OpenStreetMap `telecom=data_center` additions when Overpass answers. |
| `infra.dams` | Dams | Infrastructure | Notable Wikidata dams (with a height, reservoir volume, generating capacity, reservoir item or English Wikipedia article), sized by height. |
| `infra.cables` | Submarine cables | Infrastructure | TeleGeography cables in their own colours (planned cables dashed, wider on hover), landing points, amber rings for countries reached by two or fewer cables in service. |
| `infra.companies` | Listed companies | Companies | Wikidata companies with a stock exchange (P414) and headquarters coordinates, sized by employees or market value. |
| `infra.grid` | Electricity grids | Infrastructure | Live country choropleth for 36 European countries plus Great Britain's 14 DNO regions, coloured by renewable share now (default), carbon intensity or day-ahead price (5 classes each, one-hue ramps), with value labels and Britain's 10 interconnectors as straight lines between converter stations, arrows showing the live flow direction. Options: colour by, GB by region, interconnector flows, values on the map. |

Toolbar: **Markets** opens a drawer (stock markets, precious metals and copper, energy, food and farm,
base metals and ore, currencies, crypto), refreshed every 3 minutes while open. Rows for stock
markets fly to the exchange.

Toolbar: **Energy** opens the Electricity drawer, refreshed every minute while open.
- *Great Britain*: carbon intensity now with NESO's index, renewable and low-carbon share, demand,
  price; a 48-hour sparkline (24 h measured, solid; 24 h forecast, dashed; hover for each half hour)
  with the cleanest and dirtiest 3-hour windows ahead in UK time; the generation mix; the 10
  interconnectors as diverging bars (imports right, exports left) with net imports; the 14 regions,
  cleanest first.
- *Europe*: a sortable table (country, renewables now, carbon, price now) for Great Britain and 36
  countries; clicking a country opens its card.

Cards: a European country (renewables today, day-ahead prices today, generation mix, estimated
intensity, a link to Energy-Charts, "Country profile" when intel runs), a GB region (24-hour
regional forecast, cleanest window, regional mix) and an interconnector (flow, capacity, use).
Clicking Great Britain itself opens the drawer. "What's here?" gains an **Electricity now** section
(the grid and, in Britain, the region under the point). The airport card gains a **Delays** section
from the flights module's `GET /api/flights/airport-status/{ident}` when the flights module runs; it
stays hidden on 404 or any error. It lists every status in `items` (programmes, delays, closures,
de-icing and notices) with the FAA's summary, says "No delays reported" only when none of them is a
delay, and shows when the flights module last checked (`fetched_at`), not the FAA's own "last changed"
time, which can be months old for a long closure. The badge is the most serious status, toned like the
`flights.delays` card (red high, amber medium, blue low). Helpers: `web/js/layers/infra/delays.js`.

## Entity kinds and ids

`power_plant` (GPPD `gppd_idnr`), `airport` (OurAirports `ident`), `port` (`wpi:<n>` or
`locode:<CCLLL>`; old `locode:` ids resolve to the WPI port that replaced them), `datacenter`
(`peeringdb:<id>` or `osm:<type>/<id>`), `dam` (`wikidata:Q…`), `cable` (`tg:<slug>`),
`landing_point` (`tg:<slug>`), `company` (`wikidata:Q…`). Every handler copes with a bare
`{kind, id}` and shows a "no longer in the source data" card when the id has gone.

## Endpoints (all GET)

| Path | Returns |
|---|---|
| `/api/infra/status` | Dataset readiness, build errors, counts, which quote keys are set |
| `/api/infra/geo/{name}` | Built GeoJSON (ETag + gzip, 503 `BUILDING` until built): `power`, `power-minor`, `airports-major`, `airports-minor`, `ports`, `datacenters`, `dams`, `cables`, `landings`, `companies` |
| `/api/infra/power/{id}` `/airport/{code}` `/port/{id}` `/datacenter/{id}` `/dam/{id}` `/company/{id}` | Full record plus `entity`. Airports accept ident, ICAO or IATA. |
| `/api/infra/metar/{code}` | Latest METAR for the airport, or the nearest report within 40 km (`nearest`, `distance_km`); cached 5 minutes |
| `/api/infra/cable/{id}` | Cable, landing points, countries with their cable counts, plus TeleGeography details fetched on demand (cached 30 days; `?details=0` skips the fetch) |
| `/api/infra/landing/{id}` | Landing point, cables there, the country's cable dependency |
| `/api/infra/country/{iso2}` | CONTRACT 9.1: `{cables, cable_names, landing_points, power_plants, capacity_mw: {fuel: mw}, airports, ports, datacenters}` plus `planned_cables`, `cable_dependency` (`none`/`high`/`moderate`/`low`), `capacity_total_mw`, `airports_by_type`, `dams`, `companies`, `ready` |
| `/api/infra/countries` | Cable dependency table for every country with landings, fewest cables first |
| `/api/infra/quote?symbol=` | One quote: `XAU XAG XPT XPD HG BTC ETH` (live), FX pairs like `EURUSD` (ECB daily), `wb:BRENT` etc. (World Bank monthly), `fred:SP500` etc. (end of day), shares as `TICKER:MIC` (needs a key), or a drawer item id (`gold`, `brent`, `fx_eurusd`) |
| `/api/infra/markets` | The drawer snapshot; a background refresh starts when it is older than 2 minutes |
| `/api/infra/energy/gb` | Great Britain now: `intensity` (now, index, 48 h sparkline, cleanest and dirtiest 3 h windows), `mix` (NESO, with renewable, fossil, low-carbon and import shares), `generation` (Elexon FUELINST by fuel), `interconnectors` (signed MW, direction, capacity, both ends as `[lon, lat]`), `flows`, `demand` (INDO with today's peak), `price` (market index £/MWh and €), `regions`, `nations`, `errors`, `attribution` |
| `/api/infra/energy/europe` | Table rows for GB and 36 countries: `renewable_now` (`renewable_kind` `now` or `latest`, `renewable_note` `no_signal` or `disagrees`), `intensity` (`intensity_kind` `neso` or `estimate`), `fossil_pct`, `low_carbon_pct`, `load_mw`, `price` (€/MWh now), `price_zone`, times, `pending`; plus the `pending` count and attribution |
| `/api/infra/energy/map` | Compact values for the layer: `countries {iso2: {r, i, p, f, x}}`, `regions {id: {i, x, r, n}}`, `links` |
| `/api/infra/energy/country/{iso2}` | One country: the row plus `signal` (today's renewable share series), `power` (the latest mix) and `prices` (today's day-ahead prices); `GB` answers `{gb: true}`; asking moves the country to the front of the fetch queue |
| `/api/infra/energy/gb/region/{1-14}` | One GB region now plus NESO's 24-hour regional forecast (cached 15 minutes) and its cleanest window |
| `/api/infra/energy/at?lat&lon` | The grid country and GB region under a point (for "What's here?"); it does not start polling |
| `/api/infra/geo/energy-countries`, `/api/infra/geo/energy-regions` | Built shapes: 37 countries (Natural Earth 1:50m, overseas parts and Northern Ireland dropped, about 170 KB) and the 14 DNO areas reprojected to WGS84 (about 120 KB) |

Data file for other modules: `data/build/infra/ports.json` = `[{id, name, country, lat, lon, size}]`
(`size` is `large`, `medium`, `small`, `very small` or null). It is written as ASCII-escaped JSON
(Kakumäe is stored as `"Kakum\u00e4e"`), so it decodes the same whatever encoding the reader opens it with. Sub-harbours
of big ports appear as separate entries.

## Counts (build of 22 November 2026)

| Dataset | Count |
|---|---|
| Power plants | 34,936 (5,707 GW); 13,875 at 30 MW or more load at world view |
| Airports | 48,075: 1,174 large, 4,108 medium, 42,793 small |
| Ports | 12,412: UN/LOCODE positions plus Natural Earth (537 sized, 467 placed where UN/LOCODE has no position, 77 added); 174 large |
| Data centres | 8,628: 5,304 PeeringDB facilities, plus 3,324 more from OpenStreetMap (of 4,883 mapped) |
| Dams | 12,945 notable dams, 6,677 with a height, 362 with a reservoir volume |
| Submarine cables | 714 cables, 1,929 landing points, 186 country documents; 53 countries reached by 2 or fewer cables |
| Listed companies | 11,207 with headquarters coordinates (1,558 precise, the rest city positions); 212 exchanges |
| Commodity prices | 23 World Bank monthly series to October 2026 |

## Search, nearby, AI tools

- Search: airports by ICAO, IATA, ident, name, town or keywords; ports by name or LOCODE; power
  plants by name, owner or GPPD/WEPP id; cables and landing points; companies by name, ticker,
  `TICKER:MIC`, ISIN or QID; dams and data centres by name.
- Nearby: every point kind. Above 25 km radius, small airfields, tiny plants, unsized ports and low
  dams are skipped so lists stay useful.
- AI tools: `infra_near` (lat, lon, radius_km, kinds), `airport_info` (code; includes METAR and a
  LiveATC link), `cable_info` (query), `company_lookup` (query; includes a quote when a key is set),
  `market_quote` (symbol), `grid_now` (country: a name or ISO code, GB or any of the 36 countries)
  and `uk_carbon_intensity` (optional region name or id 1-14, optional outward postcode such as
  RG1: national now, the next 24 hours, the cleanest and dirtiest 3-hour windows, all 14 regions).

## Sources, licences, refresh

| Source | Licence and terms | Refresh |
|---|---|---|
| WRI Global Power Plant Database v1.3.0, GitHub CSV (11.97 MB, 34,936 rows); WRI zip as fallback | CC BY 4.0 | monthly check (static since 2021) |
| OurAirports airports, runways, frequencies, regions | Public domain | weekly |
| aviationweather.gov METAR API | US Government, public domain; under 100 requests/minute (we space 0.7 s) | on demand, 5 minutes |
| NGA World Port Index (Pub. 150) | US Government, public domain | separate `wpi` dataset; retried with backoff (0, 20, 60 s), then at most every 6 hours while missing |
| UN/LOCODE code list (datasets/un-locode on GitHub, from UNECE) | free to use with attribution | monthly |
| Natural Earth ports | Public domain | monthly |
| PeeringDB `/api/fac` | PeeringDB acceptable use policy; one keyless request per refresh, contact fields dropped | every 3 days (raw cached 1 day) |
| OpenStreetMap via Overpass | ODbL | with PeeringDB, when Overpass answers |
| Wikidata Query Service (dams, companies) | CC0; small POST batches, 1 s spacing, split on timeout | dams monthly, companies every 2 weeks |
| TeleGeography Submarine Cable Map API v3 | CC BY-NC-SA 3.0, attribution, non-commercial | every 2 weeks (cable and landing GeoJSON plus about 190 country documents at 1 per second); cable details on demand, cached 30 days |
| gold-api.com | free API; no more than about one request per second (we space 1.1 s) | while the drawer is open, cached 90 s |
| ECB euro reference rates (90-day XML) | free reuse with the source named | cached 1 hour |
| World Bank Pink Sheet monthly prices (link found on the commodity-markets page) | CC BY 4.0 | every 3 days |
| FRED `fredgraph.csv` | public data, some series copyrighted by their owners | end of day, cached 3 hours, 6-hour back-off after a failure |
| Finnhub, Twelve Data | free tiers, need `FINNHUB_API_KEY` / `TWELVEDATA_API_KEY` | on demand, cached 60 s; keys travel in headers, never in URLs, logs or the browser |
| NESO Carbon Intensity API (`/intensity/{from}/{to}` for 48 h, `/regional`; the regional forecast and postcode lookups on demand) | CC BY 4.0 under NESO's terms of use (do not replace their service; a rate limit applies); "Supported by National Energy SO Open Data" | every 5 minutes while the energy views are in use (2 requests), cached 4 minutes |
| Elexon Insights Solution (`/datasets/FUELINST`, `/demand/outturn`, `/balancing/pricing/market-index`), keyless | BMRS data licence: "Contains BMRS data © Elexon Limited copyright and database right [year]" with a link to the licence | every 5 minutes while in use (3 requests) |
| Energy-Charts API, Fraunhofer ISE (`/signal`, `/public_power`, `/price`) | CC BY 4.0, attribution to energy-charts.info; prices CC BY 4.0 from Bundesnetzagentur, SMARD.de | published limit 2 requests a minute per endpoint (20 for `/signal`): one worker asks 1 per 4 s (`/signal`) and 1 per 40 s (`/public_power`, `/price`), each country hourly, each price zone daily, never the same thing within 15 minutes and at most 200 requests an hour, pausing an endpoint on HTTP 429 for its Retry-After; results are kept in `data/cache/infra/energy-europe.json` (36 hours) |
| Natural Earth admin-0 countries 1:50m (GitHub GeoJSON, 3 MB) | Public domain | `energy-shapes` dataset, monthly |
| NESO GIS boundaries for GB DNO licence areas (20240503 GeoJSON, 3 MB, EPSG:27700) | NESO Open Data Licence (based on OGL 3.0): "Supported by National Energy SO Open Data" | `energy-shapes` dataset, monthly; converted with the Ordnance Survey transverse Mercator and Helmert formulas |

Electricity polling uses its own activity key (`infra.energy`, 10 minutes), set by the energy routes
only, so other infra layers never start it. With `--no-tasks` the drawer still fills: the European
worker then starts on demand and stops after 10 idle minutes.

Settings: `FINNHUB_API_KEY`, `TWELVEDATA_API_KEY` (secret), `INFRA_WPI_URL` (non-secret; the
World Port Index download link if NGA publishes a new edition).

Raw downloads live in `data/raw/infra/` (about 45 MB; built files about 90 MB) and are only parsed, never executed.

## Known gaps

- **Carbon intensity outside Great Britain is an estimate**: the latest Energy-Charts generation mix
  (often 2 to 5 hours old, because ENTSO-E data lags) times NESO's published emission factors (coal
  937, oil 935, CCGT 394, biomass 120, other and waste 300 g per kWh, zero for nuclear, hydro, wind,
  solar and storage; lignite and peat count as coal). It counts direct CO2 from domestic generation
  only, so it is lower than lifecycle figures and ignores imports. Great Britain's figure is NESO's.
- **Renewable share** is Energy-Charts' share of load now (partly forecast) for Europe and NESO's
  share of generation (imports count as non-renewable) for Great Britain: close, not identical.
  Countries that import a lot in winter (Switzerland, Hungary, Lithuania) show low shares of load,
  and Switzerland and a few others report only part of their generation to ENTSO-E. Switzerland's
  `/signal` forecast counts little but solar (about 1% at night while the reported mix, mostly hydro,
  gives 75%): when the signal and the latest mix (under 12 hours old) are 30 points and three times
  apart, the table and card use the mix (marked †, `renewable_note: disagrees`). One- and two-step
  glitches in `/signal` (Italy jumps from 21% to 82% for 15 minutes) are dropped before use: a point
  3 times above or below the median of its two neighbours each side, and 20 points from it. Ireland,
  Ukraine and Serbia answered `/signal` with no data during testing (retried every 6 hours);
  Bosnia and Herzegovina, North Macedonia, Cyprus, Moldova, Georgia and Kosovo have no day-ahead
  price zone on Energy-Charts.
- **The first fill of the European table takes about 20 minutes** at Energy-Charts' published rate;
  the cache makes later starts immediate.
- **ENTSO-E** (key-optional) is not used: Energy-Charts already republishes ENTSO-E's generation and
  prices keylessly for the same countries, and neither covers Great Britain.
- **US grid (EIA)** is not built: it needs `EIA_API_KEY` and was left for later.
- **Pipelines and LNG terminals**: Global Energy Monitor's trackers need a download form, so they are
  not used; OpenStreetMap pipelines would need Overpass, which timed out when tested.
- **GB price** is Elexon's market index (the APX short-term market, half-hourly), not a day-ahead
  auction price like the European ones; it is converted to euros with the ECB reference rate.
- Interconnector lines are straight between approximate converter-station positions, not cable routes.
- Northern Ireland is part of the all-island Irish market; it is dropped from the GB shape and has
  no figures of its own here.

- **Share prices without a key.** Stooq removed `/q/l/` (404) and now serves a proof-of-work
  browser check on every page, so it cannot be used within its terms. No other documented keyless
  share-price API was found. With a free Finnhub or Twelve Data key, company cards show delayed
  prices and the stock-market rows show US-listed country funds (SPY, EWU, EWG…) as stand-ins.
- **Index levels.** FRED supplies S&P 500, Dow, Nasdaq and Nikkei end-of-day levels and daily oil and
  gas prices. It answered the server, but reset curl connections carrying the same generic User-Agent
  during testing, so it may be intermittent; after a failure the module waits 6 hours and shows World
  Bank monthly averages where they exist. Other markets (UK, Germany, Hong Kong, China, India) need a
  share-price key and then show US-listed country funds.
- **World Port Index.** msi.nga.mil answered 503 when tested; ports fall back to UN/LOCODE
  positions (nearest minute) with Natural Earth sizes for about 560 major ports.
- **Overpass** can time out or answer 502 on some networks; data centres are then PeeringDB only until the
  next build. It answers today, so OpenStreetMap adds 3,324 data centres.
- **Company HQ positions** are often the city's coordinates, so many headquarters share a point.
- GPPD has not been updated since 2021, and about 70 plant names in WRI's CSV already contain the
  Unicode replacement character U+FFFD (for example in Altdöbern); they are shown as published.
- Port sizes without the World Port Index come from Natural Earth's display rank, with a short list
  of the largest cargo ports forced to "large".
- Dams: Wikidata coverage is uneven (about 57,000 small US dams are left out unless they have a
  height, volume, power figure, reservoir or English article).
