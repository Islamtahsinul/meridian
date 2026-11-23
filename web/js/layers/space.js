// Space module (CONTRACT.md section 13): satellites and the ISS, launches, rain radar, NASA GIBS
// weather and imagery layers, Meteosat imagery and lightning (EUMETSAT), point weather, the
// before/after imagery compare tool, the satellite scene finder, the aurora forecast and the space
// weather panel (NOAA SWPC).
// The code lives in web/js/layers/space/*.js; this file only wires it up.

import { registerSatellites } from './space/satellites.js';
import { registerLaunches } from './space/launches.js';
import { registerWeather } from './space/weather.js';
import { registerImagery } from './space/imagery.js';
import { registerAurora } from './space/aurora.js';
import { registerSpaceWeather } from './space/spaceweather.js';
import { registerScenes } from './space/scenes.js';

export async function register(ctx) {
  // Each part fails on its own: a broken part never stops the others.
  const parts = [['satellites', registerSatellites], ['launches', registerLaunches], ['weather', registerWeather], ['imagery', registerImagery],
    ['aurora', registerAurora], ['space weather', registerSpaceWeather], ['scene finder', registerScenes]];
  for (const [name, fn] of parts) {
    try {
      await fn(ctx);
    } catch (err) {
      console.error(`space: the ${name} part failed to start`, err);
    }
  }
}
