// Protests (GDELT CAMEO root 14, machine-coded): map layer of diamonds coloured by kind, the protest card,
// and the country-card section.

import { CONFLICT, INK, PROTEST_RAMP, PROTEST_COLOUR, sevBadge, bboxParam, safeGet, errorText, short } from './common.js';

const SRC = 'ov-intel-protests';
const L_RING = 'ov-intel-protests-ring';
const L_ICON = 'ov-intel-protests-icon';
const L_LABEL = 'ov-intel-protests-label';
const ICON = 'intel-protest';
const KIND_COLOUR = ['match', ['get', 'c'], 'r', PROTEST_RAMP[2], 'b', PROTEST_RAMP[1], 's', PROTEST_RAMP[1], PROTEST_RAMP[0]];
const KIND_NAMES = { r: 'Riot or violent protest', b: 'Blockade', s: 'Strike or boycott', d: 'Demonstration' };

function drawDiamond(g, s) {
  const c = s / 2;
  const r = s / 2 - 2;
  g.fillStyle = '#fff';
  g.beginPath();
  g.moveTo(c, c - r);
  g.lineTo(c + r, c);
  g.lineTo(c, c + r);
  g.lineTo(c - r, c);
  g.closePath();
  g.fill();
}

export function registerProtests(ctx) {
  const { map2, fmt } = ctx;
  map2.icon(ICON, drawDiamond, { size: 20, sdf: true });
  const size = ['interpolate', ['linear'], ['zoom'], 2, ['interpolate', ['linear'], ['get', 's'], 0, 0.32, 10, 0.62],
    8, ['interpolate', ['linear'], ['get', 's'], 0, 0.55, 10, 1.0]];

  const layer = ctx.layers.register({
    id: 'intel.protests', group: 'Conflict', title: 'Protests', subtitle: 'GDELT, machine-coded from news reports',
    description: 'Demonstrations, strikes, blockades and riots reported in the news, deduplicated by place and day. Shown when 2 or more sources or 3 or more articles carry them. Machine-coded, not verified on the ground.',
    source: 'GDELT 2.0 Events, CAMEO root 14 (open data, credit the GDELT Project)', color: PROTEST_COLOUR, symbol: 'point',
    defaultOn: false, refreshMs: 5 * 60 * 1000, viewDependent: true,
    options: [
      { id: 'days', label: 'Period', type: 'select', default: '3',
        choices: [{ value: '1', label: 'Last 24 hours' }, { value: '3', label: 'Last 3 days' }, { value: '7', label: 'Last 7 days' }, { value: '14', label: 'Last 14 days' }] },
      { id: 'minSev', label: 'Lowest severity', type: 'range', min: 0, max: 9, step: 1, default: 0, format: v => (v ? `${v}+` : 'any') },
      { id: 'single', label: 'Include single-source reports', type: 'toggle', default: false },
    ],
    legend: [{ color: PROTEST_RAMP[0], label: 'Demonstration or dissent' }, { color: PROTEST_RAMP[1], label: 'Strike, boycott or blockade' },
      { color: PROTEST_RAMP[2], label: 'Riot or violent protest' }],
    enable() {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: L_RING, type: 'circle', source: SRC, filter: ['all', ['==', ['get', 'c'], 'r'], ['>=', ['get', 's'], 6]],
        paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, 6, 8, 11], 'circle-color': 'rgba(0,0,0,0)',
          'circle-stroke-color': PROTEST_RAMP[2], 'circle-stroke-width': 1.2, 'circle-stroke-opacity': 0.6, 'circle-pitch-alignment': 'map' } }, 'points');
      map2.addLayer({ id: L_ICON, type: 'symbol', source: SRC,
        layout: { 'icon-image': ICON, 'icon-size': size, 'icon-allow-overlap': true, 'icon-ignore-placement': true, 'symbol-sort-key': ['get', 's'] },
        paint: { 'icon-color': KIND_COLOUR, 'icon-opacity': ['case', ['==', ['get', 'p'], 1], 0.92, 0.55], 'icon-halo-color': INK, 'icon-halo-width': 1 } }, 'points');
      map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC, minzoom: 8, filter: ['>=', ['get', 's'], 4],
        layout: { 'text-field': ['get', 'l'], 'text-size': 11, 'text-font': ['Open Sans Regular'], 'text-offset': [0, 1.1], 'text-anchor': 'top',
          'text-optional': true, 'symbol-sort-key': ['-', 10, ['get', 's']] },
        paint: { 'text-color': '#f6c6da', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
    },
    disable() { map2.removeSource(SRC); },
    async refresh({ bounds, zoom, signal, options }) {
      const fc = await ctx.api.get('/api/intel/protests', { bbox: bboxParam(bounds), days: options.days, min_severity: options.minSev,
        single: options.single ? 1 : 0, limit: zoom < 4 ? 2500 : 5000 }, { signal });
      map2.setData(SRC, fc);
      layer.setCount(fc.features.length);
      const meta = fc.meta || {};
      if (meta.error && !fc.features.length) layer.setStatus('error', `GDELT is not answering: ${meta.error}`);
      else if (!fc.features.length) layer.setStatus('ok', 'No protest reports match these settings in this area.');
      else if (meta.limited) layer.setStatus('ok', `Showing the ${fmt.int(fc.features.length)} most severe reports here. Zoom in for more.`);
      else layer.setStatus('ok', null);
    },
  });
  map2.onClick([L_ICON, L_RING], (f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    openProtest(ctx, { kind: 'protest', id: p.id, title: `${p.l} in ${p.n}`, lat, lon, layer: 'intel.protests' });
  });
  map2.onHover([L_ICON, L_RING], (f) => {
    const p = f.properties;
    return `${p.l}, ${p.n}, ${fmt.ago(p.t)}, severity ${(+p.s).toFixed(1)}`;
  });
  ctx.entities.handler('protest', (e) => openProtest(ctx, e), { group: 'Conflict', icon: '◆' });
  return layer;
}

export async function openProtest(ctx, entity) {
  const { h, fmt } = ctx;
  const key = `protest:${entity.id}`;
  const ent = ctx.dom.validLatLon(entity.lat, entity.lon)
    ? { kind: 'protest', id: entity.id, title: entity.title || 'Protest', subtitle: entity.subtitle || '', lat: +entity.lat, lon: +entity.lon, layer: 'intel.protests' }
    : null;
  const card = ctx.cards.open({ key, kind: 'protest', title: entity.title || 'Protest', subtitle: 'Loading the report…', icon: '◆', accent: CONFLICT, entity: ent });
  const d = await safeGet(ctx, `/api/intel/protests/${encodeURIComponent(entity.id)}`);
  if (d.__error) {
    card.update({ subtitle: entity.subtitle || 'Protest',
      sections: [{ text: d.__error.status === 404 ? 'This report has left the 14-day window, so its details are gone. Saved items keep their title and position.' : errorText(d.__error) }] });
    return card;
  }
  const entityFull = { kind: 'protest', id: d.id, title: d.title, subtitle: d.place_display || d.place, lat: d.lat, lon: d.lon, layer: 'intel.protests' };
  const badges = [sevBadge(d.severity), { text: d.category_label, tone: d.category === 'riot' ? 'amber' : 'grey' }];
  if (d.precision !== 'city') badges.push({ text: 'Province-level place', tone: 'amber', title: 'GDELT only knew the province' });
  if (d.single_source) badges.push({ text: 'Single source', tone: 'grey', title: 'Fewer than 2 sources and 3 articles' });
  const sources = (d.sources_list || []).map(s => h('li', null, h('a', { href: s.url }, s.title || s.domain), s.title ? h('span', { class: 'li-s' }, s.domain) : null));
  card.update({
    title: d.title, subtitle: d.place_display || d.place, badges,
    block: [{ label: 'Severity', value: (+d.severity).toFixed(1), unit: '/10' }, { label: 'Mentions', value: fmt.int(d.mentions) },
      { label: 'Sources', value: fmt.int(d.sources) }, { label: 'Reports merged', value: fmt.int(d.reports) }],
    sections: [
      { title: 'What was reported', rows: [
        ['Action', d.action], ['Kind', d.category_label !== d.action ? d.category_label : null],
        ['Actors', [d.actor1, d.actor2].filter(Boolean).join(' and ') || 'Not identified'],
        ['Also named', (d.actors || []).filter(a => a !== d.actor1 && a !== d.actor2).join(', ') || null],
        ['Place (GDELT)', d.place], ['Country', d.country], ['Event date', d.date],
        ['First reported', fmt.utc(d.t_first)], ['Last reported', `${fmt.utc(d.t_last)} (${fmt.ago(d.t_last)})`],
        ['Average tone', (+d.tone).toFixed(1)], ['Theatres', (d.theatres || []).length ? d.theatres.join(', ') : null],
      ] },
      sources.length ? { title: 'Source articles', el: h('ul', { class: 'card-list intel-links' }, sources) } : null,
      { title: 'About this data', text: 'Machine-coded from news reports, not verified. GDELT codes protests (CAMEO 14) from news worldwide; anniversaries, court cases and metaphors can be coded as protests too. Several reports at the same place on the same day are merged here.' },
      { title: 'Nearby', nearby: { lat: d.lat, lon: d.lon, radius_km: 30, exclude: entityFull } },
    ],
    links: (d.main_url || d.urls?.[0]) ? [{ text: 'Open the main source article', href: d.main_url || d.urls[0] }] : [],
    entity: entityFull,
    foot: `GDELT 2.0 Events. Severity ${(+d.severity).toFixed(1)} combines the kind of protest, Goldstein scale, mentions, sources and tone.`,
  });
  return card;
}

/** The "Protests" section of the country card. */
export function protestSection(ctx, p) {
  const { fmt } = ctx;
  return {
    title: `Protests, last 7 days (${fmt.int(p?.events_7d || 0)})`,
    list: (p?.recent || []).slice(0, 6).map(e => ({ title: e.title, subtitle: short(e.subtitle, 70), detail: (+e.severity).toFixed(1),
      onClick: () => { ctx.map2.flyTo(e.lat, e.lon, 8); openProtest(ctx, e); } })),
    empty: 'No protests reported here in the last 7 days (2 or more sources).',
  };
}

export { KIND_NAMES };
