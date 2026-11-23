# Timeline module

Timeline replay: scrub or play back the last 72 hours of aircraft, ships and events. A time bar
docks above the status bar; dragging its playhead, pressing play or stepping puts Meridian into
replay (`ctx.time`, CONTRACT 15.3). The live aircraft and vessel layers step aside and replay layers
draw what Meridian recorded at that time. "Back to live" (or closing the bar) puts everything back.

Files: `meridian/modules/timeline/__init__.py` (the `history_near` AI tool), `web/js/layers/timeline.js`
(controller) and `web/js/layers/timeline/` (`bar.js` the time bar, `data.js` prefetch and tracks,
`render.js` map layers, `cards.js` replay cards, `interp.js` the maths), `web/css/timeline.css`,
`tests/test_timeline.py` and `tests/test_timeline_web.mjs`. The positions come from history kept by
the flights module (`meridian/modules/flights/history.py`) and the ships module
(`meridian/modules/ships/history.py`); see their docs.

## Sources

Nothing new is fetched from the internet. Replay reads Meridian's own history:

| History | Kept by | Endpoint | What it holds |
|---|---|---|---|
| Events | core (CONTRACT 15.3) | `/api/core/history/events`, `/api/core/history/summary` | Every event provider's output, snapshotted every 5 minutes, 30 days |
| Aircraft | flights | `/api/flights/history` (CONTRACT 15.4) | Positions sampled from what the flights module already receives, at least 60 s and at most 5 minutes apart, 72 h |
| Vessels | ships | `/api/ships/history` (CONTRACT 15.4) | The AIS track points the ships module keeps (2 minutes to 2 hours apart), 72 h |

History only exists for times Meridian was running, and aircraft only where the map was being
viewed (plus worldwide military aircraft while that layer or a zoomed-out view was in use, and
emergencies at all times): the flights module fetches traffic for the area on screen and nothing
else. Vessels are recorded continuously wherever the AIS sources reach (Finnish waters and the
northern Baltic without an aisstream.io key). The time bar's coverage strips show where each exists.

## Using it

- **Open**: the "Timeline" toolbar button or the `R` key (the first free of R, H, Y and K; the button's
  tooltip names it). The bar opens live, the playhead at the right end.
- **The bar**: from the left, play and pause, four speeds (1 min/s, 5 min/s, 15 min/s, 1 h/s; picking
  one starts playing), one hour back and forward, the track, the replay time in UTC with what is on
  the map, "Back to live" and close.
- **The track** spans the last 72 hours, or since history began. Columns are UTC hours of events in
  view (log scale, as ongoing events all count in the hour of their latest report), coloured by the
  highest severity: grey routine (0-2), amber notable (3-5), red serious (6-8), red with a pale cap
  extreme (9-10). Below them a pink strip marks hours with aircraft positions and a blue one hours with
  vessel positions. Hovering shows the hour's counts and severity in words.
- **Keyboard** (on the track): left and right 5 minutes (with Shift 1 minute), Page Up and Page Down one
  hour, Home the start, End back to live, Space play or pause, Esc closes the bar.
- **Empty state**: with no history at all the track says "History starts when Meridian has been
  running for a while."

## Replay

Entering replay remembers which of `flights`, `flights.military`, `flights.alerts`, `ships`,
`ships.dark` and `ships.sanctioned` were on and turns them off without saving that (a reload is live
and brings them back). Other layers keep showing the present. Replay layers (`ov-timeline-*`, icons
`timeline-plane` and `timeline-ship`) exist only while replaying.

- **Aircraft** are drawn where they were at the replay time: between two samples up to 15 minutes
  apart on the path between them (a straight line in latitude and longitude for samples under 1.2
  degrees apart, which is within about 200 m of the great circle, and the great circle beyond), with
  the altitude and track interpolated; after the last sample the aircraft stays 2 minutes (drawn faded
  after 75 s), as the live map drops an aircraft a minute or two after its last report. Class colours
  as on the live layer, emergencies red and larger, OFAC-listed ringed in red, callsign labels from
  zoom 7, and a 4-minute fading trail.
- **Vessels**: between points up to an hour apart (7.5 hours when both are stationary) on a straight
  line; after the last point the vessel stays an hour under way or six hours stationary, faded after 15
  minutes and 1 hour, which is what the live map does. Arrows under way, dots when stationary, a
  one-hour fading trail.
- **Events** active around the replay time: an event appears at its first time, reaches full
  strength over 5 minutes, stays while its span lasts (storms, tracked ships, jamming cells), then
  fades out over 1, 3, 6 or 12 hours by severity. Never before it happened.
- **Prefetch**: aircraft in 15-minute and vessels in 30-minute requests for the view padded by 30%,
  enough around the playhead to draw it (17 minutes back and 7 ahead for aircraft, 2 h 15 back and 35
  minutes ahead for vessels) plus four seconds of playback ahead, at most three requests per kind at a
  time. Panning within the padded box reuses what is loaded; leaving it starts again. Chunks far from
  the playhead are dropped (32 and 24 kept). Chunks touching the present are refreshed every minute.
  Events come in one request for the range and view, refreshed every 5 minutes.
- **Caps**: at most 4,000 aircraft and 4,000 vessels drawn (emergency, OFAC-listed and military
  aircraft first), 1,500 and 1,200 trails; the bar says "Showing part of the traffic: zoom in for all
  of it" when a cap or a request limit bites.
- **Smoothness**: redraws at most about 11 times a second (fewer when a redraw is expensive), vessels
  about 3 times a second while playing; playback waits for data rather than jumping over it.

**Cards.** Clicking a replay aircraft, vessel or event opens a card for that moment ("Position at
14:05 UTC") with the interpolated readings, the samples it is between, what is recorded for it, and
"Open live card" (the module's own card for the same aircraft, vessel or event, without leaving
replay) and "Show whole track" (everything recorded in the range). Replay cards follow the playhead
and close when replay ends.

**Leaving replay** ("Back to live", End, closing the bar, or anything calling `ctx.time.live()`)
closes replay cards, removes the replay layers and turns back on exactly the live layers it turned
off (a layer you switched on yourself during replay stays on). If something else puts Meridian into
replay (`ctx.time.setReplay`), the bar opens and follows it.

## AI tool and map action

- `history_near {lat, lon, from?, to?, radius_km?, kinds?, min_severity?, limit?}`: events Meridian
  recorded near a place in a past window (default the last 24 h, at most 30 days; times as ISO 8601
  or epoch seconds; radius 1 to 1,000 km, default 100), most severe first, with `distance_km`, UTC
  times, a few provider fields (magnitude and the like) and counts by kind.
- Browser map action `replay_at {time, lat?, lon?, zoom?}` opens the bar at that time (for the
  assistant once its server side offers it, and for other modules).
- Debugging handle `window.__meridian.timeline` (`open`, `seek`, `play`, `pause`, `live`, `state`,
  `stats`, `openCard`, `visible`).

## Storage and volume

The timeline stores nothing itself. Measured on the development copy (keyless, one map view open
over the Gulf of Finland, worldwide military polled while in use): about 6,000 aircraft samples an
hour (626 to 1,550 aircraft an hour, 4 to 7 samples per aircraft-hour), 62 bytes a sample with its
index and callsign spells, so about 0.4 MB an hour; vessels 2,000 points an hour from Digitraffic
alone and up to 40,000 an hour with the aisstream.io world feed, 41 bytes a point (0.1 to 1.6 MB an
hour). Caps: 2 million aircraft samples (about 120 MB) and 2.5 million vessel points (about 100 MB).

## Performance

Server, on the development data: a 15-minute aircraft request for a regional view 12 to 35 ms, a
30-minute vessel request (about 600 vessels, 32 KB) 25 to 80 ms, the 72-hour event summary about
120 ms. Browser: a replay redraw with real data (about 100 aircraft, 4,000 vessels, 240 events) takes
1 to 7 ms of script; with 6,000 synthetic aircraft and 6,000 vessels (4,000 of each drawn) 41 ms
median. In the headless test browser (software WebGL) the map itself repaints at about 9 frames a
second with the live layers, and replay plays at 6 to 7 while keeping time (275 to 292 replay seconds
per second at 5 min/s); a hardware GPU is much faster.

## Known gaps

- Aircraft history exists only for areas someone was watching (and worldwide military and
  emergencies); there is no world feed. Replay of a time Meridian was not running is empty.
- Vessel class and name are today's, not those at the replay time; the AIS track has no per-point
  status. Aircraft callsigns, registrations and types are as they were (callsign spells).
- The event histogram counts events in view by the hour of their latest report, so a long-running
  event appears once, at its end.
- Every history request marks the flights or ships module active (core behaviour), which makes their
  live pollers use their in-use cadence while replaying.
