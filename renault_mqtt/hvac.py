"""HVAC schedule polling, advertisement gating and circuit-breaker recovery."""
from renault_mqtt.parse import _hvac_schedule_fields


def hvac_due(supported_eps, breakers, skips, *, retry_every, endpoint="hvac-settings"):
    """Return True when endpoint is supported and either untripped or due for a retry probe."""
    if endpoint not in supported_eps:
        return False
    if not breakers.get(endpoint):
        return True
    n = skips[endpoint] = skips.get(endpoint, 0) + 1
    if n >= retry_every:
        skips[endpoint] = 0
        return True
    return False


async def poll_hvac_settings(fetch, supported_eps, breakers, fails, skips, *,
                             trip, retry_every, on_unavailable, on_tripped,
                             on_recovered, due=None, transform=_hvac_schedule_fields,
                             endpoint="hvac-settings"):
    """Poll HVAC schedule settings when due and manage breaker trip, retry and recovery.

    Calls due(supported_eps) when provided, else hvac_due(...). When due, awaits fetch()
    and summarises the payload through transform. Any success clears the failure count
    and, if tripped, removes the breaker and calls on_recovered(). Pre-trip exceptions
    increment fails, calling on_unavailable(err) below trip and tripping the breaker with
    on_tripped(n, err) at trip. Failed periodic probes while tripped remain tripped and
    silent. BaseExceptions such as CancelledError propagate.
    """
    is_due = (hvac_due(supported_eps, breakers, skips, retry_every=retry_every, endpoint=endpoint)
              if due is None else due(supported_eps))
    if not is_due:
        return {}
    tripped = bool(breakers.get(endpoint))
    try:
        fields = transform(await fetch())
        fails[endpoint] = 0
        if breakers.pop(endpoint, None):
            on_recovered()
        return fields
    except Exception as err:
        if not tripped:
            n = fails[endpoint] = fails.get(endpoint, 0) + 1
            if n < trip:
                on_unavailable(err)
            elif n == trip:
                breakers[endpoint] = True
                on_tripped(n, err)
        return {}
