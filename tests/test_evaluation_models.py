from src.models.evaluation import OperationEvaluation, ParameterCompletenessScore


def test_parameter_completeness_accepts_not_applicable() -> None:
    operation = OperationEvaluation(
        method="get",
        path="/health",
        description_quality="good",
        parameter_completeness="not_applicable",
        response_completeness="good",
    )

    assert operation.parameter_completeness == ParameterCompletenessScore.NOT_APPLICABLE