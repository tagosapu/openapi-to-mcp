from datetime import datetime

from src.models.evaluation import (
    OpenAPIEvaluationResult,
    OperationEvaluation,
    OverallEvaluation,
    ParameterCompletenessScore,
    QualityScore,
)


def test_parameter_completeness_accepts_not_applicable() -> None:
    operation = OperationEvaluation(
        method="get",
        path="/health",
        description_quality="good",
        parameter_completeness="not_applicable",
        response_completeness="good",
    )

    assert operation.parameter_completeness == ParameterCompletenessScore.NOT_APPLICABLE


def test_evaluation_timestamps_use_iso_format_in_json_mode() -> None:
    timestamp = datetime(2026, 9, 24, 12, 0, 0)
    result = OpenAPIEvaluationResult(
        evaluation_id="test-eval",
        evaluation_timestamp=timestamp,
        timestamp=timestamp,
        openapi_version="3.0.3",
        api_title="Test API",
        api_version="1.0.0",
        overall=OverallEvaluation(
            overall_quality=QualityScore.GOOD,
            completeness_score=4,
            ai_readiness_score=4,
        ),
    )

    serialized = result.model_dump(mode="json")

    assert serialized["evaluation_timestamp"] == "2026-09-24T12:00:00"
    assert serialized["timestamp"] == "2026-09-24T12:00:00"