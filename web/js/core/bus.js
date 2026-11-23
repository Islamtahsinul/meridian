// Tiny event bus (CONTRACT.md sections 10.3 and 15.3).
// Core events: map:ready, map:moveend {bounds, zoom}, map:click-empty {lngLat}, layer:toggled {id, on},
// card:opened {key}, card:closed {key}, basemap:changed {id}, projection:changed {type},
// favourites:changed, casefiles:changed, entity:opened {entity}, time:mode {mode},
// time:changed {t, mode}, notify:changed {permission, enabled}.

const listeners = new Map();

export const bus = {
  on(evt, fn) {
    if (!listeners.has(evt)) listeners.set(evt, new Set());
    listeners.get(evt).add(fn);
    return () => bus.off(evt, fn);
  },
  off(evt, fn) {
    listeners.get(evt)?.delete(fn);
  },
  emit(evt, payload) {
    for (const fn of [...(listeners.get(evt) || [])]) {
      try { fn(payload); } catch (err) { console.error(`bus handler for ${evt} failed`, err); }
    }
  },
};
