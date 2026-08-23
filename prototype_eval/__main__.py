from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import batch_evaluate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate scanned SmartOMR prototype sheets.")
    parser.add_argument("--course-id", required=True, help="Folder id like CS301_2026 or CS301_2026_MIDSEM")
    parser.add_argument("--manifest", required=True, type=Path, help="Generated OMR manifest JSON")
    parser.add_argument("--students", required=True, type=Path, help="students.csv roster")
    parser.add_argument("--answer-key", required=True, type=Path, help="answer_key.csv from professor")
    parser.add_argument("--scans", required=True, type=Path, help="Scan image/PDF file or folder")
    parser.add_argument("--output-root", default=Path("prototype_eval/data"), type=Path)
    parser.add_argument("--dpi", default=200.0, type=float, help="Canonical reading DPI")
    args = parser.parse_args(argv)

    results, summary_path = batch_evaluate(
        course_id=args.course_id,
        manifest_path=args.manifest,
        students_path=args.students,
        answer_key_path=args.answer_key,
        scans_path=args.scans,
        output_root=args.output_root,
        dpi=args.dpi,
    )
    ready = sum(1 for r in results if r.status == "ready")
    review = sum(1 for r in results if r.status == "needs_review")
    errors = sum(1 for r in results if r.status == "error")
    print(f"Wrote {summary_path}")
    print(f"ready={ready} needs_review={review} error={errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
