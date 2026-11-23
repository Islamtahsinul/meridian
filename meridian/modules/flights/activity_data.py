"""Reference data for the air activity detectors (activity.py): the regions watched for military
surges, aircraft roles by ICAO type designator, and the major airports whose movements are counted.

Regions are hand-drawn, simplified outlines ([lon, lat] rings, closed). They are areas of interest,
not borders or official airspace: an aircraft counts in every region whose outline contains it.
``hotspots`` names the GPS-sampler circles (const.HOTSPOTS) inside the region; their all-traffic
counts are the region's receiver-coverage check. Airports are major airports; ``radius_km`` is the
counting circle (smaller where another big airport is close).
"""

from __future__ import annotations

# id, name, ring ([lon, lat] ... closed), label point [lon, lat], GPS-sampler hotspots inside, and an
# optional "over" phrase for titles (default "over the <name>")
REGIONS = (
    {"id": "baltic", "name": "Baltic Sea",
     "ring": [[9.5, 53.6], [14.0, 53.6], [19.0, 54.2], [22.9, 54.4], [24.0, 56.0], [28.5, 57.8], [30.6, 59.6],
              [30.6, 61.0], [24.0, 61.6], [21.0, 63.8], [17.0, 62.6], [18.6, 60.0], [16.2, 56.0], [12.6, 55.9],
              [9.5, 55.0], [9.5, 53.6]],
     "label": [19.5, 57.2], "hotspots": ("kaliningrad", "gulf-of-finland")},
    {"id": "east_flank", "name": "Poland and the Belarus and Ukraine borders",
     "over": "over Poland and the Belarus and Ukraine borders",
     "ring": [[20.5, 49.0], [26.0, 49.0], [28.6, 51.5], [28.6, 54.5], [26.2, 55.6], [23.6, 54.3], [22.5, 53.2],
              [20.5, 52.0], [20.5, 49.0]],
     "label": [24.6, 52.0], "hotspots": ("belarus-border",)},
    {"id": "black_sea", "name": "Black Sea",
     "ring": [[27.3, 41.0], [29.5, 40.9], [33.0, 41.3], [36.5, 40.9], [41.8, 41.0], [42.0, 42.5], [40.0, 43.6],
              [38.0, 45.0], [39.6, 47.3], [35.0, 47.3], [33.5, 46.4], [31.0, 46.8], [29.5, 45.6], [28.2, 44.5],
              [27.3, 42.5], [27.3, 41.0]],
     "label": [34.5, 43.3], "hotspots": ("black-sea-west", "black-sea-east")},
    {"id": "east_med", "name": "Eastern Mediterranean",
     "ring": [[28.5, 30.6], [35.0, 29.4], [36.5, 31.0], [37.0, 33.5], [36.8, 35.8], [36.0, 37.0], [32.0, 36.6],
              [28.5, 36.2], [28.5, 30.6]],
     "label": [32.8, 33.6], "hotspots": ("east-med",)},
    {"id": "iraq_syria", "name": "Iraq and Syria", "over": "over Iraq and Syria",
     "ring": [[37.0, 33.5], [36.8, 35.8], [36.6, 37.2], [42.5, 37.4], [45.5, 36.5], [46.5, 33.0], [48.4, 30.2],
              [47.0, 29.0], [44.5, 29.2], [39.0, 32.0], [37.0, 33.5]],
     "label": [42.4, 33.6], "hotspots": ("iraq", "syria-turkey")},
    {"id": "persian_gulf", "name": "Persian Gulf and Strait of Hormuz",
     "ring": [[47.6, 30.5], [50.5, 30.2], [52.5, 28.0], [56.5, 27.3], [58.8, 25.6], [59.0, 23.0], [56.5, 23.6],
              [54.0, 24.0], [51.5, 23.8], [50.0, 25.0], [48.5, 27.5], [47.6, 29.0], [47.6, 30.5]],
     "label": [52.6, 26.4], "hotspots": ("persian-gulf", "hormuz")},
    {"id": "red_sea", "name": "Red Sea and Gulf of Aden",
     "ring": [[32.5, 29.9], [35.0, 28.0], [40.0, 21.0], [43.5, 13.0], [45.0, 12.8], [51.5, 12.0], [51.5, 10.8],
              [44.0, 10.4], [43.0, 11.5], [42.3, 12.5], [38.0, 17.5], [36.0, 22.0], [33.5, 27.0], [32.5, 29.9]],
     "label": [39.0, 19.0], "hotspots": ("red-sea",)},
    {"id": "north_sea", "name": "North Sea",
     "ring": [[1.0, 51.0], [3.0, 51.3], [5.0, 52.8], [8.5, 53.5], [8.5, 57.0], [7.0, 58.2], [5.0, 61.5],
              [-1.0, 61.5], [-2.8, 58.6], [-1.8, 56.5], [-0.5, 54.5], [0.5, 53.2], [1.8, 52.6], [1.0, 51.0]],
     "label": [3.2, 56.0], "hotspots": ()},
    {"id": "barents", "name": "Norwegian and Barents seas",
     "ring": [[5.0, 62.0], [12.0, 64.5], [16.0, 68.0], [24.0, 68.5], [30.0, 68.0], [41.0, 66.0], [45.0, 68.0],
              [45.0, 74.0], [20.0, 74.0], [5.0, 70.0], [0.0, 66.0], [5.0, 62.0]],
     "label": [22.0, 71.5], "hotspots": ("finnmark",)},
    {"id": "taiwan_strait", "name": "Taiwan Strait",
     "ring": [[117.0, 22.0], [121.0, 21.5], [122.6, 23.0], [122.6, 26.0], [120.5, 27.0], [118.5, 26.0],
              [117.0, 24.0], [117.0, 22.0]],
     "label": [119.8, 24.2], "hotspots": ()},
    {"id": "korea", "name": "Korean peninsula",
     "ring": [[124.0, 33.0], [130.0, 34.5], [131.5, 37.5], [130.5, 42.5], [129.0, 42.5], [124.0, 40.0],
              [124.0, 33.0]],
     "label": [127.6, 37.0], "hotspots": ("korea-dmz",)},
    {"id": "south_china_sea", "name": "South China Sea",
     "ring": [[105.5, 8.0], [109.0, 4.0], [114.0, 4.5], [119.5, 10.0], [120.5, 18.5], [117.0, 21.6],
              [111.0, 21.0], [108.0, 18.0], [109.5, 12.0], [105.5, 8.0]],
     "label": [114.0, 13.0], "hotspots": ()},
    {"id": "caribbean", "name": "Caribbean",
     "ring": [[-85.0, 21.5], [-80.0, 23.5], [-74.0, 22.0], [-68.0, 19.5], [-61.0, 18.5], [-59.5, 13.0],
              [-61.0, 9.8], [-72.0, 10.2], [-77.0, 8.5], [-83.5, 9.5], [-85.0, 14.0], [-85.0, 21.5]],
     "label": [-72.0, 15.5], "hotspots": ()},
)

# Roles from ICAO type designators (military aircraft only: the detector never looks at civil ones).
TANKER_TYPES = frozenset({"K35R", "K35E", "KC10", "KC46", "A332", "A310", "IL78"})
ISR_TYPES = frozenset({"R135", "C135", "E3TF", "E3CF", "E3", "E6", "E8", "E2", "E737", "P8", "P3", "IL38", "Q4",
                       "RQ4", "MQ9", "MQ4", "HRON", "U2"})
TRANSPORT_TYPES = frozenset({"C17", "C5", "C5M", "A400", "C130", "C30J", "C27J", "C160", "C295", "CN35", "IL76",
                             "A124", "AN12", "AN26", "AN30", "AN32", "Y8", "Y9", "C2", "B752", "B737", "B738",
                             "B739", "B742", "B748", "A319", "A320", "A321", "A333", "A339", "A359"})
FIGHTER_TYPES = frozenset({"F15", "F16", "F18", "F22", "F35", "FA18", "EUFI", "TORN", "RFAL", "A10", "SU27",
                           "SU30", "SU35", "MG29", "MG31", "MIR2", "F5", "F4", "F14", "JS39", "A4"})
BOMBER_TYPES = frozenset({"B1", "B2", "B52", "TU95", "T160", "TU22"})
TRAINER_TYPES = frozenset({"HAWK", "T38", "M346", "L39", "L159", "PC21", "PC9", "PC7", "T6", "TEX2", "T45",
                           "TUCA", "G120", "SR20", "DA40", "M339", "T34"})

ROLES = ("tanker", "isr", "transport", "fighter", "bomber", "other", "trainer", "helicopter")
COUNTED_ROLES = ("tanker", "isr", "transport", "fighter", "bomber", "other")
ROLE_LABELS = {"tanker": "Tankers", "isr": "Surveillance and patrol", "transport": "Transports",
               "fighter": "Fighters", "bomber": "Bombers", "other": "Other military", "trainer": "Trainers",
               "helicopter": "Helicopters"}
ROLE_CODES = {"tanker": "T", "isr": "S", "transport": "C", "fighter": "F", "bomber": "B", "other": "O",
              "trainer": "R", "helicopter": "H"}
CODE_ROLES = {v: k for k, v in ROLE_CODES.items()}

# Major airports: ICAO ident, IATA, name, lat, lon, ISO country, counting radius (km).
AIRPORTS = (
    ("EFHK", "HEL", "Helsinki", 60.3172, 24.9633, "FI", 40),
    ("EETN", "TLL", "Tallinn", 59.4133, 24.8328, "EE", 40),
    ("EVRA", "RIX", "Riga", 56.9236, 23.9711, "LV", 40),
    ("EYVI", "VNO", "Vilnius", 54.6341, 25.2858, "LT", 40),
    ("EPWA", "WAW", "Warsaw Chopin", 52.1657, 20.9671, "PL", 35),
    ("EPGD", "GDN", "Gdansk", 54.3776, 18.4662, "PL", 40),
    ("EPRZ", "RZE", "Rzeszow", 50.1100, 22.0190, "PL", 40),
    ("ULLI", "LED", "St Petersburg Pulkovo", 59.8003, 30.2625, "RU", 40),
    ("UMMS", "MSQ", "Minsk", 53.8825, 28.0325, "BY", 40),
    ("EKCH", "CPH", "Copenhagen", 55.6181, 12.6561, "DK", 40),
    ("ESSA", "ARN", "Stockholm Arlanda", 59.6519, 17.9186, "SE", 40),
    ("ENGM", "OSL", "Oslo", 60.1939, 11.1004, "NO", 40),
    ("LBWN", "VAR", "Varna", 43.2321, 27.8251, "BG", 35),
    ("LBBG", "BOJ", "Burgas", 42.5696, 27.5152, "BG", 35),
    ("LROP", "OTP", "Bucharest", 44.5711, 26.0850, "RO", 40),
    ("LUKK", "KIV", "Chisinau", 46.9277, 28.9310, "MD", 40),
    ("LTFM", "IST", "Istanbul", 41.2753, 28.7519, "TR", 35),
    ("UGTB", "TBS", "Tbilisi", 41.6692, 44.9547, "GE", 40),
    ("UDYZ", "EVN", "Yerevan", 40.1473, 44.3959, "AM", 40),
    ("LLBG", "TLV", "Tel Aviv Ben Gurion", 32.0114, 34.8867, "IL", 40),
    ("OLBA", "BEY", "Beirut", 33.8209, 35.4884, "LB", 35),
    ("LCLK", "LCA", "Larnaca", 34.8751, 33.6249, "CY", 40),
    ("LCPH", "PFO", "Paphos", 34.7180, 32.4857, "CY", 35),
    ("OJAI", "AMM", "Amman Queen Alia", 31.7226, 35.9932, "JO", 40),
    ("OSDI", "DAM", "Damascus", 33.4115, 36.5156, "SY", 40),
    ("ORBI", "BGW", "Baghdad", 33.2625, 44.2346, "IQ", 40),
    ("ORER", "EBL", "Erbil", 36.2376, 43.9632, "IQ", 40),
    ("OKKK", "KWI", "Kuwait", 29.2266, 47.9689, "KW", 40),
    ("OEDF", "DMM", "Dammam", 26.4712, 49.7979, "SA", 40),
    ("OBBI", "BAH", "Bahrain", 26.2708, 50.6336, "BH", 35),
    ("OTHH", "DOH", "Doha Hamad", 25.2731, 51.6081, "QA", 40),
    ("OMAA", "AUH", "Abu Dhabi", 24.4330, 54.6511, "AE", 35),
    ("OMDB", "DXB", "Dubai", 25.2528, 55.3644, "AE", 30),
    ("OOMS", "MCT", "Muscat", 23.5933, 58.2844, "OM", 40),
    ("RKSI", "ICN", "Seoul Incheon", 37.4602, 126.4407, "KR", 25),
    ("RKSS", "GMP", "Seoul Gimpo", 37.5583, 126.7906, "KR", 15),
    ("RCTP", "TPE", "Taipei Taoyuan", 25.0777, 121.2328, "TW", 30),
    ("RJTT", "HND", "Tokyo Haneda", 35.5523, 139.7797, "JP", 35),
    ("VHHH", "HKG", "Hong Kong", 22.3080, 113.9185, "HK", 35),
    ("WSSS", "SIN", "Singapore Changi", 1.3644, 103.9915, "SG", 35),
    ("EGLL", "LHR", "London Heathrow", 51.4706, -0.4619, "GB", 30),
    ("EHAM", "AMS", "Amsterdam Schiphol", 52.3086, 4.7639, "NL", 35),
    ("EBBR", "BRU", "Brussels", 50.9014, 4.4844, "BE", 35),
    ("EDDF", "FRA", "Frankfurt", 50.0333, 8.5706, "DE", 35),
    ("EDDM", "MUC", "Munich", 48.3538, 11.7861, "DE", 40),
    ("LFPG", "CDG", "Paris Charles de Gaulle", 49.0097, 2.5479, "FR", 30),
    ("KJFK", "JFK", "New York JFK", 40.6398, -73.7789, "US", 25),
    ("KIAD", "IAD", "Washington Dulles", 38.9445, -77.4558, "US", 30),
    ("KATL", "ATL", "Atlanta", 33.6367, -84.4281, "US", 40),
    ("KORD", "ORD", "Chicago O'Hare", 41.9786, -87.9048, "US", 30),
    ("KDFW", "DFW", "Dallas Fort Worth", 32.8968, -97.0380, "US", 35),
    ("KDEN", "DEN", "Denver", 39.8617, -104.6731, "US", 40),
    ("KLAX", "LAX", "Los Angeles", 33.9425, -118.4081, "US", 30),
    ("KSFO", "SFO", "San Francisco", 37.6188, -122.3756, "US", 25),
)
