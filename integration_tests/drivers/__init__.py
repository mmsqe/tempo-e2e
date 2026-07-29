"""Backend driver registry: pick with ``--backend <name>`` or ``$E2E_BACKEND``."""

from __future__ import annotations

from .allegro import AllegroDriver
from .tempo import TempoDriver

_DRIVERS = {"tempo": TempoDriver, "allegro": AllegroDriver}


def get_driver(name: str):
    if name not in _DRIVERS:
        raise RuntimeError(f"unknown backend {name!r}; known: {sorted(_DRIVERS)}")
    return _DRIVERS[name]()
