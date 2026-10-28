"""Car time and feed time must never stand in for one another."""

import pytest

from renault_mqtt import freshness
from renault_mqtt.util import iso

NOW = 1767225600.0
STALE = 6 * 3600


@pytest.fixture
def clock(monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(freshness, "now_ts", lambda: clock["t"])
    return clock


@pytest.mark.parametrize("car_age,data_stale", [(STALE - 1, "off"), (STALE, "off"), (STALE + 1, "on")])
@pytest.mark.parametrize("feed_age,poll_failing", [(STALE - 1, "off"), (STALE, "off"), (STALE + 1, "on")])
def test_car_and_feed_thresholds_are_independent(clock, car_age, data_stale, feed_age, poll_failing):
    state = {"other": "preserved"}
    assert freshness.freshness_fields(state, iso(NOW - car_age), STALE, NOW - feed_age) == {
        "data_stale": data_stale, "poll_failing": poll_failing,
    }
    assert state == {"other": "preserved", "last_payload_ts": NOW - car_age}


def test_old_car_payload_stays_stale_when_every_poll_succeeds(clock):
    assert freshness.freshness_fields({}, iso(NOW - 68 * 3600), STALE, NOW) == {
        "data_stale": "on", "poll_failing": "off",
    }


@pytest.mark.parametrize("payload", [None, "", "not-a-timestamp", "1970-01-01T00:00:00Z"])
@pytest.mark.parametrize("last_ok,poll_failing", [(0, "on"), (NOW, "off")])
def test_unknown_car_time_is_omitted_without_persisted_state(clock, payload, last_ok, poll_failing):
    state = {"other": "preserved"}
    assert freshness.freshness_fields(state, payload, STALE, last_ok) == {"poll_failing": poll_failing}
    assert state == {"other": "preserved"}


@pytest.mark.parametrize("payload", [None, "", "not-a-timestamp", "1970-01-01T00:00:00Z"])
@pytest.mark.parametrize("age,expected", [(600, "off"), (8 * 3600, "on")])
def test_missing_or_invalid_payload_reuses_persisted_car_time(clock, payload, age, expected):
    state = {"last_payload_ts": NOW - age, "other": "preserved"}
    assert freshness.freshness_fields(state, payload, STALE, NOW - age) == {
        "data_stale": expected, "poll_failing": expected,
    }
    assert state == {"last_payload_ts": NOW - age, "other": "preserved"}


def test_seen_car_time_is_persisted_and_ages_during_an_outage(clock):
    state = {}
    assert freshness.freshness_fields(state, iso(NOW - 600), STALE, NOW) == {
        "data_stale": "off", "poll_failing": "off",
    }
    assert state == {"last_payload_ts": NOW - 600}
    clock["t"] += STALE
    assert freshness.freshness_fields(state, None, STALE, NOW) == {
        "data_stale": "on", "poll_failing": "off",
    }
    clock["t"] += 1
    assert freshness.freshness_fields(state, None, STALE, NOW) == {
        "data_stale": "on", "poll_failing": "on",
    }
    assert state == {"last_payload_ts": NOW - 600}


def test_new_payload_replaces_persisted_timestamp_not_with_poll_time(clock):
    state = {"last_payload_ts": NOW - 8 * 3600, "other": "preserved"}
    assert freshness.freshness_fields(state, iso(NOW - 600), STALE, NOW) == {
        "data_stale": "off", "poll_failing": "off",
    }
    assert state == {"last_payload_ts": NOW - 600, "other": "preserved"}
