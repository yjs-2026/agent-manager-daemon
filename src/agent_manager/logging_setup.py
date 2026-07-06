"""Logging setup.

Single source of truth for the daemon's log format. Used by both the
WSGI entry point (gunicorn / `python -m agent_manager`) and by tests.
"""

from __future__ import annotations

import logging
import sys
from typing import Any


def configure_logging(level: str = "INFO", file: str = "", fmt: str | None = None) -> None:
    """Reset root logging to a deterministic shape.

    Tests and the daemon both call this. We *replace* handlers instead
    of adding to the root, otherwise duplicate log lines appear when
    pytest reloads modules.
    """
    if fmt is None:
        fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler: logging.Handler
    if file:
        handler = logging.FileHandler(file)
    else:
        handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(fmt))

    root.addHandler(handler)
    root.setLevel(level.upper())
    # Flask's werkzeug logger is chatty at INFO; quiet it down to WARNING
    # by default so request lines don't drown out our own messages.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


__all__: list[Any] = ["configure_logging", "get_logger"]