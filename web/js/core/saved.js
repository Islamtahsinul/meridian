// Favourites and case files (CONTRACT.md section 10.11). Both live in the server's SQLite database.

import { bus } from './bus.js';
import { h, clear, fmt, download, debounce, validLatLon, kindLabel } from './dom.js';
import { api } from './api.js';
import { ui } from './ui.js';
import { entities } from './entities.js';
import * as mapx from './map.js';
import { layers } from './layers.js';

// ------------------------------------------------------------------ favourites
let favs = [];
const favKeys = new Set();

async function loadFavs() {
  try {
    favs = (await api.get('/api/core/favourites')).favourites || [];
  } catch { favs = []; }
  favKeys.clear();
  for (const f of favs) favKeys.add(`${f.kind}|${f.ref}`);
  bus.emit('favourites:changed', { favourites: favs });
}

export const favourites = {
  load: loadFavs,
  list(kind = null) { return kind ? favs.filter(f => f.kind === kind) : [...favs]; },
  isFav(kind, ref) { return favKeys.has(`${kind}|${ref}`); },
  async add(entity, payload = {}) {
    await api.post('/api/core/favourites', { action: 'add', kind: entity.kind, ref: String(entity.id), title: entity.title || '',
      subtitle: entity.subtitle || '', lat: validLatLon(entity.lat, entity.lon) ? +entity.lat : null,
      lon: validLatLon(entity.lat, entity.lon) ? +entity.lon : null, layer: entity.layer || '', payload });
    await loadFavs();
    ui.toast('Added to favourites', 'green');
  },
  async remove(kind, ref) {
    await api.post('/api/core/favourites', { action: 'remove', kind, ref: String(ref) });
    await loadFavs();
    ui.toast('Removed from favourites');
  },
  async toggle(entity, payload = {}) {
    if (favourites.isFav(entity.kind, String(entity.id))) await favourites.remove(entity.kind, entity.id);
    else await favourites.add(entity, payload);
  },
  onChange(fn) { return bus.on('favourites:changed', fn); },
};

export function openFavourites() {
  const body = h('div');
  const render = () => {
    clear(body);
    if (!favs.length) {
      body.append(h('p', { class: 'empty' }, 'No favourites yet. Open any camera, ship, aircraft or place and press the star on its card.'));
      return;
    }
    const byKind = new Map();
    for (const f of favs) { if (!byKind.has(f.kind)) byKind.set(f.kind, []); byKind.get(f.kind).push(f); }
    for (const [kind, items] of byKind) {
      body.append(h('h3', { style: { margin: '14px 0 4px', fontSize: '14px', color: 'var(--paper-dim)' } }, kindLabel(kind, true)));
      const tbl = h('table', { class: 'tbl' });
      for (const f of items) {
        const openBtn = h('button', { class: 'text-btn', type: 'button' }, f.title || f.ref);
        openBtn.addEventListener('click', () => { m.close(); entities.open({ kind: f.kind, id: f.ref, title: f.title, subtitle: f.subtitle, lat: f.lat, lon: f.lon, layer: f.layer, payload: f.payload }); });
        const rm = h('button', { class: 'text-btn', type: 'button', title: 'Remove' }, 'Remove');
        rm.addEventListener('click', async () => { await favourites.remove(f.kind, f.ref); render(); });
        tbl.append(h('tr', null, h('td', null, openBtn), h('td', { class: 'dim' }, f.subtitle || ''), h('td', { class: 'num' }, rm)));
      }
      body.append(tbl);
    }
  };
  render();
  const m = ui.modal({ title: 'Favourites', body, wide: false });
}

// ------------------------------------------------------------------ case files
async function listCasefiles() {
  return (await api.get('/api/core/casefiles')).casefiles || [];
}

export const casefiles = {
  list: listCasefiles,
  async create(title) {
    const r = await api.post('/api/core/casefiles', { action: 'create', title });
    bus.emit('casefiles:changed', {});
    return r.id;
  },
  async addItem(cid, entity, payload = {}) {
    await api.post('/api/core/casefiles', { action: 'add_item', id: cid, item: {
      kind: entity.kind, ref: String(entity.id), title: entity.title || '', subtitle: entity.subtitle || '',
      lat: validLatLon(entity.lat, entity.lon) ? +entity.lat : null, lon: validLatLon(entity.lat, entity.lon) ? +entity.lon : null,
      layer: entity.layer || '', payload } });
    bus.emit('casefiles:changed', {});
  },
  /** Menu anchored at a button: pick a case file (or make one) and add the entity to it. */
  async addItemMenu(anchor, entity, payload = {}) {
    let files = [];
    try { files = await listCasefiles(); } catch { ui.toast('Case files are unavailable right now.', 'red'); return; }
    const items = files.slice(0, 12).map(f => ({ text: f.title, onClick: async () => {
      await casefiles.addItem(f.id, entity, payload); ui.toast(`Added to “${f.title}”`, 'green');
    } }));
    if (items.length) items.push({ separator: true });
    items.push({ text: 'New case file…', onClick: async () => {
      const title = await ui.prompt('New case file', { label: 'Name', value: entity.title || '' , ok: 'Create' });
      if (!title) return;
      const id = await casefiles.create(title);
      await casefiles.addItem(id, entity, payload);
      ui.toast(`Added to “${title}”`, 'green');
    } });
    ui.openMenu(anchor, items, { align: 'left' });
  },
};

export async function openCasefiles() {
  const body = h('div');
  const m = ui.modal({ title: 'Case files', body, wide: true });
  const renderList = async () => {
    clear(body);
    const newBtn = h('button', { class: 'btn primary', type: 'button' }, 'New case file');
    newBtn.addEventListener('click', async () => {
      const title = await ui.prompt('New case file', { label: 'Name', ok: 'Create' });
      if (title) { const id = await casefiles.create(title); renderDetail(id); }
    });
    body.append(h('p', { class: 'dim' }, 'Case files collect the things you are following, with your own notes. They stay on this computer.'), newBtn);
    let files = [];
    try { files = await listCasefiles(); } catch { body.append(h('p', { class: 'empty' }, 'Case files are unavailable right now.')); return; }
    if (!files.length) { body.append(h('p', { class: 'empty' }, 'No case files yet.')); return; }
    const tbl = h('table', { class: 'tbl', style: { marginTop: '12px' } }, h('tr', null, h('th', null, 'Name'), h('th', { class: 'num' }, 'Items'), h('th', null, 'Updated')));
    for (const f of files) {
      const b = h('button', { class: 'text-btn', type: 'button' }, f.title);
      b.addEventListener('click', () => renderDetail(f.id));
      tbl.append(h('tr', null, h('td', null, b), h('td', { class: 'num' }, fmt.int(f.items)), h('td', { class: 'dim' }, fmt.ago(f.updated_at))));
    }
    body.append(tbl);
  };
  const renderDetail = async (cid) => {
    let cf;
    try { cf = await api.get(`/api/core/casefiles/${cid}`); } catch { ui.toast('That case file could not be opened.', 'red'); return renderList(); }
    clear(body);
    const back = h('button', { class: 'text-btn', type: 'button' }, 'All case files');
    back.addEventListener('click', renderList);
    const title = h('input', { type: 'text', value: cf.title, 'aria-label': 'Case file name' });
    title.addEventListener('change', async () => { cf.title = title.value; await api.post('/api/core/casefiles', { action: 'rename', id: cid, title: title.value }); });
    const notes = h('textarea', { 'aria-label': 'Notes', placeholder: 'Notes, links, what to watch for…' });
    notes.value = cf.notes || '';
    const saved = h('span', { class: 'faint', style: { fontSize: '12px' } }, '');
    const saveNotes = debounce(async () => {
      await api.post('/api/core/casefiles', { action: 'notes', id: cid, notes: notes.value });
      saved.textContent = `Saved ${fmt.clock(Date.now())} UTC`;
    }, 700);
    notes.addEventListener('input', () => { cf.notes = notes.value; saved.textContent = 'Saving…'; saveNotes(); });
    const show = h('button', { class: 'btn', type: 'button' }, 'Show on map');
    show.addEventListener('click', () => { showOnMap(cf); m.close(); });
    const exportJson = h('button', { class: 'btn', type: 'button' }, 'Export JSON');
    exportJson.addEventListener('click', () => download(`${slug(cf.title)}.json`, JSON.stringify(cf, null, 2)));
    const exportMd = h('button', { class: 'btn', type: 'button' }, 'Export Markdown');
    exportMd.addEventListener('click', () => download(`${slug(cf.title)}.md`, toMarkdown(cf), 'text/markdown'));
    const del = h('button', { class: 'btn danger', type: 'button' }, 'Delete');
    del.addEventListener('click', async () => {
      if (await ui.confirm(`Delete “${cf.title}” and its ${cf.items.length} items?`, { ok: 'Delete', danger: true })) {
        await api.post('/api/core/casefiles', { action: 'delete', id: cid });
        if (shownCaseId === cid) clearShownCase();
        renderList();
      }
    });
    body.append(back, h('div', { class: 'field' }, h('label', null, 'Name'), title),
      h('div', { class: 'field' }, h('label', null, 'Notes'), notes, saved),
      h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', margin: '8px 0 14px' } }, show, exportJson, exportMd, del));
    if (!cf.items.length) {
      body.append(h('p', { class: 'empty' }, 'Nothing in this case file yet. Use “Add to case file” on any card.'));
      return;
    }
    const tbl = h('table', { class: 'tbl' }, h('tr', null, h('th', null, 'Item'), h('th', null, 'Kind'), h('th', null, 'Added'), h('th', null, 'Note'), h('th')));
    for (const it of cf.items) {
      const openBtn = h('button', { class: 'text-btn', type: 'button' }, it.title || it.ref);
      openBtn.addEventListener('click', () => { m.close(); entities.open({ kind: it.kind, id: it.ref, title: it.title, subtitle: it.subtitle, lat: it.lat, lon: it.lon, layer: it.layer, payload: it.payload }); });
      const note = h('input', { type: 'text', value: it.note || '', 'aria-label': 'Item note', style: { width: '100%', background: 'var(--ink-0)', border: '1px solid var(--ink-3)', borderRadius: '4px', padding: '3px 6px' } });
      note.addEventListener('change', () => api.post('/api/core/casefiles', { action: 'item_note', id: cid, item_id: it.id, note: note.value }));
      const rm = h('button', { class: 'text-btn', type: 'button' }, 'Remove');
      rm.addEventListener('click', async () => { await api.post('/api/core/casefiles', { action: 'remove_item', id: cid, item_id: it.id }); renderDetail(cid); });
      tbl.append(h('tr', null, h('td', null, openBtn, it.subtitle ? h('div', { class: 'dim', style: { fontSize: '12px' } }, it.subtitle) : null),
        h('td', { class: 'dim' }, kindLabel(it.kind)), h('td', { class: 'dim' }, fmt.utc(it.added_at)), h('td', { style: { minWidth: '200px' } }, note), h('td', null, rm)));
    }
    body.append(tbl);
  };
  renderList();
}

function slug(s) { return String(s || 'case-file').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'case-file'; }

function toMarkdown(cf) {
  const lines = [`# ${cf.title}`, '', `Exported ${fmt.utc(Date.now())}`, ''];
  if (cf.notes) lines.push(cf.notes, '');
  lines.push('## Items', '');
  for (const it of cf.items) {
    const pos = validLatLon(it.lat, it.lon) ? ` (${fmt.latlon(it.lat, it.lon)})` : '';
    lines.push(`- **${it.title || it.ref}**, ${kindLabel(it.kind).toLowerCase()}${pos}, added ${fmt.utc(it.added_at)}${it.note ? `\n  - ${it.note}` : ''}`);
  }
  return lines.join('\n');
}

let shownCaseId = null;

function clearShownCase() {
  shownCaseId = null;
  mapx.setData('ov-saved-casefile', mapx.emptyFC());
  layers.setCount('saved.casefile', null);
}

function showOnMap(cf) {
  shownCaseId = cf.id;
  const feats = cf.items.filter(i => validLatLon(i.lat, i.lon)).map(i => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [i.lon, i.lat] },
    properties: { title: i.title || i.ref, kind: i.kind, ref: i.ref, layer: i.layer || '' } }));
  const entry = layers.get('saved.casefile');
  mapx.addSource('ov-saved-casefile', { type: 'geojson', data: { type: 'FeatureCollection', features: feats } });
  if (entry && !entry.on) layers.setOn('saved.casefile', true);
  if (feats.length) {
    const xs = feats.map(f => f.geometry.coordinates[0]);
    const ys = feats.map(f => f.geometry.coordinates[1]);
    if (feats.length === 1) mapx.flyTo(ys[0], xs[0], 9);
    else mapx.fitBbox([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]);
  }
  layers.setCount('saved.casefile', feats.length);
  ui.toast(`Showing ${feats.length} items from “${cf.title}”`);
}

/** Core-owned layers in the Saved group: favourites and the case file being shown. */
export function registerSavedLayers() {
  const favData = () => ({ type: 'FeatureCollection', features: favs.filter(f => validLatLon(f.lat, f.lon)).map(f => ({ type: 'Feature',
    geometry: { type: 'Point', coordinates: [f.lon, f.lat] }, properties: { title: f.title, kind: f.kind, ref: f.ref, layer: f.layer || '' } })) });
  layers.register({
    id: 'saved.favourites', group: 'Saved', title: 'Favourites', symbol: 'point', color: 'var(--amber)',
    description: 'Everything you starred, wherever it is.',
    enable() {
      mapx.addSource('ov-saved-favs', { type: 'geojson', data: favData() });
      mapx.addLayer({ id: 'ov-saved-favs', type: 'symbol', source: 'ov-saved-favs', layout: { 'text-field': '★', 'text-size': 18, 'text-allow-overlap': true, 'text-font': ['Open Sans Bold'] },
        paint: { 'text-color': '#ffb547', 'text-halo-color': '#111a2c', 'text-halo-width': 1.5 } }, 'top');
      layers.setCount('saved.favourites', favs.length);
    },
    disable() { mapx.removeSource('ov-saved-favs'); },
  });
  favourites.onChange(() => {
    if (layers.isOn('saved.favourites')) { mapx.setData('ov-saved-favs', favData()); layers.setCount('saved.favourites', favs.length); }
  });
  mapx.onClick(['ov-saved-favs', 'ov-saved-casefile'], (f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    entities.open({ kind: p.kind, id: p.ref, title: p.title, lat, lon, layer: p.layer || null }, { fly: false });
  });
  mapx.onHover(['ov-saved-favs', 'ov-saved-casefile'], f => f.properties.title);
  layers.register({
    id: 'saved.casefile', group: 'Saved', title: 'Case file on map', symbol: 'point', color: 'var(--paper)',
    description: 'Items from the case file you chose with “Show on map”.',
    enable() {
      mapx.addSource('ov-saved-casefile', { type: 'geojson', data: mapx.emptyFC() });
      mapx.addLayer({ id: 'ov-saved-casefile', type: 'circle', source: 'ov-saved-casefile',
        paint: { 'circle-radius': 7, 'circle-color': '#ebe6d6', 'circle-stroke-color': '#111a2c', 'circle-stroke-width': 2 } }, 'top');
      mapx.addLayer({ id: 'ov-saved-casefile-label', type: 'symbol', source: 'ov-saved-casefile',
        layout: { 'text-field': ['get', 'title'], 'text-size': 12, 'text-offset': [0, 1.2], 'text-anchor': 'top', 'text-font': ['Open Sans Regular'] },
        paint: { 'text-color': '#ebe6d6', 'text-halo-color': '#111a2c', 'text-halo-width': 1.5 } }, 'top');
    },
    disable() { mapx.removeSource('ov-saved-casefile'); },
  });
}
