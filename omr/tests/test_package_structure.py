from __future__ import annotations

import inspect

from omr.io.csv import load_students
from omr.io import load_written_question_metadata
from omr.grading import WrittenGradeRequest, build_written_grader
from omr.reader.identity import read_roll_number
from omr.reader.scan import ScanError
from omr.workflows.evaluate import batch_evaluate, evaluate_scan
from omr.workflows.parse import parse_scan, parse_scans
from omr.workflows.review import initialize_verification_index, verify_student
from omr.workflows.written import auto_grade_written_answers, export_written_grading_packet, import_written_marks


def test_promoted_scan_evaluation_modules_are_importable_from_omr():
    assert callable(load_students)
    assert callable(load_written_question_metadata)
    assert callable(build_written_grader)
    assert WrittenGradeRequest.__name__ == "WrittenGradeRequest"
    assert callable(read_roll_number)
    assert issubclass(ScanError, RuntimeError)
    assert callable(evaluate_scan)
    assert callable(batch_evaluate)
    assert callable(parse_scan)
    assert callable(parse_scans)
    assert callable(initialize_verification_index)
    assert callable(verify_student)
    assert callable(auto_grade_written_answers)
    assert callable(export_written_grading_packet)
    assert callable(import_written_marks)


def test_prototype_eval_is_only_a_compatibility_path_now():
    from prototype_eval.csv_io import load_students as compat_load_students
    from prototype_eval.cv_scan import ScanError as compat_scan_error
    from prototype_eval.pipeline import evaluate_scan as compat_evaluate_scan
    from prototype_eval.roll_reader import read_roll_number as compat_read_roll_number

    assert compat_load_students is load_students
    assert compat_scan_error is ScanError
    assert compat_evaluate_scan is evaluate_scan
    assert compat_read_roll_number is read_roll_number


def test_new_and_compat_workflows_have_intentional_default_output_roots():
    import prototype_eval.pipeline as compat_pipeline

    assert inspect.signature(batch_evaluate).parameters["output_root"].default == "data/evaluations"
    assert (
        inspect.signature(compat_pipeline.batch_evaluate).parameters["output_root"].default
        == "prototype_eval/data"
    )
