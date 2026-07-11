"""Tests for the config seam: add-on option parsing (`cfg`/`_opt_flag`) and the
secret-redaction primitives (`redact` / `_RedactingFilter` / `_config_secrets`).

Written model-agnostically: a test ENV_PREFIX is injected (as each add-on injects its own
`A290_` / `R5_`) so the package carries no per-model assumption. monkeypatch restores both the
prefix and the discovered-account global after every test.
"""
import logging

import pytest

from renault_mqtt import config


@pytest.fixture(autouse=True)
def _prefix(monkeypatch):
    """Arm the redaction net with a test prefix, as the add-on does at startup."""
    monkeypatch.setattr(config, "ENV_PREFIX", "TEST_")


def test_config_secrets_fails_closed_when_prefix_unset(monkeypatch):
    # Fail closed: if an add-on forgets to inject ENV_PREFIX, redaction must raise loudly rather
    # than silently read unprefixed names and leave configured secrets unredacted.
    monkeypatch.setattr(config, "ENV_PREFIX", None)
    with pytest.raises(RuntimeError, match="ENV_PREFIX is not set"):
        config._config_secrets()
    with pytest.raises(RuntimeError, match="ENV_PREFIX is not set"):
        config.redact("any string reaches _config_secrets")


def test_cfg_reads_env_and_defaults(monkeypatch):
    monkeypatch.setenv("TEST_THING", "value")
    assert config.cfg("TEST_THING") == "value"
    assert config.cfg("TEST_MISSING") == ""                 # default default is ""
    assert config.cfg("TEST_MISSING", "fallback") == "fallback"


def test_opt_flag_parses_values_and_defaults(monkeypatch):
    monkeypatch.setenv("TEST_FLAG", "true")
    assert config._opt_flag("TEST_FLAG", False) is True
    for v in ("false", "0", "off"):
        monkeypatch.setenv("TEST_FLAG", v)
        assert config._opt_flag("TEST_FLAG", True) is False
    for v in ("", "null", "  "):          # bashio can export these on an upgraded install
        monkeypatch.setenv("TEST_FLAG", v)
        assert config._opt_flag("TEST_FLAG", True) is True     # -> default
    monkeypatch.delenv("TEST_FLAG", raising=False)
    assert config._opt_flag("TEST_FLAG", True) is True         # unset -> default


def test_config_secrets_honours_injected_prefix(monkeypatch):
    monkeypatch.setenv("TEST_VIN", "VF1AAAABBBB12345")
    monkeypatch.setenv("TEST_USERNAME", "me@example.com")
    # a stray same-named var under a DIFFERENT prefix must NOT be picked up
    monkeypatch.setenv("OTHER_VIN", "should-not-appear")
    secrets = config._config_secrets()
    assert "VF1AAAABBBB12345" in secrets and "me@example.com" in secrets
    assert "should-not-appear" not in secrets


def test_redact_masks_configured_secrets(monkeypatch):
    monkeypatch.setenv("TEST_VIN", "VF1AAAABBBB12345")
    monkeypatch.setenv("TEST_ACCOUNT_ID", "acct-9911")
    monkeypatch.setenv("TEST_USERNAME", "me@example.com")
    monkeypatch.setenv("TEST_PASSWORD", "hunter2")
    # an aiohttp-style error embedding the request URL (which carries the VIN + account id)
    err = RuntimeError("500, message='Server error', "
                       "url='https://api.example/accounts/acct-9911/vehicles/VF1AAAABBBB12345/charges'")
    out = config.redact(err)
    assert "VF1AAAABBBB12345" not in out and "acct-9911" not in out
    assert out.count("***") == 2 and "message='Server error'" in out   # non-secret text kept
    # empty/absent secrets never mask (would otherwise blank random text)
    for k in ("TEST_VIN", "TEST_ACCOUNT_ID", "TEST_USERNAME", "TEST_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert config.redact("nothing secret here") == "nothing secret here"


def test_redact_masks_auto_discovered_account_id(monkeypatch):
    # account_id left blank -> discovered at runtime; it still embeds in the Kamereon URL and
    # must be redacted even though it was never a configured (env) value.
    monkeypatch.delenv("TEST_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(config, "_DISCOVERED_ACCOUNT_ID", "acct-discovered-42")
    out = config.redact("404 url='https://api/accounts/acct-discovered-42/vehicles/V/charges'")
    assert "acct-discovered-42" not in out and "***" in out


def test_redact_masks_supervisor_token(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervis-tok-abc")
    assert "supervis-tok-abc" not in config.redact("ws error with token supervis-tok-abc")


def test_redacting_filter_scrubs_log_records(monkeypatch):
    monkeypatch.setenv("TEST_VIN", "VF1FILTERVIN")
    monkeypatch.setattr(config, "_DISCOVERED_ACCOUNT_ID", "acct-flt")
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1,
                            "poll failed: %s",
                            ("url=/accounts/acct-flt/vehicles/VF1FILTERVIN/charges",), None)
    assert config._RedactingFilter().filter(rec) is True
    msg = rec.getMessage()
    assert "VF1FILTERVIN" not in msg and "acct-flt" not in msg
    assert msg.count("***") == 2 and rec.args == ()
