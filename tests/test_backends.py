"""Check the exact requests sent to each provider, using the real SDKs with a fake transport."""

import asyncio
import json

import anthropic
import pytest

# Recent anthropic/openai SDKs build on httpx2; older ones on httpx. Use whichever they use.
try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from bill_extractor.backends import BackendError
from bill_extractor.backends.anthropic_backend import (
    MAX_OPTIONAL_PARAMS,
    MAX_UNION_PARAMS,
    AnthropicBackend,
    schema_complexity,
    structured_output_schema,
)
from bill_extractor.backends.mock_backend import MOCK_BILL
from bill_extractor.backends.openai_backend import OpenAICompatibleBackend
from bill_extractor.preprocess import prepare
from bill_extractor.prompts import TOOL_NAME
from bill_extractor.schema import BillExtraction


def _pages(bill):
    return prepare(bill[1]).pages


def _message(content: list[dict], stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1500, "output_tokens": 300},
    }


def _anthropic(handler) -> AsyncAnthropic:
    return AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# The exact error claude-sonnet-5-5 returns for a forced tool call.
FORCED_TOOL_REJECTED = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": 'tool_choice: type "tool" and "any" are not supported for this model.',
    },
}


def test_anthropic_default_prompt_mode_sends_schema_and_images(bill):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_message([{"type": "text", "text": json.dumps(MOCK_BILL)}]))

    backend = AnthropicBackend(model="claude-test", client=_anthropic(handler))
    response = asyncio.run(backend.extract(_pages(bill)))

    body = seen["body"]
    assert backend.mode == "prompt"
    assert not {"output_config", "tools", "tool_choice"} & body.keys()
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert '"title":"BillExtraction"' in content[-1]["text"]
    assert response.data["supplier_name"] == "Fernwick Energy"
    assert (response.input_tokens, response.output_tokens) == (1500, 300)


def test_anthropic_structured_output_mode(bill):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_message([{"type": "text", "text": json.dumps(MOCK_BILL)}]))

    backend = AnthropicBackend(model="claude-test", mode="json_schema", client=_anthropic(handler))
    response = asyncio.run(backend.extract(_pages(bill)))

    body = seen["body"]
    assert "tool_choice" not in body and "tools" not in body
    output_format = body["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["schema"]["title"] == "BillExtraction"
    assert output_format["schema"]["additionalProperties"] is False
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[-1]["type"] == "text"
    assert response.data["supplier_name"] == "Fernwick Energy"
    assert (response.input_tokens, response.output_tokens) == (1500, 300)


def test_anthropic_falls_back_through_modes_and_remembers(bill):
    """No structured output, and forced tool calls rejected: ends up on prompt-only JSON."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mode = "json_schema" if "output_config" in body else "tool" if "tools" in body else "prompt"
        calls.append(mode)
        if mode == "json_schema":
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "output_config: not supported",
                    },
                },
            )
        if mode == "tool":
            return httpx.Response(400, json=FORCED_TOOL_REJECTED)
        assert "JSON schema" in body["messages"][0]["content"][-1]["text"]
        return httpx.Response(
            200,
            json=_message(
                [{"type": "text", "text": "```json\n" + json.dumps(MOCK_BILL) + "\n```"}]
            ),
        )

    backend = AnthropicBackend(model="claude-test", mode="json_schema", client=_anthropic(handler))
    first = asyncio.run(backend.extract(_pages(bill)))
    assert calls == ["json_schema", "tool", "prompt"]
    assert first.data["total_amount"] == 91.12
    assert backend.mode == "prompt"

    asyncio.run(backend.extract(_pages(bill)))
    assert calls[3:] == ["prompt"]  # no wasted requests after the first


def test_anthropic_tool_mode_reads_tool_input(bill):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        block = {"type": "tool_use", "id": "toolu_1", "name": TOOL_NAME, "input": MOCK_BILL}
        return httpx.Response(200, json=_message([block], stop_reason="tool_use"))

    backend = AnthropicBackend(model="claude-test", mode="tool", client=_anthropic(handler))
    response = asyncio.run(backend.extract(_pages(bill)))
    assert seen["body"]["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert response.data["supplier_name"] == "Fernwick Energy"


def test_anthropic_skips_thinking_blocks(bill):
    def handler(request: httpx.Request) -> httpx.Response:
        content = [
            {"type": "thinking", "thinking": "Reading the charges table...", "signature": "sig"},
            {"type": "text", "text": json.dumps(MOCK_BILL)},
        ]
        return httpx.Response(200, json=_message(content))

    backend = AnthropicBackend(model="claude-test", client=_anthropic(handler))
    assert asyncio.run(backend.extract(_pages(bill))).data["account_number"] == "40391827"


def test_anthropic_token_limit_gives_a_clear_error(bill):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_message([], stop_reason="max_tokens"))

    backend = AnthropicBackend(model="claude-test", client=_anthropic(handler))
    with pytest.raises(BackendError, match="BILLX_MAX_OUTPUT_TOKENS"):
        asyncio.run(backend.extract(_pages(bill)))


def test_anthropic_auth_error_is_not_retried_in_other_modes(bill):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid x-api-key"},
            },
        )

    backend = AnthropicBackend(model="claude-test", client=_anthropic(handler))
    with pytest.raises(BackendError, match="401"):
        asyncio.run(backend.extract(_pages(bill)))
    assert len(calls) == 1


def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 900, "completion_tokens": 200, "total_tokens": 1100},
    }


def test_openai_compatible_sends_schema_and_parses_fenced_json(bill):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("```json\n" + json.dumps(MOCK_BILL) + "\n```"))

    client = AsyncOpenAI(
        api_key="x", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    backend = OpenAICompatibleBackend(
        "ollama", "qwen-test", client=client, extra_params={"temperature": 0}
    )
    response = asyncio.run(backend.extract(_pages(bill)))

    body = seen["body"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["temperature"] == 0
    user = body["messages"][1]["content"]
    assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert response.data["total_amount"] == 91.12
    assert response.input_tokens == 900


def test_openai_compatible_falls_back_when_json_schema_is_rejected(bill):
    formats = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        formats.append(body.get("response_format", {}).get("type"))
        if formats[-1] == "json_schema":
            return httpx.Response(400, json={"error": {"message": "json_schema not supported"}})
        return httpx.Response(200, json=_completion(json.dumps(MOCK_BILL)))

    client = AsyncOpenAI(
        api_key="x",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    backend = OpenAICompatibleBackend("local", "m", client=client)
    asyncio.run(backend.extract(_pages(bill)))
    assert formats == ["json_schema", "json_object"]
    assert backend.response_format == "json_object"  # remembered for later calls


def test_openai_compatible_server_error_becomes_backend_error(bill):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    client = AsyncOpenAI(
        api_key="x",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    backend = OpenAICompatibleBackend("local", "m", client=client)
    with pytest.raises(BackendError):
        asyncio.run(backend.extract(_pages(bill)))


def test_structured_output_schema_is_within_claude_limits():
    """Claude rejected the plain schema: 17 union-typed parameters against a limit of 16."""
    unions, optional = schema_complexity(structured_output_schema())
    assert unions <= MAX_UNION_PARAMS
    assert optional <= MAX_OPTIONAL_PARAMS
    raw_unions, _ = schema_complexity(anthropic.transform_schema(BillExtraction))
    assert raw_unions > MAX_UNION_PARAMS  # the problem this works around


def test_structured_output_schema_keeps_types_and_descriptions():
    props = structured_output_schema()["properties"]
    assert props["billing_period_start"]["type"] == "string"
    assert props["billing_period_start"]["format"] == "date"
    assert props["total_amount"]["type"] == "number"
    assert "default" not in props["total_amount"]["description"]
    rate = structured_output_schema()["$defs"]["UnitRate"]
    assert set(rate["required"]) == {"rate", "rate_unit"}
    assert rate["properties"]["kwh"]["type"] == "number"


def test_omitted_fields_validate_as_none():
    """In structured-output mode the model omits unprinted fields instead of sending null."""
    sparse = {
        "supplier_name": "Fernwick Energy",
        "unit_rates": [{"rate": 24.86, "rate_unit": "p/kWh"}],
    }
    parsed = BillExtraction.model_validate(sparse)
    assert parsed.previous_reading is None
    assert parsed.unit_rates[0].kwh is None
