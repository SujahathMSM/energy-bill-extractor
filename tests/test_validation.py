import json

import pytest

from bill_extractor.backends.mock_backend import MOCK_BILL
from bill_extractor.schema import BillExtraction
from bill_extractor.validation import analyse, mpan_check_digit, mpan_core, parse_unit


def _statuses(ex: BillExtraction) -> dict[str, str]:
    return {c.name: c.status for c in analyse(ex)[1]}


def _mock(**changes) -> BillExtraction:
    return BillExtraction.model_validate({**MOCK_BILL, **changes})


@pytest.mark.parametrize(
    ("unit", "factor", "currency", "per"),
    [
        ("p/kWh", 0.01, "GBP", "kwh"),
        ("pence per kWh", 0.01, "GBP", "kwh"),
        ("p per day", 0.01, "GBP", "day"),
        ("£/day", 1.0, "GBP", "day"),
        ("LKR/kWh", 1.0, "LKR", "kwh"),
        ("Rs/month", 1.0, "LKR", "month"),
        ("fils/kWh", 0.01, "AED", "kwh"),
        ("c/kWh", 0.01, None, "kwh"),
    ],
)
def test_parse_unit(unit, factor, currency, per):
    parsed = parse_unit(unit)
    assert parsed is not None
    assert (parsed.factor, parsed.currency, parsed.per) == (factor, currency, per)


@pytest.mark.parametrize("unit", [None, "", "kWh", "per kWh", "bananas/kWh", "p/fortnight"])
def test_parse_unit_rejects_unknown(unit):
    assert parse_unit(unit) is None


def test_mpan_check_digit_round_trip():
    assert mpan_check_digit("123456789012") in range(10)
    core = MOCK_BILL["meter_point_id"]
    assert mpan_core(f"S 01 801 100 {core}") == core  # full 21-digit form keeps the core
    assert mpan_core("12345") is None


def test_mock_bill_passes_every_check():
    derived, checks = analyse(_mock())
    assert {c.status for c in checks} == {"pass"}
    assert derived.period_days == 30
    assert derived.unit_rates[0].per_kwh == pytest.approx(0.2486)
    assert derived.standing_charge_per_day == pytest.approx(0.5142)
    assert derived.estimated_annual_kwh == round(287 / 30 * 365)


def test_ground_truth_of_every_synthetic_bill_is_consistent(dataset):
    """The generator and the checks must agree, or the evaluation would be meaningless."""
    for line in (dataset / "manifest.jsonl").read_text().splitlines():
        truth = BillExtraction.model_validate(json.loads(line)["truth"])
        failed = [c for c in analyse(truth)[1] if c.status == "fail"]
        assert not failed, failed


def test_total_mismatch_is_flagged():
    assert _statuses(_mock(total_amount=95.12))["total_reconciles"] == "fail"


def test_readings_mismatch_is_flagged():
    assert _statuses(_mock(current_reading=18829))["readings_match_consumption"] == "fail"


def test_pence_read_as_pounds_is_flagged():
    statuses = _statuses(
        _mock(unit_rates=[{"label": "Standard", "rate": 24.86, "rate_unit": "£/kWh", "kwh": 287}])
    )
    assert statuses["rates_plausible"] == "fail"
    assert statuses["cost_reconciles"] == "fail"


def test_misread_mpan_digit_is_flagged():
    core = MOCK_BILL["meter_point_id"]
    wrong = core[:5] + str((int(core[5]) + 1) % 10) + core[6:]
    assert _statuses(_mock(meter_point_id=wrong))["meter_point_checksum"] == "fail"


def test_currency_mismatch_is_flagged():
    assert _statuses(_mock(currency="EUR"))["currency_consistent"] == "fail"


def test_missing_fields_fail_onboarding_and_skip_maths():
    statuses = _statuses(BillExtraction(supplier_name="Fernwick Energy"))
    assert statuses["onboarding_fields"] == "fail"
    for name in ("cost_reconciles", "total_reconciles", "readings_match_consumption"):
        assert statuses[name] == "skipped"


def test_period_ending_before_start_is_flagged():
    statuses = _statuses(_mock(billing_period_start="2026-09-12", billing_period_end="2026-08-14"))
    assert statuses["billing_period"] == "fail"


def test_monthly_fixed_charge_converts_to_daily():
    derived, _ = analyse(
        BillExtraction(currency="LKR", standing_charge=365.0, standing_charge_unit="LKR/month")
    )
    assert derived.standing_charge_per_day == pytest.approx(12.0)


def _coop_bill(**changes) -> BillExtraction:
    """Shaped like the Oklahoma Electric Cooperative sample: no unit rates, a Roundup line."""
    fields = {
        "supplier_name": "Oklahoma Electric Cooperative",
        "account_number": "1234567",
        "currency": "USD",
        "billing_period_start": "2026-05-15",
        "billing_period_end": "2026-06-15",
        "period_days": 31,
        "previous_reading": 12258,
        "current_reading": 12512,
        "consumption_kwh": 254,
        "subtotal_excl_tax": 48.50,
        "tax_amount": 0.97,
        "other_charges": [{"label": "Roundup", "amount": 0.53}],
        "total_amount": 50.00,
    }
    return BillExtraction.model_validate({**fields, **changes})


def test_printed_day_count_wins_over_counting_dates():
    derived, checks = analyse(_coop_bill())
    assert derived.period_days == 31  # the dates span 32 counting both ends
    period = next(c for c in checks if c.name == "billing_period")
    assert period.status == "pass"
    assert "31 days as printed" in period.detail
    assert derived.avg_daily_kwh == round(254 / 31, 3)


def test_day_count_without_printed_days_counts_both_ends():
    derived, _ = analyse(_coop_bill(period_days=None))
    assert derived.period_days == 32


def test_printed_day_count_far_from_dates_is_flagged():
    assert _statuses(_coop_bill(period_days=12))["billing_period"] == "fail"


def test_printed_days_feed_the_derived_figures():
    # Mock bill spans 30 days; a printed 29 wins.
    assert _statuses(_mock())["cost_reconciles"] == "pass"
    derived, _ = analyse(_mock(period_days=29))
    assert derived.period_days == 29


def test_other_charges_reconcile_the_total():
    statuses = _statuses(_coop_bill())
    assert statuses["total_reconciles"] == "pass"
    # Only the genuinely missing unit rates remain flagged.
    assert [name for name, status in statuses.items() if status == "fail"] == ["onboarding_fields"]


def test_missing_other_charge_still_fails_the_total():
    assert _statuses(_coop_bill(other_charges=[]))["total_reconciles"] == "fail"


def test_credit_as_negative_other_charge():
    bill = _coop_bill(other_charges=[{"label": "Discount", "amount": -1.00}], total_amount=48.47)
    assert _statuses(bill)["total_reconciles"] == "pass"
