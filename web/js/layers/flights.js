// Flights module, browser half: live aircraft, military and emergency layers, aircraft cards,
// trails, GPS interference, airspace and airport status (US airport delays, US flight
// restrictions, EASA conflict-zone advisories) and air activity (military surges, GPS
// interference trends, airport movement drops). Data comes from /api/flights/* (adsb.fi,
// adsb.lol, OpenSky, adsbdb, FAA, EASA).

import { registerIcons } from './flights/icons.js';
import * as traffic from './flights/traffic.js';
import * as cards from './flights/card.js';
import * as gpsjam from './flights/gpsjam.js';
import * as airspace from './flights/airspace.js';
import * as activity from './flights/activity.js';
import { AMBER, CLASSES, GROUP, RED, rowEmergencyText, titleOf } from './flights/style.js';

const CLASS_CHOICES = [
  { value: 'all', label: 'Everything' },
  ...Object.entries(CLASSES).filter(([k]) => k !== 'unknown').map(([value, c]) => ({ value, label: c.label })),
  { value: 'emergency', label: 'Emergencies' },
  { value: 'sanctioned', label: 'OFAC-listed' },
];

export async function register(ctx) {
  traffic.init(ctx);
  cards.init(ctx);
  registerIcons(ctx.map2);

  // -- a quiet hint on the map when the view is too wide for full traffic ---------------------------
  const hint = ctx.h('div', { class: 'flights-hint', role: 'status', hidden: true });
  document.getElementById('map-wrap')?.append(hint);
  let hintTimer = null;
  let hintMode = null;
  const showHint = (text) => {
    clearTimeout(hintTimer);
    hint.textContent = text || '';
    hint.hidden = !text;
    if (text) hintTimer = setTimeout(() => { hint.hidden = true; }, 8000);   // the layer row keeps the warning
  };

  // -- the main layer: the area on screen ------------------------------------------------------------
  let followups = 0;
  const main = ctx.layers.register({
    id: 'flights', group: 'Flights', title: 'Aircraft', subtitle: 'Live ADS-B from adsb.fi, with adsb.lol and OpenSky as backups',
    description: 'Live aircraft for the area on screen, coloured by class. Emergencies are red, OFAC-listed aircraft are ringed in red, and hollow outlines are estimated positions for aircraft that left receiver coverage. Zoomed out, you see military aircraft, emergencies and traffic already loaded.',
    source: 'adsb.fi open data (personal, non-commercial use), adsb.lol (ODbL), OpenSky Network; routes from adsbdb',
    color: GROUP, symbol: 'icon', defaultOn: true, refreshMs: 10000, viewDependent: true,
    options: [
      { id: 'kind', label: 'Show', type: 'select', default: 'all', choices: CLASS_CHOICES, refresh: false },
      { id: 'military', label: 'Military only', type: 'toggle', default: false, refresh: false },
      { id: 'hideGround', label: 'Hide aircraft on the ground', type: 'toggle', default: false, refresh: false },
      { id: 'vehicles', label: 'Show airport ground vehicles', type: 'toggle', default: false, refresh: false },
      { id: 'minAlt', label: 'Lowest altitude', type: 'range', min: 0, max: 40000, step: 1000, default: 0, refresh: false,
        format: v => (v ? `${ctx.fmt.int(v)} ft` : 'any') },
      { id: 'estimated', label: 'Estimated positions', type: 'toggle', default: true, refresh: false },
      { id: 'labels', label: 'Callsign labels', type: 'toggle', default: true, refresh: false },
      { id: 'photos', label: 'Photos from planespotters.net in cards', type: 'toggle', default: false, refresh: false },
    ],
    legend: [
      ...Object.values(CLASSES).map(c => ({ color: c.color, label: c.label })),
      { color: RED, label: 'Emergency' },
      { color: RED, label: 'OFAC-listed (red ring)', shape: 'line' },
      { color: '#aab4ca', label: 'Hollow outline: estimated position', shape: 'line' },
    ],
    async enable({ options }) {
      traffic.setOptions(options || {});
      ctx.settings.set('flights.photos', !!options?.photos);
      traffic.setActive('view', true);
    },
    async disable() {
      traffic.setActive('view', false);
      showHint(null);
      hintMode = null;
    },
    async refresh({ bounds, zoom, signal, reason }) {
      const res = await ctx.api.get('/api/flights/live', { bbox: bounds.join(','), zoom: zoom.toFixed(2) }, { signal });
      if (signal.aborted || !main.isOn()) return;
      traffic.setRows('view', res);
      main.setCount(traffic.count('view'));
      if (res.error) main.setStatus('error', res.error);
      else if (res.mode === 'wide') main.setStatus('warn', 'Zoom in to see all traffic. Zoomed out, only military aircraft, emergencies and traffic already loaded are shown.');
      else if (res.truncated) main.setStatus('warn', 'Very busy area: showing the most important aircraft. Zoom in to see them all.');
      if (res.mode !== hintMode) { hintMode = res.mode; showHint(res.mode === 'wide' ? res.note : null); }
      if (res.tiles?.pending > 0 && reason !== 'followup' && followups < 2) {
        followups += 1;
        setTimeout(() => { followups = Math.max(0, followups - 1); main.refresh('followup'); }, 2500);
      }
    },
    onOption(id, value, all) {
      traffic.setOptions(all);
      if (id === 'photos') ctx.settings.set('flights.photos', !!value);
      main.setCount(traffic.count('view'));
    },
  });

  // -- military worldwide ---------------------------------------------------------------------------
  const mil = ctx.layers.register({
    id: 'flights.military', group: 'Flights', title: 'Military aircraft', subtitle: 'Worldwide, those broadcasting ADS-B',
    description: 'Military aircraft anywhere in the world that broadcast ADS-B, from the community military database flag. Many military flights do not broadcast, so this is never the full picture.',
    source: 'adsb.fi open data (personal, non-commercial use), adsb.lol (ODbL)', color: CLASSES.military.color, symbol: 'icon',
    defaultOn: false, refreshMs: 60000,
    async enable() { traffic.setActive('mil', true); },
    async disable() { traffic.setActive('mil', false); },
    async refresh({ signal }) {
      const res = await ctx.api.get('/api/flights/military', null, { signal });
      if (signal.aborted || !mil.isOn()) return;
      traffic.setRows('mil', res);
      mil.setCount(traffic.count('mil'));
      if (res.error) mil.setStatus('error', res.error);
    },
  });

  // -- emergencies worldwide ------------------------------------------------------------------------
  let knownEmergencies = null;
  const alerts = ctx.layers.register({
    id: 'flights.alerts', group: 'Flights', title: 'Aircraft emergencies', subtitle: 'Squawk 7500, 7600 and 7700 worldwide',
    description: 'Aircraft anywhere squawking 7500 (unlawful interference), 7600 (radio failure) or 7700 (general emergency), or reporting an emergency status. Short squawks can be mistakes.',
    source: 'adsb.fi open data squawk search (personal, non-commercial use)', color: RED, symbol: 'icon',
    defaultOn: false, refreshMs: 30000,
    legend: [{ color: RED, label: 'Emergency (red halo)' }],
    async enable() { traffic.setActive('alerts', true); },
    async disable() { traffic.setActive('alerts', false); knownEmergencies = null; },
    async refresh({ signal }) {
      const res = await ctx.api.get('/api/flights/alerts', null, { signal });
      if (signal.aborted || !alerts.isOn()) return;
      const map = traffic.setRows('alerts', res);
      alerts.setCount(map.size);
      const recent = (res.recent || []).length;
      if (!map.size) alerts.setStatus('ok', recent ? `None right now. ${recent} in the last 12 h.` : 'No aircraft are reporting an emergency right now.');
      if (knownEmergencies) {
        for (const row of map.values()) {
          if (!knownEmergencies.has(row.hex)) ctx.ui.toast(`${titleOf(row)}: ${rowEmergencyText(row)}`, 'red', 7000);
        }
      }
      knownEmergencies = new Set(map.keys());
    },
  });

  // -- interaction ----------------------------------------------------------------------------------
  ctx.map2.onClick([traffic.L_ICON, traffic.L_EMG, traffic.L_SANC], (f) => {
    const hex = f.properties?.hex;
    if (!hex) return;
    const [lon, lat] = f.geometry.coordinates;
    cards.openAircraft(hex, { lat, lon });
  });
  ctx.map2.onHover([traffic.L_ICON, traffic.L_EMG], f => traffic.hoverText(f));
  ctx.entities.handler('aircraft', (e) => cards.openAircraft(e.id, e), { group: 'Flights', icon: '✈' });
  ctx.entities.handler('sanctioned_aircraft', (e) => cards.openSanctioned(e), { group: 'Flights', icon: '✈' });
  ctx.search.provider(q => traffic.search(q));

  gpsjam.register(ctx);
  const air = airspace.register(ctx);
  const act = activity.register(ctx);

  // Debug and test handle (tools/browser_check.mjs): window.__meridian.flights
  ctx.flights = { traffic, cards, gpsjam, airspace: air, activity: act,
    layers: { main, mil, alerts, ...air.layers, activity: act.layer }, colours: { RED, AMBER } };
}
