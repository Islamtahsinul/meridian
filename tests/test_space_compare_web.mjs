// node tests/test_space_compare_web.mjs : the compare tool's date pair after a change of imagery source
// (pairForSource in web/js/layers/space/imagery.js). Also run by python -m unittest tests.test_space_compare.

import assert from 'node:assert/strict';
import { pairForSource } from '../web/js/layers/space/imagery.js';

let passed = 0;
const failures = [];
async function test(name, fn) {
  try { await fn(); passed++; } catch (err) { failures.push(name); console.log(`FAIL ${name}\n  ${err?.stack || err}`); }
}
const upTo = latest => d => (d > latest ? { date: latest, clamped: true } : { date: d, clamped: false });
const NO2 = 'Nitrogen dioxide (Sentinel-5P)';

await test('a source that ends before both dates keeps the two sides apart', () => {
  const r = pairForSource('2026-11-15', '2026-11-22', upTo('2026-11-13'), NO2);
  assert.equal(r.after, '2026-11-13');
  assert.equal(r.before, '2026-11-06');
  assert.match(r.note, /^No Nitrogen dioxide \(Sentinel-5P\) imagery after .*13.*Nov.* 2026 yet, so both sides moved back\.$/);
});

await test('only After clamped and still after Before: Before stays, note without "moved back"', () => {
  const r = pairForSource('2026-11-10', '2026-11-22', upTo('2026-11-13'), NO2);
  assert.deepEqual([r.before, r.after], ['2026-11-10', '2026-11-13']);
  assert.ok(r.note && !/moved back/.test(r.note), r.note);
});

await test('After clamped onto Before moves Before back', () => {
  const r = pairForSource('2026-11-13', '2026-11-22', upTo('2026-11-13'), NO2);
  assert.deepEqual([r.before, r.after], ['2026-11-06', '2026-11-13']);
  assert.match(r.note, /moved back/);
});

await test('nothing clamped: dates and the user\'s own choice are left alone, no note', () => {
  assert.deepEqual(pairForSource('2026-11-15', '2026-11-22', upTo('2026-11-22'), 'MODIS Terra (morning)'),
    { before: '2026-11-15', after: '2026-11-22', note: '' });
  assert.deepEqual(pairForSource('2026-11-15', '2026-11-15', upTo('2026-11-22'), 'x'),
    { before: '2026-11-15', after: '2026-11-15', note: '' });
});

await test('swapped dates (After earlier) that clamp together are separated too', () => {
  const r = pairForSource('2026-11-22', '2026-11-20', upTo('2026-11-13'), NO2);
  assert.deepEqual([r.before, r.after], ['2026-11-06', '2026-11-13']);
});

console.log(`${passed} passed, ${failures.length} failed`);
if (failures.length) process.exit(1);
