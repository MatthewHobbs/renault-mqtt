"""Single-owner session reuse and cleanup, including inherited cancellation limits."""

import asyncio

import pytest

from renault_mqtt.session import VehicleSession


async def _cancel_and_drain(task):
    task.cancel()
    _, pending = await asyncio.wait({task}, timeout=1)
    if pending:
        task.cancel()
        _, pending = await asyncio.wait(pending, timeout=1)
    assert not pending, "session task did not stop after cancellation"
    await asyncio.gather(task, return_exceptions=True)


class Transport:
    def __init__(self, close_error=None):
        self.closes = 0
        self.close_error = close_error

    async def close(self):
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error


def test_lazy_reuse_invalidate_and_idempotent_close():
    async def scenario():
        sessions, logins = [], []

        def factory():
            session = Transport()
            sessions.append(session)
            return session

        async def login(session):
            logins.append(session)
            return object()

        cache = VehicleSession(factory, login)
        assert sessions == logins == []
        await cache.close()
        first = await cache.vehicle()
        assert await cache.vehicle() is first
        assert logins == sessions and len(sessions) == 1
        await cache.invalidate()
        assert sessions[0].closes == 1
        second = await cache.vehicle()
        assert second is not first
        assert logins == sessions and len(sessions) == 2
        await cache.close()
        await cache.close()
        assert [s.closes for s in sessions] == [1, 1]
        assert await cache.vehicle() is not second
        await cache.close()
        assert [s.closes for s in sessions] == [1, 1, 1]

    asyncio.run(scenario())


def test_construction_failure_can_retry_without_login_or_cleanup():
    async def scenario():
        session, vehicle = Transport(), object()
        attempts, logins = [], []
        error = RuntimeError("construction refused")

        def factory():
            attempts.append(True)
            if len(attempts) == 1:
                raise error
            return session

        async def login(ws):
            logins.append(ws)
            return vehicle

        cache = VehicleSession(factory, login)
        with pytest.raises(RuntimeError) as caught:
            await cache.vehicle()
        assert caught.value is error
        assert logins == [] and session.closes == 0
        assert await cache.vehicle() is vehicle
        assert await cache.vehicle() is vehicle
        assert len(attempts) == 2 and logins == [session]
        await cache.close()
        assert session.closes == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("close_error", [None, RuntimeError("close refused")])
@pytest.mark.parametrize("login_error", [RuntimeError("login refused"), asyncio.CancelledError()])
def test_failed_login_closes_transport_and_retry_is_reused(login_error, close_error):
    async def scenario():
        sessions = [Transport(close_error), Transport()]
        made, logins = [], []
        vehicle = object()

        def factory():
            ws = sessions[len(made)]
            made.append(ws)
            return ws

        async def login(ws):
            logins.append(ws)
            if ws is sessions[0]:
                raise login_error
            return vehicle

        cache = VehicleSession(factory, login)
        with pytest.raises(type(login_error)) as caught:
            await cache.vehicle()
        assert caught.value is login_error
        assert sessions[0].closes == 1
        await cache.close()
        assert sessions[0].closes == 1
        assert await cache.vehicle() is vehicle
        assert await cache.vehicle() is vehicle
        assert logins == made == sessions
        await cache.close()
        assert sessions[1].closes == 1

    asyncio.run(scenario())


def test_task_cancellation_during_login_closes_before_propagating():
    async def scenario():
        started = asyncio.Event()
        release_login = asyncio.Event()
        transport = Transport()

        async def login(ws):
            assert ws is transport
            started.set()
            await release_login.wait()

        cache = VehicleSession(lambda: transport, login)
        task = asyncio.create_task(cache.vehicle())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=1)
            assert transport.closes == 1
            await cache.close()
            assert transport.closes == 1
        finally:
            release_login.set()
            await _cancel_and_drain(task)

    asyncio.run(scenario())


def test_close_error_is_suppressed_and_cached_vehicle_is_dropped():
    async def scenario():
        transports = [Transport(RuntimeError("close refused")), Transport()]
        logins = []

        async def login(ws):
            logins.append(ws)
            return object()

        cache = VehicleSession(lambda: transports[len(logins)], login)
        first = await cache.vehicle()
        await cache.close()
        await cache.close()
        assert transports[0].closes == 1
        assert await cache.vehicle() is not first
        assert logins == transports
        await cache.close()

    asyncio.run(scenario())


def test_cancelled_close_keeps_cached_vehicle_until_cleanup_is_retried():
    async def scenario():
        transport, vehicle = Transport(asyncio.CancelledError()), object()
        logins = []

        async def login(ws):
            logins.append(ws)
            return vehicle

        cache = VehicleSession(lambda: transport, login)
        assert await cache.vehicle() is vehicle
        with pytest.raises(asyncio.CancelledError):
            await cache.invalidate()
        assert await cache.vehicle() is vehicle
        assert logins == [transport] and transport.closes == 1
        transport.close_error = None
        await cache.close()
        await cache.close()
        assert transport.closes == 2

    asyncio.run(scenario())


def test_second_cancellation_can_interrupt_failed_login_cleanup():
    async def scenario():
        login_started, close_started = asyncio.Event(), asyncio.Event()
        release_login, release_close = asyncio.Event(), asyncio.Event()

        class SlowClose(Transport):
            async def close(self):
                self.closes += 1
                close_started.set()
                await release_close.wait()

        transport = SlowClose()

        async def login(ws):
            login_started.set()
            await release_login.wait()

        cache = VehicleSession(lambda: transport, login)
        task = asyncio.create_task(cache.vehicle())
        try:
            await asyncio.wait_for(login_started.wait(), timeout=1)
            task.cancel()
            await asyncio.wait_for(close_started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=1)
            assert transport.closes == 1
            transport.close = Transport.close.__get__(transport)
            await cache.close()
            await cache.close()
            assert transport.closes == 2
        finally:
            release_login.set()
            release_close.set()
            await _cancel_and_drain(task)

    asyncio.run(scenario())
