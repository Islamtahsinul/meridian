"""Places the maritime anomaly detectors need to know about (docs/modules/ships.md, "Anomalies").

Two lists, both plain circles (centre and radius) so lookups stay cheap:

- ``USUAL_AREAS``: anchorages, waiting areas and recognised ship-to-ship (STS) transfer or bunkering
  areas where slow ships, ships lying close together and drifting ships are routine. The detectors
  never flag a ship-to-ship candidate or a loitering vessel inside them. The Gulf of Finland entries
  were drawn from Meridian's own AIS (November 2026: dozens of tankers and bulk carriers at anchor or
  drifting with Ust-Luga, Primorsk, Vysotsk or St Petersburg as destination); the others are widely
  published bunkering and transfer anchorages. Radii are deliberately generous. Meridian also learns
  usual anchorages and waiting areas from what it sees (anomalies.py), so this list only has to cover
  the first two weeks and the places the research showed would otherwise flag every day.
- ``AREAS_OF_INTEREST``: waters named in public government advisories or reporting about ship-to-ship
  transfers, AIS manipulation or sanctions-related shipping. Being in one never makes a vessel
  suspect; it adds context (and +1 severity) to a candidate that was flagged for other reasons, and
  AIS gap episodes are only published when they touch one of these or a monitored strait. Every area
  names its public source in ``basis``.

Coordinates are WGS84 decimal degrees.
"""

from __future__ import annotations

import math

from meridian import geo

# kind: anchorage (ships at anchor), waiting (ships drifting or anchoring while they wait for a berth or
# orders), transfer (a recognised STS transfer or bunkering area).
USUAL_AREAS: list[dict] = [
    {"id": "gof_ust_luga_roads", "phrase": "the Ust-Luga outer anchorage", "name": "Ust-Luga outer anchorage", "kind": "anchorage",
     "lat": 59.80, "lon": 28.42, "radius_km": 8.0,
     "basis": "Observed in Meridian's AIS: tankers and bulk carriers at anchor bound for Ust-Luga."},
    {"id": "gof_east_waiting", "phrase": "the waiting area between Ust-Luga and Primorsk", "name": "Waiting area between Ust-Luga and Primorsk", "kind": "waiting",
     "lat": 59.95, "lon": 28.60, "radius_km": 16.0,
     "basis": "Observed in Meridian's AIS: tankers anchored or drifting for hours while they wait for berths "
              "at Ust-Luga and Primorsk (the research found these must not flag)."},
    {"id": "gof_primorsk_roads", "phrase": "the Primorsk anchorage", "name": "Primorsk anchorage", "kind": "anchorage",
     "lat": 60.33, "lon": 28.68, "radius_km": 7.0,
     "basis": "Observed in Meridian's AIS: tankers at anchor off Primorsk."},
    {"id": "gof_vysotsk_roads", "phrase": "the Vysotsk anchorage", "name": "Vysotsk anchorage", "kind": "anchorage",
     "lat": 60.33, "lon": 28.03, "radius_km": 7.0,
     "basis": "Observed in Meridian's AIS: tankers at anchor bound for Vysotsk."},
    {"id": "gof_kronstadt_roads", "phrase": "the St Petersburg outer anchorage off Kronstadt", "name": "St Petersburg outer anchorage off Kronstadt", "kind": "anchorage",
     "lat": 60.07, "lon": 29.35, "radius_km": 10.0,
     "basis": "Observed in Meridian's AIS: ships at anchor waiting for St Petersburg."},
    {"id": "sg_east_anchorages", "phrase": "Singapore's eastern anchorages", "name": "Singapore eastern anchorages", "kind": "anchorage",
     "lat": 1.28, "lon": 103.96, "radius_km": 9.0,
     "basis": "Observed in Meridian's AIS: dozens of tankers and bunker barges at anchor, many alongside."},
    {"id": "sg_eopl", "phrase": "the Eastern Outer Port Limits off Singapore and Johor", "name": "Eastern Outer Port Limits, off Singapore and Johor", "kind": "waiting",
     "lat": 1.33, "lon": 104.40, "radius_km": 25.0,
     "basis": "One of the world's largest waiting, bunkering and transfer anchorages (published port "
              "information for Singapore and Johor)."},
    {"id": "ae_fujairah", "phrase": "the Fujairah and Khor Fakkan anchorages", "name": "Fujairah and Khor Fakkan anchorages", "kind": "anchorage",
     "lat": 25.22, "lon": 56.48, "radius_km": 22.0,
     "basis": "A major bunkering and transfer anchorage (published port information for Fujairah)."},
    {"id": "mt_hurds_bank", "phrase": "Hurd's Bank off Malta", "name": "Hurd's Bank, off Malta", "kind": "transfer",
     "lat": 35.85, "lon": 14.67, "radius_km": 10.0,
     "basis": "A designated bunkering and ship-to-ship transfer area (Transport Malta)."},
    {"id": "gr_laconian", "phrase": "the Laconian Gulf transfer anchorage", "name": "Laconian Gulf transfer anchorage", "kind": "transfer",
     "lat": 36.55, "lon": 22.75, "radius_km": 20.0,
     "basis": "A long-used ship-to-ship transfer anchorage in southern Greece."},
    {"id": "tg_lome", "phrase": "the Lomé offshore anchorage", "name": "Lomé offshore anchorage", "kind": "transfer",
     "lat": 6.00, "lon": 1.30, "radius_km": 25.0,
     "basis": "A long-used offshore ship-to-ship transfer and waiting anchorage off Togo."},
    {"id": "gb_southwold", "phrase": "the Southwold transfer area", "name": "Southwold transfer area", "kind": "transfer",
     "lat": 52.30, "lon": 1.85, "radius_km": 10.0,
     "basis": "A long-used ship-to-ship transfer anchorage off the Suffolk coast."},
    {"id": "dk_skagen", "phrase": "the Skagen anchorage", "name": "Skagen anchorage", "kind": "anchorage",
     "lat": 57.75, "lon": 10.75, "radius_km": 15.0,
     "basis": "A long-used bunkering and waiting anchorage off northern Denmark."},
    {"id": "za_algoa", "phrase": "the Algoa Bay bunkering area", "name": "Algoa Bay offshore bunkering area", "kind": "transfer",
     "lat": -33.85, "lon": 25.75, "radius_km": 20.0,
     "basis": "An offshore bunkering and ship-to-ship transfer area off Gqeberha (Port Elizabeth)."},
]

AREAS_OF_INTEREST: list[dict] = [
    {"id": "gulf_of_finland_east", "name": "the approaches to Primorsk and Ust-Luga", "lat": 60.00, "lon": 28.20,
     "radius_km": 70.0,
     "basis": "Russia's main Baltic crude and product export terminals. UK and EU shadow-fleet sanctions "
              "designations (2024 and 2025) name many tankers that load here, and Finnish and Estonian "
              "authorities have said publicly that they monitor such tankers in the Gulf of Finland."},
    {"id": "kerch", "name": "the Kerch Strait and the approaches to the Sea of Azov", "lat": 45.20, "lon": 36.60,
     "radius_km": 50.0,
     "basis": "US Maritime Administration (MARAD) maritime security advisories for the Black Sea and Sea of "
              "Azov (2022 onwards) and reporting on ship-to-ship transfers near Kavkaz."},
    {"id": "laconian", "name": "the Laconian Gulf", "lat": 36.60, "lon": 22.75, "radius_km": 30.0,
     "basis": "2022 and 2023 reporting (Reuters, Lloyd's List) on ship-to-ship transfers of Russian oil in "
              "the gulf, and Greek naval exercise notices there in 2023."},
    {"id": "johor_east", "name": "the waters east of Johor", "lat": 1.35, "lon": 104.45, "radius_km": 40.0,
     "basis": "2023 and 2024 reporting (Reuters, Bloomberg) and Malaysian Marine Department statements (2023) "
              "on unauthorised ship-to-ship transfers off Johor."},
    {"id": "gulf_of_oman_fujairah", "name": "the Gulf of Oman off Fujairah and Khor Fakkan", "lat": 25.30,
     "lon": 56.50, "radius_km": 45.0,
     "basis": "OFAC advisory to the maritime petroleum shipping community on Iranian oil (4 September 2019) "
              "and the OFAC, State Department and US Coast Guard 'Sanctions Advisory for the Maritime "
              "Industry, Energy and Metals Sectors, and Related Communities' (14 May 2020), which describe "
              "ship-to-ship transfers and AIS manipulation; reporting on transfers off Fujairah."},
    {"id": "kharg", "name": "the waters off Kharg Island", "lat": 29.25, "lon": 50.30, "radius_km": 40.0,
     "basis": "Iran's main crude export terminal, the subject of the same OFAC advisories on Iranian "
              "petroleum shipments (2019 and 2020)."},
    {"id": "baniyas", "name": "the Syrian coast off Baniyas", "lat": 35.25, "lon": 35.90, "radius_km": 40.0,
     "basis": "OFAC advisory 'Sanctions Risks Related to Petroleum Shipments involving Iran and Syria' "
              "(20 November 2018, updated 25 March 2019)."},
    {"id": "jose", "name": "the Venezuelan coast off Jose", "lat": 10.15, "lon": -64.80, "radius_km": 40.0,
     "basis": "US Treasury (OFAC) Venezuela-related designations of tankers carrying oil from PDVSA's Jose "
              "terminal (2019 and 2020) and reporting on transfers of Venezuelan oil."},
    {"id": "nampo", "name": "the Yellow Sea west of Nampo", "lat": 38.60, "lon": 124.80, "radius_km": 80.0,
     "basis": "UN Security Council Panel of Experts (resolution 1874) reports, 2018 onwards, and the US "
              "'North Korea Sanctions Advisory: Updated Guidance on Addressing North Korea's Illicit Shipping "
              "Practices' (21 March 2019), which document ship-to-ship transfers for North Korea."},
]


def _dist_ok(lat: float, lon: float, area: dict, extra_km: float = 0.0) -> float | None:
    radius = area["radius_km"] + extra_km
    if abs(lat - area["lat"]) * 111.2 > radius:
        return None
    d = geo.haversine_km(lat, lon, area["lat"], area["lon"])
    return d if d <= radius else None


def usual_area(lat: float, lon: float) -> dict | None:
    """The built-in anchorage, waiting or transfer area containing the point, if any (``phrase`` is the
    form used inside sentences)."""
    best = None
    for a in USUAL_AREAS:
        d = _dist_ok(lat, lon, a)
        if d is not None and (best is None or d < best[0]):
            best = (d, a)
    return best[1] if best else None


def curated_aoi(lat: float, lon: float, extra_km: float = 0.0) -> dict | None:
    for a in AREAS_OF_INTEREST:
        if _dist_ok(lat, lon, a, extra_km) is not None:
            return a
    return None


class AreasOfInterest:
    """The monitored straits plus the curated areas, as one lookup."""

    STRAIT_MARGIN_DEG = 0.15        # "near a chokepoint": inside the outline or just outside its bounding box

    def __init__(self, strait_defs: list[dict]):
        self.straits = strait_defs

    def at(self, lat: float, lon: float) -> dict | None:
        """{kind: "strait"|"area", id, name, basis?} for the first area containing the point."""
        for d in self.straits:
            w, s, e, n = d["bbox"]
            m = self.STRAIT_MARGIN_DEG
            if s - m <= lat <= n + m and w - m <= lon <= e + m:
                inside = geo.point_in_geometry(lon, lat, d["geometry"])
                return {"kind": "strait", "id": d["id"], "name": d["name"], "inside": inside,
                        "label": "inside" if inside else "next to"}
        a = curated_aoi(lat, lon)
        if a is not None:
            return {"kind": "area", "id": a["id"], "name": a["name"], "basis": a["basis"], "inside": True,
                    "label": "in"}
        return None

    def on_path(self, lat0: float, lon0: float, lat1: float, lon1: float, step_km: float = 5.0) -> dict | None:
        """The first area touched by the start, the end or the straight line between them."""
        hit = self.at(lat0, lon0) or self.at(lat1, lon1)
        if hit is not None:
            return hit
        dist = geo.haversine_km(lat0, lon0, lat1, lon1)
        if dist <= step_km:
            return None
        n = min(60, int(dist / step_km))
        brg = geo.bearing_deg(lat0, lon0, lat1, lon1)
        for i in range(1, n):
            lat, lon = geo.destination(lat0, lon0, brg, dist * i / n)
            hit = self.at(lat, lon)
            if hit is not None:
                return dict(hit, label="on the way through")
        return None


def aoi_phrase(aoi: dict | None) -> str:
    """"near the Strait of Hormuz" / "in the Laconian Gulf"."""
    if not aoi:
        return ""
    name = aoi["name"]
    if aoi["kind"] == "strait":
        return f"{'in' if aoi.get('inside') else 'near'} {_the(name)}"
    return f"in {name}"


def _the(name: str) -> str:
    lower = name.lower()
    if lower.startswith(("strait of", "gulf of", "bab-el", "bosphorus", "dardanelles")):
        return "the " + name
    if lower.endswith(("canal", "strait", "sea")):
        return "the " + name
    return name


# ---------------------------------------------------------------------------------------------
# Grid cells (integer keys) for the learned anchorages and waiting areas
# ---------------------------------------------------------------------------------------------
def cell_key(lat: float, lon: float, deg: float) -> int:
    rows = int(round(180.0 / deg))
    cols = int(round(360.0 / deg))
    r = min(max(int(math.floor((lat + 90.0) / deg)), 0), rows - 1)
    c = int(math.floor((lon + 180.0) / deg)) % cols
    return r * cols + c


def cell_neighbours(key: int, deg: float) -> list[int]:
    """The cell and its eight neighbours (wrapping in longitude)."""
    cols = int(round(360.0 / deg))
    rows = int(round(180.0 / deg))
    r, c = divmod(key, cols)
    out = []
    for dr in (-1, 0, 1):
        rr = r + dr
        if not 0 <= rr < rows:
            continue
        for dc in (-1, 0, 1):
            out.append(rr * cols + (c + dc) % cols)
    return out


def cell_center(key: int, deg: float) -> tuple[float, float]:
    cols = int(round(360.0 / deg))
    r, c = divmod(key, cols)
    return (-90.0 + (r + 0.5) * deg, -180.0 + (c + 0.5) * deg)
