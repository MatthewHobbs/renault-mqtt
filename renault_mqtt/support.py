"""Incremental capability probing without endpoint or transport policy."""


async def resolve_support(probe, supported, unresolved, on_error):
    """Resolve pending endpoints in sorted order, mutating the caller's sets in place.

    Await probe(endpoint); truthy answers add support and false answers remove it.
    Each definite answer immediately leaves unresolved. Ordinary probe failures call
    on_error(endpoint, exception) synchronously and remain pending, preserving prior
    support. Cancellation propagates with completed answers intact. The caller owns
    defaults, deadlines, acquisition, invalidation and logging; calls are serialized.
    """
    for ep in sorted(unresolved):
        try:
            if await probe(ep):
                supported.add(ep)
            else:
                supported.discard(ep)
            unresolved.discard(ep)
        except Exception as err:
            on_error(ep, err)
