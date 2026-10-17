import io

import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from bill_extractor.preprocess import UnsupportedFileError, prepare, sniff_type


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def _pdf(pages: int) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 720, f"page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_sniff_type_ignores_file_names():
    assert sniff_type(_pdf(1)) == "pdf"
    assert sniff_type(_png(10, 10)) == "png"
    with pytest.raises(UnsupportedFileError):
        sniff_type(b"hello, I am a text file")


def test_pdf_pages_are_capped_and_scaled():
    doc = prepare(_pdf(5), max_pages=3, max_side=1000)
    assert doc.pages_in_file == 5
    assert len(doc.pages) == 3
    assert all(max(p.width, p.height) <= 1000 for p in doc.pages)
    assert doc.pages[0].media_type == "image/png"


def test_large_image_is_downscaled():
    doc = prepare(_png(4000, 3000), max_side=1568)
    page = doc.pages[0]
    assert max(page.width, page.height) == 1568
    assert page.data_url.startswith("data:image/png;base64,")


def test_jpeg_stays_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buf, format="JPEG")
    doc = prepare(buf.getvalue())
    assert doc.pages[0].media_type == "image/jpeg"


def test_corrupt_pdf_is_rejected():
    with pytest.raises(UnsupportedFileError):
        prepare(b"%PDF-1.7 this is not really a pdf")
