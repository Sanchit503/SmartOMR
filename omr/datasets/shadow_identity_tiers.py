"""Run the production identity resolver in non-mutating shadow mode."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from omr.io.csv import load_students
from omr.workflows.identity_resolution import resolve_identity_proposals, resolution_digest


def _read(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_optional_path(value: object, base: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    for candidate in (base / path, Path.cwd() / path):
        if candidate.exists():
            return candidate
    return base / path


def _current_assignments(parse_index: dict[str, Any]) -> dict[int, str]:
    assignments: dict[int, str] = {}
    for student in parse_index.get("students", []):
        roll_no = str(student.get("roll_no") or "")
        for page in student.get("pages", []):
            source_index = page.get("source_index")
            if roll_no and source_index is not None:
                assignments[int(source_index)] = roll_no
    return assignments


def build(
    preview_path: Path,
    output_path: Path,
    *,
    students_path: Path | None = None,
    parse_index_path: Path | None = None,
) -> dict[str, int]:
    preview_path = preview_path.resolve()
    run_dir = preview_path.parent.parent
    state = _read(run_dir / "run_state.json")
    inputs = state.get("inputs") if isinstance(state.get("inputs"), dict) else {}
    students_path = students_path or _resolve_optional_path(inputs.get("students_path"), run_dir)
    parse_index_path = parse_index_path or _resolve_optional_path(state.get("parse_index_path"), run_dir)

    students = load_students(students_path) if students_path and students_path.is_file() else None
    parse_index = _read(parse_index_path)
    assignments = _current_assignments(parse_index)
    inspection = _read(run_dir / "inspection" / "index.json")
    aligned_paths = {
        int(page.get("source_index") or 0): str(page.get("aligned") or "")
        for page in inspection.get("pages", [])
        if isinstance(page, dict)
    }

    preview = _read(preview_path)
    pages = []
    for row in preview.get("pages", []):
        if not isinstance(row, dict) or not row.get("sheet_page"):
            continue
        copied = dict(row)
        source_index = int(copied.get("source_index") or 0)
        copied["page_image_path"] = aligned_paths.get(source_index, "")
        copied["current_roll"] = assignments.get(source_index)
        pages.append(copied)

    payload = resolve_identity_proposals(
        pages,
        valid_rolls=set(students) if students else None,
        program_by_roll={roll: student.program for roll, student in (students or {}).items()},
        current_assignments=assignments,
    )
    payload["source_preview_path"] = str(preview_path)
    payload["source_parse_index_path"] = str(parse_index_path) if parse_index_path else None
    payload["roster_path"] = str(students_path) if students_path else None
    payload["digest"] = resolution_digest(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return dict(payload["counts"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--students", type=Path)
    parser.add_argument("--parse-index", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.preview,
                args.out,
                students_path=args.students,
                parse_index_path=args.parse_index,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
