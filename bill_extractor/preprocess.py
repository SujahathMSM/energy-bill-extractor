"""Turn an uploaded file (PDF or image) into page images a vision model can read."""

import base64
import io
import threading
from dataclasses import dataclass

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError


class UnsupportedFileError(ValueError):
    """The upload is not a PDF, PNG, JPEG or WebP file."""


# PDFium is not thread-safe. Rendering runs in worker threads (so it does not block the event
# loop), so every PDFium call goes through this lock. Concurrent renders crash the process.
_PDFIUM_LOCK = threading.Lock()


@dataclass(frozen=True)
class PageImage:
    data: bytes
    media_type: str
    width: int
    height: int

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.b64}"


@dataclass(frozen=True)
class PreparedDocument:
    pages: list[PageImage]
    pages_in_file: int
    source_type: str


def sniff_type(data: bytes) -> str:
    """Detect the file type from its first bytes. Client-supplied content types are not trusted."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise UnsupportedFileError("Upload a PDF, PNG, JPEG or WebP file.")


def _fit(img: Image.Image, max_side: int) -> Image.Image:
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


def _to_png(img: Image.Image) -> PageImage:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return PageImage(buf.getvalue(), "image/png", img.width, img.height)


def _to_jpeg(img: Image.Image) -> PageImage:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return PageImage(buf.getvalue(), "image/jpeg", img.width, img.height)


def prepare(data: bytes, max_pages: int = 3, max_side: int = 1600) -> PreparedDocument:
    """Render up to `max_pages` pages, each scaled so its longest side is at most `max_side`."""
    kind = sniff_type(data)

    if kind == "pdf":
        rendered: list[Image.Image] = []
        with _PDFIUM_LOCK:
            try:
                pdf = pdfium.PdfDocument(data)
            except pdfium.PdfiumError as exc:
                raise UnsupportedFileError("The PDF could not be opened.") from exc
            try:
                total = len(pdf)
                for index in range(min(total, max_pages)):
                    page = pdf[index]
                    width_pt, height_pt = page.get_size()
                    bitmap = page.render(scale=max_side / max(width_pt, height_pt))
                    rendered.append(bitmap.to_pil().copy())  # own the pixels before closing
                    page.close()
            finally:
                pdf.close()
        # Encoding happens outside the lock. Digital PDFs are crisp text: PNG keeps them sharp.
        return PreparedDocument([_to_png(_fit(img, max_side)) for img in rendered], total, "pdf")

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise UnsupportedFileError("The image could not be read.") from exc
    # Phone photos often store rotation in EXIF rather than in the pixels.
    img = _fit(ImageOps.exif_transpose(img), max_side)
    # Photos compress far better as JPEG; scans and screenshots stay PNG.
    page = _to_jpeg(img) if kind == "jpeg" else _to_png(img)
    return PreparedDocument([page], 1, kind)
