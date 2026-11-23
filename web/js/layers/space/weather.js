// Space module: rain radar (RainViewer), NASA GIBS weather layers and point weather (Open-Meteo).

import { COLOR, addRaster, gibsMeta, GIBS_FALLBACK_BASE, hm, svg, clamp, whatsHereOrder, watchTileErrors } from './common.js';
import { registerProductLayer } from './gibs.js';

export { resolveTime } from './gibs.js';

const RANK = { radar: 50 };
// Weather-group products of the imagery catalogue, in panel order (meteosat.py and imagery.py WEATHER)
const WEATHER = ['precip', 'clouds', 'sst', 'lst', 'aod', 'snow', 'no2', 'so2', 'seaice', 'lightning'];
const RADAR_LEGEND = [
  { color: '#88ddee', label: 'Light' }, { color: '#0077aa', label: 'Moderate' }, { color: '#ffee00', label: 'Heavy' },
  { color: '#ff4400', label: 'Very heavy' }, { color: '#ff77ff', label: 'Extreme or hail' }, { color: '#7fbfff', label: 'Snow' },
];

// ------------------------------------------------------------------ rain radar
const R = { ctx: null, layer: null, host: null, frames: [], idx: -1, playing: false, timer: null, drawer: null, ui: null, opts: null };

function frameId(f) { return `ov-space-radar-${f.time}`; }
function frameUrl(f) {
  const o = R.opts || {};
  return `${R.host}${f.path}/256/{z}/{x}/{y}/2/${o.smooth === false ? 0 : 1}_${o.snow === false ? 0 : 1}.png`;
}

function ensureFrame(f, visible) {
  const { map, map2 } = R.ctx;
  const id = frameId(f);
  if (!map.getSource(id)) {
    map2.addSource(id, { type: 'raster', tiles: [frameUrl(f)], tileSize: 256, maxzoom: 7,
      attribution: '<a href="https://www.rainviewer.com/">Weather data by RainViewer</a>' });
  }
  if (!map.getLayer(id)) {
    addRaster(map, map2, { id, type: 'raster', source: id, layout: { visibility: visible ? 'visible' : 'none' },
      paint: { 'raster-opacity': 0, 'raster-opacity-transition': { duration: 0 }, 'raster-fade-duration': 0 } }, RANK.radar);
  }
}

function showFrame(i) {
  const { map } = R.ctx;
  if (!R.frames.length) return;
  R.idx = clamp(i, 0, R.frames.length - 1);
  const op = (R.opts?.opacity ?? 75) / 100;
  R.frames.forEach((f, k) => {
    const id = frameId(f);
    if (k === R.idx || R.playing) ensureFrame(f, true);
    if (!map.getLayer(id)) return;
    if (k === R.idx) {
      map.setLayoutProperty(id, 'visibility', 'visible');
      map.setPaintProperty(id, 'raster-opacity', op);
    } else {
      map.setPaintProperty(id, 'raster-opacity', 0);
      if (!R.playing) map.setLayoutProperty(id, 'visibility', 'none');
    }
  });
  updateDrawer();
}

function play(on) {
  R.playing = on;
  clearTimeout(R.timer);
  if (on) {
    R.frames.forEach(f => ensureFrame(f, true));   // preload every frame (opacity 0) so playback is smooth
    R.frames.forEach(f => R.ctx.map.setLayoutProperty(frameId(f), 'visibility', 'visible'));
    const step = () => {
      if (!R.playing) return;
      const last = R.idx >= R.frames.length - 1;
      showFrame(last ? 0 : R.idx + 1);
      R.timer = setTimeout(step, R.idx === R.frames.length - 1 ? 1600 : 550);
    };
    R.timer = setTimeout(step, 400);
  } else {
    showFrame(R.idx);
  }
  updateDrawer();
}

function removeFrames(keep = new Set()) {
  const { map, map2 } = R.ctx;
  for (const l of map.getStyle().layers) {
    if (l.id.startsWith('ov-space-radar-') && !keep.has(l.id)) map2.removeSource(l.id);
  }
}

async function loadFrames(signal) {
  const res = await R.ctx.api.get('/api/space/radar', null, { signal });
  const atLatest = R.idx < 0 || R.idx === R.frames.length - 1;
  const current = R.frames[R.idx];
  R.host = res.host;
  R.frames = res.frames;
  removeFrames(new Set(R.frames.map(frameId)));
  let idx = R.frames.length - 1;
  if (!atLatest && current) {
    const k = R.frames.findIndex(f => f.time === current.time);
    if (k >= 0) idx = k;
  }
  R.idx = idx;
  if (R.playing) play(true); else showFrame(idx);
  const newest = R.frames[R.frames.length - 1];
  R.layer.setCount(newest ? `${hm(newest.time)} UTC` : null);
}

function updateDrawer() {
  if (!R.ui) return;
  const { fmt } = R.ctx;
  const f = R.frames[R.idx];
  R.ui.slider.max = String(Math.max(0, R.frames.length - 1));
  R.ui.slider.value = String(Math.max(0, R.idx));
  R.ui.time.textContent = f ? `${hm(f.time)} UTC` : '–';
  R.ui.ago.textContent = f ? fmt.ago(f.time) : '';
  R.ui.play.textContent = R.playing ? 'Pause' : 'Play';
  R.ui.play.setAttribute('aria-pressed', String(R.playing));
}

function openDrawer() {
  const { h, ui } = R.ctx;
  if (R.drawer) return;
  const play_ = h('button', { type: 'button', class: 'btn primary space-radar-play', 'aria-pressed': 'false' }, 'Play');
  const back = h('button', { type: 'button', class: 'btn', title: 'Earlier frame', 'aria-label': 'Earlier frame' }, '‹');
  const fwd = h('button', { type: 'button', class: 'btn', title: 'Later frame', 'aria-label': 'Later frame' }, '›');
  const latest = h('button', { type: 'button', class: 'text-btn' }, 'Latest');
  const slider = h('input', { type: 'range', min: 0, max: 0, step: 1, value: 0, class: 'space-radar-slider', 'aria-label': 'Radar frame' });
  const time = h('strong', { class: 'space-radar-time' }, '–');
  const ago = h('span', { class: 'faint' }, '');
  play_.addEventListener('click', () => play(!R.playing));
  back.addEventListener('click', () => { play(false); showFrame(R.idx - 1); });
  fwd.addEventListener('click', () => { play(false); showFrame(R.idx + 1); });
  latest.addEventListener('click', () => { play(false); showFrame(R.frames.length - 1); });
  slider.addEventListener('input', () => { play(false); showFrame(+slider.value); });
  const body = h('div', { class: 'space-radar' },
    h('div', { class: 'space-radar-row' }, play_, back, fwd, h('div', { class: 'space-radar-when' }, time, ago), latest),
    slider,
    h('div', { class: 'space-radar-scale', 'aria-hidden': 'true' }, h('span', null, '2 h ago'), h('span', null, 'now')),
    h('div', { class: 'legend space-legend' }, RADAR_LEGEND.map(l => h('span', null, h('i', { style: { background: l.color } }), l.label))),
    h('p', { class: 'space-credit' }, h('a', { href: 'https://www.rainviewer.com/' }, 'Weather data by RainViewer'),
      '. Detail stops at zoom 7; closer in, tiles are enlarged.'));
  R.ui = { play: play_, slider, time, ago };
  R.drawer = ui.drawer({ id: 'space-radar', title: 'Rain radar', el: body, width: 340,
    onClose: () => { R.drawer = null; R.ui = null; if (R.layer.isOn()) R.ctx.layers.setOn('space.radar', false); } });
  updateDrawer();
}

function registerRadar(ctx) {
  R.ctx = ctx;
  watchTileErrors(ctx.map, 'ov-space-radar-', () => R.layer?.isOn() && R.layer.setStatus('warn', 'Some radar tiles did not load from RainViewer.'));
  R.layer = ctx.layers.register({
    id: 'space.radar', group: 'Weather', title: 'Rain radar', subtitle: 'Past two hours, every 10 minutes',
    description: 'Radar mosaic from more than 1,000 weather radars. Best over Europe, North America, Japan and Australia; most oceans and much of Africa have no radar. Play it to see which way the rain is moving.',
    source: 'Weather data by RainViewer', color: COLOR.weather, symbol: 'area', refreshMs: 5 * 60000,
    options: [
      { id: 'opacity', label: 'Opacity', type: 'range', min: 20, max: 100, step: 5, default: 75, format: v => `${v}%`, refresh: false },
      { id: 'snow', label: 'Snow in its own colours', type: 'toggle', default: true, refresh: false },
      { id: 'smooth', label: 'Smooth edges', type: 'toggle', default: true, refresh: false },
    ],
    legend: RADAR_LEGEND.map(l => ({ ...l, shape: 'area' })),
    async enable({ options }) {
      R.opts = options;
      R.idx = -1;
      R.frames = [];
      openDrawer();
    },
    async refresh({ signal }) {
      await loadFrames(signal);
    },
    async disable() {
      play(false);
      removeFrames();
      R.frames = [];
      R.idx = -1;
      if (R.drawer) { const d = R.drawer; R.drawer = null; R.ui = null; d.close(); }
    },
    onOption(id, value, all) {
      R.opts = all;
      if (!R.layer.isOn()) return;
      if (id === 'opacity') { showFrame(R.idx); return; }
      removeFrames();
      showFrame(R.idx);
      if (R.playing) play(true);
    },
  });
}

// ------------------------------------------------------------------ point weather (What's here)
const TEMP_COLOR = '#d95926';     // dataviz dark slot 2, validated on the card surface #16213a
const RAIN_COLOR = '#3987e5';     // dataviz dark slot 1

function localHour(t, offset) { const d = new Date((t + offset) * 1000); return d.getUTCHours(); }
function localHm(t, offset) {
  const d = new Date((t + offset) * 1000);
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
}
const n0 = (v) => (v == null ? '–' : Math.round(v).toLocaleString('en-GB'));
const n1 = (v) => (v == null ? '–' : (+v).toFixed(1));

function weatherChart(ctx, w) {
  const { h } = ctx;
  const hours = w.hourly || [];
  const off = w.utc_offset_s || 0;
  const W = 340; const padL = 8; const padR = 8;
  // rows: temperature title and max label (y 8), temperature band, min label (tBot + 13),
  // precipitation title and max-rain label (pTop - 3), precipitation band, hour axis
  const tTop = 14; const tBot = 62; const pTop = 90; const pBot = 114; const axisY = 128;
  const temps = hours.map(x => x.temp_c).filter(v => v != null);
  if (hours.length < 2 || !temps.length) return null;
  let lo = Math.min(...temps); let hi = Math.max(...temps);
  if (hi - lo < 4) { const mid = (hi + lo) / 2; lo = mid - 2; hi = mid + 2; }
  const x = (i) => padL + (i * (W - padL - padR)) / (hours.length - 1);
  const yT = (v) => tBot - ((v - lo) / (hi - lo)) * (tBot - tTop);
  const rain = hours.map(x => x.precip_mm || 0);
  const rMax = Math.max(1, ...rain);
  const yP = (v) => pBot - (v / rMax) * (pBot - pTop);
  const root = svg('svg', { viewBox: `0 0 ${W} 136`, class: 'space-wx-chart', role: 'img',
    'aria-label': `Next 24 hours: ${n0(Math.min(...temps))} to ${n0(Math.max(...temps))} °C, ${n1(rain.reduce((a, b) => a + b, 0))} mm of precipitation` });
  // baselines (hairlines)
  root.append(svg('line', { x1: padL, x2: W - padR, y1: tBot + 0.5, y2: tBot + 0.5, class: 'space-wx-base' }));
  root.append(svg('line', { x1: padL, x2: W - padR, y1: pBot + 0.5, y2: pBot + 0.5, class: 'space-wx-base' }));
  // temperature line
  let d = '';
  hours.forEach((hh, i) => { if (hh.temp_c != null) d += `${d ? 'L' : 'M'}${x(i).toFixed(1)},${yT(hh.temp_c).toFixed(1)}`; });
  root.append(svg('path', { d, fill: 'none', stroke: TEMP_COLOR, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  // selective labels: the warmest and coldest hour
  const iMax = hours.findIndex(hh => hh.temp_c === Math.max(...temps));
  const iMin = hours.findIndex(hh => hh.temp_c === Math.min(...temps));
  for (const [i, below] of [[iMax, false], [iMin, true]]) {
    if (i < 0) continue;
    const anchor = i < 3 ? 'start' : i > hours.length - 4 ? 'end' : 'middle';
    root.append(svg('circle', { cx: x(i), cy: yT(hours[i].temp_c), r: 3.5, fill: TEMP_COLOR, class: 'space-wx-dot' }));
    root.append(svg('text', { x: x(i), y: yT(hours[i].temp_c) + (below ? 13 : -6), 'text-anchor': anchor, class: 'space-wx-label' }, `${n0(hours[i].temp_c)}°`));
  }
  // precipitation columns, 4 px rounded data end, square at the baseline
  const slot = (W - padL - padR) / hours.length;
  const bw = Math.min(10, slot * 0.6);
  let anyRain = false;
  rain.forEach((v, i) => {
    if (v < 0.05) return;
    anyRain = true;
    const top = yP(v); const left = x(i) - bw / 2; const r = Math.min(2, (pBot - top) / 2, bw / 2);
    root.append(svg('path', { class: 'space-wx-bar', fill: RAIN_COLOR,
      d: `M${left},${pBot} V${top + r} Q${left},${top} ${left + r},${top} H${left + bw - r} Q${left + bw},${top} ${left + bw},${top + r} V${pBot} Z` }));
  });
  let iR = -1;
  if (anyRain) {
    iR = rain.indexOf(Math.max(...rain));
    root.append(svg('text', { x: x(iR), y: yP(rain[iR]) - 3, 'text-anchor': iR < 3 ? 'start' : iR > hours.length - 4 ? 'end' : 'middle', class: 'space-wx-label' }, `${n1(rain[iR])} mm`));
  } else {
    root.append(svg('text', { x: W / 2, y: (pTop + pBot) / 2 + 4, 'text-anchor': 'middle', class: 'space-wx-muted' }, 'No precipitation expected'));
  }
  // axis: local hours every 6 h
  hours.forEach((hh, i) => {
    const lh = localHour(hh.time, off);
    if (lh % 6 === 0) {
      root.append(svg('line', { x1: x(i), x2: x(i), y1: pBot + 1, y2: pBot + 4, class: 'space-wx-tick' }));
      root.append(svg('text', { x: x(i), y: axisY, 'text-anchor': 'middle', class: 'space-wx-axis' }, `${String(lh).padStart(2, '0')}:00`));
    }
  });
  // each axis title shares a row with a max label, so it goes on the other half of the chart
  const titleAt = (i) => (i >= 0 && x(i) < W / 2 ? { x: W - padR, 'text-anchor': 'end' } : { x: padL, 'text-anchor': 'start' });
  root.append(svg('text', { ...titleAt(iMax), y: 8, class: 'space-wx-axis' }, 'Temperature, °C'));
  root.append(svg('text', { ...titleAt(iR), y: pTop - 3, class: 'space-wx-axis' }, 'Precipitation, mm per hour'));
  // hover layer: crosshair snapping to the nearest hour, one tooltip with every reading
  const cross = svg('line', { y1: tTop - 4, y2: pBot, class: 'space-wx-cross', visibility: 'hidden' });
  const dot = svg('circle', { r: 4, fill: TEMP_COLOR, class: 'space-wx-dot', visibility: 'hidden' });
  root.append(cross, dot);
  const tip = h('div', { class: 'space-wx-tip', hidden: true, role: 'status' });
  const wrap = h('div', { class: 'space-wx-wrap', tabindex: '0', 'aria-label': 'Hourly forecast chart. Use the arrow keys to step through the hours.' }, root, tip);
  let cur = -1;
  const show = (i) => {
    cur = clamp(i, 0, hours.length - 1);
    const hh = hours[cur];
    const cx = x(cur);
    cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('visibility', 'visible');
    if (hh.temp_c != null) { dot.setAttribute('cx', cx); dot.setAttribute('cy', yT(hh.temp_c)); dot.setAttribute('visibility', 'visible'); }
    ctx.dom.clear(tip);
    tip.append(
      h('div', { class: 'space-wx-tip-t' }, `${localHm(hh.time, off)} local`),
      h('div', null, h('strong', null, `${n1(hh.temp_c)} °C`), h('span', { class: 'faint' }, `, feels ${n1(hh.feels_c)}`)),
      h('div', null, h('strong', null, `${n1(hh.precip_mm)} mm`), h('span', { class: 'faint' }, hh.precip_prob != null ? `, ${n0(hh.precip_prob)}% chance` : '')),
      h('div', null, h('strong', null, `${n0(hh.wind_kmh)} km/h`), h('span', { class: 'faint' }, `${hh.wind_from ? ` ${hh.wind_from}` : ''}, gusts ${n0(hh.gust_kmh)}`)),
      h('div', { class: 'faint' }, `${n0(hh.cloud_pct)}% cloud, ${hh.text.toLowerCase()}`));
    tip.hidden = false;
    // beside the crosshair, on whichever side has room
    const px = (cx / W) * wrap.clientWidth;
    const tw = tip.offsetWidth || 128;
    tip.style.left = `${px + 10 + tw <= wrap.clientWidth ? px + 10 : Math.max(0, px - 10 - tw)}px`;
  };
  const hide = () => { cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); tip.hidden = true; };
  wrap.addEventListener('pointermove', (e) => {
    const r = root.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    show(Math.round(((px - padL) / (W - padL - padR)) * (hours.length - 1)));
  });
  wrap.addEventListener('pointerleave', hide);
  wrap.addEventListener('focus', () => show(cur < 0 ? 0 : cur));
  wrap.addEventListener('blur', hide);
  wrap.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowRight') { e.preventDefault(); show(cur + 1); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); show(cur - 1); }
    if (e.key === 'Escape') hide();
  });
  return wrap;
}

function hourlyTable(ctx, w) {
  const { h } = ctx;
  const off = w.utc_offset_s || 0;
  const rows = (w.hourly || []).filter((_, i) => i % 3 === 0);
  return h('details', { class: 'space-wx-details' }, h('summary', null, 'Hourly table'),
    h('table', { class: 'tbl space-wx-table' },
      h('tr', null, h('th', null, 'Local'), h('th', null, 'Weather'), h('th', { class: 'num' }, '°C'), h('th', { class: 'num' }, 'mm'), h('th', { class: 'num' }, 'Wind')),
      rows.map(hh => h('tr', null, h('td', { class: 'num' }, localHm(hh.time, off)), h('td', null, hh.text),
        h('td', { class: 'num' }, n1(hh.temp_c)), h('td', { class: 'num' }, n1(hh.precip_mm)),
        h('td', { class: 'num' }, `${n0(hh.wind_kmh)}${hh.wind_from ? ` ${hh.wind_from}` : ''}`)))));
}

export function weatherEl(ctx, w) {
  const { h } = ctx;
  const c = w.current;
  const s = w.summary;
  const wind = c.wind_kmh != null ? `${n0(c.wind_kmh)} km/h${c.wind_from ? ` from ${c.wind_from}` : ''}${c.gust_kmh != null ? `, gusts ${n0(c.gust_kmh)}` : ''}` : '–';
  const nextBits = [];
  if (s.min_c != null) nextBits.push(`${n0(s.min_c)} to ${n0(s.max_c)} °C`);
  nextBits.push(s.precip_total_mm > 0 ? `${n1(s.precip_total_mm)} mm of precipitation${s.first_rain ? ` from ${localHm(s.first_rain, w.utc_offset_s || 0)}` : ''}` : 'dry');
  if (s.max_gust_kmh != null) nextBits.push(`gusts up to ${n0(s.max_gust_kmh)} km/h`);
  const chart = weatherChart(ctx, w);
  return h('div', { class: 'space-wx' },
    h('div', { class: 'space-wx-now' },
      h('span', { class: 'space-wx-temp' }, `${n1(c.temp_c)}`, h('small', null, '°C')),
      h('div', null, h('div', { class: 'space-wx-text' }, `${c.text}${c.is_day ? '' : ', night'}`),
        h('div', { class: 'dim' }, `Feels like ${n1(c.feels_c)} °C`))),
    h('dl', { class: 'kv' },
      h('dt', null, 'Wind'), h('dd', null, wind),
      h('dt', null, 'Precipitation'), h('dd', null, `${n1(c.precip_mm)} mm in the last 15 min`),
      h('dt', null, 'Cloud and humidity'), h('dd', null, `${n0(c.cloud_pct)}% cloud, ${n0(c.humidity_pct)}% humidity`),
      h('dt', null, 'Pressure'), h('dd', null, c.pressure_hpa != null ? `${n0(c.pressure_hpa)} hPa` : '–'),
      h('dt', null, 'Next 24 hours'), h('dd', null, nextBits.join(', '))),
    chart,
    hourlyTable(ctx, w),
    h('p', { class: 'space-credit' }, h('a', { href: 'https://open-meteo.com/' }, 'Open-Meteo'), ' (CC BY 4.0). ',
      `Local times are ${w.timezone}${w.timezone_abbreviation ? ` (${w.timezone_abbreviation})` : ''}.`));
}

// ------------------------------------------------------------------ register
export async function registerWeather(ctx) {
  registerRadar(ctx);
  // static titles and legends now (instant); dates and times are fetched when a layer is switched on
  let cat = null;
  try { cat = await gibsMeta(ctx.api); } catch { cat = null; }
  ctx._spaceGibsBase = cat?.base || GIBS_FALLBACK_BASE;
  const fallback = {
    precip: { title: 'Precipitation rate', period: 'PT30M' }, clouds: { title: 'Clouds, infrared', period: 'PT10M' },
    sst: { title: 'Sea surface temperature', period: 'P1D' }, lst: { title: 'Land surface temperature, day', period: 'P1D' },
    aod: { title: 'Aerosols (smoke, dust, haze)', period: 'P1D' }, snow: { title: 'Snow cover', period: 'P1D' },
    no2: { title: 'Nitrogen dioxide', period: 'P1D' }, so2: { title: 'Sulphur dioxide', period: 'P1D' },
    seaice: { title: 'Sea ice', period: 'P1D' }, lightning: { title: 'Lightning', period: 'PT5M' },
  };
  for (const pid of WEATHER) {
    try { registerProductLayer(ctx, pid, cat?.products?.[pid] || fallback[pid]); } catch (err) { console.error(`space: layer ${pid} failed`, err); }
  }

  ctx.whatshere.section((lat, lon) => {
    if (!ctx.moduleOk('space')) return null;
    const p = (async () => {
      try {
        const w = await ctx.api.get('/api/space/weather', { lat: lat.toFixed(3), lon: lon.toFixed(3) }, { timeoutMs: 15000 });
        return { title: 'Weather now', el: weatherEl(ctx, w) };
      } catch {
        return { title: 'Weather now', text: 'Weather is unavailable right now (Open-Meteo is not answering).' };
      }
    })();
    whatsHereOrder.weather = p;
    return p;
  });
}
