"""SmartOMR sheet generator — the entry point.

Run it and answer the prompts (how many MCQs, how many written questions,
course, exam name); it writes the printable PDF and its template manifest
and opens the PDF. That's the whole workflow.

    python -m omr.generator.main                      # ask me the questions
    python -m omr.generator.main --config quiz.json   # repeatable, from a saved config

Both modes end up in the same `generate_exam()` call, so a sheet produced
from prompts is byte-identical to the same sheet produced from a config file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .config import DEFAULT_UNIVERSITY_NAME, ExamConfig, NumericalQuestionConfig, WrittenQuestionConfig
from .generate import generate_exam

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data" / "exams"
EXAMPLE_CONFIG_DIR = Path(__file__).resolve().parent / "configs"


# --------------------------------------------------------------------------
# Prompt helpers
# --------------------------------------------------------------------------

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
            print(f"  Please enter a valid {getattr(cast, '__name__', 'value')}.")


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


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please answer y or n.")


# --------------------------------------------------------------------------
# Wizard
# --------------------------------------------------------------------------

def prompt_for_config() -> ExamConfig:
    """Ask for an exam's requirements one at a time.

    Numerical and written questions are asked for in bulk first because most
    exams reuse one grid/box shape. Only differing questions fall through to
    per-question prompts.
    """
    # Plain ASCII on purpose: the default Windows console codepage mangles
    # non-ASCII punctuation into replacement characters.
    print("=== SmartOMR - Generate an OMR Sheet ===")
    print("Answer the prompts. Press Enter to accept a default.\n")

    print("-- Exam details --")
    university_name = ask("University name", default=DEFAULT_UNIVERSITY_NAME)
    course_code = ask("Course code (e.g. CS301)")
    exam_name = ask("Exam name (e.g. Mid-Semester Examination)")
    exam_type = ask_choice("Exam type", ["quiz", "midsem", "endsem"], "quiz")
    exam_id = ask("Exam ID (used as the filename)", default=f"{course_code}_{exam_type.upper()}")

    print("\n-- Section A: MCQs --")
    num_mcq = ask("How many MCQs?", default="0", cast=int)
    mcq_options = 4
    marks_per_mcq = 1.0
    if num_mcq > 0:
        mcq_options = ask_int_range("How many options per MCQ (A-B up to A-F)?", 2, 6, default="4")
        marks_per_mcq = ask("Marks per correct MCQ", default="1", cast=float)

    print("\n-- Numerical answers (non-negative whole numbers) --")
    num_numerical = ask_int_range("How many numerical questions?", 0, 1000, default="0")
    numerical_questions: list[NumericalQuestionConfig] = []
    if num_numerical:
        uniform = num_numerical == 1 or ask_yes_no(
            f"Do all {num_numerical} numerical questions have the same marks and maximum digits?"
        )
        if uniform:
            max_marks = ask("  Max marks for each numerical question", default="1", cast=float)
            digits = ask_int_range("  Maximum digits per answer", 1, 8, default="3")
            numerical_questions = [
                NumericalQuestionConfig(q_no=num_mcq + i + 1, max_marks=max_marks, digits=digits)
                for i in range(num_numerical)
            ]
        else:
            for i in range(num_numerical):
                q_no = num_mcq + i + 1
                max_marks = ask(f"  Max marks for Q{q_no}", default="1", cast=float)
                digits = ask_int_range(f"  Maximum digits for Q{q_no}", 1, 8, default="3")
                numerical_questions.append(NumericalQuestionConfig(q_no=q_no, max_marks=max_marks, digits=digits))

    print("\n-- Written answers --")
    num_written = ask("How many written questions?", default="0", cast=int)
    written_questions: list[WrittenQuestionConfig] = []

    if num_written > 0:
        uniform = num_written == 1 or ask_yes_no(
            f"Do all {num_written} written questions have the same marks and answer lines?"
        )
        if uniform:
            max_marks = ask("  Max marks for each", default="5", cast=float)
            lines = ask("  Answer lines for each (2 = a two-line short answer)", default="2", cast=int)
            written_questions = [
                WrittenQuestionConfig(q_no=num_mcq + num_numerical + i + 1, max_marks=max_marks, lines=lines)
                for i in range(num_written)
            ]
        else:
            for i in range(num_written):
                q_no = num_mcq + num_numerical + i + 1
                print(f"  -- Q{q_no} --")
                max_marks = ask(f"    Max marks for Q{q_no}", default="5", cast=float)
                lines = ask(f"    Answer lines for Q{q_no}", default="2", cast=int)
                written_questions.append(
                    WrittenQuestionConfig(q_no=q_no, max_marks=max_marks, lines=lines)
                )

    return ExamConfig(
        exam_id=exam_id,
        university_name=university_name,
        course_code=course_code,
        exam_name=exam_name,
        exam_type=exam_type,
        num_mcq=num_mcq,
        mcq_options=mcq_options,
        marks_per_mcq=marks_per_mcq,
        written_questions=written_questions,
        numerical_questions=numerical_questions,
    )


def load_config_file(path: Path) -> ExamConfig:
    """Load a Section 4.1 JSON config — the professor-facing input contract,
    meant to be hand-edited or produced by an admin-panel form later."""
    return ExamConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _open_pdf(pdf_path: Path) -> None:
    if sys.platform == "win32":
        try:
            os.startfile(pdf_path)  # noqa: S606 — opens in the default PDF viewer
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartomr-generate",
        description="Generate a printable OMR sheet (PDF) and its template manifest (JSON).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Example configs live in {EXAMPLE_CONFIG_DIR}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="FILE.json",
        help="Generate from a saved JSON exam config instead of asking (Section 4.1 format)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Where to write the PDF + manifest (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't open the generated PDF in a viewer afterwards",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="Skip the preflight readability check (not recommended before printing)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.config:
            if not args.config.exists():
                print(f"Config file not found: {args.config}", file=sys.stderr)
                return 2
            config = load_config_file(args.config)
        else:
            config = prompt_for_config()
    except ValidationError as exc:
        print(f"\nThat exam configuration isn't valid:\n{exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 130
    except json.JSONDecodeError as exc:
        print(f"\n{args.config} isn't valid JSON: {exc}", file=sys.stderr)
        return 1

    try:
        result = generate_exam(config, args.output_dir)
    except PermissionError as exc:
        print(
            "\nCould not write the output file. If the PDF or manifest is open, close it and run "
            "the generator again. You can also use a different Exam ID or --output-dir.\n"
            f"Locked path: {exc.filename or exc}",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        # The generator refuses to guess a layout it can't place (Section 2,
        # principle 4), so this is a real hard stop, not a warning.
        print(f"\nLayout error: {exc}", file=sys.stderr)
        return 1

    num_pages = result["manifest"]["num_pages"]
    print(f"\nGenerated {num_pages} page{'s' if num_pages != 1 else ''}:")
    print(f"  PDF:      {result['pdf_path']}")
    print(f"  Manifest: {result['manifest_path']}")

    if not args.no_check:
        from .preflight import check_sheet

        report = check_sheet(result["pdf_path"], result["manifest"])
        print()
        print(report.format())
        if not report.ok:
            # The sheet exists, but printing it would produce answer sheets
            # that can't be graded reliably — say so with a failing exit code
            # rather than letting it look like a clean run.
            return 1

    if not args.no_open:
        _open_pdf(result["pdf_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
