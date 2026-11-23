"""GloFAS flood outlook: the forecast date from the WMS capabilities (every 6 hours while the layer
is in use) and a small tile proxy that turns XYZ tiles into WMS GetMap requests, with a memory cache
of six hours so the same tiles are not rendered twice. Helpers are in ``glofas.py``.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from meridian.net import NetError

from . import glofas

TILE_TTL = 6 * 3600
MAX_TILES = 3000
MAX_BYTES = 48_000_000


class Outlook:
    def __init__(self, svc):
        self.svc = svc
        self.net = svc.net
        self.log = svc.log
        self.times: dict[str, str] = {}          # WMS layer -> default time ("2026-11-23T00:00Z")
        self._tiles: OrderedDict[tuple, tuple[float, bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.served = 0
        self.fetched = 0
        self.errors: list[tuple[float, str]] = []

    def refresh(self):
        text = self.net.get_text(glofas.WMS_URL, params={"service": "WMS", "request": "GetCapabilities",
                                                          "version": "1.3.0"}, timeout=45, retries=1)
        times = glofas.parse_times(text)
        if not times:
            raise RuntimeError("the GloFAS capabilities list none of the flood summary layers")
        self.times = times
        date = glofas.date_of(times.get(glofas.PRODUCTS["days1-3"][0]))
        return len(times), f"forecast of {date}" if date else "forecast date unknown"

    def date(self) -> str | None:
        return glofas.date_of(self.times.get(glofas.PRODUCTS["days1-3"][0]))

    def meta(self) -> dict:
        now = time.time()
        recent = [e for e in self.errors if now - e[0] < 600]
        date = self.date()
        year = (date or time.strftime("%Y", time.gmtime(now)))[:4]
        return {"date": date, "products": [{"id": k, "layer": v[0], "title": v[1], "time": self.times.get(v[0])}
                                           for k, v in glofas.PRODUCTS.items()],
                "legend": glofas.LEGEND, "note": glofas.NOTE, "maxzoom": 8,
                "credit": f"Generated using Copernicus Emergency Management Service information {year}",
                "site": glofas.SITE_URL, "licence": glofas.LICENCE_URL,
                "tile_errors": len(recent), "last_error": recent[-1][1] if recent else None,
                "tiles_cached": len(self._tiles), "source": self.svc.source_info("glofas")}

    def tile(self, product: str, z: int, x: int, y: int, date: str | None) -> tuple[bytes, bool]:
        """(PNG bytes, from GloFAS) for one tile; a blank tile when GloFAS has nothing or fails."""
        layer = glofas.PRODUCTS[product][0]
        now = time.time()
        when = glofas.time_for_date(date, now) or self.times.get(layer)
        key = (layer, when, z, x, y)
        with self._lock:
            hit = self._tiles.get(key)
            if hit and now - hit[0] < TILE_TTL:
                self._tiles.move_to_end(key)
                self.served += 1
                return hit[1], True
        try:
            body = self.net.get_bytes(glofas.WMS_URL, params=glofas.getmap_params(layer, z, x, y, when),
                                      timeout=10, retries=0, allow_redirects=False, max_bytes=4_000_000)
        except NetError as err:
            self._error(f"GloFAS tile failed: {str(err)[:120]}")
            return glofas.BLANK, False
        if not glofas.is_png(body):
            self._error("GloFAS answered without an image (" + body[:80].decode("utf-8", "replace").strip() + ")")
            return glofas.BLANK, False
        with self._lock:
            old = self._tiles.pop(key, None)
            if old:
                self._bytes -= len(old[1])
            self._tiles[key] = (now, body)
            self._bytes += len(body)
            while self._tiles and (len(self._tiles) > MAX_TILES or self._bytes > MAX_BYTES):
                _, (_, b) = self._tiles.popitem(last=False)
                self._bytes -= len(b)
            self.fetched += 1
            self.served += 1
        return body, True

    def _error(self, msg: str) -> None:
        now = time.time()
        with self._lock:
            self.errors = [e for e in self.errors if now - e[0] < 3600][-50:] + [(now, msg)]
        self.log.info("%s", msg)
