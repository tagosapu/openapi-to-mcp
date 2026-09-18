import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from jinja2 import Template

from src.models.evaluation import LintingResult
from src.services.llm_client import LLMResponse
from src.services.openapi_enhancer import EnhancementRequest
from src.services.openapi_enhancer import OpenAPIEnhancer


def test_large_azure_prompt_uses_chunk_path() -> None:
    enhancer = OpenAPIEnhancer.__new__(OpenAPIEnhancer)
    enhancer.llm_client = SimpleNamespace(provider="azure")

    assert enhancer._should_chunk_prompt("x" * 260_000) is True
    assert enhancer._should_chunk_prompt("short prompt") is False


def test_non_azure_prompt_keeps_single_call_path() -> None:
    enhancer = OpenAPIEnhancer.__new__(OpenAPIEnhancer)
    enhancer.llm_client = SimpleNamespace(provider="anthropic")

    assert enhancer._should_chunk_prompt("x" * 128_000) is False


def test_chunk_evaluations_merge_operations_schemas_and_overall_scores() -> None:
    first = {
        "api_title": "Example",
        "api_version": "1.0.0",
        "openapi_version": "3.0.3",
        "operations": [{"method": "get", "path": "/users"}],
        "schemas": [{"schema_name": "User"}],
        "security_schemes": [{"type": "apiKey", "name": "X-API-Key"}],
        "overall": {
            "overall_quality": "good",
            "completeness_score": 4,
            "ai_readiness_score": 3,
            "has_comprehensive_descriptions": True,
            "has_good_examples": True,
            "has_proper_error_handling": True,
            "security_well_defined": True,
            "major_improvements_needed": ["Add pagination"],
            "minor_improvements_suggested": [],
            "key_strengths": ["Clear paths"],
            "areas_for_improvement": [],
            "recommendations": [],
        },
    }
    second = {
        "api_title": "Example",
        "api_version": "1.0.0",
        "openapi_version": "3.0.3",
        "operations": [{"method": "post", "path": "/users"}],
        "schemas": [{"schema_name": "User"}, {"schema_name": "Order"}],
        "security_schemes": [{"type": "apiKey", "name": "X-API-Key"}],
        "overall": {
            "overall_quality": "fair",
            "completeness_score": 2,
            "ai_readiness_score": 3,
            "has_comprehensive_descriptions": False,
            "has_good_examples": True,
            "has_proper_error_handling": False,
            "security_well_defined": True,
            "major_improvements_needed": ["Add pagination"],
            "minor_improvements_suggested": ["Add examples"],
            "key_strengths": [],
            "areas_for_improvement": ["Responses"],
            "recommendations": ["Document errors"],
        },
    }

    merged = OpenAPIEnhancer._merge_chunk_evaluations([first, second])

    assert {(item["method"], item["path"]) for item in merged["operations"]} == {
        ("get", "/users"),
        ("post", "/users"),
    }
    assert [item["schema_name"] for item in merged["schemas"]] == ["User", "Order"]
    assert len(merged["security_schemes"]) == 1
    assert merged["overall"]["overall_quality"] == "fair"
    assert merged["overall"]["completeness_score"] == 3
    assert merged["overall"]["has_comprehensive_descriptions"] is False
    assert merged["overall"]["major_improvements_needed"] == ["Add pagination"]


def test_chunk_overall_scores_are_clamped_to_public_range() -> None:
    evaluation = {
        "overall": {
            "overall_quality": "excellent",
            "completeness_score": 8,
            "ai_readiness_score": 0,
        }
    }

    merged = OpenAPIEnhancer._merge_chunk_evaluations([evaluation])

    assert merged["overall"]["completeness_score"] == 5
    assert merged["overall"]["ai_readiness_score"] == 1


def test_chunk_merge_fills_schema_entries_omitted_by_model() -> None:
    evaluation = {
        "schemas": [
            {
                "schema_name": "User",
                "description_quality": "good",
                "properties_documented": True,
                "examples_provided": True,
                "required_fields_specified": True,
            }
        ],
        "overall": {
            "overall_quality": "good",
            "completeness_score": 4,
            "ai_readiness_score": 4,
        },
    }
    spec = {
        "components": {
            "schemas": {
                "User": {"description": "A user"},
                "Order": {"type": "object", "properties": {"id": {}}},
            }
        }
    }

    merged = OpenAPIEnhancer._merge_chunk_evaluations(
        [evaluation], spec_dict=spec
    )

    assert [schema["schema_name"] for schema in merged["schemas"]] == [
        "User",
        "Order",
    ]
    assert merged["schemas"][1]["description_quality"] == "missing"


@pytest.mark.asyncio
async def test_chunk_retries_transient_provider_failure() -> None:
    class RateLimitError(Exception):
        status_code = 429

    class FakeLLMClient:
        provider = "azure"
        model = "azure/test"

        def __init__(self) -> None:
            self.calls = 0

        async def generate_text(self, request):
            self.calls += 1
            if self.calls == 1:
                raise RateLimitError("try again")
            return LLMResponse(
                text=json.dumps(
                    {
                        "operations": [],
                        "schemas": [],
                        "security_schemes": [],
                        "overall": {
                            "overall_quality": "good",
                            "completeness_score": 4,
                            "ai_readiness_score": 4,
                        },
                    }
                ),
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                model=self.model,
            )

    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Example", "version": "1.0.0"},
        "paths": {"/health": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    fake_client = FakeLLMClient()
    enhancer = OpenAPIEnhancer.__new__(OpenAPIEnhancer)
    enhancer.llm_client = fake_client
    enhancer.evaluation_template = Template("{{ openapi_spec }}")
    enhancer.chunk_evaluation_template = Template("{{ openapi_spec }}")
    request = EnhancementRequest(
        spec_content=json.dumps(spec), spec_format="json", original_filename="example.json"
    )
    linting_results = LintingResult(
        total_issues=0, linting_score=5, linting_summary="clean"
    )

    def get_int(key, default):
        return {
            "azure_chunk_prompt_tokens": 2_140,
            "azure_chunk_prompt_overhead_tokens": 2_000,
            "azure_chunk_max_tokens": 100,
            "azure_chunk_retry_limit": 1,
            "azure_max_concurrency": 1,
        }.get(key, default)

    with (
        patch("src.services.openapi_enhancer.config.get_int", side_effect=get_int),
        patch("src.services.openapi_enhancer.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        evaluation = await enhancer._evaluate_large_specification(
            spec_dict=spec,
            metadata={"api_title": "Example", "api_version": "1.0.0"},
            request=request,
            linting_results=linting_results,
            max_tokens=100,
            temperature=0.1,
        )

    assert fake_client.calls == 2
    sleep.assert_awaited_once()
    assert evaluation.llm_calls_count == 1


@pytest.mark.asyncio
async def test_large_specification_uses_multiple_llm_calls_and_merges_results() -> None:
    class FakeLLMClient:
        provider = "azure"
        model = "azure/test"

        def __init__(self) -> None:
            self.calls = []

        async def generate_text(self, request):
            self.calls.append(request)
            path = "/a" if '"/a"' in request.prompt else "/b"
            payload = {
                "operations": [
                    {
                        "method": "get",
                        "path": path,
                        "description_quality": "good",
                        "parameter_completeness": "not_applicable",
                        "response_completeness": "good",
                    }
                ],
                "schemas": [],
                "security_schemes": [],
                "overall": {
                    "overall_quality": "good",
                    "completeness_score": 4,
                    "ai_readiness_score": 4,
                },
            }
            return LLMResponse(
                text=json.dumps(payload),
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "total_cost_usd": 0.01,
                },
                model=self.model,
            )

    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Example", "version": "1.0.0"},
        "paths": {
            "/a": {"get": {"description": "a" * 200, "responses": {"200": {"description": "ok"}}}},
            "/b": {"get": {"description": "b" * 200, "responses": {"200": {"description": "ok"}}}},
        },
    }
    fake_client = FakeLLMClient()
    enhancer = OpenAPIEnhancer.__new__(OpenAPIEnhancer)
    enhancer.llm_client = fake_client
    enhancer.evaluation_template = Template("{{ openapi_spec }}")
    enhancer.chunk_evaluation_template = Template("CHUNK {{ openapi_spec }}")

    def get_int(key, default):
        return {
            "azure_chunk_prompt_tokens": 2_140,
            "azure_chunk_prompt_overhead_tokens": 2_000,
            "azure_chunk_max_tokens": 100,
            "azure_max_concurrency": 2,
        }.get(key, default)

    request = EnhancementRequest(
        spec_content=json.dumps(spec), spec_format="json", original_filename="example.json"
    )
    linting_results = LintingResult(
        total_issues=0, linting_score=5, linting_summary="clean"
    )

    with patch("src.services.openapi_enhancer.config.get_int", side_effect=get_int):
        evaluation = await enhancer._evaluate_large_specification(
            spec_dict=spec,
            metadata={"api_title": "Example", "api_version": "1.0.0"},
            request=request,
            linting_results=linting_results,
            max_tokens=100,
            temperature=0.1,
        )

    assert len(fake_client.calls) == 2
    assert evaluation.llm_calls_count == 2
    assert {operation.path for operation in evaluation.operations} == {"/a", "/b"}
    assert evaluation.total_tokens == 240
    assert evaluation.evaluation_id
    assert evaluation.api_title == "Example"
    assert evaluation.api_version == "1.0.0"
    assert evaluation.openapi_version == "3.0.3"
    assert all(call.prompt.startswith("CHUNK ") for call in fake_client.calls)