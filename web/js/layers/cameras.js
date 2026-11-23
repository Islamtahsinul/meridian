// Cameras module (browser half): the public camera layer, camera cards, the camera grid and a
// "Nearest cameras" section for What's here. See docs/modules/cameras.md.

import { createCameraLayer, registerIcons } from './cameras/maplayer.js';
import { openCameraCard } from './cameras/card.js';
import { openGrid } from './cameras/grid.js';
import { GRID_PATH, courtesyLine, directInfo, entityOf, imgUrl } from './cameras/shared.js';

// a direct camera (Windy): its own thumbnail link, linked to its page there; the name opens the card
function directItem(ctx, c, credits) {
  const { h } = ctx;
  const img = h('img', { alt: '', decoding: 'async', referrerpolicy: 'no-referrer' });
  const pic = h('a', { class: 'cameras-nearpic', title: 'Open this webcam on its source' }, img);
  const name = h('button', { type: 'button', class: 'cameras-nearname cameras-nearbtn', title: `${c.name}${c.subtitle ? `, ${c.subtitle}` : ''}` }, c.name);
  const item = h('div', { class: 'cameras-nearitem cameras-nearitem-direct' }, pic, name,
    h('span', { class: 'cameras-neardist' }, ctx.fmt.km(c.distance_km)));
  name.addEventListener('click', () => ctx.entities.open(entityOf(c)));
  directInfo(ctx, c.id).then((info) => {
    if (info.link) { pic.setAttribute('href', ctx.dom.safeUrl(info.link) || ''); pic.target = '_blank'; pic.rel = 'noopener noreferrer'; }
    if (!info.ok || !(info.thumb || info.image)) { item.classList.add('cameras-nearitem-noimg'); return; }
    img.src = ctx.dom.safeUrl(info.thumb || info.image) || '';
    if (info.thumb_width) img.style.maxWidth = `min(100%, ${info.thumb_width}px)`;
    if (info.courtesy?.length && !credits.dataset.on) {
      credits.dataset.on = '1';
      const line = courtesyLine(ctx, info.courtesy);
      if (line) credits.append(line);
    }
  }).catch(() => item.classList.add('cameras-nearitem-noimg'));
  img.addEventListener('error', () => item.classList.add('cameras-nearitem-noimg'));
  return item;
}

async function nearestSection(ctx, lat, lon) {
  let res;
  try {
    res = await ctx.api.get('/api/cameras/list', { lat: lat.toFixed(5), lon: lon.toFixed(5), radius_km: 15, limit: 3 });
  } catch { return null; }
  const cams = (res.cameras || []).filter(c => c.img_url || c.direct);
  if (!cams.length) return null;
  const { h } = ctx;
  const credits = h('div', { class: 'cameras-nearcredit' });
  const row = h('div', { class: 'cameras-near' }, cams.map((c) => {
    if (c.direct) return directItem(ctx, c, credits);
    const b = h('button', { type: 'button', class: 'cameras-nearitem', title: `${c.name}${c.subtitle ? `, ${c.subtitle}` : ''}` },
      h('img', { src: imgUrl(ctx, c.id, { fallback: 1 }), alt: '', loading: 'lazy', decoding: 'async' }),
      h('span', { class: 'cameras-nearname' }, c.name),
      h('span', { class: 'cameras-neardist' }, ctx.fmt.km(c.distance_km)));
    const img = b.querySelector('img');
    img.addEventListener('error', () => b.classList.add('cameras-nearitem-noimg'));
    b.addEventListener('click', () => ctx.entities.open(entityOf(c)));
    return b;
  }));
  return { title: 'Nearest cameras', el: h('div', null, row, credits) };
}

export async function register(ctx) {
  registerIcons(ctx);
  createCameraLayer(ctx, { onPick: (entity) => openCameraCard(ctx, entity) });
  ctx.entities.handler('camera', (entity) => openCameraCard(ctx, entity), { group: 'Cameras' });
  ctx.ui.toolbarButton({ id: 'cameras-grid', title: 'Camera grid', label: 'Cameras', icon: GRID_PATH, onClick: () => openGrid(ctx), order: 20 });
  ctx.whatshere.section((lat, lon) => nearestSection(ctx, lat, lon));
  ctx.ai.register('open_camera_grid', ({ mode } = {}) => openGrid(ctx, { mode: ['mine', 'favourites', 'view'].includes(mode) ? mode : null }));
}
