# Third-party software and data

Meridian's own code is MIT licensed (see [LICENSE](LICENSE)). The browser libraries below are
vendored unchanged under `web/vendor/` and keep their own licences; each folder contains the
original licence file.

| Library | Version | Folder | Licence | Licence file |
|---|---|---|---|---|
| [MapLibre GL JS](https://github.com/maplibre/maplibre-gl-js) | 5.24.1 | `web/vendor/maplibre-gl/` | BSD 3-Clause (with the notices in its licence file) | [LICENSE.txt](web/vendor/maplibre-gl/LICENSE.txt) |
| [hls.js](https://github.com/video-dev/hls.js) | 1.6.15 | `web/vendor/hls.js/` | Apache License 2.0 | [LICENSE](web/vendor/hls.js/LICENSE) (upstream notice) and [LICENSE-2.0.txt](web/vendor/hls.js/LICENSE-2.0.txt) (full licence text) |
| [satellite.js](https://github.com/shashwatak/satellite-js) | 7.2.0 | `web/vendor/satellite.js/` | MIT | [LICENSE.md](web/vendor/satellite.js/LICENSE.md) |

The Python server uses the standard library only; there are no Python dependencies.

## Data

The data Meridian downloads at runtime is not part of this repository and is not covered by the MIT
licence. Every source, its licence or terms, and how often it is fetched are listed in
[docs/SOURCES.md](docs/SOURCES.md); basemaps and other sources the browser loads directly are listed
in the README under "Map tiles and browser sources". Some sources allow personal, non-commercial use
only.

Test fixtures under `tests/fixtures/` let the tests run offline. Many are synthetic: they copy only the
shape of a source's responses, with invented names, IDs, numbers, text and links. The rest are small
samples of public responses. The fixtures are not covered by the MIT licence: each keeps its source's
licence, and share-alike (CC BY-SA) and ODbL fixtures stay under those licences. Every fixture file is
listed with its source, licence and attribution line in [tests/fixtures/README.md](tests/fixtures/README.md).
