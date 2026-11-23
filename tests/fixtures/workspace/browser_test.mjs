// node tests/fixtures/workspace/browser_test.mjs : the Workspaces browser parts that need a page, run
// against a fake ctx, map and window: My places cards (web/js/layers/workspace/places.js), applying a
// workspace and Undo (panel.js), "Export what's on screen" (exporter.js) and the deep-link start-up and
// hash changes (web/js/core/deeplink.js). Run by python -m unittest tests.test_workspace_web.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

let passed = 0;
const failures = [];
async function test(name, fn) {
  try { await fn(); passed++; } catch (err) { failures.push(name); console.log(`FAIL ${name}\n  ${err?.stack || err}`); }
}
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

class FakeStorage {
  constructor(items = {}) { this.items = new Map(Object.entries(items)); }
  getItem(key) { return this.items.has(key) ? this.items.get(key) : null; }
  setItem(key, value) { this.items.set(key, String(value)); }
  removeItem(key) { this.items.delete(key); }
}
function useLocalStorage(store) {
  Object.defineProperty(globalThis, 'localStorage', { value: store, configurable: true, writable: true });
  return store;
}

// ------------------------------------------------------------------ My places (places.js)
const { createPlaces } = await import('../../../web/js/layers/workspace/places.js');
const HOME = { id: 1, name: 'Home', type: 'home', lat: 51.50735, lon: -0.12776, radius_km: 25, zoom: 11, notes: '', alerts_watchlist_id: null };

function placesPage() {
  const seen = { cards: [], flyTo: [], highlight: [], cleared: 0, setOn: [], handler: null, provider: null };
  const ctx = {
    h: () => ({}),
    api: { get: async () => ({ places: [HOME], max: 50 }), post: async () => ({}) },
    ui: { toast() {}, confirm: async () => false },
    fmt: { num: v => String(v), latlon: (a, b) => `${a}, ${b}` },
    moduleOk: name => name === 'alerts',
    map: { getSource: () => null },
    map2: { addSource() {}, addLayer() {}, removeSource() {}, setData() {}, onClick() {}, onHover() {},
      flyTo: (...a) => seen.flyTo.push(a), highlight: (...a) => seen.highlight.push(a), clearHighlight: () => { seen.cleared++; } },
    layers: { register: () => ({ setCount() {} }), isOn: () => false, setOn: (id, on) => seen.setOn.push([id, on]) },
    cards: { open: (spec) => { seen.cards.push(spec); return { spec }; }, close() {}, get: () => null },
    entities: { handler: (kind, fn) => { seen.handler = { kind, fn }; } },
    search: { provider: (fn) => { seen.provider = fn; } },
  };
  return { ctx, seen, places: createPlaces(ctx, { backend: true }) };
}

await test('a My places card carries no core entity, so no favourite star and no case file', async () => {
  const { seen, places } = placesPage();
  await places.load();
  places.openCard(places.byId(1));
  const spec = seen.cards.at(-1);
  // cards.js adds the star, "Add to case file", "Fly to" and "What's nearby" for any spec.entity
  assert.equal(spec.entity, undefined);
  assert.equal(spec.favPayload, undefined);
  assert.deepEqual(spec.actions.map(a => a.text), ['Fly to', 'Watch in Alerts']);
  assert.ok(!JSON.stringify(spec).includes('casefile'));
  assert.deepEqual(seen.highlight.at(-1), [HOME.lat, HOME.lon, '#ebe6d6']);
  spec.onClose();
  assert.equal(seen.cleared, 1);
});

await test('the card\'s own Fly to shows the layer and flies to the place', async () => {
  const { seen, places } = placesPage();
  await places.load();
  places.openCard(places.byId(1));
  seen.cards.at(-1).actions.find(a => a.text === 'Fly to').onClick();
  assert.deepEqual(seen.flyTo.at(-1), [HOME.lat, HOME.lon, 11]);
  assert.deepEqual(seen.setOn.at(-1), ['workspace.places', true]);
});

await test('deep links and the search box still open a place card', async () => {
  const { seen, places } = placesPage();
  await places.load();
  assert.equal(seen.handler.kind, 'my_place');
  await seen.handler.fn({ kind: 'my_place', id: '1' });
  assert.equal(seen.cards.at(-1).key, 'my_place:1');
  assert.equal(seen.cards.at(-1).entity, undefined);
  const hits = seen.provider('home');
  assert.equal(hits.length, 1);
  assert.deepEqual([hits[0].kind, hits[0].id, hits[0].lat, hits[0].lon], ['my_place', '1', HOME.lat, HOME.lon]);
});

// ------------------------------------------------------------------ applying a workspace (panel.js)
const { createPanel } = await import('../../../web/js/layers/workspace/panel.js');
const SHOW = { id: 'show', type: 'select', choices: [{ value: 'all' }, { value: 'traffic' }], refresh: false };
const TYPES = { id: 'types', type: 'select', choices: [{ value: 'all' }, { value: 'cargo,tanker' }] };

function panelPage(defs) {
  const changed = [];
  const entries = new Map(defs.map(d => [d.id, {
    spec: { id: d.id, title: d.id, options: d.specOptions, onOption: (name, value) => changed.push([d.id, name, value]) },
    on: d.on, options: { ...d.options },
  }]));
  const toasts = [];
  const el = () => ({ append() {}, addEventListener() {}, setAttribute() {}, querySelector: () => null });
  const ctx = {
    h: el, dom: { clear() {}, debounce: fn => fn, download() {} }, api: {}, fmt: {},
    ui: { toast: (message, tone, ms, opts) => toasts.push({ message, ...opts }) },
    settings: { get: (key, def) => def, set() {} },
    layers: {
      list: () => [...entries.values()].map(e => ({ id: e.spec.id, on: e.on, title: e.spec.title, group: 'Test' })),
      get: id => entries.get(id) || null,
      setOn: async (id, on) => { entries.get(id).on = !!on; },
      isOn: id => !!entries.get(id)?.on,
      refresh() {},
    },
    map: { getCenter: () => ({ lng: -2.6, lat: 54.3 }), getZoom: () => 5.4, getBearing: () => 0, getPitch: () => 0, jumpTo() {}, flyTo() {} },
    map2: { viewState: () => ({ basemap: 'dark', projection: 'mercator', terrain: false }), setBasemap() {}, setProjection() {},
      setTerrain() {}, reducedMotion: () => true },
    bus: { on: () => () => {} },
  };
  const panel = createPanel(ctx, { backend: false, places: { state: { places: [] }, on: () => () => {} } });
  return { panel, entries, toasts, changed };
}

await test('Undo puts back the options a workspace set on a layer that was off', async () => {
  const store = useLocalStorage(new FakeStorage());
  const { panel, entries, toasts } = panelPage([
    { id: 'cameras', on: false, options: { show: 'all', cones: true, labels: true }, specOptions: [SHOW] },
  ]);
  await panel.apply({ v: 1, layers: { cameras: { on: true, options: { show: 'traffic' } } } }, { name: 'UK weather day' });
  assert.equal(entries.get('cameras').on, true);
  assert.equal(entries.get('cameras').options.show, 'traffic');
  assert.equal(JSON.parse(store.getItem('meridian.layers.opt.cameras')).show, 'traffic');
  await toasts.at(-1).action.onClick();
  assert.equal(entries.get('cameras').on, false);
  assert.deepEqual(entries.get('cameras').options, { show: 'all', cones: true, labels: true });
  assert.equal(store.getItem('meridian.layers.opt.cameras'), null, 'nothing was stored before, so nothing is stored now');
});

await test('Undo also puts back options on a layer that stays on, and what the core had stored', async () => {
  const store = useLocalStorage(new FakeStorage({ 'meridian.layers.opt.ships': '{"types":"all"}' }));
  const { panel, entries, toasts, changed } = panelPage([
    { id: 'ships', on: true, options: { types: 'all', labels: true }, specOptions: [TYPES] },
    { id: 'cameras', on: false, options: { show: 'all' }, specOptions: [SHOW] },
  ]);
  await panel.apply({ v: 1, layers: { ships: { on: true, options: { types: 'cargo,tanker' } }, cameras: { on: true, options: { show: 'traffic' } } } });
  assert.deepEqual(changed, [['ships', 'types', 'cargo,tanker']]);
  await toasts.at(-1).action.onClick();
  assert.equal(entries.get('ships').on, true);
  assert.equal(entries.get('ships').options.types, 'all');
  assert.deepEqual(changed.at(-1), ['ships', 'types', 'all'], 'the layer was told, as the layer row does');
  assert.equal(store.getItem('meridian.layers.opt.ships'), '{"types":"all"}');
  assert.equal(entries.get('cameras').options.show, 'all');
  assert.equal(store.getItem('meridian.layers.opt.cameras'), null);
});

// ------------------------------------------------------------------ export what's on screen (exporter.js)
const E = await import('../../../web/js/layers/workspace/exporter.js');
const CAMS = [['tfl:00001.07458', -0.1311, 51.5126], ['tfl:00001.07551', -0.1302, 51.5119], ['tfl:00001.07550', -0.1288, 51.5111],
  ['tfl:00001.02500', -0.1247, 51.5108], ['tfl:00001.06590', -0.1231, 51.5122], ['tfl:00001.03711', -0.1219, 51.5099],
  ['tfl:00001.04412', -0.1203, 51.5131]];
const fc = features => ({ type: 'FeatureCollection', features });
const POINTS = fc(CAMS.map(([id, lon, lat], i) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [lon, lat] }, properties: { id, h: 10 * i, g: 'u' } })));
const NAMES = fc(CAMS.slice(0, 5).map(([id, lon, lat]) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [lon, lat] }, properties: { id, n: `Camera ${id.slice(-4)}`, a: 'London' } })));

function exportPage() {
  const style = { 'ov-cameras-clusters': { type: 'circle', source: 'ov-cameras' }, 'ov-cameras-points': { type: 'symbol', source: 'ov-cameras' },
    'ov-cameras-labels': { type: 'symbol', source: 'ov-cameras-names' } };
  const sources = { 'ov-cameras': { type: 'geojson', getData: async () => POINTS }, 'ov-cameras-names': { type: 'geojson', getData: async () => NAMES } };
  const drawn = (data, source) => data.features.map(f => ({ source, properties: { ...f.properties }, geometry: f.geometry,
    toJSON: () => ({ type: 'Feature', geometry: f.geometry, properties: { ...f.properties } }) }));
  const files = [];
  const ctx = {
    map: {
      getStyle: () => ({ layers: Object.keys(style).map(id => ({ id })) }),
      getLayer: id => style[id] || null,
      // MapLibre lists the topmost map layer first: the street-zoom names, then the camera points
      queryRenderedFeatures: () => [...drawn(NAMES, 'ov-cameras-names'), ...drawn(POINTS, 'ov-cameras')],
      getSource: id => sources[id] || null,
      getZoom: () => 15.6,
    },
    layers: { list: () => [{ id: 'cameras' }], get: id => (id === 'cameras' ? { on: true, spec: { title: 'Public cameras', color: '#efe3c2', source: 'TfL' } } : null) },
    moduleOk: () => false,
    api: { get: async () => { throw new Error('offline'); } },
    dom: { download: (name, text) => files.push({ name, text }) },
  };
  return { ctx, files };
}

await test('a camera drawn as a point and as a street-zoom name is exported once', async () => {
  const { ctx, files } = exportPage();
  const got = await E.collect(ctx, 'cameras');
  const ids = got.features.map(f => f.properties.id);
  assert.equal(ids.length, 7);
  assert.equal(new Set(ids).size, 7);
  assert.deepEqual(got.sources, ['ov-cameras', 'ov-cameras-names'], 'the points source is read first');
  assert.ok(got.features.every(f => typeof f.properties.h === 'number'), 'the camera points are the ones kept');
  assert.equal(E.quickCount(ctx, 'cameras').count, 7);
  const csv = await E.exportLayer(ctx, 'cameras', 'csv');
  const geo = await E.exportLayer(ctx, 'cameras', 'geojson');
  assert.equal(csv.count, 7);
  assert.equal(files[0].text.charCodeAt(0), 0xfeff);
  const rows = files[0].text.slice(1).split('\r\n').slice(1).filter(Boolean);
  assert.equal(rows.length, 7);
  assert.equal(new Set(rows.map(r => r.split(',')[2])).size, 7);
  assert.equal(geo.count, 7);
  assert.equal(JSON.parse(files[1].text).features.length, 7);
});

// ------------------------------------------------------------------ deep links (deeplink.js)
const KEY = 'meridian.layers.on';
const SPECS = [{ id: 'cameras', title: 'Public cameras', defaultOn: true }, { id: 'flights', title: 'Aircraft', defaultOn: true },
  { id: 'hazards.quakes', title: 'Earthquakes', defaultOn: true }, { id: 'ships', title: 'Ships', defaultOn: true },
  ...Array.from({ length: 10 }, (_, i) => ({ id: `extra.l${i}`, title: `Extra ${i}` }))];
let instance = 0;

/** A page: window, storage, the core's layer registry as layers.js keeps it, toasts and confirmations. */
async function linkPage({ hash = '', saved, session = {} } = {}) {
  const D = await import(`../../../web/js/core/deeplink.js?page=${++instance}`);   // a fresh module per page
  const local = new FakeStorage(saved === undefined ? {} : { [KEY]: saved });
  const listeners = {};
  const win = {
    location: { hash, pathname: '/', search: '', origin: 'http://127.0.0.1:8790' },
    history: { state: null, replaceState(_state, _title, url) { win.location.hash = url.includes('#') ? url.slice(url.indexOf('#')) : ''; } },
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
    removeEventListener: (type, fn) => { listeners[type] = (listeners[type] || []).filter(x => x !== fn); },
    localStorage: local, sessionStorage: new FakeStorage(session),
  };
  globalThis.window = win;
  const reg = new Map();
  const switched = [];
  const layers = {
    // as layers.js: each layer starts as the saved set says (the defaults when nothing is saved), and
    // switching one saves what is on unless persist is false
    register(spec) {
      const raw = local.getItem(KEY);
      reg.set(spec.id, { spec, on: raw != null ? JSON.parse(raw).includes(spec.id) : !!spec.defaultOn });
    },
    list: () => [...reg.values()].map(e => ({ id: e.spec.id, on: e.on, title: e.spec.title })),
    get: id => reg.get(id) || null,
    async setOn(id, on, { persist = true } = {}) {
      const e = reg.get(id);
      if (!e || e.on === !!on) return;
      e.on = !!on;
      switched.push({ id, on: !!on, persist });
      if (persist) local.setItem(KEY, JSON.stringify([...reg.values()].filter(x => x.on).map(x => x.spec.id)));
    },
  };
  const toasts = [];
  const asks = [];
  const deps = {
    map: {}, layers, dom: {}, bus: { on() {} },
    entities: { meta: () => null, open: async () => null },
    ui: { toast: (message, tone, ms, opts) => toasts.push({ message, ...opts }), confirm: message => new Promise(resolve => asks.push({ message, resolve })) },
  };
  const on = () => layers.list().filter(l => l.on).map(l => l.id);
  const fire = async (type) => { for (const fn of listeners[type] || []) fn(); for (let i = 0; i < 5; i++) await tick(); };
  return { D, deps, local, layers, switched, toasts, asks, on, fire, win,
    async open() { D.init(deps); for (const s of SPECS) layers.register(s); await D.start(); } };
}

await test('a link on a first visit shows its layers in this tab and saves nothing', async () => {
  const page = await linkPage({ hash: '#map=5/51/0&l=' });
  page.D.init(page.deps);
  assert.equal(page.local.getItem(KEY), '[]', 'while the modules register, the link stands in for the saved set');
  for (const s of SPECS) page.layers.register(s);
  await page.D.start();
  assert.deepEqual(page.on(), []);
  assert.equal(page.local.getItem(KEY), null, 'there was no saved set, and there still is none');
  assert.equal(page.toasts.at(-1).title, 'Opened a shared view');
  await page.toasts.at(-1).action.onClick();
  await tick();
  assert.deepEqual(page.on(), ['cameras', 'flights', 'hazards.quakes', 'ships']);
  assert.equal(page.local.getItem(KEY), null);
  assert.ok(page.switched.every(s => s.persist === false), JSON.stringify(page.switched));
});

await test('a link never replaces the saved layer set', async () => {
  const page = await linkPage({ hash: '#map=5/51/0&l=flights,hazards.quakes', saved: '["ships"]' });
  await page.open();
  assert.deepEqual(page.on(), ['flights', 'hazards.quakes']);
  assert.equal(page.local.getItem(KEY), '["ships"]');
  await page.toasts.at(-1).action.onClick();
  await tick();
  assert.deepEqual(page.on(), ['ships']);
  assert.equal(page.local.getItem(KEY), '["ships"]');
});

await test('switching a layer after a link saves what is on, as always; Undo then saves the old set', async () => {
  const page = await linkPage({ hash: '#l=flights', saved: '["ships"]' });
  await page.open();
  await page.layers.setOn('cameras', true);   // the user, from the layer panel
  assert.deepEqual(JSON.parse(page.local.getItem(KEY)), ['cameras', 'flights']);
  await page.toasts.at(-1).action.onClick();
  await tick();
  assert.deepEqual(page.on(), ['ships']);
  assert.deepEqual(JSON.parse(page.local.getItem(KEY)), ['ships']);
});

await test('closing the page while it starts puts the saved set back', async () => {
  const page = await linkPage({ hash: '#l=flights', saved: '["ships"]' });
  page.D.init(page.deps);
  assert.equal(page.local.getItem(KEY), '["flights"]');
  await page.fire('pagehide');
  assert.equal(page.local.getItem(KEY), '["ships"]');
});

await test('a link pasted into an open tab is not saved either', async () => {
  const page = await linkPage({ hash: '#map=5/51/0', saved: '["ships"]' });
  await page.open();
  page.win.location.hash = '#map=5/51/0&l=cameras,flights';
  await page.fire('hashchange');
  assert.deepEqual(page.on(), ['cameras', 'flights']);
  assert.equal(page.local.getItem(KEY), '["ships"]');
  assert.ok(page.switched.every(s => s.persist === false));
});

await test('a link that turns on more than 8 layers asks first', async () => {
  const many = SPECS.slice(4).map(s => s.id).join(',');   // ten layers that are off
  const page = await linkPage({ hash: '#map=5/51/0', saved: '["ships"]' });
  await page.open();
  page.win.location.hash = `#map=5/51/0&l=${many}`;
  await page.fire('hashchange');
  assert.equal(page.asks.length, 1);
  assert.ok(page.asks[0].message.startsWith('This link turns on 10 layers: Extra 0, Extra 1'), page.asks[0].message);
  assert.deepEqual(page.on(), ['ships'], 'nothing changes before the answer');
  page.asks[0].resolve(false);
  for (let i = 0; i < 5; i++) await tick();
  assert.deepEqual(page.on(), ['ships']);
  page.win.location.hash = `#map=5/51/0&l=ships,${many}`;
  await page.fire('hashchange');
  page.asks[1].resolve(true);
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(page.on().length, 11);
  assert.equal(page.local.getItem(KEY), '["ships"]');
  const few = await linkPage({ hash: '#map=5/51/0' });
  await few.open();
  few.win.location.hash = `#map=5/51/0&l=${SPECS.slice(4, 12).map(s => s.id).join(',')}`;   // eight
  await few.fire('hashchange');
  assert.equal(few.asks.length, 0);
  assert.equal(few.on().length, 8);
});

await test('a long link at start-up leaves the saved set alone and asks after the modules load', async () => {
  const many = SPECS.slice(3).map(s => s.id).join(',');   // ships and ten more
  const page = await linkPage({ hash: `#l=${many}`, saved: '["cameras"]' });
  page.D.init(page.deps);
  assert.equal(page.local.getItem(KEY), '["cameras"]');
  for (const s of SPECS) page.layers.register(s);
  const started = page.D.start();
  await tick();
  assert.deepEqual(page.on(), ['cameras']);
  assert.equal(page.asks.length, 1);
  page.asks[0].resolve(true);
  await started;
  assert.equal(page.on().length, 11);
  assert.equal(page.local.getItem(KEY), '["cameras"]');
});

await test('a reload keeps the saved set (the tab wrote that hash itself)', async () => {
  const page = await linkPage({ hash: '#l=flights', saved: '["ships"]', session: { 'meridian.deeplink.self': JSON.stringify(['flights', null]) } });
  await page.open();
  assert.deepEqual(page.on(), ['ships']);
  assert.equal(page.toasts.length, 0);
});

await test('the turn-on limit is the assistant\'s set_layers limit (app.js)', async () => {
  const { LIMITS } = await import('../../../web/js/core/deeplink.js');
  const app = readFileSync(new URL('../../../web/js/core/app.js', import.meta.url), 'utf8');
  assert.equal(LIMITS.turnOn, 8);
  assert.ok(app.includes(`known(on, ${LIMITS.turnOn})`), 'app.js set_layers turns on at most LIMITS.turnOn layers');
  const bridge = readFileSync(new URL('../../../tools/mcp_meridian.py', import.meta.url), 'utf8');
  assert.equal(Number(/^LINK_TURN_ON = (\d+)/m.exec(bridge)?.[1]), LIMITS.turnOn, 'map_link tells Claude the same limit');
  assert.equal(Number(/^MAX_LINK_LAYERS = (\d+)/m.exec(bridge)?.[1]), LIMITS.layers);
});

console.log(`${failures.length ? 'not ok' : 'ok'} workspace browser parts: ${passed} tests passed${failures.length ? `, ${failures.length} failed: ${failures.join(', ')}` : ''}`);
process.exit(failures.length ? 1 : 0);
