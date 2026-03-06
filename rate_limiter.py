from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)


class RateLimiter:
    """Token-bucket rate limiter + concurrency semaphore for shared MiniMax API.

    Parameters:
        max_rpm: Maximum requests per minute.
        max_concurrent: Maximum concurrent API calls.
    """

    def __init__(self, max_rpm: int = 18, max_concurrent: int = 5) -> None:
        self._max_rpm = max_rpm
        self._interval = 60.0 / max_rpm  # seconds between tokens
        self._tokens = float(max_rpm)
        self._max_tokens = float(max_rpm)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._max_tokens, self._tokens + elapsed / self._interval)
        self._last_refill = now

    async def acquire(self) -> None:
        """Wait until a request slot is available (rate + concurrency)."""
        await self._semaphore.acquire()

        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_time = (1.0 - self._tokens) * self._interval

            # Sleep OUTSIDE the lock so other coroutines can proceed
            log.debug("Rate limiter: waiting %.2fs for token", wait_time)
            await asyncio.sleep(wait_time)

    def release(self) -> None:
        """Release the concurrency semaphore after an API call completes."""
        self._semaphore.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        self.release()
