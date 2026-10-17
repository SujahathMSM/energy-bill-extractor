"""Unit normalisation, derived figures and plausibility checks.

The model reads; this module does the arithmetic. Every check is independent and reports
pass / fail / skipped, so a reviewer can see exactly why a bill was flagged. A failed check
does not mean the model was wrong (bills do contain rounding quirks), only that a person
should look before the numbers are used.
"""

import re
from dataclasses import dataclass

from .schema import BillExtraction, Check, Derived, NormalisedRate

# Minor units: (currency if unambiguous, factor to major units)
_MINOR_UNITS: dict[str, tuple[str | None, float]] = {
    "p": ("GBP", 0.01),
    "pence": ("GBP", 0.01),
    "c": (None, 0.01),
    "ct": (None, 0.01),
    "cent": (None, 0.01),
    "cents": (None, 0.01),
    "¢": (None, 0.01),
    "fils": ("AED", 0.01),
}
_MAJOR_UNITS: dict[str, str] = {
    "£": "GBP",
    "gbp": "GBP",
    "€": "EUR",
    "eur": "EUR",
    "$": "USD",
    "usd": "USD",
    "aed": "AED",
    "dhs": "AED",
    "dh": "AED",
    "lkr": "LKR",
    "rs": "LKR",
    "rs.": "LKR",
    "inr": "INR",
    "₹": "INR",
}
_PER: dict[str, str] = {
    "kwh": "kwh",
    "unit": "kwh",
    "day": "day",
    "d": "day",
    "month": "month",
    "mth": "month",
    "mo": "month",
    "year": "year",
    "yr": "year",
    "annum": "year",
}

# Plausible unit rates in major currency units per kWh. Wide on purpose: the check is for
# unit mix-ups (pence read as pounds) and misread digits, not for judging the tariff.
PLAUSIBLE_RATE_PER_KWH: dict[str, tuple[float, float]] = {
    "GBP": (0.05, 1.00),
    "EUR": (0.05, 1.00),
    "USD": (0.03, 1.00),
    "AED": (0.05, 1.00),
    "LKR": (1.0, 200.0),
    "INR": (1.0, 25.0),
}

_MPAN_PRIMES = (3, 5, 7, 13, 17, 19, 23, 29, 31, 37, 41, 43)


@dataclass(frozen=True)
class ParsedUnit:
    factor: float
    currency: str | None
    per: str


def parse_unit(unit: str | None) -> ParsedUnit | None:
    """Parse units such as "p/kWh", "pence per kWh", "£/day" or "LKR/month"."""
    if not unit:
        return None
    text = unit.strip().lower().replace(" per ", "/").replace(" a ", "/").replace(" ", "")
    if "/" not in text:
        return None
    money, per = text.split("/", 1)
    per_key = _PER.get(per.strip("."))
    if per_key is None:
        return None
    if money in _MINOR_UNITS:
        currency, factor = _MINOR_UNITS[money]
        return ParsedUnit(factor, currency, per_key)
    if money in _MAJOR_UNITS:
        return ParsedUnit(1.0, _MAJOR_UNITS[money], per_key)
    return None


def mpan_check_digit(first_twelve: str) -> int:
    """Check digit for a GB MPAN core (the last of its 13 digits)."""
    total = sum(int(d) * p for d, p in zip(first_twelve, _MPAN_PRIMES, strict=True))
    return total % 11 % 10


def mpan_core(value: str | None) -> str | None:
    """Return the 13-digit MPAN core if `value` looks like one (13 digits, or a full 21)."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 21:
        digits = digits[-13:]
    return digits if len(digits) == 13 else None


def _close(a: float, b: float, abs_tol: float, rel_tol: float = 0.0) -> bool:
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))


def _per_day(value: float | None, unit: ParsedUnit | None) -> float | None:
    if value is None or unit is None:
        return None
    major = value * unit.factor
    return {"day": major, "month": major * 12 / 365, "year": major / 365}.get(unit.per)


def analyse(ex: BillExtraction) -> tuple[Derived, list[Check]]:
    checks: list[Check] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append(Check(name=name, status=status, detail=detail))

    # --- units and currency -------------------------------------------------------------
    rate_units = [parse_unit(r.rate_unit) for r in ex.unit_rates]
    standing_unit = parse_unit(ex.standing_charge_unit)
    hints = {u.currency for u in [*rate_units, standing_unit] if u and u.currency}
    declared = ex.currency.strip().upper() if ex.currency else None
    currency = declared or (next(iter(hints)) if len(hints) == 1 else None)

    unparsed = [r.rate_unit for r, u in zip(ex.unit_rates, rate_units, strict=True) if u is None]
    not_per_kwh = [
        r.rate_unit
        for r, u in zip(ex.unit_rates, rate_units, strict=True)
        if u is not None and u.per != "kwh"
    ]
    if ex.standing_charge_unit and standing_unit is None:
        unparsed.append(ex.standing_charge_unit)

    # --- derived figures ----------------------------------------------------------------
    counted_days = None
    if ex.billing_period_start and ex.billing_period_end:
        delta = (ex.billing_period_end - ex.billing_period_start).days
        if delta >= 0:
            counted_days = delta + 1  # most bills count both the first and last day
    # Suppliers differ (some count nights, not days), so a printed day count wins.
    printed_days = ex.period_days if ex.period_days and ex.period_days > 0 else None
    period_days = printed_days or counted_days

    rates = [
        NormalisedRate(
            label=r.label,
            per_kwh=round(r.rate * u.factor, 6) if u and u.per == "kwh" else None,
            kwh=r.kwh,
        )
        for r, u in zip(ex.unit_rates, rate_units, strict=True)
    ]
    standing_per_day = _per_day(ex.standing_charge, standing_unit)
    avg_daily = (
        ex.consumption_kwh / period_days if ex.consumption_kwh is not None and period_days else None
    )
    derived = Derived(
        currency=currency,
        period_days=period_days,
        unit_rates=rates,
        standing_charge_per_day=round(standing_per_day, 6) if standing_per_day else None,
        avg_daily_kwh=round(avg_daily, 3) if avg_daily is not None else None,
        estimated_annual_kwh=round(avg_daily * 365) if avg_daily is not None else None,
    )

    # --- checks ---------------------------------------------------------------------------
    missing = [
        label
        for label, present in [
            ("supplier_name", ex.supplier_name),
            ("meter_point_id or account_number", ex.meter_point_id or ex.account_number),
            ("billing_period_start", ex.billing_period_start),
            ("billing_period_end", ex.billing_period_end),
            ("consumption_kwh", ex.consumption_kwh is not None),
            ("unit_rates", ex.unit_rates),
        ]
        if not present
    ]
    if missing:
        add("onboarding_fields", "fail", "Missing: " + ", ".join(missing))
    else:
        add("onboarding_fields", "pass", "All fields needed for onboarding are present.")

    if not (ex.billing_period_start and ex.billing_period_end):
        if printed_days:
            add("billing_period", "skipped", f"{printed_days} days printed; dates missing.")
        else:
            add("billing_period", "skipped", "Start or end date missing.")
    elif counted_days is None:
        add("billing_period", "fail", "Period ends before it starts.")
    elif period_days > 400:
        add("billing_period", "fail", f"Period of {period_days} days is implausibly long.")
    elif printed_days is None:
        add("billing_period", "pass", f"{period_days} days, counting both end dates.")
    elif abs(printed_days - counted_days) <= 1:
        note = "" if printed_days == counted_days else f" (dates span {counted_days} counting both)"
        add("billing_period", "pass", f"{printed_days} days as printed{note}.")
    else:
        add(
            "billing_period",
            "fail",
            f"Bill prints {printed_days} days but the dates span {counted_days}.",
        )

    if unparsed or not_per_kwh:
        problems = [f"'{u}' not understood" for u in unparsed]
        problems += [f"'{u}' is not a per-kWh rate" for u in not_per_kwh]
        add("units_understood", "fail", "; ".join(problems))
    elif ex.unit_rates or ex.standing_charge_unit:
        add("units_understood", "pass", "All rate units understood.")
    else:
        add("units_understood", "skipped", "No units to interpret.")

    if declared and hints and hints != {declared}:
        add(
            "currency_consistent",
            "fail",
            f"Bill currency {declared} but units imply {', '.join(sorted(hints))}.",
        )
    elif declared and hints:
        add("currency_consistent", "pass", f"Units agree with {declared}.")
    else:
        add("currency_consistent", "skipped", "Not enough information.")

    known = [r for r in rates if r.per_kwh is not None]
    bounds = PLAUSIBLE_RATE_PER_KWH.get(currency or "")
    if not known or bounds is None:
        add("rates_plausible", "skipped", "No normalised rates or unknown currency.")
    else:
        low, high = bounds
        outside = [r for r in known if not low <= r.per_kwh <= high]
        if outside:
            listed = ", ".join(f"{r.label or 'rate'}={r.per_kwh:g} {currency}/kWh" for r in outside)
            add("rates_plausible", "fail", f"Outside {low:g}-{high:g} {currency}/kWh: {listed}")
        else:
            add("rates_plausible", "pass", f"All rates within {low:g}-{high:g} {currency}/kWh.")

    if None in (ex.previous_reading, ex.current_reading, ex.consumption_kwh):
        add("readings_match_consumption", "skipped", "Readings or consumption missing.")
    else:
        diff = ex.current_reading - ex.previous_reading
        if _close(diff, ex.consumption_kwh, abs_tol=1.0, rel_tol=0.005):
            add("readings_match_consumption", "pass", f"{diff:g} kWh between readings.")
        else:
            add(
                "readings_match_consumption",
                "fail",
                f"Readings differ by {diff:g} kWh but consumption is {ex.consumption_kwh:g} kWh.",
            )

    band_kwh = [r.kwh for r in ex.unit_rates]
    if not ex.unit_rates or None in band_kwh or ex.consumption_kwh is None:
        add("bands_match_consumption", "skipped", "Per-band kWh or consumption missing.")
    else:
        total_kwh = sum(band_kwh)
        if _close(total_kwh, ex.consumption_kwh, abs_tol=1.0, rel_tol=0.005):
            add("bands_match_consumption", "pass", f"Bands add up to {total_kwh:g} kWh.")
        else:
            add(
                "bands_match_consumption",
                "fail",
                f"Bands add up to {total_kwh:g} kWh, consumption is {ex.consumption_kwh:g} kWh.",
            )

    # Energy cost: each band's kWh (or total consumption when there is a single band).
    energy_parts: list[float] | None = []
    for rate in rates:
        kwh = rate.kwh
        if kwh is None and len(rates) == 1:
            kwh = ex.consumption_kwh
        if rate.per_kwh is None or kwh is None:
            energy_parts = None
            break
        energy_parts.append(rate.per_kwh * kwh)
    if not rates or energy_parts is None or ex.subtotal_excl_tax is None:
        add("cost_reconciles", "skipped", "Rates, kWh or subtotal missing.")
    elif ex.standing_charge is not None and (standing_per_day is None or period_days is None):
        add(
            "cost_reconciles", "skipped", "Standing charge could not be converted to a period cost."
        )
    else:
        standing_cost = (standing_per_day or 0.0) * (period_days or 0)
        expected = sum(energy_parts) + standing_cost
        if _close(expected, ex.subtotal_excl_tax, abs_tol=0.5, rel_tol=0.02):
            add(
                "cost_reconciles",
                "pass",
                f"Rates x kWh + standing charge = {expected:.2f}, "
                f"subtotal {ex.subtotal_excl_tax:.2f}.",
            )
        else:
            add(
                "cost_reconciles",
                "fail",
                f"Rates x kWh + standing charge = {expected:.2f}, "
                f"but subtotal is {ex.subtotal_excl_tax:.2f}.",
            )

    if None in (ex.subtotal_excl_tax, ex.tax_amount, ex.total_amount):
        add("total_reconciles", "skipped", "Subtotal, tax or total missing.")
    else:
        other = sum(c.amount for c in ex.other_charges)
        expected_total = ex.subtotal_excl_tax + ex.tax_amount + other
        parts = "subtotal + tax"
        if ex.other_charges:
            n = len(ex.other_charges)
            parts += f" + {n} other charge{'s' if n != 1 else ''} ({other:.2f})"
        if _close(expected_total, ex.total_amount, abs_tol=0.02):
            add("total_reconciles", "pass", f"{expected_total:.2f} = {parts}.")
        else:
            add(
                "total_reconciles",
                "fail",
                f"{parts.capitalize()} = {expected_total:.2f}, but total is {ex.total_amount:.2f}.",
            )

    core = mpan_core(ex.meter_point_id)
    if core is None or currency not in (None, "GBP"):
        add("meter_point_checksum", "skipped", "Only defined for GB MPANs.")
    elif mpan_check_digit(core[:12]) == int(core[12]):
        add("meter_point_checksum", "pass", "MPAN check digit is valid.")
    else:
        add(
            "meter_point_checksum", "fail", "MPAN check digit is wrong; a digit was likely misread."
        )

    return derived, checks
