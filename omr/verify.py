"""Round-trip a generated sheet: fill it in, grade it, check it came back.

Preflight proves a sheet is *printable and readable*. This proves the whole
loop actually closes on your own exam: it takes a sheet you generated,
simulates a student filling it in, grades the result through the real
grading path, and checks every answer came back as the one that went in.

That matters because the generator and the grader agree only through the
manifest. Preflight inspects one side of that contract; this exercises
both. If a custom config ever produced a layout the grader mis-reads, this
is what says so — before the sheets are printed rather than after they're
written on.

    python -m omr.verify data/exams/CS301_MIDSEM_2026A.manifest.json

It does not need a printer, a scanner, or a student.

Two things it deliberately does not prove by itself, so the green result
isn't read as more than it is:

*Complete manifest-vs-print agreement.* The reader now locally registers to
printed bubble outlines, so ordinary MCQ drift between a PDF and manifest is
caught. Preflight is still the full print contract check: it inspects marker
quiet zones, page identity marks, answer boxes, margins, and all generated
page geometry against the rendered PDF.

*Survival on real paper.* There's no perspective here, no lighting
gradient, no toner spread, no fold. The thresholds are calibrated against
clean renders and should be re-checked against real scans in Phase 2.
"""
from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .contracts import load_manifest
from .contracts.geometry import mm_to_px, px_per_mm
from .grading.mcq import MCQOutcome, grade_mcq_responses, read_mcq_responses

VERIFY_DPI = 200

# How a simulated student fills a bubble. Nobody colours one in perfectly,
# so the default is a firm but imperfect pen mark rather than a solid disc —
# verifying against a perfect fill would prove less than nothing.
DEFAULT_COVERAGE = 0.8
DEFAULT_DARKNESS = 25


@dataclass
class VerifyResult:
    exam_id: str
    num_pages: int
    total_questions: int
    recovered: int = 0
    mismatches: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    min_filled_ratio: float = 1.0
    max_empty_ratio: float = 0.0
    marks_awarded: float = 0.0
    marks_possible: float = 0.0
    filled_sheet_path: Path | None = None

    @property
    def separation(self) -> float:
        """Gap between the faintest deliberate fill and the darkest bubble
        left blank. This is the headroom the reader has to work with — the
        bigger it is, the more abuse a real scan can take."""
        return self.min_filled_ratio - self.max_empty_ratio

    @property
    def ok(self) -> bool:
        return self.recovered == self.total_questions and not self.mismatches

    def format(self) -> str:
        lines = [f"Round-trip verification of {self.exam_id} ({self.num_pages} page(s)):"]
        if self.total_questions == 0:
            lines.append("  no MCQs on this sheet - nothing to round-trip")
            return "\n".join(lines)

        lines.append(f"  answers recovered:  {self.recovered}/{self.total_questions}")
        lines.append(
            f"  marks:              {self.marks_awarded:g}/{self.marks_possible:g} "
            "(every simulated answer was the correct one)"
        )
        lines.append(
            f"  signal separation:  {self.separation:.3f} "
            f"(faintest fill {self.min_filled_ratio:.3f} vs darkest blank {self.max_empty_ratio:.3f})"
        )
        if self.flagged:
            lines.append(f"  flagged for review: {len(self.flagged)}")
            for f in self.flagged[:5]:
                lines.append(f"    - {f}")
        for m in self.mismatches[:10]:
            lines.append(f"  [ERROR] {m}")
        if self.filled_sheet_path:
            lines.append(f"  simulated sheet:    {self.filled_sheet_path}")

        if self.ok and self.separation > 0.4:
            lines.append("  Generator and grader agree on this sheet.")
        elif self.ok:
            lines.append(
                "  Answers round-trip, but the signal separation is tight - "
                "a poor scan may struggle."
            )
        else:
            lines.append("  MISMATCH: the grader did not read back what was filled in.")
        return "\n".join(lines)


def _render(pdf_path: Path, dpi: float):
    import pymupdf
    from PIL import Image

    images = {}
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=int(dpi), colorspace=pymupdf.csGRAY)
            images[i + 1] = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    return images


def _fill(draw, manifest, x_mm, y_mm, dpi, coverage, darkness):
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    r = manifest["bubble_sample_radius_mm"] * px_per_mm(dpi) * (coverage ** 0.5)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=darkness)


def verify_sheet(
    manifest_path: str | Path,
    pdf_path: str | Path | None = None,
    seed: int = 0,
    coverage: float = DEFAULT_COVERAGE,
    darkness: int = DEFAULT_DARKNESS,
    save_filled_to: str | Path | None = None,
    dpi: float = VERIFY_DPI,
) -> VerifyResult:
    from PIL import ImageDraw

    manifest_path = Path(manifest_path)
    manifest = load_manifest(manifest_path)
    if pdf_path is None:
        pdf_path = manifest_path.with_suffix("").with_suffix(".pdf")
        if not pdf_path.exists():
            pdf_path = manifest_path.parent / f"{manifest['exam_id']}.pdf"
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"could not find the PDF for this manifest (looked for {pdf_path})")

    images = _render(pdf_path, dpi)
    result = VerifyResult(
        exam_id=manifest["exam_id"],
        num_pages=manifest["num_pages"],
        total_questions=len(manifest["mcq_block"]),
    )
    if not manifest["mcq_block"]:
        return result

    rng = random.Random(seed)
    draws = {page: ImageDraw.Draw(img) for page, img in images.items()}

    # Fill one randomly chosen option per question, and treat that same
    # choice as the answer key: a correct round trip must recover every one.
    intended: dict[int, str] = {}
    for entry in manifest["mcq_block"]:
        page = entry.get("page", 1)
        option = rng.choice(entry["options"])
        intended[entry["q_no"]] = option
        i = entry["options"].index(option)
        x_mm = entry["x_mm"] + manifest["mcq_label_offset_mm"] + i * manifest["mcq_option_pitch_mm"]
        _fill(draws[page], manifest, x_mm, entry["y_mm"], dpi, coverage, darkness)

    if save_filled_to:
        save_filled_to = Path(save_filled_to)
        save_filled_to.parent.mkdir(parents=True, exist_ok=True)
        images[1].save(save_filled_to)
        result.filled_sheet_path = save_filled_to

    arrays = {page: np.array(img) for page, img in images.items()}
    readings = read_mcq_responses(arrays, manifest, dpi)
    marks_per_mcq = manifest["exam"].get("marks_per_mcq", 1)
    grades = grade_mcq_responses(readings, intended, marks_per_mcq)

    for reading in readings:
        want = intended[reading.q_no]
        for opt, ratio in reading.fill_ratios.items():
            if opt == want:
                result.min_filled_ratio = min(result.min_filled_ratio, ratio)
            else:
                result.max_empty_ratio = max(result.max_empty_ratio, ratio)

        if reading.outcome != MCQOutcome.ANSWERED or reading.selected_option != want:
            result.mismatches.append(
                f"Q{reading.q_no}: filled '{want}' but read {reading.outcome.value}"
                f"{' as ' + reading.selected_option if reading.selected_option else ''} "
                f"(ratios {', '.join(f'{k}={v:.2f}' for k, v in reading.fill_ratios.items())})"
            )
        else:
            result.recovered += 1
        if reading.needs_human_review:
            result.flagged.append(f"Q{reading.q_no}: {reading.review_reason}")

    result.marks_awarded = sum(g.marks_awarded for g in grades)
    result.marks_possible = marks_per_mcq * len(grades)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smartomr-verify",
        description=(
            "Fill in a generated OMR sheet, grade it, and check every answer came back. "
            "Proves the generator and grader agree on your exam's layout."
        ),
    )
    parser.add_argument("manifest", type=Path, help="Path to a generated <exam_id>.manifest.json")
    parser.add_argument("--pdf", type=Path, default=None, help="PDF path, if not beside the manifest")
    parser.add_argument("--seed", type=int, default=0, help="Seed for the simulated answers")
    parser.add_argument(
        "--coverage",
        type=float,
        default=DEFAULT_COVERAGE,
        help=f"How much of the bubble a simulated pen covers, 0-1 (default {DEFAULT_COVERAGE})",
    )
    parser.add_argument(
        "--darkness",
        type=int,
        default=DEFAULT_DARKNESS,
        help=f"Grey level of the simulated mark, 0=black (default {DEFAULT_DARKNESS})",
    )
    parser.add_argument(
        "--save-filled",
        type=Path,
        default=None,
        metavar="PNG",
        help="Write page 1 of the simulated filled-in sheet here, to look at",
    )
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        print(f"Manifest not found: {args.manifest}", file=sys.stderr)
        return 2

    try:
        result = verify_sheet(
            args.manifest,
            pdf_path=args.pdf,
            seed=args.seed,
            coverage=args.coverage,
            darkness=args.darkness,
            save_filled_to=args.save_filled,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(result.format())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
