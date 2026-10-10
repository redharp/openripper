"""Live capacity and measured transfer feedback; never persisted as job history."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager


class RipSlots:
    def __init__(self, limit: int):
        self.limit = limit
        self.active = 0
        self._condition = asyncio.Condition()
        self._overrides: set[str] = set()
        self.active_overrides: set[str] = set()

    async def override(self, job_id: str) -> None:
        async with self._condition:
            self._overrides.add(job_id)
            self._condition.notify_all()

    def clear_override(self, job_id: str) -> None:
        self._overrides.discard(job_id)

    @asynccontextmanager
    async def slot(self, job_id: str):
        acquired = False
        try:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: self.active < self.limit or job_id in self._overrides
                )
                if job_id in self._overrides:
                    self.active_overrides.add(job_id)
                self._overrides.discard(job_id)
                self.active += 1
                acquired = True
            yield
        finally:
            async with self._condition:
                self._overrides.discard(job_id)
                self.active_overrides.discard(job_id)
                if acquired:
                    self.active -= 1
                self._condition.notify_all()

    async def set_limit(self, limit: int) -> None:
        async with self._condition:
            self.limit = limit
            self._condition.notify_all()

    async def __aenter__(self):
        async with self._condition:
            await self._condition.wait_for(lambda: self.active < self.limit)
            self.active += 1
        return self

    async def __aexit__(self, *_):
        async with self._condition:
            self.active -= 1
            self._condition.notify_all()


class TransferMeter:
    def __init__(self, now: float, initial: int = 0):
        self.samples: deque[tuple[float, int]] = deque([(now, initial)])
        self.last_change = now
        self.last_bytes = initial

    def sample(self, now: float, written: int, expected: int) -> dict:
        if written > self.last_bytes:
            self.last_change = now
        self.last_bytes = written
        self.samples.append((now, written))
        while len(self.samples) > 2 and self.samples[1][0] < now - 30:
            self.samples.popleft()
        elapsed = now - self.samples[0][0]
        rate = max(0, written - self.samples[0][1]) / elapsed if elapsed >= 5 else None
        remaining = max(0, expected - written)
        return {
            "bytes_written": written,
            "bytes_expected": expected,
            "bytes_per_second": round(rate) if rate is not None else None,
            "eta_seconds": round(remaining / rate) if rate and expected else None,
            "seconds_since_write": round(now - self.last_change),
        }
