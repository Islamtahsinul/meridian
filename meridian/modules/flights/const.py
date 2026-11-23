"""Constants shared by the flights module: windows, flags, classes, labels and GPS hotspots."""

from __future__ import annotations

import re

# -- time windows (seconds) ---------------------------------------------------------------------
LIVE_MAX_AGE = 60.0            # a position older than this is never shown as live
LIVE_MAX_AGE_SLOW = 110.0      # ... except from worldwide sets polled once a minute (military, squawks)
EST_MAX_AGE = 20 * 60.0        # aircraft that drop out of coverage are dead-reckoned for up to 20 min
TRAIL_MAX_AGE = 45 * 60.0      # trail history per aircraft
TRAIL_MAX_POINTS = 400
TRAIL_MIN_STEP = 15.0          # seconds between trail points in straight flight
KEEP_AFTER_LAST = EST_MAX_AGE + 10 * 60.0   # drop aircraft from memory this long after their last report
AIRCRAFT_MAX = 25000           # hard cap on aircraft held in memory
TRAIL_POINTS_MAX_TOTAL = 1_500_000
MAX_ROWS = 6000                # rows in one /live answer

# -- browser row format ---------------------------------------------------------------------------
FIELDS = ["hex", "lat", "lon", "trk", "alt", "gs", "vr", "cs", "reg", "typ", "cls", "shp", "sz", "flags", "age", "sq"]

FLAG_EST = 1        # estimated (dead-reckoned) position
FLAG_MIL = 2        # military
FLAG_EMG = 4        # emergency squawk or status
FLAG_SANC = 8       # on the OFAC SDN list
FLAG_GND = 16       # on the ground
FLAG_MLAT = 32      # position from multilateration
FLAG_PRIV = 64      # LADD or PIA privacy programme
FLAG_LOCAL = 128    # from the local receiver

DB_MILITARY = 1
DB_INTERESTING = 2
DB_PIA = 4
DB_LADD = 8

# -- classes --------------------------------------------------------------------------------------
CLASSES = {
    "airliner": "Airliner",
    "cargo": "Cargo",
    "business": "Business jet",
    "military": "Military",
    "helicopter": "Helicopter",
    "light": "Light aircraft",
    "glider": "Glider, balloon or drone",
    "ground": "Ground vehicle",
    "unknown": "Unknown",
}

SHAPES = ("jet", "heavy", "turboprop", "bizjet", "prop", "heli", "fighter", "glider", "balloon", "uav",
          "ground", "generic")

CATEGORY_LABELS = {
    "A0": "No category reported",
    "A1": "Light (under 7 t)",
    "A2": "Small (7 to 34 t)",
    "A3": "Large (34 to 136 t)",
    "A4": "High-vortex large (B757)",
    "A5": "Heavy (over 136 t)",
    "A6": "High performance",
    "A7": "Rotorcraft",
    "B0": "No category reported",
    "B1": "Glider or sailplane",
    "B2": "Lighter than air",
    "B3": "Parachutist or skydiver",
    "B4": "Ultralight, hang glider or paraglider",
    "B6": "Unmanned aerial vehicle",
    "B7": "Space or transatmospheric vehicle",
    "C0": "No category reported",
    "C1": "Surface vehicle, emergency",
    "C2": "Surface vehicle, service",
    "C3": "Point obstacle",
    "C4": "Cluster obstacle",
    "C5": "Line obstacle",
}

EMERGENCY_SQUAWKS = {"7500": "Unlawful interference", "7600": "Radio failure", "7700": "General emergency"}
EMERGENCY_STATUS = {
    "general": "General emergency",
    "lifeguard": "Medical priority",
    "minfuel": "Minimum fuel",
    "nordo": "Radio failure",
    "unlawful": "Unlawful interference",
    "downed": "Aircraft downed",
    "reserved": "Emergency (reserved code)",
}
SPECIAL_SQUAWKS = {
    "7400": "Unmanned aircraft, lost link",
    "7000": "VFR conspicuity (Europe)",
    "1200": "VFR (North America)",
    "2000": "Entering controlled airspace without a code",
    "0000": "No code assigned",
}
# Emergency statuses that count as emergencies. "lifeguard" (medical priority) is routine for air
# ambulances and "reserved" is unused, so neither is an alert. A status without an emergency squawk
# is often a transponder glitch, so it rates lower than the squawk itself.
EMERGENCY_STATUS_CODES = ("general", "nordo", "unlawful", "downed", "minfuel")
EMERGENCY_SEVERITY = {"7500": 9, "7700": 7, "7600": 6, "unlawful": 6, "general": 6, "nordo": 5, "minfuel": 5,
                      "downed": 5}

POSITION_SOURCES = {
    "adsb_icao": "ADS-B",
    "adsb_icao_nt": "ADS-B (non-transponder)",
    "adsr_icao": "ADS-R (rebroadcast)",
    "adsb_other": "ADS-B (anonymous address)",
    "adsr_other": "ADS-R (anonymous address)",
    "tisb_icao": "TIS-B (radar-derived)",
    "tisb_trackfile": "TIS-B (radar-derived)",
    "tisb_other": "TIS-B (radar-derived)",
    "mlat": "Multilateration (MLAT)",
    "adsc": "ADS-C (satellite)",
    "mode_s": "Mode S, no position",
    "other": "Other",
    "unknown": "Unknown",
    "opensky_adsb": "ADS-B",
    "opensky_asterix": "Radar (ASTERIX)",
    "opensky_mlat": "Multilateration (MLAT)",
    "opensky_flarm": "FLARM",
}

PROVIDER_NAMES = {
    "adsbfi": "adsb.fi",
    "adsblol": "adsb.lol",
    "opensky": "OpenSky Network",
    "local": "Local receiver",
}

# -- GPS interference -----------------------------------------------------------------------------
GPS_CELL_DEG = 0.5           # storage resolution
GPS_WINDOW_H = 24            # default rolling window
GPS_KEEP_H = 48              # longest window the layer and cell card show
GPS_HISTORY_H = 14 * 24      # hour buckets are kept this long (the 7-day trend needs its own history)
GPS_MIN_SAMPLES = 12         # aircraft-hours a cell needs before it gets a colour
GPS_AMBER = 0.02             # affected share at or above which a cell is amber
GPS_RED = 0.10               # ... and red
GPS_AMBER_MIN_BAD = 2        # amber/red also need at least this many affected aircraft-hours
GPS_RED_MIN_BAD = 3
GPS_NACP_BAD = 8             # NACp below this counts as degraded
GPS_NIC_BAD = 7              # NIC below this counts as degraded (used when NACp is missing)

# Known interference hotspots sampled in the background: (id, name, lat, lon, radius_nm).
HOTSPOTS = (
    ("kaliningrad", "Kaliningrad and the southern Baltic", 55.0, 20.0, 190),
    ("gulf-of-finland", "Gulf of Finland and Estonia", 59.6, 26.8, 160),
    ("finnmark", "Finnmark and the Kola border", 69.6, 28.5, 170),
    ("belarus-border", "Poland, Lithuania and the Belarus border", 52.8, 24.8, 170),
    ("black-sea-west", "Western Black Sea", 44.2, 30.5, 200),
    ("black-sea-east", "Crimea and the eastern Black Sea", 44.0, 37.0, 200),
    ("east-med", "Cyprus, Lebanon and Israel", 33.8, 34.6, 190),
    ("syria-turkey", "Northern Syria and south-east Turkey", 36.8, 39.8, 180),
    ("iraq", "Iraq", 33.2, 44.4, 200),
    ("persian-gulf", "Persian Gulf", 26.6, 51.8, 210),
    ("hormuz", "Strait of Hormuz and Oman", 25.2, 57.4, 160),
    ("red-sea", "Southern Red Sea and Yemen", 15.6, 41.8, 210),
    ("caucasus", "Armenia, Azerbaijan and Georgia", 40.8, 45.8, 180),
    ("libya", "Libya", 31.8, 19.5, 200),
    ("myanmar", "Myanmar", 20.5, 96.0, 220),
    ("korea-dmz", "Korean demilitarised zone", 37.9, 127.0, 140),
)


# -- display text ---------------------------------------------------------------------------------
_KEEP_UPPER = frozenset({
    "ATR", "BAE", "DHC", "CASA", "EADS", "IAI", "PZL", "MBB", "BAC", "HAL", "KAI", "NAMC", "SIAI", "AIDC", "CAC",
    "EMB", "ERJ", "CRJ", "II", "III", "IV", "NG", "XLS", "XL", "ER", "LR", "SP", "SR", "LX", "GT", "MD", "SA",
    "LLC", "LTD", "PLC", "GMBH", "AG", "NV", "BV", "AB", "NA", "USA", "UK", "UAE", "RAF", "USAF", "NATO", "TBM", "SAS"})
_SMALL_WORDS = frozenset({"OF", "AND", "THE", "DE", "DU", "LA", "LE", "DEL", "DA", "VON", "VAN", "FOR", "Y", "E"})


def pretty_name(text: str | None) -> str | None:
    """'AIRBUS A-320neo' -> 'Airbus A-320neo'. Model codes and real acronyms are kept."""
    if not text:
        return text
    if re.search(r"[a-z]", text) and not re.search(r"\b[A-Z]{4,}\b", text):
        return text
    out = []
    for i, word in enumerate(re.split(r"(\s+)", text)):
        if not re.fullmatch(r"[A-Z]{2,}[,.]?", word):
            out.append(word)
            continue
        bare = word.rstrip(",.")
        if bare in _KEEP_UPPER:
            out.append(word)
        elif i > 0 and bare in _SMALL_WORDS:
            out.append(word.lower())
        else:
            out.append(word[0] + word[1:].lower())
    return "".join(out)
