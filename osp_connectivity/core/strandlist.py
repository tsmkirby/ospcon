"""Every strand of one fiber cable, with the circuit it carries, as ``A end ↔︎ Z end-1``."""

from __future__ import annotations

from .catalog import DEVICE_ROLES, feature_id
from .fields import parse_count, text_of
from .fiberpath import _describe, _ranges, fiber_paths
from .naming import local_asset_id
from .notation import LINK
from .panels import panel_is_simplex
from .strands import compress_splices


def _same(left, right) -> bool:
    return bool(text_of(left)) and (text_of(left) or "").casefold() == (text_of(right) or "").casefold()


def strand_count(cable: dict, tables: dict) -> int:
    """The cable's strand count, or the highest strand any saved record uses."""
    count = parse_count((cable.get("values") or {}).get("strand_count"))
    if count:
        return count
    identifier = feature_id(cable)
    numbers = [0]
    for row in tables.get("strands") or []:
        if _same(row.get("cable_id"), identifier):
            numbers.append(_int(row.get("strand_no")))
    for row in tables.get("splices") or []:
        for side in ("a", "b"):
            if _same(row.get(f"{side}_cable_id"), identifier):
                numbers.append(_int(row.get(f"{side}_strand_no")))
    for row in tables.get("terminations") or []:
        if _same(row.get("cable_id"), identifier):
            numbers.append(_int(row.get("strand_no")))
    return max(numbers)


def strand_list(cable: dict, features: list[dict], tables: dict) -> dict:
    """One row per strand: its circuit label, the ends it reaches, and its splices."""
    identifier = feature_id(cable) or ""
    count = strand_count(cable, tables)
    found = fiber_paths(identifier, cable.get("layer_id") or "", list(range(1, count + 1)), features, tables)
    paths = found["paths"]
    rows = [{"number": path["number"], "circuit": "", "index": 0, "size": 0,
             "a_end": "", "z_end": "", "source": "",
             "route": f"{_end_text(_a_term(path, features), features)}{LINK}{_end_text(_z_term(path, features), features)}",
             "splices": compress_splices(path["hops"])} for path in paths]

    # 1. A circuit record that covers this fiber anywhere on its route.
    circuits = [row for row in tables.get("circuits") or []
                if row.get("strand_from") not in (None, "") and (text_of(row.get("media_type")) or "fiber").casefold() == "fiber"]
    pending = []
    for row, path in zip(rows, paths):
        record, position = _circuit_for(path, circuits)
        if record is None:
            pending.append((row, path))
            continue
        first, last = _int(record.get("strand_from")), _int(record.get("strand_to")) or _int(record.get("strand_from"))
        room = text_of(record.get("idf_id")) or ""
        panel = text_of(record.get("panel_id")) or ""
        z_end = _panel_end(room, panel, text_of(record.get("panel_port")) or "")
        a_end = text_of(record.get("device_id")) or _device_end(path, features) or _other_end(path, z_end, features)
        _label(row, a_end, z_end, position - first + 1, last - first + 1, "circuit record")

    # 2. Otherwise fibers on the same duplex port pairs, going to the same ends, form one circuit.
    groups: dict[tuple, list[tuple[dict, dict]]] = {}
    for row, path in pending:
        key = _pair_key(path, features)
        if key is not None:
            groups.setdefault(key, []).append((row, path))
    for members in groups.values():
        members.sort(key=lambda item: (_z_term(item[1], features).get("port") or 0, item[1]["number"]))
        device = _device_end(members[0][1], features)
        z_terms = [_z_term(path, features) for _row, path in members]
        z_end = _terms_end(z_terms)
        a_end = device or _terms_end([_a_term(path, features) for _row, path in members])
        for position, (row, _path) in enumerate(members, 1):
            _label(row, a_end, z_end, position, len(members), "duplex ports" if len(members) > 1 else "single port")
    return {"cable_id": identifier, "count": count, "rows": rows, "warnings": found["warnings"]}


def _end_text(term, features):
    """One end of one strand's route; a device reads as its id rather than a missing panel."""
    device = _device_at(term, features)
    if device is not None and not term.get("panel"):
        return f"{feature_id(device)} ({term['cable']}:{term['number']})"
    return _describe([term])


def _label(row, a_end, z_end, index, size, source):
    row.update({"a_end": a_end, "z_end": z_end, "index": index, "size": size, "source": source,
                "circuit": f"{a_end or 'unknown'}{LINK}{z_end or 'unknown'}-{index}"})


def _circuit_for(path, circuits):
    """The circuit record covering any cable and fiber this path passes through, with that fiber's number."""
    stops = [(path["cable"], path["number"])]
    for hop in path["hops"]:
        stops.append((hop["a_cable_id"], hop["a_strand_no"]))
        stops.append((hop["b_cable_id"], hop["b_strand_no"]))
    for cable_id, number in stops:
        for record in circuits:
            first = _int(record.get("strand_from"))
            last = _int(record.get("strand_to")) or first
            if _same(record.get("cable_id"), cable_id) and first <= (number or 0) <= last:
                return record, number
    return None, None


def _is_panel(term):
    return term.get("kind") == "end" and bool(term.get("panel")) and term.get("port") is not None


def _device_at(term, features):
    """The device a termination reaches: named on the cable end, or one served by the end cable."""
    if term.get("kind") != "end":
        return None
    for feature in features:
        if feature.get("role") in DEVICE_ROLES and _same(feature_id(feature), term.get("place")):
            return feature
    if term.get("panel"):
        return None
    for feature in features:
        if feature.get("role") in DEVICE_ROLES and _same((feature.get("values") or {}).get("cable_ref"), term.get("cable")):
            return feature
    return None


def _device_end(path, features):
    for side in ("start", "end"):
        device = _device_at(path[side], features)
        if device is not None:
            return feature_id(device)
    return ""


def _a_term(path, features):
    """The first termination on the route, or the device when the route ends at one, so a device is always A."""
    if _device_at(path["end"], features) is not None and _device_at(path["start"], features) is None:
        return path["end"]
    return path["start"]


def _z_term(path, features):
    """The last termination on the route: whichever end is not A."""
    return path["end"] if _a_term(path, features) is path["start"] else path["start"]


def _other_end(path, z_end, features):
    a = _a_term(path, features)
    return _terms_end([a]) if _is_panel(a) else ""


def _panel_end(room, panel, ports):
    name = local_asset_id(panel, room)
    text = f"{name}:{ports}" if name and ports else name
    return " ".join(part for part in (room, text) if part)


def _terms_end(terms):
    first = terms[0]
    if not _is_panel(first):
        return ""
    return _panel_end(first.get("place") or "", first["panel"], _ranges([term["port"] for term in terms]))


def _pair_key(path, features):
    """Fibers with equal keys are one circuit. None unless both ends reach a panel port or a device."""
    parts = []
    for side in ("start", "end"):
        term = path[side]
        device = _device_at(term, features)
        if device is not None:
            parts.append(("device", feature_id(device).casefold()))
        elif _is_panel(term):
            simplex = _panel_simplex(term["panel"], features)
            pair = term["port"] if simplex else (term["port"] + 1) // 2
            parts.append(("panel", (term.get("place") or "").casefold(), term["panel"].casefold(), pair, simplex))
        else:
            return None  # open, unspliced, or unrecorded: not a circuit
    return tuple(parts)


def _panel_simplex(panel_id, features):
    for feature in features:
        if feature.get("role") in ("patch_panel", "fdp") and _same(feature_id(feature), panel_id):
            return panel_is_simplex(feature)
    return False


def _int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
