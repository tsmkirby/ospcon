"""Shared display notation. Stored asset identifiers are never rewritten."""
from .naming import local_asset_id

LINK = " ↔︎ "


def fiber_end(cable, start, end=None):
    span = str(start) if end is None or start == end else f"{start}-{end}"
    return f"{cable}:{span}"


def fiber_ranges(cable, numbers):
    groups = []
    for number in sorted(set(numbers)):
        if groups and number == groups[-1][1] + 1:
            groups[-1][1] = number
        else:
            groups.append([number, number])
    return [fiber_end(cable, start, end) for start, end in groups]


def fiber_link(a_cable, a_start, a_end, b_cable, b_start, b_end):
    left = fiber_end(a_cable, a_start, a_end)
    return left + LINK + (fiber_end(b_cable, b_start, b_end) if b_cable and b_start else "OPEN")


def panel_end(panel, port):
    return f"{panel}:{port}" if panel and port else str(panel or (f"port {port}" if port else ""))


def horizontal_link(room, switch, switch_port, panel, port, cable, cable_type, jacks=(), devices=()):
    """Site horizontal-link convention: active port -- panel -- cable -- outlet -- device."""
    origin = f"{room or '[IDF unknown]'} {local_asset_id(switch, room) or '[switch unknown]'} {switch_port or '[switch port unknown]'}"
    parts = [origin, panel_end(local_asset_id(panel, room) or '[panel unknown]', port or '[port unknown]'),
             f"[{cable} {cable_type or '[cable type unknown]'}]"]
    parts.extend(jacks)
    parts.extend(devices)
    return ' -- '.join(parts)
