// Watchlists tab: create (draw, place and radius, whole world), edit rules, rename, pause, show on map,
// redraw and delete. Areas are drawn with ctx.draw (CONTRACT.md 15.3).

import { circlePolygon, isPlainEscape } from './logic.js';

export const COLORS = [['#6cb6ff', 'Blue'], ['#ffb547', 'Amber'], ['#5fd38d', 'Green'], ['#ff5d52', 'Red']];
const DEFAULT_RULES = ['quakes', 'storms', 'warnings', 'volcanoes'];
const RADII = [5, 10, 25, 50, 100, 250, 500, 1000];
export const DRAFT_ID = 'alerts:draft';
const MODES = [['polygon', 'Polygon', 'Click points on the map; double-click or press Enter to finish'],
  ['circle', 'Circle', 'Press at the centre and drag out the radius'],
  ['rectangle', 'Rectangle', 'Press at one corner and drag to the opposite one']];

function zoomFor(radiusKm) {
  return radiusKm <= 5 ? 11 : radiusKm <= 10 ? 10 : radiusKm <= 25 ? 9 : radiusKm <= 50 ? 8 : radiusKm <= 100 ? 7
    : radiusKm <= 250 ? 6 : radiusKm <= 500 ? 5 : 4;
}

export function watchlistsView(ctx, state, { onSaved = null, onDrawing = null } = {}) {
  const { h, dom, ui, fmt } = ctx;
  const root = h('div', { class: 'alerts-wl' });
  let draft = null;           // {area, name, color, rules: {preset: {enabled, params}}, preview, areaText, saving}
  let place = null;           // {q, results, busy, error}
  let drawing = null;         // {mode, watchlist?} while ctx.draw is active
  const expanded = new Set();
  const previews = new Map(); // watchlist id -> 'loading' | {count, events} | {error}

  const nextColor = () => COLORS[state.watchlists.length % COLORS.length][0];
  const defaults = (p) => Object.fromEntries(p.params.map(x => [x.id, x.default]));

  // ------------------------------------------------------------------ shared controls
  function presetRow(p, value, onChange, key) {
    const id = `alerts-${key}-${p.id}`;
    const off = !p.available;
    const check = h('input', { type: 'checkbox', id, checked: (value.enabled && !off) || null, disabled: off || null,
      dataset: { focusKey: id } });
    check.addEventListener('change', () => onChange({ enabled: check.checked }));
    const controls = h('div', { class: 'alerts-params' });
    for (const par of p.params) {
      const fk = `${id}-${par.id}`;
      if (par.type === 'select') {
        const sel = h('select', { 'aria-label': `${p.label}: ${par.label}`, dataset: { focusKey: fk } },
          (par.choices || []).map(c => h('option', { value: String(c.value), selected: String(c.value) === String(value.params[par.id]) || null }, c.label)));
        sel.addEventListener('change', () => {
          const choice = (par.choices || []).find(c => String(c.value) === sel.value);
          if (choice) onChange({ params: { ...value.params, [par.id]: choice.value } });
        });
        controls.append(h('label', { class: 'alerts-param' }, h('span', null, par.label), sel));
      } else if (par.type === 'toggle') {
        const cb = h('input', { type: 'checkbox', checked: value.params[par.id] || null, dataset: { focusKey: fk } });
        cb.addEventListener('change', () => onChange({ params: { ...value.params, [par.id]: cb.checked } }));
        controls.append(h('label', { class: 'alerts-param alerts-param-toggle' }, cb, h('span', null, par.label)));
      }
    }
    return h('div', { class: `alerts-preset${value.enabled && !off ? ' on' : ''}${off ? ' unavailable' : ''}` },
      h('div', { class: 'alerts-preset-head' }, check, h('label', { for: id }, p.label)),
      h('p', { class: 'alerts-preset-desc' }, off ? (p.note || 'Not available right now.') : p.description),
      value.enabled && !off && p.params.length ? controls : null);
  }

  function colorPicker(current, onPick, key) {
    const box = h('div', { class: 'alerts-colors', role: 'radiogroup', 'aria-label': 'Colour on the map' });
    for (const [c, label] of COLORS) {
      const b = h('button', { type: 'button', class: 'alerts-color', role: 'radio', 'aria-checked': String(c === current),
        'aria-label': label, title: label, style: { '--c': c }, dataset: { focusKey: `${key}-color-${c}` } });
      b.addEventListener('click', () => onPick(c));
      box.append(b);
    }
    return box;
  }

  function radiusSelect(current, onPick, key) {
    const values = RADII.includes(current) ? RADII : [...RADII, current].sort((a, b) => a - b);
    const sel = h('select', { 'aria-label': 'Radius', dataset: { focusKey: `${key}-radius` } },
      values.map(r => h('option', { value: String(r), selected: r === current || null }, fmt.km(r))));
    sel.addEventListener('change', () => onPick(+sel.value));
    return sel;
  }

  const field = (label, control) => h('div', { class: 'alerts-field' }, h('span', { class: 'alerts-field-label' }, label), control);

  // ------------------------------------------------------------------ drafts (a new watchlist)
  function startDraft(area, { name = '', preview = null, rules = null, areaText = '' } = {}) {
    const r = {};
    for (const p of state.presets) {
      const given = rules ? rules[p.id] : null;
      r[p.id] = { enabled: rules ? !!given : DEFAULT_RULES.includes(p.id), params: { ...defaults(p), ...(given || {}) } };
    }
    draft = { area, name, color: nextColor(), rules: r, preview, areaText, saving: false };
    place = null;
    showPreview();
    render();
    root.querySelector('.alerts-draft input[name="name"]')?.focus({ preventScroll: true });
  }

  function showPreview() {
    if (!draft?.preview) { ctx.draw.hide(DRAFT_ID); return; }
    try {
      ctx.draw.show(DRAFT_ID, draft.preview, { color: draft.color, label: draft.name.trim() || 'New area' });
    } catch (err) {
      console.warn('alerts: preview failed', err);
    }
  }

  function cancelDraft() {
    draft = null;
    place = null;
    ctx.draw.hide(DRAFT_ID);
    render();
  }

  async function drawArea(mode, watchlist = null) {
    if (ctx.draw.active()) ctx.draw.cancel();
    draft = null;
    place = null;
    ctx.draw.hide(DRAFT_ID);
    drawing = { mode, watchlist };
    render();
    onDrawing?.(true);
    let f = null;
    try {
      f = await ctx.draw.start({ mode, color: watchlist ? watchlist.color : nextColor() });
    } catch (err) {
      ui.toast(`Drawing is not available: ${err?.message || err}`, 'red');
    } finally {
      drawing = null;
      onDrawing?.(false);
    }
    if (!f) { render(); return; }
    const area = mode === 'circle'
      ? { type: 'circle', center: f.properties.center, radius_km: f.properties.radius_km }
      : { type: mode, geometry: f };
    if (watchlist) {
      try {
        await state.update(watchlist.id, { area });
        previews.delete(watchlist.id);
        ui.toast(`The area of ${watchlist.name} was redrawn. It starts again from what is there now.`, 'green');
      } catch (err) {
        ui.toast(`The new area was not saved: ${err?.message || err}`, 'red');
      }
      render();
      return;
    }
    const size = f.properties?.area_km2;
    const word = { polygon: 'Polygon', circle: 'Circle', rectangle: 'Rectangle' }[mode];
    const text = mode === 'circle' ? `${word}, radius ${fmt.km(f.properties.radius_km)}` : `${word}${size ? `, ${fmt.int(size)} km²` : ''}`;
    startDraft(area, { preview: f, areaText: text });
  }

  function startPlace() {
    if (ctx.draw.active()) ctx.draw.cancel();
    draft = null;
    ctx.draw.hide(DRAFT_ID);
    place = { q: '', results: null, busy: false, error: null };
    render();
    root.querySelector('.alerts-place input')?.focus({ preventScroll: true });
  }

  async function searchPlace(q) {
    const text = q.trim();
    if (text.length < 2) return;
    place = { ...place, q: text, busy: true, error: null };
    render();
    try {
      const r = await ctx.api.get('/api/core/geocode', { q: text });
      place = { ...place, results: (r.results || []).slice(0, 6), busy: false };
    } catch (err) {
      place = { ...place, results: [], busy: false, error: err?.message || 'no answer' };
    }
    render();
  }

  function pickPlace(p) {
    const radius = 50;
    const area = { type: 'place', place: { name: p.title, lat: +p.lat, lon: +p.lon }, radius_km: radius };
    ctx.map2.flyTo(+p.lat, +p.lon, zoomFor(radius));
    startDraft(area, { name: p.title, preview: circlePolygon(+p.lat, +p.lon, radius), areaText: `${fmt.km(radius)} around ${p.title}` });
  }

  function startWorld() {
    if (ctx.draw.active()) ctx.draw.cancel();
    startDraft({ type: 'world' }, { name: 'World', rules: { any: { min_severity: 9 } },
      areaText: 'The whole world. Keep the severity high: the world produces hundreds of events a day.' });
  }

  async function saveDraft() {
    const d = draft;
    if (!d || d.saving) return;
    const rules = Object.entries(d.rules).filter(([id, v]) => v.enabled && state.preset(id)?.available !== false)
      .map(([preset, v]) => ({ preset, params: v.params }));
    if (!rules.length) { ui.toast('Tick at least one thing to watch for.', 'amber'); return; }
    d.saving = true;
    render();
    try {
      const w = await state.create({ name: d.name.trim() || null, color: d.color, area: d.area, rules });
      draft = null;
      ctx.draw.hide(DRAFT_ID);
      onSaved?.(w);
      ui.toast(`Watching ${w.name}. New alerts will arrive in the inbox.`, 'green');
    } catch (err) {
      d.saving = false;
      ui.toast(`The watchlist was not saved: ${err?.message || err}`, 'red');
    }
    render();
  }

  // Escape inside the draft or place form cancels just that form (like its Cancel button) and keeps the
  // drawer open; without this the core's Escape handler would close the whole drawer and drop the draft.
  // A search box with text is cleared by the browser first.
  function escapeCancels(form) {
    form.addEventListener('keydown', (e) => {
      if (!isPlainEscape(e)) return;
      e.stopPropagation();
      if (e.target?.type === 'search' && e.target.value) return;
      e.preventDefault();
      cancelDraft();
      root.querySelector('[data-focus-key="new-draw"]')?.focus({ preventScroll: true });
    });
    return form;
  }

  function draftForm() {
    const d = draft;
    const name = h('input', { type: 'text', name: 'name', value: d.name, maxlength: 80, autocomplete: 'off',
      placeholder: d.area.type === 'world' ? 'World' : 'For example: Home, Baltic Sea', 'aria-label': 'Watchlist name',
      dataset: { focusKey: 'draft-name' } });
    name.addEventListener('input', () => { d.name = name.value; showPreviewSoon(); });
    name.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveDraft(); });
    const radius = d.area.type === 'place'
      ? radiusSelect(d.area.radius_km, (r) => {
        d.area = { ...d.area, radius_km: r };
        d.preview = circlePolygon(d.area.place.lat, d.area.place.lon, r);
        d.areaText = `${fmt.km(r)} around ${d.area.place.name}`;
        ctx.map2.flyTo(d.area.place.lat, d.area.place.lon, zoomFor(r));
        showPreview();
        render();
      }, 'draft') : null;
    const presets = h('div', { class: 'alerts-presets' }, state.presets.map(p => presetRow(p, d.rules[p.id], (ch) => {
      Object.assign(d.rules[p.id], ch);
      render();
    }, 'draft')));
    const save = h('button', { type: 'button', class: 'btn primary', disabled: d.saving || null }, d.saving ? 'Saving…' : 'Save watchlist');
    save.addEventListener('click', saveDraft);
    const cancel = h('button', { type: 'button', class: 'btn' }, 'Cancel');
    cancel.addEventListener('click', cancelDraft);
    return escapeCancels(h('section', { class: 'alerts-draft', 'aria-label': 'New watchlist', style: { '--wl': d.color } },
      h('h3', null, 'New watchlist'), d.areaText ? h('p', { class: 'alerts-note' }, d.areaText) : null,
      field('Name', name), radius ? field('Radius', radius) : null,
      field('Colour', colorPicker(d.color, (c) => { d.color = c; showPreview(); render(); }, 'draft')),
      h('h4', null, 'Watch for'), presets, h('div', { class: 'alerts-actions' }, save, cancel)));
  }

  let previewTimer = null;
  function showPreviewSoon() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(showPreview, 300);
  }

  function placeForm() {
    const input = h('input', { type: 'search', value: place.q, placeholder: 'Town, address or landmark', 'aria-label': 'Place',
      autocomplete: 'off', dataset: { focusKey: 'place-q' } });
    const go = h('button', { type: 'button', class: 'btn primary', disabled: place.busy || null }, place.busy ? 'Searching…' : 'Search');
    go.addEventListener('click', () => searchPlace(input.value));
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') searchPlace(input.value); });
    const cancel = h('button', { type: 'button', class: 'btn' }, 'Cancel');
    cancel.addEventListener('click', cancelDraft);
    const out = h('section', { class: 'alerts-draft alerts-place', 'aria-label': 'Place and radius' },
      h('h3', null, 'Place and radius'), h('p', { class: 'alerts-note' }, 'Search for a place, then choose how far around it to watch.'),
      h('div', { class: 'alerts-search' }, input, go));
    if (place.error) out.append(h('p', { class: 'alerts-note bad' }, `Place search is unavailable right now (${place.error}).`));
    else if (place.results && !place.results.length) out.append(h('p', { class: 'alerts-note' }, 'No places found. Try another spelling or a larger place.'));
    if (place.results?.length) {
      const list = h('ul', { class: 'alerts-results' });
      for (const p of place.results) {
        const b = h('button', { type: 'button', class: 'alerts-result' }, h('span', { class: 'alerts-title' }, p.title),
          p.subtitle ? h('span', { class: 'alerts-why' }, p.subtitle) : null);
        b.addEventListener('click', () => pickPlace(p));
        list.append(h('li', null, b));
      }
      out.append(list);
    }
    out.append(h('p', { class: 'alerts-note faint' }, 'Place search by OpenStreetMap Nominatim.'), h('div', { class: 'alerts-actions' }, cancel));
    return escapeCancels(out);
  }

  // ------------------------------------------------------------------ saved watchlists
  async function change(w, patch, { quiet = false } = {}) {
    try {
      await state.update(w.id, patch);
      return true;
    } catch (err) {
      if (!quiet) ui.toast(`That change was not saved: ${err?.message || err}`, 'red');
      return false;
    }
  }

  async function loadPreview(w) {
    previews.set(w.id, 'loading');
    render();
    try {
      previews.set(w.id, await state.matches(w.id, 8));
    } catch (err) {
      previews.set(w.id, { error: err?.message || 'no answer' });
    }
    render();
  }

  function previewBox(w) {
    const p = previews.get(w.id);
    if (!p) {
      const b = h('button', { type: 'button', class: 'text-btn', dataset: { focusKey: `wl${w.id}-preview` } }, 'Show what matches now');
      b.addEventListener('click', () => loadPreview(w));
      return h('div', { class: 'alerts-preview' }, b);
    }
    if (p === 'loading') return h('div', { class: 'alerts-preview' }, h('p', { class: 'alerts-note' }, 'Checking…'));
    if (p.error) return h('div', { class: 'alerts-preview' }, h('p', { class: 'alerts-note bad' }, `Not available right now (${p.error}).`));
    const box = h('div', { class: 'alerts-preview' }, h('p', { class: 'alerts-note' }, p.count
      ? `${fmt.int(p.count)} event${p.count === 1 ? '' : 's'} here match${p.count === 1 ? 'es' : ''} your rules right now. You hear only about new ones.`
      : (p.note || 'Nothing here matches your rules right now.')));
    if (p.events?.length) {
      const list = h('ul', { class: 'alerts-list compact' });
      for (const ev of p.events) {
        const b = h('button', { type: 'button', class: 'alerts-item' }, h('span', { class: 'alerts-text' },
          h('span', { class: 'alerts-title' }, ev.title), h('span', { class: 'alerts-meta' }, `${ev.rule}${ev.cone ? ', forecast cone' : ''}, severity ${fmt.num(ev.severity, 1)}`)));
        b.addEventListener('click', () => ctx.entities.open(ev.entity));
        list.append(h('li', { class: 'alerts-row' }, b));
      }
      box.append(list);
    }
    return box;
  }

  function editPanel(w) {
    const values = {};
    for (const p of state.presets) {
      const r = w.rules.find(x => x.preset === p.id);
      values[p.id] = { enabled: !!r?.enabled, params: { ...defaults(p), ...(r?.params || {}) } };
    }
    const presets = h('div', { class: 'alerts-presets' }, state.presets.map(p => presetRow(p, values[p.id], async (ch) => {
      const next = { enabled: ch.enabled ?? values[p.id].enabled, params: ch.params ?? values[p.id].params };
      try {
        await state.setRule(w.id, p.id, next);
        previews.delete(w.id);
      } catch (err) {
        ui.toast(`That rule was not saved: ${err?.message || err}`, 'red');
        render();
      }
    }, `wl${w.id}`)));
    const tools = h('div', { class: 'alerts-actions' });
    if (['polygon', 'rectangle', 'circle'].includes(w.area.type)) {
      const redraw = h('button', { type: 'button', class: 'btn', dataset: { focusKey: `wl${w.id}-redraw` } }, 'Redraw the area');
      redraw.addEventListener('click', () => drawArea(w.area.type, w));
      tools.append(redraw);
    }
    const radius = w.area.type === 'place' ? radiusSelect(w.area.radius_km, async (r) => {
      if (await change(w, { area: { type: 'place', place: w.area.place, radius_km: r } })) previews.delete(w.id);
    }, `wl${w.id}`) : null;
    return h('div', { class: 'alerts-card-edit' },
      radius ? field('Radius', radius) : null,
      field('Colour', colorPicker(w.color, (c) => change(w, { color: c }), `wl${w.id}`)),
      h('h4', null, 'Watch for'), presets, tools.childElementCount ? tools : null, previewBox(w),
      h('p', { class: 'alerts-note faint' }, 'Changing the area or a rule starts it again from what is there now, so nothing old floods the inbox.'));
  }

  function card(w) {
    const open = expanded.has(w.id);
    const on = w.rules.filter(r => r.enabled);
    const sw = h('input', { type: 'checkbox', checked: w.enabled || null, 'aria-label': `Watch ${w.name}`, dataset: { focusKey: `wl${w.id}-on` } });
    sw.addEventListener('change', () => change(w, { enabled: sw.checked }));
    const act = (text, fn, key) => {
      const b = h('button', { type: 'button', class: 'text-btn', dataset: { focusKey: `wl${w.id}-${key}` } }, text);
      b.addEventListener('click', fn);
      return b;
    };
    const stats = [w.unread ? `${fmt.int(w.unread)} unread` : null,
      w.last_24h ? `${fmt.int(w.last_24h)} alert${w.last_24h === 1 ? '' : 's'} in 24 hours` : 'No alerts in 24 hours'].filter(Boolean).join(', ');
    return h('article', { class: `alerts-card${w.enabled ? '' : ' paused'}${open ? ' open' : ''}`, style: { '--wl': w.color }, 'aria-label': w.name },
      h('div', { class: 'alerts-card-head' },
        h('span', { class: 'alerts-swatch', 'aria-hidden': 'true' }),
        h('h3', { class: 'alerts-card-name' }, w.name),
        h('label', { class: 'alerts-watching' }, h('span', null, w.enabled ? 'Watching' : 'Paused'), h('span', { class: 'switch' }, sw, h('span')))),
      h('p', { class: 'alerts-card-area' }, w.area.summary || ''),
      h('ul', { class: 'alerts-card-rules' }, !on.length ? h('li', { class: 'faint' }, 'Nothing switched on yet')
        : [...(on.length > 4 && !open ? on.slice(0, 3) : on).map(r => h('li', null, r.summary)),
          on.length > 4 && !open ? h('li', { class: 'faint' }, `and ${on.length - 3} more`) : null]),
      h('p', { class: 'alerts-card-stats tnum' }, stats),
      h('div', { class: 'alerts-card-actions' },
        act(open ? 'Done' : 'Edit', () => { if (open) expanded.delete(w.id); else expanded.add(w.id); render(); }, 'edit'),
        w.area.type !== 'world' ? act(w.show_on_map ? 'Hide on map' : 'Show on map', async () => {
          if (await change(w, { show_on_map: !w.show_on_map }) && !w.show_on_map) ctx.layers.setOn('alerts.areas', true);
        }, 'show') : null,
        act('Fly to', () => {
          if (w.area.view_bbox) ctx.map2.fitBbox(w.area.view_bbox);
          else ctx.map2.flyTo(20, 0, 1.5);
        }, 'fly'),
        act('Rename', async () => {
          const name = await ui.prompt('Rename watchlist', { label: 'Name', value: w.name, ok: 'Rename' });
          if (name && name !== w.name) change(w, { name });
        }, 'rename'),
        act('Delete', async () => {
          if (!await ui.confirm(`Delete “${w.name}” and its alerts?`, { ok: 'Delete', danger: true })) return;
          try {
            await state.remove(w.id);
            ui.toast(`Deleted ${w.name}.`);
          } catch (err) {
            ui.toast(`${w.name} was not deleted: ${err?.message || err}`, 'red');
          }
        }, 'delete')),
      open ? editPanel(w) : null);
  }

  function newBar() {
    const busy = !!drawing;
    const drawBtn = h('button', { type: 'button', class: 'btn primary', 'aria-haspopup': 'menu', disabled: busy || null, dataset: { focusKey: 'new-draw' } }, 'Draw an area');
    drawBtn.addEventListener('click', () => ui.openMenu(drawBtn, MODES.map(([mode, label, note]) => ({ text: label, note, onClick: () => drawArea(mode) })), { align: 'left' }));
    const placeBtn = h('button', { type: 'button', class: 'btn', disabled: busy || null, dataset: { focusKey: 'new-place' } }, 'Place and radius');
    placeBtn.addEventListener('click', startPlace);
    const worldBtn = h('button', { type: 'button', class: 'btn', disabled: busy || null, dataset: { focusKey: 'new-world' } }, 'Whole world');
    worldBtn.addEventListener('click', startWorld);
    return h('div', { class: 'alerts-new', role: 'group', 'aria-label': 'New watchlist' }, drawBtn, placeBtn, worldBtn);
  }

  function render() {
    const focusKey = root.contains(document.activeElement) ? document.activeElement?.dataset?.focusKey : null;
    dom.clear(root);
    root.append(newBar());
    if (drawing) {
      const stop = h('button', { type: 'button', class: 'text-btn' }, 'Cancel');
      stop.addEventListener('click', () => ctx.draw.cancel());
      const what = drawing.watchlist ? `Drawing a new area for ${drawing.watchlist.name}.` : `Drawing a ${drawing.mode} on the map.`;
      root.append(h('p', { class: 'alerts-note alerts-drawing' }, `${what} Follow the hint at the top of the map, or press Esc to stop. `, stop));
    }
    if (draft) root.append(draftForm());
    else if (place) root.append(placeForm());
    if (!state.loaded) {
      root.append(h('p', { class: 'alerts-empty' }, 'Loading…'));
    } else if (!state.watchlists.length) {
      if (!draft && !place && !drawing) root.append(h('div', { class: 'alerts-empty' }, h('p', null, 'No watchlists yet. Draw an area to start.'),
        h('p', { class: 'faint' }, 'Or pick a place and a radius, or watch the whole world for the most serious events.')));
    } else {
      const list = h('div', { class: 'alerts-cards' });
      for (const w of state.watchlists) list.append(card(w));
      root.append(list);
    }
    if (focusKey) root.querySelector(`[data-focus-key="${CSS.escape(focusKey)}"]`)?.focus({ preventScroll: true });
  }

  /** Close: drop an unsaved draft and stop drawing. */
  function reset() {
    if (drawing && ctx.draw.active()) ctx.draw.cancel();
    draft = null;
    place = null;
    ctx.draw.hide(DRAFT_ID);
  }

  return { el: root, render, reset, drawArea, startPlace, startWorld, hasDraft: () => !!draft || !!place };
}
