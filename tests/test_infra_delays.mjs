// Tests for the airport card's FAA status section (web/js/layers/infra/delays.js), without a browser.
// Fixtures are trimmed answers of GET /api/flights/airport-status/{ident} from 23 Nov 2026.
//
//   node tests/test_infra_delays.mjs        (also run by python -m unittest tests.test_infra_web)

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { delayBadge, delayItems, delaySection } from '../web/js/layers/infra/delays.js';
import { fmt } from '../web/js/core/dom.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const fixture = (name) => JSON.parse(readFileSync(join(ROOT, 'tests', 'fixtures', 'infra', name), 'utf8'));

// A stand-in for ctx.h that records the tree; nodes support append() like DOM elements.
function h(tag, attrs, ...children) {
  const node = { tag, attrs: { ...(attrs || {}) }, children: [], append(...more) { add(more); } };
  const add = (list) => { for (const c of list.flat(Infinity)) if (c != null && c !== false && c !== '') node.children.push(typeof c === 'object' ? c : { text: String(c) }); };
  add(children);
  return node;
}
const text = (n) => (n.text !== undefined ? n.text : (n.children || []).map(text).join(''));
/** [[dt, dd], ...] from the section's definition list. */
function pairs(section) {
  const dl = section.children.find(c => c.tag === 'dl');
  const out = [];
  for (let i = 0; i < dl.children.length; i += 2) out.push([text(dl.children[i]), text(dl.children[i + 1]), dl.children[i].attrs.class || null]);
  return out;
}

let passed = 0;
function test(name, fn) {
  try { fn(); passed += 1; } catch (err) { console.error(`not ok: ${name}`); throw err; }
}

test('a partial closure is shown, and "no delays" is still true', () => {
  const d = fixture('faa_status_klax.json');
  const rows = pairs(delaySection(d, { h, fmt }));
  assert.equal(rows[0][0], 'Now');
  assert.equal(rows[0][1], 'No delays reported');
  assert.equal(rows[1][0], 'Closed to some traffic');
  assert.match(rows[1][1], /^Closed to non-scheduled transient general aviation/);
  assert.equal(rows[1][2], 'infra-delay-blue');
  // The check time, not the FAA's months-old "last changed" time.
  assert.equal(rows[2][0], 'Checked');
  assert.equal(rows[2][1], fmt.ago(d.fetched_at));
  assert.ok(!rows.some(r => r[0] === 'Updated'));
  assert.deepEqual(delayBadge(d), { text: 'Closed to some traffic', tone: 'blue', title: d.summary });
});

test('every FAA status at an airport gets a row, with its summary', () => {
  const d = fixture('faa_status_ksan.json');
  const rows = pairs(delaySection(d, { h, fmt }));
  assert.deepEqual(rows.map(r => r[0]), ['Ground delay programme', 'Departure delays', 'Closed to some traffic', 'Checked']);
  assert.match(rows[0][1], /up to 80 min/);
  assert.equal(rows[1][1], 'Departures are delayed 31 to 45 min, increasing.');
  assert.deepEqual(rows.slice(0, 3).map(r => r[2]), ['infra-delay-amber', 'infra-delay-amber', 'infra-delay-blue']);
  assert.ok(!rows.some(r => r[1] === 'No delays reported'));
  assert.equal(delayBadge(d).tone, 'amber');
  assert.equal(delayBadge(d).text, 'Ground delay programme');
});

test('items without a summary are described from their fields', () => {
  const d = { delay: true, type: 'Ground stop', level: 'high', items: [
    { delay: true, type: 'Ground stop', level: 'high', reason: 'WX:Thunderstorms' },
    { delay: true, type: 'Arrival delays', level: 'medium', min_delay_min: 16, max_delay_min: 30, trend: 'Increasing' }] };
  const rows = pairs(delaySection(d, { h, fmt }));
  assert.deepEqual(rows, [['Ground stop', 'Thunderstorms', 'infra-delay-red'],
    ['Arrival delays', 'Delays 16 to 30 min, increasing', 'infra-delay-amber']]);
  assert.deepEqual(delayBadge(d), { text: 'Ground stop', tone: 'red', title: 'Thunderstorms' });
});

test('older answers without items still work, and the FAA time is labelled for what it is', () => {
  const d = { delay: true, type: 'Departure delays', reason: 'Volume', avg_delay_min: 25, updated: 1795440000 };
  assert.equal(delayItems(d).length, 1);
  const rows = pairs(delaySection(d, { h, fmt }));
  assert.deepEqual(rows[0].slice(0, 2), ['Departure delays', 'Volume, 25 min on average']);
  assert.deepEqual(rows[1].slice(0, 2), ['FAA last changed', fmt.ago(1795440000)]);
  assert.equal(delaySection(null, { h, fmt }), null);
  assert.equal(delayBadge({ items: [] }), null);
  assert.equal(pairs(delaySection({ items: [], fetched_at: 1795440000 }, { h, fmt }))[0][1], 'No delays reported');
});

console.log(`ok ${passed} tests`);
