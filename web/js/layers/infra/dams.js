// Dams (Wikidata): notable dams sized by height (or reservoir volume when the height is unknown).

import { INFRA, INK } from './shared.js';

const SRC = 'ov-infra-dams';
const L_DOT = 'ov-infra-dams-c';
const L_LBL = 'ov-infra-dams-l';
export const DAM = '#7ec7ee';

export function registerDams(ctx, s) {
  const { fmt, map2 } = ctx;
  // Size: height in metres, else a volume-based stand-in, else a small default.
  const size = ['coalesce', ['get', 'h'], ['case', ['has', 'v'], ['*', 12, ['log10', ['max', 1, ['to-number', ['get', 'v'], 1]]]], 8]];
  const zoomFilter = ['step', ['zoom'],
    ['any', ['>=', ['coalesce', ['get', 'h'], 0], 100], ['>=', ['coalesce', ['get', 'v'], 0], 3000]], 3.5,
    ['any', ['>=', ['coalesce', ['get', 'h'], 0], 40], ['>=', ['coalesce', ['get', 'v'], 0], 300], ['has', 'p']], 5.5, true];

  async function show() {
    map2.addSource(SRC, { type: 'geojson', data: s.geoUrl('dams') });
    map2.addLayer({ id: L_DOT, type: 'circle', source: SRC, filter: zoomFilter, layout: { 'circle-sort-key': size }, paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'],
        2, ['min', 7, ['+', 1.4, ['*', 0.02, size]]], 6, ['min', 11, ['+', 2.1, ['*', 0.035, size]]],
        10, ['min', 15, ['+', 3, ['*', 0.05, size]]]],
      'circle-color': ['case', ['has', 'p'], DAM, 'rgba(126, 199, 238, 0.7)'],
      'circle-stroke-color': ['case', ['has', 'p'], '#e8f6ff', INK],
      'circle-stroke-width': ['case', ['has', 'p'], 1, 0.6] } }, 'points');
    map2.addLayer({ id: L_LBL, type: 'symbol', source: SRC, minzoom: 6,
      filter: ['step', ['zoom'], ['>=', ['coalesce', ['get', 'h'], 0], 120], 8, ['>=', ['coalesce', ['get', 'h'], 0], 40], 10, true],
      layout: { 'text-field': ['get', 'n'], 'text-font': ['Noto Sans Regular'], 'text-size': 11, 'text-offset': [0, 0.9],
        'text-anchor': 'top', 'text-max-width': 9, 'text-optional': true, 'symbol-sort-key': ['-', 0, size] },
      paint: { 'text-color': '#cdeafa', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
    const st = await s.status();
    layer.setCount(st.counts?.dams ?? null);
  }

  const layer = ctx.layers.register({
    id: 'infra.dams', group: 'Infrastructure', title: 'Dams', subtitle: 'Wikidata',
    description: 'Notable dams sized by height. Dams with hydropower have a light outline. Lower dams appear as you zoom in.',
    source: 'Wikidata (CC0)', color: DAM, symbol: 'point',
    legend: [{ color: DAM, label: 'Dam with hydropower' }, { color: 'rgba(126,199,238,.7)', label: 'Other dam' }],
    async enable() { if (await s.ensureReady('dams', layer, show)) await show(); },
    disable() { map2.removeSource(SRC); },
  });

  map2.onClick([L_DOT], (f) => {
    const [lon, lat] = f.geometry.coordinates;
    openDam({ kind: 'dam', id: f.properties.id, title: f.properties.n, lat, lon, layer: 'infra.dams' });
  });
  map2.onHover([L_DOT], (f) => `${f.properties.n}${f.properties.h ? `, ${fmt.int(f.properties.h)} m high` : ''}`);

  function volume(mcm) {
    if (mcm == null) return null;
    if (mcm >= 1000) return { value: fmt.num(mcm / 1000, 1), unit: 'km³' };
    return { value: fmt.num(mcm, mcm < 10 ? 1 : 0), unit: 'million m³' };
  }

  async function openDam(entity) {
    let d;
    try { d = await s.get(s.entityPath('dam', entity.id)); } catch (err) { return s.missingCard(entity, 'dam', INFRA, err); }
    const vol = volume(d.reservoir_mcm);
    const own = volume(d.volume_mcm);
    const pw = d.power_mw != null ? s.power(d.power_mw) : null;
    const water = d.river || d.water;
    return ctx.cards.open({
      key: `dam:${d.id}`, kind: 'dam', title: d.name, subtitle: s.placeLine(water ? `On the ${water.replace(/^the /i, '')}` : 'Dam', d.country_name),
      icon: '≋', accent: INFRA,
      badges: [d.height_m >= 15 ? { text: 'Large dam (15 m or more)', tone: 'grey' } : null, d.power_mw ? { text: 'Hydropower', tone: 'blue' } : null],
      block: [
        { label: 'Height', value: d.height_m != null ? fmt.num(d.height_m, 0) : '–', unit: d.height_m != null ? 'm' : '' },
        vol ? { label: 'Reservoir', value: vol.value, unit: vol.unit } : null,
        pw ? { label: 'Power', value: pw.value, unit: pw.unit } : null,
        { label: 'Built', value: d.year ? String(d.year) : '–' },
        // Many dams lack height and volume in Wikidata: the length keeps the block at two readings or more.
        d.length_m != null ? { label: 'Length', value: fmt.int(d.length_m), unit: 'm' } : null,
      ].filter(Boolean).slice(0, 4),
      sections: [
        { title: 'Dam', rows: [['River', d.river], ['Water body', d.water], ['Reservoir', d.reservoir], ['Operator', d.operator], ['Uses', (d.uses || []).join(', ')],
          ['Length', d.length_m != null ? `${fmt.int(d.length_m)} m` : null],
          ['Volume in Wikidata', own ? `${own.value} ${own.unit} (the dam or its reservoir, as entered)` : null], ['Country', d.country_name], ['Position', fmt.latlon(d.lat, d.lon)]] },
        { title: 'Nearby', nearby: { lat: d.lat, lon: d.lon, radius_km: 30, exclude: { kind: 'dam', id: d.id } } },
      ],
      links: [d.wikipedia ? { text: 'Wikipedia', href: d.wikipedia } : null, { text: 'Wikidata', href: d.wikidata }].filter(Boolean),
      entity: { ...d.entity, layer: 'infra.dams' },
      foot: 'From Wikidata (CC0). Heights and volumes are as entered there and can be incomplete.',
    });
  }

  ctx.entities.handler('dam', openDam, { group: 'Infrastructure' });
  return { openDam };
}
