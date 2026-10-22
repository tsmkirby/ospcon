"""Part 3: follow a device, cable, or panel port back through splices."""

from __future__ import annotations
from uuid import uuid5, NAMESPACE_URL

from .catalog import DEVICE_ROLES, build_index, feature_id, resolve_end
from .fields import media_of, parse_count, text_of, ranges_overlap, parse_range
from .handholes import handhole_label, handholes_on_cables
from .fiberpath import fiber_routes, route_lines
from .strands import compress_splices, strand_numbers_for
from .notation import fiber_ranges, horizontal_link, panel_end
from .naming import local_asset_id


def cables_for_device(device: dict, features: list[dict], index: dict) -> list[dict]:
    return _cables_for_device(device, features, index)


def trace_selection(selected: list[dict], features: list[dict], tables: dict, tolerance: float = 1.0) -> dict:
    devices = [feature for feature in selected if feature.get("role") in DEVICE_ROLES]
    cables = [feature for feature in selected if feature.get("role") == "cable"]
    anchors = [feature for feature in selected if feature.get("role") in ("idf", "patch_panel", "fdp", "splice_enc")]
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
            "warnings": ["Select an AP, camera, jack, cable, IDF, panel, or splice enclosure."],
        }

    reports = []
    highlights = _empty_highlights()
    warnings = []
    for target in targets:
        if kind == "device":
            pieces = _trace_device(target, features, tables, tolerance)
        elif kind == "cable":
            pieces = _trace_cable(target, features, tables, tolerance)
        else:
            pieces = _trace_anchor(target, features, tables, tolerance)
        for piece in pieces:
            reports.append(piece)
            _merge_highlights(highlights, piece.get("highlights") or _empty_highlights())
            warnings.extend(piece.get("warnings") or [])
    return {"reports": reports, "highlights": highlights, "warnings": warnings}


def trace_fibers(cable_id, layer_id, first, last, features: list[dict], tables: dict, tolerance: float = 1.0) -> dict:
    """Trace only the stored route of one cable's fiber range, such as one splice in an enclosure."""
    cable = _find_id(features, cable_id, {"cable"}, layer_id or None)
    if cable is None:
        return {"reports": [], "highlights": _empty_highlights(),
                "warnings": [f"Cable {cable_id or '(blank)'} is not in the project."]}
    try:
        first = int(first)
        last = int(last if last is not None else first)
    except (TypeError, ValueError):
        return {"reports": [], "highlights": _empty_highlights(),
                "warnings": [f"Fiber range {first}-{last} on {cable_id} is not valid."]}
    circuit = {"cable_id": feature_id(cable), "strand_from": first, "strand_to": last, "media_type": "fiber",
               "_route_only": True}
    report = _report_from_cable(None, cable, circuit, features, tables, build_index(features), tolerance)
    return {"reports": [report], "highlights": report.get("highlights") or _empty_highlights(), "warnings": []}


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
    if report.get('associated_devices'):
        lines.append('Associated devices: ' + ', '.join(report['associated_devices']))
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
    if report.get("handholes"):
        lines.append("Handholes: " + " > ".join(report["handholes"]))
    if report.get("idf_id"):
        lines.append(f"IDF: {report['idf_id']}")
    if report.get("panel_id") or report.get("panel_port"):
        lines.append("Panel: " + panel_end(report.get('panel_id'), report.get('panel_port')))
    if report.get("horizontal_link"):
        lines.append("Horizontal link: " + report['horizontal_link'])
    elif report.get('switch_id'):
        lines.append('Switch: ' + report['switch_id'] + ' ' + (report.get('switch_port') or ''))
    for hop_line in report.get("strand_lines") or []:
        lines.append(hop_line)
    if report.get("splice_lines"):
        lines.append("Saved route splices (all associated fibers):")
        lines.extend(report['splice_lines'])
    for warning in report.get("warnings") or []:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)


def _empty_highlights() -> dict:
    return {"cables": [], "devices": [], "idfs": [], "enclosures": [], "handholes": []}


def _merge_highlights(target: dict, extra: dict) -> None:
    for key in target:
        for item in extra.get(key) or []:
            if item not in target[key]:
                target[key].append(item)


def _trace_device(device: dict, features: list[dict], tables: dict, tolerance: float = 1.0) -> list[dict]:
    index = build_index(features)
    identifier = feature_id(device)
    circuits = [
        row
        for row in tables.get("circuits") or []
        if text_of(row.get("device_id")) and text_of(row.get("device_id")).casefold() == (identifier or "").casefold()
    ]
    if circuits:
        return [_report_from_circuit(device, row, features, tables, index, tolerance) for row in circuits]

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
                    "handholes": [],
                },
            }
        ]
    return [_report_from_cable(device, cable, None, features, tables, index, tolerance) for cable in cables]


def _trace_cable(cable: dict, features: list[dict], tables: dict, tolerance: float = 1.0) -> list[dict]:
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
        return [_report_from_cable(device, cable, None, features, tables, index, tolerance) for device in devices]
    return [_report_from_cable(None, cable, None, features, tables, index, tolerance)]


def _trace_anchor(anchor: dict, features: list[dict], tables: dict, tolerance: float = 1.0) -> list[dict]:
    index = build_index(features)
    identifier = feature_id(anchor)
    reports = []
    if anchor.get("role") == "splice_enc":
        from .splice_editor import editor_model
        model = editor_model(anchor, features, tables, tolerance)
        for cable in model['cables']:
            if not cable.get('missing'):
                reports.extend(_trace_cable(cable, features, tables, tolerance))
        if not reports:
            reports = [{"warnings": [f"No saved cables on {identifier}."],
                        'splice_lines': compress_splices(model['original']), 'highlights': _empty_highlights()}]
        for report in reports:
            if identifier not in report.setdefault('enclosures', []):
                report['enclosures'].append(identifier)
            if _ref(anchor) not in report['highlights']['enclosures']:
                report['highlights']['enclosures'].append(_ref(anchor))
        return reports
    if anchor.get("role") == "idf":
        for cable in features:
            if cable.get("role") != "cable":
                continue
            values = cable.get("values") or {}
            ends = []
            for end_name in ("a_end", "z_end"):
                ends.extend(resolve_end(index, values.get(end_name), cable))
            if any(feature_id(hit) == identifier and hit.get("role") == "idf" for hit in ends):
                reports.extend(_trace_cable(cable, features, tables, tolerance))
    else:
        for row in tables.get("circuits") or []:
            if text_of(row.get("panel_id")) and text_of(row.get("panel_id")).casefold() == (identifier or "").casefold():
                device = _find_id(features, row.get("device_id"), DEVICE_ROLES)
                cable = _find_id(features, row.get("cable_id"), {"cable"})
                if cable:
                    reports.append(_report_from_cable(device, cable, row, features, tables, index, tolerance))
    if not reports:
        return [{"warnings": [f"Nothing is terminated on {identifier}."], "highlights": _empty_highlights()}]
    return reports


def _report_from_circuit(device, circuit, features, tables, index, tolerance: float = 1.0) -> dict:
    cable = _find_id(features, circuit.get("cable_id"), {"cable"}, circuit.get("cable_layer_id"))
    if cable is None:
        return {
            "device_id": feature_id(device),
            "warnings": [f"Circuit {circuit.get('circuit_id')} points at missing cable {circuit.get('cable_id')}."],
            "highlights": {"cables": [], "devices": [_ref(device)] if device else [], "idfs": [], "enclosures": [], "handholes": []},
        }
    return _report_from_cable(device, cable, circuit, features, tables, index, tolerance)


def _report_from_cable(device, cable, circuit, features, tables, index, tolerance: float = 1.0) -> dict:
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
    numbers = []
    routes = []
    if media == "fiber":
        if circuit and circuit.get("strand_from"):
            start = int(circuit["strand_from"])
            end = int(circuit.get("strand_to") or start)
            numbers = list(range(start, end + 1))
        if not numbers:
            if device:
                numbers = parse_range(((device or {}).get('values') or {}).get('port'))
                if not numbers and device_end:
                    numbers = parse_range(values.get('a_port' if device_end == 'a_end' else 'z_port'))
                if not numbers:
                    warnings.append('Circuit strand assignment is unknown; the circuit route cannot be fully determined.')
            else:
                numbers = strand_numbers_for(values)
        if numbers and parse_count(values.get("strand_count")) and max(numbers) > parse_count(values.get("strand_count")):
            warnings.append(f"Strand list exceeds the strand count on {identifier}.")
        if numbers:
            # Known fibers are followed hop by hop to both terminations, in route order.
            found = fiber_routes(identifier, cable.get("layer_id") or "", numbers, features, tables)
            routes = found["routes"]
            warnings.extend(found["warnings"])
            rows = [hop for route in routes for hop in route["rows"]]
            strand_lines = route_lines(routes)
        else:
            rows = _walk_strands(identifier, numbers, tables.get("splices") or [])
            strand_lines = compress_splices(rows) if rows else []
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
    path_ids = list(dict.fromkeys(name for route in routes for name in route["cables"]))
    route_only = bool(routes) and bool((circuit or {}).get("_route_only"))
    if route_only:
        # Only the plant these fibers pass through, not every cable sharing an enclosure with them.
        cable_ids = path_ids
        enclosure_ids = list(dict.fromkeys(name for route in routes for name in route["enclosures"]))
    associated_ids = {item.casefold() for item in cable_ids}
    route_splices = [row for row in tables.get('splices') or []
                    if any((text_of(row.get(f'{side}_cable_id')) or '').casefold() in associated_ids
                           for side in ('a', 'b'))] if media == 'fiber' and not route_only else []
    splice_lines = compress_splices(route_splices)
    path_cables = [item for item in features if item.get('role') == 'cable'
                   and (feature_id(item) or '').casefold() in associated_ids] or [cable]
    route_idfs, route_panels = {}, set()
    for segment in path_cables:
        segment_values = segment.get('values') or {}
        for side in ('a', 'z'):
            hits = resolve_end(index, segment_values.get(f'{side}_end'), segment)
            for hit in hits:
                if hit.get('role') == 'idf':
                    route_idfs[_ref(hit)] = hit
                    if text_of(segment_values.get(f'{side}_panel')):
                        route_panels.add((text_of(segment_values.get(f'{side}_panel')),
                                          text_of(segment_values.get(f'{side}_port'))))
    if routes and len(routes) == 1 and not text_of((circuit or {}).get("panel_id")):
        # The route's own start: where the traced fibers leave the plant on the first cable's side.
        first = routes[0]
        start_room = _find_id(features, first["start_place"], {"idf"})
        if start_room:
            idf_hit = start_room
        if first["start_panel"]:
            panel, port = first["start_panel"], first["start_ports"] or None
    if not idf_hit and len(route_idfs) == 1:
        idf_hit = next(iter(route_idfs.values()))
    if not panel and len(route_panels) == 1:
        panel, port = next(iter(route_panels))
    if len(route_idfs) > 1 and not routes:
        warnings.append('Associated route has multiple IDFs: ' + ', '.join(feature_id(item) for item in route_idfs.values()))
    idf_id = feature_id(idf_hit) if idf_hit else (text_of(circuit.get('idf_id')) if circuit else None)
    panel_feature = _find_id(features, panel, {'patch_panel', 'fdp'})
    idf_id = idf_id or text_of(((panel_feature or {}).get('values') or {}).get('idf_ref'))
    patches = [row for row in tables.get('patches') or []
               if (text_of(row.get('panel_id')) or '').casefold() == (panel or '').casefold()
               and ranges_overlap(row.get('panel_port'), port)
               and (not idf_id or not text_of(row.get('mdf_idf_id'))
                    or text_of(row.get('mdf_idf_id')).casefold() == idf_id.casefold())
               and (text_of(row.get('media_type')) or 'copper').casefold() == media]
    patch_rooms = {text_of(row.get('mdf_idf_id')) for row in patches if text_of(row.get('mdf_idf_id'))}
    if not idf_id and len(patch_rooms) == 1:
        idf_id = next(iter(patch_rooms))
    copper_link = None
    if media == 'copper':
        related = [item for item in features if item.get('role') in DEVICE_ROLES
                   and any(_ref(hit) == _ref(cable) for hit in _cables_for_device(item, features, index))]
        if device and not any(_ref(item) == _ref(device) for item in related):
            related.append(device)
        jacks = list(dict.fromkeys(feature_id(item) for item in related if item.get('role') == 'data_jack'))
        endpoints = list(dict.fromkeys(feature_id(item) for item in related if item.get('role') != 'data_jack'))
        links = []
        for patch in patches or [{}]:
            sw_id = text_of(patch.get('sw_id'))
            sw_port = text_of(patch.get('switch_port_id'))
            switch = next((row for row in tables.get('switches') or []
                           if sw_id and (text_of(row.get('sw_id')) or '').casefold() == sw_id.casefold()), {})
            room = idf_id or text_of(patch.get('mdf_idf_id')) or text_of(switch.get('mdf_idf_id'))
            idf_id = idf_id or room
            links.append(horizontal_link(room, sw_id, sw_port, panel, port, identifier,
                                         text_of(values.get('cable_type')) or text_of(values.get('media')),
                                         jacks, endpoints))
            if not all((room, sw_id, sw_port, panel, port)):
                warnings.append('Horizontal link is incomplete: IDF, switch, switch port, panel, or panel port is missing.')
        if not jacks and not endpoints:
            warnings.append('Horizontal link has no recorded data jack or endpoint device.')
        if len(patches) > 1:
            warnings.append('Multiple switch patches are recorded for this panel port; all are shown.')
        copper_link = ' | '.join(dict.fromkeys(links))
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
    handhole_features = handholes_on_cables(
        path_cables,
        [feature for feature in features if feature.get("role") == "handhole"],
        tolerance,
    )
    stored_ids = []
    for row in tables.get("handholes") or []:
        if (text_of(row.get("cable_id")) or "").casefold() not in {item.casefold() for item in cable_ids}:
            continue
        identifier_text = text_of(row.get("handhole_id"))
        if identifier_text and identifier_text not in stored_ids:
            stored_ids.append(identifier_text)
    handhole_ids = []
    handhole_refs = []
    for handhole in handhole_features:
        name = handhole_label(handhole)
        if name and name not in handhole_ids:
            handhole_ids.append(name)
        ref = _ref(handhole)
        if ref not in handhole_refs:
            handhole_refs.append(ref)
    for name in stored_ids:
        if name not in handhole_ids:
            handhole_ids.append(name)
        found = _find_id(features, name, {"handhole"})
        if found and _ref(found) not in handhole_refs:
            handhole_refs.append(_ref(found))
    route_devices = {}
    for segment in path_cables:
        for side in ('a', 'z'):
            for hit in resolve_end(index, (segment.get('values') or {}).get(f'{side}_end'), segment):
                if hit.get('role') in DEVICE_ROLES:
                    route_devices[_ref(hit)] = hit
    for item in features:
        if item.get('role') in DEVICE_ROLES and (text_of((item.get('values') or {}).get('cable_ref')) or '').casefold() in associated_ids:
            route_devices[_ref(item)] = item
    if device:
        route_devices[_ref(device)] = device
    highlights = {
        "cables": highlighted or [_ref(cable)],
        "devices": list(route_devices),
        "idfs": list(dict.fromkeys([*route_idfs, *([_ref(idf_hit)] if idf_hit else [])])),
        "enclosures": enclosure_refs,
        "handholes": handhole_refs,
    }
    # Circuit-only route data deliberately excludes other cables sharing an enclosure.
    circuit_ids = list(dict.fromkeys([identifier, *[row.get(f'{side}_cable_id')
                        for row in rows for side in ('a', 'b') if row.get(f'{side}_cable_id')]]))
    if routes:
        circuit_ids = list(path_ids)
    circuit_set = {name.casefold() for name in circuit_ids}
    circuit_cables = [item for item in path_cables if (feature_id(item) or '').casefold() in circuit_set]
    circuit_encs = list(dict.fromkeys(row['splice_enc_id'] for row in rows if row.get('splice_enc_id')))
    for row in tables.get('memberships') or []:
        if (text_of(row.get('cable_id')) or '').casefold() in circuit_set:
            enc_id = text_of(row.get('splice_enc_id'))
            if enc_id and enc_id not in circuit_encs:
                circuit_encs.append(enc_id)
    # An enclosure can be present at a cable end even without a saved splice row.
    for item in circuit_cables:
        for side in ('a', 'z'):
            for hit in resolve_end(index, (item.get('values') or {}).get(f'{side}_end'), item):
                if hit.get('role') == 'splice_enc' and feature_id(hit) not in circuit_encs:
                    circuit_encs.append(feature_id(hit))
    circuit_hhs = [handhole_label(item) for item in handholes_on_cables(
        circuit_cables, [item for item in features if item.get('role') == 'handhole'], tolerance)]
    for row in tables.get('handholes') or []:
        if (text_of(row.get('cable_id')) or '').casefold() in circuit_set and text_of(row.get('handhole_id')):
            circuit_hhs.append(text_of(row['handhole_id']))
    circuit_ducts = list(dict.fromkeys(label for name in circuit_ids
                         for label in _duct_labels(name, '', tables.get('routes') or [])))
    hop_groups = {}
    for row in rows:
        key = tuple(row.get(name) for name in ('a_cable_id', 'b_cable_id', 'splice_enc_id',
                                             'splice_type', 'a_layer_id', 'b_layer_id'))
        hop_groups.setdefault(key, []).append(row)
    ordered_splices = [line for group in hop_groups.values() for line in compress_splices(group)]
    # With known fibers the segments are the route itself, in order; otherwise every associated segment.
    segments = path_ids if routes else cable_ids
    segments = segments if len(segments) > 1 else []

    def tracking_value(*keys):
        return next((text_of(source.get(key)) for source in (circuit or {}, values) for key in keys
                     if text_of(source.get(key))), '')

    installation_status = next((value for value in
                               (tracking_value('installation_status', 'install_status'),
                                text_of((circuit or {}).get('status')), text_of(values.get('status')))
                               if value and value.casefold() != 'proposed'), '')
    return {
        "link_id": 'L-' + uuid5(NAMESPACE_URL, repr((cable.get('layer_id') or '',
                   text_of((circuit or {}).get('circuit_id')) or
                   (f'ckt.{feature_id(device)}.{identifier}' if device else identifier)))).hex,
        "media": media,
        "installation_status": installation_status,
        "test_result": tracking_value('test_result', 'test_results'),
        "completion_date": tracking_value('completion_date', 'installation_date', 'install_date'),
        "circuit_route": {'segments': circuit_ids, 'enclosures': circuit_encs,
                          'handholes': list(dict.fromkeys(circuit_hhs)), 'ducts': circuit_ducts,
                          'splices': ordered_splices},
        "switch_id": ' | '.join(dict.fromkeys(local_asset_id(row.get('sw_id'), idf_id) for row in patches if text_of(row.get('sw_id')))),
        "switch_port": ' | '.join(dict.fromkeys(text_of(row.get('switch_port_id')) for row in patches if text_of(row.get('switch_port_id')))),
        "jack_id": ' | '.join(jacks) if media == 'copper' else '',
        "device_id": feature_id(device) if device else None,
        "associated_devices": list(dict.fromkeys(feature_id(item) for item in route_devices.values())),
        "pole_id": pole_id,
        "cable_id": identifier,
        "cable_type": text_of(values.get("cable_type")) or text_of(values.get("media")),
        "length": length,
        "ducts": ducts,
        "idf_id": idf_id,
        "horizontal_link": copper_link,
        "panel_id": local_asset_id(panel, idf_id),
        "panel_key": panel,
        "panel_port": port,
        "strand_lines": strand_lines if routes else (ordered_splices or strand_lines),
        "splice_lines": splice_lines,
        "segments": segments,
        "enclosures": enclosure_ids,
        "handholes": handhole_ids,
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
            if current.casefold() in ((a_cable or '').casefold(), (b_cable or '').casefold()):
                add_enclosure(row.get('splice_enc_id'))
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
    """Walk each physical connection once, including both directions from a middle segment."""
    from collections import deque

    index = {}
    for edge, splice in enumerate(splices):
        for side, other in (("a", "b"), ("b", "a")):
            identifier = text_of(splice.get(f"{side}_cable_id"))
            number = splice.get(f"{side}_strand_no")
            if identifier and number not in (None, ""):
                index.setdefault((identifier.casefold(), int(number)), []).append((edge, other, splice))
    rows, seen_edges = [], set()
    for start in numbers:
        pending = deque([(cable_id, int(start))])
        while pending:
            current, number = pending.popleft()
            for edge, other, splice in index.get((current.casefold(), number), []):
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                next_cable = text_of(splice.get(f"{other}_cable_id"))
                next_number = splice.get(f"{other}_strand_no")
                next_number = int(next_number) if next_number not in (None, "") else None
                if (splice.get('splice_type') or '').upper() == 'OPEN':
                    next_cable, next_number = None, None
                rows.append({"a_cable_id": current, "a_strand_no": number,
                             "b_cable_id": next_cable, "b_strand_no": next_number,
                             "splice_type": splice.get("splice_type") or "splice",
                             "splice_enc_id": splice.get("splice_enc_id"),
                             "a_layer_id": splice.get('b_layer_id' if other == 'a' else 'a_layer_id'),
                             "b_layer_id": splice.get(f'{other}_layer_id')})
                if (splice.get("splice_type") or "").upper() != "OPEN" and next_cable and next_number:
                    pending.append((next_cable, next_number))
    return rows


def _duct_labels(cable_id: str, layer_id: str, routes: list[dict]) -> list[str]:
    matched = []
    for route in routes:
        if not text_of(route.get("cable_id")) or text_of(route.get("cable_id")).casefold() != (cable_id or "").casefold():
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
