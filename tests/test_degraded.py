import random

import pytest
from PIL import Image

from bill_extractor.synth import (
    DEGRADATION_LEVELS,
    degraded_quality,
    generate,
    make_bill,
    photo_variant,
    rasterise,
    render_pdf,
)


def _scan():
    bill = make_bill(0, random.Random(1))
    return rasterise(render_pdf(bill))


def _sharpness(img: Image.Image) -> float:
    """Mean absolute Laplacian response: higher means sharper."""
    gray = img.convert("L")
    px = list(gray.tobytes())
    w, h = gray.size
    total = 0.0
    count = 0
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            centre = px[y * w + x]
            lap = abs(
                4 * centre
                - px[(y - 1) * w + x]
                - px[(y + 1) * w + x]
                - px[y * w + x - 1]
                - px[y * w + x + 1]
            )
            total += lap
            count += 1
    return total / count


def test_unknown_level_is_rejected():
    scan = _scan()
    with pytest.raises(ValueError, match="Unknown degradation level"):
        photo_variant(scan, random.Random(1), level="melted")
    with pytest.raises(ValueError, match="Unknown degradation level"):
        degraded_quality(random.Random(1), level="melted")


def test_levels_are_ordered_by_damage():
    scan = _scan()
    # Same RNG seed per level, so the comparison measures the level, not the draw.
    sharp = {
        level: _sharpness(photo_variant(scan, random.Random(42), level=level))
        for level in DEGRADATION_LEVELS
    }
    assert sharp["clean"] > sharp["mild"] > sharp["moderate"] > sharp["severe"]


def test_severe_is_smaller_from_crop_and_compression():
    scan = _scan()
    clean = photo_variant(scan, random.Random(1), level="clean")
    severe = photo_variant(scan, random.Random(1), level="severe")
    assert severe.size[0] <= clean.size[0] or severe.size[1] <= clean.size[1]


def test_quality_ranges_are_ordered():
    for level in DEGRADATION_LEVELS:
        q = degraded_quality(random.Random(1), level)
        assert 1 <= q <= 100
    assert degraded_quality(random.Random(1), "clean") > degraded_quality(
        random.Random(1), "severe"
    )


def test_generate_without_degraded_is_unchanged(tmp_path):
    records = generate(tmp_path, n=2, seed=7)
    assert len(records) == 2
    assert set(records[0]["files"]) == {"pdf", "png", "photo"}
    assert len(list(tmp_path.iterdir())) == 2 * 3 + 1  # 3 files per bill + manifest


def test_generate_with_degraded_writes_all_levels(tmp_path):
    records = generate(tmp_path, n=2, seed=7, degraded=True)
    assert len(records) == 2
    for record in records:
        for level in DEGRADATION_LEVELS:
            key = f"photo_{level}"
            assert key in record["files"]
            path = tmp_path / record["files"][key]
            assert path.exists()
            with Image.open(path) as img:
                img.verify()
    assert len(list(tmp_path.iterdir())) == 2 * (3 + len(DEGRADATION_LEVELS)) + 1
