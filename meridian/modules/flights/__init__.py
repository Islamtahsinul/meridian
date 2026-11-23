"""Live ADS-B aircraft: merged adsb.fi, adsb.lol, OpenSky and local receiver data, emergencies,
military, OFAC-listed aircraft, trails, airport traffic and GPS interference. Airspace and airport
status: US airport delays (FAA NAS Status), US temporary flight restrictions (FAA) and EASA
conflict-zone bulletins. Air activity: military surges by region, GPS interference trends and
airport movement drops against Meridian's own baselines (activity*.py)."""

from __future__ import annotations

import logging
import time

from . import shapes
from .airspace import AirspaceService
from .airspace_api import register_airspace_ai, register_airspace_discovery, register_airspace_routes
from .api import register_ai, register_discovery, register_routes
from .history import register_history
from .service import FlightsService

try:                     # the detectors are optional: the live layers work without them (rule 7)
    from .activity import ActivityService
    from .activity_api import register_activity
    _ACTIVITY_ERROR = None
except Exception as _err:   # pragma: no cover - only when a shared helper is broken
    ActivityService = None
    register_activity = None
    _ACTIVITY_ERROR = _err


def register(m) -> None:
    m.key("OPENSKY_CLIENT_ID", "OpenSky Network API client id. With the secret it raises OpenSky's daily "
          "allowance from 400 to 4,000 credits.", "https://opensky-network.org/my-opensky/account")
    m.key("OPENSKY_CLIENT_SECRET", "OpenSky Network API client secret (pairs with OPENSKY_CLIENT_ID).",
          "https://opensky-network.org/my-opensky/account")
    m.key("ADSBLOL_API_KEY", "adsb.lol API key. Without one adsb.lol allows 500 metered requests a day, so it is "
          "only a fallback.", "https://www.adsb.lol/docs/open-data/api/")
    m.key("LOCAL_ADSB_URL", "Your own receiver's aircraft.json (readsb or tar1090), for example "
          "http://10.0.0.20/tar1090/data/aircraft.json.", "https://github.com/wiedehopf/tar1090", secret=False)
    m.key("FLIGHTS_GPSJAM_SAMPLER", "Set to 0 to stop the background GPS interference sampler "
          "(16 hotspots, every 5 min while you use Meridian, every 10 min otherwise).", secret=False)
    m.key("FLIGHTS_PROVIDERS", "Order of live sources for map tiles, default adsbfi,adsblol,opensky.", secret=False)
    m.key("FLIGHTS_MIL_SAMPLER", "Set to 0 to stop the background military snapshot (the worldwide military list "
          "every 10 min, 144 small requests a day) that the military surge baselines need.", secret=False)
    m.key("FLIGHTS_AIRPORT_WATCH", "Set to 0 to stop sampling favourited airports (up to 6, one small request each "
          "every 10 min) for the airport movement baselines.", secret=False)

    m.source("flights.adsbfi", "adsb.fi open data API", "https://adsb.fi/",
             "Personal, non-commercial use; credit adsb.fi with a link", layer="flights",
             refresh="every 10 to 15 s for the area on screen, military and emergencies every 60 s")
    m.source("flights.adsblol", "adsb.lol API", "https://www.adsb.lol/", "ODbL 1.0, attribution required",
             layer="flights", refresh="fallback and trail history (500 metered requests a day without a key)")
    m.source("flights.opensky", "OpenSky Network REST API", "https://opensky-network.org/",
             "OpenSky terms of use, non-commercial", layer="flights",
             refresh="fallback (400 credits a day anonymously, 4,000 with an API client)")
    m.source("flights.local", "Local ADS-B receiver (readsb or tar1090)", "https://github.com/wiedehopf/readsb",
             "Your own receiver", layer="flights", refresh="every 5 s while in use", needs_key="LOCAL_ADSB_URL")
    m.source("flights.adsbdb", "adsbdb (routes and aircraft details)", "https://www.adsbdb.com/",
             "Free public API; routes are unverified", layer="flights", refresh="when a card opens, cached a day")
    m.source("flights.gpsjam", "GPS interference (derived from adsb.fi navigation accuracy)", "https://adsb.fi/",
             "Derived from adsb.fi open data", layer="flights.gpsjam",
             refresh="16 hotspots every 5 min while in use, every 10 min otherwise")
    m.source("flights.faa_nas", "FAA NAS Status (US airport delays)", "https://nasstatus.faa.gov/",
             "US Government work, public domain", layer="flights.delays",
             refresh="every 2 min while in use, every 15 min otherwise")
    m.source("flights.faa_tfr", "FAA temporary flight restrictions (tfr.faa.gov)", "https://tfr.faa.gov/",
             "US Government work, public domain", layer="flights.tfr",
             refresh="every 5 min while in use, every 30 min otherwise; each notice once, again when it changes")
    m.source("flights.easa_czib", "EASA Conflict Zone Information Bulletins",
             "https://www.easa.europa.eu/en/domains/air-operations/czibs",
             "EASA: reproduction authorised provided the source is acknowledged", layer="flights.czib",
             refresh="every 2 h while in use, every 12 h otherwise")
    m.source("flights.vatspy_firs", "FIR outlines (VATSpy data project)", shapes.VATSPY_SITE,
             "CC BY-SA 4.0; made for flight simulation, outlines are approximate", layer="flights.czib",
             refresh="weekly")
    m.source("flights.ne_countries", "Natural Earth admin-0 countries (1:50m)", shapes.NE_SITE, "Public domain",
             layer="flights.czib", refresh="monthly")

    m.source("flights.activity", "Air activity detectors (derived from adsb.fi open data)", "https://adsb.fi/",
             "Derived from adsb.fi open data and the module's own history; personal, non-commercial",
             layer="flights.activity", optional=True,
             refresh="worldwide military list every 10 min (144 requests a day); GPS cells and airports from data "
                     "already received, plus favourited airports every 10 min; assessed every 5 min")

    svc = FlightsService(m)
    m.flights = svc  # handle for tests and debugging
    air = AirspaceService(m)
    m.airspace = air
    act = None
    if ActivityService is not None:
        try:
            act = ActivityService(m, svc, air)
        except Exception as err:          # e.g. a damaged table: the rest of the module still runs
            m.log.warning("air activity detectors are off: %s", err)
            act = None
    elif _ACTIVITY_ERROR is not None:
        logging.getLogger("meridian.flights").warning("air activity detectors are off: %s", _ACTIVITY_ERROR)
    svc.activity = act

    m.layer("flights", "Aircraft", "Flights",
            description="Live ADS-B aircraft for the area on screen, coloured by class, with emergencies in red "
                        "and OFAC-listed aircraft ringed.",
            source="adsb.fi (personal non-commercial), adsb.lol (ODbL), OpenSky Network, adsbdb routes",
            refresh="every 10 s", use="Who is flying where, traffic around an airport, unusual aircraft",
            count=lambda: svc.tracker.counts(time.time())["live"])
    m.layer("flights.military", "Military aircraft", "Flights",
            description="Military aircraft worldwide that broadcast ADS-B (many do not).",
            source="adsb.fi, adsb.lol", refresh="every 60 s", use="Tanker, transport and patrol activity",
            count=lambda: len(svc.mil_hexes))
    m.layer("flights.alerts", "Aircraft emergencies", "Flights",
            description="Aircraft worldwide squawking 7500, 7600 or 7700 or reporting an emergency.",
            source="adsb.fi squawk search", refresh="every 60 s", use="Emergencies as they happen",
            count=lambda: svc.tracker.counts(time.time())["emergency"])
    m.layer("flights.gpsjam", "GPS interference", "Conflict",
            description="Share of aircraft reporting degraded GPS accuracy per 0.5 degree cell, last 24 h.",
            source="Derived from adsb.fi navigation accuracy", refresh="every 5 min",
            use="Jamming around conflict zones and military exercises",
            count=lambda: sum(1 for c in svc.gps.cells() if c["level"] in ("amber", "red")))
    m.layer("flights.delays", "Airport delays (US)", "Flights",
            description="US airports with FAA ground stops, ground delay programmes, arrival and departure delays, "
                        "closures and notices, coloured by severity.",
            source="FAA NAS Status (public domain)", refresh="every 2 min",
            use="Where flights are held or delayed and why",
            count=lambda: sum(1 for a in air.airports.values() if any(s["delay"] for s in a["statuses"])))
    m.layer("flights.tfr", "Flight restrictions (US)", "Flights",
            description="FAA temporary flight restrictions: VIP movement, security, hazards such as wildfires, space "
                        "operations, air shows and sporting events, with altitudes and times.",
            source="FAA tfr.faa.gov (public domain)", refresh="every 5 min",
            use="Where aircraft may not fly right now and why",
            count=lambda: sum(1 for t in air.tfrs.values() if t["status"] in ("active", "listed")))
    m.layer("flights.czib", "Conflict zone advisories", "Flights",
            description="EASA Conflict Zone Information Bulletins shaded on the flight information regions (or the "
                        "countries) they cover, with the recommendation and validity.",
            source="EASA (source acknowledged), FIR outlines from VATSpy (CC BY-SA 4.0), Natural Earth",
            refresh="every 2 h", use="Airspace EU operators are told to avoid or treat with caution",
            count=lambda: len(air.active_czibs()))
    m.layer("flights.activity", "Air activity", "Flights",
            description="Candidates from Meridian's own baselines: regions with more military aircraft than usual, "
                        "new or worsening GPS interference, and airports with far fewer movements than usual.",
            source="Derived from adsb.fi open data (personal non-commercial)", refresh="every 5 min",
            use="What changed in the air compared with the usual for the place and time",
            count=(lambda: act.flags()) if act is not None else None)

    register_routes(m, svc)
    register_history(m, svc)       # sampled positions for timeline replay (CONTRACT 15.4)
    register_discovery(m, svc)
    register_ai(m, svc)
    register_airspace_routes(m, air)
    register_airspace_discovery(m, air)
    register_airspace_ai(m, air)
    if act is not None:
        register_activity(m, act)
        m.events(act.events)
        m.task("activity-mil", act.sample_mil, every=30.0, initial_delay=15.0)
        m.task("activity-airports", act.sample_airports, every=60.0, initial_delay=45.0)
        m.task("activity-detect", act.detect, every=300.0, initial_delay=90.0)
        m.task("activity-seed", act.seed_from_history, every=900.0, initial_delay=150.0)

    m.dataset("firs", shapes.build_firs, max_age_hours=24 * 7,
              description="FIR outlines from the VATSpy data project (for conflict-zone advisories)")
    m.dataset("countries", shapes.build_countries, max_age_hours=24 * 30,
              description="Natural Earth country outlines (for advisories limited to a country)")
    m.on_dataset("firs", air.rebuild_czib_shapes)
    m.on_dataset("countries", air.rebuild_czib_shapes)
    m.task("airport-status", air.tick_nas, every=30.0, initial_delay=12.0)
    m.task("tfr", air.tick_tfr, every=30.0, initial_delay=20.0)
    m.task("czib", air.tick_czib, every=120.0, initial_delay=30.0)
    m.task("airspace-housekeeping", air.housekeeping, every=3600.0, initial_delay=300.0)

    for q in svc.queues():
        m.thread(f"queue-{q.name}", q.run)
    m.task("tiles", svc.refresh_wanted_tiles, every=2.0, initial_delay=2.0)
    m.task("military", svc.poll_military, every=60.0, initial_delay=5.0,
           when=lambda: m.active("flights.mil", within=150))
    m.task("alerts", svc.poll_alerts, every=20.0, initial_delay=8.0)
    m.task("gps-sampler", svc.sample_hotspot, every=15.0, initial_delay=20.0)
    m.task("gps-flush", svc.flush_gps, every=60.0, initial_delay=60.0)
    m.task("watch", svc.poll_watch, every=10.0, initial_delay=10.0, when=lambda: m.active(within=60))
    m.task("sanctions", svc.sanctions_watch, every=60.0, initial_delay=30.0,
           when=lambda: m.active("flights.live", within=180) or m.active("flights.mil", within=180))
    if svc.local.enabled():
        m.task("local", svc.poll_local, every=5.0, initial_delay=3.0)
    m.task("housekeeping", svc.housekeeping, every=30.0, initial_delay=30.0)
