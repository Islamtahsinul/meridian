// US temporary flight restrictions (layer flights.tfr) from tfr.faa.gov: polygons coloured by type,
// solid while in effect and dashed when they start later, a dot per restriction when zoomed out.
// Cards for entity kind "flight_restriction" (id = the NOTAM number, "6/9435").

import { AMBER, BLUE, GROUP, INK, RED, utcShort } from './style.js';

const SRC = 'ov-flights-tfr';
const L_FILL = 'ov-flights-tfr-fill';
const L_LINE = 'ov-flights-tfr-line';
const L_LINE_LATER = 'ov-flights-tfr-later';
const L_DOT = 'ov-flights-tfr-dot';
const L_LABEL = 'ov-flights-tfr-label';
export const CATEGORIES = {
  vip: { color: RED, label: 'VIP movement', tone: 'red' },
  security: { color: AMBER, label: 'Security', tone: 'amber' },
  hazards: { color: '#f08c5a', label: 'Hazard, for example a wildfire', tone: 'amber' },
  space: { color: '#a08ff5', label: 'Space operations', tone: 'blue' },
  sports: { color: BLUE, label: 'Air show or sporting event', tone: 'blue' },
  drones: { color: '#86d6f7', label: 'Drones only', tone: 'grey' },
  other: { color: '#aab4ca', label: 'Other', tone: 'grey' },
};
const CAT_COLOUR = ['match', ['get', 'cat'], ...Object.entries(CATEGORIES).flatMap(([k, v]) => [k, v.color]), '#aab4ca'];
const IS_AREA = ['==', ['get', 'role'], 'area'];
const IS_CENTRE = ['==', ['get', 'role'], 'centre'];
const ACTIVE = ['==', ['get', 'active'], 1];

let ctx = null;
let layer = null;

function optionFilter() {
  const cat = layer?.option('kind') || 'all';
  const later = layer?.option('later') !== false;
  const drones = !!layer?.option('drones');
  const parts = ['all'];
  if (cat !== 'all') parts.push(['==', ['get', 'cat'], cat]);
  else if (!drones) parts.push(['!=', ['get', 'cat'], 'drones']);
  if (!later) parts.push(ACTIVE);
  return parts;
}

function applyFilters() {
  const base = optionFilter();
  const set = (id, extra) => { if (ctx.map.getLayer(id)) ctx.map.setFilter(id, [...base, ...extra]); };
  set(L_FILL, [IS_AREA]);
  set(L_LINE, [IS_AREA, ACTIVE]);
  set(L_LINE_LATER, [IS_AREA, ['!=', ['get', 'active'], 1]]);
  set(L_DOT, [IS_CENTRE]);
  set(L_LABEL, [IS_CENTRE]);
}

function ensureLayers() {
  ctx.map2.addSource(SRC, { type: 'geojson', data: ctx.map2.emptyFC() });
  ctx.map2.addLayer({ id: L_FILL, type: 'fill', source: SRC, filter: IS_AREA,
    paint: { 'fill-color': CAT_COLOUR, 'fill-opacity': ['case', ACTIVE, ['match', ['get', 'cat'], 'vip', 0.3, 0.22], 0.07] } }, 'polygons');
  ctx.map2.addLayer({ id: L_LINE, type: 'line', source: SRC, filter: ['all', IS_AREA, ACTIVE],
    layout: { 'line-join': 'round' },
    paint: { 'line-color': CAT_COLOUR, 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.9, 10, 1.8], 'line-opacity': 0.95 } }, 'lines');
  ctx.map2.addLayer({ id: L_LINE_LATER, type: 'line', source: SRC, filter: ['all', IS_AREA, ['!=', ['get', 'active'], 1]],
    paint: { 'line-color': CAT_COLOUR, 'line-width': 1.2, 'line-dasharray': [2, 2], 'line-opacity': 0.85 } }, 'lines');
  // Zoomed out the areas are too small to see: a ring marks each one (hollow, so it reads as an area,
  // not an airport), drawn under the point layers so airport symbols and labels stay on top.
  ctx.map2.addLayer({ id: L_DOT, type: 'circle', source: SRC, maxzoom: 7.5, filter: IS_CENTRE,
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, ['match', ['get', 'cat'], 'vip', 5.5, 4], 7, ['match', ['get', 'cat'], 'vip', 8, 6]],
      'circle-color': 'rgba(17,26,44,0.35)', 'circle-stroke-color': CAT_COLOUR,
      'circle-stroke-width': ['case', ACTIVE, 2, 1.2], 'circle-stroke-opacity': ['case', ACTIVE, 1, 0.7],
    } }, 'lines');
  ctx.map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC, minzoom: 7, filter: IS_CENTRE,
    layout: {
      'text-field': ['format', ['get', 'type'], {}, '\n', {}, ['concat', 'FDC ', ['get', 'id']], { 'font-scale': 0.85 }],
      'text-font': ['Open Sans Bold'], 'text-size': 11, 'text-max-width': 12, 'text-padding': 4,
    },
    paint: { 'text-color': '#f3ecdc', 'text-halo-color': INK, 'text-halo-width': 1.5 } }, 'top');
  applyFilters();
}

async function refresh({ signal }) {
  const fc = await ctx.api.get('/api/flights/tfrs', null, { signal });
  if (signal.aborted || !layer.isOn()) return;
  ctx.map2.setData(SRC, fc);
  const m = fc.meta || {};
  layer.setCount(m.active || 0);
  if (m.error && !m.updated) { layer.setStatus('error', 'tfr.faa.gov is not answering right now. Retrying shortly.'); return; }
  const msg = `${m.active || 0} in effect and ${m.upcoming || 0} starting later; ${m.mapped || 0} of ${m.count || 0} have a map area.`;
  layer.setStatus(m.error ? 'warn' : 'ok', m.details_pending ? `${msg} Times and altitudes are still loading for ${m.details_pending}.` : msg);
}

// The data block keeps values short: a day in large figures with the time small beside it, and the
// ceiling in feet with its reference (MSL or above ground) in the label.
function dayTime(t) {
  const s = utcShort(t);
  if (!s) return { value: null, unit: '' };
  const i = s.lastIndexOf(' ');
  return { value: s.slice(0, i), unit: s.slice(i + 1) };
}

function ceilingCell(text) {
  const m = /^([\d,]+) ft (MSL|above ground)$/.exec(String(text || ''));
  if (m) return { label: m[2] === 'MSL' ? 'Ceiling, MSL' : 'Ceiling, above ground', value: m[1], unit: 'ft' };
  return { label: 'Ceiling', value: text || null };
}

function statusBadge(t) {
  const { fmt } = ctx;
  if (t.status === 'upcoming') return { text: `Starts ${fmt.ago(t.start)}`, tone: 'grey' };
  if (t.status === 'active') return { text: t.permanent ? 'In effect until further notice' : 'In effect now', tone: 'amber' };
  if (t.status === 'listed') return { text: 'Listed by the FAA', tone: 'grey' };
  return { text: 'Ended', tone: 'grey' };
}

function sentence(s) {
  if (!s) return null;
  const t = /[a-z]/.test(s) ? s : s.toLowerCase();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

export function buildSpec(t) {
  const { fmt, h } = ctx;
  const cat = CATEGORIES[t.category] || CATEGORIES.other;
  const started = t.start && t.start * 1000 <= Date.now();
  const block = [
    { label: started ? 'Started, UTC' : 'Starts, UTC', ...dayTime(t.start) },
    t.permanent ? { label: 'Ends', value: 'Open-ended' } : { label: 'Ends, UTC', ...dayTime(t.end) },
    ceilingCell(t.upper),
  ];
  const sections = [];
  if (!t.has_detail) sections.push({ text: 'The notice details (times and altitudes) are not loaded yet. The official notice is linked below.' });
  sections.push({ rows: [['Restriction', t.rule_text || t.type_label], ['Purpose', sentence(t.purpose)], ['Schedule', t.schedule],
    ['Near', t.city], ['Issued', t.issued ? `${fmt.utc(t.issued)} (${fmt.ago(t.issued)})` : null],
    ['Controlling facility', [t.facility, t.coord_facility ? sentence(t.coord_facility) : null].filter(Boolean).join(', ') || null]] });
  if ((t.areas || []).length) {
    sections.push({ title: (t.areas.length > 1 ? 'Areas' : 'Area'), rows: t.areas.map((a, i) => [a.name || `Area ${i + 1}`,
      [a.lower && a.upper ? `${a.lower} to ${a.upper}` : null, a.schedule].filter(Boolean).join(', ') || 'See the notice']) });
  }
  if (!t.mapped) sections.push({ text: 'This notice has no map area of its own (for example a nationwide notice that lists places elsewhere).' });
  if (t.text) {
    const det = h('details', { class: 'flights-notam' }, h('summary', null, 'Notice text, as issued'), h('p', null, t.text));
    sections.push({ el: det });
  }
  const actions = t.bbox ? [{ text: 'Show the whole area', onClick: () => ctx.map2.fitBbox(t.bbox) }] : [];
  return {
    key: `flight_restriction:${t.id}`, kind: 'flight_restriction', title: t.place || `Flight restriction ${t.id}`, subtitle: `${t.type_label}, FDC ${t.id}`,
    icon: '⊘', accent: GROUP, badges: [{ text: cat.label === 'Other' ? (t.type_label || 'Restriction') : cat.label, tone: cat.tone }, statusBadge(t)],
    block, sections, links: [{ text: 'Official notice on tfr.faa.gov', href: t.link }], actions, entity: t.entity,
    foot: 'FAA temporary flight restriction. Altitudes are above mean sea level unless marked above ground. Check the official notice before any flight.',
  };
}

export async function openTfr(id, seed = null) {
  const m = /^(\d)[/_](\d{4})$/.exec(String(id || '').trim());
  if (!m) return null;
  const notam = `${m[1]}/${m[2]}`;
  const card = ctx.cards.open({ key: `flight_restriction:${notam}`, kind: 'flight_restriction', title: seed?.title || `Flight restriction ${notam}`, subtitle: 'Loading…', icon: '⊘', accent: GROUP,
    sections: [{ text: 'Loading the notice…' }] });
  try {
    const t = await ctx.api.get(`/api/flights/tfrs/${m[1]}_${m[2]}`);
    if (card.isOpen()) card.update(buildSpec(t));
  } catch (err) {
    if (card.isOpen()) {
      card.update({ subtitle: err?.status === 404 ? 'No longer listed' : 'Unavailable',
        sections: [{ text: err?.status === 404 ? 'This restriction is not on the FAA list any more. It has expired or been cancelled.'
          : 'The restriction could not be loaded right now.' }],
        links: [{ text: 'FAA temporary flight restrictions', href: 'https://tfr.faa.gov/' }] });
    }
  }
  return card;
}

export function register(c) {
  ctx = c;
  layer = ctx.layers.register({
    id: 'flights.tfr', group: 'Flights', title: 'Flight restrictions (US)', subtitle: 'FAA temporary flight restrictions',
    description: 'Airspace the FAA has closed for a while: VIP movements, security areas, hazards such as wildfires, space launches, air shows and stadiums. Solid outlines are in effect now; dashed ones start later.',
    source: 'FAA tfr.faa.gov (US Government, public domain)', color: AMBER, symbol: 'area', defaultOn: false, refreshMs: 300000,
    options: [
      { id: 'kind', label: 'Show', type: 'select', default: 'all', refresh: false,
        choices: [{ value: 'all', label: 'Every type' }, ...Object.entries(CATEGORIES).filter(([k]) => k !== 'other').map(([value, v]) => ({ value, label: v.label }))] },
      { id: 'later', label: 'Include restrictions that start later', type: 'toggle', default: true, refresh: false },
      { id: 'drones', label: 'Include drone-only restrictions', type: 'toggle', default: false, refresh: false },
    ],
    legend: [...Object.values(CATEGORIES).filter(v => v.label !== 'Other').map(v => ({ color: v.color, label: v.label, shape: 'area' })),
      { color: '#aab4ca', label: 'Dashed outline: starts later', shape: 'line' }],
    async enable() { ensureLayers(); },
    async disable() { ctx.map2.removeSource(SRC); },
    refresh,
    onOption() { applyFilters(); },
  });
  ctx.map2.onClick([L_FILL, L_DOT, L_LABEL], (f) => openTfr(f.properties?.id, { title: f.properties?.place }));
  ctx.map2.onHover([L_FILL, L_DOT, L_LABEL], (f) => {
    const p = f.properties || {};
    const alt = p.lower && p.upper ? `, ${p.lower === 'Surface' ? 'surface' : p.lower} to ${p.upper}` : '';
    return `${p.type}: ${p.place || `FDC ${p.id}`}${alt}${p.active === 1 ? '' : ', starts later'}`;
  });
  ctx.entities.handler('flight_restriction', (e) => openTfr(e.id, e), { group: 'Flights', icon: '⊘' });
  return layer;
}
