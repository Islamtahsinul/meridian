// Infrastructure and Companies (CONTRACT.md module "infra"): power plants, airports, ports, data
// centres, dams, submarine cables, listed companies, the markets drawer and live electricity grids.

import { createShared } from './infra/shared.js';
import { registerPower } from './infra/power.js';
import { registerAirports } from './infra/airports.js';
import { registerPorts } from './infra/ports.js';
import { registerDatacenters } from './infra/datacenters.js';
import { registerDams } from './infra/dams.js';
import { registerCables } from './infra/cables.js';
import { registerCompanies } from './infra/companies.js';
import { registerMarkets } from './infra/markets.js';
import { registerEnergy } from './infra/energy.js';

export async function register(ctx) {
  const s = createShared(ctx);
  const parts = { registerPower, registerAirports, registerPorts, registerDatacenters, registerDams, registerCables, registerCompanies, registerEnergy };
  const api = {};
  for (const [name, fn] of Object.entries(parts)) {
    try { Object.assign(api, fn(ctx, s)); } catch (err) { console.error(`infra: ${name} failed to start`, err); }
  }
  try { Object.assign(api, registerMarkets(ctx)); } catch (err) { console.error('infra: markets failed to start', err); }
  ctx.infra = api;   // handy for tests: __meridian.infra.open()
}
