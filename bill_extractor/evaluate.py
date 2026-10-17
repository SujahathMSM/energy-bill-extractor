"""Run the extractor over a labelled bill set and report accuracy, review-flag quality and latency.

    python -m bill_extractor.synth --out data/synthetic
    BILLX_BACKEND=anthropic python -m bill_extractor.evaluate --data data/synthetic

Reports go to reports/ as JSON (every document's outcomes) and Markdown (the summary).
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .backends import BackendError, VLMBackend, build_backend
from .config import Settings
from .extractor import BillExtractor, ExtractionError
from .preprocess import UnsupportedFileError
from .schema import BillExtraction
from .scoring import DocResult, render_markdown, score_fields, summarise


def load_manifest(data_dir: Path) -> list[dict]:
    path = data_dir / "manifest.jsonl"
    if not path.exists():
        raise SystemExit(
            f"No manifest at {path}. Run: python -m bill_extractor.synth --out {data_dir}"
        )
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def evaluate(
    extractor: BillExtractor,
    data_dir: Path,
    variants: list[str],
    limit: int | None = None,
    concurrency: int = 2,
) -> list[DocResult]:
    records = load_manifest(data_dir)[:limit]
    semaphore = asyncio.Semaphore(concurrency)
    seen_errors: set[str] = set()

    async def run_one(record: dict, variant: str) -> DocResult:
        result = DocResult(
            record["id"],
            variant,
            record["template"],
            fault=record.get("fault"),
            expected_check=record.get("expected_check"),
        )
        truth = BillExtraction.model_validate(record["truth"])
        data = (data_dir / record["files"][variant]).read_bytes()
        async with semaphore:
            try:
                extracted = await extractor.extract(data, request_id=f"{record['id']}:{variant}")
            except (ExtractionError, BackendError, UnsupportedFileError) as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                if result.error not in seen_errors:
                    seen_errors.add(result.error)
                    print(f"[{record['id']}:{variant}] {result.error}", file=sys.stderr)
                return result
        result.outcomes = score_fields(truth, extracted.extraction)
        result.needs_review = extracted.needs_review
        result.failed_checks = [c.name for c in extracted.checks if c.status == "fail"]
        if result.expected_check is not None:
            result.expected_check_failed = any(
                c.name == result.expected_check and c.status == "fail" for c in extracted.checks
            )
        result.latency_ms = extracted.meta.latency_ms
        result.attempts = extracted.meta.attempts
        result.input_tokens = extracted.meta.input_tokens
        result.output_tokens = extracted.meta.output_tokens
        return result

    tasks = [run_one(record, variant) for record in records for variant in variants]
    return list(await asyncio.gather(*tasks))


def write_reports(results: list[DocResult], backend: VLMBackend, out_dir: Path) -> tuple[Path, str]:
    summary = summarise(results)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_model = backend.model.replace("/", "_").replace(":", "_")
    base = out_dir / f"eval-{backend.name}-{safe_model}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    title = f"Evaluation: {backend.name} / {backend.model}"
    markdown = render_markdown(summary, title)
    base.with_suffix(".md").write_text(markdown)
    base.with_suffix(".json").write_text(
        json.dumps(
            {
                "backend": backend.name,
                "model": backend.model,
                "created_utc": stamp,
                "summary": summary,
                "documents": [r.__dict__ for r in results],
            },
            indent=2,
            default=str,
        )
    )
    return base, markdown


async def _main(args: argparse.Namespace) -> None:
    overrides = {"BILLX_BACKEND": args.backend} if args.backend else {}
    settings = Settings(**overrides)
    backend = build_backend(settings)
    extractor = BillExtractor(
        backend,
        max_attempts=settings.max_attempts,
        max_pages=settings.max_pages,
        max_side=settings.max_image_side,
    )
    started = time.perf_counter()
    try:
        results = await evaluate(
            extractor, args.data, args.variants.split(","), args.limit, args.concurrency
        )
    finally:
        await backend.aclose()
    base, markdown = write_reports(results, backend, args.out)
    print(markdown)
    print(f"Finished in {time.perf_counter() - started:.1f}s. Reports: {base}.md / .json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data", type=Path, default=Path("data/synthetic"))
    parser.add_argument("--out", type=Path, default=Path("reports"))
    parser.add_argument("--backend", choices=["mock", "anthropic", "openai", "ollama"])
    parser.add_argument("--variants", default="pdf,png,photo", help="comma-separated")
    parser.add_argument("--limit", type=int, help="only the first N bills")
    parser.add_argument("--concurrency", type=int, default=2)
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
