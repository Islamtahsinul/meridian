"""Conflict and intelligence: GDELT conflict events, protests and news, front lines, countries, travel advice,
risk zones, convergences, cyber (with Cloudflare Radar), news spikes.

Backend of the ``intel`` module. Layers (browser ids): intel.conflict, intel.protests, intel.frontlines, intel.ucdp
(group Conflict) and intel.news, intel.countries, intel.advice, intel.risk, intel.convergences, intel.cyber (group
Intelligence). Sources, formulas and endpoints are documented in docs/modules/intel.md.
"""

from __future__ import annotations

import time

from . import advice, api, api_plus, builds, countries, cyber, db, frontlines, news, radar, ucdp
from .service import (Intel, SRC_CF, SRC_CF_BGP, SRC_DEEPSTATE, SRC_DERIVED, SRC_DOC, SRC_FCDO, SRC_FEODO, SRC_GDELT,
                      SRC_IODA, SRC_NE, SRC_SEED, SRC_UCDP, SRC_US, SRC_WB)


def register(m) -> None:
    db.create(m.store)
    svc = Intel(m)
    m.intel = svc  # handy for tests and debugging

    m.net.set_host_interval(news.DOC_HOST, news.DOC_INTERVAL_S)
    m.net.set_host_interval("api.worldbank.org", 1.0)
    m.net.set_host_interval("api.ioda.inetintel.cc.gatech.edu", 2.0)
    m.net.set_host_interval(advice.FCDO_HOST, 1.0)
    m.net.set_host_interval(advice.US_HOST, 2.0)
    m.net.set_host_interval("api.cloudflare.com", 1.0)

    # -- settings and optional keys ------------------------------------------------------------------
    m.key("INTEL_BACKFILL_HOURS", "Hours of GDELT history fetched at first start (1-72, default 48).",
          "https://www.gdeltproject.org/data.html", secret=False)
    m.key("UCDP_ACCESS_TOKEN", "UCDP API token, only used if the public GED download stops working.",
          "https://ucdp.uu.se/apidocs/")
    m.key("CLOUDFLARE_RADAR_TOKEN", "Cloudflare Radar API token: adds verified traffic anomalies, outage reports, "
          "BGP hijacks and route leaks to the cyber layer and country cards.",
          "https://developers.cloudflare.com/radar/get-started/first-request/")
    m.key("INTEL_SEED_DAYS", "Days of GDELT history fetched once for the news-spike baseline (0-30, default 16, "
          "about 6 MB a day).", "https://www.gdeltproject.org/data.html", secret=False)
    m.key("INTEL_SEED_MAX_MB", "Download limit of that one-off history seed in MB (default 150).",
          "https://www.gdeltproject.org/data.html", secret=False)

    # -- sources ----------------------------------------------------------------------------------------
    m.source(SRC_GDELT, "GDELT 2.0 Events (15-minute exports)", "https://www.gdeltproject.org/",
             "Open data, free use with attribution to the GDELT Project", refresh="every 15 min (continuous)",
             layer="intel.conflict")
    m.source(SRC_DOC, "GDELT DOC 2.0 API (theatre headlines)", news.DOC_SITE,
             "Open data, attribution to the GDELT Project; max 1 request per 5 s", refresh="20 min per theatre while in use",
             layer="intel.news")
    m.source(SRC_DEEPSTATE, "DeepStateMap (Ukraine front line)", frontlines.DEEPSTATE_SITE,
             "Public map data, credited to DeepStateMap; personal, non-commercial use", refresh="every 3 h",
             layer="intel.frontlines")
    m.source(SRC_NE, "Natural Earth admin-0 countries 1:50m", countries.NE_SITE, "Public domain", refresh="monthly",
             layer="intel.countries")
    m.source(SRC_WB, "World Bank WDI and Worldwide Governance Indicators", countries.WB_SITE, "CC BY 4.0",
             refresh="weekly", layer="intel.countries")
    m.source(SRC_FEODO, "abuse.ch Feodo Tracker botnet C2 blocklist", cyber.FEODO_SITE,
             "abuse.ch terms of use (free for commercial and non-commercial use)", refresh="every 6 h", layer="intel.cyber")
    m.source(SRC_IODA, "IODA internet outage signals (Georgia Tech)", cyber.IODA_SITE,
             "Free for research and non-commercial use, with attribution", refresh="30 min while in use", layer="intel.cyber")
    m.source(SRC_CF, "Cloudflare Radar traffic anomalies and outage reports", radar.OUTAGE_CENTER,
             "Cloudflare Radar, CC BY-NC 4.0", refresh="30 min while in use, 2 h otherwise", layer="intel.cyber",
             needs_key="CLOUDFLARE_RADAR_TOKEN")
    m.source(SRC_CF_BGP, "Cloudflare Radar BGP hijacks and route leaks", radar.ROUTING_SITE,
             "Cloudflare Radar, CC BY-NC 4.0", refresh="30 min while in use, 2 h otherwise", layer="intel.cyber",
             needs_key="CLOUDFLARE_RADAR_TOKEN")
    m.source(SRC_FCDO, "UK FCDO foreign travel advice (GOV.UK content API)", advice.FCDO_SITE,
             "Open Government Licence v3.0 (GOV.UK)", refresh="index every 3 h, changed pages only", layer="intel.advice")
    m.source(SRC_US, "US State Department travel advisories", advice.US_SITE,
             "US Government work, public domain", refresh="every 6 h", layer="intel.advice")
    m.source(SRC_SEED, "GDELT history seed for the news-spike baseline (one-off)", "https://www.gdeltproject.org/",
             "Open data, credit the GDELT Project", refresh="once, bounded", layer="intel.news", optional=True)
    m.source(SRC_UCDP, "UCDP Georeferenced Event Dataset (GED)", ucdp.SITE, "CC BY 4.0, cite UCDP",
             refresh="monthly", layer="intel.ucdp")
    m.source(SRC_DERIVED, "Risk zones, convergences and instability index (computed here)", "",
             "Derived from the sources above", refresh="every 10 min", layer="intel.risk")

    # -- layer table -----------------------------------------------------------------------------------
    m.layer("intel.conflict", "Conflict events", "Conflict",
            description="Assaults, fighting and attacks machine-coded by GDELT from news reports, deduplicated, last 7 days.",
            source="GDELT 2.0 Events", refresh="every 15 min",
            use="Where violence is being reported right now; not verified on the ground.",
            count=lambda: db.count_events(m.store, time.time() - 7 * 86400))
    m.layer("intel.protests", "Protests", "Conflict",
            description="Demonstrations, strikes, blockades and riots machine-coded by GDELT from news reports, "
                        "deduplicated, with 2 or more sources or 3 or more articles.",
            source="GDELT 2.0 Events (CAMEO root 14)", refresh="every 15 min",
            use="Where unrest is being reported; not verified on the ground.",
            count=lambda: db.count_events(m.store, time.time() - 3 * 86400, table="intel_protests"))
    m.layer("intel.frontlines", "Front lines", "Conflict",
            description="Ukraine: occupied and contested areas and the derived front line.",
            source="DeepStateMap", refresh="every 3 h", use="Territorial control in the Russia-Ukraine war.",
            count=lambda: (svc.frontline_meta or {}).get("front_pieces"))
    m.layer("intel.ucdp", "Historical conflicts", "Conflict",
            description="Organised violence since 1989 (UCDP GED), deaths per 0.5° cell and year.",
            source="UCDP GED (CC BY 4.0)", refresh="monthly", use="Long-run context: where wars have been fought.",
            count=lambda: len((svc.ucdp or {}).get("cells") or []))
    m.layer("intel.news", "News", "Intelligence",
            description="Geolocated news reports (latest per place) and feeds for eight monitored theatres.",
            source="GDELT 2.0 Events and DOC API", refresh="every 15 min", use="What is being reported where.",
            count=lambda: int(m.store.scalar("SELECT COUNT(*) FROM intel_news WHERE t >= ? AND titled = 1",
                                             (time.time() - 86400,), 0)))
    m.layer("intel.countries", "Country instability", "Intelligence",
            description="Instability index 0-100 per country (governance, conflict, news tone, hazards); click for a profile.",
            source="World Bank, Natural Earth, GDELT", refresh="every 10 min", use="Comparing countries at a glance.",
            count=lambda: sum(1 for v in svc.index.values() if v.get("index") is not None))
    m.layer("intel.advice", "Travel advice", "Intelligence",
            description="Official travel advice levels per country from the UK FCDO and the US State Department, on a "
                        "common 1-4 scale with the original wording.",
            source="FCDO (OGL v3.0), US State Department (public domain)", refresh="every 3 to 6 h",
            use="Where governments advise against travel, and when that changes.",
            count=lambda: len(svc.advice.fcdo) or None)
    m.layer("intel.risk", "Risk zones", "Intelligence",
            description="Areas where conflict events cluster over 7 days (density x severity).",
            source="Derived from GDELT", refresh="every 10 min", use="Spotting hot spots and their extent.",
            count=lambda: len(svc.zones))
    m.layer("intel.convergences", "Convergences", "Intelligence",
            description="Places where 3 or more kinds of event from all layers coincide within 24 hours.",
            source="Derived from every module's events", refresh="every 10 min", use="Unusual coincidences worth a look.",
            count=lambda: len(svc.convergences))
    m.layer("intel.cyber", "Cyber", "Intelligence",
            description="Botnet command servers by hosting country, internet outage signals and traffic anomalies, "
                        "possible BGP hijacks and route leaks.",
            source="abuse.ch Feodo Tracker, IODA, Cloudflare Radar (token)", refresh="6 h and 30 min",
            use="Where malicious infrastructure is hosted; connectivity and routing disruptions.",
            count=lambda: svc.feodo_meta.get("count"))

    # -- data, background work, routes, providers ------------------------------------------------------
    builds.register_datasets(m, svc)
    m.thread("gdelt", svc.gdelt_loop)
    m.thread("derive", svc.derive_loop)
    m.thread("doc", svc.doc_loop)
    m.task("outages", svc.poll_outages, every=1800, initial_delay=30, when=lambda: m.active(within=1800))
    m.task("prune", lambda: db.prune(m.store), every=3600, initial_delay=900)
    m.task("radar", svc.poll_radar, every=300, initial_delay=40)          # polls when due (30 min in use, 2 h idle)
    svc.advice.load()
    m.thread("advice", svc.advice.loop)
    m.thread("seed", svc.seed.run)
    api.register_routes(m, svc)
    api.register_providers(m, svc)
    api_plus.register_routes(m, svc)
    api_plus.register_providers(m, svc)

