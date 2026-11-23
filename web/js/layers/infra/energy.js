// Electricity grids, live: the Energy drawer (Great Britain now, and a table of 36 European
// countries), the "Electricity grids" map layer (country choropleth, GB regions, Britain's
// interconnector flows), cards for countries, GB regions and interconnectors, and a "What's here?"
// section. Sources: NESO Carbon Intensity API, Elexon BMRS, Energy-Charts (Fraunhofer ISE).

import { INFRA, INK, PAPER } from './shared.js';
import { GOLD, METRICS, barList, classColor, fillPaint, flowList, indexBadge, legendFor, sparkline, ukDayTime, ukTime,
  utcTime } from './energy-viz.js';

const ICON = 'M11.5 2.5 5 11h4.5l-1 6.5L15 9h-4.5l1-6.5Z';
const SRC_C = 'ov-infra-grid-countries';
const SRC_R = 'ov-infra-grid-regions';
const SRC_L = 'ov-infra-grid-links';
const SRC_T = 'ov-infra-grid-labels';
const L_CFILL = 'ov-infra-grid-cfill';
const L_CLINE = 'ov-infra-grid-cline';
const L_HOVER = 'ov-infra-grid-hover';
const L_RFILL = 'ov-infra-grid-rfill';
const L_RLINE = 'ov-infra-grid-rline';
const L_LINK = 'ov-infra-grid-link';
const L_ARROW = 'ov-infra-grid-arrow';
const L_LINKLBL = 'ov-infra-grid-linklbl';
const L_CLBL = 'ov-infra-grid-clbl';
const L_RLBL = 'ov-infra-grid-rlbl';
const L_GLBL = 'ov-infra-grid-glbl';
const GB_BOX = [-8.6, 49.8, 2.1, 59.0];
const LINK_GOLD = '#e3cd6b';
const DRAWER_MS = 60000;

export function registerEnergy(ctx, s) {
  const { h, fmt, map2, map, ui } = ctx;
  let shapes = null;           // {countries: FC, regions: FC}
  let mapDoc = null;
  let hovered = null;
  let quickTimer = null;
  let drawer = null;
  let drawerTab = 'gb';
  let drawerTimer = null;
  let drawerQuick = null;
  let btn = null;
  let sortKey = 'renewable_now';
  let euDoc = null;
  let gbDoc = null;
  let lastStatus = ['ok', null];

  const gbNum = (v, d = 0) => (v == null ? '–' : fmt.num(v, d));
  const gw = (mw) => (mw == null ? '–' : fmt.num(mw / 1000, 1));
  const metric = () => (METRICS[layer.option('metric')] ? layer.option('metric') : 'renewables');

  // ------------------------------------------------------------------ map layer
  const spec = {
    id: 'infra.grid', group: 'Infrastructure', title: 'Electricity grids', subtitle: 'Live, NESO, Elexon and Energy-Charts',
    description: 'Renewable share now, carbon intensity or day-ahead price for 36 European countries and Great Britain\'s 14 grid regions, with Britain\'s interconnectors and their live flows. Click a country or region for details.',
    source: 'NESO Carbon Intensity API (CC BY 4.0); Elexon BMRS; Energy-Charts, Fraunhofer ISE (CC BY 4.0); Natural Earth; NESO DNO areas',
    color: '#5598e7', symbol: 'area', refreshMs: 5 * 60 * 1000,
    options: [
      { id: 'metric', label: 'Colour by', type: 'select', default: 'renewables', refresh: false, choices: [
        { value: 'renewables', label: 'Renewable share now' }, { value: 'carbon', label: 'Carbon intensity' },
        { value: 'price', label: 'Day-ahead price now' }] },
      { id: 'regions', label: 'Great Britain by region', type: 'toggle', default: true, refresh: false },
      { id: 'links', label: 'Interconnector flows', type: 'toggle', default: true, refresh: false },
      { id: 'labels', label: 'Values on the map', type: 'toggle', default: true, refresh: false },
    ],
    legend: legendFor('renewables'),
    async enable() {
      spec.legend = legendFor(metric());
      if (await s.ensureReady('energy-shapes', layer, show)) await show();
    },
    disable() {
      clearTimeout(quickTimer);
      hovered = null;
      for (const id of [SRC_T, SRC_L, SRC_R, SRC_C]) map2.removeSource(id);
    },
    async refresh({ signal }) {
      if (!map.getSource(SRC_C)) return;
      const doc = await ctx.api.get('/api/infra/energy/map', null, { signal });
      mapDoc = doc;
      apply();
      const n = Object.values(doc.countries || {}).filter(v => v.r != null || v.i != null).length;
      layer.setCount(n);
      clearTimeout(quickTimer);
      if (doc.pending || doc.gb_pending) {
        layer.setStatus('warn', doc.gb_pending ? 'Waiting for the first readings from NESO and Elexon.'
          : `Still loading ${doc.pending} European countries. Energy-Charts allows a few requests a minute, so the first full map takes about 20 minutes.`);
        lastStatus = ['warn', doc.gb_pending ? 'Waiting for the first readings from NESO and Elexon.'
          : `Still loading ${doc.pending} European countries. Energy-Charts allows a few requests a minute, so the first full map takes about 20 minutes.`];
        quickTimer = setTimeout(() => layer.refresh('quick'), doc.gb_pending ? 4000 : 45000);
      } else { lastStatus = ['ok', null]; layer.setStatus('ok'); }
    },
    onOption(id, value, all) {
      if (!map.getLayer(L_CFILL)) return;
      if (id === 'metric') {
        spec.legend = legendFor(METRICS[value] ? value : 'renewables');
        layer.setStatus(...lastStatus);   // re-renders the layer row, so the new legend shows
        for (const lid of [L_CFILL, L_RFILL]) {
          const p = fillPaint(value);
          map.setPaintProperty(lid, 'fill-color', p['fill-color']);
          map.setPaintProperty(lid, 'fill-opacity', p['fill-opacity']);
        }
      }
      if (id === 'labels') { for (const lid of [L_CLBL, L_GLBL, L_RLBL, L_LINKLBL]) map2.setVisible(lid, value && (lid !== L_LINKLBL || all.links !== false)); }
      if (id === 'links') for (const lid of [L_LINK, L_ARROW, L_LINKLBL]) map2.setVisible(lid, value && (lid !== L_LINKLBL || all.labels !== false));
      apply();
    },
  };
  const layer = ctx.layers.register(spec);

  map2.icon('infra-flow-arrow', (g, size) => {
    g.fillStyle = '#ffffff';
    g.beginPath();
    g.moveTo(size * 0.18, size * 0.2);
    g.lineTo(size * 0.86, size * 0.5);
    g.lineTo(size * 0.18, size * 0.8);
    g.lineTo(size * 0.34, size * 0.5);
    g.closePath();
    g.fill();
  }, { size: 24, sdf: true });

  async function show() {
    const [countries, regions] = await Promise.all([
      s.get('/api/infra/geo/energy-countries', null, { ttlMs: 6 * 3600 * 1000 }),
      s.get('/api/infra/geo/energy-regions', null, { ttlMs: 6 * 3600 * 1000 })]);
    shapes = { countries, regions };
    const opts = layer.options();
    const paint = fillPaint(metric());
    map2.addSource(SRC_C, { type: 'geojson', data: countries, promoteId: 'id' });
    map2.addSource(SRC_R, { type: 'geojson', data: regions, promoteId: 'id' });
    map2.addSource(SRC_L, { type: 'geojson', data: map2.emptyFC() });
    map2.addSource(SRC_T, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: L_CFILL, type: 'fill', source: SRC_C, paint }, 'polygons');
    map2.addLayer({ id: L_CLINE, type: 'line', source: SRC_C, paint: { 'line-color': '#3a5080', 'line-width': 0.6, 'line-opacity': 0.7 } }, 'polygons');
    map2.addLayer({ id: L_RFILL, type: 'fill', source: SRC_R, paint, layout: { visibility: opts.regions === false ? 'none' : 'visible' } }, 'polygons');
    map2.addLayer({ id: L_RLINE, type: 'line', source: SRC_R, layout: { visibility: opts.regions === false ? 'none' : 'visible' },
      paint: { 'line-color': '#75839f', 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.4, 8, 0.9], 'line-opacity': 0.8 } }, 'polygons');
    map2.addLayer({ id: L_HOVER, type: 'line', source: SRC_C,
      paint: { 'line-color': PAPER, 'line-width': ['case', ['boolean', ['feature-state', 'hover'], false], 1.4, 0], 'line-opacity': 0.9 } }, 'lines');
    const linkVis = opts.links === false ? 'none' : 'visible';
    const lineColor = ['match', ['get', 'dir'], 'import', LINK_GOLD, 'export', '#aab4ca', '#3a5080'];
    map2.addLayer({ id: L_LINK, type: 'line', source: SRC_L, layout: { visibility: linkVis, 'line-cap': 'round' },
      paint: { 'line-color': lineColor, 'line-opacity': 0.9,
        'line-width': ['interpolate', ['linear'], ['zoom'], 3, ['+', 0.8, ['*', ['get', 'mw'], 0.0012]], 8, ['+', 1.5, ['*', ['get', 'mw'], 0.0022]]] } }, 'lines');
    map2.addLayer({ id: L_ARROW, type: 'symbol', source: SRC_L, filter: ['!=', ['get', 'dir'], 'idle'],
      layout: { visibility: linkVis, 'symbol-placement': 'line-center', 'icon-image': 'infra-flow-arrow', 'icon-rotation-alignment': 'map',
        'icon-size': ['interpolate', ['linear'], ['zoom'], 3, 0.45, 8, 0.8], 'icon-allow-overlap': true, 'icon-ignore-placement': true },
      paint: { 'icon-color': lineColor, 'icon-halo-color': INK, 'icon-halo-width': 1 } }, 'lines');
    map2.addLayer({ id: L_LINKLBL, type: 'symbol', source: SRC_L, minzoom: 5,
      layout: { visibility: opts.links === false || opts.labels === false ? 'none' : 'visible', 'symbol-placement': 'line-center',
        'text-field': ['get', 'label'], 'text-font': ['Noto Sans Regular'], 'text-size': 10.5, 'text-offset': [0, -0.9], 'text-optional': true,
        'text-max-angle': 40, 'text-keep-upright': true },
      paint: { 'text-color': '#e7dcc0', 'text-halo-color': INK, 'text-halo-width': 1.5 } }, 'lines');
    const lblVis = opts.labels === false ? 'none' : 'visible';
    map2.addLayer({ id: L_CLBL, type: 'symbol', source: SRC_T, filter: ['==', ['get', 'k'], 'c'], minzoom: 2.6,
      layout: { visibility: lblVis, 'text-field': ['get', 't'], 'text-font': ['Open Sans Bold'], 'text-size': ['interpolate', ['linear'], ['zoom'], 3, 10, 6, 12.5],
        'text-optional': true, 'symbol-sort-key': ['get', 'o'] },
      paint: { 'text-color': PAPER, 'text-halo-color': INK, 'text-halo-width': 1.5 } }, 'top');
    map2.addLayer({ id: L_GLBL, type: 'symbol', source: SRC_T, filter: ['==', ['get', 'k'], 'g'], minzoom: 2.6, maxzoom: 4.6,
      layout: { visibility: lblVis, 'text-field': ['get', 't'], 'text-font': ['Open Sans Bold'], 'text-size': ['interpolate', ['linear'], ['zoom'], 3, 10, 6, 12.5],
        'text-optional': true },
      paint: { 'text-color': PAPER, 'text-halo-color': INK, 'text-halo-width': 1.5 } }, 'top');
    map2.addLayer({ id: L_RLBL, type: 'symbol', source: SRC_T, filter: ['==', ['get', 'k'], 'r'], minzoom: 4.6,
      layout: { visibility: lblVis, 'text-field': ['get', 't'], 'text-font': ['Open Sans Bold'], 'text-size': ['interpolate', ['linear'], ['zoom'], 5, 10, 8, 12],
        'text-optional': true },
      paint: { 'text-color': PAPER, 'text-halo-color': INK, 'text-halo-width': 1.5 } }, 'top');
    if (mapDoc) apply();
  }

  /** Push the latest values into feature-state, labels and interconnector lines. */
  function apply() {
    if (!mapDoc || !shapes || !map.getSource(SRC_C)) return;
    const m = metric();
    const key = METRICS[m].key;
    const opts = layer.options();
    const regionsOn = opts.regions !== false && m !== 'price';
    const labels = [];
    for (const f of shapes.countries.features || []) {
      const id = f.properties.id;
      const v = mapDoc.countries?.[id]?.[key];
      const hideGb = id === 'GB' && regionsOn;
      map.setFeatureState({ source: SRC_C, id }, { v: v == null || hideGb ? null : +v });
      if (v != null && f.properties.label) {   // with GB regions on, Britain's own value shows only below region zoom
        labels.push({ type: 'Feature', geometry: { type: 'Point', coordinates: f.properties.label },
          properties: { k: hideGb ? 'g' : 'c', t: METRICS[m].text(v), o: id === 'GB' ? 0 : 1 } });
      }
    }
    for (const f of shapes.regions.features || []) {
      const r = mapDoc.regions?.[String(f.properties.id)];
      const v = !regionsOn || !r ? null : (m === 'carbon' ? r.i : r.r);
      map.setFeatureState({ source: SRC_R, id: f.properties.id }, { v: v == null ? null : +v });
      if (v != null && f.properties.label) {
        labels.push({ type: 'Feature', geometry: { type: 'Point', coordinates: f.properties.label }, properties: { k: 'r', t: METRICS[m].text(v) } });
      }
    }
    map2.setVisible(L_RFILL, opts.regions !== false);
    map2.setVisible(L_RLINE, opts.regions !== false);
    map2.setData(SRC_T, { type: 'FeatureCollection', features: labels });
    const links = (mapDoc.links || []).map((l) => ({ type: 'Feature',
      geometry: { type: 'LineString', coordinates: l.direction === 'import' ? [l.far, l.gb] : [l.gb, l.far] },
      properties: { code: l.code, name: l.name, to: l.to, dir: l.direction, mw: Math.abs(l.mw), cap: l.capacity_mw,
        label: `${l.name} ${fmt.int(Math.abs(l.mw))} MW` } }));
    map2.setData(SRC_L, { type: 'FeatureCollection', features: links });
  }

  function countryHover(f) {
    const id = f.properties.id;
    if (hovered !== id && map.getSource(SRC_C)) {
      if (hovered) map.setFeatureState({ source: SRC_C, id: hovered }, { hover: false });
      map.setFeatureState({ source: SRC_C, id }, { hover: true });
      hovered = id;
    }
    const v = mapDoc?.countries?.[id];
    const name = f.properties.name || id;
    if (!v) return `${name}: loading`;
    const m = metric();
    if (m === 'carbon') return v.i == null ? `${name}: no carbon figure yet` : `${name}: ${fmt.int(v.i)} g CO2 per kWh${id === 'GB' ? '' : ', estimated'}`;
    if (m === 'price') return v.p == null ? `${name}: no day-ahead price` : `${name}: €${fmt.num(v.p, 0)} per MWh now`;
    return v.r == null ? `${name}: no renewable reading yet` : `${name}: ${fmt.num(v.r, 0)}% renewable now`;
  }

  map2.onHover([L_CFILL], countryHover);
  map2.onHover([L_RFILL], (f) => {
    const r = mapDoc?.regions?.[String(f.properties.id)];
    if (!r) return f.properties.name;
    return metric() === 'renewables' ? `${f.properties.name}: ${fmt.num(r.r, 0)}% renewable now`
      : `${f.properties.name}: ${fmt.int(r.i)} g CO2 per kWh${r.x ? `, ${r.x}` : ''}`;
  });
  map2.onHover([L_LINK], (f) => {
    const p = f.properties;
    const what = p.dir === 'import' ? 'into Britain' : p.dir === 'export' ? `to ${p.to}` : 'idle';
    return `${p.name}, Britain and ${p.to}: ${fmt.int(p.mw)} MW ${what}`;
  });
  const clearHover = () => { if (hovered && map.getSource(SRC_C)) map.setFeatureState({ source: SRC_C, id: hovered }, { hover: false }); hovered = null; };
  ctx.map.on('mouseout', clearHover);
  ctx.map.on('mousemove', ctx.dom.throttle((e) => {
    // the hover callback only runs over a country, so clear the outline once the pointer reaches the sea
    if (hovered && map.getLayer(L_CFILL) && !map.queryRenderedFeatures(e.point, { layers: [L_CFILL] }).length) clearHover();
  }, 80));
  map2.onClick([L_RFILL], (f) => openRegion(+f.properties.id));
  map2.onClick([L_LINK], (f) => openLink(f.properties.code));
  map2.onClick([L_CFILL], (f) => (f.properties.id === 'GB' ? openDrawer('gb') : openCountry(f.properties.id, f.properties.name)));

  function showOnMap(bbox) {
    if (!layer.isOn()) ctx.layers.setOn('infra.grid', true);
    if (bbox) map2.fitBbox(bbox, { maxZoom: 6 });
  }

  // ------------------------------------------------------------------ the drawer
  function statTiles(items) {
    return h('div', { class: 'infra-stats infra-en-stats' }, items.map(([label, value, unit, note]) => h('div', null,
      h('b', null, value, unit ? h('small', null, ` ${unit}`) : null), h('span', null, label),
      note ? h('span', { class: 'infra-en-note' }, note) : null)));
  }

  function gbBody(doc) {
    const it = doc.intensity || {};
    const cur = it.now || {};
    const mix = doc.mix || {};
    const price = doc.price;
    const dem = doc.demand;
    const box = h('div', { class: 'infra-en' });
    if (doc.pending) {
      box.append(h('p', { class: 'dim infra-en-pad' }, doc.errors?.neso || doc.errors?.elexon || 'Fetching the first readings from NESO and Elexon…'));
      return box;
    }
    const badge = indexBadge(cur.index);
    box.append(statTiles([
      ['Carbon intensity', cur.value != null ? fmt.int(cur.value) : '–', 'g/kWh',
        badge ? h('span', { class: `badge ${badge.tone}`, title: 'NESO carbon intensity index' }, badge.text.replace(' carbon', '')) : null],
      ['Renewables', mix.renewable_pct != null ? fmt.num(mix.renewable_pct, 0) : '–', '%', mix.low_carbon_pct != null ? `${fmt.num(mix.low_carbon_pct, 0)}% low carbon` : null],
      ['Demand', gw(dem?.mw), 'GW', dem?.peak ? `peak ${gw(dem.peak.mw)} GW today` : null],
      ['Price', price ? `£${fmt.num(price.gbp_mwh, 0)}` : '–', '/MWh', price?.eur_mwh != null ? `about €${fmt.num(price.eur_mwh, 0)}` : null],
    ]));
    const win = it.windows || {};
    const sp = it.spark || {};
    box.append(h('section', { class: 'infra-en-sec' },
      h('h3', null, 'Carbon intensity, last 24 hours and the next 24'),
      sparkline(ctx, { past: sp.past || [], future: sp.future || [], color: METRICS.carbon.colors[2], unit: 'g', label: 'Carbon intensity, past 24 hours measured and next 24 hours forecast' }),
      h('p', { class: 'infra-en-cap' }, win.cleanest
        ? `Cleanest 3 hours ahead: ${ukTime(win.cleanest.from)} to ${ukTime(win.cleanest.to)} UK time, about ${fmt.int(win.cleanest.avg)} g. Dirtiest: ${ukTime(win.dirtiest.from)} to ${ukTime(win.dirtiest.to)}, about ${fmt.int(win.dirtiest.avg)} g.`
        : 'The forecast is not available right now.',
      cur.last_actual != null ? ` Last measured ${fmt.int(cur.last_actual)} g for the half hour from ${ukTime(cur.last_actual_t)}.` : '')));
    const fuels = (mix.fuels || []).filter(f => f.pct >= 0.5);
    const small = (mix.fuels || []).filter(f => f.pct < 0.5).map(f => f.label.toLowerCase());
    box.append(h('section', { class: 'infra-en-sec' },
      h('h3', null, 'Where the power comes from'),
      barList(ctx, fuels.map(f => ({ label: f.label, value: f.pct, text: `${fmt.num(f.pct, 1)}%` })), { max: Math.max(50, ...fuels.map(f => f.pct)) }),
      h('p', { class: 'infra-en-cap' }, small.length ? `Under 0.5%: ${small.join(', ')}. ` : '',
        'Shares of generation for the half hour from ', ukTime(mix.t), ', including NESO\'s estimate of rooftop solar and small wind.')));
    const flows = doc.flows;
    box.append(h('section', { class: 'infra-en-sec' },
      h('h3', null, 'Interconnectors'),
      flowList(ctx, doc.interconnectors || []),
      flows ? h('p', { class: 'infra-en-cap' }, flows.net_import_mw >= 0
        ? `Net imports ${gw(flows.net_import_mw)} GW${dem?.mw ? `, about ${fmt.num(flows.net_import_mw / dem.mw * 100, 0)}% of demand` : ''}. Bars to the right are power coming into Britain.`
        : `Net exports ${gw(-flows.net_import_mw)} GW. Bars to the left are power leaving Britain.`) : null));
    const regions = [...(doc.regions || [])].sort((a, b) => (a.intensity ?? 1e9) - (b.intensity ?? 1e9));
    const showBtn = h('button', { type: 'button', class: 'text-btn infra-en-link' }, 'Show the regions on the map');
    showBtn.addEventListener('click', () => showOnMap(GB_BOX));
    box.append(h('section', { class: 'infra-en-sec' },
      h('h3', null, 'Regions, cleanest first'),
      barList(ctx, regions.map(r => ({ label: r.name, value: r.intensity ?? 0, text: `${fmt.int(r.intensity)} g`, note: r.index || '' })),
        { max: Math.max(300, ...regions.map(r => r.intensity || 0)) }),
      h('p', { class: 'infra-en-cap' }, 'NESO\'s regional forecast for this half hour, in grams of CO2 per kWh. ', showBtn)));
    const t = doc.generation?.t;
    box.append(h('div', { class: 'infra-en-foot' },
      h('p', null, `Times are UK time. Generation and flows for ${ukTime(t)}, demand for the half hour from ${ukTime(dem?.t)}, price for the half hour from ${ukTime(price?.t)}.`),
      (doc.attribution || []).map(a => h('p', null, a)),
      h('p', null, h('a', { href: doc.links?.neso }, 'carbonintensity.org.uk'), ', ', h('a', { href: doc.links?.elexon }, 'Elexon BMRS'), ', ',
        h('a', { href: doc.links?.elexon_licence }, 'BMRS data licence')),
      Object.values(doc.errors || {}).map(e => h('p', { class: 'infra-warn' }, e))));
    return box;
  }

  const SORTS = {
    name: { label: 'Country', cmp: (a, b) => a.name.localeCompare(b.name) },
    renewable_now: { label: 'Renewables', cmp: (a, b) => (b.renewable_now ?? -1) - (a.renewable_now ?? -1) },
    intensity: { label: 'Carbon', cmp: (a, b) => (a.intensity ?? 1e9) - (b.intensity ?? 1e9) },
    price: { label: 'Price', cmp: (a, b) => (a.price ?? 1e9) - (b.price ?? 1e9) },
  };

  function euBody(doc) {
    const box = h('div', { class: 'infra-en' });
    const rows = [...(doc.countries || [])].filter(r => !r.no_data).sort(SORTS[sortKey].cmp);
    const head = h('tr', null, Object.entries(SORTS).map(([k, v]) => {
      const b = h('button', { type: 'button', class: 'text-btn infra-en-sort', 'aria-pressed': String(k === sortKey) }, v.label);
      b.addEventListener('click', () => { sortKey = k; renderDrawer(); });
      return h('th', { class: k === 'name' ? '' : 'num', 'aria-sort': k === sortKey ? (k === 'renewable_now' ? 'descending' : 'ascending') : null }, b);
    }));
    const body = rows.map((r) => {
      const name = h('button', { type: 'button', class: 'text-btn infra-en-name' }, r.name);
      name.addEventListener('click', () => (r.country === 'GB' ? switchTab('gb') : openCountry(r.country, r.name)));
      const cell = (v, text, title) => h('td', { class: 'num', title: title || null }, v == null ? (r.pending ? '…' : '–') : text);
      return h('tr', { class: r.country === 'GB' ? 'infra-en-gb' : '' },
        h('td', null, name),
        cell(r.renewable_now, `${fmt.num(r.renewable_now, 0)}%${r.renewable_kind === 'latest' ? '†' : ''}`,
          r.renewable_t ? `Renewable share of electricity use, ${r.renewable_kind === 'latest' ? 'latest reported, ' : ''}${utcTime(r.renewable_t)}` : null),
        cell(r.intensity, `${fmt.int(r.intensity)}${r.intensity_kind === 'estimate' ? '*' : ''}`, r.intensity_t ? `${r.intensity_kind === 'estimate' ? 'Estimated from the mix at' : 'NESO, half hour from'} ${utcTime(r.intensity_t)}` : null),
        cell(r.price, `€${fmt.num(r.price, 0)}`, r.price_zone ? `Day-ahead price, zone ${r.price_zone}${r.price_gbp != null ? ` (£${fmt.num(r.price_gbp, 0)})` : ''}` : null));
    });
    if (doc.pending) {
      box.append(h('p', { class: 'infra-en-cap infra-en-pad' }, `Filling in: ${doc.total - 1 - doc.pending} of ${doc.total - 1} countries so far. Energy-Charts allows a few requests a minute, so the first full table takes about 20 minutes; it then stays up to date while you use it.`));
    }
    box.append(h('table', { class: 'tbl infra-en-tbl' }, h('thead', null, head), h('tbody', null, body)));
    box.append(h('div', { class: 'infra-en-foot' },
      h('p', null, 'Renewables: share of electricity use now (Energy-Charts estimate); for Great Britain, NESO\'s generation mix. Carbon in g CO2 per kWh. Price: day-ahead €/MWh now; Great Britain\'s is Elexon\'s market index price converted from pounds.'),
      h('p', null, '* Estimated from the latest generation mix, often 2 to 5 hours old, with NESO\'s emission factors (direct CO2, imports not counted).'),
      rows.some(r => r.renewable_kind === 'latest') ? h('p', null, '† No live estimate for this country, or one that does not match its reported generation, as with Switzerland at times: the share from its latest reported generation mix.') : null,
      (doc.attribution || []).slice(0, 1).map(a => h('p', null, a)),
      h('p', null, h('a', { href: doc.links?.energy_charts }, 'energy-charts.info'))));
    return box;
  }

  function renderDrawer() {
    if (!drawer) return;
    const body = drawer.body.firstElementChild;
    if (!body) return;
    const doc = drawerTab === 'gb' ? gbDoc : euDoc;
    ctx.dom.clear(body);
    if (!doc) { body.append(h('p', { class: 'dim infra-en-pad' }, 'Loading…')); return; }
    body.append(drawerTab === 'gb' ? gbBody(doc) : euBody(doc));
  }

  async function loadDrawer(quickTries = 0) {
    if (!drawer) return;
    clearTimeout(drawerQuick);
    try {
      if (drawerTab === 'gb') gbDoc = await ctx.api.get('/api/infra/energy/gb');
      else euDoc = await ctx.api.get('/api/infra/energy/europe');
    } catch (err) {
      const body = drawer?.body.firstElementChild;
      if (body && !(drawerTab === 'gb' ? gbDoc : euDoc)) { ctx.dom.clear(body); body.append(h('p', { class: 'empty infra-en-pad' }, 'Grid data is unavailable right now.')); }
      return;
    }
    renderDrawer();
    const doc = drawerTab === 'gb' ? gbDoc : euDoc;
    const waiting = drawerTab === 'gb' ? (doc.pending || doc.refreshing || !doc.price) : doc.pending > 0;
    if (waiting && quickTries < 10) drawerQuick = setTimeout(() => loadDrawer(quickTries + 1), drawerTab === 'gb' ? 3000 : 20000);
  }

  let tabButtons = {};
  function switchTab(tab) {
    if (!drawer) { openDrawer(tab); return; }
    drawerTab = tab;
    for (const [k, b] of Object.entries(tabButtons)) b.setAttribute('aria-pressed', String(k === tab));
    renderDrawer();
    loadDrawer();
  }

  function openDrawer(tab = null) {
    if (drawer) { if (tab) switchTab(tab); else drawer.close(); return; }
    if (tab) drawerTab = tab;
    const seg = h('div', { class: 'seg infra-en-seg', role: 'group', 'aria-label': 'Area' });
    tabButtons = {};
    for (const [k, label] of [['gb', 'Great Britain'], ['eu', 'Europe']]) {
      const b = h('button', { type: 'button', 'aria-pressed': String(k === drawerTab) }, label);
      b.addEventListener('click', () => switchTab(k));
      tabButtons[k] = b;
      seg.append(b);
    }
    const refresh = h('button', { class: 'icon-btn', type: 'button', title: 'Refresh', 'aria-label': 'Refresh' },
      ctx.dom.svgIcon('M15.5 8.5A6 6 0 1 0 16 11M16 4v4.5h-4.5'));
    refresh.addEventListener('click', () => loadDrawer());
    const body = h('div', { class: 'infra-en-body' }, h('div', null, h('p', { class: 'dim infra-en-pad' }, 'Loading…')));
    drawer = ui.drawer({ id: 'infra-energy', title: 'Electricity', el: body, width: 430, tools: [seg, refresh], onClose() {
      clearInterval(drawerTimer); clearTimeout(drawerQuick); drawerTimer = null; drawer = null; btn?.setAttribute('aria-pressed', 'false');
    } });
    btn?.setAttribute('aria-pressed', 'true');
    loadDrawer();
    drawerTimer = setInterval(() => { if (document.visibilityState === 'visible') loadDrawer(); }, DRAWER_MS);
  }

  btn = ui.toolbarButton({ id: 'infra-energy', title: 'Electricity grids now', label: 'Energy', icon: ICON, onClick: () => openDrawer(), order: 41 });

  // ------------------------------------------------------------------ cards
  function seriesSplit(series, now = Date.now() / 1000) {
    const past = [];
    const future = [];
    for (const [t, v] of series || []) (t <= now ? past : future).push([t, v]);
    return { past, future };
  }

  function countrySpec(d) {
    const pw = d.power || {};
    const pr = d.prices || {};
    const sig = d.signal || {};
    const mix = (pw.mix || []).filter(g => g.pct >= 0.5);
    const shape = shapes?.countries?.features?.find(f => f.properties.id === d.country);
    const bbox = shape ? geomBbox(shape.geometry) : null;
    const sigSplit = seriesSplit(sig.series);
    const priceSplit = seriesSplit(pr.series);
    const first = (sig.series || [])[0]?.[0];
    const last = (sig.series || []).slice(-1)[0]?.[0];
    const sections = [];
    if (sig.series?.length > 1) {
      sections.push({ title: 'Renewable share of electricity use today', el: h('div', null,
        sparkline(ctx, { ...sigSplit, color: METRICS.renewables.colors[2], unit: '%', max: Math.max(50, ...sig.series.map(p => p[1] * 1.15)),
          time: (t) => utcTime(t), label: 'Renewable share today, later hours forecast',
          xLabels: [[utcTime(first), 0, 'start'], ['Now', 'now', 'middle'], [utcTime(last), 400, 'end']] }),
        h('p', { class: 'infra-en-cap' }, 'Energy-Charts\' estimate; the dashed part is its forecast.',
          d.renewable_note === 'disagrees' ? ` It does not match the reported generation mix (${fmt.num(pw.renewable_share_load, 0)}% at ${utcTime(pw.t)}), so the reading above uses the mix.` : '')) });
    }
    if (pr.series?.length > 1) {
      sections.push({ title: `Day-ahead price today${d.zone && d.zone !== d.country ? `, ${d.zone_name || d.zone}` : ''}`, el: h('div', null,
        sparkline(ctx, { ...priceSplit, color: METRICS.price.colors[2], unit: '€/MWh', time: (t) => utcTime(t), label: 'Day-ahead price today',
          xLabels: [[utcTime(pr.series[0][0]), 0, 'start'], ['Now', 'now', 'middle'], [utcTime(pr.series[pr.series.length - 1][0]), 400, 'end']] }),
        h('p', { class: 'infra-en-cap' }, `Average €${fmt.num(pr.avg, 0)}, lowest €${fmt.num(pr.min, 0)}, highest €${fmt.num(pr.max, 0)} per MWh.`)) });
    }
    if (mix.length) {
      sections.push({ title: `Generation mix at ${utcTime(pw.t)}`, el: h('div', null,
        barList(ctx, mix.map(g => ({ label: g.label, value: g.pct, text: `${fmt.num(g.pct, 0)}%`, note: `${gw(g.mw)} GW` })), { max: Math.max(50, ...mix.map(g => g.pct)) }),
        h('p', { class: 'infra-en-cap' }, `Fossil ${fmt.num(pw.fossil_pct, 0)}%, low carbon (renewables and nuclear) ${fmt.num(pw.low_carbon_pct, 0)}%. `,
          pw.intensity_est != null ? `About ${fmt.int(pw.intensity_est)} g CO2 per kWh, estimated from this mix with NESO's emission factors.` : '')) });
    }
    if (!sections.length) sections.push({ text: d.no_data ? 'Energy-Charts has no recent figures for this country.' : 'Loading the first figures. Energy-Charts allows few requests a minute, so this can take a minute or two.' });
    return {
      key: `energy-country:${d.country}`, kind: 'energy_grid', title: d.name, subtitle: 'Electricity grid now, from Energy-Charts',
      icon: 'ϟ', accent: INFRA,
      badges: [d.pending ? { text: 'Still loading', tone: 'grey' } : null, d.no_data ? { text: 'No recent data', tone: 'grey' } : null].filter(Boolean),
      block: [
        { label: d.renewable_kind === 'latest' ? `Renewables at ${utcTime(d.renewable_t).replace(' UTC', '')}` : 'Renewables now',
          value: d.renewable_now != null ? fmt.num(d.renewable_now, 0) : '–', unit: '%' },
        { label: 'Carbon, estimated', value: d.intensity != null ? fmt.int(d.intensity) : '–', unit: 'g/kWh' },
        { label: 'Price now', value: d.price != null ? `€${fmt.num(d.price, 0)}` : '–', unit: '/MWh' },
        { label: 'Load', value: gw(d.load_mw), unit: 'GW' },
      ],
      sections,
      links: [{ text: 'Energy-Charts for this country', href: d.page }],
      actions: [bbox ? { text: 'Show on the map', onClick: () => showOnMap(bbox) } : null,
        ctx.moduleOk('intel') ? { text: 'Country profile', onClick: () => ctx.entities.open({ kind: 'country', id: d.country, title: d.name }) } : null].filter(Boolean),
      foot: 'Energy-Charts.info, Fraunhofer ISE (CC BY 4.0). Day-ahead prices: Bundesnetzagentur | SMARD.de (CC BY 4.0).',
    };
  }

  async function openCountry(iso, title = null) {
    const key = `energy-country:${iso}`;
    const card = ctx.cards.open({ key, kind: 'energy_grid', title: title || iso, subtitle: 'Loading the grid…', icon: 'ϟ', accent: INFRA });
    let tries = 0;
    const load = async () => {
      try {
        const d = await ctx.api.get(`/api/infra/energy/country/${encodeURIComponent(iso)}`);
        if (d.gb) { card.close(); openDrawer('gb'); return null; }
        if (!card.isOpen()) return null;
        card.update({ ...countrySpec(d), live: { everyMs: 5 * 60 * 1000, update: async () => countrySpec(await ctx.api.get(`/api/infra/energy/country/${encodeURIComponent(iso)}`)) } });
        if (d.pending && tries < 6) { tries += 1; setTimeout(load, 20000); }
        return d;
      } catch (err) {
        if (card.isOpen()) card.update({ subtitle: 'Electricity grid', sections: [{ text: err?.status === 404 ? 'No live grid data for this country.' : 'The grid figures could not be loaded right now.' }] });
        return null;
      }
    };
    await load();
    return card;
  }

  function regionSpec(d) {
    const cur = d.now || {};
    const fc = d.forecast?.series || [];
    const win = d.windows || {};
    const mix = (cur.mix || []).filter(g => g.pct >= 0.5);
    const badge = indexBadge(cur.index);
    const name = cur.name || d.shape?.name || `Region ${d.id}`;
    return {
      key: `energy-region:${d.id}`, kind: 'energy_region', title: d.shape?.name || name,
      subtitle: d.shape?.dno_full ? `Grid region, ${d.shape.dno_full}` : 'Great Britain grid region',
      icon: 'ϟ', accent: INFRA, badges: badge ? [badge] : [],
      block: [
        { label: 'Carbon intensity', value: cur.intensity != null ? fmt.int(cur.intensity) : '–', unit: 'g/kWh' },
        { label: 'Renewables', value: cur.renewable_pct != null ? fmt.num(cur.renewable_pct, 0) : '–', unit: '%' },
        { label: 'Low carbon', value: cur.low_carbon_pct != null ? fmt.num(cur.low_carbon_pct, 0) : '–', unit: '%' },
      ],
      sections: [
        fc.length > 1 ? { title: 'Next 24 hours, forecast', el: h('div', null,
          sparkline(ctx, { past: [], future: fc.map(r => [r.t, r.forecast]).filter(p => p[1] != null), color: METRICS.carbon.colors[2], unit: 'g',
            label: 'Regional carbon intensity forecast, next 24 hours', xLabels: [['Now', 0, 'start'], ['In 24 h', 400, 'end']] }),
          win.cleanest ? h('p', { class: 'infra-en-cap' }, `Cleanest 3 hours: ${ukTime(win.cleanest.from)} to ${ukTime(win.cleanest.to)} UK time, about ${fmt.int(win.cleanest.avg)} g.`) : null) }
          : { text: d.forecast_error || 'Loading the forecast…' },
        mix.length ? { title: 'Generation mix in this region', el: barList(ctx, mix.map(g => ({ label: g.label, value: g.pct, text: `${fmt.num(g.pct, 0)}%` })), { max: Math.max(50, ...mix.map(g => g.pct)) }) } : null,
        { text: 'NESO\'s regional estimate counts the power generated in the region and the power flowing in from elsewhere.' },
      ],
      actions: [{ text: 'Great Britain now', onClick: () => openDrawer('gb') }],
      foot: 'NESO Carbon Intensity API, regional forecast (CC BY 4.0). Supported by National Energy SO Open Data. Outlines: NESO DNO licence areas.',
    };
  }

  async function openRegion(id) {
    const card = ctx.cards.open({ key: `energy-region:${id}`, kind: 'energy_region', title: `Region ${id}`, subtitle: 'Loading…', icon: 'ϟ', accent: INFRA });
    try {
      const d = await ctx.api.get(`/api/infra/energy/gb/region/${encodeURIComponent(id)}`);
      if (card.isOpen()) card.update({ ...regionSpec(d), live: { everyMs: 10 * 60 * 1000, update: async () => regionSpec(await ctx.api.get(`/api/infra/energy/gb/region/${encodeURIComponent(id)}`)) } });
    } catch {
      if (card.isOpen()) card.update({ subtitle: 'Great Britain grid region', sections: [{ text: 'The regional figures could not be loaded right now.' }] });
    }
    return card;
  }

  function openLink(code) {
    const l = (mapDoc?.links || []).find(x => x.code === code);
    if (!l) return null;
    const used = l.capacity_mw ? Math.abs(l.mw) / l.capacity_mw * 100 : null;
    return ctx.cards.open({
      key: `energy-link:${code}`, kind: 'energy_link', title: l.name, subtitle: `Interconnector, Great Britain and ${l.to}`,
      icon: '⇄', accent: INFRA,
      badges: [{ text: l.direction === 'import' ? 'Importing to Britain' : l.direction === 'export' ? `Exporting to ${l.to}` : 'Idle', tone: 'grey' }],
      block: [{ label: 'Flow now', value: fmt.int(Math.abs(l.mw)), unit: 'MW' }, { label: 'Capacity', value: fmt.int(l.capacity_mw), unit: 'MW' },
        { label: 'In use', value: used != null ? fmt.num(Math.min(used, 100), 0) : '–', unit: '%' }],
      sections: [{ text: 'The line joins the two converter stations (positions approximate); it is not the cable route.' }],
      actions: [{ text: 'All interconnectors', onClick: () => openDrawer('gb') }],
      foot: 'Elexon FUELINST, 5-minute readings. Contains BMRS data © Elexon Limited copyright and database right.',
    });
  }

  function geomBbox(g) {
    let w = 180, so = 90, e = -180, n = -90;
    const walk = (c) => { if (typeof c[0] === 'number') { w = Math.min(w, c[0]); e = Math.max(e, c[0]); so = Math.min(so, c[1]); n = Math.max(n, c[1]); } else c.forEach(walk); };
    walk(g.coordinates);
    return w <= e ? [w, so, e, n] : null;
  }

  // ------------------------------------------------------------------ What's here?
  ctx.whatshere.section(async (lat, lon) => {
    let r;
    try { r = await ctx.api.get('/api/infra/energy/at', { lat: lat.toFixed(4), lon: lon.toFixed(4) }); } catch { return null; }
    const c = r.country;
    const g = r.region;
    if (!c || (c.renewable_now == null && c.intensity == null && !g)) return null;
    const open = h('button', { type: 'button', class: 'btn' }, g ? `${g.name} grid` : `${c.name} grid`);
    open.addEventListener('click', () => (g ? openRegion(g.id) : c.country === 'GB' ? openDrawer('gb') : openCountry(c.country, c.name)));
    return {
      title: 'Electricity now',
      rows: [
        ['Grid', c.name],
        g ? ['Region', `${g.name}, ${g.intensity != null ? `${fmt.int(g.intensity)} g CO2 per kWh` : 'no reading'}${g.index ? `, ${g.index}` : ''}`] : null,
        ['Carbon intensity', c.intensity != null ? `${fmt.int(c.intensity)} g CO2 per kWh${c.intensity_kind === 'estimate' ? ', estimated' : ''}` : null],
        ['Renewables', c.renewable_now != null ? `${fmt.num(c.renewable_now, 0)}%` : null],
        ['Price', c.price != null ? `€${fmt.num(c.price, 0)} per MWh${c.price_gbp != null ? ` (£${fmt.num(c.price_gbp, 0)})` : ''}` : null],
      ].filter(Boolean),
      el: h('div', { class: 'infra-en-wh' }, open),
    };
  });

  return { openEnergy: openDrawer, openGridCountry: openCountry, openGridRegion: openRegion, energyLayer: layer, GOLD };
}
