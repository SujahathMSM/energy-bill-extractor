import asyncio
import json
from datetime import date

import pytest

from bill_extractor.backends import BackendError, BackendResponse, VLMBackend
from bill_extractor.evaluate import evaluate, load_manifest, write_reports
from bill_extractor.extractor import BillExtractor
from bill_extractor.preprocess import prepare
from bill_extractor.schema import BillExtraction
from bill_extractor.scoring import (
    ALL_FIELDS,
    DocResult,
    compare_scalar,
    score_fields,
    summarise,
)


@pytest.mark.parametrize(
    ("name", "truth", "pred", "expected"),
    [
        ("total_amount", 91.12, 91.12, "correct"),
        ("total_amount", 91.12, 91.13, "correct"),  # within a penny
        ("total_amount", 91.12, 50.82, "wrong"),
        ("total_amount", 91.12, None, "missed"),
        ("previous_reading", None, 18342.0, "hallucinated"),
        ("previous_reading", None, None, "correct"),
        ("meter_point_id", "1600123456786", "16 0012 3456 786", "correct"),
        ("supplier_name", "Fernwick Energy", "FERNWICK ENERGY.", "correct"),
        ("reading_type", "estimated", "E", "correct"),
        ("reading_type", "actual", "Estimated", "wrong"),
        ("billing_period_end", date(2026, 9, 12), date(2026, 9, 12), "correct"),
        ("currency", "GBP", "gbp", "correct"),
    ],
)
def test_compare_scalar(name, truth, pred, expected):
    assert compare_scalar(name, truth, pred) == expected


def test_unit_rates_compare_by_normalised_price(dataset):
    truth = BillExtraction.model_validate(load_manifest(dataset)[0]["truth"])
    pred = truth.model_copy(deep=True)
    rate = pred.unit_rates[0]
    rate.rate, rate.rate_unit = rate.rate / 100, "£/kWh"  # same price, written in pounds
    assert score_fields(truth, pred)["unit_rates"] == "correct"
    rate.rate *= 100  # now 100x too expensive
    assert score_fields(truth, pred)["unit_rates"] == "wrong"


def test_summary_counts_review_catches():
    perfect = {name: "correct" for name in ALL_FIELDS}
    flawed = {**perfect, "total_amount": "wrong"}
    results = [
        DocResult("a", "pdf", "t", perfect, needs_review=False, latency_ms=100, attempts=1),
        DocResult("b", "pdf", "t", flawed, needs_review=True, latency_ms=200, attempts=1),
        DocResult("c", "photo", "t", flawed, needs_review=False, latency_ms=300, attempts=2),
        DocResult("d", "photo", "t", error="BackendError: boom"),
    ]
    summary = summarise(results)
    assert summary["errors"] == 1
    assert summary["doc_exact_match"] == pytest.approx(33.3)
    assert summary["review_flag"]["caught"] == 1
    assert summary["review_flag"]["catch_rate"] == 50.0
    assert summary["review_flag"]["false_alarm_rate"] == 0.0
    assert summary["by_variant"]["photo"]["docs"] == 2


class OracleBackend(VLMBackend):
    """Looks up the ground truth for the exact page image it is sent."""

    name = "oracle"
    model = "ground-truth"

    def __init__(self, answers: dict[bytes, dict]) -> None:
        self.answers = answers

    async def extract(self, pages, feedback=None):
        return BackendResponse(data=self.answers[pages[0].data])


def test_evaluation_end_to_end_with_an_oracle(dataset, tmp_path):
    """A backend that returns ground truth must score 100%: proves the harness wiring."""
    records = load_manifest(dataset)
    variants = ["pdf", "photo"]
    answers = {
        prepare((dataset / r["files"][v]).read_bytes(), 3, 1568).pages[0].data: r["truth"]
        for r in records
        for v in variants
    }
    oracle = OracleBackend(answers)
    results = asyncio.run(evaluate(BillExtractor(oracle), dataset, variants, concurrency=4))
    summary = summarise(results)
    assert summary["field_accuracy_overall"] == 100.0
    assert summary["review_flag"]["false_alarm_rate"] == 0.0

    base, markdown = write_reports(results, oracle, tmp_path)
    assert "Field accuracy: 100.0%" in markdown
    saved = json.loads(base.with_suffix(".json").read_text())
    assert len(saved["documents"]) == len(records) * 2


class FailingBackend(VLMBackend):
    name = "failing"
    model = "always-401"

    async def extract(self, pages, feedback=None):
        raise BackendError("Claude request failed: Error code: 401 - authentication_error")


def test_failed_run_reports_why(dataset, tmp_path):
    results = asyncio.run(
        evaluate(BillExtractor(FailingBackend()), dataset, ["pdf"], limit=2, concurrency=1)
    )
    summary = summarise(results)
    assert summary["errors"] == 2
    assert summary["error_messages"][0][1] == 2
    _, markdown = write_reports(results, FailingBackend(), tmp_path)
    assert "2x BackendError: Claude request failed: Error code: 401" in markdown
