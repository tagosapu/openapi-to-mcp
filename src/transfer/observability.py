from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover - optional deployment dependency
    trace = None

try:
    from prometheus_client import REGISTRY, Counter, Histogram
except ImportError:  # pragma: no cover - optional deployment dependency
    Counter = None
    Histogram = None
    REGISTRY = None


_REDACTED = "[REDACTED]"
_SAFE_HEADERS = {"x-request-id", "traceparent"}
_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "password",
    "secret",
    "token",
    "credential",
    "credentials",
    "raw_value",
}
_SENSITIVE_CONTAINERS = {
    "body",
    "correction",
    "corrections",
    "document",
    "fields",
    "line_items",
    "ocr",
    "payload",
    "request",
    "response",
    "values",
}
_SAFE_DETAIL_KEYS = {
    "actor",
    "attempt",
    "classification",
    "code",
    "connector_id",
    "connector_version",
    "correlation_id",
    "duration_ms",
    "event_type",
    "from_status",
    "mapping_id",
    "mapping_version",
    "operation",
    "phase",
    "reason",
    "reason_present",
    "request_id",
    "resolution",
    "status",
    "target_resource_id",
    "to_status",
    "correction_ref",
}
_LABEL_NAMES = ("status", "classification")


class SecretRedactor:
    def event_detail(self, detail: Any) -> Any:
        return self._redact(detail)

    def headers(self, headers: dict[str, Any]) -> dict[str, Any]:
        return {
            name: value if name.lower() in _SAFE_HEADERS else _REDACTED
            for name, value in headers.items()
        }

    def _redact(self, value: Any, key: str | None = None) -> Any:
        normalized_key = key.lower() if key else None
        if normalized_key in _SENSITIVE_KEYS or normalized_key in _SENSITIVE_CONTAINERS:
            return _REDACTED
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for child_key, child_value in value.items():
                child_name = str(child_key)
                if child_name.lower() == "headers" and isinstance(child_value, dict):
                    result[child_name] = self.headers(child_value)
                elif child_name.lower() in _SENSITIVE_KEYS or child_name.lower() in _SENSITIVE_CONTAINERS:
                    result[child_name] = _REDACTED
                elif child_name.lower() in _SAFE_DETAIL_KEYS:
                    result[child_name] = self._redact(child_value, child_name)
                else:
                    result[child_name] = _REDACTED
            return result
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value


class TransferObservability:
    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        tracer: Any | None = None,
        event_counter: Any | None = None,
        duration_histogram: Any | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._logger = logger
        self._tracer = tracer if tracer is not None else _default_tracer()
        self._event_counter = event_counter if event_counter is not None else _metric_counter()
        self._duration_histogram = (
            duration_histogram
            if duration_histogram is not None
            else _metric_histogram()
        )
        self.redactor = redactor or SecretRedactor()

    def record_event(
        self,
        *,
        status: str,
        classification: str,
        connector_id: str,
        duration_ms: int | None = None,
    ) -> None:
        labels = {
            "status": status,
            "classification": classification,
            "connector_id": connector_id,
        }
        metric_labels = {
            "status": status,
            "classification": classification,
        }
        if self._event_counter is not None:
            self._event_counter.labels(**metric_labels).inc()
        if duration_ms is not None and self._duration_histogram is not None:
            self._duration_histogram.labels(**metric_labels).observe(duration_ms)
        if self._logger is not None:
            self._logger.info("transfer event", extra=labels)

    @contextmanager
    def span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        *,
        status: str | None = None,
        classification: str | None = None,
        connector_id: str | None = None,
    ) -> Iterator[Any]:
        safe_attributes = _safe_attributes(
            attributes,
            status=status,
            classification=classification,
            connector_id=connector_id,
        )
        if self._tracer is None:
            with nullcontext() as span:
                yield span
            return
        with self._tracer.start_as_current_span(name, attributes=safe_attributes) as span:
            yield span


def _safe_attributes(
    attributes: dict[str, Any] | None,
    *,
    status: str | None,
    classification: str | None,
    connector_id: str | None,
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    if attributes:
        for key in _LABEL_NAMES:
            if key in attributes:
                safe[key] = str(attributes[key])
    for key, value in (
        ("status", status),
        ("classification", classification),
        ("connector_id", connector_id),
    ):
        if value is not None:
            safe[key] = str(value)
    return safe


def _default_tracer() -> Any | None:
    return trace.get_tracer("ocr-transfer") if trace is not None else None


def _metric_counter() -> Any | None:
    if Counter is None:
        return None
    return _get_or_create_metric(
        Counter,
        "ocr_transfer_events_total",
        "OCR transfer processing events",
    )


def _metric_histogram() -> Any | None:
    if Histogram is None:
        return None
    return _get_or_create_metric(
        Histogram,
        "ocr_transfer_delivery_duration_ms",
        "OCR transfer delivery duration in milliseconds",
    )


def _get_or_create_metric(metric_type: Any, name: str, documentation: str) -> Any:
    try:
        return metric_type(name, documentation, labelnames=_LABEL_NAMES)
    except ValueError:
        if REGISTRY is not None:
            collector = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
            if collector is not None:
                return collector
        return None