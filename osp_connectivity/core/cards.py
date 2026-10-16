"""One printable page per traced circuit. The PDF uses only the standard fonts."""

from __future__ import annotations


PAGE_WIDTH = 612
PAGE_HEIGHT = 792
LEFT = 48
RIGHT = 564
TOP = 744
BOTTOM = 52


def circuit_card_pages(reports: list[dict]) -> list[list[dict]]:
    """Pages of text blocks. Each report starts on its own page."""
    if not reports:
        raise ValueError("Select at least one circuit.")
    pages = []
    for report in reports:
        blocks = _blocks(report)
        pages.extend(_pack(blocks))
    return pages


def write_circuit_cards(path: str, reports: list[dict]) -> int:
    """Write a letter-size PDF and return the number of pages."""
    pages = circuit_card_pages(reports)
    payload = _pdf(pages)
    with open(path, "wb") as handle:
        handle.write(payload)
    return len(pages)


def _blocks(report: dict) -> list[dict]:
    device = report.get("device_id") or ""
    cable = report.get("cable_id") or ""
    identity = device or cable or report.get("idf_id") or "Circuit"
    blocks = [
        _block("OSP CIRCUIT CARD", 16, True, 22, rule=True),
        _block(identity, 18, True, 30),
    ]
    path = _path(report)
    if path:
        blocks.append(_block("PATH", 9, True, 14))
        for line in _wrap(path, 70):
            blocks.append(_block(line, 11, False, 16))
        blocks.append(_block("", 4, False, 10, rule=True))
    rows = [
        ("Device", device),
        ("Pole", report.get("pole_id") or ""),
        ("Cable", _cable_line(report)),
        ("Segments", " > ".join(report.get("segments") or [])),
        ("Ducts", " > ".join(report.get("ducts") or [])),
        ("Enclosures", ", ".join(report.get("enclosures") or [])),
        ("IDF", report.get("idf_id") or ""),
        ("Panel", _panel_line(report)),
        ("Strands", ""),
    ]
    for label, value in rows:
        if label == "Strands":
            continue
        if not value:
            continue
        for index, line in enumerate(_wrap(f"{label}:  {value}", 78)):
            shown = line if index == 0 else f"    {line}"
            blocks.append(_block(shown, 11, False, 16))
    strands = report.get("strand_lines") or []
    if strands:
        blocks.append(_block("Strands:", 11, True, 16))
        for strand in strands:
            for line in _wrap(strand, 78):
                blocks.append(_block(f"    {line}", 10, False, 14))
    for warning in report.get("warnings") or []:
        for line in _wrap(f"Warning:  {warning}", 78):
            blocks.append(_block(line, 10, False, 14))
    return blocks


def _path(report: dict) -> str:
    parts = []
    if report.get("device_id"):
        parts.append(report["device_id"])
    if report.get("segments"):
        parts.extend(report["segments"])
    elif report.get("cable_id"):
        parts.append(report["cable_id"])
    for enclosure in report.get("enclosures") or []:
        if enclosure not in parts:
            parts.append(enclosure)
    panel = _panel_line(report)
    idf = report.get("idf_id") or ""
    if panel and idf:
        parts.append(f"{panel} @ {idf}")
    elif panel:
        parts.append(panel)
    elif idf:
        parts.append(idf)
    return "  >  ".join(parts)


def _cable_line(report: dict) -> str:
    bits = [report.get("cable_id") or ""]
    if report.get("cable_type"):
        bits.append(report["cable_type"])
    if report.get("length"):
        bits.append(report["length"])
    return "   ".join(bit for bit in bits if bit)


def _panel_line(report: dict) -> str:
    panel = report.get("panel_id") or ""
    port = report.get("panel_port") or ""
    if panel and port:
        return f"{panel} port {port}"
    return panel or (f"port {port}" if port else "")


def _block(text: str, size: int, bold: bool, gap: int, rule: bool = False) -> dict:
    return {"text": text, "size": size, "bold": bold, "gap": gap, "rule": rule}


def _pack(blocks: list[dict]) -> list[list[dict]]:
    pages = []
    current = []
    used = 0
    room = TOP - BOTTOM
    identity = blocks[1]["text"] if len(blocks) > 1 else "Circuit"
    continued = [
        _block("OSP CIRCUIT CARD", 16, True, 22, rule=True),
        _block(f"{identity} (continued)", 14, True, 24),
    ]

    def start(first: bool) -> None:
        nonlocal current, used
        current = [] if first else list(continued)
        used = 0 if first else sum(item["gap"] for item in continued)

    start(True)
    for block in blocks:
        if used + block["gap"] > room and current:
            pages.append(current)
            start(False)
        current.append(block)
        used += block["gap"]
    if current:
        pages.append(current)
    return pages


def _wrap(text: str, width: int) -> list[str]:
    words = str(text).split()
    if not words:
        return []
    lines = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _pdf(pages: list[list[dict]]) -> bytes:
    # Objects: 1 catalog, 2 pages, then for each page a page object and a content object,
    # then two fonts. Built after the streams so lengths are known.
    fonts_index = 3 + len(pages) * 2
    kids = []
    body = []
    body.append("1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    for index, blocks in enumerate(pages):
        page_number = 3 + index * 2
        contents_number = page_number + 1
        kids.append(f"{page_number} 0 R")
        body.append(
            f"{page_number} 0 obj << /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Contents {contents_number} 0 R "
            f"/Resources << /Font << /F1 {fonts_index} 0 R /F2 {fonts_index + 1} 0 R >> >> >> endobj\n"
        )
        stream = _content(blocks, index + 1, len(pages))
        body.append(
            f"{contents_number} 0 obj << /Length {len(stream)} >> stream\n".encode("ascii")
            + stream
            + b"\nendstream\nendobj\n"
        )
    kids_text = " ".join(kids)
    body.insert(1, f"2 0 obj << /Type /Pages /Kids [{kids_text}] /Count {len(pages)} >> endobj\n".encode("ascii"))
    body.append(
        f"{fonts_index} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n".encode("ascii")
    )
    body.append(
        f"{fonts_index + 1} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >> endobj\n".encode("ascii")
    )
    encoded = [item if isinstance(item, bytes) else item.encode("ascii") for item in body]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for item in encoded:
        offsets.append(len(output))
        output.extend(item)
    xref_at = len(output)
    count = len(offsets)
    output.extend(f"xref\n0 {count}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer << /Size {count} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def _content(blocks: list[dict], page_number: int, page_count: int) -> bytes:
    y = TOP
    lines = ["0.4 w", "0.15 0.25 0.35 RG", "0.15 0.25 0.35 rg"]
    for block in blocks:
        y -= block["gap"]
        if block["text"]:
            font = "F2" if block["bold"] else "F1"
            lines.append(
                f"BT /{font} {block['size']} Tf 1 0 0 1 {LEFT} {y} Tm ({_pdf_text(block['text'])}) Tj ET"
            )
        if block.get("rule"):
            rule_y = y - 6
            lines.append(f"{LEFT} {rule_y} m {RIGHT} {rule_y} l S")
    lines.append(
        f"BT /F1 9 Tf 1 0 0 1 {LEFT} 36 Tm (Page {page_number} of {page_count}) Tj ET"
    )
    return "\n".join(lines).encode("latin-1", "replace")


def _pdf_text(value: str) -> str:
    text = str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return text.encode("latin-1", "replace").decode("latin-1")
