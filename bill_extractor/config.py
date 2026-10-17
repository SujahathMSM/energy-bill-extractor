"""Runtime settings, read from environment variables (or a .env file)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BackendName = Literal["mock", "anthropic", "openai", "ollama"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Which model provider to call.
    backend: BackendName = Field("mock", alias="BILLX_BACKEND")

    # Anthropic (Claude)
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-sonnet-5-5", alias="BILLX_ANTHROPIC_MODEL")
    # prompt (JSON requested in the prompt; default) | json_schema (structured output) |
    # tool (forced tool call). A rejected mode falls back to json_schema, tool, then prompt.
    anthropic_output_mode: Literal["json_schema", "tool", "prompt"] = Field(
        "prompt", alias="BILLX_ANTHROPIC_OUTPUT_MODE"
    )

    # OpenAI or any OpenAI-compatible server (vLLM, LM Studio, ...)
    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(None, alias="OPENAI_BASE_URL")
    openai_model: str | None = Field(None, alias="BILLX_OPENAI_MODEL")
    openai_response_format: Literal["json_schema", "json_object", "none"] = Field(
        "json_schema", alias="BILLX_OPENAI_RESPONSE_FORMAT"
    )

    # Ollama (uses its OpenAI-compatible endpoint)
    ollama_base_url: str = Field("http://localhost:11434/v1", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field("qwen2.5vl:3b", alias="BILLX_OLLAMA_MODEL")

    # Where `python -m bill_extractor` listens. docker compose publishes BILLX_PORT too.
    host: str = Field("127.0.0.1", alias="BILLX_HOST")
    port: int = Field(8000, alias="BILLX_PORT", ge=1, le=65535)

    # Input handling
    max_pages: int = Field(3, alias="BILLX_MAX_PAGES", ge=1, le=10)
    # 1568 px on the long side: Claude downscales anything larger anyway, and it is
    # enough resolution for bill text at A4.
    max_image_side: int = Field(1568, alias="BILLX_MAX_IMAGE_SIDE", ge=512, le=4096)
    max_upload_mb: float = Field(10.0, alias="BILLX_MAX_UPLOAD_MB", gt=0)

    # Model calls
    max_attempts: int = Field(2, alias="BILLX_MAX_ATTEMPTS", ge=1, le=5)
    timeout_s: float = Field(90.0, alias="BILLX_TIMEOUT_S", gt=0)
    max_output_tokens: int = Field(4096, alias="BILLX_MAX_OUTPUT_TOKENS", ge=256)


@lru_cache
def get_settings() -> Settings:
    return Settings()
