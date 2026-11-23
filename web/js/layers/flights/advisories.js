// Conflict-zone advisories (layer flights.czib): EASA Conflict Zone Information Bulletins shaded on
// the flight information regions they name (VATSpy outlines) or, for bulletins limited to a
// country's territory, on the country. Cards for entity kind "airspace_advisory".

import { AMBER, BLUE, GROUP, INK, RED, dateShort, prose } from './style.js';

const SRC = 'ov-flights-czib';
const L_FILL = 'ov-flights-czib-fill';
const L_LINE = 'ov-flights-czib-line';
const L_LINE_PART = 'ov-flights-czib-part';
const L_LABEL = 'ov-flights-czib-label';
const LEVEL = {
  avoid: { color: RED, label: 'Do not operate', tone: 'red', text: '#ffd6d2' },
  caution: { color: AMBER, label: 'Caution', tone: 'amber', text: '#ffe2b0' },
  info: { color: BLUE, label: 'Information', tone: 'blue', text: '#cfe4ff' },
};
const LEVEL_COLOUR = ['match', ['get', 'level'], 'avoid', RED, 'caution', AMBER, BLUE];
const IS_AREA = ['==', ['get', 'role'], 'area'];
const PARTIAL = ['==', ['get', 'partial'], 1];

let ctx = null;
let layer = null;

function applyFilters() {
  const caution = layer?.option('caution') !== false;
  const lv = caution ? ['all'] : ['all', ['==', ['get', 'level'], 'avoid']];
  const set = (id, extra) => { if (ctx.map.getLayer(id)) ctx.map.setFilter(id, [...lv, ...extra]); };
  set(L_FILL, [IS_AREA]);
  set(L_LINE, [IS_AREA, ['!', PARTIAL]]);
  set(L_LINE_PART, [IS_AREA, PARTIAL]);
  set(L_LABEL, [['==', ['get', 'role'], 'label']]);
}

function ensureLayers() {
  ctx.map2.addSource(SRC, { type: 'geojson', data: ctx.map2.emptyFC() });
  ctx.map2.addLayer({ id: L_FILL, type: 'fill', source: SRC, filter: IS_AREA,
    paint: { 'fill-color': LEVEL_COLOUR, 'fill-opacity': ['case', PARTIAL, 0.14, ['match', ['get', 'level'], 'avoid', 0.24, 0.18]] } }, 'polygons');
  ctx.map2.addLayer({ id: L_LINE, type: 'line', source: SRC, filter: ['all', IS_AREA, ['!', PARTIAL]],
    layout: { 'line-join': 'round' },
    paint: { 'line-color': LEVEL_COLOUR, 'line-width': ['interpolate', ['linear'], ['zoom'], 2, 0.8, 7, 1.6], 'line-opacity': 0.85 } }, 'lines');
  ctx.map2.addLayer({ id: L_LINE_PART, type: 'line', source: SRC, filter: ['all', IS_AREA, PARTIAL],
    paint: { 'line-color': LEVEL_COLOUR, 'line-width': ['interpolate', ['linear'], ['zoom'], 2, 0.8, 7, 1.6], 'line-dasharray': [3, 2], 'line-opacity': 0.85 } }, 'lines');
  ctx.map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC, minzoom: 2, filter: ['==', ['get', 'role'], 'label'],
    layout: { 'text-field': ['get', 'short'], 'text-font': ['Open Sans Bold'], 'text-size': ['interpolate', ['linear'], ['zoom'], 2, 10.5, 6, 13],
      'text-max-width': 9, 'text-padding': 6 },
    paint: { 'text-color': ['match', ['get', 'level'], 'avoid', LEVEL.avoid.text, 'caution', LEVEL.caution.text, LEVEL.info.text],
      'text-halo-color': INK, 'text-halo-width': 1.6 } }, 'top');
  applyFilters();
}

async function refresh({ signal }) {
  const fc = await ctx.api.get('/api/flights/advisories', null, { signal });
  if (signal.aborted || !layer.isOn()) return;
  ctx.map2.setData(SRC, fc);
  const m = fc.meta || {};
  layer.setCount(m.count || 0);
  if (m.error && !m.count) { layer.setStatus('error', 'EASA is not answering right now. Retrying later.'); return; }
  const parts = [`${m.by_level?.avoid || 0} do not operate`, `${m.by_level?.caution || 0} caution`];
  let msg = `${m.count || 0} active bulletins: ${parts.join(', ')}.`;
  if (m.count && !m.mapped) msg += ' Outlines are still loading.';
  else if (m.by_shape?.country) msg += ` ${m.by_shape.country} shaded by country.`;
  layer.setStatus(m.error ? 'warn' : 'ok', msg);
}

function shapeNote(c) {
  const parts = c.parts || [];
  if (!parts.length) return 'No outline is available for this bulletin, so it is not shaded on the map.';
  if (c.shape === 'country') {
    return `Shaded on the country outline (${parts.map(p => p.name).join(', ')}, Natural Earth) because the bulletin is limited to the country's territory or names no flight information region we can draw.`;
  }
  const firs = parts.map(p => p.code).join(', ');
  return `Shaded on the flight information regions ${firs}. Outlines come from the VATSpy data project (CC BY-SA 4.0), made for flight simulation, so treat the edges as approximate.`;
}

// The data block keeps values short: "All", "FL260", "Varies".
function altitudeBlock(text) {
  const m = /^Below (FL\d{3})$/.exec(text || '');
  if (m) return [{ label: 'Below', value: m[1] }];
  if (text === 'All altitudes') return [{ label: 'Altitudes', value: 'All' }];
  if (text === 'Varies by area') return [{ label: 'Altitudes', value: 'Varies' }];
  return [{ label: 'Altitudes', value: 'See text' }];
}

export function buildSpec(c) {
  const lv = LEVEL[c.level] || LEVEL.info;
  const badges = [{ text: c.level_label || lv.label, tone: lv.tone }];
  if (c.partial) badges.push({ text: 'Covers part of the shaded area', tone: 'grey', title: 'Read the recommendation for the exact limits' });
  if (c.status && c.status !== 'Active') badges.push({ text: c.status, tone: 'grey' });
  const sections = [];
  if (c.recommendation) sections.push({ title: 'Recommendation', el: prose(ctx.h, c.recommendation, { max: 1600 }) });
  if (c.airspace) sections.push({ title: 'Affected airspace', el: prose(ctx.h, c.airspace, { max: 700 }) });
  sections.push({ rows: [
    ['Countries', (c.countries || []).map(x => x.name).join(', ') || c.countries_text],
    ['Applies to', c.applicability],
    ['Bulletin', c.number],
    ['Issued', dateShort(c.issued)],
    ['Last revision', c.revision_text],
    ['Valid until', c.valid_text || dateShort(c.valid_until)],
  ] });
  if (c.description) sections.push({ title: 'Background', el: prose(ctx.h, c.description, { max: 900 }) });
  sections.push({ title: 'About the shading', text: shapeNote(c) });
  return {
    key: `airspace_advisory:${c.id}`, kind: 'airspace_advisory', title: c.subject, subtitle: `EASA conflict zone bulletin ${c.number}`,
    icon: '▲', accent: GROUP, badges,
    block: altitudeBlock(c.altitudes).concat([{ label: 'Valid until', value: dateShort(c.valid_until) },
      { label: 'In force since', value: dateShort(c.issued) }]),
    sections, links: [{ text: 'Bulletin on easa.europa.eu', href: c.link || 'https://www.easa.europa.eu/en/domains/air-operations/czibs' }],
    actions: c.bbox ? [{ text: 'Show the whole area', onClick: () => ctx.map2.fitBbox(c.bbox, { maxZoom: 7 }) }] : [],
    entity: c.entity,
    foot: 'EASA Conflict Zone Information Bulletin (source: EASA). Recommendations to EU operators; national rules may go further.',
  };
}

export async function openAdvisory(id, seed = null) {
  const cid = String(id || '').trim().toLowerCase();
  if (!/^czib-\d{4}-\d{2}$/.test(cid)) return null;
  const card = ctx.cards.open({ key: `airspace_advisory:${cid}`, kind: 'airspace_advisory', title: seed?.title || 'Conflict zone advisory',
    subtitle: 'Loading…', icon: '▲', accent: GROUP, sections: [{ text: 'Loading the bulletin…' }] });
  try {
    const c = await ctx.api.get(`/api/flights/advisories/${cid}`);
    if (card.isOpen()) card.update(buildSpec(c));
  } catch (err) {
    if (card.isOpen()) {
      card.update({ subtitle: err?.status === 404 ? 'No longer listed' : 'Unavailable',
        sections: [{ text: err?.status === 404 ? 'EASA no longer lists this bulletin.' : 'The bulletin could not be loaded right now.' }],
        links: [{ text: 'EASA conflict zone bulletins', href: 'https://www.easa.europa.eu/en/domains/air-operations/czibs' }] });
    }
  }
  return card;
}

export function register(c) {
  ctx = c;
  layer = ctx.layers.register({
    id: 'flights.czib', group: 'Flights', title: 'Conflict zone advisories', subtitle: 'EASA Conflict Zone Information Bulletins',
    description: 'Airspace that EASA tells European airlines not to use (red) or to use with caution (amber) because of conflict. Shaded on the flight information regions each bulletin names; dashed outlines mean the bulletin covers only part of the shaded area.',
    source: 'EASA (source acknowledged); FIR outlines from the VATSpy data project (CC BY-SA 4.0); Natural Earth countries', color: RED, symbol: 'area',
    defaultOn: false, refreshMs: 30 * 60 * 1000,
    options: [{ id: 'caution', label: 'Show caution advisories', type: 'toggle', default: true, refresh: false }],
    legend: [{ color: RED, label: 'Do not operate', shape: 'area' }, { color: AMBER, label: 'Caution', shape: 'area' },
      { color: '#aab4ca', label: 'Dashed outline: covers part of the area', shape: 'line' }],
    async enable() { ensureLayers(); },
    async disable() { ctx.map2.removeSource(SRC); },
    refresh,
    onOption() { applyFilters(); },
  });
  ctx.map2.onClick([L_FILL, L_LABEL], (f) => openAdvisory(f.properties?.id, { title: f.properties?.subject }));
  ctx.map2.onHover([L_FILL, L_LABEL], (f) => {
    const p = f.properties || {};
    const lv = (LEVEL[p.level] || LEVEL.info).label.toLowerCase();
    const where = p.role === 'area' && p.part && p.pkind === 'fir' ? ` (FIR ${p.part})` : '';
    return `${p.subject}${where}: ${lv}${p.alt ? `, ${p.alt.toLowerCase().replace('fl', 'FL')}` : ''}`;
  });
  ctx.entities.handler('airspace_advisory', (e) => openAdvisory(e.id, e), { group: 'Flights', icon: '▲' });
  return layer;
}
