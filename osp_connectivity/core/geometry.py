"""Line measurements, duct chaining, and cable splits in plain coordinates."""

from __future__ import annotations

import math
from typing import Optional, Sequence


Point = tuple[float, float]
Line = Sequence[Point]


def distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def length_of(coords: Line) -> float:
    total = 0.0
    for start, end in zip(coords, coords[1:]):
        total += distance(start, end)
    return total


def dedupe(coords: Sequence[Point], tolerance: float = 1e-6) -> list[Point]:
    cleaned: list[Point] = []
    for point in coords:
        if not cleaned or distance(cleaned[-1], point) > tolerance:
            cleaned.append((float(point[0]), float(point[1])))
    return cleaned


def _closest_on_segment(point: Point, start: Point, end: Point) -> tuple[Point, float]:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return start, 0.0
    ratio = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared
    ratio = max(0.0, min(1.0, ratio))
    return (start[0] + ratio * dx, start[1] + ratio * dy), ratio


def closest_on_line(point: Point, coords: Line) -> tuple[Point, float, float]:
    """Return the closest point, its distance, and the measure from the start."""
    best_point = coords[0]
    best_distance = float("inf")
    best_measure = 0.0
    traveled = 0.0
    for start, end in zip(coords, coords[1:]):
        candidate, ratio = _closest_on_segment(point, start, end)
        gap = distance(point, candidate)
        if gap < best_distance:
            best_distance = gap
            best_point = candidate
            best_measure = traveled + ratio * distance(start, end)
        traveled += distance(start, end)
    return best_point, best_distance, best_measure


def point_within(point: Point, coords: Line, tolerance: float) -> bool:
    if len(coords) < 2:
        return bool(coords) and distance(point, coords[0]) <= tolerance
    _point, gap, _measure = closest_on_line(point, coords)
    return gap <= tolerance


def cable_uses_duct(cable: Line, duct: Line, tolerance: float) -> bool:
    """True when a 2-point duct's ends both sit on the cable, or most vertices do."""
    if len(cable) < 2 or len(duct) < 2:
        return False
    hits = sum(1 for point in duct if point_within(point, cable, tolerance))
    return hits / float(len(duct)) >= 1.0 or (len(duct) > 2 and hits / float(len(duct)) >= 0.8)


def ducts_along_cable(cable: Line, ducts: Sequence[dict], tolerance: float) -> list[dict]:
    """Order duct records by where their midpoint falls along the cable."""
    ranked = []
    for duct in ducts:
        coords = duct.get("coords") or []
        if not cable_uses_duct(cable, coords, tolerance):
            continue
        midpoint = coords[len(coords) // 2]
        _point, _gap, measure = closest_on_line(midpoint, cable)
        ranked.append((measure, duct))
    ranked.sort(key=lambda item: item[0])
    return [duct for _measure, duct in ranked]


def split_linestring(coords: Line, point: Point, tolerance: float) -> dict:
    """Split a line at point. Endpoint hits and misses are reported and not split."""
    cleaned = dedupe(coords)
    if len(cleaned) < 2:
        return {"kind": "miss", "distance": None}
    nearest, gap, measure = closest_on_line(point, cleaned)
    total = length_of(cleaned)
    if gap > tolerance:
        return {"kind": "miss", "distance": gap}
    if measure <= tolerance or total - measure <= tolerance:
        return {"kind": "endpoint", "distance": gap, "at": nearest, "measure": measure}

    traveled = 0.0
    for index, (start, end) in enumerate(zip(cleaned, cleaned[1:])):
        segment_length = distance(start, end)
        if traveled + segment_length + 1e-9 >= measure:
            first = dedupe(list(cleaned[: index + 1]) + [nearest])
            second = dedupe([nearest] + list(cleaned[index + 1 :]))
            if len(first) < 2 or len(second) < 2:
                return {"kind": "endpoint", "distance": gap, "at": nearest, "measure": measure}
            return {
                "kind": "through",
                "distance": gap,
                "at": nearest,
                "measure": measure,
                "a": first,
                "b": second,
            }
        traveled += segment_length
    return {"kind": "miss", "distance": gap}


def _cluster_endpoints(segments: Sequence[dict], tolerance: float) -> list[int]:
    endpoints = []
    for segment in segments:
        coords = segment.get("coords") or []
        if len(coords) < 2:
            endpoints.append(None)
            endpoints.append(None)
            continue
        endpoints.append(coords[0])
        endpoints.append(coords[-1])
    parent = list(range(len(endpoints)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left in range(len(endpoints)):
        if endpoints[left] is None:
            continue
        for right in range(left + 1, len(endpoints)):
            if endpoints[right] is None:
                continue
            if distance(endpoints[left], endpoints[right]) <= tolerance:
                union(left, right)
    return [find(index) for index in range(len(endpoints))]


def chain_segments(segments: Sequence[dict], tolerance: float) -> tuple[list[dict], bool]:
    """Order touching lines into one path. The bool is false when they branch or have a gap."""
    usable = [segment for segment in segments if segment.get("coords") and len(segment["coords"]) >= 2]
    if len(usable) <= 1:
        return list(usable), True

    nodes = _cluster_endpoints(usable, tolerance)
    groups: dict[int, list[int]] = {}
    for endpoint_index, node in enumerate(nodes):
        segment_index = endpoint_index // 2
        groups.setdefault(node, [])
        if segment_index not in groups[node]:
            groups[node].append(segment_index)

    adjacency: dict[int, set[int]] = {index: set() for index in range(len(usable))}
    for members in groups.values():
        if len(members) < 2:
            continue
        if len(members) > 2:
            return list(usable), False
        adjacency[members[0]].add(members[1])
        adjacency[members[1]].add(members[0])

    ends = [index for index, neighbors in adjacency.items() if len(neighbors) <= 1]
    if len(ends) != 2:
        return list(usable), False

    def free_point(index: int) -> Point:
        for which in (0, 1):
            node = nodes[index * 2 + which]
            if len(groups.get(node, [])) == 1:
                coords = usable[index]["coords"]
                return coords[0] if which == 0 else coords[-1]
        return usable[index]["coords"][0]

    start = min(ends, key=lambda index: (free_point(index)[0], free_point(index)[1]))
    order = [start]
    previous: Optional[int] = None
    current = start
    while len(order) < len(usable):
        nxt = [item for item in adjacency[current] if item != previous]
        if len(nxt) != 1:
            return list(usable), False
        previous, current = current, nxt[0]
        if current in order:
            return list(usable), False
        order.append(current)
    return [usable[index] for index in order], True
