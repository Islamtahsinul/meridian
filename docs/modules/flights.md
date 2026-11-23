# Flights module

Live ADS-B aircraft for Meridian: the traffic on screen, military aircraft and emergencies worldwide,
OFAC-listed aircraft, trails, airport traffic for the infra module, and a GPS interference layer
derived from aircraft-reported navigation accuracy. Airspace and airport status: US airport delays
(FAA NAS Status), US temporary flight restrictions (FAA) and EASA conflict-zone advisories. Air
activity: military surges by region, GPS interference trends and airport movement drops, each against
Meridian's own baseline for the place and time (see "Air activity" below).

Files: `meridian/modules/flights/` (backend; airspace in `airspace.py`, `airspace_api.py`,
`airspace_parse.py`, `airspace_data.py`, `shapes.py`; replay history in `history.py`; air activity in
`activity.py` (tallies, samplers, episodes, events), `activity_rules.py` (pure rules and wording),
`activity_api.py` (routes, AI tools) and `activity_data.py` (regions, roles, airports)),
`web/js/layers/flights.js` and `web/js/layers/flights/*.js` (browser; airspace in `airspace.js`, `delays.js`,
`restrictions.js`, `advisories.js`; air activity in `activity.js`), `web/css/flights.css`, `tests/test_flights.py`,
`tests/test_flights_service.py`, `tests/test_flights_airspace.py`, `tests/test_flights_activity.py`,
`tests/fixtures/flights/` (replay history: `tests/test_timeline.py`).

## Sources and licences

| Source | Used for | Terms | Limits we keep to |
|---|---|---|---|
| [adsb.fi open data](https://adsb.fi/) | Map tiles, military, squawk searches, hex lookups, GPS hotspots | Personal, non-commercial use; cite adsb.fi with a link | 1 request per second (enforced per host, 1.1 s spacing); no invalid requests |
| [adsb.lol](https://www.adsb.lol/) | Fallback for tiles and worldwide sets, trail history (`/1/trace/recent`) | ODbL 1.0, attribution required | Unkeyed traffic is metered at 500 requests a day per IP: we budget 300 a day (900 with `ADSBLOL_API_KEY`) and stop when its `x-ratelimit-remaining` falls under 40 |
| [OpenSky Network](https://opensky-network.org/) | Fallback for tiles | OpenSky terms of use, non-commercial | Anonymous 400 credits a day (we budget 350), 4,000 with an API client (we budget 3,600); boxes cost 1 to 4 credits by area |
| Local receiver (`LOCAL_ADSB_URL`) | Your own readsb or tar1090 `aircraft.json` | Your data | Every 5 s while in use, 30 s otherwise |
| [adsbdb](https://www.adsbdb.com/) | Routes (shown as "route, unverified") and aircraft details for cards | Free public API | Only when a card opens; cached a day in memory and SQLite (misses 6 h) |
| OFAC SDN list (core sanctions module) | OFAC-listed aircraft | US Government public domain | In-process lookups |
| [FAA NAS Status](https://nasstatus.faa.gov/) `api/airport-events` (JSON) | US airport delays, closures, notices, de-icing, runway configuration | US Government work, public domain | One request every 2 min while the delays layer is in use, every 15 min otherwise; 2 s host spacing; cached 50 s |
| [FAA TFR site](https://tfr.faa.gov/): `tfrapi/exportTfrList`, GeoServer WFS `TFR:V_TFR_LOC`, `download/detail_<n>.xml` | Temporary flight restrictions: list, polygons, and each notice (XNOTAM: times, altitudes, purpose, text) | US Government work, public domain | List and polygons every 5 min in use, 30 min otherwise; each notice fetched once (kept in SQLite) and again only when the FAA modifies it, at most 8 per 30 s in use and 2 otherwise; 1.5 s host spacing |
| [EASA Conflict Zone Information Bulletins](https://www.easa.europa.eu/en/domains/air-operations/czibs): the list's CSV export and the RSS feed | Bulletins: subject, number, status, validity, affected airspace and countries, recommendation, links | EASA: "reproduction is authorised, provided the source is acknowledged" | CSV and feed every 2 h in use, 12 h otherwise (kept in SQLite across restarts) |
| [VATSpy data project](https://github.com/vatsimnetwork/vatspy-data-project) `Boundaries.geojson` | FIR outlines for the advisories | CC BY-SA 4.0; made for flight simulation, so treat edges as approximate | Dataset `flights.firs`, weekly (2.3 MB) |
| [Natural Earth](https://www.naturalearthdata.com/) admin-0, 1:50m | Country outlines for bulletins limited to a country's territory | Public domain | Dataset `flights.countries`, monthly (3 MB) |

Not used: airplanes.live (403 from the build network) and planespotters.net server calls (their
API requires a contact address in the User-Agent, which Meridian never sends). Card photos from
planespotters.net are an opt-in layer option ("Photos from planespotters.net in cards", off by
default): the browser asks planespotters.net directly and the card shows the photographer credit
and a link to the photo page, as their terms require.

## Layers

| Layer | Group | What | Refresh |
|---|---|---|---|
| `flights` (on by default) | Flights | Aircraft for the area on screen, coloured by class, icon size by ADS-B category, callsign labels from zoom 6.5 (altitude from zoom 9), emergencies red with a halo, OFAC-listed ringed in red, estimated positions hollow and labelled "estimated" | 10 s |
| `flights.military` | Flights | Military aircraft worldwide (database military flag) | 60 s |
| `flights.alerts` | Flights | Squawk 7500, 7600, 7700 and ADS-B emergency status worldwide; a toast for new ones | 30 s (server polls every 60 s) |
| `flights.gpsjam` | Conflict | GPS interference per 0.5 degree cell (1 degree below zoom 4.5), last 6, 12, 24 or 48 h | 5 min |
| `flights.delays` | Flights | US airports with an FAA status: red (ringed) ground stop or closed, amber delays or a ground delay programme, blue notices and de-icing; dot size by average delay; FAA code and delay labels from zoom 3.5. The row count is airports with a delay or a full closure; the row text also says how many more have notices only (closed to some traffic, de-icing) | 2 min |
| `flights.tfr` | Flights | FAA temporary flight restrictions coloured by type (VIP red, security amber, hazards orange, space operations violet, air shows and sports blue, drones-only light blue, off by default); solid when in effect, dashed when starting later; a hollow ring marks each one below zoom 7.5 | 5 min |
| `flights.czib` | Flights | EASA bulletins shaded on the FIRs they name (or the country): red "do not operate", amber "caution"; dashed outline when the bulletin covers only part of the shaded area; a label per bulletin | 30 min (server every 2 h) |
| `flights.activity` | Flights | Air activity candidates: the 13 watched regions as thin outlines (red fill: unusual military activity, amber: busier than usual), new or worse GPS interference cells in red, and sampled airports (red: a movement drop, amber: one low hour or a drop that cannot be verified); region labels give the count an hour, and the multiple of usual when the usual is one aircraft-hour or more | 5 min |

The three aircraft layers share one map source, so an aircraft is drawn once. Options on `flights`
(applied in the browser, no refetch): class filter (including emergencies and OFAC-listed),
military only, hide aircraft on the ground, lowest altitude, estimated positions on or off, labels,
opt-in photos.

Classes: airliner, cargo, business jet, military, helicopter, light aircraft, glider/balloon/drone,
ground vehicle, unknown. Order of evidence: the database military flag, ADS-B emitter category
(A1-A7, B1-B7, C1-C5), ICAO type designator and description (business-jet, helicopter, turboprop,
widebody and military type lists), then callsign patterns (cargo airlines, military telephony
prefixes, airline flight numbers versus registrations). Twelve silhouettes are drawn on canvas and
converted to signed distance fields so MapLibre can tint them and draw an ink halo or a hollow outline.

## How live data is fetched

- The view is covered by point-radius queries (max 250 nm) on a fixed global grid with three
  levels (5.5, 2 and 0.75 degrees of latitude). The smallest level that covers the view with four
  tiles or fewer is used; level 0 up to six tiles (`FLIGHTS_MAX_TILES`, max 8). Tiles stay fresh
  15, 12 or 10 s by level, so panning reuses them.
- Wider views ("wide" mode) show military aircraft, emergencies, OFAC-listed aircraft and traffic
  already in memory, plus live traffic from up to six level-0 tiles nearest the centre from zoom
  4.5. The layer goes amber with "Zoom in to see all traffic" and a hint appears on the map.
- Each host has one priority queue and worker: card lookups first, then the view, emergencies,
  military, sanctioned aircraft, GPS hotspots, discovery. A 429 honours `Retry-After`; errors back
  off 15 s to 5 min; queued jobs for a cooling host do not fire. Tiles fall back to the next
  provider in `FLIGHTS_PROVIDERS` (default `adsbfi,adsblol,opensky`).
- Observations from every source merge by ICAO hex; the freshest position wins.

## Estimated positions

When a tile or worldwide set is refreshed and an aircraft that was inside it is missing, it is
marked as a coverage dropout. Dropouts that are airborne, above 3,000 ft, faster than 80 kn and not
descending low (probably landing) are dead-reckoned along their last track and ground speed for
up to 20 minutes. They are drawn hollow, labelled "estimated", carry an "Estimated position" badge
in the card, and are never counted as live. Aircraft that simply left the area you are viewing are
not estimated: they are hidden after 60 s (110 s for once-a-minute worldwide sets).

## Trails and cards

Positions are kept in memory for 45 minutes per aircraft (thinned to one point per 15 s in
straight flight, more in turns; at most 400 points per aircraft and 1.5 million in total). "Show
trail" draws it with an altitude colour ramp; when fewer than five minutes are known, one metered
adsb.lol trace request fills in the earlier part.

Cards (entity kind `aircraft`, id = lower-case hex) work from a bare `{kind, id}`: the server looks
the aircraft up on adsb.fi if it is not in memory, and otherwise shows when and where it was last
seen (SQLite `flights_seen`, 48 h). Operators: airline and company operators are shown; owners
that look like private people are shown as "Private owner"; LADD and PIA privacy-programme
aircraft never show an owner. OFAC cards never name private individuals linked to a listing.

## Airspace and airport status

**US airport delays.** NAS Status lists only airports that have a current status. Each status
becomes a record with a sentence-case `type` (Ground stop, Ground delay programme, Arrival delays,
Departure delays, Airport closed, Closed to some traffic, Airport notice, De-icing), a reason
("WX:Low Ceilings" becomes "Weather: low ceilings"), average, minimum and maximum delay in minutes,
trend, start and end (UTC), the FAA advisory link and a plain-language summary. FAA "free form"
closures such as "AD AP CLSD TO NON SKED TRANSIENT GA ACFT EXC 24HR PPR" are partial closures
(blue, not delays); the summary spells them out and leaves phone numbers in the notice text.
Airport ids: contiguous-US FAA ids map to "K" + id; Alaska, Hawaii and the territories use a
small table (HNL to PHNL, SJU to TJSJ and so on).

**Temporary flight restrictions.** The list gives type and place, the WFS layer gives polygons
(several per notice: a VIP restriction has an outer ring and an inner core), and the XNOTAM notice
gives times, altitudes per area (MSL, above ground or flight level), daily schedules, the rule
(14 CFR 91.141 VIP movement, 91.137 hazards, 91.143 space, 91.145 sports and air shows, 99.7
security, 49 USC 44812 drones) and the notice text. Notice times are UTC even when the notice names
a local zone; "PERM" notices are open-ended. Notices that list places elsewhere (the nationwide
drone notices) have no polygon and appear in lists, search and the AI tool only.

**Conflict-zone advisories.** Only active bulletins are drawn. A bulletin is shaded on the FIRs it
gives in brackets ("FIR LVIV (UKLV)"), using VATSpy outlines; upper regions such as UIR KYIV (UKBU)
lie over FIRs and are not drawn separately. "West of longitude 60° East" (Russia) clips the FIRs at
that meridian. Bulletins limited to a country's territory (Mali, inside the much larger Niamey and
Dakar FIRs; Sudan) use the Natural Earth country outline instead, and so does any bulletin whose
FIRs we cannot draw. The recommendation sets the colour: "not operate" or "not to fly" is red,
"exercise caution" or "take risks into account" is amber. A dashed outline means the bulletin only
covers part of the shaded area (portions of a FIR, waters only, provinces); the card quotes the
recommendation, so read it for the exact limits. Altitudes come from the text: "All", "Below
FL260" or "Varies".

**Events** (`m.events`, modest severities): ground stops and full closures 5; ground delay
programmes and arrival or departure delays 4 when the average reaches 60 min (or the maximum 120),
3 when the average reaches 30 min (or the maximum 45). One event per programme (keyed by its start
time, or continued while it is seen within 45 minutes), stored for 14 days so the feed covers the
day even after a stop is lifted; ids are `<ICAO>:<kind>:<first seen>`. New flight restrictions:
VIP anywhere 4, space operations 3, other types over one of 63 large US cities 3 (the city centre
inside the area or within 15 km of its centre), drone-only restrictions never. New EASA bulletins
4, revisions that change the content 3 (validity extensions are not events).

**Background polling (CONTRACT rule 8).** The three sources keep a slow cadence when nobody is
looking so the events feed stays current: NAS Status every 15 min (96 small requests a day), the
TFR list and polygons every 30 min (96 a day) plus each new notice once (a handful a day), EASA
twice a day. While a layer or its cards are open they speed up to 2 min, 5 min and 2 h; the infra
airport card and the AI tools refresh on demand when the copy is older than 3 min (delays) or
5 min (restrictions). Failures back off from 1 to 30 minutes and the last good copy stays in service.

## GPS interference method

Every airborne ADS-B sample (version 1 or later, or reporting NACp; not TIS-B, ADS-C or ground
stations; MLAT positions only for ADS-B-equipped aircraft) is counted in its 0.5 degree cell as an
aircraft-hour (distinct aircraft per cell per hour). It is affected when NACp is below 8 (or NIC
below 7 without NACp) or the network reports the GPS stopped being usable (`gpsOkBefore`). Cells
need 12 aircraft-hours; colours: green under 2%, amber 2 to 10% (at least 2 affected), red 10% or
more (at least 3 affected). Hour buckets are stored in `flights_gpsjam` and pruned after 14 days (the
layer and its card still show at most 48 h; the older days feed each cell's 7-day trend, below). Window
queries read the primary key's hour range (`GROUP BY +row, +col`: grouping by the bare columns made
SQLite scan the whole table through the `(row, col)` index), so keeping 14 days does not slow the layer,
its events or the hotspot sampler (about 40 ms for 24 hours on the development copy's 250,000 rows).

This is derived from what aircraft report, not a measurement of radio signals. It cannot see
spoofing that keeps the reported accuracy high, and some older avionics always report low accuracy.

### Background polling (CONTRACT rule 8)

Two pollers run when the browser is closed, because their history is the point:

- GPS hotspot sampler: 16 known interference areas (Baltic and Kaliningrad, Gulf of Finland,
  Finnmark, the Belarus border, both halves of the Black Sea, the eastern Mediterranean, northern
  Syria, Iraq, the Persian Gulf, Hormuz, the Red Sea, the Caucasus, Libya, Myanmar, the Korean
  DMZ), one 140-220 nm circle at a time, each every 5 min while Meridian is in use and every 10 min
  otherwise (`FLIGHTS_GPSJAM_INTERVAL`; `FLIGHTS_GPSJAM_SAMPLER=0` turns it off). About 2,300
  requests a day when idle.
- Emergency squawk search (7500, 7600, 7700): every 60 s in use, every 3 min otherwise (adsb.fi
  only), about 1,440 small requests a day when idle, so the events feed covers the whole day.

Everything else (tiles, military, sanctioned aircraft, watched cards, discovery) runs only while
the browser uses the module, except the air activity samplers described below (the worldwide military
list every 10 minutes, and favourited airports).

## Replay history (timeline)

`history.py` keeps 72 hours of sampled positions for the timeline module (CONTRACT 15.4). It makes
no requests: one hook in `FlightsService._absorb` passes it every batch the service already
merges (map tiles for the area on screen, the worldwide military and emergency sets, watched cards,
sanctioned aircraft, discovery look-ups, a local receiver).

- Per aircraft: never more often than every 60 s and at least every 5 minutes while it is received;
  in between, a new sample once the track has turned 8 degrees, the altitude changed 1,000 ft or the
  flags changed (ground, emergency, military, OFAC). The last observation before an aircraft stops
  arriving is written 90 s later, so replay ends where the live map lost it. Straight-line
  interpolation between samples stays within about a kilometre.
- Not kept: estimated positions, LADD and PIA privacy-programme aircraft (rule 2: shown live as the
  feeds show them, but no movement history), and from the GPS sampler's 16 hotspots everything except
  military, emergency and OFAC-listed aircraft. Hotspot traffic would add an estimated 15,000 to
  30,000 rows an hour around the clock (16 circles of about 150 aircraft, 6 to 12 passes an hour: 1 to
  2 million rows and 60 to 130 MB per 72 h) of 5-to-10-minute snapshots that aircraft cross between
  samples, for areas you may never look at; the sampler exists for the interference statistics, and
  the notable aircraft are kept.
- Volume follows what you watch: about 4 to 7 samples per aircraft-hour, roughly 60 bytes per sample
  with its index (measured). A busy European view of 3,000 aircraft is about 15,000 rows an hour while
  it is open; nothing is added for areas nobody looks at.
- Bounds: 72 h retention, `FLIGHTS_HISTORY_MAX_ROWS` (default 2,000,000, about 120 MB; the oldest go
  first), at most 200,000 samples buffered between writes (every 20 s) and 60,000 aircraft with state.

## Air activity (military surges, GPS interference trends, airport drops)

Three detectors compare what the module receives with what is usual for the same place and time, and
publish **candidates for a closer look, never findings**. Each one states its evidence (the number, the
usual number, the window), its coverage (snapshots, receiver checks) and its limits, and lists the
common innocent explanations. Military and airport baselines say "Learning baseline: n of 7 days" until
they have a week of history; a GPS cell says "Learning: n of 5 days" until 5 of its previous 7 days had
enough aircraft at the same hours. Nothing is flagged in the meantime. Statistics come from the shared `meridian.stats`
helpers (robust z on log counts, Poisson tails, seasonal windows with an uptime mask); the rules and
all wording live in `activity_rules.py`, so they are tested on synthetic series.

**Snapshots, not polls.** Military aircraft and airport movements are counted from snapshots: the first
adsb.fi answer in each 10-minute slot (at most 6 an hour), however often the map is polling. An hour
with the map open (military list every 60 s, tiles every 10 s) is therefore measured exactly like an
idle hour, and an hour's snapshot count is also its uptime: windows with under 80% of their snapshots
are left out of every baseline, so a quiet period counts only if Meridian was running. Only adsb.fi
answers become snapshots; the adsb.lol and OpenSky fallbacks have other feeders and would move the
baseline.

### Events (kind `air_activity`, CONTRACT 5.6)

| `category` | id | Position | Severity |
|---|---|---|---|
| `military_surge` | `flights:surge:<region>:<start>` | the region's label point, with `bbox` | 4 to 6 |
| `gps_interference` | `flights:gps:<lat0>:<lon0>:<start>` (the first cell's south-west corner) | the cluster's centre, with `bbox` | 5; 6 when 30% or more of aircraft are affected and the baseline is high confidence; 4 when it is low confidence |
| `airport_drop` | `flights:airport:<ident>:<start>` | the airport | 4, 5 when near zero for 2 hours, 3 when overflights are down too |

One event per episode: an episode continues while it is flagged again within 3 hours (2 hours for
airports), so the event history holds one row per episode with its span. Extra fields: `ref` (region
id, airport ident or cell), `confidence` (high, medium or low), and `ratio` (null when the usual is under
one aircraft-hour in the window), `share`, or `change` and `cells`. Episodes are kept 30 days in `flights_activity`. Derived anomalies stay at severity 6 or below.

`gps_jam` events (one per amber or red cell) are unchanged in ids and severities, so the alerts GPS
preset, the briefing's GPS row and the timeline keep working. They now also carry the cell's `trend`
(new, worse, chronic, recurring, easing, learning or quiet), `days_affected_7d` (days of the previous 7
on which the cell was affected at any hour) and a `category`:
`new_interference` for new or worse cells, `chronic_interference` for cells that repeat. Convergences
can skip `chronic_interference` cells and should exclude kind `air_activity`, whose region points would
make coincidences of position (a request to the intel module).

### Military surges by region

- **Regions** (`activity_data.REGIONS`, simplified hand-drawn outlines, not borders): Baltic Sea, Poland
  and the Belarus and Ukraine borders, Black Sea, Eastern Mediterranean, Iraq and Syria, Persian Gulf and
  Strait of Hormuz, Red Sea and Gulf of Aden, North Sea, Norwegian and Barents seas, Taiwan Strait, Korean
  peninsula, South China Sea, Caribbean. An aircraft counts in every region whose outline contains it.
- **Sampler** (CONTRACT rule 8: the baseline needs continuous history): one request for adsb.fi's
  worldwide military list every 10 minutes when nothing else has fetched it in that slot, adsb.fi only:
  144 small requests a day (`FLIGHTS_MIL_SAMPLER=0` turns it off; the map's own polling still adds
  snapshots while the military layer is on). A 10-minute cadence catches any aircraft that stays in a
  region for 10 minutes, which covers patrols, tankers, surveillance orbits and transports; brief
  transits are partly missed, in every hour alike.
- **Counted**: airborne aircraft flagged military in the community database, not in the LADD or PIA
  privacy programmes, with a fresh position. Roles from the ICAO type: tankers, surveillance and patrol
  (including drones), transports, fighters, bombers, other. **Trainers and helicopters are left out of
  the count** (shown separately on the card): they fly to base timetables and training areas, and rarely
  say anything about wider activity.
- **Measure**: aircraft-hours over the last 3 complete hours (the sum of each hour's distinct aircraft),
  shown as aircraft an hour, with the distinct aircraft in the window beside it.
- **Baseline**: the same 3 hours on each of the previous 7 to 28 days (hour of day; at least 7 days, so
  "learning" until then) or, from two weeks, the same 3 hours on the same weekday on its own (hour of
  week, 2 to 4 weeks): like with like, so a weekly schedule such as a training day is usual on its weekday,
  and a Saturday is compared with earlier Saturdays rather than with busier weekdays (the higher of the two
  levels used to win, which hid weekend surges). In the second week (days 7 to 13), when only one
  same-weekday window exists, a value under twice that window is held back to "busier than usual" with the
  reason "like the same hours a week ago", so last week's training wave is not published as a surge.
- **Unusual** (an event): at least 8 aircraft-hours, 4 different aircraft, twice the usual and a robust z
  of 3 or more. For small baselines (median under 5) a quasi-Poisson test instead: both sides are divided
  by the window's aircraft-hours per aircraft (an aircraft present for 2 hours counts twice), then 1 in
  1,000 or rarer, with at least 10 aircraft-hours, 5 aircraft and 3 times the usual. **Busier than usual**
  (panel and card only): 1.5 times and z 2.
- **Coverage controls**: the worldwide list's own size against its usual (outside 60 to 160%: "Feed
  changed", nothing flagged); and, for regions with GPS-sampler circles, all traffic in those circles
  (more than 140% of usual: receiver coverage may have grown, nothing flagged). A traffic *drop* never
  suppresses a surge, because airspace closures empty the sky of airliners at the same time as military
  activity rises; the card says "civil traffic is 40% of usual (airspace closures or receivers offline)".
- **Confidence**: high needs 14 days of baseline (or the weekday baseline, from 3 weeks) and z 4 or more;
  at most medium without a receiver check (Taiwan Strait, South China Sea, North Sea and Caribbean have no
  sampler circle), with mostly seeded history, or on the small-numbers test; low with fewer than 6
  aircraft. The card lists the reasons.
- **Seed**: at the first start, the replay history (`flights_history`, 72 h) is tallied the same way for
  the 10-minute slots in which the worldwide list was evidently being polled (40 or more military aircraft
  outside the GPS circles in the slot) and before the sampler's first snapshot. Seeded hours are marked,
  and cap confidence at medium while they are most of the baseline.
- **Wording**: "16 military aircraft an hour on average over the last 3 hours (15:00 to 18:00 UTC), 23
  different aircraft. Usual for this time of day (median of 13 days) is 4 an hour, so 4.1 times the
  usual. Aircraft-hours by role: transports 25 (usually 7), tankers 15 (usually 5) ..." The role usuals
  use the same baseline. Titles and map labels lead with the count ("More military aircraft than usual
  over the Baltic Sea: 16 an hour, usually 4.5"; "Baltic Sea 16/h (3.6×)"). When the usual is under one
  aircraft-hour in the 3 hours no multiple is given (the shared helper divides by at least 0.5, which made
  "24 times the usual" out of 12 aircraft-hours): "4 an hour where usually there are none". Why this may
  be innocent: exercises, deployments in transit, air shows, receiver coverage, and most military aircraft
  never broadcasting.

### GPS interference trends

Every 10 minutes each 0.5 degree cell's last 6 complete hours are compared with the same 6 clock hours on
each of its previous 7 days, as the military and airport detectors compare like with like (24-hour blocks
used to be the baseline, which made any cell affected at the same hours every day "worse" during those
hours and "easing" outside them). An earlier window, or a whole day, counts with 12 or more aircraft-hours
and is affected at the layer's amber level (2% or more, at least 2 affected). Affected now means 10% or
more of 12 or more aircraft-hours, at least 4 affected, with 2 or more affected aircraft in the same hour in
at least 2 of the 6 hours: hours are counted per aircraft, so one aircraft with degraded avionics circling
a quiet cell can make 4 affected aircraft-hours on its own, but never 2 in one hour.

- **new**: affected now after a quiet week: under 2% in the same hours over at least 5 days with data, and
  no window and no whole day affected;
- **worse**: affected now, at least twice the cell's highest share in the same hours on any earlier day and
  5 points above it, and more than even that day's share would give by chance (Poisson, 1 in 1,000).
  Judging against the worst day, not the week's pooled aircraft-hours, lets the spread between days count
  (interference is clustered, so single aircraft-hours are not independent). A chronic cell is only worse
  at twice its worst day;
- **chronic**: the same hours affected on 4 or more of the 7 days, or the cell affected at any hour on 4 or
  more of them (so a cell that repeats daily at other hours stays chronic, and its `gps_jam` events stay
  `chronic_interference`); **recurring**: on 1 to 3; **easing**: chronic in these hours but under 2% now
  (judged only with 12 or more recent samples); **learning**: fewer than 5 days with data in these hours.
  Five: a cell affected on 4 days a week shows 5 quiet days in a row by chance about 1.5% of the time (3
  days, the earlier rule, about 8%), and two missing days (restarts, quiet nights) are allowed.
- **Confidence** (new and worse cells and their clusters): high with all 7 days and a cell inside a
  GPS-sampler circle; medium with 5 or 6 days, or 7 days from viewing only; low with 5 or 6 days from
  viewing only (cells outside the circles have data only while the map shows them). Severity follows it
  (6 needs high confidence, low gives 4), and the subtitle gives the real baseline: "18% of aircraft
  affected in the last 6 h (1 cell); 0% in the same hours on 5 earlier days with data (75 aircraft-hours);
  confidence low".

Adjacent new and worse cells (8-neighbour) form one cluster and one `gps_interference` event, named after
the GPS-sampler area or region it lies in. Most cells repeat every day, so these events are a small
fraction of the cells. Check on the development copy (its real last 24 hours copied back as 13 identical
days, so nothing can truly be new or worse): with the same-hours baseline no cell was new or worse at any
of 18 clock times through the day and all 386 amber or red cells were chronic; the 24-hour blocks had given
1 or 2 false "worse" cells (Strait of Hormuz and Oman) from 17:00 UTC and 17 to 57 false "easing" cells
every hour. The GPS cell card shows the cell's 7-day trend: one column per UTC day coloured like the
layer, the 10% line, a badge (New this week, Worse than its week, Repeats daily) and the trend sentence
(which names the hours and the baseline's days and aircraft-hours). Why this may be innocent:
avionics that always report low accuracy, announced GPS testing and exercises, geomagnetic storms at high
latitudes, receivers misreporting accuracy fields.

### Airport movement drops

- **Airports**: 54 major airports (`activity_data.AIRPORTS`, ICAO ident, counting radius 15 to 40 km,
  smaller where another big airport is close) plus up to 6 favourited airports (kind `airport`, the infra
  card's star). Background coverage: airports whose whole circle lies inside a GPS-sampler circle get a
  snapshot from every sampler pass (Helsinki, Tallinn, St Petersburg, Vilnius, Warsaw, Minsk, Gdansk,
  Varna, Burgas, Tbilisi, Yerevan, Tel Aviv, Beirut, Larnaca, Paphos, Amman, Damascus, Baghdad, Dammam,
  Bahrain, Doha, Dubai, Muscat, Seoul Incheon and Gimpo); favourites get one adsb.fi point query every 10
  minutes when no sampler circle covers them (`FLIGHTS_AIRPORT_WATCH=0` turns that off; at most 864
  requests a day); any other airport is counted while you view it (map tiles and the infra airport card).
- **Counting** (the airport traffic logic of `/api/flights/airport/{ident}`, without routes, so it is the
  same every hour): within the circle, descending below 12,000 ft counts as arriving and climbing as
  departing; on the ground within 6 km; overflights at 25,000 ft or more are the receiver check.
  Helicopters, gliders, ground vehicles and privacy-programme aircraft are left out. Distinct aircraft
  per hour; a busy hour reads about three quarters of its real movements (departures climb through
  12,000 ft in a few minutes), but it does so every hour.
- **Baseline**: the same hour on the previous 7 to 14 days or, from two weeks, the same weekday on its own
  (like with like; the lower of the two used to win, which hid drops on an airport's busy days), and the
  evidence names the one used ("usual for this hour on Saturdays (median of 4 weeks) is 40"). Hours that
  usually have fewer than 10 movements (nights, curfews) are never judged.
- **Drop** (an event): 40% of usual or less, Poisson P(X <= observed) under 1% and robust z of -2.5 or
  lower, in both of the last two complete hours, with overflights at least half their usual. One such
  hour is "Fewer movements than usual" (panel only). When overflights are down too, it is published at
  severity 3 with low confidence as "Far fewer aircraft than usual around ...": the airspace may be closed
  or the receivers covering it offline, and ADS-B cannot tell which.
- **FAA**: US airports link to FAA NAS Status and show its current status (ground stop, closure, delays)
  when it has one. Why this may be innocent: schedule changes and holidays, runway works or night
  restrictions, weather, and a nearby receiver going offline (low aircraft are only seen by nearby
  receivers).

### Panel, layer and cards

The toolbar button **Air** opens the "Air activity" drawer with three tabs: Military (every region: now,
usual, multiple and state, or "Learning: n of 7 days"), GPS (how many cells repeat, come and go, ease,
are new or worse, or are learning, then the new or worse clusters) and Airports (sampled airports
first); each tab ends with the last 7 days of episodes. Cards (entity kind `air_activity`, also from a
bare `{kind, id}` in favourites and case files): the region card (block: now, usual, multiple, aircraft;
evidence and role drivers; a 48-hour chart of aircraft per hour against the detector's own usual (the
same 3-hour windows, lined up with the compared one, and the same profile; no line while learning), hours
with under 5 of their 6 snapshots drawn as gaps, the compared window shaded and a "Show the numbers" table; roles; coverage and checks with the confidence reasons;
military aircraft in the region now; innocent explanations; earlier episodes; method), the airport card
(block: last hour, usual, share of usual, snapshots; the 48-hour chart against the detector's usual for
each hour; this hour so far; FAA status; coverage) and the GPS cluster card (affected now against the same
hours on the earlier days with data, confidence and its reasons, daily chart, cells).
Charts use one colour for the columns, a neutral line for "usual" and a shaded band for the compared
window (never colour alone), with a table view beside them.

### Background polling budget (CONTRACT rule 8)

The air activity detectors add the worldwide military list every 10 minutes (144 small adsb.fi requests
a day) and favourited airports (none unless you favourite airports; at most 864 a day). Everything else
they use comes from requests the module already makes: the GPS sampler's circles, map tiles, the infra
airport card and the military layer. Background samples of the military list also reach the replay
history (about 400 military aircraft every 10 minutes, roughly 170,000 samples per 72 hours, within its
row cap). With the GPS sampler (about 2,300 a day idle) and the squawk search (about 1,440), the module
stays under 5,000 adsb.fi requests a day when idle, about one every 17 seconds.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/flights/history?from&to&bbox&limit&format` | CONTRACT 15.4: sampled positions `{hex, t, lat, lon, alt, trk, cls, flags}` with `from <= t <= to`, oldest first (samples not written yet included), plus `idents {hex: {cs, reg, typ, desc}}` (the callsign spell overlapping the window), `count`, `aircraft`, `truncated`, `available {from, to}`, `sampling`. `from`/`to` epoch seconds (ms accepted; default the last hour, at most 6 h a request), `bbox` w,s,e,n (west > east crosses 180°), `limit` 1 to 50,000 (default 20,000), `format=rows` gives `fields` + `rows` arrays. `alt` is barometric ft (null when unknown), `trk` degrees, `flags` as in `/live` (2 military, 4 emergency, 8 OFAC-listed, 16 on the ground, 32 MLAT, 128 local receiver) |
| `GET /api/flights/history/{hex}?from&to` | One aircraft's samples in the window (default and at most 72 h; up to 5,000), with its callsign spells `idents [{cs, reg, typ, desc, from, to}]` |
| `GET /api/flights/history/coverage?from&to` | Samples per UTC hour `{by_hour: [{t, samples}], available, rows}` (the time bar's coverage strip) |
| `GET /api/flights/live?bbox=w,s,e,n&zoom=z` | `{mode, level, tiles, fields, ac: [[hex, lat, lon, trk, alt, gs, vr, cs, reg, typ, cls, shp, sz, flags, age, sq]], counts, providers, note, error}`; flags: 1 estimated, 2 military, 4 emergency, 8 OFAC-listed, 16 on the ground, 32 MLAT, 64 privacy programme, 128 local receiver |
| `GET /api/flights/military` | Same row format, worldwide military |
| `GET /api/flights/alerts` | Same rows for current emergencies, `details`, `recent` (12 h) |
| `GET /api/flights/aircraft/{hex}` | Card data: live state, class, category, squawk meaning, source, position age, navigation accuracy, operator (privacy rule), route (adsbdb, unverified, with a plausibility check), OFAC listing, estimate, last seen, links |
| `GET /api/flights/trail/{hex}` | `{points: [[lon, lat, alt_ft, t]], estimated: [lon, lat] or null, note}` |
| `GET /api/flights/airport/{ident}?lat&lon&radius_km=40` | CONTRACT 9.1: `{arrivals, departures, overhead, on_ground, covered, note}`; items are aircraft entities plus `alt_ft`, `route`, `distance_km`, `vr_fpm`, `gs_kt`, `phase` |
| `GET /api/flights/gpsjam?bbox&zoom&hours` | `{fields, cells: [[lat0, lon0, size, total, bad, level 1-3, last_bad_t]], counts, method}` |
| `GET /api/flights/gpsjam/cell?lat&lon&hours&agg` | One cell with hourly counts, plus `trend {label, text, category, window {from, to}, days: [[day_start, total, bad]] (7 UTC days, for the chart), windows: [[total, bad]] (the same 6 clock hours on each of the 7 earlier days, most recent first), share_now, share_base, days_with_data, days_affected, max_daily_share (the highest share in those hours on any earlier day), days_any_with_data, days_affected_any (whole days), shared_hours, one_aircraft_at_a_time, confidence, confidence_reasons}` |
| `GET /api/flights/sanctioned/{uid}` | An OFAC aircraft record (no individuals), live match or last sighting |
| `GET /api/flights/status` | Providers, budgets, queues, tiles, counts (diagnostics) |
| `GET /api/flights/airport-status` | GeoJSON points, one per US airport with an FAA status: `{id, faa, name, code, type, level 1-3, delay 0/1, avg, max, n, label}`; `meta {airports, delayed, counts, updated, stale, error}` |
| `GET /api/flights/airport-status/{ident}` | Cross-module (infra airport card): `{delay, type, reason, avg_delay_min, trend, source, updated}` for the airport's most serious status, plus `code`, `level`, `summary`, `max_delay_min`, `start`, `end`, `items` (every status in the same shape), `airport`, `runways`, `link`, `entity`. `ident` is ICAO (KSFO, PHNL), FAA or IATA (SFO). `delay` is false for notices, partial closures (closed to some traffic) and de-icing. 404 when the FAA lists nothing for it (non-US airports included), 503 when NAS Status has not answered since start-up. `updated` is epoch seconds |
| `GET /api/flights/tfrs` | GeoJSON: polygons (`role: area`) and centre points (`role: centre`) with `{id, nid, cat, type, place, status, active, lower, upper}`; `meta {count, mapped, active, upcoming, by_category, details_pending, updated, error}` |
| `GET /api/flights/tfrs/{nid}` | One restriction (`nid` = NOTAM number with an underscore, `6_9435`): type, rule, purpose, place, status (active, upcoming, listed), start, end, permanent, areas with altitudes and schedules, notice text, official link, entity. Fetches the notice on demand when it is not loaded yet |
| `GET /api/flights/advisories` | GeoJSON: one area per FIR or country (`role: area`, `part`, `pkind`) and one label point per bulletin (`role: label`) with `{id, number, subject, short, level, partial, alt}`; `meta {count, mapped, by_level, by_shape, withdrawn, updated, error}` |
| `GET /api/flights/advisories/{id}` | One bulletin (`czib-2022-01`): number, revision, status, issue and revision dates, validity, affected airspace and countries, recommendation, background, references, level, altitudes, partial, shading parts, EASA link, entity |
| `GET /api/flights/airspace/at?lat&lon` | Restrictions and bulletins containing the point and airport statuses within 30 km ("What's here?") |
| `GET /api/flights/airspace/status` | Feed health, cadences, counts (diagnostics) |
| `GET /api/flights/activity` | The Air activity panel: `regions` (state, status, severity, confidence, ratio, per_hour, usual_per_hour, distinct, baseline_days, needed, coverage, profile, this_hour, entity), `gps {clusters (with confidence, confidence_reasons, days_with_data, base_total), counts}`, `airports` (state, status, value, usual, share, coverage, receivers, background, watched, this_hour, entity), `episodes` (last 7 days, as events), `sampler`, `method`, `caveats`. States: unusual, notable, normal, learning, no_data, feed_changed, coverage_changed; airports drop, drop_unverified, notable, normal, learning, no_coverage |
| `GET /api/flights/activity/map` | GeoJSON for the `flights.activity` layer: region outlines and label points, new or worse GPS cells, sampled airports (`kind`, `level` 0 to 3, `state`, `status`) |
| `GET /api/flights/activity/region/{id}` | One region: the brief above plus `evidence`, `drivers`, `roles`, `checks`, `confidence_reasons`, `series` (49 hours of `[t, aircraft, coverage, usual]`; `usual` is the detector's usual an hour for the 3-hour block, null while learning), `aircraft` (military aircraft there now), `geometry`, `episodes` (30 days), `innocent`, `limits`, `method`. 404 for an unknown id |
| `GET /api/flights/activity/episode?id=` | One episode (`flights:surge:...`, `flights:gps:...`, `flights:airport:...`): the event plus `data`, `active`, and for GPS clusters `cells` and `days`. 404 after 30 days |
| `GET /api/flights/airport/{ident}/movements` | Proposed CONTRACT 9.1 row (for the infra airport card): `{ident, name, monitored, state, status, value (last hour), usual, share, coverage, receivers, this_hour, faa, series (49 hours of [t, arrivals, departures, on_ground, overflights, coverage, usual], usual being the detector's own for the hour, null while learning), hours, evidence, coverage_note, innocent, method, episodes}`; `monitored: false` with a note for airports Meridian does not count |

Search (live aircraft by callsign, registration, hex and type; airports with a status by code or
name; restrictions by NOTAM number, place or type; bulletins by country, FIR code, number or
subject), nearby (live aircraft, no ground vehicles; airports with a status; restrictions), events
(emergencies severity 4-9, OFAC-listed aircraft seen severity 5, GPS cells 4-6 with their trend, the
airspace events above, and `air_activity` episodes 3-6) and AI tools `flights_in_area`, `aircraft_details`,
`flights_emergencies`, `gps_interference`, `airport_delays` (current US delays, all or one airport, by kind
or minimum delay), `airspace_restrictions` (restrictions and bulletins by area, country or kind),
`air_activity` (surges by region, new or worse GPS interference and airport drops, by region id or name,
area or category, with evidence, confidence and innocent explanations; a name matching several regions,
such as "gulf", limits the GPS, airport and episode sections to all of them, and `scope` says what the
sections cover) and `airport_movements` (one
airport's hourly arrivals and departures for 24 hours against the usual, coverage and FAA status).
Browser entity handlers: `aircraft`, `sanctioned_aircraft` (sanctions search results), `gps_jam`,
`airport_delay` (id = ICAO, or `<ICAO>:<kind>:<time>` from events), `flight_restriction` (id = NOTAM
number, `6/9435`), `airspace_advisory` (id = `czib-2022-01`, the number without its revision, so
favourites follow revisions) and `air_activity` (`flights:surge:<region>` opens the region card,
`flights:airport:<ident>` the airport card and an episode id its card). "What's here?" sections report GPS interference for the clicked cell
and the airspace at the point.

## Storage

SQLite tables: `flights_history` (72 h of replay samples, clustered on time, with a hex index) and
`flights_history_ident` (callsign spells, 72 h), `flights_gpsjam` (14 days), `flights_events` (14 days), `flights_seen` (48 h),
`flights_lookup` (adsbdb cache, 7 days), `flights_sanctioned_hex` (transponder codes learned for
listed registrations), `flights_airport_events` (14 days), `flights_tfr_detail` (parsed notices,
dropped 7 days after leaving the FAA list). Air activity: `flights_mil_hour` (counted military aircraft
per region and hour with role counts, 90 days; the hex list of each hour, used for distinct counts, is
cleared after 48 h), `flights_mil_cov` (snapshot bit mask, worldwide count and a seeded flag per hour, 90
days), `flights_traffic_hour` (all-traffic counts per GPS-sampler circle and hour, 35 days),
`flights_apt_hour` (arrivals, departures, on the ground, overflights and snapshot mask per airport and
hour, 35 days; hex sets only while the hour is open, for restarts) and `flights_activity` (episodes, 30
days). At most about 28,000 small rows for the military (13 regions, 90 days) and 50,000 for airports. kv: `flights.budget.*`,
`flights.sanctions.cursor`, `flights.czib.items`, `flights.czib.links`, `flights.czib.fetched_at`,
`flights.czib.format`, `flights.activity.seed` (when the history seed ran, and what it found).
Datasets: `flights.firs` (`data/build/flights/firs.json`, 416 FIRs) and `flights.countries`
(`countries.json`, 237 countries). Stored copies carry a parser version; a newer parser refetches.

## Settings

`OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET`, `ADSBLOL_API_KEY` (secrets), `LOCAL_ADSB_URL`,
`FLIGHTS_PROVIDERS`, `FLIGHTS_GPSJAM_SAMPLER`, `FLIGHTS_HISTORY` (0 keeps no replay history),
`FLIGHTS_HISTORY_MAX_ROWS`, `FLIGHTS_MIL_SAMPLER` (0 stops the background military snapshot),
`FLIGHTS_AIRPORT_WATCH` (0 stops sampling favourited airports), plus undeclared tuning:
`FLIGHTS_MAX_TILES`, `FLIGHTS_GPSJAM_INTERVAL`, `ADSBLOL_DAILY_BUDGET`. The military snapshot cadence is
fixed at one per 10-minute slot: baselines leave out hours with under 5 of their 6 snapshots, so the former
`FLIGHTS_MIL_SAMPLE_S` (any value of 900 s or more) kept every region learning; it is no longer read, and
`FLIGHTS_MIL_SAMPLER=0` is the way to stop the sampler. Everything works with no keys.

## Known gaps

- Zoomed out to the whole world you see military aircraft, emergencies and recently loaded areas
  only; full world traffic would need a bulk feed (adsb.fi's feeder snapshot, or an OpenSky world
  snapshot every couple of minutes with an API client).
- Many military, government and private flights do not broadcast ADS-B or are filtered by the
  feeds; MLAT coverage varies.
- adsbdb routes are schedule-based and can be wrong for charters, diversions and reused callsigns;
  the card says so and flags routes far from the aircraft's position.
- GPS interference needs a day of samples before most cells get a colour, and is sparse at night.
- OFAC aircraft are matched by Mode S code (8 records carry one) or registration; transponder
  codes for listed registrations are learned over time while the map is in use.
- Airport delays and flight restrictions cover the US only. No European delay feed was used:
  EUROCONTROL's public NOP portal has no documented open feed (it would mean scraping) and its
  data services need registration.
- FIR outlines come from a flight-simulation project; they follow the real FIRs closely but are not
  official. Bulletins covering part of a FIR (waters only, provinces, west of a line) are shaded
  on the whole FIR with a dashed outline; only meridian limits are drawn exactly.
- The EASA safety directives that prohibit Belarusian airspace (FIR Minsk) are not in the bulletin
  list, so Belarus is not shaded; the Ukraine bulletin mentions them in its text.
- NAS Status lists only airports with something to report, so no entry means the FAA reports
  nothing for that airport, not a confirmed all-clear.
- Air activity baselines need history: a week for military surges and airport drops (two weeks for
  weekday patterns), five days with enough aircraft at the same hours for GPS trends. A new installation says "learning" for that long; the
  history seed helps only if the military layer was in use during the previous 72 hours.
- GPS trends need 2 affected aircraft in the same hour (twice in 6 hours): sparse cells where jamming
  reaches only one aircraft an hour are not called new. Cells far from the sampler circles are judged only
  at hours you viewed them on 5 of the previous 7 days, and at low confidence.
- Military surges see only aircraft that broadcast ADS-B and are flagged military in the community
  database; Russian, Chinese and many other air forces rarely appear, so a quiet Taiwan Strait or Barents
  Sea says little. Four regions have no receiver-coverage check (no GPS-sampler circle there).
- Airport drops have background coverage only for the 25 airports inside the GPS-sampler circles and for
  favourites; the others build their baseline only from hours you viewed them, which may take weeks. A
  receiver near the airport going offline looks like a drop (the overflight check catches network-wide
  outages, not one receiver). The infra airport card could show `/api/flights/airport/{ident}/movements`
  (a change on infra's side). "My places" (workspace module) could later add watched airports.
- Ideas: dismissals ("Not interesting here") once the core has them; NOTAM links for exercise areas;
  orbit detection for surveillance and tanker tracks from the snapshots.
