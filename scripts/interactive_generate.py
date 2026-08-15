"""Interactive exam sheet generator — for demoing live in front of someone
who's calling out requirements on the spot (no JSON editing required).

Prompts one question at a time, generates the PDF + manifest as soon as
you're done, and opens the PDF automatically.

Usage:
    python scripts/interactive_generate.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omr.generator.config import ExamConfig, WrittenQuestionConfig  # noqa: E402
from omr.generator.generate import generate_exam  # noqa: E402


def ask(prompt: str, default: str | None = None, cast=str):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            raw = default
        if not raw:
            print("  This field is required.")
            continue
        try:
            return cast(raw)
        except ValueError:
            print(f"  Please enter a valid {cast.__name__ if hasattr(cast, '__name__') else 'value'}.")


def ask_int_range(prompt: str, lo: int, hi: int, default: str) -> int:
    while True:
        value = ask(prompt, default=default, cast=int)
        if lo <= value <= hi:
            return value
        print(f"  Please enter a number between {lo} and {hi}.")


def ask_choice(prompt: str, choices: list[str], default: str) -> str:
    while True:
        raw = (input(f"{prompt} ({'/'.join(choices)}) [{default}]: ").strip() or default).lower()
        if raw in choices:
            return raw
        print(f"  Please enter one of: {', '.join(choices)}")


def main() -> None:
    print("=== OMR Sheet Generator ===")
    print("Answer as the requirements are called out. Press Enter to accept a default.\n")

    exam_id = ask("Exam ID (used as filename, e.g. CS301_MIDSEM_2026A)")
    course_code = ask("Course code (e.g. CS301)")
    exam_name = ask("Exam name (e.g. Mid-Semester Examination)")
    exam_type = ask_choice("Exam type", ["quiz", "midsem", "endsem"], "quiz")

    num_mcq = ask("Number of MCQs", default="0", cast=int)
    mcq_options = 4
    marks_per_mcq = 1.0
    if num_mcq > 0:
        mcq_options = ask_int_range("Options per MCQ", 2, 6, default="4")
        marks_per_mcq = ask("Marks per correct MCQ", default="1", cast=float)

    written_questions: list[WrittenQuestionConfig] = []
    num_written = ask("Number of written questions", default="0", cast=int)
    for i in range(num_written):
        q_no = num_mcq + i + 1
        print(f"-- Written question {i + 1} (Q{q_no}) --")
        max_marks = ask(f"  Max marks for Q{q_no}", default="5", cast=float)
        lines = ask(f"  Number of answer lines for Q{q_no}", default="2", cast=int)
        written_questions.append(WrittenQuestionConfig(q_no=q_no, max_marks=max_marks, lines=lines))

    try:
        config = ExamConfig(
            exam_id=exam_id,
            course_code=course_code,
            exam_name=exam_name,
            exam_type=exam_type,
            num_mcq=num_mcq,
            mcq_options=mcq_options,
            marks_per_mcq=marks_per_mcq,
            written_questions=written_questions,
        )
    except ValidationError as exc:
        print(f"\nThat combination isn't valid:\n{exc}")
        sys.exit(1)

    output_dir = Path(__file__).resolve().parent.parent / "data" / "exams"
    try:
        result = generate_exam(config, output_dir)
    except ValueError as exc:
        print(f"\nLayout error: {exc}")
        print("Try fewer questions/lines, or split into multiple sheets.")
        sys.exit(1)

    print("\nGenerated:")
    print(f"  PDF:      {result['pdf_path']}")
    print(f"  Manifest: {result['manifest_path']}")

    if sys.platform == "win32":
        try:
            os.startfile(result["pdf_path"])  # opens in the default PDF viewer
        except OSError:
            pass


if __name__ == "__main__":
    main()
