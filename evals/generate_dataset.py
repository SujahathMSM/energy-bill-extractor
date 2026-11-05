"""Generate the synthetic evaluation set: realistic UK electricity bills with ground truth.

    python -m evals.generate_dataset            # 24 bills -> evals/data/synthetic

Each bill comes from one of three layouts (single rate with meter readings, Economy 7
day/night, account-summary-first with a previous balance) and one of five fictional
suppliers, with varied date formats, unit spellings and decimal places. Every bill is
written as a digital PDF, a 150 dpi scan (PNG) and phone-style photos (JPEG: tilted,
blurred, unevenly lit) at four damage levels. Six bills carry one planted inconsistency
(a wrong total, a bad MPAN digit, swapped readings, ...) that a check must catch.

The generator itself is `bill_extractor.synth`; this wraps it with the evaluation defaults.
The output is deterministic for a seed and is not committed (see .gitignore).
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bill_extractor.synth import generate  # noqa: E402

DEFAULT_DATA = ROOT / "evals" / "data" / "synthetic"
DEFAULTS = {"n": 24, "faulty_n": 6, "seed": 7, "degraded": True}


def ensure_dataset(data_dir: Path = DEFAULT_DATA) -> Path:
    """Generate the default set into `data_dir` unless a manifest is already there."""
    if not (data_dir / "manifest.jsonl").exists():
        print(f"Generating {DEFAULTS['n']} synthetic bills into {data_dir} ...", file=sys.stderr)
        generate(data_dir, **DEFAULTS)
    return data_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--n", type=int, default=DEFAULTS["n"])
    parser.add_argument("--faulty", type=int, default=DEFAULTS["faulty_n"])
    parser.add_argument("--seed", type=int, default=DEFAULTS["seed"])
    args = parser.parse_args()
    records = generate(args.out, args.n, args.seed, faulty_n=args.faulty, degraded=True)
    print(f"Wrote {len(records)} bills to {args.out}")


if __name__ == "__main__":
    main()
