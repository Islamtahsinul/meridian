// Earthquakes (USGS): circles sized by magnitude, coloured by age, PAGER alert rings and
// magnitude labels. Default-on layer, refreshed every 5 minutes.

import { GROUP, INK, PAGER, QUAKE_AGE, stepExpr, now } from './kit.js';

const SRC = 'ov-hazards-quakes';
const IDS = ['ov-hazards-quakes-halo', 'ov-hazards-quakes', 'ov-hazards-quakes-alert', 'ov-hazards-quakes-label'];

export function registerQuakes(ctx, kit) {
  const { map2, fmt } = ctx;
  let last = null;

  const magRadius = ['interpolate', ['exponential', 1.55], ['max', -1, ['coalesce', ['get', 'mag'], 0]],
    -1, 1.6, 2, 2.6, 4, 5, 5, 7.5, 6, 11.5, 7, 17, 8, 25, 9, 34];
  // zoom must stay the top-level input, so scaled variants are built stop by stop
  const zr = (mult = 1, add = 0) => ['interpolate', ['linear'], ['zoom'],
    1, ['+', add, ['*', 0.8 * mult, magRadius]], 5, ['+', add, ['*', mult, magRadius]], 10, ['+', add, ['*', 1.7 * mult, magRadius]]];
  const radius = zr();
  const ageColour = stepExpr(['get', 'age'], [[0, QUAKE_AGE[0].color], [1, QUAKE_AGE[1].color], [24, QUAKE_AGE[2].color], [168, QUAKE_AGE[3].color]]);

  function addLayers() {
    map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: 'ov-hazards-quakes-halo', type: 'circle', source: SRC,
      filter: ['all', ['>=', ['coalesce', ['get', 'mag'], 0], 5.5], ['<', ['get', 'age'], 72]],
      paint: { 'circle-radius': zr(2.1), 'circle-color': ageColour, 'circle-opacity': 0.14, 'circle-blur': 0.6,
        'circle-pitch-alignment': 'map' } }, 'points');
    map2.addLayer({ id: 'ov-hazards-quakes', type: 'circle', source: SRC,
      layout: { 'circle-sort-key': ['coalesce', ['get', 'mag'], 0] },
      paint: {
        'circle-radius': radius, 'circle-color': ageColour,
        'circle-opacity': ['interpolate', ['linear'], ['get', 'age'], 0, 0.92, 24, 0.82, 168, 0.62, 720, 0.5],
        'circle-stroke-color': INK, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 1, 0.6, 6, 1.2],
        'circle-stroke-opacity': 0.85, 'circle-pitch-alignment': 'map',
      } }, 'points');
    map2.addLayer({ id: 'ov-hazards-quakes-alert', type: 'circle', source: SRC,
      filter: ['!=', ['get', 'alert'], ''],
      paint: { 'circle-radius': zr(1, 3.5), 'circle-color': 'rgba(0,0,0,0)',
        'circle-stroke-color': ['match', ['get', 'alert'], 'red', PAGER.red, 'orange', PAGER.orange, 'yellow', PAGER.yellow, PAGER.green],
        'circle-stroke-width': 2, 'circle-pitch-alignment': 'map' } }, 'points');
    const magText = ['number-format', ['coalesce', ['get', 'mag'], 0], { 'min-fraction-digits': 1, 'max-fraction-digits': 1 }];
    const vis = layer.option('labels') === false ? 'none' : 'visible';
    map2.addLayer({ id: 'ov-hazards-quakes-label', type: 'symbol', source: SRC,
      filter: ['>=', ['coalesce', ['get', 'mag'], 0], 5.5],
      layout: { 'text-field': magText, 'text-font': ['Open Sans Bold'], 'text-size': ['interpolate', ['linear'], ['zoom'], 1, 9.5, 5, 11, 9, 13],
        'text-allow-overlap': true, 'symbol-sort-key': ['-', 10, ['coalesce', ['get', 'mag'], 0]], visibility: vis },
      paint: { 'text-color': INK } }, 'top');
    map2.addLayer({ id: 'ov-hazards-quakes-label-small', type: 'symbol', source: SRC,
      filter: ['all', ['<', ['coalesce', ['get', 'mag'], 0], 5.5],
        ['any', ['all', ['>=', ['coalesce', ['get', 'mag'], 0], 4.5], ['>=', ['zoom'], 3]], ['>=', ['zoom'], 6.5]]],
      layout: { 'text-field': magText, 'text-font': ['Open Sans Bold'], 'text-size': 10.5, 'text-anchor': 'left',
        'text-radial-offset': ['interpolate', ['linear'], ['coalesce', ['get', 'mag'], 0], 0, 0.5, 5, 0.9],
        'symbol-sort-key': ['-', 10, ['coalesce', ['get', 'mag'], 0]], visibility: vis },
      paint: { 'text-color': '#ebe6d6', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'top');
  }

  const layer = ctx.layers.register({
    id: 'hazards.quakes', group: GROUP, title: 'Earthquakes', subtitle: 'USGS, refreshed every 5 minutes',
    description: 'Earthquakes from the USGS feeds, sized by magnitude and coloured by age. Rings show PAGER impact alerts.',
    source: 'USGS Earthquake Hazards Program (public domain)', color: '#ff8c42', symbol: 'point',
    defaultOn: true, refreshMs: 300000, viewDependent: false,
    options: [
      { id: 'window', label: 'Show', type: 'select', default: 'week', choices: [
        { value: 'day', label: 'Past day, every size' }, { value: 'week', label: 'Past week, M2.5 and up' },
        { value: 'month', label: 'Past month, significant' }] },
      { id: 'minMag', label: 'Smallest magnitude', type: 'range', min: 0, max: 7, step: 0.5, default: 0,
        format: v => (+v ? `M${(+v).toFixed(1)}` : 'Any') },
      { id: 'labels', label: 'Magnitude labels', type: 'toggle', default: true, refresh: false },
    ],
    onOption(id, value) {
      if (id === 'labels') for (const l of ['ov-hazards-quakes-label', 'ov-hazards-quakes-label-small']) map2.setVisible(l, !!value);
    },
    legend: [
      ...QUAKE_AGE.map(a => ({ color: a.color, label: a.label })),
      { color: PAGER.orange, label: 'PAGER alert ring', shape: 'line' },
    ],
    async enable() { addLayers(); },
    async disable() { map2.removeSource(SRC); last = null; },
    async refresh({ signal, options }) {
      const data = await kit.fetchReady('/api/hazards/quakes', { window: options.window || 'week', min_mag: options.minMag || '' }, signal,
        d => d.meta?.source?.pending || d.meta?.partial, { tries: 4, delay: 1500 });
      const t = now();
      for (const f of data.features) f.properties.age = (t - f.properties.t) / 3600;
      map2.setData(SRC, data);
      last = data;
      layer.setCount(data.features.length);
      const b = data.meta?.biggest;
      const state = kit.statusFrom(layer, data.meta, 'No earthquakes match these settings.',
        b ? `Largest: M ${(+b.mag).toFixed(1)}, ${b.place}` : null);
      if (state === 'pending' || data.meta?.partial) kit.retryLater(layer, 5000);
    },
  });

  map2.onClick(IDS.concat('ov-hazards-quakes-label-small'), (f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    ctx.entities.open({ kind: 'earthquake', id: p.id, title: `M ${(+p.mag).toFixed(1)}, ${p.place}`, lat, lon, layer: 'hazards.quakes' }, { fly: false });
  });
  map2.onHover(['ov-hazards-quakes', 'ov-hazards-quakes-alert'], (f) => {
    const p = f.properties;
    const depth = p.d != null ? `, ${Math.round(p.d)} km deep` : '';
    return `M ${p.mag != null ? (+p.mag).toFixed(1) : '?'} ${p.place}, ${fmt.ago(p.t)}${depth}`;
  });
  return { layer, data: () => last };
}
