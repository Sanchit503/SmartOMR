from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from omr.datasets.train_roll_digit_resnet import _simulate_faint_pencil


def test_faint_pencil_augmentation_lightens_ink_without_erasing_the_stroke(monkeypatch):
    image = Image.new("L", (32, 32), 255)
    ImageDraw.Draw(image).line([(8, 24), (16, 6), (24, 24)], fill=0, width=3)
    values = iter([0.50, 0.0, 0.0])
    monkeypatch.setattr("omr.datasets.train_roll_digit_resnet.random.uniform", lambda *_args: next(values))

    augmented = np.asarray(_simulate_faint_pencil(image))

    assert 0 < int(augmented[10, 14]) < 255
    assert int(augmented[10, 14]) < int(augmented[1, 1])
