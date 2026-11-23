"""GloFAS (Copernicus EMS Global Flood Awareness System) flood outlook: WMS products, tile maths
and the capabilities parser. Pure functions; the tile proxy is in ``outlook.py``.

Terms (CEMS-FLOODS datasets licence, read 23 Nov 2026): free reproduction, distribution and
communication to the public, with the notice "Generated using Copernicus Emergency Management
Service information <year>". The licence also says the data "does not constitute in any way a
flood warning", which the layer text repeats.
"""

from __future__ import annotations

import math
import re
import struct
import zlib
from datetime import datetime, timezone

WMS_URL = "https://ows.globalfloods.eu/glofas-ows/ows.py"
SITE_URL = "https://global-flood.emergency.copernicus.eu/"
LICENCE_URL = ("https://object-store.os-api.cci2.ecmwf.int/cci2-prod-catalogue/licences/cems-floods/"
               "cems-floods_428a6e1019ec50b3dad9c37a90d630fab139059933a939dd5df620bfcb420cc3.pdf")
HOST = "ows.globalfloods.eu"

# product id (in Meridian URLs) -> (WMS layer, title)
PRODUCTS = {
    "days1-3": ("sumAL41EGE", "Flood summary, days 1 to 3"),
    "days4-10": ("sumAL42EGE", "Flood summary, days 4 to 10"),
    "days11-15": ("sumAL43EGE", "Flood summary, days 11 to 15"),
    "points": ("reportingPoints", "Reporting points"),
}
MAX_ZOOM = 10
LEGEND = [
    {"color": "#a800d6", "label": "Above a 20-year flood"},
    {"color": "#e60000", "label": "5 to 20-year flood"},
    {"color": "#ffff00", "label": "2 to 5-year flood"},
    {"color": "#f3f0a6", "label": "Possible 2-year flood"},
]
NOTE = ("A forecast signal, not a flood warning: official warnings come from national and regional services. "
        "Darker shades mean the flood is more likely (expected, probable, possible).")

_EARTH = 6378137.0
_ORIGIN = math.pi * _EARTH
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def valid_tile(z: int, x: int, y: int) -> bool:
    return 0 <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z


def tile_bbox_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(minx, miny, maxx, maxy) in EPSG:3857 metres of an XYZ (slippy map) tile."""
    size = 2 * _ORIGIN / (2 ** z)
    return (-_ORIGIN + x * size, _ORIGIN - (y + 1) * size, -_ORIGIN + (x + 1) * size, _ORIGIN - y * size)


def getmap_params(layer: str, z: int, x: int, y: int, time_value: str | None, size: int = 256) -> dict:
    bbox = ",".join(f"{v:.3f}" for v in tile_bbox_3857(z, x, y))
    params = {"service": "WMS", "request": "GetMap", "version": "1.3.0", "layers": layer, "styles": "",
              "crs": "EPSG:3857", "bbox": bbox, "width": str(size), "height": str(size), "format": "image/png",
              "transparent": "true"}
    if time_value:
        params["time"] = time_value
    return params


def parse_times(xml_text: str) -> dict[str, str]:
    """{WMS layer name: default time} for the products Meridian uses, read from GetCapabilities."""
    wanted = {layer for layer, _ in PRODUCTS.values()}
    out: dict[str, str] = {}
    if not xml_text:
        return out
    for m in re.finditer(r"<Name>([A-Za-z0-9_]+)</Name>", xml_text):
        name = m.group(1)
        if name not in wanted or name in out:
            continue
        end = xml_text.find("</Layer>", m.end())
        chunk = xml_text[m.end(): end if end > 0 else m.end() + 6000]
        d = re.search(r'<Dimension[^>]*name="time"[^>]*default="([^"]+)"', chunk)
        if d and _DATE.match(d.group(1)):
            out[name] = d.group(1)
    return out


def date_of(time_value: str | None) -> str | None:
    m = _DATE.match(time_value or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def time_for_date(date_text: str | None, now: float) -> str | None:
    """A forecast date the browser asked for ("2026-11-23"), checked: within the past 30 days and not
    in the future. Returns the WMS time value ("2026-11-23T00:00Z") or None."""
    m = _DATE.fullmatch(date_text or "")
    if not m:
        return None
    try:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None
    t = d.timestamp()
    if t > now + 86400 or t < now - 31 * 86400:
        return None
    return d.strftime("%Y-%m-%dT00:00Z")


def is_png(data: bytes | None) -> bool:
    return bool(data) and data[:8] == b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def blank_png(size: int = 256) -> bytes:
    """A fully transparent PNG (served when GloFAS has no tile, so the map shows nothing there)."""
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * size for _ in range(size))
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


BLANK = blank_png()
