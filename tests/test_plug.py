"""Persisted baseline, inclusive evidence bounds and distinct unknown reports."""
import json

import pytest

from renault_mqtt.plug import detect_plug_suspect


def detect(state, *, plugged=True, unplugged=False, mileage=1001, soc=48,
           charging=False, now=1600, min_age=600, max_age=43200, km_delta=1, soc_drop=2):
    return detect_plug_suspect(state, plugged=plugged, unplugged=unplugged,
                               mileage=mileage, soc=soc, charging=charging, now=now,
                               min_age=min_age, max_age=max_age, km_delta=km_delta, soc_drop=soc_drop)


def baseline(**changes):
    return dict(plug_prev="plugged", plug_base_ts=1000,
                plug_base_mileage=1000, plug_base_soc=50, **changes)


def test_first_sighting_persists_exact_json_safe_baseline_and_reuses_it():
    state = {"other": [1]}
    assert detect(state, mileage=1000, soc=50, now=1000) == "off"
    expected = baseline(other=[1])
    assert state == expected
    state = json.loads(json.dumps(state))
    assert detect(state, now=4600, mileage=1005, soc=46) == "on"
    assert state == expected


@pytest.mark.parametrize("age,expected", [(-1, "off"), (599, "off"), (600, "on"),
                                        (601, "on"), (43199, "on"), (43200, "on"), (43201, "off")])
def test_age_bounds_are_inclusive_without_rebasing(age, expected):
    state = baseline()
    assert detect(state, now=1000 + age) == expected
    assert state == baseline()


@pytest.mark.parametrize("mileage,soc,expected", [(1000, 50, "off"), (1000, 48, "off"),
                                                (1001, 49, "off"), (1001, 48, "on"),
                                                (1005, 46, "on"), (999, 52, "off")])
def test_both_driving_thresholds_are_required(mileage, soc, expected):
    state = baseline()
    assert detect(state, mileage=mileage, soc=soc) == expected
    assert state == baseline()


@pytest.mark.parametrize("key", ["plug_base_ts", "plug_base_mileage", "plug_base_soc"])
def test_missing_persisted_values_are_not_invented(key):
    state = baseline()
    del state[key]
    before = state.copy()
    assert detect(state) == "off"
    if key == "plug_base_ts":
        assert state == dict(plug_prev="plugged", plug_base_ts=1600,
                             plug_base_mileage=1001, plug_base_soc=48)
    else:
        assert state == before


@pytest.mark.parametrize("key", ["plug_base_ts", "plug_base_mileage", "plug_base_soc"])
@pytest.mark.parametrize("value", [None, 0])
def test_null_and_zero_persisted_values_keep_their_existing_semantics(key, value):
    state = baseline()
    state[key] = value
    before = state.copy()
    expected = "on" if value == 0 and key == "plug_base_mileage" else "off"
    assert detect(state) == expected
    assert state == before


@pytest.mark.parametrize("mileage,soc", [(None, 48), (1001, None), (None, None)])
def test_missing_live_values_do_not_complete_an_incomplete_baseline(mileage, soc):
    state = {}
    assert detect(state, mileage=mileage, soc=soc, now=1000) == "off"
    expected = dict(plug_prev="plugged", plug_base_ts=1000,
                    plug_base_mileage=mileage, plug_base_soc=soc)
    assert state == expected
    assert detect(state, now=4600) == "off"
    assert state == expected
    assert detect(baseline(), mileage=mileage, soc=soc) == "off"


@pytest.mark.parametrize("unplugged,charging,expected", [(True, True, "on"), (True, False, "off"),
                                                       (False, True, "off"), (False, False, "off")])
def test_nonplugged_reports_preserve_baseline_but_only_explicit_unplugged_can_conflict(unplugged, charging, expected):
    state = baseline()
    assert detect(state, plugged=False, unplugged=unplugged, charging=charging) == expected
    assert state == {**baseline(), "plug_prev": "unplugged"}
    state = json.loads(json.dumps(state))
    assert detect(state, mileage=1010, soc=40, now=5000) == "off"
    assert state == dict(plug_prev="plugged", plug_base_ts=5000,
                         plug_base_mileage=1010, plug_base_soc=40)


def test_charging_does_not_suppress_connected_driving_evidence():
    assert detect(baseline(), charging=True) == "on"


def test_policy_is_supplied_by_the_caller():
    assert detect(baseline(), now=1010, min_age=10, max_age=20, km_delta=1, soc_drop=2) == "on"
    assert detect(baseline(), now=1010, min_age=10, max_age=20, km_delta=2, soc_drop=2) == "off"
    assert detect(baseline(), now=1010, min_age=10, max_age=20, km_delta=1, soc_drop=3) == "off"
