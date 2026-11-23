// Space module: SGP4 propagation off the main thread (module worker, satellite.js from web/vendor).
//   {type: 'set', seq, omm: [...]}  -> {type: 'ready', seq, n, failed}
//   {type: 'tick', seq, t, limit?}  -> {type: 'pos', seq, t, buf: Float32Array [lon, lat, altKm] * limit}  (transferred)

import * as sat from '/vendor/satellite.js/dist/index.js';

const R2D = 180 / Math.PI;
let recs = [];
let setSeq = 0;

self.onmessage = (event) => {
  const msg = event.data || {};
  if (msg.type === 'set') {
    setSeq = msg.seq;
    let failed = 0;
    recs = (msg.omm || []).map((o) => {
      try { return sat.json2satrec(o); } catch { failed += 1; return null; }
    });
    self.postMessage({ type: 'ready', seq: msg.seq, n: recs.length, failed });
    return;
  }
  if (msg.type === 'tick') {
    const d = new Date(msg.t);
    const gmst = sat.gstime(d);
    // a partial tick (limit) covers only the first `limit` satellites, the named ones
    const n = Math.min(recs.length, Number.isFinite(msg.limit) ? msg.limit : recs.length);
    const buf = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const r = recs[i];
      let lon = NaN;
      let lat = NaN;
      let alt = NaN;
      if (r) {
        try {
          const pv = sat.propagate(r, d);
          if (pv && pv.position) {
            const g = sat.eciToGeodetic(pv.position, gmst);
            lon = g.longitude * R2D;
            lat = g.latitude * R2D;
            alt = g.height;
            if (!(alt > -50)) { lon = NaN; lat = NaN; }
          }
        } catch { /* decayed or out-of-range elements: leave NaN */ }
      }
      buf[i * 3] = lon;
      buf[i * 3 + 1] = lat;
      buf[i * 3 + 2] = alt;
    }
    self.postMessage({ type: 'pos', seq: msg.seq, setSeq, t: msg.t, buf }, [buf.buffer]);
  }
};
