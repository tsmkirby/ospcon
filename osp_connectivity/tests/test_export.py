"""Spreadsheet export keeps one column layout and can grow an existing file."""

import os
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from osp_connectivity.core.export import export_reports, read_table, _write_xlsx


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
    def test_customer_sort_does_not_move_tracking_values_to_another_link(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'sorted.xlsx')
            headers = ['Link ID', 'Device', 'Test result']
            _write_xlsx(path, headers, [['second', 'AP-2', 'Pass 2'], ['first', 'AP-1', 'Pass 1']])
            export_reports(path, [_report(link_id='first', device_id='AP-1 updated'),
                                  _report(link_id='second', device_id='AP-2 updated')],
                           headers, append=False, update=True)
            actual_headers, rows = read_table(path)
            records = {row[actual_headers.index('Link ID')]: dict(zip(actual_headers, row)) for row in rows}
            self.assertEqual(records['first']['Test result'], 'Pass 1')
            self.assertEqual(records['second']['Test result'], 'Pass 2')
            self.assertEqual(records['first']['Device'], 'AP-1 updated')

    def test_refresh_preserves_formulas_and_excel_namespace_declarations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'custom.xlsx')
            _write_xlsx(path, ['Link ID', 'Device', 'Customer notes'], [['key', 'AP-1', 'Manual note']])
            with zipfile.ZipFile(path) as archive:
                parts = {item.filename: archive.read(item.filename) for item in archive.infolist()}
            main = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
            sheet = parts['xl/worksheets/sheet1.xml'].decode()
            sheet = sheet.replace(f'<worksheet xmlns="{main}">',
                                  f'<worksheet xmlns="{main}" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac" mc:Ignorable="x14ac">')
            sheet = sheet.replace('<c r="C2" s="2" t="inlineStr"><is><t xml:space="preserve">Manual note</t></is></c>',
                                  '<c r="C2" s="2" t="str"><f>CONCATENATE("Manual ","note")</f><v>Manual note</v></c>')
            parts['xl/worksheets/sheet1.xml'] = sheet.encode()
            with zipfile.ZipFile(path, 'w') as archive:
                for name, payload in parts.items():
                    archive.writestr(name, payload)
            export_reports(path, [_report(link_id='key', device_id='AP-1 updated')],
                           ['Link ID', 'Device', 'Date'], append=False, update=True)
            with zipfile.ZipFile(path) as archive:
                payload = archive.read('xl/worksheets/sheet1.xml')
                self.assertIn(b'xmlns:x14ac=', payload)
                tree = ET.fromstring(payload)
                self.assertEqual(next(node.text for node in tree.iter() if node.tag.endswith('}f')),
                                 'CONCATENATE("Manual ","note")')

    def test_tracker_update_matches_ids_keeps_manual_tracking_and_native_workbook_parts(self):
        fields = ['Link ID', 'Device', 'Cable', 'Installation status', 'Test result', 'Date']
        reports = [_report(link_id='ckt.AP-1.CB-1', installation_status='proposed'),
                   _report(link_id='ckt.AP-2.CB-2', device_id='AP-2', cable_id='CB-2')]
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'tracker.xlsx')
            export_reports(path, reports, fields, append=False)
            headers, rows = read_table(path)
            status, result, day = [headers.index(name) for name in ('Installation status', 'Test result', 'Date')]
            self.assertEqual(rows[0][status], '')
            rows[0][status], rows[0][result], rows[0][day] = 'Installed', 'Pass / test-001', '2026-10-21'
            headers.append('Customer notes')
            rows[0].append('Keep this note')
            rows[1].append('')
            _write_xlsx(path, headers, rows)
            with zipfile.ZipFile(path, 'a') as archive:
                archive.writestr('customXml/customer.xml', '<customer>Keep this metadata</customer>')
            changed = [_report(link_id='ckt.AP-1.CB-1', cable_id='CB-1-ASBUILT', installation_status='proposed'),
                       _report(link_id='ckt.AP-3.CB-3', device_id='AP-3', cable_id='CB-3',
                               installation_status='Installed', completion_date='2026-10-22', test_result='Pass')]
            self.assertEqual(export_reports(path, changed, fields, append=False, update=True), 2)
            actual_headers, actual_rows = read_table(path)
            self.assertEqual(len(actual_rows), 3)
            first = dict(zip(actual_headers, actual_rows[0]))
            self.assertEqual(first['Cable'], 'CB-1-ASBUILT')
            self.assertEqual(first['Installation status'], 'Installed')
            self.assertEqual(first['Test result'], 'Pass / test-001')
            self.assertEqual(first['Date'], '2026-10-21')
            self.assertEqual(first['Customer notes'], 'Keep this note')
            self.assertEqual(dict(zip(actual_headers, actual_rows[2]))['Date'], '2026-10-22')
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.read('customXml/customer.xml'), b'<customer>Keep this metadata</customer>')
                sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
                self.assertTrue(any(node.tag.endswith('pane') and node.get('state') == 'frozen' for node in sheet.iter()))
                self.assertTrue(any(node.tag.endswith('autoFilter') for node in sheet.iter()))

    def test_duplicate_or_missing_link_ids_leave_existing_tracker_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'tracker.xlsx')
            export_reports(path, [_report(link_id='unique')], ['Link ID', 'Device'], append=False)
            with open(path, 'rb') as handle:
                original = handle.read()
            for reports in ([_report(link_id='')], [_report(link_id='same'), _report(link_id='same', device_id='OTHER')]):
                with self.assertRaises(ValueError):
                    export_reports(path, reports, ['Link ID', 'Device'], append=False, update=True)
                with open(path, 'rb') as handle:
                    self.assertEqual(handle.read(), original)

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
