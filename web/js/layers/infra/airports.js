// Airports (OurAirports): large airports as rings, medium as dots, small airfields from zoom 7.
// Cards add runways, frequencies, live METAR, live arrivals and departures and every FAA status
// (delays, programmes, closures and notices; both from the flights module, when it runs) and a
// LiveATC search link (link only: their streams are never embedded or proxied).

import { INFRA, INK } from './shared.js';
import { delayBadge, delaySection as renderDelays } from './delays.js';

const SRC = 'ov-infra-airports';
const SRC_MINOR = 'ov-infra-airports-minor';
const SRC_RWY = 'ov-infra-airports-runways';
const L_LARGE = 'ov-infra-ap-large';
const L_MEDIUM = 'ov-infra-ap-medium';
const L_SMALL = 'ov-infra-ap-small';
const L_LBL_LARGE = 'ov-infra-ap-lbl-large';
const L_LBL_MEDIUM = 'ov-infra-ap-lbl-medium';
const L_LBL_SMALL = 'ov-infra-ap-lbl-small';
const L_RWY = 'ov-infra-ap-runways';
const GOLD = '#e3cd6b';
const CAT_TONE = { VFR: 'green', MVFR: 'blue', IFR: 'red', LIFR: 'red' };

export function registerAirports(ctx, s) {
  const { h, fmt, map2 } = ctx;
  let minor = null;

  function addMinorLayers() {
    map2.addLayer({ id: L_SMALL, type: 'circle', source: SRC_MINOR, minzoom: 6.5, paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 6.5, 1.3, 10, 2.6, 14, 4.2],
      'circle-color': 'rgba(212, 191, 94, 0.78)', 'circle-stroke-color': INK, 'circle-stroke-width': 0.6 } }, 'points');
    map2.addLayer({ id: L_LBL_SMALL, type: 'symbol', source: SRC_MINOR, minzoom: 11, layout: {
      'text-field': ['get', 'n'], 'text-font': ['Noto Sans Regular'], 'text-size': 11, 'text-offset': [0, 0.8],
      'text-anchor': 'top', 'text-max-width': 9, 'text-optional': true },
      paint: { 'text-color': '#d9cf9f', 'text-halo-color': INK, 'text-halo-width': 1.3 } }, 'points');
  }

  async function show() {
    map2.addSource(SRC, { type: 'geojson', data: s.geoUrl('airports-major') });
    map2.addLayer({ id: L_MEDIUM, type: 'circle', source: SRC, filter: ['==', ['get', 't'], 2], minzoom: 3.5, paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 3.5, 1.4, 6, 2.4, 9, 3.6, 13, 5.5],
      'circle-color': INFRA, 'circle-opacity': ['interpolate', ['linear'], ['zoom'], 3.5, 0.55, 6, 0.95],
      'circle-stroke-color': INK, 'circle-stroke-width': 0.8 } }, 'points');
    map2.addLayer({ id: L_LARGE, type: 'circle', source: SRC, filter: ['==', ['get', 't'], 3], paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 1.5, 1.6, 4, 3, 7, 5, 11, 7.5],
      'circle-color': INK, 'circle-stroke-color': GOLD,
      'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 1.5, 0.9, 4, 1.4, 6, 1.8, 10, 2.4] } }, 'points');
    map2.addLayer({ id: L_LBL_LARGE, type: 'symbol', source: SRC, filter: ['==', ['get', 't'], 3], minzoom: 4.2, layout: {
      'text-field': ['step', ['zoom'], ['coalesce', ['get', 'i'], ''], 7.5, ['get', 'n']],
      'text-font': ['step', ['zoom'], ['literal', ['Open Sans Bold']], 7.5, ['literal', ['Noto Sans Regular']]],
      'text-size': ['interpolate', ['linear'], ['zoom'], 4.2, 10.5, 9, 12.5], 'text-variable-anchor': ['top', 'bottom', 'right', 'left'],
      'text-radial-offset': 0.85, 'text-justify': 'auto', 'text-max-width': 10, 'text-optional': true,
      'symbol-sort-key': ['case', ['has', 's'], 0, 1] },
      paint: { 'text-color': GOLD, 'text-halo-color': INK, 'text-halo-width': 1.5 } }, 'points');
    map2.addLayer({ id: L_LBL_MEDIUM, type: 'symbol', source: SRC, filter: ['==', ['get', 't'], 2], minzoom: 7.5, layout: {
      'text-field': ['step', ['zoom'], ['coalesce', ['get', 'i'], ''], 9.5, ['get', 'n']],
      'text-font': ['Noto Sans Regular'], 'text-size': 11, 'text-offset': [0, 0.8], 'text-anchor': 'top',
      'text-max-width': 9, 'text-optional': true },
      paint: { 'text-color': '#d9cf9f', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
    minor = s.lazySource(SRC_MINOR, s.geoUrl('airports-minor'), 6.5, addMinorLayers);
    minor.start();
    const st = await s.status();
    layer.setCount(st.counts?.airports ?? null);
  }

  const layer = ctx.layers.register({
    id: 'infra.airports', group: 'Infrastructure', title: 'Airports', subtitle: 'OurAirports, with live weather',
    description: 'Large airports as rings, medium airports as dots and small airfields from zoom 7. Click for runways, radio frequencies, the latest weather report and live traffic.',
    source: 'OurAirports (public domain); METAR from aviationweather.gov', color: INFRA, symbol: 'point',
    legend: [{ color: GOLD, label: 'Large airport (ring)' }, { color: INFRA, label: 'Medium airport' }, { color: 'rgba(212,191,94,.78)', label: 'Small airfield' }],
    async enable() {
      map2.addSource(SRC_RWY, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: L_RWY, type: 'line', source: SRC_RWY, minzoom: 10, layout: { 'line-cap': 'butt' }, paint: {
        'line-color': GOLD, 'line-opacity': 0.55, 'line-width': ['interpolate', ['exponential', 2], ['zoom'], 10, 2, 16, 40] } }, 'lines');
      if (await s.ensureReady('airports', layer, show)) await show();
    },
    disable() { minor?.stop(); minor = null; map2.removeSource(SRC); map2.removeSource(SRC_RWY); },
  });

  map2.onClick([L_LARGE, L_MEDIUM, L_SMALL], (f) => {
    const [lon, lat] = f.geometry.coordinates;
    openAirport({ kind: 'airport', id: f.properties.id, title: f.properties.n, lat, lon, layer: 'infra.airports' });
  });
  map2.onHover([L_LARGE, L_MEDIUM, L_SMALL], (f) => [f.properties.n, f.properties.i].filter(Boolean).join(', '));

  function showRunways(a) {
    const feats = (a.runways || []).filter(r => r.ends).map(r => ({ type: 'Feature', geometry: { type: 'LineString', coordinates: r.ends }, properties: { n: r.name || '' } }));
    map2.setData(SRC_RWY, { type: 'FeatureCollection', features: feats });
  }

  function metarSection(m) {
    if (!m) return h('p', { class: 'dim' }, 'Loading the latest weather report…');
    if (m.error) return h('p', { class: 'dim' }, m.error);
    const r = m.report;
    if (!r) return h('p', { class: 'dim' }, 'No weather report (METAR) from this airport or within 40 km.');
    const rows = [
      ['Station', m.nearest ? `${r.station}, ${fmt.km(m.distance_km)} away` : r.station],
      ['Observed', r.time ? `${fmt.utc(r.time)} (${fmt.ago(r.time)})` : null],
      ['Conditions', r.category_label], ['Wind', r.wind], ['Visibility', r.visibility],
      ['Temperature', r.temp_c != null ? `${fmt.num(r.temp_c, 0)} °C, dew point ${fmt.num(r.dewpoint_c, 0)} °C` : null],
      ['Pressure', r.altimeter_hpa != null ? `${fmt.num(r.altimeter_hpa, 0)} hPa` : null],
      ['Cloud', (r.clouds || []).join(', ') || null], ['Weather', r.weather]];
    const dl = h('dl', { class: 'kv' });
    for (const [k, v] of rows) if (v != null && v !== '') dl.append(h('dt', null, k), h('dd', null, String(v)));
    return h('div', null, dl, h('pre', { class: 'infra-raw' }, r.raw || ''));
  }

  function trafficSection(t) {
    if (t === undefined) return null;
    if (t === null) return h('p', { class: 'dim' }, 'Looking for aircraft near the airport…');
    if (t.error) return h('p', { class: 'dim' }, t.error);
    const list = (items, verb) => {
      if (!items?.length) return h('p', { class: 'dim' }, `No ${verb} aircraft seen right now.`);
      const ul = h('ul', { class: 'card-list' });
      for (const it of items.slice(0, 8)) {
        const b = h('button', { type: 'button' }, h('span', { class: 'li-d' }, it.alt_ft != null ? fmt.ft(it.alt_ft) : ''),
          h('span', { class: 'li-t' }, it.title || it.id), h('span', { class: 'li-s' }, [it.route, it.subtitle].filter(Boolean).join(', ')));
        b.addEventListener('click', () => ctx.entities.open(it));
        ul.append(h('li', null, b));
      }
      return ul;
    };
    return h('div', { class: 'infra-traffic' },
      h('h4', null, `Arriving (${t.arrivals?.length || 0})`), list(t.arrivals, 'arriving'),
      h('h4', null, `Departing (${t.departures?.length || 0})`), list(t.departures, 'departing'),
      t.note ? h('p', { class: 'faint' }, t.note) : null);
  }

  // FAA delays from the flights module (CONTRACT 9.1 cross-team endpoint); helpers in ./delays.js.
  const delaySection = (d) => renderDelays(d, { h, fmt });

  async function loadDelays(a) {
    try {
      const d = await ctx.api.get(`/api/flights/airport-status/${encodeURIComponent(a.id)}`);
      return d && typeof d === 'object' ? d : undefined;
    } catch { return undefined; }   // 404 (no FAA delay data for this airport) or any error: hide the section
  }

  function spec(a, extra) {
    const met = extra.metar?.report;
    const rw = a.runways || [];
    const longest = a.longest_runway_ft;
    // One row per service: "TWR Heathrow Tower 118.500, 118.700 MHz".
    const groups = [];
    for (const f of a.frequencies || []) {
      const key = `${f.type || ''}|${f.description || ''}`;
      let g = groups.find(x => x.key === key);
      if (!g) { g = { key, type: f.type, description: f.description, mhz: [] }; groups.push(g); }
      if (!g.mhz.includes(f.mhz)) g.mhz.push(f.mhz);
    }
    const freqEl = groups.length ? h('table', { class: 'infra-tbl' }, groups.slice(0, 10).map(g => h('tr', null,
      h('td', null, g.type || ''), h('td', { class: 'dim' }, g.description || ''),
      h('td', { class: 'num' }, `${g.mhz.map(x => x.toFixed(3)).join(', ')} MHz`)))) : null;
    const rwEl = rw.length ? h('table', { class: 'infra-tbl' }, rw.map(r => h('tr', null,
      h('td', null, r.name || 'Runway'),
      h('td', { class: 'num' }, r.length_ft ? `${fmt.int(r.length_ft * 0.3048)} m` : ''),
      h('td', { class: 'num dim' }, r.width_ft ? `${fmt.int(r.width_ft * 0.3048)} m wide` : ''),
      h('td', { class: 'dim' }, [r.surface, r.lighted ? 'lit' : null].filter(Boolean).join(', '))))) : null;
    const flightsOn = ctx.moduleOk('flights');
    const cat = met?.category;
    return {
      key: `airport:${a.id}`, kind: 'airport', title: a.name,
      subtitle: s.placeLine([a.iata, a.icao || a.id].filter(Boolean).join(', '), a.municipality, a.country_name),
      icon: '✈︎', accent: INFRA,
      badges: [{ text: a.type_label, tone: a.type === 3 ? 'blue' : 'grey' }, a.scheduled ? { text: 'Scheduled service', tone: 'grey' } : null,
        cat ? { text: `${cat} now`, tone: CAT_TONE[cat] || 'grey', title: met.category_label } : null, delayBadge(extra.delays)],
      block: [
        { label: 'IATA', value: a.iata || '–' },
        { label: a.icao ? 'ICAO' : 'Code', value: a.icao || a.id },
        { label: 'Elevation', value: a.elevation_ft != null ? fmt.int(a.elevation_ft) : '–', unit: a.elevation_ft != null ? 'ft' : '' },
        longest ? { label: 'Longest runway', value: fmt.int(longest * 0.3048), unit: 'm' } : { label: 'Runways', value: String(rw.length) },
      ],
      sections: [
        flightsOn && extra.delays ? { title: 'Delays', el: delaySection(extra.delays) } : null,
        { title: 'Weather now', el: metarSection(extra.metar) },
        flightsOn ? { title: 'Live traffic', el: trafficSection(extra.traffic) } : null,
        rwEl ? { title: `Runways (${rw.length})`, el: rwEl } : null,
        freqEl ? { title: 'Radio frequencies', el: freqEl } : null,
        { title: 'Airport', rows: [['Type', a.type_label], ['Town', a.municipality], ['Region', a.region], ['Country', a.country_name],
          ['GPS code', a.gps_code], ['Local code', a.local_code], ['Position', fmt.latlon(a.lat, a.lon)]] },
        { title: 'Nearby', nearby: { lat: a.lat, lon: a.lon, radius_km: 30, exclude: { kind: 'airport', id: a.id } } },
      ],
      links: [
        a.icao ? { text: 'Listen to tower audio on LiveATC', href: `https://www.liveatc.net/search/?icao=${encodeURIComponent(a.icao)}` } : null,
        { text: 'OurAirports page', href: `https://ourairports.com/airports/${encodeURIComponent(a.id)}/` },
        a.wikipedia ? { text: 'Wikipedia', href: a.wikipedia } : null, a.home ? { text: 'Airport website', href: a.home } : null,
      ].filter(Boolean),
      entity: { ...a.entity, layer: 'infra.airports' },
      foot: 'Airport data from OurAirports (public domain). Weather from aviationweather.gov, refreshed every 5 minutes.',
    };
  }

  async function loadTraffic(a) {
    try {
      return await ctx.api.get(`/api/flights/airport/${encodeURIComponent(a.id)}`, { lat: a.lat, lon: a.lon });
    } catch { return { error: 'Live traffic is unavailable right now.' }; }
  }

  async function loadMetar(a) {
    try { return await ctx.api.get(`/api/infra/metar/${encodeURIComponent(a.id)}`); } catch (err) {
      return { error: err?.message || 'The weather report could not be loaded.' };
    }
  }

  async function openAirport(entity) {
    let a;
    try { a = await s.get(s.entityPath('airport', entity.id)); } catch (err) { return s.missingCard(entity, 'airport', INFRA, err); }
    const flightsOn = ctx.moduleOk('flights');
    const extra = { metar: null, traffic: flightsOn ? null : undefined, delays: undefined };
    if (ctx.map.getSource(SRC_RWY)) showRunways(a);
    const card = ctx.cards.open({ ...spec(a, extra),
      live: { everyMs: 60000, update: async () => {
        const [metar, traffic, delays] = await Promise.all([loadMetar(a), extra.traffic !== undefined ? loadTraffic(a) : Promise.resolve(undefined),
          flightsOn ? loadDelays(a) : Promise.resolve(undefined)]);
        extra.metar = metar;
        if (traffic !== undefined) extra.traffic = traffic;
        extra.delays = delays;
        return spec(a, extra);
      } },
      onClose() { if (ctx.map.getSource(SRC_RWY)) map2.setData(SRC_RWY, map2.emptyFC()); },
    });
    const live = (patch) => { if (card.isOpen()) card.update({ ...spec(a, Object.assign(extra, patch)) }); };
    loadMetar(a).then(metar => live({ metar }));
    if (extra.traffic !== undefined) loadTraffic(a).then(traffic => live({ traffic }));
    if (flightsOn) loadDelays(a).then(delays => { if (delays) live({ delays }); });
    return card;
  }

  ctx.entities.handler('airport', openAirport, { group: 'Infrastructure' });
  return { openAirport, delaySection, delayBadge };
}
