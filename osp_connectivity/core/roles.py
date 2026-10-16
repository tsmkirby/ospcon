"""Decide what kind of OSP layer a field list is, without knowing the file name."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .fields import map_fields


@dataclass
class LayerInfo:
    layer_id: str
    name: str
    role: Optional[str]
    geometry_type: str
    field_map: dict
    source: str = ""


def classify(geometry_type: str, field_names: Iterable[str]) -> Optional[str]:
    names = {name.casefold() for name in field_names}
    geometry = geometry_type or "None"

    if geometry in ("None", "Unknown", "NoGeometry"):
        if "switch_port_id" in names and "panel_port" in names:
            return "cross_connect"
        if "switch_port_id" in names and "sw_id" in names:
            return "switch_port"
        if "sw_id" in names:
            return "switch"
        if "a_strand_no" in names and "b_strand_no" in names:
            return "fiber_splice"
        if "strand_no" in names and "fdp_id" in names:
            return "fiber_termination"
        if "strand_no" in names and "cable_id" in names:
            return "fiber_strand"
        if "pp_id" in names and ("panel_port" in names or "cable_id" in names):
            return "copper_termination"
        if "handhole_id" in names and "cable_id" in names:
            return "cable_handhole"
        if "route_order" in names and "cable_id" in names:
            return "cable_route"
        if "support_id" in names and "device_id" in names:
            return "mounted_device"
        if "splice_enc_id" in names and "cable_id" in names:
            return "splice_enc_cable"
        if "circuit_id" in names:
            return "circuit"
        return None

    if geometry == "Line":
        if any(name in names for name in ("cable_id", "strand_count", "fiber_count", "media_type")):
            return "cable"
        if any(name in names for name in ("segment_id", "conduit_capacity", "cconduit_capacity")):
            return "duct"
        return None

    if geometry == "Polygon":
        if "idf_id" in names or "mdf_idf_id" in names:
            return "idf"
        if "building_id" in names:
            return "building"
        return None

    if geometry != "Point":
        return None

    if "pp_id" in names:
        return "patch_panel"
    if "fdp_id" in names:
        return "fdp"
    if "splice_enc_id" in names:
        return "splice_enc"
    if "jack_id" in names:
        return "data_jack"
    if "pos_id" in names:
        return "pos"
    if "camera_type" in names or "cctv_id" in names:
        return "cctv"
    if "ap_id" in names or any(name in names for name in ("antenna_type", "antenna_model", "uplink_type")):
        return "ap"
    if "idf_id" in names or "mdf_idf_id" in names:
        return "idf"
    if "access_id" in names:
        return "handhole"
    if any(name in names for name in ("lid_material", "wall_material", "cover_material")):
        return "handhole"
    if "support_id" in names or "wifi_present" in names or "cctv_present" in names:
        return "structure"
    if "building_id" in names and "idf_id" not in names:
        return "building"
    if "structure_id" in names:
        return "structure"
    return None


def inspect_layer(layer_id: str, name: str, geometry_type: str, field_names: Iterable[str], source: str = "") -> LayerInfo:
    names = list(field_names)
    role = classify(geometry_type, names)
    return LayerInfo(
        layer_id=layer_id,
        name=name,
        role=role,
        geometry_type=geometry_type,
        field_map=map_fields(role, names),
        source=source,
    )
