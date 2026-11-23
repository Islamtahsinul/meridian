// Data centres (PeeringDB facilities plus OpenStreetMap when Overpass answers), sized by the number
// of networks present.

import { INFRA, INK, sqrtRadius } from './shared.js';

const SRC = 'ov-infra-dc';
const L_DOT = 'ov-infra-dc-c';
const L_LBL = 'ov-infra-dc-l';
// Cool white: the old lilac could not be told apart from nuclear plants when both layers were on.
export const DC = '#dfe6f5';

export function registerDatacenters(ctx, s) {
  const { h, fmt, map2 } = ctx;
  const zoomFilter = ['step', ['zoom'], ['>=', ['get', 'k'], 40], 3, ['>=', ['get', 'k'], 12], 4.5, ['>=', ['get', 'k'], 3], 6, true];

  async function show() {
    map2.addSource(SRC, { type: 'geojson', data: s.geoUrl('datacenters') });
    map2.addLayer({ id: L_DOT, type: 'circle', source: SRC, filter: zoomFilter, layout: { 'circle-sort-key': ['get', 'k'] }, paint: {
      'circle-radius': sqrtRadius('k', { z: [2, 6, 10, 14], base: [1.3, 2, 3, 4.2], k: [0.13, 0.22, 0.3, 0.38], max: 16 }),
      'circle-color': ['case', ['has', 'o'], 'rgba(223, 230, 245, 0.5)', DC], 'circle-opacity': 0.88,
      'circle-stroke-color': INK, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 2, 0.4, 8, 1] } }, 'points');
    map2.addLayer({ id: L_LBL, type: 'symbol', source: SRC, minzoom: 9,
      filter: ['step', ['zoom'], ['>=', ['get', 'k'], 30], 11, ['>=', ['get', 'k'], 5], 13, true],
      layout: { 'text-field': ['get', 'n'], 'text-font': ['Noto Sans Regular'], 'text-size': 11, 'text-offset': [0, 0.9],
        'text-anchor': 'top', 'text-max-width': 10, 'text-optional': true, 'symbol-sort-key': ['-', 0, ['get', 'k']] },
      paint: { 'text-color': '#e8edf8', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
    const st = await s.status();
    layer.setCount(st.counts?.datacenters ?? null);
  }

  const layer = ctx.layers.register({
    id: 'infra.datacenters', group: 'Infrastructure', title: 'Data centres', subtitle: 'PeeringDB facilities',
    description: 'Colocation and interconnection facilities, sized by how many networks are present. Busy hubs show first; smaller sites appear as you zoom in.',
    source: 'PeeringDB; OpenStreetMap contributors (ODbL) when Overpass answers', color: DC, symbol: 'point',
    legend: [{ color: DC, label: 'PeeringDB facility' }, { color: 'rgba(223,230,245,.5)', label: 'OpenStreetMap only' }],
    async enable() { if (await s.ensureReady('datacenters', layer, show)) await show(); },
    disable() { map2.removeSource(SRC); },
  });

  map2.onClick([L_DOT], (f) => {
    const [lon, lat] = f.geometry.coordinates;
    openDc({ kind: 'datacenter', id: f.properties.id, title: f.properties.n, lat, lon, layer: 'infra.datacenters' });
  });
  map2.onHover([L_DOT], (f) => `${f.properties.n}${f.properties.k ? `, ${fmt.int(f.properties.k)} networks` : ''}`);

  async function openDc(entity) {
    let d;
    try { d = await s.get(s.entityPath('datacenter', entity.id)); } catch (err) { return s.missingCard(entity, 'data centre', INFRA, err); }
    const pdb = d.source === 'PeeringDB';
    return ctx.cards.open({
      key: `datacenter:${d.id}`, kind: 'datacenter', title: d.name, subtitle: s.placeLine(d.operator, d.city, d.country_name || d.country),
      icon: '▦', accent: INFRA,
      badges: [{ text: d.source, tone: pdb ? 'blue' : 'grey' }, d.networks >= 100 ? { text: 'Major interconnection hub', tone: 'blue' } : null],
      block: pdb ? [
        { label: 'Networks', value: fmt.int(d.networks) },
        { label: 'Exchanges', value: fmt.int(d.exchanges) },
        { label: 'Carriers', value: fmt.int(d.carriers) },
      ] : [{ label: 'Operator', value: d.operator || 'Unknown' }, { label: 'City', value: d.city || '–' }],
      sections: [
        { title: 'Facility', rows: [['Operator', d.operator], ['Also known as', d.aka], ['Address', s.placeLine(d.address, d.zipcode)],
          ['City', s.placeLine(d.city, d.state)], ['Country', d.country_name || d.country], ['Region', d.region], ['CLLI code', d.clli],
          ['Property', d.property], ['Power', (d.voltage || []).length ? d.voltage.join(', ') : null], ['Diverse substations', d.substations],
          ['Updated', d.updated ? fmt.utc(d.updated) : null], ['Position', fmt.latlon(d.lat, d.lon)]] },
        d.notes ? { title: 'Notes', text: d.notes } : null,
        d.campus?.length ? { title: 'Same campus', list: d.campus.map(c => ({ title: c.title, subtitle: c.subtitle, detail: fmt.km(c.distance_km), entity: c })) } : null,
        { title: 'Nearby', nearby: { lat: d.lat, lon: d.lon, radius_km: 10, exclude: { kind: 'datacenter', id: d.id } } },
      ],
      links: [pdb ? { text: 'PeeringDB record', href: `https://www.peeringdb.com/fac/${d.pdb_id}` } : null,
        d.osm_url ? { text: 'OpenStreetMap', href: d.osm_url } : null, d.website ? { text: 'Website', href: d.website } : null].filter(Boolean),
      entity: { ...d.entity, layer: 'infra.datacenters' },
      foot: pdb ? 'Facility data from PeeringDB, maintained by the networks themselves.' : 'Mapped by OpenStreetMap contributors (ODbL).',
    });
  }

  ctx.entities.handler('datacenter', openDc, { group: 'Infrastructure' });
  return { openDc };
}
