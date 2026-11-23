"""Hazards+ (Programme 2, phase 3a): Copernicus EMS rapid mapping activations, England flood
warnings with river and sea gauges, and the GloFAS flood outlook. Registered by the hazards
module's ``register(m)``; the sources plug into the module's own scheduler (``Hazards.run``), so
they get the same timing, back-off and status reporting as the rest.
"""

from __future__ import annotations

import time

from meridian.net import NetError
from meridian.registry import HTTPError, Response

from . import cems, ea, glofas
from .common import LAYER_FLOODS, LAYER_MAPPING, LAYER_OUTLOOK, SourceState, fc
from .floods import STATION_TTL, Floods
from .mapping import Mapping
from .outlook import Outlook

HOSTS = {cems.HOST: 1.0, cems.VIEWER_HOST: 0.5, "environment.data.gov.uk": 0.5, glofas.HOST: 0.1}


class Plus:
    def __init__(self, svc):
        self.svc = svc
        self.mapping = Mapping(svc)
        self.floods = Floods(svc)
        self.outlook = Outlook(svc)

    def search(self, q: str, limit: int) -> list[dict]:
        return self.mapping.search(q, limit) + self.floods.search(q, limit)

    def nearby(self, lat: float, lon: float, radius_km: float, limit: int) -> list[dict]:
        return self.mapping.nearby(lat, lon, radius_km, limit) + self.floods.nearby(lat, lon, radius_km, limit)

    def housekeeping(self) -> None:
        self.mapping.housekeeping()
        self.floods.housekeeping()

    def summary(self) -> dict:
        c = self.floods.counts()
        return {"emergency_mapping_open": sum(1 for a in self.mapping.items if not a["closed"]),
                "emergency_mapping_listed": len(self.mapping.items),
                "england_severe_flood_warnings": c["severe"], "england_flood_warnings": c["warning"],
                "england_flood_alerts": c["alert"], "england_gauges": len(self.floods.stations),
                "flood_outlook_date": self.outlook.date()}

    def kick(self, name: str) -> None:
        """Refresh a stale source in the background, but leave a failing one to its back-off."""
        st = self.svc.state[name]
        if st.error and time.time() - st.attempt_at < min(3600.0, 120.0 * 2 ** min(st.failures, 5)):
            return
        self.svc.kick(name)


def register(m, svc) -> Plus:
    plus = Plus(svc)
    svc.extras = plus
    for host, gap in HOSTS.items():
        m.net.set_host_interval(host, gap)
    idle = svc.background
    when_idle = "while in use, every 2 h otherwise" if idle else "while in use"

    # -- sources ----------------------------------------------------------------------------------
    m.source("hazards.cems", "Copernicus EMS rapid mapping activations", "https://mapping.emergency.copernicus.eu/activations/",
             "Copernicus EMS terms: free, full and open access, cite the source", refresh=f"every 30 min {when_idle}",
             layer=LAYER_MAPPING)
    m.source("hazards.ea_floods", "Environment Agency flood warnings (England)", ea.DOCS_URL,
             "Open Government Licence v3 (Environment Agency)",
             refresh="every 15 min while in use, hourly otherwise" if idle else "every 15 min while in use",
             layer=LAYER_FLOODS)
    m.source("hazards.ea_gauges", "Environment Agency river and sea levels (latest readings)", ea.DOCS_URL,
             "Open Government Licence v3 (Environment Agency)", refresh="every 15 min while the layer is on",
             layer=LAYER_FLOODS)
    m.source("hazards.ea_stations", "Environment Agency monitoring stations", ea.DOCS_URL,
             "Open Government Licence v3 (Environment Agency)", refresh="weekly, once the layer is used", layer=LAYER_FLOODS)
    m.source("hazards.glofas", "GloFAS global flood forecast (Copernicus EMS)", glofas.SITE_URL,
             "CEMS-FLOODS licence: free reuse, credit Copernicus Emergency Management Service",
             refresh="checked every 6 h while the layer is on; tiles cached 6 h", layer=LAYER_OUTLOOK)

    # -- layers (browser registers them; these rows feed the layer table) ---------------------------
    m.layer(LAYER_MAPPING, "Emergency mapping", "Events",
            description="Copernicus EMS rapid mapping activations: satellite emergency maps of floods, fires, quakes, "
                        "storms and landslides, with their areas of interest.",
            source="Copernicus EMS", refresh="30 min",
            use="Click an activation for its map products, the matching GDACS alert and the mapped area.",
            count=lambda: sum(1 for a in plus.mapping.items if not a["closed"]))
    m.layer(LAYER_FLOODS, "Flood warnings (England)", "Events",
            description="Environment Agency flood warnings and alerts with their areas, and river and sea levels near them.",
            source="Environment Agency", refresh="15 min",
            use="Click an area for the message and nearby gauges; right-click for flood warnings near a point.",
            count=lambda: sum(1 for w in plus.floods.warnings if w["level"] <= 3))
    m.layer(LAYER_OUTLOOK, "Flood outlook", "Events",
            description="GloFAS global river flood forecast for the next 1 to 15 days (a forecast signal, not a warning).",
            source="Copernicus EMS GloFAS", refresh="daily", count=None)

    # -- scheduling ---------------------------------------------------------------------------------
    svc.state["cems"] = SourceState("hazards.cems", 1800, 7200 if idle else None)
    svc.state["ea_floods"] = SourceState("hazards.ea_floods", 900, 3600 if idle else None)
    svc.state["ea_gauges"] = SourceState("hazards.ea_gauges", 900, None)
    svc.state["ea_stations"] = SourceState("hazards.ea_stations", STATION_TTL, None)
    svc.state["glofas"] = SourceState("hazards.glofas", 6 * 3600, None)
    svc.refreshers.update(cems=plus.mapping.refresh, ea_floods=plus.floods.refresh_warnings,
                          ea_gauges=plus.floods.refresh_readings, ea_stations=plus.floods.refresh_stations,
                          glofas=plus.outlook.refresh)
    svc.layers_for.update(cems=(LAYER_MAPPING,), ea_floods=(LAYER_FLOODS,), ea_gauges=(LAYER_FLOODS,),
                          ea_stations=(LAYER_FLOODS,), glofas=(LAYER_OUTLOOK,))
    plus.mapping.ensure_tables()
    plus.floods.ensure_tables()
    for name, delay in (("cems", 15), ("ea_floods", 8), ("ea_gauges", 12), ("ea_stations", 30), ("glofas", 14)):
        m.task(name, (lambda n=name: svc.tick(n)), every=30, initial_delay=delay)
    m.task("plus_housekeeping", plus.housekeeping, every=6 * 3600, initial_delay=900)

    # -- discovery ----------------------------------------------------------------------------------
    svc.detail_handlers.update(cems=plus.mapping.detail, ea=plus.floods.detail, eagauge=plus.floods.gauge_detail)
    svc.event_hooks.extend([plus.mapping.events, plus.floods.events])
    m.search(lambda q, limit: plus.search(q, limit))
    m.nearby(lambda lat, lon, r, limit: plus.nearby(lat, lon, r, limit))
    m.ai_tool(
        "flood_warnings",
        "Environment Agency flood warnings and alerts in force in England, for an area (place, county or river name, "
        "or a bbox) or around a point (lat, lon, radius_km). Each has the severity in plain words, the official "
        "message and nearby river and sea gauge levels. England only (not Scotland or Wales).",
        {"type": "object", "properties": {
            "area": {"type": "string", "description": "Place, county, river or Environment Agency area, e.g. Somerset "
                                                      "or River Severn (optional)"},
            "bbox": {"type": "string", "description": "west,south,east,north in degrees (optional)"},
            "lat": {"type": "number"}, "lon": {"type": "number"},
            "radius_km": {"type": "number", "description": "Around lat/lon (default 10, max 100)"},
            "min_severity": {"type": "string", "enum": ["severe", "warning", "alert", "ended"],
                             "description": "Lowest level to include (default alert)"},
            "include_gauges": {"type": "boolean", "description": "Add nearby river and sea levels (default true)"},
            "limit": {"type": "integer", "description": "Default 25, max 100"}},
         "additionalProperties": False},
        plus.floods.tool)
    m.ai_tool(
        "emergency_mapping",
        "Copernicus Emergency Management Service rapid mapping activations: satellite emergency maps requested for "
        "floods, wildfires, earthquakes, storms, landslides and other disasters worldwide. Lists open and recent "
        "activations with category, countries, dates, areas of interest, map products (first estimate, "
        "delineation, grading) and the linked GDACS event; or one activation by code.",
        {"type": "object", "properties": {
            "days": {"type": "number", "description": "Closed activations from the past this many days (default 30); "
                                                      "open ones are always listed"},
            "country": {"type": "string", "description": "Country name (optional)"},
            "category": {"type": "string", "description": "Flood, wildfire, earthquake, storm, mass movement, ... (optional)"},
            "open_only": {"type": "boolean", "description": "Only activations still open (default false)"},
            "code": {"type": "string", "description": "One activation, e.g. EMSR936 (optional)"},
            "limit": {"type": "integer", "description": "Default 20, max 60"}},
         "additionalProperties": False},
        plus.mapping.tool)

    _routes(m, svc, plus)
    return plus


def _routes(m, svc, plus: Plus) -> None:
    def version(*names: str):
        return tuple((svc.state[n].version, svc.state[n].failures, svc.state[n].error is None) for n in names)

    @m.route("GET", "/api/hazards/mapping")
    def r_mapping(req):
        days = req.arg("days", 60, type=int, min=1, max=365)
        scope = req.arg("scope", "recent", choices={"recent", "open"})
        svc.touch(LAYER_MAPPING)
        plus.kick("cems")
        return svc.cached_json(f"mapping:{days}:{scope}", (plus.mapping.version, version("cems"), int(time.time() // 300)),
                               lambda: plus.mapping.map_fc(days, scope == "open"))

    @m.route("GET", "/api/hazards/mapping/extent")
    def r_mapping_extent(req):
        eid = req.arg("id")
        code = cems.valid_code(eid.split(":", 1)[1]) if eid.startswith("cems:") else None
        if not code:
            raise HTTPError(400, "BAD_ARG", "id must look like cems:EMSR936.")
        svc.touch(LAYER_MAPPING)
        try:
            return plus.mapping.extent(code)
        except (NetError, ValueError):
            return fc([], note="Copernicus EMS did not answer; try again later.")

    @m.route("GET", "/api/hazards/floods")
    def r_floods(req):
        bbox = req.bbox("bbox", None)
        zoom = req.arg("zoom", 6.0, type=float, min=0.0, max=24.0)
        min_level = req.arg("min", 3, type=int, min=1, max=3)
        ended = req.arg("ended", False, type=bool)
        gauges = req.arg("gauges", True, type=bool)
        svc.touch(LAYER_FLOODS)
        plus.kick("ea_floods")
        if plus.floods.stations or not svc.state["ea_stations"].error:
            plus.kick("ea_gauges")
        if plus.floods.loaded:
            plus.floods.kick_outlines()
        if zoom < 9:
            key = f"floods:{min_level}:{ended}:{gauges}"
            return svc.cached_json(key, (plus.floods.version, version("ea_floods", "ea_gauges", "ea_stations"),
                                         int(time.time() // 300)),
                                   lambda: plus.floods.map_fc(zoom, None, min_level, ended, gauges))
        return plus.floods.map_fc(zoom, bbox, min_level, ended, gauges)

    @m.route("GET", "/api/hazards/floods/here")
    def r_floods_here(req):
        lat, lon = req.latlon()
        return plus.floods.here(lat, lon)

    @m.route("GET", "/api/hazards/floods/gauge-history")
    def r_gauge_history(req):
        gid = req.arg("id")
        hours = req.arg("hours", 48, type=int, min=6, max=120)
        ref = gid.split(":", 1)[1] if gid.startswith("eagauge:") else ""
        if not ea.STATION_ID.fullmatch(ref):
            raise HTTPError(400, "BAD_ARG", "id must look like eagauge:1029TH.")
        try:
            return plus.floods.history(ref, hours)
        except (NetError, ValueError):
            return {"points": [], "note": "The Environment Agency did not answer; try again later."}

    @m.route("GET", "/api/hazards/flood-outlook")
    def r_outlook(req):
        svc.touch(LAYER_OUTLOOK)
        plus.kick("glofas")
        return plus.outlook.meta()

    @m.route("GET", "/api/hazards/flood-outlook/tile/{product}/{z}/{x}/{y}")
    def r_outlook_tile(req):
        product = req.params["product"]
        if product not in glofas.PRODUCTS:
            raise HTTPError(404, "NOT_FOUND", "Unknown flood outlook product.")
        try:
            z = int(req.params["z"])
            x = int(req.params["x"])
            y = int(req.params["y"].removesuffix(".png"))
        except ValueError:
            raise HTTPError(400, "BAD_ARG", "Tile numbers must be whole numbers.") from None
        if not glofas.valid_tile(z, x, y):
            raise HTTPError(400, "BAD_ARG", "Tile out of range.")
        date = req.arg("t", None)
        if date is not None and len(date) > 20:
            raise HTTPError(400, "BAD_ARG", "t must be a date like 2026-11-23.")
        svc.touch(LAYER_OUTLOOK)
        body, ok = plus.outlook.tile(product, z, x, y, date)
        return Response(body, content_type="image/png", cache="max-age=21600" if ok else "no-store")
