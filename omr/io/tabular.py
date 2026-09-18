"""Small dependency-free readers for CSV and XLSX tables."""
from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree


_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _column_index(reference: str) -> int:
    letters = "".join(char for char in reference if char.isalpha()).upper()
    total = 0
    for char in letters:
        total = total * 26 + ord(char) - ord("A") + 1
    return max(total - 1, 0)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read(path))
    namespace = {"a": _SHEET_NS}
    return [
        "".join(node.text or "" for node in item.findall(".//a:t", namespace))
        for item in root.findall("a:si", namespace)
    ]


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    namespace = {"a": _SHEET_NS, "r": _DOC_REL_NS, "rel": _REL_NS}
    sheet = workbook.find("a:sheets/a:sheet", namespace)
    if sheet is None:
        raise ValueError("XLSX file has no worksheets")
    relationship_id = sheet.attrib.get(f"{{{_DOC_REL_NS}}}id")
    for relationship in relationships.findall("rel:Relationship", namespace):
        if relationship.attrib.get("Id") == relationship_id:
            target = relationship.attrib.get("Target", "")
            if target.startswith("/"):
                return target.lstrip("/")
            return "xl/" + target.lstrip("/")
    raise ValueError("XLSX first worksheet relationship was not found")


def _cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value = cell.find(f"{{{_SHEET_NS}}}v")
    if value is None or value.text is None:
        inline = cell.find(f".//{{{_SHEET_NS}}}t")
        return inline.text if inline is not None and inline.text else ""
    raw = value.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (IndexError, ValueError):
            raise ValueError(f"XLSX contains an invalid shared-string index: {raw!r}") from None
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw


def _xlsx_rows(path: Path) -> list[list[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            shared_strings = _shared_strings(archive)
            sheet_path = _first_sheet_path(archive)
            root = ElementTree.fromstring(archive.read(sheet_path))
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise ValueError(f"invalid XLSX file {path}: {exc}") from exc

    namespace = {"a": _SHEET_NS}
    rows: list[list[str]] = []
    for row in root.findall(".//a:sheetData/a:row", namespace):
        values: dict[int, str] = {}
        for cell in row.findall("a:c", namespace):
            index = _column_index(cell.attrib.get("r", ""))
            values[index] = _cell_text(cell, shared_strings).strip()
        if values:
            rows.append([values.get(index, "") for index in range(max(values) + 1)])
    return rows


def _csv_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [[str(value).strip() for value in row] for row in csv.reader(handle)]


def read_tabular_rows(path: str | Path) -> list[dict[str, str]]:
    """Read the first table in a CSV or XLSX file as dictionaries.

    Leading blank rows are ignored. The first nonblank row is the header.
    Empty trailing cells are harmless, while duplicate nonblank headers are
    rejected because they make marks mapping ambiguous.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"table not found: {source}")
    suffix = source.suffix.lower()
    if suffix == ".csv":
        raw_rows = _csv_rows(source)
    elif suffix == ".xlsx":
        raw_rows = _xlsx_rows(source)
    else:
        raise ValueError(f"unsupported table type {source.suffix!r}; use .csv or .xlsx")

    nonblank = [row for row in raw_rows if any(str(value).strip() for value in row)]
    if not nonblank:
        raise ValueError(f"{source} contains no rows")
    headers = [str(value).strip() for value in nonblank[0]]
    meaningful = [header for header in headers if header]
    if not meaningful:
        raise ValueError(f"{source} has no header row")
    canonical = [re.sub(r"[^a-z0-9]+", "", header.lower()) for header in meaningful]
    if len(canonical) != len(set(canonical)):
        raise ValueError(f"{source} contains duplicate column headers")

    rows: list[dict[str, str]] = []
    for values in nonblank[1:]:
        row = {
            header: str(values[index]).strip() if index < len(values) else ""
            for index, header in enumerate(headers)
            if header
        }
        if any(row.values()):
            rows.append(row)
    if not rows:
        raise ValueError(f"{source} contains no data rows")
    return rows
