// "What's here?" (CONTRACT.md section 10.12): right-click anywhere for place name, coordinates,
// module-contributed sections (weather, country, ...) and every nearby entity.

import { h, fmt, copyText, clear, kindLabel } from './dom.js';
import { api } from './api.js';
import { open as openCard } from './cards.js';
import { ui } from './ui.js';
import { flyTo, getMap } from './map.js';

const sections = [];   // async (lat, lon) -> section | null

/** section(fn): fn(lat, lon) returns a card section ({title, rows|el|list|text}) or null. */
export function section(fn) {
  sections.push(fn);
}

export function mount() {
  const map = getMap();
  map.on('contextmenu', (e) => {
    e.preventDefault?.();
    const { lat, lng } = e.lngLat;
    const oe = e.originalEvent;
    ui.contextMenu(oe.clientX, oe.clientY, [
      { text: "What's here?", onClick: () => open(lat, lng) },
      { text: 'Copy coordinates', onClick: async () => ui.toast((await copyText(`${lat.toFixed(5)}, ${lng.toFixed(5)}`)) ? 'Coordinates copied' : 'Copy failed', 'green') },
      { text: 'Centre the map here', onClick: () => flyTo(lat, lng, map.getZoom()) },
    ], fmt.latlon(lat, lng, 4));
  });
}

export async function open(lat, lon, { exclude = null, radius_km = null } = {}) {
  const map = getMap();
  const z = map.getZoom();
  const radius = radius_km ?? (z >= 12 ? 3 : z >= 9 ? 10 : z >= 6 ? 40 : 120);
  const placeBox = h('p', { class: 'dim' }, 'Finding the place name…');
  const nearbyBox = h('div', null, h('p', { class: 'dim' }, 'Looking around…'));
  const extraBox = h('div');
  const card = openCard({
    key: 'whatshere', kind: 'whatshere', title: "What's here?", subtitle: fmt.latlon(lat, lon, 5), icon: '⌖', accent: 'var(--signal)',
    sections: [{ el: placeBox }, { el: extraBox }, { title: `Within ${fmt.km(radius)}`, el: nearbyBox }],
    entity: { kind: 'point', id: `${lat.toFixed(5)},${lon.toFixed(5)}`, title: `Point ${fmt.latlon(lat, lon, 3)}`, lat, lon },
    actions: [{ text: 'Copy coordinates', onClick: async () => ui.toast((await copyText(`${lat.toFixed(5)}, ${lon.toFixed(5)}`)) ? 'Coordinates copied' : 'Copy failed') }],
  });
  api.get('/api/core/whatshere', { lat: lat.toFixed(5), lon: lon.toFixed(5), radius_km: radius, zoom: Math.round(Math.min(18, Math.max(3, z))) }).then((res) => {
    clear(placeBox);
    const p = res.place;
    placeBox.append(p && p.name ? p.name : (p && p.note ? `No named place (${p.note}).` : 'No place name found.'));
    let items = res.nearby || [];
    if (exclude) items = items.filter(i => !(i.kind === exclude.kind && String(i.id) === String(exclude.id)));
    clear(nearbyBox);
    if (!items.length) { nearbyBox.append(h('p', { class: 'dim' }, 'Nothing from the loaded layers is this close.')); return; }
    const byKind = new Map();
    for (const it of items) { if (!byKind.has(it.kind)) byKind.set(it.kind, []); byKind.get(it.kind).push(it); }
    for (const [kind, list] of byKind) {
      nearbyBox.append(h('h3', { style: { margin: '8px 0 2px', fontSize: '12.5px', color: 'var(--paper-faint)' } }, `${kindLabel(kind, list.length !== 1)} (${list.length})`));
      const ul = h('ul', { class: 'card-list' });
      for (const it of list.slice(0, 6)) {
        const b = h('button', { type: 'button' }, h('span', { class: 'li-d' }, it.distance_km != null ? fmt.km(it.distance_km) : ''),
          h('span', { class: 'li-t' }, it.title || it.id), it.subtitle ? h('span', { class: 'li-s' }, it.subtitle) : null);
        b.addEventListener('click', () => import('./entities.js').then(m => m.entities.open(it)));
        ul.append(h('li', null, b));
      }
      nearbyBox.append(ul);
    }
  }).catch(() => { clear(placeBox); placeBox.append('Place lookup is unavailable right now.'); clear(nearbyBox); });
  for (const fn of sections) {
    Promise.resolve().then(() => fn(lat, lon)).then((sec) => {
      if (!sec || !card.isOpen()) return;
      const box = h('section', { class: 'card-section' }, sec.title ? h('h3', null, sec.title) : null);
      if (sec.rows) {
        const dl = h('dl', { class: 'kv' });
        for (const [k, v] of sec.rows) if (v != null && v !== '') dl.append(h('dt', null, k), h('dd', null, v instanceof Node ? v : String(v)));
        box.append(dl);
      }
      if (sec.text) box.append(h('p', null, sec.text));
      if (Array.isArray(sec.list)) {
        const ul = h('ul', { class: 'card-list' });
        for (const it of sec.list.slice(0, 12)) {
          if (!it) continue;
          const content = [it.detail ? h('span', { class: 'li-d' }, it.detail) : null, h('span', { class: 'li-t' }, it.title || ''),
            it.subtitle ? h('span', { class: 'li-s' }, it.subtitle) : null];
          if (it.entity || it.onClick) {
            const b = h('button', { type: 'button' }, content);
            b.addEventListener('click', () => (it.onClick ? it.onClick() : import('./entities.js').then(m => m.entities.open(it.entity))));
            ul.append(h('li', null, b));
          } else {
            ul.append(h('li', null, content));
          }
        }
        if (!sec.list.length && sec.empty) box.append(h('p', { class: 'dim' }, sec.empty));
        else box.append(ul);
      }
      if (sec.el) box.append(sec.el);
      extraBox.append(box);
    }).catch((err) => console.warn("what's here section failed", err));
  }
  return card;
}

export const whatshere = { section, open, mount };
