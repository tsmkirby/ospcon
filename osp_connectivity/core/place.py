"""Decide what a click or a sketched cable should link to, without touching the map."""

from __future__ import annotations

import re

from .catalog import DEVICE_ROLES, build_index, feature_id, resolve_end
from .fields import is_blank, media_of, text_of
from .geometry import closest_on_line, distance, point_within
from .panels import cable_idf_edits, new_panel_request, panels_for
from .strands import strand_numbers_for


_NAME_ROLES = {
    "pos": "pos",
    "cctv": "cctv",
    "camera": "cctv",
    "ap": "ap",
    "jack": "data_jack",
}


def device_layer_choices(infos) -> list[dict]:
    """Point layers a new device can be added to, including a geometry-only POS layer."""
    choices = []
    seen = set()
    for info in infos:
        geometry = _get(info, "geometry_type")
        role = _get(info, "role")
        layer_id = _get(info, "layer_id")
        name = _get(info, "name") or layer_id
        if geometry != "Point" or not layer_id or layer_id in seen:
            continue
        if role in DEVICE_ROLES:
            choices.append({"layer_id": layer_id, "role": role, "label": name})
            seen.add(layer_id)
            continue
        if role not in (None, "structure"):
            continue
        tokens = set(re.split(r"[^a-z0-9]+", name.casefold()))
        for hint, mapped in _NAME_ROLES.items():
            if hint in tokens:
                choices.append({"layer_id": layer_id, "role": mapped, "label": name})
                seen.add(layer_id)
                break
    return choices


def cable_layer_choices(infos) -> list[dict]:
    return [
        {"layer_id": _get(info, "layer_id"), "role": "cable", "label": _get(info, "name") or _get(info, "layer_id")}
        for info in infos
        if _get(info, "role") == "cable" and _get(info, "layer_id")
    ]


def name_taken(name: str, existing) -> bool:
    wanted = (text_of(name) or "").casefold()
    if not wanted:
        return False
    return any((text_of(item) or "").casefold() == wanted for item in existing)


def taken_device_names(features: list[dict]) -> list[str]:
    found = []
    for feature in features:
        if feature.get("role") not in DEVICE_ROLES:
            continue
        for value in (feature_id(feature), (feature.get("values") or {}).get("name")):
            text = text_of(value)
            if text and text.casefold() not in {item.casefold() for item in found}:
                found.append(text)
    return found


def taken_cable_ids(features: list[dict]) -> list[str]:
    found = []
    for feature in features:
        if feature.get("role") != "cable":
            continue
        text = feature_id(feature)
        if text and text.casefold() not in {item.casefold() for item in found}:
            found.append(text)
    return found


def nearest_cable(point, cables: list[dict], tolerance: float):
    best = None
    best_gap = tolerance
    for cable in cables:
        coords = cable.get("coords") or []
        if len(coords) < 2:
            continue
        _point, gap, _measure = closest_on_line(point, coords)
        if gap <= best_gap:
            best = cable
            best_gap = gap
    return best


def nearest_feature(point, features: list[dict], roles, tolerance: float):
    """Closest feature of the given roles whose geometry is within tolerance of the point."""
    best = None
    best_gap = tolerance
    wanted = set(roles)
    for feature in features:
        if feature.get("role") not in wanted:
            continue
        coords = feature.get("coords") or []
        if not coords:
            continue
        if len(coords) >= 2:
            _point, gap, _measure = closest_on_line(point, coords)
        else:
            gap = distance(point, coords[0])
        if gap <= best_gap:
            best = feature
            best_gap = gap
    return best


def features_along_line(line, features: list[dict], roles: set, tolerance: float) -> list[dict]:
    found = []
    if len(line) < 2:
        return found
    for feature in features:
        if feature.get("role") not in roles:
            continue
        coords = feature.get("coords") or []
        if not coords:
            continue
        if point_within(coords[0], line, tolerance):
            found.append(feature)
    return found


def idfs_serving_cable(cable: dict, features: list[dict]) -> list[dict]:
    index = build_index(features)
    found = []
    values = cable.get("values") or {}
    for end_name in ("a_end", "z_end"):
        for hit in resolve_end(index, values.get(end_name), cable):
            if hit.get("role") == "idf" and hit not in found:
                found.append(hit)
    return found


def nearer_end_name(coords, point) -> str:
    if point and len(coords) >= 2 and distance(coords[-1], point) < distance(coords[0], point):
        return "z_end"
    return "a_end"


def placement_edits(
    choices: dict,
    cable: dict | None,
    device: dict | None,
    idf: dict | None,
    new_cable_id: str | None = None,
    new_device_id: str | None = None,
    panel_feature_ids: list | None = None,
    media: str = "copper",
    structure: dict | None = None,
) -> dict:
    """Field writes for a new or existing cable and device. Filled values are left alone."""
    warnings = []
    edits = []
    cable_values = {}
    device_values = {}
    if new_cable_id:
        cable_values["id"] = new_cable_id
        cable_values["name"] = new_cable_id
        cable_values["media"] = media
        cable_values["status"] = "proposed"
    if new_device_id:
        device_values["id"] = new_device_id
        device_values["name"] = new_device_id
        device_values["status"] = "proposed"
    if new_device_id and cable and feature_id(cable):
        device_values["cable_ref"] = feature_id(cable)
    mount = _mount_row(structure, new_device_id, choices.get("role"))
    if mount:
        device_values["mount"] = mount["support_id"]

    if not choices.get("associate"):
        _record_loose_ends(choices, cable_values, device, idf, new_cable_id, edits, warnings)
        return _result(edits, cable_values, device_values, None, warnings, mount=mount)

    panel = text_of(choices.get("panel_id"))
    port = text_of(choices.get("port"))
    a_role = choices.get("a_role") or "device"
    z_role = choices.get("z_role") or ("idf" if a_role == "device" else "device")
    device_end = "a_end" if a_role == "device" else "z_end"
    idf_end = "z_end" if device_end == "a_end" else "a_end"
    device_id = new_device_id or (feature_id(device) if device else None)
    cable_id = new_cable_id or (feature_id(cable) if cable else None)
    idf_id = feature_id(idf) if idf else text_of(choices.get("idf_id"))

    if device_id and cable_id:
        _assign(device, "cable_ref", cable_id, device_values, edits, warnings)
    if device_id:
        _assign(cable, device_end, device_id, cable_values, edits, warnings)
    if idf_id:
        _assign(cable, idf_end, idf_id, cable_values, edits, warnings)
    if panel:
        panel_field = "a_panel" if idf_end == "a_end" else "z_panel"
        port_field = "a_port" if idf_end == "a_end" else "z_port"
        _assign(cable, panel_field, panel, cable_values, edits, warnings)
        if port:
            _assign(cable, port_field, port, cable_values, edits, warnings)
        _assign(device, "panel", panel, device_values, edits, warnings)
    if port:
        _assign(device, "port", port, device_values, edits, warnings)

    numbers = strand_numbers_for({"port": choices.get("strands") or port}) if media == "fiber" else []
    circuit = None
    if device_id and idf_id and cable_id:
        circuit = {
            "circuit_id": f"ckt.{device_id}.{cable_id}",
            "media_type": media,
            "device_layer_id": (device or {}).get("layer_id") or choices.get("device_layer_id"),
            "device_id": device_id,
            "idf_layer_id": (idf or {}).get("layer_id"),
            "idf_id": idf_id,
            "panel_id": panel,
            "panel_port": port,
            "cable_layer_id": (cable or {}).get("layer_id") or choices.get("cable_layer_id"),
            "cable_id": cable_id,
            "strand_from": numbers[0] if numbers else None,
            "strand_to": numbers[-1] if numbers else None,
            "status": "proposed",
        }
    new_panel = None
    if panel and idf:
        new_panel = new_panel_request(panel, idf, media, panel_feature_ids or [])
    return _result(edits, cable_values, device_values, circuit, warnings, new_panel, mount)


def panel_media_groups(point, features: list[dict], tolerance: float) -> list[dict]:
    """Cables under a click, split into copper and fiber, with the device on each."""
    cables = [
        feature
        for feature in features
        if feature.get("role") == "cable" and point_within(point, feature.get("coords") or [], tolerance)
    ]
    groups = []
    for media in ("copper", "fiber"):
        members = [cable for cable in cables if _cable_media(cable) == media]
        if not members:
            continue
        devices = []
        for cable in members:
            for device in features_along_line(cable.get("coords") or [], features, DEVICE_ROLES, tolerance):
                if not any(_same_feature(device, item) for item in devices):
                    devices.append(device)
        touching = members
        device = devices[0] if len(devices) == 1 else None
        if device and device.get("coords"):
            on_device = [
                cable
                for cable in members
                if point_within(device["coords"][0], cable.get("coords") or [], tolerance)
            ]
            if on_device:
                touching = on_device
        cable = nearest_cable(point, touching, tolerance) or touching[0]
        groups.append(
            {
                "media": media,
                "cables": members,
                "cable": cable,
                "devices": devices,
                "device": device,
                "idf_end": nearer_end_name(cable.get("coords") or [], point),
            }
        )
    return groups


def panel_click_edits(
    choice: dict,
    cables: list[dict],
    cable: dict | None,
    device: dict | None,
    idf: dict | None,
    point,
    panel_ids: list | None,
    media: str,
) -> dict:
    """Write the panel onto each cable of this media. Port and device stay on the nearest cable."""
    plan = placement_edits(
        dict(choice, associate=True),
        cable,
        device,
        idf,
        panel_feature_ids=panel_ids,
        media=media,
    )
    if point and plan.get("new_panel"):
        plan["new_panel"]["x"] = point[0]
        plan["new_panel"]["y"] = point[1]
    extras = []
    warnings = list(plan["warnings"])
    panel = text_of(choice.get("panel_id"))
    idf_id = feature_id(idf) if idf else None
    if plan.get("circuit") is None and cable and idf_id and panel:
        numbers = strand_numbers_for({"port": choice.get("strands") or choice.get("port")}) if media == "fiber" else []
        plan["circuit"] = {
            "circuit_id": f"ckt.{panel}.{feature_id(cable)}",
            "media_type": media,
            "device_layer_id": (device or {}).get("layer_id") or choice.get("device_layer_id"),
            "device_id": feature_id(device) if device else None,
            "idf_layer_id": idf.get("layer_id"),
            "idf_id": idf_id,
            "panel_id": panel,
            "panel_port": text_of(choice.get("port")),
            "cable_layer_id": cable.get("layer_id") or choice.get("cable_layer_id"),
            "cable_id": feature_id(cable),
            "strand_from": numbers[0] if numbers else None,
            "strand_to": numbers[-1] if numbers else None,
            "status": "proposed",
        }
    for other in cables:
        if cable is not None and _same_feature(other, cable):
            continue
        extra, extra_warnings = cable_idf_edits(other, idf_id or "", panel or "", point)
        extras.extend(extra)
        warnings.extend(extra_warnings)
    plan["edits"] = list(plan["edits"]) + extras
    plan["warnings"] = warnings
    return plan


def panels_for_idfs(idfs: list[dict], features: list[dict]) -> dict:
    index = build_index(features)
    return {feature_id(idf) or "": panels_for([feature_id(idf) or ""], features, index) for idf in idfs}


def _assign(feature, canonical, value, values: dict, edits: list, warnings: list) -> None:
    if not value:
        return
    if feature is None:
        values[canonical] = value
        return
    current = (feature.get("values") or {}).get(canonical)
    if is_blank(current):
        edits.append(
            {
                "layer_id": feature.get("layer_id"),
                "fid": feature.get("fid"),
                "canonical": canonical,
                "value": value,
            }
        )
        return
    if (text_of(current) or "").casefold() != str(value).casefold():
        warnings.append(f"{feature_id(feature) or canonical} already has {canonical} {current}.")


def _result(edits, cable_values, device_values, circuit, warnings, new_panel=None, mount=None) -> dict:
    return {
        "edits": edits,
        "cable_values": cable_values,
        "device_values": device_values,
        "circuit": circuit,
        "new_panel": new_panel,
        "mount": mount,
        "warnings": warnings,
    }


def _mount_row(structure, device_id, role):
    support = feature_id(structure) if structure else None
    if not support or not device_id:
        return None
    return {
        "mount_id": f"{support}.{device_id}",
        "support_id": support,
        "device_id": device_id,
        "device_type": (role or "device").upper(),
        "status": "proposed",
    }


def _record_loose_ends(choices, cable_values, device, idf, new_cable_id, edits, warnings) -> None:
    """Write a device or IDF the new cable already touches when the full association dialog did not run."""
    if not new_cable_id:
        return
    device_end = "a_end" if (choices.get("a_role") or "device") == "device" else "z_end"
    idf_end = "z_end" if device_end == "a_end" else "a_end"
    if device and feature_id(device):
        cable_values[device_end] = feature_id(device)
        _assign(device, "cable_ref", new_cable_id, {}, edits, warnings)
    idf_id = feature_id(idf) if idf else None
    if idf_id:
        cable_values[idf_end] = idf_id


def adopt_typed_id(plan: dict, field_map: dict, attributes: dict, kind: str) -> None:
    """Keep ends, the circuit, and the mount aligned when the attribute form changes the id."""
    id_field = (field_map or {}).get("id")
    if not id_field or not attributes:
        return
    new_id = text_of(attributes.get(id_field))
    bucket = "device_values" if kind == "device" else "cable_values"
    values = plan.get(bucket) or {}
    old = text_of(values.get("id"))
    if not new_id or not old or new_id == old:
        return
    values["id"] = new_id
    if text_of(values.get("name")) == old:
        values["name"] = new_id
        name_field = (field_map or {}).get("name")
        if name_field and text_of(attributes.get(name_field)) == old:
            attributes[name_field] = new_id
    if kind == "device":
        for edit in plan.get("edits") or []:
            if text_of(edit.get("value")) == old and edit.get("canonical") in ("a_end", "z_end"):
                edit["value"] = new_id
    else:
        for edit in plan.get("edits") or []:
            if edit.get("canonical") == "cable_ref" and text_of(edit.get("value")) == old:
                edit["value"] = new_id
    circuit = plan.get("circuit")
    if circuit:
        if kind == "device" and text_of(circuit.get("device_id")) == old:
            circuit["device_id"] = new_id
        if kind == "cable" and text_of(circuit.get("cable_id")) == old:
            circuit["cable_id"] = new_id
        if circuit.get("device_id") and circuit.get("cable_id"):
            circuit["circuit_id"] = f"ckt.{circuit['device_id']}.{circuit['cable_id']}"
    mount = plan.get("mount")
    if mount and text_of(mount.get("device_id")) == old:
        mount["device_id"] = new_id
        mount["mount_id"] = f"{mount.get('support_id')}.{new_id}"


def _cable_media(cable: dict) -> str:
    return "fiber" if media_of(cable.get("values") or {}) == "fiber" else "copper"


def _same_feature(left: dict, right: dict) -> bool:
    return left.get("layer_id") == right.get("layer_id") and left.get("fid") == right.get("fid")


def _get(info, name):
    if isinstance(info, dict):
        return info.get(name)
    return getattr(info, name, None)
