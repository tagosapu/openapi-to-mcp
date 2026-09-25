"""Verification result contracts for generated artifacts."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class VerificationStatus(str, Enum):
    """Verification lifecycle states."""

    PASSED = "passed"
    FAILED = "failed"
    UNVALIDATED = "unvalidated"
    SKIPPED = "skipped"


class OperationVerificationResult(BaseModel):
    """Verification outcome for one generated operation."""

    operation_id: str = Field(description="Stable operation identifier")
    tool_name: str = Field(description="Name of the generated tool")
    scenario_kind: str = Field(description="Scenario classification")
    status: VerificationStatus = Field(description="Verification status")
    request_valid: bool = Field(description="Whether the request matched the contract")
    response_valid: bool = Field(
        description="Whether the response matched the contract"
    )
    error_kind: str | None = Field(
        default=None, description="High-level error classification"
    )
    failure_code: str | None = Field(
        default=None, description="Machine-readable failure code"
    )
    message: str | None = Field(default=None, description="Redacted failure message")
    redacted_input: Any | None = Field(
        default=None, description="Redacted request payload or metadata"
    )
    redacted_output: Any | None = Field(
        default=None, description="Redacted response payload or metadata"
    )

    model_config = {"str_strip_whitespace": True}


class RepairAttemptResult(BaseModel):
    """Outcome of one repair attempt."""

    attempt: int = Field(description="1-based repair attempt number")
    changed_files: list[str] = Field(
        default_factory=list, description="Files changed by the repair"
    )
    candidate_dir: str | None = Field(
        default=None, description="Directory containing the repaired candidate"
    )
    accepted: bool = Field(description="Whether the repair was accepted")
    failure_codes: list[str] = Field(
        default_factory=list, description="Failure codes observed on this attempt"
    )

    model_config = {"str_strip_whitespace": True}


class GeneratedArtifactVerificationResult(BaseModel):
    """Aggregate verification result for one generated artifact directory."""

    artifact_dir: str = Field(description="Artifact directory under verification")
    spec_sha256: str = Field(description="SHA-256 digest of the input spec")
    generator_version: str = Field(description="Generator version string")
    seed: int = Field(description="Deterministic seed used for generation")
    status: VerificationStatus = Field(description="Overall verification status")
    attempts: int = Field(description="Total verification attempts")
    operations: list[OperationVerificationResult] = Field(
        default_factory=list, description="Per-operation verification results"
    )
    repair_attempts: list[RepairAttemptResult] = Field(
        default_factory=list, description="Repair attempt results"
    )
    failures: list[str] = Field(
        default_factory=list, description="Top-level failure codes"
    )

    model_config = {"str_strip_whitespace": True}