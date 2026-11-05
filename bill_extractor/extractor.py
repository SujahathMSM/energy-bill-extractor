"""Pipeline: file -> page images -> model -> validated fields -> derived figures and checks."""

import asyncio
import re
import time
import uuid

from pydantic import ValidationError

from .backends import MalformedReplyError, VLMBackend
from .preprocess import MIN_LONG_SIDE, PreparedDocument, prepare
from .schema import BillExtraction, Check, ExtractionMeta, ExtractionResult
from .validation import analyse


class ExtractionError(RuntimeError):
    """The model never produced output that matched the schema."""


_NUMERIC_FIELDS = (
    "previous_reading",
    "current_reading",
    "consumption_kwh",
    "standing_charge",
    "subtotal_excl_tax",
    "tax_amount",
    "total_amount",
)
_NUMBER_NOISE = re.compile(r"[£$€¢,\s]|kwh|p$", re.IGNORECASE)


def _clean_number(value):
    """Turn "1,234.5", "£86.78" or "287 kWh" into a float; leave anything else unchanged."""
    if not isinstance(value, str):
        return value
    stripped = _NUMBER_NOISE.sub("", value.strip())
    try:
        return float(stripped)
    except ValueError:
        return value


def clean_raw(raw: dict) -> dict:
    """Light, predictable clean-up of common formatting slips before validation.

    Anything this cannot fix is left for schema validation to reject, which triggers a retry
    with the validation errors fed back to the model.
    """
    data = dict(raw)
    for key in _NUMERIC_FIELDS:
        if key in data:
            data[key] = _clean_number(data[key])
    rates = data.get("unit_rates")
    if isinstance(rates, list):
        data["unit_rates"] = [
            {**r, "rate": _clean_number(r.get("rate")), "kwh": _clean_number(r.get("kwh"))}
            if isinstance(r, dict)
            else r
            for r in rates
        ]
    for key, value in data.items():
        if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "n/a"}:
            data[key] = None
    return data


def summarise_errors(exc: ValidationError, limit: int = 8) -> str:
    lines = []
    for error in exc.errors()[:limit]:
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"- {location}: {error['msg']}")
    return "\n".join(lines)


def input_quality_check(document: PreparedDocument, blur_threshold: float) -> Check:
    """Flag inputs too small or too blurry to trust, so a person checks the result.

    Model confidence is not calibrated, but input quality is measurable: on the synthetic set,
    accuracy holds up to "moderate" photo damage and drops sharply at "severe" (see EVALS.md).
    """
    if document.source_size is None:
        return Check(
            name="input_quality", status="skipped", detail="Digital PDF: rendered to size."
        )
    problems = []
    width, height = document.source_size
    if max(width, height) < MIN_LONG_SIDE:
        problems.append(f"low resolution ({width}x{height} px)")
    if (
        blur_threshold > 0
        and document.sharpness is not None
        and document.sharpness < blur_threshold
    ):
        problems.append(f"blurry (sharpness {document.sharpness:g}, threshold {blur_threshold:g})")
    if problems:
        return Check(
            name="input_quality",
            status="fail",
            detail="Photo is " + " and ".join(problems) + "; values may be misread.",
        )
    detail = f"{width}x{height} px"
    if document.sharpness is not None:
        detail += f", sharpness {document.sharpness:g}"
    return Check(name="input_quality", status="pass", detail=detail + ".")


class BillExtractor:
    def __init__(
        self,
        backend: VLMBackend,
        max_attempts: int = 2,
        max_pages: int = 3,
        max_side: int = 1568,
        max_megapixels: float = 40.0,
        blur_threshold: float = 2.0,
    ) -> None:
        self.backend = backend
        self.max_attempts = max_attempts
        self.max_pages = max_pages
        self.max_side = max_side
        self.max_megapixels = max_megapixels
        self.blur_threshold = blur_threshold

    async def extract(self, data: bytes, request_id: str | None = None) -> ExtractionResult:
        started = time.perf_counter()
        # Rendering is CPU work; keep it off the event loop.
        document = await asyncio.to_thread(
            prepare, data, self.max_pages, self.max_side, self.max_megapixels
        )

        feedback: str | None = None
        input_tokens = output_tokens = 0
        extraction: BillExtraction | None = None
        attempt = 0
        for attempt in range(1, self.max_attempts + 1):  # noqa: B007 - attempt used after loop
            try:
                response = await self.backend.extract(document.pages, feedback)
            except MalformedReplyError as exc:
                # Repair attempt: say what was wrong with the reply and ask again.
                feedback = f"- (reply): {exc} Reply with a single JSON object and nothing else."
                continue
            input_tokens += response.input_tokens or 0
            output_tokens += response.output_tokens or 0
            try:
                extraction = BillExtraction.model_validate(clean_raw(response.data))
                break
            except ValidationError as exc:
                feedback = summarise_errors(exc)

        if extraction is None:
            raise ExtractionError(
                f"Model output failed validation after {self.max_attempts} attempts:\n{feedback}"
            )

        derived, checks = analyse(extraction)
        checks.append(input_quality_check(document, self.blur_threshold))
        failed = [c for c in checks if c.status == "fail"]
        flagged = list(dict.fromkeys(f for c in failed for f in c.fields))
        return ExtractionResult(
            extraction=extraction,
            derived=derived,
            checks=checks,
            needs_review=bool(failed),
            review_reasons=[f"{c.name}: {c.detail}" for c in failed],
            flagged_fields=flagged,
            warnings=document.warnings,
            meta=ExtractionMeta(
                request_id=request_id or uuid.uuid4().hex[:12],
                backend=self.backend.name,
                model=self.backend.model,
                pages_sent=len(document.pages),
                pages_in_file=document.pages_in_file,
                attempts=attempt,
                latency_ms=round((time.perf_counter() - started) * 1000),
                input_tokens=input_tokens or None,
                output_tokens=output_tokens or None,
            ),
        )
