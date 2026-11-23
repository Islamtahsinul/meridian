// GDACS disaster alerts: one badge per event (hazard glyph on an alert-coloured disc).
// Opening a card draws the event's affected area from the GDACS geometry service.

import { GROUP, INK, ALERT, GDACS_ICON } from './kit.js';

const SRC = 'ov-hazards-disasters';
const CLICKABLE = ['ov-hazards-disasters-disc', 'ov-hazards-disasters-icon'];

export function registerDisasters(ctx, kit) {
  const { map2, fmt } = ctx;
  const alertColour = ['match', ['get', 'alert'], 'Red', ALERT.Red, 'Orange', ALERT.Orange, ALERT.Green];
  const size = ['match', ['get', 'alert'], 'Red', 1.25, 'Orange', 1.08, 0.78];

  function addLayers() {
    map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: 'ov-hazards-disasters-halo', type: 'circle', source: SRC, filter: ['>=', ['get', 'rank'], 2],
      paint: { 'circle-radius': ['*', 17, size], 'circle-color': alertColour, 'circle-opacity': ['*', 0.2, ['+', 0.4, ['*', 0.6, ['get', 'cur']]]],
        'circle-blur': 0.5, 'circle-pitch-alignment': 'map' } }, 'points');
    map2.addLayer({ id: 'ov-hazards-disasters-disc', type: 'circle', source: SRC,
      layout: { 'circle-sort-key': ['get', 'rank'] },
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, ['*', 7.5, size], 6, ['*', 10.5, size]],
        'circle-color': alertColour,
        'circle-opacity': ['case', ['==', ['get', 'cur'], 1], ['match', ['get', 'alert'], 'Green', 0.82, 0.96], 0.45],
        'circle-stroke-color': INK, 'circle-stroke-width': 1.3, 'circle-pitch-alignment': 'map',
      } }, 'points');
    map2.addLayer({ id: 'ov-hazards-disasters-icon', type: 'symbol', source: SRC,
      layout: {
        'icon-image': ['match', ['get', 'type'], ...Object.entries(GDACS_ICON).flat(), 'hazards-alert'],
        'icon-size': ['interpolate', ['linear'], ['zoom'], 1, ['*', 0.36, size], 6, ['*', 0.5, size]],
        'icon-allow-overlap': true, 'icon-ignore-placement': true, 'symbol-sort-key': ['get', 'rank'],
      },
      paint: { 'icon-color': INK, 'icon-opacity': ['case', ['==', ['get', 'cur'], 1], 1, 0.7] } }, 'points');
    map2.addLayer({ id: 'ov-hazards-disasters-label', type: 'symbol', source: SRC,
      filter: ['any', ['all', ['>=', ['get', 'rank'], 2], ['==', ['get', 'cur'], 1]], ['>=', ['zoom'], 5]],
      layout: { 'text-field': ['get', 'short'], 'text-font': ['Open Sans Regular'], 'text-size': 11, 'text-anchor': 'left',
        'text-offset': [1.4, 0], 'text-optional': true, 'symbol-sort-key': ['-', 5, ['get', 'rank']] },
      paint: { 'text-color': '#ebe6d6', 'text-halo-color': INK, 'text-halo-width': 1.4,
        'text-opacity': ['case', ['==', ['get', 'cur'], 1], 1, 0.7] } }, 'top');
  }

  const layer = ctx.layers.register({
    id: 'hazards.disasters', group: GROUP, title: 'Disaster alerts', subtitle: 'GDACS, the UN and EU alert system',
    description: 'Current GDACS alerts for earthquakes, tropical cyclones, floods, volcanoes, droughts and wildfires, plus orange and red alerts from the past 30 days.',
    source: 'GDACS (European Commission JRC, UN OCHA)', color: ALERT.Orange, symbol: 'point',
    defaultOn: false, refreshMs: 600000, viewDependent: false,
    options: [
      { id: 'types', label: 'Hazard', type: 'select', default: '', choices: [{ value: '', label: 'Every hazard' },
        { value: 'EQ', label: 'Earthquakes' }, { value: 'TC', label: 'Tropical cyclones' }, { value: 'FL', label: 'Floods' },
        { value: 'VO', label: 'Volcanoes' }, { value: 'DR', label: 'Droughts' }, { value: 'WF', label: 'Wildfires' }] },
      { id: 'minAlert', label: 'Lowest alert', type: 'select', default: '1', choices: [
        { value: '1', label: 'Green and up' }, { value: '2', label: 'Orange and red' }, { value: '3', label: 'Red only' }] },
    ],
    legend: [{ color: ALERT.Green, label: 'Green alert' }, { color: ALERT.Orange, label: 'Orange alert' }, { color: ALERT.Red, label: 'Red alert' }],
    async enable() { addLayers(); },
    async disable() { map2.removeSource(SRC); },
    async refresh({ signal, options }) {
      const data = await kit.fetchReady('/api/hazards/disasters', { types: options.types || '', min_alert: options.minAlert || 1 }, signal, d => d.meta?.source?.pending);
      map2.setData(SRC, data);
      layer.setCount(data.features.length);
      const red = data.features.filter(f => f.properties.alert === 'Red' && f.properties.cur).length;
      const orange = data.features.filter(f => f.properties.alert === 'Orange' && f.properties.cur).length;
      const st = kit.statusFrom(layer, data.meta, 'No GDACS alerts match these settings.',
        `${red} red and ${orange} orange alerts in force`);
      if (st === 'pending') kit.retryLater(layer);
    },
  });

  map2.onClick(CLICKABLE, (f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    ctx.entities.open({ kind: p.kind, id: p.id, title: p.name, lat, lon, layer: 'hazards.disasters' }, { fly: false });
  });
  map2.onHover(CLICKABLE, (f) => {
    const p = f.properties;
    return `${p.alert} alert: ${p.short}${p.sev ? `, ${p.sev}` : ''}${+p.cur ? '' : ' (ended)'}`;
  });

  /** Draw the GDACS outline of an event while its card is open. */
  async function showArea(cardKey, id, alert) {
    try {
      const fc = await ctx.api.get('/api/hazards/disaster-geometry', { id });
      const feats = (fc.features || []).filter(f => f.geometry && f.geometry.type !== 'Point' && !/^Point_/.test(f.properties.role || ''));
      if (feats.length && ctx.cards.get(cardKey)) kit.select(cardKey, feats, ALERT[alert] || ALERT.Green);
    } catch { /* an outline is a bonus */ }
  }
  return { layer, showArea };
}
