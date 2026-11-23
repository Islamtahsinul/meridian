"""Vessel classes from the AIS "type of ship and cargo" code (ITU-R M.1371 table 53).

The browser holds the matching colour scheme (web/js/layers/ships/common.js); the class ids and
their order are shared, so keep the two lists in step.
"""

from __future__ import annotations

# (id, label, shape) in legend order. "hull" = large-ship arrow, "dart" = small-craft arrow.
CLASSES = [
    ("cargo", "Cargo", "hull"),
    ("tanker", "Tanker", "hull"),
    ("passenger", "Passenger", "hull"),
    ("fishing", "Fishing", "dart"),
    ("service", "Tug, pilot, SAR or law enforcement", "dart"),
    ("pleasure", "Sailing or pleasure", "dart"),
    ("highspeed", "High-speed craft", "dart"),
    ("military", "Military", "hull"),
    ("other", "Other or unknown", "dart"),
]
CLASS_IDS = [c[0] for c in CLASSES]
CLASS_INDEX = {c: i for i, c in enumerate(CLASS_IDS)}
CLASS_LABEL = {c[0]: c[1] for c in CLASSES}

_HAZARD = {1: "hazardous category A", 2: "hazardous category B", 3: "hazardous category C",
           4: "hazardous category D"}


def classify(ship_type: int | None) -> str:
    t = ship_type if isinstance(ship_type, int) else None
    if t is None:
        return "other"
    if 70 <= t <= 79:
        return "cargo"
    if 80 <= t <= 89:
        return "tanker"
    if 60 <= t <= 69:
        return "passenger"
    if t == 30:
        return "fishing"
    if t in (31, 32) or 50 <= t <= 59:
        return "service"
    if t == 35:
        return "military"
    if t in (36, 37):
        return "pleasure"
    if 40 <= t <= 49:
        return "highspeed"
    return "other"


def type_label(ship_type: int | None) -> str:
    """Plain-language AIS ship type, e.g. 84 -> "Tanker, hazardous category D"."""
    t = ship_type if isinstance(ship_type, int) else None
    if t is None or t == 0:
        return "Type not reported"
    if 1 <= t <= 19:
        return f"Reserved type {t}"
    tens, unit = divmod(t, 10)
    if tens in (2, 4, 6, 7, 8, 9):
        base = {2: "Wing in ground craft", 4: "High-speed craft", 6: "Passenger ship", 7: "Cargo ship",
                8: "Tanker", 9: "Other type"}[tens]
        if unit == 0 or unit == 9:
            return base
        if unit in _HAZARD:
            return f"{base}, {_HAZARD[unit]}"
        return f"{base} (reserved code {t})"
    special = {
        30: "Fishing vessel", 31: "Towing vessel", 32: "Towing vessel, long tow", 33: "Dredger or underwater operations",
        34: "Diving operations", 35: "Military operations", 36: "Sailing vessel", 37: "Pleasure craft",
        38: "Reserved type 38", 39: "Reserved type 39",
        50: "Pilot vessel", 51: "Search and rescue vessel", 52: "Tug", 53: "Port tender",
        54: "Anti-pollution vessel", 55: "Law enforcement vessel", 56: "Local vessel", 57: "Local vessel",
        58: "Medical transport", 59: "Non-combatant ship",
    }
    return special.get(t, f"Type {t}")


def size_class(length: float | None) -> int:
    """0 unknown, 1 under 40 m, 2 from 40 m, 3 from 150 m, 4 from 250 m."""
    if not length:
        return 0
    if length >= 250:
        return 4
    if length >= 150:
        return 3
    if length >= 40:
        return 2
    return 1
