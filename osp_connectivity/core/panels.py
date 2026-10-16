"""Patch panels that belong to an IDF, and the cable end written when a cable lands there."""

from __future__ import annotations

import re

from .catalog import feature_id, resolve_end
from .fields import is_blank, next_id, text_of
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


def _edit(cable: dict, canonical: str, value: str) -> dict:
    return {
        "layer_id": cable.get("layer_id"),
        "fid": cable.get("fid"),
        "canonical": canonical,
        "value": value,
    }
