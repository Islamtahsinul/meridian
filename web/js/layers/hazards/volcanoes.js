// Volcanoes: the Smithsonian GVP Holocene list as faint triangles, with volcanoes under an alert
// or erupting now drawn larger and coloured by level (USGS HANS, NASA EONET, GDACS).

import { GROUP, INK } from './kit.js';

const SRC = 'ov-hazards-volcanoes';
const CLICKABLE = ['ov-hazards-volcanoes-base', 'ov-hazards-volcanoes-active'];
export const VOLCANO_LEVEL = { 0: '#a88f72', 1: '#a88f72', 2: '#ffd166', 3: '#ff8c42', 4: '#ff5d52' };
const LEVEL_TEXT = { 2: 'Advisory or ongoing activity', 3: 'Watch or orange alert', 4: 'Warning or red alert' };

export function registerVolcanoes(ctx, kit) {
  const { map2 } = ctx;
  const levelColour = ['match', ['get', 'a'], 2, VOLCANO_LEVEL[2], 3, VOLCANO_LEVEL[3], 4, VOLCANO_LEVEL[4], VOLCANO_LEVEL[0]];

  function addLayers() {
    map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: 'ov-hazards-volcanoes-base', type: 'symbol', source: SRC, filter: ['<', ['get', 'a'], 2],
      layout: { 'icon-image': 'hazards-volcano', 'icon-allow-overlap': true,
        'icon-size': ['interpolate', ['linear'], ['zoom'], 1, 0.2, 5, 0.32, 10, 0.5],
        'text-field': ['step', ['zoom'], '', 7, ['get', 'name']], 'text-font': ['Open Sans Italic'], 'text-size': 10.5,
        'text-anchor': 'top', 'text-offset': [0, 0.8], 'text-optional': true },
      paint: { 'icon-color': VOLCANO_LEVEL[0],
        'icon-opacity': ['interpolate', ['linear'], ['zoom'], 1, ['case', ['>=', ['get', 'y'], 1900], 0.75, 0.42], 6, 0.9],
        'text-color': '#cdbfae', 'text-halo-color': INK, 'text-halo-width': 1.2 } }, 'points');
    map2.addLayer({ id: 'ov-hazards-volcanoes-glow', type: 'circle', source: SRC, filter: ['>=', ['get', 'a'], 2],
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 9, 6, 15], 'circle-color': levelColour,
        'circle-opacity': 0.22, 'circle-blur': 0.6, 'circle-pitch-alignment': 'map' } }, 'points');
    map2.addLayer({ id: 'ov-hazards-volcanoes-active', type: 'symbol', source: SRC, filter: ['>=', ['get', 'a'], 2],
      layout: { 'icon-image': 'hazards-volcano', 'icon-allow-overlap': true, 'icon-ignore-placement': true,
        'icon-size': ['interpolate', ['linear'], ['zoom'], 1, 0.42, 6, 0.62, 10, 0.8], 'symbol-sort-key': ['get', 'a'],
        'text-field': ['step', ['zoom'], '', 3, ['get', 'name']], 'text-font': ['Open Sans Bold'], 'text-size': 11,
        'text-anchor': 'top', 'text-offset': [0, 1.0], 'text-optional': true },
      paint: { 'icon-color': levelColour, 'icon-halo-color': INK, 'icon-halo-width': 1.4,
        'text-color': '#ebe6d6', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
  }

  const layer = ctx.layers.register({
    id: 'hazards.volcanoes', group: GROUP, title: 'Volcanoes', subtitle: 'Smithsonian GVP with live alerts',
    description: 'About 1,200 volcanoes active in the past 12,000 years. Coloured ones are under an alert or erupting now.',
    source: 'Smithsonian Global Volcanism Program, USGS HANS, NASA EONET, GDACS', color: '#ff8c42', symbol: 'icon',
    defaultOn: false, refreshMs: 1800000, viewDependent: false,
    options: [{ id: 'scope', label: 'Show', type: 'select', default: 'all', choices: [
      { value: 'all', label: 'Every Holocene volcano' }, { value: 'active', label: 'Active and on alert only' }] }],
    legend: [{ color: VOLCANO_LEVEL[2], label: LEVEL_TEXT[2] }, { color: VOLCANO_LEVEL[3], label: LEVEL_TEXT[3] },
      { color: VOLCANO_LEVEL[4], label: LEVEL_TEXT[4] }, { color: VOLCANO_LEVEL[0], label: 'Quiet' }],
    async enable() { addLayers(); },
    async disable() { map2.removeSource(SRC); },
    async refresh({ signal, options }) {
      const data = await kit.fetchReady('/api/hazards/volcanoes', { scope: options.scope || 'all' }, signal, d => d.meta?.source?.hans?.pending && d.meta?.source?.eonet?.pending);
      map2.setData(SRC, data);
      const m = data.meta || {};
      layer.setCount(data.features.length);
      if (!m.base && options.scope !== 'active') {
        layer.setStatus('warn', 'The Smithsonian volcano list is still downloading; showing active volcanoes only.');
        kit.retryLater(layer, 15000);
        return;
      }
      layer.setStatus('ok', `${m.active || 0} active or on alert`);
    },
  });

  map2.onClick(CLICKABLE, (f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    ctx.entities.open({ kind: 'volcano', id: p.id, title: p.name, lat, lon, layer: 'hazards.volcanoes' }, { fly: false });
  });
  map2.onHover(CLICKABLE, (f) => {
    const p = f.properties;
    const bits = [p.name];
    if (+p.a >= 2) bits.push(LEVEL_TEXT[p.a] || 'Active');
    if (p.e != null && p.e !== 'null') bits.push(`${ctx.fmt.int(p.e)} m`);
    return bits.join(', ');
  });
  return { layer };
}
