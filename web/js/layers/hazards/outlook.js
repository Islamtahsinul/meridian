// GloFAS flood outlook (Copernicus EMS): the global river flood forecast for days 1 to 3, 4 to 10
// or 11 to 15 as a raster overlay, plus the reporting points (numbers are the share of forecast
// runs that exceed the flood level; triangles show a rising or falling trend). Tiles come through
// the hazards server, which turns them into WMS requests and caches them for six hours.

import { GROUP } from './kit.js';

const SRC = 'ov-hazards-outlook';
const SRC_POINTS = 'ov-hazards-outlook-points';
const WINDOWS = { 'days1-3': 'days 1 to 3', 'days4-10': 'days 4 to 10', 'days11-15': 'days 11 to 15' };
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const LEGEND = [{ color: '#a800d6', label: 'Above a 20-year flood', shape: 'area' }, { color: '#e60000', label: '5 to 20-year flood', shape: 'area' },
  { color: '#ffff00', label: '2 to 5-year flood', shape: 'area' }, { color: '#f3f0a6', label: 'Possible 2-year flood', shape: 'area' }];

export function registerOutlook(ctx, kit) {
  const { map2, fmt } = ctx;
  let shown = { product: null, date: null, points: null };

  const tileUrl = (product, date) =>
    `${location.origin}/api/hazards/flood-outlook/tile/${encodeURIComponent(product)}/{z}/{x}/{y}${date ? `?t=${encodeURIComponent(date)}` : ''}`;

  function place(id, url, opacity, maxzoom, resampling) {
    if (ctx.map.getSource(id)) map2.removeSource(id);
    map2.addSource(id, { type: 'raster', tiles: [url], tileSize: 256, minzoom: 0, maxzoom,
      attribution: 'Generated using Copernicus Emergency Management Service information (GloFAS)' });
    map2.addLayer({ id, type: 'raster', source: id,
      paint: { 'raster-opacity': opacity, 'raster-fade-duration': 150, 'raster-resampling': resampling } }, 'polygons');
  }

  function clear() {
    for (const id of [SRC, SRC_POINTS]) if (ctx.map.getSource(id)) map2.removeSource(id);
    shown = { product: null, date: null, points: null };
  }

  const layer = ctx.layers.register({
    id: 'hazards.flood_outlook', group: GROUP, title: 'Flood outlook', subtitle: 'GloFAS, Copernicus EMS',
    description: 'Where rivers may rise above their 2, 5 or 20-year flood levels in the coming days, from the GloFAS global ensemble forecast. Darker shades are more likely. This is a forecast signal, not a flood warning: official warnings come from national services.',
    source: 'GloFAS, Copernicus Emergency Management Service (generated using CEMS information, free reuse)', color: '#a800d6', symbol: 'area',
    defaultOn: false, refreshMs: 3600000, viewDependent: false,
    options: [
      { id: 'window', label: 'Forecast', type: 'select', default: 'days1-3', choices: [
        { value: 'days1-3', label: 'Days 1 to 3' }, { value: 'days4-10', label: 'Days 4 to 10' }, { value: 'days11-15', label: 'Days 11 to 15' }] },
      { id: 'points', label: 'Reporting points', type: 'toggle', default: true },
      { id: 'opacity', label: 'Opacity', type: 'range', min: 30, max: 100, step: 10, default: 90, format: v => `${v}%`, refresh: false },
    ],
    onOption(id, value) {
      if (id === 'opacity' && ctx.map.getLayer(SRC)) ctx.map.setPaintProperty(SRC, 'raster-opacity', value / 100);
    },
    legend: [...LEGEND, { color: '#8c8c8c', label: 'Reporting point, no signal' }],
    async enable() { clear(); },
    async disable() { clear(); },
    async refresh({ signal, options }) {
      const meta = await kit.fetchReady('/api/hazards/flood-outlook', null, signal, d => d.source?.pending, { tries: 5, delay: 2000 });
      const date = meta.date || null;
      const product = WINDOWS[options.window] ? options.window : 'days1-3';
      const opacity = (options.opacity ?? 90) / 100;
      if (shown.product !== product || shown.date !== date || !ctx.map.getSource(SRC)) {
        place(SRC, tileUrl(product, date), opacity, 8, 'nearest');
        shown.product = product;
        shown.date = date;
        shown.points = null;      // keep the points above the summary
      }
      if (options.points !== false) {
        if (shown.points !== date || !ctx.map.getSource(SRC_POINTS)) {
          place(SRC_POINTS, tileUrl('points', date), 1, 10, 'linear');
          shown.points = date;
        }
      } else if (ctx.map.getSource(SRC_POINTS)) {
        map2.removeSource(SRC_POINTS);
        shown.points = null;
      }
      layer.setCount(null);
      const when = date ? `Forecast of ${words(date)}` : 'Latest forecast';
      const text = `${when}, ${WINDOWS[product]}. ${meta.credit || ''}`.trim();
      if (meta.tile_errors) {
        layer.setStatus('warn', `${text} GloFAS did not answer for ${fmt.int(meta.tile_errors)} tile${meta.tile_errors === 1 ? '' : 's'} in the past 10 minutes.`);
      } else if (meta.source?.error && !date) {
        layer.setStatus('warn', `${text} The forecast date could not be read (${meta.source.error}).`);
      } else {
        layer.setStatus('ok', text);
      }
    },
  });
  return { layer };
}

function words(date) {
  const [y, m, d] = String(date).split('-').map(Number);
  return y && m && d ? `${d} ${MONTHS[m - 1]} ${y}` : String(date);
}
