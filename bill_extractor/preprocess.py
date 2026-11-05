"""Turn an uploaded file (PDF or image) into page images a vision model can read."""

import base64
import io
import threading
from dataclasses import dataclass, field

import pypdfium2 as pdfium
from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

try:  # Optional: iPhone photos are HEIC by default. pip install "energy-bill-extractor[heic]"
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:  # pragma: no cover - depends on the optional extra
    HEIC_SUPPORTED = False


class UnsupportedFileError(ValueError):
    """The upload is not a readable PDF, PNG, JPEG, WebP or HEIC file."""


class ImageTooLargeError(ValueError):
    """The image would decode to more pixels than allowed (a decompression bomb guard)."""


# PDFium is not thread-safe. Rendering runs in worker threads (so it does not block the event
# loop), so every PDFium call goes through this lock. Concurrent renders crash the process.
_PDFIUM_LOCK = threading.Lock()

# FPDF_ERR_PASSWORD in PDFium's error codes.
_PDFIUM_PASSWORD_ERROR = 4
# HEIF brands seen on phone photos (ISO/IEC 23008-12).
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
# Below this many pixels on the long side, small print on an A4 bill is hard to read.
MIN_LONG_SIDE = 1000
# Images are downscaled to this before scoring sharpness, so the score does not depend on
# the camera's resolution.
_SHARPNESS_SIDE = 1200


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
    # Original pixel size of an uploaded image (None for PDFs, which are rendered to size).
    source_size: tuple[int, int] | None = None
    # Edge-sharpness score of an uploaded photo (None for PDFs, PNG scans and screenshots).
    sharpness: float | None = None
    warnings: list[str] = field(default_factory=list)


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
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "heic"
    raise UnsupportedFileError("Upload a PDF, PNG, JPEG, WebP or HEIC file.")


def sharpness(img: Image.Image) -> float:
    """Mean edge response of the image, downscaled to a fixed size. Higher means sharper.

    A heuristic: it also depends on how much text the page has. On the synthetic bills it
    separates the "severe" photo damage level (below 2.0) from "moderate" (above 2.0).
    """
    gray = img.convert("L")
    gray.thumbnail((_SHARPNESS_SIDE, _SHARPNESS_SIDE), Image.Resampling.BILINEAR)
    return round(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0], 3)


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


def _prepare_pdf(data: bytes, max_pages: int, max_side: int) -> PreparedDocument:
    rendered: list[Image.Image] = []
    with _PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            if getattr(exc, "err_code", None) == _PDFIUM_PASSWORD_ERROR:
                raise UnsupportedFileError(
                    "The PDF is password-protected. Remove the password and upload it again."
                ) from exc
            raise UnsupportedFileError(
                "The PDF could not be opened. It may be damaged or incomplete."
            ) from exc
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
    if total == 0:
        raise UnsupportedFileError("The PDF has no pages.")
    warnings = []
    if total > max_pages:
        warnings.append(
            f"Only the first {max_pages} of {total} pages were read; "
            "fields printed only on later pages will be missing."
        )
    # Encoding happens outside the lock. Digital PDFs are crisp text: PNG keeps them sharp.
    pages = [_to_png(_fit(img, max_side)) for img in rendered]
    return PreparedDocument(pages, total, "pdf", warnings=warnings)


def prepare(
    data: bytes,
    max_pages: int = 3,
    max_side: int = 1600,
    max_megapixels: float = 40.0,
) -> PreparedDocument:
    """Render up to `max_pages` pages, each scaled so its longest side is at most `max_side`."""
    kind = sniff_type(data)
    if kind == "pdf":
        return _prepare_pdf(data, max_pages, max_side)
    if kind == "heic" and not HEIC_SUPPORTED:
        raise UnsupportedFileError(
            "HEIC photos need the optional pillow-heif package on the server. "
            "Export the photo as JPEG, or install energy-bill-extractor[heic]."
        )

    try:
        img = Image.open(io.BytesIO(data))
        # Image.open reads only the header, so the size is known before decoding.
        if img.width * img.height > max_megapixels * 1_000_000:
            raise ImageTooLargeError(
                f"The image is {img.width}x{img.height} pixels; the limit is "
                f"{max_megapixels:g} megapixels. Resize it and upload again."
            )
        img.load()
    except Image.DecompressionBombError as exc:
        raise ImageTooLargeError("The image has too many pixels to process safely.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        if isinstance(exc, ImageTooLargeError):
            raise
        raise UnsupportedFileError(
            "The image could not be read. It may be damaged or incomplete."
        ) from exc

    # Phone photos often store rotation in EXIF rather than in the pixels.
    img = ImageOps.exif_transpose(img)
    source_size = img.size
    warnings = []
    if max(source_size) < MIN_LONG_SIDE:
        warnings.append(
            f"The image is only {source_size[0]}x{source_size[1]} pixels; "
            "small print may be misread. A larger photo or the PDF works better."
        )
    # Lossy photo formats get a sharpness score; PNG scans and screenshots are taken as sharp.
    score = sharpness(img) if kind in ("jpeg", "webp", "heic") else None
    img = _fit(img, max_side)
    # Photos compress far better as JPEG (and the model cannot read HEIC); scans, screenshots
    # and WebP stay PNG.
    page = _to_jpeg(img) if kind in ("jpeg", "heic") else _to_png(img)
    return PreparedDocument([page], 1, kind, source_size, score, warnings)
