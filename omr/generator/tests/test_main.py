"""Covers the entry point itself — the thing a user actually runs.

Every mode has to land in the same `generate_exam()` call, so a sheet made
through the wizard is identical to the same sheet made from a config file.
That equivalence is what `test_wizard_and_config_file_produce_identical_sheets`
pins down; the rest are the exit paths.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from omr.contracts import load_manifest
from omr.generator.main import main, prompt_for_config

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


def feed(monkeypatch, answers: list[str]) -> None:
    """Answer the wizard's prompts in order; '' means 'accept the default'."""
    remaining = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(remaining))


def test_config_mode_writes_pdf_and_manifest(tmp_path):
    code = main(
        [
            "--config",
            str(CONFIG_DIR / "midsem_cs301.json"),
            "--output-dir",
            str(tmp_path),
            "--no-open",
        ]
    )
    assert code == 0
    manifest = load_manifest(tmp_path / "CS301_MIDSEM_2026A.manifest.json")
    assert (tmp_path / "CS301_MIDSEM_2026A.pdf").stat().st_size > 0
    assert len(manifest["mcq_block"]) == 20
    assert len(manifest["written_block"]) == 3


@pytest.mark.parametrize("config_name", [p.name for p in sorted(CONFIG_DIR.glob("*.json"))])
def test_every_example_config_is_valid_input(config_name, tmp_path):
    """The example configs are the documented starting point — a broken one
    is a broken tutorial. `example_impossible_question` is deliberately
    unplaceable and must fail loudly rather than produce a wrong sheet."""
    expected = 1 if config_name == "example_impossible_question.json" else 0
    code = main(["--config", str(CONFIG_DIR / config_name), "--output-dir", str(tmp_path), "--no-open"])
    assert code == expected


def test_wizard_asks_once_when_written_questions_are_uniform(monkeypatch):
    feed(
        monkeypatch,
        [
            "CS301",                      # course code
            "Mid-Semester Examination",   # exam name
            "midsem",                     # exam type
            "CS301_MIDSEM_2026A",         # exam id
            "20",                         # how many MCQs
            "4",                          # options per MCQ
            "1",                          # marks per MCQ
            "3",                          # how many written questions
            "y",                          # all the same?
            "5",                          # marks for each
            "2",                          # lines for each
        ],
    )
    config = prompt_for_config()
    assert config.num_mcq == 20
    assert [(w.q_no, w.max_marks, w.lines) for w in config.written_questions] == [
        (21, 5, 2),
        (22, 5, 2),
        (23, 5, 2),
    ]


def test_wizard_asks_per_question_when_they_differ(monkeypatch):
    feed(
        monkeypatch,
        [
            "CS301", "Mid-Semester Examination", "midsem", "CS301_MIDSEM_2026A",
            "20", "4", "1",
            "3",                          # how many written questions
            "n",                          # they differ
            "5", "2",                     # Q21
            "5", "2",                     # Q22
            "10", "4",                    # Q23
        ],
    )
    config = prompt_for_config()
    assert [(w.q_no, w.max_marks, w.lines) for w in config.written_questions] == [
        (21, 5, 2),
        (22, 5, 2),
        (23, 10, 4),
    ]


def test_wizard_and_config_file_produce_identical_sheets(monkeypatch, tmp_path):
    from_config = tmp_path / "from_config"
    from_wizard = tmp_path / "from_wizard"

    main(["--config", str(CONFIG_DIR / "midsem_cs301.json"), "--output-dir", str(from_config), "--no-open"])

    feed(
        monkeypatch,
        [
            "CS301", "Mid-Semester Examination", "midsem", "CS301_MIDSEM_2026A",
            "20", "4", "1",
            "3", "n", "5", "2", "5", "2", "10", "4",
        ],
    )
    main(["--output-dir", str(from_wizard), "--no-open"])

    name = "CS301_MIDSEM_2026A.manifest.json"
    assert json.loads((from_config / name).read_text()) == json.loads((from_wizard / name).read_text())


def test_defaults_keep_a_bare_mcq_only_quiz_to_one_page(monkeypatch, tmp_path):
    feed(
        monkeypatch,
        [
            "CS201", "Quiz 3", "",   # exam type defaults to quiz
            "",                       # exam id defaults to CS201_QUIZ
            "10", "", "2",            # 10 MCQs, default 4 options, 2 marks each
            "0",                      # no written questions
        ],
    )
    config = prompt_for_config()
    assert config.exam_id == "CS201_QUIZ"
    assert config.exam_type == "quiz"
    assert config.mcq_options == 4
    assert config.written_questions == []


def test_missing_config_file_exits_without_writing_anything(tmp_path):
    assert main(["--config", str(tmp_path / "nope.json"), "--output-dir", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []


def test_gui_and_config_are_mutually_exclusive(tmp_path):
    assert main(["--gui", "--config", str(CONFIG_DIR / "quiz_short.json")]) == 2


def test_unplaceable_layout_is_a_hard_stop_not_a_guess(tmp_path):
    """Section 2, principle 4 — the generator refuses rather than silently
    producing a sheet with a question it couldn't fit."""
    code = main(
        [
            "--config",
            str(CONFIG_DIR / "example_impossible_question.json"),
            "--output-dir",
            str(tmp_path),
            "--no-open",
        ]
    )
    assert code == 1
    assert not list(tmp_path.glob("*.pdf"))
