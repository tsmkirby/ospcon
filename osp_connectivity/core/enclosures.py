"""Fiber cables that end at a splice enclosure, and the splices edited there."""

from __future__ import annotations

from .catalog import feature_id
from .fields import media_of, parse_count, text_of
from .geometry import distance, split_linestring
from .place import nearer_end_name
from .strands import build_splice_rows


def enclosure_hits(features: list[dict], point, tolerance: float, selected=None) -> dict:
    """Fiber cables a new enclosure at ``point`` attaches to.

    ``selected`` holds the (layer_id, fid) of selected features. When any of
    them is a cable, only those cables are used; otherwise every fiber cable
    at the point is. The enclosure is snapped onto the nearest through cable,
    so a click on a cable drawn apart from its stack, or anywhere within the
    tolerance, still puts the enclosure on the stored line, and every cable is
    split and labeled at that one spot.
    """
    selected = set(selected or ())
    cables = [feature for feature in features if feature.get("role") == "cable"]
    chosen = [feature for feature in cables if (feature.get("layer_id"), feature.get("fid")) in selected]
    limited = bool(chosen)
    candidates = [feature for feature in (chosen or cables) if media_of(feature.get("values") or {}) == "fiber"]

    def classify(at):
        hits = []
        for feature in candidates:
            result = split_linestring(feature.get("coords") or [], at, tolerance)
            if result["kind"] in ("through", "endpoint"):
                hits.append((feature, result))
        hits.sort(key=lambda hit: hit[1]["distance"])
        return hits

    hits = classify(point)
    through = [hit for hit in hits if hit[1]["kind"] == "through"]
    snapped = tuple(through[0][1]["at"]) if through else tuple(point)
    if through and distance(snapped, point) > 0:
        hits = classify(snapped)
        through = [hit for hit in hits if hit[1]["kind"] == "through"]
    return {
        "point": snapped,
        "through": through,
        "laterals": [hit for hit in hits if hit[1]["kind"] == "endpoint"],
        "limited": limited,
        "selected_cables": len(chosen),
        "selected_fiber": sum(1 for feature in chosen if media_of(feature.get("values") or {}) == "fiber"),
    }


def enclosure_end_links(line, features: list[dict], tolerance: float, cable_id: str, layer_id: str) -> list[dict]:
    """Enclosures a new fiber cable is drawn into, one per end."""
    if len(line) < 2 or not cable_id:
        return []
    links = []
    seen = set()
    for feature in features:
        if feature.get("role") != "splice_enc":
            continue
        point = (feature.get("coords") or [None])[0]
        identifier = feature_id(feature)
        if not point or not identifier or identifier.casefold() in seen:
            continue
        end = _end_at(line, point, tolerance)
        if not end:
            continue
        seen.add(identifier.casefold())
        links.append(
            {
                "end": end,
                "enclosure_id": identifier,
                "membership": {
                    "splice_enc_id": identifier,
                    "cable_layer_id": layer_id,
                    "cable_id": cable_id,
                },
            }
        )
    return links


def enclosure_association(enclosure: dict, features: list[dict], tables: dict, tolerance: float) -> dict:
    """Cables already at this enclosure, plus fiber cables that end here and are not spliced yet."""
    enc_id = feature_id(enclosure) or ""
    point = (enclosure.get("coords") or [None])[0]
    touching = []
    for feature in features:
        if feature.get("role") != "cable" or media_of(feature.get("values") or {}) != "fiber":
            continue
        if _cable_ends_here(feature, enc_id, point, tolerance):
            touching.append(feature)
    member_ids = _ids(
        text_of(row.get("cable_id"))
        for row in tables.get("memberships") or []
        if _same(row.get("splice_enc_id"), enc_id)
    )
    splice_rows = [row for row in tables.get("splices") or [] if _same(row.get("splice_enc_id"), enc_id)]
    spliced_ids = _ids(
        text_of(row.get(key))
        for row in splice_rows
        for key in ("a_cable_id", "b_cable_id")
    )
    labeled_ids = _ids(
        feature_id(feature)
        for feature in touching
        if _same((feature.get("values") or {}).get("a_end"), enc_id) or _same((feature.get("values") or {}).get("z_end"), enc_id)
    )
    known = {item.casefold() for item in (*member_ids, *spliced_ids, *labeled_ids)}
    spliced = {item.casefold() for item in spliced_ids}
    unadded = [feature for feature in touching if (feature_id(feature) or "").casefold() not in spliced]
    from_id, express_id = _express_pair(splice_rows)
    ordered = _order_cables(touching, from_id, known)
    from_cable = ordered[0] if ordered else None
    if from_cable and not from_id:
        from_id = feature_id(from_cable) or ""
    if not express_id:
        express_id = next(
            (feature_id(feature) for feature in ordered[1:] if (feature_id(feature) or "").casefold() in known),
            "",
        )
    targets = _targets(ordered, from_id, express_id, unadded)
    count = _strand_count(from_cable, splice_rows, from_id)
    return {
        "enclosure": enclosure,
        "cables": ordered,
        "unadded": unadded,
        "from_cable": from_cable,
        "targets": targets,
        "strand_count": count,
        "opens": _open_text(splice_rows, from_id, count),
        "fusions": _fusion_groups(splice_rows, from_id, express_id),
        "splices": splice_rows,
    }


def enclosure_edits(association: dict, choices: dict | None, tables: dict, tolerance: float | None = None) -> dict:
    """Membership and end writes for every cable here, plus a replacement splice map when one was edited."""
    enclosure = association["enclosure"]
    enc_id = feature_id(enclosure) or ""
    point = (enclosure.get("coords") or [None])[0]
    member_ids = {
        (text_of(row.get("cable_id")) or "").casefold()
        for row in tables.get("memberships") or []
        if _same(row.get("splice_enc_id"), enc_id)
    }
    edits = []
    memberships = []
    for cable in association.get("cables") or []:
        cable_id = feature_id(cable)
        if not cable_id:
            continue
        if cable_id.casefold() not in member_ids:
            memberships.append(
                {
                    "splice_enc_id": enc_id,
                    "cable_layer_id": cable.get("layer_id"),
                    "cable_id": cable_id,
                }
            )
        values = cable.get("values") or {}
        if _same(values.get("a_end"), enc_id) or _same(values.get("z_end"), enc_id):
            continue
        coords = cable.get("coords") or []
        if tolerance is not None and (not point or len(coords) < 2 or not _end_at(coords, point, tolerance)):
            continue
        end = nearer_end_name(coords, point)
        if text_of(values.get(end)):
            continue
        edits.append(
            {
                "layer_id": cable.get("layer_id"),
                "fid": cable.get("fid"),
                "canonical": end,
                "value": enc_id,
            }
        )
    deletes = []
    splices = []
    strands = []
    from_cable = association.get("from_cable")
    from_id = feature_id(from_cable) if from_cable else ""
    if choices and from_id:
        for row in association.get("splices") or []:
            if _same(row.get("a_cable_id"), from_id) and row.get("_fid") is not None:
                deletes.append((row.get("_layer_id"), row.get("_fid")))
        target = (association.get("targets") or [None])[0]
        express_id = (target or {}).get("id") or ""
        rows = []
        if express_id or choices.get("fusions"):
            rows = build_splice_rows(
                int(choices.get("strand_count") or association.get("strand_count") or 1),
                from_id,
                express_id or from_id,
                enc_id,
                choices.get("fusions") or [],
                choices.get("opens") or [],
                (from_cable or {}).get("layer_id") or "",
                (target or {}).get("layer_id") or "",
            )
            if not express_id:
                rows = [row for row in rows if row.get("splice_type") != "EXPRESS"]
        strand_keys = _strand_keys(tables.get("strands") or [])
        count = int(choices.get("strand_count") or association.get("strand_count") or 0)
        for row in rows:
            row["splice_id"] = (
                f"{enc_id}.{row['a_cable_id']}.{row['a_strand_no']}.{row.get('b_cable_id')}.{row.get('b_strand_no')}"
            )
            splices.append(row)
        for cable in [from_cable, *(item.get("feature") for item in association.get("targets") or [])]:
            if not cable:
                continue
            cable_id = feature_id(cable)
            for number in range(1, count + 1):
                key = ((cable.get("layer_id") or ""), cable_id.casefold(), number)
                if key in strand_keys:
                    continue
                strand_keys.add(key)
                strands.append(
                    {
                        "strand_id": f"{cable_id}.{number}",
                        "source_layer_id": cable.get("layer_id") or "",
                        "cable_id": cable_id,
                        "strand_no": number,
                        "status": "spare",
                    }
                )
    return {"edits": edits, "memberships": memberships, "delete": deletes, "splices": splices, "strands": strands}


def _end_at(line, point, tolerance: float) -> str | None:
    if distance(line[0], point) <= tolerance:
        return "a_end"
    if distance(line[-1], point) <= tolerance:
        return "z_end"
    return None


def _cable_ends_here(cable: dict, enc_id: str, point, tolerance: float) -> bool:
    values = cable.get("values") or {}
    if _same(values.get("a_end"), enc_id) or _same(values.get("z_end"), enc_id):
        return True
    coords = cable.get("coords") or []
    if not point or len(coords) < 2:
        return False
    return distance(coords[0], point) <= tolerance or distance(coords[-1], point) <= tolerance


def _same(left, right) -> bool:
    text = text_of(left)
    other = text_of(right)
    return bool(text) and bool(other) and text.casefold() == other.casefold()


def _ids(values) -> list[str]:
    found = []
    for value in values:
        text = text_of(value)
        if text and text.casefold() not in {item.casefold() for item in found}:
            found.append(text)
    return found


def _express_pair(rows: list[dict]) -> tuple[str, str]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        if (text_of(row.get("splice_type")) or "").upper() != "EXPRESS":
            continue
        if row.get("b_strand_no") in (None, "") or int(row["a_strand_no"]) != int(row["b_strand_no"]):
            continue
        pair = (text_of(row.get("a_cable_id")) or "", text_of(row.get("b_cable_id")) or "")
        if not pair[0] or not pair[1]:
            continue
        counts[pair] = counts.get(pair, 0) + 1
    if not counts:
        return "", ""
    return max(counts, key=counts.get)


def _order_cables(cables: list[dict], from_id: str, known: set[str]) -> list[dict]:
    def rank(feature: dict) -> tuple:
        identifier = (feature_id(feature) or "").casefold()
        return (0 if from_id and identifier == from_id.casefold() else 1, 0 if identifier in known else 1, identifier)

    return sorted(cables, key=rank)


def _targets(cables: list[dict], from_id: str, express_id: str, unadded: list[dict]) -> list[dict]:
    unadded_ids = {(feature_id(feature) or "").casefold() for feature in unadded}
    others = [feature for feature in cables if not _same(feature_id(feature), from_id)]

    def rank(feature: dict) -> tuple:
        identifier = feature_id(feature) or ""
        return (0 if _same(identifier, express_id) else 1, 1 if identifier.casefold() in unadded_ids else 0, identifier)

    targets = []
    for feature in sorted(others, key=rank):
        identifier = feature_id(feature)
        if not identifier:
            continue
        label = f"{identifier} (new)" if identifier.casefold() in unadded_ids else identifier
        targets.append(
            {
                "id": identifier,
                "layer_id": feature.get("layer_id") or "",
                "label": label,
                "feature": feature,
            }
        )
    return targets


def _strand_count(cable: dict | None, rows: list[dict], from_id: str) -> int:
    count = parse_count((cable or {}).get("values", {}).get("strand_count") if cable else None) or 0
    for row in rows:
        if from_id and not _same(row.get("a_cable_id"), from_id):
            continue
        count = max(count, int(row.get("a_strand_no") or 0))
    return count or 12


def _open_text(rows: list[dict], from_id: str, count: int) -> str:
    if not from_id:
        return ""
    used = {
        int(row["a_strand_no"])
        for row in rows
        if _same(row.get("a_cable_id"), from_id) and row.get("a_strand_no") not in (None, "")
    }
    if not used:
        return ""
    return _range_text([number for number in range(1, count + 1) if number not in used])


def _fusion_groups(rows: list[dict], from_id: str, express_id: str) -> list[dict]:
    chosen = []
    for row in rows:
        if not from_id or not _same(row.get("a_cable_id"), from_id):
            continue
        kind = (text_of(row.get("splice_type")) or "").upper()
        b_cable = text_of(row.get("b_cable_id")) or ""
        if row.get("b_strand_no") in (None, ""):
            continue
        a_no = int(row["a_strand_no"])
        b_no = int(row["b_strand_no"])
        if kind == "EXPRESS" and _same(b_cable, express_id) and a_no == b_no:
            continue
        chosen.append((a_no, b_cable, b_no, text_of(row.get("b_layer_id")) or ""))
    chosen.sort()
    groups = []
    for a_no, b_cable, b_no, b_layer in chosen:
        current = groups[-1] if groups else None
        if (
            current
            and current["b_cable_id"] == b_cable
            and a_no == current["a_to"] + 1
            and b_no == current["b_start"] + (current["a_to"] - current["a_from"]) + 1
        ):
            current["a_to"] = a_no
            continue
        groups.append(
            {
                "a_from": a_no,
                "a_to": a_no,
                "b_cable_id": b_cable,
                "b_layer_id": b_layer,
                "b_start": b_no,
            }
        )
    return groups


def _range_text(numbers: list[int]) -> str:
    if not numbers:
        return ""
    ordered = sorted(set(numbers))
    ranges = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)


def _strand_keys(rows: list[dict]) -> set:
    keys = set()
    for row in rows:
        cable_id = text_of(row.get("cable_id"))
        if not cable_id or row.get("strand_no") in (None, ""):
            continue
        keys.add((text_of(row.get("source_layer_id")) or "", cable_id.casefold(), int(row["strand_no"])))
    return keys
