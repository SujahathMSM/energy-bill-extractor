import asyncio

import pytest

from bill_extractor.backends import BackendError, parse_json_object
from bill_extractor.backends.mock_backend import MOCK_BILL
from bill_extractor.extractor import BillExtractor, ExtractionError, clean_raw
from tests.conftest import ScriptedBackend


def test_retry_feeds_validation_errors_back(bill):
    _, pdf = bill
    bad = {**MOCK_BILL, "billing_period_start": "sometime in August"}
    backend = ScriptedBackend([bad, MOCK_BILL])
    result = asyncio.run(BillExtractor(backend, max_attempts=2).extract(pdf))

    assert result.meta.attempts == 2
    assert backend.feedback[0] is None
    assert "billing_period_start" in backend.feedback[1]
    assert result.meta.input_tokens == 200
    assert not result.needs_review


def test_gives_up_after_max_attempts(bill):
    _, pdf = bill
    bad = {**MOCK_BILL, "unit_rates": "twenty five pence"}
    backend = ScriptedBackend([bad, bad])
    with pytest.raises(ExtractionError):
        asyncio.run(BillExtractor(backend, max_attempts=2).extract(pdf))


def test_failed_checks_set_needs_review(bill):
    _, pdf = bill
    backend = ScriptedBackend([{**MOCK_BILL, "total_amount": 99.99}])
    result = asyncio.run(BillExtractor(backend).extract(pdf))
    assert result.needs_review
    assert any(r.startswith("total_reconciles") for r in result.review_reasons)


def test_clean_raw_fixes_common_number_formats():
    cleaned = clean_raw(
        {
            "total_amount": "£1,091.12",
            "consumption_kwh": "287 kWh",
            "standing_charge": "51.42p",
            "tariff_name": "N/A",
            "unit_rates": [{"rate": "24.86p", "rate_unit": "p/kWh", "kwh": "287"}],
        }
    )
    assert cleaned["total_amount"] == 1091.12
    assert cleaned["consumption_kwh"] == 287.0
    assert cleaned["standing_charge"] == 51.42
    assert cleaned["tariff_name"] is None
    assert cleaned["unit_rates"][0]["rate"] == 24.86


@pytest.mark.parametrize(
    "reply",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Here is the data:\n{"a": 1}\nLet me know if you need more.',
        '{"a": 1, "note": "braces } inside strings are fine"}',
    ],
)
def test_parse_json_object_tolerates_wrapping(reply):
    assert parse_json_object(reply)["a"] == 1


@pytest.mark.parametrize("reply", [None, "", "no json here", "[1, 2, 3]", '{"a": 1'])
def test_parse_json_object_rejects_bad_replies(reply):
    with pytest.raises(BackendError):
        parse_json_object(reply)
