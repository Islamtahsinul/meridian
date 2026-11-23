"""Aircraft classification: class (colour), silhouette (icon) and icon size.

Inputs, in order of trust: the database military flag, the ADS-B emitter category (A1-A7, B1-B7,
C1-C5), the ICAO type designator and its description, and callsign patterns. Pure functions only.
"""

from __future__ import annotations

import re

from .const import DB_MILITARY

# Military operators' ICAO telephony designators (callsign = prefix + digits).
_MIL_CALLSIGN = re.compile(
    r"^(?:RCH|RRR|CFC|GAF|BAF|IAM|FAF|CTM|ASY|NAF|SVF|PLF|HAF|MMF|NATO|CNV|PAT|SPAR|EVAC|AME|CEF|HUF|DAF|NOW)\d")

# Types that are military by design (civil operators are vanishingly rare).
MIL_TYPES = frozenset({
    "A400", "C17", "C5", "C5M", "K35R", "K35E", "KC10", "KC46", "E3TF", "E3CF", "E6", "E8", "P8", "R135",
    "B52", "B1", "B2", "U2", "F15", "F16", "F18", "F22", "F35", "FA18", "EUFI", "TORN", "RFAL", "HAWK",
    "T38", "C160", "H64", "V22", "MQ9", "RQ4", "A10", "SU27", "SU30", "SU35", "MG29", "MG31", "MIR2",
    "M346", "TIGR", "NH90", "EH10",
})

FIGHTER_TYPES = frozenset({
    "F15", "F16", "F18", "F22", "F35", "FA18", "EUFI", "TORN", "RFAL", "HAWK", "T38", "A10", "SU27", "SU30",
    "SU35", "MG29", "MG31", "MIR2", "M346", "L39", "F5", "JS39", "A4",
})

UAV_TYPES = frozenset({"MQ9", "RQ4", "MQ1", "HRON", "UAV", "Q9"})

HELI_TYPES = frozenset({
    "EC20", "EC25", "EC30", "EC35", "EC45", "EC55", "EC75", "AS32", "AS3B", "AS50", "AS55", "AS65", "A109",
    "A119", "A129", "A139", "A149", "A169", "A189", "B06", "B06T", "B105", "B212", "B222", "B230", "B407",
    "B412", "B427", "B429", "B430", "B47G", "B505", "BK17", "EH10", "H160", "H500", "H60", "H47", "H53",
    "H64", "MI8", "MI24", "MI26", "MI28", "KA27", "KA32", "KA52", "NH90", "R22", "R44", "R66", "S61", "S64",
    "S76", "S92", "EXPL", "G2CA", "GAZL", "PUMA", "LYNX", "ALO2", "ALO3", "UH1", "TIGR", "H269", "EN28",
    "EN48", "MD52", "MD60", "AS35", "AW09", "H125", "H135", "H145", "H175",
})

BIZJET_TYPES = frozenset({
    "C25A", "C25B", "C25C", "C25M", "C500", "C501", "C510", "C525", "C526", "C550", "C551", "C55B", "C560",
    "C56X", "C650", "C680", "C68A", "C700", "C750", "CL30", "CL35", "CL60", "GL5T", "GL6T", "GL7T", "GLEX",
    "GLF2", "GLF3", "GLF4", "GLF5", "GLF6", "GA5C", "GA6C", "GA7C", "GA8C", "G150", "G280", "GALX", "ASTR",
    "F2TH", "F900", "FA50", "FA6X", "FA7X", "FA8X", "FA10", "FA20", "E50P", "E55P", "E545", "E550", "E35L",
    "PRM1", "H25A", "H25B", "H25C", "HA4T", "LJ23", "LJ24", "LJ25", "LJ28", "LJ31", "LJ35", "LJ40", "LJ45",
    "LJ55", "LJ60", "LJ70", "LJ75", "BE40", "PC24", "EA50", "SF50", "HDJT", "WW24", "J328",
})

TURBOPROP_TYPES = frozenset({
    "AT43", "AT44", "AT45", "AT46", "AT72", "AT73", "AT75", "AT76", "DH8A", "DH8B", "DH8C", "DH8D", "SF34",
    "JS31", "JS32", "JS41", "E120", "SB20", "F50", "F27", "D328", "L410", "B190", "BE20", "BE30", "B350",
    "BE9L", "BE9T", "PC12", "C208", "C30J", "C130", "A400", "C160", "C295", "CN35", "AN24", "AN26", "AN30",
    "AN32", "AN12", "IL18", "Y12", "DHC6", "DHC7", "SH36", "SW4", "MU2", "P180", "TBM7", "TBM8", "TBM9",
    "PAY1", "PAY2", "PAY3", "PAY4", "AC90", "AC95", "P3", "E314", "PC21", "PC9", "T6", "K100", "M7",
    "AT8T", "AT6T", "C27J", "G222", "Y8", "Y9",
})

WIDEBODY_TYPES = frozenset({
    "A306", "A30B", "A310", "A332", "A333", "A337", "A338", "A339", "A342", "A343", "A345", "A346", "A359",
    "A35K", "A388", "B741", "B742", "B743", "B744", "B748", "B74S", "B74R", "B762", "B763", "B764", "B772",
    "B773", "B77L", "B77W", "B778", "B779", "B788", "B789", "B78X", "MD11", "DC10", "L101", "IL86", "IL96",
    "A124", "A225", "IL76", "C17", "C5", "C5M", "KC10", "KC46", "B52", "A3ST", "BLCF", "BSCA", "A337",
})

_AIRLINER_TYPE = re.compile(
    r"^(?:A3[0-9][0-9N]|A[12][0-9]N|A3[0-9]{2}|B7[0-9]{2}|B7[0-9][A-Z]|B3[789]M|B3XM|E1[79]0|E[12]9[05]|E75[LS]"
    r"|E170|E135|E145|E45X|CRJ[0-9X]|AT[4-7][0-9]|DH8[A-D]|BCS[13]|SU95|C919|AJ27|MD8[0-9]|MD90|B46[0-9]"
    r"|RJ[0-9]{2}|RJ1H|F70|F100|A148|AN48|T204|IL96|Y12|A21N|A20N|A19N)$")

_CARGO_CALLSIGN = frozenset({
    "FDX", "UPS", "GTI", "CLX", "BCS", "DHK", "DHX", "DHL", "DAE", "BOX", "GEC", "CKS", "ABW", "PAC", "ABX",
    "ATN", "NCA", "CAO", "CKK", "CSS", "CYZ", "YZR", "MPH", "SQC", "TAY", "SWN", "NPT", "ICV", "MSX", "ADB",
    "VDA", "WGN", "NAC", "KYE", "AJT", "LCO", "TPA", "GSS", "SRR", "AHK", "AZG",
})

_AIRLINE_CALLSIGN = re.compile(r"^([A-Z]{3})(\d{1,4}[A-Z]{0,2})$")
_REG_LIKE = re.compile(r"^(?:N\d{1,5}[A-Z]{0,2}|[A-Z]{1,2}[A-Z0-9]{3,5})$")
_HELI_WORDS = re.compile(r"HELICOPTER|ROTORCRAFT|\bHELI", re.I)
_GLIDER_WORDS = re.compile(r"GLIDER|SAILPLANE|MOTORGLIDER", re.I)
_BALLOON_WORDS = re.compile(r"BALLOON|AIRSHIP", re.I)
_BIZJET_WORDS = re.compile(r"CITATION|LEARJET|GULFSTREAM|FALCON|CHALLENGER|GLOBAL [0-9X]|PHENOM|LEGACY|PRAETOR"
                           r"|HAWKER|HONDAJET|PC-24|PREMIER|BEECHJET|NEXTANT", re.I)
_CARGO_WORDS = re.compile(r"\bCARGO\b|FREIGHT|FEDERAL EXPRESS", re.I)

GROUND_TYPES = frozenset({"GRND", "TWR", "SERV", "EMER", "VEH", "GND"})

CAT_SIZE = {"A1": 0.62, "A2": 0.76, "A3": 0.86, "A4": 0.9, "A5": 1.0, "A6": 0.8, "A7": 0.78,
            "B1": 0.74, "B2": 0.7, "B3": 0.55, "B4": 0.6, "B6": 0.62, "B7": 0.9,
            "C1": 0.5, "C2": 0.5, "C3": 0.5, "C4": 0.5, "C5": 0.5}
SHAPE_SIZE = {"heavy": 1.0, "jet": 0.86, "turboprop": 0.78, "bizjet": 0.72, "prop": 0.62, "heli": 0.78,
              "fighter": 0.8, "glider": 0.74, "balloon": 0.68, "uav": 0.62, "ground": 0.5, "generic": 0.74}


def airline_prefix(callsign: str | None) -> str | None:
    """'BAW123' -> 'BAW'. None for registrations and other non-airline callsigns."""
    if not callsign:
        return None
    m = _AIRLINE_CALLSIGN.match(callsign)
    return m.group(1) if m else None


def looks_military_callsign(callsign: str | None) -> bool:
    return bool(callsign and _MIL_CALLSIGN.match(callsign))


def classify(*, category: str | None = None, typ: str | None = None, desc: str | None = None,
             db_flags: int = 0, callsign: str | None = None, reg: str | None = None,
             ownop: str | None = None) -> tuple[str, str, float]:
    """(class id, shape, icon size factor)."""
    cat = (category or "").upper()
    t = (typ or "").upper()
    d = desc or ""
    cs = (callsign or "").upper()
    group = cat[:1]

    # 1. Ground vehicles, towers and obstacles
    if cat in ("C1", "C2", "C3", "C4", "C5") or t in GROUND_TYPES:
        return "ground", "ground", CAT_SIZE.get(cat, 0.5)

    is_heli = cat == "A7" or t in HELI_TYPES or bool(_HELI_WORDS.search(d))

    # 2. Military
    if (db_flags or 0) & DB_MILITARY or t in MIL_TYPES or looks_military_callsign(cs):
        if is_heli:
            shape = "heli"
        elif t in UAV_TYPES or cat == "B6":
            shape = "uav"
        elif cat == "A6" or t in FIGHTER_TYPES:
            shape = "fighter"
        elif t in TURBOPROP_TYPES:
            shape = "turboprop"
        elif cat == "A5" or t in WIDEBODY_TYPES:
            shape = "heavy"
        elif t in BIZJET_TYPES:
            shape = "bizjet"
        elif cat == "A1":
            shape = "prop"
        else:
            shape = "jet"
        return "military", shape, _size(cat, shape)

    # 3. Helicopters
    if is_heli:
        return "helicopter", "heli", _size(cat, "heli")

    # 4. Gliders, balloons, parachutists, ultralights, drones
    if group == "B" and cat not in ("B0", "B7"):
        shape = {"B1": "glider", "B2": "balloon", "B3": "balloon", "B4": "glider", "B6": "uav"}.get(cat, "glider")
        return "glider", shape, _size(cat, shape)
    if t in UAV_TYPES:
        return "glider", "uav", _size(cat, "uav")
    if _BALLOON_WORDS.search(d) or t == "BALL":
        return "glider", "balloon", _size(cat, "balloon")
    if _GLIDER_WORDS.search(d) or t == "GLID":
        return "glider", "glider", _size(cat, "glider")

    # 5. Business jets
    if t in BIZJET_TYPES or (d and _BIZJET_WORDS.search(d) and t not in TURBOPROP_TYPES):
        return "business", "bizjet", _size(cat, "bizjet")

    prefix = airline_prefix(cs)
    reg_norm = re.sub(r"[^A-Z0-9]", "", (reg or "").upper())
    flying_as_reg = bool(cs) and (cs == reg_norm or (not prefix and _REG_LIKE.match(cs) is not None))

    def airframe_shape() -> str:
        if cat == "A5" or t in WIDEBODY_TYPES:
            return "heavy"
        if t in TURBOPROP_TYPES:
            return "turboprop"
        return "jet"

    # 6. Cargo
    if (prefix and prefix in _CARGO_CALLSIGN) or (ownop and _CARGO_WORDS.search(ownop)):
        shape = airframe_shape()
        return "cargo", shape, _size(cat, shape)

    # 7. Airliners: large/heavy categories, airliner type families, or an airline callsign
    if cat in ("A3", "A4", "A5") or _AIRLINER_TYPE.match(t) or t in WIDEBODY_TYPES:
        shape = airframe_shape()
        return "airliner", shape, _size(cat, shape)
    if prefix and not flying_as_reg and cat in ("A2", "", "A0"):
        shape = airframe_shape() if t else "jet"
        return "airliner", shape, _size(cat, shape)

    # 8. Light aircraft
    if cat == "A1" or (cat == "A2" and t in TURBOPROP_TYPES):
        shape = "turboprop" if t in TURBOPROP_TYPES else "prop"
        return "light", shape, _size(cat, shape)
    if t in TURBOPROP_TYPES:
        return "light", "turboprop", _size(cat, "turboprop")
    if flying_as_reg and cat in ("", "A0", "A2"):
        return "light", "prop", _size(cat, "prop")
    if cat == "A6":
        return "unknown", "fighter", _size(cat, "fighter")
    if cat == "A2":
        return "unknown", "jet", _size(cat, "jet")
    return "unknown", "generic", _size(cat, "generic")


def _size(cat: str, shape: str) -> float:
    size = CAT_SIZE.get(cat) or SHAPE_SIZE.get(shape, 0.74)
    if shape == "heavy":
        size = max(size, 0.96)
    return size
