// Conflict events (GDELT, machine-coded): map layer, card and entity handler.

import { CONFLICT, INK, SEV_RAMP, sevColour, sevBadge, sevLegend, bboxParam, safeGet, errorText } from './common.js';

const SRC = 'ov-intel-conflict';
const L_RING = 'ov-intel-conflict-ring';
const L_DOT = 'ov-intel-conflict-dot';
const L_LABEL = 'ov-intel-conflict-label';

export function registerConflict(ctx) {
  const { map2, h, fmt } = ctx;
  let layer = null;

  // zoom must be the top-level interpolate input, so offsets are baked into the stops
  const radiusExpr = (add = 0) => ['interpolate', ['linear'], ['zoom'],
    2, ['interpolate', ['linear'], ['get', 's'], 0, 1.6 + add, 5, 2.6 + add, 8, 4.2 + add, 10, 5.5 + add],
    6, ['interpolate', ['linear'], ['get', 's'], 0, 2.6 + add, 5, 4 + add, 8, 6.5 + add, 10, 8.5 + add],
    10, ['interpolate', ['linear'], ['get', 's'], 0, 4 + add, 5, 6 + add, 8, 9.5 + add, 10, 12 + add]];
  const radius = radiusExpr(0);

  function addLayers() {
    map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: L_RING, type: 'circle', source: SRC, filter: ['>=', ['get', 's'], 7.5],
      paint: { 'circle-radius': radiusExpr(4), 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-width': 1.2,
        'circle-stroke-color': sevColour(), 'circle-stroke-opacity': 0.55, 'circle-pitch-alignment': 'map' } }, 'points');
    map2.addLayer({ id: L_DOT, type: 'circle', source: SRC,
      layout: { 'circle-sort-key': ['get', 's'] },
      paint: {
        'circle-radius': radius,
        'circle-color': ['case', ['==', ['get', 'k'], 1], '#8b97b0', sevColour()],
        'circle-opacity': ['case', ['==', ['get', 'p'], 1], 0.9, 0.0],
        'circle-stroke-color': ['case', ['==', ['get', 'p'], 1], INK, ['case', ['==', ['get', 'k'], 1], '#8b97b0', sevColour()]],
        'circle-stroke-width': ['case', ['==', ['get', 'p'], 1], 0.8, 1.6],
        'circle-pitch-alignment': 'map',
      } }, 'points');
    map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC, minzoom: 8, filter: ['>=', ['get', 's'], 5],
      layout: { 'text-field': ['get', 'l'], 'text-size': 11, 'text-font': ['Open Sans Regular'], 'text-offset': [0, 1.1],
        'text-anchor': 'top', 'text-optional': true, 'symbol-sort-key': ['-', 10, ['get', 's']] },
      paint: { 'text-color': '#f2c4bf', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
  }

  layer = ctx.layers.register({
    id: 'intel.conflict', group: 'Conflict', title: 'Conflict events', subtitle: 'GDELT, machine-coded from news reports',
    description: 'Assaults, fighting and attacks reported in the news, deduplicated by place and day. Machine-coded, not verified on the ground.',
    source: 'GDELT 2.0 Events (open data, credit the GDELT Project)', color: CONFLICT, symbol: 'point',
    defaultOn: false, refreshMs: 5 * 60 * 1000, viewDependent: true,
    options: [
      { id: 'days', label: 'Period', type: 'select', default: '3',
        choices: [{ value: '1', label: 'Last 24 hours' }, { value: '3', label: 'Last 3 days' }, { value: '7', label: 'Last 7 days' }, { value: '14', label: 'Last 14 days' }] },
      { id: 'minSev', label: 'Lowest severity', type: 'range', min: 0, max: 9, step: 1, default: 3, format: v => (v ? `${v}+` : 'any') },
      { id: 'crime', label: 'Include crime and policing reports', type: 'toggle', default: false },
    ],
    legend: [...sevLegend(SEV_RAMP), { color: 'transparent', label: 'Province-level place (hollow)' }, { color: '#8b97b0', label: 'Crime or policing (if shown)' }],
    enable() { addLayers(); },
    disable() { map2.removeSource(SRC); },
    async refresh({ bounds, zoom, signal, options }) {
      const limit = zoom < 3 ? 2500 : zoom < 5 ? 4000 : 6000;
      const fc = await ctx.api.get('/api/intel/conflict', { bbox: bboxParam(bounds), days: options.days, min_severity: options.minSev,
        crime: options.crime ? 1 : 0, limit }, { signal });
      map2.setData(SRC, fc);
      layer.setCount(fc.features.length);
      const meta = fc.meta || {};
      if (meta.error && !fc.features.length) layer.setStatus('error', `GDELT is not answering: ${meta.error}`);
      else if (!fc.features.length && meta.pending_files) layer.setStatus('warn', `Fetching GDELT history: ${meta.pending_files} files to go.`);
      else if (!fc.features.length) layer.setStatus('ok', 'No conflict events match these settings in this area.');
      else if (meta.limited) layer.setStatus('ok', `Showing the ${fmt.int(fc.features.length)} most severe events here. Zoom in for more.`);
      else layer.setStatus('ok', meta.pending_files ? `Still fetching history: ${meta.pending_files} files to go.` : null);
    },
  });

  map2.onClick([L_DOT, L_RING], (f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    openConflict(ctx, { kind: 'conflict_event', id: p.id, title: `${p.l} in ${p.n}`, lat, lon, layer: 'intel.conflict' });
  });
  map2.onHover([L_DOT, L_RING], (f) => {
    const p = f.properties;
    return `${p.l}, ${p.n}, ${fmt.ago(p.t)}, severity ${(+p.s).toFixed(1)}`;
  });
  ctx.entities.handler('conflict_event', (e) => openConflict(ctx, e), { group: 'Conflict', icon: '✸' });
  return layer;
}

export async function openConflict(ctx, entity) {
  const { h, fmt } = ctx;
  const key = `conflict_event:${entity.id}`;
  const ent = ctx.dom.validLatLon(entity.lat, entity.lon)
    ? { kind: 'conflict_event', id: entity.id, title: entity.title || 'Conflict event', subtitle: entity.subtitle || '', lat: +entity.lat, lon: +entity.lon, layer: 'intel.conflict' }
    : null;
  const card = ctx.cards.open({ key, kind: 'conflict_event', title: entity.title || 'Conflict event', subtitle: 'Loading the report…',
    icon: '✸', accent: CONFLICT, entity: ent });
  const d = await safeGet(ctx, `/api/intel/conflict/${encodeURIComponent(entity.id)}`);
  if (d.__error) {
    card.update({ subtitle: entity.subtitle || 'Conflict event',
      sections: [{ text: d.__error.status === 404 ? 'This event has left the 14-day window, so its details are gone. Saved items keep their title and position.' : errorText(d.__error) }] });
    return card;
  }
  const entityFull = { kind: 'conflict_event', id: d.id, title: d.title, subtitle: d.place, lat: d.lat, lon: d.lon, layer: 'intel.conflict' };
  const badges = [sevBadge(d.severity), { text: d.category_label, tone: 'grey' }];
  if (d.precision !== 'city') badges.push({ text: 'Province-level place', tone: 'amber', title: 'GDELT only knew the province' });
  if (d.crime) badges.push({ text: 'Crime or policing', tone: 'grey' });
  if (d.focus != null && d.focus < 0.5) badges.push({ text: 'Busy dateline place', tone: 'grey', title: 'Most reports from this place are about other topics; severity is damped.' });
  const sources = (d.sources_list || []).map(s => h('li', null, h('a', { href: s.url }, s.title || s.domain), s.title ? h('span', { class: 'li-s' }, s.domain) : null));
  const actions = [];
  if (ctx.moduleOk('space')) {
    actions.push({ text: 'Compare imagery', onClick: () => {
      const after = new Date(d.t_last * 1000);
      const before = new Date(after.getTime() - 30 * 86400 * 1000);
      ctx.bus.emit('imagery:compare', { lat: d.lat, lon: d.lon, zoom: 13, before: before.toISOString().slice(0, 10), after: after.toISOString().slice(0, 10) });
    } });
  }
  card.update({
    title: d.title, subtitle: d.place_display || d.place,
    badges,
    block: [{ label: 'Severity', value: d.severity.toFixed(1), unit: '/10' }, { label: 'Mentions', value: fmt.int(d.mentions) },
      { label: 'Sources', value: fmt.int(d.sources) }, { label: 'Reports merged', value: fmt.int(d.reports) }],
    sections: [
      { title: 'What was reported', rows: [
        ['Action', d.action], ['Type', d.category_label],
        ['Actors', [d.actor1, d.actor2].filter(Boolean).join(' and ') || 'Not identified'],
        ['Also named', (d.actors || []).filter(a => a !== d.actor1 && a !== d.actor2).join(', ') || null],
        ['Place (GDELT)', d.place], ['Country', d.country], ['Event date', d.date],
        ['First reported', fmt.utc(d.t_first)], ['Last reported', `${fmt.utc(d.t_last)} (${fmt.ago(d.t_last)})`],
        ['Goldstein scale', `${(+d.goldstein).toFixed(1)} (from -10 to +10)`], ['Average tone', (+d.tone).toFixed(1)],
        ['Theatres', (d.theatres || []).length ? d.theatres.join(', ') : null],
      ] },
      sources.length ? { title: 'Source articles', el: h('ul', { class: 'card-list intel-links' }, sources) } : null,
      { title: 'About this data', text: 'Machine-coded from news reports, not verified. GDELT reads news worldwide and codes who did what to whom, where. Several reports of the same kind at the same place on the same day are merged here.' },
      { title: 'Nearby', nearby: { lat: d.lat, lon: d.lon, radius_km: 30, exclude: entityFull } },
    ],
    links: (d.main_url || d.urls?.[0]) ? [{ text: 'Open the main source article', href: d.main_url || d.urls[0] }] : [],
    actions, entity: entityFull,
    foot: `GDELT 2.0 Events. Severity ${d.severity.toFixed(1)} combines the CAMEO event type, Goldstein scale, mentions, sources and tone.`,
  });
  return card;
}
