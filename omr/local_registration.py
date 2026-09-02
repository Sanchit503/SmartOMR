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


def _matrix_is_reasonable(matrix: np.ndarray) -> bool:
    """Reject affine fits that only look good because bad anchors overfit."""
    linear = np.asarray(matrix[:, :2], dtype=np.float32)
    if not np.isfinite(linear).all():
        return False
    determinant = float(np.linalg.det(linear))
    if determinant <= 0:
        return False
    try:
        singular_values = np.linalg.svd(linear, compute_uv=False)
    except np.linalg.LinAlgError:
        return False
    smallest = float(singular_values.min())
    largest = float(singular_values.max())
    if smallest < 0.70 or largest > 1.35:
        return False
    return largest / max(smallest, 1e-6) <= 1.45


def _translation_transform(matches: list[tuple[Point, Point]], expected_count: int) -> LocalTransform:
    deltas = np.array(
        [[observed[0] - expected[0], observed[1] - expected[1]] for expected, observed in matches],
        dtype=np.float32,
    )
    dx, dy = np.median(deltas, axis=0)
    matrix = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float32)
    return _report_transform(matrix, matches, expected_count)


def _best_transform_from_matches(
    matches: list[tuple[Point, Point]],
    expected_count: int,
    min_points: int,
    ransac_reproj_threshold_px: float,
) -> LocalTransform | None:
    if len(matches) < min_points:
        return None

    candidates: list[LocalTransform] = []
    cv2 = _cv2()
    if cv2 is not None and len(matches) >= 3:
        src = np.asarray([expected for expected, _observed in matches], dtype=np.float32)
        dst = np.asarray([observed for _expected, observed in matches], dtype=np.float32)
        estimators = []
        if hasattr(cv2, "estimateAffine2D"):
            estimators.append(cv2.estimateAffine2D)
        estimators.append(cv2.estimateAffinePartial2D)

        for estimator in estimators:
            matrix, inliers = estimator(
                src,
                dst,
                method=cv2.RANSAC,
                ransacReprojThreshold=ransac_reproj_threshold_px,
                maxIters=2000,
                confidence=0.99,
                refineIters=10,
            )
            if matrix is None or inliers is None or not _matrix_is_reasonable(matrix):
                continue
            inlier_matches = [match for match, keep in zip(matches, inliers.ravel()) if int(keep)]
            if len(inlier_matches) >= min_points:
                candidates.append(_report_transform(matrix, inlier_matches, expected_count))

    candidates.append(_translation_transform(matches, expected_count))
    return min(
        candidates,
        key=lambda transform: (
            -transform.matched_count,
            transform.mean_residual_px,
            transform.max_residual_px,
        ),
    )


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

    threshold = ransac_reproj_threshold_px or max(2.0, max_distance_px * 0.22)
    return _best_transform_from_matches(matches, len(expected_points), min_matches, threshold)


def _unique_nearest_matches_with_offset(
    expected_points: np.ndarray,
    observed_points: np.ndarray,
    offset: np.ndarray,
    max_distance_px: float,
) -> list[tuple[Point, Point]]:
    candidates: list[tuple[float, int, int]] = []
    shifted_expected = expected_points + offset
    for expected_index, expected_point in enumerate(shifted_expected):
        distances = np.linalg.norm(observed_points - expected_point, axis=1)
        observed_index = int(np.argmin(distances))
        distance = float(distances[observed_index])
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
        expected = expected_points[expected_index]
        observed = observed_points[observed_index]
        matches.append(
            (
                (float(expected[0]), float(expected[1])),
                (float(observed[0]), float(observed[1])),
            )
        )
    return matches


def _candidate_offsets(
    expected_points: np.ndarray,
    observed_points: np.ndarray,
    vote_tolerance_px: float,
    max_candidates: int = 80,
) -> list[np.ndarray]:
    bins: dict[tuple[int, int], list[np.ndarray]] = {}
    for observed in observed_points:
        for expected in expected_points:
            delta = observed - expected
            key = (
                int(round(float(delta[0]) / vote_tolerance_px)),
                int(round(float(delta[1]) / vote_tolerance_px)),
            )
            bins.setdefault(key, []).append(delta)

    ranked = sorted(bins.values(), key=len, reverse=True)
    offsets = [np.median(np.asarray(group, dtype=np.float32), axis=0) for group in ranked[:max_candidates]]
    offsets.append(np.array([0.0, 0.0], dtype=np.float32))
    return offsets


def fit_candidate_local_transform(
    expected_points: Sequence[Point],
    observed_points: Sequence[Point],
    max_distance_px: float,
    min_matches: int = 3,
    vote_tolerance_px: float | None = None,
    ransac_reproj_threshold_px: float | None = None,
) -> LocalTransform | None:
    """Fit a local transform from unordered printed-anchor candidates.

    This is stricter than a plain nearest-neighbor fit. It first lets every
    observed anchor vote for a rough block offset, then matches expected
    manifest anchors around the best offsets before fitting a small affine
    correction. That handles blocks that moved more than half a bubble pitch
    after a rough page warp, while still preserving each question/option's
    identity from the manifest.
    """
    expected = np.asarray(expected_points, dtype=np.float32)
    observed = np.asarray(observed_points, dtype=np.float32)
    if expected.size == 0 or observed.size == 0 or len(expected) < min_matches:
        return None

    vote_tolerance = vote_tolerance_px or max(3.0, max_distance_px * 0.45)
    threshold = ransac_reproj_threshold_px or max(2.0, max_distance_px * 0.35)
    best: LocalTransform | None = None
    for offset in _candidate_offsets(expected, observed, vote_tolerance):
        matches = _unique_nearest_matches_with_offset(expected, observed, offset, max_distance_px)
        transform = _best_transform_from_matches(matches, len(expected), min_matches, threshold)
        if transform is None:
            continue
        if best is None or (
            transform.matched_count,
            -transform.mean_residual_px,
            -transform.max_residual_px,
        ) > (
            best.matched_count,
            -best.mean_residual_px,
            -best.max_residual_px,
        ):
            best = transform
    return best


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
    return _best_transform_from_matches(matches, len(expected_points), min_points, ransac_reproj_threshold_px)
