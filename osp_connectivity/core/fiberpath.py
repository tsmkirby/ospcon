"""The ordered path of individual fibers, from one termination to the other."""

from __future__ import annotations

from .catalog import feature_id
from .fields import parse_range, text_of
from .geometry import distance
from .notation import fiber_ranges
from .panels import _rear_assignments
from .strands import compress_splices

OPEN = "OPEN"
_LIMIT = 500  # A fiber never crosses more splices than this; more means the saved splices loop.


def _same(left, right) -> bool:
    return (text_of(left) or "").casefold() == (text_of(right) or "").casefold() and bool(text_of(left))


def fiber_routes(cable_id: str, layer_id: str, numbers: list[int], features: list[dict], tables: dict) -> dict:
    """Follow each fiber both ways from ``cable_id`` to its two ends.

    Returns ``routes``: one entry per group of fibers that share a path, each
    with its ordered ``hops`` (rows oriented from the ``start`` end to the
    ``end`` end), the ``cables`` and ``enclosures`` in path order, and both
    terminations. ``warnings`` names saved splices that cannot be followed.
    """
    found = fiber_paths(cable_id, layer_id, numbers, features, tables)
    return {"routes": _group(found["paths"], features, tables), "warnings": found["warnings"]}


def fiber_paths(cable_id: str, layer_id: str, numbers: list[int], features: list[dict], tables: dict) -> dict:
    """One path per fiber: its ordered ``hops`` and its ``start`` and ``end`` terminations, ports resolved."""
    splices = _distinct(tables.get("splices") or [])
    index: dict[tuple[str, int], list[int]] = {}
    for position, row in enumerate(splices):
        for side in ("a", "b"):
            identifier, number = text_of(row.get(f"{side}_cable_id")), _number(row.get(f"{side}_strand_no"))
            if identifier and number is not None:
                index.setdefault((identifier.casefold(), number), []).append(position)
    cables = [feature for feature in features if feature.get("role") == "cable" and feature_id(feature)]
    enclosures = {feature_id(feature).casefold(): feature for feature in features
                  if feature.get("role") == "splice_enc" and feature_id(feature)}
    start = _cable(cables, cable_id, layer_id)
    # A cable end that names no place still has one when its panel belongs to an IDF.
    panel_rooms = {feature_id(feature).casefold(): text_of((feature.get("values") or {}).get("idf_ref")) or ""
                   for feature in features if feature.get("role") in ("patch_panel", "fdp") and feature_id(feature)}
    warnings: list[str] = []
    paths = []
    for number in numbers:
        path = _walk(start, cable_id, number, splices, index, cables, enclosures, warnings)
        for side in ("start", "end"):
            term = path[side]
            if term.get("kind") == "end":
                term["panel"], term["port"] = _ports(term, features, tables)
                if not term.get("place") and term["panel"]:
                    term["place"] = panel_rooms.get(term["panel"].casefold(), "")
        paths.append(path)
    return {"paths": paths, "warnings": list(dict.fromkeys(warnings))}


def _distinct(rows):
    """Each physical splice once, however many times or in whichever direction it was saved."""
    seen, kept = set(), []
    for row in rows:
        ends = frozenset(((text_of(row.get(f"{side}_cable_id")) or "").casefold(), _number(row.get(f"{side}_strand_no")))
                         for side in ("a", "b"))
        key = ((text_of(row.get("splice_enc_id")) or "").casefold(), ends)
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return kept


def _walk(start, cable_id, number, splices, index, cables, enclosures, warnings):
    """One fiber: the hops from one end to the other, oriented along the start cable from a to z."""
    start_id = feature_id(start) if start else text_of(cable_id)
    rows = [position for position in index.get((start_id.casefold(), number), [])]
    if len(rows) > 2:
        warnings.append(f"{start_id} fiber {number} is spliced {len(rows)} times; only two splices can be followed.")
    by_end = {"a": None, "z": None}
    for position in rows[:2]:
        end = _end_at(start, splices[position].get("splice_enc_id"), enclosures)
        if end is None or by_end[end] is not None:
            end = "z" if by_end["z"] is None else "a"
        by_end[end] = position
    sides = {}
    seen: set[int] = set()  # shared, so a ring of splices is listed once, not once from each side
    for end in ("z", "a"):
        sides[end] = _outward(start_id, start, number, end, by_end[end], splices, index, cables, enclosures, warnings, seen)
    # The a side was walked outward, so reverse it to read from its far end in toward the start cable.
    a_hops, a_term = sides["a"]
    z_hops, z_term = sides["z"]
    hops = [_flip(hop) for hop in reversed(a_hops)] + z_hops
    return {"number": number, "hops": hops, "start": a_term, "end": z_term, "cable": start_id,
            "layer_id": (start or {}).get("layer_id") or ""}


def _outward(cable_id, cable, number, end, position, splices, index, cables, enclosures, warnings, seen):
    """Hops from ``cable``'s ``end`` outward until the fiber terminates."""
    hops = []
    while position is not None:
        if position in seen or len(hops) >= _LIMIT:
            warnings.append(f"The saved splices for {cable_id} fiber {number} loop back on themselves; "
                            "the route stops where the loop starts.")
            return hops, {"kind": "loop"}
        seen.add(position)
        row = splices[position]
        here = "a" if _same(row.get("a_cable_id"), cable_id) and _number(row.get("a_strand_no")) == number else "b"
        there = "b" if here == "a" else "a"
        enclosure = text_of(row.get("splice_enc_id")) or ""
        kind = (text_of(row.get("splice_type")) or "splice").upper()
        next_id, next_number = text_of(row.get(f"{there}_cable_id")), _number(row.get(f"{there}_strand_no"))
        if kind == OPEN or not next_id or next_number is None:
            return hops, {"kind": "open", "enclosure": enclosure, "cable": cable_id, "number": number}
        hops.append(_hop(cable_id, number, next_id, next_number, enclosure, kind, row, here, there))
        cable = _cable(cables, next_id, row.get(f"{there}_layer_id"))
        cable_id, number = feature_id(cable) if cable else next_id, next_number
        entry = _end_at(cable, enclosure, enclosures)
        candidates = [other for other in index.get((cable_id.casefold(), number), []) if other != position]
        if len(candidates) > 1:
            warnings.append(f"{cable_id} fiber {number} is spliced {len(candidates) + 1} times; only two splices can be followed.")
        position = candidates[0] if candidates else None
        end = "z" if entry == "a" else "a" if entry == "z" else None
    return hops, _termination(cable, cable_id, number, end, enclosures)


def _hop(a_id, a_number, b_id, b_number, enclosure, kind, row, here, there):
    return {"a_cable_id": a_id, "a_strand_no": a_number, "b_cable_id": b_id, "b_strand_no": b_number,
            "splice_enc_id": enclosure, "splice_type": kind,
            "a_layer_id": row.get(f"{here}_layer_id") or "", "b_layer_id": row.get(f"{there}_layer_id") or ""}


def _flip(hop):
    flipped = dict(hop)
    for key in ("cable_id", "strand_no", "layer_id"):
        flipped[f"a_{key}"], flipped[f"b_{key}"] = hop[f"b_{key}"], hop[f"a_{key}"]
    return flipped


def _termination(cable, cable_id, number, end, enclosures=None):
    """Where a fiber leaves the plant at ``cable``'s ``end``: the IDF or device, panel, and port."""
    values = (cable or {}).get("values") or {}
    if end is None:
        # The splice gave no hint which end it was at; a panel on only one end is the termination.
        with_panel = [side for side in ("a", "z") if text_of(values.get(f"{side}_panel"))]
        end = with_panel[0] if len(with_panel) == 1 else None
    if end is None:
        return {"kind": "end", "cable": cable_id, "number": number, "end": None, "place": "", "panel": "", "port": None}
    place = text_of(values.get(f"{end}_end")) or ""
    if place and place.casefold() in (enclosures or {}):
        # The fiber reaches an enclosure with no splice recorded for it there.
        return {"kind": "unspliced", "cable": cable_id, "number": number, "end": end, "enclosure": place}
    return {"kind": "end", "cable": cable_id, "number": number, "end": end,
            "place": place, "panel": text_of(values.get(f"{end}_panel")) or "",
            "port_text": text_of(values.get(f"{end}_port")) or "", "port": None}


def _end_at(cable, enclosure_id, enclosures):
    """Which end of ``cable`` an enclosure is at: by its end labels, then by geometry."""
    if not cable or not text_of(enclosure_id):
        return None
    values = cable.get("values") or {}
    labeled = [end for end in ("a", "z") if _same(values.get(f"{end}_end"), enclosure_id)]
    if len(labeled) == 1:
        return labeled[0]
    enclosure = enclosures.get(text_of(enclosure_id).casefold())
    coords = cable.get("coords") or []
    point = ((enclosure or {}).get("coords") or [None])[0]
    if point and len(coords) >= 2:
        first, last = distance(point, coords[0]), distance(point, coords[-1])
        if first != last:
            return "a" if first < last else "z"
    return None


def _cable(cables, identifier, layer_id=""):
    found = [cable for cable in cables if _same(feature_id(cable), identifier)]
    exact = [cable for cable in found if layer_id and cable.get("layer_id") == layer_id]
    return (exact or found or [None])[0]


def _number(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _ports(term, features, tables):
    """The panel port each terminating fiber lands on, from fiber terminations or the cable's end."""
    for row in tables.get("terminations") or []:
        if _same(row.get("cable_id"), term["cable"]) and _number(row.get("strand_no")) == term["number"]:
            panel = text_of(row.get("pp_id") or row.get("fdp_id") or row.get("panel_id"))
            port = text_of(row.get("panel_port") or row.get("panel_position") or row.get("port"))
            if panel:
                return panel, _number(port)
    if not term.get("panel"):
        return "", None
    ports = parse_range(term.get("port_text"))
    if not ports:
        cable = next((item for item in features if item.get("role") == "cable" and _same(feature_id(item), term["cable"])), None)
        ports = next((numbers for item, numbers in _rear_assignments(term["panel"], features) if item is cable), [])
    port = ports[term["number"] - 1] if 0 < term["number"] <= len(ports) else None
    return term["panel"], port


def _group(paths, features, tables):
    """Fibers that take the same path, compressed into ranges."""
    groups: dict[tuple, list[dict]] = {}
    for path in paths:
        key = (tuple((hop["a_cable_id"].casefold(), (hop["b_cable_id"] or "").casefold(), hop["splice_enc_id"].casefold(),
                      hop["splice_type"]) for hop in path["hops"]),
               _term_key(path["start"]), _term_key(path["end"]))
        groups.setdefault(key, []).append(path)
    routes = []
    for members in groups.values():
        hops = [compress_splices([member["hops"][position] for member in members])
                for position in range(len(members[0]["hops"]))]
        cables, enclosures = [], []
        for hop in members[0]["hops"]:
            for identifier in (hop["a_cable_id"], hop["b_cable_id"]):
                if identifier and identifier not in cables:
                    cables.append(identifier)
            if hop["splice_enc_id"] and hop["splice_enc_id"] not in enclosures:
                enclosures.append(hop["splice_enc_id"])
        if not cables:
            cables.append(members[0]["cable"])
        starts = [member["start"] for member in members]
        ends = [member["end"] for member in members]
        routes.append({
            "numbers": [member["number"] for member in members],
            "rows": [member["hops"][position] for position in range(len(members[0]["hops"])) for member in members],
            "start_place": starts[0].get("place") or "", "end_place": ends[0].get("place") or "",
            "start_panel": starts[0].get("panel") or "", "end_panel": ends[0].get("panel") or "",
            "start_ports": _ranges([term["port"] for term in starts]) if all(term.get("port") is not None for term in starts) else "",
            "end_ports": _ranges([term["port"] for term in ends]) if all(term.get("port") is not None for term in ends) else "",
            "cable": members[0]["cable"],
            "hops": [line for lines in hops for line in lines],
            "cables": cables,
            "enclosures": enclosures,
            "start": _describe([member["start"] for member in members]),
            "end": _describe([member["end"] for member in members]),
            "start_term": members[0]["start"],
            "end_term": members[0]["end"],
        })
    return routes


def _term_key(term):
    return (term.get("kind"), (term.get("cable") or "").casefold(), term.get("end"), (term.get("place") or "").casefold(),
            (term.get("panel") or "").casefold(), (term.get("enclosure") or "").casefold())


def _describe(terms):
    """One end of a group of fibers, such as ``IDF-1 PP1:1-2`` or ``OPEN @ EN-1``."""
    first = terms[0]
    if first.get("kind") == "loop":
        return "Route loops; see warnings"
    fibers = ", ".join(fiber_ranges(first["cable"], [term["number"] for term in terms]))
    if first.get("kind") == "open":
        return f"{fibers} left OPEN @ {first.get('enclosure')}"
    if first.get("kind") == "unspliced":
        return f"{fibers} not spliced @ {first.get('enclosure')}"
    place = first.get("place") or ""
    ports = [term.get("port") for term in terms]
    if first.get("panel") and all(port is not None for port in ports):
        panel = f"{first['panel']}:{_ranges(ports)}"
        return " ".join(part for part in (place, panel) if part) + f" ({fibers})"
    if first.get("panel"):
        return " ".join(part for part in (place, first["panel"], "port unknown") if part) + f" ({fibers})"
    if place:
        return f"{place}, no panel recorded ({fibers})"
    return f"{fibers}, cable end not recorded"


def _ranges(numbers):
    ordered = sorted(set(numbers))
    parts, start, previous = [], ordered[0], ordered[0]
    for number in ordered[1:] + [None]:
        if number is not None and number == previous + 1:
            previous = number
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        if number is not None:
            start = previous = number
    return ", ".join(parts)


def route_lines(routes) -> list[str]:
    """Each route as From, its splices in order, and To."""
    lines = []
    for route in routes:
        if len(routes) > 1:
            if lines:
                lines.append("")
            lines.append("Fibers " + ", ".join(fiber_ranges(route["cable"], route["numbers"])) + ":")
        lines.append(f"From: {route['start']}")
        lines.extend(route["hops"])
        lines.append(f"To: {route['end']}")
    return lines
