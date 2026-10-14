"""Publish digit-only crops without roll numbers, scan paths or cell order."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import shutil

from PIL import Image


FIELDS = ['image_path', 'label', 'writer_id', 'crop_sha256']


def _rows(path):
    with path.open(newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_public_dataset(source: Path, output: Path, *, local_fit: Path | None = None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('Use a new output directory; existing datasets are not overwritten')
    key = secrets.token_bytes(32)
    catalog, images = {}, {}
    for row in _rows(source / 'labels.csv'):
        path = (source / row['image_path'].replace('\\', '/')).resolve()
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError('Crop is missing or outside the source dataset')
        digest = _hash(path)
        if digest != row['crop_sha256']:
            raise ValueError('Crop hash mismatch')
        if row['label'] not in ['blank', *map(str, range(10))] or not row.get('writer_id'):
            raise ValueError('Each crop needs a supported label and writer group')
        with Image.open(path) as image:
            if max(image.size) > 256 or image.format != 'PNG' or image.text:
                raise ValueError('Only small PNG cells without embedded text can be published')
        public = dict(image_path=f'cells/{digest}.png', label=row['label'],
                      writer_id=hmac.new(key, row['writer_id'].encode(), hashlib.sha256).hexdigest(),
                      crop_sha256=digest)
        if digest in catalog and catalog[digest] != public:
            raise ValueError('Conflicting labels or writers for a duplicate crop')
        catalog[digest], images[digest] = public, path
    exports = {'labels': list(catalog.values())}
    inputs = {'labels': source / 'labels.csv'}
    for name in ('train', 'val', 'test', 'local_fit_train', 'local_fit_val'):
        if name.startswith('local_fit_'):
            if local_fit is None:
                continue
            path = Path(local_fit) / (name.removeprefix('local_fit_') + '.csv')
        else:
            path = source / (name + '.csv')
        rows = _rows(path)
        public_rows = []
        for row in rows:
            public = catalog.get(row['crop_sha256'])
            if public is None or row['label'] != public['label']:
                raise ValueError('Split contains an unknown or conflicting crop')
            public_rows.append(public)
        exports[name], inputs[name] = public_rows, path
    for names in (('train', 'val', 'test'), ('local_fit_train', 'local_fit_val')):
        names = [name for name in names if name in exports]
        if not names:
            continue
        seen_writers, seen_hashes = set(), set()
        for name in names:
            writers = {row['writer_id'] for row in exports[name]}
            hashes = {row['crop_sha256'] for row in exports[name]}
            if writers & seen_writers or hashes & seen_hashes:
                raise ValueError('Writer or duplicate-crop leakage across split boundaries')
            if len(hashes) != len(exports[name]):
                raise ValueError('Duplicate crop inside a split')
            seen_writers |= writers
            seen_hashes |= hashes
        if seen_hashes != set(catalog):
            raise ValueError('Splits do not cover the complete dataset')
    (output / 'cells').mkdir(parents=True)
    for digest, path in images.items():
        shutil.copyfile(path, output / 'cells' / f'{digest}.png')
    for name, rows in exports.items():
        # Neutral hash order removes the original per-student cell sequence.
        with (output / f'{name}.csv').open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda row: row['crop_sha256']))
    report = dict(schema_version=1, counts={name: len(rows) for name, rows in exports.items()},
                  writer_counts={name: len({r['writer_id'] for r in rows}) for name, rows in exports.items()},
                  class_counts={name: dict(Counter(r['label'] for r in rows)) for name, rows in exports.items()},
                  source_csv_sha256={name + '.csv': _hash(path) for name, path in inputs.items()},
                  csv_sha256={name + '.csv': _hash(output / f'{name}.csv') for name in exports},
                  privacy='Digit cells only; no roll numbers, roster, scan paths, cell positions or writer lookup key. '
                          'Writer IDs are pseudonymous, not a guarantee of anonymity. Labels and pixels are unchanged.',
                  split_note='Colab train/val/test are writer-disjoint. Local-fit train/val reflect earlier training; '
                             'the local models have exposure to much of the Colab test set. Not a fair shared holdout.')
    (output / 'dataset_manifest.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--local-fit', type=Path)
    args = parser.parse_args()
    print(json.dumps(export_public_dataset(args.source, args.output, local_fit=args.local_fit)['counts']))


if __name__ == '__main__':
    main()
