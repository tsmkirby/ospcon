"""Handholes a cable passes through, and the rows that record that association."""

from __future__ import annotations

from .catalog import feature_id
from .geometry import closest_on_line


def handholes_on_cables(cables: list[dict], handholes: list[dict], tolerance: float) -> list[dict]:
    """Handholes the cables pass through, ordered along the run."""
    ranked = []
    seen = set()
    for cable_index, cable in enumerate(cables):
        coords = cable.get("coords") or []
        if len(coords) < 2:
            continue
        for handhole in handholes:
            key = (handhole.get("layer_id"), handhole.get("fid"))
            if key in seen:
                continue
            point = (handhole.get("coords") or [None])[0]
            if not point:
                continue
            _nearest, gap, measure = closest_on_line(point, coords)
            if gap <= tolerance:
                seen.add(key)
                ranked.append((cable_index, measure, handhole))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


def handhole_rows(cables: list[dict], handholes: list[dict], tolerance: float) -> list[dict]:
    rows = []
    for cable in cables:
        along = handholes_on_cables([cable], handholes, tolerance)
        cable_id = feature_id(cable)
        for position, handhole in enumerate(along, start=1):
            rows.append(
                {
                    "cable_source_layer_id": cable.get("layer_id"),
                    "cable_id": cable_id,
                    "handhole_layer_id": handhole.get("layer_id"),
                    "handhole_fid": handhole.get("fid"),
                    "handhole_id": feature_id(handhole) or "",
                    "route_order": position,
                }
            )
    return rows
