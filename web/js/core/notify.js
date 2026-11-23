// Notifications (CONTRACT.md section 15.3). ctx.notify.send({title, body, tag, tone, entity}) always
// shows an in-app toast and, when the browser allows it and the user has them switched on, a desktop
// notification (only while Meridian is not the focused tab, at most six a minute). The same tag
// never repeats within 10 minutes, across reloads and tabs. Clicking the desktop notification focuses
// Meridian and opens the entity; the toast has a Show button for the same.
//
// createNotifier(env) takes stand-ins for the browser (Notification, storage, clock, focus) so the
// logic can be tested in Node (tests/test_core_draw.mjs). Nothing here touches the DOM at import.

import { bus } from './bus.js';
import { h, svgIcon } from './dom.js';

export const REPEAT_MS = 10 * 60 * 1000;
export const DESKTOP_PER_MINUTE = 6;
const SETTING_KEY = 'meridian.notify.enabled';
const SENT_KEY = 'meridian.notify.sent';
const SENT_MAX = 400;
const TONES = new Set(['red', 'amber', 'green', 'blue', 'grey']);
const BELL = 'M10 3.2a4.4 4.4 0 0 0-4.4 4.4v2.9L4.2 13.6h11.6l-1.4-3.1V7.6A4.4 4.4 0 0 0 10 3.2ZM8.2 15.6a1.9 1.9 0 0 0 3.6 0';

const clip = (v, max) => (v == null ? '' : String(v).trim().slice(0, max));

function browserStorage() {
  try { return globalThis.localStorage || null; } catch { return null; }
}

function pageFocused() {
  return typeof document !== 'undefined' && document.visibilityState === 'visible' && document.hasFocus();
}

export function createNotifier(env = {}) {
  const now = env.now || (() => Date.now());
  const storage = () => (env.storage !== undefined ? env.storage : browserStorage());
  const Api = () => (env.Notification !== undefined ? env.Notification : (globalThis.Notification || null));
  const focused = env.focused || pageFocused;
  const hooks = {
    toast: env.toast || null,
    openEntity: env.openEntity || null,
    focusWindow: env.focusWindow || (() => { try { globalThis.focus?.(); } catch { /* ignore */ } }),
    icon: env.icon || null,
  };
  const sent = new Map();        // tag -> ms, mirrored in storage
  const desktopTimes = [];

  const read = (key) => { try { return storage()?.getItem(key) ?? null; } catch { return null; } };
  const write = (key, value) => { try { storage()?.setItem(key, value); } catch { /* private mode, full */ } };
  const setting = () => { const v = read(SETTING_KEY); return v === 'true' ? true : v === 'false' ? false : null; };

  /** 'granted', 'denied', 'default' (not asked yet) or 'unsupported'. */
  function permission() {
    const N = Api();
    if (!N) return 'unsupported';
    return N.permission === 'granted' || N.permission === 'denied' ? N.permission : 'default';
  }

  /** Desktop notifications will show: permission granted and the setting on (on by default once granted). */
  function enabled() {
    return permission() === 'granted' && setting() !== false;
  }

  function changed() {
    bus.emit('notify:changed', { permission: permission(), enabled: enabled() });
  }

  /** Ask the browser (call it from a click). Resolves to the permission. */
  async function request() {
    const N = Api();
    if (!N) return 'unsupported';
    if (permission() !== 'default') return permission();
    let result;
    try {
      result = await new Promise((resolve, reject) => {   // the promise form, or the old callback form
        const r = N.requestPermission(resolve);
        if (r && typeof r.then === 'function') r.then(resolve, reject);
      });
    } catch {
      result = N.permission;
    }
    if (result === 'granted' && setting() === null) write(SETTING_KEY, 'true');
    changed();
    return permission();
  }

  function setEnabled(on) {
    write(SETTING_KEY, on ? 'true' : 'false');
    changed();
    return enabled();
  }

  function recall(t) {
    let stored = {};
    try { stored = JSON.parse(read(SENT_KEY) || '{}') || {}; } catch { stored = {}; }
    if (stored && typeof stored === 'object') {
      for (const [k, v] of Object.entries(stored)) if (Number.isFinite(v) && !(sent.get(k) >= v)) sent.set(k, v);
    }
    for (const [k, v] of sent) if (t - v >= REPEAT_MS || v - t > REPEAT_MS) sent.delete(k);
  }

  function remember(tag, t) {
    sent.set(tag, t);
    const keep = [...sent.entries()].sort((a, b) => b[1] - a[1]).slice(0, SENT_MAX);
    sent.clear();
    for (const [k, v] of keep) sent.set(k, v);
    write(SENT_KEY, JSON.stringify(Object.fromEntries(keep)));
  }

  function desktopRoom(t) {
    while (desktopTimes.length && t - desktopTimes[0] >= 60000) desktopTimes.shift();
    return desktopTimes.length < DESKTOP_PER_MINUTE;
  }

  /** send({title, body?, tag?, tone?, entity?}) -> {toast, desktop, suppressed} */
  function send(opts = {}) {
    const title = clip(opts?.title, 120);
    if (!title) throw new TypeError('notify.send needs a title');
    const body = clip(opts.body, 300);
    const tag = clip(opts.tag, 200);
    const tone = TONES.has(opts.tone) ? opts.tone : '';
    const entity = opts.entity && typeof opts.entity === 'object' && opts.entity.kind ? opts.entity : null;
    const t = now();
    if (tag) {
      recall(t);
      if (sent.has(tag)) return { toast: false, desktop: false, suppressed: true };
      remember(tag, t);
    }
    const open = entity && hooks.openEntity
      ? () => { try { hooks.openEntity(entity); } catch (err) { console.error('notify: opening the entity failed', err); } }
      : null;
    let toast = false;
    if (hooks.toast) {
      try {
        hooks.toast(body || title, tone, 8000, { title: body ? title : null, action: open ? { text: 'Show', onClick: open } : null });
        toast = true;
      } catch (err) { console.error('notify: toast failed', err); }
    }
    let desktop = false;
    const N = Api();
    if (N && enabled() && !focused() && desktopRoom(t)) {
      try {
        const icon = hooks.icon ? hooks.icon() : null;
        const n = new N(title, { body, tag: tag || undefined, icon: icon || undefined });
        n.onclick = (ev) => {
          try { ev?.preventDefault?.(); } catch { /* ignore */ }
          hooks.focusWindow();
          try { n.close(); } catch { /* ignore */ }
          open?.();
        };
        desktopTimes.push(t);
        desktop = true;
      } catch (err) {
        console.warn('notify: desktop notification failed', err);
      }
    }
    return { toast, desktop, suppressed: false };
  }

  return {
    permission, request, enabled, setEnabled, send,
    /** Core wiring: {toast(message, tone, ms, opts), openEntity(entity)}. */
    configure(more) { Object.assign(hooks, more || {}); },
  };
}

// ------------------------------------------------------------------ the browser instance
let iconUrl;
function brandIcon() {
  if (iconUrl !== undefined) return iconUrl;
  iconUrl = null;
  try {
    const c = document.createElement('canvas');
    c.width = c.height = 64;
    const g = c.getContext('2d');
    g.fillStyle = '#111a2c';
    g.beginPath(); g.arc(32, 32, 32, 0, Math.PI * 2); g.fill();
    g.strokeStyle = '#ebe6d6'; g.lineWidth = 3.4;
    g.beginPath(); g.arc(32, 32, 24, 0, Math.PI * 2); g.stroke();
    g.lineWidth = 2.4; g.globalAlpha = 0.7;
    g.beginPath(); g.ellipse(32, 32, 10, 24, 0, 0, Math.PI * 2); g.stroke();
    g.globalAlpha = 1; g.strokeStyle = '#6cb6ff'; g.lineWidth = 4;
    g.beginPath(); g.moveTo(32, 4); g.lineTo(32, 60); g.stroke();
    iconUrl = c.toDataURL('image/png');
  } catch { iconUrl = null; }
  return iconUrl;
}

export const notify = createNotifier({ icon: brandIcon });

/** The "Notifications" switch in the status bar. */
export function mountSwitch(bar, before, { toast } = {}) {
  const say = (msg, tone = '', ms = 4500) => { try { toast?.(msg, tone, ms); } catch { /* ignore */ } };
  const label = h('span', { class: 'label' });
  const btn = h('button', { class: 'sb-item sb-btn notify-switch', id: 'notify-switch', type: 'button', role: 'switch', 'aria-checked': 'false' },
    svgIcon(BELL, { size: 14 }), label);
  const render = () => {
    const p = notify.permission();
    const on = notify.enabled();
    btn.setAttribute('aria-checked', String(on));
    btn.classList.toggle('on', on);
    btn.classList.toggle('blocked', p === 'denied' || p === 'unsupported');
    label.textContent = p === 'unsupported' ? 'Notifications unavailable' : p === 'denied' ? 'Notifications blocked' : on ? 'Notifications on' : 'Notifications off';
    btn.title = p === 'unsupported' ? 'This browser cannot show desktop notifications. Alerts still appear inside Meridian.'
      : p === 'denied' ? 'Desktop notifications are blocked for this site. Allow them in the browser’s site settings.'
        : p === 'default' ? 'Desktop notifications are off. Click to allow them.'
          : on ? 'Desktop notifications are on. Click to turn them off.' : 'Desktop notifications are off. Click to turn them on.';
  };
  btn.addEventListener('click', async () => {
    const p = notify.permission();
    if (p === 'unsupported') say('This browser cannot show desktop notifications. Alerts still appear inside Meridian.');
    else if (p === 'denied') say('Desktop notifications are blocked for this site. Allow them in the browser’s site settings, then reload.', 'amber', 7000);
    else if (p === 'default') {
      const r = await notify.request();
      say(r === 'granted' ? 'Desktop notifications are on.' : 'Desktop notifications were not allowed. Alerts still appear inside Meridian.', r === 'granted' ? 'green' : 'amber');
    } else {
      const on = notify.setEnabled(!notify.enabled());
      say(on ? 'Desktop notifications are on.' : 'Desktop notifications are off. Alerts still appear inside Meridian.', on ? 'green' : '');
    }
    render();
  });
  bus.on('notify:changed', render);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') render(); });
  try {
    navigator.permissions?.query({ name: 'notifications' }).then((st) => { st.onchange = render; }).catch(() => {});
  } catch { /* older browsers */ }
  render();
  bar.insertBefore(btn, before || null);
  return btn;
}
