// Space module: satellites (CelesTrak elements, SGP4 in a Web Worker) and the ISS layer.

import {
  COLOR, FAMILY, FAMILY_IDS, loadSat, rowToOmm, propagateOne, orbitInfo, ORBIT_TEXT, groundTrack,
  footprintPolygon, footprintRadiusKm, dayLabel, hm, span, whatsHereOrder,
} from './common.js';

const ISS = 25544;
const TIANGONG = 48274;
const HUBBLE = 20580;
const NAMES = { [ISS]: 'ISS', [TIANGONG]: 'Tiangong', [HUBBLE]: 'Hubble' };
const HOSTS = [ISS, TIANGONG];
const FAMILY_INDEX = { station: 0, nav: 1, earth: 2, general: 3 };
const FALLBACK_GROUPS = [
  ['stations', 'Space stations', 'station', true], ['visual', 'Brightest satellites', 'general', true],
  ['weather', 'Weather satellites (includes NOAA)', 'earth', true], ['goes', 'GOES', 'earth', false],
  ['resource', 'Earth resources', 'earth', false], ['science', 'Science', 'earth', true], ['gps-ops', 'GPS', 'nav', true],
  ['galileo', 'Galileo', 'nav', false], ['glo-ops', 'GLONASS', 'nav', false], ['beidou', 'BeiDou', 'nav', false],
  ['amateur', 'Amateur radio', 'general', false], ['last-30-days', 'Launched in the last 30 days', 'general', false],
  ['geo', 'Geostationary belt', 'general', false], ['starlink', 'Starlink (about 10,000, heavy)', 'general', false],
].map(([id, title, family, def]) => ({ id, title, family, default: def }));

const SRC = 'ov-space-sats';
const SRC_SWARM = 'ov-space-swarm';
const L_ICON = 'ov-space-sats';
const L_SWARM = 'ov-space-sats-swarm';
const SWARM_EVERY_MS = 5000;   // the Starlink swarm redraws every 5 s; named satellites every 1 to 2 s
const L_LABEL = 'ov-space-sats-label';
const SRC_SEL_TRACK = 'ov-space-sel-track';
const SRC_SEL_FOOT = 'ov-space-sel-foot';
const SRC_ISS = 'ov-space-iss';
const SRC_ISS_TRACK = 'ov-space-iss-track';
const SRC_ISS_FOOT = 'ov-space-iss-foot';

const S = {
  ctx: null, sat: null, groups: FALLBACK_GROUPS, fields: null,
  rows: new Map(),            // norad -> {row, groups: Set, omm, satrec?}
  groupData: new Map(),       // group -> {loadedAt, ids, info}
  extras: new Set(),
  order: [], index: new Map(), feats: [], stationIdx: [], nCore: 0, lastSwarm: 0, swarmCount: 0,
  docked: new Map(),          // index -> host norad
  worker: null, workerFailed: false, seq: 0, readySeq: -1, timer: null, waiting: false,
  positions: null, posTime: 0, on: false, layer: null, refreshTimer: null,
  selected: null, selSatrec: null, selTrackAt: 0, follow: null,
  iss: { on: false, layer: null, satrec: null, row: null, timer: null, trackAt: 0, loadedAt: 0, last: null },
};

// ------------------------------------------------------------------ helpers
function fmtKm(n) { return n == null || Number.isNaN(+n) ? '–' : Math.round(+n).toLocaleString('en-GB'); }

function familyOf(groups) {
  let best = 'general';
  for (const g of groups) {
    const meta = S.groups.find(x => x.id === g);
    const f = meta ? meta.family : 'general';
    if (FAMILY[f] && FAMILY[f].rank > FAMILY[best].rank) best = f;
  }
  return best;
}

function satrecFor(id) {
  const e = S.rows.get(id);
  if (!e || !S.sat) return null;
  if (!e.satrec) {
    try { e.satrec = S.sat.json2satrec(e.omm || (e.omm = rowToOmm(S.fields, e.row))); } catch { e.satrec = null; }
  }
  return e.satrec;
}

function addRow(fields, row, group) {
  const id = row[0];
  let e = S.rows.get(id);
  if (!e) { e = { groups: new Set() }; S.rows.set(id, e); }
  if (!e.row || e.row[3] !== row[3]) { e.row = row; e.omm = rowToOmm(fields, row); e.satrec = null; }
  if (group) e.groups.add(group);
  return e;
}

function nameOf(id) { return S.rows.get(id)?.row?.[1] || `NORAD ${id}`; }

function launchYear(cospar) {
  const m = /^(\d{4})-/.exec(cospar || '');
  return m ? m[1] : null;
}

function interval() {
  const n = S.nCore;
  return n <= 2500 ? 1000 : n <= 6000 ? 2000 : 3000;
}

// ------------------------------------------------------------------ worker
function startWorker() {
  if (S.worker || S.workerFailed) return;
  try {
    S.worker = new Worker(new URL('./worker.js', import.meta.url), { type: 'module' });
    S.worker.onmessage = onWorker;
    S.worker.onerror = (e) => {
      console.warn('space: propagation worker failed, falling back to the main thread', e.message || e);
      S.workerFailed = true;
      try { S.worker.terminate(); } catch { /* ignore */ }
      S.worker = null;
      S.readySeq = S.seq;
      schedule(100);
    };
  } catch (err) {
    console.warn('space: module workers unavailable, propagating on the main thread', err);
    S.workerFailed = true;
  }
}

function onWorker(ev) {
  const msg = ev.data || {};
  if (msg.type === 'ready') {
    if (msg.seq === S.seq) { S.readySeq = msg.seq; if (S.on) schedule(0); }
    return;
  }
  if (msg.type === 'pos') {
    S.waiting = false;
    if (msg.setSeq !== S.seq || !S.on) return;
    render(msg.buf, msg.t);
    schedule(interval());
  }
}

/** Fallback when there is no worker: propagate everything on the main thread (tick runs it at most every 2 s). */
function propagateMain(t) {
  const n = S.order.length;
  const buf = new Float32Array(n * 3);
  const d = new Date(t);
  for (let i = 0; i < n; i++) {
    const p = propagateOne(S.sat, satrecFor(S.order[i]), d);
    buf[i * 3] = p ? p.lon : NaN;
    buf[i * 3 + 1] = p ? p.lat : NaN;
    buf[i * 3 + 2] = p ? p.alt : NaN;
  }
  return buf;
}

function schedule(ms) {
  clearTimeout(S.timer);
  S.timer = setTimeout(tick, ms);
}

function tick() {
  if (!S.on) return;
  if (document.visibilityState !== 'visible') { schedule(1500); return; }
  if (!S.order.length) { schedule(1000); return; }
  if (S.worker && !S.workerFailed) {
    if (S.readySeq !== S.seq) { schedule(150); return; }
    if (S.waiting) { schedule(250); return; }
    S.waiting = true;
    const full = S.nCore === S.order.length || !S.positions || Date.now() - S.lastSwarm >= SWARM_EVERY_MS;
    S.worker.postMessage({ type: 'tick', seq: S.seq, t: Date.now(), limit: full ? S.order.length : S.nCore });
    return;
  }
  const t = Date.now();
  render(propagateMain(t), t);
  schedule(Math.max(interval(), 2000));
}

// ------------------------------------------------------------------ data
async function fetchGroup(group, { force = false } = {}) {
  const have = S.groupData.get(group);
  if (have && !force && Date.now() - have.loadedAt < 25 * 60000) return have;
  const res = await S.ctx.api.get('/api/space/satellites', { group }, { timeoutMs: 90000 });
  S.fields = res.fields;
  const ids = [];
  for (const row of res.sats || []) { addRow(res.fields, row, group); ids.push(row[0]); }
  const entry = { loadedAt: Date.now(), ids, info: res.info };
  S.groupData.set(group, entry);
  return entry;
}

function enabledGroups(options) {
  return S.groups.filter(g => options[`g_${g.id}`]).map(g => g.id);
}

async function loadGroups(options, { force = false } = {}) {
  const want = enabledGroups(options);
  const results = await Promise.allSettled(want.map(g => fetchGroup(g, { force })));
  const failed = [];
  results.forEach((r, i) => { if (r.status === 'rejected') failed.push([want[i], r.reason]); });
  rebuild(options);
  if (!want.length) {
    S.layer.setStatus('warn', 'Choose at least one group under the layer options.');
  } else if (failed.length === want.length) {
    throw new Error(failed[0][1]?.message || 'CelesTrak is not answering right now.');
  } else if (failed.length) {
    const names = failed.map(([g]) => S.groups.find(x => x.id === g)?.title || g).join(', ');
    S.layer.setStatus('warn', `Some groups are unavailable right now: ${names}.`);
  } else {
    const stale = want.map(g => S.groupData.get(g)?.info).filter(i => i && i.age_s > 6 * 3600);
    S.layer.setStatus(stale.length ? 'warn' : 'ok', stale.length ? 'Some orbital elements are more than 6 hours old; CelesTrak may be slow to update.' : null);
  }
}

function rebuild(options = S.layer?.options() || {}) {
  const groups = new Set(enabledGroups(options));
  const ids = [];
  const seen = new Set();
  for (const [g, d] of S.groupData) {
    if (!groups.has(g)) continue;
    for (const id of d.ids) if (!seen.has(id)) { seen.add(id); ids.push(id); }
  }
  for (const id of S.extras) if (!seen.has(id) && S.rows.has(id)) { seen.add(id); ids.push(id); }
  const labelsAll = options.labels !== false;
  // named satellites first, the Starlink-only swarm after (it has its own, slower source)
  const isSwarm = (id) => { const shown = [...S.rows.get(id).groups].filter(g => groups.has(g)); return shown.length === 1 && shown[0] === 'starlink'; };
  const core = ids.filter(id => !isSwarm(id));
  const swarm = ids.filter(id => isSwarm(id));
  S.order = core.concat(swarm);
  S.nCore = core.length;
  S.index = new Map(S.order.map((id, i) => [id, i]));
  S.stationIdx = [];
  S.docked = new Map();
  S.feats = S.order.map((id, i) => {
    const e = S.rows.get(id);
    if (i >= S.nCore) return { type: 'Feature', geometry: { type: 'Point', coordinates: [0, 0] }, properties: { id } };
    const shown = new Set([...e.groups].filter(g => groups.has(g)));
    const fam = familyOf(shown.size ? shown : e.groups);
    if (fam === 'station') S.stationIdx.push(i);
    const label = NAMES[id] || (fam === 'station' ? e.row[1] : '');
    return { type: 'Feature', geometry: { type: 'Point', coordinates: [0, 0] },
      properties: { i, id, n: e.row[1], f: FAMILY_INDEX[fam], s: FAMILY[fam].rank, l: label, a: 0, all: labelsAll ? 1 : 0 } };
  });
  S.lastSwarm = 0;
  S.swarmCount = 0;
  if (!swarm.length && S.ctx.map.getSource(SRC_SWARM)) S.ctx.map2.setData(SRC_SWARM, S.ctx.map2.emptyFC());
  S.seq += 1;
  S.positions = null;
  // the worker returns positions by index, so it must get the elements in S.order (named first, swarm after)
  const omm = S.order.map(id => S.rows.get(id).omm);
  if (S.worker && !S.workerFailed) S.worker.postMessage({ type: 'set', seq: S.seq, omm });
  else S.readySeq = S.seq;
  schedule(0);
}

function render(buf, t) {
  const n = S.order.length;
  const full = buf.length >= n * 3;
  // one position buffer for the whole catalogue; partial ticks refresh the named part only
  if (!S.positions || S.positions.length !== n * 3) S.positions = new Float32Array(n * 3).fill(NaN);
  S.positions.set(full ? buf.subarray(0, n * 3) : buf.subarray(0, Math.min(buf.length, S.nCore * 3)));
  S.posTime = t;
  const pos = S.positions;
  findDocked(pos);
  const hideIss = S.iss.on;
  const out = [];
  for (let i = 0; i < S.nCore; i++) {
    const lon = pos[i * 3];
    if (Number.isNaN(lon) || S.docked.has(i)) continue;
    if (hideIss && S.order[i] === ISS) continue;
    const f = S.feats[i];
    f.geometry.coordinates[0] = lon;
    f.geometry.coordinates[1] = pos[i * 3 + 1];
    f.properties.a = Math.round(pos[i * 3 + 2]);
    out.push(f);
  }
  S.ctx.map2.setData(SRC, { type: 'FeatureCollection', features: out });
  if (full && n > S.nCore) {
    const sw = [];
    for (let i = S.nCore; i < n; i++) {
      const lon = pos[i * 3];
      if (Number.isNaN(lon)) continue;
      const f = S.feats[i];
      f.geometry.coordinates[0] = Math.round(lon * 1e4) / 1e4;
      f.geometry.coordinates[1] = Math.round(pos[i * 3 + 1] * 1e4) / 1e4;
      sw.push(f);
    }
    S.ctx.map2.setData(SRC_SWARM, { type: 'FeatureCollection', features: sw });
    S.swarmCount = sw.length;
    S.lastSwarm = Date.now();
  }
  S.layer.setCount(out.length + (n > S.nCore ? S.swarmCount : 0));
}

function findDocked(buf) {
  S.docked.clear();
  const hosts = HOSTS.map(h => [h, S.index.get(h)]).filter(([, i]) => i !== undefined && !Number.isNaN(buf[i * 3]));
  if (!hosts.length) return;
  for (const i of S.stationIdx) {
    const id = S.order[i];
    if (HOSTS.includes(id) || Number.isNaN(buf[i * 3])) continue;
    for (const [host, hi] of hosts) {
      const dLat = (buf[i * 3 + 1] - buf[hi * 3 + 1]) * 111.2;
      const dLon = (buf[i * 3] - buf[hi * 3]) * 111.2 * Math.cos(buf[hi * 3 + 1] * Math.PI / 180);
      const dAlt = buf[i * 3 + 2] - buf[hi * 3 + 2];
      if (Math.hypot(dLat, dLon, dAlt) < 12) { S.docked.set(i, host); break; }
    }
  }
}

function dockedWith(host) {
  const out = [];
  for (const [i, h] of S.docked) if (h === host) out.push(S.order[i]);
  return out;
}

// ------------------------------------------------------------------ map layers
function addSatLayers() {
  const { map2 } = S.ctx;
  map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC(), buffer: 32 });
  map2.addSource(SRC_SWARM, { type: 'geojson', data: map2.emptyFC(), buffer: 16, maxzoom: 7 });
  const color = ['match', ['get', 'f'], 0, FAMILY.station.color, 1, FAMILY.nav.color, 2, FAMILY.earth.color, FAMILY.general.color];
  map2.addLayer({ id: L_SWARM, type: 'circle', source: SRC_SWARM,
    paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 0, 1.1, 4, 1.8, 8, 2.6], 'circle-color': FAMILY.general.color,
      'circle-opacity': 0.7, 'circle-stroke-width': 0, 'circle-pitch-alignment': 'map' } }, 'moving');
  map2.addLayer({ id: L_ICON, type: 'symbol', source: SRC,
    layout: { 'icon-image': 'space-sat', 'icon-size': ['interpolate', ['linear'], ['zoom'], 0, 0.55, 3, 0.68, 7, 0.9],
      'icon-allow-overlap': true, 'icon-ignore-placement': true, 'symbol-sort-key': ['get', 's'],
      'icon-pitch-alignment': 'viewport', 'icon-rotation-alignment': 'viewport' },
    paint: { 'icon-color': color, 'icon-halo-color': COLOR.ink, 'icon-halo-width': 1.1 } }, 'moving');
  map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC,
    filter: ['any', ['!=', ['get', 'l'], ''], ['==', ['get', 'all'], 1]],
    layout: { 'text-field': ['step', ['zoom'], ['get', 'l'], 4.5,
      ['case', ['!=', ['get', 'l'], ''], ['get', 'l'], ['==', ['get', 'all'], 1], ['get', 'n'], '']],
      'text-font': ['Open Sans Regular'], 'text-size': ['interpolate', ['linear'], ['zoom'], 1, 10.5, 6, 12],
      'text-offset': [0, 0.95], 'text-anchor': 'top', 'text-optional': true, 'text-max-width': 12,
      'symbol-sort-key': ['-', 10, ['get', 's']], 'text-padding': 3 },
    paint: { 'text-color': '#d9d3f7', 'text-halo-color': COLOR.ink, 'text-halo-width': 1.4 } }, 'top');
}

function addTrackLayers(srcTrack, srcFoot, idPrefix, color) {
  const { map2 } = S.ctx;
  map2.addSource(srcFoot, { type: 'geojson', data: map2.emptyFC() });
  map2.addLayer({ id: `${idPrefix}-foot-fill`, type: 'fill', source: srcFoot,
    paint: { 'fill-color': color, 'fill-opacity': 0.07 } }, 'lines');
  map2.addLayer({ id: `${idPrefix}-foot-line`, type: 'line', source: srcFoot,
    paint: { 'line-color': color, 'line-opacity': 0.55, 'line-width': 1 } }, 'lines');
  map2.addSource(srcTrack, { type: 'geojson', data: map2.emptyFC() });
  map2.addLayer({ id: `${idPrefix}-track-past`, type: 'line', source: srcTrack, filter: ['==', ['get', 'part'], 'past'],
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': color, 'line-opacity': 0.45, 'line-width': 1.2, 'line-dasharray': [2, 3] } }, 'lines');
  map2.addLayer({ id: `${idPrefix}-track-future`, type: 'line', source: srcTrack, filter: ['==', ['get', 'part'], 'future'],
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': color, 'line-opacity': 0.9, 'line-width': 1.6 } }, 'lines');
}

function trackFC(track) {
  if (!track) return S.ctx.map2.emptyFC();
  return { type: 'FeatureCollection', features: [
    { type: 'Feature', properties: { part: 'past' }, geometry: { type: 'LineString', coordinates: track.past } },
    { type: 'Feature', properties: { part: 'future' }, geometry: { type: 'LineString', coordinates: track.future } },
  ] };
}

function footFC(lat, lon, alt) {
  const poly = footprintPolygon(lat, lon, footprintRadiusKm(alt));
  return poly ? { type: 'FeatureCollection', features: [{ type: 'Feature', properties: {}, geometry: poly }] } : S.ctx.map2.emptyFC();
}

// ------------------------------------------------------------------ selection (track and footprint)
function selectSat(id) {
  S.selected = id;
  S.selSatrec = satrecFor(id);
  S.selTrackAt = 0;
  if (!S.ctx.map.getSource(SRC_SEL_FOOT)) addTrackLayers(SRC_SEL_TRACK, SRC_SEL_FOOT, 'ov-space-sel', COLOR.space);
  updateSelection();
}

function updateSelection() {
  if (S.selected == null || !S.selSatrec) return null;
  if (S.selected === ISS && S.iss.on) { clearSelectionShapes(); return propagateOne(S.sat, S.selSatrec, new Date()); }
  const now = Date.now();
  const p = propagateOne(S.sat, S.selSatrec, new Date(now));
  if (!p) return null;
  S.ctx.map2.setData(SRC_SEL_FOOT, footFC(p.lat, p.lon, p.alt));
  if (now - S.selTrackAt > 30000) {
    const info = orbitInfo(S.selSatrec);
    const per = Math.min(info.period, 24 * 60);
    S.ctx.map2.setData(SRC_SEL_TRACK, trackFC(groundTrack(S.sat, S.selSatrec, now, per, info.period > 300 ? 240 : 180)));
    S.selTrackAt = now;
  }
  return p;
}

function clearSelectionShapes() {
  S.ctx.map2.setData(SRC_SEL_FOOT, S.ctx.map2.emptyFC());
  S.ctx.map2.setData(SRC_SEL_TRACK, S.ctx.map2.emptyFC());
}

function deselect(id) {
  if (S.selected !== id) return;
  S.selected = null;
  S.selSatrec = null;
  if (S.follow === id) S.follow = null;
  if (S.ctx.map.getSource(SRC_SEL_FOOT)) clearSelectionShapes();
}

// ------------------------------------------------------------------ passes
function passList(res, { limit = 5, empty } = {}) {
  const { h } = S.ctx;
  const items = (res.passes || []).slice(0, limit);
  if (!items.length) {
    return h('p', { class: 'dim space-note' }, empty || (res.visible_only
      ? `No visible passes in the next ${Math.round(res.hours)} hours. The ISS shows up around dawn and dusk, when it is in sunlight and you are in darkness.`
      : `No passes above ${res.min_elevation}° in the next ${Math.round(res.hours)} hours.`));
  }
  const ul = h('ul', { class: 'card-list space-passes' });
  const now = Date.now() / 1000;
  for (const p of items) {
    const from = p.visible_from || p.rise.time;
    const to = p.visible_to || p.set.time;
    const maxEl = p.visible_max_el ?? p.max.el;
    const dur = span(to - from);
    const bits = [`${dur}`, `up to ${Math.round(maxEl)}°`, `${p.rise.dir} to ${p.set.dir}`];
    if (p.magnitude != null) bits.push(`about ${p.magnitude.toFixed(1).replace('-', '−')} mag`);
    if (!p.visible && res.visible_only === false) bits.push('not visible (daylight or shadow)');
    ul.append(h('li', null,
      h('span', { class: 'li-d' }, from > now ? `in ${span(from - now)}` : 'now'),
      h('span', { class: 'li-t' }, `${dayLabel(from)}, ${hm(from)} to ${hm(to)} UTC`),
      h('span', { class: 'li-s' }, bits.join(', '))));
  }
  return ul;
}

async function loadPasses(box, norad, lat, lon, { visible = true, hours = 72 } = {}) {
  const { h, dom } = S.ctx;
  dom.clear(box);
  box.append(h('p', { class: 'dim' }, `Working out passes over ${S.ctx.fmt.latlon(lat, lon, 2)}…`));
  try {
    const res = await S.ctx.api.get('/api/space/passes', { lat: lat.toFixed(4), lon: lon.toFixed(4), norad, hours, visible: visible ? 1 : 0 });
    dom.clear(box);
    box.append(h('p', { class: 'faint space-note' }, `Over ${S.ctx.fmt.latlon(lat, lon, 2)}, next ${hours} hours${visible ? ', visible only' : ''}.`));
    box.append(passList(res, { limit: 6 }));
    if (res.deep_space) box.append(h('p', { class: 'faint space-note' }, 'High orbit: passes are long and slow; times are approximate.'));
  } catch (err) {
    dom.clear(box);
    box.append(h('p', { class: 'dim' }, `Passes are unavailable right now (${err.message}).`));
  }
}

// ------------------------------------------------------------------ cards
function satBlock(p, info) {
  return [
    { label: 'Altitude', value: p ? fmtKm(p.alt) : '–', unit: 'km' },
    { label: 'Speed', value: p ? p.speed.toFixed(2) : '–', unit: 'km/s' },
    { label: 'Inclination', value: info.inc.toFixed(1), unit: '°' },
    { label: 'Period', value: info.period >= 600 ? (info.period / 60).toFixed(1) : info.period.toFixed(1), unit: info.period >= 600 ? 'h' : 'min' },
  ];
}

function sunlitNow(p) {
  if (!p || !S.sat) return null;
  try {
    const sun = S.sat.sunPos(S.sat.jday(new Date()));
    return S.sat.shadowFraction(sun.rsun, p.eci) < 0.5;
  } catch { return null; }
}

function badgesFor(id, p, info, epochMs) {
  const ageH = (Date.now() - epochMs) / 3600000;
  const lit = sunlitNow(p);
  const out = [{ text: info.cls, tone: 'blue', title: ORBIT_TEXT[info.cls] }];
  if (lit != null) out.push({ text: lit ? 'In sunlight' : "In Earth's shadow", tone: 'grey' });
  if (ageH > 72) out.push({ text: `Elements ${Math.round(ageH / 24)} days old`, tone: 'amber', title: 'Old elements: the position is less certain.' });
  if (!p) out.push({ text: 'No position (decayed or bad elements)', tone: 'red' });
  return out;
}

export async function openSatCard(id, { fly = false } = {}) {
  const ctx = S.ctx;
  const { h, fmt } = ctx;
  if (!S.sat) S.sat = await loadSat();
  if (!S.rows.has(id)) {
    try {
      const d = await ctx.api.get(`/api/space/satellite/${id}`);
      S.fields = S.fields || d.fields;
      const e = addRow(d.fields, d.row, null);
      for (const g of d.groups || []) e.groups.add(g);
      S.extras.add(id);
      if (S.on) rebuild();
    } catch (err) {
      return ctx.cards.open({ key: `satellite:${id}`, kind: 'satellite', title: `NORAD ${id}`, icon: '◉', accent: COLOR.space,
        subtitle: 'Satellite', sections: [{ text: err.status === 404 ? 'CelesTrak has no current orbit for this object. It may have re-entered, or its orbit is not public.' : `The orbit could not be loaded right now (${err.message}).` }] });
    }
  }
  const e = S.rows.get(id);
  const satrec = satrecFor(id);
  if (!satrec) return null;
  const info = orbitInfo(satrec);
  const epochMs = Date.parse(e.row[3].endsWith('Z') ? e.row[3] : `${e.row[3]}Z`);
  const groupTitles = [...e.groups].map(g => S.groups.find(x => x.id === g)?.title || g);
  const p = propagateOne(S.sat, satrec, new Date());
  if (fly && p) ctx.map2.flyTo(p.lat, p.lon, Math.min(ctx.map.getZoom(), 4));
  selectSat(id);
  const passesBox = h('div', { class: 'space-passes-box' });
  const centrePasses = () => { const c = ctx.map.getCenter(); loadPasses(passesBox, id, c.lat, c.lng, { visible: id === ISS }); };
  if (id === ISS) centrePasses();
  else passesBox.append(h('p', { class: 'dim space-note' }, 'Press "Passes over map centre" to see when it rises over the middle of the map.'));
  const cospar = e.row[2];
  const docked = () => {
    const list = dockedWith(id);
    if (!list.length) return null;
    return { title: 'Modules and docked vehicles', list: list.map(d => ({ title: nameOf(d), subtitle: `NORAD ${d}` })) };
  };
  const sectionsFor = (pp) => [
    { title: 'Now', rows: [
      ['Over', pp ? fmt.latlon(pp.lat, pp.lon, 2) : '–'],
      ['Speed', pp ? `${fmtKm(pp.speed * 3600)} km/h` : '–'],
      ['Footprint', pp ? `${fmtKm(footprintRadiusKm(pp.alt))} km radius` : '–'],
    ] },
    docked(),
    { title: 'Orbit', rows: [
      ['Class', ORBIT_TEXT[info.cls]],
      ['Perigee and apogee', `${fmtKm(info.perigee)} km and ${fmtKm(info.apogee)} km`],
      ['Period', `${info.period.toFixed(1)} min, ${(1440 / info.period).toFixed(2)} orbits a day`],
      ['Inclination', `${info.inc.toFixed(2)}°`],
      ['Eccentricity', info.e.toFixed(5)],
    ] },
    { title: 'Identity', rows: [
      ['NORAD number', String(id)],
      ['International designator', cospar || '–'],
      ['Launched', launchYear(cospar)],
      ['CelesTrak groups', groupTitles.join(', ') || 'Looked up by number'],
      ['Elements', `epoch ${fmt.utc(epochMs)}, ${fmt.ago(epochMs)}`],
    ] },
    { title: id === ISS ? 'Visible passes over the map centre' : 'Passes over the map centre', el: passesBox },
  ];
  const entityAt = (pp) => ({ kind: 'satellite', id: String(id), title: e.row[1], subtitle: `NORAD ${id}, ${info.cls}`,
    lat: pp ? +pp.lat.toFixed(5) : null, lon: pp ? +pp.lon.toFixed(5) : null, layer: id === ISS ? 'space.iss' : 'space.satellites', zoom: 3 });
  const followText = () => (S.follow === id ? 'Stop following' : 'Follow');
  const card = ctx.cards.open({
    key: `satellite:${id}`, kind: 'satellite', title: e.row[1], subtitle: `NORAD ${id}${cospar ? `, ${cospar}` : ''}`,
    icon: '◉', accent: COLOR.space,
    badges: badgesFor(id, p, info, epochMs),
    block: satBlock(p, info),
    sections: sectionsFor(p),
    links: [
      { text: 'CelesTrak catalogue entry', href: `https://celestrak.org/satcat/table-satcat.php?CATNR=${id}` },
      { text: 'Heavens-Above', href: `https://www.heavens-above.com/satinfo.aspx?satid=${id}` },
    ],
    actions: [
      { text: followText(), onClick: (c) => { S.follow = S.follow === id ? null : id; c.update({ actions: actionsFor() }); if (S.follow) followNow(); } },
      { text: 'Passes over map centre', onClick: centrePasses },
    ],
    entity: p ? entityAt(p) : null,
    live: { everyMs: 2000, update: () => {
      const pp = id === ISS && S.iss.on ? (S.iss.last || propagateOne(S.sat, satrec, new Date())) : (updateSelection() || propagateOne(S.sat, satrec, new Date()));
      if (S.follow === id && pp) followNow(pp);
      return { block: satBlock(pp, info), badges: badgesFor(id, pp, info, epochMs), sections: sectionsFor(pp), entity: pp ? entityAt(pp) : null };
    } },
    foot: 'Position from SGP4 on CelesTrak elements, computed in your browser. Accurate to about a kilometre for recent elements.',
    onClose: () => deselect(id),
  });
  function actionsFor() {
    return [
      { text: followText(), onClick: (c) => { S.follow = S.follow === id ? null : id; c.update({ actions: actionsFor() }); if (S.follow) followNow(); } },
      { text: 'Passes over map centre', onClick: centrePasses },
    ];
  }
  // Following re-centres every 6 s (not every update) so view-dependent layers elsewhere refresh gently.
  let lastFollow = 0;
  function followNow(pp) {
    const q = pp || propagateOne(S.sat, satrec, new Date());
    if (!q || S.follow !== id) return;
    const map = ctx.map;
    if (map.isZooming() || map.isRotating() || Date.now() - lastFollow < 5500) return;
    lastFollow = Date.now();
    map.easeTo({ center: [q.lon, q.lat], duration: ctx.map2.reducedMotion() ? 0 : 1200 });
  }
  return card;
}

// ------------------------------------------------------------------ ISS layer
async function loadIss(force = false) {
  if (!force && S.iss.satrec && Date.now() - S.iss.loadedAt < 30 * 60000) return;
  const d = await S.ctx.api.get(`/api/space/satellite/${ISS}`);
  S.fields = S.fields || d.fields;
  const e = addRow(d.fields, d.row, null);
  for (const g of d.groups || []) e.groups.add(g);
  S.iss.row = d.row;
  S.iss.satrec = S.sat.json2satrec(rowToOmm(d.fields, d.row));
  S.iss.loadedAt = Date.now();
  S.iss.trackAt = 0;
}

function issTick() {
  if (!S.iss.on) return;
  clearTimeout(S.iss.timer);
  S.iss.timer = setTimeout(issTick, 1000);
  if (document.visibilityState !== 'visible' || !S.iss.satrec) return;
  const now = Date.now();
  const p = propagateOne(S.sat, S.iss.satrec, new Date(now));
  if (!p) { S.iss.layer.setStatus('error', 'The ISS position could not be computed from the current elements.'); return; }
  S.iss.last = p;
  const { map2 } = S.ctx;
  map2.setData(SRC_ISS, { type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
    properties: { id: ISS, n: 'ISS', a: Math.round(p.alt) } }] });
  if (S.iss.footAt == null || now - S.iss.footAt > 2000) { map2.setData(SRC_ISS_FOOT, footFC(p.lat, p.lon, p.alt)); S.iss.footAt = now; }
  if (now - S.iss.trackAt > 30000) {
    map2.setData(SRC_ISS_TRACK, trackFC(groundTrack(S.sat, S.iss.satrec, now, orbitInfo(S.iss.satrec).period, 180)));
    S.iss.trackAt = now;
  }
  if (now - S.iss.loadedAt > 30 * 60000) loadIss(true).catch(() => { /* keep the old elements */ });
}

// ------------------------------------------------------------------ register
export async function registerSatellites(ctx) {
  S.ctx = ctx;
  const { h } = ctx;
  try {
    const res = await ctx.api.get('/api/space/groups', null, { timeoutMs: 5000 });
    if (Array.isArray(res.groups) && res.groups.length) S.groups = res.groups.map(g => ({ id: g.id, title: g.title, family: g.family, default: !!g.default }));
  } catch { /* backend not up yet: the built-in list matches the server's */ }

  ctx.map2.icon('space-sat', (g, s) => {
    g.fillStyle = '#fff';
    g.fillRect(s * 0.38, s * 0.36, s * 0.24, s * 0.28);
    g.fillRect(s * 0.06, s * 0.39, s * 0.26, s * 0.22);
    g.fillRect(s * 0.68, s * 0.39, s * 0.26, s * 0.22);
    g.fillRect(s * 0.32, s * 0.47, s * 0.06, s * 0.06);
    g.fillRect(s * 0.62, s * 0.47, s * 0.06, s * 0.06);
  }, { size: 32, sdf: true });
  ctx.map2.icon('space-iss', (g, s) => {
    g.fillStyle = '#fff';
    g.fillRect(s * 0.06, s * 0.46, s * 0.88, s * 0.08);
    for (const x of [0.08, 0.25, 0.62, 0.79]) { g.fillRect(s * x, s * 0.2, s * 0.13, s * 0.24); g.fillRect(s * x, s * 0.56, s * 0.13, s * 0.24); }
    g.fillRect(s * 0.42, s * 0.34, s * 0.16, s * 0.32);
  }, { size: 40, sdf: true });

  // -------------------------------------------------------------- satellites layer
  const groupOptions = S.groups.map(g => ({ id: `g_${g.id}`, label: g.title, type: 'toggle', default: g.default, refresh: false }));
  S.layer = ctx.layers.register({
    id: 'space.satellites', group: 'Space', title: 'Satellites', subtitle: 'CelesTrak orbital elements, positions computed in your browser',
    description: 'Active satellites by group, moving live. Click one for its orbit, ground track and the area that can see it.',
    source: 'CelesTrak GP data, SGP4 via satellite.js', color: COLOR.space, symbol: 'icon', defaultOn: false,
    options: [...groupOptions, { id: 'labels', label: 'Names from zoom 5', type: 'toggle', default: true, refresh: false }],
    legend: FAMILY_IDS.map(f => ({ color: FAMILY[f].color, label: FAMILY[f].label })),
    async enable({ options }) {
      S.sat = S.sat || await loadSat();
      startWorker();
      addSatLayers();
      S.on = true;
      await loadGroups(options);
      clearInterval(S.refreshTimer);
      S.refreshTimer = setInterval(() => {
        if (document.visibilityState === 'visible') loadGroups(S.layer.options(), { force: true }).catch(err => S.layer.setStatus('warn', err.message));
      }, 30 * 60000);
    },
    async disable() {
      S.on = false;
      clearTimeout(S.timer);
      clearInterval(S.refreshTimer);
      ctx.map2.removeSource(SRC);
      ctx.map2.removeSource(SRC_SWARM);
      S.positions = null;
      if (S.selected != null && S.selected !== ISS) {
        ctx.cards.close(`satellite:${S.selected}`);
      }
    },
    onOption(id, value, all) {
      if (!S.on) return;
      if (id === 'labels') { rebuild(all); return; }
      if (id === 'g_starlink' && value) ctx.ui.toast('Loading about 10,000 Starlink satellites. They redraw every 5 s.', '', 5000);
      S.layer.setStatus('loading');
      loadGroups(all).catch(err => S.layer.setStatus('error', err.message));
    },
  });
  ctx.map2.onClick([L_ICON, L_SWARM, L_LABEL], (f) => openSatCard(f.properties.id));
  ctx.map2.onHover([L_ICON, L_SWARM], (f) => {
    const q = f.properties;
    if (q.n) return `${q.n}, ${Number(q.a).toLocaleString('en-GB')} km up`;
    const i = S.index.get(q.id);
    const alt = i !== undefined && S.positions ? Math.round(S.positions[i * 3 + 2]) : NaN;
    return `${nameOf(q.id)}${Number.isNaN(alt) ? '' : `, ${alt.toLocaleString('en-GB')} km up`}`;
  });

  // -------------------------------------------------------------- ISS layer
  S.iss.layer = ctx.layers.register({
    id: 'space.iss', group: 'Space', title: 'International Space Station', subtitle: 'Live position, ground track and footprint',
    description: 'Where the ISS is now, its next orbit (solid line), the last one (dashed) and the area that can see it. Click it for passes over the map centre.',
    source: 'CelesTrak elements, SGP4', color: COLOR.paper, symbol: 'icon', defaultOn: false,
    legend: [{ color: COLOR.space, label: 'Next orbit', shape: 'line' }, { color: '#6f64b0', label: 'Last orbit', shape: 'line' },
      { color: COLOR.space, label: 'Area that can see it', shape: 'area' }],
    async enable() {
      S.sat = S.sat || await loadSat();
      await loadIss();
      addTrackLayers(SRC_ISS_TRACK, SRC_ISS_FOOT, 'ov-space-iss', COLOR.space);
      ctx.map2.addSource(SRC_ISS, { type: 'geojson', data: ctx.map2.emptyFC() });
      ctx.map2.addLayer({ id: 'ov-space-iss', type: 'symbol', source: SRC_ISS,
        layout: { 'icon-image': 'space-iss', 'icon-size': ['interpolate', ['linear'], ['zoom'], 0, 0.7, 6, 1], 'icon-allow-overlap': true,
          'icon-ignore-placement': true, 'text-field': 'ISS', 'text-font': ['Open Sans Bold'], 'text-size': 12, 'text-offset': [0, 1.5],
          'text-anchor': 'top', 'text-allow-overlap': true },
        paint: { 'icon-color': COLOR.paper, 'icon-halo-color': COLOR.ink, 'icon-halo-width': 1.4,
          'text-color': COLOR.paper, 'text-halo-color': COLOR.ink, 'text-halo-width': 1.5 } }, 'top');
      S.iss.on = true;
      S.iss.trackAt = 0;
      S.iss.footAt = null;
      issTick();
      S.iss.layer.setCount(1);
      if (S.selected === ISS) clearSelectionShapes();
    },
    async disable() {
      S.iss.on = false;
      clearTimeout(S.iss.timer);
      ctx.map2.removeSource(SRC_ISS);
      ctx.map2.removeSource(SRC_ISS_TRACK);
      ctx.map2.removeSource(SRC_ISS_FOOT);
      if (S.selected === ISS) { S.selTrackAt = 0; updateSelection(); }
    },
  });
  ctx.map2.onClick(['ov-space-iss'], () => openSatCard(ISS));
  ctx.map2.onHover(['ov-space-iss'], (f) => `International Space Station, ${Number(f.properties.a).toLocaleString('en-GB')} km up`);

  // -------------------------------------------------------------- entities, search, what's here
  // A bare {kind, id} (favourites, case files, the ISS search shortcut) has no position yet: fly once it is computed.
  ctx.entities.handler('satellite', (entity) => openSatCard(Number(entity.id), { fly: !ctx.dom.validLatLon(entity.lat, entity.lon) }), { group: 'Space', icon: '◉' });

  ctx.search.provider((q) => {
    const qu = q.trim().toUpperCase();
    if (qu.length < 2) return [];
    const alias = { ISS: ISS, 'SPACE STATION': ISS, 'INTERNATIONAL SPACE STATION': ISS, TIANGONG: TIANGONG, HUBBLE: HUBBLE };
    if (!S.rows.size) {
      // Catalogue not loaded yet (satellite layers off): still answer "ISS" with the station itself,
      // so it is not outranked by a company or an airport code. The card loads the rest.
      if (alias[qu] !== ISS) return [];
      return [{ kind: 'satellite', id: String(ISS), title: 'International Space Station', subtitle: 'ISS (ZARYA), NORAD 25544',
        layer: 'space.iss', zoom: 3, score: 0.97 }];
    }
    const out = [];
    const digits = /^\d+$/.test(qu);
    for (const [id, e] of S.rows) {
      const name = e.row[1].toUpperCase();
      let score = 0;
      if (alias[qu] === id) score = 0.97;
      else if (digits && String(id) === qu) score = 0.95;
      else if (name === qu) score = 0.9;
      else if (name.startsWith(qu)) score = 0.74;
      else if (name.split(/[^A-Z0-9]+/).some(w => w && w.startsWith(qu))) score = 0.62;
      else if (qu.length >= 4 && name.includes(qu)) score = 0.5;
      if (!score) continue;
      let lat = null; let lon = null;
      const i = S.index.get(id);
      if (S.positions && i !== undefined && !Number.isNaN(S.positions[i * 3])) { lon = S.positions[i * 3]; lat = S.positions[i * 3 + 1]; }
      else if (S.sat) { const p = propagateOne(S.sat, satrecFor(id), new Date()); if (p) { lat = p.lat; lon = p.lon; } }
      out.push({ kind: 'satellite', id: String(id), title: e.row[1], subtitle: `Satellite, NORAD ${id}`, lat, lon, zoom: 3,
        layer: id === ISS ? 'space.iss' : 'space.satellites', score });
      if (out.length > 40) break;
    }
    return out.sort((a, b) => b.score - a.score).slice(0, 8);
  });

  ctx.whatshere.section(async (lat, lon) => {
    if (!ctx.moduleOk('space')) return null;
    let res;
    try { res = await ctx.api.get('/api/space/passes', { lat: lat.toFixed(4), lon: lon.toFixed(4), hours: 72, visible: 1 }); } catch { return null; }
    const first = whatsHereOrder.weather;
    if (first) await Promise.race([first, new Promise(r => setTimeout(r, 4000))]);
    const box = h('div', { class: 'space-wh-passes' }, passList(res, { limit: 4 }));
    const show = h('button', { type: 'button', class: 'text-btn space-inline-btn' }, 'Show the ISS on the map');
    show.addEventListener('click', () => ctx.layers.setOn('space.iss', true));
    box.append(show);
    return { title: 'Next ISS passes over here', el: box };
  });

  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && S.on) schedule(0); });
  ctx.map.on('dragstart', () => { S.follow = null; });
}
