import pytest
from fastapi.testclient import TestClient

from bill_extractor.api import app
from bill_extractor.backends.mock_backend import MOCK_BILL
from bill_extractor.config import get_settings
from bill_extractor.savings import TargetTariff, estimate_savings
from bill_extractor.schema import BillExtraction
from bill_extractor.validation import analyse


def _mock_extraction(**changes):
    return BillExtraction.model_validate({**MOCK_BILL, **changes})


def _target(**changes):
    base = {
        "name": "Fuse Standard",
        "currency": "GBP",
        "unit_rates": [{"label": None, "rate": 20.0, "rate_unit": "p/kWh"}],
        "standing_charge": 40.0,
        "standing_charge_unit": "p/day",
    }
    base.update(changes)
    return TargetTariff.model_validate(base)


def test_mock_bill_saving_against_cheaper_target():
    # Mock bill: 287 kWh over 30 days -> 3492 kWh/year at 24.86p + 51.42p/day.
    # Target: 20p/kWh + 40p/day. Current 1055.79, target 844.40, saving 211.39.
    result = estimate_savings(_mock_extraction(), _target())
    assert result.currency == "GBP"
    assert result.annual_kwh == pytest.approx(3492.0)
    assert result.current.total == pytest.approx(1055.79)
    assert result.current.energy == pytest.approx(868.11)
    assert result.current.standing == pytest.approx(187.68)
    assert result.target.total == pytest.approx(844.40)
    assert result.saving_annual == pytest.approx(211.39)
    assert result.saving_monthly == pytest.approx(17.62)
    assert result.saving_pct == pytest.approx(20.0)
    assert result.bands[0].label == "Standard"
    assert "exclude tax" in result.assumptions[0].lower()
    assert result.warnings == []


def test_expensive_target_is_negative_saving():
    result = estimate_savings(
        _mock_extraction(),
        _target(unit_rates=[{"rate": 30.0, "rate_unit": "p/kWh"}], standing_charge=60.0),
    )
    assert result.saving_annual < 0
    assert result.saving_annual == pytest.approx(1055.79 - (3492 * 0.30 + 0.60 * 365))


def test_economy7_split_across_bands():
    extraction = _mock_extraction(
        consumption_kwh=400,
        period_days=30,
        unit_rates=[
            {"label": "Day", "rate": 30.0, "rate_unit": "p/kWh", "kwh": 250},
            {"label": "Night", "rate": 12.0, "rate_unit": "p/kWh", "kwh": 150},
        ],
        standing_charge=50.0,
    )
    # Single-rate target applies to all usage. Annual use is rounded, like Derived.
    single = estimate_savings(extraction, _target())
    assert single.annual_kwh == pytest.approx(4867.0)
    assert len(single.bands) == 2
    assert sum(b.annual_kwh for b in single.bands) == pytest.approx(single.annual_kwh, abs=0.2)
    # Band-matched target: Day 25p, Night 10p.
    matched = estimate_savings(
        extraction,
        _target(
            unit_rates=[
                {"label": "Day", "rate": 25.0, "rate_unit": "p/kWh"},
                {"label": "Night", "rate": 10.0, "rate_unit": "p/kWh"},
            ]
        ),
    )
    assert matched.target.energy < single.target.energy  # night rate is cheaper than 20p
    assert matched.bands[1].target_cost < matched.bands[1].current_cost


def test_target_band_label_mismatch_is_rejected():
    extraction = _mock_extraction(
        unit_rates=[
            {"label": "Day", "rate": 30.0, "rate_unit": "p/kWh", "kwh": 250},
            {"label": "Night", "rate": 12.0, "rate_unit": "p/kWh", "kwh": 150},
        ],
        consumption_kwh=400,
    )
    with pytest.raises(ValueError, match="no rate for band 'Night'"):
        estimate_savings(
            extraction,
            _target(unit_rates=[{"label": "Day", "rate": 25.0, "rate_unit": "p/kWh"}]),
        )


def test_missing_unit_rates_cannot_be_priced():
    extraction = _mock_extraction(unit_rates=[])
    with pytest.raises(ValueError, match="no usable unit rates"):
        estimate_savings(extraction, _target())


def test_currency_mismatch_is_rejected():
    with pytest.raises(ValueError, match="does not match bill currency"):
        estimate_savings(_mock_extraction(), _target(currency="USD"))


def test_missing_standing_charge_is_zero_with_warning():
    extraction = _mock_extraction(standing_charge=None, standing_charge_unit=None)
    result = estimate_savings(extraction, _target(standing_charge=None))
    assert result.current.standing == 0.0
    assert any("standing charge" in w for w in result.warnings)


def test_needs_review_adds_warning():
    result = estimate_savings(_mock_extraction(), _target(), needs_review=True)
    assert any("flagged for review" in w for w in result.warnings)


def test_derived_figures_are_reused_when_supplied():
    extraction = _mock_extraction()
    derived, _ = analyse(extraction)
    result = estimate_savings(extraction, _target(), derived=derived)
    assert result.current.total == pytest.approx(1055.79)


def test_monthly_standing_charge_converts_to_daily():
    result = estimate_savings(
        _mock_extraction(),
        _target(standing_charge=12.0, standing_charge_unit="£/month"),
    )
    # 12 GBP/month -> 12*12/365 = 0.3945/day -> 144.00/year.
    assert result.target.standing == pytest.approx(144.00)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BILLX_BACKEND", "mock")
    get_settings.cache_clear()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_savings_endpoint(client):
    response = client.post(
        "/v1/savings",
        json={
            "extraction": MOCK_BILL,
            "target": {
                "name": "Fuse Standard",
                "currency": "GBP",
                "unit_rates": [{"rate": 20.0, "rate_unit": "p/kWh"}],
                "standing_charge": 40.0,
                "standing_charge_unit": "p/day",
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["saving_annual"] == pytest.approx(211.39)
    assert body["target_name"] == "Fuse Standard"


def test_savings_endpoint_rejects_unpriceable_bill(client):
    bill = {**MOCK_BILL, "unit_rates": []}
    response = client.post(
        "/v1/savings",
        json={
            "extraction": bill,
            "target": {"unit_rates": [{"rate": 20.0, "rate_unit": "p/kWh"}]},
        },
    )
    assert response.status_code == 422
    assert "no usable unit rates" in response.json()["detail"]
