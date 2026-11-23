// Drawing (CONTRACT.md section 15.3). ctx.draw.start({mode, color}) lets the user draw a polygon,
// circle or rectangle on the map (flat or globe) and resolves to a GeoJSON Polygon Feature, or null
// when cancelled. ctx.draw.show(id, feature, {color, label}) and hide(id) display saved shapes.
//
// While drawing, clicks, double-clicks and right-clicks on the map are taken in the capture phase on
// the map container, so they never reach MapLibre: no cards open, the map does not zoom and the
// "What's here?" menu stays shut. Drags still pan the map while drawing a polygon; for circles and
// rectangles a drag draws the shape. Geometry lives in geometry.js (pure, tested in Node).

import { h, clear, fmt } from './dom.js';
import * as mapx from './map.js';
import * as G from './geometry.js';

export const DEFAULT_COLOR = '#6cb6ff';
const MODES = ['polygon', 'circle', 'rectangle'];
const MODE_LABEL = { polygon: 'Polygon', circle: 'Circle', rectangle: 'Rectangle' };
const SRC = 'ov-core-draw';
const SHAPES = 'ov-core-shapes';
const SHAPE_LABELS = 'ov-core-shapes-label';
const TAP_PX = { mouse: 5, pen: 8, touch: 12 };
const CLOSE_PX = { mouse: 10, pen: 14, touch: 22 };   // a click this close to the first point closes the polygon
const DOUBLE_MS = 500;      // a second click on the last point within this finishes the polygon
const GRACE_MS = 450;       // after finishing, keep swallowing clicks this long (the rest of a double-click)
const MIN_PX = 4;           // circles and rectangles smaller than this on screen are ignored
const COLOR = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

let session = null;
let grace = null;           // the tail of the last session: {timer, done}

const colorOf = (c) => (typeof c === 'string' && COLOR.test(c.trim()) ? c.trim() : DEFAULT_COLOR);
const getMap = () => mapx.getMap();

// ------------------------------------------------------------------ public API
/** start({mode: 'polygon'|'circle'|'rectangle', color}) -> Promise<Feature|null>. Starting again
 *  cancels a drawing in progress (it resolves to null). */
export function start({ mode = 'polygon', color = DEFAULT_COLOR } = {}) {
  const map = getMap();
  if (!map) return Promise.reject(new Error('The map is not ready yet.'));
  if (!MODES.includes(mode)) return Promise.reject(new TypeError(`draw.start: mode must be one of ${MODES.join(', ')}`));
  if (session) end(null);
  flushGrace();
  return new Promise((resolve, reject) => {
    const s = { mode, color: colorOf(color), resolve, points: [], cursor: null, screen: null, press: null,
      lastTap: 0, pointers: new Set(), raf: 0, hint: null, off: [], swallowOff: [], restore: {}, inMap: () => false };
    session = s;
    try {
      begin(map, s);
      update();
    } catch (err) {                  // never leave a half-started drawing behind
      s.resolve = () => {};
      end(null);
      reject(err);
    }
  });
}

export function cancel() {
  if (session) end(null);
}

export function active() {
  return !!session;
}

/** Point-in-shape test that copes with antimeridian crossings (see geometry.js). */
export function contains(feature, lat, lon) {
  return G.pointInShape(feature, lat, lon);
}

// ------------------------------------------------------------------ session set-up and tear-down
function begin(map, s) {
  const box = map.getContainer();
  const canvasBox = map.getCanvasContainer();
  const inMap = (e) => !!e.target && canvasBox.contains(e.target);
  s.inMap = inMap;
  s.restore = { dragPan: map.dragPan.isEnabled(), boxZoom: map.boxZoom.isEnabled(), dblZoom: map.doubleClickZoom.isEnabled() };
  map.doubleClickZoom.disable();
  if (s.mode !== 'polygon') { map.dragPan.disable(); map.boxZoom.disable(); }
  mapx.setDispatchPaused(true);
  map.getCanvas().style.cursor = 'crosshair';

  const swallow = (e) => {
    if (!inMap(e)) return;
    e.stopPropagation();
    e.preventDefault();
    if (e.type === 'dblclick' && session === s && s.mode === 'polygon' && s.points.length >= 3) finishPolygon();
  };
  const on = (target, type, fn, opts) => { target.addEventListener(type, fn, opts); return () => target.removeEventListener(type, fn, opts); };
  s.swallowOff = [on(box, 'click', swallow, true), on(box, 'dblclick', swallow, true), on(box, 'contextmenu', swallow, true)];
  s.off.push(
    on(box, 'pointerdown', onDown, true),
    on(window, 'pointermove', onMove, true),
    on(window, 'pointerup', onUp, true),
    on(window, 'pointercancel', onPointerCancel, true),
    on(document, 'keydown', onKey, true),
  );
  map.on('move', onMapMove);
  s.off.push(() => map.off('move', onMapMove));
  mapx.addSource(SRC, { type: 'geojson', data: mapx.emptyFC() });
  const role = (r) => ['==', ['get', 'role'], r];
  mapx.addLayer({ id: `${SRC}-fill`, type: 'fill', source: SRC, filter: role('shape'), paint: { 'fill-color': s.color, 'fill-opacity': 0.18 } }, 'top');
  mapx.addLayer({ id: `${SRC}-line`, type: 'line', source: SRC, filter: role('path'), layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': s.color, 'line-width': 2.2 } }, 'top');
  mapx.addLayer({ id: `${SRC}-guide`, type: 'line', source: SRC, filter: role('guide'),
    paint: { 'line-color': s.color, 'line-width': 1.5, 'line-dasharray': [2, 2], 'line-opacity': 0.85 } }, 'top');
  mapx.addLayer({ id: `${SRC}-vertex`, type: 'circle', source: SRC, filter: role('vertex'),
    paint: { 'circle-radius': ['case', ['==', ['get', 'first'], true], 6, 4], 'circle-color': '#111a2c',
      'circle-stroke-color': s.color, 'circle-stroke-width': 2, 'circle-pitch-alignment': 'map' } }, 'top');
  mountHint(s);
}

function end(feature) {
  const s = session;
  if (!s) return;
  session = null;
  const map = getMap();
  for (const off of s.off) { try { off(); } catch { /* ignore */ } }
  if (s.raf) cancelAnimationFrame(s.raf);
  try { mapx.removeSource(SRC); } catch { /* ignore */ }
  s.hint?.el.remove();
  if (map) {
    map.getCanvas().style.cursor = '';
    if (s.restore.dragPan) map.dragPan.enable();
    if (s.restore.boxZoom) map.boxZoom.enable();
  }
  // The rest of a double-click (its second click and the dblclick) is still on its way: keep
  // swallowing map clicks and hold back double-click zoom for a moment.
  flushGrace();
  const done = () => {
    grace = null;
    for (const off of s.swallowOff) { try { off(); } catch { /* ignore */ } }
    if (map && s.restore.dblZoom) map.doubleClickZoom.enable();
    if (!session) mapx.setDispatchPaused(false);
  };
  grace = { timer: setTimeout(done, GRACE_MS), done };
  try { s.resolve(feature); } catch { /* ignore */ }
}

function flushGrace() {
  if (!grace) return;
  clearTimeout(grace.timer);
  grace.done();
}

// ------------------------------------------------------------------ pointer and keyboard handling
// Positions are relative to the canvas (MapLibre's canvas container has no height of its own).
function point(e) {
  const r = getMap().getCanvas().getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

/** [lng, lat] under a container point, or null off the globe (in space). */
function lngLatAt(pt) {
  const map = getMap();
  let ll;
  try { ll = map.unproject([pt.x, pt.y]); } catch { return null; }
  if (!ll || !Number.isFinite(ll.lng) || !Number.isFinite(ll.lat)) return null;
  if (mapx.viewState().projection === 'globe') {
    const back = map.project(ll);
    if (!back || !(Math.hypot(back.x - pt.x, back.y - pt.y) <= 3)) return null;
    return [ll.lng, Math.max(-90, Math.min(90, ll.lat))];
  }
  return [ll.lng, Math.max(-85.0511, Math.min(85.0511, ll.lat))];
}

function screenOf(ll) {
  try { const p = getMap().project(ll); return { x: p.x, y: p.y }; } catch { return null; }
}

const dist = (a, b) => (a && b ? Math.hypot(a.x - b.x, a.y - b.y) : Infinity);
const tol = (type) => TAP_PX[type] || TAP_PX.mouse;

function onCanvas(pt) {
  const r = getMap().getCanvas().getBoundingClientRect();
  return pt.x >= 0 && pt.y >= 0 && pt.x <= r.width && pt.y <= r.height;
}

function onDown(e) {
  const s = session;
  if (!s || !s.inMap(e)) return;
  if (e.isPrimary) s.pointers.clear();                               // forget any pointer whose "up" we missed
  s.pointers.add(e.pointerId);
  if (s.pointers.size > 1) { s.press = null; return; }              // a pinch, not a click
  if (e.pointerType === 'mouse' && e.button !== 0) return;           // right-drag rotates the map
  if (s.mode !== 'polygon' && (e.ctrlKey || e.metaKey)) return;      // ctrl-drag rotates too
  const pt = point(e);
  s.screen = pt;
  s.press = { id: e.pointerId, x: pt.x, y: pt.y, t: performance.now(), type: e.pointerType || 'mouse', moved: false, anchor: false };
  if (s.mode !== 'polygon' && !s.points.length) {
    const ll = lngLatAt(pt);
    if (ll) { s.points = [ll]; s.cursor = ll; s.press.anchor = true; update(); }
  }
}

function onMove(e) {
  const s = session;
  if (!s) return;
  const pressing = s.press && s.press.id === e.pointerId;
  if (!pressing && !s.inMap(e)) return;
  const pt = point(e);
  s.screen = pt;
  if (pressing && !s.press.moved && dist(pt, s.press) > tol(s.press.type)) s.press.moved = true;
  const ll = lngLatAt(pt);
  if (ll) s.cursor = ll;
  scheduleUpdate();
}

function onUp(e) {
  const s = session;
  if (!s) return;
  s.pointers.delete(e.pointerId);
  const press = s.press;
  if (!press || press.id !== e.pointerId) return;
  s.press = null;
  const pt = point(e);
  const tap = !press.moved && dist(pt, press) <= tol(press.type);
  if (s.mode === 'polygon') {
    if (tap) addPoint(pt, press);
    return;
  }
  if (!s.points.length) return;                       // pressed off the globe
  if (press.anchor && tap) { update(); return; }      // centre or corner placed by a click
  const edge = onCanvas(pt) ? lngLatAt(pt) : null;    // released in space or off the map: keep going
  if (edge) finishShape(edge, pt); else update();     // dragged out, or the second click
}

function onPointerCancel(e) {
  const s = session;
  if (!s) return;
  s.pointers.delete(e.pointerId);
  if (s.press?.id === e.pointerId) s.press = null;
}

function onMapMove() {
  const s = session;
  if (!s || !s.screen) return;
  const ll = lngLatAt(s.screen);
  if (ll) s.cursor = ll;
  scheduleUpdate();
}

function editable(t) {
  return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ''));
}

function onKey(e) {
  const s = session;
  if (!s) return;
  if (document.querySelector('.modal-backdrop, .menu, .ctx-menu')) return;   // ui.js closes those first
  const key = e.key;
  const stop = () => { e.preventDefault(); e.stopPropagation(); };
  if (key === 'Escape') { stop(); end(null); return; }
  if (editable(e.target)) return;
  if (key === 'Enter') { stop(); finishNow(); return; }
  if (key === 'Backspace' || key === 'Delete' || ((e.ctrlKey || e.metaKey) && (key === 'z' || key === 'Z'))) { stop(); undo(); }
}

// ------------------------------------------------------------------ editing
function addPoint(pt, press) {
  const s = session;
  const ll = lngLatAt(pt);
  if (!ll) return;
  const close = CLOSE_PX[press.type] || CLOSE_PX.mouse;
  const last = s.points[s.points.length - 1];
  if (last && dist(screenOf(last), pt) < close) {
    // A second click on the last point: a double-click (or double-tap) finishes, otherwise nothing.
    if (press.t - s.lastTap < DOUBLE_MS && s.points.length >= 3) finishPolygon();
    s.lastTap = press.t;
    return;
  }
  if (s.points.length >= 3 && dist(screenOf(s.points[0]), pt) < close) { finishPolygon(); return; }
  s.points.push(ll);
  s.cursor = ll;
  s.lastTap = press.t;
  update();
}

function undo() {
  const s = session;
  if (!s || !s.points.length) return;
  if (s.mode === 'polygon') s.points.pop(); else s.points = [];
  s.lastTap = 0;
  update();
}

function finishNow() {
  const s = session;
  if (!s) return;
  if (s.mode === 'polygon') finishPolygon();
  else if (s.points.length && s.cursor) finishShape(s.cursor, screenOf(s.cursor));
}

function finishPolygon() {
  const s = session;
  if (!s) return;
  const f = s.points.length >= 3 ? G.polygonFeature(s.points) : null;
  if (!f) { flash('Add at least three points to make an area.'); return; }
  end(f);
}

function shapeFrom(s, edge) {
  if (!s.points.length || !edge) return null;
  const a = s.points[0];
  if (s.mode === 'circle') return G.circleFeature(a, G.haversineKm(a[1], a[0], edge[1], edge[0]));
  return G.rectangleFeature(a, edge);
}

function finishShape(edge, pt) {
  const s = session;
  if (!s) return;
  edge = edge || s.cursor;
  const f = shapeFrom(s, edge);
  if (!f || dist(screenOf(s.points[0]), pt || screenOf(edge)) < MIN_PX) { update(); return; }   // too small: keep going
  end(f);
}

// ------------------------------------------------------------------ preview and hint bar
function scheduleUpdate() {
  const s = session;
  if (!s || s.raf) return;
  s.raf = requestAnimationFrame(() => { s.raf = 0; if (session === s) update(); });
}

function previewData(s) {
  const feats = [];
  const add = (geometry, properties) => feats.push({ type: 'Feature', geometry, properties });
  if (s.mode === 'polygon') {
    const pts = G.unwrapPath(s.points.length && s.cursor ? [...s.points, s.cursor] : [...s.points]);
    if (pts.length >= 3) add({ type: 'Polygon', coordinates: [[...pts, pts[0]]] }, { role: 'shape' });
    if (pts.length >= 2) add({ type: 'LineString', coordinates: pts }, { role: 'path' });
    if (pts.length >= 3) add({ type: 'LineString', coordinates: [pts[pts.length - 1], pts[0]] }, { role: 'guide' });
    pts.slice(0, s.points.length).forEach((p, i) => add({ type: 'Point', coordinates: p }, { role: 'vertex', first: i === 0 && s.points.length >= 3 }));
    return { type: 'FeatureCollection', features: feats };
  }
  if (!s.points.length) return { type: 'FeatureCollection', features: feats };
  const f = shapeFrom(s, s.cursor);
  if (f) {
    add(f.geometry, { role: 'shape' });
    add({ type: 'LineString', coordinates: f.geometry.coordinates[0] }, { role: 'path' });
  }
  const [a, b] = G.unwrapPath([s.points[0], s.cursor || s.points[0]]);
  if (s.mode === 'circle' && f) add({ type: 'LineString', coordinates: [a, b] }, { role: 'guide' });
  add({ type: 'Point', coordinates: a }, { role: 'vertex', first: false });
  return { type: 'FeatureCollection', features: feats };
}

const kbd = (k) => h('kbd', null, k);

function hintText(s) {
  const n = s.points.length;
  if (s.mode === 'polygon') {
    if (!n) return ['Click the map to place the first point.'];
    if (n < 3) return ['Click to add more points. ', kbd('Backspace'), ' removes the last one.'];
    return ['Double-click, press ', kbd('Enter'), ' or click the first point to finish.'];
  }
  if (s.mode === 'circle') return n ? ['Click the edge of the circle.'] : ['Press at the centre and drag out the radius, or click the centre.'];
  return n ? ['Click the opposite corner.'] : ['Press at one corner and drag to the opposite one, or click both corners.'];
}

function area(km2) {
  if (!(km2 > 0)) return '';
  return `${km2 >= 100 ? fmt.int(km2) : fmt.num(km2, km2 < 10 ? 2 : 1)} km²`;
}

function measureText(s, f) {
  if (s.mode === 'polygon') {
    const n = s.points.length;
    if (!n) return '';
    const done = n >= 3 ? G.polygonFeature(s.points) : null;
    return `${n} point${n === 1 ? '' : 's'}${done ? `, ${area(done.properties.area_km2)}` : ''}`;
  }
  if (!f) return '';
  if (s.mode === 'circle') return `Radius ${fmt.km(f.properties.radius_km)}, ${area(f.properties.area_km2)}`;
  const [w, south, e, north] = [f.geometry.coordinates[0][0][0], f.bbox[1], f.geometry.coordinates[0][1][0], f.bbox[3]];
  const mid = (south + north) / 2;
  return `${fmt.km(G.haversineKm(mid, w, mid, e))} by ${fmt.km(G.haversineKm(south, w, north, w))}`;
}

function mountHint(s) {
  const wrap = document.getElementById('map-wrap') || getMap().getContainer().parentElement;
  const text = h('span', { class: 'draw-hint-text', role: 'status', 'aria-live': 'polite' });
  const measure = h('span', { class: 'draw-hint-measure tnum' });
  const undoBtn = h('button', { class: 'btn', type: 'button', title: 'Remove the last point (Backspace)' }, 'Undo');
  const finishBtn = h('button', { class: 'btn primary', type: 'button', title: 'Finish the shape (Enter)', hidden: s.mode !== 'polygon' }, 'Finish');
  const cancelBtn = h('button', { class: 'btn', type: 'button', title: 'Stop drawing (Esc)' }, 'Cancel');
  undoBtn.addEventListener('click', undo);
  finishBtn.addEventListener('click', finishNow);
  cancelBtn.addEventListener('click', () => { if (session === s) end(null); });
  const el = h('div', { class: 'draw-hint', role: 'group', 'aria-label': `Drawing a ${s.mode}`, style: { '--draw-color': s.color } },
    h('span', { class: 'draw-hint-mode' }, MODE_LABEL[s.mode]), text, measure, h('span', { class: 'draw-hint-tools' }, undoBtn, finishBtn, cancelBtn));
  wrap.append(el);
  s.hint = { el, text, measure, undoBtn, finishBtn, key: '' };
}

function flash(message) {
  const s = session;
  if (!s?.hint) return;
  clear(s.hint.text).append(message);
  s.hint.key = 'flash';
  s.hint.flashUntil = performance.now() + 2500;
  s.hint.el.classList.remove('flash');
  void s.hint.el.offsetWidth;   // restart the animation
  s.hint.el.classList.add('flash');
  setTimeout(() => { if (session === s) update(); }, 2600);
}

function update() {
  const s = session;
  if (!s) return;
  mapx.setData(SRC, previewData(s));
  if (!s.hint) return;
  const key = `${s.mode}:${s.points.length < 3 ? s.points.length : 3}`;
  const flashing = performance.now() < (s.hint.flashUntil || 0);
  if (!flashing && key !== s.hint.key) {   // only rewrite the live region when the instruction changes
    s.hint.key = key;
    clear(s.hint.text).append(...hintText(s).map(x => (typeof x === 'string' ? document.createTextNode(x) : x)));
  }
  s.hint.measure.textContent = measureText(s, s.mode === 'polygon' ? null : shapeFrom(s, s.cursor));
  s.hint.undoBtn.disabled = !s.points.length;
  s.hint.finishBtn.disabled = s.points.length < 3;
}

// ------------------------------------------------------------------ saved shapes (show / hide)
const shown = new Map();   // id -> {geometry, color, label, at}

/** Show a saved Polygon or MultiPolygon (Feature or geometry) under id; showing an id again replaces it. */
export function show(id, feature, { color = DEFAULT_COLOR, label = null } = {}) {
  const key = id == null ? '' : String(id);
  if (!key) throw new TypeError('draw.show needs an id');
  const geometry = feature?.type === 'Feature' ? feature.geometry : feature;
  if (!validArea(geometry)) throw new TypeError('draw.show needs a Polygon or MultiPolygon');
  const text = label == null ? '' : String(label).slice(0, 80);
  shown.set(key, { geometry, color: colorOf(color), label: text, at: text ? G.labelPoint(feature) : null });
  renderShown();
}

export function hide(id) {
  if (shown.delete(id == null ? '' : String(id))) renderShown();
}

function validArea(g) {
  const ring = (r) => Array.isArray(r) && r.length >= 4 && r.every(p => Array.isArray(p) && p.length >= 2
    && Number.isFinite(p[0]) && Number.isFinite(p[1]) && Math.abs(p[1]) <= 90);
  const poly = (rings) => Array.isArray(rings) && rings.length > 0 && rings.every(ring);
  if (!g || !Array.isArray(g.coordinates)) return false;
  if (g.type === 'Polygon') return poly(g.coordinates);
  if (g.type === 'MultiPolygon') return g.coordinates.length > 0 && g.coordinates.every(poly);
  return false;
}

function renderShown() {
  if (!getMap()) return;
  if (!shown.size) {
    mapx.removeSource(SHAPES);
    mapx.removeSource(SHAPE_LABELS);
    return;
  }
  const fc = (features) => ({ type: 'FeatureCollection', features });
  const items = [...shown.entries()];
  mapx.addSource(SHAPES, { type: 'geojson', data: fc(items.map(([id, s]) => ({ type: 'Feature', geometry: s.geometry, properties: { id, color: s.color } }))) });
  mapx.addLayer({ id: `${SHAPES}-fill`, type: 'fill', source: SHAPES, paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.12 } }, 'polygons');
  mapx.addLayer({ id: `${SHAPES}-line`, type: 'line', source: SHAPES, layout: { 'line-join': 'round' },
    paint: { 'line-color': ['get', 'color'], 'line-width': 1.8, 'line-opacity': 0.9 } }, 'lines');
  const labels = items.filter(([, s]) => s.label && s.at);
  if (!labels.length) { mapx.removeSource(SHAPE_LABELS); return; }
  mapx.addSource(SHAPE_LABELS, { type: 'geojson', data: fc(labels.map(([id, s]) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: s.at }, properties: { id, label: s.label, color: s.color } }))) });
  mapx.addLayer({ id: SHAPE_LABELS, type: 'symbol', source: SHAPE_LABELS,
    layout: { 'text-field': ['get', 'label'], 'text-font': ['Open Sans Bold'], 'text-size': 12, 'text-max-width': 12 },
    paint: { 'text-color': ['get', 'color'], 'text-halo-color': '#111a2c', 'text-halo-width': 1.6 } }, 'top');
}

export const draw = { start, cancel, active, show, hide, contains };
