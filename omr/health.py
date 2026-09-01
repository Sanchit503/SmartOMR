"""Deployment health checks for SmartOMR command-line services."""
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


def run_checks(data_dir: str | Path | None = None) -> dict[str, Any]:
    checks = _module_checks()
    checks.append(_writable_check(Path(data_dir) if data_dir else None))
    ok = all(check["status"] == "ready" for check in checks)
    return {
        "service": "smartomr",
        "version": __version__,
        "status": "ready" if ok else "error",
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartomr-health",
        description="Check whether SmartOMR runtime dependencies and data storage are usable.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Runtime data directory that should be writable by the deployed process.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable health status.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_checks(args.data_dir)
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
