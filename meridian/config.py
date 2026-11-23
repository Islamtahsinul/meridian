"""Configuration: the `.env` file plus environment variables (CONTRACT.md section 4).

Secrets stay on the server. The browser only ever learns *whether* a key is set, never its value.
Modules declare the keys they understand with ``m.key(...)`` so the Settings panel can list them.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
ENV_FILE = ROOT / ".env"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790


def parse_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines; ``#`` comments, blank lines and an optional ``export`` prefix allowed."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, IsADirectoryError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


class Config:
    """Read-only view of settings. Environment variables win over ``.env`` values."""

    def __init__(self, env_file: Path | None = ENV_FILE, overrides: dict[str, str] | None = None):
        self._lock = threading.Lock()
        self.env_file = env_file
        self._file_values = parse_env_file(env_file) if env_file else {}
        self._overrides = dict(overrides or {})
        self.declared: dict[str, dict] = {}

    # -- reading -------------------------------------------------------------------------------
    def get(self, name: str, default: str | None = None) -> str | None:
        if name in self._overrides:
            value = self._overrides[name]
        else:
            value = os.environ.get(name)
            if value is None or value.strip() == "":
                value = self._file_values.get(name)
        if value is None or str(value).strip() == "":
            return default
        return str(value).strip()

    def get_int(self, name: str, default: int) -> int:
        try:
            return int(self.get(name, str(default)))
        except (TypeError, ValueError):
            return default

    def get_float(self, name: str, default: float) -> float:
        try:
            return float(self.get(name, str(default)))
        except (TypeError, ValueError):
            return default

    def get_bool(self, name: str, default: bool = False) -> bool:
        value = self.get(name)
        if value is None:
            return default
        return value.lower() in ("1", "true", "yes", "on")

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    # -- declared keys -------------------------------------------------------------------------
    def declare(self, name: str, *, description: str, url: str = "", module: str = "",
                secret: bool = True) -> None:
        with self._lock:
            self.declared[name] = {"name": name, "description": description, "url": url,
                                   "module": module, "secret": bool(secret)}

    def key_status(self) -> list[dict]:
        """Declared keys and settings for the Settings panel. Secret values are never included."""
        out = []
        with self._lock:
            items = sorted(self.declared.values(), key=lambda d: (d["module"], d["name"]))
        for item in items:
            row = dict(item)
            row["configured"] = self.has(item["name"])
            if not item["secret"]:
                row["value"] = self.get(item["name"])
            out.append(row)
        return out

    def secret_values(self) -> list[str]:
        names = set(self.declared)
        names.update(n for n in list(self._file_values) if _looks_secret(n))
        values = []
        for name in names:
            meta = self.declared.get(name)
            if meta is not None and not meta["secret"]:
                continue
            value = self.get(name)
            if value and len(value) >= 6:
                values.append(value)
        return sorted(set(values), key=len, reverse=True)

    def redact(self, text: str) -> str:
        """Replace every secret value, plain or URL-encoded, with ***."""
        if not text:
            return text
        from urllib.parse import quote, quote_plus
        for value in self.secret_values():
            for form in {value, quote(value, safe=""), quote_plus(value), quote(value)}:
                if form and form in text:
                    text = text.replace(form, "***")
        return text

    # -- paths ---------------------------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        custom = self.get("MERIDIAN_DATA_DIR")
        return Path(custom).expanduser().resolve() if custom else ROOT / "data"


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(part in upper for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
