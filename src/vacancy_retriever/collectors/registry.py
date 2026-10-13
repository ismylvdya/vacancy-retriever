from __future__ import annotations

import importlib
import pkgutil

from ..config import SourceConfig
from .base import Collector
from .http import PoliteHttpClient

_REGISTRY: dict[str, type[Collector]] = {}
_loaded = False


def register[C: type[Collector]](cls: C) -> C:
    _REGISTRY[cls.type_name] = cls
    return cls


def _load_all() -> None:
    global _loaded
    if _loaded:
        return
    from . import email, sites, telegram  # noqa: F401

    for package in (sites, email):
        for module in pkgutil.iter_modules(package.__path__):
            importlib.import_module(f"{package.__name__}.{module.name}")
    _loaded = True


def collector_for(source: SourceConfig, http: PoliteHttpClient) -> Collector:
    _load_all()
    try:
        cls = _REGISTRY[source.type]
    except KeyError:
        raise KeyError(f"No collector for source type '{source.type}' (source '{source.id}')") from None
    return cls(source, http)


def known_types() -> list[str]:
    _load_all()
    return sorted(_REGISTRY)
