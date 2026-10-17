"""Backend interface and helpers shared by the model providers."""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..preprocess import PageImage


class BackendError(RuntimeError):
    """The model provider failed or returned something unusable."""


@dataclass
class BackendResponse:
    data: dict
    input_tokens: int | None = None
    output_tokens: int | None = None


class VLMBackend(ABC):
    name: str
    model: str

    @abstractmethod
    async def extract(self, pages: list[PageImage], feedback: str | None = None) -> BackendResponse:
        """Send page images to the model and return the raw field dictionary."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(text: str | None) -> dict:
    """Pull the first JSON object out of a model reply.

    Local models in particular wrap JSON in code fences or add a sentence before it.
    """
    if not text:
        raise BackendError("The model returned an empty reply.")
    cleaned = _FENCE.sub("", text.strip())
    start = cleaned.find("{")
    if start == -1:
        raise BackendError("The model reply did not contain a JSON object.")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(cleaned[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise BackendError(f"The model reply was not valid JSON: {exc}") from exc
                if not isinstance(value, dict):
                    raise BackendError("The model reply was not a JSON object.")
                return value
    raise BackendError("The model reply contained an unterminated JSON object.")
