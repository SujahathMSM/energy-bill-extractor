"""HTTP API."""

import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .backends import BackendError, build_backend
from .config import get_settings
from .extractor import BillExtractor, ExtractionError
from .preprocess import UnsupportedFileError
from .schema import ExtractionResult

log = logging.getLogger("bill_extractor")
STATIC_DIR = Path(__file__).parent / "static"


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
    }


@app.post(
    "/v1/bills/extract",
    response_model=ExtractionResult,
    responses={
        413: {"description": "File too large"},
        415: {"description": "Not a PDF, PNG, JPEG or WebP file"},
        422: {"description": "Model output never matched the schema"},
        502: {"description": "Model provider error"},
    },
)
async def extract_bill(
    request: Request,
    response: Response,
    file: UploadFile = File(..., description="Bill as PDF, PNG, JPEG or WebP"),
) -> ExtractionResult:
    settings = request.app.state.settings
    extractor: BillExtractor = request.app.state.extractor
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    response.headers["X-Request-ID"] = request_id

    limit = int(settings.max_upload_mb * 1024 * 1024)
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb:g} MB.")
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")

    try:
        result = await extractor.extract(data, request_id=request_id)
    except UnsupportedFileError as exc:
        raise HTTPException(415, str(exc)) from exc
    except ExtractionError as exc:
        log.warning("request_id=%s extraction failed: %s", request_id, exc)
        raise HTTPException(422, "The model's answer did not match the bill schema.") from exc
    except BackendError as exc:
        log.error("request_id=%s backend error: %s", request_id, exc)
        raise HTTPException(502, "The model provider returned an error.") from exc

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


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
