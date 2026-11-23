// The camera card: live picture, the data block (facing, cadence, image age, elevation), place,
// source credit and licence, nearby cameras, "Add to grid" and "Open on source". Works from a bare
// {kind, id} (favourites, case files, search) by asking the server for the camera.

import { CAM, CAMERA_PATH, COMPASS_WORD, KIND_LABEL, TAG_LABEL, ageParts, cadence, entityOf, inGrid, toggleGrid } from './shared.js';
import { createView } from './view.js';

const views = new Map();   // card key -> view (one picture per open card)

function dropView(key) {
  const v = views.get(key);
  if (v) { v.destroy(); views.delete(key); }
}

function icon(ctx) {
  return ctx.dom.svgIcon(CAMERA_PATH, { size: 17 });
}

function blockFor(ctx, cam, facts) {
  const block = [];
  if (cam.compass) block.push({ label: 'Facing', value: cam.compass });
  if (cam.direct) {
    // the source does not say how often a webcam updates: show how old the picture is
    const a = ageParts(facts?.imageTime ? facts.imageTime * 1000 : NaN);
    block.push({ label: 'Picture age', value: a.value, unit: a.unit });
    if (facts?.views != null) block.push({ label: 'Views', value: ctx.fmt.compact(facts.views) });
    if (cam.elevation != null && block.length < 4) block.push({ label: 'Elevation', value: ctx.fmt.int(cam.elevation), unit: 'm' });
    return block;
  }
  const c = cadence(cam.refresh);
  block.push({ label: 'Updates every', value: c.value, unit: c.unit });
  const t = facts?.imageTime ? facts.imageTime * 1000 : facts?.fetchedAt ? facts.fetchedAt * 1000 : NaN;
  const a = ageParts(t);
  block.push({ label: facts?.imageTime ? 'Image age' : 'Fetched', value: a.value, unit: a.unit });
  if (cam.elevation != null && block.length < 4) block.push({ label: 'Elevation', value: ctx.fmt.int(cam.elevation), unit: 'm' });
  return block;
}

function badgesFor(cam, source, facts) {
  const out = [];
  out.push({ text: cam.type === 'webcam' && cam.kind === 'image' ? 'Webcam' : (KIND_LABEL[cam.kind] || 'Camera'),
    tone: cam.kind === 'hls' ? 'green' : cam.kind === 'video' ? 'blue' : 'grey' });
  for (const t of (cam.tags || []).filter(t => t !== 'traffic').slice(0, 3)) out.push({ text: TAG_LABEL[t] || t, tone: 'grey' });
  if (facts?.ok === false) out.push({ text: 'Not answering', tone: 'amber', title: facts.error || '' });
  else if (facts?.stale) out.push({ text: 'Earlier frame', tone: 'amber' });
  if (source?.stale) out.push({ text: 'Camera list from an earlier day', tone: 'amber', title: 'This source did not answer at the last daily update.' });
  return out;
}

// "Southbound", "S", "south" add nothing to "Facing south"; anything else is shown in brackets
function directionText(cam) {
  if (!cam.compass) return cam.direction || null;
  const word = COMPASS_WORD[cam.compass] || cam.compass;
  const plain = String(cam.direction || '').toLowerCase().replace(/(bound|wards?|erly)/g, '').replace(/[^a-z]/g, '');
  const same = !plain || plain === word.replace(/[^a-z]/g, '') || plain === cam.compass.toLowerCase();
  if (same) return `Facing ${word}`;
  // "West (A501 twds Paddington)" becomes "Facing west (A501 twds Paddington)", not "Facing west (West (...))"
  const raw = String(cam.direction).trim();
  // word and compass come from the fixed COMPASS_WORD table (letters and hyphens), so they are safe in a pattern
  const lead = new RegExp(`^(${word}|${cam.compass})(bound|wards?|erly)?(?![a-z])`, 'i').exec(raw);
  const rest = (lead ? raw.slice(lead[0].length) : raw).replace(/^[\s,:-]+/, '').replace(/^\((.*)\)$/, '$1').trim();
  return `Facing ${word} (${rest || raw})`;
}

// "View: West (A501 twds Paddington)" only repeats the direction row
function extraDescription(cam) {
  const d = String(cam.description || '').trim();
  if (!d || d === cam.name) return null;
  if (cam.direction && d.replace(/^view:\s*/i, '').trim().toLowerCase() === String(cam.direction).trim().toLowerCase()) return null;
  return d;
}

function placeRows(ctx, cam) {
  const dir = directionText(cam);
  return [
    ['Road', cam.road],
    ['Area', cam.area],
    ['Region', cam.region],
    ['Country', cam.country_name],
    ['Direction', dir],
    ['Elevation', cam.elevation != null ? `${ctx.fmt.int(cam.elevation)} m` : null],
    ['Position', ctx.fmt.latlon(cam.lat, cam.lon, 4)],
    ['Camera id', cam.id],
  ];
}

function sourceRows(ctx, cam, source) {
  const licence = source.licence_url ? ctx.h('a', { href: source.licence_url }, cam.licence || 'Licence') : cam.licence;
  return [
    ['Operator', source.owner],
    ['Feed', source.name],
    ['Licence', licence],
    ['Credit', cam.attribution],
    ['List updated', source.list_fetched_at ? ctx.fmt.ago(source.list_fetched_at) : null],
  ];
}

async function nearbyList(ctx, cam) {
  try {
    const res = await ctx.api.get('/api/cameras/list', { lat: cam.lat, lon: cam.lon, radius_km: 8, limit: 5 });
    return (res.cameras || []).filter(c => c.id !== cam.id).slice(0, 4).map(c => ({
      title: c.name, subtitle: c.subtitle, detail: c.distance_km < 0.05 ? 'Same spot' : ctx.fmt.km(c.distance_km),
      entity: { ...entityOf(c), lat: c.lat, lon: c.lon },
    }));
  } catch { return null; }
}

function simpleCard(ctx, entity, { title, subtitle, text, badge, rows = [], actions = [] }) {
  const valid = ctx.dom.validLatLon(entity.lat, entity.lon);
  return ctx.cards.open({
    key: `camera:${entity.id}`, kind: 'camera', title, subtitle, icon: icon(ctx), accent: CAM,
    badges: badge ? [badge] : [], sections: [{ text }, rows.length ? { rows } : null],
    entity: valid ? { kind: 'camera', id: String(entity.id), title, lat: +entity.lat, lon: +entity.lon, layer: 'cameras' } : null,
    actions,
  });
}

export async function openCameraCard(ctx, entity) {
  const id = String(entity.id || '');
  const key = `camera:${id}`;
  let data;
  try {
    data = await ctx.api.get(`/api/cameras/camera/${encodeURIComponent(id)}`);
  } catch (err) {
    dropView(key);
    if (err?.status === 503) {
      return simpleCard(ctx, entity, { title: entity.title || 'Camera', subtitle: 'Camera list still building',
        text: 'The camera list is still being built. This takes a few minutes the first time Meridian starts.',
        actions: [{ text: 'Try again', primary: true, onClick: () => openCameraCard(ctx, entity) }] });
    }
    return simpleCard(ctx, entity, { title: entity.title || 'Camera', subtitle: 'Details unavailable',
      text: err?.status === 400 ? 'That is not a valid camera id.' : 'The camera details could not be loaded right now.',
      actions: [{ text: 'Try again', onClick: () => openCameraCard(ctx, entity) }] });
  }
  if (!data || data.missing || !data.camera) {
    dropView(key);
    const seen = data?.last_seen;
    return simpleCard(ctx, { ...entity, lat: entity.lat ?? seen?.lat, lon: entity.lon ?? seen?.lon }, {
      title: entity.title || seen?.name || 'Camera', subtitle: 'No longer in the public camera lists',
      badge: { text: 'Not listed', tone: 'amber' },
      text: 'This camera is not in today’s camera lists. Its operator may have removed, renamed or switched it off.',
      rows: [['Last listed', seen?.last_seen ? ctx.fmt.utc(seen.last_seen) : 'Not known'], ['Camera id', id]],
    });
  }

  const cam = data.camera;
  const source = data.source || {};
  dropView(key);
  let card = null;
  let nearby = null;
  const wantVideo = !!(cam.playable && (ctx.settings.get('cameras.video', false) || !cam.img_url));
  const view = createView(ctx, cam, {
    video: wantVideo,
    onState: (facts) => {
      if (card?.isOpen()) card.update({ block: blockFor(ctx, cam, facts), badges: badgesFor(cam, source, facts), actions: actions(), links: links(facts) });
    },
  });
  views.set(key, view);

  const actions = () => {
    const list = [];
    const pinned = inGrid(ctx, cam.id);
    list.push({ text: pinned ? 'Remove from grid' : 'Add to grid', onClick: () => {
      const added = toggleGrid(ctx, cam);
      ctx.ui.toast(added ? 'Added to your camera grid' : 'Removed from your camera grid', added ? 'green' : '');
      card.update({ actions: actions() });
    } });
    if (cam.playable && cam.img_url) {            // stream-only cameras have no still to switch to
      list.push({ text: view.isVideo() ? 'Show still image' : (cam.kind === 'hls' || cam.kind === 'mjpeg' ? 'Play live video' : 'Play video clip'), onClick: async () => {
        if (view.isVideo()) { view.stopVideo(); ctx.settings.set('cameras.video', false); }
        else { await view.playVideo(); ctx.settings.set('cameras.video', true); }
        card.update({ actions: actions() });
      } });
    }
    return list;
  };

  const sections = () => [
    { el: view.el },
    { title: 'Camera', rows: placeRows(ctx, cam) },
    extraDescription(cam) ? { text: extraDescription(cam) } : null,
    { title: 'Source', rows: sourceRows(ctx, cam, source) },
    nearby && nearby.length ? { title: 'Nearby cameras', list: nearby } : null,
  ];

  const links = (facts) => [
    cam.open_url ? { text: source.name ? `Open on ${source.name}` : 'Open on the source site', href: cam.open_url } : null,
    cam.direct && facts?.player ? { text: 'Timelapse player', href: facts.player } : null,
  ].filter(Boolean);

  card = ctx.cards.open({
    key, kind: 'camera', title: cam.name, subtitle: cam.subtitle || '', icon: icon(ctx), accent: CAM,
    badges: badgesFor(cam, source, view.facts),
    block: blockFor(ctx, cam, view.facts),
    sections: sections(),
    links: links(view.facts),
    actions: actions(),
    entity: { kind: 'camera', id: cam.id, title: cam.name, subtitle: cam.subtitle || '', lat: cam.lat, lon: cam.lon, layer: 'cameras', zoom: 15 },
    favPayload: { src: cam.src },
    live: {
      everyMs: 30000,
      update: async () => ({ block: blockFor(ctx, cam, view.facts), badges: badgesFor(cam, source, view.facts) }),
    },
    foot: cam.direct
      ? `Latest picture, loaded from ${source.owner || source.name || 'its source'} and linked to it; nothing is recorded.`
      : `Latest frame only, nothing is recorded. ${cam.attribution || source.name || ''}.`,
    onClose: () => { if (views.get(key) === view) dropView(key); else view.destroy(); },
  });

  nearbyList(ctx, cam).then((items) => {
    nearby = items;
    if (card.isOpen() && items && items.length) card.update({ sections: sections() });
  });
  return card;
}
