"""Local block registration helpers shared by reader and grading code.

The global page homography handles the big camera/perspective error. This
module handles the small residual drift left inside a roll-number or MCQ
block by estimating one stable transform from many nearby printed anchors.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


Point = tuple[float, float]


@dataclass(frozen=True)
class LocalTransform:
    matrix: np.ndarray
    expected_count: int
    matched_count: int
    mean_residual_px: float
    max_residual_px: float

    def apply(self, point: Point) -> Point:
        x, y = point
        transformed = self.matrix @ np.array([x, y, 1.0], dtype=np.float32)
        return float(transformed[0]), float(transformed[1])


def _cv2():
    try:
        import cv2
    except ImportError:
        return None
    return cv2


def _unique_nearest_matches(
    expected_points: Sequence[Point],
    observed_points: Sequence[Point],
    max_distance_px: float,
) -> list[tuple[Point, Point]]:
    expected = np.asarray(expected_points, dtype=np.float32)
    observed = np.asarray(observed_points, dtype=np.float32)
    if expected.size == 0 or observed.size == 0:
        return []

    candidates: list[tuple[float, int, int]] = []
    for observed_index, observed_point in enumerate(observed):
        distances = np.linalg.norm(expected - observed_point, axis=1)
        expected_index = int(np.argmin(distances))
        distance = float(distances[expected_index])
        if distance <= max_distance_px:
            candidates.append((distance, expected_index, observed_index))

    matches: list[tuple[Point, Point]] = []
    used_expected: set[int] = set()
    used_observed: set[int] = set()
    for _distance, expected_index, observed_index in sorted(candidates):
        if expected_index in used_expected or observed_index in used_observed:
            continue
        used_expected.add(expected_index)
        used_observed.add(observed_index)
        matches.append(
            (
                (float(expected[expected_index][0]), float(expected[expected_index][1])),
                (float(observed[observed_index][0]), float(observed[observed_index][1])),
            )
        )
    return matches


def _report_transform(matrix: np.ndarray, matches: list[tuple[Point, Point]], expected_count: int) -> LocalTransform:
    residuals: list[float] = []
    for expected, observed in matches:
        predicted = matrix @ np.array([expected[0], expected[1], 1.0], dtype=np.float32)
        residuals.append(float(np.linalg.norm(predicted - np.asarray(observed, dtype=np.float32))))

    return LocalTransform(
        matrix=matrix.astype(np.float32),
        expected_count=expected_count,
        matched_count=len(matches),
        mean_residual_px=float(np.mean(residuals)) if residuals else 0.0,
        max_residual_px=max(residuals, default=0.0),
    )


def _translation_transform(matches: list[tuple[Point, Point]], expected_count: int) -> LocalTransform:
    deltas = np.array(
        [[observed[0] - expected[0], observed[1] - expected[1]] for expected, observed in matches],
        dtype=np.float32,
    )
    dx, dy = np.median(deltas, axis=0)
    matrix = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float32)
    return _report_transform(matrix, matches, expected_count)


def estimate_local_transform(
    expected_points: Sequence[Point],
    observed_points: Sequence[Point],
    max_distance_px: float,
    min_matches: int = 3,
    ransac_reproj_threshold_px: float | None = None,
) -> LocalTransform | None:
    """Estimate one transform for a local OMR block.

    `expected_points` come from the manifest. `observed_points` come from
    detected printed anchors near that block. Matching is deliberately local
    and one-to-one so a filled bubble or stray mark cannot pull the entire
    transform far away.
    """
    matches = _unique_nearest_matches(expected_points, observed_points, max_distance_px)
    if len(matches) < min_matches:
        return None

    cv2 = _cv2()
    if cv2 is not None and len(matches) >= 3:
        src = np.asarray([expected for expected, _observed in matches], dtype=np.float32)
        dst = np.asarray([observed for _expected, observed in matches], dtype=np.float32)
        threshold = ransac_reproj_threshold_px or max(2.0, max_distance_px * 0.22)
        matrix, inliers = cv2.estimateAffinePartial2D(
            src,
            dst,
            method=cv2.RANSAC,
            ransacReprojThreshold=threshold,
            maxIters=1000,
            confidence=0.98,
            refineIters=10,
        )
        if matrix is not None and inliers is not None:
            inlier_matches = [match for match, keep in zip(matches, inliers.ravel()) if int(keep)]
            if len(inlier_matches) >= min_matches:
                return _report_transform(matrix, inlier_matches, len(expected_points))

    return _translation_transform(matches, len(expected_points))


def fit_ordered_local_transform(
    expected_points: Sequence[Point],
    observed_points: Sequence[Point],
    min_points: int = 3,
    ransac_reproj_threshold_px: float = 3.0,
) -> LocalTransform | None:
    """Fit a transform when expected and observed anchors are already paired.

    Regular OMR grids have topology: row 0 is the top row, row 1 is the next
    row, and so on. Preserving that order avoids nearest-neighbor aliasing
    when a whole block is shifted by more than half a row/column pitch.
    """
    if len(expected_points) != len(observed_points) or len(expected_points) < min_points:
        return None

    matches = list(zip(expected_points, observed_points))
    cv2 = _cv2()
    if cv2 is not None and len(matches) >= 3:
        src = np.asarray(expected_points, dtype=np.float32)
        dst = np.asarray(observed_points, dtype=np.float32)
        matrix, inliers = cv2.estimateAffinePartial2D(
            src,
            dst,
            method=cv2.RANSAC,
            ransacReprojThreshold=ransac_reproj_threshold_px,
            maxIters=1000,
            confidence=0.98,
            refineIters=10,
        )
        if matrix is not None and inliers is not None:
            inlier_matches = [match for match, keep in zip(matches, inliers.ravel()) if int(keep)]
            if len(inlier_matches) >= min_points:
                return _report_transform(matrix, inlier_matches, len(expected_points))

    return _translation_transform(matches, len(expected_points))
