"""Write a trace report to CSV or Excel, including appending to a workbook that already exists."""

from __future__ import annotations

import csv
import os
import zipfile
from xml.etree import ElementTree as ET

COLUMNS = (
    "Device",
    "Pole",
    "Cable",
    "Cable type",
    "Length",
    "Ducts",
    "Handholes",
    "IDF",
    "Panel",
    "Port",
    "Strands",
    "Segments",
    "Enclosures",
    "Warnings",
)

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def report_record(report: dict) -> dict:
    """One spreadsheet row. The keys are the column titles and never change."""
    return {
        "Device": report.get("device_id") or "",
        "Pole": report.get("pole_id") or "",
        "Cable": report.get("cable_id") or "",
        "Cable type": report.get("cable_type") or "",
        "Length": report.get("length") or "",
        "Ducts": " > ".join(report.get("ducts") or []),
        "Handholes": " > ".join(report.get("handholes") or []),
        "IDF": report.get("idf_id") or "",
        "Panel": report.get("panel_id") or "",
        "Port": report.get("panel_port") or "",
        "Strands": " | ".join(report.get("strand_lines") or []),
        "Segments": " > ".join(report.get("segments") or []),
        "Enclosures": ", ".join(report.get("enclosures") or []),
        "Warnings": " | ".join(report.get("warnings") or []),
    }


def item_label(report: dict, index: int) -> str:
    record = report_record(report)
    device = record["Device"]
    cable = record["Cable"]
    if device and cable:
        return f"{device} - {cable}"
    return device or cable or record["IDF"] or f"Row {index + 1}"


def table_from_reports(reports: list[dict], fields: list[str]) -> tuple[list[str], list[list[str]]]:
    headers = [name for name in COLUMNS if name in fields]
    rows = []
    for report in reports:
        record = report_record(report)
        rows.append([record.get(name, "") for name in headers])
    return headers, rows


def export_reports(path: str, reports: list[dict], fields: list[str], append: bool) -> int:
    headers, rows = table_from_reports(reports, fields)
    if not headers:
        raise ValueError("Select at least one field.")
    if not rows:
        raise ValueError("Select at least one item.")
    kind = _kind(path)
    if append:
        if not os.path.exists(path):
            raise ValueError(f"{path} does not exist.")
        old_headers, old_rows = read_table(path)
        headers, rows = merge_tables(old_headers, old_rows, headers, rows)
    if kind == "csv":
        _write_csv(path, headers, rows)
    else:
        _write_xlsx(path, headers, rows)
    if append:
        return len(rows) - len(old_rows)
    return len(rows)


def read_table(path: str) -> tuple[list[str], list[list[str]]]:
    kind = _kind(path)
    if kind == "csv":
        return _read_csv(path)
    return _read_xlsx(path)


def merge_tables(
    old_headers: list[str],
    old_rows: list[list[str]],
    new_headers: list[str],
    new_rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    """Keep every column already in the file, then add any selected column that is new."""
    if not old_headers:
        return list(new_headers), [list(row) for row in new_rows]
    headers = list(old_headers)
    for name in new_headers:
        if name not in headers:
            headers.append(name)

    def align(source_headers: list[str], source_row: list[str]) -> list[str]:
        lookup = {}
        for index, name in enumerate(source_headers):
            lookup[name] = source_row[index] if index < len(source_row) else ""
        return [lookup.get(name, "") for name in headers]

    merged = [align(old_headers, row) for row in old_rows]
    merged.extend(align(new_headers, row) for row in new_rows)
    return headers, merged


def _kind(path: str) -> str:
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".csv":
        return "csv"
    if suffix == ".xlsx":
        return "xlsx"
    raise ValueError("Use a .csv or .xlsx file.")


def _write_csv(path: str, headers: list[str], rows: list[list[str]]) -> None:
    # utf-8-sig lets Excel on Windows read the IDs without treating the file as ANSI.
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)


def _read_csv(path: str) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        grid = list(csv.reader(handle))
    if not grid:
        return [], []
    headers = [cell.strip() for cell in grid[0]]
    return headers, [[cell for cell in row] for row in grid[1:] if any(cell.strip() for cell in row)]


def _write_xlsx(path: str, headers: list[str], rows: list[list[str]]) -> None:
    grid = [headers, *rows]
    sheet_rows = []
    for row_number, row in enumerate(grid, start=1):
        cells = []
        for column, value in enumerate(row):
            ref = f"{_column_letter(column)}{row_number}"
            cells.append(
                f'<c r="{ref}" t="inlineStr"><is><t>{_xml_text(value)}</t></is></c>'
            )
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_MAIN}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_MAIN}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="OSP Report" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def _read_xlsx(path: str) -> tuple[list[str], list[list[str]]]:
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        sheet_name = _first_sheet_name(archive)
        sheet = ET.fromstring(archive.read(sheet_name))
    grid = []
    for row in sheet.iter():
        if _local(row.tag) != "row":
            continue
        cells = {}
        next_index = 0
        for cell in list(row):
            if _local(cell.tag) != "c":
                continue
            ref = cell.attrib.get("r", "")
            index = _column_index(ref) if ref else next_index
            cells[index] = _cell_text(cell, shared)
            next_index = index + 1
        if not cells:
            continue
        width = max(cells) + 1
        grid.append([cells.get(index, "") for index in range(width)])
    if not grid:
        return [], []
    width = max(len(row) for row in grid)
    headers = []
    for index in range(width):
        value = grid[0][index].strip() if index < len(grid[0]) else ""
        headers.append(value or f"Column {index + 1}")
    rows = []
    for row in grid[1:]:
        padded = [row[index] if index < len(row) else "" for index in range(width)]
        if any(cell.strip() for cell in padded):
            rows.append(padded)
    return headers, rows


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(name))
    values = []
    for item in root:
        if _local(item.tag) != "si":
            continue
        values.append("".join(node.text or "" for node in item.iter() if _local(node.tag) == "t"))
    return values


def _first_sheet_name(archive: zipfile.ZipFile) -> str:
    names = archive.namelist()
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relation_id = ""
    for sheet in workbook.iter():
        if _local(sheet.tag) == "sheet":
            relation_id = sheet.attrib.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                "",
            )
            break
    target = ""
    rels_name = "xl/_rels/workbook.xml.rels"
    if relation_id and rels_name in names:
        rels = ET.fromstring(archive.read(rels_name))
        for rel in rels:
            if rel.attrib.get("Id") == relation_id:
                target = rel.attrib.get("Target", "")
                break
    if target:
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        if target in names:
            return target
    for name in names:
        if name.startswith("xl/worksheets/") and name.endswith(".xml"):
            return name
    raise ValueError("The workbook has no worksheet.")


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t", "")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t")
    value = ""
    for node in cell:
        if _local(node.tag) == "v":
            value = node.text or ""
            break
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    return value


def _column_letter(index: int) -> str:
    number = index + 1
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _column_index(ref: str) -> int:
    letters = "".join(character for character in ref if character.isalpha())
    number = 0
    for character in letters:
        number = number * 26 + (ord(character.upper()) - 64)
    return number - 1


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(value) -> str:
    text = "" if value is None else str(value)
    cleaned = []
    for character in text:
        code = ord(character)
        if code in (0x9, 0xA, 0xD) or code >= 0x20:
            cleaned.append(character)
    escaped = "".join(cleaned)
    return (
        escaped.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
