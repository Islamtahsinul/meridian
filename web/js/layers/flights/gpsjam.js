// GPS interference layer (group Conflict): 0.5 degree cells coloured by the share of aircraft that
// report degraded navigation accuracy. Card per cell, "What's here?" section, entity kind gps_jam.

import { AMBER, GREEN, RED } from './style.js';
import { dayChart } from './activity.js';

const SRC = 'ov-flights-gpsjam';
const FILL = 'ov-flights-gpsjam-fill';
const LINE = 'ov-flights-gpsjam-line';
const LEVEL = { 1: 'Low', 2: 'Medium', 3: 'High' };
const TONE = { 1: 'green', 2: 'amber', 3: 'red' };
const METHOD = 'Derived from the navigation accuracy aircraft report about themselves (ADS-B NACp and NIC), not a direct '
  + 'measurement of radio interference. Each cell shows the share of aircraft passing through it with degraded accuracy '
  + 'over the window: green under 2%, amber 2 to 10%, red 10% or more. Spoofing that keeps the reported accuracy high is '
  + 'not detected, and some older avionics always report low accuracy.';

let ctx = null;
let layer = null;
let lastAgg = 1;

function cellFeature(c, ix) {
  const lat0 = c[ix.lat0];
  const lon0 = c[ix.lon0];
  const size = c[ix.size];
  const total = c[ix.total];
  const bad = c[ix.bad];
  return {
    type: 'Feature',
    geometry: { type: 'Polygon', coordinates: [[[lon0, lat0], [lon0 + size, lat0], [lon0 + size, lat0 + size], [lon0, lat0 + size], [lon0, lat0]]] },
    properties: { lat0, lon0, size, total, bad, level: c[ix.level], share: total ? bad / total : 0, last: c[ix.last_bad_t] || 0 },
  };
}

function ensureLayers() {
  ctx.map2.addSource(SRC, { type: 'geojson', data: ctx.map2.emptyFC() });
  ctx.map2.addLayer({ id: FILL, type: 'fill', source: SRC,
    paint: {
      'fill-color': ['match', ['get', 'level'], 3, RED, 2, AMBER, GREEN],
      'fill-opacity': ['match', ['get', 'level'], 3, 0.35, 2, 0.27, 0.1],
      'fill-antialias': true,
    } }, 'polygons');
  ctx.map2.addLayer({ id: LINE, type: 'line', source: SRC, minzoom: 3.5, filter: ['>=', ['get', 'level'], 2],
    paint: { 'line-color': ['match', ['get', 'level'], 3, RED, AMBER], 'line-width': 0.8, 'line-opacity': 0.75 } }, 'polygons');
}

function hoursOption() {
  const v = Number(layer?.option('hours'));
  return [6, 12, 24, 48].includes(v) ? v : 24;
}

async function refresh({ bounds, zoom, signal, options }) {
  const res = await ctx.api.get('/api/flights/gpsjam', { bbox: bounds.join(','), zoom: zoom.toFixed(1), hours: hoursOption() }, { signal });
  const ix = Object.fromEntries((res.fields || []).map((n, i) => [n, i]));
  const showLow = options?.low !== false;
  const feats = (res.cells || []).filter(c => showLow || c[ix.level] >= 2).map(c => cellFeature(c, ix));
  lastAgg = res.agg || 1;
  ctx.map2.setData(SRC, { type: 'FeatureCollection', features: feats });
  const flagged = (res.counts?.amber || 0) + (res.counts?.red || 0);
  layer.setCount(flagged);
  if (!res.cells?.length) {
    // Zoomed in over closed or quiet airspace (Ukraine, say) there may never be samples: that is not a fault.
    const world = bounds[2] - bounds[0] >= 300;
    if (world) layer.setStatus('warn', 'No cells have enough aircraft samples yet. The background sampler fills this in over the first hours.');
    else layer.setStatus('ok', 'No cell in view has enough aircraft to judge. Closed or quiet airspace stays blank; zoom out to see the areas being watched.');
  } else {
    layer.setStatus('ok', `${res.counts?.red || 0} red and ${res.counts?.amber || 0} amber cells over the last ${res.hours} h.`);
  }
}

function sparkline(hourly) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  const n = Math.max(hourly.length, 1);
  svg.setAttribute('viewBox', `0 0 ${n * 10} 40`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('class', 'flights-spark');
  svg.setAttribute('aria-hidden', 'true');
  const max = Math.max(1, ...hourly.map(x => x[1]));
  hourly.forEach(([, total, bad], i) => {
    const hTot = Math.max(1.5, (total / max) * 38);
    const hBad = total ? hTot * (bad / total) : 0;
    const base = document.createElementNS(ns, 'rect');
    base.setAttribute('x', String(i * 10 + 1));
    base.setAttribute('width', '8');
    base.setAttribute('y', String(40 - hTot));
    base.setAttribute('height', String(hTot - hBad));
    base.setAttribute('class', 'ok');
    svg.append(base);
    if (hBad > 0) {
      const r = document.createElementNS(ns, 'rect');
      r.setAttribute('x', String(i * 10 + 1));
      r.setAttribute('width', '8');
      r.setAttribute('y', String(40 - hBad));
      r.setAttribute('height', String(hBad));
      r.setAttribute('class', bad / total >= 0.1 ? 'bad' : 'some');
      svg.append(r);
    }
  });
  return svg;
}

// last_bad_t is the end of the hour bucket that last saw an affected aircraft, so it can be ahead of now
function lastAffected(fmt, t) {
  if (!t) return 'None in the window';
  const span = `${fmt.utc(t - 3600).slice(0, -4)} to ${fmt.clock(t).slice(0, 5)} UTC`;
  return t > Date.now() / 1000 ? `${span} (this hour)` : `${span} (${fmt.ago(t)})`;
}

function cellName(fmt, lat0, lon0, size) {
  const ns = (v) => `${Math.abs(v).toFixed(1)}° ${v >= 0 ? 'N' : 'S'}`;
  const ew = (v) => `${Math.abs(v).toFixed(1)}° ${v >= 0 ? 'E' : 'W'}`;
  return `${ns(lat0)} to ${ns(lat0 + size)}, ${ew(lon0)} to ${ew(lon0 + size)}`;
}

export async function openCell(lat, lon, { agg = lastAgg, size = null } = {}) {
  const { fmt, h } = ctx;
  const hours = hoursOption();
  const key = `gps_jam:${lat.toFixed(2)}:${lon.toFixed(2)}`;
  const card = ctx.cards.open({ key, kind: 'gps_jam', title: 'GPS interference', subtitle: 'Loading…', icon: '⌁', accent: '#e5534b', sections: [{ text: METHOD }] });
  let d;
  try {
    // a known cell size (map click, saved id) decides the grid; the layer's current grid is only a fallback
    d = await ctx.api.get('/api/flights/gpsjam/cell', { lat: lat.toFixed(4), lon: lon.toFixed(4), hours, agg: size ? (size >= 1 ? 2 : 1) : agg });
  } catch {
    card.update({ subtitle: 'Unavailable', sections: [{ text: 'The GPS interference data could not be loaded right now.' }] });
    return card;
  }
  const lv = d.level === 'red' ? 3 : d.level === 'amber' ? 2 : d.level === 'green' ? 1 : 0;
  const share = d.share != null ? d.share * 100 : null;
  const clat = d.lat0 + d.size / 2;
  const clon = d.lon0 + d.size / 2;
  const badges = lv ? [{ text: `${LEVEL[lv]} interference`, tone: TONE[lv] }] : [{ text: 'Not enough samples', tone: 'grey' }];
  const tr = d.trend;
  if (tr?.label === 'new' || tr?.label === 'worse') {
    badges.push({ text: tr.label === 'new' ? 'New this week' : 'Worse than its week', tone: 'red' });
    if (tr.confidence) badges.push({ text: `Confidence ${tr.confidence}`, tone: 'blue' });
  } else if (tr?.label === 'chronic') badges.push({ text: 'Repeats daily', tone: 'grey' });
  else if (tr?.label === 'learning') badges.push({ text: 'Trend still learning', tone: 'grey' });
  const sections = [];
  if (d.hourly?.length) {
    sections.push({ title: `Aircraft per hour, last ${d.hours} h`, el: h('div', { class: 'flights-spark-wrap' }, sparkline(d.hourly),
      h('div', { class: 'flights-spark-key' }, h('span', { class: 'ok' }, 'Normal'), h('span', { class: 'bad' }, 'Degraded accuracy'))) });
  }
  if (tr) {
    // the cell against its own last 7 days: new and clearly worse cells raise air activity events, chronic ones do not
    sections.push({ title: 'Last 7 days', el: h('div', null, dayChart(tr.days || [], ctx), h('p', { class: 'flights-trend-text' }, tr.text)) });
  }
  sections.push({ rows: [['Cell', cellName(fmt, d.lat0, d.lon0, d.size)], ['Last affected aircraft', lastAffected(fmt, d.last_bad_t)],
    ['Minimum sample', `${d.min_samples} aircraft-hours before a cell is coloured`]] });
  sections.push({ title: 'How this is measured', text: METHOD });
  card.update({
    subtitle: cellName(fmt, d.lat0, d.lon0, d.size), badges,
    block: [{ label: 'Affected share', value: share != null ? fmt.num(share, share < 10 ? 1 : 0) : null, unit: '%' },
      { label: 'Aircraft-hours', value: fmt.int(d.total) }, { label: 'Affected', value: fmt.int(d.bad) },
      { label: 'Window', value: String(d.hours), unit: 'h' }],
    sections,
    entity: { kind: 'gps_jam', id: `gpsjam:${d.lat0}:${d.lon0}:${d.size}`, title: `GPS interference ${share != null ? `${Math.round(share)}%` : ''}`.trim(),
      subtitle: cellName(fmt, d.lat0, d.lon0, d.size), lat: +clat.toFixed(5), lon: +clon.toFixed(5), layer: 'flights.gpsjam', zoom: 7 },
    foot: 'Derived from adsb.fi open data. Not a direct measurement.',
  });
  return card;
}

export function register(c) {
  ctx = c;
  layer = ctx.layers.register({
    id: 'flights.gpsjam', group: 'Conflict', title: 'GPS interference', subtitle: 'Derived from aircraft navigation accuracy',
    description: 'Share of aircraft reporting degraded GPS accuracy, per half-degree cell over a rolling window. A sign of jamming, not a direct measurement.',
    source: 'Derived from adsb.fi open data (personal, non-commercial)', color: '#e5534b', symbol: 'area',
    defaultOn: false, refreshMs: 300000, viewDependent: true,
    options: [
      { id: 'hours', label: 'Window', type: 'select', default: '24', choices: [{ value: '6', label: 'Last 6 h' }, { value: '12', label: 'Last 12 h' }, { value: '24', label: 'Last 24 h' }, { value: '48', label: 'Last 48 h' }] },
      { id: 'low', label: 'Show cells with little interference', type: 'toggle', default: true },
    ],
    legend: [{ color: RED, label: '10% or more affected', shape: 'area' }, { color: AMBER, label: '2 to 10%', shape: 'area' },
      { color: GREEN, label: 'Under 2%', shape: 'area' }],
    async enable() { ensureLayers(); },
    async disable() { ctx.map2.removeSource(SRC); },
    refresh,
  });
  ctx.map2.onClick([FILL], (f, lngLat) => {
    const p = f.properties;
    openCell(p.lat0 + p.size / 2, p.lon0 + p.size / 2, { size: p.size });
  });
  ctx.map2.onHover([FILL], (f) => {
    const p = f.properties;
    const pct = p.total ? (p.bad / p.total) * 100 : 0;
    return `GPS interference ${ctx.fmt.num(pct, pct < 10 ? 1 : 0)}%, ${ctx.fmt.int(p.total)} aircraft-hours`;
  });
  ctx.entities.handler('gps_jam', (e) => {
    const m = /^gpsjam:(-?[\d.]+):(-?[\d.]+):([\d.]+)$/.exec(String(e.id || ''));
    if (m) return openCell(+m[1] + +m[3] / 2, +m[2] + +m[3] / 2, { size: +m[3] });
    if (ctx.dom.validLatLon(e.lat, e.lon)) return openCell(+e.lat, +e.lon);
    return null;
  }, { group: 'Conflict', icon: '⌁' });
  ctx.whatshere.section(async (lat, lon) => {
    if (!ctx.moduleOk('flights')) return null;
    let d;
    try { d = await ctx.api.get('/api/flights/gpsjam/cell', { lat: lat.toFixed(4), lon: lon.toFixed(4), hours: 24 }); } catch { return null; }
    if (!d || !d.total || d.level === 'none') return null;
    const pct = d.share != null ? d.share * 100 : 0;
    return { title: 'GPS interference', rows: [['Last 24 h', `${ctx.fmt.num(pct, pct < 10 ? 1 : 0)}% of ${ctx.fmt.int(d.total)} aircraft-hours affected`],
      ['Level', d.level === 'red' ? 'High' : d.level === 'amber' ? 'Medium' : 'Low']] };
  });
}
