"""The evals/ entry point: oracle must be perfect, simulated errors must be scored and caught."""

import json

import pytest

from bill_extractor.synth import generate
from evals import run_eval


@pytest.fixture(scope="module")
def small_set(tmp_path_factory):
    out = tmp_path_factory.mktemp("evalset")
    generate(out, n=6, seed=5, faulty_n=3)
    return out


def _run(data, backend, *extra):
    args = ["--backend", backend, "--data", str(data), "--variants", "pdf,photo", "--no-write"]
    return run_eval.main([*args, *extra])


def test_oracle_scores_perfectly_and_flags_every_planted_fault(small_set):
    report = _run(small_set, "oracle")
    summary = report["summary"]
    assert report["simulated"] and report["label"].startswith("ORACLE")
    assert summary["field_accuracy_overall"] == 100.0
    assert summary["schema_valid_rate"] == 100.0
    assert summary["first_attempt_valid_rate"] == 100.0
    assert summary["planted_faults"]["catch_rate"] == 100.0
    assert summary["planted_faults"]["expected_check_caught"] == 100.0
    assert summary["review_flag"]["false_alarm_rate"] == 0.0
    assert summary["cost_usd"]["per_doc"] > 0
    assert set(summary["checks"]) >= {"total_reconciles", "meter_point_checksum", "input_quality"}


def test_simulated_errors_are_scored_and_repeatable(small_set):
    first = _run(small_set, "simulated")
    second = _run(small_set, "simulated")
    assert first["label"].startswith("SIMULATED")
    assert first["summary"]["outcomes"] == second["summary"]["outcomes"]  # seeded
    assert first["summary"]["field_accuracy_overall"] <= 100.0


def test_auto_without_key_uses_simulated(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert run_eval.pick_backend("auto") == "simulated"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert run_eval.pick_backend("auto") == "anthropic"


def test_write_updates_results_and_evals_md(small_set, tmp_path, monkeypatch):
    evals_md = tmp_path / "EVALS.md"
    evals_md.write_text(f"# Eval\n\n{run_eval.START}\nold\n{run_eval.END}\n\nfooter\n")
    monkeypatch.setattr(run_eval, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(run_eval, "EVALS_MD", evals_md)
    report = _run(small_set, "simulated", "--limit", "2")
    run_eval.write(report)
    saved = json.loads((tmp_path / "results" / "latest.json").read_text())
    assert saved["summary"]["docs"] == 4
    text = evals_md.read_text()
    assert "old" not in text and "SIMULATED" in text and text.endswith("footer\n")


def test_missing_variant_is_a_clear_error(small_set):
    with pytest.raises(SystemExit, match="photo_severe"):
        run_eval.main(
            ["--backend", "oracle", "--data", str(small_set), "--variants", "photo_severe"]
        )
