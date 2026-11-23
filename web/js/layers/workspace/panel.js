// The Workspaces drawer: three tabs (Workspaces, My places, Export) and a "Copy link" tool.
// Applying a workspace sets the view, the display settings and exactly its layers and options, with
// Undo. Saved workspaces live on the server (/api/workspace/workspaces); presets come with Meridian.

import { FORMATS, PLACE_RADII, PLACE_TYPES, describeState, parseExport, planApply, snapshot } from './logic.js';
import { exportLayer, quickCount } from './exporter.js';
import { typeLabel } from './places.js';

const TABS = [['workspaces', 'Workspaces'], ['places', 'My places'], ['export', 'Export']];
const TAB_KEY = 'workspace.tab';
const FORMAT_KEY = 'workspace.format';
const OPT_KEY = 'meridian.layers.opt.';   // where the core keeps each layer's options (layers.js)

export function createPanel(ctx, { backend, places }) {
  const { h, dom, ui, api, fmt } = ctx;
  const s = { workspaces: [], presets: [], max: 100, loaded: false, error: null };
  let drawer = null;
  let tab = ctx.settings.get(TAB_KEY, 'workspaces');
  if (!TABS.some(([id]) => id === tab)) tab = 'workspaces';
  let exportLayerId = null;
  let exportFormat = ctx.settings.get(FORMAT_KEY, 'geojson');
  if (!FORMATS[exportFormat]) exportFormat = 'geojson';
  let exportNote = null;
  const openListeners = new Set();

  const tabBar = h('div', { class: 'workspace-tabs', role: 'tablist', 'aria-label': 'Workspaces' });
  const panel = h('div', { class: 'workspace-panel', role: 'tabpanel' });
  const root = h('div', { class: 'workspace-drawer' }, tabBar, panel);
  const fileInput = h('input', { type: 'file', accept: '.json,application/json', hidden: true });
  fileInput.addEventListener('change', () => { const f = fileInput.files?.[0]; fileInput.value = ''; if (f) importFile(f); });
  root.append(fileInput);

  // ------------------------------------------------------------------ the page as a state, and back
  function registry() {
    return ctx.layers.list().map((l) => {
      const e = ctx.layers.get(l.id);
      return { id: l.id, on: l.on, options: { ...(e?.options || {}) }, specOptions: e?.spec?.options || [] };
    });
  }

  function current() {
    const c = ctx.map.getCenter();
    const v = ctx.map2.viewState();
    return snapshot({ view: { center: { lng: c.lng, lat: c.lat }, zoom: ctx.map.getZoom(), bearing: ctx.map.getBearing(), pitch: ctx.map.getPitch() },
      display: { basemap: v.basemap, projection: v.projection, terrain: v.terrain }, layers: registry() });
  }

  // Layers have no public "set option" yet (a core change is requested): do what the layer row does.
  function setOption(id, name, value, notify) {
    const entry = ctx.layers.get(id);
    if (!entry) return;
    entry.options[name] = value;
    try { localStorage.setItem(OPT_KEY + id, JSON.stringify(entry.options)); } catch { /* private mode */ }
    if (notify) { try { entry.spec.onOption?.(name, value, { ...entry.options }); } catch (err) { console.error(`workspace: option ${name} of ${id} failed`, err); } }
  }

  // A snapshot keeps the options of the layers that are on only. Undo also needs the options a workspace
  // changes on layers that were off: their values before, and what the core had stored for each layer.
  function optionsBefore(changes) {
    const out = new Map();   // layer id -> {values: {name: value}, stored: text or null}
    for (const o of changes) {
      const entry = ctx.layers.get(o.id);
      if (!entry) continue;
      if (!out.has(o.id)) {
        let stored = null;
        try { stored = localStorage.getItem(OPT_KEY + o.id); } catch { /* private mode */ }
        out.set(o.id, { values: {}, stored });
      }
      out.get(o.id).values[o.name] = entry.options[o.name];
    }
    return out;
  }

  function putBackOptions(saved) {
    for (const [id, { values, stored }] of saved) {
      const entry = ctx.layers.get(id);
      if (!entry) continue;
      Object.assign(entry.options, values);
      try { if (stored == null) localStorage.removeItem(OPT_KEY + id); else localStorage.setItem(OPT_KEY + id, stored); } catch { /* private mode */ }
    }
  }

  function undoApply(before, saved) {
    // Applying the old state turns the extra layers off and resets the options of the layers that were
    // on (with their onOption and refresh); then every changed option gets its old value and storage back.
    const done = apply(before, { name: 'The previous view', undo: false });
    putBackOptions(saved);
    return done;
  }

  async function apply(state, { name = 'Workspace', undo = true } = {}) {
    const before = undo ? current() : null;
    const plan = planApply(state, registry());
    const saved = undo ? optionsBefore(plan.options) : null;
    const view = ctx.map2.viewState();
    if (plan.display.basemap && plan.display.basemap !== view.basemap) ctx.map2.setBasemap(plan.display.basemap);
    if (plan.display.projection && plan.display.projection !== view.projection) ctx.map2.setProjection(plan.display.projection);
    if (plan.display.terrain !== undefined && plan.display.terrain !== view.terrain) ctx.map2.setTerrain(plan.display.terrain);
    if (plan.view) {
      const to = { center: plan.view.center, zoom: plan.view.zoom, bearing: plan.view.bearing, pitch: plan.view.pitch };
      if (ctx.map2.reducedMotion()) ctx.map.jumpTo(to); else ctx.map.flyTo({ ...to, speed: 1.6, curve: 1.4, essential: true });
    }
    const refresh = new Set();
    for (const o of plan.options) {
      setOption(o.id, o.name, o.value, o.layerOn);
      const declared = (ctx.layers.get(o.id)?.spec?.options || []).find(x => x.id === o.name);
      if (o.layerOn && declared?.refresh !== false) refresh.add(o.id);   // as the layer row does
    }
    // setOn flips a layer at once and then waits for its data: start them all, tell the user, then wait.
    const toggles = [...plan.turnOff.map(id => ctx.layers.setOn(id, false)), ...plan.turnOn.map(id => ctx.layers.setOn(id, true))];
    for (const id of refresh) if (ctx.layers.isOn(id)) ctx.layers.refresh(id, 'option');
    const missing = plan.unknown.length ? ` ${plan.unknown.length} of its layers ${plan.unknown.length === 1 ? 'isn\'t' : 'aren\'t'} available here.` : '';
    ui.toast(missing ? `Some layers were left out.${missing}` : describeState(state), missing ? 'amber' : 'green', 7000,
      { title: `${name} applied`, action: before ? { text: 'Undo', onClick: () => undoApply(before, saved) } : null });
    await Promise.allSettled(toggles);
    return plan;
  }

  // ------------------------------------------------------------------ server calls
  async function load() {
    if (!backend) { s.error = 'The workspace module is not running on the server, so only the presets are available.'; s.presets = []; render(); return; }
    try {
      const r = await api.get('/api/workspace/workspaces');
      s.workspaces = r.workspaces || [];
      s.presets = r.presets || [];
      s.max = r.max || 100;
      s.loaded = true;
      s.error = null;
    } catch (err) {
      s.error = `Workspaces could not be loaded: ${err?.message || err}`;
    }
    render();
  }

  async function saveCurrent() {
    const typed = await ui.prompt('Save this view as a workspace', { label: 'Name', value: '', ok: 'Save' });
    const name = (typed || '').trim().replace(/\s+/g, ' ');
    if (!name) return;
    const state = current();
    const existing = s.workspaces.find(w => w.name.toLowerCase() === name.toLowerCase());
    try {
      if (existing) {
        if (!(await ui.confirm(`There is already a workspace called "${existing.name}". Replace it with this view?`, { ok: 'Replace' }))) return;
        await api.post('/api/workspace/workspaces', { action: 'update', id: existing.id, state });
        ui.toast(`Updated "${existing.name}": ${describeState(state)}.`, 'green');
      } else {
        await api.post('/api/workspace/workspaces', { action: 'create', name, state });
        ui.toast(`Saved "${name}": ${describeState(state)}.`, 'green');
      }
    } catch (err) {
      ui.toast(err?.code === 'NAME_TAKEN' ? `${err.message} Try again: the list was out of date.` : (err?.message || String(err)), 'red');
    }
    await load();
  }

  async function updateWith(w) {
    if (!(await ui.confirm(`Replace "${w.name}" with the map as it is now?`, { ok: 'Update' }))) return;
    try { await api.post('/api/workspace/workspaces', { action: 'update', id: w.id, state: current() }); ui.toast(`Updated "${w.name}".`, 'green'); }
    catch (err) { ui.toast(err?.message || String(err), 'red'); }
    await load();
  }

  async function rename(w) {
    const name = await ui.prompt('Rename workspace', { label: 'Name', value: w.name, ok: 'Rename' });
    if (!name || name === w.name) return;
    try { await api.post('/api/workspace/workspaces', { action: 'rename', id: w.id, name }); }
    catch (err) { ui.toast(err?.message || String(err), 'red'); }
    await load();
  }

  async function remove(w) {
    if (!(await ui.confirm(`Delete the workspace "${w.name}"? This can't be undone.`, { ok: 'Delete', danger: true }))) return;
    try { await api.post('/api/workspace/workspaces', { action: 'delete', id: w.id }); ui.toast(`Deleted "${w.name}".`); }
    catch (err) { ui.toast(err?.message || String(err), 'red'); }
    await load();
  }

  function stamp() { return new Date().toISOString().slice(0, 10).replace(/-/g, ''); }

  function downloadDoc(items, filename) {
    const doc = { kind: 'meridian-workspaces', v: 1, exported_at: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'), count: items.length, workspaces: items };
    dom.download(filename, `${JSON.stringify(doc, null, 1)}\n`, 'application/json');
  }

  async function exportAll() {
    try {
      const doc = await api.get('/api/workspace/export');
      if (!doc.count) { ui.toast('There are no saved workspaces to export yet.', 'amber'); return; }
      dom.download(`meridian-workspaces-${stamp()}.json`, `${JSON.stringify(doc, null, 1)}\n`, 'application/json');
      ui.toast(`Exported ${doc.count} workspace${doc.count === 1 ? '' : 's'}.`, 'green');
    } catch (err) { ui.toast(err?.message || String(err), 'red'); }
  }

  async function importFile(file) {
    if (file.size > 400 * 1024) { ui.toast('That file is too large for a workspaces export.', 'red'); return; }
    let items;
    try { items = parseExport(await file.text()); } catch (err) { ui.toast(`That file can't be imported: ${err?.message || err}`, 'red'); return; }
    if (!items.length) { ui.toast('That file has no workspaces in it.', 'amber'); return; }
    const names = new Set(s.workspaces.map(w => w.name.toLowerCase()));
    const replace = items.filter(w => names.has(String(w.name).trim().toLowerCase())).length;
    const msg = `Import ${items.length} workspace${items.length === 1 ? '' : 's'} from ${file.name}?`
      + (replace ? ` ${replace} with the same name as one you have will replace it.` : '');
    if (!(await ui.confirm(msg, { ok: 'Import' }))) return;
    try {
      const r = await api.post('/api/workspace/workspaces', { action: 'import', workspaces: items });
      const parts = [`${r.created} added`, `${r.replaced} replaced`];
      if (r.skipped?.length) parts.push(`${r.skipped.length} skipped`);
      ui.toast(`Imported: ${parts.join(', ')}.`, r.skipped?.length ? 'amber' : 'green', 6000);
    } catch (err) { ui.toast(err?.message || String(err), 'red'); }
    await load();
  }

  // ------------------------------------------------------------------ rendering
  function renderTabs() {
    dom.clear(tabBar);
    for (const [id, label] of TABS) {
      const extra = id === 'workspaces' && s.workspaces.length ? h('span', { class: 'workspace-tab-count tnum' }, fmt.int(s.workspaces.length))
        : id === 'places' && places.state.places.length ? h('span', { class: 'workspace-tab-count tnum' }, fmt.int(places.state.places.length)) : null;
      const b = h('button', { type: 'button', role: 'tab', class: 'workspace-tab', 'aria-selected': String(id === tab) }, label, extra);
      b.addEventListener('click', () => show(id));
      tabBar.append(b);
    }
  }

  function row({ title, meta, onOpen, tools = [], cls = '' }) {
    const main = h('button', { type: 'button', class: 'workspace-item' }, h('span', { class: 'workspace-item-title' }, title),
      meta ? h('span', { class: 'workspace-item-meta' }, meta) : null);
    main.addEventListener('click', onOpen);
    return h('li', { class: `workspace-row ${cls}` }, main, ...tools);
  }

  function moreButton(label, items) {
    const b = h('button', { type: 'button', class: 'workspace-more', 'aria-label': label, title: label }, '⋯');
    b.addEventListener('click', () => ui.openMenu(b, items));
    return b;
  }

  function renderWorkspaces() {
    const save = h('button', { type: 'button', class: 'btn primary' }, 'Save this view');
    save.addEventListener('click', saveCurrent);
    save.disabled = !backend || null;
    const imp = h('button', { type: 'button', class: 'btn' }, 'Import');
    imp.addEventListener('click', () => fileInput.click());
    imp.disabled = !backend || null;
    const exp = h('button', { type: 'button', class: 'btn' }, 'Export all');
    exp.addEventListener('click', exportAll);
    exp.disabled = !backend || !s.workspaces.length || null;
    panel.append(h('div', { class: 'workspace-actions' }, save, h('span', { class: 'workspace-spacer' }), imp, exp));
    if (s.error) panel.append(h('p', { class: 'workspace-note bad' }, s.error));
    panel.append(h('h3', { class: 'workspace-h' }, 'Your workspaces'));
    if (!s.workspaces.length) {
      panel.append(h('p', { class: 'workspace-empty' }, s.loaded
        ? 'None saved yet. Set up the map the way you like it, then save this view. The presets below are good starting points.'
        : (backend ? 'Loading…' : 'Saving needs the workspace module on the server.')));
    } else {
      const ul = h('ul', { class: 'workspace-list' });
      for (const w of s.workspaces) {
        ul.append(row({ title: w.name, meta: `${describeState(w.state)}, saved ${fmt.ago(w.updated_at)}`,
          onOpen: () => apply(w.state, { name: `"${w.name}"` }),
          tools: [moreButton(`More for ${w.name}`, [
            { text: 'Apply', onClick: () => apply(w.state, { name: `"${w.name}"` }) },
            { text: 'Update with this view', onClick: () => updateWith(w) },
            { text: 'Rename', onClick: () => rename(w) },
            { text: 'Export', onClick: () => downloadDoc([{ name: w.name, state: w.state }], `meridian-workspace-${w.name.replace(/[^A-Za-z0-9_-]+/g, '-').toLowerCase()}-${stamp()}.json`) },
            { separator: true },
            { text: 'Delete', onClick: () => remove(w) },
          ])] }));
      }
      panel.append(ul);
      if (s.workspaces.length >= s.max) panel.append(h('p', { class: 'workspace-note' }, `You keep the most workspaces Meridian allows (${s.max}). Delete one to save another.`));
    }
    panel.append(h('h3', { class: 'workspace-h' }, 'Presets'));
    if (!s.presets.length) {
      panel.append(h('p', { class: 'workspace-empty' }, backend ? 'Loading…' : 'The presets come from the workspace module on the server.'));
    } else {
      const ul = h('ul', { class: 'workspace-list' });
      for (const p of s.presets) ul.append(row({ title: p.name, meta: p.description, onOpen: () => apply(p.state, { name: p.name }), cls: 'preset' }));
      panel.append(ul);
    }
    panel.append(h('p', { class: 'workspace-note faint' }, 'A workspace sets the view, the background map, globe or flat, 3D terrain, and exactly which layers are on with their settings. Applying one can be undone from its message.'));
  }

  async function placeForm({ title, place, ok }) {
    return new Promise((resolve) => {
      let done = false;
      const name = h('input', { type: 'text', value: place.name || '', maxlength: 80, 'aria-label': 'Name' });
      const type = h('select', { 'aria-label': 'Type' }, PLACE_TYPES.map(t => h('option', { value: t.id, selected: t.id === (place.type || 'home') || null }, t.label)));
      const radii = PLACE_RADII.includes(place.radius_km) ? PLACE_RADII : [...PLACE_RADII, place.radius_km].filter(Boolean).sort((a, b) => a - b);
      const radius = h('select', { 'aria-label': 'Radius for alerts' }, radii.map(r => h('option', { value: r, selected: r === (place.radius_km || 25) || null }, fmt.km(r))));
      const notes = h('input', { type: 'text', value: place.notes || '', maxlength: 500, 'aria-label': 'Notes' });
      const save = h('button', { type: 'button', class: 'btn primary' }, ok);
      const body = h('div', { class: 'workspace-form' },
        h('p', { class: 'workspace-note' }, `${fmt.latlon(place.lat, place.lon, 4)}. Kept in Meridian's database on this computer only.`),
        h('div', { class: 'field' }, h('label', null, 'Name'), name),
        h('div', { class: 'field' }, h('label', null, 'Type'), type),
        h('div', { class: 'field' }, h('label', null, 'Radius, if you watch it in Alerts'), radius),
        h('div', { class: 'field' }, h('label', null, 'Notes (optional)'), notes));
      const m = ui.modal({ title, body, foot: save, onClose: () => { if (!done) resolve(null); } });
      const submit = () => {
        if (!name.value.trim()) { name.focus(); return; }
        done = true;
        m.close();
        resolve({ name: name.value.trim(), type: type.value, radius_km: +radius.value, notes: notes.value.trim() });
      };
      save.addEventListener('click', submit);
      name.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
      name.focus();
    });
  }

  async function addCentre() {
    const c = ctx.map.getCenter();
    const base = { lat: +c.lat.toFixed(5), lon: +((((c.lng + 180) % 360) + 360) % 360 - 180).toFixed(5),
      zoom: Math.min(18, Math.max(2, Math.round(ctx.map.getZoom() * 10) / 10)) };
    const form = await placeForm({ title: 'Add the centre of the map', place: { ...base, type: 'home', radius_km: 25 }, ok: 'Add place' });
    if (!form) return;
    try { await places.create({ ...base, ...form }); ui.toast(`Added ${form.name} to My places.`, 'green'); }
    catch (err) { ui.toast(err?.message || String(err), 'red'); }
  }

  async function addDrawn() {
    if (!ctx.draw) return;
    ui.toast('Press and drag on the map: the centre is the place, the radius is for Alerts.', 'blue', 5000);
    let f = null;
    try { f = await ctx.draw.start({ mode: 'circle', color: '#ebe6d6' }); } catch { f = null; }
    if (!f) return;
    const [lon, lat] = f.properties?.center || [];
    if (!dom.validLatLon(lat, lon)) return;
    const r = Math.min(1000, Math.max(1, Math.round(f.properties.radius_km || 25)));
    const place = { lat: +(+lat).toFixed(5), lon: +(+lon).toFixed(5), zoom: Math.min(18, Math.max(2, Math.round(ctx.map.getZoom() * 10) / 10)), radius_km: r, type: 'home' };
    const form = await placeForm({ title: 'Add a place', place, ok: 'Add place' });
    if (!form) return;
    try { await places.create({ ...place, ...form }); ui.toast(`Added ${form.name} to My places.`, 'green'); }
    catch (err) { ui.toast(err?.message || String(err), 'red'); }
  }

  async function editPlace(p) {
    const form = await placeForm({ title: `Edit ${p.name}`, place: p, ok: 'Save' });
    if (!form) return;
    try { await places.update(p.id, form); } catch (err) { ui.toast(err?.message || String(err), 'red'); }
  }

  async function moveHere(p) {
    const c = ctx.map.getCenter();
    if (!(await ui.confirm(`Move ${p.name} to the centre of the map?`, { ok: 'Move' }))) return;
    try { await places.update(p.id, { lat: +c.lat.toFixed(5), lon: +(((c.lng + 180) % 360 + 360) % 360 - 180).toFixed(5) }); }
    catch (err) { ui.toast(err?.message || String(err), 'red'); }
  }

  async function deletePlace(p) {
    const extra = places.watching(p) ? ' Its Alerts watchlist stays in Alerts.' : '';
    if (!(await ui.confirm(`Delete ${p.name} from My places?${extra}`, { ok: 'Delete', danger: true }))) return;
    try { await places.remove(p.id); } catch (err) { ui.toast(err?.message || String(err), 'red'); }
  }

  function renderPlaces() {
    const st = places.state;
    panel.append(h('p', { class: 'workspace-note' }, 'Your own places, kept in Meridian\'s database on this computer. They are never sent to the assistant, to MCP tools or to any website. Watching one in Alerts gives Alerts its own copy.'));
    const centre = h('button', { type: 'button', class: 'btn primary' }, 'Add the map centre');
    centre.addEventListener('click', addCentre);
    const drawBtn = h('button', { type: 'button', class: 'btn' }, 'Draw a circle');
    drawBtn.addEventListener('click', addDrawn);
    const full = st.places.length >= st.max;
    centre.disabled = !backend || full || null;
    drawBtn.disabled = !backend || full || !ctx.draw || null;
    const showId = 'workspace-show-places';
    const show = h('input', { type: 'checkbox', id: showId, checked: ctx.layers.isOn('workspace.places') || null });
    show.addEventListener('change', () => ctx.layers.setOn('workspace.places', show.checked));
    panel.append(h('div', { class: 'workspace-actions' }, centre, drawBtn, h('span', { class: 'workspace-spacer' }),
      h('label', { class: 'workspace-switch', for: showId }, h('span', { class: 'switch' }, show, h('span')), 'On the map')));
    if (st.error) panel.append(h('p', { class: 'workspace-note bad' }, st.error));
    if (!st.places.length) {
      panel.append(h('p', { class: 'workspace-empty' }, st.loaded ? 'No places yet. Move the map to your home, work or family, then add the centre, or draw a circle.' : (backend ? 'Loading…' : 'Places need the workspace module on the server.')));
      return;
    }
    const ul = h('ul', { class: 'workspace-list' });
    for (const p of st.places) {
      const tools = [];
      const fly = h('button', { type: 'button', class: 'btn small' }, 'Fly to');
      fly.addEventListener('click', () => places.flyTo(p));
      tools.push(fly);
      if (places.watching(p)) tools.push(h('span', { class: 'workspace-tag', title: 'Alerts keeps a watchlist for this place' }, 'In Alerts'));
      else if (ctx.moduleOk('alerts')) {
        const watch = h('button', { type: 'button', class: 'btn small' }, 'Watch in Alerts');
        watch.addEventListener('click', () => places.watchInAlerts(p));
        tools.push(watch);
      }
      tools.push(moreButton(`More for ${p.name}`, [
        { text: 'Open card', onClick: () => { places.flyTo(p); places.openCard(p); } },
        { text: 'Edit', onClick: () => editPlace(p) },
        { text: 'Move to the map centre', onClick: () => moveHere(p) },
        { separator: true },
        { text: 'Delete', onClick: () => deletePlace(p) },
      ]));
      ul.append(row({ title: p.name, meta: `${typeLabel(p.type)}, ${fmt.latlon(p.lat, p.lon, 3)}, ${fmt.km(p.radius_km)}`,
        onOpen: () => { places.flyTo(p); places.openCard(p); }, tools, cls: 'place' }));
    }
    panel.append(ul);
    if (full) panel.append(h('p', { class: 'workspace-note' }, `You keep the most places Meridian allows (${st.max}).`));
  }

  function exportable() {
    return ctx.layers.list().filter(l => l.on);
  }

  function renderExport() {
    panel.append(h('h3', { class: 'workspace-h' }, 'Export what\'s on screen'));
    panel.append(h('p', { class: 'workspace-note' }, 'Saves what one layer has drawn inside the current view, as it is now. It is not the whole dataset: move or zoom the map to change what goes in. The file is made in this browser.'));
    const on = exportable();
    if (!on.length) { panel.append(h('p', { class: 'workspace-empty' }, 'Turn on a layer first.')); return; }
    if (!exportLayerId || !on.some(l => l.id === exportLayerId)) exportLayerId = on[0].id;
    const select = h('select', { 'aria-label': 'Layer to export' }, on.map(l => h('option', { value: l.id, selected: l.id === exportLayerId || null }, `${l.title} (${l.group})`)));
    select.addEventListener('change', () => { exportLayerId = select.value; exportNote = null; renderExportCount(); });
    const seg = h('div', { class: 'seg workspace-seg', role: 'group', 'aria-label': 'File format' });
    for (const [id, f] of Object.entries(FORMATS)) {
      const b = h('button', { type: 'button', 'aria-pressed': String(id === exportFormat) }, f.label);
      b.addEventListener('click', () => { exportFormat = id; ctx.settings.set(FORMAT_KEY, id); for (const x of seg.children) x.setAttribute('aria-pressed', String(x === b)); });
      seg.append(b);
    }
    const go = h('button', { type: 'button', class: 'btn primary' }, 'Download');
    go.addEventListener('click', async () => {
      go.disabled = true;
      exportNote = { text: 'Collecting what is on screen…' };
      paintNote();
      try {
        const r = await exportLayer(ctx, exportLayerId, exportFormat);
        if (!r.ok) exportNote = { text: r.message, tone: 'amber' };
        else {
          const extras = [];
          if (r.capped) extras.push('capped at 50,000 features: zoom in for a smaller area');
          if (r.shared) extras.push('this layer shares its map drawing with others in its group, so their features are included');
          if (r.dropped?.windy) extras.push(`${fmt.int(r.dropped.windy)} Windy webcams left out (their terms)`);
          if (r.dropped?.privacy) extras.push(`${fmt.int(r.dropped.privacy)} aircraft left out (privacy programmes, or not confirmed)`);
          exportNote = { text: `Saved ${fmt.int(r.count)} feature${r.count === 1 ? '' : 's'} as ${r.name}.${extras.length ? ` Note: ${extras.join('; ')}.` : ''}`, tone: 'green' };
        }
      } catch (err) {
        exportNote = { text: `The export failed: ${err?.message || err}`, tone: 'red' };
      } finally {
        go.disabled = false;
        paintNote();
      }
    });
    panel.append(h('div', { class: 'field' }, h('label', null, 'Layer'), select),
      h('div', { class: 'workspace-actions' }, seg, h('span', { class: 'workspace-spacer' }), go),
      h('p', { class: 'workspace-count dim', 'data-role': 'count' }),
      h('p', { class: 'workspace-note', 'data-role': 'note', hidden: true }),
      h('p', { class: 'workspace-note faint' }, 'Credits and licences from the layer\'s sources go into the file. Windy webcams, aircraft in privacy programmes, owner details, trails and selections are never exported.'));
    renderExportCount();
    paintNote();
  }

  function renderExportCount() {
    const el = panel.querySelector('[data-role="count"]');
    if (!el || !exportLayerId) return;
    const q = quickCount(ctx, exportLayerId);
    el.textContent = q.raster ? 'This layer is a picture (map tiles): there are no features to export.'
      : q.count ? `About ${fmt.int(q.count)} feature${q.count === 1 ? '' : 's'} drawn in view.${exportLayerId === 'cameras' ? ' Windy webcams among them are left out.' : ''}`
        : 'Nothing from this layer is drawn in view right now.';
  }

  function paintNote() {
    const el = panel.querySelector('[data-role="note"]');
    if (!el) return;
    el.hidden = !exportNote;
    el.textContent = exportNote?.text || '';
    el.className = `workspace-note${exportNote?.tone ? ` ${exportNote.tone}` : ''}`;
  }

  function render() {
    if (!drawer) return;
    renderTabs();
    const top = panel.scrollTop;
    dom.clear(panel);
    if (tab === 'workspaces') renderWorkspaces();
    else if (tab === 'places') renderPlaces();
    else renderExport();
    panel.scrollTop = top;
  }

  function show(id) {
    tab = id;
    ctx.settings.set(TAB_KEY, id);
    if (!drawer) { open(); return; }
    panel.scrollTop = 0;
    render();
    if (id === 'places') { places.load().then(() => places.checkWatched()).catch(() => {}); }
  }

  const unsub = [];
  function open() {
    if (drawer) return;
    const copy = h('button', { type: 'button', class: 'text-btn', title: 'Copy a link that opens this view, its layers and the open card' }, 'Copy link');
    copy.addEventListener('click', () => ctx.deeplink?.copy());
    drawer = ui.drawer({ id: 'workspace', title: 'Workspaces', el: root, width: 420, tools: copy, onClose: () => {
      drawer = null;
      while (unsub.length) unsub.pop()();
      for (const fn of openListeners) fn(false);
    } });
    unsub.push(places.on(() => { if (tab === 'places' || tab === 'workspaces') render(); }));
    unsub.push(ctx.bus.on('map:moveend', dom.debounce(() => { if (drawer && tab === 'export') renderExportCount(); }, 400)));
    unsub.push(ctx.bus.on('layer:toggled', dom.debounce(() => { if (drawer && (tab === 'export' || tab === 'places')) render(); }, 300)));
    for (const fn of openListeners) fn(true);
    render();
    load();
    if (tab === 'places') places.load().then(() => places.checkWatched()).catch(() => {});
  }

  return {
    open, show, apply, current, load,
    close() { drawer?.close(); },
    toggle() { if (drawer) drawer.close(); else open(); },
    isOpen: () => !!drawer,
    onOpenChange(fn) { openListeners.add(fn); return () => openListeners.delete(fn); },
  };
}
