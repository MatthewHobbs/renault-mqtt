"""Debug API-dump seam shared by the Renault-platform add-ons.

The one-shot `debug_dump` diagnostic: fetch the readable endpoints once per restart, redact
identifiers/secrets from the raw payloads, and log the lot at WARNING. Split out of the poll loop
so it stays lean and the redaction logic sits next to the dump it protects.

Imports only the config leaf (`cfg` + `_config_secrets` + the injected `ENV_PREFIX`), so it has no
cycle. `_debug_redact` here is the *structural* key/value scrubber for decoded API payloads —
distinct from config.redact, the substring scrubber for arbitrary log strings; the dump uses both
(config's secret list feeds this module's value masking).

Per-model variation: the `debug_dump` option name is read under config.ENV_PREFIX, and an add-on
may register extra per-model probes via EXTRA_SPECIALS (e.g. the Renault 5's raw `alerts` GET,
which the A290 does not expose). The no-arg method list and the redaction key-set are shared.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from renault_mqtt import config

LOG = logging.getLogger("renault_mqtt.debug")

# No-arg readable telemetry endpoints. Deliberately excludes get_location (GPS), get_contracts
# and get_notification_settings — those carry location / contact / account PII with no
# sensor-mapping diagnostic value. Includes ones a given model forbids (charge-mode, pressure,
# lock-status, res-state, hvac-history, hvac-sessions) so the dump documents the full
# supported/forbidden picture. Date-ranged endpoints (charges, charge-history) are probed
# separately below — they can't be called arg-less.
_DEBUG_METHODS = [
    "get_details", "get_car_adapter", "get_battery_status", "get_battery_soc", "get_cockpit",
    "get_hvac_status", "get_hvac_settings", "get_hvac_history", "get_hvac_sessions",
    "get_charge_schedule", "get_charge_mode", "get_charging_settings",
    "get_tyre_pressure", "get_lock_status", "get_res_state",
]
_DEBUG_RANGE_DAYS = 30
# Keys masked regardless of value type — identifiers / contact / location fields.
_DEBUG_REDACT_KEYS = {
    "registrationnumber", "vin", "tcucode", "radiocode", "siret", "msisdn", "phonenumber",
    "phone", "mobile", "email", "firstname", "lastname", "gigyaid", "personid", "accountid",
    "iccid", "imei", "contractid", "address", "postcode", "zipcode", "city", "country",
    "gpslatitude", "gpslongitude", "latitude", "longitude",
    # Vehicle-lifecycle / privacy / build-spec — quasi-identifying or owner-private. The
    # `assets` block carries 3dv.renault.com render URLs that embed the build-spec (VCD)
    # code in the path; mask the whole subtree (it has no sensor-mapping diagnostic value).
    "deliverydate", "firstregistrationdate", "vehicleid", "batterycode",
    "privacymode", "privacymodeupdatedate", "svtflag", "svtblockflag", "assets",
    # Defense-in-depth: token/credential field names. The endpoint allowlist + logger floor
    # already keep tokens out of the dump; this guards a future token-bearing payload too.
    "token", "accesstoken", "refreshtoken", "idtoken", "jwt", "authorization", "apikey",
    "secret", "password", "gigyacookievalue",
}
_DEBUG_STATE = {"dumped": False}

# Optional per-model probe hook. An add-on may set this to a callable
# `(vehicle, start, end) -> iterable of (name, present, call)` to add extra endpoints to the dump
# beyond the shared date-ranged ones (get_charges / get_charge_history) — e.g. the R5's raw
# `alerts` GET. `present` is the truthy capability check (falsy -> skipped); `call` is an
# awaitable factory. Left None here; the A290 registers nothing.
EXTRA_SPECIALS = None


def debug_enabled():
    # Fail closed if the add-on never injected its prefix: don't dump (a diagnostic dump on a
    # misconfigured install is the one that could leak). The config redaction net raises loudly
    # on the same misconfiguration, so this stays silent rather than crashing the poll loop.
    if config.ENV_PREFIX is None:
        return False
    return config.cfg(config.ENV_PREFIX + "DEBUG_DUMP", "false").strip().lower() in ("true", "1", "on")


def _debug_redact(obj, secrets):
    """Mask identifiers (by key, any value type) + configured secret values; keep telemetry."""
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in _DEBUG_REDACT_KEYS else _debug_redact(v, secrets))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_debug_redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            if s and s in obj:
                obj = obj.replace(s, "***")
        return obj
    if any(s and s == str(obj) for s in secrets):   # secret value held as a number (e.g. id)
        return "***"
    return obj


async def _dump_one(out, name, call, secrets):
    """Run one debug probe, redact its raw payload, store the result; never fatal. Handles
    dict, list (e.g. charges returns a list of sessions), and raw_data-bearing objects — a
    list must be parsed, not str()'d, or key-based GPS/id redaction is skipped."""
    try:
        res = await call()
        if isinstance(res, dict):
            raw = res
        elif isinstance(res, list):
            raw = [getattr(x, "raw_data", x) for x in res]
        else:
            raw = getattr(res, "raw_data", None) or {"_repr": str(res)}
        out[name] = _debug_redact(raw, secrets)
    except Exception as err:  # noqa: BLE001
        out[name] = {"_error": f"{type(err).__name__}: {err}"}


async def dump_api(vehicle):
    """DEBUG: fetch every readable endpoint, redact IDs/secrets, log the lot. Never fatal."""
    secrets = config._config_secrets()
    out = {}
    for meth in _DEBUG_METHODS:
        fn = getattr(vehicle, meth, None)
        if fn is not None:
            await _dump_one(out, meth, lambda _f=fn: _f(), secrets)
    # Date-ranged endpoints can't be called arg-less; probe the last N days. Any per-model extras
    # (EXTRA_SPECIALS) are appended and gated on their own capability check the same way.
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=_DEBUG_RANGE_DAYS)
    specials = [
        ("get_charges", getattr(vehicle, "get_charges", None),
         lambda: vehicle.get_charges(start, end)),
        ("get_charge_history", getattr(vehicle, "get_charge_history", None),
         lambda: vehicle.get_charge_history(start, end, "month")),
    ]
    if EXTRA_SPECIALS is not None:
        specials += list(EXTRA_SPECIALS(vehicle, start, end))
    for name, present, call in specials:
        if present:
            await _dump_one(out, name, call, secrets)
    LOG.warning("API DEBUG DUMP — may contain personal data; redaction is best-effort, do NOT "
                "paste publicly. One-shot per restart; turn debug_dump off when done.\n%s",
                json.dumps(out, indent=2, default=str, ensure_ascii=False))


async def maybe_dump_api(vehicle):
    """Run the debug dump once per restart when debug_dump is on (not every poll)."""
    if debug_enabled() and not _DEBUG_STATE["dumped"]:
        _DEBUG_STATE["dumped"] = True
        await dump_api(vehicle)
