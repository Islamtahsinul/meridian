"""Every camera source, in priority order (earlier sources win when two list the same image).

New adapters only have to exist: any module in this package with ``SOURCE`` (or a ``SOURCES``
list) is picked up after the curated list below. A module that fails to import is skipped and
listed in ``BROKEN`` (and logged by the module), so one unfinished adapter never takes the camera
layer down.
"""

from __future__ import annotations

from ..common import Source
from . import (alaska, alertca, autobahn, caltrans, cities, dgt, digitraffic, drivebc, hongkong, ibi511, iceland,
               illinois, iowa, nyc, nzta, singapore, tfl, windy)

ALL: list[Source] = [
    tfl.SOURCE,
    caltrans.SOURCE,
    iowa.SOURCE,
    illinois.SOURCE,
    illinois.LAKE_COUNTY,
    dgt.SOURCE,
    digitraffic.SOURCE,
    hongkong.SOURCE,
    drivebc.SOURCE,
    alertca.SOURCE,
    iceland.SOURCE,
    alaska.SOURCE,
    nzta.SOURCE,
    cities.YORK,
    cities.AUSTIN,
    cities.CALGARY,
    cities.NEWFOUNDLAND,
    singapore.SOURCE,
    autobahn.SOURCE,
    nyc.SOURCE,
    windy.SOURCE,
    ibi511.ONTARIO,
    ibi511.ALBERTA,
    ibi511.NEW_YORK,
]

BROKEN: list[tuple[str, str]] = []      # (module name, error) for adapter files that failed to import


def _discover() -> None:
    """Add sources from any other module in this package (SOURCE, or a SOURCES list), so a new adapter
    file only has to exist to be picked up. The curated order above wins for duplicates."""
    import importlib
    import pkgutil
    known = {s.id for s in ALL}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as err:  # an adapter being written right now must not break the others
            BROKEN.append((info.name, f"{type(err).__name__}: {err}"[:300]))
            continue
        found = list(getattr(mod, "SOURCES", []) or [])
        if getattr(mod, "SOURCE", None) is not None:
            found.insert(0, mod.SOURCE)
        for src in found:
            if isinstance(src, Source) and src.id not in known:
                ALL.append(src)
                known.add(src.id)


_discover()

BY_ID: dict[str, Source] = {s.id: s for s in ALL}


def bind_config(config) -> None:
    """Sources whose image lookup needs configuration (keys) get it here."""
    windy.SOURCE.direct = windy.make_direct(config)
