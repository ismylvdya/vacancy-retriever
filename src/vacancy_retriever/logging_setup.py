from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .paths import PATHS


def configure_logging(name: str, verbose: bool = False) -> None:
    PATHS.logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(PATHS.logs / f"{name}.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(formatter)
    root.addHandler(console)
    logging.getLogger("httpx").setLevel(logging.WARNING)
