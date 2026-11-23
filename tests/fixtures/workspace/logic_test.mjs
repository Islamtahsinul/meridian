// node tests/fixtures/workspace/logic_test.mjs : the Workspaces module's pure logic
// (web/js/layers/workspace/logic.js): states, layer attribution, privacy rules and the file formats.
// Run by python -m unittest tests.test_workspace_web.

import assert from 'node:assert/strict';
import * as L from '../../../web/js/layers/workspace/logic.js';

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed++; } catch (err) { failures.push(name); console.log(`FAIL ${name}\n  ${err?.stack || err}`); }
}

const SHIPS_OPTS = [{ id: 'types', type: 'select', choices: [{ value: 'all' }, { value: 'cargo,tanker' }, { value: 'tanker' }] },
  { id: 'moving', type: 'toggle' }, { id: 'labels', type: 'toggle' }];
const QUAKE_OPTS = [{ id: 'minMag', type: 'range', min: 0, max: 7 }, { id: 'window', type: 'select', choices: [{ value: 'day' }, { value: 'week' }] }];

// ------------------------------------------------------------------ states
test('option values are checked against their declared type', () => {
  assert.deepEqual(L.optionValue({ type: 'toggle' }, true), { ok: true, value: true });
  assert.equal(L.optionValue({ type: 'toggle' }, 'true').ok, false);
  assert.deepEqual(L.optionValue(SHIPS_OPTS[0], 'tanker'), { ok: true, value: 'tanker' });
  assert.equal(L.optionValue(SHIPS_OPTS[0], 'yachts').ok, false);
  assert.deepEqual(L.optionValue({ type: 'select', choices: [{ value: 3 }] }, '3'), { ok: true, value: 3 });
  assert.deepEqual(L.optionValue(QUAKE_OPTS[0], 4.5), { ok: true, value: 4.5 });
  assert.equal(L.optionValue(QUAKE_OPTS[0], 9).ok, false);
  assert.equal(L.optionValue(QUAKE_OPTS[0], NaN).ok, false);
  assert.equal(L.optionValue(undefined, 1).ok, false);
});

test('snapshot keeps the view, display and the layers that are on with their declared options', () => {
  const s = L.snapshot({
    view: { center: { lng: 384.9, lat: 59.612345678 }, zoom: 6.234, bearing: 12.34, pitch: 40.04 },
    display: { basemap: 'relief', projection: 'globe', terrain: true },
    layers: [{ id: 'ships', on: true, options: { types: 'tanker', moving: false, junk: 1, labels: 'yes' }, specOptions: SHIPS_OPTS },
      { id: 'flights', on: false, options: {}, specOptions: [] }, { id: 'bad id', on: true }],
  });
  assert.deepEqual(s, { v: 1, view: { center: [24.9, 59.61235], zoom: 6.23, bearing: 12.3, pitch: 40 }, basemap: 'relief', projection: 'globe',
    terrain: true, layers: { ships: { on: true, options: { types: 'tanker', moving: false } } } });
});

test('planApply turns on, turns off, sets valid options and reports unknown layers', () => {
  const registry = [
    { id: 'ships', on: false, options: { types: 'all' }, specOptions: SHIPS_OPTS },
    { id: 'hazards.quakes', on: true, options: { minMag: 0, window: 'week' }, specOptions: QUAKE_OPTS },
    { id: 'cameras', on: true, options: {}, specOptions: [] },
  ];
  const plan = L.planApply({ v: 1, view: { center: [24.9, 59.6], zoom: 30, bearing: 5, pitch: 99 }, basemap: 'dark', projection: 'nope', terrain: false,
    layers: { ships: { on: true, options: { types: 'tanker', moving: 'x' } }, 'hazards.quakes': { on: true, options: { minMag: 5, window: 'week' } },
      'space.radar': { on: true }, flights: { on: false } } }, registry);
  assert.deepEqual(plan.turnOn, ['ships']);
  assert.deepEqual(plan.turnOff, ['cameras']);
  assert.deepEqual(plan.unknown, ['space.radar']);
  assert.deepEqual(plan.options, [{ id: 'ships', name: 'types', value: 'tanker', layerOn: false }, { id: 'hazards.quakes', name: 'minMag', value: 5, layerOn: true }]);
  assert.deepEqual(plan.view, { center: [24.9, 59.6], zoom: 22, bearing: 5, pitch: 85 });
  assert.deepEqual(plan.display, { basemap: 'dark', terrain: false });
  const untouched = L.planApply({ v: 1, basemap: 'light' }, registry);
  assert.equal(untouched.touchesLayers, false);
  assert.deepEqual([untouched.turnOn, untouched.turnOff], [[], []]);
  assert.equal(L.planApply(null, registry).view, null);
});

test('describeState and readExport', () => {
  assert.equal(L.describeState({ layers: { a: { on: true }, b: { on: true } }, basemap: 'dark', projection: 'globe', terrain: true }), '2 layers, dark map, globe, 3D terrain');
  assert.equal(L.describeState({ basemap: 'light' }), 'layers unchanged, light map');
  const items = L.readExport({ kind: 'meridian-workspaces', workspaces: [{ name: 'A', state: { v: 1 } }, { name: 2, state: {} }, null, { name: 'B' }] });
  assert.deepEqual(items, [{ name: 'A', state: { v: 1 } }]);
  assert.throws(() => L.readExport({ kind: 'something-else', workspaces: [] }));
  assert.throws(() => L.readExport({ hello: 1 }));
  assert.deepEqual(L.readExport([{ name: 'C', state: {} }]), [{ name: 'C', state: {} }]);
});

test('broken JSON in an import gets plain words, not the parser message', () => {
  assert.throws(() => L.parseExport('{not json'), { message: "it isn't a Meridian workspaces file (the JSON is broken)." });
  assert.throws(() => L.parseExport('{"hello": 1}'), { message: 'This file has no workspaces in it.' });
  assert.deepEqual(L.parseExport('[{"name":"C","state":{}}]'), [{ name: 'C', state: {} }]);
});

// ------------------------------------------------------------------ attribution
const STYLE = ['bm-hillshade', 'ov-core-highlight-ring', 'ov-cameras-clusters', 'ov-cameras-points', 'ov-cameras-labels',
  'ov-hazards-quakes-halo', 'ov-hazards-quakes', 'ov-hazards-quakes-label', 'ov-hazards-selection-fill', 'ov-hazards-tsunami-ring',
  'ov-infra-ap-large', 'ov-infra-airports-runways', 'ov-infra-dc-c', 'ov-infra-dams-c', 'ov-infra-ports-c', 'ov-infra-power-c',
  'ov-infra-cables-line', 'ov-infra-cables-hit', 'ov-infra-landings-c', 'ov-ships-live-move', 'ov-ships-live-label', 'ov-ships-dark-ring',
  'ov-ships-sanc-ring', 'ov-ships-straits-fill', 'ov-ships-trail-line', 'ov-flights-icon', 'ov-flights-label', 'ov-flights-gpsjam-fill',
  'ov-space-sats', 'ov-space-sel', 'ov-space-night', 'ov-space-launch', 'ov-intel-conv-dot', 'ov-intel-conflict-dot', 'ov-intel-front-line',
  'ov-intel-countries-fill'];
const REGISTERED = ['cameras', 'hazards.quakes', 'hazards.tsunami', 'infra.airports', 'infra.datacenters', 'infra.dams', 'infra.ports',
  'infra.power', 'infra.cables', 'ships', 'ships.dark', 'ships.sanctioned', 'ships.straits', 'flights', 'flights.military', 'flights.gpsjam',
  'space.satellites', 'space.nightlights', 'space.launches', 'intel.convergences', 'intel.conflict', 'intel.frontlines', 'intel.countries'];
const ids = (layer) => L.styleLayersFor(layer, STYLE, REGISTERED);

test('each layer finds its own map layers, abbreviations included, helpers left out', () => {
  assert.deepEqual(ids('hazards.quakes'), { ids: ['ov-hazards-quakes-halo', 'ov-hazards-quakes', 'ov-hazards-quakes-label'], shared: false });
  assert.deepEqual(ids('hazards.tsunami').ids, ['ov-hazards-tsunami-ring']);
  assert.deepEqual(ids('infra.airports').ids, ['ov-infra-ap-large', 'ov-infra-airports-runways']);
  assert.deepEqual(ids('infra.datacenters').ids, ['ov-infra-dc-c']);
  assert.deepEqual(ids('infra.dams').ids, ['ov-infra-dams-c']);
  assert.deepEqual(ids('infra.ports').ids, ['ov-infra-ports-c']);
  assert.deepEqual(ids('infra.power').ids, ['ov-infra-power-c']);
  assert.deepEqual(ids('infra.cables').ids, ['ov-infra-cables-line', 'ov-infra-landings-c']);
  assert.deepEqual(ids('ships.sanctioned').ids, ['ov-ships-sanc-ring']);
  assert.deepEqual(ids('ships.dark').ids, ['ov-ships-dark-ring']);
  assert.deepEqual(ids('space.satellites').ids, ['ov-space-sats']);
  assert.deepEqual(ids('space.nightlights').ids, ['ov-space-night']);
  assert.deepEqual(ids('space.launches').ids, ['ov-space-launch']);
  assert.deepEqual(ids('intel.convergences').ids, ['ov-intel-conv-dot']);
  assert.deepEqual(ids('intel.conflict').ids, ['ov-intel-conflict-dot']);
  assert.deepEqual(ids('intel.frontlines').ids, ['ov-intel-front-line']);
  assert.deepEqual(ids('intel.countries').ids, ['ov-intel-countries-fill']);
  assert.deepEqual(ids('cameras'), { ids: ['ov-cameras-clusters', 'ov-cameras-points', 'ov-cameras-labels'], shared: false });
});

test('a main layer gets the unnamed map layers, shared ones are flagged', () => {
  assert.deepEqual(ids('ships'), { ids: ['ov-ships-live-move', 'ov-ships-live-label'], shared: true });
  assert.deepEqual(ids('flights'), { ids: ['ov-flights-icon', 'ov-flights-label'], shared: true });
  assert.deepEqual(ids('flights.military'), { ids: ['ov-flights-icon', 'ov-flights-label'], shared: true });
  assert.deepEqual(ids('flights.gpsjam').ids, ['ov-flights-gpsjam-fill']);
  assert.deepEqual(L.styleLayersFor('alerts.areas', STYLE, ['alerts.areas']).ids, []);
});

test('ownerOf scores exact, prefix and abbreviation matches', () => {
  assert.equal(L.ownerOf('ov-infra-ap-large', 'infra', ['airports', 'ports', 'power']), 'airports');
  assert.equal(L.ownerOf('ov-infra-ports-c', 'infra', ['airports', 'ports', 'power']), 'ports');
  assert.equal(L.ownerOf('ov-ships-live-move', 'ships', ['dark', 'sanctioned', 'straits']), null);
  assert.equal(L.ownerOf('ov-other-x', 'ships', ['dark']), null);
});

// ------------------------------------------------------------------ features
test('signatures match what MapLibre hands back for a drawn feature', () => {
  const source = { name: 'Pier 1', n: 3, ok: true, none: null, nested: { a: 1 }, list: [1, 2] };
  const drawn = { ok: true, n: 3, name: 'Pier 1', nested: '{"a":1}', list: '[1,2]' };
  assert.equal(L.signature(source), L.signature(drawn));
  assert.notEqual(L.signature({ n: 3 }), L.signature({ n: 4 }));
  assert.equal(L.signature({ z: -0 }), L.signature({ z: 0 }));
  assert.equal(L.signature(null), '[]');
});

test('drawn features are matched back to their full source features', () => {
  const line = { type: 'Feature', geometry: { type: 'LineString', coordinates: [[0, 0], [10, 0], [20, 0]] }, properties: { id: 'c1', name: 'Cable' } };
  const twinA = { type: 'Feature', geometry: { type: 'Point', coordinates: [1, 1] }, properties: { t: 1 } };
  const twinB = { type: 'Feature', geometry: { type: 'Point', coordinates: [5, 5] }, properties: { t: 1 } };
  const index = L.indexFeatures({ type: 'FeatureCollection', features: [line, twinA, twinB, null, { type: 'Feature' }] });
  assert.equal(L.matchFeature(index, { geometry: { type: 'LineString', coordinates: [[9.99, 0], [12, 0]] }, properties: { name: 'Cable', id: 'c1' } }), line);
  assert.equal(L.matchFeature(index, { geometry: { type: 'Point', coordinates: [4.9, 5.1] }, properties: { t: 1 } }), twinB);
  assert.equal(L.matchFeature(index, { geometry: { type: 'Point', coordinates: [0, 0] }, properties: { t: 2 } }), null);
});

test('a point drawn by two sources is the same thing; parts and shapes that share an id are not', () => {
  const pt = (props, lon = -0.1311, lat = 51.5126) => ({ geometry: { type: 'Point', coordinates: [lon, lat] }, properties: props });
  assert.equal(L.stableId({ id: 'tfl:00001.07458', n: 'x' }), 'id:tfl:00001.07458');
  assert.equal(L.stableId({ hex: '4ca2b3' }), 'hex:4ca2b3');
  assert.equal(L.stableId({ mmsi: 265786030 }), 'mmsi:265786030');
  assert.equal(L.stableId({ id: '  ', name: 'x' }), null);
  assert.equal(L.stableId(null), null);
  const dup = L.crossSourceDuplicates();
  assert.equal(dup(pt({ id: 'cam1', h: 90 }), 'ov-cameras'), false);
  assert.equal(dup(pt({ id: 'cam1', n: 'Name' }), 'ov-cameras-names'), true, 'its street-zoom name');
  assert.equal(dup(pt({ id: 'cam1', n: 'Name' }, -0.13111, 51.51261), 'ov-cameras-names'), true, 'within a few metres');
  assert.equal(dup(pt({ id: 'cam1' }, -0.1411), 'ov-cameras-names'), false, 'far apart: not the same point');
  assert.equal(dup(pt({ id: 'cam2' }), 'ov-cameras'), false);
  assert.equal(dup(pt({ id: 'cam2' }), 'ov-cameras'), false, 'the same source: left to the usual matching');
  const line = { geometry: { type: 'LineString', coordinates: [[0, 0], [1, 1]] }, properties: { id: 'c1' } };
  assert.equal(dup(line, 'a'), false);
  assert.equal(dup(line, 'b'), false, 'lines and shapes are never dropped here');
  assert.equal(dup(pt({ name: 'no id' }), 'a'), false);
  assert.equal(dup(pt({ name: 'no id' }), 'b'), false);
});

test('restrict leaves out Windy webcams, unconfirmed or private aircraft and owner fields', () => {
  const feats = [
    { geometry: { type: 'Point', coordinates: [8, 47] }, properties: { id: 'windy:1000550952' } },
    { geometry: { type: 'Point', coordinates: [8, 47] }, properties: { id: 'chmeteo:ETZ', owner: 'x', registered_owner: 'y', owner_country: 'GB', name: 'Cam' } },
    { geometry: { type: 'Point', coordinates: [0, 51] }, properties: { hex: 'aaaaaa' } },
    { geometry: { type: 'Point', coordinates: [0, 51] }, properties: { hex: 'bbbbbb' } },
  ];
  const r = L.restrict(feats, { aircraft: { allowed: new Set(['aaaaaa']) } });
  assert.deepEqual(r.features.map(f => f.properties), [{ id: 'chmeteo:ETZ', name: 'Cam' }, { hex: 'aaaaaa' }]);
  assert.deepEqual(r.dropped, { windy: 1, privacy: 1 });
  const unknown = L.restrict(feats, { aircraft: null });
  assert.equal(unknown.features.filter(f => f.properties.hex).length, 0);
  assert.equal(L.restrict(feats).features.filter(f => f.properties.hex).length, 2);
});

test('aircraft rows decode the flights module answers and carry the privacy flag', () => {
  const rows = L.aircraftRows([{ fields: ['hex', 'lat', 'lon', 'flags', 'cs'], ac: [['aaaaaa', 51, 0, 2, 'RYR1'], ['bbbbbb', 51, 0, 64 | 2, 'PRIV'], ['bad']] },
    { fields: ['lat'], ac: [[1]] }, null]);
  assert.equal(rows.size, 2);
  assert.equal(rows.get('bbbbbb').flags & L.PRIVACY_FLAG, L.PRIVACY_FLAG);
  assert.equal(rows.get('aaaaaa').cs, 'RYR1');
});

// ------------------------------------------------------------------ formats
const POINT = { type: 'Feature', geometry: { type: 'Point', coordinates: [-0.4614123456, 51.4775987654] },
  properties: { id: 'us7000abcd', title: '=HYPERLINK("http://evil")', mag: 5.2, time: 1795380000, depth_km: -1.5, note: 'a "quoted", line\nbreak', tags: ['a', 'b'] } };
const POLY = { type: 'Feature', geometry: { type: 'Polygon', coordinates: [[[10, 50], [11, 50], [11, 51], [10, 51], [10, 50]], [[10.2, 50.2], [10.4, 50.2], [10.4, 50.4], [10.2, 50.2]]] },
  properties: { name: 'Zone <A> & "B"', start: '2026-11-23T10:00:00Z' } };

test('CSV cells are quoted per RFC 4180 and formulas are made harmless', () => {
  assert.equal(L.csvCell('plain'), 'plain');
  assert.equal(L.csvCell('a,b'), '"a,b"');
  assert.equal(L.csvCell('say "hi"'), '"say ""hi"""');
  assert.equal(L.csvCell('=1+1'), "'=1+1");
  assert.equal(L.csvCell('+44 20'), "'+44 20");
  assert.equal(L.csvCell('-cmd'), "'-cmd");
  assert.equal(L.csvCell('@SUM(A1)'), "'@SUM(A1)");
  assert.equal(L.csvCell('\tx'), "'\tx");
  assert.equal(L.csvCell(-1.5), '-1.5');
  assert.equal(L.csvCell(NaN), '');
  assert.equal(L.csvCell(true), 'true');
  assert.equal(L.csvCell(null), '');
});

test('CSV files have a BOM, CRLF lines, common columns, layer fields and credits', () => {
  const text = L.toCSV([POINT, POLY], { layer: 'hazards.quakes', source: 'USGS' });
  assert.equal(text.charCodeAt(0), 0xfeff);
  const lines = text.slice(1).split('\r\n');
  assert.equal(lines.at(-1), '');
  const header = lines[0].split(',');
  assert.deepEqual(header.slice(0, 8), ['layer', 'kind', 'id', 'title', 'lat', 'lon', 'time_utc', 'geometry']);
  assert.equal(header.at(-1), 'source');
  assert.ok(header.includes('mag') && header.includes('tags'));
  assert.ok(lines[1].startsWith('hazards.quakes,,us7000abcd,"\'=HYPERLINK(""http://evil"")",51.4776,-0.46141,2026-11-22T20:40:00Z,Point,'), lines[1]);
  assert.ok(lines[1].includes('"a ""quoted"", line\nbreak"'), 'a line break inside a cell stays inside its quotes');
  assert.ok(lines[1].includes(',-1.5,'), 'negative numbers are numbers, not formulas');
  assert.ok(lines.some(l => l.startsWith('hazards.quakes,,,') && l.includes('Zone <A> & ""B""') && l.includes('2026-11-23T10:00:00Z') && l.includes(',Polygon,')));
  assert.ok(text.includes('"[""a"",""b""]"'), 'nested values become JSON text');
});

test('GeoJSON files round coordinates to 5 decimals and carry credits and the on-screen note', () => {
  const doc = JSON.parse(L.toGeoJSON([POINT, POLY, { type: 'Feature', geometry: null, properties: {} }],
    { layer: 'hazards.quakes', title: 'Earthquakes, on screen', attribution: 'USGS', licence: 'Public domain', note: 'Only what was drawn', exportedAt: '2026-11-23T17:00:00Z' }));
  assert.equal(doc.type, 'FeatureCollection');
  assert.equal(doc.features.length, 2);
  assert.deepEqual(doc.features[0].geometry.coordinates, [-0.46141, 51.4776]);
  assert.deepEqual(doc.bbox, [-0.46141, 50, 11, 51.4776]);
  assert.equal(doc.attribution, 'USGS');
  assert.equal(doc.licence, 'Public domain');
  assert.equal(doc.note, 'Only what was drawn');
  assert.equal(doc.exported_at, '2026-11-23T17:00:00Z');
  assert.deepEqual(doc.features[0].properties.tags, ['a', 'b']);
});

test('KML files escape text, use one style, carry credits and timestamps, and draw every geometry', () => {
  const multi = { type: 'Feature', geometry: { type: 'MultiLineString', coordinates: [[[0, 0], [1, 1]], [[2, 2], [3, 3]]] }, properties: { name: 'Lines' } };
  const coll = { type: 'Feature', geometry: { type: 'GeometryCollection', geometries: [{ type: 'Point', coordinates: [1, 2] }, { type: 'MultiPoint', coordinates: [[3, 4]] }] }, properties: {} };
  const kml = L.toKML([POINT, POLY, multi, coll], { title: 'Quakes & "zones"', color: '#f0a43a', attribution: 'USGS <public>', note: 'On screen only.', exportedAt: '2026-11-23T17:00:00Z' });
  assert.ok(kml.startsWith('<?xml version="1.0" encoding="UTF-8"?>'));
  assert.ok(kml.includes('<name>Quakes &amp; &quot;zones&quot;</name>'));
  assert.ok(kml.includes('<color>ff3aa4f0</color>') && kml.includes('<color>593aa4f0</color>'));
  assert.equal((kml.match(/<Placemark>/g) || []).length, 4);
  assert.ok(kml.includes('<name>Zone &lt;A&gt; &amp; &quot;B&quot;</name>'));
  assert.ok(kml.includes('<when>2026-11-22T20:40:00Z</when>'));
  assert.ok(kml.includes('<innerBoundaryIs>'));
  assert.ok(kml.includes('<coordinates>-0.46141,51.4776</coordinates>'));
  assert.ok(kml.includes('Source: USGS &lt;public&gt;'));
  assert.ok(!/<Icon>|<href>/.test(kml), 'no remote icons');
  assert.ok(kml.includes('<MultiGeometry><LineString>'));
  assert.equal(L.xmlText('a\u0001b\u000bc'), 'abc');
});

test('rows, times, points and file names', () => {
  assert.equal(L.isoTime(1795380000), '2026-11-22T20:40:00Z');
  assert.equal(L.isoTime(1795380000000), '2026-11-22T20:40:00Z');
  assert.equal(L.isoTime('2026-11-23T10:00:00Z'), '2026-11-23T10:00:00Z');
  assert.equal(L.isoTime(42), '');
  assert.equal(L.isoTime('soon'), '');
  assert.deepEqual(L.representativePoint(POLY.geometry), [10.5, 50.5]);
  const r = L.rowOf({ geometry: { type: 'Point', coordinates: [1, 2] }, properties: { hex: '4ca2b3', lbl: 'RYR8NE\n36,000 ft' } });
  assert.deepEqual(r, { kind: '', id: '4ca2b3', title: 'RYR8NE, 36,000 ft', time_utc: '', lat: 2, lon: 1, geometry: 'Point' });
  // A count in `n` (ships, fires) is not a title; a name in `n` (infra, cameras) is. Unnamed ships get their MMSI.
  const ship = (p) => L.rowOf({ geometry: { type: 'Point', coordinates: [2.41681, 51.64561] }, properties: p });
  assert.equal(ship({ mmsi: 255916297, name: '', n: 1 }).title, 'MMSI 255916297');
  assert.equal(ship({ mmsi: 255916297, name: '', n: 37 }).title, 'MMSI 255916297');
  assert.equal(ship({ mmsi: 255916297, name: 'EVER GIVEN', n: 1 }).title, 'EVER GIVEN');
  assert.equal(ship({ kind: 'cell', n: 412 }).title, '');
  assert.equal(ship({ id: 'LHR', n: 'Heathrow' }).title, 'Heathrow');
  assert.equal(L.exportFilename('hazards.quakes', 'csv', new Date(Date.UTC(2026, 10, 23, 17, 2))), 'meridian-hazards-quakes-on-screen-20261123-1702.csv');
  assert.equal(L.exportFilename('../x y', 'kml', new Date(Date.UTC(2026, 0, 2, 3, 4))), 'meridian-x-y-on-screen-20260102-0304.kml');
  assert.deepEqual(Object.keys(L.FORMATS), ['geojson', 'csv', 'kml']);
});

console.log(`${failures.length ? 'not ok' : 'ok'} workspace logic: ${passed} tests passed${failures.length ? `, ${failures.length} failed: ${failures.join(', ')}` : ''}`);
process.exit(failures.length ? 1 : 0);
