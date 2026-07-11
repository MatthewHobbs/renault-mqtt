"""Shared core for the Renault-platform Home Assistant add-ons (Alpine A290, Renault 5, …).

Each add-on is a thin, per-model shim (its own ``catalog.py`` + wiring) over this package: the
poll loop, MQTT discovery, charge-session reconciliation, debug-dump redaction and the pure
primitives live here once, parameterised by the add-on's catalog, instead of being hand-mirrored
between the sibling repos. See the ``renault-mqtt`` repo README for the extraction plan.
"""

__version__ = "0.12.0"
