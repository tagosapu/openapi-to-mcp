from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class TransferSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRANSFER_",
        env_file=".env",
        extra="ignore",
    )

    database_path: Path | str
    jwt_issuer: str
    jwt_audience: str
    jwks_url: str
    allowed_hosts: list[str]
    worker_poll_seconds: float = 1.0
    worker_enabled: bool = True
    max_attempts: int
    max_payload_bytes: int
    idempotency_retention_hours: int = 24
    payload_retention_days: int = 30
    audit_retention_days: int = 90
    requests_per_minute: int
    burst: int
    credentials_json: SecretStr | None = None
    data_encryption_key_ref: SecretStr | None = None