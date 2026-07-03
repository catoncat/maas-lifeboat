"""Single-account pressure controls."""

from __future__ import annotations

import asyncio
import time

from .logging import console_log


class PressurePermit:
    def __init__(self, gate: "AccountPressureGate", request_id: str, surface: str, waited_s: float):
        self._gate = gate
        self._request_id = request_id
        self._surface = surface
        self.waited_s = waited_s
        self._released = False

    async def __aenter__(self) -> "PressurePermit":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._gate.release(self._request_id, self._surface)


class AccountPressureGate:
    """Limit simultaneous client generations for one upstream account."""

    def __init__(self, limit: int, busy_cooldown_s: float = 0.0):
        self.limit = max(1, limit)
        self.busy_cooldown_s = max(0.0, busy_cooldown_s)
        self._semaphore = asyncio.Semaphore(self.limit)
        self._cooldown_until = 0.0

    async def acquire(self, request_id: str, surface: str) -> PressurePermit:
        started = time.perf_counter()
        if self._semaphore.locked():
            console_log(f"queue wait id={request_id} surface={surface} limit={self.limit}")
        await self._semaphore.acquire()
        cooldown_remaining = self._cooldown_until - time.perf_counter()
        if cooldown_remaining > 0:
            console_log(f"cooldown wait id={request_id} surface={surface} sleep={round(cooldown_remaining, 3)}s")
            await asyncio.sleep(cooldown_remaining)
        waited = round(time.perf_counter() - started, 3)
        console_log(f"queue acquired id={request_id} surface={surface} limit={self.limit} waited={waited}s")
        return PressurePermit(self, request_id, surface, waited)

    def release(self, request_id: str, surface: str) -> None:
        self._semaphore.release()
        console_log(f"queue release id={request_id} surface={surface} limit={self.limit}")

    def observe_attempts(self, request_id: str, surface: str, attempts: list[object]) -> None:
        if not attempts or self.busy_cooldown_s <= 0:
            return
        any_ok = any(bool(getattr(attempt, "ok", False)) for attempt in attempts)
        all_busy = all(str(getattr(attempt, "error_code", "")) == "10310" for attempt in attempts)
        if any_ok or not all_busy:
            return
        self._cooldown_until = max(self._cooldown_until, time.perf_counter() + self.busy_cooldown_s)
        console_log(f"cooldown set id={request_id} surface={surface} sleep={self.busy_cooldown_s}s reason=all_attempts_10310")
