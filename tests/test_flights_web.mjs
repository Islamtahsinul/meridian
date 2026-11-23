// node tests/test_flights_web.mjs : small browser helpers of the flights layers (delays row text and
// block dates, CZIB block dates). Also run by python -m unittest tests.test_flights_web.

import assert from 'node:assert/strict';
import { statusMessage, when } from '../web/js/layers/flights/delays.js';
import { buildSpec as czibSpec } from '../web/js/layers/flights/advisories.js';
import { utcClock } from '../web/js/layers/flights/style.js';

let passed = 0;
const failures = [];
async function test(name, fn) {
  try { await fn(); passed++; } catch (err) { failures.push(name); console.log(`FAIL ${name}\n  ${err?.stack || err}`); }
}
const ts = iso => Date.parse(iso) / 1000;
const feat = (type, delay) => ({ properties: { type, delay } });

await test('delays row names the airports with notices only', () => {
  const m = { airports: 4, delayed: 1, counts: { partial_closure: 3, deicing: 1, ground_delay: 1, departure_delay: 1 } };
  const fs = [feat('Ground delay programme', 1), feat('Closed to some traffic', 0), feat('Closed to some traffic', 0), feat('De-icing', 0)];
  assert.equal(statusMessage(m, fs),
    '1 US airport with delays or full closures (1 delay programme). 3 more with FAA notices only (2 closed to some traffic, 1 de-icing).');
});

await test('delays row with no delays still mentions notices', () => {
  assert.equal(statusMessage({ airports: 1, delayed: 0, counts: { deicing: 1 } }, [feat('De-icing', 0)]),
    'No US airport reports delays right now. 1 more with FAA notices only (1 de-icing).');
  assert.equal(statusMessage({ airports: 0, delayed: 0, counts: {} }, []), 'No US airport reports delays right now.');
  assert.equal(statusMessage({ airports: 2, delayed: 2, counts: { ground_stop: 2 } }, [feat('Ground stop', 1), feat('Ground stop', 1)]),
    '2 US airports with delays or full closures (2 ground stops).');
});

await test('delays row falls back to meta when features are missing', () => {
  assert.equal(statusMessage({ airports: 3, delayed: 1, counts: {} }), '1 US airport with delays or full closures. 2 more with FAA notices only.');
});

await test('block dates carry the year outside the current one', () => {
  const now = new Date('2026-11-23T10:00:00Z');
  assert.deepEqual(when(ts('2027-05-28T16:00:00Z'), now), { value: '28 May 2027', unit: '16:00 UTC' });
  assert.deepEqual(when(ts('2026-11-24T23:00:00Z'), now), { value: '24 Nov', unit: '23:00 UTC' });
  assert.deepEqual(when(ts('2026-11-23T16:00:00Z'), now), { value: '16:00', unit: 'UTC' });
  assert.deepEqual(when(ts('2025-12-31T08:05:00Z'), now), { value: '31 Dec 2025', unit: '08:05 UTC' });
  assert.deepEqual(when(null, now), { value: null, unit: '' });
  assert.equal(utcClock(ts('2026-11-23T16:00:00Z'), now), '16:00');
});

await test('CZIB block shows the full issue date', () => {
  const spec = czibSpec({ id: 'czib-2026-09', subject: 'Test', number: '2026-09', level: 'avoid', altitudes: 'All altitudes',
    issued: 1790726400, valid_until: ts('2027-01-31T00:00:00Z') });
  const since = spec.block.find(b => b.label === 'In force since');
  assert.equal(since.value, '30 Sep 2026');
  assert.equal(spec.block.find(b => b.label === 'Valid until').value, '31 Jan 2027');
  assert.equal(czibSpec({ id: 'x', subject: 'X', number: '1', level: 'info' }).block.find(b => b.label === 'In force since').value, null);
});

console.log(`${passed} passed, ${failures.length} failed`);
process.exit(failures.length ? 1 : 0);
