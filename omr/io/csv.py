"""CSV loading/writing for rosters, answer keys, rubrics, and results."""
from __future__ import annotations

import csv
import re
from pathlib import Path

from omr.models import AnswerKeyEntry, EvaluationResult, Student, WrittenQuestionMeta


ROLL_HEADERS = ("roll_no", "roll", "roll_number", "student_roll", "student_id", "rollno")
NAME_HEADERS = ("name", "student_name", "studentname")
EMAIL_HEADERS = ("email", "mail", "student_email", "email_id", "emailid")
PROGRAM_HEADERS = ("program", "degree", "class_type", "classtype")
WRITTEN_Q_HEADERS = ("q_no", "question_no", "question_number", "q")
QUESTION_TEXT_HEADERS = ("question_text", "question", "prompt")
RUBRIC_HEADERS = ("rubric", "marking_guideline", "marking_guidelines", "guideline", "guidelines")
MODEL_ANSWER_HEADERS = ("model_answer", "expected_answer", "sample_answer")
MAX_MARKS_HEADERS = ("max_marks", "marks", "marks_possible")
PROGRAM_ALIASES = {
    "BTECH": "BTECH",
    "BTECHNOLOGY": "BTECH",
    "MTECH": "MTECH",
    "MT": "MTECH",
    "MTECHNOLOGY": "MTECH",
    "PHD": "PHD",
    "DOCTOROFPHILOSOPHY": "PHD",
    "SP": "SP",
}


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def normalize_roll(value: str) -> str:
    return "".join(str(value).upper().split())


def normalize_student_roll(value: str, program: str | None = None) -> str:
    roll = normalize_roll(value)
    if not roll:
        return roll
    program_key = re.sub(r"[^A-Z0-9]+", "", str(program or "").upper())
    if program_key in {"MTECH", "MT"} and roll.isdigit():
        return f"MT{roll}"
    if program_key == "PHD" and roll.isdigit():
        return f"PHD{roll}"
    if program_key.startswith("SP") and not roll.startswith("SP"):
        return f"SP{roll}"
    return roll


def _program_from_row(value: str, raw_roll: str, row: dict[str, str]) -> str:
    program_key = re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())
    if program_key in PROGRAM_ALIASES:
        return PROGRAM_ALIASES[program_key]

    roll = normalize_roll(raw_roll)
    if re.fullmatch(r"PHD\d+", roll):
        return "PHD"
    if re.fullmatch(r"MT\d+", roll):
        return "MTECH"
    if re.fullmatch(r"SP[A-Z0-9]+", roll):
        return "SP"
    if re.fullmatch(r"\d{7}", roll):
        return "BTECH"

    context = " ".join(str(item or "") for item in row.values()).upper()
    compact_context = re.sub(r"[^A-Z0-9]+", "", context)
    for marker, program in (("PHD", "PHD"), ("MTECH", "MTECH"), ("BTECH", "BTECH")):
        if marker in compact_context:
            return program
    return ""


def _email_from_row(row: dict[str, str]) -> str:
    configured = _field(row, EMAIL_HEADERS, required=False)
    if configured:
        return configured

    candidates = {
        str(value).strip()
        for value in row.values()
        if value is not None
        and re.fullmatch(r"[^\s@,]+@[^\s@,]+\.[^\s@,]+", str(value).strip())
    }
    if len(candidates) == 1:
        return candidates.pop()
    if len(candidates) > 1:
        raise ValueError("CSV row contains multiple possible email addresses")
    raise ValueError(f"CSV row is missing one of these columns: {', '.join(EMAIL_HEADERS)}")


def _field(row: dict[str, str], names: tuple[str, ...], required: bool = True) -> str:
    lowered = {k.strip().lower(): v for k, v in row.items()}
    canonical = {_header_key(k): v for k, v in row.items()}
    for name in names:
        if name in lowered and str(lowered[name]).strip():
            return str(lowered[name]).strip()
        key = _header_key(name)
        if key in canonical and str(canonical[key]).strip():
            return str(canonical[key]).strip()
    if required:
        raise ValueError(f"CSV row is missing one of these columns: {', '.join(names)}")
    return ""


def _column(fieldnames: list[str], names: tuple[str, ...]) -> str | None:
    lowered = {name.strip().lower(): name for name in fieldnames}
    canonical = {_header_key(name): name for name in fieldnames}
    for name in names:
        if name in lowered:
            return lowered[name]
        key = _header_key(name)
        if key in canonical:
            return canonical[key]
    return None


def load_students(path: str | Path) -> dict[str, Student]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row")
        students: dict[str, Student] = {}
        known = {_header_key(name) for name in ROLL_HEADERS + NAME_HEADERS + EMAIL_HEADERS + PROGRAM_HEADERS}
        for row_no, row in enumerate(reader, start=2):
            if not any(str(value or "").strip() for value in row.values()):
                continue
            raw_roll = _field(row, ROLL_HEADERS)
            program = _program_from_row(_field(row, PROGRAM_HEADERS, required=False), raw_roll, row)
            roll_no = normalize_student_roll(raw_roll, program)
            if not roll_no:
                raise ValueError(f"{path}:{row_no} has an empty roll number")
            if roll_no in students:
                raise ValueError(f"{path}:{row_no} duplicates roll number {roll_no}")
            extra = {
                k: v
                for k, v in row.items()
                if k and _header_key(k) not in known and v is not None and str(v).strip()
            }
            try:
                email = _email_from_row(row)
            except ValueError as exc:
                raise ValueError(f"{path}:{row_no} {exc}") from exc
            students[roll_no] = Student(
                roll_no=roll_no,
                name=" ".join(_field(row, NAME_HEADERS).split()),
                email=email,
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


def load_written_question_metadata(path: str | Path) -> dict[int, WrittenQuestionMeta]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row")

        q_col = _column(reader.fieldnames, WRITTEN_Q_HEADERS)
        text_col = _column(reader.fieldnames, QUESTION_TEXT_HEADERS)
        rubric_col = _column(reader.fieldnames, RUBRIC_HEADERS)
        model_col = _column(reader.fieldnames, MODEL_ANSWER_HEADERS)
        marks_col = _column(reader.fieldnames, MAX_MARKS_HEADERS)
        if not q_col:
            raise ValueError("written rubric CSV must have a q_no column")
        known = {
            name
            for name in (q_col, text_col, rubric_col, model_col, marks_col)
            if name is not None
        }

        metadata: dict[int, WrittenQuestionMeta] = {}
        for row_no, row in enumerate(reader, start=2):
            try:
                q_no = int(str(row.get(q_col, "")).strip())
            except ValueError as exc:
                raise ValueError(f"{path}:{row_no} has invalid q_no {row.get(q_col)!r}") from exc

            question_text = str(row.get(text_col, "")).strip() if text_col else ""
            rubric = str(row.get(rubric_col, "")).strip() if rubric_col else ""
            model_answer = str(row.get(model_col, "")).strip() if model_col else ""
            max_marks = None
            if marks_col and str(row.get(marks_col, "")).strip():
                try:
                    max_marks = float(row[marks_col])
                except ValueError as exc:
                    raise ValueError(f"{path}:{row_no} has invalid max_marks {row[marks_col]!r}") from exc
                if max_marks <= 0:
                    raise ValueError(f"{path}:{row_no} has non-positive max_marks {max_marks:g}")

            if q_no in metadata:
                raise ValueError(f"{path}:{row_no} duplicates Q{q_no}")
            if not any((question_text, rubric, model_answer, max_marks is not None)):
                raise ValueError(f"{path}:{row_no} has no written-question metadata for Q{q_no}")

            extra = {
                key: value
                for key, value in row.items()
                if key and key not in known and value is not None and str(value).strip()
            }
            metadata[q_no] = WrittenQuestionMeta(
                q_no=q_no,
                question_text=question_text,
                rubric=rubric,
                model_answer=model_answer,
                max_marks=max_marks,
                extra=extra,
            )
    if not metadata:
        raise ValueError(f"{path} contains no written-question metadata")
    return metadata


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
