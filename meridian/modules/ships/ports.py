"""Ports for the "busiest ports" list: the infra module's ``data/build/infra/ports.json`` when it
exists (CONTRACT 9.1), otherwise a small built-in list of major ports (public coordinates)."""

from __future__ import annotations

import json
import re
from pathlib import Path

# name, ISO country, lat, lon, size (L large, M medium)
_BUILTIN = """
Helsinki|FI|60.160|24.955|M
Helsinki Vuosaari|FI|60.205|25.190|M
HaminaKotka (Mussalo)|FI|60.440|26.920|M
Turku|FI|60.435|22.220|M
Naantali|FI|60.465|22.020|M
Rauma|FI|61.130|21.460|M
Pori Mäntyluoto|FI|61.595|21.470|M
Vaasa|FI|63.085|21.570|M
Oulu|FI|65.010|25.410|M
Kemi Ajos|FI|65.670|24.520|M
Hanko|FI|59.820|22.970|M
Kokkola|FI|63.860|23.040|M
Mariehamn|AX|60.090|19.930|M
Tallinn Old City Harbour|EE|59.445|24.765|M
Muuga|EE|59.500|24.960|M
Paldiski|EE|59.350|24.050|M
Sillamäe|EE|59.420|27.730|M
Primorsk|RU|60.340|28.720|M
Vysotsk|RU|60.620|28.570|M
Ust-Luga|RU|59.680|28.400|L
Saint Petersburg|RU|59.880|30.210|L
Stockholm|SE|59.330|18.100|M
Kapellskär|SE|59.720|19.070|M
Gävle|SE|60.680|17.210|M
Umeå|SE|63.700|20.340|M
Luleå|SE|65.580|22.160|M
Ventspils|LV|57.400|21.550|M
Riga|LV|57.020|24.080|M
Gdańsk|PL|54.400|18.680|L
Klaipėda|LT|55.700|21.120|M
Gothenburg|SE|57.690|11.860|L
Copenhagen|DK|55.700|12.610|M
Oslo|NO|59.900|10.730|M
Bergen|NO|60.400|5.310|M
Hamburg|DE|53.540|9.970|L
Bremerhaven|DE|53.570|8.550|L
Rotterdam|NL|51.950|4.050|L
Antwerp-Bruges|BE|51.270|4.330|L
Felixstowe|GB|51.950|1.320|L
London Gateway|GB|51.500|0.470|L
Southampton|GB|50.900|-1.420|L
Dover|GB|51.120|1.330|M
Calais|FR|50.970|1.870|M
Le Havre|FR|49.470|0.150|L
Algeciras|ES|36.130|-5.430|L
Gibraltar|GI|36.140|-5.360|M
Tanger Med|MA|35.880|-5.500|L
Valencia|ES|39.440|-0.320|L
Barcelona|ES|41.350|2.160|L
Sines|PT|37.950|-8.870|M
Marseille-Fos|FR|43.400|4.870|L
Genoa|IT|44.400|8.920|L
Gioia Tauro|IT|38.450|15.900|L
Piraeus|GR|37.940|23.620|L
Istanbul Ambarlı|TR|40.970|28.680|L
Constanța|RO|44.170|28.660|L
Odesa|UA|46.500|30.750|M
Novorossiysk|RU|44.720|37.790|L
Port Said|EG|31.260|32.300|L
Suez|EG|29.940|32.550|M
Jeddah|SA|21.470|39.160|L
Djibouti|DJ|11.600|43.130|M
Salalah|OM|16.950|54.000|L
Jebel Ali|AE|25.010|55.060|L
Fujairah|AE|25.170|56.370|L
Bandar Abbas|IR|27.140|56.210|L
Ras Tanura|SA|26.650|50.160|L
Karachi|PK|24.830|66.970|L
Mundra|IN|22.740|69.700|L
Nhava Sheva|IN|18.950|72.950|L
Colombo|LK|6.950|79.840|L
Chittagong|BD|22.300|91.800|L
Port Klang|MY|2.990|101.380|L
Tanjung Pelepas|MY|1.360|103.550|L
Singapore|SG|1.260|103.840|L
Laem Chabang|TH|13.080|100.880|L
Cai Mep|VN|10.560|107.030|L
Jakarta Tanjung Priok|ID|-6.100|106.880|L
Manila|PH|14.590|120.960|L
Hong Kong|HK|22.310|114.130|L
Shenzhen|CN|22.500|113.880|L
Guangzhou Nansha|CN|22.700|113.650|L
Xiamen|CN|24.470|118.070|L
Kaohsiung|TW|22.610|120.280|L
Shanghai Yangshan|CN|30.630|122.070|L
Ningbo-Zhoushan|CN|29.930|121.850|L
Qingdao|CN|36.070|120.310|L
Tianjin|CN|38.980|117.780|L
Busan|KR|35.100|129.040|L
Tokyo|JP|35.620|139.780|L
Yokohama|JP|35.450|139.660|L
Nagoya|JP|35.050|136.870|L
Kobe|JP|34.670|135.200|L
Sydney Botany|AU|-33.970|151.210|L
Melbourne|AU|-37.830|144.920|L
Durban|ZA|-29.870|31.030|L
Mombasa|KE|-4.060|39.660|M
Lagos Apapa|NG|6.440|3.380|L
Santos|BR|-23.970|-46.300|L
Buenos Aires|AR|-34.590|-58.370|L
Callao|PE|-12.050|-77.150|L
Cartagena|CO|10.400|-75.530|L
Colón|PA|9.350|-79.900|L
Balboa|PA|8.950|-79.570|L
Houston|US|29.730|-95.020|L
Savannah|US|32.130|-81.140|L
New York and New Jersey|US|40.670|-74.130|L
Los Angeles|US|33.740|-118.260|L
Long Beach|US|33.750|-118.210|L
Vancouver|CA|49.290|-123.100|L
"""


def _slug(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-").replace("--", "-")


def builtin_ports() -> list[dict]:
    out = []
    for line in _BUILTIN.strip().splitlines():
        name, country, lat, lon, size = line.split("|")
        out.append({"id": f"ships:{_slug(name)}", "name": name, "country": country, "lat": float(lat),
                    "lon": float(lon), "size": "large" if size == "L" else "medium", "source": "builtin"})
    return out


def _size(value) -> str:
    s = str(value or "").strip().lower()
    if s.startswith("l"):
        return "large"
    if s.startswith("m"):
        return "medium"
    if s.startswith(("s", "v")):
        return "small"
    return "unknown"


SIZE_RANK = {"large": 3, "medium": 2, "small": 1, "unknown": 0}
_BROKEN = re.compile("\s*\([^)]*�[^)]*\)")


def clean_name(value) -> str:
    """A port name for sentences: alternative names spoiled by a broken text encoding upstream ("Porvoo
    (Borg�)") are dropped and stray replacement characters removed."""
    name = _BROKEN.sub("", str(value or "")).replace("�", "").strip()
    return (name or "Unnamed port")[:80]


def ports_path(data_dir: Path) -> Path:
    return Path(data_dir) / "build" / "infra" / "ports.json"


def load_ports(data_dir: Path) -> tuple[list[dict], str]:
    """(ports, source) where source is "infra" or "builtin". Invalid rows are skipped."""
    path = ports_path(data_dir)
    try:
        if path.is_file() and path.stat().st_size < 50_000_000:
            rows = json.loads(path.read_text(encoding="utf-8"))
            ports = []
            for r in rows if isinstance(rows, list) else []:
                try:
                    lat, lon = float(r["lat"]), float(r["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                ports.append({"id": str(r.get("id") or f"{lat:.3f},{lon:.3f}")[:60],
                              "name": clean_name(r.get("name")),
                              "country": str(r.get("country") or "")[:3].upper(), "lat": lat, "lon": lon,
                              "size": _size(r.get("size")), "source": "infra"})
            if ports:
                return ports, "infra"
    except (OSError, ValueError):
        pass
    return builtin_ports(), "builtin"


def port_radius_km(size: str) -> float:
    return {"large": 12.0, "medium": 8.0, "small": 5.0}.get(size, 4.0)
