"""Reference tables for airspace and airport status: FAA location ids outside the contiguous US,
major US cities (for "TFR over a city" events), conflict-zone country names and TFR rule texts."""

from __future__ import annotations

# FAA location id -> ICAO code for airports outside the contiguous US (Alaska, Hawaii, territories).
# Contiguous-US airports are "K" + the FAA id (SFO -> KSFO).
NONCONUS_ICAO = {
    "ANC": "PANC", "FAI": "PAFA", "JNU": "PAJN", "KTN": "PAKT", "SIT": "PASI", "BET": "PABE", "ADQ": "PADQ",
    "OME": "PAOM", "OTZ": "PAOT", "BRW": "PABR", "CDV": "PACV", "YAK": "PAYA", "DLG": "PADL", "AKN": "PAKN",
    "ENA": "PAEN", "SCC": "PASC", "MRI": "PAMR", "DUT": "PADU", "WRG": "PAWG", "PSG": "PAPG",
    "HNL": "PHNL", "OGG": "PHOG", "KOA": "PHKO", "LIH": "PHLI", "ITO": "PHTO", "MKK": "PHMK", "LNY": "PHNY",
    "JHM": "PHJH", "GUM": "PGUM", "GSN": "PGSN", "ROP": "PGRO", "TNI": "PGWT",
    "SJU": "TJSJ", "BQN": "TJBQ", "PSE": "TJPS", "SIG": "TJIG", "MAZ": "TJMZ", "STT": "TIST", "STX": "TISX",
    "PPG": "NSTU",
}
ICAO_TO_FAA = {v: k for k, v in NONCONUS_ICAO.items()}

# The largest US cities (centre points). A new temporary flight restriction near one of them is an event.
MAJOR_CITIES = [
    ("New York", 40.71, -74.01), ("Los Angeles", 34.05, -118.24), ("Chicago", 41.88, -87.63),
    ("Houston", 29.76, -95.37), ("Phoenix", 33.45, -112.07), ("Philadelphia", 39.95, -75.17),
    ("San Antonio", 29.42, -98.49), ("San Diego", 32.72, -117.16), ("Dallas", 32.78, -96.80),
    ("San Jose", 37.34, -121.89), ("Austin", 30.27, -97.74), ("Jacksonville", 30.33, -81.66),
    ("Fort Worth", 32.76, -97.33), ("Columbus", 39.96, -83.00), ("Charlotte", 35.23, -80.84),
    ("Indianapolis", 39.77, -86.16), ("San Francisco", 37.77, -122.42), ("Seattle", 47.61, -122.33),
    ("Denver", 39.74, -104.99), ("Washington", 38.91, -77.04), ("Boston", 42.36, -71.06),
    ("Nashville", 36.16, -86.78), ("Oklahoma City", 35.47, -97.52), ("El Paso", 31.76, -106.49),
    ("Las Vegas", 36.17, -115.14), ("Detroit", 42.33, -83.05), ("Portland", 45.52, -122.68),
    ("Memphis", 35.15, -90.05), ("Louisville", 38.25, -85.76), ("Baltimore", 39.29, -76.61),
    ("Milwaukee", 43.04, -87.91), ("Albuquerque", 35.08, -106.65), ("Tucson", 32.22, -110.97),
    ("Fresno", 36.74, -119.79), ("Sacramento", 38.58, -121.49), ("Kansas City", 39.10, -94.58),
    ("Atlanta", 33.75, -84.39), ("Miami", 25.76, -80.19), ("Omaha", 41.26, -95.93), ("Raleigh", 35.78, -78.64),
    ("Minneapolis", 44.98, -93.27), ("Tampa", 27.95, -82.46), ("New Orleans", 29.95, -90.07),
    ("Cleveland", 41.50, -81.69), ("Honolulu", 21.31, -157.86), ("Anchorage", 61.22, -149.90),
    ("Pittsburgh", 40.44, -80.00), ("St. Louis", 38.63, -90.20), ("Cincinnati", 39.10, -84.51),
    ("Orlando", 28.54, -81.38), ("Salt Lake City", 40.76, -111.89), ("San Juan", 18.47, -66.11),
    ("Buffalo", 42.89, -78.88), ("Richmond", 37.54, -77.44), ("Birmingham", 33.52, -86.80),
    ("Hartford", 41.76, -72.68), ("Providence", 41.82, -71.41), ("Virginia Beach", 36.85, -75.98),
    ("Oakland", 37.80, -122.27), ("Long Beach", 33.77, -118.19), ("Tulsa", 36.15, -95.99),
    ("Newark", 40.74, -74.17), ("West Palm Beach", 26.72, -80.05),
]

# Country names used in EASA bulletins -> ISO 3166-1 alpha-2.
COUNTRY_ISO2 = {
    "afghanistan": "AF", "algeria": "DZ", "armenia": "AM", "azerbaijan": "AZ", "bahrain": "BH", "bangladesh": "BD",
    "belarus": "BY", "burkina faso": "BF", "cameroon": "CM", "central african republic": "CF", "chad": "TD",
    "china": "CN", "colombia": "CO", "cyprus": "CY", "democratic republic of the congo": "CD", "djibouti": "DJ",
    "egypt": "EG", "eritrea": "ER", "estonia": "EE", "ethiopia": "ET", "finland": "FI", "georgia": "GE",
    "guyana": "GY", "haiti": "HT", "india": "IN", "iran": "IR", "islamic republic of iran": "IR", "iraq": "IQ",
    "israel": "IL", "japan": "JP", "jordan": "JO", "kenya": "KE", "kosovo": "XK", "kuwait": "KW", "latvia": "LV",
    "lebanon": "LB", "libya": "LY", "lithuania": "LT", "mali": "ML", "mauritania": "MR", "moldova": "MD",
    "myanmar": "MM", "niger": "NE", "nigeria": "NG", "north korea": "KP",
    "democratic people's republic of korea": "KP", "oman": "OM", "pakistan": "PK", "palestine": "PS",
    "poland": "PL", "qatar": "QA", "romania": "RO", "russia": "RU", "russian federation": "RU",
    "saudi arabia": "SA", "serbia": "RS", "somalia": "SO", "south korea": "KR", "south sudan": "SS",
    "sudan": "SD", "syria": "SY", "syrian arab republic": "SY", "taiwan": "TW", "tunisia": "TN", "turkey": "TR",
    "türkiye": "TR", "uae": "AE", "uganda": "UG", "ukraine": "UA", "united arab emirates": "AE",
    "venezuela": "VE", "yemen": "YE",
}

# TFR list "type" -> (category, label). Categories colour the map.
TFR_TYPES = {
    "VIP": ("vip", "VIP movement"),
    "SECURITY": ("security", "Security"),
    "SPECIAL": ("security", "Special security notice"),
    "HAZARDS": ("hazards", "Hazard"),
    "SPACE OPERATIONS": ("space", "Space operations"),
    "AIR SHOWS/SPORTS": ("sports", "Air show or sporting event"),
    "UAS PUBLIC GATHERING": ("drones", "Drone restriction for a public gathering"),
    "EMERGENCY TRAFFIC RULES": ("hazards", "Emergency air traffic rules"),
    "HAWAII DISASTER": ("hazards", "Hawaii disaster area"),
    "PRESSURE": ("hazards", "High barometric pressure"),
}
TFR_CATEGORY_LABELS = {
    "vip": "VIP movement", "security": "Security", "hazards": "Hazard", "space": "Space operations",
    "sports": "Air show or sporting event", "drones": "Drones only", "other": "Other",
}

# 14 CFR / 49 USC section quoted in the notice -> what it is in plain words.
TFR_RULES = [
    ("91.137(a)(1)", "Disaster or hazard area (14 CFR 91.137(a)(1))"),
    ("91.137(a)(2)", "Relief aircraft operations, for example firefighting (14 CFR 91.137(a)(2))"),
    ("91.137(a)(3)", "Disaster site, sightseeing aircraft kept out (14 CFR 91.137(a)(3))"),
    ("91.137", "Disaster or hazard area (14 CFR 91.137)"),
    ("91.138", "National disaster area in Hawaii (14 CFR 91.138)"),
    ("91.139", "Emergency air traffic rules (14 CFR 91.139)"),
    ("91.141", "Presidential and other VIP movement (14 CFR 91.141)"),
    ("91.143", "Space flight operations (14 CFR 91.143)"),
    ("91.144", "Abnormally high barometric pressure (14 CFR 91.144)"),
    ("91.145", "Aerial demonstration or major sporting event (14 CFR 91.145)"),
    ("99.7", "Special security instructions (14 CFR 99.7)"),
    ("44812", "Drone operations over a public gathering (49 USC 44812)"),
]

# Common NOTAM contractions in FAA airport notices, for plain-language summaries.
NOTAM_WORDS = {
    "AD": "aerodrome", "AP": "airport", "CLSD": "closed", "NON": "non", "SKED": "scheduled", "ACFT": "aircraft",
    "GA": "general aviation", "EXC": "except", "PPR": "prior permission", "TFC": "traffic", "RWY": "runway",
    "TWY": "taxiway", "TRANSIENT": "transient", "OPS": "operations", "MAINT": "maintenance", "AVBL": "available",
    "BTN": "between", "DLY": "daily", "TIL": "until", "WEF": "from", "HEL": "helicopter", "MIL": "military",
    "CIV": "civil", "ALL": "all", "TO": "to", "AND": "and", "OR": "or", "WITH": "with", "FOR": "for",
    "IFR": "IFR", "VFR": "VFR", "24HR": "24 hours'", "48HR": "48 hours'", "72HR": "72 hours'",
}
