"""Write map layers to a KMZ file Google Earth on the web can import.

The document is KML 2.2: folders, placemarks, points, lines, polygons, and
simple styles. Earth web stores an imported file in a Drive project and opens
that project in the phone app. Desktop-only KML (tours, models, screen
overlays, network links, HTML balloons) is left out.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import zipfile
from xml.sax.saxutils import escape

# Earth web imports up to these counts as editable project features. A larger
# file is still imported, as a read-only data layer. One line or polygon over
# the vertex count is simplified by Earth on the way in.
EDITABLE_FEATURE_LIMIT = 10_000
EDITABLE_VERTEX_LIMIT = 250_000
SIMPLIFY_VERTEX_LIMIT = 10_000

_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

# Trace colors from the map highlight, stored as KML aabbggrr.
_STYLES = {
    "cable": ("ff1478e6", 3),
    "duct": ("ffb48246", 2),
    "device": ("ffc85a14", 2),
    "idf": ("ff3c8c14", 2),
    "enclosure": ("ffa01eb4", 2),
    "handhole": ("ff145a8c", 2),
    "structure": ("ff285078", 2),
    "building": ("ff788c5a", 2),
    "panel": ("ffa07814", 2),
    "default": ("ff5a5a5a", 2),
}

_ROLE_STYLE = {
    "cable": "cable",
    "duct": "duct",
    "ap": "device",
    "cctv": "device",
    "data_jack": "device",
    "pos": "device",
    "idf": "idf",
    "splice_enc": "enclosure",
    "handhole": "handhole",
    "structure": "structure",
    "building": "building",
    "patch_panel": "panel",
    "fdp": "panel",
}


def write_kmz(path: str, layers: list[dict], **limits) -> dict:
    """Write one KMZ. Returns feature and vertex counts for the result message."""
    kml, summary = build_kml(layers, **limits)
    if summary["features"] == 0:
        raise ValueError("Nothing to export. Every feature was empty or outside WGS84.")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    descriptor, temporary = tempfile.mkstemp(suffix=".kmz", dir=directory)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("doc.kml", kml.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return summary


def build_kml(
    layers: list[dict],
    *,
    feature_limit: int = EDITABLE_FEATURE_LIMIT,
    vertex_limit: int = EDITABLE_VERTEX_LIMIT,
    simplify_limit: int = SIMPLIFY_VERTEX_LIMIT,
) -> tuple[str, dict]:
    """Build the KML document from layers that are already in longitude, latitude.

    A layer is ``{"name", "role", "features"}``. A feature is
    ``{"name", "attributes", "kind", "parts"}``. ``kind`` is ``point``,
    ``line``, or ``polygon``. Point parts are ``(lon, lat)``. Line parts are
    lists of those pairs. Polygon parts are ``{"outer", "holes"}``. Multipart
    features become one placemark per part.
    """
    rendered = []
    rejected = 0
    simplified = []
    feature_count = 0
    vertex_count = 0
    for layer in layers or []:
        placemarks = []
        style_id = _style_id(layer.get("role"))
        for feature in layer.get("features") or []:
            name, attributes, ready, bad = _parts(feature)
            rejected += bad
            count = len(ready)
            for index, (kind, geometry, vertices) in enumerate(ready):
                placemark_name = _part_name(name, index, count)
                placemarks.append((placemark_name, attributes, kind, geometry))
                feature_count += 1
                vertex_count += vertices
                if vertices > simplify_limit:
                    simplified.append(placemark_name)
        if placemarks:
            rendered.append((layer.get("name") or "Layer", style_id, placemarks))

    styles = []
    seen = set()
    for _name, style_id, _placemarks in rendered:
        if style_id in seen:
            continue
        seen.add(style_id)
        color, width = _STYLES[style_id]
        styles.append(_style_xml(style_id, color, width))

    folders = [_folder_xml(name, style_id, placemarks) for name, style_id, placemarks in rendered]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "  <Document>",
        "    <name>OSP Connectivity</name>",
        *styles,
        *folders,
        "  </Document>",
        "</kml>",
        "",
    ]
    summary = {
        "features": feature_count,
        "vertices": vertex_count,
        "within_editable_limits": feature_count <= feature_limit and vertex_count <= vertex_limit,
        "simplified": simplified,
        "rejected": rejected,
    }
    return "\n".join(lines), summary


def export_note(summary: dict, collected: dict | None = None) -> str:
    """Plain-language result: counts, Earth web limits, and the import steps."""
    collected = collected or {}
    lines = [
        f"Wrote {summary.get('features', 0):,} feature(s) and {summary.get('vertices', 0):,} vertices.",
        "In Google Earth on the web, choose New → Import file to project, or open a project and choose File → Import.",
        "The project is saved in Google Drive. Open it in the Google Earth app to compare the phone's GPS position with these features.",
    ]
    if summary.get("within_editable_limits"):
        lines.append("This file is within the editable project limits of 10,000 features and 250,000 vertices.")
    else:
        lines.append(
            "This file is past the editable project limits of 10,000 features or 250,000 vertices. "
            "Import it as a data layer. It stays in the Drive project and on the phone, and the features are not individually editable."
        )
    simplified = list(summary.get("simplified") or [])
    if simplified:
        shown = ", ".join(simplified[:8])
        if len(simplified) > 8:
            shown += f" and {len(simplified) - 8} more"
        lines.append(
            "Google Earth will simplify these lines or polygons because each has more than 10,000 vertices: "
            f"{shown}. The file still contains every vertex."
        )
    rejected = summary.get("rejected") or 0
    if rejected:
        lines.append(f"Left out {rejected} feature part(s) with empty geometry or coordinates outside WGS84.")
    _append_collected(lines, collected)
    lines.append(
        "The phone's GPS is only as accurate as that phone. "
        "The file keeps the source coordinates in WGS84 and does not add survey-grade positioning."
    )
    return "\n".join(lines)


def empty_export_message(collected: dict, selected_only: bool) -> str:
    lines = ["Nothing with a map location was exported."]
    if selected_only:
        lines.append("Only features selected on the map were requested.")
    _append_collected(lines, collected)
    return "\n".join(lines)


def _append_collected(lines: list[str], collected: dict) -> None:
    skipped_geometry = collected.get("skipped_geometry") or {}
    if skipped_geometry:
        details = ", ".join(f"{name} ({count})" for name, count in skipped_geometry.items())
        lines.append("Left out features with empty or unsupported geometry: " + details + ".")
    skipped_crs = collected.get("skipped_crs") or []
    if skipped_crs:
        lines.append("Skipped layers with no usable CRS: " + ", ".join(skipped_crs) + ".")
    skipped_transform = collected.get("skipped_transform") or []
    if skipped_transform:
        lines.append("Could not transform these layers to WGS84: " + ", ".join(skipped_transform) + ".")
    partial = collected.get("partial_failures") or []
    if partial:
        lines.append("Some features could not be transformed to WGS84: " + ", ".join(partial) + ".")
    empty_selection = collected.get("empty_selection") or []
    if empty_selection:
        lines.append("No features are selected on: " + ", ".join(empty_selection) + ".")


def _style_id(role) -> str:
    if not role:
        return "default"
    return _ROLE_STYLE.get(role, "default")


def _parts(feature: dict):
    kind = feature.get("kind")
    parts = feature.get("parts") or []
    name = (feature.get("name") or "").strip() or "Feature"
    attributes = feature.get("attributes") if isinstance(feature.get("attributes"), dict) else {}
    ready = []
    rejected = 0
    if not parts:
        return name, attributes, ready, 1
    if kind == "point":
        for part in parts:
            point = _as_point(part)
            if point is None:
                rejected += 1
                continue
            ready.append(("point", [point], 1))
    elif kind == "line":
        for part in parts:
            line = _as_line(part)
            if line is None:
                rejected += 1
                continue
            ready.append(("line", line, len(line)))
    elif kind == "polygon":
        for part in parts:
            polygon = _as_polygon(part)
            if polygon is None:
                rejected += 1
                continue
            vertices = len(polygon["outer"]) + sum(len(hole) for hole in polygon["holes"])
            ready.append(("polygon", polygon, vertices))
    else:
        rejected += 1
    return name, attributes, ready, rejected


def _part_name(name: str, index: int, count: int) -> str:
    if count <= 1 or index == 0:
        return name
    return f"{name} ({index + 1})"


def _as_point(part):
    if not isinstance(part, (list, tuple)) or len(part) < 2 or isinstance(part[0], (list, tuple)):
        return None
    try:
        lon = float(part[0])
        lat = float(part[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat) and -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    return (lon, lat)


def _as_line(part):
    if not isinstance(part, (list, tuple)) or len(part) < 2:
        return None
    points = []
    for pair in part:
        point = _as_point(pair)
        if point is None:
            return None
        points.append(point)
    if len(points) < 2:
        return None
    return points


def _as_ring(coords):
    if not isinstance(coords, (list, tuple)) or len(coords) < 3:
        return None
    points = []
    for pair in coords:
        point = _as_point(pair)
        if point is None:
            return None
        points.append(point)
    if len(points) < 3:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    if len(points) < 4:
        return None
    return points


def _as_polygon(part):
    if not isinstance(part, dict):
        return None
    outer = _as_ring(part.get("outer"))
    if outer is None:
        return None
    holes = []
    for hole in part.get("holes") or []:
        ring = _as_ring(hole)
        if ring is None:
            return None
        holes.append(_orient_ring(ring, counterclockwise=False))
    return {"outer": _orient_ring(outer, counterclockwise=True), "holes": holes}


def _orient_ring(points, *, counterclockwise):
    # Unwrap longitude for rings crossing the antimeridian. Keep the original
    # coordinates and starting vertex; only reverse their order if necessary.
    unwrapped = [(0.0, points[0][1])]
    for previous, current in zip(points, points[1:]):
        delta = (current[0] - previous[0] + 180.0) % 360.0 - 180.0
        unwrapped.append((unwrapped[-1][0] + delta, current[1]))
    area = sum((a[0] * b[1] - b[0] * a[1]) for a, b in zip(unwrapped, unwrapped[1:]))
    if area and (area > 0) != counterclockwise:
        return list(reversed(points))
    return points


def _format_coord(lon: float, lat: float) -> str:
    return f"{lon:.8f},{lat:.8f}"


def _coordinates(points) -> str:
    return " ".join(_format_coord(lon, lat) for lon, lat in points)


def _description(attributes: dict) -> str:
    lines = []
    for key, value in attributes.items():
        label = "" if key is None else str(key).strip()
        text = "" if value is None else str(value).strip()
        if not label or not text:
            continue
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


def _xml(value) -> str:
    return escape(_INVALID_XML.sub("", str(value)), {'"': "&quot;", "'": "&apos;"})


def _style_xml(style_id: str, color: str, width: int) -> str:
    fill = "66" + color[2:]
    return (
        f'  <Style id="{style_id}">\n'
        f"    <IconStyle><color>{color}</color></IconStyle>\n"
        f"    <LineStyle><color>{color}</color><width>{width}</width></LineStyle>\n"
        f"    <PolyStyle><color>{fill}</color></PolyStyle>\n"
        f"  </Style>"
    )


def _placemark_xml(name: str, style_id: str, attributes: dict, kind: str, geometry) -> str:
    lines = [
        "    <Placemark>",
        f"      <name>{_xml(name)}</name>",
        f"      <styleUrl>#{style_id}</styleUrl>",
    ]
    description = _description(attributes)
    if description:
        lines.append(f"      <description>{_xml(description)}</description>")
    if kind == "point":
        lines.extend([
            "      <Point>",
            "        <altitudeMode>clampToGround</altitudeMode>",
            f"        <coordinates>{_format_coord(*geometry[0])}</coordinates>",
            "      </Point>",
        ])
    elif kind == "line":
        lines.extend([
            "      <LineString>",
            "        <tessellate>1</tessellate>",
            "        <altitudeMode>clampToGround</altitudeMode>",
            f"        <coordinates>{_coordinates(geometry)}</coordinates>",
            "      </LineString>",
        ])
    else:
        lines.extend([
            "      <Polygon>",
            "        <tessellate>1</tessellate>",
            "        <altitudeMode>clampToGround</altitudeMode>",
            "        <outerBoundaryIs><LinearRing>",
            f"          <coordinates>{_coordinates(geometry['outer'])}</coordinates>",
            "        </LinearRing></outerBoundaryIs>",
        ])
        for hole in geometry["holes"]:
            lines.extend([
                "        <innerBoundaryIs><LinearRing>",
                f"          <coordinates>{_coordinates(hole)}</coordinates>",
                "        </LinearRing></innerBoundaryIs>",
            ])
        lines.append("      </Polygon>")
    lines.append("    </Placemark>")
    return "\n".join(lines)


def _folder_xml(name: str, style_id: str, placemarks) -> str:
    body = "\n".join(
        _placemark_xml(placemark_name, style_id, attributes, kind, geometry)
        for placemark_name, attributes, kind, geometry in placemarks
    )
    return f"  <Folder>\n    <name>{_xml(name)}</name>\n{body}\n  </Folder>"
