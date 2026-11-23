// One camera's live picture, used by the card and the grid: the latest still from the server's
// proxy (refreshed at the camera's cadence, swapped in only once decoded so it never flickers), or
// the camera's video when the source lets the browser play it. Latest frame only: nothing is kept.

import { ageParts, courtesyLine, directInfo, imgUrl } from './shared.js';

const CARD_MIN_MS = 15000;
const TILE_MIN_MS = 30000;
const MAX_MS = 5 * 60 * 1000;
const DIRECT_MS = 5 * 60 * 1000;     // a direct picture's link is looked up again every 5 min

export function createView(ctx, cam, opts = {}) {
  return cam.direct ? createDirectView(ctx, cam, opts) : createProxiedView(ctx, cam, opts);
}

// A direct camera (Windy): the browser shows the source's own picture link, never a proxied copy,
// at its own size or smaller (never stretched), linked to the camera's page on the source, with
// the source's credit line in the corner.
function createDirectView(ctx, cam, { tile = false, onState = null, small = false } = {}) {
  const { h } = ctx;
  const img = h('img', { alt: tile ? '' : `Latest image from ${cam.name}`, decoding: 'async', draggable: 'false',
    referrerpolicy: 'no-referrer', hidden: true });
  const link = h('a', { class: 'cameras-directlink', title: 'Open this webcam on its source for the full view' }, img);
  const msg = h('div', { class: 'media-msg cameras-msg', hidden: true });
  const stamp = h('span', { class: 'stamp cameras-stamp' });
  const credit = h('span', { class: 'credit cameras-creditline' });
  const spinner = h('div', { class: 'cameras-spin', 'aria-hidden': 'true' });
  const root = h('div', { class: `card-media cameras-view cameras-direct${tile ? ' cameras-tileview' : ''}`, dataset: { src: `camera:${cam.id}` } },
    link, spinner, msg, credit, stamp);
  let timer = null;
  let destroyed = false;
  let shown = null;
  const facts = { imageTime: null, fetchedAt: null, stale: false, error: null, ok: null, views: null, player: null, link: null };

  function updateStamp() {
    const t = facts.imageTime ? facts.imageTime * 1000 : null;
    if (!t) { stamp.textContent = ''; return; }
    const a = ageParts(t);
    stamp.textContent = tile ? `${a.value} ${a.unit} ago` : `${ctx.fmt.clock(t).slice(0, 5)} UTC, ${a.value} ${a.unit} ago`;
    root.classList.toggle('cameras-old', Date.now() - t > 3 * 3600 * 1000);
  }

  function showMsg(text) {
    msg.textContent = text;
    msg.hidden = false;
    msg.classList.toggle('cameras-msg-over', !!shown);
  }

  async function load(fresh = false) {
    clearTimeout(timer);
    if (destroyed) return;
    if (document.visibilityState !== 'visible') { timer = setTimeout(() => load(), DIRECT_MS); return; }
    root.classList.add('cameras-loading');
    try {
      const info = await directInfo(ctx, cam.id, { fresh });
      if (destroyed) return;
      ctx.dom.clear(credit);
      const line = courtesyLine(ctx, info.courtesy);
      if (line) credit.append(line);
      if (info.link) { link.setAttribute('href', ctx.dom.safeUrl(info.link) || ''); link.target = '_blank'; link.rel = 'noopener noreferrer'; }
      facts.link = info.link || null;
      facts.player = info.player || null;
      facts.views = info.views ?? null;
      if (!info.ok) throw new Error(info.error || 'No current picture.');
      const src = small ? (info.thumb || info.image) : info.image;
      if (src !== shown) {
        const probe = new Image();
        probe.referrerPolicy = 'no-referrer';
        probe.src = ctx.dom.safeUrl(src) || '';
        await probe.decode();
        if (destroyed) return;
        img.src = probe.src;
        // never larger than the picture itself (the source's terms: original size or smaller)
        const w = probe.naturalWidth || (small ? info.thumb_width : info.width);
        const hgt = probe.naturalHeight || (small ? info.thumb_height : info.height);
        if (w) img.style.maxWidth = `min(100%, ${w}px)`;
        if (hgt) img.style.maxHeight = `min(100%, ${hgt}px)`;
        if (!tile && w && hgt) root.style.aspectRatio = String(Math.min(21 / 9, Math.max(4 / 3, w / hgt)));
        img.hidden = false;
        shown = src;
      }
      facts.imageTime = info.updated_at || null;
      facts.fetchedAt = info.fetched_at || Date.now() / 1000;
      facts.ok = true;
      facts.error = null;
      msg.hidden = true;
      updateStamp();
    } catch (err) {
      if (destroyed) return;
      facts.ok = false;
      facts.error = err?.message || 'The picture could not be loaded right now.';
      showMsg(shown ? `${facts.error} Showing the last picture.` : facts.error);
    } finally {
      root.classList.remove('cameras-loading');
      onState?.(facts);
      if (!destroyed) timer = setTimeout(() => load(true), DIRECT_MS);
    }
  }

  const onVisible = () => { if (document.visibilityState === 'visible' && !destroyed) load(); };
  document.addEventListener('visibilitychange', onVisible);
  const ageTick = setInterval(() => { if (!destroyed) updateStamp(); else clearInterval(ageTick); }, 15000);
  load();
  return {
    el: root,
    facts,
    reload: () => load(true),
    playVideo: async () => false,
    stopVideo: () => {},
    isVideo: () => false,
    destroy() {
      destroyed = true;
      clearTimeout(timer);
      clearInterval(ageTick);
      document.removeEventListener('visibilitychange', onVisible);
    },
  };
}

function createProxiedView(ctx, cam, { tile = false, video = false, onState = null } = {}) {
  const { h } = ctx;
  const img = h('img', { alt: tile ? '' : `Latest image from ${cam.name}`, decoding: 'async', draggable: 'false' });
  const msg = h('div', { class: 'media-msg cameras-msg', hidden: true });
  const stamp = h('span', { class: 'stamp cameras-stamp' });
  const credit = tile ? null : h('span', { class: 'credit' }, cam.attribution || '');
  const spinner = h('div', { class: 'cameras-spin', 'aria-hidden': 'true' });
  const root = h('div', { class: `card-media cameras-view${tile ? ' cameras-tileview' : ''}`, dataset: { src: `camera:${cam.id}` } },
    img, spinner, msg, credit, stamp);

  let timer = null;
  let ac = null;
  let objUrl = null;
  let destroyed = false;
  let mode = 'still';
  let videoEl = null;
  let hls = null;
  let failures = 0;
  const facts = { imageTime: null, fetchedAt: null, stale: false, error: null, ok: null };

  const period = () => {
    const cadence = Math.max(1, +cam.refresh || 60) * 1000;
    const base = tile ? Math.max(TILE_MIN_MS, cadence) : Math.max(CARD_MIN_MS, cadence / 2);
    return Math.min(MAX_MS, base * (failures ? Math.min(4, 1 + failures) : 1));
  };

  function updateStamp() {
    if (mode !== 'still') { stamp.textContent = ''; return; }
    const t = facts.imageTime ? facts.imageTime * 1000 : facts.fetchedAt ? facts.fetchedAt * 1000 : null;
    if (!t) { stamp.textContent = ''; return; }
    const a = ageParts(t);
    const clock = ctx.fmt.clock(t).slice(0, 5);
    stamp.textContent = facts.stale ? `Earlier frame, ${clock} UTC` : tile ? `${a.value} ${a.unit} ago` : `${clock} UTC, ${a.value} ${a.unit} ago`;
    root.classList.toggle('cameras-old', Date.now() - t > Math.max(900000, (+cam.refresh || 60) * 3000));
  }

  function showMsg(text) {
    msg.textContent = text;
    msg.hidden = false;
    if (!img.getAttribute('src')) img.hidden = true;
  }

  function schedule() {
    clearTimeout(timer);
    if (destroyed || mode !== 'still') return;
    timer = setTimeout(load, period());
  }

  async function load() {
    clearTimeout(timer);
    if (destroyed || mode !== 'still') return;
    if (document.visibilityState !== 'visible') { schedule(); return; }
    ac?.abort();
    ac = new AbortController();
    const my = ac;
    root.classList.add('cameras-loading');
    try {
      const res = await fetch(imgUrl(ctx, cam.id, { fallback: 1 }), { signal: AbortSignal.any([my.signal, AbortSignal.timeout(30000)]), credentials: 'same-origin' });
      const failed = res.headers.get('X-Camera-Error');
      if (!res.ok || failed) {
        // fallback=1 answers failures with a placeholder image and the reason in headers
        let text = 'The image could not be loaded right now.';
        try { text = JSON.parse(res.headers.get('X-Camera-Message') || 'null') || text; } catch { /* keep the default */ }
        if (!failed) { try { const body = await res.json(); if (body?.error?.message) text = body.error.message; } catch { /* not JSON */ } }
        throw Object.assign(new Error(text), { status: +res.headers.get('X-Camera-Status') || res.status });
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const probe = new Image();
      probe.src = url;
      await probe.decode();
      if (destroyed || my.signal.aborted) { URL.revokeObjectURL(url); return; }
      if (!tile && probe.naturalWidth && probe.naturalHeight) {
        // fit the frame to the camera's own shape (between 4:3 and 21:9) so there are no side bars
        const ratio = Math.min(21 / 9, Math.max(4 / 3, probe.naturalWidth / probe.naturalHeight));
        root.style.aspectRatio = String(ratio);
      }
      img.src = url;
      img.hidden = false;
      if (objUrl) URL.revokeObjectURL(objUrl);
      objUrl = url;
      facts.imageTime = +res.headers.get('X-Camera-Image-Time') || null;
      facts.fetchedAt = +res.headers.get('X-Camera-Fetched') || Date.now() / 1000;
      facts.stale = res.headers.get('X-Camera-Stale') === '1';
      facts.ok = true;
      facts.error = null;
      failures = 0;
      msg.hidden = true;
      updateStamp();
    } catch (err) {
      if (my.signal.aborted && !destroyed && err?.name === 'AbortError') return;
      if (destroyed) return;
      failures += 1;
      facts.ok = false;
      facts.error = err?.message || 'The image could not be loaded right now.';
      showMsg(objUrl ? `${facts.error} Showing the last image.` : facts.error);
      if (objUrl) msg.classList.add('cameras-msg-over'); else msg.classList.remove('cameras-msg-over');
    } finally {
      if (ac === my) root.classList.remove('cameras-loading');
      onState?.(facts);
      schedule();
    }
  }

  async function playVideo() {
    if (!cam.stream) return false;
    mode = 'video';
    clearTimeout(timer);
    ac?.abort();
    img.hidden = true;
    msg.hidden = true;
    stamp.textContent = '';
    if (cam.kind === 'mjpeg') {
      // a motion-JPEG stream plays in an <img>; the page only allows https pictures, so http streams can't
      if (!/^https:\/\//i.test(cam.stream)) {
        mode = 'still';
        showMsg('This live stream is only served over plain http, which this page does not load. Open it on its source.');
        onState?.({ ...facts, videoError: 'http stream' });
        return false;
      }
      videoEl = h('img', { class: 'cameras-mjpeg', alt: `Live stream from ${cam.name}`, referrerpolicy: 'no-referrer', src: cam.stream });
      videoEl.addEventListener('error', () => { if (mode === 'video') { stopVideo(); showMsg('The live stream stopped. It may be offline; showing the still image.'); } });
      root.insertBefore(videoEl, msg);
      root.classList.add('cameras-playing');
      return true;
    }
    videoEl = h('video', { muted: true, autoplay: true, playsinline: true, controls: !tile, loop: cam.kind === 'video' });
    videoEl.muted = true;
    if (objUrl) videoEl.poster = objUrl;
    root.insertBefore(videoEl, msg);
    root.classList.add('cameras-playing');
    const fail = (text) => {
      if (mode !== 'video') return;
      stopVideo();
      showMsg(text);
      msg.classList.toggle('cameras-msg-over', !!objUrl);
      onState?.({ ...facts, videoError: text });
    };
    videoEl.addEventListener('error', () => fail('The video could not be played. Showing the still image instead.'));
    videoEl.addEventListener('loadedmetadata', () => {
      if (!tile && videoEl?.videoWidth && videoEl.videoHeight) {
        root.style.aspectRatio = String(Math.min(21 / 9, Math.max(4 / 3, videoEl.videoWidth / videoEl.videoHeight)));
      }
    });
    if (cam.kind === 'hls' && !videoEl.canPlayType('application/vnd.apple.mpegurl')) {
      const Hls = await ctx.cards.loadHls();
      if (destroyed || mode !== 'video') return false;
      if (!Hls || !Hls.isSupported()) { fail('This browser cannot play this stream.'); return false; }
      hls = new Hls({ enableWorker: true, lowLatencyMode: true, maxBufferLength: 12, backBufferLength: 0 });
      hls.on(Hls.Events.ERROR, (_e, data) => { if (data.fatal) fail('The live stream stopped. It may be offline; showing the still image.'); });
      hls.loadSource(cam.stream);
      hls.attachMedia(videoEl);
    } else {
      videoEl.src = cam.stream;
    }
    return true;
  }

  function stopVideo() {
    if (hls) { try { hls.destroy(); } catch { /* ignore */ } hls = null; }
    if (videoEl) {
      try {
        if (videoEl.tagName === 'IMG') videoEl.removeAttribute('src');          // closes the MJPEG connection
        else { videoEl.pause(); videoEl.removeAttribute('src'); videoEl.load(); }
      } catch { /* ignore */ }
      videoEl.remove();
      videoEl = null;
    }
    root.classList.remove('cameras-playing');
    mode = 'still';
    if (objUrl) img.hidden = false;
    load();
  }

  function destroy() {
    destroyed = true;
    clearTimeout(timer);
    ac?.abort();
    if (hls) { try { hls.destroy(); } catch { /* ignore */ } hls = null; }
    if (videoEl) { try { if (videoEl.tagName === 'IMG') videoEl.removeAttribute('src'); else { videoEl.pause(); videoEl.removeAttribute('src'); videoEl.load(); } } catch { /* ignore */ } }
    if (objUrl) URL.revokeObjectURL(objUrl);
    objUrl = null;
  }

  const onVisible = () => { if (document.visibilityState === 'visible' && mode === 'still' && !destroyed) load(); };
  document.addEventListener('visibilitychange', onVisible);
  const ageTick = setInterval(() => { if (!destroyed) updateStamp(); else clearInterval(ageTick); }, 15000);

  if (cam.kind === 'page' && !cam.dynamic) {
    showMsg('This camera only shows its picture on the operator’s website.');
  } else if (!cam.img_url && cam.stream) {
    // stream-only camera: there is no still to ask for, so play it (cards) or say so (tiles)
    if (!tile && cam.playable) playVideo();
    else showMsg(cam.playable ? 'Live video only: open the camera to play it.' : 'This camera only has live video, on its operator’s website.');
  } else if (video && cam.stream) {
    playVideo();
  } else {
    load();
  }

  return {
    el: root,
    facts,
    reload: () => { failures = 0; load(); },
    playVideo,
    stopVideo,
    isVideo: () => mode === 'video',
    destroy() {
      destroy();
      clearInterval(ageTick);
      document.removeEventListener('visibilitychange', onVisible);
    },
  };
}
