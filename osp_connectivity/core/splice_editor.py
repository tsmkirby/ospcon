"""Lossless range editing and validation of every connection in an enclosure."""

from __future__ import annotations

import re
from uuid import uuid4

from .catalog import feature_id
from .fields import media_of, parse_count, text_of
from .geometry import distance


def same(left, right):
    return (text_of(left) or "").casefold() == (text_of(right) or "").casefold()


def cable_key(cable):
    return (cable.get("layer_id") or "", (feature_id(cable) or "").casefold())


def editor_model(enclosure, features, tables, tolerance=1.0):
    enc_id = feature_id(enclosure)
    rows = [dict(row) for row in tables.get("splices", []) if same(row.get("splice_enc_id"), enc_id)]
    members = [row for row in tables.get("memberships", []) if same(row.get("splice_enc_id"), enc_id)]
    point = (enclosure.get("coords") or [None])[0]
    cables = []
    for feature in features:
        if feature.get("role") != "cable" or not feature_id(feature):
            continue
        values = feature.get("values") or {}
        linked = any(same(row.get("cable_id"), feature_id(feature))
                     and (not row.get("cable_layer_id") or row["cable_layer_id"] == feature.get("layer_id")) for row in members)
        linked |= any(same(row.get(f"{side}_cable_id"), feature_id(feature))
                      and (not row.get(f"{side}_layer_id") or row[f"{side}_layer_id"] == feature.get("layer_id"))
                      for row in rows for side in ("a", "b"))
        labeled = any(same(values.get(end), enc_id) for end in ("a_end", "z_end"))
        coords = feature.get("coords") or []
        touching = bool(point and coords and min(distance(point, coords[0]), distance(point, coords[-1])) <= tolerance)
        if not (linked or labeled or (touching and media_of(values) == "fiber")):
            continue
        cable = dict(feature)
        count = parse_count(values.get("strand_count"))
        inventory = [int(row["strand_no"]) for row in tables.get("strands", [])
                     if same(row.get("cable_id"), feature_id(feature)) and row.get("strand_no")
                     and (not row.get("source_layer_id") or row["source_layer_id"] == feature.get("layer_id"))]
        cable["capacity"] = count or max(inventory, default=0)
        cable["capacity_inferred"] = not bool(count)
        cable["new"] = not linked and not labeled
        cable["label"] = f"{feature_id(feature)} — {feature.get('layer_name') or feature.get('layer_id') or 'cable'}"
        cables.append(cable)
    # Preserve references even if a cable layer has been removed from the project.
    for row in rows:
        for side in ("a", "b"):
            identifier = text_of(row.get(f"{side}_cable_id"))
            if identifier and not any(same(feature_id(cable), identifier) for cable in cables):
                cables.append({"id": identifier, "layer_id": row.get(f"{side}_layer_id") or "",
                               "capacity": 0, "missing": True,
                               "label": f"{identifier} (missing cable)", "values": {}})
    return {"enclosure": enclosure, "cables": cables, "original": rows,
            "groups": group_rows(rows), "memberships": members}


def _resolve(cables, identifier, layer=""):
    found = [cable for cable in cables if same(feature_id(cable), identifier)]
    exact = [cable for cable in found if layer and cable.get("layer_id") == layer]
    return exact[0] if len(exact) == 1 else found[0] if len(found) == 1 else None


def _strand(value):
    """A stored strand number, or None when it is blank or not a whole number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return int(number) if number.is_integer() else None


def _loss(value):
    """Loss as a number; numeric text from a string field is accepted, anything else is None."""
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def group_rows(rows):
    """Compress contiguous, equivalent rows while retaining every original attribute."""
    ordered = sorted(rows, key=lambda row: (
        str(row.get("a_layer_id") or ""), str(row.get("a_cable_id") or ""),
        str(row.get("b_layer_id") or ""), str(row.get("b_cable_id") or ""),
        str(row.get("splice_type") or ""), _strand(row.get("a_strand_no")) or 0,
    ))
    groups = []
    for row in ordered:
        a = _strand(row.get("a_strand_no")) or 0
        b = _strand(row.get("b_strand_no"))
        previous = groups[-1] if groups else None
        if (previous and _metadata(previous["_originals"][-1]) == _metadata(row)
                and a == previous["a_to"] + 1
                and ((b is None and previous["b_to"] is None)
                     or (b is not None and previous["b_to"] is not None and b == previous["b_to"] + 1))):
            previous["a_to"], previous["b_to"] = a, b
            previous["_originals"].append(dict(row))
        else:
            groups.append({"a_cable_id": row.get("a_cable_id") or "", "a_layer_id": row.get("a_layer_id") or "",
                           "a_from": a, "a_to": a, "b_cable_id": row.get("b_cable_id") or "",
                           "b_layer_id": row.get("b_layer_id") or "", "b_from": b, "b_to": b,
                           "splice_type": row.get("splice_type") or "FUSION", "status": row.get("status") or "",
                           "tray": row.get("tray") or "", "notes": row.get("notes") or "",
                           "loss_db": row.get("loss_db"), "_originals": [dict(row)]})
    return groups


def _metadata(row):
    excluded = {"fid", "ogc_fid", "splice_id", "a_strand_no", "b_strand_no"}
    return {key: value for key, value in row.items() if key not in excluded and not key.startswith("_")}


def strict_range(value):
    """A range editor must never silently discard text or reverse a range."""
    match = re.fullmatch(r"\s*([1-9]\d*)\s*(?:-\s*([1-9]\d*)\s*)?", str(value or ""))
    if not match:
        raise ValueError("Use one fiber number or a range, such as 1 or 47-48.")
    first, last = int(match[1]), int(match[2] or match[1])
    if first > last:
        raise ValueError("The end of a range must be greater than or equal to the start.")
    return first, last


def expand_groups(groups, enclosure_id):
    """Expand edited ranges without losing custom attributes on existing strand rows."""
    rows = []
    for group in groups:
        originals = group.get("_originals") or []
        for offset, a in enumerate(range(group["a_from"], group["a_to"] + 1)):
            base = dict(originals[min(offset, len(originals) - 1)]) if originals else {}
            old = dict(base)
            b = group["b_from"] + offset if group.get("b_from") is not None else None
            base.update({"splice_enc_id": enclosure_id, "a_cable_id": group["a_cable_id"],
                         "a_layer_id": group.get("a_layer_id") or "", "a_strand_no": a,
                         "b_cable_id": group.get("b_cable_id") or None, "b_layer_id": group.get("b_layer_id") or "",
                         "b_strand_no": b, "splice_type": group["splice_type"], "status": group.get("status") or ""})
            for attribute in ("tray", "notes", "loss_db"):
                if group.get(attribute) not in (None, "") or attribute in base:
                    base[attribute] = group.get(attribute)
            # Keep nulls, IDs, and all other native attributes on unchanged rows.
            fields = {"a_cable_id", "a_strand_no", "b_cable_id", "b_strand_no", "splice_type", "status", "tray", "notes", "loss_db"}
            fields.update(key for key in ("a_layer_id", "b_layer_id") if old.get(key) or group.get("_force_layers"))
            equivalent = True
            for key in fields:
                before, after = old.get(key), base.get(key)
                if key.endswith("cable_id") or key.endswith("layer_id"):
                    matches = same(before, after)
                elif key in ("status", "notes", "tray"):
                    matches = (before or "") == (after or "")
                else:
                    matches = before == after
                equivalent &= matches
            if originals and equivalent:
                base = old
            else:
                base["splice_id"] = "sp." + uuid4().hex
                for key in list(base):
                    if key.startswith("_") or key in ("fid", "ogc_fid"):
                        base.pop(key)
            rows.append(base)
    return rows


def validate_groups(groups, cables):
    errors, used = [], {}
    for index, group in enumerate(groups, 1):
        prefix = f"Row {index}: "
        kind = str(group.get("splice_type") or "").upper()
        if not kind:
            errors.append(prefix + "choose a splice type.")
        loss = group.get("loss_db")
        if loss not in (None, "") and (_loss(loss) is None or not 0 <= _loss(loss) < float("inf")):
            errors.append(prefix + "loss must be a finite, nonnegative number.")
        a, b = _resolve(cables, group.get("a_cable_id"), group.get("a_layer_id")), _resolve(cables, group.get("b_cable_id"), group.get("b_layer_id"))
        sides = [("A", a, group.get("a_from"), group.get("a_to"))]
        if kind != "OPEN":
            sides.append(("B", b, group.get("b_from"), group.get("b_to")))
            if ((group.get("a_to") or 0) - (group.get("a_from") or 0)
                    != (group.get("b_to") or 0) - (group.get("b_from") or 0)):
                errors.append(prefix + "both fiber ranges must have equal lengths.")
            if a and b and cable_key(a) == cable_key(b):
                errors.append(prefix + "choose two different cable segments.")
        if kind == "EXPRESS" and (group.get("a_from"), group.get("a_to")) != (group.get("b_from"), group.get("b_to")):
            errors.append(prefix + "express fibers must keep the same numbers.")
        original_unchanged = bool(group.get("_originals")) and group_rows(group["_originals"])[0] == group
        for side, cable, first, last in sides:
            if cable is None:
                errors.append(prefix + f"cable {side} is missing or ambiguous; select a cable layer.")
                continue
            if not isinstance(first, int) or not isinstance(last, int) or first < 1 or last < first:
                errors.append(prefix + f"invalid fiber range on cable {side}.")
                continue
            capacity = int(cable.get("capacity") or 0)
            if group.get("_originals") and not original_unchanged and last - first <= 10000:
                original_unchanged = expand_groups([group], "") == group["_originals"]
            if not capacity and (not original_unchanged or parse_count((cable.get("values") or {}).get("strand_count"))):
                errors.append(prefix + f"set the fiber count for {feature_id(cable)} before editing its splices.")
            elif capacity and last > capacity:
                errors.append(prefix + f"{feature_id(cable)} only has {capacity} fibers.")
            if last - first > 100000:
                errors.append(prefix + "fiber range is too large.")
                continue
            for number in range(first, last + 1):
                key = (*cable_key(cable), number)
                if key in used:
                    errors.append(prefix + f"{feature_id(cable)} fiber {number} is already used by row {used[key]}.")
                    break
                used[key] = index
    return errors


def splice_changes(model, groups):
    """Diff all rows at this enclosure; leave unchanged rows and other enclosures intact."""
    errors = validate_groups(groups, model["cables"])
    if errors:
        raise ValueError("\n".join(errors))
    desired = expand_groups(groups, feature_id(model["enclosure"]))
    remaining = list(desired)
    deletes = []
    for old in model["original"]:
        found = next((row for row in remaining if row == old), None)
        if found is not None:
            remaining.remove(found)
        else:
            if old.get("_fid") is None or not old.get("_layer_id"):
                raise ValueError("A stored splice row cannot be identified for editing.")
            deletes.append((old["_layer_id"], old["_fid"]))
    for row in remaining:
        for key in list(row):
            if key.startswith("_") or key in ("fid", "ogc_fid"):
                row.pop(key)
    return {"delete": deletes, "splices": remaining, "rows": desired}


def fiber_usage(cables, groups):
    used = {cable_key(cable): set() for cable in cables}
    for group in groups:
        for side in ("a", "b"):
            cable = _resolve(cables, group.get(f"{side}_cable_id"), group.get(f"{side}_layer_id"))
            first, last = group.get(f"{side}_from"), group.get(f"{side}_to")
            if cable and isinstance(first, int) and isinstance(last, int):
                used[cable_key(cable)].update(range(first, min(last, 100000) + 1))
    return used


def available_fibers(cable, cables, groups):
    """Fiber numbers on one cable that no connection in ``groups`` uses."""
    capacity = int(cable.get("capacity") or 0)
    return sorted(set(range(1, capacity + 1)) - fiber_usage(cables, groups).get(cable_key(cable), set()))


def resolve_cable(cables, identifier, layer=""):
    return _resolve(cables, identifier, layer)
