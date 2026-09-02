"""Local runtime diagnostics for SmartOMR command-line workflows."""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import __version__


REQUIRED_MODULES = {
    "cv2": "OpenCV",
    "pymupdf": "PyMuPDF",
    "numpy": "NumPy",
    "PIL": "Pillow",
    "pydantic": "Pydantic",
    "pypdf": "pypdf",
    "reportlab": "ReportLab",
}


def _module_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for module_name, label in REQUIRED_MODULES.items():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            checks.append(
                {
                    "name": label,
                    "status": "error",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        checks.append(
            {
                "name": label,
                "status": "ready",
                "version": str(getattr(module, "__version__", "unknown")),
            }
        )
    return checks


def _writable_check(path: Path | None) -> dict[str, Any]:
    target = path or Path(tempfile.gettempdir())
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".smartomr_healthcheck"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as exc:
        return {
            "name": "runtime_data_dir",
            "status": "error",
            "path": str(target),
            "detail": f"{type(exc).__name__}: {exc}",
        }
    return {
        "name": "runtime_data_dir",
        "status": "ready",
        "path": str(target),
    }


def _handwriting_ocr_check() -> dict[str, Any]:
    try:
        import pytesseract
    except Exception as exc:
        return {
            "name": "handwriting_ocr",
            "status": "error",
            "detail": f"pytesseract unavailable: {type(exc).__name__}: {exc}",
        }
    try:
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:
        return {
            "name": "handwriting_ocr",
            "status": "error",
            "detail": f"tesseract binary unavailable: {type(exc).__name__}: {exc}",
        }
    return {
        "name": "handwriting_ocr",
        "status": "ready",
        "version": version,
    }


def _digit_model_check(model_path: Path) -> dict[str, Any]:
    try:
        from omr.reader.digit_model import OpenCVDigitKnn

        model = OpenCVDigitKnn(model_path)
    except Exception as exc:
        return {
            "name": "digit_model",
            "status": "error",
            "path": str(model_path),
            "detail": f"{type(exc).__name__}: {exc}",
        }
    return {
        "name": "digit_model",
        "status": "ready",
        "path": str(model_path),
        "samples": int(model.labels.shape[0]),
        "k": int(model.k),
    }


def run_checks(
    data_dir: str | Path | None = None,
    check_handwriting_ocr: bool = False,
    digit_model: str | Path | None = None,
) -> dict[str, Any]:
    checks = _module_checks()
    checks.append(_writable_check(Path(data_dir) if data_dir else None))
    if check_handwriting_ocr:
        checks.append(_handwriting_ocr_check())
    if digit_model:
        checks.append(_digit_model_check(Path(digit_model)))
    ok = all(check["status"] == "ready" for check in checks)
    return {
        "service": "smartomr",
        "version": __version__,
        "status": "ready" if ok else "error",
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartomr-doctor",
        description="Check whether SmartOMR runtime dependencies and local data storage are usable.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Local data directory that should be writable by SmartOMR.",
    )
    parser.add_argument(
        "--check-handwriting-ocr",
        action="store_true",
        help="Also verify local Tesseract handwriting OCR support.",
    )
    parser.add_argument(
        "--digit-model",
        type=Path,
        default=None,
        help="Optional .npz digit model to load and validate.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable health status.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_checks(
        args.data_dir,
        check_handwriting_ocr=args.check_handwriting_ocr,
        digit_model=args.digit_model,
    )
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"SmartOMR {payload['version']}: {payload['status']}")
        for check in payload["checks"]:
            detail = check.get("version") or check.get("path") or check.get("detail", "")
            print(f"  {check['name']}: {check['status']} {detail}".rstrip())
    return 0 if payload["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
