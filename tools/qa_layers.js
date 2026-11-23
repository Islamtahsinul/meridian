// Browser QA expression for tools/browser_check.mjs --eval "$(cat tools/qa_layers.js)".
// Turns every layer on in turn (alone), waits for it to settle, records its count/status/message,
// then opens one card of each main kind. Returns a JSON summary.
(async () => {
  const M = window.__meridian;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = { layers: [], cards: [], errors: [] };
  const errs = [];
  window.addEventListener('error', (e) => errs.push(String(e.message || e)));
  window.addEventListener('unhandledrejection', (e) => errs.push(String(e.reason && (e.reason.stack || e.reason.message) || e.reason)));
  const views = {
    flights: [51.47, -0.45, 8], 'flights.gpsjam': [57, 22, 4], ships: [59.4, 24.6, 7], 'ships.straits': [56, 15, 4],
    cameras: [51.5, -0.12, 10], 'infra.airports': [51.5, -0.3, 8], 'infra.ports': [53.5, 9.9, 8],
    'intel.frontlines': [48.5, 36, 5], 'space.radar': [51, 0, 5], 'hazards.warnings': [50, 5, 4],
  };
  const all = M.layers.list().map((l) => l.id);
  for (const id of all) M.layers.setOn(id, false);
  await sleep(500);
  for (const id of all) {
    const v = views[id] || views[id.split('.')[0]] || [25, 10, 2];
    M.map.jumpTo({ center: [v[1], v[0]], zoom: v[2] });
    const t0 = performance.now();
    await M.layers.setOn(id, true);
    for (let i = 0; i < 30; i++) {
      const l = M.layers.list().find((x) => x.id === id);
      if (l.status !== 'loading') break;
      await sleep(300);
    }
    await sleep(600);
    const l = M.layers.list().find((x) => x.id === id);
    out.layers.push([id, l.status, l.count, Math.round(performance.now() - t0), (l.message || '').slice(0, 90)]);
    await M.layers.setOn(id, false);
  }
  // One card per kind, found through the core search/nearby APIs.
  const tries = [
    ['camera', () => M.api.get('/api/core/nearby', { lat: 51.5, lon: -0.12, radius_km: 3, kind: 'camera', limit: 1 }).then((r) => r.results[0])],
    ['airport', async () => ({ kind: 'airport', id: 'EGLL' })],
    ['country', async () => ({ kind: 'country', id: 'GB' })],
    ['satellite', async () => ({ kind: 'satellite', id: '25544' })],
    ['search Tallinn port', () => M.api.get('/api/core/search', { q: 'Tallinn' }).then((r) => r.results.find((x) => x.kind === 'port'))],
    ['search power plant', () => M.api.get('/api/core/search', { q: 'Drax' }).then((r) => r.results.find((x) => x.kind === 'power_plant'))],
    ['search cable', () => M.api.get('/api/core/search', { q: '2Africa' }).then((r) => r.results.find((x) => x.kind === 'cable'))],
    ['search company', () => M.api.get('/api/core/search', { q: 'Unilever' }).then((r) => r.results.find((x) => x.kind === 'company'))],
    ['quake', () => M.api.get('/api/core/events', { hours: 48, kinds: 'earthquake', limit: 1 }).then((r) => r.events[0])],
    ['conflict', () => M.api.get('/api/core/events', { hours: 48, kinds: 'conflict_event', limit: 1 }).then((r) => r.events[0])],
    ['ship', () => M.api.get('/api/core/nearby', { lat: 59.44, lon: 24.76, radius_km: 20, kind: 'ship', limit: 1 }).then((r) => r.results[0])],
    ['aircraft', () => M.api.get('/api/core/nearby', { lat: 51.47, lon: -0.45, radius_km: 60, kind: 'aircraft', limit: 1 }).then((r) => r.results[0])],
    ['launch', () => M.api.get('/api/core/search', { q: 'Falcon' }).then((r) => r.results.find((x) => x.kind === 'launch'))],
    ['strait', () => M.api.get('/api/core/search', { q: 'Hormuz' }).then((r) => r.results.find((x) => x.kind === 'strait'))],
  ];
  for (const [name, find] of tries) {
    try {
      const ent = await find();
      if (!ent) { out.cards.push([name, 'not found']); continue; }
      await M.entities.open(ent, { fly: false });
      await sleep(1800);
      const top = document.querySelector('#cards .card');
      out.cards.push([name, top ? top.querySelector('.card-title')?.textContent : 'no card', top ? top.querySelectorAll('.card-section').length : 0]);
    } catch (err) {
      out.cards.push([name, 'ERROR ' + (err && err.message)]);
    }
  }
  out.errors = errs.slice(0, 20);
  return out;
})()
