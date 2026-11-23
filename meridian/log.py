"""Logging with secret redaction. Every handler formats through :class:`RedactingFormatter`."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


class RedactingFormatter(logging.Formatter):
    def __init__(self, config, fmt: str):
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")
        self._config = config

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        try:
            return self._config.redact(text)
        except Exception:  # never let redaction break logging
            return text


def setup(config, log_dir: Path, *, level: int = logging.INFO, console: bool = True,
          filename: str = "meridian.log") -> logging.Logger:
    root = logging.getLogger("meridian")
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / filename, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if level <= logging.DEBUG else logging.INFO)
    file_handler.setFormatter(RedactingFormatter(config, fmt))
    root.addHandler(file_handler)
    if console:
        try:
            sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
            sys.stderr.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(level)
        stream.setFormatter(RedactingFormatter(config, "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(stream)
    root.propagate = False
    return root
