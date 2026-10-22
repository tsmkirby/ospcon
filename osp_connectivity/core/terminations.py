"""Changing what is terminated on a patch panel: copper cable ports and fiber strand positions."""

from __future__ import annotations

import re

from .catalog import DEVICE_ROLES, feature_id
from .fields import parse_count, parse_range, text_of
from .panels import _format_ports, _is_fiber, _same_text

COPPER = "copper"
FIBER = "fiber"


def panel_media(panel: dict, features: list[dict]) -> str:
    """Fiber for an FDP or a panel marked fiber, or one whose landing cables are fiber; otherwise copper."""
    if panel.get("role") == "fdp":
        return FIBER
    values = panel.get("values") or {}
    for key in ("media", "media_type", "panel_type"):
        text = (text_of(values.get(key)) or "").casefold()
        if text.startswith("fiber") or text in ("fdp", "lgx", "lc", "sc"):
            return FIBER
        if text.startswith("copper"):
            return COPPER
    landing = [cable for cable, _end in landing_cables(feature_id(panel) or "", features)]
    if landing and all(_is_fiber(cable.get("values") or {}) for cable in landing):
        return FIBER
    return COPPER


def panel_capacity(panel: dict) -> int:
    values = panel.get("values") or {}
    for key in ("port_count", "position_count", "ports"):
        count = parse_count(values.get(key))
        if count:
            return count
    return 0


def landing_cables(panel_id: str, features: list[dict]) -> list[tuple[dict, str]]:
    """Cables with an end that names this panel, and which end ('a' or 'z')."""
    found = []
    for feature in features:
        if feature.get("role") != "cable":
            continue
        values = feature.get("values") or {}
        for end in ("a", "z"):
            if _same_text(values.get(f"{end}_panel"), panel_id):
                found.append((feature, end))
    found.sort(key=lambda item: ((feature_id(item[0]) or "").casefold(), item[1]))
    return found


def strict_ports(text) -> list[int]:
    """Port numbers typed by a person: '12', '1-4', or '1-4, 9'. Anything else is refused."""
    value = str(text or "").strip()
    if not re.fullmatch(r"\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*", value):
        raise ValueError("Use a port number or a range, such as 12, 1-4, or 1-4, 9.")
    numbers = []
    for part in value.split(","):
        first, _dash, last = part.partition("-")
        first, last = int(first), int(last or first)
        if first < 1 or last < first:
            raise ValueError("Ports start at 1, and a range must run upward, such as 1-4.")
        numbers.extend(range(first, last + 1))
    if len(set(numbers)) != len(numbers):
        raise ValueError("A port is listed twice.")
    return numbers


def _remap(value, old: list[int], new: list[int]) -> str | None:
    """Move the ports in ``value`` from ``old`` to the matching positions in ``new``."""
    numbers = parse_range(value)
    if not numbers or not set(numbers) & set(old):
        return None
    if len(old) == len(new):
        mapping = dict(zip(old, new))
        return _format_ports([mapping.get(number, number) for number in numbers])
    return _format_ports(new)


# ----- copper -------------------------------------------------------------


def copper_rows(panel: dict, features: list[dict]) -> list[dict]:
    """Each cable end landing on the panel, with its ports and the devices it serves here."""
    panel_id = feature_id(panel) or ""
    rows = []
    for cable, end in landing_cables(panel_id, features):
        values = cable.get("values") or {}
        devices = [item for item in features if item.get("role") in DEVICE_ROLES
                   and _same_text((item.get("values") or {}).get("cable_ref"), feature_id(cable))
                   and _same_text((item.get("values") or {}).get("panel"), panel_id)]
        rows.append({"cable": cable, "end": end, "ports": text_of(values.get(f"{end}_port")) or "",
                     "devices": devices})
    return rows


def copper_changes(panel: dict, rows: list[dict], new_ports: list[str], features: list[dict], tables: dict,
                   move_patches: bool = True) -> dict:
    """Validate new copper ports and list every edit that keeps the panel consistent.

    ``new_ports`` matches ``rows``. A blank entry leaves that cable unchanged.
    Raises ValueError naming each problem.
    """
    panel_id = feature_id(panel) or ""
    capacity = panel_capacity(panel)
    problems = []
    final = {}
    changed = []
    for row, text in zip(rows, new_ports):
        label = f"{feature_id(row['cable'])} ({row['end'].upper()} end)"
        text = str(text or "").strip()
        old = parse_range(row["ports"])
        if not text or text == row["ports"]:
            final[id(row)] = old
            continue
        try:
            new = strict_ports(text)
        except ValueError as exc:
            problems.append(f"{label}: {exc}")
            continue
        if capacity and max(new) > capacity:
            problems.append(f"{label}: {panel_id} has {capacity} ports.")
        final[id(row)] = new
        if new != old:
            changed.append((row, old, new))
    owners: dict[int, str] = {}
    for row in rows:
        for number in final.get(id(row), []):
            label = feature_id(row["cable"])
            if number in owners and owners[number] != label:
                problems.append(f"Port {number} is given to both {owners[number]} and {label}.")
            owners[number] = label
    # Ports held by something that does not move with these cables: another device or termination.
    from .panels import panel_ports

    landing = {(feature_id(row["cable"]) or "").casefold() for row in rows}
    moving_devices = {(device.get("layer_id"), device.get("fid")) for row in rows for device in row["devices"]}
    blocked = {}
    for port in panel_ports(panel, features, tables)["ports"]:
        holder_cable = (port.get("cable_id") or "").casefold()
        device = port.get("device")
        if holder_cable in landing:
            continue
        if device is not None and (device.get("layer_id"), device.get("fid")) in moving_devices:
            continue
        if port.get("cable_id") or port.get("device_id"):
            blocked[port["number"]] = port.get("cable_id") or port.get("device_id")
    for row, _old, new in changed:
        for number in new:
            if number in blocked:
                problems.append(f"{feature_id(row['cable'])}: port {number} is already used by {blocked[number]}.")
    if problems:
        raise ValueError("\n".join(dict.fromkeys(problems)))

    cable_edits, device_edits, row_updates = [], [], []
    for row, old, new in changed:
        cable = row["cable"]
        new_text = _format_ports(new)
        cable_edits.append({"layer_id": cable["layer_id"], "fid": cable["fid"],
                            "canonical": f"{row['end']}_port", "value": new_text})
        for device in row["devices"]:
            moved = _remap((device.get("values") or {}).get("port"), old, new) if old else new_text
            if moved is not None:
                device_edits.append({"layer_id": device["layer_id"], "fid": device["fid"],
                                     "canonical": "port", "value": moved})
        identifier = feature_id(cable)
        buckets = [("circuits", ("panel_id",), "panel_port"), ("terminations", ("pp_id", "panel_id"), "panel_port")]
        for bucket, panel_keys, port_key in buckets:
            for record in tables.get(bucket) or []:
                if record.get("_fid") is None or not _same_text(record.get("cable_id"), identifier):
                    continue
                if not any(_same_text(record.get(key), panel_id) for key in panel_keys):
                    continue
                moved = _remap(record.get(port_key), old, new)
                if moved is not None:
                    row_updates.append({"layer_id": record["_layer_id"], "fid": record["_fid"], "values": {port_key: moved}})
        if move_patches and old:
            for record in tables.get("patches") or []:
                if record.get("_fid") is None or not _same_text(record.get("panel_id"), panel_id):
                    continue
                moved = _remap(record.get("panel_port"), old, new)
                if moved is not None:
                    row_updates.append({"layer_id": record["_layer_id"], "fid": record["_fid"], "values": {"panel_port": moved}})
    return {"cable_edits": cable_edits, "device_edits": device_edits, "row_updates": row_updates,
            "changed": [(feature_id(row["cable"]), _format_ports(old), _format_ports(new)) for row, old, new in changed]}


# ----- fiber --------------------------------------------------------------


def fiber_terminations(panel: dict, tables: dict) -> list[dict]:
    """Strand terminations stored for this panel, one per strand, ordered by port."""
    panel_id = feature_id(panel) or ""
    rows = []
    for row in tables.get("terminations") or []:
        if row.get("strand_no") in (None, "") or not _same_text(row.get("fdp_id") or row.get("pp_id") or row.get("panel_id"), panel_id):
            continue
        port = parse_range(row.get("panel_position") or row.get("panel_port") or row.get("port"))
        rows.append({"cable_id": text_of(row.get("cable_id")) or "", "strand": int(float(row["strand_no"])),
                     "port": port[0] if port else None, "status": text_of(row.get("status")) or "",
                     "_fid": row.get("_fid"), "_layer_id": row.get("_layer_id")})
    rows.sort(key=lambda row: (row["port"] is None, row["port"] or 0, row["cable_id"].casefold(), row["strand"]))
    return rows


def termination_groups(rows: list[dict]) -> list[dict]:
    """Consecutive strands of one cable on consecutive ports, as one line."""
    groups = []
    for row in rows:
        last = groups[-1] if groups else None
        if (last and last["cable_id"].casefold() == row["cable_id"].casefold() and row["port"] is not None
                and last["port_to"] is not None and row["strand"] == last["strand_to"] + 1
                and row["port"] == last["port_to"] + 1 and row["status"] == last["status"]):
            last["strand_to"], last["port_to"] = row["strand"], row["port"]
            last["rows"].append(row)
            continue
        groups.append({"cable_id": row["cable_id"], "strand_from": row["strand"], "strand_to": row["strand"],
                       "port_from": row["port"], "port_to": row["port"], "status": row["status"], "rows": [row]})
    return groups


def fiber_candidates(panel: dict, features: list[dict]) -> list[dict]:
    """Fiber cables that can be terminated here: those landing on the panel, then those ending at its IDF."""
    panel_id = feature_id(panel) or ""
    idf_id = text_of((panel.get("values") or {}).get("idf_ref"))
    found, seen = [], set()
    for feature in features:
        if feature.get("role") != "cable" or not _is_fiber(feature.get("values") or {}):
            continue
        values = feature.get("values") or {}
        ends = [end for end in ("a", "z") if _same_text(values.get(f"{end}_panel"), panel_id)]
        at_idf = [end for end in ("a", "z") if idf_id and _same_text(values.get(f"{end}_end"), idf_id)]
        if not ends and not at_idf:
            continue
        key = (feature.get("layer_id"), feature.get("fid"))
        if key in seen:
            continue
        seen.add(key)
        found.append({"cable": feature, "end": (ends or at_idf)[0], "landing": bool(ends),
                      "count": parse_count(values.get("strand_count")) or 0})
    found.sort(key=lambda item: (not item["landing"], (feature_id(item["cable"]) or "").casefold()))
    return found


def used_ports(panel: dict, features: list[dict], tables: dict, removed=()) -> dict[int, str]:
    """Port number to what holds it, leaving out termination rows being removed.

    A port held by a cable's own end (its port field or its assumed block)
    is listed under that cable's id, so the cable's strands can land there.
    """
    from .panels import panel_ports

    removed = {(row.get("_layer_id"), row.get("_fid")) for row in removed}
    kept = dict(tables)
    kept["terminations"] = [row for row in tables.get("terminations") or []
                            if (row.get("_layer_id"), row.get("_fid")) not in removed or row.get("_fid") is None]
    held = {}
    for port in panel_ports(panel, features, kept)["ports"]:
        if port["occupied"]:
            held[port["number"]] = port.get("cable_id") or port.get("device_id") or port.get("switch_id") or "in use"
    return held


def blocked_ports(panel: dict, cable_id: str, features: list[dict], tables: dict, removed=()) -> set[int]:
    """Ports a strand of ``cable_id`` cannot land on: held by anything else, or by another strand."""
    removed_keys = {(row.get("_layer_id"), row.get("_fid")) for row in removed}
    blocked = {number for number, holder in used_ports(panel, features, tables, removed).items()
               if holder.casefold() != (cable_id or "").casefold()}
    blocked |= {row["port"] for row in fiber_terminations(panel, tables)
                if row["port"] is not None and (row["_layer_id"], row["_fid"]) not in removed_keys}
    return blocked


def next_free_port(held, count: int, after: int = 0) -> int:
    """The first port of a free run of ``count`` ports, starting after ``after``."""
    port = max(after, 0) + 1
    while any(port + offset in held for offset in range(max(count, 1))):
        port += 1
    return port


def fiber_additions(panel: dict, additions: list[dict], features: list[dict], tables: dict, removed=()) -> dict:
    """Validate new strand terminations and build their rows.

    Each addition is ``{"cable": feature, "strand_from", "strand_to", "port_from"}``.
    Raises ValueError naming each problem.
    """
    panel_id = feature_id(panel) or ""
    capacity = panel_capacity(panel)
    held = used_ports(panel, features, tables, removed)
    removed_keys = {(row.get("_layer_id"), row.get("_fid")) for row in removed}
    kept = [row for row in fiber_terminations(panel, tables) if (row["_layer_id"], row["_fid"]) not in removed_keys]
    terminated = {(row["cable_id"].casefold(), row["strand"]) for row in kept}
    # A strand already on a port blocks it, even for another strand of the same cable.
    strand_ports = {row["port"]: f"{row['cable_id']}:{row['strand']}" for row in kept if row["port"] is not None}
    problems, rows, panel_edits = [], [], []
    assigned: set[int] = set()
    for addition in additions:
        cable = addition["cable"]
        identifier = feature_id(cable) or ""
        values = cable.get("values") or {}
        count = parse_count(values.get("strand_count")) or 0
        first, last, port = int(addition["strand_from"]), int(addition["strand_to"]), int(addition["port_from"])
        label = f"{identifier}:{first}" + (f"-{last}" if last != first else "")
        if first < 1 or last < first:
            problems.append(f"{label}: choose a strand range that runs upward from 1.")
            continue
        if count and last > count:
            problems.append(f"{label}: {identifier} only has {count} strands.")
        for offset, strand in enumerate(range(first, last + 1)):
            number = port + offset
            if (identifier.casefold(), strand) in terminated:
                problems.append(f"{label}: strand {strand} is already terminated on {panel_id}.")
                break
            if capacity and number > capacity:
                problems.append(f"{label}: {panel_id} has {capacity} ports, so port {number} does not exist.")
                break
            if number in strand_ports:
                problems.append(f"{label}: port {number} already has {strand_ports[number]}.")
                break
            if number in assigned or (number in held and held[number].casefold() != identifier.casefold()):
                problems.append(f"{label}: port {number} is already used by {held.get(number) or identifier}.")
                break
            terminated.add((identifier.casefold(), strand))
            assigned.add(number)
            held[number] = identifier
            rows.append({"cable_id": identifier, "strand_no": strand, "fdp_id": panel_id,
                         "panel_position": str(number), "status": addition.get("status") or "proposed"})
        # Name the panel on the cable end at this IDF when that end has no panel yet.
        end = addition.get("end")
        if end and not text_of(values.get(f"{end}_panel")) and not any(
                edit["fid"] == cable.get("fid") and edit["layer_id"] == cable.get("layer_id") for edit in panel_edits):
            panel_edits.append({"layer_id": cable.get("layer_id"), "fid": cable.get("fid"),
                                "canonical": f"{end}_panel", "value": panel_id})
    if problems:
        raise ValueError("\n".join(dict.fromkeys(problems)))
    return {"rows": rows, "cable_edits": panel_edits}
