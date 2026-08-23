"""CSV loading/writing for rosters, answer keys, and scan-evaluation results."""
from __future__ import annotations

import csv
from pathlib import Path

from omr.models import AnswerKeyEntry, EvaluationResult, Student


ROLL_HEADERS = ("roll_no", "roll", "roll_number", "student_roll", "student_id")
NAME_HEADERS = ("name", "student_name")
EMAIL_HEADERS = ("email", "mail", "student_email")
PROGRAM_HEADERS = ("program", "degree")


def normalize_roll(value: str) -> str:
    return "".join(str(value).upper().split())


def _field(row: dict[str, str], names: tuple[str, ...], required: bool = True) -> str:
    lowered = {k.strip().lower(): v for k, v in row.items()}
    for name in names:
        if name in lowered and str(lowered[name]).strip():
            return str(lowered[name]).strip()
    if required:
        raise ValueError(f"CSV row is missing one of these columns: {', '.join(names)}")
    return ""


def load_students(path: str | Path) -> dict[str, Student]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row")
        students: dict[str, Student] = {}
        known = set(ROLL_HEADERS + NAME_HEADERS + EMAIL_HEADERS + PROGRAM_HEADERS)
        for row_no, row in enumerate(reader, start=2):
            roll_no = normalize_roll(_field(row, ROLL_HEADERS))
            if not roll_no:
                raise ValueError(f"{path}:{row_no} has an empty roll number")
            if roll_no in students:
                raise ValueError(f"{path}:{row_no} duplicates roll number {roll_no}")
            program = _field(row, PROGRAM_HEADERS, required=False).upper()
            extra = {
                k: v
                for k, v in row.items()
                if k and k.strip().lower() not in known and v is not None and str(v).strip()
            }
            students[roll_no] = Student(
                roll_no=roll_no,
                name=_field(row, NAME_HEADERS),
                email=_field(row, EMAIL_HEADERS),
                program=program,
                extra=extra,
            )
    if not students:
        raise ValueError(f"{path} contains no students")
    return students


def load_answer_key(path: str | Path, default_marks: float = 1.0) -> dict[int, AnswerKeyEntry]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row")
        lowered = {name.strip().lower(): name for name in reader.fieldnames}
        q_col = lowered.get("q_no") or lowered.get("question") or lowered.get("question_no")
        answer_col = lowered.get("answer") or lowered.get("correct") or lowered.get("correct_option")
        marks_col = lowered.get("marks") or lowered.get("mark")
        if not q_col or not answer_col:
            raise ValueError("answer key CSV must have q_no and answer columns")

        key: dict[int, AnswerKeyEntry] = {}
        for row_no, row in enumerate(reader, start=2):
            try:
                q_no = int(str(row[q_col]).strip())
            except ValueError as exc:
                raise ValueError(f"{path}:{row_no} has invalid q_no {row[q_col]!r}") from exc
            answer = str(row[answer_col]).strip().upper()
            if not answer:
                raise ValueError(f"{path}:{row_no} has an empty answer")
            marks = default_marks
            if marks_col and str(row.get(marks_col, "")).strip():
                try:
                    marks = float(row[marks_col])
                except ValueError as exc:
                    raise ValueError(f"{path}:{row_no} has invalid marks {row[marks_col]!r}") from exc
            if q_no in key:
                raise ValueError(f"{path}:{row_no} duplicates Q{q_no}")
            key[q_no] = AnswerKeyEntry(q_no=q_no, answer=answer, marks=marks)
    if not key:
        raise ValueError(f"{path} contains no answer key entries")
    return key


def write_results_csv(results: list[EvaluationResult], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "scan_path",
        "status",
        "roll_no",
        "program",
        "student_name",
        "student_email",
        "score",
        "total",
        "review_flags",
        "details_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "scan_path": result.scan_path,
                    "status": result.status,
                    "roll_no": result.roll_no or "",
                    "program": result.program or "",
                    "student_name": result.student_name or "",
                    "student_email": result.student_email or "",
                    "score": f"{result.score:g}",
                    "total": f"{result.total:g}",
                    "review_flags": " | ".join(result.review_flags),
                    "details_path": result.details_path or "",
                }
            )
    return path
