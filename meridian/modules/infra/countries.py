"""ISO 3166-1 country codes (alpha-2, alpha-3, short English name) plus the name variants the
infrastructure sources use (GPPD, TeleGeography, World Bank style "Congo, Dem. Rep.", ...).

Facts only (country codes and names); no third-party data is copied here.
"""

from __future__ import annotations

import re
import unicodedata

_TABLE = """
AD AND Andorra
AE ARE United Arab Emirates
AF AFG Afghanistan
AG ATG Antigua and Barbuda
AI AIA Anguilla
AL ALB Albania
AM ARM Armenia
AO AGO Angola
AQ ATA Antarctica
AR ARG Argentina
AS ASM American Samoa
AT AUT Austria
AU AUS Australia
AW ABW Aruba
AX ALA Aland Islands
AZ AZE Azerbaijan
BA BIH Bosnia and Herzegovina
BB BRB Barbados
BD BGD Bangladesh
BE BEL Belgium
BF BFA Burkina Faso
BG BGR Bulgaria
BH BHR Bahrain
BI BDI Burundi
BJ BEN Benin
BL BLM Saint Barthelemy
BM BMU Bermuda
BN BRN Brunei
BO BOL Bolivia
BQ BES Caribbean Netherlands
BR BRA Brazil
BS BHS Bahamas
BT BTN Bhutan
BV BVT Bouvet Island
BW BWA Botswana
BY BLR Belarus
BZ BLZ Belize
CA CAN Canada
CC CCK Cocos (Keeling) Islands
CD COD Democratic Republic of the Congo
CF CAF Central African Republic
CG COG Republic of the Congo
CH CHE Switzerland
CI CIV Cote d'Ivoire
CK COK Cook Islands
CL CHL Chile
CM CMR Cameroon
CN CHN China
CO COL Colombia
CR CRI Costa Rica
CU CUB Cuba
CV CPV Cape Verde
CW CUW Curacao
CX CXR Christmas Island
CY CYP Cyprus
CZ CZE Czechia
DE DEU Germany
DJ DJI Djibouti
DK DNK Denmark
DM DMA Dominica
DO DOM Dominican Republic
DZ DZA Algeria
EC ECU Ecuador
EE EST Estonia
EG EGY Egypt
EH ESH Western Sahara
ER ERI Eritrea
ES ESP Spain
ET ETH Ethiopia
FI FIN Finland
FJ FJI Fiji
FK FLK Falkland Islands
FM FSM Micronesia
FO FRO Faroe Islands
FR FRA France
GA GAB Gabon
GB GBR United Kingdom
GD GRD Grenada
GE GEO Georgia
GF GUF French Guiana
GG GGY Guernsey
GH GHA Ghana
GI GIB Gibraltar
GL GRL Greenland
GM GMB Gambia
GN GIN Guinea
GP GLP Guadeloupe
GQ GNQ Equatorial Guinea
GR GRC Greece
GS SGS South Georgia and the South Sandwich Islands
GT GTM Guatemala
GU GUM Guam
GW GNB Guinea-Bissau
GY GUY Guyana
HK HKG Hong Kong
HM HMD Heard Island and McDonald Islands
HN HND Honduras
HR HRV Croatia
HT HTI Haiti
HU HUN Hungary
ID IDN Indonesia
IE IRL Ireland
IL ISR Israel
IM IMN Isle of Man
IN IND India
IO IOT British Indian Ocean Territory
IQ IRQ Iraq
IR IRN Iran
IS ISL Iceland
IT ITA Italy
JE JEY Jersey
JM JAM Jamaica
JO JOR Jordan
JP JPN Japan
KE KEN Kenya
KG KGZ Kyrgyzstan
KH KHM Cambodia
KI KIR Kiribati
KM COM Comoros
KN KNA Saint Kitts and Nevis
KP PRK North Korea
KR KOR South Korea
KW KWT Kuwait
KY CYM Cayman Islands
KZ KAZ Kazakhstan
LA LAO Laos
LB LBN Lebanon
LC LCA Saint Lucia
LI LIE Liechtenstein
LK LKA Sri Lanka
LR LBR Liberia
LS LSO Lesotho
LT LTU Lithuania
LU LUX Luxembourg
LV LVA Latvia
LY LBY Libya
MA MAR Morocco
MC MCO Monaco
MD MDA Moldova
ME MNE Montenegro
MF MAF Saint Martin
MG MDG Madagascar
MH MHL Marshall Islands
MK MKD North Macedonia
ML MLI Mali
MM MMR Myanmar
MN MNG Mongolia
MO MAC Macao
MP MNP Northern Mariana Islands
MQ MTQ Martinique
MR MRT Mauritania
MS MSR Montserrat
MT MLT Malta
MU MUS Mauritius
MV MDV Maldives
MW MWI Malawi
MX MEX Mexico
MY MYS Malaysia
MZ MOZ Mozambique
NA NAM Namibia
NC NCL New Caledonia
NE NER Niger
NF NFK Norfolk Island
NG NGA Nigeria
NI NIC Nicaragua
NL NLD Netherlands
NO NOR Norway
NP NPL Nepal
NR NRU Nauru
NU NIU Niue
NZ NZL New Zealand
OM OMN Oman
PA PAN Panama
PE PER Peru
PF PYF French Polynesia
PG PNG Papua New Guinea
PH PHL Philippines
PK PAK Pakistan
PL POL Poland
PM SPM Saint Pierre and Miquelon
PN PCN Pitcairn Islands
PR PRI Puerto Rico
PS PSE Palestine
PT PRT Portugal
PW PLW Palau
PY PRY Paraguay
QA QAT Qatar
RE REU Reunion
RO ROU Romania
RS SRB Serbia
RU RUS Russia
RW RWA Rwanda
SA SAU Saudi Arabia
SB SLB Solomon Islands
SC SYC Seychelles
SD SDN Sudan
SE SWE Sweden
SG SGP Singapore
SH SHN Saint Helena, Ascension and Tristan da Cunha
SI SVN Slovenia
SJ SJM Svalbard and Jan Mayen
SK SVK Slovakia
SL SLE Sierra Leone
SM SMR San Marino
SN SEN Senegal
SO SOM Somalia
SR SUR Suriname
SS SSD South Sudan
ST STP Sao Tome and Principe
SV SLV El Salvador
SX SXM Sint Maarten
SY SYR Syria
SZ SWZ Eswatini
TC TCA Turks and Caicos Islands
TD TCD Chad
TF ATF French Southern Territories
TG TGO Togo
TH THA Thailand
TJ TJK Tajikistan
TK TKL Tokelau
TL TLS Timor-Leste
TM TKM Turkmenistan
TN TUN Tunisia
TO TON Tonga
TR TUR Turkey
TT TTO Trinidad and Tobago
TV TUV Tuvalu
TW TWN Taiwan
TZ TZA Tanzania
UA UKR Ukraine
UG UGA Uganda
UM UMI United States Minor Outlying Islands
US USA United States
UY URY Uruguay
UZ UZB Uzbekistan
VA VAT Vatican City
VC VCT Saint Vincent and the Grenadines
VE VEN Venezuela
VG VGB British Virgin Islands
VI VIR US Virgin Islands
VN VNM Vietnam
VU VUT Vanuatu
WF WLF Wallis and Futuna
WS WSM Samoa
XK XKX Kosovo
YE YEM Yemen
YT MYT Mayotte
ZA ZAF South Africa
ZM ZMB Zambia
ZW ZWE Zimbabwe
"""

# Name variants used by the sources, normalised with norm_name() before lookup.
_ALIASES = {
    "AE": ["UAE"],
    "BA": ["Bosnia"],
    "BN": ["Brunei Darussalam"],
    "BO": ["Bolivia (Plurinational State of)"],
    "BQ": ["Bonaire, Sint Eustatius and Saba", "Sint Eustatius and Saba", "Bonaire", "Bonaire Sint Eustatius and Saba"],
    "BS": ["The Bahamas", "Bahamas, The"],
    "CD": ["Congo, Dem. Rep.", "DR Congo", "Congo (Kinshasa)", "Democratic Republic of Congo", "Congo, Democratic Republic of the", "Zaire"],
    "CG": ["Congo, Rep.", "Congo", "Congo (Brazzaville)", "Republic of Congo"],
    "CI": ["Cote DIvoire", "Ivory Coast", "Cote d Ivoire"],
    "CV": ["Cabo Verde"],
    "CZ": ["Czech Republic"],
    "EG": ["Egypt, Arab Rep."],
    "FM": ["Micronesia, Fed. Sts.", "Federated States of Micronesia", "Micronesia (Federated States of)"],
    "GB": ["UK", "Great Britain", "United Kingdom of Great Britain and Northern Ireland", "England", "Scotland", "Wales", "Northern Ireland"],
    "GM": ["The Gambia", "Gambia, The"],
    "HK": ["Hong Kong SAR, China", "Hong Kong SAR", "Hong Kong, China"],
    "IR": ["Iran, Islamic Rep.", "Islamic Republic of Iran", "Iran (Islamic Republic of)"],
    "KG": ["Kyrgyz Republic"],
    "KN": ["St. Kitts and Nevis", "St Kitts and Nevis"],
    "KP": ["Korea, Dem. People's Rep.", "Korea, Dem. Rep.", "Democratic People's Republic of Korea", "Korea (North)"],
    "KR": ["Korea, Rep.", "Korea", "Republic of Korea", "Korea (South)"],
    "LA": ["Lao PDR", "Lao People's Democratic Republic"],
    "LC": ["St. Lucia", "St Lucia"],
    "MD": ["Republic of Moldova", "Moldova, Republic of"],
    "MF": ["Saint Martin (French part)", "St. Martin (French part)"],
    "MK": ["Macedonia", "Republic of North Macedonia", "Macedonia, FYR"],
    "MM": ["Burma"],
    "MO": ["Macau", "Macao SAR, China", "Macao SAR"],
    "PS": ["West Bank and Gaza", "Palestinian Territories", "State of Palestine", "Palestine, State of"],
    "RE": ["Reunion Island"],
    "RU": ["Russian Federation"],
    "SH": ["Ascension and Tristan da Cunha", "Saint Helena", "St. Helena", "Ascension Island", "Tristan da Cunha"],
    "SK": ["Slovak Republic"],
    "SX": ["Sint Maarten (Dutch part)"],
    "SY": ["Syrian Arab Republic"],
    "SZ": ["Swaziland"],
    "TL": ["East Timor"],
    "TR": ["Turkiye", "Republic of Turkey"],
    "TW": ["Taiwan, China", "Chinese Taipei", "Republic of China"],
    "TZ": ["United Republic of Tanzania", "Tanzania, United Republic of"],
    "US": ["United States of America", "USA", "U.S.", "U.S.A."],
    "VA": ["Holy See", "Vatican"],
    "VC": ["St. Vincent and the Grenadines", "St Vincent and the Grenadines"],
    "VE": ["Venezuela, RB", "Venezuela (Bolivarian Republic of)"],
    "VG": ["Virgin Islands (U.K.)", "Virgin Islands, British", "British Virgin Is."],
    "VI": ["Virgin Islands (U.S.)", "Virgin Islands, U.S.", "United States Virgin Islands", "U.S. Virgin Islands"],
    "VN": ["Viet Nam"],
    "XK": ["Kosova"],
    "YE": ["Yemen, Rep."],
}

# Alpha-3 codes some sources use that are not ISO (GPPD uses KOS for Kosovo).
_EXTRA_ALPHA3 = {"KOS": "XK", "XKX": "XK", "ROM": "RO", "ZAR": "CD", "TMP": "TL"}


def norm_name(text: str | None) -> str:
    """Case-, accent- and punctuation-insensitive form of a country name."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("&", " and ").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"^the ", "", s.strip())
    return s.strip()


ALPHA2: dict[str, str] = {}      # alpha-2 -> name
ALPHA3: dict[str, str] = {}      # alpha-3 -> alpha-2
_BY_NAME: dict[str, str] = {}    # normalised name -> alpha-2

for _line in _TABLE.strip().splitlines():
    _a2, _a3, _name = _line.split(" ", 2)
    ALPHA2[_a2] = _name
    ALPHA3[_a3] = _a2
    _BY_NAME[norm_name(_name)] = _a2
for _a2, _names in _ALIASES.items():
    for _n in _names:
        _BY_NAME.setdefault(norm_name(_n), _a2)
for _a3, _a2 in _EXTRA_ALPHA3.items():
    ALPHA3.setdefault(_a3, _a2)


def name(iso2: str | None) -> str | None:
    """Short English name for an alpha-2 code (None when unknown)."""
    return ALPHA2.get((iso2 or "").upper())


def from_alpha3(code: str | None) -> str | None:
    return ALPHA3.get((code or "").strip().upper())


def from_name(text: str | None) -> str | None:
    """Alpha-2 code for a country name or alias (None when unknown)."""
    key = norm_name(text)
    if not key:
        return None
    return _BY_NAME.get(key)


def resolve(value: str | None) -> str | None:
    """Alpha-2 from an alpha-2 code, alpha-3 code or a name."""
    text = (value or "").strip()
    if not text:
        return None
    up = text.upper()
    if len(up) == 2 and up in ALPHA2:
        return up
    if len(up) == 3 and up in ALPHA3:
        return ALPHA3[up]
    return from_name(text)


def split_place(text: str) -> tuple[str, str | None]:
    """Split "City, [Region,] Country" into (place, alpha-2) using the longest known country suffix.

    "Muanda, Congo, Dem. Rep." -> ("Muanda", "CD"); "Pacific City, OR, United States" ->
    ("Pacific City, OR", "US"). Returns (text, None) when no suffix is a known country.
    """
    parts = [p.strip() for p in str(text or "").split(",")]
    for take in (3, 2, 1):
        if len(parts) > take:
            code = from_name(", ".join(parts[-take:]))
            if code:
                return ", ".join(parts[:-take]), code
    if len(parts) == 1:
        code = from_name(parts[0])
        if code:
            return "", code
    return str(text or ""), None
