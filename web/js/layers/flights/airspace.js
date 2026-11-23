// Airspace and airport status: registers the delay, restriction and conflict-zone layers and adds an
// "Airspace" section to the right-click "What's here?" card.

import * as delays from './delays.js';
import * as restrictions from './restrictions.js';
import * as advisories from './advisories.js';

// The "What's here?" card takes rows, text or an element; this builds the same list markup the
// cards use, each entry opening its card.
function listEl(ctx, items) {
  const { h } = ctx;
  const ul = h('ul', { class: 'card-list' });
  for (const it of items) {
    const b = h('button', { type: 'button' }, h('span', { class: 'li-d' }, it.detail || ''), h('span', { class: 'li-t' }, it.title),
      it.subtitle ? h('span', { class: 'li-s' }, it.subtitle) : null);
    b.addEventListener('click', () => ctx.entities.open(it.entity));
    ul.append(h('li', null, b));
  }
  return ul;
}

export function register(ctx) {
  const layers = {
    delays: delays.register(ctx),
    restrictions: restrictions.register(ctx),
    advisories: advisories.register(ctx),
  };
  ctx.whatshere.section(async (lat, lon) => {
    if (!ctx.moduleOk('flights')) return null;
    let d;
    try { d = await ctx.api.get('/api/flights/airspace/at', { lat: lat.toFixed(4), lon: lon.toFixed(4) }); } catch { return null; }
    const list = [];
    for (const t of d.tfrs || []) {
      const alt = t.lower && t.upper ? `${t.lower} to ${t.upper}` : null;
      const when = t.status === 'upcoming' ? `starts ${ctx.fmt.ago(t.start)}` : (t.status === 'active' ? 'in effect' : null);
      list.push({ title: `${t.type_label}: ${t.place || `FDC ${t.id}`}`, subtitle: [alt, when].filter(Boolean).join(', '), detail: 'FAA', entity: t.entity });
    }
    for (const c of d.advisories || []) {
      list.push({ title: c.subject, subtitle: [c.level_label, c.altitudes].filter(Boolean).join(', '), detail: 'EASA', entity: c.entity });
    }
    for (const a of d.delays || []) list.push({ title: a.title, subtitle: a.summary, detail: ctx.fmt.km(a.distance_km), entity: a });
    return list.length ? { title: 'Airspace', el: listEl(ctx, list) } : null;
  });
  return { layers, openDelay: delays.openDelay, openTfr: restrictions.openTfr, openAdvisory: advisories.openAdvisory };
}
