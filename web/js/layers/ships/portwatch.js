// IMF PortWatch daily chokepoint transits (satellite AIS) beside Meridian's own counts: the strait card
// section, the PortWatch-only chokepoint card (entity kind strait, id portwatch:<chokepoint id>) and the
// Maritime drawer's "Chokepoints" tab. Backend: meridian/modules/ships/portwatch.py.

import { GROUP_COLOR } from './common.js';

const SVG = 'http://www.w3.org/2000/svg';
const ICON = '⇄';
export const PW_LINK = 'https://portwatch.imf.org/';

function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, String(v));
  return el;
}

function dayText(day) {
  const d = new Date(`${day}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return day;
  return `${d.getUTCDate()} ${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getUTCMonth()]}`;
}

function nextDay(day) {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

/** A 2 px line of daily transits with a faint area, gaps where PortWatch has no day, and a dashed line at the
 *  1-year average. Hover or arrow keys move a readout; the numbers are also in the rows and table beside it. */
export function sparkline(ctx, series, { mean = null, label = 'PortWatch daily transits' } = {}) {
  const { h, fmt } = ctx;
  const wrap = h('div', { class: 'ships-spark' });
  if (!series?.length) return wrap;
  // Fill missing days so gaps stay gaps.
  const days = [];
  const byDay = new Map(series.map(r => [r[0], r]));
  for (let d = series[0][0], i = 0; d <= series[series.length - 1][0] && i < 400; d = nextDay(d), i++) days.push(byDay.get(d) || [d, null, null]);
  const W = 300;
  const H = 60;
  const max = Math.max(1, mean || 0, ...days.map(r => r[1] || 0)) * 1.08;
  const x = (i) => (days.length === 1 ? W / 2 : (i / (days.length - 1)) * W);
  const y = (v) => H - (v / max) * (H - 4) - 2;
  let line = '';
  let area = '';
  let open = null;
  days.forEach((r, i) => {
    if (r[1] == null) {
      if (open != null) area += `L${x(i - 1).toFixed(1)},${H}L${x(open).toFixed(1)},${H}Z`;
      open = null;
      return;
    }
    const px = x(i).toFixed(1);
    const py = y(r[1]).toFixed(1);
    if (open == null) { line += `M${px},${py}`; area += `M${px},${H}L${px},${py}`; open = i; } else { line += `L${px},${py}`; area += `L${px},${py}`; }
  });
  if (open != null) area += `L${x(days.length - 1).toFixed(1)},${H}L${x(open).toFixed(1)},${H}Z`;
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', class: 'ships-spark-svg', role: 'img', tabindex: '0',
    'aria-label': `${label}: ${days.length} days from ${dayText(days[0][0])} to ${dayText(days[days.length - 1][0])}${mean != null ? `, 1-year average ${fmt.num(mean, 1)} a day` : ''}. Use the arrow keys to read each day.` });
  svg.append(svgEl('line', { x1: 0, x2: W, y1: H - 0.5, y2: H - 0.5, class: 'base' }));
  svg.append(svgEl('path', { d: area, class: 'area' }));
  svg.append(svgEl('path', { d: line, class: 'line', stroke: GROUP_COLOR }));
  if (mean != null) svg.append(svgEl('line', { x1: 0, x2: W, y1: y(mean), y2: y(mean), class: 'ref' }));
  const cross = svgEl('line', { x1: 0, x2: 0, y1: 0, y2: H, class: 'cross', visibility: 'hidden' });
  svg.append(cross);
  const readout = h('div', { class: 'ships-spark-read', 'aria-live': 'polite' }, ' ');
  const axis = h('div', { class: 'ships-spark-axis' }, h('span', null, dayText(days[0][0])),
    mean != null ? h('span', { class: 'ref-label' }, `dashed: 1-year average, ${fmt.num(mean, mean < 10 ? 1 : 0)} a day`) : null,
    h('span', null, dayText(days[days.length - 1][0])));
  let at = days.length - 1;
  const show = (i) => {
    at = Math.max(0, Math.min(days.length - 1, i));
    const r = days[at];
    cross.setAttribute('x1', x(at));
    cross.setAttribute('x2', x(at));
    cross.setAttribute('visibility', 'visible');
    readout.textContent = r[1] == null ? `${dayText(r[0])}: no PortWatch data` : `${dayText(r[0])}: ${fmt.int(r[1])} transit${r[1] === 1 ? '' : 's'}${r[2] != null ? `, ${fmt.int(r[2])} tanker${r[2] === 1 ? '' : 's'}` : ''}`;
  };
  const hide = () => { cross.setAttribute('visibility', 'hidden'); readout.textContent = ' '; };
  svg.addEventListener('pointermove', (ev) => {
    const box = svg.getBoundingClientRect();
    if (box.width > 0) show(Math.round(((ev.clientX - box.left) / box.width) * (days.length - 1)));
  });
  svg.addEventListener('pointerleave', hide);
  svg.addEventListener('focus', () => show(at));
  svg.addEventListener('blur', hide);
  svg.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') { ev.preventDefault(); show(at + (ev.key === 'ArrowLeft' ? -1 : 1)); }
  });
  wrap.append(readout, svg, axis);
  return wrap;
}

function changeText(ctx, p) {
  if (p.change_1y_pct == null) return null;
  const c = p.change_1y_pct;
  return c === 0 ? 'Same as the 1-year average' : `${Math.abs(c)}% ${c < 0 ? 'below' : 'above'} the 1-year average`;
}

/** The strait card section: figures, sparkline, the cross-check in plain words and a small table twin. */
export function portwatchSection(ctx, p) {
  const { h, fmt } = ctx;
  const box = h('div', { class: 'ships-pw' });
  if (!p || !p.latest) {
    const lines = p?.cross_check?.lines || ['IMF PortWatch has no daily counts for this strait yet. Meridian fetches them one chokepoint every few minutes, then weekly.'];
    for (const line of lines) box.append(h('p', { class: 'ships-pw-line' }, line));
    box.append(h('p', { class: 'ships-pw-credit' }, 'Source: ', h('a', { href: PW_LINK }, 'IMF PortWatch'), ' (International Monetary Fund).'));
    return box;
  }
  const rows = h('dl', { class: 'kv' });
  const add = (k, v) => { if (v != null && v !== '') rows.append(h('dt', null, k), h('dd', null, v)); };
  add('Latest day', `${fmt.int(p.latest.total)} on ${dayText(p.latest.day)} (${fmt.int(p.lag_days)} days ago)`);
  add('Last 7 days', p.mean_7d != null ? `${fmt.num(p.mean_7d, 1)} a day` : null);
  add('Past year', p.mean_1y != null ? `${fmt.num(p.mean_1y, 1)} a day` : null);
  add('Change', changeText(ctx, p));
  if (p.by_type_7d) {
    const t = p.by_type_7d;
    add('Types, last 7 days', `${fmt.int(t.tanker)} tankers, ${fmt.int(t.container)} container, ${fmt.int(t.dry_bulk)} dry bulk, ${fmt.int((t.general_cargo || 0) + (t.roro || 0))} general cargo and ro-ro`);
  }
  box.append(sparkline(ctx, p.series || [], { mean: p.mean_1y }));
  box.append(rows);
  for (const line of p.cross_check?.lines || []) box.append(h('p', { class: 'ships-pw-line' }, line));
  const own = new Map((p.own_days || []).map(d => [d.day, d]));
  const recent = (p.series || []).slice(-10).reverse();
  if (recent.length) {
    const tbl = h('table', { class: 'tbl ships-tbl ships-pw-tbl' },
      h('tr', null, h('th', null, 'Day'), h('th', { class: 'num' }, 'PortWatch'), h('th', { class: 'num' }, 'Meridian, cargo and tankers'), h('th', { class: 'num' }, 'Hours heard')));
    for (const [day, total] of recent) {
      const m = own.get(day);
      tbl.append(h('tr', null, h('td', null, dayText(day)), h('td', { class: 'num' }, total == null ? '–' : fmt.int(total)),
        h('td', { class: 'num' }, m ? fmt.int(m.transits_ct) : '–'), h('td', { class: 'num' }, m ? fmt.int(m.covered_hours) : '–')));
    }
    box.append(h('details', { class: 'ships-pw-days' }, h('summary', null, 'Daily figures'), tbl));
  }
  box.append(h('p', { class: 'ships-pw-credit' }, 'Source: ', h('a', { href: PW_LINK }, 'IMF PortWatch'), ' (International Monetary Fund), transits derived from satellite AIS, published weekly.'));
  return box;
}

export function portwatchBadges(p) {
  const out = [];
  if (!p || !p.latest) return out;
  if ((p.flags || []).includes('far_below')) out.push({ text: `PortWatch ${Math.abs(p.change_1y_pct)}% below its year`, tone: 'amber', title: 'Its latest week is far below its own 1-year average: a real fall or a gap in satellite AIS' });
  if ((p.flags || []).includes('missing_days')) out.push({ text: 'PortWatch days missing', tone: 'amber' });
  return out;
}

// ------------------------------------------------------------------ PortWatch-only chokepoint card
function chokepointSpec(ctx, p) {
  const { fmt } = ctx;
  return {
    key: `strait:portwatch:${p.id}`, kind: 'strait', title: p.name, subtitle: 'Chokepoint, IMF PortWatch daily transits', icon: ICON, accent: GROUP_COLOR,
    badges: [{ text: 'Satellite AIS, a week behind', tone: 'grey' }, ...portwatchBadges(p)],
    block: [
      { label: 'Latest day', value: p.latest ? fmt.int(p.latest.total) : '–' },
      { label: '7-day average', value: p.mean_7d != null ? fmt.num(p.mean_7d, 1) : '–' },
      { label: '1-year average', value: p.mean_1y != null ? fmt.num(p.mean_1y, 1) : '–' },
    ],
    sections: [{ title: 'PortWatch daily transits (satellite AIS)', el: portwatchSection(ctx, p) }],
    entity: { kind: 'strait', id: `portwatch:${p.id}`, title: p.name, subtitle: 'IMF PortWatch chokepoint', lat: p.lat, lon: p.lon, layer: 'ships.straits', zoom: 7 },
    foot: 'Meridian does not monitor this chokepoint with its own AIS; the figures are PortWatch\'s alone.',
  };
}

export async function openChokepoint(ctx, entity) {
  const pid = String(entity.id || '').replace(/^portwatch:/, '');
  const card = ctx.cards.open({ key: `strait:portwatch:${pid}`, kind: 'strait', title: entity.title || 'Chokepoint', subtitle: 'Loading PortWatch figures…', icon: ICON, accent: GROUP_COLOR });
  try {
    const p = await ctx.api.get(`/api/ships/chokepoints/${encodeURIComponent(pid)}`);
    return ctx.cards.open(chokepointSpec(ctx, p));
  } catch (err) {
    card.update({ subtitle: err?.status === 404 ? 'Not on IMF PortWatch\'s list' : 'IMF PortWatch figures are unavailable right now.' });
    return card;
  }
}

// ------------------------------------------------------------------ Maritime drawer tab
export async function chokepointsTab(ctx, body, { openEntity, table, linkCell }) {
  const { h, fmt } = ctx;
  const r = await ctx.api.get('/api/ships/chokepoints');
  ctx.dom.clear(body);
  if (!r.enabled) { body.append(h('p', { class: 'ships-empty' }, r.note)); return; }
  body.append(h('p', { class: 'ships-note' }, r.note));
  if (r.error) body.append(h('p', { class: 'ships-note ships-err' }, `Last attempt failed: ${r.error}`));
  const flagged = r.chokepoints.filter(c => c.cross_check?.pw_gap && c.latest && (c.flags || []).includes('far_below'));
  for (const c of flagged) body.append(h('p', { class: 'ships-note ships-warn' }, `${c.name}: ${c.cross_check.lines.slice(0, 3).join(' ')}`));
  if (!r.chokepoints.length) { body.append(h('p', { class: 'ships-empty' }, 'The chokepoint list has not been fetched yet.')); return; }
  const rows = r.chokepoints.map(c => h('tr', { class: c.latest ? '' : 'ships-dim' },
    linkCell(c.name, c.latest ? `${dayText(c.latest.day)}${c.meridian ? ', Meridian monitors it' : ''}` : (r.error ? 'No daily counts' : 'No daily counts yet'), () => {
      if (c.strait) openEntity({ kind: 'strait', id: `ships:${c.strait}`, title: c.meridian?.name || c.name, lat: c.lat, lon: c.lon, layer: 'ships.straits' });
      else openEntity({ kind: 'strait', id: `portwatch:${c.id}`, title: c.name, lat: c.lat, lon: c.lon, layer: 'ships.straits', zoom: 7 });
    }),
    h('td', { class: 'num' }, c.latest ? fmt.int(c.latest.total) : '–'),
    h('td', { class: 'num' }, c.mean_7d != null ? fmt.num(c.mean_7d, c.mean_7d < 10 ? 1 : 0) : '–'),
    h('td', { class: 'num' }, c.change_1y_pct != null ? `${c.change_1y_pct > 0 ? '+' : ''}${c.change_1y_pct}%` : '–'),
    h('td', { class: 'num' }, c.meridian ? (!c.meridian.coverage ? 'no cover' : c.meridian.heard_inside === false ? 'none inside' : fmt.int(c.meridian.transits_24h)) : '–')));
  body.append(table([['Chokepoint'], ['Latest', true], ['7-day', true], ['vs year', true], ['Meridian 24 h', true]], rows));
  body.append(h('p', { class: 'ships-note' }, 'Source: ', h('a', { href: r.link }, 'IMF PortWatch'), '. "vs year" compares the latest 7 days with the 1-year average. Meridian 24 h is its own terrestrial AIS transit count, all vessel types; "none inside" means it heard ships near the strait but none in it.'));
}
