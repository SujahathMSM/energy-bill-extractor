"""HTTP API."""

import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .backends import BackendError, BackendRateLimitError, BackendTimeoutError, build_backend
from .config import get_settings
from .extractor import BillExtractor, ExtractionError
from .preprocess import HEIC_SUPPORTED, ImageTooLargeError, UnsupportedFileError
from .savings import SavingsEstimate, SavingsRequest, estimate_savings
from .schema import ExtractionResult

log = logging.getLogger("bill_extractor")
STATIC_DIR = Path(__file__).parent / "static"
# Client-supplied request IDs are echoed and logged, so only plain tokens are accepted.
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Multipart framing around the file; a request bigger than the limit plus this is rejected
# before the body is parsed.
_MULTIPART_OVERHEAD = 64 * 1024


def _configure_logging() -> None:
    """Uvicorn configures only its own loggers; give ours a handler too."""
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    settings = get_settings()
    backend = build_backend(settings)
    app.state.settings = settings
    app.state.extractor = BillExtractor(
        backend,
        max_attempts=settings.max_attempts,
        max_pages=settings.max_pages,
        max_side=settings.max_image_side,
        max_megapixels=settings.max_image_megapixels,
        blur_threshold=settings.blur_threshold,
    )
    log.info("Using backend=%s model=%s", backend.name, backend.model)
    yield
    await backend.aclose()


app = FastAPI(
    title="Energy Bill Extractor",
    version=__version__,
    description=(
        "Upload an electricity bill (PDF or photo) and get tariff and usage fields back, "
        "with plausibility checks that say when a person should review the result."
    ),
    lifespan=lifespan,
)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# CORS is read at import time (middleware cannot be added once the app has started).
# Default: no origins, i.e. same-origin only; the bundled UI is served by this app.
_cors = get_settings().cors_origin_list
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    return supplied if _REQUEST_ID.match(supplied) else uuid.uuid4().hex[:12]


@app.middleware("http")
async def reject_oversized_uploads(request: Request, call_next):
    """Refuse an upload by its Content-Length before the multipart body is read."""
    if request.method == "POST" and request.url.path == "/v1/bills/extract":
        limit_mb = get_settings().max_upload_mb
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length > limit_mb * 1024 * 1024 + _MULTIPART_OVERHEAD:
            return JSONResponse(
                {"detail": f"File is larger than {limit_mb:g} MB."}, status_code=413
            )
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def readable_validation_error(request: Request, exc: RequestValidationError):
    """One readable sentence in `detail` (what the UI shows), plus the structured errors."""
    errors = exc.errors()
    if request.url.path == "/v1/bills/extract" and any(
        e.get("loc", ())[-1:] == ("file",) for e in errors
    ):
        detail = 'Attach the bill as a multipart form field named "file".'
    else:
        parts = []
        for error in errors[:5]:
            where = ".".join(str(p) for p in error.get("loc", ()) if p != "body")
            parts.append(f"{where or 'body'}: {error.get('msg')}")
        detail = "Invalid request. " + "; ".join(parts)
    safe = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in errors]
    return JSONResponse({"detail": detail, "errors": safe}, status_code=422)


@app.get("/health")
async def health(request: Request) -> dict:
    extractor: BillExtractor = request.app.state.extractor
    settings = request.app.state.settings
    return {
        "status": "ok",
        "version": __version__,
        "backend": extractor.backend.name,
        "model": extractor.backend.model,
        "limits": {"max_upload_mb": settings.max_upload_mb, "max_pages": settings.max_pages},
        "formats": ["pdf", "png", "jpeg", "webp", *(["heic"] if HEIC_SUPPORTED else [])],
    }


@app.post(
    "/v1/bills/extract",
    response_model=ExtractionResult,
    responses={
        400: {"description": "Empty file"},
        413: {"description": "File too large, or too many pixels"},
        415: {"description": "Not a readable PDF, PNG, JPEG, WebP or HEIC file"},
        422: {"description": "Model output never matched the schema, or no file field"},
        502: {"description": "Model provider error"},
        503: {"description": "Model provider busy or rate-limited; retry later"},
        504: {"description": "Model provider timed out"},
    },
)
async def extract_bill(
    request: Request,
    response: Response,
    file: UploadFile = File(..., description="Bill as PDF, PNG, JPEG, WebP or HEIC"),
) -> ExtractionResult:
    settings = request.app.state.settings
    extractor: BillExtractor = request.app.state.extractor
    request_id = _request_id(request)
    response.headers["X-Request-ID"] = request_id
    headers = {"X-Request-ID": request_id}

    limit = int(settings.max_upload_mb * 1024 * 1024)
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            413, f"File is larger than {settings.max_upload_mb:g} MB.", headers=headers
        )
    if not data:
        raise HTTPException(400, "The uploaded file is empty.", headers=headers)

    # Error logs carry the error class and our own message, never the bill's contents.
    try:
        result = await extractor.extract(data, request_id=request_id)
    except UnsupportedFileError as exc:
        log.info("request_id=%s rejected: %s", request_id, exc)
        raise HTTPException(415, str(exc), headers=headers) from exc
    except ImageTooLargeError as exc:
        log.info("request_id=%s rejected: %s", request_id, exc)
        raise HTTPException(413, str(exc), headers=headers) from exc
    except ExtractionError as exc:
        log.warning("request_id=%s extraction failed: %s", request_id, exc)
        raise HTTPException(
            422,
            "The model's answer did not match the bill schema, even after a repair attempt. "
            "Try a clearer photo or the PDF.",
            headers=headers,
        ) from exc
    except BackendTimeoutError as exc:
        log.error("request_id=%s provider timeout: %s", request_id, exc)
        raise HTTPException(
            504, "The model took too long to answer. Try again.", headers=headers
        ) from exc
    except BackendRateLimitError as exc:
        log.error("request_id=%s provider busy: %s", request_id, exc)
        raise HTTPException(
            503,
            "The model provider is busy. Try again in a minute.",
            headers={**headers, "Retry-After": "30"},
        ) from exc
    except BackendError as exc:
        log.error("request_id=%s backend error: %s", request_id, type(exc).__name__)
        log.debug("request_id=%s backend error detail: %s", request_id, exc)
        raise HTTPException(502, "The model provider returned an error.", headers=headers) from exc

    # One structured line per request. No bill contents are logged: bills are personal data.
    log.info(
        json.dumps(
            {
                "event": "bill_extracted",
                "request_id": request_id,
                "backend": result.meta.backend,
                "model": result.meta.model,
                "latency_ms": result.meta.latency_ms,
                "attempts": result.meta.attempts,
                "needs_review": result.needs_review,
                "failed_checks": [c.name for c in result.checks if c.status == "fail"],
            }
        )
    )
    return result


@app.post(
    "/v1/savings",
    response_model=SavingsEstimate,
    responses={
        422: {"description": "Saving cannot be estimated from this bill/target pair"},
    },
)
async def savings(request_body: SavingsRequest) -> SavingsEstimate:
    """Compare an extracted bill with a target tariff and estimate the annual saving.

    Takes the extraction (and optionally its derived figures) from
    POST /v1/bills/extract plus a target tariff. Costs exclude tax; see
    bill_extractor.savings for the assumptions.
    """
    try:
        return estimate_savings(
            request_body.extraction,
            request_body.target,
            derived=request_body.derived,
            needs_review=request_body.needs_review,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
