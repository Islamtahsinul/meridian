// Countries: instability choropleth (feature-state per ISO code), country card, What's here section.

import { INTEL, INK, INDEX_RAMP, INDEX_BANDS, indexColour, bandOf, money, people, meter, safeGet, errorText, short } from './common.js';
import { openConflict } from './conflict.js';
import { adviceSection } from './advice.js';
import { internetSection } from './radar.js';
import { protestSection } from './protests.js';
import { spikeSection } from './spikes.js';

const SRC = 'ov-intel-countries';
const L_FILL = 'ov-intel-countries-fill';
const L_LINE = 'ov-intel-countries-line';
const L_HOVER = 'ov-intel-countries-hover';
const PART_KEYS = { index: 'i', governance: 'g', conflict: 'c', tone: 't' };

export function registerCountries(ctx) {
  const { map2, h, fmt } = ctx;
  let shapesLoaded = false;
  let lastIndex = null;
  let hovered = null;

  const fillColour = (key) => ['case', ['==', ['coalesce', ['feature-state', key], -1], -1], 'rgba(0,0,0,0)',
    ['step', ['feature-state', key], INDEX_RAMP[0], 20, INDEX_RAMP[1], 40, INDEX_RAMP[2], 60, INDEX_RAMP[3], 80, INDEX_RAMP[4]]];
  // Stable countries recede: opacity rises with the band as well as lightness.
  const fillOpacity = (key) => ['case', ['==', ['coalesce', ['feature-state', key], -1], -1], 0,
    ['step', ['feature-state', key], 0.08, 20, 0.13, 40, 0.19, 60, 0.26, 80, 0.33]];

  function applyIndex(data) {
    lastIndex = data;
    const map = ctx.map;
    if (!map.getSource(SRC)) return;
    for (const [iso2, v] of Object.entries(data.countries || {})) {
      map.setFeatureState({ source: SRC, id: iso2 }, { i: v.i, g: v.g, c: v.c, t: v.t, h: v.h });
    }
  }

  const layer = ctx.layers.register({
    id: 'intel.countries', group: 'Intelligence', title: 'Country instability', subtitle: 'Index 0-100, click a country for its profile',
    description: 'A composite of political stability (World Bank WGI), conflict events and news tone (GDELT) and active hazards. A rough guide, not a forecast.',
    source: 'Natural Earth (public domain), World Bank (CC BY 4.0), GDELT', color: INDEX_RAMP[3], symbol: 'area',
    defaultOn: false, refreshMs: 10 * 60 * 1000,
    options: [{ id: 'part', label: 'Colour by', type: 'select', default: 'index', refresh: false,
      choices: [{ value: 'index', label: 'Instability index' }, { value: 'governance', label: 'Political stability part' },
        { value: 'conflict', label: 'Conflict part' }, { value: 'tone', label: 'News tone part' }] }],
    legend: INDEX_BANDS.map((b, i) => ({ color: INDEX_RAMP[i], label: `${b.label} (${b.from}${b.to > 100 ? '+' : `–${b.to}`})`, shape: 'area' })),
    async enable({ options }) {
      const shapes = await ctx.api.get('/api/intel/countries/shapes');
      map2.addSource(SRC, { type: 'geojson', data: shapes, promoteId: 'iso2' });
      shapesLoaded = true;
      const key = PART_KEYS[options.part] || 'i';
      map2.addLayer({ id: L_FILL, type: 'fill', source: SRC, paint: { 'fill-color': fillColour(key), 'fill-opacity': fillOpacity(key) } }, 'polygons');
      map2.addLayer({ id: L_LINE, type: 'line', source: SRC, paint: { 'line-color': '#3a5080', 'line-width': 0.5, 'line-opacity': 0.7 } }, 'polygons');
      map2.addLayer({ id: L_HOVER, type: 'line', source: SRC,
        paint: { 'line-color': '#ebe6d6', 'line-width': ['case', ['boolean', ['feature-state', 'hover'], false], 1.6, 0], 'line-opacity': 0.9 } }, 'lines');
    },
    disable() { map2.removeSource(SRC); shapesLoaded = false; hovered = null; },
    async refresh({ signal }) {
      const data = await ctx.api.get('/api/intel/countries/index', null, { signal });
      applyIndex(data);
      const n = Object.keys(data.countries || {}).length;
      layer.setCount(n);
      if (!n) layer.setStatus('warn', 'The index is still being computed (World Bank data and GDELT history are loading).');
      else layer.setStatus('ok', data.coverage_days < 7 ? `Conflict parts use ${fmt.num(data.coverage_days, 1)} days of GDELT history so far, scaled to 30 days.` : null);
    },
    onOption(id, value) {
      if (id === 'part' && ctx.map.getLayer(L_FILL)) {
        ctx.map.setPaintProperty(L_FILL, 'fill-color', fillColour(PART_KEYS[value] || 'i'));
        ctx.map.setPaintProperty(L_FILL, 'fill-opacity', fillOpacity(PART_KEYS[value] || 'i'));
      }
    },
  });

  map2.onClick([L_FILL], (f, lngLat) => {
    const iso2 = f.properties.iso2;
    if (iso2) openCountry(ctx, { kind: 'country', id: iso2, title: f.properties.name });
    else ctx.whatshere.open(lngLat.lat, lngLat.lng);
  });
  map2.onHover([L_FILL], (f) => {
    const iso2 = f.properties.iso2;
    if (hovered !== iso2 && ctx.map.getSource(SRC)) {
      if (hovered) ctx.map.setFeatureState({ source: SRC, id: hovered }, { hover: false });
      if (iso2) ctx.map.setFeatureState({ source: SRC, id: iso2 }, { hover: true });
      hovered = iso2 || null;
    }
    const v = lastIndex?.countries?.[iso2];
    return v ? `${f.properties.name}: ${v.i}, ${v.b.toLowerCase()}` : f.properties.name;
  });
  const clearHover = () => {
    if (hovered && ctx.map.getSource(SRC)) ctx.map.setFeatureState({ source: SRC, id: hovered }, { hover: false });
    hovered = null;
  };
  ctx.map.on('mouseout', clearHover);
  ctx.map.on('mousemove', ctx.dom.throttle((e) => {
    // the hover callback only runs over features, so clear the outline when the pointer reaches the sea
    if (!hovered || !ctx.map.getLayer(L_FILL)) return;
    if (!ctx.map.queryRenderedFeatures(e.point, { layers: [L_FILL] }).length) clearHover();
  }, 80));
  ctx.entities.handler('country', (e) => openCountry(ctx, e), { group: 'Intelligence' });

  ctx.whatshere.section(async (lat, lon) => {
    const r = await safeGet(ctx, '/api/intel/country-at', { lat: lat.toFixed(5), lon: lon.toFixed(5) });
    if (r.__error || !r.country) return null;
    const c = r.country;
    const el = h('div', { class: 'intel-wh' });
    const name = h('div', { class: 'intel-wh-name' }, c.name);
    el.append(name);
    if (c.index != null) {
      el.append(h('div', { class: 'intel-wh-index' }, h('span', { class: 'intel-swatch', style: { background: indexColour(c.index) } }),
        `Instability ${c.index}, ${String(c.band).toLowerCase()}`, c.capital ? `, capital ${c.capital}` : ''));
    }
    if (c.note) el.append(h('p', { class: 'intel-wh-note' }, c.note));
    const codes = c.iso2 ? [c.iso2] : (c.related || []);
    const btns = h('div', { class: 'intel-wh-btns' });
    for (const iso2 of codes) {
      const b = h('button', { type: 'button', class: 'btn' }, codes.length > 1 ? `${iso2} profile` : 'Country profile');
      b.addEventListener('click', () => openCountry(ctx, { kind: 'country', id: iso2 }));
      btns.append(b);
    }
    el.append(btns);
    return { title: 'Country', el };
  });
}

export async function openCountry(ctx, entity) {
  const { h, fmt } = ctx;
  const iso2 = String(entity.id || '').toUpperCase();
  const key = `country:${iso2}`;
  const card = ctx.cards.open({ key, kind: 'country', title: entity.title || iso2, subtitle: 'Loading the profile…', icon: '⚑', accent: INTEL });
  const d = await safeGet(ctx, `/api/intel/country/${encodeURIComponent(iso2)}`);
  if (d.__error) {
    card.update({ subtitle: 'Country', sections: [{ text: errorText(d.__error) }] });
    return card;
  }
  const ind = d.indicators || {};
  const v = (k) => ind[k]?.value;
  const yr = (k) => (ind[k]?.year ? ` (${ind[k].year})` : '');
  const inst = d.instability || {};
  const badges = [];
  const fa = d.travel_advice?.fcdo;
  if (fa && fa.level >= 3) badges.push({ text: fa.level >= 4 ? 'FCDO: against all travel' : 'FCDO: against all but essential travel', tone: fa.level >= 4 ? 'red' : 'amber' });
  if (inst.index != null) badges.push({ text: `Instability ${inst.index}, ${inst.band.toLowerCase()}`, tone: inst.index >= 60 ? 'red' : inst.index >= 40 ? 'amber' : 'grey' });
  if (d.income) badges.push({ text: d.income, tone: 'grey' });
  for (const t of d.theatres || []) badges.push({ text: `Theatre: ${t}`, tone: 'blue', title: 'One of the monitored news theatres' });
  const entityFull = ctx.dom.validLatLon(d.lat, d.lon) ? { kind: 'country', id: d.iso2, title: d.name, subtitle: 'Country', lat: d.lat, lon: d.lon,
    layer: 'intel.countries', zoom: 4, bbox: d.bbox || undefined } : null;

  const meters = h('div', { class: 'intel-meters' });
  for (const p of inst.parts || []) meters.append(meter(ctx, p));
  if (inst.missing?.length) meters.append(h('p', { class: 'intel-meter-note' }, `Missing parts are left out and the weights rescaled: ${inst.missing.join(', ')}.`));

  const sections = [
    adviceSection(ctx, d.travel_advice),
    { title: 'Instability index', el: inst.parts ? meters : h('p', { class: 'dim' }, 'Not computed yet.') },
    { title: 'People and economy', rows: [
      ['Capital', d.capital], ['Region', d.region], ['Population', v('pop') != null ? `${people(fmt, v('pop'))}${yr('pop')}` : null],
      ['GDP', v('gdp') != null ? `${money(fmt, v('gdp'))}${yr('gdp')}` : null],
      ['GDP per capita', v('gdp_pc') != null ? `${money(fmt, v('gdp_pc'))}${yr('gdp_pc')}` : null],
      ['Inflation', v('inflation') != null ? `${fmt.num(v('inflation'), 1)}%${yr('inflation')}` : null],
      ['Unemployment', v('unemployment') != null ? `${fmt.num(v('unemployment'), 1)}%${yr('unemployment')}` : null],
      ['Military spending', v('military') != null ? `${fmt.num(v('military'), 1)}% of GDP${yr('military')}` : null],
      ['Political stability', v('stability') != null ? `${(+v('stability')).toFixed(2)} on the WGI scale (-2.5 to 2.5)${yr('stability')}` : null],
    ] },
    { title: `Conflict events, last 7 days (${fmt.int(d.conflict?.events_7d || 0)})`,
      list: (d.conflict?.recent || []).slice(0, 8).map(e => ({ title: e.title, subtitle: short(e.subtitle, 70), detail: (+e.severity).toFixed(1),
        onClick: () => { ctx.map2.flyTo(e.lat, e.lon, 8); openConflict(ctx, e); } })),
      empty: 'No conflict events reported here in the last 7 days.' },
    protestSection(ctx, d.protests),
    { title: `News, last 24 hours (${fmt.int(d.news?.points_24h || 0)} places)`,
      list: (d.news?.top || []).slice(0, 4).map(n => ({ title: short(n.title, 90), subtitle: n.subtitle, detail: (+n.severity).toFixed(1), entity: n })),
      empty: 'No geolocated reports in the last 24 hours.' },
    spikeSection(ctx, d.news_spikes),
  ];
  if (d.cyber && (d.cyber.c2_servers || d.cyber.outage_signals)) {
    sections.push({ title: 'Cyber', rows: [
      ['Botnet command servers hosted here', d.cyber.c2_servers ? `${d.cyber.c2_servers} (${d.cyber.c2_online} online)` : null],
      ['Internet outage signals (24 h)', d.cyber.outage_signals ? `${d.cyber.outage_signals.events} events, ${d.cyber.outage_signals.methods.join(', ')}` : null],
    ] });
  }
  const net = internetSection(ctx, d.internet);
  if (net) sections.push(net);
  const infraBox = h('div', null, h('p', { class: 'dim' }, 'Loading infrastructure…'));
  if (ctx.moduleOk('infra')) sections.push({ title: 'Infrastructure', el: infraBox });
  for (const n of d.notes || []) sections.push({ text: n });

  card.update({
    title: d.name, subtitle: [d.capital ? `Capital ${d.capital}` : null, d.region].filter(Boolean).join(', '),
    icon: d.flag || '⚑', badges,
    block: [{ label: 'Instability', value: inst.index != null ? String(inst.index) : '–', unit: '/100' },
      { label: 'Population', value: v('pop') != null ? fmt.compact(v('pop')) : '–' },
      { label: 'GDP per head', value: v('gdp_pc') != null ? `$${fmt.compact(v('gdp_pc'))}` : '–' },
      { label: 'Events, 7 days', value: fmt.int(d.conflict?.events_7d || 0) }],
    sections,
    links: [{ text: 'World Bank data', href: `https://data.worldbank.org/country/${d.iso2.toLowerCase()}` },
      { text: 'Wikipedia', href: `https://en.wikipedia.org/wiki/${encodeURIComponent(d.name.replace(/ /g, '_'))}` }],
    entity: entityFull,
    foot: 'World Bank WDI and WGI (CC BY 4.0), Natural Earth, GDELT, FCDO (OGL v3.0), US State Department, Cloudflare Radar (CC BY-NC 4.0). The index is a rough composite, documented in the module notes.',
  });
  if (ctx.moduleOk('infra')) {
    ctx.api.get(`/api/infra/country/${encodeURIComponent(d.iso2)}`).then((r) => {
      ctx.dom.clear(infraBox);
      const cap = r.capacity_mw ? Object.entries(r.capacity_mw).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, mw]) => `${k} ${fmt.int(mw)} MW`).join(', ') : '';
      const cables = r.cables ? `${fmt.int(r.cables)}${r.cable_names?.length ? ` (${r.cable_names.slice(0, 3).join(', ')}${r.cable_names.length > 3 ? ' and others' : ''})` : ''}` : r.cables;
      const dl = h('dl', { class: 'kv' });
      for (const [k, val] of [['Submarine cables', cables], ['Cable landing points', r.landing_points], ['Power plants', r.power_plants],
        ['Total capacity', r.capacity_total_mw ? `${fmt.int(r.capacity_total_mw)} MW` : null], ['Largest sources', cap || null],
        ['Airports', r.airports], ['Ports', r.ports], ['Data centres', r.datacenters], ['Dams', r.dams]]) {
        if (val == null || val === '') continue;
        dl.append(h('dt', null, k), h('dd', null, typeof val === 'number' ? fmt.int(val) : String(val)));
      }
      infraBox.append(dl.children.length ? dl : h('p', { class: 'dim' }, 'No infrastructure listed.'));
    }).catch(() => { ctx.dom.clear(infraBox); infraBox.append(h('p', { class: 'dim' }, 'Infrastructure summary unavailable.')); });
  }
  return card;
}
