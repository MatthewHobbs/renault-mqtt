"""Config reading + secret-redaction primitives shared by the Renault-platform add-ons.

A leaf seam: it imports nothing else in the package, so every other module (main, debug, mqtt,
charge) can depend on it without an import cycle. It holds add-on option reading (`cfg`,
`_opt_flag`) and the secret-redaction net (`redact` / `_RedactingFilter` / `_config_secrets`)
plus the auto-discovered account id those mask.

The only per-model variation is the environment-variable prefix the add-on exports its options
under (`A290_` for the Alpine A290, `R5_` for the Renault 5). The add-on injects it by setting
``config.ENV_PREFIX`` at startup (from its ``catalog.ENV_PREFIX``) — everything else here is
identical between models. `_config_secrets` reads the prefixed names lazily, so the prefix only
has to be set before the first redaction (the add-on wires it at import time).
"""
import logging
import os

# The add-on's environment-variable prefix (e.g. "A290_" / "R5_"), injected by the add-on at
# startup via `config.ENV_PREFIX = catalog.ENV_PREFIX`. Defaults to None (NOT ""): the redaction
# net FAILS CLOSED — if an add-on forgets to inject it, _config_secrets raises loudly at the first
# redaction (startup, caught by CI's boot) rather than silently reading unprefixed names and
# leaving the configured VIN / account_id / username / password unredacted. A correctly-wired
# add-on sets it before logging is configured, so this never fires in production.
ENV_PREFIX = None

# The account id auto-discovered by resolve_account() when <PREFIX>ACCOUNT_ID is left blank. Held
# here so redact() can mask it in error strings (the Kamereon URL embeds it) even though it was
# never a configured value. The add-on's resolve_account() sets it via `config._DISCOVERED_ACCOUNT_ID`.
_DISCOVERED_ACCOUNT_ID = None


def cfg(name, default=""):
    return os.environ.get(name, default)


def _opt_flag(name, default):
    """Read a boolean add-on option, tolerating bashio exporting '', 'null', or unset on an
    upgraded install (in which case the default applies)."""
    v = os.environ.get(name)
    if v is None or v.strip().lower() in ("", "null"):
        return default
    return v.strip().lower() in ("true", "1", "on")


def _config_secrets():
    """The sensitive values to scrub from anything logged or served: VIN, account_id (the
    configured one AND the auto-discovered one — users are told to leave account_id blank, so
    the discovered value is the common case), username, password, and the Supervisor token.
    The Kamereon request URL embeds the VIN + account_id, so an aiohttp error string (which
    includes the URL) carries them — see redact(). Read under ENV_PREFIX so a single injected
    prefix covers every configured option name. Fails closed if the prefix was never injected."""
    if ENV_PREFIX is None:
        raise RuntimeError(
            "renault_mqtt.config.ENV_PREFIX is not set — the add-on must set it "
            "(e.g. `config.ENV_PREFIX = catalog.ENV_PREFIX`) before any logging/redaction, "
            "or configured secrets would not be redacted.")
    return [v for v in (cfg(ENV_PREFIX + "VIN"), cfg(ENV_PREFIX + "ACCOUNT_ID"), _DISCOVERED_ACCOUNT_ID,
                        cfg(ENV_PREFIX + "USERNAME"), cfg(ENV_PREFIX + "PASSWORD"),
                        os.environ.get("SUPERVISOR_TOKEN")) if v]


def redact(text):
    """Mask the configured secrets in an arbitrary string before it is logged or placed in the
    status-panel snapshot. API/HTTP error strings embed the request URL (…/accounts/<account_id>
    /vehicles/<vin>/…), so an ordinary transient failure would otherwise leak the VIN/account_id
    to the container log and to GET /api/state. Best-effort substring masking."""
    s = str(text)
    for secret in _config_secrets():
        if secret and secret in s:
            s = s.replace(secret, "***")
    return s


class _RedactingFilter(logging.Filter):
    """Redacts configured secrets from EVERY log record — ours and the renault-api library's —
    at the root handler. A central net so no current or future logging path (any of the
    per-endpoint poll warnings, a library line that prints a request URL, etc.) can leak the
    VIN / account id / token embedded in an API URL. Complements the explicit redact() at the
    error/snapshot paths; idempotent, so double-redaction is harmless."""

    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True
