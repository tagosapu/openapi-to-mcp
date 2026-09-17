"""
Simple LLM client using LiteLLM for unified provider access.
Supports all LiteLLM-compatible providers through a single interface.
"""

import asyncio
import logging
import os
import time
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

import litellm
import openai
from openai import AzureOpenAI

from .config_loader import config


# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,  # Set the log level to INFO
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)

# Configure LiteLLM with default debug value
# Will be properly set when LLMClient is initialized
litellm.set_verbose = False


def _configure_diagnostic_file_logging() -> None:
    """Persist request lifecycle logs without writing prompts or credentials."""
    root_logger = logging.getLogger()
    if any(
        getattr(handler, "_openapi_to_mcp_diagnostic", False)
        for handler in root_logger.handlers
    ):
        return

    try:
        diagnostic_path = config.get_path(
            "diagnostic_log_file", "./logs/aoai_run.log"
        )
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            diagnostic_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},"
                "%(levelname)s,%(message)s"
            )
        )
        handler._openapi_to_mcp_diagnostic = True
        root_logger.addHandler(handler)
    except OSError as error:
        logger.warning("Could not create diagnostic log file: %s", error)


class LLMRequest(BaseModel):
    """Request model for LLM API calls."""

    prompt: str = Field(description="The prompt to send to the model")
    max_tokens: int = Field(default=4000, description="Maximum tokens in response")
    temperature: float = Field(
        default=0.1, ge=0.0, le=1.0, description="Response temperature"
    )
    top_p: float = Field(
        default=0.9, ge=0.0, le=1.0, description="Top-p sampling parameter"
    )
    stop_sequences: List[str] = Field(
        default_factory=list, description="Stop sequences for generation"
    )

    model_config = {"str_strip_whitespace": True}


class LLMResponse(BaseModel):
    """Response model from LLM APIs."""

    text: str = Field(description="Generated text response")
    stop_reason: Optional[str] = Field(
        default=None, description="Reason generation stopped"
    )
    usage: Optional[Dict[str, Any]] = Field(
        default=None, description="Token usage information"
    )
    model: str = Field(description="Model that generated the response")

    model_config = {"str_strip_whitespace": True}


class LLMClient:
    """LLM client using direct Azure OpenAI or LiteLLM for other providers."""

    def __init__(
        self,
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
        timeout_seconds: int = None,
        debug: bool = None,
    ):
        """Initialize the LLM client.

        Args:
            model: Model string (format: provider/model_name)
            max_tokens: Maximum tokens for responses
            temperature: Temperature for responses
            timeout_seconds: Timeout for requests
            debug: Whether to enable debug mode
        """
        try:
            _configure_diagnostic_file_logging()

            # Use passed parameters or fall back to config.yml
            self.model = model if model is not None else config.get_model()
            self.max_tokens = (
                max_tokens
                if max_tokens is not None
                else config.get_int("max_tokens", 4096)
            )
            self.temperature = (
                temperature
                if temperature is not None
                else config.get_float("temperature", 0.1)
            )
            self.timeout_seconds = (
                timeout_seconds
                if timeout_seconds is not None
                else config.get_int("timeout_seconds", 300)
            )
            self.debug = debug if debug is not None else config.get_bool("debug", False)
            self.azure_deployment = self._get_azure_deployment()
            self._azure_client = None
            self.provider = self._get_provider()

            if self.provider == "azure":
                self._initialize_azure_client()

            # Let LiteLLM handle all credential validation
            logger.info(f"Initialized LLM client with model: {self.model}")

            # Print all parameters for debugging
            logger.info("=== LLM Client Configuration ===")
            logger.info(f"  Model: {self.model}")
            logger.info(f"  Max Tokens: {self.max_tokens}")
            logger.info(f"  Temperature: {self.temperature}")
            logger.info(f"  Timeout: {self.timeout_seconds} seconds")
            logger.info(f"  Debug Mode: {self.debug}")

            # Print environment variables related to credentials
            logger.info("=== Environment Variables ===")
            logger.info(f"  AWS_REGION: {os.environ.get('AWS_REGION', 'Not set')}")
            logger.info(f"  AWS_PROFILE: {os.environ.get('AWS_PROFILE', 'Not set')}")
            logger.info(
                f"  ANTHROPIC_API_KEY: {'Set' if os.environ.get('ANTHROPIC_API_KEY') else 'Not set'}"
            )
            logger.info(
                f"  AZURE_OPENAI_API_KEY: {'Set' if os.environ.get('AZURE_OPENAI_API_KEY') else 'Not set'}"
            )
            logger.info(
                f"  AZURE_OPENAI_ENDPOINT: {'Set' if os.environ.get('AZURE_OPENAI_ENDPOINT') else 'Not set'}"
            )

        except Exception as e:
            logger.error(f"Failed to initialize LLM client: {e}")
            raise

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - cleanup any open sessions."""
        # LiteLLM should handle session cleanup internally
        # but we can add explicit cleanup if needed
        pass

    def _get_provider(self) -> str:
        """Determine whether this client should use direct Azure OpenAI access."""
        normalized_model = self.model.strip().lower()
        if normalized_model.startswith("azure/"):
            return "azure"
        if normalized_model.startswith("bedrock/"):
            return "bedrock"
        if normalized_model.startswith("anthropic/") or "claude" in normalized_model:
            return "anthropic"
        if normalized_model.startswith("openai/"):
            return "openai"
        if (
            os.getenv("AZURE_OPENAI_API_KEY")
            and os.getenv("AZURE_OPENAI_ENDPOINT")
        ) or (os.getenv("AZURE_API_KEY") and os.getenv("AZURE_API_BASE")):
            return "azure"
        return normalized_model.split("/", 1)[0] if "/" in normalized_model else "unknown"

    def _get_azure_deployment(self) -> str:
        """Get the Azure deployment name without the optional provider prefix."""
        if self.model.lower().startswith("azure/"):
            return self.model.split("/", 1)[1]
        return self.model

    def _initialize_azure_client(self) -> None:
        """Initialize the official Azure OpenAI client using sibling-project settings."""
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("AZURE_API_BASE")
        api_key = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_API_KEY")
        api_version = (
            os.getenv("AZURE_API_VERSION")
            or os.getenv("AZURE_OPENAI_API_VERSION")
            or config.get_str("azure_api_version", "2024-10-21")
        )

        if not endpoint or not api_key:
            raise ValueError(
                "Azure OpenAI requires AZURE_OPENAI_ENDPOINT and "
                "AZURE_OPENAI_API_KEY"
            )

        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)

        self._azure_client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            timeout=self.timeout_seconds,
            max_retries=config.get_int("azure_max_retries", 0),
        )

    async def generate_text(self, request: LLMRequest) -> LLMResponse:
        """Generate text using the configured model via LiteLLM."""
        request_started_at = time.monotonic()
        prompt_chars = len(request.prompt)
        estimated_prompt_tokens = max(1, prompt_chars // 4)
        try:
            logger.info(
                "LLM request started: provider=%s model=%s prompt_chars=%d "
                "estimated_prompt_tokens=%d max_completion_tokens=%d timeout_seconds=%d",
                self.provider,
                self.model,
                prompt_chars,
                estimated_prompt_tokens,
                request.max_tokens,
                self.timeout_seconds,
            )

            if estimated_prompt_tokens >= 100_000:
                logger.warning(
                    "Large LLM request: estimated prompt size is %d tokens; "
                    "Azure processing may take several minutes",
                    estimated_prompt_tokens,
                )

            messages = [{"role": "user", "content": request.prompt}]
            if self.provider == "azure":
                params = {
                    "model": self.azure_deployment,
                    "messages": messages,
                    "max_completion_tokens": request.max_tokens,
                }
                if request.stop_sequences:
                    params["stop"] = request.stop_sequences

                logger.info(
                    "Azure OpenAI request sent: deployment=%s timeout_seconds=%d",
                    self.azure_deployment,
                    self.timeout_seconds,
                )
                response = await asyncio.to_thread(
                    self._azure_client.chat.completions.create, **params
                )
            else:
                params = {
                    "model": self.model,
                    "messages": messages,
                    "max_tokens": request.max_tokens,
                    "temperature": request.temperature,
                    "top_p": request.top_p,
                    "timeout": self.timeout_seconds,
                }

                if request.stop_sequences:
                    params["stop"] = request.stop_sequences

                logger.info("LiteLLM request sent: timeout_seconds=%d", self.timeout_seconds)
                response = await litellm.acompletion(**params)

            # Extract response data
            content = response.choices[0].message.content or ""
            finish_reason = response.choices[0].finish_reason

            # Extract usage and cost information
            usage = None
            if hasattr(response, "usage") and response.usage:
                prompt_tokens = response.usage.prompt_tokens
                completion_tokens = response.usage.completion_tokens
                total_tokens = response.usage.total_tokens

                usage = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }

                # Calculate cost using LiteLLM's completion_cost function
                try:
                    total_cost = litellm.completion_cost(completion_response=response)
                    if total_cost and total_cost > 0:
                        usage["total_cost_usd"] = total_cost

                        # Calculate cost per token to estimate prompt vs completion costs
                        if total_tokens > 0:
                            cost_per_token = total_cost / total_tokens
                            usage["prompt_cost_usd"] = prompt_tokens * cost_per_token
                            usage["completion_cost_usd"] = (
                                completion_tokens * cost_per_token
                            )
                except Exception as cost_error:
                    logger.debug(f"Could not calculate cost: {cost_error}")
                    # Fallback to direct attributes if available
                    if hasattr(response.usage, "prompt_tokens_cost_usd"):
                        usage["prompt_cost_usd"] = response.usage.prompt_tokens_cost_usd
                    if hasattr(response.usage, "completion_tokens_cost_usd"):
                        usage["completion_cost_usd"] = (
                            response.usage.completion_tokens_cost_usd
                        )
                    if hasattr(response.usage, "total_cost_usd"):
                        usage["total_cost_usd"] = response.usage.total_cost_usd

            # Create response
            llm_response = LLMResponse(
                text=content, stop_reason=finish_reason, usage=usage, model=self.model
            )

            elapsed_seconds = time.monotonic() - request_started_at
            logger.info(
                "LLM request completed: elapsed_seconds=%.1f response_chars=%d "
                "finish_reason=%s",
                elapsed_seconds,
                len(content),
                finish_reason,
            )

            # Log usage and cost information
            if usage:
                logger.info("📊 Token Usage:")
                logger.info(f"   Prompt tokens: {usage.get('prompt_tokens', 'N/A')}")
                logger.info(
                    f"   Completion tokens: {usage.get('completion_tokens', 'N/A')}"
                )
                logger.info(f"   Total tokens: {usage.get('total_tokens', 'N/A')}")

                if "total_cost_usd" in usage:
                    logger.info("💰 Cost Information:")
                    if "prompt_cost_usd" in usage:
                        logger.info(f"   Prompt cost: ${usage['prompt_cost_usd']:.6f}")
                    if "completion_cost_usd" in usage:
                        logger.info(
                            f"   Completion cost: ${usage['completion_cost_usd']:.6f}"
                        )
                    logger.info(f"   Total cost: ${usage['total_cost_usd']:.6f}")

            logger.info("Successfully generated text response")
            return llm_response

        except BaseException as e:
            elapsed_seconds = time.monotonic() - request_started_at
            status_code = getattr(e, "status_code", None)
            request_id = getattr(e, "request_id", None)
            response = getattr(e, "response", None)
            if response is not None:
                request_id = request_id or response.headers.get("x-request-id")
            retry_after = None
            if response is not None:
                retry_after = response.headers.get("retry-after")

            logger.exception(
                "LLM request failed: elapsed_seconds=%.1f provider=%s model=%s "
                "prompt_chars=%d timeout_seconds=%d error_type=%s error=%s",
                elapsed_seconds,
                self.provider,
                self.model,
                prompt_chars,
                self.timeout_seconds,
                type(e).__name__,
                e,
            )
            logger.error(
                "LLM API diagnostics: status_code=%s request_id=%s retry_after=%s "
                "is_timeout=%s is_rate_limit=%s is_connection_error=%s",
                status_code,
                request_id,
                retry_after,
                isinstance(e, openai.APITimeoutError),
                isinstance(e, openai.RateLimitError),
                isinstance(e, openai.APIConnectionError),
            )
            raise
        finally:
            logger.info(
                "LLM request ended: elapsed_seconds=%.1f",
                time.monotonic() - request_started_at,
            )

    def test_connection(self) -> bool:
        """Test the connection with a simple request."""
        try:
            logger.info(f"Testing connection for model: {self.model}")

            messages = [
                {
                    "role": "user",
                    "content": "Hello, please respond with 'Connection successful' to test the API.",
                }
            ]
            if self.provider == "azure":
                response = self._azure_client.chat.completions.create(
                    model=self.azure_deployment,
                    messages=messages,
                    max_completion_tokens=50,
                    timeout=30,
                )
            else:
                response = litellm.completion(
                    model=self.model,
                    messages=messages,
                    max_tokens=50,
                    temperature=0.1,
                    timeout=30,
                )

            content = response.choices[0].message.content or ""
            logger.info(f"Connection test successful: {content[:100]}")
            return True

        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model."""
        try:
            # Extract provider from model string using LiteLLM convention
            # LiteLLM uses format: "provider/model_name" or just "model_name"
            model_info = {
                "model": self.model,
                "provider": self.provider,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "timeout_seconds": self.timeout_seconds,
            }

            logger.info(f"Retrieved model info: {model_info}")
            return model_info
        except Exception as e:
            logger.error(f"Failed to get model info: {e}")
            raise


@lru_cache(maxsize=8)
def _create_llm_client_cached(
    model: str, max_tokens: int, temperature: float, timeout_seconds: int, debug: bool
) -> LLMClient:
    """Create a cached LLM client instance with specific parameters.

    This internal function uses lru_cache to cache client instances
    based on their configuration parameters.
    """
    client = LLMClient(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        debug=debug,
    )
    logger.info(f"Created cached LLM client with model: {model}")
    return client


def get_llm_client(
    model: str = None,
    max_tokens: int = None,
    temperature: float = None,
    timeout_seconds: int = None,
    debug: bool = None,
) -> LLMClient:
    """Get an LLM client instance with the specified configuration.

    This function returns a cached client instance if one exists with
    the same parameters, otherwise creates a new one. Parameters default
    to values from config.yml if not provided.

    Args:
        model: Model string (format: provider/model_name)
        max_tokens: Maximum tokens for responses
        temperature: Temperature for responses
        timeout_seconds: Timeout for requests
        debug: Whether to enable debug mode

    Returns:
        Configured LLM client instance
    """
    # Use provided parameters or fall back to config defaults
    actual_model = model if model is not None else config.get_model()
    actual_max_tokens = (
        max_tokens if max_tokens is not None else config.get_int("max_tokens", 4096)
    )
    actual_temperature = (
        temperature if temperature is not None else config.get_float("temperature", 0.1)
    )
    actual_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else config.get_int("timeout_seconds", 300)
    )
    actual_debug = debug if debug is not None else config.get_bool("debug", False)

    # Get cached client or create new one
    return _create_llm_client_cached(
        model=actual_model,
        max_tokens=actual_max_tokens,
        temperature=actual_temperature,
        timeout_seconds=actual_timeout,
        debug=actual_debug,
    )


async def cleanup_llm_client():
    """Clear the LLM client cache.

    This clears all cached client instances. Individual clients
    should handle their own cleanup through context managers.
    """
    logger.info("Clearing LLM client cache...")
    _create_llm_client_cached.cache_clear()
    logger.info("LLM client cache cleared")


if __name__ == "__main__":
    """Standalone testing of LLM client."""
    import asyncio

    async def test_llm_client():
        """Test the LLM client functionality."""
        logger.info("Testing LLM client...")

        try:
            # Test client creation
            client = LLMClient()
            logger.info(f"Created client for model: {client.model}")

            # Test connection
            connection_ok = client.test_connection()
            logger.info(f"Connection test result: {connection_ok}")

            # Get model info
            model_info = client.get_model_info()
            logger.info(f"Model info: {model_info}")

            # Test text generation (only if connection works)
            if connection_ok:
                test_request = LLMRequest(
                    prompt="Evaluate this simple OpenAPI operation: GET /users/{id}. Provide a brief assessment.",
                    max_tokens=200,
                    temperature=0.1,
                )

                response = await client.generate_text(test_request)
                logger.info(
                    f"Generated response from {response.model}: {response.text[:200]}..."
                )

            logger.info("LLM client test completed successfully")

        except Exception as e:
            logger.error(f"LLM client test failed: {e}")
            raise

    # Run the test
    asyncio.run(test_llm_client())
