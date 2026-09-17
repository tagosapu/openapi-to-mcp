from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.services.llm_client import LLMClient, LLMRequest


def test_uses_sibling_project_azure_environment(monkeypatch) -> None:
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("VISION_MODEL", "gpt-5.4")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")

    with patch("src.services.llm_client.AzureOpenAI") as azure_openai:
        client = LLMClient(timeout_seconds=30)

    assert client.model == "azure/gpt-5.4"
    assert client.get_model_info()["provider"] == "azure"
    azure_openai.assert_called_once_with(
        azure_endpoint="https://example.openai.azure.com/",
        api_key="test-key",
        api_version="2024-10-21",
        timeout=30,
        max_retries=0,
    )


def test_explicit_non_azure_model_is_not_sent_to_azure(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")

    with patch("src.services.llm_client.AzureOpenAI") as azure_openai:
        client = LLMClient(model="anthropic/claude-sonnet-4-20250514")

    assert client.get_model_info()["provider"] == "anthropic"
    azure_openai.assert_not_called()


@pytest.mark.asyncio
async def test_azure_generation_uses_deployment_name() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="generated text"),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=3,
            completion_tokens=2,
            total_tokens=5,
        ),
    )

    with patch("src.services.llm_client.AzureOpenAI") as azure_openai:
        azure_openai.return_value.chat.completions.create.return_value = response
        with patch.dict(
            "os.environ",
            {
                "MODEL": "azure/gpt-5.4",
                "AZURE_OPENAI_API_KEY": "test-key",
                "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
                "AZURE_API_VERSION": "2024-10-21",
            },
            clear=False,
        ):
            client = LLMClient(timeout_seconds=30)
            result = await client.generate_text(LLMRequest(prompt="Say hello"))

    assert result.text == "generated text"
    assert result.usage["total_tokens"] == 5
    call = azure_openai.return_value.chat.completions.create.call_args
    assert call.kwargs["model"] == "gpt-5.4"
    assert call.kwargs["max_completion_tokens"] == 4000