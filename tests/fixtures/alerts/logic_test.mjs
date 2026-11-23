// Node tests for web/js/layers/alerts/logic.js (run by tests/test_alerts_web.py).
import assert from 'node:assert/strict';
import { toneOf, inQuietHours, shouldNotify, plan, circlePolygon, age, isPlainEscape, tabTarget } from '../../../web/js/layers/alerts/logic.js';

let n = 0;
const test = (name, fn) => { fn(); n += 1; };
const at = (h, m = 0) => new Date(2026, 10, 23, h, m);
const quiet = (on, start = '22:00', end = '07:00', allow = true) => ({ notify: { on: true, min_severity: 0 }, quiet_hours: { on, start, end, allow_extreme: allow } });

test('tones follow the contract bands', () => {
  assert.equal(toneOf(10), 'red');
  assert.equal(toneOf(9), 'red');
  assert.equal(toneOf(8.9), 'amber');
  assert.equal(toneOf(6), 'amber');
  assert.equal(toneOf(5.9), 'blue');
  assert.equal(toneOf(null), 'blue');
});

test('quiet hours across midnight and within a day', () => {
  assert.equal(inQuietHours(quiet(true), at(23, 30)), true);
  assert.equal(inQuietHours(quiet(true), at(6, 59)), true);
  assert.equal(inQuietHours(quiet(true), at(7, 0)), false);
  assert.equal(inQuietHours(quiet(true), at(12, 0)), false);
  assert.equal(inQuietHours(quiet(false), at(23, 30)), false);
  assert.equal(inQuietHours(quiet(true, '13:00', '14:00'), at(13, 30)), true);
  assert.equal(inQuietHours(quiet(true, '13:00', '14:00'), at(14, 30)), false);
  assert.equal(inQuietHours(quiet(true, '08:00', '08:00'), at(8, 0)), false);
  assert.equal(inQuietHours(quiet(true, 'soon', '07:00'), at(23, 0)), false);
  assert.equal(inQuietHours(null, at(23, 0)), false);
});

test('who pops up', () => {
  const a = (severity, extra = {}) => ({ id: 1, severity, reason: 'new', ...extra });
  assert.equal(shouldNotify(a(5), quiet(false), at(12)), true);
  assert.equal(shouldNotify(a(5), quiet(true), at(23)), false);
  assert.equal(shouldNotify(a(9), quiet(true), at(23)), true);                  // extreme gets through
  assert.equal(shouldNotify(a(9), quiet(true, '22:00', '07:00', false), at(23)), false);
  assert.equal(shouldNotify(a(5, { folded: true }), quiet(false), at(12)), false);
  assert.equal(shouldNotify(a(0, { reason: 'test' }), quiet(true), at(23)), true);   // the user asked for it
  const picky = { notify: { on: true, min_severity: 6 }, quiet_hours: { on: false } };
  assert.equal(shouldNotify(a(5), picky, at(12)), false);
  assert.equal(shouldNotify(a(6), picky, at(12)), true);
  assert.equal(shouldNotify(a(9), { notify: { on: false } }, at(12)), false);
  assert.equal(shouldNotify(a(4), null, at(12)), true);                         // defaults before settings load
});

test('a burst is announced as three plus a summary, most severe first', () => {
  const alerts = [1, 2, 3, 4, 5].map(i => ({ id: i, severity: i, reason: 'new' }));
  const p = plan(alerts, quiet(false), at(12));
  assert.deepEqual(p.each.map(a => a.id), [5, 4, 3]);
  assert.equal(p.more, 2);
  assert.equal(p.top.id, 5);
  assert.equal(plan([], quiet(false)).more, 0);
});

test('preview circles are closed, sized and continuous across 180°', () => {
  const g = circlePolygon(10, 179.9, 100);
  const ring = g.coordinates[0];
  assert.equal(g.type, 'Polygon');
  assert.deepEqual(ring[0], ring[ring.length - 1]);
  assert.ok(ring.some(p => p[0] > 180), 'keeps continuous longitudes');
  const north = ring[0];
  assert.ok(Math.abs(north[1] - (10 + 100 / 111.2)) < 0.01);
  const pole = circlePolygon(89.5, 0, 500).coordinates[0];
  assert.ok(pole.every(p => p[1] <= 90));
});

test('ages', () => {
  const now = Date.UTC(2026, 10, 23, 12);
  assert.equal(age(now / 1000 - 10, now), 'just now');
  assert.equal(age(now / 1000 - 300, now), '5 min ago');
  assert.equal(age(now / 1000 - 3 * 3600, now), '3 h ago');
  assert.equal(age(now / 1000 - 3 * 86400, now), '3 days ago');
});

test('plain Escape only', () => {
  assert.equal(isPlainEscape({ key: 'Escape' }), true);
  assert.equal(isPlainEscape({ key: 'Escape', isComposing: true }), false);
  assert.equal(isPlainEscape({ key: 'Escape', ctrlKey: true }), false);
  assert.equal(isPlainEscape({ key: 'Enter' }), false);
  assert.equal(isPlainEscape(null), false);
});

test('tab list arrow keys wrap, Home and End jump', () => {
  assert.equal(tabTarget('ArrowRight', 0, 3), 1);
  assert.equal(tabTarget('ArrowRight', 2, 3), 0);
  assert.equal(tabTarget('ArrowLeft', 0, 3), 2);
  assert.equal(tabTarget('ArrowLeft', 2, 3), 1);
  assert.equal(tabTarget('Home', 2, 3), 0);
  assert.equal(tabTarget('End', 0, 3), 2);
  assert.equal(tabTarget('ArrowDown', 0, 3), null);
  assert.equal(tabTarget('Tab', 0, 3), null);
  assert.equal(tabTarget('ArrowRight', -1, 3), 1);    // unknown current tab counts as the first
  assert.equal(tabTarget('ArrowRight', 0, 0), null);
});

console.log(`alerts logic: ${n} tests passed`);
