# intel: conflict and intelligence

Port for solo testing: 8796 (`python server.py --port 8796 --only intel`).

Code: `meridian/modules/intel/` (backend), `web/js/layers/intel.js` and `web/js/layers/intel/*.js` (browser),
`web/css/intel.css`. Tests: `tests/test_intel.py` and `tests/test_intel_plus.py` (pure functions),
`tests/test_intel_service.py` and `tests/test_intel_plus_service.py` (module on a temporary app, offline).
Fixtures: `tests/fixtures/intel/` (trimmed real responses: Radar, FCDO, US State Department).

Programme 2 (Intelligence+) added Cloudflare Radar anomalies and BGP events (`radar.py`), official travel advice
(`advice.py`, `advice_service.py`), protests (`protests.py`), news spikes (`spikes.py`), a one-off history seed
(`seed.py`), and their routes and providers (`api_plus.py`, `service_plus.py`).

## Layers

| Id | Group | What it shows |
|---|---|---|
| `intel.conflict` | Conflict | GDELT conflict events (assault, fighting, mass violence), deduplicated, last 1 to 14 days |
| `intel.protests` | Conflict | GDELT protests (CAMEO 14): demonstrations, strikes, blockades, riots; diamonds coloured by kind |
| `intel.frontlines` | Conflict | Ukraine: occupied (since 2014, since 2022), unclear-status and retaken areas, derived front line |
| `intel.ucdp` | Conflict | UCDP GED deaths per 0.5° cell for a chosen period (1989 to the current candidate year) |
| `intel.news` | Intelligence | Latest geolocated GDELT article per place; toolbar button **News** opens the theatre drawer |
| `intel.countries` | Intelligence | Instability index 0-100 choropleth; click for the country card |
| `intel.advice` | Intelligence | Official travel advice, common 1-4 scale, coloured by FCDO, the US or the stricter of the two |
| `intel.risk` | Intelligence | Risk zones: half-degree cells where conflict events cluster over 7 days |
| `intel.convergences` | Intelligence | Places where 3+ kinds of event from all modules coincide within 24 h |
| `intel.cyber` | Intelligence | Botnet command servers by hosting country; outage signals (IODA, Radar reports and anomalies); possible BGP hijacks (arcs) and route leaks (Radar, token) |

Entity kinds handled in the browser: `conflict_event`, `protest` (`gp…`), `news`, `theatre`, `country` (id = ISO2),
`frontline`, `risk_zone`, `convergence`, `cyber` (`feodo:<ISO2>`, `cfbgp:<Radar id>` hijack, `cfleak:<Radar id>` leak),
`outage` (`ioda:<ISO2>`, `cfr:<Radar uuid>` traffic anomaly), `advice_change` (`fcdo:<ISO2>:<epoch>`,
`us:<ISO2>:<epoch>`), `news_spike` (`<ISO2>:<volume|conflict|protest>:<epoch of the first flagged hour>`),
`conflict_history` (UCDP cell). `protest`, `advice_change` and `news_spike` are not in the canonical kind list yet.
The country card gained Travel advice (first), Protests, News volume and Internet (Cloudflare Radar) sections; the
News drawer opens with an "Unusual news volume" list.
What's here gets a Country section (point in polygon, with a neutral note for Crimea and other disputed areas).

## Sources, licences and refresh

| Source | Use | Licence or terms | Refresh | Reachability when tested |
|---|---|---|---|---|
| GDELT 2.0 Events, 15-minute exports | conflict events, news points, country tallies | Open, credit the GDELT Project | every 15 min, continuous | works |
| GDELT DOC 2.0 API | theatre headlines | Open, credit GDELT; max 1 request per 5 s | 20 min per theatre, only while the module is in use | HTTP 429 (rate limited); feeds fall back to GDELT reports |
| DeepStateMap `api/history/last` | Ukraine areas | Public map data, credited; personal, non-commercial | every 3 h | works |
| Natural Earth admin-0 1:50m | country shapes, labels, point in polygon | Public domain | monthly | works |
| World Bank API (WDI and WGI) | population, GDP, GDP per capita, inflation, unemployment, military spending, political stability | CC BY 4.0 | weekly | works |
| abuse.ch Feodo Tracker | botnet command servers | abuse.ch terms (free use) | every 6 h | works (5 entries today) |
| IODA (Georgia Tech) | outage signals per country | Research and non-commercial use with credit | 30 min while in use | works (timed out earlier in the day) |
| Cloudflare Radar `traffic_anomalies`, `annotations/outages` | verified traffic anomalies (7 days), Outage Center reports | CC BY-NC 4.0 | 30 min while in use, 2 h otherwise | needs `CLOUDFLARE_RADAR_TOKEN` (set in the main app; works) |
| Cloudflare Radar `bgp/hijacks/events`, `bgp/leaks/events` | possible BGP hijacks (24 h, confidence 8+), the 50 most widely seen route leaks (24 h) | CC BY-NC 4.0 | 30 min while in use, 2 h otherwise | needs the token (works) |
| UK FCDO foreign travel advice, GOV.UK content API (`/api/content/foreign-travel-advice`) | FCDO status per country, advice sentences, change notes | Open Government Licence v3.0 | index every 3 h; a country page only when its update time moved | works (226 pages, 229 territories) |
| US State Department travel advisories (`cadataapi.state.gov/api/TravelAdvisories`) | US level 1-4 per country | US Government work, public domain | every 6 h | works (218 advisories, 211 countries mapped) |
| UCDP GED (public CSV) + candidate events | historical layer | CC BY 4.0, cite Sundberg and Melander (2013) | monthly | works (about 40 MB zip) |

**Why some sources run without the browser.** Travel advice and Cloudflare Radar feed change, anomaly and hijack
events to alerts, the briefing and the timeline, so they keep polling (advice every 3 to 6 h, Radar every 2 h when
nobody is looking, 30 min while the module is in use). About 30 KB for the FCDO index, a few changed pages a day, 100 KB
for the US list and four Radar calls of a few KB each.

**Why GDELT is polled continuously:** the conflict layer, risk zones (7 days) and the instability index (30 days)
need a rolling history that a browser-triggered poll cannot provide. Cost: about 96 small zips a day (25-60 KB
each, about 4 MB a day) plus a 320-byte index file every 5 minutes. At first start the module backfills
`INTEL_BACKFILL_HOURS` (default 48, at most 72) of exports, one file a second, newest first; after a restart it
fills gaps inside that window. A 404 on a slot younger than 2 hours is retried every 4 minutes; older 404s are
recorded as missing. Everything else (DOC, IODA, Cloudflare) runs only while the browser uses the module.

## Processing

### Protests
* Kept rows: CAMEO root 14 (140 dissent, 141 demonstration, 142 hunger strike, 143 strike or boycott, 144 blockade,
  145 riot or violent protest; fourth digits give the aim, for example "for policy change"). GDELT codes these
  QuadClass 3, so only the root is checked. Same geocode and retrospective rules as conflict events.
* Dedupe key: root + place rounded to 0.1° + event day (one event per place and day); the most violent code wins
  (riot, blockade, strike, demonstration), then the most mentioned. Shown on the map and in events only with 2 or
  more sources or 3 or more articles after merging (layer option to include single-source reports).
* **Severity 0-10** = 0.2 × |Goldstein| (≤ 2) + kind (dissent 0.5, demonstration 1.0, hunger strike 1.0, strike
  1.2, blockade 1.5, riot 2.5) + 2.5 × log share of mentions (200) + 1.5 × log share of sources (30) + up to 0.5 for
  negative tone + up to 0.5 for repeated reports.
* Events: single protests with severity 6.5 or more and 3 or more sources (or 5 articles), kind `protest`. Country
  surges come through the news-spike detector (metric `protest`), so alerts are not flooded by single rows.
* Volume seen on 23 November 2026: about 0.1 to 1.2 % of GDELT rows; about 100 deduplicated protests a day.

### Travel advice
Common scale with the original wording kept beside it:

| Level | Name | UK FCDO | US State Department |
|---|---|---|---|
| 1 | Normal precautions | no advice against travel | Level 1, exercise normal precautions |
| 2 | Some areas advised against | against all (but essential) travel to parts | Level 2, exercise increased caution |
| 3 | Reconsider travel | against all but essential travel to the whole country | Level 3, reconsider travel |
| 4 | Do not travel | against all travel to the whole country | Level 4, do not travel |

* FCDO publishes four status codes, which are coarse: Sudan ("FCDO advises against all travel to Sudan", except
  two small border areas) is coded `avoid_all_travel_to_parts`, and Chad's codes omit that "the rest of Chad" is all
  but essential. The FCDO level is therefore the higher of the codes and of a conservative reading of the "advises
  against" sentences on the Warnings and insurance page: a sentence that names the whole country ("to Sudan", "to
  the whole of the Central African Republic") or its remainder ("the rest of Chad", "all other regions of Ukraine",
  "the remaining areas of Venezuela"). The name must end there ("Chad's borders", "the Djibouti-Eritrea border" and
  "Kenya border areas" do not count), and text never adds advice to a country without a status code or lowers one.
  Cards say when a level comes from the text. On 23 November 2026 this moved Sudan, Somalia, Libya, the Central
  African Republic and Ukraine to 4 and Chad, Venezuela and Eritrea to 3; FCDO overall: 16 at 4, 5 at 3, 49 at 2.
* FCDO pages map to ISO codes through a table of all 226 slugs (`advice.FCDO_SLUG_ISO2`; one page can cover several
  territories, for example Cook Islands, Tokelau and Niue); new pages fall back to a name match. US advisories map
  through the ISO3 code in their link, then the FIPS code in `Category` (Yemen is `YM`, Bahrain `BA`), then the name;
  duplicates keep the newest.
* Stored FCDO entries carry a parser version (`advice.PARSER_VERSION`): after a change to the normalisation every page
  is read once more at the next index check, so levels never mix two rule sets. Re-reading is not a change by itself
  (only a different level or status set is).
* **Change detection.** First sight of a country is never a change. Later, a change of FCDO level or status codes is a
  `level` change ("FCDO now advises against all travel to Niger (previously against all but essential travel)"); an
  update whose own FCDO note says "FCDO now advises…" or "no longer advises…" is an `areas` change quoting FCDO. The
  first time a page is read, such notes from its change history in the last 30 days are recorded too (Ethiopia and
  Kenya on 23 November). US changes are level changes ("US State Department raises Venezuela to Level 4: do not travel
  (previously Level 3)"). Events: kind `advice_change`, severity 3 for a lowering or an areas update (4 when the
  country is at level 3 or 4), otherwise 4, 5, 6 for new levels 2, 3, 4 (+0.5 for a jump of two levels), at most 7.

### Cloudflare Radar
* Traffic anomalies: type LOCATION (whole country) or AS (one network, placed in the network's country); status
  UNVERIFIED (automatic), TP (verified by Radar) or VERIFIED (verified by Radar and a third party). The default
  answer holds TP and VERIFIED. Joined to Outage Center reports through `outageId` (cause, scope, description).
  Severity: 5 for a country, 4 for a network; +1 VERIFIED, +1 while ongoing, +0.5 for 6 hours or more, +1 for a
  government-directed shutdown, at most 7. Shown as outage rings (bold while ongoing) for 24 hours; listed for 7 days.
* BGP hijacks: confidence 0-12 with RPKI and IRR tags. Arcs (great circles) from the announcing network's country to
  the affected network's country, coloured by confidence, thicker with more detections, brighter while ongoing; a
  ring when both are in one country. The layer shows confidence 10+ by default (option 8 or 12). Ongoing is
  `on_going_count > 0` and not stale. Leaks: the 50 most widely seen in 24 hours; rings at the leaking network's
  country (from Radar's `asn_info`, because `countries` is not in path order) when 20 or more peers saw it (off by
  default).
* Events: TP and VERIFIED anomalies (kind `outage`, id `cfr:<uuid>`) and ongoing hijacks of confidence 10 or more
  (kind `cyber`, id `cfbgp:<id>`, severity 4 + (confidence - 10) / 2, +0.5 ongoing, +0.5 for 5+ prefixes or 2+
  victim countries, at most 6). Wording: "Possible BGP hijack of networks in Malaysia by AS24514 (…)".
* Seen on 23 November 2026: 8 anomalies in 7 days (Congo, Gabon, French Guiana power cut, Pakistan SEA-ME-WE 5 cable
  fault, networks in Cameroon, Switzerland and Malaysia), 42 hijacks of confidence 8+ in 24 hours (6 ongoing at 10+),
  394 leaks.

### News spikes
Per country and metric (all rows = news volume, conflict rows, protest rows), the last 24 hours (hourly tallies)
against complete UTC days before that window (`intel_country_day`, up to 28, at least 14, each with 90 % or more of
its 96 export files). Each baseline day is turned into its share of all GDELT rows that day times the current window's
total, which removes weekday swings and GDELT-wide drops.
* Test: robust z = (ln(1 + x) - median(ln(1 + B))) / (1.4826 × MAD(ln(1 + B)) + 0.1) when the baseline median is 5 or
  more, otherwise the Poisson upper tail p = P(X ≥ x | mean(B)) (summed directly, so tiny tails do not round to 0).
* Unusual: z ≥ 3 (or p < 0.001), at least twice the median and at least 50 rows (volume), 10 (conflict) or 8
  (protest), in two consecutive hourly windows. Notable: z ≥ 2 (or p < 0.01), 1.5 times the median, at least 30, 6 or 5.
* Guards: 60 % or more of the window's rows in one deduplicated event ("Mostly one story") or half of the country's
  rows from one source domain ("Mostly one source") downgrade to notable. Detection pauses under 87 of 96 files in
  the window or when global volume is under half its usual level.
* Unusual flags open or extend an episode (`intel_spikes`; a flag within 4 hours of the last one extends it) and become
  events of kind `news_spike` (position: the country's label point), severity 4 at twice the usual level, 5 at four
  times, 6 at eight times or more. Wording: "Conflict reports about Sudan are 3.4 times the usual level: 412
  machine-coded conflict reports in the last 24 hours against a usual 120 (median of 28 days, adjusted for total GDELT
  volume). Most come from El Fasher and Khartoum. GDELT is machine-coded from news and can spike when one story is
  widely repeated." Never "crisis".
* Until 14 valid days exist the detector says "Building the baseline: n of 14 days" and never flags.

**History seed (one-off, bounded).** Stored daily tallies count from the start; the seed fills the rest once, newest
first, one file every 1.2 s: gaps inside the 14-day event window are ingested fully (status `ok`), older gaps add
tallies only (status `seed`, which the instability index does not count as coverage, so its severity sums stay
consistent), and files ingested before protests and hourly tallies existed are read again for those counts (`tallies`
1 to 2). Never repeated once finished; stops at `INTEL_SEED_MAX_MB`. Five failed downloads in a row (network down, computer
asleep, GDELT outage) pause it, waiting 5, 10, 20, 40 and then 60 minutes between tries until GDELT answers again. Failed slots
get up to three passes, 30 minutes apart; any still failing are counted (`errors` in `intel.seed`, and in the source note)
and retried at the next start, so a seed with failures is never treated as complete. On 23 November 2026, 16 days took 1,519 files and 99 MB
(about 65 KB a file) in about 50 minutes; the detector then had 14 baseline days and flagged 11 countries as unusual.

### Conflict events
* Kept rows: QuadClass 4 and CAMEO root 18, 19 or 20; ActionGeo type 3 or 4 (city or landmark) or type 5
  (province, flagged as approximate and drawn hollow). Country (1) and US-state (2) geocodes are dropped. Rows whose
  event date is more than 2 days before the report are dropped as retrospective.
* Country: FIPS 10-4 to ISO 3166-1 (`codes.py`, cross-checked with Natural Earth). Places GDELT files under Israel
  that fall inside the Palestine polygon count to Palestine.
* Dedupe key: root code + place rounded to 0.1° + event day. Merged reports add up mentions, sources and articles;
  the most specific, most severe CAMEO code wins and its report's link becomes the main source; other links and
  actors are ranked by mentions.
* Category: attack or mass violence (bombings, assassination, abduction, 20x), fighting (19x), assault (18x).
  *Armed* when an armed actor type is coded (military, armed group, rebels, insurgents, separatists, intelligence),
  for heavy weapons, bombings and mass killing, or for military-force codes with a state actor. *Crime or policing*
  when the only typed actors are police, criminals or courts; hidden by default (layer option).
* **Severity 0-10** = 0.2 × |Goldstein| (≤ 2) + focus × (type: assault 0.3, fighting 1.0, mass violence 1.5;
  +1.0 heavy weapons, bombing or mass killing; +2.5 × log share of mentions (saturates at 200); +1.5 × log share of
  sources (30); up to +0.5 for tone below 0; +0.5 if armed; up to +0.5 for repeated reports). *Focus* (0.25-1) is
  the share of all rows at that place in the same export that are violent conflict, divided by 30 %: capitals and
  other datelines, where unrelated stories pile up under one place, get damped.
* Card: action in plain words (CAMEO mapping), actors, place, dates, mentions, sources, Goldstein, tone, source
  links with headlines rebuilt from the link, and "Machine-coded from news reports, not verified."

### News
* GDELT points: every precisely geocoded row with a link; one point per place (GDELT feature id), latest wins, kept
  3 days. Headlines are rebuilt from the link slug (sentence case, known acronyms and place names capitalised);
  links without words are kept but hidden from the default view.
* **News severity 0-10** = 1 + QuadClass (material conflict 3, verbal conflict 1.8, material cooperation 0.5)
  + 0.15 × |negative Goldstein| + negative tone / 6 (≤ 1.5) + log share of mentions (≤ 1) + alarm words in the
  headline (first strong word 2, second 1, first moderate 0.8, second 0.4; ≤ 3). Strong words include killed, missile,
  explosion, shelling; moderate ones attack, drone, clashes, troops.
* DOC headline severity = 2 + 2.2 per strong word (≤ 2) + 1 per moderate word (≤ 2) + up to 1.5 when several outlets
  carry the same headline. DOC items are placed at a place name found in the headline (gazetteer built from the
  theatre's own recent events) or at the theatre centre, flagged as approximate.
* Theatres (8): Ukraine and Russia, Israel Gaza and Lebanon, Red Sea and Yemen, Sudan, Central Sahel (Mali, Burkina
  Faso, Niger), Myanmar, Taiwan Strait and South China Sea, DR Congo. Each is a list of countries plus boxes with an
  allowed-country list (at-sea boxes only accept points with no country).

### Front line (DeepStateMap)
DeepState publishes polygons, not a line. Polygons named occupied, Crimea, ORDLO and Tuzla are occupied; "unknown
status" is contested; "dismissed" is retaken by Ukraine (off by default). Editorial polygons on the same map
(for example "East Prussia", "Karelia", "Ichkeria", Transnistria, Abkhazia) are not shown, and unit markers are
ignored. The front line is every occupied-area edge whose far side (probed 2.5 km away) is inside Ukraine and not
occupied, using 0.01° raster masks; Ukraine comes from Natural Earth, eroded by 4 cells (about 4 km) so international
borders and coasts never count. Areas are measured on the same grid. Observed today: 116,400 km² occupied
(43,700 km² since 2014), front about 930 km in 65 pieces.

### Instability index (0-100)
Weighted mean of the available parts (missing parts are dropped and the weights rescaled):

| Part | Weight | Score |
|---|---|---|
| Political stability (World Bank WGI `GOV_WGI_PV.EST`) | 35 % | 100 × clamp((1.5 − estimate) / 4) |
| Conflict, 30 days (GDELT) | 40 % | 100 × (0.5 × S + 0.3 × A + 0.2 × P), each 0-1 |
| News tone, 7 days (GDELT) | 15 % | 100 × clamp((−tone − 1) / 5), needs 30+ reports |
| Hazards, 7 days (other modules' events) | 10 % | 100 × clamp(Σ severity/10 / 4); only when a hazards module is loaded |

S = share of the country's GDELT rows that are violent conflict, / 20 % (needs 200+ rows); A = log share of
severity-weighted events (Σ severity / 10, scaled to 30 days), saturating at 1,500; P = the same per million people,
saturating at 20. While history builds up, counts are scaled from the days covered to 30 days. Bands: low (< 20),
moderate (< 40), elevated (< 60), high (< 80), very high. It is a rough composite, not a forecast.

### Risk zones
Precisely placed, non-crime conflict events of the last 7 days add (severity / 10) × 0.5^(age / 3.5 days) to their
0.5° cell. Each cell is multiplied by (conflict share / 25 %)^1.5, capped at 1, where the conflict share is the
fraction of all GDELT rows from that cell (7 days) that are violent conflict; with fewer than 30 rows the weight is 1.
Cells with 4+ events and a weighted score of 2+ are hot; touching hot cells form a zone (outline traced from the
cell edges). Levels by total score: elevated (< 6), high (< 15), very high.

### Convergences
Every 10 minutes from `App.collect_events(now − 24 h)`: events are binned into 0.5° cells; each 1° × 1° window on a
0.5° stride with 3+ distinct kinds qualifies; overlapping windows are thinned greedily (most kinds, then total
severity). News, risk zones, countries and convergences never count as kinds, nor do country-level or planet-wide
signals: `news_spike` and `advice_change` (placed at a country's label point) and `space_weather` (placed at the
geomagnetic pole or under the Sun). Protests and conflict events are both GDELT reports, often from the same
articles, so together they count as one kind ("conflict and protest reports"). Severity = 1.5 × (kinds − 2) + 0.5 ×
worst member severity + 0.3 × log2(members), capped at 10. Convergences are also emitted as events.

### Cyber
Feodo entries are grouped by hosting country (wording: "hosted in"). IODA severity = 1.6 × log10(1 + overall score),
capped at 9. Cloudflare Radar outage reports (now including network-level ones, placed in the network's country) and
traffic anomalies join the outage rings when a token is set; see Cloudflare Radar above.

### UCDP
The newest GED CSV zip and the current year's candidate CSVs are found by link pattern on the downloads page and read
straight from the archive (never extracted). Events are summed per 0.5° cell and year (events, best-estimate deaths,
deaths by type of violence, top conflicts); events geocoded to a country or to international waters
(`where_prec` 6 and 7) are counted but not mapped. If the public files disappear and `UCDP_ACCESS_TOKEN` is set, the
build pages through the token API for the last 5 years instead.

## Endpoints (all GET)

| Path | Returns |
|---|---|
| `/api/intel/status` | ingestion progress, counts, DOC and source state |
| `/api/intel/conflict?bbox&days&min_severity&crime&limit` | GeoJSON (props: id, s, c, k, p, l, n, t) |
| `/api/intel/conflict/{id}` | event detail (404 once outside the 14-day window) |
| `/api/intel/news?bbox&hours&min_severity&titled&limit` | GeoJSON news points |
| `/api/intel/news/item/{id}` | GDELT point (`gn…`) or DOC item (`dn…`) |
| `/api/intel/news/feeds?theatre&min_severity&q&hours&limit` | items per theatre plus DOC state |
| `/api/intel/theatres` | theatre list with bboxes |
| `/api/intel/frontlines`, `/api/intel/frontlines/meta` | GeoJSON (ETag) and the numbers |
| `/api/intel/countries/shapes`, `/api/intel/countries/index` | simplified shapes (ETag), index per ISO2 |
| `/api/intel/country/{iso2}` | country profile (CONTRACT 9.1) |
| `/api/intel/country-at?lat&lon` | country of a point, with notes for disputed areas |
| `/api/intel/risk`, `/api/intel/risk/{id}` | zone GeoJSON, zone detail with contributing events |
| `/api/intel/convergences`, `/api/intel/convergences/{id}` | GeoJSON plus list, detail with members |
| `/api/intel/cyber`, `/api/intel/cyber/{c2|outage}/{iso2}` | points, country detail |
| `/api/intel/ucdp?from&to`, `/api/intel/ucdp/cell/{id}` | cells for a period, per-year history of a cell |
| `/api/intel/protests?bbox&days&min_severity&single&limit` | GeoJSON (props: id, s, c = r/b/s/d, p, l, n, t, k) |
| `/api/intel/protests/{id}` | protest detail (404 once outside the 14-day window) |
| `/api/intel/advice` | `{fcdo: {ISO2: {l, b, t}}, us: {ISO2: {l, t}}, checked, pending, errors, levels}` |
| `/api/intel/advice/{iso2}`, `/api/intel/advice/changes?days`, `/api/intel/advice/change/{id}` | both sources for a country; recent changes; one change |
| `/api/intel/radar?country`, `/api/intel/radar/anomaly/{uuid}`, `/api/intel/radar/bgp/{hijack|leak|pair}/{id}` | Radar snapshot (or one country's), one anomaly, one BGP event or a country pair (`TR-GB`) |
| `/api/intel/spikes?days&min_z`, `/api/intel/spikes/country/{iso2}`, `/api/intel/spikes/{id}` | detector state, current flags and episodes; one country's reading with a 28-day series; one episode |

Search: countries (name, ISO2, ISO3), theatres, conflict places (7 days), protest places (7 days), news headlines
(3 days), AS numbers in Radar data ("AS24514"). Nearby: conflict events (7 days), protests (7 days), news (24 h), risk
zones containing the point. Events: conflict events with severity ≥ 4, news ≥ 6.5, convergences, protests ≥ 6.5 with
3+ sources, travel-advice changes (30 days), news-spike episodes (30 days), Radar anomalies and ongoing hijacks of
confidence 10+. AI tools: `country_profile`, `conflict_events`, `news_search`, `convergences`, `travel_advice
{country?, days?}`, `internet_anomalies {country?, min_confidence?}`, `protests {country?, bbox?, kind?, days?,
min_severity?, limit?}`, `news_spikes {country?, days?}`. Browser AI action: `intel_show_theatre {id}`.

## Storage and bounds
SQLite tables `intel_files` (45 days; `tallies` 2 = also counted for protests and hours), `intel_events` (14 days),
`intel_protests` (14 days), `intel_news` (3 days), `intel_cell_day` (8 days), `intel_country_day` (45 days; `protest_rows`
NULL for days before protests were counted), `intel_country_hour` (3 days), `intel_domain_hour` (2 days, the 3 busiest
domains per country, hour and file), `intel_spikes` (90 days), `intel_advice` (current advice), `intel_advice_changes`
(365 days), pruned hourly. Seed progress in the kv store (`intel.seed`). Raw downloads in `data/raw/intel/` (about 60 MB: GED zip, candidate
CSVs, Natural Earth, DeepState, Feodo); older GED versions are deleted on update. GDELT zips are processed in memory.

## Settings
`INTEL_BACKFILL_HOURS` (non-secret, default 48), `INTEL_SEED_DAYS` (non-secret, 0-30, default 16; 0 turns the seed off),
`INTEL_SEED_MAX_MB` (non-secret, default 150), `UCDP_ACCESS_TOKEN` (optional fallback), `CLOUDFLARE_RADAR_TOKEN`
(optional: Radar anomalies, outage reports, BGP hijacks and leaks; without it the layer and cards say it needs the token).

## Colours
Single-hue sequential ramps validated for the dark map (monotone lightness, one hue): conflict severity (red),
news severity (teal), instability index (orange, opacity also rises with the band so stable countries recede), risk
weight (yellow). UCDP type of violence uses three categorical slots validated all-pairs on the dark surface. Ordinal
ramps validated with `--ordinal` on `#111a2c`: travel advice levels 1-4 in violet (`#5a4cb8`, `#7566d6`, `#9085e9`,
`#b9b0f5`, opacity 0.05 to 0.34 so normal-precaution countries recede), protest kinds in magenta (`#a8457a`
demonstration, `#d55181` strike or blockade, `#f59ac0` riot; diamonds, so shape also separates them from conflict
circles), BGP confidence 8, 10, 12 in steps of the teal news ramp.

## Known gaps
* Front lines exist for Ukraine only. Wikipedia "detailed map" modules (CC BY-SA) cover Myanmar, Sudan, Mali,
  Somalia, Yemen and others, but their colours map to parties only through prose legends on article pages, so they
  cannot be read robustly; not used.
* GDELT is machine-coded and English-heavy: metaphors, anniversaries and court reports become "fighting", and big
  media markets get many events. Focus, conflict-share weighting and the crime flag reduce but do not remove this.
* DOC headlines are rate limited from shared networks; the feeds then rely on headlines rebuilt from links.
* The GDELT GEO 2.0 API is retired and not used. ACLED would add verified events but needs registration and has
  restrictive terms; not used.
* Feodo currently lists very few servers, so the cyber layer is sparse.
* The FCDO text reading is deliberately narrow: advice that covers a whole country through a list of regions without
  saying "the rest of" stays at level 2. The US list gives one national level (its regional "do not travel" areas are
  only in the summary text).
* BGP hijack detections include false positives (moves, siblings, customers); cards show confidence, tags and
  "Why this may be innocent". Leak volume is high (about 400 a day) and mostly harmless.
* News spikes need 14 complete days; GDELT's English-heavy machine coding still applies, and the guards reduce but do
  not remove single-story spikes. The research's hourly same-window baseline and theatre-level spikes are not built:
  baselines use whole UTC days so stored history counts at once.
