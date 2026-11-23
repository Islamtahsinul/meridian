// Space module: the aurora forecast layer (NOAA SWPC OVATION as server-drawn probability tiles, plus
// viewing lines) and the "Aurora chance here tonight" section of What's here.

import { COLOR, addRaster, hm, dayLabel, whatsHereOrder, watchTileErrors, resetTileErrors } from './common.js';

const SRC_FIELD = 'ov-space-aurora-field';
const SRC_LINES = 'ov-space-aurora-lines';
const L_OVER = 'ov-space-aurora-overhead';
const L_HORIZON = 'ov-space-aurora-horizon';
const L_LABEL = 'ov-space-aurora-label';
const RANK = 55;                 // above the weather rasters (clouds, precipitation, radar): aurora is above the clouds
export const AURORA = { line: '#7edc9e', lineDim: '#4cc382' };
// swatches as drawn (the tile palette in aurora.py: one green, lighter and more opaque with the chance)
const LEGEND = [
  { color: 'rgba(36,135,90,.34)', label: '10% chance', shape: 'area' }, { color: 'rgba(66,185,123,.56)', label: '30%', shape: 'area' },
  { color: 'rgba(114,214,151,.67)', label: '50%', shape: 'area' }, { color: 'rgba(189,242,204,.78)', label: '80% or more', shape: 'area' },
  { color: AURORA.line, label: 'May be seen overhead', shape: 'line' },
  { color: AURORA.lineDim, label: 'Low on the horizon (dashed)', shape: 'line' },
];

const AU = { ctx: null, layer: null, meta: null, version: null };

function tileUrl(meta) {
  // MapLibre fills {z}/{x}/{y}; the URL constructor would escape the braces, so build it by hand
  return `${location.origin}/api/space/aurora/tiles/{z}/{x}/{y}.png?v=${encodeURIComponent(meta.version)}`;
}

function applyField(meta, options) {
  const { map, map2 } = AU.ctx;
  const op = (options.opacity ?? 100) / 100;
  if (AU.version !== meta.version || !map.getSource(SRC_FIELD)) {
    // a new forecast gets a new source (setTiles would keep failed tiles around in MapLibre 5.24)
    map2.removeSource(SRC_FIELD);
    map2.addSource(SRC_FIELD, { type: 'raster', tiles: [tileUrl(meta)], tileSize: 256, minzoom: 0, maxzoom: meta.maxzoom || 3,
      attribution: '<a href="https://www.spaceweather.gov/products/aurora-30-minute-forecast">NOAA SWPC OVATION</a>' });
    addRaster(map, map2, { id: SRC_FIELD, type: 'raster', source: SRC_FIELD,
      paint: { 'raster-opacity': op, 'raster-fade-duration': 0, 'raster-resampling': 'linear' } }, RANK);
    AU.version = meta.version;
    resetTileErrors(SRC_FIELD);
  } else if (map.getLayer(SRC_FIELD)) {
    map.setPaintProperty(SRC_FIELD, 'raster-opacity', op);
  }
}

function addLines(options) {
  const { map2 } = AU.ctx;
  map2.addSource(SRC_LINES, { type: 'geojson', data: map2.emptyFC() });
  const vis = options.lines === false ? 'none' : 'visible';
  map2.addLayer({ id: L_HORIZON, type: 'line', source: SRC_LINES, filter: ['==', ['get', 'kind'], 'horizon'],
    layout: { visibility: vis, 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': AURORA.lineDim, 'line-width': ['interpolate', ['linear'], ['zoom'], 1, 1.2, 6, 2], 'line-opacity': 0.9,
      'line-dasharray': [2, 2.5] } }, 'lines');
  map2.addLayer({ id: L_OVER, type: 'line', source: SRC_LINES, filter: ['==', ['get', 'kind'], 'overhead'],
    layout: { visibility: vis, 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': AURORA.line, 'line-width': ['interpolate', ['linear'], ['zoom'], 1, 1.6, 6, 2.6], 'line-opacity': 0.95 } }, 'lines');
  map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC_LINES, minzoom: 1.2,
    layout: { visibility: options.lines === false || options.labels === false ? 'none' : 'visible',
      'symbol-placement': 'line', 'symbol-spacing': 420, 'text-field': ['get', 'label'], 'text-font': ['Open Sans Regular'],
      'text-size': 11, 'text-offset': [0, -0.75], 'text-keep-upright': true, 'text-max-angle': 35, 'text-padding': 8 },
    paint: { 'text-color': '#e4f7ea', 'text-halo-color': COLOR.ink, 'text-halo-width': 1.6 } }, 'top');
}

function countText(meta) {
  return meta.forecast ? `for ${hm(meta.forecast)} UTC` : null;
}

async function refreshAurora({ options, signal }) {
  const meta = await AU.ctx.api.get('/api/space/aurora', null, { signal, timeoutMs: 45000 });
  AU.meta = meta;
  applyField(meta, options);
  AU.ctx.map2.setData(SRC_LINES, meta.lines || AU.ctx.map2.emptyFC());
  AU.layer.setCount(countText(meta));
  const ageMin = meta.forecast ? (Date.now() / 1000 - meta.forecast) / 60 : 0;
  if (meta.error) AU.layer.setStatus('warn', `Showing the last OVATION forecast; the latest did not load (${meta.error}).`);
  else if (ageMin > 60) AU.layer.setStatus('warn', `This OVATION forecast is ${Math.round(ageMin)} minutes old.`);
  else AU.layer.setStatus('ok', `Up to ${meta.north_max}% in the north and ${meta.south_max}% in the south, for ${hm(meta.forecast)} UTC.`);
}

/** Fly to the oval in whichever hemisphere the map is looking at. */
export function showOval(ctx) {
  const c = ctx.map.getCenter();
  const south = c.lat < -10;
  const meta = AU.meta;
  const edge = south ? meta?.south_edge : meta?.north_edge;
  const lat = edge ? (south ? Math.max(-72, edge[0] - 6) : Math.min(72, edge[0] + 6)) : (south ? -64 : 64);
  ctx.map2.flyTo(lat, c.lng, Math.min(Math.max(ctx.map.getZoom(), 1.6), 2.6));     // honours reduced motion
}

// ------------------------------------------------------------------ What's here: aurora chance here tonight
function localTime(t, off) {
  if (t == null) return '–';
  if (off == null) return `${hm(t)} UTC`;
  return hm(t + off);
}

function darkRow(res) {
  const d = res.dark || {};
  const n = res.night || {};
  const off = res.utc_offset_s;
  const zone = off == null ? '' : ' local';
  if (d.polar_night) return 'Dark around the clock (polar night)';
  if (!n.start) return d.returns ? `No dark sky until about ${dayLabel(d.returns, { weekday: false })}` : 'No dark sky at this time of year';
  const range = `${localTime(n.start, off)} to ${n.end ? localTime(n.end, off) : 'after the next day'}${zone}`;
  if (d.dark_now && n.current) return `Dark now, until ${localTime(n.end, off)}${zone}`;
  if (d.dark_now && !n.current) return `Dark now until ${localTime(d.end, off)}; next ${range}`;
  return range;
}

function bestTime(res) {
  if (res.magnetic_midnight_ut == null) return null;
  const now = new Date();
  const base = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) / 1000;
  const t = base + res.magnetic_midnight_ut * 3600;
  return `Around ${localTime(t, res.utc_offset_s)}${res.utc_offset_s == null ? '' : ' local'} (magnetic midnight), give or take two hours`;
}

function neededRow(res) {
  if (res.kp_needed_horizon == null) return null;
  const side = res.hemisphere === 'south' ? 'southern' : 'northern';
  const r = (v) => Math.round(v);
  if (res.kp_needed_overhead <= 0) return 'Any Kp: this is under the auroral oval';
  if (res.kp_needed_horizon <= 0) return `Any Kp low on the ${side} horizon, about ${r(res.kp_needed_overhead)} overhead`;
  const over = res.kp_needed_overhead > 9 ? 'more than 9 overhead' : `about ${r(res.kp_needed_overhead)} overhead`;
  const hor = res.kp_needed_horizon > 9 ? 'more than 9' : `about ${r(res.kp_needed_horizon)}`;
  return `${hor} low on the ${side} horizon, ${over}`;
}

const kpTxt = (v) => (v == null ? '–' : (Math.round(v * 10) / 10).toString());

function hereEl(ctx, res) {
  const { h } = ctx;
  const tone = { good: 'green', possible: 'amber', low: 'grey', unlikely: 'grey', none: 'grey', unknown: 'grey' }[res.chance] || 'grey';
  const word = { good: 'Good chance', possible: 'Possible', low: 'Long shot', unlikely: 'Unlikely', none: 'No chance', unknown: 'No outlook' }[res.chance] || '';
  const ov = res.ovation;
  const rows = [];
  if (ov) {
    rows.push(['OVATION now', `${kpTxt(ov.here_pct)}% here, ${kpTxt(ov.poleward_pct)}% within 330 km towards the pole${ov.time ? ` (for ${hm(ov.time)} UTC)` : ''}`]);
  }
  rows.push(['Dark sky', darkRow(res)]);
  const best = bestTime(res);
  if (best && res.night?.start) rows.push(['Best time', best]);
  if (res.cloud) rows.push(['Cloud', `${res.cloud.pct}% cover in the dark hours (${res.cloud.text}, Open-Meteo)`]);
  else if (res.night?.start) rows.push(['Cloud', res.sources?.weather ? 'No cloud forecast for those hours yet' : 'Cloud forecast unavailable']);
  rows.push(['Kp forecast', `${res.kp_tonight != null ? `up to ${kpTxt(res.kp_tonight)} in the dark hours` : 'unavailable'}${res.kp_now != null ? `, ${kpTxt(res.kp_now)} now` : ''}`]);
  const need = neededRow(res);
  if (need) rows.push(['Kp needed', need]);
  if (res.cgm_lat != null) rows.push(['Magnetic latitude', `${Math.abs(res.cgm_lat).toFixed(1)}° ${res.cgm_lat >= 0 ? 'N' : 'S'} (corrected geomagnetic)`]);
  const dl = h('dl', { class: 'kv space-au-kv' });
  for (const [k, v] of rows) dl.append(h('dt', null, k), h('dd', null, v));
  const on = ctx.layers.isOn('space.aurora');
  const show = h('button', { type: 'button', class: 'text-btn space-inline-btn' }, on ? 'Show the oval' : 'Show the aurora forecast');
  show.addEventListener('click', async () => { if (!ctx.layers.isOn('space.aurora')) await ctx.layers.setOn('space.aurora', true); else showOval(ctx); });
  const panel = h('button', { type: 'button', class: 'text-btn space-inline-btn' }, 'Space weather now');
  panel.addEventListener('click', () => { ctx.layers.setOn('space.spaceweather', true); });
  return h('div', { class: 'space-au' },
    h('div', { class: 'space-au-head' }, h('span', { class: `badge ${tone}` }, word), h('p', { class: 'space-au-sentence' }, res.sentence)),
    dl,
    h('div', { class: 'space-au-actions' }, show, panel),
    h('p', { class: 'space-credit' }, 'OVATION and Kp forecast from ', h('a', { href: 'https://www.spaceweather.gov/products/aurora-30-minute-forecast' }, 'NOAA SWPC'),
      '. Kp needed follows SWPC’s rule of thumb (oval edge near 66 − 2 × Kp degrees of magnetic latitude); dark means the Sun is 12° or more below the horizon.'));
}

// ------------------------------------------------------------------ register
export async function registerAurora(ctx) {
  AU.ctx = ctx;
  watchTileErrors(ctx.map, SRC_FIELD, () => AU.layer?.isOn() && AU.layer.setStatus('warn', 'Some aurora tiles did not load.'));
  AU.layer = ctx.layers.register({
    id: 'space.aurora', group: 'Space', title: 'Aurora forecast', subtitle: 'NOAA OVATION, 30 to 90 minutes ahead',
    description: 'Chance of seeing the aurora overhead, from NOAA’s OVATION model for both hemispheres. The solid line marks where it may be seen overhead (10% or more); the dashed line, about 330 km further from the pole, where it may be seen low on the horizon on a clear, dark night. The lines are Meridian’s estimate. Aurora is only visible in the dark.',
    source: 'NOAA SWPC OVATION (public domain)', color: AURORA.line, symbol: 'area', refreshMs: 5 * 60000,
    options: [
      { id: 'opacity', label: 'Opacity', type: 'range', min: 20, max: 100, step: 5, default: 100, format: v => `${v}%`, refresh: false },
      { id: 'lines', label: 'Viewing lines', type: 'toggle', default: true, refresh: false },
      { id: 'labels', label: 'Line labels', type: 'toggle', default: true, refresh: false },
    ],
    legend: LEGEND,
    async enable({ options }) {
      AU.version = null;
      addLines(options);
    },
    async refresh(args) { await refreshAurora(args); },
    async disable() {
      ctx.map2.removeSource(SRC_FIELD);
      ctx.map2.removeSource(SRC_LINES);
      AU.version = null;
    },
    onOption(id, value, all) {
      if (!AU.layer.isOn()) return;
      if (id === 'opacity' && ctx.map.getLayer(SRC_FIELD)) ctx.map.setPaintProperty(SRC_FIELD, 'raster-opacity', value / 100);
      if (id === 'lines') {
        ctx.map2.setVisible(L_OVER, value !== false);
        ctx.map2.setVisible(L_HORIZON, value !== false);
        ctx.map2.setVisible(L_LABEL, value !== false && all.labels !== false);
      }
      if (id === 'labels') ctx.map2.setVisible(L_LABEL, value !== false && all.lines !== false);
    },
  });
  ctx.map2.onHover([L_OVER, L_HORIZON], (f) => (f.properties.kind === 'overhead'
    ? 'Poleward of this line the aurora may be seen overhead (OVATION 10% or more)'
    : `From here to the solid line the aurora may be seen low on the ${f.properties.hemisphere === 'south' ? 'southern' : 'northern'} horizon`));

  ctx.whatshere.section(async (lat, lon) => {
    if (!ctx.moduleOk('space')) return null;
    if (Math.abs(lat) < 25) return null;          // aurora needs a magnetic latitude the tropics never have
    const first = whatsHereOrder.weather;          // the weather section warms the shared Open-Meteo cache
    if (first) await Promise.race([first, new Promise(r => setTimeout(r, 4000))]);
    let res;
    try { res = await ctx.api.get('/api/space/aurora/here', { lat: lat.toFixed(3), lon: lon.toFixed(3) }, { timeoutMs: 40000 }); } catch { return null; }
    if (!res || !res.relevant) return null;
    return { title: 'Aurora chance here tonight', el: hereEl(ctx, res) };
  });
}
