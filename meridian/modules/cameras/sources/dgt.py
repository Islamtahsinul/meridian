"""DGT (Spain) road cameras from the national access point's DATEX II device publication."""

from __future__ import annotations

from ..common import FetchContext, Source, clean, make_camera, parse_xml, read_bytes, strip_ns, title_case

URL = "https://nap.dgt.es/datex2/v3/dgt/DevicePublication/camaras_datex2_v36.xml"


def _text(el, path: str) -> str:
    found = el.find(path)
    return clean(found.text) if found is not None and found.text else ""


def parse(data: bytes, src: Source) -> tuple[list[dict], int]:
    root = strip_ns(parse_xml(data))
    rows: list[dict] = []
    skipped = 0
    for dev in root.iter("device"):
        if _text(dev, "typeOfDevice") not in ("", "camera"):
            continue
        native = dev.get("id") or ""
        road = _text(dev, ".//roadName")
        dest = title_case(_text(dev, ".//roadDestination"))
        km = _text(dev, ".//kilometerPoint")
        province = title_case(_text(dev, ".//province"))
        lat = _text(dev, ".//pointCoordinates/latitude")
        lon = _text(dev, ".//pointCoordinates/longitude")
        name = road or f"Camera {native}"
        if km:
            name = f"{name} km {km}"
        if dest:
            name = f"{name}, towards {dest}"
        row = make_camera(src, native, name, lat, lon, image=_text(dev, "deviceUrl"), road=road, region=province)
        if row:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def fetch(fc: FetchContext) -> list[dict]:
    data = read_bytes(fc.download(URL, "camaras_datex2_v36.xml", min_bytes=10_000, timeout=120))
    rows, skipped = parse(data, fc.src)
    fc.skip(skipped)
    return rows


SOURCE = Source(
    id="dgt", name="DGT road cameras", owner="Dirección General de Tráfico (Spain)", url="https://nap.dgt.es/",
    licence="DGT open data: free reuse with attribution (Spanish public sector information law)",
    licence_url="https://nap.dgt.es/", attribution="DGT, Dirección General de Tráfico", fetch=fetch,
    hosts=("etraffic.dgt.es", "infocar.dgt.es"), country="ES", refresh=180, tags=("traffic",),
    coverage="Spanish state road network (outside Catalonia and the Basque Country)",
)
