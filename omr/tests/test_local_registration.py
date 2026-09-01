from __future__ import annotations

import pytest

from omr.local_registration import estimate_local_transform, fit_ordered_local_transform


def test_local_transform_recovers_block_shift_with_one_disturbed_anchor():
    expected = [(col * 30.0, row * 24.0) for row in range(5) for col in range(4)]
    observed = [(x + 7.0, y - 4.0) for x, y in expected]
    observed[6] = (expected[6][0] + 25.0, expected[6][1] + 20.0)
    observed.append((900.0, 900.0))

    transform = estimate_local_transform(
        expected,
        observed,
        max_distance_px=40.0,
        min_matches=8,
        ransac_reproj_threshold_px=3.0,
    )

    assert transform is not None
    assert transform.matched_count >= 18
    assert transform.mean_residual_px < 1.0
    assert transform.apply((90.0, 96.0)) == pytest.approx((97.0, 92.0), abs=1.0)


def test_ordered_local_transform_preserves_grid_row_identity():
    expected = [(col * 30.0, row * 24.0) for row in range(5) for col in range(4)]
    observed = [(x + 3.0, y + 20.0) for x, y in expected]

    transform = fit_ordered_local_transform(expected, observed, min_points=3)

    assert transform is not None
    assert transform.apply((60.0, 48.0)) == pytest.approx((63.0, 68.0), abs=0.5)
