// Space module: "Space weather now" (NOAA SWPC) as a drawer opened from the toolbar or the layer row,
// space weather event cards (storms, flares, radiation storms, watches) and their entity handler.

import { COLOR, hm, dayLabel, svg, clamp } from './common.js';
import { showOval } from './aurora.js';

const SUN_ICON = 'M10 6.6a3.4 3.4 0 1 1 0 6.8 3.4 3.4 0 0 1 0-6.8ZM10 2.3v2M10 15.7v2M2.3 10h2M15.7 10h2M4.6 4.6 6 6M14 14l1.4 1.4M4.6 15.4 6 14M14 6l1.4-1.4';
const NAME = ['none', 'minor', 'moderate', 'strong', 'severe', 'extreme'];
const FORECAST_BAR = '#6b60b6';   // Space lavender stepped down: validated against #a08ff5 on the drawer surface
const SW = { ctx: null, layer: null, drawer: null, body: null, data: null, btn: null, error: null };

const toneOf = (lvl) => (lvl >= 3 ? 'red' : lvl >= 1 ? 'amber' : 'grey');
const kpTxt = (v) => (v == null ? '–' : String(Math.round(v * 10) / 10));
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const n0 = (v) => (v == null ? '–' : Math.round(v).toLocaleString('en-GB'));
const n1 = (v) => (v == null ? '–' : (Math.round(v * 10) / 10).toFixed(1).replace('-', '−'));   // a true minus sign

/** "today", "tomorrow" or "Wed 25 Nov" (UTC days). */
function dayWord(t) {
  const d = Math.floor(t / 86400) - Math.floor(Date.now() / 86400000);
  return d === 0 ? 'today' : d === 1 ? 'tomorrow' : d === -1 ? 'yesterday' : dayLabel(t);
}

function barColor(s) {
  if (s.g >= 3) return COLOR.red;
  if (s.g >= 1) return COLOR.amber;
  return s.kind === 'observed' ? COLOR.space : FORECAST_BAR;
}

function slotLabel(t) {
  const a = hm(t).slice(0, 2);
  const b = hm(t + 10800).slice(0, 2);
  return `${dayLabel(t)}, ${a}:00 to ${b}:00 UTC`;
}

// ------------------------------------------------------------------ Kp chart (observed and forecast, 3-hour bars)
function kpChart(ctx, kp) {
  const { h } = ctx;
  const series = kp.series || [];
  if (series.length < 4) return null;
  const W = 372; const L = 22; const R = 4; const T = 14; const B = 112; const AX = 126;
  const t0 = series[0].time; const t1 = series[series.length - 1].time + 10800;
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
  const y = (v) => B - (clamp(v, 0, 9) / 9) * (B - T);
  const slot = (W - L - R) / series.length;
  const bw = Math.max(3, Math.min(9, slot - 2));
  const now = Date.now() / 1000;
  const root = svg('svg', { viewBox: `0 0 ${W} 134`, class: 'space-sw-chart', role: 'img',
    'aria-label': `Kp, past 36 hours and next three days. Now ${kpTxt(kp.now?.kp)}, forecast up to ${kpTxt(kp.forecast_max?.kp)}.` });
  // recessive grid: baseline, storm threshold (Kp 5 = G1), axis labels
  root.append(svg('line', { x1: L, x2: W - R, y1: B + 0.5, y2: B + 0.5, class: 'space-sw-base' }));
  root.append(svg('line', { x1: L, x2: W - R, y1: y(5) + 0.5, y2: y(5) + 0.5, class: 'space-sw-grid' }));
  for (const v of [0, 5, 9]) root.append(svg('text', { x: L - 6, y: y(v) + 3.5, 'text-anchor': 'end', class: 'space-sw-axis' }, String(v)));
  root.append(svg('text', { x: W - R, y: y(5) - 3, 'text-anchor': 'end', class: 'space-sw-axis' }, 'Storm level (G1)'));
  // day ticks at 00 UTC
  const d0 = Math.ceil(t0 / 86400) * 86400;
  for (let t = d0; t < t1; t += 86400) {
    root.append(svg('line', { x1: x(t), x2: x(t), y1: B + 1, y2: B + 5, class: 'space-sw-tick' }));
    if (x(t) < W - 40) root.append(svg('text', { x: x(t) + 3, y: AX, class: 'space-sw-axis' }, dayLabel(t)));
  }
  // bars: 2 px rounded data end, square at the baseline, 2 px surface gap between neighbours
  let iMax = -1;
  series.forEach((s, i) => {
    if (s.kind !== 'observed' && s.time + 10800 > now && (iMax < 0 || s.kp > series[iMax].kp)) iMax = i;
    const cx = x(s.time + 5400);
    const top = y(Math.max(s.kp, 0.12));
    const left = cx - bw / 2;
    const r = Math.min(2, bw / 2, (B - top) / 2);
    root.append(svg('path', { class: 'space-sw-bar', fill: barColor(s), opacity: s.kind !== 'observed' && s.g >= 1 ? 0.7 : 1,
      d: `M${left},${B} V${top + r} Q${left},${top} ${left + r},${top} H${left + bw - r} Q${left + bw},${top} ${left + bw},${top + r} V${B} Z` }));
  });
  // label each run of storm-level bars once, at its peak (selective labels, never one per bar)
  for (let i = 0; i < series.length;) {
    if (series[i].g < 1) { i += 1; continue; }
    let j = i;
    let peak = i;
    while (j < series.length && series[j].g >= 1) { if (series[j].kp > series[peak].kp) peak = j; j += 1; }
    const s = series[peak];
    root.append(svg('text', { x: clamp(x(s.time + 5400), L + 8, W - R - 8), y: y(s.kp) - 4, 'text-anchor': 'middle', class: 'space-sw-label' }, `G${s.g}`));
    i = j;
  }
  if (iMax >= 0 && series[iMax].g < 1) {
    const s = series[iMax];
    root.append(svg('text', { x: clamp(x(s.time + 5400), L + 8, W - R - 8), y: y(s.kp) - 4, 'text-anchor': 'middle', class: 'space-sw-label' }, kpTxt(s.kp)));
  }
  // now marker
  if (now > t0 && now < t1) {
    root.append(svg('line', { x1: x(now), x2: x(now), y1: T - 6, y2: B, class: 'space-sw-now' }));
    root.append(svg('text', { x: x(now), y: T - 8, 'text-anchor': 'middle', class: 'space-sw-axis' }, 'Now'));
  }
  // hover and keyboard readout: one tooltip per 3-hour bar
  const cross = svg('rect', { y: T - 4, height: B - T + 4, width: slot, class: 'space-sw-hover', visibility: 'hidden' });
  root.append(cross);
  const tip = h('div', { class: 'space-sw-tip', hidden: true, role: 'status' });
  const wrap = h('div', { class: 'space-sw-chartwrap', tabindex: '0', 'aria-label': 'Kp chart. Use the arrow keys to step through the 3-hour periods.' }, root, tip);
  let cur = -1;
  const show = (i) => {
    cur = clamp(i, 0, series.length - 1);
    const s = series[cur];
    cross.setAttribute('x', x(s.time)); cross.setAttribute('width', slot); cross.setAttribute('visibility', 'visible');
    ctx.dom.clear(tip);
    tip.append(h('div', { class: 'space-sw-tip-t' }, slotLabel(s.time)),
      h('div', null, h('strong', null, `Kp ${kpTxt(s.kp)}`), h('span', { class: 'faint' }, s.kind === 'observed' ? ', observed' : ', forecast')),
      h('div', { class: 'faint' }, s.g ? `${cap(NAME[s.g])} storm (G${s.g})` : s.kp >= 4 ? 'Active, below storm level' : 'Below storm level'));
    tip.hidden = false;
    const px = (x(s.time + 5400) / W) * wrap.clientWidth;
    const tw = tip.offsetWidth || 150;
    tip.style.left = `${px + 12 + tw <= wrap.clientWidth ? px + 12 : Math.max(0, px - 12 - tw)}px`;
  };
  const hide = () => { cross.setAttribute('visibility', 'hidden'); tip.hidden = true; };
  wrap.addEventListener('pointermove', (e) => {
    const r = root.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    show(Math.floor((px - L) / slot));
  });
  wrap.addEventListener('pointerleave', hide);
  wrap.addEventListener('focus', () => show(cur < 0 ? series.findIndex(s => s.time + 10800 > now) : cur));
  wrap.addEventListener('blur', hide);
  wrap.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowRight') { e.preventDefault(); show(cur + 1); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); show(cur - 1); }
    if (e.key === 'Escape') hide();
  });
  const legend = h('div', { class: 'legend space-legend' },
    h('span', null, h('i', { class: 'area', style: { background: COLOR.space } }), 'Observed'),
    h('span', null, h('i', { class: 'area', style: { background: FORECAST_BAR } }), 'Forecast'),
    h('span', null, h('i', { class: 'area', style: { background: COLOR.amber } }), 'Storm G1 to G2'),
    h('span', null, h('i', { class: 'area', style: { background: COLOR.red } }), 'G3 or more'));
  const table = h('details', { class: 'space-wx-details' }, h('summary', null, 'Kp table'),
    h('table', { class: 'tbl space-wx-table' },
      h('tr', null, h('th', null, '3 hours from (UTC)'), h('th', { class: 'num' }, 'Kp'), h('th', null, 'Kind'), h('th', null, 'Scale')),
      series.map(s => h('tr', null, h('td', { class: 'num' }, `${dayLabel(s.time)} ${hm(s.time)}`), h('td', { class: 'num' }, kpTxt(s.kp)),
        h('td', null, s.kind === 'observed' ? 'Observed' : 'Forecast'), h('td', null, s.g ? `G${s.g}` : '–')))));
  return h('div', null, wrap, legend, table);
}

// ------------------------------------------------------------------ sparkline for the solar wind tiles
function spark(series, idx, { zero = false } = {}) {
  const pts = series.filter(r => r[idx] != null);
  if (pts.length < 3) return null;
  const W = 156; const H = 34; const P = 4;
  const t0 = pts[0][0]; const t1 = pts[pts.length - 1][0];
  let lo = Math.min(...pts.map(r => r[idx])); let hi = Math.max(...pts.map(r => r[idx]));
  if (zero) { lo = Math.min(lo, -1); hi = Math.max(hi, 1); }
  if (hi - lo < 1e-6) { hi += 1; lo -= 1; }
  const x = (t) => P + ((t - t0) / Math.max(1, t1 - t0)) * (W - 2 * P - 4);
  const y = (v) => H - P - ((v - lo) / (hi - lo)) * (H - 2 * P);
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'space-sw-spark', 'aria-hidden': 'true' });
  if (zero) root.append(svg('line', { x1: P, x2: W - P, y1: y(0) + 0.5, y2: y(0) + 0.5, class: 'space-sw-base' }));
  let d = '';
  for (const r of pts) d += `${d ? 'L' : 'M'}${x(r[0]).toFixed(1)},${y(r[idx]).toFixed(1)}`;
  root.append(svg('path', { d, fill: 'none', stroke: COLOR.space, 'stroke-width': 1.6, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  const last = pts[pts.length - 1];
  root.append(svg('circle', { cx: x(last[0]), cy: y(last[idx]), r: 3, fill: COLOR.space, class: 'space-sw-dot' }));
  return root;
}

function windWords(w) {
  if (!w || w.speed == null) return null;
  const sp = w.speed < 400 ? 'Slow' : w.speed < 550 ? 'Moderate' : w.speed < 750 ? 'Fast' : 'Very fast';
  let bz = '';
  if (w.bz != null) {
    const steady = w.minutes >= 20 && w.south_minutes != null ? w.south_minutes / w.minutes : null;
    if (w.bz <= -10) bz = 'a strongly southward magnetic field, which drives aurora and storms';
    else if (w.bz <= -5) bz = 'a southward magnetic field that helps the aurora';
    else if (w.bz < 0) bz = steady != null && steady > 0.7 ? 'a weak but mostly southward magnetic field' : 'a weak magnetic field';
    else bz = w.bz >= 5 ? 'a northward magnetic field, which keeps the aurora quiet' : 'a weak, northward magnetic field';
  }
  return `${sp} solar wind${bz ? ` with ${bz}` : ''}.`;
}

// ------------------------------------------------------------------ sections
function section(ctx, title, ...children) {
  const { h } = ctx;
  return h('section', { class: 'space-sw-sec' }, title ? h('h3', null, title) : null, ...children);
}

function blockEl(ctx, d) {
  const { h } = ctx;
  const kp = d.kp?.now;
  const w = d.wind || {};
  const x = d.xray || {};
  const cell = (v, unit, label) => h('div', null, h('span', { class: 'v' }, v == null || v === '' ? '–' : String(v), unit && v != null ? h('small', null, unit) : null), h('span', { class: 'k' }, label));
  return h('div', { class: 'data-block space-sw-block' },
    cell(kp ? kpTxt(kp.kp) : null, null, 'Kp now'),
    cell(w.speed != null ? n0(w.speed) : null, 'km/s', 'Solar wind'),
    cell(w.bz != null ? n1(w.bz) : null, 'nT', w.bz == null ? 'Bz' : `Bz, ${w.bz < 0 ? 'south' : 'north'}`),
    cell(x.now_class || null, null, 'X-rays now'));
}

function scalesEl(ctx, d) {
  const { h } = ctx;
  const sc = d.scales;
  if (!sc) return section(ctx, 'NOAA space weather scales', h('p', { class: 'dim' }, 'The NOAA scales are unavailable right now.'));
  const max = sc.max_24h || {};
  const tiles = [['G', 'Geomagnetic storms'], ['S', 'Solar radiation'], ['R', 'Radio blackouts']].map(([k, name]) => {
    const lvl = sc.now?.[k]?.level ?? 0;
    const m24 = max[k]?.level ?? 0;
    return h('div', { class: `space-sw-scale ${toneOf(lvl)}` },
      h('span', { class: `badge ${toneOf(lvl)} space-sw-lvl` }, `${k}${lvl}`),
      h('div', { class: 'space-sw-scale-t' }, name),
      h('div', { class: 'space-sw-scale-s' }, lvl ? `${cap(NAME[lvl])} now` : 'None now'),
      h('div', { class: 'space-sw-scale-s faint' }, `Past 24 hours: ${k}${m24}`));
  });
  const fc = sc.forecast || [];
  const dayName = (f, i) => (i === 0 ? 'Today' : dayLabel(Date.parse(`${f.date}T12:00:00Z`)).split(' ').slice(0, 2).join(' '));
  const pct = (v) => (v == null ? '–' : `${v}%`);
  const table = fc.length ? h('table', { class: 'tbl space-sw-fc' },
    h('tr', null, h('th', null, 'Forecast'), fc.map((f, i) => h('th', { class: 'num' }, dayName(f, i)))),
    h('tr', null, h('td', null, 'Geomagnetic storm'), fc.map(f => h('td', { class: 'num' }, f.G?.level ? `G${f.G.level}` : 'None'))),
    h('tr', null, h('td', null, 'Radiation storm, S1 or more'), fc.map(f => h('td', { class: 'num' }, pct(f.S?.prob)))),
    h('tr', null, h('td', null, 'Radio blackout, R1 to R2'), fc.map(f => h('td', { class: 'num' }, pct(f.R?.minor_prob)))),
    h('tr', null, h('td', null, 'Radio blackout, R3 or more'), fc.map(f => h('td', { class: 'num' }, pct(f.R?.major_prob))))) : null;
  return section(ctx, 'NOAA space weather scales', h('div', { class: 'space-sw-scales' }, tiles), table);
}

function kpEl(ctx, d) {
  const { h } = ctx;
  const kp = d.kp || {};
  const bits = [];
  if (kp.last) bits.push(`Last 3 hours ${kpTxt(kp.last.kp)}`);
  if (kp.max_24h) bits.push(`past day up to ${kpTxt(kp.max_24h.kp)}`);
  if (kp.forecast_max) {
    const w = dayWord(kp.forecast_max.time);
    bits.push(`forecast up to ${kpTxt(kp.forecast_max.kp)}${kp.forecast_max.g ? ` (G${kp.forecast_max.g})` : ''} ${/^\d|^[A-Z]/.test(w) ? `on ${w}` : w}`);
  }
  const chart = kpChart(ctx, kp);
  return section(ctx, 'Kp index, past 36 hours and next 3 days',
    h('p', { class: 'space-sw-note' }, bits.length ? `${bits.join(', ')}.` : 'No Kp values right now.'),
    chart || h('p', { class: 'dim' }, 'The Kp forecast is unavailable right now.'));
}

function windEl(ctx, d) {
  const { h } = ctx;
  const w = d.wind;
  if (!w) return section(ctx, 'Solar wind', h('p', { class: 'dim' }, 'Solar wind data are unavailable right now.'));
  const series = w.series || [];
  const tile = (label, value, unit, sp) => h('div', { class: 'space-sw-wtile' },
    h('div', { class: 'space-sw-wv' }, value, h('small', null, unit)), h('div', { class: 'space-sw-wk' }, label), sp);
  const south = w.south_minutes != null && w.minutes ? `south for ${w.south_minutes} of the last ${w.minutes} min` : (w.bz != null ? (w.bz < 0 ? 'southward' : 'northward') : '');
  const meta = [];
  if (w.density != null) meta.push(`density ${n1(w.density)} per cm³`);
  if (w.bt != null) meta.push(`total field ${n1(w.bt)} nT`);
  const when = w.time ? `Measured at L1, 1.5 million km sunward, at ${hm(w.time)} UTC${w.arrives ? `; reaches Earth around ${hm(w.arrives)} UTC` : ''}.` : '';
  return section(ctx, 'Solar wind',
    h('div', { class: 'space-sw-wind' },
      tile('Speed, last hour', n0(w.speed), 'km/s', spark(series, 1)),
      tile(`Bz, ${south || 'north-south field'}`, n1(w.bz), 'nT', spark(series, 2, { zero: true }))),
    h('p', { class: 'space-sw-note' }, [windWords(w), meta.length ? ` ${cap(meta.join(', '))}.` : ''].join('')),
    when ? h('p', { class: 'space-sw-meta' }, when) : null);
}

function flareRow(ctx, f) {
  const { h } = ctx;
  const letter = f.class[0];
  const tone = f.r >= 3 ? 'red' : f.r >= 1 ? 'amber' : 'grey';
  return h('li', null, h('span', { class: `badge ${tone} space-sw-class` }, f.class),
    h('span', { class: 'space-sw-li-t' }, `${dayLabel(f.max)}, peak ${hm(f.max)} UTC${f.end == null ? ', still going' : ''}`),
    h('span', { class: 'space-sw-li-s' }, letter === 'C' && !f.r ? 'Small flare, no effect on Earth' : f.impact));
}

function flaresEl(ctx, d) {
  const { h } = ctx;
  const x = d.xray;
  if (!x) return section(ctx, 'Solar flares', h('p', { class: 'dim' }, 'GOES X-ray data are unavailable right now.'));
  const lvl = x.now_class ? x.now_class[0] : null;
  const word = { A: 'very quiet', B: 'quiet', C: 'slightly raised', M: 'high: a strong flare', X: 'very high: a major flare' }[lvl] || '';
  const items = [];
  if (x.ongoing) items.push(x.ongoing);
  for (const f of x.recent || []) if (!items.some(g => Math.abs(g.max - f.max) < 600)) items.push(f);
  const big = x.biggest_7d;
  const c = x.counts_7d || {};
  return section(ctx, 'Solar flares',
    h('p', { class: 'space-sw-note' }, x.now_class ? `X-ray level now ${x.now_class}${word ? `, ${word}` : ''}.` : 'No current X-ray level.'),
    items.length ? h('ul', { class: 'space-sw-list' }, items.slice(0, 6).map(f => flareRow(ctx, f)))
      : h('p', { class: 'dim' }, 'No flares of C class or above in the last three days.'),
    h('p', { class: 'space-sw-meta' }, `Past week: ${c.C || 0} C, ${c.M || 0} M and ${c.X || 0} X flares${big ? `; the biggest, ${big.class}, peaked on ${dayLabel(big.max)} at ${hm(big.max)} UTC` : ''}. M and X flares black out HF radio on the sunlit side.`));
}

function alertItem(ctx, a) {
  const { h } = ctx;
  const tone = toneOf(a.level || 0);
  const kind = { warning: 'Warning', watch: 'Watch', alert: 'Alert', summary: 'Summary' }[a.type] || 'Message';
  const when = [`${kind}, issued ${dayLabel(a.issued)} ${hm(a.issued)} UTC`];
  if (a.end && a.type !== 'alert' && a.type !== 'summary') when.push(`in force until ${dayLabel(a.end)} ${hm(a.end)} UTC`);
  const aurora = (a.impacts || []).find(t => /^Aurora\b/.test(t));
  return h('li', { class: `space-sw-alert ${tone}` },
    h('div', { class: 'space-sw-alert-h' }, a.scale ? h('span', { class: `badge ${tone}` }, a.scale) : null, h('strong', null, a.title)),
    a.detail ? h('div', { class: 'space-sw-alert-d' }, a.detail) : null,
    aurora ? h('div', { class: 'space-sw-alert-d faint' }, aurora.replace(/^Aurora\s*-\s*/, '')) : null,
    h('div', { class: 'space-sw-alert-m' }, when.join(', ')),
    h('details', { class: 'space-sw-raw' }, h('summary', null, 'SWPC message'), h('pre', null, a.message || '')));
}

function alertsEl(ctx, d) {
  const { h } = ctx;
  const al = d.alerts;
  if (!al) return section(ctx, 'SWPC alerts, watches and warnings', h('p', { class: 'dim' }, 'SWPC alerts are unavailable right now.'));
  const active = al.active || [];
  const recent = al.recent || [];
  return section(ctx, 'SWPC alerts, watches and warnings',
    active.length ? h('ul', { class: 'space-sw-alerts' }, active.map(a => alertItem(ctx, a)))
      : h('p', { class: 'space-sw-note' }, 'None in force.'),
    recent.length ? h('details', { class: 'space-wx-details' }, h('summary', null, `Earlier this week (${recent.length})`),
      h('ul', { class: 'space-sw-list space-sw-recent' }, recent.map(a => h('li', null,
        h('span', { class: 'space-sw-li-t' }, a.title), h('span', { class: 'space-sw-li-s' }, `${dayLabel(a.issued)} ${hm(a.issued)} UTC`))))) : null);
}

function auroraEl(ctx, d) {
  const { h } = ctx;
  const au = d.aurora;
  const btn = h('button', { type: 'button', class: 'btn' }, ctx.layers.isOn('space.aurora') ? 'Show the oval' : 'Show the aurora forecast');
  btn.addEventListener('click', async () => {
    if (!ctx.layers.isOn('space.aurora')) await ctx.layers.setOn('space.aurora', true);
    showOval(ctx);
  });
  const text = au ? `OVATION gives up to ${au.north_max}% in the north and ${au.south_max}% in the south, for ${hm(au.forecast)} UTC. Right-click any place and choose What’s here for the chance there tonight.`
    : 'Turn on the aurora forecast to see where it is now. Right-click any place and choose What’s here for the chance there tonight.';
  return section(ctx, 'Aurora', h('p', { class: 'space-sw-note' }, text), btn);
}

function footEl(ctx, d) {
  const { h } = ctx;
  const bad = Object.entries(d.feeds || {}).filter(([k, v]) => k !== 'ovation' && !v.ok);
  return h('section', { class: 'space-sw-sec space-sw-foot' },
    h('div', { class: 'space-sw-links' }, (d.links || []).map(l => h('a', { href: l.href }, l.text))),
    bad.length ? h('p', { class: 'space-sw-meta' }, `Not updating right now: ${bad.map(([k]) => k.replace('_', ' ')).join(', ')}.`) : null,
    h('p', { class: 'space-credit' }, 'Data from ', h('a', { href: 'https://www.spaceweather.gov/' }, 'NOAA Space Weather Prediction Center'),
      ` (public domain). Updated ${hm(d.time)} UTC; SWPC refreshes most products every 1 to 5 minutes.`));
}

function render(d) {
  const ctx = SW.ctx;
  const { h, dom } = ctx;
  if (!SW.body) return;
  const scroll = SW.drawer?.body?.scrollTop || 0;
  dom.clear(SW.body);
  SW.body.append(
    h('section', { class: 'space-sw-sec space-sw-top' }, h('p', { class: 'space-sw-summary' }, d.summary || ''), blockEl(ctx, d)),
    scalesEl(ctx, d), kpEl(ctx, d), alertsEl(ctx, d), windEl(ctx, d), flaresEl(ctx, d), auroraEl(ctx, d), footEl(ctx, d));
  if (SW.drawer?.body) SW.drawer.body.scrollTop = scroll;
}

function renderError(message) {
  const { h, dom } = SW.ctx;
  if (!SW.body || SW.data) return;
  dom.clear(SW.body);
  SW.body.append(h('section', { class: 'space-sw-sec' }, h('p', { class: 'dim' }, message)));
}

// ------------------------------------------------------------------ data and drawer
async function load(signal) {
  try {
    const d = await SW.ctx.api.get('/api/space/spaceweather', null, { signal, timeoutMs: 40000 });
    SW.data = d;
    SW.error = null;
    render(d);
    const g = d.scales?.now?.G?.level;
    SW.layer.setCount(d.kp?.now ? `Kp ${kpTxt(d.kp.now.kp)}` : (g != null ? `G${g}` : null));
    const bad = Object.entries(d.feeds || {}).filter(([k, v]) => k !== 'ovation' && !v.ok).map(([k]) => k.replace('_', ' '));
    SW.layer.setStatus(bad.length ? 'warn' : 'ok', bad.length ? `Some SWPC feeds are not updating: ${bad.join(', ')}.` : d.summary);
  } catch (err) {
    if (err?.name === 'AbortError') throw err;
    SW.error = err.message;
    renderError(`Space weather is unavailable right now (${err.message}).`);
    throw err;
  }
}

function openDrawer() {
  const { h, ui } = SW.ctx;
  if (SW.drawer) return;
  SW.body = h('div', { class: 'space-sw' }, h('section', { class: 'space-sw-sec' }, h('p', { class: 'dim' }, 'Loading space weather from NOAA SWPC…')));
  SW.drawer = ui.drawer({ id: 'space-weather', title: 'Space weather now', el: SW.body, width: 404,
    onClose: () => { SW.drawer = null; SW.body = null; syncButton(); if (SW.layer.isOn()) SW.ctx.layers.setOn('space.spaceweather', false); } });
  if (SW.data) render(SW.data);
  syncButton();
}

function closeDrawer() {
  if (!SW.drawer) return;
  const d = SW.drawer;
  SW.drawer = null;
  SW.body = null;
  d.close();
  syncButton();
}

function syncButton() {
  SW.btn?.setAttribute('aria-pressed', String(!!SW.drawer));
}

// ------------------------------------------------------------------ event cards
const ABOUT = {
  geomagnetic_storm: 'The solar wind is shaking Earth’s magnetic field. Aurora reaches lower latitudes, power grids can see induced currents, GPS and HF radio degrade and satellites feel extra drag.',
  solar_flare: 'A burst of X-rays from the Sun ionises the sunlit upper atmosphere and blacks out HF radio there for minutes to hours. It is no danger to people on the ground.',
  radiation_storm: 'Energetic protons from the Sun reach Earth, mostly over the poles: HF radio fades on polar routes, polar flights get extra radiation and satellites can glitch.',
  storm_watch: 'SWPC expects a geomagnetic storm, usually because a coronal mass ejection is on its way. Arrival times are uncertain by many hours.',
};
const WHERE = {
  solar_flare: 'The map point is under the Sun at the peak: the centre of the sunlit side, where the blackout is strongest.',
  default: 'Space weather is planet-wide; the map point is the north geomagnetic pole, near where the effects are strongest.',
};

async function openEventCard(entity) {
  const ctx = SW.ctx;
  let e = entity;
  if (e.severity == null || !e.type) {
    try { e = await ctx.api.get(`/api/space/spaceweather/event/${encodeURIComponent(entity.id)}`); } catch (err) {
      return ctx.cards.open({ key: `space_weather:${entity.id}`, kind: 'space_weather', title: entity.title || 'Space weather event', icon: '☀', accent: COLOR.space,
        sections: [{ text: err.status === 404 ? 'This event is no longer in SWPC’s recent lists, which cover about a week.' : `The event could not be loaded right now (${err.message}).` }],
        actions: [{ text: 'Space weather now', onClick: () => ctx.layers.setOn('space.spaceweather', true) }] });
    }
  }
  const lvl = Number(String(e.scale || '').slice(1)) || 0;
  const isFlare = e.type === 'solar_flare';
  return ctx.cards.open({
    key: `space_weather:${e.id}`, kind: 'space_weather', title: e.title, subtitle: e.subtitle, icon: '☀', accent: COLOR.space,
    badges: [{ text: `NOAA ${e.scale}, ${NAME[lvl] || ''}`.trim(), tone: toneOf(lvl) }, { text: 'NOAA SWPC', tone: 'grey' }],
    block: [{ label: 'NOAA scale', value: e.scale }, { label: isFlare ? 'Peak' : 'Latest', value: hm(e.time), unit: 'UTC' }, { label: 'Date', value: dayLabel(e.time) }],
    sections: [{ title: 'What it means', text: ABOUT[e.type] || '' }, { title: 'On the map', text: isFlare ? WHERE.solar_flare : WHERE.default }],
    links: e.url ? [{ text: 'SWPC page', href: e.url }] : [],
    actions: [{ text: 'Space weather now', primary: true, onClick: () => ctx.layers.setOn('space.spaceweather', true) }],
    entity: { kind: 'space_weather', id: e.id, title: e.title, subtitle: e.subtitle, lat: e.lat, lon: e.lon, layer: 'space.spaceweather', time: e.time, zoom: 2 },
    foot: 'From NOAA SWPC alerts, scales, Kp and GOES X-ray data.',
  });
}

// ------------------------------------------------------------------ register
export async function registerSpaceWeather(ctx) {
  SW.ctx = ctx;
  SW.layer = ctx.layers.register({
    id: 'space.spaceweather', group: 'Space', title: 'Space weather now', subtitle: 'Kp, storms, flares, solar wind and SWPC alerts',
    description: 'Opens a panel with Kp now and the 3-day forecast, the NOAA G, S and R levels, the latest solar flares, solar wind speed and Bz, and SWPC alerts in plain words, with links to the official pages.',
    source: 'NOAA Space Weather Prediction Center (public domain)', color: COLOR.space, symbol: 'point', refreshMs: 2 * 60000,
    async enable() { openDrawer(); },
    async refresh({ signal }) { await load(signal); },
    async disable() { closeDrawer(); },
  });
  SW.btn = ctx.ui.toolbarButton({ id: 'space-weather', title: 'Space weather now', label: 'Space weather', icon: SUN_ICON, order: 45,
    onClick: () => ctx.layers.setOn('space.spaceweather', !ctx.layers.isOn('space.spaceweather')) });
  ctx.bus.on('layer:toggled', ({ id }) => { if (id === 'space.spaceweather') syncButton(); });

  ctx.entities.handler('space_weather', (entity) => {
    const id = String(entity.id || '');
    if (id.startsWith('swpc:aurora')) { showOval(ctx); return null; }
    if (id.startsWith('swpc:now')) { openDrawer(); SW.drawer?.el?.querySelector('.space-sw-summary')?.scrollIntoView?.({ block: 'nearest' }); return null; }
    return openEventCard(entity);
  }, { group: 'Space', icon: '☀' });
}
