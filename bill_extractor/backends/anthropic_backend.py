"""Claude backend.

Three ways to get JSON back:

1. ``prompt`` (default): the schema is written into the prompt and the reply is parsed as JSON.
   Works with any model; the reply is then validated against the schema, with one retry.
2. ``json_schema``: structured output (``output_config.format``) constrains the reply to the
   schema. With claude-sonnet-5-5 this schema fails to compile ("Schema is too complex") after
   about two minutes, so it is opt-in.
3. ``tool``: a forced call to a tool whose input schema is the bill schema. claude-sonnet-5-5
   rejects forced tool calls; older models accept them.

If the API rejects the chosen mode with a 400 error, the backend moves down this list
(json_schema, tool, prompt) and remembers the mode that worked.
"""

import copy
import logging
import re
from typing import Literal

import anthropic
from anthropic import AsyncAnthropic

from ..preprocess import PageImage
from ..prompts import SYSTEM_PROMPT, TOOL_NAME, build_user_prompt
from ..schema import BillExtraction, extraction_json_schema
from .base import BackendError, BackendResponse, VLMBackend, parse_json_object

log = logging.getLogger(__name__)

Mode = Literal["json_schema", "tool", "prompt"]
MODES: tuple[Mode, ...] = ("json_schema", "tool", "prompt")

# Structured-output limits per request (Claude docs, "Schema complexity limits").
MAX_UNION_PARAMS = 16
MAX_OPTIONAL_PARAMS = 24

_DEFAULT_NOTE = re.compile(r"\n\n\{default: [^}]*\}$")


def _drop_null(prop: dict) -> dict:
    """``anyOf: [X, null]`` -> ``X``, keeping the description and title."""
    variants = prop.get("anyOf")
    if not isinstance(variants, list):
        return prop
    non_null = [v for v in variants if v.get("type") != "null"]
    if len(non_null) != 1 or len(non_null) == len(variants):
        return prop
    merged = {**non_null[0], **{k: v for k, v in prop.items() if k != "anyOf"}}
    if "description" in merged:
        merged["description"] = _DEFAULT_NOTE.sub("", merged["description"])
    return merged


def _optional_not_nullable(schema: dict) -> dict:
    for name, prop in list(schema.get("properties", {}).items()):
        if name not in schema.get("required", []):
            schema["properties"][name] = _drop_null(prop)
    for definition in schema.get("$defs", {}).values():
        _optional_not_nullable(definition)
    return schema


def structured_output_schema() -> dict:
    """The bill schema in the form Claude's structured output accepts.

    Every bill field is nullable, which puts the schema over the limit of 16 union-typed
    parameters. Fields that are not printed are optional instead: the model leaves them out
    and Pydantic fills in None. That leaves no union types and 20 optional parameters.
    """
    return _optional_not_nullable(copy.deepcopy(anthropic.transform_schema(BillExtraction)))


def schema_complexity(schema: dict) -> tuple[int, int]:
    """Count (union-typed parameters, optional parameters) the way the API limits them."""
    unions = optional = 0
    objects = [schema, *schema.get("$defs", {}).values()]
    for obj in objects:
        required = set(obj.get("required", []))
        for name, prop in obj.get("properties", {}).items():
            if "anyOf" in prop or isinstance(prop.get("type"), list):
                unions += 1
            if name not in required:
                optional += 1
    return unions, optional


class AnthropicBackend(VLMBackend):
    name = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        timeout: float = 90.0,
        max_tokens: int = 4096,
        mode: Mode = "prompt",
        client: AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.mode: Mode = mode
        self.client = client or AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.output_schema = structured_output_schema()
        self.tool = {
            "name": TOOL_NAME,
            "description": "Record the fields read from an electricity bill.",
            "input_schema": extraction_json_schema(),
        }

    def _params(self, mode: Mode, pages: list[PageImage], feedback: str | None) -> dict:
        content: list[dict] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": page.media_type, "data": page.b64},
            }
            for page in pages
        ]
        prompt = build_user_prompt(len(pages), feedback, include_schema=mode == "prompt")
        content.append({"type": "text", "text": prompt})
        params: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
        }
        if mode == "json_schema":
            params["output_config"] = {
                "format": {"type": "json_schema", "schema": self.output_schema}
            }
        elif mode == "tool":
            params["tools"] = [self.tool]
            params["tool_choice"] = {"type": "tool", "name": TOOL_NAME}
        return params

    @staticmethod
    def _read(mode: Mode, message) -> dict:
        if message.stop_reason == "refusal":
            raise BackendError("Claude declined to read this document.")
        if mode == "tool":
            block = next((b for b in message.content if b.type == "tool_use"), None)
            if block is not None and isinstance(block.input, dict):
                return dict(block.input)
        else:
            text = "".join(b.text for b in message.content if b.type == "text")
            if text.strip():
                return parse_json_object(text)
        if message.stop_reason == "max_tokens":
            raise BackendError(
                "Claude hit the output token limit before answering; raise BILLX_MAX_OUTPUT_TOKENS."
            )
        raise BackendError(f"Claude returned no usable answer (stop_reason={message.stop_reason}).")

    async def extract(self, pages: list[PageImage], feedback: str | None = None) -> BackendResponse:
        start = MODES.index(self.mode)
        for mode in MODES[start:]:
            try:
                message = await self.client.messages.create(**self._params(mode, pages, feedback))
            except anthropic.BadRequestError as exc:
                if mode == MODES[-1]:
                    raise BackendError(f"Claude request failed: {exc}") from exc
                log.warning("Claude rejected %s output (%s); trying the next mode", mode, exc)
                continue
            except anthropic.APIError as exc:
                raise BackendError(f"Claude request failed: {exc}") from exc

            if mode != self.mode:
                log.warning("Using %s output for %s from now on", mode, self.model)
                self.mode = mode
            return BackendResponse(
                data=self._read(mode, message),
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
            )
        raise BackendError("No output mode left to try.")  # unreachable

    async def aclose(self) -> None:
        await self.client.close()
