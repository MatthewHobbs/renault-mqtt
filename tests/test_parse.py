"""Tests for the parse seam: the shared value-formatting + Kamereon-payload parsing helpers.

Model-agnostic — these operate on values/payloads the add-on's poll_once feeds in, so a stdlib enum
and plain dicts/namespaces stand in for the renault-api types.
"""
import enum
import types

import pytest

from renault_mqtt import parse


class _E(enum.Enum):
    CHARGE_IN_PROGRESS = 1
    NOT_IN_CHARGE = 2


def test_mi_and_dist():
    assert parse._mi(100) == 62.1                    # 100 km -> mi
    assert parse._mi(None) is None
    assert parse._dist(100, "mi") == 62.1
    assert parse._dist(100, "km") == 100.0           # _num rounds
    assert parse._dist(None, "km") is None


@pytest.mark.parametrize("v", [True, "true", "True", "on", "ON", 1, "1"])
def test_bool_on_truthy(v):
    assert parse._bool_on(v) == "on"


@pytest.mark.parametrize("v", [False, "false", "off", 0, "0", None, "nonsense"])
def test_bool_on_falsy(v):
    assert parse._bool_on(v) == "off"


def test_enum_label_known_mapped():
    assert parse._enum_label(_E.CHARGE_IN_PROGRESS, {_E.CHARGE_IN_PROGRESS: "Charging"}, None) == "Charging"


def test_enum_label_unmapped_is_prettified():
    assert parse._enum_label(_E.NOT_IN_CHARGE, {}, None) == "Not In Charge"


def test_enum_label_none_uses_raw():
    assert parse._enum_label(None, {}, None) == "Unknown"
    assert parse._enum_label(None, {}, 7.0) == "Unknown (7.0)"


def test_find_precond_locates_nested_block():
    payload = {"data": {"attributes": {"ev": {"preconditioningHeatedStrgWheel": True}}}}
    assert parse._find_precond(payload) == {"preconditioningHeatedStrgWheel": True}


def test_find_precond_empty_when_absent_or_too_deep():
    assert parse._find_precond({"nothing": 1}) == {}
    assert parse._find_precond("not-a-dict") == {}
    # deeper than the recursion cap -> {}
    deep = cur = {}
    for _ in range(6):
        cur["attributes"] = {}
        cur = cur["attributes"]
    cur["preconditioningX"] = True
    assert parse._find_precond(deep) == {}


def test_fmt_hhmm_only_reformats_bare_four_digits():
    assert parse._fmt_hhmm("0730") == "07:30"
    assert parse._fmt_hhmm(None) is None
    assert parse._fmt_hhmm("07:30") == "07:30"       # already formatted -> as-is
    assert parse._fmt_hhmm("") is None
    assert parse._fmt_hhmm(1230) == "12:30"          # 4-digit int coerced to str + reformatted
    assert parse._fmt_hhmm(730) == "730"             # 3 digits -> not a bare HHMM, returned as-is


def test_charge_schedule_fields_extracts_and_defaults():
    out = parse._charge_schedule_fields(
        {"chargeModeRq": "scheduled_charge", "chargeTimeStart": "0700", "chargeDuration": 120})
    assert out == {"charge_schedule_mode": "Scheduled Charge",
                   "scheduled_charge_start": "07:00", "scheduled_charge_duration": 120.0}
    assert parse._charge_schedule_fields({}) == {
        "charge_schedule_mode": None, "scheduled_charge_start": None, "scheduled_charge_duration": None}


def test_fmt_ready_normalises_time_forms():
    assert parse._fmt_ready("T07:00Z") == "07:00"
    assert parse._fmt_ready("0700") == "07:00"
    assert parse._fmt_ready("07:00:00") == "07:00"
    assert parse._fmt_ready(None) is None


def test_hvac_schedule_fields_active_and_inactive():
    day = types.SimpleNamespace(readyAtTime="T07:00Z")
    sched = types.SimpleNamespace(activated=True, monday=day, friday=types.SimpleNamespace(readyAtTime="0800"))
    settings = types.SimpleNamespace(mode="scheduled", schedules=[sched])
    out = parse._hvac_schedule_fields(settings)
    assert out["climate_schedule_mode"] == "Scheduled"
    assert out["climate_ready_time"] == "Mon 07:00, Fri 08:00"
    # no active schedule / no mode -> None values
    empty = parse._hvac_schedule_fields(types.SimpleNamespace(mode=None, schedules=[]))
    assert empty == {"climate_schedule_mode": None, "climate_ready_time": None}


def test_available_energy_prefers_reported_else_estimates_from_soc():
    # car reports the real value -> use it verbatim
    assert parse.available_energy(28.4, 55, 52.0) == 28.4
    assert parse.available_energy("28.4", 55, 52.0) == 28.4
    # car omits it (e.g. the R5) -> estimate soc% of capacity
    assert parse.available_energy(None, 50, 52.0) == 26.0
    assert parse.available_energy(None, 14, 52.0) == 7.28
    # neither a value nor a SoC -> None (genuinely unknown)
    assert parse.available_energy(None, None, 52.0) is None


def test_hvac_schedule_fields_active_but_no_ready_times():
    sched = types.SimpleNamespace(activated=True, monday=None)   # day present but None -> skipped
    out = parse._hvac_schedule_fields(types.SimpleNamespace(mode="x", schedules=[sched]))
    assert out["climate_ready_time"] is None      # no parts -> None
