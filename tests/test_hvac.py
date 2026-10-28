"""HVAC schedule gating, circuit-breaker trip, retry cadence and recovery."""
import asyncio
from types import SimpleNamespace

import pytest

from renault_mqtt.hvac import hvac_due, poll_hvac_settings


def make_settings(mode="scheduled_value", active=True, monday="T07:00Z", friday="0830"):
    def day(t):
        return SimpleNamespace(readyAtTime=t) if t else None

    sched = SimpleNamespace(
        activated=active, monday=day(monday), tuesday=None, wednesday=None,
        thursday=None, friday=day(friday), saturday=None, sunday=None,
    )
    return SimpleNamespace(mode=mode, schedules=[sched])


def run_poll(fetch, *, supported=frozenset({"hvac-settings"}), breakers=None,
             fails=None, skips=None, trip=3, retry_every=12, due=None,
             endpoint="hvac-settings"):
    breakers = {} if breakers is None else breakers
    fails = {} if fails is None else fails
    skips = {} if skips is None else skips
    events = []
    out = asyncio.run(poll_hvac_settings(
        fetch, supported, breakers, fails, skips,
        trip=trip, retry_every=retry_every, due=due, endpoint=endpoint,
        on_unavailable=lambda err: events.append(("unavailable", str(err))),
        on_tripped=lambda n, err: events.append(("tripped", n, str(err))),
        on_recovered=lambda: events.append(("recovered",)),
    ))
    return out, breakers, fails, skips, events


def test_unlisted_endpoint_is_not_due_and_never_touches_state_or_fetch():
    breakers, fails, skips = {"other": True}, {"other": 1}, {"other": 2}
    assert hvac_due({"pressure"}, breakers, skips, retry_every=12) is False
    assert (breakers, fails, skips) == ({"other": True}, {"other": 1}, {"other": 2})

    async def forbidden():
        raise AssertionError("fetch must not run when endpoint is unlisted")

    out, b, f, s, events = run_poll(forbidden, supported={"pressure", "charge-mode"},
                                    breakers=breakers, fails=fails, skips=skips)
    assert out == {} and events == []
    assert (b, f, s) == ({"other": True}, {"other": 1}, {"other": 2})


@pytest.mark.parametrize("tripped_state", [True, False])
def test_unlisted_endpoint_stays_quiet_even_when_breaker_or_skips_exist(tripped_state):
    breakers = {"hvac-settings": tripped_state}
    skips = {"hvac-settings": 5}
    assert hvac_due(set(), breakers, skips, retry_every=12) is False
    assert skips == {"hvac-settings": 5}


def test_supported_clear_breaker_calls_fetch_and_parses_schedule_fields():
    calls = []

    async def fetch():
        calls.append(1)
        return make_settings()

    breakers, fails, skips = {"other": True}, {"other": 2}, {"other": 4}
    out, b, f, s, events = run_poll(fetch, breakers=breakers, fails=fails, skips=skips)
    assert len(calls) == 1
    assert out == {"climate_schedule_mode": "Scheduled Value",
                   "climate_ready_time": "Mon 07:00, Fri 08:30"}
    assert events == []
    assert b == {"other": True}
    assert f == {"other": 2, "hvac-settings": 0}
    assert s == {"other": 4}


@pytest.mark.parametrize("failures", [1, 2])
def test_pre_trip_failures_log_unavailable_and_reset_on_success(failures):
    state = {"fail": True}
    breakers, fails, skips = {}, {}, {}

    async def fetch():
        if state["fail"]:
            raise RuntimeError("502000")
        return make_settings(active=False)

    all_events = []
    for i in range(1, failures + 1):
        out, _, _, _, events = run_poll(fetch, breakers=breakers, fails=fails, skips=skips)
        all_events.extend(events)
        assert out == {}
        assert fails["hvac-settings"] == i
        assert not breakers.get("hvac-settings")
        assert skips == {}
    assert all_events == [("unavailable", "502000")] * failures

    state["fail"] = False
    out, _, _, _, events = run_poll(fetch, breakers=breakers, fails=fails, skips=skips)
    assert out == {"climate_schedule_mode": "Scheduled Value", "climate_ready_time": None}
    assert events == []
    assert fails["hvac-settings"] == 0
    assert not breakers.get("hvac-settings")


def test_trip_threshold_pauses_endpoint_and_skips_until_retry_cadence():
    calls = []

    async def fetch():
        calls.append(1)
        raise RuntimeError("502000")

    breakers, fails, skips = {}, {}, {}
    all_events = []
    for _ in range(3):
        out, _, _, _, events = run_poll(fetch, breakers=breakers, fails=fails, skips=skips)
        all_events.extend(events)
        assert out == {}
    assert len(calls) == 3
    assert breakers == {"hvac-settings": True}
    assert fails == {"hvac-settings": 3}
    assert skips == {}
    assert all_events == [
        ("unavailable", "502000"),
        ("unavailable", "502000"),
        ("tripped", 3, "502000"),
    ]

    for expected_skip in range(1, 12):
        out, _, _, _, events = run_poll(fetch, breakers=breakers, fails=fails, skips=skips)
        assert out == {} and events == []
        assert len(calls) == 3
        assert skips["hvac-settings"] == expected_skip


def test_failed_retry_probe_stays_tripped_and_silent_across_cycles():
    calls = []

    async def fetch():
        calls.append(1)
        raise RuntimeError("502000")

    breakers, fails, skips = {}, {}, {}
    all_events = []
    for _ in range(3 + 12 + 12 + 2):
        _, _, _, _, events = run_poll(fetch, breakers=breakers, fails=fails, skips=skips)
        all_events.extend(events)

    assert len(calls) == 5
    assert breakers == {"hvac-settings": True}
    assert fails == {"hvac-settings": 3}
    assert skips == {"hvac-settings": 2}
    assert len(all_events) == 3
    assert all_events[-1] == ("tripped", 3, "502000")


def test_successful_retry_probe_clears_breaker_resets_fails_and_logs_recovery():
    calls = []
    broken = {"v": True}

    async def fetch():
        calls.append(1)
        if broken["v"]:
            raise RuntimeError("502000")
        return make_settings(mode="always", active=True, monday="0615", friday=None)

    breakers, fails, skips = {}, {}, {}
    for _ in range(3 + 11):
        run_poll(fetch, breakers=breakers, fails=fails, skips=skips)
    assert len(calls) == 3
    assert breakers == {"hvac-settings": True}
    assert skips == {"hvac-settings": 11}

    broken["v"] = False
    out, _, _, _, events = run_poll(fetch, breakers=breakers, fails=fails, skips=skips)
    assert len(calls) == 4
    assert out == {"climate_schedule_mode": "Always", "climate_ready_time": "Mon 06:15"}
    assert events == [("recovered",)]
    assert "hvac-settings" not in breakers
    assert fails["hvac-settings"] == 0
    assert skips["hvac-settings"] == 0

    out, _, _, _, events = run_poll(fetch, breakers=breakers, fails=fails, skips=skips)
    assert len(calls) == 5
    assert out["climate_schedule_mode"] == "Always"
    assert events == []


@pytest.mark.parametrize("trip,retry_every", [(2, 4), (4, 3)])
def test_custom_policy_and_due_hook_are_honoured(trip, retry_every):
    calls, due_seen = [], []
    broken = {"v": True}

    async def fetch():
        calls.append(1)
        if broken["v"]:
            raise RuntimeError("boom")
        return SimpleNamespace(mode=None, schedules=[])

    breakers, fails, skips = {}, {}, {}

    def due(eps):
        due_seen.append(set(eps))
        return hvac_due(eps, breakers, skips, retry_every=retry_every)

    for _ in range(trip):
        _, _, _, _, events = run_poll(fetch, breakers=breakers, fails=fails, skips=skips,
                                      trip=trip, retry_every=retry_every, due=due)
    assert len(calls) == trip
    assert breakers["hvac-settings"] is True
    assert events == [("tripped", trip, "boom")]

    for _ in range(retry_every - 1):
        run_poll(fetch, breakers=breakers, fails=fails, skips=skips,
                 trip=trip, retry_every=retry_every, due=due)
    assert len(calls) == trip

    broken["v"] = False
    out, _, _, _, events = run_poll(fetch, breakers=breakers, fails=fails, skips=skips,
                                    trip=trip, retry_every=retry_every, due=due)
    assert len(calls) == trip + 1
    assert out == {"climate_schedule_mode": None, "climate_ready_time": None}
    assert events == [("recovered",)]
    assert len(due_seen) == trip + retry_every


def test_cancellation_propagates_without_tripping_or_logging():
    cancel = asyncio.CancelledError()

    async def fetch():
        raise cancel

    breakers, fails, skips = {}, {"hvac-settings": 2}, {}
    with pytest.raises(asyncio.CancelledError) as caught:
        run_poll(fetch, breakers=breakers, fails=fails, skips=skips)
    assert caught.value is cancel
    assert breakers == {}
    assert fails == {"hvac-settings": 2}
    assert skips == {}
