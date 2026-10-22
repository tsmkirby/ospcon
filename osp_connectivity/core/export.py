"""Write a trace report to CSV or Excel, including appending to a workbook that already exists."""

from __future__ import annotations

import csv
import os
import zipfile
import tempfile
import io
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

COLUMNS = (
    "Link ID",
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
    "Horizontal link",
    "Splices",
    "Segments",
    "Enclosures",
    "Warnings",
    "Switch",
    "Switch port",
    "Data jack",
    "Installation status",
    "Test result",
    "Date",
)

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def report_record(report: dict) -> dict:
    """One spreadsheet row. The keys are the column titles and never change."""
    route = report.get('circuit_route') or {}
    status = report.get('installation_status') or ''
    if status.casefold() == 'proposed':
        status = ''
    return {
        "Link ID": report.get('link_id') or '',
        "Device": report.get("device_id") or "",
        "Pole": report.get("pole_id") or "",
        "Cable": report.get("cable_id") or "",
        "Cable type": report.get("cable_type") or "",
        "Length": report.get("length") or "",
        "Ducts": "\n".join(route['ducts']) if 'ducts' in route else " > ".join(report.get('ducts') or []),
        "Handholes": "\n".join(route['handholes']) if 'handholes' in route else " > ".join(report.get('handholes') or []),
        "IDF": report.get("idf_id") or "",
        "Panel": report.get("panel_id") or "",
        "Port": report.get("panel_port") or "",
        "Strands": "\n".join(route.get('splices') or report.get("strand_lines") or []),
        "Horizontal link": report.get("horizontal_link") or "",
        "Splices": "\n".join(route.get('splices', report.get("splice_lines") or [])),
        "Segments": "\n".join(route.get('segments', report.get("segments") or [])),
        "Enclosures": "\n".join(route.get('enclosures', report.get("enclosures") or [])),
        "Warnings": " | ".join(report.get("warnings") or []),
        "Switch": report.get('switch_id') or '',
        "Switch port": report.get('switch_port') or '',
        "Data jack": report.get('jack_id') or '',
        "Installation status": status,
        "Test result": report.get('test_result') or '',
        "Date": report.get('completion_date') or '',
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
    seen = {}
    for report in reports:
        record = report_record(report)
        key = record['Link ID']
        if key and key in seen:
            if seen[key] != record:
                raise ValueError(f'Conflicting reports for {key}. Trace that link separately before exporting.')
            continue
        if key:
            seen[key] = record
        rows.append([record.get(name, "") for name in headers])
    return headers, rows


def export_reports(path: str, reports: list[dict], fields: list[str], append: bool, update: bool = False) -> int:
    headers, rows = table_from_reports(reports, fields)
    if not headers:
        raise ValueError("Select at least one field.")
    if not rows:
        raise ValueError("Select at least one item.")
    kind = _kind(path)
    selected_count = len(rows)
    if append or update:
        if not os.path.exists(path):
            raise ValueError(f"{path} does not exist.")
        old_headers, old_rows = read_table(path)
        if update:
            headers, rows = update_links(old_headers, old_rows, headers, rows)
        else:
            headers, rows = merge_tables(old_headers, old_rows, headers, rows)
    # Build the complete file before replacing an existing customer tracker.
    descriptor, temporary = tempfile.mkstemp(suffix='.' + kind, dir=os.path.dirname(os.path.abspath(path)))
    os.close(descriptor)
    try:
        if kind == "csv":
            _write_csv(temporary, headers, rows)
        elif update:
            _refresh_xlsx(path, temporary, headers, rows, old_headers, old_rows)
        else:
            _write_xlsx(temporary, headers, rows)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    if update:
        return selected_count
    if append:
        return len(rows) - len(old_rows)
    return len(rows)


def _refresh_xlsx(source, target, headers, rows, old_headers, old_rows):
    """Patch changed cells in place, retaining user formatting and other workbook parts."""
    ET.register_namespace('', _MAIN)
    ns = '{' + _MAIN + '}'
    with zipfile.ZipFile(source) as archive:
        sheet_name = _first_sheet_name(archive)
        original_sheet = archive.read(sheet_name)
        namespaces = dict(value for _, value in ET.iterparse(io.BytesIO(original_sheet), events=['start-ns']))
        for prefix, uri in namespaces.items():
            if not (prefix.startswith('ns') and prefix[2:].isdigit()):
                ET.register_namespace(prefix, uri)
        sheet = ET.fromstring(original_sheet)
        data = sheet.find(ns + 'sheetData')
        if data is None:
            raise ValueError('The tracker has no worksheet data.')
        row_nodes = list(data)
        old_end = f'{_column_letter(len(old_headers) - 1)}{max(int(node.get("r")) for node in data)}'
        shared = _shared_strings(archive)
        # Empty rows entered by the customer keep their positions.
        populated = [node for node in row_nodes if any(_cell_text(cell, shared).strip() for cell in node)]
        if len(populated) != len(old_rows) + 1:
            raise ValueError('The tracker row layout could not be matched. Create a new export instead.')
        templates = { _column_index(cell.get('r', '')): cell for cell in populated[-1] }
        header_template = next(iter(populated[0]), None)
        body_template = next((cell for column, cell in templates.items()
                              if len(populated) > 1 and old_headers[column] != 'Date'), None)
        date_styles = _date_styles(archive)
        cols = sheet.find(ns + 'cols')
        widths = [13.0] * len(headers)
        if cols is not None:
            for col in cols:
                for index in range(int(col.get('min')) - 1, min(int(col.get('max')), len(headers))):
                    widths[index] = float(col.get('width', '13'))
        if len(headers) > len(old_headers):
            if cols is None:
                cols = ET.Element(ns + 'cols')
                sheet.insert(list(sheet).index(data), cols)
            for index in range(len(old_headers), len(headers)):
                widths[index] = _column_width(headers[index])
                ET.SubElement(cols, ns + 'col', {'min': str(index + 1), 'max': str(index + 1),
                                               'width': str(widths[index]), 'customWidth': '1'})
        old_grid = [old_headers, *old_rows]
        grid = [headers, *rows]
        for index, values in enumerate(grid):
            changed = False
            if index < len(populated):
                node = populated[index]
            else:
                number = max((int(item.get('r', '0')) for item in data), default=0) + 1
                node = ET.SubElement(data, ns + 'row', {'r': str(number), 'ht': '32', 'customHeight': '1'})
            number = node.get('r')
            cells = {_column_index(cell.get('r', '')): cell for cell in node}
            for column, value in enumerate(values):
                old = old_grid[index][column] if index < len(old_grid) and column < len(old_grid[index]) else None
                if old == value:
                    continue
                changed = True
                cell = cells.get(column)
                if cell is None:
                    template = header_template if index == 0 else templates.get(column, body_template)
                    attributes = {'r': f'{_column_letter(column)}{number}'}
                    if template is not None and 's' in template.attrib:
                        attributes['s'] = template.get('s')
                    cell = ET.SubElement(node, ns + 'c', attributes)
                for child in list(cell):
                    cell.remove(child)
                serial = _excel_date(value) if index and headers[column] == 'Date' else None
                if serial is not None and int(cell.get('s', '0')) not in date_styles and date_styles:
                    cell.set('s', str(min(date_styles)))
                # Existing Date cells retain their native Excel date formatting.
                if serial is not None and int(cell.get('s', '0')) in date_styles:
                    cell.attrib.pop('t', None)
                    ET.SubElement(cell, ns + 'v').text = str(serial)
                else:
                    cell.set('t', 'inlineStr')
                    text_node = ET.SubElement(ET.SubElement(cell, ns + 'is'), ns + 't')
                    text_node.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                    text_node.text = str(value or '')
            node[:] = sorted(node, key=lambda cell: _column_index(cell.get('r', '')))
            if index and changed:
                line_count = max((sum(max(1, (len(part) + int(widths[col]) - 1) // max(1, int(widths[col])))
                                      for part in str(value).split('\n'))
                                  for col, value in enumerate(values) if headers[col] != 'Link ID'), default=1)
                height = min(409, max(float(node.get('ht', '32')), line_count * 15 + 8))
                node.set('ht', str(height))
                node.set('customHeight', '1')
        end = f'{_column_letter(len(headers) - 1)}{max(int(node.get("r")) for node in data)}'
        for name in ('dimension', 'autoFilter'):
            element = sheet.find(ns + name)
            if element is not None:
                element.set('ref', 'A1:' + end)
        payload = ET.tostring(sheet, encoding='utf-8', xml_declaration=True)
        # Excel's mc:Ignorable may refer to prefixes absent from actual node names.
        for prefix, uri in namespaces.items():
            if prefix and f'xmlns:{prefix}='.encode() not in payload:
                sheet.set('xmlns:' + prefix, uri)
        payload = ET.tostring(sheet, encoding='utf-8', xml_declaration=True)
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            for item in archive.infolist():
                content = payload if item.filename == sheet_name else archive.read(item.filename)
                if item.filename.startswith('xl/tables/') and item.filename.endswith('.xml'):
                    table = ET.fromstring(content)
                    if table.get('ref') == 'A1:' + old_end:
                        table.set('ref', 'A1:' + end)
                        table_filter = table.find(ns + 'autoFilter')
                        if table_filter is not None:
                            table_filter.set('ref', 'A1:' + end)
                        columns = table.find(ns + 'tableColumns')
                        if columns is not None:
                            next_id = max((int(col.get('id')) for col in columns), default=0) + 1
                            for name in headers[len(old_headers):]:
                                ET.SubElement(columns, ns + 'tableColumn', {'id': str(next_id), 'name': name})
                                next_id += 1
                            columns.set('count', str(len(columns)))
                        content = ET.tostring(table, encoding='utf-8', xml_declaration=True)
                output.writestr(item, content)


def update_links(old_headers, old_rows, new_headers, new_rows):
    """Refresh by persistent Link ID; blank QGIS tracking fields preserve manual values."""
    if 'Link ID' not in old_headers or 'Link ID' not in new_headers:
        raise ValueError('Updating links requires the Link ID column in both exports. Create a new tracker including Link ID first.')
    headers = list(old_headers) + [name for name in new_headers if name not in old_headers]

    def keyed(source_headers, source_rows):
        result = {}
        for row in source_rows:
            record = dict(zip(source_headers, row))
            key = str(record.get('Link ID') or '').strip()
            if not key or key in result:
                raise ValueError('Link ID must be populated and unique for every row before updating.')
            result[key] = record
        return result

    existing = keyed(old_headers, old_rows)
    for record in existing.values():
        if str(record.get('Installation status') or '').strip().casefold() == 'proposed':
            record['Installation status'] = ''
    incoming = keyed(new_headers, new_rows)
    tracking = {'Installation status', 'Test result', 'Date'}
    for key, record in incoming.items():
        target = existing.setdefault(key, {})
        for name, value in record.items():
            if name in tracking and not str(value or '').strip():
                continue
            target[name] = value
    return headers, [[record.get(name, '') for name in headers] for record in existing.values()]


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


def _column_width(name):
    return (64 if name in ('Strands', 'Splices', 'Horizontal link', 'Warnings') else
            30 if name in ('Segments', 'Enclosures', 'Handholes', 'Ducts', 'Test result', 'Link ID') else
            22 if name in ('Device', 'Cable', 'Cable type', 'IDF', 'Switch', 'Data jack') else 18)


def _write_xlsx(path: str, headers: list[str], rows: list[list[str]]) -> None:
    grid = [headers, *rows]
    sheet_rows = []
    widths = [_column_width(name) for name in headers]
    for row_number, row in enumerate(grid, start=1):
        cells = []
        for column, value in enumerate(row):
            ref = f"{_column_letter(column)}{row_number}"
            style = 1 if row_number == 1 else 2 if row_number % 2 == 0 else 3
            date_value = _excel_date(value) if row_number > 1 and headers[column] == 'Date' else None
            if row_number > 1 and headers[column] == 'Date':
                style = 4 if row_number % 2 == 0 else 5
            if date_value is not None:
                cells.append(f'<c r="{ref}" s="{style}"><v>{date_value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{_xml_text(value)}</t></is></c>')
        lines = max((sum(max(1, (len(part) + int(widths[col]) - 1) // int(widths[col]))
                         for part in str(value).split('\n')) for col, value in enumerate(row)), default=1)
        height = 32 if row_number == 1 else min(409, max(32, 15 * lines + 8))
        sheet_rows.append(f'<row r="{row_number}" ht="{height}" customHeight="1">{"".join(cells)}</row>')
    columns = ''.join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"' +
                      (' hidden="1"' if headers[i - 1] == 'Link ID' else '') + '/>'
                      for i, width in enumerate(widths, 1))
    end = f'{_column_letter(len(headers) - 1)}{len(grid)}'
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_MAIN}"><dimension ref="A1:{end}"/>'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
        '<pane xSplit="2" ySplit="1" topLeftCell="C2" activePane="bottomRight" state="frozen"/>'
        '</sheetView></sheetViews>'
        f'<cols>{columns}</cols><sheetData>{"".join(sheet_rows)}</sheetData>'
        f'<autoFilter ref="A1:{end}"/>'
        '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
        '<pageSetup paperSize="1" orientation="landscape" scale="100"/>'
        '<headerFooter><oddFooter>&amp;LOSP Link Register&amp;RPage &amp;P of &amp;N</oddFooter></headerFooter>'
        '</worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_MAIN}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="OSP Links" sheetId="1" r:id="rId1"/></sheets>'
        '<definedNames><definedName name="_xlnm.Print_Titles" localSheetId="0">\'OSP Links\'!$1:$1</definedName></definedNames></workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
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
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/styles.xml", _styles())


def _excel_date(value):
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        text = str(value or '').strip()
        if not text:
            return None
        for pattern in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y'):
            try:
                value = datetime.strptime(text, pattern).date()
                break
            except ValueError:
                pass
        else:
            return None  # Retain unrecognized user text verbatim.
    return (value - date(1899, 12, 30)).days


def _styles():
    return (f'<styleSheet xmlns="{_MAIN}">'
            '<fonts count="2"><font><sz val="11"/><color rgb="FF172B3A"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
            '<fills count="4"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FF183C50"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFF0F5F8"/><bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="14" fontId="0" fillId="3" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def _read_xlsx(path: str) -> tuple[list[str], list[list[str]]]:
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        sheet_name = _first_sheet_name(archive)
        sheet = ET.fromstring(archive.read(sheet_name))
        date_styles = _date_styles(archive)
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
            if int(cell.get('s', '0')) in date_styles and cells[index] and cell.get('t', 'n') == 'n':
                try:
                    cells[index] = (date(1899, 12, 30) + timedelta(days=float(cells[index]))).isoformat()
                except (ValueError, OverflowError):
                    pass
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


def _date_styles(archive):
    if 'xl/styles.xml' in archive.namelist():
        styles = ET.fromstring(archive.read('xl/styles.xml'))
        for node in styles:
            if _local(node.tag) == 'cellXfs':
                return {i for i, xf in enumerate(node) if int(xf.get('numFmtId', '0')) in range(14, 23)}
    return set()


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
