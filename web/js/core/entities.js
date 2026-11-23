// Entities: one way to open anything from search, nearby lists, favourites, case files and the AI
// (CONTRACT.md section 10.9). Modules register a handler per kind that opens the right card.

import { bus } from './bus.js';
import { fmt, validLatLon } from './dom.js';
import { open as openCard } from './cards.js';
import { flyTo, fitBbox } from './map.js';
import { layers, groupColor } from './layers.js';

const handlers = new Map();   // kind -> {open, group, icon}

/** handler(kind, open(entity) -> card|Promise, {group, icon}) */
export function handler(kind, fn, { group = null, icon = null } = {}) {
  handlers.set(kind, { open: fn, group, icon });
}

export function meta(kind) {
  return handlers.get(kind) || null;
}

/** entity: {kind, id, title, subtitle?, lat?, lon?, layer?, zoom?, bbox?} */
export async function open(entity, { fly = true } = {}) {
  if (!entity) return null;
  if (entity.layer && layers.get(entity.layer) && !layers.isOn(entity.layer)) {
    try { await layers.setOn(entity.layer, true); } catch { /* ignore */ }
  }
  if (fly) {
    if (Array.isArray(entity.bbox) && entity.bbox.length === 4) fitBbox(entity.bbox);
    else if (validLatLon(entity.lat, entity.lon)) flyTo(+entity.lat, +entity.lon, entity.zoom || defaultZoom(entity.kind));
  }
  bus.emit('entity:opened', { entity });
  const h = handlers.get(entity.kind);
  if (h) {
    try { return await h.open(entity); } catch (err) { console.error(`entity handler for ${entity.kind} failed`, err); }
  }
  return genericCard(entity);
}

function defaultZoom(kind) {
  if (kind === 'country') return 4;
  if (kind === 'place') return 11;
  return 10;
}

export function genericCard(entity) {
  if (entity.kind === 'place' && !entity.subtitle) return null;
  const rows = [];
  if (validLatLon(entity.lat, entity.lon)) rows.push(['Position', fmt.latlon(+entity.lat, +entity.lon)]);
  if (entity.distance_km != null) rows.push(['Distance', fmt.km(entity.distance_km)]);
  if (entity.time) rows.push(['Time', fmt.utc(entity.time)]);
  for (const [k, v] of Object.entries(entity.fields || {})) rows.push([k, v]);
  return openCard({
    key: `${entity.kind}:${entity.id}`,
    kind: entity.kind,
    title: entity.title || entity.kind,
    subtitle: entity.subtitle || '',
    icon: entity.kind === 'place' ? '⌖' : '•',
    accent: entity.layer ? groupColor(layers.get(entity.layer)?.spec.group) : 'var(--paper-faint)',
    sections: [{ rows }],
    entity: validLatLon(entity.lat, entity.lon) ? entity : null,
  });
}

export const entities = { handler, open, meta, genericCard };
