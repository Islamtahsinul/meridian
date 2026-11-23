# Ships module

Live AIS vessel tracking for Meridian: one vessel store keyed by MMSI, merged from three
independent providers, with dark-ship estimates, sanctions matches, a straits monitor, port
activity, maritime anomaly candidates (ship-to-ship transfers, loitering, AIS gap episodes, GNSS
spoofing) and IMF PortWatch's daily chokepoint transits beside Meridian's own strait counts.
Backend `meridian/modules/ships/`, browser `web/js/layers/ships.js` (+ `ships/*.js`), styles
`web/css/ships.css`, tests `tests/test_ships*.py`, port 8792 for solo runs (8845 for the Maritime+
team). The 72-hour history for timeline replay is `history.py` (tests in `tests/test_timeline.py`); the
anomaly detectors are `anomalies.py` with `aoi.py` (places) and `land.py` (coastline); PortWatch is
`portwatch.py`.

## Sources and licences

| Source | What | Access | Licence and terms |
|---|---|---|---|
| Fintraffic Digitraffic marine AIS | Finnish waters and the northern Baltic (about 57.5 to 66 N, 15.5 to 33 E): Gulf of Finland, Gulf of Bothnia, Archipelago and Åland seas, Saimaa | Keyless REST, `Digitraffic-User: open-data-map-client` header | CC BY 4.0, "Source: Fintraffic / digitraffic.fi" |
| aisstream.io | Community receivers worldwide (coastal; open ocean is sparse) | WebSocket, free key `AISSTREAM_API_KEY` | aisstream.io terms of service; keys stay server-side, browsers never connect directly |
| Kystverket (Norwegian Coastal Administration) | Norwegian coastal AIS network | Raw NMEA on TCP `153.44.253.27:5631`, off unless `KYSTVERKET_AIS=1` | NLOD 2.0 (Norwegian Licence for Open Government Data) |
| Sanctions lists via the core `sanctions` module: US OFAC SDN list, UK Sanctions List, UN Security Council list, EU financial sanctions list | Listed vessels by IMO, MMSI and name | Downloaded by core | OFAC public domain; UK OGL v3.0; UN terms (personal, non-commercial); EU reuse notice |
| IMF PortWatch | Daily transits for 28 chokepoints by ship type, derived by the IMF from satellite AIS; weekly, about a week behind | Two public ArcGIS feature services on `services9.arcgis.com`, keyless, small filtered queries only | IMF terms: reuse and redistribution with attribution to the IMF; "The IMF prohibits bulk download of information by automated technology without explicit permission" |
| Natural Earth admin-0 countries, 1:50m (built by the intel module as `data/build/intel/countries.geojson`, read-only) | Land and coastline for the anomaly detectors (on land, distance offshore) | Local file, optional | Public domain |
| infra `data/build/infra/ports.json` (CONTRACT 9.1, read-only) | Ports for exclusions, nearest-port wording and destination matching | Local file, optional (built-in list otherwise; the built-in list is also always used for destinations) | As built by infra (World Port Index, UN/LOCODE) |
| gpsd regression samples | Public AIVDM sentences used only as unit-test fixtures | `tests/fixtures/ships/nmea_samples.txt` | BSD (gpsd project) |
| PortWatch fixtures | Trimmed real answers (4 chokepoints, Hormuz 1 to 15 November 2026, 1-year averages) | `tests/fixtures/ships/portwatch_*.json` | IMF terms, with attribution |

Vessel pages on MarineTraffic and VesselFinder are links only; nothing is fetched from them.

### Digitraffic: getting the whole fleet

`GET /api/ais/v1/locations` without parameters returns the latest position of every vessel heard
in the last 24 hours (about 780 vessels, 326 KB of JSON). The "31 KB" seen in the first probe was
the gzip-compressed size of that same answer, so the default call already is the full fleet;
`from` (epoch milliseconds) only narrows it (`from` = 7 days gives about 1,360 vessels, mostly
stale). Meridian polls the default 24-hour window once, then asks only for positions newer than
the newest one it has (`from = newest - 90 s`), which keeps each poll small. Static data
(`/api/ais/v1/vessels`) is fetched on start and then every 10 minutes with `from`. Their CDN caches
for 60 s, so polling faster is pointless. The optional MQTT-over-WebSocket feed is not used.

## Settings

| Name | Secret | Meaning |
|---|---|---|
| `AISSTREAM_API_KEY` | yes | Enables the aisstream.io stream. Without it the source shows "Needs AISSTREAM_API_KEY". |
| `AISSTREAM_AREA` | no | `world` (default), `straits` (boxes around the monitored straits only) or custom boxes `west,south,east,north;...` (max 50). |
| `KYSTVERKET_AIS` | no | `1` reads the Norwegian TCP stream. Blocked on some networks; it then shows an error and retries with backoff up to 15 minutes. |
| `SHIPS_HISTORY_MAX_ROWS` | no | Most `ships_track` points kept (default 2,500,000, about 100 MB); the oldest go first. Checked every 15 minutes (timeline replay). |
| `SHIPS_PORTWATCH` | no | `0` stops the IMF PortWatch fetch (on by default: about 30 small requests a week). |

## Ingestion, refresh and why it runs continuously

Dark-ship detection, strait transit counts, port arrivals and the anomaly detectors need an unbroken
history, so ingestion continues while the browser is closed (contract rule 8 exception). To stay
polite, Digitraffic is polled every 60 s while the map is in use and every 2 minutes otherwise.
aisstream.io is a single long-lived WebSocket (their limit is 3 connections per key), negotiated with
permessage-deflate because the service bandwidth-limits uncompressed connections; it reconnects with
exponential backoff and jitter (5 s to 10 minutes, 30 minutes after a refused key). Positions from the
stream are thinned to one fix per vessel every 10 s before they reach the store.

Bandwidth: Digitraffic is about 30 KB (gzip) for the first poll and a few KB per minute after.
A `world` aisstream subscription can carry hundreds of messages a second; use
`AISSTREAM_AREA=straits` or custom boxes if that is too much.

IMF PortWatch is weekly data, fetched whether or not the map is open (another rule 8 exception: it
changes weekly and the cross-check needs an unbroken daily series). It makes at most one request every
three minutes and about 30 a week: the chokepoint list once a month (about 20 KB), one server-side
statistics query a week for the 1-year averages (28 rows), and one filtered query per chokepoint a
week (`portid = '...' AND date >= DATE '...'` with a short field list; the first fill reads 120 days,
about 6 KB, and later fills start 14 days before the latest day held). Host spacing is 20 s, the
timeout 60 s (the host is slow: 7 to 14 s a query), and failures back off from 15 minutes to 6 hours. While backing off, the Sources note says what is
stored and when the next try is ("Fetching is paused after the failure; next try in about 30 min.").
The first fill starts with the Strait of Hormuz, then the chokepoints Meridian also monitors.

## Vessel store and retention

In memory: one compact record per MMSI (`__slots__`), a live snapshot rebuilt every 10 s (immutable,
swapped atomically, with a 1-degree grid index for bbox and radius queries). SQLite tables:

| Table | Contents | Kept |
|---|---|---|
| `ships_vessels` | latest position, static data, motion state | 7 days after the last report |
| `ships_track` | trail points: at least 2 minutes apart; every 10 minutes on a straight course, more in turns, every 2 hours when stationary; also the timeline's vessel history | 72 hours, at most `SHIPS_HISTORY_MAX_ROWS` points |
| `ships_calls` | stops of 20 minutes or more (arrival, departure, position) | 7 days |
| `ships_gaps` | AIS gap episodes of vessels flagged dark or quiet (start, end, positions) plus their context: speed, course, draught, destination, name and IMO before and after, area of interest, severity, and the episode and card content as JSON (`info`); older databases gain the columns by `ALTER TABLE` | 30 days |
| `ships_visits` | strait visits (in, out, class, aligned moving reports, direction score) | 8 days |
| `ships_samples` | hourly "inside now" counts per strait, for the congestion baseline | 8 days |
| `ships_sts`, `ships_loiter` | ship-to-ship and loitering episodes (pair or vessel, times, place, state, severity, the episode and the card's content as JSON) | 30 days |
| `ships_gnss`, `ships_gnss_days` | GNSS spoofing hotspots (first and last seen, centre, radius, vessels, signatures, aircraft cross-check) and the days each 0.5° cell had one | 90 days |
| `ships_stopcells`, `ships_loitercells` | learned usual places: one row per 0.05° cell, UTC day and vessel stopped for an hour or more outside ports; one per 0.1° cell, day and vessel that loitered | 15 days |
| `ships_pw_points`, `ships_pw_daily` | PortWatch chokepoints (name, position, 1-year averages, when each was fetched) and daily transits by type | list kept; days 400 days |
| `ships_strait_days` | Meridian's own daily tallies per strait (transits, cargo and tanker transits, vessels, hours with coverage), for same-day comparisons with PortWatch | 400 days |

`kv` keys: `ships.anomalies.days` (detector runs per UTC day, for the learning counters) and
`ships.portwatch.meta` (fetch schedule and last error).

Writes are batched every 60 s; pruning runs every 15 minutes (anomaly tables every 6 hours).
Positions are rejected when they are older than what we have, more than 2 days old, at 0,0, outside
the ship MMSI range (base stations, aids to navigation, SAR aircraft and beacons are ignored) or imply
more than 80 kn within 10 minutes.

## Classification and flags

AIS ship type: cargo 70-79, tanker 80-89, passenger 60-69, fishing 30, tug, pilot, SAR and law
enforcement 31, 32 and 50-59, military 35, sailing and pleasure 36-37, high-speed 40-49, everything
else "other or unknown". Colours were checked with a CVD validator on the dark water colour
(#2c353c): large-ship classes (cargo blue, tanker orange, passenger aqua) and small-craft classes
(fishing yellow, service violet, pleasure magenta) each form a triad that passes all-pairs checks;
the two triads use different icon shapes (hull arrow and dart), and size scales with length. Flags
come from the MMSI's Maritime Identification Digits (full ITU table in `mid.py`).

## Heuristics and their limits

**Dark ships** (`ships.dark`). A vessel is dark when it (1) is class A and not a sailing or
pleasure craft, (2) was under way at 3 kn or more and not anchored, moored or aground, (3) sent at
least 6 reports in the 30 minutes before going quiet, (4) has been silent for more than 2 hours and
at most 24, (5) was heard by a source that has been receiving continuously since before that last
report, and (6) coverage is proven: at least 3 other vessels within 30 km of its last position and
2 near its estimated position were heard in the last 20 minutes. A vessel whose newer fixes are being
dropped as impossible jumps is still transmitting and is never called dark. The estimate is dead
reckoning along the last course at the last speed for at most 3 hours; the uncertainty radius is
2 km + 35% of the distance it could have run, so the circle keeps growing after the estimate stops.
Severity 4, +1 for tankers or ships of 150 m and more, +1 after 6 hours or for sanctioned ships.
Limits: receiver outages, vessels leaving coverage between receivers, and transponders legitimately
switched off (port approaches, piracy areas, faults) all look the same. Nothing is flagged in the
first two hours after a restart.

**Suspect positions (GNSS interference).** A reported speed above 45 kn (except high-speed craft)
marks the fix as suspect, and so does a position that implies more than 80 kn from the previous one
after a pause of 10 minutes or more (inside 10 minutes such a fix is dropped, as before). Jumps of 5 km
or more at over 45 kn implied, between reports at most 30 minutes apart, are kept for half an hour,
dropped or not, for the spoofing signatures below. Inside an active hotspot every fix is suspect too,
but such fixes are not new evidence: only a fix that itself reported over 45 kn counts as speed
evidence (kept with its own time and position, for 30 minutes), so a hotspot cannot keep itself alive
or follow its vessels to their berths.
Suspect fixes are shown faded and never create trail points, strait transits, port calls or dark-ship
candidates. On the first day of testing this caught a live cluster in inland Russia (57.67 N,
32.53 E) with vessels "sailing" at 40-80 kn, a known spoofing signature.

**Sanctions** (`ships.sanctioned`). Matched with `sanctions.lookup_vessel` against the merged OFAC,
UK, UN and EU index: IMO first, then MMSI. An MMSI match whose listed IMO differs from the vessel's
IMO is downgraded. Name-only matches are "possible", amber, off by default on the map, and never
shown as listed. Matches keep the record's `lists`, `programmes`, `listings`, `listed_on` and `imos`,
and every vessel is matched again whenever `sanctions.version()` changes (any list reloading), not
only when the number of listed vessels does. Text names the lists ("On the UK Sanctions List
(UK: Russia), matched by IMO") and says "OFAC" alone only when OFAC is the only list involved
(CONTRACT 5.7).

**Straits** (`ships.straits`). Fourteen hand-drawn polygons: Gibraltar, Dover, Bosphorus,
Dardanelles, Suez, Bab-el-Mandeb, Hormuz, Malacca, Singapore, Panama, Øresund and Great Belt,
Taiwan, plus two Baltic chokepoints Digitraffic covers (Gulf of Finland between Porkkala and
Paldiski, Åland Sea). "Inside now" counts vessels heard inside in the last 30 minutes. A transit is a
visit with at least one report moving at 3 kn or more within 50 degrees of the strait's axis; its
direction is the majority of those reports. Ferries crossing the axis count as vessels, not
transits. Congestion: inside now at least 8 and 1.5 times the median of a week of hourly samples
(needs a day of samples first). Coverage is stated per strait; without aisstream.io only the two
Baltic straits have live data.

**Port calls and activity.** A call is a stop (under 0.5 kn) of 20 minutes or more, ended by moving
at 1 kn or more and 300 m. Arrivals count only stops whose start Meridian saw.

## Anomalies (`anomalies.py`, layer `ships.anomalies`)

Candidates for a closer look, never accusations. Every one says what was seen and when, how
confident the detector is and why, the receiver coverage behind it, its limits, why it may be
innocent, and the vessels involved with any sanctions-list match. No text calls anything illegal,
smuggling, evasion, suspicious or confirmed (a test checks the wording). Derived anomalies stay at
severity 7 or below. The detectors run with the analysis task every 60 s (loitering every 10
minutes) and cost about 0.04 s a run on 27,000 live vessels (1.5 s on loitering runs).

### Known benign patterns, excluded before anything is flagged

| Pattern | How it is recognised |
|---|---|
| Ports | within the port's radius plus 3 km (radius 12 km large, 8 medium, 5 small, 4 unsized), from infra `ports.json` or the built-in list |
| Anchorages, waiting and transfer areas | the built-in list (`aoi.USUAL_AREAS`, below); cells where 8 or more large vessels stopped for an hour or more in 14 days (0.05°, with neighbours); cells where 3 or more vessels loitered on 5 or more of the last 14 days (0.1°, with neighbours); and anywhere with 6 or more other stopped vessels within 3 km |
| Waiting for a berth | a loitering vessel whose reported destination (name or UN/LOCODE, for example `RUULU`, `UST-LUGA`, `FIPRV`) is a port within 50 km (about as far as anchorages reach) and no more than 25 km further than the nearest listed port. The destination is crew-set free text, so it never clears a vessel in an area of interest or with a sanctions match: those stay candidates and list it as a possible reason |
| Pilot boarding areas | a pilot vessel (AIS type 50) heard within 3 km in the last hour |
| Offshore installations, floating storage | either vessel stationary for 7 days or more, or such a vessel within 3 km (5 km for loitering); offshore terminals in `ports.json` are ports |
| Close inshore | on land or within 1 km of the coast (Natural Earth), when the land shapes are built |
| Classes that do this for a living | fishing, service craft (tugs, pilots, SAR, law enforcement), pleasure craft, passenger ships (scheduled traffic) and military vessels (training and patrol) never qualify; class B transponders do not either |

Built-in usual areas (circles, deliberately generous; the Gulf of Finland ones were drawn from
Meridian's own AIS in November 2026, the others are widely published bunkering and transfer
anchorages): Ust-Luga outer anchorage; the waiting area between Ust-Luga and Primorsk (59.95 N,
28.60 E, 16 km); Primorsk, Vysotsk and Kronstadt anchorages; Singapore's eastern anchorages and the
Eastern Outer Port Limits; Fujairah and Khor Fakkan; Hurd's Bank off Malta; the Laconian Gulf transfer
anchorage; Lomé offshore anchorage; Southwold; Skagen; Algoa Bay.

Several of these circles cover much of an area of interest (two thirds of the Laconian Gulf's open
water, about two fifths east of Johor). So "nothing flagged" there does not mean "no transfers":
ship-to-ship pairs excluded by place inside an area of interest are kept as unflagged items (state
`usual`, severity 2, the reason first among the innocent explanations, seen with "Also show what was not
flagged" or `include_unflagged`), a usual anchorage never clears a pair with an IMO or MMSI sanctions
match, and the detector status (`excluded_waters`, and `not_flagged_by_place` counts in the AI tool)
says which waters are excluded.

### Ship-to-ship transfer candidates (`sts_candidate`)

Rule: two vessels both reporting under 2 kn within 500 m of each other for 30 minutes or more, in open
water. Candidates are class A tankers, cargo ships (60 m or more when the length is known) and other
vessels of 100 m or more; at least one must be a tanker or have an IMO or MMSI sanctions match; neither
may have had a suspect GNSS fix in the last 2 hours. Positions may be up to 30 minutes old, so a pair
survives one vessel going unheard for that long; a pair more than 1 km apart with fresh positions
ends at once. The episode keeps first and last time, closest and average distance, the largest silence
between reports of each vessel, and each vessel's draught at the start and for 24 hours after the end.

Severity: 3, or 4 when together for 2 hours or more; +1 in an area of interest; +1 if either vessel has
an IMO or MMSI sanctions match; +1 for a draught change of 0.8 m or more; +1 if either went quiet for
20 minutes or more, or has an AIS gap episode within 2 hours; at most 7. Confidence: high for 2 hours or
more with reports 300 m or less apart and at least 20 observations; medium for an hour or more, or
300 m or less ("probably alongside"); otherwise low. When the smaller vessel is a tanker under 80 m (the
size of many bunker barges; bunker tankers can be larger, so this is a hint only) the pair is labelled
"possible bunkering" (severity 2, not flagged, shown only with "Also show what was not flagged", worded
"may be bunkering"), except when either vessel has a sanctions match or the pair is in an area of
interest: then it stays a candidate and bunkering is listed as a possible innocent reason. Event: kind
`ship`, id the MMSI of the tanker or larger vessel.

Measured on the dev copy (27,750 live vessels): 6,295 pairs of slow vessels within 500 m, 513 with
eligible classes, 251 with a tanker, 27 outside port radii, every one of them in Singapore's crowded
eastern anchorages or a bunkering pair off Tanger Med. No open-water candidate, as the research
expected (a few a day worldwide at most).

### Loitering (`loitering`)

Rule, over the last 12 hours of the vessel's track (the recent stretch it spent within 10 km of its
latest position): 8 or more points over 6 hours or more, radius of gyration 5 km or less, mean speed
0.5 to 6 kn (fully stopped is anchoring), path at least 10 km and at least three times the net
displacement. Classes as for ship-to-ship (60 m or more); vessels at anchor for 6 hours within 1 km of
their stop are skipped. Severity 3, +1 in an area of interest, +1 for a sanctions match, at most 5.
Confidence by length and density of the track.

Usual places are labelled, not flagged ("It is in the waiting area between Ust-Luga and Primorsk,
where ships routinely wait"; "It reports Ust'-Luga as its destination, 34 km away"). Every loiter-shaped
track is also learned into `ships_loitercells`. **Learning:** loitering is not flagged until Meridian
has seen 7 days with at least 6 hours of checks; until then candidates are shown on the layer and
cards as "Learning usual waiting areas: n of 7 days" and no event is published. On the dev copy every
loiterer (10, all in the eastern Gulf of Finland and off the Finnish coast) was classed as usual, as the
research required.

### AIS gap episodes (`ais_gap`)

Built on the dark-ship detector: when a vessel flagged dark reappears, the store records the gap with
what the vessel looked like before (speed, course, draught, destination, name, IMO) and the coverage
proof at the moment it was flagged (other vessels heard within 30 km). The episode then has the
duration, the distance moved, the implied speed and the reading `r = distance / (last speed x hours x
1.852)`, capped at 2 (0.7 to 1.3 "sailed on through a coverage hole", under 0.3 "stopped or stayed
near the same place"), draught, destination, name and IMO after the gap (statics are watched for 2
hours), a stopped tanker within 1 km of where it went silent, and coverage along its path: other
vessels' stored track points within 30 km of where it should have been, in up to 8 half-hour checks
spread across the gap (its expected position in each check is on the straight line between where it
went silent and where it reappeared, at the same fraction of the gap; the real route is unknown). A second watcher covers stopped vessels: a class A tanker or cargo ship stopped outside
ports and anchorages in an area of interest, reporting regularly, then silent for more than 3 hours
while 3 or more vessels within 30 km keep reporting (ongoing gaps are shown as such).

Gaps are published only when the start, the end or the line between them touches a monitored strait
(or just outside its outline) or an area of interest below; others are kept and shown as "outside
areas of interest". Severity 3, +1 area of interest, +1 stopped, +1 draught change, +1 sanctions match,
at most 6. Confidence: high when coverage along the path held in 80% of the checks with 5 or more
neighbours, medium when proven at the start, low when few vessels were heard along the path. A gap read
as "sailed on through a coverage hole" without coverage proven along the path is what a hole between
receivers looks like: it is kept as `coverage_hole` (low confidence, severity 2 or less, not published).

### GNSS spoofing clusters (`gnss_interference`, kind `gps_jam`)

Each run groups the last 30 minutes of suspect positions (fixes over 45 kn and the jumps above, at most
the latest 4 jumps per vessel so a spoofing event that fills the 4,000-jump buffer stays cheap) that lie
within 30 km of each other and looks for signatures:

- speed: 2 or more vessels report impossible speeds within 10 km (the old rule);
- jump: 3 or more vessels jumped 5 km or more to within 1.5 km of a common point;
- circle: 8 or more distinct suspect positions (one vessel's positions within 0.5 km of each other count
  once) from 3 or more vessels lie at nearly equal distances from a centre (least-squares circle, worst
  deviation under 8% of the radius, radius 1 to 50 km, no angular gap over 120 degrees). With 4 points
  a circle fits random scatter about a quarter of the time; the circle is a shape of the same points the
  other signatures use, so it does not count as a separate signature for confidence;
- land: 3 or more vessels jumped from the sea to 3 km or more inland (river and lake craft, whose jumps
  start inland, never count).

A hotspot lasts 6 hours after its last evidence, keeps a stable id (`ships:<lat>,<lon>` of its first
centre) and makes every fix inside it suspect. It is cross-checked every 10 minutes against aircraft GPS
interference cells (flights' `gps_jam` events in the core event history, read-only). Confidence high
with aircraft corroboration or two signatures (not counting the circle) and 3 or more vessels, medium
with 3 or more vessels or two such signatures, low otherwise; severity 6, 5 or 4. Cards say "seen on n of the last 14 days" from
`ships_gnss_days` (and how many days of history exist so far). Benign look-alikes: two ships sharing an
MMSI (one vessel jumping back and forth: needs several vessels), faulty units and data errors.

### Areas of interest (`aoi.AREAS_OF_INTEREST`)

The fourteen strait outlines plus these waters, each named in public government advisories or
reporting about ship-to-ship transfers, AIS manipulation or sanctions-related shipping. Being in one
never makes a vessel suspect: it adds context and +1 severity to something flagged for other reasons,
and it decides whether an AIS gap is published. The citations below were written from the team's
knowledge of public documents and could not be re-read when this was written; check the current versions
before relying on them.

| Area | Public basis |
|---|---|
| Approaches to Primorsk and Ust-Luga (eastern Gulf of Finland) | Russia's main Baltic crude and product terminals; UK and EU shadow-fleet sanctions designations (2024, 2025) name many tankers that load there; Finnish and Estonian authorities' public statements on monitoring such tankers |
| Kerch Strait and the approaches to the Sea of Azov | US Maritime Administration (MARAD) maritime security advisories for the Black Sea and Sea of Azov (2022 onwards); reporting on transfers near Kavkaz |
| Laconian Gulf | 2022 and 2023 reporting (Reuters, Lloyd's List) on ship-to-ship transfers of Russian oil; Greek naval exercise notices there in 2023 |
| Waters east of Johor | 2023 and 2024 reporting (Reuters, Bloomberg); Malaysian Marine Department statements (2023) on unauthorised transfers |
| Gulf of Oman off Fujairah and Khor Fakkan | OFAC advisory to the maritime petroleum shipping community (4 September 2019); OFAC, State Department and US Coast Guard "Sanctions Advisory for the Maritime Industry, Energy and Metals Sectors, and Related Communities" (14 May 2020) |
| Waters off Kharg Island | the same OFAC advisories on Iranian petroleum shipments (2019, 2020) |
| Syrian coast off Baniyas | OFAC advisory "Sanctions Risks Related to Petroleum Shipments involving Iran and Syria" (20 November 2018, updated 25 March 2019) |
| Venezuelan coast off Jose | US Treasury (OFAC) Venezuela-related designations of tankers (2019, 2020) and reporting on transfers of Venezuelan oil |
| Yellow Sea west of Nampo | UN Security Council Panel of Experts (resolution 1874) reports, 2018 onwards; "North Korea Sanctions Advisory: Updated Guidance on Addressing North Korea's Illicit Shipping Practices" (21 March 2019) |

### Learning and uptime

Learned places need history: usual anchorages count after 14 days of stop learning (status "Learning
usual anchorages: n of 14 days"; built-in and crowded anchorages are excluded meanwhile, so ship-to-ship
works at once) and loitering waits for 7 days (above). A day counts only when the detector ran for at
least 6 hours of it, so time when Meridian was off does not count as quiet. Episodes of the last 48
hours are restored after a restart.

## IMF PortWatch (`portwatch.py`)

Meridian strait to PortWatch chokepoint: Suez (1), Panama (2), Bosphorus (3), Bab-el-Mandeb (4),
Malacca (5), Hormuz (6), Gibraltar (8), Dover (9), Øresund and Great Belt (10, the Sound only), Taiwan
(11). Dardanelles, Singapore Strait and the two Baltic straits have no PortWatch counterpart; the other
18 PortWatch chokepoints (Cape of Good Hope, Korea, Tsugaru, Luzon, Lombok, Ombai, Bohai, Torres, Sunda,
Makassar, Magellan, Yucatan, Windward, Mona, Balabac, Bering, Mindoro, Kerch) appear in the drawer and
open a PortWatch-only card (entity kind `strait`, id `portwatch:<chokepoint id>`).

Per chokepoint: the latest day and its lag, means over the latest 7 and 28 days (missing days are left
out, not counted as zero), the 1-year average (computed by PortWatch's server), the change against it,
transits by type, missing and zero days, and flags (`far_below` under 35% of the 1-year average,
`far_above` over 2.5 times, `missing_days` 3 or more of 28, `stale` over 21 days old). Meridian keeps its
own daily tallies per strait from the strait visits and hourly samples (`ships_strait_days`: transits,
cargo and tanker transits, vessels, hours with coverage), so the same days can be compared once both
exist. An hour counts as covered only when vessels were heard inside the strait itself (an hourly
sample with vessels inside, or a visit that began then): the samples are written whenever anything is
heard within about 55 km, which for the Bosphorus can be the Sea of Marmara alone. A day with no vessels
inside reads "Meridian heard no vessels inside the strait itself", not a count of 0, and the drawer's
"Meridian 24 h" column shows "none inside". Malformed PortWatch days (for example 2026-11-31) are
skipped when parsed and when read back, and a failing summary leaves the straits layer up without its
PortWatch brief.

The cross-check says plainly when either source has gaps. For the Strait of Hormuz on 23 November
2026: "PortWatch counted 2 transits on 15 Nov; 2.3 a day over its latest 7 days, against 22.3 a day over
the past year. That is 90% below PortWatch's own 1-year average. PortWatch does not say why: traffic may
really have fallen, or its satellite AIS may be missing ships... Meridian hears no terrestrial AIS here,
so it cannot check PortWatch's figures: neither source can be confirmed against the other."
(PortWatch read 51 to 78 transits a day for Hormuz in early November 2025 and 2 to 11 in early June
2026.) PortWatch publishes no events: the data is a week old and its lows are unexplained.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/ships/live?bbox&zoom&types&moving` | `{rows: [[mmsi, lon, lat, dir, cls, flags, sog10, size, name, age, n]], fields, classes, in_view, shown, thinned, total, note?}`; dir -1 = stationary; flags 1 sanctioned, 2 possible, 4 class B, 8 stale, 16 suspect; thinned to one vessel per ~5 px cell (max 6,000) above 2,500 vessels |
| `GET /api/ships/vessel/{mmsi}` | full record (identity, voyage, position, state, sanctions with `lists`, dark, suspect, stops, gaps, `anomalies` involving it, links); `known: false` with flag when unseen for 7 days |
| `GET /api/ships/trail/{mmsi}?hours=` | `{points: [[lon, lat, t, sog]], geometry}` (up to 24 h) |
| `GET /api/ships/straits[?geometry=0]` and `/api/ships/straits/{id}` | status of every strait with a `portwatch` brief; the detail adds `vessels` inside and `portwatch` (90-day series, summary, `cross_check {lines, pw_gap, meridian_gap, overlap}`, Meridian's own days, credit) |
| `GET /api/ships/port-activity?lat&lon&radius_km` | CONTRACT 9.1: `{moored, anchored, underway, arrivals_24h, departures_24h, vessels, coverage}` where `coverage` is a sentence; also `coverage_ok`, `sources`, `by_class`, `total` |
| `GET /api/ships/ports?limit=` | busiest ports (infra `ports.json` when built, otherwise a built-in list of about 110 ports); each vessel counts for one port |
| `GET /api/ships/dark?bbox=` | dark ships with last and estimated positions |
| `GET /api/ships/sanctioned?possible=1` | matched vessels (with `lists` and `programs`); `lists` loaded and a `note` naming them |
| `GET /api/ships/sdn/{uid}` | one listed vessel (entity `sanctioned_vessel`, any list's id) with `lists`, `programmes` and `listings`, and the live vessel it matches |
| `GET /api/ships/anomalies?bbox&kinds&hours&all` | `{anomalies, count, flagged, detectors {sts, loitering, ais_gap, gnss: {state, status, flagged, unflagged, excluded, last_run, error}}, note}`; `kinds` is `sts,loitering,ais_gap,gnss` (category names work too), `hours` 1 to 720 (default 48; beyond 48 hours read from the tables), `all=1` adds what was not flagged |
| `GET /api/ships/anomalies/{id}` | one anomaly: `id, kind, category, title, subtitle, lat, lon, first_time, time, ongoing, severity, confidence, confidence_text, observed, evidence [[label, value]], innocent [..], coverage, limits, vessels [entities with role and sanctions], aoi, state, published, geometry, block`; ids `sts:<a>-<b>:<t0>`, `loiter:<mmsi>:<t0>`, `gap:<mmsi>:<t0>`, `gnss:<lat>,<lon>` |
| `GET /api/ships/chokepoints?series=` | the 28 PortWatch chokepoints with summaries, Meridian's counts where it monitors them and the cross-check; `series` adds up to 90 days |
| `GET /api/ships/chokepoints/{id}` | one chokepoint (`chokepoint6`) with 90 days and the cross-check |
| `GET /api/ships/status` | counts, provider stats and states (PortWatch included), GNSS hotspots (with signatures), the detectors' states |
| `GET /api/ships/history?from&to&bbox&limit&format` | CONTRACT 15.4, for timeline replay: track points `{mmsi, t, lat, lon, sog, cog, cls}` with `from <= t <= to`, oldest first, plus points not written yet and (for windows reaching the last 15 minutes) each vessel's latest fix; `idents {mmsi: {name, cls, length}}`, `count`, `truncated`, `available {from, to}`. `from`/`to` epoch seconds (ms accepted; default the last hour, at most 6 h a request), `limit` 1 to 50,000 (default 20,000), `format=rows` gives `fields` + `rows` arrays. `cls` and the name are the vessel's current ones |
| `GET /api/ships/history/{mmsi}?from&to` | One vessel's points in the window (default and at most 72 h), with `name` and `cls` |
| `GET /api/ships/history/coverage?from&to` | Stored points per UTC hour `{by_hour: [{t, samples}], available}` (the time bar's coverage strip) |

The three history routes are registered with `activity=False`: replay reads never mark the module
active, so they do not speed up live polling.

Entity kinds: `ship` (id MMSI), `strait` (id `ships:<id>`, or `portwatch:<chokepoint id>` for PortWatch-only
chokepoints), and the browser also opens `sanctioned_vessel` records from search and `gps_jam` entities
whose id starts with `ships:` (the flights module keeps every other `gps_jam` id).

## Events

| Event | Kind and id | Category | Severity |
|---|---|---|---|
| Dark ship | `ship`, MMSI | `dark_ship` | 4 to 6 |
| Sanctioned vessel seen | `ship`, MMSI | `sanctioned_vessel` (also `lists`) | 6 |
| Possible ship-to-ship transfer | `ship`, MMSI of the tanker or larger vessel | `sts_candidate` | 3 to 7 |
| Loitering vessel | `ship`, MMSI | `loitering` | 3 to 5 |
| AIS gap near a strait or area of interest | `ship`, MMSI | `ais_gap` | 3 to 6 |
| Possible GNSS spoofing of ships | `gps_jam`, `ships:<lat>,<lon>` | `gnss_interference` (also `signatures`, `radius_km`) | 4 to 6 |
| Unusual strait congestion | `strait`, `ships:<id>` | `strait_congestion` | 4 to 5 |

Anomaly events also carry `anomaly_id` and `confidence`, and their layer is `ships.anomalies`. The core
event history keeps one row per kind and id, so a vessel that is both sanctioned and part of a
ship-to-ship candidate shows its most severe report there; the anomaly itself stays in
`/api/ships/anomalies`.

AI tools: `ships_in_area`, `vessel_details`, `strait_status` (now with a PortWatch brief per strait),
`dark_ships`, `ship_anomalies {bbox?, kinds?, hours?, include_unflagged?}` (candidates with evidence,
coverage, why they may be innocent and the vessels with any sanctions match) and `chokepoint_transits
{chokepoint?, days?}` (PortWatch daily counts with Meridian's and the cross-check); browser map action
`show_vessel_trail {mmsi}`.

## Browser

Layers in the Ships group: Vessels (`ships`, on by default, refreshed every 30 s and on every map
move, with a type filter, "moving only" and names from zoom 9), Dark ships, Sanctioned vessels
(optional name-only matches), Straits monitor (outlines at regional zoom, rings at world zoom) and
Maritime anomalies (`ships.anomalies`: a solid line joins a ship-to-ship pair, dashed circles and the
track show loitering, a dotted line the silent stretch of an AIS gap, a red dashed circle a GNSS
hotspot; options switch each detector and "Also show what was not flagged", drawn grey).
Cards: ship (data block of speed, course, heading and draught; identity, voyage, position,
sanctions, dark estimate, suspect position, recent stops, AIS gaps, anomalies involving it, nearby,
trail toggle, links), strait (with "PortWatch daily transits (satellite AIS)": a 90-day line with the
1-year average dashed, readable by hover or arrow keys, the figures, the cross-check in plain words and
a "Daily figures" table beside Meridian's own counts), PortWatch-only chokepoint, anomaly (what was seen,
evidence, vessels involved with sanctions badges, how confident, coverage, why this may be innocent,
limits, the area of interest's public basis, and "Show trails"), port activity and listed-vessel
record. The Maritime toolbar button opens a drawer with straits, chokepoints (PortWatch beside
Meridian), busiest ports, anomalies (flagged candidates and each detector's learning state), dark
ships, sanctioned vessels and source health. Right-click "What's here?" adds a Shipping section inside a
strait.

## Known gaps

- Without an aisstream.io key, coverage is the northern Baltic only; twelve of the fourteen straits
  then say "no coverage". Kystverket could not be tested (TCP 5631 was blocked on the test network).
- Terrestrial AIS only: no satellite AIS, so open-ocean tracks have holes and dark-ship and gap checks
  only work near receivers. Hormuz, Bab-el-Mandeb, Suez and Malacca had no coverage in the research.
- Strait polygons are approximations; transit direction relies on reported course.
- Flags come from the MMSI (what the vessel claims), not from registries.
- The Digitraffic MQTT stream would give second-by-second updates in the Baltic; polling once a
  minute is enough for ships, so it is left out.
- Anomaly thresholds were tuned on a day of mostly Baltic data; the research's acceptance (at most 20
  flags a day worldwide after 7 days of home data) still needs a week on the always-on instance.
- No "Not interesting here" dismissals yet: the core dismissals API (P1) does not exist; when it does,
  the anomaly card should offer it.
- Identity changes (one IMO under a new MMSI) are shown inside gap episodes only, not as their own
  detector; the research found them mostly innocent.
- The land check uses simplified 1:50m country shapes: country borders count as coast (so an inland
  point near a border reads as close to the sea) and large lakes read as land.
- Designated lightering zones not in the built-in list (for example in the US Gulf) become usual areas
  only after two weeks of learning.
- PortWatch is a week behind and its Hormuz lows are unexplained; Meridian cannot check them without
  coverage there. Same-day comparisons start a week after Meridian begins keeping daily tallies.
- On 23 November 2026, between about 17:45 and 19:20 UTC, PortWatch's `Daily_Chokepoints_Data` service
  stopped answering publicly: every query (and its layer description) now returns ArcGIS error 499
  "Token Required", and it no longer appears in the organisation's public service list, while the
  chokepoint list (`PortWatch_chokepoints_database`) still answers. It may be a weekly republish or a
  permanent change. Meridian never signs in (CONTRACT rule 1): it reports "PortWatch's daily
  chokepoint service is not public right now" in Sources, the drawer and the cross-check, backs off from
  15 minutes to 6 hours, and resumes by itself if the service comes back. If it stays private, the
  owner could ask the IMF for access or the feature should be retired.
