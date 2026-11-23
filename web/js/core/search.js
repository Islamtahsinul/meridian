// Universal search (CONTRACT.md section 10.10). Type-ahead covers loaded data (client providers) and
// every module's server search; Enter adds place results from Nominatim (their policy forbids type-ahead).

import { h, clear, debounce, svgIcon, fmt } from './dom.js';
import { api } from './api.js';
import { entities } from './entities.js';
import { layers, groupColor } from './layers.js';
import { shortcut } from './ui.js';

const providers = [];     // fn(q) -> entities[] | Promise
let input = null;
let box = null;
let results = [];
let selected = -1;
let seq = 0;

/** provider(fn): fn(q) returns entities ({kind, id, title, subtitle, lat, lon, layer, score}) */
export function provider(fn) {
  providers.push(fn);
}

export function mount(el) {
  input = h('input', { type: 'search', placeholder: 'Search places, flights, ships, cameras, ports…', 'aria-label': 'Search',
    autocomplete: 'off', spellcheck: 'false', role: 'combobox', 'aria-expanded': 'false', 'aria-controls': 'search-results', 'aria-autocomplete': 'list' });
  box = h('div', { class: 'search-results', id: 'search-results', role: 'listbox', hidden: true });
  const icon = svgIcon('M8.5 3.5a5 5 0 1 0 0 10 5 5 0 0 0 0-10Zm3.6 8.6 4.4 4.4');
  icon.classList.add('search-icon');
  el.append(icon, input, h('kbd', { 'aria-hidden': 'true' }, '/'), box);
  input.addEventListener('input', debounce(() => run(false), 180));
  input.addEventListener('keydown', onKey);
  input.addEventListener('focus', () => { if (results.length) show(); });
  document.addEventListener('pointerdown', (e) => { if (!el.contains(e.target)) hide(); });
  shortcut('/', 'Search', () => { input.focus(); input.select(); });
}

function onKey(e) {
  if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
  else if (e.key === 'Enter') {
    e.preventDefault();
    if (selected >= 0 && results[selected]) choose(results[selected]);
    else run(true);
  } else if (e.key === 'Escape') { hide(); input.blur(); }
}

function move(d) {
  if (!results.length) return;
  selected = (selected + d + results.length) % results.length;
  renderSelection();
}

async function run(withPlaces) {
  const q = input.value.trim();
  const my = ++seq;
  if (q.length < 2) { results = []; hide(); return; }
  const local = [];
  for (const p of providers) {
    try {
      const r = await p(q);
      if (Array.isArray(r)) local.push(...r);
    } catch (err) { console.warn('search provider failed', err); }
  }
  let server = [];
  try { server = (await api.get('/api/core/search', { q, limit: 30 })).results || []; } catch { /* offline */ }
  let places = [];
  if (withPlaces) {
    try { places = (await api.get('/api/core/geocode', { q })).results || []; } catch { places = []; }
  }
  if (my !== seq) return;
  const seen = new Set();
  const merged = [];
  for (const item of [...local.sort((a, b) => (b.score || 0) - (a.score || 0)), ...server, ...places]) {
    const key = `${item.kind}:${item.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(item);
  }
  results = merged.slice(0, 40);
  selected = results.length ? 0 : -1;
  render(q, withPlaces);
}

function label(kind) {
  return ({ place: 'Places', aircraft: 'Aircraft', ship: 'Ships', camera: 'Cameras', airport: 'Airports', port: 'Ports',
    power_plant: 'Power plants', dam: 'Dams', datacenter: 'Data centres', cable: 'Submarine cables', company: 'Companies',
    country: 'Countries', sanctioned_vessel: 'Sanctioned vessels', sanctioned_aircraft: 'Sanctioned aircraft',
    satellite: 'Satellites', launch: 'Launches', earthquake: 'Earthquakes', volcano: 'Volcanoes', news: 'News',
    conflict_event: 'Conflict events' })[kind] || (kind ? kind.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()) : 'Other');
}

function render(q, withPlaces) {
  clear(box);
  if (!results.length) {
    box.append(h('div', { class: 'search-empty' }, withPlaces ? `No matches for “${q}”.` : 'No matches in loaded data yet. Press Enter to search places.'));
    show();
    return;
  }
  let lastKind = null;
  results.forEach((r, i) => {
    if (r.kind !== lastKind) { box.append(h('div', { class: 'search-group' }, label(r.kind))); lastKind = r.kind; }
    const colour = r.layer ? groupColor(layers.get(r.layer)?.spec.group) : (r.kind === 'place' ? 'var(--paper)' : 'var(--paper-faint)');
    const row = h('div', { class: 'search-item', role: 'option', id: `sr-${i}`, 'aria-selected': String(i === selected) },
      h('span', { class: 'sw', style: { background: colour } }),
      h('div', { style: { minWidth: 0 } }, h('div', { class: 't' }, r.title || '(untitled)'), r.subtitle ? h('div', { class: 's' }, r.subtitle) : null),
      r.distance_km != null ? h('span', { class: 's', style: { marginLeft: 'auto' } }, fmt.km(r.distance_km)) : null);
    row.addEventListener('pointerdown', (e) => { e.preventDefault(); choose(r); });
    box.append(row);
  });
  if (!withPlaces) box.append(h('div', { class: 'search-empty' }, 'Press Enter to include places from OpenStreetMap.'));
  show();
}

function renderSelection() {
  box.querySelectorAll('.search-item').forEach((el, i) => el.setAttribute('aria-selected', String(i === selected)));
  const el = box.querySelector(`#sr-${selected}`);
  el?.scrollIntoView({ block: 'nearest' });
  input.setAttribute('aria-activedescendant', el ? el.id : '');
}

function choose(r) {
  hide();
  input.blur();
  entities.open(r);
}

function show() { box.hidden = false; input.setAttribute('aria-expanded', 'true'); }
function hide() { if (box) box.hidden = true; input?.setAttribute('aria-expanded', 'false'); }

export const search = { provider, mount };
