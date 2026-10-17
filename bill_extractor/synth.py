"""Generate synthetic UK electricity bills with exact ground truth.

Each bill is rendered three ways: a digital PDF, a PNG scan, and a "phone photo" JPEG
(rotated, blurred, unevenly lit), so the evaluation shows how accuracy changes with input
quality. Suppliers, people and addresses are fictional and every page is watermarked.

Clean bills are internally consistent, so every check passes. Planted-fault bills
(`--faulty N`) print one deliberate inconsistency -- a wrong total, a bad MPAN check
digit, swapped readings, a day count that disagrees with the dates, a consumption
figure that disagrees with the bands/readings, or no unit rate at all -- while the
ground truth records exactly what is printed. A faithful extraction therefore still
scores 100% on fields, but the checks must flag the bill for review. That is what
makes the review-flag catch rate measurable: on clean bills the flag should stay
off, on faulty bills it must turn on.

    python -m bill_extractor.synth --n 24 --out data/synthetic
    python -m bill_extractor.synth --n 24 --faulty 12 --out data/synthetic
    python -m bill_extractor.synth --n 24 --degraded --out data/synthetic
"""

import argparse
import io
import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from .preprocess import _PDFIUM_LOCK
from .schema import BillExtraction, UnitRate
from .validation import mpan_check_digit

SUPPLIERS = [
    {"name": "Fernwick Energy", "colour": "#1F6F5C"},
    {"name": "Halcyon Volt", "colour": "#3A4FA3"},
    {"name": "Brackenridge Power", "colour": "#8A3B2E"},
    {"name": "Tidewell Energy", "colour": "#0E7490"},
]
CUSTOMERS = ["Mr J. Fernando", "Ms R. Patel", "Mr D. Hughes", "Mrs K. Owusu", "Ms L. Novak"]
STREETS = ["Alder Close", "Mill Lane", "Station Road", "Orchard Way", "Kingfisher Drive"]
TOWNS = [("Harrowgate Vale", "HV"), ("Northmere", "NM"), ("Castleford Down", "CD")]
TEMPLATES = ("single_rate", "economy7", "summary_first")
DATE_STYLES = ("%d %b %Y", "%d %B %Y", "%d/%m/%Y")
UNIT_STYLES = (("p/kWh", "p/day"), ("p per kWh", "p per day"), ("pence/kWh", "pence/day"))
VAT_RATE = Decimal("0.05")  # UK domestic energy

# Deliberate faults that can be planted in a printed bill. Each one targets one
# check: the ground truth records what is printed (fault included), so a faithful
# extraction still scores 100% on fields, but the checks must flag the bill.
FAULT_TYPES = (
    "wrong_total",
    "bad_mpan",
    "swapped_readings",
    "wrong_period_days",
    "wrong_consumption",
    "missing_unit_rates",
)
FAULT_TO_CHECK = {
    "wrong_total": "total_reconciles",
    "bad_mpan": "meter_point_checksum",
    "swapped_readings": "readings_match_consumption",
    "wrong_period_days": "billing_period",
    # Wrong consumption breaks both bands_match and (on metered bills)
    # readings_match; bands_match fails on every template, so it is the expected one.
    "wrong_consumption": "bands_match_consumption",
    "missing_unit_rates": "onboarding_fields",
}


def applicable_faults(template: str) -> list[str]:
    """Faults that can be printed on a bill of this template."""
    if template == "economy7":
        # Economy 7 bills have no cumulative readings to swap.
        return [f for f in FAULT_TYPES if f != "swapped_readings"]
    return list(FAULT_TYPES)


WIDTH, HEIGHT = A4
MARGIN = 42


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _gbp(value: Decimal) -> str:
    return f"£{value:,.2f}"


def _fmt_rate(number: str, unit: str) -> str:
    """'24.86' + 'p/kWh' -> '24.86p/kWh'; '24.86' + 'pence/kWh' -> '24.86 pence/kWh'."""
    joined = unit.startswith("p/") or unit.startswith("p per")
    return f"{number}{'' if joined else ' '}{unit}"


@dataclass
class Charge:
    label: str
    detail: str
    amount: Decimal


@dataclass
class SyntheticBill:
    bill_id: str
    template: str
    supplier: dict
    customer: str
    address: list[str]
    issue_date: date
    date_style: str
    truth: BillExtraction
    charges: list[Charge]
    previous_balance: Decimal
    payments: Decimal
    amount_due: Decimal
    rate_decimals: int
    fault: str | None = None

    def d(self, value: date) -> str:
        text = value.strftime(self.date_style)
        return text.lstrip("0") if self.date_style.startswith("%d ") else text


def _apply_fault(bill: SyntheticBill, fault: str) -> None:
    """Rewrite a clean bill so the printed page carries one deliberate inconsistency.

    The ground truth is rewritten to match what is printed, fault included, so a
    faithful extraction still scores 100% on fields. Other figures are kept
    consistent where possible, so the fault targets one check.
    """
    if fault not in FAULT_TYPES:
        raise ValueError(f"Unknown fault: {fault}")
    bill.fault = fault
    t = bill.truth
    if fault == "wrong_total":
        faulty_total = _money(Decimal(f"{t.total_amount:.2f}") + Decimal("5.00"))
        t.total_amount = float(faulty_total)
        bill.amount_due = bill.previous_balance - bill.payments + faulty_total
    elif fault == "bad_mpan":
        core = t.meter_point_id
        for delta in (1, 2, 3):
            candidate = core[:5] + str((int(core[5]) + delta) % 10) + core[6:]
            if mpan_check_digit(candidate[:12]) != int(candidate[12]):
                t.meter_point_id = candidate
                break
        else:  # pragma: no cover - a delta that breaks the digit always exists
            raise ValueError("Could not break the MPAN check digit")
    elif fault == "swapped_readings":
        if t.previous_reading is None or t.current_reading is None:
            raise ValueError("swapped_readings needs cumulative readings")
        t.previous_reading, t.current_reading = t.current_reading, t.previous_reading
    elif fault == "wrong_period_days":
        counted = (t.billing_period_end - t.billing_period_start).days + 1
        printed = counted + 5
        # Recompute the standing charge on the printed days so the cost check
        # still reconciles and only the billing-period check fails.
        standing_rate = Decimal(f"{t.standing_charge:.2f}")
        energy_total = sum((c.amount for c in bill.charges[:-1]), Decimal("0"))
        new_standing = _money(standing_rate * printed / 100)
        bill.charges[-1] = Charge(
            "Standing charge",
            f"{printed} days at {_fmt_rate(f'{standing_rate:.2f}', t.standing_charge_unit)}",
            new_standing,
        )
        new_subtotal = energy_total + new_standing
        new_vat = _money(new_subtotal * VAT_RATE)
        new_total = new_subtotal + new_vat
        t.period_days = printed
        t.subtotal_excl_tax = float(new_subtotal)
        t.tax_amount = float(new_vat)
        t.total_amount = float(new_total)
        bill.amount_due = bill.previous_balance - bill.payments + new_total
    elif fault == "wrong_consumption":
        t.consumption_kwh = float(t.consumption_kwh + 50)
    elif fault == "missing_unit_rates":
        # The bill prints no unit rate at all (like the Oklahoma sample): the
        # energy lines go, the standing line stays, and the totals still include
        # the energy cost. Onboarding must fail; the total still reconciles.
        t.unit_rates = []
        bill.charges = [bill.charges[-1]]


def make_bill(index: int, rng: random.Random, fault: str | None = None) -> SyntheticBill:
    template = TEMPLATES[index % len(TEMPLATES)]
    supplier = rng.choice(SUPPLIERS)
    brand = supplier["name"].split()[0]
    rate_unit, standing_unit = rng.choice(UNIT_STYLES)
    decimals = rng.choice((2, 2, 3))

    start = date(2025, 10, 1) + timedelta(days=rng.randint(0, 330))
    days = rng.randint(28, 35)
    end = start + timedelta(days=days - 1)
    standing = Decimal(f"{rng.uniform(45, 65):.2f}")

    def rate(low: float, high: float) -> Decimal:
        return Decimal(f"{rng.uniform(low, high):.{decimals}f}")

    if template == "economy7":
        bands = [
            ("Day", rate(26, 33), rng.randint(120, 380)),
            ("Night", rate(11, 16), rng.randint(50, 220)),
        ]
        previous = current = None
        reading_type = "smart"
        tariff = rng.choice([f"{brand} Economy 7 Fixed", "Economy 7 Variable"])
    else:
        kwh = rng.randint(120, 520)
        bands = [("Standard", rate(21, 30), kwh)]
        previous = rng.randint(8_000, 60_000)
        current = previous + kwh
        reading_type = rng.choice(["actual", "estimated"])
        month = rng.choice(["Mar", "Jun", "Nov"])
        tariff = rng.choice([f"{brand} Fixed {month} 2027", f"{brand} Flex", "Standard Variable"])

    charges = [
        Charge(
            label,
            f"{kwh} kWh at {_fmt_rate(f'{r:.{decimals}f}', rate_unit)}",
            _money(r * kwh / 100),
        )
        for label, r, kwh in bands
    ]
    charges.append(
        Charge(
            "Standing charge",
            f"{days} days at {_fmt_rate(f'{standing:.2f}', standing_unit)}",
            _money(standing * days / 100),
        )
    )
    subtotal = sum((c.amount for c in charges), Decimal("0"))
    vat = _money(subtotal * VAT_RATE)
    total = subtotal + vat

    if template == "summary_first":
        previous_balance = _money(Decimal(f"{rng.uniform(-30, 80):.2f}"))
        payments = previous_balance if previous_balance > 0 and rng.random() < 0.7 else Decimal("0")
    else:
        previous_balance = payments = Decimal("0")
    amount_due = previous_balance - payments + total

    first12 = f"{rng.randint(10, 23)}{rng.randint(0, 9_999_999_999):010d}"
    town, prefix = rng.choice(TOWNS)

    truth = BillExtraction(
        supplier_name=supplier["name"],
        account_number=str(rng.randint(10_000_000, 99_999_999)),
        meter_point_id=first12 + str(mpan_check_digit(first12)),
        meter_serial=f"{rng.randint(10, 24)}{rng.choice('EKLP')}{rng.randint(10**6, 10**7 - 1)}",
        tariff_name=tariff,
        currency="GBP",
        billing_period_start=start,
        billing_period_end=end,
        period_days=days,  # every layout prints it on the standing-charge line
        previous_reading=previous,
        current_reading=current,
        reading_type=reading_type,
        consumption_kwh=float(sum(kwh for _, _, kwh in bands)),
        unit_rates=[
            UnitRate(label=label, rate=float(r), rate_unit=rate_unit, kwh=float(kwh))
            for label, r, kwh in bands
        ],
        standing_charge=float(standing),
        standing_charge_unit=standing_unit,
        subtotal_excl_tax=float(subtotal),
        tax_amount=float(vat),
        total_amount=float(total),
    )

    bill = SyntheticBill(
        bill_id=f"bill_{index:03d}",
        template=template,
        supplier=supplier,
        customer=rng.choice(CUSTOMERS),
        address=[
            f"{rng.randint(2, 180)} {rng.choice(STREETS)}",
            town,
            f"{prefix}{rng.randint(1, 9)} {rng.randint(1, 9)}{rng.choice('ABDEFGHJLNPQRSTUWXY')}"
            f"{rng.choice('ABDEFGHJLNPQRSTUWXY')}",
        ],
        issue_date=end + timedelta(days=rng.randint(2, 6)),
        date_style=rng.choice(DATE_STYLES),
        truth=truth,
        charges=charges,
        previous_balance=previous_balance,
        payments=payments,
        amount_due=amount_due,
        rate_decimals=decimals,
    )
    if fault is not None:
        _apply_fault(bill, fault)
    return bill


# --- rendering ------------------------------------------------------------------------------


class _Page:
    """Small drawing helper over a reportlab canvas, y measured from the top of the page."""

    def __init__(self, c: canvas.Canvas, accent: str) -> None:
        self.c = c
        self.accent = HexColor(accent)

    def text(self, x, y, s, size=10, bold=False, colour=None, align="left"):
        self.c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        self.c.setFillColor(colour or HexColor("#1F2328"))
        draw = {"left": self.c.drawString, "right": self.c.drawRightString}[align]
        draw(x, HEIGHT - y, s)

    def rule(self, y, x0=MARGIN, x1=WIDTH - MARGIN, colour="#D0D7DE"):
        self.c.setStrokeColor(HexColor(colour))
        self.c.setLineWidth(0.6)
        self.c.line(x0, HEIGHT - y, x1, HEIGHT - y)

    def box(self, x, y, w, h, fill="#F3F5F7", stroke=None):
        self.c.setFillColor(HexColor(fill))
        if stroke:
            self.c.setStrokeColor(HexColor(stroke))
        self.c.rect(x, HEIGHT - y - h, w, h, fill=1, stroke=1 if stroke else 0)

    def heading(self, y, s):
        self.text(MARGIN, y, s, size=12, bold=True, colour=self.accent)
        self.rule(y + 6)


def _header(p: _Page, bill: SyntheticBill) -> None:
    p.c.setFillColor(p.accent)
    p.c.rect(0, HEIGHT - 78, WIDTH, 78, fill=1, stroke=0)
    p.text(MARGIN, 46, bill.supplier["name"], size=22, bold=True, colour=white)
    p.text(WIDTH - MARGIN, 40, "Your electricity bill", size=12, colour=white, align="right")
    p.text(
        WIDTH - MARGIN,
        58,
        f"Bill date: {bill.d(bill.issue_date)}",
        size=9,
        colour=white,
        align="right",
    )

    y = 112
    p.text(MARGIN, y, bill.customer, size=10, bold=True)
    for i, line in enumerate(bill.address):
        p.text(MARGIN, y + 14 * (i + 1), line, size=10)
    p.box(WIDTH - MARGIN - 200, 96, 200, 52)
    p.text(WIDTH - MARGIN - 188, 114, "Account number", size=8, colour=HexColor("#57606A"))
    p.text(WIDTH - MARGIN - 188, 132, bill.truth.account_number, size=13, bold=True)


def _footer_and_watermark(p: _Page) -> None:
    p.c.saveState()
    p.c.setFillColor(Color(0.55, 0.55, 0.55, alpha=0.13))
    p.c.setFont("Helvetica-Bold", 64)
    p.c.translate(WIDTH / 2, HEIGHT / 2)
    p.c.rotate(35)
    p.c.drawCentredString(0, 0, "SYNTHETIC SAMPLE")
    p.c.restoreState()
    p.rule(HEIGHT - 48)
    p.text(
        MARGIN,
        HEIGHT - 34,
        "Synthetic sample for software testing. Not issued by any energy supplier.",
        size=7.5,
        colour=HexColor("#57606A"),
    )


def _mpan_display(core: str) -> str:
    return f"{core[:2]} {core[2:6]} {core[6:10]} {core[10:]}"


def _details_block(p: _Page, bill: SyntheticBill, y: float) -> float:
    t = bill.truth
    rows = [
        ("Tariff", t.tariff_name),
        ("Supply number (MPAN)", _mpan_display(t.meter_point_id)),
        ("Meter serial number", t.meter_serial),
    ]
    for label, value in rows:
        p.text(MARGIN, y, label, size=9.5, colour=HexColor("#57606A"))
        p.text(MARGIN + 150, y, value, size=9.5, bold=True)
        y += 16
    return y


def _charges_table(p: _Page, bill: SyntheticBill, y: float) -> float:
    for charge in bill.charges:
        p.text(MARGIN, y, charge.label, size=10, bold=True)
        p.text(MARGIN + 130, y, charge.detail, size=10)
        p.text(WIDTH - MARGIN, y, _gbp(charge.amount), size=10, align="right")
        y += 18
    p.rule(y - 8)
    t = bill.truth
    rows = [
        ("Charges before VAT", Decimal(f"{t.subtotal_excl_tax:.2f}"), False),
        ("VAT at 5%", Decimal(f"{t.tax_amount:.2f}"), False),
        ("Total electricity charges", Decimal(f"{t.total_amount:.2f}"), True),
    ]
    for label, amount, bold in rows:
        y += 6
        p.text(MARGIN + 130, y, label, size=10.5 if bold else 10, bold=bold)
        p.text(WIDTH - MARGIN, y, _gbp(amount), size=10.5 if bold else 10, bold=bold, align="right")
        y += 12
    return y


def _readings(p: _Page, bill: SyntheticBill, y: float) -> float:
    t = bill.truth
    code = {"actual": "A", "estimated": "E"}[t.reading_type]
    p.text(MARGIN, y, "Meter readings", size=10, bold=True)
    y += 16
    for label, when, value, flag in [
        ("Previous reading", t.billing_period_start, t.previous_reading, "A"),
        ("Latest reading", t.billing_period_end, t.current_reading, code),
    ]:
        p.text(MARGIN, y, label, size=10)
        p.text(MARGIN + 150, y, bill.d(when), size=10)
        p.text(MARGIN + 300, y, f"{int(value):05d} {flag}", size=10, bold=True)
        y += 15
    p.text(
        MARGIN, y, "A = actual reading, E = estimated reading", size=8, colour=HexColor("#57606A")
    )
    y += 18
    p.text(
        MARGIN, y, f"Electricity used this period: {int(t.consumption_kwh)} kWh", size=10, bold=True
    )
    return y + 22


def _period(bill: SyntheticBill) -> str:
    t = bill.truth
    # The printed day count wins: on clean bills it equals the date span, on a
    # wrong_period_days bill it is the faulty printed figure the reader must copy.
    days = t.period_days or (t.billing_period_end - t.billing_period_start).days + 1
    return f"{bill.d(t.billing_period_start)} to {bill.d(t.billing_period_end)} ({days} days)"


def _render_single_rate(p: _Page, bill: SyntheticBill) -> None:
    y = 200
    p.heading(y, "Your bill at a glance")
    y += 28
    p.text(MARGIN, y, f"Billing period: {_period(bill)}", size=10)
    y += 16
    p.text(
        MARGIN,
        y,
        f"Total charges this period: {_gbp(Decimal(f'{bill.truth.total_amount:.2f}'))}",
        size=10,
    )
    y += 16
    due = bill.issue_date + timedelta(days=14)
    p.text(
        MARGIN,
        y,
        f"Amount due: {_gbp(bill.amount_due)}, paid by Direct Debit on {bill.d(due)}",
        size=10,
    )
    y += 36
    p.heading(y, "Your electricity")
    y = _details_block(p, bill, y + 28) + 10
    y = _readings(p, bill, y)
    p.heading(y, "Your charges")
    _charges_table(p, bill, y + 28)


def _render_economy7(p: _Page, bill: SyntheticBill) -> None:
    t = bill.truth
    y = 200
    p.heading(y, f"Electricity charges, {_period(bill)}")
    y += 26
    p.text(MARGIN, y, "Readings were sent automatically by your smart meter.", size=9.5)
    y += 26
    headers = [("Rate", MARGIN), ("Units used", MARGIN + 120), ("Unit rate", MARGIN + 230)]
    for label, x in headers:
        p.text(x, y, label, size=9, bold=True, colour=HexColor("#57606A"))
    p.text(WIDTH - MARGIN, y, "Cost", size=9, bold=True, colour=HexColor("#57606A"), align="right")
    p.rule(y + 6)
    y += 22
    if t.unit_rates:
        unit = t.unit_rates[0].rate_unit
        for band, charge in zip(t.unit_rates, bill.charges, strict=False):
            p.text(MARGIN, y, band.label, size=10, bold=True)
            p.text(MARGIN + 120, y, f"{int(band.kwh)} kWh", size=10)
            p.text(MARGIN + 230, y, _fmt_rate(f"{band.rate:.{bill.rate_decimals}f}", unit), size=10)
            p.text(WIDTH - MARGIN, y, _gbp(charge.amount), size=10, align="right")
            y += 18
    else:
        p.text(MARGIN, y, "Unit rate not printed on this bill", size=10)
        y += 18
    standing = bill.charges[-1]
    days = t.period_days or (t.billing_period_end - t.billing_period_start).days + 1
    p.text(MARGIN, y, "Standing charge", size=10, bold=True)
    p.text(MARGIN + 120, y, f"{days} days", size=10)
    p.text(MARGIN + 230, y, _fmt_rate(f"{t.standing_charge:.2f}", t.standing_charge_unit), size=10)
    p.text(WIDTH - MARGIN, y, _gbp(standing.amount), size=10, align="right")
    y += 10
    p.rule(y)
    y += 18
    p.text(MARGIN, y, f"Total units used: {int(t.consumption_kwh)} kWh", size=10, bold=True)
    for label, amount, bold in [
        ("Charges before VAT", t.subtotal_excl_tax, False),
        ("VAT at 5%", t.tax_amount, False),
        ("Total", t.total_amount, True),
    ]:
        p.text(MARGIN + 260, y, label, size=10, bold=bold)
        p.text(WIDTH - MARGIN, y, _gbp(Decimal(f"{amount:.2f}")), size=10, bold=bold, align="right")
        y += 16
    y += 24
    p.heading(y, "Your supply")
    _details_block(p, bill, y + 28)


def _render_summary_first(p: _Page, bill: SyntheticBill) -> None:
    t = bill.truth
    y = 196
    p.box(WIDTH - MARGIN - 190, y - 8, 190, 70, fill="#FFF4D6")
    p.text(WIDTH - MARGIN - 178, y + 14, "Amount due", size=10, bold=True)
    p.text(WIDTH - MARGIN - 178, y + 42, _gbp(bill.amount_due), size=22, bold=True)

    p.text(MARGIN, y + 6, "Account summary", size=12, bold=True, colour=p.accent)
    balance = bill.previous_balance
    rows = [
        (
            "Balance from last bill",
            f"{_gbp(abs(balance))} {'credit' if balance < 0 else ''}".strip(),
        ),
        ("Payments received", f"-{_gbp(bill.payments)}" if bill.payments else _gbp(Decimal("0"))),
        ("This bill's charges", _gbp(Decimal(f"{t.total_amount:.2f}"))),
    ]
    yy = y + 26
    for label, value in rows:
        p.text(MARGIN, yy, label, size=10)
        p.text(MARGIN + 250, yy, value, size=10, align="right")
        yy += 15

    y = 300
    p.heading(y, f"Charges for {bill.d(t.billing_period_start)} - {bill.d(t.billing_period_end)}")
    y = _readings(p, bill, y + 28)
    y = _charges_table(p, bill, y + 4)
    y += 30
    p.heading(y, "Tariff and meter details")
    _details_block(p, bill, y + 28)


_RENDERERS = {
    "single_rate": _render_single_rate,
    "economy7": _render_economy7,
    "summary_first": _render_summary_first,
}


def render_pdf(bill: SyntheticBill) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4, invariant=1)  # invariant: byte-identical reruns
    c.setTitle(f"Synthetic electricity bill {bill.bill_id}")
    page = _Page(c, bill.supplier["colour"])
    _header(page, bill)
    _RENDERERS[bill.template](page, bill)
    _footer_and_watermark(page)
    c.showPage()
    c.save()
    return buffer.getvalue()


def rasterise(pdf_bytes: bytes, dpi: int = 150) -> Image.Image:
    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(pdf_bytes)
        try:
            return pdf[0].render(scale=dpi / 72).to_pil().convert("RGB")
        finally:
            pdf.close()


DEGRADATION_LEVELS = ("clean", "mild", "moderate", "severe")

# Per-level photo damage: (rotate_deg, blur, brightness, contrast, glare_alpha, crop, jpeg_quality).
# "mild" keeps the original photo_variant ranges, so existing samples are unchanged.
_DEGRADATION_PARAMS = {
    "clean": ((1.0,), (0.0, 0.3), (0.9, 1.0), (0.95, 1.05), 0.0, 0.0, (85, 95)),
    "mild": ((3.0,), (0.4, 0.9), (0.6, 0.8), (0.85, 1.0), 0.0, 0.0, (55, 75)),
    "moderate": ((6.0,), (1.2, 1.8), (0.5, 0.7), (0.70, 0.85), 0.35, 0.05, (40, 55)),
    "severe": ((10.0,), (2.0, 3.0), (0.4, 0.6), (0.60, 0.75), 0.55, 0.10, (25, 40)),
}


def _glare(photo: Image.Image, rng: random.Random, alpha: float) -> Image.Image:
    """A bright spot as if a light reflected off the paper."""
    overlay = Image.new("RGBA", photo.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = photo.size
    cx, cy = rng.uniform(0.2, 0.8) * w, rng.uniform(0.2, 0.8) * h
    radius = rng.uniform(0.15, 0.30) * min(w, h)
    box = [cx - radius, cy - radius, cx + radius, cy + radius]
    draw.ellipse(box, fill=(255, 255, 255, int(255 * alpha)))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius / 3))
    return Image.alpha_composite(photo.convert("RGBA"), overlay).convert("RGB")


def _crop_edge(photo: Image.Image, rng: random.Random, fraction: float) -> Image.Image:
    """Cut `fraction` off one random edge, as if the photo missed the page edge."""
    w, h = photo.size
    side = rng.choice(("left", "right", "top", "bottom"))
    if side == "left":
        return photo.crop((int(w * fraction), 0, w, h))
    if side == "right":
        return photo.crop((0, 0, int(w * (1 - fraction)), h))
    if side == "top":
        return photo.crop((0, int(h * fraction), w, h))
    return photo.crop((0, 0, w, int(h * (1 - fraction))))


def photo_variant(img: Image.Image, rng: random.Random, level: str = "mild") -> Image.Image:
    """Make a scan look like a phone photo, with `level` of damage.

    Levels run from "clean" (nearly straight, sharp) to "severe" (tilted,
    blurred, dim, glare spot, one edge cropped). "mild" is the original photo.
    """
    if level not in _DEGRADATION_PARAMS:
        raise ValueError(f"Unknown degradation level {level!r}; use one of {DEGRADATION_LEVELS}.")
    (max_rotate,), blur, brightness, contrast, glare_alpha, crop, _ = _DEGRADATION_PARAMS[level]
    background = tuple(rng.randint(70, 120) for _ in range(3))
    pad = int(img.width * 0.06)
    framed = Image.new("RGB", (img.width + 2 * pad, img.height + 2 * pad), background)
    framed.paste(img, (pad, pad))
    photo = framed.rotate(
        rng.uniform(-max_rotate, max_rotate),
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=background,
    )
    # Uneven lighting: darken one side with a horizontal gradient.
    shade = Image.linear_gradient("L").rotate(90).resize(photo.size)
    darker = ImageEnhance.Brightness(photo).enhance(rng.uniform(*brightness))
    if rng.random() < 0.5:
        shade = shade.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    photo = Image.composite(photo, darker, shade)
    photo = ImageEnhance.Contrast(photo).enhance(rng.uniform(*contrast))
    photo = photo.filter(ImageFilter.GaussianBlur(rng.uniform(*blur)))
    if glare_alpha:
        photo = _glare(photo, rng, glare_alpha)
    if crop:
        photo = _crop_edge(photo, rng, crop)
    photo.thumbnail((1400, 1400))
    return photo


def degraded_quality(rng: random.Random, level: str) -> int:
    """JPEG quality for a degradation level (lower = more compression artefacts)."""
    if level not in _DEGRADATION_PARAMS:
        raise ValueError(f"Unknown degradation level {level!r}; use one of {DEGRADATION_LEVELS}.")
    low, high = _DEGRADATION_PARAMS[level][6]
    return rng.randint(low, high)


def generate(
    out_dir: Path, n: int = 24, seed: int = 7, faulty_n: int = 0, degraded: bool = False
) -> list[dict]:
    """Generate `n` bills, `faulty_n` of them carrying a planted fault.

    Faulty bills are spread evenly through the set, so an evaluation `--limit`
    still sees a mix of clean and faulty bills. Fault types cycle in
    FAULT_TYPES order, skipping faults that do not apply to a bill's template
    (swapped_readings on Economy 7).

    When `degraded` is set, each bill also gets one photo per degradation
    level (clean / mild / moderate / severe) as `photo_clean` etc., so an
    evaluation over those variants shows accuracy against photo damage.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    faulty_n = max(0, min(faulty_n, n))
    records = []
    next_fault = 0
    for index in range(n):
        rng = random.Random(seed * 10_000 + index)
        fault: str | None = None
        is_faulty_slot = (index + 1) * faulty_n // n > index * faulty_n // n if n else False
        if is_faulty_slot:
            template = TEMPLATES[index % len(TEMPLATES)]
            allowed = set(applicable_faults(template))
            # Cycle through FAULT_TYPES in global order, skipping faults that
            # do not apply to this template, so every fault type gets used.
            for offset in range(len(FAULT_TYPES)):
                candidate = FAULT_TYPES[(next_fault + offset) % len(FAULT_TYPES)]
                if candidate in allowed:
                    fault = candidate
                    next_fault = (FAULT_TYPES.index(candidate) + 1) % len(FAULT_TYPES)
                    break
        bill = make_bill(index, rng, fault=fault)
        pdf_bytes = render_pdf(bill)
        scan = rasterise(pdf_bytes)

        files = {
            "pdf": f"{bill.bill_id}.pdf",
            "png": f"{bill.bill_id}.png",
            "photo": f"{bill.bill_id}_photo.jpg",
        }
        (out_dir / files["pdf"]).write_bytes(pdf_bytes)
        scan.save(out_dir / files["png"], optimize=True)
        photo = photo_variant(scan, rng)
        photo.save(out_dir / files["photo"], quality=degraded_quality(rng, "mild"))
        if degraded:
            for level in DEGRADATION_LEVELS:
                key = f"photo_{level}"
                files[key] = f"{bill.bill_id}_photo_{level}.jpg"
                photo_variant(scan, rng, level=level).save(
                    out_dir / files[key], quality=degraded_quality(rng, level)
                )

        records.append(
            {
                "id": bill.bill_id,
                "template": bill.template,
                "files": files,
                "truth": bill.truth.model_dump(mode="json"),
                "fault": bill.fault,
                "expected_check": FAULT_TO_CHECK.get(bill.fault) if bill.fault else None,
            }
        )
    with (out_dir / "manifest.jsonl").open("w") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--n", type=int, default=24, help="number of bills")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--faulty",
        type=int,
        default=0,
        help="number of bills with a planted fault, for measuring review-flag catch rate",
    )
    parser.add_argument(
        "--degraded",
        action="store_true",
        help="also write one photo per degradation level (clean/mild/moderate/severe)",
    )
    parser.add_argument("--out", type=Path, default=Path("data/synthetic"))
    args = parser.parse_args()
    records = generate(args.out, args.n, args.seed, faulty_n=args.faulty, degraded=args.degraded)
    faulty = sum(1 for r in records if r["fault"])
    per_bill = 3 + (len(DEGRADATION_LEVELS) if args.degraded else 0)
    print(
        f"Wrote {len(records)} bills ({len(records) * per_bill} files) to {args.out} "
        f"({faulty} with a planted fault)" + (" with degraded photos" if args.degraded else "")
    )


if __name__ == "__main__":
    main()
