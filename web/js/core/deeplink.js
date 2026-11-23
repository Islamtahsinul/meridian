// Deep links: the URL hash carries the view, the layers that are on and one open entity, so a link
// opens Meridian as it was (format in docs/modules/workspace.md).
//
//   #map=8/57.6/11.2[/bearing/pitch]&l=ships,ships.straits&e=strait:ships:danish_straits
//
// map= is MapLibre's own key (hash: 'map' in map.js): it keeps it current and restores it at start-up.
// This file keeps l= (layer ids, comma-separated; an empty value means no layers) and e= (kind:id of
// the topmost card; the kind ends at the first colon, ids may contain more) in step with the page, and
// applies them when the page opens with a link or when the hash changes.
//
// Rules:
// - Every value is validated. Layer ids must be registered (at most 32 per link); a list with no known
//   id is ignored, so only an explicit empty "l=" turns everything off. The entity kind must have a
//   card handler and its id is at most 120 characters from [A-Za-z0-9:._~@+,=-]. The rest is ignored.
// - A link never sends a POST, never runs code and never builds HTML: it turns layers on and off as
//   the user could and opens one card (without flying when the link also carries map=).
// - A link's layers are for this tab only (persist: false): the saved layer set changes only when the
//   user switches a layer. A link that would turn on more than 8 layers (the assistant's limit for one
//   set_layers action, app.js) asks first.
// - Reloading a tab doesn't re-apply the layers its own hash carries (sessionStorage remembers what this
//   tab wrote), so the saved layer set, and whatever the Timeline restores, wins as before.
// - A link that changes the layers shows a toast with Undo.
//
// The pure helpers are exported for the Node tests (tests/fixtures/workspace/deeplink_test.mjs);
// app.js calls init() before the modules load and start() once they have registered.

// layers: ids a link may carry; turnOn: layers a link may switch on without asking (as set_layers in app.js).
export const LIMITS = Object.freeze({ layers: 32, turnOn: 8, idLength: 120, hashLength: 8192 });

const LAYER_RE = /^[A-Za-z0-9_.:-]{1,80}$/;
const KIND_RE = /^[a-z][a-z0-9_]{1,39}$/;
const ID_RE = /^[A-Za-z0-9:._~@+,=-]{1,120}$/;
const SELF_KEY = 'meridian.deeplink.self';   // sessionStorage: the l= and e= this tab last wrote
const LAYERS_KEY = 'meridian.layers.on';     // layers.js restores the layers on from this key

// ------------------------------------------------------------------ pure helpers
function decode(value) {
  try { return decodeURIComponent(String(value)); } catch { return null; }
}

/** location.hash -> ordered [key, rawValue] pairs, split the way MapLibre splits it ('&', then '='). */
export function splitHash(hash) {
  const text = String(hash ?? '').replace(/^#/, '');
  if (!text || text.length > LIMITS.hashLength) return [];
  return text.split('&').filter(Boolean).map((part) => {
    const i = part.indexOf('=');
    return i < 0 ? [part, ''] : [part.slice(0, i), part.slice(i + 1)];
  });
}

export function validLayerId(id) { return typeof id === 'string' && LAYER_RE.test(id); }

export function validEntity(entity) {
  if (!entity || typeof entity !== 'object') return false;
  const id = typeof entity.id === 'number' && Number.isFinite(entity.id) ? String(entity.id) : entity.id;
  return typeof entity.kind === 'string' && KIND_RE.test(entity.kind) && typeof id === 'string' && ID_RE.test(id);
}

/** "ships,ships.straits" -> ['ships', 'ships.straits']; '' -> [] (none on); nothing usable -> null. */
export function parseLayers(raw) {
  const text = decode(raw);
  if (text == null) return null;
  if (text.trim() === '') return [];
  const out = [];
  for (const part of text.split(',')) {
    const id = part.trim();
    if (validLayerId(id) && !out.includes(id)) out.push(id);
    if (out.length >= LIMITS.layers) break;
  }
  return out.length ? out : null;
}

/** "camera:tfl:00001.06514" -> {kind: 'camera', id: 'tfl:00001.06514'} (the kind ends at the first colon). */
export function parseEntity(raw) {
  const text = decode(raw);
  if (!text) return null;
  const i = text.indexOf(':');
  if (i <= 0) return null;
  const entity = { kind: text.slice(0, i), id: text.slice(i + 1) };
  return validEntity(entity) ? entity : null;
}

/** MapLibre's "zoom/lat/lon[/bearing[/pitch]]" -> {zoom, lat, lon, bearing, pitch}, or null. */
export function parseMap(raw) {
  const parts = String(raw ?? '').split('/');
  if (parts.length < 3 || parts.length > 5) return null;
  const n = parts.map(Number);
  if (n.some(v => !Number.isFinite(v)) || parts.some(p => p.trim() === '')) return null;
  const [zoom, lat, lon, bearing = 0, pitch = 0] = n;
  if (zoom < 0 || zoom > 24 || lat < -90 || lat > 90 || lon < -540 || lon > 540) return null;
  if (bearing < -180 || bearing > 180 || pitch < 0 || pitch > 85) return null;
  return { zoom, lat, lon, bearing, pitch };
}

/** Everything a hash says. Duplicate keys: the last one wins (as in MapLibre). */
export function parseHash(hash) {
  const out = { map: null, layers: null, entity: null, rest: [] };
  for (const [key, value] of splitHash(hash)) {
    if (key === 'map') out.map = parseMap(value);
    else if (key === 'l') out.layers = parseLayers(value);
    else if (key === 'e') out.entity = parseEntity(value);
    else if (key) out.rest.push([key, value]);
  }
  return out;
}

export function encodeEntity(entity) {
  if (!validEntity(entity)) return null;
  return encodeURIComponent(`${entity.kind}:${entity.id}`).replace(/%3A/gi, ':').replace(/%2C/gi, ',');
}

export function encodeLayers(ids) {
  const out = [];
  for (const id of ids || []) {
    if (validLayerId(id) && !out.includes(id)) out.push(id);
    if (out.length >= LIMITS.layers) break;
  }
  return out.join(',');
}

function trim(value, decimals) {
  const text = (Math.round(value * 10 ** decimals) / 10 ** decimals).toFixed(Math.max(0, decimals));
  const short = text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text;
  return short === '-0' ? '0' : short;
}

export function wrapLon(lon) { return ((((lon + 180) % 360) + 360) % 360) - 180; }

/** {zoom, lat, lon, bearing?, pitch?} -> MapLibre's own hash value, with its precision rule. */
export function mapPart({ zoom, lat, lon, bearing = 0, pitch = 0 }) {
  const z = Math.round(zoom * 100) / 100;
  const precision = Math.max(0, Math.ceil((z * Math.LN2 + Math.log(512 / 360 / 0.5)) / Math.LN10));
  let out = `${trim(z, 2)}/${trim(lat, precision)}/${trim(wrapLon(lon), precision)}`;
  const b = Math.round(bearing * 10) / 10;
  const p = Math.round(pitch);
  if (b || p) out += `/${trim(b, 1)}`;
  if (p) out += `/${p}`;
  return out;
}

/** {map?, layers?, entity?, rest?} -> "#map=...&l=...&e=..." ('' when there is nothing). */
export function buildHash({ map = null, layers = null, entity = null, rest = [] } = {}) {
  const parts = [];
  if (map) parts.push(`map=${typeof map === 'string' ? map : mapPart(map)}`);
  if (Array.isArray(layers)) parts.push(`l=${encodeLayers(layers)}`);
  const e = entity ? encodeEntity(entity) : null;
  if (e) parts.push(`e=${e}`);
  for (const [key, value] of rest || []) parts.push(value === '' ? key : `${key}=${value}`);
  return parts.length ? `#${parts.join('&')}` : '';
}

/** Put l= and e= into an existing hash, keeping map= and every other key where it was. */
export function withLinkState(hash, { layers = null, entity = null } = {}) {
  const pairs = splitHash(hash).filter(([key]) => key !== 'l' && key !== 'e');
  const mine = [];
  if (Array.isArray(layers)) mine.push(['l', encodeLayers(layers)]);
  const e = entity ? encodeEntity(entity) : null;
  if (e) mine.push(['e', e]);
  const at = pairs.findIndex(([key]) => key === 'map');
  pairs.splice(at < 0 ? pairs.length : at + 1, 0, ...mine);
  const text = pairs.map(([key, value]) => (value === '' && key !== 'l' ? key : `${key}=${value}`)).join('&');
  return text ? `#${text}` : '';
}

/** What identifies a tab's own hash: its l= and e= values. */
export function selfKey(hash) {
  let l = null;
  let e = null;
  for (const [key, value] of splitHash(hash)) {
    if (key === 'l') l = value;
    else if (key === 'e') e = value;
  }
  return JSON.stringify([l, e]);
}

/** Which layers to turn on and off so exactly `wanted` (registered ids only) are on. */
export function layerPlan(wanted, registered, on) {
  const known = new Set(registered);
  const target = (wanted || []).filter(id => known.has(id));
  const isOn = new Set(on);
  const want = new Set(target);
  return { target, turnOn: target.filter(id => !isOn.has(id)), turnOff: [...isOn].filter(id => known.has(id) && !want.has(id)) };
}

// ------------------------------------------------------------------ browser wiring
let deps = null;          // {map, layers, entities, ui, dom, bus}
let incoming = null;      // the hash the page opened with, parsed
let incomingHash = '';
let reload = false;       // this tab wrote that hash itself (a reload)
let previousSaved;        // the saved layer set while the link's layers stand in for it (undefined: they don't)
let standIn = null;       // what was written in its place during start-up (null: nothing to put back)
let started = false;
let timer = null;
const cards = [];         // [{key, entity}] open cards with a usable entity, most recent last

function storage(kind) {
  try { return kind === 'session' ? window.sessionStorage : window.localStorage; } catch { return null; }
}

function currentLayers() {
  return deps.layers.list().filter(l => l.on).map(l => l.id);
}

function currentEntity() {
  return cards.length ? cards[cards.length - 1].entity : null;
}

function writeHash() {
  timer = null;
  if (!started) return;
  const next = withLinkState(window.location.hash, { layers: currentLayers(), entity: currentEntity() });
  if (next !== window.location.hash) {
    try { window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}${next}`); } catch { /* ignore */ }
  }
  try { storage('session')?.setItem(SELF_KEY, selfKey(next)); } catch { /* ignore */ }
}

function schedule() {
  if (!started) return;
  clearTimeout(timer);
  timer = setTimeout(writeHash, 250);
}

// A link's layers are for this tab only: nothing here saves the layer set unless asked to.
async function setExactly(ids, { persist = false } = {}) {
  const plan = layerPlan(ids, deps.layers.list().map(l => l.id), currentLayers());
  // setOn flips the layer at once and then waits for its data, so start them all together: one slow
  // source must not hold up the rest.
  await Promise.allSettled([...plan.turnOff.map(id => deps.layers.setOn(id, false, { persist })),
    ...plan.turnOn.map(id => deps.layers.setOn(id, true, { persist }))]);
  return plan;
}

function savedSet() {
  try { return storage('local')?.getItem(LAYERS_KEY) ?? null; } catch { return null; }
}

/** Put the user's saved layer set back once the link's layers no longer need to stand in for it. */
function putBackSaved() {
  if (standIn == null) return;
  const local = storage('local');
  try {
    // Unless a layer was switched meanwhile: that saved what was on, as switching a layer always does.
    if (local && local.getItem(LAYERS_KEY) === standIn) {
      if (previousSaved == null) local.removeItem(LAYERS_KEY); else local.setItem(LAYERS_KEY, previousSaved);
    }
  } catch { /* ignore */ }
  standIn = null;
}

function offerUndo(before) {
  const now = currentLayers();
  const same = before.length === now.length && before.every(id => now.includes(id));
  if (same) return;
  const saved = savedSet();
  deps.ui.toast('The link changed which layers are on, in this tab only: your saved layers are unchanged.', 'blue', 9000, {
    title: 'Opened a shared view',
    // Undo is for this tab too, unless a layer was switched since (which saved the link's layers).
    action: { text: 'Undo', onClick: () => { setExactly(before, { persist: savedSet() !== saved }).then(schedule); } },
  });
}

function confirmMany(ids) {
  const titles = ids.map(id => deps.layers.get(id)?.spec?.title || id);
  const named = titles.length > 10 ? `${titles.slice(0, 10).join(', ')} and ${titles.length - 10} more` : titles.join(', ');
  return Promise.resolve(deps.ui.confirm(`This link turns on ${ids.length} layers: ${named}. Each one loads its own data. `
    + 'Turn them on in this tab?', { ok: 'Turn them on' })).catch(() => false);
}

/** Show a link's layers in this tab (never saved), asking first when it turns on more than LIMITS.turnOn. */
async function applyLinkLayers(ids) {
  const plan = layerPlan(ids, deps.layers.list().map(l => l.id), currentLayers());
  if (!plan.target.length && ids.length) return;   // none of its layers exist here: l= is ignored
  if (plan.turnOn.length > LIMITS.turnOn && !(await confirmMany(plan.turnOn))) return;
  const before = currentLayers();
  const done = setExactly(plan.target, { persist: false });   // the layers flip at once; their data follows
  offerUndo(before);
  await done;
}

function openEntity(entity, { fly }) {
  if (!entity || !deps.entities.meta(entity.kind)) return;
  const top = currentEntity();
  if (top && top.kind === entity.kind && top.id === entity.id) return;
  Promise.resolve(deps.entities.open({ kind: entity.kind, id: entity.id }, { fly })).catch((err) => console.warn('deep link: the card did not open', err));
}

function onCardOpened({ key, spec } = {}) {
  const i = cards.findIndex(c => c.key === key);
  if (i >= 0) cards.splice(i, 1);
  const e = spec?.entity;
  const entity = e ? { kind: e.kind, id: typeof e.id === 'number' ? String(e.id) : e.id } : null;
  if (entity && validEntity(entity) && deps.entities.meta(entity.kind)) cards.push({ key, entity });
  schedule();
}

function onCardClosed({ key } = {}) {
  const i = cards.findIndex(c => c.key === key);
  if (i >= 0) cards.splice(i, 1);
  schedule();
}

async function onHashChange() {
  if (!started) return;
  const hash = window.location.hash;
  if (selfKey(hash) === storage('session')?.getItem(SELF_KEY)) return;   // only the view moved
  const link = parseHash(hash);
  if (link.layers) await applyLinkLayers(link.layers);
  openEntity(link.entity, { fly: !link.map });
  schedule();
}

/** Called by app.js before any layer registers: read the link the page opened with. */
export function init(dependencies) {
  deps = dependencies;
  incomingHash = window.location.hash;
  incoming = parseHash(incomingHash);
  reload = selfKey(incomingHash) === storage('session')?.getItem(SELF_KEY);
  const local = storage('local');
  if (incoming.layers && !reload && local && incoming.layers.length <= LIMITS.turnOn) {
    // Layers restore themselves from the saved set as they register: let this page start with the link's
    // layers, so nothing outside the link is switched on only to be switched off again. They only stand
    // in for the saved set: it goes back once every module has registered (start), or if the page closes
    // first. A longer link is applied after start-up instead, and may ask first.
    try {
      previousSaved = local.getItem(LAYERS_KEY);
      standIn = JSON.stringify(incoming.layers);
      local.setItem(LAYERS_KEY, standIn);
      window.addEventListener('pagehide', putBackSaved);
    } catch { previousSaved = undefined; standIn = null; }
  }
  window.addEventListener('hashchange', () => { onHashChange().catch(err => console.warn('deep link failed', err)); });
}

/** Called by app.js once every module has registered: finish applying the link, then keep the hash in step. */
export async function start() {
  if (!deps || !incoming || started) return;
  const { bus } = deps;
  bus.on('card:opened', onCardOpened);
  bus.on('card:closed', onCardClosed);
  bus.on('layer:toggled', schedule);
  let pending = null;
  if (previousSaved !== undefined) {
    putBackSaved();
    const registered = deps.layers.list().map(l => l.id);
    let before = [];
    if (previousSaved == null) {
      before = registered.filter(id => deps.layers.get(id)?.spec?.defaultOn);   // a first visit: the defaults
    } else {
      try { before = JSON.parse(previousSaved); } catch { before = []; }
      before = Array.isArray(before) ? before.filter(id => registered.includes(id)) : [];
    }
    const plan = layerPlan(incoming.layers, registered, []);
    if (!plan.target.length && incoming.layers.length) {
      await setExactly(before, { persist: false });   // none of the link's layers exist here: ignore l=
    } else {
      offerUndo(before);
    }
  } else if (incoming.layers && !reload) {
    // A longer link (or no storage): applied now, over the saved set; the page carries on while it asks.
    pending = applyLinkLayers(incoming.layers).catch(err => console.warn('deep link: the layers were not applied', err));
  }
  started = true;
  openEntity(incoming?.entity, { fly: !incoming?.map });
  writeHash();
  if (pending) { await pending; schedule(); }
}

/** An absolute link to the current view: map=, l= and (unless entity: false) e=. */
export function linkFor({ entity = true } = {}) {
  const map = deps.map;
  const c = map.getCenter();
  const hash = buildHash({ map: { zoom: map.getZoom(), lat: c.lat, lon: c.lng, bearing: map.getBearing(), pitch: map.getPitch() },
    layers: currentLayers(), entity: entity ? currentEntity() : null });
  return `${window.location.origin}${window.location.pathname}${hash}`;
}

/** Copy the link to this view (from a click). Falls back to a box to copy from by hand. */
export async function copyLink() {
  const url = linkFor();
  const { ui, dom } = deps;
  const many = currentLayers().length > LIMITS.layers;
  if (await dom.copyText(url)) {
    ui.toast(many ? `Link copied. Only the first ${LIMITS.layers} layers fit in a link.` : 'Link to this view copied.', 'green');
    return url;
  }
  const input = dom.h('input', { type: 'text', value: url, readonly: true, 'aria-label': 'Link to this view' });
  ui.modal({ title: 'Link to this view', body: dom.h('div', { class: 'field' },
    dom.h('label', null, 'The browser did not allow copying. Select the link and copy it.'), input) });
  input.focus();
  input.select();
  return url;
}

export const deeplink = { init, start, linkFor, copyLink, parseHash, buildHash, mapPart, LIMITS };
