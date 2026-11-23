// node tests/test_timeline_web.mjs : the timeline's replay maths (web/js/layers/timeline/interp.js):
// interpolation between samples, holds and gaps, trails across the antimeridian, event fading, the
// time axis and view boxes. Also run by python -m unittest tests.test_timeline.

import assert from 'node:assert/strict';
import * as I from '../web/js/layers/timeline/interp.js';

let passed = 0;
const failures = [];
async function test(name, fn) {
  try { await fn(); passed++; } catch (err) { failures.push(name); console.log(`FAIL ${name}\n  ${err?.stack || err}`); }
}
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg || ''} ${a} is not within ${tol} of ${b}`);
const T0 = 1795400000;

await test('lowerIndex', () => {
  const s = [[10], [20], [30]];
  assert.equal(I.lowerIndex(s, 5), -1);
  assert.equal(I.lowerIndex(s, 10), 0);
  assert.equal(I.lowerIndex(s, 25), 1);
  assert.equal(I.lowerIndex(s, 30), 2);
  assert.equal(I.lowerIndex(s, 99), 2);
  assert.equal(I.lowerIndex([], 1), -1);
});

await test('angles', () => {
  assert.equal(I.angleDiff(350, 10), 20);
  assert.equal(I.angleDiff(10, 350), -20);
  near(I.lerpAngle(350, 10, 0.5), 0, 1e-9);
  near(I.lerpAngle(90, 270, 0.25), 135, 1e-9);
  assert.equal(I.lerpAngle(null, 40, 0.3), 40);
  assert.equal(I.lerpAngle(40, null, 0.3), 40);
  near(I.bearing(0, 0, 0, 1), 90, 1e-9);
  near(I.bearing(0, 0, 1, 0), 0, 1e-9);
  assert.equal(I.wrapLon(190), -170);
  assert.equal(I.wrapLon(-190), 170);
  assert.equal(I.wrapLon(180), 180);
});

await test('great circle', () => {
  const [lat, lon] = I.greatCircle(0, 0, 0, 90, 0.5);
  near(lat, 0, 1e-9); near(lon, 45, 1e-9);
  const [lat2, lon2] = I.greatCircle(10, 179, 10, -179, 0.5);      // across the antimeridian
  near(Math.abs(lon2), 180, 1e-6); near(lat2, 10.0015, 1e-3);
  const [lat3, lon3] = I.greatCircle(51.47, -0.45, 51.47, -0.45, 0.7);
  assert.deepEqual([lat3, lon3], [51.47, -0.45]);
  // London to New York: the midpoint lies north of both ends (a great circle, not a rhumb line).
  const [mlat] = I.greatCircle(51.47, -0.45, 40.64, -73.78, 0.5);
  assert.ok(mlat > 51.47, `midpoint latitude ${mlat}`);
  near(I.haversineKm(51.47, -0.4543, 40.6413, -73.7781), 5540, 15);
});

const air = { s: [[T0, 50, 0, 30000, 90, 0], [T0 + 300, 50, 1, 31000, 90, 0], [T0 + 2400, 50, 5, 32000, 95, 4]] };

await test('aircraft between samples, exact, held and gone', () => {
  assert.equal(I.positionAt(air, T0 - 1, I.AIR), null);                     // before it was seen
  const exact = I.positionAt(air, T0, I.AIR);
  assert.equal(exact.state, 'exact');
  assert.deepEqual([exact.lat, exact.lon, exact.a], [50, 0, 30000]);
  const mid = I.positionAt(air, T0 + 150, I.AIR, true);
  assert.equal(I.positionAt(air, T0 + 150, I.AIR).speedKn, null);           // only cards ask for speed
  assert.equal(mid.state, 'between');
  near(mid.lon, 0.5, 1e-3); near(mid.lat, 50, 1e-9);                       // close samples: a straight line
  const far = I.positionAt({ s: [[T0, 50, 0, 0, 90, 0], [T0 + 600, 50, 4, 0, 90, 0]] }, T0 + 300, I.AIR);
  near(far.lat, I.greatCircle(50, 0, 50, 4, 0.5)[0], 1e-9);              // distant ones: the great circle
  assert.ok(far.lat > 50.01);
  near(mid.a, 30500, 1e-9); near(mid.dir, 90, 1e-9);
  near(mid.speedKn, I.haversineKm(50, 0, 50, 1) / (300 / 3600) / 1.852, 1e-6);
  // 300 s to 2400 s is a 35 min gap: no interpolation across it; held for 2 min, then gone.
  const held = I.positionAt(air, T0 + 300 + 60, I.AIR, true);
  assert.equal(held.state, 'held');
  assert.equal(held.age, 60);
  assert.equal(held.lon, 1);
  near(held.speedKn, mid.speedKn, 1e-9);                                     // from the previous pair
  assert.equal(I.positionAt(air, T0 + 300 + 121, I.AIR), null);
  assert.equal(I.positionAt(air, T0 + 2400 + 121, I.AIR), null);
  assert.equal(I.positionAt(air, T0 + 2400, I.AIR).flags, 4);
  assert.equal(I.positionAt({ s: [] }, T0, I.AIR), null);
});

await test('vessels hold like the live map, stationary ones longer', () => {
  const moving = { s: [[T0, 60, 24, 12, 90, 0]] };
  assert.equal(I.positionAt(moving, T0 + 3000, I.SEA).state, 'held');
  assert.ok(I.positionAt(moving, T0 + 1000, I.SEA).stale);                    // after 15 min
  assert.equal(I.positionAt(moving, T0 + 3700, I.SEA), null);                 // after 1 h
  const still = { s: [[T0, 60, 24, 0.1, null, 0], [T0 + 7200, 60.0001, 24, 0, null, 0]] };
  const p = I.positionAt(still, T0 + 3600, I.SEA);
  assert.equal(p.state, 'between');                                            // 2 h apart, both stationary
  assert.equal(p.dir, null);                                                   // no heading for a still vessel
  assert.equal(I.positionAt({ s: [[T0, 60, 24, 0, null, 0]] }, T0 + 5 * 3600, I.SEA).state, 'held');
  assert.equal(I.positionAt({ s: [[T0, 60, 24, 0, null, 0]] }, T0 + 7 * 3600, I.SEA), null);
  const gap = { s: [[T0, 60, 24, 12, 90, 0], [T0 + 2 * 3600, 60, 26, 12, 90, 0]] };   // moving, 2 h apart
  assert.equal(I.positionAt(gap, T0 + 1800, I.SEA).state, 'held');
  assert.equal(I.positionAt(gap, T0 + 5400, I.SEA), null);
  const noCourse = { s: [[T0, 60, 24, 12, null, 0], [T0 + 600, 60, 24.1, 12, null, 0]] };
  near(I.positionAt(noCourse, T0 + 300, I.SEA).dir, 90, 0.1);                 // bearing between samples
});

await test('trails', () => {
  const pos = I.positionAt(air, T0 + 150, I.AIR);
  const line = I.trail(air, T0 + 150, 600, pos);
  assert.deepEqual(line[0], [0, 50]);
  near(line[1][0], 0.5, 1e-3);
  assert.equal(line.length, 2);
  assert.equal(I.trail(air, T0, 600, I.positionAt(air, T0, I.AIR)), null);       // one point is no line
  const dateline = { s: [[T0, 10, 179.6, 0, 90, 0], [T0 + 60, 10, 179.9, 0, 90, 0], [T0 + 120, 10, -179.8, 0, 90, 0]] };
  const p2 = I.positionAt(dateline, T0 + 150, I.AIR);
  const l2 = I.trail(dateline, T0 + 150, 600, p2);
  assert.deepEqual(l2.map(c => +c[0].toFixed(1)), [179.6, 179.9, 180.2]);         // continuous, not wrapped
  const short = I.trail(air, T0 + 2400 + 30, 10, I.positionAt(air, T0 + 2400 + 30, I.AIR));
  assert.equal(short, null);
});

await test('events fade in, stay while active and fade out by severity', () => {
  const quake = { t0: T0, t1: T0, sev: 7 };
  assert.equal(I.eventAlpha(quake, T0 - 1), 0);
  near(I.eventAlpha(quake, T0), 0.3, 1e-9);
  near(I.eventAlpha(quake, T0 + I.EVENT_FADE_IN), 1, 1e-9);
  near(I.eventAlpha(quake, T0 + I.EVENT_FADE_IN + 3 * 3600), 0.5, 1e-9);   // 6 h linger at severity 7
  assert.equal(I.eventAlpha(quake, T0 + I.EVENT_FADE_IN + 6 * 3600 + 1), 0);
  const storm = { t0: T0, t1: T0 + 86400, sev: 2 };
  assert.equal(I.eventAlpha(storm, T0 + 43200), 1);
  near(I.eventAlpha(storm, T0 + 86400 + 1800), 0.5, 1e-9);                 // 1 h linger when routine
  assert.equal(I.linger(9.5), 12 * 3600);
  assert.equal(I.linger(3), 3 * 3600);
  assert.deepEqual([0, 2.9, 3, 5.9, 6, 8.9, 9, 10].map(I.severityLevel),
    ['routine', 'routine', 'notable', 'notable', 'serious', 'serious', 'extreme', 'extreme']);
});

await test('time axis and labels', () => {
  const start = Date.UTC(2026, 10, 21, 18) / 1000;
  const end = start + 72 * 3600;
  const tk = I.ticks(start, end, 1200);
  assert.ok(tk.length >= 8 && tk.length <= 24, `${tk.length} ticks`);
  assert.ok(tk.every(t => t.t % 3600 === 0 && t.x >= 0 && t.x <= 1200));
  const days = tk.filter(t => t.day).map(t => t.label);
  assert.deepEqual(days, ['Sun 22', 'Mon 23', 'Tue 24']);
  assert.equal(tk.find(t => !t.day).label.length, 5);
  assert.ok(I.ticks(start, start + 3600, 1200).length >= 1);
  assert.deepEqual(I.ticks(end, start, 100), []);
  assert.equal(I.clockText(Date.UTC(2026, 10, 23, 14, 5, 30) / 1000, true), '14:05:30');
  assert.equal(I.dateText(Date.UTC(2026, 10, 23, 14) / 1000), 'Mon 23 Nov');
  const now = Date.UTC(2026, 10, 23, 18) / 1000;
  assert.equal(I.whenText(Date.UTC(2026, 10, 23, 14, 5) / 1000, now), '14:05 UTC');
  assert.equal(I.whenText(Date.UTC(2026, 10, 22, 14, 5) / 1000, now), 'Sun 22 Nov, 14:05 UTC');
  assert.equal(I.spanText(95), '1 min 35 s');
  assert.equal(I.spanText(4000), '1 h 7 min');
  assert.equal(I.spanText(7200), '2 h');
});

await test('chunks and boxes', () => {
  assert.deepEqual(I.chunksFor(1000, 2800, 900), [900, 1800, 2700]);
  assert.deepEqual(I.chunksFor(900, 900, 900), [900]);
  assert.ok(I.boxContains([0, 0, 10, 10], [1, 1, 9, 9]));
  assert.ok(!I.boxContains([0, 0, 10, 10], [1, 1, 11, 9]));
  assert.ok(I.boxContains([170, 0, -170, 10], [175, 1, -175, 9]));          // across the antimeridian
  assert.ok(!I.boxContains([170, 0, -170, 10], [10, 1, 20, 9]));
  assert.ok(I.boxContains([-180, -85, 180, 85], [170, 1, -175, 9]));        // the world holds everything
  assert.deepEqual(I.padBox([0, 0, 10, 10], 0.25), [-2.5, -2.5, 12.5, 12.5]);
  assert.deepEqual(I.padBox([-170, 0, 170, 10]), [-180, -2.5, 180, 12.5]);
  const p = I.padBox([175, 0, -175, 10], 0.25);
  assert.deepEqual(p, [172.5, -2.5, -172.5, 12.5]);
});

console.log(`${passed} passed, ${failures.length} failed`);
if (failures.length) process.exit(1);
