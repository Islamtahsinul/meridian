// Ships: live AIS vessels, dark-ship estimates, sanctioned vessels, straits monitor, port activity, maritime
// anomaly candidates and IMF PortWatch chokepoint transits.
// Backend: meridian/modules/ships. Browser parts live in ./ships/*.js.

import { registerIcons, pointInGeometry } from './ships/common.js';
import { liveLayer, trails, darkLayer, sanctionedLayer, straitsLayer, straitCache } from './ships/maplayers.js';
import { openShip, openStrait, openSanctionRecord } from './ships/cards.js';
import { anomaliesLayer, openAnomaly } from './ships/anomalies.js';
import { overview } from './ships/drawer.js';

const ANCHOR_ICON = 'M10 3.2a1.6 1.6 0 1 1 0 3.2 1.6 1.6 0 0 1 0-3.2ZM10 6.4V17M6.8 9h6.4M3.8 12.2c.5 2.8 3.1 4.8 6.2 4.8s5.7-2 6.2-4.8';

export async function register(ctx) {
  if (!ctx.moduleOk('ships')) {
    console.warn('ships: the backend module is not loaded; vessel layers are unavailable');
  }
  registerIcons(ctx.map2);

  const trail = trails(ctx);
  const open = (entity) => openShip(ctx, entity, trail);
  const openEntity = (entity) => ctx.entities.open(entity);

  liveLayer(ctx, { onOpen: open });
  darkLayer(ctx, { onOpen: open });
  sanctionedLayer(ctx, { onOpen: open });
  const straits = straitCache(ctx);
  straitsLayer(ctx, { onOpen: (e) => openStrait(ctx, e), cache: straits });
  anomaliesLayer(ctx, { trail });

  ctx.entities.handler('ship', (entity) => openShip(ctx, entity, trail), { group: 'Ships', icon: '⚓︎' });
  ctx.entities.handler('strait', (entity) => openStrait(ctx, entity), { group: 'Ships', icon: '⇄' });
  ctx.entities.handler('sanctioned_vessel', (entity) => openSanctionRecord(ctx, entity, trail), { group: 'Ships', icon: '⚓︎' });
  // GNSS spoofing clusters seen by ships are gps_jam entities with ids "ships:<lat>,<lon>"; the flights module owns
  // the kind (aircraft cells), so wrap its handler and keep every other id going to it.
  const gpsPrev = ctx.entities.meta('gps_jam');
  ctx.entities.handler('gps_jam', (entity) => {
    const id = String(entity?.id || '');
    if (id.startsWith('ships:')) return openAnomaly(ctx, { id: `gnss:${id.slice(6)}`, title: entity.title }, trail);
    return gpsPrev ? gpsPrev.open(entity) : ctx.entities.genericCard(entity);
  }, { group: gpsPrev?.group || 'Ships', icon: gpsPrev?.icon || '⌁' });

  // The drawer reports open and closed (including its own close button) so the toolbar button stays in step.
  let btn = null;
  const panel = overview(ctx, { straits, openEntity, trail, onState: (open) => btn?.setAttribute('aria-pressed', String(open)) });
  btn = ctx.ui.toolbarButton({ id: 'ships', title: 'Maritime overview: straits, chokepoints, ports, anomalies, dark and sanctioned ships', label: 'Maritime',
    icon: ANCHOR_ICON, order: 30, onClick: () => panel.toggle() });

  ctx.ai.register('show_vessel_trail', async ({ mmsi }) => {
    if (!/^\d{9}$/.test(String(mmsi || ''))) throw new Error('show_vessel_trail needs a nine-digit mmsi');
    return trail.show(String(mmsi));
  });

  // Straits by name ("Hormuz", "Bosphorus", "Gulf of Finland"): the server search does not list them.
  ctx.search.provider(async (q) => {
    const ql = q.trim().toLowerCase();
    if (ql.length < 3 || !ctx.moduleOk('ships')) return [];
    let list = straits.list();
    if (!list.length) {
      try { list = await straits.load(); } catch { return []; }
    }
    const out = [];
    for (const s of list) {
      const name = String(s.name || '').toLowerCase();
      const words = name.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
      const score = name === ql || name.startsWith(`${ql},`) ? 0.95
        : name.startsWith(ql) || words.some(w => w.startsWith(ql)) ? 0.85
          : ql.length >= 4 && name.includes(ql) ? 0.6 : 0;
      if (!score) continue;
      const [lon, lat] = Array.isArray(s.label) ? s.label : [];
      out.push({ kind: 'strait', id: s.id, title: s.name, layer: 'ships.straits', score, lat, lon, bbox: s.bbox,
        subtitle: s.coverage ? `Strait monitor, ${ctx.fmt.int(s.inside_now)} vessel${s.inside_now === 1 ? '' : 's'} inside now` : 'Strait monitor, no live AIS coverage' });
    }
    return out.sort((a, b) => b.score - a.score).slice(0, 5);
  });

  ctx.whatshere.section(async (lat, lon) => {
    let list = straits.list();
    if (!list.length) {
      try { list = await straits.load(); } catch { return null; }
    }
    const s = list.find(x => pointInGeometry(lon, lat, x.geometry));
    if (!s) return null;
    const rows = s.coverage
      ? [['Strait', s.name], ['Inside now', ctx.fmt.int(s.inside_now)], ['Transits in 24 hours', ctx.fmt.int(s.transits_24h)]]
      : [['Strait', s.name], ['Coverage', 'No live AIS here']];
    return { title: 'Shipping', rows };
  });
}
