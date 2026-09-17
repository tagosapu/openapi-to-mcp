from src.services.config_loader import ConfigLoader
from src.services.config_manager import _detect_provider_from_model


def test_detects_azure_openai_model() -> None:
    assert _detect_provider_from_model("azure/gpt-4o-deployment") == "azure"


def test_detects_azure_openai_model_case_insensitively() -> None:
    assert _detect_provider_from_model(" Azure/GPT-4O ") == "azure"


def test_model_environment_overrides_yaml_config(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("model: anthropic/claude-test\n")
    monkeypatch.setenv("MODEL", "azure/gpt-4o-deployment")

    loader = ConfigLoader(config_dir=str(config_dir))

    assert loader.get_model() == "azure/gpt-4o-deployment"