// Space module: the satellite scene finder. The latest Sentinel-2, Sentinel-1 and Landsat scenes at the
// map view or a clicked point (/api/space/scenes, keyless STAC catalogues), with quicklooks, footprints
// on the map and links to the official viewers. It searches only when asked; the server caches an hour.

import { COLOR, dateLabel, hm, whatsHereOrder } from './common.js';

// three categorical hues validated all-pairs for colour-vision deficiency on the dark map (common.js FAMILY)
const MISSIONS = [
  { id: 'sentinel-2', label: 'Sentinel-2', color: '#199e70', note: 'optical, 10 m' },
  { id: 'sentinel-1', label: 'Sentinel-1', color: '#d95926', note: 'radar, sees through cloud' },
  { id: 'landsat', label: 'Landsat', color: '#9085e9', note: 'optical, 30 m' },
];
const MISSION = Object.fromEntries(MISSIONS.map(m => [m.id, m]));
const CLOUD = [{ value: '100', label: 'Any cloud' }, { value: '60', label: 'Up to 60% cloud' }, { value: '30', label: 'Up to 30% cloud' },
  { value: '10', label: 'Up to 10% cloud' }];
const DAYS = [{ value: '14', label: 'Last 2 weeks' }, { value: '30', label: 'Last 30 days' }, { value: '60', label: 'Last 60 days' },
  { value: '120', label: 'Last 120 days' }];
const SRC = 'ov-space-scenes';
const L_FILL = 'ov-space-scenes-fill';
const L_LINE = 'ov-space-scenes-line';
const ICON = 'M3.5 6.2 10 3l6.5 3.2v7.6L10 17l-6.5-3.2zM6.5 8.3l7 3.4M13.5 8.3l-7 3.4';
const MAX_BOX_DEG = 3;           // the server searches a larger view at its centre (scenes.py MAX_BOX_DEG)

const SC = { ctx: null, layer: null, drawer: null, btn: null, ui: null, res: null, where: null, sel: null, hover: null, seq: 0, abort: null };

function settings() {
  const s = SC.ctx.settings;
  const missions = s.get('space.scenes.missions', MISSIONS.map(m => m.id));
  return {
    missions: Array.isArray(missions) && missions.some(m => MISSION[m]) ? missions.filter(m => MISSION[m]) : MISSIONS.map(m => m.id),
    cloud: CLOUD.some(c => c.value === String(s.get('space.scenes.cloud', '60'))) ? String(s.get('space.scenes.cloud', '60')) : '60',
    days: DAYS.some(d => d.value === String(s.get('space.scenes.days', '30'))) ? String(s.get('space.scenes.days', '30')) : '30',
  };
}

/** The map view as a search area: a small view is searched whole, a large one at its centre. */
function fromView() {
  const { map, map2 } = SC.ctx;
  const c = map.getCenter();
  const [w, s, e, n] = map2.viewBounds();
  if (w <= e && e - w <= MAX_BOX_DEG && n - s <= MAX_BOX_DEG) return { bbox: [w, s, e, n], label: 'In this view' };
  return { lat: c.lat, lon: ((c.lng + 540) % 360) - 180, label: 'At the map centre' };
}

function whereText(where) {
  const { fmt } = SC.ctx;
  if (!where) return '';
  if (where.bbox) {
    const [w, s, e, n] = where.bbox;
    const km = (a, b) => Math.round(SC.ctx.dom.haversineKm(a[0], a[1], b[0], b[1]));
    const mid = (s + n) / 2;
    return `${where.label}, about ${km([mid, w], [mid, e])} by ${km([s, w], [n, w])} km`;
  }
  return `${where.label}, ${fmt.latlon(where.lat, where.lon, 3)}`;
}

function sceneWhen(s) { return `${dateLabel(s.datetime.slice(0, 10), false)}, ${hm(s.time)} UTC`; }

function sceneLine(s) {
  const bits = [];
  if (s.kind === 'radar') bits.push('Radar');
  else if (s.cloud != null) bits.push(`Cloud ${Math.round(s.cloud)}%`);
  // "Tile 30UXC" reads "tile 30UXC" after "Cloud 34%"; acronyms such as IW stay as they are
  if (s.detail) bits.push(bits.length && /^[A-Z][a-z]/.test(s.detail) ? s.detail.charAt(0).toLowerCase() + s.detail.slice(1) : s.detail);
  if (s.covers_point === false) bits.push('misses the point');
  return bits.join(', ');
}

function creditFor(s, credits) {
  const c = (credits || []).find(x => x.mission === s.mission);
  return c ? `${c.text}. Quicklook from ${c.catalogue}.` : '';
}

// ------------------------------------------------------------------ footprints
function footprintData() {
  const scenes = SC.res?.scenes || [];
  const feats = [...scenes].reverse().map(s => ({   // oldest first, so the newest outline draws on top
    type: 'Feature', geometry: s.geometry,
    properties: { key: s.key, color: MISSION[s.mission]?.color || COLOR.imagery, title: `${s.platform}, ${sceneWhen(s)}${s.cloud != null ? `, cloud ${Math.round(s.cloud)}%` : ''}`,
      sel: s.key === SC.sel, hover: s.key === SC.hover },
  }));
  return { type: 'FeatureCollection', features: feats };
}

function drawFootprints() {
  const { map, map2 } = SC.ctx;
  if (!SC.layer?.isOn()) return;
  if (!map.getSource(SRC)) {
    map2.addSource(SRC, { type: 'geojson', data: footprintData() });
    map2.addLayer({ id: L_FILL, type: 'fill', source: SRC,
      paint: { 'fill-color': ['get', 'color'], 'fill-opacity': ['case', ['get', 'sel'], 0.18, ['get', 'hover'], 0.1, 0] } }, 'polygons');
    map2.addLayer({ id: L_LINE, type: 'line', source: SRC, layout: { 'line-join': 'round' },
      paint: { 'line-color': ['get', 'color'], 'line-width': ['case', ['get', 'sel'], 2.6, ['get', 'hover'], 2.2, 1.2],
        'line-opacity': ['case', ['any', ['get', 'sel'], ['get', 'hover']], 1, 0.75] } }, 'lines');
  } else {
    map2.setData(SRC, footprintData());
  }
}

function clearFootprints() { SC.ctx.map2.removeSource(SRC); }

function select(key, { fit = true } = {}) {
  SC.sel = key;
  drawFootprints();
  const s = (SC.res?.scenes || []).find(x => x.key === key);
  for (const row of SC.ui?.list?.children || []) row.classList.toggle('sel', row.dataset.key === key);
  if (s && fit) SC.ctx.map2.fitBbox(s.bbox, { maxZoom: 9, padding: 60 });
}

// ------------------------------------------------------------------ quicklook view
function openQuicklook(s, credits) {
  const { h, ui } = SC.ctx;
  const img = h('img', { src: s.quicklook, alt: `Quicklook of the ${s.platform} scene of ${sceneWhen(s)}`, decoding: 'async', referrerpolicy: 'no-referrer' });
  const note = s.kind === 'radar' ? ' Radar quicklooks are in the satellite’s viewing geometry, so they can look mirrored or turned.' : '';
  const body = h('figure', { class: 'space-sc-big' }, img,
    h('figcaption', { class: 'space-credit' }, `${sceneLine(s)}. ${creditFor(s, credits)}${note}`));
  img.addEventListener('error', () => img.replaceWith(h('p', { class: 'dim' }, 'The quicklook could not be loaded right now.')));
  const foot = h('div', { class: 'space-sc-links' }, (s.links || []).map(l => h('a', { href: l.href }, l.text)));
  ui.modal({ title: `${s.platform}, ${sceneWhen(s)}`, body, foot, wide: true });
}

// ------------------------------------------------------------------ rendering
function thumb(s, size, credits) {
  const { h } = SC.ctx;
  if (!s.quicklook) return h('span', { class: `space-sc-thumb none ${size}` }, 'No quicklook');
  const img = h('img', { src: s.quicklook, alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer' });
  const b = h('button', { type: 'button', class: `space-sc-thumb ${size}`, title: 'Enlarge the quicklook', 'aria-label': `Enlarge the quicklook of the ${s.platform} scene of ${sceneWhen(s)}` }, img);
  img.addEventListener('error', () => { b.classList.add('none'); b.replaceChildren('No quicklook'); });
  b.addEventListener('click', () => openQuicklook(s, credits));
  return b;
}

function sceneRow(s, { compact = false, credits = [] } = {}) {
  const { h, fmt } = SC.ctx;
  const m = MISSION[s.mission] || {};
  const viewer = (s.links || [])[0];
  const actions = compact ? null : h('div', { class: 'space-sc-a' },
    (() => { const b = h('button', { type: 'button', class: 'text-btn space-inline-btn' }, 'Show footprint'); b.addEventListener('click', () => select(s.key)); return b; })(),
    viewer ? h('a', { href: viewer.href }, viewer.text.replace(/^Open in /, '')) : null);
  const li = h('li', { class: `space-sc-row${s.key === SC.sel ? ' sel' : ''}${compact ? ' compact' : ''}`, dataset: { key: s.key } },
    thumb(s, compact ? 'small' : 'row', credits),
    h('div', { class: 'space-sc-text' },
      h('div', { class: 'space-sc-t' }, h('i', { class: 'space-sc-dot', style: { background: m.color || COLOR.imagery }, 'aria-hidden': 'true' }),
        h('strong', null, s.platform), h('span', { class: 'faint' }, sceneWhen(s))),
      h('div', { class: 'space-sc-s' }, sceneLine(s) || m.note || '', h('span', { class: 'faint' }, ` ${fmt.ago(s.time)}`)),
      actions));
  if (!compact) {
    li.addEventListener('pointerenter', () => { SC.hover = s.key; drawFootprints(); });
    li.addEventListener('pointerleave', () => { if (SC.hover === s.key) { SC.hover = null; drawFootprints(); } });
  }
  return li;
}

function statusText(res) {
  const n = res.scenes.length;
  const bad = Object.entries(res.missions).filter(([, r]) => !r.ok).map(([id, r]) => `${MISSION[id]?.label || id}: ${r.error}`);
  const head = n ? `${n} scene${n === 1 ? '' : 's'} in the last ${res.days} days, newest first.`
    : `No scenes in the last ${res.days} days${res.max_cloud != null && res.max_cloud < 100 ? ` with ${Math.round(res.max_cloud)}% cloud or less` : ''}. Try more cloud or a longer period.`;
  return [head, ...bad].join(' ');
}

function render() {
  const { h, dom } = SC.ctx;
  if (!SC.ui) return;
  const { list, status, where, credits } = SC.ui;
  where.textContent = whereText(SC.where);
  dom.clear(list);
  dom.clear(credits);
  const res = SC.res;
  if (!res) return;
  status.textContent = statusText(res);
  status.classList.toggle('warn', Object.values(res.missions).some(r => !r.ok));
  for (const s of res.scenes.slice(0, 12)) list.append(sceneRow(s, { credits: res.credits }));
  const cr = (res.credits || []).filter(c => res.missions[c.mission]);
  credits.append(...cr.flatMap((c, i) => [i ? ' ' : '', `${MISSION[c.mission]?.label}: ${c.text}, via `, h('a', { href: c.url }, c.catalogue), '.']));
}

async function search(where) {
  const ctx = SC.ctx;
  SC.where = where;
  const my = ++SC.seq;
  SC.abort?.abort();
  const ac = new AbortController();
  SC.abort = ac;
  const st = settings();
  if (SC.ui) {
    SC.ui.where.textContent = whereText(where);
    SC.ui.status.textContent = 'Searching the catalogues…';
    SC.ui.status.classList.remove('warn');
  }
  const params = { days: st.days, max_cloud: st.cloud, missions: st.missions.join(','), limit: 4 };
  if (where.bbox) params.bbox = where.bbox.map(v => v.toFixed(4)).join(',');
  else { params.lat = where.lat.toFixed(4); params.lon = where.lon.toFixed(4); }
  SC.layer?.setStatus('loading');
  try {
    const res = await ctx.api.get('/api/space/scenes', params, { signal: ac.signal, timeoutMs: 45000 });
    if (my !== SC.seq) return null;
    SC.res = res;
    SC.sel = null;
    SC.hover = null;
    render();
    drawFootprints();
    SC.layer?.setCount(res.scenes.length);
    const bad = Object.values(res.missions).filter(r => !r.ok).length;
    SC.layer?.setStatus(bad ? 'warn' : 'ok', bad ? statusText(res) : null);
    return res;
  } catch (err) {
    if (err?.name === 'AbortError' || my !== SC.seq) return null;
    SC.res = null;
    render();
    if (SC.ui) { SC.ui.status.textContent = `The scene catalogues are not answering right now (${err.message}).`; SC.ui.status.classList.add('warn'); }
    SC.layer?.setStatus('error', 'The scene catalogues are not answering right now.');
    return null;
  }
}

// ------------------------------------------------------------------ drawer
function openDrawer() {
  const ctx = SC.ctx;
  const { h, ui } = ctx;
  if (SC.drawer) return;
  const st = settings();
  const where = h('p', { class: 'space-sc-where-t' }, '');
  const here = h('button', { type: 'button', class: 'btn' }, 'Search here');
  here.addEventListener('click', () => search(fromView()));
  const toggles = MISSIONS.map((m) => {
    const b = h('button', { type: 'button', 'aria-pressed': String(st.missions.includes(m.id)), title: `${m.label}: ${m.note}` },
      h('i', { class: 'space-sc-dot', style: { background: m.color }, 'aria-hidden': 'true' }), m.label);
    b.addEventListener('click', () => {
      const cur = settings().missions;
      const next = cur.includes(m.id) ? cur.filter(x => x !== m.id) : [...cur, m.id];
      if (!next.length) return;                     // keep at least one
      ctx.settings.set('space.scenes.missions', MISSIONS.map(x => x.id).filter(x => next.includes(x)));
      b.setAttribute('aria-pressed', String(next.includes(m.id)));
      if (SC.where) search(SC.where);
    });
    return b;
  });
  const sel = (choices, value, label, key) => {
    const el = h('select', { 'aria-label': label }, choices.map(c => h('option', { value: c.value, selected: c.value === value || null }, c.label)));
    el.addEventListener('change', () => { ctx.settings.set(key, el.value); if (SC.where) search(SC.where); });
    return el;
  };
  const status = h('p', { class: 'space-sc-status', role: 'status' }, 'Press Search here to find scenes.');
  const list = h('ul', { class: 'space-sc-list' });
  const credits = h('p', { class: 'space-credit' });
  const body = h('div', { class: 'space-sc' },
    h('section', { class: 'space-sc-sec' },
      h('div', { class: 'space-sc-where' }, where, here),
      h('div', { class: 'space-sc-controls' },
        h('div', { class: 'seg space-sc-miss', role: 'group', 'aria-label': 'Satellites' }, toggles),
        sel(CLOUD, st.cloud, 'Cloud cover', 'space.scenes.cloud'), sel(DAYS, st.days, 'Period', 'space.scenes.days')),
      status),
    list,
    h('section', { class: 'space-sc-sec space-sc-foot' }, credits,
      h('p', { class: 'space-credit' }, 'Searches only when you ask; results are kept for an hour. Cloud cover is for the whole scene.')));
  SC.ui = { where, status, list, credits };
  SC.drawer = ui.drawer({ id: 'space-scenes', title: 'Latest satellite scenes', el: body, width: 400,
    onClose: () => { SC.drawer = null; SC.ui = null; syncButton(); if (SC.layer.isOn()) ctx.layers.setOn('space.scenes', false); } });
  render();
  syncButton();
}

function closeDrawer() {
  if (!SC.drawer) return;
  const d = SC.drawer;
  SC.drawer = null;
  SC.ui = null;
  d.close();
  syncButton();
}

function syncButton() { SC.btn?.setAttribute('aria-pressed', String(!!SC.drawer)); }

/** Open the finder at a point (What's here) and search there. */
async function openAt(lat, lon, res = null) {
  const ctx = SC.ctx;
  SC.pending = { lat, lon, label: 'At the point you chose', res };
  if (!ctx.layers.isOn('space.scenes')) await ctx.layers.setOn('space.scenes', true);
  else { const p = SC.pending; SC.pending = null; await usePending(p); }
}

async function usePending(p) {
  if (p.res) { SC.where = { lat: p.lat, lon: p.lon, label: p.label }; SC.res = p.res; SC.sel = null; render(); drawFootprints(); SC.layer.setCount(p.res.scenes.length); }
  else await search({ lat: p.lat, lon: p.lon, label: p.label });
}

// ------------------------------------------------------------------ register
export async function registerScenes(ctx) {
  SC.ctx = ctx;
  SC.layer = ctx.layers.register({
    id: 'space.scenes', group: 'Imagery', title: 'Satellite scenes', subtitle: 'Latest Sentinel-2, Sentinel-1 and Landsat passes',
    description: 'Opens the scene finder: the latest scenes at the map view or a point from What’s here, with cloud cover, quicklooks, footprints on the map and links to the official viewers. It only searches when you ask.',
    source: 'Element 84 Earth Search, Copernicus Data Space Ecosystem, Microsoft Planetary Computer',
    color: COLOR.imagery, symbol: 'line',
    legend: MISSIONS.map(m => ({ color: m.color, label: m.label, shape: 'line' })),
    async enable() {
      openDrawer();
      const p = SC.pending;
      SC.pending = null;
      if (p) await usePending(p);
      else await search(fromView());
    },
    async disable() {
      SC.abort?.abort();
      closeDrawer();
      clearFootprints();
      SC.res = null;
      SC.sel = null;
    },
  });
  SC.btn = ctx.ui.toolbarButton({ id: 'space-scenes', title: 'Latest satellite scenes', label: 'Scenes', icon: ICON, order: 42,
    onClick: () => ctx.layers.setOn('space.scenes', !ctx.layers.isOn('space.scenes')) });
  ctx.bus.on('layer:toggled', ({ id }) => { if (id === 'space.scenes') syncButton(); });

  ctx.map2.onClick([L_FILL, L_LINE], (f) => {
    const key = f.properties?.key;
    if (!key) return;
    select(key, { fit: false });
    SC.ui?.list?.querySelector(`[data-key="${CSS.escape(key)}"]`)?.scrollIntoView({ block: 'nearest' });
  });
  ctx.map2.onHover([L_LINE], f => f.properties?.title || null);

  ctx.whatshere.section(async (lat, lon) => {
    if (!ctx.moduleOk('space')) return null;
    const first = whatsHereOrder.weather;          // weather comes first in What's here (weather.js)
    if (first) await Promise.race([first, new Promise(r => setTimeout(r, 4000))]);
    const { h } = ctx;
    const btn = h('button', { type: 'button', class: 'btn' }, 'Find scenes');
    const out = h('div', { class: 'space-sc-wh-out' });
    const box = h('div', { class: 'space-sc-wh' },
      h('p', { class: 'space-note' }, 'Sentinel-2, Sentinel-1 and Landsat passes over this point, with quicklooks. Searches only when you ask.'), btn, out);
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      ctx.dom.clear(out);
      out.append(h('p', { class: 'dim' }, 'Searching the catalogues…'));
      const st = settings();
      let res = null;
      try {
        res = await ctx.api.get('/api/space/scenes', { lat: lat.toFixed(4), lon: lon.toFixed(4), days: st.days, max_cloud: st.cloud,
          missions: st.missions.join(','), limit: 4 }, { timeoutMs: 45000 });
      } catch (err) {
        ctx.dom.clear(out);
        out.append(h('p', { class: 'dim' }, `The scene catalogues are not answering right now (${err.message}).`));
        btn.disabled = false;
        return;
      }
      ctx.dom.clear(out);
      out.append(h('p', { class: 'space-sc-status' }, statusText(res)),
        h('ul', { class: 'space-sc-list compact' }, res.scenes.slice(0, 4).map(s => sceneRow(s, { compact: true, credits: res.credits }))));
      const more = h('button', { type: 'button', class: 'text-btn space-inline-btn' }, 'Open in the scene finder');
      more.addEventListener('click', () => openAt(lat, lon, res));
      out.append(more);
      btn.remove();
    });
    return { title: 'Latest satellite scenes here', el: box };
  });
}
