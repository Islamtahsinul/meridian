"""Code tables for the intel module: CAMEO event and actor codes in plain words, FIPS 10-4 country
codes (used by GDELT for places) to ISO 3166-1 alpha-2, and a few naming helpers. Pure data."""

from __future__ import annotations

# ---------------------------------------------------------------------------------------------
# CAMEO event codes (GDELT 2.0). Root codes 01-20 and the detailed codes of the material-conflict
# roots 18, 19 and 20 in plain words.
# ---------------------------------------------------------------------------------------------
CAMEO_ROOT = {
    "01": "Public statement", "02": "Appeal", "03": "Intent to cooperate", "04": "Consultation",
    "05": "Diplomatic cooperation", "06": "Material cooperation", "07": "Aid", "08": "Concession",
    "09": "Investigation", "10": "Demand", "11": "Criticism", "12": "Rejection", "13": "Threat",
    "14": "Protest", "15": "Show of force", "16": "Reduced relations", "17": "Coercion",
    "18": "Assault", "19": "Fighting", "20": "Mass violence",
}

CAMEO_EVENT = {
    "180": "Violent attack",
    "181": "Abduction or hostage-taking",
    "182": "Physical assault",
    "1821": "Sexual assault",
    "1822": "Torture",
    "1823": "Killing by assault",
    "183": "Bombing",
    "1831": "Suicide bombing",
    "1832": "Vehicle bombing",
    "1833": "Roadside bombing",
    "1834": "Bombing of a location",
    "184": "Use of human shields",
    "185": "Assassination attempt",
    "186": "Assassination",
    "190": "Use of military force",
    "191": "Blockade or movement restriction",
    "192": "Occupation of territory",
    "193": "Small-arms fighting",
    "194": "Artillery or tank fire",
    "195": "Air strike",
    "1951": "Precision-guided air strike",
    "1952": "Drone strike",
    "196": "Ceasefire violation",
    "200": "Mass violence",
    "201": "Mass expulsion",
    "202": "Mass killing",
    "203": "Ethnic cleansing",
    "204": "Use of weapons of mass destruction",
    "2041": "Chemical, biological or radiological attack",
    "2042": "Nuclear detonation",
}

# Codes that describe attacks or mass violence (category "attack").
ATTACK_CODES = frozenset({"181", "183", "1831", "1832", "1833", "1834", "184", "185", "186",
                          "200", "201", "202", "203", "204", "2041", "2042"})
# Heavy weapons, bombings and mass killing: a severity bonus and always "armed".
HEAVY_CODES = frozenset({"194", "195", "1951", "1952", "183", "1831", "1832", "1833", "1834",
                         "202", "203", "204", "2041", "2042"})
# Military-style codes that count as armed conflict when a state actor is involved.
STATE_FORCE_CODES = frozenset({"190", "191", "192", "196"})
# Ordering used when several codes merge into one event: the more specific and severe wins.
_CODE_WEIGHT = {
    "2042": 30, "2041": 29, "204": 28, "203": 27, "202": 26, "201": 22, "200": 21,
    "1952": 25, "1951": 24, "195": 23, "194": 20, "1831": 19, "1832": 19, "1833": 19, "1834": 18,
    "183": 17, "186": 16, "1823": 15, "196": 14, "193": 13, "185": 12, "181": 11, "192": 10,
    "190": 9, "1822": 8, "1821": 8, "191": 7, "182": 6, "184": 6, "180": 5,
    # protest codes (root 14) only ever merge with each other: riots first, then blockades, strikes, rallies
    "145": 4, "1451": 4, "1452": 4, "1453": 4, "1454": 4, "144": 3, "1441": 3, "1442": 3, "1443": 3, "1444": 3,
    "143": 2, "1431": 2, "1432": 2, "1433": 2, "1434": 2, "142": 2, "1421": 2, "1422": 2, "1423": 2, "1424": 2,
    "141": 1, "1411": 1, "1412": 1, "1413": 1, "1414": 1,
}

CATEGORY_LABEL = {
    "attack": "Attack or mass violence",
    "fighting": "Fighting",
    "assault": "Assault",
}

# ---------------------------------------------------------------------------------------------
# CAMEO actor type codes
# ---------------------------------------------------------------------------------------------
ACTOR_TYPE = {
    "COP": "police", "GOV": "government", "INS": "insurgents", "JUD": "judiciary", "MIL": "military",
    "OPP": "opposition", "REB": "rebels", "SEP": "separatists", "SPY": "intelligence service",
    "UAF": "armed group", "AGR": "agriculture", "BUS": "business", "CRM": "criminals",
    "CVL": "civilians", "DEV": "development", "EDU": "education", "ELI": "elites",
    "ENV": "environmental group", "HLH": "health", "HRI": "human rights group", "LAB": "labour",
    "LEG": "legislature", "MED": "media", "REF": "refugees", "MOD": "moderates", "RAD": "radicals",
    "AMN": "Amnesty International", "IRC": "Red Cross", "GRP": "group", "NGO": "aid organisation",
    "IGO": "intergovernmental body", "MNC": "multinational company", "SET": "settlers", "UNO": "United Nations",
    "PTY": "political party", "CHR": "Christian", "MOS": "Muslim", "JEW": "Jewish", "HIN": "Hindu",
    "BUD": "Buddhist",
}
ARMED_TYPES = frozenset({"MIL", "UAF", "REB", "INS", "SEP", "SPY"})
CRIME_TYPES = frozenset({"COP", "CRM", "JUD"})

# Region and bloc codes CAMEO uses in place of a country.
CAMEO_REGIONS = {
    "AFR": "Africa", "ASA": "Asia", "BLK": "the Balkans", "CRB": "the Caribbean", "CAU": "the Caucasus",
    "CFR": "Central Africa", "CAS": "Central Asia", "CEU": "Central Europe", "EIN": "East Indies",
    "EAF": "East Africa", "EEU": "Eastern Europe", "EUR": "Europe", "LAM": "Latin America",
    "MEA": "the Middle East", "MDT": "the Mediterranean", "NAF": "North Africa", "NMR": "North America",
    "PGS": "the Persian Gulf", "SCN": "Scandinavia", "SAM": "South America", "SAS": "South Asia",
    "SEA": "Southeast Asia", "SAF": "Southern Africa", "WAF": "West Africa", "WST": "the West",
}

# ---------------------------------------------------------------------------------------------
# FIPS 10-4 (GDELT ActionGeo_CountryCode) to ISO 3166-1 alpha-2. Cross-checked against the
# FIPS_10 / ISO_A2_EH pairs in Natural Earth admin-0; extra rows cover codes NE leaves blank.
# None marks places with no country (oceans, disputed islands).
# ---------------------------------------------------------------------------------------------
FIPS_TO_ISO2: dict[str, str | None] = {
    "AA": "AW", "AC": "AG", "AE": "AE", "AF": "AF", "AG": "DZ", "AJ": "AZ", "AL": "AL", "AM": "AM",
    "AN": "AD", "AO": "AO", "AQ": "AS", "AR": "AR", "AS": "AU", "AT": "AU", "AU": "AT", "AV": "AI",
    "AY": "AQ", "BA": "BH", "BB": "BB", "BC": "BW", "BD": "BM", "BE": "BE", "BF": "BS", "BG": "BD",
    "BH": "BZ", "BK": "BA", "BL": "BO", "BM": "MM", "BN": "BJ", "BO": "BY", "BP": "SB", "BR": "BR",
    "BT": "BT", "BU": "BG", "BV": "BV", "BX": "BN", "BY": "BI", "CA": "CA", "CB": "KH", "CD": "TD",
    "CE": "LK", "CF": "CG", "CG": "CD", "CH": "CN", "CI": "CL", "CJ": "KY", "CK": "CC", "CM": "CM",
    "CN": "KM", "CO": "CO", "CQ": "MP", "CR": "AU", "CS": "CR", "CT": "CF", "CU": "CU", "CV": "CV",
    "CW": "CK", "CY": "CY", "DA": "DK", "DJ": "DJ", "DO": "DM", "DR": "DO", "EC": "EC", "EG": "EG",
    "EI": "IE", "EK": "GQ", "EN": "EE", "ER": "ER", "ES": "SV", "ET": "ET", "EZ": "CZ", "FG": "GF",
    "FI": "FI", "FJ": "FJ", "FK": "FK", "FM": "FM", "FO": "FO", "FP": "PF", "FR": "FR", "FS": "TF",
    "GA": "GM", "GB": "GA", "GG": "GE", "GH": "GH", "GI": "GI", "GJ": "GD", "GK": "GG", "GL": "GL",
    "GM": "DE", "GP": "GP", "GQ": "GU", "GR": "GR", "GT": "GT", "GV": "GN", "GY": "GY", "GZ": "PS",
    "HA": "HT", "HK": "HK", "HM": "HM", "HO": "HN", "HR": "HR", "HU": "HU", "IC": "IS", "ID": "ID",
    "IM": "IM", "IN": "IN", "IO": "IO", "IR": "IR", "IS": "IL", "IT": "IT", "IV": "CI", "IZ": "IQ",
    "JA": "JP", "JE": "JE", "JM": "JM", "JN": "SJ", "JO": "JO", "KE": "KE", "KG": "KG", "KN": "KP",
    "KR": "KI", "KS": "KR", "KT": "CX", "KU": "KW", "KV": "XK", "KZ": "KZ", "LA": "LA", "LE": "LB",
    "LG": "LV", "LH": "LT", "LI": "LR", "LO": "SK", "LS": "LI", "LT": "LS", "LU": "LU", "LY": "LY",
    "MA": "MG", "MB": "MQ", "MC": "MO", "MD": "MD", "MF": "YT", "MG": "MN", "MH": "MS", "MI": "MW",
    "MJ": "ME", "MK": "MK", "ML": "ML", "MN": "MC", "MO": "MA", "MP": "MU", "MR": "MR", "MT": "MT",
    "MU": "OM", "MV": "MV", "MX": "MX", "MY": "MY", "MZ": "MZ", "NC": "NC", "NE": "NU", "NF": "NF",
    "NG": "NE", "NH": "VU", "NI": "NG", "NL": "NL", "NN": "SX", "NO": "NO", "NP": "NP", "NR": "NR",
    "NS": "SR", "NT": "CW", "NU": "NI", "NZ": "NZ", "OD": "SS", "PA": "PY", "PC": "PN", "PE": "PE",
    "PK": "PK", "PL": "PL", "PM": "PA", "PO": "PT", "PP": "PG", "PS": "PW", "PU": "GW", "QA": "QA",
    "RB": "RS", "RE": "RE", "RI": "RS", "RM": "MH", "RN": "MF", "RO": "RO", "RP": "PH", "RQ": "PR",
    "RS": "RU", "RW": "RW", "SA": "SA", "SB": "PM", "SC": "KN", "SE": "SC", "SF": "ZA", "SG": "SN",
    "SH": "SH", "SI": "SI", "SL": "SL", "SM": "SM", "SN": "SG", "SO": "SO", "SP": "ES", "ST": "LC",
    "SU": "SD", "SV": "SJ", "SW": "SE", "SX": "GS", "SY": "SY", "SZ": "CH", "TB": "BL", "TD": "TT",
    "TH": "TH", "TI": "TJ", "TK": "TC", "TL": "TK", "TN": "TO", "TO": "TG", "TP": "ST", "TS": "TN",
    "TT": "TL", "TU": "TR", "TV": "TV", "TW": "TW", "TX": "TM", "TZ": "TZ", "UC": "CW", "UG": "UG",
    "UK": "GB", "UP": "UA", "US": "US", "UV": "BF", "UY": "UY", "UZ": "UZ", "VC": "VC", "VE": "VE",
    "VI": "VG", "VM": "VN", "VQ": "VI", "VT": "VA", "WA": "NA", "WE": "PS", "WF": "WF", "WI": "EH",
    "WQ": "UM", "WS": "WS", "WZ": "SZ", "YI": "RS", "YM": "YE", "ZA": "ZM", "ZI": "ZW",
    "BQ": "UM", "DQ": "UM", "FQ": "UM", "HQ": "UM", "JQ": "UM", "KQ": "UM", "LQ": "UM", "MQ": "UM",
    "OS": None, "PF": None, "PG": None, "IP": None, "XX": None,
}


def fips_to_iso2(code: str | None) -> str | None:
    if not code:
        return None
    return FIPS_TO_ISO2.get(code.strip().upper())


def event_label(code: str | None, root: str | None = None) -> str:
    """Plain words for a CAMEO event code, falling back to the root code."""
    code = (code or "").strip()
    if code in CAMEO_EVENT:
        return CAMEO_EVENT[code]
    for n in (3, 2):
        if len(code) > n and code[:n] in CAMEO_EVENT:
            return CAMEO_EVENT[code[:n]]
    r = (root or code[:2]).strip()
    return CAMEO_ROOT.get(r, "Reported event")


def code_weight(code: str | None) -> int:
    return _CODE_WEIGHT.get((code or "").strip(), 0)


def actor_label(name: str | None, type_code: str | None, country_name: str | None = None) -> str | None:
    """'PAKISTAN' + None -> 'Pakistan'; 'TERRORIST' + 'UAF' -> 'Terrorist (armed group)'."""
    name = (name or "").strip()
    words = ACTOR_TYPE.get((type_code or "").strip().upper())
    if name:
        label = name.title()
        if words and words.lower() not in label.lower() and label.lower() not in words.lower():
            label = f"{label} ({words})"
        if country_name and country_name.lower() not in label.lower():
            label = f"{label}, {country_name}"
        return label
    if words:
        label = words[0].upper() + words[1:]
        return f"{label}, {country_name}" if country_name else label
    return country_name


def flag_emoji(iso2: str | None) -> str:
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        return ""
    iso2 = iso2.upper()
    if iso2 == "XK":  # Kosovo has no regional-indicator flag in most fonts
        return "\U0001F1FD\U0001F1F0"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso2)
