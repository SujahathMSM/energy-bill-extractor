import random
from pathlib import Path

import pytest

from bill_extractor.backends import BackendResponse, VLMBackend
from bill_extractor.synth import generate, make_bill, render_pdf


class ScriptedBackend(VLMBackend):
    """Returns queued replies in order and records the feedback it was given."""

    name = "scripted"
    model = "scripted-test"

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.feedback: list[str | None] = []

    async def extract(self, pages, feedback=None):
        self.feedback.append(feedback)
        return BackendResponse(data=self.replies.pop(0), input_tokens=100, output_tokens=50)


@pytest.fixture
def bill():
    """One synthetic single-rate bill and its PDF."""
    synthetic = make_bill(0, random.Random(1))
    return synthetic, render_pdf(synthetic)


@pytest.fixture(scope="session")
def dataset(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("synthetic")
    generate(out, n=6, seed=3)
    return out
