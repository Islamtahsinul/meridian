"""Infrastructure and companies: power plants, airports, ports, data centres, dams, submarine cables,
listed companies, a markets board and live electricity grids, all from open data used within each
source's terms."""

from __future__ import annotations

import re

from meridian.registry import HTTPError

from . import airports, cables, companies, countries, dams, datacenters, energy_service, markets, ports, power
from .service import State, safe_id

GEO_FILES = {
    "power": "power.geojson", "power-minor": "power-minor.geojson", "airports-major": "airports-major.geojson", "airports-minor": "airports-minor.geojson",
    "ports": "ports.geojson", "datacenters": "datacenters.geojson", "dams": "dams.geojson",
    "cables": "cables.geojson", "landings": "landings.geojson", "companies": "companies.geojson",
    "energy-countries": "energy-countries.geojson", "energy-regions": "energy-gb-regions.geojson",
}
DATASETS = ("power", "airports", "ports", "wpi", "datacenters", "dams", "cables", "companies", "commodities",
            "energy-shapes")

_state: State | None = None


def state() -> State | None:
    """The loaded state (for tests and other modules); None before register()."""
    return _state


def register(m) -> None:
    global _state
    st = State(m)
    _state = st
    mk = markets.Markets(m, m.data_dir / "commodities.json")
    st.markets = mk
    st.energy = energy_service.register(m, mk)   # live grids: routes /api/infra/energy/..., AI tools, polling

    # -- politeness ---------------------------------------------------------------------------------
    for host, secs in (("query.wikidata.org", 1.0), (cables.HOST, 1.0), (markets.GOLDAPI_HOST, 1.1),
                       (airports.METAR_HOST, 0.7), ("finnhub.io", 1.0), ("api.twelvedata.com", 2.0),
                       ("www.peeringdb.com", 3.0), ("overpass-api.de", 5.0), ("fred.stlouisfed.org", 1.0),
                       ("www.ecb.europa.eu", 1.0)):
        m.net.set_host_interval(host, secs)

    # -- keys -----------------------------------------------------------------------------------------
    m.key("FINNHUB_API_KEY", "Delayed share prices for US-listed companies on company cards and the markets "
          "drawer (free key).", "https://finnhub.io/register")
    m.key("INFRA_WPI_URL", "Download link of the NGA World Port Index CSV, if NGA publishes a new edition.",
          ports.WPI_PAGE, secret=False)
    m.key("TWELVEDATA_API_KEY", "Delayed share prices and fund prices (free key, 800 requests a day).",
          "https://twelvedata.com/pricing")

    # -- sources --------------------------------------------------------------------------------------
    m.source(power.SOURCE_ID, "WRI Global Power Plant Database v1.3.0", power.GPPD_PAGE, "CC BY 4.0, World Resources Institute",
             refresh="checked monthly (static database)", layer="infra.power")
    m.source(airports.SOURCE_ID, "OurAirports airports, runways and frequencies", "https://ourairports.com/data/",
             "Public domain", refresh="weekly", layer="infra.airports")
    m.source(airports.METAR_SOURCE_ID, "Aviation Weather Center METAR reports", "https://aviationweather.gov/data/api/",
             "US Government, public domain", refresh="on demand, cached 5 minutes", layer="infra.airports")
    m.source(ports.SOURCE_WPI, "NGA World Port Index (Pub. 150)", ports.WPI_PAGE, "US Government, public domain",
             refresh="monthly when msi.nga.mil answers", layer="infra.ports", optional=True)
    m.source(ports.SOURCE_LOCODE, "UN/LOCODE port locations", ports.LOCODE_PAGE, "UNECE, free to use with attribution",
             refresh="monthly", layer="infra.ports")
    m.source(ports.SOURCE_NE, "Natural Earth ports", ports.NE_PAGE, "Public domain", refresh="monthly", layer="infra.ports")
    m.source(datacenters.SOURCE_PDB, "PeeringDB facilities", datacenters.PEERINGDB_PAGE,
             "PeeringDB acceptable use policy (open data)", refresh="every 3 days", layer="infra.datacenters")
    m.source(datacenters.SOURCE_OSM, "OpenStreetMap data centres (Overpass API)", "https://www.openstreetmap.org/copyright",
             "ODbL, OpenStreetMap contributors", refresh="every 3 days when Overpass answers", layer="infra.datacenters", optional=True)
    m.source(dams.SOURCE_ID, "Wikidata dams", dams.PAGE, "CC0", refresh="monthly", layer="infra.dams")
    m.source(cables.SOURCE_ID, "TeleGeography Submarine Cable Map", cables.PAGE, cables.LICENCE,
             refresh="every 2 weeks, cable details on demand", layer="infra.cables")
    m.source(companies.SOURCE_ID, "Wikidata listed companies", companies.PAGE, "CC0", refresh="every 2 weeks",
             layer="infra.companies")
    m.source(markets.SRC_GOLD, "gold-api.com metal and crypto prices", "https://gold-api.com/", "Free API, fair use",
             refresh="while the markets drawer is open, cached 90 s")
    m.source(markets.SRC_ECB, "ECB euro foreign exchange reference rates", markets.ECB_PAGE, "ECB, free reuse with source",
             refresh="daily, cached 1 hour")
    m.source(markets.SRC_WB, "World Bank commodity prices (Pink Sheet)", markets.WB_PAGE, "CC BY 4.0, World Bank",
             refresh="monthly averages, checked every 3 days")
    m.source(markets.SRC_FRED, "FRED daily series (oil, gas, US indices)", markets.FRED_PAGE,
             "Public data, some series copyrighted by their owners", refresh="end of day, cached 3 hours")
    m.source(markets.SRC_FINNHUB, "Finnhub share prices", "https://finnhub.io/", "Free tier terms",
             refresh="on demand, cached 60 s", layer="infra.companies", needs_key="FINNHUB_API_KEY")
    m.source(markets.SRC_TWELVE, "Twelve Data share prices", "https://twelvedata.com/", "Free tier terms",
             refresh="on demand, cached 60 s", layer="infra.companies", needs_key="TWELVEDATA_API_KEY")

    # -- datasets -------------------------------------------------------------------------------------
    def guarded(sid: str, coll: str, fn):
        def build(ds):
            try:
                fn(ds)
            except Exception as err:
                if coll and len(st.coll(coll)):
                    m.status(sid, ok=True, note=f"Last refresh failed ({type(err).__name__}); serving the previous copy")
                else:
                    m.status(sid, ok=False, error=str(err)[:300])
                raise
        return build

    m.dataset("power", guarded(power.SOURCE_ID, "power", power.build), max_age_hours=24 * 30,
              description="WRI power plants (35k)")
    m.dataset("airports", guarded(airports.SOURCE_ID, "airports", airports.build), max_age_hours=24 * 7,
              description="OurAirports airports, runways, frequencies")
    m.dataset("ports", guarded(ports.SOURCE_LOCODE, "ports", ports.build), max_age_hours=24 * 30,
              description="Ports (World Port Index or UN/LOCODE with Natural Earth)")
    m.dataset("wpi", guarded(ports.SOURCE_WPI, "", lambda ds: ports.build_wpi(ds, m.config.get("INFRA_WPI_URL",
                                                                                              ports.WPI_CSV))),
              max_age_hours=24 * 30, description="NGA World Port Index (retried every 30 minutes to 6 hours)")
    m.dataset("datacenters", guarded(datacenters.SOURCE_PDB, "datacenters", datacenters.build), max_age_hours=72,
              description="PeeringDB facilities (+ OpenStreetMap)")
    m.dataset("dams", guarded(dams.SOURCE_ID, "dams", dams.build), max_age_hours=24 * 30,
              description="Wikidata notable dams")
    m.dataset("cables", guarded(cables.SOURCE_ID, "cables", cables.build), max_age_hours=24 * 14,
              description="TeleGeography cables, landing points, country links")
    m.dataset("companies", guarded(companies.SOURCE_ID, "companies",
                                   lambda ds: companies.build(ds, eur_rates=mk.eur_rates())),
              max_age_hours=24 * 14, description="Wikidata listed companies with HQ coordinates", after=("dams",))
    m.dataset("commodities", guarded(markets.SRC_WB, "", markets.build_commodities), max_age_hours=72,
              description="World Bank monthly commodity prices")

    m.on_dataset("power", st.load_power)
    m.on_dataset("airports", st.load_airports)
    m.on_dataset("ports", st.load_ports)
    m.on_dataset("wpi", st.load_ports)
    m.on_dataset("datacenters", st.load_datacenters)
    m.on_dataset("dams", st.load_dams)
    m.on_dataset("cables", st.load_cables)
    m.on_dataset("companies", st.load_companies)
    m.on_dataset("commodities", lambda: m.status(markets.SRC_WB, ok=True,
                                                 note=f"Updated {mk.wb_data().get('updated') or 'unknown'}"))

    # -- layer table ----------------------------------------------------------------------------------
    m.layer("infra.power", "Power plants", "Infrastructure",
            description="About 35,000 power stations sized by capacity and coloured by primary fuel.",
            source="WRI Global Power Plant Database v1.3.0 (CC BY 4.0)", refresh="static database (2021), checked monthly",
            use="Energy mix, grid exposure near an event, which plants feed a region", count=lambda: len(st.coll("power")))
    m.layer("infra.airports", "Airports", "Infrastructure",
            description="Large, medium and small airports with runways, frequencies and live METAR weather.",
            source="OurAirports (public domain), aviationweather.gov METAR", refresh="weekly; weather on demand",
            use="Airport details, local weather, live arrivals and departures", count=lambda: len(st.coll("airports")))
    m.layer("infra.ports", "Ports", "Infrastructure", description="Sea and river ports with size and live vessel activity.",
            source="UN/LOCODE, Natural Earth; NGA World Port Index when reachable", refresh="monthly",
            use="Shipping hubs, port congestion with the ships layer", count=lambda: len(st.coll("ports")))
    m.layer("infra.datacenters", "Data centres", "Infrastructure",
            description="Colocation and interconnection facilities sized by the networks present.",
            source="PeeringDB; OpenStreetMap contributors (ODbL)", refresh="every 3 days",
            use="Internet infrastructure, cloud and carrier hubs", count=lambda: len(st.coll("datacenters")))
    m.layer("infra.dams", "Dams", "Infrastructure", description="Notable dams with height, reservoir and power data.",
            source="Wikidata (CC0)", refresh="monthly", use="Flood risk, water security, hydropower",
            count=lambda: len(st.coll("dams")))
    m.layer("infra.cables", "Submarine cables", "Infrastructure",
            description="Undersea internet cables and their landing points, with each country's cable dependency.",
            source="TeleGeography Submarine Cable Map (CC BY-NC-SA 3.0)", refresh="every 2 weeks",
            use="Internet resilience, cable cuts, which countries depend on few cables",
            count=lambda: len(st.coll("cables")))
    m.layer("infra.companies", "Listed companies", "Companies",
            description="Stock-market listed companies at their headquarters, sized by employees or market value.",
            source="Wikidata (CC0); share prices with a Finnhub or Twelve Data key", refresh="every 2 weeks",
            use="Corporate presence, exposure of listed firms to an area", count=lambda: len(st.coll("companies")))

    # -- routes -----------------------------------------------------------------------------------------
    def need(rec, what: str, coll: str | None = None):
        if rec is None:
            if coll and not len(st.coll(coll)):
                raise HTTPError(503, "BUILDING", f"The {what} list is still loading. Try again in a minute.")
            raise HTTPError(404, "NOT_FOUND", f"No such {what}.")
        return rec

    def ident(req, name: str = "rid") -> str:
        v = safe_id(req.params.get(name, ""))
        if not v:
            raise HTTPError(400, "BAD_ARG", "That id is not valid.")
        return v

    @m.route("GET", "/api/infra/status")
    def r_status(req):
        info = {d["key"].split(".", 1)[1]: d for d in m.app.datasets.info() if d["key"].startswith("infra.")}
        out = {}
        for name in DATASETS:
            d = info.get(name) or {}
            out[name] = {"ready": bool(d.get("ready")), "building": bool(d.get("building")),
                         "error": d.get("last_error"), "built_at": d.get("built_at"), "rows": d.get("rows"),
                         "notes": d.get("notes") or []}
        return {"datasets": out, "counts": st.counts(), "keys": mk.keys()}

    @m.route("GET", "/api/infra/geo/{name}")
    def r_geo(req):
        fname = GEO_FILES.get(req.params["name"])
        if not fname:
            raise HTTPError(404, "NOT_FOUND", "No such map file.")
        return m.serve_file(m.data_dir / fname, "application/geo+json; charset=utf-8")

    @m.route("GET", "/api/infra/power/{rid}")
    def r_power(req):
        return need(st.power_detail(ident(req)), "power plant", "power")

    @m.route("GET", "/api/infra/airport/{rid}")
    def r_airport(req):
        return need(st.airport_detail(ident(req)), "airport", "airports")

    @m.route("GET", "/api/infra/metar/{rid}")
    def r_metar(req):
        rec = need(st.airport_detail(ident(req)), "airport", "airports")
        try:
            out = airports.fetch_metar(m.net, rec)
            m.status(airports.METAR_SOURCE_ID, ok=True)
        except Exception as err:
            m.status(airports.METAR_SOURCE_ID, ok=False, error=str(err))
            raise HTTPError(502, "UPSTREAM", "aviationweather.gov is not answering right now.") from None
        return out

    @m.route("GET", "/api/infra/port/{rid}")
    def r_port(req):
        return need(st.port_detail(ident(req)), "port", "ports")

    @m.route("GET", "/api/infra/datacenter/{rid}")
    def r_dc(req):
        return need(st.datacenter_detail(ident(req)), "data centre", "datacenters")

    @m.route("GET", "/api/infra/dam/{rid}")
    def r_dam(req):
        return need(st.dam_detail(ident(req)), "dam", "dams")

    @m.route("GET", "/api/infra/cable/{rid}")
    def r_cable(req):
        return need(st.cable_detail(ident(req), fetch=req.arg("details", True, type=bool)), "cable", "cables")

    @m.route("GET", "/api/infra/landing/{rid}")
    def r_landing(req):
        return need(st.landing_detail(ident(req)), "landing point", "landings")

    @m.route("GET", "/api/infra/company/{rid}")
    def r_company(req):
        return need(st.company_detail(ident(req)), "company", "companies")

    @m.route("GET", "/api/infra/country/{iso2}")
    def r_country(req):
        cc = req.params["iso2"].strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", cc):
            raise HTTPError(400, "BAD_ARG", "Use a two-letter ISO country code.")
        if not countries.name(cc):
            raise HTTPError(404, "NOT_FOUND", "Unknown country code.")
        return st.country(cc)

    @m.route("GET", "/api/infra/countries")
    def r_countries(req):
        return {"countries": st.countries_table()}

    @m.route("GET", "/api/infra/quote")
    def r_quote(req):
        sym = req.arg("symbol").strip()
        if not markets.SYMBOL_RE.fullmatch(sym):
            raise HTTPError(400, "BAD_ARG", "Use a symbol such as XAU, EURUSD, wb:BRENT or AAPL:XNAS.")
        return mk.quote(sym)

    @m.route("GET", "/api/infra/markets")
    def r_markets(req):
        return mk.board()

    # -- discovery ------------------------------------------------------------------------------------
    m.search(lambda q, limit: st.search(q, limit))
    m.nearby(lambda lat, lon, radius_km, limit: st.nearby(lat, lon, radius_km, limit))

    m.ai_tool("infra_near", "Infrastructure near a point: airports, ports, power plants, data centres, dams, cable "
              "landing points and listed company headquarters, nearest first.",
              {"type": "object", "properties": {
                  "lat": {"type": "number"}, "lon": {"type": "number"},
                  "radius_km": {"type": "number", "description": "1 to 500, default 50"},
                  "kinds": {"type": "array", "items": {"type": "string", "enum": sorted(
                      ["airport", "port", "power_plant", "datacenter", "dam", "landing_point", "company"])}}},
               "required": ["lat", "lon"], "additionalProperties": False}, st.ai_near)
    m.ai_tool("airport_info", "Airport details by ICAO, IATA or OurAirports code (or name): runways, frequencies, "
              "latest METAR weather and a LiveATC search link.",
              {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"],
               "additionalProperties": False}, st.ai_airport)
    m.ai_tool("cable_info", "Submarine cable details by name or id: owners, length, ready-for-service year, landing "
              "points and the cable count of each country it lands in.",
              {"type": "object", "properties": {"query": {"type": "string", "description": "Cable name or id"}},
               "required": ["query"], "additionalProperties": False}, st.ai_cable)
    m.ai_tool("company_lookup", "Listed company facts by name or ticker: headquarters, listings, industry, "
              "employees, revenue, market value and a delayed share price when a quote key is configured.",
              {"type": "object", "properties": {"query": {"type": "string", "description": "Company name or ticker"}},
               "required": ["query"], "additionalProperties": False}, st.ai_company)
    m.ai_tool("market_quote", "Price for a market symbol: XAU, XAG, XPT, XPD, HG, BTC, ETH (live), currency pairs "
              "like EURUSD (ECB daily), wb:BRENT and other World Bank monthly series, fred:SP500 (end of day) or a "
              "share as TICKER:MIC (needs a quote key).",
              {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"],
               "additionalProperties": False}, st.ai_quote)
