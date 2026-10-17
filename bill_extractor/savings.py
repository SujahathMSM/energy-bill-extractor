"""Switching-savings estimate: what would this bill cost on another tariff?

The model reads; this module does the arithmetic, like the checks. It compares
the bill's own tariff with a configurable target tariff on the same annual
usage, and reports the difference. Costs exclude tax: tax rates vary by
country and tariff, and both sides are treated the same, so the saving is
comparable. Annual use is the bill's daily average x 365, which ignores
seasons -- the same estimate the extraction already reports.

A target tariff can be a single unlabelled rate (all usage charged at that
rate, the common case for a switching quote) or one rate per band, matched
to the bill's bands by label. A bill with no unit rates printed (like the
Oklahoma sample) cannot be priced, so it raises ValueError instead of
inventing a current cost.
"""

from pydantic import BaseModel, ConfigDict, Field

from .schema import BillExtraction, Derived
from .validation import analyse, parse_unit


class TargetUnitRate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str | None = Field(
        None, description='Rate band, e.g. "Standard", "Day", "Night". Ignored for a single rate.'
    )
    rate: float = Field(description="Unit rate number, e.g. 20.00.")
    rate_unit: str = Field(description='Unit, e.g. "p/kWh", "£/kWh", "LKR/kWh".')


class TargetTariff(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field("Target tariff", description="Name shown in the result.")
    currency: str | None = Field(
        None, description="ISO currency code. When omitted, the bill's currency is used."
    )
    unit_rates: list[TargetUnitRate] = Field(
        ..., min_length=1, description="One rate, or one per band matched by label."
    )
    standing_charge: float | None = Field(
        None, description="Standing/fixed charge rate, e.g. 40.00. Omitted means none."
    )
    standing_charge_unit: str | None = Field(
        None, description='Standing charge unit, e.g. "p/day", "LKR/month".'
    )


class SavingsBreakdown(BaseModel):
    energy: float = Field(description="Annual energy cost, excluding tax.")
    standing: float = Field(description="Annual standing-charge cost, excluding tax.")
    total: float = Field(description="Annual total, excluding tax.")


class SavingsBand(BaseModel):
    label: str | None
    annual_kwh: float
    current_cost: float = Field(description="Annual cost on the bill's tariff, excluding tax.")
    target_cost: float = Field(description="Annual cost on the target tariff, excluding tax.")


class SavingsEstimate(BaseModel):
    currency: str | None
    annual_kwh: float = Field(description="Estimated annual use, daily average x 365.")
    current: SavingsBreakdown
    target: SavingsBreakdown
    target_name: str | None
    saving_annual: float = Field(
        description="Current annual total minus target annual total. Positive means saving."
    )
    saving_monthly: float = Field(description="saving_annual / 12.")
    saving_pct: float | None = Field(
        description="saving_annual as a percentage of the current annual total."
    )
    bands: list[SavingsBand]
    warnings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class SavingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    extraction: BillExtraction
    target: TargetTariff
    derived: Derived | None = Field(
        None, description="Derived figures from the extraction. Recomputed when omitted."
    )
    needs_review: bool = Field(False, description="Whether the extraction was flagged for review.")


def _standing_per_day(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    if unit is None:
        raise ValueError("Standing charge has no unit, so it cannot be converted to per-day.")
    parsed = parse_unit(unit)
    if parsed is None or parsed.per not in ("day", "month", "year"):
        raise ValueError(f"Standing charge unit {unit!r} is not a per-day/month/year unit.")
    major = value * parsed.factor
    return {"day": major, "month": major * 12 / 365, "year": major / 365}[parsed.per]


def _norm_label(label: str | None) -> str:
    return (label or "").strip().casefold()


def _target_per_kwh(target: TargetTariff, band_label: str | None) -> float:
    # A single unlabelled rate applies to all usage. Anything else matches by label.
    if len(target.unit_rates) == 1 and target.unit_rates[0].label is None:
        chosen = target.unit_rates[0]
    else:
        matches = [r for r in target.unit_rates if _norm_label(r.label) == _norm_label(band_label)]
        if not matches:
            raise ValueError(
                f"Target tariff has no rate for band {band_label!r}; "
                "give one unlabelled target rate, or one per band matched by label."
            )
        chosen = matches[0]
    parsed = parse_unit(chosen.rate_unit)
    if parsed is None or parsed.per != "kwh":
        raise ValueError(f"Target rate unit {chosen.rate_unit!r} is not a per-kWh unit.")
    return chosen.rate * parsed.factor


def _money(value: float) -> float:
    return round(value + 1e-9, 2)


def estimate_savings(
    extraction: BillExtraction,
    target: TargetTariff,
    derived: Derived | None = None,
    needs_review: bool = False,
) -> SavingsEstimate:
    if derived is None:
        derived, _ = analyse(extraction)
    warnings: list[str] = []
    if needs_review:
        warnings.append(
            "The extraction was flagged for review; the saving is only as good as the extraction."
        )

    bill_currency = (derived.currency or extraction.currency or "").strip().upper() or None
    target_currency = (target.currency or "").strip().upper() or None
    if bill_currency and target_currency and bill_currency != target_currency:
        raise ValueError(
            f"Target currency {target_currency} does not match bill currency {bill_currency}."
        )
    currency = bill_currency or target_currency

    if not derived.unit_rates or all(r.per_kwh is None for r in derived.unit_rates):
        raise ValueError(
            "Current bill has no usable unit rates, so its annual cost cannot be estimated."
        )
    if derived.estimated_annual_kwh is None or derived.estimated_annual_kwh <= 0:
        raise ValueError("Cannot estimate savings without annual usage.")

    rates = derived.unit_rates
    # Per-band kWh: single band without a kWh figure uses the bill total,
    # mirroring the cost check. Multi-band bills need every band's kWh.
    band_kwh: list[float] = []
    for rate in rates:
        if rate.kwh is not None:
            band_kwh.append(rate.kwh)
        elif len(rates) == 1 and extraction.consumption_kwh is not None:
            band_kwh.append(extraction.consumption_kwh)
        else:
            raise ValueError("Cannot split annual use across bands without per-band kWh.")
    total_band_kwh = sum(band_kwh)
    if total_band_kwh <= 0:
        raise ValueError("Cannot estimate savings without positive band usage.")

    annual_kwh = derived.estimated_annual_kwh
    current_standing_per_day = derived.standing_charge_per_day
    if current_standing_per_day is None and extraction.standing_charge is not None:
        # analyse() already converts it when possible; reaching here means the
        # unit could not be understood, so fail loudly rather than assume zero.
        raise ValueError("Current standing charge unit could not be understood.")
    if current_standing_per_day is None:
        warnings.append("Current bill has no standing charge printed; treated as 0.")
    target_standing_per_day = (
        _standing_per_day(target.standing_charge, target.standing_charge_unit)
        if target.standing_charge is not None
        else None
    )
    if target_standing_per_day is None and target.standing_charge is not None:
        raise ValueError("Target standing charge could not be converted to per-day.")
    if target.standing_charge is None:
        warnings.append("Target tariff has no standing charge; treated as 0.")

    bands: list[SavingsBand] = []
    current_energy = 0.0
    target_energy = 0.0
    for rate, kwh in zip(rates, band_kwh, strict=True):
        share = kwh / total_band_kwh
        band_annual = annual_kwh * share
        target_rate = _target_per_kwh(target, rate.label)
        band_current = band_annual * rate.per_kwh
        band_target = band_annual * target_rate
        current_energy += band_current
        target_energy += band_target
        bands.append(
            SavingsBand(
                label=rate.label,
                annual_kwh=round(band_annual, 1),
                current_cost=_money(band_current),
                target_cost=_money(band_target),
            )
        )

    current_standing = (current_standing_per_day or 0.0) * 365
    target_standing = (target_standing_per_day or 0.0) * 365
    current_total = current_energy + current_standing
    target_total = target_energy + target_standing
    saving = current_total - target_total
    saving_pct = round(saving / current_total * 100, 1) if current_total > 0 else None

    assumptions = [
        "Costs exclude tax/VAT; both tariffs are treated the same.",
        "Annual use is daily average x 365 and ignores seasons.",
    ]
    if len(target.unit_rates) == 1 and target.unit_rates[0].label is None:
        assumptions.append("Target is a single rate applied to all usage.")
    else:
        assumptions.append("Target bands are matched to the bill's bands by label.")

    return SavingsEstimate(
        currency=currency,
        annual_kwh=round(annual_kwh, 1),
        current=SavingsBreakdown(
            energy=_money(current_energy),
            standing=_money(current_standing),
            total=_money(current_total),
        ),
        target=SavingsBreakdown(
            energy=_money(target_energy),
            standing=_money(target_standing),
            total=_money(target_total),
        ),
        target_name=target.name,
        saving_annual=_money(saving),
        saving_monthly=_money(saving / 12),
        saving_pct=saving_pct,
        bands=bands,
        warnings=warnings,
        assumptions=assumptions,
    )
