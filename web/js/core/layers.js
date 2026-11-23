// Layer registry and the left panel (CONTRACT.md section 10.6).
// Modules call ctx.layers.register(spec); the core owns toggling, persistence, refresh timers,
// view-change refreshes, counts, status dots, options and legends.

import { bus } from './bus.js';
import { h, clear, fmt, debounce, svgIcon } from './dom.js';
import { viewBounds, getMap } from './map.js';

export const GROUPS = [
  { id: 'Cameras', color: 'var(--g-cameras)' },
  { id: 'Flights', color: 'var(--g-flights)' },
  { id: 'Ships', color: 'var(--g-ships)' },
  { id: 'Conflict', color: 'var(--g-conflict)' },
  { id: 'Intelligence', color: 'var(--g-intel)' },
  { id: 'Infrastructure', color: 'var(--g-infra)' },
  { id: 'Companies', color: 'var(--g-companies)' },
  { id: 'Events', color: 'var(--g-events)' },
  { id: 'Space', color: 'var(--g-space)' },
  { id: 'Weather', color: 'var(--g-weather)' },
  { id: 'Imagery', color: 'var(--g-imagery)' },
  { id: 'Saved', color: 'var(--g-saved)' },
];

export function groupColor(group) {
  return (GROUPS.find(g => g.id === group) || { color: 'var(--paper-faint)' }).color;
}

const registry = new Map();      // id -> entry
const order = [];
let panel = null;
let filterText = '';
const closedGroups = new Set(JSON.parse(localStorage.getItem('meridian.layers.closed') || '[]'));
let restoring = false;

function savedOn() {
  try { return new Set(JSON.parse(localStorage.getItem('meridian.layers.on') || '[]')); } catch { return new Set(); }
}
const suspended = new Set();   // layers switched off by suspend(): still saved as on

function persistOn() {
  const on = [...new Set([...[...registry.values()].filter(e => e.on).map(e => e.spec.id), ...suspended])];
  try { localStorage.setItem('meridian.layers.on', JSON.stringify(on)); } catch { /* ignore */ }
}
function loadOptions(id) {
  try { return JSON.parse(localStorage.getItem('meridian.layers.opt.' + id) || '{}'); } catch { return {}; }
}
function saveOptions(entry) {
  try { localStorage.setItem('meridian.layers.opt.' + entry.spec.id, JSON.stringify(entry.options)); } catch { /* ignore */ }
}

/**
 * spec: {
 *   id, group, title, subtitle?, description?, source?, color?, symbol?: 'point'|'line'|'area'|'icon',
 *   defaultOn?, refreshMs?, viewDependent?, minzoom?,
 *   enable(ctx)?, disable()?, refresh({bounds, zoom, signal, reason})?,
 *   options?: [{id, label, type: 'toggle'|'select'|'range', default, choices?: [{value, label}], min?, max?, step?}],
 *   onOption?(id, value, allOptions), legend?: [{color, label, shape?}]
 * }
 */
export function register(spec) {
  if (!spec || !spec.id) throw new Error('layer spec needs an id');
  if (registry.has(spec.id)) throw new Error(`layer ${spec.id} registered twice`);
  const options = {};
  for (const o of spec.options || []) options[o.id] = o.default;
  Object.assign(options, loadOptions(spec.id));
  const entry = { spec, on: false, count: null, status: 'idle', message: null, options, timer: null, abort: null,
    busy: false, enabledOnce: false, row: null, expanded: false };
  registry.set(spec.id, entry);
  order.push(spec.id);
  render();
  const wanted = savedOn();
  const hasSaved = localStorage.getItem('meridian.layers.on') != null;
  if ((hasSaved && wanted.has(spec.id)) || (!hasSaved && spec.defaultOn)) {
    queueMicrotask(() => setOn(spec.id, true, { persist: false }));
  }
  return handleFor(spec.id);
}

function handleFor(id) {
  return {
    id,
    isOn: () => isOn(id),
    setCount: (n) => setCount(id, n),
    setStatus: (s, msg) => setStatus(id, s, msg),
    option: (name) => registry.get(id)?.options[name],
    options: () => ({ ...(registry.get(id)?.options || {}) }),
    refresh: (reason = 'manual') => refresh(id, reason),
  };
}

export function get(id) { return registry.get(id) || null; }
export function isOn(id) { return !!registry.get(id)?.on; }
export function list() {
  return order.map(id => {
    const e = registry.get(id);
    return { id, title: e.spec.title, group: e.spec.group, on: e.on, count: e.count, status: e.status, message: e.message,
      description: e.spec.description || '', source: e.spec.source || '', refreshMs: e.spec.refreshMs || 0 };
  });
}

export async function setOn(id, on, { persist = true } = {}) {
  const entry = registry.get(id);
  if (!entry || entry.on === !!on) return;
  entry.on = !!on;
  updateRow(entry);
  if (persist && !restoring) persistOn();
  bus.emit('layer:toggled', { id, on: entry.on });
  if (entry.on) {
    try {
      setStatus(id, 'loading');
      if (entry.spec.enable) await entry.spec.enable({ options: { ...entry.options } });
      entry.enabledOnce = true;
      if (entry.status === 'loading' && !entry.spec.refresh) setStatus(id, 'ok');
      if (entry.spec.refresh) await refresh(id, 'enable');
      startTimer(entry);
    } catch (err) {
      console.error(`layer ${id} failed to enable`, err);
      setStatus(id, 'error', err?.message || String(err));
    }
  } else {
    stopTimer(entry);
    entry.abort?.abort();
    try { if (entry.spec.disable) await entry.spec.disable(); } catch (err) { console.error(`layer ${id} failed to disable`, err); }
    setStatus(id, 'idle');
  }
}

export async function refresh(id, reason = 'timer') {
  const entry = registry.get(id);
  if (!entry || !entry.on || !entry.spec.refresh) return;
  const map = getMap();
  if (entry.spec.minzoom != null && map.getZoom() < entry.spec.minzoom) {
    setStatus(id, 'warn', `Zoom in to see this layer (from zoom ${entry.spec.minzoom}).`);
    return;
  }
  if (entry.busy && reason === 'timer') return;
  entry.abort?.abort();
  const controller = new AbortController();
  entry.abort = controller;
  entry.busy = true;
  if ((entry.status !== 'ok' && entry.status !== 'warn') || reason !== 'timer') setStatus(id, 'loading');
  try {
    const c = map.getCenter();
    await entry.spec.refresh({ bounds: viewBounds(), zoom: map.getZoom(), center: { lat: c.lat, lon: c.lng }, signal: controller.signal, reason, options: { ...entry.options } });
    if (!controller.signal.aborted && entry.on && (entry.status === 'loading' || entry.status === 'idle')) setStatus(id, 'ok');
  } catch (err) {
    if (err?.name === 'AbortError' || controller.signal.aborted) return;
    console.warn(`layer ${id} refresh failed`, err);
    setStatus(id, 'error', err?.message || String(err));
  } finally {
    if (entry.abort === controller) { entry.busy = false; entry.abort = null; }
  }
}

function startTimer(entry) {
  stopTimer(entry);
  const ms = entry.spec.refreshMs;
  if (!ms || !entry.spec.refresh) return;
  entry.timer = setInterval(() => {
    if (document.visibilityState === 'visible') refresh(entry.spec.id, 'timer');
  }, Math.max(2000, ms));
}

function stopTimer(entry) {
  if (entry.timer) clearInterval(entry.timer);
  entry.timer = null;
}

export function setCount(id, n) {
  const entry = registry.get(id);
  if (!entry) return;
  entry.count = n;
  updateRow(entry);
  renderGroupCounts();
}

export function setStatus(id, status, message = null) {
  const entry = registry.get(id);
  if (!entry) return;
  entry.status = status;
  entry.message = message;
  updateRow(entry);
}

/** Switch layers off for a while (e.g. during replay) without saving that; resume() restores them. */
export async function suspend(ids) {
  const done = [];
  for (const id of [].concat(ids || [])) {
    const e = registry.get(id);
    if (!e || !e.on) continue;
    suspended.add(id);
    await setOn(id, false, { persist: false });
    done.push(id);
  }
  return done;
}

export async function resume(ids = null) {
  const list = ids ? [].concat(ids).filter(id => suspended.has(id)) : [...suspended];
  // All together: a slow or dead source must not hold back the layers after it in the list.
  for (const id of list) suspended.delete(id);
  const results = await Promise.allSettled(list.map(id => setOn(id, true, { persist: false })));
  results.forEach((r, i) => { if (r.status === 'rejected') console.error(`layer ${list[i]} failed to resume`, r.reason); });
  persistOn();
  return list;
}

export function isSuspended(id) { return suspended.has(id); }

/** Set one option of a layer as if the user changed it (saved; refreshes when on). */
export function setOption(id, optId, value) {
  const entry = registry.get(id);
  if (!entry || !(entry.spec.options || []).some(o => o.id === optId)) return false;
  entry.options[optId] = value;
  saveOptions(entry);
  try { entry.spec.onOption?.(optId, value, { ...entry.options }); } catch (err) { console.error(err); }
  const opt = entry.spec.options.find(o => o.id === optId);
  if (entry.on && entry.spec.refresh && opt.refresh !== false) refresh(id, 'option');
  if (entry.expanded && entry.row) fillDetail(entry, entry.row.detail);
  return true;
}

export function setAllOff() {
  for (const id of order) if (registry.get(id).on) setOn(id, false);
}

// view-dependent layers refresh after the map settles
bus.on('map:moveend', debounce(() => {
  for (const id of order) {
    const e = registry.get(id);
    if (e.on && e.spec.viewDependent) refresh(id, 'view');
  }
}, 250));

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  for (const id of order) {
    const e = registry.get(id);
    if (e.on && e.spec.refreshMs) refresh(id, 'visible');
  }
});

// ------------------------------------------------------------------ panel rendering
export function mountPanel(el, filterInput, offButton) {
  panel = el;
  filterInput.addEventListener('input', debounce(() => { filterText = filterInput.value.trim().toLowerCase(); render(); }, 120));
  offButton.addEventListener('click', () => setAllOff());
  render();
}

const render = debounce(() => {
  if (!panel) return;
  clear(panel);
  for (const g of GROUPS) {
    const ids = order.filter(id => registry.get(id).spec.group === g.id && matches(registry.get(id)));
    if (!ids.length) continue;
    const closed = closedGroups.has(g.id) && !filterText;
    const head = h('button', { class: 'lg-head', type: 'button', 'aria-expanded': String(!closed), style: { '--gc': g.color } },
      h('span', { class: 'lg-rule' }), g.id, h('span', { class: 'lg-count', dataset: { group: g.id } }),
      svgIcon('M5 8l5 5 5-5', { size: 12 }));
    head.querySelector('svg').classList.add('chev');
    const body = h('div', { class: 'lg-body' });
    const section = h('section', { class: `lg${closed ? ' closed' : ''}`, 'aria-label': g.id }, head, body);
    head.addEventListener('click', () => {
      if (closedGroups.has(g.id)) closedGroups.delete(g.id); else closedGroups.add(g.id);
      localStorage.setItem('meridian.layers.closed', JSON.stringify([...closedGroups]));
      section.classList.toggle('closed');
      head.setAttribute('aria-expanded', String(!section.classList.contains('closed')));
    });
    for (const id of ids) body.append(buildRow(registry.get(id), g.color));
    panel.append(section);
  }
  const ungrouped = order.filter(id => !GROUPS.some(g => g.id === registry.get(id).spec.group) && matches(registry.get(id)));
  if (ungrouped.length) {
    const body = h('div', { class: 'lg-body' });
    for (const id of ungrouped) body.append(buildRow(registry.get(id), 'var(--paper-faint)'));
    panel.append(h('section', { class: 'lg' }, h('div', { class: 'lg-head' }, h('span', { class: 'lg-rule' }), 'Other'), body));
  }
  renderGroupCounts();
}, 30);

function matches(entry) {
  if (!filterText) return true;
  const s = entry.spec;
  return [s.title, s.subtitle, s.group, s.description, s.source].filter(Boolean).join(' ').toLowerCase().includes(filterText);
}

function buildRow(entry, groupColour) {
  const s = entry.spec;
  const colour = s.color || groupColour;
  const sym = h('span', { class: 'lr-sym' }, h('i', { class: s.symbol || 'point' }));
  const count = h('span', { class: 'lr-count' });
  const state = h('span', { class: 'lr-state', 'aria-hidden': 'true' });
  const main = h('button', { class: 'lr-main', type: 'button', role: 'switch', 'aria-checked': String(entry.on), title: s.description || s.title },
    sym, h('span', { class: 'lr-title' }, s.title), count, state);
  main.addEventListener('click', () => setOn(s.id, !entry.on));
  const more = h('button', { class: 'lr-more', type: 'button', 'aria-label': `More about ${s.title}`, 'aria-expanded': String(entry.expanded) }, '⋯');
  const detail = h('div', { class: 'lr-detail' });
  more.addEventListener('click', () => {
    entry.expanded = !entry.expanded;
    row.classList.toggle('expanded', entry.expanded);
    more.setAttribute('aria-expanded', String(entry.expanded));
    if (entry.expanded) fillDetail(entry, detail);
  });
  const row = h('div', { class: 'lr', style: { '--lc': colour }, dataset: { layer: s.id } }, main, more, detail);
  entry.row = { row, main, count, state, detail };
  if (entry.expanded) { row.classList.add('expanded'); fillDetail(entry, detail); }
  updateRow(entry);
  return row;
}

function fillDetail(entry, detail) {
  clear(detail);
  const s = entry.spec;
  if (s.subtitle) detail.append(h('p', null, s.subtitle));
  if (s.description) detail.append(h('p', null, s.description));
  if (entry.message) detail.append(h('p', { class: entry.status === 'error' ? 'lr-error' : '' }, entry.message));
  if (s.legend?.length) {
    detail.append(h('div', { class: 'legend' }, s.legend.map(l => h('span', null, h('i', { class: l.shape || '', style: { background: l.color } }), l.label))));
  }
  if (s.options?.length) {
    const box = h('div', { class: 'lr-opts' });
    for (const o of s.options) box.append(optionControl(entry, o));
    detail.append(box);
  }
  if (s.source) detail.append(h('p', { class: 'lr-source' }, `Source: ${s.source}`));
}

function optionControl(entry, o) {
  const id = `opt-${entry.spec.id}-${o.id}`.replace(/[^a-zA-Z0-9_-]/g, '_');
  const value = entry.options[o.id];
  const set = (v) => {
    entry.options[o.id] = v;
    saveOptions(entry);
    try { entry.spec.onOption?.(o.id, v, { ...entry.options }); } catch (err) { console.error(err); }
    if (entry.on && entry.spec.refresh && o.refresh !== false) refresh(entry.spec.id, 'option');
  };
  if (o.type === 'toggle') {
    const input = h('input', { type: 'checkbox', id, checked: !!value || null });
    input.addEventListener('change', () => set(input.checked));
    return h('div', { class: 'lr-opt' }, h('label', { for: id }, o.label), h('span', { class: 'switch' }, input, h('span')));
  }
  if (o.type === 'select') {
    const sel = h('select', { id }, (o.choices || []).map(c => h('option', { value: c.value, selected: String(c.value) === String(value) || null }, c.label)));
    sel.addEventListener('change', () => set(sel.value));
    return h('div', { class: 'lr-opt' }, h('label', { for: id }, o.label), sel);
  }
  if (o.type === 'range') {
    const out = h('span', { class: 'tnum' }, o.format ? o.format(value) : String(value));
    const input = h('input', { type: 'range', id, min: o.min, max: o.max, step: o.step || 1, value });
    input.addEventListener('input', () => { out.textContent = o.format ? o.format(+input.value) : input.value; });
    input.addEventListener('change', () => set(+input.value));
    return h('div', { class: 'lr-opt' }, h('label', { for: id }, o.label), input, out);
  }
  return h('div');
}

function updateRow(entry) {
  if (!entry.row) return;
  const { row, main, count, detail } = entry.row;
  row.classList.toggle('on', entry.on);
  row.classList.toggle('loading', entry.on && entry.status === 'loading');
  row.classList.toggle('error', entry.on && entry.status === 'error');
  row.classList.toggle('warn', entry.on && entry.status === 'warn');
  main.setAttribute('aria-checked', String(entry.on));
  count.textContent = entry.on && entry.count != null ? (typeof entry.count === 'number' ? fmt.int(entry.count) : String(entry.count)) : '';
  main.title = entry.message ? `${entry.spec.title}: ${entry.message}` : (entry.spec.description || entry.spec.title);
  if (entry.expanded) fillDetail(entry, detail);
}

function renderGroupCounts() {
  if (!panel) return;
  for (const el of panel.querySelectorAll('.lg-count')) {
    const g = el.dataset.group;
    const on = order.filter(id => registry.get(id).spec.group === g && registry.get(id).on).length;
    el.textContent = on ? `${on} on` : '';
  }
}

export const layers = { register, get, isOn, list, setOn, refresh, setCount, setStatus, setAllOff, suspend, resume, isSuspended, setOption, groupColor, GROUPS };
