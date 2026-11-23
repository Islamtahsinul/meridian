"""State and provincial 511 sites on the shared IBI developer API (``/api/v2/get/cameras``). Each
needs a free developer key from its own site (register, then request a key on the site's
``/developers/doc`` page); without one the source is listed as needing that key.

Not here: FL511 (no public developer sign-up), 511PA (PennDOT data-feed agreement, not a key),
New England 511 (keys come from a separate partner portal), Nova Scotia 511 and the Saskatchewan
Highway Hotline (no developer page; Saskatchewan's public API was taken down)."""

from __future__ import annotations

from .na_common import ibi_source

_GEO = "the site blocks requests from outside the US, so it may only work from a US network"

SOURCES = [
    ibi_source("usnv511", "Nevada 511 cameras", "Nevada Department of Transportation (NVRoads)",
               "https://www.nvroads.com", "NV511_KEY", country="US", region="Nevada",
               hosts=("www.nvroads.com", "nvroads.com"), coverage="Nevada highways, Las Vegas and Reno"),
    ibi_source("usga511", "Georgia 511 cameras", "Georgia Department of Transportation (511GA)",
               "https://511ga.org", "GA511_KEY", country="US", region="Georgia",
               hosts=("511ga.org", "www.511ga.org"), coverage="Georgia interstates and metro Atlanta"),
    ibi_source("usaz511", "Arizona 511 cameras", "Arizona Department of Transportation (AZ511)",
               "https://az511.com", "AZ511_KEY", country="US", region="Arizona",
               hosts=("az511.com", "www.az511.com", "az511.gov", "www.az511.gov"),
               coverage="Arizona highways, Phoenix and Tucson"),
    ibi_source("usla511", "Louisiana 511 cameras", "Louisiana Department of Transportation and Development (511LA)",
               "https://511la.org", "LA511_KEY", country="US", region="Louisiana",
               hosts=("511la.org", "www.511la.org"), coverage="Louisiana interstates, New Orleans and Baton Rouge"),
    ibi_source("usid511", "Idaho 511 cameras", "Idaho Transportation Department (511 Idaho)",
               "https://511.idaho.gov", "ID511_KEY", country="US", region="Idaho",
               hosts=("511.idaho.gov",), coverage="Idaho highways and mountain passes", tags=("traffic", "weather")),
    ibi_source("usct511", "CTroads cameras", "Connecticut Department of Transportation (CTroads)",
               "https://ctroads.org", "CT511_KEY", country="US", region="Connecticut",
               hosts=("ctroads.org", "www.ctroads.org"), coverage="Connecticut interstates and highways"),
    ibi_source("usnc511", "DriveNC cameras", "North Carolina Department of Transportation (DriveNC)",
               "https://www.drivenc.gov", "NC511_KEY", country="US", region="North Carolina",
               hosts=("www.drivenc.gov", "drivenc.gov"), coverage="North Carolina interstates and highways"),
    ibi_source("usut511", "UDOT Traffic cameras", "Utah Department of Transportation (UDOT Traffic)",
               "https://www.udottraffic.utah.gov", "UT511_KEY", country="US", region="Utah",
               hosts=("www.udottraffic.utah.gov", "udottraffic.utah.gov"),
               coverage="Utah highways and canyons", note=_GEO, tags=("traffic", "weather")),
    ibi_source("uswi511", "511 Wisconsin cameras", "Wisconsin Department of Transportation (511 Wisconsin)",
               "https://511wi.gov", "WI511_KEY", country="US", region="Wisconsin",
               hosts=("511wi.gov", "www.511wi.gov"), coverage="Wisconsin interstates and highways", note=_GEO),
    ibi_source("camb511", "Manitoba 511 cameras", "Manitoba Transportation and Infrastructure (Manitoba 511)",
               "https://www.manitoba511.ca", "MB511_KEY", country="CA", region="Manitoba",
               hosts=("www.manitoba511.ca", "manitoba511.ca"), coverage="Manitoba highways", tags=("traffic", "weather")),
    ibi_source("canb511", "New Brunswick 511 cameras",
               "New Brunswick Department of Transportation and Infrastructure (New Brunswick 511)",
               "https://511.gnb.ca", "NB511_KEY", country="CA", region="New Brunswick",
               hosts=("511.gnb.ca",), coverage="New Brunswick highways", tags=("traffic", "weather")),
    ibi_source("cayt511", "511 Yukon cameras", "Government of Yukon, Highways and Public Works (511 Yukon)",
               "https://511yukon.ca", "YT511_KEY", country="CA", region="Yukon",
               hosts=("511yukon.ca", "www.511yukon.ca"), coverage="Yukon highways", tags=("traffic", "weather")),
]
