"""Part 2: propose one circuit from the features the user has selected."""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import DEVICE_ROLES, build_index, end_convention, feature_id, resolve_end
from .fields import media_of, ranges_overlap, text_of
from .geometry import chain_segments
from .handholes import handhole_rows, handholes_on_cables
from .panels import new_panel_request, panel_feature_ids, panels_for
from .strands import strand_numbers_for
from .trace import cables_for_device


@dataclass
class AssociationProposal:
    cables: list
    devices: list
    idfs: list
    structures: list
    ducts: list
    enclosures: list
    duct_clean: bool
    cable_clean: bool
    media: str
    convention: str
    panels: list[str] = field(default_factory=list)
    panels_by_idf: dict = field(default_factory=dict)
    panel_feature_ids: list = field(default_factory=list)
    handholes: list = field(default_factory=list)
    tolerance: float = 1.0
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.cables) and not any(item.startswith("Stop:") for item in self.warnings)


def propose(selected: list[dict], project_features: list[dict], tolerance: float = 1.0) -> AssociationProposal:
    cables = [feature for feature in selected if feature.get("role") == "cable"]
    devices = [feature for feature in selected if feature.get("role") in DEVICE_ROLES]
    idfs = [feature for feature in selected if feature.get("role") == "idf"]
    structures = [feature for feature in selected if feature.get("role") == "structure"]
    ducts = [feature for feature in selected if feature.get("role") == "duct"]
    enclosures = [feature for feature in selected if feature.get("role") == "splice_enc"]
    warnings: list[str] = []

    if not cables:
        warnings.append("Stop: select at least one cable.")
    if len(devices) > 1:
        warnings.append("Stop: select one device. More than one AP, camera, or jack is selected.")
    if len(idfs) > 2:
        warnings.append("Stop: select one IDF, or two when the cable is a fiber backbone.")

    ordered_ducts, duct_clean = chain_segments(ducts, tolerance)
    ordered_cables, cable_clean = chain_segments(cables, tolerance)
    if ducts and not duct_clean:
        warnings.append("The duct segments do not make one chain. Reorder them before saving.")
    if len(cables) > 1 and not cable_clean:
        warnings.append("The cables do not meet end to end. Reorder them before saving.")

    medias = {media_of(cable.get("values") or {}) for cable in ordered_cables}
    medias.discard("unknown")
    if len(medias) > 1:
        warnings.append("Stop: the selected cables mix copper and fiber.")
    media = next(iter(medias), "unknown")
    if media == "unknown" and devices:
        uplink = media_of(devices[0].get("values") or {})
        if uplink != "unknown":
            media = uplink

    index = build_index(project_features)
    convention = end_convention(
        [feature for feature in project_features if feature.get("role") == "cable"],
        index,
    )
    panels_by_idf = {feature_id(idf) or "": panels_for([feature_id(idf) or ""], project_features, index) for idf in idfs}
    panels = panels_by_idf.get(feature_id(idfs[0]) or "", []) if idfs else panels_for([], project_features, index)
    project_handholes = [feature for feature in project_features if feature.get("role") == "handhole"]
    selected_handholes = [feature for feature in selected if feature.get("role") == "handhole"]
    along = handholes_on_cables(ordered_cables, project_handholes, tolerance)
    for handhole in selected_handholes:
        if not _same(handhole, along):
            along.append(handhole)
    return AssociationProposal(
        cables=ordered_cables,
        devices=devices,
        idfs=idfs,
        structures=structures,
        ducts=ordered_ducts,
        enclosures=enclosures,
        duct_clean=duct_clean,
        cable_clean=cable_clean,
        media=media,
        convention=convention,
        panels=panels,
        panels_by_idf=panels_by_idf,
        panel_feature_ids=panel_feature_ids(project_features),
        handholes=along,
        tolerance=tolerance,
        warnings=warnings,
    )


def device_association(device: dict, features: list[dict], tables: dict, tolerance: float = 1.0) -> dict:
    """Everything currently tied to one device, including handholes on its cable."""
    index = build_index(features)
    cables = cables_for_device(device, features, index)
    warnings = []
    if not cables:
        warnings.append(f"{feature_id(device) or 'The device'} has no cable.")
    candidates = [feature for feature in features if feature.get("role") == "handhole"]
    along = handholes_on_cables(cables, candidates, tolerance)
    stored = _stored_handhole_ids(cables, tables.get("handholes") or [])
    if stored:
        for handhole in candidates:
            identifier = (feature_id(handhole) or "").casefold()
            if identifier in stored and not _same(handhole, along):
                along.append(handhole)
        checked = [handhole for handhole in along if (feature_id(handhole) or "").casefold() in stored]
    else:
        checked = list(along)
    idfs = []
    panel = port = None
    for cable in cables:
        values = cable.get("values") or {}
        for end_name in ("a_end", "z_end"):
            for hit in resolve_end(index, values.get(end_name), cable):
                if hit.get("role") == "idf" and not _same(hit, idfs):
                    idfs.append(hit)
        if panel is None:
            panel, port = _panel_on_cable(values, feature_id(idfs[0]) if idfs else None)
    if panel is None:
        panel = text_of((device.get("values") or {}).get("panel"))
        port = text_of((device.get("values") or {}).get("port"))
    panels_by_idf = {feature_id(idf) or "": panels_for([feature_id(idf) or ""], features, index) for idf in idfs}
    return {
        "ready": bool(cables),
        "device": device,
        "cables": cables,
        "idf": idfs[0] if idfs else None,
        "idfs": idfs,
        "panel": panel or "",
        "port": port or "",
        "media": media_of((cables[0].get("values") if cables else {}) or {}),
        "handholes": along,
        "checked": checked,
        "panels_by_idf": panels_by_idf,
        "panel_feature_ids": panel_feature_ids(features),
        "tolerance": tolerance,
        "circuit_rows": [
            row
            for row in tables.get("circuits") or []
            if (text_of(row.get("device_id")) or "").casefold() == (feature_id(device) or "").casefold()
        ],
        "warnings": warnings,
    }


def modification_edits(association: dict, choices: dict, handhole_rows_now: list[dict]) -> dict:
    """Overwrite the panel, port, and handhole list for a device the user is editing."""
    device = association.get("device")
    cables = association.get("cables") or []
    idf = association.get("idf")
    wanted = text_of(choices.get("idf_id"))
    if wanted:
        idf = next((item for item in association.get("idfs") or [] if feature_id(item) == wanted), idf)
    panel = text_of(choices.get("panel_id"))
    port = text_of(choices.get("port"))
    idf_id = feature_id(idf) if idf else None
    edits = []
    for cable in cables:
        end_name = _idf_end(cable, idf_id)
        if not end_name or not panel:
            continue
        panel_field = "a_panel" if end_name == "a_end" else "z_panel"
        port_field = "a_port" if end_name == "a_end" else "z_port"
        edits.append(_field_edit(cable, panel_field, panel))
        if port:
            edits.append(_field_edit(cable, port_field, port))
    if device and panel:
        edits.append(_field_edit(device, "panel", panel))
    if device and port:
        edits.append(_field_edit(device, "port", port))
    chosen = choices.get("handholes") or []
    rows = handhole_rows(cables, chosen, association.get("tolerance") or 1)
    cable_ids = {(feature_id(cable) or "").casefold() for cable in cables}
    delete = []
    for row in handhole_rows_now:
        if (text_of(row.get("cable_id")) or "").casefold() not in cable_ids:
            continue
        if row.get("_fid") is None:
            continue
        delete.append((row.get("_layer_id"), row.get("_fid")))
    circuit_updates = []
    for row in association.get("circuit_rows") or []:
        if row.get("_fid") is None:
            continue
        values = {"panel_id": panel, "panel_port": port}
        if association.get("media") == "fiber" and text_of(choices.get("strands")):
            numbers = strand_numbers_for({"port": choices.get("strands")})
            if numbers:
                values["strand_from"] = numbers[0]
                values["strand_to"] = numbers[-1]
        circuit_updates.append({"layer_id": row.get("_layer_id"), "fid": row.get("_fid"), "values": values})
    return {
        "edits": edits,
        "handholes": rows,
        "delete": delete,
        "circuit_updates": circuit_updates,
        "new_panel": new_panel_request(panel, idf, association.get("media") or "copper", association.get("panel_feature_ids") or [])
        if idf
        else None,
    }


def occupied_ports(features: list[dict], terminations: list[dict]) -> list[tuple[str, str]]:
    pairs = []

    def add(panel, port) -> None:
        panel_text = text_of(panel)
        port_text = text_of(port)
        if panel_text and port_text:
            pairs.append((panel_text, port_text))

    for feature in features:
        values = feature.get("values") or {}
        add(values.get("panel"), values.get("port"))
        add(values.get("a_panel"), values.get("a_port"))
        add(values.get("z_panel"), values.get("z_port"))
    for row in terminations:
        add(row.get("panel_id") or row.get("pp_id") or row.get("fdp_id"), row.get("panel_port") or row.get("panel_position"))
    return pairs


def port_in_use(occupied: list[tuple[str, str]], panel: str, port: str) -> bool:
    for used_panel, used_port in occupied:
        if used_panel.casefold() != (panel or "").casefold():
            continue
        if ranges_overlap(used_port, port):
            return True
    return False


def assignment_edits(proposal: AssociationProposal, choices: dict) -> dict:
    """Turn the dialog choices into field edits and circuit values. Does not touch the map."""
    warnings = []
    if not proposal.cables:
        return {"edits": [], "circuit": None, "routes": [], "mount": None, "warnings": ["No cable to update."]}

    device = proposal.devices[0] if proposal.devices else None
    idf = None
    if proposal.idfs:
        wanted = text_of(choices.get("idf_id"))
        idf = next((item for item in proposal.idfs if feature_id(item) == wanted), proposal.idfs[0])

    a_role = choices.get("a_role") or ("device" if proposal.convention == "a_device" else "idf")
    z_role = choices.get("z_role") or ("idf" if a_role == "device" else "device")
    if a_role == z_role and device and idf:
        warnings.append("A and Z were both set to the same kind of asset. Z was switched.")
        z_role = "idf" if a_role == "device" else "device"

    def asset_for(role: str):
        if role == "device":
            return device
        if role == "idf":
            return idf
        return None

    panel = text_of(choices.get("panel_id"))
    port = text_of(choices.get("port"))
    if idf and (not panel or not port):
        warnings.append("Enter both a panel and a port. The IDF end was still labeled.")

    edits = []
    cable = proposal.cables[0] if len(proposal.cables) == 1 else None
    # End labels are written on every selected cable only when there is a single cable.
    # A multi-cable fiber run keeps each section's existing ends; the splice tool owns the breaks.
    if cable is not None:
        for role_name, canonical in ((a_role, "a_end"), (z_role, "z_end")):
            asset = asset_for(role_name)
            if asset and feature_id(asset):
                edits.append(
                    {
                        "layer_id": cable["layer_id"],
                        "fid": cable["fid"],
                        "canonical": canonical,
                        "value": feature_id(asset),
                    }
                )
        idf_end = "a_end" if a_role == "idf" else "z_end" if z_role == "idf" else None
        if idf_end and panel:
            panel_field = "a_panel" if idf_end == "a_end" else "z_panel"
            port_field = "a_port" if idf_end == "a_end" else "z_port"
            edits.append({"layer_id": cable["layer_id"], "fid": cable["fid"], "canonical": panel_field, "value": panel})
            if port:
                edits.append({"layer_id": cable["layer_id"], "fid": cable["fid"], "canonical": port_field, "value": port})

    if device and proposal.cables:
        edits.append(
            {
                "layer_id": device["layer_id"],
                "fid": device["fid"],
                "canonical": "cable_ref",
                "value": feature_id(proposal.cables[-1] if z_role == "device" else proposal.cables[0]),
            }
        )
        if panel:
            edits.append({"layer_id": device["layer_id"], "fid": device["fid"], "canonical": "panel", "value": panel})
        if port:
            edits.append({"layer_id": device["layer_id"], "fid": device["fid"], "canonical": "port", "value": port})

    mount = None
    if device and proposal.structures:
        structure = proposal.structures[0]
        if feature_id(structure):
            edits.append(
                {
                    "layer_id": device["layer_id"],
                    "fid": device["fid"],
                    "canonical": "mount",
                    "value": feature_id(structure),
                }
            )
            mount = {
                "mount_id": f"{feature_id(structure)}.{feature_id(device)}",
                "support_id": feature_id(structure),
                "device_id": feature_id(device),
                "device_type": (device.get("role") or "").upper(),
                "status": text_of((device.get("values") or {}).get("status")) or "proposed",
            }

    routes = []
    for cable_feature in proposal.cables:
        for position, duct in enumerate(proposal.ducts, start=1):
            routes.append(
                {
                    "cable_source_layer_id": cable_feature.get("layer_id"),
                    "cable_id": feature_id(cable_feature),
                    "segment_layer_id": duct.get("layer_id"),
                    "segment_fid": duct.get("fid"),
                    "segment_id": feature_id(duct) or "",
                    "route_order": position,
                }
            )

    numbers = strand_numbers_for({"port": choices.get("strands") or port}) if proposal.media == "fiber" else []
    circuit = None
    if device and idf and proposal.cables:
        served = proposal.cables[-1] if z_role == "device" else proposal.cables[0]
        circuit = {
            "circuit_id": f"ckt.{feature_id(device)}.{feature_id(served)}",
            "media_type": proposal.media,
            "device_layer_id": device.get("layer_id"),
            "device_id": feature_id(device),
            "idf_layer_id": idf.get("layer_id"),
            "idf_id": feature_id(idf),
            "panel_id": panel,
            "panel_port": port,
            "cable_layer_id": served.get("layer_id"),
            "cable_id": feature_id(served),
            "strand_from": numbers[0] if numbers else None,
            "strand_to": numbers[-1] if numbers else None,
            "status": text_of((served.get("values") or {}).get("status")) or "proposed",
        }

    chosen_handholes = choices.get("handholes")
    if chosen_handholes is None:
        chosen_handholes = list(proposal.handholes)
    return {
        "edits": edits,
        "circuit": circuit,
        "routes": routes,
        "handholes": handhole_rows(proposal.cables, chosen_handholes, proposal.tolerance),
        "mount": mount,
        "new_panel": new_panel_request(panel, idf, proposal.media, proposal.panel_feature_ids) if idf else None,
        "warnings": warnings,
    }


def _same(feature: dict, features: list) -> bool:
    return any(item.get("layer_id") == feature.get("layer_id") and item.get("fid") == feature.get("fid") for item in features)


def _stored_handhole_ids(cables: list[dict], rows: list[dict]) -> set[str]:
    cable_ids = {(feature_id(cable) or "").casefold() for cable in cables}
    found = set()
    for row in rows:
        if (text_of(row.get("cable_id")) or "").casefold() not in cable_ids:
            continue
        identifier = text_of(row.get("handhole_id"))
        if identifier:
            found.add(identifier.casefold())
    return found


def _panel_on_cable(values: dict, idf_id: str | None) -> tuple:
    end_name = _idf_end({"values": values}, idf_id)
    if end_name == "a_end":
        return text_of(values.get("a_panel")), text_of(values.get("a_port"))
    if end_name == "z_end":
        return text_of(values.get("z_panel")), text_of(values.get("z_port"))
    return text_of(values.get("a_panel")) or text_of(values.get("z_panel")), text_of(values.get("a_port")) or text_of(values.get("z_port"))


def _idf_end(cable: dict, idf_id: str | None) -> str | None:
    if not idf_id:
        return None
    values = cable.get("values") or {}
    wanted = idf_id.casefold()
    for end_name in ("a_end", "z_end"):
        if (text_of(values.get(end_name)) or "").casefold() == wanted:
            return end_name
    return None


def _field_edit(feature: dict, canonical: str, value: str) -> dict:
    return {
        "layer_id": feature.get("layer_id"),
        "fid": feature.get("fid"),
        "canonical": canonical,
        "value": value,
    }
