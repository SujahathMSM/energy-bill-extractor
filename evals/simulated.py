"""Stand-in backends for running the evaluation harness without an API key.

SIMULATED RESULTS ARE NOT MODEL ACCURACY. These backends exist to prove the harness works end
to end -- scoring, the repair path, the review flag, the report -- and to show what its output
looks like. Real numbers need a real model: set ANTHROPIC_API_KEY and run `make eval`.

- ``OracleBackend`` returns the ground truth. Field accuracy must be 100%; planted faults must
  all be flagged. Any other result is a harness bug.
- ``SimulatedBackend`` returns the ground truth with errors injected at rates that grow with
  photo damage, using the error types seen in real runs (README, "Results"): misread digits in
  long IDs, blank day counts, pence read as pounds, a wrong total, an invented reading, and
  replies that are not valid JSON or do not match the schema. Seeded, so runs are repeatable.
"""

import copy
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from bill_extractor.backends import BackendResponse, MalformedReplyError, VLMBackend
from bill_extractor.preprocess import PageImage, prepare

# Chance that a document gets one injected error, by input variant.
ERROR_RATE = {
    "pdf": 0.08,
    "png": 0.12,
    "photo": 0.25,
    "photo_clean": 0.10,
    "photo_mild": 0.25,
    "photo_moderate": 0.30,
    "photo_severe": 0.70,
}
# Error type -> relative weight. Digit misreads dominate, as in the real degraded-photo run.
ERROR_WEIGHTS = {
    "mpan_digit": 4,
    "serial_digit": 3,
    "drop_period_days": 2,
    "pence_as_pounds": 1,
    "total_digit": 1,
    "invented_reading": 1,
    "malformed_json_once": 2,
    "schema_error_once": 1,
    "unrecoverable_json": 1,
}
# Rough Claude vision token cost of an image: width x height / 750 (Claude docs).
_PROMPT_TOKENS = 1900  # system prompt + instructions + JSON schema


@dataclass
class _Doc:
    doc_id: str
    variant: str
    truth: dict
    error: str | None
    calls: int = 0
    answer: dict = field(default_factory=dict)


def _change_digit(value: str, rng: random.Random) -> str:
    digits = [i for i, ch in enumerate(value) if ch.isdigit()]
    if not digits:
        return value + "1"
    i = rng.choice(digits)
    new = str((int(value[i]) + rng.randint(1, 9)) % 10)
    return value[:i] + new + value[i + 1 :]


def _inject(truth: dict, error: str, rng: random.Random) -> dict:
    answer = copy.deepcopy(truth)
    if error == "mpan_digit" and answer.get("meter_point_id"):
        answer["meter_point_id"] = _change_digit(answer["meter_point_id"], rng)
    elif error == "serial_digit" and answer.get("meter_serial"):
        answer["meter_serial"] = _change_digit(answer["meter_serial"], rng)
    elif error == "drop_period_days":
        answer["period_days"] = None
    elif error == "pence_as_pounds" and answer.get("unit_rates"):
        answer["unit_rates"][0]["rate_unit"] = "£/kWh"
    elif error == "total_digit" and answer.get("total_amount") is not None:
        answer["total_amount"] = round(answer["total_amount"] + 9, 2)
    elif error == "invented_reading":
        if answer.get("previous_reading") is None:
            answer["previous_reading"] = 18342.0
        else:
            answer["reading_type"] = (
                "estimated" if answer.get("reading_type") != "estimated" else "actual"
            )
    return answer


def _tokens(pages: list[PageImage], answer: dict) -> tuple[int, int]:
    image = sum(p.width * p.height // 750 for p in pages)
    return _PROMPT_TOKENS + image, len(json.dumps(answer)) // 3


class OracleBackend(VLMBackend):
    """Returns the ground truth for the exact page it is sent."""

    name = "oracle"
    model = "ground-truth"

    def __init__(self, docs: dict[bytes, _Doc]) -> None:
        self.docs = docs

    async def extract(self, pages, feedback=None):
        doc = self.docs[pages[0].data]
        return BackendResponse(dict(doc.truth), *_tokens(pages, doc.truth))


class SimulatedBackend(OracleBackend):
    """Ground truth with seeded, realistic errors. See the module docstring."""

    name = "simulated"
    model = "simulated-errors"

    async def extract(self, pages, feedback=None):
        doc = self.docs[pages[0].data]
        doc.calls += 1
        error = doc.error
        if error == "unrecoverable_json" or (error == "malformed_json_once" and doc.calls == 1):
            raise MalformedReplyError("The model reply was not valid JSON: Expecting ',' delimiter")
        if error == "schema_error_once" and doc.calls == 1:
            answer = {**doc.truth, "billing_period_start": "the 1st"}
        elif error in ("malformed_json_once", "schema_error_once", None):
            answer = doc.truth
        else:
            answer = doc.answer
        return BackendResponse(dict(answer), *_tokens(pages, answer))


def build(
    kind: str,
    data_dir: Path,
    records: list[dict],
    variants: list[str],
    max_pages: int,
    max_side: int,
    seed: int = 11,
) -> VLMBackend:
    """Index every document's first page so the backend can look up its ground truth."""
    weights = list(ERROR_WEIGHTS.items())
    docs: dict[bytes, _Doc] = {}
    for record in records:
        for variant in variants:
            rng = random.Random(f"{seed}:{record['id']}:{variant}")
            error = None
            if kind == "simulated" and rng.random() < ERROR_RATE.get(variant, 0.2):
                error = rng.choices([w[0] for w in weights], [w[1] for w in weights])[0]
            data = (data_dir / record["files"][variant]).read_bytes()
            page = prepare(data, max_pages, max_side).pages[0]
            doc = _Doc(record["id"], variant, record["truth"], error)
            if error:
                doc.answer = _inject(record["truth"], error, rng)
            docs[page.data] = doc
    return SimulatedBackend(docs) if kind == "simulated" else OracleBackend(docs)
