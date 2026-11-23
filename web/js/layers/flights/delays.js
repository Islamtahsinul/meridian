// US airport delays (layer flights.delays): FAA NAS Status ground stops, ground delay programmes,
// arrival and departure delays, closures and notices. Cards for entity kind "airport_delay".

import { AMBER, BLUE, GROUP, INK, RED, minutesText, utcClock } from './style.js';

const SRC = 'ov-flights-delays';
const L_RING = 'ov-flights-delays-ring';
const L_DOT = 'ov-flights-delays-dot';
const L_LABEL = 'ov-flights-delays-label';
const LEVEL_COLOUR = ['match', ['get', 'level'], 3, RED, 2, AMBER, BLUE];
const TONE = { high: 'red', medium: 'amber', low: 'blue' };
const ABOUT = 'A ground stop holds flights bound for the airport at their departure airports. A ground delay programme '
  + 'gives them later departure slots, so the delay is taken on the ground. Arrival and departure delays are what the '
  + 'FAA reports for the airport itself. Times are UTC.';

let ctx = null;
let layer = null;

function ensureLayers() {
  ctx.map2.addSource(SRC, { type: 'geojson', data: ctx.map2.emptyFC() });
  ctx.map2.addLayer({ id: L_RING, type: 'circle', source: SRC, filter: ['==', ['get', 'level'], 3],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, 9, 6, 13, 10, 18],
      'circle-color': 'rgba(255,93,82,0.16)', 'circle-stroke-color': RED, 'circle-stroke-width': 1.4,
      'circle-pitch-alignment': 'map',
    } }, 'points');
  ctx.map2.addLayer({ id: L_DOT, type: 'circle', source: SRC,
    layout: { 'circle-sort-key': ['get', 'sk'] },
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'],
        2, ['interpolate', ['linear'], ['coalesce', ['get', 'avg'], 0], 0, 4, 30, 5, 60, 6.5, 120, 8],
        8, ['interpolate', ['linear'], ['coalesce', ['get', 'avg'], 0], 0, 6, 30, 7.5, 60, 9.5, 120, 12]],
      'circle-color': LEVEL_COLOUR, 'circle-stroke-color': INK, 'circle-stroke-width': 1.5,
    } }, 'points');
  ctx.map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC, minzoom: 3.5,
    layout: {
      'text-field': ['get', 'label'], 'text-font': ['Open Sans Bold'], 'text-size': 11,
      'text-anchor': 'left', 'text-offset': [0.9, 0], 'text-padding': 2, 'symbol-sort-key': ['-', 0, ['get', 'sk']],
    },
    paint: { 'text-color': ['match', ['get', 'level'], 3, '#ffd6d2', 2, '#ffe2b0', '#cfe4ff'], 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
  applyFilter();
}

function applyFilter() {
  const showInfo = layer?.option('info') !== false;
  const f = showInfo ? null : ['>=', ['get', 'level'], 2];
  for (const id of [L_DOT, L_LABEL]) if (ctx.map.getLayer(id)) ctx.map.setFilter(id, f);
}

async function refresh({ signal }) {
  const fc = await ctx.api.get('/api/flights/airport-status', null, { signal });
  if (signal.aborted || !layer.isOn()) return;
  ctx.map2.setData(SRC, fc);
  const m = fc.meta || {};
  layer.setCount(m.delayed || 0);
  if (m.error && !m.updated) { layer.setStatus('error', 'FAA NAS Status is not answering right now. Retrying shortly.'); return; }
  const msg = statusMessage(m, fc.features);
  layer.setStatus(m.stale || m.error ? 'warn' : 'ok', m.stale || m.error ? `${msg} The FAA data is out of date.` : msg);
}

/** The layer row text. The count is airports with a delay or a full closure; airports with only a notice
 *  (closed to some traffic, de-icing, ...) are drawn too, so the text says how many of those there are,
 *  by the top status of each (features from /api/flights/airport-status). */
export function statusMessage(m, features = []) {
  const delayed = m.delayed || 0;
  const stops = m.counts?.ground_stop || 0;
  const gdp = m.counts?.ground_delay || 0;
  const parts = [];
  if (stops) parts.push(`${stops} ground ${stops === 1 ? 'stop' : 'stops'}`);
  if (gdp) parts.push(`${gdp} delay ${gdp === 1 ? 'programme' : 'programmes'}`);
  const kinds = new Map();
  for (const f of features || []) {
    const p = f?.properties;
    if (!p || p.delay) continue;
    const k = String(p.type || 'Airport notice').toLowerCase();
    kinds.set(k, (kinds.get(k) || 0) + 1);
  }
  const others = [...kinds.values()].reduce((x, y) => x + y, 0) || Math.max(0, (m.airports || 0) - delayed);
  const list = [...kinds].map(([k, n]) => `${n} ${k}`).join(', ');
  const notes = others ? ` ${others} more with FAA notices only${list ? ` (${list})` : ''}.` : '';
  return delayed ? `${delayed} US ${delayed === 1 ? 'airport' : 'airports'} with delays or full closures${parts.length ? ` (${parts.join(', ')})` : ''}.${notes}`
    : `No US airport reports delays right now.${notes}`;
}

// Block cells keep values short: '16:00' today, '24 Nov' with the time small beside it on other days,
// and '28 May 2027' when the date is in another year (so a date next year never reads as one gone by).
export function when(t, now = new Date()) {
  const c = utcClock(t, now);
  if (!c) return { value: null, unit: '' };
  const i = c.lastIndexOf(' ');
  if (i < 0) return { value: c, unit: 'UTC' };
  const y = new Date(+t * 1000).getUTCFullYear();
  const day = y === now.getUTCFullYear() ? c.slice(0, i) : `${c.slice(0, i)} ${y}`;
  return { value: day, unit: `${c.slice(i + 1)} UTC` };
}

function blockFor(d) {
  const { fmt } = ctx;
  const top = d.items?.[0] || d;
  const out = [];
  if (top.code === 'ground_stop') {
    out.push(top.end ? { label: 'Ground stop until', ...when(top.end) } : { label: 'Ground stop', value: 'Open-ended' });
  } else if (top.code === 'ground_delay') {
    out.push({ label: 'Average delay', value: top.avg_delay_min != null ? fmt.int(top.avg_delay_min) : null, unit: 'min' });
    if (top.max_delay_min) out.push({ label: 'Maximum', value: fmt.int(top.max_delay_min), unit: 'min' });
    if (top.end) out.push({ label: 'Programme ends', ...when(top.end) });
  } else if (top.code === 'arrival_delay' || top.code === 'departure_delay') {
    const range = top.min_delay_min != null && top.max_delay_min != null ? `${top.min_delay_min}–${top.max_delay_min}` : (top.avg_delay_min != null ? String(top.avg_delay_min) : null);
    out.push({ label: top.code === 'arrival_delay' ? 'Arrivals delayed' : 'Departures delayed', value: range, unit: 'min' });
    if (top.trend) out.push({ label: 'Trend', value: top.trend });
  } else if (top.code === 'closure' || top.code === 'partial_closure') {
    out.push(top.end ? { label: top.code === 'closure' ? 'Closed until' : 'Restricted until', ...when(top.end) } : { label: top.code === 'closure' ? 'Closed' : 'Restricted', value: 'Open-ended' });
  } else if (top.code === 'deicing') {
    out.push({ label: 'De-icing since', ...when(top.start) });
  }
  if (d.runways?.arrival_rate) out.push({ label: 'Arrival rate', value: fmt.int(d.runways.arrival_rate), unit: 'an hour' });
  return out.slice(0, 4);
}

function itemRows(it) {
  const { fmt } = ctx;
  const range = it.min_delay_min != null && it.max_delay_min != null ? `${it.min_delay_min} to ${it.max_delay_min} min` : null;
  return [
    ['Reason', it.reason],
    ['Average delay', it.code === 'ground_delay' || !range ? minutesText(it.avg_delay_min) : null],
    ['Maximum delay', it.code === 'ground_delay' ? minutesText(it.max_delay_min) : null],
    ['Delays', range],
    ['Trend', it.trend],
    ['Starts', it.start && it.start * 1000 > Date.now() ? `${fmt.utc(it.start)} (${fmt.ago(it.start)})` : null],
    ['In effect since', it.start && it.start * 1000 <= Date.now() ? fmt.utc(it.start) : null],
    ['Ends', it.end ? `${fmt.utc(it.end)} (${fmt.ago(it.end)})` : null],
    ['Updated', it.updated ? `${fmt.utc(it.updated)} (${fmt.ago(it.updated)})` : null],
  ];
}

export function buildSpec(d) {
  const { fmt } = ctx;
  const a = d.airport || {};
  const items = d.items || [];
  const badges = items.map(it => ({ text: it.type, tone: TONE[it.level] || 'grey' }));
  if (items.some(it => it.trend === 'Increasing')) badges.push({ text: 'Delays increasing', tone: 'amber' });
  const sections = [];
  for (const it of items) {
    sections.push({ title: it.type, text: it.summary });
    sections.push({ rows: itemRows(it) });
  }
  const rw = d.runways || {};
  if (rw.arrival || rw.departure || rw.arrival_rate) {
    sections.push({ title: 'Runways in use', rows: [['Arrivals', rw.arrival], ['Departures', rw.departure],
      ['Arrival rate', rw.arrival_rate ? `${fmt.int(rw.arrival_rate)} aircraft an hour` : null]] });
  }
  sections.push({ title: 'What these mean', text: ABOUT });
  const links = [];
  for (const it of items) if (it.advisory && !links.some(l => l.href === it.advisory)) links.push({ text: `FAA advisory, ${it.type.toLowerCase()}`, href: it.advisory });
  links.push({ text: 'FAA NAS Status', href: d.link || 'https://nasstatus.faa.gov/' });
  const actions = [];
  if (ctx.moduleOk('infra') && (a.icao || a.faa)) {
    actions.push({ text: 'Airport details', onClick: () => ctx.entities.open({ kind: 'airport', id: a.icao || a.faa, title: a.name, lat: a.lat, lon: a.lon }, { fly: false }) });
  }
  return {
    key: `airport_delay:${d.entity?.id || a.icao || a.faa}`, kind: 'airport_delay', title: a.name || a.faa,
    subtitle: [a.faa, a.icao, 'FAA NAS Status'].filter(Boolean).join(', '), icon: '◷', accent: GROUP,
    badges, block: blockFor(d), sections, links, actions, entity: d.entity,
    foot: `FAA NAS Status${d.fetched_at ? `, checked ${fmt.ago(d.fetched_at)}` : ''}. US airports only.`,
  };
}

function goneSpec(ident, seed, err) {
  const missing = err?.status === 404;
  return {
    key: `airport_delay:${ident}`, kind: 'airport_delay', title: seed?.title || ident, subtitle: missing ? 'No current delays' : 'Unavailable',
    icon: '◷', accent: GROUP, badges: missing ? [{ text: 'Normal operations', tone: 'green' }] : [],
    sections: [{ text: missing ? 'The FAA reports no delay, closure or notice for this airport right now. Delays clear quickly; this card was saved earlier.'
      : 'The FAA delay information could not be loaded right now. The card will retry.' }],
    links: [{ text: 'FAA NAS Status', href: 'https://nasstatus.faa.gov/' }],
    entity: seed && ctx.dom.validLatLon(seed.lat, seed.lon) ? { kind: 'airport_delay', id: ident, title: seed.title || ident, lat: seed.lat, lon: seed.lon, layer: 'flights.delays' } : null,
  };
}

export async function openDelay(ident, seed = null) {
  ident = String(ident || '').trim().toUpperCase();
  if (!/^[A-Z0-9-]{2,12}$/.test(ident)) return null;
  const path = `/api/flights/airport-status/${encodeURIComponent(ident)}`;
  const card = ctx.cards.open({
    key: `airport_delay:${ident}`, kind: 'airport_delay', title: seed?.title || ident, subtitle: 'Loading…', icon: '◷', accent: GROUP,
    sections: [{ text: 'Loading the FAA status…' }],
    live: { everyMs: 120000, update: async () => { try { return buildSpec(await ctx.api.get(path)); } catch (err) { return goneSpec(ident, seed, err); } } },
  });
  try {
    const d = await ctx.api.get(path);
    if (card.isOpen()) card.update(buildSpec(d));
  } catch (err) {
    if (card.isOpen()) card.update(goneSpec(ident, seed, err));
  }
  return card;
}

export function register(c) {
  ctx = c;
  layer = ctx.layers.register({
    id: 'flights.delays', group: 'Flights', title: 'Airport delays (US)', subtitle: 'Ground stops, delay programmes and closures from the FAA',
    description: 'US airports where the FAA reports a ground stop, a ground delay programme, arrival or departure delays, a closure or a notice. Red: flights held or the airport closed. Amber: delays. Blue: information such as de-icing.',
    source: 'FAA NAS Status (US Government, public domain)', color: AMBER, symbol: 'point', defaultOn: false, refreshMs: 120000,
    options: [{ id: 'info', label: 'Show notices and de-icing', type: 'toggle', default: true, refresh: false }],
    legend: [{ color: RED, label: 'Ground stop or closed (ringed)' }, { color: AMBER, label: 'Delays or a delay programme' },
      { color: BLUE, label: 'Notice or de-icing' }, { color: '#aab4ca', label: 'Larger dot: longer average delay' }],
    async enable() { ensureLayers(); },
    async disable() { ctx.map2.removeSource(SRC); },
    refresh,
    onOption() { applyFilter(); },
  });
  ctx.map2.onClick([L_DOT, L_RING, L_LABEL], (f) => {
    const p = f.properties || {};
    const [lon, lat] = f.geometry.coordinates;
    openDelay(p.id || p.faa, { title: p.name, lat, lon });
  });
  ctx.map2.onHover([L_DOT, L_RING, L_LABEL], (f) => {
    const p = f.properties || {};
    const extra = p.avg ? `, ${p.avg} min average` : (p.max ? `, up to ${p.max} min` : '');
    return `${p.name} (${p.faa}): ${String(p.type || '').toLowerCase()}${extra}${p.n > 1 ? `, and ${p.n - 1} more` : ''}`;
  });
  ctx.entities.handler('airport_delay', (e) => openDelay(String(e.id || '').split(':')[0], e), { group: 'Flights', icon: '◷' });
  return layer;
}
