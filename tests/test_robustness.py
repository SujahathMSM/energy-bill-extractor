"""Hardening: strict schema, JSON repair, input handling, error mapping, API guards."""

import asyncio
import importlib
import io
import json
import random

import anthropic
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from reportlab.pdfgen import canvas

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from bill_extractor import api as api_module
from bill_extractor.backends import (
    BackendRateLimitError,
    BackendResponse,
    BackendTimeoutError,
    MalformedReplyError,
    VLMBackend,
    parse_json_object,
)
from bill_extractor.backends.anthropic_backend import AnthropicBackend
from bill_extractor.backends.mock_backend import MOCK_BILL, SAMPLES_DIR, MockBackend
from bill_extractor.backends.openai_backend import OpenAICompatibleBackend
from bill_extractor.config import get_settings
from bill_extractor.extractor import BillExtractor, ExtractionError
from bill_extractor.preprocess import (
    HEIC_SUPPORTED,
    ImageTooLargeError,
    UnsupportedFileError,
    prepare,
    sharpness,
)
from bill_extractor.schema import BillExtraction
from bill_extractor.synth import make_bill, photo_variant, rasterise, render_pdf
from bill_extractor.validation import analyse


def _png(size=(1240, 1754), colour="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


def _jpeg(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _pdf(pages: int = 1, encrypt: str | None = None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, encrypt=encrypt)
    for n in range(pages):
        c.drawString(72, 720, f"Page {n + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


# --- strict schema -----------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "code"), [("£", "GBP"), (" gbp ", "GBP"), ("euros", "EUR")])
def test_currency_aliases_normalise_to_iso(raw, code):
    assert BillExtraction(currency=raw).currency == code


@pytest.mark.parametrize("raw", ["pounds sterling", "GB", "12$"])
def test_non_iso_currency_is_rejected(raw):
    with pytest.raises(ValidationError, match="ISO 4217"):
        BillExtraction(currency=raw)


@pytest.mark.parametrize(
    "fields",
    [
        {"consumption_kwh": float("nan")},
        {"total_amount": float("inf")},
        {"consumption_kwh": -5},
        {"standing_charge": -1},
        {"period_days": 0},
        {"unit_rates": [{"rate": -24.86, "rate_unit": "p/kWh"}]},
        {"unit_rates": [{"rate": 24.86, "rate_unit": "  "}]},
        {"billing_period_start": "sometime in August"},
    ],
)
def test_schema_rejects_impossible_values(fields):
    with pytest.raises(ValidationError):
        BillExtraction.model_validate(fields)


def test_schema_allows_credits_and_trims_strings():
    ex = BillExtraction.model_validate(
        {
            "supplier_name": "  Fernwick Energy ",
            "total_amount": -12.5,  # a credit bill
            "other_charges": [{"label": "Discount", "amount": -3.0}],
        }
    )
    assert ex.supplier_name == "Fernwick Energy"
    assert ex.total_amount == -12.5


# --- checks name the fields to review ---------------------------------------------------


def test_failed_check_names_the_fields_involved():
    ex = BillExtraction.model_validate({**MOCK_BILL, "total_amount": 99.99})
    _, checks = analyse(ex)
    total = next(c for c in checks if c.name == "total_reconciles")
    assert total.status == "fail"
    assert "total_amount" in total.fields and "tax_amount" in total.fields


def test_result_lists_flagged_fields(bill):
    _, pdf = bill
    reply = {**MOCK_BILL, "meter_point_id": "1600123456780", "supplier_name": None}
    result = asyncio.run(BillExtractor(_Scripted([reply])).extract(pdf))
    assert result.needs_review
    assert "meter_point_id" in result.flagged_fields
    assert "supplier_name" in result.flagged_fields
    assert "total_amount" not in result.flagged_fields


# --- JSON repair path ------------------------------------------------------------------


class _Scripted(VLMBackend):
    """Replies in order; an Exception in the queue is raised instead of returned."""

    name = "scripted"
    model = "scripted-test"

    def __init__(self, replies):
        self.replies = list(replies)
        self.feedback = []

    async def extract(self, pages, feedback=None):
        self.feedback.append(feedback)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return BackendResponse(data=reply, input_tokens=10, output_tokens=5)


def test_invalid_json_gets_one_repair_attempt(bill):
    _, pdf = bill
    backend = _Scripted([MalformedReplyError("The model reply was not valid JSON."), MOCK_BILL])
    result = asyncio.run(BillExtractor(backend, max_attempts=2).extract(pdf))
    assert result.meta.attempts == 2
    assert "not valid JSON" in backend.feedback[1]
    assert "single JSON object" in backend.feedback[1]


def test_invalid_json_twice_is_an_extraction_error(bill):
    _, pdf = bill
    broken = MalformedReplyError("The model reply did not contain a JSON object.")
    backend = _Scripted([broken, broken])
    with pytest.raises(ExtractionError, match="JSON object"):
        asyncio.run(BillExtractor(backend, max_attempts=2).extract(pdf))


@pytest.mark.parametrize(
    "text", ["", "Sorry, I can't read that.", '{"supplier_name": "X"', "[1, 2]", "{bad json}"]
)
def test_unparseable_replies_raise_malformed(text):
    with pytest.raises(MalformedReplyError):
        parse_json_object(text)


# --- input handling --------------------------------------------------------------------


def test_password_protected_pdf_has_a_clear_message():
    with pytest.raises(UnsupportedFileError, match="password"):
        prepare(_pdf(encrypt="secret"))


def test_truncated_pdf_is_flagged_in_warnings():
    doc = prepare(_pdf(pages=5), max_pages=3)
    assert len(doc.pages) == 3 and doc.pages_in_file == 5
    assert "first 3 of 5 pages" in doc.warnings[0]


def test_pixel_cap_stops_decompression_bombs():
    with pytest.raises(ImageTooLargeError, match="megapixels"):
        prepare(_png(size=(3000, 3000)), max_megapixels=1)


def test_truncated_image_is_unsupported():
    with pytest.raises(UnsupportedFileError, match="damaged"):
        prepare(_png()[:200])


@pytest.mark.skipif(not HEIC_SUPPORTED, reason="pillow-heif not installed")
def test_heic_photo_is_converted_to_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), "white").save(buf, format="HEIF")
    doc = prepare(buf.getvalue())
    assert doc.source_type == "heic"
    assert doc.pages[0].media_type == "image/jpeg"


def test_severe_blur_scores_below_threshold_and_mild_above():
    scan = rasterise(render_pdf(make_bill(0, random.Random(1))))
    mild = photo_variant(scan, random.Random(2), level="mild")
    severe = photo_variant(scan, random.Random(2), level="severe")
    assert sharpness(mild) > 2.0 > sharpness(severe)


def test_blurry_photo_fails_input_quality_and_needs_review():
    scan = rasterise(render_pdf(make_bill(0, random.Random(1))))
    data = _jpeg(photo_variant(scan, random.Random(2), level="severe"), quality=30)
    result = asyncio.run(BillExtractor(_Scripted([MOCK_BILL])).extract(data))
    quality = next(c for c in result.checks if c.name == "input_quality")
    assert quality.status == "fail" and "blurry" in quality.detail
    assert result.needs_review


def test_small_image_fails_input_quality():
    result = asyncio.run(BillExtractor(_Scripted([MOCK_BILL])).extract(_png(size=(600, 800))))
    quality = next(c for c in result.checks if c.name == "input_quality")
    assert quality.status == "fail" and "low resolution" in quality.detail


def test_pdf_input_quality_is_skipped(bill):
    _, pdf = bill
    result = asyncio.run(BillExtractor(_Scripted([MOCK_BILL])).extract(pdf))
    quality = next(c for c in result.checks if c.name == "input_quality")
    assert quality.status == "skipped"
    assert not result.needs_review


def test_blur_check_can_be_disabled():
    scan = rasterise(render_pdf(make_bill(0, random.Random(1))))
    data = _jpeg(photo_variant(scan, random.Random(2), level="severe"), quality=30)
    extractor = BillExtractor(_Scripted([MOCK_BILL]), blur_threshold=0)
    result = asyncio.run(extractor.extract(data))
    assert next(c for c in result.checks if c.name == "input_quality").status == "pass"


# --- mock backend replays the bundled samples -------------------------------------------


def test_mock_returns_ground_truth_for_bundled_samples():
    truth = json.loads((SAMPLES_DIR / "truth.json").read_text())
    extractor = BillExtractor(MockBackend())
    for name, expected in truth.items():
        result = asyncio.run(extractor.extract((SAMPLES_DIR / name).read_bytes()))
        assert result.extraction.supplier_name == expected["supplier_name"]
        assert result.extraction.total_amount == expected["total_amount"]


def test_mock_returns_fixed_bill_for_other_files(bill):
    _, pdf = bill
    result = asyncio.run(BillExtractor(MockBackend()).extract(pdf))
    assert result.extraction.supplier_name == MOCK_BILL["supplier_name"]


# --- provider errors: retry with backoff, then a specific error -----------------------


def _anthropic_client(handler, retries=0):
    return AsyncAnthropic(
        api_key="test",
        max_retries=retries,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _claude_message() -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": json.dumps(MOCK_BILL)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1500, "output_tokens": 300},
    }


def _overloaded():
    return httpx.Response(
        529,
        headers={"retry-after-ms": "1"},
        json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )


def test_transient_errors_are_retried_by_the_sdk(bill):
    calls = []

    def handler(request):
        calls.append(1)
        return _overloaded() if len(calls) == 1 else httpx.Response(200, json=_claude_message())

    backend = AnthropicBackend(model="claude-test", client=_anthropic_client(handler, retries=2))
    response = asyncio.run(backend.extract(prepare(bill[1]).pages))
    assert len(calls) == 2
    assert response.data["supplier_name"] == MOCK_BILL["supplier_name"]


def test_persistent_overload_becomes_rate_limit_error(bill):
    backend = AnthropicBackend(
        model="claude-test", client=_anthropic_client(lambda r: _overloaded(), retries=1)
    )
    with pytest.raises(BackendRateLimitError):
        asyncio.run(backend.extract(prepare(bill[1]).pages))


def test_claude_timeout_becomes_timeout_error(bill):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    backend = AnthropicBackend(model="claude-test", client=_anthropic_client(handler))
    with pytest.raises(BackendTimeoutError):
        asyncio.run(backend.extract(prepare(bill[1]).pages))


def test_openai_rate_limit_becomes_rate_limit_error(bill):
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    client = AsyncOpenAI(
        api_key="x",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    backend = OpenAICompatibleBackend("openai", "m", client=client)
    with pytest.raises(BackendRateLimitError):
        asyncio.run(backend.extract(prepare(bill[1]).pages))


def test_anthropic_sdk_error_classes_exist():
    # The backend relies on these names; fail loudly if an SDK upgrade renames them.
    assert issubclass(anthropic.OverloadedError, anthropic.APIStatusError)
    assert issubclass(anthropic.APITimeoutError, anthropic.APIConnectionError)


# --- API guards ------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BILLX_BACKEND", "mock")
    monkeypatch.setenv("BILLX_MAX_UPLOAD_MB", "1")
    monkeypatch.setenv("BILLX_MAX_IMAGE_MEGAPIXELS", "4")
    get_settings.cache_clear()
    with TestClient(api_module.app) as test_client:
        yield test_client
    get_settings.cache_clear()


class _FailingBackend(VLMBackend):
    name = "failing"
    model = "failing-test"

    def __init__(self, exc):
        self.exc = exc

    async def extract(self, pages, feedback=None):
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (BackendTimeoutError("slow"), 504),
        (BackendRateLimitError("busy"), 503),
        (MalformedReplyError("not JSON"), 422),
    ],
)
def test_provider_failures_map_to_clear_status_codes(client, bill, exc, status):
    client.app.state.extractor = BillExtractor(_FailingBackend(exc))
    response = client.post("/v1/bills/extract", files={"file": ("b.pdf", bill[1])})
    assert response.status_code == status
    assert isinstance(response.json()["detail"], str)
    assert response.headers["x-request-id"]
    if status == 503:
        assert response.headers["retry-after"] == "30"


def test_too_many_pixels_is_413(client):
    response = client.post("/v1/bills/extract", files={"file": ("big.png", _png((2500, 2500)))})
    assert response.status_code == 413
    assert "megapixels" in response.json()["detail"]


def test_oversized_body_is_rejected_before_parsing(client):
    big = b"%PDF" + b"0" * (2 * 1024 * 1024)
    response = client.post("/v1/bills/extract", files={"file": ("big.pdf", big)})
    assert response.status_code == 413


def test_missing_file_field_has_a_readable_message(client):
    response = client.post("/v1/bills/extract", data={"other": "x"})
    assert response.status_code == 422
    assert response.json()["detail"] == 'Attach the bill as a multipart form field named "file".'


def test_savings_validation_error_is_readable(client):
    response = client.post("/v1/savings", json={"extraction": {}})
    assert response.status_code == 422
    assert response.json()["detail"].startswith("Invalid request. target")


def test_unsafe_request_id_is_replaced(client, bill):
    response = client.post(
        "/v1/bills/extract",
        files={"file": ("b.pdf", bill[1])},
        headers={"X-Request-ID": 'evil id" injected'},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] != 'evil id" injected'
    assert response.json()["meta"]["request_id"] == response.headers["x-request-id"]


def test_cors_is_off_by_default(client):
    response = client.options(
        "/v1/bills/extract",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_configured_origins(monkeypatch):
    monkeypatch.setenv("BILLX_BACKEND", "mock")
    monkeypatch.setenv("BILLX_CORS_ORIGINS", "https://app.example.com")
    get_settings.cache_clear()
    module = importlib.reload(api_module)
    try:
        with TestClient(module.app) as test_client:
            ok = test_client.options(
                "/v1/bills/extract",
                headers={
                    "Origin": "https://app.example.com",
                    "Access-Control-Request-Method": "POST",
                },
            )
            other = test_client.get("/health", headers={"Origin": "https://evil.example"})
        assert ok.headers["access-control-allow-origin"] == "https://app.example.com"
        assert "access-control-allow-origin" not in other.headers
    finally:
        monkeypatch.delenv("BILLX_CORS_ORIGINS")
        get_settings.cache_clear()
        importlib.reload(api_module)


def test_logs_never_contain_bill_contents(client, caplog):
    sample = SAMPLES_DIR / "bill_000.pdf"
    truth = json.loads((SAMPLES_DIR / "truth.json").read_text())["bill_000.pdf"]
    with caplog.at_level("DEBUG", logger="bill_extractor"):
        response = client.post("/v1/bills/extract", files={"file": ("b.pdf", sample.read_bytes())})
    assert response.status_code == 200
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "bill_extracted" in logged
    for value in (truth["supplier_name"], truth["account_number"], truth["meter_point_id"]):
        assert value not in logged
