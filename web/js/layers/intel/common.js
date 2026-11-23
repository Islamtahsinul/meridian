// Shared helpers for the intel module's browser layers: colour ramps (validated single-hue
// sequential ramps for the dark map, see docs/modules/intel.md), severity wording, small
// formatting helpers and a tiny bar chart for cards.

export const CONFLICT = '#e5534b';
export const INTEL = '#43c4b2';
export const ALERT_AMBER = '#ffb547';
export const INK = '#111a2c';

// One hue each, lightness rising with the value (dark map: more = brighter).
export const SEV_RAMP = ['#8a3330', '#b2403b', '#e5534b', '#f2796f', '#ffab9f'];     // conflict severity
export const NEWS_RAMP = ['#2a7a70', '#33998c', '#43c4b2', '#76dccd', '#aef0e6'];    // news severity
export const INDEX_RAMP = ['#70401f', '#9a4a1f', '#c4561f', '#e8692f', '#ff9d6a'];   // instability index
export const RISK_RAMP = ['#6e5a12', '#94781a', '#bf9b1f', '#e3bd2e', '#ffe07a'];    // risk zone weight
// Categorical (3 slots, validated all-pairs on the dark surface): UCDP type of violence.
export const UCDP_COLOURS = { state: '#3987e5', nonstate: '#d95926', onesided: '#199e70' };
// Ordinal ramps (one hue, monotone lightness, validated with --ordinal on #111a2c): travel advice levels 1-4
// (violet) and protest kinds (magenta: demonstration, strike or blockade, riot).
export const ADVICE_RAMP = ['#5a4cb8', '#7566d6', '#9085e9', '#b9b0f5'];
export const PROTEST_RAMP = ['#a8457a', '#d55181', '#f59ac0'];
export const PROTEST_COLOUR = PROTEST_RAMP[1];
// BGP detections by confidence (8, 10, 12): steps of the validated teal ramp.
export const BGP_RAMP = [NEWS_RAMP[1], NEWS_RAMP[2], NEWS_RAMP[4]];
export const ADVICE_LEVELS = { 1: 'Normal precautions', 2: 'Some areas advised against', 3: 'Reconsider travel', 4: 'Do not travel' };

/** 'Level 3, reconsider travel' style chip text plus its ramp colour. */
export function adviceChip(level) {
  const l = Math.max(1, Math.min(4, +level || 1));
  return { text: `Level ${l}, ${ADVICE_LEVELS[l].toLowerCase()}`, colour: ADVICE_RAMP[l - 1] };
}

export const INDEX_BANDS = [
  { from: 0, to: 20, label: 'Low' }, { from: 20, to: 40, label: 'Moderate' }, { from: 40, to: 60, label: 'Elevated' },
  { from: 60, to: 80, label: 'High' }, { from: 80, to: 101, label: 'Very high' },
];

/** MapLibre colour expression for a 0-10 severity property. */
export function sevColour(prop = 's', ramp = SEV_RAMP) {
  return ['interpolate', ['linear'], ['to-number', ['get', prop], 0], 0, ramp[0], 3, ramp[1], 5, ramp[2], 7, ramp[3], 9, ramp[4]];
}

export function sevWord(s) {
  if (s == null) return 'Unrated';
  if (s >= 9) return 'Extreme';
  if (s >= 6) return 'Serious';
  if (s >= 3) return 'Notable';
  return 'Routine';
}

export function sevTone(s) {
  if (s >= 9) return 'red';
  if (s >= 6) return 'amber';
  if (s >= 3) return 'blue';
  return 'grey';
}

export function sevBadge(s) {
  return { text: `Severity ${Number(s).toFixed(1)}, ${sevWord(s).toLowerCase()}`, tone: sevTone(s) };
}

export function sevLegend(ramp = SEV_RAMP) {
  return [{ color: ramp[0], label: 'Routine' }, { color: ramp[2], label: 'Notable' }, { color: ramp[3], label: 'Serious' },
    { color: ramp[4], label: 'Extreme' }];
}

export function indexColour(i) {
  if (i == null || Number.isNaN(+i)) return null;
  const k = INDEX_BANDS.findIndex(b => i < b.to);
  return INDEX_RAMP[k < 0 ? INDEX_RAMP.length - 1 : k];
}

export function bandOf(i) {
  if (i == null) return 'No data';
  return (INDEX_BANDS.find(b => i < b.to) || INDEX_BANDS[INDEX_BANDS.length - 1]).label;
}

export function bboxParam(bounds) {
  if (!bounds) return null;
  return bounds.map(v => (+v).toFixed(3)).join(',');
}

export function money(fmt, v) {
  if (v == null) return null;
  const a = Math.abs(v);
  if (a >= 1e12) return `$${fmt.num(v / 1e12, 2)} trillion`;
  if (a >= 1e9) return `$${fmt.num(v / 1e9, 1)} billion`;
  if (a >= 1e6) return `$${fmt.num(v / 1e6, 1)} million`;
  return `$${fmt.int(v)}`;
}

export function people(fmt, v) {
  if (v == null) return null;
  if (v >= 1e9) return `${fmt.num(v / 1e9, 2)} billion`;
  if (v >= 1e6) return `${fmt.num(v / 1e6, 1)} million`;
  return fmt.int(v);
}

export function short(text, n = 90) {
  const s = String(text ?? '');
  return s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s;
}

export function dateOnly(fmt, t) {
  const s = fmt.utc(t);
  return s === '–' ? s : s.slice(0, 10);
}

/** Small bar chart (inline SVG) for card sections: values per label, single series, hover titles. */
export function barChart(ctx, items, { colour = CONFLICT, height = 64, unit = '', label = '', markMax = false } = {}) {
  const ns = 'http://www.w3.org/2000/svg';
  const W = 330;
  const max = Math.max(1, ...items.map(i => i.value || 0));
  const n = Math.max(1, items.length);
  const gap = n > 40 ? 1 : 2;
  const bw = Math.max(1, (W - gap * (n - 1)) / n);
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${height + 16}`);
  svg.setAttribute('class', 'intel-bars');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', label || 'Bar chart');
  const base = document.createElementNS(ns, 'line');
  base.setAttribute('x1', '0'); base.setAttribute('x2', String(W));
  base.setAttribute('y1', String(height + 0.5)); base.setAttribute('y2', String(height + 0.5));
  base.setAttribute('class', 'intel-bars-base');
  svg.append(base);
  items.forEach((it, i) => {
    const v = it.value || 0;
    const h = v ? Math.max(1.5, (v / max) * (height - (markMax ? 14 : 2))) : 0;
    const x = i * (bw + gap);
    const hit = document.createElementNS(ns, 'rect');
    hit.setAttribute('x', String(x)); hit.setAttribute('y', '0');
    hit.setAttribute('width', String(bw + gap)); hit.setAttribute('height', String(height));
    hit.setAttribute('class', 'intel-bars-hit');
    const title = document.createElementNS(ns, 'title');
    title.textContent = `${it.label}: ${ctx.fmt.int(v)}${unit ? ` ${unit}` : ''}`;
    hit.append(title);
    if (h) {
      const r = document.createElementNS(ns, 'rect');
      r.setAttribute('x', String(x)); r.setAttribute('y', String(height - h));
      r.setAttribute('width', String(bw)); r.setAttribute('height', String(h));
      r.setAttribute('rx', String(Math.min(2, bw / 2)));
      r.setAttribute('fill', colour);
      r.setAttribute('class', 'intel-bars-bar');
      svg.append(r);
    }
    svg.append(hit);
  });
  if (markMax && max > 0) {
    // one selective direct label: the peak value, kept inside the chart
    const i = items.findIndex(it => (it.value || 0) === max);
    const x = Math.min(W - 2, Math.max(2, i * (bw + gap) + bw / 2));
    const t = document.createElementNS(ns, 'text');
    t.setAttribute('x', String(x)); t.setAttribute('y', '9');
    t.setAttribute('text-anchor', x > W * 0.75 ? 'end' : x < W * 0.25 ? 'start' : 'middle');
    t.setAttribute('class', 'intel-bars-peak');
    t.textContent = `${ctx.fmt.compact(max)} in ${items[i].label}`;
    svg.append(t);
  }
  const first = items[0]?.label ?? '';
  const last = items[items.length - 1]?.label ?? '';
  for (const [x, text, anchor] of [[0, first, 'start'], [W, last, 'end']]) {
    const t = document.createElementNS(ns, 'text');
    t.setAttribute('x', String(x)); t.setAttribute('y', String(height + 13));
    t.setAttribute('text-anchor', anchor); t.setAttribute('class', 'intel-bars-axis');
    t.textContent = String(text);
    svg.append(t);
  }
  return svg;
}

/** A horizontal meter 0-100 with a label and value, for index parts. */
export function meter(ctx, { label, score, weight, detail }) {
  const h = ctx.h;
  const pct = score == null ? 0 : Math.max(0, Math.min(100, score));
  const bar = h('span', { class: 'intel-meter-bar' }, h('span', { class: 'intel-meter-fill', style: { width: `${pct}%`, background: indexColour(score) || 'var(--ink-4)' } }));
  return h('div', { class: 'intel-meter' },
    h('div', { class: 'intel-meter-head' }, h('span', null, label), h('span', { class: 'tnum' }, score == null ? 'No data' : `${Math.round(score)}`),
      h('span', { class: 'intel-meter-w' }, `weight ${Math.round(weight * 100)}%`)),
    bar, detail ? h('div', { class: 'intel-meter-detail' }, detail) : null);
}

export async function safeGet(ctx, path, params, opts) {
  try {
    return await ctx.api.get(path, params, opts);
  } catch (err) {
    if (err?.name === 'AbortError') throw err;
    return { __error: err };
  }
}

export function errorText(err, fallback = 'This is unavailable right now.') {
  if (!err) return fallback;
  if (err.code === 'BUILDING') return 'Still being prepared on the server. Try again in a minute.';
  if (err.status === 404) return err.message || 'Not found.';
  return err.message || fallback;
}
