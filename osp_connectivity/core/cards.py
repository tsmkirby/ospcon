"""One printable page per traced circuit. The PDF uses only the standard fonts."""

from __future__ import annotations
from .notation import panel_end


PAGE_WIDTH = 612
PAGE_HEIGHT = 792
LEFT = 48
RIGHT = 564
TOP = 744
BOTTOM = 52
MAP_HEIGHT = 292
MAP_BOTTOM = 44
MAP_LABEL = 16


def circuit_card_pages(reports: list[dict], images: list | None = None) -> list[dict]:
    """Pages for each report. The first page of a report keeps its map image."""
    if not reports:
        raise ValueError("Select at least one circuit.")
    pictures = list(images or [])
    pages = []
    for index, report in enumerate(reports):
        image = pictures[index] if index < len(pictures) else None
        packed = _pack(_blocks(report), bool(image))
        for page_index, blocks in enumerate(packed):
            pages.append({"blocks": blocks, "image": image if page_index == 0 else None})
    return pages


def write_circuit_cards(path: str, reports: list[dict], images: list | None = None) -> int:
    """Write a letter-size PDF and return the number of pages."""
    pages = circuit_card_pages(reports, images)
    payload = _pdf(pages)
    with open(path, "wb") as handle:
        handle.write(payload)
    return len(pages)


def fit_extent(boxes: list[tuple], pixel_width: int, pixel_height: int, pad_ratio: float = 0.3, min_span: float = 0) -> tuple | None:
    """Extent around the traced features, padded and matched to the map frame."""
    if not boxes or pixel_width <= 0 or pixel_height <= 0:
        return None
    xmin = min(box[0] for box in boxes)
    ymin = min(box[1] for box in boxes)
    xmax = max(box[2] for box in boxes)
    ymax = max(box[3] for box in boxes)
    width = max(xmax - xmin, 0)
    height = max(ymax - ymin, 0)
    span = max(width, height, min_span, 1e-9)
    if width < span:
        extra = span - width
        xmin -= extra / 2
        xmax += extra / 2
        width = span
    if height < span:
        extra = span - height
        ymin -= extra / 2
        ymax += extra / 2
        height = span
    pad_x = width * pad_ratio
    pad_y = height * pad_ratio
    xmin -= pad_x
    xmax += pad_x
    ymin -= pad_y
    ymax += pad_y
    width = xmax - xmin
    height = ymax - ymin
    target = pixel_width / float(pixel_height)
    current = width / float(height)
    if current > target:
        extra = (width / target) - height
        ymin -= extra / 2
        ymax += extra / 2
    else:
        extra = (height * target) - width
        xmin -= extra / 2
        xmax += extra / 2
    return (xmin, ymin, xmax, ymax)


def jpeg_info(data: bytes) -> tuple[int, int, str]:
    """Width, height, and PDF color space for a JPEG."""
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("The map image is not a JPEG.")
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            raise ValueError("The map image is not a JPEG.")
        marker = data[index + 1]
        if marker in (0xD8, 0xD9):
            index += 2
            continue
        if index + 4 > len(data):
            break
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            components = data[index + 9]
            space = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(components, "DeviceRGB")
            return width, height, space
        index += 2 + length
    raise ValueError("The map image is not a JPEG.")


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
        blocks.append(_block("ASSOCIATED PLANT (grouped by asset type)", 9, True, 14))
        for line in _wrap(path, 70):
            blocks.append(_block(line, 11, False, 16))
        blocks.append(_block("", 4, False, 10, rule=True))
    rows = [
        ("Device", device),
        ("Associated devices", ', '.join(report.get('associated_devices') or [])),
        ("Pole", report.get("pole_id") or ""),
        ("Cable", _cable_line(report)),
        ("Segments", " > ".join(report.get("segments") or [])),
        ("Ducts", " > ".join(report.get("ducts") or [])),
        ("Handholes", " > ".join(report.get("handholes") or [])),
        ("Enclosures", ", ".join(report.get("enclosures") or [])),
        ("IDF", report.get("idf_id") or ""),
        ("Panel", _panel_line(report)),
        ("Horizontal link", report.get("horizontal_link") or ""),
        ("Switch", '' if report.get('horizontal_link') else
         ' '.join(value for value in (report.get('switch_id'), report.get('switch_port')) if value)),
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
    if report.get('splice_lines'):
        blocks.append(_block("Saved route splices (all associated fibers):", 11, True, 16))
        for splice in report['splice_lines']:
            for line in _wrap(splice, 78):
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
    for handhole in report.get("handholes") or []:
        if handhole not in parts:
            parts.append(handhole)
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
    return panel_end(panel, port)


def _block(text: str, size: int, bold: bool, gap: int, rule: bool = False) -> dict:
    return {"text": text, "size": size, "bold": bold, "gap": gap, "rule": rule}


def _pack(blocks: list[dict], map_reserved: bool = False) -> list[list[dict]]:
    pages = []
    current = []
    used = 0
    room = TOP - (MAP_BOTTOM + MAP_HEIGHT + MAP_LABEL if map_reserved else BOTTOM)
    identity = blocks[1]["text"] if len(blocks) > 1 else "Circuit"
    continued = [
        _block("OSP CIRCUIT CARD", 16, True, 22, rule=True),
        _block(f"{identity} (continued)", 14, True, 24),
    ]

    def start(first: bool) -> None:
        nonlocal current, used, room
        current = [] if first else list(continued)
        used = 0 if first else sum(item["gap"] for item in continued)
        room = TOP - (MAP_BOTTOM + MAP_HEIGHT + MAP_LABEL if first and map_reserved else BOTTOM)

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
    # A site ID can be longer than an entire line; split it instead of clipping.
    words = [chunk for word in str(text).split()
             for chunk in (word[index:index + width] for index in range(0, len(word), width))]
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


def _pdf(pages: list[dict]) -> bytes:
    # Standard text fonts plus Symbol for the bidirectional link arrow.
    fonts_index = 3 + len(pages) * 2
    image_numbers = {}
    next_image = fonts_index + 4
    for page in pages:
        image = page.get("image")
        if image and id(image) not in image_numbers:
            image_numbers[id(image)] = next_image
            next_image += 1
    kids = []
    body = []
    body.append("1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    for index, page in enumerate(pages):
        page_number = 3 + index * 2
        contents_number = page_number + 1
        kids.append(f"{page_number} 0 R")
        image = page.get("image")
        xobject = ""
        if image:
            xobject = f" /XObject << /Im0 {image_numbers[id(image)]} 0 R >>"
        body.append(
            f"{page_number} 0 obj << /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Contents {contents_number} 0 R "
            f"/Resources << /Font << /F1 {fonts_index} 0 R /F2 {fonts_index + 1} 0 R /F3 {fonts_index + 2} 0 R >>{xobject} >> >> endobj\n"
        )
        stream = _content(page["blocks"], index + 1, len(pages), bool(image))
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
    body.append(f"{fonts_index + 2} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Symbol /ToUnicode {fonts_index + 3} 0 R >> endobj\n")
    cmap = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /LinkArrow def /CMapType 2 def\n"
            "1 begincodespacerange <00> <FF> endcodespacerange\n"
            "1 beginbfchar <AB> <2194FE0E> endbfchar\n"
            "endcmap CMapName currentdict /CMap defineresource pop end end\n").encode('ascii')
    body.append(f"{fonts_index + 3} 0 obj << /Length {len(cmap)} >> stream\n".encode('ascii')
                + cmap + b"endstream\nendobj\n")
    written = set()
    for page in pages:
        image = page.get("image")
        if not image or id(image) in written:
            continue
        written.add(id(image))
        width, height, space = jpeg_info(image)
        number = image_numbers[id(image)]
        body.append(
            f"{number} 0 obj << /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace /{space} /BitsPerComponent 8 /Filter /DCTDecode /Length {len(image)} >> stream\n".encode("ascii")
            + image
            + b"\nendstream\nendobj\n"
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


def _content(blocks: list[dict], page_number: int, page_count: int, draw_map: bool) -> bytes:
    y = TOP
    lines = ["0.4 w", "0.15 0.25 0.35 RG", "0.15 0.25 0.35 rg"]
    for block in blocks:
        y -= block["gap"]
        if block["text"]:
            font = "F2" if block["bold"] else "F1"
            # Symbol's 0xAB is the actual left-right arrow, not a Latin-1 substitute.
            parts = str(block['text']).replace('\u2194\ufe0e', '\u2194').split('\u2194')
            runs = f" /{font} {block['size']} Tf (" + _pdf_text(parts[0]) + ") Tj"
            for part in parts[1:]:
                runs += f" /F3 {block['size']} Tf <AB> Tj /{font} {block['size']} Tf ({_pdf_text(part)}) Tj"
            lines.append(
                f"BT 1 0 0 1 {LEFT} {y} Tm{runs} ET"
            )
        if block.get("rule"):
            rule_y = y - 6
            lines.append(f"{LEFT} {rule_y} m {RIGHT} {rule_y} l S")
    if draw_map:
        label_y = MAP_BOTTOM + MAP_HEIGHT + 4
        width = RIGHT - LEFT
        lines.append(
            f"BT /F1 9 Tf 1 0 0 1 {LEFT} {label_y} Tm (Map: background, associated plant, and handholes) Tj ET"
        )
        lines.append(f"q {width} 0 0 {MAP_HEIGHT} {LEFT} {MAP_BOTTOM} cm /Im0 Do Q")
        lines.append(f"{LEFT} {MAP_BOTTOM} {width} {MAP_HEIGHT} re S")
    lines.append(
        f"BT /F1 9 Tf 1 0 0 1 {LEFT} 28 Tm (Page {page_number} of {page_count}) Tj ET"
    )
    return "\n".join(lines).encode("latin-1", "replace")


def _pdf_text(value: str) -> str:
    text = str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return text.encode("latin-1", "replace").decode("latin-1")
