"""Dependency-free, single-poll-owner vehicle session lifecycle."""

import asyncio


class VehicleSession:
    """Lazily create a transport and cache the logged-in vehicle until invalidated.

    session_factory() constructs a transport with async close(); async login(transport)
    returns the vehicle. The caller supplies transport, authentication and model policy.
    Calls must be serialized by one owner; commands use separate sessions.

    Ordinary close errors are suppressed. Cancellation during close propagates and
    leaves the references intact; cleanup is neither shielded nor time-bounded.
    close() is idempotent after completed cleanup and permits subsequent reuse.
    """

    def __init__(self, session_factory, login):
        self._session_factory = session_factory
        self._login = login
        self._websession = None
        self._vehicle = None

    async def vehicle(self):
        if self._vehicle is None:
            self._websession = self._session_factory()
            try:
                self._vehicle = await self._login(self._websession)
            except asyncio.CancelledError:
                await self.invalidate()
                raise
            except Exception:
                await self.invalidate()
                raise
        return self._vehicle

    async def invalidate(self):
        """Drop the cached login so the next vehicle() re-authenticates."""
        if self._websession is not None:
            try:
                await self._websession.close()
            except Exception:
                pass
        self._websession = None
        self._vehicle = None

    async def close(self):
        """Release the session at shutdown."""
        await self.invalidate()
