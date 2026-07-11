"""Shared pure primitives for the Renault-platform add-ons.

A leaf seam: it imports nothing else in the package, so any module (the poll loop, charge
reconciliation, MQTT layer) can depend on it without a cycle. Deliberately scoped to
genuinely-shared, side-effect-free helpers — the wall clock (`now_ts`), epoch→ISO formatting
(`iso`), and numeric coercion (`_num`) — used by both the poll loop and the charge-session
reconciliation. Not a junk drawer: model-specific helpers (unit conversion, schedule formatting)
stay in each add-on's own modules.
"""
import time
from datetime import datetime, timezone


def now_ts():
    return time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None
