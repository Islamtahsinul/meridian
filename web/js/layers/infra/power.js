// Power plants (WRI Global Power Plant Database): circles sized by capacity, coloured by fuel.

import { FUELS, FUEL_GROUPS, INK, INFRA, fuelColorExpr, fuelLabel, sqrtRadius } from './shared.js';

const SRC = 'ov-infra-power';
const SRC_MINOR = 'ov-infra-power-minor';
const L_MAJOR = 'ov-infra-power-c';
const L_MINOR = 'ov-infra-power-minor-c';
const L_LABEL = 'ov-infra-power-l';

export function registerPower(ctx, s) {
  const { h, fmt, map2 } = ctx;
  let minor = null;

  const radius = sqrtRadius('c', { z: [1.5, 5, 8, 12], base: [0.9, 1.5, 2.4, 3.6], k: [0.065, 0.11, 0.17, 0.25], max: 26 });
  const paint = {
    'circle-radius': radius,
    'circle-color': fuelColorExpr(),
    'circle-opacity': 0.86,
    'circle-stroke-color': INK,
    'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 2, 0.25, 6, 0.7, 10, 1.1],
    'circle-pitch-alignment': 'map',
  };

  // One filter per zoom band (top-level step on zoom): only big stations at world view.
  function filterFor(opts, bands = [[0, 400], [2.5, 150], [3.5, 50], [4.5, 20], [5.5, 0]]) {
    const fuel = opts.fuel || 'all';
    const fuelF = fuel in FUEL_GROUPS ? ['in', ['get', 'f'], ['literal', FUEL_GROUPS[fuel]]]
      : fuel !== 'all' ? ['==', ['get', 'f'], fuel] : true;
    const min = +opts.min || 0;
    const band = (mw) => ['all', fuelF, ['>=', ['get', 'c'], Math.max(min, mw)]];
    const expr = ['step', ['zoom'], band(bands[0][1])];
    for (const [z, mw] of bands.slice(1)) expr.push(z, band(mw));
    return expr;
  }
  const labelFilter = (opts) => filterFor(opts, [[0, 1e9], [7, 1000], [8, 300], [10, 50]]);

  function addMinorLayers() {
    map2.addLayer({ id: L_MINOR, type: 'circle', source: SRC_MINOR, minzoom: 5, paint, layout: { 'circle-sort-key': ['get', 'c'] },
      filter: filterFor(layer.options()) }, 'points');
  }

  async function show() {
    map2.addSource(SRC, { type: 'geojson', data: s.geoUrl('power') });
    map2.addLayer({ id: L_MAJOR, type: 'circle', source: SRC, paint, layout: { 'circle-sort-key': ['get', 'c'] },
      filter: filterFor(layer.options()) }, 'points');
    map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC, minzoom: 7,
      filter: labelFilter(layer.options()),
      layout: { 'text-field': ['get', 'n'], 'text-font': ['Noto Sans Regular'], 'text-size': 11.5, 'text-offset': [0, 0.9],
        'text-anchor': 'top', 'text-max-width': 9, 'text-optional': true, 'symbol-sort-key': ['-', 0, ['get', 'c']] },
      paint: { 'text-color': '#e7dcc0', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
    minor = s.lazySource(SRC_MINOR, s.geoUrl('power-minor'), 5, addMinorLayers);
    minor.start();
    const st = await s.status();
    layer.setCount(st.counts?.power ?? null);
  }

  const layer = ctx.layers.register({
    id: 'infra.power', group: 'Infrastructure', title: 'Power plants', subtitle: 'WRI Global Power Plant Database',
    description: 'About 35,000 power stations sized by capacity and coloured by primary fuel. Smaller plants appear as you zoom in.',
    source: 'WRI Global Power Plant Database v1.3.0 (CC BY 4.0)', color: '#e9a15f', symbol: 'point',
    options: [
      { id: 'fuel', label: 'Fuel', type: 'select', default: 'all', choices: [
        { value: 'all', label: 'All fuels' }, { value: 'fossil', label: 'Fossil fuels' }, { value: 'clean', label: 'Low carbon' },
        { value: 'renewable', label: 'Renewables' }, ...FUELS.filter(f => f.id !== 'other').map(f => ({ value: f.id, label: f.label }))] },
      { id: 'min', label: 'Smallest plant', type: 'range', min: 0, max: 2000, step: 50, default: 0, format: v => `${fmt.int(v)} MW` },
    ],
    onOption(_id, _v, all) {
      for (const id of [L_MAJOR, L_MINOR]) if (ctx.map.getLayer(id)) ctx.map.setFilter(id, filterFor(all));
      if (ctx.map.getLayer(L_LABEL)) ctx.map.setFilter(L_LABEL, labelFilter(all));
    },
    legend: FUELS.map(f => ({ color: f.color, label: f.label })),
    async enable() {
      if (await s.ensureReady('power', layer, show)) await show();
    },
    disable() { minor?.stop(); minor = null; map2.removeSource(SRC); },
  });

  map2.onClick([L_MAJOR, L_MINOR], (f) => {
    const [lon, lat] = f.geometry.coordinates;
    openPlant({ kind: 'power_plant', id: f.properties.id, title: f.properties.n, lat, lon, layer: 'infra.power' });
  });
  map2.onHover([L_MAJOR, L_MINOR], (f) => `${f.properties.n}, ${fuelLabel(f.properties.f)}, ${fmt.int(f.properties.c)} MW`);

  async function openPlant(entity) {
    let p;
    try { p = await s.get(s.entityPath('power_plant', entity.id)); } catch (err) { return s.missingCard(entity, 'power plant', INFRA, err); }
    const cap = s.power(p.capacity_mw);
    const gen = p.generation_gwh != null ? { label: `Generation ${p.generation_year}`, value: fmt.num(p.generation_gwh, 0), unit: 'GWh' }
      : p.estimated_generation_gwh != null ? { label: `Estimated ${p.estimated_generation_year}`, value: fmt.num(p.estimated_generation_gwh, 0), unit: 'GWh' } : null;
    const cf = p.capacity_mw && (p.generation_gwh ?? p.estimated_generation_gwh) != null
      ? ((p.generation_gwh ?? p.estimated_generation_gwh) * 1000) / (p.capacity_mw * 8760) * 100 : null;
    const fuels = [p.fuel_label, ...(p.other_fuels || [])].filter(Boolean);
    return ctx.cards.open({
      key: `power_plant:${p.id}`, kind: 'power_plant', title: p.name, subtitle: s.placeLine(fuels.map((f, i) => (i ? String(f).toLowerCase() : f)).join(' and '), p.country_name),
      icon: 'ϟ', accent: INFRA,
      badges: [p.capacity_mw >= 1000 ? { text: 'Major plant', tone: 'blue' } : null,
        p.fuel === 'nuclear' ? { text: 'Nuclear', tone: 'amber' } : null],
      block: [
        { label: 'Capacity', value: cap.value, unit: cap.unit },
        { label: 'Fuel', value: p.fuel_label },
        p.commissioning_year ? { label: 'Commissioned', value: String(p.commissioning_year) } : null,
        gen,
      ].filter(Boolean).slice(0, 4),
      sections: [
        { title: 'Plant', rows: [
          ['Owner', p.owner], ['Fuels', fuels.join(', ')], ['Country', p.country_name],
          ['Capacity data year', p.capacity_year], ['Reported generation', p.generation_gwh != null ? `${fmt.num(p.generation_gwh, 0)} GWh in ${p.generation_year}` : null],
          ['Estimated generation', p.estimated_generation_gwh != null ? `${fmt.num(p.estimated_generation_gwh, 0)} GWh in ${p.estimated_generation_year}` : null],
          ['Capacity factor', cf != null && cf > 0 && cf < 100 ? `${fmt.num(cf, 0)}% (from the generation figure)` : null],
          ['Position', fmt.latlon(p.lat, p.lon)], ['Position source', p.geolocation_source],
          ['GPPD id', p.id], ['WEPP id', p.wepp_id]] },
        { title: 'Nearby', nearby: { lat: p.lat, lon: p.lon, radius_km: 25, exclude: { kind: 'power_plant', id: p.id } } },
      ],
      links: [p.source_url ? { text: p.source ? `Source: ${p.source}` : 'Source record', href: p.source_url } : null,
        { text: 'About the database (WRI)', href: 'https://datasets.wri.org/dataset/globalpowerplantdatabase' }].filter(Boolean),
      entity: { ...p.entity, layer: 'infra.power' },
      foot: 'Global Power Plant Database v1.3.0, World Resources Institute (CC BY 4.0). Figures as reported up to 2021.',
    });
  }

  ctx.entities.handler('power_plant', openPlant, { group: 'Infrastructure' });
  return { openPlant };
}
