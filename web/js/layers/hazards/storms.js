// Tropical storms: forecast cones, wind areas, past and forecast tracks coloured by intensity,
// track points and a cyclone symbol at the current position (NOAA NHC, GDACS, NASA EONET).

import { GROUP, INK, STORM, RED, AMBER } from './kit.js';

const SRC = 'ov-hazards-storms';
const CLICKABLE = ['ov-hazards-storms-now', 'ov-hazards-storms-now-ring', 'ov-hazards-storms-pts'];

export function registerStorms(ctx, kit) {
  const { map2, fmt } = ctx;
  const catColour = ['match', ['get', 'cat'], -2, STORM['-2'], -1, STORM['-1'], 0, STORM[0], 1, STORM[1], 2, STORM[2], 3, STORM[3],
    4, STORM[4], 5, STORM[5], STORM['-2']];
  const clsColour = ['match', ['get', 'cls'], 'TD', STORM['-1'], 'TS', STORM[0], 'HU', STORM[3], 'TY', STORM[3], 'STY', STORM[5],
    'MH', STORM[4], '#c9d3e6'];
  const active = ['==', ['get', 'act'], 1];

  function addLayers() {
    map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: 'ov-hazards-storms-cone', type: 'fill', source: SRC, filter: ['==', ['get', 'role'], 'cone'],
      paint: { 'fill-color': '#e8eefc', 'fill-opacity': 0.1 } }, 'polygons');
    map2.addLayer({ id: 'ov-hazards-storms-cone-line', type: 'line', source: SRC, filter: ['==', ['get', 'role'], 'cone'],
      paint: { 'line-color': '#e8eefc', 'line-opacity': 0.55, 'line-width': 1.1, 'line-dasharray': [3, 2] } }, 'lines');
    map2.addLayer({ id: 'ov-hazards-storms-wind', type: 'fill', source: SRC, filter: ['==', ['get', 'role'], 'wind'],
      paint: { 'fill-color': ['match', ['get', 'kmh'], 120, RED, 90, '#ff8c42', AMBER],
        'fill-opacity': ['match', ['get', 'kmh'], 120, 0.2, 90, 0.14, 0.1] } }, 'polygons');
    map2.addLayer({ id: 'ov-hazards-storms-track-casing', type: 'line', source: SRC, filter: ['==', ['get', 'role'], 'track'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': INK, 'line-width': ['interpolate', ['linear'], ['zoom'], 2, 3.4, 8, 5.5], 'line-opacity': 0.55 } }, 'lines');
    map2.addLayer({ id: 'ov-hazards-storms-track', type: 'line', source: SRC,
      filter: ['all', ['==', ['get', 'role'], 'track'], ['==', ['get', 'fc'], 0]],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': clsColour, 'line-width': ['interpolate', ['linear'], ['zoom'], 2, 1.8, 8, 3.2],
        'line-opacity': ['case', active, 1, 0.5] } }, 'lines');
    map2.addLayer({ id: 'ov-hazards-storms-forecast', type: 'line', source: SRC,
      filter: ['all', ['==', ['get', 'role'], 'track'], ['==', ['get', 'fc'], 1]],
      layout: { 'line-cap': 'butt', 'line-join': 'round' },
      paint: { 'line-color': clsColour, 'line-width': ['interpolate', ['linear'], ['zoom'], 2, 1.6, 8, 2.8], 'line-dasharray': [2, 1.6],
        'line-opacity': ['case', active, 0.95, 0.45] } }, 'lines');
    map2.addLayer({ id: 'ov-hazards-storms-pts', type: 'circle', source: SRC, filter: ['==', ['get', 'role'], 'pt'],
      minzoom: 2.5,
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 2.5, 2, 8, 4],
        'circle-color': ['case', ['==', ['get', 'fc'], 1], INK, '#e8eefc'],
        'circle-stroke-color': '#e8eefc', 'circle-stroke-width': ['case', ['==', ['get', 'fc'], 1], 1.2, 0],
        'circle-opacity': ['case', active, 0.95, 0.5], 'circle-pitch-alignment': 'map' } }, 'points');
    map2.addLayer({ id: 'ov-hazards-storms-pt-label', type: 'symbol', source: SRC,
      filter: ['all', ['==', ['get', 'role'], 'pt'], ['!=', ['get', 'label'], '']], minzoom: 5,
      layout: { 'text-field': ['get', 'label'], 'text-font': ['Open Sans Regular'], 'text-size': 10, 'text-anchor': 'left',
        'text-offset': [0.8, 0], 'text-optional': true },
      paint: { 'text-color': '#c9d3e6', 'text-halo-color': INK, 'text-halo-width': 1.2 } }, 'top');
    map2.addLayer({ id: 'ov-hazards-storms-now-ring', type: 'circle', source: SRC, filter: ['==', ['get', 'role'], 'now'],
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 11, 6, 17], 'circle-color': catColour,
        'circle-opacity': ['case', active, 0.22, 0.08], 'circle-stroke-color': catColour, 'circle-stroke-width': 1.2,
        'circle-stroke-opacity': ['case', active, 0.9, 0.35], 'circle-pitch-alignment': 'map' } }, 'moving');
    map2.addLayer({ id: 'ov-hazards-storms-now', type: 'symbol', source: SRC, filter: ['==', ['get', 'role'], 'now'],
      layout: { 'icon-image': 'hazards-storm', 'icon-size': ['interpolate', ['linear'], ['zoom'], 1, 0.62, 6, 0.95],
        'icon-allow-overlap': true, 'icon-ignore-placement': true,
        'text-field': ['get', 'label'], 'text-font': ['Open Sans Bold'], 'text-size': 12, 'text-anchor': 'top',
        'text-offset': [0, 1.6], 'text-optional': true },
      paint: { 'icon-color': catColour, 'icon-halo-color': INK, 'icon-halo-width': 1.2,
        'icon-opacity': ['case', active, 1, 0.55], 'text-color': '#ebe6d6', 'text-halo-color': INK, 'text-halo-width': 1.5,
        'text-opacity': ['case', active, 1, 0.6] } }, 'moving');
  }

  const layer = ctx.layers.register({
    id: 'hazards.storms', group: GROUP, title: 'Tropical storms', subtitle: 'Hurricanes, typhoons and cyclones',
    description: 'Active tropical cyclones with past track, forecast track, cone of uncertainty and wind areas (60, 90 and 120 km/h).',
    source: 'NOAA National Hurricane Center (public domain), GDACS, NASA EONET', color: STORM[3], symbol: 'line',
    defaultOn: false, refreshMs: 600000, viewDependent: false,
    options: [{ id: 'ended', label: 'Include storms of the past week', type: 'toggle', default: false }],
    legend: [
      { color: STORM['-1'], label: 'Depression', shape: 'line' }, { color: STORM[0], label: 'Tropical storm', shape: 'line' },
      { color: STORM[1], label: 'Category 1' }, { color: STORM[3], label: 'Category 3' }, { color: STORM[5], label: 'Category 5' },
      { color: '#e8eefc', label: 'Forecast cone', shape: 'area' },
    ],
    async enable() { addLayers(); },
    async disable() { map2.removeSource(SRC); },
    async refresh({ signal, options }) {
      const data = await kit.fetchReady('/api/hazards/storms', { ended: options.ended ? 1 : '' }, signal, d => d.meta?.source?.gdacs?.pending && d.meta?.source?.nhc?.pending);
      map2.setData(SRC, data);
      const m = data.meta || {};
      layer.setCount(m.active ?? 0);
      const src = m.source || {};
      const err = [src.gdacs, src.nhc].filter(s => s && s.error && !s.updated_at);
      if (err.length === 2) { layer.setStatus('error', `Storm sources unavailable: ${err[0].error}`); return; }
      if (src.gdacs?.pending && src.nhc?.pending) { layer.setStatus('loading', 'Fetching storms…'); kit.retryLater(layer); return; }
      const names = (m.storms || []).filter(s => s.active).map(s => s.name);
      layer.setStatus('ok', names.length ? `Active: ${names.join(', ')}` : 'No active tropical cyclones right now.');
    },
  });

  map2.onClick(CLICKABLE, (f) => {
    const p = f.properties;
    const id = p.id || p.sid;
    const [lon, lat] = f.geometry.coordinates;
    ctx.entities.open({ kind: 'storm', id, title: p.title || p.name, lat, lon, layer: 'hazards.storms' }, { fly: false });
  });
  map2.onHover(CLICKABLE, (f) => {
    const p = f.properties;
    if (p.role === 'pt') return `${p.label || 'Track point'}${+p.fc ? ', forecast' : ''}`;
    const wind = +p.wind ? `, ${fmt.int(p.wind)} km/h` : '';
    return `${p.title}${wind}${+p.act ? '' : ' (no recent advisory)'}`;
  });
  return { layer };
}
