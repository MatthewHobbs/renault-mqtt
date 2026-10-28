"""Unit tests for renault_mqtt.commands (locks, preconditioning temp, SoC limits and command dispatch)."""
import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from renault_mqtt import config
from renault_mqtt.commands import (
    COMMAND_DEBOUNCE_S,
    command_lock_get,
    dispatch_command,
    hvac_start_action,
    precondition_temp,
    set_soc_level,
)

LOG = logging.getLogger("renault_mqtt.test_commands")
RANGES = {
    "soc_min": (15, 45, 5),
    "soc_target": (55, 100, 5),
    "soc_min_target": (15, 45, 5),
    "soc_max_target": (55, 100, 5),
}
ROLES = {
    "soc_min": "min",
    "soc_target": "target",
    "soc_min_target": "min",
    "soc_max_target": "target",
}


def _bounded(coro):
    return asyncio.run(asyncio.wait_for(coro, 2))


@pytest.fixture(autouse=True)
def _default_env_prefix(monkeypatch):
    monkeypatch.setattr(config, "ENV_PREFIX", "A290_")


@asynccontextmanager
async def _yield_vehicle(vehicle):
    yield vehicle


class _Horn:
    def __init__(self, fail=False):
        self.fail = fail
        self.honks = 0

    async def start_horn(self):
        self.honks += 1
        if self.fail:
            raise RuntimeError("action failed at the car")


class _SocVehicle:
    def __init__(self, soc_min=20, soc_target=80, fail=False):
        self.soc_min = soc_min
        self.soc_target = soc_target
        self.fail = fail
        self.soc_set = None

    async def get_battery_soc(self):
        return SimpleNamespace(socMin=self.soc_min, socTarget=self.soc_target)

    async def set_battery_soc(self, *, min, target):
        if self.fail:
            raise RuntimeError("write failed for secret-vin")
        self.soc_set = (min, target)


def _actions():
    return {
        "charge_start": lambda v: v.set_charge_start(),
        "horn": lambda v: v.start_horn(),
        "lights": lambda v: v.start_lights(),
        "climate_start": lambda v: v.set_ac_start(21.0),
        "climate_stop": lambda v: v.set_ac_stop(),
        "hvac_start": lambda v: v.set_ac_start(21.0),
        "hvac_stop": lambda v: v.set_ac_stop(),
        "refresh_location": lambda v: v.refresh_location(),
    }


def _run(cmd, payload="", *, vehicle=None, acquire=None, last_command=None, in_flight=None,
         publish_location=True, enable_refresh_location=False, now=lambda: 1000.0,
         set_soc=None, climate_cmds=frozenset({"climate_start", "climate_stop"}),
         number_cmds=frozenset({"soc_min", "soc_target"})):
    last = {} if last_command is None else last_command
    inflight = set() if in_flight is None else in_flight
    v = _Horn() if vehicle is None else vehicle
    acq = (lambda: _yield_vehicle(v)) if acquire is None else acquire

    async def default_soc(which, val):
        await set_soc_level(
            which, val, number_ranges=RANGES, acquire_vehicle=acq,
            log=LOG, number_roles=ROLES,
        )

    return dispatch_command(
        cmd, payload,
        number_cmds=number_cmds,
        set_soc=default_soc if set_soc is None else set_soc,
        location_cmds=frozenset({"refresh_location"}),
        publish_location=publish_location,
        enable_refresh_location=enable_refresh_location,
        actions=_actions(),
        last_command=last,
        in_flight=inflight,
        acquire_vehicle=acq,
        log=LOG,
        climate_cmds=climate_cmds,
        debounce_s=COMMAND_DEBOUNCE_S,
        now=now,
    )


def test_command_lock_get_separates_groups_and_recreates_across_loops():
    async def locks_in_loop():
        climate_1 = command_lock_get("climate")
        climate_2 = command_lock_get("climate")
        soc = command_lock_get("soc")
        assert climate_1 is climate_2
        assert climate_1 is not soc
        return climate_1, soc

    first_climate, first_soc = asyncio.run(locks_in_loop())
    second_climate, second_soc = asyncio.run(locks_in_loop())
    assert second_climate is not first_climate
    assert second_soc is not first_soc


def test_precondition_temp_unset_and_configured(monkeypatch):
    monkeypatch.setattr(config, "ENV_PREFIX", None)
    monkeypatch.setenv("PRECONDITION_TEMPERATURE", "19.5")
    assert precondition_temp() == 19.5
    monkeypatch.setattr(config, "ENV_PREFIX", "A290_")
    monkeypatch.delenv("A290_PRECONDITION_TEMPERATURE", raising=False)
    assert precondition_temp() is None
    assert precondition_temp(prefix="A290_") is None
    monkeypatch.setenv("A290_PRECONDITION_TEMPERATURE", " 18.5 ")
    assert precondition_temp() == 18.5
    assert precondition_temp(prefix="A290_") == 18.5
    assert precondition_temp("A290_PRECONDITION_TEMPERATURE") == 18.5


@pytest.mark.parametrize("configured,schedule,expected", [
    (23.0, "unexpected", 23.0),
    (None, {"preconditioningTemperature": 19}, 19.0),
    (None, {}, 21.0),
    (None, RuntimeError("api down"), 21.0),
])
def test_hvac_start_action_resolves_temperature_and_fallback(configured, schedule, expected):
    sent = []

    class V:
        async def get_charge_schedule(self):
            if schedule == "unexpected":
                raise AssertionError("schedule must not be read when option is configured")
            if isinstance(schedule, Exception):
                raise schedule
            return schedule

        async def set_ac_start(self, temp):
            sent.append(temp)

    asyncio.run(hvac_start_action(V(), get_temp=lambda: configured))
    assert sent == [expected]


def test_dispatch_command_dispatches_and_ignores_unknown(caplog):
    v = _Horn()
    last, inflight = {}, set()
    asyncio.run(_run("horn", vehicle=v, last_command=last, in_flight=inflight))
    assert v.honks == 1
    assert last == {"horn": 1000.0}
    assert not inflight

    last.clear()
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        asyncio.run(_run("does-not-exist", vehicle=v, last_command=last, in_flight=inflight))
    assert "does-not-exist" not in last
    assert not inflight
    assert any("Ignoring unknown command: does-not-exist" in r.getMessage() for r in caplog.records)


def test_dispatch_command_error_is_logged_and_redacted(monkeypatch, caplog):
    monkeypatch.setattr(config, "ENV_PREFIX", "A290_")
    monkeypatch.setenv("A290_VIN", "VF1SECRETVIN00001")

    @asynccontextmanager
    async def boom():
        raise RuntimeError("login failed for VF1SECRETVIN00001")
        yield

    with caplog.at_level(logging.ERROR, logger=LOG.name):
        asyncio.run(_run("horn", acquire=boom))
    assert any("Command 'horn' failed:" in r.getMessage() for r in caplog.records)
    assert "VF1SECRETVIN00001" not in caplog.text


def test_debounce_is_per_command_and_ends_at_exact_window():
    sent = []

    class V:
        async def start_horn(self):
            sent.append("horn")

        async def start_lights(self):
            sent.append("lights")

    v = V()
    last, inflight = {}, set()
    clock = {"t": 1000.0}
    for t, cmd in ((1000.0, "horn"), (1002.0, "horn"), (1004.9, "horn"),
                   (1004.9, "lights"), (1005.0, "horn")):
        clock["t"] = t
        asyncio.run(_run(cmd, vehicle=v, last_command=last, in_flight=inflight,
                         now=lambda: clock["t"]))
    assert sent == ["horn", "lights", "horn"]


def test_first_press_near_zero_monotonic_time_is_sent():
    v = _Horn()
    last, inflight = {}, set()
    asyncio.run(_run("horn", vehicle=v, last_command=last, in_flight=inflight, now=lambda: 1.0))
    assert v.honks == 1


def test_press_failing_before_dispatch_clears_stamp_while_action_failure_keeps_it():
    v = _Horn()
    logins = {"n": 0}
    last, inflight = {}, set()
    clock = {"t": 1000.0}

    @asynccontextmanager
    async def flaky_login():
        logins["n"] += 1
        if logins["n"] == 1:
            raise RuntimeError("login failed")
        yield v

    asyncio.run(_run("horn", acquire=flaky_login, last_command=last, in_flight=inflight,
                     now=lambda: clock["t"]))
    assert "horn" not in last
    clock["t"] = 1001.0
    asyncio.run(_run("horn", acquire=flaky_login, last_command=last, in_flight=inflight,
                     now=lambda: clock["t"]))
    assert (logins["n"], v.honks) == (2, 1)

    v_fail = _Horn(fail=True)
    last2, inflight2 = {}, set()
    clock["t"] = 2000.0
    asyncio.run(_run("horn", vehicle=v_fail, last_command=last2, in_flight=inflight2,
                     now=lambda: clock["t"]))
    clock["t"] = 2001.0
    asyncio.run(_run("horn", vehicle=v_fail, last_command=last2, in_flight=inflight2,
                     now=lambda: clock["t"]))
    assert v_fail.honks == 1


@pytest.mark.parametrize("login_fails", [False, True])
def test_simultaneous_presses_pass_once_and_in_flight_blocks_after_window(login_fails):
    v = _Horn()
    logins = {"n": 0}
    last, inflight = {}, set()
    clock = {"t": 1000.0}
    gate = None

    @asynccontextmanager
    async def slow_login():
        logins["n"] += 1
        await gate.wait()
        if login_fails:
            raise RuntimeError("login failed")
        yield v

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        first = asyncio.create_task(_run("horn", acquire=slow_login, last_command=last,
                                         in_flight=inflight, now=lambda: clock["t"]))
        await asyncio.sleep(0)
        clock["t"] = 1006.0
        second = asyncio.create_task(_run("horn", acquire=slow_login, last_command=last,
                                          in_flight=inflight, now=lambda: clock["t"]))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(first, second)

    _bounded(scenario())
    assert logins["n"] == 1
    assert v.honks == (0 if login_fails else 1)
    assert ("horn" in last) is not login_fails
    assert "horn" not in inflight


@pytest.mark.parametrize("start_cmd,stop_cmd,climate_set", [
    ("climate_start", "climate_stop", frozenset({"climate_start", "climate_stop"})),
    ("hvac_start", "hvac_stop", frozenset({"hvac_start", "hvac_stop"})),
])
def test_climate_stop_waits_for_earlier_start_login_while_horn_does_not_block_lights(
        start_cmd, stop_cmd, climate_set):
    async def scenario():
        entered, release, submitted, lit = (
            asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
        )
        logins, actions = [], []
        last, inflight = {}, set()

        class V:
            async def get_charge_schedule(self):
                return {}

            async def set_ac_start(self, temp):
                actions.append("start")

            async def set_ac_stop(self):
                actions.append("stop")

            async def start_lights(self):
                lit.set()

        @asynccontextmanager
        async def acquire():
            name = asyncio.current_task().get_name()
            logins.append(name)
            if name in ("start", "horn"):
                entered.set()
                await release.wait()
            yield V()

        async def stop():
            submitted.set()
            await _run(stop_cmd, acquire=acquire, last_command=last, in_flight=inflight,
                       climate_cmds=climate_set)

        tasks = [asyncio.create_task(
            _run(start_cmd, acquire=acquire, last_command=last, in_flight=inflight,
                 climate_cmds=climate_set),
            name="start",
        )]
        try:
            await entered.wait()
            tasks.append(asyncio.create_task(stop(), name="stop"))
            await submitted.wait()
            blocked_logins, blocked_actions = list(logins), list(actions)
            tasks.append(asyncio.create_task(
                _run("lights", acquire=acquire, last_command=last, in_flight=inflight,
                     climate_cmds=climate_set),
                name="lights",
            ))
            await lit.wait()
            assert not tasks[0].done()
            release.set()
            await asyncio.gather(*tasks)
            assert (blocked_logins, blocked_actions, actions) == (["start"], [], ["start", "stop"])
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    _bounded(scenario())


@pytest.mark.parametrize("stage", ["queued", "login", "action"])
def test_cancelled_climate_command_releases_queue_and_preserves_retry_rules(stage):
    async def scenario():
        entered, release, submitted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        actions = []
        last, inflight = {}, set()
        clock = {"t": 1000.0}

        class V:
            async def get_charge_schedule(self):
                return {}

            async def set_ac_start(self, temp):
                actions.append("start")
                if stage == "action":
                    entered.set()
                    await release.wait()

            async def set_ac_stop(self):
                actions.append("stop")

        @asynccontextmanager
        async def acquire():
            if asyncio.current_task().get_name() == "start" and stage != "action":
                entered.set()
                await release.wait()
            yield V()

        async def stop():
            submitted.set()
            await _run("climate_stop", acquire=acquire, last_command=last, in_flight=inflight,
                       now=lambda: clock["t"])

        tasks = [asyncio.create_task(
            _run("climate_start", acquire=acquire, last_command=last, in_flight=inflight,
                 now=lambda: clock["t"]),
            name="start",
        )]
        try:
            await entered.wait()
            tasks.append(asyncio.create_task(stop()))
            await submitted.wait()
            cancelled = tasks[1] if stage == "queued" else tasks[0]
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert ("climate_stop" if stage == "queued" else "climate_start") not in inflight
            if stage == "queued":
                tasks.append(asyncio.create_task(
                    _run("climate_stop", acquire=acquire, last_command=last, in_flight=inflight,
                         now=lambda: clock["t"])
                ))
            release.set()
            await asyncio.gather(*(t for t in tasks if t is not cancelled))
            clock["t"] = 1001.0
            if stage != "queued":
                await _run("climate_start", acquire=acquire, last_command=last, in_flight=inflight,
                           now=lambda: clock["t"])
            assert actions == (["stop", "start"] if stage == "login" else ["start", "stop"])
            if stage == "action":
                clock["t"] = 1006.0
                await _run("climate_start", acquire=acquire, last_command=last, in_flight=inflight,
                           now=lambda: clock["t"])
                assert actions == ["start", "stop", "start"]
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    _bounded(scenario())


@pytest.mark.parametrize("publish_location,enable_refresh,dispatched", [
    (True, True, True),
    (True, False, False),
    (False, True, False),
    (False, False, False),
])
def test_refresh_location_gates_before_login_and_leaves_no_debounce_stamp(
        publish_location, enable_refresh, dispatched):
    logins = {"n": 0}
    refreshed = {"n": 0}
    last, inflight = {}, set()

    class Veh:
        async def refresh_location(self):
            refreshed["n"] += 1

    @asynccontextmanager
    async def acquire():
        logins["n"] += 1
        yield Veh()

    asyncio.run(_run("refresh_location", acquire=acquire, last_command=last, in_flight=inflight,
                     publish_location=publish_location, enable_refresh_location=enable_refresh))
    assert refreshed["n"] == (1 if dispatched else 0)
    assert logins["n"] == (1 if dispatched else 0)
    assert ("refresh_location" in last) is dispatched
    if not dispatched:
        asyncio.run(_run("refresh_location", acquire=acquire, last_command=last, in_flight=inflight,
                         publish_location=True, enable_refresh_location=True))
        assert refreshed["n"] == 1


@pytest.mark.parametrize("cmd,payload,roles,expected", [
    ("soc_target", "90", None, (20, 90)),
    ("soc_min", "30", None, (30, 80)),
    ("soc_max_target", "90", ROLES, (20, 90)),
    ("soc_min_target", "30", ROLES, (30, 80)),
    ("soc_target", "55", None, (20, 55)),
    ("soc_target", "100", None, (20, 100)),
    ("soc_min", "15", None, (15, 80)),
    ("soc_min", "45", None, (45, 80)),
])
def test_set_soc_level_writes_valid_limits_with_default_or_role_mapping(
        cmd, payload, roles, expected):
    v = _SocVehicle()
    asyncio.run(set_soc_level(
        cmd, payload, number_ranges=RANGES,
        acquire_vehicle=lambda: _yield_vehicle(v), log=LOG, number_roles=roles,
    ))
    assert v.soc_set == expected


@pytest.mark.parametrize("cmd,payload", [
    ("soc_target", "not-a-number"),
    ("soc_target", None),
    ("soc_target", "101"),
    ("soc_target", "54"),
    ("soc_target", "500"),
    ("soc_target", "-5"),
    ("soc_target", "0"),
    ("soc_min", "46"),
    ("soc_min", "14"),
    ("soc_target", "inf"),
    ("soc_target", "-inf"),
    ("soc_min", "nan"),
    ("soc_min", "45.9"),
    ("soc_min", "17"),
    ("soc_target", "57"),
])
def test_set_soc_level_rejects_invalid_or_out_of_range_values_before_login(caplog, cmd, payload):
    logins = {"n": 0}

    @asynccontextmanager
    async def acquire():
        logins["n"] += 1
        yield _SocVehicle()

    with caplog.at_level(logging.WARNING, logger=LOG.name):
        asyncio.run(set_soc_level(cmd, payload, number_ranges=RANGES,
                                  acquire_vehicle=acquire, log=LOG))
    assert logins["n"] == 0
    assert any(r.levelno == logging.WARNING and cmd in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("which,payload,car_min,car_target,expected_level,expected_msg", [
    ("soc_min", "45", 15, 40, logging.WARNING, "must be below target"),
    ("soc_min", "45", 15, 45, logging.WARNING, "must be below target"),
    ("soc_target", "55", 65, 80, logging.WARNING, "must be below target"),
    ("soc_target", "90", None, None, logging.ERROR, "current limits unavailable"),
])
def test_set_soc_level_validates_opposing_limit_and_ordering(
        caplog, which, payload, car_min, car_target, expected_level, expected_msg):
    v = _SocVehicle(soc_min=car_min, soc_target=car_target)
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        asyncio.run(set_soc_level(
            which, payload, number_ranges=RANGES,
            acquire_vehicle=lambda: _yield_vehicle(v), log=LOG,
        ))
    assert v.soc_set is None
    assert any(r.levelno == expected_level and expected_msg in r.getMessage() for r in caplog.records)


def test_set_soc_level_serialises_concurrent_writes_and_is_not_debounced_or_fatal(
        monkeypatch, caplog):
    monkeypatch.setattr(config, "ENV_PREFIX", "A290_")
    monkeypatch.setenv("A290_VIN", "VF1SECRETVIN00002")
    car = {"min": 20, "target": 80}

    class V:
        async def get_battery_soc(self):
            return SimpleNamespace(socMin=car["min"], socTarget=car["target"])

        async def set_battery_soc(self, *, min, target):
            await asyncio.sleep(0)
            car["min"], car["target"] = min, target

    async def both():
        await asyncio.gather(
            _run("soc_min", "30", vehicle=V()),
            _run("soc_target", "90", vehicle=V()),
        )

    _bounded(both())
    assert car == {"min": 30, "target": 90}

    v = _SocVehicle()
    last, inflight = {}, set()
    asyncio.run(_run("soc_target", "70", vehicle=v, last_command=last, in_flight=inflight))
    asyncio.run(_run("soc_target", "75", vehicle=v, last_command=last, in_flight=inflight))
    assert v.soc_set == (20, 75)

    v_err = _SocVehicle(fail=True)
    with caplog.at_level(logging.ERROR, logger=LOG.name):
        asyncio.run(set_soc_level(
            "soc_target", "90", number_ranges=RANGES,
            acquire_vehicle=lambda: _yield_vehicle(v_err), log=LOG,
        ))
    assert any("Failed to set soc_target=90:" in r.getMessage() for r in caplog.records)
    assert "VF1SECRETVIN00002" not in caplog.text
