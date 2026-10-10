"""Small file logger. Writes everything to ~/.cache/screenwave/screenwave.log
and mirrors to stderr, so crashes become diagnosable.

Usable without gi: pure stdlib.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _log_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "screenwave" / "screenwave.log"


def setup_logging() -> logging.Logger:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("screenwave")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:  # idempotent
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler(path, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("logging to %s", path)
    return logger


def install_crash_hooks(logger: logging.Logger) -> None:
    """Log uncaught exceptions and (via faulthandler) native segfaults."""
    import faulthandler
    import threading

    def excepthook(exc_type, exc, tb):
        logger.critical("uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = excepthook
    threading.excepthook = lambda args: logger.critical(
        "uncaught thread exception", exc_info=args.exc_info
    )

    log_file = _log_path().open("a", encoding="utf-8")
    faulthandler.enable(file=log_file, all_threads=True)
    logger.info("crash hooks installed")