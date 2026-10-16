"""Part 3: follow a device, cable, or panel port back through splices."""

from __future__ import annotations

from .catalog import DEVICE_ROLES, build_index, feature_id, resolve_end
from .fields import media_of, parse_count, text_of
from .strands import compress_splices, strand_numbers_for


def trace_selection(selected: list[dict], features: list[dict], tables: dict) -> dict:
    devices = [feature for feature in selected if feature.get("role") in DEVICE_ROLES]
    cables = [feature for feature in selected if feature.get("role") == "cable"]
    anchors = [feature for feature in selected if feature.get("role") in ("idf", "patch_panel", "fdp")]
    if devices:
        targets = devices
        kind = "device"
    elif cables:
        targets = cables
        kind = "cable"
    elif anchors:
        targets = anchors
        kind = "anchor"
    else:
        return {
            "reports": [],
            "highlights": _empty_highlights(),
            "warnings": ["Select an AP, camera, jack, cable, IDF, or panel."],
        }

    reports = []
    highlights = _empty_highlights()
    warnings = []
    for target in targets:
        if kind == "device":
            pieces = _trace_device(target, features, tables)
        elif kind == "cable":
            pieces = _trace_cable(target, features, tables)
        else:
            pieces = _trace_anchor(target, features, tables)
        for piece in pieces:
            reports.append(piece)
            _merge_highlights(highlights, piece.get("highlights") or _empty_highlights())
            warnings.extend(piece.get("warnings") or [])
    return {"reports": reports, "highlights": highlights, "warnings": warnings}


def format_report(result: dict) -> str:
    blocks = []
    for warning in result.get("warnings") or []:
        if warning not in blocks:
            blocks.append(f"Warning: {warning}")
    for report in result.get("reports") or []:
        blocks.append(_format_one(report))
    return "\n\n".join(block for block in blocks if block).strip() or "Nothing to report."


def _format_one(report: dict) -> str:
    lines = []
    if report.get("device_id"):
        lines.append(f"Device: {report['device_id']}")
    if report.get("pole_id"):
        lines.append(f"Pole: {report['pole_id']}")
    if report.get("cable_id"):
        length = f"  {report['length']}" if report.get("length") else ""
        kind = f"  {report['cable_type']}" if report.get("cable_type") else ""
        lines.append(f"Cable: {report['cable_id']}{kind}{length}")
    if report.get("segments"):
        lines.append("Segments: " + " > ".join(report["segments"]))
    if report.get("enclosures"):
        lines.append("Enclosures: " + ", ".join(report["enclosures"]))
    if report.get("ducts"):
        lines.append("Ducts: " + " > ".join(report["ducts"]))
    if report.get("idf_id"):
        lines.append(f"IDF: {report['idf_id']}")
    if report.get("panel_id") or report.get("panel_port"):
        port = f"  port {report['panel_port']}" if report.get("panel_port") else ""
        lines.append(f"Panel: {report.get('panel_id') or ''}{port}".rstrip())
    for hop_line in report.get("strand_lines") or []:
        lines.append(hop_line)
    for warning in report.get("warnings") or []:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)


def _empty_highlights() -> dict:
    return {"cables": [], "devices": [], "idfs": [], "enclosures": []}


def _merge_highlights(target: dict, extra: dict) -> None:
    for key in target:
        for item in extra.get(key) or []:
            if item not in target[key]:
                target[key].append(item)


def _trace_device(device: dict, features: list[dict], tables: dict) -> list[dict]:
    index = build_index(features)
    identifier = feature_id(device)
    circuits = [
        row
        for row in tables.get("circuits") or []
        if text_of(row.get("device_id")) and text_of(row.get("device_id")).casefold() == (identifier or "").casefold()
    ]
    if circuits:
        return [_report_from_circuit(device, row, features, tables, index) for row in circuits]

    cables = _cables_for_device(device, features, index)
    if not cables:
        return [
            {
                "device_id": identifier,
                "warnings": [f"{identifier} has no cable."],
                "highlights": {
                    "cables": [],
                    "devices": [_ref(device)],
                    "idfs": [],
                    "enclosures": [],
                },
            }
        ]
    return [_report_from_cable(device, cable, None, features, tables, index) for cable in cables]


def _trace_cable(cable: dict, features: list[dict], tables: dict) -> list[dict]:
    index = build_index(features)
    identifier = feature_id(cable)
    devices = []
    for feature in features:
        if feature.get("role") not in DEVICE_ROLES:
            continue
        cable_ref = text_of((feature.get("values") or {}).get("cable_ref"))
        if cable_ref and identifier and cable_ref.casefold() == identifier.casefold():
            devices.append(feature)
    values = cable.get("values") or {}
    for end_name in ("a_end", "z_end"):
        for hit in resolve_end(index, values.get(end_name), cable):
            if hit.get("role") in DEVICE_ROLES and hit not in devices:
                devices.append(hit)
    if devices:
        return [_report_from_cable(device, cable, None, features, tables, index) for device in devices]
    return [_report_from_cable(None, cable, None, features, tables, index)]


def _trace_anchor(anchor: dict, features: list[dict], tables: dict) -> list[dict]:
    index = build_index(features)
    identifier = feature_id(anchor)
    reports = []
    if anchor.get("role") == "idf":
        for cable in features:
            if cable.get("role") != "cable":
                continue
            values = cable.get("values") or {}
            ends = []
            for end_name in ("a_end", "z_end"):
                ends.extend(resolve_end(index, values.get(end_name), cable))
            if any(feature_id(hit) == identifier and hit.get("role") == "idf" for hit in ends):
                reports.extend(_trace_cable(cable, features, tables))
    else:
        for row in tables.get("circuits") or []:
            if text_of(row.get("panel_id")) and text_of(row.get("panel_id")).casefold() == (identifier or "").casefold():
                device = _find_id(features, row.get("device_id"), DEVICE_ROLES)
                cable = _find_id(features, row.get("cable_id"), {"cable"})
                if cable:
                    reports.append(_report_from_cable(device, cable, row, features, tables, index))
    if not reports:
        return [{"warnings": [f"Nothing is terminated on {identifier}."], "highlights": _empty_highlights()}]
    return reports


def _report_from_circuit(device, circuit, features, tables, index) -> dict:
    cable = _find_id(features, circuit.get("cable_id"), {"cable"}, circuit.get("cable_layer_id"))
    if cable is None:
        return {
            "device_id": feature_id(device),
            "warnings": [f"Circuit {circuit.get('circuit_id')} points at missing cable {circuit.get('cable_id')}."],
            "highlights": {"cables": [], "devices": [_ref(device)], "idfs": [], "enclosures": []},
        }
    return _report_from_cable(device, cable, circuit, features, tables, index)


def _report_from_cable(device, cable, circuit, features, tables, index) -> dict:
    warnings = []
    identifier = feature_id(cable)
    copies = [
        feature
        for feature in features
        if feature.get("role") == "cable" and feature_id(feature) and feature_id(feature).casefold() == (identifier or "").casefold()
    ]
    if len(copies) > 1:
        warnings.append(f"Cable id {identifier} exists on {len(copies)} features. All of them are highlighted.")

    values = cable.get("values") or {}
    device_hit = None
    idf_hit = None
    device_end = None
    for end_name in ("a_end", "z_end"):
        for hit in resolve_end(index, values.get(end_name), cable):
            if hit.get("role") in DEVICE_ROLES and device_hit is None:
                device_hit = hit
                device_end = end_name
            elif hit.get("role") == "idf" and idf_hit is None:
                idf_hit = hit
    if device is None:
        device = device_hit
    elif device_hit is None:
        device_hit = device

    if circuit:
        panel = text_of(circuit.get("panel_id"))
        port = text_of(circuit.get("panel_port"))
        if circuit.get("idf_id") and idf_hit is None:
            idf_hit = _find_id(features, circuit.get("idf_id"), {"idf"})
    else:
        panel, port = _panel_for(values, device_end, device)

    pole_id = None
    if device:
        mount = text_of((device.get("values") or {}).get("mount"))
        structures = [hit for hit in resolve_end(index, mount, device) if hit.get("role") == "structure"]
        if structures:
            pole_id = feature_id(structures[0])
        elif mount:
            pole_id = mount

    media = media_of(values)
    if circuit and text_of(circuit.get("media_type")) in ("fiber", "copper"):
        media = circuit.get("media_type")
    strand_lines = []
    enclosure_refs = []
    rows = []
    if media == "fiber":
        numbers = []
        if circuit and circuit.get("strand_from"):
            start = int(circuit["strand_from"])
            end = int(circuit.get("strand_to") or start)
            numbers = list(range(start, end + 1))
        if not numbers:
            numbers = strand_numbers_for(values) or strand_numbers_for((device or {}).get("values") or {})
        if numbers and parse_count(values.get("strand_count")) and max(numbers) > parse_count(values.get("strand_count")):
            warnings.append(f"Strand list exceeds the strand count on {identifier}.")
        rows = _walk_strands(identifier, numbers, tables.get("splices") or [])
        strand_lines = compress_splices(rows) if rows else []
        if numbers and not strand_lines:
            strand_lines = [f"{identifier} strands {numbers[0]}-{numbers[-1]}" if len(numbers) > 1 else f"{identifier} strand {numbers[0]}"]
        seen_enc = set()
        for row in rows:
            enc_id = text_of(row.get("splice_enc_id"))
            if not enc_id or enc_id in seen_enc:
                continue
            seen_enc.add(enc_id)
            enc = _find_id(features, enc_id, {"splice_enc"})
            if enc:
                enclosure_refs.append(_ref(enc))

    length = text_of(values.get("length_f"))
    if length:
        length = f"{length} ft"
    elif text_of(values.get("length_m")):
        length = f"{values.get('length_m')} m"

    cable_ids, enclosure_ids = linked_fiber(identifier, features, tables, index)
    ducts = []
    for cable_name in cable_ids:
        for label in _duct_labels(cable_name, "", tables.get("routes") or []):
            if label not in ducts:
                ducts.append(label)
    highlighted = []
    for feature in features:
        if feature.get("role") != "cable":
            continue
        if feature_id(feature) and feature_id(feature).casefold() in {item.casefold() for item in cable_ids}:
            ref = _ref(feature)
            if ref not in highlighted:
                highlighted.append(ref)
    for enc_id in enclosure_ids:
        enc = _find_id(features, enc_id, {"splice_enc"})
        if enc and _ref(enc) not in enclosure_refs:
            enclosure_refs.append(_ref(enc))
    highlights = {
        "cables": highlighted or [_ref(cable)],
        "devices": [_ref(device)] if device else [],
        "idfs": [_ref(idf_hit)] if idf_hit else [],
        "enclosures": enclosure_refs,
    }
    return {
        "device_id": feature_id(device) if device else None,
        "pole_id": pole_id,
        "cable_id": identifier,
        "cable_type": text_of(values.get("cable_type")) or text_of(values.get("media")),
        "length": length,
        "ducts": ducts,
        "idf_id": feature_id(idf_hit) if idf_hit else (text_of(circuit.get("idf_id")) if circuit else None),
        "panel_id": panel,
        "panel_port": port,
        "strand_lines": strand_lines,
        "segments": cable_ids if len(cable_ids) > 1 else [],
        "enclosures": enclosure_ids,
        "warnings": warnings,
        "highlights": highlights,
    }


def linked_fiber(start_id: str, features: list[dict], tables: dict, index: dict) -> tuple[list[str], list[str]]:
    """Cables and enclosures connected to this segment by splices, membership, or shared ends."""
    start = text_of(start_id)
    if not start:
        return [], []
    splices = tables.get("splices") or []
    memberships = tables.get("memberships") or []
    cables = [feature for feature in features if feature.get("role") == "cable" and feature_id(feature)]
    segments: list[str] = []
    enclosures: list[str] = []
    seen_cables: set[str] = set()
    seen_enc: set[str] = set()
    queue = [start]

    def add_enclosure(value) -> None:
        enc = text_of(value)
        if enc and enc.casefold() not in seen_enc:
            seen_enc.add(enc.casefold())
            enclosures.append(enc)
            for other in cables:
                values = other.get("values") or {}
                ends = {
                    (text_of(values.get("a_end")) or "").casefold(),
                    (text_of(values.get("z_end")) or "").casefold(),
                }
                if enc.casefold() in ends:
                    queue.append(feature_id(other))
            for row in memberships:
                if (text_of(row.get("splice_enc_id")) or "").casefold() == enc.casefold():
                    other_id = text_of(row.get("cable_id"))
                    if other_id:
                        queue.append(other_id)

    while queue:
        current = text_of(queue.pop(0))
        if not current or current.casefold() in seen_cables:
            continue
        seen_cables.add(current.casefold())
        segments.append(current)
        cable = next((item for item in cables if feature_id(item).casefold() == current.casefold()), None)
        if cable:
            values = cable.get("values") or {}
            for end_name in ("a_end", "z_end"):
                for hit in resolve_end(index, values.get(end_name), cable):
                    if hit.get("role") == "splice_enc":
                        add_enclosure(feature_id(hit))
        for row in splices:
            a_cable = text_of(row.get("a_cable_id"))
            b_cable = text_of(row.get("b_cable_id"))
            if (a_cable or "").casefold() == current.casefold() and b_cable:
                queue.append(b_cable)
                add_enclosure(row.get("splice_enc_id"))
            elif (b_cable or "").casefold() == current.casefold() and a_cable:
                queue.append(a_cable)
                add_enclosure(row.get("splice_enc_id"))
        for row in memberships:
            if (text_of(row.get("cable_id")) or "").casefold() == current.casefold():
                add_enclosure(row.get("splice_enc_id"))
    return segments, enclosures


def _panel_for(values: dict, device_end: str | None, device: dict | None) -> tuple:
    if device_end == "a_end":
        panel = text_of(values.get("z_panel"))
        port = text_of(values.get("z_port"))
    elif device_end == "z_end":
        panel = text_of(values.get("a_panel"))
        port = text_of(values.get("a_port"))
    else:
        panel = text_of(values.get("a_panel")) or text_of(values.get("z_panel"))
        port = text_of(values.get("a_port")) or text_of(values.get("z_port"))
    if device and not panel:
        panel = text_of((device.get("values") or {}).get("panel"))
        port = port or text_of((device.get("values") or {}).get("port"))
    return panel, port


def _cables_for_device(device: dict, features: list[dict], index: dict) -> list[dict]:
    identifier = feature_id(device)
    cable_ref = text_of((device.get("values") or {}).get("cable_ref"))
    found = []
    for cable in features:
        if cable.get("role") != "cable":
            continue
        if cable_ref and feature_id(cable) and feature_id(cable).casefold() == cable_ref.casefold():
            found.append(cable)
            continue
        values = cable.get("values") or {}
        for end_name in ("a_end", "z_end"):
            if text_of(values.get(end_name)) and text_of(values.get(end_name)).casefold() == (identifier or "").casefold():
                found.append(cable)
                break
    return found


def _walk_strands(cable_id: str, numbers: list[int], splices: list[dict]) -> list[dict]:
    """Follow each strand until it stops. Express and fusion both continue."""
    index: dict[tuple, list[dict]] = {}
    for splice in splices:
        for side, other in (("a", "b"), ("b", "a")):
            side_cable = text_of(splice.get(f"{side}_cable_id"))
            side_strand = splice.get(f"{side}_strand_no")
            if not side_cable or side_strand in (None, ""):
                continue
            index.setdefault((side_cable.casefold(), int(side_strand)), []).append((other, splice))

    rows = []
    for start in numbers:
        current_cable = cable_id
        current_strand = start
        seen = set()
        while current_cable and current_strand:
            key = (current_cable.casefold(), int(current_strand))
            if key in seen:
                break
            seen.add(key)
            options = index.get(key) or []
            if not options:
                break
            other_side, splice = options[0]
            next_cable = text_of(splice.get(f"{other_side}_cable_id"))
            next_strand = splice.get(f"{other_side}_strand_no")
            rows.append(
                {
                    "a_cable_id": current_cable,
                    "a_strand_no": int(current_strand),
                    "b_cable_id": next_cable,
                    "b_strand_no": int(next_strand) if next_strand not in (None, "") else None,
                    "splice_type": splice.get("splice_type") or "splice",
                    "splice_enc_id": splice.get("splice_enc_id"),
                }
            )
            if (splice.get("splice_type") or "").upper() == "OPEN" or not next_cable:
                break
            current_cable = next_cable
            current_strand = int(next_strand)
    return rows


def _duct_labels(cable_id: str, layer_id: str, routes: list[dict]) -> list[str]:
    matched = []
    for route in routes:
        if text_of(route.get("cable_id")) and text_of(route.get("cable_id")).casefold() != (cable_id or "").casefold():
            continue
        route_layer = text_of(route.get("cable_source_layer_id") or route.get("cable_layer_id"))
        if route_layer and layer_id and route_layer != layer_id:
            continue
        matched.append(route)
    matched.sort(key=lambda route: int(route.get("route_order") or 0))
    labels = []
    for route in matched:
        label = text_of(route.get("segment_id"))
        if not label:
            label = f"fid {route.get('segment_fid')}"
        if label:
            labels.append(label)
    return labels


def _find_id(features: list[dict], identifier, roles: set, layer_id: str | None = None):
    wanted = text_of(identifier)
    if not wanted:
        return None
    for feature in features:
        if roles and feature.get("role") not in roles:
            continue
        if layer_id and feature.get("layer_id") != layer_id:
            continue
        if feature_id(feature) and feature_id(feature).casefold() == wanted.casefold():
            return feature
    if layer_id:
        return _find_id(features, identifier, roles, None)
    return None


def _ref(feature: dict) -> tuple:
    return (feature.get("layer_id"), feature.get("fid"))
