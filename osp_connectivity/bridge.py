"""Read and write the open QGIS project. Core modules decide what the edits mean."""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRelation,
    QgsUnitTypes,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .core.fields import id_candidates, text_of
from .core.roles import inspect_layer

TABLE_ROLES = {
    "fiber_splice",
    "fiber_strand",
    "fiber_termination",
    "copper_termination",
    "cable_route",
    "cable_handhole",
    "mounted_device",
    "splice_enc_cable",
    "circuit",
}

GRAPH_ROLES = (
    "fiber_strand",
    "fiber_splice",
    "cable_route",
    "cable_handhole",
    "circuit",
    "splice_enc",
    "splice_enc_cable",
)


def plain(value):
    if value is None:
        return None
    if type(value).__name__ in ("QPyNullVariant", "NULL"):
        return None
    if not isinstance(value, (str, int, float, bool)) and hasattr(value, "isNull"):
        try:
            if value.isNull():
                return None
        except Exception:
            pass
    return value


def geometry_kind(layer: QgsVectorLayer) -> str:
    kind = QgsWkbTypes.geometryType(layer.wkbType())
    if kind == QgsWkbTypes.LineGeometry:
        return "Line"
    if kind == QgsWkbTypes.PointGeometry:
        return "Point"
    if kind == QgsWkbTypes.PolygonGeometry:
        return "Polygon"
    return "None"


def gpkg_path(layer: QgsVectorLayer):
    source = (layer.source() or "").split("|")[0]
    if source.lower().endswith(".gpkg") and os.path.exists(source):
        return source
    return None


def scan(project: QgsProject):
    layers = {}
    infos = []
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        info = inspect_layer(
            layer.id(),
            layer.name(),
            geometry_kind(layer),
            [field.name() for field in layer.fields()],
            layer.source() or "",
        )
        layers[layer.id()] = layer
        infos.append(info)
    return layers, infos


def read_features(project: QgsProject, layers: dict, infos: list) -> list[dict]:
    features = []
    for info in infos:
        if info.role is None or info.role in TABLE_ROLES:
            continue
        layer = layers.get(info.layer_id)
        if layer is None:
            continue
        for qgs_feature in layer.getFeatures():
            attrs = {field.name(): plain(qgs_feature[field.name()]) for field in layer.fields()}
            values = {}
            for canonical, actual in info.field_map.items():
                values[canonical] = attrs.get(actual)
            identifier = _first_id(info.role, attrs) or text_of(values.get("id"))
            features.append(
                {
                    "layer_id": info.layer_id,
                    "layer_name": info.name,
                    "fid": qgs_feature.id(),
                    "role": info.role,
                    "geometry": info.geometry_type,
                    "coords": _coords(qgs_feature, layer, project),
                    "id": identifier,
                    "fields": dict(info.field_map),
                    "values": values,
                }
            )
    return features


def read_tables(layers: dict, infos: list) -> dict:
    tables = {
        "circuits": [],
        "splices": [],
        "routes": [],
        "strands": [],
        "terminations": [],
        "mounts": [],
        "memberships": [],
        "handholes": [],
    }
    role_to_bucket = {
        "circuit": "circuits",
        "fiber_splice": "splices",
        "cable_route": "routes",
        "cable_handhole": "handholes",
        "fiber_strand": "strands",
        "fiber_termination": "terminations",
        "copper_termination": "terminations",
        "mounted_device": "mounts",
        "splice_enc_cable": "memberships",
    }
    for info in infos:
        bucket = role_to_bucket.get(info.role)
        if not bucket:
            continue
        layer = layers.get(info.layer_id)
        if layer is None:
            continue
        for qgs_feature in layer.getFeatures():
            row = {field.name(): plain(qgs_feature[field.name()]) for field in layer.fields()}
            if bucket in ("handholes", "circuits"):
                row["_fid"] = qgs_feature.id()
                row["_layer_id"] = info.layer_id
            tables[bucket].append(row)
    return tables


def existing_keys(features: list[dict], tables: dict) -> dict:
    cable_layers = [feature["layer_id"] for feature in features if feature.get("role") == "cable"]
    only_cable = cable_layers[0] if len(set(cable_layers)) == 1 else ""
    strands = set()
    for row in tables.get("strands") or []:
        cable_id = text_of(row.get("cable_id"))
        number = row.get("strand_no")
        if not cable_id or number in (None, ""):
            continue
        source = text_of(row.get("source_layer_id")) or only_cable
        strands.add((source, cable_id.casefold(), int(number)))
    routes = set()
    for row in tables.get("routes") or []:
        cable_id = text_of(row.get("cable_id"))
        if not cable_id:
            continue
        source = text_of(row.get("cable_source_layer_id") or row.get("cable_layer_id")) or only_cable
        routes.add((source, cable_id.casefold(), text_of(row.get("segment_layer_id")) or "", row.get("segment_fid")))
    circuits = set()
    for row in tables.get("circuits") or []:
        device_id = text_of(row.get("device_id"))
        cable_id = text_of(row.get("cable_id"))
        if device_id and cable_id:
            circuits.add((device_id.casefold(), cable_id.casefold()))
    mounts = set()
    for row in tables.get("mounts") or []:
        device_id = text_of(row.get("device_id"))
        support_id = text_of(row.get("support_id"))
        if device_id and support_id:
            mounts.add((device_id.casefold(), support_id.casefold()))
    return {"strands": strands, "routes": routes, "circuits": circuits, "mounts": mounts}


class EditSession:
    def __init__(self):
        self.layers = []

    def edit(self, layer: QgsVectorLayer) -> QgsVectorLayer:
        if layer not in self.layers:
            if not layer.isEditable() and not layer.startEditing():
                raise RuntimeError(f"{layer.name()} is not editable.")
            layer.beginEditCommand("OSP Connectivity")
            self.layers.append(layer)
        return layer

    def commit(self) -> list[str]:
        """Close the undo step and leave the layers editable so one Undo can reverse it."""
        for layer in self.layers:
            if layer.isEditable():
                layer.endEditCommand()
                layer.updateExtents()
                layer.triggerRepaint()
        return []

    def rollback(self) -> None:
        for layer in self.layers:
            if layer.isEditable():
                layer.destroyEditCommand()


def ensure_project(project: QgsProject, layers: dict, infos: list):
    """Add missing template fields, then create only the link tables this project does not have."""
    _ensure_cable_end_fields(layers, infos)
    layers, infos = scan(project)
    store = {}
    for info in infos:
        if info.role in GRAPH_ROLES or info.role in ("mounted_device", "copper_termination", "fiber_termination"):
            store.setdefault(info.role, layers[info.layer_id])

    native_graph = "fiber_splice" in store or "copper_termination" in store
    missing = [role for role in GRAPH_ROLES if role not in store and role != "cable_handhole"]
    need_handhole = "cable_handhole" not in store
    if not missing and not need_handhole:
        _register_strand_relation(project, store, infos)
        return layers, infos, store

    if native_graph:
        missing = [role for role in missing if role == "circuit"]
        target = _cable_gpkg(layers, infos) or _sidecar_path(project)
    else:
        target = _sidecar_path(project)
    if missing and not target:
        raise RuntimeError("Save the QGIS project before building associations.")

    specs = _layer_specs(project)
    for role in missing:
        layer_name, title, wkb, fields = specs[role]
        _write_layer(project, target, layer_name, title, wkb, fields)
    if need_handhole:
        sidecar = _sidecar_path(project)
        if not sidecar:
            raise RuntimeError("Save the QGIS project before building associations.")
        layer_name, title, wkb, fields = specs["cable_handhole"]
        _write_layer(project, sidecar, layer_name, title, wkb, fields)
    layers, infos = scan(project)
    store = {}
    for info in infos:
        if info.role in GRAPH_ROLES or info.role in ("mounted_device", "copper_termination", "fiber_termination"):
            store.setdefault(info.role, layers[info.layer_id])
    _register_strand_relation(project, store, infos)
    return layers, infos, store


def apply_edits(session: EditSession, layers: dict, infos: list, edits: list) -> list[str]:
    by_id = {info.layer_id: info for info in infos}
    skipped = []
    for edit in edits:
        info = by_id.get(edit.layer_id)
        layer = layers.get(edit.layer_id)
        actual = info.field_map.get(edit.canonical) if info else None
        if layer is None or not actual:
            skipped.append(edit.note)
            continue
        index = layer.fields().indexFromName(actual)
        if index < 0:
            skipped.append(edit.note)
            continue
        session.edit(layer)
        layer.changeAttributeValue(edit.fid, index, edit.value)
    return skipped


def delete_features(session: EditSession, layers: dict, pairs: list) -> None:
    grouped = {}
    for layer_id, fid in pairs:
        if layer_id is None or fid is None:
            continue
        grouped.setdefault(layer_id, []).append(fid)
    for layer_id, fids in grouped.items():
        layer = layers.get(layer_id)
        if layer is None or not fids:
            continue
        session.edit(layer)
        layer.deleteFeatures(fids)


def update_feature_fields(session: EditSession, layers: dict, updates: list) -> None:
    for update in updates:
        layer = layers.get(update.get("layer_id"))
        if layer is None or update.get("fid") is None:
            continue
        session.edit(layer)
        for name, value in (update.get("values") or {}).items():
            index = layer.fields().indexFromName(name)
            if index >= 0:
                layer.changeAttributeValue(update["fid"], index, value)


def apply_inserts(session: EditSession, store: dict, inserts: list) -> int:
    written = 0
    for insert in inserts:
        layer = store.get(insert.role)
        if layer is None:
            continue
        if insert.role == "cable_route" and not insert.values.get("segment_id"):
            if layer.fields().indexFromName("segment_fid") < 0:
                continue
        session.edit(layer)
        feature = QgsFeature(layer.fields())
        for key, value in insert.values.items():
            index = layer.fields().indexFromName(key)
            if index >= 0 and value is not None:
                feature.setAttribute(index, value)
        if layer.addFeature(feature):
            written += 1
    return written


def apply_plain_edits(session: EditSession, layers: dict, infos: list, edits: list) -> list[str]:
    """Same as apply_edits, but the edits are dicts from the associate step."""
    from .core.link import FieldEdit

    return apply_edits(
        session,
        layers,
        infos,
        [
            FieldEdit(edit["layer_id"], edit["fid"], edit["canonical"], edit["value"], edit["canonical"])
            for edit in edits
        ],
    )


def line_geometry(coords, project: QgsProject, layer: QgsVectorLayer) -> QgsGeometry:
    geometry = QgsGeometry.fromPolylineXY([QgsPointXY(point[0], point[1]) for point in coords])
    if project.crs() != layer.crs():
        geometry.transform(QgsCoordinateTransform(project.crs(), layer.crs(), project))
    return geometry


def point_geometry(point, project: QgsProject, layer: QgsVectorLayer) -> QgsGeometry:
    geometry = QgsGeometry.fromPointXY(QgsPointXY(point[0], point[1]))
    if project.crs() != layer.crs():
        geometry.transform(QgsCoordinateTransform(project.crs(), layer.crs(), project))
    return geometry


def split_cable(session, project, layers, infos, feature: dict, parts: dict, new_id: str, enclosure_id: str) -> None:
    """Replace one cable line with the start piece and add the piece beyond the enclosure."""
    layer = layers[feature["layer_id"]]
    info = next(item for item in infos if item.layer_id == feature["layer_id"])
    session.edit(layer)
    original = layer.getFeature(feature["fid"])
    first = line_geometry(parts["a"], project, layer)
    second = line_geometry(parts["b"], project, layer)
    layer.changeGeometry(feature["fid"], first)
    created = QgsFeature(layer.fields())
    for field in layer.fields():
        if field.name().casefold() == "fid":
            continue
        created.setAttribute(field.name(), original[field.name()])
    created.setGeometry(second)
    _set_field(created, info, "id", new_id)
    _set_field(created, info, "a_end", enclosure_id)
    _set_field(created, info, "a_panel", None)
    _set_field(created, info, "a_port", None)
    _set_length(created, info, second, layer)
    if not layer.addFeature(created):
        raise RuntimeError(f"Could not add the new cable segment on {layer.name()}.")

    _change(layer, feature["fid"], info, "z_end", enclosure_id)
    _change(layer, feature["fid"], info, "z_panel", None)
    _change(layer, feature["fid"], info, "z_port", None)
    meters = _meters(first, layer)
    if meters is not None:
        _change(layer, feature["fid"], info, "length_m", round(meters, 2))
        _change(layer, feature["fid"], info, "length_f", round(meters * 3.28084, 2))


def add_enclosure_point(session, project, layer, point, enclosure_id: str) -> None:
    session.edit(layer)
    feature = QgsFeature(layer.fields())
    geometry = point_geometry(point, project, layer)
    if layer.geometryType() != QgsWkbTypes.NullGeometry:
        feature.setGeometry(geometry)
    for name, value in (
        ("splice_enc_id", enclosure_id),
        ("splice_enc_name", enclosure_id),
        ("status", "proposed"),
        ("midspan_access", 1),
        ("enclosure_function", "midspan"),
    ):
        index = layer.fields().indexFromName(name)
        if index >= 0:
            feature.setAttribute(index, value)
    if not layer.addFeature(feature):
        raise RuntimeError("Could not add the splice enclosure.")


def ensure_panel_layer(project: QgsProject, layers: dict, infos: list, fiber: bool):
    """Use the project's copper or fiber panel layer, or create one beside the project."""
    preferred = "fdp" if fiber else "patch_panel"
    for info in infos:
        if info.role == preferred:
            return layers, infos, layers[info.layer_id]
    target = _sidecar_path(project)
    if not target:
        raise RuntimeError("Save the QGIS project before adding a patch panel.")
    if fiber:
        layer_name, title = "osp_fiber_panels", "OSP Fiber Panels"
        fields = [
            QgsField("fdp_id", QVariant.String),
            QgsField("fdp_name", QVariant.String),
            QgsField("status", QVariant.String),
            QgsField("mdf_idf_id", QVariant.String),
            QgsField("media_type", QVariant.String),
        ]
    else:
        layer_name, title = "osp_patch_panels", "OSP Patch Panels"
        fields = [
            QgsField("pp_id", QVariant.String),
            QgsField("pp_name", QVariant.String),
            QgsField("status", QVariant.String),
            QgsField("mdf_idf_id", QVariant.String),
            QgsField("media_type", QVariant.String),
        ]
    _write_layer(project, target, layer_name, title, "Point", fields)
    layers, infos = scan(project)
    for info in infos:
        if info.role == preferred and layer_name in (info.source or ""):
            return layers, infos, layers[info.layer_id]
    for info in infos:
        if info.role == preferred:
            return layers, infos, layers[info.layer_id]
    raise RuntimeError("Could not open the patch panel layer after creating it.")


def add_mapped_feature(session, project, layer, info, geometry, values: dict, kind: str):
    """Add a point or line and set the canonical fields that this layer actually has."""
    session.edit(layer)
    created = QgsFeature(layer.fields())
    qgs_geometry = None
    if kind == "line" and geometry:
        qgs_geometry = line_geometry(geometry, project, layer)
    elif kind == "point" and geometry is not None:
        qgs_geometry = point_geometry(geometry, project, layer)
    if qgs_geometry is not None and layer.geometryType() != QgsWkbTypes.NullGeometry:
        created.setGeometry(qgs_geometry)
    for canonical, value in values.items():
        if value is not None:
            _set_field(created, info, canonical, value)
    if kind == "line" and qgs_geometry is not None:
        _set_length(created, info, qgs_geometry, layer)
    if not layer.addFeature(created):
        raise RuntimeError(f"Could not add the feature on {layer.name()}.")
    return created.id()


def ensure_id_field(layer, role: str) -> None:
    """Give a geometry-only layer an id field so a new device name can be stored."""
    from .core.fields import id_candidates

    names = {field.name().casefold() for field in layer.fields()}
    candidates = id_candidates(role) or ["device_id"]
    if any(candidate.casefold() in names for candidate in candidates):
        return
    added = layer.dataProvider().addAttributes(
        [
            QgsField(candidates[0], QVariant.String),
            QgsField("device_name", QVariant.String),
            QgsField("status", QVariant.String),
        ]
    )
    if not added:
        raise RuntimeError(f"Could not add an id field on {layer.name()}.")
    layer.updateFields()


def add_panel_feature(session, project, layer, info, point, panel_id: str, idf_id: str, media: str) -> None:
    if point is None:
        raise RuntimeError(f"{idf_id} has no location, so the patch panel was not created.")
    session.edit(layer)
    created = QgsFeature(layer.fields())
    if layer.geometryType() != QgsWkbTypes.NullGeometry:
        created.setGeometry(point_geometry(point, project, layer))
    _set_field(created, info, "id", panel_id)
    _set_field(created, info, "name", panel_id)
    _set_field(created, info, "idf_ref", idf_id)
    _set_field(created, info, "status", "proposed")
    _set_field(created, info, "media", "fiber" if media == "fiber" else "copper")
    if not layer.addFeature(created):
        raise RuntimeError("Could not add the patch panel.")


def _first_id(role, attrs: dict):
    lookup = {name.casefold(): name for name in attrs}
    for candidate in id_candidates(role):
        actual = lookup.get(candidate.casefold())
        if actual and text_of(attrs.get(actual)):
            return text_of(attrs.get(actual))
    return None


def _coords(qgs_feature, layer, project):
    geometry = qgs_feature.geometry()
    if geometry is None or geometry.isEmpty():
        return None
    geometry = QgsGeometry(geometry)
    if layer.crs() != project.crs():
        geometry.transform(QgsCoordinateTransform(layer.crs(), project.crs(), project))
    kind = geometry.type()
    if kind == QgsWkbTypes.LineGeometry:
        if geometry.isMultipart():
            parts = geometry.asMultiPolyline()
            polyline = parts[0] if parts else []
        else:
            polyline = geometry.asPolyline()
        return [(point.x(), point.y()) for point in polyline]
    if kind == QgsWkbTypes.PointGeometry:
        if geometry.isMultipart():
            points = geometry.asMultiPoint()
            point = points[0] if points else None
        else:
            point = geometry.asPoint()
        return [(point.x(), point.y())] if point is not None else None
    if kind == QgsWkbTypes.PolygonGeometry:
        point = geometry.centroid().asPoint()
        return [(point.x(), point.y())]
    return None


def _ensure_cable_end_fields(layers, infos) -> None:
    for info in infos:
        if info.role != "cable":
            continue
        layer = layers.get(info.layer_id)
        if layer is None:
            continue
        names = {field.name().casefold() for field in layer.fields()}
        if "cable_id" not in names:
            continue
        if "a_end_location" in names or "a_end_id" in names:
            if "strand_count" not in names and "fiber_count" not in names:
                if not layer.dataProvider().addAttributes([QgsField("strand_count", QVariant.Int)]):
                    raise RuntimeError(f"Could not add strand_count on {layer.name()}.")
                layer.updateFields()
            continue
        added = layer.dataProvider().addAttributes(
            [
                QgsField("a_end_type", QVariant.String),
                QgsField("a_end_id", QVariant.String),
                QgsField("z_end_type", QVariant.String),
                QgsField("z_end_id", QVariant.String),
                QgsField("strand_count", QVariant.Int),
            ]
        )
        if not added:
            raise RuntimeError(f"Could not add cable end fields on {layer.name()}.")
        layer.updateFields()


def _cable_gpkg(layers, infos):
    for info in infos:
        if info.role == "cable":
            path = gpkg_path(layers[info.layer_id])
            if path:
                return path
    return None


def _sidecar_path(project: QgsProject):
    filename = project.fileName()
    if not filename:
        return None
    return os.path.join(os.path.dirname(filename), "osp_links.gpkg")


def _layer_specs(project: QgsProject):
    text = lambda name: QgsField(name, QVariant.String)
    integer = lambda name: QgsField(name, QVariant.Int)
    strand_fields = [
        text("strand_id"),
        text("source_layer_id"),
        text("cable_id"),
        integer("strand_no"),
        integer("tube_no"),
        text("fiber_color"),
        text("status"),
    ]
    splice_fields = [
        text("splice_id"),
        text("splice_enc_id"),
        text("a_layer_id"),
        text("a_cable_id"),
        integer("a_strand_no"),
        text("b_layer_id"),
        text("b_cable_id"),
        integer("b_strand_no"),
        text("splice_type"),
        text("status"),
    ]
    route_fields = [
        text("cable_source_layer_id"),
        text("cable_id"),
        text("segment_layer_id"),
        integer("segment_fid"),
        text("segment_id"),
        integer("route_order"),
    ]
    handhole_fields = [
        text("cable_source_layer_id"),
        text("cable_id"),
        text("handhole_layer_id"),
        integer("handhole_fid"),
        text("handhole_id"),
        integer("route_order"),
    ]
    circuit_fields = [
        text("circuit_id"),
        text("media_type"),
        text("device_layer_id"),
        text("device_id"),
        text("idf_layer_id"),
        text("idf_id"),
        text("panel_id"),
        text("panel_port"),
        text("cable_layer_id"),
        text("cable_id"),
        integer("strand_from"),
        integer("strand_to"),
        text("status"),
    ]
    enclosure_fields = [
        text("splice_enc_id"),
        text("splice_enc_name"),
        text("status"),
        integer("midspan_access"),
        text("enclosure_function"),
    ]
    membership_fields = [
        text("splice_enc_id"),
        text("cable_layer_id"),
        text("cable_id"),
    ]
    return {
        "fiber_strand": ("osp_fiber_strands", "OSP Fiber Strands", "None", strand_fields),
        "fiber_splice": ("osp_fiber_splices", "OSP Fiber Splices", "None", splice_fields),
        "cable_route": ("osp_cable_routes", "OSP Cable Routes", "None", route_fields),
        "cable_handhole": ("osp_cable_handholes", "OSP Cable Handholes", "None", handhole_fields),
        "circuit": ("osp_circuits", "OSP Circuits", "None", circuit_fields),
        "splice_enc": ("osp_splice_enc", "OSP Splice Enclosures", "Point", enclosure_fields),
        "splice_enc_cable": ("osp_splice_enc_cables", "OSP Enclosure Cables", "None", membership_fields),
    }


def _write_layer(project, path, layer_name, title, wkb, fields) -> None:
    uri = "None" if wkb == "None" else f"{wkb}?crs={project.crs().authid()}"
    memory = QgsVectorLayer(uri, layer_name, "memory")
    if not memory.isValid():
        raise RuntimeError(f"Could not prepare {title}.")
    memory.dataProvider().addAttributes(fields)
    memory.updateFields()
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = layer_name
    options.actionOnExistingFile = (
        QgsVectorFileWriter.CreateOrOverwriteLayer
        if os.path.exists(path)
        else QgsVectorFileWriter.CreateOrOverwriteFile
    )
    error, message, _new_path, _new_layer = QgsVectorFileWriter.writeAsVectorFormatV3(
        memory,
        path,
        project.transformContext(),
        options,
    )
    if error != QgsVectorFileWriter.NoError:
        raise RuntimeError(message or f"Could not create {title}.")
    loaded = QgsVectorLayer(f"{path}|layername={layer_name}", title, "ogr")
    if not loaded.isValid():
        raise RuntimeError(f"Could not open {title} after creating it.")
    project.addMapLayer(loaded)


def _register_strand_relation(project, store, infos) -> None:
    strands = store.get("fiber_strand")
    cables = [info for info in infos if info.role == "cable"]
    if strands is None or len(cables) != 1:
        return
    if "cable_id" not in [field.name() for field in strands.fields()]:
        return
    relation = QgsRelation()
    relation.setId("osp_cable_strands")
    relation.setName("Fiber Strands")
    relation.setReferencingLayer(strands.id())
    relation.setReferencedLayer(cables[0].layer_id)
    relation.addFieldPair("cable_id", "cable_id")
    if relation.isValid() and not project.relationManager().relation(relation.id()).isValid():
        project.relationManager().addRelation(relation)


def _set_field(feature, info, canonical, value) -> None:
    actual = info.field_map.get(canonical)
    if not actual:
        return
    index = feature.fields().indexFromName(actual)
    if index >= 0:
        feature.setAttribute(index, value)


def _change(layer, fid, info, canonical, value) -> None:
    actual = info.field_map.get(canonical)
    if not actual:
        return
    index = layer.fields().indexFromName(actual)
    if index >= 0:
        layer.changeAttributeValue(fid, index, value)


def _set_length(feature, info, geometry, layer) -> None:
    meters = _meters(geometry, layer)
    if meters is None:
        return
    for canonical, multiplier in (("length_m", 1), ("length_f", 3.28084)):
        actual = info.field_map.get(canonical)
        if not actual:
            continue
        index = feature.fields().indexFromName(actual)
        if index >= 0:
            feature.setAttribute(index, round(meters * multiplier, 2))


def _meters(geometry, layer):
    if geometry is None or geometry.isEmpty():
        return None
    if layer.crs().mapUnits() != QgsUnitTypes.DistanceMeters:
        return None
    return geometry.length()
