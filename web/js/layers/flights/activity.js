// Air activity (layer flights.activity, toolbar "Air"): military surges by region, new or worsening
// GPS interference and airport movement drops, measured against Meridian's own baselines. Cards for
// entity kind "air_activity" (ids flights:surge:<region>[:t], flights:gps:<lat>:<lon>:<t>,
// flights:airport:<ident>[:t]). Every reading is a candidate with its evidence, coverage and the
// common innocent explanations, never a finding.

import { AMBER, GREEN, GROUP, INK, RED } from './style.js';

const SRC = 'ov-flights-activity';
const L_FILL = 'ov-flights-activity-fill';
const L_LINE = 'ov-flights-activity-line';
const L_LABEL = 'ov-flights-activity-label';
const L_APT = 'ov-flights-activity-apt';
const L_APT_LABEL = 'ov-flights-activity-apt-label';
const DRAWER = 'flights-activity';
const ICON = 'M10 2.6v7.2M5 7.4l5-1.8 5 1.8M8.2 9.8 10 9.2l1.8.6M2.6 16.4h3.2l1.8-3 2.4 3.6 2-2.8h5.4';
const SVG_NS = 'http://www.w3.org/2000/svg';
const BAR = GROUP;                    // one series colour; the compared window is a band, not a colour
const USUAL = '#aab4ca';              // neutral reference line (paper-dim)
const NO_DATA = '#3a5080';
const TABS = [{ id: 'military', label: 'Military' }, { id: 'gps', label: 'GPS' }, { id: 'airports', label: 'Airports' }];
const STATE = {
  unusual: { text: 'Unusual activity', tone: 'red' },
  notable: { text: 'Busier than usual', tone: 'amber' },
  normal: { text: 'Usual levels', tone: 'green' },
  learning: { text: 'Learning baseline', tone: 'grey' },
  no_data: { text: 'No recent snapshots', tone: 'grey' },
  feed_changed: { text: 'Feed changed', tone: 'grey' },
  coverage_changed: { text: 'Coverage changed', tone: 'grey' },
  drop: { text: 'Far fewer movements', tone: 'red' },
  drop_unverified: { text: 'Fewer aircraft, unverified', tone: 'amber' },
  no_coverage: { text: 'No coverage now', tone: 'grey' },
  not_monitored: { text: 'Not monitored', tone: 'grey' },
};
const APT_STATE = { normal: { text: 'Usual movements', tone: 'green' }, notable: { text: 'Fewer movements', tone: 'amber' } };
const FOOT = 'Derived from adsb.fi open data. A candidate for a closer look, not a finding.';

let ctx = null;
let layer = null;
let summary = null;
let drawer = null;
let drawerTimer = null;
let tab = 'military';
let btn = null;

// ------------------------------------------------------------------ small helpers
const pad2 = n => String(n).padStart(2, '0');
const hourText = t => { const d = new Date(t * 1000); return `${pad2(d.getUTCHours())}:00`; };
const dayText = t => new Date(t * 1000).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', timeZone: 'UTC' });

function num(v, d = 1) {
  if (v == null || !Number.isFinite(+v)) return null;
  const x = +v;
  return Math.abs(x) >= 10 ? ctx.fmt.int(Math.round(x)) : ctx.fmt.num(x, Number.isInteger(x) ? 0 : d);
}

function pct(v, d = 0) {
  return v == null ? null : `${ctx.fmt.num(v * 100, v * 100 < 10 && d === 0 ? 1 : d)}%`;
}

function stateBadge(state, apt = false) {
  const b = (apt && APT_STATE[state]) || STATE[state];
  return b ? { text: b.text, tone: b.tone } : null;
}

function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, String(v));
  return el;
}

// A column with a rounded data end and a square foot on the baseline.
function columnPath(x, y, w, hgt, r = 1.5) {
  const rr = Math.min(r, w / 2, hgt);
  return `M${x},${y + hgt}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + hgt}Z`;
}

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
  return 10 * p;
}

/**
 * Hourly columns with a "usual" reference line and the compared window as a band.
 * rows: [{t, value|null (null = no snapshots), usual|null, tip}], window: how many final hours were compared.
 */
function hourChart(rows, { window = 3, unit = 'aircraft', barLabel, usualLabel = 'Usual for the hour' } = {}) {
  const { h } = ctx;
  const W = 360;            // about the card's width, so text in the chart is drawn at its real size
  const PH = 70;            // plot height
  const TOP = 13;
  const AX = 15;            // x-axis band
  const n = rows.length;
  const step = W / Math.max(1, n);
  const bw = Math.max(2, Math.min(24, step - 2));          // 2px surface gap between neighbours
  const max = niceMax(Math.max(1, ...rows.map(r => Math.max(r.value || 0, r.usual || 0))));
  const y = v => TOP + PH - (v / max) * PH;
  const root = svg('svg', { viewBox: `0 0 ${W} ${TOP + PH + AX}`, class: 'flights-act-chart', role: 'img',
    'aria-label': `${barLabel} by hour, last ${n} hours, with the usual level` });
  // recessive grid: the top tick and the baseline
  root.append(svg('line', { x1: 0, x2: W, y1: TOP, y2: TOP, class: 'grid' }));
  root.append(svg('line', { x1: 0, x2: W, y1: TOP + PH, y2: TOP + PH, class: 'axis' }));
  const tick = svg('text', { x: 2, y: TOP - 3, class: 'tick' });
  tick.textContent = `${ctx.fmt.int(max)} ${unit}`;
  root.append(tick);
  if (window > 0 && n >= window) {
    const x0 = (n - window) * step;
    root.append(svg('rect', { x: x0, y: TOP, width: W - x0, height: PH, class: 'band' }));
    const bl = svg('text', { x: W - 2, y: TOP - 3, class: 'tick end' });
    bl.textContent = `compared: last ${window} h`;
    root.append(bl);
  }
  rows.forEach((r, i) => {
    const x = i * step + (step - bw) / 2;
    const g = svg('g', { class: 'col', tabindex: '-1' });
    const title = svg('title');
    title.textContent = r.tip;
    g.append(title);
    // the hit area is the whole slot, not just the painted column
    g.append(svg('rect', { x: i * step, y: TOP, width: step, height: PH, class: 'hit' }));
    if (r.value == null) {
      g.append(svg('line', { x1: x, x2: x + bw, y1: TOP + PH - 0.5, y2: TOP + PH - 0.5, class: 'nodata', stroke: NO_DATA }));
    } else if (r.value > 0) {
      const top = y(r.value);
      g.append(svg('path', { d: columnPath(x, top, bw, TOP + PH - top), fill: BAR, class: 'bar' }));
    }
    root.append(g);
  });
  // usual: a 2px line, broken where there is no baseline
  let d = '';
  let pen = false;
  rows.forEach((r, i) => {
    if (r.usual == null) { pen = false; return; }
    const cx = i * step + step / 2;
    d += `${pen ? 'L' : 'M'}${cx.toFixed(1)},${y(r.usual).toFixed(1)}`;
    pen = true;
  });
  if (d) root.append(svg('path', { d, class: 'usual', stroke: USUAL }));
  const left = svg('text', { x: 0, y: TOP + PH + 12, class: 'tick' });
  left.textContent = `${n} h ago`;
  const right = svg('text', { x: W, y: TOP + PH + 12, class: 'tick end' });
  right.textContent = hourText(rows[n - 1].t + 3600);
  root.append(left, right);
  const key = h('div', { class: 'flights-act-key' },
    h('span', { class: 'k-bar' }, barLabel), h('span', { class: 'k-usual' }, usualLabel),
    h('span', { class: 'k-band' }, 'Compared window'), h('span', { class: 'k-gap' }, 'Too few snapshots'));
  return h('div', { class: 'flights-act-chart-wrap' }, root, key);
}

/** Seven daily columns of the affected share, coloured by the GPS layer's levels, with the 10% line.
 *  Also used by the GPS cell card (gpsjam.js), which passes its own context. */
export function dayChart(days, c = ctx) {
  const { h, fmt } = c;
  const share = v => `${fmt.num(v * 100, v * 100 < 10 ? 1 : 0)}%`;
  const W = 360;
  const PH = 58;
  const TOP = 13;
  const AX = 15;
  const n = days.length;
  const step = W / Math.max(1, n);
  const bw = Math.min(24, step - 2);
  const shares = days.map(([, total, bad]) => (total >= 12 ? bad / total : null));
  const max = Math.max(0.2, niceMax(Math.max(0, ...shares.filter(v => v != null))));
  const y = v => TOP + PH - (v / max) * PH;
  const root = svg('svg', { viewBox: `0 0 ${W} ${TOP + PH + AX}`, class: 'flights-act-chart', role: 'img',
    'aria-label': 'Share of aircraft with degraded GPS accuracy by day, last 7 days' });
  root.append(svg('line', { x1: 0, x2: W, y1: TOP + PH, y2: TOP + PH, class: 'axis' }));
  root.append(svg('line', { x1: 0, x2: W, y1: y(0.1), y2: y(0.1), class: 'grid' }));
  const t10 = svg('text', { x: 2, y: y(0.1) - 3, class: 'tick' });
  t10.textContent = 'red from 10%';
  root.append(t10);
  days.forEach(([t, total, bad], i) => {
    const s = shares[i];
    const x = i * step + (step - bw) / 2;
    const g = svg('g', { class: 'col' });
    const title = svg('title');
    title.textContent = s == null ? `${dayText(t)}: too few aircraft (${total} aircraft-hours)`
      : `${dayText(t)}: ${share(s)} of ${fmt.int(total)} aircraft-hours affected`;
    g.append(title, svg('rect', { x: i * step, y: TOP, width: step, height: PH, class: 'hit' }));
    if (s == null) {
      g.append(svg('line', { x1: x, x2: x + bw, y1: TOP + PH - 0.5, y2: TOP + PH - 0.5, class: 'nodata', stroke: NO_DATA }));
    } else {
      const top = y(Math.max(s, 0.004));
      g.append(svg('path', { d: columnPath(x, top, bw, TOP + PH - top, 3), fill: s >= 0.1 ? RED : s >= 0.02 ? AMBER : GREEN }));
    }
    const lab = svg('text', { x: i * step + step / 2, y: TOP + PH + 12, class: 'tick mid' });
    lab.textContent = dayText(t);
    root.append(g, lab);
  });
  return h('div', { class: 'flights-act-chart-wrap' }, root);
}

// The table view: the same numbers without the chart (keyboard and screen reader friendly).
function tableView(head, rows) {
  const { h } = ctx;
  const tbl = h('table', { class: 'tbl flights-act-tbl' }, h('tr', null, head.map(([label, numCol]) => h('th', { class: numCol ? 'num' : null }, label))));
  for (const r of rows) tbl.append(h('tr', null, r.map((v, i) => h('td', { class: head[i][1] ? 'num' : null }, v == null ? '–' : String(v)))));
  return h('details', { class: 'flights-act-table' }, h('summary', null, 'Show the numbers'), tbl);
}

// ------------------------------------------------------------------ region card
function regionSpec(d) {
  const { h, fmt } = ctx;
  // hours with under 80% of their snapshots are gaps, as in the baseline (and the airport chart)
  const rows = (d.series || []).slice(0, -1).map(([t, n, cov, usual]) => {
    const value = n != null && cov >= 0.8 ? n : null;
    const tip = n == null ? `${hourText(t)} UTC: no snapshots`
      : value == null ? `${hourText(t)} UTC: ${n} military aircraft in ${Math.round(cov * 6)} of 6 snapshots, too few to compare`
        : `${hourText(t)} UTC: ${n} military aircraft${usual != null ? `, usual ${num(usual)}` : ''}`;
    return { t, value, usual, tip };
  });
  const badges = [stateBadge(d.state)];
  if (d.confidence && (d.state === 'unusual' || d.state === 'notable')) badges.push({ text: `Confidence ${d.confidence}`, tone: 'blue' });
  if (d.state === 'learning') badges.push({ text: `${d.baseline_days} of ${d.needed} days`, tone: 'grey' });
  const roleRows = Object.values(d.roles || {}).filter(r => r.counted && (r.now || r.usual)).map(r => [r.label,
    `${r.now} aircraft-hours${r.usual != null ? `, usually ${num(r.usual)}` : ''}`]);
  const left = Object.values(d.roles || {}).filter(r => !r.counted && r.now).map(r => `${r.label.toLowerCase()} ${r.now}`);
  const checks = d.checks || {};
  const feed = checks.feed ? `${pct(checks.feed.share)} of its usual size` : 'Still learning its usual size';
  const recv = !checks.receivers_available ? 'No check for this region (no GPS-sampler circle here)'
    : checks.receivers ? `All traffic in the region's sampler circles is ${pct(checks.receivers.share)} of usual` : 'Still learning';
  const sections = [
    { title: 'Evidence', text: [d.evidence, d.drivers].filter(Boolean).join(' ') },
    rows.length ? { title: 'Military aircraft by hour, last 48 hours', el: hourChart(rows, { window: d.window?.hours || 3, barLabel: 'Military aircraft in the hour', usualLabel: 'Usual an hour, by 3-hour window' }) } : null,
    rows.length ? { el: tableView([['Hour (UTC)'], ['Aircraft', true], ['Usual', true]], rows.slice(-24).reverse().map(r => [hourText(r.t), r.value, num(r.usual)])) } : null,
    roleRows.length ? { title: 'By role, last 3 hours', rows: roleRows } : null,
    left.length ? { text: `Left out of the count: ${left.join(', ')} (they fly to base timetables).` } : null,
    { title: 'Coverage and checks', rows: [
      ['Snapshots', `${pct(d.coverage)} of the last 3 hours`],
      ['Baseline', d.state === 'learning' ? `${d.baseline_days} of ${d.needed} days` : `${d.baseline_days} days${d.profile_text ? `, ${d.profile_text}` : ''}`],
      ['Worldwide feed', feed], ['Receivers here', recv],
      ['Confidence', d.confidence ? `${d.confidence}: ${(d.confidence_reasons || []).join('; ')}` : null],
      ['This hour so far', d.this_hour ? `${d.this_hour.aircraft} aircraft in ${d.this_hour.snapshots} snapshots` : null]] },
    d.aircraft?.length ? { title: `Military aircraft here now (${d.aircraft.length})`, list: d.aircraft.slice(0, 15).map(a => ({ title: a.title, subtitle: a.subtitle, entity: a })) }
      : { title: 'Military aircraft here now', text: d.aircraft_note || 'None in the latest worldwide list.' },
    { title: 'Why this may be innocent', el: h('ul', { class: 'flights-act-why' }, (d.innocent || []).map(x => h('li', null, x))) },
    d.episodes?.length ? { title: 'Earlier episodes, 30 days', list: d.episodes.map(e => ({ title: e.title, subtitle: `${fmt.utc(e.first_time)} to ${fmt.utc(e.time)}`, entity: e })) } : null,
    { title: 'How this is measured', text: `${d.method} ${d.limits}` },
  ];
  return {
    key: `air_activity:region:${d.id}`, kind: 'air_activity', title: `Military activity: ${d.name}`, subtitle: d.status,
    icon: '✈', accent: GROUP, badges: badges.filter(Boolean),
    block: [{ label: 'Now, an hour', value: num(d.per_hour) }, { label: 'Usual, an hour', value: num(d.usual_per_hour) },
      { label: 'Multiple', value: d.ratio != null ? `${num(d.ratio)}×` : null }, { label: 'Aircraft, 3 h', value: d.distinct }],
    sections,
    actions: [{ text: 'Show the region', onClick: () => ctx.map2.fitBbox(d.bbox) },
      { text: 'Military aircraft layer', onClick: () => ctx.layers.setOn('flights.military', true) }],
    entity: { kind: 'air_activity', id: `flights:surge:${d.id}`, title: `Military activity: ${d.name}`, subtitle: d.status,
      lat: d.label[1], lon: d.label[0], layer: 'flights.activity', bbox: d.bbox, zoom: 5 },
    foot: FOOT,
  };
}

export async function openRegion(rid, seed = null) {
  const path = `/api/flights/activity/region/${encodeURIComponent(rid)}`;
  const card = ctx.cards.open({ key: `air_activity:region:${rid}`, kind: 'air_activity', title: seed?.title || 'Military activity',
    subtitle: 'Loading…', icon: '✈', accent: GROUP, sections: [{ text: 'Loading the counts and the baseline…' }],
    live: { everyMs: 300000, update: async () => { try { return regionSpec(await ctx.api.get(path)); } catch { return null; } } } });
  try {
    const d = await ctx.api.get(path);
    if (card.isOpen()) card.update(regionSpec(d));
  } catch (err) {
    if (card.isOpen()) card.update({ subtitle: 'Unavailable', sections: [{ text: err?.status === 404 ? 'This region is not watched any more.' : 'The air activity data could not be loaded right now.' }] });
  }
  return card;
}

// ------------------------------------------------------------------ airport card
function airportSpec(d) {
  const { h, fmt } = ctx;
  const key = `air_activity:airport:${d.ident}`;
  if (!d.monitored) {
    return { key, kind: 'air_activity', title: `Movements: ${d.ident}`, subtitle: d.status, icon: '◷', accent: GROUP,
      badges: [stateBadge('not_monitored')], sections: [{ text: d.note }, { title: 'How this is measured', text: d.method }], foot: FOOT };
  }
  const rows = (d.series || []).slice(0, -1).map(([t, arr, dep, gnd, high, cov, usual]) => {
    const value = cov >= 0.8 && arr != null ? arr + dep : null;
    return { t, value, usual, arr, dep, gnd, high, cov,
      tip: value == null ? `${hourText(t)} UTC: not enough snapshots` : `${hourText(t)} UTC: ${value} movements (${arr} arriving, ${dep} departing)${usual != null ? `, usual ${num(usual)}` : ''}` };
  });
  const badges = [stateBadge(d.state, true)];
  if (d.confidence) badges.push({ text: `Confidence ${d.confidence}`, tone: 'blue' });
  if (d.watched) badges.push({ text: 'Favourite, sampled in the background', tone: 'grey' });
  const faa = d.faa;
  const faaRows = faa ? (faa.statuses?.length ? faa.statuses.map(s => [s.type, [s.reason, s.avg_delay_min ? `${s.avg_delay_min} min average` : null].filter(Boolean).join(', ') || 'In effect'])
    : [['FAA NAS Status', 'No delay, closure or notice reported right now']]) : null;
  const th = d.this_hour || {};
  const sections = [
    { title: 'Evidence', text: d.evidence },
    rows.length ? { title: 'Arrivals and departures by hour, last 48 hours', el: hourChart(rows, { window: 2, unit: 'movements', barLabel: 'Movements seen in the hour' }) } : null,
    rows.length ? { el: tableView([['Hour (UTC)'], ['Arriving', true], ['Departing', true], ['Overflights', true], ['Usual', true]],
      rows.slice(-24).reverse().map(r => [hourText(r.t), r.value == null ? null : r.arr, r.value == null ? null : r.dep, r.value == null ? null : r.high, num(r.usual)])) } : null,
    { title: 'This hour so far', rows: [['Arriving', th.arrivals], ['Departing', th.departures], ['On the ground', th.on_ground], ['Overflights', th.overflights],
      ['Snapshots', th.snapshots != null ? `${th.snapshots} of 6` : null]] },
    faaRows ? { title: 'FAA status', rows: faaRows } : null,
    { title: 'Coverage', rows: [['Sampling', d.coverage_note], ['Overflight check', { ok: 'Normal: receivers are working', low: 'Far below usual too', unavailable: 'Not enough overflights to check' }[d.receivers]],
      ['Baseline', d.state === 'learning' ? `${d.days} of ${d.needed} days` : `${d.days} days`]] },
    { title: 'Why this may be innocent', el: h('ul', { class: 'flights-act-why' }, (d.innocent || []).map(x => h('li', null, x))) },
    d.episodes?.length ? { title: 'Earlier drops, 30 days', list: d.episodes.map(e => ({ title: e.title, subtitle: `${fmt.utc(e.first_time)} to ${fmt.utc(e.time)}`, entity: e })) } : null,
    { title: 'How this is measured', text: `${d.method} ${d.limits}` },
  ];
  const actions = [{ text: 'Live traffic here', onClick: () => { ctx.layers.setOn('flights', true); ctx.map2.flyTo(d.lat, d.lon, 9.5); } }];
  if (ctx.moduleOk('infra')) actions.push({ text: 'Airport details', onClick: () => ctx.entities.open({ kind: 'airport', id: d.ident, title: d.name, lat: d.lat, lon: d.lon }, { fly: false }) });
  return {
    key, kind: 'air_activity', title: `Movements: ${d.name} (${d.ident})`, subtitle: d.status, icon: '◷', accent: GROUP,
    badges: badges.filter(Boolean),
    block: [{ label: 'Last hour', value: d.value != null && d.state !== 'learning' && d.state !== 'no_coverage' ? fmt.int(d.value) : null },
      { label: 'Usual', value: num(d.usual) }, { label: 'Of usual', value: d.share != null ? pct(d.share) : null },
      { label: 'Snapshots', value: d.coverage != null ? pct(d.coverage) : null }],
    sections, actions, links: faa ? [{ text: 'FAA NAS Status', href: faa.link }] : [],
    entity: { kind: 'air_activity', id: `flights:airport:${d.ident}`, title: `Movements: ${d.name} (${d.ident})`, subtitle: d.status,
      lat: d.lat, lon: d.lon, layer: 'flights.activity', zoom: 9 },
    foot: FOOT,
  };
}

export async function openAirport(ident, seed = null) {
  ident = String(ident || '').toUpperCase();
  if (!/^[A-Z0-9-]{2,12}$/.test(ident)) return null;
  const path = `/api/flights/airport/${encodeURIComponent(ident)}/movements`;
  const card = ctx.cards.open({ key: `air_activity:airport:${ident}`, kind: 'air_activity', title: seed?.title || `Movements: ${ident}`,
    subtitle: 'Loading…', icon: '◷', accent: GROUP, sections: [{ text: 'Loading the movement counts…' }],
    live: { everyMs: 300000, update: async () => { try { return airportSpec(await ctx.api.get(path)); } catch { return null; } } } });
  try {
    const d = await ctx.api.get(path);
    if (card.isOpen()) card.update(airportSpec(d));
  } catch {
    if (card.isOpen()) card.update({ subtitle: 'Unavailable', sections: [{ text: 'The movement counts could not be loaded right now.' }] });
  }
  return card;
}

// ------------------------------------------------------------------ GPS interference episode card
function gpsSpec(e) {
  const { h, fmt } = ctx;
  const data = e.data || {};
  const badges = [{ text: data.change === 'worse' ? 'Worse than its week' : data.change === 'new' ? 'New this week' : 'New or worse', tone: 'red' },
    data.confidence ? { text: `Confidence ${data.confidence}`, tone: 'blue' } : null,
    e.active ? null : { text: 'Ended', tone: 'grey' }];
  const days = data.days_with_data;
  const cells = (e.cells || []).map(c => ({
    title: `${c.lat0.toFixed(1)}° to ${(c.lat0 + c.size).toFixed(1)}°, ${c.lon0.toFixed(1)}° to ${(c.lon0 + c.size).toFixed(1)}°`,
    subtitle: c.share_now != null ? `${pct(c.share_now)} now, ${pct(c.share_base || 0)} in the same hours on earlier days` : c.label || '',
    entity: { kind: 'gps_jam', id: `gpsjam:${c.lat0}:${c.lon0}:${c.size}`, title: 'GPS interference', lat: c.lat0 + c.size / 2, lon: c.lon0 + c.size / 2, layer: 'flights.gpsjam' },
  }));
  // the real baseline: how many earlier days had enough aircraft in these hours, and how many aircraft-hours
  const base = days != null
    ? `in the same hours on ${days} earlier ${days === 1 ? 'day' : 'days'} with data (${fmt.int(data.base_total || 0)} aircraft-hours)`
    : 'in the same cells over the previous 7 days';
  const evidence = `${pct(data.share_now)} of ${fmt.int(data.recent_total)} aircraft-hours in the last 6 complete hours reported degraded GPS accuracy `
    + `(${data.n} ${data.n === 1 ? 'cell' : 'cells'}), against ${pct(data.share_base || 0)} ${base}. `
    + `First flagged ${fmt.utc(e.first_time)}${e.active ? '' : `, last flagged ${fmt.utc(e.time)}`}.`;
  return {
    key: `air_activity:${e.id}`, kind: 'air_activity', title: e.title, subtitle: e.subtitle, icon: '⌁', accent: '#e5534b',
    badges: badges.filter(Boolean),
    block: [{ label: 'Affected now', value: pct(data.share_now) }, { label: 'Same hours before', value: pct(data.share_base || 0) },
      { label: 'Days with data', value: days != null ? `${days} of 7` : null }, { label: 'Cells', value: data.n }],
    sections: [
      { title: 'Evidence', text: evidence },
      data.confidence ? { title: 'Confidence', text: `${data.confidence[0].toUpperCase()}${data.confidence.slice(1)}: ${(data.confidence_reasons || []).join('; ')}.` } : null,
      e.days?.length ? { title: 'Affected share by day (UTC), these cells', el: dayChart(e.days) } : null,
      cells.length ? { title: 'Cells', list: cells.slice(0, 20) } : null,
      { title: 'Why this may be innocent', el: h('ul', { class: 'flights-act-why' }, (e.innocent || []).map(x => h('li', null, x))) },
      { title: 'How this is measured', text: `${e.method} ${e.limits}` },
    ],
    actions: [{ text: 'GPS interference layer', onClick: () => { ctx.layers.setOn('flights.gpsjam', true); if (data.bbox) ctx.map2.fitBbox(data.bbox); } }],
    entity: { kind: 'air_activity', id: e.id, title: e.title, subtitle: e.subtitle, lat: e.lat, lon: e.lon, layer: 'flights.activity', bbox: data.bbox, zoom: 7 },
    foot: 'Derived from the navigation accuracy aircraft report (adsb.fi open data). Not a measurement of radio signals.',
  };
}

export async function openEpisode(id, seed = null) {
  const path = '/api/flights/activity/episode';
  const card = ctx.cards.open({ key: `air_activity:${id}`, kind: 'air_activity', title: seed?.title || 'Air activity', subtitle: 'Loading…',
    icon: '⌁', accent: GROUP, sections: [{ text: 'Loading…' }] });
  try {
    const e = await ctx.api.get(path, { id });
    if (e.category === 'military_surge') { card.close(); return openRegion(e.ref, e); }
    if (e.category === 'airport_drop') { card.close(); return openAirport(e.ref, e); }
    if (card.isOpen()) card.update(gpsSpec(e));
  } catch (err) {
    if (card.isOpen()) {
      card.update({ subtitle: err?.status === 404 ? 'No longer kept' : 'Unavailable',
        sections: [{ text: err?.status === 404 ? 'Air activity episodes are kept for 30 days; this one has gone.' : 'This episode could not be loaded right now.' }],
        entity: seed && ctx.dom.validLatLon(seed.lat, seed.lon) ? { kind: 'air_activity', id, title: seed.title || 'Air activity', lat: seed.lat, lon: seed.lon, layer: 'flights.activity' } : null });
    }
  }
  return card;
}

/** Opens the card for any air_activity entity, also a bare {kind, id} from favourites or case files. */
export function openEntity(e) {
  const id = String(e?.id || '');
  const m = /^flights:(surge|airport|gps):([^:]+)(?::(.*))?$/.exec(id);
  if (!m) return null;
  const [, kind, ref, rest] = m;
  if (kind === 'surge') return openRegion(ref, e);
  if (kind === 'airport') return openAirport(ref, e);
  return rest ? openEpisode(id, e) : null;
}

// ------------------------------------------------------------------ drawer
function tableOf(head, rows) {
  const { h } = ctx;
  const tbl = h('table', { class: 'tbl flights-act-tbl' }, h('tr', null, head.map(([label, numCol]) => h('th', { class: numCol ? 'num' : null }, label))));
  for (const r of rows) tbl.append(r);
  return tbl;
}

function linkCell(title, sub, onClick, tone = null) {
  const { h } = ctx;
  const b = h('button', { type: 'button', class: 'text-btn flights-act-link' }, title);
  b.addEventListener('click', onClick);
  return h('td', null, tone ? h('span', { class: `flights-act-dot ${tone}`, 'aria-hidden': 'true' }) : null, b,
    sub ? h('div', { class: 'flights-act-sub' }, sub) : null);
}

function recentList(category) {
  const { h, fmt } = ctx;
  const eps = (summary?.episodes || []).filter(e => e.category === category);
  if (!eps.length) return null;
  const ul = h('ul', { class: 'card-list flights-act-recent' });
  for (const e of eps.slice(0, 8)) {
    const b = h('button', { type: 'button' }, h('span', { class: 'li-d' }, fmt.ago(e.time)), h('span', { class: 'li-t' }, e.title), h('span', { class: 'li-s' }, e.subtitle));
    b.addEventListener('click', () => ctx.entities.open(e));
    ul.append(h('li', null, b));
  }
  return h('div', null, h('h3', { class: 'flights-act-h3' }, 'Last 7 days'), ul);
}

function renderMilitary(body) {
  const { h } = ctx;
  const s = summary;
  const mil = s.sampler?.military || {};
  body.append(h('p', { class: 'flights-act-note' }, mil.on
    ? `The worldwide military list is sampled every ${Math.round((mil.every_s || 600) / 60)} minutes (${ctx.fmt.int(mil.snapshots_24h)} of ${ctx.fmt.int(mil.expected_24h)} snapshots in the last 24 hours). Each region is compared with the same hours on earlier days, and on the same weekday after two weeks.`
    : 'The background military sampler is off (FLIGHTS_MIL_SAMPLER=0), so baselines only grow while the military layer is on.'));
  const rows = s.regions.map((r) => {
    const tone = r.level >= 3 ? 'red' : r.level === 2 ? 'amber' : null;
    const sub = r.state === 'learning' ? `Learning: ${r.baseline_days} of ${r.needed} days` : r.status;
    return h('tr', { class: r.level === 0 ? 'flights-act-dim' : '' },
      linkCell(r.name, sub, () => openRegion(r.id, r.entity), tone),
      h('td', { class: 'num' }, num(r.per_hour) ?? '–'), h('td', { class: 'num' }, num(r.usual_per_hour) ?? '–'),
      h('td', { class: 'num' }, r.ratio != null && r.level >= 1 ? `${num(r.ratio)}×` : '–'));
  });
  body.append(tableOf([['Region'], ['Now /h', true], ['Usual /h', true], ['×', true]], rows));
  const rec = recentList('military_surge');
  if (rec) body.append(rec);
}

function renderGps(body) {
  const { h } = ctx;
  const g = summary.gps || {};
  const c = g.counts || {};
  const total = c.amber_or_red_24h || 0;
  const fresh = (c.new || 0) + (c.worse || 0);
  const groups = [[c.chronic, 'repeat daily'], [c.recurring, 'come and go'], [c.easing, 'are easing'],
    [fresh, 'are new or worse than their own week'], [c.learning, 'are still learning'], [c.quiet, 'were quiet all week']]
    .filter(([n]) => n).map(([n, words]) => `${ctx.fmt.int(n)} ${words}`);
  const listed = groups.length > 1 ? `${groups.slice(0, -1).join(', ')} and ${groups[groups.length - 1]}` : groups.join('');
  body.append(h('p', { class: 'flights-act-note' }, total
    ? `Of ${ctx.fmt.int(total)} cells with interference in the last 24 hours, ${listed}. Only new or worse areas raise events.`
    : 'No cell has had interference in the last 24 hours, or the GPS sampler has not run long enough yet.'));
  if (!summary.sampler?.gps?.sampler_on) body.append(h('p', { class: 'flights-act-note' }, 'The GPS hotspot sampler is off, so cells only have data while you view them.'));
  if (!g.clusters?.length) {
    body.append(h('p', { class: 'flights-act-empty' }, 'No new or worsening interference right now.'));
  } else {
    const ul = h('ul', { class: 'card-list' });
    for (const cl of g.clusters) {
      const b = h('button', { type: 'button' }, h('span', { class: 'li-d' }, `${cl.cells} ${cl.cells === 1 ? 'cell' : 'cells'}`), h('span', { class: 'li-t' }, cl.title), h('span', { class: 'li-s' }, cl.subtitle));
      b.addEventListener('click', () => (cl.episode ? openEpisode(cl.episode, cl.entity) : ctx.map2.fitBbox(cl.bbox)));
      ul.append(h('li', null, b));
    }
    body.append(ul);
  }
  const rec = recentList('gps_interference');
  if (rec) body.append(rec);
}

function renderAirports(body) {
  const { h } = ctx;
  const list = summary.airports || [];
  const shown = list.filter(a => a.level >= 2 || a.background || a.hours_seen > 0);
  const rest = list.length - shown.length;
  const watch = summary.sampler?.airports || {};
  const gpsOn = !!summary.sampler?.gps?.sampler_on;
  const bg = [gpsOn ? "Airports inside the GPS sampler's circles" : null, watch.watch_on ? `favourited airports (up to ${watch.watch_max})` : null].filter(Boolean);
  const how = bg.length ? `${bg.join(' and ')} are sampled in the background; others only while you view them.`
    : 'Nothing is sampled in the background right now (the GPS sampler and favourite sampling are off), so airports are counted only while you view them.';
  body.append(h('p', { class: 'flights-act-note' }, `Arrivals and departures from ADS-B snapshots every 10 minutes. ${how.charAt(0).toUpperCase()}${how.slice(1)} A drop needs 2 hours at 40% of usual or less while overflights stay normal.`));
  if (!shown.length) {
    body.append(h('p', { class: 'flights-act-empty' }, 'No airport has been sampled yet.'));
  } else {
    const rows = shown.map((a) => {
      const tone = a.level >= 3 ? 'red' : a.level === 2 ? 'amber' : null;
      const sub = a.state === 'learning' ? `Learning: ${a.days} of ${a.needed} days` : a.status;
      const judged = !['learning', 'no_coverage'].includes(a.state);
      return h('tr', { class: a.level === 0 ? 'flights-act-dim' : '' },
        linkCell(`${a.name} (${a.ident})`, sub, () => openAirport(a.ident, a.entity), tone),
        h('td', { class: 'num' }, judged ? ctx.fmt.int(a.value) : '–'), h('td', { class: 'num' }, num(a.usual) ?? '–'),
        h('td', { class: 'num' }, judged && a.share != null ? pct(a.share) : '–'));
    });
    body.append(tableOf([['Airport'], ['Last hour', true], ['Usual', true], ['Share', true]], rows));
  }
  if (rest > 0) body.append(h('p', { class: 'flights-act-note' }, `${rest} more major airports are counted only while you view them. Favourite an airport (its card's star) to sample it in the background.`));
  const rec = recentList('airport_drop');
  if (rec) body.append(rec);
}

async function loadDrawer() {
  if (!drawer) return;
  const { h } = ctx;
  const body = drawer.panel;
  try {
    summary = await ctx.api.get('/api/flights/activity');
  } catch (err) {
    ctx.dom.clear(body);
    body.append(h('p', { class: 'flights-act-empty' }, `Air activity is unavailable right now (${err?.message || 'no answer'}).`));
    return;
  }
  if (!drawer) return;
  renderTabs();
  ctx.dom.clear(body);
  if (tab === 'gps') renderGps(body);
  else if (tab === 'airports') renderAirports(body);
  else renderMilitary(body);
  body.append(h('p', { class: 'flights-act-foot' }, (summary.caveats || []).join(' ')));
}

function renderTabs() {
  const { h } = ctx;
  const bar = drawer.tabs;
  ctx.dom.clear(bar);
  const counts = {
    military: (summary?.regions || []).filter(r => r.level >= 2).length,
    gps: (summary?.gps?.clusters || []).length,
    airports: (summary?.airports || []).filter(a => a.level >= 2).length,
  };
  for (const t of TABS) {
    const b = h('button', { type: 'button', role: 'tab', class: 'flights-act-tab', 'aria-selected': String(t.id === tab) }, t.label,
      counts[t.id] ? h('span', { class: 'flights-act-count' }, String(counts[t.id])) : null);
    b.addEventListener('click', () => { tab = t.id; ctx.settings.set('flights.activity.tab', tab); loadDrawer(); });
    bar.append(b);
  }
}

export function toggleDrawer() {
  if (drawer) { drawer.handle.close(); return; }
  const { h } = ctx;
  tab = ctx.settings.get('flights.activity.tab', 'military');
  if (!TABS.some(t => t.id === tab)) tab = 'military';
  const tabs = h('div', { class: 'flights-act-tabs', role: 'tablist' });
  const panel = h('div', { class: 'flights-act-panel', role: 'tabpanel' }, h('p', { class: 'flights-act-empty' }, 'Loading…'));
  const handle = ctx.ui.drawer({ id: DRAWER, title: 'Air activity', el: h('div', { class: 'flights-act-drawer' }, tabs, panel), width: 460,
    onClose: () => { drawer = null; clearInterval(drawerTimer); drawerTimer = null; btn?.setAttribute('aria-pressed', 'false'); } });
  drawer = { handle, tabs, panel };
  btn?.setAttribute('aria-pressed', 'true');
  loadDrawer();
  drawerTimer = setInterval(() => { if (document.visibilityState === 'visible') loadDrawer(); }, 60000);
}

// ------------------------------------------------------------------ map layer
function ensureLayers() {
  ctx.map2.addSource(SRC, { type: 'geojson', data: ctx.map2.emptyFC() });
  ctx.map2.addLayer({ id: L_FILL, type: 'fill', source: SRC,
    filter: ['any', ['==', ['get', 'kind'], 'gps'], ['all', ['==', ['get', 'kind'], 'region'], ['>=', ['get', 'level'], 2]]],
    paint: { 'fill-color': ['case', ['==', ['get', 'kind'], 'gps'], RED, ['>=', ['get', 'level'], 3], RED, AMBER],
      'fill-opacity': ['case', ['==', ['get', 'kind'], 'gps'], 0.3, ['>=', ['get', 'level'], 3], 0.16, 0.1] } }, 'polygons');
  ctx.map2.addLayer({ id: L_LINE, type: 'line', source: SRC, filter: ['in', ['get', 'kind'], ['literal', ['region', 'gps']]],
    paint: { 'line-color': ['case', ['==', ['get', 'kind'], 'gps'], RED, ['>=', ['get', 'level'], 3], RED, ['==', ['get', 'level'], 2], AMBER, GROUP],
      'line-width': ['case', ['>=', ['get', 'level'], 2], 1.6, 1],
      'line-opacity': ['case', ['>=', ['get', 'level'], 2], 0.9, ['==', ['get', 'level'], 1], 0.45, 0.25] } }, 'lines');
  ctx.map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC, filter: ['==', ['get', 'kind'], 'region_label'],
    layout: { 'text-field': ['get', 'text'], 'text-font': ['Open Sans Bold'], 'text-size': ['interpolate', ['linear'], ['zoom'], 2, 10.5, 6, 13],
      'text-max-width': 9, 'symbol-sort-key': ['-', 0, ['get', 'level']] },
    paint: { 'text-color': ['match', ['get', 'level'], 3, '#ffd6d2', 2, '#ffe2b0', '#e8c7da'], 'text-halo-color': INK, 'text-halo-width': 1.4,
      'text-opacity': ['case', ['>=', ['get', 'level'], 1], 0.95, 0.6] } }, 'top');
  ctx.map2.addLayer({ id: L_APT, type: 'circle', source: SRC, filter: ['==', ['get', 'kind'], 'airport'],
    paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, ['match', ['get', 'level'], 3, 6, 2, 5, 3], 8, ['match', ['get', 'level'], 3, 10, 2, 8, 5]],
      'circle-color': ['match', ['get', 'level'], 3, RED, 2, AMBER, 'rgba(224,90,168,0.25)'],
      'circle-stroke-color': ['match', ['get', 'level'], 3, RED, 2, AMBER, GROUP], 'circle-stroke-width': 1.4 } }, 'points');
  ctx.map2.addLayer({ id: L_APT_LABEL, type: 'symbol', source: SRC, filter: ['all', ['==', ['get', 'kind'], 'airport'], ['>=', ['get', 'level'], 2]],
    layout: { 'text-field': ['get', 'label'], 'text-font': ['Open Sans Bold'], 'text-size': 11, 'text-anchor': 'left', 'text-offset': [0.9, 0] },
    paint: { 'text-color': ['match', ['get', 'level'], 3, '#ffd6d2', '#ffe2b0'], 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
}

async function refresh({ signal }) {
  const fc = await ctx.api.get('/api/flights/activity/map', null, { signal });
  if (signal.aborted || !layer.isOn()) return;
  ctx.map2.setData(SRC, fc);
  const props = (fc.features || []).map(f => f.properties || {});
  const regions = props.filter(p => p.kind === 'region');
  const surges = regions.filter(p => p.level >= 3).length;
  const busier = regions.filter(p => p.level === 2).length;
  const learning = regions.filter(p => p.state === 'learning').length;
  const gps = props.filter(p => p.kind === 'gps').length;
  const drops = props.filter(p => p.kind === 'airport' && p.level >= 3).length;
  layer.setCount(fc.meta?.flags || 0);
  const parts = [];
  if (surges) parts.push(`${surges} ${surges === 1 ? 'region' : 'regions'} with unusual military activity`);
  if (busier) parts.push(`${busier} busier than usual`);
  if (gps) parts.push(`${gps} new or worse GPS ${gps === 1 ? 'area' : 'areas'}`);
  if (drops) parts.push(`${drops} airport ${drops === 1 ? 'drop' : 'drops'}`);
  const msg = parts.length ? `${parts.join(', ')}.` : 'Nothing unusual right now.';
  layer.setStatus('ok', learning ? `${msg} ${learning} of ${regions.length} regions are still learning their baseline.` : msg);
}

export function register(c) {
  ctx = c;
  layer = ctx.layers.register({
    id: 'flights.activity', group: 'Flights', title: 'Air activity', subtitle: 'Changes against the usual for the place and time',
    description: 'Candidates from Meridian\'s own baselines: regions with more military aircraft than usual, new or worsening GPS interference, and airports with far fewer movements than usual. Each card gives the evidence, the coverage and innocent explanations.',
    source: 'Derived from adsb.fi open data (personal, non-commercial)', color: GROUP, symbol: 'area', defaultOn: false, refreshMs: 300000,
    legend: [{ color: RED, label: 'Unusual military activity, new GPS interference or an airport drop', shape: 'area' },
      { color: AMBER, label: 'Busier than usual, or a drop not yet confirmed', shape: 'area' },
      { color: GROUP, label: 'Watched region or sampled airport', shape: 'line' }],
    async enable() { ensureLayers(); },
    async disable() { ctx.map2.removeSource(SRC); },
    refresh,
  });
  ctx.map2.onClick([L_FILL, L_LINE, L_LABEL, L_APT, L_APT_LABEL], (f) => {
    const p = f.properties || {};
    if (p.kind === 'airport') return openAirport(p.id, { title: p.name });
    if (p.kind === 'gps') return p.id ? openEpisode(p.id, { title: p.title, lat: p.lat, lon: p.lon }) : null;
    return openRegion(p.id, { title: p.name });
  });
  ctx.map2.onHover([L_FILL, L_LINE, L_LABEL, L_APT], (f) => {
    const p = f.properties || {};
    if (p.kind === 'airport') return `${p.name} (${p.id}): ${p.status}`;
    if (p.kind === 'gps') return p.title;
    if (p.kind === 'region' || p.kind === 'region_label') return p.status ? `${p.name || p.text}: ${p.status}` : p.text;
    return null;
  });
  ctx.entities.handler('air_activity', (e) => openEntity(e), { group: 'Flights', icon: '✈' });
  btn = ctx.ui.toolbarButton({ id: 'flights-activity', title: 'Air activity: military surges, GPS interference trends and airport drops',
    label: 'Air', icon: ICON, order: 31, onClick: () => toggleDrawer() });
  return { layer, openRegion, openAirport, openEpisode, openEntity, toggleDrawer, summary: () => summary };
}
