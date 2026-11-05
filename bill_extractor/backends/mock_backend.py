"""Offline backend: no API key, no GPU.

For the demo page's bundled sample bills it returns that bill's ground truth, so clicking a
sample shows the right supplier and numbers. For any other file it returns one fixed,
internally consistent example bill (Fernwick Energy). This lets the API, demo page and tests
run anywhere; it does not read anything.
"""

import hashlib
import json
from pathlib import Path

from ..preprocess import PageImage, prepare
from ..validation import mpan_check_digit
from .base import BackendResponse, VLMBackend

_MPAN_FIRST_12 = "160012345678"
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "static" / "samples"

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


def _page_key(page: PageImage) -> str:
    return hashlib.sha256(page.data).hexdigest()


class MockBackend(VLMBackend):
    name = "mock"
    model = "mock-demo"

    def __init__(
        self, max_pages: int = 3, max_side: int = 1568, samples_dir: Path = SAMPLES_DIR
    ) -> None:
        self.max_pages = max_pages
        self.max_side = max_side
        self.samples_dir = samples_dir
        self._known: dict[str, dict] | None = None

    def _index(self) -> dict[str, dict]:
        """Map the first rendered page of each bundled sample to its ground truth.

        Rendering is deterministic, so an upload of the same file renders to the same bytes.
        Built lazily on first use, with the same page settings as the extractor.
        """
        if self._known is None:
            self._known = {}
            truth_file = self.samples_dir / "truth.json"
            if truth_file.exists():
                for name, truth in json.loads(truth_file.read_text()).items():
                    path = self.samples_dir / name
                    if path.exists():
                        doc = prepare(path.read_bytes(), self.max_pages, self.max_side)
                        self._known[_page_key(doc.pages[0])] = truth
        return self._known

    async def extract(self, pages: list[PageImage], feedback: str | None = None) -> BackendResponse:
        truth = self._index().get(_page_key(pages[0])) if pages else None
        if truth is not None:
            return BackendResponse(data=dict(truth), input_tokens=0, output_tokens=0)
        return BackendResponse(data=dict(MOCK_BILL), input_tokens=0, output_tokens=0)
