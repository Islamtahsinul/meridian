// node tests/fixtures/workspace/deeplink_test.mjs : the deep-link format (web/js/core/deeplink.js),
// encoded and decoded without a browser. Run by python -m unittest tests.test_workspace_web.

import assert from 'node:assert/strict';
import * as D from '../../../web/js/core/deeplink.js';

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed++; } catch (err) { failures.push(name); console.log(`FAIL ${name}\n  ${err?.stack || err}`); }
}

test('parse a full link', () => {
  const link = D.parseHash('#map=8/57.6/11.2&l=ships,ships.straits&e=strait:ships:danish_straits');
  assert.deepEqual(link.map, { zoom: 8, lat: 57.6, lon: 11.2, bearing: 0, pitch: 0 });
  assert.deepEqual(link.layers, ['ships', 'ships.straits']);
  assert.deepEqual(link.entity, { kind: 'strait', id: 'ships:danish_straits' });
  assert.deepEqual(link.rest, []);
});

test('the entity kind ends at the first colon (camera ids contain colons)', () => {
  assert.deepEqual(D.parseEntity('camera:tfl:00001.06514'), { kind: 'camera', id: 'tfl:00001.06514' });
  assert.deepEqual(D.parseEntity('camera%3Atfl%3A00001.06514'), { kind: 'camera', id: 'tfl:00001.06514' });
  assert.deepEqual(D.parseEntity('gps_jam:ships:51.5,-0.5'), { kind: 'gps_jam', id: 'ships:51.5,-0.5' });
  assert.deepEqual(D.parseEntity('aircraft:~2c1b3a'), { kind: 'aircraft', id: '~2c1b3a' });
  assert.deepEqual(D.parseEntity('my_place:3'), { kind: 'my_place', id: '3' });
});

test('hostile entities are ignored', () => {
  for (const bad of ['', ':x', 'ship', 'ship:', 'Ship:1', '1ship:2', 'ship:../../api/core', 'ship:a/b', 'ship:<script>',
    'ship:a b', 'ship:%E0%A4%A', 'ship:javascript:alert(1)', `ship:${'x'.repeat(121)}`, 'k:x', `${'a'.repeat(41)}:1`,
    'ship:a"b', "ship:a'b", 'ship:a#b', 'ship:a?b', 'ship:a%25b', 'ship:\u202ex']) {
    assert.equal(D.parseEntity(bad), null, bad);
  }
  assert.deepEqual(D.parseEntity(`ship:${'9'.repeat(120)}`), { kind: 'ship', id: '9'.repeat(120) });
});

test('layers: validated, unique, capped at 32; empty means none; nothing usable is ignored', () => {
  assert.deepEqual(D.parseLayers('ships,ships,flights'), ['ships', 'flights']);
  assert.deepEqual(D.parseLayers(''), []);
  assert.equal(D.parseLayers('<script>,javascript:alert(1)'), null);
  assert.deepEqual(D.parseLayers('%3Cscript%3E,javascript:alert(1),ships'), ['ships']);
  assert.equal(D.parseLayers('%E0%A4%A'), null);
  const many = Array.from({ length: 40 }, (_, i) => `l${i}`).join(',');
  assert.equal(D.parseLayers(many).length, D.LIMITS.layers);
  assert.deepEqual(D.parseLayers('hazards.quakes%2Cintel.conflict'), ['hazards.quakes', 'intel.conflict']);
  assert.equal(D.parseLayers('a'.repeat(81)), null);
});

test('map values follow MapLibre: zoom/lat/lon[/bearing[/pitch]] within range', () => {
  assert.deepEqual(D.parseMap('12.25/51.4775/-0.4614/30/45'), { zoom: 12.25, lat: 51.4775, lon: -0.4614, bearing: 30, pitch: 45 });
  for (const bad of ['', '8', '8/1', '99/0/0', '8/91/0', '8/0/0/181', '8/0/0/0/90', '8/x/0', '8//0', '1/2/3/4/5/6', 'Infinity/0/0']) {
    assert.equal(D.parseMap(bad), null, bad);
  }
});

test('duplicate keys: the last wins; unknown keys are kept aside', () => {
  const link = D.parseHash('#l=ships&l=flights&x=1&e=ship:1&e=ship:2&foo');
  assert.deepEqual(link.layers, ['flights']);
  assert.deepEqual(link.entity, { kind: 'ship', id: '2' });
  assert.deepEqual(link.rest, [['x', '1'], ['foo', '']]);
});

test('absurdly long hashes are ignored', () => {
  assert.deepEqual(D.splitHash(`#l=${'a,'.repeat(5000)}`), []);
  assert.equal(D.parseHash(`#l=${'a,'.repeat(5000)}`).layers, null);
});

test('mapPart uses MapLibre precision and leaves out a zero bearing and pitch', () => {
  assert.equal(D.mapPart({ zoom: 8, lat: 57.6, lon: 11.2 }), '8/57.6/11.2');
  assert.equal(D.mapPart({ zoom: 12.25, lat: 51.4775, lon: -0.4614, bearing: 30, pitch: 45 }), '12.25/51.4775/-0.4614/30/45');
  assert.equal(D.mapPart({ zoom: 2.2, lat: 30.123456, lon: 0.000001 }), '2.2/30.12/0');
  assert.equal(D.mapPart({ zoom: 5, lat: 10, lon: 190 }), '5/10/-170');
  assert.equal(D.mapPart({ zoom: 5, lat: 0, lon: 0, bearing: 0, pitch: 30 }), '5/0/0/0/30');
  assert.equal(D.mapPart({ zoom: 5, lat: -0.0001, lon: 0, bearing: -12.34 }), '5/0/0/-12.3');
});

test('build and parse round trip', () => {
  const state = { map: { zoom: 9, lat: 57.7, lon: 11.9 }, layers: ['infra.ports', 'ships'], entity: { kind: 'ship', id: '265786030' } };
  const hash = D.buildHash(state);
  assert.equal(hash, '#map=9/57.7/11.9&l=infra.ports,ships&e=ship:265786030');
  const back = D.parseHash(hash);
  assert.deepEqual(back.layers, state.layers);
  assert.deepEqual(back.entity, state.entity);
  assert.equal(back.map.zoom, 9);
  assert.equal(D.buildHash({ layers: [] }), '#l=');
  assert.deepEqual(D.parseHash(D.buildHash({ layers: [] })).layers, []);
  assert.equal(D.buildHash({}), '');
});

test('encoding keeps ids readable and escapes what needs it', () => {
  assert.equal(D.encodeEntity({ kind: 'camera', id: 'tfl:00001.06514' }), 'camera:tfl:00001.06514');
  assert.equal(D.encodeEntity({ kind: 'gps_jam', id: 'ships:51.5,-0.5' }), 'gps_jam:ships:51.5,-0.5');
  assert.equal(D.encodeEntity({ kind: 'x_y', id: 'a=b+c@d' }), 'x_y:a%3Db%2Bc%40d');
  assert.deepEqual(D.parseEntity(D.encodeEntity({ kind: 'x_y', id: 'a=b+c@d' })), { kind: 'x_y', id: 'a=b+c@d' });
  assert.equal(D.encodeEntity({ kind: 'ship', id: 'a/b' }), null);
  assert.equal(D.encodeEntity({ kind: 'satellite', id: 25544 }), 'satellite:25544');
  assert.equal(D.encodeLayers(['ships', 'bad id', 'ships', 'flights']), 'ships,flights');
});

test('withLinkState replaces l= and e= and keeps map= and other keys in place', () => {
  assert.equal(D.withLinkState('#map=5/1/2&x=1', { layers: ['a', 'b'], entity: { kind: 'ship', id: '9' } }), '#map=5/1/2&l=a,b&e=ship:9&x=1');
  assert.equal(D.withLinkState('#map=5/1/2&l=old&e=ship:1', { layers: ['new'] }), '#map=5/1/2&l=new');
  assert.equal(D.withLinkState('', { layers: [] }), '#l=');
  assert.equal(D.withLinkState('#x=1', { layers: null, entity: null }), '#x=1');
  assert.equal(D.withLinkState('#map=5/1/2&l=a&e=ship:1', {}), '#map=5/1/2');
});

test('selfKey compares only l= and e=', () => {
  assert.equal(D.selfKey('#map=5/1/2&l=a,b&e=ship:1'), D.selfKey('#map=9/9/9&l=a,b&e=ship:1'));
  assert.notEqual(D.selfKey('#l=a'), D.selfKey('#l=a,b'));
  assert.notEqual(D.selfKey('#l='), D.selfKey(''));
});

test('layerPlan turns on and off only registered layers', () => {
  const plan = D.layerPlan(['ships', 'nope', 'flights'], ['ships', 'flights', 'cameras', 'hazards.quakes'], ['cameras', 'ships', 'gone']);
  assert.deepEqual(plan.target, ['ships', 'flights']);
  assert.deepEqual(plan.turnOn, ['flights']);
  assert.deepEqual(plan.turnOff, ['cameras']);
  assert.deepEqual(D.layerPlan([], ['a', 'b'], ['a']).turnOff, ['a']);
});

test('a link built by tools/mcp_meridian.py map_link decodes here', () => {
  // the same arguments as tests/test_ai_mcp.py test_map_link_format
  const link = D.parseHash('#map=10/51.12/1.33&l=ships,infra.ports&e=port:wpi:31160');
  assert.deepEqual(link.map, { zoom: 10, lat: 51.12, lon: 1.33, bearing: 0, pitch: 0 });
  assert.deepEqual(link.layers, ['ships', 'infra.ports']);
  assert.deepEqual(link.entity, { kind: 'port', id: 'wpi:31160' });
  assert.deepEqual(D.parseHash('#map=12.25/51.4775/-0.4614/30/45&e=camera:tfl:00001.06514').entity, { kind: 'camera', id: 'tfl:00001.06514' });
});

test('the module imports without a browser', () => {
  assert.equal(typeof D.init, 'function');
  assert.equal(typeof D.start, 'function');
  assert.equal(typeof globalThis.window, 'undefined');
});

console.log(`${failures.length ? 'not ok' : 'ok'} deeplink: ${passed} tests passed${failures.length ? `, ${failures.length} failed: ${failures.join(', ')}` : ''}`);
process.exit(failures.length ? 1 : 0);
