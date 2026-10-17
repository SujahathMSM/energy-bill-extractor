"""OpenAI-compatible backend: OpenAI itself, Ollama, vLLM, LM Studio and similar servers."""

import logging

import openai
from openai import AsyncOpenAI

from ..preprocess import PageImage
from ..prompts import SYSTEM_PROMPT, build_user_prompt
from ..schema import extraction_json_schema
from .base import BackendError, BackendResponse, VLMBackend, parse_json_object

log = logging.getLogger(__name__)


class OpenAICompatibleBackend(VLMBackend):
    def __init__(
        self,
        name: str,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        response_format: str = "json_schema",
        timeout: float = 90.0,
        extra_params: dict | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.response_format = response_format
        # Local servers ignore the key, but the SDK insists on one.
        self.client = client or AsyncOpenAI(
            base_url=base_url, api_key=api_key or "not-needed", timeout=timeout, max_retries=2
        )
        self.extra_params = extra_params or {}

    def _response_format(self) -> dict | None:
        if self.response_format == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {"name": "bill_extraction", "schema": extraction_json_schema()},
            }
        if self.response_format == "json_object":
            return {"type": "json_object"}
        return None

    async def _create(self, pages: list[PageImage], feedback: str | None):
        content: list[dict] = [
            {"type": "text", "text": build_user_prompt(len(pages), feedback, include_schema=True)}
        ]
        content += [{"type": "image_url", "image_url": {"url": page.data_url}} for page in pages]
        params: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            **self.extra_params,
        }
        response_format = self._response_format()
        if response_format:
            params["response_format"] = response_format
        return await self.client.chat.completions.create(**params)

    async def extract(self, pages: list[PageImage], feedback: str | None = None) -> BackendResponse:
        try:
            completion = await self._create(pages, feedback)
        except openai.BadRequestError as exc:
            # Some servers do not support JSON-schema output. Fall back once to plain JSON mode;
            # the schema is in the prompt and the reply is validated either way.
            if self.response_format != "json_schema":
                raise BackendError(f"{self.name} request failed: {exc}") from exc
            log.warning("%s rejected json_schema output; falling back to json_object", self.name)
            self.response_format = "json_object"
            try:
                completion = await self._create(pages, feedback)
            except openai.APIError as retry_exc:
                raise BackendError(f"{self.name} request failed: {retry_exc}") from retry_exc
        except openai.APIError as exc:
            raise BackendError(f"{self.name} request failed: {exc}") from exc

        if not completion.choices:
            raise BackendError(f"{self.name} returned no choices.")
        data = parse_json_object(completion.choices[0].message.content)
        usage = completion.usage
        return BackendResponse(
            data=data,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )

    async def aclose(self) -> None:
        await self.client.close()
