"""Spreadsheet export keeps one column layout and can grow an existing file."""

import os
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from osp_connectivity.core.export import export_reports, read_table


def _report(**values):
    base = {
        "device_id": "AP-1",
        "pole_id": "PL-1",
        "cable_id": "CB-1",
        "cable_type": "CAT6",
        "length": "120 ft",
        "ducts": ["0001", "0002"],
        "idf_id": "IDF-1",
        "panel_id": "PP01",
        "panel_port": "14",
        "strand_lines": [],
        "warnings": [],
    }
    base.update(values)
    return base


class ExportTests(unittest.TestCase):
    def test_csv_round_trip_uses_the_standard_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "report.csv")
            written = export_reports(path, [_report()], ["Device", "Cable", "Ducts", "Port"], append=False)
            headers, rows = read_table(path)
        self.assertEqual(written, 1)
        self.assertEqual(headers, ["Device", "Cable", "Ducts", "Port"])
        self.assertEqual(rows, [["AP-1", "CB-1", "0001 > 0002", "14"]])

    def test_xlsx_round_trip_and_append_keeps_extra_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "report.xlsx")
            export_reports(path, [_report()], ["Device", "Cable", "IDF"], append=False)
            # A column typed into the sheet by hand must survive the next append.
            headers, rows = read_table(path)
            headers.append("Notes")
            rows[0].append("checked")
            _write_shared_string_book(path, headers, rows)
            written = export_reports(
                path,
                [_report(device_id="AP-2", cable_id="CB-2", idf_id="IDF-2", strand_lines=["CB-2 strands 1-2"])],
                ["Device", "Cable", "IDF", "Strands"],
                append=True,
            )
            headers, rows = read_table(path)
        self.assertEqual(written, 1)
        self.assertEqual(headers, ["Device", "Cable", "IDF", "Notes", "Strands"])
        self.assertEqual(rows[0], ["AP-1", "CB-1", "IDF-1", "checked", ""])
        self.assertEqual(rows[1], ["AP-2", "CB-2", "IDF-2", "", "CB-2 strands 1-2"])

    def test_csv_append_adds_a_new_column_without_dropping_old_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "report.csv")
            export_reports(path, [_report()], ["Device", "Panel"], append=False)
            export_reports(path, [_report(device_id="AP-2", panel_id="PP02", panel_port="3")], ["Device", "Panel", "Port"], append=True)
            headers, rows = read_table(path)
        self.assertEqual(headers, ["Device", "Panel", "Port"])
        self.assertEqual(rows, [["AP-1", "PP01", ""], ["AP-2", "PP02", "3"]])


def _write_shared_string_book(path, headers, rows):
    """An Excel-style sheet that stores text in the shared-string table."""
    values = []
    lookup = {}

    def index_of(text):
        if text not in lookup:
            lookup[text] = len(values)
            values.append(text)
        return lookup[text]

    body = []
    for row_number, row in enumerate([headers, *rows], start=1):
        cells = []
        for column, value in enumerate(row):
            letter = chr(65 + column)
            cells.append(f'<c r="{letter}{row_number}" t="s"><v>{index_of(value)}</v></c>')
        body.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    shared_items = "".join(f"<si><t>{_escape(value)}</t></si>" for value in values)
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{main}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="OSP Report" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="{main}">{shared_items}</sst>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{main}"><sheetData>{"".join(body)}</sheetData></worksheet>',
        )
    ET.fromstring(zipfile.ZipFile(path).read("xl/worksheets/sheet1.xml"))


def _escape(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;")


if __name__ == "__main__":
    unittest.main()
