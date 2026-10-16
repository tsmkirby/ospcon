"""Switches on an IDF, and the patches that connect a panel port to a switch port."""

from __future__ import annotations

import re

from .catalog import feature_id
from .fields import next_id, parse_count, text_of
from .panels import panel_is_simplex, panel_ports


def suggest_switch_id(existing, idf_id: str) -> str:
    """VIB-IDF-0002 suggests the next VIB-SW- number."""
    match = re.match(r"^([A-Za-z]{2,8})-", text_of(idf_id) or "")
    venue = match.group(1).upper() if match else "OSP"
    return next_id(list(existing), f"{venue}-SW-0000")


def expand_switch_ports(count: int, first: str) -> list[dict]:
    """gi1/0/1 with a count of 3 becomes gi1/0/1, gi1/0/2, gi1/0/3."""
    if count < 1:
        raise ValueError("Enter the number of switch ports.")
    text = (first or "").strip() or "gi1/0/1"
    match = re.match(r"^(.*?)(\d+)$", text)
    if not match:
        raise ValueError("Switch port id should end with a number, such as gi1/0/1.")
    prefix, raw, start = match.group(1), match.group(2), int(match.group(2))
    pad = len(raw) if raw.startswith("0") and len(raw) > 1 else 0
    ports = []
    for offset in range(int(count)):
        number = start + offset
        identifier = f"{prefix}{number:0{pad}d}" if pad else f"{prefix}{number}"
        ports.append({"switch_port_id": identifier, "switch_port_name": identifier})
    return ports


def switches_for_idf(idf_id: str, switches: list[dict]) -> list[dict]:
    return [row for row in switches if _same(row.get("mdf_idf_id"), idf_id) and text_of(row.get("sw_id"))]


def choice_label(identifier, name) -> str:
    """'PP-1  Lobby' when a name is present, otherwise just the id."""
    text = text_of(identifier) or ""
    label = text_of(name) or ""
    if label and label.casefold() != text.casefold():
        return f"{text} {label}"
    return text


def panels_on_idf(idf_id: str, features: list[dict]) -> list[dict]:
    return panels_for_idf(idf_id, features, "copper") + panels_for_idf(idf_id, features, "fiber")


def switch_inventory(switch: dict, ports: list[dict], patches: list[dict]) -> dict:
    """Every port on a switch, and the panel port patched to each one."""
    sw_id = text_of(switch.get("sw_id")) or ""
    patched = {}
    for row in patches:
        if not _same(row.get("sw_id"), sw_id):
            continue
        key = (text_of(row.get("switch_port_id")) or "").casefold()
        if key:
            patched[key] = row
    items = []
    for port in ports:
        if not _same(port.get("sw_id"), sw_id):
            continue
        identifier = text_of(port.get("switch_port_id")) or ""
        hit = patched.get(identifier.casefold())
        items.append(
            {
                "label": identifier,
                "switch_port_id": identifier,
                "switch_port_name": text_of(port.get("switch_port_name")) or "",
                "occupied": hit is not None,
                "panel_id": text_of((hit or {}).get("panel_id")) or "",
                "panel_port": text_of((hit or {}).get("panel_port")) or "",
            }
        )
    count = parse_count(switch.get("sw_port_count")) or len(items)
    return {
        "title": choice_label(sw_id, switch.get("sw_name")),
        "sw_id": sw_id,
        "sw_name": text_of(switch.get("sw_name")) or "",
        "idf_id": text_of(switch.get("mdf_idf_id")) or "",
        "count": max(count, len(items)),
        "ports": items,
        "legend": "Blue ports are patched. Click one to see the patch panel and port.",
    }


def panels_for_idf(idf_id: str, features: list[dict], media: str) -> list[dict]:
    role = "fdp" if media == "fiber" else "patch_panel"
    found = []
    for feature in features:
        if feature.get("role") != role:
            continue
        if _same((feature.get("values") or {}).get("idf_ref"), idf_id) and feature_id(feature):
            found.append(feature)
    return found


def open_panel_ports(panel: dict, features: list[dict], tables: dict, media: str, single_fiber: bool) -> list[str]:
    """Panel ports with no patching record.

    A device or rear cable on a port leaves it available. Fiber is offered as
    odd-even pairs unless the panel is simplex or a single fiber is requested.
    """
    inventory = panel_ports(panel, features, tables)
    taken = {port["number"] for port in inventory["ports"] if port.get("patched")}
    total = inventory["count"]
    if not total:
        return []
    if media != "fiber" or single_fiber or panel_is_simplex(panel):
        return [str(number) for number in range(1, total + 1) if number not in taken]
    choices = []
    number = 1
    while number + 1 <= total:
        if number not in taken and number + 1 not in taken:
            choices.append(f"{number}-{number + 1}")
        number += 2
    return choices


def open_switch_ports(sw_id: str, ports: list[dict], patches: list[dict]) -> list[dict]:
    used = {
        (text_of(row.get("switch_port_id")) or "").casefold()
        for row in patches
        if _same(row.get("sw_id"), sw_id)
    }
    return [
        row
        for row in ports
        if _same(row.get("sw_id"), sw_id)
        and text_of(row.get("switch_port_id"))
        and (text_of(row.get("switch_port_id")) or "").casefold() not in used
    ]


def patches_for_idf(idf_id: str, patches: list[dict]) -> list[dict]:
    return [row for row in patches if _same(row.get("mdf_idf_id"), idf_id)]


def patch_line(row: dict) -> str:
    kind = "fiber" if (text_of(row.get("media_type")) or "").casefold() == "fiber" else "copper"
    return (
        f"{kind}: {text_of(row.get('panel_id')) or '?'} port {text_of(row.get('panel_port')) or '?'} "
        f"to {text_of(row.get('sw_id')) or '?'} {text_of(row.get('switch_port_id')) or '?'}"
    )


def new_patch(choices: dict) -> dict:
    panel_id = text_of(choices.get("panel_id"))
    panel_port = text_of(choices.get("panel_port"))
    sw_id = text_of(choices.get("sw_id"))
    switch_port_id = text_of(choices.get("switch_port_id"))
    if not panel_id or not panel_port or not sw_id or not switch_port_id:
        raise ValueError("Choose a panel port and a switch port.")
    media = "fiber" if choices.get("media") == "fiber" else "copper"
    return {
        "patch_id": f"{panel_id}.{panel_port}.{sw_id}.{switch_port_id}",
        "mdf_idf_id": text_of(choices.get("idf_id")),
        "media_type": media,
        "panel_id": panel_id,
        "panel_port": panel_port,
        "single_fiber": 1 if media == "fiber" and choices.get("single_fiber") else 0,
        "sw_id": sw_id,
        "switch_port_id": switch_port_id,
        "switch_port_name": text_of(choices.get("switch_port_name")) or switch_port_id,
        "status": "proposed",
    }


def _same(left, right) -> bool:
    text = text_of(left)
    other = text_of(right)
    return bool(text) and bool(other) and text.casefold() == other.casefold()
