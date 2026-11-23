"""Rule presets: what a watchlist can watch for, with thresholds and plain wording.

Each preset matches events from ``app.history.events`` (CONTRACT.md 15.3) by their real ``kind`` and,
where needed, the extra fields modules publish (``magnitude``, ``category``, ``event``,
``severity_text``). Kinds and fields were read from the live event history, not guessed:

==================  ==========  ===========================================================
preset              kind        extra fields used
==================  ==========  ===========================================================
quakes              earthquake  ``magnitude`` (falls back to "M 6.5" in the title)
aircraft            aircraft    ``event == "emergency"`` (flights also publishes "sanctioned")
ships_dark          ship        ``category == "dark_ship"``
ships_sanctioned    ship        ``category == "sanctioned_vessel"``
conflict            conflict_event  severity (intel publishes 4 to 9)
storms              storm       severity (3 depression to 9 category 5); forecast cones
warnings            warning     ``severity_text`` Severe or Extreme
volcanoes           volcano     severity 3, 6 or 9 (alert level)
outbreaks           outbreak    ``places`` (WHO names several countries)
gps                 gps_jam     severity 4 amber, 5 or 6 red; ships publish ``gnss_interference``
any                 (all)       severity
==================  ==========  ===========================================================
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

ESCALATE_BY = 2.0                 # re-alert when severity rises by this much
MOVING_KINDS = frozenset({"ship", "aircraft", "storm", "sanctioned_vessel", "sanctioned_aircraft"})


class RuleError(ValueError):
    """Bad rule parameters (the message is shown to the user)."""


@dataclass(frozen=True)
class Param:
    id: str
    label: str
    type: str                      # "select" or "toggle"
    default: Any
    choices: tuple = ()            # ((value, label), ...) for selects

    def clean(self, value: Any) -> Any:
        if value is None:
            return self.default
        if self.type == "toggle":
            if not isinstance(value, bool):
                raise RuleError(f"{self.label} must be on or off")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise RuleError(f"{self.label} must be one of the listed values")
        for v, _label in self.choices:
            if abs(float(v) - float(value)) < 1e-9:
                return v
        raise RuleError(f"{self.label} must be one of the listed values")

    def to_json(self) -> dict:
        out = {"id": self.id, "label": self.label, "type": self.type, "default": self.default}
        if self.choices:
            out["choices"] = [{"value": v, "label": label} for v, label in self.choices]
        return out


def _sev(ev: dict) -> float:
    v = ev.get("severity")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        return 0.0
    return float(v)


_MAG = re.compile(r"^\s*M\s*([0-9]+(?:\.[0-9]+)?)")


def magnitude(ev: dict) -> float | None:
    v = ev.get("magnitude")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
        return float(v)
    m = _MAG.match(str(ev.get("title") or ""))
    return float(m.group(1)) if m else None


def _num_text(v: float) -> str:
    return f"{v:g}"


SEVERITY_CHOICES = tuple((n, f"{n}, {'extreme' if n >= 9 else 'serious' if n >= 6 else 'notable'}") for n in range(3, 11))


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str
    modules: tuple                 # modules whose events it needs
    kinds: tuple | None            # None: every kind
    match: Callable[[dict, dict], bool]
    phrase: Callable[[dict], str]  # the rule in words, given clean params
    detail: Callable[[dict, dict], str]   # what about this event matched, for the "why" line
    params: tuple = field(default_factory=tuple)
    gps: bool = False              # only new, returning or worsening cells (chronic ones stay quiet)

    def clean_params(self, raw: Any) -> dict:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise RuleError("rule settings must be an object")
        known = {p.id for p in self.params}
        unknown = [k for k in raw if k not in known]
        if unknown:
            raise RuleError(f"unknown setting {unknown[0]!r} for {self.label.lower()}")
        return {p.id: p.clean(raw.get(p.id)) for p in self.params}

    def to_json(self) -> dict:
        defaults = {p.id: p.default for p in self.params}
        return {"id": self.id, "label": self.label, "description": self.description, "modules": list(self.modules),
                "kinds": list(self.kinds) if self.kinds else None, "params": [p.to_json() for p in self.params],
                "summary": self.phrase(defaults)}


# ---------------------------------------------------------------------------------------------
# the presets
# ---------------------------------------------------------------------------------------------
def _quake_match(ev, p):
    mag = magnitude(ev)
    return ev.get("kind") == "earthquake" and mag is not None and mag >= p["min_magnitude"] - 1e-9


def _aircraft_match(ev, p):
    return ev.get("kind") == "aircraft" and ev.get("event") == "emergency" and _sev(ev) >= p["min_severity"]


def _dark_match(ev, p):
    return ev.get("kind") == "ship" and ev.get("category") == "dark_ship"


def _sanctioned_match(ev, p):
    return ((ev.get("kind") == "ship" and ev.get("category") == "sanctioned_vessel")
            or ev.get("kind") == "sanctioned_vessel")


def _conflict_match(ev, p):
    return ev.get("kind") == "conflict_event" and _sev(ev) >= p["min_severity"]


def _storm_match(ev, p):
    return ev.get("kind") == "storm" and _sev(ev) >= p["min_severity"]


def _warning_match(ev, p):
    if ev.get("kind") != "warning":
        return False
    text = str(ev.get("severity_text") or "").lower()
    if p["extreme_only"]:
        return text == "extreme" or (not text and _sev(ev) >= 9)
    return text in ("severe", "extreme") or (not text and _sev(ev) >= 6)


def _volcano_match(ev, p):
    return ev.get("kind") == "volcano" and _sev(ev) >= p["min_severity"]


def _outbreak_match(ev, p):
    return ev.get("kind") == "outbreak"


def _gps_match(ev, p):
    if ev.get("kind") != "gps_jam" or _sev(ev) < p["min_severity"]:
        return False
    return p["ships"] or ev.get("module") != "ships"


def _any_match(ev, p):
    return _sev(ev) >= p["min_severity"]


def _warning_detail(ev, p):
    text = str(ev.get("severity_text") or "").strip().lower()
    return f"{text} warning" if text else f"warning, severity {_num_text(_sev(ev))}"


def _kind_word(kind: str) -> str:
    return KIND_WORDS.get(kind) or str(kind or "event").replace("_", " ")


KIND_WORDS = {
    "earthquake": "earthquake", "aircraft": "aircraft", "ship": "ship", "conflict_event": "conflict event",
    "news": "news report", "gps_jam": "GPS interference", "warning": "weather warning", "fire": "fire",
    "storm": "tropical cyclone", "volcano": "volcano alert", "outbreak": "disease outbreak", "flood": "flood",
    "disaster": "disaster alert", "tsunami": "tsunami message", "launch": "launch", "convergence": "convergence",
    "airport_delay": "airport delay", "tfr": "flight restriction", "strait": "strait", "space_weather": "space weather",
    "outage": "internet outage", "cyber": "cyber event", "risk_zone": "risk zone",
}

PRESETS: dict[str, Preset] = {}


def _add(p: Preset) -> None:
    PRESETS[p.id] = p


_add(Preset(
    "quakes", "Earthquakes", "USGS earthquakes inside the area.", ("hazards",), ("earthquake",), _quake_match,
    lambda p: f"Earthquakes of magnitude {_num_text(p['min_magnitude'])} or more",
    lambda ev, p: f"magnitude {_num_text(magnitude(ev) or 0)}, threshold {_num_text(p['min_magnitude'])}",
    (Param("min_magnitude", "Smallest magnitude", "select", 5.0,
           tuple((m, f"M {m:g}") for m in (3.0, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5))),)))
_add(Preset(
    "aircraft", "Aircraft emergencies",
    "Aircraft squawking 7500, 7600 or 7700, or reporting an emergency status. Only aircraft that broadcast "
    "ADS-B to volunteer receivers are seen.", ("flights",), ("aircraft",), _aircraft_match,
    lambda p: "Aircraft emergencies" if p["min_severity"] < 7 else "Aircraft emergencies: 7700 and 7500 only",
    lambda ev, p: "emergency squawk or status",
    (Param("min_severity", "Which emergencies", "select", 0,
           ((0, "Every emergency"), (7, "General emergency (7700) and unlawful interference (7500)"))),)))
_add(Preset(
    "ships_dark", "Ships going dark",
    "Vessels whose AIS went silent while ships nearby kept reporting. Gaps have innocent causes too, such as "
    "receiver coverage and equipment faults.", ("ships",), ("ship",), _dark_match,
    lambda p: "Ships going dark", lambda ev, p: "AIS went silent"))
_add(Preset(
    "ships_sanctioned", "Sanctioned ships seen",
    "Vessels on the sanctions lists (OFAC, UK, UN or EU) reporting AIS positions inside the area.",
    ("ships",), ("ship", "sanctioned_vessel"), _sanctioned_match,
    lambda p: "Sanctioned ships seen", lambda ev, p: "sanctions-listed vessel reporting its position"))
_add(Preset(
    "conflict", "Conflict events",
    "Machine-coded conflict reports (GDELT). Severity runs from 0 (routine) to 10 (extreme).", ("intel",),
    ("conflict_event",), _conflict_match,
    lambda p: f"Conflict events of severity {p['min_severity']} or more",
    lambda ev, p: f"severity {_num_text(_sev(ev))}, threshold {p['min_severity']}",
    (Param("min_severity", "Lowest severity", "select", 6, SEVERITY_CHOICES[1:7]),)))
_add(Preset(
    "storms", "Tropical storms",
    "Active cyclones from NOAA NHC, GDACS and NASA EONET. With cones on, a storm counts when its centre, "
    "forecast cone, forecast track or wind area touches the area.", ("hazards",), ("storm",), _storm_match,
    lambda p: ({3: "Tropical cyclones", 4: "Tropical storms or stronger", 5: "Category 1 or stronger",
                7: "Category 3 or stronger"}.get(p["min_severity"], "Tropical cyclones")
               + (", including forecast cones" if p["cones"] else "")),
    lambda ev, p: f"severity {_num_text(_sev(ev))}",
    (Param("min_severity", "Strength", "select", 3,
           ((3, "Any tropical cyclone"), (4, "Tropical storm or stronger"), (5, "Category 1 or stronger"),
            (7, "Category 3 or stronger"))),
     Param("cones", "Include forecast cones and tracks", "toggle", True))))
_add(Preset(
    "warnings", "Weather warnings",
    "Official severe and extreme warnings from the US National Weather Service and MeteoAlarm (Europe).",
    ("hazards",), ("warning",), _warning_match,
    lambda p: "Extreme weather warnings" if p["extreme_only"] else "Severe and extreme weather warnings",
    _warning_detail, (Param("extreme_only", "Extreme warnings only", "toggle", False),)))
_add(Preset(
    "volcanoes", "Volcano alerts", "Volcano alerts from USGS, GDACS and NASA EONET.", ("hazards",), ("volcano",),
    _volcano_match,
    lambda p: {3: "Volcano alerts", 6: "Volcano alerts at orange or red", 9: "Volcano alerts at red"}.get(
        p["min_severity"], "Volcano alerts"),
    lambda ev, p: f"alert severity {_num_text(_sev(ev))}",
    (Param("min_severity", "Alert level", "select", 3,
           ((3, "Any alert"), (6, "Orange or red"), (9, "Red only"))),)))
_add(Preset(
    "outbreaks", "Disease outbreaks",
    "WHO Disease Outbreak News, placed in the countries each report names.", ("hazards",), ("outbreak",),
    _outbreak_match, lambda p: "Disease outbreaks reported by WHO", lambda ev, p: "WHO outbreak report"))
_add(Preset(
    "gps", "GPS interference",
    "Interference inferred from what aircraft (and ships) report. Cells that repeat every day stay quiet: you "
    "hear about new cells, cells that come back after three quiet days and cells that get worse.",
    ("flights",), ("gps_jam",), _gps_match,
    lambda p: "New or worsening GPS interference" + ("" if p["ships"] else ", aircraft only"),
    lambda ev, p: f"severity {_num_text(_sev(ev))}",
    (Param("min_severity", "Cells", "select", 4, ((4, "Amber and red"), (5, "Red only"))),
     Param("ships", "Include possible GNSS spoofing reported by ships", "toggle", True)),
    gps=True))
_add(Preset(
    "any", "Any event",
    "Every kind of event Meridian records: hazards, conflict, news, flights, ships, space and more.", (),
    None, _any_match, lambda p: f"Any event of severity {p['min_severity']} or more",
    lambda ev, p: f"{_kind_word(ev.get('kind'))}, severity {_num_text(_sev(ev))}, threshold {p['min_severity']}",
    (Param("min_severity", "Lowest severity", "select", 8, SEVERITY_CHOICES),)))

ORDER = tuple(PRESETS)


def get(preset_id: str) -> Preset:
    p = PRESETS.get(preset_id) if isinstance(preset_id, str) else None
    if p is None:
        raise RuleError(f"unknown rule {preset_id!r}; choose one of {', '.join(ORDER)}")
    return p


# ---------------------------------------------------------------------------------------------
# episodes and dedupe keys
# ---------------------------------------------------------------------------------------------
def episode_gap_s(ev: dict) -> float | None:
    """How long an entity must have been quiet before a fresh report counts as a new episode.

    The history keeps one row per (module, kind, id), so the same aircraft's next emergency, a ship
    going dark again or a jamming cell coming back reuse the row. A jump in its latest ``time`` this big
    since we last saw it is a new episode. One-off events (quakes, conflict, news) have none."""
    kind = ev.get("kind")
    if kind == "aircraft":
        return 6 * 3600.0
    if kind == "ship":
        return 6 * 3600.0 if ev.get("category") == "dark_ship" else 48 * 3600.0
    if kind == "gps_jam":
        return 72 * 3600.0
    if kind == "airport_delay":
        return 12 * 3600.0
    if kind == "strait":
        return 24 * 3600.0
    return None


def dedupe_ref(ev: dict) -> str:
    """The id used for dedupe. Ships' GNSS spoofing hotspots are keyed by position (``ships:59.91,28.66``),
    so a drifting hotspot would look new every time: fold them into 0.5° cells like the aircraft ones."""
    ref = str(ev.get("id") or "")
    if ev.get("kind") == "gps_jam" and ev.get("module") == "ships":
        try:
            lat = math.floor(float(ev["lat"]) * 2) / 2
            lon = math.floor(float(ev["lon"]) * 2) / 2
            return f"cell:{lat:g}:{lon:g}"
        except (KeyError, TypeError, ValueError):
            return ref
    return ref
