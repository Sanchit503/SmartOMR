from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from omr.contracts.geometry import canonical_size_px, mm_to_px, px_per_mm
from omr.generator.config import ExamConfig
from omr.generator.layout import build_layout
from omr.generator.manifest import build_manifest
from prototype_eval.cv_scan import ScanError
from prototype_eval.pipeline import evaluate_scan


DPI = 200


def cv2():
    pytest.importorskip("cv2")
    import cv2 as _cv2

    return _cv2


def _manifest(num_mcq: int = 4) -> dict:
    config = ExamConfig(
        exam_id="PROTO_TEST",
        course_code="CS301",
        exam_name="Prototype Test",
        exam_type="midsem",
        num_mcq=num_mcq,
        marks_per_mcq=1,
        written_questions=[],
    )
    return build_manifest(build_layout(config))


def _rect(img: np.ndarray, dpi: float, x_mm: float, y_mm: float, w_mm: float, h_mm: float, fill: bool) -> None:
    c = cv2()
    scale = px_per_mm(dpi)
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    half_w = round(w_mm * scale / 2)
    half_h = round(h_mm * scale / 2)
    thickness = -1 if fill else 2
    c.rectangle(img, (cx - half_w, cy - half_h), (cx + half_w, cy + half_h), 0, thickness)


def _bubble(img: np.ndarray, manifest: dict, dpi: float, x_mm: float, y_mm: float, filled: bool = False) -> None:
    c = cv2()
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    outline_r = round(manifest["bubble_radius_mm"] * px_per_mm(dpi))
    c.circle(img, (cx, cy), outline_r, 0, 2)
    if filled:
        fill_r = round(manifest["bubble_sample_radius_mm"] * px_per_mm(dpi) * 1.12)
        c.circle(img, (cx, cy), fill_r, 0, -1)


def _draw_page(
    manifest: dict,
    roll_no: str = "2026001",
    answers: dict[int, str] | None = None,
    program: str = "BTECH",
) -> np.ndarray:
    c = cv2()
    width, height = canonical_size_px(manifest, DPI)
    img = np.full((height, width), 255, dtype=np.uint8)
    for fid in [f for f in manifest["fiducials"] if f.get("page", 1) == 1]:
        _rect(img, DPI, fid["x_mm"], fid["y_mm"], fid["size_mm"], fid["size_mm"], fill=True)
    for marker in [m for m in manifest["orientation_marker"] if m.get("page", 1) == 1]:
        _rect(img, DPI, marker["x_mm"], marker["y_mm"], marker["size_mm"], marker["size_mm"], fill=True)
    for mark in [m for m in manifest["page_marks"] if m.get("page", 1) == 1]:
        _rect(img, DPI, mark["x_mm"], mark["y_mm"], mark["width_mm"], mark["height_mm"], fill=mark["filled"])

    rb = manifest["roll_number_block"]
    for name, coords in rb["program_selector"].items():
        _bubble(img, manifest, DPI, coords["x_mm"], coords["y_mm"], filled=(name == program))

    grid = rb["btech_digits"] if program == "BTECH" else rb["mtech_digits"]
    digits = roll_no if program == "BTECH" else roll_no.removeprefix("MT")
    for col, digit_char in enumerate(digits):
        digit = int(digit_char)
        for row_digit in range(10):
            x_mm = grid["x_mm"] + col * grid["col_pitch_mm"]
            y_mm = grid["y_mm"] + row_digit * grid["row_pitch_mm"]
            _bubble(img, manifest, DPI, x_mm, y_mm, filled=(row_digit == digit))

    answers = answers or {}
    for entry in manifest["mcq_block"]:
        if entry.get("page", 1) != 1:
            continue
        for idx, option in enumerate(entry["options"]):
            x_mm = entry["x_mm"] + manifest["mcq_label_offset_mm"] + idx * manifest["mcq_option_pitch_mm"]
            _bubble(img, manifest, DPI, x_mm, entry["y_mm"], filled=(answers.get(entry["q_no"]) == option))
    return img


def _tilt(img: np.ndarray, angle: float = 3.0) -> np.ndarray:
    c = cv2()
    h, w = img.shape
    transform = c.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return c.warpAffine(img, transform, (w, h), flags=c.INTER_LINEAR, borderValue=255)


def _write_inputs(tmp_path: Path, manifest: dict) -> tuple[Path, Path, Path]:
    manifest_path = tmp_path / "PROTO_TEST.manifest.json"
    students_path = tmp_path / "students.csv"
    answer_key_path = tmp_path / "answer_key.csv"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    students_path.write_text("roll_no,name,email,program\n2026001,Aryan,aryan@example.com,BTECH\n", encoding="utf-8")
    answer_key_path.write_text("q_no,answer,marks\n1,A,1\n2,B,1\n3,C,1\n4,D,1\n", encoding="utf-8")
    return manifest_path, students_path, answer_key_path


def test_tilted_scan_recovers_roll_number_and_score(tmp_path: Path):
    c = cv2()
    manifest = _manifest()
    manifest_path, students_path, answer_key_path = _write_inputs(tmp_path, manifest)
    scan = _tilt(_draw_page(manifest, answers={1: "A", 2: "B", 3: "C", 4: "D"}))
    scan_path = tmp_path / "student_1.png"
    assert c.imwrite(str(scan_path), scan)

    result = evaluate_scan(
        scan_path,
        manifest_path=manifest_path,
        students_path=students_path,
        answer_key_path=answer_key_path,
        output_dir=tmp_path / "results",
        dpi=DPI,
    )

    assert result.status == "ready"
    assert result.roll_no == "2026001"
    assert result.student_email == "aryan@example.com"
    assert result.score == 4
    assert result.total == 4


def test_half_page_scan_is_rejected(tmp_path: Path):
    c = cv2()
    manifest = _manifest()
    manifest_path, students_path, answer_key_path = _write_inputs(tmp_path, manifest)
    full = _draw_page(manifest, answers={1: "A", 2: "B", 3: "C", 4: "D"})
    half = full[: full.shape[0] // 2, :]
    scan_path = tmp_path / "half_page.png"
    assert c.imwrite(str(scan_path), half)

    with pytest.raises(ScanError):
        evaluate_scan(
            scan_path,
            manifest_path=manifest_path,
            students_path=students_path,
            answer_key_path=answer_key_path,
            output_dir=tmp_path / "results",
            dpi=DPI,
        )
