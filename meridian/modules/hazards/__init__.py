"""Live natural hazards and health events: earthquakes, fires, natural events, disaster alerts,
tropical storms, volcanoes, weather warnings, tsunami messages, disease outbreaks, Copernicus
emergency mapping activations, England flood warnings with river and sea levels, and the GloFAS
flood outlook.

Sources (all open, credited in the layer text and the status panel): USGS, NASA FIRMS, NASA EONET,
GDACS, NOAA NHC, Smithsonian GVP, USGS HANS, US NWS, MeteoAlarm, NOAA tsunami.gov, WHO, Copernicus
EMS (rapid mapping and GloFAS) and the Environment Agency. See docs/modules/hazards.md.
"""

from __future__ import annotations

import threading
import time

from meridian.registry import HTTPError

from . import entities, gdacs, plus, providers, who
from .common import (KINDS, LAYER_DISASTERS, LAYER_FIRES, LAYER_NATURAL, LAYER_OUTBREAKS, LAYER_QUAKES, LAYER_STORMS,
                     LAYER_TSUNAMI, LAYER_VOLCANOES, LAYER_WARNINGS, fc)
from .service import Hazards, countries_dataset_build, gvp_dataset_build

_svc: Hazards | None = None

HOSTS = {"api.weather.gov": 0.25, "feeds.meteoalarm.org": 0.5, "www.gdacs.org": 0.5, "eonet.gsfc.nasa.gov": 1.0,
         "firms.modaps.eosdis.nasa.gov": 1.0, "www.who.int": 1.0, "webservices.volcano.si.edu": 1.0,
         "volcanoes.usgs.gov": 1.0, "mapservices.weather.noaa.gov": 0.5, "www.nhc.noaa.gov": 1.0,
         "www.tsunami.gov": 1.0, "gitlab.com": 1.0}
NATURAL_CATS = {"wildfires", "severeStorms", "volcanoes", "floods", "seaLakeIce", "landslides", "dustHaze",
                "drought", "snow", "tempExtremes", "earthquakes", "manmade", "waterColor"}


def get_service() -> Hazards | None:
    """The running service (other modules may read it; it is never required)."""
    return _svc


def register(m) -> None:
    global _svc
    svc = Hazards(m)
    _svc = svc
    for host, gap in HOSTS.items():
        m.net.set_host_interval(host, gap)

    # -- keys and settings ------------------------------------------------------------------------
    m.key("FIRMS_MAP_KEY", "NASA FIRMS map key (free). Uses the FIRMS area API for active fires; without it the "
          "public 24-hour files are used.", "https://firms.modaps.eosdis.nasa.gov/api/map_key/", secret=True)
    m.key("HAZARDS_FIRMS_SATELLITES", "Fire satellites to load: noaa20, noaa21, snpp, modis (default "
          "noaa20,snpp,modis; each VIIRS file is about 8 MB a day).", "", secret=False)
    m.key("HAZARDS_METEOALARM_COUNTRIES", "MeteoAlarm countries for weather warnings, by feed name or ISO code "
          "(default all 39; Germany and Switzerland are the largest feeds).", "https://feeds.meteoalarm.org/", secret=False)
    m.key("HAZARDS_BACKGROUND", "Keep a slow refresh of earthquakes, disasters, storms, tsunami messages and outbreaks "
          "when no browser is open, so the AI and convergences stay current (default on).", "", secret=False)

    # -- sources ----------------------------------------------------------------------------------
    idle = "while in use" if not svc.background else "while in use, slower otherwise"
    m.source("hazards.usgs", "USGS earthquake feeds", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php",
             "US Government public domain (USGS)", refresh=f"every 5 min {idle}", layer=LAYER_QUAKES)
    m.source("hazards.firms", "NASA FIRMS active fires (VIIRS and MODIS)", "https://firms.modaps.eosdis.nasa.gov/",
             "NASA open data (EOSDIS data use policy), cite NASA FIRMS", refresh="every 30 min while the layer is on",
             layer=LAYER_FIRES)
    m.source("hazards.eonet", "NASA EONET natural events", "https://eonet.gsfc.nasa.gov/", "NASA open data",
             refresh=f"every 15 min {idle}", layer=LAYER_NATURAL)
    m.source("hazards.gdacs", "GDACS disaster alerts (EC JRC, UN OCHA)", "https://www.gdacs.org/",
             "GDACS terms of use: free use with credit to GDACS", refresh=f"every 10 min {idle}", layer=LAYER_DISASTERS)
    m.source("hazards.nhc", "NOAA National Hurricane Center", "https://www.nhc.noaa.gov/",
             "US Government public domain (NOAA)", refresh=f"every 10 min {idle}", layer=LAYER_STORMS)
    m.source("hazards.gvp", "Smithsonian Global Volcanism Program, Volcanoes of the World",
             "https://volcano.si.edu/", "Smithsonian GVP: free non-commercial use with citation", refresh="monthly",
             layer=LAYER_VOLCANOES)
    m.source("hazards.hans", "USGS Volcano Hazards Program notices (HANS)", "https://volcanoes.usgs.gov/hans-public/",
             "US Government public domain (USGS)", refresh=f"every 30 min {idle}", layer=LAYER_VOLCANOES)
    m.source("hazards.nws", "US National Weather Service alerts", "https://www.weather.gov/documentation/services-web-api",
             "US Government public domain (NOAA/NWS)", refresh="every 5 min while the layer is on", layer=LAYER_WARNINGS)
    m.source("hazards.nws_zones", "NWS forecast, county and marine zone outlines", "https://api.weather.gov/zones",
             "US Government public domain (NOAA/NWS)", refresh="once per zone, kept 6 months", layer=LAYER_WARNINGS)
    m.source("hazards.meteoalarm", "MeteoAlarm warnings (EUMETNET, 39 countries)", "https://meteoalarm.org/",
             "MeteoAlarm terms (equivalent to CC BY 4.0, credit MeteoAlarm and the national service)",
             refresh="every 30 min while the layer is on", layer=LAYER_WARNINGS)
    m.source("hazards.meteoalarm_regions", "MeteoAlarm warning region outlines (geocodes)",
             "https://gitlab.com/meteoalarm-pm-group/documents", "MeteoAlarm terms",
             refresh="when MeteoAlarm publishes a new file", layer=LAYER_WARNINGS)
    m.source("hazards.tsunami", "NOAA tsunami warning centres", "https://www.tsunami.gov/",
             "US Government public domain (NOAA)", refresh="every 5 min, history kept 4 months", layer=LAYER_TSUNAMI)
    m.source("hazards.who", "WHO Disease Outbreak News", "https://www.who.int/emergencies/disease-outbreak-news",
             "WHO terms of use: non-commercial reuse with attribution", refresh="every 6 hours", layer=LAYER_OUTBREAKS)
    m.source("hazards.naturalearth", "Natural Earth country outlines", "https://www.naturalearthdata.com/",
             "Public domain", refresh="every 90 days", layer=LAYER_OUTBREAKS)

    # -- layers (the browser registers them; these rows feed the layer table) ----------------------
    m.layer(LAYER_QUAKES, "Earthquakes", "Events", description="USGS earthquakes: past day, past week M2.5+ or "
            "significant past month.", source="USGS", refresh="5 min", use="Click a quake for depth, felt reports, "
            "PAGER alert and ShakeMap.", count=lambda: len(svc.quakes))
    m.layer(LAYER_DISASTERS, "Disaster alerts", "Events", description="GDACS alerts for earthquakes, cyclones, floods, "
            "volcanoes, droughts and wildfires.", source="GDACS", refresh="10 min", count=lambda: len(svc.gdacs_events))
    m.layer(LAYER_STORMS, "Tropical storms", "Events", description="Active cyclones with tracks, forecast cones and "
            "wind areas.", source="NOAA NHC, GDACS, NASA EONET", refresh="10 min",
            count=lambda: sum(1 for s in svc.storms if s["active"]))
    m.layer(LAYER_FIRES, "Active fires", "Events", description="Satellite fire detections from the past 24 hours.",
            source="NASA FIRMS", refresh="30 min", count=lambda: len(svc.fire_data))
    m.layer(LAYER_VOLCANOES, "Volcanoes", "Events", description="Holocene volcanoes, with current alerts and eruptions.",
            source="Smithsonian GVP, USGS HANS, NASA EONET, GDACS", refresh="30 min",
            count=lambda: len(svc.volcano_list))
    m.layer(LAYER_WARNINGS, "Weather warnings", "Events", description="Official warnings for the US and Europe, "
            "coloured by severity.", source="NWS, MeteoAlarm", refresh="5 to 30 min",
            count=lambda: len(svc.all_warnings()))
    m.layer(LAYER_TSUNAMI, "Tsunami messages", "Events", description="Messages from the US tsunami warning centres.",
            source="NOAA tsunami.gov", refresh="5 min", count=lambda: len(svc.tsunami_msgs))
    m.layer(LAYER_NATURAL, "Natural events", "Events", description="NASA EONET: storms, volcanoes, ice, floods, dust "
            "and more.", source="NASA EONET", refresh="15 min", count=lambda: len(svc.eonet_events))
    m.layer(LAYER_OUTBREAKS, "Disease outbreaks", "Events", description="WHO Disease Outbreak News from the past 60 days.",
            source="WHO", refresh="6 hours", count=lambda: len(svc.active_outbreaks(who.ACTIVE_DAYS)))

    # -- datasets and start-up loads --------------------------------------------------------------
    m.dataset("gvp", gvp_dataset_build, max_age_hours=24 * 30, description="Smithsonian GVP Holocene volcanoes")
    m.dataset("countries", countries_dataset_build, max_age_hours=24 * 90,
              description="Natural Earth 1:110m country outlines (outbreak areas)")
    m.on_dataset("gvp", svc.load_gvp)
    m.on_dataset("countries", svc.load_country_shapes)
    svc.ensure_tables()
    threading.Thread(target=svc.load_ma_regions, daemon=True, name="hazards-load-regions").start()

    # -- background work: one ticking task per source; each decides whether it is due ------------
    for name, delay in (("quakes", 2), ("tsunami", 5), ("gdacs", 7), ("eonet", 9), ("nhc", 11), ("hans", 13),
                        ("who", 17), ("nws", 6), ("zones", 20), ("meteoalarm", 10), ("ma_regions", 25),
                        ("fires", 4)):
        m.task(name, (lambda n=name: svc.tick(n)), every=20 if name in ("zones",) else 30, initial_delay=delay)
    m.task("housekeeping", svc.housekeeping, every=6 * 3600, initial_delay=600)

    # -- emergency mapping, England floods and the flood outlook (plus.py) -------------------------
    plus.register(m, svc)

    # -- routes -----------------------------------------------------------------------------------
    _routes(m, svc)

    # -- discovery ----------------------------------------------------------------------------------
    m.search(lambda q, limit: providers.search(svc, q, limit))
    m.nearby(lambda lat, lon, r, limit: providers.nearby(svc, lat, lon, r, limit))
    m.events(lambda since: providers.events(svc, since))
    m.ai_tool(
        "hazards_recent",
        "Recent notable natural hazards and health events (earthquakes M4.5+, fire clusters, GDACS disaster alerts, "
        "active tropical storms, volcano alerts, severe weather warnings, tsunami messages, WHO outbreak reports, "
        "England flood warnings, Copernicus emergency mapping activations). "
        "Each item has kind, title, lat, lon, time (epoch s) and severity 0-10.",
        {"type": "object", "properties": {
            "bbox": {"type": "string", "description": "west,south,east,north in degrees (optional)"},
            "kinds": {"type": "array", "items": {"type": "string", "enum": list(KINDS)},
                      "description": "Only these kinds (optional)"},
            "hours": {"type": "number", "description": "Look back this many hours (default 48, max 720)"},
            "min_severity": {"type": "integer", "description": "Lowest severity 0-10 (default 3)"},
            "limit": {"type": "integer", "description": "Most items to return (default 40)"}},
         "additionalProperties": False},
        lambda a: providers.tool_recent(svc, a))
    m.ai_tool(
        "earthquakes",
        "Earthquakes from USGS: filter by minimum magnitude, a bounding box or a region name (country or place text) "
        "and a time window. Windows longer than a week query the USGS event service.",
        {"type": "object", "properties": {
            "min_magnitude": {"type": "number", "description": "Default 4.5"},
            "bbox": {"type": "string", "description": "west,south,east,north (optional)"},
            "region": {"type": "string", "description": "Country or place name, e.g. Japan or Alaska (optional)"},
            "hours": {"type": "number", "description": "Look back this many hours (default 168)"},
            "limit": {"type": "integer", "description": "Default 30, max 200"}},
         "additionalProperties": False},
        lambda a: providers.tool_earthquakes(svc, a))
    m.ai_tool(
        "weather_warnings_at",
        "Official weather warnings in force or upcoming at a point: US National Weather Service alerts and "
        "MeteoAlarm warnings for Europe (including the UK).",
        {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
         "required": ["lat", "lon"], "additionalProperties": False},
        lambda a: providers.tool_warnings_at(svc, a))


def _routes(m, svc: Hazards) -> None:
    def touch(layer: str, *sources: str) -> None:
        svc.touch(layer)
        for s in sources:
            svc.kick(s)

    @m.route("GET", "/api/hazards/quakes")
    def r_quakes(req):
        window = req.arg("window", "week", choices={"day", "week", "month"})
        min_mag = req.arg("min_mag", -1.0, type=float, min=-2.0, max=9.9)
        touch(LAYER_QUAKES, "tsunami")
        st = svc.state["quakes"]
        if window in ("day", "week") and time.time() - svc._quake_feed_at["day"] > 330:
            svc.kick("quakes", force=True)
        else:
            svc.kick("quakes")
        return svc.cached_json(f"quakes:{window}:{min_mag}", (st.version, int(time.time() // 60)),
                               lambda: svc.quakes_fc(window, min_mag))

    @m.route("GET", "/api/hazards/fires")
    def r_fires(req):
        bbox = req.bbox("bbox", (-180.0, -85.0, 180.0, 85.0))
        zoom = req.arg("zoom", 2.0, type=float, min=0.0, max=24.0)
        conf = req.arg("conf", 0, type=int, min=0, max=2)
        touch(LAYER_FIRES, "fires")
        if not len(svc.fire_data):
            svc.kick("eonet")
        return svc.fires_payload(bbox, zoom, conf)

    @m.route("GET", "/api/hazards/natural")
    def r_natural(req):
        cats = {c for c in (req.arg("cats", "") or "").split(",") if c in NATURAL_CATS} or None
        touch(LAYER_NATURAL, "eonet")
        return svc.cached_json(f"natural:{sorted(cats or [])}", svc.state["eonet"].version,
                               lambda: svc.natural_fc(cats))

    @m.route("GET", "/api/hazards/disasters")
    def r_disasters(req):
        types = {t for t in (req.arg("types", "") or "").upper().split(",") if t in gdacs.TYPES} or None
        min_alert = req.arg("min_alert", 1, type=int, min=1, max=3)
        touch(LAYER_DISASTERS, "gdacs")
        return svc.cached_json(f"disasters:{sorted(types or [])}:{min_alert}", svc.state["gdacs"].version,
                               lambda: svc.disasters_fc(types, min_alert))

    @m.route("GET", "/api/hazards/disaster-geometry")
    def r_disaster_geometry(req):
        eid = req.arg("id")
        if not eid.startswith("gdacs:") or len(eid) > 60:
            raise HTTPError(400, "BAD_ARG", "id must be a GDACS event id like gdacs:FL:1103456.")
        svc.touch(LAYER_DISASTERS)
        try:
            return svc.disaster_geometry(eid)
        except Exception:
            return fc([], error="GDACS did not return an outline for this event.")

    @m.route("GET", "/api/hazards/storms")
    def r_storms(req):
        ended = req.arg("ended", False, type=bool)
        touch(LAYER_STORMS, "gdacs", "nhc")
        return svc.cached_json(f"storms:{ended}", (svc.versions["storms"], int(time.time() // 300)),
                               lambda: svc.storms_fc(ended))

    @m.route("GET", "/api/hazards/volcanoes")
    def r_volcanoes(req):
        scope = req.arg("scope", "all", choices={"all", "active"})
        touch(LAYER_VOLCANOES, "hans", "eonet")
        return svc.cached_json(f"volcanoes:{scope}", svc.versions["volcanoes"], lambda: svc.volcanoes_fc(scope))

    @m.route("GET", "/api/hazards/warnings")
    def r_warnings(req):
        bbox = req.bbox("bbox", (-180.0, -85.0, 180.0, 85.0))
        zoom = req.arg("zoom", 3.0, type=float, min=0.0, max=24.0)
        min_rank = req.arg("min", 2, type=int, min=0, max=4)
        src = {s for s in (req.arg("src", "nws,meteoalarm") or "").split(",") if s in ("nws", "meteoalarm")} \
            or {"nws", "meteoalarm"}
        touch(LAYER_WARNINGS, "nws", "meteoalarm")
        if not svc.nws_full:
            svc.kick("nws", force=True)
        return svc.warnings_fc(bbox, zoom, min_rank, src)

    @m.route("GET", "/api/hazards/tsunami")
    def r_tsunami(req):
        days = req.arg("days", 7, type=int, min=1, max=90)
        touch(LAYER_TSUNAMI, "tsunami")
        return svc.cached_json(f"tsunami:{days}", (svc.state["tsunami"].version, int(time.time() // 600)),
                               lambda: svc.tsunami_fc(days))

    @m.route("GET", "/api/hazards/outbreaks")
    def r_outbreaks(req):
        days = req.arg("days", who.ACTIVE_DAYS, type=int, min=7, max=365)
        touch(LAYER_OUTBREAKS, "who")
        return svc.cached_json(f"outbreaks:{days}", (svc.versions["outbreaks"], int(time.time() // 3600)),
                               lambda: svc.outbreaks_fc(days))

    @m.route("GET", "/api/hazards/details")
    def r_details(req):
        kind = req.arg("kind", choices=set(KINDS))
        eid = req.arg("id")
        if len(eid) > 200 or ":" not in eid:
            raise HTTPError(400, "BAD_ARG", "id must look like source:native-id.")
        return entities.details(svc, kind, eid)

    @m.route("GET", "/api/hazards/here")
    def r_here(req):
        lat, lon = req.latlon()
        return providers.here(svc, lat, lon)

    @m.route("GET", "/api/hazards/recent")
    def r_recent(req):
        args = {"hours": req.arg("hours", 48.0, type=float, min=0.5, max=720.0),
                "kinds": req.arg("kinds", ""), "bbox": req.arg("bbox", None),
                "min_severity": req.arg("min_severity", 3, type=int, min=0, max=10),
                "limit": req.arg("limit", 100, type=int, min=1, max=200)}
        try:
            return providers.tool_recent(svc, args)
        except ValueError as err:
            raise HTTPError(400, "BAD_ARG", str(err)) from None

    @m.route("GET", "/api/hazards/summary")
    def r_summary(req):
        return {
            "sources": {name: dict(st.info(), sid=st.sid) for name, st in svc.state.items()},
            "counts": {"quakes": len(svc.quakes), "fires": len(svc.fire_data), "natural": len(svc.eonet_events),
                       "disasters": len(svc.gdacs_events), "storms_active": sum(1 for s in svc.storms if s["active"]),
                       "volcanoes": len(svc.volcano_list), "volcanoes_active": len(svc.volcano_activity),
                       "warnings_nws": len(svc.nws_alerts),
                       "warnings_meteoalarm": sum(len(v) for v in svc.ma_warnings.values()),
                       "tsunami_messages": len(svc.tsunami_msgs), "outbreaks_active": len(svc.active_outbreaks(60))},
            "fires": {k: v for k, v in svc.fire_meta.items() if k != "sources"} | {"sources": svc.fire_meta.get("sources")},
            "plus": svc.extras.summary() if svc.extras else None,
        }
