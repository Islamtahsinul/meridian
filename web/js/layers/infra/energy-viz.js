// Small charts for the electricity views: a 48-hour sparkline (measured solid, forecast dashed, with a
// hover readout), ranked bar lists and diverging flow bars. Plain DOM and SVG, no HTML strings.
// Colour ramps are one-hue steps validated as ordinal ramps against the panel surface.

const NS = 'http://www.w3.org/2000/svg';
export const SURFACE = '#16213a';
export const GOLD = '#c9b458';

export const METRICS = {
  renewables: {
    key: 'r', label: 'Renewable share now', short: 'Renewables', unit: '%',
    breaks: [20, 40, 60, 80], colors: ['#1c5cab', '#2a78d6', '#5598e7', '#86b6ef', '#b7d3f6'],
    legend: ['Under 20%', '20 to 40%', '40 to 60%', '60 to 80%', '80% or more'],
    text: (v) => `${Math.round(v)}%`,
  },
  carbon: {
    key: 'i', label: 'Carbon intensity', short: 'Carbon', unit: 'g/kWh',
    breaks: [100, 200, 300, 450], colors: ['#983d18', '#bf542a', '#e76b3b', '#fe9068', '#fdc0aa'],
    legend: ['Under 100 g', '100 to 200 g', '200 to 300 g', '300 to 450 g', '450 g or more'],
    text: (v) => `${Math.round(v)} g`,
  },
  price: {
    key: 'p', label: 'Day-ahead price now', short: 'Price', unit: '€/MWh',
    breaks: [50, 100, 175, 250], colors: ['#0e6f4c', '#188e63', '#2cae7c', '#3fcf95', '#51f1b0'],
    legend: ['Under €50', '€50 to €100', '€100 to €175', '€175 to €250', '€250 or more'],
    text: (v) => `€${Math.round(v)}`,
  },
};
const OPACITY = [0.17, 0.21, 0.25, 0.30, 0.35];
const MISSING = -9999;

/** MapLibre paint expressions for a metric held in feature-state "v". */
export function fillPaint(metric) {
  const m = METRICS[metric] || METRICS.renewables;
  const v = ['coalesce', ['feature-state', 'v'], MISSING];
  const missing = ['<', v, MISSING + 1];
  const colour = ['step', v, m.colors[0]];
  const opacity = ['step', v, OPACITY[0]];
  m.breaks.forEach((b, i) => { colour.push(b, m.colors[i + 1]); opacity.push(b, OPACITY[i + 1]); });
  return { 'fill-color': ['case', missing, 'rgba(0,0,0,0)', colour], 'fill-opacity': ['case', missing, 0, opacity] };
}

export function classColor(metric, value) {
  const m = METRICS[metric] || METRICS.renewables;
  if (value == null || Number.isNaN(+value)) return null;
  let i = 0;
  while (i < m.breaks.length && +value >= m.breaks[i]) i += 1;
  return m.colors[i];
}

export function legendFor(metric) {
  const m = METRICS[metric] || METRICS.renewables;
  return m.legend.map((label, i) => ({ color: m.colors[i], label, shape: 'area' }));
}

const ukFmt = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', hour: '2-digit', minute: '2-digit', hour12: false });
const ukDay = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', weekday: 'short' });
const toMs = (t) => (t < 1e11 ? t * 1000 : t);
/** "14:30" in UK time (GMT or BST). */
export function ukTime(t) { return t == null ? '–' : ukFmt.format(new Date(toMs(t))); }
export function ukDayTime(t) { return t == null ? '–' : `${ukDay.format(new Date(toMs(t)))} ${ukFmt.format(new Date(toMs(t)))}`; }
/** "14:30 UTC" */
export function utcTime(t) {
  if (t == null) return '–';
  const d = new Date(toMs(t));
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`;
}

function el(tag, attrs = {}) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, String(v));
  return e;
}

/**
 * A sparkline: past points solid with a 10% wash, future points dashed (a forecast), a "now" dot.
 * opts: {past: [[t, v]], future: [[t, v]], color, unit, format(v), time(t), max, label, height}
 */
export function sparkline(ctx, { past = [], future = [], color = GOLD, unit = '', format = (v) => String(Math.round(v)),
  time = ukDayTime, max = null, label = '', height = 74, xLabels = null } = {}) {
  const { h } = ctx;
  const W = 400;
  const H = height;
  const padTop = 14;
  const padBottom = 16;
  const all = [...past, ...future];
  const wrap = h('div', { class: 'infra-spark' });
  if (all.length < 2) {
    wrap.append(h('p', { class: 'dim' }, 'Not enough data for a chart yet.'));
    return wrap;
  }
  const t0 = all[0][0];
  const t1 = all[all.length - 1][0];
  const top = max != null ? max : Math.max(...all.map((p) => p[1]), 1) * 1.12;
  const bottom = Math.min(0, ...all.map((p) => p[1]));
  const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * W;
  const y = (v) => padTop + (1 - (v - bottom) / Math.max(1e-9, top - bottom)) * (H - padTop - padBottom);
  const path = (pts) => pts.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('');
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img', 'aria-label': label, preserveAspectRatio: 'xMidYMid meet' });
  svg.append(el('line', { x1: 0, x2: W, y1: y(0), y2: y(0), stroke: '#283a5e', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' }));
  if (past.length > 1) {
    const area = `${path(past)}L${x(past[past.length - 1][0]).toFixed(1)},${y(0).toFixed(1)}L${x(past[0][0]).toFixed(1)},${y(0).toFixed(1)}Z`;
    svg.append(el('path', { d: area, fill: color, 'fill-opacity': 0.1, stroke: 'none' }));
    svg.append(el('path', { d: path(past), fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' }));
  }
  if (future.length > 1) {
    const joined = past.length ? [past[past.length - 1], ...future] : future;
    svg.append(el('path', { d: path(joined), fill: 'none', stroke: color, 'stroke-opacity': 0.85, 'stroke-width': 2, 'stroke-dasharray': '4 3', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' }));
  }
  const nowPt = past.length ? past[past.length - 1] : future[0];
  const nx = x(nowPt[0]);
  svg.append(el('line', { x1: nx, x2: nx, y1: padTop - 4, y2: y(0), stroke: '#3a5080', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' }));
  svg.append(el('circle', { cx: nx, cy: y(nowPt[1]), r: 4, fill: color, stroke: SURFACE, 'stroke-width': 2 }));
  const peak = all.reduce((a, b) => (b[1] > a[1] ? b : a));
  const tick = el('text', { x: 2, y: 10, fill: '#75839f', 'font-size': 10.5 });
  tick.textContent = `Peak ${format(peak[1])}${unit ? ` ${unit}` : ''}`;
  svg.append(tick);
  const labels = (xLabels || [['24 h ago', 0, 'start'], ['Now', 'now', 'middle'], ['In 24 h', W, 'end']])
    .map(([text, at, anchor]) => [text, at === 'now' ? nx : at, anchor])
    .filter(([, at, anchor]) => anchor !== 'middle' || (at > 56 && at < W - 56));   // a centred label never sits on an end label
  for (const [text, at, anchor] of labels) {
    const t = el('text', { x: at, y: H - 3, fill: '#75839f', 'font-size': 10.5, 'text-anchor': anchor });
    t.textContent = text;
    svg.append(t);
  }
  // Hover: a crosshair and a readout of the nearest half hour.
  const cross = el('line', { y1: padTop - 4, y2: y(0), stroke: '#aab4ca', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke', visibility: 'hidden' });
  const dot = el('circle', { r: 4, fill: color, stroke: SURFACE, 'stroke-width': 2, visibility: 'hidden' });
  const hit = el('rect', { x: 0, y: 0, width: W, height: H, fill: 'transparent' });
  svg.append(cross, dot, hit);
  const tip = h('div', { class: 'infra-spark-tip', hidden: true });
  const isFuture = new Set(future.map((p) => p[0]));
  const move = (ev) => {
    const r = svg.getBoundingClientRect();
    if (!r.width) return;
    const tx = t0 + ((ev.clientX - r.left) / r.width) * (t1 - t0);
    let best = all[0];
    for (const p of all) if (Math.abs(p[0] - tx) < Math.abs(best[0] - tx)) best = p;
    const px = x(best[0]);
    cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.setAttribute('visibility', 'visible');
    dot.setAttribute('cx', px); dot.setAttribute('cy', y(best[1])); dot.setAttribute('visibility', 'visible');
    tip.textContent = `${time(best[0])}: ${format(best[1])}${unit ? ` ${unit}` : ''}${isFuture.has(best[0]) ? ', forecast' : ''}`;
    tip.hidden = false;
    const left = Math.min(Math.max(0, (px / W) * r.width - 60), r.width - 150);
    tip.style.left = `${left}px`;
  };
  hit.addEventListener('pointermove', move);
  hit.addEventListener('pointerleave', () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); });
  wrap.append(svg, tip);
  return wrap;
}

/** Ranked rows with one thin bar each: [{label, value, text, note}] (value 0..max). */
export function barList(ctx, rows, { max = 100, color = GOLD, empty = 'Nothing to show.' } = {}) {
  const { h } = ctx;
  if (!rows.length) return h('p', { class: 'dim' }, empty);
  const box = h('div', { class: `infra-bars${rows.some(r => r.note) ? ' has-note' : ''}`, role: 'table' });
  for (const r of rows) {
    const w = Math.max(0, Math.min(100, (+r.value / max) * 100));
    box.append(h('div', { class: 'infra-bar-row', role: 'row' },
      h('span', { class: 'infra-bar-name', role: 'cell' }, r.label),
      h('span', { class: 'infra-bar-track', role: 'cell', 'aria-hidden': 'true' },
        h('i', { style: { width: `${w.toFixed(1)}%`, background: r.color || color } })),
      h('span', { class: 'infra-bar-val tnum', role: 'cell' }, r.text),
      r.note ? h('span', { class: 'infra-bar-note', role: 'cell' }, r.note) : null));
  }
  return box;
}

/** Diverging rows for interconnectors: imports to the right of the centre line, exports to the left. */
export function flowList(ctx, links, { color = GOLD } = {}) {
  const { h, fmt } = ctx;
  if (!links.length) return h('p', { class: 'dim' }, 'No interconnector readings yet.');
  const top = Math.max(...links.map((l) => l.capacity_mw || Math.abs(l.mw)), 1);
  const box = h('div', { class: 'infra-flows', role: 'table' });
  for (const l of links) {
    const share = Math.min(50, (Math.abs(l.mw) / top) * 50);
    const verb = l.direction === 'import' ? 'in' : l.direction === 'export' ? 'out' : 'idle';
    const bar = h('i', { style: { width: `${share.toFixed(1)}%`, background: color,
      left: l.mw >= 0 ? '50%' : `${(50 - share).toFixed(1)}%` } });
    box.append(h('div', { class: 'infra-flow-row', role: 'row', title: `${l.name}: ${fmt.int(Math.abs(l.mw))} MW ${l.direction === 'import' ? 'into Britain' : l.direction === 'export' ? 'out of Britain' : 'idle'}, capacity ${fmt.int(l.capacity_mw)} MW` },
      h('span', { class: 'infra-bar-name', role: 'cell' }, l.name, h('small', null, l.to)),
      h('span', { class: 'infra-flow-track', role: 'cell', 'aria-hidden': 'true' }, bar),
      h('span', { class: 'infra-bar-val tnum', role: 'cell' }, verb === 'idle' ? 'Idle' : `${fmt.int(Math.abs(l.mw))} MW ${verb}`)));
  }
  return box;
}

/** A NESO index ("very high") -> badge tone and sentence-case label. */
export function indexBadge(index) {
  const tones = { 'very low': 'green', low: 'green', moderate: 'grey', high: 'amber', 'very high': 'red' };
  if (!index) return null;
  return { text: `${index.charAt(0).toUpperCase()}${index.slice(1)} carbon`, tone: tones[index] || 'grey' };
}
