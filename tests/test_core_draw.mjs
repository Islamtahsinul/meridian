// node tests/test_core_draw.mjs : browser core logic that runs without a browser (CONTRACT 15.3):
// drawing geometry (web/js/core/geometry.js), the replay clock (time.js) and notification rules
// (notify.js, with a stand-in Notification API). Also run by python -m unittest tests.test_core_web.

import assert from 'node:assert/strict';
import * as G from '../web/js/core/geometry.js';
import { bus } from '../web/js/core/bus.js';
import * as T from '../web/js/core/time.js';
import { createNotifier, REPEAT_MS, DESKTOP_PER_MINUTE } from '../web/js/core/notify.js';

let passed = 0;
const failures = [];
async function test(name, fn) {
  try { await fn(); passed++; } catch (err) { failures.push(name); console.log(`FAIL ${name}\n  ${err?.stack || err}`); }
}
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg || ''} ${a} is not within ${tol} of ${b}`);
const closed = (ring) => assert.deepEqual(ring[0], ring[ring.length - 1], 'ring is closed');
const ccw = (ring) => assert.ok(G.signedArea(ring.slice(0, -1)) > 0, 'ring is counter-clockwise');

// ------------------------------------------------------------------ geometry
await test('lon helpers', () => {
  assert.equal(G.wrapLon(190), -170);
  assert.equal(G.wrapLon(-190), 170);
  assert.equal(G.wrapLon(540), -180);
  assert.equal(G.unwrapNear(-175, 170), 185);
  assert.equal(G.unwrapNear(175, -170), -185);
  assert.deepEqual(G.unwrapPath([[170, 0], [-170, 0], [-160, 0]]), [[170, 0], [190, 0], [200, 0]]);
  assert.deepEqual(G.unwrapPath([[370, 0], [372, 1]]), [[10, 0], [12, 1]]);       // a world copy
  const [lon, lat] = G.destination(0, 0, 90, 111.195);
  near(lon, 1, 1e-3); near(lat, 0, 1e-9);
  near(G.haversineKm(51.47, -0.4543, 40.6413, -73.7781), 5540, 15, 'LHR-JFK');
});

await test('circle polygon', () => {
  const f = G.circleFeature([-0.1276, 51.5072], 25);
  assert.equal(f.type, 'Feature');
  assert.equal(f.geometry.type, 'Polygon');
  const ring = f.geometry.coordinates[0];
  closed(ring); ccw(ring);
  assert.equal(ring.length, 73);
  for (const [x, y] of ring) near(G.haversineKm(51.5072, -0.1276, y, x), 25, 0.01, 'vertex on the circle');
  assert.deepEqual(f.properties.center, [-0.1276, 51.5072]);
  assert.equal(f.properties.radius_km, 25);
  assert.equal(f.properties.mode, 'circle');
  near(f.properties.area_km2, Math.PI * 25 * 25, Math.PI * 25 * 25 * 0.01, 'area');
  const [w, s, e, n] = f.bbox;
  assert.ok(w < -0.1276 && e > -0.1276 && s < 51.5072 && n > 51.5072);
  for (const v of ring.flat()) assert.equal(v, Math.round(v * 1e5) / 1e5, '5 decimals');
  assert.ok(G.pointInShape(f, 51.5, -0.12));
  assert.ok(!G.pointInShape(f, 51.9, -0.12));
});

await test('circle across the antimeridian and near a pole', () => {
  const f = G.circleFeature([179.5, 0], 200);
  const lons = f.geometry.coordinates[0].map(p => p[0]);
  assert.ok(Math.max(...lons) > 180, 'continuous longitudes past 180');
  for (let i = 1; i < lons.length; i++) assert.ok(Math.abs(lons[i] - lons[i - 1]) < 10, 'no jump across the ring');
  assert.ok(f.bbox[0] > f.bbox[2], 'bbox crosses: west > east');
  assert.ok(G.pointInShape(f, 0, -179.5));
  assert.ok(G.pointInShape(f, 0, 180));
  assert.ok(!G.pointInShape(f, 0, 170));
  const polar = G.circleFeature([10, 88], 1000);
  assert.ok(polar.properties.radius_km < G.haversineKm(88, 10, 90, 10), 'kept off the pole');
  assert.ok(polar.geometry.coordinates[0].every(([, y]) => y < 90));
  assert.equal(G.circleFeature([10, 10], 0), null);
  assert.equal(G.circleFeature([370, 10], 50).properties.center[0], 10);
  assert.equal(G.circleFeature([0, 0], 1e6).properties.radius_km, G.MAX_RADIUS_KM);
});

await test('rectangle', () => {
  const f = G.rectangleFeature([10, 20], [12, 18]);
  assert.deepEqual(f.geometry.coordinates[0], [[10, 18], [12, 18], [12, 20], [10, 20], [10, 18]]);
  assert.deepEqual(f.bbox, [10, 18, 12, 20]);
  assert.equal(f.properties.mode, 'rectangle');
  const expect = G.haversineKm(19, 10, 19, 12) * G.haversineKm(18, 10, 20, 10);
  near(f.properties.area_km2, expect, expect * 0.02, 'area');
  assert.deepEqual(G.rectangleFeature([12, 18], [10, 20]).geometry, f.geometry, 'corner order does not matter');
  // The shorter way round: 4 degrees across the antimeridian, not 356.
  const x = G.rectangleFeature([178, -1], [-178, 1]);
  assert.deepEqual(x.geometry.coordinates[0], [[178, -1], [182, -1], [182, 1], [178, 1], [178, -1]]);
  assert.deepEqual(x.bbox, [178, -1, -178, 1]);
  assert.ok(G.pointInShape(x, 0, 179.9) && G.pointInShape(x, 0, -179.9) && !G.pointInShape(x, 0, 0));
  assert.deepEqual(G.rectangleFeature([370, 10], [372, 12]).bbox, [10, 10, 12, 12]);
  assert.equal(G.rectangleFeature([5, 5], [5, 5]), null);
  assert.equal(G.rectangleFeature([5, 5], [9, 5]), null);
});

await test('polygon', () => {
  const cw = [[0, 0], [0, 1], [1, 1], [1, 0]];                      // clockwise
  const f = G.polygonFeature(cw);
  const ring = f.geometry.coordinates[0];
  closed(ring); ccw(ring);
  assert.equal(ring.length, 5);
  assert.equal(f.properties.mode, 'polygon');
  near(f.properties.area_km2, 12364, 60, '1 degree square at the equator');
  assert.equal(G.polygonFeature([[0, 0], [1, 1]]), null);
  assert.equal(G.polygonFeature([[0, 0], [1, 1], [2, 2]]), null, 'collinear');
  assert.equal(G.polygonFeature([[0, 0], [0, 0], [1, 0], [1, 0]]), null, 'duplicates removed');
  const dup = G.polygonFeature([[0, 0], [1, 0], [1, 0], [1, 1], [0, 0]]);
  assert.equal(dup.geometry.coordinates[0].length, 4);
  const r = G.polygonFeature([[1.123456789, 2], [3, 2], [3, 4.987654321]]);
  assert.deepEqual(r.geometry.coordinates[0][0], [1.12346, 2]);
  const am = G.polygonFeature([[170, 10], [-170, 10], [-170, -10], [170, -10]]);
  assert.deepEqual(am.bbox, [170, -10, -170, 10]);
  assert.ok(G.pointInShape(am, 0, 180) && G.pointInShape(am, 5, -175) && !G.pointInShape(am, 0, 160));
});

await test('point in shape: holes, multipolygons, bad input', () => {
  const holed = { type: 'Polygon', coordinates: [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]] };
  assert.ok(G.pointInShape(holed, 2, 2));
  assert.ok(!G.pointInShape(holed, 5, 5));
  const multi = { type: 'Feature', geometry: { type: 'MultiPolygon', coordinates: [holed.coordinates, [[[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]]]] } };
  assert.ok(G.pointInShape(multi, 20.5, 20.5));
  assert.ok(!G.pointInShape(null, 0, 0));
  assert.ok(!G.pointInShape(holed, NaN, 0));
  assert.deepEqual(G.labelPoint(G.circleFeature([5, 6], 10)), [5, 6]);
  assert.deepEqual(G.labelPoint(G.rectangleFeature([0, 0], [2, 2])), [1, 1]);
});

// ------------------------------------------------------------------ replay clock
await test('replay clock', () => {
  const seen = [];
  const offMode = bus.on('time:mode', (p) => seen.push(['mode', p.mode]));
  const offChanged = bus.on('time:changed', (p) => seen.push(['changed', p.t, p.mode]));
  assert.equal(T.mode(), 'live');
  near(T.now(), Date.now(), 50);
  const t = Date.now() - 3600_000;
  assert.equal(T.setReplay(t), t);
  assert.equal(T.mode(), 'replay');
  assert.equal(T.now(), t);
  assert.deepEqual(seen, [['mode', 'replay'], ['changed', t, 'replay']]);
  T.setReplay(t);                                     // no move, no events
  assert.equal(seen.length, 2);
  assert.equal(T.setReplay(Math.floor(t / 1000)), Math.floor(t / 1000) * 1000, 'epoch seconds accepted');
  assert.ok(T.setReplay(Date.now() + 86400_000) <= Date.now(), 'the future is clamped to now');
  assert.throws(() => T.setReplay('not a time'), TypeError);
  assert.throws(() => T.setReplay(NaN), TypeError);
  assert.equal(T.mode(), 'replay');
  seen.length = 0;
  T.live();
  assert.equal(T.mode(), 'live');
  assert.equal(seen[0][0], 'mode');
  assert.equal(seen[0][1], 'live');
  assert.equal(seen[1][0], 'changed');
  near(seen[1][1], Date.now(), 50);
  T.live();
  assert.equal(seen.length, 2, 'live twice: no events');
  offMode(); offChanged();
});

// ------------------------------------------------------------------ notifications
function fakeEnv({ permission = 'default', answer = 'granted', focused = false } = {}) {
  const store = new Map();
  const env = {
    clock: 1_800_000_000_000,
    toasts: [], opened: [], focusCalls: 0, focused,
    storage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) },
  };
  class FakeNotification {
    static permission = permission;
    static instances = [];
    static requestPermission() { FakeNotification.permission = answer; return Promise.resolve(answer); }
    constructor(title, opts) { this.title = title; this.opts = opts; this.closed = false; FakeNotification.instances.push(this); }
    close() { this.closed = true; }
  }
  env.Notification = FakeNotification;
  env.notifier = (extra = {}) => createNotifier({
    Notification: FakeNotification, storage: env.storage, now: () => env.clock, focused: () => env.focused,
    toast: (msg, tone, ms, opts) => env.toasts.push({ msg, tone, ms, ...opts }),
    openEntity: (e) => env.opened.push(e), focusWindow: () => { env.focusCalls++; }, ...extra,
  });
  return env;
}

await test('notify: permission and the enabled setting', async () => {
  assert.equal(createNotifier({ Notification: null }).permission(), 'unsupported');
  const env = fakeEnv();
  const n = env.notifier();
  assert.equal(n.permission(), 'default');
  assert.equal(n.enabled(), false);
  const events = [];
  const off = bus.on('notify:changed', (p) => events.push(p));
  assert.equal(await n.request(), 'granted');
  assert.equal(n.enabled(), true, 'on once granted');
  assert.equal(n.setEnabled(false), false);
  assert.equal(n.enabled(), false);
  assert.equal(await n.request(), 'granted', 'asking again just answers');
  assert.equal(n.enabled(), false, 'the user choice sticks');
  n.setEnabled(true);
  assert.deepEqual(events.at(-1), { permission: 'granted', enabled: true });
  off();
  const denied = fakeEnv({ answer: 'denied' }).notifier();
  assert.equal(await denied.request(), 'denied');
  assert.equal(denied.enabled(), false);
  const already = fakeEnv({ permission: 'granted' }).notifier();
  assert.equal(already.enabled(), true, 'granted earlier, never switched off');
});

await test('notify: toast always, desktop when enabled and Meridian is not focused', async () => {
  const env = fakeEnv({ permission: 'granted' });
  const n = env.notifier();
  assert.throws(() => n.send({ body: 'no title' }), TypeError);
  const entity = { kind: 'earthquake', id: 'us1', lat: 1, lon: 2 };
  assert.deepEqual(n.send({ title: 'M6.1 earthquake', body: 'Near Tokyo', tag: 'q:us1', tone: 'red', entity }), { toast: true, desktop: true, suppressed: false });
  const toast = env.toasts.at(-1);
  assert.equal(toast.title, 'M6.1 earthquake');
  assert.equal(toast.msg, 'Near Tokyo');
  assert.equal(toast.tone, 'red');
  toast.action.onClick();
  assert.deepEqual(env.opened.at(-1), entity);
  const desk = env.Notification.instances.at(-1);
  assert.equal(desk.title, 'M6.1 earthquake');
  assert.equal(desk.opts.tag, 'q:us1');
  desk.onclick({ preventDefault() {} });
  assert.equal(env.focusCalls, 1);
  assert.ok(desk.closed);
  assert.equal(env.opened.length, 2);
  env.focused = true;
  assert.deepEqual(n.send({ title: 'Seen in the app' }), { toast: true, desktop: false, suppressed: false });
  env.focused = false;
  n.setEnabled(false);
  assert.equal(n.send({ title: 'Switched off' }).desktop, false);
  assert.equal(env.toasts.at(-1).msg, 'Switched off');
  assert.equal(env.toasts.at(-1).title, null, 'a title alone is the toast text');
  assert.equal(env.toasts.at(-1).action, null, 'no entity, no button');
  assert.equal(n.send({ title: 'Odd tone', tone: '<b>' }).toast, true);
  assert.equal(env.toasts.at(-1).tone, '');
});

await test('notify: a tag never repeats within 10 minutes, across reloads', () => {
  const env = fakeEnv({ permission: 'granted' });
  const n = env.notifier();
  assert.equal(n.send({ title: 'A', tag: 't1' }).suppressed, false);
  env.clock += 60_000;
  assert.deepEqual(n.send({ title: 'A again', tag: 't1' }), { toast: false, desktop: false, suppressed: true });
  assert.equal(n.send({ title: 'B', tag: 't2' }).suppressed, false);
  const reloaded = env.notifier();                     // a new page (or another tab), same storage
  assert.equal(reloaded.send({ title: 'A after reload', tag: 't1' }).suppressed, true);
  env.clock += REPEAT_MS - 60_000;                     // exactly 10 minutes after the first
  assert.equal(reloaded.send({ title: 'A later', tag: 't1' }).suppressed, false);
  assert.equal(n.send({ title: 'no tag' }).suppressed, false);
  assert.equal(n.send({ title: 'no tag' }).suppressed, false, 'untagged notifications always show');
});

await test('notify: at most six desktop notifications a minute', () => {
  const env = fakeEnv({ permission: 'granted' });
  const n = env.notifier();
  const results = [];
  for (let i = 0; i < DESKTOP_PER_MINUTE + 3; i++) { results.push(n.send({ title: `Alert ${i}`, tag: `burst${i}` })); env.clock += 1000; }
  assert.equal(results.filter(r => r.desktop).length, DESKTOP_PER_MINUTE);
  assert.equal(results.filter(r => r.toast).length, DESKTOP_PER_MINUTE + 3, 'every one is toasted');
  env.clock += 60_000;
  assert.equal(n.send({ title: 'Later', tag: 'later' }).desktop, true);
});

await test('notify: a broken Notification constructor still toasts', () => {
  const env = fakeEnv({ permission: 'granted' });
  class Throwing { static permission = 'granted'; constructor() { throw new Error('needs a service worker'); } }
  const n = env.notifier({ Notification: Throwing });
  const warn = console.warn;
  console.warn = () => {};
  try { assert.deepEqual(n.send({ title: 'Still here' }), { toast: true, desktop: false, suppressed: false }); } finally { console.warn = warn; }
});

console.log(failures.length ? `${failures.length} failed: ${failures.join(', ')}` : `ok ${passed} tests`);
if (failures.length) process.exit(1);
