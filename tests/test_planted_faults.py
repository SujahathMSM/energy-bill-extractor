import asyncio
import json
import random

import pytest

from bill_extractor.evaluate import evaluate, load_manifest
from bill_extractor.extractor import BillExtractor
from bill_extractor.preprocess import prepare
from bill_extractor.schema import BillExtraction
from bill_extractor.scoring import ALL_FIELDS, DocResult, render_markdown, summarise
from bill_extractor.synth import (
    FAULT_TO_CHECK,
    FAULT_TYPES,
    applicable_faults,
    generate,
    make_bill,
    render_pdf,
)
from bill_extractor.validation import analyse
from tests.test_scoring_and_eval import OracleBackend


def _template_index(template: str) -> int:
    return {"single_rate": 0, "economy7": 1, "summary_first": 2}[template]


@pytest.mark.parametrize("fault", FAULT_TYPES)
@pytest.mark.parametrize("template", ["single_rate", "economy7", "summary_first"])
def test_each_fault_fails_its_expected_check(fault, template):
    if fault not in applicable_faults(template):
        pytest.skip(f"{fault} does not apply to {template}")
    bill = make_bill(_template_index(template), random.Random(42), fault=fault)
    assert bill.fault == fault
    _, checks = analyse(bill.truth)
    statuses = {c.name: c.status for c in checks}
    expected = FAULT_TO_CHECK[fault]
    assert statuses[expected] == "fail", statuses
    assert any(c.status == "fail" for c in checks)
    # A faithful extraction of a faulty bill must be flagged for review.
    assert any(c.status == "fail" for c in checks)


def test_wrong_consumption_is_the_only_multi_check_fault():
    """Every fault except wrong_consumption isolates to a single check."""
    for fault in FAULT_TYPES:
        for template in ("single_rate", "economy7", "summary_first"):
            if fault not in applicable_faults(template):
                continue
            bill = make_bill(_template_index(template), random.Random(7), fault=fault)
            failed = [c.name for c in analyse(bill.truth)[1] if c.status == "fail"]
            if fault == "wrong_consumption":
                assert FAULT_TO_CHECK[fault] in failed
            else:
                assert failed == [FAULT_TO_CHECK[fault]], (fault, template, failed)


def test_clean_bill_still_passes_every_check():
    bill = make_bill(0, random.Random(1))
    assert bill.fault is None
    failed = [c for c in analyse(bill.truth)[1] if c.status == "fail"]
    assert not failed


def test_unknown_fault_is_rejected():
    with pytest.raises(ValueError):
        make_bill(0, random.Random(1), fault="no_such_fault")


def test_swapped_readings_needs_readings():
    with pytest.raises(ValueError):
        make_bill(1, random.Random(1), fault="swapped_readings")


@pytest.mark.parametrize("fault", FAULT_TYPES)
def test_faulty_bills_render(fault, tmp_path):
    # Every fault must render on every template it applies to.
    for template in ("single_rate", "economy7", "summary_first"):
        if fault not in applicable_faults(template):
            continue
        bill = make_bill(_template_index(template), random.Random(3), fault=fault)
        pdf = render_pdf(bill)
        assert pdf.startswith(b"%PDF")


def test_generate_mixes_clean_and_faulty(tmp_path):
    records = generate(tmp_path, n=12, seed=5, faulty_n=6)
    assert len(records) == 12
    faulty = [r for r in records if r["fault"]]
    clean = [r for r in records if not r["fault"]]
    assert len(faulty) == 6
    assert len(clean) == 6
    # Six faulty bills cycle through all six fault types exactly once.
    assert {r["fault"] for r in faulty} == set(FAULT_TYPES)
    for record in faulty:
        assert record["expected_check"] == FAULT_TO_CHECK[record["fault"]]
        truth = BillExtraction.model_validate(record["truth"])
        statuses = {c.name: c.status for c in analyse(truth)[1]}
        assert statuses[record["expected_check"]] == "fail"
    for record in clean:
        assert record["expected_check"] is None
        truth = BillExtraction.model_validate(record["truth"])
        assert not [c for c in analyse(truth)[1] if c.status == "fail"]


def test_generate_default_is_all_clean(tmp_path):
    records = generate(tmp_path, n=6, seed=3)
    assert all(r["fault"] is None for r in records)
    assert all(r["expected_check"] is None for r in records)


def test_summarise_planted_faults():
    perfect = {name: "correct" for name in ALL_FIELDS}
    results = [
        # Clean + correct + unflagged: not a false alarm.
        DocResult("a", "pdf", "t", perfect, needs_review=False),
        # Clean + correct + flagged: a false alarm.
        DocResult("b", "pdf", "t", perfect, needs_review=True),
        # Faulty + correct fields + flagged: a catch, not a false alarm.
        DocResult(
            "c",
            "pdf",
            "t",
            perfect,
            needs_review=True,
            fault="wrong_total",
            expected_check="total_reconciles",
            expected_check_failed=True,
        ),
        # Faulty + correct fields + unflagged: a miss.
        DocResult(
            "d",
            "pdf",
            "t",
            perfect,
            needs_review=False,
            fault="bad_mpan",
            expected_check="meter_point_checksum",
            expected_check_failed=False,
        ),
        # Clean + wrong field + flagged: a model-error catch.
        DocResult(
            "e",
            "pdf",
            "t",
            {**perfect, "total_amount": "wrong"},
            needs_review=True,
        ),
    ]
    summary = summarise(results)
    assert summary["planted_faults"]["docs"] == 2
    assert summary["planted_faults"]["caught"] == 1
    assert summary["planted_faults"]["catch_rate"] == 50.0
    assert summary["planted_faults"]["expected_check_caught"] == 50.0
    assert summary["planted_faults"]["by_fault"]["wrong_total"]["catch_rate"] == 100.0
    assert summary["planted_faults"]["by_fault"]["bad_mpan"]["catch_rate"] == 0.0
    # False alarms count only clean correct docs: 1 of 2.
    assert summary["review_flag"]["false_alarm_rate"] == 50.0
    # Model-error catch counts only clean docs: 1 of 1.
    assert summary["review_flag"]["catch_rate"] == 100.0
    markdown = render_markdown(summary, "T")
    assert "Planted faults: checks flagged 1 of 2" in markdown
    assert "wrong_total (total_reconciles)" in markdown


def test_summarise_without_faulty_docs_has_no_planted_section():
    perfect = {name: "correct" for name in ALL_FIELDS}
    summary = summarise([DocResult("a", "pdf", "t", perfect, needs_review=False)])
    assert summary["planted_faults"]["docs"] == 0
    assert summary["planted_faults"]["catch_rate"] is None
    assert "Planted faults" not in render_markdown(summary, "T")


def test_oracle_catches_every_planted_fault(tmp_path):
    """An oracle that returns ground truth scores 100% on fields and flags every faulty bill."""
    out = tmp_path / "faulty"
    generate(out, n=12, seed=11, faulty_n=6)
    records = load_manifest(out)
    variants = ["pdf"]
    answers = {
        prepare((out / r["files"][v]).read_bytes(), 3, 1568).pages[0].data: r["truth"]
        for r in records
        for v in variants
    }
    results = asyncio.run(evaluate(BillExtractor(OracleBackend(answers)), out, variants))
    summary = summarise(results)
    assert summary["field_accuracy_overall"] == 100.0
    assert summary["planted_faults"]["docs"] == 6
    assert summary["planted_faults"]["catch_rate"] == 100.0
    assert summary["planted_faults"]["expected_check_caught"] == 100.0
    assert summary["review_flag"]["false_alarm_rate"] == 0.0
    assert "Planted faults" in render_markdown(summary, "T")
    # Manifest round-trips through JSON.
    assert json.loads((out / "manifest.jsonl").read_text().splitlines()[0])["fault"] is None
