"""Module discovery (CONTRACT.md section 5.1).

Every sub-package of ``meridian.modules`` with a ``register(m)`` function is a module. ``sanctions``
loads first (others use it); the rest load alphabetically. A module that fails to import or
register is reported in the manifest and status panel; it never stops the server.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import time
import traceback

log = logging.getLogger("meridian.modules")


def discover() -> list[str]:
    names = sorted(name for _, name, ispkg in pkgutil.iter_modules(__path__) if ispkg and not name.startswith("_"))
    if "sanctions" in names:
        names.remove("sanctions")
        names.insert(0, "sanctions")
    return names


def load_all(app, only: set[str] | None = None) -> None:
    for name in discover():
        if only and name not in only and name != "sanctions":
            continue
        ctx = app.module(name)
        info = app.modules[name]
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
            register = getattr(mod, "register", None)
            if register is None:
                raise RuntimeError("module has no register(m) function")
            register(ctx)
            info.ok = True
            info.loaded_at = time.time()
            info.description = (mod.__doc__ or "").strip().splitlines()[0] if mod.__doc__ else ""
        except Exception as err:
            info.ok = False
            info.error = app.config.redact(f"{type(err).__name__}: {err}")[:500]
            log.error("module %s failed to load: %s\n%s", name, info.error,
                      app.config.redact(traceback.format_exc()))
