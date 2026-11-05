"""Data models.

`BillExtraction` is what the model is asked to return: values exactly as printed on the bill.
Everything else (unit conversion, derived figures, checks) is computed in code, because
arithmetic and unit handling are cheap and reliable in Python and unreliable in a model.
"""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Shared strictness: unknown keys are dropped, strings are trimmed, NaN/Infinity are rejected.
_STRICT = ConfigDict(extra="ignore", str_strip_whitespace=True, allow_inf_nan=False)

# Currency names and symbols a model sometimes writes instead of the ISO 4217 code.
_CURRENCY_ALIASES = {
    "£": "GBP",
    "POUND": "GBP",
    "POUNDS": "GBP",
    "STERLING": "GBP",
    "€": "EUR",
    "EURO": "EUR",
    "EUROS": "EUR",
    "$": "USD",
    "US$": "USD",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "₹": "INR",
    "RS": "LKR",
    "RS.": "LKR",
}
_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")


class UnitRate(BaseModel):
    model_config = _STRICT

    label: str | None = Field(
        None, description='Rate band as printed, e.g. "Standard", "Day", "Night", "Peak".'
    )
    rate: float = Field(ge=0, description="Unit rate number exactly as printed, e.g. 24.86.")
    rate_unit: str = Field(
        min_length=1,
        description='Unit exactly as printed, e.g. "p/kWh", "£/kWh", "LKR/kWh", "fils/kWh".',
    )
    kwh: float | None = Field(None, ge=0, description="kWh charged at this rate, if shown.")


class OtherCharge(BaseModel):
    model_config = _STRICT

    label: str = Field(
        min_length=1, description='Line as printed, e.g. "Roundup", "Fuel adjustment", "SSCL".'
    )
    amount: float = Field(description="Amount as printed; negative for a credit or discount.")


class BillExtraction(BaseModel):
    """Fields read from one electricity bill. Use null for anything not printed on the bill."""

    model_config = _STRICT

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
        None, ge=1, description="Number of days in the bill period, only if printed on the bill."
    )
    previous_reading: float | None = Field(
        None, ge=0, description="Previous cumulative meter reading (single-register meters only)."
    )
    current_reading: float | None = Field(
        None, ge=0, description="Current cumulative meter reading (single-register meters only)."
    )
    reading_type: str | None = Field(
        None, description='How the current reading was taken, e.g. "actual", "estimated", "smart".'
    )
    consumption_kwh: float | None = Field(None, ge=0, description="Total kWh used in the period.")
    unit_rates: list[UnitRate] = Field(
        default_factory=list, description="Each unit rate charged, one entry per band."
    )
    standing_charge: float | None = Field(
        None, ge=0, description="Standing/fixed charge rate as printed, e.g. 53.35."
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

    @field_validator("currency", mode="before")
    @classmethod
    def _iso_currency(cls, value):
        """Accept "gbp", "£" or "pounds" as GBP; reject anything that is not an ISO code."""
        if not isinstance(value, str):
            return value
        text = value.strip().upper()
        if not text:
            return None
        text = _CURRENCY_ALIASES.get(text, text)
        if not _ISO_CURRENCY.match(text):
            raise ValueError("currency must be a 3-letter ISO 4217 code such as GBP")
        return text


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
    fields: list[str] = Field(
        default_factory=list, description="Extraction fields this check looks at."
    )


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
    flagged_fields: list[str] = Field(
        default_factory=list,
        description="Fields involved in a failed check: what a reviewer should look at first.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Notes about the input that do not fail a check, e.g. pages not read.",
    )
    meta: ExtractionMeta


def extraction_json_schema() -> dict:
    """JSON schema handed to the model (tool input schema / response format)."""
    return BillExtraction.model_json_schema()
