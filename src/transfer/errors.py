from __future__ import annotations


class TransferError(Exception):
    pass


class NotFoundError(TransferError):
    pass


class InvalidTransitionError(TransferError):
    pass


class IdempotencyConflict(TransferError):
    pass


class TenantIsolationError(TransferError):
    pass


class MappingValidationError(TransferError):
    pass


class PayloadLimitError(TransferError):
    pass