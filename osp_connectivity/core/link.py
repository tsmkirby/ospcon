"""Part 1: turn ids that are already written on features into association rows."""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import (
    DEVICE_ROLES,
    build_index,
    duplicate_cable_ids,
    feature_id,
    resolve_end,
)
from .fields import is_blank, parse_count, text_of
from .geometry import ducts_along_cable
from .strands import strand_numbers_for
from .fields import media_of


@dataclass
class FieldEdit:
    layer_id: str
    fid: int
    canonical: str
    value: object
    note: str


@dataclass
class RowInsert:
    role: str
    values: dict
    note: str


@dataclass
class Notice:
    level: str
    message: str


@dataclass
class LinkPlan:
    edits: list[FieldEdit] = field(default_factory=list)
    inserts: list[RowInsert] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)


def _key_cable(feature: dict) -> tuple:
    return (feature.get("layer_id"), (feature_id(feature) or "").casefold())


def plan_links(
    features: list[dict],
    existing_strands: set[tuple] | None = None,
    existing_routes: set[tuple] | None = None,
    existing_circuits: set[tuple] | None = None,
    existing_mounts: set[tuple] | None = None,
    tolerance: float = 1.0,
    spatial_routes: bool = True,
    write_mount_rows: bool = True,
) -> LinkPlan:
    """Build the missing reciprocal fields and child rows. Never overwrite a filled field."""
    plan = LinkPlan()
    existing_strands = existing_strands or set()
    existing_routes = existing_routes or set()
    existing_circuits = existing_circuits or set()
    existing_mounts = existing_mounts or set()

    index = build_index(features)
    claimed: set[tuple] = set()
    cables = [feature for feature in features if feature.get("role") == "cable"]
    ducts = [feature for feature in features if feature.get("role") == "duct"]
    devices = [feature for feature in features if feature.get("role") in DEVICE_ROLES]
    duplicates = duplicate_cable_ids(cables)

    for key, rows in sorted(duplicates.items()):
        names = ", ".join(sorted({row.get("layer_name") or row.get("layer_id") or "" for row in rows}))
        shown = feature_id(rows[0])
        plan.notices.append(
            Notice(
                "warning",
                f"Cable id {shown} is on {len(rows)} features ({names}). Those copies were not merged.",
            )
        )

    for cable in cables:
        cable_identifier = feature_id(cable)
        if not cable_identifier:
            plan.notices.append(
                Notice("skipped", f"Cable feature {cable.get('fid')} on {cable.get('layer_name')} has no cable id.")
            )
            continue
        if cable_identifier.casefold() in duplicates:
            continue
        values = cable.get("values") or {}
        _link_ends(plan, cable, index, claimed)
        _link_strands(plan, cable, existing_strands)
        if spatial_routes and cable.get("coords"):
            _link_routes(plan, cable, ducts, existing_routes, tolerance)
        media = media_of(values)
        device_hits = []
        idf_hits = []
        for end_name in ("a_end", "z_end"):
            for hit in resolve_end(index, values.get(end_name), cable):
                if hit.get("role") in DEVICE_ROLES:
                    device_hits.append((end_name, hit))
                elif hit.get("role") == "idf":
                    idf_hits.append((end_name, hit))
        if len(device_hits) == 1 and len(idf_hits) == 1:
            _link_circuit(plan, cable, device_hits[0][1], idf_hits[0][1], device_hits[0][0], existing_circuits, media)

    for device in devices:
        _link_mount(plan, device, index, existing_mounts, write_mount_rows)
        _link_device_cable_text(plan, device, index)

    if not plan.edits and not plan.inserts:
        plan.notices.append(Notice("present", "No missing associations were found."))
    return plan


def _link_ends(plan: LinkPlan, cable: dict, index: dict, claimed: set[tuple]) -> None:
    values = cable.get("values") or {}
    cable_identifier = feature_id(cable)
    for end_name in ("a_end", "z_end"):
        panel_key = "a_panel" if end_name == "a_end" else "z_panel"
        port_key = "a_port" if end_name == "a_end" else "z_port"
        for hit in resolve_end(index, values.get(end_name), cable):
            if hit.get("role") not in DEVICE_ROLES:
                continue
            hit_values = hit.get("values") or {}
            _queue_edit(
                plan,
                claimed,
                hit,
                "cable_ref",
                cable_identifier,
                is_blank(hit_values.get("cable_ref")),
                f"Set {feature_id(hit)} cable to {cable_identifier} from the cable {end_name}.",
            )
            _queue_edit(
                plan,
                claimed,
                hit,
                "panel",
                values.get(panel_key),
                bool(text_of(values.get(panel_key))) and is_blank(hit_values.get("panel")),
                f"Copied panel {values.get(panel_key)} onto {feature_id(hit)}.",
            )
            _queue_edit(
                plan,
                claimed,
                hit,
                "port",
                values.get(port_key),
                bool(text_of(values.get(port_key))) and is_blank(hit_values.get("port")),
                f"Copied port {values.get(port_key)} onto {feature_id(hit)}.",
            )


def _queue_edit(plan, claimed, feature, canonical, value, allowed, note) -> None:
    if not allowed or canonical not in (feature.get("fields") or {}):
        return
    key = (feature.get("layer_id"), feature.get("fid"), canonical)
    if key in claimed:
        plan.notices.append(
            Notice("skipped", f"{feature_id(feature)} already received a {canonical} from another cable.")
        )
        return
    claimed.add(key)
    plan.edits.append(FieldEdit(feature["layer_id"], feature["fid"], canonical, value, note))


def _link_device_cable_text(plan: LinkPlan, device: dict, index: dict) -> None:
    """A device that already names a cable is reported when that cable does not exist."""
    cable_ref = text_of((device.get("values") or {}).get("cable_ref"))
    if not cable_ref:
        return
    hits = [hit for hit in resolve_end(index, cable_ref, device) if hit.get("role") == "cable"]
    # resolve_end prefers assets. Look up the raw index via the device's cable text
    # by searching cables directly when resolve returns nothing useful.
    if hits:
        return
    from .catalog import lookup

    cable_hits = [hit for hit in lookup(index, cable_ref, device) if hit.get("role") == "cable"]
    if cable_hits:
        plan.notices.append(
            Notice("present", f"{feature_id(device)} already references cable {cable_ref}.")
        )
        return
    if cable_ref.casefold() in {"unk", "unknown", "na", "n/a", "tbd"}:
        plan.notices.append(Notice("skipped", f"{feature_id(device)} cable id is '{cable_ref}' and was left as-is."))
        return
    plan.notices.append(Notice("warning", f"{feature_id(device)} references cable {cable_ref}, which is not in the project."))


def _link_strands(plan: LinkPlan, cable: dict, existing: set[tuple]) -> None:
    values = cable.get("values") or {}
    if media_of(values) != "fiber":
        return
    count = parse_count(values.get("strand_count"))
    if not count:
        plan.notices.append(
            Notice("skipped", f"{feature_id(cable)} is fiber but has no strand count, so no strand rows were created.")
        )
        return
    cable_identifier = feature_id(cable)
    created = 0
    for number in range(1, count + 1):
        key = (cable.get("layer_id"), cable_identifier.casefold(), number)
        if key in existing:
            continue
        plan.inserts.append(
            RowInsert(
                "fiber_strand",
                {
                    "strand_id": f"{cable_identifier}.{number}",
                    "source_layer_id": cable.get("layer_id"),
                    "cable_id": cable_identifier,
                    "strand_no": number,
                    "status": "spare",
                },
                f"Strand {number} on {cable_identifier}.",
            )
        )
        created += 1
    if created == 0:
        plan.notices.append(Notice("present", f"{cable_identifier} already has strands 1-{count}."))
    else:
        plan.notices.append(Notice("created", f"Added {created} strand row(s) for {cable_identifier}."))


def _link_routes(plan: LinkPlan, cable: dict, ducts: list[dict], existing: set[tuple], tolerance: float) -> None:
    matched = ducts_along_cable(cable.get("coords") or [], ducts, tolerance)
    cable_identifier = feature_id(cable)
    order = 0
    for duct in matched:
        order += 1
        key = (cable.get("layer_id"), cable_identifier.casefold(), duct.get("layer_id"), duct.get("fid"))
        if key in existing:
            continue
        segment_identifier = feature_id(duct) or ""
        plan.inserts.append(
            RowInsert(
                "cable_route",
                {
                    "cable_source_layer_id": cable.get("layer_id"),
                    "cable_id": cable_identifier,
                    "segment_layer_id": duct.get("layer_id"),
                    "segment_fid": duct.get("fid"),
                    "segment_id": segment_identifier,
                    "route_order": order,
                },
                f"{cable_identifier} follows duct {segment_identifier or duct.get('fid')} at position {order}.",
            )
        )
    if matched:
        plan.notices.append(
            Notice("created", f"{cable_identifier} follows {len(matched)} duct segment(s). Existing rows were kept.")
        )


def _link_circuit(plan, cable, device, idf, device_end, existing, media: str) -> None:
    cable_identifier = feature_id(cable)
    device_identifier = feature_id(device)
    key = (device_identifier.casefold(), cable_identifier.casefold())
    if key in existing:
        plan.notices.append(Notice("present", f"Circuit {device_identifier} on {cable_identifier} already exists."))
        return
    values = cable.get("values") or {}
    if device_end == "a_end":
        panel_key, port_key = "z_panel", "z_port"
    else:
        panel_key, port_key = "a_panel", "a_port"
    device_values = device.get("values") or {}
    panel = text_of(values.get(panel_key)) or text_of(device_values.get("panel"))
    port = text_of(values.get(port_key)) or text_of(device_values.get("port"))
    numbers = strand_numbers_for({"strand_count": None, "port": port}) if media == "fiber" else []
    plan.inserts.append(
        RowInsert(
            "circuit",
            {
                "circuit_id": f"ckt.{device_identifier}.{cable_identifier}",
                "media_type": media,
                "device_layer_id": device.get("layer_id"),
                "device_id": device_identifier,
                "idf_layer_id": idf.get("layer_id"),
                "idf_id": feature_id(idf),
                "panel_id": panel,
                "panel_port": port,
                "cable_layer_id": cable.get("layer_id"),
                "cable_id": cable_identifier,
                "strand_from": numbers[0] if numbers else None,
                "strand_to": numbers[-1] if numbers else None,
                "status": text_of(values.get("status")) or "proposed",
            },
            f"Circuit from {device_identifier} along {cable_identifier} to {feature_id(idf)}.",
        )
    )
    plan.notices.append(
        Notice("created", f"Circuit {device_identifier} — {cable_identifier} — {feature_id(idf)}.")
    )


def _link_mount(plan: LinkPlan, device: dict, index: dict, existing: set[tuple], write_rows: bool) -> None:
    mount_id = text_of((device.get("values") or {}).get("mount"))
    device_identifier = feature_id(device)
    if not mount_id or not device_identifier:
        return
    structures = [hit for hit in resolve_end(index, mount_id, device) if hit.get("role") == "structure"]
    if not structures:
        plan.notices.append(Notice("warning", f"{device_identifier} mount {mount_id} does not match a pole or bollard."))
        return
    key = (device_identifier.casefold(), mount_id.casefold())
    if key in existing:
        plan.notices.append(Notice("present", f"{device_identifier} is already mounted on {mount_id}."))
        return
    plan.notices.append(Notice("present", f"{device_identifier} is mounted on {mount_id}."))
    if not write_rows:
        return
    plan.inserts.append(
        RowInsert(
            "mounted_device",
            {
                "mount_id": f"{mount_id}.{device_identifier}",
                "support_id": mount_id,
                "device_id": device_identifier,
                "device_type": (device.get("role") or "").upper(),
                "status": text_of((device.get("values") or {}).get("status")) or "proposed",
            },
            f"Mount {device_identifier} on {mount_id}.",
        )
    )
