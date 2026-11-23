"""EUMETSAT EUMETView WMS: Meteosat Third Generation (MTG-I, 0 degree full disc) imagery and lightning.

The browser loads GetMap tiles straight from view.eumetsat.int (the service sends
``Access-Control-Allow-Origin: *`` and caches a fixed time for a week). The server only reads the
small GetCapabilities document (about 6 KB) for the latest frame of each layer, cached 10 minutes.

Licence (EUMETSAT Data Policy, last amended 25 November 2025, read on 23 November 2026):
- Article 4 lists "All SEVIRI, FCI, IRS and LI Advanced Image Products" among the "Core" Meteosat
  data, and Annex II defines an Advanced Image Product as "the combination of different channels
  allocating a colour to each channel (i.e. RGB), visualisations of individual channels (e.g. used in
  Web Map Services)".
- Article 5.1: "Access to Core Data and Products is granted to all users world-wide on a Free and
  Unrestricted basis under a CC-BY-4.0 licence, regardless of when and how these are made available".
- Article 6.3 asks for the credit "[Contains modified] EUMETSAT [Meteosat/Metop] [data/product]
  [Year of publication or distribution]".
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

WMS_URL = "https://view.eumetsat.int/geoserver/mtg_fd/wms"
CAPS_URL = "https://view.eumetsat.int/geoserver/mtg_fd/ows"
CAPS_PARAMS = {"service": "WMS", "request": "GetCapabilities", "version": "1.3.0"}
WORKSPACE = "mtg_fd"
PREFIX = "eumetsat:"                 # layer ids in imagery.PRODUCTS that come from this service
CAPS_TTL = 600.0
STALE_OK = 6 * 3600.0                # serve the last good capabilities this long while the service is down
RETRY_AFTER_ERROR = 120.0
POLICY_URL = "https://www.eumetsat.int/legal-framework/data-policy"
VIEWER_URL = "https://view.eumetsat.int/"

_NS = "{http://www.opengis.net/wms}"


def credit(year: int | None = None) -> str:
    y = year or datetime.now(timezone.utc).year
    return f"EUMETSAT Meteosat product {y} (CC BY 4.0)"


def parse_capabilities(body: bytes) -> dict[str, dict]:
    """GetCapabilities (WMS 1.3.0) -> {layer name: {title, time, default, bbox}}; layers without a
    time dimension are skipped (every MTG layer has one)."""
    root = ET.fromstring(body)
    cap = root.find(_NS + "Capability")
    if cap is None:
        raise ValueError("no Capability element")
    out: dict[str, dict] = {}
    for layer in cap.iter(_NS + "Layer"):
        name_el = layer.find(_NS + "Name")
        if name_el is None or not (name_el.text or "").strip():
            continue
        dim = next((d for d in layer.findall(_NS + "Dimension") if (d.get("name") or "").lower() == "time"), None)
        if dim is None or not (dim.text or "").strip():
            continue
        title_el = layer.find(_NS + "Title")
        bbox = None
        ex = layer.find(_NS + "EX_GeographicBoundingBox")
        if ex is not None:
            try:
                bbox = [float(ex.find(_NS + t).text) for t in ("westBoundLongitude", "southBoundLatitude",
                                                                 "eastBoundLongitude", "northBoundLatitude")]
            except (AttributeError, TypeError, ValueError):
                bbox = None
        name = name_el.text.strip().split(":")[-1]
        out[name] = {"title": (title_el.text or "").strip() if title_el is not None else name,
                     "time": " ".join(dim.text.split()), "default": (dim.get("default") or "").strip() or None,
                     "bbox": bbox}
    if not out:
        raise ValueError("no time-enabled layers in the capabilities")
    return out


def wms_spec(name: str, style: str = "", fmt: str = "image/png") -> dict:
    """What the browser needs to build a GetMap template for one layer."""
    return {"url": WMS_URL, "layers": f"{WORKSPACE}:{name}", "styles": style, "format": fmt}


class Meteosat:
    """Cached EUMETView capabilities; never raises to callers of ``layers_or_none``."""

    def __init__(self, net, log, status=None):
        self.net = net
        self.log = log
        self._status = status or (lambda **kw: None)
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()   # one capabilities request at a time (the catalogue asks in parallel)
        self._caps: dict[str, dict] | None = None
        self._at = 0.0
        self._failed_at = 0.0
        self.error: str | None = None

    def layers(self) -> dict[str, dict]:
        now = time.time()
        with self._lock:
            caps, at = self._caps, self._at
        if caps is not None and now - at < CAPS_TTL:
            return caps
        with self._fetch_lock:
            return self._refresh()

    def _refresh(self) -> dict[str, dict]:
        now = time.time()
        with self._lock:
            caps, at, failed = self._caps, self._at, self._failed_at
        if caps is not None and now - at < CAPS_TTL:      # another thread fetched while this one waited
            return caps
        if now - failed < RETRY_AFTER_ERROR:
            if caps is not None and now - at < STALE_OK:
                return caps
            raise RuntimeError(self.error or "EUMETView is not answering")
        try:
            body = self.net.get_bytes(CAPS_URL, params=CAPS_PARAMS, timeout=12, retries=1, max_bytes=2_000_000)
            parsed = parse_capabilities(body)
        except Exception as err:   # network, XML or empty document
            msg = f"EUMETView capabilities unavailable: {err}"
            with self._lock:
                self._failed_at = now
                self.error = msg
            self.log.info(msg)
            self._status(ok=False, error=msg)
            if caps is not None and now - at < STALE_OK:
                return caps
            raise RuntimeError(msg) from None
        with self._lock:
            self._caps, self._at, self._failed_at, self.error = parsed, now, 0.0, None
        self._status(ok=True, count=len(parsed), note=f"{len(parsed)} Meteosat layers")
        return parsed

    def layers_or_none(self) -> dict[str, dict] | None:
        try:
            return self.layers()
        except Exception:
            return None
