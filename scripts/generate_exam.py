"""Generate an exam sheet (PDF) + template manifest (JSON) from a config file.

The config file is plain JSON matching CLAUDE.md Section 4.1 exactly — this
is the professor-facing input contract, so it's meant to be hand-edited or
produced by an admin-panel form later, not Python code.

Usage:
    python scripts/generate_exam.py configs/midsem_cs301.json
    python scripts/generate_exam.py configs/midsem_cs301.json --output-dir data/exams
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omr.generator.config import ExamConfig  # noqa: E402
from omr.generator.generate import generate_exam  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", type=Path, help="Path to a JSON exam config file (Section 4.1 format)")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "exams",
        help="Directory to write the PDF + manifest into (default: data/exams)",
    )
    args = parser.parse_args()

    if not args.config.exists():
        parser.error(f"config file not found: {args.config}")

    raw = json.loads(args.config.read_text())
    try:
        config = ExamConfig.model_validate(raw)
    except Exception as exc:
        parser.error(f"invalid exam config in {args.config}:\n{exc}")
        return

    try:
        result = generate_exam(config, args.output_dir)
    except ValueError as exc:
        print(f"Layout error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"PDF:      {result['pdf_path']}")
    print(f"Manifest: {result['manifest_path']}")


if __name__ == "__main__":
    main()
