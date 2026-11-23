// intel: Conflict and Intelligence groups (CONTRACT.md section 13). The layers live in ./intel/*.js.
//
// Conflict:      intel.conflict (GDELT events), intel.protests (GDELT CAMEO 14), intel.frontlines (DeepStateMap),
//                intel.ucdp (UCDP GED)
// Intelligence:  intel.news (+ News drawer with unusual news volume), intel.countries (instability index),
//                intel.advice (FCDO and US travel advice), intel.risk, intel.convergences, intel.cyber (+ Cloudflare Radar)

import { registerConflict } from './intel/conflict.js';
import { registerFrontlines } from './intel/frontlines.js';
import { registerUcdp } from './intel/ucdp.js';
import { registerNews } from './intel/news.js';
import { registerCountries } from './intel/countries.js';
import { registerZones } from './intel/zones.js';
import { registerCyber } from './intel/cyber.js';
import { registerProtests } from './intel/protests.js';
import { registerAdvice } from './intel/advice.js';
import { registerSpikes } from './intel/spikes.js';

export async function register(ctx) {
  // Registration order is the order inside each group of the layer panel.
  const parts = [registerConflict, registerProtests, registerFrontlines, registerUcdp, registerNews, registerCountries, registerAdvice,
    registerZones, registerCyber, registerSpikes];
  for (const part of parts) {
    try {
      part(ctx);
    } catch (err) {
      console.error(`intel: ${part.name} failed`, err);
    }
  }
}
