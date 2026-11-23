// Official weather warnings: US NWS alerts (storm polygons and forecast/county/marine zones) and
// MeteoAlarm warnings for 39 European countries, filled by severity. Upcoming warnings are lighter
// with a dashed edge. World view loads simplified outlines once; zoomed in, detailed outlines for
// the view.

import { GROUP, INK, SEVERITY, SEVERITY_NAME, warningName } from './kit.js';

const SRC = 'ov-hazards-warnings';
const CLICKABLE = ['ov-hazards-warnings-fill'];

export function registerWarnings(ctx, kit) {
  const { map2, fmt } = ctx;
  const sevColour = ['match', ['get', 'sev'], 4, SEVERITY[4], 3, SEVERITY[3], 2, SEVERITY[2], 1, SEVERITY[1], '#9aa6bd'];
  let loaded = null;   // {level, at, key}

  function addLayers() {
    map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: 'ov-hazards-warnings-fill', type: 'fill', source: SRC,
      layout: { 'fill-sort-key': ['get', 'sev'] },
      paint: { 'fill-color': sevColour,
        'fill-opacity': ['case', ['==', ['get', 'up'], 1], 0.1, ['interpolate', ['linear'], ['get', 'sev'], 1, 0.18, 4, 0.34]] } }, 'polygons');
    map2.addLayer({ id: 'ov-hazards-warnings-line', type: 'line', source: SRC, filter: ['==', ['get', 'up'], 0],
      layout: { 'line-join': 'round' },
      paint: { 'line-color': sevColour, 'line-width': ['interpolate', ['linear'], ['zoom'], 2, 0.5, 7, 1.3], 'line-opacity': 0.85 } }, 'lines');
    map2.addLayer({ id: 'ov-hazards-warnings-line-up', type: 'line', source: SRC, filter: ['==', ['get', 'up'], 1],
      paint: { 'line-color': sevColour, 'line-width': 1, 'line-opacity': 0.7, 'line-dasharray': [2, 2] } }, 'lines');
    map2.addLayer({ id: 'ov-hazards-warnings-label', type: 'symbol', source: SRC, minzoom: 6,
      layout: { 'text-field': ['get', 'ev'], 'text-font': ['Open Sans Regular'], 'text-size': 10.5, 'symbol-placement': 'point',
        'text-max-width': 9, 'text-optional': true, 'symbol-sort-key': ['-', 5, ['get', 'sev']] },
      paint: { 'text-color': '#ebe6d6', 'text-halo-color': INK, 'text-halo-width': 1.3 } }, 'top');
  }

  const layer = ctx.layers.register({
    id: 'hazards.warnings', group: GROUP, title: 'Weather warnings', subtitle: 'US National Weather Service and MeteoAlarm',
    description: 'Warnings, watches and advisories from the NWS (US and its waters) and 39 European weather services, including the UK Met Office.',
    source: 'NOAA/NWS (public domain), MeteoAlarm and the national services (CC BY 4.0 terms)', color: SEVERITY[3], symbol: 'area',
    defaultOn: false, refreshMs: 300000, viewDependent: true,
    options: [
      { id: 'min', label: 'Lowest severity', type: 'select', default: '2', choices: [
        { value: '1', label: 'Minor and up' }, { value: '2', label: 'Moderate and up' }, { value: '3', label: 'Severe and extreme' },
        { value: '4', label: 'Extreme only' }] },
      { id: 'src', label: 'Services', type: 'select', default: 'nws,meteoalarm', choices: [
        { value: 'nws,meteoalarm', label: 'US and Europe' }, { value: 'nws', label: 'US (NWS) only' },
        { value: 'meteoalarm', label: 'Europe (MeteoAlarm) only' }] },
    ],
    onOption() { loaded = null; },
    legend: [1, 2, 3, 4].map(k => ({ color: SEVERITY[k], label: SEVERITY_NAME[k], shape: 'area' })),
    async enable() { addLayers(); loaded = null; },
    async disable() { map2.removeSource(SRC); loaded = null; },
    async refresh({ bounds, zoom, signal, options, reason }) {
      const level = zoom >= 5 ? 'fine' : 'coarse';
      const key = `${options.min}|${options.src}`;
      const fresh = loaded && Date.now() - loaded.at < 240000 && loaded.key === key;
      // simplified world outlines are loaded once; only detailed views follow the map
      if (reason === 'view' && fresh && level === 'coarse' && loaded.level === 'coarse') return;
      if (reason === 'view' && fresh && level === 'fine' && loaded.level === 'fine' && inside(bounds, loaded.bbox)) return;
      const box = level === 'coarse' ? null : expand(bounds);
      const bbox = box ? box.join(',') : null;
      const params = { bbox, zoom: zoom.toFixed(1), min: options.min || 2, src: options.src };
      const data = await kit.fetchReady('/api/hazards/warnings', params, signal,
        d => (d.meta?.source?.nws?.pending && d.meta?.source?.meteoalarm?.pending) || (!d.meta?.nws_full && !d.meta?.ma_countries), { tries: 6, delay: 2500 });
      map2.setData(SRC, data);
      loaded = { level, at: Date.now(), key, bbox: box };
      const m = data.meta || {};
      layer.setCount(data.features.length);
      const src = m.source || {};
      if (src.nws?.error && src.meteoalarm?.error) { layer.setStatus('error', `Warning services unavailable: ${src.nws.error}`); return; }
      const loadingEurope = m.ma_countries < m.ma_wanted && src.meteoalarm?.pending;
      const pending = [];
      if (loadingEurope) pending.push(`MeteoAlarm, ${m.ma_countries} of ${m.ma_wanted} countries so far`);
      if (!m.nws_full) pending.push('NWS, severe and extreme alerts so far');
      if (m.zones_pending) pending.push(`${fmt.int(m.zones_pending)} US zone outline${+m.zones_pending === 1 ? '' : 's'}`);
      if (!m.regions && src.regions && !src.regions.updated_at) pending.push('European region outlines');
      const busy = m.zones_pending || loadingEurope || !m.nws_full;
      const counts = `${fmt.int(m.nws)} NWS alerts and ${fmt.int(m.meteoalarm)} MeteoAlarm warnings.`;
      layer.setStatus(busy ? 'warn' : 'ok', pending.length ? `${counts} Still loading ${pending.join('; ')}.` : counts);
      if (busy) { loaded = null; kit.retryLater(layer, loadingEurope || !m.nws_full ? 10000 : 25000); }
    },
  });

  function inside(b, outer) {
    if (!outer) return false;
    return b[0] >= outer[0] && b[1] >= outer[1] && b[2] <= outer[2] && b[3] <= outer[3] && b[0] <= b[2];
  }

  function expand(b) {
    const [w, s, e, n] = b;
    const dx = Math.min(10, (e - w) * 0.25);
    const dy = Math.min(8, (n - s) * 0.25);
    return [Math.max(-180, w - dx), Math.max(-85, s - dy), Math.min(180, e + dx), Math.min(85, n + dy)].map(v => +v.toFixed(3));
  }

  map2.onClick(CLICKABLE, (f, lngLat) => {
    const p = f.properties;
    ctx.entities.open({ kind: 'warning', id: p.id, title: warningName(p.ev), subtitle: p.area, lat: lngLat.lat, lon: lngLat.lng,
      layer: 'hazards.warnings', others: String(p.ids || '').split(',').filter(x => x && x !== p.id) }, { fly: false });
  });
  map2.onHover(CLICKABLE, (f) => {
    const p = f.properties;
    const more = +p.n > 1 ? ` (+${+p.n - 1} more)` : '';
    const when = +p.up ? `from ${fmt.utc(+p.on)}` : +p.exp ? `until ${fmt.utc(+p.exp)}` : '';
    return `${warningName(p.ev)}${more}: ${p.area}${when ? `, ${when}` : ''}`;
  });
  return { layer };
}
