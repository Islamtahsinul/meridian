# Test fixtures: sources, licences and attribution

These files let the tests run offline. They are not covered by the MIT licence of Meridian's own code
unless the table says so. Each keeps the licence of its source, and the attribution column gives the
credit that source asks for.

- Files marked *Synthetic* or *(shape)* were made up for the tests. They copy only the structure of the
  source's responses (field names, and the URL host patterns the parsers check). Names, IDs, numbers, text
  and links are invented.
- The other files are small samples of public responses, usually trimmed or edited for the tests.
- **Share-alike and ODbL files stay under their own licence, not MIT**: CC BY-SA 2.0
  (`cameras/illinois.json`, `cameras/lakecounty.json`), CC BY-SA 4.0 (`cameras/eu_malaga.kml`,
  `cameras/eu_meteogalicia.json`, `cameras/eu_slovenia.json`, `flights/vatspy_firs.geojson`) and ODbL 1.0
  (`ai/nominatim_*.json`, `flights/adsblol_trace.json`, `infra/overpass_sample.json`). Anyone who reuses them
  must follow those terms.
- Open Government Licence v3.0 files (`hazards/ea_*`, `intel/fcdo_*`, `cameras/eu_durham_*`,
  `cameras/eu_netravel_*`, `sanctions/uk_sample.xml`): Contains public sector information licensed under the Open Government Licence v3.0.
- Samples from non-commercial sources (adsb.fi, OpenSky, 511 Alberta) are here only so the tests can run.
  Do not reuse them commercially.

The sources the app uses at runtime, with their terms, are listed in [docs/SOURCES.md](../../docs/SOURCES.md).

## ai

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `ai/anthropic_final.json` | Anthropic Messages API (shape) | Hand-written test responses |  |
| `ai/anthropic_tool_use.json` | Anthropic Messages API (shape) | Hand-written test responses |  |
| `ai/nominatim_reverse.json` | OpenStreetMap Nominatim | ODbL 1.0 (not MIT) | (c) OpenStreetMap contributors, ODbL 1.0, https://www.openstreetmap.org/copyright |
| `ai/nominatim_search.json` | OpenStreetMap Nominatim | ODbL 1.0 (not MIT) | (c) OpenStreetMap contributors, ODbL 1.0, https://www.openstreetmap.org/copyright |
| `ai/ollama_chat_final.json` | Ollama chat API (shape) | Hand-written test responses |  |
| `ai/ollama_chat_tool.json` | Ollama chat API (shape) | Hand-written test responses |  |
| `ai/sdn.json` | OFAC SDN list (shape) | US Government work, public domain; test entries |  |

## alerts

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `alerts/logic_test.mjs` | Meridian test code or hand-written test data | MIT (this repository) |  |

## briefing

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `briefing/countries.geojson` | Natural Earth (simplified shapes) | Public domain | Made with Natural Earth. |

## cameras

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `cameras/alaska.json` | Alaska DOT&PF road weather (shape) | Synthetic: invented names, IDs and file names |  |
| `cameras/alertca.json` | ALERTCalifornia (shape) | Synthetic: invented locations and camera IDs |  |
| `cameras/ashcam.json` | USGS volcano webcams | Public domain (USGS) | Credit: USGS |
| `cameras/austin.json` | City of Austin open data | Public domain |  |
| `cameras/calgary.json` | City of Calgary open data | Open Government Licence - City of Calgary | Contains information licensed under the Open Government Licence - City of Calgary. |
| `cameras/caltrans_d07.json` | Caltrans CWWP2 (shape) | Synthetic: invented names, IDs and paths |  |
| `cameras/dgt.xml` | DGT Spain | DGT open data: free reuse with attribution | Source: Direccion General de Trafico (DGT). |
| `cameras/drivebc.json` | DriveBC | Open Government Licence - British Columbia | Contains information licensed under the Open Government Licence - British Columbia. |
| `cameras/eu_a22.json` | Autostrada del Brennero via Open Data Hub | CC0 (record) | Credit: A22 Autostrada del Brennero |
| `cameras/eu_basque_p1.json` | Open Data Euskadi | CC BY 4.0 | Source: Open Data Euskadi (Basque Government), CC BY 4.0. |
| `cameras/eu_basque_p2.json` | Open Data Euskadi | CC BY 4.0 | Source: Open Data Euskadi (Basque Government), CC BY 4.0. |
| `cameras/eu_catalonia_fgc.json` | FGC (Catalonia) | CC BY 4.0 | Source: Ferrocarrils de la Generalitat de Catalunya, CC BY 4.0. |
| `cameras/eu_catalonia_sct.xml` | Servei Catala de Transit | Llicencia oberta d'us d'informacio - Catalunya | Source: Servei Catala de Transit. |
| `cameras/eu_croatia.xml` | HAC Croatia (national access point) | Croatian NAP licence, free for registered users | Source: Hrvatske autoceste (HAC). |
| `cameras/eu_durham_traffic.json` | Durham County Council | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `cameras/eu_durham_weather.json` | Durham County Council | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `cameras/eu_estonia.json` | Transpordiamet | CC BY 4.0 | Source: Transpordiamet, CC BY 4.0. |
| `cameras/eu_geneva.json` | SITG Geneva (opendata.swiss) | Open use (opendata.swiss terms_open) | Source: SITG Geneve. |
| `cameras/eu_hungary.json` | Utinform (shape) | Synthetic: invented places, roads, IDs and descriptions |  |
| `cameras/eu_loireatlantique.json` | Loire-Atlantique open data | Licence Ouverte v2.0 (Etalab) | Source: Departement de Loire-Atlantique, Licence Ouverte v2.0. |
| `cameras/eu_luxembourg.kml` | CITA Luxembourg (data.public.lu) | CC0 1.0 |  |
| `cameras/eu_luxembourg_page.html` | CITA Luxembourg (data.public.lu) | CC0 1.0 |  |
| `cameras/eu_lyon.json` | Metropole de Lyon (Criter) | Licence Ouverte v2.0 (Etalab) | Source: Metropole de Lyon, Licence Ouverte v2.0. |
| `cameras/eu_madrid.kml` | Ayuntamiento de Madrid open data | CC BY 4.0 | Source: Ayuntamiento de Madrid, CC BY 4.0. |
| `cameras/eu_malaga.kml` | Ayuntamiento de Malaga open data | CC BY-SA 4.0 (share-alike; not MIT) | Source: Ayuntamiento de Malaga, CC BY-SA 4.0. This file stays under CC BY-SA 4.0. |
| `cameras/eu_meteogalicia.json` | MeteoGalicia (Xunta de Galicia) | CC BY-SA 4.0 (share-alike; not MIT) | Source: MeteoGalicia, Xunta de Galicia, CC BY-SA 4.0. This file stays under CC BY-SA 4.0. |
| `cameras/eu_meteoswiss.json` | MeteoSwiss Open Data | CC BY 4.0 | Source: MeteoSwiss. |
| `cameras/eu_netravel_dynamic.json` | North East England UTMC (netraveldata.co.uk) | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `cameras/eu_netravel_static.json` | North East England UTMC (netraveldata.co.uk) | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `cameras/eu_norway_cctv.json` | Statens vegvesen | NLOD 2.0 | Contains data under the Norwegian licence for Open Government data (NLOD) made available by Statens vegvesen. |
| `cameras/eu_norway_status.json` | Statens vegvesen | NLOD 2.0 | Contains data under the Norwegian licence for Open Government data (NLOD) made available by Statens vegvesen. |
| `cameras/eu_slovenia.json` | NAP Slovenia | CC BY-SA 4.0 (share-alike; not MIT) | Source: NAP Slovenia, CC BY-SA 4.0. This file stays under CC BY-SA 4.0. |
| `cameras/eu_sweden.json` | Trafikverket | CC0 1.0 |  |
| `cameras/fi_detail.json` | Fintraffic Digitraffic | CC BY 4.0 | Source: Fintraffic / digitraffic.fi, CC BY 4.0. |
| `cameras/fi_stations.json` | Fintraffic Digitraffic | CC BY 4.0 | Source: Fintraffic / digitraffic.fi, CC BY 4.0. |
| `cameras/hk.xml` | DATA.GOV.HK (Transport Department) | DATA.GOV.HK terms (free use with attribution) | Source: Transport Department, HKSAR Government, via DATA.GOV.HK. |
| `cameras/iceland.json` | Vegagerdin (shape) | Synthetic: invented names and links |  |
| `cameras/illinois.json` | Illinois DOT Gateway | CC BY-SA 2.0 (share-alike; not MIT) | Source: Illinois DOT Gateway, CC BY-SA 2.0. This file stays under CC BY-SA 2.0. |
| `cameras/iowa.json` | Iowa DOT | CC BY 4.0 | Source: Iowa DOT, CC BY 4.0. |
| `cameras/lakecounty.json` | Illinois DOT Gateway | CC BY-SA 2.0 (share-alike; not MIT) | Source: Illinois DOT Gateway, CC BY-SA 2.0. This file stays under CC BY-SA 2.0. |
| `cameras/na_alberta.json` | 511 Alberta | 511 Alberta terms: personal and public non-commercial reproduction | Source: 511 Alberta. |
| `cameras/na_brla.json` | Open Data BR (Baton Rouge) | Public domain |  |
| `cameras/na_deldot.json` | DelDOT (shape) | Synthetic: invented titles, IDs and stream names |  |
| `cameras/na_ibi.json` | 511 developer API (IBI platform) | 511 developer API terms | Source: the 511 service named in the test. |
| `cameras/na_iteris_mt.json` | Montana DOT / Iteris (shape) | Synthetic: invented names, IDs and image paths |  |
| `cameras/na_iteris_sd.json` | SDDOT / Iteris (shape) | Synthetic: invented names and image paths |  |
| `cameras/na_kingcounty.json` | King County GIS | King County GIS data terms | Source: King County. |
| `cameras/na_kirkland.json` | City of Kirkland GIS (shape) | Synthetic: invented locations, IDs and file names |  |
| `cameras/na_ohgo.json` | OHGO (Ohio DOT) | OHGO public API terms | Source: OHGO, Ohio Department of Transportation. |
| `cameras/na_ottawa.json` | City of Ottawa open data | City of Ottawa open data terms | Contains information licensed under the Open Government Licence - City of Ottawa. |
| `cameras/na_toronto.geojson` | City of Toronto open data | Open Government Licence - Toronto | Contains information licensed under the Open Government Licence - Toronto. |
| `cameras/na_toronto_package.json` | City of Toronto open data | Open Government Licence - Toronto | Contains information licensed under the Open Government Licence - Toronto. |
| `cameras/na_usgs.json` | USGS | Public domain | Credit: USGS |
| `cameras/na_vancouver_list.json` | City of Vancouver open data | Open Government Licence - Vancouver | Contains information licensed under the Open Government Licence - Vancouver. |
| `cameras/na_vancouver_page.html` | City of Vancouver camera page (shape) | Hand-written stub with only the elements the parser reads |  |
| `cameras/na_wsdot.json` | WSDOT Traveler Information API | WSDOT API terms | Source: Washington State DOT. |
| `cameras/ndbc_buoycams.json` | NOAA NDBC | Public domain | Credit: NOAA NDBC |
| `cameras/nl.json` | Newfoundland and Labrador highway cameras | Open Government Licence - Newfoundland and Labrador | Contains information licensed under the Open Government Licence - Newfoundland and Labrador. |
| `cameras/nps_webcams.json` | US National Park Service | Public domain | Credit: NPS |
| `cameras/nz.xml` | NZTA Waka Kotahi | CC BY 4.0 | Source: NZ Transport Agency Waka Kotahi, CC BY 4.0. |
| `cameras/singapore.json` | Singapore Open Data (LTA) | Singapore Open Data Licence v1.0 | Contains information from LTA accessed via data.gov.sg under the Singapore Open Data Licence v1.0. |
| `cameras/tfl.json` | Transport for London | TfL open data terms (based on OGL v2.0) | Powered by TfL Open Data. |
| `cameras/windy_countries.json` | Windy Webcams API | Country codes and names only (facts) | Webcams provided by Windy.com |
| `cameras/windy_direct.json` | Windy Webcams API (shape) | Synthetic: invented titles, IDs, counts and provider links |  |
| `cameras/windy_page.json` | Windy Webcams API (shape) | Synthetic: invented titles, IDs, counts and provider links |  |
| `cameras/world_debris.json` | Taiwan government open data | Open Government Data License 1.0 (Taiwan) | Source: the Taiwan agency named in the test. |
| `cameras/world_dgac.html` | DGAC Chile (shape) | Synthetic: invented names and file names |  |
| `cameras/world_dgac_camera.html` | DGAC Chile (shape) | Synthetic: invented names and file names |  |
| `cameras/world_geonet.geojson` | GeoNet (Earth Sciences New Zealand) | CC BY 3.0 NZ | Source: GeoNet, CC BY 3.0 NZ. |
| `cameras/world_hko.js` | Hong Kong Observatory (shape) | Synthetic: invented names and positions |  |
| `cameras/world_jma.geojson` | Japan Meteorological Agency | Public Data License 1.0 (Japan) | Source: Japan Meteorological Agency. |
| `cameras/world_jma_page.html` | Japan Meteorological Agency | Public Data License 1.0 (Japan) | Source: Japan Meteorological Agency. |
| `cameras/world_jpriver_1301.json` | MLIT river cameras (Japan) | Government Standard Terms of Use 2.0 (Japan) | Source: Ministry of Land, Infrastructure, Transport and Tourism (MLIT). |
| `cameras/world_jpriver_prefs.json` | MLIT river cameras (Japan) | Government Standard Terms of Use 2.0 (Japan) | Source: Ministry of Land, Infrastructure, Transport and Tourism (MLIT). |
| `cameras/world_jpriver_record.json` | MLIT river cameras (Japan) | Government Standard Terms of Use 2.0 (Japan) | Source: Ministry of Land, Infrastructure, Transport and Tourism (MLIT). |
| `cameras/world_nsw.json` | Transport for NSW Open Data | CC BY 4.0 | Source: Transport for NSW, CC BY 4.0. |
| `cameras/world_thb.xml` | Taiwan government open data | Open Government Data License 1.0 (Taiwan) | Source: the Taiwan agency named in the test. |
| `cameras/world_wra.json` | Taiwan government open data | Open Government Data License 1.0 (Taiwan) | Source: the Taiwan agency named in the test. |
| `cameras/york.json` | York Region open data | York Region Open Data Licence | Contains information licensed under the York Region Open Data Licence. |

## flights

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `flights/adsbdb_both.json` | adsbdb | Free public API | Source: adsbdb.com |
| `flights/adsbdb_unknown.json` | adsbdb | Free public API | Source: adsbdb.com |
| `flights/adsbfi_empty.json` | adsb.fi open data | Personal, non-commercial use | Data: adsb.fi (https://adsb.fi) |
| `flights/adsbfi_sqk7700.json` | adsb.fi open data | Personal, non-commercial use | Data: adsb.fi (https://adsb.fi) |
| `flights/adsbfi_v2_point.json` | adsb.fi open data | Personal, non-commercial use | Data: adsb.fi (https://adsb.fi) |
| `flights/adsbfi_v3_point.json` | adsb.fi open data | Personal, non-commercial use | Data: adsb.fi (https://adsb.fi) |
| `flights/adsblol_trace.json` | adsb.lol | ODbL 1.0 (not MIT) | Data: adsb.lol, ODbL 1.0. |
| `flights/easa_czib.csv` | EASA Conflict Zone Information Bulletins | Reproduction authorised with acknowledgement | Source: European Union Aviation Safety Agency (EASA). |
| `flights/easa_czib_feed.xml` | EASA Conflict Zone Information Bulletins | Reproduction authorised with acknowledgement | Source: European Union Aviation Safety Agency (EASA). |
| `flights/faa_airport_events.json` | FAA | US Government work, public domain |  |
| `flights/ne_countries.geojson` | Natural Earth (simplified shapes) | Public domain | Made with Natural Earth. |
| `flights/opensky_states.json` | OpenSky Network | OpenSky terms of use, non-commercial | Data: The OpenSky Network, https://opensky-network.org |
| `flights/tfr_detail_6_6924.xml` | FAA | US Government work, public domain |  |
| `flights/tfr_detail_6_9335.xml` | FAA | US Government work, public domain |  |
| `flights/tfr_detail_6_9435.xml` | FAA | US Government work, public domain |  |
| `flights/tfr_list.json` | FAA | US Government work, public domain |  |
| `flights/tfr_wfs.json` | FAA | US Government work, public domain |  |
| `flights/vatspy_firs.geojson` | VATSpy data project | CC BY-SA 4.0 (share-alike; not MIT) | Source: VATSpy data project, CC BY-SA 4.0. This file stays under CC BY-SA 4.0. |

## hazards

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `hazards/cems_EMSR936.json` | Copernicus EMS Rapid Mapping | Free and open, cite the source; prose replaced by placeholders | (c) European Union, Copernicus Emergency Management Service. |
| `hazards/cems_EMSR937.json` | Copernicus EMS Rapid Mapping | Free and open, cite the source; prose replaced by placeholders | (c) European Union, Copernicus Emergency Management Service. |
| `hazards/cems_list.json` | Copernicus EMS Rapid Mapping | Free and open, cite the source; prose replaced by placeholders | (c) European Union, Copernicus Emergency Management Service. |
| `hazards/cems_observed.json` | Copernicus EMS Rapid Mapping | Free and open, cite the source; prose replaced by placeholders | (c) European Union, Copernicus Emergency Management Service. |
| `hazards/ea_floods.json` | Environment Agency | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `hazards/ea_history.json` | Environment Agency | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `hazards/ea_polygon_113FWCSEVERE.json` | Environment Agency | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `hazards/ea_polygon_113FWFEXE01.json` | Environment Agency | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `hazards/ea_polygon_114WAFT1W01A00.json` | Environment Agency | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `hazards/ea_readings.json` | Environment Agency | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `hazards/ea_readings_later.json` | Environment Agency | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `hazards/ea_stations.json` | Environment Agency | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `hazards/eonet.json` | NASA EONET | NASA open data | Source: NASA EONET |
| `hazards/firms_api.csv` | NASA FIRMS | NASA open data (EOSDIS policy) | Source: NASA FIRMS |
| `hazards/firms_empty.csv` | NASA FIRMS | NASA open data (EOSDIS policy) | Source: NASA FIRMS |
| `hazards/firms_modis.csv` | NASA FIRMS | NASA open data (EOSDIS policy) | Source: NASA FIRMS |
| `hazards/firms_viirs.csv` | NASA FIRMS | NASA open data (EOSDIS policy) | Source: NASA FIRMS |
| `hazards/gdacs_events.json` | GDACS (EC JRC, UN OCHA) | Free use with credit | Source: GDACS |
| `hazards/gdacs_rss.xml` | GDACS (EC JRC, UN OCHA) | Free use with credit | Source: GDACS |
| `hazards/gdacs_tc_map.json` | GDACS (EC JRC, UN OCHA) | Free use with credit | Source: GDACS |
| `hazards/glofas_caps.xml` | GloFAS (Copernicus EMS) | CEMS-FLOODS licence: free reuse with credit | Source: Copernicus Emergency Management Service (GloFAS). |
| `hazards/gvp_wfs.json` | Smithsonian Global Volcanism Program | Names, numbers and positions only; prose replaced by placeholders | Global Volcanism Program, Smithsonian Institution, Volcanoes of the World. |
| `hazards/hans_cap.json` | USGS Volcano Hazards Program | Public domain | Credit: USGS |
| `hazards/hans_elevated.json` | USGS Volcano Hazards Program | Public domain | Credit: USGS |
| `hazards/meteoalarm_aliases.csv` | MeteoAlarm (EUMETNET) | MeteoAlarm terms (CC BY 4.0 equivalent) | Source: MeteoAlarm, EUMETNET, and the national weather service. |
| `hazards/meteoalarm_cap.xml` | MeteoAlarm (EUMETNET) | MeteoAlarm terms (CC BY 4.0 equivalent) | Source: MeteoAlarm, EUMETNET, and the national weather service. |
| `hazards/meteoalarm_geocodes.json` | MeteoAlarm (EUMETNET) | MeteoAlarm terms (CC BY 4.0 equivalent) | Source: MeteoAlarm, EUMETNET, and the national weather service. |
| `hazards/meteoalarm_spain.xml` | MeteoAlarm (EUMETNET) | MeteoAlarm terms (CC BY 4.0 equivalent) | Source: MeteoAlarm, EUMETNET, and the national weather service. |
| `hazards/meteoalarm_uk.xml` | MeteoAlarm (EUMETNET) | MeteoAlarm terms (CC BY 4.0 equivalent) | Source: MeteoAlarm, EUMETNET, and the national weather service. |
| `hazards/nhc_cone.geojson` | NOAA / NWS | US Government work, public domain | Credit: NOAA |
| `hazards/nhc_current.json` | NOAA / NWS | US Government work, public domain | Credit: NOAA |
| `hazards/nhc_points.geojson` | NOAA / NWS | US Government work, public domain | Credit: NOAA |
| `hazards/nhc_service.json` | NOAA / NWS | US Government work, public domain | Credit: NOAA |
| `hazards/nws_alerts.json` | NOAA / NWS | US Government work, public domain | Credit: NOAA |
| `hazards/nws_zone.json` | NOAA / NWS | US Government work, public domain | Credit: NOAA |
| `hazards/tsunami_PAAQAtom.xml` | NOAA / NWS | US Government work, public domain | Credit: NOAA |
| `hazards/tsunami_PHEBAtom.xml` | NOAA / NWS | US Government work, public domain | Credit: NOAA |
| `hazards/usgs_detail.json` | USGS | Public domain | Credit: USGS |
| `hazards/usgs_feed.json` | USGS | Public domain | Credit: USGS |
| `hazards/who_don.json` | WHO Disease Outbreak News (shape) | Synthetic: invented report text and IDs |  |

## infra

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `infra/airports_sample.csv` | OurAirports | Public domain |  |
| `infra/cable_geo_sample.json` | TeleGeography Submarine Cable Map (shape) | Synthetic: invented cables and owners |  |
| `infra/ecb_90d_sample.xml` | European Central Bank | Free reuse with source | Source: ECB |
| `infra/energy_ci_intensity.json` | NESO Carbon Intensity API | CC BY 4.0 | Source: National Energy System Operator (NESO), CC BY 4.0. |
| `infra/energy_ci_postcode.json` | NESO Carbon Intensity API | CC BY 4.0 | Source: National Energy System Operator (NESO), CC BY 4.0. |
| `infra/energy_ci_region13.json` | NESO Carbon Intensity API | CC BY 4.0 | Source: National Energy System Operator (NESO), CC BY 4.0. |
| `infra/energy_ci_regional.json` | NESO Carbon Intensity API | CC BY 4.0 | Source: National Energy System Operator (NESO), CC BY 4.0. |
| `infra/energy_ec_power_de.json` | Energy-Charts (Fraunhofer ISE) | CC BY 4.0 | Source: Energy-Charts, Fraunhofer ISE, CC BY 4.0. |
| `infra/energy_ec_price_fr.json` | Energy-Charts (Fraunhofer ISE) | CC BY 4.0 | Source: Energy-Charts, Fraunhofer ISE, CC BY 4.0. |
| `infra/energy_ec_signal_empty.json` | Energy-Charts (Fraunhofer ISE) | CC BY 4.0 | Source: Energy-Charts, Fraunhofer ISE, CC BY 4.0. |
| `infra/energy_ec_signal_fr.json` | Energy-Charts (Fraunhofer ISE) | CC BY 4.0 | Source: Energy-Charts, Fraunhofer ISE, CC BY 4.0. |
| `infra/energy_elexon_fuelinst.json` | Elexon BMRS | BMRS data licence | Contains BMRS data (c) Elexon Limited copyright and database right 2026. |
| `infra/energy_elexon_indo.json` | Elexon BMRS | BMRS data licence | Contains BMRS data (c) Elexon Limited copyright and database right 2026. |
| `infra/energy_elexon_mid.json` | Elexon BMRS | BMRS data licence | Contains BMRS data (c) Elexon Limited copyright and database right 2026. |
| `infra/faa_status_klax.json` | FAA | US Government work, public domain |  |
| `infra/faa_status_ksan.json` | FAA | US Government work, public domain |  |
| `infra/finnhub_aapl.json` | Finnhub (shape) | Free tier terms |  |
| `infra/fred_brent_sample.csv` | FRED (Federal Reserve Bank of St. Louis) | Public data; some series copyrighted by their owners | Source: FRED, Federal Reserve Bank of St. Louis. |
| `infra/frequencies_sample.csv` | OurAirports | Public domain |  |
| `infra/goldapi_xau.json` | gold-api.com (shape) | Free API, fair use |  |
| `infra/gppd_sample.csv` | WRI Global Power Plant Database | CC BY 4.0 | Source: World Resources Institute, CC BY 4.0. |
| `infra/landing_geo_sample.json` | TeleGeography Submarine Cable Map (shape) | Synthetic: invented cables and owners |  |
| `infra/locode_latin1_sample.csv` | UN/LOCODE (UNECE) | Free to use with attribution | Source: UNECE UN/LOCODE. |
| `infra/locode_sample.csv` | UN/LOCODE (UNECE) | Free to use with attribution | Source: UNECE UN/LOCODE. |
| `infra/metar_bbox.json` | Aviation Weather Center | US Government work, public domain |  |
| `infra/metar_egll.json` | Aviation Weather Center | US Government work, public domain |  |
| `infra/ne_ports_sample.geojson` | Natural Earth (simplified shapes) | Public domain | Made with Natural Earth. |
| `infra/overpass_sample.json` | OpenStreetMap (Overpass API) | ODbL 1.0 (not MIT) | (c) OpenStreetMap contributors, ODbL 1.0. |
| `infra/peeringdb_sample.json` | PeeringDB | PeeringDB acceptable use policy | Source: PeeringDB |
| `infra/regions_sample.csv` | OurAirports | Public domain |  |
| `infra/runways_sample.csv` | OurAirports | Public domain |  |
| `infra/tg_cable_alpha.json` | TeleGeography Submarine Cable Map (shape) | Synthetic: invented cables and owners |  |
| `infra/tg_country_france.json` | TeleGeography Submarine Cable Map (shape) | Synthetic: invented cables and owners |  |
| `infra/tg_country_tonga.json` | TeleGeography Submarine Cable Map (shape) | Synthetic: invented cables and owners |  |
| `infra/tg_country_tunisia.json` | TeleGeography Submarine Cable Map (shape) | Synthetic: invented cables and owners |  |
| `infra/twelvedata_aapl.json` | Twelve Data (shape) | Free tier terms |  |
| `infra/wpi_sample.csv` | NGA World Port Index | US Government work, public domain |  |

## intel

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `intel/deepstate_sample.json` | DeepStateMap (shape) | Synthetic: boxes and invented labels in the source's label format |  |
| `intel/doc_sample.json` | GDELT DOC 2.0 API | Open data, attribution to the GDELT Project | Source: The GDELT Project |
| `intel/fcdo_index.json` | GOV.UK content API (FCDO travel advice) | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `intel/fcdo_pages.json` | GOV.UK content API (FCDO travel advice) | Open Government Licence v3.0 | Contains public sector information licensed under the Open Government Licence v3.0. |
| `intel/feodo_sample.json` | abuse.ch Feodo Tracker | abuse.ch terms of use | Source: abuse.ch Feodo Tracker |
| `intel/gdelt_sample.export.CSV` | GDELT 2.0 Events | Open data; source URLs replaced by example.org links | Source: The GDELT Project |
| `intel/ioda_sample.json` | IODA, Georgia Tech (shape) | Synthetic scores |  |
| `intel/radar_annotations.json` | Cloudflare Radar (shape) | Synthetic: documentation-range ASNs and prefixes, invented names and text |  |
| `intel/radar_anomalies.json` | Cloudflare Radar (shape) | Synthetic: documentation-range ASNs and prefixes, invented names and text |  |
| `intel/radar_hijacks.json` | Cloudflare Radar (shape) | Synthetic: documentation-range ASNs and prefixes, invented names and text |  |
| `intel/radar_leaks.json` | Cloudflare Radar (shape) | Synthetic: documentation-range ASNs and prefixes, invented names and text |  |
| `intel/ucdp_sample.csv` | UCDP GED | CC BY 4.0 | Source: Uppsala Conflict Data Program (UCDP), CC BY 4.0. |
| `intel/us_advisories.json` | US State Department travel advisories | US Government work, public domain |  |
| `intel/wb_countries.json` | World Bank | CC BY 4.0 | Source: The World Bank, CC BY 4.0. |
| `intel/wb_indicator.json` | World Bank | CC BY 4.0 | Source: The World Bank, CC BY 4.0. |

## sanctions

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `sanctions/eu_sample.xml` | EU consolidated financial sanctions list | Reuse with acknowledgement (Decision 2011/833/EU) | Source: European Commission. |
| `sanctions/sdn_sample.csv` | OFAC SDN list (shape) | US Government work, public domain; test entries |  |
| `sanctions/uk_sample.xml` | UK Sanctions List (FCDO) | Open Government Licence v3.0; one record replaced by a fictional ship | Contains public sector information licensed under the Open Government Licence v3.0. |
| `sanctions/un_sample.xml` | UN Security Council consolidated list (shape) | Synthetic: fictional entries only |  |

## ships

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `ships/aisstream_messages.jsonl` | aisstream.io (shape) | Test messages with invented vessels |  |
| `ships/digitraffic_locations.json` | Fintraffic Digitraffic marine | CC BY 4.0 | Source: Fintraffic / digitraffic.fi, CC BY 4.0. |
| `ships/digitraffic_vessels.json` | Fintraffic Digitraffic marine | CC BY 4.0 | Source: Fintraffic / digitraffic.fi, CC BY 4.0. |
| `ships/nmea_samples.txt` | Meridian test code or hand-written test data | MIT (this repository) |  |
| `ships/portwatch_chokepoints.json` | IMF PortWatch | IMF terms: reuse with attribution | Source: IMF PortWatch. |
| `ships/portwatch_daily_hormuz.json` | IMF PortWatch | IMF terms: reuse with attribution | Source: IMF PortWatch. |
| `ships/portwatch_stats.json` | IMF PortWatch | IMF terms: reuse with attribution | Source: IMF PortWatch. |

## space

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `space/gibs_domain_daily.xml` | NASA GIBS | NASA EOSDIS open data | Credit: NASA GIBS |
| `space/gibs_domain_ir.xml` | NASA GIBS | NASA EOSDIS open data | Credit: NASA GIBS |
| `space/gp_gps.json` | CelesTrak | Free public data | Source: CelesTrak |
| `space/gp_stations.json` | CelesTrak | Free public data | Source: CelesTrak |
| `space/ll2_pads.json` | The Space Devs Launch Library 2 (shape) | Invented mission names, IDs and links; placeholder text | Credit: The Space Devs |
| `space/ll2_previous.json` | The Space Devs Launch Library 2 (shape) | Invented mission names, IDs and links; placeholder text | Credit: The Space Devs |
| `space/ll2_upcoming.json` | The Space Devs Launch Library 2 (shape) | Invented mission names, IDs and links; placeholder text | Credit: The Space Devs |
| `space/openmeteo_london.json` | Open-Meteo (shape) | Synthetic weather values |  |
| `space/plus/eumetview_caps.xml` | EUMETSAT EUMETView | CC BY 4.0 (Core data) | Contains EUMETSAT data, CC BY 4.0. |
| `space/plus/gibs_domain_no2.xml` | NASA GIBS | NASA EOSDIS open data | Credit: NASA GIBS |
| `space/plus/gibs_domain_sar.xml` | NASA GIBS | NASA EOSDIS open data | Credit: NASA GIBS |
| `space/plus/stac_cdse_s1.json` | STAC search APIs (Copernicus, Element 84, Planetary Computer) | Sentinel data free and open; Landsat public domain | Contains modified Copernicus Sentinel data 2026; Landsat imagery courtesy of USGS. |
| `space/plus/stac_e84_s2.json` | STAC search APIs (Copernicus, Element 84, Planetary Computer) | Sentinel data free and open; Landsat public domain | Contains modified Copernicus Sentinel data 2026; Landsat imagery courtesy of USGS. |
| `space/plus/stac_pc_landsat.json` | STAC search APIs (Copernicus, Element 84, Planetary Computer) | Sentinel data free and open; Landsat public domain | Contains modified Copernicus Sentinel data 2026; Landsat imagery courtesy of USGS. |
| `space/rainviewer.json` | RainViewer (shape) | Synthetic frame paths |  |
| `space/sgp4_reference.json` | SGP4 reference vectors | Published verification data |  |
| `space/swpc/alerts.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/alerts_storm.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/noaa-planetary-k-index-forecast.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/noaa-planetary-k-index-storm.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/noaa-planetary-k-index.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/noaa-scales-storm.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/noaa-scales.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/planetary_k_index_1m.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/propagated-solar-wind-1-hour.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/solar-wind-mag-field.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/solar-wind-speed.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/xray-flares-7-day.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/xray-flares-latest.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |
| `space/swpc/xray-flares-storm.json` | NOAA SWPC | Public domain | Credit: NOAA SWPC |

## workspace

| File | Source | Licence or status | Attribution |
|---|---|---|---|
| `workspace/browser_test.mjs` | Meridian test code or hand-written test data | MIT (this repository) |  |
| `workspace/deeplink_test.mjs` | Meridian test code or hand-written test data | MIT (this repository) |  |
| `workspace/logic_test.mjs` | Meridian test code or hand-written test data | MIT (this repository) |  |
