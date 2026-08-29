"""Logging setup -- rotating file handler plus console, per spec section 25."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from ..config import settings

FMT = "%(asctime)s  %(levelname)-7s  %(name)-34s  %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> None:
    settings.ensure_dirs()
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(settings.log_level.upper())

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(FMT, DATEFMT))
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        settings.log_dir / "app.log",
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(FMT, DATEFMT))
    root.addHandler(file_handler)

    # third-party noise
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
