// Front lines: DeepStateMap occupied and contested areas in Ukraine, and the derived front line.

import { CONFLICT, INK, safeGet, errorText } from './common.js';

const SRC = 'ov-intel-front';
const L_OCC = 'ov-intel-front-occ';
const L_OCC14 = 'ov-intel-front-occ14';
const L_CONT = 'ov-intel-front-contested';
const L_LIB = 'ov-intel-front-lib';
const L_EDGE = 'ov-intel-front-edge';
const L_CASE = 'ov-intel-front-case';
const L_LINE = 'ov-intel-front-line';
const COLOURS = { occupied: '#e5534b', occupied_2014: '#a83a48', contested: '#b9b1a1', liberated: '#6cb6ff', front: '#ff6f61' };

export function registerFrontlines(ctx) {
  const { map2 } = ctx;
  let meta = null;

  function applyOptions(opts) {
    map2.setVisible(L_LIB, !!opts.liberated);
    map2.setVisible(L_CONT, opts.contested !== false);
  }

  const layer = ctx.layers.register({
    id: 'intel.frontlines', group: 'Conflict', title: 'Front lines', subtitle: 'Ukraine, from DeepStateMap',
    description: 'Areas of Ukraine occupied by Russia (since 2014 and since 2022), areas of unclear status, and the front line where occupied land meets Ukrainian-held land. Other theatres have no open, reusable source yet.',
    source: 'DeepStateMap (deepstatemap.live), credited; front line derived here', color: CONFLICT, symbol: 'area',
    defaultOn: false, refreshMs: 30 * 60 * 1000,
    options: [
      { id: 'contested', label: 'Areas of unclear status', type: 'toggle', default: true, refresh: false },
      { id: 'liberated', label: 'Areas retaken by Ukraine', type: 'toggle', default: false, refresh: false },
    ],
    legend: [{ color: COLOURS.front, label: 'Front line', shape: 'line' }, { color: COLOURS.occupied, label: 'Occupied since 2022', shape: 'area' },
      { color: COLOURS.occupied_2014, label: 'Occupied since 2014', shape: 'area' }, { color: COLOURS.contested, label: 'Status unclear', shape: 'area' },
      { color: COLOURS.liberated, label: 'Retaken by Ukraine (optional)', shape: 'area' }],
    enable({ options }) {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: L_LIB, type: 'fill', source: SRC, filter: ['==', ['get', 'kind'], 'liberated'],
        paint: { 'fill-color': COLOURS.liberated, 'fill-opacity': 0.12 } }, 'polygons');
      map2.addLayer({ id: L_CONT, type: 'fill', source: SRC, filter: ['==', ['get', 'kind'], 'contested'],
        paint: { 'fill-color': COLOURS.contested, 'fill-opacity': 0.3 } }, 'polygons');
      map2.addLayer({ id: L_OCC14, type: 'fill', source: SRC, filter: ['==', ['get', 'kind'], 'occupied_2014'],
        paint: { 'fill-color': COLOURS.occupied_2014, 'fill-opacity': 0.3 } }, 'polygons');
      map2.addLayer({ id: L_OCC, type: 'fill', source: SRC, filter: ['==', ['get', 'kind'], 'occupied'],
        paint: { 'fill-color': COLOURS.occupied, 'fill-opacity': 0.26 } }, 'polygons');
      map2.addLayer({ id: L_EDGE, type: 'line', source: SRC, filter: ['in', ['get', 'kind'], ['literal', ['occupied', 'occupied_2014']]],
        paint: { 'line-color': '#c9483f', 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.3, 9, 0.8], 'line-opacity': 0.55 } }, 'lines');
      map2.addLayer({ id: L_CASE, type: 'line', source: SRC, filter: ['==', ['get', 'kind'], 'front'],
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': INK, 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 2.6, 7, 4.2, 11, 6.5], 'line-opacity': 0.85 } }, 'lines');
      map2.addLayer({ id: L_LINE, type: 'line', source: SRC, filter: ['==', ['get', 'kind'], 'front'],
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': COLOURS.front, 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 1.2, 7, 2.0, 11, 3.2] } }, 'lines');
      applyOptions(options);
    },
    disable() { map2.removeSource(SRC); },
    async refresh({ signal }) {
      const fc = await ctx.api.get('/api/intel/frontlines', null, { signal });
      map2.setData(SRC, fc);
      meta = await ctx.api.get('/api/intel/frontlines/meta', null, { signal }).catch(() => null);
      const front = fc.features.find(f => f.properties.kind === 'front');
      layer.setCount(meta?.occupied_km2 != null ? `${ctx.fmt.compact(meta.occupied_km2)} km²` : fc.features.length);
      layer.setStatus('ok', meta?.updated ? `DeepStateMap update ${ctx.fmt.ago(meta.updated)}${front ? '' : '; no front line derived'}.` : null);
    },
    onOption(id, value, all) { applyOptions(all); },
  });

  map2.onClick([L_LINE, L_CASE, L_OCC, L_OCC14, L_CONT, L_LIB], (f, lngLat) => openFrontline(ctx, { lat: lngLat.lat, lon: lngLat.lng, kind: f.properties.kind }));
  map2.onHover([L_LINE, L_CASE, L_OCC, L_OCC14, L_CONT, L_LIB], f => f.properties.label);
  ctx.entities.handler('frontline', (e) => openFrontline(ctx, e), { group: 'Conflict' });
  return layer;
}

export async function openFrontline(ctx, at = {}) {
  const { fmt } = ctx;
  const card = ctx.cards.open({ key: 'frontline:deepstate:ukraine', kind: 'frontline', title: 'Front line in Ukraine', subtitle: 'Loading…', icon: '⟋', accent: CONFLICT });
  const m = await safeGet(ctx, '/api/intel/frontlines/meta');
  if (m.__error) {
    card.update({ subtitle: 'DeepStateMap', sections: [{ text: errorText(m.__error) }] });
    return card;
  }
  const lat = at.lat ?? 48.3;
  const lon = at.lon ?? 37.5;
  const here = at.kind && at.kind !== 'front' ? { occupied: 'You clicked an area occupied since 2022.', occupied_2014: 'You clicked an area occupied since 2014 (Crimea or parts of Donetsk and Luhansk).',
    contested: 'You clicked an area whose control DeepState marks as unclear.', liberated: 'You clicked an area retaken by Ukraine.' }[at.kind] : null;
  card.update({
    subtitle: `DeepStateMap, updated ${m.updated ? fmt.utc(m.updated) : 'recently'}`,
    badges: [{ text: 'Front line derived from the areas', tone: 'grey' }],
    block: [{ label: 'Occupied area', value: fmt.compact(m.occupied_km2), unit: 'km²' }, { label: 'Since 2022', value: fmt.compact(m.occupied_2022_km2), unit: 'km²' },
      { label: 'Front length', value: fmt.int(m.front_km), unit: 'km' }, { label: 'Unclear status', value: fmt.compact(m.contested_km2), unit: 'km²' }],
    sections: [
      here ? { text: here } : null,
      { rows: [['Occupied since 2014', `${fmt.int(m.occupied_2014_km2)} km² (Crimea and parts of Donetsk and Luhansk)`],
        ['Occupied since 2022', `${fmt.int(m.occupied_2022_km2)} km²`], ['Front line pieces', fmt.int(m.front_pieces)],
        ['Fetched', m.built_at ? fmt.ago(m.built_at) : null]] },
      { title: 'How the front line is drawn', text: 'DeepState publishes areas, not a line. Here the line follows the edges of occupied areas that face land held by Ukraine; edges along the international border and the coast are left out. Areas are measured on a 1 km grid.' },
      { title: 'About the source', text: 'DeepStateMap is a Ukrainian open-source mapping project. Its analysts compile the map from public reports, footage and official statements. Positions are not independently verified here.' },
    ],
    links: [{ text: 'Open DeepStateMap', href: m.url || 'https://deepstatemap.live/en' }],
    entity: { kind: 'frontline', id: 'deepstate:ukraine', title: 'Front line in Ukraine', subtitle: 'DeepStateMap', lat, lon, layer: 'intel.frontlines', zoom: 6 },
    foot: 'Map data: DeepStateMap (deepstatemap.live). Credit them when you share it.',
  });
  return card;
}
