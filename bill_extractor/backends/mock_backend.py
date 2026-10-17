"""Offline backend that returns a fixed, internally consistent bill.

Lets the API, demo page and tests run with no API key and no GPU.
"""

from ..preprocess import PageImage
from ..validation import mpan_check_digit
from .base import BackendResponse, VLMBackend

_MPAN_FIRST_12 = "160012345678"

MOCK_BILL: dict = {
    "supplier_name": "Fernwick Energy",
    "account_number": "40391827",
    "meter_point_id": _MPAN_FIRST_12 + str(mpan_check_digit(_MPAN_FIRST_12)),
    "meter_serial": "19E0482716",
    "tariff_name": "Fernwick Fixed Nov 2027",
    "currency": "GBP",
    "billing_period_start": "2026-08-14",
    "billing_period_end": "2026-09-12",
    "previous_reading": 18342,
    "current_reading": 18629,
    "reading_type": "actual",
    "consumption_kwh": 287,
    "unit_rates": [{"label": "Standard", "rate": 24.86, "rate_unit": "p/kWh", "kwh": 287}],
    "standing_charge": 51.42,
    "standing_charge_unit": "p/day",
    "subtotal_excl_tax": 86.78,
    "tax_amount": 4.34,
    "total_amount": 91.12,
}


class MockBackend(VLMBackend):
    name = "mock"
    model = "mock-fixed-bill"

    async def extract(self, pages: list[PageImage], feedback: str | None = None) -> BackendResponse:
        return BackendResponse(data=dict(MOCK_BILL), input_tokens=0, output_tokens=0)
