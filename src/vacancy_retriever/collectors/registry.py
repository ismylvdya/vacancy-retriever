from __future__ import annotations

import importlib
import pkgutil

from ..config import SourceConfig
from .base import Collector
from .http import PoliteHttpClient

_REGISTRY: dict[str, type[Collector]] = {}


def register[C: type[Collector]](cls: C) -> C:
    _REGISTRY[cls.type_name] = cls
    return cls


def _load_all() -> None:
    from . import email as _email  # noqa: F401
    from . import sites, telegram  # noqa: F401

    for module in pkgutil.iter_modules(sites.__path__):
        importlib.import_module(f"{sites.__name__}.{module.name}")


def collector_for(source: SourceConfig, http: PoliteHttpClient) -> Collector:
    if not _REGISTRY:
        _load_all()
    try:
        cls = _REGISTRY[source.type]
    except KeyError:
        raise KeyError(f"No collector for source type '{source.type}' (source '{source.id}')") from None
    return cls(source, http)


def known_types() -> list[str]:
    if not _REGISTRY:
        _load_all()
    return sorted(_REGISTRY)
