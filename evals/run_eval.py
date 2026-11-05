"""Run the extractor over the synthetic bill set and report how well it did.

    make eval                                  # Claude with ANTHROPIC_API_KEY, else simulated
    python -m evals.run_eval --backend anthropic --model claude-haiku-5-5
    python -m evals.run_eval --backend simulated  # no key: proves the harness end to end
    python -m evals.run_eval --backend oracle     # ground truth: must score 100%

Reports: per-field accuracy (exact match for IDs and dates, a penny or 0.5% for numbers),
overall field accuracy, schema-valid rate, consistency-check pass rates, review-flag catch
rate on planted faults, latency p50/p95 and estimated cost per bill.

Writes evals/results/latest.json and evals/results/latest.md, and refreshes the "Latest run"
section of EVALS.md. A simulated or oracle run is labelled as such everywhere it appears.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python evals/run_eval.py` as well as `-m`
    sys.path.insert(0, str(ROOT))

from bill_extractor.backends import build_backend  # noqa: E402
from bill_extractor.config import Settings  # noqa: E402
from bill_extractor.evaluate import add_cost, evaluate, load_manifest  # noqa: E402
from bill_extractor.extractor import BillExtractor  # noqa: E402
from bill_extractor.scoring import render_markdown, summarise  # noqa: E402
from evals import simulated  # noqa: E402
from evals.generate_dataset import DEFAULT_DATA, ensure_dataset  # noqa: E402

RESULTS = ROOT / "evals" / "results"
EVALS_MD = ROOT / "EVALS.md"
START, END = "<!-- latest-run:start -->", "<!-- latest-run:end -->"
DEFAULT_VARIANTS = "pdf,png,photo,photo_severe"
REAL_BACKENDS = ("anthropic", "openai", "ollama")


def pick_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "simulated"


def _label(kind: str, model: str) -> str:
    if kind == "simulated":
        return "SIMULATED (no API key: injected errors, not model accuracy)"
    if kind == "oracle":
        return "ORACLE (ground truth: harness self-test, not model accuracy)"
    return f"{kind} / {model}"


async def run(args: argparse.Namespace) -> dict:
    kind = pick_backend(args.backend)
    data_dir = ensure_dataset(args.data)
    records = load_manifest(data_dir)[: args.limit]
    variants = [v for v in args.variants.split(",") if v]
    missing = sorted({v for r in records for v in variants if v not in r["files"]})
    if missing:
        raise SystemExit(f"Variants {missing} are not in {data_dir}; regenerate with --degraded.")

    overrides = {"BILLX_BACKEND": kind} if kind in REAL_BACKENDS else {}
    if args.model and kind == "anthropic":
        overrides["BILLX_ANTHROPIC_MODEL"] = args.model
    elif args.model and kind == "openai":
        overrides["BILLX_OPENAI_MODEL"] = args.model
    elif args.model and kind == "ollama":
        overrides["BILLX_OLLAMA_MODEL"] = args.model
    settings = Settings(**overrides)
    if kind in REAL_BACKENDS:
        backend = build_backend(settings)
    else:
        backend = simulated.build(
            kind, data_dir, records, variants, settings.max_pages, settings.max_image_side
        )
    extractor = BillExtractor(
        backend,
        max_attempts=settings.max_attempts,
        max_pages=settings.max_pages,
        max_side=settings.max_image_side,
        max_megapixels=settings.max_image_megapixels,
        blur_threshold=settings.blur_threshold,
    )

    started = time.perf_counter()
    try:
        results = await evaluate(extractor, data_dir, variants, args.limit, args.concurrency)
    finally:
        await backend.aclose()
    elapsed = time.perf_counter() - started

    summary = summarise(results)
    # Simulated token counts are priced at the default model's rates, to show the arithmetic.
    priced_model = settings.anthropic_model if kind not in REAL_BACKENDS else backend.model
    label = None if kind in REAL_BACKENDS else f"simulated tokens priced as {priced_model}"
    add_cost(summary, priced_model, label)
    return {
        "backend": kind,
        "model": backend.model,
        "label": _label(kind, backend.model),
        "simulated": kind not in REAL_BACKENDS,
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset": {
            "path": str(data_dir.relative_to(ROOT) if data_dir.is_relative_to(ROOT) else data_dir),
            "bills": len(records),
            "faulty_bills": sum(1 for r in records if r.get("fault")),
            "variants": variants,
        },
        "elapsed_s": round(elapsed, 1),
        "summary": summary,
        "documents": [r.__dict__ for r in results],
    }


def failure_patterns(report: dict, top: int = 8) -> list[str]:
    """The most common wrong/missed/invented fields and the checks that failed most."""
    counts: dict[tuple[str, str], int] = {}
    for doc in report["documents"]:
        for name, outcome in doc["outcomes"].items():
            if outcome != "correct":
                counts[(name, outcome)] = counts.get((name, outcome), 0) + 1
    rows = sorted(counts.items(), key=lambda kv: -kv[1])[:top]
    lines = [f"- `{name}` {outcome}: {n} document(s)" for (name, outcome), n in rows]
    errors = report["summary"]["error_messages"]
    lines += [f"- Extraction failed ({n}x): {' '.join(msg.split())[:160]}" for msg, n in errors]
    return lines or ["- No field errors."]


def render(report: dict) -> str:
    summary = report["summary"]
    ds = report["dataset"]
    head = [
        f"**{report['label']}** - {report['created_utc']}",
        "",
        f"{ds['bills']} synthetic bills ({ds['faulty_bills']} with a planted fault) x "
        f"{len(ds['variants'])} inputs ({', '.join(ds['variants'])}) = {summary['docs']} "
        f"documents. Run time {report['elapsed_s']} s.",
        "",
    ]
    if report["simulated"]:
        head += [
            "> These numbers come from a stand-in backend, not a model. They show the harness "
            "works; they say nothing about extraction accuracy. Set `ANTHROPIC_API_KEY` and run "
            "`make eval` for real numbers.",
            "",
        ]
    body = render_markdown(summary, "Latest run").split("\n", 2)[2]  # drop the H1
    return "\n".join(
        [*head, body.strip(), "", "Most common failures:", "", *failure_patterns(report), ""]
    )


def write(report: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "latest.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    markdown = render(report)
    (RESULTS / "latest.md").write_text(markdown)
    if EVALS_MD.exists():
        text = EVALS_MD.read_text()
        if START in text and END in text:
            section = f"{START}\n{markdown}\n{END}"
            text = re.sub(
                re.escape(START) + ".*?" + re.escape(END), lambda _: section, text, flags=re.S
            )
            EVALS_MD.write_text(text)


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "simulated", "oracle", *REAL_BACKENDS],
        help="auto: anthropic when ANTHROPIC_API_KEY is set, otherwise simulated",
    )
    parser.add_argument("--model", help="model name for the chosen backend")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--variants", default=DEFAULT_VARIANTS, help="comma-separated")
    parser.add_argument("--limit", type=int, help="only the first N bills")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--no-write", action="store_true", help="print only; keep latest.*")
    args = parser.parse_args(argv)

    report = asyncio.run(run(args))
    if not args.no_write:
        write(report)
    print(render(report))
    if not args.no_write:
        print(f"Wrote {RESULTS / 'latest.json'} and {RESULTS / 'latest.md'}; updated EVALS.md.")
    return report


if __name__ == "__main__":
    main()
