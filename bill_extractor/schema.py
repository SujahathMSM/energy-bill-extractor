"""Data models.

`BillExtraction` is what the model is asked to return: values exactly as printed on the bill.
Everything else (unit conversion, derived figures, checks) is computed in code, because
arithmetic and unit handling are cheap and reliable in Python and unreliable in a model.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UnitRate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str | None = Field(
        None, description='Rate band as printed, e.g. "Standard", "Day", "Night", "Peak".'
    )
    rate: float = Field(description="Unit rate number exactly as printed, e.g. 24.86.")
    rate_unit: str = Field(
        description='Unit exactly as printed, e.g. "p/kWh", "£/kWh", "LKR/kWh", "fils/kWh".'
    )
    kwh: float | None = Field(None, description="kWh charged at this rate, if shown.")


class OtherCharge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = Field(description='Line as printed, e.g. "Roundup", "Fuel adjustment", "SSCL".')
    amount: float = Field(description="Amount as printed; negative for a credit or discount.")


class BillExtraction(BaseModel):
    """Fields read from one electricity bill. Use null for anything not printed on the bill."""

    model_config = ConfigDict(extra="ignore")

    supplier_name: str | None = Field(None, description="Energy supplier's name.")
    account_number: str | None = Field(None, description="Customer account number.")
    meter_point_id: str | None = Field(
        None,
        description=(
            "Supply point number: the MPAN in Great Britain (13 digit core), "
            "or the premises/meter point ID elsewhere."
        ),
    )
    meter_serial: str | None = Field(None, description="Meter serial number.")
    tariff_name: str | None = Field(None, description="Tariff or plan name.")
    currency: str | None = Field(None, description="ISO 4217 currency code, e.g. GBP.")
    billing_period_start: date | None = Field(None, description="First day of the bill period.")
    billing_period_end: date | None = Field(None, description="Last day of the bill period.")
    period_days: int | None = Field(
        None, description="Number of days in the bill period, only if printed on the bill."
    )
    previous_reading: float | None = Field(
        None, description="Previous cumulative meter reading (single-register meters only)."
    )
    current_reading: float | None = Field(
        None, description="Current cumulative meter reading (single-register meters only)."
    )
    reading_type: str | None = Field(
        None, description='How the current reading was taken, e.g. "actual", "estimated", "smart".'
    )
    consumption_kwh: float | None = Field(None, description="Total kWh used in the period.")
    unit_rates: list[UnitRate] = Field(
        default_factory=list, description="Each unit rate charged, one entry per band."
    )
    standing_charge: float | None = Field(
        None, description="Standing/fixed charge rate as printed, e.g. 53.35."
    )
    standing_charge_unit: str | None = Field(
        None, description='Standing charge unit as printed, e.g. "p/day", "LKR/month".'
    )
    subtotal_excl_tax: float | None = Field(
        None, description="This period's energy + standing charges before tax."
    )
    tax_amount: float | None = Field(None, description="Tax (e.g. VAT) on this period's charges.")
    other_charges: list[OtherCharge] = Field(
        default_factory=list,
        description=(
            "This period's charge lines that are neither energy, standing charge nor tax "
            "(levies, adjustments, roundup donations, discounts), each with its amount."
        ),
    )
    total_amount: float | None = Field(
        None,
        description=(
            "This period's charges including tax and other charge lines. Not the amount due "
            "if that includes a previous balance, payments or credits."
        ),
    )


class NormalisedRate(BaseModel):
    label: str | None
    per_kwh: float | None = Field(description="Rate in major currency units per kWh.")
    kwh: float | None


class Derived(BaseModel):
    currency: str | None
    period_days: int | None = Field(
        description="Printed day count when shown, otherwise both end dates counted."
    )
    unit_rates: list[NormalisedRate]
    standing_charge_per_day: float | None = Field(description="Major currency units per day.")
    avg_daily_kwh: float | None
    estimated_annual_kwh: float | None = Field(
        description="Average daily use x 365. Ignores seasonality; a starting estimate only."
    )


class Check(BaseModel):
    name: str
    status: Literal["pass", "fail", "skipped"]
    detail: str


class ExtractionMeta(BaseModel):
    request_id: str
    backend: str
    model: str
    pages_sent: int
    pages_in_file: int
    attempts: int
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None


class ExtractionResult(BaseModel):
    extraction: BillExtraction
    derived: Derived
    checks: list[Check]
    needs_review: bool = Field(description="True when any check failed.")
    review_reasons: list[str]
    meta: ExtractionMeta


def extraction_json_schema() -> dict:
    """JSON schema handed to the model (tool input schema / response format)."""
    return BillExtraction.model_json_schema()
