from __future__ import annotations

from math import ceil
from typing import Protocol

from pydantic import BaseModel


class RateLimitDecision(BaseModel):
    allowed: bool
    retry_after_seconds: int


class Clock(Protocol):
    def monotonic(self) -> float: ...


class RateLimiter:
    def __init__(self, requests_per_minute: int, burst: int, clock: Clock) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        if burst <= 0:
            raise ValueError("burst must be positive")
        self._rate_per_second = requests_per_minute / 60.0
        self._burst = float(burst)
        self._clock = clock
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}

    def check(self, tenant_id: str, client_id: str) -> RateLimitDecision:
        now = self._clock.monotonic()
        key = (tenant_id, client_id)
        tokens, last_checked = self._buckets.get(key, (self._burst, now))
        elapsed = max(0.0, now - last_checked)
        tokens = min(self._burst, tokens + elapsed * self._rate_per_second)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return RateLimitDecision(allowed=True, retry_after_seconds=0)

        retry_after = max(1, ceil((1.0 - tokens) / self._rate_per_second))
        self._buckets[key] = (tokens, now)
        return RateLimitDecision(allowed=False, retry_after_seconds=retry_after)