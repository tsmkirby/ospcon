"""Patch panels that belong to an IDF, and the cable end written when a cable lands there."""

from __future__ import annotations

import re

from .catalog import DEVICE_ROLES, feature_id, resolve_end
from .fields import is_blank, next_id, parse_count, parse_range, text_of
from .geometry import distance


def panels_for(idf_ids: list[str], features: list[dict], index: dict) -> list[str]:
    """Panel ids that belong to these IDFs, from panel features and from cable ends."""
    found: list[str] = []
    wanted = {text.casefold() for text in idf_ids if text_of(text)}

    def add(value) -> None:
        text = text_of(value)
        if text and text.casefold() not in {item.casefold() for item in found}:
            found.append(text)

    for feature in features:
        if feature.get("role") in ("patch_panel", "fdp"):
            parent = text_of((feature.get("values") or {}).get("idf_ref"))
            if not wanted or (parent or "").casefold() in wanted:
                add(feature_id(feature))
        if feature.get("role") != "cable" or not wanted:
            continue
        values = feature.get("values") or {}
        for end_name, panel_key in (("a_end", "a_panel"), ("z_end", "z_panel")):
            hits = resolve_end(index, values.get(end_name), feature)
            if any((feature_id(hit) or "").casefold() in wanted for hit in hits if hit.get("role") == "idf"):
                add(values.get(panel_key))
            elif (text_of(values.get(end_name)) or "").casefold() in wanted:
                add(values.get(panel_key))
    return found


def panel_ports(panel: dict, features: list[dict], tables: dict | None = None) -> dict:
    """Every port on a panel, and the device and cable that occupy each one."""
    tables = tables or {}
    panel_id = feature_id(panel) or ""
    values = panel.get("values") or {}
    stated = parse_count(values.get("port_count")) or 0
    by_port: dict[int, dict] = {}

    def claim(port_value, device=None, cable=None, switch=None) -> None:
        for number in parse_range(port_value):
            slot = by_port.setdefault(number, {"devices": [], "cables": [], "switches": []})
            if device is not None and not any(_same_feature(device, item) for item in slot["devices"]):
                slot["devices"].append(device)
            if cable is not None and not any(_same_feature(cable, item) for item in slot["cables"]):
                slot["cables"].append(cable)
            if switch and switch not in slot["switches"]:
                slot["switches"].append(switch)

    for feature in features:
        feature_values = feature.get("values") or {}
        if feature.get("role") in DEVICE_ROLES and _same_text(feature_values.get("panel"), panel_id):
            claim(feature_values.get("port"), device=feature)
        if feature.get("role") != "cable":
            continue
        if _same_text(feature_values.get("a_panel"), panel_id):
            claim(feature_values.get("a_port"), cable=feature, device=_device_on_cable(feature, features))
        if _same_text(feature_values.get("z_panel"), panel_id):
            claim(feature_values.get("z_port"), cable=feature, device=_device_on_cable(feature, features))
    for row in tables.get("terminations") or []:
        if _same_text(row.get("pp_id") or row.get("fdp_id") or row.get("panel_id"), panel_id):
            claim(row.get("panel_port") or row.get("panel_position") or row.get("port"))
    for row in tables.get("circuits") or []:
        if not _same_text(row.get("panel_id"), panel_id):
            continue
        claim(
            row.get("panel_port"),
            device=_find_feature(features, row.get("device_id"), DEVICE_ROLES),
            cable=_find_feature(features, row.get("cable_id"), {"cable"}),
        )
    for row in tables.get("patches") or []:
        if not _same_text(row.get("panel_id"), panel_id):
            continue
        claim(row.get("panel_port"), switch=(text_of(row.get("sw_id")) or "", text_of(row.get("switch_port_id")) or ""))
    highest = max(by_port) if by_port else 0
    total = max(stated, highest)
    ports = []
    for number in range(1, total + 1):
        slot = by_port.get(number) or {"devices": [], "cables": [], "switches": []}
        device = slot["devices"][0] if slot["devices"] else None
        cable = slot["cables"][0] if slot["cables"] else None
        switch = slot["switches"][0] if slot.get("switches") else ("", "")
        ports.append(
            {
                "number": number,
                "occupied": bool(slot["devices"] or slot["cables"] or slot.get("switches")),
                "device_id": feature_id(device) if device else "",
                "cable_id": feature_id(cable) if cable else "",
                "switch_id": switch[0],
                "switch_port_id": switch[1],
                "device": device,
                "cable": cable,
            }
        )
    return {
        "panel_id": panel_id,
        "idf_id": text_of(values.get("idf_ref")) or "",
        "count": total,
        "ports": ports,
    }


def panel_feature_ids(features: list[dict]) -> list[str]:
    found = []
    for feature in features:
        if feature.get("role") not in ("patch_panel", "fdp"):
            continue
        identifier = feature_id(feature)
        if identifier and identifier.casefold() not in {item.casefold() for item in found}:
            found.append(identifier)
    return found


def new_panel_request(panel_id: str, idf: dict, media: str, existing_ids: list[str]) -> dict | None:
    """A panel feature to create. An id that already exists is left alone."""
    panel = text_of(panel_id)
    idf_id = feature_id(idf) if idf else None
    if not panel or not idf_id:
        return None
    if panel.casefold() in {text_of(item).casefold() for item in existing_ids if text_of(item)}:
        return None
    coords = idf.get("coords") or []
    point = coords[0] if coords else None
    return {
        "panel_id": panel,
        "idf_id": idf_id,
        "media": "fiber" if media == "fiber" else "copper",
        "x": point[0] if point else None,
        "y": point[1] if point else None,
    }


def suggest_panel_id(existing: list[str], idf_id: str, fiber: bool, template_style: bool) -> str:
    kind = "FD" if fiber else "PP"
    samples = []
    for value in existing:
        text = text_of(value) or ""
        if template_style and f"-{kind}-" not in text.upper():
            continue
        samples.append(text)
    if samples:
        return next_id(existing, samples[0])
    venue = "OSP"
    match = re.match(r"^([A-Za-z]{2,4})-", idf_id or "")
    if match:
        venue = match.group(1).upper()
    return next_id(list(existing), f"{venue}-{kind}-0000")


def cable_idf_edits(cable: dict, idf_id: str, panel_id: str, point: tuple | None) -> tuple[list[dict], list[str]]:
    """Point the nearer blank cable end at the IDF and record the panel. Filled fields stay as they are."""
    edits = []
    warnings = []
    identifier = feature_id(cable) or "cable"
    values = cable.get("values") or {}
    coords = cable.get("coords") or []
    if point and len(coords) >= 2:
        end_name = "a_end" if distance(coords[0], point) <= distance(coords[-1], point) else "z_end"
    elif is_blank(values.get("z_end")):
        end_name = "z_end"
    else:
        end_name = "a_end"
    panel_field = "a_panel" if end_name == "a_end" else "z_panel"
    if is_blank(values.get(end_name)) and idf_id:
        edits.append(_edit(cable, end_name, idf_id))
    elif idf_id and text_of(values.get(end_name)).casefold() != idf_id.casefold():
        warnings.append(f"{identifier} already has {end_name} {values.get(end_name)}, so the IDF was not written there.")
    if panel_id and is_blank(values.get(panel_field)):
        edits.append(_edit(cable, panel_field, panel_id))
    return edits, warnings


def _same_text(left, right) -> bool:
    text = text_of(left)
    other = text_of(right)
    return bool(text) and bool(other) and text.casefold() == other.casefold()


def _same_feature(left: dict, right: dict) -> bool:
    return left.get("layer_id") == right.get("layer_id") and left.get("fid") == right.get("fid")


def _find_feature(features: list[dict], identifier, roles: set):
    for feature in features:
        if feature.get("role") in roles and _same_text(feature_id(feature), identifier):
            return feature
    return None


def _device_on_cable(cable: dict, features: list[dict]):
    values = cable.get("values") or {}
    for end_name in ("a_end", "z_end"):
        found = _find_feature(features, values.get(end_name), DEVICE_ROLES)
        if found:
            return found
    cable_id = feature_id(cable)
    for feature in features:
        if feature.get("role") in DEVICE_ROLES and _same_text((feature.get("values") or {}).get("cable_ref"), cable_id):
            return feature
    return None


def _edit(cable: dict, canonical: str, value: str) -> dict:
    return {
        "layer_id": cable.get("layer_id"),
        "fid": cable.get("fid"),
        "canonical": canonical,
        "value": value,
    }
