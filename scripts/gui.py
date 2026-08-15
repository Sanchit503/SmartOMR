"""Desktop GUI for generating OMR sheets — fill in a form, click Generate.

No terminal typing required. Launch it by double-clicking
`Run OMR Generator.bat` in the project root (which runs this windowless via
pythonw.exe), or directly with:

    python scripts/gui.py

Wraps the exact same `generate_exam()` pipeline used by the CLI scripts —
this is just a friendlier front end over `omr/generator/`.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from tkinter import (
    Button,
    Entry,
    Frame,
    Label,
    OptionMenu,
    StringVar,
    Tk,
    filedialog,
    messagebox,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from omr.generator.config import ExamConfig, WrittenQuestionConfig  # noqa: E402
from omr.generator.generate import generate_exam  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "exams"


class WrittenQuestionRow(Frame):
    def __init__(self, parent, remove_callback):
        super().__init__(parent)
        self.label = Label(self, text="", width=12, anchor="w")
        self.label.grid(row=0, column=0, padx=(0, 8))
        Label(self, text="Max marks").grid(row=0, column=1)
        self.marks_var = StringVar(value="5")
        Entry(self, textvariable=self.marks_var, width=6).grid(row=0, column=2, padx=(4, 12))
        Label(self, text="Lines").grid(row=0, column=3)
        self.lines_var = StringVar(value="2")
        Entry(self, textvariable=self.lines_var, width=6).grid(row=0, column=4, padx=4)
        Button(self, text="Remove", command=lambda: remove_callback(self)).grid(row=0, column=5, padx=12)

    def set_label(self, q_no: int) -> None:
        self.label.config(text=f"Written Q{q_no}")

    def values(self) -> tuple[str, str]:
        return self.marks_var.get(), self.lines_var.get()


class OMRGeneratorApp:
    def __init__(self, root: Tk):
        self.root = root
        root.title("OMR Sheet Generator")
        root.geometry("620x640")
        root.minsize(560, 480)

        self.written_rows: list[WrittenQuestionRow] = []

        form = Frame(root, padx=14, pady=12)
        form.pack(fill="x")

        self.exam_id = self._field(form, 0, "Exam ID", "CS301_MIDSEM_2026A")
        self.course_code = self._field(form, 1, "Course Code", "CS301")
        self.exam_name = self._field(form, 2, "Exam Name", "Mid-Semester Examination")

        Label(form, text="Exam Type").grid(row=3, column=0, sticky="w", pady=4)
        self.exam_type = StringVar(value="quiz")
        OptionMenu(form, self.exam_type, "quiz", "midsem", "endsem").grid(row=3, column=1, sticky="w")

        self.num_mcq = self._field(form, 4, "Number of MCQs", "20")
        self.mcq_options = self._field(form, 5, "Options per MCQ (2-6)", "4")
        self.marks_per_mcq = self._field(form, 6, "Marks per MCQ", "1")

        section_label = Frame(root, padx=14)
        section_label.pack(fill="x")
        Label(section_label, text="Written Questions", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        self.wq_container = Frame(root, padx=14)
        self.wq_container.pack(fill="both", expand=True)

        add_row_frame = Frame(root, padx=14, pady=6)
        add_row_frame.pack(fill="x")
        Button(add_row_frame, text="+ Add Written Question", command=self.add_written_row).pack(side="left")

        out_frame = Frame(root, padx=14, pady=6)
        out_frame.pack(fill="x")
        Label(out_frame, text="Output folder").pack(side="left")
        self.output_dir = StringVar(value=str(DEFAULT_OUTPUT_DIR))
        Entry(out_frame, textvariable=self.output_dir, width=44).pack(side="left", padx=6)
        Button(out_frame, text="Browse...", command=self.browse_output).pack(side="left")

        generate_frame = Frame(root, padx=14, pady=14)
        generate_frame.pack(fill="x")
        Button(
            generate_frame,
            text="Generate OMR Sheet",
            command=self.generate,
            bg="#2563eb",
            fg="white",
            activebackground="#1d4ed8",
            activeforeground="white",
            font=("Segoe UI", 11, "bold"),
            height=2,
        ).pack(fill="x")

        self.status = Label(root, text="", padx=14, anchor="w", justify="left", wraplength=580)
        self.status.pack(fill="x")

    def _field(self, parent: Frame, row: int, label: str, default: str) -> StringVar:
        Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        var = StringVar(value=default)
        Entry(parent, textvariable=var, width=40).grid(row=row, column=1, sticky="w", pady=4)
        return var

    def add_written_row(self) -> None:
        row = WrittenQuestionRow(self.wq_container, self.remove_written_row)
        row.pack(fill="x", pady=2, anchor="w")
        self.written_rows.append(row)
        self._renumber_rows()

    def remove_written_row(self, row: WrittenQuestionRow) -> None:
        row.destroy()
        self.written_rows.remove(row)
        self._renumber_rows()

    def _renumber_rows(self) -> None:
        num_mcq = self._safe_int(self.num_mcq.get())
        for i, row in enumerate(self.written_rows):
            row.set_label(num_mcq + i + 1)

    @staticmethod
    def _safe_int(raw: str, default: int = 0) -> int:
        try:
            return int(raw)
        except ValueError:
            return default

    def browse_output(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_dir.get() or str(DEFAULT_OUTPUT_DIR))
        if chosen:
            self.output_dir.set(chosen)

    def generate(self) -> None:
        try:
            num_mcq = int(self.num_mcq.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Number of MCQs must be a whole number.")
            return

        written_questions = []
        try:
            for i, row in enumerate(self.written_rows):
                marks_str, lines_str = row.values()
                written_questions.append(
                    WrittenQuestionConfig(q_no=num_mcq + i + 1, max_marks=float(marks_str), lines=int(lines_str))
                )
        except (ValueError, ValidationError) as exc:
            messagebox.showerror("Invalid written question", str(exc))
            return

        try:
            config = ExamConfig(
                exam_id=self.exam_id.get().strip(),
                course_code=self.course_code.get().strip(),
                exam_name=self.exam_name.get().strip(),
                exam_type=self.exam_type.get(),
                num_mcq=num_mcq,
                mcq_options=int(self.mcq_options.get()),
                marks_per_mcq=float(self.marks_per_mcq.get()),
                written_questions=written_questions,
            )
        except (ValueError, ValidationError) as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        try:
            result = generate_exam(config, Path(self.output_dir.get()))
        except ValueError as exc:
            messagebox.showerror("Layout error", str(exc))
            return
        except Exception:
            messagebox.showerror("Unexpected error", traceback.format_exc())
            return

        num_pages = result["manifest"]["num_pages"]
        self.status.config(text=f"Generated ({num_pages} page{'s' if num_pages != 1 else ''}): {result['pdf_path']}")
        messagebox.showinfo(
            "Done",
            f"Generated {num_pages} page(s).\n\nPDF: {result['pdf_path']}\nManifest: {result['manifest_path']}",
        )
        if sys.platform == "win32":
            try:
                os.startfile(result["pdf_path"])
            except OSError:
                pass


def main() -> None:
    root = Tk()
    OMRGeneratorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
