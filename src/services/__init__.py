"""
Service classes for OpenAPI to MCP converter.
"""

from typing import Any

__all__ = [
    # LLM Client (unified via LiteLLM)
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "get_llm_client",
    # OpenAPI Enhancement
    "EnhancementRequest",
    "EnhancementResult",
    "OpenAPIEnhancer",
]


_LAZY_EXPORTS = {
    "LLMClient": ".llm_client",
    "LLMRequest": ".llm_client",
    "LLMResponse": ".llm_client",
    "get_llm_client": ".llm_client",
    "EnhancementRequest": ".openapi_enhancer",
    "EnhancementResult": ".openapi_enhancer",
    "OpenAPIEnhancer": ".openapi_enhancer",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = __import__(f"{__name__}{module_name}", fromlist=[name])
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted([*globals(), *__all__])
