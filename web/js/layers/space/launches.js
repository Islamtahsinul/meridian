// Space module: orbital launches at their pads (The Space Devs Launch Library 2).

import { COLOR, countdown, shortCountdown, dayLabel, hm, span, sentence } from './common.js';

const SRC = 'ov-space-launches';
const L_HALO = 'ov-space-launch-halo';
const L_DOT = 'ov-space-launch';
const L_LABEL = 'ov-space-launch-label';
const STATUS_TONE = { Go: 'green', TBD: 'grey', TBC: 'amber', Hold: 'amber', 'In Flight': 'blue', Success: 'green', Failure: 'red', 'Partial Failure': 'amber' };

const LS = { ctx: null, layer: null, data: [], byId: new Map(), at: 0, tickers: new Set(), tickTimer: null };

// ------------------------------------------------------------------ countdown tickers (one timer for every open card)
function startTicker() {
  if (LS.tickTimer) return;
  LS.tickTimer = setInterval(() => {
    for (const t of LS.tickers) {
      if (!t.el.isConnected) { t.misses = (t.misses || 0) + 1; if (t.misses > 5) LS.tickers.delete(t); continue; }
      t.misses = 0;
      t.el.textContent = countdown(t.at - Date.now());
    }
    if (!LS.tickers.size) { clearInterval(LS.tickTimer); LS.tickTimer = null; }
  }, 1000);
}

function ticker(atMs) {
  const el = LS.ctx.h('span', { class: 'space-countdown' }, countdown(atMs - Date.now()));
  const t = { el, at: atMs };
  LS.tickers.add(t);
  startTicker();
  return t;
}

// ------------------------------------------------------------------ data
async function load({ force = false, signal } = {}) {
  if (!force && LS.data.length && Date.now() - LS.at < 10 * 60000) return LS.data;
  const res = await LS.ctx.api.get('/api/space/launches', { which: 'all' }, { signal, timeoutMs: 45000 });
  LS.data = res.launches || [];
  LS.byId = new Map(LS.data.map(r => [r.id, r]));
  LS.at = Date.now();
  return LS.data;
}

function isPast(r, now = Date.now()) {
  if (['Success', 'Failure', 'Partial Failure'].includes(r.status)) return true;
  if (r.status === 'In Flight') return false;
  return r.net * 1000 < now - 3600000;
}

function filtered(options, now = Date.now()) {
  const aheadMs = (Number(options.ahead) || 30) * 86400000;
  const recent = options.recent !== false;
  return LS.data.filter((r) => {
    const t = r.net * 1000;
    if (isPast(r, now)) return recent && t >= now - 7 * 86400000;
    return t <= now + aheadMs;
  });
}

function rel(r, now = Date.now()) {
  const dt = r.net - now / 1000;
  if (isPast(r, now)) return `${span(-dt)} ago`;
  if (dt < 0) return 'due now';
  return `in ${span(dt)}`;
}

function features(options) {
  const now = Date.now();
  const byPad = new Map();
  for (const r of filtered(options, now)) {
    const k = r.pad.id;
    if (!byPad.has(k)) byPad.set(k, []);
    byPad.get(k).push(r);
  }
  const feats = [];
  for (const list of byPad.values()) {
    const upcoming = list.filter(r => !isPast(r, now)).sort((a, b) => a.net - b.net);
    const past = list.filter(r => isPast(r, now)).sort((a, b) => b.net - a.net);
    const main = upcoming[0] || past[0];
    const up = !isPast(main, now);
    const soon = up && main.net * 1000 - now < 86400000;
    const title = main.mission || main.name;
    const label = `${title}, ${up ? rel(main, now) : `launched ${rel(main, now)}`}`;
    feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [main.pad.lon, main.pad.lat] },
      properties: { lid: main.id, pid: main.pad.id, up: up ? 1 : 0, soon: soon ? 1 : 0, fail: main.status === 'Failure' ? 1 : 0,
        n: list.length, label, sort: up ? -main.net : 1e10 - main.net, title, pad: main.pad.name, rocket: main.rocket } });
  }
  return { type: 'FeatureCollection', features: feats };
}

function render() {
  const fc = features(LS.layer.options());
  LS.ctx.map2.setData(SRC, fc);
  const up = fc.features.filter(f => f.properties.up).length;
  LS.layer.setCount(filtered(LS.layer.options()).length);
  return up;
}

// ------------------------------------------------------------------ cards
function launchBlock(r) {
  const now = Date.now();
  const past = isPast(r, now);
  const ws = r.window_start || r.net;
  return [
    { label: past ? 'Since launch' : 'Countdown', value: shortCountdown(r.net * 1000 - now) },
    { label: past ? 'Lift-off' : 'Opens', value: hm(past ? r.net : ws), unit: 'UTC' },
    { label: 'Date', value: dayLabel(r.net, { weekday: false }) },
    // The probability is Launch Library's chance of good launch weather: only meaningful before lift-off.
    past ? null : r.probability != null ? { label: 'Good weather', value: String(r.probability), unit: '%' } : { label: 'Status', value: r.status },
  ].filter(Boolean);
}

function launchBadges(r) {
  const out = [{ text: sentence(r.status_name) || r.status, tone: STATUS_TONE[r.status] || 'grey', title: r.status_text || null }];
  if (r.webcast_live) out.push({ text: 'Webcast live', tone: 'blue' });
  if (r.crewed) out.push({ text: 'Crewed', tone: 'blue' });
  if (r.net_precision && !['Second', 'Minute'].includes(r.net_precision)) out.push({ text: `Time known to the ${r.net_precision.toLowerCase()}`, tone: 'grey' });
  return out;
}

function windowText(r) {
  if (!r.window_start || !r.window_end) return null;
  if (Math.abs(r.window_end - r.window_start) < 60) return `Instantaneous, ${hm(r.window_start)} UTC`;
  return `${hm(r.window_start)} to ${hm(r.window_end)} UTC (${span(r.window_end - r.window_start)})`;
}

export async function openLaunchCard(rec) {
  const ctx = LS.ctx;
  const { h, fmt } = ctx;
  let r = rec;
  let samePad = null;
  if (!r || !r.pad) {
    try { r = await ctx.api.get(`/api/space/launch/${encodeURIComponent(rec.id || rec)}`); samePad = r.same_pad || []; } catch (err) {
      return ctx.cards.open({ key: `launch:${rec.id || rec}`, kind: 'launch', title: rec.title || 'Launch', icon: '▲', accent: COLOR.space,
        sections: [{ text: err.status === 404 ? 'This launch is no longer in the Launch Library lists the map keeps (it may be older than the recent list).' : `The launch could not be loaded right now (${err.message}).` }] });
    }
  }
  if (!samePad) samePad = LS.data.filter(x => x.pad.id === r.pad.id && x.id !== r.id);
  const now = Date.now();
  samePad = samePad.slice().sort((a, b) => (isPast(a, now) - isPast(b, now)) || (isPast(a, now) ? b.net - a.net : a.net - b.net)).slice(0, 6);
  const t = ticker(r.net * 1000);
  const past = isPast(r, now);
  const tickBox = h('div', { class: 'space-tminus' }, t.el,
    h('span', { class: 'faint' }, past ? `Launched ${dayLabel(r.net)} at ${hm(r.net)} UTC` : `NET ${dayLabel(r.net)}, ${hm(r.net)} UTC${r.net_precision && !['Second', 'Minute'].includes(r.net_precision) ? ' (not final)' : ''}`));
  const links = [];
  for (const v of r.videos || []) links.push({ text: v.title ? `Watch: ${v.title}` : `Webcast on ${v.source || 'video'}`, href: v.url });
  for (const i of r.info || []) links.push({ text: i.title || i.source || 'Mission page', href: i.url });
  if (r.pad.wiki_url) links.push({ text: 'Pad on Wikipedia', href: r.pad.wiki_url });
  if (r.rocket_url) links.push({ text: `${r.rocket_family || 'Rocket'} details`, href: r.rocket_url });
  const card = ctx.cards.open({
    key: `launch:${r.id}`, kind: 'launch', title: r.mission || r.name, subtitle: [r.rocket, r.provider].filter(Boolean).join(', '),
    icon: '▲', accent: COLOR.space,
    badges: launchBadges(r),
    block: launchBlock(r),
    media: r.image ? { type: 'image', src: r.image, alt: r.name, credit: 'Image via The Space Devs' } : null,
    sections: [
      { el: tickBox },
      { title: 'Launch', rows: [
        ['Window', windowText(r)],
        ['Status', r.status_text || sentence(r.status_name)],
        ['Weather', r.weather_concerns || null],
        ['On hold because', r.holdreason || null],
        ['Failure', r.failreason || null],
      ] },
      { title: 'Mission', rows: [
        ['Type', sentence(r.mission_type) || null], ['Orbit', sentence(r.orbit) || null], ['Programme', (r.programs || []).join(', ') || null],
        ['Crew', r.crewed ? 'Yes' : null],
      ] },
      r.mission_description ? { title: 'About', text: r.mission_description } : null,
      { title: 'Vehicle and provider', rows: [
        ['Rocket', r.rocket || null],
        ['Provider', [r.provider, sentence(r.provider_type), r.provider_country].filter(Boolean).join(', ') || null],
      ] },
      { title: 'Pad', rows: [['Pad', r.pad.name], ['Site', r.pad.location || null], ['Position', fmt.latlon(r.pad.lat, r.pad.lon, 3)]] },
      samePad.length ? { title: 'Also from this pad', list: samePad.map(x => ({ title: x.mission || x.name, subtitle: `${x.rocket}, ${dayLabel(x.net)} ${hm(x.net)} UTC`, detail: rel(x), onClick: () => openLaunchCard(x) })) } : null,
    ],
    links,
    entity: { kind: 'launch', id: r.id, title: r.mission || r.name, subtitle: [r.rocket, r.provider].filter(Boolean).join(', '),
      lat: r.pad.lat, lon: r.pad.lon, layer: 'space.launches', time: r.net, zoom: 9 },
    live: { everyMs: 20000, update: () => ({ block: launchBlock(r), badges: launchBadges(r) }) },
    foot: 'From The Space Devs Launch Library 2. NET means no earlier than; times change often in the last days.',
    onClose: () => LS.tickers.delete(t),
  });
  return card;
}

async function openPadCard(entity) {
  const ctx = LS.ctx;
  const { h, fmt } = ctx;
  const pid = String(entity.id || '').replace('ll2pad:', '');
  let res;
  try { res = await ctx.api.get(`/api/space/pad/${encodeURIComponent(pid)}`); } catch (err) {
    return ctx.cards.open({ key: `launch_pad:${pid}`, kind: 'launch_pad', title: entity.title || 'Launch pad', icon: '▲', accent: COLOR.space,
      sections: [{ text: `This pad could not be loaded right now (${err.message}).` }] });
  }
  const p = res.pad;
  const now = Date.now();
  const list = (res.launches || []).slice().sort((a, b) => (isPast(a, now) - isPast(b, now)) || (isPast(a, now) ? b.net - a.net : a.net - b.net));
  const next = list.find(x => !isPast(x, now));
  return ctx.cards.open({
    key: `launch_pad:${p.id}`, kind: 'launch_pad', title: p.name, subtitle: p.location || 'Launch pad', icon: '▲', accent: COLOR.space,
    block: [
      { label: 'Next launch', value: next ? shortCountdown(next.net * 1000 - now) : 'None listed' },
      { label: 'Launches from here', value: p.total_launch_count ? fmt.int(p.total_launch_count) : '–' },
      { label: 'Country', value: p.country_code || '–' },
    ],
    sections: [
      { title: 'Upcoming and recent', list: list.slice(0, 8).map(x => ({ title: x.mission || x.name, subtitle: `${x.rocket}, ${dayLabel(x.net)} ${hm(x.net)} UTC`, detail: rel(x), onClick: () => openLaunchCard(x) })),
        empty: 'Nothing from this pad in the upcoming or recent lists.' },
      { title: 'Pad', rows: [['Position', fmt.latlon(p.lat, p.lon, 4)], ['Time zone', p.timezone || null]] },
    ],
    links: [p.wiki_url ? { text: 'Pad on Wikipedia', href: p.wiki_url } : null, p.map_url ? { text: 'Satellite view', href: p.map_url } : null].filter(Boolean),
    entity: { kind: 'launch_pad', id: `ll2pad:${p.id}`, title: p.name, subtitle: p.location, lat: p.lat, lon: p.lon, layer: 'space.launches', zoom: 12 },
    foot: 'From The Space Devs Launch Library 2.',
  });
}

// ------------------------------------------------------------------ register
export async function registerLaunches(ctx) {
  LS.ctx = ctx;
  LS.layer = ctx.layers.register({
    id: 'space.launches', group: 'Space', title: 'Launches', subtitle: 'Upcoming and recent orbital launches',
    description: 'Every orbital launch on the books at its pad. Larger rings launch within 24 hours; hollow circles launched in the last week.',
    source: 'The Space Devs Launch Library 2 (free tier)', color: COLOR.space, symbol: 'point', refreshMs: 60000,
    options: [
      { id: 'ahead', label: 'Look ahead', type: 'select', default: '30', refresh: false, choices: [
        { value: '1', label: 'Next 24 hours' }, { value: '3', label: 'Next 3 days' }, { value: '7', label: 'Next week' },
        { value: '30', label: 'Next 30 days' }, { value: '3650', label: 'Everything listed' }] },
      { id: 'recent', label: 'Launches in the last week', type: 'toggle', default: true, refresh: false },
      { id: 'labels', label: 'Labels', type: 'toggle', default: true, refresh: false },
    ],
    legend: [{ color: COLOR.space, label: 'Upcoming' }, { color: '#ffffff', label: 'Within 24 hours (outer ring)' }, { color: '#4b4178', label: 'Launched this week' },
      { color: COLOR.red, label: 'Failed' }],
    async enable({ options }) {
      const { map2 } = ctx;
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: L_HALO, type: 'circle', source: SRC, filter: ['==', ['get', 'soon'], 1],
        paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 9, 8, 14], 'circle-color': 'rgba(0,0,0,0)',
          'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1.5, 'circle-stroke-opacity': 0.85, 'circle-pitch-alignment': 'map' } }, 'points');
      map2.addLayer({ id: L_DOT, type: 'circle', source: SRC,
        paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, ['case', ['==', ['get', 'up'], 1], 4.5, 3.5], 8, ['case', ['==', ['get', 'up'], 1], 8, 6]],
          'circle-color': ['case', ['==', ['get', 'up'], 1], COLOR.space, COLOR.ink],
          'circle-stroke-color': ['case', ['==', ['get', 'fail'], 1], COLOR.red, ['==', ['get', 'up'], 1], COLOR.paper, COLOR.space],
          'circle-stroke-width': ['case', ['==', ['get', 'up'], 1], 1.5, 1.8], 'circle-pitch-alignment': 'map' } }, 'points');
      map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC,
        layout: { visibility: options.labels === false ? 'none' : 'visible',
          'text-field': ['step', ['zoom'], ['case', ['==', ['get', 'soon'], 1], ['get', 'label'], ''], 3, ['get', 'label']],
          'text-font': ['Open Sans Regular'], 'text-size': 11.5, 'text-offset': [0, 1.1], 'text-anchor': 'top', 'text-max-width': 14,
          'text-optional': true, 'symbol-sort-key': ['get', 'sort'], 'text-padding': 4 },
        paint: { 'text-color': '#d9d3f7', 'text-halo-color': COLOR.ink, 'text-halo-width': 1.5 } }, 'top');
    },
    async refresh({ signal }) {
      await load({ signal });
      render();
    },
    async disable() {
      ctx.map2.removeSource(SRC);
    },
    onOption(id, value) {
      if (!LS.layer.isOn()) return;
      if (id === 'labels') { ctx.map2.setVisible(L_LABEL, value !== false); return; }
      render();
    },
  });
  ctx.map2.onClick([L_DOT, L_HALO, L_LABEL], (f) => {
    const r = LS.byId.get(f.properties.lid);
    if (r) openLaunchCard(r);
  });
  ctx.map2.onHover([L_DOT, L_HALO], (f) => {
    const p = f.properties;
    return `${p.title}, ${p.rocket}${p.n > 1 ? ` (+${p.n - 1} more from ${p.pad})` : ''}`;
  });

  ctx.entities.handler('launch', async (entity) => {
    const r = LS.byId.get(entity.id) || (await load().catch(() => null), LS.byId.get(entity.id));
    return openLaunchCard(r || entity);
  }, { group: 'Space', icon: '▲' });
  ctx.entities.handler('launch_pad', (entity) => openPadCard(entity), { group: 'Space', icon: '▲' });

  ctx.search.provider((q) => {
    const ql = q.trim().toLowerCase();
    if (ql.length < 3 || !LS.data.length) return [];
    const word = new RegExp(`(^|[^a-z0-9])${ql.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`);
    const now = Date.now();
    const out = [];
    for (const r of LS.data) {
      const hay = `${r.mission} ${r.name} ${r.rocket} ${r.provider}`.toLowerCase();
      if (!word.test(hay)) continue;
      const up = !isPast(r, now);
      out.push({ kind: 'launch', id: r.id, title: r.mission || r.name, subtitle: `${r.rocket}, ${up ? 'NET' : 'launched'} ${dayLabel(r.net)} ${hm(r.net)} UTC`,
        lat: r.pad.lat, lon: r.pad.lon, layer: 'space.launches', zoom: 9, time: r.net,
        score: ((r.mission || '').toLowerCase().startsWith(ql) ? 0.66 : 0.5) + (up ? 0.08 : 0) });
    }
    return out.sort((a, b) => b.score - a.score).slice(0, 8);
  });
}
