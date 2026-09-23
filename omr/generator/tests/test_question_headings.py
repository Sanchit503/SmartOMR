"""Printed question headings and instructions follow the actual exam content."""
from __future__ import annotations

from itertools import product

import pymupdf
import pytest

from omr.generator.config import ExamConfig, NumericalQuestionConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam


def config_for(mcqs=0, numerical=(), written=0):
    return ExamConfig(
        exam_id="QUESTION_HEADINGS", course_code="TEST", exam_name="Question headings",
        exam_type="quiz", num_mcq=mcqs,
        numerical_questions=[
            NumericalQuestionConfig(q_no=mcqs + i, max_marks=1, digits=digits)
            for i, digits in enumerate(numerical, 1)
        ],
        written_questions=[
            WrittenQuestionConfig(q_no=mcqs + len(numerical) + i, max_marks=2, lines=2)
            for i in range(1, written + 1)
        ],
    )


@pytest.mark.parametrize("mcqs,numerical,written", [p for p in product((0, 1), repeat=3) if any(p)])
def test_section_letters_appear_only_for_mixed_question_types(tmp_path, mcqs, numerical, written):
    result = generate_exam(config_for(mcqs, [2] * numerical, written), tmp_path)
    titles = [
        title for present, title in (
            (mcqs, "Multiple Choice"), (numerical, "Numerical Answers"), (written, "Written Answers"),
        ) if present
    ]
    with pymupdf.open(result["pdf_path"]) as document:
        text = "\n".join(page.get_text() for page in document)
    if len(titles) == 1:
        assert titles[0] in text
        assert "Section " not in text
    else:
        headings = [f"Section {chr(ord('A') + i)} - {title}" for i, title in enumerate(titles)]
        assert all(heading in text for heading in headings)
        assert [text.index(heading) for heading in headings] == sorted(text.index(heading) for heading in headings)


@pytest.mark.parametrize("config,title", [
    (config_for(mcqs=150), "Multiple Choice"),
    (config_for(numerical=[2] * 9), "Numerical Answers"),
    (config_for(written=15), "Written Answers"),
])
def test_single_type_continuation_heading_has_no_section_letter(tmp_path, config, title):
    result = generate_exam(config, tmp_path)
    with pymupdf.open(result["pdf_path"]) as document:
        assert len(document) > 1
        for page_no, page in enumerate(document):
            text = page.get_text()
            assert "Section " not in text
            expected = title if page_no == 0 else f"{title}  (continued)"
            assert expected in text


def test_mixed_exam_keeps_section_letters_on_pages_with_only_one_type(tmp_path):
    result = generate_exam(config_for(mcqs=1, numerical=[2] * 17, written=1), tmp_path)
    entries = result["manifest"]["numerical_block"]
    with pymupdf.open(result["pdf_path"]) as document:
        for page_no in sorted({entry["page"] for entry in entries}):
            assert "Section B - Numerical Answers" in document[page_no - 1].get_text()
        assert "Section C - Written Answers" in document[-1].get_text()


@pytest.mark.parametrize("digits", range(1, 9))
def test_leading_zero_example_matches_configured_digits(tmp_path, digits):
    result = generate_exam(config_for(numerical=[digits]), tmp_path)
    with pymupdf.open(result["pdf_path"]) as document:
        page = document[0]
        text = page.get_text()
        if digits == 1:
            assert "leading zeros" not in text
            assert "7 as" not in text
        else:
            example = f"7 as {7:0{digits}d} in a {digits}-digit grid"
            assert example in text
        instructions = [word for word in page.get_text("words") if word[4] == "grid)."]
        assert all(word[2] < page.rect.width - 12 * 72 / 25.4 for word in instructions)


def test_examples_follow_each_page_and_name_the_width_for_mixed_grids(tmp_path):
    result = generate_exam(config_for(numerical=[2] * 8 + [3, 2]), tmp_path)
    with pymupdf.open(result["pdf_path"]) as document:
        assert len(document) == 2
        assert "7 as 07 in a 2-digit grid" in document[0].get_text()
        assert "007" not in document[0].get_text()
        assert "7 as 007 in a 3-digit grid" in document[1].get_text()
