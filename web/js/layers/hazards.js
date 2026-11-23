// Hazards module (Events group): earthquakes, disaster alerts, tropical storms, active fires,
// volcanoes, weather warnings, tsunami messages, natural events, disease outbreaks, Copernicus
// emergency mapping, England flood warnings with river and sea gauges, and the GloFAS flood outlook.
// Every layer reads /api/hazards/*; cards open from a bare {kind, id}; right-click "What's here?"
// gains a hazards section and a "Flood warnings near here" section.

import { createKit, GLYPH, warningName } from './hazards/kit.js';
import { registerQuakes } from './hazards/quakes.js';
import { registerDisasters } from './hazards/disasters.js';
import { registerStorms } from './hazards/storms.js';
import { registerFires } from './hazards/fires.js';
import { registerVolcanoes } from './hazards/volcanoes.js';
import { registerWarnings } from './hazards/warnings.js';
import { registerTsunami, registerNatural, registerOutbreaks } from './hazards/others.js';
import { registerCards } from './hazards/cards.js';
import { registerMapping } from './hazards/mapping.js';
import { registerFloods } from './hazards/floods.js';
import { registerOutlook } from './hazards/outlook.js';

export async function register(ctx) {
  const kit = createKit(ctx);
  const extras = { builders: {} };
  registerCards(ctx, kit, extras);
  registerQuakes(ctx, kit);
  extras.disasters = registerDisasters(ctx, kit);
  registerMapping(ctx, kit, extras);
  registerStorms(ctx, kit);
  registerFires(ctx, kit);
  registerVolcanoes(ctx, kit);
  registerWarnings(ctx, kit);
  registerFloods(ctx, kit, extras);
  registerOutlook(ctx, kit);
  registerTsunami(ctx, kit);
  registerNatural(ctx, kit);
  registerOutbreaks(ctx, kit);
  registerWhatsHere(ctx);
}

function registerWhatsHere(ctx) {
  const { h, fmt } = ctx;
  ctx.whatshere.section(async (lat, lon) => {
    let r;
    try {
      r = await ctx.api.get('/api/hazards/here', { lat: lat.toFixed(4), lon: lon.toFixed(4) });
    } catch {
      return null;
    }
    const items = [];
    for (const w of r.warnings || []) {
      items.push({ glyph: GLYPH.warning, title: warningName(w.title), sub: [w.severity, w.expires ? `until ${fmt.utc(w.expires)}` : null].filter(Boolean).join(', '), entity: { ...w, title: warningName(w.title) } });
    }
    for (const q of r.quakes || []) {
      items.push({ glyph: GLYPH.earthquake, title: q.title, sub: `${fmt.km(q.distance_km)} away, ${fmt.ago(q.time)}`, entity: q });
    }
    if (r.volcano) items.push({ glyph: GLYPH.volcano, title: r.volcano.title, sub: `${fmt.km(r.volcano.distance_km)} away, ${r.volcano.subtitle}`, entity: r.volcano });
    if (r.fires?.count) {
      const one = +r.fires.count === 1;
      items.push({ glyph: GLYPH.fire, title: `${fmt.int(r.fires.count)} fire detection${one ? '' : 's'} within 15 km`,
        sub: one ? `${fmt.km(r.fires.nearest_km)} away, ${fmt.num(r.fires.frp_max, 0)} MW (past 24 h)`
          : `Nearest ${fmt.km(r.fires.nearest_km)}, strongest ${fmt.num(r.fires.frp_max, 0)} MW (past 24 h)`, entity: r.fires.entity });
    }
    if (!items.length) return null;
    const ul = h('ul', { class: 'card-list hazards-here' });
    for (const it of items) {
      const b = h('button', { type: 'button' }, h('span', { class: 'li-t' }, h('span', { class: 'hazards-glyph', 'aria-hidden': 'true' }, it.glyph), it.title),
        h('span', { class: 'li-s' }, it.sub));
      b.addEventListener('click', () => ctx.entities.open(it.entity));
      ul.append(h('li', null, b));
    }
    return { title: 'Hazards', el: ul };
  });
}
