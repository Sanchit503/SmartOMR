import hashlib
import itertools

import pytest

from omr.datasets.split_roll_digit_holdout import split_rows


def rows():
    return [{"writer_id": f"writer_{writer}", "label": str(writer % 10),
             "crop_sha256": hashlib.sha256(f"{writer}_{crop}".encode()).hexdigest()}
            for writer in range(20) for crop in range(7)]


def test_split_is_reproducible_and_keeps_whole_writers():
    values = rows()
    result = split_rows(values, test_target=28, validation_target=14)
    assert result == split_rows(values, test_target=28, validation_target=14)
    assert {name: len(items) for name, items in result.items()} == {"train": 98, "val": 14, "test": 28}
    assert sum(map(len, result.values())) == len(values)
    for first, second in itertools.combinations(result, 2):
        assert not {row["writer_id"] for row in result[first]} & {row["writer_id"] for row in result[second]}
        assert not {row["crop_sha256"] for row in result[first]} & {row["crop_sha256"] for row in result[second]}


def test_duplicate_crops_link_writer_groups():
    values = rows()
    values[7]["crop_sha256"] = values[0]["crop_sha256"]
    result = split_rows(values, test_target=28, validation_target=14)
    assignment = {row["writer_id"]: name for name, items in result.items() for row in items}
    assert assignment["writer_0"] == assignment["writer_1"]


@pytest.mark.parametrize("test,validation", [(0, 10), (10, 0), (100, 40)])
def test_invalid_targets_rejected(test, validation):
    with pytest.raises(ValueError):
        split_rows(rows(), test_target=test, validation_target=validation)


def test_exact_count_is_not_forced_by_splitting_a_writer():
    result = split_rows(rows(), test_target=29, validation_target=13)
    assert len(result["test"]) == 28 and len(result["val"]) == 14
