"""Field-level scoring of extractions against ground truth."""

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from .schema import BillExtraction
from .validation import parse_unit

SCALAR_FIELDS = [
    "supplier_name",
    "account_number",
    "meter_point_id",
    "meter_serial",
    "tariff_name",
    "currency",
    "billing_period_start",
    "billing_period_end",
    "period_days",
    "previous_reading",
    "current_reading",
    "reading_type",
    "consumption_kwh",
    "standing_charge",
    "subtotal_excl_tax",
    "tax_amount",
    "total_amount",
]
ALL_FIELDS = [*SCALAR_FIELDS, "unit_rates", "other_charges"]
_ID_FIELDS = {"account_number", "meter_point_id", "meter_serial"}
_READING_TYPES = {"a": "actual", "e": "estimated", "s": "smart", "c": "customer"}


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", value.casefold())).strip()


def _norm_id(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", value).upper()


def _num_equal(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.01, 0.005 * abs(b))


def _empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def compare_scalar(name: str, truth, pred) -> str:
    """Return 'correct', 'wrong', 'missed' (null for a printed value) or 'hallucinated'."""
    if _empty(truth) and _empty(pred):
        return "correct"
    if _empty(truth):
        return "hallucinated"
    if _empty(pred):
        return "missed"
    if isinstance(truth, float | int) and not isinstance(truth, bool):
        return "correct" if _num_equal(float(pred), float(truth)) else "wrong"
    if isinstance(truth, date):
        return "correct" if pred == truth else "wrong"
    if name in _ID_FIELDS:
        return "correct" if _norm_id(str(pred)) == _norm_id(str(truth)) else "wrong"
    if name == "currency":
        return "correct" if str(pred).strip().upper() == str(truth).upper() else "wrong"
    if name == "reading_type":
        first = _norm_text(str(pred))[:1]
        return "correct" if _READING_TYPES.get(first) == truth else "wrong"
    return "correct" if _norm_text(str(pred)) == _norm_text(str(truth)) else "wrong"


def _rate_key(rate) -> tuple[float | None, float | None]:
    unit = parse_unit(rate.rate_unit)
    per_kwh = rate.rate * unit.factor if unit and unit.per == "kwh" else None
    return per_kwh, rate.kwh


def compare_rates(truth: BillExtraction, pred: BillExtraction) -> str:
    """Rates match when every band's per-kWh price (and kWh, if printed) matches."""
    if not truth.unit_rates:
        return "correct" if not pred.unit_rates else "hallucinated"
    if not pred.unit_rates:
        return "missed"
    if len(truth.unit_rates) != len(pred.unit_rates):
        return "wrong"
    t_keys = sorted((_rate_key(r) for r in truth.unit_rates), key=lambda k: k[0] or 0)
    p_keys = sorted((_rate_key(r) for r in pred.unit_rates), key=lambda k: k[0] or 0)
    for (t_rate, t_kwh), (p_rate, p_kwh) in zip(t_keys, p_keys, strict=True):
        if p_rate is None or not _num_equal(p_rate, t_rate):
            return "wrong"
        if t_kwh is not None and (p_kwh is None or not _num_equal(p_kwh, t_kwh)):
            return "wrong"
    return "correct"


def compare_other_charges(truth: BillExtraction, pred: BillExtraction) -> str:
    """Other charges match when the amounts match one for one; labels vary too much to score."""
    if not truth.other_charges:
        return "correct" if not pred.other_charges else "hallucinated"
    if not pred.other_charges:
        return "missed"
    t_amounts = sorted(c.amount for c in truth.other_charges)
    p_amounts = sorted(c.amount for c in pred.other_charges)
    if len(t_amounts) != len(p_amounts):
        return "wrong"
    ok = all(_num_equal(p, t) for t, p in zip(t_amounts, p_amounts, strict=True))
    return "correct" if ok else "wrong"


def score_fields(truth: BillExtraction, pred: BillExtraction) -> dict[str, str]:
    outcomes = {
        name: compare_scalar(name, getattr(truth, name), getattr(pred, name))
        for name in SCALAR_FIELDS
    }
    outcomes["unit_rates"] = compare_rates(truth, pred)
    outcomes["other_charges"] = compare_other_charges(truth, pred)
    return outcomes


@dataclass
class DocResult:
    doc_id: str
    variant: str
    template: str
    outcomes: dict[str, str] = field(default_factory=dict)
    needs_review: bool | None = None
    failed_checks: list[str] = field(default_factory=list)
    latency_ms: int | None = None
    attempts: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None

    @property
    def all_correct(self) -> bool:
        return self.error is None and all(v == "correct" for v in self.outcomes.values())


def _pct(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1])


def summarise(results: list[DocResult]) -> dict:
    scored = [r for r in results if r.error is None]
    error_counts = Counter(r.error for r in results if r.error is not None)
    field_accuracy = {
        name: _pct(sum(r.outcomes[name] == "correct" for r in scored), len(scored))
        for name in ALL_FIELDS
    }
    outcome_counts = {
        kind: sum(v == kind for r in scored for v in r.outcomes.values())
        for kind in ("correct", "wrong", "missed", "hallucinated")
    }
    total_fields = sum(outcome_counts.values())

    with_errors = [r for r in scored if not r.all_correct]
    clean = [r for r in scored if r.all_correct]

    by_variant = {}
    for variant in sorted({r.variant for r in results}):
        group = [r for r in scored if r.variant == variant]
        fields_in_group = [v for r in group for v in r.outcomes.values()]
        by_variant[variant] = {
            "docs": len([r for r in results if r.variant == variant]),
            "field_accuracy": _pct(
                sum(v == "correct" for v in fields_in_group), len(fields_in_group)
            ),
            "doc_exact_match": _pct(sum(r.all_correct for r in group), len(group)),
        }

    latencies = [r.latency_ms for r in scored if r.latency_ms is not None]
    attempts = [r.attempts for r in scored if r.attempts is not None]
    return {
        "docs": len(results),
        "errors": len(results) - len(scored),
        "error_messages": [[message, count] for message, count in error_counts.most_common(5)],
        "field_accuracy_overall": _pct(outcome_counts["correct"], total_fields),
        "doc_exact_match": _pct(len(clean), len(scored)),
        "outcomes": outcome_counts,
        "field_accuracy": field_accuracy,
        "by_variant": by_variant,
        "review_flag": {
            # Of documents the model got wrong, how many did the checks flag for review?
            "docs_with_any_error": len(with_errors),
            "caught": sum(bool(r.needs_review) for r in with_errors),
            "catch_rate": _pct(sum(bool(r.needs_review) for r in with_errors), len(with_errors)),
            # Of documents the model got fully right, how many were flagged anyway?
            "false_alarm_rate": _pct(sum(bool(r.needs_review) for r in clean), len(clean)),
        },
        "latency_ms": {"p50": _percentile(latencies, 50), "p95": _percentile(latencies, 95)},
        "mean_attempts": round(statistics.fmean(attempts), 2) if attempts else None,
        "tokens": {
            "input": sum(r.input_tokens or 0 for r in scored),
            "output": sum(r.output_tokens or 0 for r in scored),
        },
    }


def _error_lines(summary: dict) -> list[str]:
    if not summary.get("error_messages"):
        return []
    lines = ["- Extraction errors (most common first):"]
    for message, count in summary["error_messages"]:
        lines.append(f"  - {count}x {message[:300]}")
    return lines


def render_markdown(summary: dict, title: str) -> str:
    def fmt(value) -> str:
        return "n/a" if value is None else f"{value}%"

    review = summary["review_flag"]
    lines = [
        f"# {title}",
        "",
        f"- Documents: {summary['docs']} ({summary['errors']} failed to extract)",
        f"- Field accuracy: {fmt(summary['field_accuracy_overall'])}",
        f"- Documents with every field correct: {fmt(summary['doc_exact_match'])}",
        f"- Outcomes: {summary['outcomes']}",
        f"- Review flag caught {review['caught']} of {review['docs_with_any_error']} documents "
        f"with an error ({fmt(review['catch_rate'])}); false alarms on correct documents: "
        f"{fmt(review['false_alarm_rate'])}",
        *_error_lines(summary),
        f"- Latency p50 / p95: {summary['latency_ms']['p50']} / {summary['latency_ms']['p95']} ms; "
        f"mean attempts {summary['mean_attempts']}",
        f"- Tokens: {summary['tokens']['input']} in / {summary['tokens']['output']} out",
        "",
        "| Input | Docs | Field accuracy | All fields correct |",
        "|---|---|---|---|",
    ]
    for variant, row in summary["by_variant"].items():
        lines.append(
            f"| {variant} | {row['docs']} | {fmt(row['field_accuracy'])} | "
            f"{fmt(row['doc_exact_match'])} |"
        )
    lines += ["", "| Field | Accuracy |", "|---|---|"]
    for name, accuracy in summary["field_accuracy"].items():
        lines.append(f"| {name} | {fmt(accuracy)} |")
    return "\n".join(lines) + "\n"
