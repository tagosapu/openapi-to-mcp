from src.models.generated_validation import (
    GeneratedArtifactVerificationResult,
    OperationVerificationResult,
    VerificationStatus,
)


def test_verification_result_serializes_status_and_operation_failures() -> None:
    result = GeneratedArtifactVerificationResult(
        artifact_dir="/tmp/mcpserver",
        spec_sha256="abc",
        generator_version="test",
        seed=7,
        status=VerificationStatus.FAILED,
        attempts=1,
        operations=[
            OperationVerificationResult(
                operation_id="getUser",
                tool_name="getUser",
                scenario_kind="success",
                status=VerificationStatus.FAILED,
                request_valid=False,
                response_valid=False,
                failure_code="request_schema_mismatch",
                message="request body does not match schema",
            )
        ],
    )

    payload = result.model_dump(mode="json")

    assert payload["status"] == "failed"
    assert payload["operations"][0]["failure_code"] == "request_schema_mismatch"


def test_result_rejects_secret_values_in_redacted_fields() -> None:
    result = OperationVerificationResult(
        operation_id="listUsers",
        tool_name="listUsers",
        scenario_kind="success",
        status=VerificationStatus.PASSED,
        request_valid=True,
        response_valid=True,
        redacted_input={"headers": {"Authorization": "[REDACTED]"}},
    )

    assert "[REDACTED]" in str(result.model_dump())