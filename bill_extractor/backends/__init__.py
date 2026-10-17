"""Model backends and the factory that picks one from settings."""

from ..config import Settings
from .base import BackendError, BackendResponse, VLMBackend, parse_json_object

__all__ = [
    "BackendError",
    "BackendResponse",
    "VLMBackend",
    "build_backend",
    "parse_json_object",
]


def build_backend(settings: Settings) -> VLMBackend:
    if settings.backend == "mock":
        from .mock_backend import MockBackend

        return MockBackend()

    if settings.backend == "anthropic":
        from .anthropic_backend import AnthropicBackend

        if not settings.anthropic_api_key:
            raise RuntimeError("Set ANTHROPIC_API_KEY to use the anthropic backend.")
        return AnthropicBackend(
            model=settings.anthropic_model,
            api_key=settings.anthropic_api_key,
            timeout=settings.timeout_s,
            max_tokens=settings.max_output_tokens,
            mode=settings.anthropic_output_mode,
        )

    from .openai_backend import OpenAICompatibleBackend

    if settings.backend == "ollama":
        return OpenAICompatibleBackend(
            name="ollama",
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            response_format=settings.openai_response_format,
            timeout=settings.timeout_s,
            # Deterministic decoding; local servers accept these parameters.
            extra_params={"temperature": 0, "max_tokens": settings.max_output_tokens},
        )

    if not settings.openai_model:
        raise RuntimeError("Set BILLX_OPENAI_MODEL to use the openai backend.")
    if not settings.openai_api_key and not settings.openai_base_url:
        raise RuntimeError("Set OPENAI_API_KEY (or OPENAI_BASE_URL for a local server).")
    # Parameters such as temperature are left at provider defaults: some hosted models reject
    # them. Set them per deployment if your model accepts them.
    return OpenAICompatibleBackend(
        name="openai",
        model=settings.openai_model,
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        response_format=settings.openai_response_format,
        timeout=settings.timeout_s,
    )
