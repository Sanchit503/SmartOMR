import csv
import hashlib
import json

from PIL import Image
import pytest

from omr.datasets.export_public_dataset import FIELDS, export_public_dataset


def fixture_dataset(tmp_path):
    source = tmp_path / 'private'
    source.mkdir()
    rows = []
    for i in range(3):
        path = source / f'student-secret-{i}.png'
        Image.new('L', (32, 32), 100 + i).save(path)
        rows.append(dict(image_path=path.name, label=str(i), writer_id=f'private-roll-{i}',
                         crop_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                         roll_no=f'private-roll-{i}', source_image_path='private-scan', cell_index='1'))
    def write(name, selected):
        with (source / name).open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(selected)
    write('labels.csv', rows)
    for name, row in zip(('train', 'val', 'test'), rows):
        write(name + '.csv', [row])
    return source, rows, write


def test_public_export_preserves_pixels_labels_splits_but_removes_identifiers(tmp_path):
    source, rows, _ = fixture_dataset(tmp_path)
    output = tmp_path / 'public'
    report = export_public_dataset(source, output)
    assert report['counts'] == dict(labels=3, train=1, val=1, test=1)
    for path in output.glob('*.csv'):
        text = path.read_text()
        assert 'private' not in text and 'roll_no' not in text and 'cell_index' not in text
        with path.open(newline='') as handle:
            reader = csv.DictReader(handle)
            assert reader.fieldnames == FIELDS
            for row in reader:
                assert hashlib.sha256((output / row['image_path']).read_bytes()).hexdigest() == row['crop_sha256']
                assert len(row['writer_id']) == 64
    assert json.loads((output / 'dataset_manifest.json').read_text())['counts'] == report['counts']
    with pytest.raises(FileExistsError):
        export_public_dataset(source, output)


def test_public_export_rejects_writer_leakage_without_creating_output(tmp_path):
    source, rows, write = fixture_dataset(tmp_path)
    rows[1]['writer_id'] = rows[0]['writer_id']
    write('labels.csv', rows)
    write('val.csv', [rows[1]])
    output = tmp_path / 'public'
    with pytest.raises(ValueError, match='leakage'):
        export_public_dataset(source, output)
    assert not output.exists()


def test_public_export_rejects_wrong_crop_hash(tmp_path):
    source, rows, write = fixture_dataset(tmp_path)
    rows[0]['crop_sha256'] = 'wrong'
    write('labels.csv', rows)
    with pytest.raises(ValueError, match='hash mismatch'):
        export_public_dataset(source, tmp_path / 'public')
