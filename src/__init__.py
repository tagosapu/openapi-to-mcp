"""
OpenAPI to MCP Converter Package.
Converts OpenAPI specifications to MCP servers with LLM enhancement.
"""

from typing import Any

__version__ = "0.1.0"
__author__ = "OpenAPI to MCP Team"
__description__ = "Convert OpenAPI specifications to MCP servers with LLM enhancement"

__all__ = ["main_cli"]


def __getattr__(name: str) -> Any:
	if name == "main_cli":
		from .cli import main_cli

		return main_cli
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
	return sorted([*globals(), *__all__])
