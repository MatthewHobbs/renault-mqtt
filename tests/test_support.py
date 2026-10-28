"""Incremental capability answers survive failures and interrupted retries."""

import asyncio

import pytest

from renault_mqtt.support import resolve_support


@pytest.mark.parametrize("answer", [True, 1, False, None])
def test_definite_answers_add_or_remove_and_settle_only_pending_probes(answer):
    supported, pending, calls = {"known", "b"}, {"b", "a"}, []

    async def probe(ep):
        calls.append(ep)
        return answer

    asyncio.run(resolve_support(probe, supported, pending, lambda *_: pytest.fail("unexpected error")))
    assert calls == ["a", "b"]
    assert pending == set()
    assert supported == ({"known", "a", "b"} if answer else {"known"})
    asyncio.run(resolve_support(probe, supported, pending, lambda *_: pytest.fail("unexpected error")))
    assert calls == ["a", "b"]


@pytest.mark.parametrize("error", [RuntimeError("offline"), TimeoutError("probe deadline")])
def test_ordinary_probe_errors_preserve_prior_support_and_retry_only_unknown(error):
    supported, pending, calls, errors = {"a", "known"}, {"c", "b", "a"}, [], []

    async def probe(ep):
        calls.append(ep)
        if ep in {"a", "b"} and calls.count(ep) == 1:
            raise error
        return ep != "c"

    asyncio.run(resolve_support(probe, supported, pending, lambda ep, err: errors.append((ep, err))))
    assert supported == {"a", "known"} and pending == {"a", "b"}
    assert calls == ["a", "b", "c"] and errors == [("a", error), ("b", error)]
    asyncio.run(resolve_support(probe, supported, pending, lambda *_: pytest.fail("unexpected error")))
    assert calls == ["a", "b", "c", "a", "b"]
    assert supported == {"a", "b", "known"} and pending == set()


def test_error_callback_failure_propagates_without_losing_answers_or_probing_later_endpoints():
    supported, pending, calls, errors = {"b", "c", "known"}, {"a", "b", "c", "d"}, [], []
    probe_error = ValueError("probe unavailable")
    callback_error = RuntimeError("error observer failed")

    async def probe(ep):
        calls.append(ep)
        if ep == "c":
            raise probe_error
        return ep != "b"

    def on_error(ep, err):
        errors.append((ep, err))
        raise callback_error

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(resolve_support(probe, supported, pending, on_error))
    assert caught.value is callback_error
    assert errors == [("c", probe_error)]
    assert supported == {"a", "c", "known"}
    assert pending == {"c", "d"}
    assert calls == ["a", "b", "c"]


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
def test_interruption_preserves_partial_answers_in_original_sets(interrupt):
    async def scenario():
        supported, pending, calls, errors = {"b", "known"}, {"a", "b", "c", "d"}, [], []
        started, release = asyncio.Event(), asyncio.Event()

        async def probe(ep):
            calls.append(ep)
            if ep == "c":
                started.set()
                await release.wait()
            return ep != "b"

        task = asyncio.create_task(resolve_support(probe, supported, pending, lambda *args: errors.append(args)))
        try:
            await asyncio.wait_for(started.wait(), 1)
            if interrupt == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 1)
            else:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(task, 0.01)
            assert supported == {"a", "known"}
            assert pending == {"c", "d"} and calls == ["a", "b", "c"]
            assert errors == []
            release.set()
            await resolve_support(probe, supported, pending, lambda *_: pytest.fail("unexpected error"))
            assert calls == ["a", "b", "c", "c", "d"]
            assert supported == {"a", "c", "d", "known"} and pending == set()
        finally:
            release.set()
            task.cancel()
            done, remaining = await asyncio.wait({task}, timeout=1)
            assert not remaining
            await asyncio.gather(*done, return_exceptions=True)

    asyncio.run(scenario())
