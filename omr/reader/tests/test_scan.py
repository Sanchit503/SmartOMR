from __future__ import annotations

import numpy as np
import pytest

from omr.generator.config import ExamConfig
from omr.generator.layout import build_layout
from omr.generator.manifest import build_manifest
from omr.reader.scan import ScanError, align_scan_page


def _manifest() -> dict:
    config = ExamConfig(
        exam_id="SCAN_TEST",
        course_code="CSE101",
        exam_name="Scan Test",
        exam_type="quiz",
        num_mcq=4,
        mcq_options=4,
        marks_per_mcq=1,
        written_questions=[],
    )
    return build_manifest(build_layout(config))


def test_four_random_black_squares_are_not_enough_to_be_an_omr():
    import cv2

    image = np.full((500, 700), 255, dtype=np.uint8)
    for x, y in [(80, 80), (620, 80), (80, 360), (620, 360)]:
        cv2.rectangle(image, (x - 14, y - 14), (x + 14, y + 14), 0, -1)

    with pytest.raises(ScanError):
        align_scan_page(image, _manifest(), dpi=200)
