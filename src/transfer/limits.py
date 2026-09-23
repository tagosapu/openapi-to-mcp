from __future__ import annotations

import json
from math import ceil
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel

from .errors import PayloadLimitError


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


class PayloadLimits:
    def __init__(
        self,
        max_payload_bytes: int = 10 * 1024 * 1024,
        max_fields: int = 500,
        max_line_items: int = 200,
        max_string_length: int = 10_000,
    ) -> None:
        for name, value in (
            ("max_payload_bytes", max_payload_bytes),
            ("max_fields", max_fields),
            ("max_line_items", max_line_items),
            ("max_string_length", max_string_length),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self.max_payload_bytes = max_payload_bytes
        self.max_fields = max_fields
        self.max_line_items = max_line_items
        self.max_string_length = max_string_length

    def validate(self, payload: BaseModel | Mapping[str, Any]) -> None:
        value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.validate_bytes(len(encoded))

        if _count_fields(value) > self.max_fields:
            raise PayloadLimitError("MAX_FIELDS_EXCEEDED")

        line_items = _line_item_count(value)
        if line_items > self.max_line_items:
            raise PayloadLimitError("MAX_LINE_ITEMS_EXCEEDED")

        if _has_long_string(value, self.max_string_length):
            raise PayloadLimitError("MAX_STRING_LENGTH_EXCEEDED")

    def validate_bytes(self, payload_bytes: int) -> None:
        if payload_bytes > self.max_payload_bytes:
            raise PayloadLimitError("MAX_PAYLOAD_BYTES_EXCEEDED")


def _count_fields(value: Any) -> int:
    if isinstance(value, Mapping):
        return len(value) + sum(_count_fields(child) for child in value.values())
    if isinstance(value, list):
        return sum(_count_fields(child) for child in value)
    return 0


def _line_item_count(value: Any) -> int:
    if not isinstance(value, Mapping):
        return 0
    ocr = value.get("ocr")
    if not isinstance(ocr, Mapping):
        return 0
    line_items = ocr.get("line_items")
    return len(line_items) if isinstance(line_items, list) else 0


def _has_long_string(value: Any, maximum: int) -> bool:
    if isinstance(value, str):
        return len(value) > maximum
    if isinstance(value, Mapping):
        return any(
            len(key) > maximum or _has_long_string(child, maximum)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_has_long_string(child, maximum) for child in value)
    return False